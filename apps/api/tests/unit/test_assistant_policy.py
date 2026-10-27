"""四个工具的契约与收敛策略的单元测试。

收敛规则全部来自 1.0 实测出的失败：小模型反复改 query 重搜直到工具预算耗尽、同一张图
死磕到底、同样的错误重试到没预算。1.0 把这些散在主循环的十几个裸计数器里，2.0 收进
一个决定。
"""

from __future__ import annotations

import pytest

from sentrix.domain.assistant.evidence_ledger import EvidenceType
from sentrix.domain.assistant.models import ToolCall
from sentrix.domain.assistant.profile import DEFAULT_PROFILE
from sentrix.domain.assistant.task_state import ToolCallRecord
from sentrix.domain.assistant.tool_policy import ToolAction, ToolPolicy
from sentrix.domain.assistant.tools import TOOL_SPECS, TOOLS_BY_NAME, openai_tools


def call(name: str, **arguments: object) -> ToolCall:
    import json

    return ToolCall(call_id=f"call_{name}", name=name, arguments_json=json.dumps(arguments))


def test_the_tool_list_is_exactly_what_ships():
    """七个取证工具 + 一个终止动作。

    **这是一个逐字断言的清单，不是 ``>= 4``。** 工具多一个少一个都会改变模型看到的
    契约，而「多了一个没人注意的工具」正是那种不会报错、只会让模型偶尔调错东西的改动。
    清单变了就必须在这里显式改一次。

    ``final_answer`` 是终止动作而不是取证工具：交付是它上面的一个字段
    （1.0 ``runtime.py:309`` 的 ``final`` 动作），所以它排在最末。
    """
    assert [spec.name for spec in TOOL_SPECS] == [
        "search_memories",
        "query_memory_facts",
        "query_photo_people",
        "inspect_photo",
        "read_photo_text",
        "get_result_page",
        "get_person_profile",
        "final_answer",
    ]


def test_every_tool_declares_the_evidence_it_satisfies():
    """1.0 的 ``produces_evidence`` 只是一句声明，没有任何地方校验工具真的产出了它。

    2.0 用它驱动账本——「工具调用不等于证据」这条纪律（``requirement_completion.py:63``）
    在 2.0 的落点就是这张显式的绑定表。
    """
    assert TOOLS_BY_NAME["search_memories"].satisfies == (EvidenceType.PHOTO_SET,)
    assert TOOLS_BY_NAME["final_answer"].satisfies == (EvidenceType.DELIVERED_PHOTO,)
    assert TOOLS_BY_NAME["get_person_profile"].satisfies == (EvidenceType.PERSON_PROFILE,)


def test_parameters_are_real_json_schema_not_example_values():
    """1.0 的 ``input_schema`` 是示例 dict（``{"query": "", "mode": "best|all|..."}``），
    被 json.dumps 之后拼进提示词——模型看到的是不像 schema 的一段字。这里钉住标准形态。"""
    for spec in TOOL_SPECS:
        assert spec.parameters["type"] == "object"
        assert spec.parameters["additionalProperties"] is False
        assert set(spec.parameters["required"]) == set(spec.required)
        for name in spec.required:
            assert name in spec.parameters["properties"]


def test_openai_tools_wraps_each_spec_as_a_function():
    tools = openai_tools()

    assert len(tools) == len(TOOL_SPECS)
    assert tools[0]["type"] == "function"
    assert tools[0]["function"]["name"] == "search_memories"
    assert tools[0]["function"]["parameters"] == dict(TOOLS_BY_NAME["search_memories"].parameters)


def test_a_valid_call_is_executed_with_parsed_arguments(make_state):
    decision = ToolPolicy().decide(call("search_memories", query="海边"), make_state())

    assert decision.action is ToolAction.EXECUTE
    assert decision.arguments == {"query": "海边"}
    assert decision.signature == "search_memories(query='海边')"


