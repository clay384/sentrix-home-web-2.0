from __future__ import annotations

import json
import select
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from scripts.benchmarks.remote_vllm_capacity import (
    Journal,
    SafetyGuard,
    capacity_report,
    client_sample,
    output_lease,
    parse_metrics,
    request_once,
    run_round,
    validate_endpoint,
)


def test_metric_versions_model_filter_and_missing_are_explicit() -> None:
    raw = """# HELP ignored text
vllm:num_requests_running{engine="0",model_name="wanted"} 2
vllm:num_requests_running{engine="1",model_name="wanted"} 3
vllm:num_requests_running{engine="0",model_name="other"} 90
vllm:gpu_cache_usage_perc{model_name="wanted"} 0.2
vllm:time_to_first_token_seconds_sum{model_name="wanted"} 6
vllm:time_to_first_token_seconds_count{model_name="wanted"} 3
vllm:prompt_tokens_total{model_name="wanted"} NaN
"""
    result = parse_metrics(raw, "wanted")
    assert result["running"] == 5
    assert result["kv_usage_fraction"] == 0.2
    assert result["ttft_sum_s"] == 6
    assert result["ttft_count"] == 3
    assert result["waiting"] is None
    assert result["prompt_tokens_total"] is None
    result = parse_metrics(raw + '\nvllm:kv_cache_usage_perc{model_name="wanted"} 0.4', "wanted")
    assert result["kv_usage_fraction"] == 0.4


def sample(waiting: int = 0, preemptions: int = 0, swap: int = 0, available: int = 20) -> dict[str, Any]:
    return {
        "remote_model": {"metrics": {"running": 1, "waiting": waiting, "preemptions_total": preemptions}},
        "remote_host": {"available_bytes": available * 2**30, "swap_out_pages": swap},
    }


@pytest.mark.parametrize("field", ["preemption", "restart", "memory", "metrics"])
def test_guard_stops_on_resource_or_observability_fault(field: str) -> None:
    guard = SafetyGuard(8 * 2**30)
    assert guard.check(sample(preemptions=2)) is None
    if field == "preemption":
        assert guard.check(sample(preemptions=3)) is None
        assert guard.preemptions_since_baseline == 1
        assert guard.check(sample(preemptions=4)) == "repeated_kv_preemption"
    elif field == "restart":
        assert guard.check(sample()) == "model_counter_reset_possible_restart"
    elif field == "memory":
        assert guard.check(sample(available=7)) == "remote_available_memory_below_guard_or_unavailable"
    else:
        assert (
            guard.check({"remote_model": {"metrics": parse_metrics("")}})
            == "required_model_metrics_unavailable"
        )


def test_queue_and_swap_require_sustained_growth() -> None:
    guard = SafetyGuard()
    assert guard.check(sample(waiting=0)) is None
    assert guard.check(sample(waiting=1)) is None
    assert guard.check(sample(waiting=2)) is None
    assert guard.check(sample(waiting=3)) == "persistent_wait_queue_growth"
    guard = SafetyGuard()
    assert guard.check(sample(swap=0)) is None
    assert guard.check(sample(swap=1)) is None
    assert guard.check(sample(swap=2)) == "sustained_remote_swap_growth"


def qualified(n: int, rate: float, round_no: int) -> dict[str, Any]:
    return {
        "concurrency": n,
        "passed": True,
        "elapsed_seconds": 1800,
        "completed": int(rate * 1800),
        "resumed": False,
        "round_id": f"c{n}-r{round_no}",
    }


def test_report_prefers_lower_within_five_percent_and_does_not_invent_boundary() -> None:
    rows = [qualified(n, rate, r) for n, rate in [(1, 1.0), (2, 1.04), (4, 1.03)] for r in [1, 2]]
    report = capacity_report(rows)
    assert report["recommended_concurrency"] == 1
    assert report["qualified_stable_at_least"] == 4
    assert report["maximum_stable_concurrency"] is None
    assert not report["failure_boundary_observed"]
    rows.append({"concurrency": 8, "passed": False})
    assert capacity_report(rows)["maximum_stable_concurrency"] is None
    rows.append({"concurrency": 5, "passed": False})
    assert capacity_report(rows)["maximum_stable_concurrency"] is None
    derived = [dict(row) for row in rows]
    derived[-1].update(
        capacity_boundary_confirmed=True,
        capacity_boundary_reason="Owned model OOM with fixed workload and no competing load",
        capacity_boundary_evidence=["sha256:reviewed-model-log", "sha256:reviewed-host-samples"],
    )
    assert capacity_report(derived)["maximum_stable_concurrency"] == 4
    assert "capacity_boundary_confirmed" not in rows[-1]


