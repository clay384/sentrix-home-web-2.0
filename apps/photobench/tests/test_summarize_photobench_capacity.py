from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.benchmarks.summarize_photobench_capacity import summarize_run


def checkpoint() -> dict:
    return {
        "run_id": "private-run",
        "qa_count": 4,
        "phases": {
            "qa_eval": {
                "started_at": "2026-10-26T10:00:00+00:00",
                "finished_at": "2026-10-26T10:00:12+00:00",
                "agent_phase_started_at": "2026-10-26T10:00:00+00:00",
                "agent_phase_finished_at": "2026-10-26T10:00:10+00:00",
                "judge_phase_started_at": "2026-10-26T10:00:04+00:00",
                "judge_phase_finished_at": "2026-10-26T10:00:12+00:00",
                "agent_phase_wall_ms": 10000,
                "judge_phase_wall_ms": 8000,
                "progress": {"completed": 4, "judge_waiting": 0, "judge_running": 0},
                "judge_requests": {"requests_started": 17, "http_429": 2},
            }
        },
    }


def test_overlap_is_phase_envelope_not_subtracted_worker_time():
    state = checkpoint()
    state["items"] = [
        {
            "timing_breakdown": {"agent_wall_ms": 8500},
            "wall_clock_ms": 90000,
            "judge_ms": 80000,
            "model_call_metrics": [{"duration_ms": 60000, "ttft_ms": 200}],
            "runtime_turns": [{"trace_bundle": {"model_calls": [{"duration_ms": 60000}]}}],
            "tool_trace": [
                {"tool": "search_memories", "duration_ms": 40000},
                {"tool": "inspect_photo", "duration_ms": 10000},
            ],
        }
    ]
    report = summarize_run(state)
    assert report["phases"]["agent_judge_envelope_overlap_ms"] == 6000
    assert report["phases"]["judge_finish_lag_after_agent_ms"] == 2000
    assert report["phases"]["qa_eval_wall_ms"] == 12000
    assert report["phases"]["agent_completed_qa_per_second"] == 0.4
    assert report["model"]["calls_recorded"] == 1
    assert report["model"]["call_wall_ms"]["sum_ms"] == 60000
    assert report["retrieval_tools"]["cpu_only_ms"] is None
    assert report["agent_item_wall_ms"]["sum_ms"] == 8500
    assert report["judge"]["requests"]["requests_started"] == 17  # Not four questions.


def test_missing_historical_facts_stay_unknown_and_zero_is_preserved():
    report = summarize_run({"qa_count": 1167, "items": [{"judge_ms": 0}]})
    assert report["phases"]["agent_wall_ms"] is None
    assert report["judge"]["requests"]["requests_started"] is None
    assert report["model"]["calls_recorded"] is None
    assert report["model"]["call_wall_ms"]["sum_ms"] is None
    assert report["judge"]["item_worker_wall_ms"]["sum_ms"] == 0
    assert report["diagnosis"]["judge_backlog_growth_while_qa_active"] is None


def test_growing_judge_backlog_before_qa_finishes_survives_final_drain():
    state = checkpoint()
    samples = [
        {
            "observed_at": i + 1,
            "run_id": "private-run",
            "progress": {"agent_completed": i, "agent_total": 4, "judge_waiting": i},
        }
        for i in range(4)
    ]
    samples.append(
        {"observed_at": 5, "progress": {"agent_completed": 4, "agent_total": 4, "judge_waiting": 0}}
    )
    report = summarize_run(state, samples)
    assert report["judge"]["current"]["judge_waiting"] == 0
    assert report["judge"]["backlog_while_qa_active"]["peak"] == 3
    assert report["diagnosis"]["judge_backlog_growth_while_qa_active"] is True
    assert report["diagnosis"]["exclusive_bottleneck"] == "not_established"


def test_endpoint_counter_deltas_are_paired_and_not_single_run_attribution():
    samples = [
        {"observed_at": at, "remote_model": {"endpoint": "http://private-host:18104", "metrics": data}}
        for at, data in [
            (1, {"queue_sum_s": 10, "queue_count": 5, "ttft_sum_s": 20, "ttft_count": 5}),
            (2, {"queue_sum_s": 16, "queue_count": 7, "ttft_sum_s": 30, "ttft_count": 7}),
        ]
    ]
    report = summarize_run(checkpoint(), samples)
    assert report["model"]["endpoint"]["mean_queue_ms"] == 3000
    assert report["model"]["endpoint"]["mean_ttft_ms"] == 5000
    assert "other clients" in report["model"]["endpoint"]["scope"]
    samples[0]["remote_model"]["metrics"].pop("queue_count")
    assert summarize_run(checkpoint(), samples)["model"]["endpoint"]["mean_queue_ms"] is None
    samples[1]["remote_model"]["metrics"]["ttft_count"] = 1
    assert summarize_run(checkpoint(), samples)["model"]["endpoint"]["mean_ttft_ms"] is None


