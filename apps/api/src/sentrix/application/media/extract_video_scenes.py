"""视频抽帧与场景成型（S8b · T511–T513）。

一条链三件事，顺序是硬约束：

    切窗（domain 规则）→ 每个窗中点取一帧（端口 → ffmpeg）→ 派生帧落库为 image 资产
    → **送进现有图片流水线**（``process_image``，带 forced_event_id 挂到场景事件上）

1. **派生帧就是 ``media_type='image'`` 的普通 Asset**（1.0 ``video/processor.py:188``，
   用户裁决④）。这是整条设计里最省力的一环：关键帧因此白得 observation、wemm 向量、
   人脸、事件归并，而这一层一行语义代码都不用写——它只把资产建出来，然后交给流水线。
2. **每个场景一条 ``source_type='video_scene'`` 的事件**（1.0
   ``db.py:2397`` 的 ``create_video_scene_event``），区间写 ``source_start_sec`` /
   ``source_end_sec``。事件建在前、帧挂上去在后：``process_image`` 的
   ``forced_event_id`` 要的就是那条事件。
3. **幂等靠唯一血缘索引**（迁移 0010 的 ``(parent_asset_id, source_frame_index)``）：
   已有同一帧号就跳过该帧，已有同一场景序号就复用事件（``create_video_scene`` 自己
   先查后插）。**不要改成按 timestamp 判重**——REAL 的秒数在同一场景里可能重复，
   迁移表头写了理由。这里再加一层应用侧的前置检查，是因为流水线不能对一行不存在的
   资产开观察（外键）；DB 索引仍然是最后一道。

**不做双算法并存**（用户裁决⑧）：``derived_kind`` 只有一个取值
（``KEYFRAME_DERIVED_KIND``），没有 legacy/现用之分。
"""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sentrix.domain.media.models import Asset, AssetStatus, MediaType
from sentrix.domain.media.ports import (
    AssetRepository,
    VideoKeyframeExtractor,
    VideoRepository,
    VideoScenePlanner,
)
from sentrix.domain.media.scenes import KEYFRAME_DERIVED_KIND, SceneWindow, plan_scenes
from sentrix.domain.memories.events import EventRecord
from sentrix.domain.memories.ports import EventRepository
from sentrix.domain.places.ports import AssetPlaceRepository

# 1.0 建场景事件时的占位地点（``db.py:2407`` 的 ``data.get("place") or "其他或不确定"``）。
# 地点解析不出结果时事件不该是一行空字符串——那是「不知道」冒充「没有地点」。
UNKNOWN_PLACE = "其他或不确定"

# 1.0 的 ``video/processor.py:203``：场景标题是「视频场景 N」（N 从 1 起），
# 摘要带区间。两者都写死在事件上，后续的 ``refresh_event_summary`` 会按参与者重写。
SCENE_TITLE_PREFIX = "视频场景"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class VideoSceneExtraction:
    """一次抽取的结果。三个计数分开，是因为它们回答的是三个不同的问题：

    - ``scene_count``：这次**确保存在**的场景事件数（幂等复用的也算）；
    - ``keyframe_count``：这次**新建**的派生帧数；
    - ``reused``：命中原有帧、跳过的场景数（幂等的可观测面）。
    """

    scene_count: int = 0
    keyframe_count: int = 0
    reused: int = 0
    reason: str = ""
    # 各阶段墙钟耗时（秒）。关键帧提取与关键帧记忆构建必须分开：前者是 ffmpeg I/O，
    # 后者才会调用视觉/VLM/人脸/实体/事实流水线，把两者揉成一个数无法判断瓶颈。
    timings: dict[str, float] = field(default_factory=dict)


