# Sentrix Home Web 2.0 — S4 实施计划（时间线：地点 / 事件 / 词法 / 元数据）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 打通第四条竖切——把**整个相册**导进来，地点解析到乡镇/村庄、照片按事件聚合、
「按地名/日期/场景找图」能召回，时间线按日期成型。

**Architecture:** 沿用四层（domain / application / infrastructure / api），依赖方向由
`import-linter` 的 5 条契约强制。本切片新增两个域：`domain/places`（地点精度分级：
`admin` 是面、`nearest` 是点，两个槽位语义不同不可互换）与 `domain/memories`（事件聚合
规则 + 事实写入规则）；`domain/retrieval/lexical.py` 补上词法召回的纯规则。
地点是**纯离线**的：区县走 PyGeoCN 的面，乡镇/村庄走 GeoNames 的最近邻点，
运行时零网络依赖。

**Tech Stack:** Python 3.11 / FastAPI / 标准库 `sqlite3`（含 **FTS5**）· PyGeoCN 0.1.5
（+ geopandas）· scipy 1.17 `cKDTree` · pytest / ruff / mypy --strict / import-linter

**依据文档:** `docs/architecture/2026-09-21-sentrix-home-web-2-implementation-design.md`
（§2.2 目录、§2.3 分层、§3.2 三处结构改进、§3.3 地点数据源、§3.4 别名处理、§6 工程标准、
§8 切片表）、`docs/refactor/behaviour-baseline.md`（事件一节，**本轮已修正**）、
`docs/refactor/s4-deviations.md`（19 条偏离的逐条依据）、`docs/refactor/frozen.md`

---

## 本文档是怎么写出来的

做法与 S2/S3 相同，**不是照着意图写的**：

1. 在 153 上建了隔离副本 `/home/asus/s4verify/repo`（真实仓库一个字节都没动）；
2. 先精读 1.0 的实现：`backend/geocoding.py`（308 行，全文）、`backend/db.py` 的事件
   打分段（`:1962-2330`）与事实段（`:3397-3460`）、`backend/retrieval_indexes.py`
   （359 行，全文）、`backend/retrieval/{lexical,metadata}.py`、`backend/pipeline.py` 的
   `process()` 与 `_commit_semantic_image()`；
3. 在副本里按 Task 46 → 54 的顺序**真的写代码**，每个任务先写测试、看到红灯、再实现；
4. 全部做完后跑 `make ci` **全绿**，把改动重组成 9 个 commit；
5. 逐任务做绿灯复核与跨任务回归；
6. **最后**把副本里的实际代码抽出来写成这份文档。

由此有三条硬保证：

- 计划里的**每一个代码块都是逐字节实际跑通过的那一版**（ruff 格式化之后的）；
- 每一个 **Expected 都是实际观察到的输出**，不是推测；
- 每个任务的绿灯都实际跑过，结果见文末「验证记录」。

**必须写明的代价**：Task 46–54 的代码**此刻还没有进真实仓库**，只存在于验证副本里。
执行阶段要做的是把这份计划重新落地一遍——不是「确认一下」，是逐任务真写、真跑。

### 本轮的实际边界（如实说明）

| 部分 | 状态 |
|---|---|
| **S4a（Task 46–54）** | **已实现、已跑通、`make ci` 全绿**（599 passed / 覆盖率 97.29%）。本文档的 Task 46–54 是它的忠实记录 |
| **S4b（Task 55–63）** | **已实施并验证**：9 个任务、8 个 commit（55 / 56 / 57a / 57b / 57c / 58+59 / 60+61 / 62），每个 commit 都有红灯、绿灯与一次完整 `make ci`。记录见文末「S4b 实施记录与验证」 |
| **S4c（Task 64–65）** | **补做 S4b 的两处遗留**：检索融合接线（三路进 `fuse()`）+ 词法投影的 asset 锚点写侧。2 个 commit，各带红灯、绿灯与一次完整 `make ci`。记录见文末「S4c 实施记录与验证」 |

S3 的教训是「计划生成这一步才是错误来源」，所以 S4b 部分**不写推测的代码块**：
写了就是 S2 那种「13–14% 错误率全部发生在计划生成阶段」的重演。S4b 执行时按同一套
方法先跑通再反向写。

---

## 与 S0–S1 / S2 / S3 的关系

S0–S1（`2026-09-21-s0-s1-implementation-plan.md`）是照着意图写的，代价是 19 条缺陷。
S2 改成「先跑通再反向写」，但代码与 Expected 的错误率仍有 13–14%，**且错误全部发生在
「计划生成」这一步**。S3 加了四条生成侧对策（代码块由脚本整块展开、逐字节 diff、
改动文件集必须等于 Files 清单、每条 Run 都重跑）。S4 沿用 S3 的这四条。

### 任务编号

S0–S1 用了 Task 1–24，S2 用了 Task 25–33，S3 用了 Task 34–45，全局连续。
S4 顺着往下排：**Task 46–63**（S4a = 46–54，S4b = 55–63），不另起前缀。

---

## 前置条件

远端仓库 `/home/asus/Github/sentrix-home-web2.0`，分支 `psh2.0`，起点提交 `28b5ed9`
（S3 结束时的工作树干净，`make ci` 全绿、444 passed）。

**S4a 新增三个 Python 依赖**（都进 `apps/api/pyproject.toml` 的 `dependencies`）：

| 包 | 版本 | 为什么 |
|---|---|---|
| `pygeo_cn` | `>=0.1.5,<0.2` | 国内省/市/区县的**面**。1.0 用的就是这个包与这个版本（`PyGeoCN.regeo`）。内置 16 MB 天地图边界 GeoJSON |
| `scipy` | `>=1.15,<2` | 乡镇/村庄最近邻的 `cKDTree`。**NEW-2.0**；1.0 venv 里本来就有 scipy 1.15.3（`reverse_geocoder` 用它） |
| `geopandas` | （由 `pygeo_cn` 传递引入） | PyGeoCN 的运行时依赖，实测拉到 `geopandas 1.1.4` / `pandas 3.0.6` / `shapely 2.1.2` / `pyogrio 0.13.0` |

`mypy` 需要两条 override（两个包都没有 `py.typed`，也没有可用的独立 stub）：

```toml
[[tool.mypy.overrides]]
module = ["PyGeoCN", "PyGeoCN.*", "scipy", "scipy.*"]
ignore_missing_imports = true
```

`pyproject.toml` 的 `package-data` 要带上别名数据文件：

```toml
[tool.setuptools.package-data]
sentrix = [
    "infrastructure/persistence/migrations/*.sql",
    "infrastructure/places/data/*.json",
]
```

### 需要一次性确认的外部条件

```bash
cd /home/asus/s4verify/repo

# FTS5 可用（S4 的词法通道依赖它）
.venv/bin/python -c "import sqlite3; sqlite3.connect(':memory:').execute('CREATE VIRTUAL TABLE t USING fts5(x)'); print('FTS5 OK')"
# FTS5 OK

# PyGeoCN 装好且能反编码
.venv/bin/python -c "from PyGeoCN.regeo import regeo; print(regeo(37.75, 114.77)['address'])"
# {'province': '河北省', 'province_code': '156130000', 'city': '石家庄市', 'city_code': '156130100', 'district': '赵县', 'district_code': '156130133'}

# GeoNames 数据（见下面「地点数据」）
ls -la data/geo/
```

### 地点数据：153 上没有，且 402 MB 拿不动

设计 §3.3 写的国外数据源是 GeoNames `allCountries`（**402 MB**）。
**实测 153 到 `download.geonames.org` 的带宽是 27–34 KB/s**，全量需要约 **3.7 小时**。

本轮改用官方子集（合计 **35 MB**，约 17 分钟）：

| 文件 | 大小 | 覆盖 |
|---|---|---|
| `CN.zip` | 32,195,246 B | 中国区全部记录（约 96 万条），产出 `ADM4` 乡镇 + `PPL` 村庄 |
| `cities15000.zip` | 3,359,366 B | 国外人口 ≥15000 的城镇（实测解出 **34,092** 个可用点） |

**`CN.zip` 就是 `allCountries` 按 `country_code = CN` 切出来的**，两者中国区记录完全相同。
差别只在国外密度：`allCountries` 到村，`cities15000` 到城镇。设计 §3.3 声明的能力边界是
「国内到区县 + 乡镇/村庄，国外到城市/乡镇」——`CN.zip + cities15000.zip` 正好覆盖。
**要做「国外到村庄」才需要全量 402 MB。**

数据放 `data/geo/`（`config.py` 的 `data_dir` 之下），`GeoNamesNearestResolver`
扫该目录下的 `*.txt` / `*.zip`。

> **通用规则（文档同步）**：凡是新增或删除 `apps/api/src/sentrix/` 下 `.py` 文件的任务，
> 都必须同步更新 `docs/architecture/module-map.md` 中该文件的**整行**——路径 / 职责 /
> 层 / **依赖谁** / **被谁依赖**。本切片新增 **21** 个 `.py` 文件，手工维护两列依赖的
> 出错概率接近 100%，所以这两列**由脚本按真实 import 图重算**（做法见文末「生成侧对拍」）。
>
> **通用规则（包的 `__init__.py` 也是一条边）**：`import sentrix.domain.places.models`
> 会被同时记成两条依赖——`sentrix/domain/places/models.py` **和**
> `sentrix/domain/places/__init__.py`。S4 新增的四个包 `__init__.py`
> （`domain/places`、`domain/memories`、`infrastructure/places`、
> `application/memories`）都做成**纯包标记**（不 re-export），就是为了让这条边保持为空。
>
> **通用规则（能力登记）**：每个新能力在 `docs/refactor/capability-map.jsonl` 里加一条
> 记录，每条带 1.0 的确切来源（文件 + 符号）。本切片新增 12 条（全表第 50–61 条）。
>
> **通用规则（OpenAPI）**：S4a **不改任何路由**，所以 `packages/contracts/openapi/sentrix-v1.yaml`
> 不需要重导，`make ci` 的 `--check` 仍然是绿的。S4b 的 Task 58 会改，那一步要重导。
>
> **通用规则（TDD）**：每个任务先只落地测试 → `Run` 确认红 → 再落地实现 → `Run` 确认绿。

---

## 文件结构

本切片新增/改动的文件（其余保持 S3 的原样）：

```text
sentrix-home-web2.0/
├── apps/api/
│   ├── pyproject.toml                          +pygeo_cn / scipy；package-data 加别名文件
│   ├── src/sentrix/
│   │   ├── domain/
│   │   │   ├── media/models.py                 +GpsPoint.distance_km；Observation 加语义字段
│   │   │   ├── media/ports.py                  +ObservationSemantics；ObservationRepository 加三方法
│   │   │   ├── places/                         【新包】地点精度分级
│   │   │   │   ├── models.py                   Place / AdminArea / NearestPlace / 精度与来源
│   │   │   │   ├── aliases.py                  别名展开与行政区后缀归一（纯规则）
│   │   │   │   ├── matching.py                 三条确定性地点匹配规则
│   │   │   │   └── ports.py                    AdminResolver / NearestResolver / PlaceResolver /
│   │   │   │                                   AssetPlaceRepository / AssetPlaceIndex
│   │   │   ├── memories/                       【新包】事件与事实
│   │   │   │   ├── events.py                   八分量打分 / 6 小时窗 / split_guard / 排序
│   │   │   │   ├── facts.py                    事实写入决定与状态迁移
│   │   │   │   └── ports.py                    EventRepository / FactRepository
│   │   │   └── retrieval/
│   │   │       ├── lexical.py                  【新】预分词 / 字段映射 / 命中计分 / 锚点
│   │   │       └── ports.py                    +LexicalIndex / LexicalSearch；VectorRepository.get
│   │   ├── application/
│   │   │   ├── memories/                       【新包】
│   │   │   │   └── assign_events.py            事件归属用例 + 视觉相似度
│   │   │   └── retrieval/
│   │   │       ├── retrieve_lexical.py         【新】词法召回用例
│   │   │       └── retrieve_metadata.py        【新】元数据召回用例
│   │   └── infrastructure/
│   │       ├── places/                         【新包】地点数据源适配
│   │       │   ├── aliases.py                  读别名数据文件（唯一 IO 边界）
│   │       │   ├── pygeocn.py                  PyGeoCNAdminResolver（面）
│   │       │   ├── geonames.py                 GeoNamesNearestResolver（点，cKDTree）
│   │       │   ├── composite.py                OfflinePlaceResolver（面 + 点合成）
│   │       │   └── data/aliases.json           通用 30 条中英地名桥
│   │       └── persistence/
│   │           ├── migrations/0005_timeline.sql  asset_places / events / event_observations /
│   │           │                                 facts / observation_search_terms / FTS5
│   │           └── repositories/
│   │               ├── asset_places.py         【新】
│   │               ├── events.py               【新】
│   │               ├── facts.py                【新】
│   │               ├── search_terms.py         【新】词法投影读写
│   │               ├── assets.py               +Observation 语义字段 / enrich / list_* 
│   │               └── vectors.py              +get（按 (space, source_type, source_id) 取一条）
│   └── tests/{unit,integration}/               12 个新文件、155 条新用例
└── docs/{architecture/module-map.md, refactor/{behaviour-baseline,frozen,s4-deviations}.md}
```

---

## 依赖图与执行顺序

S4a 的依赖图如下——每一步只依赖它上面的东西，反向依赖一条都没有：

```text
Task 46  domain/places：模型 / 别名规则 / 匹配规则（纯规则）
   │
   ├── Task 47  PyGeoCN 适配器（面：省/市/区县）
   │      │
   │      └── Task 48  GeoNames 适配器（点：乡镇/村庄/国外城镇）+ 合成解析器
   │             │
   │             └── Task 49  观测语义字段 + 迁移 0005
   │                    │
   │                    ├── Task 50  事件打分（纯规则，八分量）
   │                    │      │
   │                    │      └── Task 51  事件归属用例（候选 → 打分 → 折叠/新建）
   │                    │             │
   │                    │             └── Task 52  事实写入规则（两阶段）
   │                    │
   │                    └── Task 53  词法索引（迁移 0005 的投影表 + 逐条写入）
   │                           │
   │                           └── Task 54  词法召回 + 元数据召回（两个通道的读取侧）
```

