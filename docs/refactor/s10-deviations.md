# S10 违背 / 偏离审计（PhotoBench 前端重构）

审计对象：`docs/refactor/2026-10-05-s10-implementation-plan.md`。
基线：设计文档 `docs/architecture/2026-09-21-sentrix-home-web-2-implementation-design.md:515`
对 S10 的定义，与 1.0 的 `services/photobench/`（**只读，唯一权威**）。

**这份文档现在的形态是「施工中的偏离登记」**（S3/S4/S5 那三份是收尾审计）。
S10b（前端重写）动工后若有新偏离，追加到 §5。

---

## 0. 一句话摘要

S10 按用户 2026-10-05 的裁决**拆成两段**：S10a（补齐后端端点）→ S10b（前端重写）。

S10a 施工中**范围扩到了 `apps/api`**：计划假定那些端点是纯新增，核查发现其中两条
（`keyframe-analysis` / `memory-effectiveness`）要用的数据 2.0 根本没暴露。
根因是**同一个缺陷形态出现了三次**——1.0 的读侧是 `SELECT *`，2.0 把它换成窄投影时
丢掉的列里，有几列**有真实的读者**（见 §2.2）。

另外**修正了一处 S8 的错误判断**：`raw` 被当成「没有读侧」去掉了，实际有一个读者（§2.1）。

端点数是 **8 条不是 7 条**：计划漏了 `export-sft`（§4.3）。

**没有一条偏离是凭喜好的**——每条都指到 1.0 的具体行，或指到本仓库的一条既有契约。

---

## 1. 与计划 / 设计文档的偏离

### 1.1 S10a 的范围扩到 `apps/api`（用户 2026-10-05 裁决）

计划 §1.5 把 S10a 定义成「补齐 7 个 PhotoBench 后端端点」。施工前核查发现，
那些端点里有 2 条依赖 **2.0 主 API 没有暴露、甚至没有存储**的数据。

| # | 缺什么 | 1.0 从哪读 | 2.0 的状况 | 处置 |
|---|---|---|---|---|
| 1 | 关键帧血缘：`derived_kind` / `parent_asset_id` / `source_timestamp_sec` | `GET /api/assets`（`SELECT *`，`backend/db.py:1505`） | `AssetSummary` 是窄投影，三个字段都没了 | 补进 `AssetSummary` |
| 2 | `nb_frames`（原片总帧数） | `metadata_json.video_metadata.raw.streams[].nb_frames`（`:5721`） | **没存**——S8 主动去掉了整包 `raw` | 补回，只存这一个整数 |
| 3 | `video_processing_seconds` | `metadata_json.video_processing_seconds`（`:5729`） | **全仓无** | 补回，视频流水线加计时 |
| 4 | 记忆空间的 `created_at` | `GET /api/memory-spaces`（`SELECT *`，`backend/db.py:1410`） | spaces 投影只挑四列 | 补回（§4.4） |

**第 4 条是施工中才发现的**（第 1–3 条在计划阶段就报给了用户并获裁决）。
它和第 1 条是同一个形态，处置口径一致，所以**没有再单独问一次**——见 §4.4 的说明。

**为什么值得扩范围，而不是「PhotoBench 自己绕开」。** 绕开那条路核过：

- 逐视频打 `/videos/{id}/scenes` → `/video-scenes/{id}` 只能拼出血缘，**第 2、3、4 条
  2.0 根本没存/没暴露，绕不过去**；而且每次分析要多 ~40 次 HTTP。
- 补齐的四处**都是恢复 1.0 有过的东西**，不是给 2.0 加新能力。

**代价**：S10a 从「PhotoBench 后端」变成「PhotoBench 后端 + `apps/api` 四处小补」。
**好处**：PhotoBench 的分析函数与 1.0 逐行同形，日后并排比对不必先解释差异。

### 1.2 设计文档 `:515` 的行数是陈旧的

文档写「3,646 行 Vue 单文件」，**实测 3,797 行**（脚本 `:1-2836` + 模板 `:2838-3797`，
无 `<style>` 块）。与 S9 的 7,751→7,997 同一类偏差，不影响范围。

---

## 2. 修 2.0 的缺陷（不是偏离，是**不把缺陷留在 2.0**）

### 2.1 S8 判定 `raw`「没有读侧」——**这个判断是错的**

`apps/api/src/sentrix/domain/media/models.py:110` 原话：

> 只有 `raw` 不搬——那是整包 ffprobe JSON，1.0 写进 `metadata_json.video_metadata`
> 之后**没有任何读侧用过**

实测 1.0 全仓（排除 `.bak-*` 与 `__pycache__`），两个字段**各恰好有一个读者**，
都在 `services/photobench/backend/benchmark_orchestrator.py` 的 `get_keyframe_analysis`：