def test_an_unknown_tool_is_denied_with_the_available_names(make_state):
    decision = ToolPolicy().decide(call("delete_everything"), make_state())

    assert decision.action is ToolAction.DENY
    assert decision.code == "unknown_tool"
    assert "search_memories" in decision.reason


def test_broken_json_is_denied_not_raised(make_state):
    state = make_state()
    broken = ToolCall(call_id="c1", name="search_memories", arguments_json="{oops")

    decision = ToolPolicy().decide(broken, state)

    assert decision.action is ToolAction.DENY
    assert decision.code == "bad_arguments"


def test_a_missing_required_argument_is_denied(make_state):
    decision = ToolPolicy().decide(call("get_result_page", page=1), make_state())

    assert decision.action is ToolAction.DENY
    assert decision.code == "missing_argument"
    assert "result_set_id" in decision.reason


def test_search_memories_cannot_be_called_twice(make_state):
    state = make_state()
    policy = ToolPolicy()
    first = call("search_memories", query="海边")
    assert policy.decide(first, state).action is ToolAction.EXECUTE
    state.record(ToolCallRecord(call_id="c1", tool="search_memories", signature="x", status="ok"))

    second = policy.decide(call("search_memories", query="海边2"), state)

    assert second.action is ToolAction.DENY
    assert second.code == "search_call_limit"
    assert "get_result_page" in second.reason


def test_search_limit_does_not_lapse_when_the_model_budget_is_gone(make_state):
    """1.0 的拦截条件里带着 ``and turn.budget.can_model_step()``（``runtime.py:2993``），
    模型步耗尽之后这个 ``and`` 短路，**第二次 search 会被真的执行**。2.0 的计数与预算
    分开，不存在这个短路。

    反向验证：把模型步耗光、工具预算也耗光，拦截仍然成立。
    """
    state = make_state()
    state.model_steps = state.profile.max_steps
    state.record(ToolCallRecord(call_id="c1", tool="search_memories", signature="x", status="ok"))

    decision = ToolPolicy().decide(call("search_memories", query="海边2"), state)

    assert decision.action is ToolAction.DENY
    assert decision.code == "search_call_limit"


def test_a_duplicate_call_replays_the_cached_observation(make_state):
    state = make_state()
    policy = ToolPolicy()
    state.record(
        ToolCallRecord(
            call_id="c1",
            tool="search_memories",
            signature="search_memories(query='海边')",
            status="ok",
            model_visible={"result_set_id": "rs_1"},
        )
    )

    decision = policy.decide(call("search_memories", query="海边"), state)

    assert decision.action is ToolAction.REPLAY
    assert decision.replay == {"result_set_id": "rs_1"}


def test_a_second_duplicate_is_denied(make_state):
    """回放只能给一次：模型拿着同一份结果反复要，等于把模型步烧在一个不会变的答案上。"""
    state = make_state()
    policy = ToolPolicy()
    signature = "search_memories(query='海边')"
    state.record(
        ToolCallRecord(
            call_id="c1",
            tool="search_memories",
            signature=signature,
            status="ok",
            model_visible={"result_set_id": "rs_1"},
        )
    )
    state.record(ToolCallRecord(call_id="c2", tool="search_memories", signature=signature, status="replayed"))

    decision = policy.decide(call("search_memories", query="海边"), state)

    assert decision.action is ToolAction.DENY
    assert decision.code == "duplicate_call_stopped"


def test_tool_budget_exhaustion_denies(make_state):
    """总预算用尽时回 ``tool_budget_exhausted``。

    **这里刻意不用 ``get_result_page`` 当填充物。** 它现在有自己的额度
    （``max_page_calls``），而那道闸排在总预算之前——用它来填会把这条用例变成在测
    另一条规则。凡是带自己额度的工具都不适合当这个填充物。
    """
    state = make_state()
    for index in range(state.profile.max_tool_calls):
        state.record(
            ToolCallRecord(call_id=f"c{index}", tool="query_memory_facts", signature=f"s{index}", status="ok")
        )

    decision = ToolPolicy().decide(call("query_memory_facts", operation="count", subject="asset"), state)

    assert decision.action is ToolAction.DENY
    assert decision.code == "tool_budget_exhausted"


