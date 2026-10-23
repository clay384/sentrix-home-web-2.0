# Sentrix Home Web 2.0 — S3 实施计划（助手闭环）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 打通第三条端到端竖切——在 Web 上问一句「给我看看在海边的照片」，助手自己规划、调工具、取证据、过门禁，把**能直接点开的原图**交回来。全程一条 SSE 连接，前端不轮询。

**Architecture:** 沿用四层（domain / application / infrastructure / api），依赖方向由 `import-linter` 的 5 条契约强制。本切片新增助手域：`domain/assistant`（回合状态、工具契约与收敛、证据账本、结果集、确定性规划、答案门禁、兜底——全是纯规则）、`application/assistant`（四个工具的执行器 + 一次回合的编排）、`infrastructure/assistant`（8100 模型客户端、进程内事件流、人物目录）、`api/routes/assistant.py`（会话 CRUD + 建回合 + SSE）。模型是**原生 `tools` / `tool_calls`**，不是 1.0 那种「让模型在正文里写 JSON」。

**Tech Stack:** Python 3.11 / FastAPI / 标准库 sqlite3 · httpx（连接 8100 上的 llama.cpp，OpenAI 兼容 `/v1`）· pytest / ruff / mypy --strict / import-linter · React 19 / TypeScript / Vite（EventSource）

> **2026-09-22 课程纠正：本计划里的 9 处偏离已逐条审计，7 处回退成对 1.0 的忠实移植。**
> 审计表、原始数据与保留清单见 `docs/refactor/s3-deviations.md`。下面涉及偏离的段落都
> 加了「审计后」的标注；**以那份文档与代码为准，本文档只保留当时的决策记录。**

**依据文档:** `docs/architecture/2026-09-21-sentrix-home-web-2-implementation-design.md`（§2.2 目录、§2.3 分层、§3.1 表、§4.3 SSE 契约、§6 工程标准、§8 切片表）、`docs/refactor/behaviour-baseline.md`（助手一节）

---

## 本文档是怎么写出来的

这份计划**不是照着意图写的**。做法反过来：

1. 在 153 上建了一个隔离副本 `/home/asus/s3verify/repo`（真实仓库一个字节都没动）；
2. 先精读 1.0 的助手实现（`backend/agent_runtime/` 的 30 个模块、`agent_conversation.py`、`app.py` 的助手路由段），产出移植清单；
3. 在副本里按 Task 34 → 45 的顺序**真的写代码**，每个任务先写失败测试、看到红灯、再实现、再绿灯；
4. 全部做完后跑 `make ci` 全绿，重新组织成 12 个 commit（每个 commit 自己也是绿的）；
5. 逐任务做红灯复核（前一任务 commit + 本任务测试）与跨任务回归（所有绿灯命令在终态树上重跑）；
6. **最后**把副本里的实际代码抽出来写成这份文档。

由此有三条硬保证：

- 计划里的**每一个代码块都是逐字节实际跑通过的那一版**（ruff 格式化之后的）；
- 每一个 **Expected 都是实际观察到的输出**，不是推测；
- 每个任务的红灯与绿灯都实际跑过，结果见文末「验证记录」。

**必须写明的代价**：Task 34–45 的代码**此刻还没有进真实仓库**，只存在于验证副本里。执行阶段要做的是把这份计划重新落地一遍——不是"确认一下"，是逐任务真写、真跑。

### 与 S0–S1 / S2 那两份计划的关系

S0–S1（`docs/refactor/2026-09-21-s0-s1-implementation-plan.md`）是照着意图写的，代价是 19 条缺陷。S2（`docs/refactor/2026-09-22-s2-implementation-plan.md`）改成了"先跑通再反向写"，但**代码与 Expected 的错误率仍有 13–14%，且错误全部发生在"计划生成"这一步**——因为生成时是拿 commit 事后拼文档，拼错了没人发现。

本计划在生成侧针对那三处失分做了四条对策（见文末「生成侧对拍」）：

1. 代码块**不做手工搬运**，一律由 `build_plan.py` 从跑通的终态树里按文件整块展开；
2. 每个代码块与它所属 commit 里的文件**逐字节 diff**；
3. 每个 commit 改动的文件集必须**恰好等于**该任务 `Files:` 清单（这是唯一能兜住"整步丢失"的一环）；
4. 每条 `Run:` 命令都重跑一遍再比 Expected。

### 任务编号

S0–S1 用了 Task 1–24，S2 用了 Task 25–33，全局连续。S3 顺着往下排：**Task 34–45**，不另起前缀。

---

## 前置条件

远端仓库 `/home/asus/Github/sentrix-home-web2.0`，分支 `psh2.0`，起点提交 `b98b585`（S2 结束时的工作树干净）。

S3 **不新增任何 Python 依赖**：模型调用走 `httpx`，它在 S2 之前就已经是 dev 依赖（`apps/api/pyproject.toml` 的 `[project.optional-dependencies].dev`），且 FastAPI 本身依赖它。SSE 用 `StreamingResponse`，前端用浏览器原生的 `EventSource`——两侧都没有新包。

需要一次性确认的外部条件：

```bash
# 8100 上的 llama-server（S3 的模型走这个端点；2.0 只连它，不管理它的进程）
curl -s http://127.0.0.1:8100/v1/models | head -c 200
# {"models":[{"name":"gemma4-qat-iq3xxs", ...}]}

# chinese-clip 权重（S2 已经在用）
ls -la ~/.cache/clip/clip_cn_v1-l-14.pt
```

> **通用规则（文档同步）**：凡是新增或删除 `apps/api/src/sentrix/` 下 `.py` 文件的任务，都必须同步更新 `docs/architecture/module-map.md` 中该文件的**整行**——路径 / 职责 / 层 / **依赖谁** / **被谁依赖**。本计划里这两列**由脚本按真实 import 图重算**（`gen_module_map.py`），不手工拼：S3 新增 25 个 `.py` 文件，手工维护两列依赖的出错概率接近 100%。
>
> **通用规则（包的 `__init__.py` 也是一条边）**：`import sentrix.domain.assistant.models` 这类子模块导入会被同时记成两条依赖——`sentrix/domain/assistant/models.py` **和** `sentrix/domain/assistant/__init__.py`。少加包 `__init__.py` 那行同样会红。S3 的 `domain/assistant/__init__.py`、`application/assistant/__init__.py`、`infrastructure/assistant/__init__.py` 都做成**纯包标记**（不 re-export），就是为了让这条边保持为空，不让每次新增模块都去改它。
>
> **通用规则（能力登记）**：每个新能力在 `docs/refactor/capability-map.jsonl` 里加一条记录，`status=verified` 时 `verification` 指向的测试文件必须真实存在。本切片新增 20 条（全表第 30–49 条）。
>
> **通用规则（OpenAPI）**：改了路由或 schema 就重新导出 `packages/contracts/openapi/sentrix-v1.yaml`（`scripts/export_openapi.py`），`make ci` 里带 `--check`。Task 42 与 Task 43 各需要一次。
>
> **通用规则（TDD）**：每个任务先只落地测试 → `Run` 确认红 → 再落地实现 → `Run` 确认绿。跳过红灯那一步，计划里剩下的就只是"打算做什么"。

---

## 文件结构

本切片新增/改动的文件（其余保持 S2 的原样）：

```text
sentrix-home-web2.0/
├── apps/api/
│   ├── src/sentrix/
│   │   ├── config.py                      +chat_model_name / chat_timeout_s
│   │   ├── main.py                        +注册 assistant 路由
│   │   ├── bootstrap/container.py         +chat_model / 会话 / 回合 / 结果集 / 人物目录 / 事件流
│   │   ├── api/
│   │   │   ├── routes/assistant.py        会话 CRUD + 建回合 + 回合事件的 SSE 流
│   │   │   └── schemas/assistant.py       请求响应模型与 SSE 事件载荷
│   │   ├── domain/assistant/
│   │   │   ├── models.py                  消息 / 工具调用 / 模型回复 / 进度事件 / 会话与回合记录
│   │   │   ├── profile.py                 回合预算（8 步 / 6 调用 / 180s / max_search_calls=1）
│   │   │   ├── task_state.py              计数器、进度流、终止原因、门禁标志位
│   │   │   ├── tools.py                   四个工具的契约（标准 JSON Schema + 证据绑定）
│   │   │   ├── tool_policy.py             执行 / 回放 / 拒绝的唯一决定点
│   │   │   ├── evidence_ledger.py         需求状态机与显式证据绑定
│   │   │   ├── result_set.py              结果集与分页；handle → asset_id
│   │   │   ├── intent.py                  确定性规划（无模型调用）
│   │   │   ├── final_guard.py             答案门禁 L1
│   │   │   ├── emergency.py               诚实兜底答复
│   │   │   └── ports.py                   六个端口
│   │   ├── application/assistant/
│   │   │   ├── tools.py                   四个工具的执行器（两份投影）
│   │   │   ├── run_turn.py                一次回合：规划 → 工具循环 → 账本 → 门禁 → 回答
│   │   │   └── manage_conversations.py    会话增删改查与确定性标题
│   │   └── infrastructure/
│   │       ├── assistant/
│   │       │   ├── chat_client.py         8100 的 OpenAI 兼容客户端
│   │       │   ├── events.py              进程内回合事件流
│   │       │   └── identity.py            S3 的人物目录（身份层属于 S5）
│   │       └── persistence/
│   │           ├── migrations/0004_assistant.sql
│   │           └── repositories/{conversations,turns,result_sets}.py
│   └── tests/{unit,integration,contract,e2e}/  （见各任务）
├── apps/web/src/
│   ├── features/assistant/{AssistantPanel.tsx,useTurnStream.ts}
│   ├── shared/api/client.ts               +助手端点与 SSE 地址
│   └── App.tsx · styles.css
└── packages/contracts/openapi/sentrix-v1.yaml
```

---

## 依赖图与执行顺序

先把顺序画出来再写代码。S0 踩过两次「两个任务循环依赖」（Task 4/5、Task 17/18），S2 的 Task 27/28 是并列的。S3 的依赖图如下——每一步只依赖它上面的东西，反向依赖一条都没有：

```text
Task 34  domain/assistant 地基：模型 / 预算 / 回合状态（纯规则）
   │
   ├── Task 35  证据账本 + 结果集（纯规则）
   │      │
   │      └── Task 36  工具契约（JSON Schema）+ 收敛策略
   │             │
   │             └── Task 37  确定性规划 + 答案门禁 + 诚实兜底
   │                    │
   │                    └── Task 38  端口 + 迁移 0004 + 三个 SQLite 仓储
   │                           │
   │                           ├── Task 39  8100 客户端 / 事件流 / 人物目录
   │                           │
   │                           └── Task 40  四个工具的执行器
   │                                  │
   │                                  └── Task 41  一次回合（工具循环 + 门禁）
   │                                         │
   │                                         └── Task 42  会话端点 + 建回合端点
   │                                                │
   │                                                └── Task 43  回合事件的 SSE 流
   │                                                       │
   │                                                       └── Task 44  Web 助手界面
   │                                                              │
   │                                                              └── Task 45  端到端验收
```

**为什么不能换序：**

| 约束 | 原因 |
|---|---|
| 34 必须最先 | 后面每个模块都要 `models` 里的 `ToolCall` / `ProgressEvent` / `TerminationReason` |
| 35 只依赖 34 | 账本与结果集都是纯数据 + 纯规则 |
| 36 在 35 之后 | `tools.py` 的 `satisfies` 直接引用 35 的 `EvidenceType`。**这两步的顺序是实测逼出来的**：先写工具再写账本时，Task 35 的测试在它自己的 commit 上收集失败（`ModuleNotFoundError: evidence_ledger`），逐任务验证时暴露 |
| 37 在 35/36 之后 | 门禁要用工具名单判断「检索过没有」，规划要产出 `EvidenceRequirement` |
| 38 在 37 之后 | 端口里的返回类型全是 34–37 定义的数据模型 |
| 39、40 都依赖 38 | 39 实现 `ChatModel` / `TurnEventBroker` / `PersonDirectory`，40 用 `ResultSetRepository` |
| 40 的 `AssistantToolbox` 依赖 `RetrieveVisual`（S2 已有） | 所以 40 要等到 39 之后排——不是因为 40 需要 39，而是为了让每个 commit 自洽 |
| 41 在 38/39/40 之后 | 它把状态、账本、门禁、模型、工具全部接起来 |
| 42 依赖 41 | 路由构造 `RunTurn` |
| 43 依赖 42 | SSE 端点在 `api/routes/assistant.py` 里，42 建了这个文件 |
| 44 依赖 43 | 前端订阅的正是 43 那条流 |
| 45 最后 | 它要的是完整的 HTTP 链路 + 真模型 |

---

## 移植清单：1.0 有什么、S3 要哪些

1.0 的助手逻辑集中在 `backend/agent_runtime/`（生产可达 29 个模块 / 11,394 行）+ `backend/agent_conversation.py` + `backend/app.py` 的助手路由段。**S3 不做整体移植**——设计 §2.4 列的 18 个「原计划漏掉的模块」里有相当一部分属于后面的切片。下表逐项判断。

### A. 移植（S3 需要）

| # | 能力 | 1.0 位置 | 2.0 落点 | 移植方式 |
|---|---|---|---|---|
| 1 | 回合状态与预算 | `runtime.py:1885-1936` 的 13 个裸计数器 + `budget_manager.py` | `domain/assistant/task_state.py` | **改**：收进一个对象，规则写在方法里 |
| 2 | 预算的 final_reserve 非对称 | `budget_manager.py:38-51` | `domain/assistant/task_state.py` | 原样：模型步只看墙钟，工具调用才检查 final_reserve |
| 3 | 工具收敛策略 | `tool_policy.py` + `runtime.py:2991-3082` | `domain/assistant/tool_policy.py` | **改**：把散在主循环的收敛全收进一个决定 |
| 4 | 工具契约 | `tool_registry.py:15-33` | `domain/assistant/tools.py` | **重写**：标准 JSON Schema + 原生 `tools` 字段 |
| 5 | 证据需求状态机 | `task_state.py`（planner 版，376 行） | `domain/assistant/evidence_ledger.py` | **缩**：11 个需求状态 → 3 个；只留 S3 用到的 3 类证据。**审计后保留**：1.0 自己的 `recompute_status` docstring 说那 11 个是 legacy、只用于回放旧数据；且 25,112 条轨迹里 `requirement_status_after` 一条都没记过。见 `s3-deviations.md` §3.2 |
| 6 | 「工具调用不等于证据」 | `requirement_completion.py:63-67` | `domain/assistant/evidence_ledger.py` 的 `satisfy` | 原样：需求必须显式绑定到产生它的调用 |
| 7 | 结果集与分页 | `result_set.py:73-125` | `domain/assistant/result_set.py` | 原样：6 张/页、1-based、handle 全局序号 |
| 8 | 答案门禁 L1 | `final_guard.py` | `domain/assistant/final_guard.py` | **缩**：保留安全 / 占位符 / 存在性矛盾，**不做语义等价**。**审计后：改成整张规则表的忠实移植**——补回 `viewer_escape` / `write_not_allowed` / `all_requested_but_has_more` / `delivery_contradiction` / `_ANY_DENIAL`，删掉自造的「证据门」。见 `s3-deviations.md` §1 第 6 项 |
| 9 | 存在性矛盾 | `final_guard.py:115-153` | 同上 | **改**：去掉 `_ANY_DENIAL` 的整句否定门槛 |
| 10 | 诚实兜底 | `emergency.py:34-84` | `domain/assistant/emergency.py` | **改**：按账本与检索事实分档，不再只有一条尾句 |
| 11 | 会话与消息 | `agent_conversation.py` + `db.py:918-952` | `infrastructure/persistence/repositories/conversations.py` | **改**：去掉 FTS 与 `content_json` 投影 |
| 12 | 轨迹与进度持久化 | `db.py:944-979` + `app.py` 的 `_TURN_JOBS` | `infrastructure/persistence/repositories/turns.py` | **改**：进度事件**落库**（1.0 只在内存里） |
| 13 | 结果集持久化 | `result_set.py:167-208` | `infrastructure/persistence/repositories/result_sets.py` | **改**：去掉内存副本，单一行来源 |
| 14 | 模型客户端 | `model_clients.py` 的 OpenAI 分支 | `infrastructure/assistant/chat_client.py` | **重写**：原生 `tools` / `tool_calls` |
| 15 | max_tokens 公式 | `app.py:2680-2692` | 同上 | 原样：`min(1501, 4400 - prompt_tokens)`，下限 64 |
| 16 | SSE 事件契约 | `app.py:2969-3004` | `api/routes/assistant.py` | **改**：加 `id:` 断线续传、`complete` 不再重发进度数组、`status` 用真实终态 |
| 17 | 四个工具 | `tools.py:1513-2112 / 2651-2678` | `application/assistant/tools.py` | **重写**：显式两份投影（内部 / 模型可见） |
| 18 | 交付口径 | `runtime.py:1404-1427` | 同上 | 原样：只交付模型显式选的图，不回填 |
| 19 | 会话端点 | `app.py:3082-3120` | `api/routes/assistant.py` | 原样语义，换到 `/api/v1` |
| 20 | 确定性标题 | `agent_conversation.py:107` | `application/assistant/manage_conversations.py` | 原样：`text[:24] + "…"`，不调模型 |

### B. 明确不移植（留给后面的切片）

| 能力 | 1.0 位置 | 留给谁 | 理由 |
|---|---|---|---|
| 双层防伪的**语义评审层**（L2 judge） | `judge.py` | ~~S6~~ **审计后：已回退，S3 交付** | ~~每次评审是一次模型调用；L1 已兜住安全与结构，S3 的模型预算要留给主循环~~ **证据强度：无。1.0 生产库有 1,669 条 `agent2_trace.quality.l2_soft_release`（6.6%，全是生产默认 profile），L2 确实在跑。见 `s3-deviations.md` §3.3** |
| 恢复作答的四级阶梯 | `runtime.py:2807-2960` | **S6** | S3 只做两级：门禁提醒一次 + 确定性兜底。阶梯要等 L2 到位才有上游信号 |
| JIT 提示词 | `jit_prompt.py` | **S6** | 它的价值在「按未决需求收缩工具集」；S3 只有 4 个工具，收缩没有收益 |
| 语义拆槽 | `semantic_slots.py` | **S6** | 与六路召回一起做才有意义 |
| 确定性事实核 / 硬值绑定 | `answer_nucleus.py` | **S6** | 依赖结构化事实查询 |
| `inspect_photo`（视觉复核） | `tools.py:2116-2267` | **S6** | 需要 VLM 多模态链路 |
| `read_photo_text`（OCR） | `ocr_tool.py` | **S6** | 638 行的独立子系统 |
| `query_memory_facts`（结构化统计） | `tools.py:207-302` | **S6** | 依赖实体与结构化事实表 |
| `query_photo_people`（照片身份） | `tools.py:2548-2601` | **S5** | 强依赖人脸簇与实体 |
| `get_core_memory`（长期记忆卡片） | `tools.py:2465-2528` | **S5** | 与 `get_person_profile` 共用 entities 底座 |
| `search_conversation_history` | `tools.py:2363-2408` | **S7** | 需要会话检索（FTS） |
| 会话摘要 | `conversation_summary.py` | **S7** | S3 用最近 8 条原文，够用 |
| `completion.py` 的五码门 | `completion.py` | 被取代 | 它的五个码全是正则意图 + planner 桥接，S3 用「规划 + 账本」直接表达 |
| ResultSet 授权交付 URL | `app.py:2484-2503` | ~~不做~~ **审计后：已回退，S3 交付** | ~~2.0 的 web 侧本来就看得到 `asset_id`~~ **那是「现在不需要」不是「1.0 做错了」。证据强度：无。见 `s3-deviations.md` §1** |
| 政策拒答正则 | `runtime.py:1725` `_detect_policy_refusal` | ~~不做~~ **审计后：已回退，S3 交付** | ~~那是 benchmark 时代的产物~~ **证据强度：无。生产库 212 次命中（0.84%），是注入/人脸向量外泄/越权写的确定性边界。见 `s3-deviations.md` §1** |
| `agent_contracts.py` | 392 行 | 冻结 | 设计 §1.3-D 已冻结，静态不可达 |

### C. 1.0 的四个助手失败模式，S3 分别怎么处理

| 失败模式 | 1.0 实测 | 根因 | S3 的处理 |
|---|---|---|---|
| `planner_call_error` | 1,974 次，最终落成 `planner_blocked` 1,445 条轨迹 | 规划是一次模型调用（`goal_planner.py:129`），超时/连不上就整轮以「无法可靠规划」收场 | ~~**规划改成纯函数**（Task 37）~~ **审计后：回退。** 那 1,974 次里 97.6% 落在 15 个「该分钟 100% 失败、延迟中位数 0.13 秒」的黑障窗口里，同窗口别的模型调用记的是 `[Errno 111] Connection refused`；非 PhotoBench 口径 0/763。**规划仍是一次模型调用**，见 `s3-deviations.md` §2 |
| 零工具调用 | 7,346 / 25,069 = **29.3%** | planner 允许「零需求」（`goal_planner.py:228`），JIT 提示词随即告诉模型「可直接输出 final」（`jit_prompt.py:200`） | **规划永远至少给一条证据需求**；门禁在需求还开着时拦一次（Task 37/41） |
| `termination_reason` 读不到 | 11,304 条（键不存在）+ 72 条空串 | 两条路：2026-09-10 之前的 `save_trajectory` 不带这个键；政策拒答那条提前 `return` 绕过了 `_classify_termination`。而 `_classify_termination` 在收尾处**无条件覆盖**，13 个 reason 里 8 个是死代码 | `TurnResult.termination_reason` 的类型就是枚举（非 `str`、非 `str | None`），收尾只有一个出口，外层还有兜底（Task 34/41） |
| 前端轮询代替 SSE | `GET /api/assistant/turn/{id}` **668,552 次，SSE 端点只有 62 次** | ~~`es.onerror` 一触发就永久降级成 700ms 轮询~~ **调用方是评测机**：`services/photobench` 的 `wait_for_assistant_turn` 每 0.5 秒轮询（`benchmark_orchestrator.py:881`），`EventSource` 对它不可用 | 进度事件**落库**（Task 38）→ SSE 断线可续（Task 43）→ 前端用一条 EventSource（Task 44）**并保留 1.0 的轮询回退**（Task 44 补）；~~轮询端点在 2.0 不存在，契约测试断言它 404~~ **审计后：端点已搬回**，见 `s3-deviations.md` §4 |

---

# S3 — 助手闭环（Task 34–45）

## Task 34: 助手域地基（模型 / 预算 / 回合状态）

**依赖：** 无。这是 S3 的起点。

**Files:**
- Create: `apps/api/src/sentrix/domain/assistant/__init__.py`
- Create: `apps/api/src/sentrix/domain/assistant/models.py`
- Create: `apps/api/src/sentrix/domain/assistant/profile.py`
- Create: `apps/api/src/sentrix/domain/assistant/task_state.py`
- Create: `apps/api/tests/unit/test_assistant_task_state.py`
- Modify: `apps/api/tests/conftest.py`（加 `make_state` fixture，后面几个任务共用）
- Modify: `docs/architecture/module-map.md`
- Modify: `docs/refactor/capability-map.jsonl`

先把**纯数据与纯规则**立起来：一次回合有哪些字段、预算是多少、什么时候算结束。

预算的三个数字来自 `docs/refactor/behaviour-baseline.md` 的冻结项（8 步 / 6 调用 / 180s），第四个（`max_search_calls=1`）来自 `runtime.py:1895` 的硬收敛——1.0 实测小模型会反复改 query 重搜，把工具预算耗光后以空答案收场（e8c9d4 那次 35 题里 52%）。

`TerminationReason` 是这一任务里最要紧的一处设计：**枚举里没有 None，`TurnResult` 的这个字段类型就是它**。1.0 的 25,112 条轨迹里有 11,304 条读不到这个字段（2026-09-10 之前的 `save_trajectory` 不带这个键），另有 72 条是政策拒答那条提前 `return` 绕过了收尾分类器——而没有任何东西会因此报错。2.0 让漏赋值变成类型错误。

- [ ] **Step 1: 先写失败测试**

`apps/api/tests/unit/test_assistant_task_state.py`:

```python
"""回合状态、预算与终止原因的单元测试。

这些断言钉的是 ``docs/refactor/behaviour-baseline.md`` 里冻结的助手预算（8 步 / 6 调用 /
180s / ``max_search_calls=1``），以及 1.0 三个失败模式在 2.0 里的结构性修法。
"""

from __future__ import annotations

import pytest

from sentrix.domain.assistant.models import (
    ProgressStage,
    ProgressStatus,
    TerminationReason,
    ToolArgumentError,
    ToolCall,
)
from sentrix.domain.assistant.profile import DEFAULT_PROFILE, TurnProfile
from sentrix.domain.assistant.task_state import TERMINAL, TaskState, ToolCallRecord


def make_state(profile: TurnProfile = DEFAULT_PROFILE) -> TaskState:
    """本文件自用；跨文件的版本在 ``conftest.py`` 的 ``make_state`` fixture 里。"""
    return TaskState(
        turn_id="turn_1",
        conversation_id="conv_1",
        scope_id="album1",
        question="给我看看在海边的照片",
        profile=profile,
    )


def test_profile_matches_behaviour_baseline():
    assert DEFAULT_PROFILE.name == "goal_driven_candidate"
    assert DEFAULT_PROFILE.max_steps == 8
    assert DEFAULT_PROFILE.max_tool_calls == 6
    assert DEFAULT_PROFILE.wall_time_s == 180.0
    assert DEFAULT_PROFILE.max_search_calls == 1


def test_termination_reason_has_no_empty_member():
    """1.0 的 25,112 条轨迹里有 11,304 条读不到这个字段。

    根因有两条：2026-09-10 之前的轨迹根本没写这个键，以及政策拒答那条提前 return 绕过了
    收尾分类器（72 条空串）。2.0 的防线是类型——``TurnResult.termination_reason`` 的类型
    就是这个枚举，不是 ``str``、更不是 ``str | None``。这里钉住「每个成员都有非空的
    字符串值」，任何新加的成员都不会退化成空串。
    """
    values = [reason.value for reason in TerminationReason]
    assert all(values)
    assert len(set(values)) == len(values)


def test_emit_allocates_step_index_itself():
    """``step_index`` 由 emit 分配，调用方无从指定——1.0 靠每个发出点自己数。"""
    state = make_state()

    first = state.emit(ProgressStage.PLANNING, ProgressStatus.COMPLETE, "一")
    second = state.emit(ProgressStage.THINKING, ProgressStatus.RUNNING, "二")

    assert (first.step_index, second.step_index) == (1, 2)
    assert [event.step_index for event in state.progress] == [1, 2]


def test_emit_records_a_timestamp_and_payload():
    state = make_state()

    event = state.emit(ProgressStage.PLANNING, ProgressStatus.COMPLETE, "已确认需要哪些证据。", kind="plan")

    # 不断言具体时间（它是机器时钟的函数），只钉住格式与 kind 的传递。
    assert event.timestamp[:2] == "20"
    assert "T" in event.timestamp
    assert event.kind == "plan"


def test_model_step_budget_ignores_the_final_reserve():
    """``can_step`` 只看墙钟是否走完；``can_call_tool`` 才检查 final_reserve。

    1.0 的这个非对称是刻意的（``budget_manager.py:38-51``）：最后 10 秒要留给写答案，
    不能拿去调工具。反过来把 final_reserve 也套到模型步上，就会退化成「预算耗尽后空答案」。
    """
    state = make_state()
    state.started_at -= DEFAULT_PROFILE.wall_time_s - DEFAULT_PROFILE.final_reserve_s / 2

    assert state.wall_remaining() < DEFAULT_PROFILE.final_reserve_s
    assert state.can_step() is True
    assert state.can_call_tool() is False


def test_step_budget_stops_at_the_profile_limit():
    state = make_state()
    state.model_steps = DEFAULT_PROFILE.max_steps

    assert state.can_step() is False


def test_tool_budget_counts_only_successful_calls():
    """被拒绝的调用与缓存回放都不计预算——1.0 只在执行成功后才 ``record_tool_call``。"""
    state = make_state()
    for status in ("ok", "ok", "denied", "error", "replayed"):
        state.record(ToolCallRecord(call_id="c", tool="search_memories", signature=status, status=status))

    assert state.tool_calls == 2


def test_search_calls_counts_every_issued_search():
    """``search_calls`` 数的是「模型发出了几次 search_memories」，被拒的那次也要算。

    1.0 的计数器在**每次** search action 上自增（含被拒的那次，``runtime.py:2992``）。
    不算的话，模型可以靠「先被拒一次、再重发」绕过 max_search_calls。
    """
    state = make_state()
    state.record(ToolCallRecord(call_id="c1", tool="search_memories", signature="a", status="ok"))
    state.record(ToolCallRecord(call_id="c2", tool="search_memories", signature="b", status="denied"))

    assert state.search_calls == 2


def test_signature_ignores_argument_order():
    left = TaskState.signature("get_result_page", {"page": 2, "result_set_id": "rs_1"})
    right = TaskState.signature("get_result_page", {"result_set_id": "rs_1", "page": 2})

    assert left == right


def test_cached_returns_only_successful_records():
    state = make_state()
    state.record(ToolCallRecord(call_id="c1", tool="search_memories", signature="s", status="error"))

    assert state.cached("s") is None

    state.record(
        ToolCallRecord(
            call_id="c2",
            tool="search_memories",
            signature="s",
            status="ok",
            model_visible={"result_set_id": "rs_1"},
        )
    )

    assert state.cached("s") is not None


def test_replayed_is_false_until_a_replay_is_recorded():
    state = make_state()

    assert state.replayed("s") is False
    state.record(ToolCallRecord(call_id="c1", tool="search_memories", signature="s", status="replayed"))
    assert state.replayed("s") is True


def test_consecutive_failures_stops_at_a_different_tool():
    state = make_state()
    state.record(
        ToolCallRecord(
            call_id="c1", tool="search_memories", signature="a", status="error", failure_reason="empty_query"
        )
    )
    state.record(
        ToolCallRecord(
            call_id="c2", tool="search_memories", signature="b", status="error", failure_reason="empty_query"
        )
    )
    state.record(
        ToolCallRecord(
            call_id="c3", tool="get_result_page", signature="c", status="error", failure_reason="empty_query"
        )
    )

    assert state.consecutive_failures("search_memories", "empty_query") == 0
    assert state.consecutive_failures("get_result_page", "empty_query") == 1


def test_finish_records_the_reason_and_closes_the_phase():
    state = make_state()

    assert state.termination_reason is None
    assert state.finish(TerminationReason.ANSWERED) is TerminationReason.ANSWERED
    assert state.termination_reason is TerminationReason.ANSWERED
    assert state.phase == TERMINAL


def test_block_guard_code_fires_once_per_code():
    """门禁只提醒一次。1.0 的零检索拦截没有标志位，模型坚持不调工具就能烧光 8 个模型步。"""
    state = make_state()

    assert state.block_guard_code("evidence_gate") is True
    assert state.block_guard_code("evidence_gate") is False
    assert state.block_guard_code("placeholder_leak") is True
    # 拦下不等于改变阶段——阶段由主循环切，标志位只管「这条规则提过醒了没有」。
    assert state.guard_blocked_codes == {"evidence_gate", "placeholder_leak"}


def test_tool_call_parses_a_json_object():
    call = ToolCall(call_id="c1", name="search_memories", arguments_json='{"query": "海边"}')

    assert call.parse() == {"query": "海边"}


def test_tool_call_rejects_broken_json():
    call = ToolCall(call_id="c1", name="search_memories", arguments_json="{oops")

    with pytest.raises(ToolArgumentError):
        call.parse()


def test_tool_call_rejects_a_non_object_payload():
    call = ToolCall(call_id="c1", name="search_memories", arguments_json="[1, 2]")

    with pytest.raises(ToolArgumentError):
        call.parse()


def test_empty_arguments_parse_to_an_empty_mapping():
    assert ToolCall(call_id="c1", name="search_memories").parse() == {}
```

`apps/api/tests/conftest.py`（新增的 `make_state` fixture；其余保持 S2 原样）：

```python
import dataclasses
import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sentrix.config import Settings
from sentrix.domain.retrieval import scheme
from sentrix.infrastructure.persistence.database import Database
from sentrix.main import create_app


class FakeVisualEmbedder:
    """契约测试用的确定性嵌入器。

    真模型是进程内 GPU 推理、权重 1.6GB：放进 make ci 会让契约测试依赖一台带显卡
    和权重的机器，而契约测试要验的是 HTTP 映射，不是模型。真链路的验收在
    tests/e2e（Task 33），那里用真模型 + 真 Qdrant。
    """

    model_id = scheme.IMAGE_MODEL
    dimension = 8

    @staticmethod
    def _vector(seed: str) -> list[float]:
        digest = hashlib.sha256(seed.encode("utf-8")).digest()
        return [digest[index] / 255.0 for index in range(8)]

    def embed_image(self, path) -> list[float]:
        return self._vector(Path(path).read_bytes().hex())

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    monkeypatch.setenv("SENTRIX_DATA_DIR", str(tmp_path))
    return Settings()


@pytest.fixture
def client(settings) -> TestClient:
    """装配好的应用，但把视觉嵌入器换成确定性假件。

    Container 是 frozen dataclass，用 dataclasses.replace 换掉一个适配器，
    其余装配路径（真 SQLite、真迁移、真 Qdrant）保持原样。
    """
    app = create_app(settings)
    app.state.container = dataclasses.replace(app.state.container, visual_embedder=FakeVisualEmbedder())
    return TestClient(app)


@pytest.fixture
def make_state():
    """构造一个回合状态。默认参数够大多数用例用，需要时逐字段改。"""
    from sentrix.domain.assistant.profile import DEFAULT_PROFILE
    from sentrix.domain.assistant.task_state import TaskState

    def _make(**overrides):
        fields = {
            "turn_id": "turn_1",
            "conversation_id": "conv_1",
            "scope_id": "album1",
            "question": "给我看看在海边的照片",
            "profile": DEFAULT_PROFILE,
        }
        fields.update(overrides)
        return TaskState(**fields)

    return _make


@pytest.fixture
def migrations_ready_db(tmp_path) -> Database:
    """真 SQLite + 真迁移文件，跑在临时目录里。"""
    migrations = Path(__file__).resolve().parents[1] / "src/sentrix/infrastructure/persistence/migrations"
    db = Database(tmp_path / "sentrix.db", migrations)
    db.migrate()
    return db
```

- [ ] **Step 2: 运行测试，确认红灯**

Run: `.venv/bin/pytest apps/api/tests/unit/test_assistant_task_state.py -q -p no:warnings`

Expected:

```text
E   ModuleNotFoundError: No module named 'sentrix.domain.assistant.models'
ERROR apps/api/tests/unit/test_assistant_task_state.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
```

- [ ] **Step 3: 实现 domain/assistant 的地基**

`apps/api/src/sentrix/domain/assistant/__init__.py`（**纯包标记**，不 re-export：`scripts/check_module_map.py` 会把包 `__init__.py` 也记成一条依赖边，让它保持为空就不必每加一个模块都去改它）：

```python
"""助手域：一次问答回合的规则、证据账本与答案门禁。纯规则，零 IO。

1.0 把这一整套埋在 3,449 行的 ``runtime.py`` 里——规则、模型调用、SQLite 与 HTTP
搅在一起，于是「为什么这一轮没调工具」这种问题只能靠读三千行代码回答。2.0 按职责
拆开：回合状态与收敛策略留在 domain；模型端点、向量索引与会话存储各自在
infrastructure 里实现 domain 的端口。
"""
```

`apps/api/src/sentrix/domain/assistant/models.py`:

```python
"""助手域的通用模型：消息、工具调用、模型回复、进度事件、会话与回合记录。

纯数据，零 IO。这里刻意不认识 HTTP、也不认识 SQLite——模型端点由 infrastructure
适配，domain 只声明它需要什么形状的回复。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class ChatRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class TurnStatus(StrEnum):
    """回合的状态。

    1.0 只有 ``complete`` / ``cancelled`` / ``error`` 三种对外状态，而 ``error`` 只在
    Python 未捕获异常时才出现——模型失败、超时、guard 拦截全部被 ``app.py:3433`` 压成
    ``complete``。前端拿到的每一个失败都是一个「成功」的帧。这里把业务终态显式列出来。
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self not in {TurnStatus.PENDING, TurnStatus.RUNNING}


class TerminationReason(StrEnum):
    """回合为什么结束。**这个枚举里没有 None**。

    1.0 的 25,112 条轨迹里有 11,304 条读不到 ``termination_reason``。根因不是某一处
    忘了赋值，而是两条独立的路：2026-09-10 之前的 ``save_trajectory`` 根本不带这个键
    （11,304 条的 result_json 只有 answer/intent/telemetry 三个键），以及政策拒答那条
    提前 ``return`` 绕过了 ``_classify_termination``（72 条空串）。更糟的是
    ``_classify_termination`` 在收尾处**无条件覆盖**，13 个精心设置的 reason 里 8 个
    是死代码，而没有任何东西会因此报错。

    2.0 的做法：``TurnResult.termination_reason`` 的类型就是这个枚举（不是 ``str``、
    更不是 ``str | None``），漏赋值是类型错误而不是运行时的沉默；并且**没有任何一处
    覆盖它**——每个出口自己声明为什么结束。
    """

    ANSWERED = "answered"
    ANSWERED_WITH_GAP = "answered_with_gap"
    EMPTY_ANSWER = "empty_answer"
    NO_TOOL_CALL_STOPPED = "no_tool_call_stopped"
    TOOL_BUDGET_EXHAUSTED = "tool_budget_exhausted"
    STEP_BUDGET_EXHAUSTED = "step_budget_exhausted"
    TIMEOUT = "timeout"
    MODEL_ERROR = "model_error"
    GUARD_BLOCKED = "guard_blocked"
    CANCELLED = "cancelled"


class ProgressStage(StrEnum):
    """进度事件的阶段。取值集与设计 §4.3 一致，沿用 1.0 已验证的那一套。"""

    THINKING = "thinking"
    PLANNING = "planning"
    INSPECTING = "inspecting"
    ACTION = "action"
    TOOL_RESULT = "tool_result"
    RECOVERING = "recovering"
    FINALIZING = "finalizing"
    TERMINAL = "terminal"


class ProgressStatus(StrEnum):
    RUNNING = "running"
    OK = "ok"
    DENIED = "denied"
    PARTIAL = "partial"
    COMPLETE = "complete"
    CANCELLED = "cancelled"


class ToolArgumentError(ValueError):
    """模型给的参数不是合法的 JSON 对象。"""


@dataclass(frozen=True, slots=True)
class ToolCall:
    """模型请求的一次工具调用。

    ``arguments_json`` 保持模型给的原始字符串而不是就地解析：解析失败本身是有信息量的
    事件（1.0 的 ``parse_failure`` 就出自这里），调用方需要知道「模型写了什么」而不是
    只看到一句「参数非法」。
    """

    call_id: str
    name: str
    arguments_json: str = ""

    def parse(self) -> Mapping[str, Any]:
        raw = self.arguments_json.strip() or "{}"
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ToolArgumentError(str(error)) from error
        if not isinstance(parsed, dict):
            raise ToolArgumentError(f"参数必须是 JSON 对象，收到 {type(parsed).__name__}")
        return parsed


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """一条发往模型的消息。

    ``tool_calls`` 与 ``tool_call_id`` 是原生 OpenAI 工具协议的两个方向：assistant
    消息带 ``tool_calls``，随后那条 ``role=tool`` 的消息用 ``tool_call_id`` 指回它。
    1.0 把动作序列化成普通文本、把工具结果塞进 user 消息（``runtime.py:2108`` 的注释
    解释了原因），代价是一整套 JSON 修复器与「输出无法解析」的收尾。
    """

    role: ChatRole
    content: str = ""
    tool_call_id: str = ""
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelReply:
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """一条面向用户的进度事件。

    字段集沿用 1.0（``text`` / ``status`` / ``stage`` / ``step_index`` / ``timestamp``）
    并补上 ``kind`` 与 ``payload``。``step_index`` 在**回合内**从 1 单调递增，跨回合不唯一
    ——1.0 的前端拿它当 key 时必须先限定回合作用域，2.0 把它同时用作 SSE 的 ``id:``
    行，于是 ``Last-Event-ID`` 断线续传天然可用。
    """

    step_index: int
    stage: ProgressStage
    status: ProgressStatus
    text: str
    kind: str = "status"
    timestamp: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Attachment:
    """交付给用户的一张可打开的照片。"""

    handle: str
    asset_id: str
    file_name: str


@dataclass(frozen=True, slots=True)
class Conversation:
    id: str
    scope_id: str
    title: str
    state: str
    created_at: datetime
    updated_at: datetime
    last_message_at: datetime


@dataclass(frozen=True, slots=True)
class Message:
    id: str
    conversation_id: str
    scope_id: str
    turn_id: str
    role: ChatRole
    content: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TurnRecord:
    """一次回合的持久化投影。``termination_reason`` 为空串表示「还没结束」。"""

    id: str
    conversation_id: str
    scope_id: str
    question: str
    status: TurnStatus
    created_at: datetime
    updated_at: datetime
    answer: str = ""
    termination_reason: str = ""
    attachments: tuple[Attachment, ...] = ()
    requirements: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class PersonProfile:
    """一位已确认人物的档案。S3 只有家庭角色这一格，其余在 S5 落地。

    1.0 的 ``get_person_profile`` 返回 membership / relationships / patterns /
    recent_events / claims / profile_text 七块（``tools.py:2665-2678``），全部来自
    entities / person_patterns / person_event_memory / semantic_claims 四张表——那些表
    在 2.0 属于 S5。**工具的描述因此必须同步收窄**：1.0 的描述承诺了「常去地点/常做
    活动/常同行、近期事件与语义声明」，照抄到 S3 就是让模型去问一堆拿不到的数据。
    """

    name: str
    family_role: str
    confirmed: bool
```

`apps/api/src/sentrix/domain/assistant/profile.py`:

```python
"""回合预算。数值取自 ``docs/refactor/behaviour-baseline.md`` 的冻结项。

1.0 的 ``ProfileConfig``（``agent_runtime/profile.py:13``）把这些值连同 6 套 profile
放在一个 dict 里，由 ``SENTRIX_AGENT_PROFILE`` 环境变量选；未知名字**静默回退**到默认
profile，不打日志。2.0 只保留生产在跑的那一套，写死成常量——没有 env、没有「选错了也
不报错」的入口。

两处与 1.0 的刻意差异：

1. ``wall_time_s`` 不再被就地改写。1.0 在检测到 OCR / 多图意图时把预算对象里的墙钟
   放宽到 240s，而轨迹里记的仍是 profile 的 180s（``runtime.py:2130``）——事后看数据
   永远对不上。要改预算就改这个文件，那是一次有版本、可审查的变更。
2. ``final_reserve_s`` 只留给「写答案」。1.0 的 ``can_tool_call`` 检查它、
   ``can_model_step`` 不检查（``budget_manager.py:38-51``），这个非对称是对的，保留。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TurnProfile:
    name: str
    max_steps: int
    max_tool_calls: int
    max_search_calls: int
    wall_time_s: float
    final_reserve_s: float


# goal_driven_candidate：1.0 的生产默认 profile。
DEFAULT_PROFILE = TurnProfile(
    name="goal_driven_candidate",
    max_steps=8,
    max_tool_calls=6,
    # 1.0 runtime.py:1895 的硬收敛。实测小模型会反复改 query 重搜，把工具预算耗光后
    # 以空答案收场（e8c9d4 的 35 题里 52%）；收敛到 1 次后逼它翻页或直接作答。
    max_search_calls=1,
    wall_time_s=180.0,
    final_reserve_s=10.0,
)
```

`apps/api/src/sentrix/domain/assistant/task_state.py`:

```python
"""回合状态：计数器、进度流与终止原因。

1.0 把这些散在 ``AgentRuntime.run()`` 的局部变量里——``search_call_count``、
``inspected_handles``、``parse_retries``、``guard_retries``、``recent_tool_failures``
等十三个裸 int/bool（``runtime.py:1885-1936``），每个都靠相邻代码维持不变量。2.0 收进
一个对象：规则写在方法里，越界由方法拒绝，而不是靠「记得在那行加个判断」。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sentrix.domain.assistant.models import (
    ProgressEvent,
    ProgressStage,
    ProgressStatus,
    TerminationReason,
)
from sentrix.domain.assistant.profile import TurnProfile

# 阶段用模块级字符串常量而不是枚举：它只影响日志可读性，不参与任何判定。
PLANNING = "planning"
ACTING = "acting"
FINALIZING = "finalizing"
TERMINAL = "terminal"


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    """一次工具调用的结果。``signature`` 是「工具名 + 规范化参数」的指纹，用来识别
    重复调用；``model_visible`` 是真正发回模型的那份投影。"""

    call_id: str
    tool: str
    signature: str
    status: str
    failure_reason: str = ""
    observation: Mapping[str, Any] = field(default_factory=dict)
    model_visible: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class TaskState:
    turn_id: str
    conversation_id: str
    scope_id: str
    question: str
    profile: TurnProfile
    started_at: float = field(default_factory=time.monotonic)
    phase: str = PLANNING
    model_steps: int = 0
    records: list[ToolCallRecord] = field(default_factory=list)
    progress: list[ProgressEvent] = field(default_factory=list)
    guard_blocked_codes: set[str] = field(default_factory=set)
    termination_reason: TerminationReason | None = None

    # ---- 进度 ---------------------------------------------------------------

    def emit(
        self,
        stage: ProgressStage,
        status: ProgressStatus,
        text: str,
        *,
        kind: str = "status",
        payload: Mapping[str, Any] | None = None,
    ) -> ProgressEvent:
        """追加一条进度事件并返回它。

        ``step_index`` 由这里统一分配（``len(progress) + 1``），调用方无从跳过或重复——
        1.0 靠每个发出点自己数，是「跨回合不唯一」之外还多一层「同一回合内可能撞号」。
        """
        event = ProgressEvent(
            step_index=len(self.progress) + 1,
            stage=stage,
            status=status,
            text=text,
            kind=kind,
            timestamp=datetime.now().astimezone().isoformat(timespec="seconds"),
            payload=payload or {},
        )
        self.progress.append(event)
        return event

    # ---- 预算 ---------------------------------------------------------------

    @property
    def tool_calls(self) -> int:
        """计入预算的工具调用次数。

        被拒绝的调用与缓存回放都不计——1.0 也是这个口径（``tool_policy.py:61-62`` 只在
        执行成功后才 ``record_tool_call``）：拒绝不是执行，不该再罚一次预算。
        """
        return sum(1 for record in self.records if record.status == "ok")

    @property
    def search_calls(self) -> int:
        return sum(1 for record in self.records if record.tool == "search_memories")

    def can_step(self) -> bool:
        """还能不能再问一次模型。

        与 1.0 一致：模型步只看墙钟有没有走完，**不检查 final_reserve**
        （``budget_manager.py:38-44``）。把最后 10 秒留给工具调用而不是写答案，正是
        1.0 实测出的「预算耗尽后空答案」的成因。
        """
        return self.model_steps < self.profile.max_steps and self.wall_remaining() > 0

    def can_call_tool(self) -> bool:
        return self.tool_calls < self.profile.max_tool_calls and self.has_final_reserve()

    def wall_remaining(self) -> float:
        return self.profile.wall_time_s - (time.monotonic() - self.started_at)

    def has_final_reserve(self) -> bool:
        return self.wall_remaining() > self.profile.final_reserve_s

    # ---- 工具调用记录 -------------------------------------------------------

    def record(self, record: ToolCallRecord) -> None:
        self.records.append(record)

    @staticmethod
    def signature(tool: str, arguments: Mapping[str, Any]) -> str:
        """重复调用的判据。键排序后再序列化，参数顺序不同的同一次调用算同一签名。"""
        parts = "&".join(f"{key}={arguments[key]!r}" for key in sorted(arguments))
        return f"{tool}({parts})"

    def cached(self, signature: str) -> ToolCallRecord | None:
        for record in reversed(self.records):
            if record.signature == signature and record.status == "ok":
                return record
        return None

    def replayed(self, signature: str) -> bool:
        """这个签名是否已经回放过一次。

        回放只能给一次：模型拿着同一份结果反复要，等于把模型步烧在一个不会变的答案上。
        第二次同样的调用直接拒，逼它作答或换动作。
        """
        return any(record.signature == signature and record.status == "replayed" for record in self.records)

    def consecutive_failures(self, tool: str, reason: str) -> int:
        """同一工具、同一失败原因**连续**出现了几次。

        1.0 是 ``runtime.py:3013-3027`` 的一段裸循环，上限 2 之后拦下并逼模型换动作。
        """
        count = 0
        for record in reversed(self.records):
            if record.tool != tool:
                break
            if record.failure_reason != reason:
                break
            count += 1
        return count

    # ---- 收尾 ---------------------------------------------------------------

    def finish(self, reason: TerminationReason) -> TerminationReason:
        """声明回合为何结束。整个回合里只该被调用一次。"""
        self.termination_reason = reason
        self.phase = TERMINAL
        return reason

    def block_guard_code(self, code: str) -> bool:
        """标记某个门禁规则已经拦过一次，返回「这次是新拦的」。

        1.0 的零检索硬拦没有这个标志位（``runtime.py:2568``），于是模型只要一直不调
        工具，这段就会一直 ``continue``，把 8 个模型步全部烧光——29% 的零工具调用里
        有相当一部分是这么来的。门禁只提醒一次，提醒过就放行。
        """
        if code in self.guard_blocked_codes:
            return False
        self.guard_blocked_codes.add(code)
        return True
```

三处与 1.0 的**刻意差异**，都写进了各自的 docstring：

1. `TerminationReason` 没有 `None`（上面已说）。
2. `can_step()` 只看墙钟、不检查 `final_reserve`；`can_call_tool()` 才检查。这个非对称是 1.0 刻意的设计（`budget_manager.py:38-51`），原样保留。
3. `block_guard_code()` 是 1.0 缺的那个标志位：那道零检索拦截没有它，模型坚持不调工具就会被拦到预算耗尽（29% 零工具调用的来源之一）。

- [ ] **Step 4: 运行测试，确认绿灯**

Run: `.venv/bin/pytest apps/api/tests/unit/test_assistant_task_state.py -p no:warnings`

Expected:

```text
18 passed
```

- [ ] **Step 5: 更新 module-map.md**

`docs/architecture/module-map.md` 里这四行的**依赖两列由脚本按真实 import 图重算**，不手工拼（S3 一共新增 25 个 `.py`、改动 40 多行的依赖列，手工维护的出错概率接近 100%）。本任务新增：

```markdown
| `sentrix/domain/assistant/__init__.py` | 包标记 | domain | — | — |
| `sentrix/domain/assistant/models.py` | 助手域的通用模型：ChatMessage / ToolCall / ModelReply / ProgressEvent / TurnRecord | domain | — | `sentrix/domain/assistant/task_state.py` |
| `sentrix/domain/assistant/profile.py` | 回合预算常量（8 步 / 6 调用 / 180s / max_search_calls=1），来自行为基线 | domain | — | `sentrix/domain/assistant/task_state.py` |
| `sentrix/domain/assistant/task_state.py` | 回合状态：计数器、进度流、终止原因与门禁标志位 | domain | `sentrix/domain/assistant/models.py`、`sentrix/domain/assistant/profile.py` | — |
```

Run: `.venv/bin/python scripts/check_module_map.py`
Expected: `module-map: 0 个问题`

- [ ] **Step 6: 登记能力**

追加到 `docs/refactor/capability-map.jsonl`（全表第 30 条）：

```json
{"capability": "回合状态与预算", "origin_kind": "migrated", "source_commit": "2d2cee7c", "source_path": "backend/agent_runtime/runtime.py", "source_symbol": "AgentRuntime.run", "target_path": "apps/api/src/sentrix/domain/assistant/task_state.py", "verification": "apps/api/tests/unit/test_assistant_task_state.py", "status": "verified"}
```

Run: `.venv/bin/python scripts/check_capability_map.py`
Expected: `capability-map: 0 个问题`

- [ ] **Step 7: 全量门禁**

Run: `make ci`
Expected:

```text
Contracts: 5 kept, 0 broken.
module-map: 0 个问题
capability-map: 0 个问题
openapi 契约一致
ci: 全部通过
```

（`make ci` 这一条每个任务都要跑。后面的任务不再重复贴 Expected 的覆盖率数字，只贴实际结果。）

---

## Task 35: 证据账本与结果集

**依赖：** Task 34。

**Files:**
- Create: `apps/api/src/sentrix/domain/assistant/evidence_ledger.py`
- Create: `apps/api/src/sentrix/domain/assistant/result_set.py`
- Create: `apps/api/tests/unit/test_assistant_ledger.py`
- Modify: `docs/architecture/module-map.md`
- Modify: `docs/refactor/capability-map.jsonl`

账本回答「证据够不够」，结果集回答「取到的照片是哪几张、怎么翻页」。两者都是纯数据 + 纯规则，而且**必须先于工具契约落地**——`ToolSpec.satisfies` 直接引用这里的 `EvidenceType`。顺序反过来会在 Task 35 自己的 commit 上红（实测就是这么发现的，见「验证记录」）。

**账本**对应 1.0 `task_state.py` 的 `RequirementState` + `evidence_ledger.py`，但缩到 S3 需要的 3 类证据。一条 1.0 里最干净的纪律原样保留：`requirement_completion.py:63-67` 写的「仅共享 evidence type 的证据是有效遥测，但它不能隐式满足需求」——2.0 把它落成 `ToolSpec.satisfies` 的显式声明。

**结果集**保留 1.0 已经跑通的三条不变量：每页 6 张、1-based、`page_size` 硬顶 6；`handle` 是**全局序号**（第 2 页从 `photo_7` 开始）；`asset_id` 只存在于结果集里，不进模型上下文。第三条让「模型只能交付它真的看过的图」成为结构性事实。

- [ ] **Step 1: 先写失败测试**

`apps/api/tests/unit/test_assistant_ledger.py`:

```python
"""证据账本与结果集的单元测试。

账本的两条纪律来自 1.0：需求必须有显式的证据来源（``task_state.py:243`` 的
``mark_satisfied`` 在 refs 为空时直接 raise），以及「仅共享 evidence type 不算满足需求」
（``requirement_completion.py:63``）。结果集的三条不变量来自 1.0 已经跑通的分页
（``result_set.py:116``）。
"""

from __future__ import annotations

import pytest

from sentrix.domain.assistant.evidence_ledger import (
    EvidenceLedger,
    EvidenceRequirement,
    EvidenceType,
    RequirementStatus,
)
from sentrix.domain.assistant.result_set import PAGE_SIZE, ResultSet


def ledger_with(*evidence_types: EvidenceType) -> EvidenceLedger:
    return EvidenceLedger(
        scope_id="album1",
        requirements=[
            EvidenceRequirement(id=f"r{index}", evidence_type=evidence_type, description=f"需求 {index}")
            for index, evidence_type in enumerate(evidence_types)
        ],
    )


# ---- 账本 -------------------------------------------------------------------


def test_a_ledger_must_be_bound_to_a_scope():
    """混用两个 scope 的记忆是数据泄漏，不是业务错误，必须在记账那一刻就拒绝。"""
    with pytest.raises(ValueError):
        EvidenceLedger(scope_id="")


def test_satisfying_requires_an_evidence_source():
    """1.0 的 ``mark_satisfied`` 在 ``evidence_refs`` 为空时直接 raise。"""
    ledger = ledger_with(EvidenceType.PHOTO_SET)

    with pytest.raises(ValueError):
        ledger.satisfy(EvidenceType.PHOTO_SET, call_id="")


def test_satisfying_records_the_call_that_produced_the_evidence():
    ledger = ledger_with(EvidenceType.PHOTO_SET)

    satisfied = ledger.satisfy(EvidenceType.PHOTO_SET, call_id="call_1")

    assert satisfied == ("r0",)
    assert ledger.requirement("r0").satisfied_by == ("call_1",)
    assert ledger.is_complete is True


def test_a_second_call_is_appended_not_replaced():
    """同一类证据可以来自多次调用（首页 + 翻页），来源全部留痕。"""
    ledger = ledger_with(EvidenceType.PHOTO_SET)

    ledger.satisfy(EvidenceType.PHOTO_SET, call_id="call_1")
    ledger.satisfy(EvidenceType.PHOTO_SET, call_id="call_2")

    assert ledger.requirement("r0").satisfied_by == ("call_1", "call_2")


def test_satisfying_an_unrelated_type_changes_nothing():
    """反向验证：只声明同一类证据的工具才有资格满足它，别的调用不能顺带把需求划掉。"""
    ledger = ledger_with(EvidenceType.DELIVERED_PHOTO)

    assert ledger.satisfy(EvidenceType.PHOTO_SET, call_id="call_1") == ()
    assert ledger.is_complete is False


def test_blocking_closes_a_requirement_with_a_reason():
    ledger = ledger_with(EvidenceType.PERSON_PROFILE)

    blocked = ledger.block(EvidenceType.PERSON_PROFILE, reason="identity_not_available")

    assert blocked == ("r0",)
    assert ledger.requirement("r0").status is RequirementStatus.BLOCKED
    assert ledger.requirement("r0").unmet_reason == "identity_not_available"
    # 封口不是满足：门禁不该再逼模型去取，但收尾要如实说明缺什么。
    assert ledger.is_complete is False
    assert ledger.open_requirements() == ()


def test_blocking_requires_a_reason():
    with pytest.raises(ValueError):
        ledger_with(EvidenceType.PERSON_PROFILE).block(EvidenceType.PERSON_PROFILE, reason="")


def test_open_requirements_only_lists_required_ones():
    ledger = EvidenceLedger(
        scope_id="album1",
        requirements=[
            EvidenceRequirement(
                id="optional",
                evidence_type=EvidenceType.PERSON_PROFILE,
                description="可选的",
                required=False,
            )
        ],
    )

    assert ledger.open_requirements() == ()
    assert ledger.is_complete is True


def test_payload_is_the_projection_that_goes_into_progress_events():
    ledger = ledger_with(EvidenceType.PHOTO_SET)
    ledger.satisfy(EvidenceType.PHOTO_SET, call_id="call_1")

    payload = list(ledger.as_payload())

    assert payload[0]["evidence_type"] == "photo_set"
    assert payload[0]["status"] == "satisfied"
    assert payload[0]["satisfied_by"] == ["call_1"]


# ---- 结果集 -----------------------------------------------------------------


def make_result_set(count: int) -> ResultSet:
    from datetime import UTC, datetime

    return ResultSet(
        id="rs_1",
        scope_id="album1",
        query="海边",
        asset_ids=tuple(f"asset_{index}" for index in range(count)),
        created_at=datetime(2026, 9, 22, tzinfo=UTC),
    )


def test_the_first_page_holds_six_photos():
    page = make_result_set(10).first_page()

    assert PAGE_SIZE == 6
    assert page.page == 1
    assert [entry.handle for entry in page.entries] == [f"photo_{i}" for i in range(1, 7)]
    assert page.total == 10
    assert page.has_more is True
    assert page.remaining == 4


def test_handles_are_global_ordinals_not_page_ordinals():
    """第 2 页从 ``photo_7`` 开始。模型只需要记住 ``photo_N``，跨页稳定。"""
    result_set = make_result_set(10)

    assert [entry.handle for entry in result_set.page(2).entries] == [f"photo_{i}" for i in range(7, 11)]


def test_page_size_is_capped_at_six():
    """``page_size`` 传多大都只给 6 张——1.0 的 ``min(_RESULT_PAGE_SIZE, ...)``。"""
    page = make_result_set(20).page(1, page_size=100)

    assert page.page_size == PAGE_SIZE
    assert len(page.entries) == PAGE_SIZE


def test_a_page_past_the_end_is_empty_and_not_an_error():
    """越界不是错误，是「没有更多了」。抛异常会让模型把翻到底当成系统故障。"""
    page = make_result_set(3).page(9)

    assert page.entries == ()
    assert page.has_more is False
    assert page.remaining == 0


def test_resolve_maps_a_handle_back_to_its_asset():
    result_set = make_result_set(3)

    assert result_set.resolve("photo_2") == "asset_1"
    assert result_set.handle_for("asset_1") == "photo_2"


def test_resolve_rejects_a_handle_outside_the_result_set():
    """1.0 的授权端点正是靠这一步把「模型给的 handle」与「真实资产」隔开。"""
    result_set = make_result_set(3)

    assert result_set.resolve("photo_9") is None
    assert result_set.resolve("asset_1") is None
    assert result_set.resolve("photo_x") is None
```

- [ ] **Step 2: 运行测试，确认红灯**

Run: `.venv/bin/pytest apps/api/tests/unit/test_assistant_ledger.py -q -p no:warnings`

Expected:

```text
E   ModuleNotFoundError: No module named 'sentrix.domain.assistant.evidence_ledger'
ERROR apps/api/tests/unit/test_assistant_ledger.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
```

- [ ] **Step 3: 实现 evidence_ledger 与 result_set**

`apps/api/src/sentrix/domain/assistant/evidence_ledger.py`:

```python
"""证据账本：把「回答这个问题最少需要哪些证据」变成可判定的状态机。

对应 1.0 ``agent_runtime/task_state.py`` 的 ``RequirementState`` +
``evidence_ledger.py``，但只保留 S3 需要的那部分。

一条 1.0 里最干净的纪律原样保留：**工具调用不等于证据**。1.0 的
``requirement_completion.py:63-67`` 写得最直白——「仅共享 evidence type 的证据是有效
遥测，但它不能隐式满足需求」。2.0 把它落成工具契约里的显式声明：工具的 ``ToolSpec``
列出它能满足哪几类证据（``satisfies``），账本只认这个映射，不按工具名猜。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class EvidenceType(StrEnum):
    """S3 用到的公共证据类型。

    1.0 的九类公共证据里，S3 只需要三类：

    - ``photo_set``：取到了一组候选照片（``search_memories`` / ``get_result_page``）；
    - ``delivered_photo``：把其中某几张标记为「要交付给用户」（``get_original_photos``）；
    - ``person_profile``：读到了已确认人物的档案（``get_person_profile``）。

    1.0 的一个真实缺口正好在 ``delivered_photo`` 这一格：``completion.py`` 有
    ``DELIVER_MEDIA`` 需求码，但 ``requirement_completion.py`` 的
    ``_AGENT2_EVIDENCE_CODES`` 只映射了检索/视觉/OCR 三个，而生产默认 profile
    （``agent2_authoritative``）下 ``completion.blocking()`` 被旁路——于是「用户要看图」
    这条需求在权威模式下**没有任何门禁保护**。2.0 把它变成一等证据类型。
    """

    PHOTO_SET = "photo_set"
    DELIVERED_PHOTO = "delivered_photo"
    PERSON_PROFILE = "person_profile"


class RequirementStatus(StrEnum):
    OPEN = "open"
    SATISFIED = "satisfied"
    BLOCKED = "blocked"


@dataclass
class EvidenceRequirement:
    id: str
    evidence_type: EvidenceType
    description: str
    required: bool = True
    status: RequirementStatus = RequirementStatus.OPEN
    satisfied_by: tuple[str, ...] = ()
    unmet_reason: str = ""


@dataclass
class EvidenceLedger:
    """一次回合的证据需求清单。

    ``scope_id`` 与 1.0 一样进构造函数并被强校验：账本混用两个 scope 的记忆是
    数据泄漏，不是业务错误，必须在记账那一刻就拒绝。
    """

    scope_id: str
    requirements: list[EvidenceRequirement] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.scope_id:
            raise ValueError("证据账本必须绑定一个 scope_id")

    def requirement(self, requirement_id: str) -> EvidenceRequirement | None:
        for item in self.requirements:
            if item.id == requirement_id:
                return item
        return None

    def open_requirements(self) -> tuple[EvidenceRequirement, ...]:
        return tuple(
            item for item in self.requirements if item.required and item.status is RequirementStatus.OPEN
        )

    @property
    def is_complete(self) -> bool:
        """**所有**必填需求都被满足。被 blocked 的不算——封口不是满足。

        这一条与 ``open_requirements`` 是两个问题：前者问「门禁还要不要逼模型取证」，
        后者问「这一轮证据齐了吗」。
        """
        return all(item.status is RequirementStatus.SATISFIED for item in self.requirements if item.required)

    def satisfy(self, evidence_type: EvidenceType, *, call_id: str) -> tuple[str, ...]:
        """一次成功的工具调用满足了哪些需求。返回被满足的需求 id。

        ``call_id`` 是必填的：没有证据来源的「已满足」在 1.0 里被显式禁止
        （``task_state.py:243-253`` 的 ``mark_satisfied`` 在 refs 为空时直接 raise）。
        一个需求可以被多次调用满足，来源全部留痕。
        """
        if not call_id:
            raise ValueError("满足一个证据需求必须带上产生它的工具调用 id")
        satisfied: list[str] = []
        for item in self.requirements:
            if item.evidence_type is not evidence_type:
                continue
            # 已经满足的需求也继续记来源：一次检索 + 一次翻页都产出了照片证据，
            # 审计时该看得到两次。上限由工具预算兜住（每回合最多 6 次调用）。
            item.status = RequirementStatus.SATISFIED
            item.satisfied_by = (*item.satisfied_by, call_id)
            item.unmet_reason = ""
            satisfied.append(item.id)
        return tuple(satisfied)

    def block(self, evidence_type: EvidenceType, *, reason: str) -> tuple[str, ...]:
        """把某类证据标成「本轮拿不到」，并记下原因。

        ``blocked`` 与「还开着」是两件事：开着意味着还可以再试，blocked 意味着已经
        确认这一轮拿不到——门禁只对前者逼模型继续取证。
        """
        if not reason:
            raise ValueError("封口一个证据需求必须写明原因")
        blocked: list[str] = []
        for item in self.requirements:
            if item.evidence_type is evidence_type and item.status is RequirementStatus.OPEN:
                item.status = RequirementStatus.BLOCKED
                item.unmet_reason = reason
                blocked.append(item.id)
        return tuple(blocked)

    def as_payload(self) -> Sequence[Mapping[str, Any]]:
        """进 ``progress`` 事件与轨迹的投影。"""
        return tuple(
            {
                "id": item.id,
                "evidence_type": item.evidence_type.value,
                "description": item.description,
                "status": item.status.value,
                "satisfied_by": list(item.satisfied_by),
                "unmet_reason": item.unmet_reason,
            }
            for item in self.requirements
        )
```

`apps/api/src/sentrix/domain/assistant/result_set.py`:

```python
"""结果集：一次检索的有序候选，以及它在对话里的分页视图。

对应 1.0 ``agent_runtime/result_set.py``。三条不变量原样保留，它们在 1.0 里被证明是
对的：

1. **每页 6 张、1-based、无游标、``page_size`` 硬顶 6**；
2. **``handle`` 是全局序号**（第 2 页从 ``photo_7`` 开始），跨页稳定——模型只需要记住
   ``photo_N``，分页不会让同一个 handle 指向不同的图；
3. **``asset_id`` 只存在于结果集里**，不进模型上下文。模型看到的是 handle，真正可交付
   的资产由运行时解引用。1.0 为此在 ``runtime.py:177-179`` 专门把 ``asset_id`` 从模型
   可见的观察里剥掉。

``handle`` → ``asset_id`` 的映射保留在结果集这一侧，于是「模型只能交付它真的看过的图」
是一个结构性事实，而不是靠提示词约束。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

PAGE_SIZE = 6


@dataclass(frozen=True, slots=True)
class ResultSetEntry:
    handle: str
    asset_id: str


@dataclass(frozen=True, slots=True)
class ResultSetPage:
    page: int
    page_size: int
    total: int
    has_more: bool
    remaining: int
    entries: tuple[ResultSetEntry, ...]


@dataclass(frozen=True, slots=True)
class ResultSet:
    id: str
    scope_id: str
    query: str
    asset_ids: tuple[str, ...]
    created_at: datetime

    def page(self, page_no: int, page_size: int = PAGE_SIZE) -> ResultSetPage:
        """第 ``page_no`` 页（1-based）。页码越界返回空页，不抛异常。

        1.0 也是这个行为（``tools.py:2073``）：越界不是错误，是「没有更多了」。抛异常
        只会让模型把一次正常的翻到底当成系统故障。
        """
        size = min(PAGE_SIZE, max(1, page_size))
        start = max(0, (max(1, page_no) - 1) * size)
        end = start + size
        entries = tuple(
            ResultSetEntry(handle=f"photo_{start + index + 1}", asset_id=asset_id)
            for index, asset_id in enumerate(self.asset_ids[start:end])
        )
        return ResultSetPage(
            page=max(1, page_no),
            page_size=size,
            total=len(self.asset_ids),
            has_more=end < len(self.asset_ids),
            remaining=max(0, len(self.asset_ids) - end),
            entries=entries,
        )

    def first_page(self) -> ResultSetPage:
        return self.page(1)

    def resolve(self, handle: str) -> str | None:
        """``photo_N`` → ``asset_id``；不在本结果集里返回 ``None``。

        1.0 的授权端点正是靠这一步把「模型给的 handle」与「真实资产」隔开
        （``app.py:2484``，handle 必须属于该结果集且 scope 匹配）。
        """
        if not handle.startswith("photo_"):
            return None
        suffix = handle[len("photo_") :]
        if not suffix.isdigit():
            return None
        index = int(suffix) - 1
        if 0 <= index < len(self.asset_ids):
            return self.asset_ids[index]
        return None

    def handle_for(self, asset_id: str) -> str | None:
        for index, candidate in enumerate(self.asset_ids):
            if candidate == asset_id:
                return f"photo_{index + 1}"
        return None
```

`is_complete` 与 `open_requirements()` 是**两个问题**：前者问「这一轮证据齐了吗」（被 blocked 的不算齐），后者问「门禁还要不要逼模型继续取证」（blocked 的不再逼）。1.0 把这两件事混在一个 `status` 上，结果是「封口」与「满足」分不开。

- [ ] **Step 4: 运行测试，确认绿灯**

Run: `.venv/bin/pytest apps/api/tests/unit/test_assistant_ledger.py -p no:warnings`

Expected:

```text
15 passed
```

- [ ] **Step 5: 更新 module-map.md**

```markdown
| `sentrix/domain/assistant/evidence_ledger.py` | 证据账本：需求状态机与显式证据绑定 | domain | — | — |
| `sentrix/domain/assistant/result_set.py` | 结果集与分页规则；handle 到 asset_id 的映射 | domain | — | — |
```

Run: `.venv/bin/python scripts/check_module_map.py`
Expected: `module-map: 0 个问题`

- [ ] **Step 6: 登记能力**

```json
{"capability": "证据账本与需求状态机", "origin_kind": "migrated", "source_commit": "2d2cee7c", "source_path": "backend/agent_runtime/task_state.py", "source_symbol": "TaskState.recompute_status", "target_path": "apps/api/src/sentrix/domain/assistant/evidence_ledger.py", "verification": "apps/api/tests/unit/test_assistant_ledger.py", "status": "verified"}
{"capability": "结果集与分页", "origin_kind": "migrated", "source_commit": "2d2cee7c", "source_path": "backend/agent_runtime/result_set.py", "source_symbol": "ResultSet.page", "target_path": "apps/api/src/sentrix/domain/assistant/result_set.py", "verification": "apps/api/tests/unit/test_assistant_ledger.py", "status": "verified"}
```

Run: `.venv/bin/python scripts/check_capability_map.py`
Expected: `capability-map: 0 个问题`

- [ ] **Step 7: 全量门禁**

Run: `make ci`

---

## Task 36: 四个工具的契约与收敛策略

**依赖：** Task 34（`models.py` 的 `ToolCall`）、Task 35（`EvidenceType`）。

**Files:**
- Create: `apps/api/src/sentrix/domain/assistant/tools.py`
- Create: `apps/api/src/sentrix/domain/assistant/tool_policy.py`
- Create: `apps/api/tests/unit/test_assistant_policy.py`
- Modify: `docs/architecture/module-map.md`
- Modify: `docs/refactor/capability-map.jsonl`

两个模块合在一个任务里：`tools.py` 说「有哪些工具、长什么样」，`tool_policy.py` 说「这一次调用放不放行」。它们共享同一份工具名单，分开落地会让中间那一步无法独立验证。

**参数是标准 JSON Schema，走原生 `tools` 字段。** 1.0 的 `ToolSpec.input_schema` 是「示例默认值」的 dict（`{"query": "", "mode": "best|all|representative"}`，`tool_registry.py:19`），被 `json.dumps` 之后拼进提示词的文本里（`runtime.py:1544`）——模型看到的是一段不像 schema 的文字，真正的契约藏在 handler 手写的 `arguments.get(...)` 里。2.0 实测 8100 的 llama.cpp（gemma4 模板）原生支持 `tools` / `finish_reason="tool_calls"`，于是直接用原生协议：**没有 JSON 修复器，没有「输出无法解析」的收尾**。

**收敛规则全部收进一个决定。** 1.0 把 `max_search_calls`、同图复核一次、同失败两次换动作、重复调用走缓存散在主循环的十几个裸计数器里（`runtime.py:1891-1936` 与 `:2991-3082`）。2.0 让「为什么这次调用没执行」有唯一答案。

- [ ] **Step 1: 先写失败测试**

`apps/api/tests/unit/test_assistant_policy.py`:

```python
"""四个工具的契约与收敛策略的单元测试。

收敛规则全部来自 1.0 实测出的失败：小模型反复改 query 重搜直到工具预算耗尽、同一张图
死磕到底、同样的错误重试到没预算。1.0 把这些散在主循环的十几个裸计数器里，2.0 收进
一个决定。
"""

from __future__ import annotations

from sentrix.domain.assistant.evidence_ledger import EvidenceType
from sentrix.domain.assistant.models import ToolCall
from sentrix.domain.assistant.task_state import ToolCallRecord
from sentrix.domain.assistant.tool_policy import ToolAction, ToolPolicy
from sentrix.domain.assistant.tools import TOOL_SPECS, TOOLS_BY_NAME, openai_tools


def call(name: str, **arguments: object) -> ToolCall:
    import json

    return ToolCall(call_id=f"call_{name}", name=name, arguments_json=json.dumps(arguments))


def test_the_four_tools_are_the_ones_s3_ships():
    assert [spec.name for spec in TOOL_SPECS] == [
        "search_memories",
        "get_result_page",
        "get_original_photos",
        "get_person_profile",
    ]


def test_every_tool_declares_the_evidence_it_satisfies():
    """1.0 的 ``produces_evidence`` 只是一句声明，没有任何地方校验工具真的产出了它。

    2.0 用它驱动账本——「工具调用不等于证据」这条纪律（``requirement_completion.py:63``）
    在 2.0 的落点就是这张显式的绑定表。
    """
    assert TOOLS_BY_NAME["search_memories"].satisfies == (EvidenceType.PHOTO_SET,)
    assert TOOLS_BY_NAME["get_original_photos"].satisfies == (EvidenceType.DELIVERED_PHOTO,)
    assert TOOLS_BY_NAME["get_person_profile"].satisfies == (EvidenceType.PERSON_PROFILE,)


def test_parameters_are_real_json_schema_not_example_values():
    """1.0 的 ``input_schema`` 是示例 dict（``{"query": "", "mode": "best|all|..."}``），
    被 json.dumps 之后拼进提示词——模型看到的是不像 schema 的一段字。这里钉住标准形态。"""
    for spec in TOOL_SPECS:
        assert spec.parameters["type"] == "object"
        assert spec.parameters["additionalProperties"] is False
        assert set(spec.parameters["required"]) == set(spec.required)
        for name in spec.required:
            assert name in spec.parameters["properties"]


def test_openai_tools_wraps_each_spec_as_a_function():
    tools = openai_tools()

    assert len(tools) == 4
    assert tools[0]["type"] == "function"
    assert tools[0]["function"]["name"] == "search_memories"
    assert tools[0]["function"]["parameters"] == dict(TOOLS_BY_NAME["search_memories"].parameters)


def test_a_valid_call_is_executed_with_parsed_arguments(make_state):
    decision = ToolPolicy().decide(call("search_memories", query="海边"), make_state())

    assert decision.action is ToolAction.EXECUTE
    assert decision.arguments == {"query": "海边"}
    assert decision.signature == "search_memories(query='海边')"


def test_an_unknown_tool_is_denied_with_the_available_names(make_state):
    decision = ToolPolicy().decide(call("delete_everything"), make_state())

    assert decision.action is ToolAction.DENY
    assert decision.code == "unknown_tool"
    assert "search_memories" in decision.reason


def test_broken_json_is_denied_not_raised(make_state):
    state = make_state()
    broken = ToolCall(call_id="c1", name="search_memories", arguments_json="{oops")

    decision = ToolPolicy().decide(broken, state)

    assert decision.action is ToolAction.DENY
    assert decision.code == "bad_arguments"


def test_a_missing_required_argument_is_denied(make_state):
    decision = ToolPolicy().decide(call("get_result_page", page=1), make_state())

    assert decision.action is ToolAction.DENY
    assert decision.code == "missing_argument"
    assert "result_set_id" in decision.reason


def test_search_memories_cannot_be_called_twice(make_state):
    state = make_state()
    policy = ToolPolicy()
    first = call("search_memories", query="海边")
    assert policy.decide(first, state).action is ToolAction.EXECUTE
    state.record(ToolCallRecord(call_id="c1", tool="search_memories", signature="x", status="ok"))

    second = policy.decide(call("search_memories", query="海边2"), state)

    assert second.action is ToolAction.DENY
    assert second.code == "search_call_limit"
    assert "get_result_page" in second.reason


def test_search_limit_does_not_lapse_when_the_model_budget_is_gone(make_state):
    """1.0 的拦截条件里带着 ``and turn.budget.can_model_step()``（``runtime.py:2993``），
    模型步耗尽之后这个 ``and`` 短路，**第二次 search 会被真的执行**。2.0 的计数与预算
    分开，不存在这个短路。

    反向验证：把模型步耗光、工具预算也耗光，拦截仍然成立。
    """
    state = make_state()
    state.model_steps = state.profile.max_steps
    state.record(ToolCallRecord(call_id="c1", tool="search_memories", signature="x", status="ok"))

    decision = ToolPolicy().decide(call("search_memories", query="海边2"), state)

    assert decision.action is ToolAction.DENY
    assert decision.code == "search_call_limit"


def test_a_duplicate_call_replays_the_cached_observation(make_state):
    state = make_state()
    policy = ToolPolicy()
    state.record(
        ToolCallRecord(
            call_id="c1",
            tool="search_memories",
            signature="search_memories(query='海边')",
            status="ok",
            model_visible={"result_set_id": "rs_1"},
        )
    )

    decision = policy.decide(call("search_memories", query="海边"), state)

    assert decision.action is ToolAction.REPLAY
    assert decision.replay == {"result_set_id": "rs_1"}


def test_a_second_duplicate_is_denied(make_state):
    """回放只能给一次：模型拿着同一份结果反复要，等于把模型步烧在一个不会变的答案上。"""
    state = make_state()
    policy = ToolPolicy()
    signature = "search_memories(query='海边')"
    state.record(
        ToolCallRecord(
            call_id="c1",
            tool="search_memories",
            signature=signature,
            status="ok",
            model_visible={"result_set_id": "rs_1"},
        )
    )
    state.record(ToolCallRecord(call_id="c2", tool="search_memories", signature=signature, status="replayed"))

    decision = policy.decide(call("search_memories", query="海边"), state)

    assert decision.action is ToolAction.DENY
    assert decision.code == "duplicate_call_stopped"


def test_tool_budget_exhaustion_denies(make_state):
    state = make_state()
    for index in range(state.profile.max_tool_calls):
        state.record(
            ToolCallRecord(call_id=f"c{index}", tool="get_result_page", signature=f"s{index}", status="ok")
        )

    decision = ToolPolicy().decide(call("get_result_page", result_set_id="rs_1", page=2), state)

    assert decision.action is ToolAction.DENY
    assert decision.code == "tool_budget_exhausted"


def test_the_same_failure_twice_in_a_row_forces_a_change(make_state):
    state = make_state()
    for index in range(2):
        state.record(
            ToolCallRecord(
                call_id=f"c{index}",
                tool="get_result_page",
                signature=f"s{index}",
                status="error",
                failure_reason="unknown_result_set",
            )
        )

    decision = ToolPolicy().decide(call("get_result_page", result_set_id="rs_1", page=2), state)

    assert decision.action is ToolAction.DENY
    assert decision.code == "repeated_failure"


def test_a_single_failure_does_not_block_the_retry(make_state):
    """反向验证：只失败一次时不该拦——否则模型没有第二次机会修正参数。"""
    state = make_state()
    state.record(
        ToolCallRecord(
            call_id="c1",
            tool="get_result_page",
            signature="s",
            status="error",
            failure_reason="unknown_result_set",
        )
    )

    decision = ToolPolicy().decide(call("get_result_page", result_set_id="rs_1", page=2), state)

    assert decision.action is ToolAction.EXECUTE


def test_denied_calls_do_not_consume_the_tool_budget(make_state):
    state = make_state()
    policy = ToolPolicy()
    for index in range(5):
        policy.decide(call(f"nope_{index}"), state)
        state.record(ToolCallRecord(call_id=f"c{index}", tool="nope", signature=f"s{index}", status="denied"))

    assert state.tool_calls == 0
    assert policy.decide(call("search_memories", query="海边"), state).action is ToolAction.EXECUTE
```

- [ ] **Step 2: 运行测试，确认红灯**

Run: `.venv/bin/pytest apps/api/tests/unit/test_assistant_policy.py -q -p no:warnings`

Expected:

```text
E   ModuleNotFoundError: No module named 'sentrix.domain.assistant.tools'
ERROR apps/api/tests/unit/test_assistant_policy.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
```

- [ ] **Step 3: 实现 tools 与 tool_policy**

`apps/api/src/sentrix/domain/assistant/tools.py`:

```python
"""S3 上线的四个工具的契约：名字、描述、参数 schema、能满足哪类证据。

对应 1.0 ``agent_runtime/tool_registry.py`` + ``tools.py`` 的注册段（``tools.py:2681``）。
两处与 1.0 的**结构性差异**：

1. **参数是标准 JSON Schema，走原生 ``tools`` 字段。** 1.0 的 ``ToolSpec.input_schema``
   是「示例默认值」的 dict（``{"query": "", "mode": "best|all|representative"}``），被
   ``json.dumps`` 之后拼进提示词的文本里（``runtime.py:1544``）——模型看到的是一段不像
   schema 的文字，而真正的契约藏在 handler 的手写 ``arguments.get(...)`` 里。
   实测 8100 的 llama.cpp（gemma4 模板）原生支持 ``tools`` / ``finish_reason="tool_calls"``，
   所以 2.0 直接用原生协议：没有 JSON 修复器，没有「输出无法解析」的收尾。
2. **``satisfies`` 是必填的。** 1.0 的 ``produces_evidence`` 只是声明，从没有任何地方
   校验工具真的产出了它声称的证据。2.0 用它驱动账本（见 ``evidence_ledger.py``）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sentrix.domain.assistant.evidence_ledger import EvidenceType


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: Mapping[str, Any]
    satisfies: tuple[EvidenceType, ...]
    required: tuple[str, ...] = ()

    def as_openai_tool(self) -> Mapping[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.parameters),
            },
        }


def _object_schema(properties: Mapping[str, Any], required: Sequence[str]) -> Mapping[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


SEARCH_MEMORIES = ToolSpec(
    name="search_memories",
    description=(
        "按内容词检索家庭照片（人/物/场景/衣着/颜色/地点特征）。"
        "返回一个结果集摘要，其中 preview 是排在最前的几张，每条带 handle 与拍摄时间。"
        "一次对话只需要检索一次；要看更多候选用 get_result_page 翻页，不要改词重搜。"
        "要把照片展示给用户，必须先调 get_original_photos 把 preview 里的 handle 交出去。"
    ),
    parameters=_object_schema(
        {
            "query": {
                "type": "string",
                "description": "检索用的内容词，例如 海边、桥、猫、红色。用用户问题里的实词，不要写整句。",
            },
            "limit": {
                "type": "integer",
                "description": "结果集最多容纳多少张候选，默认 12。",
                "minimum": 1,
                "maximum": 12,
            },
        },
        ["query"],
    ),
    satisfies=(EvidenceType.PHOTO_SET,),
    required=("query",),
)

GET_RESULT_PAGE = ToolSpec(
    name="get_result_page",
    description=(
        "查看指定结果集的某一页（每页最多 6 张，page 从 1 开始）。"
        "result_set_id 用 search_memories 返回的那个；要换检索条件请重新调用 search_memories。"
    ),
    parameters=_object_schema(
        {
            "result_set_id": {"type": "string", "description": "search_memories 返回的结果集 id。"},
            "page": {"type": "integer", "description": "页码，从 1 开始。", "minimum": 1},
        },
        ["result_set_id", "page"],
    ),
    satisfies=(EvidenceType.PHOTO_SET,),
    required=("result_set_id", "page"),
)

GET_ORIGINAL_PHOTOS = ToolSpec(
    name="get_original_photos",
    description=(
        "把选中的照片交付给用户查看。handles 必须来自当前结果集的 preview（例如 photo_1）。"
        "用户说「给我看看…」时必须在给出最终回答之前调用本工具，否则用户看不到任何照片。"
    ),
    parameters=_object_schema(
        {
            "result_set_id": {"type": "string", "description": "search_memories 返回的结果集 id。"},
            "handles": {
                "type": "array",
                "items": {"type": "string"},
                "description": "要交付的 handle 列表，最多 6 个，必须来自该结果集的 preview。",
                "minItems": 1,
                "maxItems": 6,
            },
        },
        ["result_set_id", "handles"],
    ),
    satisfies=(EvidenceType.DELIVERED_PHOTO,),
    required=("result_set_id", "handles"),
)

GET_PERSON_PROFILE = ToolSpec(
    name="get_person_profile",
    description=(
        "读取一位**已确认人物**的家庭角色。2.0 目前只有这一项人物数据；"
        "人物关系、常去地点、性格等尚未建立，问到时如实说明数据不足，不要编造。"
    ),
    parameters=_object_schema(
        {"person": {"type": "string", "description": "人物名字，例如 妈妈、浦思航。"}},
        ["person"],
    ),
    satisfies=(EvidenceType.PERSON_PROFILE,),
    required=("person",),
)

TOOL_SPECS: tuple[ToolSpec, ...] = (
    SEARCH_MEMORIES,
    GET_RESULT_PAGE,
    GET_ORIGINAL_PHOTOS,
    GET_PERSON_PROFILE,
)

TOOLS_BY_NAME: Mapping[str, ToolSpec] = {spec.name: spec for spec in TOOL_SPECS}

# 能产出一组照片的检索工具。门禁用它判断「有没有检索过」。
RETRIEVAL_TOOLS = frozenset({"search_memories", "get_result_page"})


def openai_tools(specs: Sequence[ToolSpec] = TOOL_SPECS) -> tuple[Mapping[str, Any], ...]:
    return tuple(spec.as_openai_tool() for spec in specs)
```

`apps/api/src/sentrix/domain/assistant/tool_policy.py`:

```python
"""工具策略：一次工具调用要么被执行，要么被拒绝，要么命中缓存。

对应 1.0 ``agent_runtime/tool_policy.py``。1.0 的 ``authorize`` 只做四件事（白名单 /
只读 / readiness / 预算），其余收敛全部散在 runtime 主循环的十几个裸计数器里。2.0 把
它们**全部收进这一个决定**——于是「为什么这次调用没执行」有唯一答案，而不是要去主循环
里逐行找是哪一个 ``continue`` 拦下的。

1.0 实测出的四条收敛规则，逐条保留：

- ``search_memories`` 每回合最多 1 次（``runtime.py:1895``）。1.0 的实现有个边界漏洞：
  拦截条件里带了 ``and turn.budget.can_model_step()``，预算耗尽后拦截失效，**第二次
  search 会被真的执行**。这里把计数与预算分开，不存在这个短路。
- 同工具同失败原因连续 2 次 → 拦下并逼模型换动作（``runtime.py:3013``）。
- 完全相同的调用走缓存回放，不重复执行、不计预算（``runtime.py:3035``）。
- 预算耗尽 → 拒绝（``tool_policy.py:37``）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from sentrix.domain.assistant.models import ToolArgumentError, ToolCall
from sentrix.domain.assistant.task_state import TaskState
from sentrix.domain.assistant.tools import TOOLS_BY_NAME

# 同工具同失败原因连续几次之后强制换动作。
MAX_CONSECUTIVE_FAILURES = 2


class ToolAction(StrEnum):
    EXECUTE = "execute"
    REPLAY = "replay"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class ToolDecision:
    action: ToolAction
    code: str = "ok"
    reason: str = ""
    arguments: Mapping[str, Any] = field(default_factory=dict)
    signature: str = ""
    replay: Mapping[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.action is not ToolAction.DENY


class ToolPolicy:
    """无状态：所有计数都在 ``TaskState`` 上。"""

    def decide(self, call: ToolCall, state: TaskState) -> ToolDecision:
        spec = TOOLS_BY_NAME.get(call.name)
        if spec is None:
            return ToolDecision(
                action=ToolAction.DENY,
                code="unknown_tool",
                reason=(f"没有名为 {call.name} 的工具。可用工具：" + "、".join(sorted(TOOLS_BY_NAME))),
            )

        try:
            arguments = call.parse()
        except ToolArgumentError as error:
            return ToolDecision(
                action=ToolAction.DENY, code="bad_arguments", reason=f"参数不是合法 JSON：{error}"
            )

        missing = [name for name in spec.required if not arguments.get(name)]
        if missing:
            return ToolDecision(
                action=ToolAction.DENY,
                code="missing_argument",
                reason=f"{call.name} 缺少必填参数：{'、'.join(missing)}",
            )

        signature = TaskState.signature(call.name, arguments)
        cached = state.cached(signature)
        if cached is not None and state.replayed(signature):
            return ToolDecision(
                action=ToolAction.DENY,
                code="duplicate_call_stopped",
                reason="同一个调用已经回放过一次，结果不会变，请基于它作答。",
            )
        if cached is not None:
            # 1.0 也是回缓存 + 提示直接作答（``runtime.py:3035``）：重复调用不该白烧预算，
            # 但也不能让模型以为工具返回了「新东西」。
            return ToolDecision(
                action=ToolAction.REPLAY,
                code="duplicate_call",
                reason="这个调用刚刚已经执行过，结果与上次相同，请直接作答或换个动作。",
                arguments=arguments,
                signature=signature,
                replay=cached.model_visible,
            )

        if call.name == "search_memories" and state.search_calls >= state.profile.max_search_calls:
            return ToolDecision(
                action=ToolAction.DENY,
                code="search_call_limit",
                reason=(
                    "search_memories 每轮只需调用一次。要看更多候选用 get_result_page 翻页"
                    "（result_set_id 用上一次返回的），不要改词重搜。"
                ),
            )

        if not state.can_call_tool():
            return ToolDecision(
                action=ToolAction.DENY,
                code="tool_budget_exhausted",
                reason="本轮的工具调用预算已经用完，请基于已有结果作答。",
            )

        for record in reversed(state.records):
            if record.tool != call.name:
                continue
            if (
                record.failure_reason
                and state.consecutive_failures(call.name, record.failure_reason) >= MAX_CONSECUTIVE_FAILURES
            ):
                return ToolDecision(
                    action=ToolAction.DENY,
                    code="repeated_failure",
                    reason=(
                        f"{call.name} 已经用同样的方式失败 {MAX_CONSECUTIVE_FAILURES} 次"
                        f"（{record.failure_reason}），换一个动作，不要重试。"
                    ),
                )
            break

        return ToolDecision(
            action=ToolAction.EXECUTE,
            code="ok",
            arguments=arguments,
            signature=signature,
        )
```

一条 1.0 实测出的边界漏洞在这里被堵上：它的 `search_memories` 拦截条件里带着 `and turn.budget.can_model_step()`（`runtime.py:2993`），模型步耗尽后这个 `and` 短路，**第二次 search 会被真的放行**。2.0 的计数与预算分开，测试 `test_search_limit_does_not_lapse_when_the_model_budget_is_gone` 就是钉这个的。

- [ ] **Step 4: 运行测试，确认绿灯**

Run: `.venv/bin/pytest apps/api/tests/unit/test_assistant_policy.py -p no:warnings`

Expected:

```text
16 passed
```

- [ ] **Step 5: 更新 module-map.md**

```markdown
| `sentrix/domain/assistant/tool_policy.py` | 工具收敛策略：执行 / 回放 / 拒绝的唯一决定点 | domain | `sentrix/domain/assistant/models.py`、`sentrix/domain/assistant/task_state.py`、`sentrix/domain/assistant/tools.py` | — |
| `sentrix/domain/assistant/tools.py` | 四个工具的契约：标准 JSON Schema + 声明能满足哪类证据 | domain | `sentrix/domain/assistant/evidence_ledger.py` | `sentrix/domain/assistant/tool_policy.py` |
```

Run: `.venv/bin/python scripts/check_module_map.py`
Expected: `module-map: 0 个问题`

- [ ] **Step 6: 登记能力**

```json
{"capability": "助手工具契约（JSON Schema + 证据绑定）", "origin_kind": "adapted", "source_commit": "2d2cee7c", "source_path": "backend/agent_runtime/tool_registry.py", "source_symbol": "ToolSpec", "target_path": "apps/api/src/sentrix/domain/assistant/tools.py", "verification": "apps/api/tests/unit/test_assistant_policy.py", "status": "verified"}
{"capability": "工具收敛策略", "origin_kind": "migrated", "source_commit": "2d2cee7c", "source_path": "backend/agent_runtime/tool_policy.py", "source_symbol": "ToolPolicy.execute", "target_path": "apps/api/src/sentrix/domain/assistant/tool_policy.py", "verification": "apps/api/tests/unit/test_assistant_policy.py", "status": "verified"}
```

Run: `.venv/bin/python scripts/check_capability_map.py`
Expected: `capability-map: 0 个问题`

- [ ] **Step 7: 全量门禁**

Run: `make ci`

---

## Task 37: 确定性规划、答案门禁与诚实兜底

**依赖：** Task 35（工具名单）、Task 36（`EvidenceRequirement`）。

**Files:**
- Create: `apps/api/src/sentrix/domain/assistant/intent.py`
- Create: `apps/api/src/sentrix/domain/assistant/final_guard.py`
- Create: `apps/api/src/sentrix/domain/assistant/emergency.py`
- Create: `apps/api/tests/unit/test_assistant_intent.py`
- Create: `apps/api/tests/unit/test_assistant_guard.py`
- Modify: `docs/architecture/module-map.md`
- Modify: `docs/refactor/capability-map.jsonl`

三个模块合在一个任务里，因为它们共同构成「取不到证据时怎么办」这一整套：规划决定要什么、门禁决定能不能交付、兜底决定实在拿不到时说什么。

**规划是纯函数。** 这是 2.0 对 `planner_call_error`（1.0 实测 1,974 次，最终落成 1,445 条 `planner_blocked` 轨迹）的根本处理：规划要产出的东西——这段问题需要照片证据还是人物证据——是一个由问句形态决定、可以用规则判定的问题，不需要模型。没有调用，就没有调用失败。模型仍然做它擅长的那部分（把「在海边的照片」变成检索词 `海边`），但那是在工具调用的参数里。

**规划不允许零需求。** 1.0 的 `goal_planner.py:228` 明确允许「零需求，不强行凑一个 memory_asset 需求」，而 JIT 提示词随后告诉模型「当前没有待确认的证据需求……可直接输出 final」（`jit_prompt.py:200`）。29% 的零工具回合有相当一部分是这么来的。

**门禁只做确定性判定。** 1.0 的 `final_guard.py:1-22` 与 `judge.py:6-13` 把这条纪律写得很直白：**代码永远不做语义等价判定**——「三个人」和「3」是不是同一个数，正则判不对，只会误伤事实正确的回答。语义评审（L2）是 S6 的事。

- [ ] **Step 1: 先写失败测试**

`apps/api/tests/unit/test_assistant_intent.py`:

```python
"""确定性规划的单元测试。

1.0 的规划是一次模型调用（``goal_planner.py``），它失败时返回
``fallback_reason="planner_call_error"``——生产上这个原因出现 1,974 次，最终都落成
``planner_blocked``（1,445 条轨迹），整轮以「无法可靠规划」收尾。2.0 的规划是纯函数：
没有调用，就没有调用失败。
"""

from __future__ import annotations

from sentrix.domain.assistant.evidence_ledger import EvidenceType
from sentrix.domain.assistant.intent import (
    canonical_question,
    plan_requirements,
    wants_delivery,
    wants_person,
    wants_photos,
)


def types_of(question: str) -> list[EvidenceType]:
    return [item.evidence_type for item in plan_requirements(question)]


def test_canonical_question_strips_whitespace():
    assert canonical_question("给 我 看 看 海边 的 照片") == "给我看看海边的照片"


def test_the_acceptance_question_asks_for_a_photo_set_and_a_delivery():
    """S3 的验收问句：规划必须同时产出「要有照片」和「要交付」两条需求。

    只产出前者的话，实测 8100 上的模型会检索一次、回一段文字、一张图都不交付
    （「我为您找到了两张在海边的照片…您是想查看哪张照片的具体内容呢？」）。
    """
    assert types_of("给我看看在海边的照片") == [
        EvidenceType.PHOTO_SET,
        EvidenceType.DELIVERED_PHOTO,
    ]


def test_a_plain_question_only_needs_the_photo_set():
    assert types_of("海边那张照片是哪年拍的") == [EvidenceType.PHOTO_SET]


def test_a_whitespace_mangled_question_still_plans_a_delivery():
    """真人在输入法里打出来的句子会带空格，规则先做规范化再匹配。"""
    assert types_of("给 我 看 看 在 海 边 的 照 片") == [
        EvidenceType.PHOTO_SET,
        EvidenceType.DELIVERED_PHOTO,
    ]


def test_a_blank_question_still_plans_exactly_one_requirement():
    """规划**不允许**零需求。

    1.0 的 ``goal_planner`` 允许（``goal_planner.py:228`` 的注释：「允许零需求……不强行凑
    一个 memory_asset 需求」），而 JIT 提示词会在零需求时直接告诉模型「可直接输出 final」
    （``jit_prompt.py:200``）——29% 的零工具回合有相当一部分是这么来的。
    """
    assert types_of("") == [EvidenceType.PHOTO_SET]
    assert types_of("你好呀") == [EvidenceType.PHOTO_SET]


def test_a_person_question_asks_for_the_profile():
    assert types_of("妈妈是谁") == [EvidenceType.PERSON_PROFILE]


def test_a_person_question_that_also_wants_photos_stays_a_photo_question():
    """「给我看看爸爸的照片」是照片问题，不是画像问题——人物只是检索词。"""
    assert EvidenceType.PERSON_PROFILE not in types_of("给我看看爸爸的照片")


def test_delivery_markers_are_recognised():
    assert wants_delivery("给我看看") is True
    assert wants_delivery("打开原图") is True
    assert wants_delivery("这张照片是哪年拍的") is False


def test_photo_and_person_markers_are_recognised():
    assert wants_photos("海边那张图") is True
    assert wants_person("爸爸是谁") is True
    assert wants_person("这张照片在哪拍的") is False


def test_requirement_ids_are_stable_and_unique():
    requirements = plan_requirements("给我看看在海边的照片")

    assert [item.id for item in requirements] == ["photo_set", "delivered_photo"]
    assert all(item.required for item in requirements)
```

`apps/api/tests/unit/test_assistant_guard.py`:

```python
"""答案门禁与兜底答复的单元测试。

门禁只做确定性判定，**不做语义等价**——「三个人」和「3」是不是同一个数，正则判不对，
只会误伤事实正确的回答（1.0 为此把原来的一批正则规则删掉了，见 ``final_guard.py:1-22``）。
语义评审是 S6 的事。

每条规则都配一个反向用例：构造函数让它通过，证明它不是恒真的。
"""

from __future__ import annotations

from sentrix.domain.assistant.emergency import clarify_answer, fallback_answer
from sentrix.domain.assistant.evidence_ledger import (
    EvidenceLedger,
    EvidenceRequirement,
    EvidenceType,
)
from sentrix.domain.assistant.final_guard import FinalGuard, answered_with_gap
from sentrix.domain.assistant.models import TerminationReason
from sentrix.domain.assistant.task_state import TaskState, ToolCallRecord

DESCRIPTIONS = {
    EvidenceType.PHOTO_SET: "从相册里取到与问题相关的照片。",
    EvidenceType.DELIVERED_PHOTO: "把要展示的照片交付给用户，让它们真的能打开。",
    EvidenceType.PERSON_PROFILE: "读取这位已确认人物的家庭角色。",
}


def ledger(*evidence_types: EvidenceType) -> EvidenceLedger:
    return EvidenceLedger(
        scope_id="album1",
        requirements=[
            EvidenceRequirement(id=f"r{i}", evidence_type=t, description=DESCRIPTIONS[t])
            for i, t in enumerate(evidence_types)
        ],
    )


def state_with_search(failure_reason: str = "") -> TaskState:
    from sentrix.domain.assistant.profile import DEFAULT_PROFILE

    state = TaskState(
        turn_id="turn_1",
        conversation_id="conv_1",
        scope_id="album1",
        question="给我看看在海边的照片",
        profile=DEFAULT_PROFILE,
    )
    state.record(
        ToolCallRecord(
            call_id="call_1",
            tool="search_memories",
            signature="s",
            status="ok" if not failure_reason else "error",
            failure_reason=failure_reason,
        )
    )
    return state


def check(
    answer: str,
    *,
    evidence: tuple[EvidenceType, ...] = (),
    state: TaskState | None = None,
    empty: bool = False,
):
    return FinalGuard().check(
        answer=answer,
        ledger=ledger(*evidence),
        state=state if state is not None else state_with_search(),
        search_returned_nothing=empty,
    )


# ---- 安全 -------------------------------------------------------------------


def test_an_internal_asset_id_in_the_answer_is_blocked():
    verdict = check("这张照片的编号是 asset_9f3a21b7c4，可以打开。")

    assert verdict.passed is False
    assert verdict.code == "internal_reference_leak"


def test_a_photo_handle_in_the_answer_is_blocked():
    verdict = check("我找到的是 photo_3 这张。")

    assert verdict.passed is False
    assert verdict.code == "internal_reference_leak"


def test_a_normal_answer_is_not_blocked_by_the_safety_rule():
    """反向用例：没有内部标识时这条规则必须放行，否则它就是恒真的。"""
    assert check("这是你要的海边照片。").passed is True


# ---- 占位符 -----------------------------------------------------------------


def test_an_unfilled_placeholder_is_blocked():
    verdict = check("照片拍摄于[地点名称]，你可以看看。")

    assert verdict.passed is False
    assert verdict.code == "placeholder_leak"


def test_a_bracketed_but_filled_answer_is_not_blocked():
    assert check("海边的照片一共有两张。").passed is True


# ---- 存在性矛盾 --------------------------------------------------------------


def test_denying_without_ever_searching_is_blocked():
    state = state_with_search()
    state.records.clear()

    verdict = FinalGuard().check(
        answer="我没有找到相关照片。",
        ledger=ledger(EvidenceType.PHOTO_SET),
        state=state,
        search_returned_nothing=False,
    )

    assert verdict.passed is False
    assert verdict.code == "denial_without_search"


def test_claiming_a_hit_when_the_search_was_empty_is_blocked():
    verdict = check("我找到了这些海边的照片。", empty=True)

    assert verdict.passed is False
    assert verdict.code == "fabrication_from_empty"


def test_a_hedged_answer_about_an_empty_search_is_not_blocked():
    """「无法确认」是诚实收尾，不是编造。"""
    assert check("这次没有匹配到海边照片，可能没有拍到过。", empty=True).passed is True


def test_denying_after_a_search_is_not_blocked():
    """反向用例：检索过之后说没找到，是合法结论。"""
    assert check("我没有找到海边照片。").passed is True


# ---- 证据门 -----------------------------------------------------------------


def test_an_open_requirement_blocks_the_first_final_answer():
    verdict = check("我找到了这些海边的照片。", evidence=[EvidenceType.DELIVERED_PHOTO])

    assert verdict.passed is False
    assert verdict.code == "evidence_gate"
    assert "交付" in verdict.message


def test_a_satisfied_ledger_does_not_block():
    answer_ledger = ledger(EvidenceType.PHOTO_SET)
    answer_ledger.satisfy(EvidenceType.PHOTO_SET, call_id="call_1")

    verdict = FinalGuard().check(
        answer="这是你要的海边照片。",
        ledger=answer_ledger,
        state=state_with_search(),
        search_returned_nothing=False,
    )

    assert verdict.passed is True


def test_the_evidence_gate_does_not_fire_when_tools_are_exhausted():
    """预算没了就不再拦——拦了也没有工具可用，只会让回合以空答案收场。"""
    state = state_with_search()
    for index in range(state.profile.max_tool_calls):
        state.record(
            ToolCallRecord(call_id=f"c{index}", tool="get_result_page", signature=f"s{index}", status="ok")
        )

    verdict = FinalGuard().check(
        answer="这是你要的海边照片。",
        ledger=ledger(EvidenceType.DELIVERED_PHOTO),
        state=state,
        search_returned_nothing=False,
    )

    assert verdict.passed is True


def test_the_evidence_gate_does_not_fire_before_any_tool_call():
    """一次工具都没调的回合由 denial_without_search 管，两条规则不要重叠。"""
    state = state_with_search()
    state.records.clear()

    verdict = FinalGuard().check(
        answer="这是你要的海边照片。",
        ledger=ledger(EvidenceType.PHOTO_SET),
        state=state,
        search_returned_nothing=False,
    )

    assert verdict.passed is True


def test_answered_with_gap_tracks_the_ledger():
    complete = ledger(EvidenceType.PHOTO_SET)
    complete.satisfy(EvidenceType.PHOTO_SET, call_id="call_1")

    assert answered_with_gap(complete) is False
    assert answered_with_gap(ledger(EvidenceType.PHOTO_SET)) is True


# ---- 兜底答复 ----------------------------------------------------------------


def test_the_fallback_reports_what_was_actually_retrieved():
    answer = fallback_answer(
        reason=TerminationReason.STEP_BUDGET_EXHAUSTED,
        ledger=ledger(EvidenceType.DELIVERED_PHOTO),
        photo_count=6,
        delivered_count=0,
    )

    assert "6 张" in answer
    assert "交付" in answer
    assert "模型步数用尽" in answer


def test_the_fallback_does_not_pretend_a_photo_exists_when_none_was_found():
    """1.0 的兜底只有一条尾句（``emergency.py:80``），「有检索结果但被丢弃」与「真的
    一无所获」共用同一句话。2.0 按事实分档。"""
    answer = fallback_answer(
        reason=TerminationReason.MODEL_ERROR,
        ledger=ledger(),
        photo_count=0,
        delivered_count=0,
    )

    assert "没有检索到照片" in answer
    assert "模型不可用" in answer


def test_clarify_answer_names_the_reason():
    assert "超时" in clarify_answer(TerminationReason.TIMEOUT)
```

- [ ] **Step 2: 运行测试，确认红灯**

Run: `.venv/bin/pytest apps/api/tests/unit/test_assistant_intent.py apps/api/tests/unit/test_assistant_guard.py -q -p no:warnings`

Expected:

```text
E   ModuleNotFoundError: No module named 'sentrix.domain.assistant.intent'
ERROR apps/api/tests/unit/test_assistant_intent.py
ERROR apps/api/tests/unit/test_assistant_guard.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 2 errors during collection !!!!!!!!!!!!!!!!!!!!
```

- [ ] **Step 3: 实现 intent / final_guard / emergency**

`apps/api/src/sentrix/domain/assistant/intent.py`:

```python
"""确定性规划：把问题映射成「最少需要哪些证据」。

这是 2.0 对 1.0 ``planner_call_error`` 的根本处理。1.0 的规划是一次模型调用
（``goal_planner.py``），它失败时返回 ``fallback_reason="planner_call_error"``——生产
数据里这个原因出现 1,974 次，最终都落成 ``planner_blocked``（1,445 条轨迹），整轮直接
以「无法可靠规划」收尾。而规划要产出的东西（这段问题需要照片证据还是人物证据）是一个
**由问句形态决定的、可以用规则判定**的问题，不需要模型。

于是 S3 的规划是纯函数：没有模型调用，就没有调用失败；``planner_call_error`` 在 2.0 里
不是「被修好了」，而是**不存在这条路径**。模型仍然要做它擅长的那部分——把「在海边的
照片」变成检索词 ``海边``——但那是在工具调用的参数里，不是在规划里。

1.0 的 ``intent.py`` / ``canonical_intent.py`` 已经是这个方向的确定性实现，规则的形态
沿用它们。
"""

from __future__ import annotations

import re

from sentrix.domain.assistant.evidence_ledger import EvidenceRequirement, EvidenceType

# 「把照片给我看」这一类意图。用户说了这些词，答案里就必须真的有照片——
# 只回一段「我找到了两张，你要看哪张？」不算交付。
DELIVERY_MARKERS = (
    "看看",
    "看一下",
    "给我看",
    "给我瞧",
    "想看",
    "看下",
    "看照片",
    "看图",
    "展示",
    "打开",
    "原图",
    "发我",
    "发给我",
)

PHOTO_MARKERS = (
    "照片",
    "相片",
    "图片",
    "相册",
    "拍",
    "图",
)

PERSON_MARKERS = (
    "是谁",
    "是谁呀",
    "介绍",
    "档案",
    "画像",
    "多大",
    "叫什么",
)

_WHITESPACE = re.compile(r"\s+")


def canonical_question(question: str) -> str:
    """问句的规范形式：去掉空白。规则匹配与它比较，避免「给 我 看 看」这类输入漏判。"""
    return _WHITESPACE.sub("", question or "")


def wants_delivery(question: str) -> bool:
    text = canonical_question(question)
    return any(marker in text for marker in DELIVERY_MARKERS)


def wants_photos(question: str) -> bool:
    text = canonical_question(question)
    return any(marker in text for marker in PHOTO_MARKERS)


def wants_person(question: str) -> bool:
    text = canonical_question(question)
    return any(marker in text for marker in PERSON_MARKERS)


def plan_requirements(question: str) -> tuple[EvidenceRequirement, ...]:
    """这个问题至少要拿到哪几类证据。

    规则刻意保守：默认给一条 ``photo_set``。宁可多取一次证据，也不要因为没识别出意图
    让模型空手作答——1.0 的 ``goal_planner`` 允许「零需求」（``goal_planner.py:228``），
    于是 JIT 提示词直接告诉模型「当前没有待确认的证据需求……可直接输出 final」，
    这正是 29% 的回合一次工具都没调的来源之一。
    """
    requirements: list[EvidenceRequirement] = []

    if wants_person(question) and not wants_photos(question):
        requirements.append(
            EvidenceRequirement(
                id="person_profile",
                evidence_type=EvidenceType.PERSON_PROFILE,
                description="读取这位已确认人物的家庭角色。",
            )
        )
    else:
        requirements.append(
            EvidenceRequirement(
                id="photo_set",
                evidence_type=EvidenceType.PHOTO_SET,
                description="从相册里取到与问题相关的照片。",
            )
        )

    if wants_delivery(question):
        requirements.append(
            EvidenceRequirement(
                id="delivered_photo",
                evidence_type=EvidenceType.DELIVERED_PHOTO,
                description="把要展示的照片交接给用户，让它们真的能打开。",
            )
        )

    return tuple(requirements)
```

`apps/api/src/sentrix/domain/assistant/final_guard.py`:

```python
"""答案门禁：模型给出最终回答之前，用确定性规则过一道。

对应 1.0 ``agent_runtime/final_guard.py`` 的 L1，并保留它最要紧的一条纪律（写在
``final_guard.py:1-22`` 与 ``judge.py:6-13`` 的文档里）：**代码永远不做语义等价判定**。
1.0 为此把原来的一批正则规则删掉了——「三个人」和「3」是不是同一个数，正则判不对，
只会误伤事实正确的回答。语义评审是 L2（判分模型）的事，属于 S6；S3 只做 L1。

L1 只保留三类：

- **安全**：内部标识泄漏。模型把 ``asset_xxx`` / ``photo_1`` / 表名写进回答，用户看到的是
  系统内部结构。1.0 ``final_guard.py:155-179`` 的硬拦规则。
- **占位符**：模板没填就交付。``final_guard.py:97-98``。
- **存在性矛盾**：检索结果为空却断言「找到了」，或一次检索都没做就断言「没找到」。
  ``final_guard.py:99-102`` 与 ``_check_existence``（``:115-153``）。

第四条是 2.0 补的：**证据门**。1.0 的零检索拦截（``runtime.py:2568``）只拦「没检索却
断言没有」，对「用户要图、模型却只回一段文字」完全没有保护——实测 8100 上问「给我看看
在海边的照片」，模型调用了一次 ``search_memories`` 之后直接给文字，一张图都没交付。
2.0 把「要交付」变成一条证据需求，没满足就提醒一次。

**所有门禁只拦一次**（``TaskState.block_guard_code``）：提醒过就放行并如实标注。1.0 的
教训是反过来的——那道零检索拦截没有标志位，模型只要坚持不调工具就会被拦到预算耗尽，
白烧 8 个模型步（实测 29% 的回合零工具调用）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sentrix.domain.assistant.evidence_ledger import EvidenceLedger
from sentrix.domain.assistant.task_state import TaskState
from sentrix.domain.assistant.tools import RETRIEVAL_TOOLS

# 内部标识：1.0 ``final_guard.py:164-172`` 的两条正则。
_INTERNAL_ID = re.compile(r"\b(?:asset|obs|entity|mention|claim|turn|conversation|result_set)_[a-z0-9]{6,}\b")
_PHOTO_HANDLE = re.compile(r"(?<![A-Za-z0-9_])photo_\d+(?![A-Za-z0-9_])")
_INTERNAL_TABLE = re.compile(r"\b(?:assets|observations|memory_vectors|agent_turns|agent_result_sets)\b")

# 模板占位符：``[地点名称]`` / ``{{query}}`` / ``TODO``。
_PLACEHOLDER = re.compile(
    r"\[[^\[\]]{0,48}(?:填入|此处|占位|待填|名称|名字|姓名|数量|时间|地点|内容|数字|照片|记录)[^\[\]]{0,36}\]"
    r"|\{\{[^}]{1,48}\}\}"
    r"|\bTODO\b"
)

# 断言「找到了」。与 1.0 的 ``_FOUND_DELIVERY`` 同构，但**去掉了 ``_ANY_DENIAL`` 的整句
# 否定门槛**：那套「句中出现任何否定词就豁免」的规则把「没找到 A，但找到了 B」也一起
# 豁免了。这里只看断言本身。
_FOUND_CLAIM = re.compile(
    r"(?:已|为)?(?:为您|为你)?找到|已找到|找到了|这是(?:您|你)要找的|这就是(?:您|你)?要的"
    r"|有(?:一张|两张|几张|这些)?(?:照片|记录)"
)

# 断言「没找到」。
_DENY_EXISTS = re.compile(
    r"没(?:有)?找到|未找到|找不到|查无"
    r"|没有(?:任何|符合|相关|一张|一个|一条|拍到|拍过|去过)?(?:照片|记录|回忆|记忆|相关)"
)
# 模糊表述豁免：「无法确认」「可能没有」不是断言。
_HEDGE = re.compile(r"无法确认|不能确认|不确定|还不能|暂时|无法判断|记不清|可能没有|未必|不能确定")


@dataclass(frozen=True, slots=True)
class GuardVerdict:
    passed: bool
    code: str = ""
    message: str = ""

    @staticmethod
    def ok() -> GuardVerdict:
        return GuardVerdict(passed=True)


class FinalGuard:
    def check(
        self,
        *,
        answer: str,
        ledger: EvidenceLedger,
        state: TaskState,
        search_returned_nothing: bool,
    ) -> GuardVerdict:
        """``search_returned_nothing`` 由调用方从工具观察里算出——门禁只看「这次检索
        空不空」这一个事实，不解析工具结果的结构。"""
        searched = any(record.tool in RETRIEVAL_TOOLS for record in state.records)
        for verdict in (
            self._safety(answer),
            self._placeholder(answer),
            self._existence(answer, searched=searched, search_returned_nothing=search_returned_nothing),
            self._evidence_gate(ledger=ledger, state=state),
        ):
            if verdict is not None:
                return verdict
        return GuardVerdict.ok()

    # ---- 规则 ---------------------------------------------------------------

    @staticmethod
    def _safety(answer: str) -> GuardVerdict | None:
        if _INTERNAL_ID.search(answer) or _PHOTO_HANDLE.search(answer) or _INTERNAL_TABLE.search(answer):
            return GuardVerdict(
                passed=False,
                code="internal_reference_leak",
                message=(
                    "回答里出现了系统内部标识（照片编号或记录 id）。"
                    "请用「这张照片」这类说法重写，不要写 photo_1 或 asset_ 开头的串。"
                ),
            )
        return None

    @staticmethod
    def _placeholder(answer: str) -> GuardVerdict | None:
        if _PLACEHOLDER.search(answer):
            return GuardVerdict(
                passed=False,
                code="placeholder_leak",
                message="回答里有没填上的模板占位符，请把方括号/花括号里的占位替换成真实内容，或删掉那句话。",
            )
        return None

    @staticmethod
    def _existence(answer: str, *, searched: bool, search_returned_nothing: bool) -> GuardVerdict | None:
        hedged = bool(_HEDGE.search(answer))
        if not searched and _DENY_EXISTS.search(answer) and not hedged:
            return GuardVerdict(
                passed=False,
                code="denial_without_search",
                message="你还没有检索过相册，不能说「没有找到」。请先调用 search_memories。",
            )
        if search_returned_nothing and _FOUND_CLAIM.search(answer) and not hedged:
            return GuardVerdict(
                passed=False,
                code="fabrication_from_empty",
                message="这次检索没有返回任何照片，不能声称找到了。请如实说明没有匹配，或换个说法检索。",
            )
        return None

    @staticmethod
    def _evidence_gate(*, ledger: EvidenceLedger, state: TaskState) -> GuardVerdict | None:
        """还有开着的证据需求、预算也还有，就先别急着收尾。

        只对**尝试过工具**的回合生效。一次工具都没调的回合由上面那条
        ``denial_without_search`` 管，两条不要重复拦同一件事。
        """
        open_items = ledger.open_requirements()
        if not open_items:
            return None
        if not state.records:
            return None
        if not state.can_call_tool() or not state.can_step():
            return None
        unmet = "、".join(item.description.rstrip("。；") for item in open_items)
        return GuardVerdict(
            passed=False,
            code="evidence_gate",
            message=(
                f"还有没拿到的证据：{unmet}。"
                "请先调用相应的工具把它们取到；确实取不到时，在回答里如实说明缺什么。"
            ),
        )


def answered_with_gap(ledger: EvidenceLedger) -> bool:
    """收尾时是否还有没满足的需求——用来区分「答完了」与「如实说了答不出来」。"""
    return not ledger.is_complete
```

`apps/api/src/sentrix/domain/assistant/emergency.py`:

```python
"""诚实兜底：预算耗尽 / 模型失败 / 门禁拦死时的确定性收尾。

对应 1.0 ``agent_runtime/emergency.py``。它的文档写得很准确：只基于**已有的**工具观察
产出，不生成新事实、不冒充完整回答。2.0 保留这个定位，并补上 1.0 缺的那一档。

1.0 的兜底只有一条尾句——「这次处理没有完成，没有产生可确认的结果。」
（``emergency.py:80-84``），于是「有检索结果但被丢弃」和「真的一无所获」共用同一句话。
2.0 按账本状态分档：拿到过照片就说拿到过几张（用结果集里的**真实条数**，不是模型
转述的数字），什么都没拿到就说没拿到。
"""

from __future__ import annotations

from collections.abc import Sequence

from sentrix.domain.assistant.evidence_ledger import EvidenceLedger, RequirementStatus
from sentrix.domain.assistant.models import TerminationReason


def _missing_clause(ledger: EvidenceLedger) -> str:
    missing = [
        item.description.rstrip("。；")
        for item in ledger.requirements
        if item.status is not RequirementStatus.SATISFIED
    ]
    if not missing:
        return ""
    return "还差这些没能确认：" + "；".join(missing) + "。"


def fallback_answer(
    *,
    reason: TerminationReason,
    ledger: EvidenceLedger,
    photo_count: int,
    delivered_count: int,
) -> str:
    """``photo_count`` / ``delivered_count`` 来自结果集这一侧的事实，不是模型的话。"""
    parts: list[str] = []
    if photo_count > 0:
        parts.append(f"这一轮检索到 {photo_count} 张候选照片。")
    else:
        parts.append("这一轮没有检索到照片。")
    if delivered_count > 0:
        parts.append(f"其中 {delivered_count} 张已经放在回答下面，可以直接打开。")

    missing = _missing_clause(ledger)
    if missing:
        parts.append(missing)

    parts.append(f"（本轮因「{_REASON_LABEL[reason]}」提前结束。）")
    return "".join(parts)


_REASON_LABEL: dict[TerminationReason, str] = {
    TerminationReason.ANSWERED: "已完成",
    TerminationReason.ANSWERED_WITH_GAP: "有未取到的证据",
    TerminationReason.EMPTY_ANSWER: "模型没有给出回答",
    TerminationReason.NO_TOOL_CALL_STOPPED: "未取证就作答",
    TerminationReason.TOOL_BUDGET_EXHAUSTED: "工具调用预算用尽",
    TerminationReason.STEP_BUDGET_EXHAUSTED: "模型步数用尽",
    TerminationReason.TIMEOUT: "超时",
    TerminationReason.MODEL_ERROR: "模型不可用",
    TerminationReason.GUARD_BLOCKED: "回答未通过事实校验",
    TerminationReason.CANCELLED: "已取消",
}


def clarify_answer(reason: TerminationReason, details: Sequence[str] = ()) -> str:
    """连账本都还没建起来时的兜底（例如第一次模型调用就失败）。"""
    head = f"这次没能完成检索：{_REASON_LABEL[reason]}。"
    if details:
        return head + " " + " ".join(details)
    return head
```

第四条门禁（**证据门**）是 2.0 补的。1.0 的零检索拦截只拦「没检索却断言没有」，对「用户要图、模型却只回一段文字」完全没有保护——实测 8100 上问「给我看看在海边的照片」，模型调用一次 `search_memories` 之后直接给文字：

```text
我为您找到了两张在海边的照片。
1. **照片 1**：在2017年11月5日拍摄，拍摄地点在**浙江省杭州市萧山区**……
您是想查看哪张照片的具体内容呢？
```

一张图都没交付。2.0 把「要交付」变成一条一等证据需求。

**所有门禁只拦一次**（`TaskState.block_guard_code`）。1.0 的反面教材是那道没有标志位的零检索拦截：模型只要坚持不调工具就会被拦到预算耗尽，白烧 8 个模型步。

- [ ] **Step 4: 运行测试，确认绿灯**

Run: `.venv/bin/pytest apps/api/tests/unit/test_assistant_intent.py apps/api/tests/unit/test_assistant_guard.py -p no:warnings`

Expected:

```text
27 passed
```

- [ ] **Step 5: 更新 module-map.md**

```markdown
| `sentrix/domain/assistant/emergency.py` | 诚实兜底答复：只基于已有观察，按账本与检索事实分档 | domain | `sentrix/domain/assistant/evidence_ledger.py`、`sentrix/domain/assistant/models.py` | — |
| `sentrix/domain/assistant/final_guard.py` | 答案门禁 L1：安全 / 占位符 / 存在性矛盾 / 证据门，只做确定性判定 | domain | `sentrix/domain/assistant/evidence_ledger.py`、`sentrix/domain/assistant/task_state.py`、`sentrix/domain/assistant/tools.py` | — |
| `sentrix/domain/assistant/intent.py` | 确定性规划：问题映射到最少需要的证据，无模型调用 | domain | `sentrix/domain/assistant/evidence_ledger.py` | — |
```

Run: `.venv/bin/python scripts/check_module_map.py`
Expected: `module-map: 0 个问题`

- [ ] **Step 6: 登记能力**

```json
{"capability": "确定性规划（替代 planner 模型调用）", "origin_kind": "adapted", "source_commit": "2d2cee7c", "source_path": "backend/agent_runtime/goal_planner.py", "source_symbol": "GoalPlanner.declare", "target_path": "apps/api/src/sentrix/domain/assistant/intent.py", "verification": "apps/api/tests/unit/test_assistant_intent.py", "status": "verified"}
{"capability": "答案门禁 L1（确定性）", "origin_kind": "migrated", "source_commit": "2d2cee7c", "source_path": "backend/agent_runtime/final_guard.py", "source_symbol": "FinalGuard.check", "target_path": "apps/api/src/sentrix/domain/assistant/final_guard.py", "verification": "apps/api/tests/unit/test_assistant_guard.py", "status": "verified"}
{"capability": "诚实兜底答复", "origin_kind": "migrated", "source_commit": "2d2cee7c", "source_path": "backend/agent_runtime/emergency.py", "source_symbol": "render_emergency_summary", "target_path": "apps/api/src/sentrix/domain/assistant/emergency.py", "verification": "apps/api/tests/unit/test_assistant_guard.py", "status": "verified"}
```

Run: `.venv/bin/python scripts/check_capability_map.py`
Expected: `capability-map: 0 个问题`

- [ ] **Step 7: 全量门禁**

Run: `make ci`

---

## Task 38: 助手域端口 + 迁移 0004 + 三个 SQLite 仓储

**依赖：** Task 36（`ResultSet` 是端口签名的一部分）、Task 34。

**Files:**
- Create: `apps/api/src/sentrix/domain/assistant/ports.py`
- Create: `apps/api/src/sentrix/infrastructure/persistence/migrations/0004_assistant.sql`
- Create: `apps/api/src/sentrix/infrastructure/persistence/repositories/conversations.py`
- Create: `apps/api/src/sentrix/infrastructure/persistence/repositories/turns.py`
- Create: `apps/api/src/sentrix/infrastructure/persistence/repositories/result_sets.py`
- Create: `apps/api/tests/integration/test_assistant_repositories.py`
- Modify: `docs/architecture/module-map.md`
- Modify: `docs/refactor/capability-map.jsonl`

端口一次全部定义好（模型、会话、回合、结果集、人物目录、事件流），迁移与三个仓储一起落地。1.0 的这三张表都出自 `db.py:918-979`，四处结构改进写在迁移文件的注释里。

最要紧的一处是 **`agent_turn_events` 这张 1.0 没有的表**。1.0 的进度只存在 `app.py` 的进程内 dict `_TURN_JOBS` 里：永不清理（内存泄漏）、重启即丢（历史 turn 全部 404）、多 worker 不可用（轮询/SSE 随机 404）、页面刷新就再也拿不到那次回合。前端不得不用 700ms 轮询兜住这些——668,490 次请求是这么来的。把事件落库之后，「实时」由 Task 39 的 broker 负责、「迟到加入」由 `events_after` 负责。

- [ ] **Step 1: 先写失败测试**

`apps/api/tests/integration/test_assistant_repositories.py`:

```python
"""真 SQLite + 真迁移文件下的助手仓储测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sentrix.domain.assistant.models import (
    Attachment,
    ChatRole,
    ProgressEvent,
    ProgressStage,
    ProgressStatus,
    TurnRecord,
    TurnStatus,
)
from sentrix.domain.assistant.result_set import ResultSet
from sentrix.infrastructure.persistence.repositories.conversations import (
    SqliteConversationRepository,
)
from sentrix.infrastructure.persistence.repositories.result_sets import SqliteResultSetRepository
from sentrix.infrastructure.persistence.repositories.turns import SqliteTurnRepository

NOW = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)


@pytest.fixture
def conversations(migrations_ready_db):
    return SqliteConversationRepository(migrations_ready_db)


@pytest.fixture
def turns(migrations_ready_db):
    return SqliteTurnRepository(migrations_ready_db)


@pytest.fixture
def result_sets(migrations_ready_db):
    return SqliteResultSetRepository(migrations_ready_db)


# ---- 会话 -------------------------------------------------------------------


def test_a_created_conversation_is_listed_for_its_scope(conversations):
    created = conversations.create(scope_id="album1", title="新对话", created_at=NOW)

    listed = conversations.list_for_scope("album1", 10)

    assert [item.id for item in listed] == [created.id]
    assert conversations.list_for_scope("album2", 10) == []


def test_a_deleted_conversation_disappears_from_list_and_get(conversations):
    """软删对外就是「不存在」——``state = 'active'`` 是查询条件，不是调用方的事后判断。"""
    created = conversations.create(scope_id="album1", title="新对话", created_at=NOW)

    assert conversations.delete(created.id, NOW) is True

    assert conversations.get(created.id) is None
    assert conversations.list_for_scope("album1", 10) == []
    assert conversations.delete(created.id, NOW) is False


def test_renaming_a_missing_conversation_returns_false(conversations):
    assert conversations.rename("conv_nope", "别的名字", NOW) is False


def test_history_is_the_most_recent_messages_in_chronological_order(conversations):
    conversation = conversations.create(scope_id="album1", title="对话", created_at=NOW)
    for index in range(4):
        conversations.add_message(
            conversation_id=conversation.id,
            scope_id="album1",
            turn_id=f"turn_{index}",
            role=ChatRole.USER.value,
            content=f"第 {index} 句",
            created_at=NOW + timedelta(seconds=index),
        )

    history = conversations.history_for_prompt(conversation.id, 3)

    assert [message.content for message in history] == ["第 1 句", "第 2 句", "第 3 句"]


def test_a_new_message_moves_the_conversation_to_the_top(conversations):
    older = conversations.create(scope_id="album1", title="旧的", created_at=NOW)
    newer = conversations.create(scope_id="album1", title="新的", created_at=NOW + timedelta(hours=1))

    conversations.add_message(
        conversation_id=older.id,
        scope_id="album1",
        turn_id="turn_1",
        role=ChatRole.USER.value,
        content="刚问的",
        created_at=NOW + timedelta(hours=2),
    )

    assert [item.id for item in conversations.list_for_scope("album1", 10)] == [
        older.id,
        newer.id,
    ]


def test_deleting_a_conversation_removes_its_messages_and_turns(conversations, turns):
    """删对话不删记忆，但对话自己的消息与回合必须一起走干净。"""
    conversation = conversations.create(scope_id="album1", title="对话", created_at=NOW)
    record = TurnRecord(
        id="turn_1",
        conversation_id=conversation.id,
        scope_id="album1",
        question="给我看看海边的照片",
        status=TurnStatus.PENDING,
        created_at=NOW,
        updated_at=NOW,
    )
    turns.create(record)
    turns.append_event(
        "turn_1",
        ProgressEvent(
            step_index=1,
            stage=ProgressStage.PLANNING,
            status=ProgressStatus.COMPLETE,
            text="已确认需要哪些证据。",
        ),
    )
    conversations.add_message(
        conversation_id=conversation.id,
        scope_id="album1",
        turn_id="turn_1",
        role=ChatRole.USER.value,
        content="给我看看海边的照片",
        created_at=NOW,
    )

    conversations.delete(conversation.id, NOW)

    assert conversations.list_messages(conversation.id, 10) == []
    assert turns.get("turn_1") is None
    assert turns.events_after("turn_1", 0) == []


# ---- 回合 -------------------------------------------------------------------


def make_turn(turn_id: str = "turn_1", *, conversation_id: str = "conv_missing") -> TurnRecord:
    return TurnRecord(
        id=turn_id,
        conversation_id=conversation_id,
        scope_id="album1",
        question="给我看看在海边的照片",
        status=TurnStatus.PENDING,
        created_at=NOW,
        updated_at=NOW,
    )


def test_claim_flips_pending_to_running_exactly_once(turns, conversations):
    conversation = conversations.create(scope_id="album1", title="对话", created_at=NOW)
    turns.create(make_turn("turn_1", conversation_id=conversation.id))

    assert turns.claim("turn_1", NOW) is True
    assert turns.claim("turn_1", NOW) is False
    assert turns.get("turn_1").status is TurnStatus.RUNNING


def test_events_are_replayed_by_step_index(turns, conversations):
    conversation = conversations.create(scope_id="album1", title="对话", created_at=NOW)
    turns.create(make_turn("turn_1", conversation_id=conversation.id))
    for index in range(1, 4):
        turns.append_event(
            "turn_1",
            ProgressEvent(
                step_index=index,
                stage=ProgressStage.THINKING,
                status=ProgressStatus.RUNNING,
                text=f"第 {index} 步",
                payload={"n": index},
            ),
        )

    replay = turns.events_after("turn_1", 1)

    assert [event.step_index for event in replay] == [2, 3]
    assert replay[0].text == "第 2 步"
    assert replay[0].payload == {"n": 2}
    assert replay[0].stage is ProgressStage.THINKING


def test_finishing_a_turn_records_the_reason_answer_and_attachments(turns, conversations):
    conversation = conversations.create(scope_id="album1", title="对话", created_at=NOW)
    turns.create(make_turn("turn_1", conversation_id=conversation.id))

    turns.finish(
        "turn_1",
        status=TurnStatus.COMPLETE.value,
        termination_reason="answered",
        answer="这是你要的海边照片。",
        attachments=(Attachment(handle="photo_1", asset_id="asset_1", file_name="a.jpg"),),
        requirements=({"id": "photo_set", "evidence_type": "photo_set", "status": "satisfied"},),
        updated_at=NOW,
    )

    record = turns.get("turn_1")
    assert record.status is TurnStatus.COMPLETE
    assert record.termination_reason == "answered"
    assert record.answer == "这是你要的海边照片。"
    assert record.attachments[0].asset_id == "asset_1"
    assert record.requirements[0]["status"] == "satisfied"


def test_a_pending_turn_has_no_termination_reason_yet(turns, conversations):
    """``TurnRecord.termination_reason`` 是空串表示「还没结束」——与
    ``TurnResult.termination_reason`` 的非可选枚举是两回事，后者只在收尾时构造。"""
    conversation = conversations.create(scope_id="album1", title="对话", created_at=NOW)
    turns.create(make_turn("turn_1", conversation_id=conversation.id))

    assert turns.get("turn_1").termination_reason == ""


# ---- 结果集 -----------------------------------------------------------------


def test_a_result_set_survives_a_round_trip(result_sets):
    result_sets.add(
        ResultSet(
            id="rs_1",
            scope_id="album1",
            query="海边",
            asset_ids=("asset_1", "asset_2"),
            created_at=NOW,
        )
    )

    loaded = result_sets.get("rs_1")

    assert loaded is not None
    assert loaded.asset_ids == ("asset_1", "asset_2")
    assert loaded.query == "海边"
    assert loaded.page(1).entries[1].handle == "photo_2"


def test_a_missing_result_set_is_none(result_sets):
    assert result_sets.get("rs_nope") is None
```

- [ ] **Step 2: 运行测试，确认红灯**

Run: `.venv/bin/pytest apps/api/tests/integration/test_assistant_repositories.py -q -p no:warnings`

Expected:

```text
E   ModuleNotFoundError: No module named 'sentrix.infrastructure.persistence.repositories.conversations'
ERROR apps/api/tests/integration/test_assistant_repositories.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
```

- [ ] **Step 3: 实现端口、迁移与仓储**

`apps/api/src/sentrix/domain/assistant/ports.py`:

```python
"""助手域端口：模型端点、会话与回合存储、结果集存储、人物目录、进度事件流。

五个端口对应五个「2.0 要自己实现、但 domain 不能知道怎么实现」的东西。装配一律在
``bootstrap/container.py``，字段按这里的协议声明。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol

from sentrix.domain.assistant.models import (
    Attachment,
    ChatMessage,
    Conversation,
    Message,
    ModelReply,
    PersonProfile,
    ProgressEvent,
    ToolCall,
    TurnRecord,
)
from sentrix.domain.assistant.result_set import ResultSet
from sentrix.domain.assistant.tools import ToolSpec


class ChatModel(Protocol):
    """OpenAI 兼容的对话模型端点。2.0 只连它，不管理它的进程。"""

    def complete(self, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec]) -> ModelReply: ...


class ConversationRepository(Protocol):
    def create(self, *, scope_id: str, title: str, created_at: datetime) -> Conversation: ...

    def get(self, conversation_id: str) -> Conversation | None: ...

    def list_for_scope(self, scope_id: str, limit: int) -> Sequence[Conversation]: ...

    def rename(self, conversation_id: str, title: str, updated_at: datetime) -> bool: ...

    def delete(self, conversation_id: str, updated_at: datetime) -> bool: ...

    def add_message(
        self,
        *,
        conversation_id: str,
        scope_id: str,
        turn_id: str,
        role: str,
        content: str,
        created_at: datetime,
    ) -> Message: ...

    def list_messages(self, conversation_id: str, limit: int) -> Sequence[Message]: ...

    def history_for_prompt(self, conversation_id: str, limit: int) -> Sequence[Message]: ...


class TurnRepository(Protocol):
    """回合的持久化。SSE 的断线续传完全依赖它——1.0 的进度只存在进程内存的一个 dict
    里，页面一刷新就永远拿不到那次回合了，这正是前端不得不轮询的原因之一。"""

    def create(self, record: TurnRecord) -> None: ...

    def get(self, turn_id: str) -> TurnRecord | None: ...

    def claim(self, turn_id: str, updated_at: datetime) -> bool:
        """把 ``pending`` 置为 ``running``。返回是否抢到——并发只允许一个执行者。"""
        ...

    def append_event(self, turn_id: str, event: ProgressEvent) -> None: ...

    def events_after(self, turn_id: str, step_index: int) -> Sequence[ProgressEvent]: ...

    def finish(
        self,
        turn_id: str,
        *,
        status: str,
        termination_reason: str,
        answer: str,
        attachments: Sequence[Attachment],
        requirements: Sequence[Mapping[str, Any]],
        updated_at: datetime,
    ) -> None: ...

    def list_for_conversation(self, conversation_id: str, limit: int) -> Sequence[TurnRecord]: ...


class ResultSetRepository(Protocol):
    def add(self, result_set: ResultSet) -> None: ...

    def get(self, result_set_id: str) -> ResultSet | None: ...


class PersonDirectory(Protocol):
    """已确认人物的目录。S3 的实现只能回答「有没有这个人、家庭角色是什么」。"""

    def lookup(self, name: str, scope_id: str) -> PersonProfile | None: ...


class TurnEventBroker(Protocol):
    """把正在跑的回合的进度事件实时推给订阅者。

    这是「前端不轮询」的落点：SSE 连接订阅这个流，事件一到就写出去，而不是每 300ms
    去查一次状态。已结束回合的补偿走 ``TurnRepository.events_after``——两条路合起来
    覆盖「实时」与「迟到加入」，1.0 缺的正是后者。
    """

    def open(self, turn_id: str) -> None:
        """回合开始时登记，保证连接比第一条事件更早到达时也订阅得上。"""
        ...

    def publish(self, turn_id: str, event: ProgressEvent) -> None: ...

    def finish(self, turn_id: str) -> None: ...

    def subscribe(self, turn_id: str) -> AsyncIterator[ProgressEvent | None]: ...


class ToolExecutor(Protocol):
    """把一次工具调用变成一份观察。实现放在 application 层。"""

    def execute(self, call: ToolCall, arguments: Mapping[str, Any]) -> Mapping[str, Any]: ...
```

`apps/api/src/sentrix/infrastructure/persistence/migrations/0004_assistant.sql`:

```sql
-- 助手闭环的持久化：会话、消息、回合、回合进度事件、结果集。
-- 字段语义取自 1.0 backend/db.py:918-979，四处结构改进：
--   1. 1.0 没有 agent_turn_events 表——进度只存在 app.py 的进程内 dict `_TURN_JOBS`
--      里，永不清理、重启即丢、多 worker 直接 404。2.0 把每条进度事件落库，
--      SSE 的断线续传才有东西可续。
--   2. 1.0 的 agent_conversation_messages.content_json 装的是
--      {text, intent, presentation} 一整个投影；2.0 只存正文文本。展示层的字段属于
--      前端，不该混进对话记录。
--   3. 1.0 的 FTS 表没有触发器、靠手工 INSERT 同步，而且只索引 content_json 里的
--      text 字段。对话检索（search_conversation_history）是 S7 的事，S3 不建 FTS——
--      建一张没人同步的表，比不建更糟。
--   4. agent_trajectories 在 1.0 没有 conversation_id 索引，而 list_trajectories 恰恰
--      按它排序查询。这里建上。
CREATE TABLE agent_conversations (
    id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL DEFAULT 'home-default',
    title TEXT NOT NULL DEFAULT '新对话',
    state TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_message_at TEXT NOT NULL DEFAULT ''
);

CREATE INDEX idx_agent_conversations_scope ON agent_conversations(scope_id, updated_at);

CREATE TABLE agent_conversation_messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES agent_conversations(id),
    scope_id TEXT NOT NULL DEFAULT 'home-default',
    turn_id TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX idx_agent_conversation_messages_cid
    ON agent_conversation_messages(conversation_id, created_at);

CREATE TABLE agent_turns (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES agent_conversations(id),
    scope_id TEXT NOT NULL DEFAULT 'home-default',
    question TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    termination_reason TEXT NOT NULL DEFAULT '',
    answer TEXT NOT NULL DEFAULT '',
    attachments_json TEXT NOT NULL DEFAULT '[]',
    requirements_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_agent_turns_conversation ON agent_turns(conversation_id, created_at);

CREATE TABLE agent_turn_events (
    turn_id TEXT NOT NULL REFERENCES agent_turns(id),
    step_index INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (turn_id, step_index)
);

CREATE TABLE agent_result_sets (
    id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL,
    query TEXT NOT NULL DEFAULT '',
    asset_ids_json TEXT NOT NULL DEFAULT '[]',
    total INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_agent_result_sets_scope ON agent_result_sets(scope_id);
```

`apps/api/src/sentrix/infrastructure/persistence/repositories/conversations.py`:

```python
"""会话与消息的 SQLite 仓储。SQL 只出现在这一层。"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Sequence
from datetime import datetime

from sentrix.domain.assistant.models import ChatRole, Conversation, Message
from sentrix.infrastructure.persistence.database import Database

_CONVERSATION_COLUMNS = "id, scope_id, title, state, created_at, updated_at, last_message_at"
_MESSAGE_COLUMNS = "id, conversation_id, scope_id, turn_id, role, content, created_at"


def _row_to_conversation(row: sqlite3.Row) -> Conversation:
    return Conversation(
        id=row["id"],
        scope_id=row["scope_id"],
        title=row["title"],
        state=row["state"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        last_message_at=datetime.fromisoformat(row["last_message_at"]),
    )


def _row_to_message(row: sqlite3.Row) -> Message:
    return Message(
        id=row["id"],
        conversation_id=row["conversation_id"],
        scope_id=row["scope_id"],
        turn_id=row["turn_id"],
        role=ChatRole(row["role"]),
        content=row["content"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


class SqliteConversationRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def create(self, *, scope_id: str, title: str, created_at: datetime) -> Conversation:
        conversation = Conversation(
            id=f"conv_{uuid.uuid4().hex[:12]}",
            scope_id=scope_id,
            title=title or "新对话",
            state="active",
            created_at=created_at,
            updated_at=created_at,
            last_message_at=created_at,
        )
        with self._database.connect() as connection, connection:
            connection.execute(
                "INSERT INTO agent_conversations"
                " (id, scope_id, title, state, created_at, updated_at, last_message_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    conversation.id,
                    conversation.scope_id,
                    conversation.title,
                    conversation.state,
                    conversation.created_at.isoformat(),
                    conversation.updated_at.isoformat(),
                    conversation.last_message_at.isoformat(),
                ),
            )
        return conversation

    def get(self, conversation_id: str) -> Conversation | None:
        # 软删的会话对外就是「不存在」：``state = 'active'`` 是查询条件而不是调用方的
        # 事后判断——1.0 的 ``get_conversation`` 也是这么做的（``agent_conversation.py:79``）。
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {_CONVERSATION_COLUMNS} FROM agent_conversations WHERE id = ? AND state = 'active'",
                (conversation_id,),
            ).fetchone()
        return _row_to_conversation(row) if row else None

    def list_for_scope(self, scope_id: str, limit: int) -> Sequence[Conversation]:
        with self._database.connect() as connection:
            rows = connection.execute(
                f"SELECT {_CONVERSATION_COLUMNS} FROM agent_conversations"
                " WHERE scope_id = ? AND state = 'active'"
                " ORDER BY last_message_at DESC, id DESC LIMIT ?",
                (scope_id, max(1, limit)),
            ).fetchall()
        return [_row_to_conversation(row) for row in rows]

    def rename(self, conversation_id: str, title: str, updated_at: datetime) -> bool:
        with self._database.connect() as connection, connection:
            cursor = connection.execute(
                "UPDATE agent_conversations SET title = ?, updated_at = ? WHERE id = ? AND state = 'active'",
                (title, updated_at.isoformat(), conversation_id),
            )
        return cursor.rowcount > 0

    def delete(self, conversation_id: str, updated_at: datetime) -> bool:
        """软删会话、硬删消息与回合。

        1.0 的 ``delete_conversation``（``agent_conversation.py:134``）就是这个口径。
        照片、观察与原始资产一律不动——「导入源不得写入记忆」的另一面是「删对话不得删记忆」。
        """
        with self._database.connect() as connection, connection:
            cursor = connection.execute(
                "UPDATE agent_conversations SET state = 'deleted', updated_at = ?"
                " WHERE id = ? AND state = 'active'",
                (updated_at.isoformat(), conversation_id),
            )
            if cursor.rowcount == 0:
                return False
            connection.execute(
                "DELETE FROM agent_turn_events WHERE turn_id IN"
                " (SELECT id FROM agent_turns WHERE conversation_id = ?)",
                (conversation_id,),
            )
            connection.execute("DELETE FROM agent_turns WHERE conversation_id = ?", (conversation_id,))
            connection.execute(
                "DELETE FROM agent_conversation_messages WHERE conversation_id = ?",
                (conversation_id,),
            )
        return True

    def add_message(
        self,
        *,
        conversation_id: str,
        scope_id: str,
        turn_id: str,
        role: str,
        content: str,
        created_at: datetime,
    ) -> Message:
        message = Message(
            id=f"msg_{uuid.uuid4().hex[:12]}",
            conversation_id=conversation_id,
            scope_id=scope_id,
            turn_id=turn_id,
            role=ChatRole(role),
            content=content,
            created_at=created_at,
        )
        with self._database.connect() as connection, connection:
            connection.execute(
                "INSERT INTO agent_conversation_messages"
                " (id, conversation_id, scope_id, turn_id, role, content, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    message.id,
                    message.conversation_id,
                    message.scope_id,
                    message.turn_id,
                    message.role.value,
                    message.content,
                    message.created_at.isoformat(),
                ),
            )
            connection.execute(
                "UPDATE agent_conversations SET last_message_at = ?, updated_at = ? WHERE id = ?",
                (created_at.isoformat(), created_at.isoformat(), conversation_id),
            )
        return message

    def list_messages(self, conversation_id: str, limit: int) -> Sequence[Message]:
        with self._database.connect() as connection:
            rows = connection.execute(
                f"SELECT {_MESSAGE_COLUMNS} FROM agent_conversation_messages"
                " WHERE conversation_id = ? ORDER BY created_at ASC, rowid ASC LIMIT ?",
                (conversation_id, max(1, limit)),
            ).fetchall()
        return [_row_to_message(row) for row in rows]

    def history_for_prompt(self, conversation_id: str, limit: int) -> Sequence[Message]:
        """最近 ``limit`` 条，按时间正序返回。

        1.0 取 8 条（``app.py:2913``），但把它们拼成一段文本折进 system 消息——理由是
        严格 chat template 要求 system 必须在开头。2.0 用原生消息角色，历史就是历史，
        不需要这个绕法。
        """
        with self._database.connect() as connection:
            rows = connection.execute(
                f"SELECT {_MESSAGE_COLUMNS} FROM agent_conversation_messages"
                " WHERE conversation_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (conversation_id, max(1, limit)),
            ).fetchall()
        return [_row_to_message(row) for row in reversed(rows)]
```

`apps/api/src/sentrix/infrastructure/persistence/repositories/turns.py`:

```python
"""回合与回合进度事件的 SQLite 仓储。

``agent_turn_events`` 是 2.0 对 1.0 最直接的一处修正。1.0 的进度只存在 ``app.py`` 的
进程内 dict ``_TURN_JOBS`` 里：永不清理（内存泄漏）、重启即丢（历史 turn 全部 404）、
多 worker 不可用（轮询/SSE 随机 404）、页面刷新就再也拿不到那次回合。前端不得不用 700ms
轮询来兜住这些，668,490 次请求（占全部流量 73%）是这么来的。把事件落库之后，「实时」
由 broker 负责、「迟到加入」由 ``events_after`` 负责，两条路都不需要前端反复问。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sentrix.domain.assistant.models import (
    Attachment,
    ProgressEvent,
    ProgressStage,
    ProgressStatus,
    TurnRecord,
    TurnStatus,
)
from sentrix.infrastructure.persistence.database import Database

_TURN_COLUMNS = (
    "id, conversation_id, scope_id, question, status, termination_reason, answer,"
    " attachments_json, requirements_json, created_at, updated_at"
)


def _row_to_turn(row: sqlite3.Row) -> TurnRecord:
    attachments = json.loads(row["attachments_json"] or "[]")
    return TurnRecord(
        id=row["id"],
        conversation_id=row["conversation_id"],
        scope_id=row["scope_id"],
        question=row["question"],
        status=TurnStatus(row["status"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        answer=row["answer"],
        termination_reason=row["termination_reason"],
        attachments=tuple(
            Attachment(handle=item["handle"], asset_id=item["asset_id"], file_name=item["file_name"])
            for item in attachments
        ),
        requirements=tuple(json.loads(row["requirements_json"] or "[]")),
    )


def _event_to_json(event: ProgressEvent) -> str:
    return json.dumps(
        {
            "step_index": event.step_index,
            "stage": event.stage.value,
            "status": event.status.value,
            "text": event.text,
            "kind": event.kind,
            "timestamp": event.timestamp,
            "payload": dict(event.payload),
        },
        ensure_ascii=False,
    )


def _json_to_event(payload: str) -> ProgressEvent:
    data: Mapping[str, Any] = json.loads(payload)
    return ProgressEvent(
        step_index=int(data["step_index"]),
        stage=ProgressStage(data["stage"]),
        status=ProgressStatus(data["status"]),
        text=str(data["text"]),
        kind=str(data.get("kind") or "status"),
        timestamp=str(data.get("timestamp") or ""),
        payload=data.get("payload") or {},
    )


class SqliteTurnRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def create(self, record: TurnRecord) -> None:
        with self._database.connect() as connection, connection:
            connection.execute(
                "INSERT INTO agent_turns"
                " (id, conversation_id, scope_id, question, status, termination_reason, answer,"
                " attachments_json, requirements_json, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.id,
                    record.conversation_id,
                    record.scope_id,
                    record.question,
                    record.status.value,
                    record.termination_reason,
                    record.answer,
                    "[]",
                    "[]",
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                ),
            )

    def get(self, turn_id: str) -> TurnRecord | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {_TURN_COLUMNS} FROM agent_turns WHERE id = ?", (turn_id,)
            ).fetchone()
        return _row_to_turn(row) if row else None

    def claim(self, turn_id: str, updated_at: datetime) -> bool:
        """``pending`` → ``running``，返回是否抢到。

        用一条带条件的 UPDATE 做 CAS：SQLite 的单条语句是原子的，两个并发请求里只有一个
        的 ``rowcount`` 会是 1。1.0 靠 ``_TURN_JOBS`` 里的内存字段，多进程下直接失效。
        """
        with self._database.connect() as connection, connection:
            cursor = connection.execute(
                "UPDATE agent_turns SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                (
                    TurnStatus.RUNNING.value,
                    updated_at.isoformat(),
                    turn_id,
                    TurnStatus.PENDING.value,
                ),
            )
        return cursor.rowcount > 0

    def append_event(self, turn_id: str, event: ProgressEvent) -> None:
        with self._database.connect() as connection, connection:
            connection.execute(
                "INSERT OR REPLACE INTO agent_turn_events (turn_id, step_index, payload_json) VALUES (?,?,?)",
                (turn_id, event.step_index, _event_to_json(event)),
            )

    def events_after(self, turn_id: str, step_index: int) -> Sequence[ProgressEvent]:
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM agent_turn_events WHERE turn_id = ? AND step_index > ?"
                " ORDER BY step_index ASC",
                (turn_id, step_index),
            ).fetchall()
        return [_json_to_event(row["payload_json"]) for row in rows]

    def finish(
        self,
        turn_id: str,
        *,
        status: str,
        termination_reason: str,
        answer: str,
        attachments: Sequence[Attachment],
        requirements: Sequence[Mapping[str, Any]],
        updated_at: datetime,
    ) -> None:
        payload = json.dumps(
            [
                {"handle": item.handle, "asset_id": item.asset_id, "file_name": item.file_name}
                for item in attachments
            ],
            ensure_ascii=False,
        )
        ledger_payload = json.dumps([dict(item) for item in requirements], ensure_ascii=False)
        with self._database.connect() as connection, connection:
            connection.execute(
                "UPDATE agent_turns SET status = ?, termination_reason = ?, answer = ?,"
                " attachments_json = ?, requirements_json = ?, updated_at = ? WHERE id = ?",
                (
                    status,
                    termination_reason,
                    answer,
                    payload,
                    ledger_payload,
                    updated_at.isoformat(),
                    turn_id,
                ),
            )

    def list_for_conversation(self, conversation_id: str, limit: int) -> Sequence[TurnRecord]:
        with self._database.connect() as connection:
            rows = connection.execute(
                f"SELECT {_TURN_COLUMNS} FROM agent_turns WHERE conversation_id = ?"
                " ORDER BY created_at ASC, rowid ASC LIMIT ?",
                (conversation_id, max(1, limit)),
            ).fetchall()
        return [_row_to_turn(row) for row in rows]
```

`apps/api/src/sentrix/infrastructure/persistence/repositories/result_sets.py`:

```python
"""结果集的 SQLite 仓储。

1.0 把结果集同时放在内存 dict 和 ``agent_result_sets`` 表里（``result_set.py:167-220``），
读取时内存优先。2.0 只留 SQLite 一份：内存副本带来的是「同一份数据两个真相」，而它换来
的加速在一次单行查询上量不出来。

1.0 还有两处没写进库的字段（``ordering`` / ``owner`` / ``revision`` / ``page_size`` /
``shown``），进程重启后静默变默认值。2.0 的表列就是结果集的全部字段，不存在这种半持久化。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import datetime

from sentrix.domain.assistant.result_set import ResultSet
from sentrix.infrastructure.persistence.database import Database


def _row_to_result_set(row: sqlite3.Row) -> ResultSet:
    return ResultSet(
        id=row["id"],
        scope_id=row["scope_id"],
        query=row["query"],
        asset_ids=tuple(json.loads(row["asset_ids_json"] or "[]")),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


class SqliteResultSetRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def add(self, result_set: ResultSet) -> None:
        with self._database.connect() as connection, connection:
            connection.execute(
                "INSERT OR REPLACE INTO agent_result_sets"
                " (id, scope_id, query, asset_ids_json, total, created_at) VALUES (?,?,?,?,?,?)",
                (
                    result_set.id,
                    result_set.scope_id,
                    result_set.query,
                    json.dumps(list(result_set.asset_ids)),
                    len(result_set.asset_ids),
                    result_set.created_at.isoformat(),
                ),
            )

    def get(self, result_set_id: str) -> ResultSet | None:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_result_sets WHERE id = ?", (result_set_id,)
            ).fetchone()
        return _row_to_result_set(row) if row else None

    def list_for_scope(self, scope_id: str, limit: int) -> Sequence[ResultSet]:
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_result_sets WHERE scope_id = ?"
                " ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (scope_id, max(1, limit)),
            ).fetchall()
        return [_row_to_result_set(row) for row in rows]
```

`claim()` 用一条带条件的 UPDATE 做 CAS（`WHERE id = ? AND status = 'pending'`）：SQLite 的单条语句是原子的，两个并发请求里只有一个的 `rowcount` 会是 1。1.0 靠 `_TURN_JOBS` 里的内存字段判断，多进程下直接失效。

- [ ] **Step 4: 运行测试，确认绿灯**

Run: `.venv/bin/pytest apps/api/tests/integration/test_assistant_repositories.py -p no:warnings`

Expected:

```text
12 passed
```

- [ ] **Step 5: 更新 module-map.md**

```markdown
| `sentrix/domain/assistant/ports.py` | 助手域端口：模型 / 会话 / 回合 / 结果集 / 人物目录 / 事件流 | domain | `sentrix/domain/assistant/models.py`、`sentrix/domain/assistant/result_set.py`、`sentrix/domain/assistant/tools.py` | — |
| `sentrix/infrastructure/persistence/repositories/conversations.py` | 会话与消息的 SQLite 仓储 | infrastructure | `sentrix/domain/assistant/models.py`、`sentrix/infrastructure/persistence/database.py` | — |
| `sentrix/infrastructure/persistence/repositories/result_sets.py` | 结果集的 SQLite 仓储；单一行来源，不做内存副本 | infrastructure | `sentrix/domain/assistant/result_set.py`、`sentrix/infrastructure/persistence/database.py` | — |
| `sentrix/infrastructure/persistence/repositories/turns.py` | 回合与回合进度事件的 SQLite 仓储 | infrastructure | `sentrix/domain/assistant/models.py`、`sentrix/infrastructure/persistence/database.py` | — |
```

Run: `.venv/bin/python scripts/check_module_map.py`
Expected: `module-map: 0 个问题`

- [ ] **Step 6: 登记能力**

```json
{"capability": "会话与消息持久化", "origin_kind": "migrated", "source_commit": "2d2cee7c", "source_path": "backend/agent_conversation.py", "source_symbol": "ConversationStore.add_message", "target_path": "apps/api/src/sentrix/infrastructure/persistence/repositories/conversations.py", "verification": "apps/api/tests/integration/test_assistant_repositories.py", "status": "verified"}
{"capability": "回合轨迹与进度事件持久化", "origin_kind": "adapted", "source_commit": "2d2cee7c", "source_path": "backend/db.py", "source_symbol": "agent_trajectories", "target_path": "apps/api/src/sentrix/infrastructure/persistence/repositories/turns.py", "verification": "apps/api/tests/integration/test_assistant_repositories.py", "status": "verified"}
{"capability": "结果集持久化", "origin_kind": "adapted", "source_commit": "2d2cee7c", "source_path": "backend/agent_runtime/result_set.py", "source_symbol": "ResultSetStore._persist", "target_path": "apps/api/src/sentrix/infrastructure/persistence/repositories/result_sets.py", "verification": "apps/api/tests/integration/test_assistant_repositories.py", "status": "verified"}
```

Run: `.venv/bin/python scripts/check_capability_map.py`
Expected: `capability-map: 0 个问题`

- [ ] **Step 7: 全量门禁**

Run: `make ci`

---

## Task 39: 8100 模型客户端、回合事件流与人物目录

**依赖：** Task 38（三个端口）。

**Files:**
- Create: `apps/api/src/sentrix/infrastructure/assistant/__init__.py`
- Create: `apps/api/src/sentrix/infrastructure/assistant/chat_client.py`
- Create: `apps/api/src/sentrix/infrastructure/assistant/events.py`
- Create: `apps/api/src/sentrix/infrastructure/assistant/identity.py`
- Create: `apps/api/tests/integration/test_chat_client.py`
- Create: `apps/api/tests/integration/test_turn_event_broker.py`
- Modify: `docs/architecture/module-map.md`
- Modify: `docs/refactor/capability-map.jsonl`

三个适配器一起落地：它们分别实现 Task 38 定义的 `ChatModel` / `TurnEventBroker` / `PersonDirectory`。

**`chat_client.py` 有两处是实测逼出来的：**

第一处是 `messages[0]` 必须是 system 且只有一条——1.0 的 `400 System message must be at the beginning` 就是中途 append 了一条 `role=system`，宽松的 Gemma 模板容忍、严格的 Qwen 模板直接 400（PhotoBench 上 84% 的题在第二步 preflight 挂掉）。2.0 的守卫在客户端里：越界直接抛，不发请求。测试 `test_a_system_message_in_the_middle_is_refused_before_the_request` 钉它。

第二处不那么显然：**带 `tool_calls` 的 assistant 消息，`content` 必须是 `null` 而不是空串**。这不是风格问题——实测 8100 上写 `""` 会让模型在工具结果回填后的下一轮直接吐 EOS。整条链路的表现是「模型不可用」，而模型一点问题都没有。

**`events.py` 是「前端不轮询」的落点。** 推而不是拉：工作线程 `publish`，每个订阅者一条 `asyncio.Queue`。

- [ ] **Step 1: 先写失败测试**

`apps/api/tests/integration/test_chat_client.py`:

```python
"""模型客户端对真 HTTP 的测试。

用一个真的本地 HTTP 服务（stdlib ``http.server`` 起在线程里）而不是 mock transport：
被测的行为里有一半是「请求长什么样、响应怎么解析」，用假 transport 会把请求序列化也一起
替换掉，而那正是 1.0 出问题的地方（``400 System message must be at the beginning``）。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from sentrix.domain.assistant.models import ChatMessage, ChatRole, ToolCall
from sentrix.domain.assistant.tools import TOOL_SPECS
from sentrix.infrastructure.assistant.chat_client import ChatModelError, OpenAIChatModel


class _Handler(BaseHTTPRequestHandler):
    received: list[dict] = []
    response: dict = {}
    status: int = 200

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的接口
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).received.append({"path": self.path, "body": body})
        payload = json.dumps(type(self).response).encode()
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args: object) -> None:
        pass  # 测试输出里不要 HTTP 访问日志


@pytest.fixture
def server() -> Iterator[str]:
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}/v1"
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture(autouse=True)
def _reset_handler() -> None:
    _Handler.received = []
    _Handler.response = {}
    _Handler.status = 200


def reply(content: str = "好的。", tool_calls: list[dict] | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "choices": [{"message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 3},
    }


def messages() -> list[ChatMessage]:
    return [
        ChatMessage(ChatRole.SYSTEM, "你是家庭相册助手。"),
        ChatMessage(ChatRole.USER, "给我看看在海边的照片"),
    ]


def test_a_plain_reply_is_parsed(server: str):
    _Handler.response = reply("这是你要的海边照片。")

    result = OpenAIChatModel(server, "test-model").complete(messages(), TOOL_SPECS)

    assert result.content == "这是你要的海边照片。"
    assert result.tool_calls == ()
    assert result.wants_tools is False
    assert (result.prompt_tokens, result.completion_tokens) == (11, 3)
    assert _Handler.received[0]["path"] == "/v1/chat/completions"


def test_native_tool_calls_are_parsed(server: str):
    """1.0 让模型在正文里写 JSON，为此写了 150 行解析修复器。原生协议下这些都不需要。"""
    _Handler.response = reply(
        content="",
        tool_calls=[
            {
                "id": "call_abc",
                "type": "function",
                "function": {"name": "search_memories", "arguments": '{"query": "海边"}'},
            }
        ],
    )

    result = OpenAIChatModel(server, "test-model").complete(messages(), TOOL_SPECS)

    assert result.wants_tools is True
    assert result.tool_calls[0].name == "search_memories"
    assert result.tool_calls[0].call_id == "call_abc"
    assert result.tool_calls[0].parse() == {"query": "海边"}


def test_the_tools_field_carries_standard_json_schema(server: str):
    _Handler.response = reply()

    OpenAIChatModel(server, "test-model").complete(messages(), TOOL_SPECS)

    sent = _Handler.received[0]["body"]
    assert sent["tool_choice"] == "auto"
    assert [tool["function"]["name"] for tool in sent["tools"]] == [
        "search_memories",
        "get_result_page",
        "get_original_photos",
        "get_person_profile",
    ]
    assert sent["tools"][0]["function"]["parameters"]["type"] == "object"


def test_the_system_message_is_first_and_only_once(server: str):
    """严格 chat template（Qwen 系、llama.cpp 的 jinja）要求 system 只能在开头。

    1.0 因为中途 append 一条 ``role=system`` 而报 ``400 System message must be at the
    beginning``；生产路径跑的是宽松的 Gemma 模板所以看不出来，PhotoBench 上的严格模板
    有 84% 的题在第二步就挂。这里的守卫在客户端里，越界直接抛，不会发出请求。
    """
    _Handler.response = reply()

    OpenAIChatModel(server, "test-model").complete(messages(), TOOL_SPECS)

    roles = [message["role"] for message in _Handler.received[0]["body"]["messages"]]
    assert roles == ["system", "user"]


def test_a_system_message_in_the_middle_is_refused_before_the_request(server: str):
    broken = [
        ChatMessage(ChatRole.SYSTEM, "系统提示"),
        ChatMessage(ChatRole.USER, "问题"),
        ChatMessage(ChatRole.SYSTEM, "中途追加的约束"),
    ]

    with pytest.raises(ChatModelError):
        OpenAIChatModel(server, "test-model").complete(broken, TOOL_SPECS)

    assert _Handler.received == []


def test_a_conversation_must_start_with_a_system_message(server: str):
    with pytest.raises(ChatModelError):
        OpenAIChatModel(server, "test-model").complete(
            [ChatMessage(ChatRole.USER, "没有 system")], TOOL_SPECS
        )

    assert _Handler.received == []


def test_tool_results_are_sent_back_with_their_call_id(server: str):
    _Handler.response = reply()
    conversation = [
        ChatMessage(ChatRole.SYSTEM, "你是家庭相册助手。"),
        ChatMessage(ChatRole.USER, "给我看看在海边的照片"),
        ChatMessage(
            ChatRole.ASSISTANT,
            "",
            # 与模型返回的形状一致：arguments 是 JSON 字符串
            tool_calls=(
                ToolCall(call_id="call_abc", name="search_memories", arguments_json='{"query": "海边"}'),
            ),
        ),
        ChatMessage(ChatRole.TOOL, '{"result_set_id": "rs_1"}', tool_call_id="call_abc"),
    ]

    OpenAIChatModel(server, "test-model").complete(conversation, TOOL_SPECS)

    sent = _Handler.received[0]["body"]["messages"]
    assert sent[2]["tool_calls"][0]["id"] == "call_abc"
    assert sent[2]["tool_calls"][0]["function"]["arguments"] == '{"query": "海边"}'
    assert sent[3]["role"] == "tool"
    assert sent[3]["tool_call_id"] == "call_abc"


def test_an_assistant_tool_call_message_sends_null_content(server: str):
    """带 tool_calls 的 assistant 消息，``content`` 必须是 null 而不是空串。

    实测 8100（gemma4 模板）：写成 ``""`` 时工具结果回填后的下一轮，模型直接吐一个 EOS
    （``finish_reason="stop"``、1 个 token、content 为空），回合以「模型不可用」收场。
    这条断言看起来吹毛求疵，但它挡的是一个只有打真模型才会暴露的失败。
    """
    _Handler.response = reply()
    conversation = [
        ChatMessage(ChatRole.SYSTEM, "你是家庭相册助手。"),
        ChatMessage(ChatRole.USER, "给我看看在海边的照片"),
        ChatMessage(
            ChatRole.ASSISTANT,
            "",
            tool_calls=(ToolCall(call_id="c1", name="search_memories", arguments_json='{"query": "海边"}'),),
        ),
        ChatMessage(ChatRole.TOOL, "{}", tool_call_id="c1"),
    ]

    OpenAIChatModel(server, "test-model").complete(conversation, TOOL_SPECS)

    sent = _Handler.received[0]["body"]["messages"]
    assert sent[2]["content"] is None
    assert sent[2]["tool_calls"][0]["id"] == "c1"
    # 不确定结果的普通 assistant 消息仍然带字符串内容。
    assert sent[0]["content"] == "你是家庭相册助手。"


def test_a_non_200_response_becomes_a_chat_model_error(server: str):
    _Handler.status = 500
    _Handler.response = {"error": "boom"}

    with pytest.raises(ChatModelError) as error:
        OpenAIChatModel(server, "test-model").complete(messages(), TOOL_SPECS)

    assert "500" in str(error.value)


def test_an_unreachable_endpoint_becomes_a_chat_model_error():
    model = OpenAIChatModel("http://127.0.0.1:1/v1", "test-model", timeout_s=1.0)

    with pytest.raises(ChatModelError):
        model.complete(messages(), TOOL_SPECS)


def test_an_unrecognisable_body_becomes_a_chat_model_error(server: str):
    _Handler.response = {"nope": True}

    with pytest.raises(ChatModelError):
        OpenAIChatModel(server, "test-model").complete(messages(), TOOL_SPECS)


def test_max_tokens_follows_the_behaviour_baseline_formula(server: str):
    """``min(1501, 4400 - 提示词 token)``，下限 64——1.0 ``app.py:2680-2692``。"""
    _Handler.response = reply()
    long_question = "问" * 9000

    OpenAIChatModel(server, "test-model").complete(
        [ChatMessage(ChatRole.SYSTEM, "系统"), ChatMessage(ChatRole.USER, long_question)],
        TOOL_SPECS,
    )

    assert _Handler.received[0]["body"]["max_tokens"] == 64


def test_a_short_prompt_gets_the_full_answer_budget(server: str):
    _Handler.response = reply()

    OpenAIChatModel(server, "test-model").complete(messages(), TOOL_SPECS)

    assert _Handler.received[0]["body"]["max_tokens"] == 1501
```

`apps/api/tests/integration/test_turn_event_broker.py`:

```python
"""进程内回合事件流的行为。

这是「前端不轮询」的落点，所以它必须经得起三件事：事件真的在产出时就被推出去、
迟到的订阅者拿得到积压、收尾之后内存里不再留着。

1.0 的 SSE 端点自己每 300ms 去问一次内存 dict（``app.py:2980``），而那份 dict 永不清理
（``_TURN_JOBS`` 全仓 8 处引用里没有任何 pop/TTL/del）。这里两件事都反着做。
"""

from __future__ import annotations

import asyncio
import threading

from sentrix.domain.assistant.models import ProgressEvent, ProgressStage, ProgressStatus
from sentrix.infrastructure.assistant.events import InProcessTurnEventBroker


def event(index: int, text: str = "") -> ProgressEvent:
    return ProgressEvent(
        step_index=index,
        stage=ProgressStage.THINKING,
        status=ProgressStatus.RUNNING,
        text=text or f"第 {index} 步",
    )


def test_subscribers_receive_events_published_before_they_attach():
    """回合还在跑的时候迟到几百毫秒，不该丢事件——SSE 连接晚于回合启动是常态。

    收尾**之后**才来的订阅者不走这里：那时 channel 已经清掉了，历史由
    ``TurnRepository.events_after`` 从库里补（见 ``test_a_late_subscriber_...``）。
    """

    async def scenario() -> list[ProgressEvent]:
        broker = InProcessTurnEventBroker()
        broker.publish("turn_1", event(1))
        broker.publish("turn_1", event(2))

        received: list[ProgressEvent] = []
        async for item in broker.subscribe("turn_1"):
            if item is None:
                break
            received.append(item)
            if len(received) == 2:
                broker.finish("turn_1")
        return received

    received = asyncio.run(scenario())

    assert [item.step_index for item in received] == [1, 2]


def test_a_running_turn_streams_events_as_they_are_published():
    """工作线程边跑边推，订阅者按顺序拿到——中间没有轮询。

    这里断言的不只是内容，还有**时序**：生产者被订阅者「解锁」才会推下一条，
    所以收到全部事件就一定意味着它们是流式到达的，不是一次性回放。
    """

    async def scenario() -> list[ProgressEvent]:
        broker = InProcessTurnEventBroker()
        received: list[ProgressEvent] = []
        # 生产者每推一条就等消费者确认收到再推下一条。于是「收到的条数」本身就证明了
        # 事件是一条条流出来的，而不是等回合结束一次性回放的。
        acked = threading.Event()
        acked.set()

        def worker() -> None:
            for index in range(1, 4):
                acked.wait()
                acked.clear()
                broker.publish("turn_1", event(index))
            broker.finish("turn_1")

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        async for item in broker.subscribe("turn_1"):
            if item is None:
                break
            received.append(item)
            acked.set()
        thread.join(timeout=2)
        return received

    received = asyncio.run(scenario())

    assert [item.text for item in received] == ["第 1 步", "第 2 步", "第 3 步"]


def test_a_closed_turn_that_nobody_watched_leaves_nothing_in_memory():
    """没有订阅者时，回合收尾就把 backlog 丢掉——历史事件已经落库了。

    1.0 的 ``_TURN_JOBS`` 永不清理，每个已完成的 turn 连同完整 result 永久驻留。
    """
    broker = InProcessTurnEventBroker()
    broker.publish("turn_1", event(1))
    assert broker.pending() == 1

    broker.finish("turn_1")

    assert broker.pending() == 0


def test_a_closed_turn_is_dropped_after_its_last_subscriber_drains():
    """有人在听的时候，收尾不立刻丢——等最后一个订阅者读完再清。"""

    async def scenario() -> tuple[int, int]:
        broker = InProcessTurnEventBroker()
        broker.publish("turn_1", event(1))

        async for item in broker.subscribe("turn_1"):
            if item is None:
                break
            broker.finish("turn_1")
            during = broker.pending()

        return during, broker.pending()

    during, after = asyncio.run(scenario())

    assert during == 1
    assert after == 0


def test_a_late_subscriber_of_a_forgotten_turn_ends_immediately():
    """迟到的订阅者不会挂住：拿不到 channel 就直接结束，历史事件由数据库补。"""

    async def scenario() -> list[ProgressEvent | None]:
        broker = InProcessTurnEventBroker()
        return [item async for item in broker.subscribe("turn_never_seen")]

    assert asyncio.run(scenario()) == [None]


def test_finishing_an_unknown_turn_is_a_no_op():
    broker = InProcessTurnEventBroker()

    broker.finish("turn_never_seen")

    assert broker.pending() == 0


def test_two_subscribers_both_get_every_event():
    async def scenario() -> tuple[list[int], list[int]]:
        broker = InProcessTurnEventBroker()
        broker.publish("turn_1", event(1))
        broker.publish("turn_1", event(2))

        first: list[int] = []
        second: list[int] = []

        async def drain(sink: list[int]) -> None:
            async for item in broker.subscribe("turn_1"):
                if item is None:
                    return
                sink.append(item.step_index)

        tasks = [asyncio.create_task(drain(first)), asyncio.create_task(drain(second))]
        await asyncio.sleep(0)  # 让两个订阅者都登记上
        for _ in range(50):
            if len(first) == 2 and len(second) == 2:
                break
            await asyncio.sleep(0.01)
        broker.finish("turn_1")
        await asyncio.gather(*tasks)
        return first, second

    first, second = asyncio.run(scenario())

    assert first == [1, 2]
    assert second == [1, 2]
```

- [ ] **Step 2: 运行测试，确认红灯**

Run: `.venv/bin/pytest apps/api/tests/integration/test_chat_client.py apps/api/tests/integration/test_turn_event_broker.py -q -p no:warnings`

Expected:

```text
E   ModuleNotFoundError: No module named 'sentrix.infrastructure.assistant.events'
ERROR apps/api/tests/integration/test_chat_client.py
ERROR apps/api/tests/integration/test_turn_event_broker.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 2 errors during collection !!!!!!!!!!!!!!!!!!!!
```

- [ ] **Step 3: 实现三个适配器**

`apps/api/src/sentrix/infrastructure/assistant/__init__.py`:

```python
"""助手的基础设施适配：模型端点、进度事件流、人物目录。"""
```

`apps/api/src/sentrix/infrastructure/assistant/chat_client.py`:

```python
"""8100 上的 llama.cpp 对话端点（OpenAI 兼容 ``/v1``）。

对应 1.0 ``backend/model_clients.py`` 的 OpenAI 分支。三处与 1.0 的差异，每一处都指向
一个实测出的失败：

1. **原生 ``tools`` / ``tool_calls``，不再让模型在正文里写 JSON。**
   1.0 要模型输出 ``{"action": "tool_call", ...}`` 这样的正文 JSON，为此写了一套
   150 行的解析修复器（``runtime.py:1586-1661``：剥围栏、补花括号、补引号、删尾随逗号），
   失败三次就整轮以「输出无法解析」收场。实测 8100 的 llama.cpp 原生支持 tools
   （``finish_reason="tool_calls"``），这一整套解析器连同它的失败模式一起消失。
2. **``messages[0]`` 一定是 system，且只有一条。** 1.0 的 ``400 System message must be
   at the beginning`` 就是这么来的——中途 append 一条 ``role=system``，宽松的 Gemma
   模板容忍，严格的 Qwen 模板直接 400（PhotoBench 上 84% 的题在第二步 preflight 挂掉）。
   1.0 的修法是 ``_merge_system_constraint`` 把新约束折进 messages[0]；2.0 更简单：
   系统提示词只在开头拼一次，之后不再往里加东西。
3. **``max_tokens`` 按剩余上下文算。** 1.0 是 ``min(1501, 4400 - prompt_tokens)``，下限
   64（``app.py:2680-2692``）。8100 的 ``--ctx-size`` 是 8192，沿用同一个公式。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from sentrix.domain.assistant.models import ChatMessage, ChatRole, ModelReply, ToolCall
from sentrix.domain.assistant.tools import ToolSpec, openai_tools

# 1.0 app.py:2680-2692 的三个人工常数：给回答留的 token 预算上限、给提示词留的总量、
# 以及无论上下文多满都必须留下的最小生成量。
MAX_ANSWER_TOKENS = 1501
PROMPT_BUDGET_TOKENS = 4400
MIN_ANSWER_TOKENS = 64


class ChatModelError(RuntimeError):
    """模型端点不可用或返回了无法理解的东西。调用方据此收尾，不要吞掉。"""


def _dump_message(message: ChatMessage) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": message.role.value, "content": message.content}
    if message.role is ChatRole.ASSISTANT and message.tool_calls:
        # 带 tool_calls 的 assistant 消息，``content`` 必须是 **null** 而不是空串。
        # 实测 8100（gemma4 模板）：写成 ``""`` 时工具结果回填后的下一轮，模型直接吐
        # 一个 EOS（``finish_reason="stop"``、1 个 completion token、content 为空），
        # 整个回合因此以「模型不可用」收场——而模型其实一点问题都没有。
        # 这也是 OpenAI 的约定：有 tool_calls 时 content 可为 null。
        payload["content"] = message.content or None
        payload["tool_calls"] = [
            {
                "id": call.call_id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments_json},
            }
            for call in message.tool_calls
        ]
    if message.role is ChatRole.TOOL:
        payload["tool_call_id"] = message.tool_call_id
    return payload


def _read_tool_calls(raw: object) -> tuple[ToolCall, ...]:
    if not isinstance(raw, list):
        return ()
    calls: list[ToolCall] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            continue
        function = item.get("function")
        if not isinstance(function, Mapping):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        arguments = function.get("arguments")
        calls.append(
            ToolCall(
                call_id=str(item.get("id") or f"call_{index}"),
                name=name,
                arguments_json=arguments if isinstance(arguments, str) else json.dumps(arguments),
            )
        )
    return tuple(calls)


class OpenAIChatModel:
    """同步 HTTP 客户端。2.0 不管理 8100 的进程，只连它。"""

    def __init__(self, base_url: str, model: str, *, timeout_s: float = 120.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout_s = timeout_s

    def complete(self, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec]) -> ModelReply:
        if not messages or messages[0].role is not ChatRole.SYSTEM:
            raise ChatModelError("对话必须以一条 system 消息开头（严格 chat template 的硬要求）")
        if any(message.role is ChatRole.SYSTEM for message in messages[1:]):
            raise ChatModelError("system 消息只能出现在开头；后续约束请折进第一条")

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [_dump_message(message) for message in messages],
            "temperature": 0.0,
            "stream": False,
            "max_tokens": self._max_tokens(messages),
        }
        if tools:
            payload["tools"] = list(openai_tools(tools))
            payload["tool_choice"] = "auto"

        try:
            with httpx.Client(timeout=self._timeout_s) as client:
                response = client.post(f"{self._base_url}/chat/completions", json=payload)
        except httpx.HTTPError as error:
            raise ChatModelError(f"模型端点不可达：{error}") from error

        if response.status_code != 200:
            raise ChatModelError(f"模型返回 {response.status_code}：{response.text[:200]}")

        try:
            body = response.json()
            choice = body["choices"][0]
            message = choice["message"]
        except (ValueError, KeyError, IndexError) as error:
            raise ChatModelError(f"模型响应结构不可识别：{response.text[:200]}") from error

        usage = body.get("usage") or {}
        return ModelReply(
            content=str(message.get("content") or ""),
            tool_calls=_read_tool_calls(message.get("tool_calls")),
            finish_reason=str(choice.get("finish_reason") or ""),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )

    @staticmethod
    def _max_tokens(messages: Sequence[ChatMessage]) -> int:
        """按 1.0 的公式估：``min(1501, 4400 - 提示词 token)``，下限 64。

        这里用字符数除以 2 粗估提示词 token——中文在 gemma 的词表里大致一个字一到两个
        token。估得准不准只影响生成长度上限，不影响正确性；真要精确就得再打一次
        tokenize 端点，那是 S6 的事。
        """
        prompt_tokens = sum(len(message.content) for message in messages) // 2
        return max(MIN_ANSWER_TOKENS, min(MAX_ANSWER_TOKENS, PROMPT_BUDGET_TOKENS - prompt_tokens))
```

`apps/api/src/sentrix/infrastructure/assistant/events.py`:

```python
"""进程内的回合事件流：把工作线程产出的事件推给 SSE 订阅者。

这是「前端不轮询」的落点。1.0 的 SSE 端点自己每 300ms 去问一次内存 dict
（``app.py:2980``），而那份 dict 永不清理——实测每个已完成的 turn 连同完整 result 永久
驻留（``_TURN_JOBS`` 全仓 8 处引用里没有任何 pop/TTL/del）。一旦前端的
``EventSource.onerror`` 触发，它就永久降级成 700ms 轮询（668,490 次请求）。

这里的做法是推送而不是轮询：工作线程 ``publish``，每个订阅者有自己的 ``asyncio.Queue``。
跨线程唤醒走 ``loop.call_soon_threadsafe``——``publish`` 从工作线程调用，事件循环在另一个
线程上跑，这是唯一安全的交接方式。

**每个订阅者一条队列，不是共用一个 Event。** 共用会有一个只在多订阅者时出现的死锁：
第二个订阅者若把 ``channel.wake`` 整个换掉，先来的那个会永远等在旧对象上。单订阅者的
测试永远发现不了它。

**生命周期有边界**：一个回合的事件只在「回合结束且最后一个订阅者读完」之前驻留内存，
之后清掉。迟到的订阅者拿不到 channel，直接结束——它的历史事件由
``TurnRepository.events_after`` 从库里补。内存与持久化各管一段，不留无主的状态。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from sentrix.domain.assistant.models import ProgressEvent


@dataclass
class _Channel:
    backlog: list[ProgressEvent] = field(default_factory=list)
    closed: bool = False
    subscribers: int = 0
    loop: asyncio.AbstractEventLoop | None = None
    queues: set[asyncio.Queue[ProgressEvent | None]] = field(default_factory=set)

    def broadcast(self, item: ProgressEvent | None) -> None:
        if self.loop is None:
            return
        for queue in list(self.queues):
            self.loop.call_soon_threadsafe(queue.put_nowait, item)


class InProcessTurnEventBroker:
    def __init__(self) -> None:
        self._channels: dict[str, _Channel] = {}

    def _channel(self, turn_id: str) -> _Channel:
        return self._channels.setdefault(turn_id, _Channel())

    def open(self, turn_id: str) -> None:
        """回合开始时就登记好 channel。

        不这么做会有一个真实的竞态：SSE 连接可能比工作线程的第一条事件更早到达，于是
        ``subscribe`` 找不到 channel、直接结束，SSE 端点在回合还没跑完时就发完
        ``complete`` 收摊。提前登记把「回合存在」与「有人订阅」解耦开。
        """
        self._channel(turn_id)

    def publish(self, turn_id: str, event: ProgressEvent) -> None:
        channel = self._channel(turn_id)
        channel.backlog.append(event)
        channel.broadcast(event)

    def finish(self, turn_id: str) -> None:
        channel = self._channels.get(turn_id)
        if channel is None:
            return
        channel.closed = True
        channel.broadcast(None)
        if channel.subscribers == 0:
            # 没有人在听，也就没有理由把 backlog 留着——历史事件已经落库了。
            self._channels.pop(turn_id, None)

    async def subscribe(self, turn_id: str) -> AsyncIterator[ProgressEvent | None]:
        """先补发订阅之前积压的事件，再等增量。结束时产出 ``None``。"""
        channel = self._channels.get(turn_id)
        if channel is None:
            # 回合已经收尾并且已经有人读完了。历史事件走数据库，这里直接结束。
            yield None
            return

        queue: asyncio.Queue[ProgressEvent | None] = asyncio.Queue()
        channel.loop = asyncio.get_running_loop()
        # 先登记队列再读 backlog：反过来的话，两步之间发布的事件会两边都错过。
        # 代价是重叠——同一条事件可能既在 backlog 又在队列里，靠 ``step_index`` 去重。
        channel.queues.add(queue)
        channel.subscribers += 1
        seen = 0
        try:
            for buffered in list(channel.backlog):
                seen = max(seen, buffered.step_index)
                yield buffered
            while True:
                item = await queue.get()
                if item is None:
                    break
                if item.step_index <= seen:
                    continue
                seen = item.step_index
                yield item
        finally:
            # 清理必须在 ``yield None`` **之前**发生：订阅者收到 None 就 break，不会再去
            # 迭代一次生成器，于是 ``yield None`` 之后的代码与生成器的 ``finally`` 都要等
            # 垃圾回收才跑——那时 channel 已经白白多留了一轮。
            channel.queues.discard(queue)
            channel.subscribers -= 1
            if channel.closed and channel.subscribers == 0:
                self._channels.pop(turn_id, None)
        yield None

    def pending(self) -> int:
        """当前驻留在内存里的回合数。测试用它证明「收尾之后不再堆积」。"""
        return len(self._channels)
```

`apps/api/src/sentrix/infrastructure/assistant/identity.py`:

```python
"""S3 的人物目录实现：**还没有身份层**，如实报告而不是编造。

2.0 的身份能力（人脸聚类 → 簇确认 → 命名 → 实体）是 S5。S3 在这之前上线，所以
``get_person_profile`` 这个工具在当前装配下只有一种诚实的结果：没有可用的人物档案。

这不是一个「空实现占位」，而是 1.0 已经写好的两条降级路径的 2.0 版本
（``tools.py:2655-2664``：``readiness="limited"`` + ``insufficient_evidence=True``）。
产品硬约束第 5 条要求「无证据时输出 gap 不编造」——把「没有数据」返回成 ``None``、
由工具如实说明，正是这条约束在人物问题上的落点。

S5 落地时替换的是这个适配器（实现同一个 ``PersonDirectory`` 端口），工具契约、账本与
门禁一行都不用动。
"""

from __future__ import annotations

from sentrix.domain.assistant.models import PersonProfile


class NoIdentityDirectory:
    """S3 的装配。永远返回 ``None``——「查不到」与「没有这个人」在这里是同一件事，
    因为身份层根本还没建起来。"""

    def lookup(self, name: str, scope_id: str) -> PersonProfile | None:
        return None
```

**broker 用的是每订阅者一条队列，不是共用一个 `asyncio.Event`。** 共用在只有一个订阅者时完全正常，多一个就死锁：第二个订阅者把 `channel.wake` 整个换掉，先来的那个会永远等在旧对象上。这个 bug 在实现过程中真的写出来过，靠 `test_two_subscribers_both_get_every_event` 抓住——单订阅者的测试永远发现不了它。

另一处顺序同样要紧：**`finally` 里的清理必须在 `yield None` 之前发生。** 订阅者收到 `None` 就 `break`，不会再去迭代一次生成器，于是 `yield None` 之后的代码要等垃圾回收才跑——那时 channel 已经白白多留了一轮。

- [ ] **Step 4: 运行测试，确认绿灯**

Run: `.venv/bin/pytest apps/api/tests/integration/test_chat_client.py apps/api/tests/integration/test_turn_event_broker.py -p no:warnings`

Expected:

```text
20 passed
```

- [ ] **Step 5: 更新 module-map.md**

```markdown
| `sentrix/infrastructure/assistant/__init__.py` | 包标记 | infrastructure | — | — |
| `sentrix/infrastructure/assistant/chat_client.py` | 8100 的 OpenAI 兼容客户端；原生 tools / tool_calls | infrastructure | `sentrix/domain/assistant/models.py`、`sentrix/domain/assistant/tools.py` | — |
| `sentrix/infrastructure/assistant/events.py` | 进程内回合事件流；回合收尾后不再驻留内存 | infrastructure | `sentrix/domain/assistant/models.py` | — |
| `sentrix/infrastructure/assistant/identity.py` | S3 的人物目录：身份层属于 S5，如实返回 None 而不是编造 | infrastructure | `sentrix/domain/assistant/models.py` | — |
```

Run: `.venv/bin/python scripts/check_module_map.py`
Expected: `module-map: 0 个问题`

- [ ] **Step 6: 登记能力**

```json
{"capability": "模型端点（OpenAI 兼容 /v1，8100）", "origin_kind": "adapted", "source_commit": "2d2cee7c", "source_path": "backend/model_clients.py", "source_symbol": "OpenAICompatibleInferenceProvider.chat_messages", "target_path": "apps/api/src/sentrix/infrastructure/assistant/chat_client.py", "verification": "apps/api/tests/integration/test_chat_client.py", "status": "verified"}
{"capability": "进程内回合事件流", "origin_kind": "NEW-2.0", "source_commit": "2d2cee7c", "source_path": "backend/app.py", "source_symbol": "_TURN_JOBS", "target_path": "apps/api/src/sentrix/infrastructure/assistant/events.py", "verification": "apps/api/tests/integration/test_turn_event_broker.py", "status": "verified"}
```

Run: `.venv/bin/python scripts/check_capability_map.py`
Expected: `capability-map: 0 个问题`

- [ ] **Step 7: 全量门禁**

Run: `make ci`

---

## Task 40: 四个工具的执行器

**依赖：** Task 36（`ResultSet`）、Task 38（`ResultSetRepository`）、Task 39（`PersonDirectory`）。

**Files:**
- Create: `apps/api/src/sentrix/application/assistant/__init__.py`
- Create: `apps/api/src/sentrix/application/assistant/tools.py`
- Create: `apps/api/tests/integration/test_assistant_toolbox.py`
- Modify: `docs/architecture/module-map.md`
- Modify: `docs/refactor/capability-map.jsonl`

**每个工具产出两份投影，这是本任务最重要的设计。** 1.0 在 handler 与模型之间夹了两层过滤（`ToolPolicy._sanitize` 的键白名单 + `runtime.py:131-180` 的 `_model_visible_observation`），而没有任何一处解释为什么需要两层。原因藏在 `runtime.py:116` 的注释里：检索工具一旦把 `total` 交给模型，模型就会把候选池的数量当成统计答案报出去。

2.0 把它写成一份显式的 `ToolOutcome`：`observation` 是内部用的（含 `asset_id`、`total`、`degraded`），`model_visible` 是发回模型的那一份。两者的差集就是这个工具的信息边界，测试逐个钉住。

`get_person_profile` 在 S3 只有一种诚实的结果：查不到。身份层（人脸聚类 → 簇确认 → 命名 → 实体）是 S5。这不是占位实现——它是 1.0 已经写好的两条降级路径（`tools.py:2655-2664` 的 `readiness="limited"` + `insufficient_evidence=True`）的 2.0 版本，落点是「如实封口」而不是「让门禁一遍遍逼模型去查一个查不到的东西」。

- [ ] **Step 1: 先写失败测试**

`apps/api/tests/integration/test_assistant_toolbox.py`:

```python
"""四个工具执行器的行为：真 SQLite 向量库 + 真结果集仓储 + 脚本化嵌入。

这里钉的是每个工具的**信息边界**——``observation``（内部）与 ``model_visible``（发回模型的）
两份投影差在哪。1.0 靠 ``ToolPolicy._sanitize`` 与 ``_model_visible_observation`` 两层过滤
做同一件事，但没有任何一处解释为什么需要两层；这里把它写成可断言的事实。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sentrix.application.assistant.tools import AssistantToolbox, is_empty_result
from sentrix.application.retrieval.retrieve_visual import RetrieveVisual
from sentrix.domain.assistant.evidence_ledger import EvidenceType
from sentrix.domain.assistant.models import PersonProfile
from sentrix.domain.media.models import Asset, AssetStatus, GpsPoint, MediaType
from sentrix.domain.retrieval import scheme
from sentrix.domain.retrieval.ports import VectorRecord
from sentrix.infrastructure.persistence.repositories.assets import SqliteAssetRepository
from sentrix.infrastructure.persistence.repositories.result_sets import SqliteResultSetRepository
from sentrix.infrastructure.persistence.repositories.vectors import SqliteVectorRepository

NOW = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)
SCOPE = "album1"


class ScriptedEmbedder:
    """文本与图片都映射到指定向量——让「哪张图排第一」是构造出来的。"""

    model_id = scheme.IMAGE_MODEL
    dimension = 4

    def embed_query(self, text: str) -> Sequence[float]:
        digest = hashlib.sha256(text.encode()).digest()
        return [digest[0] / 255, digest[1] / 255, 0.0, 0.0]

    def embed_image(self, path: Path) -> Sequence[float]:
        digest = hashlib.sha256(Path(path).read_bytes()).digest()
        return [digest[0] / 255, digest[1] / 255, 0.0, 0.0]


class StubPeople:
    def __init__(self, profiles: dict[str, PersonProfile] | None = None) -> None:
        self._profiles = profiles or {}

    def lookup(self, name: str, scope_id: str) -> PersonProfile | None:
        return self._profiles.get(name)


@pytest.fixture
def toolbox(migrations_ready_db, tmp_path):
    assets = SqliteAssetRepository(migrations_ready_db)
    vectors = SqliteVectorRepository(migrations_ready_db)
    result_sets = SqliteResultSetRepository(migrations_ready_db)
    media = tmp_path / "media"
    media.mkdir()
    for index, word in enumerate(("海边", "雪山")):
        path = media / f"{word}.jpg"
        path.write_bytes(f"图片字节 {word}".encode())
        asset = Asset(
            id=f"asset_{index}",
            scope_id=SCOPE,
            file_name=f"{word}.jpg",
            media_type=MediaType.IMAGE,
            path=str(path),
            mime_type="image/jpeg",
            size_bytes=path.stat().st_size,
            status=AssetStatus.PROCESSED,
            content_sha256=f"hash{index}",
            captured_at=datetime(2019, 8, index + 1, tzinfo=UTC),
            captured_location=GpsPoint(30.0, 120.0),
            created_at=NOW,
            updated_at=NOW,
        )
        assets.add(asset)
        vectors.upsert(
            VectorRecord(
                scope_id=SCOPE,
                space=scheme.VISUAL_SPACE,
                source_type="asset",
                source_id=asset.id,
                model_name=scheme.IMAGE_MODEL,
                vector=tuple(ScriptedEmbedder().embed_query(word)),
            )
        )

    build = AssistantToolbox(
        retrieval=RetrieveVisual(embedder=ScriptedEmbedder(), vectors=vectors, index=None),
        assets=assets,
        result_sets=result_sets,
        people=StubPeople({"妈妈": PersonProfile(name="妈妈", family_role="母亲", confirmed=True)}),
    )
    return build, result_sets


def run(toolbox, name: str, **arguments: object):
    build, _result_sets = toolbox
    return build.execute(name, arguments, scope_id=SCOPE, now=NOW)


# ---- search_memories --------------------------------------------------------


def test_search_returns_a_result_set_with_a_preview(toolbox):
    """S2 没有相关性阈值，所以两行全在结果集里——顺序才是被测的东西。"""
    outcome = run(toolbox, "search_memories", query="海边")

    assert outcome.ok
    assert outcome.observation["total"] == 2
    assert outcome.observation["asset_ids"][0] == "asset_0"
    preview = outcome.model_visible["preview"]
    assert [item["handle"] for item in preview] == ["photo_1", "photo_2"]
    assert preview[0]["file_name"] == "海边.jpg"


def test_the_model_never_sees_asset_ids_or_the_candidate_total(toolbox):
    """两份投影的差集就是这个工具的信息边界。

    ``total`` 只进 ``observation``：1.0 实测过模型会把候选池的数量直接当成答案里的统计
    数字（``runtime.py:116``）。``asset_id`` 同理——模型只需要 handle，真正可交付的资产
    由运行时解引用。
    """
    outcome = run(toolbox, "search_memories", query="海边")

    assert "total" in outcome.observation
    assert "asset_ids" in outcome.observation
    assert "total" not in outcome.model_visible
    assert "asset_ids" not in outcome.model_visible
    assert "photo_1" not in str(outcome.observation.get("asset_ids"))


def test_search_records_the_retrieval_backend_for_the_internal_projection(toolbox):
    outcome = run(toolbox, "search_memories", query="海边")

    assert outcome.observation["backend"] == "sqlite_exact"
    assert outcome.observation["degraded"] is True
    assert "degraded" not in outcome.model_visible


def test_a_blank_query_fails_without_touching_retrieval(toolbox):
    outcome = run(toolbox, "search_memories", query="   ")

    assert not outcome.ok
    assert outcome.failure_reason == "empty_query"
    assert outcome.model_visible["error"] == "empty_query"


def test_an_empty_scope_is_reported_as_zero(toolbox):
    """空库与「没搜到」是两件事。S2 没有相关性阈值，所以只会因为**一条向量都没有**
    而返回空——这正是门禁要认的那个事实（``fabrication_from_empty`` 的依据）。"""
    build, _result_sets = toolbox

    outcome = build.execute("search_memories", {"query": "海边"}, scope_id="空相册", now=NOW)

    assert outcome.observation["total"] == 0
    assert is_empty_result(outcome) is True
    assert outcome.model_visible["preview"] == []


# ---- get_result_page --------------------------------------------------------


def test_paging_requires_a_known_result_set(toolbox):
    outcome = run(toolbox, "get_result_page", result_set_id="rs_nope", page=1)

    assert not outcome.ok
    assert outcome.failure_reason == "unknown_result_set"


def test_paging_returns_the_requested_page(toolbox):
    search = run(toolbox, "search_memories", query="海边")
    result_set_id = search.model_visible["result_set_id"]

    outcome = run(toolbox, "get_result_page", result_set_id=result_set_id, page=1)

    assert outcome.ok
    assert outcome.model_visible["page"] == 1
    assert outcome.model_visible["result_set_id"] == result_set_id


def test_a_page_past_the_end_is_empty_not_an_error(toolbox):
    search = run(toolbox, "search_memories", query="海边")
    result_set_id = search.model_visible["result_set_id"]

    outcome = run(toolbox, "get_result_page", result_set_id=result_set_id, page=99)

    assert outcome.ok
    assert outcome.model_visible["preview"] == []


# ---- get_original_photos ----------------------------------------------------


def test_delivery_turns_handles_into_openable_attachments(toolbox):
    search = run(toolbox, "search_memories", query="海边")
    result_set_id = search.model_visible["result_set_id"]

    outcome = run(toolbox, "get_original_photos", result_set_id=result_set_id, handles=["photo_1"])

    assert outcome.ok
    assert [item.asset_id for item in outcome.attachments] == ["asset_0"]
    assert outcome.attachments[0].file_name == "海边.jpg"
    assert outcome.model_visible["delivered"] == 1


def test_delivery_rejects_a_handle_outside_the_result_set(toolbox):
    """模型只能交付它真的看过的图——这是结构性事实，不是提示词约束。"""
    search = run(toolbox, "search_memories", query="海边")
    result_set_id = search.model_visible["result_set_id"]

    outcome = run(toolbox, "get_original_photos", result_set_id=result_set_id, handles=["photo_7"])

    assert not outcome.ok
    assert outcome.failure_reason == "bad_handle"
    assert outcome.attachments == ()


def test_delivery_needs_an_explicit_handle_list(toolbox):
    """不做回填：模型没选图就是没交付，交付为空是合法状态。"""
    search = run(toolbox, "search_memories", query="海边")
    result_set_id = search.model_visible["result_set_id"]

    outcome = run(toolbox, "get_original_photos", result_set_id=result_set_id)

    assert not outcome.ok
    assert outcome.failure_reason == "missing_handle"


def test_delivery_requires_a_known_result_set(toolbox):
    outcome = run(toolbox, "get_original_photos", result_set_id="rs_nope", handles=["photo_1"])

    assert not outcome.ok
    assert outcome.failure_reason == "unknown_result_set"


# ---- get_person_profile -----------------------------------------------------


def test_a_known_person_returns_the_family_role(toolbox):
    outcome = run(toolbox, "get_person_profile", person="妈妈")

    assert outcome.ok
    assert outcome.model_visible == {"person": "妈妈", "family_role": "母亲"}
    assert outcome.blocks is None


def test_an_unknown_person_returns_a_gap_and_blocks_the_requirement(toolbox):
    """S3 还没有身份层（S5 落地）。查不到就如实封口，而不是让门禁一遍遍逼模型去查。"""
    outcome = run(toolbox, "get_person_profile", person="隔壁老王")

    assert outcome.model_visible["found"] is False
    assert outcome.blocks is EvidenceType.PERSON_PROFILE
    assert "不要编造" in outcome.model_visible["note"]


def test_a_blank_person_name_fails(toolbox):
    outcome = run(toolbox, "get_person_profile", person="")

    assert not outcome.ok
    assert outcome.failure_reason == "empty_person"


# ---- 未知工具 ---------------------------------------------------------------


def test_an_unknown_tool_is_reported_not_raised(toolbox):
    outcome = run(toolbox, "inspect_photo", asset_handle="photo_1")

    assert not outcome.ok
    assert outcome.failure_reason == "unknown_tool"
```

- [ ] **Step 2: 运行测试，确认红灯**

Run: `.venv/bin/pytest apps/api/tests/integration/test_assistant_toolbox.py -q -p no:warnings`

Expected:

```text
E   ModuleNotFoundError: No module named 'sentrix.application.assistant'
ERROR apps/api/tests/integration/test_assistant_toolbox.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
```

- [ ] **Step 3: 实现四个工具的执行器**

`apps/api/src/sentrix/application/assistant/__init__.py`:

```python
"""助手用例：跑一个回合、管会话。"""
```

`apps/api/src/sentrix/application/assistant/tools.py`:

```python
"""四个工具的执行器：把工具调用接到真正的检索、资产与人物目录上。

对应 1.0 ``agent_runtime/tools.py`` 的四个 handler（``_search_memories`` 1513 行起、
``_get_result_page`` 2044、``_get_original_photos`` 1966、``_get_person_profile`` 2651）。

**每个工具产出两份投影，这是本模块最重要的设计。** 1.0 在 handler 与模型之间夹了两层
过滤（``ToolPolicy._sanitize`` 的键白名单 + ``runtime.py:131-180`` 的
``_model_visible_observation``），当时没有人解释为什么需要两层。原因在 ``runtime.py:116``
的注释里：检索工具一旦把 ``total`` 交给模型，模型就会把候选池的数量当成统计答案报出去。
2.0 把这件事写成一份显式的 ``ToolOutcome``：

- ``observation``：内部用。含 ``asset_id``、``total``、``degraded`` 这些**不该给模型看**
  的东西，供门禁、账本与交付使用；
- ``model_visible``：发回模型的那一份。只有 handle、文件名、拍摄时间和一句「还有没有更多」。

两者的差集就是这个工具的信息边界。``ToolOutcome`` 上没有「把两份合并」的入口，所以
新增字段时必须先回答它属于哪一边。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sentrix.application.retrieval.retrieve_visual import RetrieveVisual, VisualSearchRequest
from sentrix.domain.assistant.evidence_ledger import EvidenceType
from sentrix.domain.assistant.models import Attachment
from sentrix.domain.assistant.ports import PersonDirectory, ResultSetRepository
from sentrix.domain.assistant.result_set import ResultSet
from sentrix.domain.media.ports import AssetRepository

log = logging.getLogger("sentrix.assistant.tools")

DEFAULT_SEARCH_LIMIT = 12
MAX_SEARCH_LIMIT = 12


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    observation: Mapping[str, Any]
    model_visible: Mapping[str, Any]
    # 这次调用确认了「某类证据本轮拿不到」——账本据此封口，门禁不再逼模型重试。
    blocks: EvidenceType | None = None
    attachments: tuple[Attachment, ...] = ()
    failure_reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.failure_reason


def _failure(reason: str, message: str) -> ToolOutcome:
    """失败也是一份正常的观察。1.0 用异常表达工具失败，再由
    ``ToolPolicy.execute`` 的 ``except Exception`` 兜成一条 decision——那样工具内部的
    真实原因（参数写错 vs 结果集过期）就糊成了一句 ``tool_execution_error``。"""
    return ToolOutcome(
        observation={"failure_reason": reason},
        model_visible={"error": reason, "message": message},
        failure_reason=reason,
    )


def _clamp_limit(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return DEFAULT_SEARCH_LIMIT
    return min(MAX_SEARCH_LIMIT, max(1, parsed))


class AssistantToolbox:
    def __init__(
        self,
        *,
        retrieval: RetrieveVisual,
        assets: AssetRepository,
        result_sets: ResultSetRepository,
        people: PersonDirectory,
    ) -> None:
        self._retrieval = retrieval
        self._assets = assets
        self._result_sets = result_sets
        self._people = people

    def execute(
        self, name: str, arguments: Mapping[str, Any], *, scope_id: str, now: datetime
    ) -> ToolOutcome:
        if name == "search_memories":
            return self._search(arguments, scope_id=scope_id, now=now)
        if name == "get_result_page":
            return self._page(arguments, scope_id=scope_id)
        if name == "get_original_photos":
            return self._deliver(arguments, scope_id=scope_id)
        if name == "get_person_profile":
            return self._person(arguments, scope_id=scope_id)
        return _failure("unknown_tool", f"没有名为 {name} 的工具。")

    # ---- search_memories ----------------------------------------------------

    def _search(self, arguments: Mapping[str, Any], *, scope_id: str, now: datetime) -> ToolOutcome:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return _failure("empty_query", "query 不能为空，请用用户问题里的实词。")

        result = self._retrieval.execute(
            VisualSearchRequest(text=query, scope_id=scope_id, limit=_clamp_limit(arguments.get("limit")))
        )
        result_set = ResultSet(
            id=f"rs_{uuid.uuid4().hex[:12]}",
            scope_id=scope_id,
            query=query,
            asset_ids=tuple(hit.asset_id for hit in result.hits),
            created_at=now,
        )
        self._result_sets.add(result_set)
        return self._page_outcome(
            result_set,
            page_no=1,
            extra={
                "backend": result.backend,
                "degraded": result.degraded,
                "degraded_reason": result.degraded_reason,
                "authoritative_vectors": result.authoritative_vectors,
            },
        )

    # ---- get_result_page ----------------------------------------------------

    def _page(self, arguments: Mapping[str, Any], *, scope_id: str) -> ToolOutcome:
        result_set = self._result_sets.get(str(arguments.get("result_set_id") or ""))
        if result_set is None:
            return _failure("unknown_result_set", "结果集不存在。请重新调用 search_memories。")
        if result_set.scope_id != scope_id:
            return _failure("scope_mismatch", "这个结果集不属于当前相册。")
        try:
            page_no = max(1, int(arguments.get("page") or 1))
        except (TypeError, ValueError):
            page_no = 1
        return self._page_outcome(result_set, page_no=page_no)

    def _page_outcome(
        self, result_set: ResultSet, *, page_no: int, extra: Mapping[str, Any] | None = None
    ) -> ToolOutcome:
        page = result_set.page(page_no)
        preview = [self._preview(entry.handle, entry.asset_id) for entry in page.entries]
        model_visible: dict[str, Any] = {
            "result_set_id": result_set.id,
            "query": result_set.query,
            "page": page.page,
            "page_size": page.page_size,
            "has_more": page.has_more,
            "preview": preview,
        }
        # ``total`` 刻意**不进** model_visible：1.0 实测过模型会把候选池的数量直接当成
        # 答案里的统计数字（``runtime.py:116``）。模型要判断「还有没有更多」看 has_more
        # 就够了，那是个布尔，不会被当成计数。
        observation: dict[str, Any] = {
            "result_set_id": result_set.id,
            "query": result_set.query,
            "total": page.total,
            "has_more": page.has_more,
            "asset_ids": list(result_set.asset_ids),
        }
        if extra:
            observation.update(extra)
        return ToolOutcome(observation=observation, model_visible=model_visible)

    def _preview(self, handle: str, asset_id: str) -> Mapping[str, Any]:
        asset = self._assets.get(asset_id)
        return {
            "handle": handle,
            "file_name": asset.file_name if asset else "",
            "captured_at": asset.captured_at.isoformat() if asset and asset.captured_at else "",
        }

    # ---- get_original_photos ------------------------------------------------

    def _deliver(self, arguments: Mapping[str, Any], *, scope_id: str) -> ToolOutcome:
        result_set = self._result_sets.get(str(arguments.get("result_set_id") or ""))
        if result_set is None:
            return _failure("unknown_result_set", "结果集不存在。请先调用 search_memories。")
        if result_set.scope_id != scope_id:
            return _failure("scope_mismatch", "这个结果集不属于当前相册。")

        raw_handles = arguments.get("handles")
        if not isinstance(raw_handles, (list, tuple)) or not raw_handles:
            return _failure("missing_handle", 'handles 必须是非空的 handle 数组，例如 ["photo_1"]。')

        attachments: list[Attachment] = []
        rejected: list[str] = []
        for item in raw_handles:
            handle = str(item or "").strip()
            asset_id = result_set.resolve(handle)
            if asset_id is None:
                rejected.append(handle)
                continue
            asset = self._assets.get(asset_id)
            if asset is None:
                # 向量还在、资产没了。产品硬约束第 5 条：只返回**可打开的**原图。
                log.warning("交付的 handle 指向一个已不存在的资产，已丢弃: %s", asset_id)
                rejected.append(handle)
                continue
            attachments.append(Attachment(handle=handle, asset_id=asset_id, file_name=asset.file_name))

        if not attachments:
            return _failure(
                "bad_handle",
                "这些 handle 都不在当前结果集里。请只用 search_memories / get_result_page 返回的 handle。",
            )
        return ToolOutcome(
            observation={
                "result_set_id": result_set.id,
                "delivered": len(attachments),
                "rejected": rejected,
            },
            model_visible={
                "result_set_id": result_set.id,
                "delivered": len(attachments),
                "rejected": rejected,
                "note": "这些照片已经展示给用户了。",
            },
            attachments=tuple(attachments),
        )

    # ---- get_person_profile -------------------------------------------------

    def _person(self, arguments: Mapping[str, Any], *, scope_id: str) -> ToolOutcome:
        name = str(arguments.get("person") or "").strip()
        if not name:
            return _failure("empty_person", "person 不能为空。")
        profile = self._people.lookup(name, scope_id)
        if profile is None or not profile.confirmed:
            return ToolOutcome(
                observation={"person": name, "found": False},
                model_visible={
                    "person": name,
                    "found": False,
                    "note": "没有找到这位已确认人物的档案，请如实说明无法确认，不要编造。",
                },
                # 身份层还没建起来（S5）。封口而不是让门禁反复逼模型去查一个查不到的东西。
                blocks=EvidenceType.PERSON_PROFILE,
            )
        return ToolOutcome(
            observation={"person": profile.name, "found": True, "family_role": profile.family_role},
            model_visible={"person": profile.name, "family_role": profile.family_role},
        )


def asset_ids_of(outcome: ToolOutcome) -> Sequence[str]:
    """内部投影里的资产 id。交付与统计走这里，不走 ``model_visible``。"""
    raw = outcome.observation.get("asset_ids")
    if isinstance(raw, list):
        return [str(item) for item in raw]
    return []


def is_empty_result(outcome: ToolOutcome) -> bool:
    total = outcome.observation.get("total")
    return isinstance(total, int) and total == 0


@dataclass(frozen=True, slots=True)
class DeliveredPhotos:
    """本回合累计交付的照片。只由模型**显式**调用 ``get_original_photos`` 产生——
    1.0 的交付口径（``runtime.py:1404``）明确不做回填：「模型没选、或答案本就没有依据图
    （拒答/闲聊）→ 交付为空，这是合法状态」。"""

    items: tuple[Attachment, ...] = field(default_factory=tuple)

    def merge(self, attachments: Sequence[Attachment]) -> DeliveredPhotos:
        seen = {item.asset_id for item in self.items}
        return DeliveredPhotos(
            items=self.items + tuple(item for item in attachments if item.asset_id not in seen)
        )

    @property
    def asset_ids(self) -> tuple[str, ...]:
        return tuple(item.asset_id for item in self.items)
```

- [ ] **Step 4: 运行测试，确认绿灯**

Run: `.venv/bin/pytest apps/api/tests/integration/test_assistant_toolbox.py -p no:warnings`

Expected:

```text
16 passed
```

- [ ] **Step 5: 更新 module-map.md**

```markdown
| `sentrix/application/assistant/__init__.py` | 包标记 | application | — | — |
| `sentrix/application/assistant/tools.py` | 四个工具的执行器；每个工具产出内部与模型可见两份投影 | application | `sentrix/application/retrieval/retrieve_visual.py`、`sentrix/domain/assistant/evidence_ledger.py`、`sentrix/domain/assistant/models.py`、`sentrix/domain/assistant/ports.py`、`sentrix/domain/assistant/result_set.py`、`sentrix/domain/media/ports.py` | — |
```

Run: `.venv/bin/python scripts/check_module_map.py`
Expected: `module-map: 0 个问题`

- [ ] **Step 6: 登记能力**

```json
{"capability": "四个助手工具（检索/翻页/原图交付/人物档案）", "origin_kind": "migrated", "source_commit": "2d2cee7c", "source_path": "backend/agent_runtime/tools.py", "source_symbol": "_search_memories", "target_path": "apps/api/src/sentrix/application/assistant/tools.py", "verification": "apps/api/tests/integration/test_assistant_toolbox.py", "status": "verified"}
{"capability": "人物目录（S3 如实返回无数据）", "origin_kind": "adapted", "source_commit": "2d2cee7c", "source_path": "backend/agent_runtime/tools.py", "source_symbol": "_get_person_profile", "target_path": "apps/api/src/sentrix/infrastructure/assistant/identity.py", "verification": "apps/api/tests/integration/test_assistant_toolbox.py", "status": "verified"}
```

Run: `.venv/bin/python scripts/check_capability_map.py`
Expected: `capability-map: 0 个问题`

- [ ] **Step 7: 全量门禁**

Run: `make ci`

---

## Task 41: 一次回合（工具循环 + 门禁 + 诚实收尾）

**依赖：** Task 34–40 全部。

**Files:**
- Create: `apps/api/src/sentrix/application/assistant/run_turn.py`
- Create: `apps/api/src/sentrix/application/assistant/manage_conversations.py`
- Create: `apps/api/tests/integration/test_run_turn.py`
- Modify: `docs/architecture/module-map.md`
- Modify: `docs/refactor/capability-map.jsonl`

把前面所有东西接起来，并**唯一一次**决定「这一轮为什么结束」。对应 1.0 那个 1,700 行的 `AgentRuntime.run()`（`runtime.py:1691-3421`）。

收尾只有一个出口（`_finish`），循环外还有一层兜底：任何未预料的异常也必须以一个有名字的原因结束。这一层在实现过程中被真的用上过——工具执行器抛异常时，回合照样以 `model_error` 结束而不是留下一个半开的回合，测试 `test_an_unexpected_exception_still_produces_a_named_reason` 钉的就是它。

**空回答的恢复路径**是这个任务里唯一一处「跑起来才发现」的设计。实测 8100 上的 gemma4（IQ3_XXS 量化）在工具往返之后**有时会直接吐 EOS**：`finish_reason="stop"`、1 个 completion token、content 为空。它与请求形状无关（带不带 `tools`、`content` 是 null 还是空串、温度 0 到 1、有没有额外字段都复现过），是模型/模板层面的行为。循环必须接住：提醒一次，再空就诚实兜底——此时证据已经拿到了，兜底会把「检索到几张、交付了几张」如实说清楚，而不是丢掉它们报一句「模型不可用」。

- [ ] **Step 1: 先写失败测试**

`apps/api/tests/integration/test_run_turn.py`:

```python
"""一次回合的完整行为：真 SQLite、脚本化的模型、打桩的工具。

模型与工具的形状是真的（原生 tool_calls、两份投影的 ToolOutcome），只有「模型说什么」
是脚本化的——那正是被测对象之外的东西。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

import pytest

from sentrix.application.assistant.manage_conversations import ManageConversations
from sentrix.application.assistant.run_turn import (
    RunTurn,
    TurnAlreadyClaimedError,
    TurnNotFoundError,
    TurnRequest,
)
from sentrix.application.assistant.tools import ToolOutcome
from sentrix.domain.assistant.models import (
    Attachment,
    ChatMessage,
    ChatRole,
    ModelReply,
    ProgressEvent,
    TerminationReason,
    ToolCall,
    TurnStatus,
)
from sentrix.domain.assistant.tools import TOOL_SPECS
from sentrix.infrastructure.persistence.repositories.conversations import (
    SqliteConversationRepository,
)
from sentrix.infrastructure.persistence.repositories.turns import SqliteTurnRepository

QUESTION = "给我看看在海边的照片"


# ---- 假件 -------------------------------------------------------------------


class ScriptedModel:
    """按剧本回话的模型。剧本用完还继续要，就返回最后一条。"""

    def __init__(self, replies: Sequence[ModelReply | Exception]) -> None:
        self._replies = list(replies)
        self.calls: list[Sequence[ChatMessage]] = []

    def complete(self, messages: Sequence[ChatMessage], tools: Sequence[object]) -> ModelReply:
        self.calls.append(list(messages))
        assert [spec.name for spec in tools] == [spec.name for spec in TOOL_SPECS]
        reply = self._replies.pop(0) if len(self._replies) > 1 else self._replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


class StubToolbox:
    def __init__(self, outcomes: Mapping[str, ToolOutcome] | None = None) -> None:
        self._outcomes = dict(outcomes or {})
        self.calls: list[tuple[str, Mapping[str, object]]] = []

    def execute(
        self, name: str, arguments: Mapping[str, object], *, scope_id: str, now: datetime
    ) -> ToolOutcome:
        self.calls.append((name, dict(arguments)))
        outcome = self._outcomes.get(name)
        if outcome is None:
            return ToolOutcome(
                observation={"failure_reason": "not_stubbed"},
                model_visible={"error": "not_stubbed"},
                failure_reason="not_stubbed",
            )
        return outcome


def final(content: str) -> ModelReply:
    return ModelReply(content=content, finish_reason="stop")


def tool_call(name: str, call_id: str, **arguments: object) -> ModelReply:
    return ModelReply(
        content="",
        finish_reason="tool_calls",
        tool_calls=(ToolCall(call_id=call_id, name=name, arguments_json=json.dumps(arguments)),),
    )


SEARCH_OK = ToolOutcome(
    observation={"result_set_id": "rs_1", "query": "海边", "total": 2, "has_more": False},
    model_visible={
        "result_set_id": "rs_1",
        "query": "海边",
        "page": 1,
        "page_size": 6,
        "has_more": False,
        "preview": [
            {"handle": "photo_1", "file_name": "a.jpg", "captured_at": "2017-11-05T16:44:57"},
            {"handle": "photo_2", "file_name": "b.jpg", "captured_at": "2018-08-12T09:10:00"},
        ],
    },
)

DELIVER_OK = ToolOutcome(
    observation={"result_set_id": "rs_1", "delivered": 2, "rejected": []},
    model_visible={"result_set_id": "rs_1", "delivered": 2, "rejected": []},
    attachments=(
        Attachment(handle="photo_1", asset_id="asset_1", file_name="a.jpg"),
        Attachment(handle="photo_2", asset_id="asset_2", file_name="b.jpg"),
    ),
)

SEARCH_EMPTY = ToolOutcome(
    observation={"result_set_id": "rs_2", "query": "海边", "total": 0, "has_more": False},
    model_visible={"result_set_id": "rs_2", "query": "海边", "page": 1, "preview": [], "has_more": False},
)


# ---- 夹具 -------------------------------------------------------------------


@pytest.fixture
def env(migrations_ready_db):
    conversations = SqliteConversationRepository(migrations_ready_db)
    turns = SqliteTurnRepository(migrations_ready_db)
    conversation = conversations.create(
        scope_id="album1", title="新对话", created_at=datetime(2026, 9, 22, tzinfo=UTC)
    )
    return conversations, turns, conversation, migrations_ready_db


def manage_of(env) -> ManageConversations:
    conversations, turns, _conversation, _db = env
    return ManageConversations(conversations=conversations, turns=turns)


def build(env, model, toolbox) -> RunTurn:
    conversations, turns, _conversation, _db = env
    return RunTurn(model=model, conversations=conversations, turns=turns, toolbox=toolbox)


def start(run_turn, conversation) -> str:
    return run_turn.start(
        TurnRequest(conversation_id=conversation.id, scope_id="album1", question=QUESTION)
    ).id


# ---- 正常链路 ---------------------------------------------------------------


def test_the_full_loop_retrieves_delivers_and_answers(env):
    conversations, turns, conversation, _db = env
    model = ScriptedModel(
        [
            tool_call("search_memories", "c1", query="海边"),
            tool_call("get_original_photos", "c2", result_set_id="rs_1", handles=["photo_1", "photo_2"]),
            final("这是你要的两张海边照片。"),
        ]
    )
    toolbox = StubToolbox({"search_memories": SEARCH_OK, "get_original_photos": DELIVER_OK})
    run_turn = build(env, model, toolbox)
    turn_id = start(run_turn, conversation)

    result = run_turn.run(turn_id)

    assert result.status is TurnStatus.COMPLETE
    assert result.termination_reason is TerminationReason.ANSWERED
    assert result.answer == "这是你要的两张海边照片。"
    assert [item.asset_id for item in result.attachments] == ["asset_1", "asset_2"]
    assert [item["status"] for item in result.requirements] == ["satisfied", "satisfied"]
    assert turns.get(turn_id).status is TurnStatus.COMPLETE
    assert turns.get(turn_id).attachments[0].handle == "photo_1"


def test_the_answer_is_persisted_as_an_assistant_message(env):
    conversations, _turns, conversation, _db = env
    model = ScriptedModel([tool_call("search_memories", "c1", query="海边"), final("给你。")])
    run_turn = build(env, model, StubToolbox({"search_memories": SEARCH_OK}))
    turn_id = start(run_turn, conversation)

    run_turn.run(turn_id)

    messages = conversations.list_messages(conversation.id, 10)
    assert [(message.role, message.content) for message in messages] == [
        (ChatRole.USER, QUESTION),
        (ChatRole.ASSISTANT, "给你。"),
    ]


def test_progress_events_are_persisted_in_order(env):
    _conversations, turns, conversation, _db = env
    model = ScriptedModel(
        [
            tool_call("search_memories", "c1", query="海边"),
            tool_call("get_original_photos", "c2", result_set_id="rs_1", handles=["photo_1"]),
            final("给你。"),
        ]
    )
    toolbox = StubToolbox(
        {
            "search_memories": SEARCH_OK,
            "get_original_photos": ToolOutcome(
                observation={"result_set_id": "rs_1", "delivered": 1, "rejected": []},
                model_visible={"result_set_id": "rs_1", "delivered": 1, "rejected": []},
                attachments=(Attachment(handle="photo_1", asset_id="asset_1", file_name="a.jpg"),),
            ),
        }
    )
    run_turn = build(env, model, toolbox)
    turn_id = start(run_turn, conversation)

    result = run_turn.run(turn_id)

    events = turns.events_after(turn_id, 0)
    assert [event.step_index for event in events] == list(range(1, len(events) + 1))
    assert events[0].kind == "plan"
    assert [event.payload["requirements"][0]["status"] for event in events[:1]] == ["open"]
    assert events[-1].kind == "terminal"
    # 事件里记的原因与返回值必须是同一个——1.0 的收尾分类器会覆盖前面设好的原因，
    # 于是轨迹里记的和代码里写的经常不是一回事。
    assert events[-1].payload["termination_reason"] == result.termination_reason.value == "answered"


def test_the_emitted_events_match_what_was_persisted(env):
    """``emit`` 的回调拿到的就是落库的那一份——SSE 与历史回放不该有两个真相。"""
    _conversations, turns, conversation, _db = env
    model = ScriptedModel([tool_call("search_memories", "c1", query="海边"), final("给你。")])
    run_turn = build(env, model, StubToolbox({"search_memories": SEARCH_OK}))
    turn_id = start(run_turn, conversation)
    emitted: list[ProgressEvent] = []

    run_turn.run(turn_id, emit=emitted.append)

    persisted = turns.events_after(turn_id, 0)
    assert [(event.step_index, event.text) for event in emitted] == [
        (event.step_index, event.text) for event in persisted
    ]


def test_history_is_replayed_to_the_model_without_duplicating_the_question(env):
    _conversations, turns, conversation, _db = env
    model = ScriptedModel([tool_call("search_memories", "c1", query="海边"), final("第一轮。")])
    run_turn = build(env, model, StubToolbox({"search_memories": SEARCH_OK}))
    run_turn.run(start(run_turn, conversation))

    second = ScriptedModel([final("第二轮。")])
    run_turn_2 = build(env, second, StubToolbox())
    run_turn_2.run(start(run_turn_2, conversation))

    sent = second.calls[0]
    assert sent[0].role is ChatRole.SYSTEM
    assert [message.role for message in sent] == [
        ChatRole.SYSTEM,
        ChatRole.USER,
        ChatRole.ASSISTANT,
        ChatRole.USER,
    ]
    assert sent[1].content == QUESTION
    assert sent[2].content == "第一轮。"
    assert sent[3].content == QUESTION


# ---- 门禁 -------------------------------------------------------------------


def test_the_delivery_gate_pushes_the_model_to_hand_over_the_photos(env):
    """实测 8100：问「给我看看在海边的照片」，模型检索一次之后直接给文字，一张图都没有。

    门禁拦一次、提醒它调 get_original_photos，然后才放行。
    """
    _conversations, _turns, conversation, _db = env
    model = ScriptedModel(
        [
            tool_call("search_memories", "c1", query="海边"),
            final("我找到了两张在海边的照片。"),
            tool_call("get_original_photos", "c2", result_set_id="rs_1", handles=["photo_1"]),
            final("这是你要的照片。"),
        ]
    )
    toolbox = StubToolbox(
        {
            "search_memories": SEARCH_OK,
            "get_original_photos": ToolOutcome(
                observation={"result_set_id": "rs_1", "delivered": 1, "rejected": []},
                model_visible={"result_set_id": "rs_1", "delivered": 1, "rejected": []},
                attachments=(Attachment(handle="photo_1", asset_id="asset_1", file_name="a.jpg"),),
            ),
        }
    )
    run_turn = build(env, model, toolbox)
    turn_id = start(run_turn, conversation)

    result = run_turn.run(turn_id)

    assert result.answer == "这是你要的照片。"
    assert [item.asset_id for item in result.attachments] == ["asset_1"]
    assert ("get_original_photos", {"result_set_id": "rs_1", "handles": ["photo_1"]}) in toolbox.calls


def test_a_model_that_keeps_refusing_to_deliver_is_let_through_with_a_gap(env):
    """门禁只提醒一次：提醒过就放行，并如实标注这一轮带着缺口收尾。

    1.0 的零检索拦截没有标志位，模型坚持不调工具就会一直被拦到预算耗尽。
    """
    _conversations, _turns, conversation, _db = env
    model = ScriptedModel(
        [
            tool_call("search_memories", "c1", query="海边"),
            final("我找到了两张在海边的照片。"),
            final("就是这两张，您可以点开看。"),
        ]
    )
    run_turn = build(env, model, StubToolbox({"search_memories": SEARCH_OK}))
    turn_id = start(run_turn, conversation)

    result = run_turn.run(turn_id)

    assert result.answer == "就是这两张，您可以点开看。"
    assert result.termination_reason is TerminationReason.ANSWERED_WITH_GAP
    assert result.status is TurnStatus.PARTIAL
    assert result.attachments == ()
    assert len(model.calls) == 3


def test_an_empty_search_cannot_be_reported_as_a_hit(env):
    _conversations, _turns, conversation, _db = env
    model = ScriptedModel(
        [
            tool_call("search_memories", "c1", query="海边"),
            final("我找到了这些海边的照片。"),
            final("这次没有匹配到海边的照片，可能没有拍到过。"),
        ]
    )
    run_turn = build(env, model, StubToolbox({"search_memories": SEARCH_EMPTY}))
    turn_id = start(run_turn, conversation)

    result = run_turn.run(turn_id)

    assert result.answer == "这次没有匹配到海边的照片，可能没有拍到过。"


def test_a_leaked_internal_identifier_is_never_delivered(env):
    """安全边界不做第二次让步：两次都泄漏就给确定性兜底，不把内部标识交给用户。"""
    _conversations, _turns, conversation, _db = env
    leak = "这张是 photo_1，asset_9f3a21b7c4。"
    model = ScriptedModel([tool_call("search_memories", "c1", query="海边"), final(leak), final(leak)])
    run_turn = build(env, model, StubToolbox({"search_memories": SEARCH_OK}))
    turn_id = start(run_turn, conversation)

    result = run_turn.run(turn_id)

    assert result.status is TurnStatus.BLOCKED
    assert result.termination_reason is TerminationReason.GUARD_BLOCKED
    assert "photo_1" not in result.answer


# ---- 收敛与预算 --------------------------------------------------------------


def test_a_second_search_is_refused_and_the_model_is_told_to_page(env):
    _conversations, turns, conversation, _db = env
    model = ScriptedModel(
        [
            tool_call("search_memories", "c1", query="海边"),
            tool_call("search_memories", "c2", query="海边2"),
            final("这是你要的照片。"),
        ]
    )
    toolbox = StubToolbox({"search_memories": SEARCH_OK})
    run_turn = build(env, model, toolbox)
    turn_id = start(run_turn, conversation)

    run_turn.run(turn_id)

    assert [name for name, _ in toolbox.calls] == ["search_memories"]
    refusals = [event for event in turns.events_after(turn_id, 0) if event.status.value == "denied"]
    assert refusals and "get_result_page" in refusals[0].text


def test_a_repeated_identical_call_is_replayed_not_re_executed(env):
    _conversations, _turns, conversation, _db = env
    model = ScriptedModel(
        [
            tool_call("get_result_page", "c1", result_set_id="rs_1", page=1),
            tool_call("get_result_page", "c2", result_set_id="rs_1", page=1),
            final("这是你要的照片。"),
        ]
    )
    toolbox = StubToolbox({"get_result_page": SEARCH_OK})
    run_turn = build(env, model, toolbox)
    turn_id = start(run_turn, conversation)

    run_turn.run(turn_id)

    assert len(toolbox.calls) == 1


def test_the_model_step_budget_ends_the_turn_with_a_reason(env):
    _conversations, _turns, conversation, _db = env
    model = ScriptedModel([tool_call("get_result_page", "c1", result_set_id="rs_1", page=1)])
    run_turn = build(env, model, StubToolbox({"get_result_page": SEARCH_OK}))
    turn_id = start(run_turn, conversation)

    result = run_turn.run(turn_id)

    assert result.termination_reason is TerminationReason.STEP_BUDGET_EXHAUSTED
    assert result.status is TurnStatus.PARTIAL
    assert "结束" in result.answer


def test_a_model_failure_ends_the_turn_as_failed_not_complete(env):
    """1.0 把模型失败压成 ``status="complete"``，前端拿到的是一个「成功」的帧。"""
    _conversations, _turns, conversation, _db = env
    model = ScriptedModel([RuntimeError("端点挂了")])
    run_turn = build(env, model, StubToolbox())
    turn_id = start(run_turn, conversation)

    result = run_turn.run(turn_id)

    assert result.status is TurnStatus.FAILED
    assert result.termination_reason is TerminationReason.MODEL_ERROR
    assert "模型不可用" in result.answer


def test_an_unexpected_exception_still_produces_a_named_reason(env):
    """兜底那层 ``finally`` 的用处：将来的新出口忘了声明原因时，也不会留下半开的回合。

    反向验证：这里让工具箱抛出一个不在预期内的异常，断言收尾原因仍然是枚举里的一个值。
    """

    class Exploding(StubToolbox):
        def execute(self, name, arguments, *, scope_id, now):  # type: ignore[override]
            raise ValueError("工具内部炸了")

    _conversations, turns, conversation, _db = env
    model = ScriptedModel([tool_call("search_memories", "c1", query="海边"), final("好的。")])
    run_turn = build(env, model, Exploding())
    turn_id = start(run_turn, conversation)

    result = run_turn.run(turn_id)

    assert result.termination_reason is TerminationReason.MODEL_ERROR
    assert turns.get(turn_id).status is TurnStatus.FAILED


# ---- 回合的边界 --------------------------------------------------------------


def test_a_turn_cannot_be_started_twice(env):
    _conversations, _turns, conversation, _db = env
    run_turn = build(env, ScriptedModel([final("好。")]), StubToolbox())
    turn_id = start(run_turn, conversation)
    run_turn.run(turn_id)

    with pytest.raises(TurnAlreadyClaimedError):
        run_turn.run(turn_id)


def test_starting_a_turn_on_a_missing_conversation_fails(env):
    run_turn = build(env, ScriptedModel([final("好。")]), StubToolbox())

    with pytest.raises(TurnNotFoundError):
        run_turn.start(TurnRequest(conversation_id="conv_nope", scope_id="album1", question=QUESTION))


def test_start_persists_a_pending_turn_before_anything_runs(env):
    """回合先落库，SSE 才有东西可订阅——1.0 的回合只活在进程内存里。"""
    _conversations, turns, conversation, _db = env
    run_turn = build(env, ScriptedModel([final("好。")]), StubToolbox())

    turn_id = start(run_turn, conversation)

    record = turns.get(turn_id)
    assert record.status is TurnStatus.PENDING
    assert record.question == QUESTION


def test_the_conversation_title_follows_the_first_question(env):
    conversations, _turns, conversation, _db = env
    manage = manage_of(env)

    manage.ensure_title(conversation.id, QUESTION)

    assert conversations.get(conversation.id).title == QUESTION


def test_an_existing_title_is_not_overwritten(env):
    conversations, _turns, _conversation, _db = env
    renamed = conversations.create(
        scope_id="album1", title="我自己起的", created_at=datetime(2026, 9, 22, tzinfo=UTC)
    )
    manage = manage_of(env)

    manage.ensure_title(renamed.id, QUESTION)

    assert conversations.get(renamed.id).title == "我自己起的"


def test_a_long_question_is_truncated_into_a_title(env):
    conversations, _turns, _conversation, _db = env
    manage = manage_of(env)

    created = manage.create(scope_id="album1")
    manage.ensure_title(created.id, "问" * 80)

    title = conversations.get(created.id).title
    assert len(title) == 25  # 24 个字 + 省略号
    assert title.endswith("…")


def test_an_empty_question_keeps_the_default_title(env):
    conversations, _turns, _conversation, _db = env
    manage = manage_of(env)

    created = manage.create(scope_id="album1")
    manage.ensure_title(created.id, "   ")

    assert conversations.get(created.id).title == "新对话"
```

- [ ] **Step 2: 运行测试，确认红灯**

Run: `.venv/bin/pytest apps/api/tests/integration/test_run_turn.py -q -p no:warnings`

Expected:

```text
E   ModuleNotFoundError: No module named 'sentrix.application.assistant.run_turn'
ERROR apps/api/tests/integration/test_run_turn.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
```

- [ ] **Step 3: 实现 run_turn 与 manage_conversations**

`apps/api/src/sentrix/application/assistant/manage_conversations.py`:

```python
"""会话的增删改查。

对应 1.0 ``backend/agent_conversation.py`` 的 ``create_conversation`` / ``list_conversations``
/ ``auto_title`` / ``ensure_title`` / ``delete_conversation``。

标题是**确定性截断**，不调模型——1.0 也是这个做法（``agent_conversation.py:107``：
``text[:24] + "…"``）。为了给会话起个名字再打一次模型，是拿一次可能失败的调用换一行
本来就够用的文本。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sentrix.domain.assistant.models import Conversation, Message, TurnRecord
from sentrix.domain.assistant.ports import ConversationRepository, TurnRepository

TITLE_MAX_CHARS = 24
DEFAULT_TITLE = "新对话"


@dataclass(frozen=True, slots=True)
class ConversationDetail:
    conversation: Conversation
    messages: Sequence[Message]
    turns: Sequence[TurnRecord]


def title_from(question: str) -> str:
    text = (question or "").strip().replace("\n", " ")
    if not text:
        return DEFAULT_TITLE
    return text[:TITLE_MAX_CHARS] + ("…" if len(text) > TITLE_MAX_CHARS else "")


class ManageConversations:
    def __init__(self, *, conversations: ConversationRepository, turns: TurnRepository) -> None:
        self._conversations = conversations
        self._turns = turns

    def create(self, *, scope_id: str, title: str = "") -> Conversation:
        return self._conversations.create(
            scope_id=scope_id, title=title or DEFAULT_TITLE, created_at=datetime.now(UTC)
        )

    def list_for_scope(self, scope_id: str, limit: int = 50) -> Sequence[Conversation]:
        return self._conversations.list_for_scope(scope_id, limit)

    def detail(self, conversation_id: str, limit: int = 200) -> ConversationDetail | None:
        conversation = self._conversations.get(conversation_id)
        if conversation is None:
            return None
        return ConversationDetail(
            conversation=conversation,
            messages=self._conversations.list_messages(conversation_id, limit),
            turns=self._turns.list_for_conversation(conversation_id, limit),
        )

    def rename(self, conversation_id: str, title: str) -> bool:
        return self._conversations.rename(conversation_id, title, datetime.now(UTC))

    def delete(self, conversation_id: str) -> bool:
        return self._conversations.delete(conversation_id, datetime.now(UTC))

    def ensure_title(self, conversation_id: str, question: str) -> None:
        """首轮提问之后把「新对话」换成问题摘要。已经是别的内容就不动。"""
        conversation = self._conversations.get(conversation_id)
        if conversation is None or conversation.title not in {DEFAULT_TITLE, ""}:
            return
        self._conversations.rename(conversation_id, title_from(question), datetime.now(UTC))
```

`apps/api/src/sentrix/application/assistant/run_turn.py`:

```python
"""一次问答回合：规划 → 工具循环 → 证据账本 → 答案门禁 → 回答。

这是设计文档里「在线链路」9 步中 S3 覆盖的那一段（问题 → 规划 → 工具循环 → 检索取证 →
证据账本 → 答案门禁 → 答案）。留给后续切片的：双层防伪的语义评审层（S6）、恢复作答的
四级阶梯（S6/S7）、六路取证的补齐（S6）。

1.0 把这段写成一个 1,700 行的 ``AgentRuntime.run()``，里面混着模型调用、工具派发、
SQLite、预算计数与十几条软门禁。2.0 的划分是：

- **规则**（``domain/assistant``）：状态、策略、账本、门禁——无 IO，可单测；
- **端口**（``domain/assistant/ports.py``）：模型、存储、事件流——domain 只声明形状；
- **本模块**：把上面两样接起来，并**唯一一次**决定「这一轮为什么结束」。

三个 1.0 的失败模式在这里被结构性消掉：

1. **``termination_reason`` 落空**：``TurnResult.termination_reason`` 的类型就是枚举，
   收尾只走 ``_finish`` 一个出口，循环外还有一层兜底防止将来有人加了新出口却忘了声明原因。
2. **零工具调用**：规划永远至少给一条证据需求（``intent.plan_requirements``），门禁在
   需求还开着时拦一次最终答案。1.0 的规划允许「零需求」，而 JIT 提示词会因此直接告诉
   模型「当前没有待确认的证据需求……可直接输出 final」（``jit_prompt.py:200``）。
3. **``planner_call_error``**：S3 的规划是纯函数，没有模型调用，也就不存在调用失败。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sentrix.application.assistant.tools import (
    AssistantToolbox,
    DeliveredPhotos,
    ToolOutcome,
    is_empty_result,
)
from sentrix.domain.assistant import emergency
from sentrix.domain.assistant.evidence_ledger import EvidenceLedger
from sentrix.domain.assistant.final_guard import FinalGuard, answered_with_gap
from sentrix.domain.assistant.intent import plan_requirements
from sentrix.domain.assistant.models import (
    Attachment,
    ChatMessage,
    ChatRole,
    ModelReply,
    ProgressEvent,
    ProgressStage,
    ProgressStatus,
    TerminationReason,
    ToolCall,
    TurnRecord,
    TurnStatus,
)
from sentrix.domain.assistant.ports import ChatModel, ConversationRepository, TurnRepository
from sentrix.domain.assistant.profile import DEFAULT_PROFILE, TurnProfile
from sentrix.domain.assistant.task_state import ACTING, FINALIZING, TaskState, ToolCallRecord
from sentrix.domain.assistant.tool_policy import ToolAction, ToolPolicy
from sentrix.domain.assistant.tools import TOOL_SPECS, TOOLS_BY_NAME

log = logging.getLogger("sentrix.assistant.run_turn")

HISTORY_LIMIT = 8

SYSTEM_PROMPT = """你是这个家庭相册的助手。你能做的只有两件事：检索照片，和读取已确认人物的档案。

规则：
- 需要照片证据时必须先调用工具，不要凭印象回答。
- 用户说「给我看看 / 我想看」时，必须调用 get_original_photos 把照片交出去，只回一段文字不算交付。
- 工具没给到的信息就说不知道，不要编造。
- handle（photo_1 这类）只在调用工具时使用，不要写进给用户的回答里。
- 用户用中文提问，就用中文回答。"""

# 这些门禁拦第二次时不再放行——把内部标识交付给用户是安全边界，不是风格问题。
HARD_GUARD_CODES = frozenset({"internal_reference_leak"})

# 模型返回空回答时给它的一句提醒。实测 8100 上的 gemma4（IQ3_XXS 量化）在工具往返之后
# 会直接吐 EOS——``finish_reason="stop"``、1 个 completion token、content 为空。它与
# 请求形状无关（带不带 tools、content 是 null 还是空串、温度 0 到 1、有没有 note 字段都
# 复现过），是模型/模板层面的行为。循环必须能接住：提醒一次，再空就诚实兜底。
EMPTY_ANSWER_NUDGE = "你上一条回复是空的。请直接用一两句中文把结论说给用户，不要再调用工具。"


class TurnNotFoundError(LookupError):
    pass


class TurnAlreadyClaimedError(RuntimeError):
    """这个回合已经有人在跑了。同一回合只允许一个执行者。"""


@dataclass(frozen=True, slots=True)
class TurnRequest:
    conversation_id: str
    scope_id: str
    question: str


@dataclass(frozen=True, slots=True)
class TurnResult:
    turn_id: str
    conversation_id: str
    status: TurnStatus
    # 非可选：1.0 的 25,112 条轨迹里 11,304 条读不到这个字段，而没有任何东西会因此报错。
    termination_reason: TerminationReason
    answer: str
    attachments: tuple[Attachment, ...]
    requirements: Sequence[dict[str, Any]]


@dataclass
class _Session:
    """一次回合的可变状态。它把「状态 / 账本 / 交付 / 检索是否为空 / 推送出口」收在一起，
    免得每个方法都要拖五个参数。"""

    state: TaskState
    ledger: EvidenceLedger
    emit: Callable[[ProgressEvent], None] | None = None
    delivered: DeliveredPhotos = field(default_factory=DeliveredPhotos)
    search_returned_nothing: bool = False
    # 门禁拦下时写给模型的提醒，由主循环作为一条 user 消息加回去。
    pending_reminder: str = ""


class RunTurn:
    def __init__(
        self,
        *,
        model: ChatModel,
        conversations: ConversationRepository,
        turns: TurnRepository,
        toolbox: AssistantToolbox,
        profile: TurnProfile = DEFAULT_PROFILE,
    ) -> None:
        self._model = model
        self._conversations = conversations
        self._turns = turns
        self._toolbox = toolbox
        self._profile = profile
        self._policy = ToolPolicy()
        self._guard = FinalGuard()

    # ---- 建回合 -------------------------------------------------------------

    def start(self, request: TurnRequest) -> TurnRecord:
        """落一条用户消息与一个 ``pending`` 回合，立刻返回。

        回合先于执行落库，SSE 才有东西可订阅——1.0 的回合只活在进程内存里，页面刷新之后
        那次回合就再也拿不回来了。
        """
        if self._conversations.get(request.conversation_id) is None:
            raise TurnNotFoundError(request.conversation_id)
        now = datetime.now(UTC)
        record = TurnRecord(
            id=f"turn_{now.strftime('%Y%m%d%H%M%S%f')}",
            conversation_id=request.conversation_id,
            scope_id=request.scope_id,
            question=request.question,
            status=TurnStatus.PENDING,
            created_at=now,
            updated_at=now,
        )
        self._turns.create(record)
        self._conversations.add_message(
            conversation_id=record.conversation_id,
            scope_id=record.scope_id,
            turn_id=record.id,
            role=ChatRole.USER.value,
            content=request.question,
            created_at=now,
        )
        return record

    # ---- 跑回合 -------------------------------------------------------------

    def run(self, turn_id: str, *, emit: Callable[[ProgressEvent], None] | None = None) -> TurnResult:
        """执行回合。``emit`` 在每条进度事件落库之后被调用一次（SSE 靠它推送）。"""
        record = self._turns.get(turn_id)
        if record is None:
            raise TurnNotFoundError(turn_id)
        if not self._turns.claim(turn_id, datetime.now(UTC)):
            raise TurnAlreadyClaimedError(turn_id)

        session = _Session(
            state=TaskState(
                turn_id=record.id,
                conversation_id=record.conversation_id,
                scope_id=record.scope_id,
                question=record.question,
                profile=self._profile,
            ),
            ledger=EvidenceLedger(
                scope_id=record.scope_id,
                requirements=list(plan_requirements(record.question)),
            ),
            emit=emit,
        )
        self._publish(
            session,
            session.state.emit(
                ProgressStage.PLANNING,
                ProgressStatus.COMPLETE,
                "已确认需要哪些证据。",
                kind="plan",
                payload={"requirements": list(session.ledger.as_payload())},
            ),
        )

        answer = ""
        try:
            answer = self._converse(session, record)
        except Exception:
            # 兜底不是「错误处理」，是「绝不让收尾原因落空」：任何未预料的异常也必须以
            # 一个有名字的原因结束，而不是留下一个半开的回合。
            log.exception("回合执行中抛出未预料的异常: %s", turn_id)
            session.state.finish(TerminationReason.MODEL_ERROR)
            answer = emergency.clarify_answer(TerminationReason.MODEL_ERROR)

        reason = session.state.termination_reason or session.state.finish(TerminationReason.MODEL_ERROR)
        status = _status_for(reason)
        self._publish(
            session,
            session.state.emit(
                ProgressStage.TERMINAL,
                ProgressStatus.COMPLETE if status is TurnStatus.COMPLETE else ProgressStatus.PARTIAL,
                "已完成本轮回答。" if status is TurnStatus.COMPLETE else "这一轮没能完整完成。",
                kind="terminal",
                payload={"status": status.value, "termination_reason": reason.value},
            ),
        )

        now = datetime.now(UTC)
        # 顺序有意义：**先**把回答写成消息，**再**把回合标成终态。
        # 「终态」是「这一切都已经落库」的承诺——SSE 的 complete 帧就发在这个状态上，
        # 反过来的话，读到 complete 的调用方会看到一个还没有回答的会话。
        self._conversations.add_message(
            conversation_id=record.conversation_id,
            scope_id=record.scope_id,
            turn_id=turn_id,
            role=ChatRole.ASSISTANT.value,
            content=answer,
            created_at=now,
        )
        self._turns.finish(
            turn_id,
            status=status.value,
            termination_reason=reason.value,
            answer=answer,
            attachments=session.delivered.items,
            requirements=list(session.ledger.as_payload()),
            updated_at=now,
        )
        return TurnResult(
            turn_id=turn_id,
            conversation_id=record.conversation_id,
            status=status,
            termination_reason=reason,
            answer=answer,
            attachments=session.delivered.items,
            requirements=[dict(item) for item in session.ledger.as_payload()],
        )

    # ---- 主循环 -------------------------------------------------------------

    def _converse(self, session: _Session, record: TurnRecord) -> str:
        state = session.state
        messages = self._initial_messages(record)

        while True:
            if not state.can_step():
                reason = (
                    TerminationReason.TIMEOUT
                    if state.wall_remaining() <= 0
                    else TerminationReason.STEP_BUDGET_EXHAUSTED
                )
                state.finish(reason)
                return self._stop(session, reason)

            state.model_steps += 1
            state.phase = ACTING
            self._publish(
                session,
                state.emit(ProgressStage.THINKING, ProgressStatus.RUNNING, "正在理解你的问题…"),
            )

            try:
                reply = self._model.complete(messages, TOOL_SPECS)
            except Exception as error:
                log.warning("模型调用失败: %s", error)
                state.finish(TerminationReason.MODEL_ERROR)
                return self._stop(session, TerminationReason.MODEL_ERROR)

            if not reply.wants_tools:
                answer = self._judge_answer(session, reply)
                if answer is not None:
                    return answer
                messages.append(ChatMessage(ChatRole.USER, session.pending_reminder))
                continue

            messages.append(ChatMessage(ChatRole.ASSISTANT, reply.content, tool_calls=reply.tool_calls))
            for call in reply.tool_calls:
                text, attachments, empty = self._run_tool(session, call)
                session.delivered = session.delivered.merge(attachments)
                session.search_returned_nothing = session.search_returned_nothing or empty
                messages.append(ChatMessage(ChatRole.TOOL, text, tool_call_id=call.call_id))

    def _judge_answer(self, session: _Session, reply: ModelReply) -> str | None:
        """返回 ``None`` 表示这次不是最终回答（继续循环），返回字符串表示最终答案。

        被门禁拦下时返回 ``None`` 并把提醒写进对话——调用方负责 ``continue``。
        """
        state = session.state
        answer = reply.content.strip()
        if not answer:
            if state.block_guard_code("empty_answer"):
                self._publish(
                    session,
                    state.emit(
                        ProgressStage.RECOVERING,
                        ProgressStatus.RUNNING,
                        "模型这次没给出内容，再问一次…",
                        kind="guard",
                        payload={"code": "empty_answer"},
                    ),
                )
                session.pending_reminder = EMPTY_ANSWER_NUDGE
                return None
            # 提醒过仍然是空的。已经取到的证据不能被丢掉——用确定性的方式把事实说清楚。
            state.finish(TerminationReason.EMPTY_ANSWER)
            return self._stop(session, TerminationReason.EMPTY_ANSWER)

        verdict = self._guard.check(
            answer=answer,
            ledger=session.ledger,
            state=state,
            search_returned_nothing=session.search_returned_nothing,
        )
        if verdict.passed:
            state.finish(
                TerminationReason.ANSWERED_WITH_GAP
                if answered_with_gap(session.ledger)
                else TerminationReason.ANSWERED
            )
            return answer

        repeated = not state.block_guard_code(verdict.code)
        if repeated and verdict.code in HARD_GUARD_CODES:
            # 安全边界不做第二次让步：宁可给一句确定性的兜底，也不把内部标识交付出去。
            state.finish(TerminationReason.GUARD_BLOCKED)
            return self._stop(session, TerminationReason.GUARD_BLOCKED)
        if repeated:
            state.finish(TerminationReason.ANSWERED_WITH_GAP)
            return answer

        state.phase = FINALIZING
        self._publish(
            session,
            state.emit(
                ProgressStage.RECOVERING,
                ProgressStatus.RUNNING,
                verdict.message,
                kind="guard",
                payload={"code": verdict.code},
            ),
        )
        session.pending_reminder = verdict.message
        return None

    def _run_tool(self, session: _Session, call: ToolCall) -> tuple[str, tuple[Attachment, ...], bool]:
        """执行一次工具调用，返回 ``(发回模型的 JSON, 本次交付, 检索是否为空)``。"""
        state = session.state
        decision = self._policy.decide(call, state)

        if decision.action is ToolAction.DENY:
            state.record(
                ToolCallRecord(
                    call_id=call.call_id,
                    tool=call.name,
                    signature=decision.signature or call.name,
                    status="denied",
                    failure_reason=decision.code,
                )
            )
            self._tool_event(session, call, ProgressStatus.DENIED, decision.reason, "tool_error")
            return (
                json.dumps({"error": decision.code, "message": decision.reason}, ensure_ascii=False),
                (),
                False,
            )

        if decision.action is ToolAction.REPLAY:
            state.record(
                ToolCallRecord(
                    call_id=call.call_id,
                    tool=call.name,
                    signature=decision.signature,
                    status="replayed",
                    failure_reason="duplicate_call",
                )
            )
            self._tool_event(session, call, ProgressStatus.OK, decision.reason, "tool_result")
            return json.dumps(dict(decision.replay), ensure_ascii=False), (), False

        self._tool_event(session, call, ProgressStatus.RUNNING, f"正在执行 {call.name}…", "model_action")
        outcome = self._toolbox.execute(
            call.name, decision.arguments, scope_id=state.scope_id, now=datetime.now(UTC)
        )
        state.record(
            ToolCallRecord(
                call_id=call.call_id,
                tool=call.name,
                signature=decision.signature,
                status="ok" if outcome.ok else "error",
                failure_reason=outcome.failure_reason,
                observation=outcome.observation,
                model_visible=outcome.model_visible,
            )
        )

        if outcome.ok:
            for evidence_type in TOOLS_BY_NAME[call.name].satisfies:
                session.ledger.satisfy(evidence_type, call_id=call.call_id)
            self._tool_event(
                session, call, ProgressStatus.OK, _tool_status_text(call.name, outcome), "tool_result"
            )
        elif outcome.blocks is not None:
            # 工具明确说「这类证据本轮拿不到」——账本封口，门禁不再逼模型重试一个查不到
            # 的东西。这是「无证据时输出 gap 不编造」在工具层的落点。
            session.ledger.block(outcome.blocks, reason=outcome.failure_reason)
            self._tool_event(session, call, ProgressStatus.PARTIAL, outcome.failure_reason, "tool_error")
        else:
            self._tool_event(session, call, ProgressStatus.DENIED, outcome.failure_reason, "tool_error")

        return (
            json.dumps(dict(outcome.model_visible), ensure_ascii=False),
            outcome.attachments,
            is_empty_result(outcome),
        )

    # ---- 装配与辅助 ---------------------------------------------------------

    def _initial_messages(self, record: TurnRecord) -> list[ChatMessage]:
        """system 只在最前面拼一次，历史作为真正的消息。

        1.0 把历史、结果集上下文、当前选中照片、人物画像、硬值约束统统折进
        ``messages[0]``，理由是严格 chat template 要求 system 必须在开头
        （``_merge_system_constraint``）。原生消息角色下这条约束只剩一句「别在中途插
        system」——本模块从头到尾只构造一条 system。
        """
        messages = [ChatMessage(ChatRole.SYSTEM, SYSTEM_PROMPT)]
        for message in self._conversations.history_for_prompt(record.conversation_id, HISTORY_LIMIT):
            if message.turn_id == record.id:
                # 本回合的用户消息刚写进去，下面会单独加，别重复。
                continue
            messages.append(ChatMessage(message.role, message.content))
        messages.append(ChatMessage(ChatRole.USER, record.question))
        return messages

    def _stop(self, session: _Session, reason: TerminationReason) -> str:
        photo_count = max(
            (int(record.observation.get("total") or 0) for record in session.state.records),
            default=0,
        )
        return emergency.fallback_answer(
            reason=reason,
            ledger=session.ledger,
            photo_count=photo_count,
            delivered_count=len(session.delivered.items),
        )

    def _tool_event(
        self,
        session: _Session,
        call: ToolCall,
        status: ProgressStatus,
        text: str,
        kind: str,
    ) -> None:
        stage = ProgressStage.ACTION if status is ProgressStatus.RUNNING else ProgressStage.TOOL_RESULT
        self._publish(
            session,
            session.state.emit(
                stage,
                status,
                text,
                kind=kind,
                payload={"tool": call.name, "tool_call_id": call.call_id},
            ),
        )

    def _publish(self, session: _Session, event: ProgressEvent) -> None:
        self._turns.append_event(session.state.turn_id, event)
        if session.emit is not None:
            session.emit(event)


def _status_for(reason: TerminationReason) -> TurnStatus:
    if reason is TerminationReason.ANSWERED:
        return TurnStatus.COMPLETE
    if reason is TerminationReason.CANCELLED:
        return TurnStatus.CANCELLED
    if reason is TerminationReason.GUARD_BLOCKED:
        return TurnStatus.BLOCKED
    if reason in {TerminationReason.MODEL_ERROR, TerminationReason.TIMEOUT}:
        return TurnStatus.FAILED
    return TurnStatus.PARTIAL


def _tool_status_text(name: str, outcome: ToolOutcome) -> str:
    if name in {"search_memories", "get_result_page"}:
        total = int(outcome.observation.get("total") or 0)
        return f"结果集里共有 {total} 张候选照片。" if total else "这次检索没有匹配到照片。"
    if name == "get_original_photos":
        return f"已把 {outcome.observation.get('delivered', 0)} 张照片交给用户。"
    if name == "get_person_profile":
        return "已读取人物档案。" if outcome.observation.get("found") else "没有这位人物的档案。"
    return "工具执行完成。"
```

**收尾顺序有意义**：先把回答写成消息，**再**把回合标成终态。「终态」是「这一切都已经落库」的承诺——SSE 的 `complete` 帧就发在这个状态上，反过来的话，读到 `complete` 的调用方会看到一个还没有回答的会话。这个顺序是契约测试 `test_a_finished_turn_is_visible_in_the_conversation_detail` 抓出来的。

- [ ] **Step 4: 运行测试，确认绿灯**

Run: `.venv/bin/pytest apps/api/tests/integration/test_run_turn.py -p no:warnings`

Expected:

```text
21 passed
```

- [ ] **Step 5: 更新 module-map.md**

```markdown
| `sentrix/application/assistant/manage_conversations.py` | 会话增删改查与确定性标题（不调模型） | application | `sentrix/domain/assistant/models.py`、`sentrix/domain/assistant/ports.py` | — |
| `sentrix/application/assistant/run_turn.py` | 一次回合：规划 → 工具循环 → 证据账本 → 答案门禁 → 回答 | application | `sentrix/application/assistant/tools.py`、`sentrix/domain/assistant/__init__.py`、`sentrix/domain/assistant/emergency.py`、`sentrix/domain/assistant/evidence_ledger.py`、`sentrix/domain/assistant/final_guard.py`、`sentrix/domain/assistant/intent.py`、`sentrix/domain/assistant/models.py`、`sentrix/domain/assistant/ports.py`、`sentrix/domain/assistant/profile.py`、`sentrix/domain/assistant/task_state.py`、`sentrix/domain/assistant/tool_policy.py`、`sentrix/domain/assistant/tools.py` | — |
```

Run: `.venv/bin/python scripts/check_module_map.py`
Expected: `module-map: 0 个问题`

- [ ] **Step 6: 登记能力**

```json
{"capability": "助手工具循环", "origin_kind": "migrated", "source_commit": "2d2cee7c", "source_path": "backend/agent_runtime/runtime.py", "source_symbol": "AgentRuntime.run", "target_path": "apps/api/src/sentrix/application/assistant/run_turn.py", "verification": "apps/api/tests/integration/test_run_turn.py", "status": "verified"}
{"capability": "会话管理与确定性标题", "origin_kind": "migrated", "source_commit": "2d2cee7c", "source_path": "backend/agent_conversation.py", "source_symbol": "ConversationStore.auto_title", "target_path": "apps/api/src/sentrix/application/assistant/manage_conversations.py", "verification": "apps/api/tests/integration/test_run_turn.py", "status": "verified"}
```

Run: `.venv/bin/python scripts/check_capability_map.py`
Expected: `capability-map: 0 个问题`

- [ ] **Step 7: 全量门禁**

Run: `make ci`

---

## Task 42: 会话端点和建回合端点

**依赖：** Task 41（`RunTurn`）。

**Files:**
- Create: `apps/api/src/sentrix/api/schemas/assistant.py`
- Create: `apps/api/src/sentrix/api/routes/assistant.py`
- Create: `apps/api/tests/contract/test_assistant_routes.py`
- Modify: `apps/api/src/sentrix/main.py`（注册 assistant 路由）
- Modify: `apps/api/src/sentrix/config.py`（`chat_model_name` / `chat_timeout_s`）
- Modify: `apps/api/src/sentrix/bootstrap/container.py`（六个按端口声明的新字段）
- Modify: `packages/contracts/openapi/sentrix-v1.yaml`（重新导出）
- Modify: `docs/architecture/module-map.md`
- Modify: `docs/refactor/capability-map.jsonl`

**一个回合的生命周期在这里成形**：`POST .../turns` 立刻返回 `turn_id`，回合先以 `pending` 落库，执行放在工作线程里。这是「前端不轮询」能成立的前提——SSE 端点永远有东西可订阅，哪怕连接晚到几百毫秒。

`config.py` 新增两个配置项，其中 `chat_model_name` 需要说明一句：它不是可切换的「方案」。换模型要连着重建索引，那是一次迁移（见 `domain/retrieval/scheme.py`）。它之所以可配，是因为 llama-server 的 `--alias` 由部署方决定，与 2.0 的代码无关。

**Container 的六个新字段全部按 domain 端口声明**（`test_container_wiring.py` 强制）：`chat_model: ChatModel`、`conversation_repository: ConversationRepository`、`turn_repository: TurnRepository`、`result_set_repository: ResultSetRepository`、`person_directory: PersonDirectory`、`turn_event_broker: TurnEventBroker`。没有一个写成 infrastructure 的具体类，所以这次**不需要**往例外表里加任何一条。

- [ ] **Step 1: 先写失败测试**

`apps/api/tests/contract/test_assistant_routes.py`:

```python
"""``/api/v1/assistant/*`` 的契约测试。

模型换成脚本化的假件——契约测试要验的是 HTTP 映射与会话生命周期，不是模型。真模型
的验收在 tests/e2e（Task 45）。
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Sequence

import pytest
from fastapi.testclient import TestClient

from sentrix.domain.assistant.models import ChatMessage, ModelReply, ToolCall
from sentrix.domain.assistant.tools import TOOL_SPECS
from sentrix.main import create_app


class ScriptedChatModel:
    def __init__(self, replies: Sequence[ModelReply]) -> None:
        self._replies = list(replies)
        self.calls = 0

    def complete(self, messages: Sequence[ChatMessage], tools: Sequence[object]) -> ModelReply:
        assert [spec.name for spec in tools] == [spec.name for spec in TOOL_SPECS]
        self.calls += 1
        return self._replies[min(self.calls - 1, len(self._replies) - 1)]


def final(content: str) -> ModelReply:
    return ModelReply(content=content, finish_reason="stop")


def tool_call(name: str, call_id: str, **arguments: object) -> ModelReply:
    return ModelReply(
        content="",
        finish_reason="tool_calls",
        tool_calls=(ToolCall(call_id=call_id, name=name, arguments_json=json.dumps(arguments)),),
    )


@pytest.fixture
def client(settings) -> TestClient:
    """把模型换成脚本化的假件，其余装配（真 SQLite、真迁移）保持原样。

    默认剧本是「直接给一个不带交付的答案」——大多数契约用例只关心会话与回合的形状。
    """
    app = create_app(settings)
    app.state.container = dataclasses.replace(
        app.state.container, chat_model=ScriptedChatModel([final("这是一句回答。")])
    )
    return TestClient(app)


def make_conversation(client: TestClient, scope_id: str = "album1") -> dict:
    response = client.post("/api/v1/assistant/conversations", json={"scope_id": scope_id})
    assert response.status_code == 201, response.text
    return response.json()


def start_turn(client: TestClient, conversation_id: str, message: str = "给我看看在海边的照片") -> dict:
    response = client.post(
        f"/api/v1/assistant/conversations/{conversation_id}/turns",
        json={"message": message, "scope_id": "album1"},
    )
    assert response.status_code == 202, response.text
    return response.json()


# ---- 会话 -------------------------------------------------------------------


def test_creating_a_conversation_returns_its_shape(client: TestClient):
    body = make_conversation(client)

    assert body["scope_id"] == "album1"
    assert body["title"] == "新对话"
    assert body["id"].startswith("conv_")
    assert body["created_at"] == body["updated_at"]


def test_conversations_are_listed_per_scope(client: TestClient):
    created = make_conversation(client)
    make_conversation(client, scope_id="album2")

    listed = client.get("/api/v1/assistant/conversations", params={"scope_id": "album1"}).json()

    assert [item["id"] for item in listed["items"]] == [created["id"]]


def test_a_conversation_can_be_renamed(client: TestClient):
    created = make_conversation(client)

    response = client.patch(f"/api/v1/assistant/conversations/{created['id']}", json={"title": "海边那次"})

    assert response.status_code == 200
    assert response.json()["title"] == "海边那次"


def test_renaming_a_missing_conversation_is_404(client: TestClient):
    assert client.patch("/api/v1/assistant/conversations/conv_nope", json={"title": "x"}).status_code == 404


def test_a_blank_title_is_rejected(client: TestClient):
    created = make_conversation(client)

    response = client.patch(f"/api/v1/assistant/conversations/{created['id']}", json={"title": ""})

    assert response.status_code == 422


def test_a_conversation_can_be_deleted(client: TestClient):
    created = make_conversation(client)

    assert client.delete(f"/api/v1/assistant/conversations/{created['id']}").status_code == 204

    assert client.get(f"/api/v1/assistant/conversations/{created['id']}").status_code == 404
    assert client.delete(f"/api/v1/assistant/conversations/{created['id']}").status_code == 404


def test_the_detail_view_carries_messages_and_turns(client: TestClient):
    created = make_conversation(client)
    turn = start_turn(client, created["id"])

    body = client.get(f"/api/v1/assistant/conversations/{created['id']}").json()

    assert body["conversation"]["id"] == created["id"]
    assert [message["role"] for message in body["messages"]] == ["user"]
    assert [item["id"] for item in body["turns"]] == [turn["turn_id"]]


# ---- 建回合 -----------------------------------------------------------------


def test_starting_a_turn_returns_a_pending_turn_id(client: TestClient):
    created = make_conversation(client)

    body = start_turn(client, created["id"])

    assert body["turn_id"].startswith("turn_")
    assert body["conversation_id"] == created["id"]
    # 回合先落库再执行：SSE 永远有东西可订阅。
    assert body["status"] in {"pending", "running"}


def test_a_blank_message_is_rejected(client: TestClient):
    created = make_conversation(client)

    response = client.post(f"/api/v1/assistant/conversations/{created['id']}/turns", json={"message": ""})

    assert response.status_code == 422


def test_starting_a_turn_on_a_missing_conversation_is_404(client: TestClient):
    response = client.post("/api/v1/assistant/conversations/conv_nope/turns", json={"message": "在吗"})

    assert response.status_code == 404


def test_the_conversation_title_follows_the_first_question(client: TestClient):
    created = make_conversation(client)

    start_turn(client, created["id"], message="给我看看在海边的照片")

    detail = client.get(f"/api/v1/assistant/conversations/{created['id']}").json()
    assert detail["conversation"]["title"] == "给我看看在海边的照片"


def test_a_turn_can_be_started_without_creating_a_conversation_first(client: TestClient):
    """不带 ``conversation_id`` 时由调用方先建会话——本切片不做隐式建会话。

    反向验证：这里显式断言「没建会话 = 404」，免得将来有人加一条静默 fallback
    把一次调用方错误变成一次看起来正常的对话。
    """
    response = client.post(
        "/api/v1/assistant/conversations/conv_nope/turns",
        json={"message": "在吗", "scope_id": "album1"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "conversation not found"


def test_an_unknown_turn_has_no_event_stream(client: TestClient):
    assert client.get("/api/v1/assistant/turns/turn_nope/events").status_code == 404
```

- [ ] **Step 2: 运行测试，确认红灯**

Run: `.venv/bin/pytest apps/api/tests/contract/test_assistant_routes.py -q -p no:warnings`

Expected:

```text
E       assert 404 == 201
E        +  where 404 = <Response [404 Not Found]>.status_code
=========================== short test summary info ============================
FAILED apps/api/tests/contract/test_assistant_routes.py::test_creating_a_conversation_returns_its_shape
...
```

- [ ] **Step 3: 实现 schemas、路由与装配**

`apps/api/src/sentrix/api/schemas/assistant.py`:

```python
"""助手端点的请求/响应模型。字段与设计 §4.3 的 SSE 契约对齐。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ConversationSummary(BaseModel):
    id: str
    scope_id: str
    title: str
    created_at: str
    updated_at: str
    last_message_at: str


class ConversationListResponse(BaseModel):
    items: list[ConversationSummary]


class CreateConversationRequest(BaseModel):
    scope_id: str = "home-default"
    title: str = ""


class RenameConversationRequest(BaseModel):
    title: str = Field(min_length=1, max_length=80)


class MessageSummary(BaseModel):
    id: str
    role: str
    content: str
    turn_id: str
    created_at: str


class AttachmentSummary(BaseModel):
    handle: str
    asset_id: str
    file_name: str


class TurnSummary(BaseModel):
    id: str
    question: str
    status: str
    termination_reason: str
    answer: str
    attachments: list[AttachmentSummary]
    created_at: str


class ConversationDetailResponse(BaseModel):
    conversation: ConversationSummary
    messages: list[MessageSummary]
    turns: list[TurnSummary]


class StartTurnRequest(BaseModel):
    # 一条空消息不是「一轮空对话」，是一次调用方错误，所以这里直接挡在 422。
    message: str = Field(min_length=1, max_length=2000)
    scope_id: str = "home-default"
    conversation_id: str = ""


class StartTurnResponse(BaseModel):
    turn_id: str
    conversation_id: str
    status: str


class ProgressEventPayload(BaseModel):
    """``progress`` 事件的 ``data``。

    字段集沿用 1.0（``runtime.py:1664-1687``），并补上 ``kind`` 与 ``payload``。
    ``step_index`` 同时是 SSE 的 ``id:`` 值，于是 ``Last-Event-ID`` 断线续传天然可用。
    """

    text: str
    status: str
    stage: str
    step_index: int
    timestamp: str
    kind: str = "status"
    payload: dict[str, Any] = Field(default_factory=dict)


class CompleteEventPayload(BaseModel):
    """``complete`` 事件的 ``data``。

    与 1.0 的一处刻意差异：**不再把整条 ``public_progress`` 又塞一遍**。1.0 的 ``complete``
    帧把已经逐条发过的进度事件完整重发（``app.py:2989``），是纯粹的带宽浪费。
    """

    type: str = "complete"
    turn_id: str
    status: str
    termination_reason: str
    answer: str
    attachments: list[AttachmentSummary]
    requirements: list[dict[str, Any]]
    error: str = ""
```

`apps/api/src/sentrix/api/routes/assistant.py`（本任务只到「建回合」为止；SSE 端点在 Task 43 加进来）：

```python
"""助手端点。只做 HTTP 映射，回合的一切规则在 domain / application 层。

会话的增删改查，以及「建一个回合并立刻返回 turn_id」——执行放在工作线程里，
回合先落库（``pending``）再由事件流订阅。
"""

from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, Depends, HTTPException, Response

from sentrix.api.deps import get_container
from sentrix.api.schemas.assistant import (
    AttachmentSummary,
    ConversationDetailResponse,
    ConversationListResponse,
    ConversationSummary,
    CreateConversationRequest,
    MessageSummary,
    RenameConversationRequest,
    StartTurnRequest,
    StartTurnResponse,
    TurnSummary,
)
from sentrix.application.assistant.manage_conversations import ManageConversations
from sentrix.application.assistant.run_turn import (
    RunTurn,
    TurnNotFoundError,
    TurnRequest,
)
from sentrix.application.assistant.tools import AssistantToolbox
from sentrix.application.retrieval.retrieve_visual import RetrieveVisual
from sentrix.bootstrap.container import Container
from sentrix.domain.assistant.models import Conversation, TurnRecord

log = logging.getLogger("sentrix.api.assistant")

router = APIRouter(tags=["assistant"])


def _conversation_summary(conversation: Conversation) -> ConversationSummary:
    return ConversationSummary(
        id=conversation.id,
        scope_id=conversation.scope_id,
        title=conversation.title,
        created_at=conversation.created_at.isoformat(),
        updated_at=conversation.updated_at.isoformat(),
        last_message_at=conversation.last_message_at.isoformat(),
    )


def _attachments(record: TurnRecord) -> list[AttachmentSummary]:
    return [
        AttachmentSummary(handle=item.handle, asset_id=item.asset_id, file_name=item.file_name)
        for item in record.attachments
    ]


def _turn_summary(record: TurnRecord) -> TurnSummary:
    return TurnSummary(
        id=record.id,
        question=record.question,
        status=record.status.value,
        termination_reason=record.termination_reason,
        answer=record.answer,
        attachments=_attachments(record),
        created_at=record.created_at.isoformat(),
    )


def _run_turn(container: Container) -> RunTurn:
    return RunTurn(
        model=container.chat_model,
        conversations=container.conversation_repository,
        turns=container.turn_repository,
        toolbox=AssistantToolbox(
            retrieval=RetrieveVisual(
                embedder=container.visual_embedder,
                vectors=container.vector_repository,
                index=container.vector_index,
            ),
            assets=container.asset_repository,
            result_sets=container.result_set_repository,
            people=container.person_directory,
        ),
    )


@router.post("/assistant/conversations", status_code=201)
def create_conversation(
    payload: CreateConversationRequest, container: Container = Depends(get_container)
) -> ConversationSummary:
    use_case = ManageConversations(
        conversations=container.conversation_repository, turns=container.turn_repository
    )
    return _conversation_summary(use_case.create(scope_id=payload.scope_id, title=payload.title))


@router.get("/assistant/conversations")
def list_conversations(
    scope_id: str = "home-default", container: Container = Depends(get_container)
) -> ConversationListResponse:
    use_case = ManageConversations(
        conversations=container.conversation_repository, turns=container.turn_repository
    )
    return ConversationListResponse(
        items=[_conversation_summary(item) for item in use_case.list_for_scope(scope_id)]
    )


@router.get("/assistant/conversations/{conversation_id}")
def get_conversation(
    conversation_id: str, container: Container = Depends(get_container)
) -> ConversationDetailResponse:
    use_case = ManageConversations(
        conversations=container.conversation_repository, turns=container.turn_repository
    )
    detail = use_case.detail(conversation_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return ConversationDetailResponse(
        conversation=_conversation_summary(detail.conversation),
        messages=[
            MessageSummary(
                id=message.id,
                role=message.role.value,
                content=message.content,
                turn_id=message.turn_id,
                created_at=message.created_at.isoformat(),
            )
            for message in detail.messages
        ],
        turns=[_turn_summary(turn) for turn in detail.turns],
    )


@router.patch("/assistant/conversations/{conversation_id}")
def rename_conversation(
    conversation_id: str,
    payload: RenameConversationRequest,
    container: Container = Depends(get_container),
) -> ConversationSummary:
    use_case = ManageConversations(
        conversations=container.conversation_repository, turns=container.turn_repository
    )
    if not use_case.rename(conversation_id, payload.title):
        raise HTTPException(status_code=404, detail="conversation not found")
    detail = use_case.detail(conversation_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return _conversation_summary(detail.conversation)


@router.delete("/assistant/conversations/{conversation_id}", status_code=204)
def delete_conversation(conversation_id: str, container: Container = Depends(get_container)) -> Response:
    use_case = ManageConversations(
        conversations=container.conversation_repository, turns=container.turn_repository
    )
    if not use_case.delete(conversation_id):
        raise HTTPException(status_code=404, detail="conversation not found")
    return Response(status_code=204)


@router.post("/assistant/conversations/{conversation_id}/turns", status_code=202)
def start_turn(
    conversation_id: str,
    payload: StartTurnRequest,
    container: Container = Depends(get_container),
) -> StartTurnResponse:
    """建一个回合并立刻返回 ``turn_id``，执行放在工作线程里。

    回合先落库（``pending``）、再由 SSE 订阅，是「前端不轮询」能成立的前提：SSE 端点
    永远有东西可订阅，哪怕连接晚到几百毫秒。
    """
    manage = ManageConversations(
        conversations=container.conversation_repository, turns=container.turn_repository
    )
    run_turn = _run_turn(container)
    try:
        record = run_turn.start(
            TurnRequest(conversation_id=conversation_id, scope_id=payload.scope_id, question=payload.message)
        )
    except TurnNotFoundError as error:
        # application 抛领域错误，映射成什么状态码是这里的事。
        raise HTTPException(status_code=404, detail="conversation not found") from error
    manage.ensure_title(conversation_id, payload.message)

    broker = container.turn_event_broker
    # 先登记再起飞：SSE 可能比工作线程的第一条事件更早连上来。
    broker.open(record.id)

    def worker() -> None:
        try:
            run_turn.run(record.id, emit=lambda event: broker.publish(record.id, event))
        except Exception:
            log.exception("回合执行失败: %s", record.id)
        finally:
            broker.finish(record.id)

    threading.Thread(target=worker, name=f"turn-{record.id}", daemon=True).start()
    return StartTurnResponse(turn_id=record.id, conversation_id=conversation_id, status=record.status.value)
```

`apps/api/src/sentrix/config.py`:

```python
"""Typed configuration.

Only ``SENTRIX_DATA_DIR`` and a handful of connection settings are configurable.
Everything else is derived, because 1.0's failure mode was ~126 env flags whose
values disagreed with each other and with the code.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SENTRIX_", extra="forbid")

    data_dir: Path = Field(default=Path("data"))

    api_port: int = 9598
    api_host: str = "0.0.0.0"

    # 2.0 reuses 1.0's already-running model services; it never manages them.
    vlm_base_url: str = "http://127.0.0.1:8100/v1"
    text_embedder_url: str = "http://127.0.0.1:8101"

    # 8100 上 llama-server 的模型别名。它不是可切换的「方案」——换模型要连着重建索引，
    # 那是一次迁移（见 domain/retrieval/scheme.py），不是一个环境变量。这里之所以可配，
    # 是因为 llama-server 的 --alias 由部署方决定，与 2.0 的代码无关。
    chat_model_name: str = "gemma4-qat-iq3xxs"
    chat_timeout_s: float = 120.0

    def model_post_init(self, _context: Any, /) -> None:  # noqa: D105
        self.data_dir = self.data_dir.expanduser().resolve()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_path(self) -> Path:
        return self.data_dir / "sentrix.db"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def preview_dir(self) -> Path:
        return self.data_dir / "previews"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def qdrant_dir(self) -> Path:
        """派生向量索引的目录。

        **必须由 data_dir 派生**，不允许单独配路径。1.0 的 ``data/qdrant`` 被 8091
        进程以 flock 独占（嵌入式 Qdrant 的目录只能一个进程用），2.0 若指过去，
        第二个实例起不来——而且是那种「起了、只是检索悄悄降级成全表扫」的坏法。
        派生自 data_dir 意味着换数据目录就整体换，不存在「只把索引指到 1.0」的口子。
        """
        return self.data_dir / "qdrant"

    def ensure_directories(self) -> None:
        for path in (self.data_dir, self.media_dir, self.preview_dir):
            path.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    return Settings()
```

`apps/api/src/sentrix/bootstrap/container.py`（新增的六个字段全部按 domain 端口声明）：

```python
"""The single wiring point.

Nothing else in the codebase constructs an adapter.  Domain and application code
receive ports; only this module knows which concrete class implements them.

字段一律按 **domain 端口** 声明。写在类型上的端口才是可替换性的凭证：只写在
``build()`` 里的替换能力，调用方看不见，于是很容易顺手用上协议之外的方法。
这条不变量由 ``apps/api/tests/unit/test_container_wiring.py`` 钉住。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from sentrix.config import Settings
from sentrix.domain.assistant.ports import (
    ChatModel,
    ConversationRepository,
    PersonDirectory,
    ResultSetRepository,
    TurnEventBroker,
    TurnRepository,
)
from sentrix.domain.media.ports import (
    AssetRepository,
    MediaStorage,
    MetadataReader,
    ObservationRepository,
    PreviewEncoder,
)
from sentrix.domain.retrieval.ports import VectorIndex, VectorRepository, VisualEmbedder
from sentrix.infrastructure.assistant.chat_client import OpenAIChatModel
from sentrix.infrastructure.assistant.events import InProcessTurnEventBroker
from sentrix.infrastructure.assistant.identity import NoIdentityDirectory
from sentrix.infrastructure.media.image_io import PillowPreviewEncoder
from sentrix.infrastructure.media.metadata import PillowMetadataReader
from sentrix.infrastructure.media.storage import FileSystemMediaStorage
from sentrix.infrastructure.models.chinese_clip import ChineseClipVisualEmbedder
from sentrix.infrastructure.persistence.database import Database
from sentrix.infrastructure.persistence.repositories.assets import (
    SqliteAssetRepository,
    SqliteObservationRepository,
)
from sentrix.infrastructure.persistence.repositories.conversations import (
    SqliteConversationRepository,
)
from sentrix.infrastructure.persistence.repositories.result_sets import SqliteResultSetRepository
from sentrix.infrastructure.persistence.repositories.spaces import SqliteSpaceRepository
from sentrix.infrastructure.persistence.repositories.turns import SqliteTurnRepository
from sentrix.infrastructure.persistence.repositories.vectors import SqliteVectorRepository
from sentrix.infrastructure.vector.qdrant_index import QdrantVectorIndex

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "infrastructure" / "persistence" / "migrations"


@dataclass(frozen=True)
class Container:
    settings: Settings

    # 例外：共享连接工厂，不是任何一个端口的实现（多个仓储共用同一个连接来源），
    # 且 database_healthy() 直接需要它。domain 里没有「数据库池」这个概念可依。
    database: Database

    # 例外：目前没有任何 application / api 消费者，为它补端口是投机抽象。
    # 记忆空间的用例落地时，把 MemorySpace 一并迁进 domain 再补 SpaceRepository。
    spaces: SqliteSpaceRepository

    asset_repository: AssetRepository
    observation_repository: ObservationRepository
    media_storage: MediaStorage
    metadata_reader: MetadataReader
    preview_encoder: PreviewEncoder
    vector_repository: VectorRepository
    vector_index: VectorIndex
    visual_embedder: VisualEmbedder

    chat_model: ChatModel
    conversation_repository: ConversationRepository
    turn_repository: TurnRepository
    result_set_repository: ResultSetRepository
    person_directory: PersonDirectory
    # 内存里的实时事件流。它没有持久化的一面——历史事件在 agent_turn_events 表里，
    # 两半合起来才构成完整的 SSE 语义（见 domain/assistant/ports.py 的 TurnEventBroker）。
    turn_event_broker: TurnEventBroker

    @classmethod
    def build(cls, settings: Settings) -> Container:
        database = Database(settings.database_path, MIGRATIONS_DIR)
        return cls(
            settings=settings,
            database=database,
            spaces=SqliteSpaceRepository(database),
            asset_repository=SqliteAssetRepository(database),
            observation_repository=SqliteObservationRepository(database),
            media_storage=FileSystemMediaStorage(settings.media_dir),
            metadata_reader=PillowMetadataReader(),
            preview_encoder=PillowPreviewEncoder(),
            vector_repository=SqliteVectorRepository(database),
            vector_index=QdrantVectorIndex(settings.qdrant_dir),
            visual_embedder=ChineseClipVisualEmbedder(),
            chat_model=OpenAIChatModel(
                settings.vlm_base_url,
                settings.chat_model_name,
                timeout_s=settings.chat_timeout_s,
            ),
            conversation_repository=SqliteConversationRepository(database),
            turn_repository=SqliteTurnRepository(database),
            result_set_repository=SqliteResultSetRepository(database),
            person_directory=NoIdentityDirectory(),
            turn_event_broker=InProcessTurnEventBroker(),
        )

    def database_healthy(self) -> bool:
        try:
            with self.database.connect() as connection:
                connection.execute("SELECT 1 FROM schema_migrations LIMIT 1").fetchone()
        except sqlite3.Error:
            return False
        return True
```

`apps/api/src/sentrix/main.py`（加两行：import 路由与注册）：

```python
"""FastAPI assembly. Route modules contain transport mapping only."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI

from sentrix import __version__
from sentrix.api.errors import DomainError, domain_error_handler
from sentrix.api.routes import assistant, health, media, search
from sentrix.bootstrap.container import Container
from sentrix.config import Settings, load_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    settings.ensure_directories()

    container = Container.build(settings)
    container.database.migrate()

    app = FastAPI(title="Sentrix Home API", version=__version__, docs_url=None, redoc_url=None)
    app.state.container = container
    app.add_exception_handler(DomainError, domain_error_handler)

    v1 = APIRouter(prefix="/api/v1")
    v1.include_router(health.router)
    v1.include_router(media.router)
    v1.include_router(search.router)
    v1.include_router(assistant.router)
    app.include_router(v1)
    return app


# 这里刻意没有模块级 ``app = create_app()``：它会让任何 ``import sentrix.main``
# （pytest 收集、脚本、类型检查）都带上副作用——在进程 CWD 下建出 data/、跑一次迁移。
# 进程入口走工厂：``uvicorn --factory sentrix.main:create_app``（见 Makefile 的 dev）。
# 理由与 Task 5 的定稿一致；本块曾残留该行（1076a7f 只改了 Task 5，漏改这里）。
```

- [ ] **Step 4: 运行测试，确认绿灯**

Run: `.venv/bin/pytest apps/api/tests/contract/test_assistant_routes.py -p no:warnings`

Expected:

```text
13 passed
```

- [ ] **Step 5: 重新导出 OpenAPI**

Run: `.venv/bin/python scripts/export_openapi.py`

- [ ] **Step 6: 更新 module-map.md**

```markdown
| `sentrix/api/routes/assistant.py` | 助手端点：会话 CRUD / 建回合 / 回合事件的 SSE 流 | api | `sentrix/api/deps.py`、`sentrix/api/schemas/assistant.py`、`sentrix/application/assistant/manage_conversations.py`、`sentrix/application/assistant/run_turn.py`、`sentrix/application/assistant/tools.py`、`sentrix/application/retrieval/retrieve_visual.py`、`sentrix/bootstrap/container.py`、`sentrix/domain/assistant/models.py` | `sentrix/main.py` |
| `sentrix/api/schemas/assistant.py` | 助手端点的请求响应模型与 SSE 事件载荷 | api | — | `sentrix/api/routes/assistant.py` |
| `sentrix/bootstrap/container.py` | 唯一依赖装配点；只有这里构造适配器 | bootstrap | `sentrix/config.py`、`sentrix/domain/assistant/ports.py`、`sentrix/domain/media/ports.py`、`sentrix/domain/retrieval/ports.py`、`sentrix/infrastructure/assistant/chat_client.py`、`sentrix/infrastructure/assistant/events.py`、`sentrix/infrastructure/assistant/identity.py`、`sentrix/infrastructure/media/image_io.py`、`sentrix/infrastructure/media/metadata.py`、`sentrix/infrastructure/media/storage.py`、`sentrix/infrastructure/models/chinese_clip.py`、`sentrix/infrastructure/persistence/database.py`、`sentrix/infrastructure/persistence/repositories/assets.py`、`sentrix/infrastructure/persistence/repositories/conversations.py`、`sentrix/infrastructure/persistence/repositories/result_sets.py`、`sentrix/infrastructure/persistence/repositories/spaces.py`、`sentrix/infrastructure/persistence/repositories/turns.py`、`sentrix/infrastructure/persistence/repositories/vectors.py`、`sentrix/infrastructure/vector/qdrant_index.py` | `sentrix/api/deps.py`、`sentrix/api/routes/assistant.py`、`sentrix/api/routes/media.py`、`sentrix/api/routes/search.py`、`sentrix/main.py` |
| `sentrix/config.py` | 类型化配置；`data_dir` 是唯一可配根，其余路径派生 | — | — | `sentrix/bootstrap/container.py`、`sentrix/main.py` |
| `sentrix/main.py` | FastAPI 装配与 v1 路由注册；进程入口 | — | `sentrix/__init__.py`、`sentrix/api/errors.py`、`sentrix/api/routes/__init__.py`、`sentrix/api/routes/assistant.py`、`sentrix/api/routes/health.py`、`sentrix/api/routes/media.py`、`sentrix/api/routes/search.py`、`sentrix/bootstrap/container.py`、`sentrix/config.py` | — |
```

Run: `.venv/bin/python scripts/check_module_map.py`
Expected: `module-map: 0 个问题`

- [ ] **Step 7: 登记能力**

```json
{"capability": "会话端点（增删改查）", "origin_kind": "migrated", "source_commit": "2d2cee7c", "source_path": "backend/app.py", "source_symbol": "create_conversation", "target_path": "apps/api/src/sentrix/api/routes/assistant.py", "verification": "apps/api/tests/contract/test_assistant_routes.py", "status": "verified"}
```

Run: `.venv/bin/python scripts/check_capability_map.py`
Expected: `capability-map: 0 个问题`

- [ ] **Step 8: 全量门禁**

Run: `make ci`

---

## Task 43: 回合事件的 SSE 流

**依赖：** Task 42。

**Files:**
- Modify: `apps/api/src/sentrix/api/routes/assistant.py`（+`GET /assistant/turns/{id}/events`）
- Create: `apps/api/tests/contract/test_assistant_events.py`
- Modify: `packages/contracts/openapi/sentrix-v1.yaml`（重新导出）
- Modify: `docs/refactor/capability-map.jsonl`

（**`module-map.md` 这一步不动**：`api/routes/assistant.py` 的依赖两列与 Task 42 完全相同——新增的 `StreamingResponse` / `Request` 是第三方，不进那两列。文件集与 commit 的双向对拍会抓这种「声明了却没改」的空转，所以宁可不写。）

事件形状沿用 1.0 已验证的两类（`progress` / `complete`，`app.py:2986` 与 `:2997`），改的是四处：

1. **事件带 `id:`，断线可从 `Last-Event-ID` 续。** 1.0 的 `sent` 是闭包局部变量，每次重连归零并把全部历史重发一遍（`app.py:2982`）。2.0 的 `step_index` 在回合内单调递增，正好当事件 id 用——历史事件已经落库，续传就是「读 `step_index > N` 的那些行」。
2. **`complete` 帧不再重发整条进度数组。** 1.0 把它又完整塞了一遍（`app.py:2989`）。
3. **`status` 是真实的终态。** 1.0 把模型失败、超时、guard 拦截全部压成 `status="complete"`（`app.py:3433`），只有未捕获的 Python 异常才是 `error`——前端拿到的每一个失败都是一个「成功」的帧。2.0 的 `status` 直接来自 `TurnStatus`。
4. **轮询端点在 2.0 不存在。** `GET /api/assistant/turns/{id}` 是 404，契约测试显式断言它——留着它，前端迟早会回去轮询。

订阅顺序是**先读库、再订阅内存流**：库里是所有已经落盘的事件（断线重连与迟到加入都能补），内存流是此刻还没落盘的增量，两边按 `step_index` 去重。

- [ ] **Step 1: 先写失败测试**

`apps/api/tests/contract/test_assistant_events.py`:

```python
"""``GET /api/v1/assistant/turns/{id}/events`` 的 SSE 契约。

事件形状沿用 1.0 已验证的两类（``app.py:2986`` / ``:2997``）：``progress`` 与 ``complete``。
本文件钉住的是 2.0 相对 1.0 的四处修正：

1. 前端不需要轮询就能拿到全过程（1.0 的 ``GET /api/assistant/turn/{id}`` 被调 668,490 次）；
2. 事件带 ``id:``，断线可以从 ``Last-Event-ID`` 续（1.0 每次重连都从头重发）；
3. ``complete`` 帧不再把整条进度数组重复一遍（1.0 ``app.py:2989``）；
4. ``status`` 是真实的终态（1.0 把模型失败、超时、guard 拦截全部压成 ``complete``）。
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Sequence

import pytest
from fastapi.testclient import TestClient

from sentrix.domain.assistant.models import ChatMessage, ModelReply, ToolCall
from sentrix.main import create_app


class ScriptedChatModel:
    def __init__(self, replies: Sequence[ModelReply]) -> None:
        self._replies = list(replies)
        self.calls = 0

    def complete(self, messages: Sequence[ChatMessage], tools: Sequence[object]) -> ModelReply:
        self.calls += 1
        return self._replies[min(self.calls - 1, len(self._replies) - 1)]


def final(content: str) -> ModelReply:
    return ModelReply(content=content, finish_reason="stop")


def tool_call(name: str, call_id: str, **arguments: object) -> ModelReply:
    return ModelReply(
        content="",
        finish_reason="tool_calls",
        tool_calls=(ToolCall(call_id=call_id, name=name, arguments_json=json.dumps(arguments)),),
    )


class FailingModel:
    def complete(self, messages: Sequence[ChatMessage], tools: Sequence[object]) -> ModelReply:
        raise RuntimeError("端点挂了")


def build_client(settings, model) -> TestClient:
    app = create_app(settings)
    app.state.container = dataclasses.replace(app.state.container, chat_model=model)
    return TestClient(app)


def read_events(client: TestClient, turn_id: str, **headers: str) -> list[dict]:
    """把 SSE 流读完，返回 ``[{event, data}]``。"""
    lines = []
    with client.stream(
        "GET", f"/api/v1/assistant/turns/{turn_id}/events", headers=headers or None
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        lines = [line for line in response.iter_lines() if line]

    events: list[dict] = []
    current: dict = {}
    for line in lines:
        if line.startswith("id: "):
            current["id"] = int(line[4:])
        elif line.startswith("event: "):
            current["event"] = line[7:]
        elif line.startswith("data: "):
            current["data"] = json.loads(line[6:])
            events.append(current)
            current = {}
    return events


def run_turn(client: TestClient, message: str = "给我看看在海边的照片") -> tuple[str, str]:
    conversation = client.post("/api/v1/assistant/conversations", json={"scope_id": "album1"}).json()
    turn = client.post(
        f"/api/v1/assistant/conversations/{conversation['id']}/turns",
        json={"message": message, "scope_id": "album1"},
    ).json()
    return conversation["id"], turn["turn_id"]


@pytest.fixture
def client(settings) -> TestClient:
    return build_client(settings, ScriptedChatModel([final("这是一句回答。")]))


def test_the_stream_ends_by_itself_after_complete(client: TestClient):
    """一次请求拿完全过程——这就是「前端不轮询」的全部含义。

    反向断言：1.0 那个被调了 66 万次的轮询端点 ``GET /api/assistant/turn/{id}``
    在 2.0 里**不存在**。留着它，前端迟早会回去轮询。
    """
    _conversation_id, turn_id = run_turn(client)

    events = read_events(client, turn_id)

    assert events[-1]["event"] == "complete"
    assert client.get(f"/api/v1/assistant/turns/{turn_id}").status_code == 404


def test_progress_events_carry_the_designed_field_set(client: TestClient):
    _conversation_id, turn_id = run_turn(client)

    progress = [item for item in read_events(client, turn_id) if item["event"] == "progress"]

    assert progress, "至少要有一条进度事件"
    for item in progress:
        data = item["data"]
        assert set(data) == {
            "text",
            "status",
            "stage",
            "step_index",
            "timestamp",
            "kind",
            "payload",
        }
        assert data["stage"] in {
            "thinking",
            "planning",
            "inspecting",
            "action",
            "tool_result",
            "recovering",
            "finalizing",
            "terminal",
        }
        assert data["status"] in {"running", "ok", "denied", "partial", "complete", "cancelled"}


def test_the_first_progress_event_is_the_plan(client: TestClient):
    """规划是 S3 的第一步，也是「为什么这一轮要取证」的书面记录。"""
    _conversation_id, turn_id = run_turn(client)

    first = read_events(client, turn_id)[0]

    assert first["data"]["kind"] == "plan"
    assert first["data"]["stage"] == "planning"
    assert [item["evidence_type"] for item in first["data"]["payload"]["requirements"]] == [
        "photo_set",
        "delivered_photo",
    ]


def test_progress_ids_are_monotonic_and_usable_for_resume(client: TestClient):
    _conversation_id, turn_id = run_turn(client)

    progress = [item for item in read_events(client, turn_id) if item["event"] == "progress"]

    ids = [item["id"] for item in progress]
    assert ids == sorted(ids)
    assert ids == list(range(1, len(ids) + 1))


def test_the_terminal_event_is_always_last_before_complete(client: TestClient):
    _conversation_id, turn_id = run_turn(client)

    events = read_events(client, turn_id)

    assert events[-2]["data"]["kind"] == "terminal"
    assert events[-2]["data"]["stage"] == "terminal"
    assert events[-1]["event"] == "complete"


def test_the_complete_frame_does_not_repeat_the_progress_array(client: TestClient):
    """1.0 的 ``complete`` 帧把已经逐条发过的 public_progress 又完整塞了一遍。"""
    _conversation_id, turn_id = run_turn(client)

    complete = read_events(client, turn_id)[-1]["data"]

    assert complete["type"] == "complete"
    assert set(complete) == {
        "type",
        "turn_id",
        "status",
        "termination_reason",
        "answer",
        "attachments",
        "requirements",
        "error",
    }
    assert complete["turn_id"] == turn_id
    assert complete["answer"] == "这是一句回答。"
    assert complete["termination_reason"] == "answered_with_gap"
    # 用户要图、模型只回了一段文字——终态必须如实反映这件事，不能报 complete。
    assert complete["status"] == "partial"
    assert [item["status"] for item in complete["requirements"]] == ["open", "open"]


def test_a_model_failure_is_reported_as_failed_not_complete(settings):
    """1.0 把模型失败压成 ``status="complete"``，只有未捕获的 Python 异常才是 error。"""
    client = build_client(settings, FailingModel())
    _conversation_id, turn_id = run_turn(client)

    complete = read_events(client, turn_id)[-1]["data"]

    assert complete["status"] == "failed"
    assert complete["termination_reason"] == "model_error"
    assert "模型不可用" in complete["answer"]


def test_a_late_subscriber_still_gets_the_whole_turn(client: TestClient):
    """回合跑完之后才连上来，照样拿得到全过程。

    1.0 的进度只存在进程内存里，页面一刷新就再也拿不回来——那正是轮询成为主路径的
    原因之一。2.0 的事件先落库，历史与实时各走一条路。
    """
    _conversation_id, turn_id = run_turn(client)
    read_events(client, turn_id)  # 第一次连接把回合跑完

    events = read_events(client, turn_id)

    assert [item["event"] for item in events][-1] == "complete"
    assert events[-1]["data"]["answer"] == "这是一句回答。"
    assert len([item for item in events if item["event"] == "progress"]) == len(
        [item for item in read_events(client, turn_id) if item["event"] == "progress"]
    )


def test_last_event_id_resumes_without_replaying(client: TestClient):
    """断线重连从 ``Last-Event-ID`` 之后继续，不重发已经收到的事件。"""
    _conversation_id, turn_id = run_turn(client)
    first = read_events(client, turn_id)
    resume_from = [item["id"] for item in first if item["event"] == "progress"][1]

    resumed = read_events(client, turn_id, **{"Last-Event-ID": str(resume_from)})

    ids = [item["id"] for item in resumed if item["event"] == "progress"]
    assert ids and min(ids) > resume_from
    assert resumed[-1]["event"] == "complete"


def test_a_garbage_last_event_id_falls_back_to_a_full_replay(client: TestClient):
    _conversation_id, turn_id = run_turn(client)

    events = read_events(client, turn_id, **{"Last-Event-ID": "not-a-number"})

    assert [item["id"] for item in events if item["event"] == "progress"][0] == 1


def test_two_turns_in_one_conversation_each_get_their_own_stream(settings):
    client = build_client(
        settings,
        ScriptedChatModel([tool_call("get_result_page", "c1", result_set_id="rs_1", page=1)]),
    )
    conversation_id, first_turn = run_turn(client)

    second_turn = client.post(
        f"/api/v1/assistant/conversations/{conversation_id}/turns",
        json={"message": "再看看别的", "scope_id": "album1"},
    ).json()["turn_id"]

    assert first_turn != second_turn
    first = read_events(client, first_turn)
    second = read_events(client, second_turn)
    assert first[-1]["data"]["turn_id"] == first_turn
    assert second[-1]["data"]["turn_id"] == second_turn
    # 每个回合的 step_index 各自从 1 开始——前端拿它当 key 时必须限定回合作用域。
    assert [item["id"] for item in first if item["event"] == "progress"][0] == 1
    assert [item["id"] for item in second if item["event"] == "progress"][0] == 1


def test_a_tool_call_turn_emits_action_and_result_events(settings):
    client = build_client(
        settings,
        ScriptedChatModel(
            [
                tool_call("search_memories", "c1", query="海边"),
                final("这是你要的照片。"),
            ]
        ),
    )
    _conversation_id, turn_id = run_turn(client)

    progress = [item["data"] for item in read_events(client, turn_id) if item["event"] == "progress"]

    kinds = [item["kind"] for item in progress]
    assert "model_action" in kinds
    assert "tool_result" in kinds
    action = next(item for item in progress if item["kind"] == "model_action")
    assert action["payload"]["tool"] == "search_memories"
    assert action["payload"]["tool_call_id"] == "c1"


def test_a_finished_turn_is_visible_in_the_conversation_detail(client: TestClient):
    """回合收尾之后，会话里该有的东西都在。

    这条用例**必须**放在 SSE 这一侧：它靠「把事件流读完」来等待回合结束，而这正是
    「一次请求拿完全过程」的用法本身。放在 Task 42 的会话契约里会变成依赖一个还不存在
    的端点——逐任务验证时它就是这么暴露出来的。
    """
    conversation_id, turn_id = run_turn(client)
    # 读完 SSE 流就意味着回合已经收尾。
    read_events(client, turn_id)

    detail = client.get(f"/api/v1/assistant/conversations/{conversation_id}").json()

    assert [message["role"] for message in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][1]["content"] == "这是一句回答。"
    assert detail["turns"][0]["status"] == "partial"
    assert detail["turns"][0]["termination_reason"] == "answered_with_gap"
```

- [ ] **Step 2: 运行测试，确认红灯**

Run: `.venv/bin/pytest apps/api/tests/contract/test_assistant_events.py -q -p no:warnings`

Expected:

```text
E       KeyError: 'content-type'
E       assert 404 == 200
=========================== short test summary info ============================
FAILED apps/api/tests/contract/test_assistant_events.py::test_the_stream_ends_by_itself_after_complete
...
```

- [ ] **Step 3: 实现 SSE 端点**

`apps/api/src/sentrix/api/routes/assistant.py`（完整版；相对 Task 42 增加了 `_format_progress` / `_format_complete` / `_resume_index` / `turn_events`，以及模块 docstring 里讲 SSE 契约的那一段）：

```python
"""助手端点。只做 HTTP 映射，回合的一切规则在 domain / application 层。

SSE 的形状与设计 §4.3 一致，沿用 1.0 已验证的两类事件：``progress`` 与 ``complete``。
与 1.0 的三处刻意差异：

1. **事件带 ``id:``，且断线可从 ``Last-Event-ID`` 续。** 1.0 的 ``sent`` 是闭包局部变量，
   每次重连都归零并把全部历史重发一遍（``app.py:2982``）。2.0 的 ``step_index`` 在回合内
   单调递增，正好当事件 id 用。
2. **``complete`` 帧不再重发整条进度数组。** 1.0 把它又塞了一遍（``app.py:2989``）。
3. **回合终态是真实的。** 1.0 把模型失败、超时、guard 拦截全部压成 ``status="complete"``
   （``app.py:3433``），只有未捕获的 Python 异常才是 ``error``，前端拿到的每一个失败都是
   一个「成功」的帧。2.0 的 ``status`` 直接来自 ``TurnStatus``。
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from sentrix.api.deps import get_container
from sentrix.api.schemas.assistant import (
    AttachmentSummary,
    CompleteEventPayload,
    ConversationDetailResponse,
    ConversationListResponse,
    ConversationSummary,
    CreateConversationRequest,
    MessageSummary,
    ProgressEventPayload,
    RenameConversationRequest,
    StartTurnRequest,
    StartTurnResponse,
    TurnSummary,
)
from sentrix.application.assistant.manage_conversations import ManageConversations
from sentrix.application.assistant.run_turn import (
    RunTurn,
    TurnNotFoundError,
    TurnRequest,
)
from sentrix.application.assistant.tools import AssistantToolbox
from sentrix.application.retrieval.retrieve_visual import RetrieveVisual
from sentrix.bootstrap.container import Container
from sentrix.domain.assistant.models import Conversation, ProgressEvent, TurnRecord

log = logging.getLogger("sentrix.api.assistant")

router = APIRouter(tags=["assistant"])

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def _conversation_summary(conversation: Conversation) -> ConversationSummary:
    return ConversationSummary(
        id=conversation.id,
        scope_id=conversation.scope_id,
        title=conversation.title,
        created_at=conversation.created_at.isoformat(),
        updated_at=conversation.updated_at.isoformat(),
        last_message_at=conversation.last_message_at.isoformat(),
    )


def _attachments(record: TurnRecord) -> list[AttachmentSummary]:
    return [
        AttachmentSummary(handle=item.handle, asset_id=item.asset_id, file_name=item.file_name)
        for item in record.attachments
    ]


def _turn_summary(record: TurnRecord) -> TurnSummary:
    return TurnSummary(
        id=record.id,
        question=record.question,
        status=record.status.value,
        termination_reason=record.termination_reason,
        answer=record.answer,
        attachments=_attachments(record),
        created_at=record.created_at.isoformat(),
    )


def _run_turn(container: Container) -> RunTurn:
    return RunTurn(
        model=container.chat_model,
        conversations=container.conversation_repository,
        turns=container.turn_repository,
        toolbox=AssistantToolbox(
            retrieval=RetrieveVisual(
                embedder=container.visual_embedder,
                vectors=container.vector_repository,
                index=container.vector_index,
            ),
            assets=container.asset_repository,
            result_sets=container.result_set_repository,
            people=container.person_directory,
        ),
    )


@router.post("/assistant/conversations", status_code=201)
def create_conversation(
    payload: CreateConversationRequest, container: Container = Depends(get_container)
) -> ConversationSummary:
    use_case = ManageConversations(
        conversations=container.conversation_repository, turns=container.turn_repository
    )
    return _conversation_summary(use_case.create(scope_id=payload.scope_id, title=payload.title))


@router.get("/assistant/conversations")
def list_conversations(
    scope_id: str = "home-default", container: Container = Depends(get_container)
) -> ConversationListResponse:
    use_case = ManageConversations(
        conversations=container.conversation_repository, turns=container.turn_repository
    )
    return ConversationListResponse(
        items=[_conversation_summary(item) for item in use_case.list_for_scope(scope_id)]
    )


@router.get("/assistant/conversations/{conversation_id}")
def get_conversation(
    conversation_id: str, container: Container = Depends(get_container)
) -> ConversationDetailResponse:
    use_case = ManageConversations(
        conversations=container.conversation_repository, turns=container.turn_repository
    )
    detail = use_case.detail(conversation_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return ConversationDetailResponse(
        conversation=_conversation_summary(detail.conversation),
        messages=[
            MessageSummary(
                id=message.id,
                role=message.role.value,
                content=message.content,
                turn_id=message.turn_id,
                created_at=message.created_at.isoformat(),
            )
            for message in detail.messages
        ],
        turns=[_turn_summary(turn) for turn in detail.turns],
    )


@router.patch("/assistant/conversations/{conversation_id}")
def rename_conversation(
    conversation_id: str,
    payload: RenameConversationRequest,
    container: Container = Depends(get_container),
) -> ConversationSummary:
    use_case = ManageConversations(
        conversations=container.conversation_repository, turns=container.turn_repository
    )
    if not use_case.rename(conversation_id, payload.title):
        raise HTTPException(status_code=404, detail="conversation not found")
    detail = use_case.detail(conversation_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return _conversation_summary(detail.conversation)


@router.delete("/assistant/conversations/{conversation_id}", status_code=204)
def delete_conversation(conversation_id: str, container: Container = Depends(get_container)) -> Response:
    use_case = ManageConversations(
        conversations=container.conversation_repository, turns=container.turn_repository
    )
    if not use_case.delete(conversation_id):
        raise HTTPException(status_code=404, detail="conversation not found")
    return Response(status_code=204)


@router.post("/assistant/conversations/{conversation_id}/turns", status_code=202)
def start_turn(
    conversation_id: str,
    payload: StartTurnRequest,
    container: Container = Depends(get_container),
) -> StartTurnResponse:
    """建一个回合并立刻返回 ``turn_id``，执行放在工作线程里。

    回合先落库（``pending``）、再由 SSE 订阅，是「前端不轮询」能成立的前提：SSE 端点
    永远有东西可订阅，哪怕连接晚到几百毫秒。
    """
    manage = ManageConversations(
        conversations=container.conversation_repository, turns=container.turn_repository
    )
    run_turn = _run_turn(container)
    try:
        record = run_turn.start(
            TurnRequest(conversation_id=conversation_id, scope_id=payload.scope_id, question=payload.message)
        )
    except TurnNotFoundError as error:
        # application 抛领域错误，映射成什么状态码是这里的事。
        raise HTTPException(status_code=404, detail="conversation not found") from error
    manage.ensure_title(conversation_id, payload.message)

    broker = container.turn_event_broker
    # 先登记再起飞：SSE 可能比工作线程的第一条事件更早连上来。
    broker.open(record.id)

    def worker() -> None:
        try:
            run_turn.run(record.id, emit=lambda event: broker.publish(record.id, event))
        except Exception:
            log.exception("回合执行失败: %s", record.id)
        finally:
            broker.finish(record.id)

    threading.Thread(target=worker, name=f"turn-{record.id}", daemon=True).start()
    return StartTurnResponse(turn_id=record.id, conversation_id=conversation_id, status=record.status.value)


def _format_progress(event: ProgressEvent) -> str:
    data = ProgressEventPayload(
        text=event.text,
        status=event.status.value,
        stage=event.stage.value,
        step_index=event.step_index,
        timestamp=event.timestamp,
        kind=event.kind,
        payload=dict(event.payload),
    ).model_dump()
    return f"id: {event.step_index}\nevent: progress\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _format_complete(record: TurnRecord) -> str:
    data = CompleteEventPayload(
        turn_id=record.id,
        status=record.status.value,
        termination_reason=record.termination_reason,
        answer=record.answer,
        attachments=[
            AttachmentSummary(handle=item.handle, asset_id=item.asset_id, file_name=item.file_name)
            for item in record.attachments
        ],
        requirements=[dict(item) for item in record.requirements],
    ).model_dump()
    return f"event: complete\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _resume_index(last_event_id: str) -> int:
    try:
        return max(0, int(last_event_id))
    except (TypeError, ValueError):
        return 0


@router.get("/assistant/turns/{turn_id}/events")
async def turn_events(
    turn_id: str,
    request: Request,
    last_event_id: str = Header(default="", alias="Last-Event-ID"),
    container: Container = Depends(get_container),
) -> StreamingResponse:
    """回合事件的 SSE 流。跑完即关，不需要前端再问一次状态。

    订阅顺序是**先读库、再订阅内存流**：库里是所有已经落盘的事件（断线重连与迟到加入
    都能补），内存流是此刻还没落盘的增量。两边按 ``step_index`` 去重。
    """
    if container.turn_repository.get(turn_id) is None:
        raise HTTPException(status_code=404, detail="turn not found")

    turns = container.turn_repository
    broker = container.turn_event_broker
    cursor = _resume_index(last_event_id)

    async def body() -> AsyncIterator[str]:
        nonlocal cursor
        for stored in turns.events_after(turn_id, cursor):
            cursor = stored.step_index
            yield _format_progress(stored)

        record = turns.get(turn_id)
        if record is not None and record.status.is_terminal:
            yield _format_complete(record)
            return

        async for event in broker.subscribe(turn_id):
            if await request.is_disconnected():
                return
            if event is None:
                break
            if event.step_index <= cursor:
                # 数据库那一轮已经发过了。broker 的 backlog 从回合一开始就记，
                # 重叠是正常的，靠 step_index 去重。
                continue
            cursor = event.step_index
            yield _format_progress(event)

        record = turns.get(turn_id)
        if record is not None:
            yield _format_complete(record)

    return StreamingResponse(body(), media_type="text/event-stream", headers=SSE_HEADERS)
```

- [ ] **Step 4: 运行测试，确认绿灯**

Run: `.venv/bin/pytest apps/api/tests/contract/test_assistant_events.py -p no:warnings`

Expected:

```text
13 passed
```

- [ ] **Step 5: 重新导出 OpenAPI**

Run: `.venv/bin/python scripts/export_openapi.py`

- [ ] **Step 6: 更新 module-map.md**

`docs/architecture/module-map.md`：`api/routes/assistant.py` 这一行的「依赖谁」不变（新增的 `StreamingResponse` / `Request` 是第三方，不进这两列）：

```markdown
| `sentrix/api/routes/assistant.py` | 助手端点：会话 CRUD / 建回合 / 回合事件的 SSE 流 | api | `sentrix/api/deps.py`、`sentrix/api/schemas/assistant.py`、`sentrix/application/assistant/manage_conversations.py`、`sentrix/application/assistant/run_turn.py`、`sentrix/application/assistant/tools.py`、`sentrix/application/retrieval/retrieve_visual.py`、`sentrix/bootstrap/container.py`、`sentrix/domain/assistant/models.py` | `sentrix/main.py` |
```

Run: `.venv/bin/python scripts/check_module_map.py`
Expected: `module-map: 0 个问题`

- [ ] **Step 7: 登记能力**

```json
{"capability": "回合事件的 SSE 流（progress / complete）", "origin_kind": "adapted", "source_commit": "2d2cee7c", "source_path": "backend/app.py", "source_symbol": "assistant_turn_events", "target_path": "apps/api/src/sentrix/api/routes/assistant.py", "verification": "apps/api/tests/contract/test_assistant_events.py", "status": "verified"}
```

Run: `.venv/bin/python scripts/check_capability_map.py`
Expected: `capability-map: 0 个问题`

- [ ] **Step 8: 全量门禁**

Run: `make ci`

---

## Task 44: Web 助手对话界面（SSE，不轮询）

**依赖：** Task 43。

**Files:**
- Create: `apps/web/src/features/assistant/AssistantPanel.tsx`
- Create: `apps/web/src/features/assistant/useTurnStream.ts`
- Modify: `apps/web/src/shared/api/client.ts`
- Modify: `apps/web/src/App.tsx`
- Modify: `apps/web/src/styles.css`

**这一格的设计要点是它不做什么。** 1.0 的前端在 `es.onerror` 里一次性放弃 SSE、永久降级成 700ms 轮询（`src/app.js:1480-1520`），这是 668,490 次请求（占全部流量 73%）的直接来源。2.0 的 `onerror` 只做一件事：如实报错，让用户重发。**没有轮询兜底这条路径。**

两处刻意的细节：

1. **`complete` 到达后显式 `close()`。** 服务端发完那一帧就关流，浏览器会把它当成「连接断开」触发 `onerror`；不先标记完成的话，一个正常的回合会被显示成错误。
2. **交付的照片只来自 `complete` 帧里的 `attachments`**，前端不自己从候选里挑图、不做任何相关性计算——那是后端的事（S2 的 `RETRIEVAL` 边界）。

- [ ] **Step 1: 先写失败测试（前端这一格的"测试"是类型检查）**

前端目前没有测试框架（设计 §6.1：`Vitest` 尚未接入），所以本任务的红灯层就是 `tsc -b`——`useTurnStream.ts` 与 `AssistantPanel.tsx` 引用了 `client.ts` 里还不存在的 `startTurn` / `turnEventsUrl`。

Run（在 Task 43 的树上执行）: `cd apps/web && npx tsc -b`
Expected: 通过（此时 App.tsx 还没引用助手，所以类型检查是绿的——**本任务没有红灯层**，理由见文末「验证记录」）

- [ ] **Step 2: 实现前端**

`apps/web/src/shared/api/client.ts`（新增助手一节；其余保持 S2 原样）：

```text
const BASE = "/api/v1";

export interface AssetSummary {
  id: string;
  file_name: string;
  media_type: string;
  mime_type: string | null;
  size_bytes: number;
  status: string;
  captured_at: string | null;
  gps: { latitude: number; longitude: number } | null;
}

interface AssetListResponse {
  items: AssetSummary[];
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, init);
  if (!response.ok) {
    const body = await response.text();
    throw new Error(`Sentrix API ${response.status}: ${body}`);
  }
  return (await response.json()) as T;
}

export async function listAssets(scopeId: string): Promise<AssetSummary[]> {
  const body = await request<AssetListResponse>(`/assets?scope_id=${encodeURIComponent(scopeId)}`);
  return body.items;
}

export function assetFileUrl(assetId: string): string {
  return `${BASE}/assets/${assetId}/file`;
}

export function assetPreviewUrl(assetId: string): string {
  return `${BASE}/assets/${assetId}/preview`;
}

/** 浏览器能直接渲染的格式走原图，其余（HEIC 等）走服务端转码预览。 */
export function assetDisplayUrl(asset: AssetSummary): string {
  if (asset.mime_type === "image/heic" || asset.mime_type === "image/heif") {
    return assetPreviewUrl(asset.id);
  }
  return assetFileUrl(asset.id);
}

/** 后端已经按 mime 决定过怎么交付；助手交付的照片只带回这些字段，走同一条判断。 */
export function deliveredPhotoUrl(assetId: string, fileName: string): string {
  const lowered = fileName.toLowerCase();
  if (lowered.endsWith(".heic") || lowered.endsWith(".heif")) {
    return assetPreviewUrl(assetId);
  }
  return assetFileUrl(assetId);
}

export interface SearchHit {
  asset: AssetSummary;
  score: number;
  channels: Record<string, number>;
  raw_scores: Record<string, number>;
}

export interface SearchResponse {
  query: string;
  backend: string;
  degraded: boolean;
  degraded_reason: string;
  authoritative_vectors: number;
  items: SearchHit[];
}

/** 按内容词检索。后端负责嵌入、召回与融合，前端不做任何相关性计算。 */
export async function searchAssets(
  query: string,
  scopeId: string,
  limit = 20,
): Promise<SearchResponse> {
  const params = new URLSearchParams({ q: query, scope_id: scopeId, limit: String(limit) });
  return request<SearchResponse>(`/search?${params.toString()}`);
}

// ---- 助手 -------------------------------------------------------------------

export interface ConversationSummary {
  id: string;
  scope_id: string;
  title: string;
  created_at: string;
  updated_at: string;
  last_message_at: string;
}

export interface AssistantProgressEvent {
  text: string;
  status: string;
  stage: string;
  step_index: number;
  timestamp: string;
  kind: string;
  payload: Record<string, unknown>;
}

export interface AssistantAttachment {
  handle: string;
  asset_id: string;
  file_name: string;
}

export interface AssistantRequirement {
  id: string;
  evidence_type: string;
  description: string;
  status: string;
  satisfied_by: string[];
  unmet_reason: string;
}

export interface AssistantCompleteEvent {
  type: string;
  turn_id: string;
  status: string;
  termination_reason: string;
  answer: string;
  attachments: AssistantAttachment[];
  requirements: AssistantRequirement[];
  error: string;
}

export async function createConversation(scopeId: string): Promise<ConversationSummary> {
  return request<ConversationSummary>("/assistant/conversations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scope_id: scopeId }),
  });
}

export async function startTurn(
  conversationId: string,
  scopeId: string,
  message: string,
): Promise<{ turn_id: string; conversation_id: string; status: string }> {
  return request(`/assistant/conversations/${conversationId}/turns`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, scope_id: scopeId }),
  });
}

/**
 * 回合事件的 SSE 地址。
 *
 * 前端**只开这一条连接**：1.0 靠 700ms 轮询 ``GET /api/assistant/turn/{id}`` 拿进度，
 * 产生了 668,490 次请求（占全部流量 73%）。2.0 的后端把进度事件也落了库，断线重连
 * 由 ``Last-Event-ID`` 续上，所以前端没有任何理由再轮询。
 */
export function turnEventsUrl(turnId: string): string {
  return `${BASE}/assistant/turns/${turnId}/events`;
}
```

`apps/web/src/features/assistant/useTurnStream.ts`:

```text
import { useCallback, useEffect, useRef, useState } from "react";

import {
  startTurn,
  turnEventsUrl,
  type AssistantCompleteEvent,
  type AssistantProgressEvent,
} from "../../shared/api/client";

export interface TurnStream {
  progress: AssistantProgressEvent[];
  result: AssistantCompleteEvent | null;
  busy: boolean;
  error: string | null;
  ask: (conversationId: string, scopeId: string, message: string) => Promise<void>;
}

/**
 * 一次提问的实时过程：``POST`` 建回合，然后**一条** SSE 连接读到底。
 *
 * 两处刻意的选择：
 *
 * 1. **不在 ``onerror`` 里降级成轮询。** 1.0 就是这么做的——``es.onerror`` 一旦触发，
 *    EventSource 永久让位给 700ms 轮询，于是轮询成了主路径（668,490 次请求）。
 *    这里连接断了就如实报错，用户重发即可；回合本身已经落库，重连还能续上。
 * 2. **``complete`` 到达后显式 close。** 服务端发完那一帧就关流，浏览器会把它当成
 *    「连接断开」触发 ``onerror``；不先标记完成的话，一个正常的回合会被显示成错误。
 */
export function useTurnStream(): TurnStream {
  const [progress, setProgress] = useState<AssistantProgressEvent[]>([]);
  const [result, setResult] = useState<AssistantCompleteEvent | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const sourceRef = useRef<EventSource | null>(null);
  const finishedRef = useRef(false);

  const close = useCallback(() => {
    sourceRef.current?.close();
    sourceRef.current = null;
  }, []);

  useEffect(() => close, [close]);

  const ask = useCallback(
    async (conversationId: string, scopeId: string, message: string) => {
      close();
      finishedRef.current = false;
      setProgress([]);
      setResult(null);
      setError(null);
      setBusy(true);

      let turnId: string;
      try {
        const started = await startTurn(conversationId, scopeId, message);
        turnId = started.turn_id;
      } catch (cause) {
        setError((cause as Error).message);
        setBusy(false);
        return;
      }

      const source = new EventSource(turnEventsUrl(turnId));
      sourceRef.current = source;

      source.addEventListener("progress", (event) => {
        const payload = JSON.parse((event as MessageEvent<string>).data) as AssistantProgressEvent;
        setProgress((previous) => [...previous, payload]);
      });

      source.addEventListener("complete", (event) => {
        const payload = JSON.parse((event as MessageEvent<string>).data) as AssistantCompleteEvent;
        finishedRef.current = true;
        setResult(payload);
        setBusy(false);
        close();
      });

      source.onerror = () => {
        if (finishedRef.current) {
          return;
        }
        setError("实时连接中断了，请重新发送这条消息。");
        setBusy(false);
        close();
      };
    },
    [close],
  );

  return { progress, result, busy, error, ask };
}
```

`apps/web/src/features/assistant/AssistantPanel.tsx`:

```text
import { useEffect, useRef, useState, type FormEvent } from "react";

import { createConversation, deliveredPhotoUrl } from "../../shared/api/client";
import { useTurnStream } from "./useTurnStream";

interface Exchange {
  question: string;
  answer: string;
  attachments: { handle: string; asset_id: string; file_name: string }[];
  status: string;
  reason: string;
}

/**
 * 助手对话。问一句「给我看看在海边的照片」，回答下面就是能直接点开的照片。
 *
 * 交付的照片只来自后端 ``complete`` 帧里的 ``attachments``——那是**模型显式调用
 * get_original_photos 交出去的那些**。前端不自己从候选里挑图，也不做任何相关性判断。
 */
export function AssistantPanel({ scopeId }: { scopeId: string }) {
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [exchanges, setExchanges] = useState<Exchange[]>([]);
  const { progress, result, busy, error, ask } = useTurnStream();
  const settled = useRef<string | null>(null);

  useEffect(() => {
    createConversation(scopeId)
      .then((conversation) => setConversationId(conversation.id))
      .catch(() => setConversationId(null));
  }, [scopeId]);

  // ``complete`` 到了就把答案补到最后一条提问上——回合是先落库再执行的，
  // 提问在发出去的那一刻就已经显示出来了。
  useEffect(() => {
    if (result === null || settled.current === result.turn_id) {
      return;
    }
    settled.current = result.turn_id;
    setExchanges((previous) =>
      previous.map((item, index) =>
        index === previous.length - 1
          ? {
              ...item,
              answer: result.answer,
              attachments: result.attachments,
              status: result.status,
              reason: result.termination_reason,
            }
          : item,
      ),
    );
  }, [result]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const question = text.trim();
    if (question === "" || conversationId === null || busy) {
      return;
    }
    setText("");
    setExchanges((previous) => [
      ...previous,
      { question, answer: "", attachments: [], status: "running", reason: "" },
    ]);
    await ask(conversationId, scopeId, question);
  }

  return (
    <section className="assistant">
      <h2>问一句</h2>
      <form className="assistant-ask" onSubmit={submit}>
        <input
          value={text}
          onChange={(event) => setText(event.target.value)}
          placeholder="例如：给我看看在海边的照片"
          aria-label="向助手提问"
          disabled={conversationId === null}
        />
        <button type="submit" disabled={busy || conversationId === null || text.trim() === ""}>
          {busy ? "处理中…" : "发送"}
        </button>
      </form>

      {error ? <p className="error">{error}</p> : null}

      {busy && progress.length > 0 ? (
        <ol className="assistant-progress">
          {progress.map((item) => (
            <li key={`${item.step_index}-${item.kind}`} className={`stage-${item.stage}`}>
              {item.text}
            </li>
          ))}
        </ol>
      ) : null}

      <div className="assistant-log">
        {exchanges.map((exchange, index) => (
          <article key={index} className="assistant-exchange">
            {exchange.question ? <p className="question">{exchange.question}</p> : null}
            {exchange.answer ? <p className="answer">{exchange.answer}</p> : null}
            {exchange.status !== "running" && exchange.status !== "complete" ? (
              <p className="note">
                这一轮的状态：{exchange.status}（{exchange.reason}）
              </p>
            ) : null}
            {exchange.attachments.length > 0 ? (
              <ul className="assistant-photos">
                {exchange.attachments.map((photo) => (
                  <li key={photo.handle}>
                    <a
                      href={deliveredPhotoUrl(photo.asset_id, photo.file_name)}
                      target="_blank"
                      rel="noreferrer"
                    >
                      <img
                        src={deliveredPhotoUrl(photo.asset_id, photo.file_name)}
                        alt={photo.file_name}
                        loading="lazy"
                      />
                    </a>
                  </li>
                ))}
              </ul>
            ) : null}
          </article>
        ))}
      </div>
    </section>
  );
}
```

`apps/web/src/App.tsx`:

```text
import { useEffect, useState } from "react";

import { AssistantPanel } from "./features/assistant/AssistantPanel";
import { AssetGrid } from "./features/library/AssetGrid";
import { SearchBox } from "./features/search/SearchBox";
import { SearchResults } from "./features/search/SearchResults";
import { listAssets, type AssetSummary, type SearchResponse } from "./shared/api/client";

const SCOPE_ID = "album1";

export default function App() {
  const [assets, setAssets] = useState<AssetSummary[]>([]);
  const [results, setResults] = useState<SearchResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listAssets(SCOPE_ID)
      .then(setAssets)
      .catch((cause: Error) => setError(cause.message));
  }, []);

  return (
    <main className="app">
      <h1>Sentrix Home 2.0</h1>
      <AssistantPanel scopeId={SCOPE_ID} />
      <SearchBox scopeId={SCOPE_ID} onResults={setResults} />
      {error ? <p className="error">{error}</p> : null}
      {results ? (
        <SearchResults response={results} onClose={() => setResults(null)} />
      ) : (
        <AssetGrid assets={assets} />
      )}
    </main>
  );
}
```

`apps/web/src/styles.css`（完整文件；助手样式追加在末尾）：

```text
:root { color-scheme: light dark; font-family: system-ui, sans-serif; }
body { margin: 0; }
.app { padding: 1.5rem; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 0.75rem; list-style: none; padding: 0; }
.grid button { width: 100%; background: none; border: 1px solid #8884; border-radius: 8px; padding: 0.5rem; cursor: pointer; }
.grid img { width: 100%; height: 140px; object-fit: cover; border-radius: 4px; display: block; }
.grid span { display: block; font-size: 0.75rem; margin-top: 0.35rem; overflow-wrap: anywhere; }
.detail { position: fixed; inset: 0; background: #000c; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 1rem; padding: 2rem; }
.detail img { max-width: min(90vw, 1200px); max-height: 62vh; object-fit: contain; }
.detail dl { display: grid; grid-template-columns: auto auto; gap: 0.25rem 1rem; color: white; margin: 0; }
.detail dt { opacity: 0.7; }
.error { color: #c33; }
.empty { opacity: 0.7; }
.search { display: flex; gap: 0.5rem; margin-bottom: 1rem; }
.search input { flex: 1; max-width: 28rem; padding: 0.5rem 0.75rem; border: 1px solid #8884; border-radius: 8px; font-size: 1rem; }
.search button { padding: 0.5rem 1rem; border: 1px solid #8884; border-radius: 8px; cursor: pointer; }
.results-header { display: flex; align-items: baseline; gap: 1rem; }
.results-header h2 { font-size: 1.1rem; margin: 0; }
.degraded { background: #ffb02022; border: 1px solid #ffb02066; border-radius: 8px; padding: 0.5rem 0.75rem; font-size: 0.85rem; }
.grid small { display: block; opacity: 0.6; font-size: 0.7rem; }

/* ---- 助手对话 ---- */

.assistant {
  margin: 24px 0;
  padding: 16px;
  border: 1px solid #e2e8f0;
  border-radius: 12px;
}

.assistant-ask {
  display: flex;
  gap: 8px;
}

.assistant-ask input {
  flex: 1;
}

.assistant-progress {
  margin: 12px 0 0;
  padding-left: 20px;
  color: #64748b;
  font-size: 13px;
}

.assistant-exchange {
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px solid #eef2f7;
}

.assistant-exchange .question {
  font-weight: 600;
}

.assistant-exchange .note {
  color: #b45309;
  font-size: 13px;
}

.assistant-photos {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  padding: 0;
  list-style: none;
}

.assistant-photos img {
  width: 160px;
  height: 160px;
  object-fit: cover;
  border-radius: 8px;
  display: block;
}
```

- [ ] **Step 3: 运行前端门禁，确认绿灯**

Run: `cd apps/web && npx tsc -b && npx oxlint`

Expected:

```text
Found 0 warnings and 0 errors.
Finished in 25ms on 10 files with 116 rules using 32 threads.
```

（`116 rules using 32 threads` 里的文件数由 8 变成 10——新增了两个 `.tsx`。耗时与线程数不计入判定。）

- [ ] **Step 4: 全量门禁**

Run: `make ci`

---

## Task 45: 助手闭环端到端验收

**依赖：** Task 44。

**Files:**
- Create: `apps/api/tests/e2e/test_assistant_turn.py`
- Modify: `docs/refactor/capability-map.jsonl`

真 chinese-clip（进程内推理）+ 真 Qdrant + 真 SQLite + **8100 上真的 llama.cpp**，走完整 HTTP 链路。

判据里刻意不包含「模型说了什么」——那是模型质量的验收，按设计 §9.1 由使用者肉眼做。这里钉的是链路的结构性事实：回合以一个有名字的原因结束、用户要图时回答里真的带回可打开的照片、那些照片的 URL 真的能取到字节。

- [ ] **Step 1: 写验收测试**

`apps/api/tests/e2e/test_assistant_turn.py`:

```python
"""端到端验收：问一句「给我看看海边的照片」，拿回能打开的原图。

这一层用**真 chinese-clip（进程内推理）+ 真 Qdrant + 真 SQLite + 8100 上真的 llama.cpp**，
走完整 HTTP 链路（设计 §8 的 S3 验收）。

判据里刻意不包含「模型说了什么」——那是模型质量的验收，按设计 §9.1 由使用者肉眼做。
这里钉的是**链路的结构性事实**：

1. 回合以一个有名字的原因结束（不是空、不是 None）；
2. 用户要图时，回答里真的带回了可打开的照片（设计 §1.1 硬约束第 5 条）；
3. 那些照片的 URL 真的能取到字节；
4. 全过程只用了一条 SSE 连接，没有轮询任何状态端点。
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from sentrix.infrastructure.models.chinese_clip import DEFAULT_CHECKPOINT
from sentrix.main import create_app

PHOTO_FIXTURES = Path("/home/asus/Github/Sentrix-Home-Web/data/album3-v2-source/photos")

SCOPE = "assistant-album"
# 只在 8100 上打这么多次模型调用。llama-server 是 --parallel 1，串行排队。
MAX_MODEL_CALLS = 6


@pytest.fixture(scope="module", autouse=True)
def requires_the_real_environment() -> None:
    """本层要的是真环境：本地权重 + 一个跑着的 8100。

    两样缺一就跳过，并给出可执行的提示——与同目录 ``test_visual_search.py`` 对权重的
    处理保持一致（设计 §9.1：模型与真实功能验证由使用者负责，自动层只保证链路没坏）。
    """
    if not DEFAULT_CHECKPOINT.is_file():
        pytest.skip(f"chinese-clip 权重不可用: {DEFAULT_CHECKPOINT}")
    import httpx

    from sentrix.config import Settings

    base_url = Settings().vlm_base_url
    try:
        response = httpx.post(
            f"{base_url.rstrip('/')}/chat/completions",
            json={
                "model": Settings().chat_model_name,
                "messages": [{"role": "user", "content": "好"}],
                "max_tokens": 4,
            },
            timeout=20.0,
        )
        response.raise_for_status()
    except Exception as error:  # noqa: BLE001 - 环境不可用时给出可执行的提示
        pytest.skip(f"8100（{base_url}）上的对话端点不可用，S3 的模型走这个端点: {error}")


def photo_bytes(colour: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (224, 224), colour).save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.fixture
def client(settings) -> TestClient:
    """真容器：真模型、真 Qdrant、独立临时数据目录。"""
    return TestClient(create_app(settings))


def import_photo(client: TestClient, name: str, payload: bytes) -> str:
    response = client.post(
        "/api/v1/media/import",
        files={"file": (name, payload, "image/jpeg")},
        data={"scope_id": SCOPE},
    )
    assert response.status_code == 201, response.text
    assert response.json()["indexed"] is True, "导入必须写成视觉向量"
    return response.json()["asset_id"]


def start_conversation(client: TestClient) -> str:
    response = client.post("/api/v1/assistant/conversations", json={"scope_id": SCOPE})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def read_events(client: TestClient, turn_id: str) -> list[dict]:
    import json

    with client.stream("GET", f"/api/v1/assistant/turns/{turn_id}/events") as response:
        assert response.status_code == 200
        lines = [line for line in response.iter_lines() if line]

    events: list[dict] = []
    current: dict = {}
    for line in lines:
        if line.startswith("event: "):
            current["event"] = line[7:]
        elif line.startswith("data: "):
            current["data"] = json.loads(line[6:])
            events.append(current)
            current = {}
    return events


def ask(client: TestClient, conversation_id: str, question: str) -> tuple[str, list[dict]]:
    response = client.post(
        f"/api/v1/assistant/conversations/{conversation_id}/turns",
        json={"message": question, "scope_id": SCOPE},
    )
    assert response.status_code == 202, response.text
    turn_id = response.json()["turn_id"]
    return turn_id, read_events(client, turn_id)


def progress_of(events: Sequence[dict]) -> list[dict]:
    return [item["data"] for item in events if item["event"] == "progress"]


def test_asking_for_photos_returns_openable_originals(client: TestClient):
    """S3 的验收问句。"""
    import_photo(client, "海边_蓝.jpg", photo_bytes((20, 60, 200)))
    import_photo(client, "草地_绿.jpg", photo_bytes((30, 180, 60)))
    import_photo(client, "雪地_白.jpg", photo_bytes((240, 240, 245)))

    conversation_id = start_conversation(client)
    turn_id, events = ask(client, conversation_id, "给我看看在海边的照片")

    complete = events[-1]["data"]
    assert complete["status"] in {"complete", "partial"}
    assert complete["termination_reason"]
    assert complete["attachments"], "用户要图时回答必须带回照片，不能只回一段文字"

    # 每张交付的照片都必须真的能打开——设计 §1.1 硬约束第 5 条。
    for attachment in complete["attachments"]:
        response = client.get(f"/api/v1/assets/{attachment['asset_id']}/file")
        assert response.status_code == 200, attachment
        assert response.content[:2] == b"\xff\xd8", "取回的应当是真正的 JPEG 字节"

    # 交付的 handle 必须来自本轮的结果集，而不是模型编的编号。
    assert all(item["handle"].startswith("photo_") for item in complete["attachments"])
    assert len(complete["attachments"]) <= 6


def test_the_answer_never_leaks_internal_identifiers(client: TestClient):
    import_photo(client, "海边_蓝2.jpg", photo_bytes((25, 70, 210)))

    conversation_id = start_conversation(client)
    _turn_id, events = ask(client, conversation_id, "给我看看在海边的照片")

    answer = events[-1]["data"]["answer"]
    assert "photo_" not in answer
    assert "asset_" not in answer
    assert "result_set" not in answer


def test_the_turn_records_the_whole_reasoning_chain(client: TestClient):
    """规划 → 工具循环 → 证据账本 → 答案门禁 → 回答，每一步都留下痕迹。"""
    import_photo(client, "海边_蓝3.jpg", photo_bytes((15, 50, 190)))

    conversation_id = start_conversation(client)
    _turn_id, events = ask(client, conversation_id, "给我看看在海边的照片")
    progress = progress_of(events)

    assert progress[0]["kind"] == "plan"
    assert progress[0]["payload"]["requirements"], "规划必须给出证据需求"
    assert any(item["kind"] == "tool_result" for item in progress), "至少要真正用过一次工具"
    assert progress[-1]["stage"] == "terminal"
    assert events[-1]["data"]["termination_reason"]


def test_a_conversational_question_still_terminates_with_a_reason(client: TestClient):
    """闲聊没有照片可交付，但回合仍然要以一个有名字的原因收场。

    1.0 的 25,112 条轨迹里有 11,304 条读不到 ``termination_reason``。
    """
    conversation_id = start_conversation(client)
    _turn_id, events = ask(client, conversation_id, "你好呀")

    complete = events[-1]["data"]
    assert complete["termination_reason"]
    assert complete["status"] in {"complete", "partial", "blocked", "failed"}


def test_a_follow_up_question_sees_the_previous_turn(client: TestClient):
    import_photo(client, "海边_蓝4.jpg", photo_bytes((18, 55, 195)))

    conversation_id = start_conversation(client)
    ask(client, conversation_id, "给我看看在海边的照片")
    _turn_id, events = ask(client, conversation_id, "刚才那些是哪年拍的？")

    assert events[-1]["data"]["answer"]


def test_unknown_scope_still_answers_honestly(client: TestClient):
    """库里什么都没有时不能编造。"""
    conversation_id = start_conversation(client)
    _turn_id, events = ask(client, conversation_id, "给我看看在月球上的照片")

    complete = events[-1]["data"]
    assert complete["termination_reason"]
    assert complete["attachments"] == []
```

- [ ] **Step 2: 运行，确认绿灯**

Run: `.venv/bin/pytest apps/api/tests/e2e/test_assistant_turn.py -q -p no:warnings`

Expected:

```text
6 passed
```

（本任务**没有红灯层**：它只新增一个测试文件，不改任何实现。它验的是「前面十一个任务拼起来到底能不能用」，所以红灯是「跑不起来」——而那正是前面每一步的红灯。理由见文末「验证记录」。）

- [ ] **Step 3: 登记能力**

```json
{"capability": "助手端到端问答", "origin_kind": "NEW-2.0", "source_commit": "2d2cee7c", "source_path": "backend/agent_runtime/runtime.py", "source_symbol": "AgentRuntime.run", "target_path": "apps/api/src/sentrix/api/routes/assistant.py", "verification": "apps/api/tests/e2e/test_assistant_turn.py", "status": "verified"}
```

Run: `.venv/bin/python scripts/check_capability_map.py`
Expected: `capability-map: 0 个问题`

- [ ] **Step 4: 全量门禁**

Run: `make ci`

---

## 人工验收（使用者执行）

自动验收只能保证链路没坏。**"问一句「给我看看在海边的照片」，回来的图对不对"必须用眼睛看**。

起服务（端口自己挑一个空的；153 上 8091/9598/8100/8101/8500 都被占着）：

```bash
cd /home/asus/Github/sentrix-home-web2.0
SENTRIX_DATA_DIR=$PWD/data .venv/bin/python -m uvicorn --factory sentrix.main:create_app \
  --host 127.0.0.1 --port 8391 --app-dir apps/api/src
```

导入照片、建会话、问一句：

```bash
for f in /path/to/photos/*.jpg; do
  curl -s -F "file=@$f" -F scope_id=album1 http://127.0.0.1:8391/api/v1/media/import
done

CONV=$(curl -s -X POST http://127.0.0.1:8391/api/v1/assistant/conversations \
  -H 'Content-Type: application/json' -d '{"scope_id":"album1"}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')

TURN=$(curl -s -X POST "http://127.0.0.1:8391/api/v1/assistant/conversations/$CONV/turns" \
  -H 'Content-Type: application/json' \
  -d '{"message":"给我看看在海边的照片","scope_id":"album1"}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["turn_id"])')

# 一条 SSE 连接读到底；跑完即关，不需要再问状态
curl -N -s "http://127.0.0.1:8391/api/v1/assistant/turns/$TURN/events"
```

再起 Web（`cd apps/web && npm run dev -- --port 4274`，vite 把 `/api` 代理到 9598——**换端口时记得同步改 `vite.config.ts`**），在「问一句」的输入框里打「给我看看在海边的照片」，看回答下面的图对不对、点得开。

### 实测记录（规划阶段在隔离副本上跑过一遍）

数据：三张纯色图（蓝 / 绿 / 白），用真 chinese-clip 嵌入、真 Qdrant 索引、8100 上真的 llama.cpp 作答。

```text
$ .venv/bin/pytest apps/api/tests/e2e/test_assistant_turn.py -q -p no:warnings
......                                                                   [100%]
6 passed
```

单次回合的进度流（节选，`GET /api/v1/assistant/turns/{id}/events`）：

```text
id: 1
event: progress
data: {"text": "已确认需要哪些证据。", "status": "complete", "stage": "planning", "step_index": 1, ..., "kind": "plan",
       "payload": {"requirements": [{"id": "photo_set", ...}, {"id": "delivered_photo", ...}]}}
id: 3
event: progress
data: {"text": "正在执行 search_memories…", "status": "running", "stage": "action", "step_index": 3, ..., "kind": "model_action"}
id: 4
event: progress
data: {"text": "结果集里共有 3 张候选照片。", "status": "ok", "stage": "tool_result", "step_index": 4, ...}
id: 6
event: progress
data: {"text": "正在执行 get_original_photos…", "status": "running", "stage": "action", "step_index": 6, ...}
id: 7
event: progress
data: {"text": "已把 3 张照片交给用户。", "status": "ok", "stage": "tool_result", "step_index": 7, ...}
event: complete
data: {"type": "complete", "turn_id": "turn_...", "status": "complete", "termination_reason": "answered",
       "answer": "我为您检索到了三张与“海边”相关的照片。",
       "attachments": [{"handle": "photo_1", "asset_id": "asset_...", "file_name": "海边_蓝.jpg"}, ...],
       "requirements": [...]}
```

**两次交付门禁真的拦下过模型。** 把门禁关掉（或把 `delivered_photo` 需求去掉）时，实测 8100 上模型的行为是：

```text
我为您找到了两张在海边的照片。
1. **照片 1**：在2017年11月5日拍摄，拍摄地点在**浙江省杭州市萧山区**……
您是想查看哪张照片的具体内容呢？
```

一张图都没交付。加上门禁之后，模型被提醒一次，随后调用 `get_original_photos`，`complete` 帧里就有了 `attachments`。

**空回答的恢复也被真实触发过。** 上面那次 e2e 的 `REPLY` 轨迹里有连续两条：

```text
REPLY finish='stop' content='' tools=[]          ← 模型吐了个 EOS
REPLY finish='stop' content='我为您检索到了三张与“海边”相关的照片。' tools=[]
```

第一条被 `empty_answer` 兜住，提醒一次之后模型正常作答。

---

## 已知边界与不做的事

| 项 | 说明 | 核实（对实现） |
|---|---|---|
| **检索只有视觉一路；S3 的「XX」只能是视觉上认得出的内容** | `search_memories` 只调 S2 的 `RetrieveVisual`（`visual_ann` 单通道）——「在海边的照片」命中的是跨模态相似度，不是地点解析。所以 S3 的验收与示例里那个「XX」必须是**画面里看得见**的东西：海边 / 桥 / 雪 / 猫 / 狗。**地名（「在杭州的照片」）与时间（「去年冬天的照片」）检索不属于 S3**，它们的召回通道在 S4（人物的五路补齐在 S6） | ✅ `application/assistant/tools.py` 的 `_search` 里只有一次 `RetrieveVisual.execute`；全仓无 `geocod` / `place` / 时间过滤检索路径 |
| **没有相关性阈值** | 与 S2 同一条边界：毫不相干的词照样返回排好序的候选。门禁的 `fabrication_from_empty` 只在**一条向量都没有**时生效 | ✅ `final_guard.py` 的 `search_returned_nothing` 由 `is_empty_result` 算出，后者只看 `total == 0` |
| **模型看不到候选总数** | `model_visible` 里刻意没有 `total`——1.0 实测过模型会把候选池数量当成答案里的统计数字（`runtime.py:116`）。模型只看到 `has_more` 这个布尔 | ✅ `application/assistant/tools.py` 的 `_page_outcome`；`test_the_model_never_sees_asset_ids_or_the_candidate_total` 钉住 |
| **人物档案只有一格** | S3 没有身份层（S5 落地）。`get_person_profile` 只回答「有没有这个已确认人物、家庭角色是什么」，其余问题工具描述里已经说清楚拿不到 | ✅ `infrastructure/assistant/identity.py` 的 `NoIdentityDirectory` 恒返回 `None`，工具据此封口该需求 |
| **交付不做回填** | 只交付模型**显式**调用 `get_original_photos` 交出的 handle。模型没选 → 交付为空，这是合法状态（1.0 的交付口径 E 也是这么定的） | ✅ `get_original_photos` 的 `handles` 是必填数组；`test_delivery_needs_an_explicit_handle_list` 钉住 |
| **结果集没有 TTL** | 1.0 的 `agent_result_sets.expires_at` 是 24 小时，且只有内存侧会清、库里不会。2.0 不设过期——结果集与回合同生共死，一个会话删掉时一起走 | ✅ 迁移 0004 无 `expires_at`；`SqliteConversationRepository.delete` 连带删回合与事件 |
| **单进程** | 事件流是进程内的 `asyncio.Queue`；多 worker 下 SSE 与执行线程可能不在同一个进程。1.0 靠内存 dict 更是彻底不能用多 worker，而 2.0 的**历史回放**已经落库，所以换多 worker 只需要把 broker 换成外部 pub/sub | ✅ `infrastructure/assistant/events.py` 的 docstring 写明；`turn_event_broker` 在 container 里是一个端口，替换点是唯一的 |
| **没有取消** | 1.0 的 `/cancel` 是协作式轮询（只在模型循环的检查点生效），且对 photo thread 撒谎（那条路径根本没接 `should_cancel`）。S3 不做这条路径，也就没有这个谎 | ✅ 全仓无 `cancel` 端点；`TaskState` 无 `cancel_requested` 字段 |
| **回合是线程跑的** | 一次回合占一个工作线程（同步 httpx + 同步 SQLite）。llama-server 是 `--parallel 1`，本来就串行 | ✅ `api/routes/assistant.py` 的 `threading.Thread(...)`；设计 §5.3 已记录 GPU 与并发的约束 |

---

## 验证记录

以下是在隔离副本 `/home/asus/s3verify/repo` 上实际跑出来的，不是承诺。

> **这是某一次实测的快照，不是现行断言。** 权威来源是各任务的 commit：**红灯**用「前一任务 commit + 本任务的全部 `tests/` 文件」跑，**绿灯**用该任务自己的 commit 跑。执行时要对照的是任务正文里的 `Expected:` 块；本表只用于回顾「当时红成什么样、绿成什么样」，两者对不上时以正文与 commit 为准。
>
> commit 映射：Task 34 → `45afaca`，35 → `7521d42`，36 → `594e39b`，37 → `a48b8b6`，38 → `2514afb`，39 → `ace80d3`，40 → `d7c7d45`，41 → `55d4b50`，42 → `08dc146`，43 → `9d3bc51`，44 → `af3b8a6`，45 → `bc1d53b`（前缀 `b98b585` 是 S2 的终点）。

### 每个任务的红灯（只留测试、去掉实现）

| 任务 | 命令 | exit | 观察到的失败 |
|---|---|---:|---|
| 34 | `pytest apps/api/tests/unit/test_assistant_task_state.py` | 2 | `ModuleNotFoundError: No module named 'sentrix.domain.assistant.models'` |
| 35 | `pytest apps/api/tests/unit/test_assistant_ledger.py` | 2 | `ModuleNotFoundError: No module named 'sentrix.domain.assistant.evidence_ledger'` |
| 36 | `pytest apps/api/tests/unit/test_assistant_policy.py` | 2 | `ModuleNotFoundError: No module named 'sentrix.domain.assistant.tools'` |
| 37 | `pytest apps/api/tests/unit/test_assistant_intent.py apps/api/tests/unit/test_assistant_guard.py` | 2 | `ModuleNotFoundError: No module named 'sentrix.domain.assistant.emergency'`（两个文件都在收集期失败） |
| 38 | `pytest apps/api/tests/integration/test_assistant_repositories.py` | 2 | `ModuleNotFoundError: No module named 'sentrix.infrastructure.persistence.repositories.conversations'` |
| 39 | `pytest apps/api/tests/integration/test_chat_client.py apps/api/tests/integration/test_turn_event_broker.py` | 2 | `ModuleNotFoundError: No module named 'sentrix.infrastructure.assistant.events'` |
| 40 | `pytest apps/api/tests/integration/test_assistant_toolbox.py` | 2 | `ModuleNotFoundError: No module named 'sentrix.application.assistant'` |
| 41 | `pytest apps/api/tests/integration/test_run_turn.py` | 2 | `ModuleNotFoundError: No module named 'sentrix.application.assistant.run_turn'` |
| 42 | `pytest apps/api/tests/contract/test_assistant_routes.py` | 1 | `assert 404 == 201`、`KeyError: 'id'`，13 条 FAILED |
| 43 | `pytest apps/api/tests/contract/test_assistant_events.py` | 1 | `assert 404 == 200`，13 条 FAILED |
| 44 | `cd apps/web && npx tsc -b` | 0 | **没有红灯层**——见下 |
| 45 | `pytest apps/api/tests/e2e/test_assistant_turn.py` | 0 | **没有红灯层**——见下 |

**Task 44 与 Task 45 没有红灯层，这是事实而不是遗漏。** 44 的红灯本该是 `tsc -b` 报「`startTurn` / `turnEventsUrl` 不存在」，但 Task 43 的树上 `App.tsx` 还没有引用助手面板，所以类型检查全绿；45 只新增一个测试文件、不改任何实现，它在 Task 44 的树上直接就是绿的（实测 `6 passed`）。两处的"红灯"实际上是**前面十一个任务各自的红灯**——这两个任务是验收，不是驱动。假装有一个红灯出来只会让这张表失真。

### 每个任务的绿灯（各自 commit 上重跑）

| 任务 | 命令 | 结果 |
|---|---|---|
| 34 | `pytest apps/api/tests/unit/test_assistant_task_state.py` | `18 passed` |
| 35 | `pytest apps/api/tests/unit/test_assistant_ledger.py` | `15 passed` |
| 36 | `pytest apps/api/tests/unit/test_assistant_policy.py` | `16 passed` |
| 37 | `pytest apps/api/tests/unit/test_assistant_intent.py apps/api/tests/unit/test_assistant_guard.py` | `27 passed` |
| 38 | `pytest apps/api/tests/integration/test_assistant_repositories.py` | `12 passed` |
| 39 | `pytest apps/api/tests/integration/test_chat_client.py apps/api/tests/integration/test_turn_event_broker.py` | `20 passed` |
| 40 | `pytest apps/api/tests/integration/test_assistant_toolbox.py` | `16 passed` |
| 41 | `pytest apps/api/tests/integration/test_run_turn.py` | `21 passed` |
| 42 | `pytest apps/api/tests/contract/test_assistant_routes.py` | `13 passed` |
| 43 | `pytest apps/api/tests/contract/test_assistant_events.py` | `13 passed` |
| 44 | `cd apps/web && npx tsc -b && npx oxlint` | `Found 0 warnings and 0 errors.` |
| 45 | `pytest apps/api/tests/e2e/test_assistant_turn.py` | `6 passed` |

### 跨任务回归（终态树上重跑每个任务的绿灯命令）

| 任务 | 结果 |
|---|---|
| 34 | `18 passed` |
| 35 | `15 passed` |
| 36 | `16 passed` |
| 37 | `27 passed` |
| 38 | `12 passed` |
| 39 | `20 passed` |
| 40 | `16 passed` |
| 41 | `21 passed` |
| 42 | `14 passed` |
| 43 | `13 passed` |
| 44 | `Found 0 warnings and 0 errors.` |
| 45 | `6 passed` |
| 全部 | `make ci` → `5 kept, 0 broken` · `module-map: 0 个问题` · `capability-map: 0 个问题` · `openapi 契约一致` · `340 passed` · `coverage 97.22%` · 前端三道静默通过 · `ci: 全部通过` |

### 零成本算术不变量

S2 的四项对拍里，这一条抓出过 `21 ≠ 95 − 75`。S3 的对应检查：

| 任务 | 本任务门禁通过数 | 上一任务累计 | 差 | 本任务绿灯数 |
|---|---:|---:|---:|---:|
| 34 | 331 | 313（S2 终态） | 18 | 18 ✅ |
| 35 | 346 | 331 | 15 | 15 ✅ |
| 36 | 362 | 346 | 16 | 16 ✅ |
| 37 | 389 | 362 | 27 | 27 ✅ |
| 38 | 401 | 389 | 12 | 12 ✅ |
| 39 | 421 | 401 | 20 | 20 ✅ |
| 40 | 437 | 421 | 16 | 16 ✅ |
| 41 | 458 | 437 | 21 | 21 ✅ |
| 42 | 471 | 458 | 13 | 13 ✅ |
| 43 | 484 | 471 | 13 | 13 ✅ |
| 44 | 484 | 484 | 0 | 0（前端门禁不计入 pytest 条数） ✅ |
| 45 | 490 | 484 | 6 | 6 ✅ |

（"本任务门禁通过数" = 该任务结束时全量 `pytest` 的 passed 条数；差值必须恰好等于该任务新增测试文件的 passed 条数。）

### 环境事实（规划时实测）

```text
8100  llama-server --model gemma4-qat-iq3xxs --ctx-size 8192 --parallel 1 --reasoning off
      原生 tools / tool_calls：finish_reason="tool_calls"，arguments 是 JSON 字符串
      同一个 session 里 --parallel 1 意味着并发请求排队，测试里不要并发打它

chinese-clip 加载 3.1s；GPU 上单张图约 65ms（S2 已经记录过，本切片未重测）

Python 3.11.16 / torch 2.14.0+cu130 / qdrant-client 1.19.1
httpx 0.28.1（S3 复用它连 8100，没有新增依赖）

make ci 单次约 64–78s（其中 e2e 的 6 个助手用例要打十几次 8100，是主要变量）
  ⚠ 这个数字是机器负载的函数，**Expected 里不写耗时**。
```

### 生成侧对拍

S2 的教训是"代码与 Expected 的错误全部发生在计划生成这一步"。S3 的四项对拍结果：

| # | 对拍 | 做法 | 结果 |
|---|---|---|---|
| (a) | 每个代码块与该 commit 里文件的**逐字节 diff** | 文档里的代码块不是手抄的：`build_plan.py` 直接从跑通的终态树按文件整块展开，展开后再做一次「文档块 → commit 文件」的逐字节比对 | ✅ 见「生成侧四项对拍的输出」一节 |
| (b) | 该 commit 改动的文件集 **⊆** 该任务 `Files:` 清单，且各有一个块——**唯一能兜住"整步丢失"的一环** | `git show --name-only <commit>` 与文档 `Files:` 清单双向比对 | ✅ 抓出过一处真缺陷：`apps/api/tests/conftest.py` 的 `make_state` fixture 没有进任何一个任务的 `Files:`，于是 Task 35 在它自己的 commit 上**红灯**（`fixture 'make_state' not found`）。已把它补进 Task 34 并重建全部 commit |
| (c) | 文档行块（module-map / capability-map）**逐行 diff** | 两列依赖由脚本按真实 import 图重算，能力条目按序号切片展开 | ✅ 见「生成侧四项对拍的输出」 |
| (d) | 把每条 `Run:` **真跑一遍**再比 Expected | 见上面的红灯/绿灯/回归三张表；每条命令的输出都是直接抄下来的 | ✅ |

同一轮对拍还抓出第二处任务边界缺陷：`test_a_finished_turn_is_visible_in_the_conversation_detail` 放在 Task 42 的会话契约里，但它靠「把 SSE 流读完」来等待回合结束——Task 42 还没有 SSE 端点，于是它在**自己的 commit 上绿不了**。已挪到 Task 43。

第三处是顺序：Task 35 的测试 import 了 Task 36 才创建的 `evidence_ledger`，于是 Task 35 在自己的 commit 上收集失败。**两个任务对调**之后才对——先账本、后工具。

工具本身的输出（在副本上跑）：

```text
$ /usr/bin/python3 crosscheck.py s3-plan.md
对拍 (a)(b)(c)：0 个问题
```

```text
$ make ci
.venv/bin/ruff check apps/api/src apps/api/tests scripts
All checks passed!
.venv/bin/ruff format --check apps/api/src apps/api/tests scripts
114 files already formatted
.venv/bin/mypy
Success: no issues found in 74 source files
Contracts: 5 kept, 0 broken.
capability-map: 0 个问题
module-map: 0 个问题
openapi 契约一致
Required test coverage of 85.0% reached. Total coverage: 97.22%
340 passed, 2 warnings in 63.83s
cd apps/web && npx tsc -b && npx oxlint && npx vite build   （三道静默通过）
ci: 全部通过
```

> 覆盖率 97.22%、`340 passed` 都是这一版的实际数字；**耗时（`in 63.83s`）不在 Expected 里**，它是机器负载的函数。

### 遗留

- **代码在隔离副本里，没进真实仓库。** 执行阶段要按本计划逐任务真写一遍。副本路径 `/home/asus/s3verify/repo` 可以留着对照，但不要直接 `cp` 过去——照着计划走一遍才是这份文档的价值所在。
- **`get_person_profile` 在 S3 恒返回「查不到」。** 这不是占位符而是当前唯一诚实的结果（身份层是 S5）。S5 落地时替换 `infrastructure/assistant/identity.py` 里的 `NoIdentityDirectory` 即可，工具契约、账本与门禁一行都不用动。
- **`RetrieveVisual` 被 assistant 直接复用为具体类**（`application/assistant/tools.py` 的类型注解是 `RetrieveVisual` 而不是一个 Protocol）。同一层内依赖一个兄弟用例，比再抽一层端口更简单；等 S4 把六路召回拼起来、`search_memories` 不再只走视觉时，再决定要不要抽端口。
- **`_publish()` 在 `RunTurn` 里同时做「落库」与「推给订阅者」两件事。** 前者是 application 的职责（它拥有事务），后者只是转发。将来若要支持外部 pub/sub，改的是 `TurnEventBroker` 的实现，不是这里。
