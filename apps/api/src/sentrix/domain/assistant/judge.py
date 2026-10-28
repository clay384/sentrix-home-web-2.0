"""L2 语义评审的**规则侧**：提示词、判据、裁决解析。

1.0 ``backend/agent_runtime/judge.py`` 的移植。它和 L1（``final_guard``）是分工的两层，
1.0 的注释把这条分工写得很清楚（``final_guard.py:1-27`` 与 ``judge.py:6-13``）：

    L1 确定性规则，只做结构性兜底：安全 / 占位符 / 交付完整性 / 流程结构。
    L2 模型做语义级合格性判断：编造 / 矛盾 / 漏报 / 过度声称 / 缺口披露。
    回答"是否合格"由模型判断，**代码不用词语/数字正则做等价匹配**——
    "三个人"与"3"是不是同一个数，正则判不对，只会误伤事实正确的回答。

S3 当初把 L2 整体推到 S6，理由是「每次评审是一次模型调用；L1 已兜住安全与结构，
S3 的模型预算要留给主循环」。审计推翻这条理由：1.0 的生产库里有 **1,669 条轨迹带
``agent2_trace.quality.l2_soft_release = true``**（占 6.6%），全部是
``profile=goal_driven_candidate``——L2 在生产默认 profile 下确实在跑，而且确实改写了
回答。它不是纸面能力，是 1.0 已经在用的那一层。预算那句是推测，没有数据。

调用侧（``application/assistant/judge.py``）与 1.0 对齐的三点：一次回合最多评审一次、
评审本身计一个模型步、**任何异常或不可解析的裁决都放行**（fail-open）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

JUDGE_SYSTEM = """你是 Sentrix 家庭记忆助手的“事实一致性评审”。
你的任务是核对模型的最终回答是否忠实于工具观察。

工具观察是唯一可信事实来源。请判断最终回答是否存在以下问题：
注意：完整轨迹里的"观察"不止是读出的文字，还包括照片/视频/事件的拍摄时间（captured_at）、
地点等元数据与检索命中的照片信息；这些元数据与文字同等可信，
回答依据拍摄时间给出年份/日期不算编造。
1. fabrication（编造）：回答断言了观察中明确没有的事实或细节。
   例如观察说“照片中没有猫”，回答却说“猫是白色的”。
2. contradiction（矛盾）：回答与观察直接冲突。例如观察说“多云天气”，回答说“晴天阳光充足”；
   工具确认数量是 5，回答写 3。
3. omission（漏报/否认）：工具返回了结果（total>0 或有观察），
   回答却整体否认“没有找到/没找到/未找到/不存在”。
4. certainty_upgrade（过度声称）：观察/检索只是 candidate_only 或 unknown，
   回答却断言“确认/确定是”。
5. missing_disclosure（缺口未披露）：检索只是 partial/candidate，
   回答完全没提“还不能确认”就当成确定事实。

判定标准（必须严格遵守，宁可漏报，不误伤正确回答）：
- 表达等价不算任何问题：
  - 数字等价：“三个人” == “3 个人” == “3 人” == “3”；“两张” == “2 张”。中文数字与阿拉伯数字等价。
  - 日期格式等价：“2024年3月2日” == “2024-03-02” == “2024.3.2”；“3月” == “03月”。
  - 量词/同义词差异不算冲突（“3 个人” vs “三人”；
    店名/地名表达不同但指同一事物）。
  - 只有在实质数值不同（工具 5、回答 3）或存在性相反时才算 contradiction。
- fabrication 的门槛：观察明确没有该事实、或与观察相反才判；观察未提及、含糊、缺失时一律不判 fabrication。
- certainty_upgrade 的门槛：观察明确是 candidate_only/unknown/无法判断，回答却用“确定是/确认是/就是”断言才判；
  “看起来是/可能是/应该是/不能完全确认”不算。
- omission 的门槛：工具 total>0 且回答整体否认存在才算；回答只答了一部分、遗漏了某个细节不算 omission。
- missing_disclosure 只是披露问题，不影响事实正确性；回答已给出核心事实并自然说明不确定性时不判。

特别注意：
- search_memories 返回 total=0（无候选）时，回答里出现“已为您找到/找到 N 张照片”这类明确交付断言是编造；
  但如实说“没有找到”正确。
- search_memories 返回 total>0（有候选）时，回答里出现“没有找到任何相关照片”是漏报。
- read_photo_text / inspect_photo 返回 supported 的观察中直接读出的内容（价格/文字/颜色/年份/电话/店名）
  是照片复核层证据，模型引用这些细节回答不算编造；不要因为检索层只是 partial/candidate
  就要求模型把这些已读出的细节改成“还不能确认”。
- 诚实的不确定性：回答已给出核心事实，同时说明“还不能完全确认/可能是…”，
  不算 certainty_upgrade 或 missing_disclosure。
- 回答不再复述检索过程（不出现 candidate_only/partial_support/候选照片/匹配程度等内部词汇）
  不代表信息缺失，不应因此判 unfaithful。