def test_the_same_failure_twice_in_a_row_forces_a_change(make_state):
    state = make_state()
    for index in range(2):
        state.record(
            ToolCallRecord(
                call_id=f"c{index}",
                tool="get_result_page",
                signature=f"s{index}",
                status="error",
                failure_reason="unknown_result_set",
            )
        )

    decision = ToolPolicy().decide(call("get_result_page", result_set_id="rs_1", page=2), state)

    assert decision.action is ToolAction.DENY
    assert decision.code == "repeated_failure"


def test_a_single_failure_does_not_block_the_retry(make_state):
    """反向验证：只失败一次时不该拦——否则模型没有第二次机会修正参数。"""
    state = make_state()
    state.record(
        ToolCallRecord(
            call_id="c1",
            tool="get_result_page",
            signature="s",
            status="error",
            failure_reason="unknown_result_set",
        )
    )

    decision = ToolPolicy().decide(call("get_result_page", result_set_id="rs_1", page=2), state)

    assert decision.action is ToolAction.EXECUTE


def test_repeated_denials_are_stopped_before_the_budget_runs_out(make_state):
    """连撞几次之后**换一个码**拒绝，而不是继续回 ``unknown_tool``。

    （这条原先叫 ``test_denied_calls_do_not_consume_the_tool_budget``，钉的是「被拒不
    计价」那半——§3.4 A 把那半推翻了：被拒也计价，理由见 ``TaskState.tool_round_trips``。）
    这里要钉的是它**没有**被推翻的另一面：计价器变了不改变「同一个工具连试就是卡住」
    这条规则，它排在预算检查之前（W3），所以模型看到的是 ``repeated_failure``。
    """
    state = make_state()
    policy = ToolPolicy()
    for index in range(5):
        policy.decide(call(f"nope_{index}"), state)
        state.record(ToolCallRecord(call_id=f"c{index}", tool="nope", signature=f"s{index}", status="denied"))

    assert state.tool_round_trips == 5
    assert policy.decide(call("search_memories", query="海边"), state).action is ToolAction.EXECUTE


# ---- 精读预算（T123/T124 共用）------------------------------------------------------
#
# 1.0 的 ``inspect_photo`` 与 ``read_photo_text`` 都是 ``cost_class="expensive"``，
# 共用 ``BudgetState.inspections`` 一个计数器。上限取 **1.0 生产 profile 的值**：
# ``PROFILES["goal_driven_candidate"].max_inspections``（``agent_runtime/profile.py``），
# 而 1.0 的 ``start_sentrix_api_8091.sh:66`` 设了 ``SENTRIX_AGENT_PROFILE=`` 那个名字。
# **不是** ``budget_manager.py:17`` 的 dataclass 默认值——那个只在没有 profile 覆盖时生效。
# 所以这条约束是「**一轮总共能精读几张**」，不是「每个工具各几次」——
# 下面第三条用例钉的就是这一句。
#
# 下面这一组用 ``monkeypatch`` 往注册表里塞**假的**昂贵工具：它测的是机制本身，
# 不依赖生产上到底是哪几个工具标了昂贵——那个集合会变（``read_photo_text`` 就在
# 2026-10-08 从 CHEAP 改回了 EXPENSIVE，见本文件末尾那一组）。


def expensive_spec(name: str):
    from sentrix.domain.assistant.tools import CostClass, ToolSpec

    return ToolSpec(
        name=name,
        description="测试用的昂贵工具。",
        brief="测试用。",
        parameters={"type": "object", "properties": {}, "required": []},
        satisfies=(),
        cost_class=CostClass.EXPENSIVE,
    )


def use_expensive_tools(monkeypatch, *names: str) -> None:
    """把假的昂贵工具塞进**那一份**注册表。

    ``task_state`` 与 ``tool_policy`` 都是 ``from ... import TOOLS_BY_NAME``——它们持有
    的是同一个 dict 对象的引用，所以就地改一处两边都看得见，不必各打一次补丁。
    """
    for name in names:
        monkeypatch.setitem(TOOLS_BY_NAME, name, expensive_spec(name))


