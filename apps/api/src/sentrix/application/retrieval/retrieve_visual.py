"""视觉召回：内容词 → 视觉通道候选 → 融合排名。

这是 S2 的用户可感知部分：在 Web 上打一个内容词，拿回正确的照片。

分层职责与 1.0 ``backend/retrieval/visual_ann.py`` 相同，但有一处**刻意的行为
差异**：1.0 先问 Qdrant，为空就继续走静态 HNSW，两条路径都不比对「索引里到底有多少
条」。生产上 ``data/ann/visual.hnsw`` 只有 1,170 条而真实 10,590 条，只因为 Qdrant
优先才没暴露——索引落后于权威这件事本身从不可见。

2.0 的规则是**先比对计数，再决定信不信索引**：

- 索引不可用（目录被占用等）→ 回退权威存储，``degraded=True`` + 原因；
- 索引可用但条数少于权威 → 同上，原因是 ``index_stale`` / ``index_empty``；
- 计数对得上 → 用索引，``backend="qdrant"``。

无论走哪条路，结果都必须正确；区别只在快慢，以及 ``degraded`` 字段有没有置起来。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sentrix.application.retrieval.scheme_drift import scheme_drift_reason
from sentrix.domain.retrieval import scheme
from sentrix.domain.retrieval.fusion import fuse
from sentrix.domain.retrieval.models import CandidateHit
from sentrix.domain.retrieval.ports import (
    EmbeddingDiagnostics,
    VectorHit,
    VectorIndex,
    VectorIndexStatus,
    VectorIndexUnavailable,
    VectorRepository,
    VisualEmbedder,
    retrieval_operational_issue,
)

log = logging.getLogger("sentrix.retrieval.visual")

CHANNEL = "visual_ann"
# 索引是近似的、还带 scope 过滤：多取一些再让融合层裁，比在索引层精确裁更安全。
OVERSAMPLE_FACTOR = 4


@dataclass(frozen=True, slots=True)
class VisualSearchRequest:
    text: str
    # None = 跨全部相册，理由同 `EvidenceRequest.scope_id`。
    scope_id: str | None = None
    limit: int = 20


@dataclass(frozen=True, slots=True)
class VisualSearchHit:
    asset_id: str
    score: float
    channels: Mapping[str, int] = field(default_factory=dict)
    raw_scores: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VisualSearchResult:
    hits: Sequence[VisualSearchHit] = field(default_factory=tuple)
    backend: str = "none"
    degraded: bool = False
    degraded_reason: str = ""
    # 权威存储里这个 space 有多少条向量。空结果时用来区分「没导入过」与「搜不到」。
    authoritative_vectors: int = 0
    operational_issue: Mapping[str, Any] | None = None


class RetrieveVisual:
    def __init__(
        self,
        *,
        embedder: VisualEmbedder,
        vectors: VectorRepository,
        index: VectorIndex | None = None,
    ) -> None:
        self._embedder = embedder
        self._vectors = vectors
        self._index = index

    def execute(self, request: VisualSearchRequest) -> VisualSearchResult:
        text = request.text.strip()
        if not text:
            return VisualSearchResult(hits=(), backend="none")

        query = self._embedder.embed_query(text)
        if not query:
            return VisualSearchResult(
                hits=(),
                backend="none",
                degraded=True,
                degraded_reason="embedder_unavailable",
                operational_issue=(
                    self._embedder.last_operational_issue
                    if isinstance(self._embedder, EmbeddingDiagnostics)
                    else None
                ),
            )
        if len(query) != self._embedder.dimension:
            return VisualSearchResult(
                hits=(),
                backend="none",
                degraded=True,
                degraded_reason=(f"query_dimension_mismatch:{len(query)}!={self._embedder.dimension}"),
            )

        limit = max(1, request.limit)
        candidates, backend, degraded, reason, operational_issue = self._recall(request, query, limit)
        authoritative = self._vectors.count(space=scheme.VISUAL_SPACE, model_name=self._embedder.model_id)
        # **方案漂移盖过索引健康度。** 索引再健康，只要库里没有一条当前方案的向量，
        # 召回出来的东西就是错的——所以这里不看 ``_recall`` 有没有已经置过 degraded，
        # 直接覆盖。详见 ``scheme_drift`` 模块注释里那次实测。
        drift = scheme_drift_reason(
            self._vectors, space=scheme.VISUAL_SPACE, model_name=self._embedder.model_id
        )
        if drift:
            degraded, reason = True, drift

        hits = [
            CandidateHit(
                asset_id=hit.source_id,
                channel=CHANNEL,
                rank=rank,
                raw_score=hit.score,
            )
            for rank, hit in enumerate(candidates, start=1)
        ]
        fused = fuse({CHANNEL: hits}) if hits else []

        return VisualSearchResult(
            hits=tuple(
                VisualSearchHit(
                    asset_id=candidate.asset_id,
                    score=candidate.score,
                    channels=dict(candidate.channels),
                    raw_scores=dict(candidate.raw_scores),
                )
                for candidate in fused[:limit]
            ),
            backend=backend,
            degraded=degraded,
            degraded_reason=reason,
            authoritative_vectors=authoritative,
            operational_issue=operational_issue,
        )

    # ---- 召回 ------------------------------------------------------------

    def _recall(
        self, request: VisualSearchRequest, query: Sequence[float], limit: int
    ) -> tuple[Sequence[VectorHit], str, bool, str, Mapping[str, Any] | None]:
        """返回 ``(候选, 实际后端, 是否降级, 原因, 故障证据)``。"""
        fetch = max(limit * OVERSAMPLE_FACTOR, limit)
        status = self._index_status()
        issue = status.operational_issue if not status.available else None
        reason = "" if status.available else (str(issue["failure_code"]) if issue else status.reason)

        if status.available and self._index is not None:
            reason, issue = self._staleness_reason()
            if not reason:
                try:
                    hits = self._index.search(
                        space=scheme.VISUAL_SPACE,
                        vector=query,
                        limit=fetch,
                        scope_id=request.scope_id,
                        model_name=self._embedder.model_id,
                    )
                except VectorIndexUnavailable as error:
                    # status()/count() 与 search() 之间索引掉了（例如另一个进程
                    # 刚好在这时拿走目录锁）。这一格必须回退，不能把异常抛给用户。
                    issue = retrieval_operational_issue(error)
                    assert issue is not None
                    reason = str(issue["failure_code"])
                    log.warning("派生索引在检索中途不可用，回退权威存储: %s", reason)
                else:
                    return hits, "qdrant", False, "", None

        hits = self._vectors.search_exact(
            space=scheme.VISUAL_SPACE,
            vector=query,
            limit=fetch,
            scope_id=request.scope_id,
            model_name=self._embedder.model_id,
        )
        return hits, "sqlite_exact", True, reason or "index_unavailable", issue

    def _index_status(self) -> VectorIndexStatus:
        if self._index is None:
            return VectorIndexStatus(
                backend="none", available=False, degraded=True, reason="no_index_configured"
            )
        return self._index.status()

    def _staleness_reason(self) -> tuple[str, Mapping[str, Any] | None]:
        """索引条数与权威条数对不上时给出原因；真正资源故障单列证据。"""
        assert self._index is not None
        try:
            indexed = self._index.count(space=scheme.VISUAL_SPACE, model_name=self._embedder.model_id)
        except VectorIndexUnavailable as error:
            issue = retrieval_operational_issue(error)
            assert issue is not None
            return str(issue["failure_code"]), issue
        authoritative = self._vectors.count(space=scheme.VISUAL_SPACE, model_name=self._embedder.model_id)
        if indexed == 0 and authoritative > 0:
            return f"index_empty:index=0,authoritative={authoritative}", None
        if indexed < authoritative:
            return f"index_stale:index={indexed},authoritative={authoritative}", None
        return "", None
