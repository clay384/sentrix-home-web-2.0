"""Jetson 统一内存口径：nvmap 账本优先、残差兜底、零残差报不可用。

1.0 的结论（``backend/jetson_telemetry.py:149-195``）：nvmap 是分配量、可读时权威；
残差是驻留量，只在 L4T >= R36.4.7 有效。46 是 R36.4.3，nvmap.ko 不把 GPU 页记进
进程 RSS，残差恒为 0；把这个 0 当真实值会漏掉约 79% 的占用。
"""

from __future__ import annotations

import pytest

from photobench.infrastructure.runtime import jetson_telemetry as jetson


def test_nvmap_ledger_wins_when_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jetson, "_nvmap_clients_mib", lambda: {42: 2617.9})
    gpu, source = jetson._process_gpu_memory_mib({42}, rss_mib=714.6, host_rss_mib=714.6)
    assert gpu == 2617.9
    assert source == "nvmap_iovmm"


def test_residual_is_used_only_when_the_ledger_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(jetson, "_nvmap_clients_mib", lambda: None)
    gpu, source = jetson._process_gpu_memory_mib({42}, rss_mib=5370.0, host_rss_mib=1030.0)
    assert gpu == 4340.0
    assert source == "vmrss_residual"


def test_a_zero_residual_is_unavailable_not_a_real_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """46 的关键分支：差额 0 代表「没记账」，不是「没占用」。"""
    monkeypatch.setattr(jetson, "_nvmap_clients_mib", lambda: None)
    gpu, source = jetson._process_gpu_memory_mib({42}, rss_mib=714.6, host_rss_mib=714.6)
    assert gpu is None
    assert source == "unavailable"


def test_missing_rss_or_host_rss_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jetson, "_nvmap_clients_mib", lambda: None)
    assert jetson._process_gpu_memory_mib({42}, rss_mib=None, host_rss_mib=1.0) == (
        None,
        "unavailable",
    )
    assert jetson._process_gpu_memory_mib({42}, rss_mib=1.0, host_rss_mib=None) == (
        None,
        "unavailable",
    )


def test_unified_total_is_host_plus_gpu_without_double_counting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(jetson, "_nvmap_clients_mib", lambda: {42: 2617.9})
    data = jetson.unified_model_memory({42}, rss_mib=714.6, host_rss_mib=714.6)
    assert data["model_process_system_memory_used_mib"] == 3332.5
    assert data["model_process_host_memory_mib"] == 714.6
    assert data["model_process_gpu_memory_mib"] == 2617.9
    assert data["model_process_gpu_memory_source"] == "nvmap_iovmm"
    assert data["model_process_system_memory_scope"] == "host_smaps_rss_plus_gpu_uma"


def test_without_smaps_split_the_total_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jetson, "_nvmap_clients_mib", lambda: {42: 2617.9})
    data = jetson.unified_model_memory({42}, rss_mib=714.6, host_rss_mib=None)
    assert data["model_process_system_memory_used_mib"] is None
    assert data["model_process_system_memory_scope"] == "host_smaps_rss_plus_gpu_uma"
    assert data["model_process_gpu_memory_source"] == "unavailable"
    assert data["model_process_host_memory_mib"] is None


def test_no_inputs_writes_nothing() -> None:
    assert jetson.unified_model_memory({42}, rss_mib=None, host_rss_mib=None) == {}


def test_unified_metadata_replaces_the_legacy_pss_unit() -> None:
    data = {
        "model_process_host_memory_mib": 1670.84,
        "model_process_gpu_memory_mib": 5094.35,
    }
    jetson.apply_jetson_memory_metadata(data)
    assert data["memory_unit"] == "host_smaps_rss_plus_gpu_mib"
    assert data["memory_scope"] == "host_smaps_rss_plus_gpu_uma"
    assert data["memory_profile"]["method"] == "orin_smaps_rss_plus_gpu_uma_v1"


def test_no_split_fields_keeps_the_legacy_unit() -> None:
    data = {"memory_unit": "process_pss_uma_mib"}
    jetson.apply_jetson_memory_metadata(data)
    assert data["memory_unit"] == "process_pss_uma_mib"
    assert "memory_profile" not in data


