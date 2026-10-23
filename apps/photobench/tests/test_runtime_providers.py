"""``photobench.infrastructure.runtime.providers`` 的行为对照。

1.0 那边这套 provider 没有自己的测试（只被 benchmark 间接覆盖），所以这里从零补。
**全部走假件**：HTTP 与子进程一次都不真起，``/proc`` 一次都不真读——用例的结果不
取决于跑它的这台机器上有没有 GPU、有没有某个进程。

参数与口径都指回 1.0 ``backend/runtime_providers.py``（基线 2d2cee7c）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fakes import CompletedProcess, FakeHttpx, FakeResponse, FakeSubprocess, path_reader

from photobench.domain.runtime.ports import (
    InferenceProvider,
    LifecycleProvider,
    RuntimeProviders,
    TelemetryProvider,
)
from photobench.infrastructure.runtime import providers
from photobench.infrastructure.runtime.apple_telemetry import LocalAppleUnifiedTelemetryProvider
from photobench.infrastructure.runtime.hardware import read_cpu_temperature_c
from photobench.infrastructure.runtime.providers import (
    HostNvidiaTelemetryProvider,
    LlamaCppTelemetryProvider,
    ManagerLifecycleProvider,
    ManagerTelemetryProvider,
    OllamaTelemetryProvider,
    OpenAICompatibleInferenceProvider,
    OrinLlamaCppTelemetryProvider,
    UnavailableLifecycleProvider,
    UnavailableTelemetryProvider,
    VllmTelemetryProvider,
    create_runtime_providers,
    normalize_openai_base_url,
    normalize_service_url,
)

MEMINFO = "MemTotal:       16384000 kB\nMemAvailable:    8192000 kB\n"


@pytest.fixture
def http(monkeypatch: pytest.MonkeyPatch) -> FakeHttpx:
    fake = FakeHttpx()
    monkeypatch.setattr(providers, "httpx", fake)
    return fake


@pytest.fixture
def shell(monkeypatch: pytest.MonkeyPatch) -> FakeSubprocess:
    fake = FakeSubprocess()
    monkeypatch.setattr(providers, "subprocess", fake)
    return fake


@pytest.fixture
def no_host_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    """把宿主进程表与 nvidia-smi 都摘掉。

    ``_augment_host_process_memory`` 不改参数就会去扫真的 ``/proc``、问真的
    ``nvidia-smi``；本文件的用例要验的是「归属规则」，不是这台机器上跑着什么。
    """
    monkeypatch.setattr(providers, "_host_process_rows", list)
    monkeypatch.setattr(providers, "_query_compute_apps", list)


# ---- URL 归一化 -------------------------------------------------------------------


def test_normalize_service_url_adds_scheme_and_strips_trailing_slash() -> None:
    assert normalize_service_url("127.0.0.1:8100/") == "http://127.0.0.1:8100"
    assert normalize_service_url("https://host:443") == "https://host:443"
    assert normalize_service_url("  ") == ""
    assert normalize_service_url(None) == ""


def test_normalize_openai_base_url_appends_v1_once() -> None:
    assert normalize_openai_base_url("127.0.0.1:8100") == "http://127.0.0.1:8100/v1"
    assert normalize_openai_base_url("http://host:8080/v1/") == "http://host:8080/v1"
    # 已经带版本段（v1 / v2 / V1）的不再追加第二段。
    assert normalize_openai_base_url("http://host:8080/v2") == "http://host:8080/v2"
    assert normalize_openai_base_url("http://host:8080/V1") == "http://host:8080/V1"
    assert normalize_openai_base_url("") == ""


def test_system_process_judgement_can_be_replaced_by_the_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1.0 的语义：设了就用设的（覆盖默认），设成空串等于「没设」→ 回到默认值。"""
    monkeypatch.setenv("PHOTOBENCH_SYSTEM_PROCESS_PORTS", "9999, 8771")
    assert providers._configured_set("PHOTOBENCH_SYSTEM_PROCESS_PORTS", ("1",)) == {"9999", "8771"}
    monkeypatch.setenv("PHOTOBENCH_SYSTEM_PROCESS_PORTS", "")
    assert providers._configured_set("PHOTOBENCH_SYSTEM_PROCESS_PORTS", ("1",)) == {"1"}
    monkeypatch.delenv("PHOTOBENCH_SYSTEM_PROCESS_PORTS", raising=False)
    assert providers._configured_set("PHOTOBENCH_SYSTEM_PROCESS_PORTS", ("1",)) == {"1"}
    # 默认值在 S9b-1 换成 2.0 的端口表（9597/9598），见 providers.py 的常量注释。
    assert "9597" in providers.SYSTEM_PROCESS_PORTS
    assert "llama-server" in providers.SYSTEM_PROCESS_KEYWORDS


