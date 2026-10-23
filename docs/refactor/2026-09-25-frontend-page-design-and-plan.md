# Sentrix Home Web 2.0 — 前端页面设计与实施计划（F1–F6）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 2.0 补上**产品前端**——1.0 已验证的壳（侧边栏 / 顶栏 / 相册切换 / 统一页头）与
按功能划分的 10 个页面，并把前端纳入与后端对等的防腐门禁。

**Architecture:** 壳层（`AppShell` + `ScopeProvider` + `react-router`）承载导航与相册范围；
每个页面是一个自取数据的路由组件；展示原语（`PageHeader` / `StatCard` / `Section` /
`EmptyState` / `FilterChip`）从 1.0 的 `pageHeader()` 与 `styles.css` 直译。
样式分三层：`tokens.css`（设计令牌）/ `shell.css`（壳）/ `components.css`（组件）。

**Tech Stack:** React 19 / TypeScript / Vite 8 / vitest + @testing-library/react + jsdom /
oxlint · **新增依赖**：`react-router`（唯一新增，registry 实测可达）

**依据文档:** `docs/architecture/2026-09-21-sentrix-home-web-2-implementation-design.md`、
`docs/refactor/frozen.md`、`docs/refactor/2026-09-23-s5-implementation-plan.md`（任务编号接续）

---

## 本文档是怎么写出来的

### 起因

用户判断 2.0 前端「几乎无法正常查看」，经检索确认**计划从未覆盖前端页面设计**：
`docs/` 全量检索 `页面设计|页面规划|信息架构|导航模型|布局系统` → **0 命中**。
计划里所有「前端 / 页面」只做两件事：`web-check` 门禁、逐切片往 `App.tsx` 追加一个 feature。
S0–S5 的切片计划都是后端能力切片，前端每次都只是"顺手接一下"，从没有过壳、导航、页面结构。

### 基准的确定

1.0 有**两个**前端，第一轮差点认错：

| 文件 | 行数 | 是什么 |
|---|---|---|
| `App.vue` | 2086 | **PhotoBench 评测台**。页签只有「评测运行 / QA 数据集浏览」（`App.vue:1464-1467`） |
| `src/app.js` | 2257 | **家庭记忆产品界面**。侧边栏 SPA（`src/app.js:237-238`） |

本计划的基准是 **`src/app.js`**，不是 `App.vue`。

### 实测的环境事实（写计划前跑的，不是假设）

| 事实 | 值 | 怎么测的 |
|---|---|---|
| node / npm | v22.14.0 / 10.9.2 | `node -v; npm -v` |
| npm registry | **可达** | `npm view react-router version` → `8.4.0`，exit 0 |
| 2.0 是否已有 router | **没有**（`node_modules` 无 router/history） | `ls node_modules \| grep -iE "router\|history"` |
| API 前缀 | `/api/v1` | `main.py:26` `APIRouter(prefix="/api/v1")`；`curl /openapi.json` 复核 |
| 2.0 端点数 | **28 条路径 / 31 个操作** | `curl /openapi.json` 的 `paths` 去重计数 = 28；`grep "@router\." routes/*.py` = 31 条装饰器（`/assistant/conversations` 有 GET+POST 等）。**本文档一律按 28 条路径说** |
| vite dev | 端口 4274，`/api` → `127.0.0.1:9598` | `vite.config.ts` |
| vitest | jsdom，`include: src/**/*.test.ts(x)` | `vite.config.ts` |
| 现有前端测试 | **1 个文件**（`PeopleView.test.tsx`） | `find src -name "*.test.*"` |
| `module-map.md` 是否含 `apps/web` | **0 次** | `grep -c "apps/web" docs/architecture/module-map.md` |
| `check_reachability.py` 根 | 仅 `sentrix.main` / `sentrix.bootstrap` | 脚本 docstring |

### 设计决策（用户 2026-09-25 裁决）

| # | 问题 | 裁决 |
|---|---|---|
| ① | 后端未实现的能力（故事 / 行程 / 关系图谱 / 视频记忆）怎么处理 | **全导航 + 冻结项空状态** |
| ② | 视觉基准 | **移植 1.0 的样式系统** |
| ③ | 页面切换方式 | **URL 路由（react-router）** |
| ④ | 相册范围切换器 | **保留，照搬 1.0** |
| ⑤ | 2.0 的 `/search` 独立检索放哪（1.0 无对应页） | **A：单列 `/search` 资料检索页** |

### 硬保证

- 引用的 **1.0 位置（文件 + 行号 + 符号）都是实际读过的**（由独立 agent 逐条 `sed -n` 反证过）；
- **行号口径**：页面既给函数定义行，也给 `pageHeader` 调用行，两者都列，例如
  `app.js:242`（`function overview()`）/ `:245`（`pageHeader` 调用）。
  只给其中一种会让人以为另一种也是定义行。
