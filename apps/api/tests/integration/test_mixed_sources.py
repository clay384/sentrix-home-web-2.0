"""Mixed source turns retain native planning, tools and review without source leakage."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from sentrix.application.assistant.run_turn import MIXED_SYSTEM_PROMPT, RunTurn, TurnRequest
from sentrix.application.assistant.tools import AssistantToolbox
from sentrix.application.retrieval.retrieve_evidence import EvidenceHit, EvidenceResult
from sentrix.domain.assistant.evidence_ledger import EvidenceLedger, EvidenceRequirement, EvidenceType
from sentrix.domain.assistant.intent import build_prompt
from sentrix.domain.assistant.jit import select_tools
from sentrix.domain.assistant.models import ChatRole, ModelReply, ToolCall
from sentrix.domain.assistant.result_set import ResultSet
from sentrix.domain.media.models import Asset, AssetStatus, MediaType, Observation
from sentrix.infrastructure.persistence.repositories.assets import (
    SqliteAssetRepository,
    SqliteObservationRepository,
)
from sentrix.infrastructure.persistence.repositories.conversations import SqliteConversationRepository
from sentrix.infrastructure.persistence.repositories.faces import SqliteFaceRepository
from sentrix.infrastructure.persistence.repositories.result_sets import SqliteResultSetRepository
from sentrix.infrastructure.persistence.repositories.turns import SqliteTurnRepository

NOW = datetime(2026, 1, 2, tzinfo=UTC)
SCOPE = "mixed"
SOURCE_TIME = "2017-06-03 09:08:07"


class Inspector:
    def __init__(self):
        self.calls = []

    def inspect(self, path, question, **kwargs):
        self.calls.append((path, question))
        return {"observation": "照片中的物品是蓝色的。", "certainty": "supported"}


@pytest.fixture
def mixed(migrations_ready_db, tmp_path):
    assets = SqliteAssetRepository(migrations_ready_db)
    observations = SqliteObservationRepository(migrations_ready_db)
    for asset_id, media_type, scope_id in (
        ("photo", MediaType.IMAGE, SCOPE),
        ("text", MediaType.TEXT, SCOPE),
        ("unpublished", MediaType.IMAGE, SCOPE),
        ("foreign", MediaType.IMAGE, "other"),
    ):
        path = tmp_path / asset_id
        path.write_bytes(asset_id.encode())
        assets.add(
            Asset(
                id=asset_id,
                scope_id=scope_id,
                file_name=asset_id,
                media_type=media_type,
                path=str(path),
                mime_type="text/plain" if media_type is MediaType.TEXT else "image/jpeg",
                size_bytes=path.stat().st_size,
                status=AssetStatus.PROCESSED,
                content_sha256=asset_id,
                captured_at=datetime.fromisoformat(SOURCE_TIME),
                captured_location=None,
                created_at=NOW,
                updated_at=NOW,
                external_source_id="source-" + asset_id,
                source_timestamp=SOURCE_TIME,
                source_location="资料中的原始地点",
            )
        )
        observations.add(
            Observation(
                id="obs-" + asset_id,
                scope_id=scope_id,
                asset_id=asset_id,
                source_type="import",
                captured_at=datetime.fromisoformat(SOURCE_TIME),
                confidence=1.0,
                created_at=NOW,
                updated_at=NOW,
                caption="蓝色物品" if media_type is MediaType.IMAGE else "",
                source_text="会议原文：蓝色物品的日期是2017年6月3日。"
                if media_type is MediaType.TEXT
                else "",
            )
        )
    inspector = Inspector()
    result_sets = SqliteResultSetRepository(migrations_ready_db)
    retrieval = SimpleNamespace(
        execute=lambda request: EvidenceResult(
            hits=tuple(
                EvidenceHit(asset_id=asset_id, score=1.0)
                for asset_id in ("photo", "text", "unpublished", "foreign")
            )
        )
    )
    toolbox = AssistantToolbox(
        retrieval=retrieval,
        assets=assets,
        result_sets=result_sets,
        people=SimpleNamespace(lookup=lambda *args: None),
        memory_facts=None,
        faces=SqliteFaceRepository(migrations_ready_db),
        observations=observations,
        photo_inspector=inspector,
        person_crops=None,
        frozen_source_ids={"source-photo", "source-text"},
    )
    return SimpleNamespace(
        toolbox=toolbox,
        assets=assets,
        result_sets=result_sets,
        inspector=inspector,
        db=migrations_ready_db,
    )


def execute(mixed, name, **arguments):
    return mixed.toolbox.execute(name, arguments, scope_id=SCOPE, now=NOW)


def test_search_and_reads_preserve_neutral_source_metadata_and_all_modalities(mixed):
    result = execute(mixed, "search_memories", query="蓝色物品")
    preview = result.model_visible["preview"]
    assert {item["media_kind"] for item in preview} == {"image", "text"}
    assert {item["external_source_id"] for item in preview} == {"source-photo", "source-text"}
    assert all(item["source_timestamp"] == SOURCE_TIME for item in preview)
    assert all(item["source_location"] == "资料中的原始地点" for item in preview)
    assert mixed.toolbox.is_mixed_scope(SCOPE) and not mixed.toolbox.is_text_scope(SCOPE)
    text = next(item for item in preview if item["media_kind"] == "text")
    photo = next(item for item in preview if item["media_kind"] == "image")
    result_set_id = result.model_visible["result_set_id"]
    read = execute(mixed, "read_source_text", result_set_id=result_set_id, asset_handle=text["handle"])
    inspected = execute(mixed, "inspect_photo", result_set_id=result_set_id, asset_handle=photo["handle"])
    assert read.ok and read.model_visible["full_text"] == "会议原文：蓝色物品的日期是2017年6月3日。"
    for outcome, source_id in ((read, "source-text"), (inspected, "source-photo")):
        assert outcome.ok
        assert outcome.model_visible["external_source_id"] == source_id
        assert outcome.model_visible["source_timestamp"] == SOURCE_TIME
        assert outcome.model_visible["source_location"] == "资料中的原始地点"
        assert "asset_id" not in outcome.model_visible


@pytest.mark.parametrize(
    "tool", ["inspect_photo", "read_photo_text", "query_photo_people", "read_source_text", "get_result_page"]
)
@pytest.mark.parametrize(
    ("asset_id", "reason"), [("foreign", "scope_mismatch"), ("unpublished", "source_not_frozen")]
)
def test_forged_result_cannot_expose_cross_scope_or_unpublished_source(mixed, tool, asset_id, reason):
    mixed.result_sets.add(
        ResultSet(id="forged", scope_id=SCOPE, query="x", asset_ids=(asset_id,), created_at=NOW)
    )
    outcome = execute(mixed, tool, result_set_id="forged", asset_handle="photo_1")
    assert outcome.failure_reason == reason
    assert "full_text" not in outcome.model_visible
    assert "preview" not in outcome.model_visible
    assert not mixed.inspector.calls


@pytest.mark.parametrize(
    ("tool", "asset_id"),
    [("inspect_photo", "text"), ("read_photo_text", "text"), ("read_source_text", "photo")],
)
def test_wrong_media_is_rejected_before_file_or_model_access(mixed, tool, asset_id):
    mixed.result_sets.add(
        ResultSet(id="wrong-kind", scope_id=SCOPE, query="x", asset_ids=(asset_id,), created_at=NOW)
    )
    outcome = execute(mixed, tool, result_set_id="wrong-kind", asset_handle="photo_1")
    assert outcome.failure_reason == "unsupported_media_type"
    assert not mixed.inspector.calls


def test_mixed_jit_preserves_complementary_inspection_after_requirement_is_satisfied():
    ledger = EvidenceLedger(
        scope_id=SCOPE,
        requirements=[
            EvidenceRequirement(
                id="r",
                evidence_type=EvidenceType.SOURCE_TEXT,
                description="原文",
            )
        ],
    )
    ledger.satisfy(EvidenceType.SOURCE_TEXT, call_id="read")
    before = select_tools(ledger, has_result_set=False, tools_exhausted=False, mixed_mode=True)
    after = select_tools(ledger, has_result_set=True, tools_exhausted=False, mixed_mode=True)
    assert not {"inspect_photo", "read_source_text"} & {item.name for item in before}
    assert {"inspect_photo", "read_source_text", "read_photo_text"} <= {item.name for item in after}
    assert select_tools(ledger, has_result_set=True, tools_exhausted=True, mixed_mode=True) == ()
    assert all(len(item.description) <= 40 for item in after)
    final = next(item for item in after if item.name == "final_answer")
    assert "用户要求" in final.parameters["properties"]["answer"]["description"]
    assert "source_text" in build_prompt(mixed_mode=True)
    assert "source_text" not in build_prompt()


class MixedModel:
    def __init__(self, *, steal_result_set=False):
        self.calls = []
        self.steal_result_set = steal_result_set

    def complete(self, messages, tools):
        self.calls.append((list(messages), tuple(tools)))
        system = messages[0].content
        if "declare" in system:
            assert "混合资料" in system
            return ModelReply(
                content=json.dumps(
                    {
                        "action": "declare",
                        "declaration": {
                            "goal": "核对物品与日期",
                            "requirements": [
                                {"id": "r1", "evidence_type": "source_text", "description": "日期原文"},
                                {
                                    "id": "r2",
                                    "evidence_type": "visual_observation",
                                    "description": "物品颜色",
                                },
                            ],
                        },
                    }
                ),
                finish_reason="stop",
            )
        if "事实一致性评审" in system:
            return ModelReply(
                content='{"faithful":true,"problems":[],"reason":"原文与画面支持"}', finish_reason="stop"
            )
        assert system == MIXED_SYSTEM_PROMPT
        results = [json.loads(item.content) for item in messages if item.role is ChatRole.TOOL]
        if not results:
            name, args = "search_memories", {"query": "蓝色物品"}
        elif not any("full_text" in item for item in results) and not any(
            "error" in item for item in results
        ):
            found = results[0]
            text = next(item for item in found["preview"] if item["media_kind"] == "text")
            name, args = (
                "read_source_text",
                {
                    "result_set_id": "another-turn" if self.steal_result_set else found["result_set_id"],
                    "asset_handle": text["handle"],
                },
            )
        elif not any("observation" in item for item in results) and not any(
            "error" in item for item in results
        ):
            found = results[0]
            photo = next(item for item in found["preview"] if item["media_kind"] == "image")
            name, args = (
                "inspect_photo",
                {
                    "result_set_id": found["result_set_id"],
                    "asset_handle": photo["handle"],
                    "question": "物品是什么颜色？",
                },
            )
        else:
            name, args = (
                "final_answer",
                {
                    "answer": "蓝色，2017年6月3日，来源source-photo和source-text。",
                    "selected_image_handles": [],
                },
            )
        assert name in {item.name for item in tools}
        return ModelReply(
            content="",
            finish_reason="tool_calls",
            tool_calls=(
                ToolCall(
                    call_id="c" + str(len(results)),
                    name=name,
                    arguments_json=json.dumps(args),
                ),
            ),
        )


def run_mixed_turn(mixed, model):
    conversations = SqliteConversationRepository(mixed.db)
    turns = SqliteTurnRepository(mixed.db)
    conversation = conversations.create(scope_id=SCOPE, title="mixed", created_at=NOW)
    run = RunTurn(model=model, conversations=conversations, turns=turns, toolbox=mixed.toolbox)
    question = "What color is the object, and what date is recorded?"
    record = run.start(TurnRequest(conversation_id=conversation.id, question=question))
    run.run(record.id)
    return turns.get(record.id), question


def test_native_mixed_loop_reads_both_sources_and_runs_l2_without_forced_attachments(mixed):
    model = MixedModel()
    record, question = run_mixed_turn(mixed, model)
    assert record.status.value == "complete", json.dumps(record.trace, ensure_ascii=False, indent=2)
    assert record.termination_reason == "answered"
    assert record.answer == "蓝色，2017年6月3日，来源source-photo和source-text。"
    assert record.attachments == ()
    assert len(mixed.inspector.calls) == 1
    assert sum("事实一致性评审" in call[0][0].content for call in model.calls) == 1
    assert model.calls[0][0][1].content == question
    assert model.calls[1][0][-1].content == question


def test_mixed_turn_cannot_read_another_turns_result_set(mixed):
    model = MixedModel(steal_result_set=True)
    run_mixed_turn(mixed, model)
    assert any(
        json.loads(message.content).get("error") == "result_set_mismatch"
        for messages, _ in model.calls
        for message in messages
        if message.role is ChatRole.TOOL
    )
    assert not mixed.inspector.calls


def test_allowlist_route_wiring_is_fail_closed(client, tmp_path):
    from dataclasses import replace

    from sentrix.api.routes.assistant import _run_turn

    path = tmp_path / "published.json"
    path.write_text('{"source_ids":["source-photo"]}')
    original = client.app.state.container
    container = replace(
        original, settings=original.settings.model_copy(update={"corpus_source_allowlist": path})
    )
    assert _run_turn(container)._toolbox._frozen_source_ids == {"source-photo"}
    path.write_text('{"source_ids":"source-photo"}')
    with pytest.raises(ValueError, match="source_ids"):
        _run_turn(container)
