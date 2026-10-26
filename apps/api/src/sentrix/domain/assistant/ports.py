"""助手域端口：模型端点、会话与回合存储、结果集存储、人物目录、进度事件流。

五个端口对应五个「2.0 要自己实现、但 domain 不能知道怎么实现」的东西。装配一律在
``bootstrap/container.py``，字段按这里的协议声明。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from sentrix.domain.assistant.models import (
    Attachment,
    ChatMessage,
    Conversation,
    Message,
    ModelReply,
    PersonProfile,
    ProgressEvent,
    ToolCall,
    TurnRecord,
)
from sentrix.domain.assistant.result_set import ResultSet
from sentrix.domain.assistant.tools import ToolSpec


class ModelServiceError(RuntimeError):
    """模型服务**这一侧**出的问题：端点不可达 / 超时 / 模型名对不上 / 响应不可识别。

    **它为什么必须是一个 domain 里的类型，而不是让调用方去认异常字符串。**
    规划失败底下有两条完全不同的路（用户 2026-09-25 裁决二）：

    - 模型服务挂了——基础设施故障，用户该被告知去看那个端点；
    - 模型答了，但答的东西规划不出需求——模型能力问题。

    两者的用户可见文案必须不同，而 ``application`` 层按分层契约**不许** import
    ``sentrix.infrastructure``（``pyproject.toml`` 的 ``application-purity``），
    于是它认不到 ``infrastructure`` 里那个异常类。把「服务这一侧」的形状声明在端口
    旁边：实现端口的适配器抛它，用端口的用例 catch 它，两边都只依赖 domain。
    """


class PromptTooLongError(ModelServiceError):
    """提示词大到**写不完一次工具调用**，请求根本没有发出去。

    **它与「模型服务不可用」必须分得开。** 原先这条路上没有异常：``max_tokens`` 的公式
    ``max(MIN, min(1501, 4400 - prompt))`` 在 ``prompt > 4336`` 时把下限 64 抬上来，
    于是发出去的是一个「窗口其实装得下、但输出只够写半个 JSON」的请求——端点回 500
    （解析失败）或 400（超窗），而用户看到的是「模型服务当前不可用」。
    实测两轮 487 题共 **22 道**（126 题里）是这么来的。

    这是一条静默失败：把「提示词太大」（可诊断、可回收）伪装成「模型服务挂了」。
    调用方拿到它应当**压缩历史再试**，而不是直接收尾。
    """


class OutputTruncatedError(PromptTooLongError):
    """生成被 ``max_tokens`` 切断，端点因此回 500。

    llama.cpp 的原话是 ``Failed to parse tool call arguments as JSON: … invalid string:
    missing …``——它拿到的是**半句工具调用 JSON**。实测 20261009 那一轮 11 道题是这么死的，
    截断位置从 345 到 **5683** 字符不等。

    **它为什么归在 ``PromptTooLongError`` 底下而不是 ``ModelServiceError``。** 它看起来
    像服务故障（500），但那句话在用户那儿不成立——端点好好的，是**这一轮留给输出的余量
    不够**。而余量是提示词决定的：腾出上下文，余量就回来。所以两者走**同一条修法**
    （压缩历史再试），分成两个类只是为了让轨迹说得出是哪一种：
    ``PromptTooLongError`` 是请求**根本没发**（``_post`` 拦下的），这个是**发出去了、
    写不完**。
    """


class ChatModel(Protocol):
    """OpenAI 兼容的对话模型端点。2.0 只连它，不管理它的进程。

    实现这个端口时，**服务这一侧**的失败抛 ``ModelServiceError``——调用方靠它把
    「基础设施故障」与「模型答得不好」分开收尾。
    """

    def complete(self, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec]) -> ModelReply: ...


@runtime_checkable
class StructuredChatModel(Protocol):
    """**可选**能力：用 JSON Schema 约束解码。

    单独成一个端口，而不是给 ``ChatModel.complete`` 加参数：测试里十几个 fake 只实现
    ``complete``，加必填参数会把它们全部改成样板代码；规划器按 ``isinstance`` 探测，
    探测不到就退回普通 ``complete``，行为与今天完全一致。
    """

    def complete_structured(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        *,
        response_format: Mapping[str, Any],
        chat_template_kwargs: Mapping[str, Any] | None = None,
    ) -> ModelReply: ...


class ConversationRepository(Protocol):
    def create(self, *, scope_id: str, title: str, created_at: datetime) -> Conversation: ...

    def get(self, conversation_id: str) -> Conversation | None: ...

    def list_for_scope(self, scope_id: str, limit: int) -> Sequence[Conversation]: ...

    def rename(self, conversation_id: str, title: str, updated_at: datetime) -> bool: ...

    def delete(self, conversation_id: str, updated_at: datetime) -> bool: ...

    def add_message(
        self,
        *,
        conversation_id: str,
        scope_id: str,
        turn_id: str,
        role: str,
        content: str,
        created_at: datetime,
    ) -> Message: ...

    def list_messages(self, conversation_id: str, limit: int) -> Sequence[Message]: ...

    def history_for_prompt(self, conversation_id: str, limit: int) -> Sequence[Message]: ...


class TurnRepository(Protocol):
    """回合的持久化。SSE 的断线续传完全依赖它——1.0 的进度只存在进程内存的一个 dict
    里，页面一刷新就永远拿不到那次回合了，这正是前端不得不轮询的原因之一。"""

    def create(self, record: TurnRecord) -> None: ...

    def get(self, turn_id: str) -> TurnRecord | None: ...

    def claim(self, turn_id: str, updated_at: datetime) -> bool:
        """把 ``pending`` 置为 ``running``。返回是否抢到——并发只允许一个执行者。"""
        ...

    def append_event(self, turn_id: str, event: ProgressEvent) -> None: ...

    def events_after(self, turn_id: str, step_index: int) -> Sequence[ProgressEvent]: ...

    def finish(
        self,
        turn_id: str,
        *,
        status: str,
        termination_reason: str,
        answer: str,
        attachments: Sequence[Attachment],
        requirements: Sequence[Mapping[str, Any]],
        retrieved_candidates: Sequence[str],
        trace: Mapping[str, Any],
        updated_at: datetime,
    ) -> None: ...

    def list_for_conversation(self, conversation_id: str, limit: int) -> Sequence[TurnRecord]: ...


class ResultSetRepository(Protocol):
    def add(self, result_set: ResultSet) -> None: ...

    def get(self, result_set_id: str) -> ResultSet | None: ...


class PersonDirectory(Protocol):
    """已确认人物的目录：有没有这个人、他在家庭图谱里的归属与关系。

    ``membership`` 缺省是 ``"unknown"``（1.0 ``db.py:2875`` 的 ``or "unknown"``）——
    **没有归属行与「归属不明」是同一件事**，不区分。``relationships`` 为空数组同理：
    查到了人但没有关系数据，不是「查不到这个人」。
    """

    def lookup(self, name: str, scope_id: str) -> PersonProfile | None: ...


class TurnEventBroker(Protocol):
    """把正在跑的回合的进度事件实时推给订阅者。

    这是「前端不轮询」的落点：SSE 连接订阅这个流，事件一到就写出去，而不是每 300ms
    去查一次状态。已结束回合的补偿走 ``TurnRepository.events_after``——两条路合起来
    覆盖「实时」与「迟到加入」，1.0 缺的正是后者。
    """

    def open(self, turn_id: str) -> None:
        """回合开始时登记，保证连接比第一条事件更早到达时也订阅得上。"""
        ...

    def publish(self, turn_id: str, event: ProgressEvent) -> None: ...

    def finish(self, turn_id: str) -> None: ...

    def subscribe(self, turn_id: str) -> AsyncIterator[ProgressEvent | None]: ...


class ToolExecutor(Protocol):
    """把一次工具调用变成一份观察。实现放在 application 层。"""

    def execute(self, call: ToolCall, arguments: Mapping[str, Any]) -> Mapping[str, Any]: ...