# ---- 推理提供者 -------------------------------------------------------------------


def test_inference_provider_rejects_empty_base_url() -> None:
    with pytest.raises(ValueError):
        OpenAICompatibleInferenceProvider("   ")


def test_generic_chat_drops_vllm_extension_and_none_values(http: FakeHttpx) -> None:
    http.on_post = lambda url, **kwargs: FakeResponse({"choices": []})
    provider = OpenAICompatibleInferenceProvider("host:8080", api_mode="generic")
    provider.chat(
        {
            "model": "m",
            "messages": [],
            "stream_options": None,
            "chat_template_kwargs": {"enable_thinking": False},
        }
    )
    body = http.calls[0][2]["json"]
    assert "chat_template_kwargs" not in body
    assert "stream_options" not in body
    assert http.calls[0][1] == "http://host:8080/v1/chat/completions"


def test_vllm_chat_keeps_vllm_extension(http: FakeHttpx) -> None:
    http.on_post = lambda url, **kwargs: FakeResponse({"choices": []})
    provider = OpenAICompatibleInferenceProvider("host:8080", api_mode="vllm")
    provider.chat({"model": "m", "chat_template_kwargs": {"enable_thinking": False}})
    assert "chat_template_kwargs" in http.calls[0][2]["json"]


def test_chat_raises_on_error_status_instead_of_returning_the_body(http: FakeHttpx) -> None:
    http.on_post = lambda url, **kwargs: FakeResponse({"error": "bad"}, status_code=400)
    with pytest.raises(Exception, match="HTTP 400"):
        OpenAICompatibleInferenceProvider("host:8080").chat({"model": "m"})


def test_chat_sends_api_key_only_when_configured(http: FakeHttpx) -> None:
    http.on_post = lambda url, **kwargs: FakeResponse({"choices": []})
    OpenAICompatibleInferenceProvider("host:8080").chat({"model": "m"})
    assert "headers" not in http.calls[0][2] or http.calls[0][2]["headers"] == {}
    OpenAICompatibleInferenceProvider("host:8080", api_key="k").chat({"model": "m"})
    assert http.calls[1][2]["headers"] == {"Authorization": "Bearer k"}


def test_chat_stream_yields_sse_lines(http: FakeHttpx) -> None:
    http.on_stream = lambda url, **kwargs: FakeResponse(text="data: a\n\ndata: b")
    lines = list(OpenAICompatibleInferenceProvider("host:8080").chat_stream({"model": "m"}))
    assert lines == ["data: a", "", "data: b"]


def test_list_models_keeps_only_entries_with_an_id(http: FakeHttpx) -> None:
    http.on_get = lambda url, **kwargs: FakeResponse(
        {"data": [{"id": "model-a"}, {"nope": 1}, "junk", {"id": ""}]}
    )
    result = OpenAICompatibleInferenceProvider("host:8080").list_models()
    assert result["models"] == ["model-a"]
    assert http.calls[0][1] == "http://host:8080/v1/models"


def test_health_prefers_the_models_endpoint(http: FakeHttpx) -> None:
    http.on_get = lambda url, **kwargs: FakeResponse({"data": [{"id": "m"}]})
    result = OpenAICompatibleInferenceProvider("host:8080").health()
    assert result["status"] == "available"
    assert result["source"] == "openai_models"
    assert http.calls == [("GET", "http://host:8080/v1/models", http.calls[0][2])]


