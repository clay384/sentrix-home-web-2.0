# Sentrix Home Web 2.0 实施设计

日期：2026-09-21
状态：待评审

本文档是 2.0 的**实施设计**，取代并细化此前的两份文档：

- `docs/architecture/2026-09-21-sentrix-home-web-2-design.md`（架构原则，仍然有效）
- `docs/refactor/2026-09-21-sentrix-home-web-2-refactor-plan.md`（文件级计划，本文档对其中 8 处做了基于实测的修正）

修正的依据来自对 153 生产环境的实测勘察：代码静态可达性分析、90MB 生产访问日志（909,720 条请求）、25,112 条助手轨迹、59 张表的真实行数。

---

## 0. 已拍板决策

| # | 决策 | 依据 |
|---|---|---|
| 1 | **空库起步**，不迁移任何处理结果数据 | 用户决策：要能力不要数据 |
| 2 | 只做**真实在运行的**能力 + 产品硬约束需要的 | 日志 + 轨迹 + DB 行数实测 |
| 3 | 冻结行程/故事/反馈/关系假设/角色决策/实体合并/人物瞬间/洞察运行/maintenance/invites/主动建议/E2B | 零调用 + 零数据 + 未接线 |
| 4 | PhotoBench **数据复用、代码重构** | 用户澄清 |
| 5 | 推进方式：**纵向切片** | 用户决策 |
| 6 | 来源门禁改为**能力级映射**，不做逐行 | 逐行与重构目标冲突 |
| 7 | module-map **自动校验** | 用户决策 |
| 8 | `import-linter` **全量强制（S0 即启用）** | 用户决策（已修订，见 §6.2） |
| 9 | 地点：**纯离线**，区县(面) + 乡镇/村庄(点)，无 POI | 用户决策 + 数据源实测 |
| 10 | 地点别名拆成**数据文件**，只留通用中英桥接 | 发现 benchmark 泄漏 |
| 11 | 只支持 153，但**保留多机器探测接口** | 用户决策 |
| 12 | S0 顺手把 8091 拉回 **systemd 托管** | 当前是裸进程 |

---

## 1. 目标与范围

### 1.1 产品目标

本地优先的家庭记忆系统。把原始图片、音频、文本、视频转成**可回溯原始证据**的事件记忆、人物语义记忆和视觉记忆。保持 1.0 已确立的 6 条硬约束（证据链可回溯、导入源不得写入记忆、人脸簇只是候选身份、相册归属不是分组信号、查询必须返回可打开的原图、无证据时输出 gap 不编造）。

### 1.2 数据策略

**2.0 从空库开始，不迁移 1.0 的任何数据。**

不迁移清单（明确接受其代价）：

- 526 个已确认人脸簇、534 个已命名人物与家庭角色、355 条成员关系、18 条家庭关系
- 25,622 段对话历史（2026-08-11 ~ 09-17）
- 46,951 实体 / 176,243 实体属性 / 53,006 语义声明 / 116,255 关系
- 83,413 条向量、Qdrant 634 collections / 111,208 points、HNSW 索引
- 63GB 媒体 / 25,296 个文件的全部派生结果

**必须写明的代价**：2.0 上线初期是一台空机器。需要重新导入媒体、重新等待管线处理、重新做人脸簇确认。这个成本由使用者承担，不通过 ETL 规避。

**可再生的部分**（因此不值得迁移）：Qdrant 与 HNSW 重建约 1 分钟（4.2 万行）；重嵌 10,590 张图片在 3090 上约十几分钟；FTS / 词表重建用现成脚本。

### 1.3 能力范围

判定依据：生产入口 `backend/app.py` 的静态可达性 + 90MB 访问日志的真实写操作 + 25,112 条轨迹的工具调用 + 59 张表的实际行数。

#### A. 保留 —— 有真实调用

| 能力 | 证据 |
|---|---|
| 媒体导入（`/api/import`、服务器目录导入、批次管理） | 580 + 21 + 24 次 |
| Asset / Observation / 原图交付 | `assets/{id}/file` 5,379 次 |
| 助手 tool-loop | 9,927 次 turn |
| 六路检索 + 证据包 | 助手的检索地基 |
| 对话与结果集 | conversations 创建 30 / 删除 25 |
| MemorySpace 管理 | 创建 25 / 删除 5 |
| 模型 profile 绑定与切换 | 41 次 |
| 人脸检测 → 向量 → 聚类（导入自动跑） | 29,897 face_instances |
| 事件聚类与快照 | 5,142 事件 / 60,651 实体关联 |
| 实体 / 属性 / 语义声明 | 46,951 / 176,243 / 53,006 |
| 家庭关系图（runs / relationships / membership） | 7 + 1 + 1 次 |
| 健康与遥测 | 2,074 次 |
| PhotoBench | 活的，8771 |
| model-manager | 活的，8500 |

助手工具的**真实调用**（25,112 条轨迹全量）：

| 工具 | 调用次数 |
|---|---:|
| `search_memories` | 17,718 |
| `inspect_photo` | 10,721 |
| `read_photo_text` | 2,867 |
| `get_result_page` | 1,649 |
| `query_memory_facts` | 1,603 |
| `query_photo_people` | 887 |
| `search_conversation_history` | 217 |
| `get_core_memory` | 162 |
| `get_person_profile` | 108 |
| `get_original_photos` | 4 |