class ExtractVideoScenes:
    def __init__(
        self,
        *,
        assets: AssetRepository,
        events: EventRepository,
        places: AssetPlaceRepository,
        video_repository: VideoRepository,
        keyframes: VideoKeyframeExtractor,
        # 关键帧走的那条图片流水线。**传可调用对象而不是 ImportAssets 实例**：
        # 这一层要的是「把一张已落库的图片走完整条链」，不是导入用例本身
        # （它还要负责去重、落盘、探测）。第二参是 ``forced_event_id``。
        process_image: Callable[[Asset, str], object],
        scene_planner: VideoScenePlanner | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._assets = assets
        self._events = events
        self._places = places
        self._video_repository = video_repository
        self._keyframes = keyframes
        self._process_image = process_image
        self._scene_planner = scene_planner
        self._clock = clock

    def execute(self, source_asset_id: str) -> VideoSceneExtraction:
        total_started = time.perf_counter()
        asset = self._assets.get(source_asset_id)
        if asset is None or asset.media_type is not MediaType.VIDEO:
            return VideoSceneExtraction(
                reason="not_a_video", timings={"total": round(time.perf_counter() - total_started, 3)}
            )

        probe = self._video_repository.probe(source_asset_id)
        duration = (probe.metadata.duration_sec if probe and probe.metadata else 0.0) or 0.0
        planning_started = time.perf_counter()
        windows = (
            self._scene_planner(Path(asset.path), asset.id, duration)
            if self._scene_planner is not None
            else plan_scenes(duration)
        )
        planning_seconds = time.perf_counter() - planning_started
        if not windows:
            # 探测没读到时长（ffprobe 失败 / 还没探测过）。**不猜时长**：编一个只会
            # 得到「场景数量看着对、时间全是假的」，而那是没人能看出来的错误。
            return VideoSceneExtraction(
                reason="no_duration",
                timings={
                    "scene_planning": round(planning_seconds, 3),
                    "total": round(time.perf_counter() - total_started, 3),
                },
            )

        place = self._places.get(source_asset_id)
        place_text = place.display() if place else UNKNOWN_PLACE
        existing = {item.source_frame_index for item in self._video_repository.list_derived(source_asset_id)}

        scene_count = keyframe_count = reused = 0
        scene_write_seconds = 0.0
        keyframe_extract_seconds = 0.0
        keyframe_asset_write_seconds = 0.0
        keyframe_memory_seconds = 0.0
        for window in windows:
            scene_started = time.perf_counter()
            event = self._ensure_scene_event(asset, window, place_text)
            scene_write_seconds += time.perf_counter() - scene_started
            scene_count += 1
            if window.index in existing:
                reused += 1
                continue
            extract_started = time.perf_counter()
            frame = self._keyframes.extract(
                Path(asset.path), asset.id, window.index, window.keyframe_timestamp_sec
            )
            keyframe_extract_seconds += time.perf_counter() - extract_started
            asset_write_started = time.perf_counter()
            derived = self._derived_asset(asset, frame, window)
            self._video_repository.add_derived(
                parent_asset_id=asset.id,
                asset=derived,
                derived_kind=KEYFRAME_DERIVED_KIND,
                source_timestamp_sec=window.keyframe_timestamp_sec,
                source_frame_index=window.index,
                source_scene_index=window.index,
            )
            keyframe_asset_write_seconds += time.perf_counter() - asset_write_started
            memory_started = time.perf_counter()
            self._process_image(derived, event.id)
            keyframe_memory_seconds += time.perf_counter() - memory_started
            keyframe_count += 1

        return VideoSceneExtraction(
            scene_count=scene_count,
            keyframe_count=keyframe_count,
            reused=reused,
            timings={
                "scene_planning": round(planning_seconds, 3),
                "scene_write": round(scene_write_seconds, 3),
                "keyframe_extraction": round(keyframe_extract_seconds, 3),
                "keyframe_asset_write": round(keyframe_asset_write_seconds, 3),
                "keyframe_memory": round(keyframe_memory_seconds, 3),
                "total": round(time.perf_counter() - total_started, 3),
            },
        )

    # ---- 内部 ----------------------------------------------------------------

    def _ensure_scene_event(self, asset: Asset, window: SceneWindow, place_text: str) -> EventRecord:
        """1.0 ``db.py:2397`` 的 ``create_video_scene_event``：同一 (源片, 场景序号) 只有一条。

        区间是**原片秒数**，与事件自身的 ``time_start`` / ``time_end``
        （拍摄时间加偏移）是两套坐标：前者给前端 seek，后者给时间线与事件归并。
        """
        start = window.start_sec
        end = window.end_sec
        return self._events.create_video_scene(
            event_id=_new_id("evt"),
            scope_id=asset.scope_id,
            title=f"{SCENE_TITLE_PREFIX} {window.index + 1}",
            summary=f"{SCENE_TITLE_PREFIX} · {start:.1f}s~{end:.1f}s",
            place=place_text,
            time_start=_offset(asset.captured_at, start),
            time_end=_offset(asset.captured_at, end),
            source_asset_id=asset.id,
            source_scene_index=window.index,
            source_start_sec=start,
            source_end_sec=end,
        )

    def _derived_asset(self, source: Asset, frame: Path, window: SceneWindow) -> Asset:
        """派生帧的 Asset。**血缘列不在这里**——它们由 ``add_derived`` 写进迁移 0010 的列。

        拍摄时间/GPS 继承源片（1.0 ``processor.py:186`` 的 ``frame_captured_at`` /
        ``captured_location``）：关键帧是原片在这一秒的一瞥，时间与地点跟原片同源，
        只是时刻往前挪了 ``source_timestamp_sec``。
        """
        now = self._clock()
        return Asset(
            id=_new_id("asset"),
            scope_id=source.scope_id,
            file_name=frame.name,
            media_type=MediaType.IMAGE,
            path=str(frame),
            mime_type="image/jpeg",
            size_bytes=frame.stat().st_size,
            status=AssetStatus.PROCESSED,
            content_sha256=_sha256(frame),
            captured_at=_offset(source.captured_at, window.keyframe_timestamp_sec),
            captured_location=source.captured_location,
            created_at=now,
            updated_at=now,
        )


def _offset(captured_at: datetime | None, seconds: float) -> datetime | None:
    """拍摄时间加原片内偏移。源片没有拍摄时间时返回 ``None``——不拿导入时刻冒充。"""
    if captured_at is None:
        return None
    return captured_at + timedelta(seconds=float(seconds))