def test_sampler_walks_each_process_once_even_when_device_stats_are_collected(monkeypatch):
    import builtins
    import io

    from photobench.application.run_execution.gpu_sampler import GpuSampler

    provider = jetson.LocalJetsonLlamaCppTelemetryProvider("http://127.0.0.1:8100", telemetry_services={})
    monkeypatch.setattr(provider, "_pid", lambda: 42)
    monkeypatch.setattr(provider, "_metrics", lambda: {})
    monkeypatch.setattr(provider, "_llama_args", lambda pid: {})
    monkeypatch.setattr(provider, "_device", lambda: {})
    monkeypatch.setattr(jetson, "_meminfo_mib", lambda: {"system_memory_used_mib": 100})
    jetson._nvmap_cache.clear()
    jetson._nvmap_cache.update(at=float("-inf"), table=None)
    # 这台机器上若有真实的 runtime_connection.json，scope 会覆盖关键词匹配路径，
    # 于是 pid 42 根本不会被扫到。这条用例验的是「每个进程只走一次 smaps」，
    # 不是「与机器上的部署配置交互」——把它隔离掉，CI 与开发机给出同一个答案。
    monkeypatch.setattr(jetson, "load_telemetry_services", lambda: None)
    monkeypatch.setattr(jetson, "_read_nvmap_clients", lambda: "llama 0 42 3072K\n")
    monkeypatch.setattr(jetson, "_process_start_ticks", lambda pid: "123")
    monkeypatch.setattr(
        jetson,
        "_host_process_rows",
        lambda ports: [
            {"pid": 42, "start_ticks": "123", "identity": "llama-server --port 8100", "rss_mib": 1}
        ],
    )
    walks = []

    def smaps(path, *args, **kwargs):
        walks.append(str(path))
        return io.StringIO("Rss: 1024 kB\nPss: 512 kB\n")

    monkeypatch.setattr(builtins, "open", smaps)
    sampler = GpuSampler(provider)
    sampler.sample_once()
    assert walks == ["/proc/42/smaps_rollup"]
    assert sampler.samples[0]["model_process_host_memory_mib"] == 1
    assert sampler.samples[0]["model_process_system_memory_used_mib"] == 4


def test_failed_optional_metrics_probe_never_reuses_previous_kv(monkeypatch):
    provider = jetson.LocalJetsonLlamaCppTelemetryProvider("http://127.0.0.1:8100")
    values = iter([{"kv_cache_used_tokens": 12}, {}])
    monkeypatch.setattr(provider, "_metrics", lambda: next(values))
    monkeypatch.setattr(provider, "_pid", lambda: 42)
    monkeypatch.setattr(provider, "_llama_args", lambda pid: {})
    monkeypatch.setattr(provider, "_device", lambda: {})
    provider.gpu_stats()
    assert provider.kv_cache()["data"]["used_tokens"] == 12
    provider.gpu_stats()
    assert provider.kv_cache()["status"] == "unavailable"


def test_busy_optional_metrics_endpoint_has_a_short_budget(monkeypatch):
    import httpx

    provider = jetson.LocalJetsonLlamaCppTelemetryProvider("http://127.0.0.1:8100")

    def busy(url, *, timeout, trust_env, follow_redirects):
        assert timeout <= 0.2
        assert trust_env is False and follow_redirects is False
        raise httpx.ReadTimeout("busy model metrics")

    monkeypatch.setattr(jetson.httpx, "get", busy)
    assert provider._metrics() == {}


