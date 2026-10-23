"""``POST /api/test-model``（S10a · T1004）的 HTTP 契约。

1.0 的语义是**纯探活**（``OrchestratorRepository.test_model_endpoint`` ``:5232``）：
查端点 → 没有 served 模型就报错 → 打一次 ``max_tokens=1`` 的 chat → 没有 choices 就报错。

夹具刻意让「一次 chat 都没打」与「打了一次」在断言上分得开：``FakeInference`` 把每次
``chat`` 的 payload 记下来，所以两条错误路径都能顺手证明**没有发那次请求**。
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from photobench.application.model_profiles.service import ModelProfiles
from photobench.config import Settings
from photobench.main import create_app

PROVIDERS = {"default_provider_id": "cloud", "providers": {"cloud": {"url": "http://j/v1", "model": "m"}}}
TARGETS = {
    "default_target_id": "local",
    "targets": {"local": {"label": "本机", "manager_url": "", "model_base_url": ""}},
}


class FakeInference:
    """只实现这条路由会用到的三个方法；``chat`` 记录 payload 以便断言「打没打」。"""

    def __init__(self, models: list[str], choices: list[dict[str, Any]] | None = None) -> None:
        self._models = models
        self._choices = choices if choices is not None else [{"index": 0}]
        self.chat_payloads: list[dict[str, Any]] = []

    def list_models(self) -> dict[str, Any]:
        return {"models": list(self._models)}

    def capabilities(self) -> dict[str, Any]:
        return {}

    def chat(self, payload: Any, *, timeout: float | None = None) -> dict[str, Any]:
        self.chat_payloads.append(dict(payload))
        return {"choices": list(self._choices)}


class FakeLifecycle:
    def state(self) -> dict[str, Any]:
        return {}

    def profiles(self) -> dict[str, Any]:
        return {"profiles": []}


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    config_root = tmp_path / "config"
    config_root.mkdir()
    for name, payload in (
        ("runtime_connection.json", {"sentrix_url": "http://sentrix:9598"}),
        ("judge_providers.json", PROVIDERS),
        ("vllm_targets.json", TARGETS),
    ):
        (config_root / name).write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / "datasets").mkdir()
    (tmp_path / "results").mkdir()

    settings = Settings(
        config_root=config_root, results_root=tmp_path / "results", data_root=tmp_path / "datasets"
    )
    app = create_app(settings, scan_in_background=False)
    with TestClient(app) as test_client:
        yield test_client


def _with_inference(client: TestClient, inference: FakeInference) -> None:
    container = client.app.state.container
    client.app.state.container = dataclasses.replace(
        container,
        model_profiles=ModelProfiles(
            config=container.config,
            lifecycle_for=lambda _url: FakeLifecycle(),
            inference_for=lambda _base, _manager: inference,
        ),
    )


def test_test_model_probes_the_endpoint_once(client: TestClient):
    inference = FakeInference(models=["gemma"])
    _with_inference(client, inference)
    response = client.post("/api/v1/test-model", json={"model_base_url": "http://endpoint:8100"})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["model"] == "gemma"
    assert body["model_base_url"] == "http://endpoint:8100/v1"
    assert isinstance(body["latency_ms"], float)
    assert body["verified_at"]
    # 第三步那一发 payload 逐字段照搬 1.0。
    assert inference.chat_payloads == [
        {
            "model": "gemma",
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "max_tokens": 1,
            "temperature": 0,
            "stream": False,
        }
    ]


def test_test_model_without_a_served_model_is_a_400(client: TestClient):
    """端点暴露了两个模型、又没指名要哪个——served_name 是空，提示先去选一个。

    文案是 1.0 原文；而且**一次 chat 都不该打**（探活失败不该有副作用）。
    """
    inference = FakeInference(models=["a", "b"])
    _with_inference(client, inference)
    response = client.post("/api/v1/test-model", json={"model_base_url": "http://endpoint:8100"})
    assert response.status_code == 400
    assert response.json() == {"error": "select a model before testing the endpoint"}
    assert inference.chat_payloads == []


def test_test_model_rejects_an_endpoint_that_returns_no_choices(client: TestClient):
    """1.0 原文是 ``no completion choices``（不是 ``complete``）——照抄，别改字。"""
    inference = FakeInference(models=["gemma"], choices=[])
    _with_inference(client, inference)
    response = client.post("/api/v1/test-model", json={"model_base_url": "http://endpoint:8100"})
    assert response.status_code == 400
    assert response.json() == {"error": "model endpoint returned no completion choices"}
    assert len(inference.chat_payloads) == 1
