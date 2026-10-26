"""换模型 = 重启 2.0（S9c · T923）的用例。

**两条断言必须都能分辨「实现正确」与「实现错误」**：

- 「新端点进了子进程的环境变量」——这是整条路径的目的。一个只是把进程重起、
  却没把 ``SENTRIX_VLM_BASE_URL`` 传下去的实现，看起来「重启成功了」（8191 健康），
  但模型还是旧的：**这正是最该被抓住的那种假成功**。
- 「先停后起」——顺序颠倒的两个进程会抢同一个端口，第二个起不来，而健康探测
  却可能在前一个的时间窗里回 ok。

真进程原语（spawn / terminate / alive / discover）全部注入，用例里**一个真进程都不起、
一次真网络都不打**。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fakes_execution import FakeHttpClient

from photobench.application.deploy.service import SentrixModelSwitch
from photobench.domain.deploy.ports import RestartOutcome
from photobench.domain.runtime.ports import RunRuntimeSnapshot
from photobench.infrastructure.deploy.sentrix_process import (
    VLM_BASE_URL_ENV,
    SubprocessSentrixProcess,
    _snapshot_config_root,
)

ENDPOINT = "http://127.0.0.1:8100/v1"
OTHER_ENDPOINT = "http://127.0.0.1:8199/v1"


class _StepClock:
    """每次读都前进固定步长的假钟：健康轮询与停机等待都要靠它收敛。

    用真 ``time.monotonic`` 配 ``sleep=lambda _: None`` 会让「等 60 秒」变成一次
    真的忙等——用例会挂住而不是失败。
    """

    def __init__(self, step: float = 1.0) -> None:
        self.t = 0.0
        self.step = step

    def __call__(self) -> float:
        self.t += self.step
        return self.t


class _Harness:
    """把四个进程原语与两个时间源都收成可断言的记录。"""

    def __init__(self, *, alive: list[int] | None = None) -> None:
        self.alive_pids: set[int] = set(alive or [])
        self.spawned: list[tuple[list[str], dict[str, str], Path, Path]] = []
        self.terminated: list[int] = []
        self.discovered: list[int] = []
        self._next_pid = 5000
        self.clock = _StepClock()

    def spawn(self, command: Any, env: Any, cwd: Path, log_path: Path) -> int:
        self.spawned.append((list(command), dict(env), cwd, log_path))
        self._next_pid += 1
        self.alive_pids.add(self._next_pid)
        return self._next_pid

    def terminate(self, pid: int) -> None:
        self.terminated.append(pid)
        self.alive_pids.discard(pid)

    def alive(self, pid: int) -> bool:
        return pid in self.alive_pids

    def discover(self, _port: int) -> list[int]:
        return list(self.discovered)


def _process(harness: _Harness, http: FakeHttpClient, pid_file: Path) -> SubprocessSentrixProcess:
    return SubprocessSentrixProcess(
        api_url="http://127.0.0.1:8191",
        start_command=[".venv/bin/python", "-m", "uvicorn"],
        cwd=Path("/repo"),
        pid_file=pid_file,
        log_path=Path("/repo/logs/sentrix.log"),
        http=http,
        spawn=harness.spawn,
        terminate=harness.terminate,
        alive=harness.alive,
        discover=harness.discover,
        sleep=lambda _seconds: None,
        clock=harness.clock,
    )


# ---- 基础设施实现（真原语全注入） -------------------------------------------


def test_the_new_endpoint_is_exported_to_the_child_environment(tmp_path: Path):
    """**这条路径的目的**：新端点在子进程的环境变量里。

    区分力：一个只重启进程、不传 ``SENTRIX_VLM_BASE_URL`` 的实现让 8191 照样健康
    （它退回 Settings 的默认 8100），而模型其实没换——用例只断言「起来了」的话
    对它是没有区分力的。
    """
    harness = _Harness()
    process = _process(harness, FakeHttpClient([{"status": "ok"}]), tmp_path / "sentrix.json")
    outcome = process.restart(endpoint=ENDPOINT)
    assert outcome.ok is True
    assert outcome.restarted is True
    (command, env, cwd, _log) = harness.spawned[0]
    assert env[VLM_BASE_URL_ENV] == ENDPOINT
    assert command == [".venv/bin/python", "-m", "uvicorn"]
    assert cwd == Path("/repo")


def test_restart_uses_frozen_model_and_embedding_even_after_global_config_changes(tmp_path, monkeypatch):
    global_root = tmp_path / "global"
    global_root.mkdir()
    path = global_root / "runtime_connection.json"
    connection = {
        "model_base_url": ENDPOINT,
        "embedding": {
            "mode": "split_text_image",
            "text_url": "http://127.0.0.1:8101",
            "image_url": "http://127.0.0.1:8103",
        },
    }
    path.write_text(json.dumps(connection))
    snapshot = RunRuntimeSnapshot(path.read_text())
    path.write_text(
        json.dumps(
            {
                "model_base_url": OTHER_ENDPOINT,
                "embedding": {"mode": "unified_multimodal", "wemm_url": "http://other-device:8102"},
            }
        )
    )
    monkeypatch.setenv("PHOTOBENCH_CONFIG_ROOT", str(global_root))
    harness = _Harness()
    process = SubprocessSentrixProcess(
        api_url="http://127.0.0.1:8291",
        start_command=["not-executed"],
        cwd=tmp_path,
        pid_file=tmp_path / "process.json",
        log_path=tmp_path / "api.log",
        http=FakeHttpClient([{"status": "ok"}]),
        runtime_snapshot=snapshot,
        spawn=harness.spawn,
        alive=harness.alive,
        discover=harness.discover,
        terminate=harness.terminate,
        sleep=lambda seconds: None,
        clock=harness.clock,
    )
    assert process.restart(endpoint=ENDPOINT).ok
    env = harness.spawned[0][1]
    frozen_root = Path(env["PHOTOBENCH_CONFIG_ROOT"])
    assert frozen_root != global_root
    assert json.loads((frozen_root / "runtime_connection.json").read_text()) == connection
    # Exercise the real startup export script, which previously overwrote the passed target.
    script = Path(__file__).resolve().parents[3] / "scripts/deploy/runtime_connection_env.py"
    exports = subprocess.check_output([sys.executable, str(script)], env={**os.environ, **env}, text=True)
    assert ENDPOINT in exports and OTHER_ENDPOINT not in exports
    assert "http://127.0.0.1:8101" in exports and "other-device" not in exports
    assert process.running_endpoint() == ENDPOINT
    changed = RunRuntimeSnapshot(json.dumps({**connection, "embedding": {"mode": "no_local_embedding"}}))
    process._runtime_snapshot = changed
    assert process.running_endpoint() == ""  # Same model does not imply the same embedding topology.


def test_restart_passes_frozen_mac_profile_and_cpu_providers(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SENTRIX_DEPLOYMENT_PROFILE", "server")
    monkeypatch.setenv("FACE_PROVIDERS", "CUDAExecutionProvider,CPUExecutionProvider")
    connection = {
        "model_base_url": ENDPOINT,
        "embedding": {
            "mode": "split_text_image",
            "text_url": "http://127.0.0.1:8101",
            "image_url": "http://127.0.0.1:8103",
        },
        "product_settings": {
            "deployment_profile": "mac",
            "agent_wall_time_s": 600.0,
            "chat_timeout_s": 180.0,
            "face_providers": "CPUExecutionProvider",
            "retinaface_providers": "CPUExecutionProvider",
        },
    }
    harness = _Harness()
    process = SubprocessSentrixProcess(
        api_url="http://127.0.0.1:8191",
        start_command=["not-executed"],
        cwd=tmp_path,
        pid_file=tmp_path / "process.json",
        log_path=tmp_path / "sentrix.log",
        http=FakeHttpClient([{"status": "ok"}]),
        runtime_snapshot=RunRuntimeSnapshot(json.dumps(connection)),
        environment={"SENTRIX_TEXT_EMBEDDER_URL": "http://stale:8101"},
        spawn=harness.spawn,
        alive=harness.alive,
        discover=harness.discover,
        terminate=harness.terminate,
        sleep=lambda _seconds: None,
        clock=harness.clock,
    )
    assert process.restart(endpoint=ENDPOINT).ok
    env = harness.spawned[0][1]
    assert env["SENTRIX_DEPLOYMENT_PROFILE"] == "mac"
    assert env["SENTRIX_EMBEDDING_SCHEME"] == "legacy"
    assert env["SENTRIX_TEXT_EMBEDDER_URL"] == "http://127.0.0.1:8101"
    assert env["SENTRIX_LEGACY_CLIP_URL"] == "http://127.0.0.1:8103"
    assert env["FACE_PROVIDERS"] == "CPUExecutionProvider"
    assert env["RETINAFACE_PROVIDERS"] == "CPUExecutionProvider"
    assert env["SENTRIX_AGENT_WALL_TIME_S"] == "600.0"
    assert env["SENTRIX_CHAT_TIMEOUT_S"] == "180.0"


def test_the_recorded_process_reports_the_endpoint_it_was_started_with(tmp_path: Path):
    """pid 记录是「当前端点」的真相来源——第二个实例读得到，才能判「要不要重启」。

    区分力：不写记录的实现在下一次评测里会把「已经指着这里」判成「指着别处」，
    于是每轮都重启一遍主 API。前一条用例抓不到它（它只看环境变量）。
    """
    pid_file = tmp_path / "sentrix.json"
    harness = _Harness()
    first = _process(harness, FakeHttpClient([{"status": "ok"}]), pid_file)
    first.restart(endpoint=ENDPOINT)
    assert first.running_endpoint() == ENDPOINT

    second = _process(harness, FakeHttpClient([]), pid_file)
    assert second.running_endpoint() == ENDPOINT


def test_startup_snapshot_is_published_atomically_and_refuses_modified_content(tmp_path):
    connection = {"model_base_url": ENDPOINT, "embedding": {"mode": "no_local_embedding"}}
    snapshot = RunRuntimeSnapshot(json.dumps(connection))
    with ThreadPoolExecutor(max_workers=8) as pool:
        roots = list(pool.map(lambda _: _snapshot_config_root(snapshot, ENDPOINT, tmp_path), range(24)))
    assert len(set(roots)) == 1
    path = roots[0] / "runtime_connection.json"
    assert json.loads(path.read_text()) == connection
    assert path.stat().st_mode & 0o777 == 0o400
    assert list(roots[0].iterdir()) == [path]
    path.chmod(0o600)
    path.write_text("{}")
    with pytest.raises(OSError, match="modified"):
        _snapshot_config_root(snapshot, ENDPOINT, tmp_path)


def test_the_old_process_is_stopped_before_the_new_one_starts(tmp_path: Path):
    """先停后起，**两个都要停**：pid 记录里那个 + 按端口反查到的那个。

    区分力：只停自己起过的那一个的实现，第一次「换模型」会留下 ``make dev``
    起的旧 8191，新进程在端口上撞车——而它在日志里表现成「起不来」，看不出是漏停。
    """
    pid_file = tmp_path / "sentrix.json"
    pid_file.write_text('{"pid": 111, "endpoint": "http://old:8100/v1"}', encoding="utf-8")
    harness = _Harness(alive=[111, 222])
    harness.discovered = [222]
    process = _process(harness, FakeHttpClient([{"status": "ok"}]), pid_file)
    process.restart(endpoint=ENDPOINT)
    assert sorted(harness.terminated) == [111, 222]
    assert len(harness.spawned) == 1, "先起后停会让两个进程抢同一个端口"


def test_a_process_that_never_becomes_healthy_is_reported_not_swallowed(tmp_path: Path):
    """起不来就如实报 ``ok=False``——「重启了」与「起来了」是两件事。

    区分力：无条件回 ``ok=True`` 的实现会让调用方把一轮跑在旧模型上的评测当成
    「换模型成功」。这一条逼的是 ``_await_health`` 的返回值真的被用上。
    """
    harness = _Harness()
    process = _process(harness, FakeHttpClient([]), tmp_path / "sentrix.json")  # 每次健康探测都失败
    outcome = process.restart(endpoint=ENDPOINT)
    assert outcome.restarted is True
    assert outcome.ok is False
    assert outcome.error


def test_a_pid_record_that_is_gone_reads_as_no_endpoint(tmp_path: Path):
    """进程已经死了 → 当前端点视为「不知道」，而不是照着旧记录说「指着这里」。

    区分力：不查存活的实现在 2.0 崩过一次之后会一直说「已经指着这里」，
    于是再也不重启它——一个安静的死锁。
    """
    pid_file = tmp_path / "sentrix.json"
    pid_file.write_text('{"pid": 999, "endpoint": "http://old:8100/v1"}', encoding="utf-8")
    harness = _Harness()  # 999 不在 alive 里
    process = _process(harness, FakeHttpClient([]), pid_file)
    assert process.running_endpoint() == ""


# ---- 用例层：要不要重启 ------------------------------------------------------


class _RecordingProcess:
    """最小的 ``SentrixProcess`` 替身：只回当前端点，不做任何事。"""

    def __init__(self, current: str) -> None:
        self.current = current
        self.restart_calls: list[str] = []

    def running_endpoint(self) -> str:
        return self.current

    def restart(self, *, endpoint: str, timeout: float = 60) -> RestartOutcome:
        self.restart_calls.append(endpoint)
        return RestartOutcome(
            ok=True, restarted=True, endpoint=endpoint, previous_endpoint=self.current, pid=7
        )


def test_an_endpoint_already_in_place_is_not_restarted():
    """**代价最小的一条路**：端点已经对了就不动进程。

    区分力：把判据写成「总是重启」的实现会让每一轮评测都重启一次主 API——
    而在本用例里它与正确实现只有 ``restarted`` 与 ``restart_calls`` 两处不同。
    """
    process = _RecordingProcess(ENDPOINT)
    outcome = SentrixModelSwitch(process).ensure_endpoint(ENDPOINT)
    assert outcome.restarted is False
    assert outcome.ok is True
    assert outcome.reason == "already_pointing_here"
    assert process.restart_calls == []


def test_a_different_endpoint_restarts_and_reports_the_previous_one():
    process = _RecordingProcess(OTHER_ENDPOINT)
    outcome = SentrixModelSwitch(process).ensure_endpoint(ENDPOINT)
    assert process.restart_calls == [ENDPOINT]
    assert outcome.restarted is True
    assert outcome.previous_endpoint == OTHER_ENDPOINT
    assert outcome.pid == 7


def test_an_empty_endpoint_is_reported_not_guessed():
    """没要求端点就如实报，不猜一个默认值——猜错等于悄悄换了模型。"""
    process = _RecordingProcess("")
    outcome = SentrixModelSwitch(process).ensure_endpoint("   ")
    assert outcome.ok is False
    assert outcome.reason == "no_endpoint_requested"
    assert process.restart_calls == []


def test_the_outcome_keeps_the_1_0_gamma_sync_shape():
    """``to_state`` 必须保留 1.0 的 ``ok`` / ``error`` 两个键——读侧按它们判过没过。"""
    state = RestartOutcome(ok=True, restarted=False, endpoint=ENDPOINT).to_state()
    assert state["ok"] is True
    assert state["error"] is None


def test_the_port_discovery_only_looks_at_listeners(monkeypatch: pytest.MonkeyPatch):
    """按端口反查时**只认监听进程**，不能把客户端一起捞进来。

    **来历不是推演**：S9c 真机验收驱动脚本在 Part A 用自己的 httpx 客户端探过 8191
    的健康，连接留在池里；run 的 ``model_deploy`` 重启 2.0 时，``lsof -ti tcp:8191``
    把那个脚本也列了出来，于是它被自己的重启 SIGTERM 掉（静默，没有堆栈）。
    Web 前端、任何正在调 2.0 的进程都会这样被误杀。

    **区分力**：去掉 ``-sTCP:LISTEN`` 的实现会在这里红。
    """
    import photobench.infrastructure.deploy.sentrix_process as module

    captured: dict[str, list[str]] = {}

    class _Result:
        stdout = "12345\n"

    def fake_run(argv: list[str], **_kwargs: Any) -> _Result:
        captured["argv"] = list(argv)
        return _Result()

    monkeypatch.setattr(module.shutil, "which", lambda _name: "/usr/bin/lsof")
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    assert module._discover_by_port(8191) == [12345]
    assert "-sTCP:LISTEN" in captured["argv"]


@pytest.mark.parametrize("endpoint", ["", "  "])
def test_restart_refuses_an_empty_endpoint_at_the_port_boundary(tmp_path: Path, endpoint: str):
    """端口自己也挡一道：调用方绕过 ``SentrixModelSwitch`` 直接调 ``restart`` 时，
    空端点同样不该起一个「不知道指向哪」的 2.0。"""
    harness = _Harness()
    process = _process(harness, FakeHttpClient([]), tmp_path / "sentrix.json")
    outcome = process.restart(endpoint=endpoint)
    assert outcome.ok is False
    assert harness.spawned == []