def test_health_falls_back_to_health_endpoints(http: FakeHttpx) -> None:
    def scripted(url: str, **kwargs: object) -> FakeResponse:
        if url.endswith("/models"):
            raise RuntimeError("models unavailable")
        if url.endswith("/health"):
            return FakeResponse({"ok": True})
        raise AssertionError(url)

    http.on_get = scripted
    result = OpenAICompatibleInferenceProvider("host:8080").health()
    assert result["source"] == "/health"
    assert result["detail"] == {"ok": True}
    # 回落链是 /health → /api/health，第二个只是一次尝试，不该被走到。
    assert http.urls() == ["http://host:8080/v1/models", "http://host:8080/health"]


def test_health_reports_unavailable_when_every_probe_fails(http: FakeHttpx) -> None:
    http.on_get = lambda url, **kwargs: FakeResponse(None, status_code=503)
    result = OpenAICompatibleInferenceProvider("host:8080").health()
    assert result["status"] == "unavailable"
    assert "503" in result["error"]


def test_token_count_is_none_without_manager_and_checked_with_one(http: FakeHttpx) -> None:
    unmanaged = OpenAICompatibleInferenceProvider("host:8080")
    assert unmanaged.token_count([{"role": "user", "content": "hi"}]) is None

    http.on_post = lambda url, **kwargs: FakeResponse({"prompt_tokens": 12, "max_model_len": 4096})
    managed = OpenAICompatibleInferenceProvider("host:8080", manager_url="manager:8500")
    assert managed.token_count([{"role": "user", "content": "hi"}])["prompt_tokens"] == 12
    assert http.calls[0][1] == "http://manager:8500/tokenize-current"
    assert http.calls[0][2]["json"]["add_generation_prompt"] is True


def test_token_count_rejects_an_empty_budget_response(http: FakeHttpx) -> None:
    """一个「成功但没有预算」的响应必须抛——拿它去截断提示词会静默截错。"""
    http.on_post = lambda url, **kwargs: FakeResponse({"prompt_tokens": 0, "max_model_len": 0})
    managed = OpenAICompatibleInferenceProvider("host:8080", manager_url="manager:8500")
    with pytest.raises(ValueError):
        managed.token_count([{"role": "user", "content": "hi"}])


def test_capabilities_token_count_follows_the_manager(http: FakeHttpx) -> None:
    plain = OpenAICompatibleInferenceProvider("host:8080")
    managed = OpenAICompatibleInferenceProvider("host:8080", manager_url="manager:8500")
    vllm = OpenAICompatibleInferenceProvider("host:8080", api_mode="vllm")
    assert plain.capabilities()["token_count"] is False
    assert managed.capabilities()["token_count"] is True
    assert plain.capabilities()["vllm_extensions"] is False
    assert vllm.capabilities()["vllm_extensions"] is True


# ---- 生命周期 ---------------------------------------------------------------------


def test_manager_lifecycle_paths_and_verbs(http: FakeHttpx) -> None:
    http.on_request = lambda url, **kwargs: FakeResponse({"url": url})
    provider = ManagerLifecycleProvider("manager:8500")
    provider.profiles()
    provider.start({"profile": "a"})
    provider.stop()
    provider.stop({"timeout": 5}, timeout=7)
    provider.state()
    assert [(call[0], call[1]) for call in http.calls] == [
        ("GET", "http://manager:8500/profiles"),
        ("POST", "http://manager:8500/start"),
        ("POST", "http://manager:8500/stop"),
        ("POST", "http://manager:8500/stop"),
        ("GET", "http://manager:8500/state"),
    ]
    # stop 不传 payload 时补 {"timeout": 60}（1.0 的默认值）。
    assert http.calls[2][2]["json"] == {"timeout": 60}
    assert http.calls[3][2]["json"] == {"timeout": 5}
    assert http.calls[3][2]["timeout"] == 7


def test_manager_profiles_accepts_both_shapes(http: FakeHttpx) -> None:
    http.on_request = lambda url, **kwargs: FakeResponse([{"id": "a"}])
    assert ManagerLifecycleProvider("manager:8500").profiles()["profiles"] == [{"id": "a"}]
    http.on_request = lambda url, **kwargs: FakeResponse({"profiles": [{"id": "b"}]})
    assert ManagerLifecycleProvider("manager:8500").profiles()["profiles"] == [{"id": "b"}]


