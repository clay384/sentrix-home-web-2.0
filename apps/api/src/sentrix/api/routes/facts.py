"""事实端点：列表 + 确认 / 驳回。

对应 1.0 ``app.py:2539`` 的 ``GET /api/facts``、``:3905`` 的 ``confirm``、
``:3913`` 的 ``reject``。

**读侧直连仓储，不建用例**——与 ``GET /assets``（``media.py:185``）同一体例：
一次纯查询没有要落地的领域决定，套一层用例只会多一个转发。**写侧走用例**，
因为「确认要连作废旧值一起做」是个领域决定（见 ``application/memories/review_fact.py``）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from sentrix.api.deps import get_container
from sentrix.api.routes.media import refresh_memory_graph
from sentrix.api.schemas.facts import FactListResponse, FactPayload
from sentrix.application.memories.review_fact import ReviewFact
from sentrix.bootstrap.container import Container
from sentrix.domain.memories.facts import Fact

DEFAULT_LIMIT = 200
MAX_LIMIT = 1000

router = APIRouter(tags=["facts"])


def _payload(fact: Fact) -> FactPayload:
    return FactPayload(
        id=fact.id,
        scope_id=fact.scope_id,
        subject=fact.subject,
        predicate=fact.predicate,
        object=fact.object,
        status=str(fact.status),
        confidence=fact.confidence,
        evidence_ids=list(fact.evidence_ids),
        revision=fact.revision,
        supersedes_fact_id=fact.supersedes_fact_id,
    )


def _review_fact(container: Container) -> ReviewFact:
    return ReviewFact(facts=container.fact_repository)


@router.get("/facts")
def list_facts(
    # 空串与不传都表示**全部相册**（1.0 的 ``list_facts(1000)`` 根本没有 scope 参数）。
    # 2.0 的端口一度把它写成必填——那是在端口层面收窄了 1.0 的行为，而当时这个方法
    # 零调用，所以没人碰得上。接线时按 1.0 改回可选，与 ``/assets`` ``/timeline``
    # ``/search`` 三处同一口径。
    scope_id: str = "",
    status: str = Query(
        "", max_length=20, description="按状态过滤：active / pending / superseded / retracted"
    ),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    container: Container = Depends(get_container),
) -> FactListResponse:
    facts = container.fact_repository.list_facts(scope_id or None, limit=limit)
    # 状态过滤放在**取回之后**，与 1.0 同序（``app.py:2542`` 的列表推导）。
    # 放进 SQL 也行，但那样「过滤后为空」与「本来就没有」在读代码时不容易分辨，
    # 而这条链路上两者要区分（前者说明筛选条件太窄，后者说明事实还没写进来）。
    wanted = status.strip()
    return FactListResponse(
        facts=[_payload(item) for item in facts if not wanted or str(item.status) == wanted]
    )


@router.post("/facts/{fact_id}/confirm")
def confirm_fact(fact_id: str, container: Container = Depends(get_container)) -> FactPayload:
    fact = _review_fact(container).confirm(fact_id)
    refresh_memory_graph(container, fact.scope_id)
    return _payload(fact)


@router.post("/facts/{fact_id}/reject")
def reject_fact(fact_id: str, container: Container = Depends(get_container)) -> FactPayload:
    fact = _review_fact(container).reject(fact_id)
    refresh_memory_graph(container, fact.scope_id)
    return _payload(fact)