| 字段 | 1.0 的唯一读点 |
|---|---|
| `video_metadata.raw.streams[].nb_frames` | `:5721` |
| `metadata_json.video_processing_seconds` | `:5729` |

**S8 查的是主产品读侧，漏了 PhotoBench——它在另一个服务里。** 那句判断对主产品成立、
对整个 1.0 不成立。

**改法**：`VideoMetadata` 补 `frame_count`（**只补这一个整数，不存整包 raw**——
「别为每个视频塞一份完整流/标签树」的顾虑仍然成立），视频流水线补
`video_processing_seconds`。注释里那句被证伪的话一并改对。

**一处附带确认**：2.0 的 `video_processing_seconds` **探测失败也写**。1.0 同样如此——
成功路径写 `elapsed`（`processor.py:205`），失败路径写
`round(time.perf_counter() - started, 3)`（`:223`，那条带 `retryable` 标记，是 `except`
分支）。所以这不是偏离。

### 2.2 窄投影丢掉「有读者的列」——同一个形态出现三次

1.0 的 `list_assets` / `list_memory_spaces` 都是 `SELECT *`（`db.py:1505` / `:1410`），
2.0 把它们换成显式列投影。**投影本身是对的**（少回几十列），问题在于**没人核对
「被丢掉的那些列里有没有读者」**：

| # | 丢掉的东西 | 读者 | 少了会怎样 |
|---|---|---|---|
| 1 | `AssetSummary` 的 `derived_kind` / `parent_asset_id` / `source_timestamp_sec` | PhotoBench 关键帧分析 | 认不出派生帧，`retrieval` 与 `temporal` 两块归零/为空 |
| 2 | `VideoPayload` 的 `nb_frames`（现 `frame_count`）与 `processing_seconds` | 同上 | `processing` 整块空 |
| 3 | spaces 的 `created_at` | PhotoBench 复用基座 | 三处退化成**空串比较**，不报错、结论默默地不对 |

第 1 条顺带修好一条 S8 登记过的缺陷：`s8-deviations.md` §2 第 3 条「1.0 前端关键帧
计数恒 0」——主产品前端要按关键帧计数，就必须能认出关键帧，依据正是这三列。

**这三处的共同点：少一个字段不会抛异常，只会让下游算出一个看起来正常的错数。**
这是本仓库反复出现的失败形态（W1 的方案漂移、S9b 的门禁盲区都是同一类）。

---

## 3. 1.0 的历史包袱，**不移植**

| # | 包袱 | 为什么不 |
|---|---|---|
| 1 | **arbiter 状态面板**：模板 `:2845-2856` 的 8 格「实时调度状态（VLMArbiter）」，`:2711-2723` 每 3 秒轮询 `GET /api/arbiter-status` | **该端点不存在**（1.0/2.0 主 API 都没有这个能力）。搬一块永远拿不到数据、每 3 秒打一次 404 的面板过去，不如不搬。S10b 删掉 |
| 2 | `video_keyframe_webp` 这第二个 `derived_kind` 取值 | 随 `s8-deviations.md` §3 第 1 条一起去掉。读侧保留同时认两个的形状，实际只命中一个 |
| 3 | `worldmm_*` 那套挑选口径（`worldmm_selection_reason` / `worldmm_score`） | 随 `s8-deviations.md` §3 第 2 条一起去掉。读侧沿用 1.0 的 `or` 回退链（缺了就是「未记录」/ `None`），不报错 |

---

## 4. 施工中的新增偏离

### 4.1 视频元数据的来路：资产清单 → 逐视频端点（形状偏离，非口径偏离）

1.0 一次 `GET /api/assets` 就把视频时长/帧数/处理耗时全拿到了（`SELECT *` 里的
`metadata_json`）。2.0 即使补上血缘列仍是投影，所以 `RunReports` 会**逐视频再取一次**
`GET /videos/{id}`。

**代价**：每次分析多 N 次 HTTP（N = scope 里的视频数）。benchmark 相册实测最大
`album3-max-video10` 有 10 个视频，`album3-kling` 有 3 个。取失败就跳过那个视频，
与 1.0 遇到字段缺失时的行为一致（它有一整套 `isinstance(...)` 判断）。

**为什么不索性塞进 `AssetSummary`**：那会让通用的资产列表接口为一个视频专属面板
背三个视频专属字段，还要在 `list_by_scope` 里为每个视频补一次 probe——把 N+1 从
应用层挪到仓储层，问题没消失、只是更难看见。

### 4.2 资产清单的 `limit`：1.0 的 2000/5000 在 2.0 只能是 1000

1.0 关键帧分析写 `limit=2000`（`:5611`）、记忆有效性写 `limit=5000`（`:5648`）。
2.0 的 `GET /api/v1/assets` 在路由里做 `min(limit, 1000)`（`api/routes/media.py:301`）
——给更大的数**不报错，只静默截断**。

