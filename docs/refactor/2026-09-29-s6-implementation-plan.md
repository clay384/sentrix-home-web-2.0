# S6 实施计划（草案 · 2026-09-29）

## 0. 本文档怎么来的，以及一次修正

### 0.1 第一版跑偏了，错在哪

我先做了一次覆盖审计（把 1.0 的 119 条路由按四域逐条对拍），然后**基于自己的审计结果**
写了一个「清理收口」的 S6。**而路线图里 S6 早有定义**——设计文档
`docs/architecture/2026-09-21-sentrix-home-web-2-implementation-design.md` §8「切片与里程碑」：

| # | 切片 | 内容 | 验收 |
|---|---|---|---|
| **S6** | **检索补全** | 补齐六路召回 + 硬条件（时间/人物/地点）+ 证据包 + trace；加 `inspect_photo`(VLM) / `read_photo_text`(OCR) / `query_memory_facts` / `query_photo_people` | 证据包和 trace 能展开 |
| S7 | 家庭关系 | family graph + runs + relationships + membership + 关系确认 | 能建关系，助手能引用 |
| S8 | 音频与视频 | FunASR 音频、关键帧/场景切分、派生帧可回溯原视频秒数 | 导入视频，场景成型、能跳回原片 |

S3 计划里也留了明确的交接（`2026-09-22-s3-implementation-plan.md:6145`）：

> 留给后续切片的：**双层防伪的语义评审层（S6）**、**恢复作答的四级阶梯（S6/S7）**、
> **六路取证的补齐（S6）**。

**错在哪**：我把「审计发现了什么」直接当成了「下一步该做什么」，中间少了一步
**查已定义的切片划分**。这个项目从 1.0 逐行移植、每一步都要有出处，而我在写计划这一步
偏偏没查出处。**审计的结论不是计划，是计划的输入。**

**保留的部分**：审计本身有效且已复核。它回答的是「1.0 与 2.0 的差在哪」，
而 S6 要回答的是「差集里哪一块是这一步该做的」——两者不是一回事，但前者是后者的前提。
§1 是前者的结论，§2 起是后者。

### 0.2 证据来源

- 1.0 的 119 条路由（`grep '@app\.\(get\|post\|put\|patch\|delete\)' backend/*.py`）按四域逐条对拍；
- 2.0 运行中实例 9598 的 `openapi.json`（39 条端点）；
- 结构实测：五条 import-linter 契约、`scripts/check_*.py` 的判定逻辑、迁移建表与
  `git grep` 的引用比对、线上库 `data/sentrix.db` 行数；
- 路线图：设计文档 §8、S3 计划的交接段、`frozen.md`、`capability-map.jsonl`。

每条结论带 `文件:行`，均可在当前工作树复核。

---

## 1. 覆盖审计的三个结论（Q1 / Q2 / Q3）

### 1.1 框架是否足够清晰？——**骨架清晰，但自检有一条粒度断层**

**正面（实测）**

| 项 | 实测值 |
|---|---|
| 五条分层契约 | 全 KEPT；**`ignore_imports` 例外 0 条** |
| 各层规模 | domain 6.6k / application 5.7k / infrastructure 8.0k / api 2.3k / **bootstrap 仅 258 行** |
| 应用层分解 | 一个用例一个文件、动词开头（`enroll_face` / `extract_moments` / `retrieve_visual`…） |
| 生产可达性 | `check_reachability.py` 报 0 问题 |
| 文档诚实度 | 设计 §2.3 自己删掉一个空头承诺（原写「CI 打印例外条数」，实为 import-linter 不提供该输出） |

**断层：`check_reachability.py` 的判定单位是模块，不是符号**

`scripts/check_reachability.py:178-192` 取 `name`（**模块名**）去问谁用过它：

```python
callers = {other for other in reachable
           if other != path and any(target == name and local in used[other]
                                    for local, target in bound[other].items())}
wired = bool(callers)
```

于是 `repositories/facts.py` 因为 `container.py` 构造了 `SqliteFactRepository` 就算「已接线」——
**它里面 `list_facts` 有没有人调，这个问题从没被问过。**

S4 栽过两次「代码在、没人调」，这条门禁把那个教训变成了可机械判定的东西。
**但它修的是模块级，同一个失效在方法级复发了**，而且三个域同时中招（见 §1.2 ①）。

**另外两条**

- **没有任何门禁盯 schema**：`ingest_batches` 表建在 `migrations/0002_media.sql:48`、
  `assets.batch_id` 列建在 `:9`，而全 `src/` 唯一提到它的是
  `application/media/import_album.py:18` 的一句**注释**；线上库该表 0 行、该列全 NULL。
- **`check_capability_map.py` 是单向校验**（已在 `s5-deviations.md` 第 41 条登记）：
  能抓「声称验过、其实没有」，**抓不到「其实验过、却标着 pending」**——
  `person_moments` 与 `person_portrait_revisions` 就卡在 `pending`，而 S5c 已交付它们
  （2026-09-29 实测：8 行瞬间 / 2 篇画像 / `model_name: qwen35-q4km`）。

### 1.2 1.0 是否已经完全实现？——**没有**

119 条路由的三分归属：

| 域 | 路由数 | 已实现 | 已冻结（有记录） | **缺失（从没被决定过）** |
|---|---:|---:|---:|---:|
| 媒体 / 导入 / 视频 / 观察 | 19 | 7 | 0 | **13** |
| 记忆 / 事实 / 实体 / 地点 | 15 | 3 | 0 | **12** |
| 身份 / 人物 / 关系 | 44 | ~22 | ~11 | **~11** |
| 助手 / 配置 / 运维 | 41 | ~27 | ~9 | **~13** |
| **合计** | **119** | **~59** | **~20** | **~49** |

**那 49 条之所以要紧，是因为它们从没被决定过要不要做。** 三个值得注意的形状：

**① 代码写好了、没人调**

| 位置 | 状态 |
|---|---|
| `repositories/facts.py:145 list_facts` / `:103 set_status` / `:113 supersede_others` | 全仓 **0 调用者** |
| `repositories/appearance.py:176 list_for`（外观证据读） | **0 调用者**（唯一使用点是 `observe_appearance.record` 的写） |
| `repositories/relationships.py:113 create` / `:182 confirm` / `:209 retract` | **0 调用者** |

其中 **facts 是一条功能缺陷，不只是缺接口**：写链路完整（`MaintainFacts` + `review_cluster` 的
家庭角色事实），表有 `status TEXT NOT NULL DEFAULT 'active'`（`0005_timeline.sql:94`），
`FactStatus` 四值齐全（`domain/memories/facts.py:38-41`）。但 `decide_fact_write` 在「值变了」时
会插一条 `pending`——**而它永远升不了级、删不掉、也看不见**。

> 精度声明：线上 `album1` 的 `facts` 表 **0 行**（该库导入于 S4/S5b 接线之前），
> 所以这是**代码可证、数据未显**的潜伏缺陷，不是观察到的现象。

**② 后端有、前端不接**：`GET /assistant/conversations/{id}` 返回 `messages` 与 `turns`
（`api/routes/assistant.py:137,149-159`），但前端 `shared/api/client.ts` **没有 `getConversation`**
（全前端 grep 零命中）。`AssistantPanel.tsx:39-43` 明写「重新进入时由上层决定要不要回放」，上层没做。
**结果：重进一个会话是空白。**

**③ 枚举有、生产者没有**：`TerminationReason.CANCELLED` 在 `run_turn.py:632` 做了映射，
但全仓**没有任何地方产生它**，`useTurnStream.ts:107` 对 `"cancelled"` 的判断因此是死代码。
回合跑在 daemon 线程（`assistant.py:225`），前端断开 SSE 只让事件生成器 `return`，
**worker 照跑到预算耗尽为止**。

### 1.3 缺的是否必要？——**按归属分，不按大小分**

第一版我按「必要性」分了 A/B/C/D 四类，这是错的分类轴——它把「该现在做」「该 S7 做」
「不建议做」混在了一格里。正确的轴是**归属**：

| 归属 | 内容 |
|---|---|
| **S6（检索补全）** | 六路里的 `text_ann` / `adjacency`、硬条件（时间/人物/地点）、证据包与 trace 可展开、语义评审层、恢复作答阶梯、四个工具 |
| **S6 的前置依赖** | `query_memory_facts` 依赖 facts 读路径（§1.2 ①）；`query_photo_people` 依赖人物检索（S5b 已给 `retrieve_entity`） |
| **S7（家庭关系）** | family-graph 7 条、relationships 5 条、`person_role_hypotheses`、`find_graph_violations`（`s5-deviations.md` 第 15/35 条已标「随 S7」） |
| **S8（音频与视频）** | 视频 4 条路由 + 整条抽取/场景链、FunASR |
| **S9/S10（PhotoBench）** | 评测后端/前端重构 |
| **S11（服务与部署）** | model-manager 收敛；**`hardware` / `model-profiles` / `runtime-providers` 9 条都归这里**——设计文档 §2.1 明写「2.0 不管理模型进程生命周期」，且 1.0 那 8 条本身只是 8500 的薄代理 |
| **S6（补，见 §3 第六组）** | **语义实体 / entity-groups / knowledge**——2026-09-29 裁决归 S6。它确实是检索补全的一部分：2.0 的 `retrieve_entity` 目前只服务人物，非人物语义实体是 entity 通道缺的那一半 |
| **S7（补）** | **恢复作答的四级阶梯**——2026-09-29 裁决归 S7（S3 计划原写「S6/S7」，现定 S7） |
| **未归属（需裁决）** | 鉴权；observations 原始列表；OCR settings / telemetry / query-gaps；`/api/dashboard`；**回合取消**（语义已裁决「丢弃部分结果」，但它不属检索补全，本切片不排）；**会话历史回放**（后端已有、前端不接，一行 client 函数的事，但同样不属 S6） |

---

## 2. S6 的真实起点

逐条对设计文档 §8 给 S6 的四项内容：

| S6 要求 | 2.0 现状 | 证据 |
|---|---|---|
| **六路召回补齐** | **5/6 才是真缺口**（2026-09-29 核查后修正）。缺 `text_ann`；**`adjacency` 不是缺口**——见下 | 1.0 有 `retrieval/{visual_ann,text_ann,lexical,metadata,entity,adjacency}.py` 六个，但 `configs/retrieval/defaults.json:10` 写着 `"adjacency": false`，而 `build_default_retrievers`（`retrieval/__init__.py:80`）对关掉的通道**直接 `continue`，连实例都不建**。`AdjacencyRetriever` 只被自己的模块、工厂和 `test_adjacency_retriever.py` 引用，`.expand(` 唯一的生产调用点在 Kernel 里——而 Kernel 只有在它被构建时才跑。**它在 1.0 是死代码**，与 `agent_contracts.py`（392 行、静态不可达 → 不搬）同一类。2.0 的 `retrieve_evidence.py:22` 早就把这件事写进了注释，而我在写第一版计划时没核到 |
| **`text_ann` 的真实体量** | **不是「加一路召回器」** | 1.0 的 `text_ann` 要：查询文本 → `TextQueryEmbedder` → semantic/episodic ANN → 把 observation 命中映回 Asset。而 2.0 **整条文本嵌入侧都不存在**：`scheme.py` 自己写着「TEXT 一侧（BAAI/bge-m3，1024 维，HTTP sidecar）留给 S4」而 S4 没做；`config.py:27` 的 `text_embedder_url` 全仓只有它自己那一行；线上库 `memory_vectors` **只有 `visual` 空间**（53 asset + 117 face_instance），一条文本向量都没有。所以这是一条**多段任务**：端口 + adapter + 写入 + 召回 + 存量回填 |
| **硬条件（时间/人物/地点）** | **0** | `grep -iE 'hard_filter\|time_range\|person_filter\|place_filter'` 在 `application/retrieval/` 与 `domain/retrieval/` **零命中** |
| **四个工具** | **4 个全缺** | 2.0 注册的是 `search_memories` / `get_result_page` / `get_original_photos` / `get_person_profile`（`domain/assistant/tools.py:54,81,98,121`）；`inspect_photo` / `read_photo_text` / `query_memory_facts` / `query_photo_people` 全仓零命中 |
| **证据包 + trace 能展开** | **部分** | `domain/assistant/evidence_ledger.py` 在，`application/retrieval/retrieve_evidence.py` 在（融合 + 缺口传递到 HTTP 响应）；**trace 的展开面未核** |
| 语义评审层（S3 交接） | **部分** | `domain/assistant/judge.py` 203 行 + `application/assistant/judge.py` 53 行；「双层防伪」的第二层缺否未核 |
| 恢复作答的四级阶梯（S3 交接） | **移出 S6 → S7**（2026-09-29 裁决） | 仍未核形态：1.0 有 `agent_runtime/runtime.py:93 _ocr_recovery_instruction`、`:274` `system_recovery`、`:436` 的确定性恢复；**「四级阶梯」在 1.0 里我没找到独立形态**（`grep '四级'` 只命中「不确定性用自然语言四级」，那是措辞分级，不是恢复阶梯）。S7 动工前必须先核，不能猜 |
| 语义实体 / entity-groups / knowledge | **0（并入 S6）** | 2026-09-29 裁决归 S6。`semantic_claims` / `semantic_profiles` 表在 2.0 的 7 个迁移里**根本不存在**；`repositories/entities.py:180` 的列表查询**硬编码 `entity_type = 'person'`**，非人物实体抽取（1.0 `db.py:3840-4032`）没移植 |

**1.0 的调用量给优先级**（25,112 条轨迹全量，设计文档 §1.3）：

| 工具 | 1.0 调用次数 |
|---|---:|
| `search_memories` | 17,718 |
| **`inspect_photo`** | **10,721** |
| **`read_photo_text`** | **2,867** |
| `get_result_page` | 1,649 |
| **`query_memory_facts`** | **1,603** |
| **`query_photo_people`** | **887** |
| `get_original_photos` | 4 |

**S6 要补的四个工具在 1.0 的真实调用量是 16,078 次**——不是锦上添花，是助手的主要取证手段。
没有它们，2.0 的助手只能靠 `search_memories` 一条腿走路。

---

## 3. S6 任务表

任务编号续前端（终于 112），从 113 起。

### 第一组 · 前置（不做，后面的工具接不上）

| Task | 内容 | 为什么是前置 |
|---|---|---|
| **113** | facts 读路径接线：`list_facts` 接上 + `GET /facts` | **`query_memory_facts` 没有它就没有数据源**。仓储方法已写好、零调用者，是「接线」不是「新做」 |
| **114** | facts 状态机闭合：`confirm` / `reject` 两条用例 + 路由，`set_status` / `supersede_others` 接线 | 只接读不接写，`pending` 仍然永久卡死；而 `query_memory_facts` 读到的会是永远升不了级的行 |

### 第二组 · 六路补齐

| Task | 内容 |
|---|---|
| **115a** | **文本嵌入侧**：`TextEmbedder` 端口 + bge-m3 HTTP adapter（8101 sidecar 已在跑、已实测 `{"status":"ok","model":"BAAI/bge-m3","dimension":1024}`）+ `scheme.py` 的 TEXT 常量（`scheme.py` 自己注明这一侧「留给 S4」而 S4 没做） |
| **115b** | **写入侧**：导入时把 observation 的语义文本写进 `semantic` 空间（1.0 `pipeline.upsert_vector` 的 `source_type=observation`） |
| **115c** | **召回器**：`RetrieveTextAnn`（1.0 `retrieval/text_ann.py`），把命中映回 Asset |
| **115d** | **存量回填**：线上 53 张 asset 已有 text 语义但没有文本向量。回填脚本（体例照 T85 那次人脸回填） |
| ~~116~~ | **不实现 `adjacency`**（2026-09-29 核查：1.0 默认关着，是死代码）。按 `agent_contracts.py` 的先例处置，理由记入 `frozen.md` |
| **117** | 通道补齐的**对照验收**：同一 query 在 `text_ann` 接上前后各跑一次，断言 `channels` 里出现了 `text_ann` 键且**名次不是垫底**（垫底意味着它其实没贡献） |

### 第三组 · 硬条件

**2026-09-29 核查后重写。** 原计划写成「时间 / 人物 / 地点三个独立任务」，那把机制看小了。
核 1.0 之后：**它们是同一件事——条件判定（condition pass）**，而 2.0 整个缺这一步。

1.0 有**两套**机制，别混：

| 机制 | 作用 | 谁在用 | 2.0 现状 |
|---|---|---|---|
| `HardFilterContext` → 各通道的**召回前筛** | 影响「召回什么」，不保证结果满足条件 | 只有 `metadata` 通道真用（`metadata.py:33` 的早退）；`visual_ann` / `lexical` / `entity` / `text_ann` 只从它取 scope | ✅ 已有（`EvidenceRequest` 的 time_bounds / place / person / media_types） |
| `spec.constraints` 里 `strictness == HARD` → **融合后过滤** | 保证「结果真的满足条件」 | `_evaluate`（`evidence_retrieval.py:414-425`），`_condition` 对 time / media / place / person 四维返回 matched/contradicted | ❌ **完全没有** |

`retrieve_evidence.py` 的模块 docstring 自己写着「S4 只搬前两步（召回 + 融合）。
后两步要种子分级与 `EvidencePacket`，属于 S6」——第三条就是**条件证据判定**。

