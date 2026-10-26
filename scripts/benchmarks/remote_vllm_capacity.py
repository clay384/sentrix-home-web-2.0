#!/usr/bin/env python3
"""Explicit remote-model load probe; never manages a model or Sentrix process.

Requests are OpenAI chat objects in a caller-supplied JSONL file. This measures
model requests, not complete Sentrix QA tasks. Each round uses the same order.
POSTs are journalled before dispatch and are never retried, including on resume.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import hashlib
import json
import math
import os
import platform
import re
import resource
import signal
import socket
import statistics
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

Json = dict[str, Any]
PREEMPTIONS_STOP = 2
QUEUE_GROWTH_STOP = 3
SWAP_GROWTH_STOP = 2
SAFETY_PROTOCOL: Json = {
    "version": 2,
    "preemption_baseline": "first_valid_sample_of_round",
    "preemptions_since_baseline_stop_at": PREEMPTIONS_STOP,
    "queue_consecutive_growth_intervals_stop_at": QUEUE_GROWTH_STOP,
    "swap_consecutive_growth_intervals_stop_at": SWAP_GROWTH_STOP,
    "counter_reset_or_process_restart": "stop_immediately",
}
_SAMPLE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)(?:\s+\S+)?$")
_LABEL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')
_ALIASES = {
    "running": ("num_requests_running",),
    "waiting": ("num_requests_waiting",),
    "kv_usage_fraction": ("kv_cache_usage_perc", "gpu_cache_usage_perc"),
    "preemptions_total": ("num_preemptions_total",),
    "prompt_tokens_total": ("prompt_tokens_total",),
    "output_tokens_total": ("generation_tokens_total",),
    "ttft_sum_s": ("time_to_first_token_seconds_sum",),
    "ttft_count": ("time_to_first_token_seconds_count",),
    "queue_sum_s": ("request_queue_time_seconds_sum",),
    "queue_count": ("request_queue_time_seconds_count",),
    "e2e_sum_s": ("e2e_request_latency_seconds_sum",),
    "e2e_count": ("e2e_request_latency_seconds_count",),
    "success_total": ("request_success_total",),
    "process_start_time": ("process_start_time_seconds",),
}
# Fixed read-only command: callers cannot supply shell code or additional SSH options.
_HOST_PROBE = """
import json,pathlib,socket,subprocess,time
p=pathlib.Path('/proc'); result={'host':socket.gethostname(),'observed_at':time.time()}
try:
 mem={line.split(':')[0]:int(line.split()[1])*1024 for line in (p/'meminfo').read_text().splitlines()}
 vm={line.split()[0]:int(line.split()[1]) for line in (p/'vmstat').read_text().splitlines()}
 result.update(available_bytes=mem.get('MemAvailable'),swap_used_bytes=mem['SwapTotal']-mem['SwapFree'],swap_out_pages=vm.get('pswpout'))
except Exception as exc: result['memory_error']=type(exc).__name__
try:
 result['host_gpus_csv']=subprocess.check_output(['nvidia-smi','--query-gpu=uuid,memory.used,memory.free,utilization.gpu,temperature.gpu','--format=csv,noheader,nounits'],text=True,timeout=3)
except Exception as exc: result['gpu_unavailable']=type(exc).__name__
print(json.dumps(result))
"""


def guard_policy() -> Json:
    """Return a copy of the safety policy for freezing into a caller's manifest."""
    return dict(SAFETY_PROTOCOL)


def parse_metrics(raw: str, model: str | None = None) -> Json:
    """Keep absent/version-specific measurements as None, not fictitious zero."""
    samples: dict[str, list[float]] = {}
    for line in raw.splitlines():
        match = _SAMPLE.fullmatch(line.strip())
        if not match:
            continue
        name, labels_raw, value_raw = match.groups()
        labels = {k: json.loads('"' + v + '"') for k, v in _LABEL.findall(labels_raw or "")}
        if model and labels.get("model_name", model) != model:
            continue
        try:
            value = float(value_raw)
        except ValueError:
            continue
        if math.isfinite(value):
            samples.setdefault(name.removeprefix("vllm:"), []).append(value)
    result: Json = {}
    for field, aliases in _ALIASES.items():
        values = next((samples[name] for name in aliases if name in samples), None)
        result[field] = (max(values) if field == "kv_usage_fraction" else sum(values)) if values else None
    return result