benchmark 相册实测最大约 400 条，1000 够用。**已知代价**：相册超过 1000 条时这两个
面板会悄悄少算。「静默截断」这个形状本身也是 `infrastructure/sentrix/client.py`
已经登记过的差异。

### 4.3 补了 `export-sft`——它其实有一个（历史版本的）前端调用点

计划列的 7 条里没有它。实现时一并补了，当时的理由是「1.0 的真实能力，少了它会静默消失」。

**但我当时写的「前端确实零命中（`App.vue` grep 空）」只对当前版本成立。**
S10b-0 核查扫了 6 个 `App.vue.bak-*`，最老那份（`codex-macro-20260828-121920`）里有
`exportSftTraces()` 与一个「导出 SFT JSON」按钮，指向 `/api/runs/{id}/export-sft?scores=…`。
**当前版本把它删了，历史版本有。**

所以这条端点的来历不是「前端从没用过」，而是**「前端曾经用过、后来被删掉，而删除没有
同步到后端」**——这正是「换后端时能力静默消失」的另一个方向，而且更隐蔽：
**只读当前版本会得出「它是死的」，而它其实是被单方面遗弃的。**

**处置**：S10b 重写时把「导出 SFT JSON」按钮加回（与「导出轨迹 JSON」并排），后端已就绪。

### 4.4 spaces 的 `created_at`：同一形态的第四处，**没有再单独问**

`GET /api/v1/memory-spaces` 原先只回 `{id, name, kind, status}`。1.0 回的是
`SELECT *`，`created_at` 在里面，而它有**三个真实读者**（全在 PhotoBench）：

1. `GET /api/memory-spaces` 的「新创建的在前」排序；
2. `_build_reuse_bases` 挑最新 scope 做复用基座；
3. `_qa_scope_id_for_album` 的次级排序键。

少了它，这三处一起退化成 `str(None or "")` 之间的比较——**不报错**。

**为什么没有再问用户一次**：用户 2026-10-05 已就同一形态裁决过两次（§1.1 的第 1–3 条），
处置口径一致，且这是一列的暴露、不是新端点。按「计划已定时直接做完再报」执行，
在这里登记备查。

### 4.5 `sentrix_url` 查询参数的语义收窄

1.0 的 `/api/memory-spaces` 收 `sentrix_url` 参数，可以指向**任意** Sentrix
（1.0 的前缀是 `/api`）。2.0 的前缀写死 `/api/v1`，所以把它指向 1.0 的 Sentrix
会失败。默认值仍是配置里的 `sentrix_url`（线上是 `192.168.0.153:8091`，实测回 404 →
`sentrix_unavailable`，不崩）。

**没有为它加「兼容两种前缀」的分支**：那是给一个迁移期的临时便利加永久复杂度。

### 4.6 starlette 的 `FileResponse` 与 1.0 手写 Range 的两处行为差

1.0 的 `_serve_file`（`:8020+`）自己实现了分段。2.0 用 starlette 1.6.0 的
`FileResponse`（原生支持 Range），但两处行为不同，已在路由 docstring 写明：

| 情形 | 1.0 | 2.0（starlette） |
|---|---|---|
| 格式错误的 Range（`bytes=abc`） | 416 + `Content-Range: bytes */<size>` | **400**，无 `Content-Range` |
| 越界的 Range | 416 + `Content-Range` | 416 + `Content-Range`（一致） |

另外 1.0 发的 `Cache-Control: no-cache` / `Vary` 两条头没有照搬。

### 4.7 几处照 1.0 而非照我的任务描述的地方（**我的描述错了**）

施工中核对出来的，记在这里免得后人以为实现写歪了：

| 处 | 我写的 | 1.0 实际 |
|---|---|---|
| `list_manifests` 的计数 | `len(m.get("faces", []))` | **`len(m.get("faces", []))` 没有 `or []`**——`"faces": null` 会抛 `TypeError`，而那个 `except` 只接 `(OSError, KeyError, JSONDecodeError)`，于是**整个列表端点回 400**。照 1.0 |
| `test-model` 的报错文案 | `no complete choices` | `model endpoint returned no **completion** choices` |
| 1.0 行号（`raw` / `processing_seconds`） | `:5718` / `:5726` | `:5721` / `:5729` |

### 4.8 PhotoBench 侧的其它适配（2.0 与 1.0 的接口事实）

