#!/usr/bin/env python3
"""Run one isolated PhotoBench QA capacity round against a frozen memory copy.

The owning deployment restores/restarts Sentrix before each round. This command
never builds a memory database, switches models, retries a POST, resumes QA, or
rejudges completed answers. --resume resumes observation of a known run only.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import hashlib
import json
import os
import platform
import re
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

# Direct script execution starts with scripts/benchmarks on sys.path. Use the same
# canonical namespace as repository-wide type checking and module execution.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.benchmarks import memory_snapshot  # noqa: E402
from scripts.benchmarks.remote_vllm_capacity import (  # noqa: E402
    SAFETY_PROTOCOL,
    Journal,
    Sampler,
    get_text,
    opener,
    validate_endpoint,
)
from scripts.benchmarks.summarize_photobench_capacity import summarize_run  # noqa: E402
from scripts.maintenance import embedding_identity  # noqa: E402

Json = dict[str, Any]
TERMINAL = {"completed", "completed_with_errors", "failed", "cancelled", "interrupted"}
RESOURCE_ERROR = re.compile(
    r"out of memory|cuda error|gpu.*(?:fault|error)|database (?:is )?locked|"
    r"resource_exhausted|resource.?timeout|timed out|timeout|time_budget|deadline",
    re.IGNORECASE,
)
RUNTIME_KEYS = {"deployment_profile", "embedding_scheme", "agent_wall_time_s", "chat_timeout_s"}


def restore_evidence(
    data_dir: Path | None, receipt_path: Path | None, snapshot: Path, manifest: Json
) -> Json:
    """Validate the offline receipt without opening the active SQLite database."""
    if data_dir is None:
        if receipt_path is not None:
            raise ValueError("restore receipt requires the active Sentrix data directory")
        return {"status": "unavailable", "reason": "sentrix_data_dir_not_supplied"}
    path = receipt_path or memory_snapshot.receipt_path_for(data_dir)
    receipt = json.loads(path.read_text())
    manifest_sha = memory_snapshot.digest(snapshot / "manifest.json")
    memory_snapshot.validate_restore_receipt(receipt, manifest, data_dir, manifest_sha)
    return {
        "status": "verified",
        "path": str(path.resolve()),
        "sha256": memory_snapshot.digest(path),
        "receipt": receipt,
        "verification": "offline restore receipt bound to snapshot and destination; active DB not rehashed",
    }


def save_json(path: Path, value: Any) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


def service_url(raw: str, allowed_host: str) -> str:
    """Explicit target, no authentication, query, redirects or protected 153."""
    parsed = urllib.parse.urlsplit(raw)
    if parsed.path.rstrip("/"):
        raise ValueError("service URL must be an origin without an API path")
    return validate_endpoint(raw, allowed_host)


def same_url(actual: Any, expected: str) -> bool:
    return str(actual or "").rstrip("/") == expected.rstrip("/")


def subset(expected: Any, actual: Any) -> bool:
    """Dataset API may add resolved media fields; source fields must not change."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            k in actual and subset(v, actual[k]) for k, v in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(expected) == len(actual)
            and all(subset(x, y) for x, y in zip(expected, actual, strict=True))
        )
    return bool(expected == actual)


def validate_rows(
    source_rows: list[Json], observed: Json, concurrency: int, expected_count: int | None
) -> int:
    actual = observed.get("items")
    if not isinstance(actual, list) or not subset(source_rows, actual):
        raise ValueError("served QA rows/order differ from the hashed source")
    count = len(actual)
    if count < max(32, 2 * concurrency):
        raise ValueError("a capacity round needs at least 32 questions and twice QA concurrency")
    if expected_count is not None and count != expected_count:
        raise ValueError("served question count differs from expected count")
    if observed.get("count") is not None and observed["count"] != count:
        raise ValueError("dataset count contradicts its rows")
    return count


