"""Real independent Linux processes, fake HTTP: no service/cloud/model requests."""

import concurrent.futures
import json
import multiprocessing
import os
import threading
import time
from pathlib import Path

import httpx
import pytest

import photobench.application.run_execution.judge as jm
from photobench.domain.errors import RequestOutcomeUnknownError, SharedJudgeGateError
from photobench.domain.runs.execution import RunCancelledError
from photobench.infrastructure.http.client import HttpxJsonClient
from photobench.infrastructure.http.shared_gate import SharedAccountGate

URL = "https://unit.invalid/v1/chat/completions"
KEY = "Bearer DUMMY_PRIVATE_CREDENTIAL"
GOOD = {"choices": [{"message": {"content": '{"score":2,"reason":"正确"}'}, "finish_reason": "stop"}]}


def _child(root, scope, interval, count, hold, barrier, queue):
    jm.JUDGE_REQUEST_INTERVAL_SECONDS = 0
    gate = SharedAccountGate(Path(root), scope, interval_seconds=interval, max_in_flight=2)

    def handler(request):
        queue.put(("start", os.getpid(), time.monotonic()))
        time.sleep(hold)
        queue.put(("finish", os.getpid(), time.monotonic()))
        return httpx.Response(200, json=GOOD)

    runner = jm.JudgeRunner(
        http=HttpxJsonClient(httpx.Client(transport=httpx.MockTransport(handler)), shared_gate=gate),
        judge_url="https://unit.invalid/v1",
        judge_model="judge",
        judge_api_key=KEY[7:],
        http_attempt_evidence=True,
    )
    barrier.wait(timeout=10)
    for _ in range(count):
        assert runner._request({"messages": [{"content": "PRIVATE_BODY"}]}) == (GOOD, 1)
    queue.put(("metrics", os.getpid(), runner.request_metrics()))


def _crash_owner(root, queue):
    gate = SharedAccountGate(Path(root), "crash-account", interval_seconds=0.01)
    lease = gate.acquire(URL, KEY)
    queue.put((os.getpid(), lease))
    time.sleep(30)


def make_gate(tmp_path, scope="budget", **kw):
    return SharedAccountGate(tmp_path, scope, interval_seconds=kw.pop("interval_seconds", 0.01), **kw)


def runner(tmp_path, handler, *, gate=None, cancel=None):
    return jm.JudgeRunner(
        http=HttpxJsonClient(
            httpx.Client(transport=httpx.MockTransport(handler)), shared_gate=gate or make_gate(tmp_path)
        ),
        judge_url="https://unit.invalid/v1",
        judge_model="judge",
        judge_api_key=KEY[7:],
        cancel=cancel,
        http_attempt_evidence=True,
        sleep=lambda x: None,
        rand=lambda a, b: 0,
    )


@pytest.mark.parametrize("clients", [2, 4])
def test_real_processes_share_pacing_and_two_actual_HTTP_slots(tmp_path, clients):
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    barrier = ctx.Barrier(clients)
    ps = [
        ctx.Process(target=_child, args=(str(tmp_path), "same-account", 0.05, 2, 0.14, barrier, queue))
        for _ in range(clients)
    ]
    for p in ps:
        p.start()
    print(
        "gate_test_children",
        json.dumps(
            [
                {
                    "pid": p.pid,
                    "start_ticks": Path(f"/proc/{p.pid}/stat").read_text().rsplit(")", 1)[1].split()[19],
                }
                for p in ps
            ]
        ),
    )
    events = [queue.get(timeout=15) for _ in range(clients * 5)]
    for p in ps:
        p.join(10)
        assert p.exitcode == 0
    starts = sorted(x[2] for x in events if x[0] == "start")
    assert len(starts) == clients * 2
    assert min(b - a for a, b in zip(starts, starts[1:], strict=False)) >= 0.045
    active = peak = 0
    for event in sorted([x for x in events if x[0] in ["start", "finish"]], key=lambda x: x[2]):
        active += 1 if event[0] == "start" else -1
        peak = max(peak, active)
    assert peak == 2 and active == 0
    for event in events:
        if event[0] == "metrics":
            m = event[2]
            assert m["requests_started"] == 2 and m["responses_received"] == 2 and m["in_flight"] == 0
            assert m["gate_wait_ms"] > 0 and m["gate_failures"] == 0
    state = json.loads((tmp_path / "same-account/state.json").read_text())
    assert state["active"] == {} and state["blocked"] is None


