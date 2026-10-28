"""回合状态、预算与终止原因的单元测试。

这些断言钉的是 ``docs/refactor/behaviour-baseline.md`` 里冻结的助手预算（8 步 / 6 调用 /
180s / ``max_search_calls=1``），以及 1.0 三个失败模式在 2.0 里的结构性修法。
"""

from __future__ import annotations

import math

import pytest

# 唯一一处从 application 层取常量：这条断言的就是**跨层的那条关系**——
# 检索能返回多少张（application 的上限）决定了要翻几次页（domain 的额度）。
from sentrix.application.assistant.tools import MAX_SEARCH_LIMIT
from sentrix.domain.assistant.models import (
    ProgressStage,
    ProgressStatus,
    TerminationReason,
    ToolArgumentError,
    ToolCall,
)
from sentrix.domain.assistant.profile import DEFAULT_PROFILE, TurnProfile
from sentrix.domain.assistant.result_set import PAGE_SIZE
from sentrix.domain.assistant.task_state import TERMINAL, TaskState, ToolCallRecord
from sentrix.domain.assistant.tools import FINAL_ANSWER_TOOL


def make_state(profile: TurnProfile = DEFAULT_PROFILE) -> TaskState:
    """本文件自用；跨文件的版本在 ``conftest.py`` 的 ``make_state`` fixture 里。"""
    return TaskState(
        turn_id="turn_1",
        conversation_id="conv_1",
        scope_id="album1",
        question="给我看看在海边的照片",
        profile=profile,
    )


def test_profile_matches_behaviour_baseline():
    assert DEFAULT_PROFILE.name == "goal_driven_candidate"
    assert DEFAULT_PROFILE.max_steps == 8
    assert DEFAULT_PROFILE.max_tool_calls == 6
    assert DEFAULT_PROFILE.wall_time_s == 180.0
    assert DEFAULT_PROFILE.max_search_calls == 1


def test_termination_reason_has_no_empty_member():
    """1.0 的 25,112 条轨迹里有 11,304 条读不到这个字段。

    根因有两条：2026-09-10 之前的轨迹根本没写这个键，以及政策拒答那条提前 return 绕过了
    收尾分类器（72 条空串）。2.0 的防线是类型——``TurnResult.termination_reason`` 的类型
    就是这个枚举，不是 ``str``、更不是 ``str | None``。这里钉住「每个成员都有非空的
    字符串值」，任何新加的成员都不会退化成空串。
    """
    values = [reason.value for reason in TerminationReason]
    assert all(values)
    assert len(set(values)) == len(values)


def test_emit_allocates_step_index_itself():
    """``step_index`` 由 emit 分配，调用方无从指定——1.0 靠每个发出点自己数。"""
    state = make_state()

    first = state.emit(ProgressStage.PLANNING, ProgressStatus.COMPLETE, "一")
    second = state.emit(ProgressStage.THINKING, ProgressStatus.RUNNING, "二")

    assert (first.step_index, second.step_index) == (1, 2)
    assert [event.step_index for event in state.progress] == [1, 2]


def test_emit_records_a_timestamp_and_payload():
    state = make_state()

    event = state.emit(ProgressStage.PLANNING, ProgressStatus.COMPLETE, "已确认需要哪些证据。", kind="plan")

    # 不断言具体时间（它是机器时钟的函数），只钉住格式与 kind 的传递。
    assert event.timestamp[:2] == "20"
    assert "T" in event.timestamp
    assert event.kind == "plan"


def test_model_step_budget_ignores_the_final_reserve():
    """``can_step`` 只看墙钟是否走完；``can_call_tool`` 才检查 final_reserve。

    1.0 的这个非对称是刻意的（``budget_manager.py:38-51``）：最后 10 秒要留给写答案，
    不能拿去调工具。反过来把 final_reserve 也套到模型步上，就会退化成「预算耗尽后空答案」。
    """
    state = make_state()
    state.started_at -= DEFAULT_PROFILE.wall_time_s - DEFAULT_PROFILE.final_reserve_s / 2

    assert state.wall_remaining() < DEFAULT_PROFILE.final_reserve_s
    assert state.can_step() is True
    assert state.can_call_tool() is False


