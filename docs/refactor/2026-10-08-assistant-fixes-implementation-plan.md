# 助手链路的十条修正 · 实施文档

日期：2026-10-08
依据：两轮 487 题全链路评测的逐题归因（`20261007-233339` 修复前 / `20261008-232747` 修复后中断）
     + 1.0 基准轮（`20260930-113307-...-reuse-f7148c`）对照
状态：**决定已定，待施工**

---

## 0. 怎么读这份文档

每一条的状态是：**判据（实测）→ 根本原因 → 改什么（文件级）→ 门禁 → 依赖**。
编号沿用讨论时的问题编号（1–7、A–D）。**施工顺序见 §2，不要按编号顺序做。**

三条施工纪律（与项目既有纪律一致）：

1. **判据必须是机制层的**，不是"某道题答错了"。
2. **修的是系统，不是题**——禁止任何与具体问题文本、具体 asset、具体相册相关的分支。
3. **改完要有能封住它的门禁**，且断言的是**关系**（例如"文案里的数字 == profile 的值"），
   不是把常量抄一遍。

---

## 1. 背景：两轮实测与那条因果链

### 1.1 两轮对照（前 126 题，同一批 `qa_id`，已核对一致）

| | 修复前 `20261007-233339` | 修复后 `20261008-232747`（中断） |
|---|---:|---:|
| 错率 | 56.3% | **70.6%** |
| 错率（剔除 `model_error`） | 55.6% | **65.4%** |
| 交付 | 17.5% | **31.0%** |
| `model_error` | 2 | **22**（9×500 + 13×400） |
| `step_budget_exhausted` | 32 | 53 |
| 被拒调用 | 241 | 329 |

**唯一确定变好的是交付**（`758a91c` 把交付折回终止动作的字段 + 做成必填）。
**其余全面变差**，且两个新错误（500 / 400）同源——见 §3.3。

### 1.2 那条因果链（它把 2、3、4 串起来了）

```
2.0 的 preview 只给 caption（Observation 有 8 个语义列，只暴露了 1 个）
   ↓  模型看不出"这张里有什么、写了什么字"
"看图"工具调用 0.97 次/题 → 3.4 次/题（3.5 倍）
   ↓  撞 max_inspections = 3
被拒 16 次 → 741 次（拒绝率 1.4% → 26.7%）
   ↓  1.0 此刻 break 结束回合；2.0 把拒绝当普通工具结果发回，允许重试
模型步烧光 → step_budget_exhausted 123 题（102 道判 0、2 分一道没有）
```

**关键对照数字**（全 run，487 题）：

| | 1.0 | 2.0 |
|---|---:|---:|
| 成功调用 | 1092 | 2039 |
| **被拒** | **16** | **741** |
| 拒绝率 | **1.4%** | **26.7%** |
| 每题调用 | **2.28** | **5.71** |
| `inspect_photo` ok / 拒 | 336 / 11 | 732 / **379** |
| `read_photo_text` ok / 拒 | 137 / 5 | 297 / **245** |

**差距全部集中在"看图"两个工具上**（`search_memories` 两边都是 0–6% 拒绝率）。

### 1.3 1.0 真正给模型的 preview（`agent_runtime/runtime.py:119-128`）

```python
_RETRIEVAL_PREVIEW_KEEP = ("handle", "captured_at", "place", "media_kind",
                           "level", "evidence_summary", "people")   # asset_id 被显式剔除
_RETRIEVAL_TOP_KEEP = ("result_set_id", "query", "preview", "can_inspect",
                       "recommended_handle", "recommended_resolution", "page", "page_size",
                       "group_photo_count", "group_photo_sizes", "group_photo_rows")
preview[:5]      # 每页只给前 5 条
```

而 `evidence_summary` 是**五个列拼出来的**（`agent_runtime/tools.py:371-399`），按此顺序：

```
caption → activity → place → objects[:8]（顿号连接） → clothing[:8] → "文字：" + ocr_text
```

实测样例：
> 婚礼现场，新郎新娘站在紫色地毯上…；**婚礼仪式进行中**；**婚礼宴会厅**；
> **紫色地毯、白色婚纱、西装、麦克风、花束**；**文字：双喜**

**`objects` / `clothing` / `ocr_text` 正是模型会去调 `inspect_photo` / `read_photo_text`
才拿得到的东西。2.0 有这 8 个列，只暴露了 `caption`。**

1.0 的 `search_memories` 描述里还有一句 2.0 没有的话：

> 单张照片的地点、时间、人物**直接在 preview 里读，不要为此再调用其他工具**。

### 1.4 两轮的 `model_error` 同源（见 §3.3）

