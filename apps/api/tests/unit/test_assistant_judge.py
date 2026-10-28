"""L2 语义评审的规则侧：裁决解析、严重度分层、提示词形状、fail-open。

钉的是 1.0 ``backend/agent_runtime/judge.py`` 的四条判据：

1. 解析器只认「第一个平衡花括号」里的 JSON，剥 ``` 围栏；解析不出来 → 放行。
2. ``missing_disclosure`` 只是披露建议（style），其余四类才是事实性问题（truth）。
3. 缺 ``faithful`` 键 → 放行（1.0 ``judge.py:161-164``）。
4. 提示词用**完整轨迹**而不是压缩投影——1.0 的注释：投影会丢掉工具返回里的
   ``captured_at``，评审会因此把「依据拍摄时间答对的年份」误判成编造。
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import pytest

from sentrix.application.assistant.judge import FaithfulnessJudge
from sentrix.domain.assistant.judge import (
    JUDGE_SYSTEM,
    PROBLEM_MESSAGES,
    TRUTH_PROBLEM_TYPES,
    build_messages,
    build_user_prompt,
    parse_verdict,
    verdict_from_payload,
)
from sentrix.domain.assistant.models import ChatMessage, ChatRole, ModelReply

# ---- 裁决解析 ---------------------------------------------------------------


def test_a_plain_verdict_parses():
    assert parse_verdict('{"faithful": true}') == {"faithful": True}


def test_a_fenced_verdict_parses():
    assert parse_verdict('```json\n{"faithful": true}\n```') == {"faithful": True}


def test_text_around_the_json_is_ignored():
    assert parse_verdict('好的，我的判断是：{"faithful": false} 以上。') == {"faithful": False}


def test_a_truncated_verdict_is_not_repaired():
    """1.0 只取平衡花括号；没闭合就是解析不出来 → 放行（不做补括号那种猜测）。"""
    assert parse_verdict('{"faithful": false, "problems": [') is None


@pytest.mark.parametrize("raw", ("", "   ", "我不知道", "[1,2,3]"))
def test_unparseable_output_yields_nothing(raw: str):
    assert parse_verdict(raw) is None


# ---- 严重度分层 -------------------------------------------------------------


def test_the_four_truth_problem_types_are_the_1_0_set():
    assert {"fabrication", "contradiction", "omission", "certainty_upgrade"} == TRUTH_PROBLEM_TYPES
    assert "missing_disclosure" not in TRUTH_PROBLEM_TYPES


def test_a_verdict_without_the_faithful_key_lets_the_answer_through():
    verdict = verdict_from_payload({"problems": [{"type": "fabrication"}]})

    assert verdict.faithful


def test_an_unfaithful_verdict_carries_its_problems():
    verdict = verdict_from_payload(
        {"faithful": False, "problems": [{"type": "contradiction", "detail": "工具 2，回答 5"}]}
    )

    assert not verdict.faithful
    assert len(verdict.truth_problems) == 1


def test_missing_disclosure_is_not_a_truth_problem():
    verdict = verdict_from_payload(
        {"faithful": False, "problems": [{"type": "missing_disclosure", "detail": "没提只是候选"}]}
    )

    assert not verdict.faithful
    assert verdict.truth_problems == ()


def test_the_reminder_uses_the_1_0_message_table():
    verdict = verdict_from_payload(
        {"faithful": False, "problems": [{"type": "fabrication", "detail": "观察里没有猫"}]}
    )

    assert PROBLEM_MESSAGES["fabrication"] in verdict.reminder
    assert "观察里没有猫" in verdict.reminder


def test_an_unknown_problem_type_is_treated_as_style():
    """1.0 的严重度映射：不在 ``_TRUTH_TYPES`` 里的类型一律 ``SEVERITY_STYLE``——
    只建议、不退回重写。``missing_disclosure`` 就是这么归的。"""
    verdict = verdict_from_payload({"faithful": False, "problems": [{"type": "something_new"}]})

    assert not verdict.faithful
    assert verdict.truth_problems == ()
    assert verdict.reminder == "。请核对工具观察后重新给出回答。"


# ---- 提示词 -----------------------------------------------------------------


def test_the_judge_prompt_states_the_equivalence_rule():
    """1.0 的整个存在理由：代码不做语义等价判定，交给评审。"""
    assert "数字等价" in JUDGE_SYSTEM
    assert "宁可漏报" in JUDGE_SYSTEM
    assert "只输出一个 JSON 对象" in JUDGE_SYSTEM


def test_the_user_prompt_prefers_the_full_transcript():
    prompt = build_user_prompt(
        query="这是谁",
        answer="是妈妈。",
        transcript=[
            {"role": "tool", "content": "captured_at=2019-07-01"},
            {"role": "user", "content": "这是谁"},
        ],
    )

    assert "完整对话轨迹" in prompt
    assert "captured_at=2019-07-01" in prompt
    assert "模型最终回答：是妈妈。" in prompt


def test_the_transcript_is_capped_at_eight_messages_of_900_chars():
    long_history = [{"role": "user", "content": f"第 {index} 条"} for index in range(20)]
    prompt = build_user_prompt(query="q", answer="a", transcript=long_history)

    assert "第 11 条" not in prompt  # 只留最近 8 条
    assert "第 19 条" in prompt

    huge = build_user_prompt(query="q", answer="a", transcript=[{"role": "user", "content": "长" * 2000}])
    assert "…(截断)" in huge


def test_the_judge_messages_are_one_system_and_one_user():
    call = build_messages(query="q", answer="a")

    assert [item["role"] for item in call.messages] == ["system", "user"]


# ---- 调用侧 -----------------------------------------------------------------


class RecordingJudgeModel:
    def __init__(self, reply: str | Exception) -> None:
        self._reply = reply
        self.calls: list[Sequence[ChatMessage]] = []

    def complete(self, messages: Sequence[ChatMessage], tools: Sequence[object]) -> ModelReply:
        assert tuple(tools) == (), "评审调用不带工具"
        self.calls.append(list(messages))
        if isinstance(self._reply, Exception):
            raise self._reply
        return ModelReply(content=self._reply, finish_reason="stop")


def test_a_failing_call_is_fail_open():
    model = RecordingJudgeModel(RuntimeError("端点挂了"))

    verdict = FaithfulnessJudge(model).review(
        query="q", answer="a", transcript=[ChatMessage(ChatRole.USER, "q")]
    )

    assert verdict.faithful


def test_a_garbled_verdict_is_fail_open():
    model = RecordingJudgeModel("我不评价。")

    verdict = FaithfulnessJudge(model).review(
        query="q", answer="a", transcript=[ChatMessage(ChatRole.USER, "q")]
    )

    assert verdict.faithful


def test_a_truth_problem_comes_back_as_unfaithful():
    payload = json.dumps(
        {"faithful": False, "problems": [{"type": "omission", "detail": "total>0 却说没找到"}]}
    )
    model = RecordingJudgeModel(payload)

    verdict = FaithfulnessJudge(model).review(
        query="q", answer="没找到", transcript=[ChatMessage(ChatRole.USER, "q")]
    )

    assert not verdict.faithful
    assert verdict.truth_problems[0]["type"] == "omission"
    assert model.calls[0][0].role is ChatRole.SYSTEM


def test_include_debug_records_the_review_prompt_and_raw_reply():
    model = RecordingJudgeModel('{"faithful": true, "reason": "一致"}')

    verdict = FaithfulnessJudge(model).review(
        query="车停在哪",
        answer="地下车库",
        transcript=[ChatMessage(ChatRole.USER, "车停在哪")],
        include_debug=True,
        step_id="step_8",
    )

    assert verdict.faithful
    call = verdict.debug_call
    assert call is not None
    assert call["step_id"] == "step_8"
    assert call["call_type"] == "faithfulness_judge"
    assert call["messages"][0]["content"] == JUDGE_SYSTEM
    assert "地下车库" in call["messages"][1]["content"]
    assert call["response"]["content"] == '{"faithful": true, "reason": "一致"}'
    assert call["tools"] == []


def test_a_failing_review_still_records_what_was_sent():
    model = RecordingJudgeModel(RuntimeError("端点挂了"))

    verdict = FaithfulnessJudge(model).review(
        query="q",
        answer="a",
        transcript=[ChatMessage(ChatRole.USER, "q")],
        include_debug=True,
        step_id="step_9",
    )

    assert verdict.faithful
    assert verdict.debug_call is not None
    assert verdict.debug_call["status"] == "error"
    assert verdict.debug_call["error"] == "端点挂了"
    assert verdict.debug_call["messages"]
