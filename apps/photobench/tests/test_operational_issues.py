"""Operational evidence is neither an answer-quality zero nor permission to replay."""

from copy import deepcopy

import pytest

from photobench.domain.runs.analysis import (
    capability_summary,
    derive_attribution,
    effective_summary,
    hydrate_qa_metadata,
    item_summary,
    list_summary,
)
from photobench.domain.runs.operational import exclude_operational_quality, operational_issue


@pytest.mark.parametrize(
    ("record", "kind"),
    [
        ({"finish_reason": "length"}, "output_limit"),
        ({"error_code": "context_limit", "http_status": 400}, "context_limit"),
        ({"termination_reason": "step_budget_exhausted"}, "step_limit"),
        ({"error_code": "time_limit"}, "time_limit"),
        ({"http_status": 429}, "provider_rejected"),
        ({"error_code": "provider_unavailable"}, "provider_rejected"),
        ({"error_type": "OutOfMemoryError"}, "resource_error"),
    ],
)
def test_only_structured_evidence_excludes_quality_without_destroying_observations(record, kind):
    call = {
        **record,
        "request_policy": {
            "max_model_len": 262144,
            "max_output_tokens": None,
            "output_budget": "native_remaining_context",
        },
    }
    original = {
        "execution_status": "completed",
        "judge_status": "completed",
        "judge": {"score": 0},
        "runtime_turns": [{"trace_bundle": {"model_calls": [call]}, "judge": {"score": 0}}],
    }
    before = deepcopy(original)
    result = exclude_operational_quality(original)
    assert result["operational_issue"]["kind"] == kind
    assert result["operational_issue"]["evidence"]["request_policy"] == call["request_policy"]
    assert result["judge"]["score"] is None
    assert result["judge"]["excluded_observation"]["score"] == 0
    assert result["evaluation_status"] == "not_evaluable"
    assert result["execution_status"] == "operational_failure"
    assert result["judge_status"] == "skipped"
    assert result["runtime_turns"] == before["runtime_turns"]
    assert original == before
    assert exclude_operational_quality(result) == result


def test_wrong_answers_refusals_and_invalid_tool_arguments_do_not_invent_operational_failure():
    for answer in ("context limit / out of memory", "无法回答", "错误答案"):
        item = {
            "answer": answer,
            "error": "provider rejected text",
            "termination_reason": "answered",
            "runtime_turns": [
                {
                    "trace_bundle": {
                        "model_calls": [
                            {
                                "error_type": "InvalidToolArguments",
                                "error": "timeout",
                                "finish_reason": "tool_calls",
                            }
                        ]
                    }
                }
            ],
            "judge": {"score": 0, "reason": "内容错误"},
            "execution_status": "completed",
        }
        assert operational_issue(item) is None
        assert exclude_operational_quality(item) == item
    assert operational_issue({"operational_issue": {"kind": {}, "evidence": {}}}) is None


def test_unknown_model_timeout_remains_unknown():
    result = exclude_operational_quality(
        {
            "execution_status": "completed",
            "judge_status": "pending",
            "runtime_turns": [
                {
                    "trace_bundle": {
                        "model_calls": [
                            {"error_code": "time_limit", "request_policy": {"request_outcome_unknown": True}}
                        ]
                    }
                }
            ],
        }
    )
    assert result["execution_status"] == "outcome_unknown"
    assert result["operational_issue"]["kind"] == "time_limit"
    assert result["judge_status"] == "skipped"


def test_historical_budget_zero_is_not_reintroduced_by_saved_summary_or_turn_scores():
    bad = {
        "qa_id": "budget",
        "termination_reason": "step_budget_exhausted",
        "execution_status": "completed",
        "judge_status": "completed",
        "judge": {"score": 0, "reason": "内容错误"},
        "evidence_judge": {"score": 0},
        "task_judges": [{"expected_action": "answer", "correct": False}],
        "attribution": {"primary": "G", "layers": {"G": "fail"}},
        "retrieval_image_ids": ["one"],
        "retrieval_recall": 0,
        "retrieval_precision": 0,
        "agent_stability": {"completed_within_steps": False},
        "wall_clock_ms": 100,
        "runtime_turns": [{"judge": {"score": 0}}],
    }
    good = {
        "qa_id": "good",
        "execution_status": "completed",
        "judge_status": "completed",
        "judge": {"score": 2},
        "wall_clock_ms": 300,
    }
    state = {
        "items": [bad, good],
        "qa_count": 2,
        "summary": {"answer_quality_mean": 1, "judge_valid_count": 2},
    }
    before = deepcopy(state)
    for summary in (effective_summary(state), list_summary(state), capability_summary([bad, good])):
        assert summary["answer_quality_mean"] == 2
        assert summary["judge_valid_count"] == 1
        assert summary["operational"]["not_evaluable_count"] == 1
        assert summary["operational"]["by_kind"]["step_limit"] == 1
    assert effective_summary(state)["attribution"]["layer_failures"] == {}
    assert effective_summary(state)["attribution"]["primary"]["OPERATIONAL"] == 1
    assert derive_attribution(bad)["primary"] == "OPERATIONAL"
    assert item_summary(bad, 0)["judge"]["score"] is None
    assert hydrate_qa_metadata(bad, {})["judge"]["score"] is None
    assert state == before


