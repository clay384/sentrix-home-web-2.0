# S10 实施计划：PhotoBench 前端重构

> 路线图出处：设计文档 `docs/architecture/2026-09-21-sentrix-home-web-2-implementation-design.md:515`
> ——「**S10 | PhotoBench 前端重构 | 3,646 行 Vue 单文件 → React/TS | 评测控制台可用**」。
> **实测是 3,797 行**（文档陈旧，与 S9 的 7,751→7,997 同一类偏差）。
>
> 前置：S9 已完成（PhotoBench 后端在 2.0 跑起来了，9597，有自己的 openapi 契约）。
>
> **用户 2026-10-05 裁决：这一轮只做核查与计划，动工前停。**

---

## 0. 核查结论（2026-10-05）

### 0.1 1.0 那边的形状

| 项 | 值 |
|---|---|
| 位置 | `services/photobench/frontend/` |
| 主文件 | `src/App.vue` **3,797 行**（脚本 `:1-2836` 74.7% + 模板 `:2838-3797` 25.3%，**无 `<style>` 块**） |
| 样式 | `src/styles.css` 881 行 |
| 栈 | Vue 3.5 + **echarts 6**，**纯 JS 无 TypeScript** |
| 工程 | **无 lint、无测试、无 test 脚本** |
| 构建 | Vite 8；dev 端口 5173，`/api` 代理到 `${BENCH_PORT\|8771}` |
| 杂物 | 6 个 `App.vue.bak-*` 备份文件（未分析） |

**难度分布**（S10b-0 核查后的实测，替换动工前按 grep 的粗估）：

| 类别 | 量 | 说明 |
|---|---|---|
| 纯格式化/标签 | ~250 行 | 可机械翻译 |
| **trace / 时延 / 调试解析** | **470 行**（`:1340-1809`） | 把 JSON 轨迹算成「轮 → 调用列表」与色块数组 |
| **图数据构造** | **约 390 行** | `:1810-2055` 的 246 行 + `:32-175` 两个巨型 computed 的 144 行 |
| 指标汇总 | 122 行（`:426-547`） | `effectiveRunSummary` 现算 60+ 字段 |
| 网络与状态编排 | ~600 行（`:2233-2835`） | 重写核心 |
| 遥测 / 图表 | ~365 行（`:785-1150`） | 唯一有命令式生命周期的一块（echarts） |

**动工前我把 `:1340-1809` 标成了「trace 解析与图数据构造」——「图数据构造」那半是错的。**
实测那 470 行**没有一行构造图数据**，全是 trace / 时延 / 调试轨迹的解析与格式化；
真正的图数据构造在 `1810-2055`（246 行）。两块都重、都易错，但**性质不同**
（前者是「JSON 轨迹 → 表格行」，后者是「后端算好的指标 → 分层数组」），
拆文件时不该并成一个。

**另外漏算了 `:32-175` 的两个巨型 computed（144 行）**——`memoryValidityArchitecture`
与 `memoryRecallMatrices`，它们同样是图数据构造，只是位置在文件开头，按区块 grep 容易漏。

### 0.2 **7 个后端端点 2.0 没有**（这是 S10 的前置缺口）

| 端点 | 用途 | 2.0 |
|---|---|---|
| `GET /api/runs/{id}/keyframe-analysis` | 关键帧分析面板 | ❌ 全仓无实现 |
| `GET /api/runs/{id}/memory-effectiveness` | 记忆有效性 / 错误链 | ❌ |
| `GET /api/memory-spaces` | 复用相册基座列表（带 `sentrix_url`） | ❌ |
| `GET /api/runs/{id}/export-trace` | 导出 trace（`window.open` 下载） | ❌ |
| `POST /api/test-model` | 「测试模型端点」按钮 | ❌ |
| `GET /api/manifests` | 相册 manifest 列表（配置区相册下拉依赖） | ❌ |
| `GET /api/qa-dataset` | QA 数据集浏览页的数据 | ❌ |
| `GET /api/assets/{id}/file`、`GET /api/albums/{album}/{coll}/{file}` | 媒体展示 | ❌ |

**其中 4 条是「评测控制台可用」的硬依赖**（manifests / qa-dataset / 媒体服务 / memory-spaces）——
少了它们，配置区选不了相册、QA 浏览页没数据、结果里的图看不了、复用基座选不了。

