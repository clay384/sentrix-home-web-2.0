import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { RunItemDetail } from "../../api/types.ts";

import { DebugTracePanel } from "./DebugTracePanel.tsx";

afterEach(cleanup);

const DETAIL = {
  index: 0,
  question: "车停在哪",
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
  runtime_turns: [
    {
      index: 0,
      message: "车停在哪",
      answer: "停在小区地下车库",
      agent_status: "completed",
      trace_bundle: {
        debug_calls: [
          {
            step_id: "planner_1",
            call_type: "planner",
            seq: 0,
            status: "complete",
            messages: [
              { role: "system", content: "规划器系统提示词" },
              { role: "user", content: "车停在哪" },
            ],
            tools: [],
            response: { content: '{"requirements":[]}', finish_reason: "stop" },
          },
          {
            step_id: "step_1",
            call_type: "agent",
            seq: 1,
            status: "complete",
            messages: [
              { role: "system", content: "你是相册助手" },
              { role: "user", content: "车停在哪" },
            ],
            tools: [{ type: "function", function: { name: "search_memories", description: "检索照片" } }],
            response: {
              content: "",
              thought: "先检索一下",
              tool_calls: [{ id: "tool_1", name: "search_memories", arguments: '{"query":"车"}' }],
              finish_reason: "tool_calls",
            },
          },
          {
            step_id: "step_2",
            call_type: "faithfulness_judge",
            seq: 2,
            status: "complete",
            messages: [
              { role: "system", content: "评审系统提示词" },
              { role: "user", content: "车停在哪 + 回答" },
            ],
            tools: [],
            response: { content: '{"faithful":true}', finish_reason: "stop" },
          },
        ],
      },
      trace: [
        { kind: "plan", status: "complete", text: "已确认需要哪些证据", payload: { call_id: "planner_1" } },
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
        { kind: "terminal", status: "complete", text: "已完成本轮回答", payload: {} },
      ],
    },
  ],
} as unknown as RunItemDetail;

describe("DebugTracePanel · 每道题的全流程完整轨迹", () => {
  it("渲染模型实际输入、可用工具、原始输出、工具入参与最终回答", () => {
    render(<DebugTracePanel detail={DETAIL} />);

    expect(screen.getByText("全流程完整轨迹")).not.toBeNull();
    expect(screen.getByText("Agent 全流程（按发生顺序）")).not.toBeNull();
    expect(screen.getAllByText("规划器调用").length).toBeGreaterThan(0);
    // 每条模型调用的三条：messages / tools / 原始回复。
    expect(screen.getAllByText("你是相册助手").length).toBeGreaterThan(0);
    expect(screen.getAllByText("规划器系统提示词").length).toBeGreaterThan(0);
    expect(screen.getAllByText("评审系统提示词").length).toBeGreaterThan(0);
    expect(screen.getAllByText("先检索一下").length).toBeGreaterThan(0);
    expect(screen.getAllByText(/search_memories/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/total/).length).toBeGreaterThan(0);
    expect(screen.getByText("停在小区地下车库")).not.toBeNull();
    expect(screen.getByText("运行时事件（2.0 进度流 · 5 条）")).not.toBeNull();
  });

  it("历史结果没有 debug_calls 时明确提示未记录，而不是假装空轨迹", () => {
    const legacy = {
      index: 0,
      question: "车停在哪",
      runtime_turns: [
        {
          index: 0,
          message: "车停在哪",
          answer: "旧答案",
          trace: [{ kind: "status", status: "running", text: "正在理解你的问题…", payload: {} }],
          debug_trace: [{ type: "status", status: "running", text: "正在理解你的问题…" }],
        },
      ],
    } as unknown as RunItemDetail;

    render(<DebugTracePanel detail={legacy} />);
    expect(screen.getByText(/该结果没有记录模型的完整输入/)).not.toBeNull();
    expect(screen.getByText("旧答案")).not.toBeNull();
  });
});
