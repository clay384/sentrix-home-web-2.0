"""多路召回编排放的用例：真 SQLite / 真 FTS5 / 真地点表，嵌入器脚本化。

这一层验的是**接线本身**：三条通道有没有真的进同一个 ``fuse``，进去之后名次与加成
对不对。每条通道自己的行为已经由 ``test_retrieve_lexical_metadata.py``（词法 / 元数据）
与 ``test_retrieve_visual.py``（视觉）钉过，这里不重复。

``fuse`` 的数值全部来自 ``docs/refactor/behaviour-baseline.md``：``RRF_K = 60``、
anchor ``+1.0``、visual 2.5 / lexical 1.0 / metadata 1.0。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sentrix.application.retrieval.retrieve_entity import RetrieveEntity
from sentrix.application.retrieval.retrieve_evidence import (
    EvidenceRequest,
    RetrieveEvidence,
)
from sentrix.application.retrieval.retrieve_graph import GraphSearchResult
from sentrix.application.retrieval.retrieve_lexical import RetrieveLexical
from sentrix.application.retrieval.retrieve_metadata import RetrieveMetadata
from sentrix.application.retrieval.retrieve_text_ann import RetrieveTextAnn
from sentrix.application.retrieval.retrieve_visual import RetrieveVisual, VisualSearchRequest
from sentrix.domain.media.models import Asset, AssetStatus, GpsPoint, MediaType, Observation
from sentrix.domain.places.aliases import EMPTY_ALIASES
from sentrix.domain.places.models import (
    AdminArea,
    NearestPlace,
    Place,
    PlacePrecision,
    PlaceSource,
)
from sentrix.domain.retrieval import scheme
from sentrix.domain.retrieval.models import CandidateHit
from sentrix.domain.retrieval.ports import VectorRecord
from sentrix.infrastructure.persistence.database import Database
from sentrix.infrastructure.persistence.repositories.asset_places import SqliteAssetPlaceRepository
from sentrix.infrastructure.persistence.repositories.assets import (
    SqliteAssetRepository,
    SqliteObservationRepository,
)
from sentrix.infrastructure.persistence.repositories.search_terms import SqliteLexicalIndex
from sentrix.infrastructure.persistence.repositories.vectors import SqliteVectorRepository

SCOPE = "album1"
BASE = datetime(2017, 10, 5, 12, 0, 0, tzinfo=UTC)
XIAOSHAN = GpsPoint(30.1839, 120.2642)

# 三张图的向量与查询向量分开指定：视觉名次是构造出来的，不是碰运气撞上的。
QUERY = [1.0, 0.0, 0.0, 0.0]
CLOSE = [0.8, 0.6, 0.0, 0.0]
MIDDLE = [0.6, 0.8, 0.0, 0.0]
COLOURS = {"asset_a": QUERY, "asset_b": CLOSE, "asset_c": MIDDLE}


class ScriptedEmbedder:
    """文本 → 查询向量；图片字节 → 各自指定的向量。"""

    model_id = scheme.IMAGE_MODEL
    dimension = 4

    def __init__(self, *, query: Sequence[float], images: dict[bytes, Sequence[float]]) -> None:
        self._query = list(query)
        self._images = {key: list(value) for key, value in images.items()}

    def embed_query(self, text: str) -> Sequence[float]:
        return list(self._query)

    def embed_image(self, path: Path) -> Sequence[float]:
        return list(self._images[Path(path).read_bytes()])


def xiaoshan() -> Place:
    return Place(
        admin=AdminArea("浙江省", "杭州市", "萧山区"),
        nearest=NearestPlace("赵州镇", "大石桥村", 270.0),
        precision=PlacePrecision.DISTRICT,
        source=PlaceSource.PYGEOCN,
        gps=XIAOSHAN,
        confidence=0.9,
    )


@pytest.fixture
def library(migrations_ready_db: Database, tmp_path: Path):
    """三张图：A 视觉第一，B 视觉第二且词法第二，C 词法第一。"""
    assets = SqliteAssetRepository(migrations_ready_db)
    observations = SqliteObservationRepository(migrations_ready_db)
    places = SqliteAssetPlaceRepository(migrations_ready_db)
    vectors = SqliteVectorRepository(migrations_ready_db)
    index = SqliteLexicalIndex(migrations_ready_db, observations, assets=assets, places=places)

    media = tmp_path / "media"
    media.mkdir()
    payloads: dict[bytes, Sequence[float]] = {}
    captions = {"asset_a": "雪山", "asset_b": "海边日落", "asset_c": "海边"}
    for asset_id, caption in captions.items():
        payload = f"{asset_id} 的字节".encode()
        path = media / f"{asset_id}.jpg"
        path.write_bytes(payload)
        payloads[payload] = COLOURS[asset_id]
        assets.add(
            Asset(
                id=asset_id,
                scope_id=SCOPE,
                file_name=f"{asset_id}.jpg",
                media_type=MediaType.IMAGE,
                path=str(path),
                mime_type="image/jpeg",
                size_bytes=len(payload),
                status=AssetStatus.PROCESSED,
                content_sha256=f"sha-{asset_id}",
                captured_at=BASE,
                # 三张都有 GPS，但只有 A 解析出了地点——「有 GPS + asset_places 里没有行」
                # 正是 ``RetrieveMetadata`` 的 ``recognize_place`` 要剔除的那一格。
                captured_location=XIAOSHAN,
                created_at=BASE,
                updated_at=BASE,
            )
        )
        observation = Observation(
            id=f"obs_{asset_id}",
            scope_id=SCOPE,
            asset_id=asset_id,
            source_type="import",
            captured_at=BASE,
            confidence=0.65,
            created_at=BASE,
            updated_at=BASE,
            caption=caption,
        )
        observations.add(observation)
        vectors.upsert(
            VectorRecord(
                scope_id=SCOPE,
                space=scheme.VISUAL_SPACE,
                source_type="asset",
                source_id=asset_id,
                model_name=scheme.IMAGE_MODEL,
                vector=tuple(COLOURS[asset_id]),
            )
        )
        if asset_id == "asset_a":
            places.save(asset_id=asset_id, scope_id=SCOPE, gps=XIAOSHAN, place=xiaoshan())
        index.refresh_from_observation(observation)

    return {
        "assets": assets,
        "places": places,
        "observations": observations,
        "index": index,
        "vectors": vectors,
        "embedder": ScriptedEmbedder(query=QUERY, images=payloads),
    }


class ScriptedTextEmbedder:
    """文本通道的假嵌入器。

    这个夹具里不写任何文本向量，所以它返回什么都不会命中——它的作用只是让
    ``RetrieveTextAnn`` 能构造起来并且**不报降级**（返回空表会被判成
    ``embedder_unavailable``，那会让 ``degraded`` 字段在被测的链路上失真）。
    维度与视觉那个不同（3 vs 4）是有意的：共用同一个假件、维度又写成一样的话，
    「查错空间」这件事测不出来。
    """

    model_id = scheme.TEXT_MODEL
    dimension = 3

    def embed_query(self, text: str) -> Sequence[float]:
        return [1.0, 0.0, 0.0]


def build(library, *, graph=None) -> RetrieveEvidence:
    def recognize_place(asset_id: str) -> bool:
        """「这张图本来就该有地点」= asset 上有 GPS（与 ``routes/search.py`` 同一判据）。"""
        asset = library["assets"].get(asset_id)
        return asset is not None and asset.captured_location is not None

    return RetrieveEvidence(
        visual=RetrieveVisual(embedder=library["embedder"], vectors=library["vectors"], index=None),
        entity=RetrieveEntity(index=library["index"], observations=library["observations"]),
        lexical=RetrieveLexical(index=library["index"]),
        metadata=RetrieveMetadata(
            assets=library["assets"],
            places=library["places"],
            aliases=EMPTY_ALIASES,
            recognize_place=recognize_place,
        ),
        text=RetrieveTextAnn(embedder=ScriptedTextEmbedder(), vectors=library["vectors"]),
        graph=graph,
    )


def channels_of(hit) -> dict[str, int]:
    return dict(hit.channels)


# ---- 三条通道确实进了同一个 fuse ------------------------------------------------


def test_the_lexical_channel_displaces_the_visual_winner(library):
    """「海边」的两张图视觉排在最后，词法把它们顶到了视觉冠军前面。

    视觉：A(1) B(2) C(3)；词法：C(1) B(2)（C 的 caption 与整串查询相等，吃到
    ``EXACT_BOOST``）。B 的融合分 ``2.5/62 + 1.0/62 = 0.05645``、C 的
    ``2.5/63 + 1.0/61 = 0.05608``，都高于纯视觉冠军 A 的 ``2.5/61 = 0.04098``。
    B 在 C 前面不是笔误：两条通道都排它第二，而 C 的视觉名次更靠后。
    """
    result = build(library).execute(EvidenceRequest(text="海边", scope_id=SCOPE, limit=3))

    assert [hit.asset_id for hit in result.hits] == ["asset_b", "asset_c", "asset_a"]
    assert channels_of(result.hits[0]) == {"visual_ann": 2, "lexical": 2}
    assert result.hits[0].score == pytest.approx(2.5 / 62 + 1.0 / 62)


def test_without_the_lexical_channel_that_photo_is_not_reachable(library):
    """上面那条的**反向**：只走视觉通道时，同一句话的首位是 A。

    这一条与上一条必须同时成立，否则「接上了 lexical」与「没接」在行为上无从区分。
    """
    visual_only = RetrieveVisual(
        embedder=library["embedder"], vectors=library["vectors"], index=None
    ).execute(VisualSearchRequest(text="海边", scope_id=SCOPE, limit=3))

    assert [hit.asset_id for hit in visual_only.hits][0] == "asset_a"
    assert all("lexical" not in hit.channels for hit in visual_only.hits)


def test_a_candidate_outside_every_channels_head_is_still_ranked_by_the_fusion(library):
    """交付上限只裁融合之后的结果，不裁每条通道的召回。

    B 在视觉与词法里都是第二名，两条通道都把它排在各自的 ``limit``（这里是 1）之外，
    但两条通道的召回里都带着它。若按 ``limit`` 裁通道召回，候选池只有视觉冠军 A 与
    词法冠军 C，交付的会是 C；不裁则是 B（``2.5/62 + 1.0/62 = 0.05645`` 略高于 C 的
    ``2.5/63 + 1.0/61 = 0.05608``）。1.0 的原话是 "a relevant asset cannot disappear
    merely because another channel ranked it below an arbitrary head"
    （``evidence_retrieval.py:171-176``）。
    """
    result = build(library).execute(EvidenceRequest(text="海边", scope_id=SCOPE, limit=1))

    assert [hit.asset_id for hit in result.hits] == ["asset_b"]
    assert channels_of(result.hits[0]) == {"visual_ann": 2, "lexical": 2}


# ---- 名次与权重对得上基线 --------------------------------------------------------


def test_a_single_channel_hit_keeps_the_visual_weight_from_the_baseline(library):
    """三条通道各自安静时，融合结果与单通道时代逐值相同——``2.5/(60+名次)``。

    每条通道只按 ``rank`` 参与，所以「多接了两条通道」不应该让任何分数漂移。
    """
    result = build(library).execute(EvidenceRequest(text="沙漠", scope_id=SCOPE, limit=3))

    assert [channels_of(hit) for hit in result.hits] == [
        {"visual_ann": 1},
        {"visual_ann": 2},
        {"visual_ann": 3},
    ]
    assert [hit.score for hit in result.hits] == [
        pytest.approx(2.5 / 61),
        pytest.approx(2.5 / 62),
        pytest.approx(2.5 / 63),
    ]


def test_the_metadata_channel_keeps_its_anchor_boost(library):
    """metadata 是 anchor 通道：命中即 ``1.0/(60+名次) + 1.0``。

    A 被三条通道同时命中——地点条件进 metadata、地点锚点进 lexical、查询向量进视觉——
    锚点加成只加一次，不管候选被几条通道召回。这条也顺带钉住「锚点列真的写进了词法
    投影」：锚点没接线的那一版里，lexical 不会出现在这个字典里。
    """
    result = build(library).execute(EvidenceRequest(text="萧山区", scope_id=SCOPE, limit=3, place="萧山区"))

    top = result.hits[0]
    assert top.asset_id == "asset_a"
    assert channels_of(top) == {"metadata": 1, "lexical": 1, "visual_ann": 1}
    assert top.score == pytest.approx(1.0 / 61 + 1.0 / 61 + 2.5 / 61 + 1.0)


def test_metadata_stays_silent_without_a_positive_condition(library):
    """纯内容词查询不该被「scope 内每一张图」当锚点灌进来。

    1.0 ``metadata.py:28-31``。通道仍然被调用，只是它自己返回空——所以结果里既没有
    metadata 名次，也不报错。
    """
    result = build(library).execute(EvidenceRequest(text="海边", scope_id=SCOPE, limit=3))

    assert all("metadata" not in hit.channels for hit in result.hits)


# ---- 缺通道的形态 ----------------------------------------------------------------


def test_every_reported_channel_is_one_of_the_five_wired_ones(library):
    """五条通道在 S6 之后都接上了（entity 是 S5b、text_ann 是 S6 T115）。

    这条原先钉的是「报出来的只能是三条」——S4 时 entity / text_ann 还没有类，
    那条断言是对的。**但它的前提被后续切片推翻了，而它自己不会知道**：
    这次是加必填参数时顺手炸出来的；换成可选参数的话它会静默地继续通过，
    而那时它钉的已经是错的东西。

    所以改成钉**不变量**：报出来的通道名必须是这五条里的。
    「没命中的通道不出现」由
    ``test_metadata_stays_silent_without_a_positive_condition`` 单独钉住，这里不重复。
    """
    result = build(library).execute(EvidenceRequest(text="海边", scope_id=SCOPE, limit=3, place="萧山区"))

    reported = {channel for hit in result.hits for channel in hit.channels}
    assert reported <= {"metadata", "entity", "lexical", "visual_ann", "text_ann"}


def test_the_requested_limit_bounds_only_the_delivered_list(library):
    result = build(library).execute(EvidenceRequest(text="海边", scope_id=SCOPE, limit=2))

    assert len(result.hits) == 2


def test_an_empty_query_asks_nothing_of_any_channel(library):
    result = build(library).execute(EvidenceRequest(text="   ", scope_id=SCOPE, limit=3))

    assert result.hits == ()
    assert result.backend == "none"
    assert result.authoritative_vectors == 0


def test_the_visual_channel_still_reports_its_backend_health(library):
    """降级信息必须穿过编排层——1.0 就是在这一层把它丢进日志的。"""
    result = build(library).execute(EvidenceRequest(text="海边", scope_id=SCOPE, limit=3))

    assert result.backend == "sqlite_exact"
    assert result.degraded is True
    assert result.degraded_reason == "no_index_configured"
    assert result.authoritative_vectors == 3


class _RecordingGraph:
    def __init__(self) -> None:
        self.seeds: tuple[str, ...] = ()

    def execute(self, *, seed_asset_ids, scope_id, limit, question=""):
        self.seeds = tuple(seed_asset_ids)
        return GraphSearchResult(
            hits=(
                CandidateHit(
                    asset_id="asset_graph",
                    channel="graph",
                    rank=1,
                    raw_score=0.8,
                    score_kind="graph_path",
                ),
            )
        )


def test_graph_expansion_uses_primary_head_and_reaches_the_final_hybrid_result(library):
    graph = _RecordingGraph()
    use_case = build(library, graph=graph)

    result = use_case.execute(EvidenceRequest(text="海边", scope_id=SCOPE, limit=2))

    assert graph.seeds == ("asset_b", "asset_c")
    assert [hit.asset_id for hit in result.hits] == ["asset_b", "asset_graph"]
    assert result.graph_asset_ids == ("asset_graph",)
    assert result.primary_asset_ids == ("asset_b", "asset_c")
    assert result.fused_asset_ids == ("asset_b", "asset_graph")


# ---- 通道轨迹（T125：S6 的验收条件）------------------------------------------------


def test_the_real_wiring_reports_a_trace_for_every_channel(library):
    """**真接线上的那一半。** 桩件那半边（``tests/unit/test_channel_trace.py``）验的是
    失败与降级两条路；这一条验的是**五条真通道真的各留下了一条轨迹**。

    没有它的话，「五路都在跑」这件事就只有一个用桩件搭出来的证明，
    而桩件里通道是随便接的——接错一条、漏接一条，那边都不会知道。
    """
    result = build(library).execute(EvidenceRequest(text="海边", scope_id=SCOPE, limit=3))

    assert [trace.channel for trace in result.channel_trace] == [
        "metadata",
        "entity",
        "lexical",
        "visual_ann",
        "text_ann",
    ]
    assert all(trace.status in {"ok", "degraded", "error"} for trace in result.channel_trace)


def test_a_real_degraded_channel_says_so_and_still_reports(library):
    """这个夹具里没有 Qdrant 索引，所以视觉通道**降级**而不是失败——
    「跑了、只是换了个后端」与「没跑」在 trace 里必须长得不一样。"""
    result = build(library).execute(EvidenceRequest(text="海边", scope_id=SCOPE, limit=3))

    visual = next(item for item in result.channel_trace if item.channel == "visual_ann")
    assert visual.status == "degraded"
    assert visual.reason == "no_index_configured"
    assert visual.backend == "sqlite_exact"
    assert visual.candidate_count > 0, "降级不等于没结果"


def test_a_real_silent_channel_is_distinguishable_from_a_degraded_one(library):
    """**这一条把 S6 的验收条件钉在真接线上。**

    同一次检索里，``metadata`` 没有任何正向条件所以它**跑了但一条都没给**（ok / 0 条），
    而 ``visual_ann`` 是**降级了但给了候选**。两者的候选表现完全不同，
    而 trace 把「跑了没找到」与「跑了但后端换了」也分得开——那正是这一格存在的理由。
    """
    result = build(library).execute(EvidenceRequest(text="海边", scope_id=SCOPE, limit=3))

    by_channel = {trace.channel: trace for trace in result.channel_trace}
    assert by_channel["metadata"].status == "ok"
    assert by_channel["metadata"].candidate_count == 0
    assert by_channel["visual_ann"].status == "degraded"
    assert by_channel["visual_ann"].candidate_count > 0
