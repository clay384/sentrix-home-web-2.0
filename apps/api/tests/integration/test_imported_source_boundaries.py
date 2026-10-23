"""Synthetic HTTP and SQLite cases for source-boundary failure contracts."""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from sentrix.api.routes.assistant import _run_turn
from sentrix.api.routes.media import build_import_assets, to_summary
from sentrix.application.assistant.run_turn import RunTurn, TurnRequest
from sentrix.application.media.import_assets import ImportRequest, StagedImport
from sentrix.application.retrieval.retrieve_evidence import EvidenceHit, EvidenceResult
from sentrix.bootstrap.container import MIGRATIONS_DIR
from sentrix.domain.assistant.models import ChatRole, ModelReply, ToolCall
from sentrix.domain.assistant.result_set import ResultSet
from sentrix.infrastructure.persistence.database import Database
from sentrix.infrastructure.persistence.repositories.assets import (
    SqliteAssetRepository,
    SqliteObservationRepository,
)

NOW = datetime(2020, 1, 2, tzinfo=UTC)
SCOPE = "synthetic-source-boundaries"


def upload(client, *, content=b"Original synthetic source.", filename="original.txt", **fields):
    return client.post(
        "/api/v1/media/import",
        files={"file": (filename, content, "text/plain")},
        data={"scope_id": SCOPE, "external_source_id": "source-one", **fields},
    )


def source_fields():
    return {
        "source_timestamp": "2020-01-02 03:04:05",
        "source_location": "Original source location",
        "source_metadata": '{"order":1,"subject":"Synthetic subject"}',
    }


def result_handle(container, asset_id):
    result = ResultSet(
        id="synthetic-result", scope_id=SCOPE, query="Original source", asset_ids=(asset_id,), created_at=NOW
    )
    container.result_set_repository.add(result)
    return {"result_set_id": result.id, "asset_handle": result.page(1).entries[0].handle}


@pytest.mark.parametrize(
    "changes",
    [
        {"source_timestamp": "2020-01-03 03:04:05"},
        {"source_timestamp": "2020-01-02T03:04:05"},
        {"source_location": "Different source location"},
        {"source_metadata": '{"order":2,"subject":"Synthetic subject"}'},
        {"filename": "renamed.txt"},
    ],
)
def test_http_source_metadata_conflicts_preserve_existing_asset(client, changes):
    first = upload(client, **source_fields())
    assert first.status_code == 201
    before = client.get("/api/v1/assets/" + first.json()["asset_id"]).json()
    conflict = upload(client, **{**source_fields(), **changes})
    assert conflict.status_code == 409 and conflict.json()["code"] == "external_source_conflict"
    assert client.get("/api/v1/assets/" + first.json()["asset_id"]).json() == before
    assert len(client.get("/api/v1/assets", params={"scope_id": SCOPE}).json()["items"]) == 1


def test_http_metadata_json_key_order_does_not_create_false_conflict(client):
    first = upload(client, **source_fields())
    second = upload(
        client, **{**source_fields(), "source_metadata": '{"subject":"Synthetic subject","order":1}'}
    )
    assert first.status_code == second.status_code == 201
    assert second.json()["deduplicated"] and second.json()["asset_id"] == first.json()["asset_id"]


@pytest.mark.parametrize("identity", [" ", "\t", " \n "])
def test_http_whitespace_source_identity_is_rejected_before_import(client, identity):
    response = upload(client, external_source_id=identity)
    assert response.status_code == 400
    assert client.get("/api/v1/assets", params={"scope_id": SCOPE}).json()["items"] == []


def test_http_same_source_identity_in_different_scopes_is_independent(client):
    first = upload(client)
    second = upload(client, scope_id="different-source-scope")
    assert first.status_code == second.status_code == 201
    assert first.json()["asset_id"] != second.json()["asset_id"]
    original = client.get("/api/v1/assets", params={"scope_id": SCOPE}).json()["items"]
    other = client.get("/api/v1/assets", params={"scope_id": "different-source-scope"}).json()["items"]
    assert [row["id"] for row in original] == [first.json()["asset_id"]]
    assert [row["id"] for row in other] == [second.json()["asset_id"]]


@pytest.mark.parametrize("content", [b"\xff\xfeinvalid-utf8", b" \n\t "])
def test_unreadable_or_blank_text_never_becomes_supported_source_evidence(client, content):
    response = upload(client, content=content)
    assert response.status_code == 201
    container = client.app.state.container
    asset_id = response.json()["asset_id"]
    assert container.media_storage.read(asset_id) == content
    args = result_handle(container, asset_id)
    toolbox = _run_turn(container)._toolbox
    first = toolbox.execute("read_source_text", args, scope_id=SCOPE, now=NOW)
    second = toolbox.execute("read_source_text", args, scope_id=SCOPE, now=NOW)
    assert first.failure_reason == second.failure_reason == "source_text_unavailable"
    assert not first.ok and "full_text" not in first.model_visible
    assert not second.ok and "full_text" not in second.model_visible


