from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from sentrix.api.schemas.media import AssetSummary


class SearchHit(BaseModel):
    """一条命中。``asset`` 是可打开的原图本身——产品硬约束：查询必须返回原图。"""

    asset: AssetSummary
    score: float
    # 通道 -> 该通道里的名次。S2 只有 visual_ann 一条，结构先按六路留好。
    channels: dict[str, int]
    raw_scores: dict[str, float]


class ChannelTrace(BaseModel):
    """一个召回通道这一次的轨迹。

    **这一块是 S6 的验收条件。** 它要回答的问题只有两个：这一路**跑了没有**、
    以及没跑或降级时**为什么**。没有它，「通道跑了没找到」与「通道根本没跑」
    在响应上完全一样——而 S6 要证明的正是六路（2.0 是五路，``adjacency`` 按裁决不实现）
    真的都在跑。理由与判据写在 ``domain/retrieval/models.py`` 的 ``ChannelTrace`` 上。
    """

    channel: str
    # ok / degraded / error。**非 ok 时 reason 一定非空**。
    status: str
    candidate_count: int
    latency_ms: float
    backend: str = ""
    reason: str = ""
    operational_issue: dict[str, Any] | None = None


class SearchResponse(BaseModel):
    query: str
    # 实际服务的后端：qdrant 或 sqlite_exact。
    backend: str
    degraded: bool
    degraded_reason: str
    # 权威存储里的向量条数。用来区分「还没导入过」和「搜不到」。
    authoritative_vectors: int
    # 被结构化条件排除掉的候选数。**必须给出来**：没有它，「条件把所有候选都排除了」
    # 与「本来就没有候选」在响应上长得一模一样，而这两件事该做的处置相反
    # （前者是用户的条件太窄，后者是库里没有）。
    excluded_count: int = 0
    # 每个通道的轨迹，顺序与融合的通道顺序一致。
    channel_trace: list[ChannelTrace] = []
    items: list[SearchHit]
