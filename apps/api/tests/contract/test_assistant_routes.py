"""``/api/v1/assistant/*`` 的契约测试。

模型换成脚本化的假件——契约测试要验的是 HTTP 映射与会话生命周期，不是模型。真模型
的验收在 tests/e2e（Task 45）。
"""

from __future__ import annotations

import dataclasses
import io
import json
import time
from collections.abc import Sequence
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from sentrix.domain.assistant.models import ChatMessage, ModelReply, ToolCall
from sentrix.domain.assistant.tools import TOOL_SPECS, ToolSpec
from sentrix.domain.retrieval import scheme

#: 规划那次调用的 system 提示词开头。**分流按它，不按 ``tools`` 空不空**——JIT 剪枝
#: 之后工具循环也可能合法地拿到空元组（额度用尽时 ``select_tools`` 返回空集）。
#: 与 ``test_run_turn.py`` 的 ``PLANNER_MARKER`` 是同一条判据。
PLANNER_MARKER = "你正在规划家庭记忆任务"


class ScriptedChatModel:
    """主循环按剧本回话；规划走 ``planner_reply``，不消耗剧本。"""

    def __init__(self, replies: Sequence[ModelReply]) -> None:
        self._replies = list(replies)
        self.calls = 0
        self.planner_calls = 0

    def complete(self, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec]) -> ModelReply:
        if PLANNER_MARKER in (messages[0].content if messages else ""):
            self.planner_calls += 1
            return planner_reply("photo_set", "delivered_photo")
        assert {spec.name for spec in tools} <= {spec.name for spec in TOOL_SPECS}
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
                {"id": f"req_{index + 1}", "evidence_type": item, "description": f"需要 {item}"}
                for index, item in enumerate(evidence_types)
            ],
        },
    }
    return ModelReply(content=json.dumps(declaration, ensure_ascii=False), finish_reason="stop")


@pytest.fixture
def client(isolated_app) -> TestClient:
    """把模型换成脚本化的假件，其余装配（真 SQLite、真迁移）保持原样。

    默认剧本是「直接给一个不带交付的答案」——大多数契约用例只关心会话与回合的形状。
    """
    app = isolated_app
    app.state.container = dataclasses.replace(
        app.state.container, chat_model=ScriptedChatModel([final("这是一句回答。")])
    )
    return TestClient(app)


def make_conversation(client: TestClient, scope_id: str = "album1") -> dict:
    response = client.post("/api/v1/assistant/conversations", json={"scope_id": scope_id})
    assert response.status_code == 201, response.text
    return response.json()


def start_turn(client: TestClient, conversation_id: str, message: str = "给我看看在海边的照片") -> dict:
    # 不传 scope：回合的 scope 由它所属的会话决定。
    response = client.post(
        f"/api/v1/assistant/conversations/{conversation_id}/turns",
        json={"message": message},
    )
    assert response.status_code == 202, response.text
    return response.json()


# ---- 会话 -------------------------------------------------------------------


def test_creating_a_conversation_returns_its_shape(client: TestClient):
    body = make_conversation(client)

    assert body["scope_id"] == "album1"
    assert body["title"] == "新对话"
    assert body["id"].startswith("conv_")
    assert body["created_at"] == body["updated_at"]


def test_conversations_are_listed_per_scope(client: TestClient):
    created = make_conversation(client)
    make_conversation(client, scope_id="album2")

    listed = client.get("/api/v1/assistant/conversations", params={"scope_id": "album1"}).json()

    assert [item["id"] for item in listed["items"]] == [created["id"]]


def test_a_conversation_can_be_renamed(client: TestClient):
    created = make_conversation(client)

    response = client.patch(f"/api/v1/assistant/conversations/{created['id']}", json={"title": "海边那次"})

    assert response.status_code == 200
    assert response.json()["title"] == "海边那次"


def test_renaming_a_missing_conversation_is_404(client: TestClient):
    assert client.patch("/api/v1/assistant/conversations/conv_nope", json={"title": "x"}).status_code == 404


def test_a_blank_title_is_rejected(client: TestClient):
    created = make_conversation(client)

    response = client.patch(f"/api/v1/assistant/conversations/{created['id']}", json={"title": ""})

    assert response.status_code == 422


def test_a_conversation_can_be_deleted(client: TestClient):
    created = make_conversation(client)

    assert client.delete(f"/api/v1/assistant/conversations/{created['id']}").status_code == 204

    assert client.get(f"/api/v1/assistant/conversations/{created['id']}").status_code == 404
    assert client.delete(f"/api/v1/assistant/conversations/{created['id']}").status_code == 404


def test_the_detail_view_carries_messages_and_turns(client: TestClient):
    created = make_conversation(client)
    turn = start_turn(client, created["id"])
    # The worker can finish before GET; assert a stable lifecycle boundary.
    poll_until_terminal(client, turn["turn_id"])

    body = client.get(f"/api/v1/assistant/conversations/{created['id']}").json()

    assert body["conversation"]["id"] == created["id"]
    assert [message["role"] for message in body["messages"]] == ["user", "assistant"]
    assert [item["id"] for item in body["turns"]] == [turn["turn_id"]]


