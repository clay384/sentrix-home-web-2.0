/**
 * 2.0 的完整 Agent 轨迹访问器。
 *
 * 数据来源是主 API 回合终态载荷里的 `trace.debug_calls`（评测侧 `include_debug=true`
 * 才会写）：每次模型调用一条，带实际发送的 `messages` / `tools` 与原始 `response`。
 * PhotoBench 把它原样存进 `runtime_turns[i].trace_bundle`，所以这里不构造任何新事实，
 * 只负责把「模型调用」和「它发起的工具调用」按发生顺序拼回一条时间线。
 *
 * 老 run（改动前）没有 `debug_calls`；所有函数都保留 1.0 `debug_trace` 的回退，
 * 缺数据时返回空表/`null`，由渲染层显示「未记录」，**不推断**。
 */

export type Dict = Record<string, unknown>;

export interface TraceTimelineEntry {
  /** 时间线条目的类型。 */
  kind: "planner" | "model" | "judge" | "tool" | "answer";
  key: string;
  /** 模型调用（planner / model / judge）。 */
  call?: Dict;
  /** 工具调用（tool_trace 的一行）。 */
  tool?: Dict;
  /** 最终回答。 */
  answer?: string;
  attachments?: unknown[];
}

function dict(value: unknown): Dict {
  return typeof value === "object" && value !== null ? (value as Dict) : {};
}

function objectList(value: unknown): Dict[] {
  return Array.isArray(value)
    ? value.filter((item): item is Dict => typeof item === "object" && item !== null)
    : [];
}

/** `runtime_turns` 的每一轮；非数组回空。 */
export function runtimeTurns(item: unknown): Dict[] {
  return objectList(dict(item).runtime_turns);
}

/** 一轮里的完整模型调用；老 run 没有这一格时回空表。 */
export function turnDebugCalls(turn: unknown): Dict[] {
  return objectList(dict(dict(turn).trace_bundle).debug_calls);
}

/** 一轮里的原始进度事件（2.0 的 `trace`），用于「运行时事件」清单。 */
export function turnProgressEvents(turn: unknown): Dict[] {
  return objectList(dict(turn).trace);
}

/** 整题摊平后的模型调用，附带它所属的轮次号。 */
export function itemDebugCalls(item: unknown): Dict[] {
  return runtimeTurns(item).flatMap((turn) =>
    turnDebugCalls(turn).map((call) => ({ ...call, conversation_turn: Number(turn.index) || 0 })),
  );
}

/** 一次 Agent Loop 调用对应的完整模型输入/输出；按轮 + `step_id` 精确匹配。 */
export function debugCallForCall(item: unknown, call: unknown): Dict | null {
  const c = dict(call);
  const turnIndex = Number(c.conversation_turn) || 0;
  const stepId = String(c.step_id || "");
  if (!stepId) return null;
  const turn =
    runtimeTurns(item).find((entry) => Number(entry.index) === turnIndex) ?? runtimeTurns(item)[turnIndex];
  if (!turn) return null;
  const wantedType = String(c.call_type || "agent");
  return (
    turnDebugCalls(turn).find(
      (entry) =>
        String(entry.step_id || "") === stepId &&
        String(entry.call_type || "agent") === wantedType,
    ) ?? null
  );
}

/**
 * 进度事件里的一个步骤对应的模型调用。
 *
 * - `plan`：规划器调用（取最后一次成功尝试；重试用 `planner_calls` 完整展示）。
 * - `model_action`：发起该工具调用的那次 Agent 调用（`parent_step_id`）。
 * - `judge`：评审调用（后端在 payload 里带 `call_id`）。
 */
export function debugCallForStep(turn: unknown, step: unknown): Dict | null {
  const s = dict(step);
  const calls = turnDebugCalls(turn);
  if (!calls.length) return null;
  const type = String(s.type || s.kind || "");
  if (type === "plan") {
    const planners = calls.filter((call) => String(call.call_type) === "planner");
    return planners[planners.length - 1] ?? null;
  }
  if (type === "model_action") {
    const stepId = String(s.parent_step_id || "");
    return (
      calls.find(
        (call) => String(call.call_type) === "agent" && String(call.step_id || "") === stepId,
      ) ?? null
    );
  }
  if (type === "judge") {
    const callId = String(s.call_id || "");
    return (
      calls.find(
        (call) =>
          String(call.call_type) === "faithfulness_judge" &&
          (!callId || String(call.step_id || "") === callId),
      ) ?? null
    );
  }
  return null;
}