| 项 | 1.0 | 2.0 |
|---|---|---|
| 前缀 | `/api` | `/api/v1`（`API_PREFIX` 单点定义在 `domain/sentrix/ports.py`） |
| assets 信封 | `{"assets": […]}` | `{"items": […]}` |
| 资产路由 | — | 用 `{asset_id:path}`：否则 `/api/assets/a/b/file` 在 2.0 会变成 FastAPI 的 404 `{"detail": …}`，而 1.0 是 400 `invalid asset id` |
| `media_file` 穿越拦截 | 只比对 `file_name` 一段（`album_id="../../etc"` 能穿出去） | 三段都拦 |
| `reuse_bases` 的输入 | `repo.list_runs()`（全部） | `RunIndex.entries()`（全部）——**不能用 `ReadRuns.list_runs`**，它分页 20 条，会让第 21 条之后的基座整批消失 |
| `qa-dataset` 的 jsonl | 裸读 | 裸读（**不用** `AlbumSource.qa_rows`，它会叠 `expected_action`，形状就变了） |

---

## 5. 未决 / 待办

- ~~老视频的 `processing` 面板~~（§2.1 残留）：**已核实不适用**。2.0 的库里现有
  53 个资产、**全是图片、零视频**（`GET /api/v1/assets` 实测），不存在「S10a 之前导入
  的视频」这批数据。将来导入的视频会由新代码当场写上这两个数，不需要一次性重探脚本。
- **前端端口**：计划 §1.6 裁决②待核（建议 4275，`apps/web` 用 4274；后端 9597）。
- **主产品前端的 TS 类型**：`apps/web/src/shared/api/client.ts` 的 `AssetSummary`
  接口没有跟着加那三个血缘字段。字段是**增量**的，不影响 `tsc`；但主产品前端要用
  它们做关键帧计数（§2.2 第 1 条）时得补。
- **S10b 动工前要扫的**：`App.vue.bak-*` 那 6 个备份文件（可能含已从当前 `App.vue`
  移除的功能）。

---

## 6. S10b（前端重写）的偏离登记

施工中追加。**每条都指到 1.0 的具体行或本仓库的一条既有契约。**

### 6.1 我给的 1.0 行号多处不准，以文件实际内容为准

S10b-0 的核查报告与我从它转述的行号，被后续几个切片**逐处纠正**过。已经确认错的：

| 我说 | 实际 |
|---|---|
| `:1340-1809` 是「trace 解析**与图数据构造**」 | 那 470 行**全是 trace/时延/调试解析**；图构造在 `:1810-2055` |
| 图构造共 ~245 行 | **漏算了开头 `:32-175` 的两个巨型 computed（144 行）**——按区块 grep 容易漏 |
| `qaTypeLabel`/`qaActionBadge` 在 `:2821-2823` | `qaTypeLabel :2789-2791`、`qaActionBadge :2792-2794`、`qaAnswerabilityLabel :2795-2797`、`qaReferenceLabel :2821-2823` |
| `statusLabel` 在 `:337` | `:338` |
| `numericMs` 要移植 | **1.0 里根本没这个函数**（全文件 grep 无） |

**教训**：区块级 grep 得到的行号只能当线索，**派活前要让执行方以文件实际内容为准**——这次每个 agent 都这么做了，所以都正确地纠正了我。

### 6.2 类型适配（行为等价，但调用形状变了）

| 处 | 1.0 | 2.0 | 为什么 |
|---|---|---|---|
| `api()` / `post()` 的路径参数 | 收全路径 `"/api/runs…"`，`BASE` 是个没人用的常量 | 收相对路径 `"/runs"`，内部拼 `${BASE}${path}` | 照抄会让 `BASE` 触发 `noUnusedLocals`。前缀仍集中一处，`media.ts` 也 import 它 |
| `albumLocalUrl` / `decorateMedia` | 从模块级 `activeRun.value.album_id` 读 | 多一个显式 `albumId` 首参 | React 没有那个全局。逻辑一字未改，只是把入口显式化 |
| `judgeRoundState` / `judgeScoreLabel` | 读组件 computed `activeRejudge` | 多一个显式 `task` 参数 | 纯函数层没有组件状态。**调用方必须传 `activeRun.rejudge ?? null`，否则语义从根上就错** |
| `callOutcome` | 依赖 `callObservation`(1.0 `:1486-1502`，返回 6 个字段) | 只内联了用得到的那一段 | 整个 `callObservation` 属 trace 切片 |
| `api`/`post` 返回 | `Promise<any>` | 泛型 `<T = unknown>` | 可读性，行为不变 |

### 6.3 照搬未改的 1.0 缺陷与可疑之处

**这一节是「迁移非重设计」的落地**：1.0 的这些问题照搬，但记账，供日后单独决定。

