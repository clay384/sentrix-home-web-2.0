# 守卫 + 证据/答案生命周期重设计（2026-10-26）

依据：`487qa_gap_root_cause.md` / `guard_evidence_redesign.md`（AAA 定稿）。
目标不是减少守卫数量，而是修掉三处语义缺口：

1. **类型打勾 + 全局 aggregate**：检索到 95 张候选会被当成「婚宴花了多少钱」的证据；
2. **守卫覆盖答案**：`structured_fact_reminder` 把已经正确的回答推去查一个没有该字段的工具；
3. **每回合全量 L2**：531 次调用 / 1.19M prompt tokens，224 次没有改动。

## 1. 规划器（P1）

- `EvidenceRequirement` 增加 `predicate / value_type / scope`。
- `build_prompt` 增加 value_type 判定表；`planner_response_format()` 生成 JSON Schema。
- `GoalPlanner` 探测 `StructuredChatModel`，有则用 `response_format` +
  `chat_template_kwargs={"enable_thinking": false}`，没有则退回普通 `complete`。
- `OpenAIChatModel.complete_structured` 在端点拒绝 schema 时自动退回普通请求。
- `parse_json` 先取第一个完整 JSON 对象（兼容 `</think>` 后重复 JSON）。
- `parse_declaration` 校验新字段；非法时 `invalid_requirement` 并纠正重试。

**G1（19 题 shadow）**：19/19 schema、9/9 derailment、0 重试、0 占位符、
prompt token 31,653 vs 基线 29,316（+7.97%）。

## 2. 证据绑定与守卫（P2/P5）

- 新增 `domain/assistant/evidence_binding.py`：
  - `Fact`：`fact_type/value_type/value/scope/filters_applied/filters_not_applied/
    provenance/confidence/coverage`；
  - `bind_requirement`：`satisfies / partial / unsupported / scope_mismatch /
    conflict / unknown`；
  - `facts_from_state`：从成功工具记录读时投影，不另存状态。
- `query_memory_facts` 观察里写 `value_type/filters_requested/filters_not_applied/
  coverage`；参数里有、实际没应用的过滤条件可判定。
- `_one_shot_evidence_reminder`：
  - 删除 `structured_fact_reminder`；
  - `gate_unattempted` 改为 `required_evidence_missing`：required + 未尝试 +
    绑定不是 `unsupported/partial`；
  - `unsupported` / `partial` 不提醒，允许如实报缺口。
- 收尾：`_binding_gap` 把 `unsupported/partial/scope_mismatch/conflict` 计入
  `answered_with_gap`；轨迹新增 `evidence_ledger.bindings` 与 `evidence_binding` 事件。
- 答案保护：`best_answer` + `best_coverage`，收尾优先用覆盖率最高的一版。

## 3. L3 claim-level 核验（P3）

- 新增 `domain/assistant/claim_verifier.py`：高风险 claim 抽取（金额/日期/数量/地点）、
  claim 提示词、裁决解析。
- 新增 `application/assistant/claim_verifier.py`：一次模型调用，失败按 ambiguous 放行。
- `run_turn._review_with_verification`：
  - 只对「高风险 + 未绑定」的 claim 调 L3；
  - 每回合最多一次；`SENTRIX_CLAIM_VERIFIER=0` 整体回退旧 L2；
  - `contradicted/unsupported` 才重答，`ambiguous/supported` 放行。
- 轨迹新增 `claim_reviews`。

## 4. 交付生命周期（P4）

- `TaskState.result_set_ids`：本轮出现过的结果集，最近的在前。
- `AssistantToolbox.deliver` 增加 `fallback_result_set_ids`；依次尝试，
  第一个存在且 scope 匹配的胜出。
- 没有结果集时，空 `selected_image_handles` 合法（不再 `unknown_result_set`）。

## 5. 验证

- `make ci`：ruff / format / mypy / import-linter / reachability / module-map /
  capability-map / pytest / 前端构建。
- 新增单测：`test_evidence_binding.py`、`test_claim_verifier.py`、
  `test_assistant_intent.py` 的结构化字段与 `</think>` 重复 JSON、`test_run_turn.py`
  的 L3 与 unsupported 不提醒、`test_chat_client.py` 的 schema 与回退。
- 487 QA 全量回归见 153 PhotoBench run；关注均分、0/1/2 分布、L3 触发率、
  `unsupported/partial` 分布、`unknown_result_set` 次数。

## 6. 回滚

- `SENTRIX_CLAIM_VERIFIER=0`：L3 → 旧 L2。
- `SENTRIX_PLANNER_MAX_ATTEMPTS` 保持原有重试次数。
- 端点不支持 schema 时自动回退，不需要运维开关。
