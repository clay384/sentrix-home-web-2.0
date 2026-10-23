"""数据集展示层与相册媒体的 HTTP 契约（S10a · T1001 / T1002 的一半）。

**夹具必须让两种口径给出不同答案**（本仓库反复踩过的坑）：数据根里同时放
「有 manifest 的目录」「没有 manifest 的目录」「manifest 缺 ``album_id`` 的目录」，
而且它们**名字的顺序与期望结果的顺序不同**；穿越那几条则真的在数据根之外放一份
``secret.txt``——只断言「抛/404」的话，一个根本不拦的实现同样会因为文件不存在而通过。
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fakes_read import FakeJsonHttpClient
from fastapi.testclient import TestClient

from photobench.application.datasets.service import DatasetCatalog
from photobench.config import Settings
from photobench.main import create_app

PROVIDERS = {"default_provider_id": "cloud", "providers": {"cloud": {"url": "http://j/v1", "model": "m"}}}
TARGETS = {
    "default_target_id": "local",
    "targets": {"local": {"label": "本机", "manager_url": "", "model_base_url": ""}},
}

MANIFEST_A = {
    "album_id": "album-a",
    "album_name": "甲相册",
    "faces": [{"face_id": "1", "canonical_name": "明明", "ref_image": "faces/1.jpg"}],
    "photos": ["photos/pic.jpg"],
    "videos": [],
    "qa_sets": {"qa-1": "qa/one.jsonl", "qa-plain": "qa/plain.jsonl"},
}


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    config_root = tmp_path / "config"
    config_root.mkdir()
    _write(config_root / "runtime_connection.json", {"sentrix_url": "http://sentrix:9598"})
    _write(config_root / "judge_providers.json", PROVIDERS)
    _write(config_root / "vllm_targets.json", TARGETS)

    data_root = tmp_path / "datasets"
    # 目录的创建顺序刻意与期望结果的顺序不同：列表要按**名字**排序（1.0 的 sorted）。
    _write(
        data_root / "missing-qa" / "manifest.json",
        {"album_id": "missing-qa", "album_name": "丙", "qa_sets": {"qa-1": "qa/gone.jsonl"}},
    )
    _write(data_root / "album-b" / "manifest.json", {"album_id": "album-b", "album_name": "乙"})
    _write(data_root / "broken" / "manifest.json", {"album_name": "没有 album_id"})
    (data_root / "no-manifest").mkdir(parents=True)
    (data_root / "README.txt").write_text("不是目录", encoding="utf-8")
    _write(data_root / "album-a" / "manifest.json", MANIFEST_A)
    (data_root / "album-a" / "photos").mkdir(parents=True)
    (data_root / "album-a" / "photos" / "pic.jpg").write_bytes(b"PIC")
    _write(
        data_root / "album-a" / "qa" / "one.jsonl",
        {
            "qa_id": "q1",
            "question": "海边有谁?",
            "retrieval_media_refs": [{"media_type": "image", "media_id": "pic.jpg"}],
        },
    )
    _write(data_root / "album-a" / "qa" / "plain.jsonl", {"qa_id": "q2", "question": "没有媒体"})
    # 数据根**之外**的真文件：只有拦住了穿越的实现才读不到它。
    (tmp_path / "secret.txt").write_text("TOP SECRET", encoding="utf-8")

    results_root = tmp_path / "results"
    results_root.mkdir()

    settings = Settings(config_root=config_root, results_root=results_root, data_root=data_root)
    app = create_app(settings, scan_in_background=False)
    with TestClient(app) as test_client:
        yield test_client


def _with_http(client: TestClient, responses: dict[str, object]) -> FakeJsonHttpClient:
    """把展示层的 ``JsonHttpClient`` 换成假件（其余依赖不变）。"""
    http = FakeJsonHttpClient(responses)
    container = client.app.state.container
    client.app.state.container = dataclasses.replace(
        container,
        datasets=DatasetCatalog(albums=container.albums, http=http, config=container.config),
    )
    return http


# ---- GET /api/manifests ----------------------------------------------------


def test_manifests_lists_only_albums_with_a_usable_manifest(client: TestClient):
    """缺 manifest 的目录、缺 ``album_id`` 的 manifest、以及非目录条目都要消失。

    期望是排序后的三个：``album-a`` / ``album-b`` / ``missing-qa``。夹具里 ``broken``
    （缺 ``album_id``）与 ``no-manifest`` 必须**不在**其中——把 ``manifest["album_id"]``
    换成 ``.get``、或去掉那个 ``except``，它们就会冒出来。
    """
    body = client.get("/api/v1/manifests").json()
    assert [item["album_id"] for item in body["manifests"]] == ["album-a", "album-b", "missing-qa"]
    album_a = body["manifests"][0]
    assert album_a["album_name"] == "甲相册"
    assert (album_a["face_count"], album_a["photo_count"], album_a["video_count"]) == (1, 1, 0)
    assert album_a["qa_sets"] == ["qa-1", "qa-plain"]


def test_manifest_detail_is_the_raw_manifest_not_the_summary(client: TestClient):
    body = client.get("/api/v1/manifests/album-a").json()
    assert body["album_id"] == "album-a"
    # 详情返回的是 manifest 原文：有 faces，没有摘要才有的 face_count。
    assert "faces" in body and "face_count" not in body


def test_an_unknown_manifest_is_a_404_with_the_1_0_message(client: TestClient):
    response = client.get("/api/v1/manifests/nope")
    assert response.status_code == 404
    assert response.json() == {"error": "not found"}


# ---- GET /api/qa-dataset ---------------------------------------------------


def test_qa_dataset_requires_both_parameters(client: TestClient):
    """1.0 ``:7690``：缺一个就 400，文案固定（前端按它提示）。"""
    for query in ("", "?album_id=album-a", "?qa_set=qa-1"):
        response = client.get(f"/api/v1/qa-dataset{query}")
        assert response.status_code == 400
        assert response.json() == {"error": "album_id and qa_set are required"}


def test_qa_dataset_reports_the_three_missing_things_apart(client: TestClient):
    """相册 / qa_set / qa 文件是三条不同的 404——前端对它们的提示不同。"""
    album = client.get("/api/v1/qa-dataset?album_id=nope&qa_set=qa-1")
    assert (album.status_code, album.json()) == (404, {"error": "album not found"})

    qa_set = client.get("/api/v1/qa-dataset?album_id=album-a&qa_set=qa-不存在")
    assert (qa_set.status_code, qa_set.json()) == (404, {"error": "qa_set not found"})

    # ``missing-qa`` 的 manifest 指着一个盘上不存在的 jsonl。
    missing = client.get("/api/v1/qa-dataset?album_id=missing-qa&qa_set=qa-1")
    assert (missing.status_code, missing.json()) == (404, {"error": "qa file not found"})


def test_qa_dataset_resolves_media_against_sentrix(client: TestClient):
    """正常路径：读 jsonl + 打主 API 解析媒体。

    **两处 2.0 的接口差异**在这里一起钉住：路径是 ``/api/v1``（1.0 是 ``/api``），
    资产信封是 ``items``（1.0 是 ``assets``）。
    """
    http = _with_http(
        client,
        {
            "memory-spaces": {
                "spaces": [{"id": "scope-1", "name": "PhotoBench-20260101-000000-album-a-big_model"}]
            },
            "assets": {"items": [{"id": "asset-1", "file_name": "pic.jpg", "media_type": "image"}]},
        },
    )
    response = client.get("/api/v1/qa-dataset?album_id=album-a&qa_set=qa-1&sentrix_url=http://sentrix:9598")
    assert response.status_code == 200
    body = response.json()
    assert body["album_id"] == "album-a" and body["qa_set"] == "qa-1"
    assert body["media_source"] == "sentrix"
    assert body["media_resolution"] == {
        "status": "resolved",
        "scope_id": "scope-1",
        "resolved_count": 1,
        "missing_count": 0,
        "ambiguous_count": 0,
        "total_refs": 1,
    }
    ref = body["items"][0]["retrieval_media_refs"][0]
    assert ref["mapping_status"] == "ok"
    assert ref["asset_id"] == "asset-1"
    assert ref["media_url"] == "http://sentrix:9598/api/v1/assets/asset-1/file"
    # 1.0 这条路用的是**裸** load_jsonl，不叠 qa_set 级 expected_action——多补一个字段
    # 就是响应形状变了。``AlbumSource.qa_rows`` 会补，所以不能走它。
    assert "expected_action" not in body["items"][0]
    assert http.calls[0][0] == "http://sentrix:9598/api/v1/memory-spaces?limit=1000"
    assert http.calls[1][0] == "http://sentrix:9598/api/v1/assets?limit=1000&scope_id=scope-1"


def test_qa_dataset_without_media_refs_does_not_touch_sentrix(client: TestClient):
    """没有媒体引用时**不查主 API**：状态是 ``no_media`` 而不是「查完发现是空的」。

    假件在没预设任何 URL 时会直接失败——所以这条同时证明了「没有发请求」。
    """
    http = _with_http(client, {})
    body = client.get("/api/v1/qa-dataset?album_id=album-a&qa_set=qa-plain").json()
    assert body["media_resolution"] == {
        "status": "no_media",
        "resolved_count": 0,
        "missing_count": 0,
        "ambiguous_count": 0,
    }
    assert http.calls == []


def test_qa_dataset_survives_an_unreachable_sentrix(client: TestClient):
    """主 API 连不上是**状态**（``sentrix_unavailable``），不是 500。1.0 也是如此。"""
    http = _with_http(client, {})
    http.fail_with = RuntimeError("HTTP 502 Bad Gateway: boom")
    body = client.get("/api/v1/qa-dataset?album_id=album-a&qa_set=qa-1").json()
    assert body["media_resolution"]["status"] == "sentrix_unavailable"
    assert body["media_resolution"]["error"] == "HTTP 502 Bad Gateway: boom"
    assert body["media_resolution"]["missing_count"] == 1


# ---- GET /api/albums/{album}/{coll}/{file} ---------------------------------


def test_album_media_serves_the_file(client: TestClient):
    response = client.get("/api/v1/albums/album-a/photos/pic.jpg")
    assert response.status_code == 200
    assert response.content == b"PIC"
    assert response.headers["content-type"] == "image/jpeg"


def test_album_media_blocks_a_traversal_in_the_file_name(client: TestClient):
    """``../../../secret.txt`` 从 ``album-a/photos`` 正好指到数据根之外的那份文件。

    **断言的是「没读到」而不是「没返回 200」**：一个不拦的实现会把 TOP SECRET 发回来，
    而那才是这条用例要拦的事。
    """
    response = client.get("/api/v1/albums/album-a/photos/..%2F..%2F..%2Fsecret.txt")
    assert response.status_code == 404
    assert response.json() == {"error": "media not found"}
    assert b"TOP SECRET" not in response.content


def test_album_media_blocks_a_collection_that_is_not_whitelisted(client: TestClient):
    """``coll`` 只认 photos / faces / videos；``..`` 那一类直接 404。

    ``..`` 要**编码**着发（``%2E%2E``）：httpx 会在发出去之前把明文 ``/../`` 归一化掉，
    那条请求根本到不了路由——那样测的就不是白名单了。
    """
    for coll in ("secret", "%2E%2E", "%2E%2E%2F%2E%2E"):
        response = client.get(f"/api/v1/albums/album-a/{coll}/pic.jpg")
        assert response.status_code == 404
        assert response.json() == {"error": "media not found"}


def test_album_media_is_404_when_the_file_is_absent(client: TestClient):
    response = client.get("/api/v1/albums/album-a/photos/nope.jpg")
    assert (response.status_code, response.json()) == (404, {"error": "media not found"})