def test_an_expensive_tool_is_denied_once_the_budget_is_spent(make_state, monkeypatch):
    """预算之内的每一次都放行，**超出 profile 上限**的那一次才拒。

    次数按 ``DEFAULT_PROFILE.max_inspections`` 走而不是写死——上限本身由
    ``test_the_profile_carries_the_1_0_inspection_limit`` 单独钉住。
    """
    use_expensive_tools(monkeypatch, "inspect_photo")
    state = make_state()
    policy = ToolPolicy()

    for index in range(DEFAULT_PROFILE.max_inspections):
        allowed = policy.decide(call("inspect_photo", asset_handle=f"photo_{index}"), state)
        assert allowed.action is not ToolAction.DENY, f"第 {index + 1} 次精读必须放行"
        state.record(ToolCallRecord(call_id=f"c{index}", tool="inspect_photo", signature="x", status="ok"))

    denied = policy.decide(call("inspect_photo", asset_handle="photo_x"), state)

    assert denied.action is ToolAction.DENY
    assert denied.code == "inspection_budget_exhausted"


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("inspect_photo", "read_photo_text"),
        ("read_photo_text", "inspect_photo"),
    ],
)
def test_the_inspection_budget_is_shared_across_the_two_expensive_tools(
    make_state, monkeypatch, first, second
):
    """**这条是 T123/T124 共用的依据。** 精读一次之后，**另一个**昂贵工具也一样被拒——
    1.0 里它们共用一个 ``inspections`` 计数器（上限见 profile）。

    **两个方向都要断言，一个方向是不够的**：只测「先 inspect 再 read」的话，
    把计数写成「按工具名数 inspect_photo」也照样绿——因为那次记录正好就是
    ``inspect_photo``，两边算出同一个 1。定点变异实测确认过这一条（先写的版本漏了它）。
    反过来先记 ``read_photo_text``，那个实现就会算出 0 而放行 ``inspect_photo``，当场红。
    """
    use_expensive_tools(monkeypatch, "inspect_photo", "read_photo_text")
    state = make_state()
    policy = ToolPolicy()
    # 用**同一个**工具记满预算，再看**另一个**昂贵工具是否也被拒——
    # 这才是「共用计数器」的判据。
    for index in range(DEFAULT_PROFILE.max_inspections):
        state.record(ToolCallRecord(call_id=f"c{index}", tool=first, signature="x", status="ok"))

    decision = policy.decide(call(second, asset_handle="photo_1"), state)

    assert decision.action is ToolAction.DENY
    assert decision.code == "inspection_budget_exhausted"


def test_a_cheap_tool_is_not_counted_against_the_inspection_budget(make_state, monkeypatch):
    """便宜的工具怎么调都不吃精读预算——否则这条预算会变成「第二个工具调用」的别名。"""
    use_expensive_tools(monkeypatch, "inspect_photo")
    state = make_state()
    policy = ToolPolicy()
    state.record(ToolCallRecord(call_id="c1", tool="search_memories", signature="s", status="ok"))
    state.record(ToolCallRecord(call_id="c2", tool="get_person_profile", signature="p", status="ok"))

    decision = policy.decide(call("inspect_photo", asset_handle="photo_1"), state)

    assert decision.action is not ToolAction.DENY
    assert state.inspection_calls == 0


def test_a_failed_expensive_call_does_not_consume_the_budget(make_state, monkeypatch):
    """**只有真的执行成功才算一次精读。** 1.0 也是这个口径：``record_tool_call`` 在
    执行之后才调用（``tool_policy.py:61-62``）。读不了图、模型不可用这些失败不该罚预算
    ——那会让模型换一张图重试的机会被一次环境故障吃掉。"""
    use_expensive_tools(monkeypatch, "inspect_photo")
    state = make_state()
    policy = ToolPolicy()
    state.record(
        ToolCallRecord(
            call_id="c1",
            tool="inspect_photo",
            signature="x",
            status="failed",
            failure_reason="file_unavailable",
        )
    )

    decision = policy.decide(call("inspect_photo", asset_handle="photo_1"), state)

    assert decision.action is not ToolAction.DENY
    assert state.inspection_calls == 0


