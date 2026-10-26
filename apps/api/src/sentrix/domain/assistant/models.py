"""助手域的通用模型：消息、工具调用、模型回复、进度事件、会话与回合记录。

纯数据，零 IO。这里刻意不认识 HTTP、也不认识 SQLite——模型端点由 infrastructure
适配，domain 只声明它需要什么形状的回复。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class ChatRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class TurnStatus(StrEnum):
    """回合的状态。

    1.0 只有 ``complete`` / ``cancelled`` / ``error`` 三种对外状态，而 ``error`` 只在
    Python 未捕获异常时才出现——模型失败、超时、guard 拦截全部被 ``app.py:3433`` 压成
    ``complete``。前端拿到的每一个失败都是一个「成功」的帧。这里把业务终态显式列出来。
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self not in {TurnStatus.PENDING, TurnStatus.RUNNING}


class TerminationReason(StrEnum):
    """回合为什么结束。**这个枚举里没有 None**。

    1.0 的 25,112 条轨迹里有 11,304 条读不到 ``termination_reason``。根因不是某一处
    忘了赋值，而是两条独立的路：2026-09-10 之前的 ``save_trajectory`` 根本不带这个键
    （11,304 条的 result_json 只有 answer/intent/telemetry 三个键），以及政策拒答那条
    提前 ``return`` 绕过了 ``_classify_termination``（72 条空串）。更糟的是
    ``_classify_termination`` 在收尾处**无条件覆盖**，13 个精心设置的 reason 里 8 个
    是死代码，而没有任何东西会因此报错。

    2.0 的做法：``TurnResult.termination_reason`` 的类型就是这个枚举（不是 ``str``、
    更不是 ``str | None``），漏赋值是类型错误而不是运行时的沉默；并且**没有任何一处
    覆盖它**——每个出口自己声明为什么结束。
    """

    ANSWERED = "answered"
    ANSWERED_WITH_GAP = "answered_with_gap"
    NO_TOOL_CALL_STOPPED = "no_tool_call_stopped"
    TOOL_BUDGET_EXHAUSTED = "tool_budget_exhausted"
    STEP_BUDGET_EXHAUSTED = "step_budget_exhausted"
    TIMEOUT = "timeout"
    MODEL_ERROR = "model_error"
    GUARD_BLOCKED = "guard_blocked"
    CANCELLED = "cancelled"
    # 1.0 ``_classify_termination`` 的 ``parse_failure``：模型这一轮没给出可用的动作/回答。
    # 2.0 曾经把它单列成 ``empty_answer``，但 1.0 的 25,112 条轨迹里没有任何一条以
    # ``complete`` 交付过空答案（1,464 条空答案全部是 status=error/partial 的模型调用失败），
    # 也就是说 1.0 从来没有把「空回答」当成一个独立的结局——它把这类情况归进 parse_failure。
    # 照 1.0 的口径并回来，不再自造一个 1.0 没有的终止原因。
    PARSE_FAILURE = "parse_failure"
    # 1.0 ``_detect_policy_refusal`` 那条出口。1.0 提前 return 绕过了收尾分类器，于是
    # ``termination_reason`` 留成空串（库里 212 条空串正好就是这 212 条 policy_refusal）；
    # 行为不变，只是给它一个名字。
    POLICY_REFUSAL = "policy_refusal"
    # 1.0 ``goal_planner`` 的 ``planner_call_error`` 走到最后就是 ``planner_blocked``。
    PLANNER_BLOCKED = "planner_blocked"


class ProgressStage(StrEnum):
    """进度事件的阶段。取值集与设计 §4.3 一致，沿用 1.0 已验证的那一套。"""

    THINKING = "thinking"
    PLANNING = "planning"
    INSPECTING = "inspecting"
    ACTION = "action"
    TOOL_RESULT = "tool_result"
    RECOVERING = "recovering"
    FINALIZING = "finalizing"
    TERMINAL = "terminal"