def embedding_connections(config: Json, diagnostics: Json, expected_runtime: Json) -> dict[str, str]:
    """Addresses are deployment facts, never vector-space compatibility keys."""
    connection = (config.get("runtime_config") or {}).get("embedding") or {}
    actual = diagnostics.get("runtime_configuration") or {}
    if connection.get("mode") != "split_text_image" or expected_runtime.get("embedding_scheme") != "legacy":
        raise ValueError("capacity snapshot embedding requires the complete legacy scheme")
    endpoints: dict[str, str] = {}
    for role, setting, connection_key in (
        ("text", "text_embedder_url", "text_url"),
        ("visual", "legacy_clip_url", "image_url"),
    ):
        expected = expected_runtime.get(setting)
        if not isinstance(expected, str) or not expected:
            raise ValueError("expected runtime must declare both embedding connection addresses")
        if not same_url(actual.get(setting), expected) or not same_url(
            connection.get(connection_key), expected
        ):
            raise ValueError("PhotoBench/Sentrix embedding connection differs from expected runtime")
        host = urllib.parse.urlsplit(expected).hostname
        if host is None:
            raise ValueError("embedding connection has no host")
        endpoints[role] = service_url(expected, host)
    return endpoints


def validate_configuration(
    config: Json,
    diagnostics: Json,
    models: Json,
    *,
    sentrix: str,
    model_base: str,
    model: str,
    window: int,
    expected_runtime: Json,
    embedding: Any,
    embedding_health: Json,
) -> None:
    runtime = config.get("runtime_config") or {}
    if not same_url(runtime.get("sentrix_url", config.get("default_sentrix_url")), sentrix):
        raise ValueError("PhotoBench points to a different Sentrix instance")
    if not same_url(
        runtime.get("model_base_url", config.get("default_vllm_model_base_url")), model_base + "/v1"
    ):
        raise ValueError("PhotoBench model URL differs from this experiment")
    if runtime.get("vllm_manager_url") or config.get("default_vllm_api_url"):
        raise ValueError("capacity run requires an unmanaged remote model endpoint")
    embedding_identity.validate_space(embedding)
    embedding_connections(config, diagnostics, expected_runtime)
    embedding_identity.validate_health(embedding, embedding_health)
    actual = diagnostics.get("runtime_configuration") or {}
    if not same_url(actual.get("vlm_base_url"), model_base + "/v1"):
        raise ValueError("Sentrix VLM URL differs from the explicit model endpoint")
    if not RUNTIME_KEYS.issubset(expected_runtime):
        raise ValueError(
            "expected runtime must declare deployment profile, embedding scheme and both time budgets"
        )
    if not subset(expected_runtime, actual):
        raise ValueError("actual Sentrix runtime differs from the declared run budget")
    selected = next((m for m in models.get("data", []) if m.get("id") == model), None)
    if not selected or selected.get("max_model_len") != window:
        raise ValueError("served model or context window differs from this experiment")


def post_once(url: str, payload: Json, request_id: str, journal: Journal, timeout: float = 30) -> Json:
    if request_id in journal.latest():
        raise ValueError("this POST already has dispatch evidence and cannot be replayed")
    raw = json.dumps(payload).encode()
    event: Json = {
        "request_id": request_id,
        "url": url,
        "started_at": time.time(),
        "state": "request_outcome_unknown",
        "request_sha256": hashlib.sha256(raw).hexdigest(),
    }
    journal.append(event)  # fsync before touching the network
    try:
        req = urllib.request.Request(url, data=raw, headers={"Content-Type": "application/json"})
        with opener().open(req, timeout=timeout) as response:
            body = json.loads(response.read())
            event.update(state="completed", http_status=response.status, response=body)
    except urllib.error.HTTPError as exc:
        event.update(http_status=exc.code, error_sha256=hashlib.sha256(exc.read()).hexdigest())
        if 400 <= exc.code < 500:
            event["state"] = "http_rejected"
    except Exception as exc:
        event["error_type"] = type(exc).__name__
    event["elapsed_seconds"] = time.time() - event["started_at"]
    journal.append(event)
    return event


def known_run_id(event: Json | None) -> str | None:
    ids = (event or {}).get("response", {}).get("run_ids")
    return str(ids[0]) if isinstance(ids, list) and len(ids) == 1 else None


def progress_view(state: Json) -> Json:
    qa = (state.get("phases") or {}).get("qa_eval") or {}
    return {
        "status": state.get("status"),
        "qa_count": state.get("qa_count"),
        "progress": qa.get("progress"),
        "judge_requests": qa.get("judge_requests"),
        "stage_timings": state.get("phases"),
        "qa_concurrency": state.get("qa_concurrency"),
    }