/** 一个模型调用发起的工具调用（按 `parent_step_id` 绑定）。 */
export function toolsForDebugCall(item: unknown, turnIndex: number, call: unknown): Dict[] {
  const stepId = String(dict(call).step_id || "");
  if (!stepId) return [];
  return objectList(dict(item).tool_trace).filter(
    (trace) =>
      Number(trace.conversation_turn) === turnIndex &&
      String(trace.parent_step_id || "") === stepId,
  );
}

/** 规划器重试的完整列表（`plan` 步骤上要能看见每一次尝试）。 */
export function plannerCallsForTurn(turn: unknown): Dict[] {
  return turnDebugCalls(turn).filter((call) => String(call.call_type) === "planner");
}

/**
 * 一轮的完整时间线：planner → 每次模型调用 → 它发起的工具 → judge → 最终回答。
 *
 * 老 run 没有 `debug_calls` 时回退到 `debug_trace` 的 planner/model/judge/tool 步；
 * 两者都没有就只留用户问题与最终回答，让面板如实显示「模型输入输出未记录」。
 */
export function turnTimeline(item: unknown, turnIndex: number): TraceTimelineEntry[] {
  const turns = runtimeTurns(item);
  const turn = turns.find((entry) => Number(entry.index) === turnIndex) ?? turns[turnIndex];
  if (!turn) return [];
  const calls = turnDebugCalls(turn);
  const entries: TraceTimelineEntry[] = [];
  if (calls.length) {
    for (const call of calls) {
      const callType = String(call.call_type || "agent");
      const key = `call-${turnIndex}-${String(call.seq ?? entries.length)}-${String(call.step_id || "")}`;
      if (callType === "planner") {
        entries.push({ kind: "planner", key, call });
      } else if (callType === "faithfulness_judge") {
        entries.push({ kind: "judge", key, call });
      } else {
        entries.push({ kind: "model", key, call });
        for (const [toolIndex, tool] of toolsForDebugCall(item, turnIndex, call).entries()) {
          entries.push({ kind: "tool", key: `${key}-tool-${toolIndex}`, tool });
        }
      }
    }
  } else {
    const steps = objectList(dict(turn).debug_trace);
    steps.forEach((step, index) => {
      const type = String(step.type || step.kind || "");
      const key = `legacy-${turnIndex}-${index}`;
      if (type === "planner") entries.push({ kind: "planner", key, call: step });
      else if (type === "judge") entries.push({ kind: "judge", key, call: step });
      else if (type === "tool" || type === "tool_result") entries.push({ kind: "tool", key, tool: step });
      else if (type === "model" || type === "model_action") entries.push({ kind: "model", key, call: step });
    });
  }
  entries.push({
    kind: "answer",
    key: `answer-${turnIndex}`,
    answer: String(turn.answer || ""),
    attachments: Array.isArray(turn.attachments) ? turn.attachments : [],
  });
  return entries;
}

// ---------------------------------------------------------------------------
// 模型调用字段的安全访问
// ---------------------------------------------------------------------------

export interface ChatMessageView {
  role: string;
  content: string;
  toolCallId: string;
  toolCalls: Dict[];
}

export function callMessages(call: unknown): ChatMessageView[] {
  const messages = dict(call).messages;
  if (!Array.isArray(messages)) return [];
  return messages
    .filter((message): message is Dict => typeof message === "object" && message !== null)
    .map((message) => ({
      role: String(message.role || ""),
      content: typeof message.content === "string" ? message.content : JSON.stringify(message.content, null, 2),
      toolCallId: String(message.tool_call_id || ""),
      toolCalls: objectList(message.tool_calls),
    }));
}

export function callTools(call: unknown): Dict[] {
  return objectList(dict(call).tools);
}

export function callResponse(call: unknown): Dict {
  return dict(dict(call).response);
}

export function callContent(call: unknown): string {
  const content = callResponse(call).content;
  return typeof content === "string" ? content : "";
}

export function callThought(call: unknown): string {
  const thought = callResponse(call).thought;
  return typeof thought === "string" ? thought : "";
}

export function callToolCalls(call: unknown): Dict[] {
  return objectList(callResponse(call).tool_calls);
}

export function callLabel(callType: unknown): string {
  const labels: Record<string, string> = {
    planner: "规划器",
    agent: "主 Agent",
    faithfulness_judge: "语义评审",
  };
  const key = String(callType || "");
  return labels[key] || key || "模型调用";
}

/** 模型调用的状态：失败时显示错误，而不是把整个调用藏掉。 */
export function callStatus(call: unknown): string {
  const c = dict(call);
  if (c.error) return `失败 · ${String(c.error)}`;
  return String(c.status || "已记录");
}
