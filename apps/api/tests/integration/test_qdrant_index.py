"""真 Qdrant（嵌入式、临时目录）下的派生索引测试。

最后一条是本切片的核心：**目录被别人占用时必须降级得响亮**。1.0 的对应行为是
打印一行日志、接口照常返回空结果，于是「检索变差了」这件事在用户侧完全不可见。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from sentrix.domain.retrieval import scheme
from sentrix.domain.retrieval.ports import VectorIndexUnavailable, VectorRecord
from sentrix.infrastructure.vector.qdrant_index import QdrantVectorIndex

MODEL = scheme.IMAGE_MODEL
DIM = 4

# 子进程：打开目录、拿走锁、告诉父进程「就绪」，然后一直握着。
_LOCK_HOLDER = """
import sys, time
from qdrant_client import QdrantClient
client = QdrantClient(path=sys.argv[1])
client.get_collections()
print("ready", flush=True)
time.sleep(60)
"""


@pytest.fixture
def index(tmp_path: Path):
    instance = QdrantVectorIndex(tmp_path / "qdrant")
    yield instance
    instance.close()


def record(
    source_id: str,
    vector: tuple[float, ...] = (1.0, 0.0, 0.0, 0.0),
    *,
    scope_id: str = "home-default",
    source_type: str = "asset",
    model_name: str = MODEL,
    metadata: dict[str, object] | None = None,
) -> VectorRecord:
    return VectorRecord(
        scope_id=scope_id,
        space="visual",
        source_type=source_type,
        source_id=source_id,
        model_name=model_name,
        vector=vector,
        metadata=metadata or {},
    )


def test_upsert_then_search_returns_cosine_hits(index: QdrantVectorIndex):
    index.upsert([record("asset_near", (1.0, 0.0, 0.0, 0.0)), record("asset_far", (0.0, 0.0, 0.0, 1.0))])

    hits = index.search(space="visual", vector=(1.0, 0.0, 0.0, 0.0), limit=2, model_name=MODEL)

    assert [hit.source_id for hit in hits] == ["asset_near", "asset_far"]
    assert hits[0].score == pytest.approx(1.0)
    assert hits[0].metadata == {}


def test_upsert_is_idempotent_on_the_source_identity(index: QdrantVectorIndex):
    """point id 由 (space, source_type, source_id, model) 决定，重复写是覆盖不是新增。"""
    index.upsert([record("asset_1", (1.0, 0.0, 0.0, 0.0))])
    index.upsert([record("asset_1", (0.0, 1.0, 0.0, 0.0))])

    assert index.count(space="visual", model_name=MODEL) == 1
    hits = index.search(space="visual", vector=(0.0, 1.0, 0.0, 0.0), limit=1, model_name=MODEL)
    assert hits[0].score == pytest.approx(1.0)


def test_search_filters_by_scope(index: QdrantVectorIndex):
    index.upsert([record("asset_a", scope_id="album1"), record("asset_b", scope_id="album2")])

    hits = index.search(
        space="visual", vector=(1.0, 0.0, 0.0, 0.0), limit=5, scope_id="album1", model_name=MODEL
    )

    assert [hit.source_id for hit in hits] == ["asset_a"]


def test_models_do_not_share_a_collection(index: QdrantVectorIndex):
    """两套模型的向量维度可能相同，混在一个 collection 里就再也分不开。"""
    index.upsert([record("asset_new", model_name=MODEL)])
    index.upsert([record("asset_old", (0.0, 1.0, 0.0, 0.0), model_name="ViT-B-32")])

    hits = index.search(space="visual", vector=(1.0, 0.0, 0.0, 0.0), limit=5, model_name=MODEL)

    assert [hit.source_id for hit in hits] == ["asset_new"]
    assert index.count(space="visual", model_name=MODEL) == 1
    assert index.count(space="visual", model_name="ViT-B-32") == 1


def test_dimension_changes_the_collection(index: QdrantVectorIndex):
    index.upsert([record("asset_768", (1.0,) * DIM)])
    index.upsert([record("asset_1024", (1.0,) * 8)])

    hits = index.search(space="visual", vector=(1.0,) * DIM, limit=5, model_name=MODEL)

    assert [hit.source_id for hit in hits] == ["asset_768"]


def test_metadata_round_trips_through_the_payload(index: QdrantVectorIndex):
    index.upsert([record("asset_1", metadata={"observation_id": "obs_1"})])

    hits = index.search(space="visual", vector=(1.0, 0.0, 0.0, 0.0), limit=1, model_name=MODEL)

    assert hits[0].metadata == {"observation_id": "obs_1"}
    assert hits[0].scope_id == "home-default"


def test_status_is_available_on_a_fresh_directory(index: QdrantVectorIndex):
    status = index.status()

    assert status.backend == "qdrant"
    assert status.available is True
    assert status.degraded is False


def test_empty_vector_is_rejected_loudly(index: QdrantVectorIndex):
    with pytest.raises(ValueError):
        index.upsert([record("asset_empty", ())])


def test_search_on_an_empty_index_returns_no_hits(index: QdrantVectorIndex):
    assert index.search(space="visual", vector=(1.0, 0.0, 0.0, 0.0), limit=5, model_name=MODEL) == []


def test_locked_directory_degrades_loudly(tmp_path: Path):
    """目录被另一个进程占用：status 报 degraded + 原因，操作抛 VectorIndexUnavailable。

    qdrant-client 在嵌入式模式下对目录加独占锁（1.0 的 8091 就长期握着
    ``data/qdrant``）。2.0 必须把它变成一个**能被看见**的状态，而不是一行日志。
    """
    qdrant_dir = tmp_path / "locked"
    holder = subprocess.Popen(
        [sys.executable, "-c", _LOCK_HOLDER, str(qdrant_dir)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "ready", "持锁子进程没有就绪"

        index = QdrantVectorIndex(qdrant_dir)
        status = index.status()

        assert status.available is False
        assert status.degraded is True
        assert "already accessed" in status.reason
        assert status.operational_issue == {
            "error_code": "resource_error",
            "failure_code": "vector_index_unavailable",
            "error_type": "VectorIndexUnavailable",
        }

        with pytest.raises(VectorIndexUnavailable):
            index.search(space="visual", vector=(1.0, 0.0, 0.0, 0.0), limit=5, model_name=MODEL)
        with pytest.raises(VectorIndexUnavailable):
            index.upsert([record("asset_1")])
        with pytest.raises(VectorIndexUnavailable):
            index.count(space="visual", model_name=MODEL)
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_2_0_qdrant_dir_is_not_1_0_s(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """默认目录由 data_dir 派生，永远不会指到 1.0 的 data/qdrant。"""
    from sentrix.config import Settings

    monkeypatch.setenv("SENTRIX_DATA_DIR", str(tmp_path))
    settings = Settings()

    assert settings.qdrant_dir == tmp_path / "qdrant"
    assert settings.qdrant_dir != Path("/home/asus/Github/Sentrix-Home-Web/data/qdrant")


def test_concurrent_first_search_opens_one_persistent_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A cold shared adapter must not contend with its own directory lock."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Event, Lock

    import qdrant_client

    path = tmp_path / "cold-shared"
    seed = QdrantVectorIndex(path)
    seed.upsert([record("synthetic_concurrent_asset")])
    seed.close()
    instance = QdrantVectorIndex(path)
    real_client = qdrant_client.QdrantClient
    start = Barrier(8)
    constructed = Event()
    publish = Event()
    second_constructor = Event()
    counter_lock = Lock()
    attempts = 0

    def slow_constructor(*args, **kwargs):
        nonlocal attempts
        with counter_lock:
            attempts += 1
            if attempts > 1:
                second_constructor.set()
        client = real_client(*args, **kwargs)
        constructed.set()
        if not publish.wait(timeout=10):
            client.close()
            raise TimeoutError("test did not release the constructor")
        return client

    monkeypatch.setattr(qdrant_client, "QdrantClient", slow_constructor)

    def search():
        start.wait(timeout=10)
        return instance.search(space="visual", vector=(1.0, 0.0, 0.0, 0.0), limit=1, model_name=MODEL)

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(search) for _ in range(8)]
            try:
                assert constructed.wait(timeout=10), "first real Qdrant client never opened"
                # Keep the real directory lock held while other cold callers enter.
                second_constructor.wait(timeout=0.15)
            finally:
                publish.set()
            outcomes = []
            failures = []
            for future in futures:
                try:
                    outcomes.append(future.result(timeout=10))
                except VectorIndexUnavailable as error:
                    failures.append(str(error))
        assert failures == [], failures
        assert attempts == 1
        assert len(outcomes) == 8
        assert all([hit.source_id for hit in hits] == ["synthetic_concurrent_asset"] for hits in outcomes)
        assert instance.status().available
    finally:
        publish.set()
        instance.close()


def test_concurrent_initialization_failure_is_cached_and_remains_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Lock

    import qdrant_client

    instance = QdrantVectorIndex(tmp_path / "unavailable")
    start = Barrier(8)
    counter_lock = Lock()
    attempts = 0

    def unavailable_client(*args, **kwargs):
        nonlocal attempts
        with counter_lock:
            attempts += 1
        raise PermissionError("synthetic read-only vector directory")

    monkeypatch.setattr(qdrant_client, "QdrantClient", unavailable_client)

    def status():
        start.wait(timeout=10)
        return instance.status()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: status(), range(8)))

    assert attempts == 1
    assert all(not result.available and result.degraded for result in results)
    assert all("PermissionError" in result.reason for result in results)
    assert all(
        result.operational_issue and result.operational_issue["failure_code"] == "vector_index_unavailable"
        for result in results
    )
    with pytest.raises(VectorIndexUnavailable, match="PermissionError"):
        instance.search(space="visual", vector=(1.0, 0.0, 0.0, 0.0), limit=1, model_name=MODEL)
    assert attempts == 1


def test_close_waits_for_in_progress_client_initialization(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    import qdrant_client

    instance = QdrantVectorIndex(tmp_path / "closing")
    constructor_entered = Event()
    publish = Event()
    close_started = Event()
    close_done = Event()

    class Client:
        closed = False

        def close(self):
            self.closed = True

    client = Client()

    def slow_client(*args, **kwargs):
        constructor_entered.set()
        if not publish.wait(timeout=10):
            raise TimeoutError("test did not release the constructor")
        return client

    monkeypatch.setattr(qdrant_client, "QdrantClient", slow_client)

    def close():
        close_started.set()
        instance.close()
        close_done.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        opening = pool.submit(instance.status)
        try:
            assert constructor_entered.wait(timeout=10)
            closing = pool.submit(close)
            assert close_started.wait(timeout=10)
            assert not close_done.wait(timeout=0.05)
        finally:
            publish.set()
        assert opening.result(timeout=10).available
        closing.result(timeout=10)

    assert client.closed
    assert instance._client is None