def test_real_processes_default_two_second_global_interval(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    barrier = ctx.Barrier(2)
    ps = [
        ctx.Process(target=_child, args=(str(tmp_path), "default-account", 2, 1, 0.02, barrier, queue))
        for _ in range(2)
    ]
    for p in ps:
        p.start()
    print(
        "gate_test_children",
        json.dumps(
            [
                {
                    "pid": p.pid,
                    "start_ticks": Path(f"/proc/{p.pid}/stat").read_text().rsplit(")", 1)[1].split()[19],
                }
                for p in ps
            ]
        ),
    )
    events = [queue.get(timeout=10) for _ in range(6)]
    for p in ps:
        p.join(10)
        assert p.exitcode == 0
    starts = sorted(x[2] for x in events if x[0] == "start")
    assert 1.99 <= starts[1] - starts[0] <= 2.5


def test_identity_mismatch_blocks_only_its_scope_and_never_logs_credential(tmp_path):
    gate = make_gate(tmp_path, "group-a")
    lease = gate.acquire(URL, KEY)
    gate.finish(lease["lease_id"], "http_response", http_status=200)
    with pytest.raises(SharedJudgeGateError, match="identity_mismatch"):
        gate.acquire(URL, "Bearer DIFFERENT_PRIVATE_CREDENTIAL")
    with pytest.raises(SharedJudgeGateError, match="blocked"):
        gate.acquire(URL, KEY)
    other = make_gate(tmp_path, "group-b")
    lease = other.acquire(URL, KEY)
    other.finish(lease["lease_id"], "not_sent")
    public = (tmp_path / "group-a/journal.jsonl").read_text()
    assert "DUMMY_PRIVATE_CREDENTIAL" not in public and "fingerprint" not in public and URL not in public
    for p in (tmp_path / "group-a").iterdir():
        assert p.stat().st_mode & 0o077 == 0


def test_owner_crash_retains_unknown_lease_and_stops_next_process(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    p = ctx.Process(target=_crash_owner, args=(str(tmp_path), queue))
    p.start()
    pid, lease = queue.get(timeout=10)
    assert pid == p.pid
    print("gate_crash_owned_child", json.dumps({"pid": pid, "owner": lease["owner"]}))
    p.kill()
    p.join(5)
    assert p.exitcode is not None
    gate = make_gate(tmp_path, "crash-account")
    with pytest.raises(SharedJudgeGateError, match="blocked"):
        gate.acquire(URL, KEY)
    state = json.loads((tmp_path / "crash-account/state.json").read_text())
    assert state["active"][lease["lease_id"]]["status"] == "unknown" and state["blocked"]


def test_429_releases_before_original_backoff_and_every_retry_gets_new_permit(tmp_path, monkeypatch):
    monkeypatch.setattr(jm, "JUDGE_REQUEST_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(jm, "JUDGE_RETRY_ATTEMPTS", 2)
    calls = []
    backoff = []

    def handler(request):
        calls.append(1)
        return (
            httpx.Response(429, json={"error": {"code": "AccountRateLimitExceeded"}})
            if len(calls) == 1
            else httpx.Response(200, json=GOOD)
        )

    r = runner(tmp_path, handler)

    def wait(seconds):
        state = json.loads((tmp_path / "budget/state.json").read_text())
        assert state["active"] == {}
        backoff.append(seconds)

    r.sleep = wait
    assert r._request({"messages": []}) == (GOOD, 2)
    m = r.request_metrics()
    assert (
        m["requests_started"] == 2
        and m["request_errors"] == 1
        and m["http_429"] == 1
        and m["responses_received"] == 1
        and m["in_flight"] == 0
    )
    acquired = [
        json.loads(x)
        for x in (tmp_path / "budget/journal.jsonl").read_text().splitlines()
        if json.loads(x)["event"] == "acquired"
    ]
    assert len(acquired) == 2 and acquired[0]["lease_id"] != acquired[1]["lease_id"] and len(backoff) == 1


def test_connect_retry_releases_but_uncertain_read_fails_closed_without_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(jm, "JUDGE_REQUEST_INTERVAL_SECONDS", 0)
    calls = []

    def connect(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("PRIVATE_BODY", request=request)
        return httpx.Response(200, json=GOOD)

    r = runner(tmp_path, connect)
    assert r._request({}) == (GOOD, 1)
    m = r.request_metrics()
    assert m["requests_started"] == 2 and m["request_errors"] == 1 and m["responses_received"] == 1
    other = tmp_path / "uncertain"
    other.mkdir(mode=0o700)
    reads = []

    def uncertain(request):
        reads.append(1)
        raise httpx.ReadTimeout("PRIVATE_BODY", request=request)

    r = runner(other, uncertain)
    with pytest.raises(RequestOutcomeUnknownError):
        r._request({})
    assert len(reads) == 1
    state = json.loads((other / "budget/state.json").read_text())
    assert state["blocked"] and len(state["active"]) == 1
    with pytest.raises(SharedJudgeGateError):
        r._request({})
    assert len(reads) == 1 and r.request_metrics()["in_flight"] == 0


def test_gate_wait_and_failure_are_not_cloud_requests_or_quality_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(jm, "JUDGE_REQUEST_INTERVAL_SECONDS", 0)
    gate = make_gate(tmp_path, max_in_flight=1)
    held = gate.acquire(URL, KEY)
    cancel = threading.Event()
    calls = []
    r = runner(
        tmp_path, lambda request: calls.append(1) or httpx.Response(200, json=GOOD), gate=gate, cancel=cancel
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(r._request, {})
        deadline = time.monotonic() + 2
        while not r.request_metrics()["gate_waiting"]:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert r.request_metrics()["requests_started"] == 0
        cancel.set()
        with pytest.raises(RunCancelledError):
            future.result(2)
    gate.finish(held["lease_id"], "http_response", http_status=200)
    assert (
        calls == []
        and r.request_metrics()["requests_started"] == 0
        and r.request_metrics()["gate_waiting"] == 0
    )
    state = json.loads((tmp_path / "budget/state.json").read_text())
    state["blocked"] = {"reason": "test"}
    (tmp_path / "budget/state.json").write_text(json.dumps(state))
    r = runner(tmp_path, lambda request: pytest.fail("gate failure must be zero HTTP"), gate=gate)
    result = r.judge("PRIVATE_BODY", "reference", "answer")
    assert result["score"] is None and result["operational_issue"]["kind"] == "configuration_error"
    assert r.request_metrics()["requests_started"] == 0 and r.request_metrics()["gate_failures"] == 1


def test_complete_response_json_error_releases_transport_without_claiming_still_inflight(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(jm, "JUDGE_REQUEST_INTERVAL_SECONDS", 0)
    r = runner(tmp_path, lambda request: httpx.Response(200, content=b"not json"))
    with pytest.raises(RequestOutcomeUnknownError):
        r._request({})
    state = json.loads((tmp_path / "budget/state.json").read_text())
    assert state["active"] == {} and state["blocked"] is None
    assert r.request_metrics()["requests_started"] == 1 and r.request_metrics()["request_errors"] == 1


def test_config_default_off_partial_invalid_permissions_and_symlinks_fail_closed(tmp_path, monkeypatch):
    prefix = "PHOTOBENCH_JUDGE_SHARED_GATE_"
    for key in list(os.environ):
        if key.startswith(prefix):
            monkeypatch.delenv(key)
    assert SharedAccountGate.from_env() is None
    monkeypatch.setenv(prefix + "DIR", str(tmp_path))
    with pytest.raises(SharedJudgeGateError):
        SharedAccountGate.from_env()
    monkeypatch.setenv(prefix + "ENABLED", "1")
    monkeypatch.setenv(prefix + "SCOPE", "scope")
    assert SharedAccountGate.from_env() is not None
    (tmp_path / "scope/lock").symlink_to(tmp_path / "outside")
    with pytest.raises(SharedJudgeGateError):
        SharedAccountGate.from_env().acquire(URL, KEY)
    loose = tmp_path / "loose"
    loose.mkdir(mode=0o755)
    with pytest.raises(SharedJudgeGateError):
        make_gate(loose)


def test_lost_state_and_identity_never_reinitialize_used_gate(tmp_path):
    gate = make_gate(tmp_path)
    lease = gate.acquire(URL, KEY)
    (tmp_path / "budget/identity.json").unlink()
    (tmp_path / "budget/state.json").unlink()
    with pytest.raises(SharedJudgeGateError, match="state_missing"):
        gate.acquire(URL, KEY)
    assert lease["lease_id"] in (tmp_path / "budget/journal.jsonl").read_text()


def test_redirect_cannot_issue_unpermitted_HTTP_or_be_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(jm, "JUDGE_REQUEST_INTERVAL_SECONDS", 0)
    calls = []

    def redirect(request):
        calls.append(request.url)
        return httpx.Response(307, headers={"Location": "https://other.invalid/private"})

    r = runner(tmp_path, redirect)
    result = r.judge("PRIVATE_BODY", "reference", "answer")
    assert len(calls) == 1 and result["score"] is None
    assert result["operational_issue"]["kind"] == "configuration_error"
    state = json.loads((tmp_path / "budget/state.json").read_text())
    assert state["active"] == {} and state["blocked"]
    assert r.request_metrics()["requests_started"] == 1 and r.request_metrics()["gate_failures"] == 1


def test_PID_start_mismatch_is_unknown_and_complete_response_not_orphan(tmp_path):
    gate = make_gate(tmp_path)
    lease = gate.acquire(URL, KEY)
    gate.finish(lease["lease_id"], "http_response", http_status=200)
    assert json.loads((tmp_path / "budget/state.json").read_text())["active"] == {}
    lease = gate.acquire(URL, KEY)
    path = tmp_path / "budget/state.json"
    state = json.loads(path.read_text())
    state["active"][lease["lease_id"]]["owner"]["start_ticks"] = "stale"
    path.write_text(json.dumps(state))
    with pytest.raises(SharedJudgeGateError, match="blocked"):
        gate.acquire(URL, KEY)
    state = json.loads(path.read_text())
    assert state["active"][lease["lease_id"]]["status"] == "unknown"


def test_gate_counts_actual_attempts_when_safe_metadata_is_off(tmp_path, monkeypatch):
    monkeypatch.setattr(jm, "JUDGE_REQUEST_INTERVAL_SECONDS", 0)
    calls = []
    client = HttpxJsonClient(
        httpx.Client(
            transport=httpx.MockTransport(lambda request: calls.append(1) or httpx.Response(200, json=GOOD))
        ),
        shared_gate=make_gate(tmp_path),
    )
    r = jm.JudgeRunner(
        http=client,
        judge_url="https://unit.invalid/v1",
        judge_model="judge",
        judge_api_key=KEY[7:],
        http_attempt_evidence=False,
    )
    assert r._request({}) == (GOOD, 1)
    metrics = r.request_metrics()
    assert len(calls) == metrics["requests_started"] == metrics["responses_received"] == 1
    assert metrics["in_flight"] == 0 and "http_attempts" not in metrics


def test_unknown_is_not_retried_even_when_connection_retry_option_is_off(tmp_path, monkeypatch):
    monkeypatch.setattr(jm, "JUDGE_REQUEST_INTERVAL_SECONDS", 0)
    calls = []

    def uncertain(request):
        calls.append(1)
        raise httpx.ReadTimeout("PRIVATE_BODY", request=request)

    client = HttpxJsonClient(
        httpx.Client(transport=httpx.MockTransport(uncertain)),
        shared_gate=make_gate(tmp_path),
        retry_connections=False,
    )
    r = jm.JudgeRunner(
        http=client,
        judge_url="https://unit.invalid/v1",
        judge_model="judge",
        judge_api_key=KEY[7:],
        http_attempt_evidence=True,
    )
    with pytest.raises(RequestOutcomeUnknownError):
        r._request({})
    assert len(calls) == 1 and r.request_metrics()["requests_started"] == 1
    state = json.loads((tmp_path / "budget/state.json").read_text())
    assert state["blocked"] and len(state["active"]) == 1


def _slow_persistence_child(root, slow, event, queue):
    gate = SharedAccountGate(Path(root), "slow-io", interval_seconds=0.65)
    original = gate._audit

    def audit(name, **fields):
        original(name, **fields)
        if slow and name == "acquired":
            event.set()
            # Controlled fsync/scheduling lag while durable preparation still
            # owns the lock. This reproduces the old early-clock compression.
            time.sleep(0.5)

    gate._audit = audit
    if not slow:
        assert event.wait(10)
    observed = []
    client = HttpxJsonClient(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: queue.put(("http", time.monotonic())) or httpx.Response(200, json=GOOD)
            )
        ),
        shared_gate=gate,
    )
    client.request_json_observed(
        URL, {}, "POST", extra_headers={"Authorization": KEY}, observer=observed.append
    )
    queue.put(("evidence", [x for x in observed if x.get("dispatch_started")][-1]))


def test_slow_durable_preparation_does_not_consume_next_permit_interval(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    queue, event = ctx.Queue(), ctx.Event()
    ps = [
        ctx.Process(target=_slow_persistence_child, args=(str(tmp_path), slow, event, queue))
        for slow in (True, False)
    ]
    for p in ps:
        p.start()
    print(
        "slow_io_children",
        json.dumps(
            [
                {
                    "pid": p.pid,
                    "start_ticks": Path(f"/proc/{p.pid}/stat").read_text().rsplit(")", 1)[1].split()[19],
                }
                for p in ps
            ]
        ),
    )
    events = [queue.get(timeout=15) for _ in range(4)]
    for p in ps:
        p.join(10)
        assert p.exitcode == 0
    starts = sorted(x[1] for x in events if x[0] == "http")
    evidence = sorted(
        (x[1] for x in events if x[0] == "evidence"), key=lambda x: x["gate_permit_ready_monotonic"]
    )
    assert starts[1] - starts[0] >= 0.64
    assert evidence[0]["gate_durable_preparation_ms"] >= 500
    assert evidence[1]["gate_permit_ready_monotonic"] - evidence[0]["gate_permit_ready_monotonic"] >= 0.65
    assert all(x["gate_ready_to_dispatch_ms"] >= 0 for x in evidence)
    print("slow_io_timing", json.dumps({"actual_http_interval": starts[1] - starts[0], "attempts": evidence}))


def test_post_ready_scheduler_delay_is_measured_not_claimed_as_wire_guarantee(tmp_path, monkeypatch):
    monkeypatch.setattr(jm, "JUDGE_REQUEST_INTERVAL_SECONDS", 0)
    gate = make_gate(tmp_path, interval_seconds=0.65)
    original = gate.acquire
    first = True

    def delayed(*args, **kw):
        nonlocal first
        lease = original(*args, **kw)
        if first:
            first = False
            time.sleep(0.5)  # Deliberate delay AFTER ready, not fixable by durable IO ordering.
        return lease

    monkeypatch.setattr(gate, "acquire", delayed)
    starts = []
    r = runner(
        tmp_path, lambda request: starts.append(time.monotonic()) or httpx.Response(200, json=GOOD), gate=gate
    )
    assert r._request({}) == (GOOD, 1)
    assert r._request({}) == (GOOD, 1)
    attempts = r.request_metrics()["http_attempts"]
    assert attempts[0]["gate_ready_to_dispatch_ms"] >= 500
    assert attempts[1]["gate_permit_ready_monotonic"] - attempts[0]["gate_permit_ready_monotonic"] >= 0.65
    assert starts[1] - starts[0] < 0.6
    assert r.request_metrics()["requests_started"] == 2


def test_v1_idle_upgrade_preserves_identity_and_waits_new_ready_interval(tmp_path):
    gate = make_gate(tmp_path, interval_seconds=0.08)
    lease = gate.acquire(URL, KEY)
    gate.finish(lease["lease_id"], "http_response", http_status=200)
    directory = tmp_path / "budget"
    identity = (directory / "identity.json").read_bytes()
    state = {"version": 1, "next_allowed": 0, "active": {}, "blocked": None}
    (directory / "state.json").write_text(json.dumps(state))
    # A real v1 lock has only the durable used marker, no pacing slot.
    with (directory / "lock").open("r+b") as stream:
        stream.truncate(len(b"initialized\n"))
    before = time.monotonic()
    lease = gate.acquire(URL, KEY)
    assert lease["permit_ready_monotonic"] - before >= 0.08
    assert (directory / "identity.json").read_bytes() == identity
    state = json.loads((directory / "state.json").read_text())
    assert state["version"] == 2 and "next_allowed" not in state
    gate.finish(lease["lease_id"], "not_sent")


@pytest.mark.parametrize("blocked", [False, True])
def test_v1_active_or_blocked_scope_cannot_be_upgraded(tmp_path, blocked):
    gate = make_gate(tmp_path)
    lease = gate.acquire(URL, KEY)
    path = tmp_path / "budget/state.json"
    state = json.loads(path.read_text())
    state.update(version=1, next_allowed=0)
    if blocked:
        state["active"] = {}
        state["blocked"] = {"reason": "retained_unknown"}
    path.write_text(json.dumps(state))
    before = path.read_bytes()
    with pytest.raises(SharedJudgeGateError, match="blocked" if blocked else "legacy_active"):
        gate.acquire(URL, KEY)
    assert path.read_bytes() == before
    assert lease["lease_id"] in (tmp_path / "budget/journal.jsonl").read_text()


def test_v2_missing_ready_clock_fails_closed_without_new_lease(tmp_path):
    gate = make_gate(tmp_path)
    lease = gate.acquire(URL, KEY)
    gate.finish(lease["lease_id"], "not_sent")
    directory = tmp_path / "budget"
    with (directory / "lock").open("r+b") as stream:
        stream.truncate(len(b"initialized\n"))
    before = (directory / "state.json").read_bytes()
    with pytest.raises(SharedJudgeGateError, match="pacing_clock_missing"):
        gate.acquire(URL, KEY)
    assert (directory / "state.json").read_bytes() == before


def test_ready_to_transport_delay_includes_slow_start_observer(tmp_path):
    observations, starts = [], []

    def observe(evidence):
        if evidence.get("transport_attempt") and evidence.get("finished_at") is None:
            time.sleep(0.5)
        observations.append(dict(evidence))

    client = HttpxJsonClient(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: starts.append(time.monotonic()) or httpx.Response(200, json=GOOD)
            )
        ),
        shared_gate=make_gate(tmp_path),
    )
    client.request_json_observed(URL, {}, "POST", extra_headers={"Authorization": KEY}, observer=observe)
    initial, final = [e for e in observations if e.get("dispatch_started")]
    assert "gate_ready_to_dispatch_ms" not in initial
    assert final["gate_ready_to_dispatch_ms"] >= 500
    dispatch = final["gate_transport_dispatch_monotonic"]
    assert starts[0] >= dispatch
    assert dispatch - final["started_monotonic"] >= 0.5


def test_v2_zeroed_ready_clock_cannot_reset_used_pacing(tmp_path):
    gate = make_gate(tmp_path)
    lease = gate.acquire(URL, KEY)
    gate.finish(lease["lease_id"], "not_sent")
    directory = tmp_path / "budget"
    with (directory / "lock").open("r+b") as stream:
        stream.seek(32)
        stream.write(bytes(8))
    before = (directory / "state.json").read_bytes()
    with pytest.raises(SharedJudgeGateError, match="pacing_clock_stale"):
        gate.acquire(URL, KEY)
    assert (directory / "state.json").read_bytes() == before