### 0.3 一处**纠正**：前端没有用 `model-profiles/*`

grep `App.vue` 全文，`model-profiles` **零命中**。前端换模型走的是
`POST /api/current-model`（`:2618/:2622`），**2.0 有这个端点**。

所以那不是缺口。**但交互语义要重写**：1.0 的 popover 是「不重启切端点」，
2.0 换成了「重启 2.0」（S9 的裁决③）。这块 UI 要按新语义重新设计，
不是把 1.0 的搬过来。

### 0.4 一处**死面板**：arbiter 状态区

模板 `:2845-2856` 是一个 8 格的「实时调度状态（VLMArbiter）」区，
`loadArbiterStatus()`（`:2711-2723`）每 **3 秒**轮询 `GET /api/arbiter-status`——
**该端点在 S9 已被删除**（1.0/2.0 主 API 都不存在这个能力）。
**S10 应删掉这块面板**，不是把它搬过来。

`ingest-batches/cancel` 前端本来就没用（grep 空），无需处理。

### 0.5 与 `apps/web` 的复用判断（有依据，非偏好）

| 判断 | 依据 |
|---|---|
| **只复用工程约定与配置模板** | `tsconfig` 的 project references 结构、`.oxlintrc.json`、vitest+jsdom、`vite.config.ts` 的写法、以及 `make web-check` 的四步顺序（类型 → lint → 测试 → build）。`Makefile:93-113` 对这些约定写了明确理由（如 `tsc -b --force` 防增量漏检）——照做就继承这些**踩过的坑** |
| **不复用 `shared/api/client.ts`** | 它 `BASE = "/api/v1"` 且按 sentrix 的 `{code,message,detail}` 错误体解析；PhotoBench 是独立包、前缀 `/api`、有自己的 `api/errors.py`。改它等于改坏主产品客户端 |
| **不复用 `shared/ui/*` 与 `styles/tokens.css`** | 前者只有 5 个小展示组件；后者文件头自述是**逐行移植 1.0 主产品样式**，而 PhotoBench 有自己那 881 行 `styles.css`。搬过去会改变观感，**违背「迁移非重设计」** |
| **必须新增 `echarts`** | `apps/web` 里没有任何图表库（grep 空），而 PhotoBench 的遥测图全靠它 |

---

## 1. 动工前要定的（**用户裁决后再动**）

### ① 7 个后端缺口怎么处理（**这一段最大的问题**）

`manifests` / `qa-dataset` / 媒体服务 / `memory-spaces` 是控制台可用的硬依赖，
但 2.0 后端目前**没有任何 HTTP 层**提供数据集、媒体、manifest 与 memory-spaces。

三条出路：

| 方案 | 含义 | 代价 |
|---|---|---|
| **A. S10 一并补后端** | 前端 + 7 个端点一起做 | S10 从「前端重写」变成「前端重写 + 半个后端切片」，规模显著变大 |
| **B. 前端先做能做的，缺口面板留空** | 只做已有 8 条端点的部分；缺口面板挂「尚未开放」空状态 | 与设计文档「评测控制台可用」有距离；但**与主产品前端 F7 的处置同一条口径**（后端未实现的能力 → 空状态） |
| **C. 先补后端，S10 拆成 S10a/S10b** | 先把 7 个端点补上，再做前端 | 段数增加，但每段都能单独验收 |

**用户 2026-10-05 裁决：选 C —— 拆两段，先补后端（S10a）再重写前端（S10b）。**

理由与我的倾向一致：这 7 个端点里 `manifests`/`qa-dataset`/媒体服务**都不是评测能力**，
是「把本地数据集与结果图端出来」的展示层——它们与 S9 做完的评测链是两回事，
所以值得单独成段、单独验收，而不是混进前端重写里。

**② 端口已核（2026-10-05 实测）**：计划原建议的 **4275 被占**——是
`/home/asus/s5verify/web/apps/web` 的 vite（另一个验证沙箱，已跑 12 天，不归本项目）。
**4276 空闲**，取它。

另两件同一次实测到的事实：

