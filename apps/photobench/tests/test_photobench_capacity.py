"""Safety and one-dispatch contracts for the PhotoBench capacity CLI."""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts" / "benchmarks"))
import photobench_capacity as capacity  # noqa: E402


def rows(count: int = 64):
    return [{"qa_id": str(i), "question": f"Question {i}"} for i in range(count)]


def config_fixture():
    runtime = {
        "deployment_profile": "mac",
        "embedding_scheme": "legacy",
        "agent_wall_time_s": 600,
        "chat_timeout_s": 180,
    }
    embedding = {
        "mode": "split_text_image",
        "text_url": "http://127.0.0.1:8101",
        "image_url": "http://127.0.0.1:8103",
    }
    config = {
        "runtime_config": {
            "sentrix_url": "http://127.0.0.1:18191",
            "model_base_url": "http://192.168.0.100:18104/v1",
            "embedding": embedding,
        }
    }
    diag = {"runtime_configuration": {**runtime, "vlm_base_url": "http://192.168.0.100:18104/v1"}}
    models = {"data": [{"id": "qwen35-4b-awq", "max_model_len": 262144}]}
    kw = dict(
        sentrix="http://127.0.0.1:18191",
        model_base="http://192.168.0.100:18104",
        model="qwen35-4b-awq",
        window=262144,
        expected_runtime=runtime,
        embedding=embedding,
    )
    return config, diag, models, kw


def test_config_requires_both_actual_endpoints_and_fixed_budget():
    config, diag, models, kw = config_fixture()
    capacity.validate_configuration(config, diag, models, **kw)
    diag["runtime_configuration"]["vlm_base_url"] = "http://192.168.0.153:8100/v1"
    with pytest.raises(ValueError, match="Sentrix VLM URL"):
        capacity.validate_configuration(config, diag, models, **kw)
    diag["runtime_configuration"]["vlm_base_url"] = kw["model_base"] + "/v1"
    diag["runtime_configuration"]["agent_wall_time_s"] = 180
    with pytest.raises(ValueError, match="run budget"):
        capacity.validate_configuration(config, diag, models, **kw)


def test_protected_host_and_implicit_paths_are_rejected(monkeypatch):
    monkeypatch.setattr(
        capacity.socket, "getaddrinfo", lambda *_: [(None, None, None, None, ("192.168.0.153", 80))]
    )
    with pytest.raises(ValueError, match="protected"):
        capacity.service_url("http://alias:18772", "alias")
    with pytest.raises(ValueError, match="origin"):
        capacity.service_url("http://safe:18772/api/v1", "safe")


def test_actual_rows_and_order_are_checked_before_capacity_minimum():
    source = rows()
    enriched = [{**r, "media_resolution": {}} for r in source]
    assert capacity.validate_rows(source, {"items": enriched}, 32, 64) == 64
    with pytest.raises(ValueError, match="twice"):
        capacity.validate_rows(source, {"items": enriched}, 33, 64)
    with pytest.raises(ValueError, match="order"):
        capacity.validate_rows(source, {"items": list(reversed(enriched))}, 1, 64)
    changed = [{**r} for r in enriched]
    changed[0]["question"] = "Different question"
    with pytest.raises(ValueError, match="source"):
        capacity.validate_rows(source, {"items": changed}, 1, 64)


def test_unknown_post_is_durable_before_send_and_never_replayed(tmp_path, monkeypatch):
    journal = capacity.Journal(tmp_path / "dispatch.jsonl")
    calls = []

    class LostResponse:
        def open(self, request, timeout):
            calls.append(request)
            assert journal.latest()["one"]["state"] == "request_outcome_unknown"
            raise TimeoutError("server accepted request but response lost")

    monkeypatch.setattr(capacity, "opener", lambda: LostResponse())
    event = capacity.post_once("http://safe/runs", {"mode": "reuse"}, "one", journal)
    assert event["state"] == "request_outcome_unknown"
    assert capacity.known_run_id(event) is None
    with pytest.raises(ValueError, match="cannot be replayed"):
        capacity.post_once("http://safe/runs", {}, "one", journal)
    assert len(calls) == 1


def state(status="running", judge=4):
    return {
        "status": status,
        "qa_count": 64,
        "qa_concurrency": 2,
        "phases": {
            "qa_eval": {
                "progress": {
                    "total": 64,
                    "qa_concurrency": 2,
                    "judge_concurrency": judge,
                    "agent_completed": 64,
                    "judge_completed": 64 if status == "completed" else 0,
                },
                "judge_requests": {"http_requests": 127, "http_429": 3},
            }
        },
        "items": [],
    }


