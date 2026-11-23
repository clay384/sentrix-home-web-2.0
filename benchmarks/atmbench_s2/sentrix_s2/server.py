"""Loopback S2 retrieval service with immutable versioned indexes and complete retrieval traces."""

import argparse
import copy
import hashlib
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .index import SearchIndex
from .models import LocalModels, OperationalError


def serve(index, host, port, evidence):
    evidence.mkdir(parents=True, exist_ok=True)
    implementation = {p.name: p.read_text() for p in Path(__file__).parent.glob("*.py")}
    revision = hashlib.sha256(json.dumps(implementation, sort_keys=True).encode()).hexdigest()
    code_path = evidence / ("server-" + revision + "-implementation.json")
    if not code_path.exists():
        with code_path.open("x") as stream:
            json.dump(implementation, stream, ensure_ascii=False, indent=2)
    cache = {}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def emit(self, status, body):
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path != "/health":
                return self.emit(404, {"error": "not_found"})
            self.emit(
                200,
                {
                    "status": "ready",
                    "index_revision": index.revision,
                    "sources": len(index.sources),
                    "port": port,
                    "implementation_revision": revision,
                },
            )

        def do_POST(self):
            if self.path != "/search":
                return self.emit(404, {"error": "not_found"})
            trace = {
                "implementation_revision": revision,
                "request_id": uuid.uuid4().hex,
                "attempt_id": self.headers.get("X-Trace-Id"),
                "entity_id": self.headers.get("X-User-Name"),
            }
            start = time.monotonic()
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length < 32768:
                    raise ValueError("request_size")
                request = json.loads(self.rfile.read(length))
                trace["request"] = request
                if not isinstance(request, dict):
                    raise ValueError("request_not_object")
                offset = request.get("offset", 0)
                top_k = request.get("top_k", 10)
                if type(offset) is not int or offset < 0 or type(top_k) is not int or not 1 <= top_k <= 100:
                    raise ValueError("invalid_page")
                whole_request = {**request, "offset": 0, "top_k": 100}
                key = json.dumps(whole_request, sort_keys=True, ensure_ascii=False)
                # Serial admission prevents duplicate same-query computations and protects shared model RAM.
                with lock:
                    hit = key in cache
                    result = copy.deepcopy(cache[key]) if hit else index.search(**whole_request)
                    if not hit:
                        if len(cache) >= 64:
                            cache.pop(next(iter(cache)))
                        cache[key] = copy.deepcopy(result)
                result["sources"] = result["sources"][offset : offset + top_k]
                result["offset"] = offset
                result["next_offset"] = (
                    offset + top_k if offset + top_k < result["total_candidates"] else None
                )
                result["trace"]["source_ids_authorized"] = list(
                    dict.fromkeys(sid for row in result["sources"] for sid in row["matched_source_ids"])
                )
                result["cache_hit"] = hit
                trace.update(status="completed", result=result)
                status = 200
            except (ValueError, TypeError, KeyError) as error:
                result = {"error": {"type": "parameter", "message": str(error)}}
                status = 422
                trace.update(status="parameter_error", result=result)
            except Exception as error:
                result = {
                    "error": {"type": "operational", "message": type(error).__name__ + ":" + str(error)}
                }
                status = 503
                trace.update(status="operational_error", result=result)
                if isinstance(error, OperationalError) and getattr(error, "details", None):
                    trace["model_error"] = error.details
            trace["elapsed_s"] = time.monotonic() - start
            with (evidence / (trace["request_id"] + ".json")).open("x") as out:
                json.dump(trace, out, ensure_ascii=False, indent=2)
            self.emit(status, result)

        def log_message(self, fmt, *args):
            pass

    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--index", type=Path, required=True)
    p.add_argument("--port", type=int, default=19720)
    p.add_argument("--evidence", type=Path, required=True)
    p.add_argument("--models", default="http://127.0.0.1:19601")
    a = p.parse_args()
    serve(SearchIndex(a.index, LocalModels(a.models)), "127.0.0.1", a.port, a.evidence)