@pytest.mark.parametrize(
    "kind", ["output_limit", "context_limit", "time_limit", "provider_rejected", "resource_error"]
)
def test_typed_inspection_failure_is_not_hidden_by_final_text_fallback(kind):
    item = {
        "termination_reason": "answered",
        "answer": "fallback",
        "execution_status": "completed",
        "runtime_turns": [
            {
                "termination_reason": "answered",
                "trace_bundle": {
                    "tool_calls": [
                        {
                            "tool": "inspect_photo",
                            "status": "error",
                            "failure_reason": kind,
                            "observation": {
                                "operational_issue": {
                                    "kind": kind,
                                    "http_status": 400 if kind == "provider_rejected" else None,
                                    "request_policy": {"max_model_len": 262144},
                                }
                            },
                        }
                    ]
                },
            }
        ],
    }
    result = exclude_operational_quality(item)
    assert result["operational_issue"]["kind"] == kind
    assert result["operational_issue"]["evidence"]["source"] == "tool_call"
    assert result["judge"]["score"] is None
    assert result["runtime_turns"] == item["runtime_turns"]


def test_tool_argument_error_and_plain_tool_error_text_are_not_operational_evidence():
    item = {
        "runtime_turns": [
            {
                "trace_bundle": {
                    "tool_calls": [
                        {
                            "tool": "inspect_photo",
                            "status": "error",
                            "failure_reason": "bad_arguments",
                            "observation": {
                                "error": "output_limit",
                                "arguments": {"operational_issue": {"kind": "context_limit"}},
                            },
                        }
                    ]
                }
            }
        ]
    }
    assert operational_issue(item) is None


@pytest.mark.parametrize(
    "finish_reason,kind", [("length", "output_limit"), ("content_filter", "provider_rejected")]
)
def test_historical_cloud_incomplete_with_valid_zero_is_not_replayed(finish_reason, kind):
    from photobench.application.run_execution.service import RunExecution
    from photobench.domain.runs.analysis import judge_projection

    raw = {"choices": [{"finish_reason": finish_reason, "message": {"content": '{"score":0}'}}]}
    original = {
        "execution_status": "completed",
        "judge_status": "completed",
        "judge": {"score": 0, "judge_status": "completed", "attempts": [{"raw_response": raw}]},
    }
    result = RunExecution.resume_item(original)
    assert result["execution_status"] == "completed"
    assert result["judge_status"] == "failed"
    assert result["judge"]["score"] is None
    assert result["judge_issue"]["scope"] == "judge"
    assert result["judge_issue"]["kind"] == kind
    assert result["judge"]["reason"] == "judge_" + kind
    assert original["judge"]["score"] == 0
    assert judge_projection(original["judge"])["score"] is None
    summary = effective_summary({"items": [original]})
    assert summary["judge_valid_count"] == 0
    assert summary["operational"]["not_evaluable_count"] == 0
    assert summary["operational"]["judge_incomplete_count"] == 1
    assert exclude_operational_quality(result) == result


def test_explicit_rejudge_keeps_valid_zero_and_failed_judge_targets_but_excludes_agent_budget():
    from photobench.application.runs.suites import RunSuites

    base = {"question": "q", "reference_answer": "r", "answer": "a"}
    items = [
        {**base, "judge": {"score": 0}},
        {**base, "judge_status": "failed", "judge_issue": {"kind": "output_limit", "scope": "judge"}},
        {**base, "termination_reason": "step_budget_exhausted", "judge": {"score": 0}},
    ]
    assert RunSuites._rejudge_targets(items) == [(0, None), (1, None)]