def run_problem(state: Json, count: int, concurrency: int, judge_concurrency: int) -> str | None:
    if state.get("qa_count") is not None and state["qa_count"] != count:
        return "actual_run_question_count_mismatch"
    qa = (state.get("phases") or {}).get("qa_eval") or {}
    progress = qa.get("progress") or {}
    if progress:
        if progress.get("total") != count:
            return "actual_progress_question_count_mismatch"
        if progress.get("qa_concurrency") != concurrency:
            return "effective_qa_concurrency_mismatch"
        if progress.get("judge_concurrency") != judge_concurrency:
            return "effective_judge_concurrency_mismatch"
        if progress.get("outcome_unknown", 0):
            return "request_outcome_unknown"
    if resource_failure(state):
        return "resource_failure"
    counts = progress.get("operational") or {}
    if counts.get("not_evaluable_count", 0) or counts.get("judge_incomplete_count", 0):
        return "operational_issue"
    if any(item.get("operational_issue") or item.get("judge_issue") for item in state.get("items") or []):
        return "operational_issue"
    return None


def resource_failure(state: Json) -> bool:
    errors = [str(state.get("fatal_error") or "")]
    errors.extend(
        str(v.get("error") or "") for v in (state.get("phases") or {}).values() if isinstance(v, dict)
    )
    # Error evidence is structured; never scan QA prose, answers or tool arguments.
    for item in state.get("items") or []:
        if (item.get("operational_issue") or {}).get("kind") in {"resource_error", "time_limit"}:
            return True
        turns = [turn for turn in item.get("runtime_turns") or [] if isinstance(turn, dict)]
        records = [item, *turns]
        calls = item.get("model_call_metrics")
        if isinstance(calls, list):
            records.extend(call for call in calls if isinstance(call, dict))
        else:
            for turn in turns:
                records.extend(
                    call
                    for call in (turn.get("trace_bundle") or {}).get("model_calls") or []
                    if isinstance(call, dict)
                )
        for record in records:
            errors.extend(
                str(record.get(k) or "")
                for k in ("error", "agent_error", "error_message", "termination_reason")
            )
    return any(RESOURCE_ERROR.search(s) for s in errors)


def fetch_item_details(
    bench: str, run_id: str, state: Json, source_rows: Sequence[Json], indices: Sequence[int] | None = None
) -> dict[int, Json]:
    """Bounded read-only hydration; overview and item-list APIs omit full traces."""
    expected_ids = [row.get("qa_id") for row in source_rows]
    if any(not isinstance(value, str) or not value for value in expected_ids) or len(
        set(expected_ids)
    ) != len(expected_ids):
        raise ValueError("source QA identities must be nonempty and unique")
    count = state.get("item_count")
    if (
        state.get("run_id") != run_id
        or state.get("qa_count") != len(source_rows)
        or isinstance(count, bool)
        or not isinstance(count, int)
        or not 0 <= count <= len(source_rows)
    ):
        raise ValueError("run identity or item count does not match the fixed QA source")
    if state.get("status") in {"completed", "completed_with_errors"} and count != len(source_rows):
        raise ValueError("completed run does not contain the full fixed QA count")
    requested = list(range(count)) if indices is None else list(indices)
    if len(set(requested)) != len(requested) or any(i < 0 or i >= count for i in requested):
        raise ValueError("invalid requested item indexes")
    base = bench + "/api/v1/runs/" + urllib.parse.quote(run_id, safe="") + "/items/"

    def fetch(index: int) -> tuple[int, Json]:
        detail = json.loads(get_text(base + str(index), 60))
        item = detail.get("item")
        if (
            detail.get("index") != index
            or isinstance(detail.get("index"), bool)
            or not isinstance(item, dict)
            or item.get("qa_id") != expected_ids[index]
            or item.get("_index", index) != index
        ):
            raise ValueError("item index or QA identity differs from the fixed source")
        return index, item

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        return dict(pool.map(fetch, requested))


