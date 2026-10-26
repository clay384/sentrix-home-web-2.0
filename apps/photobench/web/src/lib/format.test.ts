import { describe, expect, it } from "vitest";

import {
  actionLabel,
  albumName,
  average,
  callOutcome,
  callOutcomeClass,
  callStatus,
  callType,
  callTypeDescription,
  callTypeLabel,
  completionLabel,
  duration,
  esc,
  evidenceScoreLabel,
  executionState,
  executionStateClass,
  fmtDate,
  fmtMemory,
  fmtMs,
  fmtNumber,
  fmtPct,
  fmtSeconds,
  fmtTokenRate,
  fmtTokens,
  keyframeDeltaLabel,
  keyframeReasonLabel,
  judgeRoundState,
  judgeScoreLabel,
  memoryLayerToolMetric,
  modelName,
  phaseSeconds,
  qaName,
  retrievalBackendDegraded,
  retrievalBackendLabel,
  scoreClass,
  statusLabel,
  taskDecisionLabel,
  toolBindingLabel,
  turnCompletionLabel,
  turnTerminationLabel,
} from "./format.ts";

// 夹具原则：每条断言都要能在「有值 / 无值」下给出**不同**结果，
// 否则它测不出口径被统一（`-` vs `""` vs `未记录` vs `unknown`）。

describe("esc (:323)", () => {
  it("null / undefined 回空串", () => {
    expect(esc(null)).toBe("");
    expect(esc(undefined)).toBe("");
  });
  it("0 与 false 不被吞掉（用 ?? 而非 ||）", () => {
    expect(esc(0)).toBe("0");
    expect(esc(false)).toBe("false");
  });
});

describe("modelName / albumName / qaName (:324-326)", () => {
  it("按优先级回退，末位是各自的固定兜底", () => {
    expect(modelName({ model_profile: "p", model_name: "n" })).toBe("n");
    expect(modelName({ model_name: "n", profile: "pf" })).toBe("n");
    expect(modelName({ profile: "pf" })).toBe("pf");
    expect(modelName({ model_profile: "__current__", endpoint_model: "qwen35-q4km" })).toBe("qwen35-q4km");
    expect(
      modelName({
        model_profile: "__current__",
        model_name: "__current__",
        album_id: "album3-ai-video13",
        scope_name: "PhotoBench-20261021-220518-album3-ai-video13-qwen35-q4km",
      }),
    ).toBe("qwen35-q4km");
    expect(modelName({ model_profile: "__current__" })).toBe("current");
    expect(modelName({})).toBe("unknown");
    expect(modelName(null)).toBe("unknown");

    expect(albumName({ scope_name: "s", album_id: "a" })).toBe("s");
    expect(albumName({ album_id: "a", qa_name: "q" })).toBe("a");
    expect(albumName({})).toBe("album");

    expect(qaName({ qa_set: "qs", qa_name: "qn" })).toBe("qs");
    expect(qaName({ qa_name: "qn" })).toBe("qn");
    expect(qaName({})).toBe("qa");
  });
});

describe("fmtDate (:327)", () => {
  it("falsy 回 `-`（0 与空串都算无值）", () => {
    expect(fmtDate("")).toBe("-");
    expect(fmtDate(0)).toBe("-");
    expect(fmtDate(null)).toBe("-");
    expect(fmtDate(undefined)).toBe("-");
  });
  it("有值走 zh-CN 短格式，不是 `-`", () => {
    const out = fmtDate("2026-01-02T03:04:00Z");
    expect(out).not.toBe("-");
    expect(out).toContain(":");
  });
});

describe("duration (:328-333)", () => {
  it("无 started_at 回 `-`", () => {
    expect(duration({})).toBe("-");
    expect(duration(null)).toBe("-");
  });
  it("不足一分钟 `${s}s`，否则 `${m}m${s}s`", () => {
    expect(duration({ started_at: "2026-01-02T03:04:00Z", finished_at: "2026-01-02T03:04:30Z" })).toBe("30s");
    expect(duration({ started_at: "2026-01-02T03:04:00Z", finished_at: "2026-01-02T03:05:30Z" })).toBe("1m30s");
  });
  it("结束早于开始会被 Math.max(0) 夹成 0s", () => {
    expect(duration({ started_at: "2026-01-02T03:05:00Z", finished_at: "2026-01-02T03:04:00Z" })).toBe("0s");
  });
});