| # | 处 | 现象 |
|---|---|---|
| 1 | `executionState` 返回 `key:"unknown"` 时 | 吐出的 **`status-unknown` 这个 CSS 类在 1.0 与 2.0 的样式里都不存在**——「状态待确认/未记录」会退到默认 `.status-pill` 样式。**孤儿类** |
| 2 | `effectiveRunSummary` 的 `retrieval_recall_mean` | 读的是 **`saved.retrieval_recall_macro`**，不是 `_mean`。**疑似复制粘贴 bug** |
| 3 | 同函数的 legacy 分支 `retrievalGt` | 只累加 `retrieval_image_ids`，**不含 video**；而 `retrievalItems` 由 `mediaRefs`（含视频）筛出——口径不齐 |
| 4 | `agent_throughput_qa_per_s` | `saved.X ?? null`，**从不现算** |
| 5 | `task_decision_labeled_count` 与 `task_decision_valid_count` | 恒等（同一个 `actionJudges.length`），两个名字一个值 |
| 6 | `nearestRankPercentile` | `Number(null)===0` 是有限数 → `[null]` 得 `0` 而不是 `null`。已用断言钉住这个行为 |
| 7 | `decorateMedia` 补 `.mp4` 用 `!/\.mp4$/i`，`albumLocalUrl` 用更宽的 `VIDEO_EXT` | **两个函数的视频判据不一致**，未合并 |
| 8 | `callOutcome` 的 tool_call 分支 | 既无 `next_step` 又无 `related_tool` 时输出「本轮结果：调用工具 **-**」（`-` 漏进句子里） |
| 9 | `fmtNumber` 的 `.replace(/\.0$/,"")` | 只吃单个 `.0`：`fmtNumber(12,"%",2)` 得 `12.00%`，不裁 |
| 10 | `fmtMs` / `fmtPct` | 只挡 `null`/`undefined`，非数字串走 `NaN`（得 `NaNms` / `NaN%`） |
| 11 | 未命中回退**四处各不相同** | `statusLabel` 回原值、`actionLabel` 回「未记录」、`qaTypeLabel` 回「未分类」、`qaActionBadge`/`qaAnswerabilityLabel` 回 `-`。这是最容易在重写时被「统一」掉的 |
| 12 | `reuse-base` 的 watch（1.0 `:2554`） | 用 `Object.keys(manifest?.qa_sets \|\| {})`，而 `/api/manifests` 的 `qa_sets` 是**数组** → 得到 `["0","1",…]` → `options.includes(selectedQa)` 永远匹配不到真实题集名。**疑似 1.0 bug**，归 runs 视图切片 |

### 6.4 前端工程事实（S10b-1）

- **`tsconfig.app.json` 里没有 `"strict": true`**（我在派活时误说成 strict 开着）。实际开的只有 `noUnusedLocals` / `noUnusedParameters` / `verbatimModuleSyntax` / `erasableSyntaxOnly` / `noFallthroughCasesInSwitch`。与 `apps/web` 的配置一致——**这是仓库既有的前端口径**，不是本次引入的。
- **两个同名常量别混**：`apps/photobench/src/photobench/domain/sentrix/ports.py:55` 的 `API_PREFIX = "/api/v1"` 是**调上游 apps/api 的客户端前缀**；本 app 自己的挂载前缀是 `apps/photobench/src/photobench/main.py:38` 的 `API_PREFIX = "/api"`。前端用的是后者。
- **端口 4276**（计划原建议 4275，实测被 `/home/asus/s5verify/web/apps/web` 的 vite 占着，已跑 12 天）。
- **不移植 react-router**：1.0 只用 `activeView` 切两个 `v-if`，没有 URL 路由语义。

### 6.5 恢复一处被单方面遗弃的能力（S10b）

S10b-0 扫六份 `App.vue.bak-*` 发现：「导出 SFT JSON」按钮存在于最老那份备份
（`codex-macro-20260828-121920`）的 `exportSftTraces()`（`:1469`），**当前版本已删**，
而它的后端端点直到 S10a 才补上。详见 §4.3。

**处置**：S10b 重写时把这个按钮加回（与「导出轨迹 JSON」并排）。

### 6.6 待收的整合项：同名函数被三处实现（**必须在阶段 1 收口时消掉**）

并行切片的代价，我在派活时没预料到。`trace.ts` 因为开工时 `graphs.ts` 还不存在，
为了能独立编译，**内置了两份私有副本**：

| 函数 | 1.0 行号 | 现在有几份 |
|---|---|---|
| `conversationTurns` | `:611` | `trace.ts` 私有副本 + `evidence.ts`（在写） |
| `itemTimingBreakdown` | `:2122` | `trace.ts` 私有副本 + `details.ts`（在写） |

**收口时必须**：以 `details.ts` / `evidence.ts` 的为准，删掉 `trace.ts` 里的私有副本、
改成 import，**并确认两边的行为逐字相同**（若不同，那才是真问题——要查是谁读错了 1.0）。

**教训**：并行的纯函数切片之间会有隐蔽的共享依赖。派活前该先定一份「哪个模块导出什么」的清单，
或者给每个切片划出一块**只读的公共底座**。这次靠事后收口补，能补上，但代价是白写了一份。

