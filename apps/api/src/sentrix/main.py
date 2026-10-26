"""FastAPI assembly. Route modules contain transport mapping only."""

from __future__ import annotations

# ruff: noqa: E402
# The ASGI factory is also launched directly, without start_api.sh. Apply the
# process policy before route/container imports can initialize numpy/BLAS.
from sentrix.infrastructure.thread_limits import configure_thread_environment

configure_thread_environment()

import logging
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.types import Scope

from sentrix import __version__
from sentrix.api.errors import domain_error_handler
from sentrix.api.routes import (
    assistant,
    diagnostics,
    facts,
    family,
    health,
    identity,
    insights,
    knowledge,
    maintenance,
    media,
    relationships,
    search,
    spaces,
    timeline,
    videos,
)
from sentrix.bootstrap.container import Container
from sentrix.config import Settings, load_settings
from sentrix.domain.errors import DomainError
from sentrix.infrastructure.thread_limits import apply_process_thread_limits

log = logging.getLogger("sentrix.main")

#: API 的根前缀。``/api`` 下的一切都不许被静态回落接手：``/api/v1/*`` 是公开面，
#: 而 ``/api/health`` 这种 1.0 风格的**无版本**路径也必须继续 404
#: （``tests/contract/test_health.py`` 钉着这两条）。它与 ``/api/v1`` 同源，写根。
API_ROOT = "/api"


@dataclass(frozen=True)
class WebDist:
    """Web 产物的挂载结果。

    **「收下了但什么都没发生」要看得见**（与 ``ImportResponse`` 的
    ``indexed`` / ``placed`` / ``video_probed`` 同一条口径）。dist 不存在时不能抛异常：
    开发时只跑 vite、从没 ``vite build`` 过是常态；但那个状态下这个进程**没有界面**，
    所以结果要留在 ``app.state.web_dist`` 上、并写一条 warning，而不是悄悄跳过。
    """

    directory: Path
    mounted: bool


class SpaStaticFiles(StaticFiles):
    """Web 产物：命中文件就发文件，react-router 的客户端路由回落 ``index.html``。

    **为什么不能只用 ``StaticFiles(html=True)``。** 那个只在**目录**上找 ``index.html``
    （``/`` 行，``/timeline`` 不行）——刷新 ``/timeline`` 或在地址栏直接粘进去时浏览器
    打的是 ``GET /timeline``，没有回落就是 404。SPA 的回落必须自己加。

    **为什么回落要带两个「不」。** 无条件的回落会把两类本该 404 的请求变成 200 的
    HTML：``/api/v1/nope``（API 的未知路径要 JSON 404）与 ``/assets/missing.js``
    （缺一个哈希资源时回一页 HTML，浏览器只会报 ``Unexpected token '<'``，离真正的原因
    很远）。两个「不」由 ``_is_client_route`` 判定，见那里的注释。

    **``except`` 抓的是 starlette 的 ``HTTPException``，不是 ``fastapi`` 那个。**
    后者是前者的**子类**，而 ``StaticFiles.get_response`` 抛的是**基类**——用子类去
    接会一条都接不住（实测：回落整条失效、未知路由仍然 404）。
    """

    def __init__(self, *, directory: Path, api_root: str) -> None:
        # 归一成 StaticFiles 交给 ``get_response`` 的形态：挂载点在 ``/``，
        # 传进来的 path 是相对它的——``/api/v1/nope`` 到这里是 ``api/v1/nope``，没有前导斜杠。
        self._api_root = api_root.strip("/")
        super().__init__(directory=directory, html=True, check_dir=True)

    async def get_response(self, path: str, scope: Scope) -> Response:
        if scope["method"] not in ("GET", "HEAD"):
            # 挂载点 ``/`` 会接住所有没被 API 路由匹配的路径，而 StaticFiles 对非
            # GET/HEAD 一律回 405——那是挂载**之前**没有的行为（那些路径本来 404）。
            # 静态服务不该改变 API 之外任何请求的答复形状。
            raise HTTPException(status_code=404)
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404 or not self._is_client_route(path):
                raise
        # 到这里才回落：确实是一个「不是文件、也不在 API 下」的客户端路由。
        return await super().get_response("index.html", scope)

    def _is_client_route(self, path: str) -> bool:
        if path == self._api_root or path.startswith(f"{self._api_root}/"):
            return False
        # 带扩展名的按「要一个文件」处理；react-router 的路由（``timeline`` /
        # ``people`` / ``imports``）没有扩展名，`vite build` 的资源（``assets/*.js``、
        # ``*.css``、``favicon.svg``）都有。根路径（``.``）本来就走目录分支发 index.html，
        # 到不了这里。
        return not Path(path).suffix


def _mount_web(app: FastAPI, settings: Settings) -> None:
    """把 ``apps/web/dist`` 挂到根。**这一句必须是 ``create_app`` 的最后一步**。

    Starlette 按注册顺序匹配、先注册的先赢，而挂载点在 ``/``、会接住一切没被 API
    匹配的路径——所以 API 路由必须先注册完。这条顺序就是「静态挂载不许吃掉 API 前缀」
    的实现本身。
    """
    directory = settings.web_dist_dir
    if not directory.is_dir():
        log.warning(
            "Web 产物目录不存在，本进程不提供界面（只有 API）：%s —— 先跑 vite build，"
            "或用 SENTRIX_WEB_DIST_DIR 指到产物目录",
            directory,
        )
        app.state.web_dist = WebDist(directory=directory, mounted=False)
        return
    app.mount("/", SpaStaticFiles(directory=directory, api_root=API_ROOT), name="web")
    app.state.web_dist = WebDist(directory=directory, mounted=True)


def create_app(settings: Settings | None = None) -> FastAPI:
    # 先限制算子线程再装配容器：人脸/视觉适配器在 build 时可能建 session，
    # 线程上限必须在它们之前生效（见 infrastructure.thread_limits）。
    apply_process_thread_limits()
    settings = settings or load_settings()
    settings.ensure_directories()

    container = Container.build(settings)
    container.database.migrate()

    app = FastAPI(title="Sentrix Home API", version=__version__, docs_url=None, redoc_url=None)
    app.state.container = container
    app.add_exception_handler(DomainError, domain_error_handler)

    v1 = APIRouter(prefix="/api/v1")
    v1.include_router(health.router)
    v1.include_router(media.router)
    v1.include_router(search.router)
    v1.include_router(assistant.router)
    v1.include_router(diagnostics.router)
    v1.include_router(timeline.router)
    # 视频端点（S8a）。与 timeline 分开：视频那四条有自己的响应形状与血缘读法。
    v1.include_router(videos.router)
    v1.include_router(identity.router)
    v1.include_router(maintenance.router)
    v1.include_router(spaces.router)
    v1.include_router(insights.router)
    v1.include_router(facts.router)
    v1.include_router(knowledge.router)
    v1.include_router(relationships.router)
    v1.include_router(family.router)
    app.include_router(v1)
    # 静态挂载放最后：见 ``_mount_web`` 的注释——顺序就是「不许吃掉 API 前缀」本身。
    _mount_web(app, settings)
    return app


# 这里刻意没有模块级 ``app = create_app()``：它会让任何 ``import sentrix.main``
# （pytest 收集、脚本、类型检查）都带上副作用——在进程 CWD 下建出 data/、跑一次迁移。
# 进程入口走工厂：``uvicorn --factory sentrix.main:create_app``（见 Makefile 的 dev）。
# 理由与 Task 5 的定稿一致；本块曾残留该行（1076a7f 只改了 Task 5，漏改这里）。