- **设计文档 §5.1 的端口表里没有「2.0 PhotoBench 前端」这一项**（那里 4274 是 2.0
  **主产品** Web）。所以 4276 是一条**新增约定**，不是文档里已有而我漏看。
- **1.0 的 PhotoBench 前端在 5173，现正跑着**（`services/photobench/frontend`）。
  S10b 验收时可以直接对着它逐面板比对，不用另起。

### ② 前端端口

后端在 **9597**（设计文档 §5.1）。前端 dev 端口建议 **4275**（`apps/web` 用 4274），
`/api` 代理到 `127.0.0.1:9597`。**待与设计文档端口表核对后确认。**

### ③ 要不要引 `echarts`

PhotoBench 的遥测图、记忆层级图全靠它。`apps/web` 没有图表库，
所以这是**给 PhotoBench 前端单独加一个依赖**——与「生产依赖只有 3 个」那条主产品原则不冲突
（那是 `apps/web` 的原则，PhotoBench 是另一个 app）。但**要你确认**。

---

## 1.5 S10a：补齐 7 个后端端点（**先做这一段**）

**验收**：`apps/photobench` 的 openapi 契约从 16 条路径增到约 24 条，
且前端要用的每一条都有真实响应可读。

**范围是 1.0 已有的那 7 条 + 2 条媒体服务**（§0.2 那张表）。
**1.0 是唯一权威**：`services/photobench/backend/benchmark_orchestrator.py`
里对应的 handler 要逐条读、照搬响应形状。

| # | 内容 |
|---|---|
| T1001 | **数据集展示层**：`GET /api/manifests`（相册 manifest 列表）、`GET /api/qa-dataset`（QA 数据集浏览）。数据源在 `apps/photobench/datasets/`（1.4 GB，按 `DATASET.md` 声明不入 Git）——**先核实目录形状与 DATASET.md 的约定** |
| T1002 | **媒体服务**：`GET /api/assets/{id}/file`（从 run 结果里取图）、`GET /api/albums/{album}/{coll}/{file}`（相册本地媒体）。**注意路径穿越**——1.0 那条直接拼路径，2.0 要拦（S9b-1 已有先例与用例） |
| T1003 | **`GET /api/memory-spaces`**：1.0 是编排器**转发** Sentrix 核心的接口（带 `sentrix_url` 参数）。2.0 要转发到 `apps/api` 的 `/api/v1/memory-spaces`——**这是 PhotoBench 唯一一处要调主 API 的读侧**，注意别破 `photobench-isolation` 契约（那是禁止 import，不是禁止 HTTP） |
| T1004 | **`POST /api/test-model`**：测模型端点是否可用。**2.0 的语义可能与 1.0 不同**（换模型已改成「重启 2.0」）——先读 1.0 那条做什么，再决定 2.0 版测的是什么 |
| T1005 | **结果读侧三条**：`GET /api/runs/{id}/keyframe-analysis`、`/memory-effectiveness`、`/export-trace`。前两条要从 run 的 `results.jsonl` 里算（S9b-1 的 `domain/runs/analysis.py` 已有那批纯函数可扩），第三条是导出下载 |
| T1006 | 收口：photobench 的 openapi 重跑、`capability-map` / `photobench-module-map` / `behaviour-baseline`、真机验收（**对 1.0 的真实 run 结果跑一遍**，比对 1.0 的响应） |

**S10a 的风险点**：`keyframe-analysis` 与 `memory-effectiveness` 是**两条最重的计算**
（1.0 的前端里对应 `:1810-2055` 那 245 行图数据构造）。**先读 1.0 的 handler 到底返回什么**，
再决定是「后端算好给前端」还是「原样透出原始数据让前端算」——1.0 是哪一种就照哪种。

---

## 1.6 裁决

| # | 问题 | 裁决 |
|---|---|---|
| ① | 7 个后端缺口怎么处理 | **拆两段**：S10a 补后端 → S10b 重写前端 |
| ② | 前端端口 | **4276**（见下） |
| ③ | 要不要引 `echarts` | **引**。1.0 的遥测图、记忆层级图全靠它；主产品那条「生产依赖只有 3 个」是 `apps/web` 的约束，PhotoBench 是另一个 app |

---

## 2. 建议的文件结构（待裁决后细化）

