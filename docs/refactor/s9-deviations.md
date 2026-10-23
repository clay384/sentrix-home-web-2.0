# S9 违背 / 偏离审计（PhotoBench 后端重构）

审计对象：`docs/refactor/2026-10-04-s9-implementation-plan.md`。
基线：设计文档 `docs/architecture/2026-09-21-sentrix-home-web-2-implementation-design.md:514`
对 S9 的定义，与 1.0 的实现（`services/photobench/backend/benchmark_orchestrator.py`，实测 7,997 行）。

**这份文档不改写已经写在别处的偏离，而是把它们汇总成一份可查的登记。** 大量条目
已经在计划书 §5 的施工记录与 `behaviour-baseline.md` 里逐条写过——这里给出**索引 +
一句话理由**，细节指回原处。**只有 S9c 那一段是本切片的首次登记。**

---

## 0. 一句话摘要

S9 把 1.0 那个 7,997 行的 stdlib 单文件重写成 2.0 的 FastAPI 分层服务（`apps/photobench/`），
**不是搬运**：S9b-1 实测 2.0 的 `apps/photobench/` 当时零代码。全切片四段全部完成
（S9a 遥测 → S9b-1 骨架 → S9b-2 主链 → S9b-3 judge/聚合/写侧 → S9c 适配与收口）。

**偏离按来历分三类**，每一类都能指到 1.0 的具体行或本仓库的一条既有契约：

1. **分层逼出来的形状变化**（端口换 Protocol、httpx 不进 domain、PIL 只在 infrastructure）——
   不是口味，是 `photobench-*` 契约机械判定的结果。
2. **2.0 的接口事实**（导入收服务端目录、种身份单人、问答两段式、scope 由导入内部 ensure）——
   每条都核过 2.0 的代码。
3. **用户裁决**（照搬全量；不加主 API 端点、改 PhotoBench；换模型 = 重启 2.0；
   与 `apps/api` 同一套门禁）。

**没有一条偏离是凭喜好的。**

---

## 1. 与设计文档的偏离

| # | 项 | 设计文档 / 计划书 | 实际 | 为什么 |
|---|---|---|---|---|
| 1 | 行数 | 计划书写 1.0 是「7,751 行」 | 实测 **7,997 行** | 文档比代码旧（S10 那个 3,646 同样应为 3,797）。S9a 已记 |
| 2 | 「重构」的隐藏工作量 | 没算 `backend/runtime_providers.py`（869）+ `apple_telemetry.py`（418） | **1,287 行要在 2.0 重建** | 计划书 §0.2 |
| 3 | S9c 的范围 | 计划书写 T921 端点适配 + T922 死端点 + T923 重启 + T924 前缀 + T925 收口 | **T921 提前到 S9b-2 做掉**（直接按 2.0 的接口写，不返工两遍）；T924（PhotoBench 自己的前缀 `/api` → `/api/v1`）**未做**，见 §8 | 用户 2026-10-04 定的范围调整 |
| 4 | `results/` 的 270 个 run | 计划书 §3.6 提出「要不要迁进 2.0」 | **没迁**。`results/` 与 `datasets/` 一样是本地数据（`DATASET.md` 声明不入 Git），2.0 允许用 `PHOTOBENCH_RESULTS_ROOT` 指过去 | 迁移不是本切片的验收项；验收只要求「能对 2.0 跑完一轮评测」 |

---

## 2. 修 1.0 的缺陷（不是偏离，是**不把缺陷带过来**）

| # | 1.0 的缺陷 | 2.0 怎么修 | 登记处 |
|---|---|---|---|
| 1 | **死端点 404 循环**：1.0 PhotoBench 轮询 `GET /api/arbiter/status`，该端点在 1.0/2.0 主 API **从不存在**（1.0 主 API 里 `arbiter` 零命中）；158,555 次全部 404，占 8091 流量 **17%** | **2.0 没有这个能力 → 删除**：S9b-1 只建真正需要的路由，这条**从未加过**。用例 `test_no_route_serves_the_two_dead_endpoints` + `test_the_sentrix_client_has_no_arbiter_or_batch_cancel_capability` 钉住 | 本文件 §7.1 |
| 2 | **第二个死端点**：`POST /api/ingest-batches/{id}/cancel`（1.0 `:2265-2277`，设计文档没提）。1.0 的 ingest-batches 只有 POST/GET/`complete`，**没有 `/cancel`**；调用点被 `except Exception` 吞掉，静默 404 | **确认 2.0 没有等价物 → 删除**：2.0 的 `POST /api/v1/imports` 是**同步**请求，没有批量实体可取消。用例同上 | 本文件 §7.2 |
| 3 | **`POST /api/v1/imports` 在「先种过身份」的 scope 上必然 500**（`person_moments` 的外键约束，S9b-2 真机撞出来的） | **不在 S9 范围，未修**——它是 `apps/api` 的产品缺陷，改它要重新裁决。真机验收用「先预热 scope 让导入全程去重」绕过 | 计划书 §5 · S9b-2「一个 2.0 缺陷」 |