def test_the_two_budgets_report_different_codes(make_state, monkeypatch):
    """工具预算与精读预算是**两笔**，拒绝时说得出是哪一笔满了。

    1.0 把它们合在一次 ``can_tool_call(inspection=...)`` 里，两种耗尽都只回一句
    ``budget exhausted``——模型分不出「别再调工具了」与「可以换张图但不能再精读」。
    """
    from dataclasses import replace

    from sentrix.domain.assistant.profile import DEFAULT_PROFILE

    use_expensive_tools(monkeypatch, "inspect_photo")
    policy = ToolPolicy()

    tool_budget_gone = make_state(profile=replace(DEFAULT_PROFILE, max_tool_calls=0))
    assert policy.decide(call("inspect_photo", asset_handle="photo_1"), tool_budget_gone).code == (
        "tool_budget_exhausted"
    )

    inspection_gone = make_state()
    for index in range(DEFAULT_PROFILE.max_inspections):
        inspection_gone.record(
            ToolCallRecord(call_id=f"c{index}", tool="inspect_photo", signature="x", status="ok")
        )
    assert policy.decide(call("inspect_photo", asset_handle="photo_2"), inspection_gone).code == (
        "inspection_budget_exhausted"
    )


def test_the_profile_carries_the_1_0_inspection_limit():
    """断言的是那个**具体的数 3**：1.0 生产 profile 的 ``max_inspections``。

    **这条要写死数字，不能跟随实现** —— 它守的正是「1.0 到底给几次精读」。
    2.0 一开始抄成了 ``budget_manager.py:17`` 的 dataclass 默认值 1，真机 487 题上
    的后果是：一轮只能精读一张图，而 233 个 ``answered_with_gap`` 里未满足的需求
    集中在 ``visible_text``(103) 与 ``delivered_photo``(82)——都是要看多张图细节的类型。
    1.0 干净完成 96.7%，2.0 只有 22.2%。"""
    from sentrix.domain.assistant.profile import DEFAULT_PROFILE

    assert DEFAULT_PROFILE.max_inspections == 3


# ---- 照片级取证：计费口径、告示与「卡住」的判据 -----------------------------
#
# 这一组的每一条都来自 20261007 那一轮 487 题的实测：123 道 ``step_budget_exhausted``
# 里 102 道判 0、2 分一道没有；762 次工具调用被拒；被拒之后有 51% 的回合继续试同一个
# 工具。


def test_read_photo_text_costs_a_photo_slot_like_1_0():
    """``read_photo_text`` 回到 ``EXPENSIVE``，与 ``inspect_photo`` 共用「每轮 3 次照片级取证」。

    S6/T124 曾把它降成 ``CHEAP``，理由是「读的是已落库的一列文本，照抄 expensive 会
    吃掉 ``inspect_photo`` 的精读预算」——**那条理由的前提是 ``max_inspections = 1``**。
    现在是 3，而代价翻到了另一面：``CHEAP`` 的工具**不受任何单轮次数约束**，只受 6 次
    工具总预算管，连读 6 张就把工具预算与模型步一起用光（``album3v4-026`` 连调 6 次
    ``read_photo_text`` 后一无所获，收尾是一句兜底文案）。

    两者共用这笔账不是硬凑：它们本来就是同一件事——**对某一张照片做一次取证**。
    1.0 的 ``budget_unit="image"`` 说的就是这句话。
    """
    from sentrix.domain.assistant.tools import CostClass

    assert TOOLS_BY_NAME["read_photo_text"].cost_class is CostClass.EXPENSIVE