# ---- 建回合 -----------------------------------------------------------------


def test_starting_a_turn_returns_a_pending_turn_id(client: TestClient):
    created = make_conversation(client)

    body = start_turn(client, created["id"])

    assert body["turn_id"].startswith("turn_")
    assert body["conversation_id"] == created["id"]
    # 回合先落库再执行：SSE 永远有东西可订阅。
    assert body["status"] in {"pending", "running"}


def test_a_turn_inherits_the_scope_of_its_conversation(client: TestClient):
    """回合的 scope 必须来自会话，不能来自请求。

    实测过的坏法：用 ``{"scope_id": "album1"}`` 建会话，再用**不带 scope** 的请求
    建回合，回合会落到 ``StartTurnRequest.scope_id`` 的默认值 ``home-default`` 上。
    检索于是在一个空 scope 里跑，助手如实回答「没有找到」——失败是静默的，表现为
    「搜索质量差」而不是「配置错了」。这条用例钉的就是这条不变量。
    """
    conversation = make_conversation(client, scope_id="album1")

    turn = start_turn(client, conversation["id"])

    recorded = client.app.state.container.turn_repository.get(turn["turn_id"])
    assert recorded is not None
    assert recorded.scope_id == "album1"


def test_a_scope_sent_by_the_caller_cannot_override_the_conversation(client: TestClient):
    """请求里残留的 ``scope_id`` 造不出「回合的 scope ≠ 会话的 scope」。

    缓存里的旧前端仍会带上这个字段。它现在只是一个被忽略的多余字段，而不是一次把
    回合改道到别的 scope 的静默重定向。
    """
    conversation = make_conversation(client, scope_id="album1")

    response = client.post(
        f"/api/v1/assistant/conversations/{conversation['id']}/turns",
        json={"message": "在吗", "scope_id": "album2"},
    )

    assert response.status_code == 202
    recorded = client.app.state.container.turn_repository.get(response.json()["turn_id"])
    assert recorded is not None
    assert recorded.scope_id == "album1"


def test_a_blank_message_is_rejected(client: TestClient):
    created = make_conversation(client)

    response = client.post(f"/api/v1/assistant/conversations/{created['id']}/turns", json={"message": ""})

    assert response.status_code == 422


def test_starting_a_turn_on_a_missing_conversation_is_404(client: TestClient):
    response = client.post("/api/v1/assistant/conversations/conv_nope/turns", json={"message": "在吗"})

    assert response.status_code == 404


def test_the_conversation_title_follows_the_first_question(client: TestClient):
    created = make_conversation(client)

    start_turn(client, created["id"], message="给我看看在海边的照片")

    detail = client.get(f"/api/v1/assistant/conversations/{created['id']}").json()
    assert detail["conversation"]["title"] == "给我看看在海边的照片"


def test_a_turn_can_be_started_without_creating_a_conversation_first(client: TestClient):
    """不带 ``conversation_id`` 时由调用方先建会话——本切片不做隐式建会话。

    反向验证：这里显式断言「没建会话 = 404」，免得将来有人加一条静默 fallback
    把一次调用方错误变成一次看起来正常的对话。
    """
    response = client.post(
        "/api/v1/assistant/conversations/conv_nope/turns",
        json={"message": "在吗"},
    )

    assert response.status_code == 404
    # 裁决③ 之后这个 404 由统一的领域错误处理器给出（``api/errors.py`` 那一个）：
    # 形状是 ``{code, message, detail}``，不再是 ``HTTPException`` 的 ``{detail}``。
    # 状态码与文案都没变，变的只是「谁决定它」——以前是路由硬编码，现在是异常带着走。
    assert response.json()["code"] == "not_found"
    assert response.json()["message"] == "conversation not found"


def test_an_unknown_turn_has_no_event_stream(client: TestClient):
    assert client.get("/api/v1/assistant/turns/turn_nope/events").status_code == 404


# ---- 候选集：与交付集分开暴露（§3.8）-----------------------------------------
#
# 这一组要**真的有照片可供检索**，所以自带一个脚本化嵌入器的 client——与
# ``test_search_routes.py`` 同一个套路（向量由测试指定，「哪张排第一」是构造出来的）。

RED = [1.0, 0.0, 0.0, 0.0]
GREEN = [0.0, 1.0, 0.0, 0.0]


class ScriptedEmbedder:
    model_id = scheme.IMAGE_MODEL
    dimension = 4

    def embed_image(self, path: object) -> list[float]:
        return list(RED if Path(str(path)).read_bytes() == _RED_JPEG else GREEN)

    def embed_query(self, text: str) -> list[float]:
        return list(RED if text.strip() == "红色" else GREEN)


def jpeg(color: str) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), color).save(buffer, format="JPEG")
    return buffer.getvalue()


_RED_JPEG = jpeg("red")