def test_real_busy_metrics_http_does_not_wait_for_model_work():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    entered = threading.Event()
    release = threading.Event()

    class BusyHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            entered.set()
            release.wait(3)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), BusyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        provider = jetson.LocalJetsonLlamaCppTelemetryProvider(f"http://127.0.0.1:{server.server_port}")
        assert provider._metrics() == {}
        assert entered.is_set() and not release.is_set()
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_nvmap_cache_preserves_observation_time_and_refreshes_once_expired(monkeypatch):
    clock, reads = [100.0], []
    monkeypatch.setattr(jetson.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(jetson, "_nvmap_cache", {"at": float("-inf"), "table": None})

    def read():
        reads.append(clock[0])
        clock[0] += 0.1
        return "llama 0 42 1024K\n"

    monkeypatch.setattr(jetson, "_read_nvmap_clients", read)
    assert jetson._nvmap_clients_mib() == {42: 1}
    first = dict(jetson._nvmap_cache["observation"])
    clock[0] = 101
    assert jetson._nvmap_clients_mib() == {42: 1}
    cached = jetson._nvmap_cache["observation"]
    assert cached["cache_hit"] is True
    assert cached["observed_at_monotonic"] == first["observed_at_monotonic"] == 100
    clock[0] = 103
    jetson._nvmap_clients_mib()
    assert reads == [100, 103]
    assert jetson._nvmap_cache["observation"]["observed_at_monotonic"] == 103


def test_sudo_nvmap_probe_uses_short_timeout_and_returns_unavailable(monkeypatch):
    import subprocess

    monkeypatch.setattr(jetson.Path, "read_text", lambda *a, **k: (_ for _ in ()).throw(PermissionError()))
    monkeypatch.setattr(jetson.shutil, "which", lambda name: "/usr/bin/sudo")

    def run(command, **options):
        assert command[:3] == ["/usr/bin/sudo", "-n", "cat"]
        assert options["timeout"] <= 0.2
        raise subprocess.TimeoutExpired(command, options["timeout"])

    monkeypatch.setattr(jetson.subprocess, "run", run)
    assert jetson._read_nvmap_clients() is None


def test_listener_cache_revalidates_socket_and_process_identity(monkeypatch, tmp_path):

    proc = tmp_path / "proc"
    (proc / "net").mkdir(parents=True)
    for pid in (1, 2, 3):
        (proc / str(pid)).mkdir()
    (proc / "net/tcp").write_text(
        "header\n0: 0100007F:1FA4 0:0 0A 0 0 0 0 0 100\n1: 0100007F:1FAA 0:0 0A 0 0 0 0 0 200\n"
    )
    (proc / "net/tcp6").write_text("header\n")
    monkeypatch.setattr(
        jetson, "Path", lambda name: proc / str(name).removeprefix("/proc/") if str(name) != "/proc" else proc
    )
    identities = {1: "old", 2: "other", 3: "new"}
    sockets = {1: {"100"}, 2: {"200"}, 3: set()}
    calls = []
    monkeypatch.setattr(jetson, "_process_start_ticks", lambda pid: identities[pid])
    monkeypatch.setattr(jetson, "_listener_cache", {})

    def inodes(pid):
        calls.append(pid)
        return sockets[pid]

    monkeypatch.setattr(jetson, "_socket_inodes", inodes)
    assert jetson._listening_ports_by_pid({"8100"}) == {1: {"8100"}}
    calls.clear()
    assert jetson._listening_ports_by_pid({"8100"}) == {1: {"8100"}}
    assert calls == [1], "cached topology should inspect only validated holders"
    # gpu_stats checks only the model port; process_memory checks the whole
    # frozen topology. Alternating those queries must not evict each other.
    assert jetson._listening_ports_by_pid({"8100", "8106"}) == {1: {"8100"}, 2: {"8106"}}
    calls.clear()
    assert jetson._listening_ports_by_pid({"8100"}) == {1: {"8100"}}
    assert calls == [1], "a different port selection must not force another full /proc scan"
    calls.clear()
    assert jetson._listening_ports_by_pid({"8100", "8106"}) == {1: {"8100"}, 2: {"8106"}}
    assert calls == [1, 2]
    # A reused PID with the same socket inode must not inherit old ownership.
    identities[1] = "reused"
    sockets[1], sockets[3] = set(), {"100"}
    assert jetson._listening_ports_by_pid({"8100"}) == {3: {"8100"}}


def test_pid_identity_change_invalidates_collected_memory(monkeypatch):
    from photobench.domain.runtime.metrics import memory_freshness

    clock = [100.0]
    monkeypatch.setattr(jetson.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        jetson,
        "_host_process_rows",
        lambda ports: [
            {
                "pid": 42,
                "ppid": 0,
                "start_ticks": "old",
                "rss_mib": 10,
                "listening_ports": [8100],
            }
        ],
    )
    identities = iter(["old", "new", "new"])
    monkeypatch.setattr(jetson, "_process_start_ticks", lambda pid: next(identities))
    monkeypatch.setattr(jetson, "_process_smaps_memory_mib", lambda pid: (8, 10))
    monkeypatch.setattr(jetson, "_nvmap_cache", {"at": float("-inf"), "table": None})
    monkeypatch.setattr(jetson, "_read_nvmap_clients", lambda: "llama 0 42 20480K\n")
    data = jetson._augment_local_process_memory(
        {},
        model_pid=None,
        endpoint_port="8100",
        telemetry_services={"main_model": {"ports": [8100]}},
    )
    result, _ = memory_freshness({**data, "_t": 100.1})
    assert result["model_process_system_memory_used_mib"] is None
    assert result["product_stack_memory_mib"] is None


def test_probe_budget_stops_additional_smaps_and_does_not_claim_complete_memory(monkeypatch):
    clock, walks = [100.0], []
    monkeypatch.setattr(jetson.time, "monotonic", lambda: clock[0])
    rows = [
        {
            "pid": pid,
            "ppid": 0,
            "start_ticks": str(pid),
            "rss_mib": 10,
            "listening_ports": [8100 if pid == 1 else 8103],
        }
        for pid in (1, 2)
    ]
    monkeypatch.setattr(jetson, "_host_process_rows", lambda ports: rows)
    monkeypatch.setattr(jetson, "_process_start_ticks", lambda pid: str(pid))

    def smaps(pid):
        walks.append(pid)
        clock[0] += 1.2
        return 8, 10

    monkeypatch.setattr(jetson, "_process_smaps_memory_mib", smaps)
    monkeypatch.setattr(jetson, "_nvmap_cache", {"at": float("-inf"), "table": None})
    monkeypatch.setattr(jetson, "_read_nvmap_clients", lambda: "llama 0 1 20480K\nclip 0 2 10240K\n")
    result = jetson._augment_local_process_memory(
        {},
        model_pid=None,
        endpoint_port="8100",
        telemetry_services={
            "main_model": {"ports": [8100]},
            "embedding": {
                "mode": "clip",
                "profiles": {"clip": {"ports": [8103]}},
            },
        },
    )
    assert walks == [1]
    assert result["product_stack_memory_mib"] is None
    assert result["memory_observations"]["process:2:pss"]["reason"] == "process_probe_budget_exhausted"


def test_port_discovery_inspects_only_actual_listener_candidates(monkeypatch, tmp_path):
    provider = jetson.LocalJetsonLlamaCppTelemetryProvider(
        "http://127.0.0.1:8104", pid_file=str(tmp_path / "absent" / "pid")
    )
    monkeypatch.setattr(jetson, "_listening_ports_by_pid", lambda ports: {42: {"8104"}, 43: {"8104"}})
    seen = []

    def verify(pid):
        seen.append(pid)
        if pid == 42:
            raise ValueError("listener is not the selected inference server")
        return pid

    monkeypatch.setattr(provider, "_pid_from_proc", verify)
    # No /proc directory inventory exists in this test; only listener IDs are usable.
    from pathlib import Path

    monkeypatch.setattr(
        jetson, "Path", lambda value: tmp_path / "missing-proc" if str(value) == "/proc" else Path(value)
    )
    assert provider._pid_from_port() == 43
    assert seen == [42, 43]
    monkeypatch.setattr(jetson, "_listening_ports_by_pid", lambda ports: {})
    with pytest.raises(ValueError, match="no llama-server"):
        provider._pid_from_port()


def test_scoped_process_inventory_reads_details_only_for_listeners_and_new_children(monkeypatch, tmp_path):
    from pathlib import Path

    proc = tmp_path / "proc"
    proc.mkdir()

    def write_stat(pid, parent, ticks="123"):
        directory = proc / str(pid)
        directory.mkdir(exist_ok=True)
        fields = ["S", str(parent)] + ["0"] * 17 + [ticks]
        (directory / "stat").write_text(f"{pid} (name with spaces) " + " ".join(fields))

    for pid, parent in ((1, 0), (2, 1), (3, 2), (8, 0)):
        write_stat(pid, parent)
    monkeypatch.setattr(
        jetson,
        "Path",
        lambda value: (
            proc / str(value).removeprefix("/proc/")
            if str(value).startswith("/proc/")
            else proc
            if str(value) == "/proc"
            else Path(value)
        ),
    )
    monkeypatch.setattr(
        jetson,
        "_listening_ports_by_pid",
        lambda ports: {1: {"8104"}, 8: {"8106"}} if not ports else {1: {"8104"}},
    )
    reads = []
    monkeypatch.setattr(jetson, "_process_rss_mib", lambda pid: reads.append(pid) or 10.0)
    monkeypatch.setattr(jetson, "_process_cmdline", lambda pid: "/usr/bin/llama-server --port 8104")
    rows = jetson._host_process_rows({"8104"})
    assert {r["pid"] for r in rows} == {1, 2, 3}
    assert set(reads) == {1, 2, 3}, "unrelated same-name models must not incur RSS/command probes"
    write_stat(4, 1)
    assert {r["pid"] for r in jetson._host_process_rows({"8104"})} == {1, 2, 3, 4}
    assert {r["pid"] for r in jetson._host_process_rows()} == {1, 2, 3, 4, 8}

    def reuse(pid):
        if pid == 2:
            write_stat(2, 1, "reused")
        return 10.0

    monkeypatch.setattr(jetson, "_process_rss_mib", reuse)
    assert 2 not in {r["pid"] for r in jetson._host_process_rows({"8104"})}


@pytest.mark.parametrize("selector", ["legacy", "pid", "keyword", "remote_keyword", "port"])
def test_port_inventory_optimization_preserves_non_port_service_selectors(monkeypatch, selector):
    main = {"ports": [8104]}
    product = {"ports": [8291]}
    if selector == "pid":
        product = {"pids": [7], "ports": [8291]}
    elif selector == "keyword":
        product = {"keywords": ["sentrix-private-worker"]}
    elif selector == "remote_keyword":
        product = {"keywords": ["sentrix-private-worker"], "local": False}
    scope = {} if selector == "legacy" else {"main_model": main, "product": [product]}
    rows = [
        {
            "pid": 1,
            "ppid": 0,
            "start_ticks": "1",
            "listening_ports": [8104],
            "identity": "llama-server --port 8104",
            "rss_mib": 10,
        },
        {
            "pid": 7,
            "ppid": 0,
            "start_ticks": "7",
            "listening_ports": [],
            "identity": "sentrix-private-worker",
            "rss_mib": 10,
        },
    ]
    selections, sampled = [], []

    def inventory(ports):
        selections.append(ports)
        return rows if ports is None else rows[:1]

    monkeypatch.setattr(jetson, "_host_process_rows", inventory)
    monkeypatch.setattr(jetson, "_process_start_ticks", lambda pid: str(pid))
    monkeypatch.setattr(jetson, "_process_smaps_memory_mib", lambda pid: sampled.append(pid) or (8, 10))
    monkeypatch.setattr(jetson, "SYSTEM_PROCESS_KEYWORDS", ("sentrix-private-worker",))
    monkeypatch.setattr(jetson, "_nvmap_cache", {"at": float("-inf"), "table": None})
    monkeypatch.setattr(jetson, "_read_nvmap_clients", lambda: "llama 0 1 20480K\n")
    jetson._augment_local_process_memory({}, model_pid=1, endpoint_port="8104", telemetry_services=scope)
    if selector in {"legacy", "pid", "keyword"}:
        assert selections == [None]
        assert sampled == [1, 7], "non-listening local services must not disappear from accounting"
    else:
        assert selections[0] is not None
        assert sampled == [1], "explicit port scope must not match unrelated same-name services"