def test_reading_a_fourth_photo_is_denied(make_state):
    """连读第 4 张被拒——这是「每轮 3 次」在 ``read_photo_text`` 上的落点。"""
    state = make_state()
    policy = ToolPolicy()
    for index in range(DEFAULT_PROFILE.max_inspections):
        allowed = policy.decide(
            call("read_photo_text", result_set_id="rs_1", asset_handle=f"photo_{index}"), state
        )
        assert allowed.action is not ToolAction.DENY, f"第 {index + 1} 张必须放行"
        state.record(
            ToolCallRecord(call_id=f"c{index}", tool="read_photo_text", signature=f"s{index}", status="ok")
        )

    denied = policy.decide(call("read_photo_text", result_set_id="rs_1", asset_handle="photo_9"), state)

    assert denied.action is ToolAction.DENY
    assert denied.code == "inspection_budget_exhausted"


def test_the_inspection_denial_states_the_real_limit(make_state):
    """拒绝文案里的次数必须**现取 profile**，不能写死。

    原来写的是「一轮最多精读一张图」，而 ``max_inspections`` 是 3。额度从 1 改成 3 时
    只改了值、没改文案，模型于是被一个**与现实矛盾**的告示误导：它按「必须换下一张」
    的指示继续调，撞墙后告示还说"最多一张"，只能靠试错发现真实边界——实测这类拒绝
    309 次，每次撞墙烧一个模型步。
    """
    from dataclasses import replace

    state = make_state()
    for index in range(DEFAULT_PROFILE.max_inspections):
        state.record(
            ToolCallRecord(call_id=f"c{index}", tool="inspect_photo", signature=f"s{index}", status="ok")
        )

    decision = ToolPolicy().decide(call("inspect_photo", result_set_id="rs_1", question="这是什么"), state)

    assert decision.code == "inspection_budget_exhausted"
    assert str(DEFAULT_PROFILE.max_inspections) in decision.reason
    assert "一轮最多精读一张图" not in decision.reason

    # 换个 profile，文案跟着换——这才叫「现取」。
    tight = make_state(profile=replace(DEFAULT_PROFILE, max_inspections=1))
    tight.record(ToolCallRecord(call_id="c0", tool="inspect_photo", signature="s0", status="ok"))
    assert (
        "最多 1 次"
        in ToolPolicy().decide(call("inspect_photo", result_set_id="rs_1", question="这是什么"), tight).reason
    )


def test_a_stuck_tool_is_refused_before_the_budget_code(make_state):
    """「别再用同一个工具重试」必须**排在预算检查之前**。

    预算一旦用尽，每次调用都会命中同一个预算码返回——排在它后面的 ``repeated_failure``
    于是**永远走不到**。实测 309 次 ``inspection_budget_exhausted`` 而
    ``repeated_failure`` 只 24 次：顺序反了，这条规则等于不存在。

    判据也不该要求「同一个失败原因」（那是「同一种错别再犯」），而该问**这个工具是不是
    已经卡住了**：预算类拒绝每次的原因都完全合法，可连试六次就是卡住。
    """
    state = make_state()
    for index in range(DEFAULT_PROFILE.max_inspections):
        state.record(
            ToolCallRecord(call_id=f"ok{index}", tool="inspect_photo", signature=f"s{index}", status="ok")
        )
    policy = ToolPolicy()

    # 两次死磕：都被预算拒，每次记一条 denied。
    for index in range(2):
        denied = policy.decide(call("inspect_photo", result_set_id="rs_1", question="这是什么"), state)
        assert denied.code == "inspection_budget_exhausted"
        state.record(
            ToolCallRecord(
                call_id=f"d{index}",
                tool="inspect_photo",
                signature=f"d{index}",
                status="denied",
                failure_reason=denied.code,
            )
        )

    stuck = policy.decide(call("inspect_photo", result_set_id="rs_1", question="这是什么"), state)

    assert stuck.code == "repeated_failure"
    assert "换一个动作" in stuck.reason


