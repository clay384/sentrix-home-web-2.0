"""检索域端口：权威向量存储、派生向量索引、视觉嵌入器。

三者的分工是 1.0 已经验证过的契约，2.0 原样保留：

- ``VectorRepository`` 是**权威**（SQLite 里的 memory_vectors），写入必须先到它；
- ``VectorIndex`` 是**派生镜像**（Qdrant），可重建、可缺席；
- 索引缺席或锁被占用时，检索必须回退到权威存储，并把「正在降级」这件事报出去。

1.0 的教训是降级被写进了日志但没人看：Qdrant 目录锁被 8091 独占时，第二个进程
只打印一行 ``vector search degrades to SQLite full scans``，接口照常返回空结果。
所以这里的 ``VectorIndexStatus`` 是端口的一部分，降级原因必须能一路传到 HTTP 响应。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from sentrix.domain.media.models import Asset, Observation
from sentrix.domain.retrieval.structured_query import AssetSample, GroupCount, StructuredFilter


@dataclass(frozen=True, slots=True)
class VectorRecord:
    """一条待写入的向量。``(space, source_type, source_id, model_name)`` 唯一。"""

    scope_id: str
    space: str
    source_type: str
    source_id: str
    model_name: str
    vector: tuple[float, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def dimension(self) -> int:
        return len(self.vector)


@dataclass(frozen=True, slots=True)
class VectorHit:
    source_id: str
    scope_id: str
    source_type: str
    model_name: str
    score: float
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VectorIndexStatus:
    """索引健康度。``degraded`` 为真时必须是可行动的信息，不是一个布尔摆设。"""

    backend: str
    available: bool
    degraded: bool = False
    reason: str = ""
    operational_issue: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class LexicalTerm:
    """``observation_search_terms`` 的一行。``field_type`` 见 domain/retrieval/lexical.py。"""

    observation_id: str
    asset_id: str
    scope_id: str
    field_type: str
    normalized_value: str
    confidence: float
    source_type: str
    source_revision: int


class LexicalIndex(Protocol):
    """词法投影的写入侧。**派生表**：可整表重建，每一行追得回某条 Observation 的某个 revision。

    1.0 把这两个方法放在 ``RetrievalIndex`` 一个类里（``retrieval_indexes.py:105-294``）。
    2.0 只声明 S4 真正会调的两件事：按 observation 增量刷新、按 scope 全量重建。
    1.0 的 ``search``（LIKE 子串回退）不移植——那是 FTS5 缺席时的降级路径，而
    ``make ci`` 已经在实测 FTS5 可用（``sqlite3`` 自带），留一条永不执行的降级分支
    只会让人以为它被验证过。
    """

    def refresh_from_observation(self, observation: Observation) -> int: ...

    def rebuild_scope(self, scope_id: str) -> int: ...

    def count(self, scope_id: str | None = None) -> int: ...


class LexicalSearch(Protocol):
    """词法投影的读取侧。``token_hits`` 是 ``asset_id → 命中 token 数``。"""

    def search_fts(self, query: str, *, scope_id: str | None, limit: int) -> Sequence[tuple[str, int]]: ...

    def exact_assets(self, whole_query: str, *, scope_id: str | None) -> frozenset[str]: ...

    def assets_for_person(self, name: str, *, scope_id: str | None) -> frozenset[str]:
        """含这个人的 asset 集合，读 ``observation_search_terms`` 的 ``person_bridge`` 行。

        1.0 ``retrieval/entity.py:33-49`` 那条查询。**名字按整串精确匹配**（与写入时的
        ``normalize_text`` 同一口径），不做子串：人名是短词，「妈」命中「妈妈」与
        「姑妈」是两件不同的事。

        查不到就返回空集——**空集与「这个人在库里没有照片」在调用方是一回事**，
        由 ``RetrieveEntity`` 决定要不要退回直接读观察。
        """
        ...


class VectorRepository(Protocol):
    """权威向量存储。SQLite 是唯一实现，也是索引重建的数据来源。"""

    def upsert(self, record: VectorRecord) -> None: ...

    def has_vector(self, *, space: str, source_type: str, source_id: str, model_name: str) -> bool: ...

    def update_metadata(
        self, *, space: str, source_type: str, source_id: str, metadata: dict[str, object]
    ) -> int:
        """整份替换一条向量的 metadata，返回改了几行。

        全局重聚类会改动人脸实例属于哪个簇，而 ``cluster_id`` 也记在向量 metadata 里
        （1.0 ``db.py:5028-5038`` 对每条实例逐个改）。**整份替换而不是打补丁**：一条
        metadata 的写入者只有 ``EnrollFace`` 与重聚类两处，两处都构造得出一整份；
        读出旧值再合并会让并发下的两个写入者互相覆盖对方刚写进去的键，而那种丢失不报错。
        """
        ...

    def get(
        self,
        *,
        space: str,
        source_type: str,
        source_id: str,
        model_name: str | None = None,
    ) -> VectorRecord | None:
        """取一条权威向量。``model_name`` 为空时取最近更新的一条。

        事件归并读它算视觉相似度（1.0 ``db.py:2082-2110`` 的那条 SQL：
        ``ORDER BY updated_at DESC LIMIT 1``）。
        """
        ...

    def search_exact(
        self,
        *,
        space: str,
        vector: Sequence[float],
        limit: int,
        scope_id: str | None = None,
        model_name: str | None = None,
    ) -> Sequence[VectorHit]: ...

    def count(self, *, space: str, model_name: str | None = None) -> int: ...


class VectorIndexUnavailable(RuntimeError):
    """派生索引不可用（目录被占用、权限、磁盘满）。

    这是**预期内**的失败，所以必须是一个调用方能按类型捕获的异常，而不是
    「返回 0 / 返回空列表」——后者在调用点看不出区别，1.0 的静默降级就是这么来的。
    """


def retrieval_operational_issue(error: BaseException) -> dict[str, Any] | None:
    """Classify only typed resource failures, without retaining SQL or exception text.

    SQLite exposes stable numeric error codes on its exceptions. Checking the
    standard exception's MRO keeps this domain helper independent of adapters and
    accepts subclasses without treating a similarly worded application error as a lock.
    """
    code, failure = "", ""
    sqlite_code = getattr(error, "sqlite_errorcode", None)
    is_sqlite = any(
        cls.__module__ == "sqlite3" and cls.__name__ == "OperationalError" for cls in type(error).__mro__
    )
    if is_sqlite and isinstance(sqlite_code, int) and (sqlite_code & 0xFF) in (5, 6):
        code = "resource_error"
        failure = "database_busy" if (sqlite_code & 0xFF) == 5 else "database_locked"
    elif isinstance(error, MemoryError):
        code, failure = "resource_error", "out_of_memory"
    elif isinstance(error, TimeoutError):
        code, failure = "time_limit", "retrieval_timeout"
    elif isinstance(error, VectorIndexUnavailable):
        code, failure = "resource_error", "vector_index_unavailable"
    if not code:
        return None
    result: dict[str, Any] = {
        "error_code": code,
        "failure_code": failure,
        "error_type": type(error).__name__,
    }
    if is_sqlite:
        result["sqlite_errorcode"] = sqlite_code
    return result


class VectorIndex(Protocol):
    """派生向量索引。缺席不是错误，但必须被看见。

    ``upsert`` / ``search`` / ``count`` 在索引不可用时抛 ``VectorIndexUnavailable``，
    由 application 层决定是回退到权威存储还是把错误暴露出去——适配器不替它决定。
    """

    def upsert(self, records: Sequence[VectorRecord]) -> int: ...

    def search(
        self,
        *,
        space: str,
        vector: Sequence[float],
        limit: int,
        scope_id: str | None = None,
        model_name: str | None = None,
    ) -> Sequence[VectorHit]: ...

    def count(self, *, space: str, model_name: str | None = None) -> int: ...

    def status(self) -> VectorIndexStatus: ...


@dataclass(frozen=True, slots=True)
class VisualIndexOutcome:
    """一次视觉索引的结果。三个字段让「没索引上」和「为什么」都能一路传到接口。"""

    indexed: bool
    mirrored: bool = False
    reason: str = ""


@dataclass(frozen=True, slots=True)
class PreparedVisual:
    """视觉索引「两段式」的第一段产物：**只有计算，没有写库**。

    ``vector`` 非空表示这一段真的算出了要写的向量；空表示第二段没有东西可写，
    ``skip_reason`` 说明为什么（不是图片 / 已经索引过 / 嵌入不可用）。

    单列一层，是因为导入链要把「嵌入」与「写向量」拆到两个线程上：嵌入是一次到
    8102 的往返（慢、可并行），写向量必须按序发生——事件归属要读它，
    ``score_event_candidate`` 的 ``visual_boost`` 与 ``split_guard`` 都读
    ``memory_vectors``。
    """

    vector: tuple[float, ...] = ()
    skip_reason: str = ""


class AssetVisualIndexer(Protocol):
    """把一张 Asset 变成视觉记忆。导入用例依赖它，不依赖具体的嵌入器/索引。

    ``prepare`` / ``commit`` 是**同一次索引的两段**，``index`` 是它们的复合：
    ``index(asset)`` 与 ``commit(asset, prepare(asset))`` 逐字等价。分开是为了让
    导入链把第一段放进并行侧——它不写库，因此不受「视觉向量先于事件归属」那条
    顺序约束的管；写库的第二段必须留在串行侧按序发生。
    """

    def prepare(self, asset: Asset) -> PreparedVisual: ...

    def commit(self, asset: Asset, prepared: PreparedVisual) -> VisualIndexOutcome: ...

    def index(self, asset: Asset) -> VisualIndexOutcome: ...


@runtime_checkable
class EmbeddingDiagnostics(Protocol):
    """Optional request-local diagnostics; reading must not perform IO.

    Cleared before each embedding attempt, including empty input. A shared
    adapter must isolate this value between caller threads.
    """

    @property
    def last_operational_issue(self) -> Mapping[str, Any] | None: ...


class VisualEmbedder(Protocol):
    """跨模态视觉嵌入：同一空间里的文本查询向量与图像向量。

    只管两个方向，不带设备、批次、模型元数据——``model_id`` 与 ``dimension``
    是索引兼容性校验的全部依据。

    这两个成员声明成只读 property 而不是裸属性：适配器的实现是 property，
    mypy 的 ``arg-type`` 不允许一个只读 property 去满足「可写变量」的协议约定。
    """

    @property
    def model_id(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    def embed_image(self, path: Path) -> Sequence[float]: ...

    def embed_query(self, text: str) -> Sequence[float]: ...


class TextEmbedder(Protocol):
    """文本嵌入：一段语义文本 → 向量。

    **与 ``VisualEmbedder`` 分开，不合并。** 两者的失败模式不同：视觉嵌入是本进程内
    推理，权重缺席时不可用；文本嵌入是一次 HTTP 往返（bge-m3 sidecar，8101），
    sidecar 可能没起、可能起了但模型没加载、也可能只是这一趟超时。合成一个端口，
    调用方就分不出「这台机器没有文本能力」与「刚才那一次网络抖了」——
    而前者要降级、后者应该重试。

    成员声明成只读 property，理由同 ``VisualEmbedder``：适配器的实现是 property，
    mypy 的 ``arg-type`` 不允许只读 property 去满足可写变量的协议约定。
    """

    @property
    def model_id(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    def embed_query(self, text: str) -> Sequence[float]: ...


class StructuredQueryRepository(Protocol):
    """结构化聚合的读取侧：数量 / 极值 / 分组，**全部走 SQL，不经过 ANN**。

    1.0 ``structured_memory.py`` 的 ``StructuredMemoryExecutor`` 把「筛什么」与
    「怎么筛」写在同一个类里；2.0 拆成两半，这一半只声明**能问什么**——「筛什么」
    由 ``StructuredFilter`` 决定，实现在 infrastructure。

    放进检索域的端口而不是让调用方直接拿仓储，理由与其余端口一样：调用方在
    application 层，不该 import 一个 sqlite3 的实现。

    **``captured_at`` 原样给字符串，不给 ``datetime``。** 它在库里就是 ISO 串，
    解析回 ``datetime`` 只是把同一个值绕一圈，而绕的那一圈会顺手把「这一列里
    混着 naive 值与带偏移量的值」这件事变成一个异常或者一个静默的时区假设。
    真要做归一，得先决定 naive 的值算哪个时区——那是**写入侧**的事，不是读取侧
    能替它决定的。
    """

    def count(self, filter: StructuredFilter) -> int:
        """满足筛选条件的资产数。**按资产去重**：一个资产可以有多条观察，
        一个人可以在一张照片里被提及两次，``COUNT(*)`` 会随观察条数翻倍。"""
        ...

    def captured_bound(self, filter: StructuredFilter, *, latest: bool) -> str | None:
        """最早（``latest=False``）或最晚（``latest=True``）的 ``captured_at``。

        空集返回 ``None`` 而不是抛：1.0 的 ``_first_last`` 就是这样，而且
        「这个条件下没有照片」是一个正常答案，不是错误。
        """
        ...

    def group_counts(self, filter: StructuredFilter, group_by: str) -> Sequence[GroupCount]:
        """按 ``group_by`` 分组计数，**按计数倒序**（1.0 的 ``ORDER BY n DESC, g``）。"""
        ...

    def sample_observations(self, filter: StructuredFilter, *, limit: int) -> Sequence[AssetSample]:
        """取几条代表观察，按拍摄时间倒序，给统计结果附证据。

        **返回值不能直接发给模型**：每条带 ``asset_id``。1.0 的 ``_fact_samples``
        就是在这里切了一刀，只把 ``captured_at`` / ``media_type`` / ``caption``
        放进工具返回值；2.0 把那一刀对应到 ``ToolOutcome`` 的两份投影上。
        """
        ...


@runtime_checkable
class EmbeddingHealthProbe(Protocol):
    """嵌入能力**现在**能不能用。

    **为什么单独一个端口，而不是给 ``VisualEmbedder`` 加两个方法。**
    ``VisualEmbedder`` 回答的是「怎么把一段文字/一张图变成向量」，那是**能力**；
    这一格回答的是「此刻这台机器还嵌不嵌得动」，那是**状态**。两者会独立变化——
    2026-10-10 那轮评测里侧车全程不在，而接口一切正常、检索照常返回结果，
    354/370 次调用静默降级成 ``backend=none``，``/health`` 一路回 ``ok``。

    ``available`` 与 ``load_error`` 的分工同 ``VectorIndexStatus``：能不能用是一个
    可问的问题，为什么不能用是另一个。合成一个布尔值，排查时只剩「不能用」三个字。

    两个成员声明成只读 property 而不是裸方法，理由同 ``VisualEmbedder``：
    ``WeMMSidecarEmbedder`` 的实现是 property，而 mypy 不允许一个只读 property 去
    满足「可调用的方法」的协议约定。
    """

    @property
    def available(self) -> bool: ...

    @property
    def load_error(self) -> str: ...
