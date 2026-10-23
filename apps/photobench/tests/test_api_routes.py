"""HTTP 契约：状态码与**响应体形状**都要与 1.0 一致（前端一行不动）。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from photobench.config import Settings
from photobench.main import create_app

PROVIDERS = {
    "default_provider_id": "cloud",
    "providers": {"cloud": {"url": "http://j/v1", "model": "m", "api_key": ""}},
}
# manager_url 留空：本切片的路由用例**不打模型端点**，profiles 会自己回 not_applicable。
TARGETS = {
    "default_target_id": "local",
    "targets": {"local": {"label": "本机", "manager_url": "", "model_base_url": "http://m:8100/v1"}},
}


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    config_root = tmp_path / "config"
    config_root.mkdir()
    (config_root / "runtime_connection.json").write_text(
        json.dumps({"sentrix_url": "http://sentrix:9598", "judge_provider_id": "cloud"}), encoding="utf-8"
    )
    (config_root / "judge_providers.json").write_text(json.dumps(PROVIDERS), encoding="utf-8")
    (config_root / "vllm_targets.json").write_text(json.dumps(TARGETS), encoding="utf-8")

    results_root = tmp_path / "results"
    (results_root / "run-a").mkdir(parents=True)
    (results_root / "run-a" / "run.json").write_text(
        json.dumps(
            {
                "run_id": "run-a",
                "album_id": "album3",
                "status": "completed",
                "started_at": "2026-01-01T00:00:00+08:00",
                "qa_count": 1,
                "items": [{"qa_id": "q1", "question": "一加一", "judge": {"score": 2}}],
            }
        ),
        encoding="utf-8",
    )
    data_root = tmp_path / "datasets"
    data_root.mkdir()

    settings = Settings(config_root=config_root, results_root=results_root, data_root=data_root)
    app = create_app(settings, scan_in_background=False)
    container = app.state.container
    # 用例同步扫一次：断言不必与后台线程赛跑。
    container.run_index.refresh(container.run_store)
    with TestClient(app) as test_client:
        yield test_client


def test_get_runs_lists_the_scan_result(client: TestClient):
    response = client.get("/api/v1/runs")
    assert response.status_code == 200
    body = response.json()
    assert [run["run_id"] for run in body["runs"]] == ["run-a"]
    assert body["total"] == 1
    assert body["scan"] == {"state": "ready", "scanned": 1, "total": 1, "error": None}


def test_a_bad_page_number_is_a_400_with_the_1_0_message(client: TestClient):
    """1.0 ``:7652`` 的 ``ValueError("page and page_size must be integers")`` → 400。"""
    response = client.get("/api/v1/runs?page=x")
    assert response.status_code == 400
    assert response.json() == {"error": "page and page_size must be integers"}


def test_get_run_returns_the_state_without_items(client: TestClient):
    body = client.get("/api/v1/runs/run-a").json()
    assert body["item_count"] == 1
    assert "items" not in body
    assert body["summary"]["answer_quality_mean"] == 2.0


def test_an_unknown_run_is_a_404_in_the_1_0_shape(client: TestClient):
    """1.0 ``:7757``：``{"error": str(KeyError)}`` —— ``str(KeyError("nope"))`` 带引号。"""
    response = client.get("/api/v1/runs/nope")
    assert response.status_code == 404
    assert response.json() == {"error": "'nope'"}


def test_get_run_items_paginates(client: TestClient):
    body = client.get("/api/v1/runs/run-a/items").json()
    assert body["total"] == 1
    assert body["items"][0]["qa_id"] == "q1"
    assert body["facets"]["task_types"] == []


def test_get_run_item_by_index(client: TestClient):
    assert client.get("/api/v1/runs/run-a/items/0").json()["item"]["qa_id"] == "q1"
    assert client.get("/api/v1/runs/run-a/items/9").status_code == 404


def test_reviews_route_accepts_a_write_on_a_finished_run(client: TestClient):
    """写侧在 S9b-3 接上：``run-a`` 已 ``completed``，所以评审可写（1.0 ``:4988``）。

    空 ``reviews`` 是合法的——它保存的是「这批评审现在是空的」，返回 ``saved`` 计数。
    """
    assert client.get("/api/v1/runs/run-a/reviews").json() == {"reviews": {}}
    response = client.post("/api/v1/runs/run-a/reviews", json={"reviews": {}})
    assert response.status_code == 200
    assert response.json()["saved"] == 0


def test_the_write_routes_are_mounted(client: TestClient):
    """S9b-1 那条「写侧一条都不该有」的用例，S9b-3 按实情反过来钉。

    ``POST /api/runs`` 落到 album 校验上（fixture 的 datasets 是空的）→ 400；
    ``cancel`` 对一个不在跑的 run 返回「没取消」而不是 404；``resume`` 对不存在的
    run 回 400（2026-10-15 之前它回 501「未实现」，那条路已接上生产）。
    """
    start = client.post("/api/v1/runs", json={})
    assert start.status_code == 400
    assert "manifest not found" in start.json()["error"]
    cancel = client.post("/api/v1/runs/run-a/cancel")
    assert cancel.status_code == 200
    assert cancel.json()["cancelled"] is False
    # resume 与 reuse 共用「绑到已有 scope」那块地基，于 2026-10-15 接上生产。
    # 路由现在**存在**：run 不存在时回 400，不再是「未实现」的 501。
    assert client.post("/api/v1/runs/run-a/resume").status_code == 400
    assert client.post("/api/v1/cancel-active").json()["total"] == 0
    assert client.delete("/api/v1/runs/run-a").json() == {"deleted": True, "run_id": "run-a"}
    assert client.get("/api/v1/runs/run-a").status_code == 404


def test_get_config_exposes_the_effective_values(client: TestClient):
    body = client.get("/api/v1/config").json()
    assert body["default_sentrix_url"] == "http://sentrix:9598"
    assert body["default_vllm_target_id"] == "local"
    assert body["default_judge_provider_id"] == "cloud"
    assert body["judge_providers"][0]["id"] == "cloud"
    assert body["runtime_config_file"].endswith("runtime_connection.json")
    assert body["cloud_model_profile"]["id"] == "big_model"
    # S9b-1 那条「三项 judge 域字段刻意没搬」的断言在 S9b-3 按实情反过来：搬了。
    assert body["judge_prompt"].startswith("你是通用对话任务的回答质量评测员")
    assert body["custom_judge_prompt"] is None
    assert body["evidence_judge_enabled"] is False
    kinds = {entry["kind"] for entry in client.get("/api/v1/judge-prompts").json()["kinds"]}
    assert kinds == {"answer_quality", "task_decision", "evidence"}


def test_post_config_persists_and_answers_in_the_1_0_shape(client: TestClient):
    response = client.post(
        "/api/v1/config",
        json={
            "sentrix_url": "http://new:9598",
            "judge_url": "http://j2/v1",
            "judge_model": "m2",
            "judge_provider_id": "cloud",
            "model_base_url": "http://m2:8100",
        },
    )
    assert response.status_code == 200
    assert response.json()["saved"] is True
    assert response.json()["runtime_config"]["sentrix_url"] == "http://new:9598"
    assert client.get("/api/v1/config").json()["default_sentrix_url"] == "http://new:9598"


def test_post_config_rejects_an_unknown_provider_with_a_400(client: TestClient):
    response = client.post("/api/v1/config", json={"judge_model": "m", "judge_provider_id": "nope"})
    assert response.status_code == 400
    assert "unknown judge provider" in response.json()["error"]


def test_post_config_with_a_broken_body_is_treated_as_empty(client: TestClient):
    """1.0 ``_payload``（``:7523``）：坏 JSON 当空对象，然后落到字段校验上。"""
    response = client.post(
        "/api/v1/config", content=b"{ not json", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 400
    # 空对象落到第一道校验上（1.0 的校验顺序：sentrix_url 先于 judge_model）。
    assert response.json()["error"] == "Sentrix URL must be a host:port or HTTP(S) URL"


def test_current_model_get_says_use_post(client: TestClient):
    """1.0 ``:7530``。"""
    response = client.get("/api/v1/current-model")
    assert response.status_code == 405
    assert response.json() == {"error": "current-model requires POST"}


def test_profiles_without_a_manager_is_not_an_error(client: TestClient):
    """Manager 没配是 ``status: not_applicable``，不是 4xx——1.0 ``:5069``。"""
    response = client.post("/api/v1/profiles", json={"vllm_target_id": "local"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "not_applicable"
    assert body["target_id"] == "local"


# ---- 两个死端点（S9c · T922） -----------------------------------------------


def test_no_route_serves_the_two_dead_endpoints(client: TestClient):
    """**T922 的机械凭据：2.0 的服务面里没有这两条路。**

    ``/api/v1/arbiter/status`` 与 ``/api/v1/ingest-batches/{id}/cancel`` 在 1.0 主 API 里
    都不存在（arbiter 零命中；ingest-batches 只有 POST/GET/complete）。1.0 的
    PhotoBench 轮询它们、被 ``except Exception`` 吞成静默 404，占了 8091 流量的 17%。

    2.0 是重写而不是拷贝，S9b-1 只建了真正需要的那几条路由——所以「删掉」在本仓里
    的表现是**从未加过**。这条用例把「从未加过」变成一条会红的断言，免得后来人
    照 1.0 的 handler 补路由时把它一起搬回来。
    """
    paths = [str(getattr(route, "path", "")) for route in client.app.routes]
    offenders = [path for path in paths if "arbiter" in path or "ingest-batch" in path]
    assert offenders == [], f"死端点又出现了：{offenders}"


def test_the_sentrix_client_has_no_arbiter_or_batch_cancel_capability():
    """端口上也一样：主链没有「查 arbiter」「取消批量」这两件事可做。

    2.0 的 ``POST /api/v1/imports`` 是**同步**的，没有批量实体可取消；arbiter 在 2.0
    不存在——所以两条都是「没有等价物就删」，不是「改指别处」。
    """
    from photobench.domain.sentrix.ports import SentrixClient

    names = [name for name in dir(SentrixClient) if not name.startswith("_")]
    assert [name for name in names if "arbiter" in name or "cancel" in name] == []