def test_a_denied_call_does_not_reset_the_stuck_counter(make_state):
    """被拒**不是成功**，不能把「卡住」的计数清零。

    反向用例：如果 ``trailing_failures`` 把 ``denied`` 也算成功，这条护栏就永远不触发。
    """
    state = make_state()
    state.record(ToolCallRecord(call_id="c1", tool="read_photo_text", signature="a", status="denied"))
    state.record(ToolCallRecord(call_id="c2", tool="read_photo_text", signature="b", status="denied"))

    assert state.trailing_failures("read_photo_text") == 2


def test_the_delivery_field_is_required_in_the_schema_the_model_sees():
    """``final_answer.selected_image_handles`` 必须是 **JSON Schema 上的必填项**。

    1.0 的交付字段长在正文 JSON 里，dict 缺键不报错——实测它的 final 动作有
    **239/487（49%）** 根本不带这个字段，而零交付的 210 道里 82% 是"候选里已经有正确
    答案、模型却没选"。标准 JSON Schema 可以把「这一轮交不交图」变成**必须显式回答**
    的问题，这是 1.0 的结构做不到的。
    """
    schema = TOOLS_BY_NAME["final_answer"].as_openai_tool()["function"]["parameters"]

    assert "selected_image_handles" in schema["required"]
    assert schema["properties"]["selected_image_handles"]["maxItems"] == 6


def test_an_empty_delivery_list_is_not_a_missing_argument(make_state):
    """``[]`` 是「这一轮不交图」这个**有效答案**，不是「参数缺失」。

    原来的判据是 ``not arguments.get(name)``，空数组也是 falsy——那会把必填字段变成
    「必须交至少一张图」，而产品口径恰恰是「模型没选 → 交付为空合法，不做回填」。
    """
    decision = ToolPolicy().decide(
        call("final_answer", answer="没有找到能确认的照片。", selected_image_handles=[]), make_state()
    )

    assert decision.action is not ToolAction.DENY

    absent = ToolPolicy().decide(call("final_answer", answer="没有找到能确认的照片。"), make_state())

    assert absent.code == "missing_argument"


# ---- 翻页额度 -----------------------------------------------------------------


def test_paging_is_capped_by_the_size_of_the_result_set(make_state):
    """翻页到额度就拒，并且**说清为什么够了**。

    实测 20261009 那一轮：56 个以 ``step_budget_exhausted`` 收尾的回合里 **53 个最后
    动作都是翻页**——模型被 L2 打回之后不知道说什么就翻页，而越界页返回空仍是 ``ok``，
    它拿不到「到底了」的信号。一次翻页是一个工具往返、一个模型步。
    """
    state = make_state()
    for index in range(state.profile.max_page_calls):
        state.record(
            ToolCallRecord(call_id=f"p{index}", tool="get_result_page", signature=f"p{index}", status="ok")
        )

    decision = ToolPolicy().decide(call("get_result_page", result_set_id="rs_1", page=9), state)

    assert decision.action is ToolAction.DENY
    assert decision.code == "page_call_limit"
    # 文案要说清「你已经看完了」，不是只说「到上限了」——后者会让模型以为再等等就能翻。
    assert "第 2 页从第 13 张开始" in decision.reason
    assert "已经看完" in decision.reason


def test_the_page_cap_comes_before_the_tool_budget(make_state):
    """额度用尽时回的是 ``page_call_limit`` 而不是 ``tool_budget_exhausted``。

    两句话让模型做的下一件事不一样：前者是「这一份候选你看完了，去作答」，
    后者是「别再调工具了」。命中哪一条取决于**先撞上哪道闸**，所以顺序有意义。
    """
    state = make_state()
    for index in range(state.profile.max_page_calls):
        state.record(
            ToolCallRecord(call_id=f"p{index}", tool="get_result_page", signature=f"p{index}", status="ok")
        )
    for index in range(state.profile.max_tool_calls):
        state.record(
            ToolCallRecord(call_id=f"t{index}", tool="query_memory_facts", signature=f"t{index}", status="ok")
        )

    decision = ToolPolicy().decide(call("get_result_page", result_set_id="rs_1", page=9), state)

    assert decision.code == "page_call_limit"