```
apps/photobench/web/            ← 与 apps/photobench/src/ 平级，照 apps/api + apps/web 的共存体例
├── package.json                React 19 + react-dom + **echarts**（**没有 react-router**，见下）
├── index.html / vite.config.ts / tsconfig*.json / .oxlintrc.json / .gitignore
└── src/
    ├── main.tsx / App.tsx                  两个视图：评测运行 / QA 数据集浏览
**关于 react-router：不要加。** 核查确认 1.0 只用 `activeView` 一个 ref 切两个视图
（模板 `:2844` 与 `:3676` 两条 `v-if`），**没有 URL 路由语义**——没有深链、没有前进后退、
没有按 URL 恢复视图。加 router 是净增一个依赖与一套行为，不算迁移。
（计划 §2 初稿里写了 `router.tsx`，撤掉。）

### 2.1 六份备份的扫描结论（S10b-0）

计划 §0.1 记着「6 个 `App.vue.bak-*` 备份文件（未分析）」。扫完了，方法是按函数名与模板
区块名做集合差、**只读差异项**：

| 备份 | 行数 | 相对当前 |
|---|---|---|
| `codex-macro-20260828-121920` | 2,273 | **唯一含被移除功能的**，见下 |
| `codex-recall-20260914-104947` / `codex-scroll` / `codex-scroll-qa` | 3,213–3,230 | 函数是当前子集，无移除 |
| `full-history-20260916` / `system-telemetry-20260916` | 3,497 | 当前是它们的超集 |

**当前 `App.vue` 是除最老那份外全部备份的超集**，被移除的东西只有一处：

**「导出 SFT JSON」按钮**（`exportSftTraces()`，macro 备份 `:1469`）指向
`/api/runs/{id}/export-sft`。**当前版本删了它，后端却一直没有这个端点**——直到 S10a 补上。
重写时**加回**（与「导出轨迹 JSON」并排）。详见 `s10-deviations.md` §4.3。

其余差异都是纯历史包袱（改名、被新实现取代），不必移植。
    ├── api/client.ts                       新写（BASE=/api，对齐 photobench 的 errors.py）
    ├── features/runs/                      列表 + 详情 + 发起配置
    ├── features/qa/                        逐题结果
    ├── features/qa-browser/
    ├── features/telemetry/                 echarts
    └── styles/                             移植 1.0 styles.css 881 行
```

**规模估计**：3,797 行 Vue → 拆成约 20–35 个文件，**净增（含类型与测试）预计 4,500–6,000 行**。

**门禁**：加 `photobench-web-check`（同 `web-check` 四步，`cd apps/photobench/web`），
`install` 里加 `npm ci`，`ci` 链尾追加——**不要混进 S9 那个 `photobench-check`**（它只覆盖后端 src）。

---

## 3. 施工记录

### 3.1 S10a 交付（2026-10-05）

**端点：16 → 27 条**（`packages/contracts/openapi/photobench.yaml`）。

| 任务 | 端点 | 落点 |
|---|---|---|
| T1001 | `GET /api/manifests`、`/api/manifests/{id}`、`/api/qa-dataset` | `api/routes/datasets.py` + `domain/datasets/{catalog,qa_media}.py` |
| T1002 | `GET /api/albums/{album}/{coll}/{file_name}`、`/api/assets/{id}/file` | `api/routes/{datasets,assets}.py` |
| T1003 | `GET /api/memory-spaces` | `api/routes/spaces.py` + `domain/runs/reuse.py` |
| T1004 | `POST /api/test-model` | `api/routes/model_profiles.py` |
| T1005 | `GET /api/runs/{id}/{keyframe-analysis,memory-effectiveness,export-trace,export-sft}` | `api/routes/reports.py` + `application/runs/reports.py` + `domain/runs/{keyframe_analysis,memory_effectiveness,exports}.py` |

**T1005 那两条计算，1.0 是「后端算好给前端」。** 这是动工前核查的第一个问题（本节
原本把它列为最大风险）：`get_keyframe_analysis` 返回的是 6 组算好的指标、
`get_memory_effectiveness` 返回的是 5 层表格 + 错误链诊断 + 证据有效性三张表，
**不是原始数据**。所以 2.0 同样把计算留在后端，逐行移植。

