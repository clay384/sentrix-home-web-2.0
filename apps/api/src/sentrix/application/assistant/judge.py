"""L2 语义评审的**调用侧**：一次模型调用，失败一律放行。

1.0 ``judge.py:101-198`` 的 ``judge_faithfulness`` 的移植。三处关键行为逐条对齐：

1. **一次模型调用，每个回合最多一次。** 1.0 的调用点带一个每回合只置一次的闩
   （``runtime.py:2753-2760`` 的 ``l2_faithfulness_checked``），注释写着「每问只评审一次」。
2. **调用前计一个模型步。** 1.0 ``runtime.py:2761`` 先 ``record_model_step()`` 再调——
   评审花的也是主循环那份预算，不能白花。
3. **fail-open。** 调用抛异常、输出解析不出来、缺 ``faithful`` 键——全部当「忠实」放行
   （1.0 ``judge.py:195-198``）。评审是兜底不是门槛：它挂了不该把一轮正常的回答也带走。

调用条件照抄 1.0 ``runtime.py:2753``：L1 已通过 **且** 本轮有工具结果 **且** 还有模型步。
没有工具结果就没有可核对的事实，「评审」只会凭空发挥。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sentrix.application.assistant.debug_trace import (
    chat_messages_payload,
    model_reply_payload,
)
from sentrix.domain.assistant.judge import JudgeVerdict, build_messages, parse_verdict, verdict_from_payload
from sentrix.domain.assistant.models import ChatMessage, ChatRole
from sentrix.domain.assistant.ports import ChatModel

log = logging.getLogger("sentrix.assistant.judge")


class FaithfulnessJudge:
    def __init__(self, model: ChatModel) -> None:
        self._model = model

    def review(
        self,
        *,
        query: str,
        answer: str,
        transcript: Sequence[ChatMessage],
        include_debug: bool = False,
        step_id: str = "judge",
    ) -> JudgeVerdict:
        """评审一次。任何失败都返回「忠实」——这是 1.0 的 fail-open 口径。

        ``include_debug`` 只影响**记录**，不影响裁决：即使评审调用失败，也会留下
        「发出去的是什么」这一条，否则一次 fail-open 在轨迹里会像「从未评审过」。
        """
        call = build_messages(
            query=query,
            answer=answer,
            transcript=[{"role": message.role.value, "content": message.content} for message in transcript],
        )
        messages = [ChatMessage(ChatRole(str(item["role"])), str(item["content"])) for item in call.messages]
        started = time.perf_counter()
        debug_call: dict[str, Any] | None = None
        if include_debug:
            debug_call = {
                "step_id": step_id,
                "call_type": "faithfulness_judge",
                "attempt": 1,
                "started_at": datetime.now(UTC).isoformat(),
                "status": "error",
                "messages": chat_messages_payload(messages),
                "tools": [],
                "response": None,
            }
        try:
            # 评审不带工具：它要的是一段裁决 JSON，不是一次工具往返。
            reply = self._model.complete(messages, ())
        except Exception as error:
            log.warning("L2 评审调用失败，按忠实放行", exc_info=True)
            if debug_call is not None:
                debug_call.update(
                    error_type=type(error).__name__,
                    error=str(error),
                    duration_ms=round((time.perf_counter() - started) * 1000, 1),
                )
            return JudgeVerdict(debug_call=debug_call)
        if debug_call is not None:
            debug_call.update(
                status="complete",
                prompt_tokens=reply.prompt_tokens,
                completion_tokens=reply.completion_tokens,
                finish_reason=reply.finish_reason,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
                response=model_reply_payload(reply),
            )
        payload: Mapping[str, object] | None = parse_verdict(reply.content or "")
        return verdict_from_payload(payload, debug_call=debug_call)