def test_mixed_endpoints_and_foreign_runs_are_not_combined():
    samples = [
        {"observed_at": 1, "remote_model": {"endpoint": "a", "metrics": {"waiting": 1}}},
        {"observed_at": 2, "remote_model": {"endpoint": "b", "metrics": {"waiting": 100}}},
        {"observed_at": 2, "run_id": "foreign", "progress": {"judge_waiting": 999}},
    ]
    report = summarize_run(checkpoint(), samples)
    assert report["model"]["endpoint"]["mixed_endpoints_rejected"] is True
    assert report["model"]["endpoint"]["waiting"]["peak"] is None
    assert report["judge"]["backlog"]["peak"] is None


def test_report_does_not_emit_prose_names_paths_identifiers_or_nonfinite_values():
    secret = "PRIVATE-person-/home/private/path-qa-answer"
    state = checkpoint()
    state.update(run_id=secret, fatal_error=secret, album_name=secret, model_base_url=secret)
    state["items"] = [
        {
            "question": secret,
            "answer": secret,
            "qa_id": secret,
            "model_call_metrics": [{"duration_ms": float("nan"), "ttft_ms": True, "endpoint": secret}],
            "tool_trace": [{"tool": secret, "arguments": {"name": secret}, "duration_ms": -1}],
        }
    ]
    report = summarize_run(
        state,
        [
            {
                "observed_at": 1,
                "remote_model": {"endpoint": secret, "metrics": {"waiting": 0}},
                "raw_prometheus": secret,
            }
        ],
    )
    rendered = json.dumps(report, allow_nan=False)
    assert secret not in rendered
    assert report["model"]["call_wall_ms"]["sum_ms"] is None
    assert report["retrieval_tools"]["tool_wall_ms"]["other"]["sum_ms"] is None


def test_duplicate_or_same_timestamp_samples_cannot_prove_persistent_growth():
    samples = [
        {"observed_at": 1, "progress": {"judge_waiting": n, "agent_completed": 0, "agent_total": 4}}
        for n in range(4)
    ]
    report = summarize_run(checkpoint(), samples)
    assert report["diagnosis"]["judge_backlog_growth_while_qa_active"] is None


def test_resumed_completed_item_counts_cannot_inflate_throughput():
    state = checkpoint()
    state["snapshot_source"] = "resumed_run"
    assert summarize_run(state)["phases"]["agent_completed_qa_per_second"] is None


def test_cli_writes_sanitized_report_and_refuses_to_overwrite_evidence(tmp_path):
    path = tmp_path / "run.json"
    path.write_text(json.dumps(checkpoint()))
    output = tmp_path / "report.json"
    script = Path(__file__).resolve().parents[3] / "scripts/benchmarks/summarize_photobench_capacity.py"
    command = [sys.executable, str(script), "--run", str(path)]
    subprocess.run([*command, "--output", str(output)], check=True)
    assert json.loads(output.read_text())["phases"]["agent_wall_ms"] == 10000
    assert "private-run" not in output.read_text()
    result = subprocess.run([*command, "--output", str(path)], capture_output=True)
    assert result.returncode != 0
    assert json.loads(path.read_text())["run_id"] == "private-run"


def test_naive_or_reversed_intervals_do_not_make_overlap():
    state = checkpoint()
    state["phases"]["qa_eval"]["agent_phase_started_at"] = "2026-10-26T10:01:00"
    assert summarize_run(state)["phases"]["agent_judge_envelope_overlap_ms"] is None
    state["phases"]["qa_eval"]["agent_phase_started_at"] = "2026-10-26T10:01:00+00:00"
    assert summarize_run(state)["phases"]["agent_judge_envelope_overlap_ms"] is None


def test_item_outcomes_do_not_equate_terminal_judges_with_valid_scores():
    report = summarize_run(
        {
            "items": [
                {"execution_status": "completed", "judge_status": "completed", "judge": {"score": 0}},
                {"execution_status": "completed", "judge_status": "completed", "judge": {"score": None}},
                {"execution_status": "completed", "judge_status": "failed", "judge": {"score": 1}},
                {"execution_status": "failed", "judge_status": "skipped"},
                {"execution_status": "timeout", "judge_status": "skipped"},
                {"execution_status": "outcome_unknown", "judge_status": "outcome_unknown"},
                {"execution_status": "scheduled", "judge_status": "pending"},
                {"execution_status": "running", "judge_status": "running", "judge": {"score": True}},
                {},
            ]
        }
    )
    counts = report["coverage"]["item_outcomes"]
    assert counts["agent"]["execution_completed"] == 3
    assert counts["agent"]["failed"] == 1
    assert counts["agent"]["timeout"] == 1
    assert counts["agent"]["outcome_unknown"] == 1
    assert counts["agent"]["status_not_recorded_or_unrecognized"] == 1
    assert counts["judge"]["completed"] == 2
    assert counts["judge"]["valid_answer_scores"] == 2
    assert counts["judge"]["completed_without_valid_answer_score"] == 1
    assert counts["judge"]["skipped"] == 2
    assert summarize_run({})["coverage"]["item_outcomes"]["agent"]["execution_completed"] is None