describe("fmtMs (:334)", () => {
  it("null / undefined 回 `-`", () => {
    expect(fmtMs(null)).toBe("-");
    expect(fmtMs(undefined)).toBe("-");
  });
  it("0 走 `0ms`（不是 `-`）", () => {
    expect(fmtMs(0)).toBe("0ms");
  });
  it(">=1000 换秒、一位小数；否则整毫秒", () => {
    expect(fmtMs(500)).toBe("500ms");
    expect(fmtMs(1500)).toBe("1.5s");
  });
});

describe("fmtPct (:335)", () => {
  it("null 回 `-`，0 回 `0.0%`", () => {
    expect(fmtPct(null)).toBe("-");
    expect(fmtPct(0)).toBe("0.0%");
  });
  it("乘 100、一位小数", () => {
    expect(fmtPct(0.1234)).toBe("12.3%");
  });
});

describe("fmtTokens (:336)", () => {
  it("null 与非有限数回 `-`", () => {
    expect(fmtTokens(null)).toBe("-");
    expect(fmtTokens(Number.NaN)).toBe("-");
    expect(fmtTokens("abc")).toBe("-");
  });
  it("0 是有效值", () => {
    expect(fmtTokens(0)).toBe("0 token");
  });
  it("四舍五入 + en-US 千分位", () => {
    expect(fmtTokens(1234)).toBe("1,234 token");
  });
});

describe("scoreClass (:337)", () => {
  it("2/1/0 各有类名，其余落 score-none", () => {
    expect(scoreClass(2)).toBe("score-2");
    expect(scoreClass(1)).toBe("score-1");
    expect(scoreClass(0)).toBe("score-0");
    expect(scoreClass(undefined)).toBe("score-none");
    expect(scoreClass(null)).toBe("score-none");
  });
  it("严格相等：字符串 `2` 落 score-none", () => {
    expect(scoreClass("2")).toBe("score-none");
  });
});

describe("statusLabel (:338)", () => {
  it("映射命中回中文", () => {
    expect(statusLabel("done")).toBe("完成");
    expect(statusLabel("completed_with_errors")).toBe("完成但有错误");
    expect(statusLabel("skipped")).toBe("不适用");
  });
  it("未命中回**原值**，空/缺省回 `等待`", () => {
    expect(statusLabel("weird")).toBe("weird");
    expect(statusLabel("")).toBe("等待");
    expect(statusLabel(undefined)).toBe("等待");
  });
});

describe("actionLabel (:556-558)", () => {
  it("映射命中回中文", () => {
    expect(actionLabel("answer")).toBe("回答");
    expect(actionLabel("none")).toBe("无有效行为");
  });
  it("未命中回 `未记录`（**不**回原值；与 statusLabel 不同）", () => {
    expect(actionLabel("weird")).toBe("未记录");
    expect(actionLabel(undefined)).toBe("未记录");
  });
});

describe("evidenceScoreLabel (:559-565)", () => {
  it("分数优先", () => {
    expect(evidenceScoreLabel({ score: 2 })).toBe("2 分：媒体证据支持回答");
    expect(evidenceScoreLabel({ score: 1 })).toBe("1 分：媒体证据部分支持");
    expect(evidenceScoreLabel({ score: 0 })).toBe("0 分：媒体证据无法支持");
  });
  it("无分数时看 reason", () => {
    expect(evidenceScoreLabel({ reason: "not_applicable" })).toBe("不适用");
    expect(evidenceScoreLabel({ reason: "no_answer" })).toBe("无回答，未评分");
    expect(evidenceScoreLabel({})).toBe("未记录");
    expect(evidenceScoreLabel(null)).toBe("未记录");
  });
});