- 引用的 **2.0 现状（端点数、路由、配置）都是实际跑命令枚举的**；
- 本文档**不含未实测的性能数字**（耗时 / 覆盖率 / 警告数）；
- **尚未落地的任务不给红灯 / 绿灯输出**——那是执行时观察到的，落地后逐任务回填
  （与 S5 计划两轮做法一致）。

---

## 与 S0–S5 的关系

### 任务编号

S0–S1 = 1–24，S2 = 25–33，S3 = 34–45，S4 = 46–68，S5a/b/c = 69–87。
F 系列顺着往下排：**F1 = 88–93，F2 = 94–97，F3 = 98–101，F4 = 102–104，
F5 = 105–109，F6 = 110–112**。

### 与 S5c 的关系

S5c（Task 85–87：外观证据 / 人物画像 / e2e 验收）在 2.0 主仓库里**没有落地**：
`apps/api/src/sentrix/domain/identity/` 只有 `faces.py` / `models.py` / `ports.py`，
`git log --all --diff-filter=A` 对 `portrait.py` / `moments.py` / `appearance.py` **无输出**，
stash 为空，悬空提交里也没有。

它的实现在**隔离副本** `/home/asus/s5verify/repo`（分支 `s5-tdd`）的**未提交工作树**里：

| 文件 | 大小 |
|---|---|
| `domain/identity/appearance.py` | 4092 B |
| `domain/identity/moments.py` | 6991 B |
| `domain/identity/portrait.py` | 16106 B |
| `tests/unit/test_person_appearance.py` / `test_person_moments.py` / `test_portrait.py` | — |
| `docs/refactor/2026-09-24-s5c-implementation-plan.md` | — |
| `domain/identity/models.py` | 纯新增（`+292-431`） |

**可干净搬回**：副本 HEAD 的 `models.py` 与主仓库**逐字节相同**（已 diff 验证），
故 S5c 对它是纯新增；三个模块只 import `sentrix.domain.identity.models` / `moments`，
不碰任何在副本里已分叉的 infrastructure 文件（`chat_client.py` / `model_endpoint.py` /
`container.py` / `config.py` / `face_adapter.py` / `onnx_runtime.py`）。

**但副本不能整分支合并**：副本 HEAD `e22e026` 落后主仓库 `43db1bb` 十个提交
（偏差第 109–118 条：模型名问端点、CUDA 退化防线、思考通道剥离、8100 tool calling 等），
只能挑文件。

F 系列**不依赖** S5c：本计划页面对 `/people` 的画像区按"后端未就绪"处理（空状态），
S5c 搬回后该区块自动转为真实数据，不需要改路由与壳。
**是否搬回、何时搬回是独立决策，本计划不擅自处理。**

---

## 一、根因

`apps/web/src/App.tsx` 现 55 行：3 个 view 用 `useState` 切，`SCOPE_ID = "album1"` 硬编码，
`AssistantPanel` 与 `SearchBox` **无条件渲染在首页顶部**——它们不是页面，是堆在入口的控件。
没有侧边栏、顶栏、面包屑、页头；`styles.css` 128 行零散补丁，无设计令牌。

准确描述：**打开就是助手输入框 + 搜索框 + 一堆裸数据，没有导航，不知道自己在哪一页，
也无法去到别的页。**

## 二、三个缺口（本计划必须收口）

### 缺口 1 · 相册切换器缺一个后端路由

裁决 ④ 要保留相册切换器。2.0 **有** domain 端口、有仓储、有 `container.spaces`：

- `infrastructure/persistence/repositories/spaces.py:32` `list_all()`
- `domain/spaces/ports.py:28` `list_all`
- `domain/spaces/models.py:18` `MemorySpace`

但 **`GET /api/v1/memory-spaces` 路由不存在**（28 个端点里没有它）→ 前端拿不到相册列表。
**这是本计划唯一的后端改动**（Task 93）。

### 缺口 2 · `/search` 在 1.0 里没有对应页面

1.0 的提问框是**提交进助手对话**的（`searchBar()` → assistant turn），没有独立检索页；
2.0 的 `/api/v1/search`（视觉 + 词法 + 元数据直检索）是新做的能力。
按裁决 ⑤ 单列 `/search` 页；**概览的提问框仍走助手**（同 1.0 语义）。

### 缺口 3 · 前端零防腐——「声称在检查、其实没有」的第 10 个实例

实测：`module-map.md` 里 `apps/web` 出现 **0 次**；`check_reachability.py` 以
`sentrix.main` / `sentrix.bootstrap` 为根，**完全不含前端**。前端唯一门禁是 `web-check`
（`Makefile:80-85`：`tsc -b` → `oxlint` → `vitest run` → `vite build`），而 vitest
**只有 1 个测试文件**。

