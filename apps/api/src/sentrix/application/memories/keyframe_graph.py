"""把 2.0 已有关键帧观察投影为 hpq 的非因果图边族。

只读取标准资产、观察和权威视觉向量；不运行旧版记忆生成器，也不读取 Qdrant payload。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from sentrix.domain.media.models import Asset, Observation
from sentrix.domain.media.scenes import KEYFRAME_DERIVED_KIND
from sentrix.domain.retrieval.ports import VectorRepository
from sentrix.domain.retrieval.scheme import VISUAL_SPACE

log = logging.getLogger("sentrix.memories.keyframe_graph")

HUB_THRESHOLD = 30
OBJECTS_PER_FRAME = 4
PAIR_WINDOW = 8
RELATION_WINDOW_SECONDS = 60.0
VISUAL_TOP_K = 3
VISUAL_MIN_SCORE = 0.80
VISUAL_CHUNK_SIZE = 256
NON_INFORMATIVE_OBJECTS = frozenset(
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


@dataclass(frozen=True, slots=True)
class KeyframeGraphStats:
    frame_count: int = 0
    temporal_edges: int = 0
    object_edges: int = 0
    relation_edges: int = 0
    visual_edges: int = 0
    visual_status: str = "no_frames"


@dataclass(frozen=True, slots=True)
class _Frame:
    asset: Asset
    observation: Observation
    scene_id: str
    objects: tuple[str, ...]
    relations: tuple[str, ...]

    @property
    def timestamp(self) -> float:
        return float(self.asset.source_timestamp_sec or 0.0)

    @property
    def node_id(self) -> str:
        return f"asset:{self.asset.id}"


def _labels(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value.strip().casefold() for value in values if value.strip()))


def _relations(observation: Observation) -> tuple[str, ...]:
    return _labels(
        tuple(f"{fact.subject} {fact.predicate} {fact.object}".strip() for fact in observation.facts)
    )


def _visual_neighbors(
    frames: Sequence[_Frame], vectors: VectorRepository
) -> tuple[dict[tuple[str, str], float], str]:
    """与 hpq 一样用分块余弦、top-3 且双向互选；不同模型绝不混比。"""
    by_model: dict[tuple[str, int], list[tuple[str, tuple[float, ...]]]] = defaultdict(list)
    missing = 0
    for frame in frames:
        record = vectors.get(space=VISUAL_SPACE, source_type="asset", source_id=frame.asset.id)
        if record is None or not record.vector:
            missing += 1
            continue
        by_model[(record.model_name, record.dimension)].append((frame.asset.id, record.vector))

    edges: dict[tuple[str, str], float] = {}
    for rows in by_model.values():
        if len(rows) < 2:
            continue
        ids = [item[0] for item in rows]
        matrix = np.asarray([item[1] for item in rows], dtype=np.float32)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        matrix /= norms
        neighbors: list[set[int]] = [set() for _ in ids]
        top_k = min(VISUAL_TOP_K, len(ids) - 1)
        for start in range(0, len(ids), VISUAL_CHUNK_SIZE):
            stop = min(start + VISUAL_CHUNK_SIZE, len(ids))
            similarities = matrix[start:stop] @ matrix.T
            local_rows = np.arange(stop - start)
            similarities[local_rows, np.arange(start, stop)] = -np.inf
            # argpartition 的 kth 是 0-based；取前 top_k 项时必须减一。
            top_indices = np.argpartition(-similarities, kth=top_k - 1, axis=1)[:, :top_k]
            for local_index, candidates in enumerate(top_indices):
                source = start + local_index
                for target in candidates:
                    if similarities[local_index, target] >= VISUAL_MIN_SCORE:
                        neighbors[source].add(int(target))
        for source, targets in enumerate(neighbors):
            for target in targets:
                if source not in neighbors[target]:
                    continue
                pair = (min(ids[source], ids[target]), max(ids[source], ids[target]))
                edges[pair] = float(np.dot(matrix[source], matrix[target]))
    if not by_model:
        return edges, "vectors_unavailable"
    return edges, "partial_vectors" if missing else "ok"


def project_keyframe_graph(
    *,
    assets: Sequence[Asset],
    latest_observations: Mapping[str, Observation],
    scene_by_asset: Mapping[str, str],
    vectors: VectorRepository | None,
    add_node: Callable[[str, str, Mapping[str, object]], None],
    add_edge: Callable[[str, str, str, float, Mapping[str, object], str], None],
) -> KeyframeGraphStats:
    """旧 hpq 的时序、对象、关系与视觉近邻策略在 2.0 端口上的等价投影。"""
    frames = [
        _Frame(
            asset=asset,
            observation=latest_observations[asset.id],
            scene_id=scene_by_asset.get(asset.id, ""),
            objects=tuple(
                value
                for value in _labels(latest_observations[asset.id].objects)
                if value not in NON_INFORMATIVE_OBJECTS
            )[:OBJECTS_PER_FRAME],
            relations=_relations(latest_observations[asset.id]),
        )
        for asset in assets
        if asset.derived_kind == KEYFRAME_DERIVED_KIND
        and asset.parent_asset_id
        and asset.source_timestamp_sec is not None
        and asset.id in latest_observations
    ]
    if not frames:
        return KeyframeGraphStats()
    by_video: dict[str, list[_Frame]] = defaultdict(list)
    for frame in frames:
        assert frame.asset.parent_asset_id is not None
        by_video[frame.asset.parent_asset_id].append(frame)
    temporal = object_count = relation_count = visual_count = 0
    for video_id, rows in by_video.items():
        rows.sort(key=lambda item: (item.timestamp, item.asset.id))
        for index, (left, right) in enumerate(zip(rows, rows[1:], strict=False)):
            subtype = "TIME_PRECEDES" if left.scene_id == right.scene_id else "SCENE_NEXT"
            add_edge(
                left.node_id,
                right.node_id,
                "frame_temporal_next",
                1.0,
                {
                    "sub_type": subtype,
                    "sequence_index": index,
                    "video_id": video_id,
                    "evidence_tier": "confirmed",
                },
                "",
            )
            temporal += 1

        for kind in ("object", "relation"):
            labels_to_frames: dict[str, list[_Frame]] = defaultdict(list)
            for frame in rows:
                for label in frame.objects if kind == "object" else frame.relations:
                    labels_to_frames[label].append(frame)
            for label, matches in sorted(labels_to_frames.items()):
                if kind == "relation" and len(matches) < 2:
                    continue
                if len(matches) >= HUB_THRESHOLD:
                    # 旧版 hub 阈值仍为 30；按源视频隔离，避免同名关系跨视频误连。
                    hub_id = f"{kind}_hub:{video_id}:{label}"
                    add_node(
                        hub_id,
                        kind,
                        {"source_video_id": video_id, "label": label, "frame_count": len(matches)},
                    )
                    for frame in matches:
                        relation = f"frame_mentions_{kind}"
                        weight = 0.9 if kind == "object" else 0.7
                        add_edge(
                            frame.node_id,
                            hub_id,
                            relation,
                            weight,
                            {
                                "sub_type": "MENTIONS_OBJECT" if kind == "object" else "SHARES_RELATION",
                                "label": label,
                                "evidence_tier": "supported",
                            },
                            label,
                        )
                        if kind == "object":
                            object_count += 1
                        else:
                            relation_count += 1
                    continue
                for index, left in enumerate(matches):
                    for right in matches[index + 1 : index + 1 + PAIR_WINDOW]:
                        if kind == "relation" and right.timestamp - left.timestamp > RELATION_WINDOW_SECONDS:
                            break
                        relation = f"frame_shares_{kind}"
                        weight = 0.9 if kind == "object" else 0.7
                        add_edge(
                            left.node_id,
                            right.node_id,
                            relation,
                            weight,
                            {
                                "sub_type": "SAME_ENTITY" if kind == "object" else "SHARES_RELATION",
                                "label": label,
                                "evidence_tier": "supported",
                            },
                            label,
                        )
                        if kind == "object":
                            object_count += 1
                        else:
                            relation_count += 1

    visual_status = "not_configured"
    if vectors is not None:
        try:
            visual_edges, visual_status = _visual_neighbors(frames, vectors)
            for (left_id, right_id), score in sorted(visual_edges.items()):
                add_edge(
                    f"asset:{left_id}",
                    f"asset:{right_id}",
                    "frame_visual_similar",
                    score,
                    {"sub_type": "VISUAL_SIMILAR", "similarity": score, "evidence_tier": "inferred"},
                    "",
                )
                visual_count += 1
        except Exception as error:  # 派生图不能因为向量后端失效而丢失其它边族
            visual_status = "degraded"
            log.warning("关键帧视觉近邻降级: %s", error)
    return KeyframeGraphStats(
        frame_count=len(frames),
        temporal_edges=temporal,
        object_edges=object_count,
        relation_edges=relation_count,
        visual_edges=visual_count,
        visual_status=visual_status,
    )
