"""PhotoBench 的进程入口：FastAPI 装配 + 后台扫描 ``results/``。

**端口 9597。** 设计文档 §5.1 的端口表给 2.0 的 PhotoBench 定的是 9597；1.0 跑在 8771
（裸进程 + ``scripts/deploy/restart_photobench.sh``，不受 systemd 管）。本切片只把
**服务本身**起得来，进程守护方式归 S11。

**为什么 ``create_app`` 里没有模块级 ``app = create_app()``。** 与 ``apps/api``
``sentrix/main.py`` 同一条理由：模块级实例会让任何 ``import photobench.main``
（pytest 收集、脚本、类型检查）都带上副作用——建目录、读配置、起一个扫描 11 GB 的
线程。进程入口走工厂：``uvicorn --factory photobench.main:create_app``。

**启动扫描为什么在后台线程。** 见 ``application/runs/index.py`` 的实测与理由。一句话：
1.0 因为同步扫完 267 个 ``run.json`` 才绑端口，实测 75 秒不可服务；2.0 先把端口绑上，
扫描进度由 ``GET /api/runs`` 的 ``scan`` 字段暴露出来。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.types import Scope

from photobench import __version__
from photobench.api import errors
from photobench.api.routes import assets as assets_routes
from photobench.api.routes import config as config_routes
from photobench.api.routes import datasets as datasets_routes
from photobench.api.routes import model_profiles as model_profiles_routes
from photobench.api.routes import reports as reports_routes
from photobench.api.routes import runs as runs_routes
from photobench.api.routes import spaces as spaces_routes
from photobench.api.routes import suites as suites_routes
from photobench.bootstrap.container import Container
from photobench.config import Settings

#: 1.0 的 API 前缀是 ``/api``，2.0 改成 ``/api/v1``（S9c · T924，2026-10-07 完成）。
#: 与主 API（``apps/api``）同前缀——PhotoBench 是独立发行包，这一份是它自己的常量。
API_PREFIX = "/api/v1"

log = logging.getLogger("photobench.main")


@dataclass(frozen=True)
class WebDist:
    """前端产物的挂载结果。

    **「收下了但什么都没发生」要看得见**（与 sentrix ``main.py`` 的 ``WebDist``、
    ``/api/runs`` 响应里的 ``scan`` 字段同一条口径）。dist 不存在时不能抛异常——开发时
    只跑 vite 是常态；但那个状态下这个进程**没有界面**，所以结果要留在
    ``app.state.web_dist`` 上、并写一条 warning，而不是悄悄跳过。
    """

    directory: Path
    mounted: bool


class StaticBuild(StaticFiles):
    """PhotoBench 的前端产物，直接挂。

    **没有 SPA 回落，这是有意的。** Web 用 react-router，所以它那边的挂载要回落
    ``index.html``；PhotoBench 前端两个视图用 ``useState`` 切（``web/src/App.tsx``
    开头写着「没有 URL 语义，所以不引 react-router」），UI 状态不进 URL、不存在深链，
    未知路径就该 404。

    **抛的是 starlette 的 ``HTTPException``**（同 sentrix ``main.py``）：StaticFiles
    抛的就是它，而 ``fastapi.HTTPException`` 是它的子类。
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        if scope["method"] not in ("GET", "HEAD"):
            # 挂载点 ``/`` 会接住所有没被 API 路由匹配的路径，而 StaticFiles 对非
            # GET/HEAD 一律回 405——那是挂载**之前**没有的行为（那些路径本来 404）。
            raise HTTPException(status_code=404)
        return await super().get_response(path, scope)


def _mount_web(app: FastAPI, settings: Settings) -> None:
    """把前端产物挂到根。**这一句必须是 ``create_app`` 的最后一步**。

    Starlette 按注册顺序匹配、先注册的先赢，而挂载点在 ``/``、会接住一切没被 API
    匹配的路径——所以 API 路由必须先注册完。这条顺序就是「静态挂载不许吃掉 API 前缀」
    的实现本身。
    """
    directory = settings.web_dist_root
    if not directory.is_dir():
        log.warning(
            "前端产物目录不存在，本进程不提供界面（只有 API）：%s —— 先跑 vite build，"
            "或用 PHOTOBENCH_WEB_DIST_ROOT 指到产物目录",
            directory,
        )
        app.state.web_dist = WebDist(directory=directory, mounted=False)
        return
    app.mount("/", StaticBuild(directory=directory, html=True), name="web")
    app.state.web_dist = WebDist(directory=directory, mounted=True)


def create_app(
    settings: Settings | None = None,
    *,
    container: Container | None = None,
    scan_in_background: bool = True,
) -> FastAPI:
    """建应用。

    ``container`` 与 ``scan_in_background`` 两个参数是给用例留的缝：用例注入假件、
    并且**同步**跑一次扫描，这样断言不必与线程赛跑。
    """
    settings = settings or Settings.load()
    container = container or Container.build(settings)

    app = FastAPI(title="PhotoBench", version=__version__, docs_url=None, redoc_url=None)
    app.state.container = container
    errors.install(app)

    api: APIRouter = APIRouter(prefix=API_PREFIX)
    # 写侧（S9b-3）先于只读侧挂上：``/runs/{id}/judge-prompt`` 这类更具体的路径要与
    # ``/runs/{id}`` 区分开，先挂的一方在同形路径上优先匹配。
    api.include_router(suites_routes.router)
    api.include_router(runs_routes.router)
    # 报告三条与 ``/runs/{id}`` 不同形（多一段路径），挂载顺序不影响匹配；
    # 与写侧同批挂在只读侧之后即可。
    api.include_router(reports_routes.router)
    api.include_router(config_routes.router)
    api.include_router(model_profiles_routes.router)
    # S10a 的展示层四条（1.0 ``do_GET`` 的 ``/api/manifests*`` / ``/api/qa-dataset`` /
    # ``/api/memory-spaces`` / 两条媒体）：路径与上面几条不重叠，挂最后不影响匹配。
    api.include_router(datasets_routes.router)
    api.include_router(spaces_routes.router)
    api.include_router(assets_routes.router)
    app.include_router(api)

    # 静态挂载放最后：见 ``_mount_web`` 的注释——顺序就是「不许吃掉 API 前缀」本身。
    _mount_web(app, settings)

    if scan_in_background:
        threading.Thread(
            target=container.run_index.refresh,
            args=(container.run_store,),
            name="photobench-result-scan",
            daemon=True,
        ).start()
    return app
