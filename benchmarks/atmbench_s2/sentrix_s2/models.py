"""Strict local Qwen transport; errors never become empty retrieval or zero rerank scores."""

import hashlib
import json
import math
import time
import urllib.error
import urllib.request

EMBEDDING = "Qwen/Qwen3-VL-Embedding-2B"
RERANKER = "Qwen/Qwen3-VL-Reranker-2B"


class OperationalError(RuntimeError):
    pass


class LocalModels:
    def __init__(self, base="http://127.0.0.1:19601", timeout=900):
        self.base, self.timeout = base.rstrip("/"), timeout
        self.calls = []

    def post(self, endpoint, body):
        request = urllib.request.Request(
            self.base + endpoint,
            json.dumps(body, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json"},
        )
        started = time.monotonic()
        record = {
            "endpoint": endpoint,
            "request_sha256": hashlib.sha256(request.data).hexdigest(),
            "input_count": len(body.get("input", body.get("documents", []))),
            "input_type": body.get("input_type"),
            "model": body["model"],
            "status": "dispatched",
        }
        self.calls.append(record)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as stream:
                result = json.load(stream)
                record.update(
                    status="completed",
                    service_fingerprint=stream.headers.get("X-ATM-Model-Fingerprint"),
                    elapsed_s=time.monotonic() - started,
                    queue_seconds=stream.headers.get("X-ATM-Queue-Seconds"),
                )
        except urllib.error.HTTPError as error:
            # The loopback model service returns a structured operational code.
            # Preserve it for audit rather than erasing HTTP400 as generic503.
            try:
                body = json.loads(error.read(16384))
                detail = body.get("error") if isinstance(body, dict) else None
            except (OSError, ValueError):
                detail = None
            record.update(
                status="operational_error",
                error_type=type(error).__name__,
                http_status=error.code,
                response_error=detail,
                elapsed_s=time.monotonic() - started,
            )
            failure = OperationalError("local_model_HTTPError")
            failure.details = dict(record)
            raise failure from error
        except (OSError, ValueError) as error:
            record.update(
                status="operational_error",
                error_type=type(error).__name__,
                elapsed_s=time.monotonic() - started,
            )
            raise OperationalError("local_model_" + type(error).__name__) from error
        return result

    def embed(self, texts, input_type):
        body = self.post(
            "/v1/embeddings",
            {
                "model": EMBEDDING,
                "input": texts,
                "input_type": input_type,
                "dimensions": 2048,
                "encoding_format": "float",
            },
        )
        rows = body.get("data", [])
        if len(rows) != len(texts) or sorted(x.get("index", -1) for x in rows) != list(range(len(texts))):
            raise OperationalError("embedding_shape")
        vectors = [x["embedding"] for x in sorted(rows, key=lambda x: x["index"])]
        for vector in vectors:
            if (
                len(vector) != 2048
                or not all(math.isfinite(x) for x in vector)
                or abs(sum(x * x for x in vector) - 1) > 0.02
            ):
                raise OperationalError("embedding_not_normalized_2048")
        return vectors

    def rerank(self, query, documents):
        rows = self.post(
            "/rerank",
            {
                "model": RERANKER,
                "query": query,
                "documents": documents,
                "top_n": len(documents),
                "truncate": False,
            },
        )
        if not isinstance(rows, list) or sorted(x.get("index", -1) for x in rows) != list(
            range(len(documents))
        ):
            raise OperationalError("rerank_index_not_permutation")
        for row in rows:
            score = row.get("relevance_score")
            if not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 1:
                raise OperationalError("rerank_invalid_score")
        return rows
