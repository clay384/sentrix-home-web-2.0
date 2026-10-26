"""媒体导入与交付。只做 HTTP 映射，业务在 application 层。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response

from sentrix.api.deps import get_container
from sentrix.api.schemas.media import AssetListResponse, AssetSummary, ImportResponse
from sentrix.application.identity.enroll_face import EnrollFace, EnrollFaces
from sentrix.application.media.extract_video_scenes import ExtractVideoScenes, VideoSceneExtraction
from sentrix.application.media.import_assets import ImportAssets, ImportRequest
from sentrix.application.media.index_place import IndexAssetPlace
from sentrix.application.media.maintain_entities import MaintainObservationEntities
from sentrix.application.media.observe_asset import ObserveAsset
from sentrix.application.memories.assign_events import AssignEvents, EventVisualSimilarity
from sentrix.application.memories.build_graph import BuildMemoryGraph
from sentrix.application.memories.maintain_facts import MaintainFacts
from sentrix.application.retrieval.index_observation_text import IndexObservationText
from sentrix.application.retrieval.index_visual import IndexAssetVisual
from sentrix.bootstrap.container import Container
from sentrix.domain.media.models import Asset, MediaType, Observation
from sentrix.domain.memories.graph import GraphBuildStats

router = APIRouter(tags=["media"])
log = logging.getLogger("sentrix.api.media")

PREVIEW_MAX_DIMENSION = 1600
PREVIEW_QUALITY = 85


def to_summary(asset: Asset) -> AssetSummary:
    return AssetSummary(
        id=asset.id,
        file_name=asset.file_name,
        media_type=asset.media_type.value,
        mime_type=asset.mime_type,
        size_bytes=asset.size_bytes,
        status=asset.status.value,
        captured_at=asset.captured_at.isoformat() if asset.captured_at else None,
        gps=(
            {"latitude": asset.captured_location.latitude, "longitude": asset.captured_location.longitude}
            if asset.captured_location
            else None
        ),
        # 关键帧血缘原样带出：普通照片是 None，派生帧是迁移 0010 那三列。1.0 的
        # ``SELECT *`` 顺带回了它们（``db.py:1505``），2.0 的窄投影不能少。
        derived_kind=asset.derived_kind,
        parent_asset_id=asset.parent_asset_id,
        source_timestamp_sec=asset.source_timestamp_sec,
    )


@router.post("/media/import", status_code=201)
async def import_media(
    file: UploadFile = File(...),
    scope_id: str = Form("home-default"),
    container: Container = Depends(get_container),
) -> ImportResponse:
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="empty file")

    use_case = build_import_assets(container, observe=False)
    result = use_case.execute(
        ImportRequest(
            scope_id=scope_id,
            file_name=file.filename or "unnamed",
            content_type=file.content_type,
            content=content,
        )
    )
    refresh_memory_graph(container, scope_id)
    return ImportResponse(
        asset_id=result.asset_id,
        observation_id=result.observation_id,
        media_type=result.media_type.value,
        status=result.status.value,
        deduplicated=result.deduplicated,
        indexed=result.indexed,
        placed=result.placed,
        event_id=result.event_id,
        video_probed=result.video_probed,
        video_scenes=result.video_scenes,
        audio_probed=result.audio_probed,
    )


def build_memory_graph(container: Container) -> BuildMemoryGraph:
    """把 2.0 标准记忆端口接到独立图投影用例。"""
    return BuildMemoryGraph(
        assets=container.asset_repository,
        observations=container.observation_repository,
        events=container.event_repository,
        participants=container.event_participant_repository,
        entities=container.entity_repository,
        facts=container.fact_repository,
        relationships=container.relationship_repository,
        graph=container.memory_graph,
        causal_judge=container.causal_judge,
        vectors=container.vector_repository,
    )


def refresh_memory_graph(container: Container, scope_id: str) -> GraphBuildStats | None:
    """在写入链路结束后重建派生图；图失败不阻断权威记忆写入。"""
    try:
        return build_memory_graph(container).execute(scope_id)
    except Exception as error:  # noqa: BLE001 - 派生投影必须可降级
        log.warning("记忆图重建失败（scope=%s），保留主链路结果: %s", scope_id, error)
        return None


def build_import_assets(container: Container, *, observe: bool) -> ImportAssets:
    """把导入用例的协作者接起来。**只在这里接一次**。

    单张上传与整册导入共用同一条链：分开接的话，「整册少了词法投影」这类差异会以
    「单张能搜到、批量搜不到」的形式出现，而那种 bug 在两端各看一半时都看不出来。

    ``observe`` 控制要不要为每张图付视觉观察的模型调用（首轮 + 补全 + 中文化，
    最多 3 次）。单张上传走快路径（1.0 的 ``_commit_fast_image`` 同样时机），
    整册导入才接观察器。

    **视频抽帧链（S8b）也挂在这一处**，理由同上：单张上传与整册导入都必须建场景，
    分开接会让「整册导入的视频没有场景」以「单张有、批量没有」的形式出现。
    """
    # 关键帧走**另一条 import 实例**，而且固定 ``observe=True``：视频的语义整个都在
    # 关键帧上，跳过观察等于让视频一条语义都不进库——正是 S8a 要修的那种「收下了但
    # 什么都没发生」。裁决④要的是关键帧白得 observation，不是白得一张空观察。
    # 用独立实例而不是复用入参那个，是因为入参的 ``observe`` 由调用方按「一张照片
    # 值不值得三次模型调用」定的，与视频无关。
    keyframe_importer = _build_importer(container, observe=True)
    extractor = _build_video_scenes(container, keyframe_importer.process_image)
    return _build_importer(
        container,
        observe=observe,
        video_scene_extractor=extractor.execute,
    )


def build_video_scenes(container: Container) -> ExtractVideoScenes:
    """独立的抽取链，给 ``POST /videos/{id}/reprocess`` 重跑用（S8b T514）。

    与导入时走的是同一个 ``_build_video_scenes``：重跑与首次导入是同一条链，
    差别只在重跑前先 ``cleanup_derivatives`` 清场。分开写一遍会让「重跑出来的场景
    与首次不一样」，而那种差异只有在对比两次结果时才看得出来。
    """
    return _build_video_scenes(container, _build_importer(container, observe=True).process_image)


def _build_video_scenes(container: Container, process_image: Callable[..., object]) -> ExtractVideoScenes:
    return ExtractVideoScenes(
        assets=container.asset_repository,
        events=container.event_repository,
        places=container.asset_place_repository,
        video_repository=container.video_repository,
        keyframes=container.video_keyframe_extractor,
        process_image=lambda asset, event_id: process_image(asset, forced_event_id=event_id),
        scene_planner=container.video_scene_planner,
    )


def _build_importer(
    container: Container,
    *,
    observe: bool,
    video_scene_extractor: Callable[[str], VideoSceneExtraction | int] | None = None,
) -> ImportAssets:
    """``ImportAssets`` 的装配本体。两个调用方共用：导入本身，以及关键帧那条流水线。"""
    assigner = AssignEvents(
        events=container.event_repository,
        observations=container.observation_repository,
        assets=container.asset_repository,
        places=container.asset_place_repository,
        visual_similarity=EventVisualSimilarity(
            vectors=container.vector_repository,
            observations=container.observation_repository,
            assets=container.asset_repository,
        ),
        # 事件打分的 person 分量（权重 0.20）的两个来源。装配点**只有这里**：
        # 单张上传与整册导入共用同一条链，分开接会让「整册少了人物桥」以
        # 「批量导入时事件分得更碎」的形式出现，而那种差异在两端口各看一半时看不出来。
        confirmed_people=container.entity_repository.confirmed_people_for_observation,
        confirmed_event_people=container.entity_repository.confirmed_people_for_event,
    )
    return ImportAssets(
        assets=container.asset_repository,
        observations=container.observation_repository,
        storage=container.media_storage,
        metadata=container.metadata_reader,
        visual_indexer=IndexAssetVisual(
            embedder=container.visual_embedder,
            vectors=container.vector_repository,
            index=container.vector_index,
        ),
        place_indexer=IndexAssetPlace(
            resolver=container.place_resolver, places=container.asset_place_repository
        ),
        observer=(
            ObserveAsset(
                observer=container.vlm_observer,
                observations=container.observation_repository,
            )
            if observe
            else None
        ),
        event_assigner=lambda observation: _event_id(assigner, observation),
        # 关键帧的强制归属（S8b T513）。**不受 ``observe`` 影响**：只要有人给了
        # ``forced_event_id`` 就需要它，而那一条路只有视频抽帧会走。
        event_attacher=lambda event_id, observation: container.event_repository.attach(
            event_id, observation.id
        ),
        face_enroller=_face_enroller(container),
        fact_maintainer=_fact_maintainer(container),
        text_indexer=_text_indexer(container),
        entity_extractor=_entity_extractor(container),
        lexical_index=container.lexical_index,
        # 记忆空间行（裁决一）。**只在这里接**：簇列表内连 ``memory_spaces``，
        # 少接这一处，全新部署导入一整册之后审阅页一个簇都不显示（第 97 / 103 条）。
        spaces=container.spaces,
        # 视频探测链（S8a T505）。**接在这里而不是路由里**：单张上传与整册导入共用
        # 这一处装配，分开接会让「整册导入的视频不读元数据」以「单张能读到、批量读不到」
        # 的形式出现——而那种差异在两端各看一半时都看不出来（与上面几处同一条理由）。
        video_metadata=container.video_metadata_reader,
        video_transcoder=container.video_transcoder,
        video_repository=container.video_repository,
        # 音频探测链（S8c T521）。**同样接在这一处**：单张上传与整册导入共用它，
        # 分开接会让「整册导入的音频不读元数据」以「单张能读到、批量读不到」的形式出现。
        audio_metadata=container.audio_metadata_reader,
        audio_repository=container.audio_repository,
        # 抽帧链（S8b T511–T513）。只有导入那条实例拿得到它；关键帧那条实例传 None，
        # 从根上杜绝「关键帧再去抽一层关键帧」的递归。
        video_scene_extractor=video_scene_extractor,
    )


def _face_enroller(container: Container) -> EnrollFaces:
    """人脸登记：检测 → 在线匹配。**两段式**（``EnrollFaces``）。

    **它必须排在事件归并之前**——1.0 在 ``pipeline.py:316-320`` 用注释钉过这件事：
    与已确认的人相像的脸会在这一步写下 ``entity_mentions``，而事件归并的
    ``person_score`` 读的正是它。顺序写在 ``ImportAssets.commit_image`` 里，
    这里只负责接。

    **能并行的是 ``prepare`` 那一次检测，不是匹配**：匹配读同 scope 的候选簇，
    而那些簇是同批前一张照片刚写进去的（``FaceEnroller`` 的 docstring）。

    权重没装的机器上 ``detect`` 返回空表，这条链路整个不生效——**不需要在这里判空**：
    让它自然地「没有脸」比在这里分叉出第二条路径更省事，也更难写错。
    """
    return EnrollFaces(
        detector=container.face_detector,
        enroll=EnrollFace(
            faces=container.face_repository,
            entities=container.entity_repository,
            vectors=container.vector_repository,
            assets=container.asset_repository,
        ),
    )


def _text_indexer(container: Container) -> Callable[[Observation, str | None], int]:
    """文本向量写入的接线。

    **排在事件归并之后**（``ImportAssets.execute`` 的顺序），所以入参带 ``event_id``——
    1.0 那条 ``episodic/event`` 行要的就是它。
    """
    index = IndexObservationText(embedder=container.text_embedder, vectors=container.vector_repository)

    def run(observation: Observation, event_id: str | None) -> int:
        return index.index(observation, event_id).rows

    return run


def _entity_extractor(container: Container) -> Callable[[Observation, str], tuple[str, ...]]:
    """非人物实体抽取的接线（S6 T127）。

    **排在事件判定之后**（``ImportAssets.execute`` 的顺序），所以入参带 ``event_id``——
    抽出来的地点/物件/时间要挂到那个事件上（``event_entities``）。与 1.0
    ``pipeline.py:336`` 的位置一致。
    """
    maintain = MaintainObservationEntities(
        entities=container.entity_repository,
        events=container.event_participant_repository,
        assets=container.asset_repository,
        relationships=container.relationship_repository,
    )

    def run(observation: Observation, event_id: str) -> tuple[str, ...]:
        return tuple(entity.id for entity in maintain.execute(observation, event_id=event_id).entities)

    return run


def _fact_maintainer(container: Container) -> Callable[[Observation], tuple[str, ...]]:
    """事实写入的接线。S5b 起两半都在（用户裁决④），S4 的「按设计为空」到此结束。

    1. **候选来源**：模型自报的 ``facts``，落库在 ``observations.facts_json``，读出来
       就是 ``Observation.facts``。1.0 的候选来自 ``analyze_image`` 的同一个数组
       （``model_clients.py:1130-1198``）。
    2. **门禁**：``confirmed_person_names`` 是 1.0 ``is_confirmed_person_name``
       （``db.py:1494-1503``）的集合形态——本 scope 内已确认人物的**本名**。

    **两个入参都不再有默认值**（Task 80 的同一口径）：门禁留一个恒空默认值的话，
    「忘了接线」与「真的没有已确认人物」在类型上完全一样，而事实恒空不报错、
    只是永远查不到。少写一个参数现在是 ``TypeError``。
    """
    maintain = MaintainFacts(
        facts=container.fact_repository,
        candidates=lambda observation: observation.facts,
        confirmed_people=container.entity_repository.confirmed_person_names,
    )
    return maintain.maintain


def _event_id(assigner: AssignEvents, observation: Observation) -> str:
    return assigner.execute(observation).event.id


@router.get("/assets")
def list_assets(
    # 默认与空串都表示**全部相册**：1.0 的 `list_assets(scope_id=None)` 是 `if scope_id:`
    # 才加过滤，而前端用 `""` 表达「全部」（`src/app.js` 的约定）。默认值从
    # `"home-default"` 改成 `""` 是**行为修正**——原来不传 scope 会查一个叫
    # home-default 的空间，而那个空间在 2.0 的空库里根本不存在。
    scope_id: str = "",
    limit: int = 200,
    container: Container = Depends(get_container),
) -> AssetListResponse:
    assets = container.asset_repository.list_by_scope(scope_id or None, min(limit, 1000))
    return AssetListResponse(items=[to_summary(asset) for asset in assets])


@router.get("/assets/{asset_id}")
def get_asset(asset_id: str, container: Container = Depends(get_container)) -> AssetSummary:
    asset = container.asset_repository.get(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="asset not found")
    return to_summary(asset)


def deliver_asset(asset_id: str, *, original: bool, container: Container) -> Response:
    """按 ``original`` 交付一张资产：``True`` 原文件，``False`` 服务端转码的 JPEG 预览。

    抽出来是因为两条路由共用同一份「怎么把一张已授权的图交出去」：``/assets/{id}/file``
    与助手侧的 ``/assistant/result-sets/{id}/photo``——1.0 的 ``asset_file``
    （``app.py:2450``）也是被这两处共用的。转码参数只留一份。
    """
    asset = container.asset_repository.get(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="asset not found")
    path = Path(asset.path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="media file missing on disk")
    if original:
        return FileResponse(path, media_type=asset.mime_type or "application/octet-stream")
    payload = container.preview_encoder.encode_jpeg(
        path, max_dimension=PREVIEW_MAX_DIMENSION, quality=PREVIEW_QUALITY
    )
    return Response(content=payload, media_type="image/jpeg")


def deliver_media(asset: Asset, container: Container) -> Response:
    """视频/音频的浏览器交付：**视频能转就交转码件，转不了与原片一律回落原文件**。

    1.0 ``app.py:2452-2458`` 的分流只有一句判据（``media_type == 'video' and not original
    and preview_path.is_file()``），其余一律回落原片。2.0 照搬，但把「转码件在哪」
    的读法收在 ``VideoRepository.probe`` 上——它与写入侧成对，是唯一知道
    ``metadata_json`` 里怎么放的地方。

    **音频走这里是因为它也没有服务端预览件**（S8c T521）：不转码（用户裁决②），
    交付的就是原文件，而 ``probe`` 对音频恒为 ``None``，所以自然落到最后的原片分支。
    把音频挂在这条路上，修掉的是与视频同一个 500——原先 ``/preview`` 无条件走
    ``PillowPreviewEncoder``，而 PIL 既打不开视频也打不开录音。

    **原片分支补了 ``path.exists()`` 检查**（``deliver_asset`` 一直有）：两条路
    交付同一个文件时，缺失文件应当是同一个 404。少了它，音频 ``/file``（走的正是这条
    路）会把「文件不在磁盘上」变成一个 500。
    """
    probe = container.video_repository.probe(asset.id)
    preview_path = Path(probe.browser_preview_path) if probe and probe.browser_preview_path else None
    if preview_path is not None and preview_path.is_file():
        return FileResponse(
            preview_path,
            media_type="video/mp4",
            filename=f"{Path(asset.file_name).stem}-preview.mp4",
            headers={"Cache-Control": "private, max-age=86400", "X-Sentrix-Source": "browser-preview"},
        )
    # 没有转码件（H.264 不用转 / 转码失败 / 还没探测过 / 音频）一律交原片：
    # 浏览器放不放得了是它的事，服务端不做第二次判断。
    path = Path(asset.path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="media file missing on disk")
    return FileResponse(
        path,
        media_type=asset.mime_type or "application/octet-stream",
        filename=asset.file_name,
    )


def _self_served_or_none(asset_id: str, container: Container) -> Asset | None:
    """交付物**就是文件本身**的资产：视频（有转码件时给转码件）与音频（永远给原片）。

    图片不在内——它有服务端渲染的 JPEG 预览，走 ``PillowPreviewEncoder``。
    """
    asset = container.asset_repository.get(asset_id)
    if asset is None or asset.media_type not in (MediaType.VIDEO, MediaType.AUDIO):
        return None
    return asset


@router.get("/assets/{asset_id}/file")
def get_asset_file(
    asset_id: str,
    original: bool = False,
    container: Container = Depends(get_container),
) -> Response:
    """``?original=1`` 拿原片；不传时**视频**给浏览器预览件（1.0 ``app.py:2448-2478``）。

    音频与图片这条路上 ``original`` 是空操作：音频本来就只有原片可交
    （``deliver_media`` 对音频恒回落原文件），图片的 ``/file`` 一直是原文件
    （契约测试钉着「交出去的字节与传上来的一模一样」），预览在 ``/preview``。
    不为了对称去改图片——改了会让 HEIC 从「原图直出」变成「转码件直出」，那是另一个决定。
    """
    asset = _self_served_or_none(asset_id, container)
    if asset is not None and not original:
        return deliver_media(asset, container)
    return deliver_asset(asset_id, original=True, container=container)


@router.get("/assets/{asset_id}/preview")
def get_asset_preview(asset_id: str, container: Container = Depends(get_container)) -> Response:
    asset = _self_served_or_none(asset_id, container)
    if asset is not None:
        return deliver_media(asset, container)
    return deliver_asset(asset_id, original=False, container=container)
