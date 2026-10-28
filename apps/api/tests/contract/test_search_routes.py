"""`GET /api/v1/search` 的契约测试。

嵌入器换成脚本化的假件（向量由测试直接指定），这样「哪张图该排第一」是构造出来的，
而不是碰运气撞上模型输出。真模型 + 真照片的验收在 tests/e2e（Task 33）。
"""

from __future__ import annotations

import dataclasses
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from sentrix.domain.media.ports import ObservationSemantics
from sentrix.domain.retrieval import scheme

RED = [1.0, 0.0, 0.0, 0.0]
GREEN = [0.0, 1.0, 0.0, 0.0]


class ScriptedEmbedder:
    """文本 -> 指定向量；图片 -> 由内容映射到指定向量。"""

    model_id = scheme.IMAGE_MODEL
    dimension = 4

    def __init__(self, images: dict[bytes, list[float]], texts: dict[str, list[float]]) -> None:
        self._images = images
        self._texts = texts

    def embed_image(self, path: Path) -> list[float]:
        return list(self._images.get(Path(path).read_bytes(), [0.0, 0.0, 0.0, 1.0]))

    def embed_query(self, text: str) -> list[float]:
        return list(self._texts.get(text.strip(), [0.0, 0.0, 0.0, 1.0]))


def jpeg_bytes(color: str) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), color).save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.fixture
def scripted_client(isolated_app) -> TestClient:
    red, green = jpeg_bytes("red"), jpeg_bytes("green")
    app = isolated_app
    app.state.container = dataclasses.replace(
        app.state.container,
        visual_embedder=ScriptedEmbedder(images={red: RED, green: GREEN}, texts={"红色": RED, "绿色": GREEN}),
    )
    return TestClient(app)


def import_photo(client: TestClient, payload: bytes, scope_id: str = "album1") -> str:
    return client.post(
        "/api/v1/media/import",
        files={"file": ("a.jpg", payload, "image/jpeg")},
        data={"scope_id": scope_id},
    ).json()["asset_id"]


def test_search_ranks_the_matching_photo_first(scripted_client: TestClient):
    red, green = jpeg_bytes("red"), jpeg_bytes("green")
    red_id = import_photo(scripted_client, red)
    green_id = import_photo(scripted_client, green)

    response = scripted_client.get("/api/v1/search", params={"q": "红色", "scope_id": "album1"})

    assert response.status_code == 200
    body = response.json()
    # 召回不做阈值截断：低相似度的候选仍然在列表里，只是排在后面。要的是名次，
    # 不是「有没有命中」——S2 没有校准层，凭一个阈值判断相关性只会误伤。
    assert [item["asset"]["id"] for item in body["items"]] == [red_id, green_id]
    assert body["backend"] == "qdrant"
    assert body["degraded"] is False
    assert body["authoritative_vectors"] == 2


def test_the_other_word_ranks_the_other_photo_first(scripted_client: TestClient):
    red, green = jpeg_bytes("red"), jpeg_bytes("green")
    import_photo(scripted_client, red)
    green_id = import_photo(scripted_client, green)

    items = scripted_client.get("/api/v1/search", params={"q": "绿色", "scope_id": "album1"}).json()["items"]

    assert items[0]["asset"]["id"] == green_id


def test_search_hit_carries_everything_the_web_needs(scripted_client: TestClient):
    """命中必须带回可打开的原图信息——前端不该再发一次请求才知道图在哪。"""
    import_photo(scripted_client, jpeg_bytes("red"))

    item = scripted_client.get("/api/v1/search", params={"q": "红色", "scope_id": "album1"}).json()["items"][
        0
    ]

    assert item["asset"]["file_name"] == "a.jpg"
    assert item["asset"]["mime_type"] == "image/jpeg"
    assert item["asset"]["status"] == "processed"
    assert item["channels"] == {"visual_ann": 1}
    assert item["raw_scores"]["visual_ann"] == pytest.approx(1.0)
    # 融合分数是加权 RRF（visual 2.5 / k=60），不是原始余弦。
    assert item["score"] == pytest.approx(2.5 / 61)


def test_search_is_scoped(scripted_client: TestClient):
    red = jpeg_bytes("red")
    album1_id = import_photo(scripted_client, red, scope_id="album1")
    import_photo(scripted_client, red, scope_id="album2")

    items = scripted_client.get("/api/v1/search", params={"q": "红色", "scope_id": "album1"}).json()["items"]

    assert [item["asset"]["id"] for item in items] == [album1_id]