### 3.2 计划外的范围改动

**S10a 扩到了 `apps/api`**——四处补数，全部是「恢复 1.0 有过的东西」：

| # | 补什么 | 用户裁决 |
|---|---|---|
| 1 | `AssetSummary` 加 `derived_kind` / `parent_asset_id` / `source_timestamp_sec` | 2026-10-05 裁决 |
| 2 | `VideoMetadata` 加 `frame_count`（ffprobe `nb_frames`） | 2026-10-05 裁决 |
| 3 | `VideoPayload` 加 `processing_seconds`（视频流水线计时） | 2026-10-05 裁决 |
| 4 | spaces 加 `created_at` | 施工中发现，同族第 4 例，按既有口径直接做 |

一句话：**1.0 的读侧是 `SELECT *`，2.0 换窄投影时丢掉了几列，而那几列有真实读者。**
详细证据、代价与备选方案见 `s10-deviations.md` §1.1 / §2.1 / §2.2 / §4.4。

### 3.3 验证

| 项 | 结果 |
|---|---|
| 用例 | `apps/photobench` 408 → 449 条全绿 |
| 定点变异 | **17/17 全部被杀**（领域 9 + 应用 4 + 数据层 8/4 见 §3.4） |
| 分层契约 | 11 条全 KEPT（含 `photobench-isolation`） |
| 可达性 / 模块地图 / 能力地图 | 各 0 问题 |
| 两份 openapi 契约 | 重跑后 `--check` 一致 |
| 覆盖率 | domain + application **93.54%**（门禁 85%） |
| 真机差分 | **4 个 run 逐字段一致**，见 §3.5 |

### 3.4 定点变异清单

**领域层**（`domain/runs/`，9 条）：血缘父级映射清空 / 工具轨迹回退不筛 `media_kind` /
时间戳只剩 `metadata_json` 一级 / 分母为 0 回 0 而不是 `None` / 血缘覆盖不回溯父视频 /
工具成功状态表多收一个词 / 0 分题判据改成 2 分 / `export_traces` 取第一个而非最后一个
model 步 / `export_sft` 的 `if-elif` 变成两个 `if`。

**应用层**（`application/runs/reports.py`，4 条）：关键帧分析不再把「取不到资产清单」
当 `unavailable` / 逐视频取元数据不筛 `media_type` / 没有 scope 仍去打一次清单 /
记忆有效性不再吞掉取数失败。

**数据层**（`apps/api` 与展示层，8 条）：血缘列不读回 / `frame_count` 恒 0 /
计时起点为 `None` / spaces 的 `created_at` 回空串 / manifests 跳过缺 `album_id` 的目录 /
qa-dataset 双参数校验 / 文件名穿越拦截 / `sentrix_url` 参数失效。

**写用例时发现我自己两条断言是错的**（都是直觉而非 1.0 口径），已改：单个关键帧的
`mean_span_sec` 我断言 `0.0`——1.0 的收集条件是 `len(values) > 1`，一个点**既不产生
间隔也不产生跨度**，是 `None`；`levels.L1.created` 我按成功次数算——它数的是**调用
次数**，失败的那次进 `ineffective`。

### 3.5 真机验收

**差分方法（重要，因为直觉上容易做错）。** 不能「打两个 HTTP 端点比」——那比的是
「两个后端 + 两个环境」。实测时 1.0 的 Sentrix（8091）没起、2.0 的（9598）起着但那个
scope 不存在，于是**同一个原因（资产清单取不到）在两边会走到不同分支**：「取不到」→
`unavailable`，「取到了是空的」→ 工具轨迹回退，比出来的是环境不是代码。

所以验收脚本（`scripts/diff_photobench_reports.py`，随本次提交进仓库，事后可复跑）
把环境按住：**把两边取资产清单那一步都钉成「取到了、是空的」**，逼它们走同一条回退
路径，再逐字段比输出。

**结果**：

| run | album | keyframe-analysis | memory-effectiveness |
|---|---|---|---|
| `20261002-162809-…reuse-478e58` | album3-max-video10（1167 题） | 47 字段一致 | 260 字段一致 |
| `20260903-084309-…kling-5df6ff` | album3-kling | 46 字段一致 | 254 字段一致 |
| `20260815-111203-…album3-14-bc467e` | album3-14 | 46 字段一致 | 253 字段一致 |
| `20260827-195432-…build-d1f8e4` | album3-max-video10 | 46 字段一致 | 244 字段一致 |

