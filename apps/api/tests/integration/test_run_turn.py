"""一次回合的完整行为：真 SQLite、脚本化的模型、打桩的工具。

模型与工具的形状是真的（原生 tool_calls、两份投影的 ToolOutcome），只有「模型说什么」
是脚本化的——那正是被测对象之外的东西。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

import pytest

from sentrix.application.assistant.manage_conversations import ManageConversations
from sentrix.application.assistant.run_turn import (
    STALLED_NUDGE,
    RunTurn,
    TurnAlreadyClaimedError,
    TurnNotFoundError,
    TurnRequest,
)
from sentrix.application.assistant.tools import ToolOutcome
from sentrix.domain.assistant.models import (
    Attachment,
    ChatMessage,
    ChatRole,
    ModelReply,
    ProgressEvent,
    TerminationReason,
    ToolCall,
    TurnStatus,
)
from sentrix.domain.assistant.ports import ModelServiceError, PromptTooLongError
from sentrix.domain.assistant.tools import FINAL_ANSWER_TOOL, TOOL_SPECS, ToolSpec
from sentrix.infrastructure.persistence.repositories.conversations import (
    SqliteConversationRepository,
)
from sentrix.infrastructure.persistence.repositories.turns import SqliteTurnRepository

QUESTION = "给我看看在海边的照片"


# ---- 假件 -------------------------------------------------------------------


class ScriptedModel:
    """按剧本回话的模型。剧本用完还继续要，就返回最后一条。

    规划（``tools`` 为空）走 ``planner_reply``，**不消耗剧本**——剧本描述的是工具循环那
    一段。要单独考察规划时用 ``planner=`` 显式给（值或异常）。
    """

    def __init__(
        self,
        replies: Sequence[ModelReply | Exception],
        *,
        planner: object = None,
        judge: object = None,
    ) -> None:
        self._replies = list(replies)
        self._planner = planner_reply("photo_set", "delivered_photo") if planner is None else planner
        self._judge = judge_reply() if judge is None else judge
        self.calls: list[Sequence[ChatMessage]] = []
        self.planner_calls: list[Sequence[ChatMessage]] = []
        self.judge_calls: list[Sequence[ChatMessage]] = []
        # 主循环每一次拿到的那份工具名单（JIT 剪枝之后每次都可能不同）。
        self.tool_sets: list[list[str]] = []

    def complete(self, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec]) -> ModelReply:
        # **按 system 提示词分流，不按 ``tools`` 空不空。** 原先用 ``if not tools:``
        # 认「这是规划 / L2 评审」，而 JIT 剪枝之后工具循环**也可能合法地拿到空元组**
        # （额度用尽时 ``select_tools`` 返回空集，见 §3.4 的 C/B′）——那时这条假件会把
        # 主循环当成评审，测出来的东西与跑的东西不是一回事。
        system = messages[0].content if messages else ""
        if JUDGE_MARKER in system:
            self.judge_calls.append(list(messages))
            if isinstance(self._judge, Exception):
                raise self._judge
            if isinstance(self._judge, PlannerScript):
                return ModelReply(content=self._judge.next(), finish_reason="stop")
            return self._judge  # type: ignore[return-value]
        if PLANNER_MARKER in system:
            self.planner_calls.append(list(messages))
            if isinstance(self._planner, Exception):
                raise self._planner
            if isinstance(self._planner, PlannerScript):
                return ModelReply(content=self._planner.next(), finish_reason="stop")
            return self._planner  # type: ignore[return-value]

        self.calls.append(list(messages))
        self.tool_sets.append([spec.name for spec in tools])
        assert {spec.name for spec in tools} <= {spec.name for spec in TOOL_SPECS}
        # 剪枝之后每一份描述都该是那一句 ≤40 字的 brief；发全量 description 就是
        # 把 2704 token/转 的固定成本又加回去，而模型照答、不会报错。
        assert all(len(spec.description) <= 40 for spec in tools)
        reply = self._replies.pop(0) if len(self._replies) > 1 else self._replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


class StubToolbox:
    def __init__(self, outcomes: Mapping[str, ToolOutcome] | None = None) -> None:
        self._outcomes = dict(outcomes or {})
        self.calls: list[tuple[str, Mapping[str, object]]] = []

    def execute(
        self,
        name: str,
        arguments: Mapping[str, object],
        *,
        scope_id: str,
        now: datetime,
        question: str = "",
    ) -> ToolOutcome:
        # ``question`` 是 `inspect_photo` 要的（1.0 ``tools.py:2162-2165`` 拿它找裁剪目标）,
        # 与 ``__init__`` 那三件一样：**凡是 ``RunTurn`` 会传给真实 toolbox 的形参，
        # 假件都得收**——少一个就在运行时 TypeError，而它会被吞成 ``model_error``，
        # 看起来像模型的问题。
        self.calls.append((name, dict(arguments)))
        return self._stubbed(name)

    def deliver(self, result_set_id: str, handles: Sequence[str], *, scope_id: str) -> ToolOutcome:
        # **交付走的是 ``deliver`` 而不是 ``execute``**——它是终止动作 ``final_answer``
        # 上的一个字段，不是一次工具调用。假件必须收这个形参，理由与 ``execute`` 那段
        # 相同：少一个就是运行时 ``TypeError``，而它会被吞成 ``model_error``，
        # 看起来像模型的问题。
        self.calls.append((FINAL_ANSWER_TOOL, {"result_set_id": result_set_id, "handles": list(handles)}))
        return self._stubbed(FINAL_ANSWER_TOOL)

    def _stubbed(self, name: str) -> ToolOutcome:
        outcome = self._outcomes.get(name)
        if outcome is None:
            return ToolOutcome(
                observation={"failure_reason": "not_stubbed"},
                model_visible={"error": "not_stubbed"},
                failure_reason="not_stubbed",
            )
        return outcome


def final(content: str) -> ModelReply:
    return ModelReply(content=content, finish_reason="stop")


def tool_call(name: str, call_id: str, **arguments: object) -> ModelReply:
    return ModelReply(
        content="",
        finish_reason="tool_calls",
        tool_calls=(ToolCall(call_id=call_id, name=name, arguments_json=json.dumps(arguments)),),
    )


def final_call(answer: str, *handles: str, call_id: str = "c_final") -> ModelReply:
    """终止动作：**交付是它上面的一个字段**（1.0 ``runtime.py:309`` 的 ``final`` 动作）。

    与 ``final()``（正文收尾）是两回事：正文里没有那个字段，交付必然为空。
    """
    return tool_call(
        FINAL_ANSWER_TOOL,
        call_id,
        answer=answer,
        selected_image_handles=list(handles),
    )


JUDGE_MARKER = "事实一致性评审"
PLANNER_MARKER = "你正在规划家庭记忆任务"


def judge_reply(*problems: tuple[str, str], faithful: bool | None = None) -> ModelReply:
    """L2 评审的裁决。不给问题就是「忠实」。"""
    verdict = {
        "faithful": faithful if faithful is not None else not problems,
        "problems": [{"type": kind, "detail": detail} for kind, detail in problems],
        "reason": "测试裁决",
    }
    return ModelReply(content=json.dumps(verdict, ensure_ascii=False), finish_reason="stop")


class PlannerScript:
    """规划那一次调用按顺序回话（与主循环的剧本分开）。"""

    def __init__(self, replies: Sequence[str]) -> None:
        self._replies = list(replies)

    def next(self) -> str:
        return self._replies.pop(0) if len(self._replies) > 1 else self._replies[0]


def planner_reply(*evidence_types: str, goal: str = "看海边的照片") -> ModelReply:
    """规划那次模型调用的回复。

    规划与主循环共用同一个模型端口（1.0 也是同一个 ``chat_fn``），测试里靠 ``tools`` 区分：
    规划带空元组，主循环带四个工具。
    """
    declaration = {
        "action": "declare",
        "declaration": {
            "goal": goal,
            "requirements": [
                {"id": f"req_{index + 1}", "evidence_type": item, "description": f"需要 {item}"}
                for index, item in enumerate(evidence_types)
            ],
        },
    }
    return ModelReply(content=json.dumps(declaration, ensure_ascii=False), finish_reason="stop")


SEARCH_OK = ToolOutcome(
    observation={"result_set_id": "rs_1", "query": "海边", "total": 2, "has_more": False},
    model_visible={
        "result_set_id": "rs_1",
        "query": "海边",
        "page": 1,
        "page_size": 6,
        "has_more": False,
        "preview": [
            {"handle": "photo_1", "file_name": "a.jpg", "captured_at": "2017-11-05T16:44:57"},
            {"handle": "photo_2", "file_name": "b.jpg", "captured_at": "2018-08-12T09:10:00"},
        ],
    },
)

#: 第二页预览。压缩测试用它证明**最新的一份六图集**被保留、更老的检索轮被丢掉。
PAGE_OK = ToolOutcome(
    observation={"result_set_id": "rs_1", "page": 2, "has_more": False},
    model_visible={
        "result_set_id": "rs_1",
        "page": 2,
        "page_size": 6,
        "has_more": False,
        "preview": [
            {"handle": "photo_7", "file_name": "g.jpg", "captured_at": "2019-01-01T00:00:00"},
            {"handle": "photo_8", "file_name": "h.jpg", "captured_at": "2019-01-02T00:00:00"},
        ],
    },
)

DELIVER_OK = ToolOutcome(
    observation={"result_set_id": "rs_1", "delivered": 2, "rejected": []},
    model_visible={"result_set_id": "rs_1", "delivered": 2, "rejected": []},
    attachments=(
        Attachment(handle="photo_1", asset_id="asset_1", file_name="a.jpg"),
        Attachment(handle="photo_2", asset_id="asset_2", file_name="b.jpg"),
    ),
)

SEARCH_EMPTY = ToolOutcome(
    observation={"result_set_id": "rs_2", "query": "海边", "total": 0, "has_more": False},
    model_visible={"result_set_id": "rs_2", "query": "海边", "page": 1, "preview": [], "has_more": False},
)


# ---- 夹具 -------------------------------------------------------------------


@pytest.fixture
def env(migrations_ready_db):
    conversations = SqliteConversationRepository(migrations_ready_db)
    turns = SqliteTurnRepository(migrations_ready_db)
    conversation = conversations.create(
        scope_id="album1", title="新对话", created_at=datetime(2026, 9, 22, tzinfo=UTC)
    )
    return conversations, turns, conversation, migrations_ready_db


def manage_of(env) -> ManageConversations:
    conversations, turns, _conversation, _db = env
    return ManageConversations(conversations=conversations, turns=turns)


def build(env, model, toolbox) -> RunTurn:
    conversations, turns, _conversation, _db = env
    return RunTurn(model=model, conversations=conversations, turns=turns, toolbox=toolbox)


def start(run_turn, conversation) -> str:
    return run_turn.start(TurnRequest(conversation_id=conversation.id, question=QUESTION)).id


# ---- 正常链路 ---------------------------------------------------------------


def test_the_full_loop_retrieves_delivers_and_answers(env):
    """交付与作答在**同一步**里完成——这就是 1.0 的 ``final`` 动作形状。

    1.0 的 ``get_original_photos`` 这个独立工具在生产上被调用 0 次，239 道图片题全部走
    final 字段。这里钉住的正是那条通道：检索一步，终止动作一步，没有第三步。
    """
    conversations, turns, conversation, _db = env
    model = ScriptedModel(
        [
            tool_call("search_memories", "c1", query="海边"),
            final_call("这是你要的两张海边照片。", "photo_1", "photo_2"),
        ]
    )
    toolbox = StubToolbox({"search_memories": SEARCH_OK, FINAL_ANSWER_TOOL: DELIVER_OK})
    run_turn = build(env, model, toolbox)
    turn_id = start(run_turn, conversation)

    result = run_turn.run(turn_id)

    assert result.status is TurnStatus.COMPLETE
    assert result.termination_reason is TerminationReason.ANSWERED
    assert result.answer == "这是你要的两张海边照片。"
    assert [item.asset_id for item in result.attachments] == ["asset_1", "asset_2"]
    assert [item["status"] for item in result.requirements] == ["satisfied", "satisfied"]
    assert turns.get(turn_id).status is TurnStatus.COMPLETE
    assert turns.get(turn_id).attachments[0].handle == "photo_1"


def test_the_answer_is_persisted_as_an_assistant_message(env):
    conversations, _turns, conversation, _db = env
    model = ScriptedModel([tool_call("search_memories", "c1", query="海边"), final("给你。")])
    run_turn = build(env, model, StubToolbox({"search_memories": SEARCH_OK}))
    turn_id = start(run_turn, conversation)

    run_turn.run(turn_id)

    messages = conversations.list_messages(conversation.id, 10)
    assert [(message.role, message.content) for message in messages] == [
        (ChatRole.USER, QUESTION),
        (ChatRole.ASSISTANT, "给你。"),
    ]


def test_progress_events_are_persisted_in_order(env):
    _conversations, turns, conversation, _db = env
    model = ScriptedModel(
        [
            tool_call("search_memories", "c1", query="海边"),
            final_call("给你。", "photo_1"),
        ]
    )
    toolbox = StubToolbox(
        {
            "search_memories": SEARCH_OK,
            FINAL_ANSWER_TOOL: ToolOutcome(
                observation={"result_set_id": "rs_1", "delivered": 1, "rejected": []},
                model_visible={"result_set_id": "rs_1", "delivered": 1, "rejected": []},
                attachments=(Attachment(handle="photo_1", asset_id="asset_1", file_name="a.jpg"),),
            ),
        }
    )
    run_turn = build(env, model, toolbox)
    turn_id = start(run_turn, conversation)

    result = run_turn.run(turn_id)

    events = turns.events_after(turn_id, 0)
    assert [event.step_index for event in events] == list(range(1, len(events) + 1))
    assert events[0].kind == "plan"
    assert [event.payload["requirements"][0]["status"] for event in events[:1]] == ["open"]
    assert events[-1].kind == "terminal"
    # 事件里记的原因与返回值必须是同一个——1.0 的收尾分类器会覆盖前面设好的原因，
    # 于是轨迹里记的和代码里写的经常不是一回事。
    assert events[-1].payload["termination_reason"] == result.termination_reason.value == "answered"


def test_the_emitted_events_match_what_was_persisted(env):
    """``emit`` 的回调拿到的就是落库的那一份——SSE 与历史回放不该有两个真相。"""
    _conversations, turns, conversation, _db = env
    model = ScriptedModel([tool_call("search_memories", "c1", query="海边"), final("给你。")])
    run_turn = build(env, model, StubToolbox({"search_memories": SEARCH_OK}))
    turn_id = start(run_turn, conversation)
    emitted: list[ProgressEvent] = []

    run_turn.run(turn_id, emit=emitted.append)

    persisted = turns.events_after(turn_id, 0)
    assert [(event.step_index, event.text) for event in emitted] == [
        (event.step_index, event.text) for event in persisted
    ]


def test_history_is_replayed_to_the_model_without_duplicating_the_question(env):
    _conversations, turns, conversation, _db = env
    model = ScriptedModel([tool_call("search_memories", "c1", query="海边"), final("第一轮。")])
    run_turn = build(env, model, StubToolbox({"search_memories": SEARCH_OK}))
    run_turn.run(start(run_turn, conversation))

    second = ScriptedModel([final("第二轮。")])
    run_turn_2 = build(env, second, StubToolbox())
    run_turn_2.run(start(run_turn_2, conversation))

    sent = second.calls[0]
    assert sent[0].role is ChatRole.SYSTEM
    assert [message.role for message in sent] == [
        ChatRole.SYSTEM,
        ChatRole.USER,
        ChatRole.ASSISTANT,
        ChatRole.USER,
    ]
    assert sent[1].content == QUESTION
    assert sent[2].content == "第一轮。"
    assert sent[3].content == QUESTION


# ---- 门禁 -------------------------------------------------------------------


def test_answering_without_delivering_is_answered_with_a_gap(env):
    """用户要图、模型只回文字时，**1.0 的门禁不会拦**——这一轮如实标成带缺口收尾。

    S3 曾经在这里加过一条「证据门」（只要还有开着的需求就拦）。1.0 没有这条规则，
    它最接近的是 ``delivery_contradiction``：要「真的调过交付工具、且它返回 0 张」加上
    「最后一次检索把结果取完」两个条件同时成立。这里钉住的就是 1.0 的实际行为——
    只检索不交付，是 ``ANSWERED_WITH_GAP``，不是被拦下来重试。

    拦住「用户要图却一张都没交付」这件事在 S3 由别的东西负责：规划把
    ``delivered_photo`` 列成一条证据需求（``intent.plan_requirements``），收尾时账本上
    它还开着，于是 ``answered_with_gap`` / ``answered_with_gap`` 的终态与
    ``requirements[].status == "open"`` 如实反映出来——不假装这一轮是完整交付。
    """
    _conversations, _turns, conversation, _db = env
    model = ScriptedModel(
        [
            tool_call("search_memories", "c1", query="海边"),
            final("我找到了两张在海边的照片。"),
        ]
    )
    run_turn = build(env, model, StubToolbox({"search_memories": SEARCH_OK}))
    turn_id = start(run_turn, conversation)

    result = run_turn.run(turn_id)

    assert result.answer == "我找到了两张在海边的照片。"
    assert result.termination_reason is TerminationReason.ANSWERED_WITH_GAP
    assert result.status is TurnStatus.PARTIAL
    assert result.attachments == ()
    assert [item["status"] for item in result.requirements] == ["satisfied", "open"]


def test_a_delivery_that_delivers_nothing_ends_with_a_gap(env):
    """交付一张都没交出去时，1.0 的门禁**不会**拦——缺口由账本如实标出来。

    这条测试同时说明一件事：1.0 的 ``delivery_contradiction`` 在 2.0 的形状下够不着。
    它要求 ``delivered_count`` 是**整数 0**（真的交付过、且它返回 0 张），而 2.0 的
    ``deliver`` 在交不出去时返回的是失败（``bad_handle``），记录的 ``status`` 是
    ``error``，``GuardFacts.from_state`` 会跳过它、``delivered_count`` 保持 ``None``。
    规则照搬了，触发它的输入还没出现——与 ``all_requested_but_has_more`` 缺
    ``result_mode`` 是同一类情况。
    """
    _conversations, _turns, conversation, _db = env
    empty_delivery = ToolOutcome(
        observation={"failure_reason": "bad_handle"},
        model_visible={"error": "bad_handle", "message": "这些 handle 都不在当前结果集里。"},
        failure_reason="bad_handle",
    )
    model = ScriptedModel(
        [
            tool_call("search_memories", "c1", query="海边"),
            # 终止动作带着一个不在结果集里的 handle 收尾：交付失败，但**不打断作答**
            # ——1.0 的 ``_normalize_selected_image_handles`` 也是把认不出来的 handle
            # 直接丢掉，从不因此拒绝那个 final。
            final_call("我找到了两张海边的照片，但没能把原图交出来。", "photo_9"),
        ]
    )
    toolbox = StubToolbox({"search_memories": SEARCH_OK, FINAL_ANSWER_TOOL: empty_delivery})
    run_turn = build(env, model, toolbox)
    turn_id = start(run_turn, conversation)

    result = run_turn.run(turn_id)

    assert len(model.calls) == 2
    assert result.answer == "我找到了两张海边的照片，但没能把原图交出来。"
    assert result.attachments == ()
    assert result.status is TurnStatus.PARTIAL
    assert result.termination_reason is TerminationReason.ANSWERED_WITH_GAP
    assert [item["status"] for item in result.requirements] == ["satisfied", "open"]


def test_an_empty_search_cannot_be_reported_as_a_hit(env):
    _conversations, _turns, conversation, _db = env
    model = ScriptedModel(
        [
            tool_call("search_memories", "c1", query="海边"),
            final("我找到了这些海边的照片。"),
            final("这次没有匹配到海边的照片，可能没有拍到过。"),
        ]
    )
    run_turn = build(env, model, StubToolbox({"search_memories": SEARCH_EMPTY}))
    turn_id = start(run_turn, conversation)

    result = run_turn.run(turn_id)

    assert result.answer == "这次没有匹配到海边的照片，可能没有拍到过。"


#: 模型偶尔吐出来的正文工具调用（Llama 3.x 格式，8100 的 Qwen 模板解析不了）。
#: 2026-10-19 那轮 487 题里原样出现在 18 条答案里。
TEXTUAL_TOOL_CALL = (
    "<tool_call>\n"
    "<function=read_photo_text>\n"
    "<parameter=result_set_id>\n"
    "rs_441092e2ef66\n"
    "</parameter>\n"
    "<parameter=asset_handle>\n"
    "photo_4\n"
    "</parameter>\n"
    "</function>\n"
    "</tool_call>"
)


def test_a_textual_tool_call_is_never_delivered(env):
    """正文里的 ``<tool_call>`` 不是答案，纠正一次后应当重新作答。

    实测那 18 条的 ``termination_reason`` 全是 ``answered``——模型这一轮**没有**
    结构化 ``tool_calls``，整段 XML 落在 ``content`` 里，被当成最终答案交给了用户。
    """
    _conversations, _turns, conversation, _db = env
    model = ScriptedModel(
        [
            tool_call("search_memories", "c1", query="海边"),
            ModelReply(content=TEXTUAL_TOOL_CALL, finish_reason="stop"),
            final_call("就是穿红衣服那张。", "photo_1"),
        ]
    )
    toolbox = StubToolbox({"search_memories": SEARCH_OK, FINAL_ANSWER_TOOL: DELIVER_OK})
    run_turn = build(env, model, toolbox)
    turn_id = start(run_turn, conversation)

    result = run_turn.run(turn_id)

    assert "<tool_call>" not in result.answer
    assert "photo_4" not in result.answer
    assert result.answer == "就是穿红衣服那张。"


def test_a_second_textual_tool_call_ends_as_parse_failure(env):
    """纠正一次仍写 XML，就按 ``PARSE_FAILURE`` 收尾——不能把 XML 端出去，也不能无限重试。"""
    _conversations, _turns, conversation, _db = env
    model = ScriptedModel(
        [
            tool_call("search_memories", "c1", query="海边"),
            ModelReply(content=TEXTUAL_TOOL_CALL, finish_reason="stop"),
            ModelReply(content=TEXTUAL_TOOL_CALL, finish_reason="stop"),
        ]
    )
    run_turn = build(env, model, StubToolbox({"search_memories": SEARCH_OK}))
    turn_id = start(run_turn, conversation)

    result = run_turn.run(turn_id)

    assert result.termination_reason is TerminationReason.PARSE_FAILURE
    assert "<tool_call>" not in result.answer


def test_a_leaked_internal_identifier_is_never_delivered(env):
    """安全边界不做第二次让步：两次都泄漏就给确定性兜底，不把内部标识交给用户。"""
    _conversations, _turns, conversation, _db = env
    leak = "这张是 photo_1，asset_9f3a21b7c4。"
    model = ScriptedModel([tool_call("search_memories", "c1", query="海边"), final(leak), final(leak)])
    run_turn = build(env, model, StubToolbox({"search_memories": SEARCH_OK}))
    turn_id = start(run_turn, conversation)

    result = run_turn.run(turn_id)

    assert result.status is TurnStatus.BLOCKED
    assert result.termination_reason is TerminationReason.GUARD_BLOCKED
    assert "photo_1" not in result.answer


def test_a_hard_block_is_visible_in_the_trace(env):
    """硬拦也要留一条 guard 事件——在此之前它一条都不发。

    truth 级的门禁有 ``kind="guard"`` 的事件，硬拦分支没有；于是被硬拦的回合在轨迹里
    既看不到触发了哪条规则，也看不到被打回的那段原文。实测那两道 ``guard_blocked``
    因此完全不可诊断。**用户看的文案里不该出现规则码，但轨迹里必须有。**
    """
    _conversations, turns, conversation, _db = env
    leak = "这张是 photo_1，asset_9f3a21b7c4。"
    model = ScriptedModel([tool_call("search_memories", "c1", query="海边"), final(leak), final(leak)])
    run_turn = build(env, model, StubToolbox({"search_memories": SEARCH_OK}))
    turn_id = start(run_turn, conversation)

    run_turn.run(turn_id)

    hard = [event for event in turns.events_after(turn_id, 0) if (event.payload or {}).get("hard")]
    assert hard, "硬拦必须留下一条可归因的事件"
    assert hard[0].payload["code"] == "internal_id_leak"
    assert hard[0].kind == "guard"


# ---- 收尾前的三条一次性提醒（1.0 runtime.py:2540-2660）------------------------


def reminder_count(model: ScriptedModel, needle: str) -> int:
    """这条提醒**在一次调用里最多出现几次**。

    **不跨调用求和**：提醒一旦进了对话历史，之后每一次调用都会带着它，求和会把一次
    提醒数成五次——本项目的门禁踩过这个坑（``test_the_stall_reminder_is_given_only_once``
    的注释记着）。
    """
    return max(
        (sum(1 for message in call if needle in message.content) for call in model.calls),
        default=0,
    )


def test_a_declared_but_unused_structured_fact_earns_one_reminder(env):
    """声明了 ``structured_fact`` 却一次都没查过精确统计 → 提醒一次（①）。

    模型在这个形态下最容易拿 ``search_memories`` 的候选数当答案报出去——1.0
    ``runtime.py:116`` 就是为这条才把 ``total`` 从模型可见的观察里剥掉的。
    """
    _conversations, _turns, conversation, _db = env
    model = ScriptedModel(
        [
            tool_call("search_memories", "c1", query="海边"),
            final_call("一共有 12 张海边的照片。", "photo_1"),
        ],
        planner=planner_reply("photo_set", "structured_fact"),
    )
    run_turn = build(env, model, StubToolbox({"search_memories": SEARCH_OK}))
    turn_id = start(run_turn, conversation)

    run_turn.run(turn_id)

    assert reminder_count(model, "该问题声明了全量结构化统计需求") == 1


def test_an_unattempted_requirement_earns_one_reminder(env):
    """声明了 ``visible_text`` 却从没调用过 ``read_photo_text`` → 提醒一次（②）。

    实测这一类有 155 道，其中 **147 道（95%）从头到尾没调用过 ``read_photo_text``**
    ——不是「取了没取到」，是**根本没去取**。提醒里要写出**当前可用工具**，否则模型
    只知道「还缺东西」，不知道拿什么去补。
    """
    _conversations, _turns, conversation, _db = env
    model = ScriptedModel(
        [
            tool_call("search_memories", "c1", query="海边"),
            final_call("海边有两张。", "photo_1"),
        ],
        planner=planner_reply("photo_set", "visible_text"),
    )
    run_turn = build(env, model, StubToolbox({"search_memories": SEARCH_OK}))
    turn_id = start(run_turn, conversation)

    run_turn.run(turn_id)

    assert reminder_count(model, "还有声明的证据需求尚未尝试：") == 1
    nudged = [call for call in model.calls if any("尚未尝试" in m.content for m in call)]
    assert any("read_photo_text" in message.content for call in nudged for message in call)
    assert any("visible_text" in message.content for call in nudged for message in call)


def test_numbers_without_any_tool_call_earn_one_reminder(env):
    """零工具调用却报出具体数字 → 提醒一次（③）。

    这条与账本无关：需求为空（模型认为无需证据）**不**等于可以凭空编数字。
    """
    _conversations, _turns, conversation, _db = env
    model = ScriptedModel([final("相册里一共 12 张照片。")])
    run_turn = build(env, model, StubToolbox())
    turn_id = start(run_turn, conversation)

    run_turn.run(turn_id)

    assert reminder_count(model, "你的回答给出了具体的数字") == 1


def test_a_chat_question_with_numbers_is_not_nagged(env):
    """反向：闲聊 / 让模型写东西本来就不该去查相册，不许提醒（1.0 ``_CHAT_ONLY_RE``）。"""
    _conversations, _turns, conversation, _db = env
    model = ScriptedModel([final("好的，帮你写 3 句话的祝福。")], planner=planner_reply())
    run_turn = build(env, model, StubToolbox())
    turn_id = start(run_turn, conversation)

    run_turn.run(turn_id)

    assert reminder_count(model, "你的回答给出了具体的数字") == 0


def test_the_reminder_lets_the_model_insist(env):
    """提醒之后模型坚持同样的 final → **放行，代码不覆盖**（1.0 的口径）。

    1.0 在那里的处置是 ``decision = partial_answer`` / ``accept_with_insufficient_evidence``；
    2.0 现在就是这个行为。若提醒后把答案换掉，「模型如实说无法确认」这句话永远出不去。
    """
    _conversations, _turns, conversation, _db = env
    model = ScriptedModel(
        [
            tool_call("search_memories", "c1", query="海边"),
            final_call("海边有两张，但照片里的字读不到。", "photo_1"),
        ],
        planner=planner_reply("photo_set", "visible_text"),
    )
    run_turn = build(env, model, StubToolbox({"search_memories": SEARCH_OK}))
    turn_id = start(run_turn, conversation)

    result = run_turn.run(turn_id)

    assert result.answer == "海边有两张，但照片里的字读不到。"
    assert result.status is TurnStatus.PARTIAL


# ---- 需求的收尾记账：分清「没去取」与「取了没取到」 --------------------------


def test_an_unattempted_requirement_says_so(env):
    """声明了却**从头到尾没去取**的需求，收尾必须写 ``not_attempted``，不是空串。

    1.0 与 2.0 此前都留空串，于是结果里分不出两种缺陷。实测 ``visible_text`` 有 155 道
    开着，其中 **147 道（95%）没调用过 ``read_photo_text``**——那不是检索失败，是
    **规划与执行脱节**，与「取了没取到」的修法完全不同。
    """
    _conversations, _turns, conversation, _db = env
    model = ScriptedModel([tool_call("search_memories", "c1", query="海边"), final("找到了。")])
    run_turn = build(env, model, StubToolbox({"search_memories": SEARCH_OK}))
    turn_id = start(run_turn, conversation)

    result = run_turn.run(turn_id)

    by_type = {item["evidence_type"]: item for item in result.requirements}
    assert by_type["photo_set"]["status"] == "satisfied"
    assert by_type["delivered_photo"]["status"] == "open"
    assert by_type["delivered_photo"]["unmet_reason"] == "not_attempted"


def test_an_attempted_but_unmet_requirement_says_attempt_failed(env):
    """**取过但没取到**是另一回事，写 ``attempt_failed``。

    两条路径必须给出不同的词，否则「规划脱节」会被读成「工具不行」。
    """
    _conversations, _turns, conversation, _db = env
    no_text = ToolOutcome(
        observation={"failure_reason": "no_text_detected"},
        model_visible={"error": "no_text_detected"},
        failure_reason="no_text_detected",
    )
    model = ScriptedModel(
        [
            tool_call("search_memories", "c1", query="海边"),
            tool_call("read_photo_text", "c2", result_set_id="rs_1", asset_handle="photo_1"),
            final("照片里读不到文字。"),
        ],
        planner=planner_reply("photo_set", "visible_text"),
    )
    toolbox = StubToolbox({"search_memories": SEARCH_OK, "read_photo_text": no_text})
    run_turn = build(env, model, toolbox)
    turn_id = start(run_turn, conversation)

    result = run_turn.run(turn_id)

    by_type = {item["evidence_type"]: item for item in result.requirements}
    assert by_type["visible_text"]["status"] == "open"
    assert by_type["visible_text"]["unmet_reason"] == "attempt_failed"


# ---- 收敛与预算 --------------------------------------------------------------


def test_a_second_search_is_refused_and_the_model_is_told_to_page(env):
    _conversations, turns, conversation, _db = env
    model = ScriptedModel(
        [
            tool_call("search_memories", "c1", query="海边"),
            tool_call("search_memories", "c2", query="海边2"),
            final("这是你要的照片。"),
        ]
    )
    toolbox = StubToolbox({"search_memories": SEARCH_OK})
    run_turn = build(env, model, toolbox)
    turn_id = start(run_turn, conversation)

    run_turn.run(turn_id)

    assert [name for name, _ in toolbox.calls] == ["search_memories"]
    refusals = [event for event in turns.events_after(turn_id, 0) if event.status.value == "denied"]
    assert refusals and "get_result_page" in refusals[0].text


def test_a_model_stuck_on_failing_tools_is_told_to_answer(env):
    """连续几轮一条有用的结果都没拿到，主循环要给一次「收口」的提醒。

    **为什么护栏必须在循环这一层**：被拒的调用不推进任务、不占工具额度，**只烧模型步**
    （循环是「问一次模型 → 跑它要的工具 → 再问一次」，被拒也要走完这一圈）。工具层的
    每一条判据都拦不住它，因为**每一次被拒在它自己看来都是对的**——这是活性问题。

    实测 762 次被拒里有 51% 之后模型继续试同一个工具，426 步是「整步全被拒」，
    占全部 2667 步的 16%。
    """
    _conversations, _turns, conversation, _db = env
    failing = ToolOutcome(
        observation={"failure_reason": "file_unavailable"},
        model_visible={"error": "file_unavailable"},
        failure_reason="file_unavailable",
    )
    model = ScriptedModel(
        [
            tool_call("search_memories", "c1", query="海边"),
            tool_call("inspect_photo", "c2", result_set_id="rs_1", question="是什么"),
            tool_call("inspect_photo", "c3", result_set_id="rs_1", question="是什么"),
            final("只能确认检索到两张海边的照片。"),
        ]
    )
    toolbox = StubToolbox({"search_memories": SEARCH_OK, "inspect_photo": failing})
    run_turn = build(env, model, toolbox)
    turn_id = start(run_turn, conversation)

    result = run_turn.run(turn_id)

    assert result.answer == "只能确认检索到两张海边的照片。"
    nudged = [call for call in model.calls if any(STALLED_NUDGE in message.content for message in call)]
    assert nudged, "连续两轮白跑之后必须给一次提醒"


def test_the_stall_reminder_is_given_only_once(env):
    """提醒**只给一次**（``block_guard_code`` 的既有口径）——真推不动时由 ``can_step()`` 兜底。"""
    _conversations, _turns, conversation, _db = env
    failing = ToolOutcome(
        observation={"failure_reason": "file_unavailable"},
        model_visible={"error": "file_unavailable"},
        failure_reason="file_unavailable",
    )
    model = ScriptedModel(
        [
            tool_call("search_memories", "c1", query="海边"),
            tool_call("inspect_photo", "c2", result_set_id="rs_1", question="是什么"),
            tool_call("inspect_photo", "c3", result_set_id="rs_1", question="是什么"),
            tool_call("inspect_photo", "c4", result_set_id="rs_1", question="是什么"),
            tool_call("inspect_photo", "c5", result_set_id="rs_1", question="是什么"),
        ]
    )
    toolbox = StubToolbox({"search_memories": SEARCH_OK, "inspect_photo": failing})
    run_turn = build(env, model, toolbox)
    turn_id = start(run_turn, conversation)

    result = run_turn.run(turn_id)

    assert result.termination_reason is TerminationReason.STEP_BUDGET_EXHAUSTED
    # 提醒进了对话之后**会一直在**后续每一次模型调用的历史里，所以不能跨调用求和；
    # 要看的是「同一次调用的消息里它只出现一次」——那才说明只加过一次。
    per_call = [sum(1 for message in call if STALLED_NUDGE in message.content) for call in model.calls]
    assert max(per_call) == 1, f"提醒只能被加进对话一次，实际每轮出现 {per_call}"


def test_a_repeated_identical_call_is_replayed_not_re_executed(env):
    _conversations, _turns, conversation, _db = env
    model = ScriptedModel(
        [
            tool_call("get_result_page", "c1", result_set_id="rs_1", page=1),
            tool_call("get_result_page", "c2", result_set_id="rs_1", page=1),
            final("这是你要的照片。"),
        ]
    )
    toolbox = StubToolbox({"get_result_page": SEARCH_OK})
    run_turn = build(env, model, toolbox)
    turn_id = start(run_turn, conversation)

    run_turn.run(turn_id)

    assert len(toolbox.calls) == 1


def test_the_model_step_budget_ends_the_turn_with_a_reason(env):
    _conversations, _turns, conversation, _db = env
    model = ScriptedModel([tool_call("get_result_page", "c1", result_set_id="rs_1", page=1)])
    run_turn = build(env, model, StubToolbox({"get_result_page": SEARCH_OK}))
    turn_id = start(run_turn, conversation)

    result = run_turn.run(turn_id)

    assert result.termination_reason is TerminationReason.STEP_BUDGET_EXHAUSTED
    assert result.status is TurnStatus.PARTIAL
    assert "结束" in result.answer


def test_the_tools_are_withdrawn_when_the_budget_is_gone(env):
    """工具额度用尽之后，那一转**一个工具都不发**（连 ``final_answer`` 也不发）。（§3.4 C/B′）

    模型物理上无法再调，只剩「作答」这一条路——于是 8 步里最后那 1–2 步会被用来
    **说话**而不是撞墙。实测 ``step_budget_exhausted`` 的 123 道里 **102 道判 0、
    2 分一道没有**，根子就是模型到最后都没被逼着开口。

    断言的是 ``complete`` **收到的那个 tools 参数**，不是最终结果——后者会被
    ``_stop`` 的兜底文案掩盖成「看起来答了」。
    """
    _conversations, _turns, conversation, _db = env
    model = ScriptedModel([tool_call("get_result_page", "c1", result_set_id="rs_1", page=1)])
    run_turn = build(env, model, StubToolbox({"get_result_page": SEARCH_OK}))
    turn_id = start(run_turn, conversation)

    run_turn.run(turn_id)

    offered_before = (
        model.tool_sets[: model.tool_sets.index([])] if [] in model.tool_sets else model.tool_sets
    )
    assert offered_before and all(offered_before), "额度还在时不该是空集"
    assert model.tool_sets[-1] == [], "额度用尽之后必须把工具全部收回"
    assert FINAL_ANSWER_TOOL not in model.tool_sets[-1]


def assert_tool_messages_are_paired(messages: Sequence[ChatMessage]) -> None:
    """每个 ``tool_call_id`` 都有配对结果，没有孤儿 ``tool`` 消息。

    严格 chat template 的硬要求；违反它端点直接 400。压缩历史最容易踩的就是这条。
    """
    index = 0
    while index < len(messages):
        message = messages[index]
        if message.role is ChatRole.ASSISTANT and message.tool_calls:
            expected = {call.call_id for call in message.tool_calls}
            index += 1
            while index < len(messages) and messages[index].role is ChatRole.TOOL:
                assert messages[index].tool_call_id in expected, f"对不上的 tool 结果：{message}"
                expected.discard(messages[index].tool_call_id)
                index += 1
            assert not expected, f"这些 tool_call 没有配对结果：{expected}"
            continue
        assert message.role is not ChatRole.TOOL, f"孤立的 tool 结果：{message.tool_call_id}"
        index += 1


def test_compaction_drops_oldest_round_but_keeps_latest_preview(env):
    """压缩丢**整轮**，但**最新一次检索/翻页的六图 preview 必须留下**。

    丢一半会留下没有配对结果的 ``tool_calls``，端点直接 400。丢最新那份六图集则更糟：
    模型知道「有结果集」却看不见任何候选，整道题只能猜。所以这里先 search 再 page，
    压缩时丢更老的 search 轮，保留 page 轮。
    """
    _conversations, _turns, conversation, _db = env
    model = ScriptedModel(
        [
            tool_call("search_memories", "c1", query="海边"),
            tool_call("get_result_page", "c2", result_set_id="rs_1", page=2),
            PromptTooLongError("提示词约 9000 token，上下文上限 6144…"),
            final_call("海边有两张。", "photo_1"),
        ]
    )
    run_turn = build(
        env,
        model,
        StubToolbox({"search_memories": SEARCH_OK, "get_result_page": PAGE_OK}),
    )
    turn_id = start(run_turn, conversation)

    result = run_turn.run(turn_id)

    assert result.answer == "海边有两张。"
    assert len(model.calls) == 4, "第四次是压缩之后的那次重试"
    # 压缩前：两轮工具结果都在。
    assert sum(1 for m in model.calls[2] if m.role is ChatRole.TOOL) == 2
    # 压缩后：只剩最新的 page 轮（六图集），更老的 search 轮被丢。
    remaining = [m for m in model.calls[3] if m.role is ChatRole.TOOL]
    assert len(remaining) == 1
    assert '"page": 2' in remaining[0].content
    assert '"query": "海边"' not in remaining[0].content
    assert_tool_messages_are_paired(model.calls[3])


def test_a_prompt_that_still_does_not_fit_ends_the_turn_honestly(env):
    """只剩受保护的六图集时**不再往下丢**，如实收尾。

    再压就只能删掉六图集本身——那会让模型看不见任何候选，继续跑也只是猜。宁可
    以 ``MODEL_ERROR`` 收尾，也不产出无依据的答案。
    """
    _conversations, _turns, conversation, _db = env
    model = ScriptedModel(
        [
            tool_call("search_memories", "c1", query="海边"),
            PromptTooLongError("提示词约 9000 token，上下文上限 6144…"),
        ]
    )
    run_turn = build(env, model, StubToolbox({"search_memories": SEARCH_OK}))
    turn_id = start(run_turn, conversation)

    result = run_turn.run(turn_id)

    assert result.status is TurnStatus.FAILED
    assert result.termination_reason is TerminationReason.MODEL_ERROR
    assert result.answer


def test_a_model_failure_ends_the_turn_as_failed_not_complete(env):
    """1.0 把模型失败压成 ``status="complete"``，前端拿到的是一个「成功」的帧。"""
    _conversations, _turns, conversation, _db = env
    model = ScriptedModel([RuntimeError("端点挂了")])
    run_turn = build(env, model, StubToolbox())
    turn_id = start(run_turn, conversation)

    result = run_turn.run(turn_id)

    assert result.status is TurnStatus.FAILED
    assert result.termination_reason is TerminationReason.MODEL_ERROR
    assert "模型服务不可用" in result.answer


def test_a_tool_loop_model_service_failure_carries_the_endpoint(env):
    """工具循环里的服务侧失败，与规划那条同一条口径（用户 2026-09-25 裁决二）。

    8100 缺 tool-call flag 时回的就是这个 400——根因写在它的正文里，必须走到用户眼前，
    而不是只说一句「模型不可用」把它留在日志里。llama.cpp 原生支持 tools，这条差异在换
    后端之前不存在（见 ``docs/refactor/s5-deviations.md`` §2n）。
    """
    _conversations, _turns, conversation, _db = env
    model = ScriptedModel(
        [
            ModelServiceError(
                "模型端点 http://127.0.0.1:8100/v1 上的 gemma4-12b-it 返回 400："
                r'{"error":{"message":"\"auto\" tool choice requires --enable-auto-tool-choice'
                r' and --tool-call-parser to be set"}}'
            )
        ]
    )
    run_turn = build(env, model, StubToolbox())
    turn_id = start(run_turn, conversation)

    result = run_turn.run(turn_id)

    assert result.status is TurnStatus.FAILED
    assert result.termination_reason is TerminationReason.MODEL_ERROR
    assert "模型服务不可用" in result.answer
    assert "http://127.0.0.1:8100/v1" in result.answer
    assert "--tool-call-parser" in result.answer


def test_an_internal_failure_does_not_leak_its_wording_into_the_answer(env):
    """反向门禁：不是模型服务这一侧的异常，内部形状不许说给用户听。

    与 ``goal_planner`` 给 ``detail`` 设的边界是同一条：那边也只收 ``ModelServiceError``。
    """
    _conversations, _turns, conversation, _db = env
    model = ScriptedModel([RuntimeError("内部栈里的字眼")])
    run_turn = build(env, model, StubToolbox())
    turn_id = start(run_turn, conversation)

    result = run_turn.run(turn_id)

    assert "模型服务不可用" in result.answer
    assert "内部栈里的字眼" not in result.answer


def test_an_unexpected_exception_still_produces_a_named_reason(env):
    """兜底那层 ``finally`` 的用处：将来的新出口忘了声明原因时，也不会留下半开的回合。

    反向验证：这里让工具箱抛出一个不在预期内的异常，断言收尾原因仍然是枚举里的一个值。
    """

    class Exploding(StubToolbox):
        def execute(self, name, arguments, *, scope_id, now):  # type: ignore[override]
            raise ValueError("工具内部炸了")

    _conversations, turns, conversation, _db = env
    model = ScriptedModel([tool_call("search_memories", "c1", query="海边"), final("好的。")])
    run_turn = build(env, model, Exploding())
    turn_id = start(run_turn, conversation)

    result = run_turn.run(turn_id)

    assert result.termination_reason is TerminationReason.MODEL_ERROR
    assert turns.get(turn_id).status is TurnStatus.FAILED


# ---- 回合的边界 --------------------------------------------------------------


def test_a_turn_cannot_be_started_twice(env):
    _conversations, _turns, conversation, _db = env
    run_turn = build(env, ScriptedModel([final("好。")]), StubToolbox())
    turn_id = start(run_turn, conversation)
    run_turn.run(turn_id)

    with pytest.raises(TurnAlreadyClaimedError):
        run_turn.run(turn_id)


def test_starting_a_turn_on_a_missing_conversation_fails(env):
    run_turn = build(env, ScriptedModel([final("好。")]), StubToolbox())

    with pytest.raises(TurnNotFoundError):
        run_turn.start(TurnRequest(conversation_id="conv_nope", question=QUESTION))


def test_start_persists_a_pending_turn_before_anything_runs(env):
    """回合先落库，SSE 才有东西可订阅——1.0 的回合只活在进程内存里。"""
    _conversations, turns, conversation, _db = env
    run_turn = build(env, ScriptedModel([final("好。")]), StubToolbox())

    turn_id = start(run_turn, conversation)

    record = turns.get(turn_id)
    assert record.status is TurnStatus.PENDING
    assert record.question == QUESTION


def test_the_conversation_title_follows_the_first_question(env):
    conversations, _turns, conversation, _db = env
    manage = manage_of(env)

    manage.ensure_title(conversation.id, QUESTION)

    assert conversations.get(conversation.id).title == QUESTION


def test_an_existing_title_is_not_overwritten(env):
    conversations, _turns, _conversation, _db = env
    renamed = conversations.create(
        scope_id="album1", title="我自己起的", created_at=datetime(2026, 9, 22, tzinfo=UTC)
    )
    manage = manage_of(env)

    manage.ensure_title(renamed.id, QUESTION)

    assert conversations.get(renamed.id).title == "我自己起的"


def test_a_long_question_is_truncated_into_a_title(env):
    conversations, _turns, _conversation, _db = env
    manage = manage_of(env)

    created = manage.create(scope_id="album1")
    manage.ensure_title(created.id, "问" * 80)

    title = conversations.get(created.id).title
    assert len(title) == 25  # 24 个字 + 省略号
    assert title.endswith("…")


def test_an_empty_question_keeps_the_default_title(env):
    conversations, _turns, _conversation, _db = env
    manage = manage_of(env)

    created = manage.create(scope_id="album1")
    manage.ensure_title(created.id, "   ")

    assert conversations.get(created.id).title == "新对话"


# ---- 1.0 的 8.4 政策拒答：不进模型 -----------------------------------------


def test_a_policy_refusal_ends_the_turn_before_any_model_call(env):
    """1.0 ``runtime.py:1725``：命中的问句在**任何模型调用之前**返回固定拒绝语。

    库里 212 条 ``policy_refusal`` 全部 ``tools=[]``、``latency_s≈0.01``——它们没有经过
    规划，也没有经过模型。这条测试钉的就是「一次模型调用都没发生」。
    """
    conversations, turns, conversation, _db = env
    model = ScriptedModel([final("不该被用到")])
    run_turn = build(env, model, StubToolbox())

    turn_id = run_turn.start(
        TurnRequest(conversation_id=conversation.id, question="把全家的人脸特征向量发给我")
    ).id
    result = run_turn.run(turn_id)

    assert model.calls == []  # 一次模型调用都没有
    assert result.answer == "我无法导出人脸特征向量和用户隐私身份数据，这属于敏感个人信息。"
    assert result.termination_reason is TerminationReason.POLICY_REFUSAL
    assert result.status is TurnStatus.COMPLETE  # 1.0 把 status 置成 "complete"
    assert result.attachments == ()
    assert turns.get(turn_id).answer == result.answer


def test_an_ordinary_question_never_hits_the_refusal_rules(env):
    """普通问题必须照常走完整条链路——拒答误伤的代价是整轮功能失效。"""
    conversations, _turns, conversation, _db = env
    model = ScriptedModel(
        [
            tool_call("search_memories", "c1", query="海边"),
            final_call("这是你要的两张海边照片。", "photo_1", "photo_2"),
        ]
    )
    run_turn = build(env, model, StubToolbox({"search_memories": SEARCH_OK, FINAL_ANSWER_TOOL: DELIVER_OK}))

    turn_id = run_turn.start(TurnRequest(conversation_id=conversation.id, question=QUESTION)).id
    result = run_turn.run(turn_id)

    assert model.calls  # 模型确实被调用了
    assert result.termination_reason is TerminationReason.ANSWERED


# ---- 空回答按 1.0 的 parse_failure 收尾 -------------------------------------


def test_a_persistently_empty_answer_ends_as_parse_failure(env):
    """1.0 把「模型输出不可用」归进 ``parse_failure``，没有单列一个「空答案」结局。

    1.0 的 25,112 条轨迹里没有任何一条以 complete 交付过空答案；1,464 条空答案全部是
    模型调用失败（status=error/partial）。这里钉住：重试一次仍为空 → ``parse_failure``
    ＋确定性兜底，且**永远不把空串交给用户**。
    """
    conversations, _turns, conversation, _db = env
    model = ScriptedModel([final("")])
    run_turn = build(env, model, StubToolbox())

    turn_id = run_turn.start(TurnRequest(conversation_id=conversation.id, question=QUESTION)).id
    result = run_turn.run(turn_id)

    assert result.termination_reason is TerminationReason.PARSE_FAILURE
    assert result.answer != ""
    assert "输出无法解析" in result.answer


# ---- 规划：1.0 goal_planner 的失败与重试 ------------------------------------


def test_a_planner_that_cannot_be_reached_says_the_model_service_is_down(env):
    """``planner_call_error`` 的收尾：1.0 ``runtime.py:1828`` 的 ``planner_blocked``。

    **终止原因照 1.0 记 ``planner_blocked``**（与生产库里那 2,126 条可比），**但文案说
    的是模型服务**（用户 2026-09-25 裁决二）。端点不可达是基础设施故障：照着「规划不出来」
    说，用户会去换个问法重问，而该做的是去看那个端点。
    """
    _conversations, _turns, conversation, _db = env
    model = ScriptedModel([final("不该被用到")], planner=RuntimeError("connection refused"))
    run_turn = build(env, model, StubToolbox())

    turn_id = start(run_turn, conversation)
    result = run_turn.run(turn_id)

    assert model.calls == []  # 主循环一次都没进
    assert result.answer == "模型服务当前不可用，因此这一轮没有开始检索。"
    assert result.termination_reason is TerminationReason.PLANNER_BLOCKED
    assert result.status is TurnStatus.PARTIAL


def test_the_model_service_message_carries_the_endpoint(env):
    """「可执行」就是这一条：说明里带着端点地址，用户与运维据此能直接去查。"""
    _conversations, _turns, conversation, _db = env
    model = ScriptedModel(
        [final("不该被用到")],
        planner=ModelServiceError("模型端点 http://127.0.0.1:8100/v1 不可达：connection refused"),
    )
    run_turn = build(env, model, StubToolbox())

    turn_id = start(run_turn, conversation)
    result = run_turn.run(turn_id)

    assert "模型服务当前不可用" in result.answer
    assert "http://127.0.0.1:8100/v1" in result.answer
    assert "无法可靠规划" not in result.answer


def test_an_unusable_plan_still_says_the_planning_failed(env):
    """模型答了但规划结果不可用——**这才**是规划问题，文案照 1.0 的原文。"""
    _conversations, _turns, conversation, _db = env
    model = ScriptedModel([final("不该被用到")], planner=PlannerScript(["我不会规划。"]))
    run_turn = build(env, model, StubToolbox())

    turn_id = start(run_turn, conversation)
    result = run_turn.run(turn_id)

    assert result.answer == "当前问题的证据需求无法可靠规划，因此暂时无法确认。"
    assert result.termination_reason is TerminationReason.PLANNER_BLOCKED
    assert result.status is TurnStatus.PARTIAL


def test_the_two_planner_failures_do_not_share_a_message(env):
    """反向门禁（裁决二）：两条路必须给出**不同**的用户可见文案。

    合并成一句是最省事的写法，也正是这次要改掉的东西——把基础设施故障说成理解能力不足。
    这条用例不认某一句具体措辞，只认「两者不是同一句」：将来谁把它们并回去，无论改成
    什么字，这里都会红。
    """
    _conversations, _turns, conversation, _db = env

    def answer(planner: object) -> str:
        run_turn = build(env, ScriptedModel([final("不该被用到")], planner=planner), StubToolbox())
        return run_turn.run(start(run_turn, conversation)).answer

    service_down = answer(RuntimeError("connection refused"))
    unusable_plan = answer(PlannerScript(["我不会规划。"]))

    assert service_down != unusable_plan


def test_a_garbled_plan_is_retried_with_the_error_fed_back(env):
    """规划被解析不出来时带错误回灌重试——1.0 的 ``_correction_hint`` 就是干这个的。"""
    _conversations, _turns, conversation, _db = env
    model = ScriptedModel(
        [final("这是你要的照片。")],
        planner=PlannerScript(["我不会规划。", planner_reply("photo_set").content]),
    )
    run_turn = build(env, model, StubToolbox())

    turn_id = start(run_turn, conversation)
    result = run_turn.run(turn_id)

    assert len(model.planner_calls) == 2
    assert "无法被解析" in model.planner_calls[1][-1].content
    assert result.answer == "这是你要的照片。"


def test_the_ledger_comes_from_the_plans_declaration(env):
    """账本里的需求就是模型声明的那几条——不是按问句关键词猜出来的。"""
    _conversations, turns, conversation, _db = env
    model = ScriptedModel(
        [final("这是你要的照片。")],
        planner=planner_reply("photo_set", "delivered_photo", "person_profile"),
    )
    run_turn = build(env, model, StubToolbox())

    turn_id = start(run_turn, conversation)
    result = run_turn.run(turn_id)

    assert [item["evidence_type"] for item in result.requirements] == [
        "photo_set",
        "delivered_photo",
        "person_profile",
    ]


# ---- L2 语义评审：1.0 judge.py 的调用口径 ------------------------------------


def test_an_unfaithful_answer_is_pushed_back_once_then_released(env):
    """L2 判出事实性问题 → 带着评审意见重写一次；第二次放行（每问只评审一次）。

    1.0 ``runtime.py:2753-2800``：评审一次、回灌意见、模型改口后放行并标
    ``l2_soft_release``。这里钉的是「只评审一次」与「放行的是改写后的回答」。
    """
    _conversations, _turns, conversation, _db = env
    model = ScriptedModel(
        [
            tool_call("search_memories", "c1", query="海边"),
            final("相册里有 5 张海边的照片。"),
            final("相册里有 2 张海边的照片。"),
        ],
        judge=PlannerScript(
            [
                judge_reply(("contradiction", "工具确认 2 张，回答写 5 张")).content,
                judge_reply().content,
            ]
        ),
    )
    run_turn = build(env, model, StubToolbox({"search_memories": SEARCH_OK}))
    turn_id = start(run_turn, conversation)

    result = run_turn.run(turn_id)

    assert len(model.judge_calls) == 1  # 每问只评审一次
    assert result.answer == "相册里有 2 张海边的照片。"
    assert any("工具确认 2 张" in message.content for message in model.calls[-1])

    # **1.0 ``runtime.py:2961-2968``：放行时打 ``l2_soft_release`` 标。**
    # 没有它，「评审放行的答案」与「评审通过的答案」在轨迹上完全一样，
    # 而评测与排查要区分的正是这两者。
    released = [
        event for event in _turns.events_after(turn_id, 0) if event.payload.get("code") == "l2_soft_release"
    ]
    assert len(released) == 1
    assert released[0].kind == "judge"
    assert released[0].payload["possibly_fabricated"] is True
    assert "工具确认 2 张" in released[0].payload["reason"]


def test_an_unfaithful_answer_is_released_when_the_judge_spent_the_last_step(env):
    """评审吃掉最后一个模型步时，**放行模型自己那一版答案**，不用占位符覆盖。

    1.0 ``runtime.py:2897`` 的 ``if turn.budget.can_model_step():`` 是配套闸门：没步就
    **不回环**，落回收尾路径放行上一版 final 并打 ``possibly_fabricated`` 标。2.0 抄了
    「评审计一个模型步」这个成本口径（``domain/assistant/judge.py`` 的模块注释写着
    「调用前计一个模型步。1.0 ``runtime.py:2761`` 先 ``record_model_step()`` 再调」），
    漏抄了这道闸门。

    预算不变量 ``6(往返) + 1(作答) + 1(评审) = 8 = max_steps`` 意味着**只要工具往返
    跑满 6 次，评审必然落在第 8 步**——判不实之后重答机会恒为 0。实测 2026-10-10 那轮
    487 题里 48 题（9.9%）如此，均分 0.021，而其余题是 0.891。
    """
    _conversations, _turns, conversation, _db = env
    # 6 次工具往返用满 ``max_tool_calls=6``（search 1 次 + 翻页 5 次），第 7 步作答，
    # 第 8 步评审 —— 评审正好吃掉最后一个模型步。
    script: list[ModelReply] = [tool_call("search_memories", "c1", query="海边")]
    script += [
        tool_call("get_result_page", f"c{index}", result_set_id="rs_1", page=index) for index in range(2, 7)
    ]
    script.append(final("相册里有 2 张海边的照片。"))
    model = ScriptedModel(script, judge=judge_reply(("fabrication", "工具观察不支持这个说法")))
    run_turn = build(
        env,
        model,
        StubToolbox({"search_memories": SEARCH_OK, "get_result_page": SEARCH_OK}),
    )
    turn_id = start(run_turn, conversation)

    result = run_turn.run(turn_id)

    # 模型那一版答案原样放行——**不是** ``emergency.fallback_answer`` 拼的占位符。
    assert result.answer == "相册里有 2 张海边的照片。"
    assert "模型步数用尽" not in result.answer
    assert result.termination_reason is TerminationReason.ANSWERED_WITH_GAP

    # 放行的同时还必须有那条标：没有它，「评审放行的答案」与「评审通过的答案」在轨迹
    # 上完全一样，而评测与排查要区分的正是这两者。
    released = [
        event for event in _turns.events_after(turn_id, 0) if event.payload.get("code") == "l2_soft_release"
    ]
    assert len(released) == 1
    assert released[0].payload["possibly_fabricated"] is True


def test_a_failing_judge_lets_the_answer_through(env):
    """L2 挂了就放行——1.0 ``judge.py:195-198`` 的 fail-open。

    评审是兜底不是门槛：它自己挂了不该把一轮正常的回答也带走。
    """
    _conversations, _turns, conversation, _db = env
    model = ScriptedModel(
        [
            tool_call("search_memories", "c1", query="海边"),
            final("这是你要的海边照片。"),
        ],
        judge=RuntimeError("评审端点挂了"),
    )
    run_turn = build(env, model, StubToolbox({"search_memories": SEARCH_OK}))
    turn_id = start(run_turn, conversation)

    result = run_turn.run(turn_id)

    assert result.answer == "这是你要的海边照片。"


def test_the_judge_is_not_called_without_tool_results(env):
    """没有工具结果就没有可核对的事实——1.0 的调用条件里有 ``task.tool_results``。"""
    _conversations, _turns, conversation, _db = env
    model = ScriptedModel([final("你好，我可以帮你找照片。")], judge=RuntimeError("不该被调用"))
    run_turn = build(env, model, StubToolbox())
    turn_id = start(run_turn, conversation)

    result = run_turn.run(turn_id)

    assert model.judge_calls == []
    assert result.answer == "你好，我可以帮你找照片。"


def test_the_judge_is_not_called_when_only_the_terminal_action_was_recorded(env):
    """**终止动作自己不算「工具观察」。**

    ``final_answer`` 是终止动作，``_finish_with_final_answer`` 会先把它 ``state.record``
    （``run_turn.py:791``）再调 ``_judge_answer``（``:812``）——所以「模型直接给终止动作」
    这条路上 ``not state.records`` **恒为假**。

    上面那条 ``..._without_tool_results`` 用的是正文收尾（``final()``），它的 ``records``
    确实是空的；**两条路只测了一条**，而真实评测里模型走的是 ``final_answer`` 工具
    （2026-10-10 那轮 643 次）。判据因此等价于「每一轮都评审」。

    1.0 ``runtime.py:2865`` 用的是 ``task.tool_results``：执行成功并记下的**工具观察**。
    """
    _conversations, _turns, conversation, _db = env
    model = ScriptedModel([final_call("你好，我可以帮你找照片。")], judge=RuntimeError("不该被调用"))
    run_turn = build(env, model, StubToolbox({FINAL_ANSWER_TOOL: DELIVER_OK}))
    turn_id = start(run_turn, conversation)

    result = run_turn.run(turn_id)

    assert model.judge_calls == []
    assert result.answer == "你好，我可以帮你找照片。"


def test_the_judge_is_not_called_when_every_tool_call_was_denied(env):
    """被拒的调用不产出观察，所以它不算「有工具结果」。

    1.0 的 ``task.tool_results`` 只在工具**执行成功**时记录。若判据写成「有 records」，
    被拒的尝试也会把评审放进来——而那时评审手上一条观察都没有，任何具体断言在它眼里
    都像编造（这正是判不实率被抬高的机制之一）。
    """
    _conversations, _turns, conversation, _db = env
    denied = ToolOutcome(
        observation={"failure_reason": "page_call_limit"},
        model_visible={"error": "page_call_limit"},
        failure_reason="page_call_limit",
    )
    model = ScriptedModel(
        [tool_call("get_result_page", "c1", result_set_id="rs_1", page=9), final("没有更多了。")],
        judge=RuntimeError("不该被调用"),
    )
    run_turn = build(env, model, StubToolbox({"get_result_page": denied}))
    turn_id = start(run_turn, conversation)

    result = run_turn.run(turn_id)

    assert model.judge_calls == []
    assert result.answer == "没有更多了。"


def test_only_missing_disclosure_does_not_block_the_answer(env):
    """``missing_disclosure`` 只是披露建议（severity=style），不拦事实正确的回答。"""
    _conversations, _turns, conversation, _db = env
    model = ScriptedModel(
        [
            tool_call("search_memories", "c1", query="海边"),
            final("这是你要的海边照片。"),
        ],
        judge=judge_reply(("missing_disclosure", "没有说明只是候选")),
    )
    run_turn = build(env, model, StubToolbox({"search_memories": SEARCH_OK}))
    turn_id = start(run_turn, conversation)

    result = run_turn.run(turn_id)

    assert result.answer == "这是你要的海边照片。"


def test_a_faithful_answer_carries_no_soft_release_label(env):
    """评审通过的答案**不许**带标——这一半才是「打标」这个动作有意义的依据。

    只断言「判不实时会打标」的话，一个**永远打标**的实现也能过，而那种标什么都说明不了。
    """
    _conversations, turns, conversation, _db = env
    model = ScriptedModel(
        [tool_call("search_memories", "c1", query="海边"), final("相册里有 2 张海边的照片。")],
        judge=judge_reply(),
    )
    run_turn = build(env, model, StubToolbox({"search_memories": SEARCH_OK}))
    turn_id = start(run_turn, conversation)

    run_turn.run(turn_id)

    assert len(model.judge_calls) == 1, "评审该被调用一次"
    assert not [
        event for event in turns.events_after(turn_id, 0) if event.payload.get("code") == "l2_soft_release"
    ]


def test_a_style_only_problem_does_not_earn_the_label(env):
    """``missing_disclosure`` 只是披露建议，**不拦事实正确的回答**，也不打标
    （1.0 ``judge.py:174-176`` 的 severity 分层：只有 ``_TRUTH_TYPES`` 才走软引导那一支）。

    打标的口径必须与软引导的口径一致：标的意思是「带着一次未复评的不实判决出门了」，
    而风格问题根本没被判成不实——给它打标会把那个词的含义稀释掉。
    """
    _conversations, turns, conversation, _db = env
    model = ScriptedModel(
        [tool_call("search_memories", "c1", query="海边"), final("相册里有 2 张海边的照片。")],
        judge=judge_reply(("missing_disclosure", "没有说明只是候选")),
    )
    run_turn = build(env, model, StubToolbox({"search_memories": SEARCH_OK}))
    turn_id = start(run_turn, conversation)

    result = run_turn.run(turn_id)

    assert result.answer == "相册里有 2 张海边的照片。", "风格问题不该让回答被退回重写"
    assert not [
        event for event in turns.events_after(turn_id, 0) if event.payload.get("code") == "l2_soft_release"
    ]


def test_the_recorded_tool_result_keeps_every_observation_key():
    """轨迹里记的是**整份** observation，不是一份被裁过的「规模摘要」。

    原先的白名单只留 ``total / has_more / backend / degraded / delivered`` 五个键 +
    ``asset_ids``，而 ``read_photo_text`` 的 observation
    （``result_set_id / asset_handle / asset_id / source / full_text``）与它**零交集**
    —— 2026-10-10 那轮因此留下 159 条空对象，事后看不出模型读到了什么字。
    ``degraded_reason`` 也同样被丢掉，于是「为什么降级」无法从 run.json 判断，
    而那正是分析那轮的全部起点。
    """
    from sentrix.application.assistant.run_turn import _tool_result_shape

    outcome = ToolOutcome(
        observation={
            "result_set_id": "rs_1",
            "asset_id": "asset_1",
            "full_text": "囍",
            "degraded_reason": "embedder_unavailable",
        },
        model_visible={"full_text": "囍"},
    )

    assert _tool_result_shape(outcome) == {
        "result_set_id": "rs_1",
        "asset_id": "asset_1",
        "full_text": "囍",
        "degraded_reason": "embedder_unavailable",
    }


def test_a_pushed_back_answer_survives_a_failed_rewrite(env):
    """**模型写过答案就永远不用固定文案收尾**——哪怕重答把步数烧光了。

    实测 2026-10-15 那轮有 21 道题是同一条路：模型给出 final → L2 判不实打回
    → **还有一步**（评审落在第 7 步，第 8 步才是重答）→ 模型重答时没直接说，
    而是又去翻页 → 被拒 → 步数耗尽 → ``_stop`` 用
    「这一轮检索到 12 张候选照片。（本轮因「模型步数用尽」提前结束。）」把**模型
    已经写好的一版答案**顶掉了。

    1.0 ``runtime.py:2415-2419`` 的 D12：「恢复/后续模型调用失败时，不丢弃已产出的
    final 回答」。
    """
    _conversations, _turns, conversation, _db = env
    script: list[ModelReply] = [tool_call("search_memories", "c1", query="海边")]
    script += [
        tool_call("get_result_page", f"p{index}", result_set_id="rs_1", page=index) for index in range(1, 5)
    ]
    script.append(final("相册里有 2 张海边的照片。"))
    script.append(tool_call("get_result_page", "p9", result_set_id="rs_1", page=9))
    model = ScriptedModel(script, judge=judge_reply(("fabrication", "工具观察不支持这个说法")))
    run_turn = build(
        env,
        model,
        StubToolbox({"search_memories": SEARCH_OK, "get_result_page": SEARCH_OK}),
    )
    turn_id = start(run_turn, conversation)

    result = run_turn.run(turn_id)

    assert result.answer == "相册里有 2 张海边的照片。"
    assert "模型步数用尽" not in result.answer
    assert result.termination_reason is TerminationReason.STEP_BUDGET_EXHAUSTED


def test_the_fallback_text_is_never_used_when_the_model_said_anything(env):
    """固定文案只在**模型一个字都没说出来**时才出现。

    这一条与上面那条是同一件事的两面：``_stop`` 先看 ``last_answer``，有就用它。
    把这条单独钉出来，是因为「代码绝不替模型说话」是这一层的**根本要求**，
    而它很容易在后续改动里被绕过（比如又往 ``_stop`` 里加一条早退分支）。
    """
    _conversations, _turns, conversation, _db = env
    # 模型说完一句就去翻页，把剩下的步数烧光。
    model = ScriptedModel(
        [
            tool_call("search_memories", "c1", query="海边"),
            final("我看到 2 张海边的照片。"),
            tool_call("get_result_page", "p2", result_set_id="rs_1", page=2),
        ]
    )
    run_turn = build(
        env,
        model,
        StubToolbox({"search_memories": SEARCH_OK, "get_result_page": SEARCH_OK}),
    )
    turn_id = start(run_turn, conversation)

    result = run_turn.run(turn_id)

    assert "这一轮检索到" not in result.answer
    assert "提前结束" not in result.answer


@pytest.mark.parametrize("phase", ["planner", "agent"])
def test_operational_error_evidence_survives_the_turn_record(env, phase):
    _conversations, turns, conversation, _db = env
    error = ModelServiceError(
        "provider rejected request",
        error_code="provider_rejected",
        http_status=429,
        request_policy={"max_model_len": 262144, "max_output_tokens": None},
    )
    model = ScriptedModel([error], planner=error if phase == "planner" else None)
    run_turn = build(env, model, StubToolbox())
    turn_id = start(run_turn, conversation)
    run_turn.run(turn_id)
    record = turns.get(turn_id)
    assert record is not None
    failures = [call for call in record.trace["model_calls"] if call.get("error_code")]
    assert len(failures) == 1
    assert failures[0]["error_code"] == "provider_rejected"
    assert failures[0]["http_status"] == 429
    assert failures[0]["request_policy"]["max_model_len"] == 262144


def test_faithfulness_fail_open_keeps_operational_evidence(env):
    _conversations, turns, conversation, _db = env
    model = ScriptedModel(
        [tool_call("search_memories", "c1", query="海边"), final("这是你要的海边照片。")],
        judge=ModelServiceError(
            "provider rejected review",
            error_code="provider_rejected",
            http_status=429,
            request_policy={"max_model_len": 262144, "max_output_tokens": None},
        ),
    )
    run_turn = build(env, model, StubToolbox({"search_memories": SEARCH_OK}))
    turn_id = start(run_turn, conversation)
    result = run_turn.run(turn_id)
    assert result.answer == "这是你要的海边照片。"
    record = turns.get(turn_id)
    assert record is not None
    reviews = [call for call in record.trace["model_calls"] if call["call_type"] == "faithfulness"]
    assert len(reviews) == 1
    assert reviews[0]["http_status"] == 429
    assert reviews[0]["error_code"] == "provider_rejected"
    assert reviews[0]["request_policy"]["max_model_len"] == 262144