后果：按本计划新加的 10 个页面，默认**没有任何机制**保证它们被路由挂上、或在 CI 里被真的渲染过。
→ F6 收口（Task 110–112）。

---

## 三、页面设计

### 3.1 壳层（`AppShell`，直译 `src/app.js:237-238`）

```
┌────────────────┬──────────────────────────────────────────────┐
│ 240px sidebar  │ 72px topbar（sticky + 毛玻璃）                │
│ (--ink 深色)    │  面包屑：返回 / Sentrix Home / 当前页          │
│                │  右侧：⌘ 命令入口 · 当前相册                   │
│  S Sentrix     ├──────────────────────────────────────────────┤
│  Home Memory   │                                              │
│                │  <Outlet/>                                   │
│  [当前相册 ▾]   │  每页自取数据，首屏渲染 PageHeader             │
│                │                                              │
│  家庭记忆       │                                              │
│   ⌂ 家庭概览    │                                              │
│   ⌕ 家庭记忆助手 │                                              │
│   ↕ 事件时间线  │                                              │
│   ◎ 人物与关系  │                                              │
│   ◇ 实体与知识  │                                              │
│   ▦ 资料库      │                                              │
│   ▶ 故事工作室  │                                              │
│   ↓ 导入队列    │                                              │
│                │                                              │
│  空间与系统     │                                              │
│   ◌ 设备与隐私  │                                              │
│   ? 使用帮助    │                                              │
│   ▤ QA 测评     │                                              │
│                │                                              │
│  ● 本地 AI 正常 │                                              │
│  Sentrix · 0.2.0│                                             │
└────────────────┴──────────────────────────────────────────────┘
```

- `navItems` 逐字段直译 `src/app.js:78-87`（数组 `78: const navItems = [` … `87: ];`，条目 79–86；含 icon 字符）
- 「QA 测评」指向 1.0 的 `App.vue` 评测台 → 2.0 **本期不做**，点击显示"尚未开放"
- 侧边栏底部脉冲绑定 `/health`：失败显示「本地服务不可用」（同 `src/app.js:238`）

### 3.2 页面清单与后端支撑度对拍

2.0 现有 28 个端点。逐页对拍（✅ 完整 / ⚠️ 部分 / ❌ 缺 / 🧊 冻结）：

| 路由 | 页面 | 1.0 出处 | 后端 | 本期交付 |
|---|---|---|---|---|
| `/` | 家庭概览 | `app.js:242`（定义）`:245`（pageHeader） | ⚠️ | 真实数（资产 / 事件 / 健康）+ 提问框 + 最近事件；待确认事实、视频记忆卡**不出现** |
| `/assistant` | 家庭记忆助手 | `app.js:619`（定义）`:622-623`（layout） | ✅ | 会话侧栏 + 消息流 + agent trace + 证据缩略图 + 停止本轮 |
| `/timeline` | 事件时间线 | `app.js:626`（定义）`:645`（pageHeader） | ✅ | 筛选（全部/有人物/有地点/日期）+ 按日分组折叠 + 事件详情 |
| `/people` | 人物与关系 | `app.js:648`（定义）`:670`（pageHeader） | ⚠️ | 待整理人物 tab 全功能（确认/拒绝/合并/拆分/命名）；关系图谱 tab 空状态 |
| `/knowledge` | 实体与知识 | `app.js:870`（定义）`:882`（pageHeader） | ❌ | 已确认人物段真实；实体组 / 地理地点 / 行程候选空状态 |
| `/library` | 资料库 | `app.js:683`（定义）`:687`（pageHeader） | ✅ | 相册摘要 + 媒体类型筛选 + 排序 + 网格 + 详情 |
| `/stories` | 故事工作室 | `app.js:690`（定义）`:706`（pageHeader） | 🧊 | 空状态「尚未开放」 |
| `/imports` | 导入队列 | `app.js:733`（定义）`:745`（pageHeader） | ✅ | 拖放 / 文件夹 + 处理流水线 + 导入记录 |
| `/settings` | 设备与隐私 | `app.js:756`（定义）`:771`（pageHeader） | ⚠️ | 健康网格 + 隐私边界 + 审计动作；OCR 设置、事实维护空状态 |
| `/search` | 资料检索 | —（2.0 新增） | ✅ | 视觉 / 词法 / 元数据直检索，按通道展示命中分数 |

**冻结项**（`frozen.md`）：故事工作室、行程候选、关系图谱、视频场景记忆 → 一律空状态，
**但导航入口保留**（裁决 ①）。

### 3.3 逐页设计

每页统一结构：`PageHeader(kicker, title, description, action)` + 筛选行 + 内容区 + 空状态。