---

## 3. 1.0 的历史包袱 / 只服务某种部署形态的东西，**怎么处置**

| # | 项 | 处置 | 理由 |
|---|---|---|---|
| 1 | `OrinLlamaCppTelemetryProvider`（端点/ssh 用户/PID 路径写死成「模型在 118、PhotoBench 在 153」） | **保留** | 用户裁决①「照搬全量」。2.0 若两者同机它是死重量，但那是**部署决定** |
| 2 | `LocalAppleUnifiedTelemetryProvider`（只对 Mac M4 成立） | **保留** | 同上。provider 可被替换——这正是把它做成端口的意义 |
| 3 | `backend/jetson_telemetry.py`（660 行） | **搬到 `infrastructure/runtime/jetson_telemetry.py`**（S9b-3） | 1.0 的 `runtime_providers.py` import 了它两个名字却从未使用；真正构造 Jetson provider 的是 `select_runtime_providers` |
| 4 | `OrinLlamaCppTelemetryProvider` 在**选择逻辑里没有被任何分支构造**（import 了但没用，F401） | **照 1.0 的分支搬，不替它补一条没人验证过的 ssh 采样路** | 计划书 §5 · S9b-3「一条与任务书措辞不符的实测」 |
| 5 | `backend/hardware.py` 的 `collect_gpu` / `collect_cpu` / `collect_memory` / `collect_hardware` | **不搬** | 实测它们**只被 1.0 的 `backend/app.py` 调用**，`benchmark_orchestrator.py` 一次都没调 |
| 6 | 1.0 的 ABC 基类（`raise NotImplementedError` + `token_count` 默认值） | **换成 Protocol**，默认值落到唯一实现 | 分层契约要求 domain 不碰实现细节 |
| 7 | `chat` / `chat_stream` 返回 `httpx.Response` / httpx stream 上下文 | 端口改收「解析后的 JSON」与「逐行的迭代器」 | httpx 在 `domain-purity` 的 forbidden 名单上 |
| 8 | `mode` 支持 `full` / `reuse` / `build` | **只接 `full`**，其余显式 400 | 2.0 删掉了 `scope_attach`，`reuse` 没有可复用的入口；静默降级会建出调用方想避免的新 scope |
| 9 | `POST /api/runs/{id}/resume` | **501** | 要重建 `mode=resume` 的 run + `existing_scope_id`，2.0 没有那条路 |
| 10 | 云端大模型（`use_cloud_model` / `big_model`） | 分支保留、**没有生产入口** | 2.0 没有云端模型这个概念 |

---

## 4. S9a 的偏离（T901–T904：运行时提供者与遥测）

细节见计划书 §5 · S9a「偏离与待裁决」13 条 + 「带出来的待定项」5+3 条。摘要：

- **1.0 那两个模块其实有测试**（计划书 §2 说「它们没有测试」不准确）；2.0 的用例是**重写**（改用假件对象）。
- **`SYSTEM_PROCESS_PORTS` 的默认值改指 2.0 的 9597 / 9598**（1.0 是 8771 / 8091 / 8500）。
- **Apple 那份 `SYSTEM_PROCESS_KEYWORDS` 不与 providers 合并**（多 `mlx`、不读 env；合并会悄悄改变归属结果）。
- **`_process_smaps_field_mib` 结尾不可达的 `return None` 没搬**（行为等价）。
- **`test_architecture_rules.py` 的 `CONTRACT_COUNT` 5→8**：它写死的是**全仓契约条数**，而 `lint-imports` 一次跑两个包。
- **门禁盲区如实登记**：`start` / `stop` / `list_models` / `gpu_stats` / `process_memory` 因为 photobench 自己的实现里就有 `self.*()` 调用，被判成「已接线」而不在登记表里——**那是漏报**。

---

## 5. S9b-1 的偏离（T911–T914：骨架与 HTTP 层）

细节见计划书 §5 · S9b-1。摘要：