| Task | 内容 |
|---|---|
| **118** | **条件判定的纯规则**：`_condition` 的四维（time / media / place / person）→ 领域层纯函数，各自返回 matched / contradicted / unknown |
| **119** | **融合后的排除**：HARD 约束不满足即排除（negated 反过来），并把 `excluded_count` 传出来——1.0 的 `packet.excluded_count` 就是这个 |
| **120** | **HTTP 面**：`/search` 接受 `time_range` / `person` / `place` / `media_type`，接进 `EvidenceRequest` |

**为什么原先拆成三个是错的**：三个条件走的是同一个判定与同一个排除动作，
拆开会让「排除逻辑」写三遍——而 1.0 那份是**一张表**（`_condition` 按 dimension 分派）。
按条件拆任务，等于把一张表切成三张。

**判据**：每个维度都要有「过滤前有、过滤后没有」的用例——那是这一条唯一的可见效果。
`person` 那一维特别注意：1.0 在人物不匹配时返回 **`unknown` 而不是 `contradicted`**
（`evidence_retrieval.py:479`），而 HARD 的判据是 `status != "matched"`——
所以 unknown 一样被排除。这个区别只在 `SEMANTIC` 档上才有意义（那里只排除 contradicted）。

### 第四组 · 四个工具

| Task | 内容 | 依赖 |
|---|---|---|
| **121** | **`query_memory_facts`——依赖核查后重估为一条子系统，不是一个工具**（2026-09-29 实测）。体量：`structured_memory.py` 311 行（`StructuredMemoryExecutor` + 16 个方法）+ 工具侧 `_query_memory_facts` 与四个 helper（`_draft_from_filters` / `_spec_for` / `_resolve_entity` / `_fact_samples`）约 120 行 + **`QuerySpec` / `Constraint` / draft 这套查询契约**（1.0 `query_contracts.py` 406 行，2.0 只移植了其中的 `parse_time_expression`）。三套枚举：`operation` {count, exists, first, last, group} × `subject` {asset, person_appearance, processing} × `filters` {time, media, place, person}。**建议拆成 121a 查询契约 / 121b executor / 121c 工具与注册**。前置 113/114（facts 读路径）仍然成立 |
| **122** | `query_photo_people`——**已核查，重估为「补两条读路径 + 接线」**（见 §4 Task 122）。`ResultSet.resolve(handle)` 与 scope 校验两段可直接照 1.0；缺的是「按 asset 取已绑定人脸」这一向，以及 `people_json` 里 `entity_id` 那一位（读取侧被丢掉了） | S5b 的 `retrieve_entity` + 新增两条读路径 |
| **123** | `inspect_photo`（VLM，1.0 调用 10,721 次）——**与 124 共用一层「拿问题问图」的 inspector 端口，那个端口 2.0 没有**（见 §4 Task 123/124）。**先落端口，再落本任务** | 8100 上的视觉端点（当前 qwen3.5-4B + mmproj，已实测可用） |
| **124** | `read_photo_text`——**已核查，且后端已裁决：只用 8100 的 VLM，零新依赖，工具描述如实收窄**（见 §4 Task 124）。2.0 一个 OCR 后端都没有，1.0 的 PaddleOCR 从来没加过。**与 123 共用 inspector 端口** | 同 123，先落 inspector 端口 |

> **T121b 是「重写」不是「移植」**（2026-09-29 二核）。`structured_memory.py` 里 5 个方法
> **直接拼 SQL**（`_base_query` / `_place_clause` / `_count` / `_group_by` / `_list`），
> 而那份 SQL 写死在 1.0 的 schema 上：
>
> | 1.0 | 2.0 |
> |---|---|
> | `json_extract(a.metadata_json, '$.reverse_geocode.{label,name,province,city,district}')` | **没有 `reverse_geocode`**——地点权威来源是 `asset_places` 表里的 `Place` |
> | `a.captured_location` 当文本 `LIKE` | `GpsPoint` 对象（`as_text()`） |
> | `a.derived_kind` | 待核 |
> | `store.connection` / `store.list_assets` | `Database.connect()` + 仓储 |
> | `geocoding.place_alias_names` | `PlaceAliases` |
>
> 而且按 2.0 的分层，**这些 SQL 该落进 infrastructure 的仓储**，不是 application 用例。
> 所以 T121b 的实际定义是：把 5 处 SQL 逐条翻译到 2.0 的 schema 并搬进仓储层，
> 其余 11 个方法（纯编排）才谈得上移植。**翻译 SQL 是静默出错的高发区**
> （`LIKE` 该不该是 `instr`、少了 `derived_kind` 过滤、alias 展开漏一支），
> 每条都要有「同一份数据、两条路径给出同一个数」的对照用例。

> **一条方法论，由两个正好相反的案例得出**（2026-09-29）：
>
> | 工具 | 注释说 | 代码说 | 真相 |
> |---|---|---|---|
> | `adjacency` | 配置里 `"adjacency": false` | 类存在、工厂会跳过它、`.expand(` 无生产调用者 | **是死的**——注释对 |
> | `query_memory_facts` | `tools.py` 模块 docstring：「**全量聚合工具已删除**」 | `tools.py:2690` **就注册着**；`runtime.py:604/654` 还在特判它；轨迹里 1,603 次调用 | **是活的**——注释过期 |
>
> **两次都不能只信一边。** 只信「1.0 有哪些 .py 文件」会把 adjacency 当缺口（第一版计划就这么错了）；
> 只信注释会把 query_memory_facts 当成已删。**判一个能力在不在，要看它有没有注册、有没有生产调用者**——
> 那正好就是 `check_reachability` / `check_method_reachability` 这两条门禁在 2.0 里问的问题。

### 第五组 · 收口

| Task | 内容 |
|---|---|
| ~~**125**~~ | ✅ **完成**（§8.9）——证据包 + trace 的可展开面，**§8 给 S6 的验收条件** |
| ~~**126**~~ | ✅ **完成**（§8.10）——语义评审层的第二层（「双层防伪」） |

### 第六组 · 语义实体（2026-09-29 裁决并入 S6）

非人物语义实体是 entity 通道缺的那一半：2.0 的 `retrieve_entity` 目前只服务人物
（`repositories/entities.py:180` 的列表查询**硬编码 `entity_type = 'person'`**）。
这一组做完，`GET /entities` / `entity-groups` / `knowledge` 三条 1.0 路由才算有落点。

| Task | 内容 | 动工前须核 |
|---|---|---|
| **127** | 迁移 0008：`semantic_claims` / `semantic_profiles` 表族 + 非人物实体抽取（1.0 `db.py:3840-4032`） | 1.0 那两张表的**实际列与被引用处**——它们 2.0 一张都没建，抄错列名要等到写入时才炸 |
| **128** | `GET /entities` + `GET /entities/{id}` + `PUT /entities/{id}/properties/{key}`（接 `repositories/entities.py` 的 `list_people` 放开 person 过滤、`set_property`/`maintain_property`/`list_properties` 三个零调用方法） | `maintain_entity_property_values`（多值合并）在 1.0 只被语义抽取调用，`s5-deviations.md` 第 46 条记过它「本切片没有调用者」——**这就是那个调用者** |
| **129** | `GET /entity-groups` + `/{id}`（1.0 是 `db.py:4194` 现算的投影，不落表） | 投影的**分组键**是什么 |
| **130** | `GET /knowledge`（1.0 返回 profiles + claims + spaces 三块） | — |
| **131** | 把非人物实体接通 `retrieve_entity`，让 entity 通道对语义实体也生效 | 与 119（人物硬条件）的边界：人物是硬条件，语义实体是召回通道，**两者在 1.0 里不是一回事** |

### Task 125 · 动工前核查（2026-09-30）：S6 的验收条件，差的是「每通道那一格」

设计 §8 给 S6 的验收条件只有一句：**「证据包和 trace 能展开」**。核完 1.0 之后，
那句话指的东西是具体的——`EvidencePacket.as_dict()`（`backend/evidence_retrieval.py:33-66`）：

| 1.0 的字段 | 是什么 | 2.0 现状 |
|---|---|---|
| `assets` / `exact_results` / `strong_results` / `approximate_results` | 四档候选 | **部分**：`retrieve_evidence.py` 有融合后的候选与 `channels` |
| `gaps` | 缺口（哪类证据没拿到） | **有**：`retrieve_evidence.py` 的缺口传递到 HTTP 响应 |
| `excluded_count` | 被硬条件排除的候选数 | **有**（T119 落的，`evaluate_conditions.py:67`） |
| `result_summary` | 四档计数 + `hard_constraint_violations` | **没有**（派生的，便宜） |
| **`channel_trace`** | **每通道的召回轨迹**：证明每个召回器**被调用过**，或者给出**明确的不可用原因** | ❌ **没有** |
| `retrieval_timing` | 每通道耗时 | ❌ 没有 |

**缺的那一格为什么是要紧的。** 1.0 那条注释写得比字段名清楚
（`evidence_retrieval.py:43-44`）：

> per-channel recall trace so API/benchmark can prove each retriever was invoked
> (or give an explicit unavailable reason)

2.0 现在只有**每候选**的 `channels`（`retrieve_evidence.py:109-113`：
「被哪些通道以第几名召回」）。那是**转置**过来的视角，少了一整类信息：

- `text_ann` 跑过但一张都没召回 → 响应里没有任何候选带 `text_ann` 键；
- `text_ann` **根本没跑**（模型不可用、向量库空）→ 响应里**也是**没有任何候选带那个键。

两者在读数上**完全一样**。而 §3 的 T117 对照验收要判的正是「`channels` 里出现了
`text_ann` 键且名次不是垫底」——**只有转置视角时，那条判据在「通道没跑」与
「通道跑了没找到」之间分不开**，而这正是 S6「六路取证补齐」要证明的东西。

**动工清单**：

| # | 落点 | 是什么 |
|---|---|---|
| 1 | `domain/retrieval/models.py` | `ChannelTrace`（通道名 / 是否被调用 / 候选数 / 不可用原因）与 `RetrievalTiming` |
| 2 | `application/retrieval/retrieve_evidence.py` | 六路各写一条 trace；**不可用也要写一条带原因的**（这是重点，不是可选项） |
| 3 | `api/schemas/search.py` + `api/routes/search.py` | 响应里多一块可展开的 `packet`（trace + timing + `result_summary`） |
| 4 | `apps/api/tests/` | 一条对照用例：**把某一通道弄成不可用，断言它在 trace 里仍有一条带原因的记录**——那才是「能展开」 |

**判据**：关掉 `text_ann` 的嵌入器之后，响应里那条 trace 必须仍然存在且写明原因；
而候选列表里不会因此多出任何东西。两半都要断言——只断言前一半的话，
一个「永远写六条、不管跑没跑」的实现也能过。

### Task 127 · 动工前核查（2026-09-30）：「非人物实体」到底是哪几个，以及一个必须定的口径

计划给 T127 的注记是「1.0 那两张表的**实际列与被引用处**——它们 2.0 一张都没建，
抄错列名要等到写入时才炸」。核完了，并且核出两件计划里没写的。

#### ① 两张表的列集（`CREATE TABLE` 即权威）

`_ensure_columns` 那一段（`db.py:1073-1084`）给这两张表补的列
（`scope_id` / `event_memory_json` / `patterns_json` / `confidence_source` /
`normalized_value_text` / `normalization_version` / `support_count`）
**已经在 `CREATE TABLE` 里了**——1.0 那是给老库打补丁用的。所以 2.0 的迁移
**可以一次建对**，那正是 2.0 迁移的既定风格（`0006_identity.sql:12` 写着同一句话）。

`semantic_profiles`（`db.py:811-826`）：
`id` / `scope_id` / **`person_id` UNIQUE REFERENCES entities(id)** / `summary_zh` /
`activity_summary_zh` / `place_summary_zh` / `appearance_summary_zh` /
`preference_summary_zh` / `event_memory_json` / `patterns_json` / `evidence_ids_json` /
`revision` / `created_at` / `updated_at`

`semantic_claims`（`db.py:827-849`）：
`id` / `scope_id` / **`person_id` REFERENCES entities(id)** / `dimension` / `predicate` /
`value_text` / `normalized_value_text` / `normalization_version` / `support_count` /
`value_entity_id REFERENCES entities(id)` / `valid_from` / `valid_to` /
`supporting_event_ids_json` / `evidence_ids_json` / `status` / `confidence` /
`confidence_source` / `supersedes_claim_id REFERENCES semantic_claims(id)` / `revision` /
`created_at` / `updated_at`；索引 `idx_semantic_claims_person ON semantic_claims(person_id)`

#### ② `person_id` 这个名字在两列上都撒谎——**第四次**同一形状

两列的约束都是 `NOT NULL REFERENCES entities(id)`，而 1.0 的抽取**是给非人物实体写
claim 与 profile 的**（下面 ③）。也就是说这一列装的是「这条 claim 关于哪个实体」，
与「人」无关。这与 `_confirmed_photo_identities`（名字说 confirmed，SQL 不查状态）
是同一类：**照搬列名（忠实），但把这件事写进 docstring**；改名是偏离，要单独有理由。

#### ③ 「非人物实体抽取」的具体形态：`_find_or_create_entities_batch`

`db.py:3708-3730`，四个 `entity_type`（`:3798-3803` 的 `values` 列表）：

| entity_type | 名字来自 | 摘要文案 |
|---|---|---|
| `place` | 观察的地点，或采集地点 | 由图片观察或采集地点维护 |
| `object` | 观察到的物体（`objects`） | 由图片观察到的物体 |
| `atmosphere` | 画面氛围（受控词表归一后） | 由图片观察到的画面氛围 |
| `time` | 原始拍摄日期 | 由原始拍摄时间维护 |

建完实体再逐条 `maintain_entity_property` 写属性：`geo` / `scene_type` /
`semantic_primary` / `semantic_details` / `visual_place_descriptions` /
`raw_atmosphere_labels` 等（`:3838-3934`）。

**这是一条生产路径。** 判据同 §3 那个方法论框：`create_entity(..., "place")` 这种写法
**只在 `test_entities.py` 里出现**（非测试调用点全是 `"person"`），真正建非人物实体的是
这个 batch API——所以「1.0 有没有非人物实体」不能靠 grep `create_entity` 判。

#### ④ 顺带确认了 T128 那条注记

`maintain_entity_property_values`（多值合并）**全仓非测试调用者只有 `db.py` 的观察抽取段**
（`:3854` / `:3859` / `:3907` / `:3922` / `:3923` / `:3934` / `:4031`）。
`s5-deviations.md` 第 46 条记的「本切片没有调用者」——**那个调用者就是它**，
T128 接上之后那个零调用登记可以删。

#### ⑤ 两件「不用改」，记下来免得动工时顺手加

- **泄漏守卫已经包含 `semantic_claims`。** 2.0 的 `final_guard.py:46` 的 `_TABLE_NAME`
  逐字抄了 1.0 的正则（`assets|observations|entity_mentions|semantic_claims|agent_result_sets`）。
- **`memory_feedback.target_claim_id` 那条外键不建。** 它引用 `semantic_claims`
  （`db.py:906`），但 `memory_feedback` 在 2.0 的冻结清单里（§范围决策「反馈」）。

#### ⑦ 迁移已落地；抽取那一段的体量与一处**跨切片**的牵连

**迁移 `0008_semantic_entities.sql` 已落**（两张表 + `idx_semantic_claims_person`，
列集照 ①、不搬 `_ensure_columns` 补丁、不建 `memory_feedback.target_claim_id`、
`person_id` 那个名字的实情写进注释）。

**抽取那一段（1.0 `db.py:3755-3954`，约 200 行）比计划那一行大得多，而且有三件计划里没写：**

**A. 它依赖语义受控词表，但三条腿有回退到 2.0 已有字段的路。** `maintain_observation_entities`
的输入是 `normalize_semantic_analysis(...)` 的 `semantic`（`place.primary` /
`objects[].label` / `atmosphere.labels`）——那是 **S4 明确没搬的 `detail` 族**
（`vlm_observer.py` 的模块注释：「1.0 的 semantic 受控词表……那是 S6 的精读」）。
但 1.0 自己的 `or` 链把三条腿接在了 2.0 有的字段上：

| entity_type | 1.0 首选来源 | **回退** | 2.0 有没有 |
|---|---|---|---|
| `place` | `semantic_place.primary` | `observation.place`（自由文本），再不行 `"其他或不确定"` | ✅ `Observation.place` |
| `object` | `semantic_objects[].label` | `observation.objects` | ✅ `Observation.objects` |
| `time` | — | `captured_at` 派生（date/year/month/season/part_of_day） | ✅ |
| `atmosphere` | `semantic_atmosphere.labels` | `extracted["emotions"]` 经 `normalize_mood` | ❌ **`emotions` 在 2.0 的 Observation 上不存在** |

所以**四类里三类现在就能做**，`atmosphere` 要先定「要不要把 `emotions` 也搬进来」——
而它是受控词表那一族的一部分。