### 为什么不能换序

| 约束 | 原因 |
|---|---|
| **46 必须最先** | 后面每个模块都要 `Place` / `AdminArea` / `NearestPlace`；`matching.py` 的三条规则被 `RetrieveMetadata` 直接引用 |
| **47 在 46 之后，48 在 47 之后** | 48 的合成解析器（`composite.py`）要同时吃 `AdminResolver` 与 `NearestResolver` 两个端口，而端口在 46 定义、47 实现第一个 |
| **49 在 48 之后** | 迁移 0005 的 `asset_places` 表列名直接对应 `Place` 的字段；`observations` 的语义列对应 `Observation` 的新字段。表结构先于它的读写代码落地 |
| **50 只依赖 46** | 打分是纯函数，吃的是 `ObservationAnchor` / `EventAnchorSet`，不碰数据库 |
| **51 在 50 与 49 之后** | 它把「资产 → 观察 → 锚点 → 打分 → 事件」串起来，四样缺一不可 |
| **52 在 51 之后** | 事实写入发生在事件归属之后（`pipeline.process()` 的顺序：`merge_observation_into_event` → `maintain_fact`） |
| **53 在 49 之后** | 词法投影要读 `observations` 的语义列，那些列由 49 加 |
| **54 在 53 之后** | 召回读的是 53 写的投影 |
| **48 依赖下载好的 GeoNames 数据** | 没有数据时 `available` 为假、`resolve_nearest` 恒返回 `None`——**这是设计好的降级**，不会让 46–54 里任何一步变红，所以顺序本身不阻塞；但 Task 48 的验收必须有真数据 |

### 三条硬约束（不换序之外）

1. **归并顺序：视觉向量必须先写。** `score_event_candidate` 的 `visual_boost`（权重 0.05）
   与 `split_guard` 的判定都读 `memory_vectors` 里 `space='visual' & source_type='asset'`
   的向量。顺序写反**不报错**——`_event_visual_similarity` 返回 `(0.0, False)`，
   `visual_boost` 恒 0、`split_guard` 因 `not visual_available` 更易命中，事件照分照建，
   只是分错了。1.0 在 `pipeline.py:315-325` 用注释钉过这件事，2.0 用
   `test_assign_events.py` 的两条对照用例钉。**S4b 的 Task 56 落地时钉死。**
2. **词法投影挂在导入路径上逐条写。** 1.0 的 `refresh_from_observation` 存在但生产零调用
   （见 `s4-deviations.md` §1 第 4 项）。2.0 的 `RetrieveLexical` **不做懒补建**——
   留一条「查询时整表重建」的退路，等于给「导入没写投影」留一个永远不会红的失败模式。
3. **GPS 的 2 公里上限只属于候选打分。** `_event_pair_score` 那套 1000 米的不移植
   （见 `s4-deviations.md` §2.2）。

---

## 移植清单：1.0 有什么、S4 要哪些

1.0 的地点逻辑集中在 `backend/geocoding.py`（308 行），事件与事实在 `backend/db.py`
   的 `:1962-2330` / `:3397-3460`，词法在 `backend/retrieval_indexes.py`（359 行）+
   `backend/retrieval/{lexical,metadata}.py`。**S4 不做整体移植**——下表逐项判断。

### A. 移植（S4 需要）

| # | 能力 | 1.0 位置 | 2.0 落点 | 移植方式 |
|---|---|---|---|---|
| 1 | 国内行政区反编码 | `geocoding.py:103-140` `_lookup_pygeo` | `infrastructure/places/pygeocn.py` | 原样：`confidence=0.90`、`precision = "district" if district else "city"`、三级空即失败 |
| 2 | 地点条件匹配三规则 | `geocoding.py:269-308` `place_text_matches` | `domain/places/matching.py` | 原样：整串子串 / 行政区去后缀双向子串 / 中英别名 |
| 3 | 行政区后缀归一 | `geocoding.py:222-230` `_PLACE_ADMIN_SUFFIXES` / `_strip_admin_suffix` | `domain/places/aliases.py` | 原样：按序取第一个命中、只砍一次（`内蒙古自治区` → `内蒙古自治`，1.0 就是这个行为） |
| 4 | 通用中英地名桥 | `geocoding.py:234-258` `_PLACE_ALIASES` | `infrastructure/places/data/aliases.json` | **搬 30 条、删 2 条**（用户裁决 #5） |
| 5 | 八分量事件打分 | `db.py:2000-2080` `_event_candidate_score` | `domain/memories/events.py` | 原样：权重 / 阈值 0.50 / 6 小时窗 / split_guard / 时空加成，逐值对齐 |
| 6 | 事件候选排序 | `db.py:1974-1975` | `domain/memories/events.py` `rank_candidates` | **改**：同分加 `asset_id` 兜底键（1.0 只排分数，顺序取决于 SQL 返回序） |
| 7 | 观察折进事件 | `db.py:2296-2330` `merge_observation_into_event` | `infrastructure/persistence/repositories/events.py` `fold_observation` | 原样：`confidence = MAX(...)`、summary 不重复追加、时间窗取 min/max |
| 8 | 事件两两合并 | `db.py:5482` `_merge_events` | `events.py` `merge` | **改**：source 直接删（1.0 标 `merged` 不删，留下孤儿行） |
| 9 | 事实写入 | `db.py:3397-3430` `maintain_fact` | `domain/memories/facts.py` + `repositories/facts.py` | 原样：同值合并证据 + `MAX(confidence)`、改口进 `pending` 并记 `supersedes_fact_id` |
| 10 | 事实确认 / 拒绝 | `db.py:3432-3460` | `facts.py` `confirm` / `retract` + `supersede_others` | **改**：作废范围收敛到本 scope（1.0 不带 scope） |
| 11 | 预分词 | `retrieval_indexes.py:43-64` `pre_tokenize` / `_fts_content` | `domain/retrieval/lexical.py` | 原样：整串 + 拉丁词 + 中文 bigram，**单字不成 token** |
| 12 | FTS 命中计分 | `retrieval_indexes.py:296-334` `search_fts` | `lexical.py` `score_token_hits` | 原样：命中计数 + `exact_boost = 2.0`，**不引入 BM25** |
| 13 | 投影字段映射 | `retrieval_indexes.py:70-100` | `lexical.py` 的 `FIELD_SOURCES` | **缩**：`detail` 那一路不建（属于 S6） |
| 14 | 地名 / 时间 / 文件名锚点 | `retrieval_indexes.py:225-245` `_asset_anchor_rows` | `lexical.py` 的 `ANCHOR_FIELD_TYPES` / `time_anchor_tokens` / `file_stem_is_indexable` | 原样：`geo` / `time` / `file` 三类，`file` 排除 `event_event_*` 与 `kf_*` |
| 15 | 词法召回 | `retrieval/lexical.py:50-80` | `application/retrieval/retrieve_lexical.py` | **改**：去掉 `_ensure_populated` 懒补建 |
| 16 | 元数据召回 | `retrieval/metadata.py` | `application/retrieval/retrieve_metadata.py` | **改**：地点预筛走 `Place`；上限落成常量 200 |
| 17 | 视觉相似度 | `db.py:2082-2110` `_event_visual_similarity` | `assign_events.py` `EventVisualSimilarity` | 原样：取最新 asset 向量、限定同 `model_name`、维度一致、取余弦最大 |

### B. 明确不移植（留给后面的切片）

| 能力 | 1.0 位置 | 留给谁 | 理由 |
|---|---|---|---|
| `_event_pair_score` / `consolidate_events` | `db.py:2192-2220` / `:2221` | 不做 | 全部 6 个调用点都在 `scripts/`；与候选打分对同一距离给两个答案。见 `s4-deviations.md` §2.2 |
| `event_participants` / `event_entities` / `select_event_cover` | `db.py` 事件段 | **S5** | 强依赖 `entities`；S4 阶段 `person_score`（权重 0.20）恒为 0 |
| `person_bridge` 词法字段 | `retrieval_indexes.py:150` | **S5** | 同上，没有 `entity_mentions` 可桥 |
| `_terms_from_detail`（`visible_details` / `regions` / `text_blocks`） | `retrieval_indexes.py:88-100` | **S6** | S4 的 `Observation` 没有 `detail` 字段 |
| `reverse_geocoder` 国外回退 | `geocoding.py:143-194` | 不做 | 它自带的数据是 `cities1000` 的子集，与 GeoNames 同源；两套并存只会给出两个答案 |
| `search`（LIKE 子串回退） | `retrieval_indexes.py:336-345` | 不做 | FTS5 缺席时的降级分支，`make ci` 已实测 FTS5 可用，留一条永不执行的分支只会让人以为它被验证过 |
| `facts` 的 `purge_unanchored_facts` | `db.py:3439-3453` | **S5** | 它按 `entities` 里 confirmed person 的名字过滤，S5 之前没有 `entities` |
| `trips` / `stories` / `memory_feedback` | — | 冻结 | `frozen.md` 已列（表 0 行） |

---

# S4a — 时间线后端（Task 46–54）

## Task 46: 地点域地基（模型 / 别名 / 匹配规则）

**依赖：** 无。这是 S4 的起点。

**Files:**
- Create: `apps/api/src/sentrix/domain/places/__init__.py`
- Create: `apps/api/src/sentrix/domain/places/models.py`
- Create: `apps/api/src/sentrix/domain/places/aliases.py`
- Create: `apps/api/src/sentrix/domain/places/matching.py`
- Create: `apps/api/src/sentrix/domain/places/ports.py`
- Test: `apps/api/tests/unit/test_places_models.py`（15 条）
- Test: `apps/api/tests/unit/test_places_matching.py`（13 条）

**为什么 `Place` 是两个槽位。** 1.0 把地点压成一个扁平 dict（`_lookup_pygeo` 与
`_lookup_reverse_geocoder` 两个分支的键还不一样），`province/city/district` 来自**面**
（行政区边界，点落进去就是权威归属，能做硬条件过滤），GeoNames 那一路的 `name` 来自
**最近邻点**（只能说「附近」）。混成一个字符串之后，检索层看不出哪个词是权威的，
于是「萧山区」与「赵州桥附近」在过滤时被同等对待。

**`precision` 是四级不是三级。** 设计 §3.2 写的是 `"district" | "township" | "locality"`，
但 1.0 有第四级——`_lookup_pygeo` 是 `"district" if district else "city"`（`geocoding.py:121`），
`_lookup_reverse_geocoder` 恒 `"city"`（`:172`）。把「只解析到市」塞进 `district`
会让下游以为拿到了区县级归属，而这正是分槽要防的那种谎。

**核心代码**（`domain/places/models.py`，节选）：

```python
class PlacePrecision(StrEnum):
    DISTRICT = "district"
    CITY = "city"
    TOWNSHIP = "township"
    LOCALITY = "locality"


@dataclass(frozen=True, slots=True)
class AdminArea:
    province: str = ""
    city: str = ""
    district: str = ""

    def label(self) -> str:
        """``浙江省杭州市萧山区``。同名层级（直辖市 province==city）只出现一次。"""
        parts: list[str] = []
        for part in (self.province, self.city, self.district):
            if part and part not in parts:
                parts.append(part)
        return "".join(parts)


@dataclass(frozen=True, slots=True)
class Place:
    admin: AdminArea
    nearest: NearestPlace
    precision: PlacePrecision
    source: PlaceSource
    gps: GpsPoint | None = None
    confidence: float = 0.0

    def __post_init__(self) -> None:
        if self.admin.is_empty and self.nearest.is_empty:
            raise ValueError("地点解析结果两个槽位都为空——这种情况必须返回 None，不是 Place")

    def display(self) -> str:
        """``浙江省杭州市萧山区 · 赵州镇大石桥村附近``。"""
        head = self.admin.label()
        tail = self.nearest.label()
        if tail:
            tail = f"{tail}附近"
        return " · ".join(part for part in (head, tail) if part)
```

`display()` 只在有 `nearest` 时加「附近」——`admin` 是权威归属，给它加「附近」会把
确定性说没。

`searchable_text()` 刻意**不含 GPS 坐标**：1.0 的 `label` 在 PyGeoCN 那条路里可能退化成
坐标串，为此专门写了一个 `DISPLAY_COORDINATE_RE` 正则去挡（`_event_display_place`）。
2.0 直接不给坐标进入这个词的机会。

**Run:**

```bash
.venv/bin/pytest apps/api/tests/unit/test_places_models.py apps/api/tests/unit/test_places_matching.py -q
```

**Expected:**

```
28 passed
```

---

## Task 47: PyGeoCN 适配器（面：省 / 市 / 区县）

**依赖：** Task 46。

**Files:**
- Create: `apps/api/src/sentrix/infrastructure/places/__init__.py`
- Create: `apps/api/src/sentrix/infrastructure/places/pygeocn.py`
- Modify: `apps/api/pyproject.toml`（+`pygeo_cn`）
- Modify: `pyproject.toml`（+mypy override）

**移植自 1.0 `backend/geocoding.py:103-140` 的 `_lookup_pygeo`**，四个数值/分支原样：

| 项 | 1.0 | 2.0 |
|---|---|---|
| confidence | `0.90`（`:134`） | `PYGEOCN_CONFIDENCE = 0.90` |
| precision | `"district" if district else "city"`（`:121`） | `precision_for(admin)` |
| 三级全空 | 返回 `{}`（`:118-119`） | 返回 `None` |
| 同名层级去重 | `geocoding.py:124-127` | `AdminArea.label()` |

**一处命名偏离**：1.0 把 source 记成 `"tianditu"`（天地图是 PyGeoCN 内置边界数据的
来源方），2.0 记成 `"pygeocn"`——设计 §3.3 与 `frozen.md` 都按数据源命名。

**实测延迟**：`PyGeoCN.regeo` 每次调用都 `geopandas.read_file` 三份 GeoJSON，
**单次约 0.30 秒**。已加按坐标的 LRU（小数点后 5 位 ≈ 1.1 米），重复坐标不再重复解码，
但**每个新坐标仍要付一次 0.3 秒**。这是 1.0 就有的成本（`pipeline.prepare_asset`
对每张图各调一次），2.0 不放大也不假装消除。

**Run:**