**约 1200 个字段，零差异。**

**其余端点的真机取证**：

- `export-trace` 在 1167 题那个 run 上：200，`Content-Disposition` 正确，
  **5.4 MB / 416 题，中文零转义**（验到 `ensure_ascii=False` 那条）。
- `keyframe-analysis` 与 `memory-effectiveness` 在该 run 上给出实质结论：
  回退路径恢复出 10 个视频 + 10 个关键帧；记忆有效性的错误链主表是
  `memory_hit_wrong 498` vs `memory_miss_wrong 65`——**大部分错误发生在召回之后**
  （证据选择 / 推理），不是检索。这正是这块面板存在的意义。
- 数据集端点实测：`/api/manifests` 列出 5 个相册；`/api/albums/album3/photos/<file>`
  200 + `image/jpeg`；带 `Range: bytes=0-9` → 206 + `Content-Range: bytes 0-9/2032828`。

### 3.6 全量 `make ci` 的最后两个红，都不是本次改动

`make ci` 在 `coverage` 步骤退出 1：**2 failed, 2494 passed, 8 skipped**（42:36），
覆盖率自身 **93.54%**（门禁 85%）通过。两个失败逐条查过：

| 失败 | 性质 |
|---|---|
| `tests/e2e/test_assistant_turn.py::test_the_answer_never_leaks_internal_identifiers` | **已知、且用户 2026-09-29 明确决定不修**：qwen3.5-4B 在正文里写 `photo_1` 句柄，`final_guard` 的 `internal_id_leak` 正则（要 `[a-f0-9]{6,}` 后缀）盖不住。判为模型问题，与本次改动无关 |
| `tests/e2e/test_visual_search.py::test_qdrant_ranking_matches_the_exact_scan_on_the_authoritative_store` | **显存 OOM**：`onnxruntime::BFCArena Failed to allocate memory for requested buffer of size 2359296`——进程内的 Chinese-CLIP ONNX 视觉嵌入器在 GPU 上分不出 2.3 MB |

**第二条的账**（2026-10-05 实测，卡 24576 MiB）：

| 占用 | MiB |
|---|---|
| ninfer-serve 18100（**GoQuark，另一个项目**） | 7150 |
| WeMM sidecar 8102 | 7070 |
| llama-server 8100（本次才拉起来，之前是死的） | 4892 |
| 1.0 API 8091 | 2262 |
| 2.0 API 9598 | 976 |
| **空闲** | **1723** |

（这几个数随负载浮动，上表是同一次 `nvidia-smi` 的读数。**WeMM 那一格是它服务过几张
大图之后的峰值**——见 §3.7。）

三个模型服务就占 19.1 GB，e2e 要在同一个进程里再加载 ONNX 模型时已经没地方了。

**注意：即使停掉 ninfer，`make ci` 也不会全绿**——第一条按用户的决定就是不修的。
所以「全绿」在这个部署形态下不是可达的门槛，本次验收不以它为准。

**同一份源码的一次全量 `make ci` 是退出 0 的**（当天早些时候，那时 ninfer 还没起、
8100 还活着）。这也是为什么本次的 2 个红可以判为环境而非代码：**改动集合没有变过**。

### 3.7 顺带修好的一处环境问题（8102 攥着 4.3 GB 显存）

排查 8100 起不来时发现：**8102 的 WeMM sidecar 起的时候没设 `PYTORCH_CUDA_ALLOC_CONF`**，
PyTorch 的缓存分配器把峰值水位一直攥着不还给驱动。它自带的 `GET /stats`（就是为这件事
写的）给出分解：

| | 重启前 | 带 `expandable_segments:True` 重启后 |
|---|---|---|
| `reserved_mib`（分配器高水位） | 9526 | **5204** |
| `max_allocated_mib`（峰值活张量） | 8187 | 5190 |
| `allocated_mib`（权重本身） | 5200 | 5190 |

**腾出约 4.3 GB**，8100 随即起来了。`allocated` 那 5190 MiB 降不动——2.72B 参数在 bf16
下的权重就是这么大。

