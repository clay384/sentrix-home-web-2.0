"""L3 claim-level 事实核验：只验高风险、且没有绑定到 fact 的那几条断言。

旧的 L2 每回合把整段回答 + 最近 8 条原始消息发给模型，531 次调用 / 1.19M prompt
tokens，而 224 次没有任何改动。这一层把输入收窄成「一条 claim + 支撑事实 + requirement」，
只在数字/日期/金额/地点这类**错了就是事实错误**的断言上触发。

它不替代 L1：安全/结构仍由 ``final_guard`` 确定性拦截。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sentrix.domain.assistant.evidence_binding import Fact
from sentrix.domain.assistant.evidence_ledger import EvidenceRequirement, RequirementValueType


class ClaimVerdict(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    CONTRADICTED = "contradicted"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class Claim:
    text: str
    value_type: RequirementValueType


@dataclass(frozen=True, slots=True)
class ClaimReview:
    verdict: ClaimVerdict = ClaimVerdict.AMBIGUOUS
    reason: str = ""
    claim: Claim | None = None
    debug_call: Mapping[str, Any] | None = None

    @property
    def blocks(self) -> bool:
        return self.verdict in {ClaimVerdict.UNSUPPORTED, ClaimVerdict.CONTRADICTED}


#: 高风险的「事实性断言」。只认形状，不认具体内容——具体对错交给模型核验。
_AMOUNT = re.compile(r"(?:[¥￥]\s*\d+(?:\.\d+)?|\d+(?:\.\d+)?\s*(?:元|块钱|块|万元|万|千元|百元))")
_TIME = re.compile(
    r"(?:(?:19|20)\d{2}\s*年|\d{1,2}\s*月(?:\s*\d{1,2}\s*[日号])?|"
    r"春天|夏天|秋天|冬天|春季|夏季|秋季|冬季)"
)
_COUNT = re.compile(r"\d+\s*(?:张|个|人|次|位|名|条|段|场)")
_PLACE = re.compile(
    r"(?:在|去|到|位于)\s*([一-鿿]{2,12}?(?:市|县|区|镇|村|街|路|山|湖|机场|酒店|景区|公园))"
)
_SENTENCE = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?")


def high_risk_claims(answer: str) -> tuple[Claim, ...]:
    """从回答里抽出高风险句子。同一句只取最高的那一类，避免重复核验。"""
    claims: list[Claim] = []
    for sentence in _SENTENCE.findall(answer or ""):
        text = sentence.strip()
        if not text:
            continue
        if _AMOUNT.search(text):
            claims.append(Claim(text, RequirementValueType.AMOUNT))
        elif _TIME.search(text):
            claims.append(Claim(text, RequirementValueType.TIME))
        elif _COUNT.search(text):
            claims.append(Claim(text, RequirementValueType.COUNT))
        elif _PLACE.search(text):
            claims.append(Claim(text, RequirementValueType.PLACE))
    # 同一值类型只留第一条：核验的是「这类断言有没有支撑」，不是逐句打分。
    seen: set[RequirementValueType] = set()
    unique: list[Claim] = []
    for claim in claims:
        if claim.value_type in seen:
            continue
        seen.add(claim.value_type)
        unique.append(claim)
    return tuple(unique)


def _fact_payload(fact: Fact) -> Mapping[str, Any]:
    value = fact.value
    if isinstance(value, (tuple, list, set)):
        value = list(value)[:20]
    return {
        "fact_type": fact.fact_type,
        "value_type": fact.value_type.value,
        "value": value,
        "scope": dict(fact.scope),
        "provenance": fact.provenance,
        "confidence": fact.confidence,
        "coverage": fact.coverage,
    }


def build_claim_messages(
    *,
    query: str,
    claim: Claim,
    facts: Sequence[Fact],
    requirement: EvidenceRequirement | None,
) -> tuple[dict[str, str], ...]:
    system = (
        "你是事实一致性检查器。只核对一条 claim 是否被给定 facts 支撑。\n"
        "facts 是工具观察的唯一可信投影；facts 里没有的内容不能当作已确认。\n"
        "输出且只输出一个 JSON 对象："
        '{"verdict":"supported|unsupported|contradicted|ambiguous","reason":"一句话"}。\n'
        "supported：facts 明确支持 claim；contradicted：facts 与 claim 明确冲突；"
        "unsupported：facts 没有该信息或没有能支撑它的值；ambiguous：信息不足以判断。\n"
        "数字/日期表达等价不算冲突（三个人 = 3 人，2024-03-02 = 2024年3月2日）。"
    )
    relevant = [
        _fact_payload(fact)
        for fact in facts
        if fact.value_type is claim.value_type
    ][:12]
    requirement_payload = (
        {
            "id": requirement.id,
            "value_type": requirement.value_type.value if requirement.value_type else "",
            "scope": dict(requirement.scope),
            "description": requirement.description,
        }
        if requirement is not None
        else None
    )
    user = (
        f"用户问题：{query}\n"
        f"待核验 claim（{claim.value_type.value}）：{claim.text}\n"
        f"requirement：{json.dumps(requirement_payload, ensure_ascii=False)}\n"
        f"facts：{json.dumps(relevant, ensure_ascii=False)}\n"
    )
    return ({"role": "system", "content": system}, {"role": "user", "content": user})


def parse_claim_review(raw: str) -> tuple[ClaimVerdict, str]:
    """解析 ``{"verdict":..., "reason":...}``；解析失败按 ambiguous 放行。"""
    text = (raw or "").strip()
    start = text.find("{")
    if start < 0:
        return ClaimVerdict.AMBIGUOUS, ""
    depth = 0
    end = None
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    if end is None:
        return ClaimVerdict.AMBIGUOUS, ""
    try:
        payload = json.loads(text[start:end])
    except ValueError:
        return ClaimVerdict.AMBIGUOUS, ""
    if not isinstance(payload, Mapping):
        return ClaimVerdict.AMBIGUOUS, ""
    try:
        verdict = ClaimVerdict(str(payload.get("verdict") or ""))
    except ValueError:
        return ClaimVerdict.AMBIGUOUS, ""
    return verdict, str(payload.get("reason") or "")


def claim_requirement(
    claim: Claim, requirements: Sequence[EvidenceRequirement]
) -> EvidenceRequirement | None:
    for requirement in requirements:
        if requirement.value_type is claim.value_type:
            return requirement
    return None
