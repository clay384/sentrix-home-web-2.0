"""与 hpq ``rules_vlm`` 相同的关键帧候选和规则信号（纯计算）。

只在同一源视频内取最多后继两帧、30 秒窗口；没有 bbox 的 2.0 观察走原实现的
label-only 场景稳定性回退。时间相邻本身不是因果证据。
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_FRAME_GAP = 2
MAX_SECONDS = 30.0
GENERIC_OBJECTS = frozenset(
    {"floor", "wall", "ceiling", "sky", "ground", "pavement", "road", "door", "window"}
)


@dataclass(frozen=True, slots=True)
class CausalFrame:
    asset_id: str
    parent_asset_id: str
    timestamp_sec: float
    frame_index: int
    objects: tuple[str, ...]
    relations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CausalSignals:
    rel_gone: tuple[str, ...]
    rel_new: tuple[str, ...]
    obj_new: tuple[str, ...]
    obj_gone: tuple[str, ...]
    scene_stable: int
    scene_total: int
    scene_is_stable: bool

    @property
    def has_signals(self) -> bool:
        # hpq 的 strict rules_vlm：计数变化和单纯物体出现不能独立成为因果证据。
        return self.scene_is_stable and bool(self.rel_gone or self.rel_new)


@dataclass(frozen=True, slots=True)
class CausalPair:
    before: CausalFrame
    after: CausalFrame
    signals: CausalSignals


@dataclass(frozen=True, slots=True)
class CausalVerdict:
    pair_id: int
    has_causal: bool
    edge_type: str
    confidence: float
    reason: str


class CausalParseError(ValueError):
    """判定响应不完整；调用方可以把批次缩小后重试。"""


def _labels(values: tuple[str, ...]) -> set[str]:
    return {value.strip().casefold() for value in values if value.strip()}


def extract_signals(before: CausalFrame, after: CausalFrame) -> CausalSignals:
    """hpq 的无 bbox 回退：至少两个共同的具体物体、变化物体不超过四个。"""
    objects_a = _labels(before.objects) - GENERIC_OBJECTS
    objects_b = _labels(after.objects) - GENERIC_OBJECTS
    shared = len(objects_a & objects_b)
    changed = len(objects_a ^ objects_b)
    relations_a = _labels(before.relations)
    relations_b = _labels(after.relations)
    return CausalSignals(
        rel_gone=tuple(sorted(relations_a - relations_b)),
        rel_new=tuple(sorted(relations_b - relations_a)),
        obj_new=tuple(sorted(objects_b - objects_a)),
        obj_gone=tuple(sorted(objects_a - objects_b)),
        scene_stable=shared,
        scene_total=shared,
        scene_is_stable=shared >= 2 and changed <= 4,
    )


def extract_causal_pairs(frames: tuple[CausalFrame, ...]) -> tuple[CausalPair, ...]:
    """同视频、时间有向的候选；跨场景可配对，跨视频绝不配对。"""
    by_video: dict[str, list[CausalFrame]] = {}
    for frame in frames:
        by_video.setdefault(frame.parent_asset_id, []).append(frame)
    pairs: list[CausalPair] = []
    for video_frames in by_video.values():
        ordered = sorted(
            video_frames, key=lambda frame: (frame.timestamp_sec, frame.frame_index, frame.asset_id)
        )
        for index, before in enumerate(ordered):
            for after in ordered[index + 1 : index + 1 + MAX_FRAME_GAP]:
                delta = after.timestamp_sec - before.timestamp_sec
                if delta < 0 or delta > MAX_SECONDS:
                    break
                if _labels(before.objects) == _labels(after.objects) and _labels(before.relations) == _labels(
                    after.relations
                ):
                    continue
                signals = extract_signals(before, after)
                if signals.has_signals:
                    pairs.append(CausalPair(before, after, signals))
    return tuple(pairs)


def signals_prompt(pairs: tuple[CausalPair, ...]) -> str:
    """hpq rules_vlm 的文字信号判定提示；不传图、不写评测内容。"""
    lines = [
        "You are a video understanding expert. For each keyframe pair, decide if there is a real "
        "causal relationship.",
        "",
        "Causality means: a human action in frame A directly causes an object state change in frame B.",
        "",
        "REAL causal signals:",
        "- Relation state change: e.g. person was holding X, now not holding X (or vice versa)",
        "- Object manipulation: person picks up, puts down, opens, closes, touches an object",
        "- Scene is stable (most objects unchanged) while 1-2 objects clearly change state",
        "",
        "NOT causal (always answer false):",
        "- Camera movement (many objects shift simultaneously, scene unstable)",
        "- Scene switch (many objects appear or disappear at once)",
        "- Person walking to a different location",
        "- Object detection flicker (same scene, slightly different labels)",
        "- Large furniture or appliances appearing/disappearing",
        "",
        "=== Frame Pair Signals ===",
    ]
    for index, pair in enumerate(pairs):
        signal = pair.signals
        lines.append(
            f"[pair {index}] time {pair.before.timestamp_sec:.0f}s -> {pair.after.timestamp_sec:.0f}s"
        )
        lines.append(f"  scene: {signal.scene_stable}/{signal.scene_total} objects stable, displacement=0%")
        if signal.rel_gone:
            lines.append("  relations ended: " + "; ".join(signal.rel_gone))
        if signal.rel_new:
            lines.append("  relations started: " + "; ".join(signal.rel_new))
        if signal.obj_new:
            lines.append("  appeared: " + ", ".join(signal.obj_new))
        if signal.obj_gone:
            lines.append("  disappeared: " + ", ".join(signal.obj_gone))
        lines.append("")
    lines.extend(
        (
            "Return exactly one compact JSON object per pair:",
            "{results: [{pair_id, has_causal, edge_type, confidence, reason}]}",
            "edge_type must be exactly LEADS_TO, ENABLES, or NONE.",
            "reason: Chinese, at most 12 characters; use 无 when has_causal=false.",
            "Do not add explanations, markdown, or missing fields.",
        )
    )
    return "\n".join(lines)