def process_sample(pid: int | None, expected_start: str | None = None) -> Json:
    result: Json = {"host": platform.node(), "scope": "selected_sentrix_process_on_client_host"}
    if pid is None:
        return {**result, "available": False, "reason": "no_pid_file_supplied"}
    try:
        root = Path("/proc") / str(pid)
        stat = (root / "stat").read_text().rsplit(")", 1)[1].split()
        start = stat[19]
        if expected_start is not None and start != expected_start:
            raise ValueError("selected process identity changed")
        status = dict(line.split(":", 1) for line in (root / "status").read_text().splitlines())
        ticks = os.sysconf("SC_CLK_TCK")
        result.update(
            available=True,
            pid=pid,
            start_time_ticks=start,
            cpu_user_seconds=int(stat[11]) / ticks,
            cpu_system_seconds=int(stat[12]) / ticks,
            rss_bytes=int(status["VmRSS"].split()[0]) * 1024,
            threads=int(status["Threads"]),
            command_sha256=hashlib.sha256((root / "cmdline").read_bytes()).hexdigest(),
        )
    except Exception as exc:
        result.update(available=False, error_type=type(exc).__name__)
    return result


def poll_run(
    *,
    bench: str,
    run_id: str,
    output: Path,
    ledger: Journal,
    request_id: str,
    count: int,
    concurrency: int,
    judge_concurrency: int,
    stop: threading.Event,
    sampler: Sampler,
    poll_seconds: float,
    deadline_seconds: float,
    client_pid: int | None,
    client_start: str | None,
    source_rows: Sequence[Json] | None = None,
) -> Json:
    started = time.monotonic()
    stop_reason = None
    cancel_event: Json | None = None
    samples = Journal(output / "photobench-progress.jsonl")
    state: Json = {}
    observed_items: dict[int, Json] = {}
    while True:
        try:
            state = json.loads(
                get_text(
                    bench + "/api/v1/runs/" + urllib.parse.quote(run_id, safe="") + "?include_history=false",
                    30,
                )
            )
            if source_rows is not None:
                count_seen = state.get("item_count")
                # In-flight placeholders can change. Completed Agent results are
                # immutable for model-error attribution even while Judge continues.
                pending_indexes = [
                    i
                    for i in range(
                        count_seen if isinstance(count_seen, int) and not isinstance(count_seen, bool) else 0
                    )
                    if i not in observed_items
                    or observed_items[i].get("execution_status")
                    not in {"completed", "failed", "timeout", "outcome_unknown"}
                ]
                observed_items.update(fetch_item_details(bench, run_id, state, source_rows, pending_indexes))
                state = {**state, "items": [observed_items[i] for i in sorted(observed_items)]}
            save_json(output / "latest-run.json", state)
        except Exception as exc:
            samples.append({"observed_at": time.time(), "read_error_type": type(exc).__name__})
            stop_reason = stop_reason or "run_observation_unavailable"
            stop.set()
        if state.get("status") in TERMINAL:
            # Never call resume or rejudge, even if scoring remains incomplete.
            break
        if state:
            stop_reason = stop_reason or run_problem(state, count, concurrency, judge_concurrency)
            samples.append(
                {
                    "observed_at": time.time(),
                    "run_id": run_id,
                    **progress_view(state),
                    "sentrix_client": process_sample(client_pid, client_start),
                }
            )
        stop_reason = stop_reason or sampler.failure
        if time.monotonic() - started >= deadline_seconds:
            stop_reason = stop_reason or "round_wall_time_budget_exceeded"
        if stop_reason or stop.is_set():
            stop_reason = stop_reason or "operator_stop"
            if cancel_event is None:
                key = request_id + ":cancel"
                previous = ledger.latest().get(key)
                cancel_event = previous or post_once(
                    bench + "/api/v1/runs/" + urllib.parse.quote(run_id, safe="") + "/cancel", {}, key, ledger
                )
            # Cancellation is a single dispatch; a lost acknowledgement is not retried.
            break
        stop.wait(poll_seconds)
    problem = run_problem(state, count, concurrency, judge_concurrency) if state else "missing_run_state"
    qa = (state.get("phases") or {}).get("qa_eval") or {}
    progress = qa.get("progress") or {}
    passed = (
        state.get("status") in {"completed", "completed_with_errors"}
        and not (stop_reason or problem or sampler.failure)
        and progress.get("agent_completed") == count
        and progress.get("judge_completed") == count
    )
    return {
        "run_id": run_id,
        "state": state.get("status"),
        "passed": passed,
        "failure": stop_reason
        or problem
        or sampler.failure
        or (None if passed else "run_or_judge_incomplete"),
        "qa_count": count,
        "qa_concurrency": concurrency,
        "judge_concurrency": judge_concurrency,
        "elapsed_seconds": time.monotonic() - started,
        "progress": progress,
        "judge_requests": qa.get("judge_requests"),
        "stage_timings": state.get("phases"),
        "cancel_dispatch": cancel_event,
        "model_metric_peaks": sampler.peaks,
        "capacity_only_not_model_score": True,
        "conclusion": "one observed round; no maximum or recommendation inferred",
    }