def test_search_on_an_empty_library_is_empty_and_not_degraded(scripted_client: TestClient):
    body = scripted_client.get("/api/v1/search", params={"q": "红色"}).json()

    assert body["items"] == []
    assert body["degraded"] is False
    assert body["authoritative_vectors"] == 0


def test_unrelated_word_gives_a_stable_ranking_without_a_relevance_claim(
    scripted_client: TestClient,
):
    """S2 **没有**相关性阈值：一个毫不相干的词照样返回排好序的候选。

    这条是已知边界，不是缺陷：1.0 的校准层（NeutralProbe / probe per-space 阈值）
    属于 S6。在它到位之前，宁可给出「按相似度排好的列表」，也不要凭一个拍脑袋的
    阈值假装判断了相关性。这里只钉住两件事：不报降级、两次结果一致。
    """
    import_photo(scripted_client, jpeg_bytes("red"))

    first = scripted_client.get("/api/v1/search", params={"q": "完全不相关的词"}).json()
    second = scripted_client.get("/api/v1/search", params={"q": "完全不相关的词"}).json()

    assert first["backend"] == "qdrant"
    assert first["degraded"] is False
    assert [item["asset"]["id"] for item in first["items"]] == [
        item["asset"]["id"] for item in second["items"]
    ]


def test_scores_are_non_increasing(scripted_client: TestClient):
    red, green = jpeg_bytes("red"), jpeg_bytes("green")
    import_photo(scripted_client, red)
    import_photo(scripted_client, green)

    items = scripted_client.get("/api/v1/search", params={"q": "红色", "scope_id": "album1"}).json()["items"]

    scores = [item["score"] for item in items]
    assert scores == sorted(scores, reverse=True)


def test_blank_query_is_rejected(scripted_client: TestClient):
    assert scripted_client.get("/api/v1/search", params={"q": ""}).status_code == 422
    assert scripted_client.get("/api/v1/search").status_code == 422


def test_limit_is_bounded(scripted_client: TestClient):
    assert scripted_client.get("/api/v1/search", params={"q": "红色", "limit": 0}).status_code == 422
    assert scripted_client.get("/api/v1/search", params={"q": "红色", "limit": 1000}).status_code == 422


# ---- 三条通道进同一个 fuse -------------------------------------------------------


class WordObserver:
    """确定性观察器：文件名里的词就是这张图的语义。

    契约测试要验的是「词法通道有没有进融合」，不是模型——语义由测试直接指定，
    真链路的验收在 tests/e2e。**单张上传走快路径不接观察器**，所以词法通道要靠整册
    导入喂（``media.py`` 的 ``build_import_assets(observe=...)``）。
    """

    model_id = "scripted-vlm"

    def observe(self, path: Path) -> ObservationSemantics:
        word = Path(path).stem
        return ObservationSemantics(caption=word, place=word, activity=word, objects=(word,), confidence=0.65)


class CaptionEmbedder(ScriptedEmbedder):
    def embed_query(self, text: str) -> list[float]:
        # Ingested semantic text contains labels/newlines. Give the two captions
        # distinct vectors so their text ranks cannot depend on random asset IDs.
        if "海边" in text:
            return list(GREEN)
        if "雪山" in text:
            return list(RED)
        return super().embed_query(text)


@pytest.fixture
def album(tmp_path: Path) -> Path:
    directory = tmp_path / "album"
    directory.mkdir()
    (directory / "海边.jpg").write_bytes(jpeg_bytes("red"))
    (directory / "雪山.jpg").write_bytes(jpeg_bytes("green"))
    return directory


@pytest.fixture
def channel_client(isolated_app, album: Path) -> TestClient:
    """视觉把「雪山」排第一（查询词映射到绿色向量），词法把「海边」排第一。"""
    red, green = jpeg_bytes("red"), jpeg_bytes("green")
    # W1 之后图像与文本是**同一个嵌入器**（同一个空间），导入期写文本向量走的也是
    # 这一槽。**此前这里只换了 ``visual_embedder``**，``text_embedder`` 一直是真
    # WeMM 侧车（8102）——侧车不在时 ``text_ann`` 通道就是空的，下面那条「三路融合」
    # 的用例会红，而它红的理由与代码无关，只是那台机器上没起侧车。
    embedder = CaptionEmbedder(images={red: RED, green: GREEN}, texts={})
    app = isolated_app
    app.state.container = dataclasses.replace(
        app.state.container,
        visual_embedder=embedder,
        text_embedder=embedder,
        vlm_observer=WordObserver(),
    )
    client = TestClient(app)
    response = client.post(
        "/api/v1/imports", json={"directory": str(album), "scope_id": "album1", "limit": 10}
    )
    assert response.status_code == 201, response.text
    assert response.json()["imported"] == 2, response.text
    return client