#### B. 保留 —— 产品硬约束需要（日志零调用，但被 benchmark 绕过）

日志里 `POST /api/face-clusters/{id}/confirm` **零次调用**，但原因是 PhotoBench 用 ground-truth manifest 走 `POST /api/people/seed-batch`（21 次）直接造人，**绕过了人工确认**。DB 里 526 个"已确认"簇全部来自 seed-batch，不是人点的。

因此下列能力**必须保留**，因为它们是产品硬约束（第 3 条：用户确认姓名之前，人脸聚类不得写成命名人物）：

- 人脸簇确认 / 拒绝 / 合并 / 拆分
- 人物命名与别名
- 关系确认

#### C. 保留但低频

`facts`（3 行）· `query_gaps`（180 行）· `person_appearance_evidence`（6 行）· 照片线程（4 行）· OCR 设置（GET 2,530 次、PUT 0 次，只读）· `person_insights` / 画像（profiles 333 / patterns 3,428 / person_event_memory 2,384 有数据，但 insight_runs 0 —— 保留只读展示，冻结生成流程）

#### D. 冻结 —— 不实现、不建表

| 冻结项 | 证据 |
|---|---|
| 行程 trips | 表 0 行；`db.py:3253 derive_trip_candidates` **无任何生产调用者**，只在 `test_entities.py` 跑；`trips/{id}/confirm\|reject` 零调用 |
| 故事 stories | 表 0 行；`POST/PATCH/DELETE /api/stories` 零调用 |
| 反馈 memory_feedback | 0 行 |
| 关系假设 relationship_hypotheses | 0 行；decision 端点零调用 |
| 人物角色假设 person_role_hypotheses | 0 行 |
| 实体合并候选 entity_merge_candidates | 0 行；confirm/reject 零调用 |
| 人物瞬间 person_moments | 0 行 |
| 洞察运行 person_insight_runs | 0 行 |
| 画像修订 person_portrait_revisions | 0 行 |
| 家庭范围组 / 关系阻断 | 各 0 行 |
| 邀请 invites | 0 行 |
| 主动建议 | 4 张相关表全 0 |
| `maintenance/*`（recheck / reindex-entities / summarize-events / entity-merge-candidates） | 零调用 |
| `agent_contracts.py`（392 行） | 静态不可达 |
| E2B 服务 | 进程不存在，8101 已被 bge-m3 sidecar 占用 |

冻结的能力必须有理由记录在 `docs/refactor/frozen.md`。

---

## 2. 架构

### 2.1 系统边界

```text
apps/web        → /api/v1 → apps/api
apps/photobench → /api/v1/evaluation → apps/api
1.0 的 8500（model-manager）→ managed model profiles（2.0 只通过 HTTP 使用，自己不建）

apps/api owns:       SQLite、媒体存储、后台作业、模型与向量适配
apps/photobench owns: 数据集、跑测结果、judge、评测编排
```

**主产品不得启动、停止或读取 PhotoBench。PhotoBench 不得读取主产品 Python 内部模块、SQLite 文件或日志**——只能使用版本化 HTTP 评测契约。

**2.0 不管理模型进程生命周期**。模型进程、锁、PID、显存采样、LoRA 加载逻辑都不进 2.0 的 API 进程。

**2.0 不建 model-manager**（S11/T1104 明文）：仓库没有 `services/model-manager/`，2.0 也没有任何进程起停它（`apps/api` 全仓零引用 `8500` / `model_manager` / `vllm_manager`）。模型 profile 的权威是 1.0 部署在 153 的 8500（`sentrix-vllm-api.service` --user），2.0 只在需要时**经 HTTP 消费**它：`apps/photobench` 的 `vllm_manager_url`（`application/config/service.py`）与 `vllm_targets.json` 的 `manager_url` 就是这条通路，保留。

### 2.2 目录结构

```text
sentrix-home-web2.0/
├── apps/
│   ├── api/src/sentrix/
│   │   ├── main.py                 应用装配 + v1 路由注册
│   │   ├── config.py               类型化配置（pydantic-settings）
│   │   ├── bootstrap/container.py  唯一依赖装配点
│   │   ├── api/
│   │   │   ├── routes/             每个文件只做 HTTP 映射
│   │   │   ├── schemas/            请求/响应 DTO
│   │   │   ├── errors.py           领域错误 → HTTP 映射
│   │   │   └── deps.py             依赖注入
│   │   ├── domain/                 零 IO，纯规则
│   │   │   ├── media/              Asset / Observation / 导入规则
│   │   │   ├── memories/           只留 events/
│   │   │   ├── identities/         faces / people / entities / portraits
│   │   │   ├── family/             关系 / 成员
│   │   │   ├── places/             地点精度分级模型
│   │   │   ├── retrieval/          QuerySpec / EvidencePacket / 排序规则
│   │   │   ├── assistant/          task_state / evidence_ledger / result_set /
│   │   │   │                       tool_policy / final_guard / emergency /
│   │   │   │                       answer_nucleus / intent / profile / …
│   │   │   └── runtime/            profile / 能力 / 遥测契约
│   │   ├── application/            用例，各自拥有事务
│   │   │   ├── media/              import / process_asset / video / audio
│   │   │   ├── memories/           summarize_events / manage_events
│   │   │   ├── identities/         detect_faces / manage_clusters / manage_people
│   │   │   ├── family/             manage_graph / run_analysis
│   │   │   ├── retrieval/          retrieve_evidence
│   │   │   └── assistant/          run_turn / manage_conversations
│   │   ├── infrastructure/         实现 domain port
│   │   │   ├── persistence/        SQLite 仓储 + migrations/
│   │   │   ├── media/              storage / exif / image_io / ocr / video / audio
│   │   │   ├── models/             vlm / embedding / face / asr 适配
│   │   │   ├── places/             pygeocn / geonames 适配
│   │   │   ├── vector/             qdrant / hnsw / indexes
│   │   │   ├── retrieval/          六路召回器
│   │   │   ├── runtime/            platform 探测（仅 HostPlatform 实现）
│   │   │   └── assistant/          planner / tool_registry / writer / judge
│   │   └── workers/                显式异步作业入口
│   ├── web/                        React + TypeScript + Vite
│   └── photobench/                 评测应用（后端 FastAPI，前端 React/TS）
├── packages/contracts/             openapi + 生成的 TS client
├── docs/{architecture,adr,runbooks,refactor}/
├── infra/
├── scripts/
└── tests/{unit,contract,integration,e2e}/
```

