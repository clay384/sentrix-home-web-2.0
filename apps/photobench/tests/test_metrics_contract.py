"""Regression cases from the four-device T006 correctness audit."""

from __future__ import annotations

import json

import pytest

from photobench.domain.runs import analysis
from photobench.infrastructure.runtime import jetson_telemetry as jetson


def test_unknown_nvmap_does_not_become_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jetson, "_nvmap_clients_mib", lambda: None)
    result = jetson.unified_model_memory({12}, rss_mib=100, host_rss_mib=100)
    assert result["model_process_host_memory_mib"] == 100
    assert result["model_process_gpu_memory_mib"] is None
    assert result["model_process_system_memory_used_mib"] is None
    assert result["memory_complete"] is False


def _component_sample():
    return {
        "_t": 103.0,
        "memory_observation_version": 1,
        "model_process_host_memory_mib": 10,
        "model_process_gpu_memory_mib": 20,
        "model_process_system_memory_used_mib": 30,
        "product_stack_memory_mib": 35,
        "system_memory_used_mib": 100,
        "memory_dependencies": {
            "model_process_host_memory_mib": ["pss"],
            "model_process_gpu_memory_mib": ["nvmap"],
            "model_process_system_memory_used_mib": ["pss", "nvmap"],
            "product_stack_memory_mib": ["pss", "nvmap"],
            "system_memory_used_mib": ["system"],
        },
        "memory_observations": {
            key: {
                "status": "available",
                "observed_at_monotonic": time,
                "finished_at_monotonic": time + 0.1,
                "ttl_seconds": 3,
                "cache_hit": key == "nvmap",
            }
            for key, time in (("pss", 102), ("nvmap", 100), ("system", 102.5))
        },
    }


def test_memory_uses_original_cached_observation_and_oldest_dependency():
    raw = _component_sample()
    projected, _ = analysis.validated_telemetry_sample(raw)
    assert projected["process_observed_at_monotonic"] == 100
    assert projected["memory_observations"]["nvmap"]["age_seconds"] == 3
    assert projected["memory_complete"] is True
    assert "age_seconds" not in raw["memory_observations"]["nvmap"]
    raw["_t"] = 103.001
    expired, rejected = analysis.validated_telemetry_sample(raw)
    assert expired["model_process_host_memory_mib"] == 10
    assert expired["system_memory_used_mib"] == 100
    assert expired["model_process_gpu_memory_mib"] is None
    assert expired["model_process_system_memory_used_mib"] is None
    assert expired["product_stack_memory_mib"] is None
    assert expired["process_observed_at_monotonic"] is None
    assert "product_stack_memory_mib" in rejected
    assert raw["product_stack_memory_mib"] == 35


@pytest.mark.parametrize("bad", [None, True, float("nan"), 104])
def test_memory_missing_invalid_or_future_observation_is_not_fresh(bad):
    raw = _component_sample()
    raw["memory_observations"]["nvmap"]["observed_at_monotonic"] = bad
    result, _ = analysis.validated_telemetry_sample(raw)
    assert result["model_process_gpu_memory_mib"] is None
    assert result["model_process_host_memory_mib"] == 10


def test_new_version_cannot_claim_a_total_without_dependencies_and_legacy_is_preserved():
    raw = {"_t": 103, "product_stack_memory_mib": 35}
    assert analysis.validated_telemetry_sample(raw)[0]["product_stack_memory_mib"] == 35
    result, _ = analysis.validated_telemetry_sample({**raw, "memory_observation_version": 1})
    assert result["product_stack_memory_mib"] is None


def test_nx_power_is_board_power_and_keeps_rails() -> None:
    sample = jetson._parse_tegrastats_line(
        "RAM 4000/16000MB GR3D_FREQ 75% gpu@55C "
        "VDD_IN 12000mW/11000mW VDD_CPU_GPU_CV 8000mW/7000mW VDD_SOC 2000mW/1900mW"
    )
    assert sample["power_draw_w"] == 12
    assert sample["power_scope"] == "board"
    assert sample["power_rail"] == "VDD_IN"
    assert sample["power_rails_w"]["VDD_CPU_GPU_CV"] == 8


