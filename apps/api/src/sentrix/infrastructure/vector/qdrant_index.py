"""Qdrant 派生向量索引（嵌入式本地模式）。

对应 1.0 ``backend/qdrant_memory.py``，三处刻意的差异：

1. **目录必须独立**。嵌入式 Qdrant 对目录加独占锁，1.0 的 ``data/qdrant`` 被
   8091 进程长期持有。第二个进程打开同一目录时 qdrant-client 会抛
   ``RuntimeError: Storage folder ... is already accessed by another instance``。
   2.0 把这个异常翻成 ``VectorIndexUnavailable``——**降级可以，静默不行**。
   1.0 是打印一行日志然后继续，接口照常返回空结果。
2. **collection 按 (space, model, dimension) 分，不按 scope 分**。1.0 按 scope 切，
   生产上长到 634 个 collection；每张相册一个 HNSW 图的收益抵不过这个数量级。
   scope 过滤改走 payload，权威性由 SQLite 侧的 scope 列保证。
3. **可观测**：``status()`` 与 ``count()`` 是端口的一部分，应用层据此判断索引是否
   落后于权威存储，从而决定是否回退——1.0 的 ``data/ann/visual.hnsw`` 只有 1,170
   条而真实 10,590 条，就是因为没人比对过这两个数。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sentrix.domain.retrieval.ports import (
    VectorHit,
    VectorIndexStatus,
    VectorIndexUnavailable,
    VectorRecord,
    retrieval_operational_issue,
)

DEFAULT_PREFIX = "sentrix_home"
# 与 1.0 的 uuid 命名空间不同：同一个 source_id 在 2.0 的索引里是不同的 point，
# 免得两套系统的点互相覆盖。
_POINT_NAMESPACE = uuid.UUID("1f6f2a4e-0f9c-4d2f-9a3b-6c9f1f2b7d51")
_BATCH_SIZE = 256


def _safe(value: str, limit: int = 24) -> str:
    text = re.sub(r"[^a-zA-Z0-9_]+", "_", str(value or "").lower()).strip("_")
    return (text or "default")[:limit]


class QdrantVectorIndex:
    def __init__(self, path: Path, prefix: str = DEFAULT_PREFIX) -> None:
        self.path = Path(path)
        self.prefix = prefix
        self._client: Any = None
        self._error = ""

    # ---- 客户端 ----------------------------------------------------------

    def _get_client(self) -> Any:
        """惰性取客户端。失败后缓存原因，不再每次重试——重试只是反复抛同一个异常。"""
        if self._client is not None:
            return self._client
        if self._error:
            raise VectorIndexUnavailable(self._error)
        from qdrant_client import QdrantClient

        try:
            self.path.mkdir(parents=True, exist_ok=True)
            self._client = QdrantClient(path=str(self.path))
        except Exception as error:  # noqa: BLE001 - 目录锁/权限/磁盘满都归为「索引不可用」
            self._error = f"{type(error).__name__}: {error}"
            raise VectorIndexUnavailable(self._error) from error
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # ---- 端口 ------------------------------------------------------------

    def status(self) -> VectorIndexStatus:
        try:
            self._get_client()
        except VectorIndexUnavailable as error:
            return VectorIndexStatus(
                backend="qdrant",
                available=False,
                degraded=True,
                reason=str(error),
                operational_issue=retrieval_operational_issue(error),
            )
        return VectorIndexStatus(backend="qdrant", available=True)

    def upsert(self, records: Sequence[VectorRecord]) -> int:
        if not records:
            return 0
        for record in records:
            if not record.vector:
                raise ValueError("拒绝写入空向量：空向量在检索侧表现为「这张图就是搜不到」")
        client = self._get_client()
        from qdrant_client import models

        grouped: dict[str, list[VectorRecord]] = {}
        for record in records:
            grouped.setdefault(
                self._collection(record.space, record.model_name, record.dimension), []
            ).append(record)

        written = 0
        for collection, group in grouped.items():
            sample = group[0]
            self._ensure_collection(client, collection, sample.space, sample.dimension)
            points = [
                models.PointStruct(
                    id=self._point_id(record),
                    vector={record.space: [float(value) for value in record.vector]},
                    payload={
                        "scope_id": record.scope_id,
                        "space": record.space,
                        "source_type": record.source_type,
                        "source_id": record.source_id,
                        "model_name": record.model_name,
                        "metadata": dict(record.metadata),
                    },
                )
                for record in group
            ]
            for offset in range(0, len(points), _BATCH_SIZE):
                batch = points[offset : offset + _BATCH_SIZE]
                client.upsert(collection_name=collection, points=batch, wait=True)
                written += len(batch)
        return written

    def search(
        self,
        *,
        space: str,
        vector: Sequence[float],
        limit: int,
        scope_id: str | None = None,
        model_name: str | None = None,
    ) -> Sequence[VectorHit]:
        if not vector or limit < 1:
            return []
        client = self._get_client()
        from qdrant_client import models

        query_filter = None
        if scope_id:
            query_filter = models.Filter(
                must=[models.FieldCondition(key="scope_id", match=models.MatchValue(value=scope_id))]
            )

        hits: list[VectorHit] = []
        for collection in self._collections_for(client, space, model_name, len(vector)):
            response = client.query_points(
                collection_name=collection,
                query=[float(value) for value in vector],
                using=space,
                query_filter=query_filter,
                limit=limit,
                with_payload=True,
                with_vectors=False,
            )
            hits.extend(self._hit(point) for point in response.points)
        hits.sort(key=lambda item: (-item.score, item.source_id))
        return hits[:limit]

    def count(self, *, space: str, model_name: str | None = None) -> int:
        client = self._get_client()
        total = 0
        for collection in self._collections_for(client, space, model_name, None):
            info = client.get_collection(collection)
            total += int(info.points_count or 0)
        return total

    # ---- 内部 ------------------------------------------------------------

    def _collection(self, space: str, model_name: str, dimension: int) -> str:
        return f"{self.prefix}_{_safe(space)}_{dimension}_{_safe(model_name)}"

    def _collection_prefix(self, space: str) -> str:
        return f"{self.prefix}_{_safe(space)}_"

    def _collections_for(
        self,
        client: Any,
        space: str,
        model_name: str | None,
        dimension: int | None,
    ) -> list[str]:
        prefix = self._collection_prefix(space)
        names = [item.name for item in client.get_collections().collections if item.name.startswith(prefix)]
        if model_name:
            suffix = f"_{_safe(model_name)}"
            names = [name for name in names if name.endswith(suffix)]
        if dimension is not None:
            marker = f"_{dimension}_"
            names = [name for name in names if marker in name]
        return sorted(names)

    def _ensure_collection(self, client: Any, collection: str, space: str, dimension: int) -> None:
        from qdrant_client import models

        existing = {item.name for item in client.get_collections().collections}
        if collection in existing:
            return
        client.create_collection(
            collection_name=collection,
            vectors_config={space: models.VectorParams(size=dimension, distance=models.Distance.COSINE)},
        )

    @staticmethod
    def _point_id(record: VectorRecord) -> str:
        identity = f"{record.space}\0{record.source_type}\0{record.source_id}\0{record.model_name}"
        return str(uuid.uuid5(_POINT_NAMESPACE, identity))

    @staticmethod
    def _hit(point: Any) -> VectorHit:
        payload = dict(point.payload or {})
        return VectorHit(
            source_id=str(payload.get("source_id") or ""),
            scope_id=str(payload.get("scope_id") or ""),
            source_type=str(payload.get("source_type") or ""),
            model_name=str(payload.get("model_name") or ""),
            score=float(point.score),
            metadata=dict(payload.get("metadata") or {}),
        )