def test_progress_keeps_cloud_requests_distinct_from_questions():
    view = capacity.progress_view(state())
    assert view["qa_count"] == 64
    assert view["judge_requests"]["http_requests"] == 127
    assert capacity.run_problem(state(judge=2), 64, 2, 4) == "effective_judge_concurrency_mismatch"


def poll_kwargs(tmp_path):
    return dict(
        bench="http://safe:18772",
        run_id="known",
        output=tmp_path,
        ledger=capacity.Journal(tmp_path / "dispatch.jsonl"),
        request_id="trial",
        count=64,
        concurrency=2,
        judge_concurrency=4,
        stop=threading.Event(),
        sampler=SimpleNamespace(failure=None, peaks={}),
        poll_seconds=0.1,
        deadline_seconds=30,
        client_pid=None,
        client_start=None,
    )


def test_completed_observation_does_not_resume_or_rejudge(tmp_path, monkeypatch):
    finished = state("completed")
    monkeypatch.setattr(capacity, "get_text", lambda *_: json.dumps(finished))
    monkeypatch.setattr(capacity, "post_once", lambda *_: pytest.fail("completed run must not issue a POST"))
    result = capacity.poll_run(**poll_kwargs(tmp_path))
    assert result["passed"]
    assert result["judge_requests"]["http_requests"] == 127


def test_safety_failure_cancels_once_and_preserves_unknown_cancel(tmp_path, monkeypatch):
    monkeypatch.setattr(capacity, "get_text", lambda *_: json.dumps(state(judge=1)))
    calls = []

    def cancel(url, payload, key, journal):
        calls.append((url, key))
        event = {"request_id": key, "state": "request_outcome_unknown"}
        journal.append(event)
        return event

    monkeypatch.setattr(capacity, "post_once", cancel)
    kw = poll_kwargs(tmp_path)
    first = capacity.poll_run(**kw)
    second = capacity.poll_run(**kw)
    assert first["failure"] == second["failure"] == "effective_judge_concurrency_mismatch"
    assert len(calls) == 1
    assert not first["passed"]


def test_completed_qa_pending_judge_is_not_success(tmp_path, monkeypatch):
    finished = state("completed")
    finished["phases"]["qa_eval"]["progress"]["judge_completed"] = 17
    monkeypatch.setattr(capacity, "get_text", lambda *_: json.dumps(finished))
    monkeypatch.setattr(capacity, "post_once", lambda *_: pytest.fail("no automatic rejudge"))
    assert not capacity.poll_run(**poll_kwargs(tmp_path))["passed"]


def test_model_window_and_embedding_mismatch_rejected():
    config, diag, models, kw = config_fixture()
    models["data"][0]["max_model_len"] = 131072
    with pytest.raises(ValueError, match="context window"):
        capacity.validate_configuration(config, diag, models, **kw)
    models["data"][0]["max_model_len"] = 262144
    config["runtime_config"]["embedding"] = {"mode": "wemm"}
    with pytest.raises(ValueError, match="snapshot embedding"):
        capacity.validate_configuration(config, diag, models, **kw)


