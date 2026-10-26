#!/usr/bin/env python3
"""Sanitized phase attribution from PhotoBench checkpoints and capacity JSONL logs.

No network or product imports. Missing measurements stay null. Worker-duration
sums are not phase wall time, and retrieval tool wall time is not CPU time.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

Json = dict[str, Any]
_PROGRESS_KEYS = (
    "agent_waiting",
    "agent_running",
    "judge_waiting",
    "judge_running",
    "agent_completed",
    "agent_total",
    "judge_completed",
    "judge_total",
    "judge_skipped",
)
_REQUEST_KEYS = (
    "requests_started",
    "responses_received",
    "request_errors",
    "http_429",
    "in_flight",
    "rate_waiting",
    "rate_wait_ms",
)
_MODEL_KEYS = (
    "running",
    "waiting",
    "kv_usage_fraction",
    "preemptions_total",
    "prompt_tokens_total",
    "output_tokens_total",
    "ttft_sum_s",
    "ttft_count",
    "queue_sum_s",
    "queue_count",
    "e2e_sum_s",
    "e2e_count",
    "success_total",
)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def _time(value: Any) -> float | None:
    numeric = _number(value)
    if numeric is not None:
        return numeric
    if not isinstance(value, str):
        return None
    try:
        date = datetime.fromisoformat(value.removesuffix("Z") + ("+00:00" if value.endswith("Z") else ""))
        return date.timestamp() if date.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _interval(phase: Mapping[str, Any], prefix: str = "") -> tuple[float, float] | None:
    start, end = _time(phase.get(prefix + "started_at")), _time(phase.get(prefix + "finished_at"))
    return (start, end) if start is not None and end is not None and end >= start else None


def _wall(phase: Mapping[str, Any], prefix: str = "") -> float | None:
    explicit = _number(phase.get(prefix + "wall_ms")) if prefix else _number(phase.get("total_seconds"))
    if explicit is not None:
        return explicit if prefix else explicit * 1000
    interval = _interval(phase, prefix)
    return (interval[1] - interval[0]) * 1000 if interval else None


def _stats(values: Sequence[float]) -> Json:
    ordered = sorted(values)
    return {
        "samples": len(ordered),
        "sum_ms": sum(ordered) if ordered else None,
        "mean_ms": statistics.mean(ordered) if ordered else None,
        "p50_ms": statistics.median(ordered) if ordered else None,
        "p95_ms": ordered[math.ceil(len(ordered) * 0.95) - 1] if ordered else None,
        "max_ms": max(ordered) if ordered else None,
    }


def _values(rows: Sequence[Mapping[str, Any]], *keys: str) -> list[float]:
    result = []
    for row in rows:
        for key in keys:
            value = _number(row.get(key))
            if value is not None:
                result.append(value)
                break
    return result


def _points(points: Sequence[tuple[float, Mapping[str, Any]]], key: str) -> list[tuple[float, float]]:
    by_time: dict[float, set[float]] = {}
    for at, row in points:
        value = _number(row.get(key))
        if value is not None:
            by_time.setdefault(at, set()).add(value)
    # Same timestamp conflicts are not an ordered series; omit them conservatively.
    return sorted((at, next(iter(values))) for at, values in by_time.items() if len(values) == 1)


def _series(points: Sequence[tuple[float, Mapping[str, Any]]], key: str) -> Json:
    values = _points(points, key)
    increasing = 0
    sustained = False
    for (_, previous), (_, current) in zip(values, values[1:], strict=False):
        increasing = increasing + 1 if current > previous else 0
        sustained = sustained or increasing >= 3
    return {
        "samples": len(values),
        "first": values[0][1] if values else None,
        "last": values[-1][1] if values else None,
        "peak": max(v for _, v in values) if values else None,
        "observed_span_seconds": values[-1][0] - values[0][0] if len(values) >= 2 else None,
        "net_change": values[-1][1] - values[0][1] if len(values) >= 2 else None,
        "growth_over_three_intervals": sustained if len(values) >= 4 else None,
    }


def _delta(points: Sequence[tuple[float, Mapping[str, Any]]], key: str) -> float | None:
    values = _points(points, key)
    if len(values) < 2 or values[-1][0] <= values[0][0]:
        return None
    if any(b < a for (_, a), (_, b) in zip(values, values[1:], strict=False)):
        return None  # Counter reset: endpoints/restarts cannot be combined as one interval.
    return values[-1][1] - values[0][1]


def _counter_mean(points: Sequence[tuple[float, Mapping[str, Any]]], stem: str) -> float | None:
    paired = [
        (at, row)
        for at, row in points
        if _number(row.get(stem + "_sum_s")) is not None and _number(row.get(stem + "_count")) is not None
    ]
    total, count = _delta(paired, stem + "_sum_s"), _delta(paired, stem + "_count")
    return total * 1000 / count if total is not None and count is not None and count > 0 else None


def _compact_sample(row: Mapping[str, Any]) -> Json:
    """Discard raw Prometheus, host inventories, paths and all content at read time."""
    remote = _mapping(row.get("remote_model"))
    return {
        "observed_at": row.get("observed_at"),
        "run_id": row.get("run_id"),
        "progress": {key: _number(_mapping(row.get("progress")).get(key)) for key in _PROGRESS_KEYS},
        "judge_requests": {
            key: _number(_mapping(row.get("judge_requests")).get(key)) for key in _REQUEST_KEYS
        },
        "remote_model": {
            "endpoint": remote.get("endpoint"),
            "metrics": {key: _number(_mapping(remote.get("metrics")).get(key)) for key in _MODEL_KEYS},
        }
        if remote
        else {},
    }


def _item_outcomes(items: Sequence[Mapping[str, Any]], available: bool) -> Json:
    """Execution completion, scoring completion and valid grades are distinct facts."""

    def count(key: str, statuses: set[str]) -> int | None:
        return (
            sum(item.get(key) in statuses for item in items if isinstance(item.get(key), str))
            if available
            else None
        )

    agent_statuses = {
        "completed",
        "failed",
        "timeout",
        "outcome_unknown",
        "scheduled",
        "pending",
        "started",
        "running",
    }
    judge_statuses = {"completed", "failed", "skipped", "outcome_unknown", "pending", "running"}

    def valid_score(item: Mapping[str, Any]) -> bool:
        score = _mapping(item.get("judge")).get("score")
        return isinstance(score, (int, float)) and not isinstance(score, bool) and score in (0, 1, 2)

    def missing_status(key: str, known: set[str]) -> int | None:
        return (
            sum(not isinstance(item.get(key), str) or item[key] not in known for item in items)
            if available
            else None
        )

    return {
        "agent": {
            "execution_completed": count("execution_status", {"completed"}),
            "failed": count("execution_status", {"failed"}),
            "timeout": count("execution_status", {"timeout"}),
            "outcome_unknown": count("execution_status", {"outcome_unknown"}),
            "not_started": count("execution_status", {"scheduled"}),
            "in_flight": count("execution_status", {"pending", "started", "running"}),
            "status_not_recorded_or_unrecognized": missing_status("execution_status", agent_statuses),
        },
        "judge": {
            **{status: count("judge_status", {status}) for status in sorted(judge_statuses)},
            "status_not_recorded_or_unrecognized": missing_status("judge_status", judge_statuses),
            "valid_answer_scores": sum(valid_score(item) for item in items) if available else None,
            "completed_without_valid_answer_score": sum(
                item.get("judge_status") == "completed" and not valid_score(item) for item in items
            )
            if available
            else None,
        },
        "interpretation": (
            "Execution completion does not imply a correct answer. Judge completion does not imply a valid "
            "answer-quality grade; null may also mean not applicable. Valid grades include zero."
        ),
    }


def summarize_run(state: Mapping[str, Any], samples: Sequence[Mapping[str, Any]] = ()) -> Json:
    """Return fixed-key aggregate facts only; never return source prose or identifiers."""
    phase = _mapping(_mapping(state.get("phases")).get("qa_eval"))
    progress = _mapping(phase.get("progress"))
    agent_interval, judge_interval = _interval(phase, "agent_phase_"), _interval(phase, "judge_phase_")
    overlap = lag = None
    if agent_interval and judge_interval:
        overlap = (
            max(0, min(agent_interval[1], judge_interval[1]) - max(agent_interval[0], judge_interval[0]))
            * 1000
        )
        lag = max(0, judge_interval[1] - agent_interval[1]) * 1000
    raw_items = state.get("items")
    items = [row for row in raw_items if isinstance(row, Mapping)] if isinstance(raw_items, list) else []
    calls: list[Mapping[str, Any]] = []
    tools: list[Mapping[str, Any]] = []
    call_items = tool_items = 0
    agent_durations: list[float] = []
    for item in items:
        duration = _number(_mapping(item.get("timing_breakdown")).get("agent_wall_ms"))
        if duration is not None:
            agent_durations.append(duration)
        # Prefer the canonical flattened call list, never count its nested copy twice.
        if isinstance(item.get("model_call_metrics"), list):
            call_items += 1
            calls.extend(row for row in item["model_call_metrics"] if isinstance(row, Mapping))
        else:
            turns = item.get("runtime_turns")
            nested = (
                [_mapping(_mapping(turn).get("trace_bundle")).get("model_calls") for turn in turns]
                if isinstance(turns, list)
                else []
            )
            recorded = [rows for rows in nested if isinstance(rows, list)]
            if recorded:
                call_items += 1
                calls.extend(row for rows in recorded for row in rows if isinstance(row, Mapping))
        if isinstance(item.get("tool_trace"), list):
            tool_items += 1
            tools.extend(row for row in item["tool_trace"] if isinstance(row, Mapping))
    grouped_tools: dict[str, list[Mapping[str, Any]]] = {
        "search_memories": [],
        "inspect_photo": [],
        "other": [],
    }
    for tool in tools:
        name = tool.get("tool")
        grouped_tools[
            name if isinstance(name, str) and name in {"search_memories", "inspect_photo"} else "other"
        ].append(tool)
    progress_points: list[tuple[float, Mapping[str, Any]]] = []
    active_points: list[tuple[float, Mapping[str, Any]]] = []
    model_points: list[tuple[float, Mapping[str, Any]]] = []
    request_points: list[tuple[float, Mapping[str, Any]]] = []
    endpoints = set()
    for source in samples:
        row = _compact_sample(source)
        at = _time(row.get("observed_at"))
        if at is None:
            continue
        if row.get("run_id") is not None and row.get("run_id") != state.get("run_id"):
            continue
        p = _mapping(row.get("progress"))
        if any(value is not None for value in p.values()):
            progress_points.append((at, p))
            done, total = _number(p.get("agent_completed")), _number(p.get("agent_total"))
            if done is not None and total is not None and done < total:
                active_points.append((at, p))
        requests = _mapping(row.get("judge_requests"))
        if any(value is not None for value in requests.values()):
            request_points.append((at, requests))
        remote = _mapping(row.get("remote_model"))
        if remote:
            if remote.get("endpoint") is not None:
                endpoints.add(str(remote["endpoint"]))
            model_points.append((at, _mapping(remote.get("metrics"))))
    mixed_endpoints = len(endpoints) > 1
    if mixed_endpoints:
        model_points = []
    requests = _mapping(phase.get("judge_requests"))
    if not requests and request_points:
        requests = sorted(request_points, key=lambda point: point[0])[-1][1]
    judge_backlog = _series(progress_points, "judge_waiting")
    active_backlog = _series(active_points, "judge_waiting")
    model_wait = _series(model_points, "waiting")
    declared_count = _number(state.get("qa_count"))
    completed = _number(progress.get("completed"))
    agent_wall = _wall(phase, "agent_phase_")
    resumed = state.get("snapshot_source") in {"resumed_run", "legacy_resume_current_config"}
    return {
        "schema_version": 1,
        "coverage": {
            "declared_qa_count": declared_count,
            "items_available": isinstance(raw_items, list),
            "items_observed": len(items) if isinstance(raw_items, list) else None,
            "item_outcomes": _item_outcomes(items, isinstance(raw_items, list)),
            "items_with_model_trace": call_items,
            "items_with_tool_trace": tool_items,
            "model_samples": len(model_points),
            "progress_samples": len(progress_points),
        },
        "phases": {
            "qa_eval_wall_ms": _wall(phase),
            "agent_wall_ms": agent_wall,
            "judge_wall_ms": _wall(phase, "judge_phase_"),
            "agent_judge_envelope_overlap_ms": overlap,
            "judge_finish_lag_after_agent_ms": lag,
            "completed_qa": completed,
            "agent_completed_qa_per_second": completed * 1000 / agent_wall
            if not resumed and completed is not None and agent_wall is not None and agent_wall > 0
            else None,
            "resumed_throughput_excluded": resumed,
            "interpretation": "Phase envelopes can contain idle gaps; overlap is not busy-time utilization.",
        },
        "judge": {
            "current": {
                key: _number(progress.get(key)) for key in _PROGRESS_KEYS if key.startswith("judge_")
            },
            "requests": {key: _number(requests.get(key)) for key in _REQUEST_KEYS},
            "backlog": judge_backlog,
            "backlog_while_qa_active": active_backlog,
            "item_worker_wall_ms": _stats(_values(items, "judge_ms")),
            "interpretation": (
                "HTTP attempts are measured directly; a question may require multiple cloud requests."
            ),
        },
        "model": {
            "calls_recorded": len(calls) if call_items else None,
            "call_wall_ms": _stats(_values(calls, "duration_ms", "total_ms")),
            "call_ttft_ms": _stats(_values(calls, "ttft_ms")),
            "endpoint": {
                "scope": "endpoint-wide sampled interval; may include other clients",
                "mixed_endpoints_rejected": mixed_endpoints,
                "waiting": model_wait,
                "running": _series(model_points, "running"),
                "mean_queue_ms": _counter_mean(model_points, "queue"),
                "mean_ttft_ms": _counter_mean(model_points, "ttft"),
                "mean_request_wall_ms": _counter_mean(model_points, "e2e"),
                "preemptions": _delta(model_points, "preemptions_total"),
                "prompt_tokens": _delta(model_points, "prompt_tokens_total"),
                "output_tokens": _delta(model_points, "output_tokens_total"),
            },
        },
        "retrieval_tools": {
            "cpu_only_ms": None,
            "tool_wall_ms": {
                name: _stats(_values(rows, "duration_ms")) for name, rows in grouped_tools.items()
            },
            "interpretation": (
                "search_memories wall time can include embedding HTTP, storage and CPU. "
                "inspect_photo can include VLM. CPU-only attribution is not measured."
            ),
        },
        "agent_item_wall_ms": _stats(agent_durations),
        "diagnosis": {
            "judge_backlog_growth_while_qa_active": active_backlog["growth_over_three_intervals"],
            "model_endpoint_queue_growth": model_wait["growth_over_three_intervals"],
            "cpu_bottleneck": "unknown",
            "exclusive_bottleneck": "not_established",
            "interpretation": (
                "Worker sums overlap and may nest. Never subtract them from phase wall time "
                "or add model and tool totals."
            ),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--samples", nargs="*", type=Path, default=[])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    inputs = [args.run, *args.samples]
    if args.output and args.output.resolve() in {path.resolve() for path in inputs}:
        parser.error("output must not overwrite source evidence")
    state = json.loads(args.run.read_text())
    samples = []
    for path in args.samples:
        with path.open() as source:
            for line in source:
                if line.strip():
                    samples.append(_compact_sample(json.loads(line)))
    rendered = json.dumps(summarize_run(state, samples), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