def test_short_or_resumed_rounds_cannot_qualify_as_stable() -> None:
    a, b = qualified(2, 1, 1), qualified(2, 1, 2)
    a["elapsed_seconds"] = b["elapsed_seconds"] = 900
    assert capacity_report([a, b])["recommended_concurrency"] is None
    a["elapsed_seconds"] = 1800
    b["resumed"] = True
    assert capacity_report([a, b])["recommended_concurrency"] is None


def test_client_resource_attribution_never_claims_remote_gpu() -> None:
    result = client_sample()
    assert result["scope"] == "load_generator_process_not_remote_model"
    assert result["host"]
    assert "gpu" not in result


def test_endpoint_rejects_protected_host_credentials_and_unapproved_destination() -> None:
    with pytest.raises(ValueError, match="protected"):
        validate_endpoint("http://192.168.0.153:8772/v1", "192.168.0.153")
    with pytest.raises(ValueError):
        validate_endpoint("http://secret:key@localhost:18104/v1", "localhost")
    with pytest.raises(ValueError):
        validate_endpoint("http://localhost:18104/v1", "192.168.0.100")


@pytest.fixture
def endpoint() -> Any:
    class Handler(BaseHTTPRequestHandler):
        posts = 0
        fail = False
        http_error = False
        peak = 0
        active = 0
        lock = threading.Lock()

        def log_message(self, _format: str, *args: Any) -> None:
            pass

        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(
                b"vllm:num_requests_running 0\nvllm:num_requests_waiting 0\nvllm:num_preemptions_total 0\n"
            )

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            with self.lock:
                type(self).posts += 1
                type(self).active += 1
                type(self).peak = max(type(self).peak, type(self).active)
            if self.fail:
                self.close_connection = True
            elif self.http_error:
                self.send_response(429)
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n')
                self.wfile.write(
                    b'data: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":1}}\n\n'
                )
                self.wfile.write(b"data: [DONE]\n\n")
            with self.lock:
                type(self).active -= 1

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", Handler
    server.shutdown()
    thread.join()
    server.server_close()


def test_real_http_stream_and_durable_state(endpoint: Any, tmp_path: Path) -> None:
    base, handler = endpoint
    journal = Journal(tmp_path / "requests.jsonl")
    result = request_once(
        base, {"messages": [{"role": "user", "content": "ok"}], "max_tokens": 8}, "unique-1", 1, journal
    )
    assert result["state"] == "completed"
    assert result["ttft_seconds"] is not None
    assert result["usage"]["completion_tokens"] == 1
    events = [json.loads(line) for line in journal.path.read_text().splitlines()]
    assert [row["state"] for row in events] == ["request_outcome_unknown", "completed"]
    assert "content" not in journal.path.read_text()
    assert handler.posts == 1
    with pytest.raises(ValueError, match="refusing replay"):
        request_once(base, {"messages": [], "max_tokens": 8}, "unique-1", 1, journal)
    assert handler.posts == 1


def test_dropped_post_is_not_retried_and_resume_quarantines_unknown(endpoint: Any, tmp_path: Path) -> None:
    base, handler = endpoint
    handler.fail = True
    journal = Journal(tmp_path / "requests.jsonl")
    result = request_once(
        base, {"messages": [{"role": "user", "content": "ok"}], "max_tokens": 8}, "c1-r1-000000", 1, journal
    )
    assert result["state"] == "request_outcome_unknown"
    assert handler.posts == 1
    result = run_round(
        base=base,
        model="test",
        bodies=[{"messages": []}],
        concurrency=1,
        count=2,
        duration_s=0,
        max_requests=2,
        timeout=1,
        round_id="c1-r1",
        output=tmp_path,
        stop=threading.Event(),
        sample_interval=0.1,
        ssh_target=None,
        min_available_bytes=0,
    )
    assert result["failure"] == "unknown_requests_quarantined_no_replay"
    assert handler.posts == 1