def test_manager_lifecycle_requires_a_url() -> None:
    with pytest.raises(ValueError):
        ManagerLifecycleProvider("   ")


def test_unavailable_lifecycle_reports_not_applicable() -> None:
    provider = UnavailableLifecycleProvider()
    assert provider.state() == {
        "status": "not_applicable",
        "reason": "model_manager_not_configured",
    }
    assert provider.start({"profile": "a"})["status"] == "not_applicable"
    assert provider.stop()["status"] == "not_applicable"
    assert provider.profiles() == {
        "status": "not_applicable",
        "reason": "model_manager_not_configured",
        "profiles": [],
    }


# ---- Manager 遥测 -----------------------------------------------------------------


def test_manager_telemetry_failure_is_non_fatal(http: FakeHttpx) -> None:
    def boom(url: str, **kwargs: object) -> FakeResponse:
        raise RuntimeError("offline")

    http.on_get = boom
    result = ManagerTelemetryProvider("manager:8500").gpu_stats()
    assert result["status"] == "unavailable"
    assert "offline" in result["error"]


def test_manager_kv_cache_reads_metrics_from_process_memory(http: FakeHttpx, no_host_processes: None) -> None:
    http.on_get = lambda url, **kwargs: FakeResponse(
        {"vllm_metrics": {"kv_cache_usage_pct": 42.5}, "root_pid": 7}
    )
    provider = ManagerTelemetryProvider("manager:8500")
    assert provider.kv_cache() == {"status": "available", "data": {"kv_cache_usage_pct": 42.5}}

    http.on_get = lambda url, **kwargs: FakeResponse(None, status_code=500)
    assert ManagerTelemetryProvider("manager:8500").kv_cache()["status"] == "unavailable"


def test_manager_system_memory_is_host_ram_in_mib(
    monkeypatch: pytest.MonkeyPatch, no_host_processes: None
) -> None:
    monkeypatch.setattr(providers, "Path", path_reader({"/proc/meminfo": MEMINFO}))
    data = ManagerTelemetryProvider("manager:8500").system_memory()["data"]
    assert data["system_memory_total_mib"] == 16000
    assert data["system_memory_used_mib"] == 8000
    assert data["system_memory_scope"] == "host_all_processes"


def test_linux_system_memory_reports_unavailable_without_meminfo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(providers, "Path", path_reader({}))
    result = providers._read_linux_system_memory()
    assert result["status"] == "unavailable"
    assert result["source"] == "proc_meminfo"


def test_unavailable_telemetry_reports_not_applicable() -> None:
    provider = UnavailableTelemetryProvider()
    for method in ("gpu_stats", "process_memory", "kv_cache", "system_memory"):
        assert getattr(provider, method)() == {
            "status": "not_applicable",
            "reason": "telemetry_provider_not_configured",
        }


# ---- 本机 NVIDIA 遥测 -------------------------------------------------------------


def test_host_nvidia_never_claims_every_gpu_process_is_the_model(
    http: FakeHttpx, shell: FakeSubprocess, no_host_processes: None
) -> None:
    shell.on_run = lambda command, **kwargs: CompletedProcess()
    provider = HostNvidiaTelemetryProvider(endpoint_url="http://127.0.0.1:8100/v1")
    result = provider.process_memory()
    assert result["status"] == "unavailable"
    assert result["reason"] == "matching_model_process_not_found"


def test_vllm_provider_separates_the_model_from_other_gpu_processes(
    shell: FakeSubprocess, no_host_processes: None
) -> None:
    shell.on_run = lambda command, **kwargs: CompletedProcess(
        "999001, VLLM::EngineCore, 2048\n999002, python, 512\n"
    )
    data = VllmTelemetryProvider().process_memory()["data"]
    assert data["process_memory_used_mib"] == 2048
    assert data["other_processes_memory_mib"] == 512
    assert data["all_processes_memory_mib"] == 2560
    assert data["all_processes_scope"] == "gpu_compute_processes"