def validate_endpoint(base_url: str, allowed_host: str) -> str:
    url = urllib.parse.urlsplit(base_url)
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.hostname != allowed_host
        or url.path.rstrip("/") not in {"", "/v1"}
    ):
        raise ValueError("explicit allowed model host and a plain HTTP(S) /v1 URL are required")
    addresses = {item[4][0] for item in socket.getaddrinfo(url.hostname, url.port or 80)}
    if "192.168.0.153" in addresses:
        raise ValueError("the protected 153 service cannot receive this probe")
    return urllib.parse.urlunsplit((url.scheme, url.netloc, "", "", ""))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def get_text(url: str, timeout: float = 5) -> str:
    with opener().open(url, timeout=timeout) as response:
        return str(response.read().decode())


def client_sample() -> Json:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return {
        "host": platform.node(),
        "scope": "load_generator_process_not_remote_model",
        "pid": os.getpid(),
        "cpu_user_seconds": usage.ru_utime,
        "cpu_system_seconds": usage.ru_stime,
        "peak_rss_bytes": usage.ru_maxrss * (1 if platform.system() == "Darwin" else 1024),
        "host_load_average": os.getloadavg(),
    }


def remote_host_sample(target: str) -> Json:
    if target.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.@:\-]+", target):
        raise ValueError("invalid SSH target")
    completed = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", target, "python3 -"],
        input=_HOST_PROBE,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    result: Json = json.loads(completed.stdout)
    result["scope"] = "remote_model_host_not_process_attribution"
    return result


class Journal:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.reserved: set[str] | None = None

    def reserve(self, event: Json) -> None:
        with self.lock:
            if self.reserved is None:
                self.reserved = set(self.latest())
            key = str(event["request_id"])
            if key in self.reserved:
                raise ValueError("request ID already dispatched; refusing replay")
            self.reserved.add(key)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())

    def append(self, event: Json) -> None:
        with self.lock, self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def latest(self) -> dict[str, Json]:
        if not self.path.exists():
            return {}
        result = {}
        for line in self.path.read_text().splitlines():
            row = json.loads(line)
            result[row["request_id"]] = row
        return result


def request_once(base: str, body: Json, request_id: str, timeout: float, journal: Journal) -> Json:
    payload = json.dumps({**body, "stream": True, "stream_options": {"include_usage": True}}).encode()
    started = time.time()
    event: Json = {
        "request_id": request_id,
        "state": "request_outcome_unknown",
        "started_at": started,
        "request_sha256": hashlib.sha256(payload).hexdigest(),
    }
    journal.reserve(event)  # Durable before any network IO; never replay this ID.
    try:
        req = urllib.request.Request(
            base + "/v1/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json", "X-Request-Id": request_id},
        )
        done = False
        first = None
        usage = None
        digest = hashlib.sha256()
        with opener().open(req, timeout=timeout) as response:
            for raw in response:
                if time.time() - started > timeout:
                    raise TimeoutError("request wall-time budget exceeded")
                digest.update(raw)
                if not raw.startswith(b"data: "):
                    continue
                text = raw[6:].strip()
                if text == b"[DONE]":
                    done = True
                    continue
                chunk = json.loads(text)
                if chunk.get("error"):
                    raise RuntimeError("server emitted an error event")
                if chunk.get("usage"):
                    usage = chunk["usage"]
                if first is None and any(
                    choice.get("delta", {}).get("content")
                    or choice.get("delta", {}).get("tool_calls")
                    or choice.get("delta", {}).get("reasoning")
                    for choice in chunk.get("choices", [])
                ):
                    first = time.time() - started
        event.update(
            state="completed" if done and usage and first is not None else "request_outcome_unknown",
            ttft_seconds=first,
            usage=usage,
            response_sha256=digest.hexdigest(),
        )
    except urllib.error.HTTPError as exc:
        # Retain status and error digest, not arbitrary server echoes of user prompts.
        error = exc.read()
        event.update(
            state="http_failed", http_status=exc.code, error_sha256=hashlib.sha256(error).hexdigest()
        )
    except Exception as exc:
        event["error_type"] = type(exc).__name__
    event["elapsed_seconds"] = time.time() - started
    journal.append(event)
    return event


