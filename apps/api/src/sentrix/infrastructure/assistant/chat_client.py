"""8100 上的对话端点（OpenAI 兼容 ``/v1``）——llama.cpp 与 vLLM 都是它。

对应 1.0 ``backend/model_clients.py`` 的 OpenAI 分支。**模型名不在这里写死**：两个后端
协议相同、只在「校不校验模型名」上不同，所以名字由 ``ServedModel`` 问端点要（见
``model_endpoint.py`` 的模块注释）。

三处与 1.0 的差异，每一处都指向一个实测出的失败：

1. **原生 ``tools`` / ``tool_calls``，不再让模型在正文里写 JSON。**
   1.0 要模型输出 ``{"action": "tool_call", ...}`` 这样的正文 JSON，为此写了一套
   150 行的解析修复器（``runtime.py:1586-1661``：剥围栏、补花括号、补引号、删尾随逗号），
   失败三次就整轮以「输出无法解析」收场。实测 8100 的 llama.cpp 原生支持 tools
   （``finish_reason="tool_calls"``），这一整套解析器连同它的失败模式一起消失。
2. **``messages[0]`` 一定是 system，且只有一条。** 1.0 的 ``400 System message must be
   at the beginning`` 就是这么来的——中途 append 一条 ``role=system``，宽松的 Gemma
   模板容忍，严格的 Qwen 模板直接 400（PhotoBench 上 84% 的题在第二步 preflight 挂掉）。
   1.0 的修法是 ``_merge_system_constraint`` 把新约束折进 messages[0]；2.0 更简单：
   系统提示词只在开头拼一次，之后不再往里加东西。
3. **``max_tokens`` 按剩余上下文算，而窗口是部署事实。** 1.0 是
   ``min(1501, 4400 - prompt_tokens)``、下限 64（``app.py:2680-2692``），那个 4400 是照
   它自己的 ``max_model_len = 4501`` 算的。2.0 的窗口由 ``Settings.vlm_context_tokens``
   给（生产 6144 = 8100 的 ``--ctx-size 24576 ÷ --parallel 4``）；**剩余不足 256 时
   不发请求**，抛 ``PromptTooLongError`` 让调用方压缩历史——那条下限 64 只会把
   「提示词太大」伪装成「模型服务挂了」。估算**必须把 tools 的 schema 一起算进来**，
   它走的是同一个窗口。
4. **思考通道在这里剥掉。** Gemma 4 的推理写在 ``<|channel>thought\\n……<channel|>`` 里，
   8100 没开 ``--reasoning-parser``，于是这一段原样落在 ``content`` 上。实测（2026-09-25）
   工具往返两轮之后模型给出的最终 content 是
   ``'<|channel>thought\\n<channel|>为您找到了去年在海边的照片，请查看。'``——用户看到
   的就是这串标记开头的句子。剥的位置只能是这里，理由见 ``split_thought_channel``。
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from sentrix.domain.assistant.models import ChatMessage, ChatRole, ModelReply, ToolCall
from sentrix.domain.assistant.ports import (
    ModelServiceError,
    OutputTruncatedError,
    PromptTooLongError,
)
from sentrix.domain.assistant.tools import ToolSpec, openai_tools
from sentrix.infrastructure.assistant.model_endpoint import ServedModel

log = logging.getLogger("sentrix.assistant.chat_client")

# Gemma 4 的通道语法：``<|channel>名字\\n内容<channel|>``。模型只用 ``thought`` 这一个
# 名字（模板里唯一写死的就是它，见模型目录的 ``chat_template.jinja``）。
_CHANNEL_OPEN = "<|channel>"
_CHANNEL_CLOSE = "<channel|>"

# 1.0 ``app.py:2680-2692`` 的两个人工常数：给回答留的 token 上限，与安全余量。
#
# **``PROMPT_BUDGET_TOKENS = 4400`` 已被删除，它是一条静默失败。** 那个数是照 1.0
# 自己的 ``max_model_len = 4501`` 算的（1.0 注释原文：「Phase H H4：max_model_len=4501
# 的硬上限保护」），而 2.0 的窗口是 **6144**（8100 的 ``--ctx-size 24576 ÷ --parallel 4``）。
# 常量没跟着换，于是 ``4400 - prompt_tokens`` 在提示词超过 4336 token 时给负数，被
# ``max(MIN_ANSWER_TOKENS, …)`` 抬成 64——**发出一个「窗口装得下、但输出只够写半个
# JSON」的请求**，端点回 500（解析失败）或 400（超窗），而用户看到的是「模型服务不可用」。
# 实测两轮 487 题里 22 道是这么来的。窗口现在从 ``Settings.vlm_context_tokens`` 传进来。
# ``1501`` 被改成 ``2048``，理由与上面删掉 4400 是同一条：**它也是从 1.0 自己的窗口推
# 出来的**（``4501 - 101`` 的安全余量），端点换了之后那个数就不再有意义。
#
# **它现在的职责只剩「延迟护栏」，与窗口无关**——窗口那部分由 ``context - prompt`` 管
# （见 ``_max_tokens``）。留着它的理由是一个跑飞的生成会拖过 180s 的墙钟预算。
#
# 2048 的依据是实测：20261009 那一轮 11 道题以「工具调用 JSON 被截断」死掉，最大的一处
# 断在 **5683 个字符**（参数是 ASCII 为主的 JSON，约 1500–1700 token）——**正好卡在 1501
# 上**。这是那批失败的直接成因。
MAX_ANSWER_TOKENS = 2048
# 估算器是字符数 × 系数，和端点真实 tokenizer 会有 1~几十 token 的漂移。窗口
# 恰好 4096 时，2049 input + 2048 output 只差 1 token 也会被 vLLM 400 拒掉；
# 留一段固定余量，宁可少生成，也不要把整轮打成 model_error。
CONTEXT_SAFETY_TOKENS = 128

# 无论上下文多满都必须留下的最小生成量。**它不是 64。**
# 一次工具调用的 JSON（工具名 + 一到四个参数 + ``selected_image_handles`` 数组）写不完
# 就是一次解析失败，而解析失败在用户那儿与「模型服务挂了」长得一样。256 是给
# ``final_answer`` 那种最长的一次调用留的余量。
MIN_USABLE_OUTPUT_TOKENS = 256

#: 提示词达到窗口的这个比例就**主动**压缩历史，不等只剩 256 token 的输出余量。
#: 2026-10-20 实测：0.75 在 153 的 487q 上触发 **239 次**压缩（旧阈值 57 次），
#: 六图集虽受保护，但更早的 inspect/read_text 证据被丢，均分 1.004→0.95。
#: 0.85 留出约 15% 窗口给下一轮工具结果与回答，仍早于「只剩 256 token」的硬边。
PROMPT_COMPACTION_RATIO = 0.85

# 估算提示词 token 的权重。**两个段分开算，这是实测出来的。**
#
# 2026-10-10 在 8100 上实测（``qwen35-q4km``），两段的 token/字符比**差了 44%**：
#
# | 段 | 实测 | 取用 | 为什么 |
# |---|---:|---:|---|
# | 中文正文（prompt / 工具结果 / 回答） | **0.622** | 0.68 | 留 9% 余量 |
# | 工具表 JSON（名字 + brief + 参数 schema） | **0.431** | 0.48 | JSON 的标点在词表里更省 |
#
# **一刀切 0.7 是一段历史上的错。** 它把工具表高估了 62%、正文高估 13%，合起来让
# 一条真实 5500 token（离 6144 还有 600 余量）的请求被估算成 6500+ 而**拒发**——
# 实测 20261010 那一轮这道闸响了 **222 次**，而同一轮端点**一次真超窗都没有**
# （``model_error`` = 0，成功请求的真实峰值 prompt 只有 5229）。
#
# 每次误报的代价是：压缩掉一整轮工具往返（丢证据）+ 多花一个模型步。
TOKENS_PER_CHAR = 0.68
TOOLS_TOKENS_PER_CHAR = 0.48

# 不传 ``context_tokens`` 时用的窗口。**只是为了测试与本地起得来**，生产必须由
# ``Settings.vlm_context_tokens`` 给——窗口是部署事实（见该字段的注释）。
DEFAULT_CONTEXT_TOKENS = 6144


class ChatModelError(RuntimeError):
    """2.0 自己构造的请求不合法：system 必须在第一条，且只有一条。

    **它不表示端点有问题**，所以它不是 ``ModelServiceError``。分开是有后果的：模型服务
    这一侧的失败会让用户看到「模型服务当前不可用」，而这条在用户那儿根本不成立——端点
    好好的，是 2.0 拼错了消息序列。生产里它不可达：调用方只有 ``goal_planner`` 与
    ``run_turn``，两者都把 system 放第一条。
    """


def _dump_message(message: ChatMessage) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": message.role.value, "content": message.content}
    if message.role is ChatRole.ASSISTANT and message.tool_calls:
        # 带 tool_calls 的 assistant 消息，``content`` 必须是 **null** 而不是空串。
        # 实测 8100（gemma4 模板）：写成 ``""`` 时工具结果回填后的下一轮，模型直接吐
        # 一个 EOS（``finish_reason="stop"``、1 个 completion token、content 为空），
        # 整个回合因此以「模型不可用」收场——而模型其实一点问题都没有。
        # 这也是 OpenAI 的约定：有 tool_calls 时 content 可为 null。
        payload["content"] = message.content or None
        payload["tool_calls"] = [
            {
                "id": call.call_id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments_json},
            }
            for call in message.tool_calls
        ]
    if message.role is ChatRole.TOOL:
        payload["tool_call_id"] = message.tool_call_id
    return payload


def split_thought_channel(text: str) -> tuple[str, str]:
    """把 ``content`` 拆成（给用户看的答案，模型的思考）。

    **为什么剥在客户端，而不是让 8100 加 ``--reasoning-parser``。** 8100 是 1.0 与 2.0
    共用的端点，一个服务端 flag 会把 8091 那边 1.0 的答案也一起改掉。用户 2026-09-25
    的裁决是「2.0 加就行，别动老代码」，所以这动作只能落在 2.0 自己这一侧。

    **为什么不用 ``chat_template_kwargs={"enable_thinking": False}``。** 实测 vLLM
    0.26.0 支持这个 per-request 字段（``enable_thinking=True`` 会改变输出，证明它被吃进
    了模板），但 gemma-4 模板的 ``enable_thinking | default(false)`` 本来就是 False，
    传 False 与不传逐字相同——挡不住这段标记。不产生做不到，就只能产生后再擦掉。

    **语义照抄模板自己的 ``strip_thinking`` 宏**（``chat_template.jinja:156``）：它是模型
    作者对这段标记的定义，2.0 没有理由另发明一套。逐条对应：

    - 闭合的通道整个删掉，两边正文都留下；
    - 只有开始符、没有闭合符（被截断）时，它后面的一切都不算答案——宁可这轮判成
      「没有内容」（``run_turn`` 会走 ``parse_failure`` 重试），也不能把半截标记端出去；
    - 只有闭合符、没有开始符（实测于 ``enable_thinking=True``）同样删掉；
    - **除此之外一个字都不动**，连首尾空白都原样透传。

    ``thought`` 是通道体，每段去掉名字行（``<|channel>`` 后面那一行是通道名）后按序拼起
    来，空段丢弃。它是真实产出，所以它有个去处，不是被静默扔掉。
    """
    kept: list[str] = []
    thought: list[str] = []
    for part in text.split(_CHANNEL_CLOSE):
        head, opened, rest = part.partition(_CHANNEL_OPEN)
        kept.append(head)
        if opened:
            # 第一行是通道名（本例恒为 ``thought``），不是内容。
            _name, newline, body = rest.partition("\n")
            if newline and body.strip():
                thought.append(body.strip())
    return "".join(kept), "\n".join(thought)


#: llama.cpp 解析不了工具调用参数时正文里的那句。**按它认「被切断」，不按状态码**——
#: 500 也可能是别的意思，而这一句是端点自己说出来的、
#: 唯一能区分「写不完」与「端点坏了」的证据。
_TRUNCATED_TOOL_CALL = "Failed to parse tool call arguments"


#: llama.cpp 真超窗时正文里的那句。**与「工具调用被截断」是两回事**：
#: 那个是生成写不完（500），这个是提示词根本装不下（400）。两者的修法都是压缩历史，
#: 所以归在同一个基类下，但**成因不同、判据不同**，分开认。
_CONTEXT_OVERFLOW = "exceeds the available context size"


def _is_context_overflow(response: httpx.Response) -> bool:
    """这次失败是不是「提示词超出窗口」。

    措辞变了会静默退化回 ``ModelServiceError``（也就是没有这道兜底的行为），不会更糟——
    只是失去了一次「自动压缩重试」的机会。
    """
    return _CONTEXT_OVERFLOW in response.text


def _is_truncated_tool_call(response: httpx.Response) -> bool:
    """这次失败是不是「工具调用的 JSON 被切断」。

    **按端点的原话认，不按状态码。** 500 还可能是别的东西；而这个说法只在
    ``max_tokens`` 把生成切在半句上时出现。认错方向的代价不对称：判成截断 → 压缩重试
    一次（最多白花一个模型步）；判成服务故障 → 用户看到「模型服务不可用」而端点好好的，
    而且**下一轮还会一模一样地失败**。

    措辞变了会静默退化成 ``ModelServiceError``（也就是现在的行为），不会更糟。
    """
    return _TRUNCATED_TOOL_CALL in response.text


def _read_tool_calls(raw: object) -> tuple[ToolCall, ...]:
    if not isinstance(raw, list):
        return ()
    calls: list[ToolCall] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            continue
        function = item.get("function")
        if not isinstance(function, Mapping):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        arguments = function.get("arguments")
        calls.append(
            ToolCall(
                call_id=str(item.get("id") or f"call_{index}"),
                name=name,
                arguments_json=arguments if isinstance(arguments, str) else json.dumps(arguments),
            )
        )
    return tuple(calls)


def _tool_parse_facts(raw: object) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(raw, list):
        return ()
    facts: list[Mapping[str, Any]] = []
    for index, item in enumerate(raw):
        function = item.get("function") if isinstance(item, dict) else None
        arguments = function.get("arguments") if isinstance(function, dict) else None
        valid = False
        if isinstance(arguments, str):
            with contextlib.suppress(ValueError):
                valid = isinstance(json.loads(arguments), dict)
        elif isinstance(arguments, dict):
            valid = True
        facts.append(
            {
                "index": index,
                "json_object_valid": valid,
                "envelope_valid": isinstance(function, dict) and bool(function.get("name")),
            }
        )
    return tuple(facts)


class OpenAIChatModel:
    """同步 HTTP 客户端。2.0 不管理 8100 的进程，只连它。

    ``model`` 是一个 ``ServedModel``（问端点要名字），**不是字符串**：收字符串就等于允许
    调用方把客户端绑死在某个后端上，而换后端是运维动作——2026-09-24 就是这么坏的。
    详见 ``model_endpoint.py``。
    """

    def __init__(
        self,
        base_url: str,
        model: ServedModel,
        *,
        timeout_s: float = 120.0,
        context_tokens: int = DEFAULT_CONTEXT_TOKENS,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout_s = timeout_s
        # 单个并发槽的窗口。**必须由部署方给**（``Settings.vlm_context_tokens``），
        # 默认值只是让测试与本地起得来，不代表生产。
        self._context_tokens = context_tokens

    def complete(self, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec]) -> ModelReply:
        return self._complete(messages, tools)

    def complete_structured(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        *,
        response_format: Mapping[str, Any],
        chat_template_kwargs: Mapping[str, Any] | None = None,
    ) -> ModelReply:
        """JSON Schema 约束解码。端点不认这两个字段时退回普通请求。

        llama.cpp 在「thinking 开启 + 复杂 schema」下会回 500（peg-native format），
        vLLM 的 ``response_format`` 支持范围也不一致。规划器把 schema 当**增强**而不是
        前提：失败一次就退回原路径，不会因为端点不支持而让整轮规划失败。
        """
        try:
            return self._complete(
                messages,
                tools,
                response_format=response_format,
                chat_template_kwargs=chat_template_kwargs,
            )
        except (PromptTooLongError, OutputTruncatedError):
            # 这两条与 schema 无关，回退普通请求只会再失败一次。
            raise
        except ModelServiceError as error:
            log.warning("端点不接受结构化输出，回退普通请求：%s", error)
            return self._complete(messages, tools)

    def _complete(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        *,
        response_format: Mapping[str, Any] | None = None,
        chat_template_kwargs: Mapping[str, Any] | None = None,
    ) -> ModelReply:
        if not messages or messages[0].role is not ChatRole.SYSTEM:
            raise ChatModelError("对话必须以一条 system 消息开头（严格 chat template 的硬要求）")
        if any(message.role is ChatRole.SYSTEM for message in messages[1:]):
            raise ChatModelError("system 消息只能出现在开头；后续约束请折进第一条")

        # 名字先拿到再请求：拿不到就在这里失败，不会带着一个空名字去问端点。
        model_name = self._model.current()
        response = self._post(
            model_name,
            messages,
            tools,
            response_format=response_format,
            chat_template_kwargs=chat_template_kwargs,
        )

        if response.status_code == 404:
            # 名字对不上时 vLLM 回的就是 404（llama.cpp 不校验名字，这条只在 vLLM 上出现），
            # 所以这个 404 是**端点自己**在说「你手里的名字过时了」——作废缓存重探一次。
            # 只有名字真的变了才重发：没变说明这个 404 是别的意思，拿同一个名字再问一遍
            # 只会拿到同一个错，白花一次调用。
            self._model.invalidate()
            refreshed = self._model.current()
            if refreshed != model_name:
                model_name = refreshed
                response = self._post(
                    model_name,
                    messages,
                    tools,
                    response_format=response_format,
                    chat_template_kwargs=chat_template_kwargs,
                )

        if response.status_code != 200:
            # **先认「这一句是被切断的」。** llama.cpp 解析不了工具调用的参数时会回 500，
            # 原因是 ``max_tokens`` 在半句 JSON 上把它切了——那不是端点的问题，是这一轮
            # 留给输出的余量不够，而余量由提示词决定（腾出上下文，余量就回来）。归成
            # ``ModelServiceError`` 会让用户看到「模型服务当前不可用」，而那句话在这里
            # 不成立。实测 20261009 那一轮 11 道题是这么死的。
            # **端点说「装不下」时，信端点，不信自己的估算。**
            #
            # 上面那道闸是**预测**，而预测永远追不上服务端真实的 tokenizer——实测两轮
            # 都翻过车（先低估 24% 放过 16 条注定 400 的请求，后高估 62% 误拒 222 次）。
            # 端点真超窗时回的就是这一句 400，它是**反馈**，比任何估算都准。
            #
            # 归成 ``PromptTooLongError`` 而不是 ``ModelServiceError``：修法是同一条
            # （压缩历史再试），而「模型服务不可用」这句话在这里不成立。
            if _is_context_overflow(response):
                raise PromptTooLongError(
                    f"提示词超出端点的上下文窗口（{response.status_code}）：{response.text[:200]}"
                )
            if _is_truncated_tool_call(response):
                raise OutputTruncatedError(
                    f"模型端点 {self._base_url} 上的 {model_name} 写工具调用时被截断"
                    f"（{response.status_code}）：{response.text[:200]}"
                )
            raise ModelServiceError(
                f"模型端点 {self._base_url} 上的 {model_name} 返回 {response.status_code}："
                f"{response.text[:200]}"
            )

        try:
            body = response.json()
            choice = body["choices"][0]
            message = choice["message"]
        except (ValueError, KeyError, IndexError) as error:
            raise ModelServiceError(f"模型响应结构不可识别：{response.text[:200]}") from error

        usage = body.get("usage") or {}
        content, thought = split_thought_channel(str(message.get("content") or ""))
        if thought:
            # 不静默：内容留在 ``ModelReply.thought`` 上，这里再留一行痕，便于回答看起来
            # 「莫名其妙」时倒查模型当时在想什么。
            log.debug("模型给出了 %d 字的思考通道内容（不展示给用户）", len(thought))
        return ModelReply(
            content=content,
            thought=thought,
            tool_calls=_read_tool_calls(message.get("tool_calls")),
            finish_reason=str(choice.get("finish_reason") or ""),
            tool_argument_parse_facts=_tool_parse_facts(message.get("tool_calls")),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )

    def _post(
        self,
        model_name: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        *,
        response_format: Mapping[str, Any] | None = None,
        chat_template_kwargs: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        prompt_tokens = self.estimate_prompt_tokens(messages, tools)
        budget = self._max_tokens(messages, tools)
        if budget < MIN_USABLE_OUTPUT_TOKENS:
            # **不发这个请求。** 发出去的结果只有两种，两种都会说错原因：端点回 500
            # （生成被截断、JSON 解析不了）或 400（超窗），而 ``complete`` 把它们统一
            # 报成 ``ModelServiceError`` ——用户看到「模型服务当前不可用」，运维去看端点
            # 却什么毛病都没有。真因是**提示词太大**，那是调用方可以处理的（压缩历史）。
            raise PromptTooLongError(
                f"提示词约 {prompt_tokens} token，单个并发槽的上下文上限 {self._context_tokens}，"
                f"只剩 {budget} 不足以写完一次工具调用（至少需要 {MIN_USABLE_OUTPUT_TOKENS}）。"
            )
        if self.prompt_over_soft_limit(prompt_tokens):
            raise PromptTooLongError(
                f"提示词约 {prompt_tokens} token，超过软上限 "
                f"{int(self._context_tokens * PROMPT_COMPACTION_RATIO)}"
                f"（窗口 {self._context_tokens} 的 {int(PROMPT_COMPACTION_RATIO * 100)}%）；"
                "先压缩历史再试。"
            )

        payload: dict[str, Any] = {
            "model": model_name,
            "messages": [_dump_message(message) for message in messages],
            "temperature": 0.0,
            "stream": False,
            "max_tokens": budget,
        }
        if tools:
            payload["tools"] = list(openai_tools(tools))
            payload["tool_choice"] = "auto"
        if response_format is not None:
            payload["response_format"] = dict(response_format)
        if chat_template_kwargs is not None:
            payload["chat_template_kwargs"] = dict(chat_template_kwargs)

        try:
            with httpx.Client(timeout=self._timeout_s) as client:
                return client.post(f"{self._base_url}/chat/completions", json=payload)
        except httpx.HTTPError as error:
            raise ModelServiceError(f"模型端点 {self._base_url} 不可达：{error}") from error

    def prompt_over_soft_limit(self, prompt_tokens: int) -> bool:
        """估算值超过窗口的 ``PROMPT_COMPACTION_RATIO`` 就要求调用方先压历史。

        与「只剩不到 256 token」的硬判据分开：那条是**发出去必然失败**的底线，
        这条是**再带上下一轮工具结果就会顶到窗口**的预警，触发得更早。
        """
        return prompt_tokens > int(self._context_tokens * PROMPT_COMPACTION_RATIO)

    @staticmethod
    def estimate_prompt_tokens(messages: Sequence[ChatMessage], tools: Sequence[ToolSpec] = ()) -> int:
        """提示词 token 的粗估。

        **``tools`` 必须一起算进来。** 它走的是同一个上下文窗口，而原先这一项**根本
        没有被计入**——JIT 之后每转仍要带 900–1800 字符的工具 JSON（工具名 + ≤40 字的
        brief + 参数 schema），那几百 token 是实打实占着窗口的。漏掉它就是漏掉一整块：
        估算说「还装得下」，端点回
        ``request (6591 tokens) exceeds the available context size``。

        估得准不准只影响**生成长度上限与那条拒绝判据**，不影响正确性；
        真要精确就得再打一次 tokenize 端点。
        """
        text = sum(len(message.content) for message in messages)
        estimate = text * TOKENS_PER_CHAR
        if tools:
            # **两段分开算**：工具表是 JSON，token/字符比只有正文的七成。见上面那张表。
            schema = len(json.dumps(list(openai_tools(tools)), ensure_ascii=False))
            estimate += schema * TOOLS_TOKENS_PER_CHAR
        return int(estimate)

    def _max_tokens(self, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec] = ()) -> int:
        """这次生成最多能写多少 token。**不含任何「能发就发」的下限。**

        旧公式是 ``max(64, min(1501, 4400 - prompt))``——那个 64 的下限把「提示词已经
        大到写不完了」这件事盖住了：它照样发请求，然后拿到一次截断（500）或超窗（400）。
        现在剩余量不足 ``MIN_USABLE_OUTPUT_TOKENS`` 时**不发**，由 ``_post`` 抛
        ``PromptTooLongError``，调用方压缩历史再试。见 ``ports.PromptTooLongError``。
        """
        available = (
            self._context_tokens - self.estimate_prompt_tokens(messages, tools) - CONTEXT_SAFETY_TOKENS
        )
        return min(MAX_ANSWER_TOKENS, available)
