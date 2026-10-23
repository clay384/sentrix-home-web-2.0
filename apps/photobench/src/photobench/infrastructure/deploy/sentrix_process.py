"""``SubprocessSentrixProcess``：用子进程与信号重启 2.0（S9c · T923）。

**这是一个进程管理器，不是一个 HTTP 客户端。** 它知道「2.0 怎么起、用什么环境变量
换模型端点、pid 记在哪、怎么确认它起来了」——**全是部署知识**，也正是 PhotoBench
本不该管、但按用户裁决③又不得不管的那一块。

**与 S11（服务与部署）的重叠，逐条写在这里**（计划书 §1 登记的那条）：S11 要做
「主 API / Web / PhotoBench / model-manager 独立启停」，届时本文件是**唯一要动的地方**
——端口在 ``domain/deploy/ports.py``，判断在 ``application/deploy/service.py``，
进程控制在这里，业务逻辑看不见它们。三条部署假设，S11 接管时逐条复核：

1. **同机 + ``make dev`` 形态**：启动命令默认等于仓库 ``Makefile`` 的 ``dev`` 目标，
   pid 记录与日志落在 ``apps/photobench/logs/`` 下。换形态改 ``Settings`` 的五个
   ``sentrix_*`` 字段（都有环境变量），不改本文件。
2. **PID 归属**：停一个「不是本进程起的」2.0 只能靠 ``lsof`` 按端口反查（见
   ``_discover_by_port``）——那是尽力而为，不是权威。S11 若给主 API 配了 pidfile /
   systemd 单元，应换成读那一个。
3. **健康判据**：只看 ``GET /api/v1/health`` 回不回 ``ok``/``degraded``。**不看模型
   端点**——2.0 自己不管理模型（设计口径），模型通不通是 ``model_deploy`` 相位的事。

**为什么端点是「环境变量」而不是别的**：2.0 的 ``Settings``（``apps/api/src/sentrix/
config.py``）用 ``env_prefix="SENTRIX_"`` 读 ``SENTRIX_VLM_BASE_URL``，那是**启动时**
读死的（``Container.build`` 里 ``ServedModel(settings.vlm_base_url)``）。所以「换模型」
在 2.0 里只能是「换一个环境变量再起一个进程」。
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from urllib.parse import urlparse

from photobench.domain.deploy.ports import RestartOutcome
from photobench.domain.http.ports import JsonHttpClient

#: 2.0 读模型端点的环境变量。名字由 ``sentrix.config.Settings`` 的
#: ``env_prefix="SENTRIX_"`` + 字段名 ``vlm_base_url`` 决定，**不是随口起的**——
#: 改名会让 2.0 悄悄退回默认的 8100 端点（不报错、只是打错模型）。
VLM_BASE_URL_ENV = "SENTRIX_VLM_BASE_URL"

#: 健康路径。2.0 的 ``main.py`` 把路由挂在 ``/api/v1`` 下，``health`` 路由的路径是
#: ``/health``，所以完整路径是这一条（1.0 是 ``/api/health``）。
HEALTH_PATH = "/api/v1/health"

_SpawnFn = Callable[[Sequence[str], Mapping[str, str], Path, Path], int]
_TerminateFn = Callable[[int], None]
_AliveFn = Callable[[int], bool]
_DiscoverFn = Callable[[int], list[int]]


def _spawn(command: Sequence[str], env: Mapping[str, str], cwd: Path, log_path: Path) -> int:
    """起一个**脱离本进程会话**的子进程（``start_new_session``）：PhotoBench 自己
    被停掉时不该顺手把 2.0 带走。stdout/stderr 追加到日志文件，**不经 shell**。"""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("ab")
    try:
        process = subprocess.Popen(  # 命令来自部署配置，不经 shell
            list(command),
            cwd=str(cwd),
            env=dict(env),
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        # 父进程这一份句柄要关掉，否则日志文件永远差最后一个关闭者（子进程自己有一份）。
        handle.close()
    return process.pid


def _terminate(pid: int) -> None:
    """SIGTERM——给 uvicorn 一个收尾的机会（它会等在飞的请求）。"""
    os.kill(pid, signal.SIGTERM)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # 存在但不是我们能碰的——**存在**
        return True
    return True


def _discover_by_port(port: int) -> list[int]:
    """按端口反查**监听**进程（尽力而为）。

    **为什么需要它**：``make dev`` 起的那个 2.0 不是 PhotoBench 起的，pid 记录里没有它。
    没有这一步，第一次「换模型」就停不掉旧进程（新进程会在端口上撞车）。``lsof`` 不在
    盘上时回空表——**这不是致命错误**，只是退回「只停自己起过的那些」。

    **``-sTCP:LISTEN`` 不是保险丝，是必须的。** 不加它，``lsof -ti tcp:PORT`` 会把
    **连着这个端口的客户端**也列出来——httpx 的连接池默认 keep-alive，于是
    PhotoBench 自己、Web 前端、任何正在调 2.0 的进程都会出现在这张表里，被
    ``_terminate`` 一起 SIGTERM 掉。**这是 S9c 真机验收撞出来的**：验收驱动脚本
    在 Part A 用它自己的 httpx 客户端探过 9598 健康，连接留在池里；run 的
    ``model_deploy`` 重启 2.0 时，那份发现表里就有这个脚本，于是它被自己的重启
    静默杀掉（SIGTERM，没有堆栈）。用例
    ``test_the_port_discovery_only_looks_at_listeners`` 钉住这个参数。
    """
    lsof = shutil.which("lsof")
    if lsof is None:
        return []
    try:
        result = subprocess.run(
            [lsof, "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(token) for token in result.stdout.split() if token.strip().isdigit()]


class SubprocessSentrixProcess:
    """``SentrixProcess`` 的子进程实现。"""

    def __init__(
        self,
        *,
        api_url: str,
        start_command: Sequence[str],
        cwd: Path,
        pid_file: Path,
        log_path: Path,
        http: JsonHttpClient,
        vlm_env_var: str = VLM_BASE_URL_ENV,
        startup_timeout: float = 60.0,
        shutdown_timeout: float = 30.0,
        poll_interval: float = 0.5,
        spawn: _SpawnFn | None = None,
        terminate: _TerminateFn | None = None,
        alive: _AliveFn | None = None,
        discover: _DiscoverFn | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        #: 四个进程原语与两个时间源都可注入。**不是为了「可测试」这个名头**：单元用例
        #: 里真起进程、真发信号、真等 60 秒健康探测，会把一份秒级的用例拖成分钟级，
        #: 而且依赖跑用例那台机器上有没有 9598。注入之后，用例钉的是**调用顺序**与
        #: **传下去的环境变量**——那才是这条路径的正确性所在。
        self._api_url = api_url.rstrip("/")
        self._command = list(start_command)
        self._cwd = cwd
        self._pid_file = pid_file
        self._log_path = log_path
        self._http = http
        self._vlm_env_var = vlm_env_var
        self._startup_timeout = startup_timeout
        self._shutdown_timeout = shutdown_timeout
        self._poll_interval = poll_interval
        self._spawn = spawn or _spawn
        self._terminate = terminate or _terminate
        self._alive = alive or _alive
        self._discover = discover or _discover_by_port
        self._sleep = sleep
        self._clock = clock

    # ---- 端口实现 -----------------------------------------------------------

    def running_endpoint(self) -> str:
        record = self._read_record()
        if record is None:
            return ""
        pid, endpoint = record
        if not self._alive(pid):
            return ""
        return endpoint

    def restart(self, *, endpoint: str, timeout: float = 60) -> RestartOutcome:
        """停旧的、起新的、等到健康。**顺序不能颠倒**：先起后停会让两个进程抢同一个端口。"""
        target = endpoint.strip()
        if not target:
            return RestartOutcome(
                ok=False,
                restarted=False,
                endpoint="",
                reason="no_endpoint_requested",
                error="endpoint is empty",
            )
        previous = self.running_endpoint()
        self._stop_running()
        pid = self._spawn(
            self._command,
            {**os.environ, self._vlm_env_var: target},
            self._cwd,
            self._log_path,
        )
        self._write_record(pid, target)
        ok, error = self._await_health(timeout)
        return RestartOutcome(
            ok=ok,
            restarted=True,
            endpoint=target,
            previous_endpoint=previous,
            pid=pid,
            reason="restarted",
            error=error,
        )

    # ---- 内部 ---------------------------------------------------------------

    def _stop_running(self) -> None:
        """把旧进程停掉：先看 pid 记录，再按端口把「不是我们起的」那一个也捞出来。"""
        pids: list[int] = []
        record = self._read_record()
        if record is not None and self._alive(record[0]):
            pids.append(record[0])
        for pid in self._discover(self._port()):
            if pid not in pids and self._alive(pid):
                pids.append(pid)
        if not pids:
            return
        for pid in pids:
            self._terminate(pid)
        deadline = self._clock() + self._shutdown_timeout
        while self._clock() < deadline:
            if not any(self._alive(pid) for pid in pids):
                break
            self._sleep(self._poll_interval)

    def _await_health(self, timeout: float) -> tuple[bool, str]:
        """轮询 ``/api/v1/health`` 直到 200。``ok`` / ``degraded`` 都算起来了
        （``degraded`` 只表示库有问题，进程是活的——1.0 也是这么判的）。"""
        deadline = self._clock() + timeout
        last_error = ""
        while self._clock() < deadline:
            try:
                body = self._http.request_json(f"{self._api_url}{HEALTH_PATH}", method="GET", timeout=5)
            except Exception as exc:  # noqa: BLE001 —— 还没起来就是连不上，继续等
                last_error = str(exc)
            else:
                if isinstance(body, dict) and body.get("status") in {"ok", "degraded"}:
                    return True, ""
                last_error = f"unexpected health payload: {body!r}"
            self._sleep(self._poll_interval)
        return False, last_error or f"sentrix did not become healthy within {timeout}s"

    def _port(self) -> int:
        parsed = urlparse(self._api_url)
        return parsed.port or (443 if parsed.scheme == "https" else 80)

    def _read_record(self) -> tuple[int, str] | None:
        """pid 记录：``{"pid": int, "endpoint": str}``。**坏文件当没有**——一个写坏的
        记录不该让重启路径整个不可用（与 ``ConfigService`` 对坏 JSON 的口径一致）。"""
        try:
            raw = self._pid_file.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(value, dict):
            return None
        pid, endpoint = value.get("pid"), value.get("endpoint")
        if not isinstance(pid, int) or not isinstance(endpoint, str):
            return None
        return pid, endpoint

    def _write_record(self, pid: int, endpoint: str) -> None:
        self._pid_file.parent.mkdir(parents=True, exist_ok=True)
        self._pid_file.write_text(
            json.dumps({"pid": pid, "endpoint": endpoint}, ensure_ascii=False), encoding="utf-8"
        )