def test_nearest_rank_p95_has_no_off_by_one() -> None:
    assert analysis.series_stats([{"x": n} for n in range(1, 21)], "x")["p95"] == 19


def test_slow_process_probe_cannot_publish_old_device_values_as_fresh(monkeypatch):
    from photobench.application.run_execution import gpu_sampler

    clock = [100.0]

    class SlowProcess:
        def gpu_stats(self):
            return {
                "status": "available",
                "data": {
                    "gpus": [
                        {
                            "power_draw_w": 30,
                            "temperature_c": 50,
                            "device_observed_at_monotonic": 99.5,
                            "device_observation_age_seconds": 0.5,
                            "device_observation_ttl_seconds": 1.5,
                        }
                    ]
                },
            }

        def process_memory(self):
            clock[0] = 165.0
            return {"status": "available", "data": {"product_stack_memory_mib": 500}}

        def system_memory(self):
            return {"status": "available", "data": {"system_memory_used_mib": 1000}}

        def kv_cache(self):
            return {"status": "not_applicable"}

    monkeypatch.setattr(gpu_sampler.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(gpu_sampler.time, "perf_counter", lambda: clock[0])
    sampler = gpu_sampler.GpuSampler(SlowProcess())
    sampler.sample_once()
    sample = sampler.samples[0]
    assert sample["device_observation_age_seconds"] == 65.5
    assert sample["power_draw_w"] is None
    assert sample["temperature_c"] is None
    assert sample["source_status"]["device"] == "unavailable"
    assert sample["product_stack_memory_mib"] == 500
    assert sample["system_memory_used_mib"] == 1000


def test_historical_freshness_is_derived_from_timestamps_without_mutating_raw():
    raw = {
        "_t": 165.0,
        "device_observed_at_monotonic": 99.5,
        "device_observation_age_seconds": 0.5,
        "power_draw_w": 30,
        "source_status": {"device": "available", "process": "available"},
        "product_stack_memory_mib": 500,
    }
    derived, rejected = analysis.validated_telemetry_sample(raw)
    assert derived["device_observation_age_seconds"] == 65.5
    assert derived["device_freshness_version"] == 1
    assert derived["power_draw_w"] is None
    assert "power_draw_w" in rejected
    assert raw["power_draw_w"] == 30
    assert raw["source_status"]["device"] == "available"


@pytest.mark.parametrize(("captured", "expected_power"), [(101.5, 30), (101.501, None), (99, None)])
def test_device_ttl_uses_source_period_and_preserves_memory(captured, expected_power):
    raw = {
        "_t": captured,
        "device_observed_at_monotonic": 100,
        "device_observation_ttl_seconds": 1.5,
        "power_draw_w": 30,
        "power_rails_w": {"VDD_IN": 30},
        "system_memory_used_mib": 1000,
    }
    derived, _ = analysis.validated_telemetry_sample(raw)
    assert derived["power_draw_w"] == expected_power
    assert derived["system_memory_used_mib"] == 1000
    assert raw["power_rails_w"] == {"VDD_IN": 30}


def test_downsampling_preserves_all_phase_boundaries() -> None:
    history = [{"phase": str(n // 2), "t": n} for n in range(40)]
    selected = analysis.downsample_history(history, limit=8)
    assert {point["phase"] for point in selected} == {str(n) for n in range(20)}


def test_history_rejects_apple_outliers_without_editing_raw() -> None:
    line = json.dumps({"_t": 1, "source": "apple_unified", "power_draw_w": 325, "temperature_c": 0.8})
    live: dict[str, object] = {"source": "apple_unified"}
    analysis.attach_gpu_sample_history(live, [line])
    assert not (live.get("peak") or {})
    assert json.loads(line)["power_draw_w"] == 325


def test_native_completion_does_not_require_legacy_turn_outcome() -> None:
    item = {
        "metrics_contract_version": 2,
        "agent_status": "complete",
        "termination_reason": "answered_with_gap",
        "answer": "answer",
        "agent2_trace": {"budget_outcome": {"model_steps": 3, "max_steps": 4}},
        "execution_trace": [{"stage": "model", "call_type": "agent", "action": "final"}],
    }
    result = analysis.agent_stability(item)
    assert result["completed_within_steps"] is True
    assert result["json_parse_total"] is None


@pytest.mark.parametrize(
    ("second_reason", "second_answer", "second_status", "second_steps", "expected"),
    [
        ("answered", "answer", "completed", 4, True),
        ("answered_with_gap", "answer", "completed", 4, True),
        ("step_budget_exhausted", "answer", "completed", 4, False),
        ("answered", "", "completed", 4, False),
        ("answered", "answer", "failed", 4, False),
        ("answered", "answer", "completed", 5, False),
        ("answered", "answer", "completed", None, None),
    ],
)
def test_completion_uses_real_nested_turn_budgets(
    second_reason, second_answer, second_status, second_steps, expected
) -> None:
    first = {
        "termination_reason": "answered",
        "answer": "first answer",
        "agent_status": "completed",
        "agent2_trace": {"budget_outcome": {"model_steps": 2, "max_steps": 4}},
    }
    second = {
        "termination_reason": second_reason,
        "answer": second_answer,
        "agent_status": second_status,
        "agent2_trace": {"budget_outcome": {"model_steps": second_steps, "max_steps": 4}},
    }
    item = {
        "metrics_contract_version": 2,
        "answer": "last answer",
        "termination_reason": "answered",
        "runtime_turns": [first, second],
    }
    stability = analysis.agent_stability(item)
    assert stability["completed_within_steps"] is expected
    item["agent_stability"] = stability
    summary = analysis.capability_summary([item])
    assert summary["qa_completion_valid_count"] == (0 if expected is None else 1)
    assert summary["qa_completion_within_steps_rate"] == (None if expected is None else float(expected))


def test_later_answer_does_not_hide_a_failed_earlier_turn() -> None:
    item = {
        "metrics_contract_version": 2,
        "runtime_turns": [
            {"termination_reason": "timeout"},
            {"termination_reason": "answered", "answer": "answer"},
        ],
    }
    assert analysis.agent_stability(item)["completed_within_steps"] is False


def test_endpoint_attribution_tracks_descendants_and_port_reuse() -> None:
    from photobench.infrastructure.runtime.telemetry_scope import service_rows

    rows = [
        {"pid": 1, "ppid": 0, "listening_ports": [8100], "identity": "llama-server"},
        {"pid": 2, "ppid": 1, "listening_ports": [], "identity": "worker"},
        {"pid": 3, "ppid": 0, "listening_ports": [8106], "identity": "llama-server"},
    ]
    spec = {"ports": [8100], "keywords": ["llama-server"]}
    assert {row["pid"] for row in service_rows(rows, spec)} == {1, 2}
    rows[0]["listening_ports"] = []
    rows[2]["listening_ports"] = [8100]
    assert {row["pid"] for row in service_rows(rows, spec)} == {3}
    assert service_rows([], spec) == []


def test_nvidia_ram_never_uses_nvmap_or_other_instances(monkeypatch: pytest.MonkeyPatch) -> None:
    from photobench.infrastructure.runtime import providers

    monkeypatch.setattr(
        providers,
        "_host_process_rows",
        lambda: [
            {"pid": 1, "ppid": 0, "listening_ports": [8100]},
            {"pid": 2, "ppid": 1, "listening_ports": []},
            {"pid": 3, "ppid": 0, "listening_ports": [8106]},
        ],
    )
    monkeypatch.setattr(providers, "_process_pss_mib", lambda pid: pid * 10.0)
    result = providers._augment_host_process_memory(
        {},
        telemetry_services={
            "main_model": {"ports": [8100]},
            "embedding": {},
            "product": [],
        },
        compute_apps=[{"pid": 1, "used_memory_mib": 100}, {"pid": 3, "used_memory_mib": 900}],
    )
    assert result["model_process_system_memory_used_mib"] == 30
    assert result["benchmark_process_gpu_memory_mib"] == 100
    assert result["all_processes_memory_mib"] == 1000
    assert result["memory_accounting"] == "nvidia_pss_and_vram"
    aggregate = analysis.aggregate_gpu_samples([{"source": "host_nvidia_smi", **result}])
    assert aggregate["source"] == "host_nvidia_smi"


@pytest.mark.parametrize("residual,expected", [(0, None), (20, 20)])
def test_nvmap_residual_is_only_a_positive_estimate(
    monkeypatch: pytest.MonkeyPatch, residual, expected
) -> None:
    monkeypatch.setattr(jetson, "_nvmap_clients_mib", lambda: None)
    result = jetson.unified_model_memory({12}, rss_mib=100 + residual, host_rss_mib=100)
    assert result["model_process_gpu_memory_mib"] == expected
    assert result["model_process_gpu_memory_source"] == ("vmrss_residual" if residual else "unavailable")


def test_running_execution_freezes_real_http_and_telemetry(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from photobench.application.run_execution.service import RunRequest
    from photobench.bootstrap.container import Container
    from photobench.config import Settings

    config = tmp_path / "config"
    config.mkdir()
    (config / "runtime_connection.json").write_text(
        json.dumps(
            {
                "sentrix_url": "http://127.0.0.1:8291",
                "model_base_url": "http://127.0.0.1:8100/v1",
                "embedding": {"mode": "no_local_embedding", "local": False},
            }
        )
    )
    (config / "judge_providers.json").write_text(
        json.dumps(
            {
                "default_provider_id": "cloud",
                "providers": {"cloud": {"url": "https://judge.example/v1", "model": "judge"}},
            }
        )
    )
    (config / "vllm_targets.json").write_text(
        json.dumps(
            {
                "default_target_id": "local",
                "targets": {"local": {"model_base_url": "http://127.0.0.1:8100/v1"}},
            }
        )
    )
    container = Container.build(Settings(config, tmp_path / "results", tmp_path / "data"))
    request = RunRequest(
        album_id="a",
        qa_set="q",
        model_profile="__current__",
        sentrix_url="http://127.0.0.1:8291",
        model_base_url="http://127.0.0.1:8100/v1",
        runtime_framework="llama_cpp",
    )
    execution = container.run_execution(request)
    urls = []
    monkeypatch.setattr(container.http, "request_json", lambda url, **kw: urls.append(url) or {"items": []})
    container.config.persist_runtime_connection(
        {
            "sentrix_url": "http://127.0.0.1:9999",
            "model_base_url": "http://127.0.0.1:8106/v1",
            "judge_url": "https://judge.example/v1",
            "judge_model": "judge",
        }
    )
    execution.client.list_assets(scope_id="x", limit=10)
    assert urls and all(url.startswith("http://127.0.0.1:8291/") for url in urls)
    assert execution.runtime_snapshot.connection()["model_base_url"].endswith(":8100/v1")
    assert execution.telemetry.telemetry_services["main_model"]["ports"] == [8100]


def test_missing_auxiliary_listener_is_unknown_but_explicit_absence_is_zero(monkeypatch):
    from photobench.infrastructure.runtime import providers

    monkeypatch.setattr(providers, "_host_process_rows", lambda: [{"pid": 1, "listening_ports": [8100]}])
    monkeypatch.setattr(providers, "_process_pss_mib", lambda pid: 10)
    scope = {"main_model": {"ports": [8100]}, "embedding": {}, "product": []}
    absent = providers._augment_host_process_memory({}, telemetry_services=scope, compute_apps=[])
    assert absent["sentrix_auxiliary_model_pss_mib"] == 0
    assert absent["product_stack_memory_mib"] == 10
    scope["embedding"] = {"mode": "split", "profiles": {"split": {"ports": [8101, 8103]}}}
    failed = providers._augment_host_process_memory({}, telemetry_services=scope, compute_apps=[])
    assert failed["sentrix_auxiliary_model_pss_mib"] is None
    assert failed["product_stack_memory_mib"] is None


def test_orphan_backup_does_not_block_recovery_after_interrupted_replace(tmp_path):
    from photobench.infrastructure.results.recovery import FileRunStartupRecovery

    run = tmp_path / "old"
    run.mkdir()
    (run / "run.json").write_text('{"status":"cancelling"}')
    backup = run / "run.before-orphan-recovery.json"
    backup.write_text("first evidence")
    recovery = FileRunStartupRecovery(tmp_path)
    try:
        assert recovery.acquire_and_recover() == 1
        assert backup.read_text() == "first evidence"
        assert json.loads((run / "run.json").read_text())["status"] == "interrupted"
    finally:
        recovery.close()


def test_native_parse_facts_exclude_text_and_count_bad_json() -> None:
    item = {
        "metrics_contract_version": 2,
        "termination_reason": "model_error",
        "agent_status": "error",
        "execution_trace": [
            {"stage": "model", "action": "final", "call_type": "agent"},
            {
                "stage": "model",
                "call_type": "agent",
                "tool_argument_parse_facts": [{"json_object_valid": True}, {"json_object_valid": False}],
            },
        ],
    }
    result = analysis.agent_stability(item)
    assert result["tool_argument_json_total"] == 2
    assert result["tool_argument_json_rate"] == 0.5
    assert result["json_parse_total"] is None
    assert result["completed_within_steps"] is False


def test_agent_wall_is_not_reduced_by_overlapping_judge() -> None:
    item = {"metrics_contract_version": 2, "wall_clock_ms": 100, "judge_ms": 300}
    result = analysis.capability_summary(
        [item], {"qa_eval": {"agent_phase_wall_ms": 200, "agent_completed": 1}}
    )
    assert result["agent_task_latency_mean_ms"] == 100
    assert result["agent_throughput_qa_per_s"] == 5


def test_stream_exit_and_age_invalidate_values(monkeypatch: pytest.MonkeyPatch) -> None:
    from photobench.infrastructure.runtime import apple_telemetry as apple

    class Process:
        dead = False

        def poll(self):
            return 1 if self.dead else None

    process = Process()
    stream = apple._MacmonStream()
    stream._proc = process
    stream._observed_at = 10
    stream._latest = {"power_draw_w": 10}
    monkeypatch.setattr(apple.time, "monotonic", lambda: 12)
    assert stream.latest()["power_draw_w"] == 10
    monkeypatch.setattr(apple.time, "monotonic", lambda: 14)
    assert stream.latest() == {}
    process.dead = True
    monkeypatch.setattr(stream, "start", lambda: None)
    assert stream.latest() == {}


def test_single_instance_orphan_recovery_preserves_original(tmp_path) -> None:
    from photobench.infrastructure.results.recovery import FileRunStartupRecovery

    run = tmp_path / "r"
    run.mkdir()
    original = json.dumps({"run_id": "r", "status": "cancelling", "items": [{"answer": "retained"}]})
    (run / "run.json").write_text(original)
    recovery = FileRunStartupRecovery(tmp_path)
    assert recovery.acquire_and_recover() == 1
    assert (run / "run.before-orphan-recovery.json").read_text() == original
    saved = json.loads((run / "run.json").read_text())
    assert saved["status"] == "interrupted" and saved["items"][0]["answer"] == "retained"
    second = FileRunStartupRecovery(tmp_path)
    with pytest.raises(RuntimeError, match="live owner"):
        second.acquire_and_recover()
    recovery.close()
    assert second.acquire_and_recover() == 0
    second.close()


def test_jetson_device_cache_cannot_extend_stream_lifetime(monkeypatch):
    class Stream:
        interval_ms = 500
        _observed_at = 10
        line = "VDD_IN 12000mW/11000mW GR3D_FREQ 80%"

        def latest_line(self):
            return self.line

    stream = Stream()
    monkeypatch.setattr(jetson, "_TEGRASTATS", stream)
    monkeypatch.setattr(jetson, "_meminfo_mib", lambda: {"system_memory_used_mib": 100})
    monkeypatch.setattr(jetson.time, "monotonic", lambda: 10.1)
    provider = jetson.LocalJetsonLlamaCppTelemetryProvider("http://127.0.0.1:8100")
    assert provider._device()["power_draw_w"] == 12
    stream.line = ""
    result = provider._device()
    assert "power_draw_w" not in result
    assert result["system_memory_used_mib"] == 100


def test_apple_scoped_model_workers_deduplicate_shared_roles(monkeypatch):
    from photobench.infrastructure.runtime import apple_telemetry as apple

    rows = [{"pid": 1, "ppid": 0}, {"pid": 2, "ppid": 1}, {"pid": 3, "ppid": 0}]
    for row in rows:
        row["process_name"] = "worker"
    provider = apple.LocalAppleUnifiedTelemetryProvider(
        "http://127.0.0.1:8100",
        telemetry_services={
            "main_model": {"ports": [8100]},
            "embedding": {"mode": "split", "profiles": {"split": {"ports": [8101]}}},
            "sentrix": {},
            "product": [{"ports": [8101]}],
        },
    )
    monkeypatch.setattr(provider, "_device", lambda: {})
    monkeypatch.setattr(provider, "_ports", lambda: {1: {"8100"}, 2: {"8101"}, 3: {"8106"}})
    monkeypatch.setattr(apple, "_process_rows", lambda: [dict(r) for r in rows])
    monkeypatch.setattr(apple, "_phys_footprint_bytes", lambda pid: 100 * 1048576)
    monkeypatch.setattr(apple, "_weight_map_bytes", lambda pid: 10 * 1048576)
    data = provider._augment()
    assert data["model_process_footprint_mib"] == 200
    assert data["product_stack_memory_mib"] == 220
    assert data["sentrix_auxiliary_model_pss_mib"] == 0
    assert data["memory_complete"] is True
    provider.telemetry_services["embedding"]["profiles"]["split"]["ports"] = [8103]
    assert provider._augment()["memory_complete"] is False


def test_orphan_rejudge_on_terminal_run_is_also_recovered(tmp_path):
    from photobench.infrastructure.results.recovery import FileRunStartupRecovery

    run = tmp_path / "r"
    run.mkdir()
    original = {"status": "completed", "rejudge": {"status": "running"}, "items": [{"answer": "keep"}]}
    (run / "run.json").write_text(json.dumps(original))
    lease = FileRunStartupRecovery(tmp_path)
    try:
        assert lease.acquire_and_recover() == 1
        result = json.loads((run / "run.json").read_text())
        assert result["status"] == "completed"
        assert result["rejudge"]["status"] == "interrupted"
        assert result["items"] == original["items"]
        assert json.loads((run / "run.before-orphan-recovery.json").read_text()) == original
    finally:
        lease.close()


def test_jetson_no_auxiliary_is_zero_and_disappeared_model_cannot_reuse_total(monkeypatch):
    rows = [
        {
            "pid": 1,
            "ppid": 0,
            "start_ticks": "123",
            "rss_mib": 10,
            "listening_ports": [8100],
            "process_name": "llama-server",
        }
    ]
    monkeypatch.setattr(jetson, "_host_process_rows", lambda ports: rows)
    monkeypatch.setattr(jetson, "_process_start_ticks", lambda pid: "123")
    monkeypatch.setattr(jetson, "_process_smaps_memory_mib", lambda pid: (8, 10))
    jetson._nvmap_cache.clear()
    jetson._nvmap_cache.update(at=float("-inf"), table=None)
    monkeypatch.setattr(jetson, "_read_nvmap_clients", lambda: "llama 0 1 20480K\n")
    scope = {"main_model": {"ports": [8100]}, "embedding": {}, "sentrix": {}, "product": []}
    good = jetson._augment_local_process_memory(
        {}, model_pid=None, endpoint_port="8100", telemetry_services=scope
    )
    assert good["product_stack_memory_mib"] == 28
    assert good["model_process_system_memory_used_mib"] == 30
    assert good["sentrix_auxiliary_model_uma_mib"] == 0
    rows.clear()
    lost = jetson._augment_local_process_memory(
        good, model_pid=None, endpoint_port="8100", telemetry_services=scope
    )
    assert lost["product_stack_memory_mib"] is None
    assert lost["model_process_system_memory_used_mib"] is None
    assert lost["root_pid"] is None


def test_jetson_and_nvidia_memory_remains_available_without_device_source(monkeypatch):
    from photobench.infrastructure.runtime import providers

    monkeypatch.setattr(
        jetson, "_augment_local_process_memory", lambda *a, **k: {"model_process_system_memory_used_mib": 10}
    )
    monkeypatch.setattr(jetson, "_meminfo_mib", lambda: {"system_memory_used_mib": 100})
    provider = jetson.LocalJetsonLlamaCppTelemetryProvider("http://127.0.0.1:8100")
    assert provider.process_memory()["data"]["model_process_system_memory_used_mib"] == 10
    assert provider.system_memory()["data"]["system_memory_used_mib"] == 100
    nvidia = providers.HostNvidiaTelemetryProvider(endpoint_url="http://127.0.0.1:8100")

    def fail(*a, **k):
        raise OSError("GPU driver unavailable")

    monkeypatch.setattr(nvidia, "_query", fail)
    monkeypatch.setattr(
        providers,
        "_augment_host_process_memory",
        lambda *a, **k: {"model_process_system_memory_used_mib": 20},
    )
    result = nvidia.process_memory()
    assert result["status"] == "available"
    assert result["data"]["model_process_system_memory_used_mib"] == 20
    assert result["data"]["process_memory_used_mib"] is None


def test_linux_memory_never_substitutes_memfree_for_memavailable(monkeypatch):
    from photobench.domain.runtime.metrics import system_used

    assert system_used(100, 30) == 70
    assert system_used(100, None) is None
    assert system_used(100, 200) is None
    monkeypatch.setattr(jetson.Path, "read_text", lambda *a, **k: "MemTotal: 102400 kB\nMemFree: 1024 kB\n")
    assert jetson._meminfo_mib() == {}


def test_jetson_pss_and_rss_share_one_kernel_observation(monkeypatch):
    import builtins
    import io

    reads = []

    def changing_rollup(path, **kwargs):
        reads.append(path)
        return io.StringIO(
            "Rss: 10240 kB\nPss: 8192 kB\n" if len(reads) == 1 else "Rss: 20480 kB\nPss: 16384 kB\n"
        )

    monkeypatch.setattr(builtins, "open", changing_rollup)
    assert jetson._process_smaps_memory_mib(42) == (8, 10)
    assert len(reads) == 1


def test_jetson_partial_rollup_keeps_missing_component_unavailable(monkeypatch):
    import builtins
    import io

    monkeypatch.setattr(builtins, "open", lambda *a, **k: io.StringIO("Rss: 10240 kB\n"))
    assert jetson._process_smaps_memory_mib(42) == (None, 10)


def test_per_run_product_url_overrides_stale_product_port_and_remote_is_not_local():
    from photobench.infrastructure.runtime.telemetry_scope import services_from_connection

    connection = {
        "sentrix_url": "http://127.0.0.1:8292",
        "model_base_url": "http://127.0.0.1:8100/v1",
        "product": [{"id": "sentrix_api", "ports": [8291], "local": True}],
    }
    assert services_from_connection(connection)["product"][0]["ports"] == [8292]
    connection["sentrix_url"] = "http://remote.invalid:8292"
    product = services_from_connection(connection)["product"][0]
    assert product["ports"] == [] and product["local"] is False


def test_confirmed_absence_requires_fresh_topology_even_for_zero():
    raw = _component_sample()
    raw["sentrix_auxiliary_model_uma_mib"] = 0
    raw["memory_dependencies"]["sentrix_auxiliary_model_uma_mib"] = []
    assert analysis.validated_telemetry_sample(raw)[0]["sentrix_auxiliary_model_uma_mib"] is None
    raw["memory_dependencies"]["sentrix_auxiliary_model_uma_mib"] = ["pss"]
    assert analysis.validated_telemetry_sample(raw)[0]["sentrix_auxiliary_model_uma_mib"] == 0
    raw["_t"] = 106
    assert analysis.validated_telemetry_sample(raw)[0]["sentrix_auxiliary_model_uma_mib"] is None


@pytest.mark.parametrize(
    "key,bad",
    [
        ("memory_observations", []),
        ("memory_dependencies", []),
        ("component_validity", []),
        ("source_diagnostics", []),
    ],
)
def test_malformed_memory_metadata_does_not_break_historical_reading(key, bad):
    raw = _component_sample()
    raw[key] = bad
    raw["_t"] = 110
    result, _ = analysis.validated_telemetry_sample(raw)
    assert result["product_stack_memory_mib"] is None


def test_coverage_does_not_count_stale_or_boolean_memory_as_core_observation():
    raw = _component_sample()
    raw["_t"] = 104
    assert analysis.telemetry_sampling([raw])["phases"]["unassigned"]["core_memory_valid"] == 0
    assert (
        analysis.telemetry_sampling([{"system_memory_used_mib": True}])["phases"]["unassigned"][
            "core_memory_valid"
        ]
        == 0
    )


def test_additive_observation_and_integrity_contract_round_trips_without_changing_null():
    from photobench.api.metrics_schema import RunMetricsResponse

    raw = _component_sample()
    raw["_t"] = 104
    projected, _ = analysis.validated_telemetry_sample(raw)
    payload = {
        "telemetry_live": {"latest": projected},
        "phases": {
            "gpu_metrics": {
                "status": "done",
                "samples_count": 1,
                "sampling_integrity": {
                    "callback_failures": 1,
                    "last_callback_failure": "OSError: full",
                    "stop_pending_threads": [],
                },
            },
            "old_optional": None,
        },
    }
    assert RunMetricsResponse.model_validate(payload).model_dump(exclude_unset=True) == payload
    live = {}
    analysis.attach_gpu_sample_history(live, [json.dumps(projected)])
    assert live["latest"]["model_process_gpu_memory_mib"] is None
    assert live["latest"]["memory_observations"]["nvmap"]["observed_at_monotonic"] == 100
    assert live["latest"]["memory_observations"]["nvmap"]["fresh"] is False


def test_qa_progress_projection_preserves_nested_and_legacy_counters_and_null():
    from photobench.api.metrics_schema import RunMetricsResponse

    for completed in (0, 70, None):
        payload = {
            "phases": {
                "qa_eval": {
                    "judge_completed": completed,
                    "judge_total": 487,
                    "progress": {
                        "completed": 71,
                        "agent_completed": 71,
                        "agent_total": 487,
                        "judge_completed": completed,
                        "judge_total": 487,
                        "judge_skipped": 0,
                    },
                }
            }
        }
        assert RunMetricsResponse.model_validate(payload).model_dump(exclude_unset=True) == payload


def test_capacity_progress_and_unknown_outcome_are_additive_api_facts():
    from photobench.api.metrics_schema import RunItemObservation, RunMetricsResponse
    from photobench.domain.runs.analysis import item_summary

    item = {
        "qa_id": "q",
        "execution_status": "completed",
        "judge_status": "outcome_unknown",
        "judge_dispatch_status": "outcome_unknown",
    }
    projected = item_summary(item, 0)
    assert projected["judge_status"] == "outcome_unknown"
    assert RunItemObservation.model_validate(projected).judge_dispatch_status == "outcome_unknown"
    assert "judge_dispatch_status" not in item_summary({"qa_id": "old"}, 0)
    response = RunMetricsResponse.model_validate(
        {
            "phases": {
                "qa_eval": {
                    "progress": {
                        "agent_running": 2,
                        "judge_waiting": 5,
                        "judge_running": 4,
                        "outcome_unknown": 1,
                    },
                    "judge_requests": {"version": 1, "requests_started": 17, "http_429": 2, "in_flight": 4},
                }
            }
        }
    )
    phase = response.phases["qa_eval"]
    assert phase.progress.judge_waiting == 5
    assert phase.judge_requests.requests_started == 17
    assert (
        RunMetricsResponse.model_validate({"phases": {"qa_eval": {}}}).phases["qa_eval"].judge_requests
        is None
    )
