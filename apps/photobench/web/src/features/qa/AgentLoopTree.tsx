/**
 * Agent Loop 调用树。移植 1.0 `App.vue:3554-3619`——Vue 模板里最长的一段：
 * `agent-loop-groups` → `call-tree` → `call-node` → `tool-node` / `debug-inline`
 * 三层嵌套 `<details>`，末尾还有「未绑定模型轮次的工具序列」。
 *
 * **全部解析都用 `lib/trace.ts` 的既有实现，不在这里重写**：
 * `agentLoopGroups` / `showAgentLoopGroupHeaders` / `toolsForGroupedCall` /
 * `callAgentLoopTiming` / `callLatencySegments` / `debugStepForCallInGroup` /
 * `debugToolsForCall` / `debugPromptAnnotations` / `unboundTools`（`qaShared.ts`）等。
 */
import type { RunItemDetail } from "../../api/types.ts";
import { debugCallForCall } from "../../lib/agentTrace.ts";
import {
  callOutcome,
  callOutcomeClass,
  callStatus,
  callType,
  callTypeDescription,
  callTypeLabel,
  esc,
  executionStateClass,
  fmtMs,
  fmtTokenRate,
  retrievalBackendDegraded,
  retrievalBackendLabel,
  toolBindingLabel,
  turnCompletionLabel,
  turnTerminationLabel,
} from "../../lib/format.ts";
import {
  agentLoopGroups,
  callBudget,
  callAgentLoopTiming,
  callLatencySegments,
  callObservation,
  debugPromptAnnotations,
  debugStepForCallInGroup,
  debugToolsForCall,
  latencySegmentTitle,
  noToolLabel,
  showAgentLoopGroupHeaders,
  showToolBranch,
  toolDurationMs,
  toolLatencySegments,
  toolsForGroupedCall,
  turnRecoveryCount,
} from "../../lib/trace.ts";
import type { AgentLoopGroup } from "../../lib/trace.ts";

import { ModelCallView } from "./ModelCallView.tsx";
import { segmentStyle, toolStatusLabel, unboundTools } from "./qaShared.ts";

type Dict = Record<string, unknown>;

function dict(value: unknown): Dict {
  return typeof value === "object" && value !== null ? (value as Dict) : {};
}

/** 1.0 `trace.latency_s == null ? "-" : fmtMs(Number(trace.latency_s) * 1000)`。 */
function toolLatencyLabel(trace: Dict): string {
  return trace.latency_s == null ? "-" : fmtMs(Number(trace.latency_s) * 1000);
}

export interface AgentLoopTreeProps {
  detail: RunItemDetail;
  /** 1.0 调用摘要里 `call.model || modelName(activeRun)` 的后半段。 */
  modelLabel: string;
}

export function AgentLoopTree({ detail, modelLabel }: AgentLoopTreeProps) {
  const groups = agentLoopGroups(detail);
  const showHeaders = showAgentLoopGroupHeaders(detail);
  const unbound = unboundTools(detail);
  const hasCalls = groups.some((group) => group.calls.length);

  return (
    <>
      {hasCalls ? (
        <div className="agent-loop-groups">
          {groups.map((group) => {
            const turn = group.turn;
            return (
              <section key={group.turnIndex} className="agent-loop-group">
                {showHeaders && (
                  <header className="agent-loop-turn-head">
                    <div>
                      <strong>第 {group.turnIndex + 1} 轮 Agent Loop</strong>
                      <span>{esc(turn.message) || esc(detail.question)}</span>
                    </div>
                    <span>
                      {group.calls.length} 次模型调用 ·{" "}
                      {turn.answer ? "已回答" : esc(turn.termination_reason) || "未完成"}
                    </span>
                  </header>
                )}
                {group.calls.length ? (
                  <div className="call-tree">
                    {group.calls.map((call, callIndex) => (
                      <CallNode
                        key={`${group.turnIndex}-${String(call.step_id || call._globalCallIndex || callIndex)}`}
                        detail={detail}
                        group={group}
                        call={call}
                        callIndex={callIndex}
                        modelLabel={modelLabel}
                      />
                    ))}
                  </div>
                ) : (
                  <p className="qa-performance-empty">本轮没有保存模型调用性能或失败轨迹。</p>
                )}
                {Boolean(turn.agent_status || turn.termination_reason || turn.turn_outcome) && (
                  <div className={`turn-status-strip ${executionStateClass(turn)}`}>
                    <strong>第 {group.turnIndex + 1} 轮状态</strong>
                    <span className="status-pill">{turnCompletionLabel(turn)}</span>
                    <span>
                      运行状态 <b>{esc(turn.agent_status) || "未记录"}</b>
                    </span>
                    <span>
                      终止原因 <b>{turnTerminationLabel(turn)}</b>
                    </span>
                    <span>
                      JSON 解析 <b>{esc(turn.parse_status) || "未记录"}</b>
                    </span>
                    <span>
                      恢复 <b>{turnRecoveryCount(group)} 次</b>
                    </span>
                  </div>
                )}
              </section>
            );
          })}
        </div>
      ) : (
        <p className="qa-performance-empty">该历史结果未记录主模型调用性能或失败轨迹。</p>
      )}
      {unbound.length > 0 && (
        <details className="call-node unbound-tools">
          <summary>
            <strong>未绑定模型轮次的工具序列</strong>
            <span>{unbound.length} 次 · 历史数据未保存精确轮次关系</span>
          </summary>
          <div className="call-node-body tool-tree">
            {unbound.map((trace, toolIndex) => (
              <details key={toolIndex} className="tool-node">
                <summary>
                  <strong>
                    #{toolIndex + 1} {esc(trace.tool) || "未知工具"}
                  </strong>
                  <span>{toolStatusLabel(trace)}</span>
                  <span>总耗时 {toolLatencyLabel(trace)}</span>
                  {retrievalBackendLabel(trace) ? (
                    <span
                      className={`retrieval-backend${retrievalBackendDegraded(trace) ? " degraded" : ""}`}
                    >
                      检索后端 {retrievalBackendLabel(trace)}
                    </span>
                  ) : null}
                </summary>
              </details>
            ))}
          </div>
        </details>
      )}
    </>
  );
}

