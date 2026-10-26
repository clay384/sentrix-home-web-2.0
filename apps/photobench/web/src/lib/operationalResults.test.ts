import { describe, expect, it } from "vitest";
import type { Run } from "../api/types.ts";
import { judgeScoreLabel } from "./format.ts";
import { effectiveRunSummary } from "./runSummary.ts";
import { keyMetricRows } from "./telemetry.ts";

describe("operational results remain separate from answer quality", () => {
  it("excludes retained raw scores and retrieval failures while retaining latency observations", () => {
    const run = { items: [
      { operational_issue: { kind: "output_limit" }, evaluation_status: "not_evaluable", judge: { score: 0 },
        evidence_judge: { score: 0 }, task_judge: { correct: false },
        agent_stability: { completed_within_steps: false }, metrics_contract_version: 2, wall_clock_ms: 100,
        retrieval_media_refs: [{ media_type: "image", media_id: "a" }], media_retrieval_counts: { gt: 1, predicted: 0, matched: 0 } },
      { judge: { score: 2 }, evidence_judge: { score: 2 }, task_judge: { correct: true },
        agent_stability: { completed_within_steps: true }, metrics_contract_version: 2, wall_clock_ms: 300,
        retrieval_media_refs: [{ media_type: "image", media_id: "a" }], media_retrieval_counts: { gt: 1, predicted: 1, matched: 1 } },
    ] } as unknown as Run;
    const summary = effectiveRunSummary(run);
    expect(summary.answer_quality_mean).toBe(2);
    expect(summary.evidence_mean).toBe(2);
    expect(summary.task_decision_accuracy).toBe(1);
    expect(summary.qa_completion_within_steps_rate).toBe(1);
    expect(summary.retrieval_recall_macro).toBe(1);
    expect(summary.agent_task_latency_mean_ms).toBe(200);
  });
  it("preserves explicit null in the operational contract despite retained legacy fields", () => {
    const summary = effectiveRunSummary({
      summary: { operational: { not_evaluable_count: 1, by_kind: { step_limit: 1 } },
        retrieval_recall_macro: null, answer_quality_mean: null, qa_completion_within_steps_rate: null },
      items: [{ judge: { score: 0 }, retrieval_recall: 0, agent_stability: { completed_within_steps: false } }],
    } as unknown as Run);
    expect(summary.retrieval_recall_macro).toBeNull();
    expect(summary.answer_quality_mean).toBeNull();
    expect(summary.qa_completion_within_steps_rate).toBeNull();
  });
  it("does not count a truncated Judge zero and still counts ordinary zero", () => {
    const summary = effectiveRunSummary({ items: [
      { judge_issue: { kind: "output_limit" }, judge: { score: 0 } },
      { judge: { score: 0 } },
    ] } as unknown as Run);
    expect(summary.judge_valid_count).toBe(1);
    expect(summary.answer_quality_mean).toBe(0);
  });
  it("shows the operational reason and separates Judge coverage", () => {
    expect(judgeScoreLabel({ operational_issue: { kind: "context_limit" }, judge: { score: 0 } }, null)).toBe("不可评估（上下文预算）");
    expect(judgeScoreLabel({ judge_issue: { kind: "output_limit" } }, null)).toBe("评分未完成（输出预算）");
    expect(judgeScoreLabel({ judge: { score: 0 } }, null)).toBe("0分");
    const rows = keyMetricRows({}, { operational: { not_evaluable_count: 2, by_kind: { step_limit: 2 }, judge_incomplete_count: 1, judge_by_kind: { output_limit: 1 } } });
    expect(rows.find((row) => row.label === "不可评估")).toMatchObject({ value: "2 题", note: "轮次或工具次数限制 2 题" });
    expect(rows.find((row) => row.label === "评分未完成")).toMatchObject({ value: "1 题", note: "输出预算 1 题" });
  });
});
