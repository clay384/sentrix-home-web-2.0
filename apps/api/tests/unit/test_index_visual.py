"""IndexAssetVisual 的单元测试：全部用假适配器，不碰模型也不碰磁盘。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from sentrix.application.retrieval.index_visual import SOURCE_TYPE, IndexAssetVisual
from sentrix.domain.media.models import Asset, AssetStatus, MediaType
from sentrix.domain.retrieval import scheme
from sentrix.domain.retrieval.ports import (
    VectorIndexUnavailable,
    VectorRecord,
    VisualIndexOutcome,
)

NOW = datetime(2026, 9, 22, tzinfo=UTC)


class FakeEmbedder:
    model_id = scheme.IMAGE_MODEL
    dimension = 4

    def __init__(self, vector: list[float] | None = None) -> None:
        self.vector = [1.0, 0.0, 0.0, 0.0] if vector is None else vector
        self.calls: list[Path] = []

    def embed_image(self, path: Path) -> list[float]:
        self.calls.append(Path(path))
        return list(self.vector)

    def embed_query(self, text: str) -> list[float]:
        return list(self.vector)


class FakeVectors:
    def __init__(self) -> None:
        self.records: list[VectorRecord] = []

    def upsert(self, record: VectorRecord) -> None:
        self.records.append(record)

    def has_vector(self, *, space, source_type, source_id, model_name) -> bool:
        return any(
            item.space == space
            and item.source_type == source_type
            and item.source_id == source_id
            and item.model_name == model_name
            for item in self.records
        )

    def get(self, *, space, source_type, source_id, model_name=None):
        for item in reversed(self.records):
            if (
                item.space == space
                and item.source_type == source_type
                and item.source_id == source_id
                and (model_name is None or item.model_name == model_name)
            ):
                return item
        return None

    def search_exact(self, *, space, vector, limit, scope_id=None, model_name=None):
        return []

    def count(self, *, space: str) -> int:
        return len(self.records)


class FakeIndex:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.records: list[VectorRecord] = []

    def upsert(self, records):
        if not self.available:
            raise VectorIndexUnavailable("RuntimeError: Storage folder is already accessed")
        self.records.extend(records)
        return len(records)

    def search(self, **kwargs):
        return []

    def count(self, *, space, model_name=None) -> int:
        return len(self.records)

    def status(self):
        raise AssertionError("本用例不走 status()")


class BoomIndex(FakeIndex):
    def upsert(self, records):
        raise ZeroDivisionError("适配器里的真 bug")


def asset(path: str = "/media/asset_1.jpg", media_type: MediaType = MediaType.IMAGE) -> Asset:
    return Asset(
        id="asset_1",
        scope_id="album1",
        file_name="IMG_0001.jpg",
        media_type=media_type,
        path=path,
        mime_type="image/jpeg",
        size_bytes=10,
        status=AssetStatus.PROCESSED,
        content_sha256="abc",
        captured_at=NOW,
        captured_location=None,
        created_at=NOW,
        updated_at=NOW,
    )


def build(*, embedder=None, index=None):
    vectors = FakeVectors()
    use_case = IndexAssetVisual(embedder=embedder or FakeEmbedder(), vectors=vectors, index=index)
    return use_case, vectors


def test_image_is_embedded_and_stored_in_both_stores():
    embedder = FakeEmbedder()
    index = FakeIndex()
    use_case, vectors = build(embedder=embedder, index=index)

    outcome = use_case.index(asset())

    assert outcome.indexed is True
    assert outcome.mirrored is True
    assert embedder.calls == [Path("/media/asset_1.jpg")]
    record = vectors.records[0]
    assert record.source_type == SOURCE_TYPE
    assert record.source_id == "asset_1"
    assert record.space == scheme.VISUAL_SPACE
    assert record.model_name == scheme.IMAGE_MODEL
    assert record.scope_id == "album1"
    assert record.vector == (1.0, 0.0, 0.0, 0.0)
    assert index.records == [record]


def test_non_image_assets_are_not_embedded():
    embedder = FakeEmbedder()
    use_case, vectors = build(embedder=embedder)

    outcome = use_case.index(asset(media_type=MediaType.AUDIO))

    assert outcome == VisualIndexOutcome(indexed=False, reason="not_an_image")
    assert embedder.calls == []
    assert vectors.records == []


def test_empty_embedding_is_not_written_and_is_reported():
    """空向量绝不落库——1.0 的 223 张「没有向量的图」就是这么来的。"""
    use_case, vectors = build(embedder=FakeEmbedder(vector=[]))

    outcome = use_case.index(asset())

    assert outcome.indexed is False
    assert outcome.reason == "embedding_unavailable"
    assert vectors.records == []


def test_dimension_mismatch_is_reported_not_stored():
    use_case, vectors = build(embedder=FakeEmbedder(vector=[1.0, 0.0]))

    outcome = use_case.index(asset())

    assert outcome.indexed is False
    assert outcome.reason == "dimension_mismatch:2!=4"
    assert vectors.records == []


def test_second_index_of_the_same_asset_does_not_re_embed_but_re_mirrors():
    """**重复导入不再重跑推理，但必须再镜像一次。**

    旧实现在 ``already_indexed`` 上直接返回 ``mirrored=True``——它假设镜像早就在。
    Qdrant 目录锁导致镜像静默失败之后，那条点就永远补不回来（158 实测 770/1114）。
    """
    embedder = FakeEmbedder()
    index = FakeIndex()
    use_case, _ = build(embedder=embedder, index=index)
    first = use_case.index(asset())
    second = use_case.index(asset())

    assert first.indexed is True
    assert second == VisualIndexOutcome(indexed=True, mirrored=True, reason="already_indexed")
    assert len(embedder.calls) == 1, "重复导入不该再跑一次推理"
    assert len(index.records) == 2, "第二次也要 upsert 一次（幂等），才能补回丢失的点"


def test_an_already_indexed_asset_repairs_a_lost_mirror():
    """权威存储有行、派生索引丢了点：再导入/回填一次必须把点补回来，且不重嵌。"""
    embedder = FakeEmbedder()
    index = FakeIndex()
    use_case, _ = build(embedder=embedder, index=index)

    use_case.index(asset())
    index.records.clear()  # 模拟 Qdrant 镜像写失败/索引被清掉

    outcome = use_case.index(asset())

    assert outcome.indexed is True
    assert outcome.mirrored is True
    assert len(index.records) == 1
    assert len(embedder.calls) == 1


def test_unavailable_index_does_not_fail_the_authoritative_write():
    """镜像挂了可以降级，但不能把权威写入一起拖下水。"""
    use_case, vectors = build(index=FakeIndex(available=False))

    outcome = use_case.index(asset())

    assert outcome.indexed is True
    assert outcome.mirrored is False
    assert "already accessed" in outcome.reason
    assert len(vectors.records) == 1


def test_missing_index_is_reported_as_not_mirrored():
    use_case, vectors = build(index=None)

    outcome = use_case.index(asset())

    assert outcome.indexed is True
    assert outcome.mirrored is False
    assert outcome.reason == "no_index_configured"
    assert len(vectors.records) == 1


def test_unexpected_index_errors_are_not_downgraded():
    """只吞 VectorIndexUnavailable。适配器里的真 bug 必须炸出来。"""
    use_case, _ = build(index=BoomIndex())

    with pytest.raises(ZeroDivisionError):
        use_case.index(asset())
