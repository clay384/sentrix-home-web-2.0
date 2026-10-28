/**
 * 一次模型调用的完整输入 / 输出。
 *
 * 数据来自主 API 的 `trace.debug_calls`（PhotoBench 评测时总是打开）：
 * - `messages` 是**实际发送**的消息序列（含 system、历史、工具结果）；
 * - `tools` 是**实际发送**的工具 schema（JIT 剪枝后的那一份）；
 * - `response` 是模型原始回复（正文 / 思考通道 / 原始工具调用参数）。
 *
 * 这里不做任何截断或“友好化”：复盘要看到模型真正看到的东西。字段缺失时明确显示
 * 「未记录」，而不是用摘要冒充原文。
 */
import type { Dict } from "../../lib/agentTrace.ts";
import {
  callContent,
  callLabel,
  callMessages,
  callResponse,
  callStatus,
  callThought,
  callToolCalls,
  callTools,
} from "../../lib/agentTrace.ts";

function pretty(value: unknown): string {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function RoleMessages({ messages }: { messages: ReturnType<typeof callMessages> }) {
  if (!messages.length) return <p className="muted small">未记录消息（该结果未开启 include_debug）。</p>;
  return (
    <div className="model-message-list">
      {messages.map((message, index) => (
        <article key={index} className={`model-message model-message-${message.role || "unknown"}`}>
          <header>
            <b>{message.role || "unknown"}</b>
            {message.toolCallId ? <span>tool_call_id · {message.toolCallId}</span> : null}
          </header>
          {message.content ? <pre>{message.content}</pre> : <p className="muted small">（空正文）</p>}
          {message.toolCalls.length ? (
            <div className="model-tool-calls">
              <small>assistant 请求的工具调用</small>
              {message.toolCalls.map((call, callIndex) => (
                <pre key={callIndex}>
                  {String(call.name || "")}
                  {"("}
                  {String(call.arguments || "")}
                  {")"}
                </pre>
              ))}
            </div>
          ) : null}
        </article>
      ))}
    </div>
  );
}

function ToolSpecs({ tools }: { tools: Dict[] }) {
  if (!tools.length) return <p className="muted small">本次调用没有可用的工具（规划器 / 评审不带工具）。</p>;
  return (
    <div className="model-tool-specs">
      {tools.map((tool, index) => {
        const fn = (tool.function as Dict | undefined) ?? {};
        const name = String(fn.name || tool.name || `tool_${index + 1}`);
        return (
          <details key={`${name}-${index}`} className="model-tool-spec">
            <summary>
              <strong>{name}</strong>
              <span>{String(fn.description || "").slice(0, 80)}</span>
            </summary>
            <pre>{pretty(tool)}</pre>
          </details>
        );
      })}
    </div>
  );
}

function RawResponse({ call }: { call: Dict }) {
  const response = callResponse(call);
  const content = callContent(call);
  const thought = callThought(call);
  const toolCalls = callToolCalls(call);
  const parseFacts = Array.isArray(response.tool_argument_parse_facts)
    ? response.tool_argument_parse_facts
    : [];
  if (!Object.keys(response).length) {
    return (
      <p className="muted small">
        未记录原始回复。
        {call.error ? ` 调用失败：${String(call.error)}` : ""}
      </p>
    );
  }
  return (
    <div className="model-raw-response">
      {thought ? (
        <details className="model-thought">
          <summary>思考通道（不展示给用户）</summary>
          <pre>{thought}</pre>
        </details>
      ) : null}
      <p className="muted small">正文</p>
      <pre>{content || "（模型没有输出正文）"}</pre>
      {toolCalls.length ? (
        <div className="model-tool-calls">
          <p className="muted small">原始工具调用</p>
          {toolCalls.map((toolCall, index) => (
            <pre key={index}>
              {String(toolCall.name || "")}
              {"("}
              {String(toolCall.arguments || "")}
              {")"}
            </pre>
          ))}
        </div>
      ) : null}
      {parseFacts.length ? (
        <details>
          <summary>工具参数解析结果</summary>
          <pre>{pretty(parseFacts)}</pre>
        </details>
      ) : null}
      <div className="model-usage">
        <span>finish_reason · {String(response.finish_reason || "未记录")}</span>
        <span>
          token · {String(response.prompt_tokens ?? "?")} / {String(response.completion_tokens ?? "?")}
        </span>
      </div>
    </div>
  );
}

export interface ModelCallViewProps {
  call: Dict;
  /** 覆盖标题（例如「规划器 · 第 2 次尝试」）。 */
  title?: string;
}

export function ModelCallView({ call, title }: ModelCallViewProps) {
  const messages = callMessages(call);
  const tools = callTools(call);
  return (
    <details className={`model-call-node model-call-${String(call.call_type || "unknown")}`}>
      <summary>
        <strong>{title || callLabel(call.call_type)}</strong>
        {call.step_id ? <span>步骤 {String(call.step_id)}</span> : null}
        {call.attempt != null ? <span>第 {String(call.attempt)} 次</span> : null}
        <span className={`model-call-status${call.error ? " failed" : ""}`}>{callStatus(call)}</span>
        {call.duration_ms != null ? <span>{String(call.duration_ms)} ms</span> : null}
        <span>
          {String(call.prompt_tokens ?? "?")} / {String(call.completion_tokens ?? "?")} tokens
        </span>
      </summary>
      <div className="model-call-body">
        <section>
          <h5>模型输入 · {messages.length} 条消息</h5>
          <RoleMessages messages={messages} />
        </section>
        <section>
          <h5>模型可见工具 · {tools.length}</h5>
          <ToolSpecs tools={tools} />
        </section>
        <section>
          <h5>模型原始输出</h5>
          <RawResponse call={call} />
        </section>
      </div>
    </details>
  );
}