`chat_client._max_tokens`：`max(64, min(1501, 4400 - prompt_tokens))`。
`4400` 是**照 1.0 自己的窗口 `max_model_len = 4501` 算的**（1.0 注释原文：
「Phase H H4：max_model_len=4501 的硬上限保护」），而 2.0 的窗口是
**6144**（`llama-server --ctx-size 24576 --parallel 4`）。常量没跟着改。

于是：`prompt > 4400` → 公式给负数 → `max(64, …)` 抬成 **64** → 发出一个
「窗口其实装得下、但输出只够写半个 JSON」的请求 → 截断 → **500 Parse error**；
prompt 再大到 ~6080 以上才真撞 **400**。

**这是一条静默失败**：把"提示词太大"（可诊断）伪装成"模型服务不可用"。

---

## 2. 决定汇总与施工顺序

| # | 问题 | 决定 |
|---|---|---|
| **2** | 工具 schema 2704 token + preview 缺字段 | 恢复 **JIT 剪枝**；**A 类字段全补**；**B 类全补**；**C 类不搬** |
| **1** | `max_tokens` 静默截断 | **三步都做**：常量按真实窗口重算 / 不再发注定写不完的请求 / 超预算压缩历史 |
| **4** | 上限本身 | **A** 被拒也计价 **+ B** 不变量门禁 **+ C/B′** 额度用尽后收回工具 |
| **3** | 交付无强制力 | 搬 1.0 的**三条**按证据类型的一次性提醒 |
| **5** | 规划器过度声明（212 : 3） | 收紧规划提示词 |
| **D** | `delivery_contradiction` 死规则 | **删掉** |
| **6** | 度量口径（并行） | 暴露候选集，恢复候选级指标 |
| **7** | 模型能力 39 道 | **先不动**，等上游 |
| **A** | `HISTORY_LIMIT = 8` | **不改**，登记为缺口 |
| **B** | L2 打回不复查 | **先不动** |
| **C** | `evidence_insufficient` 掉 10 倍 | **不改**（6/10 归问题 1/4，4/10 归问题 B） |

### 施工顺序

```
第一阶段（上游，决定成败）
  1. 问题 2A —— preview 补字段
  2. 问题 2B —— JIT 剪枝

第二阶段（基础设施）
  3. 问题 1  —— max_tokens 三步

第三阶段（兜底）
  4. 问题 4  —— A + B + C/B′

第四阶段（行为）
  5. 问题 3  —— 三条提醒
  6. 问题 5  —— 规划提示词
  7. 问题 D  —— 删死规则

并行（独立代码区域）
  8. 问题 6  —— 暴露候选集

最后：重跑一轮，按 §5 的四个数验证
```

---

## 3. 逐条实施

### 3.1 · 问题 2A：preview 补字段

**判据**：§1.2 的因果链；1.0 的 `_RETRIEVAL_PREVIEW_KEEP` 有 7 个字段而 2.0 只有 3 个；
`Observation` 上 8 个语义列 2.0 只暴露了 `caption`。

**文件**：`apps/api/src/sentrix/application/assistant/tools.py` 的 `AssistantToolbox._preview()`

**改法**：

1. **重建复合摘要**（照 1.0 的顺序，用 2.0 现成的列）：

   ```python
   parts = [caption, activity, place]              # 三段落，去重、非空
   parts.append("、".join(objects[:8]))            # 有才加
   parts.append("、".join(clothing[:8]))           # 有才加
   if ocr_text: parts.append(f"文字：{ocr_text}")   # 有才加
   evidence_summary = "；".join(parts)
   ```

2. **新增 `media_kind`** —— 取 `Asset.media_type.value`（`image` / `video`）。
   **注意**：视频派生的关键帧是 `media_type='image'` 的普通资产，靠
   `derived_kind` / `parent_asset_id` / `source_timestamp_sec` 认亲。
   1.0 **没有**把血缘给模型，2.0 也**不扩大**——只给 `media_kind`。

3. **新增 `level`**（相关性强度，1.0 用 `exact` / `strong` / `approximate`）。
   **2.0 的判据要从融合层现取，不要照抄 1.0 的"是不是事件成员"**：
   `domain/retrieval/fusion.py` 的 `evidence_class_for()` 已经返回
   `anchor` / `semantic` / `expander`（`ANCHOR_CHANNELS = ("metadata", "entity")`）。
   映射：`anchor → exact`、`semantic → strong`、`expander → approximate`。
   **施工前先核实** `FusedCandidate` 上确实带着 `evidence_class`；没有就把它加上
   （那是纯计算，不加任何决策）。

4. **顶层加 `can_inspect`** —— `bool(preview)`，1.0 同义（`len(preview) > 0`）。
   Hmm——2.0 已经用 `has_more` 表达"还有没有更多"，`can_inspect` 是"这批能不能精读"。
   两者不同，别合并。

5. **每页条数**：1.0 给模型 `preview[:5]`，2.0 给整页 6 条。
   **保持 2.0 的 6**（1.0 那个 5 是它自己的上限裁切，没有证据说 5 比 6 好）。

