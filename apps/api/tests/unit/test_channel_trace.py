"""``channel_trace``（T125）：**S6 的验收条件**——证据包的 trace 能展开。

判据只有一条，但它有方向性：**「这一路跑了没找到」与「这一路根本没跑」必须分得开**。
只有每候选的 ``channels`` 时两者长得一模一样（都没有候选带那个键），而 S6 要证明的
恰恰是「五路真的都在跑」。所以这份用例的主体是**对照**：

    同一条 query、同一批候选，只把 text_ann 换成一个抛异常的通道
    → 候选列表一模一样，trace 里多出一条 ``error`` + 原因。

两半都断言：只断言「trace 里有 error」的话，一个「永远写五条、不管跑没跑」的实现也能过；
只断言「候选没变」的话，trace 根本没被验到。

通道用桩件而不是真件——这一层要控制的是**通道失败与降级**，真件走不出那两条路
（``RetrieveTextAnn`` 在嵌入器挂掉时降级成空表，不抛）。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from sentrix.application.retrieval.retrieve_evidence import (
    ENTITY_CHANNEL,
    LEXICAL_CHANNEL,
    METADATA_CHANNEL,
    TEXT_CHANNEL,
    VISUAL_CHANNEL,
    EvidenceRequest,
    RetrieveEvidence,
)
from sentrix.application.retrieval.retrieve_text_ann import TextSearchHit, TextSearchResult
from sentrix.application.retrieval.retrieve_visual import VisualSearchHit, VisualSearchResult
from sentrix.domain.retrieval.models import CandidateHit

NOW = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)


def hit(asset_id: str, channel: str, rank: int) -> CandidateHit:
    return CandidateHit(asset_id=asset_id, channel=channel, rank=rank, raw_score=1.0)


class SequenceChannel:
    """一条只回固定候选的通道。``boom`` 时抛——那是「这一路挂了」的形状。"""

    def __init__(self, hits: Sequence[CandidateHit], *, boom: bool = False) -> None:
        self.hits = hits
        self.boom = boom
        self.calls = 0

    def execute(self, *_args: object, **_kwargs: object) -> Sequence[CandidateHit]:
        # 五条通道的调用形状不一样：两个向量通道收一个位置参数（请求对象），
        # 另外三条收关键字。这个桩件两条都要能接——**假件要收下真件会传的每一种形状**。
        self.calls += 1
        if self.boom:
            raise RuntimeError("嵌入器连不上")
        return self.hits


def build(*, text_channel=None, visual=None, text=None) -> RetrieveEvidence:
    return RetrieveEvidence(
        visual=visual or StubVisual(),
        lexical=SequenceChannel([hit("asset_a", LEXICAL_CHANNEL, 1)]),
        metadata=SequenceChannel([]),
        entity=SequenceChannel([]),
        text=text_channel or StubText(),
    )


class StubVisual:
    def execute(self, request: object) -> VisualSearchResult:
        return VisualSearchResult(
            hits=(VisualSearchHit(asset_id="asset_a", score=0.9, channels={VISUAL_CHANNEL: 1}),),
            backend="qdrant",
            authoritative_vectors=300,
        )


class StubText:
    """文本通道的桩件。**两个向量通道不能复用 ``SequenceChannel``**：编排层要读它们
    结果上的 ``backend`` / ``degraded``（那是「为什么这一路没结果」的唯一来源），
    所以桩件必须回一个结果对象，不能回一张裸表。

    ``boom=True`` 是「这一路挂了」；``hits=()`` 是「这一路跑了、一条都没找到」——
    **这两种情形就是 S6 要分开的那两件事**，所以它俩得分别造得出来。
    """

    def __init__(self, *, hits: Sequence[TextSearchHit] | None = None, boom: bool = False) -> None:
        self.hits = (
            hits
            if hits is not None
            else (TextSearchHit(asset_id="asset_b", score=0.8, channels={TEXT_CHANNEL: 1}),)
        )
        self.boom = boom

    def execute(self, request: object) -> TextSearchResult:
        if self.boom:
            raise RuntimeError("bge sidecar 不可达")
        return TextSearchResult(hits=tuple(self.hits), backend="sqlite_exact", authoritative_vectors=120)


def request_() -> EvidenceRequest:
    return EvidenceRequest(text="海边", scope_id="album1", limit=10)


# ---- 五路各一条 -------------------------------------------------------------------


def test_every_channel_gets_a_trace_in_the_fusion_order():
    """**顺序有意义**：anchor 加成按候选第一次出现时的通道给，所以融合的通道顺序
    是行为的一部分（``execute`` 的注释写了这件事）。trace 跟着同一个顺序，
    展开时才与融合看得对得上。"""
    result = build().execute(request_())

    assert [trace.channel for trace in result.channel_trace] == [
        METADATA_CHANNEL,
        ENTITY_CHANNEL,
        LEXICAL_CHANNEL,
        VISUAL_CHANNEL,
        TEXT_CHANNEL,
    ]


def test_the_trace_counts_what_each_channel_returned():
    result = build().execute(request_())

    counts = {trace.channel: trace.candidate_count for trace in result.channel_trace}
    assert counts[LEXICAL_CHANNEL] == 1
    assert counts[METADATA_CHANNEL] == 0
    assert counts[VISUAL_CHANNEL] == 1
    assert counts[TEXT_CHANNEL] == 1


def test_the_vector_channels_name_their_backend():
    """``backend`` 只有两个向量通道有（1.0 也是 ``getattr(retriever, "backend_used", None)``）。
    它是「这一路到底查的哪儿」的答案，没有它就只能猜。"""
    result = build().execute(request_())

    by_channel = {trace.channel: trace for trace in result.channel_trace}
    assert by_channel[VISUAL_CHANNEL].backend == "qdrant"
    assert by_channel[TEXT_CHANNEL].backend == "sqlite_exact"
    assert by_channel[LEXICAL_CHANNEL].backend == ""


# ---- 「这一路挂了」必须在 trace 里留下痕迹 ------------------------------------------


def test_a_broken_channel_still_gets_a_trace_with_a_reason():
    """**这条就是 T125 的验收条件。** 通道抛异常时**照样**留一条 trace，
    带上 ``status="error"`` 与原因——1.0 ``evidence_retrieval.py:207-211``
    的 ``except`` 分支做的是同一件事（``channel_hits[name] = []`` 之后
    trace 里 ``status="error"`` / ``reason=str(error)``）。
    """
    result = build(text_channel=StubText(boom=True)).execute(request_())

    trace = next(item for item in result.channel_trace if item.channel == TEXT_CHANNEL)
    assert trace.status == "error"
    assert trace.reason == "bge sidecar 不可达"
    assert trace.candidate_count == 0


def test_a_broken_channel_does_not_take_the_other_channels_down():
    """**一个通道挂了不该让整次检索挂掉**——这是 1.0 那个 ``except`` 的全部意义。

    断言的是「**另外四路**照样给东西」，不是「结果与没挂时一模一样」：后者会顺带
    要求挂掉那一路本来贡献的候选也还在，那是自相矛盾的（它挂了当然不贡献）。
    """
    broken = build(text_channel=StubText(boom=True)).execute(request_())

    assert [item.asset_id for item in broken.hits] == ["asset_a"], "词法与视觉那两路的候选该在"
    survivors = {trace.channel for trace in broken.channel_trace if trace.status == "ok"}
    assert survivors == {METADATA_CHANNEL, ENTITY_CHANNEL, LEXICAL_CHANNEL, VISUAL_CHANNEL}


def test_a_degraded_channel_is_not_an_error():
    """降级与失败是两回事：降级说明这一路**跑到了**，只是后端换了
    （``VisualSearchResult.degraded`` / ``TextSearchResult.degraded``）。
    两者都非 ``ok``，但把它们合成一个状态会让「修后端」与「修接线」分不开。"""
    result = build(visual=DegradedVisual()).execute(request_())

    trace = next(item for item in result.channel_trace if item.channel == VISUAL_CHANNEL)
    assert trace.status == "degraded"
    assert trace.reason == "派生索引落后于权威存储"
    assert trace.candidate_count == 1, "降级了但它仍然拿到了候选"


class DegradedVisual:
    def execute(self, request: object) -> VisualSearchResult:
        return VisualSearchResult(
            hits=(VisualSearchHit(asset_id="asset_a", score=0.9, channels={VISUAL_CHANNEL: 1}),),
            backend="sqlite_exact",
            degraded=True,
            degraded_reason="派生索引落后于权威存储",
            authoritative_vectors=300,
        )


@pytest.mark.parametrize("broken", [False, True])
def test_a_non_ok_status_always_carries_a_reason(broken: bool):
    """**不变量：``status != "ok"`` ⟹ ``reason != ""``。**

    挡住的是「状态标了 error 却不说为什么」——那种痕迹在展开时等于没展开，
    而排查的人会去猜。两个方向都跑一遍：正常时不许出现非 ok 状态，
    挂着时不许出现空原因。
    """
    result = build(text_channel=StubText(boom=True) if broken else None).execute(request_())

    for trace in result.channel_trace:
        if trace.status != "ok":
            assert trace.reason, f"{trace.channel} 报了 {trace.status} 却没说原因"
        else:
            assert not broken or trace.channel != TEXT_CHANNEL


# ---- 对照：trace 让「沉默的通道」可见 ----------------------------------------------


def test_the_trace_is_what_makes_a_silent_channel_visible():
    """**这条对照是这份用例的主线，也是 S6 的验收条件本身。**

    两种情形：``text_ann`` 跑了但一条都没召回 vs. ``text_ann`` **根本没跑**。
    在候选列表上**完全一样**——都没有任何候选带 ``text_ann`` 键。
    差别**只**在 trace 里。

    只断言「trace 有 error」是不够的（一个永远写五条 trace 的实现也能过）；
    只断言「候选相同」也是不够的（那正是分不开两件事的那种读数）。两半一起断言。
    """
    silent = build(text_channel=StubText(hits=[])).execute(request_())
    broken = build(text_channel=StubText(boom=True)).execute(request_())

    silent_keys = {key for item in silent.hits for key in item.channels}
    broken_keys = {key for item in broken.hits for key in item.channels}
    assert TEXT_CHANNEL not in silent_keys
    assert TEXT_CHANNEL not in broken_keys, "挂掉的那一路本来就该一个候选都不贡献"

    def text_trace(result):
        return next(item for item in result.channel_trace if item.channel == TEXT_CHANNEL)

    assert (text_trace(silent).status, text_trace(silent).reason) == ("ok", "")
    assert text_trace(broken).status == "error"
    assert text_trace(broken).reason == "bge sidecar 不可达"


@pytest.mark.parametrize("channel", ["text_ann", "graph"])
@pytest.mark.parametrize(
    "failure,expected_kind,expected_code",
    [
        ("busy", "resource_error", "database_busy"),
        ("locked", "resource_error", "database_locked"),
        ("timeout", "time_limit", "retrieval_timeout"),
        ("memory", "resource_error", "out_of_memory"),
        ("vector", "resource_error", "vector_index_unavailable"),
    ],
)
def test_typed_resource_failure_keeps_other_hits_and_sanitized_evidence(
    channel, failure, expected_kind, expected_code
):
    import sqlite3

    from sentrix.domain.retrieval.ports import VectorIndexUnavailable

    error = {
        "busy": sqlite3.OperationalError("private SQL and path"),
        "locked": sqlite3.OperationalError("private SQL and path"),
        "timeout": TimeoutError("private SQL and path"),
        "memory": MemoryError("private SQL and path"),
        "vector": VectorIndexUnavailable("private SQL and path"),
    }[failure]
    if failure in ("busy", "locked"):
        error.sqlite_errorcode = 5 if failure == "busy" else 262  # SQLITE_LOCKED_SHAREDCACHE

    class Broken:
        def execute(self, *_args, **_kwargs):
            raise error

    use_case = build(text_channel=Broken() if channel == "text_ann" else None)
    if channel == "graph":
        use_case._graph = Broken()
    result = use_case.execute(request_())
    trace = next(row for row in result.channel_trace if row.channel == channel)
    assert trace.status == "error" and trace.reason == expected_code
    assert trace.operational_issue["error_code"] == expected_kind
    assert trace.operational_issue["failure_code"] == expected_code
    assert trace.operational_issue["error_type"] == type(error).__name__
    assert "private" not in repr(trace)
    assert any(row.asset_id == "asset_a" for row in result.hits)


def test_real_sqlite_busy_is_classified_without_relying_on_exception_message(tmp_path):
    import sqlite3

    from sentrix.domain.retrieval.ports import retrieval_operational_issue

    with (
        sqlite3.connect(tmp_path / "locked.sqlite3", timeout=0) as first,
        sqlite3.connect(tmp_path / "locked.sqlite3", timeout=0) as second,
    ):
        first.execute("create table test (value text)")
        first.execute("begin exclusive")
        with pytest.raises(sqlite3.OperationalError) as raised:
            second.execute("insert into test values ('private content')")
    issue = retrieval_operational_issue(raised.value)
    assert issue == {
        "error_code": "resource_error",
        "failure_code": "database_busy",
        "error_type": "OperationalError",
        "sqlite_errorcode": sqlite3.SQLITE_BUSY,
    }
    assert retrieval_operational_issue(RuntimeError("database is locked")) is None
    # SQL bugs are not silently reclassified as resource shortages.
    assert retrieval_operational_issue(sqlite3.OperationalError("no such table")) is None
