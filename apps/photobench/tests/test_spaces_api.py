"""``GET /api/memory-spaces``（S10a · T1003）的 HTTP 契约。

三件必须让两种口径分开的事：

1. **``sentrix_url`` 查询参数真的生效**——假件按 URL 作答，忽略参数的实现会打到
   配置里那台上，断言立刻红；夹具把「配置里的地址」与「参数给的地址」写成**两个
   不同的主机**，否则这条断言在两种实现下都通过。
2. **信封两种键名都认**（``spaces`` 与 ``items``）——只认一个的实现会在另一条上红。
3. **``reuse_bases`` 吃全部 run**——夹具放两个 run，其中一个的 ``scope_id`` 对应
   space；把「按 scope 分组」写坏会少一条 ``source_run_ids``。
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fakes_read import FakeJsonHttpClient
from fastapi.testclient import TestClient

from photobench.application.spaces.service import MemorySpaces
from photobench.config import Settings
from photobench.main import create_app

PROVIDERS = {"default_provider_id": "cloud", "providers": {"cloud": {"url": "http://j/v1", "model": "m"}}}
TARGETS = {
    "default_target_id": "local",
    "targets": {"local": {"label": "本机", "manager_url": "", "model_base_url": ""}},
}

SPACE = {
    "id": "scope-1",
    "name": "PhotoBench-20260101-000000-album3-big_model",
    "status": "active",
    "created_at": "2026-01-01T00:00:00+08:00",
}


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    config_root = tmp_path / "config"
    config_root.mkdir()
    _write(config_root / "runtime_connection.json", {"sentrix_url": "http://configured:9598"})
    _write(config_root / "judge_providers.json", PROVIDERS)
    _write(config_root / "vllm_targets.json", TARGETS)

    results_root = tmp_path / "results"
    _write(
        results_root / "run-a" / "run.json",
        {
            "run_id": "run-a",
            "status": "completed",
            "scope_id": "scope-1",
            "album_id": "album3",
            "model_profile": "big_model",
            "started_at": "2026-01-01T01:00:00+08:00",
            "items": [],
        },
    )
    # 第二个 run 挂在另一个 scope 上：它不该进 reuse_bases（那个 scope 没有 space）。
    _write(
        results_root / "run-b" / "run.json",
        {
            "run_id": "run-b",
            "status": "completed",
            "scope_id": "scope-other",
            "album_id": "album9",
            "model_profile": "qwen",
            "started_at": "2026-01-02T01:00:00+08:00",
            "items": [],
        },
    )
    (tmp_path / "datasets").mkdir()

    settings = Settings(config_root=config_root, results_root=results_root, data_root=tmp_path / "datasets")
    app = create_app(settings, scan_in_background=False)
    container = app.state.container
    container.run_index.refresh(container.run_store)
    with TestClient(app) as test_client:
        yield test_client


def _with_http(client: TestClient, responses: dict[str, object]) -> FakeJsonHttpClient:
    http = FakeJsonHttpClient(responses)
    container = client.app.state.container
    client.app.state.container = dataclasses.replace(
        container,
        spaces=MemorySpaces(http=http, config=container.config, index=container.run_index),
    )
    return http


def test_memory_spaces_forwards_to_the_configured_url_and_builds_reuse_bases(client: TestClient):
    http = _with_http(client, {"memory-spaces": {"spaces": [SPACE]}})
    body = client.get("/api/v1/memory-spaces").json()
    assert body["spaces"] == [SPACE]
    assert http.calls[0][0] == "http://configured:9598/api/v1/memory-spaces"
    assert body["reuse_bases"] == [
        {
            "base_id": "album3::big_model",
            "album_id": "album3",
            "model_profile": "big_model",
            "scope_id": "scope-1",
            "scope_name": SPACE["name"],
            "created_at": SPACE["created_at"],
            "source_run_ids": ["run-a"],
            "ready": True,
        }
    ]


def test_memory_spaces_honours_the_sentrix_url_parameter(client: TestClient):
    """调用方可以把这一条指向别的 Sentrix。**参数被忽略时这条会红**——默认那台的
    主机名与参数给的（``http://elsewhere:9999``）刻意不同。"""
    http = _with_http(client, {"memory-spaces": {"spaces": []}})
    client.get("/api/v1/memory-spaces", params={"sentrix_url": "http://elsewhere:9999/"})
    assert http.calls[0][0] == "http://elsewhere:9999/api/v1/memory-spaces"


def test_memory_spaces_accepts_both_envelope_keys(client: TestClient):
    """上游两种信封都出现过（``spaces`` / ``items``）。只认一个的实现会在另一条上红。

    **两次替换容器要夹着两次请求**：先装 ``spaces`` 信封的假件、请求一次，再换成
    ``items`` 那个——一次装两个只会让两个断言都跑在同一个假件上，其中一条永远是空的。
    """
    spaces = _with_http(client, {"memory-spaces": {"spaces": [SPACE]}})
    assert client.get("/api/v1/memory-spaces").json()["spaces"] == [SPACE]
    items = _with_http(client, {"memory-spaces": {"items": [SPACE]}})
    assert client.get("/api/v1/memory-spaces").json()["spaces"] == [SPACE]
    assert len(spaces.calls) == 1 and len(items.calls) == 1


def test_memory_spaces_sorts_newest_first(client: TestClient):
    older = {**SPACE, "id": "scope-0", "name": "old", "created_at": "2025-01-01T00:00:00+08:00"}
    _with_http(client, {"memory-spaces": {"spaces": [older, SPACE]}})
    body = client.get("/api/v1/memory-spaces").json()
    assert [space["id"] for space in body["spaces"]] == ["scope-1", "scope-0"]