6. **必须同时加回 1.0 那句描述**（`domain/assistant/tools.py` 的 `SEARCH_MEMORIES`）：
   「单张照片的地点、时间、人物**直接在 preview 里读，不要为此再调用其他工具**」。
   **不加这句，字段加了也白加。**

**门禁**（`tests/integration/test_assistant_toolbox.py`）：

- `test_the_preview_carries_where_who_and_what` —— 有观察时三个字段有值（已写）
- `test_the_preview_keys_are_always_present` —— 没观察时键仍在、值取空（已写）
- **新增**：`test_the_summary_is_composed_like_1_0` —— 造一条带
  `activity`/`objects`/`clothing`/`ocr_text` 的观察，断言 `evidence_summary`
  **同时**含这四样（而不是只有 caption）
- **新增**：`test_the_preview_carries_the_media_kind` —— 图与视频各一条
- **新增**：`test_the_preview_carries_the_relevance_level` —— anchor 通道的命中是
  `exact`、semantic 是 `strong`

**依赖**：无（这是上游）

---

### 3.2 · 问题 2B：恢复 JIT 剪枝

**判据**：2.0 每次模型调用都带全部 8 个工具的完整 schema = **5408 字符 ≈ 2704 token**
（实测：`query_memory_facts` 一个就 1344 字符）。1.0 用 JIT 剪枝，每步只发
**150–300 token**（`LITE_TOOL_SCHEMAS`，每工具 <40 token），
对应 1.0 的真实 `prompt_tokens` 中位数 **1437**。

**现状**：`application/assistant/run_turn.py` 的 `_converse` 里
`reply = self._model.complete(messages, TOOL_SPECS)` —— 全量。

**改法**：

1. **`ToolSpec` 加一个精简描述**（`domain/assistant/tools.py`）：
   `brief: str` —— **≤40 字**，一行说清"这个工具干什么、什么时候用"。
   JIT 渲染时用 `brief` 替代 `description`。

2. **新增 JIT 选择器**，位置建议 `domain/assistant/jit.py`（1.0 是
   `agent_runtime/jit_prompt.py`，纯函数、无 IO，属 domain）。
   签名：
   ```python
   def select_tools(ledger: EvidenceLedger, *, has_result_set: bool,
                    tools_exhausted: bool) -> tuple[ToolSpec, ...]
   ```
   选择规则（**按当前未决的证据需求**）：

   | 条件 | 给出 |
   |---|---|
   | 总是 | `final_answer`（终止动作必须有） |
   | 还没有结果集，或 `photo_set` 未满足 | `search_memories` |
   | 已有结果集 且 `photo_set` 未满足 | `get_result_page` |
   | `visible_text` 未满足 | `read_photo_text` |
   | `visual_observation` 未满足 | `inspect_photo` |
   | `photo_identity` 未满足 | `query_photo_people` |
   | `structured_fact` 未满足 | `query_memory_facts` |
   | `person_profile` 未满足 | `get_person_profile` |
   | **`tools_exhausted`（工具额度用尽）** | **空集**（见 §3.4 的 C/B′） |

   **两个必须注意的点**（都来自 1.0 的实测教训）：
   - **工具集收缩后必须仍然可答**。1.0 专门补过一条：规划声明的需求可能
     **漏掉前置工具**（例如只声明了语义需求，却没给 `search_memories`），
     所以它会把"当前待解析的那一步需要的工具"补回可用集。
     2.0 的规则里"还没有结果集就必须给 `search_memories`"就是这一条。
   - **不要因为工具不在集里就让模型无从下手**——`final_answer` 永远在。

3. **渲染**：`domain/assistant/tools.py` 的 `openai_tools(specs)` 已经接受 specs 参数，
   加一个 `brief=True` 的模式（或让 JIT 返回 brief 版的 ToolSpec）。

**门禁**（`tests/unit/test_assistant_jit.py` 新建）：

- `test_only_the_tools_the_open_requirements_need_are_offered`
- `test_the_terminal_action_is_always_offered` —— 空需求也要有 `final_answer`
- `test_a_missing_result_set_always_offers_search` —— 反向：不能把检索也剪掉
- `test_the_brief_listing_is_smaller_than_the_full_one` —— 断言字符数**关系**
  （brief 版 < 全量版的 1/3），不是抄一个具体数
- **新增到 `tests/unit/test_assistant_policy.py`**：
  `test_every_tool_has_a_brief_no_longer_than_40()` —— 每工具都要有 brief，且 ≤40 字

**依赖**：与 §3.4 的 C/B′ 共用"空集"这条路，先做 3.2 做基础、3.4 接上。

---

### 3.3 · 问题 1：`max_tokens` 三步

**判据**：§1.4。实测两轮共 **22 道**（126 题里）因此产生 500/400。

