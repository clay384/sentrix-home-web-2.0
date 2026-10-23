"""``photobench.infrastructure.runtime.apple_telemetry`` 的行为对照。

同样全走假件：``_run_text``（真正起 ioreg / vm_stat / ps / lsof / sysctl 的地方）
被换成一张按命令派发的表，footprint 的 ctypes 调用被换成查表——**用例在 Linux 上
也照样跑**，结果不取决于跑它的机器是不是 Mac。

口径指回 1.0 ``backend/apple_telemetry.py``（基线 2d2cee7c）。
"""

from __future__ import annotations

import pytest

from photobench.infrastructure.runtime import apple_telemetry as apple
from photobench.infrastructure.runtime import providers

MIB = 1048576

VM_STAT = """\
Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               10.
Pages active:                             100.
Pages inactive:                           50.
Pages speculative:                        5.
Pages wired down:                         40.
Pages occupied by compressor:             10.
"""

AGX = (
    '"PerformanceStatistics" = {"Alloc system memory"=1048576,'
    '"Device Utilization %"=25,"In use system memory"=524288}'
)

LSOF = """\
p44429
f13
n*:8091
p21110
f8
n127.0.0.1:8101
p47236
f9
n*:8100
p12051
f10
n*:8771
p999
f3
n*:9999
"""

MACMON_SAMPLE = {
    "gpu_utilization_pct": 12.5,
    "power_draw_w": 4.2,
    "power_scope": "macmon_sys_power",
    "cpu_temperature_c": 41.2,
}


class _FakeMacmon:
    def __init__(self, sample: dict[str, object]) -> None:
        self.sample = sample

    def latest(self) -> dict[str, object]:
        return dict(self.sample)


def make_dispatcher(*, ps: str = "", lsof: str = "", vm_stat: str = VM_STAT):
    """按命令头派发假输出。**没预设的命令直接抛**，避免漏网去起真子进程。"""

    def dispatch(command: list[str], timeout: float = 3) -> str:
        head = command[0]
        if head == "ioreg":
            return AGX
        if head == "vm_stat":
            return vm_stat
        if head == "sysctl":
            return "16384\n" if "hw.pagesize" in command else f"{256 * MIB}\n"
        if head == "ps":
            return ps
        if head == "lsof":
            return lsof
        raise AssertionError(f"用例没有预设这条命令：{command}")

    return dispatch


# ---- 解析层（纯函数）--------------------------------------------------------------


def test_vm_stat_used_memory_is_active_wired_and_compressor() -> None:
    data = apple.parse_vm_stat(VM_STAT, page_size=16384, total_bytes=200 * 16384)
    assert data is not None
    assert data["system_memory_scope"] == "apple_app_wired_compressed"
    assert data["system_memory_used_mib"] == round(150 * 16384 / MIB, 2)
    assert data["system_memory_available_mib"] == round(50 * 16384 / MIB, 2)
    assert data["system_memory_total_mib"] == round(200 * 16384 / MIB, 2)


def test_vm_stat_missing_counter_is_none_not_zero() -> None:
    assert apple.parse_vm_stat("Pages free: 1.\n", page_size=16384) is None
    assert apple.parse_vm_stat(VM_STAT, page_size=0) is None


def test_agx_counters_and_absent_counters() -> None:
    data = apple.parse_agx_performance(AGX)
    assert data["gpu_utilization_pct"] == 25.0
    assert data["gpu_allocated_unified_memory_mib"] == 1.0
    assert data["gpu_in_use_unified_memory_mib"] == 0.5
    assert apple.parse_agx_performance("no counters") == {}


def test_macmon_sample_maps_temperature_power_and_ratio() -> None:
    data = apple.parse_macmon_sample(
        {
            "sys_power": 4.2,
            "cpu_power": 1.5,
            "gpu_power": 0.8,
            "gpu_active_ratio": 0.125,
            "temp": {"cpu_temp_avg": 41.2, "gpu_temp_avg": 38.5},
        }
    )
    assert data["cpu_temperature_c"] == 41.2
    assert data["temperature_c"] == 38.5
    assert data["power_draw_w"] == 4.2
    assert data["power_scope"] == "macmon_sys_power"
    assert data["cpu_power_w"] == 1.5
    assert data["gpu_power_w"] == 0.8
    assert data["gpu_utilization_pct"] == 12.5


