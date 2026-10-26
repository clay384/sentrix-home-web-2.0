"""SQLite 记忆图适配器。

图表是派生镜像，权威数据仍在 events / observations / assets / entities 等表里。
替换或重建图只影响这组三张表，不会改变产品主链路的写入语义。
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime

from sentrix.domain.memories.graph import (
    GraphBuildStats,
    GraphEdge,
    GraphExpansionHit,
    GraphNode,
    GraphStatus,
)
from sentrix.domain.memories.graph_reference import reference_edges_from_sources
from sentrix.infrastructure.persistence.database import Database


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


class SqliteMemoryGraphRepository:
    """按 scope 原子替换、按有限 hop 做邻接遍历。"""

    def __init__(self, database: Database) -> None:
        self._database = database

    def replace_scope(
        self,
        scope_id: str,
        *,
        nodes: Sequence[GraphNode],
        edges: Sequence[GraphEdge],
        build_revision: str,
    ) -> GraphBuildStats:
        timestamp = datetime.now(UTC).isoformat()
        node_rows = [
            (
                node.id,
                scope_id,
                node.kind,
                node.label,
                _json(dict(node.attributes)),
                build_revision,
                timestamp,
                timestamp,
            )
            for node in nodes
        ]
        edge_rows = [
            (
                edge.id,
                scope_id,
                edge.source_id,
                edge.target_id,
                edge.relation,
                float(edge.weight),
                _json(dict(edge.attributes)),
                build_revision,
                timestamp,
                timestamp,
            )
            for edge in edges
        ]
        with self._database.connect() as connection, connection:
            # 边先于节点删除，保持派生表内部始终自洽，即使未来加上 FK 也不需要改顺序。
            connection.execute("DELETE FROM memory_graph_edges WHERE scope_id = ?", (scope_id,))
            connection.execute("DELETE FROM memory_graph_nodes WHERE scope_id = ?", (scope_id,))
            connection.executemany(
                "INSERT INTO memory_graph_nodes"
                " (id, scope_id, node_type, label, attributes_json, build_revision, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                node_rows,
            )
            connection.executemany(
                "INSERT INTO memory_graph_edges"
                " (id, scope_id, source_id, target_id, relation, weight, attributes_json,"
                "  build_revision, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                edge_rows,
            )
            connection.execute(
                "INSERT INTO memory_graph_builds(scope_id, build_revision, node_count, edge_count, built_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(scope_id) DO UPDATE SET build_revision=excluded.build_revision,"
                " node_count=excluded.node_count, edge_count=excluded.edge_count, built_at=excluded.built_at",
                (scope_id, build_revision, len(node_rows), len(edge_rows), timestamp),
            )
        return GraphBuildStats(scope_id=scope_id, node_count=len(node_rows), edge_count=len(edge_rows))

    def status(self, scope_id: str) -> GraphStatus:
        with self._database.connect() as connection:
            build = connection.execute(
                "SELECT build_revision, node_count, edge_count FROM memory_graph_builds WHERE scope_id = ?",
                (scope_id,),
            ).fetchone()
        if build is None:
            return GraphStatus(scope_id=scope_id, available=False, reason="graph_not_built")
        return GraphStatus(
            scope_id=scope_id,
            available=True,
            node_count=int(build[1]),
            edge_count=int(build[2]),
            build_revision=str(build[0]),
        )

    def quality(self, scope_id: str) -> Mapping[str, object]:
        """图自身的结构审计。

        从图节点上的原始来源字段重建确定性参考边；不使用已落库的边作为参考集。
        这仍只是 source-consistency，不是独立人工真值。视觉/因果边单列为不可验。
        """

        status = self.status(scope_id)
        if not status.available:
            return {
                "available": False,
                "scope_id": scope_id,
                "reason": status.reason or "graph_not_built",
                "reference_mode": "source_consistency",
            }
        with self._database.connect() as connection:
            node_rows = connection.execute(
                "SELECT id, node_type, attributes_json FROM memory_graph_nodes WHERE scope_id = ?",
                (scope_id,),
            ).fetchall()
            edge_rows = connection.execute(
                "SELECT source_id, target_id, relation, attributes_json"
                " FROM memory_graph_edges WHERE scope_id = ?",
                (scope_id,),
            ).fetchall()

        nodes: dict[str, tuple[str, dict[str, object]]] = {}
        for row in node_rows:
            try:
                attributes = json.loads(row[2] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                attributes = {}
            nodes[str(row[0])] = (str(row[1]), attributes if isinstance(attributes, dict) else {})
        node_types = {node_id: kind for node_id, (kind, _) in nodes.items()}
        source_reference = reference_edges_from_sources(nodes)
        evaluable_relations = frozenset(relation for _, _, relation, _ in source_reference) | {
            "asset_derived_from_asset",
            "observation_of_asset",
            "event_contains_observation",
            "event_contains_asset",
            "event_source_asset",
            "event_has_entity",
            "observation_mentions_concept",
            "observation_mentions_entity",
            "fact_evidence",
            "fact_about_entity",
            "event_temporal_next",
            "frame_temporal_next",
            "frame_mentions_object",
            "frame_shares_object",
            "frame_mentions_relation",
            "frame_shares_relation",
            "event_place_context",
            "event_date_context",
            "asset_place_context",
            "asset_date_context",
        }
        allowed = {
            "asset_derived_from_asset": {("asset", "asset")},
            "event_contains_observation": {("event", "observation")},
            "event_contains_asset": {("event", "asset")},
            "event_source_asset": {("event", "asset")},
            "event_has_entity": {("event", "entity")},
            "observation_of_asset": {("observation", "asset")},
            "observation_mentions_entity": {("observation", "entity")},
            "observation_mentions_concept": {("observation", "concept")},
            "fact_evidence": {("fact", "observation")},
            "fact_about_entity": {("fact", "entity")},
            "event_temporal_next": {("event", "event")},
            "frame_temporal_next": {("asset", "asset")},
            "frame_mentions_object": {("asset", "object_hub")},
            "frame_shares_object": {("asset", "asset")},
            "frame_mentions_relation": {("asset", "relation_hub")},
            "frame_shares_relation": {("asset", "asset")},
            "frame_visual_similar": {("asset", "asset")},
            "event_place_context": {("event", "place_context")},
            "event_date_context": {("event", "date_context")},
            "asset_place_context": {("asset", "place_context")},
            "asset_date_context": {("asset", "date_context")},
            "asset_causes_asset:LEADS_TO": {("asset", "asset")},
            "asset_causes_asset:ENABLES": {("asset", "asset")},
        }
        total = len(edge_rows)
        causal_rows = [row for row in edge_rows if str(row[2]).startswith("asset_causes_asset:")]
        causal_confidences: list[float] = []
        for row in causal_rows:
            try:
                properties = json.loads(row[3] or "{}")
                if isinstance(properties, dict):
                    causal_confidences.append(float(properties.get("confidence") or 0.0))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        endpoint_valid = 0
        contract_valid = 0
        attribute_supported = 0
        evidence_supported = 0
        predicted_reference: set[tuple[str, str, str, str]] = set()
        predicted_counts: Counter[tuple[str, str, str, str]] = Counter()
        for row in edge_rows:
            source_id, target_id, relation = str(row[0]), str(row[1]), str(row[2])
            source_kind, target_kind = node_types.get(source_id), node_types.get(target_id)
            if source_kind and target_kind:
                endpoint_valid += 1
                relation_ok = (relation in allowed and (source_kind, target_kind) in allowed[relation]) or (
                    relation.startswith("entity_relationship:") and source_kind == target_kind == "entity"
                )
                if relation_ok:
                    contract_valid += 1
            try:
                attributes = json.loads(row[3] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                attributes = {}
            if isinstance(attributes, dict) and attributes:
                attribute_supported += 1
            if not isinstance(attributes, dict):
                attributes = {}
            discriminator = str(attributes.get("label") or "") if relation.startswith("frame_") else ""
            key = (source_id, target_id, relation, discriminator)
            predicted_counts[key] += 1
            if relation in evaluable_relations:
                predicted_reference.add(key)
            if relation == "frame_visual_similar":
                with suppress(TypeError, ValueError):
                    evidence_supported += float(attributes.get("similarity") or 0.0) >= 0.75
            elif relation.startswith("asset_causes_asset:"):
                with suppress(TypeError, ValueError):
                    confidence = float(attributes.get("confidence") or 0.0)
                    evidence_supported += bool(attributes.get("reason")) and confidence >= 0.5
            elif relation in evaluable_relations:
                evidence_supported += 1
        duplicate_extra = sum(max(0, count - 1) for count in predicted_counts.values())

        def rate(numerator: int, denominator: int) -> float | None:
            return round(numerator / denominator, 4) if denominator else None

        connected_nodes: set[str] = set()
        for row in edge_rows:
            if str(row[0]) in node_types and str(row[1]) in node_types:
                connected_nodes.update((str(row[0]), str(row[1])))
        true_positive = len(predicted_reference & source_reference)
        reference_count = len(source_reference)
        reference_precision = rate(true_positive, len(predicted_reference))
        reference_recall = rate(true_positive, reference_count)
        reference_f1 = (
            round(
                2 * reference_precision * reference_recall / (reference_precision + reference_recall),
                4,
            )
            if reference_precision is not None
            and reference_recall is not None
            and reference_precision + reference_recall
            else None
        )
        return {
            "available": True,
            "scope_id": scope_id,
            "build_revision": status.build_revision,
            "node_count": len(node_rows),
            "edge_count": total,
            "causal_edge_count": len(causal_rows),
            "causal_edge_mean_confidence": (
                round(sum(causal_confidences) / len(causal_confidences), 4) if causal_confidences else None
            ),
            "reference_mode": "source_consistency",
            "reference_edge_count": reference_count,
            "evaluable_predicted_edge_count": len(predicted_reference),
            "reference_edge_true_positive_count": true_positive,
            "reference_edge_precision": reference_precision,
            "reference_edge_recall": reference_recall,
            "reference_edge_f1": reference_f1,
            "valid_edge_rate": rate(endpoint_valid, total),
            "edge_consistency_rate": rate(contract_valid, total),
            "edge_attribute_support_rate": rate(attribute_supported, total),
            "edge_evidence_support_rate": rate(evidence_supported, total),
            "duplicate_edge_rate": rate(duplicate_extra, total),
            "node_connected_rate": rate(len(connected_nodes), len(node_rows)),
            "unverifiable_edge_ratio": rate(total - len(predicted_reference), total),
            "independent_ground_truth_available": False,
            "note": "P/R/F1 按来源字段重建的确定性参考边计算，不是独立人工真值；视觉/因果边不计入。",
        }

    def expand_assets(
        self,
        seed_asset_ids: Sequence[str],
        *,
        scope_id: str,
        max_hops: int,
        limit: int,
        intent: str = "general",
        direction: str = "",
    ) -> Sequence[GraphExpansionHit]:
        seeds = tuple(dict.fromkeys(f"asset:{asset_id}" for asset_id in seed_asset_ids if asset_id))
        if not seeds or max_hops < 1 or limit < 1:
            return ()

        status = self.status(scope_id)
        if not status.available:
            return ()

        # 每层最多保留有限 frontier，避免错误或过于常见的概念把整张图拖进内存。
        frontier: dict[str, tuple[float, int, tuple[str, ...], tuple[str, ...]]] = {
            node_id: (1.0, 0, (node_id,), ()) for node_id in seeds
        }
        best_hops: dict[str, int] = {node_id: 0 for node_id in seeds}
        candidates: dict[str, GraphExpansionHit] = {}

        with self._database.connect() as connection:
            for _ in range(max_hops):
                if not frontier:
                    break
                frontier_ids = tuple(frontier)
                placeholders = ",".join("?" for _ in frontier_ids)
                rows = connection.execute(
                    "SELECT source_id, target_id, relation, weight FROM memory_graph_edges"
                    f" WHERE scope_id = ? AND (source_id IN ({placeholders})"
                    f" OR target_id IN ({placeholders}))"
                    " ORDER BY id LIMIT 10000",
                    (scope_id, *frontier_ids, *frontier_ids),
                ).fetchall()
                neighbor_ids = {str(row[1]) if str(row[0]) in frontier else str(row[0]) for row in rows}
                if not neighbor_ids:
                    break
                neighbor_placeholders = ",".join("?" for _ in neighbor_ids)
                node_rows = connection.execute(
                    "SELECT id, node_type FROM memory_graph_nodes"
                    f" WHERE scope_id = ? AND id IN ({neighbor_placeholders})",
                    (scope_id, *neighbor_ids),
                ).fetchall()
                node_types = {str(row[0]): str(row[1]) for row in node_rows}
                next_frontier: dict[str, tuple[float, int, tuple[str, ...], tuple[str, ...]]] = {}

                for row in rows:
                    source_id, target_id = str(row[0]), str(row[1])
                    relation = str(row[2])
                    weight = float(row[3] or 0.0)
                    causal = relation.startswith("asset_causes_asset:")
                    temporal = relation in {"frame_temporal_next", "event_temporal_next"}
                    object_edge = relation in {"frame_shares_object", "frame_mentions_object"}
                    semantic_edge = relation in {
                        "frame_shares_relation",
                        "frame_mentions_relation",
                        "frame_visual_similar",
                    }
                    hierarchy = relation in {
                        "asset_derived_from_asset",
                        "event_contains_asset",
                        "event_contains_observation",
                        "event_source_asset",
                        "observation_of_asset",
                    }
                    # hpq: 因果边不参加普通检索，且只信 >=0.60 的推断。
                    if causal and (weight < 0.60 or intent not in {"causal", "action", "multi_hop"}):
                        continue
                    if intent == "causal" and not (causal or hierarchy):
                        continue
                    if intent == "temporal" and not (temporal or hierarchy):
                        continue
                    if intent == "object" and (causal or temporal):
                        continue
                    if intent == "scene" and (causal or temporal or object_edge):
                        continue
                    for current_id, neighbor_id in ((source_id, target_id), (target_id, source_id)):
                        if causal and (
                            (direction == "backward" and current_id != target_id)
                            or (direction != "backward" and current_id != source_id)
                        ):
                            continue
                        if (
                            temporal
                            and direction in {"forward", "backward"}
                            and (
                                (direction == "forward" and current_id != source_id)
                                or (direction == "backward" and current_id != target_id)
                            )
                        ):
                            continue
                        if (
                            intent == "causal"
                            and relation == "asset_derived_from_asset"
                            and current_id != target_id
                        ):
                            continue
                        current = frontier.get(current_id)
                        if current is None or neighbor_id == current_id:
                            continue
                        current_score, current_hops, current_path, current_relations = current
                        next_hops = current_hops + 1
                        old_hops = best_hops.get(neighbor_id)
                        if old_hops is not None and old_hops <= next_hops:
                            continue
                        best_hops[neighbor_id] = next_hops
                        safe_weight = min(1.0, max(0.0, weight))
                        intent_boost = 1.0
                        if intent in {"causal", "action", "multi_hop"} and causal:
                            intent_boost = 1.25
                        elif (
                            (intent in {"temporal", "action"} and temporal)
                            or (intent == "object" and object_edge)
                            or (intent == "scene" and semantic_edge)
                        ):
                            intent_boost = 1.15
                        next_score = (
                            current_score
                            * min(1.0, safe_weight * intent_boost)
                            * (0.72 if next_hops > 1 else 1.0)
                        )
                        state = (
                            next_score,
                            next_hops,
                            (*current_path, neighbor_id),
                            (*current_relations, relation),
                        )
                        next_frontier[neighbor_id] = state
                        if node_types.get(neighbor_id) == "asset" and neighbor_id not in seeds:
                            asset_id = neighbor_id.removeprefix("asset:")
                            existing = candidates.get(asset_id)
                            hit = GraphExpansionHit(
                                asset_id=asset_id,
                                score=next_score,
                                hops=next_hops,
                                path=state[2],
                                relations=state[3],
                            )
                            if existing is None or hit.score > existing.score:
                                candidates[asset_id] = hit
                # Keep the most promising 512 frontier nodes for the next hop.
                frontier = dict(sorted(next_frontier.items(), key=lambda item: (-item[1][0], item[0]))[:512])

        return tuple(
            sorted(
                candidates.values(),
                key=lambda item: (-item.score, item.hops, item.asset_id),
            )[:limit]
        )