**文件**：`apps/api/src/sentrix/infrastructure/assistant/chat_client.py`

**改法（三步，缺一不可）**：

1. **常量按真实上下文算，不再抄 1.0 的 4400。**
   真实窗口是**端点参数**（8100 是 `--ctx-size 24576 ÷ --parallel 4 = 6144`），
   不是代码常量。
   - 首选：**从配置读**（`Settings` 加一项，例如 `SENTRIX_VLM_CONTEXT_TOKENS`，
     默认 6144），理由与"模型名问端点"一致——那是部署事实。
   - 至少：把 `PROMPT_BUDGET_TOKENS` 从 4400 改成按 6144 留安全余量算，
     并在注释里写明它的来历（1.0 的 4501）与为什么要改。

2. **下限不能是"能发就发"。**
   算出 `available = context_tokens - prompt_tokens - safety_margin`。
   若 `available < MIN_USABLE_OUTPUT`（建议 **256**——一次工具调用的 JSON 至少这么长），
   **不要发请求**，抛一个**具名异常**（`ModelServiceError` 的子类，
   例如 `PromptTooLongError`），文案要带真实数字：
   「提示词约 N token，上下文上限 M，剩余 X 不足以写完一次工具调用」。

3. **超预算时先压缩历史再重试**（`application/assistant/run_turn.py` 的 `_converse`）：
   捕获上面的异常 → 压缩一轮对话历史 → 重试一次；**压不动就如实收尾**（走现有
   `_stop`，理由用现有枚举，不新造）。

   **压缩的单位是「一整轮工具往返」，必须成对丢**：
   一条 `assistant(tool_calls=...)` 和它对应的 `tool` 结果**要一起丢**——
   严格 chat template 要求每个 `tool_call_id` 都有配对结果，
   丢一半会直接 400（1.0 `intent.py` 的注释记过这条）。

**门禁**（`tests/unit/` 或 `tests/integration/test_chat_client.py`）：

- `test_a_prompt_that_leaves_no_room_is_refused_not_truncated` ——
  构造一个超长 messages，断言**抛出具名异常**、**且没有发请求**
  （用既有的假 HTTP handler 断言 `received == []`）
- `test_the_output_budget_is_derived_from_the_real_window` ——
  断言的是**关系**（`max_tokens == min(MAX_ANSWER, context - prompt - margin)`），
  不是抄一个数
- `test_compaction_drops_whole_tool_round_trips` —— 断言剩下的消息里
  `tool_call_id` 与 `tool` 消息**一一配对**，没有孤儿
- `test_a_prompt_that_still_does_not_fit_ends_the_turn_honestly` ——
  压到极限仍不够时，收尾原因有名字、不是崩

**依赖**：与 3.2 同源（都减小 prompt），但**可以独立做**。

---

### 3.4 · 问题 4：上限

**判据**：§1.2。实测 `step_budget_exhausted` 123 题里 **118 题恰好用满 8 步**；
被拒占 2.46/8 ≈ **31%** 的步数；`step_budget_exhausted` 的 **102 道判 0、2 分 0 道**。

**现状（`domain/assistant/profile.py` + `task_state.py`）**：

| 计数器 | 数什么 | 上限 | 被拒算不算 |
|---|---|---|---|
| `model_steps` | 主循环每转一圈 +1 | 8 | **算** |
| `tool_calls` | `status == "ok"` 且排除 `final_answer` | 6 | **不算** |
| `search_calls` | 所有 `search_memories` 记录 | 1 | **算** |
| `inspection_calls` | `status == "ok"` 且 `EXPENSIVE` | 3 | **不算** |

**改法**：

**A · 被拒也计价（把计价器修对）**
`TaskState.tool_calls` 改成数**所有发出的工具调用**（成功 + 被拒 + 失败），
即"工具往返次数"，并**改名**（`tool_round_trips`）让语义自明。
`final_answer` 仍排除（它是作答，不是工具）。

**理由（这条很重要）**：修完之后，"工具往返数"被硬限死在 6，而每次往返恰好 1 个模型步 →
**总步数 ≤ 6 + 1(作答) + 1(L2) = 8 = `max_steps`，不变量自动成立**。
现在的错位恰好相反：被拒免费 → 模型一路试到步数耗尽。

**B · 把不变量写成测试**
`tests/unit/test_assistant_task_state.py` 加：
```python
def test_the_step_budget_covers_a_full_tool_budget():
    """一次能用满工具额度的回合必须塞得进模型步预算。"""
    assert DEFAULT_PROFILE.max_steps >= DEFAULT_PROFILE.max_tool_calls + 1 + 1
    #                                              ↑作答    ↑L2 评审各占一步
```
**不改数值**：8 ≥ 6 + 1 + 1 成立。（A 之后它才是真的成立；A 之前是假成立——
被拒可以额外吃步。）