describe("executionState / executionStateClass / completionLabel (:580-601)", () => {
  it("完全无信号 -> unknown / 未记录", () => {
    expect(executionState({})).toEqual({ key: "unknown", label: "未记录" });
  });
  it("失败词命中 -> failed / 执行失败", () => {
    expect(executionState({ agent_status: "failed" })).toEqual({ key: "failed", label: "执行失败" });
    expect(executionState({ runtime_turns: [{ turn_outcome: "model_error" }] })).toEqual({ key: "failed", label: "执行失败" });
    expect(executionStateClass({ agent_status: "failed" })).toBe("status-failed");
  });
  it("部分完成词命中 -> partial", () => {
    expect(executionState({ termination_reason: "step_limit" })).toEqual({ key: "partial", label: "部分完成" });
  });
  it("final_answer / complete 状态 -> complete", () => {
    expect(executionState({ turn_outcome: "final_answer" })).toEqual({ key: "complete", label: "已完成" });
    expect(executionState({ agent_status: "done" })).toEqual({ key: "complete", label: "已完成" });
    expect(executionState({ agent_stability: { completed_within_steps: true } })).toEqual({ key: "complete", label: "已完成" });
  });
  it("有信号但认不出 -> unknown / 状态待确认（与完全无信号不同）", () => {
    expect(executionState({ agent_status: "weird" })).toEqual({ key: "unknown", label: "状态待确认" });
    expect(executionState({ agent_stability: { completed_within_steps: false } })).toEqual({ key: "unknown", label: "状态待确认" });
  });
  it("completionLabel 与 executionState().label 同源", () => {
    expect(completionLabel({ agent_status: "failed" })).toBe("执行失败");
    expect(completionLabel({})).toBe("未记录");
  });
});

describe("taskDecisionLabel (:602-606)", () => {
  it("无 expected_action 回 `未标注`", () => {
    expect(taskDecisionLabel({})).toBe("未标注");
    expect(taskDecisionLabel({ task_judge: { actual_action: "answer" } })).toBe("未标注");
  });
  it("有 expected_action 时拼期望/实际，缺实际回 `未记录`", () => {
    expect(taskDecisionLabel({ task_judge: { expected_action: "answer", actual_action: "refuse" } }))
      .toBe("期望回答，实际拒答");
    expect(taskDecisionLabel({ task_judge: { expected_action: "answer" } })).toBe("期望回答，实际未记录");
  });
});

describe("toolBindingLabel (:728-731)", () => {
  it("三档各有字面量", () => {
    expect(toolBindingLabel({ round_binding_source: "step_id" })).toBe("按步骤 ID 精确绑定");
    expect(toolBindingLabel({ round_binding_source: "inferred_single_model_call" })).toBe("单轮数据推断归属");
    expect(toolBindingLabel({ round_binding_source: "other" })).toBe("按执行轨迹绑定");
    expect(toolBindingLabel({})).toBe("按执行轨迹绑定");
  });
});

describe("retrievalBackendLabel / retrievalBackendDegraded (:732-740)", () => {
  it("有后端时去重后用 `/` 连接；无后端回**空串**（不是 `-`）", () => {
    expect(retrievalBackendLabel({ retrieval_timing: { channels: { a: { backend: "qdrant" }, b: { backend: "wemm" }, c: { backend: "qdrant" } } } })).toBe("qdrant/wemm");
    expect(retrievalBackendLabel({})).toBe("");
    expect(retrievalBackendLabel(null)).toBe("");
  });
  it("只要出现非 qdrant 就算降级；空标签不算降级", () => {
    expect(retrievalBackendDegraded({ retrieval_timing: { channels: { a: { backend: "wemm" } } } })).toBe(true);
    expect(retrievalBackendDegraded({ retrieval_timing: { channels: { a: { backend: "qdrant" } } } })).toBe(false);
    expect(retrievalBackendDegraded({})).toBe(false);
  });
});