class ProgressStatus(StrEnum):
    RUNNING = "running"
    OK = "ok"
    DENIED = "denied"
    PARTIAL = "partial"
    COMPLETE = "complete"
    CANCELLED = "cancelled"


class ToolArgumentError(ValueError):
    """模型给的参数不是合法的 JSON 对象。"""


@dataclass(frozen=True, slots=True)
class ToolCall:
    """模型请求的一次工具调用。

    ``arguments_json`` 保持模型给的原始字符串而不是就地解析：解析失败本身是有信息量的
    事件（1.0 的 ``parse_failure`` 就出自这里），调用方需要知道「模型写了什么」而不是
    只看到一句「参数非法」。
    """

    call_id: str
    name: str
    arguments_json: str = ""

    def parse(self) -> Mapping[str, Any]:
        raw = self.arguments_json.strip() or "{}"
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ToolArgumentError(str(error)) from error
        if not isinstance(parsed, dict):
            raise ToolArgumentError(f"参数必须是 JSON 对象，收到 {type(parsed).__name__}")
        return parsed


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """一条发往模型的消息。

    ``tool_calls`` 与 ``tool_call_id`` 是原生 OpenAI 工具协议的两个方向：assistant
    消息带 ``tool_calls``，随后那条 ``role=tool`` 的消息用 ``tool_call_id`` 指回它。
    1.0 把动作序列化成普通文本、把工具结果塞进 user 消息（``runtime.py:2108`` 的注释
    解释了原因），代价是一整套 JSON 修复器与「输出无法解析」的收尾。
    """

    role: ChatRole
    content: str = ""
    tool_call_id: str = ""
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelReply:
    """端点的一次回复。

    ``content`` 是**已经剥掉思考通道**的正文——用户看的那一份。模型的推理过程不在这
    里面，在 ``thought``：它是真实产出，无声丢掉就等于丢掉了这一回合唯一能解释「模型
    为什么这么答」的东西；但它也不是答案（用户问的是照片，不是模型的独白）。剥在哪、
    为什么不能靠服务端 flag 剥，见 ``infrastructure/assistant/chat_client.py``。
    """

    content: str = ""
    thought: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # Per native tool envelope. Empty tuple means no parser facts were supplied.
    tool_argument_parse_facts: tuple[Mapping[str, Any], ...] = ()
    request_policy: Mapping[str, Any] = field(default_factory=dict)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """一条面向用户的进度事件。

    字段集沿用 1.0（``text`` / ``status`` / ``stage`` / ``step_index`` / ``timestamp``）
    并补上 ``kind`` 与 ``payload``。``step_index`` 在**回合内**从 1 单调递增，跨回合不唯一
    ——1.0 的前端拿它当 key 时必须先限定回合作用域，2.0 把它同时用作 SSE 的 ``id:``
    行，于是 ``Last-Event-ID`` 断线续传天然可用。
    """

    step_index: int
    stage: ProgressStage
    status: ProgressStatus
    text: str
    kind: str = "status"
    timestamp: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Attachment:
    """交付给用户的一份可打开的素材。"""

    handle: str
    asset_id: str
    file_name: str
    # 这份素材是照片还是视频（``MediaType`` 的值）。**此前没有这一格**，于是
    # 「我手上这段素材是什么」在交付链路上不可见——2026-10-15 那轮出现过
    # 模型交付了视频的抽帧、同时如实说「没有找到视频」的矛盾（抽帧本身确实是
    # ``media_type='image'`` 的普通资产，模型没说错，但它无从知道那是视频的帧）。
    # 带上类型至少让交付物自己说清身份，前端与评测也不必再靠猜。
    media_type: str = ""


@dataclass(frozen=True, slots=True)
class Conversation:
    id: str
    scope_id: str
    title: str
    state: str
    created_at: datetime
    updated_at: datetime
    last_message_at: datetime


