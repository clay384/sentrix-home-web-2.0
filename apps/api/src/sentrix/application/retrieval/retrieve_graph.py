"""图结构记忆的有界拓展检索。"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from sentrix.domain.memories.ports import MemoryGraphRepository
from sentrix.domain.retrieval.models import CandidateHit

GRAPH_CHANNEL = "graph"
GRAPH_MAX_HOPS = 4


def graph_intent(question: str) -> str:
    """按 hpq 的 object/action/temporal/scene/multi-hop 自适应路径选择。"""
    value = question.casefold()
    if re.search(r"\d+\s*(秒|分钟|小时|sec|seconds|minutes|hours)", value) or any(
        token in value
        for token in ("什么时候", "何时", "几点", "多久", "时间线", "when", "what time", "how long")
    ):
        return "temporal"
    if any(token in value for token in ("为什么", "因果", "导致", "原因", "because", "cause", "why")):
        return "causal"
    if any(
        token in value
        for token in (
            "如何",
            "怎么",
            "然后",
            "之后",
            "之前",
            "先后",
            "过程",
            "跨视频",
            "多段",
            "before",
            "after",
            "sequence",
            "steps",
            "across videos",
            "both videos",
        )
    ):
        return "multi_hop"
    if any(
        token in value for token in ("做什么", "动作", "拿起", "打开", "放下", "action", "doing", "holding")
    ):
        return "action"
    if any(token in value for token in ("场景", "环境", "画面", "scene", "setting")):
        return "scene"
    if any(token in value for token in ("什么物体", "哪个物体", "东西", "物品", "object", "item")):
        return "object"
    return "general"


def graph_direction(question: str) -> str:
    """时序/因果边的问句方向；默认正向，不把「B 之后」误答成「B 之前」。"""
    value = question.casefold()
    if any(token in value for token in ("之前", "以前", "先前", "before", "earlier", "previous")):
        return "backward"
    if any(token in value for token in ("之后", "然后", "接着", "后来", "after", "then", "next")):
        return "forward"
    if any(token in value for token in ("为什么", "原因", "what caused", "why")):
        return "backward"
    return ""


@dataclass(frozen=True, slots=True)
class GraphSearchResult:
    hits: Sequence[CandidateHit] = ()
    backend: str = "sqlite_graph"
    degraded: bool = False
    reason: str = ""


class RetrieveGraph:
    """从已有 primary 召回结果出发，遍历图并补充未命中的 asset。"""

    def __init__(self, *, graph: MemoryGraphRepository) -> None:
        self._graph = graph

    def execute(
        self,
        *,
        seed_asset_ids: Sequence[str],
        scope_id: str | None,
        limit: int,
        question: str = "",
    ) -> GraphSearchResult:
        # 跨相册检索没有一个可安全混用的 scope 图。此时保留原有五路结果，
        # 同时把限制写入 trace，避免把「图没有跑」伪装成「图跑了但没召回」。
        if not scope_id:
            return GraphSearchResult(degraded=True, reason="graph_requires_scope")
        status = self._graph.status(scope_id)
        if not status.available:
            return GraphSearchResult(degraded=True, reason=status.reason or "graph_not_built")
        expanded = self._graph.expand_assets(
            seed_asset_ids,
            scope_id=scope_id,
            max_hops=GRAPH_MAX_HOPS,
            limit=max(1, limit),
            intent=graph_intent(question),
            direction=graph_direction(question),
        )
        return GraphSearchResult(
            hits=tuple(
                CandidateHit(
                    asset_id=item.asset_id,
                    channel=GRAPH_CHANNEL,
                    rank=rank,
                    raw_score=item.score,
                    score_kind="graph_path",
                    higher_is_better=True,
                )
                for rank, item in enumerate(expanded, 1)
            )
        )
