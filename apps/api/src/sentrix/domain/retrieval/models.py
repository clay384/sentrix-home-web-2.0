"""检索领域模型：候选命中与融合结果。纯数据与规则，零 IO。

分值不是裸 float。1.0 的 ``retrieval/base.py`` 把这件事写成了契约（P0-19）：
``CandidateHit`` 必须同时带 ``raw_score`` / ``score_kind`` / ``higher_is_better`` /
``rank``，因为跨通道的原始分数不可比——余弦相似度 0.3 与词法命中次数 1 放一起比
大小没有任何意义。融合只消费 ``rank``，原始分数只用于展示与探针校准。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class CandidateHit:
    """一条通道召回的一条候选。``rank`` 从 1 开始，与 RRF 公式一致。"""

    asset_id: str
    channel: str
    rank: int
    raw_score: float
    score_kind: str = "cosine_similarity"
    higher_is_better: bool = True

    def __post_init__(self) -> None:
        if self.rank < 1:
            raise ValueError(f"rank 从 1 开始，收到 {self.rank}")


@dataclass(frozen=True, slots=True)
class FusedCandidate:
    """融合后的候选。``channels`` 记录它被哪些通道以第几名召回。"""

    asset_id: str
    score: float
    channels: Mapping[str, int] = field(default_factory=dict)
    raw_scores: Mapping[str, float] = field(default_factory=dict)
    evidence_class: str = "semantic"


@dataclass(frozen=True, slots=True)
class ChannelTrace:
    """一个召回通道这一次的轨迹。**S6 的验收条件就是它。**（1.0 ``evidence_retrieval.py:220-230``）

    §8 给 S6 的验收条件是一句「证据包和 trace 能展开」，而它指的东西是具体的：
    ``EvidencePacket.channel_trace``。1.0 那条注释写得比字段名清楚——
    *per-channel recall trace so API/benchmark can prove each retriever was invoked
    (or give an explicit unavailable reason)*。

    **为什么「每候选的 ``channels``」不够。** 那是转置视角，它分不开这两件事：

    - 通道跑了、一张都没召回 → 没有候选带这个键；
    - 通道**根本没跑**（模型不可用、向量库空）→ **也没有**任何候选带这个键。

    两者在读数上完全一样，而 S6「六路取证补齐」要证明的恰恰是「这一路真的在跑」。

    ``status`` 三个取值，**非 ``ok`` 时 ``reason`` 必须非空**（用例钉住这一条）：

    - ``ok``：跑了，拿到了（可能 0 条）；
    - ``degraded``：跑了，但后端降级（2.0 的两个向量通道会这样报，见
      ``TextSearchResult.degraded`` / ``VisualSearchResult.degraded``）；
    - ``error``：抛了异常。1.0 也是这么收的——异常时 ``channel_hits[name] = []``
      而 **trace 照样留一条带 ``reason`` 的**。

    ``latency_ms`` 是 1.0 的 ``retrieval_timing``（同一个循环里落的，这里合成一格）。
    ``embedding_ms`` / ``embedding_events`` **不搬**：那是 1.0 ``embedding_router`` 的
    内部计时事件，2.0 的嵌入走 ``VisualEmbedder`` / ``TextEmbedder`` 端口，没有那套
    路由器，搬过来只会是一个恒空字段。
    """

    channel: str
    status: str
    candidate_count: int
    latency_ms: float
    # 两个向量通道会报实际服务的后端；另外三条没有这一格（1.0 同样是
    # ``getattr(retriever, "backend_used", None)``，取不到就是 None）。
    backend: str = ""
    reason: str = ""
    operational_issue: Mapping[str, Any] | None = None