def test_bounded_dispatch_and_completed_ids_not_replayed(endpoint: Any, tmp_path: Path) -> None:
    base, handler = endpoint
    kwargs = dict(
        base=base,
        model="test",
        bodies=[{"messages": [{"role": "user", "content": "x"}]}],
        concurrency=2,
        count=4,
        duration_s=0,
        max_requests=4,
        timeout=1,
        round_id="c2-r1",
        output=tmp_path,
        sample_interval=0.1,
        ssh_target=None,
        min_available_bytes=0,
    )
    result = run_round(**kwargs, stop=threading.Event())
    assert result["passed"]
    assert handler.posts == 4
    assert handler.peak <= 2
    result = run_round(**kwargs, stop=threading.Event())
    assert result["resumed"]
    assert handler.posts == 4


def test_duplicate_round_cannot_satisfy_repeat_requirement() -> None:
    row = qualified(2, 1, 1)
    with pytest.raises(ValueError, match="duplicate round"):
        capacity_report([row, row])


def test_http_failure_is_not_completion_or_replayed_on_resume(endpoint: Any, tmp_path: Path) -> None:
    base, handler = endpoint
    handler.http_error = True
    kwargs = dict(
        base=base,
        model="test",
        bodies=[{"messages": [{"role": "user", "content": "x"}]}],
        concurrency=1,
        count=4,
        duration_s=0,
        max_requests=4,
        timeout=1,
        round_id="c1-r1",
        output=tmp_path,
        sample_interval=0.1,
        ssh_target=None,
        min_available_bytes=0,
    )
    first = run_round(**kwargs, stop=threading.Event())
    assert first["completed"] == 0
    assert first["finished"] == first["failed"] == 1
    assert first["requests_per_second"] == 0
    assert not first["passed"]
    assert handler.posts == 1
    second = run_round(**kwargs, stop=threading.Event())
    assert second["failure"] == "previous_request_failure_no_replay"
    assert not second["passed"]
    assert handler.posts == 1
    assert capacity_report([second])["observed_pass_at_least"] is None


