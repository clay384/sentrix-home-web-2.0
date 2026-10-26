// 移植自 1.0 `services/photobench/frontend/src/App.vue`（只读，唯一权威）。
// 覆盖：`averageMetric`(:375-378) / `nearestRankPercentile`(:379-383) /
// `microMetrics`(:403-412) / `macroMetrics`(:413-425) / `effectiveRunSummary`(:426-547)。
// `effectiveRunSummary` 依赖的 `itemCallMetrics`(:1335-1339) 与 `itemLlmSummary`(:1406-1419)
// 权威实现已在 `lib/trace.ts`（1.0 逐字移植），这里 import 复用，不再放文件私有副本。
//
// 核心口径（逐字段保留，**不要**简化）：
//   先信后端已存的 `run.summary` 里的字段，缺失（`??`）才用 `run.items` 现算——
//   operational契约的显式null保留，禁止从排除观察回算；历史 run 的 summary 是**当时的后端代码**
//   算出来的，一律改成现算会与历史显示不一致。
//
// 类型说明：1.0 的这几个函数是无类型 JS。本移植把内部记录（`saved` / `item` / `call`）
// 声明为 `Record<string, any>`，使函数体可以逐行照搬、不必在 60+ 个字段上逐个塞类型断言；
// 对外参数与返回值仍走 `api/types.ts` 的 `Run` / `RunSummary`。
//
// 复用：`inferMediaType` / `mediaKey` / `mediaRefs`（:384-401）已在 `api/media.ts` 移植，
// `averageMetric` 与 `format.ts` 的 `average` 逐字相同——两处都直接复用，不重复实现。

import { mediaRefs } from "../api/media.ts";
import { average as averageMetric } from "./format.ts";
import { itemCallMetrics, itemLlmSummary } from "./trace.ts";
import type { ItemLlmSummary } from "./trace.ts";
import type { Run, RunSummary } from "../api/types.ts";

/** 1.0 的无类型记录：`saved` / `item` / `call` 都按它读。 */
type Loose = Record<string, any>;

/** `microMetrics` / `macroMetrics` 的返回形状（1.0 是匿名对象字面量）。 */
export interface RetrievalMetric {
  precision: number | null;
  recall: number | null;
  f1: number | null;
  metricCount: number;
}

/**
 * 1.0 `:379-383`。**nearest-rank**，不是线性插值：取排序后第 `ceil(n*p)` 个（1-based）。
 * 4 个点取 p95 → `ceil(3.8)-1 = 3` → 最大值，而非线性插值的 38.5。
 */
export function nearestRankPercentile(values: readonly unknown[], percentile: number): number | null {
  const valid = values.filter((value): value is number => typeof value === "number" && Number.isFinite(value)).sort((a, b) => a - b);
  if (!valid.length) return null;
  return valid[Math.max(0, Math.min(valid.length - 1, Math.ceil(valid.length * percentile) - 1))];
}

/** 1.0 `:403-412`。按题汇总（pooled）：先累加 gt/predicted/matched，再算 P/R/F1。 */
export function microMetrics(items: readonly Loose[], prefix: string): RetrievalMetric {
  const rows = items.map((item) => item?.[`${prefix}_retrieval_counts`]).filter((row) => row && Number(row.gt) > 0);
  if (!rows.length) return { precision: null, recall: null, f1: null, metricCount: 0 };
  const gt = rows.reduce((sum, row) => sum + Number(row.gt || 0), 0);
  const predicted = rows.reduce((sum, row) => sum + Number(row.predicted || 0), 0);
  const matched = rows.reduce((sum, row) => sum + Number(row.matched || 0), 0);
  const precision = predicted ? matched / predicted : (gt ? 0 : null);
  const recall = gt ? matched / gt : null;
  return { precision, recall, f1: precision != null && recall != null && precision + recall ? 2 * precision * recall / (precision + recall) : (gt ? 0 : null), metricCount: rows.length };
}

