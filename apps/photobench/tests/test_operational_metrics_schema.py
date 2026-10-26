"""Operational evidence survives API projection without changing historical quality facts."""

import pytest

from photobench.api.metrics_schema import RunItemObservation, RunMetricsResponse


@pytest.mark.parametrize(
    "kind",
    [
        "output_limit",
        "context_limit",
        "step_limit",
        "time_limit",
        "provider_rejected",
        "resource_error",
        "model_output_invalid",
    ],
)
def test_operational_item_keeps_execution_evidence_separate_from_judge_score(kind):
    raw = {
        "qa_id": "q",
        "execution_status": "operational_failure",
        "evaluation_status": "not_evaluable",
        "judge_status": "skipped",
        "operational_issue": {"version": 1, "kind": kind, "evidence": {"observed_limit": None, "count": 30}},
        "judge": {"score": None},
    }
    assert RunItemObservation.model_validate(raw).model_dump(exclude_unset=True) == raw


def test_judge_only_operational_issue_does_not_rewrite_agent_execution_or_unknown_dispatch():
    raw = {
        "execution_status": "completed",
        "judge_status": "failed",
        "judge_dispatch_status": "outcome_unknown",
        "judge": {
            "status": "failed",
            "score": None,
            "operational_issue": {
                "version": 1,
                "kind": "output_limit",
                "scope": "judge",
                "evidence": {"finish_reason": "length"},
            },
        },
    }
    assert RunItemObservation.model_validate(raw).model_dump(exclude_unset=True) == raw
    legacy = {"execution_status": "outcome_unknown", "judge": {"score": 0}, "historical_field": {"x": True}}
    assert RunItemObservation.model_validate(legacy).model_dump(exclude_unset=True) == legacy


@pytest.mark.parametrize(
    "operational",
    [
        None,
        {"not_evaluable_count": 0, "by_kind": {}},
        {"not_evaluable_count": 2, "by_kind": {"time_limit": 1, "output_limit": 1}},
    ],
)
def test_operational_summary_phase_and_progress_preserve_missing_null_and_zero(operational):
    payload = {
        "summary": {"operational": operational, "answer_quality_mean": None},
        "phases": {
            "qa_eval": {
                "operational": operational,
                "progress": {"operational": operational, "judge_completed": 0},
            }
        },
    }
    assert RunMetricsResponse.model_validate(payload).model_dump(exclude_unset=True) == payload
    legacy = {"summary": {"answer_quality_mean": 0}, "phases": {"qa_eval": {"progress": {"total": 7}}}}
    assert RunMetricsResponse.model_validate(legacy).model_dump(exclude_unset=True) == legacy


def test_judge_incomplete_counts_and_item_issue_are_distinct_from_agent_not_evaluable():
    issue = {"version": 1, "kind": "output_limit", "scope": "judge", "evidence": {"finish_reason": "length"}}
    item = {
        "execution_status": "completed",
        "judge_status": "failed",
        "judge_issue": issue,
        "judge": {"score": None, "status": "failed", "excluded_observation": {"raw_response": "partial"}},
    }
    assert RunItemObservation.model_validate(item).model_dump(exclude_unset=True) == item
    counts = {
        "not_evaluable_count": 0,
        "by_kind": {},
        "judge_incomplete_count": 1,
        "judge_by_kind": {"output_limit": 1},
    }
    response = {
        "summary": {"operational": counts},
        "phases": {"qa_eval": {"operational": counts, "progress": {"operational": counts}}},
    }
    assert RunMetricsResponse.model_validate(response).model_dump(exclude_unset=True) == response


def test_invalid_model_output_keeps_own_kind_and_null_score_in_public_contract():
    issue = {
        "version": 1,
        "kind": "model_output_invalid",
        "evidence": {"error_code": "model_output_invalid", "http_status": 200},
    }
    item = {
        "execution_status": "operational_failure",
        "evaluation_status": "not_evaluable",
        "judge_status": "skipped",
        "operational_issue": issue,
        "judge": {"score": None},
    }
    projected = RunItemObservation.model_validate(item).model_dump(exclude_unset=True)
    assert projected == item
    counts = {"not_evaluable_count": 1, "by_kind": {"model_output_invalid": 1}}
    response = {"summary": {"operational": counts}}
    assert RunMetricsResponse.model_validate(response).model_dump(exclude_unset=True) == response
    schema = RunItemObservation.model_json_schema()
    assert "model_output_invalid" in schema["$defs"]["OperationalIssue"]["properties"]["kind"]["enum"]