describe("judgeRoundState (:741-749)", () => {
  const task = { rejudge_id: "r1", status: "running" };
  it("无任务恒 normal", () => {
    expect(judgeRoundState({ judge: { status: "failed" } }, null)).toBe("normal");
  });
  it("不属于本任务的题 -> running 时 pending，否则 normal", () => {
    expect(judgeRoundState({ judge: { rejudge_id: "r2" } }, task)).toBe("pending");
    expect(judgeRoundState({ judge: { rejudge_id: "r2" } }, { rejudge_id: "r1", status: "completed" })).toBe("normal");
  });
  it("任务已停但该题还挂 pending/running -> interrupted", () => {
    expect(judgeRoundState({ judge: { rejudge_id: "r1", status: "pending" } }, { rejudge_id: "r1", status: "completed" })).toBe("interrupted");
  });
  it("任务在跑时透出该题自己的状态；已完成项 -> updated", () => {
    expect(judgeRoundState({ judge: { rejudge_id: "r1", status: "failed" } }, task)).toBe("failed");
    expect(judgeRoundState({ judge: { rejudge_id: "r1", status: "completed" } }, task)).toBe("updated");
  });
});

describe("judgeScoreLabel (:750-758)", () => {
  it("不一致优先回 `评分异常`", () => {
    expect(judgeScoreLabel({ judge: { consistency_status: "inconsistent", score: 2 } }, null)).toBe("评分异常");
  });
  it("按轮次状态出文案", () => {
    const running = { rejudge_id: "r1", status: "running" };
    expect(judgeScoreLabel({ judge: { rejudge_id: "r2" } }, running)).toBe("待重新评分");
    expect(judgeScoreLabel({ judge: { rejudge_id: "r1", status: "failed" } }, running)).toBe("评分失败");
    expect(judgeScoreLabel({ judge: { rejudge_id: "r1", status: "pending" } }, { rejudge_id: "r1", status: "completed" })).toBe("本轮未完成");
  });
  it("正常态：无分回 `未评分`，0 分回 `0分`", () => {
    expect(judgeScoreLabel({ judge: {} }, null)).toBe("未评分");
    expect(judgeScoreLabel({ judge: { score: 0 } }, null)).toBe("0分");
    expect(judgeScoreLabel({ judge: { score: 2 } }, null)).toBe("2分");
  });
});

describe("phaseSeconds (:759-768)", () => {
  it("无相位回 null", () => {
    expect(phaseSeconds(null)).toBeNull();
    expect(phaseSeconds({})).toBeNull();
    expect(phaseSeconds({ total_seconds: "abc" })).toBeNull();
    expect(phaseSeconds({ total_seconds: null })).toBeNull();
    expect(phaseSeconds({ total_seconds: false })).toBeNull();
    expect(phaseSeconds({ total_seconds: "" })).toBeNull();
    expect(phaseSeconds({ total_seconds: -1 })).toBeNull();
  });
  it("正的 preferred 优先；0 也回 0（不是 null）", () => {
    expect(phaseSeconds({ total_seconds: 5 })).toBe(5);
    expect(phaseSeconds({ total_seconds: 0 })).toBe(0);
  });
  it("preferred 非正时用起止时间兜底", () => {
    expect(phaseSeconds({ total_seconds: 0, started_at: "2026-01-02T03:04:00Z", finished_at: "2026-01-02T03:04:02Z" })).toBe(2);
  });
  it("自定义 key", () => {
    expect(phaseSeconds({ judge_seconds: 3 }, "judge_seconds")).toBe(3);
  });
});

describe("fmtSeconds (:769-776)", () => {
  it("null / 非有限数回 `-`；0 回 `<1ms`", () => {
    expect(fmtSeconds(null)).toBe("-");
    expect(fmtSeconds(Number.NaN)).toBe("-");
    expect(fmtSeconds(0)).toBe("<1ms");
  });
  it("三档刻度", () => {
    expect(fmtSeconds(0.05)).toBe("50ms");
    expect(fmtSeconds(5)).toBe("5s");
    expect(fmtSeconds(5.2)).toBe("5.2s");
    expect(fmtSeconds(12)).toBe("12s");
  });
});

