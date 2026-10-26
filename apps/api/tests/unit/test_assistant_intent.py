"""规划的单元测试：提示词、解析、归一、带错误回灌的重试。

钉的是 1.0 ``backend/agent_runtime/goal_planner.py`` 的行为：解析器的有限修复边界、
``scope_mismatch`` / ``invalid_planner_action`` 的判据、重试次数与回灌内容、
以及 ``planner_call_error`` 只在模型调用**抛异常**时出现。
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import pytest

from sentrix.application.assistant.goal_planner import GoalPlanner
from sentrix.domain.assistant.evidence_ledger import EvidenceType
from sentrix.domain.assistant.intent import (
    MIN_REQUIREMENTS,
    build_prompt,
    correction_hint,
    history_prompt_suffix,
    normalize_payload,
    parse_declaration,
    parse_json,
    planner_evidence_types,
    root_brace_balance,
)
from sentrix.domain.assistant.models import ChatMessage, ModelReply

SCOPE = "album_cbfbfce14789"


def declaration(
    *evidence_types: str,
    goal: str = "找海边的照片",
    scope: str | None = None,
    value_type: str = "media_set",
    required: object = True,
    scope_body: dict | None = None,
) -> str:
    payload: dict = {
        "action": "declare",
        "declaration": {
            "goal": goal,
            "requirements": [
                {
                    "id": f"req_{index + 1}",
                    "evidence_type": item,
                    "description": f"需要 {item}",
                    "predicate": "check",
                    "value_type": value_type,
                    "scope": scope_body or {},
                    "required": required,
                }
                for index, item in enumerate(evidence_types)
            ],
        },
    }
    if scope is not None:
        payload["declaration"]["scope_id"] = scope
    return json.dumps(payload, ensure_ascii=False)


# ---- 提示词 -----------------------------------------------------------------


def test_the_prompt_lists_exactly_the_evidence_registry():
    """注册表是 ``EvidenceType`` 的全部取值，提示词逐条列出来。

    S6 加了四类——``structured_fact``（``query_memory_facts``，T121）、
    ``photo_identity``（``query_photo_people`` / ``inspect_photo``，T122/T123）、
    ``visual_observation``（``inspect_photo``，T123）与 ``visible_text``
    （``read_photo_text``，T124）。四类和另外三类一样是
    「模型可以向规划器声明的需求」，所以照旧**由枚举唯一决定**，不另开一张表
    ——这也是这条用例断言的是 ``planner_evidence_types()`` 而不是字面量的原因。
    """
    prompt = build_prompt()

    for item in planner_evidence_types():
        assert f"- {item}" in prompt
    assert planner_evidence_types() == (
        "delivered_photo",
        "person_profile",
        "photo_identity",
        "photo_set",
        "structured_fact",
        "visible_text",
        "visual_observation",
    )


def test_delivered_photo_is_declared_by_default_for_real_album_questions():
    """``delivered_photo`` 的判据是「**这是不是一道关于相册的真问题**」，不是
    「用户有没有说要看图」。

    **两次来回，都记在这里。**
    1.0 的原句是「用户想看照片、要求展示、或问「有没有……的照片」时，声明 ``photo_set``；
    **同时声明** ``delivered_photo``……」——后半句无条件，实测 212 道声明而真正要求
    看图的只有 3 道。
    2026-10-09 收紧成「**只有**用户明确说要看图时才声明」，实测声明数掉到 **51 道**——
    **那个方向也是错的**：用户指着一张婚礼合影问「这是在哪儿拍的」，他没说「给我看看」，
    但他要的显然就是那张照片的答案。

    现在退回中间：**非普通聊天就声明**。普通聊天交给模型自己判——它读得到整句话，
    比在这里跑关键词正则准。

    这条是**文本断言，本身很弱**（证明不了模型照做）。真正的验证靠重跑后的
    「声明了 ``delivered_photo`` 的题数」。留在这里是为了挡住「有人再把它收紧回去」。
    """
    prompt = build_prompt()

    assert "关于这个相册/照片的真问题" in prompt
    assert "同时声明 delivered_photo" in prompt
    # 唯一的不声明条件是与相册无关的闲聊。
    assert "普通聊天" in prompt


def test_the_prompt_declares_the_value_type_contract():
    prompt = build_prompt()

    for value_type in ("place", "time", "duration", "amount", "count", "person", "media_set", "description", "boolean", "value"):
        assert value_type in prompt
    assert "scope" in prompt
    assert "required" in prompt
    assert "哪几类" in prompt


def test_the_prompt_never_shows_a_real_scope_id():
    """1.0 的教训：示例里出现真实 scope_id 会被小模型整段照抄，抄错一位就 scope_mismatch。"""
    assert SCOPE not in build_prompt()
    assert "scope_id" in build_prompt()  # 提示词要明确说「不要输出 scope_id」


def test_history_is_folded_into_the_system_text_not_a_second_system_message():
    assert history_prompt_suffix("") == ""
    assert history_prompt_suffix("user: 你好") == "\n\n历史对话背景：\nuser: 你好"


def test_each_failure_reason_has_its_own_correction_hint():
    assert "scope_id" in correction_hint("scope_mismatch")
    assert "完整闭合" in correction_hint("invalid_planner_action")
    assert "至少要声明一条" in correction_hint("no_requirements")
    assert "value_type" in correction_hint("invalid_requirement")
    assert "required" in correction_hint("invalid_requirement")
    # 认不出来的原因也要给一句通用的修正指令，不能返回空串。
    assert correction_hint("something_else").startswith("你上一次的输出无法被解析")


# ---- 解析器的有限修复 -------------------------------------------------------


def test_a_plain_object_parses():
    assert parse_json('{"action":"declare"}') == {"action": "declare"}


def test_a_fenced_object_parses():
    assert parse_json('```json\n{"action":"declare"}\n```') == {"action": "declare"}


def test_a_trailing_fence_without_a_leading_one_parses():
    """1.0 的注释：结尾围栏必须独立剥离，否则补 ``}`` 的修复路径会被跳过，实测 5 题里 4 题因此失败。"""
    assert parse_json('{"action":"declare"}\n```') == {"action": "declare"}


def test_one_missing_root_brace_is_repaired():
    assert parse_json('{"action":"declare","declaration":{"goal":"g"}') == {
        "action": "declare",
        "declaration": {"goal": "g"},
    }


def test_more_than_one_missing_brace_is_not_repaired():
    """只在「根括号正好差一个」时补——乱补会把坏输出修成一个看似合法的错输出。"""
    with pytest.raises(ValueError):
        parse_json('{"action":"declare","declaration":{"goal":"g"')


def test_trailing_think_tag_and_a_duplicate_object_take_the_first():
    """2026-10-26 实测：模型会在 ``</think>`` 后再重复一个 JSON。"""
    first = '{"action":"declare","declaration":{"goal":"g","requirements":[]}}'
    assert parse_json(first + " </think> " + first) == {
        "action": "declare",
        "declaration": {"goal": "g", "requirements": []},
    }


def test_root_brace_balance_ignores_braces_inside_strings():
    assert root_brace_balance('{"a":"}"}') == 0
    assert root_brace_balance('{"a":"\\""}') == 0
    assert root_brace_balance("{") == 1


# ---- 归一 -------------------------------------------------------------------


def test_scope_and_goal_are_injected_when_the_model_omits_them():
    normalized = normalize_payload(
        {"action": "declare", "requirements": []}, scope_id=SCOPE, default_goal="问题"
    )

    assert normalized["declaration"]["scope_id"] == SCOPE
    assert normalized["declaration"]["goal"] == "问题"


def test_string_requirements_are_expanded_and_deduplicated():
    normalized = normalize_payload(
        {"action": "declare", "requirements": ["photo", "image", "person"]},
        scope_id=SCOPE,
        default_goal="问题",
    )

    assert [item["evidence_type"] for item in normalized["declaration"]["requirements"]] == [
        "photo_set",
        "person_profile",
    ]


@pytest.mark.parametrize(
    ("alias", "expected"),
    (
        ("photo", "photo_set"),
        ("image", "photo_set"),
        ("memory_asset", "photo_set"),
        ("deliver", "delivered_photo"),
    ),
)
def test_known_aliases_map_onto_the_registry(alias: str, expected: str):
    normalized = normalize_payload(
        {"action": "declare", "requirements": [{"evidence_type": alias}]},
        scope_id=SCOPE,
        default_goal="问题",
    )

    assert normalized["declaration"]["requirements"][0]["evidence_type"] == expected


def test_an_unknown_alias_is_left_alone_for_validation_to_reject():
    """认不出来的别名不能静默改写成某个类型——那等于替模型编需求。"""
    normalized = normalize_payload(
        {"action": "declare", "requirements": [{"evidence_type": "location_metadata"}]},
        scope_id=SCOPE,
        default_goal="问题",
    )

    assert normalized["declaration"]["requirements"][0]["evidence_type"] == "location_metadata"
    assert parse_declaration(declaration("location_metadata"), scope_id=SCOPE, question="问题") == (
        None,
        "invalid_planner_action",
    )


# ---- 校验 -------------------------------------------------------------------


def test_a_well_formed_declaration_parses_into_requirements():
    requirements, reason = parse_declaration(
        declaration("photo_set", "delivered_photo"), scope_id=SCOPE, question="问题"
    )

    assert reason == ""
    assert requirements is not None
    assert [item.evidence_type for item in requirements] == [
        EvidenceType.PHOTO_SET,
        EvidenceType.DELIVERED_PHOTO,
    ]


def test_an_invented_scope_is_a_scope_mismatch():
    requirements, reason = parse_declaration(
        declaration("photo_set", scope="album_somewhere_else"), scope_id=SCOPE, question="问题"
    )

    assert (requirements, reason) == (None, "scope_mismatch")


def test_a_missing_scope_is_not_a_mismatch():
    """1.0 的 ``_normalize_payload`` 会把缺失的 scope 补上——模型本来就不该产出它。"""
    requirements, reason = parse_declaration(declaration("photo_set"), scope_id=SCOPE, question="问题")

    assert reason == ""
    assert requirements is not None


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    (
        ({"value_type": "event"}, "invalid_requirement"),
        ({"required": None}, "invalid_requirement"),
        ({"scope_body": {"country": "中国"}}, "invalid_requirement"),
        ({"scope_body": {"place": "<地点>"}}, "invalid_requirement"),
        ({"scope_body": {"time": "未知"}}, "invalid_requirement"),
    ),
)
def test_invalid_structured_requirement_is_rejected(kwargs: dict, expected: str):
    assert parse_declaration(
        declaration("photo_set", **kwargs), scope_id=SCOPE, question="问题"
    ) == (None, expected)


def test_structured_fields_are_preserved_on_the_requirement():
    raw = declaration("photo_set", value_type="place", scope_body={"place": "宜昌"})

    requirements, reason = parse_declaration(raw, scope_id=SCOPE, question="问题")

    assert reason == ""
    assert requirements is not None
    item = requirements[0]
    assert item.predicate == "check"
    assert item.value_type.value == "place"
    assert dict(item.scope) == {"place": "宜昌"}
    assert item.required is True


def test_garbage_is_an_invalid_planner_action():
    assert parse_declaration("我不知道该怎么规划。", scope_id=SCOPE, question="问题") == (
        None,
        "invalid_planner_action",
    )


def test_zero_requirements_is_rejected():
    """1.0 允许零需求；S3 不允许——零需求会让这一轮一次工具都不调。"""
    assert MIN_REQUIREMENTS == 1
    assert parse_declaration(declaration(), scope_id=SCOPE, question="问题") == (
        None,
        "no_requirements",
    )


def test_a_final_action_is_not_a_declaration():
    assert parse_declaration('{"action":"final","answer":"你好"}', scope_id=SCOPE, question="问题") == (
        None,
        "invalid_planner_action",
    )


# ---- 规划器 -----------------------------------------------------------------


class ScriptedPlannerModel:
    """按顺序回话的模型；``Exception`` 表示这次调用抛异常。"""

    def __init__(self, replies: Sequence[str | Exception]) -> None:
        self._replies = list(replies)
        self.calls: list[list[ChatMessage]] = []

    def complete(self, messages: Sequence[ChatMessage], tools: Sequence[object]) -> ModelReply:
        assert tuple(tools) == (), "规划调用不带工具"
        self.calls.append(list(messages))
        reply = self._replies.pop(0) if len(self._replies) > 1 else self._replies[0]
        if isinstance(reply, Exception):
            raise reply
        return ModelReply(content=reply, finish_reason="stop")


class StructuredPlannerModel:
    """实现可选的结构化能力；规划器应优先走它。"""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict[str, object]] = []

    def complete(self, messages: Sequence[ChatMessage], tools: Sequence[object]) -> ModelReply:
        raise AssertionError("有结构化能力时不应退回普通 complete")

    def complete_structured(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[object],
        *,
        response_format: dict,
        chat_template_kwargs: dict | None = None,
    ) -> ModelReply:
        self.calls.append(
            {
                "messages": list(messages),
                "tools": tuple(tools),
                "response_format": response_format,
                "chat_template_kwargs": chat_template_kwargs,
            }
        )
        return ModelReply(content=self.reply, finish_reason="stop")


def test_planner_uses_structured_completion_when_available():
    model = StructuredPlannerModel(declaration("photo_set"))

    result = GoalPlanner(model).declare("给我看看海边的照片", scope_id=SCOPE)

    assert result.ok
    assert len(model.calls) == 1
    call = model.calls[0]
    assert call["tools"] == ()
    response_format = call["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert call["chat_template_kwargs"] == {"enable_thinking": False}


def test_a_clean_declaration_costs_exactly_one_call():
    model = ScriptedPlannerModel([declaration("photo_set")])

    result = GoalPlanner(model).declare("给我看看海边的照片", scope_id=SCOPE)

    assert result.ok
    assert len(model.calls) == 1


def test_a_model_error_becomes_planner_call_error():
    """``planner_call_error`` 只在模型调用**抛异常**时出现——1.0 ``goal_planner.py:135``。"""
    model = ScriptedPlannerModel([RuntimeError("connection refused")])

    result = GoalPlanner(model).declare("给我看看海边的照片", scope_id=SCOPE)

    assert result.fallback_reason == "planner_call_error"
    assert len(result.model_calls) == 1
    assert result.model_calls[0]["status"] == "error"
    assert result.model_calls[0]["error"] == "connection refused"


def test_a_missing_brace_is_repaired_without_a_second_call():
    model = ScriptedPlannerModel([declaration("photo_set")[:-1]])

    result = GoalPlanner(model).declare("给我看看海边的照片", scope_id=SCOPE)

    assert result.ok
    assert len(model.calls) == 1


def test_a_garbled_output_is_retried_with_the_previous_output_rolled_back():
    """重试不是重发同一句话：上一轮的原文与失败原因都要回灌（1.0 的注释写得很直白）。"""
    model = ScriptedPlannerModel(["我不会规划。", declaration("photo_set")])

    result = GoalPlanner(model).declare("给我看看海边的照片", scope_id=SCOPE)

    assert result.ok
    assert len(model.calls) == 2
    second = [message.content for message in model.calls[1]]
    assert "我不会规划。" in second  # 上一轮原文
    assert "无法被解析" in second[-1]  # 失败原因翻译成的修正指令


def test_it_gives_up_after_the_configured_number_of_attempts():
    model = ScriptedPlannerModel(["不是 JSON"])

    result = GoalPlanner(model, max_attempts=3).declare("给我看看海边的照片", scope_id=SCOPE)

    assert result.fallback_reason == "invalid_planner_action"
    assert len(model.calls) == 3


def test_the_default_attempt_count_is_the_1_0_default(monkeypatch):
    monkeypatch.delenv("SENTRIX_PLANNER_MAX_ATTEMPTS", raising=False)
    model = ScriptedPlannerModel(["不是 JSON"])

    GoalPlanner(model).declare("给我看看海边的照片", scope_id=SCOPE)

    assert len(model.calls) == 3


def test_the_attempt_count_can_be_overridden_by_env(monkeypatch):
    """1.0 读 ``SENTRIX_PLANNER_MAX_ATTEMPTS``；这个旋钮照搬。"""
    monkeypatch.setenv("SENTRIX_PLANNER_MAX_ATTEMPTS", "2")
    model = ScriptedPlannerModel(["不是 JSON"])

    GoalPlanner(model).declare("给我看看海边的照片", scope_id=SCOPE)

    assert len(model.calls) == 2


def test_history_goes_into_the_system_message_not_a_second_one():
    model = ScriptedPlannerModel([declaration("photo_set")])

    GoalPlanner(model).declare("那她呢", scope_id=SCOPE, history="user: 妈妈是谁")

    first, second = model.calls[0]
    assert first.role.value == "system"
    assert "历史对话背景：\nuser: 妈妈是谁" in first.content
    assert second.role.value == "user"
    assert all(message.role.value != "system" for message in model.calls[0][1:])


def test_include_debug_records_every_planner_attempt():
    """重试的每一次都要留：只留最后一次就看不到模型第一次错在哪。"""
    model = ScriptedPlannerModel(["我不会规划。", declaration("photo_set")])

    result = GoalPlanner(model).declare("给我看看海边的照片", scope_id=SCOPE, include_debug=True)

    assert result.ok
    assert len(result.debug_calls) == 2
    first, second = result.debug_calls
    assert first["call_type"] == "planner"
    assert first["response"]["content"] == "我不会规划。"
    assert first["messages"][0]["role"] == "system"
    assert second["messages"][-1]["role"] == "user"
    assert "我不会规划。" in second["messages"][-2]["content"]
    assert second["tools"] == []


def test_planner_debug_is_off_by_default():
    model = ScriptedPlannerModel([declaration("photo_set")])

    result = GoalPlanner(model).declare("给我看看海边的照片", scope_id=SCOPE)

    assert result.debug_calls == ()
