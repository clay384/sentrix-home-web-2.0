"""``GET /api/v1/assistant/turns/{id}/events`` 的 SSE 契约与 ``GET .../{id}`` 的轮询契约。

事件形状沿用 1.0 已验证的两类（``app.py:2986`` / ``:2997``）：``progress`` 与 ``complete``。
本文件钉住的是 2.0 相对 1.0 的四处修正：

1. 事件带 ``id:``，断线可以从 ``Last-Event-ID`` 续（1.0 每次重连都从头重发）；
2. ``complete`` 帧不再把整条进度数组重复一遍（1.0 ``app.py:2989``）；
3. ``status`` 是真实的终态（1.0 把模型失败、超时、guard 拦截全部压成 ``complete``）；
4. 浏览器不必轮询就能拿到全过程。**但轮询端点照搬了回来**——1.0 的 668,552 次轮询来自
   ``services/photobench`` 的 ``wait_for_assistant_turn``（非浏览器，``EventSource`` 不可用），
   而不是 1.0 前端的 ``onerror`` 降级；把端点删掉只会让所有非浏览器调用方没法用。
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Sequence

import pytest
from fastapi.testclient import TestClient

from sentrix.domain.assistant.models import ChatMessage, ModelReply, ToolCall
from sentrix.domain.assistant.ports import ModelServiceError
from sentrix.main import create_app


class ScriptedChatModel:
    """主循环按剧本回话；规划（``tools`` 为空）走 ``planner_reply``，不消耗剧本。"""

    def __init__(self, replies: Sequence[ModelReply]) -> None:
        self._replies = list(replies)
        self.calls = 0
        self.planner_calls = 0

    def complete(self, messages: Sequence[ChatMessage], tools: Sequence[object]) -> ModelReply:
        if not tools:
            self.planner_calls += 1
            return planner_reply("photo_set", "delivered_photo")  # type: ignore[return-value]
        self.calls += 1
        return self._replies[min(self.calls - 1, len(self._replies) - 1)]


def final(content: str) -> ModelReply:
    return ModelReply(content=content, finish_reason="stop")


def tool_call(name: str, call_id: str, **arguments: object) -> ModelReply:
    return ModelReply(
        content="",
        finish_reason="tool_calls",
        tool_calls=(ToolCall(call_id=call_id, name=name, arguments_json=json.dumps(arguments)),),
    )


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
                {
                    "id": f"req_{index + 1}",
                    "evidence_type": item,
                    "description": f"需要 {item}",
                    "predicate": "check",
                    "value_type": "media_set",
                    "scope": {},
                    "required": True,
                }
                for index, item in enumerate(evidence_types)
            ],
        },
    }
    return ModelReply(content=json.dumps(declaration, ensure_ascii=False), finish_reason="stop")


class FailingModel:
    """整个端点都挂了——规划与主循环一起失败。

    抛的是 ``ModelServiceError``（``ChatModel`` 端口对「服务这一侧失败」的契约形状，
    见 ``domain/assistant/ports.py``），不是裸 ``RuntimeError``：假件比真件宽松，就验不出
    「上层只认那个类型」这类问题。
    """

    def complete(self, messages: Sequence[ChatMessage], tools: Sequence[object]) -> ModelReply:
        raise ModelServiceError("模型端点 http://127.0.0.1:8100/v1 不可达：connection refused")


class DiesAfterPlanning:
    """规划照常，进主循环就挂。

    抛的是 ``ModelServiceError``（与 ``FailingModel`` 同一条理由：假件比真件宽松，就验不出
    「上层只认那个类型」这类问题）。文案里就是 8100 缺 tool-call flag 时那句真实的 400。
    """

    planned = False

    def complete(self, messages: Sequence[ChatMessage], tools: Sequence[object]) -> ModelReply:
        if not self.planned:
            self.planned = True
            return planner_reply("photo_set", "delivered_photo")  # type: ignore[return-value]
        raise ModelServiceError(
            "模型端点 http://127.0.0.1:8100/v1 上的 gemma4-12b-it 返回 400："
            r'{"error":{"message":"\"auto\" tool choice requires --enable-auto-tool-choice'
            r' and --tool-call-parser to be set"}}'
        )


def build_client(settings, model) -> TestClient:
    app = create_app(settings)
    app.state.container = dataclasses.replace(app.state.container, chat_model=model)
    return TestClient(app)


def read_events(client: TestClient, turn_id: str, **headers: str) -> list[dict]:
    """把 SSE 流读完，返回 ``[{event, data}]``。"""
    lines = []
    with client.stream(
        "GET", f"/api/v1/assistant/turns/{turn_id}/events", headers=headers or None
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        lines = [line for line in response.iter_lines() if line]

    events: list[dict] = []
    current: dict = {}
    for line in lines:
        if line.startswith("id: "):
            current["id"] = int(line[4:])
        elif line.startswith("event: "):
            current["event"] = line[7:]
        elif line.startswith("data: "):
            current["data"] = json.loads(line[6:])
            events.append(current)
            current = {}
    return events


def run_turn(client: TestClient, message: str = "给我看看在海边的照片") -> tuple[str, str]:
    conversation = client.post("/api/v1/assistant/conversations", json={"scope_id": "album1"}).json()
    turn = client.post(
        f"/api/v1/assistant/conversations/{conversation['id']}/turns",
        json={"message": message, "scope_id": "album1"},
    ).json()
    return conversation["id"], turn["turn_id"]


@pytest.fixture
def client(settings) -> TestClient:
    return build_client(settings, ScriptedChatModel([final("这是一句回答。")]))


def test_the_stream_ends_by_itself_after_complete(client: TestClient):
    """一次请求拿完全过程——浏览器侧的主路径。

    轮询端点**同时存在**（见文件末尾的 ``test_the_polling_endpoint_*``）：它不是「回退」，
    而是 ``EventSource`` 用不了的调用方唯一的路。1.0 的 8091 访问日志里这条端点被调了
    668,552 次、SSE 只有 62 次，调用方是 ``services/photobench`` 的轮询循环，不是浏览器。
    """
    _conversation_id, turn_id = run_turn(client)

    events = read_events(client, turn_id)

    assert events[-1]["event"] == "complete"


def test_progress_events_carry_the_designed_field_set(client: TestClient):
    _conversation_id, turn_id = run_turn(client)

    progress = [item for item in read_events(client, turn_id) if item["event"] == "progress"]

    assert progress, "至少要有一条进度事件"
    for item in progress:
        data = item["data"]
        assert set(data) == {
            "text",
            "status",
            "stage",
            "step_index",
            "timestamp",
            "kind",
            "payload",
        }
        assert data["stage"] in {
            "thinking",
            "planning",
            "inspecting",
            "action",
            "tool_result",
            "recovering",
            "finalizing",
            "terminal",
        }
        assert data["status"] in {"running", "ok", "denied", "partial", "complete", "cancelled"}


def test_the_first_progress_event_is_the_plan(client: TestClient):
    """规划是 S3 的第一步，也是「为什么这一轮要取证」的书面记录。"""
    _conversation_id, turn_id = run_turn(client)

    first = read_events(client, turn_id)[0]

    assert first["data"]["kind"] == "plan"
    assert first["data"]["stage"] == "planning"
    assert [item["evidence_type"] for item in first["data"]["payload"]["requirements"]] == [
        "photo_set",
        "delivered_photo",
    ]


def test_progress_ids_are_monotonic_and_usable_for_resume(client: TestClient):
    _conversation_id, turn_id = run_turn(client)

    progress = [item for item in read_events(client, turn_id) if item["event"] == "progress"]

    ids = [item["id"] for item in progress]
    assert ids == sorted(ids)
    assert ids == list(range(1, len(ids) + 1))


def test_the_terminal_event_is_always_last_before_complete(client: TestClient):
    _conversation_id, turn_id = run_turn(client)

    events = read_events(client, turn_id)

    assert events[-2]["data"]["kind"] == "terminal"
    assert events[-2]["data"]["stage"] == "terminal"
    assert events[-1]["event"] == "complete"


def test_the_complete_frame_does_not_repeat_the_progress_array(client: TestClient):
    """1.0 的 ``complete`` 帧把已经逐条发过的 public_progress 又完整塞了一遍。"""
    _conversation_id, turn_id = run_turn(client)

    complete = read_events(client, turn_id)[-1]["data"]

    assert complete["type"] == "complete"
    assert set(complete) == {
        "type",
        "turn_id",
        "status",
        "termination_reason",
        "answer",
        "attachments",
        "requirements",
        # §3.8：检索抓回来的那批候选，与 attachments（交付的那批）分开。
        "retrieved_candidates",
        # 回合级轨迹：模型调用 / 规划 / 账本 / 门禁 / 预算。PhotoBench 的三块面板读它。
        "trace",
        "error",
    }
    assert complete["turn_id"] == turn_id
    assert complete["answer"] == "这是一句回答。"
    assert complete["termination_reason"] == "answered_with_gap"
    # 用户要图、模型只回了一段文字——终态必须如实反映这件事，不能报 complete。
    assert complete["status"] == "partial"
    assert [item["status"] for item in complete["requirements"]] == ["open", "open"]


def test_a_dead_endpoint_is_reported_as_the_model_service(settings):
    """端点整个挂掉时，失败发生在**规划**这一步——1.0 也是这么记的。

    1.0 ``runtime.py:1828``：``planner_call_error`` 走到最后就是 ``planner_blocked``，
    status 是 ``partial``。1.0 的毛病不在这里——它把这类失败也压成了 ``status="complete"``，
    2.0 如实报成 ``partial``。

    **但文案说的必须是模型服务**（用户 2026-09-25 裁决二）。1.0 生产库里那 2,126 条
    ``planner_blocked`` 的 answer 全是「当前问题的证据需求无法可靠规划，因此暂时无法确认。」
    ——那是把基础设施故障说成了理解能力不足：用户会去换个问法重问，而该做的是去看那个
    端点。这里把端点自己的说明（含地址）一并交给用户。
    """
    client = build_client(settings, FailingModel())
    _conversation_id, turn_id = run_turn(client)

    complete = read_events(client, turn_id)[-1]["data"]

    assert complete["status"] == "partial"
    assert complete["termination_reason"] == "planner_blocked"
    assert "模型服务当前不可用" in complete["answer"]
    assert "http://127.0.0.1:8100/v1" in complete["answer"]
    assert "无法可靠规划" not in complete["answer"]


def test_a_model_that_dies_after_planning_is_reported_as_failed(settings):
    """规划成功、主循环才挂时，才是 ``failed`` / ``model_error``。

    **且文案要可执行**（用户 2026-09-25 裁决二）：说模型服务、带端点、带服务端给的原因。
    8100 没有 ``--enable-auto-tool-choice`` / ``--tool-call-parser`` 时，任何带 ``tools``
    的请求一律 400（见 ``docs/refactor/s5-deviations.md`` §2n）——那句话里写着根因，
    不能让它只在日志里落地为止。
    """
    client = build_client(settings, DiesAfterPlanning())
    _conversation_id, turn_id = run_turn(client)

    complete = read_events(client, turn_id)[-1]["data"]

    assert complete["status"] == "failed"
    assert complete["termination_reason"] == "model_error"
    assert "模型服务不可用" in complete["answer"]
    assert "http://127.0.0.1:8100/v1" in complete["answer"]
    assert "--tool-call-parser" in complete["answer"]


def test_a_late_subscriber_still_gets_the_whole_turn(client: TestClient):
    """回合跑完之后才连上来，照样拿得到全过程。

    1.0 的进度只存在进程内存里，页面一刷新就再也拿不回来——那正是轮询成为主路径的
    原因之一。2.0 的事件先落库，历史与实时各走一条路。
    """
    _conversation_id, turn_id = run_turn(client)
    read_events(client, turn_id)  # 第一次连接把回合跑完

    events = read_events(client, turn_id)

    assert [item["event"] for item in events][-1] == "complete"
    assert events[-1]["data"]["answer"] == "这是一句回答。"
    assert len([item for item in events if item["event"] == "progress"]) == len(
        [item for item in read_events(client, turn_id) if item["event"] == "progress"]
    )


def test_last_event_id_resumes_without_replaying(client: TestClient):
    """断线重连从 ``Last-Event-ID`` 之后继续，不重发已经收到的事件。"""
    _conversation_id, turn_id = run_turn(client)
    first = read_events(client, turn_id)
    resume_from = [item["id"] for item in first if item["event"] == "progress"][1]

    resumed = read_events(client, turn_id, **{"Last-Event-ID": str(resume_from)})

    ids = [item["id"] for item in resumed if item["event"] == "progress"]
    assert ids and min(ids) > resume_from
    assert resumed[-1]["event"] == "complete"


def test_a_garbage_last_event_id_falls_back_to_a_full_replay(client: TestClient):
    _conversation_id, turn_id = run_turn(client)

    events = read_events(client, turn_id, **{"Last-Event-ID": "not-a-number"})

    assert [item["id"] for item in events if item["event"] == "progress"][0] == 1


def test_two_turns_in_one_conversation_each_get_their_own_stream(settings):
    client = build_client(
        settings,
        ScriptedChatModel([tool_call("get_result_page", "c1", result_set_id="rs_1", page=1)]),
    )
    conversation_id, first_turn = run_turn(client)

    second_turn = client.post(
        f"/api/v1/assistant/conversations/{conversation_id}/turns",
        json={"message": "再看看别的", "scope_id": "album1"},
    ).json()["turn_id"]

    assert first_turn != second_turn
    first = read_events(client, first_turn)
    second = read_events(client, second_turn)
    assert first[-1]["data"]["turn_id"] == first_turn
    assert second[-1]["data"]["turn_id"] == second_turn
    # 每个回合的 step_index 各自从 1 开始——前端拿它当 key 时必须限定回合作用域。
    assert [item["id"] for item in first if item["event"] == "progress"][0] == 1
    assert [item["id"] for item in second if item["event"] == "progress"][0] == 1


def test_a_tool_call_turn_emits_action_and_result_events(settings):
    client = build_client(
        settings,
        ScriptedChatModel(
            [
                tool_call("search_memories", "c1", query="海边"),
                final("这是你要的照片。"),
            ]
        ),
    )
    _conversation_id, turn_id = run_turn(client)

    progress = [item["data"] for item in read_events(client, turn_id) if item["event"] == "progress"]

    kinds = [item["kind"] for item in progress]
    assert "model_action" in kinds
    assert "tool_result" in kinds
    action = next(item for item in progress if item["kind"] == "model_action")
    assert action["payload"]["tool"] == "search_memories"
    assert action["payload"]["tool_call_id"] == "c1"


def test_a_finished_turn_is_visible_in_the_conversation_detail(client: TestClient):
    """回合收尾之后，会话里该有的东西都在。

    这条用例**必须**放在 SSE 这一侧：它靠「把事件流读完」来等待回合结束，而这正是
    「一次请求拿完全过程」的用法本身。放在 Task 42 的会话契约里会变成依赖一个还不存在
    的端点——逐任务验证时它就是这么暴露出来的。
    """
    conversation_id, turn_id = run_turn(client)
    # 读完 SSE 流就意味着回合已经收尾。
    read_events(client, turn_id)

    detail = client.get(f"/api/v1/assistant/conversations/{conversation_id}").json()

    assert [message["role"] for message in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][1]["content"] == "这是一句回答。"
    assert detail["turns"][0]["status"] == "partial"
    assert detail["turns"][0]["termination_reason"] == "answered_with_gap"


# ---- 轮询端点：1.0 app.py:2938 的 B3.4 契约 ---------------------------------


def test_the_polling_endpoint_returns_the_1_0_shape(client: TestClient):
    """``GET /api/v1/assistant/turns/{id}`` 的字段集与 1.0 一致：

    ``turn_id`` / ``status`` / ``public_progress``，终态时多一个 ``result``。
    """
    _conversation_id, turn_id = run_turn(client)

    body = client.get(f"/api/v1/assistant/turns/{turn_id}").json()

    assert body["turn_id"] == turn_id
    assert body["status"] in {
        "pending",
        "running",
        "complete",
        "partial",
        "failed",
        "blocked",
        "cancelled",
    }
    assert isinstance(body["public_progress"], list)


def test_the_polling_endpoint_carries_the_same_payload_as_the_complete_frame(client: TestClient):
    """轮询拿到的终态载荷必须与 SSE 的 ``complete`` 帧**逐字段相同**。

    两条传输给出不同的答案，「回退到轮询」就不再是等价降级——1.0 的
    ``wait_for_assistant_turn`` 读的正是 ``state["result"]``。
    """
    _conversation_id, turn_id = run_turn(client)

    complete = [item for item in read_events(client, turn_id) if item["event"] == "complete"][-1]
    polled = client.get(f"/api/v1/assistant/turns/{turn_id}").json()

    assert polled["status"] == complete["data"]["status"]
    assert polled["result"] == complete["data"]


def test_the_polling_endpoint_is_404_for_an_unknown_turn(client: TestClient):
    assert client.get("/api/v1/assistant/turns/turn_does_not_exist").status_code == 404


def test_the_polling_endpoint_answers_a_running_turn_without_a_result(client: TestClient):
    """回合还没跑完时 ``result`` 缺席——1.0 的调用方靠这个判断「继续等」。"""
    conversation = client.post("/api/v1/assistant/conversations", json={"scope_id": "album1"}).json()
    started = client.post(
        f"/api/v1/assistant/conversations/{conversation['id']}/turns",
        json={"message": "给我看看在海边的照片"},
    ).json()

    body = client.get(f"/api/v1/assistant/turns/{started['turn_id']}").json()

    # 工作线程可能已经跑完（脚本模型不慢），但两种状态都必须是自洽的。
    if body["status"] in {"pending", "running"}:
        assert body["result"] is None
    else:
        assert body["result"] is not None


def test_terminal_transition_between_history_read_and_check_cannot_drop_progress(client, monkeypatch):
    import time

    _, turn_id = run_turn(client)
    turns = client.app.state.container.turn_repository
    deadline = time.monotonic() + 3
    while not turns.get(turn_id).status.is_terminal:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    original = turns.events_after
    expected = list(original(turn_id, 0))
    assert expected
    first = True

    def read_before_commit(identifier, cursor):
        nonlocal first
        if first:
            first = False
            return []  # snapshot from just before the worker committed its events
        return original(identifier, cursor)

    monkeypatch.setattr(turns, "events_after", read_before_commit)
    events = read_events(client, turn_id)
    assert [e["id"] for e in events if e["event"] == "progress"] == [e.step_index for e in expected]
    assert events[0]["data"]["kind"] == "plan"
    assert events[-1]["event"] == "complete"