def test_framework_providers_carry_their_process_hint() -> None:
    assert LlamaCppTelemetryProvider().process_hint == "llama-server"
    assert OllamaTelemetryProvider().process_hint == "ollama"
    assert VllmTelemetryProvider().process_hint == "vllm"
    assert HostNvidiaTelemetryProvider().process_hint == ""
    assert LlamaCppTelemetryProvider().kv_cache()["runtime_framework"] == "llama.cpp"
    assert OllamaTelemetryProvider().kv_cache()["status"] == "not_applicable"


def test_host_nvidia_gpu_stats_parses_rows_and_adds_cpu_temperature(
    shell: FakeSubprocess, monkeypatch: pytest.MonkeyPatch
) -> None:
    shell.on_run = lambda command, **kwargs: CompletedProcess("0, 55, 4096, 24576, 61, 220.5, 1800\n")
    monkeypatch.setattr(providers, "read_cpu_temperature_c", lambda: 44.5)
    data = HostNvidiaTelemetryProvider().gpu_stats()["data"]
    assert data["gpus"] == [
        {
            "index": 0,
            "gpu_utilization_pct": 55.0,
            "memory_used_mib": 4096.0,
            "memory_total_mib": 24576.0,
            "temperature_c": 61.0,
            "power_draw_w": 220.5,
            "sm_clock_mhz": 1800.0,
        }
    ]
    assert data["cpu_temperature_c"] == 44.5
    assert data["memory_scope"] == "gpu_device"


def test_host_nvidia_gpu_stats_failure_is_reported_not_raised(shell: FakeSubprocess) -> None:
    shell.on_run = lambda command, **kwargs: (_ for _ in ()).throw(OSError("no nvidia-smi"))
    result = HostNvidiaTelemetryProvider().gpu_stats()
    assert result["status"] == "unavailable"
    assert result["source"] == "host_nvidia_smi"
    assert "no nvidia-smi" in result["error"]


# ---- 宿主进程归属（_augment_host_process_memory） ---------------------------------


PROCESSES = [
    {
        "pid": 100,
        "process_name": "python",
        "rss_mib": 100.0,
        "listening_ports": ["8771"],
        "identity": "python benchmark_orchestrator.py",
    },
    {
        "pid": 200,
        "process_name": "VLLM::EngineCore",
        "rss_mib": 200.0,
        "listening_ports": ["8100"],
        "identity": "vllm serve --port 8100",
    },
    {
        "pid": 999,
        "process_name": "other",
        "rss_mib": 999.0,
        "listening_ports": [],
        "identity": "other",
    },
]
COMPUTE = [
    {"pid": 100, "process_name": "python", "used_memory_mib": 11.0},
    {"pid": 200, "process_name": "VLLM::EngineCore", "used_memory_mib": 22.0},
    {"pid": 999, "process_name": "unrelated", "used_memory_mib": 33.0},
]


def test_attribution_sums_related_pss_plus_model_uma_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(providers, "_host_process_rows", lambda: [dict(row) for row in PROCESSES])
    monkeypatch.setattr(providers, "_process_pss_mib", lambda pid: {100: 50.0, 200: 150.0}[pid])
    monkeypatch.setattr(
        providers, "_process_smaps_field_mib", lambda pid, field: {100: 100.0, 200: 200.0}[pid]
    )
    data = providers._augment_host_process_memory(
        {"root_pid": 200}, process_hint="vllm", model_pids={200}, compute_apps=COMPUTE
    )
    assert data["model_process_system_memory_used_mib"] == 200.0
    # 50（sentrix PSS）+ 150（模型 PSS）+ max(0, 200-200)（UMA 补偿）= 200
    assert data["benchmark_process_memory_used_mib"] == 200.0
    assert data["benchmark_process_memory_scope"] == "all_related_pss_plus_model_uma_extra"
    assert data["sentrix_stack_pss_mib"] == 50.0
    # 只有落在「相关进程」集合里的 compute 进程才计入 GPU 归属：11 + 22 = 33，999 不算。
    assert data["benchmark_process_gpu_memory_mib"] == 33.0
    assert data["all_processes_memory_mib"] == 66.0