> **更正（S11/T1104）**：上表原列 `services/model-manager/`——**2.0 不建该目录**。依据 §2.1
> （`:139`/`:147`）：2.0 只通过 HTTP 使用 model-manager，且不管理模型进程生命周期。若补这个目录，
> 里面能放的（profile 生命周期、锁、PID、显存采样、LoRA 加载）恰好都是 §2.1 明令不进 2.0 的东西，
> 因此按「不建」处置。详见 `docs/refactor/s11-deviations.md`。

### 2.3 分层与依赖规则

由 `import-linter` 在 CI 强制，**S0 即启用**（原定 M1 结束才打开全量，已修订——理由见 §6.2）：

```
domain          → 只依赖 domain 自己
                  禁止 fastapi / sqlite3 / qdrant_client / httpx / uvicorn / PIL
                  禁止 os（比「禁 os.getenv」更宽：取环境变量本身就是设计要禁的模式；
                  纯类型用法走 collections.abc）
application     → 只依赖 domain；禁止 fastapi / sqlite3 / PIL / httpx
infrastructure  → 实现 domain 的 port；禁止被 domain 反向依赖，也禁止依赖任何上层
                  （api / application / bootstrap / main）
api             → 只依赖 application；禁止 SQL、文件系统、模型客户端（只查直接 import）
bootstrap       → 唯一允许组装 infrastructure 的地方
sentrix.main    → 进程入口，只向下调用；与 api / application / domain 同受 layers 链约束
```

契约共五条：`domain-purity`、`application-purity`、`api-transport-only`、
`infrastructure-purity`、`layers`（实际内容以 `pyproject.toml` 的 `[tool.importlinter]` 为准）。

例外通过 `ignore_imports` 声明，**每条必须带注释说明理由**。**注意：当前没有任何检查会统计例外条数**——`import-linter` 不提供该输出（本次修订前此处写的是「CI 打印例外条数作为腐化信号灯」，那是空头承诺）。若将来例外增多、需要把架构腐化变成可观测信号，应先实现统计，再写进文档。

### 2.4 相对原计划映射表的 8 处修正

**① assistant 域漏了 18 个生产模块、2,639 行**（最严重）

原计划给 assistant 映射 12 个文件。实测 `agent_runtime/` **生产可达 29 个模块 / 11,394 行**。漏掉且在生产路径上的：

| 模块 | 行数 |
|---|---:|
| `ocr_tool.py` | 638 |
| `answer_nucleus.py` | 332 |
| `completion.py` | 244 |
| `jit_prompt.py` | 214 |
| `semantic_slots.py` | 214 |
| `canonical_intent.py` | 148 |
| `requirement_completion.py` | 123 |
| `profile.py` | 102 |
| `intent.py` `capability.py` `guard_types.py` `budget_manager.py` `conversation_summary.py` `emergency.py` `evidence_contract.py` `planner_contracts.py` `runtime_contract.py` `time_context.py` | 576 |
| **小计** | **2,639** |

`emergency.py` 是 FinalGuard 失败后的兜底，`budget_manager.py` 管预算，均非可选。

**② memories 域收缩**：`{events,trips,facts,stories,feedback}.py` → 只留 `events.py`

**③ places 域新增**：`domain/places/` + `infrastructure/places/`，原计划没有

**④ models 适配不止 face**：需要 vlm / embedding / face / asr 四类（`model_clients.py` 1,938 行）+ ocr（`ocr_tool.py` 638 行）

**⑤ e2b-compat 不建**

**⑥ PhotoBench 是重构对象**：后端 7,751 行 stdlib 单文件 → FastAPI 拆分；前端 3,646 行 Vue 单文件 → React/TS

**⑦ retrieval 多两个模块**：`near_duplicate.py`(60) / `probes.py`(157) 生产可达

**⑧ `agent_contracts.py` 不搬**：392 行、静态不可达

---

## 3. 领域模型

### 3.1 数据表

1.0 的 59 张表 → 2.0 保留约 35 张、冻结约 24 张。