def test_main_resume_observes_known_run_without_replaying_and_rejects_identity_change(tmp_path, monkeypatch):
    config, diag, models, kw = config_fixture()
    source = tmp_path / "qa.jsonl"
    source.write_text("".join(json.dumps(row) + "\n" for row in rows()))
    identity = tmp_path / "identity.json"
    identity.write_text(
        json.dumps(
            {
                "weights_sha256": "a" * 64,
                "engine_version": "vllm-test",
                "model": "qwen35-4b-awq",
                "max_model_len": 262144,
            }
        )
    )
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps(kw["expected_runtime"]))
    memory = tmp_path / "memory.json"
    memory.write_text(json.dumps({"scope_id": "frozen-scope", "embedding": kw["embedding"]}))
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    output = tmp_path / "round"

    def get(url, *_):
        if url.endswith("/config"):
            return json.dumps(config)
        if url.endswith("/diagnostics"):
            return json.dumps(diag)
        if url.endswith("/models"):
            return json.dumps(models)
        if "/qa-dataset?" in url:
            return json.dumps({"items": rows()})
        if "/runs/known" in url:
            return json.dumps(state("completed"))
        raise AssertionError(url)

    monkeypatch.setattr(capacity, "get_text", get)
    monkeypatch.setattr(capacity.memory_snapshot, "verify", lambda *_: {"identity": "checked"})
    monkeypatch.setattr(
        capacity.socket, "getaddrinfo", lambda host, *_: [(None, None, None, None, (host, 80))]
    )
    monkeypatch.setattr(capacity.signal, "signal", lambda *_: None)

    class FakeSampler:
        def __init__(self, *_):
            self.failure = None
            self.peaks = {}
            self.stop = threading.Event()
            self.thread = SimpleNamespace(start=lambda: None, join=lambda **_: None, is_alive=lambda: False)

        def sample_once(self):
            return None

    monkeypatch.setattr(capacity, "Sampler", FakeSampler)
    sent = []

    class Response:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self):
            return json.dumps({"run_ids": ["known"]}).encode()

    class API:
        def open(self, req, timeout):
            sent.append(req.full_url)
            persisted = capacity.Journal(output / "dispatch.jsonl").latest()
            assert list(persisted.values())[0]["state"] == "request_outcome_unknown"
            return Response()

    monkeypatch.setattr(capacity, "opener", lambda: API())
    args = [
        "--bench-url",
        "http://127.0.0.1:18772",
        "--sentrix-url",
        "http://127.0.0.1:18191",
        "--model-url",
        "http://192.168.0.100:18104/v1",
        "--allow-bench-host",
        "127.0.0.1",
        "--allow-sentrix-host",
        "127.0.0.1",
        "--allow-model-host",
        "192.168.0.100",
        "--model",
        "qwen35-4b-awq",
        "--scope-id",
        "frozen-scope",
        "--album-id",
        "album",
        "--qa-set",
        "capacity",
        "--qa-sha256",
        capacity.hashlib.sha256(source.read_bytes()).hexdigest(),
        "--identity",
        str(identity),
        "--expected-runtime-config",
        str(runtime),
        "--memory-snapshot",
        str(snapshot),
        "--memory-identity",
        str(memory),
        "--qa-source-file",
        str(source),
        "--output",
        str(output),
        "--window",
        "262144",
        "--qa-concurrency",
        "2",
        "--expected-count",
        "64",
        "--min-model-available-gib",
        "0",
    ]
    assert capacity.main(args) == 0
    assert capacity.main(args + ["--resume"]) == 0
    assert sent == ["http://127.0.0.1:18772/api/v1/runs"]
    changed = json.loads(identity.read_text())
    changed["engine_version"] = "other-profile"
    identity.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match="identity/configuration"):
        capacity.main(args + ["--resume"])


def test_active_copy_checks_pid_identity_and_only_named_environment(tmp_path):
    data, frozen = tmp_path / "round-copy", tmp_path / "snapshot"
    data.mkdir()
    frozen.mkdir()
    proc = tmp_path / "proc"
    selected = proc / "42"
    selected.mkdir(parents=True)
    fields = ["S"] + ["0"] * 21
    fields[19] = "12345"
    (selected / "stat").write_text("42 (uvicorn) " + " ".join(fields))
    (selected / "environ").write_bytes(
        b"UNRELATED_SECRET=never-persist-this\0SENTRIX_DATA_DIR=" + os.fsencode(data) + b"\0"
    )
    record = tmp_path / "pid.json"
    record.write_text(json.dumps({"pid": 42, "start_ticks": "12345"}))
    checked = capacity.validate_active_data_copy(data, record, frozen, proc)
    assert checked["status"] == "verified"
    assert "never-persist-this" not in json.dumps(checked)
    another = tmp_path / "other-copy"
    another.mkdir()
    with pytest.raises(ValueError, match="restored data directory"):
        capacity.validate_active_data_copy(another, record, frozen, proc)
    record.write_text(json.dumps({"pid": 42, "start_ticks": "old-pid"}))
    with pytest.raises(ValueError, match="start identity"):
        capacity.validate_active_data_copy(data, record, frozen, proc)


def test_active_snapshot_path_is_rejected_and_unavailable_is_explicit(tmp_path):
    data = tmp_path / "snapshot"
    data.mkdir()
    proc = tmp_path / "proc"
    selected = proc / "42"
    selected.mkdir(parents=True)
    fields = ["S"] + ["0"] * 21
    fields[19] = "12345"
    (selected / "stat").write_text("42 (uvicorn) " + " ".join(fields))
    (selected / "environ").write_bytes(b"SENTRIX_DATA_DIR=" + os.fsencode(data) + b"\0")
    record = tmp_path / "pid.json"
    record.write_text(json.dumps({"pid": 42, "start_ticks": "12345"}))
    with pytest.raises(ValueError, match="independent"):
        capacity.validate_active_data_copy(data, record, data, proc)
    assert capacity.validate_active_data_copy(None, None, data)["status"] == "unavailable"
    assert (
        capacity.validate_active_data_copy(data, record, data, tmp_path / "missing-proc")["status"]
        == "unavailable"
    )