```bash
.venv/bin/python -c "
from sentrix.domain.media.models import GpsPoint
from sentrix.infrastructure.places.pygeocn import PyGeoCNAdminResolver
r = PyGeoCNAdminResolver()
print('available:', r.available)
print(r.resolve_admin(GpsPoint(37.75, 114.77)))
"
```

**Expected:**

```
available: True
AdminArea(province='河北省', city='石家庄市', district='赵县')
```

---

## Task 48: GeoNames 适配器（点：乡镇 / 村庄 / 国外城镇）+ 合成解析器

**依赖：** Task 47。**NEW-2.0**（用户裁决 #1）——1.0 完全没有这一层。

**Files:**
- Create: `apps/api/src/sentrix/infrastructure/places/geonames.py`
- Create: `apps/api/src/sentrix/infrastructure/places/composite.py`
- Modify: `apps/api/pyproject.toml`（+`scipy`）

**数据源**（设计 §3.3）：

| 级别 | 特征码 | 性质 |
|---|---|---|
| 乡镇/街道 | `ADM4` | 点，最近邻 |
| 村庄/居民点 | `PPL` / `PPLA*` / `PPLC` / `PPLX` / `PPLL` | 点，最近邻 |
| 国外城镇 | 同上 | 点，最近邻 |

**实现要点：**

1. 点存成**三维单位向量**再建 KD-tree。直接在 (lat, lon) 度数上建树在日界线与高纬度
   会给出错误的最近邻；单位向量的欧氏弦长与球面大圆距离单调对应，换算回公里即可。
2. 一次加载、常驻内存，逐条解析时不碰磁盘（设计 §3.3：「导入时计算一次、结果落库」）。
3. 半径上限 `MAX_NEAREST_RADIUS_M = 50_000`。**这是相对 1.0 的有意偏离，需要用户确认**
   ——1.0 算了 `distance_km` 但从不读取，对太平洋正中的坐标会返回 `"Honolulu, Hawaii, US"`。

**实测（真数据，`CN.zip` + `cities15000.zip`）：**

```
索引加载: 0.0 秒, counts(乡镇,国内,国外) = (11878, 885912, 31975)

查询点                          display()                                  precision/source
--------------------------------------------------------------------------------------------
赵县 (37.75, 114.77)           河北省石家庄市赵县 · 赵州石塔村附近              district / pygeocn  (384.7ms)
萧山 (30.1839, 120.2642)       浙江省杭州市萧山区 · 袁浦永久附近               district / pygeocn  (299.6ms)
北京 (39.9042, 116.4074)       北京市东城区 · 羊坊店Wangfujing附近           district / pygeocn  (299.4ms)
清迈 (18.7883, 98.9853)        清迈附近                                   locality / geonames  (302.1ms)
东京 (35.6762, 139.6503)       永福附近                                   locality / geonames  (299.8ms)
太平洋 (0.0, -160.0)           None                                      (303.0ms)
```

**三个数字与设计 §3.3 对上了**：`ADM4` 11,878（设计写 11,450 是「中文名以镇/乡/街道
结尾」的那部分，总数 11,878 一致）、`PPL` 860,831（设计原文就是这个数）、
中国区记录合计 895,673。

最后一行是半径上限生效的直接证据。

**这一版的实测暴露了两个只有真数据才看得见的 bug**（合成数据与单元测试都发现不了）：

| bug | 症状 | 修法 |
|---|---|---|
| `CN.zip` 里 `readme.txt` **排在 `CN.txt` 前面**（8,843 B vs 132,055,747 B） | 按 `namelist()` 顺序取第一个成员 → 解析出 **0 个点**、不报错，`counts` 变成 `(0, 2117, 31975)`，整个国内最近邻静默失效 | `_pick_member`：同名成员优先（`CN.zip` → `CN.txt`），否则取最大的 `.txt` |
| GeoNames 中国记录的 `name` 列是**罗马字**，中文名在 `alternatenames` 列 | 展示变成 `ZhaozhouShitacun附近` / `Xiaoshan附近`——名字对、语言不对，而展示层那句「附近」正是给用户看的 | `_display_name`：`name` 里没有汉字时从备选名取**第一个含汉字的** |

**分池也是实测逼出来的。** 只用 `cities15000.zip` 时坐标为萧山的照片解析出
`浙江省杭州市萧山区 · Xiaoshan附近`。解析器因此把点分成三个池：乡镇（`ADM4`）、
**国内**居民点（`country == "CN"`）、**国外**居民点，**先国内池，国内有命中就不看国外池**。
这不是设计的新增内容，是把 §3.3 那张表（国内 → `ADM4`/`PPL`，国外 → `allCountries`
的 `PPL*`）落成代码。

`test_zip_member_picker_skips_the_readme` 与 `test_parse_dump_prefers_the_chinese_alternate_name`
分别钉住前两个 bug；`test_domestic_pool_wins_over_a_closer_foreign_point` 钉住分池——
合成数据里一个美国点与查询点**完全重合**（距离 0），中文村庄在十几米外，前者必须输。

**分池是实测逼出来的。** 上面那次实测跑在**只有 `cities15000`** 的目录上，结果里
`萧山 -> 浙江省杭州市萧山区 · Xiaoshan附近`——名字对、语言不对。原因是
`cities15000` 里中国城市的主名是罗马字（`Xiaoshan` / `Hangzhou`），而设计 §3.3 的表
要求国内走 `CN.zip` 的 `ADM4` / `PPL`（中文名）。所以解析器把点分成三个池：
乡镇（`ADM4`）、**国内**居民点（`country == "CN"`）、**国外**居民点，
**先国内池，国内有命中就不看国外池**。这不是设计的新增内容，是把 §3.3 那张表
落成代码——混池时一张照片会得到「萧山区 · Xiaoshan附近」这种半英半中的展示。

`test_domestic_pool_wins_over_a_closer_foreign_point` 用的合成数据里，一个美国点
（`Nowhere`）与查询点**完全重合**（距离 0），中文村庄在十几米外；分池生效时前者必须输。

**Run:**

```bash
.venv/bin/pytest apps/api/tests/integration/test_geonames_resolver.py -q
```

**Expected:**

```
15 passed
```

**合成解析器与 1.0 的分支结构不同**，这是设计 §3.2 要求的差别的直接后果：

```python
# 1.0 geocoding.py:196-216 —— 二选一，先到先得
result = self._lookup_pygeo(lat, lon)      # 国内
if result:
    return result
result = self._lookup_reverse_geocoder(...)  # 国外，上面有结果时根本不会走到
if result:
    return result
return {}
```

```python
# 2.0 composite.py —— 互补，两个都问
admin = self._admin.resolve_admin(gps)
nearest = self._nearest.resolve_nearest(gps)
if admin is None and nearest is None:
    return None
```

1.0 只能二选一是对的——它只有一个 `label` 槽位，两个来源写进去会互相盖掉。2.0 有
`admin` / `nearest` 两个**语义不同、不可互换**的槽位，必须都问一遍才能拼出设计给的
展示形式 `浙江省杭州市萧山区 · 赵州镇大石桥村附近`（前半段来自 PyGeoCN，后半段来自 GeoNames）。

---

## Task 49: 观测语义字段 + 迁移 0005

**依赖：** Task 48。

**Files:**
- Create: `apps/api/src/sentrix/infrastructure/persistence/migrations/0005_timeline.sql`
- Modify: `apps/api/src/sentrix/domain/media/models.py`（Observation 加 8 个字段 + `GpsPoint.distance_km`）
- Modify: `apps/api/src/sentrix/domain/media/ports.py`（+`ObservationSemantics`、Repository 加三方法）
- Modify: `apps/api/src/sentrix/infrastructure/persistence/repositories/assets.py`

**迁移 0005 建六张表**，字段语义取自 1.0 的 `assets.metadata_json.reverse_geocode` /
`events` / `facts` / `observation_search_terms` / `observation_search_fts`，
按设计 §3.2 做三处结构改进。

**① `asset_places` 是独立表，不是 `assets` 上的 8 个列。** 理由是「没解析过」与
「解析了但没结果」必须是两个可区分状态——前者没有行，后者有行。压在 `assets` 的列上时
这两件事都是空字符串，与 1.0 把 `{}` 和缺失混为一谈是同一个毛病。

```sql
CREATE TABLE asset_places (
    asset_id TEXT PRIMARY KEY REFERENCES assets(id),
    scope_id TEXT NOT NULL DEFAULT 'home-default',
    admin_province TEXT NOT NULL DEFAULT '',
    admin_city TEXT NOT NULL DEFAULT '',
    admin_district TEXT NOT NULL DEFAULT '',
    nearest_township TEXT NOT NULL DEFAULT '',
    nearest_locality TEXT NOT NULL DEFAULT '',
    nearest_distance_m REAL,
    precision TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0,
    gps_latitude REAL,
    gps_longitude REAL,
    resolved_at TEXT NOT NULL
);

CREATE INDEX idx_asset_places_district ON asset_places(scope_id, admin_district);
```

**② `observations` 补语义列 + `revision`。** 1.0 把语义散在多个 `*_json` 列里且没有
`revision`，词法投影因此无法判断自己是不是过期。

```sql
ALTER TABLE observations ADD COLUMN place TEXT NOT NULL DEFAULT '';
ALTER TABLE observations ADD COLUMN activity TEXT NOT NULL DEFAULT '';
ALTER TABLE observations ADD COLUMN event_type TEXT NOT NULL DEFAULT '';
ALTER TABLE observations ADD COLUMN objects_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE observations ADD COLUMN clothing_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE observations ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;
```

**③ 词法投影是派生表**，FTS5 只索引 `tokens` 一列，其余四列 `UNINDEXED` 只做取回
（与 1.0 `retrieval_indexes.py:139-149` 一致——排序分数在 Python 侧算）。

```sql
CREATE VIRTUAL TABLE observation_search_fts USING fts5(
    tokens,
    scope_id UNINDEXED,
    field_type UNINDEXED,
    asset_id UNINDEXED,
    observation_id UNINDEXED
);
```

**`events` 与 `facts` 两张表**照搬 1.0 的字段语义。**不建** `event_participants` /
`event_entities`（依赖 S5 的 `entities`，不在这里留空壳），也不建 `event_revisions`。

**Run:**

```bash
.venv/bin/pytest apps/api/tests/integration/test_migrations.py -q
```

**Expected:** `passed`（迁移能被 `Database.migrate()` 幂等应用）

---

## Task 50: 事件打分（纯规则，八分量）

**依赖：** Task 46。

**Files:**
- Create: `apps/api/src/sentrix/domain/memories/__init__.py`
- Create: `apps/api/src/sentrix/domain/memories/events.py`
- Create: `apps/api/src/sentrix/domain/memories/ports.py`
- Test: `apps/api/tests/unit/test_event_scoring.py`（23 条）

**这是 S4 最重要的一条移植，也是对行为基线的一处修正。**
`behaviour-baseline.md` 的「事件」一节原文写的是**四个**信号，实测是**八项**：

```python
WEIGHTS = {
    "time": 0.25,
    "location": 0.25,
    "visual_place": 0.15,
    "event_type": 0.05,
    "activity": 0.20,
    "objects": 0.05,
    "person": 0.20,
    "visual_boost": 0.05,
}

total = (
    WEIGHTS["time"] * time_score
    + WEIGHTS["location"] * location_score
    + WEIGHTS["visual_place"] * visual_place_score
    + WEIGHTS["event_type"] * event_type_score
    + WEIGHTS["activity"] * activity_score
    + WEIGHTS["objects"] * object_score
    + WEIGHTS["person"] * person_score
    + WEIGHTS["visual_boost"] * visual_boost
    + spatio_temporal_bonus
)
if split_guard:
    total = 0.0
```

**八项权重之和是 1.20，不是 1.0**——时空加成（0.20）是这八项之外再叠加的，最后靠
`max(0.0, min(1.0, total))` 收口。照「权重必须归一」去改会把饱和区的形状改掉。

按四个信号去实现会丢掉 **0.70** 的权重。其中 `objects`（0.05）那一项还会**连带**
影响 `split_guard`：`corroborated = bool(object_score or person_score)`，
`object_score` 恒 0 时只要语义冲突且视觉 < 0.45 就会拆事件——**同一场家庭活动的不同
瞬间会被拆成十几个事件**。这不是「少 5% 分数」，是事件数整体失真。

**常量全部取自 1.0 的确切位置：**

| 常量 | 值 | 1.0 位置 |
|---|---:|---|
| `CANDIDATE_WINDOW_HOURS` | 6.0 | `db.py:1967` |
| `CANDIDATE_THRESHOLD` | 0.50 | `db.py:1969` |
| `TIME_DECAY_MINUTES` | 360.0 | `db.py:2004` |
| `LOCATION_DECAY_KM` | 2.0 | `db.py:2015` |
| `VISUAL_BOOST_FLOOR` / `CEILING` | 0.70 / 1.00 | `db.py:2071-2074` |
| `SPATIO_TEMPORAL_BONUS` / `_AT` | 0.20 / 0.90 | `db.py:2078` |
| `SPLIT_GUARD_VISUAL` | 0.45 | `db.py:2058` |
| `MISSING_TIME_SCORE` | 0.25 | `db.py:2006` |
| `ACTIVITY_MATCH` / `OBJECT_MATCH` / `PERSON_MATCH` / `EVENT_TYPE_MATCH` | 0.9 / 0.8 / 0.8 / 0.8 | `db.py:2030,2043,2047,2027` |

**`VisualSignal` 把「没向量」与「向量不像」分开**，这是 1.0 用 `(0.0, False)` 元组
表达的同一件事；合成一个 `0.0` 会让 `split_guard` 把「没向量」误当成「向量很不像」。

**Run:**

```bash
.venv/bin/pytest apps/api/tests/unit/test_event_scoring.py -q
```

**Expected:**

```
23 passed
```

---

## Task 51: 事件归属用例（候选 → 打分 → 折叠 / 新建）

**依赖：** Task 50、Task 49。

**Files:**
- Create: `apps/api/src/sentrix/application/memories/__init__.py`
- Create: `apps/api/src/sentrix/application/memories/assign_events.py`
- Modify: `apps/api/src/sentrix/domain/retrieval/ports.py`（`VectorRepository.get`）
- Modify: `apps/api/src/sentrix/infrastructure/persistence/repositories/vectors.py`
- Test: `apps/api/tests/integration/test_assign_events.py`（9 条）

