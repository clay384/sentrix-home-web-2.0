"""Exercise S2-Q1 over loopback with fixture inference; no external network."""

import hashlib
import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from atm_agent_q1.run import run_question
from sentrix_s2 import server
from sentrix_s2.index import SearchIndex, build
from sentrix_s2.models import LocalModels


def test_s2_q1_wire(tmp_path, monkeypatch):
    monkeypatch.setattr(
        urllib.request, "_opener", urllib.request.build_opener(urllib.request.ProxyHandler({}))
    )
    source_id = "fixture-email"
    source_ref = "src_" + hashlib.sha256(source_id.encode()).hexdigest()[:20]
    text = "The booking reference for your trip is SYN-401."
    question = "What is the booking reference for the trip?"
    fixture_calls = []

    def search(constraints, reason):
        return (
            "search_memories",
            {
                "query": "booking reference",
                "missing_field": "booking reference",
                "changed_constraint": reason,
                "constraints": constraints,
            },
        )

    decisions = iter(
        [
            [search({"source_time_after": "2030-01-01", "hard": True}, "try a hard future date")],
            [search({}, "relax the date after the empty search")],
            [
                ("read_source_text", {"source_ref": source_ref}),
                (
                    "update_evidence",
                    {
                        "slots": [
                            {
                                "field": "booking reference",
                                "status": "supported",
                                "basis": "source_text",
                                "source_refs": [source_ref],
                            }
                        ]
                    },
                ),
            ],
            [
                (
                    "final_answer",
                    {"answer": "SYN-401", "source_refs": [source_ref], "selected_source_refs": [source_ref]},
                )
            ],
        ]
    )

    class ModelFixture(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            fixture_calls.append((self.path, body))
            if self.path == "/v1/embeddings":
                vector = [1.0] + [0.0] * 2047
                result = {"data": [{"index": i, "embedding": vector} for i, _ in enumerate(body["input"])]}
            elif self.path == "/rerank":
                result = [{"index": i, "relevance_score": 0.9} for i, _ in enumerate(body["documents"])]
            elif self.path == "/v1/chat/completions":
                calls = [
                    {
                        "id": f"fixture_{i}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                    for i, (name, args) in enumerate(next(decisions))
                ]
                result = {
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": None, "tool_calls": calls},
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                }
            else:
                self.send_error(404)
                return
            payload = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("X-ATM-Model-Fingerprint", "synthetic")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    fixture = ThreadingHTTPServer(("127.0.0.1", 0), ModelFixture)
    fixture_thread = threading.Thread(target=fixture.serve_forever, daemon=True)
    fixture_thread.start()
    bound, ready = [], threading.Event()

    def bind(address, handler):
        httpd = ThreadingHTTPServer(address, handler)
        bound.append(httpd)
        ready.set()
        return httpd

    monkeypatch.setattr(server, "ThreadingHTTPServer", bind)
    sidecar_thread = None
    try:
        models_url = f"http://127.0.0.1:{fixture.server_port}"
        src = {
            "source_id": source_id,
            "parent_id": source_id,
            "modality": "email",
            "timestamp": "2024-05-13",
            "timestamp_role": "publication",
            "text": text,
            "detail": text,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
        (tmp_path / "manifest.json").write_text("{}")
        (tmp_path / "native-vectors.jsonl").write_text("")
        sources = tmp_path / "sources.jsonl"
        sources.write_text(json.dumps(src) + "\n")
        index_path = tmp_path / "S2 #1?.sqlite3"
        receipt = build(tmp_path, index_path, LocalModels(models_url, timeout=3))
        assert receipt["source_count"] == 1
        s2_traces = tmp_path / "retrieval"
        index = SearchIndex(index_path, LocalModels(models_url, timeout=3))
        sidecar_thread = threading.Thread(
            target=server.serve, args=(index, "127.0.0.1", 0, s2_traces), daemon=True
        )
        sidecar_thread.start()
        assert ready.wait(5)
        config = {
            "framework": "sentrix_s2",
            "system": "S2-Q1",
            "sources_jsonl": str(sources),
            "memory_base_url": f"http://127.0.0.1:{bound[0].server_port}",
            "gateway_url": models_url,
        }
        q1_traces = tmp_path / "q1"
        result = run_question(
            config,
            {"id": "fixture-question", "question": question},
            attempt_id="s2-wire",
            trace_dir=q1_traces,
        )
        assert result["status"] == "completed", result
        assert result["answer"] == "SYN-401"
        assert result["first_retrieval_source_ids"] == []
        assert result["first_search_empty"] is True
        assert result["first_search_query"] == question
        assert [row["candidate_ids"] for row in result["search_history"]] == [[], [source_id]]
        assert result["source_ids"] == result["selected_source_ids"] == [source_id]
        assert result["decision_rounds"] == 4
        assert result["tool_counts"]["read_source_text"] == 1
        reads = [json.loads(path.read_text()) for path in q1_traces.glob("*-tool-*.json")]
        assert text in json.dumps(next(row for row in reads if row["tool"] == "read_source_text")["result"])
        requests = [
            json.loads(path.read_text())
            for path in s2_traces.glob("*.json")
            if not path.name.startswith("server-")
        ]
        assert len(requests) == 2
        assert {row["attempt_id"] for row in requests} == {"s2-wire"}
        assert {bool(row["result"]["sources"]) for row in requests} == {False, True}
        assert [body["input_type"] for path, body in fixture_calls if path == "/v1/embeddings"] == [
            "document",
            "query",
            "query",
        ]
        assert len([1 for path, _ in fixture_calls if path == "/rerank"]) == 1
    finally:
        if bound:
            bound[0].shutdown()
            bound[0].server_close()
            sidecar_thread.join(3)
        fixture.shutdown()
        fixture.server_close()
        fixture_thread.join(3)