### 6.7 我任务描述里与 1.0 实现不符的第二处（trace 切片发现）

`qaLatencySegments` 的分段里，**「Judge 排队」那一段在 1.0 里永远不产生**：
它读 `breakdown.judge_queue_wait_ms`，而 1.0 的 `itemTimingBreakdown`（`:2122`）**从不返回这个键**，
于是 `judgeQueueMs` 恒为 `NaN`，那个分支不可达。

我在任务描述里把它列成了有效分段。**照搬未修**，并写了一条断言把这个死分支钉住
（「即使 `item.timing_breakdown.judge_queue_wait_ms=40` 也不出该段」）——
这样将来有人「顺手修好」它时会立刻红。

### 6.8 `trace.ts` 的类型体例

与 `format.ts` / `qaBrowser.ts` 一致：**入参一律 `unknown` + 内置 `dict/str/objectList` 收窄，
零类型导入**。原因之一是 `types.ts` 并没有为 call / trace / group 这些形状建模。

**这是我对「类型要覆盖」这条要求的过度承诺**：实际落地的类型覆盖比我在 F1 里写的小。
若后续要把这层收紧，应先扩 `types.ts` 的形状，而不是在各模块里各写各的收窄。

### 6.9 **`api/types.ts` 把一处嵌套结构拍平了——按它取值会静默出错数**

**这是 S10b 抓到的最重要的一处，形态正是本项目反复防的那种：不抛异常，只是数不对。**

| | |
|---|---|
| **后端实际返回** | `memory_effectiveness.py:661-674` 的 `evidence_effectiveness` 是**嵌套**的：`{tool:{image,video_keyframe}, usage:{image,video_keyframe}, event:{...}, note}` |
| **`types.ts` 声明成** | **扁平**的 `{image?, video_keyframe?, event?}` |
| **1.0 怎么读** | `App.vue:112-116` 的 `memoryRecallMatrices` 读 `evidenceBlocks[dim.key]`（`tool`/`usage`）与 `evidenceBlocks.event`——**与后端一致** |
| **后果** | 按 `types.ts` 的声明取值会一路 `undefined`，**命中矩阵的 L3 三格静默变成「未计算」** |

`lib/graphs.ts` 是**绕过去照后端取的**（它读后端源码核对过），所以功能上没错；
错的是那份类型声明——**它会把下一个照着类型写的人引到坑里**。

**教训**：这一版 `types.ts` 是照**后端源码与实测响应**写的，但它仍然在「嵌套 vs 扁平」这种最容易想当然的地方出了错。
**类型文件自己也需要被核对**——它不像测试，错了不会红。

### 6.10 并行切片留下的重复实现（**已在收口时清理**）

六个纯函数切片并行开工，后开工的为了能独立编译，各自放了私有副本。同名函数清单：

| 函数 | canonical | 副本 |
|---|---|---|
| `conversationTurns` | `lib/evidence.ts` | `lib/trace.ts` |
| `itemTimingBreakdown` | `lib/details.ts` | `lib/trace.ts` |
| `itemToolTrace` | `lib/trace.ts` | `lib/details.ts` |
| `itemCallMetrics` | 二选一 | `telemetry.ts` / `trace.ts` |
| `fmtPct` / `memoryLayerToolMetric` | `lib/format.ts` | `lib/graphs.ts` |
| media 小工具（`mediaKey`/`inferMediaType`/`albumLocalUrl`/`isVideoMedia`/`decorateMedia`/`itemMedia`） | `api/media.ts` | `lib/graphs.ts` |
| agent2 小工具 | `lib/trace.ts` | `lib/graphs.ts` |
| `EXECUTION_PHASES` | **无 canonical，新建 `lib/constants.ts`** | `lib/telemetry.ts` |

**根因是我派活的方式**：我按「1.0 的行号区块」切分，而**1.0 是单文件、没有模块边界**——
按行号切出来的区块之间本来就有共享函数。派活前该先定一份「哪个模块导出什么」的清单，
或者划出一块**只读的公共底座**。这次靠事后收口补，但代价是白写了一份实现，而且
**多份实现之间若漂了，就是新的静默错数来源**。

### 6.11 其它照搬未改的 1.0 逻辑（第二、三批）

**graphs 切片**：

| # | 处 | 现象 |
|---|---|---|
| 13 | `recorded()` | 把 `0` 当「未记录」——真实 0 事件 / 0 调用 / 0 关键帧会显示成「未记录 / 未展开」 |
| 14 | `memoryEvidenceGraph` 的 L4+ | 「证据核验」只是 `activeRun ? "Judge" : "未记录"`，**与是否真有 judge 数据无关** |
| 15 | `memoryLayerMetrics` 的 L2 | 「N 个事件」在未展开时**静默切换到全量计数**，来源会变 |
| 16 | `memoryRecallMatrices` 的 L0 | 「全量已评分题」恒为 `100.0%`（`pct(x, x)`），是**装饰格** |
| 17 | `chainDetailStatements` 的外层 `slice(0,12)` | **不可达的防御代码**：previews 先 `slice(0,8)`，上限正好 12 |