**B. 它做七件事，不只是「建实体」。** 逐条列在实现清单里：批量 find-or-create 四类实体、
place 的五个属性（含 `geo` / `scene_type` / `semantic_primary` / `semantic_details` /
`visual_place_descriptions`）、**旧 place 投影的清理**（DELETE `entity_observations` 与
`event_entities` 上的旧行）、object 的 label/category、atmosphere 的 label/details、
time 的四个派生属性、`event_entities` 关联（每类一个中文关系名）。

**C. 它调 `create_relationship`——那是 S7 的表。** 最后一组循环给每个实体建两条关系：

```python
if place_entity and entity["id"] != place_entity["id"]:
    self.create_relationship(entity["id"], "出现在", place_entity["id"], ...)
if time_entity and entity["id"] != time_entity["id"]:
    self.create_relationship(entity["id"], "记录于", time_entity["id"], ...)
```

而 2.0 的 `relationships` 表与 `RelationshipRepository.create` 的归属是 **S7**
（设计 §8 的 S7 = family graph + relationships；`check_method_reachability` 里
`RelationshipRepository.list_person_relationships` 与 `retract` 现在登记的正是 S7）。
**所以 T127 照 1.0 全搬会牵进 S7 的地界**——这是动工前要定的第二件事：
要么 T127 先不做那两条关系（记成显式的后续），要么把关系表那一半提前。

**结论**：T127 拆成三段更妥当——`0008`（已落）、**四类实体与属性**（A/B，可做）、
**关系那两条**（C，与 S7 一起）。⑥ 那条口径核完判为「甲」。

#### ⑥ **一个必须先定的口径**：地点在 2.0 是 `asset_places`，在 1.0 是 entity

这是这一组真正的分岔，也是它体量的来源：

| | 1.0 | 2.0 现状 |
|---|---|---|
| 地点怎么存 | **entity**（`entity_type='place'`）+ 属性 + `semantic_claims` + 进 entity 召回通道 | **`asset_places` 表 + `Place` 值对象**（S4 的选择），**不是实体** |
| 物体 / 氛围 / 时间 | 同样是 entity | **没有**（只在 `Observation.objects` / `clothing` 里是文本） |

所以 T127–T131 有两种可能的口径，**体量差一个量级**：

- **甲：把四类都补成实体**（照 1.0）。那要与 2.0 既有的 `asset_places` 处理关系——
  并存（同一份地点两处存，靠什么保证一致？）还是替换（S4 的地点到 S6 全换掉，
  影响 `retrieve_metadata` / 时间线展示 / `Place` 的解析链）。
- **乙：只补 `object` / `atmosphere` / `time` 三类**，地点继续走 `asset_places`
  （2.0 的既有选择），entity 通道对地点不生效。

**这条口径核完了，结论是甲，而且理由比「照 1.0」更硬：两者不冲突，因为 1.0 两样都有。**

`asset_places`（`0005_timeline.sql:16-29`）的主键是 **`asset_id`**——**一行一张照片**，
存的是 GPS 反编码（`admin_*` 面 / `nearest_*` 点 / `precision` / `source`）。
1.0 的 place 实体是**跨照片的命名实体**（「家中餐厅」），带 `entity_observations`
关联（哪些观察提到它）与多值属性累积（`semantic_details` / `visual_place_descriptions`）。

两者回答的是**不同问题**：

| | 回答 | 来源 | 粒度 |
|---|---|---|---|
| `asset_places` | **这张照片在哪** | 照片的 GPS 反编码 | 一行一 asset |
| 1.0 的 place 实体 | **这个地方是哪儿、哪些照片拍到它** | 观察里的语义地点 | 跨照片 |

**而 1.0 两样都有**：前者是 `metadata_json` 的 `reverse_geocode`（§4 Task 121b 那张
对照表里列过它），后者是 `_find_or_create_entities_batch` 建的 place 实体。
2.0 把前者提升成了 `asset_places`（S4 的选择，做得比 1.0 好），**后者则整个没搬**。

所以补四类**不是新增一条 1.0 没有的路，是补回缺的那一半**，且与 `asset_places` 并存
正是 1.0 的形状。乙（只做 object/atmosphere/time）会让「家中餐厅出现在哪些照片里」
这类问题在 2.0 永远答不了，而 1.0 答得了。

### Task 128–131 · 动工前核查（2026-09-30）：三条实体路由的实际体量

T127 落了实体之后，这一组才有东西可服务。**核查的一条主结论：三个路由里两个是薄映射，
第三个不是**——`GET /entities/{id}` 落到 1.0 的 `get_entity_detail`，那是一个
**九个子查询的复合读**（`db.py:4570-4640`）。

#### 1.0 那三条路由（`app.py`）

| 路由 | 1.0 做了什么 |
|---|---|
| `GET /api/entities`（`:1858`） | `list_entities(status, scope_id)`；**默认滤掉 `entity_type == 'person'`**（`includePeople` 打开才给）；pending 的人物把名字换成「待命名成员」 |
| `GET /api/entities/{id}`（`:1948`） | `get_entity_detail`；没有就 404；pending 的人物换名字 |
| `PUT /api/entities/{id}/properties/{key}`（`:1959`） | `set_entity_property(id, key, value, evidence_ids)`；**`KeyError`→404、`ValueError`→400** |

#### `get_entity_detail` 的九个部分（`db.py:4570-4640`）

```
entity（本体）
├── avatar_face_instance_id   ← 该实体非 rejected 的簇里检测置信度最高的一张脸
├── clusters[]                ← 名下全部簇，每个再带 samples[]（最近 12 张脸的 id/bbox/文件名）
├── relationships[]           ← 关系（S7 的表）
├── facts[]                   ← subject 或 object 等于本名的那些事实
├── event_ids[]               ← 出现过的事件
├── observations[]            ← entity_observations ⋈ observations，最近 100
├── evidence_observations[]   ← 上一项再加工出的证据形态
└── …（还有两三个派生块）
```

**这是这一组唯一「重」的地方**，而且它跨了四个域：人脸（簇/实例）、事件、事实、
关系。按 2.0 的分层，这个复合读要么落成一个 application 用例（把四个仓储编排起来），
要么在路由层拼——**2.0 的既有先例是前者**（`read_timeline.py` 就是这么干的）。

**一处必须先定的口径**：`relationships[]` 那一段是 **S7 的表**。与 T127 那条裁决同形，
`GET /entities/{id}` 要么先不回关系（并在响应里如实不留那个键——2.0 的规矩是
「不留恒空的占位键」，见 `routes/insights.py` 的 module-map 条目），要么等 S7。

#### 2.0 已有的东西（核过，直接接）

| 需要 | 2.0 现状 |
|---|---|
| 按 status/scope 列实体（含非人物） | `EntityRepository.list_people` **硬编码 person**；要放开成 `list_entities` |
| 属性读 | `EntityRepository.list_properties` **在**（零调用，PENDING 登记的就是 T128） |
| 属性写（用户值） | `EntityRepository.set_property` **在**，而且**不是零调用**——`set_person_aliases`（`entities.py:747`）已经在用它。原表写的「零调用」是错的（2026-09-30 核查更正）；方法级可达性门禁当时也没报它，因为判据是「名字被属性访问过」，而那个访问就在同一个文件里 |
| 实体本体 | `EntityRepository.get` **在** |
| 人物详情页 | `GET /people/{person_id}` **已在 `routes/identity.py:306`** ——先读它，`GET /entities/{id}` 能复用的部分不要重写 |

#### 建议的拆法

| 任务 | 内容 | 体量 |
|---|---|---|
| **128a** | `GET /entities` + `PUT /entities/{id}/properties/{key}`（放开 `list_people` 成 `list_entities`、接 `list_properties`/`set_property`） | 薄映射，与 T122 同形 |
| **128b** | `GET /entities/{id}`：先读 `GET /people/{person_id}` 与 `read_timeline` 的编排先例，再定**哪几块搬、哪几块留 S7** | 一个 application 用例 |
| **129/130** | `entity-groups` / `knowledge`：1.0 是 `db.py:4194` 现算的投影（不落表）。**动工前要核那个投影的分组键**，以及 `knowledge` 三块（profiles + claims + spaces）各来自哪 | 未核 |

#### 128a 的第二次核查（2026-09-30）：装饰不是可选项

上表把 128a 判成「薄映射」。**核查推翻了它的一半**：`GET /entities` 不是，`PUT` 是。

**`GET /entities` 落在 `list_entities`（1.0 `db.py:4041-4055`）的完整实现上，它有三段**：

| 段 | 1.0 | 体量 |
|---|---|---|
| 1 | `SELECT * FROM entities` 的 WHERE 三段 | 薄 |
| 2 | `_decorate_entities`（`db.py:4056-4141`） | **7 条批量聚合，补 11 个派生字段** |
| 3 | `public_entity`（`db.py:4674-4685`，由 `db.py:4055` 逐条调用） | 仅 `place`：读 `private_flag`/`alias` 两个属性，追加 `private`，并可按 `alias` 覆盖 `canonical_name` |

**第 2 段是承重的，证据是 1.0 前端自己读它**：

| 字段 | 1.0 前端的读取点 |
|---|---|
| `reviewable` | `src/app.js:675`——实体总览网格的**入口过滤谓词**：`state.entities.filter(e => e.status === "confirmed" \|\| e.reviewable !== false)` |
| `avatar_face_instance_id` | `app.js:678` 实体卡头像；`:874` 人物卡；`:955`、`:1002`、`:1085` |
| `mention_count` `cluster_count` `relationship_count` | `app.js:678` 实体卡的三条统计 |
| `evidence_count` | `app.js:876`（`> 0` 过滤）、`:910` 事件实体行、`:929` 归并弹窗 |
| `photo_count` | `app.js:939` 批量确认、`:2112` 批量确认的排序与筛选 |
| `single_sample` | `app.js:2112` 同上 |
| `preview_asset_id` `preview_media_type` | ⚠️ **只在 `entityGroups` 上被读到**（`app.js:785-786` 的 `group.preview_asset_id`），那是 T129 的投影。实体列表上**产出了但前端当前没有读者**——如实记下，不要为了「有人用」而把它说成承重 |

`GET /api/entities` 在前端的消费点是 `state.entities`（`app.js:1191`，来自 `calls[6]`，
请求写在 `src/api.js:42`），服务 `knowledgeView()`（`app.js:673`，「语义记忆 / 实体治理」）
的实体总览网格、归并弹窗（`:928`）与建立关系时的目标下拉（`:958`）。

**一处留 S7**：`relationship_count` 来自 `relationships` 表，而它的读路径是 S7（与 T127
那两条关系同形）。2.0 的规矩是「不留恒空的占位键」（`routes/insights.py` 的 module-map 条目），
所以**这个键不出现在响应里**，而不是恒返回 0——恒 0 会让「S7 没做」读起来像「这个实体没关系」。

**`PUT` 那一半确实是薄映射，而且仓储已经完整**：2.0 的 `set_property`
（`entities.py:604-649`）与 1.0 的 `set_entity_property`（`db.py:4746-4774`）逐步对应——
supersede 旧行 → 插新行（`source='user'`、`confidence=1`、`status='active'`、
`supersedes_property_id`、`revision+1`）→ 追一条修订 → 返回新行。连两个错误分支的字面量
都一致：`_require_entity` 抛 `KeyError(entity_id)`（`entities.py:718`）、
`_clean_key` 抛 `ValueError("property_key is required")`（`entities.py:725`）。
1.0 的 route 映成 404 `detail="entity not found"` / 400 `detail=str(error)`（`app.py:1963-1966`）；
2.0 用 `NotFoundError` / `InvalidRequestError`——响应体的 `{code, message, detail}` 形状
是全 v1 统一的既有偏离（`api/errors.py` 的模块 docstring），不是这次引入的。

**`list_people` → `list_entities` 是恢复 1.0 的形状，不是新增设计**：1.0 只有一个
`list_entities`，person 过滤在**路由层**做（`app.py:1514` 的 `/api/people`、`:1862` 的
`/api/entities`）。2.0 的 `list_people` 把类型绑死进 SQL（`entities.py:181` 的
`entity_type = ?` + `ENTITY_TYPE_PERSON`），那是 S5 只为人物列表服务时的收窄。
两个路由各自在 Python 里过滤即可。影响面：端口 `ports.py:342`、实现 `entities.py:169`、
路由 `identity.py:297-303`、测试 `test_import_memory_space.py:171,188`，
外加 `spaces/models.py:6`、`import_assets.py:17`、`media.py:132` 等处的 docstring 措辞。

**两条 1.0 的事实，记下来免得被当成契约搬过去**：

1. `/api/entities` 的 `includePeople` 默认 `false`，但 1.0 前端**永远传 `true`**
   （`src/api.js:42` 把它写死在函数体里）。那个默认值在 1.0 自己的 UI 上是死路径。
   **默认值照搬**（它是 API 契约），但不要为它编造消费场景。
2. `app.js:695` 的 `if (ent?.is_self) return` 是**死检查**：`entities` 表没有 `is_self` 列
   （`SELECT *` 的列里没有，`_decorate_entities` 也不补），这个 `if` 永远不成立。
   **别把 `is_self` 当字段移植进 2.0。**

#### 128a 完成（2026-09-30）：拆成两半，各自一次提交

| | 内容 | 提交 |
|---|---|---|
| **128a（一）** | `list_people` → `list_entities`（恢复 1.0 形状，person 过滤回到路由层）+ `PUT /entities/{id}/properties/{key}` | `267124a` |
| **128a（二）** | `_decorate_entities` 的六条批量聚合 + `public_entity` 私密投影 + `GET /entities` | 本提交 |

**与上面「建议的拆法」的差别**：那张表把 `GET /entities` 与 `PUT` 放在一起、把装饰留给
128b。核查之后反过来了——`PUT` 才是真正薄的那半（仓储 `set_property` 早就写完且与 1.0
逐步对应），而 `GET /entities` 的体量全在装饰上。所以按**薄 / 厚**切，不按端点切。

**落点**：装饰的**判断**在 `domain/identity/decoration.py`（纯函数，边界可穷举），
**取数**在 `EntityRepository.decorate_entities`（六条批量聚合，逐条对 1.0），
**读侧拼装**在路由层——与 `routes/identity.py` 既有的簇列表同一条路（该文件模块 docstring
写着这是有意为之，不额外发明一层）。

**两处留白，都不是省略**：

- **`relationship_count` 不出现在响应里**（`relationships` 的读路径是 S7）。恒 0 会让
  「S7 还没做」读起来像「这个实体确实没有关系」。有一条用例专门钉「这个键不在」。
- **`/entities/{id}` 仍然没有**（那是 128b，九个子查询的复合读）。

**顺带补的一处**：`_person_payload` 原本只做了 1.0 那个改写块四条里的三条
（`canonical_name` / `family_role` / `confirmed`），漏了 `summary`。1.0 里它们是相邻三行
（`app.py:1518-1521`），所以这是**同一个块移植得不完整**，不是独立的设计问题。
不补的话，本次反而会造出 `/entities` 与 `/people` 对同一个 pending 人物给出两句不同的说明。
补了，并有一条用例专门钉住两个端点给同一句话——**只改一处就能让它们分叉，而两边各自的
用例都不会红**，那条用例就是为这个存在的。

**一处发现但没动的差异**：1.0 的 `/api/people` 给 pending 人物的是**带序号**的
`待命名成员 {n}`（`app.py:1517-1519`，按列表位置递增），2.0 用的是不带序号的
`PENDING_CLUSTER_NAME`。序号是**列表位置**的函数，放进 `_person_payload` 意味着那个函数
要知道自己的下标——是个需要单独裁决的形状问题，记在 `behaviour-baseline.md` 里，本次不动。

**定点变异 24 条全部被杀**（128a 两批：一 6 条、二 18 条）。其中两处值得留档：

1. **`preview` 的 `o.id DESC` 次序那条用例，第一版是恒真的。** 我写的两条观察指向
   **同一个 asset**，所以去掉 id 次序也照样给出同一个 `asset_id`。变异测试把它抓了出来，
   改成指向不同 asset 之后才真正验得出来。
2. **变异脚本本身出过一次假结论。** 没关字节码缓存时，两个变异改的行不同、但产物**字节数
   相同**，写入落在同一秒的话 Python 会复用上一条的 `.pyc`——于是那一条跑的是上一条的
   变异体，把真变异体判成了「存活」。加 `PYTHONDONTWRITEBYTECODE=1` 重跑，18/18 全红。
   **教训**：变异脚本必须关字节码缓存，否则「存活」这个结论本身就不可信；反过来，
   它也可能把该红的判成红。

**门禁自己删掉的一条登记**：`EntityRepository.list_properties` 在 128a 之前挂在
`PENDING_METHODS` 上（登记理由是「等 T128」）。私密地点的投影要读实体的 `private_flag` /
`alias` 两个属性，接上的那一刻反向规则就报红要求删——与 facts 那三条、`maintain_property`
走的是同一条路。**又一条被门禁自己删掉的登记。**

#### 128b 动工前核查（2026-09-30）：不是九个子查询，是十二个键

上面写「`get_entity_detail` 是一个**九个子查询**的复合读」。**这个数不对**，实测：

- **返回 dict 有 12 个键**：`entity` / `properties` / `property_history` / `clusters` /
  `relationships` / `facts` / `profile` / `claims` / `appearance_evidence` / `events` /
  `observations` / `evidence_files`（`db.py:4614-4627`）。
