"""端到端验收：导入照片 → 用内容词搜到它。

这一层用**真 wemm（HTTP 侧车 8102）+ 真 Qdrant + 真 SQLite**，走完整 HTTP 链路。
两条不循环的判据：

1. **构造出的 ground truth**：纯色图的颜色由测试自己画出来，期望值来自画图那一侧，
   被测的是模型的文本-图像对齐；
2. **两条独立检索路径互相校验**：Qdrant ANN 的路由结果必须与权威存储上的暴力精确
   检索逐名次一致——两边是两套实现，只有数据没错它们才会一致。

模型质量本身（"桥" 找不找得到桥）按设计 §9.1 由使用者肉眼验收，不在这里假装验证。
"""

from __future__ import annotations

import dataclasses
import io
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import skip_or_fail_on_load_error
from fastapi.testclient import TestClient
from PIL import Image

from sentrix.config import Settings
from sentrix.domain.retrieval import scheme
from sentrix.infrastructure.vector.qdrant_index import QdrantVectorIndex
from sentrix.main import create_app

# Requires real album data, local weights or a running model; run via make test-real.
pytestmark = pytest.mark.real_environment

PHOTO_FIXTURES = Path(__file__).resolve().parents[4] / "data/album3-v2-source/photos"
SCOPE = "album1"

COLOURS = {"红色": (200, 30, 30), "蓝色": (30, 30, 200), "绿色": (30, 180, 60)}

_LOCK_HOLDER = """
import sys, time
from qdrant_client import QdrantClient
client = QdrantClient(path=sys.argv[1])
client.get_collections()
print("ready", flush=True)
time.sleep(60)
"""


def require_visual_embedder(client: TestClient) -> None:
    """视觉嵌入器加载不到就跳过，而不是让断言去背这个锅。

    判据取 **app 自己的**那个嵌入器（``container.visual_embedder``）——既真实，又不
    额外占显存。两个坑都踩过，都记在这里：

    - 原先的守卫查的是**代理判据**（``DEFAULT_CHECKPOINT.is_file()``）：权重文件在、
      显存不够时不跳过，用例在运行期报「导入必须写成视觉向量」，读起来像实现坏了
      （2026-09-26 实测：8100 的 vLLM 占住 24 GB 里的 22 GB，CLIP 在 ``model.to(device)``
      处 OOM）；
    - 改成在这里另建一个 ``ChineseClipVisualEmbedder()`` 探针**反而更糟**：探针自己
      占住显存，app 再建第二个就 OOM，于是守卫不跳、测试照红（实测仍有 3 条红）。
    """
    embedder = client.app.state.container.visual_embedder
    if not embedder.available:
        skip_or_fail_on_load_error(embedder.load_error, what="wemm")


@pytest.fixture
def real_client(settings: Settings) -> TestClient:
    """真容器：真模型、真 Qdrant、独立临时数据目录。"""
    return TestClient(create_app(settings))


def colour_photo(path: Path, colour: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (224, 224), colour).save(buffer, format="JPEG")
    path.write_bytes(buffer.getvalue())
    return buffer.getvalue()


def import_photo(client: TestClient, name: str, payload: bytes) -> str:
    # 守卫必须在 POST **之前**：``ChineseClipVisualEmbedder._load()`` 失败时**不缓存**
    # 失败，所以「import 那一刻失败、紧接着重试却成功」是可能的。放在后面查会读到真值，
    # 断言却拿着过期的 ``indexed: False`` 去红（实测 1 条）。放在前面还有第二个好处：
    # 这一次 ``available`` 会把模型真的加载并缓存住，随后这次 import 看到的是同一个状态。
    require_visual_embedder(client)
    response = client.post(
        "/api/v1/media/import",
        files={"file": (name, payload, "image/jpeg")},
        data={"scope_id": SCOPE},
    )
    assert response.status_code == 201, response.text
    assert response.json()["indexed"] is True, "导入必须写成视觉向量"
    return response.json()["asset_id"]


def search(client: TestClient, word: str, **params) -> dict:
    query = {"q": word, "scope_id": SCOPE}
    query.update(params)
    response = client.get("/api/v1/search", params=query)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("word", sorted(COLOURS))
def test_imported_photo_is_found_by_its_colour(real_client: TestClient, tmp_path: Path, word: str):
    """导入三张纯色图，用颜色词必须搜到对应那一张，且排第一。"""
    ids = {}
    for name, colour in COLOURS.items():
        payload = colour_photo(tmp_path / f"{colour[0]}.jpg", colour)
        ids[name] = import_photo(real_client, f"{colour[0]}.jpg", payload)

    body = search(real_client, word)

    assert body["degraded"] is False
    assert body["backend"] == "qdrant"
    assert body["authoritative_vectors"] == len(COLOURS)
    assert body["items"][0]["asset"]["id"] == ids[word], body