**`/` 家庭概览**
- 相册范围卡（当前 scope 名 + "全部相册 / 单个相册"）
- 提问区：`section-kicker` "问 Sentrix" + 标题 + 输入框 → **提交进助手对话**（同 1.0）
- 4 个 stat-card：已整理内容（`/assets` 计数）/ 已形成事件（`/timeline` 计数）/
  本地 AI 状态（`/health`）/ 已确认人物（`/people`）——**不出现事实数、视频卡**（后端无）
- 最近事件列表（`/timeline` 取前 N）+ 「查看全部」→ `/timeline`
- 端点：`/health`、`/assets`、`/timeline`、`/people`

**`/assistant` 家庭记忆助手**（旗舰页，直译 `app.js:622-623` 的 `assistant-layout`）
- 左：会话侧栏（`/assistant/conversations`，可新建 / 改名 / 删除）
- 中：消息流。每条 = `assistant-ident`（S 家庭助手）+ bubble + agent trace 时间线
- 流式中：`data-live-progress` 实时 trace + 「停止本轮回答」
- 助手回答内的证据缩略图 → `result-set/{id}/photo`
- 底部：输入框
- 端点：`/assistant/conversations*`、`/assistant/turns/{id}/events`(SSE)、`/assistant/result-sets/{id}/photo`
- **本期必须钉住**：`<|channel>thought` 不出现在气泡里（客户端已在 `chat_client.py`
  的 `split_thought_channel()` 剥离；前端再加一条断言测试防回归）

**`/timeline` 事件时间线**
- 筛选行：全部事件 / 有人物 / 有地点 / 日期选择 / 清除日期 / 刷新
- 内容：按日分组 `<details open>`，每组「N 个事件」
- 每个事件卡：封面图 + 标题 + 摘要 + 地点/张数/可回溯 + 「查看证据」
- 右侧 `side-inspector`：视频记忆说明 → 本期空状态
- 端点：`/timeline`、`/events/{id}`

**`/people` 人物与关系**
- Tab 1「关系图谱」→ 本期空状态（后端无 `relationships` / `family-graph`）
- Tab 2「待整理人物 `<b>N</b>`」→ **现有 `PeopleView` 全功能迁入**：簇确认 / 拒绝 /
  合并 / 拆分 / 命名 + 批量确认
- 端点：`/face-clusters*`、`/people*`、`/face-instances/{id}/crop`

**`/knowledge` 实体与知识**
- 4 个统计卡：已确认人物（`/people` 真实）/ 实体组 / 已确认关系 / 待维护事实（后三者为 0）
- 「已确认人物」卡网格：真实（`/people`）
- 行程候选、地理地点、实体分组：空状态（后端无 `entities` / `entity-groups` /
  `geo-places` / `trips`）

**`/library` 资料库**
- 相册摘要行 + 4 个统计（当前范围资产 / 图片 / 音频文本 / 视频）
- 筛选行：全部 / 图片 / 音频 / 文本 / 视频 + 排序切换
- 资产网格 → 点击开详情（文件 / 处理状态）
- Observation 面板：本期空状态（后端无 `/observations`）
- 端点：`/assets`、`/assets/{id}`、`/assets/{id}/file`、`/assets/{id}/preview`

**`/stories` 故事工作室** — 空状态「尚未开放」（冻结）

**`/imports` 导入队列**
- 拖放区 + 「选择相册文件夹」（`webkitdirectory`）
- `import-layout` 右侧：本地处理流水线状态行
- 下方：最近处理任务队列
- 端点：`/media/import`、`/imports`、`/assets`

**`/settings` 设备与隐私**
- 健康网格 3 卡：本地服务 / AI MODEL ROUTER（`/health` 的 models）/ MEMORY INDEX
- OCR 设置卡 → 空状态（后端无 `/settings/ocr`）
- 隐私边界列表 + 审计动作（刷新状态、重新检查失败任务 → `/maintenance/recluster-faces`）

**`/search` 资料检索**
- 搜索框 + 结果列表
- 每条结果按 `raw_scores` **所有通道**展示相似度（沿用现有 `SearchResults` 逻辑，
  不硬编码 `visual_ann`）
- 端点：`/search`

### 3.4 路由表

```
/            /assistant   /timeline    /people    /knowledge
/library     /stories     /imports     /settings  /search
```

相册范围走 **`?scope=` query + localStorage**：链接可分享、刷新保持。
持久化语义取自 `src/app.js:1162-1172`（初始化读）与 `:1247`（切换写回），
键名沿用 `sentrix.scopeId` 以保持从 1.0 迁移过来的浏览器不丢选择
（`src/app.js:1161` 是**取相册列表**，不是持久化，别引错）。

### 3.5 取数模型：每页自取（不是 1.0 的全量 `loadAll`）