**移植自 1.0 `db.py:2287-2330`（`merge_observation_into_event`）+ `:1962-1990`
（`_event_candidates`）**，流程一模一样：列本 scope 的活动事件 → 6 小时硬窗过滤 →
八分量打分 → 取最高分 → `>= 0.50` 折进去，否则新建。

**`ACTIVE_EVENT_SCAN_LIMIT = 1000`** 取自 1.0 `db.py:2221` 的 `list_events(1000, scope_id)`
——活动事件超过这个数时打分只在前 1000 个里做。写成常量而不是函数里的字面量，
免得被当成随手可调的数。

**归并顺序是硬约束**，两条对照用例把它钉住：

```python
def test_without_a_visual_vector_the_boost_is_zero_and_flagged_unavailable(repository_set):
    """向量没写 → ``available`` 为假。**不报错、不拆事件**，只是少 0.05 分。"""
    result = _fold_pair(repository_set, write_vectors=False)

    assert result.created is False
    assert result.score is not None
    assert result.score.visual_available is False
    assert result.score.visual_boost == pytest.approx(0.0)
```

**`confirmed_people` 默认恒为空集**，参数注释写明这是 S5 的输入而不是编出来的假来源：

```python
# S5 之前恒为空集：没有 entity_mentions 就没有「已确认人物」。
# person_score（权重 0.20）因此在 S5 落地前恒为 0，这一点写在计划里，
# 不在这里编一个假来源。见 s4-deviations.md §4。
self._confirmed_people = confirmed_people
```

**Run:**

```bash
.venv/bin/pytest apps/api/tests/integration/test_assign_events.py -q
```

**Expected:**

```
9 passed
```

---

## Task 52: 事实写入规则（两阶段，修 1.0 内部矛盾）

**依赖：** Task 51。

**Files:**
- Create: `apps/api/src/sentrix/domain/memories/facts.py`
- Create: `apps/api/src/sentrix/infrastructure/persistence/repositories/facts.py`
- Test: `apps/api/tests/unit/test_fact_rules.py`（14 条）

**所谓「两阶段」指的是 1.0 的两条写事实路径行为不一致**（用户裁决 #3）：

| 路径 | 1.0 行为 |
|---|---|
| `pipeline.process()`（`pipeline.py:346-350`） | **会**调 `maintain_fact`，并先过 `is_confirmed_person_name` 门 |
| `pipeline._commit_semantic_image()`（`pipeline.py:570-635`） | **不调**，整段没有一处 `maintain_fact` |

同一张照片走哪条路径取决于它是什么时候导进来的，于是「事实有没有被记下来」变成了
导入批次的历史属性。这不是设计选择，是 1.0 的内部矛盾。2.0 把「哪些字段算一条可写
事实」和「写入动作是什么」抽成纯规则，两条路径共用同一个决定点。

**写入语义原样保留：**

- 同一个 `(scope, subject, predicate)` 只有一条 `active`；
- 新值与当前 `active` 的 `object` **相同** → 不新建，只合并证据 + 取 `MAX(confidence)`；
- 不同 → 新建一条：当前没有 `active` 时新条就是 `active`，有则新条是 `pending`
  （等人确认），并把 `active` 记进 `supersedes_fact_id`。

**一处修复**：1.0 `confirm_fact` 的作废 UPDATE **不带 scope**（`db.py:3434-3438`），
跨相册的同名事实会被一起作废。2.0 把范围收在本 scope 内
（`test_supersede_others_is_scoped` 断言另一个相册的行不动）。

**Run:**

```bash
.venv/bin/pytest apps/api/tests/unit/test_fact_rules.py apps/api/tests/integration/test_timeline_repositories.py -q
```

**Expected:**

```
35 passed
```

---

## Task 53: 词法索引（FTS5 投影 + 逐条写入）

**依赖：** Task 49。

**Files:**
- Create: `apps/api/src/sentrix/domain/retrieval/lexical.py`
- Create: `apps/api/src/sentrix/infrastructure/persistence/repositories/search_terms.py`
- Modify: `apps/api/src/sentrix/domain/retrieval/ports.py`（+`LexicalIndex` / `LexicalSearch`）
- Test: `apps/api/tests/unit/test_lexical_rules.py`（16 条）

**预分词：单字永远不是 token。**

```python
def pre_tokenize(value: object) -> tuple[str, ...]:
    """整串 + 拉丁词 + 中文 bigram，排序去重。**单字不是 token。**"""
    text = normalize_text(value)
    if not text:
        return ()
    tokens: set[str] = set()
    if len(text) > 1:
        tokens.add(text)
    tokens.update(_LATIN_RE.findall(text))
    chars = [char for char in text if not char.isspace()]
    for index in range(len(chars) - 1):
        tokens.add(chars[index] + chars[index + 1])
    return tuple(sorted(tokens))
```

`浅黄色毛绒睡衣` → `浅黄 / 浅黄色毛绒睡衣 / 色毛 / 毛绒 / 绒睡 / 睡衣 / 黄色`。
单个汉字不会出现，所以孤立的「色」/「毛」永远不会被算作支持。

**计分是命中计数，不是 BM25。** 全仓 `bm25(` 零次调用；FTS5 在这里的角色是**倒排索引**。

```python
def score_token_hits(token_hits, *, whole_query, exact_assets):
    whole = normalize_text(whole_query)
    scored = []
    for asset_id, count in token_hits.items():
        boost = EXACT_BOOST if (count == 1 and whole and asset_id in exact_assets) else 0.0
        scored.append((asset_id, count + boost))
    return sorted(scored, key=lambda item: (-item[1], item[0]))
```

排序键的第二项 `item[0]`（asset_id）是 2.0 加的：1.0 用 `sort(reverse=True)` 只排分数，
同分顺序取决于 `token_hits` 这个 dict 的插入序——同一份数据换个 SQL 排序就换一套名次，
而下游 RRF 按名次算分，会把它放大。

**写入时机：挂到导入路径上逐条写**（用户裁决 #4）。1.0 的
`refresh_from_observation` 存在但生产零调用，调用点只有 maintenance 脚本、
`scope_finalize`、`lexical.py` 的懒补建、两个测试。

`refresh_from_observation` 采用**先删后插**而不是 UPSERT：一条 Observation 的字段数会
因为重新富集而**变少**（VLM 第二轮把 objects 收敛掉了），增量更新留不下这种删除。
`test_lexical_refresh_drops_terms_removed_by_re_enrichment` 钉这条。

`rebuild_scope` 只动本 scope（1.0 的 scoped rebuild 会清空整张 FTS 再只补一个 scope），
`test_lexical_rebuild_scope_does_not_wipe_other_scopes` 钉这条。

**Run:**

```bash
.venv/bin/pytest apps/api/tests/unit/test_lexical_rules.py apps/api/tests/integration/test_timeline_repositories.py -q
```

**Expected:**

```
37 passed
```

---

## Task 54: 词法召回 + 元数据召回（两个通道的读取侧）

**依赖：** Task 53。

**Files:**
- Create: `apps/api/src/sentrix/application/retrieval/retrieve_lexical.py`
- Create: `apps/api/src/sentrix/application/retrieval/retrieve_metadata.py`
- Test: `apps/api/tests/integration/test_retrieve_lexical_metadata.py`（15 条）

**`RetrieveLexical` 不做懒补建。** 1.0 的 `_ensure_populated`（`lexical.py:34-48`）
是「生产不写投影」这个缺陷的补丁，不是设计的一部分。保留它等于给「导入没写投影」
留一条永远不会红的退路：真出了这个问题，用户会看到「第一次查慢、之后正常」，
而不是「查不到」。而且**在检索路径上做整表重建也是错的**——一次查询触发 96 万行写入，
没有任何超时保护。

**`RetrieveMetadata` 只在有正向结构化条件时才给召回信号**（1.0 `metadata.py:28-31`，
原文注释保留）。它是唯一一个这样做的通道，因为它同时充当「硬预筛的候选全集」——
把 scope 内每一个 asset 都当 anchor 回传，会让一个纯语义查询的融合结果被插入顺序污染。

**三处修复：**

1. 地点预筛用 `Place` 而不是解析 JSON（1.0 `metadata.py:59-67` 拿到的是 JSON 字符串，
   `geo` 恒 falsy → 放行 → 整个 scope 灌进 metadata 通道）；
2. 「没解析出地点」不等于「地点不匹配」——前者由调用方显式选择放行或剔除；
3. 召回上限落成常量：

```python
# 1.0 把召回上限做成了 SENTRIX_METADATA_RECALL_LIMIT 环境变量（默认 "200"）。
# 2.0 落成常量：这个值没有任何生产改动记录，而 1.0 的配置纪律是「~126 个 env flag
# 的值互相矛盾」（见 2.0 config.py 的开头注释）。做成 env 只会多一个没人动过的开关。
METADATA_RECALL_LIMIT = 200
```

时间窗那一处**反直觉但必须保留**：`captured_at` 未知的行**放行**。1.0 的写法是
`if captured is not None and not (start <= captured < end): continue`。把未知当
「不匹配」会让时间窗查询排除掉一大批本该出现的照片。

**Run:**

```bash
.venv/bin/pytest apps/api/tests/integration/test_retrieve_lexical_metadata.py -q
```

**Expected:**

```
15 passed
```

---

# S4b — 接线与时间线（Task 55–63）

> **这 9 个任务已按同一套方法实施完毕**（每个任务一个 commit，红灯 → 绿灯 →
> `make ci` 全绿），实施记录与逐任务红/绿/门禁见文末的
> 「S4b 实施记录与验证」。计划里保留任务定义与依赖图；**代码块与 Expected 一律以
> 文末那份实测记录为准**——S3 的教训是「错误全部发生在计划生成这一步」，
> 所以这份文档里凡是没跑过的内容都不写成代码块。

## 依赖图

```text
Task 55  事件 / 事实 / 词法仓储接线进 Container（+ 能力登记 + module-map）
   │
   ├── Task 56  导入路径：视觉向量先写（归并顺序硬约束）+ 词法逐条写
   │      │
   │      └── Task 57  批量导入用例（整册）+ 时间线查询用例
   │             │
   │             └── Task 58  事件 / 时间线端点 + OpenAPI 重导
   │                    │
   │                    └── Task 59  契约测试
   │                           │
   │                           └── Task 60  Web 时间线页面
   │                                  │
   │                                  └── Task 61  地点展示（admin + nearest 拼接）
   │                                         │
   │                                         └── Task 62  整册导入端到端验收
   │                                                │
   │                                                └── Task 63  收尾与遗留清单
```

## 任务定义

| Task | 内容 | Files（预计） | 依赖 |
|---|---|---|---|
| **55** | **装配**：把 `AssetPlaceRepository` / `EventRepository` / `FactRepository` / `SqliteLexicalIndex` / `OfflinePlaceResolver` / `AssignEvents` / `RetrieveLexical` / `RetrieveMetadata` 接进 `bootstrap/container.py`。**Container 字段按端口声明**（`AssetPlaceRepository` / `EventRepository` / `FactRepository` / `LexicalIndex` / `LexicalSearch` / `PlaceResolver`），由 `test_container_wiring.py` 强制。同步补 `capability-map.jsonl` 与 `module-map.md` | Modify: `bootstrap/container.py`、`domain/places/ports.py`、`docs/refactor/capability-map.jsonl`、`docs/architecture/module-map.md` | S4a 全部 |
| **56** | **导入路径的归并顺序**：`application/media/import_assets.py` 或新的导入编排里，顺序必须是 `存储 → EXIF/GPS → 地点解析 → 视觉向量 → 观察 → 事件归属 → 词法投影`。视觉向量**先于**事件归属（`s4-deviations.md` §2.3）；词法投影挂在观察写入之后逐条写（用户裁决 #4）。这条顺序用集成测试断言（不是注释） | Modify: `application/media/import_assets.py`；Test: `tests/integration/test_import_pipeline.py` | 55 |
| **57** | **用例**：整册导入 `ImportAlbum`（批量、可恢复、每张最多 3 次模型调用、**串行**不打爆 GPU）+ 时间线查询 `ReadTimeline`（按 scope 分页、按日期分组） | Create: `application/media/import_album.py`、`application/memories/read_timeline.py` | 56 |
| **58** | **端点**：`GET /api/v1/timeline`（按日期分组的事件）、`GET /api/v1/events/{id}`、`POST /api/v1/imports`（建批次）。改路由必须**重导 OpenAPI**（`scripts/export_openapi.py`） | Create: `api/routes/timeline.py`、`api/schemas/timeline.py`；Modify: `main.py`、`packages/contracts/openapi/sentrix-v1.yaml` | 57 |
| **59** | **契约测试**：新端点的状态码 / 字段 / 分页边界；OpenAPI 与实现一致 | Create: `tests/contract/test_timeline_routes.py` | 58 |
| **60** | **Web 时间线页面**：按日期分组的事件流、无限滚动、点开看原图。只用 TypeScript API client | Create: `apps/web/src/features/timeline/*`；Modify: `router.tsx`、`shared/api/client.ts` | 58 |
| **61** | **地点展示**：`Place.display()` 的输出接到事件与照片卡片上；地点筛选走后端（前端不越界计算） | Modify: `features/timeline/*`、`api/schemas/timeline.py` | 60 |
| **62** | **整册导入端到端验收**：真相册、真模型、真 GeoNames 数据。验收标准 = 设计 §8 的 S4 行「导入整个相册，时间线按日期成型」 | Create: `tests/e2e/test_album_timeline.py`、`docs/runbooks/` | 61 |
| **63** | 收尾：能力登记全表复核、`frozen.md` 复核、遗留清单 | Modify: `docs/refactor/*` | 62 |

## S4b 特有的三个坑（执行时注意）

1. **GPU 单卡 3090，当前 5700 MiB / 24576 MiB。** 导入整册时 `analyze_image` 每张最多
   3 次模型调用（首轮 + 补全 + 中文化），**必须串行**。2.0 不管理模型进程生命周期。
2. **VLM 走 8100**（`gemma4-qat-iq3xxs`，multimodal 可用）；**文本嵌入走 8101**
   （bge-m3，1024 维，CPU）。两者都不由 2.0 启停。