**C/B′ · 额度用尽后收回工具**
`_converse` 里，当 `not state.can_call_tool()` 时，改成
`self._model.complete(messages, ())` —— **不带工具**。模型物理上无法再调，
只剩"作答"这一条路，于是 8 步里最后那 1–2 步会被用来**说话**而不是撞墙。

**这就是把"模型没机会说话"换成"模型被迫说话"**——实测 `step_budget_exhausted`
102/123 判 0，根子就是没机会说话。

**施工前必须实测一件事**：`complete(messages, ())` 时客户端**不能发 `"tools": []`**
（llama.cpp 可能 400），要把 `tools` 字段**整个省略**。先打一次真实请求确认。

**注意测试假件**：`tests/integration/test_run_turn.py` 的 `ScriptedModel.complete`
用 `if not tools:` 来区分"规划 / L2 评审"与主循环。传空元组会让它误判成评审。
**假件要按别的判据分流**（例如按 system 里有没有 `PLANNER_MARKER`/`JUDGE_MARKER`
——它本来就已经在看 `JUDGE_MARKER`）。

**门禁**：

- `test_a_denied_call_consumes_the_tool_budget`（反向：证明 A 生效）
- `test_the_step_budget_covers_a_full_tool_budget`（B）
- `test_the_tools_are_withdrawn_when_the_budget_is_gone`（C/B′）——
  断言额度用尽后的那次 `complete` **拿到的 tools 是空元组**

**依赖**：C/B′ 依赖 3.2 的 JIT 选择器（"空集"是它的一个输入）。

---

### 3.5 · 问题 3：搬 1.0 的三条一次性提醒

**判据**：212 题声明 `delivered_photo`，**145 道零交付，而这 145 道里交付工具一次都没被调用过**
（0 次被拒、0 次失败）——不是"想交交不了"，是"根本没走到"。同类：`visible_text`
声明 213 道、155 道未满足，其中 **147 道从未调用过** `read_photo_text`。

**1.0 的做法（`agent_runtime/runtime.py:2540-2660`）**：三条**一次性**提醒，
各自一个 `*_prompted` 标志位，动作完全一样——
**把模型刚才那个 final 塞回对话 → 追加一条 user 提醒 → `continue`**：

| # | 触发条件 | 提醒要点 |
|---|---|---|
| ① `structured_fact_reminder_prompted` | 声明了 `structured_fact` 且从未调过 `query_memory_facts` | 「该问题声明了全量结构化统计需求。若要报告数量、日期或分组，请调用 query_memory_facts 获取当前相册的精确统计；如果条件不支持或没有可确认口径，也可以直接输出 final 如实说明，**不要把检索候选数当作统计答案**。」 |
| ② `gate_unattempted_prompted` | 有**未尝试**的必需需求 | 「还有声明的证据需求尚未尝试：`{req_id}:{evidence_type}`…。当前可用工具：`{工具名}`。请先用兼容工具获取证据；如果确实没有对应的照片或记录可以确认（例如问题本身没有可查的答案），请直接输出 final 如实说明，不要编造。」 |
| ③ `fabrication_check_prompted` | **零工具调用**却给出了具体数字/金额/年份（正则 `\d[\d,，.万]*\s*(?:元|块钱|万|桌|张|人|个|名|年|月|日|号)`） | 「你的回答给出了具体的数字/金额/年份，但本轮还没有检索相册来核实。请先调用 search_memories…；如果相册里确实没有能确认该数字的依据，请如实说"现有记录无法确认"，不要编造数字。」 |

**以及最后那一环**：提醒之后模型坚持 final → **放行，代码不覆盖**
（1.0：`decision = partial_answer` 或 `accept_with_insufficient_evidence`）。
2.0 现在就是这个行为（`_judge_answer` 走到底就收），**不用改**。

> **不要搬第四条**：1.0 还有一条 `is_candidate_mode` 的「你尚未检索相册，禁止直接猜测回答」，
> 它**没有** `prompted` 标志（不是一次性的），会一直拦。2.0 已有
> `MIN_REQUIREMENTS = 1` 堵住零需求，不需要它。

**文件**：`application/assistant/run_turn.py` 的 `_judge_answer`。
位置：**L1 与 L2 都通过之后、`state.finish()` 之前**。

**用 `state.block_guard_code(code)` 做闩**——那是 2.0 既有的"只提醒一次"机制，
不要新造标志位。

**门禁**（`tests/integration/test_run_turn.py`）：

- `test_an_unattempted_requirement_earns_one_reminder` —— planner 声明
  `visible_text`、模型不调 `read_photo_text` 就 final → 断言对话里出现
  ②那条提醒、并且**只出现一次**（按"同一次调用的消息里出现几次"数，不要跨调用求和）