def test_qdrant_ranking_matches_the_exact_scan_on_the_authoritative_store(
    real_client: TestClient, tmp_path: Path
):
    """两条独立实现必须给出同一个名次。

    Qdrant 走 ANN + payload 过滤，权威存储走 SQLite 全表余弦——实现完全不同，
    只有「向量写对了、scope 过滤对了、排序没被融合层弄反」同时成立才会一致。
    """
    if not PHOTO_FIXTURES.is_dir():
        pytest.skip(f"真实照片夹具不可用: {PHOTO_FIXTURES}")
    photos = sorted(PHOTO_FIXTURES.glob("*.jpg"))[:8]
    if len(photos) < 8:
        pytest.skip(f"真实照片不足 8 张: {PHOTO_FIXTURES}")

    ids = {}
    for photo in photos:
        ids[photo.name] = import_photo(real_client, photo.name, photo.read_bytes())

    word = "海边"
    body = search(real_client, word, limit=8)
    api_order = [item["asset"]["id"] for item in body["items"]]

    container = real_client.app.state.container
    query_vector = container.visual_embedder.embed_query(word)
    exact = container.vector_repository.search_exact(
        space=scheme.VISUAL_SPACE,
        vector=query_vector,
        limit=8 * 4,
        scope_id=SCOPE,
        model_name=scheme.IMAGE_MODEL,
    )
    exact_order = [hit.source_id for hit in exact if hit.source_id in ids.values()][:8]

    assert api_order == exact_order, f"ANN 与精确回扫不一致：{api_order} != {exact_order}"
    # 分数是加权 RRF（visual 2.5 / RRK_K 60），不是原始余弦。
    assert body["items"][0]["score"] == pytest.approx(2.5 / 61)
    assert [item["channels"]["visual_ann"] for item in body["items"]] == list(range(1, len(api_order) + 1))


def test_search_is_deterministic(real_client: TestClient, tmp_path: Path):
    payload = colour_photo(tmp_path / "red.jpg", COLOURS["红色"])
    import_photo(real_client, "red.jpg", payload)

    first = search(real_client, "红色")
    second = search(real_client, "红色")

    assert first["items"] == second["items"]


def test_scope_isolation_holds_through_the_whole_chain(real_client: TestClient, tmp_path: Path):
    payload = colour_photo(tmp_path / "red.jpg", COLOURS["红色"])
    album1_id = import_photo(real_client, "red.jpg", payload)
    real_client.post(
        "/api/v1/media/import",
        files={"file": ("red.jpg", payload, "image/jpeg")},
        data={"scope_id": "album2"},
    )

    body = search(real_client, "红色", scope_id="album1")

    assert [item["asset"]["id"] for item in body["items"]] == [album1_id]


def test_locked_index_still_answers_with_the_right_photo(real_client: TestClient, tmp_path: Path):
    """S2 的硬约束端到端版本：Qdrant 目录被别的进程占用时，检索必须仍然给出正确照片，
    并把降级如实报出来（1.0 在这里是静默返回空）。"""
    # 先验明模型环境，再起锁目录子进程；缺侧车时无需创建进程，且 macOS 上
    # 已加载 ONNX/torch 后再 fork 曾在这一行触发段错误。
    require_visual_embedder(real_client)
    locked_dir = tmp_path / "locked-qdrant"
    holder = subprocess.Popen(
        [sys.executable, "-c", _LOCK_HOLDER, str(locked_dir)],
        stdout=subprocess.PIPE,
        text=True,
        # Permit posix_spawn: forking after BLAS/PyTorch started threads can
        # crash the macOS child before exec. Python-created FDs are CLOEXEC.
        close_fds=False,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "ready"

        container = real_client.app.state.container
        real_client.app.state.container = dataclasses.replace(
            container,
            vector_index=QdrantVectorIndex(locked_dir),
            visual_embedder=container.visual_embedder,
        )

        payload = colour_photo(tmp_path / "red.jpg", COLOURS["红色"])
        red_id = import_photo(real_client, "red.jpg", payload)
        colour_photo(tmp_path / "blue.jpg", COLOURS["蓝色"])
        import_photo(real_client, "blue.jpg", (tmp_path / "blue.jpg").read_bytes())

        body = search(real_client, "红色")

        assert body["degraded"] is True
        assert body["backend"] == "sqlite_exact"
        assert "already accessed" in body["degraded_reason"]
        assert body["items"][0]["asset"]["id"] == red_id
        assert body["authoritative_vectors"] == 2
    finally:
        holder.terminate()
        holder.wait(timeout=10)
