"""Domain ports and format rules.

格式支持与转码判定是**领域规则**（产品要求浏览器能打开原图），所以规则本身留在
domain；具体怎么解码由 infrastructure 实现。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from sentrix.domain.media.models import (
    Asset,
    AssetStatus,
    AudioMetadata,
    DerivedAsset,
    GpsPoint,
    MediaType,
    Observation,
    ObservationFact,
    VideoMetadata,
)
from sentrix.domain.media.scenes import SceneWindow

SUPPORTED_IMAGE_SUFFIXES = frozenset(
    {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".tif", ".tiff"}
)
# 浏览器无法原生渲染，必须服务端转码后再交付。
BROWSER_UNRENDERABLE_SUFFIXES = frozenset({".heic", ".heif"})
BROWSER_UNRENDERABLE_MIMES = frozenset({"image/heic", "image/heif"})


def needs_transcode(file_name: str, mime_type: str | None) -> bool:
    suffix = Path(file_name).suffix.lower()
    mime = (mime_type or "").split(";", 1)[0].strip().lower()
    return suffix in BROWSER_UNRENDERABLE_SUFFIXES or mime in BROWSER_UNRENDERABLE_MIMES


# 音频容器 → 交付用的 content-type。**浏览器这些都能原生播，2.0 一个都不转**
# （用户裁决②「音频不转写，只做入库与播放」；与视频的 H.264 直出是同一条思路）。
# 后缀与 ``MediaType.from_file_name`` 认的音频那一组逐字一致。
AUDIO_MIME_BY_SUFFIX = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
}
# 上传方（curl / 整册导入）常常给不出音频的具体类型，落到这个通用类型上。
GENERIC_MIME = "application/octet-stream"


def audio_mime_type(file_name: str, declared: str | None) -> str:
    """音频交付用的 content-type：上传方给了具体的 ``audio/*`` 就用它的，否则按后缀推。

    1.0 直接把 ``mime_type`` 交出去（``app.py:2463``），而上传方不给类型时它是
    ``application/octet-stream``——S8a 的验收在视频 ``?original=1`` 上观察到过同一现象。
    音频没有「转码件」这条退路（不转码），交出去的**就是原文件**，所以类型错了浏览器
    就没有第二次机会。把判定收在后端（后缀是权威）而不是指望上传方，是这个函数存在的理由。
    """
    mime = (declared or "").split(";", 1)[0].strip().lower()
    if mime.startswith("audio/"):
        return declared or mime
    suffix = Path(file_name).suffix.lower()
    return AUDIO_MIME_BY_SUFFIX.get(suffix) or declared or GENERIC_MIME


class SourceIdentityCollision(Exception):
    """Another import already owns this scope and external source identity.

    The repository reports the winner; the import use case decides whether its
    content and source metadata make this an idempotent revisit or a conflict.
    """

    def __init__(self, existing: Asset) -> None:
        self.existing = existing
        super().__init__("The external source identity already exists")


class AssetRepository(Protocol):
    def add(self, asset: Asset) -> None:
        """Insert an asset, raising SourceIdentityCollision for a source-ID race."""
        ...

    def get(self, asset_id: str) -> Asset | None: ...

    def list_by_scope(self, scope_id: str | None, limit: int) -> Sequence[Asset]:
        """``scope_id`` 为 ``None`` 表示**跨全部相册**（1.0 ``db.py:1514`` 的 ``if scope_id:``）。

        这不是「默认相册」——空与 ``"home-default"`` 是两件事：前者是用户在界面上选了
        「全部相册」，后者是一个真叫 home-default 的空间。
        """
        ...

    def find_by_hash(self, scope_id: str, content_sha256: str) -> Asset | None: ...

    def find_by_external_source_id(self, scope_id: str, external_source_id: str) -> Asset | None: ...

    def update_status(self, asset_id: str, status: AssetStatus) -> None: ...


class ObservationRepository(Protocol):
    def add(self, observation: Observation) -> None: ...

    def get(self, observation_id: str) -> Observation | None: ...

    def list_by_asset(self, asset_id: str) -> Sequence[Observation]: ...

    def list_scope(self, scope_id: str, *, limit: int) -> Sequence[Observation]: ...

    def enrich(self, observation_id: str, *, semantics: ObservationSemantics) -> Observation: ...

    def latest_ocr_text(self, asset_id: str) -> str:
        """这张照片**最近一次**观察里已经存下来的图内文字。没有就是空串。

        1.0 ``agent_runtime/ocr_tool.py:544-548``：

            SELECT ocr_text FROM observations WHERE asset_id = ?
            ORDER BY updated_at DESC LIMIT 1

        **这是 ``read_photo_text`` 的第一条路，而在 1.0 它也是第一条路**
        （在任何 OCR 引擎之前，命中就返回、零推理）——2.0 的导入期
        ``OpenAIVlmObserver`` 本来就往这一列写「图中文字，1000 字内」，
        所以这一路在 2.0 一直有数据。

        与 ``latest_people_snapshot`` 是两条单列读取，看着像重复，**但不是**：
        ``ocr_text`` 经 ``row_to_observation`` 解析后**原样保留**，所以它本来就能从
        ``Observation.ocr_text`` 拿到；``people_json`` 里那位 ``entity_id``
        **恰好被解析丢掉了**，才需要一条旁路。这一条留在这里是为了让「取最新那条」
        这个口径只有一处（1.0 也是 ``updated_at DESC LIMIT 1``）。
        """
        ...

    def latest_people_snapshot(self, asset_id: str) -> Sequence[PeopleSnapshotEntry]:
        """这张照片**最近一次**观察里的人物快照，带绑定状态（T122 用）。

        1.0 ``tools.py:2578-2581``：``SELECT people_json FROM observations
        WHERE asset_id = ? ORDER BY updated_at DESC LIMIT 1``。取最近一次是 1.0 的
        口径——一张照片可以有多条观察（换过模型、补全过），人名快照以最新那条为准。

        没有观察行时返回**空元组**，与「有一条观察但里面没有人」是同一个结果——
        1.0 也是这么收的（``observation`` 为 ``None`` 时 ``people_values = []``）。
        调用方要区分这两件事的话得自己再查，但那条链路上没有任何地方需要区分。
        """
        ...


@dataclass(frozen=True, slots=True)
class PeopleSnapshotEntry:
    """``observations.people_json`` 里的一条，**原样**。

    写入侧（``entities.py`` 的 ``_append_person_snapshot``）存的是
    ``{"entity_id":…, "name":…, "status":…}``，而读取侧一直只取 ``name``
    （``_json_people``）——「**绑没绑**」这一位在 ``Observation.people``
    （``tuple[str, ...]``）里被丢掉了。

    这个 DTO 存在的唯一理由是把它捡回来：1.0 数「未确认同行者」靠的正是
    ``people_json`` 里**没有** ``entity_id`` 的条目（``tools.py:2582-2588``），
    而那个数字是要回给模型的一句话，也是「不要给未绑定的人安名字」这条纪律的落点。

    **它是只读的旁路，不是 ``Observation.people`` 的替代品**（2026-09-30 裁决）：
    ``Observation.people`` 是 S4 定下的形状，词法投影 / 事件锚点 / 参与者投影三处都读它，
    为一个工具去改它等于让 S4 的模型背 S6 的需求。绑定状态只在这一个入口可见。
    """

    name: str
    # ``""`` 表示这一条**没有**绑定实体。1.0 的判据是 ``person.get("entity_id")``
    # 的真假，所以空串与缺失是同一件事——这里用一个值表达，不留 ``None``：
    # 调用方不该去区分「键不在」与「键是空串」，1.0 也没区分过。
    entity_id: str = ""


@dataclass(frozen=True, slots=True)
class ObservationSemantics:
    """一次语义富集写进 Observation 的字段集合。

    单独一个 DTO 而不是一长串关键字参数：``enrich`` 的调用方（VLM 观察器与测试假件）
    要能一次把整包语义构造出来，也方便断言「写了哪些、没写哪些」。
    ``revision`` 由仓储自增，不由调用方给——它是版本号，不是内容。
    """

    caption: str = ""
    place: str = ""
    activity: str = ""
    event_type: str = ""
    ocr_text: str = ""
    objects: tuple[str, ...] = ()
    clothing: tuple[str, ...] = ()
    # 模型自报的候选事实。1.0 的 ``analyze_image`` 让模型回一个 ``facts`` 数组
    # （``model_clients.py:1130-1198`` 的提示词原文），2.0 的 S4 明确不搬它，
    # 于是 ``MaintainFacts`` 的候选在 S4 的生产装配下恒为空元组——门禁全开也写不出一条
    # 事实。用户裁决④选了「按 1.0 原样：模型自报」，这个字段就是那条来源。
    facts: tuple[ObservationFact, ...] = ()
    confidence: float = 0.0
    source_text: str = ""


class VlmObserver(Protocol):
    """一张图 → 一次视觉观察。**最多 3 次模型调用**（首轮 / 补全 / 中文化），串行。

    1.0 的三段结构在 ``backend/model_clients.py:1130-1198`` 的 ``analyze_image``。
    2.0 保留同一结构与同一上限，但只落 ``ObservationSemantics`` 的七个字段——
    1.0 的 semantic 受控词表与 ``detail`` 属于 S6 的精读。

    模型不可用、读不了图、返回的不是 JSON——一律返回 ``None``。观察是**增强**，
    不是导入的前置条件。
    """

    @property
    def model_id(self) -> str: ...

    def observe(self, path: Path) -> ObservationSemantics | None: ...


class PhotoInspector(Protocol):
    """拿一个问题去问一张图。实现在 ``infrastructure/media/photo_inspector.py``。

    **为什么另起一个端口，而不是给 ``VlmObserver`` 加一个 ``question`` 形参。**
    ``VlmObserver.observe`` 是**导入期**的观察器：schema 固定七个字段、跑一次就落库、
    每次导入最多占三次推理。它问的是「这张图里有什么」，与用户当场问的那句话无关——
    而 ``ObservationSemantics`` 那七个字段是**产品要落库的形状**，不是模型能回答的形状；
    拿它去装「这张合影里谁戴了帽子」，只会得到一个恒空的字段。

    ``OpenAIPersonVision`` 也不是它：那是画像 / 瞬间 / 外观三个**专用**入口，各自有各自的
    输出形状与各自的词表。这个端口问的是**用户想知道的那件事**——1.0 的 ``inspect_photo``
    （T123）与 ``read_photo_text``（T124）各自只决定往槽位里填什么、以及怎么解析回来的
    那包 JSON（见 ``domain/media/inspection.py``）。

    与 ``PersonVision`` 同一条口径：模型不可用、读不了图、返回的不是 JSON——一律返回
    ``None``，一次都不抛。工具靠 ``None`` 决定降级，而不是靠 catch。

    **``crop`` 的存在是 1.0 的形状，不是预留。** 指名了目标人物时，1.0 发的是**两张**图：
    裁剪件在前、原图在后（``tools.py:2192`` 的 ``inspect_images.insert(0, crop)``），
    提示词里那句「第一张图（如有）是该人物的定位裁剪图」才成立。``crop`` 读不了就
    **退回只发原图**，不让整次检查失败——1.0 的裁剪块自带 ``except (OSError, ValueError,
    TypeError)`` 并落到 ``target_status = "not_located"``（``tools.py:2186-2200``）。

    ``faces`` 是**原样送进提示词**的人脸清单，本端口一个字段都不改写——见
    ``inspection.render_face_manifest`` 的 docstring。
    """

    @property
    def model_id(self) -> str: ...

    def inspect(
        self,
        path: str | Path,
        question: str,
        *,
        faces: Sequence[Mapping[str, object]] = (),
        target_person: str = "",
        crop: str | Path | None = None,
    ) -> dict[str, object] | None: ...


class AssetObserver(Protocol):
    """把一张已导入的 Asset 变成一次语义观察，并把结果写回 Observation。

    与 ``AssetVisualIndexer`` / ``AssetPlaceIndex`` 同构：导入用例依赖这个抽象，
    不依赖具体的观察器与仓储。

    ``prepare`` / ``commit`` 是同一件事的两段（与 ``AssetVisualIndexer`` 同形）：
    ``prepare`` 只打模型（最贵的一步、可并行），``commit`` 才写回观察，
    ``observe`` 是两者的复合。
    """

    def prepare(self, asset: Asset) -> ObservationSemantics | None:
        """只打模型（8100，最多 3 次调用），**不写回观察**。"""
        ...

    def commit(self, observation: Observation, semantics: ObservationSemantics | None) -> Observation | None:
        """把观察结果写回。``semantics`` 为空就什么都不写——模型不可用不是导入失败。"""
        ...

    def observe(self, asset: Asset, observation: Observation) -> Observation | None: ...


class MediaStorage(Protocol):
    def store(self, asset_id: str, file_name: str, content: bytes) -> Path: ...

    def read(self, asset_id: str) -> bytes: ...

    def path_for(self, asset: Asset) -> Path: ...


class MetadataReader(Protocol):
    """从原始文件中抽取拍摄时间、GPS 与设备。"""

    def read(self, path: Path) -> tuple[datetime | None, GpsPoint | None, str | None]: ...


class PreviewEncoder(Protocol):
    """把原图编码成浏览器可直接渲染的 JPEG。

    ``crop_jpeg`` 也在这里而不是另立一个端口：人脸裁剪与预览是同一件事的两个取景
    ——打开一张图、按 EXIF 转置、编码成 JPEG。分开会得到两个各自压一次参数的适配器，
    而它们必须用同一套转置口径，否则簇里的样本脸会与预览里的位置对不上。
    """

    def encode_jpeg(self, path: Path, *, max_dimension: int, quality: int) -> bytes: ...

    def crop_jpeg(
        self, path: Path | str, bbox: Sequence[float], *, max_dimension: int, quality: int
    ) -> bytes: ...


class MediaTypeResolver(Protocol):
    def resolve(self, content_type: str | None, file_name: str | None) -> MediaType: ...


class VideoMetadataReader(Protocol):
    """从视频文件里读出时长/帧率/编码/旋转角与拍摄时间、GPS（走 ffprobe）。

    **为什么单列一个端口，而不是扩展 ``MetadataReader``。** 后者的返回形状是
    ``(拍摄时间, GPS, 设备)`` 三元组——那是 EXIF 的形状。视频多出来的时长/帧率/编码/
    旋转角没有地方放，硬塞进同一个元组会让 EXIF 那条路也背上四个它永远为空的字段。
    而且两者的失败语义不同：EXIF 读不出来降级成 ``None``（图片照样导入），
    视频的探测结果本身就是导入要交付的那份元数据。

    实现在 ``infrastructure/media/video_metadata.py``：ffprobe 是子进程，
    ``domain-purity`` 与 ``application-purity`` 两条契约都禁止 domain/application 碰它。
    """

    def read(self, path: Path) -> VideoMetadata:
        """读不出来时抛 ``ValueError``（ffprobe 非零退出 / 没有视频流）。

        不返回 ``None``：那会让「这个文件探测不了」与「调用方忘了接线」变成同一个结果。
        调用方（``ImportAssets``）接住它并把原因写进 ``metadata_json.video_probe_error``，
        于是失败是看得见的——S8a 要修的正是「收下了但什么都没发生」。
        """
        ...


class VideoTranscoder(Protocol):
    """把浏览器放不了的视频转成 H.264 mp4。1.0 ``processor.py:58-69`` 的 ``_browser_preview``。"""

    def browser_preview(self, asset_id: str, source: Path, codec: str) -> Path | None:
        """非 H.264 才转；已经是 H.264 时返回 ``None``（**返回 None 是「不用转」，不是失败**）。

        转码失败抛异常：调用方记下 ``browser_preview_error`` 后回落原片，
        与 1.0 的 ``preview_error`` 同一条路（``processor.py:112-117``）。
        """
        ...


class VideoKeyframeExtractor(Protocol):
    """在给定的原片秒数上取一帧写成图片。实现在 ``infrastructure/media/video_keyframes.py``。

    **为什么单列一个端口。** 它起 ``ffmpeg`` 子进程；``domain-purity`` 与
    ``application-purity`` 两条契约都禁止 domain/application 碰子进程。形状与
    ``VideoTranscoder`` 同一条：端口在 domain，实现在 infrastructure。

    **抽什么帧由 domain 决定，不在这里。** 切窗规则（``SCENE_WINDOW_SEC`` /
    ``MAX_SCENES``）在 ``domain/media/scenes.py``——那是产品行为，与「怎么解码」无关。
    这个端口只回答「第 N 秒那一帧，请你给我」。
    """

    def extract(self, source: Path, asset_id: str, scene_index: int, timestamp_sec: float) -> Path:
        """把 ``source`` 第 ``timestamp_sec`` 秒那一帧写成图片，返回产物路径。

        产物路径由适配器自己拼（与 ``VideoTranscoder.browser_preview`` 同一个根：
        ``media_dir/derived/video``），调用方不必知道派生目录长什么样。

        失败抛异常（ffmpeg 非零退出 / 产物没写出来）。调用方决定是整条放弃还是跳过——
        ``ExtractVideoScenes`` 选的是整条放弃：半套场景比没有场景更难解释。
        """
        ...


class VideoScenePlanner(Protocol):
    """Plan scene windows and selected keyframe timestamps for a video."""

    def __call__(self, video_path: Path, video_id: str, duration_sec: float) -> Sequence[SceneWindow]: ...


@dataclass(frozen=True, slots=True)
class VideoProbe:
    """一次视频探测的落库形态（写进 ``assets.metadata_json``）。

    前三个字段对应 1.0 写进去的三个键（``processor.py:118-121``）：
    ``video_metadata`` / ``browser_preview_path`` / ``browser_preview_error``。
    **``probe_error`` 是 2.0 加的**：1.0 的 ffprobe 失败会把整条资产标成 ``failed``；
    2.0 选择「导入照收、把原因留在资产上」，那就得有地方放那个原因——
    否则失败又变成不可见（1.0 的 ``error``/``error_stage`` 是分散在资产行上的）。
    """

    metadata: VideoMetadata | None = None
    browser_preview_path: str = ""
    browser_preview_error: str = ""
    probe_error: str = ""
    # 整个视频阶段的秒数，1.0 写作 ``metadata_json.video_processing_seconds``
    # （``processor.py:205`` / ``:420``），由 ``ImportAssets.execute`` 计时后经
    # ``save_processing_seconds`` 落库。**不在 ``save_probe`` 那一次写**：计时范围要包含
    # 场景抽取，而场景抽取排在 ``save_probe`` 之后（见 ``import_assets.py``）。
    processing_seconds: float | None = None


class VideoRepository(Protocol):
    """视频血缘的读写：探测结果的落库与回读、派生物的查询与清场。

    **为什么与 ``AssetRepository`` 分开。** 这几个方法全都围绕「派生物怎么指回原片」
    那几列（迁移 0010）转，而 ``AssetRepository`` 管的是资产本身的通用读写。
    合在一起会让「谁在写血缘列」在类型上消失——与 ``FaceRepository`` / ``EntityRepository``
    拆开是同一条理由。
    """

    def save_probe(self, asset_id: str, probe: VideoProbe) -> None:
        """把探测结果并进 ``assets.metadata_json``。**必须在资产行落库之后调**（外键）。"""
        ...

    def save_processing_seconds(self, asset_id: str, seconds: float) -> None:
        """把整个视频阶段的秒数并进 ``metadata_json.video_processing_seconds``。

        **与 ``save_probe`` 分成两次写**：计时范围要包含场景抽取（1.0 的
        ``video_processing_seconds`` 覆盖整个视频处理阶段），而场景抽取排在
        ``save_probe`` 之后——一次写不完。同样是合并写，不覆盖 ``metadata_json`` 上的
        其他键（1.0 同一列上还压着反地理、事实 id 等）。
        """
        ...

    def probe(self, asset_id: str) -> VideoProbe | None:
        """没探测过（``metadata_json`` 里没有任何视频键）返回 ``None``。

        ``processing_seconds`` 一并读回（缺了就是 ``None``），供 ``GET /videos/{id}`` 暴露。
        """
        ...

    def list_derived(self, parent_asset_id: str, *, scene_index: int | None = None) -> Sequence[DerivedAsset]:
        """这个源片的派生物，按 ``(source_scene_index, source_timestamp_sec, source_frame_index)``。

        1.0 ``db.py:2383`` 的 ``list_derived_assets``。**``derived_kind IS NOT NULL``
        就够**——1.0 的 ``WHERE derived_kind IN ('video_keyframe','video_keyframe_webp')``
        是它自己双算法并存留下的补丁（``s8-deviations.md`` §3），2.0 只有一条路径。
        """
        ...

    def add_derived(
        self,
        *,
        parent_asset_id: str,
        asset: Asset,
        derived_kind: str,
        source_timestamp_sec: float,
        source_frame_index: int,
        source_scene_index: int,
    ) -> None:
        """把一张派生帧落库，并把迁移 0010 的血缘列写上。

        **为什么写入仍然单开一个方法。** ``Asset`` 现在**带上了三个血缘字段**
        （2026-10-05：PhotoBench 的关键帧分析经 ``/api/assets`` 读它们认血缘、算时间跨度，
        见 ``domain/media/models.py`` 的 ``Asset``），但那是**读侧**的形状；写入仍归这里，
        因为 ``add_derived`` 要一次把资产行与 ``source_frame_index`` 等血缘列写齐，
        不让调用方有机会只写一半。``AssetRepository.add`` 写的是非派生物，不带血缘。

        ``parent_asset_id`` / ``derived_kind`` 由调用方显式给，而不是复用 ``asset`` 上的
        默认 ``None``（通用构造的 ``Asset`` 正常不带血缘）。
        ``source_frame_index`` 上有唯一索引——重复插入直接 ``IntegrityError``，
        那是有意的（见 ``cleanup_derivatives`` 与迁移 0010 表头）。
        """
        ...

    def cleanup_derivatives(self, parent_asset_id: str) -> int:
        """删掉这个源片的派生物与场景事件，返回删掉的派生物行数。

        1.0 ``db.py:2435`` 的 ``cleanup_video_derivatives``。**先删随行数据再删主行**：
        ``event_observations`` 对 ``events``、``observations`` 对 ``assets`` 都有外键，
        且连接开的是 ``PRAGMA foreign_keys = ON``，顺序写反会直接 ``IntegrityError``。
        """
        ...


class AudioMetadataReader(Protocol):
    """从音频文件里读出时长/编码/采样率/声道/码率与拍摄时间、GPS（走 ffprobe）。

    **为什么单列一个端口，而不是复用 ``VideoMetadataReader``。** 形状不同：音频要的是
    时长/编码/采样率/声道/码率，视频要的是时长/帧率/宽高/旋转角；硬塞进同一个类型会让
    两边各背一半恒空字段，而类型上看不出哪几个字段对这类媒体是有意义的。这与 S8a 单列
    ``VideoMetadataReader``（不复用 EXIF 的 ``MetadataReader`` 三元组）是同一条理由。
    ffprobe 的子进程与容器标签解析与视频共用 ``infrastructure/media/ffprobe.py``。

    实现在 ``infrastructure/media/audio_metadata.py``：ffprobe 是子进程，
    ``domain-purity`` 与 ``application-purity`` 两条契约都禁止 domain/application 碰它。
    """

    def read(self, path: Path) -> AudioMetadata:
        """读不出来时抛 ``ValueError``（ffprobe 非零退出 / 没有音频流）。

        不返回 ``None``：那会让「这个文件探测不了」与「调用方忘了接线」变成同一个结果。
        调用方（``ImportAssets``）接住它并把原因写进 ``metadata_json.audio_probe_error``——
        与视频的 ``video_probe_error`` 同一条口径，探测失败不阻断导入。
        """
        ...


@dataclass(frozen=True, slots=True)
class AudioProbe:
    """一次音频探测的落库形态（写进 ``assets.metadata_json`` 的 ``audio_metadata``）。

    与 ``VideoProbe`` 的差别只有一条：音频**没有转码件**，所以没有
    ``browser_preview_path`` / ``browser_preview_error`` 两位——浏览器原生就能放
    mp3/wav/m4a/aac/flac/ogg（用户裁决②），照抄视频那两位只会得到一对恒空字段。
    ``probe_error`` 与视频同名同义：失败原因留在资产上，导入照收。
    """

    metadata: AudioMetadata | None = None
    probe_error: str = ""


class AudioRepository(Protocol):
    """音频探测结果的落库。

    **为什么与 ``VideoRepository`` 分开。** 后者管的是「派生物怎么指回原片」那几列
    （迁移 0010）加上视频的预览转码件；音频两样都没有。合在一起会让「谁在写血缘列」
    与「谁在写音频元数据」在类型上消失——与 ``VideoRepository`` 从 ``AssetRepository``
    拆出来是同一条理由。

    **只读侧没有方法是有意的**：视频那边 ``probe()`` 的存在是因为 ``GET /videos/{id}``
    与 ``deliver_media`` 要读回预览件路径；音频没有读侧消费者（元数据随 ``/assets/{id}``
    的行一起在库里，检索侧读的是 ``asset.media_type``）。**没有调用者的读方法是一条
    死代码**，`check_method_reachability.py` 会当场报红——所以不预留。
    """

    def save_probe(self, asset_id: str, probe: AudioProbe) -> None:
        """把探测结果并进 ``assets.metadata_json``。**必须在资产行落库之后调**（外键）。"""
        ...