describe("fmtNumber (:777-780)", () => {
  it("null / 非有限数回 `-`；整数值去掉 `.0`", () => {
    expect(fmtNumber(null)).toBe("-");
    expect(fmtNumber(Number.NaN)).toBe("-");
    expect(fmtNumber(1)).toBe("1");
  });
  it("自定义后缀与位数", () => {
    expect(fmtNumber(12.5, "%", 2)).toBe("12.50%");
    expect(fmtNumber(2.5, "s")).toBe("2.5s");
  });
});

describe("fmtMemory (:781-784)", () => {
  it("null 回 `-`，输入按 MiB 折 GiB、恒两位", () => {
    expect(fmtMemory(null)).toBe("-");
    expect(fmtMemory(2048)).toBe("2.00 GiB");
  });
});

describe("average (:1402-1405)", () => {
  it("空 / 全无效回 null", () => {
    expect(average([])).toBeNull();
    expect(average([Number.NaN, "abc"])).toBeNull();
  });
  it("数字字符串参与计算，非有限项被剔除", () => {
    expect(average([1, 2, "3"])).toBe(2);
    expect(average([1, 2, Number.NaN])).toBe(1.5);
  });
});

describe("fmtTokenRate (:1421-1423)", () => {
  it("null / 非有限数回 `-`", () => {
    expect(fmtTokenRate(null)).toBe("-");
    expect(fmtTokenRate(Number.NaN)).toBe("-");
  });
  it("一位小数 + 单位", () => {
    expect(fmtTokenRate(12.34)).toBe("12.3 token/s");
  });
});

describe("callStatus (:1424-1429)", () => {
  it("两个特判状态", () => {
    expect(callStatus({ status: "context_budget_exceeded" })).toBe("调用前拦截");
    expect(callStatus({ status: "error" })).toBe("调用失败");
  });
  it("其它非 complete 状态原样透出", () => {
    expect(callStatus({ status: "timeout" })).toBe("timeout");
  });
  it("complete / 无状态看 streamed", () => {
    expect(callStatus({ status: "complete", streamed: true })).toBe("成功 · 流式");
    expect(callStatus({ streamed: false })).toBe("成功 · 非流式");
    expect(callStatus({})).toBe("未记录");
  });
});

describe("callOutcome / callOutcomeClass (:1430-1443)", () => {
  it("tool_call 拼 next_step；缺了回落到 related_tool 或 `-`", () => {
    expect(callOutcome({ turn_outcome: "tool_call", next_step: "search" })).toBe("本轮结果：调用工具 search");
    expect(callOutcome({ turn_outcome: "tool_call", call_observation: { related_tool: "ocr" } })).toBe("本轮结果：调用工具 ocr");
    expect(callOutcome({ turn_outcome: "tool_call" })).toBe("本轮结果：调用工具 -");
  });
  it("各 turn_outcome 的固定文案", () => {
    expect(callOutcome({ turn_outcome: "final_answer" })).toBe("本轮结果：正常回答结束");
    expect(callOutcome({ turn_outcome: "parse_failure" })).toBe("本轮结果：JSON 解析失败");
    expect(callOutcome({ turn_outcome: "model_error" })).toBe("本轮结果：模型请求失败");
    expect(callOutcome({ turn_outcome: "context_blocked" })).toBe("本轮结果：上下文或 token 预检拦截");
    expect(callOutcome({ turn_outcome: "step_limit" })).toBe("本轮结果：达到最大执行步数");
  });
  it("未记录回 `本轮结果：历史记录未保存`", () => {
    expect(callOutcome({})).toBe("本轮结果：历史记录未保存");
    expect(callOutcome(null)).toBe("本轮结果：历史记录未保存");
  });
  it("类名：失败类 -> outcome-failed，其余有值 -> outcome-ok，无值 -> 空串", () => {
    expect(callOutcomeClass({ turn_outcome: "parse_failure" })).toBe("outcome-failed");
    expect(callOutcomeClass({ turn_outcome: "final_answer" })).toBe("outcome-ok");
    expect(callOutcomeClass({})).toBe("");
  });
});

