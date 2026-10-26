"""身份端点：簇审阅、人物、实体属性、人脸裁剪。

**只做 HTTP 映射与服务端字段拼装。** 审阅的四条规则（同名并入、已确认不可拒绝、两个已
确认的人不能自动合并、实例必须属于该簇）全在 ``application/identity`` 里；路由把它们
抛出的带码错误**原样放行**，由 ``api/errors.py`` 那一个处理器映射状态（口径写在
``domain/errors.py``）。路由自己在这里发现的边界问题（找不到实例、bbox 非法）也抛同一族
的类型，理由同样是「响应形状只有一种」。

**读侧为什么在这里拼而不是另开一个用例。** 1.0 的 ``list_face_clusters`` 与
``get_entity_detail`` 本身就是「仓储查询 + 几个现算的派生字段」；2.0 的读侧同构，
拼装的每一步都只是把记录字段搬进响应模型。真正带规则的两块——``canonical_name`` 对
``pending`` 的改写与 ``reviewable`` / ``single_sample``——1.0 也写在路由里
（``app.py:1518`` / ``db.py:5146``），2.0 保持同一个位置，不额外发明一层。
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Protocol, TypeVar

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import Response

from sentrix.api.deps import get_container
from sentrix.api.errors import InvalidRequestError, NotFoundError, UnprocessableError
from sentrix.api.routes.media import refresh_memory_graph
from sentrix.api.schemas.identity import (
    BatchConfirmFailure,
    BatchConfirmOutcome,
    BatchConfirmRequest,
    BatchConfirmResponse,
    ConfirmClusterRequest,
    ConfirmClusterResponse,
    EntityDetailAppearance,
    EntityDetailAsset,
    EntityDetailEntity,
    EntityDetailEvent,
    EntityDetailFact,
    EntityDetailObservation,
    EntityDetailRelationship,
    EntityDetailResponse,
    EntityEvidenceFile,
    EntityGroupDetailResponse,
    EntityGroupListResponse,
    EntityGroupPayload,
    EntityGroupRationale,
    EntityListResponse,
    EntityPropertyPayload,
    EntityPropertyRequest,
    EntitySummaryPayload,
    FaceClusterListResponse,
    FaceClusterSummary,
    FaceCropRef,
    MergeClustersRequest,
    PersonDetailResponse,
    PersonListResponse,
    PersonPayload,
    RejectClusterResponse,
    RenamePersonRequest,
    RenamePersonResponse,
    SeedIdentityResponse,
    SplitClusterRequest,
)
from sentrix.application.identity.name_person import NamePerson
from sentrix.application.identity.observe_appearance import ObserveAppearance
from sentrix.application.identity.read_entity_detail import EntityDetail, ReadEntityDetail
from sentrix.application.identity.read_entity_groups import (
    EntityGroup,
    EntityGroupDetail,
    ReadEntityGroups,
)
from sentrix.application.identity.review_cluster import ReviewCluster
from sentrix.application.identity.seed_identity import (
    NoSeedFacesError,
    SeedIdentity,
    SeedInstanceNotFoundError,
    SeedPhoto,
)
from sentrix.application.memories.assign_events import ConfirmedPersonEvents, EventVisualSimilarity
from sentrix.bootstrap.container import Container
from sentrix.domain.identity.decoration import is_reviewable, is_single_sample, private_place
from sentrix.domain.identity.models import (
    CLUSTER_SAMPLES_LIMIT,
    CLUSTER_STATUS_PENDING,
    CLUSTER_STATUS_REJECTED,
    ENTITY_STATUS_CONFIRMED,
    ENTITY_STATUS_PENDING,
    ENTITY_TYPE_PERSON,
    ENTITY_TYPE_PLACE,
    PENDING_CLUSTER_NAME,
    PENDING_SUMMARY_DISPLAY,
    EntityDecoration,
    EntityPropertyRecord,
    EntityRecord,
    FaceClusterRecord,
    FaceInstanceRecord,
)
from sentrix.domain.media.models import Asset, AssetStatus, MediaType, Observation

router = APIRouter(tags=["identity"])


_T = TypeVar("_T", bound="_HasId")


class _HasId(Protocol):
    """去重要用的那一个字段。"""

    @property
    def id(self) -> str: ...


# 1.0 ``app.py:2530-2532`` 的三个字面量。裁剪尺寸与质量是**交付契约**：换一个数字，
# 前端缓存里的缩略图与新的就再也对不上。
FACE_CROP_MAX_DIMENSION = 256
FACE_CROP_QUALITY = 88
FACE_CROP_CACHE_CONTROL = "private, max-age=86400"

# 1.0 ``app.py:2337`` 预置照片的来源标记。
SEED_SOURCE_TYPE = "identity_seed"


# ---- 用例装配 --------------------------------------------------------------------


def _confirmed_person_events(container: Container) -> ConfirmedPersonEvents:
    """确认之后的事件侧。**只在确认这一类用例里接**——重分段不是导入链的一部分。

    两个 ``confirmed_people`` 来源与 ``api/routes/media.py`` 的 ``AssignEvents`` 是同一对
    仓储方法：事件打分与重分段算的是同一个 ``person_score``，两个装配点取不同的来源，
    「按人找事件」的分歧就再也不会被发现。
    """
    return ConfirmedPersonEvents(
        events=container.event_repository,
        participants=container.event_participant_repository,
        observations=container.observation_repository,
        assets=container.asset_repository,
        entities=container.entity_repository,
        relationships=container.relationship_repository,
        visual_similarity=EventVisualSimilarity(
            vectors=container.vector_repository,
            observations=container.observation_repository,
            assets=container.asset_repository,
        ),
        confirmed_people=container.entity_repository.confirmed_people_for_observation,
        confirmed_event_people=container.entity_repository.confirmed_people_for_event,
    )


def _observe_appearance(container: Container) -> ObserveAppearance:
    return ObserveAppearance(
        faces=container.face_repository,
        entities=container.entity_repository,
        observations=container.observation_repository,
        appearances=container.appearance_repository,
        crops=container.person_crop_renderer,
        vision=container.person_vision,
    )


def _review_cluster(container: Container) -> ReviewCluster:
    return ReviewCluster(
        faces=container.face_repository,
        entities=container.entity_repository,
        facts=container.fact_repository,
        events=_confirmed_person_events(container),
        appearance=_observe_appearance(container),
    )


def _name_person(container: Container) -> NamePerson:
    return NamePerson(
        faces=container.face_repository,
        entities=container.entity_repository,
        review=_review_cluster(container),
    )


def _seed_identity(container: Container) -> SeedIdentity:
    return SeedIdentity(
        detector=container.face_detector,
        faces=container.face_repository,
        entities=container.entity_repository,
        vectors=container.vector_repository,
    )


# ---- 记录 → 传输模型 --------------------------------------------------------------


def _optional_isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _person_payload(container: Container, entity: EntityRecord) -> PersonPayload:
    """一个人物实体。

    ``pending`` 的实体的内部名是「待确认人物簇 · cluster-x」（``pending_cluster_name``），
    那是**落库的数据**，不该出现在接口上——1.0 同样在这一层把它换掉
    （``app.py:1518`` 的 ``display_name``）。与 ``FaceRepository`` 的簇记录用同一个常量，
    两处漂开一次，同一个实体会在簇列表里叫一个名字、在人物列表里叫另一个。

    **``summary`` 也换**，这一步与换名字是 1.0 的同一段（``app.py:1518-1521``）：
    库里存的是「由 buffalo_l embedding 生成，等待用户确认」，接口上给的是
    ``PENDING_SUMMARY_DISPLAY``——API 上不该出现模型代号。少了这一步，同一个人物候选
    在 ``/people`` 与 ``/entities`` 上会有两句不同的说明。
    """
    pending = entity.status == ENTITY_STATUS_PENDING
    return PersonPayload(
        id=entity.id,
        scope_id=entity.scope_id,
        canonical_name=PENDING_CLUSTER_NAME if pending else entity.canonical_name,
        status=entity.status,
        family_role=None if pending else entity.family_role,
        summary=PENDING_SUMMARY_DISPLAY if pending else entity.summary,
        confidence=entity.confidence,
        confirmed=entity.status == ENTITY_STATUS_CONFIRMED,
        aliases=list(container.entity_repository.person_aliases(entity.id)),
    )


def _crop_ref(instance: FaceInstanceRecord) -> FaceCropRef:
    return FaceCropRef(
        face_instance_id=instance.id,
        asset_id=instance.asset_id,
        observation_id=instance.observation_id,
        file_name=instance.file_name,
        media_type=instance.media_type,
        bbox=list(instance.bbox),
        detection_confidence=instance.detection_confidence,
        quality=instance.quality,
        validity=instance.validity,
        crop_url=f"/api/v1/face-instances/{instance.id}/crop",
    )


def _cluster_payload(
    container: Container, cluster: FaceClusterRecord, *, include_all_samples: bool = False
) -> FaceClusterSummary:
    instances = container.face_repository.list_instances(cluster.id)
    return FaceClusterSummary(
        id=cluster.id,
        scope_id=cluster.scope_id,
        status=cluster.status,
        entity_id=cluster.entity_id,
        canonical_name=cluster.canonical_name,
        family_role=cluster.family_role,
        entity_status=cluster.entity_status,
        member_count=cluster.member_count,
        confidence=cluster.confidence,
        revision=cluster.revision,
        created_at=_optional_isoformat(cluster.created_at),
        updated_at=_optional_isoformat(cluster.updated_at),
        reviewable=cluster.status != CLUSTER_STATUS_REJECTED and cluster.member_count > 0,
        single_sample=cluster.status == CLUSTER_STATUS_PENDING and cluster.member_count <= 1,
        samples=[
            _crop_ref(item)
            for item in (instances if include_all_samples else instances[:CLUSTER_SAMPLES_LIMIT])
        ],
    )


def _cluster_or_404(container: Container, cluster_id: str) -> FaceClusterRecord:
    cluster = container.face_repository.get_cluster(cluster_id)
    if cluster is None:
        raise NotFoundError(f"face cluster not found: {cluster_id}")
    return cluster


def _person_or_404(container: Container, person_id: str) -> EntityRecord:
    person = container.entity_repository.get(person_id)
    if person is None or person.entity_type != ENTITY_TYPE_PERSON:
        raise NotFoundError("person not found")
    return person


# ---- 簇列表与四审阅 ---------------------------------------------------------------


@router.get("/face-clusters")
def list_face_clusters(
    status: str | None = None,
    scope_id: str | None = None,
    include_all_samples: bool = False,
    container: Container = Depends(get_container),
) -> FaceClusterListResponse:
    clusters = container.face_repository.list_clusters(status=status)
    if scope_id:
        # 1.0 在路由里做的同一条过滤（``app.py:1972``）：仓储的 ``list_clusters`` 是
        # 「进人物视图的全部簇」，按空间再切一刀是调用方的事。
        clusters = [item for item in clusters if item.scope_id == scope_id]
    return FaceClusterListResponse(
        clusters=[
            _cluster_payload(container, item, include_all_samples=include_all_samples) for item in clusters
        ]
    )


@router.post("/face-clusters/{cluster_id}/confirm")
def confirm_face_cluster(
    cluster_id: str,
    payload: ConfirmClusterRequest,
    container: Container = Depends(get_container),
) -> ConfirmClusterResponse:
    cluster = container.face_repository.get_cluster(cluster_id)
    result = _review_cluster(container).confirm(
        cluster_id, name=payload.name, family_role=payload.family_role
    )
    refresh_memory_graph(container, cluster.scope_id if cluster is not None else result.entity.scope_id)
    return ConfirmClusterResponse(
        entity=_person_payload(container, result.entity), merged_into=result.merged_into
    )


@router.post("/face-clusters/{cluster_id}/reject")
def reject_face_cluster(
    cluster_id: str, container: Container = Depends(get_container)
) -> RejectClusterResponse:
    # 用例找不到簇时抛 ClusterNotFoundError（404），由统一处理器映射；这里不做二次判断。
    cluster = container.face_repository.get_cluster(cluster_id)
    rejected = _review_cluster(container).reject(cluster_id)
    if cluster is not None:
        refresh_memory_graph(container, cluster.scope_id)
    return RejectClusterResponse(cluster_id=cluster_id, rejected_entity_id=rejected)


@router.post("/face-clusters/merge")
def merge_face_clusters(
    payload: MergeClustersRequest, container: Container = Depends(get_container)
) -> FaceClusterSummary:
    # 两个 id 缺一个就是请求本身不成立（400）。1.0 在路由上判的是同一件事
    # （``app.py:2075``）；不判的话空 id 会一路走到仓储、变成一条 404，把调用方错误
    # 报成「这个簇不存在」。
    if not payload.target_cluster_id or not payload.source_cluster_id:
        raise InvalidRequestError("target_cluster_id and source_cluster_id are required")
    target_before = container.face_repository.get_cluster(payload.target_cluster_id)
    target = _review_cluster(container).merge(
        target_cluster_id=payload.target_cluster_id, source_cluster_id=payload.source_cluster_id
    )
    refresh_memory_graph(
        container,
        target_before.scope_id if target_before is not None else target.scope_id,
    )
    return _cluster_payload(container, target)


@router.post("/face-clusters/{cluster_id}/split")
def split_face_cluster(
    cluster_id: str,
    payload: SplitClusterRequest,
    container: Container = Depends(get_container),
) -> FaceClusterSummary:
    if not payload.face_instance_id:
        raise InvalidRequestError("face_instance_id is required")
    cluster_before = container.face_repository.get_cluster(cluster_id)
    created = _review_cluster(container).split(cluster_id, face_instance_id=payload.face_instance_id)
    refresh_memory_graph(
        container,
        cluster_before.scope_id if cluster_before is not None else created.scope_id,
    )
    return _cluster_payload(container, created)


# ---- 实体 --------------------------------------------------------------------


def _entity_summary(
    container: Container,
    entity: EntityRecord,
    decoration: EntityDecoration,
    *,
    projected: tuple[str, bool | None] | None = None,
) -> EntitySummaryPayload:
    """一条实体记录 + 它的派生字段 → 实体目录里的一行。

    三段改写，各自指着 1.0 的一处：

    1. **pending 的人物**：换名字、去掉角色、换说明（``app.py:1863-1867``）。
       三件事是一组，与 ``_person_payload`` 里那一段同源。
    2. **私密地点**（``db.py:4674-4685``，经 ``list_entities`` 的 ``public_entity``）：
       **只对 place 生效**，要读这个实体的属性，所以这里多一次仓储调用——
       1.0 也是逐条调的（``db.py:4055`` 的列表推导里每条一次 ``list_entity_properties``），
       不是批量。
    3. **两个派生布尔**：判断在 ``domain/identity/decoration.py``，这里只把数喂进去。
    """
    canonical_name = entity.canonical_name
    family_role = entity.family_role
    summary = entity.summary
    single_sample: bool | None = None
    private: bool | None = None
    if entity.entity_type == ENTITY_TYPE_PERSON and entity.status == ENTITY_STATUS_PENDING:
        canonical_name = PENDING_CLUSTER_NAME
        family_role = None
        summary = PENDING_SUMMARY_DISPLAY
    if entity.entity_type == ENTITY_TYPE_PERSON:
        single_sample = is_single_sample(status=entity.status, clusters=decoration.clusters)
    if entity.entity_type == ENTITY_TYPE_PLACE:
        if projected is not None:
            # 调用方已经算过这一次投影（实体分组的用例要拿投影后的名字给成员排序），
            # 再查一遍属性表只是把同一次计算做第二遍。
            canonical_name, private = projected
        else:
            properties = {
                item.property_key: item.value
                for item in container.entity_repository.list_properties(entity.id)
            }
            canonical_name, private = private_place(canonical_name, properties)
    return EntitySummaryPayload(
        id=entity.id,
        scope_id=entity.scope_id,
        entity_type=entity.entity_type,
        canonical_name=canonical_name,
        status=entity.status,
        family_role=family_role,
        summary=summary,
        confidence=entity.confidence,
        identity_state=entity.identity_state,
        role_state=entity.role_state,
        name_state=entity.name_state,
        created_at=_optional_isoformat(entity.created_at),
        updated_at=_optional_isoformat(entity.updated_at),
        cluster_count=decoration.cluster_count,
        photo_count=decoration.photo_count,
        mention_count=decoration.mention_count,
        evidence_count=decoration.evidence_count,
        relationship_count=decoration.relationship_count,
        reviewable=is_reviewable(
            entity_type=entity.entity_type,
            status=entity.status,
            clusters=decoration.clusters,
            evidence_count=decoration.evidence_count,
        ),
        single_sample=single_sample,
        avatar_face_instance_id=decoration.avatar_face_instance_id,
        preview_asset_id=decoration.preview_asset_id,
        preview_file_name=decoration.preview_file_name,
        preview_media_type=decoration.preview_media_type,
        private=private,
    )


@router.get("/entities")
def list_entities(
    status: str | None = None,
    include_people: bool = Query(False, alias="includePeople"),
    scope_id: str | None = None,
    container: Container = Depends(get_container),
) -> EntityListResponse:
    """实体目录。1.0 ``app.py:1858-1868``。

    **``includePeople`` 默认 ``False``，也就是默认不给人物。** 这个默认值是 API 契约
    （1.0 的三个查询参数之一），虽然 1.0 自己的前端永远传 ``true``
    （``src/api.js:42`` 写死在请求里）——**照搬默认值，但不为它编造消费场景**。

    参数名走 ``alias`` 而不是把 Python 参数写成驼峰：线上名字是 ``includePeople``
    （契约），代码里保持 snake_case（本仓库的命名规则）。
    """
    records = container.entity_repository.list_entities(status=status, scope_id=scope_id)
    if not include_people:
        # 类型过滤在路由层，与 ``GET /people`` 那一处是同一条理由——仓储给「全部实体」。
        records = [item for item in records if item.entity_type != ENTITY_TYPE_PERSON]
    decorations = container.entity_repository.decorate_entities([item.id for item in records])
    return EntityListResponse(
        entities=[_entity_summary(container, item, decorations[item.id]) for item in records]
    )


def _read_entity_detail(container: Container) -> ReadEntityDetail:
    """实体详情用例的装配。**八个协作者，只有这里知道它们是谁。**"""
    return ReadEntityDetail(
        entities=container.entity_repository,
        faces=container.face_repository,
        facts=container.fact_repository,
        events=container.event_repository,
        observations=container.observation_repository,
        assets=container.asset_repository,
        appearances=container.appearance_repository,
        relationships=container.relationship_repository,
    )


def _detail_asset(asset: Asset | None) -> EntityDetailAsset | None:
    if asset is None:
        return None
    return EntityDetailAsset(
        id=asset.id,
        file_name=asset.file_name,
        media_type=str(asset.media_type),
        path=asset.path,
    )


def _entity_detail_payload(detail: EntityDetail, container: Container) -> EntityDetailResponse:
    """把用例的结果搬进响应模型。**只有一处改写**，指 1.0 ``app.py:1954-1955``。"""
    entity = detail.entity
    canonical_name = entity.canonical_name
    if entity.entity_type == ENTITY_TYPE_PERSON and entity.status == ENTITY_STATUS_PENDING:
        # **只换名字。** 1.0 这个端点没有同时改 ``family_role`` 与 ``summary``，
        # 而同仓库的 ``/api/people/{id}/profile``（``app.py:1545-1549``）与
        # ``/evidence``（``:1561-1565``）两处都改了。**三处不一致是 1.0 的原样**，
        # 照搬——顺手「统一」会让这个端点的 ``summary`` 从落库原文变成展示文案。
        canonical_name = PENDING_CLUSTER_NAME
    return EntityDetailResponse(
        entity=EntityDetailEntity(
            id=entity.id,
            scope_id=entity.scope_id,
            entity_type=entity.entity_type,
            canonical_name=canonical_name,
            status=entity.status,
            family_role=entity.family_role,
            summary=entity.summary,
            confidence=entity.confidence,
            identity_state=entity.identity_state,
            role_state=entity.role_state,
            name_state=entity.name_state,
            created_at=_optional_isoformat(entity.created_at),
            updated_at=_optional_isoformat(entity.updated_at),
            avatar_face_instance_id=detail.avatar_face_instance_id,
            evidence_count=detail.evidence_count,
        ),
        properties=[_property_payload(item) for item in detail.properties],
        property_history=[_property_payload(item) for item in detail.property_history],
        # 簇那一块直接复用 ``FaceClusterSummary``：它的形状就是 1.0 的「簇行 + samples」，
        # 再定义一个同样的模型只会让两处漂开。
        clusters=[
            _cluster_payload(container, item.cluster).model_copy(
                update={"samples": [_crop_ref(face) for face in item.samples]}
            )
            for item in detail.clusters
        ],
        facts=[
            EntityDetailFact(
                id=fact.id,
                scope_id=fact.scope_id,
                subject=fact.subject,
                predicate=fact.predicate,
                object=fact.object,
                status=str(fact.status),
                confidence=fact.confidence,
                evidence_ids=list(fact.evidence_ids),
                revision=fact.revision,
            )
            for fact in detail.facts
        ],
        events=[
            EntityDetailEvent(
                id=event.id,
                scope_id=event.scope_id,
                title=event.title,
                event_type=event.event_type,
                time_start=_optional_isoformat(event.time_start),
                time_end=_optional_isoformat(event.time_end),
                place=event.place,
                admin_district=event.admin_district,
                activity=event.activity,
                summary=event.summary,
                confidence=event.confidence,
                aggregation_score=event.aggregation_score,
                status=event.status,
                revision=event.revision,
            )
            for event in detail.events
        ],
        observations=[
            EntityDetailObservation(
                id=item.observation.id,
                asset_id=item.observation.asset_id,
                source_type=item.observation.source_type,
                captured_at=_optional_isoformat(item.observation.captured_at),
                caption=item.observation.caption,
                asset=_detail_asset(item.asset),
            )
            for item in detail.observations
        ],
        appearance_evidence=[
            EntityDetailAppearance(
                id=item.id,
                person_id=item.person_id,
                face_instance_id=item.face_instance_id,
                observation_id=item.observation_id,
                asset_id=item.asset_id,
                crop_bbox=list(item.crop_bbox),
                clothing=list(item.clothing),
                confidence=item.confidence,
                model_name=item.model_name,
                status=item.status,
                file_name=item.file_name,
                media_type=item.media_type,
            )
            for item in detail.appearance_evidence
        ],
        evidence_files=[
            EntityEvidenceFile(
                evidence_id=item.evidence_id,
                asset_id=item.asset_id,
                file_name=item.file_name,
                kind=item.kind,
            )
            for item in detail.evidence_files
        ],
        relationships=[
            EntityDetailRelationship(
                id=item.id,
                scope_id=item.scope_id,
                subject_entity_id=item.subject_entity_id,
                predicate=item.predicate,
                object_entity_id=item.object_entity_id,
                inverse_predicate=item.inverse_predicate,
                status=item.status,
                confidence=item.confidence,
                evidence_ids=list(item.evidence_ids),
                revision=item.revision,
                created_at=_optional_isoformat(item.created_at),
                updated_at=_optional_isoformat(item.updated_at),
                subject_name=item.subject_name,
                object_name=item.object_name,
            )
            for item in detail.relationships
        ],
    )


@router.get("/entities/{entity_id}")
def get_entity_detail(
    entity_id: str,
    container: Container = Depends(get_container),
) -> EntityDetailResponse:
    """实体详情。1.0 ``app.py:1948-1956`` 加 ``db.py:4570-4627``。

    两条边界：用例对不存在的实体返回 ``None`` → 404 ``entity not found``
    （1.0 是 ``if not value:`` 判的，同一个条件）；pending 的人物**只换名字**，
    理由写在 ``_entity_detail_payload`` 里。
    """
    detail = _read_entity_detail(container).execute(entity_id)
    if detail is None:
        raise NotFoundError("entity not found")
    return _entity_detail_payload(detail, container)


def _property_payload(record: EntityPropertyRecord) -> EntityPropertyPayload:
    return EntityPropertyPayload(
        id=record.id,
        entity_id=record.entity_id,
        property_key=record.property_key,
        value=record.value,
        source=record.source,
        confidence=record.confidence,
        status=record.status,
        evidence_ids=list(record.evidence_ids),
        supersedes_property_id=record.supersedes_property_id,
        revision=record.revision,
        created_at=_optional_isoformat(record.created_at),
        updated_at=_optional_isoformat(record.updated_at),
    )


@router.put("/entities/{entity_id}/properties/{property_key}")
def set_entity_property(
    entity_id: str,
    property_key: str,
    payload: EntityPropertyRequest,
    container: Container = Depends(get_container),
) -> EntityPropertyPayload:
    """用户写下一个实体属性的当前值。1.0 ``app.py:1959-1966``。

    **这里只做异常的翻译，不重做判断。** 两条边界都在仓储里，而且与 1.0 逐字一致：
    ``_require_entity`` 抛 ``KeyError``（实体不存在）、``_clean_key`` 抛
    ``ValueError("property_key is required")``（键 trim 后为空）。1.0 把它们映成
    404 ``entity not found`` 与 400 ``str(error)``——400 的消息就是异常自己的话，
    所以这里也不改写它。

    写成属性的这条路**会顶掉派生值**（这是它存在的意义），所以它归 ``set_property``
    而不归 ``maintain_property``；两条路的差别在 ``EntityRepository`` 的 docstring 里。
    """
    try:
        written = container.entity_repository.set_property(
            entity_id, property_key, payload.value, evidence_ids=payload.evidence_ids or []
        )
    except KeyError as error:
        raise NotFoundError("entity not found") from error
    except ValueError as error:
        raise InvalidRequestError(str(error)) from error
    return _property_payload(written)


def _read_entity_groups(container: Container) -> ReadEntityGroups:
    """实体分组用例的装配。它复用实体详情那个用例来取成员详情——

    1.0 ``db.py:4254`` 的 ``get_entity_detail`` 就是逐个成员调的，2.0 保持同一条路，
    不在这里把详情再拼一遍。
    """
    return ReadEntityGroups(
        entities=container.entity_repository,
        details=_read_entity_detail(container),
    )


def _group_payload(container: Container, group: EntityGroup) -> EntityGroupPayload:
    return EntityGroupPayload(
        id=group.id,
        scope_id=group.scope_id,
        entity_type=group.entity_type,
        canonical_name=group.canonical_name,
        # 投影**已经在用例里算过**（成员排序用的就是投影后的名字），所以把结果传进去，
        # 不让它再查一遍属性表。
        members=[
            _entity_summary(
                container,
                member.entity,
                member.decoration,
                projected=(member.canonical_name, member.private),
            )
            for member in group.members
        ],
        member_entity_ids=list(group.member_entity_ids),
        source_labels=list(group.source_labels),
        semantic_details=list(group.semantic_details),
        evidence_count=group.evidence_count,
        confidence=group.confidence,
        preview_asset_id=group.preview_asset_id,
        preview_file_name=group.preview_file_name,
        preview_media_type=group.preview_media_type,
        rationale=EntityGroupRationale(
            strategy=group.rationale_strategy, normalized_key=group.canonical_name
        ),
        is_semantic_cluster=group.is_semantic_cluster,
    )


def _group_detail_payload(container: Container, detail: EntityGroupDetail) -> EntityGroupDetailResponse:
    """组详情：成员详情 + 把各成员的观察 / 事件按 id 去重后扁平合并。

    **去重放在这里而不是用例里**：它在 1.0 ``db.py:4256-4263`` 是用三个 ``set`` 做的，
    而那一段的**产物就是响应里的这两个数组**——搬进用例只会让同一份去重逻辑在两处各写
    一遍（路由侧还得把 payload 再拼一次）。顺序是**成员顺序**，1.0 也是。
    """
    members = [_entity_detail_payload(item, container) for item in detail.members]
    return EntityGroupDetailResponse(
        group=_group_payload(container, detail.group),
        members=members,
        observations=_dedupe_payloads(item for payload in members for item in payload.observations),
        events=_dedupe_payloads(item for payload in members for item in payload.events),
    )


def _dedupe_payloads(items: Iterable[_T]) -> list[_T]:
    """按 ``.id`` 去重、保序。1.0 用三个 ``set`` 各存各的 id，顺序即遍历顺序。"""
    seen: set[str] = set()
    ordered: list[_T] = []
    for item in items:
        if item.id in seen:
            continue
        seen.add(item.id)
        ordered.append(item)
    return ordered


@router.get("/entity-groups")
def list_entity_groups(
    scope_id: str | None = None,
    container: Container = Depends(get_container),
) -> EntityGroupListResponse:
    """语义实体组。1.0 ``app.py:1871-1873`` 加 ``db.py:4194-4236``。

    **现算的投影，不落表**——组没有表，``id`` 是拼出来的。所以没有「创建组」这种动作，
    也没有 LIMIT 与分页：一组只是一个概念加它的成员，**单成员组也返回**
    （``is_semantic_cluster`` 只是 ``len(members) > 1`` 的标记，不是过滤条件）。
    """
    groups = _read_entity_groups(container).execute(scope_id=scope_id)
    return EntityGroupListResponse(groups=[_group_payload(container, item) for item in groups])


@router.get("/entity-groups/{group_id}")
def get_entity_group(
    group_id: str,
    scope_id: str | None = None,
    container: Container = Depends(get_container),
) -> EntityGroupDetailResponse:
    """一个组。找不到是 404 ``semantic entity group not found``（1.0 ``app.py:1879``）。"""
    detail = _read_entity_groups(container).get(group_id, scope_id=scope_id)
    if detail is None:
        raise NotFoundError("semantic entity group not found")
    return _group_detail_payload(container, detail)


# ---- 人物 --------------------------------------------------------------------


@router.get("/people")
def list_people(
    status: str | None = None,
    scope_id: str | None = None,
    container: Container = Depends(get_container),
) -> PersonListResponse:
    # 类型过滤在路由层，不在仓储里——1.0 ``app.py:1514`` 就是这么写的。仓储给的是
    # 「全部实体」，要哪一类是各页自己的事（``/entities`` 那个路由另有一份过滤）。
    values = container.entity_repository.list_entities(status=status, scope_id=scope_id)
    people = [item for item in values if item.entity_type == ENTITY_TYPE_PERSON]
    return PersonListResponse(people=[_person_payload(container, item) for item in people])


@router.get("/people/{person_id}")
def get_person(person_id: str, container: Container = Depends(get_container)) -> PersonDetailResponse:
    person = _person_or_404(container, person_id)
    clusters = container.face_repository.list_clusters_for_entity(person.id)
    samples = [(cluster, container.face_repository.list_instances(cluster.id)) for cluster in clusters]
    avatar: FaceInstanceRecord | None = None
    for cluster, instances in samples:
        if cluster.status == CLUSTER_STATUS_REJECTED:
            continue
        for instance in instances:
            # 1.0 ``db.py:4576``：检测置信度降序、创建时间升序取首个。
            if avatar is None or (
                instance.detection_confidence,
                -instance.created_at.timestamp() if instance.created_at else 0.0,
            ) > (
                avatar.detection_confidence,
                -avatar.created_at.timestamp() if avatar.created_at else 0.0,
            ):
                avatar = instance
    return PersonDetailResponse(
        entity=_person_payload(container, person),
        avatar_face_instance_id=avatar.id if avatar else None,
        clusters=[_cluster_payload(container, cluster) for cluster, _ in samples],
    )


@router.patch("/people/{person_id}/name")
def rename_person(
    person_id: str,
    payload: RenamePersonRequest,
    container: Container = Depends(get_container),
) -> RenamePersonResponse:
    result = _name_person(container).rename(person_id, payload.name)
    if result is None:
        raise NotFoundError("person not found")
    refresh_memory_graph(container, result.entity.scope_id)
    return RenamePersonResponse(
        entity=_person_payload(container, result.entity), aliases=list(result.aliases)
    )


@router.post("/people/batch-confirm")
def batch_confirm_people(
    payload: BatchConfirmRequest, container: Container = Depends(get_container)
) -> BatchConfirmResponse:
    """逐件确认，逐件回执。**一件失败不影响其余**（1.0 ``app.py:2205`` 的形状）。"""
    use_case = _name_person(container)
    confirmed: list[BatchConfirmOutcome] = []
    failed: list[BatchConfirmFailure] = []
    scopes: set[str] = set()
    for item in payload.items:
        try:
            result = use_case.confirm_entity(item.person_id, name=item.name, family_role=item.family_role)
        except Exception as error:  # noqa: BLE001 — 逐件回执正是要把失败收进响应
            failed.append(
                BatchConfirmFailure(person_id=item.person_id, name=item.name, error=str(error)[:200])
            )
            continue
        if result is None:
            failed.append(
                BatchConfirmFailure(person_id=item.person_id, name=item.name, error="person not found")
            )
            continue
        confirmed.append(
            BatchConfirmOutcome(
                person_id=item.person_id,
                entity_id=result.entity.id,
                name=item.name,
                family_role=item.family_role,
                merged=result.merged_into is not None,
            )
        )
        scopes.add(result.entity.scope_id)
    for scope_id in sorted(scopes):
        refresh_memory_graph(container, scope_id)
    return BatchConfirmResponse(confirmed=confirmed, failed=failed, count=len(confirmed))


# ---- 身份预置 --------------------------------------------------------------------


def _store_seed_photo(
    container: Container, *, scope_id: str, file_name: str, content_type: str | None, content: bytes
) -> SeedPhoto:
    """把一张预置照片落成 asset + observation，返回用例要的那三个标识。

    **不走导入链。** 1.0 的预置是 ``create_asset`` + ``add_observation`` 两句
    （``app.py:2337-2339``），不跑 VLM、不建事件、不写词法投影——用户在说「这张脸是妈妈」，
    不是在说「这张照片属于某次旅行」。走导入链会让预置照片长出事件，把两个语义混起来。
    """
    # **id 不能用内容哈希。** 这条路原来写的是
    # ``f"asset_{sha256(content)[:12]}"``，而 ``assets.id`` 是**全局主键、不带 scope**——
    # 于是同一张预置照片只要在**任何一个** scope 里落过一次，之后每一轮预置都撞
    # ``UNIQUE constraint failed: assets.id``，路由回 500，``identity_seed`` 全军覆没。
    #
    # 实测（2026-10-10）：`faceid_1.jpg` 的哈希 `asset_131bea58c5d4` 早在
    # `PhotoBench-20261007-141043` 那一轮就落库了，**从那以后每一轮的 8 个人一个都没种进去**
    # （`seeded_count: 0, failed_count: 8`）——而"某一轮里的所有人都不存在"这件事，
    # 在评测读数上只表现为「人物类题答不对」。
    #
    # 口径改成与导入路径一致（``import_assets.py:325`` 的 ``_new_id("asset")``）：**随机 id
    # + 按 (scope, 内容哈希) 去重**。同一个 scope 里同样的字节仍然是同一个资产。
    digest = hashlib.sha256(content).hexdigest()
    existing = container.asset_repository.find_by_hash(scope_id, digest)
    if existing is not None:
        observations = container.observation_repository.list_by_asset(existing.id)
        if observations:
            # 已经有这个资产、也已经有观察——直接用它，不再落第二份。
            return SeedPhoto(asset_id=existing.id, observation_id=observations[0].id, path=existing.path)
    asset_id = f"asset_{uuid.uuid4().hex[:12]}"
    path = container.media_storage.store(asset_id, file_name, content)
    now = datetime.now(UTC)
    asset = Asset(
        id=asset_id,
        scope_id=scope_id,
        file_name=file_name,
        media_type=MediaType.from_upload(content_type, file_name),
        path=str(path),
        mime_type=content_type,
        size_bytes=len(content),
        status=AssetStatus.PROCESSED,
        content_sha256=digest,
        captured_at=now,
        captured_location=None,
        created_at=now,
        updated_at=now,
    )
    container.asset_repository.add(asset)
    observation = Observation(
        id=f"obs_{hashlib.sha256((asset_id + SEED_SOURCE_TYPE).encode()).hexdigest()[:12]}",
        scope_id=scope_id,
        asset_id=asset_id,
        source_type=SEED_SOURCE_TYPE,
        captured_at=now,
        confidence=0.0,
        created_at=now,
        updated_at=now,
    )
    container.observation_repository.add(observation)
    return SeedPhoto(asset_id=asset_id, observation_id=observation.id, path=str(path))


def _split_terms(raw: str | None) -> list[str]:
    """1.0 ``app.py:2323``：顿号或逗号分隔，去空、保序。

    别名与 ``face_instance_ids`` 共用这一条规则——两处都是「一个表单字段里的多个值」，
    两套分隔法只会让调用方猜。
    """
    return [item.strip() for item in (raw or "").replace("、", ",").split(",") if item.strip()]


@router.post("/people/seed", status_code=201)
async def seed_person_identity(
    name: str = Form(...),
    family_role: str | None = Form(None),
    aliases: str | None = Form(None),
    scope_id: str = Form("home-default"),
    face_instance_ids: str | None = Form(None),
    files: list[UploadFile] = File(default=[]),
    container: Container = Depends(get_container),
) -> SeedIdentityResponse:
    """两条入口：上传照片（1.0 的口径）与**指定已检出的人脸实例**（裁决二）。

    后者说「我指的是这张脸」，前者说「这张照片里的那个人」——在多人的家庭照上前者会挑
    质量最高的那张脸，而那通常不是用户想预置的人（见 ``seed_identity`` 的模块注释）。
    两种样本可以混用。
    """
    clean_name = name.strip()
    if not clean_name:
        # 1.0 在预置路由上判的是 400（``app.py:2321``），而「图里没有人脸」才是 422。
        # 两个不同的失败原因有两个不同的码，合并成一个码会让调用方分不清该改请求还是换图。
        raise InvalidRequestError("name is required")
    instance_ids = _split_terms(face_instance_ids)
    if not files and not instance_ids:
        # 1.0 的 ``files`` 是必填，所以这条在 1.0 不存在；两条入口之后它是新的边界：
        # 一个两个都没给请求不是「预置不出人」，是请求本身不成立（400，不是 422）。
        raise InvalidRequestError("files or face_instance_ids is required")
    photos = [
        _store_seed_photo(
            container,
            scope_id=scope_id,
            file_name=upload.filename or "identity.bin",
            content_type=upload.content_type,
            content=await upload.read(),
        )
        for upload in files
    ]
    try:
        result = _seed_identity(container).execute(
            scope_id=scope_id,
            name=clean_name,
            family_role=(family_role or "").strip() or None,
            aliases=_split_terms(aliases),
            photos=photos,
            face_instance_ids=instance_ids,
        )
        # 空间行（第 104 条的裁决，2026-09-24；落点改到校验之后是第 114 条）。
        #
        # **位置是按 1.0 的流程位置定的，不是按它的单行顺序。** 1.0 的预置路由走
        # ``create_asset``（``app.py:2337``），ensure 在那个函数里（``db.py:1462``）——
        # ``create_asset`` 是「建空间 + 建资产」在**同一个流程**里，所以一个什么都没建的请求
        # 根本走不到那一行。照抄它的单行顺序（ensure 排在写库之前）会把这条语义丢掉：404
        # （``face_instance_ids`` 指向不存在的实例）与 422（一张脸都没检出）都是在**写任何东西
        # 之前**判出来的，把它们排到 ensure 之后就说明「被拒的请求也建了空间」——而 1.0 里那行
        # 之所以在写之前，是因为它**后面紧接着就在写**，不是因为它该排在所有校验之前。
        #
        # 落点仍是两条入口的公共点（第 102 条挑 ``ImportAssets.execute`` 是同一条理由：实例
        # 那条入口一张资产都不写，接在 ``_store_seed_photo`` 上会漏掉它）。``list_clusters``
        # **内连** ``memory_spaces``，少了这一行，全新 scope 上预置出来的簇在审阅页里读不出来
        # ——不是「少一条数据」，是「这个人不存在」。
        container.spaces.ensure(scope_id, scope_id)
    except SeedInstanceNotFoundError as error:
        # 调用方指向了一个不存在的对象 → 404。与「图里没有人脸」（422）分开：
        # 一个要换 id，一个要换图。
        raise NotFoundError(str(error)) from error
    except NoSeedFacesError as error:
        # 1.0 在路由上抛 422（``app.py:2345``）：请求是好的，是这几张图里没有人脸。
        raise UnprocessableError(str(error)) from error
    refresh_memory_graph(container, scope_id)
    return SeedIdentityResponse(
        entity_id=result.entity_id,
        cluster_id=result.cluster_id,
        name=result.name,
        family_role=result.family_role,
        aliases=list(result.aliases),
        face_count=result.face_count,
        photos_without_faces=result.photos_without_faces,
    )


# ---- 人脸裁剪 --------------------------------------------------------------------


@router.get("/face-instances/{face_instance_id}/crop")
def face_instance_crop(face_instance_id: str, container: Container = Depends(get_container)) -> Response:
    """一张人脸的缩略图。1.0 ``app.py:2508``。

    bbox 是**方向校正后**的坐标（检测器用 cv2 读图，而 cv2 会应用 EXIF 方向），所以
    转置这一步必须与检测时一致——少了它，竖拍照片上的框会整体偏。
    """
    instance = container.face_repository.get_instance(face_instance_id)
    if instance is None or not instance.asset_path:
        raise NotFoundError("face instance not found")
    if len(instance.bbox) < 4:
        raise UnprocessableError("face crop is unavailable: invalid face bounding box")
    try:
        payload = container.preview_encoder.crop_jpeg(
            instance.asset_path,
            instance.bbox[:4],
            max_dimension=FACE_CROP_MAX_DIMENSION,
            quality=FACE_CROP_QUALITY,
        )
    except (OSError, ValueError, TypeError) as error:
        raise UnprocessableError(f"face crop is unavailable: {error}") from error
    return Response(
        content=payload,
        media_type="image/jpeg",
        headers={"Cache-Control": FACE_CROP_CACHE_CONTROL},
    )
