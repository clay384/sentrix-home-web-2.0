"""``observation_search_terms`` + ``observation_search_fts`` 仓储。SQL 只在这一层。

对应 1.0 ``backend/retrieval_indexes.py:105-345`` 的 ``RetrievalIndex``。

三处与 1.0 的差异，都在别处记了理由：

1. ``refresh_from_observation`` 的**调用点**是导入路径（1.0 有这个方法但生产零调用，
   见 ``s4-deviations.md`` §1 第 4 项）；
2. ``rebuild_scope`` 的删除范围按 scope 收敛，1.0 的 scoped rebuild 会先清空整张 FTS
   再只补一个 scope（``retrieval_indexes.py:271-295`` 的注释就是在记这个 bug）；
3. 不提供 LIKE 子串回退（``search``）——FTS5 缺席时的降级分支在 2.0 永远不会走。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sentrix.domain.media.models import Observation
from sentrix.domain.media.ports import AssetRepository, ObservationRepository
from sentrix.domain.places.ports import AssetPlaceRepository
from sentrix.domain.retrieval.lexical import (
    FIELD_SOURCES,
    AnchorTerm,
    anchor_terms,
    fts_content,
    normalize_text,
)
from sentrix.infrastructure.persistence.database import Database

# 1.0 ``rebuild_all`` 用的是 ``list_observations(limit=10_000)``（``:272``）。同一个上限：
# 词法投影是派生数据，一次重建漏掉第 10001 条之后的行会静默丢召回，所以重建范围
# 必须与 1.0 一样**有界且被写出来**，而不是一个看起来无穷大的默认值。
REBUILD_LIMIT = 10_000

# 1.0 ``:150-158`` 的 ``term_sources`` 七条。``person_bridge`` 在 S5b 之前是唯一缺席的
# 一条（身份层属于 S5，没有 ``entity_mentions`` 可桥），所以这一版**不再过滤**：
# 加过滤等于留一个「字段清单与 1.0 对齐、实际少一个」的假象。
_ACTIVE_FIELD_SOURCES = FIELD_SOURCES


def _new_id() -> str:
    return f"term_{uuid.uuid4().hex[:16]}"


def _values(raw: object) -> tuple[str, ...]:
    if isinstance(raw, (list, tuple, set)):
        items: Sequence[object] = list(raw)
    else:
        items = [raw]
    return tuple(value for value in (normalize_text(item) for item in items) if value)


class SqliteLexicalIndex:
    """实现 ``LexicalIndex`` 与 ``LexicalSearch`` 两个端口。"""

    def __init__(
        self,
        database: Database,
        observations: ObservationRepository,
        *,
        assets: AssetRepository,
        places: AssetPlaceRepository,
    ) -> None:
        self._database = database
        self._observations = observations
        # 锚点列（geo / time / file）的值来自 **asset** 而不是 observation，所以写侧必须
        # 拿得到 asset 与它解析出的地点。两个都是必填：缺了它们只会静默少写三列，
        # 而那正是这一版要修的缺陷（见 ``docs/refactor/s4-deviations.md`` §7）。
        self._assets = assets
        self._places = places

    # ---- 写入 ----------------------------------------------------------------

    def refresh_from_observation(self, observation: Observation) -> int:
        """删掉这条 Observation 的旧行，按当前 revision 重写词法投影。

        「先删后插」而不是 UPSERT：一条 Observation 的字段数会因为重新富集而**变少**
        （VLM 第二轮把 objects 收敛掉了），增量更新留不下这种删除。
        """
        now = datetime.now(UTC).isoformat()
        rows = self._term_rows(observation, now)
        with self._database.connect() as connection, connection:
            connection.execute(
                "DELETE FROM observation_search_terms WHERE observation_id = ?",
                (observation.id,),
            )
            connection.execute(
                "DELETE FROM observation_search_fts WHERE observation_id = ?", (observation.id,)
            )
            for row in rows:
                connection.execute(
                    "INSERT INTO observation_search_terms (id, observation_id, asset_id, scope_id,"
                    " field_type, normalized_value, confidence, source_type, source_revision,"
                    " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    row,
                )
                tokens = fts_content(row[5])
                if not tokens:
                    continue
                connection.execute(
                    "INSERT INTO observation_search_fts (tokens, scope_id, field_type, asset_id,"
                    " observation_id) VALUES (?,?,?,?,?)",
                    (tokens, observation.scope_id, row[4], observation.asset_id, observation.id),
                )
        return len(rows)

    def _term_rows(self, observation: Observation, now: str) -> list[tuple[object, ...]]:
        rows: list[tuple[object, ...]] = []
        for field_type, value in _observation_terms(observation):
            rows.append(
                (
                    _new_id(),
                    observation.id,
                    observation.asset_id,
                    observation.scope_id,
                    field_type,
                    value,
                    observation.confidence,
                    "observation",
                    observation.revision,
                    now,
                    now,
                )
            )
        for term in self._anchor_terms(observation):
            rows.append(
                (
                    _new_id(),
                    observation.id,
                    observation.asset_id,
                    observation.scope_id,
                    term.field_type,
                    term.normalized_value,
                    term.confidence,
                    term.source_type,
                    observation.revision,
                    now,
                    now,
                )
            )
        return rows

    def _anchor_terms(self, observation: Observation) -> tuple[AnchorTerm, ...]:
        """1.0 ``retrieval_indexes.py:225-245``：从 asset 的 file_name / captured_at 与
        解析出的地点生成 geo / time / file 三列锚点。

        调用时点与 1.0 相同（写在 ``refresh_from_observation`` 里），而导入路径的
        「地点解析 → 观察 → 词法投影」顺序保证这里读得到 ``asset_places`` 那一行
        （``import_assets.py`` 的模块注释把顺序写成了硬约束）。
        """
        asset = self._assets.get(observation.asset_id)
        if asset is None:
            return ()
        place = self._places.get(observation.asset_id)
        terms = anchor_terms(
            file_name=asset.file_name,
            captured_at=asset.captured_at.isoformat() if asset.captured_at else "",
            place_text=place.searchable_text() if place is not None else "",
        )
        if asset.source_location:
            terms += (
                AnchorTerm(
                    "source_location", normalize_text(asset.source_location), "imported_source_metadata", 1.0
                ),
            )
        return terms

    def rebuild_scope(self, scope_id: str) -> int:
        """按 scope 重建。**先按 scope 删**，不清整表。

        1.0 的 ``rebuild_all(scope_id)`` 无条件 ``DELETE FROM observation_search_fts``
        然后只补这一个 scope 的行，其他相册的词法通道被静默清空——1.0 自己的注释
        （``retrieval_indexes.py:278-284``）就是在记这个 bug 的修复。
        """
        rebuilt = 0
        for observation in self._observations.list_scope(scope_id, limit=REBUILD_LIMIT):
            rebuilt += self.refresh_from_observation(observation)
        return rebuilt

    def count(self, scope_id: str | None = None) -> int:
        with self._database.connect() as connection:
            if scope_id:
                row = connection.execute(
                    "SELECT COUNT(*) AS n FROM observation_search_terms WHERE scope_id = ?",
                    (scope_id,),
                ).fetchone()
            else:
                row = connection.execute("SELECT COUNT(*) AS n FROM observation_search_terms").fetchone()
        return int(row["n"]) if row else 0

    # ---- 读取 ----------------------------------------------------------------

    def search_fts(self, query: str, *, scope_id: str | None, limit: int) -> Sequence[tuple[str, int]]:
        """每个 query token 独立查一次，按 asset 累计命中数。

        返回 ``(asset_id, 命中 token 数)``。**不同 token 命中同一个 asset 才累加**——
        同一个 token 命中多条 observation（同一 asset 有多条观察）只算一次，
        与 1.0 ``retrieval_indexes.py:316-322`` 的 ``token_hits`` 语义一致。
        """
        from sentrix.domain.retrieval.lexical import query_tokens

        tokens = query_tokens(query)
        if not tokens:
            return []
        hits: dict[str, int] = {}
        with self._database.connect() as connection:
            for token in tokens:
                clause = "WHERE observation_search_fts MATCH ?"
                params: list[object] = [f'"{token}"']
                if scope_id:
                    clause += " AND scope_id = ?"
                    params.append(scope_id)
                try:
                    rows = connection.execute(
                        f"SELECT DISTINCT asset_id FROM observation_search_fts {clause}", params
                    ).fetchall()
                except Exception:
                    # FTS5 的 MATCH 语法对含引号/运算符的 token 会抛。1.0 同样吞掉这一条
                    # （``retrieval_indexes.py:311-315``），跳过该 token 继续。
                    continue
                for row in rows:
                    hits[row["asset_id"]] = hits.get(row["asset_id"], 0) + 1
        return sorted(hits.items(), key=lambda item: (-item[1], item[0]))[:limit]

    def assets_for_person(self, name: str, *, scope_id: str | None) -> frozenset[str]:
        """含这个人的 asset 集合（1.0 ``retrieval/entity.py:35-46`` 那条查询）。

        ``field_type`` 是硬条件：不加它，同名的一个地名或物品也会把 asset 带进来。
        名字走 ``normalize_text``（与写入侧同一个口径）——写成 ``name.lower()`` 会在
        全角空格或多空格的名字上悄悄漏掉。
        """
        value = normalize_text(name)
        if not value:
            return frozenset()
        clause = " WHERE field_type = 'person_bridge' AND normalized_value = ?"
        params: list[object] = [value]
        if scope_id:
            clause += " AND scope_id = ?"
            params.append(scope_id)
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT asset_id FROM observation_search_terms" + clause, params
            ).fetchall()
        return frozenset(row["asset_id"] for row in rows)

    def exact_assets(self, whole_query: str, *, scope_id: str | None) -> frozenset[str]:
        """库里存在 ``normalized_value`` 与整串 query **完全相等**的行的 asset 集合。"""
        whole = normalize_text(whole_query)
        if not whole:
            return frozenset()
        clause = " WHERE normalized_value = ?"
        params: list[object] = [whole]
        if scope_id:
            clause += " AND scope_id = ?"
            params.append(scope_id)
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT asset_id FROM observation_search_terms" + clause, params
            ).fetchall()
        return frozenset(row["asset_id"] for row in rows)


def _observation_terms(observation: Observation) -> list[tuple[str, str]]:
    """把一条 Observation 展开成 ``(field_type, normalized_value)`` 列表。

    1.0 的 ``_terms_from_field`` + ``_terms_from_detail``（``retrieval_indexes.py:70-100``）。
    ``detail`` 那一路在 2.0 不建——S4 的 Observation 没有 ``detail`` 字段，
    ``visible_details/regions/text_blocks`` 属于 S6 的 OCR/VLM 精读。
    """
    terms: list[tuple[str, str]] = []
    for field_type, attribute in _ACTIVE_FIELD_SOURCES:
        raw = getattr(observation, attribute, None)
        for value in _values(raw):
            terms.append((field_type, value))
    return terms
