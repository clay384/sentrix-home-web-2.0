"""从图节点承载的 2.0 来源字段重建确定性参考边，不读取已落库边。

这只能衡量构图投影是否忠实于输入，不能衡量 VLM 观察或因果判断的真实准确率。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime

ReferenceEdge = tuple[str, str, str, str]
NodeSource = Mapping[str, tuple[str, Mapping[str, object]]]

GENERIC_OBJECTS = frozenset(
    {
        "person",
        "people",
        "human",
        "hand",
        "body",
        "camera",
        "floor",
        "wall",
        "ceiling",
        "sky",
        "ground",
        "pavement",
        "road",
        "floor-wood",
        "wall-tile",
        "ceiling-tile",
        "door",
        "window",
    }
)


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value if str(item).strip())


def _seconds(value: object) -> float | None:
    try:
        return float(str(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _date(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def reference_edges_from_sources(nodes: NodeSource) -> set[ReferenceEdge]:
    """只按来源字段重跑确定性关系规则；漏建边会降低 recall。"""
    reference: set[ReferenceEdge] = set()
    entities_by_name: dict[str, str] = {}
    concepts: dict[tuple[str, str], str] = {}
    contexts: dict[tuple[str, str], str] = {}
    observations_by_asset: dict[str, list[tuple[int, str, str, Mapping[str, object]]]] = defaultdict(list)
    frames_by_video: dict[str, list[tuple[float, str, Mapping[str, object]]]] = defaultdict(list)
    events: list[tuple[str, Mapping[str, object]]] = []

    for node_id, (kind, attrs) in nodes.items():
        if kind == "entity":
            entities_by_name[str(attrs.get("canonical_name") or "").casefold()] = node_id
        elif kind == "concept":
            concepts[(str(attrs.get("concept_type") or ""), str(attrs.get("normalised") or ""))] = node_id
        elif kind in {"place_context", "date_context"}:
            contexts[(str(attrs.get("context_type") or ""), str(attrs.get("normalised") or ""))] = node_id
        elif kind == "observation":
            asset_id = str(attrs.get("asset_id") or "")
            observations_by_asset[asset_id].append(
                (int(str(attrs.get("revision") or 0)), str(attrs.get("updated_at") or ""), node_id, attrs)
            )
            asset_node = f"asset:{asset_id}"
            if asset_node in nodes:
                reference.add((node_id, asset_node, "observation_of_asset", ""))
        elif kind == "event":
            events.append((node_id, attrs))

    # 名称索引在第一遍读取完所有节点后才完整；再次处理名称依赖关系。
    for node_id, (kind, attrs) in nodes.items():
        if kind == "observation":
            values: list[tuple[str, str]] = []
            for source, kind_name in (("objects", "object"), ("clothing", "clothing"), ("people", "person")):
                values.extend((kind_name, text) for text in _strings(attrs.get(source)))
            values.extend(
                (kind_name, str(attrs.get(field) or ""))
                for field, kind_name in (
                    ("place", "place"),
                    ("activity", "activity"),
                    ("event_type", "event_type"),
                )
            )
            for kind_name, text in values:
                normalised = " ".join(text.casefold().split())
                if not normalised:
                    continue
                entity = entities_by_name.get(normalised) if kind_name == "person" else None
                if entity:
                    reference.add((node_id, entity, "observation_mentions_entity", ""))
                elif concept := concepts.get((kind_name, normalised)):
                    reference.add((node_id, concept, "observation_mentions_concept", ""))
        if kind == "asset":
            parent = str(attrs.get("parent_asset_id") or "")
            if parent and f"asset:{parent}" in nodes:
                reference.add((node_id, f"asset:{parent}", "asset_derived_from_asset", ""))
            if attrs.get("derived_kind") == "video_keyframe" and parent:
                timestamp = _seconds(attrs.get("source_timestamp_sec"))
                if timestamp is not None:
                    frames_by_video[parent].append((timestamp, node_id, attrs))
        elif kind == "fact":
            for evidence_id in _strings(attrs.get("evidence_ids")):
                evidence_node = f"observation:{evidence_id}"
                if evidence_node in nodes:
                    reference.add((node_id, evidence_node, "fact_evidence", ""))
            source_observation = str(attrs.get("observation_id") or "")
            if source_observation and f"observation:{source_observation}" in nodes:
                reference.add((node_id, f"observation:{source_observation}", "fact_evidence", ""))
            subject = str(attrs.get("subject") or "").strip().casefold()
            if subject in entities_by_name:
                reference.add((node_id, entities_by_name[subject], "fact_about_entity", ""))

    for event_id, attrs in events:
        place = " ".join(str(attrs.get("place") or "").strip().casefold().split())
        date_value = _date(attrs.get("time_start"))
        for kind, value in (("place", place), ("date", date_value.date().isoformat() if date_value else "")):
            if value and (context_id := contexts.get((kind, value))):
                reference.add((event_id, context_id, f"event_{kind}_context", ""))
        for observation_id in _strings(attrs.get("observation_ids")):
            observation_node = f"observation:{observation_id}"
            if observation_node not in nodes:
                continue
            reference.add((event_id, observation_node, "event_contains_observation", ""))
            observation_attrs = nodes[observation_node][1]
            asset_id = str(observation_attrs.get("asset_id") or "")
            if f"asset:{asset_id}" in nodes:
                reference.add((event_id, f"asset:{asset_id}", "event_contains_asset", ""))
        for entity_id in (
            *_strings(attrs.get("participant_entity_ids")),
            *_strings(attrs.get("event_entity_ids")),
        ):
            if f"entity:{entity_id}" in nodes:
                reference.add((event_id, f"entity:{entity_id}", "event_has_entity", ""))
        source_asset = str(attrs.get("source_asset_id") or "")
        if source_asset and f"asset:{source_asset}" in nodes:
            reference.add((event_id, f"asset:{source_asset}", "event_source_asset", ""))

    timed_events = sorted(
        (
            (moment, node_id)
            for node_id, attrs in events
            if (moment := _date(attrs.get("time_start"))) is not None
        ),
        key=lambda item: (item[0], item[1]),
    )
    for (left_time, left_id), (right_time, right_id) in zip(timed_events, timed_events[1:], strict=False):
        gap = (right_time - left_time).total_seconds()
        if 0 <= gap <= 6 * 60 * 60:
            reference.add((left_id, right_id, "event_temporal_next", ""))

    latest = {
        asset_id: max(rows, key=lambda item: (item[0], item[1]))[3]
        for asset_id, rows in observations_by_asset.items()
    }
    for video_id, rows in frames_by_video.items():
        rows = [row for row in rows if row[1].removeprefix("asset:") in latest]
        rows.sort(key=lambda row: (row[0], row[1]))
        for _, node_id, _ in rows:
            observation_attrs = latest.get(node_id.removeprefix("asset:"), {})
            place = " ".join(str(observation_attrs.get("place") or "").strip().casefold().split())
            date_value = _date(observation_attrs.get("captured_at"))
            for kind, value in (
                ("place", place),
                ("date", date_value.date().isoformat() if date_value else ""),
            ):
                if value and (context_id := contexts.get((kind, value))):
                    reference.add((node_id, context_id, f"asset_{kind}_context", ""))
        for (_, left, _), (_, right, _) in zip(rows, rows[1:], strict=False):
            reference.add((left, right, "frame_temporal_next", ""))
        for kind_name in ("object", "relation"):
            by_label: dict[str, list[tuple[float, str]]] = defaultdict(list)
            for timestamp, node_id, _ in rows:
                observation_attrs = latest.get(node_id.removeprefix("asset:"), {})
                source = "objects" if kind_name == "object" else "fact_texts"
                labels = tuple(
                    dict.fromkeys(
                        str(label).strip().casefold()
                        for label in _strings(observation_attrs.get(source))
                        if str(label).strip()
                    )
                )
                if kind_name == "object":
                    labels = tuple(label for label in labels if label not in GENERIC_OBJECTS)[:4]
                for label in labels:
                    by_label[label].append((timestamp, node_id))
            for label, matches in by_label.items():
                if kind_name == "relation" and len(matches) < 2:
                    continue
                if len(matches) >= 30:
                    hub_id = f"{kind_name}_hub:{video_id}:{label}"
                    if hub_id in nodes:
                        for _, frame_id in matches:
                            reference.add((frame_id, hub_id, f"frame_mentions_{kind_name}", label))
                else:
                    for index, (left_sec, left_id) in enumerate(matches):
                        for right_sec, right_id in matches[index + 1 : index + 8]:
                            if kind_name == "relation" and right_sec - left_sec > 60.0:
                                break
                            reference.add((left_id, right_id, f"frame_shares_{kind_name}", label))
    return reference