describe("callType / callTypeLabel / callTypeDescription (:1444-1470)", () => {
  it("callType 三级回退，末位 legacy", () => {
    expect(callType({ call_type: "agent" })).toBe("agent");
    expect(callType({ call_observation: { kind: "planner" } })).toBe("planner");
    expect(callType({})).toBe("legacy");
  });
  it("label：observation.label 优先，其次映射，未命中回 call_type 原值", () => {
    expect(callTypeLabel({ call_observation: { label: "自定义" }, call_type: "agent" })).toBe("自定义");
    expect(callTypeLabel({ call_type: "agent" })).toBe("Agent 决策 / 回答");
    expect(callTypeLabel({ call_type: "custom" })).toBe("custom");
  });
  it("description：purpose 优先，未命中回固定兜底", () => {
    expect(callTypeDescription({ call_observation: { purpose: "做事" } })).toBe("做事");
    expect(callTypeDescription({ call_type: "planner" })).toBe("解析用户目标并声明最小充分证据需求（TaskState/EvidenceLedger）");
    expect(callTypeDescription({ call_type: "custom" })).toBe("后端记录的模型调用类型");
  });
});

describe("turnTerminationLabel / turnCompletionLabel (:1384-1398)", () => {
  it("token 预检 502 的正则特判", () => {
    expect(turnTerminationLabel({ termination_reason: "tokenize-current returned 502 Bad Gateway" })).toBe("上下文 token 预检失败（tokenize 接口 502）");
  });
  it("映射命中回中文，未命中回原值，空回 `未记录`", () => {
    expect(turnTerminationLabel({ termination_reason: "complete" })).toBe("正常完成");
    expect(turnTerminationLabel({ termination_reason: "custom" })).toBe("custom");
    expect(turnTerminationLabel({})).toBe("未记录");
  });
  it("turnCompletionLabel 复用 executionState", () => {
    expect(turnCompletionLabel({ agent_status: "failed" })).toBe("执行失败");
    expect(turnCompletionLabel({})).toBe("未记录");
  });
});

describe("memoryLayerToolMetric (:1806-1809)", () => {
  it("缺该工具 -> calls 0 / okRate null", () => {
    expect(memoryLayerToolMetric({}, "ocr")).toEqual({ calls: 0, okRate: null });
    expect(memoryLayerToolMetric(null, "ocr")).toEqual({ calls: 0, okRate: null });
  });
  it("有值时数字化，ok_rate 为 0 时保留 0（不塌成 null）", () => {
    expect(memoryLayerToolMetric({ tool_performance: { ocr: { calls: "3", ok_rate: 0 } } }, "ocr")).toEqual({ calls: 3, okRate: 0 });
    expect(memoryLayerToolMetric({ tool_performance: { ocr: { calls: 3, ok_rate: "0.5" } } }, "ocr")).toEqual({ calls: 3, okRate: 0.5 });
  });
});

describe("keyframeDeltaLabel / keyframeReasonLabel (:2038-2045)", () => {
  it("差值带符号、两位小数、末缀 ` / 2`；null/NaN 回 `-`", () => {
    expect(keyframeDeltaLabel(null)).toBe("-");
    expect(keyframeDeltaLabel(Number.NaN)).toBe("-");
    expect(keyframeDeltaLabel(0)).toBe("+0.00 / 2");
    expect(keyframeDeltaLabel(0.5)).toBe("+0.50 / 2");
    expect(keyframeDeltaLabel(-0.25)).toBe("-0.25 / 2");
  });
  it("原因分布用 ` · ` 连接；空回 `-`", () => {
    expect(keyframeReasonLabel({})).toBe("-");
    expect(keyframeReasonLabel(null)).toBe("-");
    expect(keyframeReasonLabel({ a: 1 })).toBe("a×1");
    expect(keyframeReasonLabel({ a: 1, b: 2 })).toBe("a×1 · b×2");
  });
});