@pytest.mark.parametrize(
    "record,kind,status",
    [
        ({"error_code": "time_limit", "request_outcome_unknown": True}, "time_limit", "outcome_unknown"),
        ({"error_code": "provider_rejected", "http_status": 429}, "provider_rejected", "operational_failure"),
        (
            {"error_code": "provider_unavailable", "failure_code": "circuit_open"},
            "provider_rejected",
            "operational_failure",
        ),
        (
            {"error_code": "resource_error", "failure_code": "empty_vector"},
            "resource_error",
            "operational_failure",
        ),
    ],
)
def test_retrieval_faults_are_not_quality_zero_and_unknown_is_not_replayable(record, kind, status):
    item = {
        "execution_status": "completed",
        "judge": {"score": 0},
        "judge_status": "completed",
        "runtime_turns": [
            {
                "trace_bundle": {
                    "tool_calls": [
                        {
                            "tool": "search_memories",
                            "observation": {
                                "retrieval_channels": [
                                    {
                                        "channel": "text_ann",
                                        "status": "degraded",
                                        "reason": "embedder_unavailable",
                                        "operational_issue": record,
                                    }
                                ]
                            },
                        }
                    ]
                }
            }
        ],
    }
    result = exclude_operational_quality(item)
    assert result["operational_issue"]["kind"] == kind
    assert result["operational_issue"]["evidence"]["source"] == "retrieval_channel"
    assert result["operational_issue"]["evidence"]["channel"] == "text_ann"
    assert result["execution_status"] == status
    assert result["judge"]["score"] is None and result["judge_status"] == "skipped"
    assert result["runtime_turns"] == item["runtime_turns"]
    assert exclude_operational_quality(result) == result


@pytest.mark.parametrize(
    "status,reason", [("ok", ""), ("degraded", "no_index_configured"), ("degraded", "embedder_unavailable")]
)
def test_no_hits_or_legacy_untyped_degradation_does_not_invent_fault_cause(status, reason):
    item = {
        "runtime_turns": [
            {
                "trace_bundle": {
                    "tool_calls": [
                        {
                            "observation": {
                                "retrieval_channels": [
                                    {
                                        "channel": "visual_ann",
                                        "status": status,
                                        "candidate_count": 0,
                                        "reason": reason,
                                    }
                                ]
                            }
                        }
                    ]
                }
            }
        ],
        "judge": {"score": 0},
    }
    assert operational_issue(item) is None
    assert exclude_operational_quality(item) == item


@pytest.mark.parametrize(
    "code", ["tool_budget_exhausted", "search_call_limit", "page_call_limit", "inspection_budget_exhausted"]
)
def test_historical_tool_budget_denial_excludes_later_final_answer(code):
    item = {
        "execution_status": "completed",
        "termination_reason": "answered_with_gap",
        "judge": {"score": 0},
        "runtime_turns": [
            {
                "trace_bundle": {
                    "tool_calls": [
                        {
                            "tool": "inspect_photo",
                            "status": "denied",
                            "failure_reason": code,
                            "observation": {},
                        }
                    ]
                }
            }
        ],
    }
    result = exclude_operational_quality(item)
    assert result["operational_issue"]["kind"] == "step_limit"
    assert result["operational_issue"]["evidence"]["failure_code"] == code
    assert result["judge"]["score"] is None
    assert result["evaluation_status"] == "not_evaluable"


@pytest.mark.parametrize("code,kind", [("tool_budget_exhausted", "step_limit"), ("time_limit", "time_limit")])
def test_budget_gate_event_excludes_answer_without_a_denied_tool(code, kind):
    item = {
        "execution_status": "completed",
        "termination_reason": "answered",
        "judge": {"score": 2},
        "runtime_turns": [
            {
                "trace_bundle": {
                    "budget_events": [
                        {"error_code": code, "budget_name": "fixture", "used": 30, "limit": 28}
                    ],
                    "budget": {"max_steps": 30, "max_tool_calls": None},
                }
            }
        ],
    }
    result = exclude_operational_quality(item)
    assert result["operational_issue"]["kind"] == kind
    assert result["operational_issue"]["evidence"]["budget_name"] == "fixture"
    assert result["operational_issue"]["evidence"]["used"] == 30
    assert result["judge"]["score"] is None


def test_invalid_provider_stream_keeps_unknown_and_is_not_graded():
    item = {
        "execution_status": "completed",
        "judge_status": "pending",
        "runtime_turns": [
            {
                "trace_bundle": {
                    "tool_calls": [
                        {
                            "tool": "inspect_photo",
                            "status": "error",
                            "observation": {
                                "operational_issue": {
                                    "error_code": "provider_response_invalid",
                                    "request_policy": {"request_outcome_unknown": True},
                                }
                            },
                        }
                    ]
                }
            }
        ],
    }
    result = exclude_operational_quality(item)
    assert result["operational_issue"]["kind"] == "provider_rejected"
    assert result["operational_issue"]["evidence"]["error_code"] == "provider_response_invalid"
    assert result["execution_status"] == "outcome_unknown"
    assert result["judge_status"] == "skipped"
    assert result["judge"]["score"] is None