def test_missing_pss_does_not_fall_back_to_rss_and_clears_stale_product_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(providers, "_host_process_rows", lambda: [dict(row) for row in PROCESSES[:2]])
    monkeypatch.setattr(providers, "_process_pss_mib", lambda pid: {100: None, 200: 150.0}[pid])
    monkeypatch.setattr(providers, "_process_smaps_field_mib", lambda pid, field: 100.0)
    data = providers._augment_host_process_memory(
        {"root_pid": 200, "product_stack_memory_mib": 999.0},
        process_hint="llama-server",
        endpoint_port="8100",
        model_pids={200},
        compute_apps=[],
    )
    # 传进来的旧产品总量必须先被清掉：留着它会被读成这一轮的测量值。
    assert "product_stack_memory_mib" not in data
    assert "benchmark_process_memory_used_mib" not in data
    assert data["model_process_system_memory_used_mib"] == 200.0


def test_missing_model_smaps_rss_does_not_write_product_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(providers, "_host_process_rows", lambda: [dict(row) for row in PROCESSES[:2]])
    monkeypatch.setattr(providers, "_process_pss_mib", lambda pid: 150.0)
    monkeypatch.setattr(providers, "_process_smaps_field_mib", lambda pid, field: None)
    data = providers._augment_host_process_memory(
        {"root_pid": 200},
        process_hint="llama-server",
        endpoint_port="8100",
        model_pids={200},
        compute_apps=[],
    )
    assert "product_stack_memory_mib" not in data
    assert "benchmark_process_memory_used_mib" not in data


def test_known_model_pids_win_over_the_generic_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """给了确切的模型 PID 就不再摊开成「所有带 vllm 字样的进程」。"""
    monkeypatch.setattr(providers, "_host_process_rows", lambda: [dict(row) for row in PROCESSES])
    monkeypatch.setattr(providers, "_process_pss_mib", lambda pid: 10.0)
    monkeypatch.setattr(providers, "_process_smaps_field_mib", lambda pid, field: 5.0)
    data = providers._augment_host_process_memory({"root_pid": 100}, compute_apps=[])
    assert [item["pid"] for item in data["model_system_processes"]] == [100]


# ---- Orin（远端 118）--------------------------------------------------------------


def test_orin_provider_rejects_an_endpoint_it_cannot_vouch_for() -> None:
    with pytest.raises(ValueError):
        OrinLlamaCppTelemetryProvider("http://192.168.0.119:8100/v1")


def test_orin_reads_pss_over_ssh_and_never_reports_it_as_vram(http: FakeHttpx, shell: FakeSubprocess) -> None:
    shell.on_run = lambda command, **kwargs: CompletedProcess("5858097\n4096\npid=375941\n")
    http.on_get = lambda url, **kwargs: FakeResponse(
        text="llamacpp:kv_cache_tokens 22\nllamacpp:kv_cache_usage_ratio 0.125\n"
    )
    provider = OrinLlamaCppTelemetryProvider("http://192.168.0.118:8100/v1")
    provider.gpu_stats()
    data = provider.process_memory()["data"]
    assert data["memory_unit"] == "process_pss_uma_mib"
    assert data["root_pid"] == 375941
    assert data["process_memory_used_mib"] == 5720.8
    assert data["model_process_system_memory_used_mib"] == 4.0
    assert data["kv_cache_used_tokens"] == 22
    assert data["vllm_metrics"]["kv_cache_usage_pct"] == 12.5
    assert provider.kv_cache()["data"]["used_bytes"] is None
    # ssh 必须带 BatchMode 与短超时，否则评测会被交互式登录卡住。
    assert shell.calls[0][0][:4] == ["ssh", "-o", "BatchMode=yes", "-o"]


def test_orin_ssh_failure_never_leaves_a_stale_or_zero_pss(http: FakeHttpx, shell: FakeSubprocess) -> None:
    shell.on_run = lambda command, **kwargs: (_ for _ in ()).throw(OSError("ssh unavailable"))
    http.on_get = lambda url, **kwargs: FakeResponse(text="")
    provider = OrinLlamaCppTelemetryProvider("http://192.168.0.118:8100/v1")
    provider.gpu_stats()
    assert "process_memory_used_mib" not in provider.process_memory()["data"]
    assert provider.kv_cache()["status"] == "unavailable"