class SafetyGuard:
    def __init__(self, min_available_bytes: int = 0) -> None:
        self.minimum = min_available_bytes
        self.previous: Json | None = None
        self.preemption_baseline: float | None = None
        self.preemptions_since_baseline = 0.0
        self.wait_growth = 0
        self.swap_growth = 0

    def check(self, sample: Json) -> str | None:
        metrics = sample.get("remote_model", {}).get("metrics")
        if not metrics:
            return "model_metrics_unavailable"
        if any(metrics.get(key) is None for key in ("running", "waiting", "preemptions_total")):
            return "required_model_metrics_unavailable"
        if self.preemption_baseline is None:
            self.preemption_baseline = float(metrics["preemptions_total"])
        self.preemptions_since_baseline = float(metrics["preemptions_total"]) - self.preemption_baseline
        previous = self.previous
        self.previous = sample
        host = sample.get("remote_host", {})
        available = host.get("available_bytes")
        if self.minimum and (available is None or available < self.minimum):
            return "remote_available_memory_below_guard_or_unavailable"
        if previous:
            old = previous["remote_model"]["metrics"]
            if (
                metrics.get("process_start_time") is not None
                and old.get("process_start_time") is not None
                and metrics["process_start_time"] != old["process_start_time"]
            ):
                return "model_process_restart"
            for counter in ("preemptions_total", "prompt_tokens_total", "output_tokens_total"):
                current, prior = metrics.get(counter), old.get(counter)
                if current is not None and prior is not None and current < prior:
                    return "model_counter_reset_possible_restart"
            if self.preemptions_since_baseline >= PREEMPTIONS_STOP:
                return "repeated_kv_preemption"
            waiting, old_waiting = metrics.get("waiting"), old.get("waiting")
            self.wait_growth = (
                self.wait_growth + 1
                if (waiting is not None and old_waiting is not None and waiting > old_waiting)
                else 0
            )
            swap, old_swap = host.get("swap_out_pages"), previous.get("remote_host", {}).get("swap_out_pages")
            self.swap_growth = (
                self.swap_growth + 1 if (swap is not None and old_swap is not None and swap > old_swap) else 0
            )
            if self.wait_growth >= QUEUE_GROWTH_STOP:
                return "persistent_wait_queue_growth"
            if self.swap_growth >= SWAP_GROWTH_STOP:
                return "sustained_remote_swap_growth"
        return None