只输出一个 JSON 对象，不要 markdown、不要多余文字：
{"faithful": true 或 false, "problems": [{"type": "fabrication|contradiction|omission|
certainty_upgrade|missing_disclosure", "detail": "具体问题"}], "reason": "一句话理由"}"""

# 1.0 ``judge.py:66-72`` 的 ``_JUDGE_MESSAGE``。
PROBLEM_MESSAGES: Mapping[str, str] = {
    "fabrication": "评审认为回答编造了工具观察中没有的事实",
    "contradiction": "评审认为回答与工具观察直接矛盾",
    "omission": "评审认为工具返回了结果但回答却否认/漏报",
    "certainty_upgrade": "评审认为回答把未确认条件说成了确定事实",
    "missing_disclosure": "评审认为回答没有披露部分确认/相似候选的缺口",
}

# 事实性问题（可恢复，不能放行错误答案）；missing_disclosure 只是披露建议。
TRUTH_PROBLEM_TYPES = frozenset({"fabrication", "contradiction", "omission", "certainty_upgrade"})

TRANSCRIPT_LIMIT = 8
TRANSCRIPT_MESSAGE_CHARS = 900
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class JudgeVerdict:
    """评审裁决。``faithful`` 为真时 ``problems`` 为空。

    ``debug_call`` 只在调用方请求 ``include_debug`` 时非空：它是这次评审**实际发出去**
    的消息与原始回复。裁决本身与它无关，所以默认 ``None``，不参与任何判定。
    """

    faithful: bool = True
    problems: tuple[Mapping[str, Any], ...] = ()
    reason: str = ""
    debug_call: Mapping[str, Any] | None = None

    @property
    def truth_problems(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(item for item in self.problems if str(item.get("type") or "") in TRUTH_PROBLEM_TYPES)

    @property
    def reminder(self) -> str:
        """回灌给模型的那句话。1.0 把 ``_JUDGE_MESSAGE`` 与 ``detail`` 拼在一起。"""
        parts: list[str] = []
        for item in self.truth_problems:
            problem_type = str(item.get("type") or "unfaithful")
            message = PROBLEM_MESSAGES.get(problem_type, "评审认为回答与工具观察不一致")
            detail = str(item.get("detail") or "")
            parts.append(f"{message}：{detail}" if detail else message)
        return "；".join(parts) + "。请核对工具观察后重新给出回答。"


def _clip(text: str) -> str:
    squashed = _WHITESPACE.sub(" ", text).strip()
    if len(squashed) > TRANSCRIPT_MESSAGE_CHARS:
        return squashed[:TRANSCRIPT_MESSAGE_CHARS] + "…(截断)"
    return squashed


def parse_verdict(raw: str) -> dict[str, Any] | None:
    """解析评审输出；剥围栏、取第一个平衡花括号。解析不出来返回 ``None``（→ 放行）。"""
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].lstrip()
    start = text.find("{")
    if start < 0:
        return None
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
        return None
    try:
        payload = json.loads(text[start:end])
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def build_user_prompt(
    *,
    query: str,
    answer: str,
    transcript: Sequence[Mapping[str, str]] = (),
) -> str:
    """评审的 user 消息。

    1.0 的取舍：**完整轨迹优先**。``messages`` 拿得到时用最近 8 条原文（每条截 900 字），
    因为压缩投影会丢掉工具返回里的 ``captured_at`` 等字段，评审会因此把「依据拍摄时间
    答对的年份」误判成编造（1.0 ``judge.py:127-147`` 的注释）。
    """
    lines = [
        f"{str(item.get('role') or '?')}: {_clip(str(item.get('content') or ''))}"
        for item in transcript[-TRANSCRIPT_LIMIT:]
        if str(item.get("content") or "").strip()
    ]
    return (
        f"用户问题：{query}\n"
        f"完整对话轨迹（含全部工具返回与 captured_at 等元数据）：\n"
        f"{chr(10).join(lines) or '(无)'}\n"
        f"模型最终回答：{answer}"
    )


def verdict_from_payload(
    payload: Mapping[str, Any] | None,
    *,
    debug_call: Mapping[str, Any] | None = None,
) -> JudgeVerdict:
    """把解析出来的 JSON 变成裁决。缺 ``faithful`` 键一律当放行（1.0 同）。"""
    if not payload or "faithful" not in payload:
        return JudgeVerdict(debug_call=debug_call)
    if payload.get("faithful") is not False:
        return JudgeVerdict(reason=str(payload.get("reason") or ""), debug_call=debug_call)
    problems = tuple(item for item in (payload.get("problems") or []) if isinstance(item, Mapping))
    return JudgeVerdict(
        faithful=False,
        problems=problems,
        reason=str(payload.get("reason") or ""),
        debug_call=debug_call,
    )


@dataclass(frozen=True, slots=True)
class JudgeCall:
    """一次评审要发的两条消息。"""

    messages: tuple[Mapping[str, str], ...] = field(default_factory=tuple)


def build_messages(*, query: str, answer: str, transcript: Sequence[Mapping[str, str]] = ()) -> JudgeCall:
    return JudgeCall(
        messages=(
            {"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user", "content": build_user_prompt(query=query, answer=answer, transcript=transcript)},
        )
    )
