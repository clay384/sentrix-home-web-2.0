from __future__ import annotations

from collections.abc import Sequence

from sentrix.application.assistant.claim_verifier import ClaimVerifier
from sentrix.domain.assistant.claim_verifier import (
    ClaimVerdict,
    build_claim_messages,
    high_risk_claims,
    parse_claim_review,
)
from sentrix.domain.assistant.evidence_binding import Fact
from sentrix.domain.assistant.evidence_ledger import RequirementValueType
from sentrix.domain.assistant.models import ChatMessage, ModelReply


def test_high_risk_claims_extract_amount_time_count_and_place():
    claims = high_risk_claims(
        "婚宴一共花了 3000 元。那天是 2017 年 10 月。一共拍了 12 张照片。"
        "婚礼在河北省石家庄市举办。"
    )

    assert [claim.value_type for claim in claims] == [
        RequirementValueType.AMOUNT,
        RequirementValueType.TIME,
        RequirementValueType.COUNT,
        RequirementValueType.PLACE,
    ]


def test_a_plain_sentence_has_no_claims():
    assert high_risk_claims("照片里看起来很开心。") == ()


def test_parse_claim_review_reads_the_first_json_object():
    verdict, reason = parse_claim_review(
        '{"verdict":"contradicted","reason":"工具是 5，回答写 3"} trailing'
    )
    assert verdict is ClaimVerdict.CONTRADICTED
    assert reason == "工具是 5，回答写 3"


def test_parse_claim_review_fails_open():
    assert parse_claim_review("不是 JSON")[0] is ClaimVerdict.AMBIGUOUS


def test_claim_messages_only_include_matching_fact_types():
    claim = high_risk_claims("婚宴一共花了 3000 元。")[0]
    facts = [
        Fact("asset_amount", RequirementValueType.AMOUNT, 3000, provenance="a"),
        Fact("asset_count", RequirementValueType.COUNT, 5, provenance="b"),
    ]

    messages = build_claim_messages(query="婚宴花了多少钱", claim=claim, facts=facts, requirement=None)

    assert "asset_amount" in messages[1]["content"]
    assert "asset_count" not in messages[1]["content"]


class _ScriptedModel:
    def __init__(self, content: str | Exception) -> None:
        self.content = content
        self.calls: list[Sequence[ChatMessage]] = []

    def complete(self, messages: Sequence[ChatMessage], tools: Sequence[object]) -> ModelReply:
        self.calls.append(messages)
        if isinstance(self.content, Exception):
            raise self.content
        return ModelReply(content=self.content, finish_reason="stop")


def test_claim_verifier_parses_a_contradiction():
    model = _ScriptedModel('{"verdict":"contradicted","reason":"工具是 5"}')
    claim = high_risk_claims("一共 3 张照片。")[0]

    review = ClaimVerifier(model).review(query="几张", claim=claim, facts=[], requirement=None)

    assert review.verdict is ClaimVerdict.CONTRADICTED
    assert review.blocks


def test_claim_verifier_fails_open_on_model_error():
    model = _ScriptedModel(RuntimeError("endpoint down"))
    claim = high_risk_claims("一共 3 张照片。")[0]

    review = ClaimVerifier(model).review(query="几张", claim=claim, facts=[], requirement=None)

    assert review.verdict is ClaimVerdict.AMBIGUOUS
    assert not review.blocks