1.0 在 `src/app.js:1176` 一次性 `Promise.allSettled` 拉 **16 个接口**（逐个枚举过：
`assets dashboard entities entityGroups entityMergeCandidates events faceClusters
familyGraph geoPlaces health knowledge people personInsights relationships stories trips`），
存进全局 `state`，页面只是视图。

**2.0 不照搬**，理由是实测的：那 16 个里 2.0 **只有 5 个存在**
（`assets` / `events`→`/timeline` / `people` / `health` / `faceClusters`），
**11 个缺失**（`dashboard` `entities` `entityGroups` `entityMergeCandidates` `familyGraph`
`geoPlaces` `knowledge` `personInsights` `relationships` `stories` `trips`）；
且两者的分页契约不同：`/timeline` 有硬上限 **200**（`application/memories/read_timeline.py:25`
`MAX_LIMIT = 200`），1.0 式的 `limit=1000` 在它上面**直接违规**；`/assets` 则
`min(limit, 1000)` 且**无 offset**，拉 1000 条合法但拿不到「下一页」。

→ **每个页面在挂载时拉自己需要的端点**；相册范围变化 = 重新拉当前页。

**这是一处有意的偏离**，登记在本文档「偏离登记」一节。

### 3.6 样式系统移植（`styles.css` 645 行 → 三层）

| 2.0 文件 | 来源 | 内容 |
|---|---|---|
| `styles/tokens.css` | 1.0 `styles.css:3-21` `:root` | `--ink/--muted/--faint/--line/--surface/--soft/--lime/--green/--mint/--peach/--blue/--lavender/--shadow` + **字体栈**（在 `:17`：Inter + PingFang SC） |
| `styles/shell.css` | 1.0 `styles.css:27-60` | `.app-shell/.sidebar/.brand-lockup/.space-switcher/.side-label/.main-nav/.nav-item/.sidebar-footer/.local-pulse/.main-content/.topbar/.breadcrumbs` |
| `styles/components.css` | 1.0 `styles.css:77-` | `.page-heading/.eyebrow/.section-kicker/.stats-grid/.stat-card/.content-section/.section-head/.filter-chip/.empty-*/.button/.icon-button` |

**样式不重写**，按上面的行区间直译。1.0 的类名保留，便于与 1.0 截图对拍。

### 3.7 目录结构

```
apps/web/src/
  app/
    router.tsx             路由表
    AppShell.tsx           侧边栏 + 顶栏 + <Outlet/>
    ScopeProvider.tsx      相册范围 + localStorage + ?scope=
    nav.ts                 navItems（直译 app.js:78-87）
  shared/
    api/client.ts          【已有】扩展 listMemorySpaces 等
    ui/
      PageHeader.tsx  StatCard.tsx  Section.tsx
      EmptyState.tsx  FilterChip.tsx  Icon.tsx  Toast.tsx
  features/
    overview/OverviewPage.tsx
    assistant/AssistantPage.tsx        （现有 AssistantPanel 降级为子组件）
    timeline/TimelinePage.tsx          （现有 Timeline 降级为子组件）
    people/PeoplePage.tsx              （现有 PeopleView 降级为子组件）
    knowledge/KnowledgePage.tsx
    library/LibraryPage.tsx            （现有 AssetGrid/AssetDetail 降级）
    stories/StoriesPage.tsx
    imports/ImportsPage.tsx
    settings/SettingsPage.tsx
    search/SearchPage.tsx              （现有 SearchBox/SearchResults 降级）
  styles/
    tokens.css  shell.css  components.css
```

---

## 四、任务分解

> 编号接 S5c（85–87），F 系列从 **88** 起。
> **每个任务三步**：红灯 → 绿灯 → 完整 `make ci`。未落地前不给输出。

### F1 · 壳与路由（Task 88–93）

---

#### Task 88 · 设计令牌与壳层样式

**Files:**

```text
apps/web/src/styles/tokens.css        【新】1.0 styles.css:3-21 直译（`:root`，含 :17 字体栈）
apps/web/src/styles/shell.css         【新】1.0 styles.css:27-60 直译
apps/web/src/styles/components.css    【新】1.0 styles.css:77- 直译
apps/web/src/main.tsx                 【改】import 三个新样式，弃 styles.css
apps/web/index.html                   【改】<title>web</title> → Sentrix Home
apps/web/src/App.tsx                  【改】暂改为指向 AppShell（Task 90 落地）
```

**移植对照**（行区间以 1.0 `src/styles.css` 为准，逐类名对回）