interface CallNodeProps {
  detail: RunItemDetail;
  group: AgentLoopGroup;
  call: Dict;
  callIndex: number;
  modelLabel: string;
}

function CallNode({ detail, group, call, callIndex, modelLabel }: CallNodeProps) {
  const type = callType(call);
  const timing = callAgentLoopTiming(detail, call);
  const segments = callLatencySegments(detail, call);
  const observation = callObservation(call);
  const step = debugStepForCallInGroup(detail, group, call);
  // 2.0 的完整调用（messages / tools / 原始回复）按 step_id 挂在同一轮里；
  // 老 run 没有它，回退到 1.0 的 debug_trace 投影。
  const modelCall = debugCallForCall(detail, call);
  const showTools = showToolBranch(call);
  const tools = toolsForGroupedCall(detail, call);
  const toolDebugs = showTools ? debugToolsForCall(detail, group, call) : [];

  return (
    <details
      className={["call-node", `call-type-${type}`, call._traceOnly ? "trace-only-call" : ""]
        .filter(Boolean)
        .join(" ")}
    >
      <summary>
        <span className="call-round">{callIndex + 1}</span>
        <strong>{callTypeLabel(call)}</strong>
        {["agent", "recovery"].includes(type) && (
          <span className={`call-outcome ${callOutcomeClass(call)}`}>{callOutcome(call)}</span>
        )}
        <span>
          {esc(call.role) || "-"} · {esc(call.model) || modelLabel}
        </span>
        <span>TTFT {fmtMs(call.ttft_ms)}</span>
        <span>Agent Loop 总时延 {fmtMs(timing.totalMs)}</span>
        <span>模型 {fmtMs(call.total_ms)}</span>
        <span>
          Token {esc(call.preflight_prompt_tokens ?? call.prompt_tokens ?? "-")} /{" "}
          {esc(call.completion_tokens ?? "-")}
        </span>
        <span>{fmtTokenRate(call.tokens_per_second)}</span>
        <span className={`stream-state${call.streamed === true ? " streamed" : ""}`}>{callStatus(call)}</span>
      </summary>
      <div className="call-node-body">
        {segments.length > 0 && (
          <div className="latency-composition call-latency-composition">
            <div className="latency-composition-head">
              <strong>Agent Loop 时延组成</strong>
              <span>{fmtMs(timing.totalMs)}</span>
            </div>
            <div className="latency-bar" role="img" aria-label="Agent Loop 时延组成">
              {segments.map((segment, segmentIndex) => (
                <span
                  key={`${segment.kind}-${segmentIndex}`}
                  className={`latency-segment latency-segment-${segment.kind}`}
                  style={segmentStyle(segment, segments)}
                  data-tooltip={latencySegmentTitle(segment)}
                >
                  <b>{fmtMs(segment.ms)}</b>
                </span>
              ))}
            </div>
            <div className="latency-segment-legend">
              {segments.map((segment, segmentIndex) => (
                <span key={`${segment.kind}-legend-${segmentIndex}`}>
                  <i className={`latency-dot latency-dot-${segment.kind}`}></i>
                  {segment.label} {fmtMs(segment.ms)}
                </span>
              ))}
            </div>
          </div>
        )}
        <div className="call-purpose-grid">
          <span>
            <small>用途</small>
            <b>{callTypeDescription(call)}</b>
          </span>
          <span>
            <small>触发</small>
            <b>{observation.trigger}</b>
          </span>
          <span>
            <small>结果</small>
            <b>{observation.outcome}</b>
          </span>
          <span>
            <small>记录来源</small>
            <b>{observation.source}</b>
          </span>
        </div>
        <div className="call-budget-line">
          <span>请求预算</span>
          <b>{callBudget(call)}</b>
          {call.step_id ? <span>步骤 {esc(call.step_id)}</span> : null}
          {observation.relatedTool !== "-" ? <span>关联工具 {observation.relatedTool}</span> : null}
          {observation.parentStep !== "-" ? <span>父步骤 {observation.parentStep}</span> : null}
        </div>
        {modelCall ? (
          <ModelCallView call={modelCall} title="模型输入 / 原始输出" />
        ) : step ? (
          <DebugInline step={step} annotations={debugPromptAnnotations(detail, group, call)} />
        ) : null}
        {showTools && tools.length ? (
          <div className="tool-tree">
            {tools.map((trace, toolIndex) => (
              <ToolNode key={toolIndex} trace={trace} debug={toolDebugs[toolIndex]} />
            ))}
          </div>
        ) : showTools ? (
          <p className="qa-performance-empty">{noToolLabel(call)}</p>
        ) : null}
      </div>
    </details>
  );
}

