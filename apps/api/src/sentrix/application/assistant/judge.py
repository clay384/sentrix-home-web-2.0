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
from collections.abc import Callable, Mapping, Sequence

from sentrix.domain.assistant.judge import JudgeVerdict, build_messages, parse_verdict, verdict_from_payload
from sentrix.domain.assistant.models import ChatMessage, ChatRole, ModelReply
from sentrix.domain.assistant.ports import ChatModel
from sentrix.domain.assistant.tools import ToolSpec

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
        complete: Callable[[Sequence[ChatMessage], Sequence[ToolSpec]], ModelReply] | None = None,
    ) -> JudgeVerdict:
        """评审一次。任何失败都返回「忠实」——这是 1.0 的 fail-open 口径。"""
        call = build_messages(
            query=query,
            answer=answer,
            transcript=[{"role": message.role.value, "content": message.content} for message in transcript],
        )
        messages = [ChatMessage(ChatRole(str(item["role"])), str(item["content"])) for item in call.messages]
        try:
            # 评审不带工具：它要的是一段裁决 JSON，不是一次工具往返。
            # The turn supplies its recorder so fail-open never discards operational evidence.
            reply = (complete or self._model.complete)(messages, ())
        except Exception:
            log.warning("L2 评审调用失败，按忠实放行", exc_info=True)
            return JudgeVerdict()
        payload: Mapping[str, object] | None = parse_verdict(reply.content or "")
        return verdict_from_payload(payload)