- 函数体内**直接写出的 SQL 只有 4 条**（avatar `:4575`、clusters `:4580`、
  每簇 samples `:4583`、observations `:4591`），其余全走 helper。按「逻辑上的一次取数」
  数是 **13 项**。
- **不存在实体时返回 `None`**（`:4571-4573`），不是空 dict。

路由 `GET /api/entities/{entity_id}`（`app.py:1948-1956`）只做两件事：`None` → 404
`entity not found`；pending 的 person **只换 `canonical_name`**——注意它**没有**同时改
`family_role` 与 `summary`，而同仓库的 `/api/people/{id}/profile`（`:1545-1549`）与
`/evidence`（`:1561-1565`）两处都改了。**三处不一致是 1.0 的原样。**

##### 十二个键 × 2.0 现状 × 本次搬不搬

| 键 | 2.0 现状 | 决定 |
|---|---|---|
| `entity` | `EntityRepository.get` + `decorate_entities`（T128a 刚落的） | **搬**，含 `avatar_face_instance_id` 与 `evidence_count` 两个就地加的键 |
| `properties` | `list_properties(entity_id)` | **搬** |
| `property_history` | `list_properties(..., include_history=True)` | **搬**（1.0 是同一次查询的两种参数） |
| `clusters[]` + `samples[]` | `list_clusters_for_entity` + 逐簇 `list_instances` | **搬**。`samples` 是 N+1（1.0 `:4582-4586` 同样是 N+1，每簇 `LIMIT 12`） |
| `relationships[]` | **S7 的表** | **不搬**，键不进响应（与 T127 那两条关系同形） |
| `facts[]` | `FactRepository.list_facts` 只有 scope 级 | **搬**，新增按名字读（见下） |
| `events[]` | `event_ids` **在**，事件本体要逐个 `get` | **搬**，N+1 与 1.0 `:4624` 同形 |
| `observations[]` | **缺「按实体查观察」的读路径** | **搬**，新增（见下） |
| `evidence_files[]` | 派生（observations + appearances 的 id → {asset_id, file_name, kind}） | **搬** |
| `appearance_evidence[]` | `AppearanceRepository.list_for(person_id, *, include_empty, limit=500)` **在**，默认值 / 排序 / WHERE 与 1.0 `db.py:4535-4546` 逐一对应 | **搬** |
| `profile` | `semantic_profiles` **零读路径**（全仓无 Python 引用） | **不搬** → **T130**（`GET /knowledge` 就是 profiles + claims + spaces） |
| `claims` | `semantic_claims` 同上 | **不搬** → **T130** |

##### 两条要新增的读路径

1. **`facts[]`**：1.0 是拉 `list_facts(1000)`（**全库、无 scope 过滤**）再在 Python 里按
   **`canonical_name` 字符串**匹配 `subject` 或 `object`（`:4588`）。2.0 的 `facts` 表
   `subject`/`object` 存的也是**名字不是 id**（`entities.py` 删实体时同样按名字删），
   所以这条按名字查是**照搬**，不是将就。
2. **`observations[]`**：1.0 是 `entity_observations ⋈ observations`，
   `ORDER BY o.captured_at DESC LIMIT 100`（`:4590-4594`），然后逐条 `get_observation` +
   `get_asset`（`:4595-4600`，又一层 N+1，把 asset 整行挂在 `observation["asset"]` 上）。
   2.0 的 `ObservationRepository` 没有 by-entity，`entity_observations` 只有计数与
   「取最新一张预览」两处读。**落点选 `EntityRepository`**——那张表的写入者本来就是它
   （`link_observation`），而与 observations / assets 的 JOIN 在 2.0 已有先例
   （`decorate_entities` 的预览查询就是这三个表的 JOIN）。

##### 落点：一个 application 用例，照 `read_timeline.py` 的先例

四个域的仓储在用例层拼成一个结果，返回模块内的 frozen dataclass，HTTP 映射留在路由
（`application/memories/read_timeline.py` 就是这么组织的：关键字注入 4 个仓储、
返回 `TimelineResult`，路由只做 pydantic 转换）。**不在路由里拼**——这次跨了四个域，
与簇列表那种「把记录字段搬进响应模型」不是一回事。

##### 两处对不齐、如实记下的 1.0 行为

- **`relationships` 与 `facts` 都没有状态过滤。** `list_relationships`（`db.py:5590-5597`）
  连 `retracted` 都返回，`facts` 那块也没有 status 条件。2.0 的
  `RelationshipRepository.list_for_entity` 同样没有过滤——**这是巧合地对齐**，
  不是 2.0 修过。而 `properties` 是**有**过滤的（`status IN ('active','pending')`）。
  三处不一致，都是 1.0 的原样。
- **1.0 前端把二者拼在一起渲染**（`src/app.js:1031`：
  `detail.relationships.concat(detail.facts.map(...))`），所以 2.0 少了 `relationships`
  之后，那个弹窗的「关联事件与关系」一栏只剩事实。**如实反映，不补占位。**

#### T129 / T130 动工前核查（2026-09-30）：分组键是现算的四级策略

##### T129 · `entity-groups`

1.0 有两条路由（`app.py:1871-1882`），且**都是现算的投影，不落表**：
`list_semantic_entity_groups`（`db.py:4194`）与 `get_semantic_entity_group`（`db.py:4249`）。

**分组键 = `(scope_id 或 "home-default", entity_type, label)`**，`label` 由
`_semantic_entity_key`（`db.py:4144-4165`）现算，**四级策略按顺序短路**：

| 顺序 | strategy | 依据 |
|---|---|---|
| 1 | `semantic_primary` | 实体属性 `semantic_primary` 有值，且类型 ∈ {place, object, atmosphere} |
| 2 | `controlled_equivalence` | `SEMANTIC_ENTITY_EQUIVALENTS[类型]`，先精确匹配 label 再子串匹配 |
| 3 | `semantic_concept` | place 查 `SEMANTIC_PLACE_CONCEPTS`（9 条）、object 查 `SEMANTIC_OBJECT_CONCEPTS`（10 条），子串命中即取归一化词 |
| 4 | `exact_label` | 兜底，label = 去空白后的 `canonical_name` |

**要搬四张词表**（`db.py:206-247`）：`SEMANTIC_ENTITY_EQUIVALENTS`、`MOOD_NORMALIZATION`
（`emotion` 那一支引用的就是它）、`SEMANTIC_PLACE_CONCEPTS`、`SEMANTIC_OBJECT_CONCEPTS`。
都是逐字常量，属 T127 `extraction.py` 那类「纯规则放 domain」。

**两处过滤要注意，一处容易漏**：

- 源是 `list_entities(scope_id, public=False)`——**`public=False`**，所以私密地点的投影
  **不在这一层**做；但组内成员是逐条走 `public_entity` 的（`db.py:4226`）。
- 循环里**跳过 `person`**、以及 `status ∈ {rejected, superseded}`（SQL 层已经排过一遍，
  这里是第二道）。
- `emotion` → `atmosphere` 的归一只发生在**组的 key** 上（`:4199`）；传给
  `_semantic_entity_key` 的仍是原始 `"emotion"`，所以 `SEMANTIC_ENTITY_EQUIVALENTS["emotion"]`
  会被用上——两处别弄反。

**返回的组有 16 个键**，没有 LIMIT、没有分页、**没有成员数阈值**（单成员组也返回，
`is_semantic_cluster` 只是 `len(members) > 1` 的标记）。排序三处各不相同：
组按 `(entity_type, -evidence_count, canonical_name)`、成员按 `(-evidence_count, canonical_name)`、
`member_entity_ids` 按字符串。

**`GET /entity-groups/{id}` 里有一块是 S7 的**：它把每个成员的 `get_entity_detail` 结果
放进 `members`，再把所有成员的 `observations` / `events` / **`relationships`** 按 id 去重后
扁平合并。`relationships` 与前两次同样的处理——**不进响应**。找组是**线性扫描内存里的
列表**（O(n)），不是按 id 查表。

##### T130 · `GET /knowledge`

`app.py:1921-1932`，返回三块：

| 块 | 来源 | 过滤 | 排序 | LIMIT |
|---|---|---|---|---|
| `profiles` | `semantic_profiles` | 给了 `person_id` 就按它；否则对 scope 内**每个实体轮流查一次**（N+1），`None` 丢弃 | 无 | 无（主键等值） |
| `claims` | `semantic_claims` | `status IN ('active','pending')`；`person_id` 有值时按它；`scope_id` **在 Python 里过滤** | `updated_at DESC` | **1000** |
| `spaces` | `memory_spaces` | `status = 'active'` | `created_at` | 无 |

**1.0 前端只读 `claims`**（`src/app.js:872` 的 `semanticKnowledgeView`，按
`claim.status !== "superseded"` 再过滤一次）。`profiles` 与 `spaces` 在 `src/` 与 `App.vue`
里**零读取**——侧栏的空间列表走的是另一条路由 `/api/memory-spaces`
（`src/app.js:1161`），不是 knowledge 的 `spaces` 键。**如实记下，不据此砍掉**（它们是
1.0 的 API 契约，而且 `profiles` 正是 T128b 里留给 T130 的那一块）。

`semantic_profiles` / `semantic_claims` 在 2.0 **零读路径**（只在迁移里建表），所以
T130 要新建一个语义读的端口。

#### T131 动工前核查（2026-09-30）：这不是接线，是新建

§3 的任务表写「把非人类实体接进 ``retrieve_entity``，让实体通道服务它们」。
**核查推翻了它的前提：1.0 从来没有把非人物实体接进检索。**

##### 1.0 那边的事实

- 1.0 的实体通道（``retrieval/entity.py``）**只服务人物**：``:28`` 只从
  ``dimension == "person"`` 的条件里取名字，``:38-42`` 的 SQL 硬过滤
  ``field_type = 'person_bridge'``。
- 1.0 的词法投影（``retrieval_indexes.py:177-181``）写的是 **observation 的字段文本**
  （``place`` / ``activity`` / ``caption`` / ``objects`` / ``clothing`` / ``ocr_text`` /
  ``people``）与 asset 锚点（``:233-269`` 的 geo / time / file）——
  **没有一处读 ``entities.canonical_name``**。
- ``entity_mentions`` 在 1.0 只用于**人物**条件判定与结构化筛选
  （``evidence_retrieval.py:499-508`` 的 docstring 原文就写着 "benchmark person linkage
  lives in entity_mentions"），``structured_memory.py:147-150`` 那条 join 的 ``entity_ids``
  只从 ``parsed.entity_names`` 来，而它的唯一写入者是 ``agent_runtime/tools.py:147`` 的
  person 分支。
- 1.0 的 place 走的是**另一条路**：``agent_runtime/tools.py:142-144`` 把它塞进
  ``semantic_conditions``（``dimension="place"``），由条件判定与 metadata 通道的 geocode
  预筛召回，**与 ``entities`` 表无关**。

所以 place / object / time 那些实体在 1.0 只被写入与**在浏览接口里读**，**不进任何召回通道**。

##### 2.0 那边的事实

实体通道同样只服务 person，卡在三处：``search_terms.py:230`` 的
``field_type = 'person_bridge'`` 硬过滤（docstring 自己写明理由：不加它，同名的地名或
物品也会把 asset 带进来）、``retrieve_entity.py:94`` 的 ``observation.people`` 兜底、
``retrieve_evidence.py:307`` 的输入侧只传 ``request.person``。

而 1.0 用来召回 place / object 的那两条路，**2.0 都已经有了**：
``domain/retrieval/lexical.py:37-45`` 的 ``place`` / ``object`` 字段（来源是 observation
的 ``place`` / ``objects``）、``ANCHOR_FIELD_TYPES`` 的 ``geo`` / ``time`` / ``file``。

##### 结论

**T131 不是「把 1.0 已有的接线搬过来」，2.0 这边是新东西。** 按项目那条
「不新增 1.0 没有的设计，遇到 1.0 做得不对的地方先报给用户」，**先不动手**，
把证据摆出来等裁决。三个可选方向（都不在本次自行决定）：

1. **不做**：1.0 的召回能力 2.0 已经等价具备（见上表），T131 记为「1.0 无此设计」。
2. **做**：把 ``entities.canonical_name`` 写进词法投影（新增一个 ``field_type``），
   让确认为 place/object 的实体名成为召回面——**这是新行为**，会改变召回结果，
   需要先定「它该不该进融合、算不算 anchor」。
3. **只做一半**：把非人物实体接进 ``Observation.people`` 那种「快照」面，
   不改通道权重——仍然没有 1.0 依据。

**顺带记一条**：``retrieval_indexes.py:344-359`` 的 ``search(scope_id, field_type, value)``
在 1.0 生产代码里**没有调用者**（只有测试用），2.0 已经照此判断不移植它
（``domain/retrieval/ports.py:80-83``）——与 T131 是同一类「1.0 有这个函数但没人调」，
但那次判对了，这次的任务表没有。

#### T132 收口（2026-09-30）：§1.1 三条缺口的处置

| §1.1 的缺口 | 处置 |
|---|---|
| ① 可达性门禁的判定单位是模块、不是符号 | **补了**（S6 · T133）——`scripts/check_method_reachability.py`，带 `PENDING_METHODS` 例外表与反向规则。本切片接线时它**自己删掉了三条登记**（facts 的三条、`maintain_property`、`list_properties`） |
| ② **没有任何门禁盯 schema** | **记入不做**，见下 |
| ③ `check_capability_map.py` 是单向校验 | **补了**——加了反向规则（标着 `pending` 的，`target_path` 不能已经存在），并把卡住的两条改正 |

##### ③ 的具体处理

`person_moments` 与 `person_portrait_revisions` 两条在 S5c 就交付了却一直标着 `pending`,
本次改成 `verified` 并补上真实的 `target_path` / `verification`
（`repositories/moments.py` + `test_moment_repository.py`、
`repositories/portraits.py` + `test_portrait_repository.py`）。

新规则**做过反向验证**：把其中一条改回 `pending`，门禁立刻报红并指出 `target_path` 已存在。
与 `check_reachability.py` 的 `PENDING_WIRING`、`check_method_reachability.py` 的
`PENDING_METHODS` 是同一条纪律——**登记一旦被现实追上，门禁自己要求删**。

##### ② 为什么记入不做

缺口是真的，2026-09-30 复核仍然成立：`ingest_batches` 表建在
`migrations/0002_media.sql:48`、`assets.batch_id` 列建在 `:9`，而全 `apps/api/src/` 里
唯一提到它们的是 `application/media/import_album.py:18` 的一句**注释**；
线上库该表 **0 行**、该列**全 NULL**（实测）。

不做，是因为**它是另一个形状的问题**：schema 的「有没有人读写」不像模块与方法那样有
一个明确的调用点可查——列可以从 `SELECT *` 里被顺带读出来，表可以被原样透传。
要判得准就得做数据流分析，那比这一条门禁的收益大得多。**但缺口要留痕**：
这三张表/列的事实写在这里，S6 之后谁动 `ingest_batches` 都该先回来看一眼。

##### T131 的裁决（2026-10-01）：**不做**

**裁决：不做，记为「1.0 无此设计」。** 取证见上——T131 要做的事在 1.0 里没有对应物，
而 2.0 现有的词法 ``place`` / ``object`` 字段（``domain/retrieval/lexical.py:37-45``）
与 ``geo`` / ``time`` / ``file`` 锚点已经**等价覆盖**了 1.0 的召回能力。
按项目那条「不新增 1.0 没有的设计」，不引入 1.0 没有的召回行为。

**后果要写清楚**：T127 落下的 place / object / time 实体**只服务浏览与详情接口**
（T128 的 ``GET /entities`` 与 ``GET /entities/{id}``、T129 的 ``entity-groups``），
**不进任何召回通道**。这与 1.0 一致——那边的非人物实体同样只被写入、只在浏览接口里读。

**留一条给以后的线索**：如果哪天要加，第一件要定的事**不是**「怎么写投影」，而是
「它进不进融合、算不算 anchor 通道」——``domain/retrieval/fusion.py:28`` 的
``ANCHOR_CHANNELS = ("metadata", "entity")`` 会把分类钉在候选**第一次出现**的时候
（``fusion.py:76-84``），而首次出现的顺序由编排里的插入顺序决定
（``retrieve_evidence.py:287-333``）。新通道插在哪里，会改变已有多通道候选的
``evidence_class``。

### 收口

| Task | 内容 |
|---|---|
| **132** | 收口：`capability-map.jsonl` 补本切片新能力；把 §1.1 的三条自检缺口**或补或正式记入不做** |

### 伴随任务（低成本的防腐，与上面并行）

| Task | 内容 | 为什么现在做 |
|---|---|---|
| **133** | **方法级可达性**：新增一条检查，对已判定可达的模块看其公开**方法**有否调用者。§1.2 ① 的三个死方法是现成红灯样本 | 本切片要接大量方法线；接线时正是这条门禁最有用的时候——它会把「还剩几个零调用方法」变成可数的 |

**三条顺序约束**（单独看都对、顺序错了就出错）：

1. **133 要在 113–131 之前或同时**。否则接线全程没有任何东西提醒还剩哪些零调用方法——
   而「让红色消失」本身是这条门禁最好的一次验证。
