"""静态产物挂载（S11 · T1101）：9597 顺带 serve ``apps/photobench/web/dist``。

PhotoBench 前端**没有 router**（两个视图用 ``useState`` 切，见 ``web/src/App.tsx``），
所以这里**没有 SPA 回落**：未知路径就是 404。夹具让两种口径给出不同答案——同一个 dist
里 ``/`` 回 index.html、``assets/app.js`` 发文件、``/api/v1/runs`` 走 API。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Mount

from photobench.config import Settings
from photobench.main import create_app

PROVIDERS = {"default_provider_id": "cloud", "providers": {"cloud": {"url": "http://j/v1", "model": "m"}}}
TARGETS = {
    "default_target_id": "local",
    "targets": {"local": {"label": "本机", "manager_url": "", "model_base_url": ""}},
}


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    directory = tmp_path / "dist"
    (directory / "assets").mkdir(parents=True)
    (directory / "index.html").write_text("<!doctype html><title>PB</title>", encoding="utf-8")
    (directory / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    return directory


def settings_for(tmp_path: Path, dist_root: Path) -> Settings:
    config_root = tmp_path / "config"
    _write(config_root / "runtime_connection.json", {"sentrix_url": "http://sentrix:9598"})
    _write(config_root / "judge_providers.json", PROVIDERS)
    _write(config_root / "vllm_targets.json", TARGETS)
    (tmp_path / "results").mkdir(exist_ok=True)
    (tmp_path / "datasets").mkdir(exist_ok=True)
    return Settings(
        config_root=config_root,
        results_root=tmp_path / "results",
        data_root=tmp_path / "datasets",
        web_dist_root=dist_root,
    )


def build(tmp_path: Path, dist_root: Path) -> TestClient:
    return TestClient(create_app(settings_for(tmp_path, dist_root), scan_in_background=False))


def test_root_serves_the_built_index(tmp_path: Path, dist: Path):
    response = build(tmp_path, dist).get("/")

    assert response.status_code == 200
    assert "PB" in response.text


def test_a_built_asset_is_served_as_a_file(tmp_path: Path, dist: Path):
    response = build(tmp_path, dist).get("/assets/app.js")

    assert response.status_code == 200
    assert response.text == "console.log(1)"


def test_there_is_no_spa_fallback(tmp_path: Path, dist: Path):
    """这个前端没有 URL 语义，所以未知路径就该 404——回落是 Web 那边的事，别照抄过来。"""
    assert build(tmp_path, dist).get("/runs/whatever").status_code == 404


def test_the_static_mount_does_not_shadow_the_api_prefix(tmp_path: Path, dist: Path):
    """挂载点在 ``/``、会接住一切没被 API 匹配的路径：命中的走 JSON，未命中的也仍是 404。"""
    client = build(tmp_path, dist)

    runs = client.get("/api/v1/runs")
    assert runs.status_code == 200
    assert runs.json()["total"] == 0
    assert client.get("/api/v1/nope").status_code == 404


def test_the_static_mount_is_registered_after_the_api_routes(tmp_path: Path, dist: Path):
    """顺序本身也是断言：挂载点是 ``/``，排在前面就会吃掉一切。"""
    app = create_app(settings_for(tmp_path, dist), scan_in_background=False)

    assert isinstance(app.routes[-1], Mount)


def test_a_missing_dist_still_builds_and_says_so(tmp_path: Path):
    """开发时只跑 vite、从没 ``vite build`` 过：**app 照建**，只是不提供界面。"""
    never_built = tmp_path / "never-built" / "dist"

    app = create_app(settings_for(tmp_path, never_built), scan_in_background=False)

    assert app.state.web_dist.mounted is False
    assert app.state.web_dist.directory == never_built
    assert TestClient(app).get("/").status_code == 404