/** 1.0 `:413-425`。按题平均（macro）：每题先算 P/R/F1，再对题取平均。 */
export function macroMetrics(items: readonly Loose[], prefix: string): RetrievalMetric {
  const rows = items.map((item) => item?.[`${prefix}_retrieval_counts`]).filter((row) => row && Number(row.gt) > 0);
  const values = rows.map((row) => {
    const gt = Number(row.gt || 0);
    const predicted = Number(row.predicted || 0);
    const matched = Number(row.matched || 0);
    const precision = predicted ? matched / predicted : 0;
    const recall = matched / gt;
    const f1 = precision + recall ? 2 * precision * recall / (precision + recall) : 0;
    return { precision, recall, f1 };
  });
  return { precision: averageMetric(values.map((row) => row.precision)), recall: averageMetric(values.map((row) => row.recall)), f1: averageMetric(values.map((row) => row.f1)), metricCount: values.length };
}

/** 1.0 `:426-547`。 */
export function effectiveRunSummary(run: Run | null | undefined): RunSummary {
  const saved = (run?.summary || {}) as Loose;
  const items = (run?.items || []) as unknown as Loose[];
  const qualityItems = items.filter((item) => !item.operational_issue && item.evaluation_status !== "not_evaluable");
  const scoredItems = qualityItems.filter((item) => !item.judge_issue);
  const metricItems = qualityItems.filter((item) => String(item?.answerability || "").toLowerCase() !== "unanswerable");
  const judged = scoredItems.filter((item) => typeof item.judge?.score === "number" && [0, 1, 2].includes(item.judge.score) && item.judge?.consistency_status !== "inconsistent" && !["failed", "outcome_unknown"].includes(item.judge?.judge_status));
  const recalls = metricItems.map((item) => item.retrieval_recall).filter((value) => Number.isFinite(Number(value)));
  const scores = judged.map((item) => item.judge.score as number);
  const evidenceScores = scoredItems.map((item) => item.evidence_judge?.score).filter((score) => [0, 1, 2].includes(score));
  const typedRetrieval = qualityItems.some((item) => Object.prototype.hasOwnProperty.call(item, "retrieval_media_refs"));
  const retrievalItems = metricItems.filter((item) => mediaRefs(item).length);
  const retrievalTp = retrievalItems.reduce((sum, item) => sum + (item.matched_file_names || []).length, 0);
  const retrievalPredicted = retrievalItems.reduce((sum, item) => sum + (item.retrieved_file_names || []).length, 0);
  const retrievalGt = retrievalItems.reduce((sum, item) => sum + (item.retrieval_image_ids || []).length, 0);
  const retrievalPrecision = retrievalPredicted ? retrievalTp / retrievalPredicted : (retrievalGt ? 0 : null);
  const retrievalRecall = retrievalGt ? retrievalTp / retrievalGt : null;
  const retrievalF1 = retrievalPrecision != null && retrievalRecall != null && retrievalPrecision + retrievalRecall
    ? 2 * retrievalPrecision * retrievalRecall / (retrievalPrecision + retrievalRecall) : (retrievalGt ? 0 : null);
  const mediaMicro = microMetrics(metricItems, "media");
  const imageMicro = microMetrics(metricItems, "image");
  const videoMicro = microMetrics(metricItems, "video");
  const mediaMacro = macroMetrics(metricItems, "media");
  const imageMacro = macroMetrics(metricItems, "image");
  const videoMacro = macroMetrics(metricItems, "video");
  const actionJudges = scoredItems.flatMap((item) => item.task_judges?.length ? item.task_judges : [item.task_judge])
    .filter((judge) => [true, false].includes(judge?.correct) && !judge.operational_issue && !["failed", "outcome_unknown"].includes(judge.judge_status));
  const parseTotals = items.map((item) => item.agent_stability?.json_parse_total).filter(Number.isFinite);
  const parseSuccesses = items.map((item) => item.agent_stability?.json_parse_success).filter(Number.isFinite);
  const completion = qualityItems.map((item) => item.agent_stability?.completed_within_steps).filter((value) => typeof value === "boolean");
  const agentTaskLatencies = items.map((item) => item.timing_breakdown?.agent_wall_ms ?? (item.metrics_contract_version === 2 ? item.wall_clock_ms : undefined)).filter(Number.isFinite);
  const agentLoopCounts = items.map((item) => {
    const calls: Loose[] = itemCallMetrics(item);
    return calls.some((call) => call.call_type)
      ? calls.filter((call) => ["agent", "recovery"].includes(call.call_type)).length
      : null;
  }).filter(Number.isFinite);
  const dist = { ...(saved.judge_distribution || {}) };
  if (!Object.keys(saved.judge_distribution || {}).length) scores.forEach((score) => { dist[String(score)] = (dist[String(score)] || 0) + 1; });
  const llm = items.map(itemLlmSummary).filter((item): item is ItemLlmSummary => Boolean(item));
  const allCalls = items.flatMap(itemCallMetrics);
  const callTokenCounts = allCalls.map((call) => {
    const prompt = Number(call.preflight_prompt_tokens ?? call.prompt_tokens);
    const completion = Number(call.completion_tokens);
    return Number.isFinite(prompt) && Number.isFinite(completion) ? { prompt, completion, context: prompt + completion } : null;
  }).filter((row): row is { prompt: number; completion: number; context: number } => row !== null);
  const promptTokens = allCalls.map((call) => Number(call.preflight_prompt_tokens ?? call.prompt_tokens)).filter(Number.isFinite);
  const completionTokens = callTokenCounts.map((call) => call.completion);
  const contextTokens = callTokenCounts.map((call) => call.context);
  const result: Loose = {
    ...saved,
    run_valid: run?.mode === "build" ? null : saved.run_valid,
    completed: saved.completed ?? items.length,
    total: saved.total ?? run?.qa_count ?? items.length,
    judge_valid_count: saved.judge_valid_count ?? judged.length,
    judge_distribution: dist,
    retrieval_recall_mean: saved.retrieval_recall_macro ?? (typedRetrieval ? mediaMacro.recall : averageMetric(recalls)),
    retrieval_precision_macro: saved.retrieval_precision_macro ?? (typedRetrieval ? mediaMacro.precision : averageMetric(retrievalItems.map((item) => item.retrieval_precision).filter(Number.isFinite))),
    retrieval_recall_macro: saved.retrieval_recall_macro ?? (typedRetrieval ? mediaMacro.recall : averageMetric(recalls)),
    retrieval_f1_macro: saved.retrieval_f1_macro ?? (typedRetrieval ? mediaMacro.f1 : averageMetric(retrievalItems.map((item) => item.retrieval_f1).filter(Number.isFinite))),
    retrieval_excluded_unanswerable_count: saved.retrieval_excluded_unanswerable_count ?? (qualityItems.length - metricItems.length),
    answer_quality_mean: saved.answer_quality_mean ?? (scores.length ? averageMetric(scores) : null),
    exact_accuracy: saved.exact_accuracy ?? (scores.length ? scores.filter((score) => score === 2).length / scores.length : null),
    core_accuracy: saved.core_accuracy ?? (scores.length ? scores.filter((score) => score >= 1).length / scores.length : null),
    retrieval_precision_micro: saved.retrieval_precision_micro ?? (typedRetrieval ? mediaMicro.precision : retrievalPrecision),
    retrieval_recall_micro: saved.retrieval_recall_micro ?? (typedRetrieval ? mediaMicro.recall : retrievalRecall),
    retrieval_f1_micro: saved.retrieval_f1_micro ?? (typedRetrieval ? mediaMicro.f1 : retrievalF1),
    retrieval_metric_count: saved.retrieval_metric_count ?? retrievalItems.length,
    retrieval_metric_scope: saved.retrieval_metric_scope ?? (typedRetrieval ? "all_media" : "legacy_image_only"),
    media_retrieval_precision_micro: saved.media_retrieval_precision_micro ?? (typedRetrieval ? mediaMicro.precision : null),
    media_retrieval_recall_micro: saved.media_retrieval_recall_micro ?? (typedRetrieval ? mediaMicro.recall : null),
    media_retrieval_f1_micro: saved.media_retrieval_f1_micro ?? (typedRetrieval ? mediaMicro.f1 : null),
    media_retrieval_precision_macro: saved.media_retrieval_precision_macro ?? (typedRetrieval ? mediaMacro.precision : null),
    media_retrieval_recall_macro: saved.media_retrieval_recall_macro ?? (typedRetrieval ? mediaMacro.recall : null),
    media_retrieval_f1_macro: saved.media_retrieval_f1_macro ?? (typedRetrieval ? mediaMacro.f1 : null),
    media_retrieval_metric_count: saved.media_retrieval_metric_count ?? (typedRetrieval ? mediaMicro.metricCount : null),
    image_retrieval_precision_micro: saved.image_retrieval_precision_micro ?? (typedRetrieval ? imageMicro.precision : retrievalPrecision),
    image_retrieval_recall_micro: saved.image_retrieval_recall_micro ?? (typedRetrieval ? imageMicro.recall : retrievalRecall),
    image_retrieval_f1_micro: saved.image_retrieval_f1_micro ?? (typedRetrieval ? imageMicro.f1 : retrievalF1),
    image_retrieval_precision_macro: saved.image_retrieval_precision_macro ?? (typedRetrieval ? imageMacro.precision : averageMetric(retrievalItems.map((item) => item.retrieval_precision).filter(Number.isFinite))),
    image_retrieval_recall_macro: saved.image_retrieval_recall_macro ?? (typedRetrieval ? imageMacro.recall : averageMetric(recalls)),
    image_retrieval_f1_macro: saved.image_retrieval_f1_macro ?? (typedRetrieval ? imageMacro.f1 : averageMetric(retrievalItems.map((item) => item.retrieval_f1).filter(Number.isFinite))),
    image_retrieval_metric_count: saved.image_retrieval_metric_count ?? (typedRetrieval ? imageMicro.metricCount : retrievalItems.length),
    video_retrieval_precision_micro: saved.video_retrieval_precision_micro ?? (typedRetrieval ? videoMicro.precision : null),
    video_retrieval_recall_micro: saved.video_retrieval_recall_micro ?? (typedRetrieval ? videoMicro.recall : null),
    video_retrieval_f1_micro: saved.video_retrieval_f1_micro ?? (typedRetrieval ? videoMicro.f1 : null),
    video_retrieval_precision_macro: saved.video_retrieval_precision_macro ?? (typedRetrieval ? videoMacro.precision : null),
    video_retrieval_recall_macro: saved.video_retrieval_recall_macro ?? (typedRetrieval ? videoMacro.recall : null),
    video_retrieval_f1_macro: saved.video_retrieval_f1_macro ?? (typedRetrieval ? videoMacro.f1 : null),
    video_retrieval_metric_count: saved.video_retrieval_metric_count ?? (typedRetrieval ? videoMicro.metricCount : null),
    evidence_distribution: saved.evidence_distribution ?? { 0: evidenceScores.filter((score) => score === 0).length, 1: evidenceScores.filter((score) => score === 1).length, 2: evidenceScores.filter((score) => score === 2).length },
    evidence_valid_count: saved.evidence_valid_count ?? evidenceScores.length,
    evidence_mean: saved.evidence_mean ?? (evidenceScores.length ? averageMetric(evidenceScores) : null),
    evidence_fully_supported_rate: saved.evidence_fully_supported_rate ?? (evidenceScores.length ? evidenceScores.filter((score) => score === 2).length / evidenceScores.length : null),
    evidence_basically_supported_rate: saved.evidence_basically_supported_rate ?? (evidenceScores.length ? evidenceScores.filter((score) => score >= 1).length / evidenceScores.length : null),
    task_decision_labeled_count: saved.task_decision_labeled_count ?? actionJudges.length,
    task_decision_valid_count: saved.task_decision_valid_count ?? actionJudges.length,
    task_decision_accuracy: saved.task_decision_accuracy ?? (actionJudges.length ? actionJudges.filter((judge) => judge.correct).length / actionJudges.length : null),
    json_parse_total: saved.json_parse_total ?? (parseTotals.length ? parseTotals.reduce((sum, value) => sum + value, 0) : null),
    json_parse_success: saved.json_parse_success ?? (parseSuccesses.length ? parseSuccesses.reduce((sum, value) => sum + value, 0) : null),
    json_parse_success_rate: saved.json_parse_success_rate ?? (parseTotals.length ? parseSuccesses.reduce((sum, value) => sum + value, 0) / parseTotals.reduce((sum, value) => sum + value, 0) : null),
    qa_completion_valid_count: saved.qa_completion_valid_count ?? completion.length,
    qa_completion_within_steps_rate: saved.qa_completion_within_steps_rate ?? (completion.length ? completion.filter(Boolean).length / completion.length : null),
    agent_task_latency_mean_ms: saved.agent_task_latency_mean_ms ?? averageMetric(agentTaskLatencies),
    agent_loop_calls_mean: saved.agent_loop_calls_mean ?? averageMetric(agentLoopCounts),
    llm_ttft_ms_mean: saved.llm_ttft_ms_mean ?? averageMetric(llm.map((item) => item.ttft_ms_avg)),
    llm_tokens_per_second_mean: saved.llm_tokens_per_second_mean ?? averageMetric(llm.map((item) => item.tokens_per_second_avg)),
    prompt_tokens_total: saved.prompt_tokens_total ?? (allCalls.length ? llm.reduce((sum, item) => sum + (Number(item.prompt_tokens_total) || 0), 0) : null),
    completion_tokens_total: saved.completion_tokens_total ?? (allCalls.length ? llm.reduce((sum, item) => sum + (Number(item.completion_tokens_total) || 0), 0) : null),
    llm_prompt_tokens_max: saved.llm_prompt_tokens_max ?? (promptTokens.length ? Math.max(...promptTokens) : null),
    llm_prompt_tokens_p95: saved.llm_prompt_tokens_p95 ?? nearestRankPercentile(promptTokens, 0.95),
    llm_completion_tokens_max: saved.llm_completion_tokens_max ?? (completionTokens.length ? Math.max(...completionTokens) : null),
    llm_completion_tokens_p95: saved.llm_completion_tokens_p95 ?? nearestRankPercentile(completionTokens, 0.95),
    llm_context_tokens_max: saved.llm_context_tokens_max ?? (contextTokens.length ? Math.max(...contextTokens) : null),
    llm_context_tokens_p95: saved.llm_context_tokens_p95 ?? nearestRankPercentile(contextTokens, 0.95),
    llm_context_samples_count: saved.llm_context_samples_count ?? contextTokens.length,
    agent_throughput_latency_mode: saved.agent_throughput_latency_mode
      ?? (run?.phases?.qa_eval?.agent_phase_wall_ms != null ? "measured_agent_phase" : "historical_interval_estimate"),
    agent_phase_wall_ms: saved.agent_phase_wall_ms ?? run?.phases?.qa_eval?.agent_phase_wall_ms ?? null,
    agent_phase_completed_count: saved.agent_phase_completed_count ?? run?.phases?.qa_eval?.agent_completed ?? null,
    agent_throughput_qa_per_s: saved.agent_throughput_qa_per_s ?? null,
    judge_phase_wall_ms: saved.judge_phase_wall_ms ?? run?.phases?.qa_eval?.judge_phase_wall_ms ?? null,
    judge_concurrency: saved.judge_concurrency ?? run?.phases?.qa_eval?.judge_concurrency ?? run?.judge_concurrency ?? null,
  };
  // The operational contract uses explicit null for excluded/unknown metrics.
  // Never resurrect those values from retained raw observations; legacy fallback stays unchanged.
  if (Object.prototype.hasOwnProperty.call(saved, "operational")) {
    for (const [key, value] of Object.entries(saved)) if (value === null) result[key] = null;
  }
  return result as RunSummary;
}
