import dataclasses

import pytest
from fastapi.testclient import TestClient


class _UnavailableEmbeddings:
    """嵌入侧车不在、而且它自己也说不出为什么的样子。"""

    @property
    def available(self) -> bool:
        return False

    @property
    def load_error(self) -> str:
        return ""


@pytest.mark.parametrize("revision", ["development", "release-0123456789abcdef"])
def test_health_returns_ok(client: TestClient, revision: str):
    # Source releases contain build-info.json; the HTTP contract must not depend
    # on whether this checkout has a deployment manifest or on its revision.
    client.app.state.container = dataclasses.replace(client.app.state.container, build_revision=revision)
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": "0.1.0",
        "build_revision": revision,
        "database": "ok",
        "embeddings": "ok",
    }


def test_health_is_degraded_when_embeddings_are_unavailable(client: TestClient):
    """嵌入侧车不在时 ``/health`` 必须说 ``degraded``，并带上原因。

    2026-10-10 那轮 PhotoBench 评测里侧车全程不在（354/370 次检索静默降级成
    ``backend=none``），而本端点一路回 ``{"status":"ok"}`` —— 整轮数据作废却没有任何
    一处报错。数据库好、嵌入器坏，**不是 ok**。

    是 ``degraded`` 而不是 ``error``：嵌入是可选依赖，侧车不在时检索退回词法/元数据/
    实体三路，进程照常服务。两档分开才让「还能用但少了东西」与「用不了」可区分。
    """
    client.app.state.container = dataclasses.replace(
        client.app.state.container, embedding_health=_UnavailableEmbeddings()
    )

    body = client.get("/api/v1/health").json()

    assert body["status"] == "degraded"
    assert body["database"] == "ok"
    assert body["embeddings"] == "unavailable"


def test_health_reports_a_reason_and_never_an_empty_string(client: TestClient):
    """说不出原因时回一个固定的词，不回空串。

    「不可用」三个字让排查要从日志里捞，而这条路径的整个教训就是
    「降级必须自己走到人眼前」。
    """
    client.app.state.container = dataclasses.replace(
        client.app.state.container, embedding_health=_UnavailableEmbeddings()
    )

    body = client.get("/api/v1/health").json()

    assert body["embeddings_error"] == "sidecar_unreachable"


def test_unknown_v1_route_is_404(client: TestClient):
    assert client.get("/api/v1/nope").status_code == 404


def test_no_unversioned_public_routes(client: TestClient):
    """公开 API 只有 /api/v1；/api/health 这种 1.0 风格路径必须不存在。"""
    assert client.get("/api/health").status_code == 404


def test_health_live_does_not_touch_dependencies(client: TestClient):
    """轻量探针只证明事件循环还能回包：数据库/嵌入侧车都不许碰。

    看门狗用它做 1 分钟级探活，重负载下不能因为一次慢查询/慢侧车被判死。
    """

    class _Exploding:
        build_revision = "development"

        def database_healthy(self) -> bool:
            raise AssertionError("liveness must not query the database")

        @property
        def embedding_health(self) -> object:
            raise AssertionError("liveness must not probe embeddings")

    client.app.state.container = _Exploding()

    assert client.get("/api/v1/health/live").json() == {
        "status": "ok",
        "version": "0.1.0",
        "build_revision": "development",
    }


def test_diagnostics_carries_the_full_payload(client: TestClient):
    """完整诊断与 ``/health`` 同源，保留既有消费方的字段。"""
    result = client.get("/api/v1/diagnostics").json()
    effective = result.pop("runtime_configuration")
    assert effective["vlm_base_url"]
    assert effective["agent_wall_time_s"] > 0
    assert result == client.get("/api/v1/health").json()


def test_health_live_responds_with_the_entire_worker_pool_occupied(client: TestClient):
    import asyncio

    import anyio
    import httpx

    async def exercise():
        limiter = anyio.to_thread.current_default_thread_limiter()
        previous = limiter.total_tokens
        limiter.total_tokens = 1
        await limiter.acquire()
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=client.app), base_url="http://test"
            ) as http:
                response = await asyncio.wait_for(http.get("/api/v1/health/live"), timeout=1)
            assert response.status_code == 200
        finally:
            limiter.release()
            limiter.total_tokens = previous

    asyncio.run(exercise())
