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
        "text_embedder_timeout_s": 120,
        "legacy_clip_timeout_s": 120,
        "text_embedder_url": "http://127.0.0.1:8101",
        "legacy_clip_url": "http://127.0.0.1:8103",
    }
    connection = {
        "mode": "split_text_image",
        "text_url": "http://127.0.0.1:8101",
        "image_url": "http://127.0.0.1:8103",
    }
    embedding = {"identity_version": 1, "scheme": "legacy"}
    health = {}
    for role, model, dimension in (("text", "BAAI/bge-m3", 1024), ("visual", "ViT-L-14", 768)):
        body = {
            "identity_version": 1,
            "kind": role,
            "model": model,
            "dimension": dimension,
            "weights": {"sha": "weights"},
            "tokenizer": {"sha": "tokens"},
            "config": {"sha": "config"},
            "algorithm": {"revision": "test-v1"},
        }
        component = {**body, "semantic_sha256": capacity.embedding_identity.canonical_sha(body)}
        embedding[role] = component
        health[role] = {
            "status": "ok",
            "model": model,
            "dimension": dimension,
            "embedding_identity": component,
        }
    config = {
        "runtime_config": {
            "sentrix_url": "http://127.0.0.1:18191",
            "model_base_url": "http://192.168.0.100:18104/v1",
            "embedding": connection,
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
        embedding_health=health,
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
        cancel_drain_seconds=0,
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
    (snapshot / "manifest.json").write_text('{"identity":"checked"}\n')
    output = tmp_path / "round"
    run_overview, run_items = detail_fixture(64)

    def get(url, *_):
        if url.endswith("/config"):
            return json.dumps(config)
        if url.endswith("/diagnostics"):
            return json.dumps(diag)
        if url.endswith("/models"):
            return json.dumps(models)
        if url.endswith(":8101/health"):
            return json.dumps(kw["embedding_health"]["text"])
        if url.endswith(":8103/health"):
            return json.dumps(kw["embedding_health"]["visual"])
        if "/qa-dataset?" in url:
            return json.dumps({"items": rows()})
        if "/runs/known" in url:
            return detail_reader(run_overview, run_items)(url)
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


def test_restore_receipt_binds_raw_manifest_and_destination_without_reading_live_db(tmp_path):
    source = tmp_path / "original"
    data = tmp_path / "active"
    frozen = tmp_path / "snapshot"
    frozen.mkdir()
    # No active data directory/DB exists: this contract must only read the receipt.
    manifest = {
        "schema_version": 2,
        "source_root": str(source.resolve()),
        "identity_sha256": "b" * 64,
        "database": {"integrity_check": "ok"},
    }
    (frozen / "manifest.json").write_text(json.dumps(manifest, indent=4) + "\n")
    manifest_sha = capacity.memory_snapshot.digest(frozen / "manifest.json")
    receipt = {
        "schema_version": 1,
        "manifest_sha256": manifest_sha,
        "identity_sha256": manifest["identity_sha256"],
        "from_root": str(source.resolve()),
        "to_root": str(data.resolve()),
        "database": manifest["database"],
        "relocated_assets": 344,
        "post_restore_database_sha256": "c" * 64,
        "sqlite_files": {"sentrix.db": {"sha256": "c" * 64}},
    }
    path = capacity.memory_snapshot.receipt_path_for(data)
    path.write_text(json.dumps(receipt))
    result = capacity.restore_evidence(data, None, frozen, manifest)
    assert result["status"] == "verified"
    assert result["sha256"] == capacity.memory_snapshot.digest(path)
    assert result["receipt"]["manifest_sha256"] == manifest_sha
    with pytest.raises(ValueError, match="destination"):
        capacity.restore_evidence(tmp_path / "other", path, frozen, manifest)
    (frozen / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="snapshot"):
        capacity.restore_evidence(data, path, frozen, manifest)
    with pytest.raises(ValueError, match="requires"):
        capacity.restore_evidence(None, path, frozen, manifest)
    path.unlink()
    with pytest.raises(FileNotFoundError):
        capacity.restore_evidence(data, None, frozen, manifest)


def detail_fixture(count=8, error="", termination=""):
    overview = state("completed")
    overview.update(run_id="known", qa_count=count, item_count=count, read_revision="snapshot:fixed")
    overview.pop("items")  # Both public run overview modes omit this field.
    overview["phases"]["qa_eval"]["progress"].update(
        total=count, agent_completed=count, judge_completed=count
    )
    items = [
        {
            "qa_id": str(i),
            "_index": i,
            "execution_status": "completed",
            "judge_status": "completed",
            "judge": {"score": 0},
            "runtime_turns": [
                {
                    "status": "partial" if error else "complete",
                    "termination_reason": termination,
                    "trace_bundle": {
                        "model_calls": [
                            {"status": "error" if error else "complete", "error": error, "duration_ms": 100}
                        ]
                    },
                }
            ],
            "model_call_metrics": [
                {"status": "error" if error else "complete", "error": error, "duration_ms": 100}
            ],
            "tool_trace": [{"tool": "search_memories", "duration_ms": 50}],
        }
        for i in range(count)
    ]
    return overview, items


def detail_reader(overview, items):
    def read(url, *_):
        if "/items/" in url:
            index = int(url.rsplit("/", 1)[1])
            return json.dumps({"index": index, "item": items[index]})
        return json.dumps(overview)

    return read


def test_final_evidence_hydrates_real_overview_shape_with_bounded_parallel_reads(tmp_path, monkeypatch):
    overview, items = detail_fixture()
    active = peak = 0
    first_four = threading.Event()
    lock = threading.Lock()
    reader = detail_reader(overview, items)

    def read(url, *args):
        nonlocal active, peak
        if "/items/" not in url:
            return reader(url, *args)
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 4:
                first_four.set()
        try:
            assert first_four.wait(2), "detail collection must actually run in parallel"
            return reader(url, *args)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(capacity, "get_text", read)
    report = capacity.final_evidence("http://safe", "known", tmp_path, rows(8))
    assert peak == 4
    assert report["coverage"]["items_observed"] == 8
    assert report["model"]["calls_recorded"] == 8
    assert report["model"]["call_outcomes"]["completed"] == 8
    assert report["retrieval_tools"]["tool_wall_ms"]["search_memories"]["samples"] == 8
    assert [item["qa_id"] for item in json.loads((tmp_path / "final-run.json").read_text())["items"]] == [
        str(i) for i in range(8)
    ]


@pytest.mark.parametrize("corruption", ["index", "qa_id", "count", "missing_count", "revision"])
def test_final_evidence_rejects_misaligned_or_changing_details(tmp_path, monkeypatch, corruption):
    overview, items = detail_fixture()
    reader = detail_reader(overview, items)
    if corruption == "qa_id":
        items[0]["qa_id"] = "not-source-id"
    elif corruption == "count":
        overview["item_count"] = 7
    elif corruption == "missing_count":
        overview.pop("item_count")

    def read(url, *args):
        result = json.loads(reader(url, *args))
        if corruption == "index" and "/items/0" in url:
            result["index"] = 2
        if corruption == "revision" and "include_history=false" in url:
            result["read_revision"] = "snapshot:changed"
        return json.dumps(result)

    monkeypatch.setattr(capacity, "get_text", read)
    with pytest.raises(ValueError):
        capacity.final_evidence("http://safe", "known", tmp_path, rows(8))
    assert not (tmp_path / "final-run.json").exists()


def test_model_resource_failure_is_detected_inside_turn_and_call_records(tmp_path, monkeypatch):
    overview, items = detail_fixture(64, "CUDA out of memory", "model_error")
    monkeypatch.setattr(capacity, "get_text", detail_reader(overview, items))
    result = capacity.poll_run(**poll_kwargs(tmp_path), source_rows=rows(64))
    assert not result["passed"]
    assert result["failure"] == "resource_failure"
    report = capacity.final_evidence("http://safe", "known", tmp_path, rows(64))
    assert report["resource_failure"] is True
    assert report["model"]["call_outcomes"]["errors"] == 64
    assert report["coverage"]["runtime_turns"]["model_error_terminations"] == 64
    # Nested fallback works even for historical items without flattened calls.
    for item in items:
        item.pop("model_call_metrics")
    assert capacity.resource_failure({"items": items})


def test_nonresource_model_errors_and_answer_text_are_not_resource_failures(tmp_path, monkeypatch):
    overview, items = detail_fixture(64, "invalid function arguments", "model_error")
    for item in items:
        item.update(question="Explain timeout", answer="CUDA out of memory is an error message")
    monkeypatch.setattr(capacity, "get_text", detail_reader(overview, items))
    result = capacity.poll_run(**poll_kwargs(tmp_path), source_rows=rows(64))
    assert result["passed"]
    report = capacity.final_evidence("http://safe", "known", tmp_path, rows(64))
    assert report["resource_failure"] is False
    assert report["model"]["call_outcomes"]["errors"] == 64


def test_running_resource_failure_stops_new_dispatch_via_one_cancel(tmp_path, monkeypatch):
    overview, items = detail_fixture(64, "database is locked", "model_error")
    overview["status"] = "running"
    monkeypatch.setattr(capacity, "get_text", detail_reader(overview, items))
    calls = []
    monkeypatch.setattr(capacity, "post_once", lambda url, *args: calls.append(url) or {"state": "completed"})
    result = capacity.poll_run(**poll_kwargs(tmp_path), source_rows=rows(64))
    assert result["failure"] == "resource_failure"
    assert len(calls) == 1
    assert calls[0].endswith("/cancel")


def test_absent_item_trace_keeps_error_count_unknown(tmp_path, monkeypatch):
    overview, items = detail_fixture()
    for item in items:
        item.pop("runtime_turns")
        item.pop("model_call_metrics")
        item.pop("tool_trace")
    monkeypatch.setattr(capacity, "get_text", detail_reader(overview, items))
    report = capacity.final_evidence("http://safe", "known", tmp_path, rows(8))
    assert report["model"]["calls_recorded"] is None
    assert report["model"]["call_outcomes"]["errors"] is None
    assert report["coverage"]["runtime_turns"]["recorded"] is None


def test_semantic_identity_allows_new_ports_only_when_real_connections_agree():
    config, diag, models, kw = config_fixture()
    original = json.loads(json.dumps(kw["embedding"]))
    for setting, field, port in (
        ("text_embedder_url", "text_url", 18101),
        ("legacy_clip_url", "image_url", 18103),
    ):
        url = f"http://127.0.0.1:{port}"
        kw["expected_runtime"][setting] = url
        diag["runtime_configuration"][setting] = url
        config["runtime_config"]["embedding"][field] = url
    capacity.validate_configuration(config, diag, models, **kw)
    assert kw["embedding"] == original
    config["runtime_config"]["embedding"]["text_url"] = "http://127.0.0.1:8101"
    with pytest.raises(ValueError, match="connection"):
        capacity.validate_configuration(config, diag, models, **kw)


def test_capacity_rejects_legacy_connection_only_and_missing_health_identity():
    config, diag, models, kw = config_fixture()
    legacy = {**kw, "embedding": config["runtime_config"]["embedding"]}
    with pytest.raises(ValueError, match="semantic"):
        capacity.validate_configuration(config, diag, models, **legacy)
    kw["embedding_health"]["text"].pop("embedding_identity")
    with pytest.raises(ValueError, match="missing or incompatible"):
        capacity.validate_configuration(config, diag, models, **kw)


@pytest.mark.parametrize("kind", ["output_limit", "context_limit", "step_limit", "provider_rejected"])
def test_evidenced_operational_issue_is_not_quality_failure_or_capacity_boundary(kind):
    observed = state("completed_with_errors")
    observed["items"] = [
        {"operational_issue": {"kind": kind, "evidence": {"field": "error_code", "value": kind}}}
    ]
    assert capacity.run_problem(observed, 64, 2, 4) == "operational_issue"
    assert not capacity.resource_failure(observed)
    counts = capacity.summarize_run(observed)["operational"]
    assert counts["not_evaluable_count"] == 1
    assert counts["by_kind"][kind] == 1
    assert "capacity_boundary_confirmed" not in counts


def test_progress_cloud_budget_failure_stops_dispatch_without_marking_agent_resource_failure():
    observed = state()
    observed["phases"]["qa_eval"]["progress"]["operational"] = {
        "not_evaluable_count": 0,
        "judge_incomplete_count": 1,
    }
    assert capacity.run_problem(observed, 64, 2, 4) == "operational_issue"
    assert not capacity.resource_failure(observed)


def test_embedding_timeouts_are_mandatory_capacity_budget_facts():
    config, diag, models, kw = config_fixture()
    kw["expected_runtime"].pop("text_embedder_timeout_s")
    with pytest.raises(ValueError, match="embedding time budgets"):
        capacity.validate_configuration(config, diag, models, **kw)


@pytest.mark.parametrize("kind", ["time_limit", "provider_rejected", "resource_error"])
def test_embedding_operational_failure_stops_capacity_round(kind):
    from photobench.domain.runs.operational import exclude_operational_quality

    current = state()
    current["items"] = [
        exclude_operational_quality(
            {
                "execution_status": "completed",
                "runtime_turns": [
                    {
                        "trace_bundle": {
                            "tool_calls": [
                                {
                                    "observation": {
                                        "retrieval_channels": [
                                            {"channel": "text_ann", "operational_issue": {"error_code": kind}}
                                        ]
                                    }
                                }
                            ]
                        }
                    }
                ],
            }
        )
    ]
    assert capacity.run_problem(current, 64, 2, 4) in {"resource_failure", "operational_issue"}


def test_all_failed_judges_are_not_a_pass_despite_terminal_counters(tmp_path, monkeypatch):
    finished = state("completed")
    finished["status"] = "completed_with_errors"
    finished["items"] = [
        {"qa_id": str(i), "execution_status": "completed", "judge_status": "failed", "judge": {"score": None}}
        for i in range(64)
    ]
    finished["phases"]["qa_eval"]["judge_requests"] = {"requests_started": 384, "http_429": 384}
    monkeypatch.setattr(capacity, "get_text", lambda *_: json.dumps(finished))
    monkeypatch.setattr(capacity, "post_once", lambda *_: pytest.fail("terminal failures cannot be replayed"))
    result = capacity.poll_run(**poll_kwargs(tmp_path))
    assert result["passed"] is False and result["failure"] == "judge_failed"
    assert result["judge_failure_count"] == 64 and result["judge_failure_cause"] == "unknown"
    assert result["judge_requests"]["http_429"] == 384


@pytest.mark.parametrize(
    "judge",
    [
        {"score": 0, "judge_status": "completed"},
        {"score": None, "reason": "not_labeled"},
        {"score": None, "reason": "not_applicable"},
    ],
)
def test_valid_zero_and_applicable_absence_do_not_invent_judge_failure(judge):
    finished = state("completed")
    finished["items"] = [
        {
            "execution_status": "completed",
            "judge_status": "completed",
            "judge": judge,
            "evidence_judge": {"score": None, "reason": "not_applicable"},
        }
    ]
    # A run-level 429 may have recovered. It cannot be assigned to this item.
    assert capacity.run_problem(finished, 64, 2, 4) is None
    assert capacity.judge_failure_count(finished) == 0


def test_earlier_subjudge_failure_is_not_hidden_by_later_valid_quality_score():
    finished = state("completed")
    finished["items"] = [
        {
            "judge_status": "completed",
            "judge": {"score": 2},
            "runtime_turns": [
                {"task_judge": {"judge_status": "failed", "correct": None}},
                {"task_judge": {"judge_status": "completed", "correct": True}},
            ],
        }
    ]
    assert capacity.run_problem(finished, 64, 2, 4) == "judge_failed"
    assert capacity.judge_failure_count(finished) == 1


def test_poll_refreshes_unfinished_judge_only_after_terminal_counter_changes(tmp_path, monkeypatch):
    overview, items = detail_fixture(2)
    overview["status"] = "running"
    items[0]["judge_status"] = "pending"
    items[0]["judge"] = {"score": None}
    reads = {0: 0, 1: 0}
    polls = 0
    cancels = []

    def read(url, *_):
        nonlocal polls
        if "/items/" in url:
            index = int(url.rsplit("/", 1)[1])
            reads[index] += 1
            return json.dumps({"index": index, "item": items[index]})
        polls += 1
        assert polls <= 3, "new Judge failure must stop this round"
        if polls == 3:
            items[0]["judge_status"] = "failed"
        overview["phases"]["qa_eval"]["progress"]["judge_completed"] = 2 if polls == 3 else 1
        return json.dumps(overview)

    def cancel(*args):
        cancels.append(args[0])
        return {"state": "acknowledged"}

    monkeypatch.setattr(capacity, "get_text", read)
    monkeypatch.setattr(capacity, "post_once", cancel)
    kw = {**poll_kwargs(tmp_path), "count": 2, "source_rows": rows(2), "poll_seconds": 0}
    result = capacity.poll_run(**kw)
    assert not result["passed"] and result["failure"] == "judge_failed"
    assert reads == {0: 2, 1: 1} and len(cancels) == 1


def test_full_terminal_evidence_disqualifies_generic_judge_failure(tmp_path, monkeypatch):
    overview, items = detail_fixture()
    items[0]["judge_status"] = "failed"
    items[0]["task_judge"] = {"judge_status": "failed", "correct": None}
    monkeypatch.setattr(capacity, "get_text", detail_reader(overview, items))
    report = capacity.final_evidence("http://safe", "known", tmp_path, rows(8))
    assert report["judge_failure_count"] == 1 and report["judge_failure_cause"] == "unknown"
    assert report["coverage"]["item_outcomes"]["judge"]["failed"] == 1
    assert report["operational"]["judge_incomplete_count"] == 0
    result = {"passed": True}
    capacity.apply_final_evidence(result, report)
    assert result["passed"] is False and result["failure"] == "judge_failed"
    assert result["attribution"] is report


@pytest.mark.parametrize(
    "failure_code",
    ["database_busy", "database_locked", "out_of_memory", "vector_index_unavailable", "retrieval_timeout"],
)
def test_retrieval_resource_failure_cannot_pass_or_continue_dispatch(tmp_path, monkeypatch, failure_code):
    from photobench.domain.runs.operational import exclude_operational_quality

    kind = "time_limit" if failure_code == "retrieval_timeout" else "resource_error"
    current = state()
    item = exclude_operational_quality(
        {
            "execution_status": "completed",
            "judge": {"score": 0},
            "judge_status": "completed",
            "runtime_turns": [
                {
                    "trace_bundle": {
                        "tool_calls": [
                            {
                                "observation": {
                                    "retrieval_channels": [
                                        {
                                            "channel": "visual_ann",
                                            "status": "error",
                                            "operational_issue": {
                                                "error_code": kind,
                                                "failure_code": failure_code,
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    }
                }
            ],
        }
    )
    assert item["evaluation_status"] == "not_evaluable"
    assert item["judge"]["score"] is None and item["judge_status"] == "skipped"
    assert item["operational_issue"]["evidence"]["failure_code"] == failure_code
    current["items"] = [item]
    assert capacity.run_problem(current, 64, 2, 4) == "resource_failure"

    overview, items = detail_fixture(64)
    overview["status"] = "running"
    items[0].update(item)
    monkeypatch.setattr(capacity, "get_text", detail_reader(overview, items))
    calls = []
    monkeypatch.setattr(capacity, "post_once", lambda url, *args: calls.append(url) or {"state": "completed"})
    result = capacity.poll_run(**poll_kwargs(tmp_path), source_rows=rows(64))
    assert result["failure"] == "resource_failure" and not result["passed"]
    assert len(calls) == 1 and calls[0].endswith("/cancel")


def test_model_output_format_error_is_unevaluable_and_stops_capacity_dispatch():
    from photobench.domain.runs.operational import exclude_operational_quality

    current = state()
    current["items"] = [
        exclude_operational_quality(
            {
                "execution_status": "completed",
                "error_code": "model_output_invalid",
                "finish_reason": "stop",
                "request_policy": {"request_outcome_unknown": False},
                "judge": {"score": 0},
            }
        )
    ]
    assert current["items"][0]["judge"]["score"] is None
    assert capacity.run_problem(current, 64, 2, 4) == "operational_issue"
    assert not capacity.resource_failure(current)  # A format failure does not establish a capacity maximum.


@pytest.mark.parametrize("kind", ["model_output_invalid", "configuration_error"])
def test_new_operational_categories_are_not_evaluable_and_cannot_pass_capacity(kind):
    current = state("completed_with_errors")
    current["items"] = [{"operational_issue": {"kind": kind}}]
    assert capacity.run_problem(current, 64, 2, 4) == "operational_issue"
    report = capacity.summarize_run(current)
    assert report["operational"]["not_evaluable_count"] == 1
    assert report["operational"]["by_kind"][kind] == 1
    report.update(resource_failure=False, judge_failure_count=0)
    result = {"passed": True}
    capacity.apply_final_evidence(result, report)
    assert result["passed"] is False and result["failure"] == "operational_issue"


def test_invalid_cloud_content_cannot_qualify_as_completed_capacity(monkeypatch):
    from fakes_execution import FakeHttpClient, judge_reply

    from photobench.application.run_execution import judge as judge_module

    monkeypatch.setattr(judge_module, "JUDGE_REQUEST_INTERVAL_SECONDS", 0)
    http = FakeHttpClient([judge_reply("not JSON")])
    runner = judge_module.JudgeRunner(http=http, judge_url="http://fake", judge_model="fake")
    item = runner.judge_item(
        {
            "execution_status": "completed",
            "runtime_turns": [{"message": "q", "answer": "a", "expected_action": "answer"}],
        },
        {"answer": "r"},
        {},
    )
    current = state("completed")
    current["items"] = [item]
    assert len(http.calls) == 1
    assert capacity.run_problem(current, 64, 2, 4) == "operational_issue"
    report = capacity.summarize_run(current)
    report.update(resource_failure=False, judge_failure_count=capacity.judge_failure_count(current))
    result = {"passed": True}
    capacity.apply_final_evidence(result, report)
    assert result["passed"] is False
    assert report["operational"]["judge_by_kind"]["model_output_invalid"] == 1
    assert report["operational"]["not_evaluable_count"] == 0


def _cancel_drain_poll(tmp_path, monkeypatch, statuses, drain_s, *, cancel_unknown=False):
    reads, posts = [], []

    def get(url, *_):
        reads.append(url)
        status = statuses[min(len(reads) - 1, len(statuses) - 1)]
        observed = state(status)
        observed["run_id"] = "known"
        observed["phases"]["qa_eval"]["progress"].update(
            agent_running=int(status != "cancelled"),
            judge_running=0,
            outcome_unknown=int(status == "cancelled"),
        )
        return json.dumps(observed)

    def post(url, payload, key, journal):
        posts.append(url)
        event = {
            "request_id": key,
            "state": "request_outcome_unknown" if cancel_unknown else "completed",
        }
        journal.append(event)
        return event

    monkeypatch.setattr(capacity, "get_text", get)
    monkeypatch.setattr(capacity, "post_once", post)
    kwargs = poll_kwargs(tmp_path)
    kwargs.update(cancel_drain_seconds=drain_s, poll_seconds=0.001)
    kwargs["stop"].set()
    return capacity.poll_run(**kwargs), reads, posts


@pytest.mark.parametrize("cancel_unknown", [False, True])
def test_cancel_is_dispatched_once_then_drains_read_only(tmp_path, monkeypatch, cancel_unknown):
    result, reads, posts = _cancel_drain_poll(
        tmp_path,
        monkeypatch,
        ["running", "cancelling", "cancelled"],
        1,
        cancel_unknown=cancel_unknown,
    )
    assert len(posts) == 1
    assert len(reads) == 3
    assert result["state"] == "cancelled"
    assert result["failure"] == "operator_stop"
    drain = result["cancellation_drain"]
    assert drain["benchmark_terminal"] is True
    assert drain["outcome_unknown"] == 1
    assert drain["remote_requests_proven_idle"] is False
    assert drain["automatic_replay"] is False
    assert (tmp_path / "cancellation-drain.json").is_file()


def test_drain_deadline_keeps_residual_unknown_and_skips_full_item_fetch(tmp_path, monkeypatch):
    result, reads, posts = _cancel_drain_poll(tmp_path, monkeypatch, ["running"], 0.01)
    assert len(posts) == 1
    assert result["state"] == "running"
    assert result["cancellation_drain"]["deadline_exceeded"] is True
    assert result["cancellation_drain"]["remote_outcomes"] == "unresolved"
    assert result["cancellation_drain"]["remote_requests_proven_idle"] is False
    monkeypatch.setattr(capacity, "final_evidence", lambda *_: pytest.fail("must not exceed drain budget"))
    capacity.collect_final_evidence(result, "http://safe", "known", tmp_path, [])
    assert result["failure"] == "operator_stop"
    assert result["final_evidence_failure"] == "cancel_drain_incomplete"


def test_drain_read_errors_are_bounded_without_new_posts(tmp_path, monkeypatch):
    def get(*_):
        raise TimeoutError("read failed")

    monkeypatch.setattr(capacity, "get_text", get)
    monkeypatch.setattr(capacity, "post_once", lambda *_: pytest.fail("drain is GET only"))
    observed, drain = capacity.observe_cancel_drain(
        "http://safe",
        "known",
        state(),
        capacity.Journal(tmp_path / "progress.jsonl"),
        deadline_seconds=0.01,
        poll_seconds=0.001,
    )
    assert observed["status"] == "running"
    assert drain["deadline_exceeded"] is True
    assert drain["last_read_error_type"] == "TimeoutError"
    assert drain["remote_requests_proven_idle"] is False


def test_final_evidence_failure_preserves_original_cancellation_reason(tmp_path, monkeypatch):
    def evidence(*_):
        raise ValueError("changing result")

    monkeypatch.setattr(capacity, "final_evidence", evidence)
    result = {"passed": False, "failure": "operator_stop", "cancellation_drain": None}
    capacity.collect_final_evidence(result, "http://safe", "known", tmp_path, [])
    assert result["failure"] == "operator_stop"
    assert result["final_evidence_failure"] == "final_run_evidence_unavailable"


def test_terminal_cancel_does_not_extend_drain_with_full_item_hydration(tmp_path, monkeypatch):
    monkeypatch.setattr(capacity, "final_evidence", lambda *_: pytest.fail("no GET after bounded drain"))
    result = {"passed": False, "failure": "operator_stop", "cancellation_drain": {"benchmark_terminal": True}}
    capacity.collect_final_evidence(result, "http://safe", "known", tmp_path, [])
    assert result["final_evidence_failure"] == "cancelled_round_full_evidence_deferred"
    assert result["failure"] == "operator_stop"