| 2.0 文件 | 1.0 行区间 | 覆盖的类 |
|---|---|---|
| `tokens.css` | `:3-21` | `:root` 全部令牌（`3: :root {` … `21: }`）。**注意字体栈在 `:17`**，区间取错会漏掉它；`:1` 是无关的 `img { image-orientation }` |
| `shell.css` | `:27-60` | `.app-shell` `:27`、`.sidebar` `:29`、`.brand-lockup` `:30`、`.space-switcher` `:34`、`.side-label` `:39`、`.main-nav` `:41`、`.nav-item` `:42`、`.sidebar-footer` `:49`、`.local-pulse` `:50`、`.main-content` `:53`、`.topbar` `:54`、`.breadcrumbs` `:55` |
| `components.css` | `:77-` | `.page-heading` `:77`、`.eyebrow`/`.section-kicker` `:78`、`.stats-grid` `:104`、`.stat-card` `:105-109` |

**Steps:**

- [ ] 从 1.0 按行区间读出样式，落到三个文件
- [ ] `main.tsx` 换 import；`App.tsx` 保留原内容但包一层
- [ ] 红灯：断言 `document.title` 与新样式类存在
- [ ] 绿灯 → `make ci`

**Run:** `cd apps/web && npx vitest run` · `make ci`

**Expected:** `web-check` 全绿；`index.html` 标题为 `Sentrix Home`

---

#### Task 89 · navItems 与路由表（含可达性门禁）

**Files:**

```text
apps/web/src/app/nav.ts              【新】navItems，直译 1.0 src/app.js:78-87
apps/web/src/app/router.tsx          【新】10 条路由
apps/web/src/app/nav.test.ts         【新】可达性门禁
```

**移植对照**

| 2.0 符号 | 1.0 出处 | 值 |
|---|---|---|
| `NAV_ITEMS` | `src/app.js:78-87` | `overview/⌂/家庭概览`、`search/⌕/家庭记忆助手`、`timeline/↕/事件时间线`、`people/◎/人物与关系`、`knowledge/◇/实体与知识`、`library/▦/资料库`、`stories/▶/故事工作室`、`imports/↓/导入队列` |

**门禁（对应后端 `check_reachability` 的前端版）**——三条断言，缺一不可：
1. 每个 `NAV_ITEMS` 项都有对应路由（路径存在）；
2. 每条路由都指向一个真实的页面组件（非 `undefined`）；
3. **反向**：每个 `features/*/` 下的 `*Page.tsx` 都被路由表引用（没有孤儿页面）。

> 第 3 条是本任务的关键：没有它，"路由挂载"和"页面存在"两件事可以各自为真而互不相干。

**Steps:**

- [ ] 写三条断言 → 全红
- [ ] 写 `nav.ts` 与 `router.tsx`（页面组件先给最小占位）→ 全绿
- [ ] 故意删一条路由，确认第 1 条断言变红，再改回
- [ ] `make ci`

**Run:** `cd apps/web && npx vitest run` · `make ci`

**Expected:** 三条断言绿；删路由的反向验证确实变红

---

#### Task 90 · AppShell

**Files:**

```text
apps/web/src/app/AppShell.tsx        【新】侧边栏 + 顶栏 + <Outlet/>
apps/web/src/app/AppShell.test.tsx   【新】
```

**移植对照**：直译 `src/app.js:237-238` 的 `shell()` 模板串——品牌区、
相册切换器（`#space-select`）、两组 `main-nav`、`sidebar-footer` 的 `local-pulse`、
`topbar` 的 `breadcrumbs` 与 `top-actions`。

**行为钉住（来自 1.0 源码，不是转述）：**
- 当前 `view !== "overview"` 时面包屑才出现「返回」（`src/app.js:238` 的三元）
- 当前页高亮：`.nav-item.active`（`styles.css:44`）
- 后端错误时脉冲文案为「本地服务不可用」（`src/app.js:238`）

**Steps:**

- [ ] 红灯：断言 8 个导航项渲染、当前项有 `active`、面包屑含当前页名
- [ ] 绿灯 → `make ci`

**Run:** `cd apps/web && npx vitest run` · `make ci`

---

#### Task 91 · ScopeProvider（相册范围）

**Files:**

```text
apps/web/src/app/ScopeProvider.tsx       【新】
apps/web/src/app/ScopeProvider.test.tsx  【新】
```

**移植对照**：`src/app.js:1162-1172` 的范围初始化语义。
**回落规则是两级，不是"space 不存在就回落"一句话**——`:1168` 的实际写法是：

```js
state.scopeId = storedSpace?.kind === "benchmark" ? storedSpace.id : "";
```

即：存的 id 在列表里找不到 → `""`；**找得到但 `kind !== "benchmark"` → 也回落 `""`**
（1.0 的注释写明这是有意把旧的家庭选择迁到「全部相册」）。
按字面实现"只判存在性"会漏掉第二种，所以本任务的红灯必须有一条覆盖它。
切换时的写回在 `src/app.js:1247`。
2.0 额外把范围同步到 `?scope=` query（裁决 ③ 的 URL 路由要求）。

