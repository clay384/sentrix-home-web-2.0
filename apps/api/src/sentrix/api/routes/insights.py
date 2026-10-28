"""人物画像端点：读视图、跑一趟、单个人的画像与反馈。

移植 1.0 ``app.py`` 的 ``GET /api/person-insights``（:1568）、``GET``/``PATCH``
``/api/people/{person_id}/portrait``（:1764 / :1791）、
``POST /api/people/{person_id}/portrait-feedback``（:1776）。

**1.0 的三个 run 端点（``POST /api/person-insight-runs``、``GET .../{run_id}``、
``POST .../{run_id}/retry``）没有搬，因为 2.0 没有 run 表**（``0006_identity.sql:234``
写明那列随之取消）。它们合起来是「起一个后台线程跑、返回 202、让前端轮询」那一套，
而 2.0 没有任务队列（``routes/timeline.py`` 的 ``import_album`` docstring 已说明）。
替代品是一个**同步**的 ``POST /person-insights/run``：跑完才返回，响应体就是那六个计数
（``InsightRun``）。

代价照写：**请求会挂很久**（每个核心人物至少一次模型调用，加上 moments 阶段每人每资产
一次），与 ``import_album`` 是同一个量级、同一类问题。换来的是不必建一张只有一条写入者
的状态机表。这条在计划里是明确接受的。

**读视图比 1.0 少四块**，全是裁决① 的连带（``infer_graph`` 整个不搬）：``run``
（没有那张表）、``role_candidates``、``album_owner_candidates``、
``relationship_hypotheses``（三者的写入者都是 ``infer_graph`` 或角色假设功能，
0006 没建对应的表）。**不返回恒空的占位键**——留一个永远是 ``[]`` 的键会让读的人以为
那条链路是通的。理由同 ``s5-deviations.md`` 第 123 条去掉 ``suggested_relationships``。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from sentrix.api.deps import get_container
from sentrix.api.errors import InvalidRequestError, NotFoundError
from sentrix.api.schemas.insights import PortraitFeedbackRequest, RunInsightsRequest
from sentrix.application.identity.extract_moments import ExtractMoments
from sentrix.application.identity.run_insights import RunInsights
from sentrix.bootstrap.container import Container
from sentrix.domain.identity.insights import CORE_PEOPLE_LIMIT, rank_core_people
from sentrix.domain.identity.models import DEFAULT_SCOPE_ID, PortraitRevisionRecord

router = APIRouter(tags=["insights"])

# 1.0 ``app.py:1570`` 的 ``scope_id or "home-default"``。读视图给空值时兜底成默认空间
# （「看看默认那个相册里有哪些人」是个合理的默认动作），而**跑流水线不给兜底**——
# 它是重活，写错空间会往那个空间里灌一批真的画像行。
ANONYMOUS_NAME = "未命名成员"


def _extract_moments(container: Container) -> ExtractMoments:
    return ExtractMoments(
        assets=container.asset_repository,
        faces=container.face_repository,
        entities=container.entity_repository,
        moments=container.moment_repository,
        previews=container.person_preview_renderer,
        vision=container.person_vision,
        # 与整册导入的推理池同一个数：8100 有 4 个 slot，4 个并发请求正好铺满；
        # 逐张串行会把 4 个 slot 用成 1 个（album3-14 实测 insights 418s）。
        workers=container.settings.album_workers,
    )


def _run_insights(container: Container) -> RunInsights:
    return RunInsights(
        faces=container.face_repository,
        entities=container.entity_repository,
        moments=container.moment_repository,
        relationships=container.relationship_repository,
        portraits=container.portrait_repository,
        extract_moments=_extract_moments(container),
        vision=container.person_vision,
    )


def run_insights_for_scope(container: Container, scope_id: str) -> dict[str, int]:
    """跑一趟并返回那六个计数。**给别的路由用**（``routes/timeline.py`` 的批末画像）。

    单独一个函数而不是让调用方自己去拼 ``RunInsights``：装配那几个仓储的写法只该有
    一份，两处各拼一次迟早会漂。判据（空 ``scope_id`` 报 400）**不在这里**——那是
    HTTP 层的事，这个函数只接已经校验过的值。
    """
    run = _run_insights(container)(scope_id)
    return {
        "ranked": run.ranked,
        "core": run.core,
        "selections": run.selections,
        "moments": run.moments,
        "moment_failures": run.moment_failures,
        "portraits": run.portraits,
    }


@router.post("/person-insights/run")
def run_person_insights(
    payload: RunInsightsRequest, container: Container = Depends(get_container)
) -> dict[str, int]:
    """同步跑一趟五个阶段，返回计数。**不返回 run 对象——没有那个东西。**

    空 ``scope_id`` 报 400（1.0 原样）：这是重活，不该替调用方猜一个空间去写。
    """
    scope_id = payload.scope_id.strip()
    if not scope_id:
        raise InvalidRequestError("scope_id is required")
    return run_insights_for_scope(container, scope_id)


@router.get("/person-insights")
def list_person_insights(
    scope_id: str = "", container: Container = Depends(get_container)
) -> dict[str, object]:
    """这个空间里的人物按核心度分档，各带当前生效的画像。

    ``tier`` 由 ``rank_core_people`` 判（跨够天数且排进前 ``CORE_PEOPLE_LIMIT`` 的算
    ``core``），路由只负责把记录字段搬进响应——**不在这里重算分数**。
    """
    scope = scope_id or DEFAULT_SCOPE_ID
    tiers: dict[str, list[dict[str, object]]] = {"core": [], "common": [], "incidental": []}
    for item in rank_core_people(
        container.face_repository.person_candidate_features(scope), limit=CORE_PEOPLE_LIMIT
    ):
        person = container.entity_repository.get(str(item["person_id"]))
        portrait = container.portrait_repository.get_active(str(item["person_id"]))
        tier = str(item.get("tier") or "incidental")
        tiers.setdefault(tier, []).append(
            {
                **item,
                # 1.0 ``app.py:1583`` 的兜底：还没命名的人显示成「未命名成员」而不是
                # 一个 ``person_ab12cd34`` 这样的 id。
                "display_name": (person.canonical_name if person else "") or ANONYMOUS_NAME,
                "family_role": person.family_role if person else None,
                "status": person.status if person else None,
                "portrait": _portrait_payload(portrait),
            }
        )
    return {"scope_id": scope, "tiers": tiers}


@router.get("/people/{person_id}/portrait")
def get_person_portrait(person_id: str, container: Container = Depends(get_container)) -> dict[str, object]:
    """这个人当前生效的那一版画像。没有就是 404。

    ``user_locked`` 也算生效（``PortraitRepository.get_active`` 的 docstring：
    锁住的是「这一版别被顶掉」，不是「这一版作废」）。
    """
    portrait = container.portrait_repository.get_active(person_id)
    if portrait is None:
        raise NotFoundError("portrait not found")
    return _portrait_payload(portrait) or {}


@router.post("/people/{person_id}/portrait-feedback")
def set_person_portrait_feedback(
    person_id: str,
    payload: PortraitFeedbackRequest,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """记用户对**当前生效那版**的反馈。不新建版本。

    找不到当前版本就 404——1.0 在 ``app.py:1776`` 也是先取再写；这里把那个顺序收进
    路由，因为「先取哪一版」是读侧的事（``get_active``），不是仓储写方法该猜的。
    """
    active = container.portrait_repository.get_active(person_id)
    if active is None:
        raise NotFoundError("portrait not found")
    updated = container.portrait_repository.set_feedback(active.id, (payload.feedback or "").strip() or None)
    if updated is None:
        raise NotFoundError("portrait not found")
    return _portrait_payload(updated) or {}


def _portrait_payload(portrait: PortraitRevisionRecord | None) -> dict[str, object] | None:
    """一版画像的响应形状。``None`` 进、``None`` 出（调用方决定是空值还是 404）。"""
    if portrait is None:
        return None
    return {
        "id": portrait.id,
        "person_id": portrait.person_id,
        "revision": portrait.revision,
        "portrait_text": portrait.portrait_text,
        "themes": list(portrait.themes),
        "evidence_refs": list(portrait.evidence_refs),
        "status": portrait.status,
        "trigger_type": portrait.trigger_type,
        "model_name": portrait.model_name,
        "prompt_version": portrait.prompt_version,
        "feedback": portrait.user_feedback,
    }
