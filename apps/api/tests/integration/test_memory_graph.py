"""结构化记忆图的构建、持久化和有限拓展。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import sentrix.api.routes.media as media_routes
from sentrix.application.memories.build_graph import BuildMemoryGraph
from sentrix.application.retrieval.retrieve_graph import RetrieveGraph, graph_direction, graph_intent
from sentrix.domain.media.models import Asset, AssetStatus, MediaType, Observation, ObservationFact
from sentrix.domain.memories.causality import CausalVerdict
from sentrix.domain.memories.events import EventEntityRecord, EventRecord
from sentrix.domain.memories.graph import GraphEdge, GraphExpansionHit, GraphNode, GraphStatus
from sentrix.domain.memories.ports import MemoryGraphRepository
from sentrix.domain.retrieval.ports import VectorRecord
from sentrix.infrastructure.persistence.repositories.memory_graph import (
    SqliteMemoryGraphRepository,
)

SCOPE = "graph-scope"
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


def test_sqlite_graph_replaces_scope_and_expands_assets(migrations_ready_db):
    repository = SqliteMemoryGraphRepository(migrations_ready_db)
    stats = repository.replace_scope(
        SCOPE,
        nodes=(
            GraphNode("asset:a", SCOPE, "asset", "a"),
            GraphNode("event:e", SCOPE, "event", "e"),
            GraphNode("asset:b", SCOPE, "asset", "b"),
        ),
        edges=(
            GraphEdge("edge:a", SCOPE, "asset:a", "event:e", "event_contains_asset"),
            GraphEdge("edge:b", SCOPE, "event:e", "asset:b", "event_contains_asset"),
        ),
        build_revision="revision-1",
    )

    assert stats.node_count == 3
    assert stats.edge_count == 2
    assert repository.status(SCOPE) == GraphStatus(
        scope_id=SCOPE,
        available=True,
        node_count=3,
        edge_count=2,
        build_revision="revision-1",
    )
    hits = repository.expand_assets(("a",), scope_id=SCOPE, max_hops=3, limit=10)
    assert [(hit.asset_id, hit.hops) for hit in hits] == [("b", 2)]

    repository.replace_scope(SCOPE, nodes=(), edges=(), build_revision="revision-2")
    assert repository.status(SCOPE).node_count == 0
    assert repository.expand_assets(("a",), scope_id=SCOPE, max_hops=3, limit=10) == ()


class _ListSource:
    def __init__(self, values):
        self.values = tuple(values)

    def list_by_scope(self, scope_id, *, limit):
        return self.values

    def list_scope(self, scope_id, *, limit):
        return self.values

    def list_entities(self, *, scope_id, status=None):
        return self.values

    def list_facts(self, scope_id, *, limit):
        return self.values

    def list_relationships(self, scope_id):
        return self.values


class _EventSource(_ListSource):
    def __init__(self, values, observations):
        super().__init__(values)
        self.observations = observations

    def observation_ids(self, event_id):
        return self.observations


class _ParticipantSource:
    def __init__(self, event_entities=()):
        self.event_entities = tuple(event_entities)

    def list_participants(self, event_id):
        return ()

    def list_event_entities(self, event_id):
        return self.event_entities


def _asset(asset_id: str, path: str, *, parent_asset_id: str | None = None) -> Asset:
    return Asset(
        id=asset_id,
        scope_id=SCOPE,
        file_name=f"{asset_id}.jpg",
        media_type=MediaType.IMAGE,
        path=path,
        mime_type="image/jpeg",
        size_bytes=1,
        status=AssetStatus.PROCESSED,
        content_sha256=f"sha-{asset_id}",
        captured_at=NOW,
        captured_location=None,
        created_at=NOW,
        updated_at=NOW,
        derived_kind="video_keyframe" if parent_asset_id else None,
        parent_asset_id=parent_asset_id,
        source_timestamp_sec=3.5 if parent_asset_id else None,
    )


def _observation(observation_id: str, asset_id: str) -> Observation:
    return Observation(
        id=observation_id,
        scope_id=SCOPE,
        asset_id=asset_id,
        source_type="import",
        captured_at=NOW,
        confidence=0.8,
        created_at=NOW,
        updated_at=NOW,
        caption="家庭活动",
        activity="聚会",
        objects=("蛋糕",),
    )


def test_build_graph_consumes_standard_memory_records_without_keyframes(migrations_ready_db):
    assets = (_asset("a", "/tmp/a.jpg"), _asset("b", "/tmp/b.jpg"))
    observations = (_observation("obs-a", "a"), _observation("obs-b", "b"))
    event = EventRecord(
        id="event-1",
        scope_id=SCOPE,
        title="聚会",
        event_type="家庭活动",
        time_start=NOW,
        time_end=NOW,
        place="家",
        admin_district="",
        activity="聚会",
        summary="家庭聚会",
        confidence=0.9,
        aggregation_score=0.9,
        aggregation_breakdown={},
        created_at=NOW,
    )
    graph = SqliteMemoryGraphRepository(migrations_ready_db)
    use_case = BuildMemoryGraph(
        assets=_ListSource(assets),
        observations=_ListSource(observations),
        events=_EventSource((event,), ("obs-a", "obs-b")),
        participants=_ParticipantSource(),
        entities=_ListSource(()),
        facts=_ListSource(()),
        relationships=_ListSource(()),
        graph=graph,
    )

    stats = use_case.execute(SCOPE)

    assert stats.node_count >= 6
    assert stats.edge_count >= 5
    assert graph.status(SCOPE).available
    assert [hit.asset_id for hit in graph.expand_assets(("a",), scope_id=SCOPE, max_hops=4, limit=10)] == [
        "b"
    ]


def test_build_graph_keeps_video_keyframe_lineage_as_a_traversable_edge(migrations_ready_db):
    assets = (_asset("video", "/tmp/video.mp4"), _asset("frame", "/tmp/frame.webp", parent_asset_id="video"))
    observations = (_observation("obs-frame", "frame"),)
    graph = SqliteMemoryGraphRepository(migrations_ready_db)
    use_case = BuildMemoryGraph(
        assets=_ListSource(assets),
        observations=_ListSource(observations),
        events=_EventSource((), ()),
        participants=_ParticipantSource(),
        entities=_ListSource(()),
        facts=_ListSource(()),
        relationships=_ListSource(()),
        graph=graph,
    )

    use_case.execute(SCOPE)

    hits = graph.expand_assets(("video",), scope_id=SCOPE, max_hops=1, limit=10)
    assert [(hit.asset_id, hit.hops, hit.relations) for hit in hits] == [
        ("frame", 1, ("asset_derived_from_asset",))
    ]


def test_rules_vlm_causal_edge_is_directed_and_confidence_gated(migrations_ready_db):
    assets = (
        _asset("video", "/tmp/video.mp4"),
        replace(_asset("frame-a", "/tmp/a.jpg", parent_asset_id="video"), source_timestamp_sec=1.0),
        replace(_asset("frame-b", "/tmp/b.jpg", parent_asset_id="video"), source_timestamp_sec=4.0),
    )
    common = {"objects": ("person", "bottle", "table")}
    observations = (
        replace(
            _observation("obs-a", "frame-a"),
            **common,
            facts=(ObservationFact("person", "holds", "bottle"),),
        ),
        replace(
            _observation("obs-b", "frame-b"),
            **common,
            facts=(ObservationFact("person", "puts", "bottle"),),
        ),
    )

    class _Judge:
        def judge(self, prompt, *, expected_count):
            assert expected_count == 1
            assert "relations ended" in prompt and "relations started" in prompt
            return (CausalVerdict(0, True, "LEADS_TO", 0.8, "放下瓶子"),)

    graph = SqliteMemoryGraphRepository(migrations_ready_db)
    use_case = BuildMemoryGraph(
        assets=_ListSource(assets),
        observations=_ListSource(observations),
        events=_EventSource((), ()),
        participants=_ParticipantSource(),
        entities=_ListSource(()),
        facts=_ListSource(()),
        relationships=_ListSource(()),
        graph=graph,
        causal_judge=_Judge(),
    )

    stats = use_case.execute(SCOPE)

    assert stats.causal_candidate_pairs == 1
    assert stats.causal_edges == 1
    assert stats.causal_status == "ok"
    forward = graph.expand_assets(("frame-a",), scope_id=SCOPE, max_hops=1, limit=10, intent="causal")
    reverse = graph.expand_assets(("frame-b",), scope_id=SCOPE, max_hops=1, limit=10, intent="causal")
    assert any(
        hit.asset_id == "frame-b" and hit.relations == ("asset_causes_asset:LEADS_TO",) for hit in forward
    )
    assert not any(hit.asset_id == "frame-a" for hit in reverse)
    assert all(
        "asset_causes_asset:LEADS_TO" not in hit.relations
        for hit in graph.expand_assets(("frame-a",), scope_id=SCOPE, max_hops=1, limit=10)
    )
    assert graph.quality(SCOPE)["reference_edge_precision"] == 1.0
    assert graph.quality(SCOPE)["causal_edge_count"] == 1


def test_keyframe_graph_projects_hpq_edge_families_from_2_0_sources(migrations_ready_db):
    assets = (
        _asset("video", "/tmp/video.mp4"),
        *(
            replace(
                _asset(f"frame-{index}", f"/tmp/{index}.jpg", parent_asset_id="video"),
                source_timestamp_sec=float(index * 3),
            )
            for index in range(3)
        ),
    )
    observations = tuple(
        replace(
            _observation(f"obs-{index}", f"frame-{index}"),
            objects=("bottle", "table"),
            facts=(ObservationFact("person", "holds", "bottle"),),
        )
        for index in range(3)
    )

    class _Vectors:
        def get(self, *, space, source_type, source_id, model_name=None):
            assert space == "visual" and source_type == "asset"
            return VectorRecord(
                scope_id=SCOPE,
                space=space,
                source_type=source_type,
                source_id=source_id,
                model_name="clip-test",
                vector=(1.0, 0.0) if source_id != "frame-2" else (0.0, 1.0),
            )

    graph = SqliteMemoryGraphRepository(migrations_ready_db)
    stats = BuildMemoryGraph(
        assets=_ListSource(assets),
        observations=_ListSource(observations),
        events=_EventSource((), ()),
        participants=_ParticipantSource(),
        entities=_ListSource(()),
        facts=_ListSource(()),
        relationships=_ListSource(()),
        graph=graph,
        vectors=_Vectors(),
    ).execute(SCOPE)
    assert stats.keyframe_count == 3
    assert stats.keyframe_temporal_edges == 2
    assert stats.keyframe_object_edges >= 2
    assert stats.keyframe_relation_edges >= 2
    assert stats.keyframe_visual_edges == 1
    assert stats.keyframe_visual_status == "ok"
    quality = graph.quality(SCOPE)
    assert quality["reference_mode"] == "source_consistency"
    assert quality["reference_edge_recall"] == 1.0
    assert quality["unverifiable_edge_ratio"] > 0


def test_graph_quality_reference_detects_missing_deterministic_edge(migrations_ready_db):
    graph = SqliteMemoryGraphRepository(migrations_ready_db)
    graph.replace_scope(
        SCOPE,
        nodes=(
            GraphNode("asset:video", SCOPE, "asset"),
            GraphNode("asset:frame", SCOPE, "asset", attributes={"parent_asset_id": "video"}),
        ),
        edges=(),
        build_revision="missing-edge",
    )
    quality = graph.quality(SCOPE)
    assert quality["reference_edge_count"] == 1
    assert quality["reference_edge_recall"] == 0.0
    assert quality["reference_edge_precision"] is None


def test_graph_quality_reference_detects_spurious_deterministic_edge(migrations_ready_db):
    graph = SqliteMemoryGraphRepository(migrations_ready_db)
    graph.replace_scope(
        SCOPE,
        nodes=(GraphNode("asset:one", SCOPE, "asset"), GraphNode("asset:two", SCOPE, "asset")),
        edges=(GraphEdge("extra", SCOPE, "asset:one", "asset:two", "asset_derived_from_asset"),),
        build_revision="extra-edge",
    )
    quality = graph.quality(SCOPE)
    assert quality["reference_edge_count"] == 0
    assert quality["evaluable_predicted_edge_count"] == 1
    assert quality["reference_edge_precision"] == 0.0
    assert quality["reference_edge_recall"] is None


def test_causal_judge_failure_keeps_other_graph_edges_and_reports_degraded(migrations_ready_db):
    assets = (
        _asset("video", "/tmp/video.mp4"),
        replace(_asset("frame-a", "/tmp/a.jpg", parent_asset_id="video"), source_timestamp_sec=1.0),
        replace(_asset("frame-b", "/tmp/b.jpg", parent_asset_id="video"), source_timestamp_sec=4.0),
    )
    observations = (
        replace(
            _observation("obs-a", "frame-a"),
            objects=("person", "bottle", "table"),
            facts=(ObservationFact("person", "holds", "bottle"),),
        ),
        replace(
            _observation("obs-b", "frame-b"),
            objects=("person", "bottle", "table"),
            facts=(ObservationFact("person", "puts", "bottle"),),
        ),
    )

    class _BrokenJudge:
        def judge(self, prompt, *, expected_count):
            raise TimeoutError("model unavailable")

    graph = SqliteMemoryGraphRepository(migrations_ready_db)
    stats = BuildMemoryGraph(
        assets=_ListSource(assets),
        observations=_ListSource(observations),
        events=_EventSource((), ()),
        participants=_ParticipantSource(),
        entities=_ListSource(()),
        facts=_ListSource(()),
        relationships=_ListSource(()),
        graph=graph,
        causal_judge=_BrokenJudge(),
    ).execute(SCOPE)

    assert stats.causal_status == "degraded"
    assert stats.causal_candidate_pairs == 1
    assert stats.causal_edges == 0
    assert graph.status(SCOPE).available
    assert graph.expand_assets(("video",), scope_id=SCOPE, max_hops=1, limit=10)


def test_low_confidence_causal_edge_is_not_traversed(migrations_ready_db):
    graph = SqliteMemoryGraphRepository(migrations_ready_db)
    graph.replace_scope(
        SCOPE,
        nodes=(GraphNode("asset:a", SCOPE, "asset"), GraphNode("asset:b", SCOPE, "asset")),
        edges=(GraphEdge("e", SCOPE, "asset:a", "asset:b", "asset_causes_asset:ENABLES", 0.59),),
        build_revision="weak-causal",
    )

    assert graph.expand_assets(("a",), scope_id=SCOPE, max_hops=1, limit=10, intent="causal") == ()


def test_causal_backward_and_temporal_direction_are_question_specific(migrations_ready_db):
    graph = SqliteMemoryGraphRepository(migrations_ready_db)
    graph.replace_scope(
        SCOPE,
        nodes=tuple(GraphNode(f"asset:{name}", SCOPE, "asset") for name in ("a", "b", "c")),
        edges=(
            GraphEdge("causal", SCOPE, "asset:a", "asset:b", "asset_causes_asset:LEADS_TO", 0.8),
            GraphEdge("temporal", SCOPE, "asset:b", "asset:c", "frame_temporal_next", 1.0),
        ),
        build_revision="directional",
    )
    assert [
        hit.asset_id
        for hit in graph.expand_assets(
            ("b",), scope_id=SCOPE, max_hops=1, limit=10, intent="causal", direction="backward"
        )
    ] == ["a"]
    assert [
        hit.asset_id
        for hit in graph.expand_assets(
            ("c",), scope_id=SCOPE, max_hops=1, limit=10, intent="temporal", direction="backward"
        )
    ] == ["b"]
    assert (
        graph.expand_assets(
            ("c",), scope_id=SCOPE, max_hops=1, limit=10, intent="temporal", direction="forward"
        )
        == ()
    )
    assert graph_intent("这段视频什么时候开始？") == "temporal"
    assert graph_intent("为什么瓶子倒了？") == "causal"
    assert graph_direction("倒下之前是什么？") == "backward"


def test_build_graph_projects_event_entities_and_video_scene_source(migrations_ready_db):
    assets = (_asset("video", "/tmp/video.mp4"), _asset("frame", "/tmp/frame.webp"))
    observations = (_observation("obs-frame", "frame"),)
    event = EventRecord(
        id="scene-1",
        scope_id=SCOPE,
        title="视频场景",
        event_type="video_scene",
        time_start=NOW,
        time_end=NOW,
        place="家",
        admin_district="",
        activity="聚会",
        summary="场景",
        confidence=0.9,
        aggregation_score=0.9,
        aggregation_breakdown={},
        created_at=NOW,
        source_type="video_scene",
        source_asset_id="video",
        source_scene_index=0,
        source_start_sec=0.0,
        source_end_sec=4.0,
    )
    entity = SimpleNamespace(
        id="place-1",
        canonical_name="家",
        entity_type="place",
        status="active",
        family_role=None,
        updated_at=NOW,
    )
    graph = SqliteMemoryGraphRepository(migrations_ready_db)
    use_case = BuildMemoryGraph(
        assets=_ListSource(assets),
        observations=_ListSource(observations),
        events=_EventSource((event,), ("obs-frame",)),
        participants=_ParticipantSource(
            (EventEntityRecord("scene-1", "place-1", "地点", ("obs-frame",), 0.8),)
        ),
        entities=_ListSource((entity,)),
        facts=_ListSource(()),
        relationships=_ListSource(()),
        graph=graph,
    )

    use_case.execute(SCOPE)

    with migrations_ready_db.connect() as connection:
        relations = {
            str(row[0])
            for row in connection.execute(
                "SELECT relation FROM memory_graph_edges WHERE scope_id = ?", (SCOPE,)
            ).fetchall()
        }
    assert "event_has_entity" in relations
    assert "event_source_asset" in relations
    assert [
        hit.asset_id for hit in graph.expand_assets(("video",), scope_id=SCOPE, max_hops=2, limit=10)
    ] == ["frame"]


def test_memory_graph_refresh_is_fail_open_after_memory_writes(monkeypatch):
    calls: list[str] = []

    class _Builder:
        def execute(self, scope_id):
            calls.append(scope_id)
            raise RuntimeError("derived projection unavailable")

    monkeypatch.setattr(media_routes, "build_memory_graph", lambda container: _Builder())

    media_routes.refresh_memory_graph(SimpleNamespace(), SCOPE)

    assert calls == [SCOPE]


class _FakeGraph(MemoryGraphRepository):
    def replace_scope(self, scope_id, *, nodes, edges, build_revision):
        raise NotImplementedError

    def status(self, scope_id):
        return GraphStatus(scope_id=scope_id, available=True)

    def expand_assets(self, seed_asset_ids, *, scope_id, max_hops, limit, intent="general", direction=""):
        return (GraphExpansionHit(asset_id="asset-b", score=0.8, hops=2),)


def test_retrieve_graph_is_explicitly_degraded_without_scope():
    use_case = RetrieveGraph(graph=_FakeGraph())

    result = use_case.execute(seed_asset_ids=("asset-a",), scope_id=None, limit=10)

    assert result.degraded is True
    assert result.reason == "graph_requires_scope"


def test_retrieve_graph_maps_expansion_to_graph_candidates():
    use_case = RetrieveGraph(graph=_FakeGraph())

    result = use_case.execute(seed_asset_ids=("asset-a",), scope_id=SCOPE, limit=10)

    assert result.degraded is False
    assert [(hit.asset_id, hit.channel, hit.rank) for hit in result.hits] == [("asset-b", "graph", 1)]
