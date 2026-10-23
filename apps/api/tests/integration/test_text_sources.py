"""Text assets use the existing retrieval path and preserve source boundaries."""

from datetime import UTC, datetime

from sentrix.api.routes.assistant import _run_turn
from sentrix.domain.assistant.evidence_ledger import EvidenceLedger, EvidenceType
from sentrix.domain.assistant.intent import build_prompt, parse_declaration
from sentrix.domain.assistant.jit import select_tools


def upload(client, scope, text):
    response = client.post(
        "/api/v1/media/import",
        data={"scope_id": scope},
        files={"file": ("source.txt", text.encode(), "text/plain")},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_full_source_roundtrip_index_and_idempotence(client):
    text = "原始档案\n蓝色文件夹保存了完整会议记录。\n" + "Full original source.\n" * 100
    first = upload(client, "text-a", text)
    second = upload(client, "text-a", text)
    assert second["asset_id"] == first["asset_id"]
    assert second["deduplicated"] is True
    container = client.app.state.container
    rows = container.observation_repository.list_by_asset(first["asset_id"])
    assert len(rows) == 1
    assert rows[0].source_text == text
    assert rows[0].ocr_text == ""
    assert rows[0].caption == ""
    assert container.media_storage.read(first["asset_id"]).decode() == text
    with container.database.connect() as db:
        vectors = db.execute("SELECT count(*) FROM memory_vectors WHERE scope_id='text-a'").fetchone()[0]
        terms = db.execute(
            "SELECT count(*) FROM observation_search_terms "
            "WHERE scope_id='text-a' AND field_type='source_text'"
        ).fetchone()[0]
    assert vectors >= 2
    assert terms > 0


def test_read_source_requires_same_scope_and_result_handle(client):
    text = "蓝色文件夹中只有会议原文，不是照片或OCR。"
    upload(client, "text-a", text)
    upload(client, "text-b", "另一个范围的保密原文")
    toolbox = _run_turn(client.app.state.container)._toolbox
    assert toolbox.is_text_scope("text-a")
    found = toolbox.execute(
        "search_memories", {"query": "蓝色文件夹"}, scope_id="text-a", now=datetime.now(UTC)
    )
    result_id = found.model_visible["result_set_id"]
    handle = found.model_visible["preview"][0]["handle"]
    args = {"result_set_id": result_id, "asset_handle": handle}
    read = toolbox.execute("read_source_text", args, scope_id="text-a", now=datetime.now(UTC))
    assert read.ok and read.model_visible["full_text"] == text
    assert read.model_visible["source"] == "imported_source_text"
    cross = toolbox.execute("read_source_text", args, scope_id="text-b", now=datetime.now(UTC))
    assert cross.failure_reason == "scope_mismatch"
    assert "full_text" not in cross.model_visible
    bad = toolbox.execute(
        "read_source_text", {**args, "asset_handle": "invalid"}, scope_id="text-a", now=datetime.now(UTC)
    )
    assert bad.failure_reason == "bad_handle"


def test_text_evidence_has_no_photo_delivery_requirement():
    raw = (
        '{"action":"declare","declaration":{"goal":"读取资料","requirements":'
        '[{"id":"r","evidence_type":"source_text","description":"原文"}]}}'
    )
    requirements, reason = parse_declaration(raw, scope_id="text-a", question="读取资料")
    assert not reason and requirements is not None
    ledger = EvidenceLedger(scope_id="text-a", requirements=list(requirements))
    before = {x.name for x in select_tools(ledger, has_result_set=False, tools_exhausted=False)}
    after = {x.name for x in select_tools(ledger, has_result_set=True, tools_exhausted=False)}
    assert "search_memories" in before and "read_source_text" not in before
    assert "read_source_text" in after
    ledger.satisfy(EvidenceType.SOURCE_TEXT, call_id="read-1")
    assert ledger.is_complete
    assert all(x.evidence_type is not EvidenceType.DELIVERED_PHOTO for x in ledger.requirements)
    assert "source_text" in build_prompt(text_mode=True)


def test_long_question_is_accepted_without_truncation():
    from sentrix.api.schemas.assistant import StartTurnRequest

    question = "A long source question. " * 150
    assert StartTurnRequest(message=question).message == question


def test_legacy_photo_planner_keeps_original_registry():
    assert "source_text" not in build_prompt()


def test_read_only_corpus_denies_import_but_keeps_conversations(settings):
    from fastapi.testclient import TestClient

    from sentrix.main import create_app

    app = create_app(settings.model_copy(update={"corpus_read_only": True}))
    client = TestClient(app)
    denied = client.post("/api/v1/media/import", files={"file": ("file.txt", b"test", "text/plain")})
    assert denied.status_code == 403
    assert denied.json()["code"] == "corpus_read_only"
    conversation = client.post("/api/v1/assistant/conversations", json={"scope_id": "synthetic"})
    assert conversation.status_code == 201


def test_explicit_cloud_output_budget_preserves_legacy_default():
    from sentrix.domain.assistant.models import ChatMessage, ChatRole
    from sentrix.infrastructure.assistant.chat_client import OpenAIChatModel
    from sentrix.infrastructure.assistant.model_endpoint import ServedModel

    served = ServedModel("http://example.invalid/v1")
    messages = [ChatMessage(ChatRole.SYSTEM, "Synthetic"), ChatMessage(ChatRole.USER, "Hello")]
    default = OpenAIChatModel("http://example.invalid/v1", served, context_tokens=147456)
    cloud = OpenAIChatModel("http://example.invalid/v1", served, context_tokens=163840, output_tokens=32768)
    assert default._max_tokens(messages) == 2048
    assert cloud._max_tokens(messages) == 32768


def test_text_source_completes_native_loop_and_l2_without_image(client):
    import dataclasses
    import json
    import time

    from sentrix.domain.assistant.models import ChatRole, ModelReply, ToolCall

    class TextModel:
        def complete(self, messages, tools):
            system = messages[0].content
            if "declare" in system:
                return ModelReply(
                    content=json.dumps(
                        {
                            "action": "declare",
                            "declaration": {
                                "goal": "查阅会议记录",
                                "requirements": [
                                    {"id": "r1", "evidence_type": "source_text", "description": "会议原文"}
                                ],
                            },
                        },
                        ensure_ascii=False,
                    ),
                    finish_reason="stop",
                )
            if "事实一致性评审" in system:
                return ModelReply(
                    content='{"faithful":true,"problems":[],"reason":"有原文"}', finish_reason="stop"
                )
            results = [json.loads(x.content) for x in messages if x.role is ChatRole.TOOL]
            if not results:
                name, args = "search_memories", {"query": "蓝色文件夹"}
            elif not any("full_text" in x for x in results):
                found = results[0]
                name, args = (
                    "read_source_text",
                    {"result_set_id": found["result_set_id"], "asset_handle": found["preview"][0]["handle"]},
                )
            else:
                name, args = (
                    "final_answer",
                    {"answer": "蓝色文件夹里保存会议记录。", "selected_image_handles": []},
                )
            return ModelReply(
                content="",
                finish_reason="tool_calls",
                tool_calls=(
                    ToolCall(call_id="c" + str(len(results)), name=name, arguments_json=json.dumps(args)),
                ),
            )

    upload(client, "synthetic-text", "蓝色文件夹里保存会议记录。")
    client.app.state.container = dataclasses.replace(client.app.state.container, chat_model=TextModel())
    conversation = client.post("/api/v1/assistant/conversations", json={"scope_id": "synthetic-text"}).json()
    started = client.post(
        f"/api/v1/assistant/conversations/{conversation['id']}/turns",
        json={"message": "蓝色文件夹里保存了什么？"},
    ).json()
    for _ in range(100):
        result = client.get(f"/api/v1/assistant/turns/{started['turn_id']}").json()
        if result["status"] not in {"pending", "running"}:
            break
        time.sleep(0.02)
    assert result["status"] == "complete", result
    assert result["result"]["answer"] == "蓝色文件夹里保存会议记录。"
    assert result["result"]["termination_reason"] == "answered"