- `test_a_declared_but_unused_structured_fact_earns_one_reminder`（①）
- `test_numbers_without_any_tool_call_earn_one_reminder`（③）
- `test_the_reminder_lets_the_model_insist` —— 提醒后仍 final → 正常收尾，不被拦

**依赖**：②需要 §3.1 的 `unmet_reason`（已修）来判断"未尝试"。

---

### 3.6 · 问题 5：收紧规划提示词

**判据**：212 道声明 `delivered_photo`，而问题文本里真正要求看图的只有 **3 道**；
`delivered_photo + photo_set` 成了 129 道题的默认模板。

**文件**：`apps/api/src/sentrix/domain/assistant/intent.py` 的 `_DECLARATION_PROMPT`。

**改法**：把那句

> 用户想看照片、要求展示、或问「有没有……的照片」时，声明 `photo_set`；
> **同时声明 `delivered_photo`** 表示「要把照片真的交到用户手上」，只取到候选不算交付。

改成**有条件**的表述：只有**用户明确要看图**（「给我看看 / 我想看 / 有没有…的照片」）
时才同时声明 `delivered_photo`；只是"问关于照片的事"不要声明。

**注意**：1.0 用的是同一句，所以这是一次**有意的偏离**，要登记到
`docs/refactor/` 的偏离文档里，理由写清楚（212 : 3 的实测）。

**门禁**：`tests/unit/test_assistant_intent.py` ——
断言提示词里出现"只有…才"的条件词（这是文本断言，弱；真正的验证靠重跑后的
"声明了 `delivered_photo` 的题数"从 212 降下来）。

---

### 3.7 · 问题 D：删掉 `delivery_contradiction`

**判据（读代码确认，非推测）**：规则要求 `delivery_state == "complete"` **且**
`delivered_count == 0`（整数）。而 `deliver()` 成功路径的 `delivered` **恒 ≥ 1**，
失败路径走 `_failure`（`observation` 里没有 `delivered`），
`_finish_with_final_answer` 又把失败记成 `status="error"`，
`GuardFacts.from_state` 会跳过非 `ok` 记录。**所以 `delivered_count == 0` 取不到。**

**更要紧的是它的前提已经变了**：规则设计于"省略的时代"（1.0 的 final 有 49%
根本不带 `selected_image_handles`）；**问题 W6 之后字段必填**，零交付是一个
**明确写下的 `[]`**，不是遗漏。让它活起来会在 ~300 道"检索取完但没交图"的题上
各打回一次、各花一个模型步，而且打回的是模型的**显式选择**。

**文件**：`apps/api/src/sentrix/domain/assistant/final_guard.py`

**改法**：删 `_delivery_completeness` 里的 `delivery_contradiction` 分支、
`_MESSAGES` 里的对应条目；`GuardFacts.delivered_count` 若再无读者一并删
（**先 grep 确认**）。

**门禁**：删掉或改写依赖它的测试（`tests/unit/test_assistant_guard.py` 的
`deliver_record` 相关用例、`tests/integration/test_run_turn.py` 的
`test_a_delivery_that_delivers_nothing_ends_with_a_gap` 的 docstring 提到它）。
**在 `behaviour-baseline.md` 里把这条状态从"规则照搬、输入待重新引入"改成
"已删除 + 理由"**——不能留一段看起来在守的死代码。

---

### 3.8 · 问题 6：暴露候选集，恢复候选级指标（**并行**）

**判据**：`apps/photobench/src/photobench/application/run_execution/service.py:1354-1355`
把 `retrieval_media_refs` / `retrieval_image_ids` **直接赋成 `gt_refs`**，
于是 `retrieval_*` 的分母是 GT、分子是**交付集**：394 道零交付的题 `recall`
一道不差全是 0.0。而 2.0 的 item 里**没有候选集**（1.0 有
`retrieved_candidate_media` / `evidence_source_media`）——根因是助手的 HTTP 契约
不暴露候选集。**后果：分不出「检索没找到」和「没交付」。**

**改法（三段）**：

1. **助手侧落库**：`application/assistant/tools.py` 的 `search_memories`
   `observation` 里**已经有 `asset_ids`**，把它带进 `TurnResult` 并落库。
   存储方案二选一（施工时定）：复用 `agent_turns` 加一列（需迁移），
   或写进已有的 `agent_turn_events` 的 payload。
   **倾向加列**：它是回合级事实，不是事件流。

2. **接口侧**：`api/routes/assistant.py` 的 `GET /assistant/turns/{id}` 加字段；
   `api/schemas/assistant.py` + **重导 openapi 契约**
   （`scripts/export_openapi.py`，`make check-docs` 会守）。
   **这一次是有意放宽契约**——`behaviour-baseline.md` 的
   「2.0 不再暴露的轨迹」一节里 `retrieved_candidate_media` 那一行要改掉。