@pytest.fixture
def candidate_client(isolated_app) -> TestClient:
    """脚本化嵌入器 + 一份「检索后正文收尾」的模型剧本。"""
    app = isolated_app
    app.state.container = dataclasses.replace(
        app.state.container,
        visual_embedder=ScriptedEmbedder(),
        chat_model=ScriptedChatModel(
            [
                tool_call("search_memories", "c1", query="红色"),
                final("找到了两张。"),
            ]
        ),
    )
    return TestClient(app)


TERMINAL_STATUSES = {"complete", "partial", "failed"}


def poll_until_terminal(client: TestClient, turn_id: str, *, timeout_s: float = 20.0) -> dict:
    """轮询到回合终态。**回合跑在一条真工作线程里**（``start_turn`` 起线程），
    202 响应回来时它多半还在跑——这是「先落库再执行」那个设计的必然形状。"""
    deadline = time.monotonic() + timeout_s
    body: dict = {}
    while time.monotonic() < deadline:
        body = client.get(f"/api/v1/assistant/turns/{turn_id}").json()
        if body["status"] in TERMINAL_STATUSES:
            return body
        time.sleep(0.05)
    raise AssertionError(f"回合没有在 {timeout_s}s 内结束：{body}")


def import_photo(client: TestClient, payload: bytes, scope_id: str = "album1") -> str:
    return client.post(
        "/api/v1/media/import",
        files={"file": ("a.jpg", payload, "image/jpeg")},
        data={"scope_id": scope_id},
    ).json()["asset_id"]


def test_a_turn_exposes_the_retrieved_candidates(candidate_client: TestClient):
    """终态载荷里要能看到**检索抓回来的那批候选**，与交付的那批分开。

    这是一次**有意放宽契约**。2.0 原先不暴露候选集，PhotoBench 只能把
    ``retrieval_media_refs`` 直接赋成 GT——分母是 GT、分子是交付集，于是 394 道零交付的
    题 recall 一道不差全是 0.0，**分不出「检索没找到」与「没交付」**。
    """
    first = import_photo(candidate_client, _RED_JPEG)
    second = import_photo(candidate_client, jpeg("green"))
    conversation = make_conversation(candidate_client)
    turn = start_turn(candidate_client, conversation["id"], message="相册里红色的照片")

    body = poll_until_terminal(candidate_client, turn["turn_id"])

    # 正文收尾 → 交付那一格空着 → ``answered_with_gap``。状态是 ``partial`` 而不是
    # ``complete`` 是对的：「答了，但声明的交付没做到」。
    assert body["status"] == "partial"
    assert body["result"]["retrieved_candidates"] == [first, second]
    # 一句正文收尾、没有交付——**候选有、交付空**，这正是要能分辨的那种回合。
    assert body["result"]["attachments"] == []


def test_a_turn_without_retrieval_exposes_no_candidates(candidate_client: TestClient):
    """反向：没检索过就是空数组，不是把交付集填进去。

    这条守的是那个错法本身——「拿交付集冒充候选集」会让这里落成非空，测试当场红。
    """
    import_photo(candidate_client, _RED_JPEG)
    conversation = make_conversation(candidate_client)
    candidate_client.app.state.container = dataclasses.replace(
        candidate_client.app.state.container,
        chat_model=ScriptedChatModel([final("这是一句回答。")]),
    )
    turn = start_turn(candidate_client, conversation["id"], message="你好")

    body = poll_until_terminal(candidate_client, turn["turn_id"])

    assert body["result"]["retrieved_candidates"] == []


def test_include_debug_puts_the_exact_model_calls_in_the_turn_trace(client: TestClient):
    """``include_debug=true`` 时终态载荷里的 ``trace.debug_calls`` 带完整输入输出。

    这是 PhotoBench 8772 的唯一数据来源；开关关闭时它必须是空表，不能变成
    「永远采集」——那会把产品对话的提示词写进 ``agent_turns``。
    """
    conversation = make_conversation(client)
    client.app.state.container = dataclasses.replace(
        client.app.state.container,
        chat_model=ScriptedChatModel([final("你好。")]),
    )
    response = client.post(
        f"/api/v1/assistant/conversations/{conversation['id']}/turns",
        json={"message": "你好", "include_debug": True},
    )
    assert response.status_code == 202, response.text

    body = poll_until_terminal(client, response.json()["turn_id"])

    trace = body["result"]["trace"]
    assert trace["include_debug"] is True
    calls = trace["debug_calls"]
    assert [call["call_type"] for call in calls] == ["planner", "agent"]
    assert calls[0]["messages"][1]["content"] == "你好"
    assert calls[1]["messages"][0]["role"] == "system"
    assert calls[1]["response"]["content"] == "你好。"


def test_the_debug_trace_is_empty_without_include_debug(client: TestClient):
    conversation = make_conversation(client)
    client.app.state.container = dataclasses.replace(
        client.app.state.container,
        chat_model=ScriptedChatModel([final("你好。")]),
    )
    turn = start_turn(client, conversation["id"], message="你好")

    body = poll_until_terminal(client, turn["turn_id"])

    trace = body["result"]["trace"]
    assert trace["include_debug"] is False
    assert trace["debug_calls"] == []