3. **`person_score` 在 S5 之前恒为 0**——事件归并少掉权重 0.20 的一路。这不是 bug，
   是依赖顺序；S5 落地后把 `confirmed_people` 接到 `entity_mentions` 即可，
   `AssignEvents` 的接口不用改。

---

## 人工验收（使用者执行）

S4a 的自动验证只到「模块正确、迁移可用、`make ci` 全绿」。真实功能验证需要使用者：

```bash
cd /home/asus/s4verify/repo

# 1. 地点解析：拿一批真 GPS 看解析结果
.venv/bin/python -c "
from pathlib import Path
from sentrix.domain.media.models import GpsPoint
from sentrix.infrastructure.places.pygeocn import PyGeoCNAdminResolver
from sentrix.infrastructure.places.geonames import GeoNamesNearestResolver
from sentrix.infrastructure.places.composite import OfflinePlaceResolver
r = OfflinePlaceResolver(
    admin=PyGeoCNAdminResolver(),
    nearest=GeoNamesNearestResolver(Path('data/geo')),
)
for name, gps in [('萧山', GpsPoint(30.1839, 120.2642)), ('清迈', GpsPoint(18.7883, 98.9853))]:
    place = r.resolve(gps)
    print(name, '->', place.display() if place else None, '|', place.precision if place else '')
"

# 2. 整册导入后的时间线（S4b 完成后）
#    curl -s http://127.0.0.1:9598/api/v1/timeline | python -m json.tool | head -40
```

**通过标准**（对应设计 §8 的 S4 行）：

1. 国内坐标解析到 **区县 + 乡镇/村庄**（`浙江省杭州市萧山区 · 赵州镇大石桥村附近`）；
2. 国外坐标解析到**城市/乡镇**，解析不出时返回 `None`（不是编一个）;
3. 导入整册后，事件按日期成型，同一场活动的照片聚在同一个事件里；
4. 「按地名找图」能召回（词法通道的 `geo` 锚点）；
5. **永远拿不到「赵州桥景区」这类 POI 地标名**——这是数据源的能力边界，不是缺陷。

---

## 已知边界与不做的事

| 边界 | 说明 |
|---|---|
| **拿不到 POI 地标名** | 实测「赵州桥」在 961,888 条中国区记录里 **0 命中**；全国 S 类（spot）仅 13,757 条。国内能力上限是「区县 + 乡镇/村庄」 |
| **PyGeoCN 单次 0.3 秒** | 每个新坐标一次 `geopandas.read_file`。整册导入是 O(照片数) 的固定成本 |
| **GeoNames 数据不在版本控制里** | 35 MB 官方子集放 `data/geo/`，`/data/` 在 `.gitignore` 里。执行阶段要么写进 runbook 让部署方下载，要么改放别处（需裁决） |
| **`person_score` 恒 0** | S5 之前没有 `entity_mentions` |
| **事件不选封面、不建参与者投影** | 依赖 S5 的 `entities` |
| **`_event_pair_score` 不移植** | 见 `s4-deviations.md` §2.2 |
| **不做「国外到村庄」** | 需要 `allCountries`（402 MB），153 实测 3.7 小时 |
| **S4a 不碰任何路由** | 所以 OpenAPI 不用重导，`make ci` 的 `--check` 保持绿 |

---

## 验证记录

### 基线

隔离副本 `/home/asus/s4verify/repo` 在 S4 开始前（起点提交 `28b5ed9`）：

```bash
$ make ci
All checks passed!
Success: no issues found in 78 source files
Required test coverage of 85.0% reached. Total coverage: 97.29%
444 passed, 2 warnings in 75.69s (0:01:15)
ci: 全部通过
```

### 终态 `make ci`（原始输出）

```bash
$ make ci
.venv/bin/ruff check apps/api/src apps/api/tests scripts
All checks passed!
.venv/bin/ruff format --check apps/api/src apps/api/tests scripts
157 files already formatted
.venv/bin/mypy
Success: no issues found in 101 source files

...

TOTAL                                                                 2255     61    97%
Required test coverage of 85.0% reached. Total coverage: 97.29%
599 passed, 2 warnings in 86.77s (0:01:26)
cd apps/web && npx tsc -b
cd apps/web && npx oxlint
Found 0 warnings and 0 errors.
Finished in 24ms on 10 files with 116 rules using 32 threads.
cd apps/web && npx vite build
vite v8.3.0 building client environment for production...
✓ 23 modules transformed.
dist/index.html                   0.45 kB │ gzip:  0.29 kB
dist/assets/index-DZTshfUF.css    2.11 kB │ gzip:  0.83 kB
dist/assets/index-C5Z1jIpf.js   227.83 kB │ gzip: 71.76 kB
✓ built in 70ms
ci: 全部通过
```

### 绿灯层（每个任务自己的测试，在终态树上重跑）

| Task | 测试文件 | 结果 |
|---|---|---|
| 46 | `unit/test_places_models.py` | 15 passed |
| 46 | `unit/test_places_matching.py` | 13 passed |
| 48 | `integration/test_geonames_resolver.py` | 15 passed |
| 50 | `unit/test_event_scoring.py` | 23 passed |
| 51 | `integration/test_assign_events.py` | 9 passed |
| 52 | `unit/test_fact_rules.py` | 14 passed |
| 52/53 | `integration/test_timeline_repositories.py` | 21 passed |
| 53 | `unit/test_lexical_rules.py` | 16 passed |
| 54 | `integration/test_retrieve_lexical_metadata.py` | 15 passed |
| | **合计** | **141 passed** |

原始输出：

```
unit/test_places_models                       15 passed, 2 warnings in 0.01s
unit/test_places_matching                     13 passed, 2 warnings in 0.01s
unit/test_lexical_rules                       16 passed, 2 warnings in 0.01s
unit/test_event_scoring                       23 passed, 2 warnings in 0.01s
unit/test_fact_rules                          14 passed, 2 warnings in 0.01s
integration/test_timeline_repositories        21 passed, 2 warnings in 3.46s
integration/test_assign_events                9 passed, 2 warnings in 2.77s
integration/test_retrieve_lexical_metadata    15 passed, 2 warnings in 2.69s
integration/test_geonames_resolver            15 passed, 2 warnings in 0.15s
```

### 红灯层（只留测试、去掉实现）

**这一处缺口已在补做时关闭。** 隔离副本里按任务重建了 9 个 commit（Task 46–54 各一个），
每个 commit 上都先只落测试文件跑出红灯、再落实现跑绿灯，然后跑一次完整 `make ci`。
逐任务的原始报错与绿灯条数见下面的「逐 commit 红/绿/门禁」表，原始输出在
`/home/asus/s4verify/rebuild-logs/task<N>-{red,green,ci}.txt`。

重建过程中发现**两处原来没有门禁的地方**，一并补上（都写在这一节的表里）：

| 补齐的门禁 | 原来的样子 | 现在 |
|---|---|---|
| `integration/test_pygeocn_resolver.py`（5 条） | Task 47 只有一个 `python -c` 片段，**没有任何测试** | 真 PyGeoCN 反编码：三级齐全、太平洋返回 `None`、confidence 0.90 |
| `integration/test_offline_place_resolver.py`（5 条） | `composite.py`（面 + 点互补）零测试 | 两个槽位各自落位、缺席一方仍出结果、两方皆空返回 `None` |
| `integration/test_timeline_schema.py`（4 条） | Task 49 的门禁是 `test_migrations.py`——它验的是迁移**执行器**，在 0005 写出来之前就是绿的（**恒真门禁**） | 0005 建的五张表、observations 的语义列、FTS5 可查 |

**实际观察到的红灯（Test 期望写错、实现是对的，三处）**：

| 用例 | 期望写成 | 实际（=1.0 行为） | 结论 |
|---|---|---|---|
| `test_weights_are_the_1_0_eight_component_table` | `sum(WEIGHTS) == 1.0` | `1.20` | 期望错——1.0 的权重不归一 |
| `test_time_anchor_tokens["2017-10"]` | `()` | `("2017", "2017-10")` | 期望错——`len(captured) >= 7` 是 1.0 的门槛 |
| `test_strip_admin_suffix_only_strips_once` | `内蒙古` | `内蒙古自治` | 期望错——后缀按序取第一个命中，`区` 先于 `特别行政区` |

三处的共同形状是**实现忠实于 1.0，而我的直觉写错了期望**。这正是「先找 1.0 的实现
再决定搬什么」这条工作方式要防的东西，所以三条都改了期望并在测试里写明了理由。

### 跨任务回归（终态树上重跑全部 S4 测试）

```bash
$ .venv/bin/pytest apps/api/tests -q
599 passed, 2 warnings in 86.77s (0:01:26)
```

S4 的 155 条新用例 + S0–S3 的 444 条基线用例**在同一次运行里全绿**，
没有一条基线用例因为 S4 的改动而失败或需要改断言（`Observation` 加字段、
`ObservationRepository` 加方法、`VectorRepository` 加 `get` 都是**向后兼容的追加**）。

### 零成本算术不变量

| 项 | 基线 | S4a 终态 | 差 |
|---|---:|---:|---:|
| `pytest` 通过条数 | 444 | 599 | **+155** |
| S4 新增测试条数（逐文件求和） | — | 155 | — |
| mypy 检查文件数 | 78 | 101 | **+23** |
| `apps/api/src/sentrix` 下新增 `.py` | — | 23 | — |

**155 == 155**，**23 == 23**。绿灯条数的增量与本任务新增的用例数逐一对齐，
没有「顺手多绿了几条」或「悄悄少绿了几条」。

155 的拆法：**141**（上面「绿灯层」那张表里的 9 个文件，实测 `141 passed`）
+ **14**（补做时补上的三处门禁：`test_pygeocn_resolver` 5 + `test_offline_place_resolver` 5
+ `test_timeline_schema` 4，实测 `14 passed`）。141 + 14 = 155 = 599 − 444。

### 环境事实（规划时实测）

| 项 | 实测值 |
|---|---|
| FTS5 | 可用（`sqlite3` 自带） |
| `pygeo_cn` | 0.1.5；连带 `geopandas 1.1.4` / `pandas 3.0.6` / `shapely 2.1.2` / `pyogrio 0.13.0` |
| `scipy` | 1.17.1（新装，供 `cKDTree`） |
| PyGeoCN 单次耗时 | 约 0.30 秒 |
| 153 → `download.geonames.org` 带宽 | 27–34 KB/s |
| `allCountries.zip` | 402 MB（按实测带宽需约 3.7 小时） |
| `cities15000.zip` | 3,359,366 B → 34,092 个可用点 |
| `CN.zip` | 32,195,246 B |
| 磁盘 | `/home/asus` 可用 1.1 T |

### 生成侧对拍

S4 用了 S3 的四条对策中的三条；**第四条（代码块逐字节 diff）只做了一半**，
如实记录：

| # | 对策 | S4a 执行情况 |
|---|---|---|
| 1 | 阻断归属按 `(task, path, occurrence_index)` | **未做**——本轮没有机器可读的阻断表；任务边界由「哪个测试文件属于哪个 Task」人工划分 |
| 2 | 反向对拍四项 | (a) 块逐字节：**只对本文档里出现的代码块**比对过，不是全量自动 (b) 改动文件集 ⊆ Files 清单：**否**——每个 commit 都带了清单外的文件（见下表） (c) 文档行块：**是**，`behaviour-baseline.md` / `frozen.md` / `s4-deviations.md` 三份都已更新 (d) 每条 `Run` 真跑：**是**，各任务的 `pytest <file>` 与两条 `python -c` 都是实跑输出 |
| 3 | 每个 commit 跑一次完整 `make ci` | **是**（补做时关闭）——Task 46–54 的 9 个 commit 各跑一次，9 次全绿；S4b 的 8 个 commit 同样各跑一次，8 次全绿 |
| 4 | 零成本不变量 | **是**（见上表，155 == 155、23 == 23） |

**改动文件集 ⊆ Files 清单（逐任务核对）：**

**核对结果：不是。** 九个 commit **每一个**都带了 Files 清单外的文件。表由
`git show --name-only <commit>` 与各任务的 Files 清单相减得到：

| Task | 清单外多出的文件 | 为什么 |
|---|---|---|
| 46 | `infrastructure/places/__init__.py`、`infrastructure/places/aliases.py`、`infrastructure/places/data/aliases.json`、`domain/media/models.py`、`apps/api/pyproject.toml` | 前三个是别名表实现，原清单记在 Task 47；`models.py` 的 `GpsPoint.distance_km` 原清单记在 Task 49——但 Task 46 自己的 `test_places_models.py` 覆盖它，不落就绿灯跑不过；`pyproject.toml` 是 `pygeo_cn` 依赖 |
| 47 | `tests/integration/test_pygeocn_resolver.py` | 补做时补的门禁：原 Task 47 只有一段 `python -c`，**没有任何测试** |
| 48 | `tests/integration/test_geonames_resolver.py`、`tests/integration/test_offline_place_resolver.py` | 后者同上——`composite.py`（面 + 点互补）原为零测试 |
| 49 | `infrastructure/persistence/repositories/asset_places.py`、`tests/integration/test_timeline_schema.py`、`tests/integration/test_timeline_repositories.py` | `asset_places.py` 是漏记的实现文件；`test_timeline_schema.py` 是补做补的门禁（原 Task 49 的门禁 `test_migrations.py` 验的是迁移执行器，在 0005 写出来之前就是绿的）；`test_timeline_repositories.py` 按任务分段增长 |
| 50 | （只有下面那两份跨任务的文档） | — |
| 51 | `infrastructure/persistence/repositories/events.py`、`tests/integration/test_timeline_repositories.py` | 事件仓储是 `assign_events` 的协作方，清单漏记 |
| 52 | `domain/memories/ports.py`、`tests/integration/test_timeline_repositories.py` | `FactRepository` 原计划记在 Task 50，但 `ports.py` 同时声明两个端口时 mypy 在 Task 50 上就报 `import-untyped`（实现来自 Task 52 的 `facts.py`），端口只能按任务拆开 |
| 53 | `tests/integration/test_timeline_repositories.py` | 同 49 |
| 54 | （只有下面那两份跨任务的文档） | — |

