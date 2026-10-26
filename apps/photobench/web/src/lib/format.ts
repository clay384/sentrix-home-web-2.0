// 移植自 1.0 `services/photobench/frontend/src/App.vue`（只读，唯一权威）的纯格式化 / 标签函数。
//
// 口径纪律：`-` / `""` / `"unknown"` / `"未记录"` / `"未分类"` 在 1.0 里各不相同，
// 是每处逐条写死的，重写时**不要**把它们「统一」。每个函数尾注了 1.0 的行号。
//
// 不 import `../api/types`：本模块只吃基本类型与字符串字面量，形状靠 `unknown` + 运行时收窄。

type Dict = Record<string, unknown>;

/** 1.0 的可选链语义：非对象一律当空对象，取属性得 undefined。 */
function dict(value: unknown): Dict {
  return typeof value === "object" && value !== null ? (value as Dict) : {};
}

/**
 * 1.0 的回退链用 `||`，参与比较的都是字符串字段。
 * 非字符串（含 null / undefined）折成空串，从而与原 `||` 的 falsy 判定一致。
 */
function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/** `executionState` 的返回值形状（1.0 是匿名对象字面量）。 */
export interface ExecutionState {
  key: string;
  label: string;
}

/** `memoryLayerToolMetric` 的返回值形状。 */
export interface MemoryLayerToolMetric {
  calls: number;
  okRate: number | null;
}

// ── 1.0 :323-337 基础格式化 ─────────────────────────────────────────────

/** 1.0 `:323` */
export function esc(value: unknown): string {
  return String(value ?? "");
}

/** 1.0 `:324` */
export function modelName(run: unknown): string {
  const r = dict(run);
  const profile = str(r.model_profile);
  const savedName = str(r.model_name);
  if (savedName && savedName !== "__current__") return savedName;
  if (profile === "__current__" || savedName === "__current__") {
    const endpointModel = str(r.endpoint_model);
    if (endpointModel) return endpointModel;

    // 旧的运行记录没有保存 endpoint_model；复用相册的 scope 名仍保留了实际模型后缀。
    const albumId = str(r.album_id);
    const scopeName = str(r.scope_name);
    const marker = albumId ? `${albumId}-` : "";
    const markerIndex = marker ? scopeName.lastIndexOf(marker) : -1;
    if (markerIndex >= 0) {
      const inferred = scopeName.slice(markerIndex + marker.length).trim();
      if (inferred && inferred !== "__current__" && inferred !== "current") return inferred;
    }
    return "current";
  }
  return profile || savedName || str(r.profile) || "unknown";
}

/** 1.0 `:325` */
export function albumName(run: unknown): string {
  const r = dict(run);
  return str(r.scope_name) || str(r.album_id) || str(r.qa_name) || "album";
}

/** 1.0 `:326` */
export function qaName(run: unknown): string {
  const r = dict(run);
  return str(r.qa_set) || str(r.qa_name) || "qa";
}