3. **PhotoBench 侧**：`infrastructure/sentrix/client.py` 读新字段 →
   `domain/runs/execution.py` 写进 item 的 `retrieved_candidate_media` →
   `domain/runs/analysis.py` **新增候选级** precision/recall/f1，
   并把 `attribution` 的分层重建到真实口径上（现在 `primary.R = 424` 是"没交付"的影子）。

**门禁**：

- 助手侧 contract 测试：`test_a_turn_exposes_the_retrieved_candidates`
- PhotoBench 侧：`test_the_candidate_metrics_are_separate_from_the_delivery_metrics`
- **反向**：`test_a_turn_without_retrieval_exposes_no_candidates`（不是恒空数组就算过）

**依赖**：与 3.1–3.7 不冲突（动的是不同的文件）。**但它要在重跑前完成**，
否则重跑拿不到候选级指标。

---

## 4. 登记为"不做"的（写进文档，不写代码）

| 项 | 理由 |
|---|---|
| **A · `HISTORY_LIMIT = 8`** | 不是回归（`multi_turn_clarify` 2.0 62.1% vs 1.0 68.2%，2.0 更好）。**真正的缺口是多轮会极快耗尽上下文窗口**，而 1.0 也没真正设计过多轮对话。**登记为已知缺口**，等上下文预算那套（§3.3）成熟后再谈 |
| **B · L2 打回后不复查** | 不是回归（2.0 软放行 21.6% vs 1.0 29.6%，放行后判 0 的比例 60% vs 55.6%，接近）。先看问题 1/2/4 修完的效果 |
| **C · `evidence_insufficient` 掉 10 倍** | 2.0 那 10 道判 0 里 **6 道是兜底文案**（问题 1/4 的下游）、**4 道归问题 B**。没有独立成因。**但记一条**：其中两道**编了金额**（`album3v4-269` / `album3v4-373`），说明"该拒答却编造"在 2.0 确实存在，重看 L2 时一并处理 |
| **7 · 模型能力 39 道** | 先不动。等上游修完再量——`album3v4-012`（把候选数当答案）那个形态正是 §3.1 的 B 类字段要解决的 |
| **1.0 的 `recommended_resolution`** | 不搬：与规划器重复、硬编码题型词（`合影\|聚餐\|旅行`）、判据在问题文本上、未验证 |
| **1.0 的 `recommended_handle`（关键词匹配版）** | 不搬。同一个问题用 §3.1 的 `level` + 检索排名解决——那是数据，不是字面匹配 |
| **8100 的 `--ctx-size 24576 ÷ --parallel 4 = 6144`** | 部署参数，不是代码。要改得你定（代价是显存） |

---

## 5. 重跑后的验证（四个数）

跑法与上一轮完全一致（`album3-max-video10` / `mixed-image-video-487q` /
`qwen35-q4km` @ 8100），对照 `20261007-233339`（修复前）与
`20260930-113307-...-f7148c`（1.0 基准）：

| # | 指标 | 修复前 | 目标 |
|---|---|---:|---|
| 1 | **`inspect_photo` 每题调用数** | **2.3**（1.0 是 0.69） | 回到 1.0 的量级——这是 §3.1 的直接靶子 |
| 2 | **工具拒绝率** | **26.7%**（1.0 是 1.4%） | 降到个位数 |
| 3 | **`step_budget_exhausted`** | **123 题**（102 道判 0） | 明显下降 |
| 4 | **答案是兜底文案的题数** | **107**（占 279 道错题的 38%） | 明显下降——它是"模型没机会说话"的度量 |
| 5 | 交付覆盖 | 93 / 487 | 继续上升（§3.1 的 B 类 + W6） |
| 6 | `model_error`（500/400） | 22 / 126 | **归零**（§3.3） |
| 7 | `evidence_insufficient` 错率 | 19.6%（1.0 是 2.0%） | 回落（§3.4 的下游） |
| 8 | 候选级 precision/recall | **不存在** | 新增，从此可分锅（§3.8） |

**判读纪律**：先看 1、2、6 三个"机制数"——它们直接对应改动，不受模型随机性影响；
再看 3、4、5 三个"结果数"。**不要用 `retrieval_*` 判检索**（见 §3.8）。

---

## 6. 施工时的注意事项

1. **每一步都要先写失败测试**（判据必须是机制层的），跑确认红，再改，再跑绿。
2. **提交信息里写清"为什么"**，尤其是 §3.1（S3 漏加宽）与 §3.3（1.0 的 4501 常量）。
3. **`behaviour-baseline.md` 要跟着改**：§3.1（preview 字段集）、§3.4（计价口径变了，
   与 1.0 不同，要写明理由）、§3.7（删规则）、§3.8（契约放宽）。
