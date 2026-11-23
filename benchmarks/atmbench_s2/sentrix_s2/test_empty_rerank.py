"""Regression for metadata-only video parents reaching the S2 reranker."""

import hashlib
import io
import json
import sqlite3
import threading
import urllib.error
import urllib.request

import numpy as np
import pytest

from sentrix_s2.index import SCHEMA, SearchIndex
from sentrix_s2.models import LocalModels, OperationalError


class StrictModels:
    def __init__(self):
        self.calls = []
        self.documents = []

    def embed(self, texts, input_type):
        return [[1.0] + [0.0] * 2047 for _ in texts]

    def rerank(self, query, documents):
        assert documents and all(text.strip() for text in documents)
        self.documents.append(list(documents))
        # Deliberately return reverse input order to exercise original-index mapping.
        return [
            {"index": i, "relevance_score": 0.9 if "BETTER" in documents[i] else 0.1}
            for i in reversed(range(len(documents)))
        ]


def make_index(tmp_path, *, with_mail=True, matched_child=False, empty_parents=True):
    path = tmp_path / "index.sqlite3"
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    sources = []
    vectors = []
    if empty_parents:
        sources += [
            ("video-a", "video-a", "video", ""),
            ("video-b", "video-b", "video", ""),
            ("frame-unmatched", "video-a", "image", "UNRETRIEVED child caption must not be borrowed."),
        ]
        vectors += [("video-a", 1.0), ("video-b", 0.7)]
    if with_mail:
        sources += [
            ("mail-a", "mail-a", "email", "Original plain text A."),
            ("mail-b", "mail-b", "email", "Original BETTER text B."),
        ]
        vectors += [("mail-a", 0.9), ("mail-b", 0.8)]
    if matched_child:
        sources.append(("frame-matched", "video-a", "image", "MATCHED real child caption."))
        vectors.append(("frame-matched", 0.85))
    for sid, parent, kind, text in sources:
        row = {
            "source_id": sid,
            "parent_id": parent,
            "modality": kind,
            "native_asset_id": "native-" + sid,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "index_eligible": bool(text),
            "metadata": {"location_name": "published location"} if kind == "video" else {},
        }
        db.execute("INSERT INTO sources VALUES(?,?,?,?,?)", (sid, parent, kind, text, json.dumps(row)))
    for sid, similarity in vectors:
        v = np.zeros(2048, dtype="<f4")
        v[0] = similarity
        v[1] = (1 - similarity**2) ** 0.5
        channel = "native_visual" if sid == "frame-matched" else "native_episodic"
        db.execute(
            "INSERT INTO vectors VALUES(?,?,?,?)", ("vector-" + sid, json.dumps([sid]), channel, v.tobytes())
        )
    if matched_child:
        # The semantic channel retrieves the parent; a separate visual channel
        # retrieves its child (per-channel parent dedup otherwise hides the child).
        v = np.zeros(2048, dtype="<f4")
        v[0] = 1
        db.execute(
            "INSERT INTO vectors VALUES(?,?,?,?)",
            ("visual-mail", json.dumps(["mail-a"]), "native_visual", v.tobytes()),
        )
    db.executemany("INSERT INTO config VALUES(?,?)", [("build_status", "ready"), ("manifest", "{}")])
    db.commit()
    db.close()
    models = StrictModels()
    return SearchIndex(path, models), models, path


def test_two_empty_parents_do_not_abort_or_consume_reranker_indexes(tmp_path):
    index, models, path = make_index(tmp_path)
    original_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    result = index.search("source-only query", mode="vector", top_k=100)
    assert len(models.documents) == 1 and len(models.documents[0]) == 2
    assert "Original plain text A." in models.documents[0][0]
    assert "Original BETTER text B." in models.documents[0][1]
    assert result["trace"]["rrf_order"] == ["video-a", "mail-a", "mail-b", "video-b"]
    assert result["trace"]["rerank_input_card_indices"] == [1, 2]
    assert {x["index"] for x in result["trace"]["rerank"]} == {1, 2}
    assert [x["source_id"] for x in result["sources"]] == ["mail-b", "mail-a", "video-a", "video-b"]
    assert [x["score"] for x in result["sources"]] == [0.9, 0.1, None, None]
    assert result["total_candidates"] == 4
    for card in result["sources"][2:]:
        assert card["rerank_status"] == "unavailable_no_text"
        assert card["ranking_basis"] == "rrf_unscorable_tail"
        assert card["preview"]["text"] == "" and card["matched_source_ids"] == [card["source_id"]]
        assert card["source_metadata"]["metadata"]["location_name"] == "published location"
    assert "frame-unmatched" not in result["trace"]["source_ids_authorized"]
    assert not any("UNRETRIEVED" in doc for doc in models.documents[0])
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original_hash