def validate_active_data_copy(
    data_dir: Path | None,
    pid_file: Path | None,
    snapshot: Path,
    proc_root: Path = Path("/proc"),
) -> Json:
    """Verify the selected live process uses an independent restored data root.

    Read only the named environment setting; never persist the full environment.
    This proves process/path identity, not a hash of a mutating SQLite database.
    """
    if data_dir is None:
        return {"status": "unavailable", "reason": "sentrix_data_dir_not_supplied"}
    if pid_file is None:
        raise ValueError("active data-copy verification requires the Sentrix PID record")
    if not proc_root.exists():
        return {"status": "unavailable", "reason": "Linux_proc_not_available"}
    record = json.loads(pid_file.read_text())
    pid = record.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValueError("invalid Sentrix PID record")
    process = proc_root / str(pid)
    stat = (process / "stat").read_text().rsplit(")", 1)[1].split()
    recorded_start = str(record.get("start_ticks") or record.get("start_time_ticks") or "")
    if not recorded_start or stat[19] != recorded_start:
        raise ValueError("Sentrix PID start identity does not match its record")
    setting = next(
        (
            entry.partition(b"=")[2]
            for entry in (process / "environ").read_bytes().split(b"\0")
            if entry.partition(b"=")[0] == b"SENTRIX_DATA_DIR"
        ),
        None,
    )
    if setting is None:
        return {"status": "unavailable", "reason": "SENTRIX_DATA_DIR_not_in_process_environment"}
    actual = Path(os.fsdecode(setting))
    if not actual.is_absolute():
        actual = Path(os.readlink(process / "cwd")) / actual
    actual, expected, frozen = (
        actual.resolve(strict=True),
        data_dir.resolve(strict=True),
        snapshot.resolve(strict=True),
    )
    if actual != expected:
        raise ValueError("active Sentrix process is not using this round's restored data directory")
    if actual == frozen or actual.is_relative_to(frozen) or frozen.is_relative_to(actual):
        raise ValueError("active data directory and frozen snapshot must be independent")
    return {
        "status": "verified",
        "pid": pid,
        "start_time_ticks": recorded_start,
        "active_data_dir": str(actual),
        "frozen_snapshot": str(frozen),
        "verification": "PID identity and SENTRIX_DATA_DIR; active SQLite is not rehashed",
    }


