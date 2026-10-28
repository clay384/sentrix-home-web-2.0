"""视觉召回用例的单元测试：假嵌入器 + 假索引 + 假权威存储。

重点是**降级矩阵**：索引被锁、索引为空、索引落后、检索中途掉线，四种情况都必须
①仍然给出正确结果 ②把 degraded 置起来并带上原因。1.0 在这四种情况里全部静默。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from sentrix.application.retrieval.retrieve_visual import (
    CHANNEL,
    RetrieveVisual,
    VisualSearchRequest,
)
from sentrix.domain.retrieval import scheme
from sentrix.domain.retrieval.ports import (
    VectorHit,
    VectorIndexStatus,
    VectorIndexUnavailable,
)

MODEL = scheme.IMAGE_MODEL


class FakeEmbedder:
    model_id = MODEL
    dimension = 4

    def __init__(self, vector: list[float] | None = None) -> None:
        self.vector = [1.0, 0.0, 0.0, 0.0] if vector is None else vector
        self.queries: list[str] = []

    def embed_image(self, path: Path) -> list[float]:
        return list(self.vector)

    def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return list(self.vector)


class FakeVectors:
    def __init__(
        self,
        hits: list[VectorHit] | None = None,
        total: int | None = None,
        scheme_total: int | None = None,
    ) -> None:
        self.hits = hits or []
        self.total = len(self.hits) if total is None else total
        # **带当前 model 过滤**数出来的条数。默认与 ``total`` 相同——既有用例里
        # 「这个 space 有 7 条」与「有 7 条属于当前方案」本来就是同一件事。
        # 只有漂移用例需要让它为 0 而 ``total`` 不为 0。
        self.scheme_total = self.total if scheme_total is None else scheme_total
        self.searches: list[dict] = []

    def upsert(self, record):
        raise AssertionError("召回用例不该写向量")

    def has_vector(self, **kwargs) -> bool:
        return False

    def count(self, *, space: str, model_name: str | None = None) -> int:
        return self.total if model_name is None else self.scheme_total

    def search_exact(self, *, space, vector, limit, scope_id=None, model_name=None):
        self.searches.append({"space": space, "limit": limit, "scope_id": scope_id, "model_name": model_name})
        return self.hits[:limit]


class FakeIndex:
    def __init__(
        self,
        *,
        hits: list[VectorHit] | None = None,
        available: bool = True,
        count: int | None = None,
        reason: str = "RuntimeError: Storage folder is already accessed",
        fail_on_search: bool = False,
    ) -> None:
        self.hits = hits or []
        self.available = available
        self.count_value = len(self.hits) if count is None else count
        self.reason = reason
        self.fail_on_search = fail_on_search
        self.searches: list[dict] = []

    def upsert(self, records):
        raise AssertionError("召回用例不该写索引")

    def status(self) -> VectorIndexStatus:
        return VectorIndexStatus(
            backend="qdrant",
            available=self.available,
            degraded=not self.available,
            reason="" if self.available else self.reason,
        )

    def count(self, *, space, model_name=None) -> int:
        if not self.available:
            raise VectorIndexUnavailable(self.reason)
        return self.count_value

    def search(self, *, space, vector, limit, scope_id=None, model_name=None):
        if self.fail_on_search:
            raise VectorIndexUnavailable(self.reason)
        self.searches.append({"space": space, "limit": limit, "scope_id": scope_id, "model_name": model_name})
        return self.hits[:limit]


def hit(source_id: str, score: float) -> VectorHit:
    return VectorHit(
        source_id=source_id,
        scope_id="album1",
        source_type="asset",
        model_name=MODEL,
        score=score,
    )


def build(*, embedder=None, vectors=None, index=None) -> RetrieveVisual:
    return RetrieveVisual(
        embedder=embedder or FakeEmbedder(),
        vectors=vectors or FakeVectors(),
        index=index,
    )


def request(**kwargs) -> VisualSearchRequest:
    params = {"text": "桥", "scope_id": "album1", "limit": 5}
    params.update(kwargs)
    return VisualSearchRequest(**params)  # type: ignore[arg-type]


def test_healthy_index_serves_the_query():
    index = FakeIndex(hits=[hit("asset_a", 0.31), hit("asset_b", 0.28)], count=2)
    vectors = FakeVectors(total=2)

    result = build(vectors=vectors, index=index).execute(request())

    assert result.backend == "qdrant"
    assert result.degraded is False
    assert result.degraded_reason == ""
    assert [item.asset_id for item in result.hits] == ["asset_a", "asset_b"]
    assert result.authoritative_vectors == 2
    assert vectors.searches == [], "索引健康时不该回退到精确检索"


def test_fused_score_uses_the_visual_channel_weight():
    """融合分数必须是加权 RRF，不是原始余弦。

    visual 权重 2.5、RRF_K 60：第一名 = 2.5/61。若哪天有人把权重改回平权，
    这里会红。
    """
    index = FakeIndex(hits=[hit("asset_a", 0.31)], count=1)

    result = build(vectors=FakeVectors(total=1), index=index).execute(request())

    assert result.hits[0].score == pytest.approx(2.5 / 61)
    assert result.hits[0].raw_scores == {CHANNEL: 0.31}
    assert result.hits[0].channels == {CHANNEL: 1}


def test_hits_are_ranked_by_cosine_order_from_the_index():
    index = FakeIndex(hits=[hit("asset_1st", 0.9), hit("asset_2nd", 0.5)], count=2)

    result = build(vectors=FakeVectors(total=2), index=index).execute(request())

    assert [item.asset_id for item in result.hits] == ["asset_1st", "asset_2nd"]
    assert result.hits[0].score > result.hits[1].score


def test_limit_is_applied_and_oversampled_upstream():
    index = FakeIndex(hits=[hit(f"asset_{i}", 1.0 - i / 10) for i in range(10)], count=10)

    result = build(vectors=FakeVectors(total=10), index=index).execute(request(limit=3))

    assert len(result.hits) == 3
    assert index.searches[0]["limit"] == 12, "索引侧应当超取 limit*4 再融合"


def test_locked_index_falls_back_to_exact_search_and_says_so():
    """S2 的硬约束：Qdrant 目录被占用时必须回退，而且降级要可见。"""
    vectors = FakeVectors(hits=[hit("asset_a", 0.31)], total=1)
    index = FakeIndex(available=False)

    result = build(vectors=vectors, index=index).execute(request())

    assert [item.asset_id for item in result.hits] == ["asset_a"]
    assert result.backend == "sqlite_exact"
    assert result.degraded is True
    assert "already accessed" in result.degraded_reason
    assert vectors.searches[0]["model_name"] == MODEL
    assert vectors.searches[0]["scope_id"] == "album1"


def test_empty_index_while_the_authoritative_store_has_vectors_is_degraded():
    """索引被删过 / 从没建过：不能因为索引里没有就返回空。"""
    vectors = FakeVectors(hits=[hit("asset_a", 0.31)], total=7)
    index = FakeIndex(hits=[], count=0)

    result = build(vectors=vectors, index=index).execute(request())

    assert [item.asset_id for item in result.hits] == ["asset_a"]
    assert result.degraded is True
    assert result.degraded_reason == "index_empty:index=0,authoritative=7"


def test_index_behind_the_authoritative_store_is_degraded():
    """1.0 的 ``data/ann/visual.hnsw`` 只有 1,170 条而真实 10,590 条，就是这么被
    发现的——**没有人**比对过这两个数。这里把它变成一条规则。"""
    vectors = FakeVectors(hits=[hit("asset_a", 0.31)], total=10)
    index = FakeIndex(hits=[hit("asset_a", 0.31)], count=4)

    result = build(vectors=vectors, index=index).execute(request())

    assert result.backend == "sqlite_exact"
    assert result.degraded_reason == "index_stale:index=4,authoritative=10"
    assert index.searches == [], "已经判定落后就不该再去查它"


def test_index_dropping_between_status_and_search_still_returns_results():
    vectors = FakeVectors(hits=[hit("asset_a", 0.31)], total=1)
    index = FakeIndex(hits=[hit("asset_a", 0.31)], count=1, fail_on_search=True)

    result = build(vectors=vectors, index=index).execute(request())

    assert [item.asset_id for item in result.hits] == ["asset_a"]
    assert result.backend == "sqlite_exact"
    assert result.degraded is True
    assert result.degraded_reason == "vector_index_unavailable"
    assert result.operational_issue["error_code"] == "resource_error"


def test_no_index_configured_uses_the_authoritative_store():
    vectors = FakeVectors(hits=[hit("asset_a", 0.31)], total=1)

    result = build(vectors=vectors, index=None).execute(request())

    assert result.backend == "sqlite_exact"
    assert result.degraded is True
    assert result.degraded_reason == "no_index_configured"


def test_blank_query_short_circuits_without_embedding():
    embedder = FakeEmbedder()
    index = FakeIndex(count=0)

    result = build(embedder=embedder, vectors=FakeVectors(total=0), index=index).execute(request(text="   "))

    assert result.hits == ()
    assert result.backend == "none"
    assert result.degraded is False
    assert embedder.queries == []


def test_unavailable_embedder_is_reported_not_silently_empty():
    index = FakeIndex(count=0)

    result = build(embedder=FakeEmbedder(vector=[]), vectors=FakeVectors(total=3), index=index).execute(
        request()
    )

    assert result.hits == ()
    assert result.degraded is True
    assert result.degraded_reason == "embedder_unavailable"
    assert result.authoritative_vectors == 0


def test_query_dimension_mismatch_is_reported():
    result = build(embedder=FakeEmbedder(vector=[1.0, 0.0]), vectors=FakeVectors(total=1)).execute(request())

    assert result.degraded_reason == "query_dimension_mismatch:2!=4"


def test_no_matches_is_not_a_degradation():
    """索引健康、就是没搜到——这不是降级，不能报成故障。"""
    index = FakeIndex(hits=[], count=3)

    result = build(vectors=FakeVectors(total=3), index=index).execute(request())

    assert result.hits == ()
    assert result.backend == "qdrant"
    assert result.degraded is False


def test_exact_fallback_also_filters_by_model():
    """回退路径必须带模型名，否则会把别的模型的向量算进来。"""
    vectors = FakeVectors(hits=[hit("asset_a", 0.31)], total=1)

    build(vectors=vectors, index=None).execute(request())

    assert vectors.searches[0]["model_name"] == MODEL
    assert vectors.searches[0]["space"] == scheme.VISUAL_SPACE


def test_hits_are_capped_at_limit_even_when_the_fallback_oversamples():
    vectors = FakeVectors(hits=[hit(f"asset_{i}", 1.0 - i / 10) for i in range(10)], total=10)

    result = build(vectors=vectors, index=None).execute(request(limit=2))

    assert len(result.hits) == 2


def test_request_is_immutable():
    base = request()

    assert replace(base, limit=1).limit == 1


def test_vectors_from_another_scheme_are_reported_as_scheme_drift():
    """**库里有向量，却没有一条属于当前方案** = 换了方案没重建。

    2026-10-03 实测：这种状态下这条通道原本报 `status=ok / candidate_count=0`，
    响应里 `degraded` 是 `False`、`reason` 是空串，检索静默少给结果。

    定点变异：去掉 `execute` 里那段 `scheme_drift_reason`，这条红。
    """
    vectors = FakeVectors(hits=[hit("asset_a", 0.31)], total=53, scheme_total=0)

    result = build(vectors=vectors).execute(request())

    assert result.degraded is True
    assert "scheme_out_of_sync" in result.degraded_reason
    assert "53" in result.degraded_reason


def test_an_empty_store_is_not_scheme_drift():
    """**空 space 不是漂移。** 两个数都是 0 时必须安静——否则一个刚建、还没导入过东西的
    scope 每次检索都挂一条假警报，真出了漂移反而没人看。

    定点变异：把判据写成只看「当前方案的条数是 0」，这条红。
    """
    vectors = FakeVectors(hits=[hit("asset_a", 0.31)], total=0, scheme_total=0)

    result = build(vectors=vectors).execute(request())

    # **断言的判据是「报的不是漂移」，不是「degraded 为假」。** 这个夹具没配索引，
    # `_recall` 会先置 `no_index_configured`——拿 degraded 做断言等于把别的原因
    # 也算进来，那样这条用例就测不到漂移判据本身了。
    assert "scheme_out_of_sync" not in result.degraded_reason


@pytest.mark.parametrize("phase", ["status", "count"])
def test_index_resource_fault_survives_exact_fallback(phase):
    from sentrix.domain.retrieval.ports import retrieval_operational_issue

    class ResourceIndex(FakeIndex):
        def status(self):
            if phase == "status":
                return VectorIndexStatus(
                    backend="qdrant",
                    available=False,
                    degraded=True,
                    reason="private directory",
                    operational_issue=retrieval_operational_issue(
                        VectorIndexUnavailable("private directory")
                    ),
                )
            return super().status()

        def count(self, **_kwargs):
            raise VectorIndexUnavailable("private directory")

    result = build(vectors=FakeVectors(hits=[hit("asset_a", 0.9)]), index=ResourceIndex()).execute(request())
    assert [item.asset_id for item in result.hits] == ["asset_a"]
    assert result.backend == "sqlite_exact"
    assert result.degraded_reason == "vector_index_unavailable"
    assert result.operational_issue == {
        "error_code": "resource_error",
        "failure_code": "vector_index_unavailable",
        "error_type": "VectorIndexUnavailable",
    }


def test_no_index_and_stale_index_are_not_resource_failures():
    for index in (None, FakeIndex(count=0)):
        result = build(vectors=FakeVectors(hits=[hit("asset_a", 0.9)]), index=index).execute(request())
        assert result.degraded and result.hits
        assert result.operational_issue is None