class Sampler:
    def __init__(
        self,
        base: str,
        model: str,
        output: Path,
        stop_dispatch: threading.Event,
        interval: float,
        ssh_target: str | None,
        min_available_bytes: int,
    ) -> None:
        self.base, self.model, self.output = base, model, output
        self.stop_dispatch, self.interval, self.ssh_target = stop_dispatch, interval, ssh_target
        self.guard = SafetyGuard(min_available_bytes)
        self.stop = threading.Event()
        self.failure: str | None = None
        self.first_metrics: Json | None = None
        self.last_metrics: Json | None = None
        self.peaks: Json = {}
        self.thread = threading.Thread(target=self.loop, daemon=True)

    def collect(self) -> Json:
        sample: Json = {"observed_at": time.time(), "client": client_sample()}
        remote: Json = {"endpoint": self.base, "scope": "remote_model_endpoint"}
        try:
            raw = get_text(self.base + "/metrics")
            remote.update(raw_prometheus=raw, metrics=parse_metrics(raw, self.model))
        except Exception as exc:
            remote["error_type"] = type(exc).__name__
        sample["remote_model"] = remote
        if self.ssh_target:
            try:
                sample["remote_host"] = remote_host_sample(self.ssh_target)
            except Exception as exc:
                sample["remote_host"] = {"unavailable": type(exc).__name__}
        return sample

    def sample_once(self) -> None:
        sample = self.collect()
        failure = self.guard.check(sample)
        sample["safety_protocol"] = SAFETY_PROTOCOL
        sample["safety_observation"] = {
            "preemption_baseline": self.guard.preemption_baseline,
            "preemptions_since_baseline": self.guard.preemptions_since_baseline,
            "queue_growth_intervals": self.guard.wait_growth,
            "swap_growth_intervals": self.guard.swap_growth,
            "stop_reason": failure,
        }
        metrics = sample.get("remote_model", {}).get("metrics")
        if metrics:
            if self.first_metrics is None:
                self.first_metrics = metrics
            self.last_metrics = metrics
            for key in ("running", "waiting", "kv_usage_fraction"):
                if metrics.get(key) is not None:
                    self.peaks[key] = max(self.peaks.get(key, 0), metrics[key])
        Journal(self.output).append(sample)
        if failure:
            self.failure = failure
            self.stop_dispatch.set()

    def loop(self) -> None:
        while not self.stop.wait(self.interval):
            try:
                self.sample_once()
            except Exception as exc:
                self.failure = "sampler_failure:" + type(exc).__name__
                self.stop_dispatch.set()
                return


