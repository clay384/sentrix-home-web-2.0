"""``WeMMSidecarEmbedder`` 的测试：正常路径 + 四条降级路径 + 熔断 + 健康 TTL。

**重点不在「能拿到向量」，在「拿不到的时候会怎样」。** 这个适配器同时满足视觉与文本
两个端口（统一空间），失败时**必须降级成空表**，让上层按「这台机器没有嵌入能力」走；
抛出去会让每一次查询都挂在一个可选依赖上——而那个依赖是一个 HTTP 侧车（8102），
它随时可能没起。

熔断与健康 TTL 各自单独测，都用假时钟推进，不真 sleep：没有熔断，一个挂掉的侧车会让
每次查询都白等一个 60 秒超时（2B 模型，超时是 bge 的六倍）；没有健康 TTL，每一次
可用性判断都要打一趟网络。

**维度那条是 W1 新加进 2.0 的防线**：侧车若回了别的维度（1.0 的 bge 是 1024、
chinese-clip 是 768），放行的话向量会带着**正确的 model_name** 落库，而检索侧
**不报错**——只是永远算不出相似度。所以维度不符按失败处理，与空向量同类。
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from sentrix.domain.retrieval import scheme
from sentrix.infrastructure.models import wemm
from sentrix.infrastructure.models.wemm import (
    CIRCUIT_OPEN_S,
    DIMENSION,
    FAILURE_THRESHOLD,
    HEALTH_TTL_S,
    WeMMSidecarEmbedder,
)

BASE = "http://127.0.0.1:8102"


class Clock:
    """可推进的假时钟。"""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def build(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[WeMMSidecarEmbedder, list[httpx.Request]]:
    """返回 (适配器, 被请求过的请求表)——请求表用来断言「有没有真的打网络」与请求体。"""
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(wrapped))
    return WeMMSidecarEmbedder(BASE, client=client), seen


def paths(seen: list[httpx.Request]) -> list[str]:
    return [request.url.path for request in seen]


# ---- 正常路径 --------------------------------------------------------------------


def test_model_id_and_dimension_come_from_the_scheme():
    """模型名与维度只有 ``scheme`` 一个来源——这里是写入 / 查询 / 重建三处共用的那份。"""
    embedder, _ = build(lambda request: httpx.Response(200, json={}))

    assert embedder.model_id == scheme.WEMM_MODEL
    assert embedder.dimension == scheme.WEMM_DIMENSION == 2048


def test_embed_query_posts_text_and_returns_the_vector():
    embedder, seen = build(lambda request: httpx.Response(200, json={"vector": [0.5] * DIMENSION}))

    vector = embedder.embed_query("海边 蓝")

    assert len(vector) == DIMENSION
    assert paths(seen) == ["/embed_text"]
    assert json.loads(seen[0].content) == {"text": "海边 蓝"}
    assert embedder.load_error == ""


def test_embed_image_posts_path_and_returns_the_vector(tmp_path):
    photo = tmp_path / "IMG_0001.jpg"
    embedder, seen = build(lambda request: httpx.Response(200, json={"vector": [0.5] * DIMENSION}))

    vector = embedder.embed_image(photo)

    assert len(vector) == DIMENSION
    assert paths(seen) == ["/embed_image"]
    # 传的是**路径不是字节**：侧车与后端同机，1.0 就是这么传的。
    assert json.loads(seen[0].content) == {"path": str(photo)}


# ---- 降级：失败返回空表，不抛 ----------------------------------------------------


def http_500(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, json={"error": "boom"})


def times_out(request: httpx.Request) -> httpx.Response:
    raise httpx.TimeoutException("侧车超时", request=request)


def refused(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("侧车没起", request=request)


def not_json(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text="<html>不是 JSON</html>")


@pytest.mark.parametrize(
    ("handler", "marker"),
    [
        (http_500, "HTTPStatusError"),
        (times_out, "TimeoutException"),
        (refused, "ConnectError"),
        (not_json, "JSONDecodeError"),
    ],
)
def test_failure_degrades_to_empty_vector(handler, marker):
    embedder, _ = build(handler)

    assert embedder.embed_query("海边") == []
    assert marker in embedder.load_error


def test_empty_vector_from_the_sidecar_counts_as_failure():
    """侧车回 200 但向量是空的——**这是失败，不是「这段内容没有语义」**。

    当成成功的话熔断器永远不跳，而每一次查询都白跑一趟。
    """
    embedder, seen = build(lambda request: httpx.Response(200, json={"vector": []}))

    assert embedder.embed_query("海边") == []
    assert "空向量" in embedder.load_error

    # 「计入失败」的可观测形态：连着来够阈值就断开，断开之后不再打网络。
    for _ in range(FAILURE_THRESHOLD - 1):
        embedder.embed_query("海边")
    calls_before = len(seen)
    assert embedder.embed_query("海边") == []
    assert len(seen) == calls_before


def test_wrong_dimension_counts_as_failure():
    """侧车回了 768 维（1.0 的 chinese-clip 维度）——**配置错，不是抖动**。

    放过去的话向量带着正确的 model_name 落库，检索侧永远算不出相似度却不报错。
    """
    embedder, seen = build(lambda request: httpx.Response(200, json={"vector": [0.5] * 768}))

    assert embedder.embed_query("海边") == []
    assert "768" in embedder.load_error
    assert str(DIMENSION) in embedder.load_error

    for _ in range(FAILURE_THRESHOLD - 1):
        embedder.embed_query("海边")
    calls_before = len(seen)
    assert embedder.embed_query("海边") == []
    assert len(seen) == calls_before, "维度不符若不计入失败，熔断永远不跳"


# ---- 空输入：不计失败、不打网络 --------------------------------------------------


def test_empty_input_does_not_call_the_sidecar_or_count_as_failure():
    """空输入**不计入失败**——否则「用户问了个空问题」会把整条嵌入通道熔断。"""
    embedder, seen = build(lambda request: httpx.Response(500, json={}))

    for _ in range(FAILURE_THRESHOLD):
        assert embedder.embed_query("   ") == []
        assert embedder.embed_image("") == []
    assert seen == [], "空输入不该打网络——那是一次注定没用的往返"

    # 反向验证：上面那些空调用一次都没记进失败计数。若记了，熔断早就跳了，
    # 下面这次真正的失败就不会到达网络。
    embedder.embed_query("海边")
    assert paths(seen) == ["/embed_text"]


# ---- 熔断 ------------------------------------------------------------------------


def test_circuit_opens_then_recovers_after_the_window(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(wemm, "time", clock)
    embedder, seen = build(lambda request: httpx.Response(500, json={}))

    for _ in range(FAILURE_THRESHOLD):
        embedder.embed_query("海边")
    calls_before = len(seen)
    assert calls_before == FAILURE_THRESHOLD

    assert embedder.embed_query("海边") == []
    assert len(seen) == calls_before, "熔断期间不该再打网络"

    # 窗还没过完，照旧不打。
    clock.advance(CIRCUIT_OPEN_S - 0.5)
    assert embedder.embed_query("海边") == []
    assert len(seen) == calls_before

    # 过了窗就恢复：重新打网络（这一次仍失败，是失败本身，不是熔断拦截）。
    clock.advance(1.0)
    embedder.embed_query("海边")
    assert len(seen) == calls_before + 1


def test_success_resets_the_consecutive_failure_count():
    state = {"fail": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["fail"]:
            return httpx.Response(500, json={})
        return httpx.Response(200, json={"vector": [0.5] * DIMENSION})

    embedder, seen = build(handler)
    for _ in range(FAILURE_THRESHOLD - 1):
        embedder.embed_query("海边")

    state["fail"] = False
    embedder.embed_query("海边")  # 这一次成功，计数器归零

    state["fail"] = True
    calls_before = len(seen)
    # 再连失败 4 次也不该断开——阈值是「连续」5 次。
    for _ in range(FAILURE_THRESHOLD - 1):
        embedder.embed_query("海边")
    assert len(seen) == calls_before + (FAILURE_THRESHOLD - 1)


def test_a_tripped_circuit_reports_unavailable(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(wemm, "time", clock)
    embedder, seen = build(lambda request: httpx.Response(500, json={}))

    for _ in range(FAILURE_THRESHOLD):
        embedder.embed_query("海边")
    calls_before = len(seen)

    assert embedder.available is False
    assert len(seen) == calls_before, "熔断期间连探活都不该打"


# ---- available 与 load_error 分工 ------------------------------------------------


def test_available_follows_the_health_endpoint():
    embedder, seen = build(lambda request: httpx.Response(200, json={"status": "ok"}))

    assert embedder.available is True
    assert paths(seen) == ["/health"]


def test_available_is_false_when_the_probe_fails():
    embedder, _ = build(refused)

    assert embedder.available is False
    assert "侧车没起" in embedder.load_error


def test_load_error_names_the_cause_while_available_answers_a_different_question():
    """「能不能用」（``available``）与「为什么不能用」（``load_error``）是两件事。

    合成一个布尔值，排查时只剩「不能用」三个字，分不出「侧车没起」与「侧车起了但
    返回垃圾」。这里钉住：一次失败之后 ``available`` 为假，``load_error`` 带上具体原因。
    """
    embedder, _ = build(http_500)

    assert embedder.embed_query("海边") == []

    assert embedder.available is False
    assert "HTTPStatusError" in embedder.load_error
    assert "embed_text" in embedder.load_error


def test_health_probe_has_a_ttl(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(wemm, "time", clock)
    embedder, seen = build(lambda request: httpx.Response(200, json={"status": "ok"}))

    assert embedder.available is True
    assert len(seen) == 1

    clock.advance(HEALTH_TTL_S - 0.5)
    assert embedder.available is True
    assert len(seen) == 1, "TTL 内不该重复探活"

    clock.advance(1.0)
    assert embedder.available is True
    assert len(seen) == 2, "过了 TTL 才重新探活"


def test_the_circuit_window_and_timeout_are_the_ones_in_the_plan():
    """这两个数是**决定**，不是调参：1.0 ``wemm_sidecar.py:72,55`` 的值。"""
    assert CIRCUIT_OPEN_S == 30.0
    assert wemm.DEFAULT_TIMEOUT_S == 60.0
    assert wemm.DEFAULT_MODEL_ID == scheme.WEMM_MODEL