**保留**：`memory_spaces` `assets` `observations` `memory_vectors` `ingest_batches`；`events` `event_revisions` `event_observations` `event_participants` `event_entities`；`entities` `entity_revisions` `entity_properties` `entity_mentions` `entity_observations` `relationships`；`face_clusters` `face_instances` `face_prototypes` `person_appearance_evidence`；`family_person_memberships` `family_relationships` `family_analysis_runs` `family_person_portraits`；`semantic_profiles` `semantic_claims`；`person_event_memory` `person_patterns`；`agent_conversations` `agent_conversation_messages`(+FTS) `agent_trajectories` `agent_result_sets` `agent_photo_threads`；`agent_core_memory_cards` `agent_core_memory_items`；`observation_search_fts` `observation_search_terms`；`query_gaps` `runtime_settings` `rebuild_runs`

**冻结**：见 1.3-D 清单，另加遗留表 `persons`（语义已迁到 `entities`）。

### 3.2 三处结构改进

**① 新增 `schema_migrations`**

1.0 是 `CREATE TABLE IF NOT EXISTS` + 运行时 `_ensure_columns()` 用 `ALTER TABLE ADD COLUMN` 补列，59 张表没有版本号，另有 4 个一次性补丁迁移埋在代码里。2.0 使用带版本号的迁移文件，**禁止运行时补列**。

**② `metadata_json` 拆列**

1.0 把 EXIF / GPS / 反地理 / 各种派生数据全塞进一个大 JSON blob，检索只能 `LIKE '%…%'`。实测这种查询用不了索引，在 63GB 数据上是全表扫描。2.0 把需要检索的字段抽成正式列或独立表。

**③ 地点结构化**

1.0 把地点压成一个 `label` 字符串。实际存在两种不同性质的信息，必须分开建模：

```python
Place(
    admin:   AdminArea(province, city, district),      # 面，权威归属
    nearest: NearestPlace(township, locality, distance_m),  # 点，只能说"附近"
    precision: "district" | "township" | "locality",
    source:    "pygeocn" | "geonames",
)
```

区分依据：`admin` 来自边界数据，可以做硬条件过滤（"照片属于萧山区"）；`nearest` 来自最近邻点，只能说"在萧山区某村旁边"。混成一个字符串后，检索层无法区分两者。

展示形式：`浙江省杭州市萧山区 · 赵州镇大石桥村附近`

### 3.3 地点数据源（纯离线）

| 级别 | 数据源 | 条数 | 性质 |
|---|---|---:|---|
| province / city / district | PyGeoCN（内置天地图边界 GeoJSON，16MB） | — | 面，准确 |
| township（乡镇/街道） | GeoNames `ADM4` | 11,450 | 点，最近邻 |
| locality（村庄/居民点） | GeoNames `PPL` | 860,831 | 点，最近邻 |
| 国外 city / town / village | GeoNames `allCountries`（402MB） | — | 点，最近邻 |

实测（中国区 961,888 条）：

- `ADM4` 中 **11,450 / 11,878 = 96.4%** 中文名以 镇/乡/街道 结尾，确为乡镇级
- `PPL` 密度高：赵县城区 2km 内有 6 个村（大石桥村 0.27km、焦家庄村、固城村、瓜家庄村、傅家湾村、常家庄村）
- **POI 地标拿不到**："赵州桥" 在 961,888 条中 **0 命中**；全国 S 类（spot）仅 13,757 条，覆盖极稀

**明确接受的上限**：国内到「区县 + 乡镇/村庄」，国外到「城市/乡镇」，**永远拿不到"赵州桥景区"这类 POI 地标名**。

**实现**：导入时计算一次、结果落库，运行时零网络依赖。加载 12M 点建 KD-tree 索引，内存约 100–200MB，查询毫秒级。

### 3.4 地点别名的处理

1.0 的 `backend/geocoding.py` 末尾 `_PLACE_ALIASES` 硬编码了：

```python
"赵州桥": ["赵县"],
"三峡坝址": ["夷陵区"],
```

**这是 benchmark 泄漏**。证据：PhotoBench 的 QA 文件里有题「17年10月，我带我爸妈出去游玩拍合影的那次，是在河北哪个景点拍的呀？」，答案「是在河北赵县的赵州桥景区」；而 DB 里 **"赵州桥" 出现 0 次**，"赵县" 出现 27 次。系统只可能从 GPS 反编码得到"赵县"，永远不会产生"赵州桥"这个词——这条硬编码唯一的作用就是让那道评测题能匹配上。代码注释自称"这是通用双语地名知识，不是测评答案"，辩解本身即佐证。

**2.0 处理方式**：

- 删除评测地标条目
- 保留真正通用的中英地名桥接（清迈 ↔ Chiang Mai、行政区后缀归一），移到独立数据文件（如 `data/places/aliases.json`），可审查、可替换、可测试
- 该文件不得被 agent 提示词或评判逻辑引用

---

## 4. API 契约

### 4.1 原则

- 公开 API 只有 `/api/v1`；OpenAPI 是唯一真相，手写于 `packages/contracts/openapi/sentrix-v1.yaml`
- 路由实现必须匹配契约，CI 用 `openapi-diff` 拦截破坏性变更
- 前端只准使用生成的 TypeScript client，手写 `fetch` 由 ESLint 拦截

### 4.2 错误结构