def run_round(
    *,
    base: str,
    model: str,
    bodies: Sequence[Json],
    concurrency: int,
    count: int,
    duration_s: float,
    max_requests: int,
    timeout: float,
    round_id: str,
    output: Path,
    stop: threading.Event,
    sample_interval: float,
    ssh_target: str | None,
    min_available_bytes: int,
) -> Json:
    journal = Journal(output / "requests.jsonl")
    existing = journal.latest()
    resumed = any(key.startswith(round_id + "-") for key in existing)
    prior = [row for key, row in existing.items() if key.startswith(round_id + "-")]
    unsuccessful = [row for row in prior if row["state"] != "completed"]
    if unsuccessful:
        unknown_ids = [row["request_id"] for row in unsuccessful if row["state"] == "request_outcome_unknown"]
        return {
            "round_id": round_id,
            "concurrency": concurrency,
            "passed": False,
            "failure": (
                "unknown_requests_quarantined_no_replay"
                if unknown_ids
                else "previous_request_failure_no_replay"
            ),
            "unknown_ids": unknown_ids,
            "resumed": True,
            "finished": len(prior),
            "failed": len(unsuccessful),
            "completed": len(prior) - len(unsuccessful),
            "requests_per_second": None,
        }
    sampler = Sampler(
        base,
        model,
        output / (round_id + ".metrics.jsonl"),
        stop,
        sample_interval,
        ssh_target,
        min_available_bytes,
    )
    sampler.sample_once()
    sampler.thread.start()
    started = time.monotonic()
    results: list[Json] = []
    submitted = 0
    failure: str | None = None
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            pending: dict[concurrent.futures.Future[Json], str] = {}
            while True:
                while not stop.is_set() and len(pending) < concurrency and submitted < max_requests:
                    if submitted >= count and time.monotonic() - started >= duration_s:
                        break
                    request_id = f"{round_id}-{submitted:06d}"
                    body = {**bodies[submitted % len(bodies)], "model": model}
                    submitted += 1
                    if request_id in existing:
                        results.append(existing[request_id])
                        continue
                    pending[pool.submit(request_once, base, body, request_id, timeout, journal)] = request_id
                if not pending:
                    break
                finished, _ = concurrent.futures.wait(pending, return_when=concurrent.futures.FIRST_COMPLETED)
                for future in finished:
                    pending.pop(future)
                    row = future.result()
                    results.append(row)
                    if row["state"] != "completed":
                        failure = row["state"]
                        stop.set()
    finally:
        sampler.stop.set()
        sampler.thread.join(timeout=16)
        if sampler.thread.is_alive():
            failure = "sampler_not_stopped"
        else:
            sampler.sample_once()
    elapsed = time.monotonic() - started
    if elapsed < duration_s and submitted >= max_requests and not failure:
        failure = "request_cap_reached_before_requested_duration"
    completed_rows = [row for row in results if row["state"] == "completed"]
    passed = not (failure or sampler.failure or stop.is_set()) and len(completed_rows) >= count
    totals = {
        key: sum((row.get("usage") or {}).get(key, 0) for row in completed_rows)
        for key in ("prompt_tokens", "completion_tokens")
    }
    deltas: Json = {}
    if sampler.first_metrics and sampler.last_metrics:
        for key, first in sampler.first_metrics.items():
            last = sampler.last_metrics.get(key)
            if key.endswith(("_total", "_sum_s", "_count")):
                deltas[key] = (
                    last - first if first is not None and last is not None and last >= first else None
                )
    ttfts = [float(row["ttft_seconds"]) for row in completed_rows if row.get("ttft_seconds") is not None]
    return {
        "round_id": round_id,
        "concurrency": concurrency,
        "completed": len(completed_rows),
        "finished": len(results),
        "failed": len(results) - len(completed_rows),
        "elapsed_seconds": elapsed,
        "requests_per_second": len(completed_rows) / elapsed,
        "mean_ttft_seconds": statistics.mean(ttfts) if ttfts else None,
        "token_totals": totals,
        "output_tokens_per_second": totals["completion_tokens"] / elapsed,
        "remote_metric_deltas": deltas,
        "remote_metric_peaks": sampler.peaks,
        "http_429_requests": sum(row.get("http_status") == 429 for row in results),
        "passed": passed,
        "failure": failure or sampler.failure,
        "duration_requirement_met": elapsed >= duration_s,
        "resumed": resumed,
        "request_limit_reached": submitted >= max_requests,
    }