def test_existing_matched_child_text_is_used_without_fabricating_a_caption(tmp_path):
    index, models, _ = make_index(tmp_path, matched_child=True)
    result = index.search("source-only query", mode="vector", top_k=100)
    video = next(row for row in result["sources"] if row["source_id"] == "video-a")
    assert video["preview"]["text"] == "MATCHED real child caption."
    assert video["matched_source_ids"] == ["video-a", "frame-matched"]
    assert video["modality"] == "video"
    assert any("MATCHED real child caption." in doc for doc in models.documents[0])
    assert not any("UNRETRIEVED" in doc for doc in models.documents[0])
    assert result["trace"]["rerank_unscorable_source_ids"] == ["video-b"]


def test_all_empty_parents_preserve_rrf_and_pagination_without_model_call(tmp_path):
    index, models, _ = make_index(tmp_path, with_mail=False)
    first = index.search("source-only query", mode="vector", top_k=1)
    second = index.search("source-only query", mode="vector", top_k=1, offset=1)
    assert not models.documents
    assert first["sources"][0]["source_id"] == "video-a" and first["next_offset"] == 1
    assert second["sources"][0]["source_id"] == "video-b" and second["next_offset"] is None
    assert first["result_set_id"] == second["result_set_id"]
    assert first["trace"]["rerank"] == [] and first["trace"]["rerank_input_card_indices"] == []


def test_no_empty_cards_preserve_existing_nonempty_scores_order_and_input(tmp_path):
    index, models, _ = make_index(tmp_path, empty_parents=False)
    result = index.search("source-only query", mode="vector", top_k=100)
    assert result["trace"]["rrf_order"] == ["mail-a", "mail-b"]
    assert result["trace"]["rerank_input_card_indices"] == [0, 1]
    assert result["trace"]["rerank_unscorable_source_ids"] == []
    assert [(x["source_id"], x["score"]) for x in result["sources"]] == [("mail-b", 0.9), ("mail-a", 0.1)]
    assert all("rerank_status" not in card for card in result["sources"])


def test_real_reranker_failure_is_not_replaced_with_rrf_success(tmp_path):
    index, models, _ = make_index(tmp_path)

    def fail(*args):
        raise OperationalError("real_model_failure")

    models.rerank = fail
    with pytest.raises(OperationalError, match="real_model_failure"):
        index.search("source-only query", mode="vector")


@pytest.mark.parametrize(
    "body",
    [
        b'{"error":{"code":"invalid_document","type":"operational","message":"nonempty required"}}',
        b"not-json",
    ],
)
def test_http_error_records_original_status_and_body_without_retry(monkeypatch, body):
    model = LocalModels()
    sent = []

    def fail(request, timeout):
        sent.append(request)
        raise urllib.error.HTTPError(request.full_url, 400, "Bad request", {}, io.BytesIO(body))

    monkeypatch.setattr("urllib.request.urlopen", fail)
    with pytest.raises(OperationalError) as caught:
        model.rerank("query", ["source text"])
    assert len(sent) == 1 and len(model.calls) == 1
    details = caught.value.details
    assert details["http_status"] == 400 and details["endpoint"] == "/rerank"
    assert (
        details["status"] == "operational_error"
        and details["request_sha256"] == hashlib.sha256(sent[0].data).hexdigest()
    )
    assert details["response_error"] == (json.loads(body)["error"] if body.startswith(b"{") else None)


def test_sidecar_keeps_upstream_failure_evidence_and_returns_operational(monkeypatch, tmp_path):
    from sentrix_s2 import server

    real_server = server.ThreadingHTTPServer
    ready = threading.Event()
    bound = []

    def bind(address, handler):
        httpd = real_server(address, handler)
        bound.append(httpd)
        ready.set()
        return httpd

    monkeypatch.setattr(server, "ThreadingHTTPServer", bind)
    details = {"endpoint": "/rerank", "http_status": 400, "response_error": {"code": "invalid_document"}}

    class BrokenIndex:
        revision = "fixture"
        sources = {}

        def search(self, **kwargs):
            error = OperationalError("local_model_HTTPError")
            error.details = details
            raise error

    thread = threading.Thread(
        target=server.serve, args=(BrokenIndex(), "127.0.0.1", 0, tmp_path), daemon=True
    )
    thread.start()
    assert ready.wait(3)
    try:
        request = urllib.request.Request(
            "http://127.0.0.1:%d/search" % bound[0].server_port,
            json.dumps({"query": "source-only"}).encode(),
            headers={"Content-Type": "application/json", "X-Trace-Id": "fixture-attempt"},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with pytest.raises(urllib.error.HTTPError) as caught:
            opener.open(request, timeout=3)
        assert caught.value.code == 503
        assert json.loads(caught.value.read())["error"]["type"] == "operational"
        rows = [
            json.loads(p.read_text()) for p in tmp_path.glob("*.json") if not p.name.startswith("server-")
        ]
        assert len(rows) == 1 and rows[0]["model_error"] == details
        assert rows[0]["status"] == "operational_error" and rows[0]["attempt_id"] == "fixture-attempt"
    finally:
        bound[0].shutdown()
        bound[0].server_close()
        thread.join(3)