所有端点返回统一结构：`{code, message, detail}`。领域错误在 `api/errors.py` 集中映射到 HTTP 状态。

### 4.3 SSE 事件契约

沿用 1.0 已验证的两类事件（`backend/app.py:2986`、`:2997`）：

- `progress`：`{text, status, stage, step_index, timestamp}`，`stage ∈ {thinking, planning, inspecting, action, tool_result, recovering, finalizing, terminal}`
- `complete`：`{type, turn_id, status, public_progress, result?, error?}`

**2.0 前端必须使用 SSE，不得轮询。** 1.0 里 `GET /api/assistant/turn/{id}` 被轮询 **668,490 次，占全部流量 73%**——SSE 明明实现了却没用。

---

## 5. 外部依赖与部署

### 5.1 端口

| 端口 | 归属 | 2.0 处理 |
|---|---|---|
| 8091 / 4174 / 8771 | 1.0 API / Web / PhotoBench | **保持运行**，M3 之前是权威 |
| **9598 / 4274 / 9597** | 2.0 API / Web / PhotoBench | 新增 |
| 8100 | llama-server（OpenAI 兼容 `/v1`） | 直接复用 |
| 8101 | bge-m3 sidecar（文本嵌入，`.venv-text`，CPU） | 直接复用 |
| 8500 | model-manager API | 通过 HTTP 使用 |
| 11434 | ollama（**别人的项目**） | 不碰 |

### 5.2 数据目录与锁

```text
sentrix-home-web2.0/data/
  sentrix.db      SQLite（独立）
  media/          2.0 重新导入
  qdrant/         必须独立
  ann/            独立
  geo/            GeoNames KD-tree 索引
```

**`qdrant/` 必须独立**：1.0 用 `QdrantClient(path=...)` 嵌入式模式，`data/qdrant/.sentrix-qdrant.lock` 被 8091 进程 **flock 独占**。2.0 若指向同一目录，启动即失败。这是原计划未点破的硬约束。

### 5.3 GPU

单张 RTX 3090，24GB。1.0 当前占 6.7GB（API 2.5GB + llama-server 4.2GB）。2.0 复用 8100/8101 不额外占显存，但自身进程内模型（chinese-clip / RetinaFace / buffalo_l / PaddleOCR / FunASR）会各占一份，**两套并行预计再吃 3–5GB**。里程碑验收需检查显存。

主机：60GB 内存（当前用 33GB），磁盘 1.8TB（已用 624GB，可用 1.1TB）。

### 5.4 需要申请/下载的外部资源

无需第三方 key。需要一次性下载：

- GeoNames `allCountries.zip`（402MB）—— 地点数据
- PyGeoCN 随 `.venv` 安装，内置 16MB 边界数据

---

## 6. 工程标准

### 6.1 工具链

- Python 3.11+、`pyproject.toml`、ruff（lint + format）、mypy strict（**整个 `sentrix` 包 + `scripts/`**；2026-09-21 校正，原写「仅 `domain/` + `application/`」——实测其余各层在 `--strict` 下本来就全绿，把已经干净的两层漏在门外没有收益）、pytest
- TypeScript strict、oxlint（落地时为 oxlint，原写 ESLint + Prettier）、Vite（**Vitest 尚未接入**：前端目前没有测试文件，§6.5 里的 `vitest` 一环暂无实现，接入前前端门禁只有类型、lint、构建三道）
- 统一入口（取自 `Makefile` 的 `.PHONY` 全集）：`make help / install / lint / format / format-check / typecheck / arch / test / test-unit / test-contract / test-integration / coverage / check-docs / web-check / ci / dev / web-dev`

### 6.2 分层强制

见 2.3。**S0 即启用**：S0 就立规则文件、`ignore_imports` 留空，并直接把 `arch` 放进 `make ci` 全量强制。

原定"分阶段启用"（S0 只立规则、M1 结束才强制），理由是"前期架构仍在试，规则需要跟着调"。**该前提已不成立**：终态实测 `5 kept, 0 broken`，代码本来就合规，强制不带来任何摩擦；而 S0–S1 恰恰是分层最容易被悄悄破坏、也最缺其它防线的阶段。故改为声明即启用。`ignore_imports` 仍留空。

**契约集在 S0 内补全过一次**（见 2.3）：补上 `os` / `qdrant_client`、新增 `infrastructure-purity`、把 `sentrix.main` 并入 `layers`。原先 `infrastructure` 与 `bootstrap` 不在任何契约里，等于两层无人约束——「只声明」的那一版并不是最终形态。

**门禁拆分（已结束）**：`coverage` 在 S1 Task 13–15 落地前必然红，当时的做法是 `ci`（不含 `coverage`）与 `ci-full`（含）并存，避免门禁长期恒红、被所有人学会无视。S1 Task 13 已合入，`domain/` + `application/` 有代码、覆盖率实测 95.6%（阈值 85%），**两者已并回一个 `ci`，`ci-full` 已删除**。

### 6.3 数据库迁移

带版本号的迁移文件 + `schema_migrations` 表记版本。禁止 `CREATE TABLE IF NOT EXISTS` 兜底与运行时 `ALTER TABLE` 补列。

### 6.4 测试分层

