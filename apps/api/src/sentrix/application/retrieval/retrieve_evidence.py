"""多路召回编排：把已启用的通道按加权 RRF 融成一份排名。

对应 1.0 ``backend/evidence_retrieval.py:152-243`` 的
``EvidenceRetrievalKernel._retrieve_multi`` 的**召回 + 融合**两段。1.0 那条链有四步：

    多通道召回 → rank(weighted_rrf) → 种子门控的 adjacency 扩展 → 条件证据判定

S4 只搬前两步。后两步要种子分级与 ``EvidencePacket``，属于 S6。

**接线逐条照搬 1.0：**

- 通道集合来自 ``build_default_retrievers``（``backend/retrieval/__init__.py:65-85``），
  构建顺序 ``metadata → entity → lexical → visual_ann → text_ann → adjacency``；
- 召回循环**跳过 ``kind == "expander"`` 的通道**（``evidence_retrieval.py:198-201``），
  只有 primary 通道进 ``channel_hits``；
- 融合只有一次调用，权重表用 ``DEFAULT_CHANNEL_WEIGHTS``：``evidence_retrieval.py:241-243``
  → ``retrieval/ranking.py:101-110``（``weighted_rrf`` 分支）→ ``fusion.fuse``；
- **缺通道的形态是「不出现」**，不是空列表也不是错误。1.0 里没启用的通道压根不在
  ``retrievers`` 列表里，``channel_hits`` 里就没有那个键。2.0 的 entity 到 S5b 才有类
  （Task 83），text_ann 要等 S6——后者在这里连调用都没有，不是「调了然后返回空」。

**adjacency 不接。** 1.0 ``configs/retrieval/defaults.json:10`` 里 ``"adjacency": false``，
它连实例都不会被建；即使建了，``kind="expander"`` 也让它不进并行召回——Kernel 拿
primary 融合结果里的 exact/strong 当种子再 ``expand``（``evidence_retrieval.py:246-273``），
扩展结果 append 在主结果之后。S4 没有种子分级（那是 S6 的条件判定），接进来只会多一条
恒空的通道。

**通道顺序是语义，不是排版。** ``fusion.fuse`` 在候选**第一次出现**时按当时那个通道定
``evidence_class``（``domain/retrieval/fusion.py:76-84``），而 metadata 是 anchor 通道：
一个同时被 metadata 与 lexical 命中的候选，只有 metadata 先进才拿得到 +1.0 的锚点加成。
1.0 的构建顺序就是 metadata 在前。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sentrix.application.retrieval.evaluate_conditions import EvaluateConditions
from sentrix.application.retrieval.retrieve_entity import RetrieveEntity
from sentrix.application.retrieval.retrieve_graph import GRAPH_CHANNEL, RetrieveGraph
from sentrix.application.retrieval.retrieve_lexical import RetrieveLexical
from sentrix.application.retrieval.retrieve_metadata import RetrieveMetadata
from sentrix.application.retrieval.retrieve_text_ann import (
    RetrieveTextAnn,
    TextSearchRequest,
    TextSearchResult,
)
from sentrix.application.retrieval.retrieve_visual import (
    RetrieveVisual,
    VisualSearchRequest,
    VisualSearchResult,
)
from sentrix.domain.media.models import MediaType
from sentrix.domain.retrieval.conditions import Condition
from sentrix.domain.retrieval.fusion import DEFAULT_CHANNEL_WEIGHTS, fuse
from sentrix.domain.retrieval.models import CandidateHit, ChannelTrace, FusedCandidate

log = logging.getLogger("sentrix.retrieval.evidence")

METADATA_CHANNEL = "metadata"
ENTITY_CHANNEL = "entity"
LEXICAL_CHANNEL = "lexical"
VISUAL_CHANNEL = "visual_ann"
TEXT_CHANNEL = "text_ann"

# 每条通道按调用方上限过取一层再融合。1.0 把理由写在了注释里
# （``evidence_retrieval.py:171-176``）："Ask each enabled channel for the complete
# authorized scope so a relevant asset cannot disappear merely because another channel
# ranked it below an arbitrary head."——它的做法是把每条通道的 ``recall_limit`` 抬到
# ``max(requested_limit, authorized_count)``，即整个已授权 scope（``:176-181``）。
# 2.0 不整册回灌（一次查询把全 scope 拉进内存），改用与视觉 ANN 同一个过取倍数
# （``retrieve_visual.py`` 的 ``OVERSAMPLE_FACTOR = 4``）：**融合前不裁到交付上限**，
# 融合之后才按 limit 交付。
RECALL_OVERSAMPLE_FACTOR = 4
GRAPH_CHANNEL_WEIGHT = 0.5


@dataclass(frozen=True, slots=True)
class EvidenceRequest:
    """一次多路召回的输入。

    结构化条件（时间窗 / 媒体类型 / 地点）对应 1.0 ``HardFilterContext``
    （``backend/retrieval/base.py:23-67``）的正向条件那几格。S4 的 HTTP 端点还传不了
    它们（硬条件与查询解析属于 S6），但 metadata 通道**只在有正向条件时才给召回信号**
    （``retrieve_metadata.py`` 的早退），所以这几个字段必须存在，否则那条通道在类型上
    就没法被喂过——它会永远返回空，而不是「被接上了」。
    """

    text: str
    # None = 跨全部相册（1.0 `db.py:1514` 的 `if scope_id:`）。原先默认 `"home-default"`，
    # 那是一个**具体的空间**——不传 scope 时去查它，而它在 2.0 的空库里不存在。
    scope_id: str | None = None
    limit: int = 20
    media_types: tuple[MediaType, ...] = ()
    negated_media: tuple[MediaType, ...] = ()
    time_bounds: tuple[datetime, datetime] | None = None
    place: str = ""
    # 人物条件（1.0 ``RetrievalQuery.constraints`` 里 ``dimension == "person"`` 的那几条）。
    # 与上面三个正向条件同一个处境：S5 的 HTTP 端点还传不了它（把「妈妈的照片」解析成
    # 人物条件属于 S6 的查询解析），但 **entity 通道只在有人物条件时才给召回信号**——
    # 这个字段不存在的话，那条通道在类型上就没法被喂过，它会永远返回空，而不是「被接上了」。
    person: tuple[str, ...] = ()
    # 结构化条件（时间 / 媒体 / 地点 / 人物）。**与上面几个字段不是一回事**：
    # 那几个是传给通道的**召回前筛**，它们影响「召回什么」，不保证结果满足条件；
    # 这一条走的是**融合后的排除**，那才是「硬条件」的全部含义。
    conditions: tuple[Condition, ...] = ()


@dataclass(frozen=True, slots=True)
class EvidenceHit:
    """一条融合后的候选。``channels`` 是「被哪些通道以第几名召回」。"""

    asset_id: str
    score: float
    channels: Mapping[str, int] = field(default_factory=dict)
    raw_scores: Mapping[str, float] = field(default_factory=dict)
    # 融合层给这条候选的**证据分级**（``anchor`` / ``semantic`` / ``expander``，见
    # ``fusion.evidence_class_for``）。``FusedCandidate`` 一直在算它，而这里原先
    # 在转成 ``EvidenceHit`` 时把它丢了——于是「这条命中的是确定性锚点还是相似度」
    # 到不了检索预览，模型只能按名次猜哪张更可能是自己要找的。
    evidence_class: str = "semantic"


@dataclass(frozen=True, slots=True)
class EvidenceResult:
    """融合结果 + 视觉通道报出来的后端健康度。

    ``backend`` / ``degraded`` 取自视觉通道：它是唯一一个「派生索引可能落后于权威
    存储」的通道（``retrieve_visual.py`` 的模块注释）。缺口必须一路传到 HTTP 响应，
    不能只有日志里那一行——1.0 就是这么把降级丢掉的。
    """

    hits: Sequence[EvidenceHit] = field(default_factory=tuple)
    backend: str = "none"
    degraded: bool = False
    degraded_reason: str = ""
    authoritative_vectors: int = 0
    # 被条件排除掉的候选数。1.0 ``EvidencePacket.excluded_count``（``:42``）。
    # **必须传出来**：没有它，「条件把所有候选都排除了」与「本来就没有候选」
    # 在响应上长得一模一样，而这两件事该做的处置相反。
    excluded_count: int = 0
    # 每个通道这一次的轨迹。1.0 ``EvidencePacket.channel_trace``——**§8 给 S6 的
    # 验收条件就是这一格**。顺序与融合的通道顺序一致（它有意义，见 ``execute``）。
    channel_trace: Sequence[ChannelTrace] = field(default_factory=tuple)
    # PhotoBench 的图记忆评测需要知道「图加入前」和「最终交付」两份候选头。
    # 这几格是内部遥测，不进入模型可见结果；助手工具会把它们写进 run trace。
    primary_asset_ids: Sequence[str] = field(default_factory=tuple)
    graph_asset_ids: Sequence[str] = field(default_factory=tuple)
    fused_asset_ids: Sequence[str] = field(default_factory=tuple)


def _visual_candidates_health(
    result: VisualSearchResult,
) -> tuple[tuple[CandidateHit, ...], str, bool, str]:
    """视觉通道的候选 + 它的健康度。**两个一起取**，理由见 ``record`` 的 docstring。"""
    return _visual_candidates(result), result.backend, result.degraded, result.degraded_reason


def _text_candidates_health(
    result: TextSearchResult,
) -> tuple[tuple[CandidateHit, ...], str, bool, str]:
    return _text_candidates(result), result.backend, result.degraded, result.degraded_reason


def _visual_candidates(result: VisualSearchResult) -> tuple[CandidateHit, ...]:
    """还原成 1.0 ``VisualAnnRetriever.retrieve`` 交给 Kernel 的那张命中表。

    ``RetrieveVisual`` 是 S2 的单通道用例，内部已经 ``fuse`` 过一次；但融合只消费
    ``rank``，而名次原样记在 ``channels["visual_ann"]``、原始分数记在 ``raw_scores`` 里，
    所以这里是无损还原——分数由编排层按名次重算，不沿用这里带回来的那个。
    """
    return tuple(
        CandidateHit(
            asset_id=hit.asset_id,
            channel=VISUAL_CHANNEL,
            rank=hit.channels[VISUAL_CHANNEL],
            raw_score=hit.raw_scores.get(VISUAL_CHANNEL, 0.0),
        )
        for hit in result.hits
        if VISUAL_CHANNEL in hit.channels
    )


def _text_candidates(result: TextSearchResult) -> tuple[CandidateHit, ...]:
    """还原成 1.0 ``TextAnnRetriever.retrieve`` 交给 Kernel 的那张命中表。

    与 ``_visual_candidates`` 同形：``RetrieveTextAnn`` 内部已经 ``fuse`` 过一次，
    但融合只消费 ``rank``，而名次记在 ``channels["text_ann"]``、原始分数记在
    ``raw_scores`` 里，所以这里是无损还原——分数由编排层按名次重算。
    """
    return tuple(
        CandidateHit(
            asset_id=hit.asset_id,
            channel=TEXT_CHANNEL,
            rank=hit.channels[TEXT_CHANNEL],
            raw_score=hit.raw_scores.get(TEXT_CHANNEL, 0.0),
        )
        for hit in result.hits
        if TEXT_CHANNEL in hit.channels
    )


class RetrieveEvidence:
    """五条 primary 通道 → 加权 RRF，再接可选的 graph 拓展通道。"""

    def __init__(
        self,
        *,
        visual: RetrieveVisual,
        lexical: RetrieveLexical,
        metadata: RetrieveMetadata,
        entity: RetrieveEntity,
        text: RetrieveTextAnn,
        conditions: EvaluateConditions | None = None,
        graph: RetrieveGraph | None = None,
    ) -> None:
        self._visual = visual
        self._lexical = lexical
        self._metadata = metadata
        self._entity = entity
        self._text = text
        self._conditions = conditions
        self._graph = graph

    def execute(self, request: EvidenceRequest) -> EvidenceResult:
        text = request.text.strip()
        if not text:
            return EvidenceResult()
        limit = max(1, request.limit)
        recall_limit = limit * RECALL_OVERSAMPLE_FACTOR
        channel_weights = {**DEFAULT_CHANNEL_WEIGHTS, GRAPH_CHANNEL: GRAPH_CHANNEL_WEIGHT}
        primary_weights = {name: weight for name, weight in channel_weights.items() if name != GRAPH_CHANNEL}

        # **每个通道都先记一条 trace，再谈命中**——1.0 ``evidence_retrieval.py:203-230``。
        # 通道抛异常时 ``channel_hits[name] = []`` 而 trace 照样留一条带 ``reason`` 的：
        # 「这一路挂了」与「这一路跑了没找到」在候选列表上长得一模一样，
        # 而那正是 S6 要证明的东西（见 ``ChannelTrace`` 的 docstring）。
        traces: list[ChannelTrace] = []

        def record(
            name: str, run: Callable[[], tuple[Sequence[CandidateHit], str, bool, str]]
        ) -> Sequence[CandidateHit]:
            """跑一个通道并记下它的轨迹。

            ``run`` **把调用本身包在里面**，返回 ``(候选, backend, 是否降级, 原因)``。
            健康度必须在**跑完之后**才拿得到，所以不能像早先那样当参数先算出来——
            那样两个向量通道的调用会落在 try 之外，它们抛异常时整次检索就挂了
            （定点变异抓到过这一版）。**一个通道挂了不该让整次检索挂掉**，
            那是 1.0 那个 ``except`` 的全部意义。
            """
            started = time.monotonic()
            try:
                hits, backend, degraded, degraded_reason = run()
            except Exception as error:  # noqa: BLE001 - 见 docstring
                log.warning("召回通道 %s 抛异常，本次按空结果继续: %s", name, error)
                traces.append(
                    ChannelTrace(
                        channel=name,
                        status="error",
                        candidate_count=0,
                        latency_ms=round((time.monotonic() - started) * 1000, 1),
                        reason=str(error),
                    )
                )
                return ()
            traces.append(
                ChannelTrace(
                    channel=name,
                    # 降级与失败是两回事：降级说明这一路**跑到了**、只是后端换了
                    # （1.0 的 ``getattr(retriever, "status", "ok")`` 是同一个自由度）。
                    status="degraded" if degraded else "ok",
                    candidate_count=len(hits),
                    latency_ms=round((time.monotonic() - started) * 1000, 1),
                    backend=backend,
                    reason=degraded_reason if degraded else "",
                )
            )
            return hits

        def plain(hits: Sequence[CandidateHit]) -> tuple[Sequence[CandidateHit], str, bool, str]:
            """三条没有健康度的通道（metadata / entity / lexical）用这个包一下。"""
            return hits, "", False, ""

        # 视觉通道的健康度**还要往上报一层**（``EvidenceResult.backend`` / ``degraded``
        # 是响应顶层的字段），而它的调用现在在 ``record`` 里面。用一个一格盒子把它带出来。
        # 通道挂了时盒子是空的 → 顶层回 ``none``/未降级，那正是 ``EvidenceResult`` 的
        # 默认值，也是实情。
        visual_box: list[VisualSearchResult] = []

        def run_visual() -> tuple[Sequence[CandidateHit], str, bool, str]:
            result = self._visual.execute(
                VisualSearchRequest(text=text, scope_id=request.scope_id, limit=recall_limit)
            )
            visual_box.append(result)
            return _visual_candidates_health(result)

        # 这个 dict 的插入顺序必须与 1.0 的构建顺序一致：metadata 最先，锚点加成才会
        # 落在同时被 metadata 与语义通道命中的候选上。
        channel_hits: dict[str, Sequence[CandidateHit]] = {
            METADATA_CHANNEL: record(
                METADATA_CHANNEL,
                lambda: plain(
                    self._metadata.execute(
                        scope_id=request.scope_id,
                        time_bounds=request.time_bounds,
                        media_types=request.media_types,
                        negated_media=request.negated_media,
                        place=request.place,
                    )
                ),
            ),
            # entity 排在 metadata 之后、lexical 之前——1.0 ``build_default_retrievers``
            # 的构建顺序就是 metadata → entity → lexical → visual_ann。anchor 加成按
            # **候选第一次出现时**的通道给，顺序换了，同时被 entity 与 lexical 命中的
            # 候选就拿不到那 +1.0。
            ENTITY_CHANNEL: record(
                ENTITY_CHANNEL,
                lambda: plain(
                    self._entity.execute(people=request.person, scope_id=request.scope_id, limit=recall_limit)
                ),
            ),
            LEXICAL_CHANNEL: record(
                LEXICAL_CHANNEL,
                lambda: plain(
                    self._lexical.execute(whole_query=text, scope_id=request.scope_id, limit=recall_limit)
                ),
            ),
            # 两个向量通道把后端的健康度一路带上来（``backend`` / ``degraded``）——
            # 那是它们唯一能说清「为什么这一路没结果」的地方。
            VISUAL_CHANNEL: record(
                VISUAL_CHANNEL,
                run_visual,
            ),
            # text_ann 排在最后——1.0 ``build_default_retrievers`` 的构建顺序是
            # metadata → entity → lexical → visual_ann → text_ann。anchor 加成按
            # **候选第一次出现时**的通道给，顺序换了拿到的加成就不一样。
            TEXT_CHANNEL: record(
                TEXT_CHANNEL,
                lambda: _text_candidates_health(
                    self._text.execute(
                        TextSearchRequest(text=text, scope_id=request.scope_id, limit=recall_limit)
                    )
                ),
            ),
        }
        # 先固定 primary 基线，再把它的头部交给图做种子。不能把所有过取候选都
        # 送进图：那会让一个低置信度的尾部命中扩大遍历面，也会令图效果基线失去
        # 「图加入前实际交付头部」的含义。
        primary_fused = fuse(channel_hits, channel_weights=primary_weights)

        # 图是由 primary 结果提供种子的 expander，不参与前面的并行召回。
        # 这样即使图尚未构建，原有五路检索仍然完整返回；图本身的降级会单独出现在
        # channel_trace，而不是伪装成「没有相关结果」。
        if self._graph is not None:
            graph_started = time.monotonic()
            try:
                graph_result = self._graph.execute(
                    seed_asset_ids=[candidate.asset_id for candidate in primary_fused[:limit]],
                    scope_id=request.scope_id,
                    limit=max(recall_limit, limit * 2),
                    question=text,
                )
                channel_hits[GRAPH_CHANNEL] = graph_result.hits
                traces.append(
                    ChannelTrace(
                        channel=GRAPH_CHANNEL,
                        status="degraded" if graph_result.degraded else "ok",
                        candidate_count=len(graph_result.hits),
                        latency_ms=round((time.monotonic() - graph_started) * 1000, 1),
                        backend=graph_result.backend,
                        reason=graph_result.reason if graph_result.degraded else "",
                    )
                )
            except Exception as error:  # noqa: BLE001 - 图是可降级的派生通道
                log.warning("图记忆拓展抛异常，本次按空结果继续: %s", error)
                channel_hits[GRAPH_CHANNEL] = ()
                traces.append(
                    ChannelTrace(
                        channel=GRAPH_CHANNEL,
                        status="error",
                        candidate_count=0,
                        latency_ms=round((time.monotonic() - graph_started) * 1000, 1),
                        reason=str(error),
                    )
                )

        fused = fuse(channel_hits, channel_weights=channel_weights)

        # **条件判定排在融合之后、交付之前。** 1.0 ``evidence_retrieval.py:325``
        # 的 ``_evaluate_fused`` 就是这个位置。放在前面（比如放在通道里）会变成
        # 「每条通道各自过滤」，而条件判定要的是「对整个候选集一次」，
        # 否则被 A 通道排除的候选还会从 B 通道进来。
        excluded_count = 0
        if self._conditions is not None and request.conditions:
            outcome = self._conditions.filter(
                [candidate.asset_id for candidate in fused],
                request.conditions,
                scope_id=request.scope_id,
            )
            kept = set(outcome.kept)
            fused = [candidate for candidate in fused if candidate.asset_id in kept]
            primary_fused = [candidate for candidate in primary_fused if candidate.asset_id in kept]
            excluded_count = outcome.excluded_count

        # graph 是 expander，不应像第六条独立 primary 通道一样凭 RRF 分数把结果
        # 全部压在尾部（primary 的 RRF 分数天然大一个数量级，旧实现因此在正常
        # limit 下几乎永远看不到图扩展）。保留 primary 的排序作为基线，并为图扩展
        # 预留一个小而确定的尾部窗口；窗口内按 graph 的 0.5 应用层权重与路径排名
        # 选择，未命中时再由 primary 补位。这样既不改冻结 DEFAULT_CHANNEL_WEIGHTS，
        # 也不会让图候选反向改变 primary 排名。
        if channel_hits.get(GRAPH_CHANNEL):
            primary_ids = {candidate.asset_id for candidate in primary_fused}
            graph_candidates = [
                candidate
                for candidate in fused
                if GRAPH_CHANNEL in candidate.channels and candidate.asset_id not in primary_ids
            ]
            graph_candidates.sort(
                key=lambda candidate: (
                    candidate.channels.get(GRAPH_CHANNEL, 10**9),
                    -candidate.score,
                    candidate.asset_id,
                )
            )
            graph_slots = min(len(graph_candidates), max(1, min(4, limit // 4 or 1)))
            selected: list[FusedCandidate] = [
                *primary_fused[: max(0, limit - graph_slots)],
                *graph_candidates[:graph_slots],
            ]
            selected_ids = {candidate.asset_id for candidate in selected}
            if len(selected) < limit:
                selected.extend(
                    candidate for candidate in primary_fused if candidate.asset_id not in selected_ids
                )
                selected = selected[:limit]
            fused = selected
        else:
            fused = primary_fused[:limit]

        graph_asset_ids = tuple(candidate.asset_id for candidate in channel_hits.get(GRAPH_CHANNEL, ()))
        primary_asset_ids = tuple(candidate.asset_id for candidate in primary_fused[:limit])
        fused_asset_ids = tuple(candidate.asset_id for candidate in fused[:limit])

        return EvidenceResult(
            hits=tuple(
                EvidenceHit(
                    asset_id=candidate.asset_id,
                    score=candidate.score,
                    channels=dict(candidate.channels),
                    raw_scores=dict(candidate.raw_scores),
                    evidence_class=candidate.evidence_class,
                )
                for candidate in fused[:limit]
            ),
            backend=visual_box[0].backend if visual_box else "none",
            degraded=visual_box[0].degraded if visual_box else False,
            degraded_reason=visual_box[0].degraded_reason if visual_box else "",
            authoritative_vectors=visual_box[0].authoritative_vectors if visual_box else 0,
            excluded_count=excluded_count,
            channel_trace=tuple(traces),
            primary_asset_ids=primary_asset_ids,
            graph_asset_ids=graph_asset_ids,
            fused_asset_ids=fused_asset_ids,
        )
