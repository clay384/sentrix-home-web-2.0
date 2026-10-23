"""规划器：一次模型调用，让模型声明「回答这个问题最少需要哪些证据」。

1.0 ``backend/agent_runtime/goal_planner.py`` 的忠实移植。移植的是**机制**，四件事逐条对齐：

1. **规划是模型调用。** 要产出的东西是「这道题需要哪几类证据」，那是个语义判断，不是
   规则能判的——1.0 的 ``intent.py`` / ``canonical_intent.py`` 试过规则路线，结果是
   S3 之前那版纯函数规划。
2. **最多三次，带错误回灌重试。** 1.0 的注释写明理由：「模型看不到自己的输出和具体错误
   就无法自我纠正……单纯重发同样的提示词没用（模型会犯同样的错），必须把上一轮的原文和
   失败原因一起回灌」。次数从 ``SENTRIX_PLANNER_MAX_ATTEMPTS`` 读，默认 3——1.0 的注释：
   「实测第 2 次能救回绝大多数格式/scope 类失败，再往上收益很小」。
3. **调用失败 = ``planner_call_error``。** 这是 1.0 的失败原因之一，2.0 保留它。审计确认
   这条路径只在模型端点不可用时出现（见 ``intent.py`` 与 ``docs/refactor/s3-deviations.md``），
   它不是设计缺陷，而是一个真实的传输失败——端点挂了就以 ``planner_blocked`` 收场，
   而不是假装规划成功、拿一条假需求去检索。
   **但收尾文案必须说「模型服务」，不能跟着说「规划不出来」**（用户 2026-09-25 裁决二）：
   这个原因底下是基础设施故障，用户据此该去看那个端点，而不是换个问法重问。两种情形的
   分岔在 ``run_turn`` 的 ``_planner_failure_answer``；为此这里把服务侧的说明带在
   ``PlannerResult.detail`` 上。
4. **裸 ``except`` 必须留下真实异常。** 1.0 的注释：「裸 except 会让 planner 失败原因
   彻底消失（外层只看到 planner_call_error，分不清是超时、连接被拒还是 400）」。这里
   同样 ``log.exception`` 一条，再返回 ``planner_call_error``。
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from sentrix.domain.assistant.evidence_ledger import EvidenceRequirement
from sentrix.domain.assistant.intent import (
    build_prompt,
    correction_hint,
    history_prompt_suffix,
    parse_declaration,
)
from sentrix.domain.assistant.models import ChatMessage, ChatRole
from sentrix.domain.assistant.ports import ChatModel, ModelServiceError

log = logging.getLogger("sentrix.assistant.goal_planner")

DEFAULT_MAX_ATTEMPTS = 3

# 模型调用**抛异常**时的失败原因（1.0 ``goal_planner.py:135``）。与 ``intent.py`` 里那三个
# 「模型答了但答得不对」的原因（``invalid_planner_action`` / ``scope_mismatch`` /
# ``no_requirements``）是两类东西，收尾文案也因此分岔——见 ``run_turn``。
PLANNER_CALL_ERROR = "planner_call_error"


@dataclass(frozen=True, slots=True)
class PlannerResult:
    """一次规划的结果。``requirements`` 为 ``None`` 表示没规划出来，``fallback_reason``
    说明是哪一种失败。

    ``detail`` 只在 ``planner_call_error`` 且异常来自模型服务这一侧时非空：它是**给用户
    看的可执行说明**（异常自己已经写好了端点地址），不是诊断堆栈。
    """

    requirements: tuple[EvidenceRequirement, ...] | None = None
    fallback_reason: str = ""
    raw: str = ""
    detail: str = ""
    model_calls: tuple[dict[str, Any], ...] = ()

    @property
    def ok(self) -> bool:
        return self.requirements is not None


class GoalPlanner:
    def __init__(self, model: ChatModel, *, max_attempts: int | None = None) -> None:
        self._model = model
        self.max_attempts = (
            max_attempts
            if max_attempts is not None
            else max(1, int(os.getenv("SENTRIX_PLANNER_MAX_ATTEMPTS", str(DEFAULT_MAX_ATTEMPTS))))
        )

    def declare(
        self,
        question: str,
        *,
        scope_id: str,
        history: str = "",
        text_mode: bool = False,
        mixed_mode: bool = False,
    ) -> PlannerResult:
        # 历史折进 system 文本，不再插一条 system 消息——严格 chat template 要求 system
        # 必须在开头（1.0 ``goal_planner.py:115-117``，实测 24 题因此失败）。
        system = build_prompt(text_mode=text_mode, mixed_mode=mixed_mode) + history_prompt_suffix(history)
        base = [ChatMessage(ChatRole.SYSTEM, system), ChatMessage(ChatRole.USER, question)]

        messages = list(base)
        last_raw = ""
        last_reason = "invalid_planner_action"
        calls: list[dict[str, Any]] = []
        for attempt in range(self.max_attempts):
            started = time.perf_counter()
            call: dict[str, Any] = {
                "step_id": f"planner_{attempt + 1}",
                "call_type": "planner",
                "status": "error",
            }
            try:
                # 规划不带工具：它要的是一段 JSON 声明，不是一次工具往返。
                reply = self._model.complete(messages, ())
                call.update(
                    status="complete",
                    prompt_tokens=reply.prompt_tokens,
                    completion_tokens=reply.completion_tokens,
                    finish_reason=reply.finish_reason,
                )
            except Exception as error:
                log.exception("planner call failed: scope=%s attempt=%s", scope_id, attempt + 1)
                # 只有模型服务这一侧的失败才带说明：它的文案里已经写明了端点地址，直接
                # 转述比在上层另拼一句更准。别的原因（2.0 自己拼错了请求）不往里塞，
                # 免得把内部形状说给用户听。
                call.update(error_type=type(error).__name__, error=str(error))
                detail = str(error) if isinstance(error, ModelServiceError) else ""
                return PlannerResult(
                    fallback_reason=PLANNER_CALL_ERROR, detail=detail, model_calls=tuple([*calls, call])
                )
            finally:
                call["duration_ms"] = round((time.perf_counter() - started) * 1000, 1)
                calls.append(call)

            last_raw = reply.content
            requirements, reason = parse_declaration(last_raw or "", scope_id=scope_id, question=question)
            if requirements is not None:
                return PlannerResult(requirements=requirements, raw=last_raw, model_calls=tuple(calls))
            last_reason = reason
            if attempt + 1 >= self.max_attempts:
                break
            messages = [
                *base,
                ChatMessage(ChatRole.ASSISTANT, str(last_raw or "")),
                ChatMessage(ChatRole.USER, correction_hint(reason)),
            ]
        return PlannerResult(fallback_reason=last_reason, raw=last_raw, model_calls=tuple(calls))