def final_evidence(bench: str, run_id: str, output: Path, source_rows: Sequence[Json]) -> Json:
    url = bench + "/api/v1/runs/" + urllib.parse.quote(run_id, safe="")
    state = json.loads(get_text(url, 60))
    items = fetch_item_details(bench, run_id, state, source_rows)
    after = json.loads(get_text(url + "?include_history=false", 60))
    if any(after.get(key) != state.get(key) for key in ("read_revision", "item_count", "status")):
        raise ValueError("run changed while collecting final item evidence")
    state = {**state, "items": [items[i] for i in sorted(items)]}
    save_json(output / "final-run.json", state)
    samples: list[Json] = []
    for name in ("photobench-progress.jsonl", "model-metrics.jsonl"):
        path = output / name
        if path.exists():
            samples.extend(json.loads(line) for line in path.read_text().splitlines() if line.strip())
    report = summarize_run(state, samples)
    report["resource_failure"] = resource_failure(state)
    return report


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    for name in (
        "bench-url",
        "sentrix-url",
        "model-url",
        "allow-bench-host",
        "allow-sentrix-host",
        "allow-model-host",
        "model",
        "scope-id",
        "album-id",
        "qa-set",
        "qa-sha256",
    ):
        p.add_argument("--" + name, required=True)
    for name in (
        "identity",
        "expected-runtime-config",
        "memory-snapshot",
        "memory-identity",
        "qa-source-file",
        "output",
    ):
        p.add_argument("--" + name, type=Path, required=True)
    p.add_argument("--window", type=int, required=True)
    p.add_argument("--qa-concurrency", type=int, required=True)
    p.add_argument("--expected-count", type=int)
    p.add_argument(
        "--judge-concurrency",
        type=int,
        default=4,
        help="assert actual configured value; does not change Judge configuration",
    )
    p.add_argument("--turn-timeout-s", type=int, default=900)
    p.add_argument("--round-timeout-s", type=float, default=14400)
    p.add_argument("--poll-seconds", type=float, default=5)
    p.add_argument("--model-ssh")
    p.add_argument("--min-model-available-gib", type=float, default=8)
    p.add_argument("--sentrix-pid-file", type=Path)
    p.add_argument(
        "--sentrix-data-dir",
        type=Path,
        help="active restored data directory; required for a verified client copy",
    )
    p.add_argument(
        "--restore-receipt",
        type=Path,
        help="offline receipt; defaults to the restored data directory's sibling receipt",
    )
    p.add_argument(
        "--resume", action="store_true", help="observe prior known run; never replay a POST or resume QA"
    )
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if (
        min(args.qa_concurrency, args.judge_concurrency, args.window, args.turn_timeout_s) < 1
        or args.poll_seconds < 0.1
        or args.round_timeout_s <= 0
    ):
        raise ValueError("concurrency, windows and budgets must be positive")
    bench = service_url(args.bench_url, args.allow_bench_host)
    sentrix = service_url(args.sentrix_url, args.allow_sentrix_host)
    model_base = validate_endpoint(args.model_url, args.allow_model_host)
    if args.min_model_available_gib < 0 or (args.min_model_available_gib and not args.model_ssh):
        raise ValueError("a positive model-memory guard requires the model SSH sampler")
    if args.model_ssh:
        host = args.model_ssh.rsplit("@", 1)[-1]
        addresses = {x[4][0] for x in socket.getaddrinfo(host, 22)}
        if not addresses.intersection(x[4][0] for x in socket.getaddrinfo(args.allow_model_host, 80)):
            raise ValueError("SSH resource sampler must refer to the model host")
    identity = json.loads(args.identity.read_text())
    if not re.fullmatch(r"[a-fA-F0-9]{64}", str(identity.get("weights_sha256") or "")) or not identity.get(
        "engine_version"
    ):
        raise ValueError("deployment identity needs weight SHA256 and engine version")
    if (
        identity.get("model", args.model) != args.model
        or identity.get("max_model_len", args.window) != args.window
    ):
        raise ValueError("declared model identity differs from requested model/window")
    expected_runtime = json.loads(args.expected_runtime_config.read_text())
    memory_identity = json.loads(args.memory_identity.read_text())
    if memory_identity.get("scope_id") != args.scope_id:
        raise ValueError("scope differs from the frozen memory identity")
    source = args.qa_source_file.read_bytes()
    if hashlib.sha256(source).hexdigest() != args.qa_sha256:
        raise ValueError("QA source file hash mismatch")
    source_rows = [json.loads(line) for line in source.splitlines() if line.strip()]
    if args.restore_receipt and args.sentrix_data_dir is None:
        raise ValueError("restore receipt requires the active Sentrix data directory")
    if args.sentrix_data_dir and args.restore_receipt is None:
        args.restore_receipt = memory_snapshot.receipt_path_for(args.sentrix_data_dir)
    snapshot_manifest_sha = memory_snapshot.digest(args.memory_snapshot / "manifest.json")
    receipt_sha = memory_snapshot.digest(args.restore_receipt) if args.restore_receipt else None
    plan = {k: v for k, v in vars(args).items() if k not in {"resume", "output"}}
    plan = {k: str(v) if isinstance(v, Path) else v for k, v in plan.items()}
    plan.update(
        identity=identity,
        expected_runtime_config=expected_runtime,
        memory_identity=memory_identity,
        memory_manifest_sha256=snapshot_manifest_sha,
        restore_receipt_sha256=receipt_sha,
        safety_protocol=dict(SAFETY_PROTOCOL),
        model_sampler_source_sha256=hashlib.sha256(
            Path(__file__).with_name("remote_vllm_capacity.py").read_bytes()
        ).hexdigest(),
    )
    args.output.mkdir(parents=True, exist_ok=args.resume, mode=0o700)
    with (args.output / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        manifest_file = args.output / "manifest.json"
        if args.resume:
            manifest = json.loads(manifest_file.read_text())
            if manifest["plan"] != plan:
                raise ValueError("identity/configuration changed; use an independent run directory")
        else:
            manifest = {
                "request_id": "capacity-" + uuid.uuid4().hex,
                "plan": plan,
                "created_at": time.time(),
                "client_host": platform.node(),
                "model_identity_source": (
                    "operator_supplied_deployment_evidence; /models verifies name/window only"
                ),
            }
            save_json(manifest_file, manifest)
        memory_manifest = memory_snapshot.verify(args.memory_snapshot, memory_identity)
        active_copy = validate_active_data_copy(
            args.sentrix_data_dir, args.sentrix_pid_file, args.memory_snapshot
        )
        if args.sentrix_data_dir and active_copy["status"] != "verified":
            save_json(args.output / "active-data-copy.json", active_copy)
            raise ValueError("active Sentrix data-copy verification is unavailable")
        restored = restore_evidence(
            args.sentrix_data_dir, args.restore_receipt, args.memory_snapshot, memory_manifest
        )
        if memory_snapshot.digest(args.memory_snapshot / "manifest.json") != snapshot_manifest_sha or (
            restored.get("sha256") != receipt_sha
        ):
            raise ValueError("snapshot manifest or restore receipt changed during preflight")
        config = json.loads(get_text(bench + "/api/v1/config", 30))
        diagnostics = json.loads(get_text(sentrix + "/api/v1/diagnostics", 30))
        models = json.loads(get_text(model_base + "/v1/models", 10))
        embedding_identity.validate_space(memory_identity.get("embedding"))
        embedding_endpoints = embedding_connections(config, diagnostics, expected_runtime)
        embedding_health = {
            role: json.loads(get_text(base + "/health", 30)) for role, base in embedding_endpoints.items()
        }
        validate_configuration(
            config,
            diagnostics,
            models,
            sentrix=sentrix,
            model_base=model_base,
            model=args.model,
            window=args.window,
            expected_runtime=expected_runtime,
            embedding=memory_identity["embedding"],
            embedding_health=embedding_health,
        )
        query = urllib.parse.urlencode(
            {"album_id": args.album_id, "qa_set": args.qa_set, "sentrix_url": sentrix}
        )
        dataset = json.loads(get_text(bench + "/api/v1/qa-dataset?" + query, 60))
        count = validate_rows(source_rows, dataset, args.qa_concurrency, args.expected_count)
        save_json(
            args.output / "preflight.json",
            {
                "config": config,
                "diagnostics": diagnostics,
                "models": models,
                "embedding_health": embedding_health,
                "qa_count": count,
                "qa_sha256": args.qa_sha256,
                "memory_manifest_sha256": snapshot_manifest_sha,
                "active_data_copy": active_copy,
                "restore_evidence": restored,
                "restore_receipt_sha256": receipt_sha,
            },
        )
        ledger = Journal(args.output / "dispatch.jsonl")
        request_id = manifest["request_id"]
        event = ledger.latest().get(request_id)
        run_id = known_run_id(event)
        if event and not run_id:
            save_json(
                args.output / "summary.json",
                {
                    "state": "request_outcome_unknown",
                    "passed": False,
                    "request_id": request_id,
                    "reason": "POST was dispatched; observation has no known run ID and will not replay it",
                },
            )
            return 2
        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda _sig, _frame: stop.set())
        sampler = Sampler(
            model_base,
            args.model,
            args.output / "model-metrics.jsonl",
            stop,
            args.poll_seconds,
            args.model_ssh,
            int(args.min_model_available_gib * 2**30),
        )
        pid = int(json.loads(args.sentrix_pid_file.read_text())["pid"]) if args.sentrix_pid_file else None
        proc = process_sample(pid)
        if pid is not None and not proc.get("available"):
            raise ValueError("selected Sentrix process cannot be observed")
        # Validate telemetry before a new run; existing terminal runs need no model calls.
        if run_id:
            existing = json.loads(
                get_text(
                    bench + "/api/v1/runs/" + urllib.parse.quote(run_id, safe="") + "?include_history=false",
                    30,
                )
            )
            if existing.get("status") in TERMINAL:
                save_json(args.output / "latest-run.json", existing)
                save_json(
                    args.output / "summary.json",
                    {
                        "run_id": run_id,
                        "state": existing["status"],
                        "observed_only": True,
                        "progress": progress_view(existing),
                        "no_post_replayed": True,
                        "attribution": final_evidence(bench, run_id, args.output, source_rows),
                    },
                )
                return 0
        sampler.sample_once()
        if sampler.failure:
            save_json(
                args.output / "summary.json",
                {"passed": False, "failure": sampler.failure, "no_new_run_dispatched": not bool(run_id)},
            )
            return 2
        sampler.thread.start()
        try:
            if not run_id:
                if stop.is_set():
                    save_json(
                        args.output / "summary.json",
                        {
                            "passed": False,
                            "failure": sampler.failure or "operator_stop",
                            "no_new_run_dispatched": True,
                        },
                    )
                    return 2
                if args.resume:
                    raise ValueError("observation resume has no prior dispatch; refusing to start a new run")
                payload = {
                    "mode": "reuse",
                    "existing_scope_id": args.scope_id,
                    "album_id": args.album_id,
                    "qa_set": args.qa_set,
                    "models": [request_id],
                    "runtime_framework": "vllm",
                    "model_base_url": model_base + "/v1",
                    "endpoint_model": args.model,
                    "vllm_manager_url": "",
                    "sentrix_url": sentrix,
                    "qa_concurrency": args.qa_concurrency,
                    "turn_timeout": args.turn_timeout_s,
                }
                save_json(args.output / "request.json", payload)
                event = post_once(bench + "/api/v1/runs", payload, request_id, ledger)
                run_id = known_run_id(event)
                if not run_id:
                    save_json(
                        args.output / "summary.json",
                        {
                            "state": event["state"],
                            "passed": False,
                            "request_id": request_id,
                            "no_retry": True,
                        },
                    )
                    return 2
            result = poll_run(
                bench=bench,
                run_id=run_id,
                output=args.output,
                ledger=ledger,
                request_id=request_id,
                count=count,
                concurrency=args.qa_concurrency,
                judge_concurrency=args.judge_concurrency,
                stop=stop,
                sampler=sampler,
                poll_seconds=args.poll_seconds,
                deadline_seconds=args.round_timeout_s,
                client_pid=pid,
                client_start=proc.get("start_time_ticks"),
                source_rows=source_rows,
            )
            sampler.stop.set()
            sampler.thread.join(timeout=16)
            if sampler.thread.is_alive():
                result.update(passed=False, failure="model_sampler_did_not_stop")
            else:
                sampler.sample_once()
                if sampler.failure:
                    result.update(passed=False, failure=sampler.failure)
            try:
                result["attribution"] = final_evidence(bench, run_id, args.output, source_rows)
                if result["attribution"]["resource_failure"]:
                    result.update(passed=False, failure="resource_failure")
                elif any(
                    result["attribution"]["operational"].get(key)
                    for key in ("not_evaluable_count", "judge_incomplete_count")
                ):
                    result.update(passed=False, failure="operational_issue")
            except Exception as exc:
                result.update(
                    passed=False,
                    failure="final_run_evidence_unavailable",
                    evidence_error_type=type(exc).__name__,
                )
            save_json(args.output / "summary.json", result)
            print(
                json.dumps(
                    {
                        "run_id": run_id,
                        "passed": result["passed"],
                        "failure": result["failure"],
                        "summary": str(args.output / "summary.json"),
                    }
                )
            )
            return 0 if result["passed"] else 2
        finally:
            sampler.stop.set()
            sampler.thread.join(timeout=16)


if __name__ == "__main__":
    raise SystemExit(main())
