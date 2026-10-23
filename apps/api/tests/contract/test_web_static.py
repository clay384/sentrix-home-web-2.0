"""静态产物挂载（S11 · T1101）：9598 顺带 serve ``apps/web/dist``。

夹具**必须让两种口径给出不同答案**——同一份 dist 里放三种路径：``/`` 与
``timeline`` 这类 react-router 路由回 ``index.html``，``assets/app.js`` 发文件，
``assets/missing.js`` 回 404。少了后两条，「回落」是无条件还是有判据，从断言里看不出来。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Mount

from sentrix.config import Settings
from sentrix.main import create_app


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    directory = tmp_path / "dist"
    (directory / "assets").mkdir(parents=True)
    (directory / "index.html").write_text("<!doctype html><title>INDEX</title>", encoding="utf-8")
    (directory / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    return directory


def build(tmp_path: Path, dist_dir: Path) -> TestClient:
    return TestClient(create_app(Settings(data_dir=tmp_path / "data", web_dist_dir=dist_dir)))


def test_root_serves_the_built_index(tmp_path: Path, dist: Path):
    response = build(tmp_path, dist).get("/")

    assert response.status_code == 200
    assert "INDEX" in response.text


def test_a_react_router_deep_link_falls_back_to_index(tmp_path: Path, dist: Path):
    """刷新 ``/timeline``、或在地址栏直接粘一个客户端路由，浏览器打的就是这条 GET。

    没有 SPA 回落的实现（``StaticFiles(html=True)`` 只在目录上找 index）会在这里 404。
    """
    client = build(tmp_path, dist)

    for route in ("/timeline", "/some/client/route"):
        response = client.get(route)
        assert response.status_code == 200, route
        assert "INDEX" in response.text, route


def test_a_built_asset_is_still_served_as_a_file(tmp_path: Path, dist: Path):
    """反向对照：同一个夹具里 ``assets/app.js`` 必须发文件原文——否则上一条可能只是
    「一切路径都回 index.html」这个更松的实现。"""
    response = build(tmp_path, dist).get("/assets/app.js")

    assert response.status_code == 200
    assert response.text == "console.log(1)"


def test_a_missing_asset_is_404_not_index(tmp_path: Path, dist: Path):
    """缺一个哈希资源回一页 HTML 时，浏览器只报 ``Unexpected token '<'``——离真正的原因很远。"""
    assert build(tmp_path, dist).get("/assets/missing.js").status_code == 404


def test_the_static_mount_does_not_shadow_the_api_prefix(tmp_path: Path, dist: Path):
    """挂载点在 ``/``、会接住一切没被 API 匹配的路径，所以这里要两头都钉：

    - 命中的 API 路由仍然走 JSON（静态挂载没抢在它前面）；
    - **未命中的** ``/api/...`` 仍然 404，不是 ``index.html``（回落有判据，不是无条件）。
    """
    client = build(tmp_path, dist)

    health = client.get("/api/v1/health")
    assert health.status_code == 200
    # **不钉 ``status`` 的字面值**：它现在也取决于嵌入侧车的可用性（2026-10-15 起
    # ``/health`` 把嵌入器并进了状态），而这条测试问的是「API 路由有没有被挂在 ``/``
    # 的静态挂载吃掉」。``database`` 那一格与静态挂载无关，钉它同样是「回的是真 JSON
    # 而不是 index.html」的证据。
    assert health.json()["database"] == "ok"
    assert client.get("/api/v1/nope").status_code == 404
    # 1.0 风格的无版本路径：同样不许被静态回落接手。
    assert client.get("/api/health").status_code == 404


def test_the_static_mount_is_registered_after_the_api_routes(tmp_path: Path, dist: Path):
    """顺序本身也是断言：挂载点是 ``/``，排在前面就会吃掉一切。"""
    app = create_app(Settings(data_dir=tmp_path / "data", web_dist_dir=dist))

    assert isinstance(app.routes[-1], Mount)


def test_a_missing_dist_still_builds_and_says_so(tmp_path: Path):
    """开发时只跑 vite、从没 ``vite build`` 过：**app 照建**，只是不提供界面。"""
    never_built = tmp_path / "never-built" / "dist"

    app = create_app(Settings(data_dir=tmp_path / "data", web_dist_dir=never_built))

    assert app.state.web_dist.mounted is False
    assert app.state.web_dist.directory == never_built.resolve()
    assert TestClient(app).get("/").status_code == 404
