"""legacy 嵌入适配器：正常路径 + 降级 + 维度校验 + 熔断 + 健康探针。

重点仍是「拿不到的时候会怎样」：这两个侧车是可选依赖，失败必须返回空表让上层
标记 ``degraded``，不能把查询挂在一个 HTTP 超时上。
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from sentrix.domain.retrieval import scheme
from sentrix.infrastructure.models import legacy
from sentrix.infrastructure.models.legacy import (
    CIRCUIT_OPEN_S,
    FAILURE_THRESHOLD,
    BgeSidecarEmbedder,
    ChineseClipSidecarEmbedder,
    LegacyEmbeddingHealthProbe,
)

CLIP_BASE = "http://127.0.0.1:8103"
BGE_BASE = "http://127.0.0.1:8101"


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def build(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[httpx.Client, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return httpx.Client(transport=httpx.MockTransport(wrapped)), seen


def clip_with(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[ChineseClipSidecarEmbedder, list[httpx.Request]]:
    client, seen = build(handler)
    return ChineseClipSidecarEmbedder(CLIP_BASE, client=client), seen


def bge_with(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[BgeSidecarEmbedder, list[httpx.Request]]:
    client, seen = build(handler)
    return BgeSidecarEmbedder(BGE_BASE, client=client), seen


def test_clip_shapes_and_endpoints():
    embedder, seen = clip_with(
        lambda request: httpx.Response(200, json={"vector": [0.1] * scheme.LEGACY_VISUAL_DIMENSION})
    )
    assert embedder.model_id == "chinese-clip-ViT-L-14"
    assert embedder.dimension == 768

    assert len(embedder.embed_image("/tmp/a.jpg")) == 768
    assert len(embedder.embed_query("毛绒睡衣")) == 768
    assert seen[0].url.path == "/embed_image"
    assert seen[1].url.path == "/embed_query"


def test_bge_uses_the_1_0_endpoint():
    embedder, seen = bge_with(
        lambda request: httpx.Response(200, json={"vector": [0.1] * scheme.LEGACY_TEXT_DIMENSION})
    )
    assert embedder.model_id == "BAAI/bge-m3"
    assert embedder.dimension == 1024
    assert len(embedder.embed_query("去年夏天去了哪里")) == 1024
    assert seen[0].url.path == "/embed"


def test_bge_negotiates_openai_once_after_sidecar_404():
    def handler(request):
        if request.url.path == "/embed":
            return httpx.Response(404)
        return httpx.Response(200, json={"data": [{"embedding": [0.1] * 1024}]})

    embedder, seen = bge_with(handler)
    assert len(embedder.embed_query("第一次")) == 1024
    assert len(embedder.embed_query("第二次")) == 1024
    assert [r.url.path for r in seen] == ["/embed", "/v1/embeddings", "/v1/embeddings"]


@pytest.mark.parametrize("status", [400, 401, 429, 500])
def test_bge_does_not_negotiate_on_other_sidecar_errors(status):
    embedder, seen = bge_with(lambda request: httpx.Response(status))
    assert embedder.embed_query("query") == []
    assert len(seen) == 1
    assert str(status) in embedder.load_error


def test_bge_does_not_treat_arbitrary_openai_400_as_pooling_none():
    embedder, seen = bge_with(
        lambda request: (
            httpx.Response(404)
            if request.url.path == "/embed"
            else httpx.Response(400, json={"error": {"message": "bad input"}})
        )
    )
    assert embedder.embed_query("query") == []
    assert [r.url.path for r in seen] == ["/embed", "/v1/embeddings"]


@pytest.mark.parametrize("vector", [[1.0] * 512, [float("nan")] * 1024, [float("inf")] * 1024])
def test_bge_rejects_invalid_openai_vectors(vector):
    # HTTP JSON does not permit NaN/Infinity, but malformed providers still emit them.
    import json

    embedder, _ = bge_with(
        lambda request: (
            httpx.Response(404)
            if request.url.path == "/embed"
            else httpx.Response(200, content=json.dumps({"data": [{"embedding": vector}]}))
        )
    )
    assert embedder.embed_query("query") == []
    assert embedder.load_error


def test_dimension_mismatch_is_a_failure_not_a_vector():
    """1.0 的 bge 是 1024、chinese-clip 是 768；放行错维度会静默毁掉检索。"""
    embedder, _ = clip_with(lambda request: httpx.Response(200, json={"vector": [0.1] * 1024}))
    assert embedder.embed_image("/tmp/a.jpg") == []
    assert "768" in embedder.load_error


def test_empty_input_does_not_hit_the_network_or_trip_the_breaker():
    embedder, seen = clip_with(lambda request: httpx.Response(500))
    assert embedder.embed_query("  ") == []
    assert seen == []


def test_failure_returns_empty_and_trips_the_breaker(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(legacy.time, "monotonic", clock.monotonic)
    embedder, seen = clip_with(lambda request: httpx.Response(500))

    for _ in range(FAILURE_THRESHOLD):
        assert embedder.embed_query("x") == []
    assert len(seen) == FAILURE_THRESHOLD

    # 熔断期内不再打网络。
    assert embedder.embed_query("x") == []
    assert len(seen) == FAILURE_THRESHOLD

    clock.advance(CIRCUIT_OPEN_S + 0.1)
    assert embedder.embed_query("x") == []
    assert len(seen) == FAILURE_THRESHOLD + 1


def test_health_probe_requires_both_halves():
    visual, _ = clip_with(lambda request: httpx.Response(200, json={"vector": [0.1] * 768}))
    text, _ = bge_with(lambda request: httpx.Response(200, json={"vector": [0.1] * 1024}))
    probe = LegacyEmbeddingHealthProbe(visual, text)
    assert probe.available is True
    assert probe.load_error == ""

    dead_text, _ = bge_with(lambda request: httpx.Response(503))
    probe = LegacyEmbeddingHealthProbe(visual, dead_text)
    assert probe.available is False
    assert "text:" in probe.load_error


def test_sidecar_health_shape_is_what_the_adapter_expects():
    """契约：侧车 /health 返回 2xx 即视为可用。"""
    embedder, seen = clip_with(lambda request: httpx.Response(200, json={"status": "ok"}))
    assert embedder.available is True
    assert seen[-1].url.path == "/health"


@pytest.mark.parametrize(
    "error,code,unknown",
    [
        (httpx.ReadTimeout("private provider text"), "time_limit", True),
        (httpx.ConnectTimeout("private provider text"), "time_limit", False),
        (httpx.PoolTimeout("private provider text"), "time_limit", False),
        (httpx.ConnectError("private provider text"), "provider_unavailable", False),
    ],
)
def test_embedding_transport_evidence_preserves_empty_return_without_leaking_text(error, code, unknown):
    def handler(request):
        raise error

    embedder, seen = bge_with(handler)
    assert embedder.embed_query("private query") == []
    issue = embedder.last_operational_issue
    assert issue["error_code"] == code
    assert issue["request_outcome_unknown"] is unknown
    assert issue["request_policy"]["request_timeout_s"] == 10
    assert len(seen) == 1
    assert "private" not in str(issue)


@pytest.mark.parametrize("status", [400, 429, 503])
def test_embedding_http_rejection_is_structured_and_not_retried(status):
    embedder, seen = clip_with(lambda request: httpx.Response(status, text="private provider body"))
    assert embedder.embed_query("query") == []
    assert embedder.last_operational_issue["http_status"] == status
    assert embedder.last_operational_issue["error_code"] == "provider_rejected"
    assert len(seen) == 1
    assert "private" not in str(embedder.last_operational_issue)


@pytest.mark.parametrize(
    "vector,cause",
    [([], "empty_vector"), ([1.0], "dimension_mismatch"), ([float("inf")] * 768, "non_finite_vector")],
)
def test_invalid_embedding_is_not_a_successful_empty_search(vector, cause):
    # Avoid JSON's finite-only transport so the adapter's response validation is exercised.
    embedder = ChineseClipSidecarEmbedder(CLIP_BASE)
    embedder._request_values = lambda *_: vector
    assert embedder.embed_query("query") == []
    assert embedder.last_operational_issue["failure_code"] == cause
    assert embedder.last_operational_issue["error_code"] == "resource_error"


def test_embedding_evidence_clears_for_success_empty_input_and_marks_circuit():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503 if len(calls) <= FAILURE_THRESHOLD else 200, json={"vector": [0.1] * 768})

    embedder, _ = clip_with(handler)
    for _ in range(FAILURE_THRESHOLD):
        assert embedder.embed_query("query") == []
    assert embedder.last_operational_issue["http_status"] == 503
    assert embedder.embed_query("query") == []
    assert embedder.last_operational_issue["failure_code"] == "circuit_open"
    assert len(calls) == FAILURE_THRESHOLD
    assert embedder.embed_query(" ") == []
    assert embedder.last_operational_issue is None
    embedder._tripped_at = 0
    assert len(embedder.embed_query("success")) == 768
    assert embedder.last_operational_issue is None


def test_embedding_faults_are_request_local_across_concurrent_callers():
    import json
    import threading
    from concurrent.futures import ThreadPoolExecutor

    barrier = threading.Barrier(2)

    def handler(request):
        if json.loads(request.content)["text"] == "failing":
            raise httpx.ReadTimeout("lost response")
        return httpx.Response(200, json={"vector": [0.1] * 768})

    embedder, _ = clip_with(handler)

    def run(query):
        vector = embedder.embed_query(query)
        barrier.wait(timeout=5)
        return vector, embedder.last_operational_issue

    with ThreadPoolExecutor(max_workers=2) as pool:
        failed = pool.submit(run, "failing")
        passed = pool.submit(run, "success")
        assert failed.result()[1]["error_code"] == "time_limit"
        vector, issue = passed.result()
        assert len(vector) == 768 and issue is None
    assert embedder.last_operational_issue is None