**峰值来自哪**：`_encode` 本来就是逐条编码，但**没限制图像分辨率**——实测一张 2 MB 的
照片会让 `reserved` 从 5252 涨到 6752。**没有采用「限制分辨率」这个做法**：那会改变
嵌入向量，与库里已存的向量口径不一致，用户在 1.0 验过的 WeMM 检索结论就作废了。

那条环境变量目前只是这一次启动带的，**持久化要改拉起它的脚本**（不在任何仓库里）。

### 3.8 一处我先报错了的事实（留档）

排查过程中我曾报「8091（1.0 生产 API）已停 6 天」——**错的**。依据是
`systemctl is-active sentrix-api.service` 回 `inactive`，加上一次**没包含 8091 的** `ss` grep。
实际上那个 systemd unit 确实自 2026-09-29 起就是 dead，但有人手动起了一个进程，
**一直在跑且健康**（`/api/memory-spaces` 回 200）。用户 2026-10-05 确认「知道了，我自己处理」。
结论：**判断一个服务在不在，要看端口/探活，不能只看 systemd 状态**。


### 3.9 一条运维教训：**别从会被回收的会话里起长驻服务**

这一节是拿两次真实的故障换来的，写下来免得重犯。

**经过**：排查 8100 时我用 `nohup ./start_vlm_8100.sh &` 在一个**后台 Bash 任务**里起了它。
它正常服务了约 71 分钟，然后在日志里留下 `cleaning up before exit...`（llama.cpp 收到
**信号**后的干净退出，不是崩溃）——时间点正好对上那个后台任务被回收。**`nohup` 挡的是
`SIGHUP`，挡不住对会话/进程组的 `SIGTERM`。**

**做法**：长驻服务一律用 **`setsid`** 起（新会话、自己当会话首进程）。判据是
`sid == pid`：

```sh
cd /home/asus && setsid ./start_vlm_8100.sh < /dev/null > <log> 2>&1 &
```

起完用 `ps -o sid= -p <pid>` 确认 sid 等于 pid，再用探活确认，**然后主动回收发起它的
那个会话、再确认一次它还活着**——只验一次「起来了」不够，那正是这次被骗的地方。

**9598（2.0 主 API）也死过——那次不是故障，是同一台机器上的并行工作。**

我先后排除了两个嫌犯：没有可读的 `dmesg`（要 sudo 密码）能证明是 OOM；而
`apps/photobench/tests/test_sentrix_process.py` 的 `api_url` 写着
`http://127.0.0.1:9598`，看着像会动真进程——实测跑完那整个文件，9598 的 pid 不变、
探活 200，**不是它**。

真凶最后是在**进程树的父链**里找到的：

```
bash /home/asus/Github/Sentrix-Home-Web/.codex-tmp/restart-photobench-stack-20261005.sh
  ← sshd: asus@notty
```

那份脚本（另一个 agent 会话经 SSH 跑的，目的是把整套栈切到 ninfer 引擎）开头就是

```sh
stop_listener 9598 "sentrix.main:create_app"
stop_listener 8091 "backend.app:app"
stop_listener 8771 "benchmark_orchestrator.py"
```

——**它明确地、有意地杀掉 9598**，然后把 8091 与 1.0 PhotoBench（8771）重启到 ninfer 上
（`sentrix-api-8091-ninfer-c4.log` / `runtime_connection.ninfer-vision-c4-153.json`）。

**教训一**：共用机器上服务不明原因消失时，**先看进程树的父链**（`ps -o ppid=`），
别先怀疑自己或硬件。我在这上面绕了三轮才走到这一步。

**教训二**：**发现别人在管这台机器的服务之后，立刻停止自己的服务操作。** 那份脚本有
「端口上出现预期之外的进程就 `exit 1`」的守卫——我再去起 9598，会让**它的**重启失败。
共用机器上，别人的自动化优先。

**结论**：8100 那次可归因（我自己造成的，`nohup` 没挡住会话级 SIGTERM）；9598 那次
也已归因（另一个会话的重启脚本）。两个都用 `setsid` 重新拉起过；**此后本切片的操作
不再触碰这台机器上的任何服务**。