4. **偏离文档要登记**：§3.6（规划提示词偏离 1.0）、§3.4A（计价口径偏离 1.0）。
5. **改完跑 `make ci` 全链**（ruff / mypy strict / import-linter / 两级可达性 /
   capability-map / module-map / 两份 openapi / coverage / web-check）。
6. **全量评测由使用者跑**，本文档不声称成绩已恢复。

---

## 7. 施工结果（2026-10-09 补记）

**§3.1–§3.8 全部落地**，两个提交：`11450a2`（助手侧七修 + §3.8 助手侧）、
`3e709bb`（§3.8 的 PhotoBench 侧）。逐条的实施内容、判据与门禁见提交信息与
`docs/refactor/2026-10-09-assistant-deviations.md`。

施工时对计划的三处**修正/补充**，都记在这里：

### 7.1 两个测试假件必须改判据（计划没预料到）

`tests/integration/test_run_turn.py` 与 `tests/contract/test_assistant_routes.py` 的假件
用 `if not tools:` 区分「这是规划 / L2 评审」。§3.4 的 C/B′ 让工具循环**也能合法拿到
空元组**，于是这条判据会把主循环当成评审——测出来的东西与跑的东西不是一回事。改成按
system 提示词里的 `PLANNER_MARKER` / `JUDGE_MARKER` 分流。

### 7.2 一处**已红但没人发现**的门禁

`tests/integration/test_read_photo_text.py::test_the_tool_is_cheap_not_expensive` 从
`f07b86d` 起就是红的：它断言 `read_photo_text` 是 `CHEAP`，而那次提交恰恰把它改成了
`EXPENSIVE`，**提交信息里却写着「助手相关集成测试全绿」**。已改成断言 `EXPENSIVE`。

另有一处既有的红：`scripts/diff_photobench_reports.py` 的 2 条 mypy 错误（`git stash`
之后同样红），不在本次范围内。

### 7.3 **那轮「修复后」对照是在检索残废的状态下跑出来的**（重要）

`20261008-232747` 那一轮（10-08 23:27）跑在 **WeMM 侧车 8102 死掉之后**（日志最后写入
10-08 10:45，之后一直没起来）。2.0 的 `visual_embedder` 与 `text_embedder` 是**同一个
WeMM 实例**，所以那一轮里：

- 视觉通道（融合权重 **2.5**，最强的一路）全程没跑；
- 文本 ANN 通道也没跑；
- 只剩 lexical / metadata / entity 三路。

实测证据（2026-10-09 在同样的降级状态下直连 9598）：

```
GET /api/v1/search?q=海边  →  degraded=true, degraded_reason="embedder_unavailable"
channel_trace: visual_ann=unavailable, text_ann=unavailable
```

**后果**：§1.1 那张「修复前 56.3% → 修复后 70.6%」的对照表，右边那列不成立。
§3.1 的诊断本身**不受影响**——那是 1.0 vs 2.0-20261007 的对照，而 Oct 7 那一轮侧车还活着；
但「`f07b86d` 之后全面变差」这个结论**分不出多少来自代码、多少来自侧车**。

**重跑前必须确认侧车在跑**（判据是 `GET /api/v1/search` 的 `degraded == false` 且
`channel_trace` 里 `visual_ann` 是 `ok`）。启动方式见
`infrastructure/models/wemm.py` 的模块 docstring。

### 7.4 `group_photo_*`：**做了，但判据换了**（2026-10-09 补记二）

§2 的决定表写着「B 类全补」，而 §3.1 的详细改法（6 条）里没有这三个字段。去看 1.0
到底怎么算之后，决定**保留字段名与形状、换掉判据**（提交 `8166f63`）。

**§7.4 初稿把这套字段认成了「近重复归组」的产物——那是错的。** `_asset_group_key`
（`tools.py:417`）服务于别的东西（预览去重与 `event_group_count`）。真正的
`group_photo_*` 在 `tools.py:1085`：**「合影人数」**。

**1.0 的判据**：`if re.search(r"合影", display_query or user_goal)` —— 只有问题里出现
「合影」两个字时才算，人数靠正则从 `caption` / `activity` 的中文散文里抠「N名 / N人」。
它与 §4 已经否掉的 `recommended_resolution` 是同一类东西（硬编码题型词 + 判据在问题
文本上）。

**它服务多少题**：实测 `mixed-image-video-487q.jsonl` 里含「合影」的题 **77 道**，而问
「一共有几张不同人数的合影」的只有 **1 道**；另外 76 道问的是地点 / 时间 / 是谁，那些
§3.1 的 preview 已经覆盖了。

**2.0 的做法**：`people_count` 进每条 preview（数据来自人脸绑定 + 观察快照，与
`query_photo_people` 同一套算法），顶层的三个字段由 `people_count >= 2` 现算、**不按
关键词门控**。不搬 1.0 的 `description` 那一格（preview 里已有 `evidence_summary`）。