@dataclass(frozen=True, slots=True)
class Message:
    id: str
    conversation_id: str
    scope_id: str
    turn_id: str
    role: ChatRole
    content: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TurnRecord:
    """一次回合的持久化投影。``termination_reason`` 为空串表示「还没结束」。"""

    id: str
    conversation_id: str
    scope_id: str
    question: str
    status: TurnStatus
    created_at: datetime
    updated_at: datetime
    answer: str = ""
    termination_reason: str = ""
    attachments: tuple[Attachment, ...] = ()
    requirements: tuple[Mapping[str, Any], ...] = ()
    # **检索到的那批候选**（``asset_id``，按名次），不是交付的那批。两者必须分开：
    # 交付是「模型从候选里挑了哪几张」，候选是「检索捞回来了哪些」。合成一个数就分不出
    # 「检索没找到」与「找到了没交付」——而这两种缺陷的修法完全相反。
    #
    # 1.0 的 item 里有 ``retrieved_candidate_media`` / ``evidence_source_media``；
    # 2.0 此前**没有暴露它**，PhotoBench 只能把 ``retrieval_media_refs`` 直接赋成 GT，
    # 于是那个指标的分母是 GT、分子是交付集：394 道零交付的题 recall 一道不差全是 0.0。
    retrieved_candidates: tuple[str, ...] = ()
    # **这一轮到底怎么走的**——模型调用、规划、账本、门禁、预算。
    #
    # 单独一份而不是继续往 progress 事件里塞：``public_progress`` 同时是**终端用户看到的
    # 进度条**（``apps/web`` 的 ``AssistantPanel`` 把每一条都渲染成一行，只用 ``text``），
    # 往里加事件 = 用户每步多看到一行噪音。这一份是**评测读的回合级事实**，与
    # ``retrieved_candidates`` 走同一条路（终态载荷 → 落库 → PhotoBench）。
    #
    # 形状由 ``run_turn`` 组装，读它的是 PhotoBench 的 ``model_call_metrics`` /
    # ``agent2_trace`` / ``debug_trace`` 三处投影。键名不在这里定死——这一格是
    # 「助手知道的一切」的载体，投影是评测侧的事。
    trace: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PersonRelationship:
    """此人是对方的什么。1.0 ``db.py:2855-2859`` 那三个键，一个不多一个不少。

    ``predicate`` 读作「**此人**是 other_name 的 predicate」。所以只收此人为 subject
    的边——方向那件事在 ``EntityPersonDirectory._relationships`` 里细说。
    """

    predicate: str
    other_name: str
    source: str


@dataclass(frozen=True, slots=True)
class PersonProfile:
    """一位已确认人物的档案。

    **2.0 现在给得出的是 1.0 digest 的一个子集。** 1.0 ``db.py:2840-2889`` 返回 12 个键，
    这里对应的是 S7 家庭图谱带来的那几格：``membership`` / ``membership_source`` /
    ``summary``（画像文本）/ ``relationships``。

    还差的那些（``preference_summary_zh`` / ``activity_summary_zh`` / ``place_summary_zh``
    / ``appearance_summary_zh`` / ``patterns`` / ``recent_events`` / ``claims``）来自
    person memory / 语义画像 / claims 表，**属 S5**，2.0 还没有。
    **工具的描述必须与这里一致**：既不承诺拿不到的，也不否认已经拿得到的。

    **``family_role`` 不在这里了。** 1.0 的 digest 根本没有这个键——它是 2.0 在 S3 的
    占位（那时家庭图谱还不存在）。两者也不是一回事：1.0 真实库里 298 对
    「``family_role`` × ``membership``」的配对**一致率 0**，是一条自由文本轴
    与一条三值分类轴。``entities.family_role`` 列本身保留，``GET /api/v1/people/{id}``
    照旧返回它，只有助手工具不再拿它当家庭归属。
    """

    name: str
    confirmed: bool
    membership: str = "unknown"
    membership_source: str = ""
    summary: str = ""
    relationships: tuple[PersonRelationship, ...] = ()
