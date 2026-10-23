"""装配点必须按端口声明。

与 ``apps/api/tests/unit/test_container_wiring.py`` 是同一条不变量：``Container`` 的字段
如果写成 infrastructure 的具体类，「提供者可替换」就只存在于 ``build()`` 的那一行里——
调用方在类型上看不见，很容易顺手用上协议之外的方法。**结构上换得掉、类型上看不出来，
等于没换。**

``docs/architecture/photobench-module-map.md`` 把这条列成 S9b 建装配层时**必须一并立起来**
的东西，这里就是它。
"""

from __future__ import annotations

import dataclasses
import typing

from photobench.bootstrap.container import Container

#: 显式例外：类型确实落在 ``photobench.infrastructure`` 下，而且**故意**如此。
#: 一个说不出理由的例外名单会把这条不变量重新变成摆设，所以每条都必须写明为什么。
#: 本切片为空——四条适配器（配置 / 结果 / HTTP / 数据集）全都只以端口类型出现在 Container 上。
EXCEPTIONS: dict[str, str] = {}


def _infrastructure_fields() -> list[str]:
    hints = typing.get_type_hints(Container)
    return [
        f"{field.name}: {hints[field.name]}（定义在 {getattr(hints[field.name], '__module__', '?')}）"
        for field in dataclasses.fields(Container)
        if str(getattr(hints[field.name], "__module__", "")).startswith("photobench.infrastructure")
        and field.name not in EXCEPTIONS
    ]


def test_container_fields_are_declared_by_domain_ports() -> None:
    offenders = _infrastructure_fields()
    assert not offenders, (
        "Container 的字段必须按 domain 端口声明，下面这些写成了 infrastructure 的具体类：\n  "
        + "\n  ".join(offenders)
        + "\n要么改成对应的端口类型，要么加进 EXCEPTIONS 并写明理由。"
    )


def test_the_two_provider_factories_are_typed_by_the_domain_protocols() -> None:
    """``lifecycle_provider`` / ``inference_provider`` 的返回类型必须是 S9a 的协议。

    这是「本切片真的按端口装配了运行时提供者」的机械证据——写成
    ``Callable[[str], ManagerLifecycleProvider]`` 就会红在这里。
    """
    hints = typing.get_type_hints(Container)
    assert "LifecycleProvider" in str(hints["lifecycle_provider"])
    assert "InferenceProvider" in str(hints["inference_provider"])


def test_a_run_execution_can_be_built_without_a_manager_or_an_endpoint(tmp_path):
    """**没有管理器、没有端点也要能构造出主链**——「外部端点」是 2.0 的合法运行形态。

    这条用例的来历不是推演：**真机验收时在这里连炸两次**。`run_execution` 的 provider
    工厂是**无条件**构造的，而 1.0 的两条外部路线（云端模型 / 不带 Manager 的
    llama.cpp）本来就不需要 Manager：

    - `ManagerLifecycleProvider("")` 抛 `ValueError("manager URL is required")`；
    - `OpenAICompatibleInferenceProvider("")` 抛 `ValueError("model base URL is required")`。

    **S9b-3 起它收整个 `RunRequest`，并且遥测按主机形态选（`select_runtime_providers`）**。
    所以两个场景改成用**显式端点**表达，而不是靠「空串」——空串会退回配置里的地址，
    而那台机器是不是本机取决于运行用例的机器（`host_nvidia_smi` 还是 `unavailable`），
    一个机器相关的断言不能钉住选择逻辑。
    """
    from photobench.application.run_execution.service import RunRequest
    from photobench.config import APP_ROOT, Settings
    from photobench.infrastructure.runtime.providers import (
        ManagerLifecycleProvider,
        ManagerTelemetryProvider,
        UnavailableLifecycleProvider,
        UnavailableTelemetryProvider,
    )

    container = Container.build(
        Settings(config_root=APP_ROOT / "config", results_root=tmp_path, data_root=tmp_path)
    )
    # 一个**非本机**端点且没有 Manager：生命周期与遥测都是显式占位，推理仍然构造得出来。
    external = container.run_execution(
        RunRequest(
            album_id="a",
            qa_set="q",
            model_profile="m",
            sentrix_url="http://sentrix:9598",
            model_base_url="http://example.invalid:9999/v1",
        )
    )
    assert isinstance(external.lifecycle, UnavailableLifecycleProvider)
    assert isinstance(external.telemetry, UnavailableTelemetryProvider)
    assert external.telemetry_source == "unavailable"
    assert external.inference is not None

    # Manager 配了、端点是本机：生命周期归 Manager，遥测走 Manager 这一路。
    managed = container.run_execution(
        RunRequest(
            album_id="a",
            qa_set="q",
            model_profile="m",
            sentrix_url="http://sentrix:9598",
            manager_url="http://manager:8500",
            model_base_url="http://127.0.0.1:8100/v1",
        )
    )
    assert isinstance(managed.lifecycle, ManagerLifecycleProvider)
    assert isinstance(managed.telemetry, ManagerTelemetryProvider)
    assert managed.telemetry_source == "vllm_manager"


def test_the_settings_dataclass_exposes_the_app_root():
    """``APP_ROOT`` 是「配置在哪」的唯一答案；上面那条用例靠它找到仓库里的 config。"""
    from photobench.config import APP_ROOT

    assert (APP_ROOT / "config").is_dir()