def test_staged_source_without_observation_is_not_read_from_uncommitted_raw_file(client):
    container = client.app.state.container
    staged = build_import_assets(container, observe=False).stage(
        ImportRequest(
            scope_id=SCOPE,
            file_name="uncommitted.txt",
            content_type="text/plain",
            content=b"Raw archived bytes are not a committed source observation.",
            external_source_id="uncommitted-source",
        )
    )
    assert isinstance(staged, StagedImport)
    assert container.observation_repository.list_by_asset(staged.asset.id) == []
    args = result_handle(container, staged.asset.id)
    outcome = _run_turn(container)._toolbox.execute("read_source_text", args, scope_id=SCOPE, now=NOW)
    assert outcome.failure_reason == "source_text_unavailable" and "full_text" not in outcome.model_visible


def test_long_unicode_text_preserves_original_storage_and_complete_read(client):
    text = "原始记录开始\n" + ("中文和 Unicode 🌿 with line endings.\r\n" * 2400) + "最后一个来源字符：终\n"
    assert len(text) > 20000
    response = upload(client, content=text.encode())
    assert response.status_code == 201
    container = client.app.state.container
    asset_id = response.json()["asset_id"]
    assert container.media_storage.read(asset_id) == text.encode()
    assert container.observation_repository.list_by_asset(asset_id)[0].source_text == text
    outcome = _run_turn(container)._toolbox.execute(
        "read_source_text", result_handle(container, asset_id), scope_id=SCOPE, now=NOW
    )
    assert outcome.ok and outcome.model_visible["full_text"] == text


def test_pure_text_turn_cannot_read_a_real_previous_turn_result_set(client):
    current = upload(client, content=b"Current retrieved source only.")
    previous = upload(
        client,
        content=b"PREVIOUS_ONLY_BODY must not be read through a different turn.",
        external_source_id="previous-source",
    )
    assert current.status_code == previous.status_code == 201
    container = client.app.state.container
    previous_result = ResultSet(
        id="previous-turn-result",
        scope_id=SCOPE,
        query="Previous question",
        asset_ids=(previous.json()["asset_id"],),
        created_at=NOW,
    )
    container.result_set_repository.add(previous_result)

    class AttemptPreviousResult:
        def __init__(self):
            self.messages = []

        def complete(self, messages, tools):
            self.messages.extend(messages)
            if "declare" in messages[0].content:
                return ModelReply(
                    content=json.dumps(
                        {
                            "action": "declare",
                            "declaration": {
                                "goal": "Read current text",
                                "requirements": [
                                    {
                                        "id": "source",
                                        "evidence_type": "source_text",
                                        "description": "Current source body",
                                    },
                                ],
                            },
                        }
                    ),
                    finish_reason="stop",
                )
            if "事实一致性评审" in messages[0].content:
                return ModelReply(content='{"faithful":true,"problems":[]}', finish_reason="stop")
            outcomes = [json.loads(message.content) for message in messages if message.role is ChatRole.TOOL]
            if not outcomes:
                name, arguments = "search_memories", {"query": "Current source"}
            elif len(outcomes) == 1:
                name, arguments = (
                    "read_source_text",
                    {
                        "result_set_id": previous_result.id,
                        "asset_handle": previous_result.page(1).entries[0].handle,
                    },
                )
            else:
                name, arguments = (
                    "final_answer",
                    {"answer": "Current source evidence is insufficient.", "selected_image_handles": []},
                )
            return ModelReply(
                content="",
                finish_reason="tool_calls",
                tool_calls=(
                    ToolCall(
                        call_id="call-" + str(len(outcomes)), name=name, arguments_json=json.dumps(arguments)
                    ),
                ),
            )

    model = AttemptPreviousResult()
    run = _run_turn(replace(container, chat_model=model))
    run._toolbox._retrieval = SimpleNamespace(
        execute=lambda request: EvidenceResult(
            hits=(EvidenceHit(asset_id=current.json()["asset_id"], score=1.0),),
        )
    )
    assert run._toolbox.is_text_scope(SCOPE)
    assert not run._toolbox.is_mixed_scope(SCOPE)
    conversation = container.conversation_repository.create(
        scope_id=SCOPE, title="Current text turn", created_at=NOW
    )
    turn = run.start(TurnRequest(conversation_id=conversation.id, question="Read the current source."))
    run.run(turn.id)
    tool_results = [
        json.loads(message.content) for message in model.messages if message.role is ChatRole.TOOL
    ]
    assert any(result.get("error") == "result_set_mismatch" for result in tool_results)
    assert not any("full_text" in result for result in tool_results)
    assert all("PREVIOUS_ONLY_BODY" not in message.content for message in model.messages)