def test_orin_failed_reprobe_drops_the_previous_pss(http: FakeHttpx, shell: FakeSubprocess) -> None:
    """**上一次探到的 PSS 不许留在 data 里被读成当前值。**

    5 秒的采样缓存意味着「成功一次、随后 ssh 断掉」是生产上真会走到的状态：那一轮
    必须把旧指标撤掉并写明原因，而不是继续报一个几分钟前的数。
    """
    http.on_get = lambda url, **kwargs: FakeResponse(text="")
    shell.on_run = lambda command, **kwargs: CompletedProcess("5858097\n4096\npid=375941\n")
    provider = OrinLlamaCppTelemetryProvider("http://192.168.0.118:8100/v1")
    provider.gpu_stats()
    assert provider.process_memory()["data"]["process_memory_used_mib"] == 5720.8

    shell.on_run = lambda command, **kwargs: (_ for _ in ()).throw(OSError("ssh down"))
    # 采样缓存是 5 秒；把上一次的探针时刻抹掉，让这次真的重探（缓存本身不在本用例的射程内）。
    provider._last_pss_probe = 0.0
    provider.gpu_stats()
    data = provider.process_memory()["data"]
    assert "process_memory_used_mib" not in data
    assert "pss_error" in data


def test_orin_truncated_ssh_output_drops_the_memory_metric(http: FakeHttpx, shell: FakeSubprocess) -> None:
    """ssh 通了但输出少一行（远端脚本被判失败时就是这样）：指标撤掉，不写一个错值。"""
    shell.on_run = lambda command, **kwargs: CompletedProcess("5858097\npid=375941\n")
    http.on_get = lambda url, **kwargs: FakeResponse(text="")
    provider = OrinLlamaCppTelemetryProvider("http://192.168.0.118:8100/v1")
    provider.gpu_stats()
    data = provider.process_memory()["data"]
    assert "process_memory_used_mib" not in data
    assert "pss_error" in data


def test_orin_system_memory_reads_the_remote_machine(http: FakeHttpx, shell: FakeSubprocess) -> None:
    shell.on_run = lambda command, **kwargs: CompletedProcess("16384.000 8192.000\n")
    provider = OrinLlamaCppTelemetryProvider("http://192.168.0.118:8100/v1")
    data = provider.system_memory()["data"]
    assert data["system_memory_total_mib"] == 16384.0
    assert data["system_memory_used_mib"] == 8192.0

    def boom(command: list[str], **kwargs: object) -> CompletedProcess:
        raise OSError("no route to host")

    shell.on_run = boom
    result = OrinLlamaCppTelemetryProvider("http://192.168.0.118:8100/v1").system_memory()
    assert result["status"] == "unavailable"
    assert result["reason"] == "remote_meminfo_unavailable"


# ---- 工厂 -------------------------------------------------------------------------


def test_unmanaged_runtime_gets_explicit_optional_providers() -> None:
    bundle = create_runtime_providers("host:8080")
    assert isinstance(bundle.lifecycle, UnavailableLifecycleProvider)
    assert isinstance(bundle.telemetry, UnavailableTelemetryProvider)
    assert bundle.lifecycle.state()["status"] == "not_applicable"
    assert bundle.telemetry.gpu_stats()["status"] == "not_applicable"


def test_managed_runtime_gets_the_manager_implementations() -> None:
    bundle = create_runtime_providers("host:8080", manager_url="manager:8500", api_mode="vllm")
    assert isinstance(bundle.lifecycle, ManagerLifecycleProvider)
    assert isinstance(bundle.telemetry, ManagerTelemetryProvider)
    assert bundle.inference.capabilities()["vllm_extensions"] is True


# ---- 端口可以被假件替换 -------------------------------------------------------------


