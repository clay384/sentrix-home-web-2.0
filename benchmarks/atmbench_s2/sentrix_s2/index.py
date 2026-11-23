"""Sentrix S2 sidecar index: native source vectors + span BM25/Qwen vectors + parent RRF."""

import argparse
import hashlib
import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import date
from pathlib import Path

import numpy as np

from .export_native import digest
from .models import LocalModels, OperationalError
from .text import child_id, chunks, fact_spans, preview, tokens

SCHEMA = """
CREATE TABLE sources(source_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL, modality TEXT NOT NULL, text TEXT NOT NULL, row_json TEXT NOT NULL);
CREATE TABLE spans(id TEXT PRIMARY KEY, source_id TEXT NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL);
CREATE VIRTUAL TABLE lexical USING fts5(span_id UNINDEXED, body, tokenize='unicode61 remove_diacritics 2');
CREATE TABLE vectors(id TEXT PRIMARY KEY, source_ids TEXT NOT NULL, channel TEXT NOT NULL, vector BLOB NOT NULL);
CREATE TABLE config(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def build(snapshot, output, models, resume=False):
    manifest = json.loads((snapshot / "manifest.json").read_text())
    for filename in ("sources.jsonl", "native-vectors.jsonl"):
        if (
            manifest.get("files", {}).get(filename)
            and digest(snapshot / filename) != manifest["files"][filename]
        ):
            raise ValueError("frozen_snapshot_file_hash_mismatch")
    snapshot_hash = digest(snapshot / "manifest.json")
    if output.exists():
        if not resume:
            raise ValueError("index_exists_use_new_version")
        db = sqlite3.connect(output)
        row = db.execute("SELECT value FROM config WHERE key='snapshot_hash'").fetchone()
        if row is None or row[0] != snapshot_hash:
            raise ValueError("resume_snapshot_mismatch")
        if db.execute("SELECT value FROM config WHERE key='build_status'").fetchone()[0] == "ready":
            result = json.loads(db.execute("SELECT value FROM config WHERE key='manifest'").fetchone()[0])
            db.close()
            return {**result, "index_sha256": digest(output), "already_ready": True}
        return finish_build(db, snapshot, output, models)
    output.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(output)
    db.executescript(SCHEMA)
    with (snapshot / "sources.jsonl").open() as stream:
        source_rows = list(map(json.loads, stream))
    with db:
        for source in source_rows:
            sid = source["source_id"]
            text = source["text"]
            parent = source.get("parent_id") or sid
            if hashlib.sha256(text.encode()).hexdigest() != source["text_sha256"]:
                raise ValueError("snapshot_text_hash_mismatch")
            db.execute(
                "INSERT INTO sources VALUES(?,?,?,?,?)",
                (sid, parent, source["modality"], text, json.dumps(source, ensure_ascii=False)),
            )
            values = chunks(text)
            # Cards are extractive sentences with exact provenance, not generated interpretations.
            if source["modality"] in ("email", "text"):
                values += fact_spans(text)
            for start, end, kind, value in values:
                cid = child_id(sid, start, end, kind, value)
                db.execute("INSERT INTO spans VALUES(?,?,?,?,?,?)", (cid, sid, start, end, kind, value))
                db.execute("INSERT INTO lexical(span_id,body) VALUES(?,?)", (cid, " ".join(tokens(value))))
        with (snapshot / "native-vectors.jsonl").open() as vector_stream:
            for row in map(json.loads, vector_stream):
                vector = np.asarray(row["vector"], dtype="<f4")
                if (
                    vector.shape != (2048,)
                    or not np.isfinite(vector).all()
                    or abs(float(vector @ vector) - 1) > 0.02
                ):
                    raise ValueError("native_vector_invalid")
                # Native visual, semantic and episodic rows remain distinct in the trace.
                db.execute(
                    "INSERT INTO vectors VALUES(?,?,?,?)",
                    (
                        row["native_vector_id"],
                        json.dumps(row["source_ids"]),
                        row["channel"],
                        vector.tobytes(),
                    ),
                )
        db.executemany(
            "INSERT INTO config VALUES(?,?)",
            [("build_status", "building"), ("snapshot_hash", snapshot_hash), ("model_calls", "[]")],
        )
    return finish_build(db, snapshot, output, models)


def finish_build(db, snapshot, output, models):
    previous = json.loads(db.execute("SELECT value FROM config WHERE key='model_calls'").fetchone()[0])
    pending = db.execute(
        "SELECT s.id,s.source_id,s.text FROM spans s LEFT JOIN vectors v ON s.id=v.id WHERE v.id IS NULL ORDER BY s.rowid"
    ).fetchall()
    for offset in range(0, len(pending), 8):
        batch = pending[offset : offset + 8]
        try:
            vectors = models.embed([x[2] for x in batch], "document")
            identities = {
                x.get("service_fingerprint")
                for x in previous + models.calls
                if x.get("status") == "completed" and x.get("service_fingerprint")
            }
            if len(identities) > 1:
                raise OperationalError("model_identity_changed_during_build")
            with db:
                for (cid, sid, _), vector in zip(batch, vectors):
                    db.execute(
                        "INSERT INTO vectors VALUES(?,?,?,?)",
                        (cid, json.dumps([sid]), "span_semantic", np.asarray(vector, dtype="<f4").tobytes()),
                    )
        finally:
            with db:
                db.execute(
                    "UPDATE config SET value=? WHERE key='model_calls'",
                    (json.dumps(previous + models.calls),),
                )
    with db:
        db.execute("UPDATE config SET value='ready' WHERE key='build_status'")
        config = {
            "source_count": db.execute("SELECT count(*) FROM sources").fetchone()[0],
            "parent_count": db.execute("SELECT count(DISTINCT parent_id) FROM sources").fetchone()[0],
            "span_count": db.execute("SELECT count(*) FROM spans").fetchone()[0],
            "vector_count": db.execute("SELECT count(*) FROM vectors").fetchone()[0],
            "lexical": "SQLite FTS5 bm25 k1=1.2 b=.75; normalized Latin exact codes+components; CJK uni/bigrams",
            "chunking": "600 estimated multilingual lexical tokens, 80 overlap, paragraph/sentence preferred; exact source char ranges",
            "caption_policy": "same source-only C1 plus shared registered frame captions; no C2 or question-conditioned caption",
            "snapshot_manifest": json.loads((snapshot / "manifest.json").read_text()),
            "model_calls": previous + models.calls,
        }
        db.execute("INSERT INTO config VALUES(?,?)", ("manifest", json.dumps(config)))
    db.close()
    config["index_sha256"] = digest(output)
    output.with_suffix(".manifest.json").write_text(json.dumps(config, ensure_ascii=False, indent=2))
    return config


class SearchIndex:
    def __init__(self, path, models):
        self.path = Path(path)
        self.models = models
        self.revision = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with closing(self.connection()) as db:
            if db.execute("SELECT value FROM config WHERE key='build_status'").fetchone()[0] != "ready":
                raise ValueError("incomplete_s2_index")
            manifest = json.loads(db.execute("SELECT value FROM config WHERE key='manifest'").fetchone()[0])
            identities = {
                x.get("service_fingerprint")
                for x in manifest.get("model_calls", [])
                if x.get("service_fingerprint")
            }
            if len(identities) > 1:
                raise ValueError("index_model_identity_changed_during_build")
            self.model_identity = next(iter(identities), None)
            self.sources = {
                x["source_id"]: {**json.loads(x["row_json"]), "text": x["text"], "parent_id": x["parent_id"]}
                for x in db.execute("SELECT * FROM sources")
            }
            self.spans = {x["id"]: dict(x) for x in db.execute("SELECT * FROM spans")}
            rows = db.execute("SELECT * FROM vectors ORDER BY id").fetchall()
        self.vector_meta = [
            {"id": r["id"], "source_ids": json.loads(r["source_ids"]), "channel": r["channel"]} for r in rows
        ]
        self.matrix = (
            np.stack([np.frombuffer(r["vector"], dtype="<f4") for r in rows])
            if rows
            else np.zeros((0, 2048), dtype=np.float32)
        )
        self.parents = {}
        for sid, row in self.sources.items():
            self.parents.setdefault(row["parent_id"], []).append(sid)

    def connection(self):
        db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        return db

    def allowed(self, sid, constraints):
        row = self.sources[sid]
        effective_modality = "video" if row["parent_id"] != sid else row["modality"]
        if constraints.get("modality") and effective_modality != constraints["modality"]:
            return False
        parent = self.sources.get(row["parent_id"], {})
        stamp = str(row.get("timestamp") or parent.get("timestamp") or "")[:10]
        for key, operation in [
            ("source_time_after", lambda a, b: a >= b),
            ("source_time_before", lambda a, b: a <= b),
        ]:
            if constraints.get(key) and (not stamp or not operation(stamp, constraints[key])):
                return False
        return True

    def search(self, query, queries=None, mode="hybrid", top_k=10, offset=0, constraints=None):
        call_start = len(self.models.calls)
        queries = list(dict.fromkeys([query] + (queries or [])))
        if len(queries) > 3 or any(not isinstance(q, str) or not 1 <= len(q) <= 200 for q in queries):
            raise ValueError("one_original_plus_at_most_two_200_char_queries_required")
        if (
            mode not in ("hybrid", "bm25", "vector")
            or type(top_k) is not int
            or not 1 <= top_k <= 100
            or type(offset) is not int
            or offset < 0
        ):
            raise ValueError("invalid_search_mode_or_page")
        constraints = {} if constraints is None else constraints
        if not isinstance(constraints, dict):
            raise ValueError("constraints_must_be_object")
        if set(constraints) - {"modality", "source_time_after", "source_time_before", "hard"}:
            raise ValueError("unknown_constraint")
        if "hard" in constraints and type(constraints["hard"]) is not bool:
            raise ValueError("hard_must_be_boolean")
        if "modality" in constraints and constraints["modality"] not in ("image", "video", "email", "text"):
            raise ValueError("invalid_modality")
        for key in ("source_time_after", "source_time_before"):
            if key in constraints:
                value = constraints[key]
                if (
                    not isinstance(value, str)
                    or len(value) != 10
                    or date.fromisoformat(value).isoformat() != value
                ):
                    raise ValueError("source_time_requires_ISO_date")
        if constraints.get("source_time_after", "") > constraints.get("source_time_before", "9999-12-31"):
            raise ValueError("source_time_range_reversed")
        allowed = {sid for sid in self.sources if self.allowed(sid, constraints)}
        hard = constraints.get("hard", False)
        channel_rows = []
        if mode != "bm25":
            query_vectors = self.models.embed(queries, "query")
        for qi, q in enumerate(queries):
            if mode != "vector":
                terms = list(dict.fromkeys(tokens(q)))
                expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
                with closing(self.connection()) as db:
                    hits = (
                        db.execute(
                            "SELECT span_id,bm25(lexical) AS bm25 FROM lexical WHERE lexical MATCH ? ORDER BY bm25(lexical),span_id",
                            (expression,),
                        ).fetchall()
                        if expression
                        else []
                    )
                rows = []
                for h in hits:
                    span = self.spans[h["span_id"]]
                    sid = span["source_id"]
                    if hard and sid not in allowed:
                        continue
                    rows.append(
                        {
                            "source_id": sid,
                            "parent_id": self.sources[sid]["parent_id"],
                            "span_id": span["id"],
                            "score": float(h["bm25"]),
                        }
                    )
                channel_rows.append(
                    {
                        "query_index": qi,
                        "query": q,
                        "channel": "bm25",
                        "score_order": "ascending",
                        "hits": self._parent_top(rows, 50),
                    }
                )
            if mode != "bm25":
                scores = self.matrix @ np.asarray(query_vectors[qi], dtype=np.float32)
                grouped = {"semantic": [], "visual": []}
                for i in np.argsort(-scores, kind="stable"):
                    meta = self.vector_meta[int(i)]
                    channel = "visual" if meta["channel"] == "native_visual" else "semantic"
                    for sid in meta["source_ids"]:
                        if hard and sid not in allowed:
                            continue
                        grouped[channel].append(
                            {
                                "source_id": sid,
                                "parent_id": self.sources[sid]["parent_id"],
                                "vector_id": meta["id"],
                                "native_channel": meta["channel"],
                                "span_id": meta["id"] if meta["id"] in self.spans else None,
                                "score": float(scores[i]),
                            }
                        )
                for channel, rows in grouped.items():
                    channel_rows.append(
                        {
                            "query_index": qi,
                            "query": q,
                            "channel": channel,
                            "score_order": "descending",
                            "hits": self._parent_top(rows, 50),
                        }
                    )
        fused = defaultdict(float)
        evidence = defaultdict(list)
        for channel in channel_rows:
            for rank, hit in enumerate(channel["hits"], 1):
                parent = hit["parent_id"]
                sid = hit["source_id"]
                modality = self.sources[sid]["modality"]
                # Normalize available modality channels; avoid inherited photo-dominant 2.5 weight.
                denominator = (
                    (3 if modality == "image" else 2)
                    if mode == "hybrid"
                    else (2 if modality == "image" and mode == "vector" else 1)
                )
                fused[parent] += 1 / (60 + rank) / denominator
                evidence[parent].append(
                    {
                        **hit,
                        "channel": channel["channel"],
                        "query_index": channel["query_index"],
                        "rank": rank,
                    }
                )
        if constraints and not hard:
            # A matching source-date/modality is a weak ranking hint, never a silent exclusion.
            for parent in fused:
                if any(sid in allowed for sid in self.parents[parent]):
                    fused[parent] += 1 / 61 / 4
        ranked = sorted(fused, key=lambda sid: (-fused[sid], sid))[:100]
        documents = []
        cards = []
        combined = " ".join(queries)
        for parent in ranked:
            matches = sorted(evidence[parent], key=lambda x: (x["rank"], x["query_index"], x["source_id"]))
            source_ids = list(dict.fromkeys(x["source_id"] for x in matches))
            snippets = []
            for hit in matches:
                if hit.get("span_id") in self.spans:
                    span = self.spans[hit["span_id"]]
                    if not any(s["span_id"] == span["id"] for s in snippets):
                        snippets.append(
                            {
                                "span_id": span["id"],
                                "source_id": span["source_id"],
                                "offset": span["start"],
                                "end": span["end"],
                                "kind": span["kind"],
                                "text": span["text"],
                            }
                        )
                if len(snippets) >= 3:
                    break
            # An event vector can legitimately retrieve a metadata-only video
            # parent. Prefer real text from an already matched child/source; do
            # not borrow a caption from an unreturned frame to fill an empty card.
            sid = next((sid for sid in source_ids if self.sources[sid]["text"].strip()), source_ids[0])
            source = self.sources[sid]
            excerpt = preview(source["text"], combined)
            documents.append("\n".join([excerpt["text"]] + [x["text"] for x in snippets]))
            cards.append(
                {
                    "source_id": parent,
                    "matched_source_ids": source_ids,
                    "native_id": source.get("native_asset_id", sid),
                    "modality": "video" if parent != sid else source["modality"],
                    "parent_id": parent,
                    "preview": excerpt,
                    "matched_spans": snippets,
                    "source_metadata": {
                        k: source.get(k)
                        for k in ["timestamp", "timestamp_role", "timestamp_basis", "timezone", "metadata"]
                    },
                    "rrf_score": fused[parent],
                    "channels": matches,
                }
            )
        scorable = [i for i, document in enumerate(documents) if document.strip()]
        scored = self.models.rerank(query, [documents[i] for i in scorable]) if scorable else []
        # The service indexes its reduced input, not the original candidate list.
        rerank = [{**row, "index": scorable[row["index"]]} for row in scored]
        model_calls = self.models.calls[call_start:]
        if self.model_identity and any(
            x.get("service_fingerprint") != self.model_identity for x in model_calls
        ):
            raise OperationalError("model_identity_changed_since_index_build")
        results = [{**cards[x["index"]], "score": x["relevance_score"]} for x in rerank]
        # Sort explicitly even if a TEI-compatible service returns input order with scores.
        results.sort(key=lambda x: (-x["score"], -x["rrf_score"], x["source_id"]))
        unscorable = [i for i, document in enumerate(documents) if not document.strip()]
        # Keep legal no-text parents and their provenance. They remain browseable
        # in original RRF order after scored cards; None is not a fabricated zero.
        results.extend(
            {
                **cards[i],
                "score": None,
                "rerank_status": "unavailable_no_text",
                "ranking_basis": "rrf_unscorable_tail",
            }
            for i in unscorable
        )
        for rank, row in enumerate(results, 1):
            row["rank"] = rank
        key = hashlib.sha256(
            json.dumps([self.revision, queries, mode, constraints], sort_keys=True).encode()
        ).hexdigest()[:24]
        return {
            "result_set_id": "s2_" + key,
            "index_revision": self.revision,
            "sources": results[offset : offset + top_k],
            "offset": offset,
            "next_offset": offset + top_k if offset + top_k < len(results) else None,
            "total_candidates": len(results),
            "queries": queries,
            "retrieval_mode": mode,
            "constraints": constraints,
            "trace": {
                "model_calls": model_calls,
                "channels": channel_rows,
                "rrf_order": ranked,
                "rerank": rerank,
                "parent_dedup_before_rerank": True,
                "rerank_input_card_indices": scorable,
                "rerank_unscorable_source_ids": [cards[i]["source_id"] for i in unscorable],
                "rerank_order": [x["source_id"] for x in results],
                "source_ids_authorized": list(
                    dict.fromkeys(
                        sid for card in results[offset : offset + top_k] for sid in card["matched_source_ids"]
                    )
                ),
            },
        }

    @staticmethod
    def _parent_top(rows, limit):
        result = []
        seen = set()
        for row in rows:
            if row["parent_id"] not in seen:
                seen.add(row["parent_id"])
                result.append(row)
                if len(result) == limit:
                    break
        return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--snapshot", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--models", default="http://127.0.0.1:19601")
    p.add_argument("--resume", action="store_true")
    a = p.parse_args()
    print(json.dumps(build(a.snapshot, a.output, LocalModels(a.models), resume=a.resume), ensure_ascii=False))
