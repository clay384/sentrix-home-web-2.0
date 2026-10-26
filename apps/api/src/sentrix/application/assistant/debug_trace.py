"""模型调用的完整调试轨迹：把「模型这一步看到什么、回答什么」变成可落盘的 JSON。

**默认不记录。** 只有 ``StartTurnRequest.include_debug=true`` 时，
``RunTurn`` 才会把每次调用的完整 messages / tools / 原始回复收进
``TurnRecord.trace["debug_calls"]``。产品对话不付这份体积，PhotoBench 评测付。

放在 application 层而不是 domain：这是「怎么把领域对象投影成评测侧可读的形状」，
与 ``run_turn._turn_trace`` 同一条分工——domain 只定义模型，不定义调试格式。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sentrix.domain.assistant.models import ChatMessage, ModelReply, ToolCall
from sentrix.domain.assistant.tools import ToolSpec, openai_tools


def tool_call_payload(call: ToolCall) -> dict[str, Any]:
    """一次原生工具调用，保留模型给的**原始参数串**。

    参数解析失败本身是有信息量的（``ToolCall.arguments_json`` 的 docstring）；
    这里不做 ``parse()``，否则调试轨迹会把「模型写坏了参数」显示成一条正常调用。
    """
    return {
        "id": call.call_id,
        "name": call.name,
        "arguments": call.arguments_json,
    }


def chat_messages_payload(messages: Sequence[ChatMessage]) -> list[dict[str, Any]]:
    """``complete()`` 实际收到的消息序列。

    包含唯一一条 system、历史、当前用户消息，以及之前的 assistant / tool 往返。
    这正是「模型看到什么」；少任何一条都会让复盘时对不上模型当时的行为。
    """
    payload: list[dict[str, Any]] = []
    for message in messages:
        item: dict[str, Any] = {
            "role": message.role.value,
            "content": message.content,
        }
        if message.tool_call_id:
            item["tool_call_id"] = message.tool_call_id
        if message.tool_calls:
            item["tool_calls"] = [tool_call_payload(call) for call in message.tool_calls]
        payload.append(item)
    return payload


def tool_specs_payload(specs: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    """``complete()`` 实际收到的 ``tools`` 字段。

    **必须走 ``openai_tools``**，不能只记工具名：JIT 剪枝会按当前未决需求换掉每份
    spec 的 description，而「模型当时能调哪些工具、看到的承诺是什么」正是要看的东西。
    """
    return [dict(item) for item in openai_tools(specs)]


def model_reply_payload(reply: ModelReply) -> dict[str, Any]:
    """端点的原始回复，含被剥掉的思考通道与原始工具调用参数。

    ``content`` 是已经剥掉 ``<|channel>thought`` 的正文（用户看到的那一份）；
    ``thought`` 单独保留——它是模型为什么这么答的唯一直接证据。
    """
    return {
        "content": reply.content,
        "thought": reply.thought,
        "tool_calls": [tool_call_payload(call) for call in reply.tool_calls],
        "finish_reason": reply.finish_reason,
        "prompt_tokens": reply.prompt_tokens,
        "completion_tokens": reply.completion_tokens,
        "tool_argument_parse_facts": [dict(item) for item in reply.tool_argument_parse_facts],
    }