| 层 | 范围 | 门禁 |
|---|---|---|
| `tests/unit` | domain 纯逻辑，无 IO，毫秒级 | 覆盖率 **≥85%** |
| `tests/contract` | 每个 v1 端点 | 全绿 |
| `tests/integration` | 真 SQLite + 真 Qdrant（临时目录） | 全绿 |
| `tests/e2e` | 起真服务跑真流程 | 里程碑门禁 |

覆盖率门禁**只对 domain + application**，infrastructure 不设门槛——以此逼着把逻辑挪进 domain。

1.0 有 138 个后端测试文件 / 16,599 行（占后端 30%）。**不得整体搬迁**：需先筛出真实行为契约，剔除已烂掉的与测死代码的。

### 6.5 CI 门禁

`ruff` → `mypy` → `import-linter` → `openapi-diff`（`check-docs` 内）→ `pytest`（含覆盖率）→ 前端 `tsc -b + oxlint + vite build`（`web-check`）

**前端检查排在链尾**：后端检查更快、失败信息更直接，先跑后端能让多数问题在十几秒内暴露；前端构建最慢，放最后。`make ci` 遇错即停，链上任何一环红了整个门禁就是红的。

**前端的类型检查必须用 `tsc -b`，禁止 `tsc --noEmit`**。`apps/web/tsconfig.json` 是 `{"files": [], "references": [...]}` 结构，普通 `tsc` 不遍历 project references——`npx tsc --noEmit --listFiles` 实测命中 **0 个文件**、恒 exit 0。用 `--noEmit` 写成的门禁是在假装检查，本仓库禁止这种形式。

本地 pre-commit 只跑 ruff（check + format）与文档防腐两项，**不是**同一套：mypy / import-linter / 覆盖率 / 前端三件套都只在 `make ci` 上跑。提交前建议直接跑一遍 `make ci`。

### 6.6 协作

- trunk-based + 短分支，PR 必过 CI
- `CODEOWNERS` 按模块划分，每个切片（media / identities / retrieval / assistant / family / places / web / photobench）有 owner
- Conventional Commits
- `docs/adr/` 记决策，只增不改

---

## 7. 文档交付物

**原则：做成会自我校验的活文档，而不是写一次就烂掉的手册。** 这不是空话——1.0 的 `docs/PROJECT_MEMORY.md` 列了 16 个模块文件（`thin_agent.py` / `router.py` / `query_parser.py` / `answer_brief.py` / `answer_composer.py` / `response_plan.py` / `response_validator.py` / `response_writer.py` / `visible_evidence.py` / `memory_gate.py` / `task_contracts.py` / `advanced_memory_tools.py` / `claim_extractor.py` / `complex_answer.py` / `narrative_context.py` / `agent_annotations.py`），**实际一个都不存在**。

| 交付物 | 内容 | 防腐机制 |
|---|---|---|
| `docs/architecture/overview.md` | 整体架构：分层、依赖方向、数据流、外部依赖、端口 | 依赖图从 `import-linter` 配置生成 |
| `docs/architecture/module-map.md` | **每个文件的职责与关联**：路径 / 职责 / 层 / 依赖谁 / 被谁依赖 / 旧实现对应 | 脚本扫真实 import 生成依赖边，与文档不一致即报错 |
| `docs/architecture/data-model.md` | 表、关系、证据链 | 从迁移文件生成 |
| `docs/architecture/api.md` | v1 契约总览 | 从 OpenAPI 生成 |
| `docs/runbooks/*.md` | 启停、迁移、重建索引、故障处理 | — |
| `docs/adr/*.md` | 决策记录，不可改 | — |
| `docs/refactor/capability-map.jsonl` | 能力级来源映射 | 校验脚本 |
| `docs/refactor/frozen.md` | 冻结能力及理由 | — |

**module-map 的分阶段交付**：现在还没写代码，只能写到目录与模块级；**文件级内容随切片生长**——S0 落骨架和模板，每完成一条切片补全该切片的文件职责与依赖。

### capability-map.jsonl

替代原计划的逐行来源门禁。理由：逐行映射与"按商用标准重构"直接冲突——最该重写的四个文件（`db.py` 6,660 行 / `app.py` 3,915 行 / `runtime.py` 3,449 行 / `tools.py` 2,799 行）恰好最没法逐行对应，强推会退化成大量 `NEW-2.0` 标记。

一条记录 = 一个能力：

```json
{"capability": "视觉召回",
 "origin_kind": "migrated",
 "source_commit": "2d2cee7c",
 "source_path": "backend/retrieval/visual_ann.py",
 "source_symbol": "VisualAnnRetriever._retrieve_qdrant",
 "target_path": "apps/api/src/sentrix/infrastructure/retrieval/visual_ann.py",
 "verification": "tests/integration/retrieval/test_visual_ann.py",
 "status": "verified"}
```

校验脚本强制三件事：

1. 每个目标模块至少被一条记录覆盖
2. 每条记录指向的验证文件真实存在且通过
3. 冻结的能力在 `frozen.md` 中有理由

`origin_kind ∈ {migrated, adapted, NEW-2.0, frozen}`。

**`status` 的判据**：一个模块要进 2.0，至少满足「生产静态可达」**或**「有真实调用日志」**或**「有明确的产品硬约束理由」，三者皆不满足则冻结。

---

## 8. 切片与里程碑

每条切片必须"能真跑、能用眼睛验证"，跑不通不进下一条。