/** 1.0 `:327` —— 无值回 `-`（不是空串）。 */
export function fmtDate(value: unknown): string {
  if (!value) return "-";
  return new Date(value as string | number | Date).toLocaleString("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** 1.0 `:328-333` —— 未开始回 `-`；不足 1 分钟 `${s}s`，否则 `${m}m${s}s`。 */
export function duration(run: unknown): string {
  const r = dict(run);
  if (!r.started_at) return "-";
  const end = r.finished_at ? new Date(r.finished_at as string | number | Date) : new Date();
  const startMs = new Date(r.started_at as string | number | Date).getTime();
  const seconds = Math.max(0, Math.round((end.getTime() - startMs) / 1000));
  return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m${seconds % 60}s`;
}

/** 1.0 `:334` —— `null`/`undefined` 回 `-`（0 走 `0ms`）。 */
export function fmtMs(value: unknown): string {
  if (value == null) return "-";
  const ms = Number(value);
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms.toFixed(0)}ms`;
}

/** 1.0 `:335` */
export function fmtPct(value: unknown): string {
  if (value == null) return "-";
  return `${(Number(value) * 100).toFixed(1)}%`;
}

/** 1.0 `:336` —— 非有限数也回 `-`。 */
export function fmtTokens(value: unknown): string {
  if (value == null || !Number.isFinite(Number(value))) return "-";
  return `${Math.round(Number(value)).toLocaleString("en-US")} token`;
}

/** 1.0 `:337` —— 严格相等：字符串 `"2"` 落 `score-none`。 */
export function scoreClass(score: unknown): string {
  return score === 2 ? "score-2" : score === 1 ? "score-1" : score === 0 ? "score-0" : "score-none";
}

/** 1.0 `:338` —— 映射未命中时回**原值**（不是 `unknown`）。 */
export function statusLabel(status: unknown): string {
  const labels: Record<string, string> = {
    done: "完成",
    running: "进行中",
    pending: "等待",
    cancelling: "停止中",
    failed: "失败",
    completed: "完成",
    completed_with_errors: "完成但有错误",
    interrupted: "中断",
    cancelled: "已取消",
    partial: "部分完成",
    stalled: "已停滞",
    not_run: "未执行",
    skipped: "不适用",
  };
  const key = typeof status === "string" ? status : "";
  return labels[key] || key || "等待";
}

// ── 1.0 :553-623 结果卡片（纯格式的那几个） ──────────────────────────────

/** 1.0 `:556-558` —— 映射未命中时回 `未记录`（**不**回原值；与 `statusLabel` 不同）。 */
export function actionLabel(value: unknown): string {
  const labels: Record<string, string> = { answer: "回答", refuse: "拒答", clarify: "澄清", none: "无有效行为" };
  return labels[str(value)] || "未记录";
}

/** 1.0 `:559-565` */
export function evidenceScoreLabel(judge: unknown): string {
  const j = dict(judge);
  const score = j.score;
  if (score === 2) return "2 分：媒体证据支持回答";
  if (score === 1) return "1 分：媒体证据部分支持";
  if (score === 0) return "0 分：媒体证据无法支持";
  return j.reason === "not_applicable" ? "不适用" : j.reason === "no_answer" ? "无回答，未评分" : "未记录";
}

/** 1.0 `:580-597` */
export function executionState(item: unknown): ExecutionState {
  const i = dict(item);
  const stability = dict(i.agent_stability);
  const guard = dict(i.guard_debug);
  const status = String(i.agent_status || guard.status || "").toLowerCase();
  const termination = String(i.termination_reason || guard.termination_reason || "").toLowerCase();
  const turns = Array.isArray(i.runtime_turns) ? i.runtime_turns : [];
  const outcome = String(i.turn_outcome || dict(turns[turns.length - 1]).turn_outcome || "").toLowerCase();
  const failure = [status, termination, outcome].some((value) =>
    /error|failed|failure|timeout|cancel|blocked|parse_failure|model_error/.test(value));
  const partial = [status, termination, outcome].some((value) =>
    /partial|limit|budget|incomplete|tool_call_limit|step_limit/.test(value));
  if (failure) return { key: "failed", label: "执行失败" };
  if (partial) return { key: "partial", label: "部分完成" };
  if (outcome === "final_answer" || ["complete", "completed", "done", "success"].includes(status)
      || ["complete", "completed", "done", "success"].includes(termination)
      || stability.completed_within_steps === true) {
    return { key: "complete", label: "已完成" };
  }
  if (status || termination || outcome || stability.completed_within_steps === false) return { key: "unknown", label: "状态待确认" };
  return { key: "unknown", label: "未记录" };
}

/** 1.0 `:598` —— 返回 CSS 类名。 */
export function executionStateClass(item: unknown): string {
  return `status-${executionState(item).key}`;
}

/** 1.0 `:599-601` */
export function completionLabel(item: unknown): string {
  return executionState(item).label;
}

/** 1.0 `:602-606` —— 无 `expected_action` 回 `未标注`。 */
export function taskDecisionLabel(item: unknown): string {
  const judge = dict(dict(item).task_judge);
  if (!judge.expected_action) return "未标注";
  return `期望${actionLabel(judge.expected_action)}，实际${actionLabel(judge.actual_action)}`;
}

// ── 1.0 :728-784 轨迹 / 阶段 / 内存 ─────────────────────────────────────

/** 1.0 `:728-731` —— 三个分支各有各的字面量，未命中回 `按执行轨迹绑定`。 */
export function toolBindingLabel(trace: unknown): string {
  const t = dict(trace);
  if (t.round_binding_source === "step_id") return "按步骤 ID 精确绑定";
  return t.round_binding_source === "inferred_single_model_call" ? "单轮数据推断归属" : "按执行轨迹绑定";
}

/** 1.0 `:732-736` —— 无后端时回**空串**（不是 `-`）。 */
export function retrievalBackendLabel(trace: unknown): string {
  const channels = dict(dict(dict(trace).retrieval_timing).channels);
  const backends = [...new Set(Object.values(channels).map((channel) => dict(channel).backend).filter(Boolean))];
  return backends.length ? backends.join("/") : "";
}

/** 1.0 `:737-740` */
export function retrievalBackendDegraded(trace: unknown): boolean {
  const label = retrievalBackendLabel(trace);
  return Boolean(label) && label.split("/").some((backend) => backend !== "qdrant");
}

/**
 * 1.0 `:741-749`。
 *
 * 1.0 读组件 computed `activeRejudge`（`activeRun.rejudge || null`）。纯函数层没有组件状态，
 * 把它显式化成第二个参数 `task` —— 判定逻辑逐行照搬，只是把隐式依赖显式化。
 */
export function judgeRoundState(item: unknown, task: unknown): string {
  if (!task) return "normal";
  const judge = dict(dict(item).judge);
  const t = dict(task);
  if (judge.rejudge_id !== t.rejudge_id) return t.status === "running" ? "pending" : "normal";
  if (t.status !== "running" && (judge.status === "pending" || judge.status === "running")) return "interrupted";
  if (judge.status === "pending" || judge.status === "running" || judge.status === "failed") return judge.status;
  return "updated";
}

/** 1.0 `:750-758` —— 同样把 `activeRejudge` 显式化为 `task`。 */
export function judgeScoreLabel(item: unknown, task: unknown): string {
  const judge = dict(dict(item).judge);
  if (judge.consistency_status === "inconsistent") return "评分异常";
  const state = judgeRoundState(item, task);
  if (state === "pending") return "待重新评分";
  if (state === "running") return "评分中";
  if (state === "failed") return "评分失败";
  if (state === "interrupted") return "本轮未完成";
  return judge.score == null ? "未评分" : `${String(judge.score)}分`;
}

/** 1.0 `:759-768` —— 取不到就回 `null`（不是 0）。 */
export function phaseSeconds(phase: unknown, preferredKey = "total_seconds"): number | null {
  if (!phase) return null;
  const p = dict(phase);
  const value = p[preferredKey];
  const preferred = typeof value === "number" ? value : Number.NaN;
  if (Number.isFinite(preferred) && preferred > 0) return preferred;
  if (p.started_at && p.finished_at) {
    const elapsed = (new Date(p.finished_at as string).getTime() - new Date(p.started_at as string).getTime()) / 1000;
    if (Number.isFinite(elapsed) && elapsed >= 0) return elapsed;
  }
  return Number.isFinite(preferred) && preferred >= 0 ? preferred : null;
}

/** 1.0 `:769-776` —— `<1ms` / `Nms` / `Ns` 三档，`0` 落 `<1ms`。 */
export function fmtSeconds(value: unknown): string {
  if (value == null || !Number.isFinite(Number(value))) return "-";
  const seconds = Number(value);
  if (seconds < 0.001) return "<1ms";
  if (seconds < 0.1) return `${Math.round(seconds * 1000)}ms`;
  if (seconds < 10) return `${seconds.toFixed(2).replace(/0+$/, "").replace(/\.$/, "")}s`;
  return `${seconds.toFixed(1).replace(/\.0$/, "")}s`;
}

/** 1.0 `:777-780` */
export function fmtNumber(value: unknown, suffix = "", digits = 1): string {
  if (value == null || !Number.isFinite(Number(value))) return "-";
  return `${Number(value).toFixed(digits).replace(/\.0$/, "")}${suffix}`;
}

/** 1.0 `:781-784` —— 输入按 MiB 给，输出 GiB、恒两位小数。 */
export function fmtMemory(value: unknown): string {
  if (value == null || !Number.isFinite(Number(value))) return "-";
  return `${(Number(value) / 1024).toFixed(2)} GiB`;
}

// ── 1.0 :1402-1423 汇总 ─────────────────────────────────────────────────

/** 1.0 `:1402-1405` —— 无有效值回 `null`。 */
export function average(values: readonly unknown[]): number | null {
  const valid = values.map(Number).filter(Number.isFinite);
  return valid.length ? valid.reduce((sum, value) => sum + value, 0) / valid.length : null;
}

/** 1.0 `:1421-1423` */
export function fmtTokenRate(value: unknown): string {
  return value == null || !Number.isFinite(Number(value)) ? "-" : `${Number(value).toFixed(1)} token/s`;
}

// ── 1.0 :1424-1443 模型调用 ─────────────────────────────────────────────

/** 1.0 `:1424-1429` */
export function callStatus(call: unknown): string {
  const c = dict(call);
  if (c.status === "context_budget_exceeded") return "调用前拦截";
  if (c.status === "error") return "调用失败";
  if (c.status && c.status !== "complete") return String(c.status);
  return c.streamed === true ? "成功 · 流式" : c.streamed === false ? "成功 · 非流式" : "未记录";
}

/** 1.0 `:1430-1439` */
export function callOutcome(call: unknown): string {
  const c = dict(call);
  const outcome = c.turn_outcome;
  if (outcome === "tool_call") {
    // 1.0 走 `callObservation(call).relatedTool`，那个值由 `observation.related_tool || "-"` 构成，永不为空
    const relatedTool = str(dict(c.call_observation).related_tool) || "-";
    return `本轮结果：调用工具 ${str(c.next_step) || relatedTool}`.trim();
  }
  if (outcome === "final_answer") return "本轮结果：正常回答结束";
  if (outcome === "parse_failure") return "本轮结果：JSON 解析失败";
  if (outcome === "model_error") return "本轮结果：模型请求失败";
  if (outcome === "context_blocked") return "本轮结果：上下文或 token 预检拦截";
  if (outcome === "step_limit") return "本轮结果：达到最大执行步数";
  return "本轮结果：历史记录未保存";
}

/** 1.0 `:1440-1443` —— 无 `turn_outcome` 回**空串**。 */
export function callOutcomeClass(call: unknown): string {
  const outcome = dict(call).turn_outcome;
  return (["parse_failure", "model_error", "context_blocked", "step_limit"] as unknown[]).includes(outcome)
    ? "outcome-failed"
    : outcome
      ? "outcome-ok"
      : "";
}

// ── 1.0 :1444-1470 调用类型 ─────────────────────────────────────────────

/** 1.0 `:1444-1446` —— 兜底回 `legacy`。 */
export function callType(call: unknown): string {
  const c = dict(call);
  return str(c.call_type) || str(dict(c.call_observation).kind) || "legacy";
}

/** 1.0 `:1447-1458` —— `call_observation.label` 优先；映射未命中回 `call_type` 原值。 */
export function callTypeLabel(call: unknown): string {
  const c = dict(call);
  const observation = dict(c.call_observation);
  if (observation.label) return String(observation.label);
  const labels: Record<string, string> = {
    planner: "Agent 2.0 目标分解与规划",
    agent: "Agent 决策 / 回答",
    recovery: "Agent 恢复调用",
    writer: "最终回答重写",
    faithfulness_judge: "L2 事实一致性检查",
    tool_internal: "工具内部模型调用",
    legacy: "历史模型调用",
  };
  return labels[callType(call)] || str(c.call_type);
}

/** 1.0 `:1459-1470` —— `call_observation.purpose` 优先；兜底是固定文案（不是原值）。 */
export function callTypeDescription(call: unknown): string {
  const c = dict(call);
  const observation = dict(c.call_observation);
  if (observation.purpose) return String(observation.purpose);
  const descriptions: Record<string, string> = {
    planner: "解析用户目标并声明最小充分证据需求（TaskState/EvidenceLedger）",
    agent: "模型选择工具或直接生成回答",
    recovery: "由解析失败、重复工具或 Guard 纠正触发",
    writer: "仅按受控事实重写最终回答，不调用工具",
    faithfulness_judge: "检查回答与工具事实是否一致，不调用工具",
    tool_internal: "工具执行过程中调用模型完成视觉识别或 OCR",
    legacy: "旧记录未保存调用类型",
  };
  return descriptions[callType(call)] || "后端记录的模型调用类型";
}

// ── 1.0 :1384-1398 轮次 ─────────────────────────────────────────────────

/** 1.0 `:1384-1395` —— 先做 token 预检 502 的正则特判，再查映射表，未命中回原值。 */
export function turnTerminationLabel(turn: unknown): string {
  const reason = String(dict(turn).termination_reason || "");
  if (/token budget preflight failed|tokenize-current|502 Bad Gateway|tokenize.*502/i.test(reason)) return "上下文 token 预检失败（tokenize 接口 502）";
  const labels: Record<string, string> = {
    complete: "正常完成",
    parse_failure: "JSON 解析失败",
    model_error: "模型请求失败",
    context_blocked: "上下文或 token 预检拦截",
    tool_call_limit: "达到最大工具调用步数",
    step_limit: "达到最大执行步数",
  };
  return labels[reason] || reason || "未记录";
}

/** 1.0 `:1396-1398` */
export function turnCompletionLabel(turn: unknown): string {
  return executionState(turn).label;
}

// ── 1.0 :1806-1809 记忆层级工具指标 ─────────────────────────────────────

/** 1.0 `:1806-1809` —— `ok_rate` 缺省是 `null`，但 `0` 会原样保留。 */
export function memoryLayerToolMetric(summary: unknown, name: string): MemoryLayerToolMetric {
  const metric = dict(dict(dict(summary).tool_performance)[name]);
  return {
    calls: Number(metric.calls || 0),
    okRate: metric.ok_rate == null ? null : Number(metric.ok_rate),
  };
}

// ── 1.0 :2038-2054 关键帧 ───────────────────────────────────────────────

/** 1.0 `:2038-2042` —— 带正负号、两位小数、末缀 ` / 2`。 */
export function keyframeDeltaLabel(value: unknown): string {
  if (value == null || !Number.isFinite(Number(value))) return "-";
  const number = Number(value);
  return `${number >= 0 ? "+" : ""}${number.toFixed(2)} / 2`;
}

/** 1.0 `:2043-2045` —— 空对象回 `-`。 */
export function keyframeReasonLabel(value: unknown): string {
  return Object.entries(dict(value))
    .map(([key, count]) => `${key}×${String(count)}`)
    .join(" · ") || "-";
}