2. **113/114 必须在 121 之前**。`query_memory_facts` 没有 facts 读路径就没有数据源，
   而直接写工具会得到一个「永远返回空」的工具，读起来像「这个人没有事实」。
3. **115a–115b 在 115c/117 之前**——没有写入侧就没有向量，召回器接上去只会永远返回空，而「空」与「没接」在读数上分不开。117 是对照验收，没有前面几个无从对照。

---

## 4. 逐任务要点（只写不显然的地方）

### Task 114 · facts 状态机的闭合

形状由 1.0 定：`confirm_fact` 是把新值升 `active` **并且**把旧值标 `superseded`，
两件事在同一事务里（`supersede_others` 就是为它准备的）。2.0 的 `set_status` 与
`supersede_others` 已写好、签名也对，**只是没有调用者**——先确认这一点再写用例。

回归用例钉那个具体病症：造一条 `pending`，走确认，断言它变成 `active` **且旧值变成 `superseded`**。
定点变异：去掉 `supersede_others` 的调用，用例必须变红——否则它只验了「状态改了」，
没验「旧值被作废」，而后者才是「改口」这个动作的全部意义。

### Task 117 / 123 / 124 · 三个「对照验收」

这三个的共同形状是：**新增的能力必须证明它比原来多拿到了东西**，而不是「不报错」。

- 117：同一 query 补齐前后各跑一次，断言 `channels` 里出现了新键、且**名次不是垫底**
  （垫底意味着它其实没贡献）；
- 123/124：拿一张**已知内容的照片**跑，断言返回的观察/文字与人工标注一致——
  1.0 的 PhotoBench 有现成的题可以借；
- 三个都要有「关掉新通道 / 不接工具」的对照臂，否则「有输出」与「输出是对的」分不开。

### Task 121b · 结构化查询的 SQL 是重写，不是移植

1.0 的 `StructuredMemoryExecutor` 把「筛什么」与「怎么筛」写在同一个类里（311 行）。
2.0 拆成两半：`domain/retrieval/structured_query.py` 的 `derive_filter`（纯规则，
可穷举）与 `infrastructure/persistence/repositories/structured.py` 的 `build_base`
加四个聚合方法（SQL，要对真库）。**拆开的收益在验证方式上**：前半穷举单测，
后半用「同一份数据、两条路径同一个数」对照——`place_text_matches`（融合后判定）
与 `place_clause`（召回时 WHERE）必须给出同一个判定，否则同一个问题走统计路径与
走语义路径会得到两个数。

**三处与 1.0 的差异，每条都有理由**（模块 docstring 有全文）：

| 差异 | 依据 |
|---|---|
| `COALESCE(a.derived_kind,'') = ''` 整条删掉 | 2.0 的 `assets` 表既没有 `derived_kind` 也没有 `parent_asset_id`（实测 `PRAGMA table_info`），**根本不建派生资产**；照抄会直接报 no such column |
| `a.captured_location LIKE ?` 删掉 | 2.0 那一列是 `GpsPoint`，拿 `30.274100,120.155100` 去 `LIKE '%杭州%'` 永远匹配不上——`test_conditions.py:295` 早就为这件事写过用例 |
| `place` 分组的键改成确定性的 | 1.0 直接读左连接上来的 `o.place`，一个资产有多条观察时取到哪一行由 SQLite 的扫描顺序决定，同一个问题问两次可能给出不同的分组名 |

**时间边界用 `isoformat()` 不用 `str(datetime)`**：`captured_at` 存的是 ISO 串，
而 `str()` 用空格分隔日期与时间，两者的字典序不同，`>=` 会静默给出错误的边界。
同理，`derive_filter` 把时间在**派生阶段**就解析成 `(start, end)`，而不是留到拼 SQL
时现解析——那样「解析失败」会以「拿到 None」的形式悄悄过去。

**定点变异 8 条全部变红，但过程里抓出两条假绿，比变异本身更值得记：**

1. 时间边界那条最初用 `tzinfo=UTC` 造数据，落库成 `2023-11-01T00:00:00+00:00`，
   比上界 `2023-11-01T00:00:00` 多一个尾巴、字典序更大——把 `<` 改成 `<=` 之后
   它照样被排除，用例照样绿。**它绿的原因不是边界开闭，是那个时区后缀。**
   真实库里的值是 naive 的（EXIF 直读），用例改成 naive 之后才真的守住右开；
2. 通配符那条最初拿 `AB` 去对模式 `%A_B%`，而那个模式要三个字符才匹配得上，
   `instr` 与 `LIKE` 给出同一个答案，用例对这两种实现**完全没有区分力**。
   存储值改成 `AXB` 才把它劈开。

**两处需要后续处理，先记在这里：**

- **同一列里 naive 与带偏移量的值并存是一个已知的坑**
  （`test_time_bounds_compare_as_strings_so_offsets_are_a_hazard` 钉住了它）。
  时间区间是拿**字符串**比大小的，同一列只要全是同一个偏移量，字典序就等于时间序——
  今天成立。一条带非 UTC 偏移的记录进来就不成立了（`2023-10-31T20:00:00-05:00`
  排在 `2023-11-01T00:00:00` 前面）。修法在**写入侧**（落库前归一成一个偏移量），
  不在读取侧——读取侧替它决定「naive 的值算哪个时区」是猜。
- **`strip_admin_suffix` 的后缀顺序有个既有缺陷**：`区` 排在 `地区` 前面，
  于是 `阿里地区` 去掉的是 `区` 不是 `地区`，得到 `阿里地`（1.0 的 `replace` 连招
  给的是 `阿里`）。也就是说用户说「阿里」时规则 2 在 2.0 里匹配不上。这个顺序在
  `domain/places/aliases.py`，不在 SQL 那一侧；改它要连带改 `place_text_matches`，
  单列一条。

**动工 T121c 前读 1.0 的工具层，读回一个自己在 T121b 写错的判断。**
`derive_filter` 一度写成「只有 HARD 档的地点条件才进 WHERE」，理由是「SEMANTIC 档不该
排除候选」。1.0 的 `structured_memory.py:_place` **全程没看过 `strictness`**——它遍历
`constraints_for("place")` 返回第一个非空值；而 1.0 的工具层
（`agent_runtime/tools.py:_draft_from_filters`）把地点条件构造成 `semantic_required`。

两句合起来：按严格度筛的话，**每一次带地点的统计都会悄悄退化成整个相册的统计**，
而返回值看起来完全正常（`count=53` 不是错误，只是答非所问）。两档严格度的差别作用在
**融合之后逐候选判定**上（`conditions.excludes`），不作用在召回/统计的 WHERE 上——对
count/first/last 这类问题，`filters.place` 就是问题本身。

原用例把这个错误判断钉住了（`test_a_semantic_place_constraint_does_not`），所以变异
不会红。改成断言正确行为之后，把 HARD 判断植回去立刻红一条。**这是「一条绿的用例
可以钉住一个错的结论」的第二个例子**，前两个假绿在同一节的上面。

同一次核对还改掉一处：时间与地点都取**第一个**可用约束，不是「后面覆盖前面」。
1.0 遍历时碰到第一个就返回，写成覆盖的话，两条约束并存时谁生效取决于 spec 的构造顺序。

**门禁这边有一条盲区被它自己暴露出来**：`check_reachability.py` 的粒度是**模块**，
判据是「有没有被 import 过」。端口 `domain/retrieval/ports.py` 引用了
`StructuredFilter`，而 `ports.py` 从 `container.py` 可达，于是
`structured_query.py` 整个模块就算「接上了」——**模块内的规则函数
（`derive_filter` / `hard_conditions` / `normalize_group_by`）其实至今没有生产调用者。**
已在 `check_reachability.py` 的 `PENDING_WIRING` 旁边写明，T121c 是它们的第一个真调用者。

### Task 122 · query_photo_people 的动工前核查（2026-09-29）

照 S5c T86g 与本案 T127 的体例先核依赖。**核出两个缺口，都不是接线问题，是读路径缺一截。**

1.0 的 `_query_photo_people` 做三件事，逐件对到 2.0：

| 1.0 做的事 | 2.0 有吗 |
|---|---|
| `rs_store.resolve_handle(result_set_id, handle)` → asset_id | **有**。`ResultSet.resolve(handle)`（`domain/assistant/result_set.py:74`），只是名字短一点 |
| `_confirmed_photo_identities(store, asset_id)`：`face_instances ⋈ face_clusters ⋈ entities`，按 asset 取回已绑定的人脸（带 bbox / face_instance_id / cluster_id） | **没有**。`FaceRepository` 只有 `get_instance`（单条）与 `list_instances(cluster_id)`（按簇），**没有按 asset 的那一向**。缺 `list_instances_for_asset(asset_id)` |
| 数 `observations.people_json` 里**没有 `entity_id`** 的条目 = 未确认同行者 | **读不出来**。写入侧确实存了 `{"entity_id":…, "name":…, "status":…}`（`entities.py` 的 `_append_person_snapshot`），但读取侧 `_json_people` 只留名字（`assets.py:148`），`Observation.people` 是 `tuple[str, ...]`——绑定与否这一位在 domain 里被丢掉了 |

第一件事是加一个仓储方法，机械。第二件要想清楚：
1.0 的「未确认同行者」正是靠这一位算出来的，丢掉它之后这个数字无从计算——
而它是要回给模型的一句话（「另有 N 名未确认身份同行者」），也是 prompt 里
「不要给未绑定的人安名字」这条纪律的落点。

两条路：

- **改 `Observation.people` 的类型**，让它带上绑定状态。影响面大：词法投影、
  事件锚点、参与者投影都读它；
- **另开一条只读路径**，专供这个工具按 `people_json` 原样取回来。

倾向后者——**先把范围收在这次工具里**。`Observation.people` 是 S4 定下的形状，
为一个工具去改它，等于让 S4 的模型去背 S6 的需求。

**另外一处与 1.0 不同、要先定口径的：`membership`。** 1.0 逐个人调
`get_effective_family_membership(scope_id, entity_id)`，那是**家庭图谱**（S7）。
2.0 现在只有 `PersonDirectory.lookup(name, scope_id).family_role`。所以 T122 要么
先只回 `family_role`（并在工具描述里说明「尚无关系图谱」），要么把 membership 留空
标 `unknown`。**不要在 S6 里顺手把家庭图谱做了**——那是 S7 的整个主题。

结论：**T122 从「接线」升级为「补两个读路径 + 接线」**，与 T121 的升级同一形状。
`asset_handle` 的解析与 scope 校验这两段可以直接照 1.0，没有缺口。

#### T122 二核（2026-09-30）：读两次 1.0 之后，任务定义定下来了

上面那一版是对的，但有两处写得太轻，另有一处**整条漏了**。

**① `_confirmed_photo_identities` 的名字撒谎，SQL 不撒谎——照 SQL 搬。**
它的 docstring 与函数名都说 "confirmed"，而那条 JOIN 里**一个状态过滤都没有**：

```sql
FROM face_instances fi
JOIN face_clusters fc ON fc.id = fi.cluster_id
JOIN entities e     ON e.id = fc.entity_id          -- INNER：没绑簇的、簇没绑实体的都不出现
LEFT JOIN entity_mentions em ON em.face_instance_id = fi.id AND em.entity_id = fc.entity_id
WHERE fi.asset_id = ? AND e.entity_type = 'person'  -- 就这一条过滤
ORDER BY fi.quality DESC, fi.detection_confidence DESC
```

它判的是「**绑没绑**」，不是「确认没确认」（`tools.py:2277-2316`）。2.0 里簇可以带
pending 实体（`enroll_face` 就是「建簇带 pending 实体」），所以两者不是一回事。
**按 SQL 搬，并把这条不一致写进端口方法的 docstring**——顺手补一个
`status = 'confirmed'` 是在改产品行为，不是修 bug。

**② `_confirmed_photo_identities` 是 T122 与 T123 共用的件，不是 T122 私有的。**
1.0 里两个工具都调它：`_query_photo_people`（`:2577`）取 `person_name` / `membership`，
`_inspect_photo`（`:2158`）取 **`bbox`** 去裁目标人物的定位图。
所以这个读路径的返回必须带上 `bbox` / `face_instance_id` / `cluster_id`，
**即使 T122 自己一个都不用**——否则 T123 动工时会再开一条几乎一样的。
（T123 还另有第三个近似读：`_photo_face_manifest`（`:2320`）——那张表含**未绑定**的脸、
`face_id` 是局部编号 `face_1`、且带 `identity_status`。两者别合并，见 §4 Task 123。）

**③ `membership` 的口径定了：回 `family_role`，不装作有家庭图谱。**
1.0 逐个人调 `get_effective_family_membership(scope_id, entity_id)`（家庭图谱，S7），
2.0 只有实体上的 `family_role`。**输出里保留 `membership` 这个键**（工具契约的形状照 1.0），
值取 `family_role`，并在**工具描述里写明「尚无关系图谱」**。留空标 `unknown` 是更差的选择：
`membership` 会是每一行都相同的常量，读起来像「这些人都没有家庭归属」。
**注意 `family_role` 在端口/仓储那一层叫 `family_role`，映射成 `membership` 是工具层的事。**

**④ 漏掉的那条：证据类型的词表。** 1.0 的 `query_photo_people` 声明
`produces_evidence=("photo_identity", "confirmed_identity")`（`tools.py:2727`），
两个**都是**公共证据类型（`evidence_contract.py:13-14`），也都进
`planner_evidence_types()` 发给规划器。

2.0 的 `EvidenceType` 现在只有四个，两个都不在：

| 1.0 | 2.0 | 依据 |
|---|---|---|
| `confirmed_identity` | **已有，就是 `PERSON_PROFILE`** | `intent.py:89-92` 把 `person` / `identity` / `profile` 三个轴词都映射到它；1.0 的 `_TYPE_MAP`（`goal_planner.py:66-67`）把 `person` / `identity` 映射到 `confirmed_identity`——**同一个轴**，2.0 是改名加收窄 |
| `photo_identity` | **没有，要加** | 它在 1.0 是独立的一类：`evidence_contract.py:14` 的公共表里有它，规划器提示词专门交代「身份问题只有在需要确认**照片中的人名**时才声明 photo_identity」（`goal_planner.py:49`），`runtime.py:1011` 还为它单独做工具准入 |

所以 `satisfies` 是 **`(PHOTO_IDENTITY, PERSON_PROFILE)` 两条**：
`PHOTO_IDENTITY` 是这个工具自己的产物，`PERSON_PROFILE` 是 1.0 那条
`confirmed_identity` 的兼容面——**照 1.0 声明，否则「问照片里的人是谁」这类需求
在本工具被调用之后依然开着，门禁会逼模型再去找一个查不到的东西**（与
`_person` 那条 `blocks=PERSON_PROFILE` 是同一个坑的两面）。

**动工清单（四条读路径 + 接线）：**

| # | 落点 | 形状 |
|---|---|---|
| 1 | `domain/identity/ports.py` + `repositories/faces.py` | `FaceRepository.list_photo_identities(asset_id) -> Sequence[PhotoIdentityRecord]`；记录带 `face_instance_id` / `asset_id` / `cluster_id` / `entity_id` / `person_name` / `family_role` / `bbox` / `detection_confidence` / `quality` |
| 2 | `domain/media/ports.py` + `repositories/assets.py` | `ObservationRepository.latest_people_snapshot(asset_id) -> Sequence[PeopleSnapshotEntry]`（`name` + `entity_id`，按 `updated_at DESC LIMIT 1`）；**只给这个工具用**（§8.3 的裁决） |
| 3 | `domain/assistant/evidence_ledger.py` | 新增 `EvidenceType.PHOTO_IDENTITY`，描述表（`intent.py:305`）补一行 |
| 4 | `domain/assistant/tools.py` + `application/assistant/tools.py` + `api/routes/assistant.py` | `ToolSpec` + `AssistantToolbox._photo_people` + 注入两个仓储 |

工具描述照 1.0 原文（`tools.py:2723-2724`），另加一句「尚无关系图谱」。

### Task 124 · read_photo_text 的动工前核查（2026-09-29）

计划里点名要核的第一件事就是「1.0 的 `ocr_tool.py` 用的是哪个 OCR 后端」。核完了：

**1.0 是两级：PaddleOCR 小模型优先，读不到回退 VLM。**（`agent_runtime/ocr_tool.py:3` 的
模块注释原文就是这个意思。）

- `small_ocr_available()` 探的是 `import paddleocr` 能不能过，注释写着
  「**CPU，零显存，进程内推理**」（`:134-142`）；
- `_try_small_ocr` 是主路径，VLM 是回退；
- 密集文字另有两条处理：`_tile_images(path, rows=3, cols=3, scale=2.0)` 切块放大，
  `_small_row_cluster(items, y_tol=26)` 按 y 聚行；
- `_crop_region_to_temp` 的注释说明 PaddleOCR 的 predict 只吃文件路径或 ndarray，
  不吃 BytesIO——所以它**要落临时文件**，这在小图片上是笔固定开销。