| # | 切片 | 内容 | 验收 |
|---|---|---|---|
| **S0** | 骨架 + 真相基线 | 仓库骨架（pyproject / main / container / config / 空 v1 路由 / SQLite 迁移 0001）；live/dead 判定落成 `capability-map.jsonl`；把 1.0 的行为常量抄成 `behaviour-baseline.md`（事件打分权重、人脸 0.30 阈值、融合权重、guard 规则、planner 预算）；**把 8091 拉回 systemd** | `uvicorn` 起得来，`GET /api/v1/health` 200 |
| **S1** | 媒体入库 | Asset / Observation + 文件存储 + EXIF + 预览 + 原图交付 | 导入一张照片，浏览器点开看到原图 |
| **S2** | 视觉记忆 | chinese-clip 图像向量 + Qdrant 写入 + `visual_ann` 召回 + 融合骨架 | 输入内容词，搜出正确照片 |
| **S3** | 助手闭环 | Conversation / TaskState / ResultSet + tool-loop + ToolPolicy + FinalGuard；先上 4 个工具；模型走 8100；**SSE 一次到位** | 问"给我看看在 XX 的照片"，返回可打开的图 |
| **S4** | 时间线 | 地点解析（PyGeoCN + GeoNames）+ 事件聚类 + FTS5 词法召回 + 元数据召回 | 导入整个相册，时间线按日期成型 |
| **S5** | 身份 | RetinaFace + 人脸向量 + 聚类 + **人脸簇审阅（确认/拒绝/合并/拆分）+ 人物命名** + 实体 | 导入后能聚类、能确认命名、能被助手查到 |
| **S6** | 检索补全 | 补齐六路召回 + 硬条件（时间/人物/地点）+ 证据包 + trace；加 `inspect_photo`(VLM) / `read_photo_text`(OCR) / `query_memory_facts` / `query_photo_people` | 证据包和 trace 能展开 |
| **S7** | 家庭关系 | family graph + runs + relationships + membership + 关系确认 | 能建关系，助手能引用 |
| **S8** | 音频与视频 | FunASR 音频、关键帧/场景切分、派生帧可回溯原视频秒数 | 导入视频，场景成型、能跳回原片 |
| **S9** | PhotoBench 后端重构 | 7,751 行 stdlib 单文件 → FastAPI 拆分；断 `sys.path` 硬耦合；修死端点循环；改指 `/api/v1` | 能对 2.0 跑完一轮评测 |
| **S10** | PhotoBench 前端重构 | 3,646 行 Vue 单文件 → React/TS | 评测控制台可用 |
| **S11** | 服务与部署 | model-manager 收敛、端口/进程方案、runbook | 主 API / Web / PhotoBench / model-manager 独立启停 |

**里程碑**：

- **M1 = S0–S3**：能导入、能搜、能问。第一个真正可用的 2.0。（`import-linter` 全量强制已在 S0 启用，不再等到此处）
- **M2 = S4–S7**：时间线 + 人物 + 家庭关系齐备，日常家庭使用成立
- **M3 = S8–S11**：全媒体类型 + 评测 + 独立部署，可谈替换 1.0

**回退线**：M3 之前 1.0 全程保持权威且运行。S0 必须先把 8091 拉回 systemd——它当前是 2026-09-18 手动起的裸进程（pid 4188847），systemd 里是 `inactive (dead)`，机器重启即失去生产环境。

---

## 9. 验证策略

### 9.1 每个切片的验证

每项都按「先写失败测试 → 跑确认失败 → 最小实现 → 跑目标测试 → 更新 capability-map → 提交」推进。自动验证只做语法、单元、契约、静态边界；**模型与真实功能验证由使用者负责**。

### 9.2 黄金样本

- `data/album3-v2-source/photos`（真实家庭照片，144MB）—— 主夹具
- `data/household-benchmark-source` —— 第二夹具
- PhotoBench `data/` 下 5 个 album 数据集（1.4GB）—— 评测夹具

### 9.3 行为基线

S0 从 1.0 抄出的行为常量必须**逐条可验证**。这些值只存在于 1.0 的代码里、无文档：

- 事件多信号打分：时间 / GPS 2km 衰减 / 视觉相似 / 人物
- 人脸聚类余弦阈值 0.30（`db.py:4815` 硬编码）、质量阈值 0.30
- 融合权重：visual 2.5 / lexical 1.0 / metadata 1.0 / entity 1.0 / text 0.5 / adjacency 0.5；RRF_K=60；anchor +1.0
- planner 预算：`goal_driven_candidate` 8 步 / 6 调用 / 180s
- FinalGuard 的 Truth Guard 规则集
- 六路召回器的 channel 配置（`configs/retrieval/defaults.json`；其中 adjacency 是 expander、默认关闭，`retrieve()` 恒返回空，由 Kernel 拿种子后再 `expand`）

---

## 10. 要修掉的 1.0 已知问题

以下问题在 2.0 **修复，不迁移**：

