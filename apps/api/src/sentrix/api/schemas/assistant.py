"""助手端点的请求/响应模型。字段与设计 §4.3 的 SSE 契约对齐。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ConversationSummary(BaseModel):
    id: str
    scope_id: str
    title: str
    created_at: str
    updated_at: str
    last_message_at: str


class ConversationListResponse(BaseModel):
    items: list[ConversationSummary]


class CreateConversationRequest(BaseModel):
    scope_id: str = "home-default"
    title: str = ""


class RenameConversationRequest(BaseModel):
    title: str = Field(min_length=1, max_length=80)


class MessageSummary(BaseModel):
    id: str
    role: str
    content: str
    turn_id: str
    created_at: str


class AttachmentSummary(BaseModel):
    handle: str
    asset_id: str
    file_name: str


class TurnSummary(BaseModel):
    id: str
    question: str
    status: str
    termination_reason: str
    answer: str
    attachments: list[AttachmentSummary]
    created_at: str


class ConversationDetailResponse(BaseModel):
    conversation: ConversationSummary
    messages: list[MessageSummary]
    turns: list[TurnSummary]


class StartTurnRequest(BaseModel):
    """建回合的请求。**没有 scope**：回合的 scope 由它所属的会话决定。

    这里曾经有一个 ``scope_id: str = "home-default"``。它让调用方在不传 scope 时
    把回合落到默认值上，而会话自己的 scope 被完全忽略——检索于是在空库里跑，
    失败还是静默的（表现成「搜索质量差」而不是「配置错了」）。
    """

    # 一条空消息不是「一轮空对话」，是一次调用方错误，所以这里直接挡在 422。
    message: str = Field(min_length=1, max_length=131072)
    conversation_id: str = ""


class StartTurnResponse(BaseModel):
    turn_id: str
    conversation_id: str
    status: str


class ProgressEventPayload(BaseModel):
    """``progress`` 事件的 ``data``。

    字段集沿用 1.0（``runtime.py:1664-1687``），并补上 ``kind`` 与 ``payload``。
    ``step_index`` 同时是 SSE 的 ``id:`` 值，于是 ``Last-Event-ID`` 断线续传天然可用。
    """

    text: str
    status: str
    stage: str
    step_index: int
    timestamp: str
    kind: str = "status"
    payload: dict[str, Any] = Field(default_factory=dict)


class CompleteEventPayload(BaseModel):
    """``complete`` 事件的 ``data``。

    与 1.0 的一处刻意差异：**不再把整条 ``public_progress`` 又塞一遍**。1.0 的 ``complete``
    帧把已经逐条发过的进度事件完整重发（``app.py:2989``），是纯粹的带宽浪费。
    """

    type: str = "complete"
    turn_id: str
    status: str
    termination_reason: str
    answer: str
    attachments: list[AttachmentSummary]
    requirements: list[dict[str, Any]]
    # **检索抓回来的那批候选**（``asset_id``，按名次）。与 ``attachments`` 是两件事：
    # 后者是模型从里面挑了哪几张，这个是检索给了它哪些。
    #
    # 这是一次**有意放宽契约**：2.0 原先不暴露候选集，评测侧只能拿交付集冒充检索集，
    # 于是「检索没找到」与「找到了没交付」在指标上完全一样。``behaviour-baseline.md``
    # 的「2.0 不再暴露的轨迹」一节里 ``retrieved_candidate_media`` 那一行随之改掉。
    retrieved_candidates: list[str] = []
    # **这一轮到底怎么走的**：模型调用（token / 耗时 / 步号）、规划、证据账本、门禁、
    # 预算。与 ``retrieved_candidates`` 同一条路——回合级事实进终态载荷，**不进**进度流。
    #
    # 进度流（``public_progress``）同时是终端用户看到的进度条，``apps/web`` 的
    # ``AssistantPanel`` 把每一条都渲染成一行；往里加事件就是给用户每一步多加一行噪音。
    trace: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Execution facts. model_calls optionally includes status, attempt, duration_ms and "
            "tool_argument_parse_facts (json_object_valid per native tool envelope); "
            "plain-text completion is not a JSON parse attempt."
        ),
    )
    error: str = ""


class TurnPollResponse(BaseModel):
    """``GET /api/v1/assistant/turns/{turn_id}`` 的响应——1.0 ``app.py:2938`` 的 B3.4 契约。

    形状照抄 1.0：``turn_id`` / ``status`` / ``public_progress``，终态时多一个 ``result``
    （就是 SSE 的 ``complete`` 载荷），失败时 ``error`` 里带上原因。

    为什么这条端点在 2.0 必须存在：SSE 是浏览器专用的（``EventSource``），非浏览器调用方
    只能轮询。1.0 的 ``services/photobench`` 就是靠 ``wait_for_assistant_turn`` 每 0.5 秒
    打这条端点（``benchmark_orchestrator.py:881``）把一个回合跑完的——8091 访问日志里
    ``GET /api/assistant/turn/{id}`` 被调了 668,552 次，而 SSE 端点只有 62 次。
    """

    turn_id: str
    status: str
    public_progress: list[ProgressEventPayload]
    result: CompleteEventPayload | None = None
    error: str = ""