**2.0 一个都没有。** `paddleocr` 不在 `pyproject.toml` 里，`.venv` 里
`import paddleocr` 直接 `ModuleNotFoundError`。2.0 唯一的 OCR 来源是
`OpenAIVlmObserver`（`infrastructure/media/vlm_observer.py:77`）——`ocr_text` 是它
返回的八个字段之一，「图中文字，1000 字内」。

**所以 2.0 不是丢了能力，是那个「可选后端」从来没加过。** 换个说法：1.0 在
`paddleocr` 装不上的环境里会自己退化成 VLM-only（`small_ocr_available()` 返回假），
**那就是 2.0 现在的状态**。

这个区别在 T124 上是决定性的，因为 1.0 把这个工具的定位写得很明确——它的工具描述
里专门警告模型不要拿它去统计「OCR 金额、桌数」。那等于承认：**这个工具存在的理由
是「读出真的写着什么」，而不是「描述大概有什么」。** VLM 做不到后者里的前者：

| | PaddleOCR | VLM（8100 的 qwen3.5-4B） |
|---|---|---|
| 会不会读出图上没有的字 | 不会 | 会，且不会自报 |
| 精确数字（金额/日期/桌号） | 逐字 | 4B 模型上不可靠 |
| 成本 | CPU，零显存 | 每张一次 GPU 推理（1.0 调了 2,867 次） |

**结论：T124 是「先定后端，再写工具」，不是「接个线」。** 三条路：

1. **引入 PaddleOCR**——最接近 1.0，但是给 2.0 加一个新依赖 + 一套权重 + 一条 CPU
   推理路径，**这是部署面的改动，要单独裁决**；
2. **只用 8100 的 VLM**，并在工具描述里如实收窄（不说「逐字读取」，说「读取图中
   文字，可能不完整」）——零新依赖，但上面那张表右列的三条代价照单全收；
3. **先做 2，把 1 留成显式的后续**——工具接口不变，后端可换。

倾向 3，但这个选择不该由实施者替用户定：它改的是「这个工具能不能被信任来读数字」，
而 1.0 的工具描述说明那正是它的用途。**在裁决之前 T124 不动工。**

### Task 123 / 124 · 共用的那块：2.0 有「看图」，没有「问图」

T124 的后端已裁决走第 2 条路（**只用 8100 的 VLM，零新依赖，工具描述如实收窄**）。
定完之后顺手核了 T123/T124 共用的那一层，核出它们要的是同一个 2.0 没有的东西。

2.0 现有的两个视觉入口都不是「拿一个问题去问这张图」：

- `VlmObserver.observe(path) -> ObservationSemantics | None`
  （`domain/media/ports.py:92`）——**schema 是固定的七个字段**
  （caption / activity / place / event_type / ocr_text / objects / clothing / facts），
  对应 1.0 `model_clients.py:1130-1198` 的 `analyze_image`。它是**导入期**的观察器，
  每次最多 3 次调用（首轮 / 补全 / 中文化），不接问题；
- `OpenAIPersonVision`（`infrastructure/media/person_vision_model.py`）——人物画像 /
  瞬间 / 外观三个**专用**入口，各自有各自的输出形状。

而 1.0 这两个工具要的都是**问题驱动**的一次调用：

- `inspect_photo`：`_INSPECT_PROMPT` 有 `{question}` 与 `{target_instruction}` 两个槽，
  返回 `{"observation":…, "certainty":"supported|uncertain", "target_face_id":…}`，
  并且提示词里带着**人脸清单**，让模型在「问题指向某个人但没给名字」时回一个 face_id；
- `read_photo_text`：OCR 导向的提示（1.0 的主路径是 PaddleOCR，VLM 是回退）。
  按 T124 的裁决，2.0 只剩这一路。

**所以 T123 与 T124 共用一个上游件**：一个「photo inspector」端口，
`inspect(path, question, faces) -> 结构化回答`。两条工具各自只决定提示词槽位与
输出解析。

不先落这一件的话，两个工具会各自写一遍 HTTP 调用——而那一遍里最容易写错的两处
（图片编码、超时与「模型不可用」的降级）就会有两份实现，各自测各自的。

**动工顺序应当是：先 inspector 端口 + 适配器 + 它的对照用例，再 T123、再 T124。**
这也是把 T123/T124 从「接线」改判为「补一层 + 两个工具」的原因——本切片第四个
「计划说接线、实际不是」的任务（前三个：T121、T122、T124 的后端）。

继承 1.0 的一条口径：`inspect_photo` 的 `certainty` 只有
`supported` / `uncertain` 两档，**没有「确定没有」**。模型答不上来时报 `uncertain`，
不是 `contradicted`——这与 `conditions.py` 里 UNKNOWN 与 CONTRADICTED 分开是同一个道理，
不要在移植时顺手加一档。

### Task 123 · inspect_photo 的动工前核查（2026-09-30）

端口已落（§8.5），T122 也把共用的那条绑定读路径（带 `bbox`）落了。核完之后，
**T123 比「写一个工具」多三件前置**，其中两件在**策略层**，不在工具层。

**① 1.0 的「昂贵，每轮最多 1 次」是一条真预算，2.0 没有。**

1.0 的 `ToolSpec` 有一个 `cost_class` 字段，`inspect_photo` 与 `read_photo_text`
都是 `"expensive"`（`tools.py:2755`、`:2769`）。`ToolPolicy.authorize` 拿它去问预算：

```python
if self.budget is not None and not self.budget.can_tool_call(
        inspection=spec.cost_class == "expensive"):     # tool_policy.py:37-38
    return ToolDecision(False, "budget exhausted")
...
self.budget.record_tool_call(inspection=spec.cost_class == "expensive")   # :62
```

而 `BudgetState.max_inspections = 1`（`budget_manager.py:17`）。**这条预算同时管两个工具**
——它们是同一个计数器 `inspections` 的两个使用者，所以「每轮总共只能精读一张图一次」，
不是「每个工具各一次」。这正是 1.0 工具描述里那句「同一张图最多复核一次，
不要反复检查同一张」的**执行机构**；不实现它，那句话就只是一句劝告。

**2.0 现状**：`TurnProfile`（`domain/assistant/profile.py`）只有
`max_steps=8 / max_tool_calls=6 / max_search_calls=1 / wall_time_s=180 / final_reserve_s=10`，
**没有 `max_inspections`**；`ToolSpec` **没有 `cost_class` 字段**；
`tool_policy.py` 只有 `search_memories` 那一条计数。所以这一件要新增，且它落在
`domain/assistant/profile.py` + `token`/`task_state` + `tool_policy.py` 三处。

**② `preconditions` 这一件，核完之后结论翻过来了：2.0 里它没有家，不该补。**

1.0 的 `inspect_photo` 有前置条件 `asset_handle_in_current_preview`（`tools.py:2757`），
由 `ToolPolicy.execute` 校验（`tool_policy.py:44-55`）。看上去 2.0 缺一层，**实际不是**：

1.0 能这么写，是因为它的 `task_state` 是**一个可变字典**，`result_preview` 就存在里面
（`tool_policy.py:47-49` 直接读 `task_state.get("result_preview")`）。
2.0 的 `TaskState`（`domain/assistant/task_state.py`）**没有 preview、也没有 result_set
任何一个字段**——实测 grep 零命中；而 `ToolPolicy` 的类注释写死了它**无状态**
（「所有计数都在 `TaskState` 上」）。要在这条路上做前置条件，就得把仓储塞进策略层，
那会破坏 2.0 「策略只做决定、执行器才碰数据」这条分界。

**所以 faithful 的做法是把这条判据留在执行器里**——T122 的 `query_photo_people` 已经是
这个形状（`result_set.resolve(handle)` 失败 → `bad_handle`），而且它比 1.0 更严格：
1.0 的前置条件比的是「handle 在 preview 文本里」，T122 比的是「handle 真的解析得到一个
asset」。**T123 照这个形状做，不新增 `preconditions` 字段。**

（`required` 挡的是「模型漏填 handle」，这一层 2.0 有、工作正常，不涉及。）

**③ 第三个证据类型：`visual_observation`。**

`inspect_photo` 声明 `produces_evidence=("visual_observation", "photo_identity")`
（`tools.py:2756`）。`photo_identity` 已由 T122 补上；`visual_observation` 还没有。

**S6 四个工具要补的公共证据类型一共三类**，进度：

| 类型 | 谁产出 | 状态 |
|---|---|---|
| `photo_identity` | `query_photo_people`（T122）/ `inspect_photo`（T123） | **已补**（T122） |
| `visual_observation` | `inspect_photo`（T123） | 待 T123 |
| `visible_text` | `read_photo_text`（T124） | 待 T124 |

（2.0 的 `EvidenceType` 还缺 1.0 公共表里的 `location_metadata` / `temporal_metadata` /
`user_statement` 三类，那三类不属于 S6 的四个工具——登记在 §6 收口时一起判。）

**④ 人脸清单是第三条读路径，且与 T122 那条**不同**。**

1.0 的 `_photo_face_manifest`（`tools.py:2320-2358`）与 T122 用的
`_confirmed_photo_identities` 是两条查询，**不要合并**：

| | `list_photo_identities`（T122/T123 共用） | `_photo_face_manifest`（T123 独用） |
|---|---|---|
| 含未绑定的人脸 | **不含**（两个 INNER JOIN） | **含**（`LEFT JOIN face_clusters` / `LEFT JOIN entities`） |
| 人脸编号 | 无 | `face_1` / `face_2`… **局部编号**，按 `quality DESC, detection_confidence DESC, id` |
| 名字 | `person_name`（实体本名） | 只在 cluster 与 entity **双确认**时才填 `person_name`，否则留空 |
| 给谁 | T122 的 `people` 列表 / T123 的裁剪目标 | 提示词里的「人脸清单」，让模型在没给名字时回一个 `face_id` |

**「未确认者不得猜姓名」这条纪律的落点就在第三行那个双确认判据上**
（`cluster_status == 'confirmed' and entity_status == 'confirmed' and canonical_name`）。
注意它与 T122 那条 SQL 「一个状态过滤都没有」是**相反的**口径——两条查询在同一个文件里，
一个查状态一个不查，各自都有出处，**不要用一条的直觉去改另一条**。

**⑤ 裁剪件这一侧 2.0 已经齐了。** `expanded_person_crop`
（`domain/identity/appearance.py`）与 `render_person_crop`（`render_person_crop` 的
PIL 边界在 `infrastructure/media/person_vision.py`，端口是 `PersonCropRenderer`）
都在 S5c 落过，`observe_appearance` 正在用。T123 直接接上，并把它喂给
`PhotoInspector.inspect(..., crop=...)`——那个形参就是为这一条留的。

**动工清单（五件，第一件是 T123/T124 共用的前置）：**

| # | 落点 | 是什么 |
|---|---|---|
| 1 | `domain/assistant/tools.py`（`ToolSpec.cost_class`）+ `profile.py`（`max_inspections`）+ `task_state.py`（计数与 `can_inspect`）+ `tool_policy.py`（那条判定） | `cost_class` 与 `max_inspections`（**T123/T124 共用**，两边都靠它）。落点已探明：`TaskState` 加一个按 `cost_class == "expensive"` 数记录的属性，`ToolPolicy.decide` 里照 `search_memories` 那条判定的形状加一条 |
| ~~2~~ | ~~`preconditions`~~ | **不做**，见上面 ② |
| 3 | `domain/assistant/evidence_ledger.py` | `EvidenceType.VISUAL_OBSERVATION` |
| 4 | `domain/identity/ports.py` + `repositories/faces.py` | `list_face_manifest(asset_id)`（第三条读路径，含未绑定） |
| 5 | `domain/assistant/tools.py` + `application/assistant/tools.py` | `ToolSpec` 与执行器（含 `target_person` 解析、裁剪、`target_face_id` 回收） |
| 6 | `api/routes/assistant.py` | 注入 `photo_inspector` 与 `person_crop_renderer` |

**顺序约束**：第 1 件必须在工具之前——它是 T123 与 T124 **共用**的前置，
给一条工具单独写一遍等于把同一件写两遍（与 §4 Task 123/124 拒绝写两遍 HTTP 是同一条理由）。

### Task 127 · 恢复作答的阶梯

**动工前必须先核查**（照 S5c 的 T86g 体例：先核依赖，核出缺口再写任务定义）。
要核的是：1.0 的「四级阶梯」到底是哪四级、在哪个文件、由谁触发。
我这次只 `grep` 到措辞分级（确定→较可能→不确定→无依据），**那不是恢复阶梯**。
如果核查发现它其实是 S7 的事（S3 计划写的是「S6/S7」），本任务移出 S6。

---

## 5. 不做（按 2026-09-29 裁决与路线图归属）

| 项 | 归属 / 理由 |
|---|---|
| **`adjacency` 召回器** | **1.0 默认关着**（`configs/retrieval/defaults.json:10` 的 `"adjacency": false`，`build_default_retrievers` 连实例都不建），只被自己的测试跑过。实现它等于**加一个 1.0 从没跑过的东西**。按 `agent_contracts.py`（392 行、静态不可达 → 不搬）的先例：不实现，理由记入 `frozen.md`。**注意 `fusion.py` 的权重表与 `EXPANDER_CHANNELS` 里还留着它的位置——那是照抄 1.0 的权重表，留着无妨，但不要因为「表里有」就去建召回器** |
| 视频（4 条路由 + 抽取链） | **S8**；按裁决**单独排**，本切片不做 |
| family-graph 7 条、relationships CRUD、角色假设 | **S7**（设计 §8）；`s5-deviations.md` 第 15/35 条已标「随 S7」 |
| `hardware` / `model-profiles` / `runtime-providers` 9 条 | **S11**；设计 §2.1「2.0 不管理模型进程生命周期」 |
| PhotoBench 后端 / 前端 | **S9 / S10** |
| **鉴权** | 裁决：**不着急**。核实过的事实记在此：`api/routes/` 零鉴权依赖，9598/8091 绑 `0.0.0.0`，局域网内任何人可读全部家庭照片、改身份数据。**它不是「缺功能」而是「缺一个设计决定」**，等排期时单独定 |
| 语义实体 / entity-groups / knowledge | **未归属**：`repositories/entities.py:8-13` 注释推给 S7，但 S7 的定义（设计 §8）里没有它。**列进 §6 待裁决** |
| observations 原始列表、OCR settings / telemetry / query-gaps、`/api/dashboard` | 未归属，价值判断见 §6 |

**这份「不做」清单要进 `frozen.md`**（或有明确归属的记进对应切片计划），
否则下一次审计还会把它们当「缺失」重报一遍——本轮审计就已经重报了一批 S7/S11 的东西。

---

## 6. 完成定义

1. §3 的 113–133 全部落地，或逐条写明移出本切片的原因；
2. 设计 §8 给 S6 的验收条件成立：**证据包和 trace 能展开**；
3. 六路召回 6/6，且 117 的对照臂证明新通道有实际贡献（名次非垫底）；
4. 三个硬条件各有「过滤前有、过滤后没有」的用例；
5. 四个工具在真数据上各跑通一次，且有对照臂（关掉工具时结果确实变差）；
6. 133 的新门禁**有定点变异证明会红**，且 §1.2 ① 的三个死方法在接线后从红灯变绿；
7. `capability-map.jsonl` 不含「已实现却标 pending」的条目（`person_moments` /
   `person_portrait_revisions` 是现成的两条）；
8. **CI 结果可解释**：当前 `test_the_answer_never_leaks_internal_identifiers` 因
   qwen3.5-4B 会在正文写 `photo_N` 而常红（裁决：判为模型问题、不修）。
   本切片不改变这一点，完成定义要说清「CI 红 ≠ 本切片未完成」，并说明是哪一条。

---

## 7. 待裁决

| # | 问题 | 状态 |
|---|---|---|
**2026-09-29 全部裁决完毕**（含本文件第一版新提的三条）：

| # | 问题 | 裁决 |
|---|---|---|
| ① | 建/删相册 | **加入删除逻辑** |
| ② | S7 是否存在 | 存在，定义在设计文档 §8；本文件第一版误判为不存在 |
| ③ | 鉴权 | **不着急** |
| ④ | 视频 | **单独排**（对应路线图的 S8） |
| ⑤ | 回合取消后部分结果 | **丢弃** |
| ⑥ | 记忆空间删除的连带范围 | **删干净**：空间行 + 该 scope 的 assets/observations/events/entities/face_*/facts/…（按 `scope_id` 级联）+ 对应 Qdrant collection + `data/media` 下的实体文件。理由：留半份数据比删两份更糟——剩下的行会以孤儿形态出现在检索里，而**没有任何界面能让人发现它们** |
| ⑦ | 语义实体 / entity-groups / knowledge | **归 S6**（§3 第六组） |
| ⑧ | 恢复作答的阶梯 | **归 S7** |

**① + ⑥ 的落点**：建/删相册不属检索补全，**不在 S6 任务表内**——它进 §1.3 的「未归属」栏，
但**语义已定，不需要再问**：实现时按 ⑥ 的级联范围做，且删除必须是显式确认的不可逆操作。

**仍然没有归属的**（本切片不排，也不需要现在决定）：鉴权、observations 原始列表、
OCR settings / telemetry / query-gaps、`/api/dashboard`、回合取消、会话历史回放。


---

## 8. 当前进度与下一步（2026-09-29 交接）