def test_output_lease_rejects_a_second_process_but_allows_other_run(tmp_path: Path) -> None:
    code = """
import sys
from pathlib import Path
from scripts.benchmarks.remote_vllm_capacity import output_lease
with output_lease(Path(sys.argv[1])):
    print('LOCKED', flush=True)
    sys.stdin.read(1)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert select.select([child.stdout], [], [], 10)[0]
        assert child.stdout.readline().strip() == "LOCKED"
        with pytest.raises(RuntimeError, match="already in use"), output_lease(tmp_path):
            pass
        other = tmp_path / "independent-client"
        other.mkdir()
        with output_lease(other):
            pass
    finally:
        child.communicate(input="x", timeout=5)
    assert child.returncode == 0
    with output_lease(tmp_path):
        pass


def test_one_preemption_is_evidence_and_two_accumulate_across_quiet_intervals() -> None:
    guard = SafetyGuard()
    assert guard.check(sample(preemptions=100)) is None
    assert guard.check(sample(preemptions=101)) is None
    assert guard.preemptions_since_baseline == 1
    assert guard.check(sample(preemptions=101)) is None
    assert guard.check(sample(preemptions=101)) is None
    assert guard.check(sample(preemptions=102)) == "repeated_kv_preemption"
    assert guard.preemption_baseline == 100
    assert guard.preemptions_since_baseline == 2


@pytest.mark.parametrize("counter", ["prompt_tokens_total", "output_tokens_total"])
def test_token_counter_reset_stops_even_when_preemptions_stay_zero(counter: str) -> None:
    guard = SafetyGuard()
    before, after = sample(), sample()
    before["remote_model"]["metrics"][counter] = 100
    after["remote_model"]["metrics"][counter] = 0
    assert guard.check(before) is None
    assert guard.check(after) == "model_counter_reset_possible_restart"


def test_process_restart_stops_even_without_counter_reset() -> None:
    guard = SafetyGuard()
    before, after = sample(), sample()
    before["remote_model"]["metrics"]["process_start_time"] = 100
    after["remote_model"]["metrics"]["process_start_time"] = 101
    assert guard.check(before) is None
    assert guard.check(after) == "model_process_restart"


def test_manifest_freezes_safety_protocol_and_sampling_interval(tmp_path: Path, monkeypatch: Any) -> None:
    from scripts.benchmarks import remote_vllm_capacity as cli

    identity = tmp_path / "identity.json"
    identity.write_text("{}")
    workload = tmp_path / "workload.jsonl"
    workload.write_text(json.dumps({"messages": [{"role": "user", "content": "x"}], "max_tokens": 8}))
    output = tmp_path / "observe"

    def get(url: str, timeout: float = 5) -> str:
        if url.endswith("/v1/models"):
            return json.dumps({"data": [{"id": "test", "max_model_len": 128}]})
        return "vllm:num_requests_running 0\nvllm:num_requests_waiting 0\nvllm:num_preemptions_total 0\n"

    monkeypatch.setattr(cli, "get_text", get)
    monkeypatch.setattr(cli.signal, "signal", lambda *args: None)
    assert (
        cli.main(
            [
                "--base-url",
                "http://127.0.0.1:18104/v1",
                "--allow-model-host",
                "127.0.0.1",
                "--model",
                "test",
                "--window",
                "128",
                "--identity",
                str(identity),
                "--requests",
                str(workload),
                "--output",
                str(output),
                "--observe-only-seconds",
                "0.01",
                "--sample-interval-s",
                "0.1",
            ]
        )
        == 0
    )
    plan = json.loads((output / "manifest.json").read_text())["plan"]
    assert plan["safety_protocol"] == cli.SAFETY_PROTOCOL
    assert plan["safety_protocol"]["preemptions_since_baseline_stop_at"] == 2
    assert plan["safety_protocol"]["queue_consecutive_growth_intervals_stop_at"] == 3
    assert plan["safety_protocol"]["swap_consecutive_growth_intervals_stop_at"] == 2
    assert plan["sample_interval_s"] == 0.1
    row = json.loads((output / "observe.metrics.jsonl").read_text().splitlines()[0])
    assert row["safety_observation"]["preemptions_since_baseline"] == 0


@pytest.mark.parametrize(
    "failure",
    [
        "http_429",
        "invalid_payload_400",
        "sampler_failure:OSError",
        "operator_stopped",
        "external_dflash_swap",
        "request_outcome_unknown",
    ],
)
def test_trial_failures_are_inconclusive_without_confirmed_capacity_evidence(failure: str) -> None:
    rows = [qualified(2, 1, 1), qualified(2, 1, 2), {"concurrency": 3, "passed": False, "failure": failure}]
    report = capacity_report(rows)
    assert report["qualified_stable_at_least"] == 2
    assert report["maximum_stable_concurrency"] is None
    assert report["failure_boundary_observed"] is False
    assert report["failed_trial_concurrencies"] == [3]
    assert report["inconclusive_failed_concurrencies"] == [3]


@pytest.mark.parametrize(
    "annotation",
    [
        {"capacity_boundary_confirmed": True},
        {
            "capacity_boundary_confirmed": True,
            "capacity_boundary_reason": "",
            "capacity_boundary_evidence": ["log"],
        },
        {
            "capacity_boundary_confirmed": True,
            "capacity_boundary_reason": "OOM",
            "capacity_boundary_evidence": [],
        },
        {
            "capacity_boundary_confirmed": True,
            "capacity_boundary_reason": "OOM",
            "capacity_boundary_evidence": [""],
        },
    ],
)
def test_boundary_flag_requires_nonempty_reason_and_evidence(annotation: dict[str, Any]) -> None:
    rows = [qualified(2, 1, 1), qualified(2, 1, 2), {"concurrency": 3, "passed": False, **annotation}]
    report = capacity_report(rows)
    assert report["maximum_stable_concurrency"] is None
    assert report["capacity_boundary_confirmations"] == []
    assert report["inconclusive_failed_concurrencies"] == [3]


def test_confirmed_boundary_must_be_adjacent_and_duration_cannot_sum_short_rounds() -> None:
    boundary = {
        "concurrency": 4,
        "passed": False,
        "capacity_boundary_confirmed": True,
        "capacity_boundary_reason": "reviewed model capacity failure",
        "capacity_boundary_evidence": ["reviewed-log.json"],
    }
    rows = [qualified(2, 1, 1), qualified(2, 1, 2), boundary]
    report = capacity_report(rows)
    assert report["failure_boundary_observed"]
    assert report["maximum_stable_concurrency"] is None
    rows[0]["elapsed_seconds"] = rows[1]["elapsed_seconds"] = 900
    boundary["concurrency"] = 3
    report = capacity_report(rows)
    assert report["qualified_stable_at_least"] is None
    assert report["maximum_stable_concurrency"] is None
