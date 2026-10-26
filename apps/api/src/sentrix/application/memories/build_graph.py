"""从 2.0 标准记忆端口构建可重建的结构化记忆图。"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from datetime import datetime

from sentrix.domain.identity.ports import EntityRepository, RelationshipRepository
from sentrix.domain.media.models import Observation
from sentrix.domain.media.ports import AssetRepository, ObservationRepository
from sentrix.domain.media.scenes import KEYFRAME_DERIVED_KIND
from sentrix.domain.memories.causality import (
    CausalFrame,
    CausalParseError,
    extract_causal_pairs,
    signals_prompt,
)
from sentrix.domain.memories.events import EventRecord
from sentrix.domain.memories.graph import GraphBuildStats, GraphEdge, GraphNode
from sentrix.domain.memories.ports import (
    CausalJudge,
    EventParticipantRepository,
    EventRepository,
    FactRepository,
    MemoryGraphRepository,
)
from sentrix.domain.retrieval.ports import VectorRepository

from .keyframe_graph import project_keyframe_graph

log = logging.getLogger("sentrix.memories.build_graph")

GRAPH_READ_LIMIT = 100_000
TEMPORAL_EDGE_MAX_SECONDS = 6 * 60 * 60


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value else ""


def _normalise(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _digest(prefix: str, *parts: object) -> str:
    raw = "|".join(str(part) for part in (prefix, *parts))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _node_id(kind: str, value: str) -> str:
    return f"{kind}:{value}"


class BuildMemoryGraph:
    """把已有记忆投影成图。

    输入只有 2.0 的 domain 端口：资产、观察、事件、参与者、实体、事实、关系和
    可选的权威视觉向量。普通资产始终可构图；已有关键帧则额外构建 hpq 边族。
    不读取关键帧文件、Qdrant payload 或旧版内存库，也不运行旧版记忆生成器。
    """

    def __init__(
        self,
        *,
        assets: AssetRepository,
        observations: ObservationRepository,
        events: EventRepository,
        participants: EventParticipantRepository,
        entities: EntityRepository,
        facts: FactRepository,
        relationships: RelationshipRepository,
        graph: MemoryGraphRepository,
        causal_judge: CausalJudge | None = None,
        vectors: VectorRepository | None = None,
    ) -> None:
        self._assets = assets
        self._observations = observations
        self._events = events
        self._participants = participants
        self._entities = entities
        self._facts = facts
        self._relationships = relationships
        self._graph = graph
        self._causal_judge = causal_judge
        self._vectors = vectors

    def execute(self, scope_id: str) -> GraphBuildStats:
        scope = str(scope_id or "").strip()
        if not scope:
            raise ValueError("构建记忆图需要明确的 scope_id")

        assets = tuple(self._assets.list_by_scope(scope, limit=GRAPH_READ_LIMIT))
        observations = tuple(self._observations.list_scope(scope, limit=GRAPH_READ_LIMIT))
        events = tuple(self._events.list_by_scope(scope, limit=GRAPH_READ_LIMIT))
        entities = tuple(self._entities.list_entities(scope_id=scope))
        facts = tuple(self._facts.list_facts(scope, limit=GRAPH_READ_LIMIT))
        relationships = tuple(self._relationships.list_relationships(scope))

        nodes: dict[str, GraphNode] = {}
        edges: dict[str, GraphEdge] = {}
        observation_by_id = {item.id: item for item in observations}
        latest_by_asset: dict[str, Observation] = {}
        for observation in observations:
            current = latest_by_asset.get(observation.asset_id)
            if current is None or (observation.revision, observation.updated_at) > (
                current.revision,
                current.updated_at,
            ):
                latest_by_asset[observation.asset_id] = observation
        asset_by_id = {item.id: item for item in assets}
        entity_by_id = {item.id: item for item in entities}
        entity_by_name = {
            _normalise(item.canonical_name): item for item in entities if _normalise(item.canonical_name)
        }
        scene_by_asset: dict[str, str] = {}

        def add_node(node: GraphNode) -> None:
            nodes.setdefault(node.id, node)

        def add_edge(
            source_id: str,
            target_id: str,
            relation: str,
            *,
            weight: float = 1.0,
            attributes: Mapping[str, object] | None = None,
            discriminator: str = "",
        ) -> None:
            if source_id == target_id:
                return
            edge_id = _node_id(
                "edge",
                _digest(scope, source_id, target_id, relation, discriminator),
            )
            edges.setdefault(
                edge_id,
                GraphEdge(
                    id=edge_id,
                    scope_id=scope,
                    source_id=source_id,
                    target_id=target_id,
                    relation=relation,
                    weight=min(1.0, max(0.0, float(weight))),
                    attributes=dict(attributes or {}),
                ),
            )

        def add_context(source_id: str, *, place: str, captured_at: datetime | None) -> None:
            for kind, raw_value in (
                ("place", _normalise(place)),
                ("date", captured_at.date().isoformat() if captured_at else ""),
            ):
                if not raw_value:
                    continue
                context_id = _node_id("context", _digest(scope, kind, raw_value))
                add_node(
                    GraphNode(
                        id=context_id,
                        scope_id=scope,
                        kind=f"{kind}_context",
                        label=raw_value,
                        attributes={"context_type": kind, "normalised": raw_value},
                    )
                )
                add_edge(
                    source_id,
                    context_id,
                    f"{source_id.split(':', 1)[0]}_{kind}_context",
                    attributes={
                        "sub_type": "OCCURRED_AT" if kind == "place" else "CAPTURED_ON",
                        "evidence_tier": "confirmed",
                    },
                )

        for asset in assets:
            add_node(
                GraphNode(
                    id=_node_id("asset", asset.id),
                    scope_id=scope,
                    kind="asset",
                    label=asset.file_name,
                    attributes={
                        "asset_id": asset.id,
                        "media_type": asset.media_type.value,
                        "captured_at": _iso(asset.captured_at),
                        "derived_kind": asset.derived_kind or "",
                        "parent_asset_id": asset.parent_asset_id or "",
                        "source_timestamp_sec": asset.source_timestamp_sec,
                    },
                )
            )
            # 视频关键帧是普通 Asset，但它与原视频的血缘不是观察语义，不能只
            # 放在 attributes 里。把这条关系投影进图后，检索从关键帧能回到原视频，
            # 从原视频也能沿图找到对应关键帧；旧版 graph_memory 的 parent/derived
            # 关系因此在 2.0 标准 Asset 端口上有了等价落点。
            if asset.parent_asset_id and asset.parent_asset_id in asset_by_id:
                add_edge(
                    _node_id("asset", asset.id),
                    _node_id("asset", asset.parent_asset_id),
                    "asset_derived_from_asset",
                    attributes={
                        "derived_kind": asset.derived_kind or "",
                        "source_timestamp_sec": asset.source_timestamp_sec,
                    },
                )

        for entity in entities:
            add_node(
                GraphNode(
                    id=_node_id("entity", entity.id),
                    scope_id=scope,
                    kind="entity",
                    label=entity.canonical_name,
                    attributes={
                        "entity_id": entity.id,
                        "canonical_name": entity.canonical_name,
                        "entity_type": entity.entity_type,
                        "status": entity.status,
                        "family_role": entity.family_role or "",
                    },
                )
            )

        for event in events:
            event_node = _node_id("event", event.id)
            event_observation_ids = tuple(self._events.observation_ids(event.id))
            event_participants = tuple(self._participants.list_participants(event.id))
            event_entities = tuple(self._participants.list_event_entities(event.id))
            add_node(
                GraphNode(
                    id=event_node,
                    scope_id=scope,
                    kind="event",
                    label=event.title or event.summary,
                    attributes={
                        "event_id": event.id,
                        "time_start": _iso(event.time_start),
                        "time_end": _iso(event.time_end),
                        "place": event.place,
                        "activity": event.activity,
                        "event_type": event.event_type,
                        "summary": event.summary,
                        "source_asset_id": event.source_asset_id or "",
                        "source_type": event.source_type,
                        "source_scene_index": event.source_scene_index,
                        "observation_ids": list(event_observation_ids),
                        "participant_entity_ids": [item.person_id for item in event_participants],
                        "event_entity_ids": [item.entity_id for item in event_entities],
                    },
                )
            )
            add_context(event_node, place=event.place, captured_at=event.time_start)
            for observation_id in event_observation_ids:
                matched_observation = observation_by_id.get(observation_id)
                if matched_observation is None:
                    continue
                observation_node = _node_id("observation", matched_observation.id)
                add_edge(event_node, observation_node, "event_contains_observation")
                if matched_observation.asset_id in asset_by_id:
                    add_edge(
                        event_node, _node_id("asset", matched_observation.asset_id), "event_contains_asset"
                    )
                    if event.source_type == "video_scene":
                        scene_by_asset[matched_observation.asset_id] = event.id
            for participant in event_participants:
                if participant.person_id in entity_by_id:
                    add_edge(
                        event_node,
                        _node_id("entity", participant.person_id),
                        "event_has_entity",
                        weight=participant.confidence or 1.0,
                        attributes={"role": participant.role},
                    )
            for event_entity in event_entities:
                if event_entity.entity_id in entity_by_id:
                    add_edge(
                        event_node,
                        _node_id("entity", event_entity.entity_id),
                        "event_has_entity",
                        weight=event_entity.confidence or 1.0,
                        attributes={
                            "relation": event_entity.relation,
                            "evidence_ids": list(event_entity.evidence_ids),
                        },
                    )
            if event.source_asset_id in asset_by_id:
                add_edge(
                    event_node,
                    _node_id("asset", event.source_asset_id),
                    "event_source_asset",
                    attributes={
                        "source_type": event.source_type,
                        "source_scene_index": event.source_scene_index,
                        "source_start_sec": event.source_start_sec,
                        "source_end_sec": event.source_end_sec,
                    },
                )

        for observation in observations:
            observation_node = _node_id("observation", observation.id)
            add_node(
                GraphNode(
                    id=observation_node,
                    scope_id=scope,
                    kind="observation",
                    label=observation.caption or observation.activity or observation.place,
                    attributes={
                        "observation_id": observation.id,
                        "asset_id": observation.asset_id,
                        "revision": observation.revision,
                        "updated_at": _iso(observation.updated_at),
                        "captured_at": _iso(observation.captured_at),
                        "caption": observation.caption,
                        "activity": observation.activity,
                        "place": observation.place,
                        "event_type": observation.event_type,
                        "objects": list(observation.objects),
                        "clothing": list(observation.clothing),
                        "people": list(observation.people),
                        "fact_texts": [
                            f"{fact.subject} {fact.predicate} {fact.object}".strip()
                            for fact in observation.facts
                        ],
                    },
                )
            )
            asset_node = _node_id("asset", observation.asset_id)
            if observation.asset_id in asset_by_id:
                add_edge(observation_node, asset_node, "observation_of_asset")
                asset = asset_by_id[observation.asset_id]
                if (
                    asset.derived_kind == KEYFRAME_DERIVED_KIND
                    and latest_by_asset.get(asset.id) is observation
                ):
                    add_context(asset_node, place=observation.place, captured_at=observation.captured_at)

            concept_values: list[tuple[str, str]] = []
            concept_values.extend(("object", value) for value in observation.objects)
            concept_values.extend(("clothing", value) for value in observation.clothing)
            concept_values.extend(("person", value) for value in observation.people)
            concept_values.extend(
                (kind, value)
                for kind, value in (
                    ("place", observation.place),
                    ("activity", observation.activity),
                    ("event_type", observation.event_type),
                )
                if value
            )
            for concept_type, raw_value in concept_values:
                value = _normalise(raw_value)
                if not value:
                    continue
                mentioned_entity = entity_by_name.get(value) if concept_type == "person" else None
                if mentioned_entity is not None:
                    add_edge(
                        observation_node,
                        _node_id("entity", mentioned_entity.id),
                        "observation_mentions_entity",
                    )
                    continue
                concept_id = _node_id("concept", _digest(scope, concept_type, value))
                add_node(
                    GraphNode(
                        id=concept_id,
                        scope_id=scope,
                        kind="concept",
                        label=str(raw_value).strip(),
                        attributes={"concept_type": concept_type, "normalised": value},
                    )
                )
                add_edge(observation_node, concept_id, "observation_mentions_concept")

            for index, observation_fact in enumerate(observation.facts):
                fact_id = _node_id(
                    "observation_fact",
                    _digest(
                        observation.id,
                        index,
                        observation_fact.subject,
                        observation_fact.predicate,
                        observation_fact.object,
                    ),
                )
                add_node(
                    GraphNode(
                        id=fact_id,
                        scope_id=scope,
                        kind="fact",
                        label=(
                            f"{observation_fact.subject} {observation_fact.predicate} "
                            f"{observation_fact.object}"
                        ).strip(),
                        attributes={
                            "subject": observation_fact.subject,
                            "predicate": observation_fact.predicate,
                            "object": observation_fact.object,
                            "confidence": observation_fact.confidence,
                            "observation_id": observation.id,
                        },
                    )
                )
                add_edge(fact_id, observation_node, "fact_evidence")

        for persisted_fact in facts:
            fact_node = _node_id("fact", persisted_fact.id)
            add_node(
                GraphNode(
                    id=fact_node,
                    scope_id=scope,
                    kind="fact",
                    label=(
                        f"{persisted_fact.subject} {persisted_fact.predicate} {persisted_fact.object}"
                    ).strip(),
                    attributes={
                        "fact_id": persisted_fact.id,
                        "subject": persisted_fact.subject,
                        "predicate": persisted_fact.predicate,
                        "object": persisted_fact.object,
                        "status": str(persisted_fact.status),
                        "confidence": persisted_fact.confidence,
                        "evidence_ids": list(persisted_fact.evidence_ids),
                    },
                )
            )
            subject = entity_by_name.get(_normalise(persisted_fact.subject))
            if subject is not None:
                add_edge(
                    fact_node,
                    _node_id("entity", subject.id),
                    "fact_about_entity",
                    weight=persisted_fact.confidence,
                )
            for evidence_id in persisted_fact.evidence_ids:
                if evidence_id in observation_by_id:
                    add_edge(fact_node, _node_id("observation", evidence_id), "fact_evidence")

        for relationship in relationships:
            if relationship.status in {"retracted", "superseded"}:
                continue
            if (
                relationship.subject_entity_id not in entity_by_id
                or relationship.object_entity_id not in entity_by_id
            ):
                continue
            add_edge(
                _node_id("entity", relationship.subject_entity_id),
                _node_id("entity", relationship.object_entity_id),
                f"entity_relationship:{relationship.predicate}",
                weight=relationship.confidence or 1.0,
                attributes={"relationship_id": relationship.id, "status": relationship.status},
            )

        self._add_temporal_edges(events, scope, add_edge)

        keyframe_stats = project_keyframe_graph(
            assets=assets,
            latest_observations=latest_by_asset,
            scene_by_asset=scene_by_asset,
            vectors=self._vectors,
            add_node=lambda node_id, kind, attributes: add_node(
                GraphNode(
                    id=node_id,
                    scope_id=scope,
                    kind=f"{kind}_hub",
                    label=str(attributes.get("label") or ""),
                    attributes=attributes,
                )
            ),
            add_edge=lambda source, target, relation, weight, attributes, discriminator: add_edge(
                source,
                target,
                relation,
                weight=weight,
                attributes=attributes,
                discriminator=discriminator,
            ),
        )

        causal_started = time.perf_counter()
        frames: list[CausalFrame] = []
        for asset in assets:
            if asset.derived_kind != KEYFRAME_DERIVED_KIND or not asset.parent_asset_id:
                continue
            frame_observation = latest_by_asset.get(asset.id)
            if frame_observation is None or asset.source_timestamp_sec is None:
                continue
            frames.append(
                CausalFrame(
                    asset_id=asset.id,
                    parent_asset_id=asset.parent_asset_id,
                    timestamp_sec=asset.source_timestamp_sec,
                    frame_index=0,
                    objects=tuple(frame_observation.objects),
                    relations=tuple(
                        f"{fact.subject} {fact.predicate} {fact.object}".strip()
                        for fact in frame_observation.facts
                    ),
                )
            )
        pairs = extract_causal_pairs(tuple(frames))
        causal_status = "no_candidates" if not pairs else "not_configured"
        causal_edges = 0
        if pairs and self._causal_judge is not None:
            causal_status = "ok"
            for offset in range(0, len(pairs), 12):
                batch = pairs[offset : offset + 12]
                try:
                    verdicts = self._causal_judge.judge(signals_prompt(batch), expected_count=len(batch))
                except CausalParseError as error:
                    # hpq 对截断/不完整批次按更小的批次重试；只对解析错误做一次
                    # 单对重试。端点故障不重试，避免 Mac 16 GB 上把超时放大 12 倍。
                    log.warning("因果批次响应不完整 scope=%s batch=%d: %s", scope, offset // 12, error)
                    recovered = []
                    for local_index, pair in enumerate(batch):
                        try:
                            one = self._causal_judge.judge(signals_prompt((pair,)), expected_count=1)
                            recovered.append(replace(one[0], pair_id=local_index))
                        except Exception as retry_error:
                            causal_status = "degraded"
                            log.warning(
                                "因果单对重试失败 scope=%s pair=%s→%s: %s",
                                scope,
                                pair.before.asset_id,
                                pair.after.asset_id,
                                retry_error,
                            )
                    verdicts = recovered
                except Exception as error:  # 图是派生投影；故障必须可观测，不能冒充无因果边
                    causal_status = "degraded"
                    log.warning("因果边判定失败 scope=%s batch=%d: %s", scope, offset // 12, error)
                    continue
                for verdict in verdicts:
                    if not verdict.has_causal or verdict.edge_type not in {"LEADS_TO", "ENABLES"}:
                        continue
                    pair = batch[verdict.pair_id]
                    add_edge(
                        _node_id("asset", pair.before.asset_id),
                        _node_id("asset", pair.after.asset_id),
                        f"asset_causes_asset:{verdict.edge_type}",
                        weight=verdict.confidence,
                        attributes={
                            "sub_type": verdict.edge_type,
                            "confidence": verdict.confidence,
                            "evidence_tier": "inferred",
                            "reason": verdict.reason,
                            "method": "rules_vlm",
                        },
                    )
                    causal_edges += 1
        causal_seconds = time.perf_counter() - causal_started

        revision_parts: list[str] = [scope]
        revision_parts.extend(f"asset:{item.id}:{_iso(item.updated_at)}" for item in assets)
        revision_parts.extend(
            f"observation:{item.id}:{item.revision}:{item.updated_at.isoformat()}" for item in observations
        )
        revision_parts.extend(f"event:{item.id}:{item.revision}:{_iso(item.created_at)}" for item in events)
        revision_parts.extend(f"entity:{item.id}:{_iso(item.updated_at)}" for item in entities)
        revision_parts.extend(f"fact:{item.id}:{item.revision}:{item.status}" for item in facts)
        revision_parts.extend(
            f"relationship:{item.id}:{item.revision}:{_iso(item.updated_at)}" for item in relationships
        )
        revision_parts.extend(
            f"edge:{edge.id}:{edge.weight}:{sorted(edge.attributes.items())}" for edge in edges.values()
        )
        build_revision = hashlib.sha256("\n".join(sorted(revision_parts)).encode("utf-8")).hexdigest()
        result = self._graph.replace_scope(
            scope,
            nodes=tuple(nodes.values()),
            edges=tuple(edges.values()),
            build_revision=build_revision,
        )
        log.info(
            "记忆图已重建 scope=%s nodes=%d edges=%d revision=%s",
            scope,
            result.node_count,
            result.edge_count,
            build_revision[:12],
        )
        return replace(
            result,
            causal_candidate_pairs=len(pairs),
            causal_edges=causal_edges,
            causal_status=causal_status,
            causal_seconds=round(causal_seconds, 3),
            keyframe_count=keyframe_stats.frame_count,
            keyframe_temporal_edges=keyframe_stats.temporal_edges,
            keyframe_object_edges=keyframe_stats.object_edges,
            keyframe_relation_edges=keyframe_stats.relation_edges,
            keyframe_visual_edges=keyframe_stats.visual_edges,
            keyframe_visual_status=keyframe_stats.visual_status,
        )

    @staticmethod
    def _add_temporal_edges(
        events: Iterable[EventRecord],
        scope_id: str,
        add_edge: Callable[..., None],
    ) -> None:
        ordered = sorted(
            (event for event in events if event.time_start is not None),
            key=lambda event: (event.time_start, event.id),
        )
        for left, right in zip(ordered, ordered[1:], strict=False):
            assert left.time_start is not None and right.time_start is not None
            gap = (right.time_start - left.time_start).total_seconds()
            if 0 <= gap <= TEMPORAL_EDGE_MAX_SECONDS:
                add_edge(
                    _node_id("event", left.id),
                    _node_id("event", right.id),
                    "event_temporal_next",
                    weight=max(0.1, 1.0 - gap / TEMPORAL_EDGE_MAX_SECONDS),
                    attributes={"gap_seconds": gap, "scope_id": scope_id},
                )