/** 1.0 `:3575-3587`：`debugStepForCallInGroup` 命中时的「完整输入 / 输出」。 */
function DebugInline({ step, annotations }: { step: Dict; annotations: unknown[] }) {
  const isJudge = step.type === "judge" || step.call_type === "faithfulness_judge";
  const debug = dict(step.debug);
  return (
    <details className="debug-inline">
      <summary>完整输入 / 输出</summary>
      {isJudge ? (
        <div>
          <p className="muted small">评判结论</p>
          <pre>{JSON.stringify({ faithful: step.faithful, problems: step.problems }, null, 2)}</pre>
          {debug.prompt ? <p className="muted small">评判提示词</p> : null}
          {debug.prompt ? <pre>{JSON.stringify(debug.prompt, null, 2)}</pre> : null}
          {debug.raw ? <p className="muted small">评判原始回答</p> : null}
          {debug.raw ? <pre>{esc(debug.raw)}</pre> : null}
        </div>
      ) : (
        <div>
          {annotations.length ? <p className="muted small">内部控制消息（不是用户原话）</p> : null}
          {annotations.length ? <pre>{JSON.stringify(annotations, null, 2)}</pre> : null}
          <p className="muted small">完整提示词</p>
          <pre>{JSON.stringify(step.prompt, null, 2)}</pre>
          <p className="muted small">模型原始回答</p>
          <pre>{esc(step.raw_full) || esc(step.raw)}</pre>
        </div>
      )}
    </details>
  );
}

/** 1.0 `:3589-3603`：一条工具 trace。 */
function ToolNode({ trace, debug }: { trace: Dict; debug: Dict | undefined }) {
  const segments = toolLatencySegments(trace);
  const backend = retrievalBackendLabel(trace);
  return (
    <details className="tool-node">
      <summary>
        <strong>{esc(trace.tool) || "未知工具"}</strong>
        <span>{toolStatusLabel(trace)}</span>
        <span>总耗时 {toolLatencyLabel(trace)}</span>
        <span className="binding-source">{toolBindingLabel(trace)}</span>
        {backend ? (
          <span className={`retrieval-backend${retrievalBackendDegraded(trace) ? " degraded" : ""}`}>
            检索后端 {backend}
          </span>
        ) : null}
      </summary>
      {segments.length > 0 && (
        <div className="latency-composition nested-tool-latency">
          <div className="latency-composition-head">
            <strong>工具内部时延组成</strong>
            <span>{fmtMs(toolDurationMs(trace))}</span>
          </div>
          <div className="latency-bar" role="img" aria-label="工具内部时延组成">
            {segments.map((segment, segmentIndex) => (
              <span
                key={`${segment.kind}-${segmentIndex}`}
                className={`latency-segment latency-segment-${segment.kind}`}
                style={segmentStyle(segment, segments)}
                data-tooltip={`${segment.label} · ${fmtMs(segment.ms)}`}
              >
                <b>{fmtMs(segment.ms)}</b>
              </span>
            ))}
          </div>
          <div className="latency-segment-legend">
            {segments.map((segment, segmentIndex) => (
              <span key={`${segment.kind}-legend-${segmentIndex}`}>
                <i className={`latency-dot latency-dot-${segment.kind}`}></i>
                {segment.label} {fmtMs(segment.ms)}
              </span>
            ))}
          </div>
        </div>
      )}
      {debug ? (
        <details className="debug-inline">
          <summary>完整工具输入 / 输出</summary>
          <p className="muted small">工具输入</p>
          <pre>{JSON.stringify(debug.arguments, null, 2)}</pre>
          <p className="muted small">工具输出</p>
          <pre>{JSON.stringify(debug.observation, null, 2)}</pre>
        </details>
      ) : null}
    </details>
  );
}