def capacity_report(rounds: Sequence[Json]) -> Json:
    groups: dict[int, list[Json]] = {}
    seen: set[str] = set()
    for row in rounds:
        if row.get("round_id"):
            if row["round_id"] in seen:
                raise ValueError("duplicate round evidence cannot count as a repeat")
            seen.add(row["round_id"])
        groups.setdefault(int(row["concurrency"]), []).append(row)
    passed = {n: rows for n, rows in groups.items() if all(row.get("passed") for row in rows)}
    qualified = {
        n: rows
        for n, rows in passed.items()
        if len(rows) >= 2
        and max(float(row.get("elapsed_seconds", 0)) for row in rows) >= 1800
        and all(not row.get("resumed") and row.get("completed", 0) >= max(32, 2 * n) for row in rows)
    }
    rates = {
        n: sum(row["completed"] for row in rows) / sum(row["elapsed_seconds"] for row in rows)
        for n, rows in qualified.items()
    }
    best = max(rates.values(), default=0)
    recommended = min((n for n, rate in rates.items() if rate >= 0.95 * best), default=None)
    highest = max(qualified, default=None)
    failed = {n for n, rows in groups.items() if any(not row.get("passed") for row in rows)}
    # A raw failure can be an invalid request, observer fault, operator stop or
    # unrelated host load. Only an explicitly reviewed derived annotation may
    # establish a capacity boundary; never edit the original round to add it.
    confirmations = [
        {
            "concurrency": int(row["concurrency"]),
            "round_id": row.get("round_id"),
            "reason": row["capacity_boundary_reason"],
            "evidence": list(row["capacity_boundary_evidence"]),
        }
        for row in rounds
        if row.get("passed") is False
        and row.get("capacity_boundary_confirmed") is True
        and isinstance(row.get("capacity_boundary_reason"), str)
        and row["capacity_boundary_reason"].strip()
        and isinstance(row.get("capacity_boundary_evidence"), list)
        and row["capacity_boundary_evidence"]
        and all(
            isinstance(reference, str) and reference.strip()
            for reference in row["capacity_boundary_evidence"]
        )
    ]
    confirmed = {entry["concurrency"] for entry in confirmations}
    adjacent_boundary = highest is not None and highest + 1 in confirmed
    return {
        "scope": "remote_model_requests_not_sentrix_qa_or_m4_cpu_capacity",
        "observed_pass_at_least": max(passed, default=None),
        "qualified_stable_at_least": highest,
        "maximum_stable_concurrency": highest if adjacent_boundary else None,
        "failure_boundary_observed": bool(confirmed),
        "failed_trial_concurrencies": sorted(failed),
        "inconclusive_failed_concurrencies": sorted(failed - confirmed),
        "capacity_boundary_confirmations": confirmations,
        "capacity_boundary_rule": "derived confirmation with reason and evidence; adjacent integer required",
        "recommended_concurrency": recommended,
        "selection_rule": "highest qualified throughput; choose lower concurrency within 5%",
        "qualification": (
            "two rounds, one uninterrupted round >=1800 seconds; "
            ">=max(32,2N) each; no faults or resumed timing"
        ),
        "rounds": list(rounds),
    }


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--allow-model-host", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--window", required=True, type=int)
    parser.add_argument(
        "--identity", required=True, type=Path, help="deployment identity/weight manifest JSON"
    )
    parser.add_argument("--requests", required=True, type=Path, help="fixed-order OpenAI chat request JSONL")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--concurrencies", type=int, nargs="+", default=[1, 2, 4, 8])
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--samples", type=int, default=32)
    parser.add_argument("--duration-s", type=float, default=0)
    parser.add_argument("--max-requests", type=int, default=4096)
    parser.add_argument("--timeout-s", type=float, default=180)
    parser.add_argument("--sample-interval-s", type=float, default=2)
    parser.add_argument("--ssh-host", help="optional explicit read-only model-host sampling")
    parser.add_argument("--min-remote-available-gib", type=float, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--observe-only-seconds", type=float, default=0, help="collect telemetry without POSTs"
    )
    return parser


