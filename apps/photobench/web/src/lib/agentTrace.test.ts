import { describe, expect, it } from "vitest";

import {
  callContent,
  callMessages,
  callThought,
  debugCallForCall,
  debugCallForStep,
  turnDebugCalls,
  turnTimeline,
} from "./agentTrace.ts";

const CALL = (overrides: Record<string, unknown> = {}) => ({
  step_id: "step_1",
  call_type: "agent",
  seq: 1,
  messages: [
    { role: "system", content: "你是相册助手" },
    { role: "user", content: "车停在哪" },
  ],
  tools: [{ type: "function", function: { name: "search_memories", description: "检索" } }],
  response: {
    content: "",
    thought: "先检索",
    tool_calls: [{ id: "tool_1", name: "search_memories", arguments: '{"query":"车"}' }],
    finish_reason: "tool_calls",
    prompt_tokens: 100,
    completion_tokens: 20,
  },
  status: "complete",
  ...overrides,
});

const ITEM = {
  runtime_turns: [
    {
      index: 0,
      message: "车停在哪",
      answer: "停在小区地下车库",
      trace_bundle: {
        debug_calls: [
          {
            step_id: "planner_1",
            call_type: "planner",
            seq: 0,
            messages: [{ role: "user", content: "车停在哪" }],
            tools: [],
            response: { content: '{"requirements":[]}', finish_reason: "stop" },
            status: "complete",
          },
          CALL(),
          {
            step_id: "step_2",
            call_type: "faithfulness_judge",
            seq: 2,
            messages: [{ role: "system", content: "你是评审" }],
            tools: [],
            response: { content: '{"faithful":true}', finish_reason: "stop" },
            status: "complete",
          },
        ],
      },
      trace: [
        { kind: "plan", status: "complete", text: "已确认", payload: { call_id: "planner_1" } },
        {
          kind: "model_action",
          status: "running",
          text: "正在执行 search_memories",
          payload: { step_id: "tool_1", parent_step_id: "step_1", tool: "search_memories" },
        },
        {
          kind: "tool_result",
          status: "ok",
          text: "找到 2 张",
          payload: {
            step_id: "tool_1",
            parent_step_id: "step_1",
            tool: "search_memories",
            arguments: { query: "车" },
            result: { total: 2 },
          },
        },
        { kind: "judge", status: "running", text: "评审中", payload: { call_id: "step_2" } },
      ],
    },
  ],
  tool_trace: [
    {
      tool: "search_memories",
      step_id: "tool_1",
      parent_step_id: "step_1",
      conversation_turn: 0,
      arguments: { query: "车" },
      result: { total: 2 },
      status: "ok",
    },
  ],
};

describe("agentTrace · 完整模型轨迹访问器", () => {
  it("按 step_id + call_type 把 Agent Loop 调用挂到完整模型输入上", () => {
    const call = debugCallForCall(ITEM, { step_id: "step_1", call_type: "agent", conversation_turn: 0 });
    expect(call).not.toBeNull();
    expect(call?.step_id).toBe("step_1");
  });

  it("step_id 缺失时不猜（老 run 回退由 debug_trace 负责）", () => {
    expect(debugCallForCall(ITEM, { call_type: "agent", conversation_turn: 0 })).toBeNull();
  });

  it("进度步骤按 parent_step_id / call_id 找到对应调用", () => {
    const turn = ITEM.runtime_turns[0];
    expect(debugCallForStep(turn, turn.trace[0])?.call_type).toBe("planner");
    expect(debugCallForStep(turn, turn.trace[1])?.step_id).toBe("step_1");
    expect(debugCallForStep(turn, turn.trace[3])?.call_type).toBe("faithfulness_judge");
  });

  it("时间线按 planner → model → 它发起的工具 → judge → answer 排列", () => {
    const timeline = turnTimeline(ITEM, 0);
    expect(timeline.map((entry) => entry.kind)).toEqual([
      "planner",
      "model",
      "tool",
      "judge",
      "answer",
    ]);
    expect(timeline[2].tool?.tool).toBe("search_memories");
    expect(timeline[4].answer).toBe("停在小区地下车库");
  });

  it("老 run 没有 debug_calls 时回退到 debug_trace 的步", () => {
    const legacy = {
      runtime_turns: [
        {
          index: 0,
          answer: "旧答案",
          debug_trace: [
            { type: "planner", prompt: [{ role: "user", content: "旧问题" }], raw_full: "旧规划" },
            { type: "model", prompt: [{ role: "user", content: "旧问题" }], raw_full: "旧回答" },
          ],
        },
      ],
    };
    expect(turnDebugCalls(legacy.runtime_turns[0])).toEqual([]);
    expect(turnTimeline(legacy, 0).map((entry) => entry.kind)).toEqual([
      "planner",
      "model",
      "answer",
    ]);
  });

  it("同一 step_id 有失败重试时，绑定成功的那次输入，工具只挂一次", () => {
    const retry = {
      runtime_turns: [
        {
          index: 0,
          message: "车停在哪",
          answer: "地下车库",
          trace_bundle: {
            debug_calls: [
              {
                step_id: "step_1",
                call_type: "agent",
                seq: 0,
                status: "error",
                messages: [{ role: "system", content: "失败尝试" }],
                tools: [],
                response: null,
                error: "prompt too long",
              },
              { ...CALL(), seq: 1 },
            ],
          },
          trace: [],
        },
      ],
      tool_trace: ITEM.tool_trace,
    };
    const bound = debugCallForCall(retry, {
      step_id: "step_1",
      call_type: "agent",
      conversation_turn: 0,
    });
    expect(bound).not.toBeNull();
    expect(callMessages(bound)[0]?.content).toBe("你是相册助手");
    expect(turnTimeline(retry, 0).filter((entry) => entry.kind === "tool")).toHaveLength(1);
  });

  it("消息、正文、思考通道都原样取出", () => {
    const call = CALL();
    expect(callMessages(call)).toHaveLength(2);
    expect(callContent(call)).toBe("");
    expect(callThought(call)).toBe("先检索");
  });
});