**telemetry 切片**：

| # | 处 | 现象 |
|---|---|---|
| 18 | `comparableMemoryProfile` 的 replay 分支 | 形状与另两个分支**不一致**：replay 把画像字段**摊平**，另两个嵌一层 `{memory_profile:{...}}`。导致 replay 结果喂给 `memoryProfileRows` 会落进默认 vLLM 分支 |
| 19 | p95 的门槛 | `sorted[Math.floor(count*0.95)]`，n=20 时就是 max——`count>=20` 这个门槛**并不能让 p95 与 max 分离**，要到 n=21 |
| 20 | `liveTelemetryRows` 的 `effectivePeak` | 只按 `Object.keys(peak).length` 判空，但**行末的 KV 仍硬用 `peak.kv_cache_used_tokens`**（不是 effectivePeak） |
| 21 | `aggregateMetricRows` 的 `phase` 参数 | 在 1.0 函数体内**从未被引用**（1.0 自身遗留） |

**details / evidence 切片**：

| # | 处 | 现象 |
|---|---|---|
| 22 | `itemEvidenceMedia` 的 `predicted_media \|\| predicted_images` | 空的 `[]` 是 truthy，**会挡住**向 `predicted_images` 的回退 |
| 23 | `itemMedia` 内部不对称 | `predicted_file_names` / `retrieved_file_names` 分支**不套** `decorateMedia`，而 `evidence_source_file_names` **套**；且 `itemMedia` 对候选截前 6，`toolRecallMedia` 不截 |
| 24 | `phaseSummary("model_deploy", …)` | `unload_seconds` 缺失时**不回** `-`，落到通用「耗时 X」；无墙钟的未知阶段回**空串** |

### 6.12 我任务描述里与 1.0 实现不符的其余各处

| 我说 | 实际 |
|---|---|
| 「四级回退」 | 代码里其实是 **3 个分支**——1.0 把「同轮 trace 序号」与「execution_trace 的 parent」合并成同一表达式 |
| 「'Judge 排队'是一个有效分段」 | **不可达的分支**（见 §6.7） |
| `deliveryBreakdown` 有「分母」 | **没有**。它只是把两个计数原样透传，判据是「任一非零才出卡片」 |
| `tokenDistributionRows` 要「断言分桶边界」 | 该函数**没有分桶**，是 6 行格式化表 |
| `chainDetailStatements` 会被 13 个输入触发截断 | 外层 `slice(0,12)` **不可达**（见 #17） |
| `numericMs` 要移植 | 1.0 里没这个函数 |
| `memoryLayerMetrics` 的五层有 created/effective | 它是「卡片指标」字符串，没这两个数；要测分层得落在 `memoryValidityArchitecture` 上 |

### 6.13 `api()` 无法接受外部 `signal`——**这是 1.0 的原样，不是缺陷**

`api/client.ts:48-51`：

```ts
const response = await fetch(`${BASE}${path}`, {
  headers: { ... },
  ...requestOptions,          // 调用方传的 signal 在这里进来
  signal: controller.signal,  // ……紧接着被无条件覆盖
});
```

`hooks/useQaPage.ts` 想用 `AbortController` 做卸载清理，发现传不进去，改用了 `aliveRef`（卸载置 false，迟到的响应不再 setState）。

**我一度打算「修好」它——查了 1.0 才发现它一字不差也是这么写的**（`App.vue:287-321`，同一个位置、同一个覆盖）。
所以：

- **2.0 的 client 是忠实的**，这不是缺陷；
- `aliveRef` 是**正确的适配**（它保住了「卸载后不 setState」这个意图，代价是请求仍会跑完——1.0 亦然）；
- **不修**。要修的话它是一次**行为变更**，得单独裁决，不能混在迁移里。

**教训**：看到「明显的缺陷」时，先查 1.0 是不是也这样——`git blame` 不了 1.0，但 `sed -n` 可以。
这一处如果按直觉改了，就是一条不会被任何测试发现的静默偏离。

### 6.14 hooks 层的其它适配与存疑（第二、三批）