**Steps:**

- [ ] 红灯：localStorage 有值时初始化取该值；无值时取全部；`?scope=` 优先
- [ ] 绿灯 → `make ci`

**Run:** `cd apps/web && npx vitest run` · `make ci`

---

#### Task 92 · 展示原语

**Files:**

```text
apps/web/src/shared/ui/PageHeader.tsx  StatCard.tsx  Section.tsx
apps/web/src/shared/ui/EmptyState.tsx  FilterChip.tsx  Icon.tsx
apps/web/src/shared/ui/ui.test.tsx               【新】
```

**移植对照**

| 2.0 | 1.0 出处 |
|---|---|
| `PageHeader` | `src/app.js:191` `pageHeader(kicker, title, description, action)` |
| `EmptyState` | `src/app.js:203` `emptyState(title, description, action)` |
| `Icon` | `src/app.js:93` `icon(value, extra)` |
| （`escapeHtml` 参考） | `src/app.js:89` |
| `StatCard` / `Section` / `FilterChip` | `styles.css:104-109` / `.content-section` / `.filter-chip` |

**Steps:**

- [ ] 红灯：`PageHeader` 渲染 kicker/title/description 三段与 action 槽
- [ ] 绿灯 → `make ci`

**Run:** `cd apps/web && npx vitest run` · `make ci`

---

#### Task 93 · 后端补 `GET /api/v1/memory-spaces`（**本计划唯一后端改动**）

**Files:**

```text
apps/api/src/sentrix/api/routes/spaces.py        【新】只读列表路由
apps/api/src/sentrix/main.py                     【改】+1 行 include_router（v1 前缀装配处 :26-34）
apps/api/tests/contract/test_spaces_api.py       【新】
apps/web/src/shared/api/client.ts                【改】+listMemorySpaces
docs/architecture/module-map.md                  【改】+1 行（新文件）
```

**契约**：`GET /api/v1/memory-spaces` → `{"spaces":[{"id","name","kind","status"}]}`
（形状对齐 1.0 `src/api.js` 的 `memorySpaces()` 返回体 `value.spaces`，
以 1.0 前端消费处为准：`src/app.js:1161` `state.spaces = spaceResult[0].value.spaces || []`）

**Steps:**

- [ ] 红灯：契约测试（空库返回空数组；有 space 时字段齐全）
- [ ] 绿灯 → `make ci`
- [ ] **反向验证**：`curl /api/v1/memory-spaces` 实测，确认不在 28 端点清单里的这个真的出现了
- [ ] `docs/architecture/module-map.md` 补行，`check_module_map.py` 绿

**Run:** `make ci` · `curl -s http://127.0.0.1:9598/api/v1/memory-spaces`

---

### F2 · 资料库 + 导入（Task 94–97）

后端最完整的两页，作为壳的试金石。

| Task | 内容 | Files（要点） |
|---|---|---|
| 94 | `LibraryPage` 骨架 + 统计 + 媒体类型筛选 + 排序 | `features/library/LibraryPage.tsx`、`AssetGrid.tsx`【改】、`AssetDetail.tsx`【改】 |
| 95 | `LibraryPage` 详情：文件 / 处理状态；Observation 空状态 | 同上 + 测试 |
| 96 | `ImportsPage`：拖放 + 文件夹选择 + 队列 | `features/imports/ImportsPage.tsx` + 测试 |
| 97 | `ImportsPage`：处理流水线状态 + 导入记录 | 同上 + 测试 |

每任务：红灯 → 绿灯 → `make ci`。

### F3 · 时间线 + 人物（Task 98–101）

| Task | 内容 | Files（要点） |
|---|---|---|
| 98 | `TimelinePage`：筛选行（全部/有人物/有地点/日期） | `features/timeline/TimelinePage.tsx`、`Timeline.tsx`【改】 |
| 99 | `TimelinePage`：按日分组折叠 + 事件卡 + 视频侧栏空状态 | 同上 |
| 100 | `PeoplePage`：双 Tab（关系图谱空状态 / 待整理人物） | `features/people/PeoplePage.tsx`、`PeopleView.tsx`【改】 |
| 101 | `PeoplePage`：现有簇审阅全功能迁入 + 回归测试 | 同上 |

> Task 100/101 是**迁移**：现有 `PeopleView`（含 443 行测试）功能不减，只换外壳与路由。

### F4 · 助手（Task 102–104）

| Task | 内容 | Files（要点） |
|---|---|---|
| 102 | `AssistantPage`：会话侧栏 | `features/assistant/AssistantPage.tsx`、`ClientList` 新组件 |
| 103 | `AssistantPage`：消息流 + agent trace + 停止本轮 | `AssistantPanel.tsx`【改】、`useTurnStream.ts`【改】 |
| 104 | **thought 通道不泄漏断言**（防回归） | `AssistantPage.test.tsx` |