def test_explicit_empty_allowlist_hides_even_a_valid_result_handle(client, tmp_path):
    imported = upload(client)
    assert imported.status_code == 201
    original = client.app.state.container
    args = result_handle(original, imported.json()["asset_id"])
    path = tmp_path / "published-sources.json"
    path.write_text('{"source_ids":[]}')
    container = replace(
        original, settings=original.settings.model_copy(update={"corpus_source_allowlist": path})
    )
    toolbox = _run_turn(container)._toolbox
    denied = toolbox.execute("read_source_text", args, scope_id=SCOPE, now=NOW)
    assert denied.failure_reason == "source_not_frozen" and "full_text" not in denied.model_visible
    search = toolbox.execute(
        "search_memories", {"query": "Original synthetic source"}, scope_id=SCOPE, now=NOW
    )
    assert search.model_visible.get("preview", []) == []


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "not json",
        "[]",
        "{}",
        '{"source_ids":null}',
        '{"source_ids":[null]}',
        '{"source_ids":[""]}',
        '{"source_ids":"source-one"}',
    ],
)
def test_missing_or_malformed_allowlist_never_falls_back_to_unrestricted_reads(client, tmp_path, payload):
    path = tmp_path / "published-sources.json"
    if payload is not None:
        path.write_text(payload)
    original = client.app.state.container
    container = replace(
        original, settings=original.settings.model_copy(update={"corpus_source_allowlist": path})
    )
    with pytest.raises((OSError, ValueError)):
        _run_turn(container)


def test_read_only_mode_blocks_every_registered_write_except_session_and_turn_creation(
    isolated_app, monkeypatch
):
    import sentrix.main as main

    original = isolated_app.state.container
    frozen_settings = original.settings.model_copy(update={"corpus_read_only": True})
    monkeypatch.setattr(
        main.Container, "build", classmethod(lambda cls, settings: replace(original, settings=settings))
    )
    monkeypatch.setattr(RunTurn, "run", lambda *args, **kwargs: None)
    app = main.create_app(frozen_settings)
    allowed = {
        ("POST", "/api/v1/assistant/conversations"),
        ("POST", "/api/v1/assistant/conversations/{conversation_id}/turns"),
    }
    writes = {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        for method in operations
        if method in {"post", "put", "patch", "delete"} and (method.upper(), path) not in allowed
    }
    assert ("POST", "/api/v1/media/import") in writes and len(writes) > 20
    client = TestClient(app)
    for method, template in sorted(writes):
        path = re.sub(r"\{[^}]+\}", "synthetic-missing-id", template)
        response = client.request(method, path)
        assert response.status_code == 403, (method, path, response.text)
        assert response.json()["code"] == "corpus_read_only"
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/v1/assets").status_code == 200
    conversation = client.post("/api/v1/assistant/conversations", json={"scope_id": SCOPE})
    assert conversation.status_code == 201
    conversation_id = conversation.json()["id"]
    started = client.post(
        f"/api/v1/assistant/conversations/{conversation_id}/turns", json={"message": "Read synthetic source"}
    )
    assert started.status_code == 202
    assert client.get(f"/api/v1/assistant/conversations/{conversation_id}").status_code == 200
    assert client.get(f"/api/v1/assistant/turns/{started.json()['turn_id']}").status_code == 200


def test_upgrade_preserves_old_rows_and_initializes_optional_source_fields(tmp_path):
    older = tmp_path / "old-migrations"
    older.mkdir()
    for path in MIGRATIONS_DIR.glob("*.sql"):
        if int(path.name.split("_", 1)[0]) < 14:
            shutil.copyfile(path, older / path.name)
    db_path = tmp_path / "legacy.db"
    database = Database(db_path, older)
    database.migrate()
    with database.connect() as connection, connection:
        connection.execute(
            "INSERT INTO assets(id,scope_id,file_name,media_type,path,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            ("old-source", SCOPE, "legacy.txt", "text", "legacy.txt", NOW.isoformat(), NOW.isoformat()),
        )
        connection.execute(
            "INSERT INTO observations(id,scope_id,asset_id,source_type,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            ("old-observation", SCOPE, "old-source", "import", NOW.isoformat(), NOW.isoformat()),
        )
    upgraded = Database(db_path, MIGRATIONS_DIR)
    upgraded.migrate()
    upgraded.migrate()
    asset = SqliteAssetRepository(upgraded).get("old-source")
    observation = SqliteObservationRepository(upgraded).get("old-observation")
    assert asset is not None and observation is not None
    summary = to_summary(asset).model_dump()
    assert summary["external_source_id"] is None and summary["source_timestamp"] is None
    assert summary["source_location"] == "" and summary["source_metadata"] == {}
    assert asset.file_name == "legacy.txt" and observation.source_text == ""


def test_http_empty_source_identity_form_keeps_legacy_upload_semantics(client):
    first = upload(client, external_source_id="")
    second = upload(client, external_source_id="")
    assert first.status_code == second.status_code == 201
    assert second.json()["asset_id"] == first.json()["asset_id"]
    assert second.json()["deduplicated"] is True
    summary = client.get("/api/v1/assets/" + first.json()["asset_id"]).json()
    assert summary["external_source_id"] is None