class _FakeTelemetry:
    """**不继承任何东西**——结构化类型下它就应该是合法的 ``TelemetryProvider``。"""

    def gpu_stats(self) -> dict[str, object]:
        return {"status": "available", "source": "fake"}

    def process_memory(self) -> dict[str, object]:
        return {"status": "available", "source": "fake", "data": {"process_memory_used_mib": 1.0}}

    def kv_cache(self) -> dict[str, object]:
        return {"status": "not_applicable", "source": "fake"}

    def system_memory(self) -> dict[str, object]:
        return {"status": "available", "source": "fake"}


class _FakeLifecycle:
    def profiles(self) -> dict[str, object]:
        return {"status": "available", "profiles": []}

    def start(self, payload: object, *, timeout: float = 120) -> dict[str, object]:
        return {"status": "available"}

    def stop(self, payload: object = None, *, timeout: float = 90) -> dict[str, object]:
        return {"status": "available"}

    def state(self) -> dict[str, object]:
        return {"status": "available"}


class _FakeInference:
    def health(self) -> dict[str, object]:
        return {"status": "available"}

    def list_models(self) -> dict[str, object]:
        return {"models": []}

    def chat(self, payload: object, *, timeout: float | None = None) -> dict[str, object]:
        return {}

    def chat_stream(self, payload: object, *, timeout: float | None = None) -> object:
        return iter(())

    def token_count(self, messages: object, *, timeout: float = 15) -> dict[str, object] | None:
        return None

    def capabilities(self) -> dict[str, object]:
        return {}

    def max_concurrency(self) -> int | None:
        return None


@pytest.mark.parametrize(
    ("real", "port"),
    [
        (OpenAICompatibleInferenceProvider("host:8080"), InferenceProvider),
        (ManagerLifecycleProvider("manager:8500"), LifecycleProvider),
        (ManagerTelemetryProvider("manager:8500"), TelemetryProvider),
        (LocalAppleUnifiedTelemetryProvider("http://127.0.0.1:8100"), TelemetryProvider),
        (OrinLlamaCppTelemetryProvider("http://192.168.0.118:8100/v1"), TelemetryProvider),
        (HostNvidiaTelemetryProvider(), TelemetryProvider),
    ],
)
def test_real_providers_satisfy_their_ports(real: object, port: type) -> None:
    """端口方法被改名/删掉时这条会红——这正是「可替换」的凭证。"""
    assert isinstance(real, port)


def test_fake_providers_can_be_plugged_in_through_the_ports() -> None:
    bundle = RuntimeProviders(
        inference=_FakeInference(),
        lifecycle=_FakeLifecycle(),
        telemetry=_FakeTelemetry(),
    )
    assert isinstance(bundle.telemetry, TelemetryProvider)
    assert isinstance(bundle.lifecycle, LifecycleProvider)
    assert isinstance(bundle.inference, InferenceProvider)
    assert bundle.telemetry.system_memory()["source"] == "fake"
    assert bundle.lifecycle.state()["status"] == "available"
    assert bundle.inference.token_count([]) is None


# ---- CPU 温度（1.0 backend/hardware.py 搬来的那一个函数） --------------------------


def test_cpu_temperature_prefers_the_package_label(tmp_path: Path) -> None:
    hwmon = tmp_path / "hwmon0"
    hwmon.mkdir()
    (hwmon / "name").write_text("coretemp\n", encoding="ascii")
    (hwmon / "temp1_input").write_text("41200\n", encoding="ascii")
    (hwmon / "temp1_label").write_text("Package id 0\n", encoding="ascii")
    (hwmon / "temp2_input").write_text("39000\n", encoding="ascii")
    (hwmon / "temp2_label").write_text("Core 0\n", encoding="ascii")
    assert read_cpu_temperature_c(tmp_path) == 41.2


def test_cpu_temperature_ignores_unrelated_hwmon_devices(tmp_path: Path) -> None:
    nvme = tmp_path / "hwmon0"
    nvme.mkdir()
    (nvme / "name").write_text("nvme\n", encoding="ascii")
    (nvme / "temp1_input").write_text("41000\n", encoding="ascii")
    assert read_cpu_temperature_c(tmp_path) is None
    assert read_cpu_temperature_c(tmp_path / "missing") is None
