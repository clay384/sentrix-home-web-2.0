"""Separate evidenced operational limits from answer quality; never infer from prose."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

KINDS = frozenset(
    {"output_limit", "context_limit", "step_limit", "time_limit", "provider_rejected", "resource_error"}
)
_CODES = {
    "output_limit": "output_limit",
    "output_budget_exhausted": "output_limit",
    "context_limit": "context_limit",
    "context_length_exceeded": "context_limit",
    "context_window_exceeded": "context_limit",
    "prompt_too_long": "context_limit",
    "step_limit": "step_limit",
    "step_budget_limit": "step_limit",
    "step_budget_exhausted": "step_limit",
    "tool_budget_exhausted": "step_limit",
    "time_limit": "time_limit",
    "timeout": "time_limit",
    "request_timeout": "time_limit",
    "provider_rejected": "provider_rejected",
    "provider_unavailable": "provider_rejected",
    "resource_error": "resource_error",
    "out_of_memory": "resource_error",
    "cuda_error": "resource_error",
    "database_locked": "resource_error",
    "resource_exhausted": "resource_error",
}
_TYPES = {
    "PromptTooLongError": "context_limit",
    "TimeoutError": "time_limit",
    "ReadTimeout": "time_limit",
    "ConnectTimeout": "time_limit",
    "WriteTimeout": "time_limit",
    "OutOfMemoryError": "resource_error",
}
_POLICY_KEYS = frozenset(
    {
        "max_model_len",
        "max_output_tokens",
        "max_tokens",
        "max_steps",
        "wall_time_s",
        "context_window",
        "context_window_tokens",
        "output_budget_tokens",
        "agent_wall_time_s",
        "output_budget",
        "request_timeout_s",
        "request_outcome_unknown",
        "max_tool_calls",
        "max_search_calls",
        "max_page_calls",
        "max_inspections",
        "model_steps",
    }
)


def _issue(
    record: Mapping[str, Any], source: str, turn: int | None, call: int | None
) -> dict[str, Any] | None:
    field: str | None = None
    value: str | int | None = None
    kind: str | None = None
    if record.get("finish_reason") == "length":
        field, value, kind = "finish_reason", "length", "output_limit"
    for key in ("operational_code", "error_code", "termination_reason"):
        code = record.get(key)
        if kind is None and isinstance(code, str) and code in _CODES:
            field, value, kind = key, code, _CODES[code]
    error_type = record.get("error_type")
    if kind is None and isinstance(error_type, str) and error_type in _TYPES:
        field, value, kind = "error_type", error_type, _TYPES[error_type]
    status = record.get("http_status")
    if kind is None and type(status) is int and 400 <= status <= 599:
        field, value, kind = "http_status", status, "provider_rejected"
    if kind is None:
        return None
    evidence: dict[str, Any] = {"source": source, "field": field, "value": value}
    if turn is not None:
        evidence["turn_index"] = turn
    if call is not None:
        evidence["call_index"] = call
    for key in (
        "http_status",
        "error_code",
        "error_type",
        "finish_reason",
        "prompt_tokens",
        "completion_tokens",
        "request_outcome_unknown",
    ):
        if isinstance(record.get(key), (str, int)):
            evidence[key] = record[key]
    policy = record.get("request_policy")
    if isinstance(policy, Mapping):
        evidence["request_policy"] = {
            key: value
            for key, value in policy.items()
            if key in _POLICY_KEYS and isinstance(value, (str, int, float, bool, type(None)))
        }
    bundle = record.get("trace_bundle")
    budget = bundle.get("budget") if isinstance(bundle, Mapping) else None
    if isinstance(budget, Mapping):
        evidence["budget"] = {
            key: value
            for key, value in budget.items()
            if key in _POLICY_KEYS and isinstance(value, (int, float))
        }
    return {"version": 1, "kind": kind, "evidence": evidence}


def operational_issue(item: Mapping[str, Any]) -> dict[str, Any] | None:
    existing = item.get("operational_issue")
    if (
        isinstance(existing, dict)
        and isinstance(existing.get("kind"), str)
        and existing["kind"] in KINDS
        and isinstance(existing.get("evidence"), dict)
    ):
        return dict(existing)
    direct = _issue(item, "item", None, None)
    if direct:
        return direct
    for index, turn in enumerate(item.get("runtime_turns") or []):
        if not isinstance(turn, Mapping):
            continue
        issue = _issue(turn, "runtime_turn", index, None)
        if issue:
            return issue
        bundle = turn.get("trace_bundle")
        tool_calls = bundle.get("tool_calls") if isinstance(bundle, Mapping) else []
        for tool_index, tool in enumerate(tool_calls or []):
            observation = tool.get("observation") if isinstance(tool, Mapping) else None
            explicit = observation.get("operational_issue") if isinstance(observation, Mapping) else None
            if isinstance(explicit, Mapping):
                issue = _issue(
                    {**explicit, "operational_code": explicit.get("kind")}, "tool_call", index, tool_index
                )
                if issue:
                    return issue
        calls = bundle.get("model_calls") if isinstance(bundle, Mapping) else []
        for call_index, record in enumerate(calls or []):
            if isinstance(record, Mapping) and (issue := _issue(record, "model_call", index, call_index)):
                return issue
    for index, record in enumerate(item.get("model_call_metrics") or []):
        if isinstance(record, Mapping) and (issue := _issue(record, "model_call", None, index)):
            return issue
    return None


def incomplete_judge_issue(judge: Mapping[str, Any]) -> dict[str, Any] | None:
    existing = judge.get("operational_issue")
    if isinstance(existing, dict) and existing.get("scope") == "judge":
        return dict(existing)
    # Only the final received response: an earlier known failure can have been explicitly retried.
    responses = [
        attempt.get("raw_response")
        for attempt in judge.get("attempts") or []
        if isinstance(attempt, Mapping) and isinstance(attempt.get("raw_response"), Mapping)
    ]
    raw = judge.get("raw_response") or (responses[-1] if responses else {})
    choices = raw.get("choices") if isinstance(raw, Mapping) else None
    if (
        isinstance(choices, list)
        and choices
        and isinstance(choices[0], Mapping)
        and choices[0].get("finish_reason") == "length"
    ):
        return {
            "version": 1,
            "scope": "judge",
            "kind": "output_limit",
            "evidence": {"source": "judge_response", "field": "finish_reason", "value": "length"},
        }
    return None


def item_judge_issue(item: Mapping[str, Any]) -> dict[str, Any] | None:
    existing = item.get("judge_issue")
    if isinstance(existing, dict):
        return dict(existing)
    records = [item, *(turn for turn in item.get("runtime_turns") or [] if isinstance(turn, Mapping))]
    for record in records:
        for key in ("judge", "task_judge", "evidence_judge"):
            observation = record.get(key)
            if isinstance(observation, Mapping) and (issue := incomplete_judge_issue(observation)):
                return issue
    return None


def exclude_operational_quality(item: Mapping[str, Any]) -> dict[str, Any]:
    """Keep raw turns/traces and prior scores for audit, without judging/replaying."""
    result = dict(item)
    if judge_issue := item_judge_issue(item):
        result["judge_issue"] = judge_issue
        if result.get("judge_status") != "outcome_unknown":
            result["judge_status"] = "failed"
        previous = result.get("judge")
        if not (isinstance(previous, dict) and previous.get("operational_issue")):
            result["judge"] = {
                "score": None,
                "reason": "judge_output_limit",
                "judge_status": "failed",
                "operational_issue": judge_issue,
                "excluded_observation": previous,
            }
    issue = operational_issue(item)
    if issue is None:
        return result
    result.update(operational_issue=issue, evaluation_status="not_evaluable")
    calls = [
        call
        for turn in item.get("runtime_turns") or []
        if isinstance(turn, Mapping)
        for call in (turn.get("trace_bundle") or {}).get("model_calls") or []
        if isinstance(call, Mapping)
    ]
    calls.extend(call for call in item.get("model_call_metrics") or [] if isinstance(call, Mapping))
    for turn in item.get("runtime_turns") or []:
        if not isinstance(turn, Mapping):
            continue
        for tool in (turn.get("trace_bundle") or {}).get("tool_calls") or []:
            observation = tool.get("observation") if isinstance(tool, Mapping) else None
            explicit = observation.get("operational_issue") if isinstance(observation, Mapping) else None
            if isinstance(explicit, Mapping):
                calls.append(explicit)
    if any(
        call.get("request_outcome_unknown") is True
        or (
            isinstance(call.get("request_policy"), Mapping)
            and call["request_policy"].get("request_outcome_unknown") is True
        )
        for call in calls
    ):
        result["execution_status"] = "outcome_unknown"
    elif result.get("execution_status") != "outcome_unknown":
        result["execution_status"] = "operational_failure"
    if result.get("judge_status") not in {"running", "outcome_unknown"}:
        result["judge_status"] = "skipped"
    result["attribution"] = {
        "primary": "OPERATIONAL",
        "layers": {},
        "detail": {"operational_issue": issue["kind"]},
        "source": "structured_operational_evidence",
    }
    if result.get("task_judges"):
        result["excluded_task_judges"] = result["task_judges"]
    result["task_judges"] = []
    if isinstance(result.get("agent_stability"), dict):
        result["agent_stability"] = {**result["agent_stability"], "completed_within_steps": None}
    for key in ("judge", "evidence_judge", "task_judge"):
        previous = result.get(key)
        result[key] = {
            "score": None,
            "correct": None,
            "reason": "operational_issue:" + str(issue["kind"]),
            "operational_issue": issue,
            "judge_status": "skipped",
        }
        if isinstance(previous, dict) and previous.get("operational_issue"):
            if previous.get("excluded_observation"):
                result[key]["excluded_observation"] = previous["excluded_observation"]
        elif previous:
            result[key]["excluded_observation"] = previous
    return result


def operational_counts(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    kinds = [issue["kind"] for item in items if (issue := operational_issue(item))]
    judge_kinds = [(item_judge_issue(item) or {}).get("kind") for item in items]
    return {
        "not_evaluable_count": len(kinds),
        "by_kind": {kind: kinds.count(kind) for kind in sorted(KINDS)},
        "judge_incomplete_count": sum(kind in KINDS for kind in judge_kinds if isinstance(kind, str)),
        "judge_by_kind": {kind: judge_kinds.count(kind) for kind in sorted(KINDS)},
    }