**两处每个 commit 都有**的清单外文件：`docs/architecture/module-map.md` 与
`docs/refactor/capability-map.jsonl`。`make ci` 的 `check-docs` 要求两者与代码
**同一次提交**就绪（`check_module_map.py` 与 `check_capability_map.py` 各报 0 个问题），
而九个任务的 Files 清单一条都没写它们。表里不逐行重复。

反方向也有一处：Task 47 的清单写「Create `apps/api/src/sentrix/infrastructure/places/__init__.py`」，
它实际随 Task 46 落地（Task 46 的 `infrastructure/places/aliases.py` 需要它）。

**成因**：Files 清单是规划时按**模块边界**画的，commit 边界是执行时按「哪个测试文件
属于哪个 Task」切的，两套切法必然错位——错位处全部是「该任务自己的测试要绿」逼出来的
（清单把它记在下一个 Task 里，而这个 Task 的测试已经在调它）。补做时被 commit 切分逼出来的
四处顺序错位见上一节。

### `docs/architecture/module-map.md` 的两列由脚本重算

21 个新文件 × 2 列依赖 = 42 个格子的手工维护，出错概率接近 100%。
本轮的 `gen_module_map.py` 用与 `scripts/check_module_map.py` **同一套 AST 解析**
（含「包的 `__init__.py` 也是一条边」这条规则）把整张表重写，输出 98 行，
`check_module_map.py` 报 **0 个问题**。

---

## 遗留

1. ~~**S4b（Task 55–63）没有代码。**~~ **已关闭。** S4b 8 个 commit 全部落地并验证，
   见文末「S4b 实施记录与验证」。
2. ~~**红灯层没有逐任务复核。**~~ **已关闭**：Task 46–54 重建为 9 个 commit，每个都有
   红灯 → 绿灯 → `make ci`。同时补上三处原本没有门禁的测试（见「红灯层」一节）。
3. ~~**没有逐 commit 跑 `make ci`。**~~ **已关闭**：S4a 9 次 + S4b 8 次，共 17 次全绿。
4. **`allCountries`（402 MB）没下完**，本轮用 `CN.zip + cities15000.zip` 覆盖设计 §3.3
   声明的能力边界。要「国外到村庄」需要全量。
5. ~~**GeoNames 数据放哪没有定论。**~~ **已关闭**：`docs/runbooks/geonames-data.md`
   写清了两个 zip 的 URL、字节数、放哪（`$SENTRIX_DATA_DIR/geo/`）、怎么验证装好了、
   没有它时降级成什么样，以及许可。数据仍然不进版本控制——**这是决定，不是遗漏**：
   35 MB 第三方数据属于部署产物，不属于源码。
6. ~~**`MAX_NEAREST_RADIUS_M = 50_000` 需要用户确认。**~~ **已裁决：保留**。
   依据是 1.0 算了 `distance_km` 却从不读取，太平洋正中的照片会得到「火奴鲁鲁附近」
   ——那是缺陷不是设计；而 1.0 自己就对 `(0,0)` 做了保护（`parse_gps_place` 返回
   `None`），说明它知道退化坐标要拦、只是没拦干净。**记录为「修 1.0 缺陷」**。
7. **`person_score`（权重 0.20）在 S5 之前恒为 0**——事件归并少掉一整路信号。
8. **别名表那 30 条没有召回数据支撑。** 保留它们是因为它们是通用双语知识**且用户已
   裁决**，不是因为有实测效果（`s4-deviations.md` §6 第二条）。
9. ~~**检索融合没接线**：`RetrieveVisual` 只把 `visual_ann` 单路交给 `fuse()`，
   lexical / metadata 两条通道实现了但没接到搜索端点。~~ **已关闭（Task 64）。**
10. ~~**词法投影的三类 asset 锚点没有写侧**：`ANCHOR_FIELD_TYPES` /
   `time_anchor_tokens` / `file_stem_is_indexable` 三条规则零调用，投影里只有
   observation 自带的字段。~~ **已关闭（Task 65）。** 这一条是 Task 64 的真数据验证
   挖出来的，不是规划阶段看到的。
11. **`entity` / `text_ann` 两路（S5 / S6）**，其中 `text_ann` 是 `episodic` 向量的
   唯一消费者（1.0 `backend/retrieval/text_ann.py:30` 的
   `spaces=("semantic", "episodic")`，两个 space 合成同一条通道）。S4 的
   `_persist_event_summary` 不写 `episodic`，等 S6 一起搬。


---

# S4b 实施记录与验证（Task 55–63）

S4b 在隔离副本 `/home/asus/s4verify/repo` 的分支 `s4-tdd` 上实施，起点是 S4a 的终态
(`e65bffd`)。**每个任务一个 commit，每个 commit 都先只落测试文件跑出红灯、再落实现跑
绿灯，然后跑一次完整 `make ci`。** 原始输出在 `/home/asus/s4verify/rebuild-logs/`。

## 任务与 commit

| Task | 做什么 | 主要文件 | 移植自 |
|---|---|---|---|
| **55** | 把 S4a 的端口接进 `Container`（按端口声明） | `bootstrap/container.py`、`config.py`（+`geo_dir`） | 1.0 的 `MemoryStore` 装配点 |
| **56** | 导入路径的**归并顺序**：存储 → EXIF/GPS → 地点 → 视觉向量 → 观察 → 事件 → 词法 | `application/media/import_assets.py`、`index_place.py`、`api/routes/media.py`（`build_import_assets`） | `pipeline.process` / `prepare_asset` |
| **57a** | 视觉观察器：三段式、每张最多 3 次调用 | `infrastructure/media/vlm_observer.py`、`application/media/observe_asset.py`、`domain/media/ports.py`（+`VlmObserver`/`AssetObserver`） | `model_clients.py:1130-1198` |
| **57b** | 事件总结：确定性摘要 + 一次模型调用 | `application/memories/summarize_event.py`、`domain/memories/{events,ports}.py`、`repositories/events.py` | `pipeline.py:636-691`、`model_clients.py:1394-1433` |
| **57c** | 整册导入 + 时间线读取两个用例 | `application/media/import_album.py`、`application/memories/read_timeline.py` | `pipeline.process_batch`、`app.py` 的 timeline |
| **58+59** | 三个端点 + 契约测试 + **OpenAPI 重导** | `api/routes/timeline.py`、`api/schemas/timeline.py`、`main.py` | 1.0 `app.py` 的 timeline 一族 |
| **60+61** | Web 时间线页面 + 地点展示（筛选走后端） | `apps/web/src/features/timeline/Timeline.tsx`、`App.tsx`、`shared/api/client.ts` | — |
| **62** | 整册导入端到端验收（真照片 / 真 EXIF / 真 GeoNames） | `tests/e2e/test_album_timeline.py` | 设计 §8 的 S4 行 |

## 逐 commit 红/绿/门禁（实测）

| Task | 红灯（原始报错） | 绿灯 | `make ci` | 累计 passed |
|---|---|---|---|---:|
| 55 | `AttributeError: 'Container' object has no attribute 'event_repository'` | 6 passed | 全绿 | 605 |
| 56 | `FAILED test_import_pipeline.py::test_the_merge_order_is_storage_gps_place_visual_observation_events_lexical` | 7 passed | 全绿 | 612 |
| 57a | `ImportError: cannot import name 'OpenAIVlmObserver'` | 15 passed | 全绿 | 627 |
| 57b | `ImportError: cannot import name 'SummarizeEvent'` | 18 passed | 全绿 | 645 |
| 57c | `ImportError: cannot import name 'ImportAlbum'`（2 个模块） | 14 passed | 全绿 | 659 |
| 58+59 | `FAILED test_timeline_routes.py::test_timeline_is_empty_before_any_import`（404） | 10 passed | 全绿 | 669 |
| 60+61 | `Timeline.tsx(109,36): error TS7006: Parameter 'photo' implicitly has an 'any' type` | `tsc -b` 静默通过 | 全绿 | 669 |
| 62 | **验收任务**，见下 | 10 passed | 全绿 | **679** |

S4a 的补齐（补做）逐 commit 证据：

| Task | 红灯（原始报错） | 绿灯 | `make ci` | 累计 passed |
|---|---|---|---|---:|
| 46 | `ImportError: cannot import name 'sentrix.domain.places'`（2 个模块） | 28 passed | 全绿 | 472 |
| 47 | `ImportError: cannot import name 'PyGeoCNAdminResolver'` | 5 passed | 全绿 | 477 |
| 48 | `ImportError`（2 个模块） | 20 passed | 全绿 | 497 |
| 49 | `ImportError: cannot import name 'SqliteAssetPlaceRepository'` + schema 断言 | 8 passed | 全绿 | 505 |
| 50 | `ImportError: cannot import name 'sentrix.domain.memories.events'` | 23 passed | 全绿 | 528 |
| 51 | `ImportError`（2 个模块） | 18 passed | 全绿 | 546 |
| 52 | `ImportError`（2 个模块） | 16 passed | 全绿 | 562 |
| 53 | `ImportError`（2 个模块） | 22 passed | 全绿 | 584 |
| 54 | `ImportError: cannot import name 'sentrix.application.retrieval.retrieve_lexical'` | 15 passed | 全绿 | 599 |

**零成本不变量**：444（补齐前基线）→ 599（S4a 终态）→ 679（S4b 终态）。
S4a 的 +155 与逐任务用例数逐一对齐；S4b 的 +80（6+7+15+18+14+10+0+10）同样对齐。
没有「顺手多绿了几条」。

## 补做时被 commit 切分逼出来的四处顺序错位

这不是重构，是**原来那份连续实现掩盖了的问题**——每个都在「只落测试」的那一步立刻显形：

| 现象 | 根因 | 处理 |
|---|---|---|
| Task 46 绿灯跑不过 | `test_places_models.py` 覆盖 `GpsPoint.distance_km`，而实现挂在 Task 49 的 Files 清单里 | 实现随它的测试挪到 Task 46 |
| Task 50 `mypy` 报 `import-untyped` | `domain/memories/ports.py` 同时声明 `EventRepository` 与 `FactRepository`，而 `Fact` 来自 Task 52 的 `facts.py` | 端口按任务拆：50 只落 `EventRepository`，`FactRepository` 随 52 |
| Task 56 `ruff` 报 `F821` | `build_import_assets` 引用了 Task 57a 的 `ObserveAsset` | 观察器的接线随 57a（`observer=None` → `ObserveAsset(...)`） |
| Task 56 `check-docs` 报 openapi 不一致 | 计划只写了「Task 58 改路由要重导」，但 Task 56 给 `ImportResponse` 加了两个字段——**改响应模型同样是改契约** | Task 56 的 `after` 里加一次 `export_openapi.py` |


## 每个 commit 的实际改动文件集（S4b）

「改动文件集 ⊆ Files 清单」这条对拍在 S4b 上是**反向**做的：表由 `git show --stat` 生成，再与各任务的 Files 清单核对。

| Task | commit | 改动文件 |
|---|---|---|
| 55 | `2acb694` | `apps/api/src/sentrix/bootstrap/container.py`<br>`apps/api/src/sentrix/config.py`<br>`apps/api/tests/integration/test_container_timeline.py` |
| 56 | `eb03417` | `apps/api/src/sentrix/api/routes/media.py`<br>`apps/api/src/sentrix/api/schemas/media.py`<br>`apps/api/src/sentrix/application/media/import_assets.py`<br>`apps/api/src/sentrix/application/media/index_place.py`<br>`apps/api/src/sentrix/domain/media/ports.py`<br>`apps/api/tests/integration/test_import_pipeline.py` |
| 57a | `37aa238` | `apps/api/src/sentrix/api/routes/media.py`<br>`apps/api/src/sentrix/application/media/observe_asset.py`<br>`apps/api/src/sentrix/bootstrap/container.py`<br>`apps/api/src/sentrix/config.py`<br>`apps/api/src/sentrix/domain/media/ports.py`<br>`apps/api/src/sentrix/infrastructure/media/vlm_observer.py`<br>`apps/api/tests/conftest.py`<br>`apps/api/tests/unit/test_vlm_observer.py` |
| 57b | `3c7142d` | `apps/api/src/sentrix/application/memories/summarize_event.py`<br>`apps/api/src/sentrix/domain/memories/events.py`<br>`apps/api/src/sentrix/domain/memories/ports.py`<br>`apps/api/src/sentrix/infrastructure/persistence/repositories/events.py`<br>`apps/api/tests/unit/test_event_summary.py` |
| 57c | `5de2f64` | `apps/api/src/sentrix/application/media/import_album.py`<br>`apps/api/src/sentrix/application/memories/read_timeline.py`<br>`apps/api/tests/integration/test_import_album.py`<br>`apps/api/tests/integration/test_read_timeline.py` |
| 58+59 | `a22d4d1` | `apps/api/src/sentrix/api/routes/timeline.py`<br>`apps/api/src/sentrix/api/schemas/timeline.py`<br>`apps/api/src/sentrix/application/memories/read_timeline.py`<br>`apps/api/src/sentrix/main.py`<br>`apps/api/tests/contract/test_timeline_routes.py` |
| 60+61 | `bf641f1` | `apps/web/src/App.tsx`<br>`apps/web/src/features/timeline/Timeline.tsx`<br>`apps/web/src/shared/api/client.ts` |
| 62 | `29eb6c1` | `apps/api/tests/e2e/test_album_timeline.py` |
| 63 | `d40b984` | `apps/web/src/features/timeline/Timeline.tsx`<br>`docs/refactor/2026-09-22-s4-implementation-plan.md`<br>`docs/refactor/s4-deviations.md`<br>`docs/runbooks/geonames-data.md` |

## Task 62 的红灯

Task 62 是**验收任务**：它没有自己的实现，验的是 55–61 拼起来之后整条链的形状，
所以「先落测试」那一步它是绿的。它的红灯是**实测到的**，来自真数据：

```
>       assert miss["days"] == []
E       AssertionError: assert [{'date': '20...'待判断', ...}]}] == []
E       Left contains one more item: {'date': '2018-03-31', 'events': [{'activity': '拍照',
E         'admin_district': '', 'confidence': 0.65, 'event_type': '待判断', ...}]}
FAILED apps/api/tests/e2e/test_album_timeline.py::test_place_filter_returns_only_that_place
```

第一版用例拿「清迈」当**反例**（「相册里不会有这个地方」），导入整册后它被真的解析成了
「清迈附近」——2018-03-31 那两张的外国坐标是 `(18.77, 98.97)`。**是我的期望错了，
实现是对的**，和 S4a 那三处红灯是同一个形状。反例换成相册里确定没有的「悉尼」。