> Task 104 是新增门禁：`split_thought_channel()` 已在后端剥离 `<|channel>thought`，
> 前端加一条断言钉住"气泡里不出现该标记"，避免将来有人绕过。

### F5 · 概览 + 设置 + 知识 + 故事 + 检索（Task 105–109）

| Task | 内容 |
|---|---|
| 105 | `OverviewPage`：范围卡 + 提问框（走助手）+ 4 stat-card |
| 106 | `OverviewPage`：最近事件列表 + 「查看全部」 |
| 107 | `KnowledgePage`：已确认人物真实 + 三块空状态 |
| 108 | `SettingsPage`：健康网格 + 隐私边界 + 审计动作；`StoriesPage` 空状态 |
| 109 | `SearchPage`：现有 `SearchBox`/`SearchResults` 迁入；**按 `raw_scores` 全通道**展示 |

### F6 · 前端防腐门禁（Task 110–112）

| Task | 内容 |
|---|---|
| 110 | 路由可达性门禁扩展：10 页全部纳入（Task 89 建立的机制在此收口） |
| 111 | 每页渲染冒烟测试（空数据 + 有数据两态），沿用 `PeopleView.test.tsx` 的完整契约假件风格 |
| 112 | `Makefile` `web-check` 复核 + 浏览器真实数据验收（53 张照片），逐页截图对拍 1.0 |

---

## 五、验收与门禁

1. **每任务**：红灯 → 绿灯 → **完整 `make ci`**（不是只跑 `tsc -b`）。
   > 这条是 S0 的教训：`tsc --noEmit` 在本仓库是空检查，逐任务验证漏了 per-commit `make ci`。
2. **每个新增门禁必须反向验证**：故意破坏 → 确认变红 → 改回。
   > 「声称在检查、其实没有」在本项目已抓到 9 次，全部靠"真的跑一遍"发现。
3. **假件按契约完整字段造**（沿用 `PeopleView.test.tsx` 的既有纪律，该文件开头有明文说明）。
4. **e2e 断言内容而非计数**：断言具体文案 / 路径 / 数值，不写"有 N 个元素"。
5. **结束验收**：在 153 上对 53 张真实照片跑通 10 个页面，逐页截图与 1.0 对拍。

---

## 六、偏离登记

| # | 偏离 | 证据 |
|---|---|---|
| 1 | **不做 1.0 的全量 `loadAll`**，改每页自取 | 1.0 `src/app.js:1176` 全量拉 16 个接口；2.0 只有其中 **5 个**存在，其余 **11 个**（`dashboard` `entities` `entityGroups` `entityMergeCandidates` `familyGraph` `geoPlaces` `knowledge` `personInsights` `relationships` `stories` `trips`）后端未实现；且分页契约不同：`/timeline` 硬上限 200（`read_timeline.py:25`），`/assets` 无 offset |
| 2 | **`/search` 单列成页**（1.0 无此页） | 裁决 ⑤。1.0 的检索走助手对话（`searchBar()` → assistant turn）；2.0 的 `/api/v1/search` 是直检索，S6 检索补全还要扩展它 |
| 3 | **相册切换器需要新增后端路由** | 裁决 ④；2.0 有 domain 端口（`domain/spaces/ports.py:28`）与仓储（`.../repositories/spaces.py:32`）但无 HTTP 路由 |
| 4 | **加入 `react-router` 依赖**（1.0 用 `useState` 切页） | 裁决 ③；registry 实测可达，`react-router` 8.4.0 |

---

## 七、已知风险与未决项

1. **S5c 未落地**（见「与 S5c 的关系」）。实测 `python3 scripts/check_reachability.py`
   → `reachability: 0 个问题`、exit 0，且 `PENDING_WIRING` 是**空表**
   （`scripts/check_reachability.py:37`，注释写明 Task 75 落地后 S5a 登记的三条已逐条删除）。
   **所以 F 系列开始时这道门禁是全绿的——不要把它当既有红灯。**
2. **`registry.json` 未纳入版本控制**：`--enable-auto-tool-choice --tool-call-parser gemma4`
   丢失会让助手页失败，与前端无关但会影响 Task 104 的验收。
3. **`/knowledge` 页信息密度低**（3 块空状态）：这是后端能力缺口，不靠前端补。
   它同时也是 S6–S11 的可见进度表——每块空状态对应一个后续切片。
4. **「QA 测评」入口**：1.0 指向 `App.vue` 评测台，2.0 本期不做，点击显示"尚未开放"。
   是否要把 1.0 的 PhotoBench 评测台也移植进 2.0，**未决**。