| 处 | 说明 |
|---|---|
| `changeQaPageSize` | 1.0 是 `v-model.number` 先写值、`@change` 再调**无参**函数；React 无 v-model，改成接受可选新值先落 state（内部用 ref 同步，同一 tick 可读），再回第 1 页 |
| `detail.judge` 为空时的 `Object.assign` | 1.0 写的是丢弃的临时 `{}`，**实际不合并**。照搬保留了这个空转（`else if (detail.judge)`）——若后端详情必带 `judge` 则无影响，否则新 judge 会丢 |
| `loadQaPage` / `loadQaDetail` / `toggleQa` | 与 1.0 一样**不 catch**（错误上抛，1.0 靠轮询的 try/catch 兜）。组件里 `void fn()` 时需自行处理 rejection |
| 三个 DOM 全局查询 | `#runs-region` / `#qa-results` → hook 返回 `ref` 给组件挂，`scrollIntoView` 替代 `window.scrollTo` |
| 两个 `reactive(new Set())` | → `useState<Set>` + `new Set(prev)`。**直接 `set.add()` 不会重渲染** |

**集成时必做**（本切片 agent 明确点名）：把 `activeRun?.rejudge` 接进 `useQaPage`，
否则 `visibleQaItems` 的「重评进行中」过滤**不生效**——又是一个不报错、只是行为不对的点。

**可能重复的共享 helper**（各 hook 很可能各写一份，待收）：`commit()`（ref 权威 + state 镜像）、
`aliveRef` 的卸载守卫、`EMPTY_*` 重置常量。

### 6.15 hooks 层：跨切片缺口与组合层必做（**视图接线时逐条核对**）

并行切片之间又出现了接缝，这些**不报错、只是行为不对**：

| # | 缺口 | 后果 |
|---|---|---|
| 1 | `useQaPage` **没有 `resetQaState` 的对应物** | 1.0 在切 run 时清空逐题状态（`:2324-2326`）。不接的话，**切 run 后上一轮的逐题状态不会重置** |
| 2 | `activeRun?.rejudge` 未接进 `useQaPage` | `visibleQaItems` 的「重评进行中」过滤**不生效** |
| 3 | 1.0 的 `init()` 里 `Promise.all` 含 `loadRuns(1)` 与 `loadProfiles()` | 组合层必须按 1.0 并行发起这三/四条；且 `loadRuns`/`loadProfiles` 失败会**中止** `manifests` 赋值（1.0 原样） |
| 4 | `markConnectionConfigDirty` 的抑制窗口 | F1 的 `loading` 只是配置段加载态（1.0 是全局 `loading`），抑制窗口更窄。组合层若改变 `judgeProviderId` 的赋值时机，可能让 init 后立刻变 dirty |

### 6.16 我任务描述里与 1.0 界面不符的一处

**`existingScopeId` 与 `judgeProviderId` 在 1.0 的模板里都没有任何绑定点。**
前者只由基座 watch 写、由 `onModeChange` 清；后者只在 `init()` 里赋值一次。

所以「**切 Judge provider → 覆盖 url/model**」这个交互在 1.0 里**没有 UI 触发**——
那个 `watch(judgeProviderId)` 实际只在 init 时跑一次。我在任务描述里把它当成了一个真实交互。

**照搬实现，但真实触发场景存疑**，登记备查。

### 6.17 轮询为什么不能用 `setInterval`

`useRuns` 的轮询用**自续期 `setTimeout`** 而不是平铺的 `setInterval(2000)`。
理由：`setInterval` 表达不了 1.0 的两个条件——

- **不 running 就停**；
- **出错时 5 秒退避**（1.0 的 `catch` 里重新排 5s，而不是继续 2s）。

用 `setInterval` 会「启停条件照搬」这条要求落空——而那种落空是**看不出来的**：
界面照样刷新，只是失败时会以 2 秒的节奏反复打一个已经坏掉的接口。

测试用 fake timers 钉住：1999ms 不触发、2000ms 触发、没任务跑一轮即停且不再续期、卸载后 20s 内不再触发。

### 6.18 `scrollIntoView` 丢掉了 1.0 的 12px 偏移

`scrollToRunDetail`（1.0 `:2369-2378`）是 `nextTick` + `rAF` + `getBoundingClientRect().top - 12` + `window.scrollTo`。
2.0 改成 `requestAnimationFrame` + `ref.scrollIntoView({behavior:"smooth",block:"start"})`，
**丢掉了那 12px 偏移**（`scrollIntoView` 没有等价参数）。

纯观感差异，不影响功能，登记备查。

### 6.19 又一批共享 helper 重复（待收）

`activeRunIdRef` / `commit`（「ref 为权威 + state 镜像」这套模式）在 `useRuns` / `useRunDetail` / `useQaPage` **三处各写了一遍**；
`encodeURIComponent` 包装在所有 hook 里重复；`markConnectionConfigDirty` 在 F1/F2 各一份；
测试里的 `jsonResponse` / `renderHook` harness 每个文件各写一份。

**这是并行切片的第三次同型代价**（前两次：纯函数层的 8 处同名函数、`itemMedia` 的归属写错）。
派活前定一份「哪个模块导出什么」的清单，本可以避免。