- **T914 的实测否定了「懒加载」的设计直觉**：对 463 MB 的 `run.json`，纯 Python 扫描比直接 `json.loads` **慢 8 倍**。所以选「照常解析、丢掉 items」+ 启动后台扫描。
- **顺带修正一个文档数字**：1.0 脚本注释写「约 55 秒」就绪，实测**75 秒**。
- **两条存活变异都是夹具不具区分力**（路径穿越、`scope_id` 早退）——本项目第五次同一教训。
- **`ModelProfiles` 的方法改名 `query_profiles`**：否则会把 `LifecycleProvider.profiles` 挤进 method-reachability 的重名盲区。**拿一条查得到的不变量换查不到的，门禁顶回来了**。

---

## 6. S9b-2 的偏离（run-execution 主链）

细节见计划书 §5 · S9b-2「偏离与待裁决」9 条 + behaviour-baseline 的 S9b-2 段。摘要：

- **`_phase_processing` 的「空导入」判据换了**：1.0 是 `import_accepted_count == 0`，2.0 是「这个 scope 里有没有资产」（2.0 的导入会报 `deduplicated`）。
- **没有后台 persist 写线程**（S9b-3 才补）与**没有取消**（S9b-3 才补）。
- **三处刻意偏离 1.0 的行为**：id 一律 `quote(safe="")`、multipart 的非 2xx 转文案、超时分类多一条 `isinstance`。逐条在 behaviour-baseline。
- **`model_deploy` 的第 4 步（`bind-runtime`）留空**——那是 S9c 的 T923（见 §7.3）。
- **真机验收绕过了一个 2.0 缺陷**（见 §2 第 3 条）。

---

## 7. S9b-3 的偏离（judge / 聚合 / 写侧）与 S9c

S9b-3 的细节见计划书 §5 · S9b-3；behaviour-baseline 的 S9b-3 段逐条写了常量与行为。
摘要：`mode` 只接 `full`；`resume` 返回 501；重判的轮次来源从 `conversation` 换成
`runtime_turns`；重判不重判证据项；内存画像不调 `bind-runtime`；**证据判分要缩放 JPEG，
`JudgeRunner` 里内联它会红在 `photobench-application-purity`（PIL）——搬到
`infrastructure/http/judge_images.py`，惰性 import 也照样判红**。

### 7.1 `GET {sentrix}/api/arbiter/status`（T922）——**删，因为它从来没有过**

| 项 | 内容 |
|---|---|
| **核对** | 1.0 主 API（`services/photobench/backend/`）里 `arbiter` **零命中**；`docs/runbooks/153-services.md:68` 与设计文档 `:559` 都记着这个 404 循环（占 8091 流量 17%）。1.0 的 PhotoBench 侧有两个东西：`arbiter_status_snapshot()`（`:762`，打主 API）+ 它自己的路由 `/api/arbiter-status`（`:7576`，给 1.0 前端轮询）。 |
| **2.0 的状态** | 三样**都不存在**：`photobench/` 里 `arbiter` 零命中；S9b-1 只建了 2.0 真正需要的路由；**2.0 的 PhotoBench 没有前端**（没有 `dist/`、没有 `.ts`）。所以「删除」在这里的表现是**从未加过**。 |
| **处置** | 删除（不修）。**加一条会红的断言钉住「从未加过」**：`test_api_routes.py::test_no_route_serves_the_two_dead_endpoints` 扫 `app.routes`，出现含 `arbiter` 的路径即红；`::test_the_sentrix_client_has_no_arbiter_or_batch_cancel_capability` 扫端口方法名。 |
| **为什么是删不是修** | 2.0 主 API 也没有 arbiter 能力（计划书 §0.4 核对属实）。修它要凭空造一个不存在的能力；`unsupported` 兜底分支本身正是「静默 404」的成因。 |

### 7.2 `POST {sentrix}/api/ingest-batches/{id}/cancel`（T922）——**删，因为没有等价物**