def test_macmon_sample_ignores_non_objects() -> None:
    assert apple.parse_macmon_sample([]) == {}
    assert apple.parse_macmon_sample("junk") == {}


def test_listening_ports_parses_lsof_machine_readable_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []

    def fake_run(command: list[str], timeout: float = 3) -> str:
        seen.append(command)
        return LSOF

    monkeypatch.setattr(apple, "_run_text", fake_run)
    found = apple._listening_ports({"8091", "8101", "8100", "8771"})
    assert found == {
        44429: {"8091"},
        21110: {"8101"},
        47236: {"8100"},
        12051: {"8771"},
    }
    # 9999 不在目标端口里，那个 pid 一条都不该出现。
    assert 999 not in found
    assert "-Fpn" in seen[0]


def test_is_apple_silicon_requires_darwin_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(apple.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(apple.platform, "machine", lambda: "arm64")
    assert apple.is_apple_silicon() is True
    monkeypatch.setattr(apple.platform, "machine", lambda: "x86_64")
    assert apple.is_apple_silicon() is False
    monkeypatch.setattr(apple.platform, "system", lambda: "Linux")
    monkeypatch.setattr(apple.platform, "machine", lambda: "aarch64")
    assert apple.is_apple_silicon() is False


def test_process_identity_rules() -> None:
    assert apple._is_related({"listening_ports": ["6333"], "identity": "unrelated"}) is True
    assert apple._is_related({"listening_ports": [], "identity": "python mlx_lm.server"}) is True
    assert apple._is_related({"listening_ports": [], "identity": "random"}) is False

    model_by_port = {"identity": "llama-server --port 8100", "listening_ports": ["8100"]}
    assert apple._is_model_process(model_by_port, "8100") is True
    assert apple._is_model_process(model_by_port, "8101") is False
    assert apple._is_model_process(model_by_port, "") is False
    assert (
        apple._is_model_process({"identity": "python mlx_lm.server", "listening_ports": ["8100"]}, "8100")
        is True
    )
    # 只看命令行那一路**仍然要求是 llama-server**：端口号会出现在别人的参数里。
    assert (
        apple._is_model_process({"identity": "llama-server --port 8100 extra", "listening_ports": []}, "8100")
        is True
    )
    assert (
        apple._is_model_process({"identity": "my-server --port 8100 extra", "listening_ports": []}, "8100")
        is False
    )
    # 端口在听，但名字不在白名单里（既不叫 llama-server / mlx 也不叫 vllm）——不算。
    assert (
        apple._is_model_process({"identity": "python worker.py", "listening_ports": ["8100"]}, "8100")
        is False
    )


def test_apple_keyword_set_stays_the_1_0_one() -> None:
    """Apple 那份常量**刻意不与 providers 共用**：它多一个 "mlx"，且不读环境变量。

    合并两者会悄悄改变 Apple 主机上的进程归属结果（"mlx" 是 Apple 上真实的模型
    进程名）。这条用例把那个差别钉住。
    """
    assert "mlx" in apple.SYSTEM_PROCESS_KEYWORDS
    assert "mlx" not in providers.SYSTEM_PROCESS_KEYWORDS
    # 端口默认值在 S9b-1 跟着 2.0 的端口表改了（8771 → 9597、8091 → 9598）；
    # 这一条改的是数字，不是「不与 providers 共用」那条差别（差别在关键词上）。
    assert {"9597", "9598", "8500", "8501", "8100", "8101", "6333"} == apple.SYSTEM_PROCESS_PORTS


# ---- 提供者 -----------------------------------------------------------------------


@pytest.fixture
def apple_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        apple,
        "_run_text",
        make_dispatcher(
            ps="  100 python benchmark_orchestrator.py\n  200 llama-server --port 8100\n",
            lsof="p100\nf13\nn*:8771\np200\nf8\nn*:8100\n",
        ),
    )
    monkeypatch.setattr(apple, "_MACMON", _FakeMacmon(MACMON_SAMPLE))
    monkeypatch.setattr(apple, "_phys_footprint_bytes", lambda pid: {100: 128 * MIB, 200: 512 * MIB}[pid])
    # 权重映射按进程注入：只有模型进程(pid 200)有 mmap 的 .gguf 页，orchestrator 没有。
    monkeypatch.setattr(apple, "_weight_map_bytes", lambda pid: {200: 2600 * MIB}.get(pid, 0))