def test_step_budget_stops_at_the_profile_limit():
    state = make_state()
    state.model_steps = DEFAULT_PROFILE.max_steps

    assert state.can_step() is False


def test_a_denied_call_consumes_the_tool_budget():
    """**被拒也计价。**（§3.4 A）

    原先只数 ``status == "ok"``，抄的是 1.0 的口径——而 1.0 那条口径的前提是**被拒就
    ``break`` 结束回合**（``runtime.py:3318``），拒绝根本不会再来一次。2.0 把拒绝当
    普通工具结果发回、允许重试，于是同一个回合里「被拒」照样烧掉一个模型步：
    实测 762 次被拒里 426 步是「整步全被拒」，占 2667 步的 16%。

    计价器与它计的东西对不上，模型于是能靠「一直撞墙」把 8 个模型步全用光——
    ``step_budget_exhausted`` 的 123 道里 102 道判 0、2 分一道没有。
    """
    state = make_state()
    for status in ("ok", "ok", "denied", "error", "replayed"):
        state.record(ToolCallRecord(call_id="c", tool="search_memories", signature=status, status=status))

    assert state.tool_round_trips == 5


def test_the_terminal_action_is_not_a_tool_round_trip():
    """``final_answer`` 不计：它是作答那一步，不是取证。

    算进来的话，「回答被门禁打回一次」会顺带吃掉一次工具配额，而 6 次这个数是照着
    1.0 的生产 profile 定的。
    """
    state = make_state()
    state.record(ToolCallRecord(call_id="c1", tool="search_memories", signature="s1", status="ok"))
    state.record(ToolCallRecord(call_id="c2", tool=FINAL_ANSWER_TOOL, signature="s2", status="ok"))

    assert state.tool_round_trips == 1


def test_the_step_budget_covers_a_full_tool_budget():
    """一次能用满工具额度的回合必须塞得进模型步预算。（§3.4 B）

    ``max_tool_calls`` 是**工具往返**次数，每一次往返恰好消耗 1 个模型步；此外还要留
    1 步作答、1 步给 L2 评审。所以 ``max_steps >= max_tool_calls + 2``。

    **A 之前这条是假成立的**——被拒可以额外吃步，于是「用完 6 次工具额度」并不蕴含
    「用掉 6 个模型步」。它在这里被钉住，是为了让两个 profile 常量从此不能各改各的。
    """
    assert DEFAULT_PROFILE.max_steps >= DEFAULT_PROFILE.max_tool_calls + 1 + 1


def test_search_calls_counts_every_issued_search():
    """``search_calls`` 数的是「模型发出了几次 search_memories」，被拒的那次也要算。

    1.0 的计数器在**每次** search action 上自增（含被拒的那次，``runtime.py:2992``）。
    不算的话，模型可以靠「先被拒一次、再重发」绕过 max_search_calls。
    """
    state = make_state()
    state.record(ToolCallRecord(call_id="c1", tool="search_memories", signature="a", status="ok"))
    state.record(ToolCallRecord(call_id="c2", tool="search_memories", signature="b", status="denied"))

    assert state.search_calls == 2


def test_signature_ignores_argument_order():
    left = TaskState.signature("get_result_page", {"page": 2, "result_set_id": "rs_1"})
    right = TaskState.signature("get_result_page", {"result_set_id": "rs_1", "page": 2})

    assert left == right


def test_cached_returns_only_successful_records():
    state = make_state()
    state.record(ToolCallRecord(call_id="c1", tool="search_memories", signature="s", status="error"))

    assert state.cached("s") is None

    state.record(
        ToolCallRecord(
            call_id="c2",
            tool="search_memories",
            signature="s",
            status="ok",
            model_visible={"result_set_id": "rs_1"},
        )
    )

    assert state.cached("s") is not None


def test_replayed_is_false_until_a_replay_is_recorded():
    state = make_state()

    assert state.replayed("s") is False
    state.record(ToolCallRecord(call_id="c1", tool="search_memories", signature="s", status="replayed"))
    assert state.replayed("s") is True


