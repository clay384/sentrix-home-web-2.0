"""关系端点：关系图、增删、确认/撤回、批量导入。

对应 1.0 ``app.py:2098``（``GET``）、``:2111``（``POST``）、``:2120``（``batch``）、
``:2169``（``confirm``）、``:2177``（``retract``）。

**读侧直连仓储，写侧两条分工不同**（体例同 ``routes/facts.py``）：``POST`` / ``confirm``
/ ``retract`` 与仓储方法一一对应，是纯转发；``batch`` 走用例 ``ImportRelationships``——
名字解析与逐条失败收集是编排，不是搬字段。

**写侧三个端点回的名字是空的，这是有意的。** 1.0 三个端点的返回投影**各不相同**，
而且是三条不同 SQL 的副作用、不是设计：``POST`` 回 ``SELECT * FROM relationships
WHERE id = ?``（**没有 JOIN**，所以没有 ``subject_name`` / ``object_name``）；
``confirm`` 回的是 ``list_relationships()`` 里找出来的那一行（**有** JOIN，所以有名字）；
``retract`` 回的是**未解码**的原始行（``evidence_ids_json`` 还是字符串，连
``evidence_ids`` 这个键都没有）。三者还有一个共同点：**1.0 的前端一个都不读**——
``app.js:1831`` 那个 ``const created =`` 赋值完就再没用过，另外四处直接丢弃返回值。
既然没有消费方，2.0 给一个形状（``RelationshipPayload``）：名字留空，
``evidence_ids`` 统一解成列表。不为一个没人读的差异再造两个响应模型。

**``POST`` 走用例，``confirm`` / ``retract`` 直连仓储。** 差别是 ``POST`` 在
``status == "active"`` 时还要往语义声明层写一条 claim（1.0 那句「已保存家庭关系：X，
并写入长期记忆」）——那是编排，归 ``CreateRelationship``；另外两条是一对一的转发。

**``POST`` 上那条同空间校验不接。** 1.0 的 ``create_relationship`` 对「两端不在同一个
记忆空间」抛 ``ValueError``，而 1.0 的 ``app.py`` **没有任何** ``exception_handler``
（实测 grep 零命中），所以它当时就是一个裸 500。2.0 照搬不接——改成 400 是引入 1.0
没有的行为，没有证据支撑。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends

from sentrix.api.deps import get_container
from sentrix.api.errors import InvalidRequestError, NotFoundError
from sentrix.api.routes.media import refresh_memory_graph
from sentrix.api.schemas.relationships import (
    RelationshipBatchRequest,
    RelationshipBatchResponse,
    RelationshipBatchResultItem,
    RelationshipCreateRequest,
    RelationshipEdge,
    RelationshipGraphResponse,
    RelationshipNode,
    RelationshipPayload,
)
from sentrix.application.identity.create_relationship import CreateRelationship
from sentrix.application.identity.import_relationships import (
    ImportRelationships,
    RelationshipRequest,
)
from sentrix.bootstrap.container import Container
from sentrix.domain.identity.models import ENTITY_TYPE_PERSON, RelationshipRecord

# 1.0 ``app.py:2114`` 的原话，逐字（前端与 PhotoBench 都能拿到这句）。
REQUIRED_FIELDS_MESSAGE = "subject_entity_id, predicate and object_entity_id are required"

# 1.0 ``app.py:2113`` 的 ``payload.get("status", "pending")``。
DEFAULT_STATUS = "pending"

# 1.0 ``app.py:2114`` 的 ``float(payload.get("confidence", 0.5) or 0.5)``。
DEFAULT_CONFIDENCE = 0.5

router = APIRouter(tags=["relationships"])


def _optional_isoformat(value: datetime | None) -> str | None:
    """与 ``routes/identity.py`` 的同名私有函数逐字相同。两个模块各自需要它，
    提到公共位置只会为一个四行的纯函数多开一个模块。"""

    return value.isoformat() if value is not None else None


def _create_relationship(container: Container) -> CreateRelationship:
    """建关系那条路要三个协作者：关系、实体（取对方的名字）、语义声明（确认时写 claim）。"""
    return CreateRelationship(
        entities=container.entity_repository,
        relationships=container.relationship_repository,
        claims=container.semantic_repository,
    )


def _payload(record: RelationshipRecord) -> RelationshipPayload:
    return RelationshipPayload(
        id=record.id,
        scope_id=record.scope_id,
        subject_entity_id=record.subject_entity_id,
        predicate=record.predicate,
        object_entity_id=record.object_entity_id,
        inverse_predicate=record.inverse_predicate,
        status=record.status,
        confidence=record.confidence,
        evidence_ids=list(record.evidence_ids),
        revision=record.revision,
        created_at=_optional_isoformat(record.created_at),
        updated_at=_optional_isoformat(record.updated_at),
        subject_name=record.subject_name,
        object_name=record.object_name,
    )


@router.get("/relationships")
def list_relationships(
    scope_id: str | None = None,
    kind: str | None = None,
    container: Container = Depends(get_container),
) -> RelationshipGraphResponse:
    """关系图。``kind == "person"`` 时**两端都必须是人物**，且排掉 ``retracted``。

    两条分支的过滤位置不同，都是 1.0 的：``person`` 那支在 SQL 里
    （``list_person_relationships``），另一支在 Python 里——``list_entities`` 不带类型
    参数（``app.py:2100`` 的列表推导）。两边的**状态口径也相反**：``person`` 排
    ``retracted``，另一支连 ``retracted`` 都给。
    """
    if kind == "person":
        entities = [
            item
            for item in container.entity_repository.list_entities(scope_id=scope_id)
            if item.entity_type == ENTITY_TYPE_PERSON
        ]
        values = container.relationship_repository.list_person_relationships(scope_id)
    else:
        entities = list(container.entity_repository.list_entities(scope_id=scope_id))
        values = container.relationship_repository.list_relationships(scope_id)
    return RelationshipGraphResponse(
        nodes=[
            RelationshipNode(
                id=item.id,
                label=item.canonical_name,
                status=item.status,
                entity_type=item.entity_type,
            )
            for item in entities
        ],
        edges=[
            RelationshipEdge(
                source=item.subject_entity_id,
                target=item.object_entity_id,
                label=item.predicate,
                status=item.status,
                id=item.id,
            )
            for item in values
        ],
        relationships=[_payload(item) for item in values],
    )


@router.post("/relationships")
def create_relationship(
    payload: RelationshipCreateRequest,
    container: Container = Depends(get_container),
) -> RelationshipPayload:
    """建一条关系，或把已有的那条并掉（同三元组去重在仓储里）。

    **三个必填项是路由判的**，不是 pydantic——1.0 给的是 400 加一句具体的话，
    缺字段走 pydantic 会变成 422，那是同一个产品行为的另一个码。
    """
    if not (payload.subject_entity_id and payload.predicate and payload.object_entity_id):
        raise InvalidRequestError(REQUIRED_FIELDS_MESSAGE)
    # 1.0 是 ``payload.get("status", "pending")``：**键缺席**才落默认值，空串照传。
    # 唯一照搬不了的是显式 ``null``——1.0 会把它塞进 ``NOT NULL`` 列换来一个
    # IntegrityError（它没有 exception_handler，所以是个裸 500）。**不复制崩溃路径**，
    # 这里归一成默认值；1.0 的调用方都不发 null（前端恒发字符串或省略）。
    if "status" not in payload.model_fields_set or payload.status is None:
        status = DEFAULT_STATUS
    else:
        status = payload.status
    created = _create_relationship(container).execute(
        subject_entity_id=payload.subject_entity_id,
        predicate=payload.predicate,
        object_entity_id=payload.object_entity_id,
        evidence_ids=payload.evidence_ids or (),
        confidence=float(payload.confidence or DEFAULT_CONFIDENCE),
        status=status,
    )
    refresh_memory_graph(container, created.scope_id)
    return _payload(created)


@router.post("/relationships/batch")
def import_relationships(
    payload: RelationshipBatchRequest,
    container: Container = Depends(get_container),
) -> RelationshipBatchResponse:
    """批量导入。**逐条失败不让整批失败**——1.0 的 ``except Exception`` 就在循环里。

    调用方是 PhotoBench 的 identity_seed 阶段，详见用例的模块 docstring。
    """
    outcome = ImportRelationships(
        entities=container.entity_repository,
        relationships=container.relationship_repository,
    ).execute(
        scope_id=payload.scope_id or "",
        entity_by_name=payload.entity_by_name or {},
        requests=[
            RelationshipRequest(
                subject=item.subject or "",
                predicate=item.predicate or "",
                object=item.object or "",
                confidence=float(item.confidence or DEFAULT_CONFIDENCE),
            )
            for item in payload.relationships or []
        ],
    )
    if payload.scope_id:
        refresh_memory_graph(container, payload.scope_id)
    return RelationshipBatchResponse(
        requested=outcome.requested,
        imported=outcome.imported,
        results=[
            RelationshipBatchResultItem(
                subject=item.subject,
                predicate=item.predicate,
                object=item.object,
                id=item.id,
                error=item.error,
            )
            for item in outcome.results
        ],
    )


@router.post("/relationships/{relationship_id}/confirm")
def confirm_relationship(
    relationship_id: str,
    container: Container = Depends(get_container),
) -> RelationshipPayload:
    """确认。仓储会把同三元组的**其它 active 关系转 superseded**。

    **这里不写 semantic claim**——1.0 的 ``confirm_relationship`` 也不写，只有
    ``create_relationship(status='active')`` 写。那是一条反直觉但真实的行为，
    写在仓储的 docstring 里。
    """
    confirmed = container.relationship_repository.confirm(relationship_id)
    if confirmed is None:
        raise NotFoundError("relationship not found")
    refresh_memory_graph(container, confirmed.scope_id)
    return _payload(confirmed)


@router.post("/relationships/{relationship_id}/retract")
def retract_relationship(
    relationship_id: str,
    container: Container = Depends(get_container),
) -> RelationshipPayload:
    """撤回。转 ``retracted`` 并 ``revision + 1``，**不删行**——撤回也留痕。"""
    retracted = container.relationship_repository.retract(relationship_id)
    if retracted is None:
        raise NotFoundError("relationship not found")
    refresh_memory_graph(container, retracted.scope_id)
    return _payload(retracted)
