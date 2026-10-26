"""内容词检索。只做 HTTP 映射，召回与融合在 application 层。"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

from fastapi import APIRouter, Depends, HTTPException, Query

from sentrix.api.deps import get_container
from sentrix.api.routes.media import to_summary
from sentrix.api.schemas.search import ChannelTrace, SearchHit, SearchResponse
from sentrix.application.retrieval.evaluate_conditions import EvaluateConditions
from sentrix.application.retrieval.retrieve_entity import RetrieveEntity
from sentrix.application.retrieval.retrieve_evidence import EvidenceRequest, RetrieveEvidence
from sentrix.application.retrieval.retrieve_graph import RetrieveGraph
from sentrix.application.retrieval.retrieve_lexical import RetrieveLexical
from sentrix.application.retrieval.retrieve_metadata import RetrieveMetadata
from sentrix.application.retrieval.retrieve_text_ann import RetrieveTextAnn
from sentrix.application.retrieval.retrieve_visual import RetrieveVisual
from sentrix.bootstrap.container import Container
from sentrix.domain.media.models import MediaType
from sentrix.domain.retrieval.conditions import Condition
from sentrix.domain.retrieval.time_expression import parse_time_expression

log = logging.getLogger("sentrix.api.search")

router = APIRouter(tags=["search"])


def build_retrieve_evidence(container: Container) -> RetrieveEvidence:
    """把四条召回通道与编排接起来。**只在这里接一次**。

    1.0 的对应物是 ``build_default_retrievers`` 加 ``EvidenceRetrievalKernel`` 的构造
    （``backend/retrieval/__init__.py:65-85``）：只有**被启用**的通道才进 retriever 列表。
    1.0 的 text_ann 在 ``defaults.json`` 里是 ``"text": true``（启用的），
    而 2.0 到 S6 T115 才把它补上——那之前这里确实不构造它，因为「缺通道」在 1.0 里
    就是「不在列表里」，不是「构造一个返回空的通道」。现在五条都在了。

    ``recognize_place`` 的判据是「这张图本来就该有地点」= asset 上有 GPS，而
    ``asset_places`` 里没有行（``index_place.py`` 的第三个状态）。
    """
    return RetrieveEvidence(
        visual=RetrieveVisual(
            embedder=container.visual_embedder,
            vectors=container.vector_repository,
            index=container.vector_index,
        ),
        text=RetrieveTextAnn(embedder=container.text_embedder, vectors=container.vector_repository),
        entity=RetrieveEntity(index=container.lexical_search, observations=container.observation_repository),
        lexical=RetrieveLexical(index=container.lexical_search),
        metadata=RetrieveMetadata(
            assets=container.asset_repository,
            places=container.asset_place_repository,
            aliases=container.place_aliases,
            recognize_place=lambda asset_id: _has_gps(container, asset_id),
        ),
        conditions=EvaluateConditions(
            assets=container.asset_repository,
            observations=container.observation_repository,
            places=container.asset_place_repository,
            aliases=container.place_aliases,
            confirmed_people=_confirmed_people(container),
        ),
        graph=RetrieveGraph(graph=container.memory_graph),
    )


def _confirmed_people(container: Container) -> Callable[[str | None], frozenset[str]]:
    """按 scope 取已确认人物本名。**scope 为空（全部相册）时返回空集**。

    confirmed_person_names 要一个具体的 scope，而「全部相册」在 2.0 用 None
    表达。跨相册取并集要另写一条查询，而这里**不发明它**——人物条件的第 1 支
    （观察自己的 people 快照）本来就覆盖了全部相册的情形，这一支只是
    「这个 scope 里还有没有这个人」的补充。为空时那一支不生效，
    而结果**只会更严**（少匹配），不会更松。
    """

    def names(scope_id: str | None) -> frozenset[str]:
        if scope_id is None:
            return frozenset()
        return container.entity_repository.confirmed_person_names(scope_id)

    return names


def _conditions(
    *, time_range: str, person: Sequence[str], place: str, media_type: str
) -> tuple[Condition, ...]:
    """把查询参数变成结构化条件。

    **每条都是 HARD。** 1.0 的 ``HardFilterContext.from_spec`` 只把
    ``strictness == HARD`` 的约束放进过滤上下文，而 HTTP 这一层没有「软条件」的入口——
    用户显式填进 ``time_range`` 的东西不该被当成建议。哪天要加 SEMANTIC 档，
    那是一个新的查询参数，不是改这里的默认值。

    **认不出来的时间表达式不产生条件。** ``parse_time_expression`` 返回 ``None`` 时
    这里跳过——若硬造一个空区间的条件，它的判定会是 UNKNOWN，而 HARD 档下
    UNKNOWN 一样排除候选，于是「用户输入了个看不懂的时间」会表现为「一张照片都搜不到」。
    """
    built: list[Condition] = []
    if time_range.strip() and parse_time_expression(time_range) is not None:
        built.append(Condition(dimension="time", value=time_range.strip(), key="time"))
    for name in person:
        if name.strip():
            built.append(Condition(dimension="person", value=name.strip(), key=f"person:{name}"))
    if place.strip():
        built.append(Condition(dimension="place", value=place.strip(), key="place"))
    if media_type.strip():
        built.append(Condition(dimension="media", value=media_type.strip(), key="media"))
    return tuple(built)


def _has_gps(container: Container, asset_id: str) -> bool:
    asset = container.asset_repository.get(asset_id)
    return asset is not None and asset.captured_location is not None


@router.get("/search")
def search_assets(
    q: str = Query(..., min_length=1, max_length=200, description="内容词，例如 桥 / 海边 / 红色"),
    # 空串与不传都表示**全部相册**（1.0 的 `if scope_id:`）。默认值从 "home-default"
    # 改成 "" 是行为修正：原来不传会去查一个叫 home-default 的空间，而它不存在。
    scope_id: str = "",
    limit: int = Query(20, ge=1, le=100),
    time_range: str = Query("", max_length=60, description="时间条件，例如 2023年10月 / 2025年-2026年"),
    person: list[str] = Query(default=[], description="人物条件，可重复：scope 内已确认人物的本名"),
    place: str = Query("", max_length=60, description="地点条件，服务端按解析出的行政区/地名匹配"),
    media_type: str = Query("", max_length=20, description="媒体类型：image / video / audio / text"),
    container: Container = Depends(get_container),
) -> SearchResponse:
    try:
        media_types = (MediaType(media_type),) if media_type.strip() else ()
    except ValueError as error:
        raise HTTPException(status_code=400, detail=f"未知的媒体类型: {media_type}") from error

    use_case = build_retrieve_evidence(container)
    result = use_case.execute(
        EvidenceRequest(
            text=q,
            scope_id=scope_id or None,
            limit=limit,
            # 正向条件同时喂两处：**召回前筛**（metadata 通道用它决定要不要给信号）
            # 与**融合后排除**（conditions）。两者不是一回事，见 conditions.py 的模块注释。
            time_bounds=parse_time_expression(time_range) if time_range.strip() else None,
            media_types=media_types,
            place=place.strip(),
            person=tuple(name.strip() for name in person if name.strip()),
            conditions=_conditions(time_range=time_range, person=person, place=place, media_type=media_type),
        )
    )

    items: list[SearchHit] = []
    for hit in result.hits:
        asset = container.asset_repository.get(hit.asset_id)
        if asset is None:
            # 向量还在、资产没了（scope 被删等）。产品硬约束第 5 条要求查询返回
            # **可打开的**原图，所以只能丢弃——但丢弃这件事必须留痕。
            log.warning("检索命中了一个已不存在的资产，已丢弃: %s", hit.asset_id)
            continue
        items.append(
            SearchHit(
                asset=to_summary(asset),
                score=hit.score,
                channels=dict(hit.channels),
                raw_scores=dict(hit.raw_scores),
            )
        )

    return SearchResponse(
        query=q,
        backend=result.backend,
        degraded=result.degraded,
        degraded_reason=result.degraded_reason,
        authoritative_vectors=result.authoritative_vectors,
        excluded_count=result.excluded_count,
        channel_trace=[
            ChannelTrace(
                channel=trace.channel,
                status=trace.status,
                candidate_count=trace.candidate_count,
                latency_ms=trace.latency_ms,
                backend=trace.backend,
                reason=trace.reason,
            )
            for trace in result.channel_trace
        ],
        items=items,
    )