| 项 | 内容 |
|---|---|
| **核对 2.0 的导入取消语义落在哪** | **没有落点。** `apps/api/src/sentrix/api/routes/timeline.py` 的 `POST /api/v1/imports` 是**同步**请求：一次 HTTP 跑完「落盘 → EXIF → 地理 → 视觉向量 → 观察 → 人物 → 事件 → 事实 → 词法 → 批末总结」整条链（`timeline.py:130-142`）。没有任务队列、没有 batch 实体、没有 job id 可取消。全仓 grep `cancel` 在 `api/routes/` 下没有任何导入相关的取消端点。 |
| **1.0 的状态** | `_cancel_remote_batch`（`:2265-2277`）打 `{sentrix}/api/ingest-batches/{id}/cancel`，而 1.0 的 ingest-batches 只有 POST / GET / `complete`——**该端点从不存在**；调用点被 `except Exception` 吞掉。 |
| **处置** | 删除。**2.0 的 `RunExecution.cancel` 一次主 API 调用都不发**——用例 `test_run_execution_cancel.py::test_cancel_never_calls_a_remote_batch_endpoint` 断言 `client.calls == []`；照搬 1.0 那段会立刻红。 |
| **连带清理** | 1.0 的两个兜底分支（`unsupported` / `except Exception`）与它的前端契约**在 2.0 从未被移植**，无需删除动作。**若将来 2.0 给导入加异步化**（任务队列），取消语义要从头设计——不是恢复这个端点。 |

### 7.3 换模型 = 重启 2.0（T923，**用户裁决③**）

| 项 | 内容 |
|---|---|
| **1.0 怎么做** | `POST /api/model-profiles/bind-runtime`，注释写明 "Sync .100 Sentrix gamma client to the new model (**no restart**)"（`:2611`）。`_phase_model_deploy` 第 4 步与 `start_memory_profile` 各调一次。 |
| **2.0 为什么不能照做** | 2.0 的 `vlm_base_url` 是**启动时从 Settings 读死的**（`apps/api/src/sentrix/config.py` 的 `SENTRIX_VLM_BASE_URL`，`Container.build` 里 `ServedModel(settings.vlm_base_url)`）；`ServedModel` 只有**模型名**带 TTL 重探与 `invalidate()`，**base_url 换不了**。 |
| **2.0 改成什么** | PhotoBench **重启 2.0**，带上新的 `SENTRIX_VLM_BASE_URL`。三个模块：`domain/deploy/ports.py`（端口 + `RestartOutcome`）、`application/deploy/service.py`（`ensure_endpoint`：端点已对就不动进程）、`infrastructure/deploy/sentrix_process.py`（子进程 + 信号 + `lsof` 反查 + 健康轮询）。接在 `_phase_model_deploy` 的**两个分支**上（1.0 只在有 Manager 那一支做——而 153 上跑的正是没有 Manager 的外部端点那一支）。 |
| **与 S11 的重叠（计划书 §1 要求登记）** | S11 要做「主 API / Web / PhotoBench / model-manager 独立启停」，这里的重启逻辑到 S11 可能被接管或收敛。**隔离办法**：端口 / 应用服务 / 适配器三块，业务逻辑只看见 `ensure_endpoint` 一个方法；**三块的边界与三条部署假设（同机 `make dev`、pid 归属、健康判据）逐个写在 `domain/deploy/__init__.py` 与 `infrastructure/deploy/sentrix_process.py` 的模块注释里**，S11 接管时只动这三块。 |
| **偏离 1.0 的一处（刻意）** | 1.0 的 bind-runtime 失败**非致命**（收进 `gamma_sync.error`，run 继续）。2.0 重启失败**致命**——2.0 的助手用**自己的** `vlm_base_url` 答题，重启失败意味着每个答案都来自另一个模型，继续跑只会产出「标着 A 模型、其实是 B 模型」的结果。 |
| **没做的** | `start_memory_profile` 里的同一步**仍然留空**：那条路径的请求体里只有 `vllm_manager_url`，**没有端点来源**。与其猜一个端点去重启主 API，如实记 `gamma_sync.ok=false`（理由从「S9c T923 未做」改成「这条路径没有端点来源」）。 |

### 7.4 photobench 自己的 openapi 导出（T925 的一部分）

`sentrix` 的契约是 `packages/contracts/openapi/sentrix-v1.yaml`（`scripts/export_openapi.py`）。
PhotoBench 是**第二个发行包**，另起 `scripts/export_photobench_openapi.py` 导出到
`packages/contracts/openapi/photobench.yaml`，并挂上 `make check-docs`。

**文件名没有 `-v1`**：PhotoBench 的路由前缀仍是 `/api`（S9c 未做 T924，见 §8），
文件名跟着前缀走，避免「名字说 v1、路径其实是 /api」的自相矛盾。

**为什么是第二个脚本而不是给第一个加 `--app` 开关**：两个 `create_app` 没有共享入口，
开关要把「插哪个 src 根 / import 哪个工厂 / 写哪个目标」三件事都参数化，而那正是两个
脚本各自不同的**全部**内容。

