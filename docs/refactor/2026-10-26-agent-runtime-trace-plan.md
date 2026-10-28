# 2026-10-26 · 8772 全流程 Agent 轨迹

## 目标

在 PhotoBench（8772）逐题结果里看到**完整的 Agent 运行时轨迹**：

1. 现有每个步骤（规划 / 模型动作 / 工具结果 / 语义评审 / 结束）都能展开看到
   **产生这一步的模型调用实际看到了什么、回答了什么**；
2. 每道题下面有一条按发生顺序排列的**全流程**：planner → 每次 Agent 调用 →
   它发起的工具 → judge → 最终回答。

## 数据缺口（动手前的核查）

2.0 的回合终态载荷里原本只有：

- `trace.model_calls`：step_id / token / 耗时 / 工具名；
- `public_progress`：面向用户的进度事件（文本 + 工具名 + 入参/结果）；
- `turn.debug_trace`（PhotoBench 投影）：事件流，不含 prompt / raw。

**模型实际发送的 messages、tools 与原始回复从未离开主 API 进程**。因此这不是
「前端把已有字段画出来」的改动，必须先在主 API 采集。

## 决策（用户 2026-10-26 裁决）

| 问题 | 裁决 |
|---|---|
| 什么时候采集 | 仅 PhotoBench 评测时。`POST /assistant/conversations/{id}/turns` 增加 `include_debug`，默认 `false` |
| 采集哪些调用 | planner（含重试）、主 Agent 每一步、faithfulness judge 全部 |
| 保真程度 | 实际 `messages` + 实际 `tools` schema + 原始 `content` / `thought` / `tool_calls` |
| 存哪里 | 内联进 `run.json`：`runtime_turns[*].trace_bundle.debug_calls` |

历史 run 没有这些数据，**不推断**；8772 明确显示「未记录」。

## 实现

### 主 API（8191）

- `StartTurnRequest.include_debug` → `TurnRequest.include_debug`；随
  `TurnRecord.trace` 在建回合时落库，`run` 时读回（`agent_turns.trace_json` 已有该列，
  无迁移）。
- `application/assistant/debug_trace.py`：序列化
  `ChatMessage` / `ToolCall` / `ModelReply`；tools 走 `openai_tools()`，即**实际发给
  端点的那一份**（JIT 剪枝后的 description）。
- `GoalPlanner.declare(include_debug=...)`：每次尝试一条，失败也留（模型服务异常时
  仍能看到发出去的消息）。
- `FaithfulnessJudge.review(include_debug=..., step_id=...)`：裁决仍是 fail-open；
  `debug_call` 只用于记录，不参与判定。
- `RunTurn._complete_recorded`：指标记录（`model_calls`，逐题摊平）与完整记录
  （`debug_calls`，只在 trace 里）**分开两份**，避免 `model_call_metrics` 被
  messages/tools 撑大。
- `_turn_trace` 增加 `include_debug` 与 `debug_calls`（按发生顺序，`seq` 回合内唯一）。

### PhotoBench（8772）

- `SentrixClient.start_turn(include_debug=True)`；`_evaluate_one` 固定打开。
- 后端不改 `debug_trace` 的形状，只补 `step_id` / `parent_step_id` / `call_id`
  三个锚点；完整 prompt 只存一份在 `trace_bundle.debug_calls`。
- `exports.py`：`export_traces` / `export_sft` 优先读 `debug_calls`，老 run 回退
  `debug_trace`。

### 前端

- `lib/agentTrace.ts`：按 step_id 取调用、按 parent_step_id 绑定工具、构建时间线；
  老 run 回退 `debug_trace`。
- `ModelCallView`：消息列表、可用工具、思考通道、原始工具调用、token / finish_reason。
- `DebugTracePanel`：升级为「全流程完整轨迹」，同时保留原始进度事件；每个事件能
  挂回它对应的模型调用。
- `AgentLoopTree` 的每个调用节点：展开即见该步的完整输入 / 原始输出。

## 验收

- 主 API：`test_run_turn.py` 的 `include_debug_*` 三条 + planner/judge 单测 +
  `test_assistant_routes.py` 的开关契约；
- PhotoBench：`test_sentrix_client.py` / `test_run_execution.py` /
  `test_run_reports.py` 的导出；
- 前端：`lib/agentTrace.test.ts`、`DebugTracePanel.test.tsx`、
  `AgentLoopTree.test.tsx`；
- `make ci` 全绿后重启 8191 / 8772，跑一轮新评测确认 8772 能看到模型原文。

## 已知边界

- 旧 run 无法补采；界面显示「未记录」。
- `include_debug` 关闭时 `debug_calls=[]`，不是「调用没发生」。
- 轨迹体积：487 题的一轮预计让 `run.json` 从 ~33MB 增至 ~90MB（用户已确认内联）。