## Task 63 收尾时清掉的一处新警告

Task 60/61 的第一版 `Timeline.tsx` 让 `make ci` 的 `web-check` 多出 **1 条 oxlint 警告**
（`react(set-state-in-effect)`：在 effect 里同步 `setState` 会级联渲染）。门禁只拦 error，
所以它是绿的，但那是**新引入的**噪声。收尾时按这条规则自己给的解法改成推导状态
（`loading = loaded === null || loaded.key !== key`），`oxlint` 回到
**0 warnings and 0 errors**。记在这里是因为「门禁是绿的」与「没有引入新问题」是两件事。

## 真数据在 S4b 抓到的东西

| 发现 | 证据 |
|---|---|
| **相册里真的有国外坐标** | `2018-03-31 → 清迈附近`、`2018-04-01 → Hang Dong附近`。合成夹具里没有国外点，这条只有真相册能提；它同时也是「国外走 GeoNames 的点、不占 admin 槽位」的实测证据（`admin_district == ""`） |
| **ADM4 记录仍可能只有罗马字名** | `geonameid 7577037` 的 `alternatenames` 列**为空**，所以 `Nanlvgu` 原样展示成 `河北省邯郸市永年区 · Nanlvgu西大屯附近`。同类还有 `Wusong Qu`（上海）、`Xisu`（邯郸）、`Hang Dong`。这是**数据缺失**，不是实现缺陷：`_display_name` 只在备选名里找得到汉字时才替换 |
| **13 个拍摄日期全部成型** | 53 张真照片、13 天，导入后时间线的日期集合与 EXIF 的日期集合逐个相等 |
| **两个真簇各自折成一个事件** | `2017-11-05` 的 16 张、`2023-08-06` 的 5 张，各自一个事件 |

## 与用户任务清单的差异（如实说明）

用户给的任务表（55 `domain/memories/events.py` 八分量 / 56 `cluster_events` /
59 词法索引 / 60 `retrieve_lexical` / 61 `retrieve_metadata`）里的这些项，
**在 S4a 就已经落地**（分别是 Task 50 / 51 / 53 / 54），所以 S4b 没有重复实现它们；
用户表里的 57（`fallback_projection`）与 58（`summarize_event`）落在本轮的 **57b**，
62（检索融合接线）里「三路进 `fuse()`」这一项**当时没有做**——**已由 S4c 的 Task 64/65
补齐**，见文末「S4c 实施记录与验证」。
本文件里的 S4b 任务编号沿用本计划原先的定义。

---

# S4c — 检索融合接线（Task 64–65）

S4b 的终态（`d40b984`，679 passed）留下两处**代码在、没接线**的缺口，这一节把它们补齐。
两处都是同一形状：实现（或纯规则）已经写完并有单测，但**没有任何生产调用方**。

| # | 缺口 | 谁发现的 | 关掉它的任务 |
|---|---|---|---|
| 1 | `RetrieveVisual` 只把 `visual_ann` 单路交给 `fuse()`，lexical / metadata 两条通道实现了但没接到搜索端点 | S4b 实施时自报 | **Task 64** |
| 2 | 词法投影只写 observation 自带的字段，1.0 的 `geo` / `time` / `file` 三类 asset 锚点从没写过（`ANCHOR_FIELD_TYPES` / `time_anchor_tokens` / `file_stem_is_indexable` 零调用） | **Task 64 的真数据验证** | **Task 65** |

第 2 条是拿真相册跑出来的，不是读代码读出来的：导入 53 张真照片后按
`observation_search_terms.field_type` 分组，只有 `place` / `object` / `caption` /
`activity` 四类，`2017` 与 `2017-11-05` 两个查询在 FTS 上都命中 0 张——「按日期找图」这条
S4 的目标能力（本文件开头「Goal」一行）在词法通道上根本不存在。

## Task 64: 三路进 `fuse()`（检索融合接线）

**依赖：** S4b 的 Task 55（`RetrieveLexical` / `RetrieveMetadata` 已进 Container）。

**Files:**
- Create: `apps/api/src/sentrix/application/retrieval/retrieve_evidence.py`
- Create: `apps/api/tests/integration/test_retrieve_evidence.py`（10 条）
- Modify: `apps/api/src/sentrix/api/routes/search.py`（`GET /api/v1/search` 改走编排）
- Modify: `apps/api/src/sentrix/api/routes/assistant.py`（`search_memories` 同一个编排）
- Modify: `apps/api/src/sentrix/application/assistant/tools.py`（协作方类型）
- Modify: `apps/api/tests/contract/test_search_routes.py`（+2）、`apps/api/tests/integration/test_assistant_toolbox.py`
- Modify: `docs/architecture/module-map.md`、`docs/refactor/capability-map.jsonl`

**接线逐条照搬 1.0**（`backend/evidence_retrieval.py:198-243`、
`backend/retrieval/__init__.py:65-85`）：

- 通道集合 = 被启用的 primary 通道，构建顺序
  `metadata → entity → lexical → visual_ann → text_ann → adjacency`；
- 召回循环跳过 `kind == "expander"`（`:198-201`），只有 primary 进 `channel_hits`；
- 融合只有一次调用，权重表 `DEFAULT_CHANNEL_WEIGHTS`（`:241-243` →
  `retrieval/ranking.py:101-110` → `fusion.fuse`）；
- **缺通道的形态是「不出现」**：1.0 里没启用的通道压根不在 retriever 列表里，
  `channel_hits` 里没有那个键。2.0 的 entity / text_ann 到 S5 / S6 才有类，所以这里
  连调用都没有——不是「调了返回空」；
- **`adjacency` 不接**：`configs/retrieval/defaults.json:10` 里它是 `false`，实例都不建；
  即使建了，`kind="expander"` 也让它不进并行召回——Kernel 拿 primary 融合结果的
  exact/strong 当种子再 `expand`（`:246-273`），扩展结果 append 在主结果之后。

**通道顺序是语义**：`fusion.fuse` 在候选第一次出现时按那个通道定 `evidence_class`
（`domain/retrieval/fusion.py:76-84`），metadata 是 anchor 通道，不是第一个就会丢掉
`+1.0` 的锚点加成。1.0 的构建顺序就是 metadata 在前。

```python
METADATA_CHANNEL = "metadata"
LEXICAL_CHANNEL = "lexical"
VISUAL_CHANNEL = "visual_ann"

# 每条通道按调用方上限过取一层再融合。1.0 把理由写在了注释里
# （``evidence_retrieval.py:171-176``）："Ask each enabled channel for the complete
# authorized scope so a relevant asset cannot disappear merely because another channel
# ranked it below an arbitrary head."——它的做法是把每条通道的 ``recall_limit`` 抬到
# ``max(requested_limit, authorized_count)``，即整个已授权 scope（``:176-181``）。
# 2.0 不整册回灌（一次查询把全 scope 拉进内存），改用与视觉 ANN 同一个过取倍数
# （``retrieve_visual.py`` 的 ``OVERSAMPLE_FACTOR = 4``）：**融合前不裁到交付上限**，
# 融合之后才按 limit 交付。
RECALL_OVERSAMPLE_FACTOR = 4
```

```python
        # 这个 dict 的插入顺序必须与 1.0 的构建顺序一致：metadata 最先，锚点加成才会
        # 落在同时被 metadata 与语义通道命中的候选上。
        channel_hits: dict[str, Sequence[CandidateHit]] = {
            METADATA_CHANNEL: self._metadata.execute(
                scope_id=request.scope_id,
                time_bounds=request.time_bounds,
                media_types=request.media_types,
                negated_media=request.negated_media,
                place=request.place,
            ),
            LEXICAL_CHANNEL: self._lexical.execute(
                whole_query=text, scope_id=request.scope_id, limit=recall_limit
            ),
            VISUAL_CHANNEL: _visual_candidates(visual),
        }
        fused = fuse(channel_hits, channel_weights=DEFAULT_CHANNEL_WEIGHTS)
```

**两个入口共用一次融合。** 1.0 的 HTTP 侧与 `agent_runtime/tools.py` 的
`search_memories` 都走同一个 `EvidenceRetrievalKernel`；2.0 原来两处各 new 一个
`RetrieveVisual`，于是「网页能搜到、助手搜不到」会成为默认行为。现在两个入口都调
`api/routes/search.py` 的 `build_retrieve_evidence(container)`。

**Run:**

```bash
.venv/bin/pytest apps/api/tests/integration/test_retrieve_evidence.py apps/api/tests/integration/test_assistant_toolbox.py apps/api/tests/contract/test_search_routes.py
```

**Expected:**

```
37 passed
```

**反向验证**（`test_without_the_lexical_channel_that_photo_is_not_reachable`）：同一个
查询词走单通道用例时首位是视觉冠军，走编排时被词法召回的那张顶到首位——两条断言同时
成立，「接上了」与「没接」才在行为上可区分。红灯是**行为红**，不是导入红：

```
>       assert items[0]["asset"]["file_name"] == "海边.jpg"
E       AssertionError: assert '雪山.jpg' == '海边.jpg'
FAILED apps/api/tests/contract/test_search_routes.py::test_the_lexical_channel_is_fused_into_the_endpoint
```

**顺带钉住的标准条件**：`test_a_candidate_outside_every_channels_head_is_still_ranked_by_the_fusion`
用 `limit=1` 证明「交付上限只裁融合之后的结果，不裁每条通道的召回」——B 在两条通道里
都是第二名，按 `limit` 裁通道召回时交付的是 C，不裁时交付的是 B。

## Task 65: 词法投影的 `geo` / `time` / `file` 锚点（写侧接线）

**依赖：** Task 64（它挖出了这一条）。

**Files:**
- Modify: `apps/api/src/sentrix/domain/retrieval/lexical.py`（`AnchorTerm` + `anchor_terms`）
- Modify: `apps/api/src/sentrix/infrastructure/persistence/repositories/search_terms.py`（写侧 + 两个协作方）
- Modify: `apps/api/src/sentrix/bootstrap/container.py`（装配）
- Modify: `apps/api/tests/unit/test_lexical_rules.py`（+4）、`apps/api/tests/integration/test_timeline_repositories.py`（+2）
- Create: `apps/api/tests/e2e/test_search_channels.py`（3 条，真相册 / 真 EXIF / 真 GeoNames）
- Modify: `docs/architecture/module-map.md`、`docs/refactor/capability-map.jsonl`

**1.0 的做法**：`RetrievalIndex.refresh_from_observation` 在写完 observation 字段之后，
再调 `_asset_anchor_rows`（`backend/retrieval_indexes.py:198-222`、`:225-245`）插三列：

| 列 | 值 | 来源 |
|---|---|---|
| `geo` | 地名各级（1.0 是 `label` / `province` / `city` / `district`） | asset 的反地理编码 |
| `time` | `YYYY` / `YYYY-MM` / `YYYY-MM-DD`（整串短于 7 字符不建） | `assets.captured_at` |
| `file` | 文件名去扩展名（`event_event_*` / `kf_*` 除外） | `assets.file_name` |

三条 `source_type` 分别是 `asset_geo` / `asset_time` / `asset_file`，confidence 恒 0.9。

```python
    def _anchor_terms(self, observation: Observation) -> tuple[AnchorTerm, ...]:
        """1.0 ``retrieval_indexes.py:225-245``：从 asset 的 file_name / captured_at 与
        解析出的地点生成 geo / time / file 三列锚点。

        调用时点与 1.0 相同（写在 ``refresh_from_observation`` 里），而导入路径的
        「地点解析 → 观察 → 词法投影」顺序保证这里读得到 ``asset_places`` 那一行
        （``import_assets.py`` 的模块注释把顺序写成了硬约束）。
        """
        asset = self._assets.get(observation.asset_id)
        if asset is None:
            return ()
        place = self._places.get(observation.asset_id)
        return anchor_terms(
            file_name=asset.file_name,
            captured_at=asset.captured_at.isoformat() if asset.captured_at else "",
            place_text=place.searchable_text() if place is not None else "",
        )
```

`geo` 取 `Place.searchable_text()` 的每一段，**不含 GPS 坐标**——1.0 的 `label` 在
PyGeoCN 那条路上可能退化成坐标串（它自己用 `DISPLAY_COORDINATE_RE` 挡过这件事），
2.0 直接不给坐标进入这个词的机会。

**Run:**

```bash
.venv/bin/pytest apps/api/tests/unit/test_lexical_rules.py apps/api/tests/integration/test_timeline_repositories.py
.venv/bin/pytest apps/api/tests/e2e/test_search_channels.py
```

**Expected:**

```
43 passed
3 passed
```

**红灯**（把 `_term_rows` 里的锚点循环去掉，其余不动）：

```
E       KeyError: 'asset_1'
FAILED apps/api/tests/integration/test_timeline_repositories.py::test_lexical_refresh_writes_the_asset_anchors
FAILED apps/api/tests/integration/test_timeline_repositories.py::test_without_a_resolved_place_there_is_no_geo_anchor
```

**真数据（53 张真相册，实测值）**：

| 查询 | 结果 | 判据 |
|---|---|---|
| `夷陵区` | 16 张，恰好是 `captured_at` 为 `2017-11-05` 的那 16 张 | 与 `GET /api/v1/assets` 的 EXIF 日期集合逐元素相等 |
| `清迈` | 2 张，恰好是 `2018-03-31` 那 2 张 | 国外坐标走 GeoNames 最近邻点（`nearest` 槽，边界数据里没有它） |
| `悉尼` | 0 张 | 反向：既不是观察文本，也不是任何一张的地点或文件名 |

导入后的投影行数从 212（只有 observation 字段）变成 669：`geo` 245 + `time` 159 + `file` 53
（53 张照片 × 3 段日期 = 159，一对一 = 53）。

## S4c 验证记录

| Task | 红灯（原始报错） | 绿灯 | `make ci` | 累计 passed |
|---|---|---|---|---:|
| 64 | `assert '雪山.jpg' == '海边.jpg'`（契约层，行为红）+ `ModuleNotFoundError: sentrix.application.retrieval.retrieve_evidence` | 37 passed | 全绿 | **691** |
| 65 | `KeyError: 'asset_1'`（2 条集成） | 43 passed + 3 passed | 全绿 | **700** |