### 7.5 真机验收抓到的两个缺陷（都是「假件看不见」的接缝）

**这两个都不是计划里的偏离，是施工/验收过程中真的修掉的缺陷。** 记在这里是因为它们
说明「跑完一轮」这条验收为什么非真机不可。

| # | 缺陷 | 怎么暴露的 | 修法 |
|---|---|---|---|
| 1 | **`_discover_by_port` 会把客户端一起捞出来**：``lsof -ti tcp:PORT`` 列出**所有**用这个端口的进程——包括连着它的客户端。httpx 的连接池默认 keep-alive，于是 PhotoBench 自己、Web 前端、任何正在调 2.0 的进程都会被列进「要停的进程」，被 ``_terminate`` 一起 SIGTERM 掉 | **验收驱动脚本被自己触发的重启杀掉了**（静默 SIGTERM，没有堆栈）：它在 Part A 用自己的 httpx 客户端探过 9598 健康，连接留在池里；run 的 ``model_deploy`` 重启 2.0 时把它也列进了发现表 | 加 ``-sTCP:LISTEN``，只停**监听**进程。用例 `test_the_port_discovery_only_looks_at_listeners` 钉住这个参数 |
| 2 | **读侧状态缓存不失效**：``ReadRuns._state`` 把按需解析的 run 状态缓进 LRU 2，从不失效。一个 run 在「还在跑」时被读过一次，之后**哪怕它已经完成**，``GET /api/runs/{id}`` 也一直回那份旧快照 | **验收脚本按 API 轮询，run.json 已经 completed、八个相位全 done，API 一直回 running**，脚本白等了十分钟（同一次验收里磁盘与 API 给出两个矛盾的答案） | 缓存**只对终态生效**：非终态（pending/running/cancelling）每次重新解析并摘掉旧缓存。用例 `test_a_running_run_is_not_served_from_a_stale_cache` 钉住 |

**两条都是 S9b-1/S9c 自己写的代码**（不是 `apps/api` 那个不在范围的缺陷）。第 1 条尤其
说明「真机跑一轮」的价值：单元用例把进程原语全注入了，`_discover_by_port` 这个**真实现**
在假件路径上根本不被执行。

---

## 8. 没有做的 / 做不了的

| # | 项 | 状态 |
|---|---|---|
| 1 | **T924：PhotoBench 自己的路由前缀 `/api` → `/api/v1`** | ~~未做~~ → **2026-10-07 做掉了。** 当时不做的唯一理由是「前端一行不动是 S10 的范围」——S10 完成后条件就具备了。改动：后端 `API_PREFIX`、前端 `BASE`、49 处 URL 字面量（含测试期望）、vite 代理、`packages/contracts/openapi/photobench.yaml` 重新生成。实测 `/api/v1/runs` 200 / 旧前缀 404。**注意 import 路径里也含 `/api/` 字样（`from "../api/client.ts"`），批量替换必须只匹配「引号紧跟 `/api/`」的 URL 形态**——第一版正则没区分，把 80 个文件的模块路径改成了 `../api/v1/…`，被 `tsc` 当場抓住。 |
| 2 | `start_memory_profile` 的换模型同步 | 见 §7.3「没做的」。 |
| 3 | 1.0 的 8 个测试文件（约 71 KB） | **没有照搬**。2.0 的用例是重写（假件对象而非 `unittest.mock` patch 目标），覆盖主链、judge、遥测、写侧。 |
| 4 | `results/` 的 447 MB 单文件问题（T914） | **量过、有结论**（扫描比解析慢 8 倍），真机验收的 run 只有 21 KB，没有在真实大文件上再压。 |
| 5 | 前端 | **一行没动**（S10 的范围）。 |
| 6 | `POST /api/v1/imports` 的「先种过身份必然 500」缺陷 | ~~未修~~ → **2026-10-07 修了**（commit `f10cfb5`）。当时判「不在 S9 范围」，真机验收用「先预热 scope 让导入全程去重」绕过；2026-10-07 跑 PhotoBench 评测时同一个缺陷（`person_moments` 外键）变成硬失败——`photo_import` 阶段 500、整轮评测失败，绕不过去了。修法：`extract_moments._store` 里四个必填引用列任一为空就跳过那一条瞬间。 |
| 7 | method-reachability 的五方法盲区 | **未修**（修它要换判据，不在本切片）；`restart` / `running_endpoint` 两个新端口方法**不在盲区里**（实现类在 `infrastructure/`，按位置被排除，端口名唯一且被 `application/deploy` 属性访问）。 |
