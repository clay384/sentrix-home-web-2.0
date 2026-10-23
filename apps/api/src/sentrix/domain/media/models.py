"""Media domain model.

纯数据与规则，零 IO。字段语义取自 1.0 ``backend/db.py`` 的 assets / observations
建表语句，但去掉了 1.0 混在里面的大量派生字段（那些属于观察结果，不属于资产）。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class MediaType(StrEnum):
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    TEXT = "text"

    @classmethod
    def from_upload(cls, content_type: str | None, file_name: str | None) -> MediaType:
        mime = (content_type or "").split(";", 1)[0].strip().lower()
        for prefix, media in (
            ("image/", cls.IMAGE),
            ("audio/", cls.AUDIO),
            ("video/", cls.VIDEO),
            ("text/", cls.TEXT),
        ):
            if mime.startswith(prefix):
                return media
        return cls.from_file_name(file_name or "")

    @classmethod
    def from_file_name(cls, file_name: str) -> MediaType:
        suffix = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
        if suffix in {"jpg", "jpeg", "png", "heic", "heif", "webp", "gif", "tif", "tiff"}:
            return cls.IMAGE
        if suffix in {"mov", "mp4", "m4v", "avi", "mkv"}:
            return cls.VIDEO
        if suffix in {"wav", "mp3", "m4a", "aac", "flac", "ogg"}:
            return cls.AUDIO
        return cls.TEXT


class AssetStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class GpsPoint:
    latitude: float
    longitude: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.latitude) or not -90 <= self.latitude <= 90:
            raise ValueError(f"latitude 越界: {self.latitude}")
        if not math.isfinite(self.longitude) or not -180 <= self.longitude <= 180:
            raise ValueError(f"longitude 越界: {self.longitude}")

    def as_text(self) -> str:
        """1.0 把 GPS 存成 ``captured_location`` 文本列，沿用该表示以兼容检索。"""
        return f"{self.latitude:.6f},{self.longitude:.6f}"

    def distance_km(self, other: GpsPoint) -> float:
        """大圆距离（公里）。1.0 ``backend/db.py:297 gps_distance_km``，地球半径一值不差。

        事件候选的 GPS 衰减、地点解析的最近邻都读它，所以放在点上而不是某一边的
        工具模块里——两点之间的距离是点自己的性质。
        """
        latitude_one, longitude_one = math.radians(self.latitude), math.radians(self.longitude)
        latitude_two, longitude_two = math.radians(other.latitude), math.radians(other.longitude)
        delta_latitude = latitude_two - latitude_one
        delta_longitude = longitude_two - longitude_one
        a = (
            math.sin(delta_latitude / 2) ** 2
            + math.cos(latitude_one) * math.cos(latitude_two) * math.sin(delta_longitude / 2) ** 2
        )
        return 6371.0088 * 2 * math.asin(math.sqrt(a))


def _gps_point(latitude: float | None, longitude: float | None) -> GpsPoint | None:
    """经纬度 -> ``GpsPoint``。**两个都解析出来才算**——与 ``PillowMetadataReader``
    同一口径：EXIF/容器标签都允许只标一半，那半边不该冒充一个完整坐标。

    视频与音频两处元数据各要一份，抽成模块级函数而不是各写一遍。
    """
    if latitude is None or longitude is None:
        return None
    try:
        return GpsPoint(latitude, longitude)
    except ValueError:
        return None


def _captured_datetime(text: str | None) -> datetime | None:
    """适配器已把拍摄时间规整成 ISO-8601（含 ``+08:00`` 这类偏移），这里只负责解析。

    **带偏移的一律转成本地时间、再去掉 tzinfo**——这不是随手归一化，是与 1.0 对齐，
    而且不这么做会让视频整条链路挂掉（2026-10-07 实测）：

    观测的 ``captured_at`` 会被拿去减 ``events.time_start``（``domain/memories/events.py``
    的 ``within_candidate_window`` / ``_time_score``）。照片那条路的时间来自 EXIF，
    是 naive；视频这条来自 ffprobe 的 ``creation_time``，带 ``Z`` → 被
    ``normalized_datetime`` 转成 ``+00:00`` → **aware**。aware 减 naive 直接抛
    ``TypeError: can't subtract offset-naive and offset-aware datetimes``，
    于是 10 个视频**全部**导入失败，而 ``photo_import`` 只记 ``partial``。

    1.0 存的是 ``2017-09-27 11:36:15``（同一个 UTC 03:36 的视频）——本地时间、无时区。
    这里照做：``astimezone()`` 转本机时区，再 ``replace(tzinfo=None)``。
    """
    if not text:
        return None
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    return value.astimezone().replace(tzinfo=None) if value.tzinfo is not None else value


# 视频拍摄时间/GPS 的抽取结果。字段逐个照抄 1.0 ``backend/video/contracts.py`` 的
# ``VideoMetadata``（``probe_video_metadata`` 的返回形状），只有整包 ``raw`` 不搬。
#
# **``raw`` 不搬的理由在 2026-10-05 被修正。** S8 原先的判词是「1.0 写进
# ``metadata_json.video_metadata`` 之后没有任何读侧用过」——那句话只查了主产品读侧，
# 漏了另一个服务：PhotoBench 的关键帧分析（``services/photobench/backend/
# benchmark_orchestrator.py`` 的 ``get_keyframe_analysis``）从 ``raw.streams[].nb_frames``
# 取原片总帧数算采样间隔（``:5724``）。整包 raw 仍不搬（几十 KB 的流/标签树，只有一个
# 读者），**只把它真正要用的那一个整数搬成 ``frame_count``**。
#
# **它落在 domain 而不是 infrastructure**：形状是领域词汇（时长/编码/旋转角），
# 谁去读那个文件（ffprobe 子进程）才是 infrastructure 的事。
@dataclass(frozen=True, slots=True)
class VideoMetadata:
    captured_at: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    captured_location: str | None = None
    duration_sec: float = 0.0
    fps: float = 0.0
    width: int = 0
    height: int = 0
    codec: str = ""
    rotation: int = 0
    device: str = ""
    creation_source: str = ""
    # 原片总帧数，取自 ffprobe 视频流的 ``nb_frames``。1.0 它埋在整包 ``raw`` 里，
    # 唯一读者是 PhotoBench 的关键帧分析（``benchmark_orchestrator.py:5724``，1.0 的
    # 容错口径是 ``int(frames or 0)`` 且缺了就当 0）。**缺了就是 0，不是错误**：
    # 很多容器根本不给 ``nb_frames``（见 ``infrastructure/media/video_metadata.py``）。
    frame_count: int = 0

    def gps(self) -> GpsPoint | None:
        """经纬度 -> ``GpsPoint``，两个都解析出来才算。"""
        return _gps_point(self.latitude, self.longitude)

    def captured_datetime(self) -> datetime | None:
        return _captured_datetime(self.captured_at)


# 音频拍摄时间/GPS + 时长/编码/采样率/声道/码率的抽取结果。
#
# **它单列而不是复用 ``VideoMetadata``**：形状不同——硬塞进同一个类型会让两边各背
# 一半恒空字段（音频恒没有 width/height/fps/rotation，视频恒没有
# sample_rate/channels/bit_rate）。与 S8a 单列 ``VideoMetadataReader``（而不是扩展
# EXIF 的 ``MetadataReader`` 三元组）是同一条理由。
#
# **1.0 没有对应物**：1.0 的 ``probe_video_metadata`` 遇到没有视频流的文件直接抛
# ``ValueError``（``video/metadata.py:52``），音频的时长/编码从来没落过库。
# 2.0 这一份是用户 2026-10-04 裁决「音频只做入库与播放」里「能读元数据（时长等）」的落地。
@dataclass(frozen=True, slots=True)
class AudioMetadata:
    captured_at: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    captured_location: str | None = None
    duration_sec: float = 0.0
    codec: str = ""
    sample_rate: int = 0
    channels: int = 0
    bit_rate: int = 0
    creation_source: str = ""

    def gps(self) -> GpsPoint | None:
        """经纬度 -> ``GpsPoint``，两个都解析出来才算（与 ``VideoMetadata`` 共用同一条规则）。"""
        return _gps_point(self.latitude, self.longitude)

    def captured_datetime(self) -> datetime | None:
        return _captured_datetime(self.captured_at)


# 一条派生物的血缘切片。派生物**本来就是 ``media_type='image'`` 的普通资产**
# （1.0 ``video/processor.py:188``），这里只把「怎么指回原片」那几个列（迁移 0010）带出来。
# S8a 读它（``GET /video-scenes/{id}`` 的 ``keyframe_assets``），S8b 写它。
@dataclass(frozen=True, slots=True)
class DerivedAsset:
    id: str
    file_name: str
    media_type: str
    status: str
    captured_at: str | None
    captured_location: str | None
    parent_asset_id: str | None
    derived_kind: str | None
    source_timestamp_sec: float | None
    source_frame_index: int | None
    source_scene_index: int | None


@dataclass(frozen=True, slots=True)
class Asset:
    id: str
    scope_id: str
    file_name: str
    media_type: MediaType
    path: str
    mime_type: str | None
    size_bytes: int
    status: AssetStatus
    content_sha256: str
    captured_at: datetime | None
    captured_location: GpsPoint | None
    created_at: datetime
    updated_at: datetime
    # 关键帧血缘（迁移 0010）。派生物**本来就是 ``media_type='image'`` 的普通资产**
    # （1.0 ``video/processor.py:188``），没有这三列就与真照片分不开。
    # 1.0 的 ``/api/assets`` 是 ``SELECT * FROM assets``（``db.py:1505``），顺带把这几列
    # 一起回了；2.0 的 ``AssetSummary`` 是窄投影，S8 只查主产品读侧、漏了 PhotoBench——
    # 它靠 ``derived_kind`` / ``parent_asset_id`` 认出「这张图是某段视频的派生帧」，
    # 靠 ``source_timestamp_sec`` 算采样间隔与覆盖跨度（``benchmark_orchestrator.py:5712/5798``）。
    # 所以它们回到 ``Asset`` 上，并在 ``/assets``（列表与单条）一并暴露。
    # 默认 ``None``：普通照片本就没有血缘（见 ``SqliteAssetRepository.add`` 的插入列）。
    derived_kind: str | None = None
    parent_asset_id: str | None = None
    source_timestamp_sec: float | None = None
    external_source_id: str | None = None
    source_timestamp: str | None = None
    source_location: str = ""
    source_metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.media_type, MediaType):
            raise ValueError(f"未知 media_type: {self.media_type!r}")


# 1.0 ``model_clients.py:1190``：``normalize_fact_confidences(parsed.get("facts"), 0.65)``。
# 模型没给 confidence 时用它。它在**观察器**这一层，与 ``maintain_facts`` 那一层
# 处理「观察整体置信度」的兜底不是同一个数。
MODEL_FACT_CONFIDENCE = 0.65

# 1.0 ``model_clients.py`` 的提示词原文：``facts 最多8项``。
MODEL_FACT_LIMIT = 8


@dataclass(frozen=True, slots=True)
class ObservationFact:
    """模型自报的一条候选事实。1.0 ``model_clients.py`` 的 ``facts`` 数组元素。

    **候选由模型自报，写法由规则决定**（用户裁决④）。这两句不矛盾，是两件事：
    模型只负责说「画面上这个人好像去过赵县」，至于这条候选落成 ``active`` 还是
    ``pending``、要不要合并证据、要不要记 ``supersedes``，全在
    ``domain/memories/facts.py`` 的 ``decide_fact_write`` 里。

    形状定义在 media 而不是 memories，是因为它**是观察的一部分**（跟着
    ``ObservationSemantics`` 一起从观察器出来、跟着 Observation 一起落库）；
    ``domain/memories/events.py`` 反过来要 import ``GpsPoint``，两边互相 import 就成环了。
    """

    subject: str
    predicate: str
    object: str
    confidence: float = MODEL_FACT_CONFIDENCE


@dataclass(frozen=True, slots=True)
class Observation:
    """一次导入对资产抽取出的观察结果。

    S4 把 1.0 散在 ``observations.*_json`` 里的语义字段提升成正式成员：词法索引与
    事件打分都直接读它们，读之前先去解析一个 JSON blob 是 1.0 反复出错的地方
    （``MetadataRetriever`` 的 place 预筛就因为拿到的是 JSON 字符串而整条失效）。

    ``revision`` 是**词法投影的过期依据**：同一条 Observation 被重新富集时要能区分
    「同一版本重跑」与「新版本覆盖」，1.0 没有这个字段，投影只能删了重插。
    """

    id: str
    scope_id: str
    asset_id: str
    source_type: str
    captured_at: datetime | None
    confidence: float
    created_at: datetime
    updated_at: datetime
    caption: str = ""
    place: str = ""
    activity: str = ""
    event_type: str = ""
    ocr_text: str = ""
    objects: tuple[str, ...] = ()
    clothing: tuple[str, ...] = ()
    # 这条观察里**已确认**的人的名字快照。写入者是 ``EntityRepository.link_confirmed_mention``
    # （跟着提及一起写），读它的是词法投影的 ``person_bridge`` 字段。存名字而不是 id：
    # 这一列是给人看的检索面，按 id 建词法投影等于让人去搜 ``ent_ab12cd34``。
    people: tuple[str, ...] = ()
    # 模型自报的候选事实（1.0 ``observations`` 上没有这一列，它们只活在
    # ``assets.metadata_json["fact_ids"]`` 与那次调用的内存里）。2.0 单开一列而不是塞进
    # ``detail_json``：事实是**有结构**的东西，``subject/predicate/object`` 三个字段直接
    # 被 ``is_writable_fact`` 与 ``decide_fact_write`` 读，摊平进一个无结构 blob 之后
    # 那两个纯规则就只能靠字符串约定去取字段了。
    facts: tuple[ObservationFact, ...] = ()
    revision: int = 1
    source_text: str = ""

    def __post_init__(self) -> None:
        if self.revision < 1:
            raise ValueError(f"revision 从 1 开始，收到 {self.revision}")

    @property
    def has_semantics(self) -> bool:
        """VLM 语义是否已经落上。1.0 用「caption 非空」当这个判据。"""
        return bool(self.caption or self.place or self.activity or self.event_type or self.source_text)
