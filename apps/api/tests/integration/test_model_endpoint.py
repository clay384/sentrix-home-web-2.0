"""``ServedModel``：问端点要模型名，探不到就大声报错。

用**真的**本地 HTTP 服务而不是 mock transport，理由与 ``test_chat_client.py`` 同：被测的
东西有一半是「打的是哪个请求、怎么读响应」，假 transport 会把这一半一起替换掉，于是
「读的是 ``data[].id``」这种约定就没人验了。

这些用例守的是 2026-09-24 那次事故的根因：8100 从 llama.cpp 换成 vLLM 之后，2.0 手里
那个写死的名字变成 404，助手整条链路停摆。名字必须是**问来的**，而问不到的时候必须是
**响的**。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from sentrix.domain.assistant.ports import ModelServiceError
from sentrix.infrastructure.assistant.model_endpoint import ServedModel


class _Handler(BaseHTTPRequestHandler):
    served: list[str] = ["gemma4-12b-it"]
    status: int = 200
    raw: bytes | None = None
    gets: list[str] = []

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的接口
        type(self).gets.append(self.path)
        payload = type(self).raw
        if payload is None:
            payload = json.dumps(
                {
                    "object": "list",
                    "data": [{"id": name, "object": "model"} for name in type(self).served],
                }
            ).encode()
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args: object) -> None:
        pass  # 测试输出里不要 HTTP 访问日志


@pytest.fixture
def endpoint() -> Iterator[str]:
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}/v1"
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture(autouse=True)
def _reset_handler() -> None:
    _Handler.served = ["gemma4-12b-it"]
    _Handler.status = 200
    _Handler.raw = None
    _Handler.gets = []


# ---- 探到了什么 -------------------------------------------------------------


def test_the_name_comes_from_the_endpoint(endpoint: str):
    """名字的唯一来源是端点的 ``/v1/models``，不是任何一处代码里的常量。"""
    model = ServedModel(endpoint)

    assert model.current() == "gemma4-12b-it"
    assert _Handler.gets == ["/v1/models"]


@pytest.mark.parametrize("served", ["gemma4-qat-iq3xxs", "gemma4-12b-it", "llama-alias"])
def test_whatever_the_endpoint_serves_is_what_gets_used(endpoint: str, served: str):
    """llama.cpp 的 ``--alias`` 与 vLLM 的 ``--served-model-name`` 取什么名字都行。

    这条是裁决一的正面：两个后端都能当后端，客户端这边不挑。
    """
    _Handler.served = [served]

    assert ServedModel(endpoint).current() == served


def test_duplicate_ids_are_not_two_models(endpoint: str):
    """同一个名字出现两次（多副本 vLLM）不该被当成「同时服务多个模型」。"""
    _Handler.served = ["gemma4-12b-it", "gemma4-12b-it"]

    assert ServedModel(endpoint).current() == "gemma4-12b-it"


# ---- 缓存与失效 -------------------------------------------------------------


def test_the_name_is_probed_once_and_then_cached(endpoint: str):
    """每次请求都探一次是浪费：一个回合至少一次规划 + N 次主循环。"""
    model = ServedModel(endpoint)

    for _ in range(5):
        assert model.current() == "gemma4-12b-it"

    assert len(_Handler.gets) == 1


def test_the_cache_expires_and_the_name_is_probed_again(endpoint: str):
    """TTL 到点后重新探，并且**用新探到的名字**——不是只重发一个请求。"""
    now = [0.0]
    model = ServedModel(endpoint, ttl_s=60.0, monotonic=lambda: now[0])

    assert model.current() == "gemma4-12b-it"

    _Handler.served = ["llama-alias"]
    assert model.current() == "gemma4-12b-it", "TTL 之内必须还在用缓存"

    now[0] = 61.0
    assert model.current() == "llama-alias"
    assert len(_Handler.gets) == 2


def test_invalidate_forces_a_reprobe_right_now(endpoint: str):
    """``invalidate()`` 是「后端被换过」的即时失效路径，不等 TTL。"""
    model = ServedModel(endpoint)
    assert model.current() == "gemma4-12b-it"

    _Handler.served = ["llama-alias"]
    model.invalidate()

    assert model.current() == "llama-alias"
    assert len(_Handler.gets) == 2


# ---- 探不到的时候必须响 -----------------------------------------------------


def test_an_endpoint_serving_nothing_is_a_loud_error(endpoint: str):
    """空列表不退回常量、不猜：说清楚是哪个端点没在服务模型。"""
    _Handler.served = []

    with pytest.raises(ModelServiceError) as error:
        ServedModel(endpoint).current()

    assert endpoint in str(error.value)
    assert "没有任何模型" in str(error.value)


def test_an_endpoint_serving_several_models_is_a_loud_error(endpoint: str):
    """多于一个也不挑一个「看起来对」的——挑错了就是 404，而且没有依据说该挑哪个。"""
    _Handler.served = ["gemma4-12b-it", "qwen3-vl-8b"]

    with pytest.raises(ModelServiceError) as error:
        ServedModel(endpoint).current()

    message = str(error.value)
    assert "gemma4-12b-it" in message
    assert "qwen3-vl-8b" in message


def test_an_unreachable_endpoint_is_a_loud_error():
    with pytest.raises(ModelServiceError) as error:
        ServedModel("http://127.0.0.1:1/v1", timeout_s=1.0).current()

    assert "不可达" in str(error.value)


def test_a_non_200_models_response_is_a_loud_error(endpoint: str):
    _Handler.status = 503
    _Handler.raw = b"upstream down"

    with pytest.raises(ModelServiceError) as error:
        ServedModel(endpoint).current()

    assert "503" in str(error.value)


def test_a_non_json_models_response_is_a_loud_error(endpoint: str):
    _Handler.raw = b"<html>not json</html>"

    with pytest.raises(ModelServiceError) as error:
        ServedModel(endpoint).current()

    assert "不是 JSON" in str(error.value)


def test_a_probe_that_fails_is_retried_on_the_next_call(endpoint: str):
    """探测失败**不进缓存**：端点起来了就该立刻用上，不该被一个负结果卡住 TTL。"""
    model = ServedModel(endpoint)
    _Handler.status = 503

    with pytest.raises(ModelServiceError):
        model.current()

    _Handler.status = 200
    assert model.current() == "gemma4-12b-it"


@pytest.mark.parametrize("status", [401, 429, 503])
def test_discovery_rejection_is_structured_before_any_completion(endpoint, status):
    _Handler.status = status
    with pytest.raises(ModelServiceError) as raised:
        ServedModel(endpoint).current()
    assert raised.value.error_code == "provider_rejected"
    assert raised.value.http_status == status
    assert raised.value.request_policy["request_phase"] == "model_discovery"
    assert raised.value.request_policy["request_outcome_unknown"] is False
    assert _Handler.gets == ["/v1/models"]


def test_discovery_timeout_is_known_not_dispatched(monkeypatch):
    import httpx

    def timeout(*args, **kwargs):
        raise httpx.ReadTimeout("fixture")

    monkeypatch.setattr(httpx, "get", timeout)
    with pytest.raises(ModelServiceError) as raised:
        ServedModel("http://fixture/v1", timeout_s=9).current()
    assert raised.value.error_code == "time_limit"
    assert raised.value.request_policy == {
        "request_phase": "model_discovery",
        "request_timeout_s": 9,
        "request_outcome_unknown": False,
    }