def test_trailing_failures_counts_a_stuck_tool_and_stops_at_a_success():
    """卡住 = **队尾连续没成功**，失败与被拒都算，**不问原因**。

    预算类拒绝每次的原因都完全合法（``inspection_budget_exhausted`` 出现多少次都是对的），
    但模型连试六次就是卡住——而它每试一次烧一个模型步。所以这条判据不能要求「同一个原因」。
    """
    state = make_state()
    state.record(
        ToolCallRecord(
            call_id="c1",
            tool="inspect_photo",
            signature="a",
            status="error",
            failure_reason="no_text_detected",
        )
    )
    state.record(
        ToolCallRecord(
            call_id="c2",
            tool="inspect_photo",
            signature="b",
            status="denied",
            failure_reason="inspection_budget_exhausted",
        )
    )

    # 两次失败，原因不同，仍然算连续。
    assert state.trailing_failures("inspect_photo") == 2
    # 别的工具插在队尾，就不算这个工具的连续了。
    assert state.trailing_failures("search_memories") == 0

    # 一次成功把计数清零。
    state.record(ToolCallRecord(call_id="c3", tool="inspect_photo", signature="c", status="ok"))
    assert state.trailing_failures("inspect_photo") == 0


def test_finish_records_the_reason_and_closes_the_phase():
    state = make_state()

    assert state.termination_reason is None
    assert state.finish(TerminationReason.ANSWERED) is TerminationReason.ANSWERED
    assert state.termination_reason is TerminationReason.ANSWERED
    assert state.phase == TERMINAL


def test_block_guard_code_fires_once_per_code():
    """门禁只提醒一次。1.0 的零检索拦截没有标志位，模型坚持不调工具就能烧光 8 个模型步。"""
    state = make_state()

    assert state.block_guard_code("evidence_gate") is True
    assert state.block_guard_code("evidence_gate") is False
    assert state.block_guard_code("placeholder_leak") is True
    # 拦下不等于改变阶段——阶段由主循环切，标志位只管「这条规则提过醒了没有」。
    assert state.guard_blocked_codes == {"evidence_gate", "placeholder_leak"}


def test_tool_call_parses_a_json_object():
    call = ToolCall(call_id="c1", name="search_memories", arguments_json='{"query": "海边"}')

    assert call.parse() == {"query": "海边"}


def test_tool_call_rejects_broken_json():
    call = ToolCall(call_id="c1", name="search_memories", arguments_json="{oops")

    with pytest.raises(ToolArgumentError):
        call.parse()


def test_tool_call_rejects_a_non_object_payload():
    call = ToolCall(call_id="c1", name="search_memories", arguments_json="[1, 2]")

    with pytest.raises(ToolArgumentError):
        call.parse()


def test_empty_arguments_parse_to_an_empty_mapping():
    assert ToolCall(call_id="c1", name="search_memories").parse() == {}


def test_the_page_budget_covers_the_whole_result_set():
    """翻页额度**由结果集的容量推出来**，不是一个拍出来的数。

    ``search_memories`` 的 ``limit`` 上限 6、每页 6 张 → 一次给全，翻页工具不出现。
    额度小于它会让模型看不完自己检索到的东西（「检索到了」变成「检索到了但看不到」）；
    大于它只会被用在空页上。这条钉住那个关系——改了 limit 或 PAGE_SIZE 而没改额度，
    这里立刻红。
    """
    pages_in_a_full_result_set = math.ceil(MAX_SEARCH_LIMIT / PAGE_SIZE)

    assert DEFAULT_PROFILE.max_page_calls >= pages_in_a_full_result_set
    # 6 张 / 6 张一页 = 1 次。允许 +1 的余量，是为了「一次失败后还能重试」
    # （见 ``test_a_single_failure_does_not_block_the_retry``），但不能大到「可以随便翻」。
    assert DEFAULT_PROFILE.max_page_calls <= pages_in_a_full_result_set + 1


def test_a_denied_page_call_still_counts():
    """被拒的翻页也要计数——与 ``search_calls`` 同一个口径。

    不算的话，模型可以靠「先被拒一次、再重发」绕过上限，而它烧的是模型步，
    那正是这条上限要护住的东西。
    """
    state = make_state()
    state.record(ToolCallRecord(call_id="p1", tool="get_result_page", signature="s1", status="ok"))
    state.record(ToolCallRecord(call_id="p2", tool="get_result_page", signature="s2", status="denied"))

    assert state.page_calls == 2