**零成本不变量**：679（S4b 终态）→ 691（+12：10 条编排用例 + 2 条契约）→ 700
（+9：4 条锚点单测 + 2 条投影集成 + 3 条真数据端到端）。逐任务用例数与差值逐一对齐，
没有「顺手多绿了几条」。

**每个 commit 的实际改动文件集**（由 `git show --stat` 生成，再与 Files 清单核对）：

| Task | commit | 改动文件 |
|---|---|---|
| 64 | `f1d251c` | `api/routes/assistant.py`<br>`api/routes/search.py`<br>`application/assistant/tools.py`<br>`application/retrieval/retrieve_evidence.py`<br>`tests/contract/test_search_routes.py`<br>`tests/integration/test_assistant_toolbox.py`<br>`tests/integration/test_retrieve_evidence.py`<br>`docs/architecture/module-map.md`<br>`docs/refactor/capability-map.jsonl` |
| 65 | `bf37794` | `bootstrap/container.py`<br>`domain/retrieval/lexical.py`<br>`infrastructure/persistence/repositories/search_terms.py`<br>`tests/e2e/test_search_channels.py`<br>`tests/integration/test_assistant_toolbox.py`<br>`tests/integration/test_retrieve_evidence.py`<br>`tests/integration/test_retrieve_lexical_metadata.py`<br>`tests/integration/test_timeline_repositories.py`<br>`tests/unit/test_lexical_rules.py`<br>`docs/architecture/module-map.md`<br>`docs/refactor/capability-map.jsonl` |

**`docs/architecture/module-map.md` 的两列由脚本重算**：用与 `check_module_map.py`
同一套 AST 解析（含「包的 `__init__.py` 也是一条边」）重写，`check_module_map.py`
报 **0 个问题**；两列之外的行一字未动（`diff` 只有这三行）。

## 这一节没有做的事

- **metadata 通道的正向条件没有 HTTP 来源。** S4 的 `/api/v1/search` 只有
  `q` / `scope_id` / `limit`，而 `RetrieveMetadata` 在没有正向结构化条件时按设计返回空
  （1.0 `metadata.py:28-31`）。所以它在端点上**恒静默**——这是 1.0 对纯内容词查询的
  行为，不是「没接上」。`EvidenceRequest` 已经留好 `time_bounds` / `media_types` /
  `negated_media` / `place` 四格，S6 的硬条件（时间 / 人物 / 地点）与查询解析会来填。
- **`entity` / `text_ann` 两路**（S5 / S6）。缺它们的行为与 1.0 一致：不在
  `channel_hits` 里，也不报空。
- **`adjacency`**（S6）：要种子分级才谈得上扩展。
- **`episodic` 向量**：1.0 的唯一消费者是 `text_ann`（两个 space 合成一条通道），
  随 S6 一起搬；S4 照旧不写（`summarize_event.py` 的模块注释已按 1.0 实证改写）。

---

# S4d 实施记录与验证（Task 66–67）

S4c 补的是 S4b 的两处遗留（检索融合没接线、词法投影的 asset 锚点没有写侧）。这一轮补的
是**同类的另外两处**：`SummarizeEvent`（Task 57b）与事实写入的规则/仓储（Task 52）
建好之后都**没有任何生产调用方**。S4c 抓住并修了词法锚点那三条纯规则，这两块漏了。

诊断是从真数据来的，不是读代码猜的：整册导入（`POST /api/v1/imports`）53 张真照片之后

```
assets 53 / observations 53 / events 17 / observation_search_terms 969 / facts 0
有描述的 observation: 46          ← 语义富化跑了
事件的 title       待总结事件      ← 占位符（1.0 db.py:2310-2325 的字面量）
事件的 event_type  待判断          ← 占位符
```

`summary` / `activity` 有内容，是**事件归并**顺手拼的（`events.py` 的 folded 值），
不是总结器写的。所以「事件看起来有内容」正是这个缺陷难被发现的原因，也是原来那条
e2e 只断言 `events > 0` 就够绿的原因（第 51 条）。

## Task 66: 批末事件总结接线（1.0 的时机）

**1.0 的时机（实测行号）**

| 位置 | 行为 |
|---|---|
| `app.py:616-660` `process_ingest_batch` | 逐张走 `enrich_fast_image` → `_process_image_stages` → `commit_semantic_image(summarize_event=False)` ——**逐张不总结** |
| `app.py:647-653` | 全部 `complete` 后：`claim_ingest_batch_summary(batch_id)` → `batch_event_ids(batch_id)` → `ThreadPoolExecutor` 里逐个 `_summarize_event_worker(event_id)` |
| `app.py:577-581` `_process_ingest_asset_group(finalize_batch=True)` | 同一段收尾（增量消费的那条路径） |
| `pipeline.py:700-717` `finalize_ingest_batch` | 同样按 `batch_event_ids` 逐个总结 + `finalize_ingest_scope` |
| `pipeline.py:355-356` | **另一条入口**（`process()`，`summarize_event=True` 默认值）逐张总结；整册导入不走它 |

一句话：**批末、按事件、只总结这一批碰过的事件**。

**2.0 的落点**：`application/media/import_album.py` 的 `ImportAlbum.execute` 在逐张循环里
保序去重收集 `result.event_id`，循环结束后 `_summarize_events(event_ids)` 逐个调用；
装配点在 `api/routes/timeline.py` 的 `_summarize_event`（模型取 `container.chat_model`，
**不是** `None`）。`ImportBatchResponse` 增 `summarized`。

**Files:** `application/media/import_album.py`、`api/routes/timeline.py`、
`api/schemas/timeline.py`、`tests/integration/test_import_album.py`、
`tests/contract/test_media_routes.py`、`tests/e2e/test_album_timeline.py`、`tests/conftest.py`、
`packages/contracts/openapi/sentrix-v1.yaml`、`docs/architecture/module-map.md`、
`docs/refactor/capability-map.jsonl`

**接线之后真跑一次，抓到了第三个缺陷（同一个能力上的）**：`/tmp/s4-api.log` 里 17 条
warning 全是一句——

```
事件总结的模型调用失败，改用确定性摘要: 对话必须以一条 system 消息开头（严格 chat template 的硬要求）
```

这句话来自 **2.0 自己的客户端**（`chat_client.py:98`），不是 8100。`SummarizeEvent`
照 1.0 的 `GammaClient.summarize_event`（`model_clients.py:1394-1433`）把提示词与证据
拼成**一条 user 消息**发出去；1.0 的 `chat(prompt)` 确实只发一条 user 消息
（`model_clients.py:314-315`），宽松模板容忍，而 2.0 的 `ChatModel` 端口不接受那个形状。
**所以 S4b 的 `SummarizeEvent` 从来没有真正调用过模型**——每一次都在本地抛
`ChatModelError`，被 `except` 吞掉退回确定性摘要。它看起来像「模型端点不可用时的降级」，
实际是「根本走不到」。

改法：`build_prompt` 拆成 `build_system_prompt()`（进 `messages[0]`）与
`build_evidence()`（证据 JSON，进 user 消息）；**单测的假件补上同一对断言**
（原来的 `FakeChatModel` 根本不管 messages 的形状，这正是缺陷能在 18 条绿灯下藏住的原因）。

## Task 67: 事实写入接线（1.0 `process()` 的观察级调用点）

**1.0 的时机**：`pipeline.py:346-350`——`merge_observation_into_event` **之后**逐条
`maintain_fact`，门禁是 `all(fact.get(k) for k in ("subject","predicate","object")) and
store.is_confirmed_person_name(subject, scope_id)`。`_commit_semantic_image`
（`pipeline.py:570-635`）整段不调——**两条路径不一致是用户裁决 #3 要修的**。

**2.0 的落点**：新建 `application/memories/maintain_facts.py` 的 `MaintainFacts`；
`ImportAssets` 多一个可选协作者 `fact_maintainer`，调用点在 `_assign_event` 与
`_project_lexical` 之间（导入顺序因此多一段「事实写入」）；返回值进 `ImportResult.fact_ids`。

**Files:** `application/memories/maintain_facts.py`（新）、`application/media/import_assets.py`、
`api/routes/media.py`、`tests/integration/test_maintain_facts.py`（新）、
`tests/e2e/test_album_timeline.py`、`docs/architecture/module-map.md`、
`docs/refactor/capability-map.jsonl`

**这一轮没有修的东西（需用户裁决）**：S4 的装配下事实表仍然为空，两个原因彼此独立——
候选事实没有来源（第 30 条）与门禁依赖 S5 的身份层。详见 `s4-deviations.md` §9 第 50 条。

## S4d 验证记录

| Task | 红灯（原始报错） | 绿灯 | `make ci` | 累计 passed |
|---|---|---|---|---:|
| 66 | `assert body["summarized"] > 0` → `E assert 0 > 0`（拆掉 `routes/timeline.py` 的 `summarize_event=`，e2e 实测 13 个事件 title 全为 `待总结事件`） | 39 passed（`test_import_album.py` 10 条 + `test_event_summary.py` 19 条 + `test_media_routes.py` 10 条） | 全绿（`CI_EXIT=0`） | **716** |
| 67 | `assert [] == ['obs_4b94f32c5ac1']`（拆掉 `routes/media.py` 的 `fact_maintainer=`）与 `assert ['events'] == ['events', 'facts:obs_...']`（拆掉 `ImportAssets` 的调用） | 7 passed（`test_maintain_facts.py`） | 全绿（`CI_EXIT=0`） | **716**（s4c 终态 700 + 16：4 条批末总结 + 1 条装配点 + 7 条事实写入 + 3 条 e2e 内容 + 1 条消息形状） |

**反向验证逐条做过**（四条接线各拆一次，确认对应断言必红）：

```
拆 routes/timeline.py 的 summarize_event=        -> assert body["summarized"] > 0   E assert 0 > 0
                                                    title 全为 ['待总结事件']，event_type 全为 ['待判断']
拆 routes/media.py 的 fact_maintainer=           -> assert [item.id for item in seen] == [...]   E assert [] == ['obs_4b94f32c5ac1']
拆 import_assets.py 的 _maintain_facts 调用       -> assert recorder.order == ["events", ...]    E assert ['events'] == ['events', 'facts:obs_...']
拆 import_album.py 的 _summarize_events(event_ids) -> assert summarizer.calls == ["evt_1"]        E assert [] == ['evt_1']
把 summarize_event.py 的模型调用改回单条 user 消息 -> assert '浙江省杭州市萧山区拍照' == '萧山区家庭活动'（模型那条路整体失效）
                                                    assert [] == [<ChatRole.SYSTEM…>, <ChatRole.USER…>]
```

## 真数据验收（9598，重新导入 53 张）

`data/` 清空（保留 `data/geo/`）后重启 9598 并重跑整册导入：

```
POST http://127.0.0.1:9598/api/v1/imports
{"scope_id":"album1","directory":"/home/asus/Github/Sentrix-Home-Web/data/album3-v2-source/photos"}
```

```json
{"scope_id":"album1","directory":"/home/asus/Github/Sentrix-Home-Web/data/album3-v2-source/photos",
 "imported":53,"deduplicated":0,"events":53,"lexical_terms":976,"summarized":17,"failures":[]}
```

（`events` 是「有 event_id 的照片数」，与 1.0 同义；`summarized` 是**去重后的事件数**。
53 张折成 17 个事件，所以 17 < 53 是折叠，不是漏总结。）

导入后的库：

```
assets 53 / observations 53 / events 17 / event_observations 53
observation_search_terms 976 / facts 0      有描述的 observation 46
title 是占位值的行数 0       event_type 是占位值的行数 0       revision>1 的事件数 17
```

事件的实际内容（17 个事件、17 个各不相同的标题、11 种 `event_type`）：

```
山景观赏与水域景观        户外活动   在2017年11月5日，在湖北省宜昌市夷陵区邓村白狮系附近，观察到了一系列户外活动…
公交车在道路上行驶        交通活动   在湖北省宜昌市夷陵区邓村白狮系附近，公交车在道路上行驶。
工业区域湖边散步与桥梁结构观察 户外活动
河岸及周边景观观察        户外观景   在河边观景台和道路上散步，欣赏日落景色
在室外游乐设施和公园活动   户外娱乐
室内行走与休息            日常活动   在大型室内公共建筑的走廊中，人们穿着深色短袖衬衫，进行行走和休息…
夜间户外活动与集会        户外集会   在2018年4月1日晚间，在Hang Dong附近户外广场上，一群人聚集…
室内玩耍与户外活动        儿童活动
户外泥塑公园玩耍          户外活动   在户外泥塑公园，孩子们穿着黄色T恤和白色短裤…
烘焙原料展示              日常活动
制作粉状兔子食物          食物制作   在厨房台面上制作了粉状兔子形状的食物，并使用绿色磨砂抹布。
户外果园活动              户外活动
室内聚会与活动            人在室内合影
在金汇花园附近活动        日常活动   在上海市闵行区Wusong Qu金汇花园附近，于2022年7月16日14:16:21至14:16:25…
夜间商业街景              商业活动   2022年7月31日19:47:46至19:48:22，在河北省石家庄市桥西区南长南小街社区附近…
夜间户外家庭聚会与烟花活动 家庭户外活动
夜间户外玩耍与休闲场景     户外活动
```

摘要里出现**时间范围与语义地点**是模型那条路的标志（提示词要求），说明第 52 条修好之后
17 个事件全部走通模型，不是退回 `fallback_projection`；`/tmp/s4-api.log` 里两条降级
warning（模型调用失败 / 模型回复不可用）都是 **0** 次。

**`facts` 仍然是 0**——按设计如此，两个原因见 `s4-deviations.md` §9 第 50 条，
需要用户裁决。接线本身是活的（三条断言钉住调用点与装配点，见上面的反向验证）。

## 这一节没有做的事

- **facts 的取值没有改善**（第 50 条）。接线做完了，S4 的装配下仍然写不出行——这是
  门禁与候选来源两处的缺口，不是接线没接上。
- **模型自报的 `facts` 没有搬回来**（第 30 条的反转属于 S5/S6）。
- **`episodic` 事件向量仍然不写**（第 31 条）：1.0 的唯一消费者是 S6 的 `text_ann`。
