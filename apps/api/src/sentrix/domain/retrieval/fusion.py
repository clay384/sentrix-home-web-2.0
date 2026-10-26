"""候选融合：加权 RRF + 证据分级。纯函数，零 IO。

对应 1.0 ``backend/retrieval/fusion.py``。数值全部取自
``docs/refactor/behaviour-baseline.md`` 的冻结项：

- ``RRF_K = 60``（Cormack 2009 的常数）
- anchor 通道 +1.0
- 冻结通道权重 visual 2.5 / lexical 1.0 / text 0.5 / metadata 1.0 / entity 1.0 /
  adjacency 0.5；graph 的 0.5 是新能力的应用层覆盖，不改冻结基线常量。

权重不是拍脑袋：R7 消融里 chinese-clip 视觉 r10=0.887、词法 0.373、文本 0.158，
平权 RRF 会让弱通道把强通道稀释掉，混合反而低于纯视觉。改任何一个数字前先改基线。

通道分三类：
- **anchor**（metadata / entity）：确定性锚点，融合后加固定加成；
- **semantic**（lexical / visual_ann / text_ann）：只按 RRF 名次融合；
- **expander**（adjacency / graph）：继承种子，不独立提升；graph 是新的可选派生通道，
  adjacency 仍按冻结契约保留但不重新启用。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from sentrix.domain.retrieval.models import CandidateHit, FusedCandidate

RRF_K = 60
ANCHOR_BOOST = 1.0

ANCHOR_CHANNELS = ("metadata", "entity")
EXPANDER_CHANNELS = ("adjacency", "graph")

DEFAULT_CHANNEL_WEIGHTS: Mapping[str, float] = {
    "visual_ann": 2.5,
    "lexical": 1.0,
    "text_ann": 0.5,
    "metadata": 1.0,
    "entity": 1.0,
    "adjacency": 0.5,
}


def weighted_rrf_score(ranks: Mapping[str, int], weights: Mapping[str, float], k: int = RRF_K) -> float:
    """``sum(weight[channel] / (k + rank))``；未登记的通道按 1.0 计权。"""
    return sum(weights.get(channel, 1.0) / (k + rank) for channel, rank in ranks.items())


def evidence_class_for(channel: str) -> str:
    if channel in ANCHOR_CHANNELS:
        return "anchor"
    if channel in EXPANDER_CHANNELS:
        return "expander"
    return "semantic"


def fuse(
    channel_hits: Mapping[str, Sequence[CandidateHit]],
    *,
    k: int = RRF_K,
    anchor_boost: float = ANCHOR_BOOST,
    channel_weights: Mapping[str, float] | None = None,
) -> list[FusedCandidate]:
    """把各通道的排名结果合并成一份融合排名。

    与 1.0 的两处行为差异：

    1. 名次取自 ``hit.rank``，不取列表下标。1.0 用 ``enumerate``，命中列表一旦
       不是按名次排好的，算出来的分数与命中自报的名次就会对不上，而且不报错。
    2. 同分时按 ``asset_id`` 升序稳定排序。1.0 用 ``sort(reverse=True)``，同分
       名次取决于调用方传通道的先后，同一份数据换个调用顺序就换一套结果。
    """
    weights = DEFAULT_CHANNEL_WEIGHTS if channel_weights is None else channel_weights
    candidates: dict[str, FusedCandidate] = {}

    for channel, hits in channel_hits.items():
        for hit in hits:
            candidate = candidates.get(hit.asset_id)
            if candidate is None:
                candidate = FusedCandidate(
                    asset_id=hit.asset_id,
                    score=0.0,
                    channels={},
                    raw_scores={},
                    evidence_class=evidence_class_for(channel),
                )
                candidates[hit.asset_id] = candidate
            # dataclass 是 frozen 的，累加通过重建完成——避免把可变状态漏给调用方。
            channels = {**candidate.channels, channel: hit.rank}
            raw_scores = {**candidate.raw_scores, channel: hit.raw_score}
            score = weighted_rrf_score(channels, weights, k=k)
            if candidate.evidence_class == "anchor":
                score += anchor_boost
            candidates[hit.asset_id] = FusedCandidate(
                asset_id=hit.asset_id,
                score=score,
                channels=channels,
                raw_scores=raw_scores,
                evidence_class=candidate.evidence_class,
            )

    return sorted(candidates.values(), key=lambda item: (-item.score, item.asset_id))