**这一节是给下一个会话的入口。** 前面七节是计划，这一节是「计划跑到哪了」。

### 8.1 已完成

| 任务 | 状态 | 提交 |
|---|---|---|
| T121a 查询契约 | 完成 | `6028ca6` |
| T121b 筛选条件派生 + SQL | 完成 | `63b897a` `bd6dcee` `3f2dd51`（修正） |
| T121c 工具与注册 | 完成 | `c17daa5` `7b84f7f` `d001d01`（补覆盖） |
| **T123/T124 共用的 inspector 端口** | **完成**（§8.2 的「第一件事」） | 见本节末「这次落了什么」 |
| **T122 四条读路径 + `query_photo_people` 工具** | **完成** | 见 §8.6 |
| **T123 精读预算 + 人脸清单 + `inspect_photo`** | **完成** | 见 §8.7 |
| **T124 `read_photo_text`** | **完成**（后端裁决见 §8.8） | 见 §8.8 |

`query_memory_facts` 已在生产链路上：`AssistantToolbox` → `QueryMemoryFacts` →
`StructuredQueryRepository`，容器已装配，真库（53 张）跑通。

**门禁基线**：`make ci` 的 lint / format / mypy / 5 个 import 契约 / reachability /
method-reachability / module-map / capability-map / openapi **全绿**；覆盖率 97.50%（门禁 85%）。
唯一红的是 e2e `test_assistant_turn.py::test_the_answer_never_leaks_internal_identifiers`——
**已裁决为模型问题，不是代码问题，不要修**。

### 8.2 下一步：inspector 端口已落，接着按顺序做三个工具

~~**第一件事（T123/T124 共用，必须先做）**~~ → **已完成**，见 §8.5。

**S6 的四个工具全部落地**（T122 §8.6 / T123 §8.7 / T124 §8.8）。
剩下的是**第六组与收口**：T127–T131（语义实体）、T132（收口）。
**T125 已完成**（§8.9）——S6 的验收条件「证据包和 trace 能展开」现在有了可判定的落点。另外 T116（不实现 `adjacency`）的理由
**还没记进 `frozen.md`**——那是 §3 定的落点，收口前要补。

**T122 已按 §4 的「二核」清单落地**：四条读路径 + 接线 + 17 条新用例。
它比原计划的两条多出来的那两条，是**证据类型词表**（`photo_identity` 这个公共类型
2.0 没有）与 **T122/T123 共用的绑定读路径**（带 `bbox`，T122 自己不用）。

### 8.3 ~~还差一个裁决~~ → 已裁决（2026-09-30）

**T122 的 `Observation.people`**：1.0 靠 `people_json` 里有没有 `entity_id` 数出
「未确认同行者」，2.0 的读取侧只留名字（`assets.py:148` 的 `_json_people`），
而 `Observation.people` 是 `tuple[str, ...]`——绑定与否这一位在 domain 里被丢掉了。
这个数字是要回给模型的一句话，也是「不要给未绑定的人安名字」这条纪律的落点。

**裁决（2026-09-30）：先只读路径，S7 再评估改类型。**

- 本切片**只开一条只读路径**，专供这个工具按 `people_json` 原样取回来；
  **不动** `Observation.people` 的形状——那是 S4 定下的模型，
  影响面是词法投影 / 事件锚点 / 参与者投影三个读者，
  为一个工具去改它等于让 S4 的模型背 S6 的需求。
- **代价照单收下**：绑定状态只在这一个入口可见，另一个读者要它时得再开一条。
- **留给 S7 的问题**（不是「以后再说」，是一个有判据的待办）：S7 的家庭图谱落地之后，
  看**参与者投影**是否真的需要「绑定与否」这一位。需要，就该把类型改对；
  不需要，这条只读路径就是终点。

### 8.4 几条会浪费时间的门禁事实

- **每个任务的默认形状是「先核查」。** §3 的「依赖」栏写着「接线」的四个任务
  （T121/T122/T124/T123+124），核查后**没有一个是接线**。动工前先核依赖，
  核出缺口再写任务定义——这是 S5c T86g 立下的体例。
- **`check_reachability` 是模块粒度的**，判据是「有没有被 import 过」。端口里引用一个
  类型，整个模块就算「接上了」，而模块内的规则函数可能一个调用者都没有。
  它还会**按反向规则要求删掉自己的登记**——登记里的模块真被调用后，它会报红让你删。
- **coverage 只测 `sentrix.domain` 与 `sentrix.application`**（`pyproject.toml` 的
  `[tool.coverage.run] source`）。**infrastructure 一行都不算**——SQL 仓储只有集成用例
  覆盖它，门禁数不到。读覆盖率报表时要知道这件事。
- **`make ci` 的 coverage 段会被那条已知的 e2e 失败打断**，于是 `web-check` 不跑。
  报告「门禁全绿」时说清楚哪一段没跑到。
- **（本次新增）同名测试文件会让整轮 collection 直接失败。** 测试目录没有
  `__init__.py`，所以 `unit/test_photo_inspector.py` 与 `e2e/test_photo_inspector.py`
  会撞在同一个模块名上，pytest 报 `import file mismatch` 并**中断整轮**——
  `make coverage` 连一条用例都没跑就退出 2。**只有跑全量才会撞上**：单目录跑、按文件跑
  都是绿的。命名的现成规矩是 e2e 按**场景**命名（`test_visual_search` /
  `test_assistant_turn`），单元测试按**模块**命名——所以这次是
  `unit/test_photo_inspector.py` + `e2e/test_photo_inspection.py`。
- **（本次新增）`check_method_reachability` 对「先落地、后接线」的端口正是设计好的。**
  新增的 `PhotoInspector.inspect` 落地那一刻必然没有生产调用者，于是这条门禁报红要求
  登记；登记进 `PENDING_METHODS` 并写明 T123/T124，等两条工具接上时它会**按反向规则
  自己报红要求删掉那一条**。与 facts 那三条走的是同一条路。

### 8.5 本次落了什么：T123/T124 共用的 inspector 端口

按 §4 Task 123/124 的「先落端口 + 适配器 + 它的对照用例」，落了六个文件：

| 文件 | 是什么 |
|---|---|
| `domain/media/inspection.py`（新） | 1.0 `_INSPECT_PROMPT` / `TARGET_INSTRUCTION` / 人脸清单前缀的**逐字节原文**与 `build_inspect_prompt`；只装提示词不装解析 |
| `domain/media/ports.py` | 新增 `PhotoInspector` 端口 |
| `infrastructure/media/photo_inspector.py`（新） | `OpenAIPhotoInspector`：图片编码与一次往返**复用** `vlm_observer` 的 `_encode_image` / `chat_json` |
| `infrastructure/media/vlm_observer.py` | `chat_json` 的 `image` 形参从 `str` 放宽成 `str \| Sequence[str]`（1.0 指名目标人物时发两张图） |
| `bootstrap/container.py` | 装配 `photo_inspector`（按端口声明） |
| `tests/unit/test_photo_inspector.py` + `tests/e2e/test_photo_inspection.py` | 对照用例（见下） |

**动工前核出的三处，都不是「接线」**（本切片第五个「计划说接线、实际不是」）：

1. **端口签名要比 §4 写的多两位。** §4 写的是 `inspect(path, question, faces)`，而 1.0
   的 `_inspect_photo` 在指名目标人物时**发两张图**（裁剪件在前、原图在后，
   `tools.py:2192`），并且 `target_instruction` 那句只在**指名了人**时才填——与裁剪图做没
   做出来无关（裁剪失败落到 `not_located` 也照样填，因为那句话说「（如有）」，
   `tools.py:2186-2200`）。所以端口是
   `inspect(path, question, *, faces=(), target_person="", crop=None)`。
2. **`chat_json` 原本只收一个 `image`，多图是第一个真需求。** 没有另写一份请求组装——
   那一份要重复的正是「图片编码、超时、模型不可用的降级」，而它们正是这个端口存在的理由。
3. **对照用例的第一版是假绿，两处，都记在 e2e 的模块 docstring 里**，免得改回去：
   断言「两个问题得到两句不同的话」在**把问题写死**之后照样绿（采样噪声）；
   改成判内容之后仍然绿——**8100 的 4B 模型会答出问题范围之外的东西**（问形状，它把颜色
   一起说了）。所以「问题有没有进提示词」这条判据放在单元层（那里能逐字节断言发出去的
   提示词，定点变异是红的），e2e 只判结构（两种入口各自的答复形状、图真的发到了、
   `certainty` 不溢出两档）。**9 条定点变异全部变红**。

**两条给 T124 的实测证据**（`read_photo_text` 动工时直接用）：

1. **1.0 的 `_ocr_single_asset` 在任何 OCR 后端之前，先读导入期已经落库的
   `observations.ocr_text`**（`ocr_tool.py:544-559`），命中就返回
   `source: "ingestion_ocr"` / `certainty: "supported"` / `persisted: True`。而 2.0 的
   `OpenAIVlmObserver` 在导入时**本来就写了 `ocr_text`**（「图中文字，1000 字内」）。
   所以 T124 的第一条路不是「调 VLM」，是「先看这条观察里有没有现成的字」——
   1.0 也是这么排的，而且这一路**零推理开销**。
2. **这个 4B 模型答得比问的多。** 实测：问「这张图里画的是什么形状？只回答形状名称。」，
   它回「这张照片里画的是一个红色圆和一个蓝色正方形」。这正是 §4 Task 124 那张对照表里
   「会不会读出图上没有的字」那一格——**T124 的工具描述必须按裁决如实收窄**，
   不能声称逐字精确。


### 8.6 T122 落了什么：query_photo_people（四条读路径 + 接线）

| # | 落点 | 是什么 |
|---|---|---|
| 1 | `domain/identity/models.py` + `ports.py` + `repositories/faces.py` | `PhotoIdentityRecord` 与 `FaceRepository.list_photo_identities(asset_id)`——1.0 `_confirmed_photo_identities` 那条 SQL 逐字 |
| 2 | `domain/media/ports.py` + `repositories/assets.py` | `PeopleSnapshotEntry` 与 `ObservationRepository.latest_people_snapshot(asset_id)`——**只给这个工具用**（§8.3 的裁决） |
| 3 | `domain/assistant/evidence_ledger.py` | `EvidenceType.PHOTO_IDENTITY`（1.0 公共证据表里有而 2.0 缺的那一类） |
| 4 | `domain/assistant/tools.py` + `application/assistant/tools.py` + `api/routes/assistant.py` | `ToolSpec`（描述照 1.0 原文 + 一句「尚无关系图谱」）、`AssistantToolbox._photo_people`、注入两个仓储 |

用例：`apps/api/tests/integration/test_query_photo_people.py`（17 条，真 SQLite）。
**14 条定点变异，12 条变红，2 条是等价变异**（见下）。

**动工中核出来的四件事，都写进代码注释与 `behaviour-baseline.md` 了：**

1. **`people` 是按人脸实例逐行的，同一个人两张脸就出现两次**（摘要念「爸爸、爸爸」）。
   我第一版用例断言的是去重后的 `"爸爸"`，被自己写的实现推翻——回头读 1.0
   `tools.py:2589-2593`，**1.0 也是逐行生成**，它同样重复。去重会同时改掉 `people`
   的长度，而那个长度是证据。用例改成钉住 1.0 的行为。
2. **那条 SQL 有三重互相覆盖的过滤。** 把任一个 INNER JOIN 换成 LEFT JOIN 都观察不到
   差别——`NULL = 'person'` 不为真，`entity_type` 那句自己就把未绑定的行挡掉了。
   实测确认（去掉 WHERE 之后未绑定的脸立刻出现、用例立刻红），所以那两条是**等价变异**，
   不是用例漏了。**这一点写进了用例的模块 docstring**，免得下次有人看到「绿」就以为漏了。
3. **`ObservationRepository.add` 会把 `people_json` 重新序列化成
   `{"name":…, "status":"confirmed"}`——没有 `entity_id`。** 带 `entity_id` 的那一份由
   命名流程的 `_append_person_snapshot` 追加。所以「未确认同行者」这个数与
   「导入期快照 + 之后被认出来的人」两个写入者的时序有关，
   `latest_people_snapshot` 读的是**原始列**，不是 `Observation.people`。
4. **`intent.requirements_of` 是零调用者的死函数，而且它的描述表对
   `STRUCTURED_FACT` / `PHOTO_IDENTITY` 会 `KeyError`。** 活的路径
   （`parse_declaration`）用模型自己给的 description，根本不查那张表。
   **没有动它**——那是 §1.1 那类自检缺口，归 T132 收口时处理。

**两处要留意的后续**：

- `capability-map.jsonl` 没有为本次新增能力登记（§3 的 T132 是收口任务，按计划归它）；
- `check_method_reachability` **不需要**为这两个新端口方法登记——它们落地就有生产调用者
  （工具就是调用者），这正是它与 `PhotoInspector.inspect` 那次不同的地方。


### 8.7 T123 落了什么：inspect_photo（10,721 次的那一个）

三个提交，按依赖顺序：

| 提交 | 内容 |
|---|---|
| `334c571` | **精读预算**（T123/T124 共用）：`ToolSpec.cost_class` + `TurnProfile.max_inspections=1` + `TaskState.inspection_calls`/`can_inspect` + `ToolPolicy` 里那条独立判定 |
| `a085070` | **人脸清单读路径** `FaceRepository.list_face_manifest`（含未绑定、`face_1` 局部编号）+ `EvidenceType.VISUAL_OBSERVATION` |
| 本提交 | `ToolSpec` + `AssistantToolbox._inspect_photo` + 装配（`photo_inspector` / `person_crop_renderer`）+ 接线（`run_turn` 传 `question`） |

**动工前核出的两处「计划说 A、实际是 B」：**

1. **`preconditions` 不该补**（§4 Task 123 的 ②）：1.0 靠可变 `task_state` 字典里的
   `result_preview`，2.0 的 `TaskState` 没有这个字段、`ToolPolicy` 又写死了无状态。
   faithful 的做法是留在执行器里——就是 T122 的 `bad_handle` 形状。
2. **`cost_class`/`max_inspections` 是真缺**，而且 **T123/T124 共用同一个计数器**
   （1.0 的 `inspections`），所以是「一轮总共只能精读一张图一次」。

**动工中定下的三处口径：**

1. **裁剪件的坐标不做归一化。** 1.0 有一段 `max(bbox) <= 1.0` 就按图宽高换算的防御
   （`tools.py:2178-2183`），而它要 PIL 拿图宽高——2.0 的 domain-purity 不许 application
   碰 PIL。**2.0 的框本来就是像素**（`observe_appearance` 把 `instance.bbox` 直接交给
   `render_person_crop`，没有换算），所以那一段在 2.0 没有对应的输入；真来了归一化的框，
   `render_person_crop` 抛 `ValueError`，落进那个 `not_located` 分支——行为仍然安全。
2. **`target_person` 的来源少一路。** 1.0 还看 `task_state.active_person`（`:2127`），
   2.0 的 `TaskState` 没有这一格——**不补**（那是 1.0 主循环的可变状态，与「策略无状态」
   的分界冲突）。留下的两路是显式参数与「问题里提到的已确认人名」（`:2162-2165`）。
   为了后一路，`AssistantToolbox.execute` 加了一个 `question` 形参——
   **那不是加状态，是把已经在手上的那个字符串传下去**。
3. **`bad_handle` 不封口，`file_unavailable`/`model_unavailable` 封。**
   1.0 只回一个 `blocked` 列表、不做别的；2.0 的账本认封口。换一张图还有机会的那条
   **不封**，否则会把「换个 handle 再试」也一起堵死。

**用例与变异**：`test_inspect_photo.py` 26 条 + `test_face_manifest.py` 12 条；
**定点变异 19 条全部变红**（精读预算 6、人脸清单 9、工具 10，其中 `bad_handle`
那条第一次写成了占位、补跑才红）。

**变异先于用例抓到的四处**（都写进了对应用例的 docstring）：

- 精读预算「跨两个工具共用」最初只测了一个方向，把计数写成「按工具名数 inspect_photo」
  也照样绿——那次记录正好就是 inspect_photo，两边算出同一个 1。**两个方向都断言**才红。
- 人脸清单的「排序第三键」最初比的是「两次调用结果相同」，在同一次实现里恒真；
  改成**插入顺序故意与 id 顺序相反**才可分辨。
- 「按 asset 过滤」最初没造任何脸，把 WHERE 删掉照样返回空表。
- 人脸清单「有名字」那条腿的判据从 `person_name` 改成 `identity_status`
  ——空名字时两条路都算得出空串。

**顺带修的两处**（都是这次改动让它们当场红的，属于门禁按设计工作）：

- `module-map` 的 `tools.py` 状态是 `cost_class`、`application/assistant/tools.py`
  依赖 `domain/media/inspection.py`；
- `test_assistant_toolbox.py::test_an_unknown_tool_is_reported_not_raised` 原来借
  `inspect_photo` 当「不存在的工具」的例子——**它现在存在了**，同一次调用变成
  `unknown_result_set`。已换成 `no_such_tool`。