def test_the_lexical_channel_is_fused_into_the_endpoint(channel_client: TestClient):
    """词法把视觉排在第二的那张顶到第一：``1.0/61 + 2.5/62 > 2.5/61``。

    没有这条通道时这里是「雪山.jpg」在第一位——``test_search_ranks_the_matching_photo_first``
    钉的就是单通道时的形状。
    """
    items = channel_client.get("/api/v1/search", params={"q": "海边", "scope_id": "album1"}).json()["items"]

    assert items[0]["asset"]["file_name"] == "海边.jpg"
    # S6 T115 之后 text_ann 也在这条链上了——导入时写文本向量（T115b），
    # 而这个夹具正是**通过 HTTP 导入**的，所以第三条通道是端到端长出来的，
    # 不是桩出来的。它的权重 0.5 也进了分数。
    assert items[0]["channels"] == {"visual_ann": 2, "lexical": 1, "text_ann": 1}
    assert items[0]["score"] == pytest.approx(1.0 / 61 + 2.5 / 62 + 0.5 / 61)


def test_the_endpoint_reports_only_the_channels_that_have_hits(channel_client: TestClient):
    """报出来的通道必须是**真命中了的**那几条，不出现恒空的占位通道。

    **这条原先钉的是「只有两条，因为 entity / text_ann 还没有类」。** S5b 补了
    entity、S6 T115 补了 text_ann，那个前提就过期了——而断言自己不会知道：
    它只会在有人顺手加参数时炸一下（这次就是），或者更糟，一直静默地钉着错的东西。

    改成钉不变量：没召回信号的通道不出现（没有人物条件的 entity、没有正向结构化
    条件的 metadata），命中了的必须在。
    """
    items = channel_client.get("/api/v1/search", params={"q": "海边", "scope_id": "album1"}).json()["items"]

    reported = {channel for item in items for channel in item["channels"]}
    assert reported == {"visual_ann", "lexical", "text_ann"}
    assert "entity" not in reported, "没有人物条件时 entity 通道不该有召回信号"
    assert "metadata" not in reported, "没有正向结构化条件时 metadata 通道不该有召回信号"


def test_search_without_a_scope_covers_every_album(scripted_client: TestClient) -> None:
    """**空 scope = 全部相册**，与资料库、时间线同一形状，但根因深一层。

    `retrieve_metadata.execute` 收的已经是 `str | None`，却先
    `target_scope = scope_id or DEFAULT_SCOPE_ID`，再拿那个兜底值做**第二次**过滤。
    所以有两处会一起错：兜底值本身，以及用兜底值做的后半段过滤。

    因此断言的是「空 scope 覆盖到两个相册、具体 scope 只覆盖自己的」——
    只断言「有命中」的话，第二次过滤那一半仍然可能是错的。
    """
    red = jpeg_bytes("red")
    album1_id = import_photo(scripted_client, red, scope_id="album1")
    album2_id = import_photo(scripted_client, red, scope_id="album2")

    everything = scripted_client.get("/api/v1/search", params={"q": "红色", "scope_id": ""}).json()
    one_album = scripted_client.get("/api/v1/search", params={"q": "红色", "scope_id": "album1"}).json()

    assert {item["asset"]["id"] for item in everything["items"]} == {album1_id, album2_id}, (
        "空 scope 必须跨两个相册"
    )
    assert [item["asset"]["id"] for item in one_album["items"]] == [album1_id]


def test_search_http_retains_structured_embedding_failure(isolated_app):
    import httpx

    from sentrix.infrastructure.models.legacy import ChineseClipSidecarEmbedder

    isolated_app.state.container = dataclasses.replace(
        isolated_app.state.container,
        visual_embedder=ChineseClipSidecarEmbedder(
            "http://sidecar.invalid",
            client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(429))),
        ),
    )
    response = TestClient(isolated_app).get("/api/v1/search", params={"q": "query"})
    assert response.status_code == 200
    visual = next(row for row in response.json()["channel_trace"] if row["channel"] == "visual_ann")
    assert visual["operational_issue"]["error_code"] == "provider_rejected"
    assert visual["operational_issue"]["http_status"] == 429
