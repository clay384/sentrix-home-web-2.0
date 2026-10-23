"""换模型 = 重启 2.0 的 **systemd --user** 实现（S11 · T1103）的用例。

与 ``test_sentrix_process.py`` 钉的是同一件事（把新端点交给进程、失败要报不要抛），
但进程控制的形状换成了 ``systemctl --user``。**一条真 ``systemctl`` 都不跑**：命令
执行器与时间源全注入，用例钉的是**命令行参数**与**从单元读回来的值**——在 153 上真跑
``systemctl --user restart sentrix-api`` 会起停别人正在用的服务。

两条夹具区分力最关键的断言：

- ``restart`` 发给 ``systemctl`` 的 argv **逐字**是
  ``["systemctl", "--user", "restart", "sentrix-api.service"]``（少 ``--user`` 会打到
  system 级、单元名少后缀会找不到单元，两者都不会报错，只是没生效）。
- ``running_endpoint`` 读的是**单元的有效环境**且单元必须 ``active``——把判据写成
  「读了配置就算」会让一个崩掉的 2.0 永远不被重启。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fakes_execution import FakeHttpClient

from photobench.application.deploy.service import SentrixModelSwitch
from photobench.domain.deploy.ports import RestartOutcome
from photobench.infrastructure.deploy.systemd_process import (
    CommandResult,
    SystemdSentrixProcess,
    _run_systemctl,
)

ENDPOINT = "http://127.0.0.1:8100/v1"
OTHER_ENDPOINT = "http://127.0.0.1:8199/v1"


class _StepClock:
    """每次读都前进固定步长的假钟：健康轮询靠它收敛。"""

    def __init__(self, step: float = 1.0) -> None:
        self.t = 0.0
        self.step = step

    def __call__(self) -> float:
        self.t += self.step
        return self.t


class _Systemctl:
    """``systemctl`` 的替身：记下每一次 argv，``show`` 按请求的属性作答。"""

    def __init__(
        self,
        *,
        active: str = "inactive",
        environment: str = "",
        fail_actions: set[str] | None = None,
        show_rc: int = 0,
    ) -> None:
        self.calls: list[list[str]] = []
        self.active = active
        self.environment = environment
        self.fail_actions = fail_actions or set()
        self.show_rc = show_rc

    def __call__(self, argv: object) -> CommandResult:
        args = [str(item) for item in argv]  # type: ignore[union-attr]
        self.calls.append(args)
        if "show" in args:
            if self.show_rc != 0:
                return CommandResult(self.show_rc, "", "no such unit")
            requested = {a.split("=", 1)[1] for a in args if a.startswith("--property=")}
            lines = []
            if "ActiveState" in requested:
                lines.append(f"ActiveState={self.active}")
            if "Environment" in requested:
                lines.append(f"Environment={self.environment}")
            if "MainPID" in requested:
                lines.append("MainPID=4321")
            return CommandResult(0, "\n".join(lines) + "\n", "")
        action = args[2] if len(args) > 2 else ""
        if action in self.fail_actions:
            return CommandResult(1, "", f"{action}: boom")
        return CommandResult(0, "", "")

    def actions(self) -> list[list[str]]:
        return [call for call in self.calls if "show" not in call]


def _process(
    systemctl: _Systemctl,
    http: FakeHttpClient,
    tmp_path: Path,
    *,
    unit: str = "sentrix-api.service",
) -> SystemdSentrixProcess:
    return SystemdSentrixProcess(
        api_url="http://127.0.0.1:9598",
        unit=unit,
        http=http,
        systemctl=systemctl,
        config_home=tmp_path,
        sleep=lambda _seconds: None,
        clock=_StepClock(),
    )


# ---- 进程控制（真 systemctl 全注入） ----------------------------------------


def test_restart_drives_systemctl_with_the_user_scope_and_full_unit_name(tmp_path: Path):
    """命令行逐字断言：``--user`` 与 ``.service`` 后缀都必须在。

    区分力：漏 ``--user`` 会打到 system 级、漏后缀会找不到单元——两条都不报错，
    只是「重启」根本没发生，而健康探测可能在前一个进程的时间窗里回 ok。
    """
    systemctl = _Systemctl(active="inactive")
    process = _process(systemctl, FakeHttpClient([{"status": "ok"}]), tmp_path, unit="sentrix-api")
    outcome = process.restart(endpoint=ENDPOINT)
    assert systemctl.actions() == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "restart", "sentrix-api.service"],
    ]
    assert outcome.ok is True
    assert outcome.restarted is True
    assert outcome.pid == 4321


def test_restart_exports_the_new_endpoint_into_the_unit_dropin(tmp_path: Path):
    """**这条路径的目的**：新端点进单元的 ``Environment``。

    区分力：只 ``systemctl restart`` 而不写 drop-in 的实现会让 2.0 用旧端点起来，
    健康探测照样通过——**最该被抓住的假成功**。
    """
    systemctl = _Systemctl(active="inactive")
    process = _process(systemctl, FakeHttpClient([{"status": "ok"}]), tmp_path)
    process.restart(endpoint=ENDPOINT)
    dropin = tmp_path / "systemd" / "user" / "sentrix-api.service.d" / "10-sentrix-vlm-endpoint.conf"
    assert f'Environment="SENTRIX_VLM_BASE_URL={ENDPOINT}"' in dropin.read_text(encoding="utf-8")


def test_running_endpoint_reads_the_units_effective_environment(tmp_path: Path):
    """权威记录 = ``systemctl show`` 的有效环境；引号值要能正解出来。"""
    systemctl = _Systemctl(active="active", environment=f'OTHER=1 SENTRIX_VLM_BASE_URL="{ENDPOINT}"')
    process = _process(systemctl, FakeHttpClient([]), tmp_path)
    assert process.running_endpoint() == ENDPOINT


def test_running_endpoint_is_empty_when_the_unit_is_not_active(tmp_path: Path):
    """单元死着时 ``show`` 仍会回配置上的环境——只读配置会让死掉的 2.0 永远不被重启。

    区分力：去掉 ``ActiveState`` 判据的实现在这里会红（它照配置回出端点）。
    """
    systemctl = _Systemctl(active="inactive", environment=f"SENTRIX_VLM_BASE_URL={ENDPOINT}")
    process = _process(systemctl, FakeHttpClient([]), tmp_path)
    assert process.running_endpoint() == ""


def test_running_endpoint_is_empty_when_the_unit_cannot_be_inspected(tmp_path: Path):
    """``systemctl show`` 失败（单元不存在等）→ 「不知道」，不是照假记录跳过重启。"""
    systemctl = _Systemctl(show_rc=1, environment=f"SENTRIX_VLM_BASE_URL={ENDPOINT}")
    process = _process(systemctl, FakeHttpClient([]), tmp_path)
    assert process.running_endpoint() == ""


def test_a_failed_systemctl_restart_is_reported_not_raised(tmp_path: Path):
    """``systemctl restart`` 失败 → ``ok=False``、``error`` 有值、**不抛**。

    区分力：无条件回 ``ok=True`` 的实现会让调用方把「没换成」当成「换成了」；
    抛异常的实现会让整轮评测炸在 ``model_deploy`` 上（端口契约要 ``RestartOutcome``）。
    """
    systemctl = _Systemctl(active="inactive", fail_actions={"restart"})
    process = _process(systemctl, FakeHttpClient([]), tmp_path)
    outcome = process.restart(endpoint=ENDPOINT)
    assert outcome.ok is False
    assert outcome.error
    assert "restart sentrix-api.service" in outcome.error
    # 已经动过（写过 drop-in、试过重启）——不能对外声称「端点本来就对」。
    assert outcome.restarted is True


def test_a_failed_daemon_reload_is_reported_not_raised(tmp_path: Path):
    """``daemon-reload`` 失败同样收成结果——drop-in 不生效时不能假装重启成功。"""
    systemctl = _Systemctl(active="inactive", fail_actions={"daemon-reload"})
    process = _process(systemctl, FakeHttpClient([]), tmp_path)
    outcome = process.restart(endpoint=ENDPOINT)
    assert outcome.ok is False
    assert outcome.error
    assert ["systemctl", "--user", "restart", "sentrix-api.service"] not in systemctl.actions()


def test_a_service_that_never_becomes_healthy_is_reported(tmp_path: Path):
    """起不来就如实报 ``ok=False``——「重启了」与「起来了」是两件事。"""
    systemctl = _Systemctl(active="inactive")
    process = _process(systemctl, FakeHttpClient([]), tmp_path)  # 健康探测每次失败
    outcome = process.restart(endpoint=ENDPOINT)
    assert outcome.restarted is True
    assert outcome.ok is False
    assert outcome.error


@pytest.mark.parametrize("endpoint", ["", "   "])
def test_restart_refuses_an_empty_endpoint_at_the_port_boundary(tmp_path: Path, endpoint: str):
    """空端点不该起一个「不知道指向哪」的 2.0——一次 ``systemctl`` 都不许发。"""
    systemctl = _Systemctl(active="inactive")
    process = _process(systemctl, FakeHttpClient([]), tmp_path)
    outcome = process.restart(endpoint=endpoint)
    assert outcome.ok is False
    assert outcome.restarted is False
    assert systemctl.calls == []


def test_the_default_runner_reports_a_missing_systemctl(monkeypatch: pytest.MonkeyPatch):
    """``systemctl`` 不在盘上时回 127 而不是抛 ``FileNotFoundError``。"""
    import photobench.infrastructure.deploy.systemd_process as module

    def boom(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr(module.subprocess, "run", boom)
    result = _run_systemctl(["systemctl", "--user", "restart", "sentrix-api.service"])
    assert result.returncode == 127
    assert result.stderr


# ---- 用例层：要不要重启（夹具让两种口径给出不同答案） ----------------------------


class _RecordingProcess:
    """最小的 ``SentrixProcess`` 替身：只回当前端点，记下重启请求。"""

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


def test_ensure_endpoint_only_restarts_when_the_endpoint_differs():
    """同一个 ``ensure_endpoint``，两种输入给出不同答案——**去掉判据必红**。

    相同 → 一次 ``restart`` 都不发；不同 → 恰好发一次，且是请求的那个端点。
    """
    same = _RecordingProcess(ENDPOINT)
    assert SentrixModelSwitch(same).ensure_endpoint(ENDPOINT).restarted is False
    assert same.restart_calls == []

    other = _RecordingProcess(OTHER_ENDPOINT)
    assert SentrixModelSwitch(other).ensure_endpoint(ENDPOINT).restarted is True
    assert other.restart_calls == [ENDPOINT]


# ---- 装配点：两种部署形态按配置选 -------------------------------------------


def test_the_container_selects_the_deploy_form_by_the_service_unit(tmp_path: Path):
    """``sentrix_service_unit`` 留空 = 裸进程，填了 = systemd。两种形态都留下。"""
    from photobench.bootstrap.container import Container
    from photobench.config import APP_ROOT, Settings
    from photobench.infrastructure.deploy.sentrix_process import SubprocessSentrixProcess

    base = {"config_root": APP_ROOT / "config", "results_root": tmp_path, "data_root": tmp_path}
    bare = Container.build(Settings(**base)).sentrix_process
    assert isinstance(bare, SubprocessSentrixProcess)

    managed = Container.build(Settings(**base, sentrix_service_unit="sentrix-api.service")).sentrix_process
    assert isinstance(managed, SystemdSentrixProcess)