**留给 T127 的口径缺口**（`test_face_manifest.py` 里有一条用例钉住它）：
1.0 的人脸清单**不过滤 `entity_type`**，而 T122 那条过滤。1.0 里非人物实体不存在所以
无差别；T127 落非人物实体后，一个绑到「乌篷船」的簇会让清单给出这个名字。
**今天不修**——改了就是偏离 1.0，那需要一个裁决。


### 8.8 T124 落了什么：read_photo_text，以及一条推翻了前提的实测

**结论先说：1.0 的 VLM OCR 回退是死代码，那段注释在撒谎。**

`ocr_tool.py:3` 的模块注释写「PaddleOCR 小模型优先，**读不到回退 VLM**」。实测：

- 两个 VLM OCR 提示词 `_OCR_PROMPT` / `_OCR_PROMPT_FULL`（`:28-29`）在全仓
  **除了定义处一次都没被引用过**；
- `ocr_tool.py` 整条链上没有任何一次模型调用（grep `gamma` 只命中一句注释）；
  `_read_photo_text_impl` 在 `_ocr_single_asset` 返回 `None` 时直接回
  「这次没能可靠读出照片里的文字。」；
- 而 `paddleocr` **确实装在 1.0 的生产 venv 里**（`.venv` 导入成功）——
  所以那 2,867 次调用跑的都是小模型，VLM 那一路**从来没跑过**。

这是 §3 那个方法论框里的第三个：`adjacency` 是注释对、代码死的；
`query_memory_facts` 是注释过期、代码活的；**这个是注释承诺了、代码里根本没有**。
而 §4 Task 124 的前提（「1.0 是两级」）正是抄了那条注释——**于是基于它做出的裁决
（「只用 8100 的 VLM」）不是「把 1.0 的现有能力收窄」，是新增一条 1.0 从没跑过的路**。

**裁决（2026-09-30，推翻上一版）：只做第一路**——读导入期已落库的 `observations.ocr_text`。
理由：1.0 的 `_ocr_single_asset` **本来也是先读它**（`ocr_tool.py:544-559`，
命中就返回、零推理），而 2.0 的导入期 `OpenAIVlmObserver` 本来就往这一列写
「图中文字，1000 字内」——所以这一路两边都有、不需要任何新东西。

**落的三个件**：

| 落点 | 是什么 |
|---|---|
| `domain/assistant/evidence_ledger.py` + `intent.py` | `EvidenceType.VISIBLE_TEXT` 与 `text` / `ocr` / `price` 三个轴词别名（1.0 `goal_planner.py:70-72`）。**在那之前这三个词是认不出来的别名**，于是「招牌上写了什么」这类问题会让规划器报 `invalid_planner_action` 并重规划 |
| `domain/media/ports.py` + `repositories/assets.py` | `ObservationRepository.latest_ocr_text(asset_id)`——1.0 那条单列 SQL 逐字（`ORDER BY updated_at DESC LIMIT 1`） |
| `domain/assistant/tools.py` + `application/assistant/tools.py` | `ToolSpec` 与 `_read_photo_text` |

**两处与 1.0 的偏离，都是「这条路径变了」的直接后果**：

1. **不收 `question`。** 1.0 的 `required_inputs` 里有它，而它唯一的用途是决定要不要对
   小模型的结果做一次补偿重识别（`_HARD_VALUE_QUESTION_RE`，`ocr_tool.py:584-593`）。
   2.0 不重识别，收一个用不上的参数只会让模型多花几个 token 去填它。
2. **`cost_class` 是 `CHEAP`，不是 1.0 的 `expensive`。** 1.0 标昂贵是因为它真跑一次
   OCR 引擎（CPU 推理、占那个共用的 `inspections` 计数器）。照抄的后果不是「保守一点」，
   是**一次免费的读库会把 `inspect_photo` 那唯一一次精读预算吃掉**。

**工具描述按裁决如实收窄**：删掉「内部会把照片切块放大后 OCR」，换成「文字是导入时由
视觉模型抽取并落库的，本工具不再重新识别：没存下就如实说读不到，**不要据此断言照片里
没有字**」。最后那句是必要的：模型拿到空的 `full_text` 会倾向回答「照片里没有文字」，
而**「我们没读到」与「图上没有」是两件事**。

**用例与变异**：16 条新用例，**9 条定点变异全部变红**（含一条钉住 `cost_class=CHEAP`
与一条钉住「不收 question」——**偏离也要有门禁**，否则下一个人会顺手改回「照抄 1.0」）。


### 8.9 T125 落了什么：channel_trace（S6 的验收条件）

§8 给 S6 的验收条件是一句「证据包和 trace 能展开」。核查把它落到了具体的一格：
1.0 的 `EvidencePacket.channel_trace`（`evidence_retrieval.py:33-66`）。

**为什么「每候选的 `channels`」不够。** 2.0 原来只有那个（转置视角：这个候选被哪些通道
以第几名召回）。它分不开两件事：

- `text_ann` 跑了、一张都没召回 → 没有候选带这个键；
- `text_ann` **根本没跑**（嵌入器不可用）→ **也没有**任何候选带这个键。

读数完全一样，而 S6「六路取证补齐」要证明的恰恰是「这一路真的在跑」。1.0 那条注释
写得比字段名清楚：*per-channel recall trace so API/benchmark can prove each retriever
was invoked (or give an explicit unavailable reason)*。

**四个件**：

| 落点 | 是什么 |
|---|---|
| `domain/retrieval/models.py` | `ChannelTrace`：`channel` / `status` / `candidate_count` / `latency_ms` / `backend` / `reason` |
| `application/retrieval/retrieve_evidence.py` | 五路各一条 trace；**通道抛异常时照样留一条带原因的**（1.0 那个 `except` 分支的逐字对应） |
| `api/schemas/search.py` + `api/routes/search.py` | 响应多一块 `channel_trace`（`packages/contracts/openapi/sentrix-v1.yaml` 已重新生成） |
| 用例 | 单元 9 条（桩件控失败与降级）+ 集成 3 条（真接线上五条真 trace） |

**`status` 三个取值**：`ok` / `degraded` / `error`，**非 `ok` 时 `reason` 必须非空**
（一条不变量用例钉住）。降级与失败分开是必须的：降级说明这一路**跑到了**、只是后端换了
（`backend` 那一格说明换成了什么）——合成一个状态会让「修后端」与「修接线」分不开。

**两处与 1.0 的差异**：

- `embedding_ms` / `embedding_events` **不搬**：那是 1.0 `embedding_router` 的内部计时
  事件，2.0 的嵌入走 `VisualEmbedder` / `TextEmbedder` 端口，没有那套路由器，
  搬过来只会是一个恒空字段；
- 1.0 的 `retrieval_timing` 与 `channel_trace` 是两块，2.0 合成一格 `latency_ms`
  ——它们在 1.0 本来就是同一个循环里落的。

**定点变异 8 条全部变红**，其中一条抓到了实现里的真 bug：两个向量通道原本在 `record()`
**之外**被调用（健康度要跑完才知道，早先写成当参数先算），于是它们抛异常时**整次检索
直接挂掉**——而「一个通道挂了不该让整次检索挂掉」正是 1.0 那个 `except` 的全部意义。
用例先红、才改的实现。这一条写进了 `record` 的 docstring。


### 8.10 T126 落了什么：L2 的第二层，以及一处「不补」

**核查先定名。** 1.0 的「双层防伪」是**两层**，名字与分工都写在
`judge.py:3-12` 与 `final_guard.py:1-18` 上：

| 层 | 是什么 | 做什么 |
|---|---|---|
| **L1** `final_guard.py` | 确定性规则 | 只做结构性兜底：安全 / 占位符 / 交付完整性 / 流程结构。**明说不做语义等价**（「三个人」与「3」是等价的） |
| **L2** `judge.py` | **一次模型调用**（12B Observation Faithfulness Judge） | 语义级合格性：编造 / 矛盾 / 漏报 / 过度声称 / 缺口披露 |

**2.0 的两层都在**（`domain/assistant/final_guard.py` 与 `domain/assistant/judge.py` +
`application/assistant/judge.py`，`run_turn.py:480` 接线）。逐条对 1.0 的口径，
2.0 已经忠实的有：每问只评审一次（`session.judged`）、有工具结果才评、
评审计一个模型步、看完整轨迹、**severity 分层**（只有 `_TRUTH_TYPES`
= fabrication / contradiction / omission / certainty_upgrade 才退回重写，
`missing_disclosure` 只是披露建议）。

**一处真缺口：放行时的 `l2_soft_release` 打标。**

1.0 在**判不实那一刻**落标记（`runtime.py:2786-2788` 的 `turn.l2_unverified` /
`l2_unverified_reason`），在**放行那一刻**发标（`:2961-2968`：一条 `type="judge"` /
`status="soft_release"` / `possibly_fabricated: True` 的 step）。中间隔着一次软引导，
而软引导之后**不再复评**——所以这条标的意思是「**这条答案带着一次未复评的不实判决出门了**」。

**2.0 只有那句注释**：`domain/assistant/judge.py:13` 在 docstring 里引用了
1.0 轨迹里 `agent2_trace.quality.l2_soft_release = true` 的出现率（6.6%），
**而全仓没有任何代码产生它**（`grep possibly_fabricated|soft_release|l2_unverified`
在 src 里只有那一行 docstring）。

没有它，「评审放行的答案」与「评审通过的答案」在轨迹上完全一样，
而评测与排查要区分的正是这两者。两半都落了：`_Session` 加
`l2_unverified` / `l2_unverified_reason`，`_review_with_l2` 判不实时落标记，
`_judge_answer` 放行时发 `l2_soft_release` 事件。

**一处核完判定「不补」：`trusted_facts`。** 这是同一个方法论框里的**第四次**，
而这次方向又不同（前三次：`adjacency` 注释对代码死；`query_memory_facts` 注释过期代码活；
T124 注释承诺了代码里根本没有）：

1.0 的调用点算了两组「可信事实」（`_confirmed_facts(task) + _trusted_facts(task)`，
`runtime.py:1237-1260` 与 `:1205-1235`）塞进评审。看上去 2.0 缺这一整块。**实测不是**：

- 那两组事实只喂给一个 `facts_block`（`judge.py:127`），而 `facts_block`
  **只在 `else:` 分支里被引用**（`:154`，与 `obs_lines` 一起）——那个分支的前提是
  **没有 `messages`**；
- 唯一的生产调用点（`runtime.py:2769`）**总是传 `messages`** → `if messages:`
  恒真 → 那个分支**在生产上从不执行**；
- 而 1.0 自己在那段代码上方写了理由：*「完整轨迹优先：评审能看到工具返回里被压缩投影
  丢弃的字段（captured_at/地点/preview 等），**不会被『可信事实白名单』误导**」*。

**2.0 的 `build_user_prompt` 实现的正是真正跑的那一支**（轨迹优先、最近 8 条、每条 900 字），
连理由都逐字搬了。所以这里 2.0 已经忠实，补 `trusted_facts` 等于把一条 1.0 在生产上
没走过的路建起来——那正是 T124 那条裁决否掉的做法。

**用例与变异**：`test_run_turn.py` 三条（既有那条加了打标断言 + 两条新用例：
评审通过不打标、风格问题不打标），**定点变异 6 条全部变红**。
后两条用例是必需的——只断言「判不实时会打标」的话，一个**永远打标**的实现也能过，
而那种标什么都说明不了。


### 8.11 T127 落了什么：非人物实体（迁移 0008 + 抽取）

**裁决：甲**（2026-09-30）——只做四类实体与属性，两条关系留给 S7。理由与核查全在 §4 Task 127。

**落的件**（两个提交：迁移、抽取）：

| 落点 | 是什么 |
|---|---|
| `migrations/0008_semantic_entities.sql` | `semantic_profiles` + `semantic_claims` + `idx_semantic_claims_person`，列集照 1.0 `db.py:811-849` 逐字 |
| `domain/identity/extraction.py`（新） | 纯规则：`OBJECT_CATEGORIES` 六类词表、`object_category`、`time_semantics`、`build_entity_specs`、`EVENT_RELATIONS` |
| `application/media/maintain_entities.py`（新） | `MaintainObservationEntities`：建实体 → 挂 `entity_observations` → 写属性 → 挂 `event_entities` |
| `EntityRepository` +3 | `find_or_create_entities` / `link_observation` / `maintain_property_values` |
| `EventParticipantRepository` +1 | `upsert_entity`（**从 `_rehome_entities` 里那段私有形态提升**——那段注释写着「等 S6 有了写入者再开口」，T127 就是它） |
| `ImportAssets` +1 协作者 | `entity_extractor`，位置照 1.0 `pipeline.py:336`：**事件判定之后** |

**三处「1.0 也这样」的留白**（都不是省略，各自有代码依据）：

1. **`atmosphere` 那一类不建**：它的两个来源（语义词表与 `emotions`）2.0 都没有，
   而 1.0 在词表不可用时同样产不出名字。
2. **`visual_place_descriptions` 不写**：1.0 的条件是
   `visual_place and (visual_place != scene_type or has_semantic_place)`，
   无语义词表时 `scene_type` 就等于 `visual_place`、`has_semantic_place` 为假 ——
   **合取恒假**。地点在 2.0 的输入下写的是 `geo`（← asset 的 GPS）与 `scene_type`。
3. **`semantic_primary` / `semantic_details` 不写**：1.0 把它们关在
   `if has_semantic_place:` / `if spec:` 里面。

**用例 53 条，定点变异 12 条里 11 条变红**，另一条是**等价变异**（`find_or_create` 里那句
去重与后面的 `found` 检查冗余，行为完全一样——是防御性代码，不是门禁）。

**变异先抓到、我再补的三处**：

- `maintain_property_values` 的合并最初写成 `[str(item) for item in values if str(item)]`
  ——**`str(None)` 是 `"None"`（真值），于是 `None` 会以字符串形态进列表**。
  1.0 是按**原始值**判真假、不字符串化。用例先红、才改的实现。
- `link_observation` 的 `ON CONFLICT ... DO NOTHING` 与 `DO UPDATE SET confidence=MAX(...)`
  **在行数上完全一样**，只断言行数时互换是观察不到的；补了置信度那条断言。
- `test_object_category("红色外套")` 是我写错了期望——`外套` 里没有词表里的「衣」。

**顺带**：`EntityRepository.maintain_property` 的零调用登记**被反向规则自己删掉了**
（T127 的抽取要写 `label`/`category`/时间属性）——它原来登记的是「Task 128 接线」，
实际是 T127 接上的。`list_properties`（读）仍等 T128。


### 8.12 S6 完成（2026-10-01）

**S6 全部任务已了结。** 本切片最后一次全量验证：静态门禁全绿
（ruff / format / mypy / arch 五契约 + 两条可达性 / check-docs 含 OpenAPI 契约），
`make ci` 的 coverage 一步 **1836 passed / 1 failed / 29 skipped**，
`make web-check` 单独补跑通过（tsc + oxlint + 38 个 vitest + vite build）。

**那一条 failed 是已知的模型侧 flaky**，不是回归：
`tests/e2e/test_assistant_turn.py::test_the_answer_never_leaks_internal_identifiers`
（§8.1 记着「不要修」，上一次全量跑失败的是同一条）。`make ci` 会因此在 coverage
那一步停住、`web-check` 不跑——**跑完 coverage 记得单独补一次 `make web-check`**。

#### 本切片最后三段（T128–T132）的清单

| 任务 | 落点 | 关键判断 |
|---|---|---|
| T128a | `GET /entities` + `PUT /entities/{id}/properties/{key}` | 计划判它是「薄映射」——**错**，装饰是七条批量聚合；于是按**薄 / 厚**切而不是按端点切 |
| T128b | `GET /entities/{id}` | 计划说「九个子查询」——**错**，是**十二个键**；搬九个，`relationships` 留 S7、`profile`/`claims` 留 T130 |
| T129 | `entity-groups` 两条路由 | 四级语义标签短路；**单成员组也返回**；emotion 那一支按 T127 的记录留白 |
| T130 | `GET /knowledge` | `semantic_profiles` / `semantic_claims` 的**第一个读者**；`scope_id` 的过滤在 `LIMIT 1000` **之后** |
| T131 | **不做** | 核查发现 1.0 **从未**把非人物实体接进检索——不是接线是新建，按「不新增 1.0 没有的设计」裁决不做 |
| T132 | capability-map 加反向规则 | 前三条规则都是单向的，抓不到「其实验过、却还标着 pending」；两条卡住的登记改正 |

#### 三条留给 S7 的显式后续

1. **T127 的两条关系**（「出现在」/「记录于」）——`relationships` 表归 S7。
2. **`GET /entities/{id}` 的 `relationships[]`** 与 **`GET /entity-groups/{id}` 合并的 `relationships`**——同上，两个键都不进响应。
3. **`relationship_count`** 在 `GET /entities` 与 `entity-groups` 两处都不进响应（恒 0 会让「S7 还没做」读起来像「确实没有关系」）。

#### 下一步

S6 之后是 **S7（家庭关系与关系图谱）**。开工前照旧：先把上面三条后续接上，
再读 S7 的计划或设计文档 §8，并**按本切片的教训先做依赖核查**——
本切片五次「任务定义被核查推翻」，其中三次是计划里的**汇总性描述**错了
（「薄映射」「九个子查询」「零调用」），一次是「1.0 有而任务表没有」，一次是方向相反。