| 问题 | 证据 |
|---|---|
| 死端点 404 循环 | `benchmark_orchestrator.py:739` 调 `{sentrix_url}/api/arbiter/status`，该端点在主 API 上**从不存在**；158,555 次全部 404，占 8091 流量 17% |
| 前端轮询代替 SSE | `GET /api/assistant/turn/{id}` 轮询 668,490 次，占流量 73% |
| ↳ **实测更正（2026-10-07）** | **这 668k 次轮询的调用方不是浏览器，是评测机。** 2.0 前端已走 SSE（`apps/web/src/features/assistant/useTurnStream.ts` 用 `EventSource`，仅在 `onerror` 时回退轮询）；但 photobench 的 `wait_for_assistant_turn` 用 `time.sleep` 轮询（`EventSource` 对它不可用），所以轮询端点作为**公开契约被有意保留**（`apps/api/src/sentrix/api/routes/assistant.py:17-31` 的 docstring 记了这次改判）。佐证：那批轮询是十几个 turn_id 交替、无 UI 流量（`s3-deviations.md` §4 问 3）。**「前端用轮询」这一条已修复；「668k 轮询流量」没有被消除，因为它本来就不来自前端。** |
| 规划器调用失败 | `planner_call_error` 1,974 次 |
| ↳ **实测更正（2026-10-07）：本行「修复」的表述不成立。** 2.0 **移植并保留**了 `GoalPlanner`（`apps/api/src/sentrix/application/assistant/goal_planner.py:12` 自述「这是 1.0 的失败原因之一，2.0 保留它」），只是把它重新定性为**传输故障**、文案分岔到「模型服务不可用」。实跑仍出现：20261006-224324 那个 run 的 16 个 turn 里 `termination_reason` 分布为 `{answered:6, answered_with_gap:5, step_budget_exhausted:3, planner_blocked:2}`。**它是「保留」不是「修复」。** |
| 零工具调用 | 29%（7,389 / 25,112）的 turn 未调用任何工具 |
| ↳ **实测（2026-10-07）：部分修复。** 1.0 的结构性入口被堵死了 —— `domain/assistant/intent.py:41-44` 的 `MIN_REQUIREMENTS = 1`，注释直指「1.0 实测 29.3% 的回合零工具调用，JIT 提示词正是靠『当前没有待确认的证据需求』把它们放过去的」。但**结果仍在**：同一批实跑 16 个 turn 里 4 个零 `stage="action"`（25%），只是现在会被标成 `answered_with_gap` / `planner_blocked`。2.0 的 `TerminationReason.NO_TOOL_CALL_STOPPED` **全仓未被 raise**（只在 `emergency.py:72` 的文案映射表里），即没有专门拦截零工具调用的机制。 |
| 轨迹未正常收尾 | `termination_reason` 为 `None` 的有 11,304 条 |
| 模型请求失败 | `llama-server` 返回 `400 System message must be at the beginning` |
| 静默吞错 | `pipeline.py:358` 调用不存在的 `store.auto_confirm_clusters()`，被 `except Exception: pass` 吞掉，是恒空的空操作 |
| 死 env flag | 生产启动脚本设置的 `SENTRIX_THIN_AGENT_V1` / `SENTRIX_RX_V1` / `SENTRIX_CORE_MEMORY_V1` / `SENTRIX_EVIDENCE_RETRIEVAL_V1` / `SENTRIX_EVIDENCE_ANSWER_12B` / `SENTRIX_ANSWER_BRIEF_V1` / `SENTRIX_RESPONSE_PLAN_V1` / `SENTRIX_VISIBLE_EVIDENCE_V1` / `SENTRIX_RESPONSE_WRITER_V2` / `SENTRIX_RESPONSE_VALIDATOR_V1` 在业务代码里**零引用**（全项目 126 个 `SENTRIX_*` 变量，相当一部分是死的） |
| 陈旧启动参数 | `start.sh:34-35` 仍导出 `SENTRIX_TEXT_EMBEDDER=clip`，但嵌入方案已钉死 bge-m3 |
| 索引过期 | `data/ann/visual.hnsw` 只有 1,170 条，真实 10,590 条；仅因 Qdrant 优先才未暴露 |
| 日志不可用 | `logs/sentrix-api-8091.log` 90MB 无时间戳，无法做时间维度分析 |
| 遥测缺失 | `agent_trajectories.steps_json` 的 `detail` 恒为空对象，无法追溯工具调用 |
| registry 三处不一致 | **1.0 仓库** `configs/sentrix_vllm_registry_192_168_0_153.json`(13) / 部署 `/home/asus/sentrix-vllm/registry.json`(15，**权威**) / `scripts/runtime/start_sentrix_api_8091.sh` 指向 `…_local_100.json`（100 机器的）。2.0 仓库**没有** `configs/`、也不读任何 registry——权威与处置见 `docs/refactor/s11-deviations.md`（S11/T1104 已核实） |

---

## 11. 风险

| 风险 | 缓解 |
|---|---|
| 空库导致 2.0 上线初期不可用 | 已明确接受；M1 用真实照片验证，不用评测数据 |
| 重写丢失行为细节 | `behaviour-baseline.md` 逐条固化；黄金样本回归；1.0 全程可对跑 |
| GPU 争用 | 2.0 复用 8100/8101；里程碑验收检查显存 |
| 两套系统长期并行 | M3 定义替换判据；M3 前 1.0 是回退线 |
| `import-linter` 早期摩擦 | 原计划分阶段启用；实测代码本就合规，已改为 S0 即启用（无摩擦） |
| PhotoBench 前端重写工作量 | 内部工具，可排在 S10，不与产品切片争资源 |