def test_gpu_stats_sums_related_footprints_and_reports_the_model(apple_host: None) -> None:
    data = apple.LocalAppleUnifiedTelemetryProvider("http://127.0.0.1:8100").gpu_stats()["data"]
    assert data["gpus"] == [
        {
            "index": 0,
            "memory_unit": "apple_phys_footprint_plus_weight_map_mib",
            "gpu_utilization_pct": 12.5,
            "gpu_allocated_unified_memory_mib": 1.0,
            "gpu_in_use_unified_memory_mib": 0.5,
            "cpu_temperature_c": 41.2,
            "power_draw_w": 4.2,
            "power_scope": "macmon_sys_power",
        }
    ]


def test_process_memory_attributes_model_and_sentrix_separately(apple_host: None) -> None:
    provider = apple.LocalAppleUnifiedTelemetryProvider("http://127.0.0.1:8100")
    data = provider.process_memory()["data"]
    assert data["root_pid"] == 200
    # 模型进程 = footprint 512 + 权重映射 2600；llama.cpp mmap 的 .gguf 页不进
    # footprint，两者相加才是真实占用(1.0 实测：只报 footprint 是真实值的 39%)。
    assert data["model_process_system_memory_used_mib"] == 3112.0
    assert data["model_process_phys_footprint_mib"] == 512.0
    assert data["model_process_weight_map_mib"] == 2600.0
    assert data["model_process_system_memory_scope"] == "apple_phys_footprint_plus_weight_map"
    assert data["process_memory_used_mib"] == 3112.0
    assert data["sentrix_stack_pss_mib"] == 128.0
    assert data["sentrix_stack_pss_scope"] == "apple_phys_footprint_plus_weight_map_excluding_model"
    assert data["product_stack_memory_mib"] == 3240.0
    assert data["benchmark_process_memory_used_mib"] == 3240.0
    assert data["memory_unit"] == "apple_phys_footprint_plus_weight_map_mib"
    assert data["memory_profile"] == {"method": "apple_phys_footprint_plus_weight_map_v1"}
    assert data["system_memory_scope"] == "apple_app_wired_compressed"
    # 基线就是第一次采到的已用量，所以这次 delta 为 0。
    assert data["system_memory_delta_mib"] == 0.0


def test_process_memory_without_a_model_process_writes_no_product_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        apple,
        "_run_text",
        make_dispatcher(
            ps="  100 python benchmark_orchestrator.py\n",
            lsof="p100\nf13\nn*:8771\n",
        ),
    )
    monkeypatch.setattr(apple, "_MACMON", _FakeMacmon({}))
    monkeypatch.setattr(apple, "_phys_footprint_bytes", lambda pid: 128 * MIB)
    monkeypatch.setattr(apple, "_weight_map_bytes", lambda pid: 0)
    data = apple.LocalAppleUnifiedTelemetryProvider("http://127.0.0.1:8100").process_memory()["data"]
    assert "root_pid" not in data
    assert "model_process_system_memory_used_mib" not in data
    assert "product_stack_memory_mib" not in data