@contextmanager
def output_lease(output: Path) -> Iterator[None]:
    """One process owns an experiment, including resume; never unlink the lock inode."""
    with (output / ".capacity.lock").open("a") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("experiment output directory is already in use") from exc
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def main(argv: Sequence[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    if (
        min(args.concurrencies) < 1
        or max(args.concurrencies) > 256
        or args.rounds < 1
        or args.samples < 1
        or args.timeout_s <= 0
        or args.window <= 0
        or args.observe_only_seconds < 0
        or args.min_remote_available_gib < 0
    ):
        raise ValueError("concurrency, rounds, samples and timeout must be positive")
    if (
        args.sample_interval_s < 0.1
        or args.duration_s < 0
        or args.max_requests < max(32, 2 * max(args.concurrencies))
    ):
        raise ValueError("invalid sample interval, duration or request cap")
    if len(set(args.concurrencies)) != len(args.concurrencies):
        raise ValueError("duplicate concurrency stages would reuse evidence")
    base = validate_endpoint(args.base_url, args.allow_model_host)
    if args.ssh_host:
        ssh_name = args.ssh_host.rsplit("@", 1)[-1]
        addresses = {row[4][0] for row in socket.getaddrinfo(ssh_name, 22)}
        model_addresses = {row[4][0] for row in socket.getaddrinfo(args.allow_model_host, 80)}
        if not addresses.intersection(model_addresses):
            raise ValueError("SSH sampler must address the model host, not the client host")
    identity = json.loads(args.identity.read_text())
    bodies = [json.loads(line) for line in args.requests.read_text().splitlines() if line.strip()]
    if not bodies or any(not row.get("messages") or "max_tokens" not in row for row in bodies):
        raise ValueError("each workload request needs messages and an explicit max_tokens budget")
    models = json.loads(get_text(base + "/v1/models"))
    selected = next((row for row in models.get("data", []) if row.get("id") == args.model), None)
    if not selected or selected.get("max_model_len") != args.window:
        raise ValueError("served model/window does not match the explicitly requested experiment")
    plan = {
        "base_url": base,
        "model": args.model,
        "window": args.window,
        "identity": identity,
        "workload_sha256": hashlib.sha256(args.requests.read_bytes()).hexdigest(),
        "concurrencies": args.concurrencies,
        "rounds": args.rounds,
        "samples": args.samples,
        "duration_s": args.duration_s,
        "max_requests": args.max_requests,
        "timeout_s": args.timeout_s,
        "ssh_host": args.ssh_host,
        "min_remote_available_gib": args.min_remote_available_gib,
        "sample_interval_s": args.sample_interval_s,
        "safety_protocol": SAFETY_PROTOCOL,
    }
    if not args.resume:
        args.output.mkdir(parents=True, exist_ok=False)
    with output_lease(args.output):
        return execute_plan(args, base, identity, bodies, models, plan)


def execute_plan(
    args: argparse.Namespace, base: str, identity: Json, bodies: Sequence[Json], models: Json, plan: Json
) -> int:
    manifest = args.output / "manifest.json"
    if args.resume:
        if json.loads(manifest.read_text())["plan"] != plan:
            raise ValueError("resume identity/configuration differs; use a new experiment directory")
    else:
        manifest.write_text(
            json.dumps(
                {
                    "plan": plan,
                    "models": models,
                    "client_host": platform.node(),
                    "created_at": time.time(),
                    "run_id": uuid.uuid4().hex,
                    "identity_source": "operator_supplied_deployment_manifest",
                },
                indent=2,
            )
        )
    run_id = json.loads(manifest.read_text())["run_id"]
    stop = threading.Event()

    def stop_handler(_signum: int, _frame: Any) -> None:
        stop.set()

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)
    if args.observe_only_seconds:
        sampler = Sampler(
            base,
            args.model,
            args.output / "observe.metrics.jsonl",
            stop,
            args.sample_interval_s,
            args.ssh_host,
            int(args.min_remote_available_gib * 2**30),
        )
        deadline = time.monotonic() + args.observe_only_seconds
        while not stop.is_set() and time.monotonic() < deadline:
            sampler.sample_once()
            stop.wait(min(args.sample_interval_s, max(0, deadline - time.monotonic())))
        return 2 if sampler.failure else 0
    rounds: list[Json] = []
    for concurrency in args.concurrencies:
        for index in range(args.rounds):
            rid = f"{run_id}-c{concurrency}-r{index + 1}"
            path = args.output / (rid + ".json")
            if path.exists():
                row = json.loads(path.read_text())
            else:
                row = run_round(
                    base=base,
                    model=args.model,
                    bodies=bodies,
                    concurrency=concurrency,
                    count=max(args.samples, 32, 2 * concurrency),
                    duration_s=args.duration_s,
                    max_requests=args.max_requests,
                    timeout=args.timeout_s,
                    round_id=rid,
                    output=args.output,
                    stop=stop,
                    sample_interval=args.sample_interval_s,
                    ssh_target=args.ssh_host,
                    min_available_bytes=int(args.min_remote_available_gib * 2**30),
                )
                path.write_text(json.dumps(row, indent=2))
            rounds.append(row)
            (args.output / "report.json").write_text(json.dumps(capacity_report(rounds), indent=2))
            if not row.get("passed") or stop.is_set():
                return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