def test_two_model_candidates_are_not_guessed_into_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """两个候选进程时选谁都是猜——一个都不报，但 sentrix 那一半仍然照报。"""
    monkeypatch.setattr(
        apple,
        "_run_text",
        make_dispatcher(
            ps=(
                "  100 python benchmark_orchestrator.py\n"
                "  200 llama-server --port 8100\n"
                "  201 llama-server --port 8100\n"
            ),
            lsof="p100\nf13\nn*:8771\np200\nf8\nn*:8100\np201\nf9\nn*:8100\n",
        ),
    )
    monkeypatch.setattr(apple, "_MACMON", _FakeMacmon({}))
    monkeypatch.setattr(
        apple, "_phys_footprint_bytes", lambda pid: {100: 128 * MIB, 200: 512 * MIB, 201: 256 * MIB}[pid]
    )
    monkeypatch.setattr(apple, "_weight_map_bytes", lambda pid: 0)
    data = apple.LocalAppleUnifiedTelemetryProvider("http://127.0.0.1:8100").process_memory()["data"]
    assert "root_pid" not in data
    assert "benchmark_process_memory_used_mib" not in data
    assert data["sentrix_stack_pss_mib"] == 128.0


def test_a_missing_footprint_suppresses_the_sum(monkeypatch: pytest.MonkeyPatch) -> None:
    """相关进程里有一个 footprint 读不到，和就不能写——缺项的和是一个偏小的假数字。"""
    monkeypatch.setattr(
        apple,
        "_run_text",
        make_dispatcher(
            ps="  100 python benchmark_orchestrator.py\n  200 llama-server --port 8100\n",
            lsof="p100\nf13\nn*:8771\np200\nf8\nn*:8100\n",
        ),
    )
    monkeypatch.setattr(apple, "_MACMON", _FakeMacmon({}))
    monkeypatch.setattr(apple, "_phys_footprint_bytes", lambda pid: 512 * MIB if pid == 200 else None)
    monkeypatch.setattr(apple, "_weight_map_bytes", lambda pid: 0)
    data = apple.LocalAppleUnifiedTelemetryProvider("http://127.0.0.1:8100").process_memory()["data"]
    assert "sentrix_stack_pss_mib" not in data


def test_weight_map_only_changes_the_model_process_total(apple_host: None) -> None:
    """权重映射只加在**有 .gguf 映射的进程**上；orchestrator footprint 128 就是 128。

    计费口径的区分力：把 weight_map 误加给所有相关进程的实现在这里会报
    128+2600 而不是 128；product 相应 = 128 + 3112。
    """
    data = apple.LocalAppleUnifiedTelemetryProvider("http://127.0.0.1:8100").process_memory()["data"]
    assert data["sentrix_stack_pss_mib"] == 128.0
    assert data["model_process_weight_map_mib"] == 2600.0
    assert data["model_process_system_memory_used_mib"] == 3112.0
    assert data["product_stack_memory_mib"] == 3240.0


def test_system_memory_reports_unavailable_without_vm_stat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(apple, "_run_text", make_dispatcher(vm_stat=""))
    monkeypatch.setattr(apple, "_MACMON", _FakeMacmon({}))
    provider = apple.LocalAppleUnifiedTelemetryProvider("http://127.0.0.1:8100")
    assert provider.system_memory() == {
        "status": "unavailable",
        "source": "vm_stat",
        "reason": "vm_stat_unavailable",
    }

    monkeypatch.setattr(apple, "_run_text", make_dispatcher())
    monkeypatch.setattr(apple, "_phys_footprint_bytes", lambda pid: 1)
    available = apple.LocalAppleUnifiedTelemetryProvider("http://127.0.0.1:8100").system_memory()
    assert available["status"] == "available"
    assert available["source"] == "vm_stat"
    assert available["data"]["system_memory_scope"] == "apple_app_wired_compressed"


def test_kv_cache_is_not_applicable_on_unified_memory() -> None:
    result = apple.LocalAppleUnifiedTelemetryProvider("http://127.0.0.1:8100").kv_cache()
    assert result == {
        "status": "not_applicable",
        "source": "apple_unified",
        "reason": "unified_memory_has_no_discrete_kv_pool",
    }
