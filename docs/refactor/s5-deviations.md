# S5 违背 / 偏离审计（人脸与身份层 · 收尾与画像）

审计对象：`docs/refactor/2026-09-23-s5-implementation-plan.md`。
基线：1.0 提交 `2d2cee7c`（`behaviour-baseline.md` 钉的那一个），
实读的是工作树 HEAD `fba87be9`（两者在人脸与身份层无差异——`git log` 显示这两条提交
都只动 photobench 遥测）。2.0 起点提交 `f663522`。

**这份文档有两层。** §1–§2 是第一轮**规划**的产物：那时只有 Task 69 落地，
所以那些条目的「代价 / 现状」写的是**计划形态**。§2b / §2c / §2d 是**第二轮的实施记录**：
用户裁决（§2c.1）、五条顾虑的处置（§2c.3 / §2c.4）、Task 69/70/71 实测带出来的东西
（§2b / §2d）。**已落地的部分与未落地的部分在同一份文档里是分开写的**——
不把「计划里打算这么做」与「实际观察到是这样」混成一句话。

---

## 0. 一句话摘要

S5 的 33 条移植项里，**27 条原样照搬**，**6 条要改**（其中 4 条是修 1.0 的缺陷、
2 条是 2.0 分层的结构性改法）；另有 **8 条明确不移植**（3 条是 benchmark 专属或
benchmark 泄漏、3 条是死代码、2 条是遗留表）；**7 条需要用户裁决**（1.0 没有对应实现，
或与 `frozen.md` 冲突）。

**没有任何一条偏离是凭喜好的**——每条都能指到 1.0 的具体行，或者指到本仓库的一条既有契约。

---

## 1. 审计表

### 1.1 实测证据（先摆证据，再谈改不改）

| # | 事项 | 证据 | 判决 |
|---|---|---|---|
| 1 | **AdaFace 路径在 1.0 是死的** | `model_clients.py:1629` `self.identity_adapter = None`；`grep -rn "\.identity_adapter\s*="` 全仓只有这一处赋值 + `tests/test_face_clustering.py` 的 6 处测试赋值；唯一调用点 `app.py:2299` `pipeline.face.identity_adapter.embed(_crop)` → 必抛 `AttributeError` → 被上一行的裸 `except Exception: return []`（`app.py:2310`）吞掉。1.0 自己的测试：`tests/test_face_clustering.py:126` `@unittest.skip("legacy identity_adapter contract removed; identity is fixed to buffalo_l")`。 | **不移植** |
| 2 | 实际生效的身份向量是 buffalo_l | `model_clients.py:1772-1787` 与 `:1799-1840` 两个分支都把 `"embedding_model": "buffalo_l"` 写死；`_recognition_embed` 走 `buffalo_l/w600k_r50.onnx`（`:1690-1716`）。 | **按 buffalo_l 移植** |
| 3 | `FACE_EMBEDDING_MODE=adaface` 是无人读的配置 | `platform_profile.py:273` `face_embedding_is_adaface()` 全仓**零调用**；`face_embedding_mode()` 只被 `:305` 的 summary 打日志用。启动脚本 `scripts/runtime/start_sentrix_api_8091.sh` 里那一串 `ADAFACE_*` 没有任何读取者。 | **不移植**（见第 3 条裁决） |
| 4 | **全局重聚类没有生产调用者** | `recluster_faces`（`db.py:4956`）的两个调用点：`scripts/benchmarks/ingest_album3_v2.py:67`、`scripts/benchmarks/ingest_household_face_benchmark.py:57`。生产导入路径（`pipeline.py:316-320` / `:443-470`）只调 `add_face_instance`。 | **证据不变，结论被裁决③改判为「搬」**——做成显式维护入口，仍不自动调用。见 §2c.1 |
| 5 | `_attach_uncertain_faces` / `_sync_face_entity_statuses` 只被重聚类调 | `db.py:5038-5039` 是它们唯一的调用点，都在 `recluster_faces` 内。 | **不移植** |
| 6 | **benchmark 空间名硬编码进生产代码** | `db.py:1313-1324` `_migrate_evaluation_spaces`：`WHERE name LIKE 'PhotoBench%' OR name LIKE 'photobench%' OR id LIKE '%_e2b' OR id LIKE 'photobench%'`。 | **不移植**（只移植 `include_in_people` 机制） |
| 7 | 又一处为 benchmark 开的旁路 | `agent_runtime/tools.py:688` 注释自陈「直接查 entities 表解析人物（**绕过 list_entities 的 include_in_people 过滤，benchmark scope 也能用**）」。 | **不移植** |
| 8 | `_seed_face_detect` 第三级是死路径 | `app.py:2296-2310` 的整图 embedding 兜底调 `identity_adapter.embed`，而 `identity_adapter` 恒 `None`（同第 1 条）→ 该级**永远返回 `[]`**。 | **修**（要么不搬这一级，要么把它接到真实的向量适配器上） |
| 9 | `POST /api/people/seed` 必崩 | `app.py:2349` 的返回语句用了 `m_role`；`grep -n "m_role" app.py` 显示只有 `:2373`（在 `seed_persons_batch` 内）定义过它，`:2349` 在 `seed_person_identity` 里。走到 return 即 `NameError`。 | **修** |
| 10 | 画像的引用校验有白名单后门 | `person_portraits.py:119-126` `_evidence_ref_valid`：`kind == "person_moment"` 才做存在性比对；`kind in ("relationship", "semantic_claim", "appearance")` **直接 `return True`**。 | **修**（见第 2 节逐条） |
| 11 | 确定性兜底画像通不过自己的校验（实跑验证过） | 在 153 上直接跑 `deterministic_portrait` 的样本：有证据时 41 汉字、无证据时 35 汉字，而 `validate_portrait`（`:133`）要求 **80–500**；无证据时 themes 为 0，又违反 `:138` 的 2–6。→ `person_insights.py:238-241` 的兜底**任何情况下都会落进最后的 `continue`**，兜底机制实际不存在。 | **修** |
| 12 | 画像校验失败完全静默 | `person_insights.py:232-241` 里 `errors` 被赋值 4 次，**从不写日志、不落 `person_insight_runs.error`、不写 stats**。 | **修** |
| 13 | `person_insights` 的向量去重从未生效 | `person_insights.py:271` `select_representatives(candidates, {}, limit=12, per_event=2)` —— 第二个参数是空 dict，而去重逻辑（`:92-97`）要求 `vector` 非空。只有测试传了真向量（`tests/test_person_insights.py:122`）。 | **待裁决**（修 or 照搬） |
| 14 | 图推断的输入缺项 | `person_insights.py:309-318` 的 `graph_payload` 里 `cooccurrence: {}` / `moments: []` / `devices: {}` 恒空，而提示词（`person_graph.py:14-15`）宣称输入包含这三样。 | **待裁决** |
| 15 | `graph_violations` 算完即弃 | `person_graph.py:187` 返回，`person_insights.py:327/346` 只取 `roles` / `relationships`。 | **S7a 了结（2026-10-02）：随 `person_graph.py` 一起判死。** 裁决① 判死的是整个文件（`ROLE_OPTIONS` 是唯一被捞出来的东西，落在 `domain/identity/portrait.py`），而 `find_graph_violations` 的唯一调用者是同文件的 `normalize_person_graph`——两者都在假设那一支上。2.0 没有这一步，「算完即弃」因此不存在 |
| 16 | 两个 0.30 语义不同 | `db.py:4815` 的 `threshold=0.30` 是**余弦阈值**；`db.py:4868` 的 `if quality >= 0.30` 是**质量门槛**（低于它的已 verified 人脸不去匹配已有簇，直接建新簇）。数值相同、互不影响。 | **原样照搬，两个独立常量** |
| 17 | 模型版本字段名与语义不符 | `model_clients.py:1784/1831` 的 `embedding_version = os.getenv("FACE_MODEL_NAME", "buffalo_l")` —— 变量叫 MODEL_NAME 却存进 version。**有真实后果**：签名比对（`db.py:4876-4884`）比较 `(model_name, model_version)`，所以换模型包时旧簇会被整体跳过。 | **原样照搬 + 注释写明** |
| 18 | `person_bridge` 的真实位置 | `retrieval_indexes.py:20`（`_FIELD_TYPES` 列表）与 `:180`（`("person_bridge", "people")`）。**S4 的计划文档第 309 行把它记成 `retrieval_indexes.py:150`，那是笔误。** | 修正引用 |
| 19 | `purge_unanchored_facts` 的真实行号 | `db.py:3433-3447`。**S4 的计划文档第 313 行记成 `db.py:3439-3453`，那是笔误**（3439 已在函数体内）。 | 修正引用 |
| 20 | 画像的自动触发在默认装配下不生效 | `backend/pipeline.py:764-769`：`SENTRIX_PERSON_INSIGHT_SCOPES` 默认空串 → allowlist 为空 → `if scope_id not in allowlist: return None` → **任何 scope 都不自动跑**。全仓没有任何部署文件设置过这个变量（`grep` 只命中代码与测试）。 | **待裁决**（「增量触发」这条描述与 1.0 默认装配不符） |
| 21 | `person_insights` 的四个模块全是「有接线、默认断路」 | 唯一的生产自动调用点是 `backend/pipeline.py:725` 的 `_maybe_trigger_person_insight`，被上一行挡住；其余全是 HTTP 显式请求（`app.py:1820/1844`）或 benchmark（`scripts/benchmarks/evaluate_person_insights.py:154`）。 | **待裁决**（决定 Task 85 的验收定义） |

### 1.2 结构性偏离（无行为影响，但要登记）

| # | 1.0 的形状 | 2.0 的形状 | 为什么 |
|---|---|---|---|
| 22 | `entities` 的 `identity_state` / `role_state` / `name_state` 三列是运行期 `ALTER TABLE` 加的（`db.py:1240-1243`） | 写进 `0006_identity.sql` 的 `CREATE TABLE` | 2.0 的 schema 由编号迁移管理（`database.py` 的 docstring 明确说取代 1.0 的 `CREATE TABLE IF NOT EXISTS` + 运行期 ALTER） |
| 23 | `face_instances.cluster_id` 原本 NOT NULL，1.0 用「建新表 → `INSERT SELECT` → `DROP` → `RENAME`」重建（`db.py:1257-1311`） | 一次建对（可空） | 同上；1.0 那次重建是为了让「低置信度人脸作为无簇证据存在」成为可能，2.0 直接在 DDL 里表达 |
| 24 | `_refresh_event_participants` / `resegment_events_for_confirmed_entity` 是 `MemoryStore` 的方法，被 `confirm_face_cluster` 直接调用 | 参与者投影是仓储能力、编排在 application 用例里；`confirm` 在 Task 81 时回头接上 | 2.0 的分层契约：application 只依赖 domain 端口，仓储不反向依赖上层 |
| 25 | `confirm_face_cluster` 一个函数里做四件事（写身份 / 刷事件投影 / 重分段 / 重建人物记忆） | 拆成 Task 73（身份四件）+ Task 81（投影与重分段）+ Task 85（画像侧） | 拆的理由是**避免循环依赖**：投影要读 mentions，mentions 由 confirm 写。顺序错就是 S0/S1 那种「单独看都对、合起来循环」 |
| 26 | `MemorySpace` 在 1.0 是 `create_memory_space` 的一个函数 | 2.0 迁进 `domain/spaces/models.py` + `ports.py` | **1.0 没有对应实现**——这是设计 §2.3 的既定目标（`container.py:85-87` 的注释自己写着「待记忆空间用例落地时，把 `MemorySpace` 一并迁进 domain」）。不属于「新设计」 |
| 27 | 人脸模型是模块级单例（`app.py:61` import 期构造 `FaceAdapter()`） | 装配在 `Container.build()` | 2.0 的唯一装配点是容器；但**「每进程一份」这个后果需要设计确认**（见计划文档「顾虑」第 3 条） |

### 1.3 明确不移植（8 条）

| # | 能力 | 1.0 位置 | 理由 |
|---|---|---|---|
| 28 | AdaFace / MagFace 适配器 | `face_embeddings.py:72-221`、`platform_profile.py:257-274` | 见证据 1/3：死代码 + 700 MB PyTorch checkpoint + `PYTHONNOUSERSITE` / NVIDIA 库路径两个坑（启动脚本里为此写了两段注释） |
| 29 | ~~全局重聚类 + uncertain 认领 + 实体状态同步~~ **已由裁决③改判为移植** | `db.py:4956/5048/5118` | 见证据 4/5：调用点全在 `scripts/benchmarks/`。**这条判决作废**：用户裁决③要求它作为**显式维护入口**（Task 84），仍不自动调用。保留这一行是为了让「为什么当初判不搬、后来为什么改判」都可查 |
| 30 | benchmark 空间名黑名单 | `db.py:1313-1324` | 见证据 6：**硬编码 benchmark 内容进生产代码**，与 `frozen.md` 冻结硬编码泄漏条目是同一判据。移植的只是 `include_in_people` 这个机制 |
| 31 | 绕过 `include_in_people` 的查人物 | `agent_runtime/tools.py:688` | 见证据 7 |
| 32 | `persons` 遗留表与 `/api/persons` | `db.py:515/1327/3461/3499`、`app.py:1498/1505` | 无生产写入者（`upsert_person` 全仓零调用）；1.0 自己把 legacy person 迁进 native entity（`_migrate_legacy_persons`）；`frozen` 判据是「表 0 行」的同类 |
| 33 | `pairwise_metrics` 与聚类评估脚本 | `face_clustering.py:149-182`、`scripts/benchmarks/evaluate_lfw_clusters.py`、`scan_cluster_params.py` | 只在 benchmark / 测试里用 |
| 34 | `apply_relationship_threshold` | `person_graph.py:104-116` | 死代码：`normalize_person_graph` 不调它，全仓只有测试调 |
| 35 | `find_graph_violations` 的返回值 | `person_graph.py:87-103` → `:187` | 随 `person_graph.py` 判死（S7a 了结，见第 15 条） |

---

## 2. 逐条证据（改的那 6 条）

### 第 1 条 · AdaFace：不移植

1.0 的 `FACE_EMBEDDING_MODE=adaface` 与 `ADAFACE_*` 四个环境变量在
`scripts/runtime/start_sentrix_api_8091.sh` 的 `exec env` 行里挂着，看起来像生产配置。
实际读取它们的是 `platform_profile.face_embedding_mode()`，而它的唯一消费者是
`summary()`（启动时打日志），`face_embedding_is_adaface()` **零调用**。

真正决定身份向量的是 `FaceAdapter._detect_retina_tiled_unlocked`：
两个返回分支都把 `"embedding_model": "buffalo_l"` 写死，向量的来源是
`_recognition_embed`（`buffalo_l/w600k_r50.onnx`）。而唯一会把 `identity_adapter`
设成 AdaFace 的地方**不存在**——`FaceAdapter.__init__` 把它设成 `None` 之后再无赋值。

判决：**2.0 按 buffalo_l 移植**。这条偏离的证据强度是「源码 + 1.0 自己的 skip 标记」，
属于「证据强」那一档。**但如果用户认为 AdaFace 是产品意图（brief 里点名了它），
那就不是移植而是「把 1.0 断掉的接线接上」——那是新功能，需要单独立项。**

### 第 2 条 · 画像引用校验的白名单后门：要修

1.0 原文（`person_portraits.py:119-126`）：

```python
def _evidence_ref_valid(pack, ref):
    kind = str(ref.get("kind") or "")
    ref_id = str(ref.get("id") or "")
    if kind == "person_moment":
        return any(moment["id"] == ref_id for moment in pack.get("moments") or [])
    if kind in ("relationship", "semantic_claim", "appearance"):
        return True
    return False
```

只有 `person_moment` 一种引用被真正核验存在性。另外三种直接放行。

**为什么这不只是「宽松」**：用户已定的第 1 条是「**强制证据引用校验**」。
一个模型只要把 `kind` 写成 `"relationship"`，就能让任意虚构的 `id` 通过校验——
而这正是这条机制存在的理由（提示词只示范了 `person_moment` 一种，模型却可能输出别的）。

**修法（建议，待裁决）**：三种 kind 各自去 `pack` 的对应层里核验存在性
（`relationships` / `semantic_claims` / `appearance` 在 `compile_portrait_evidence` 的
证据包里都已经分层放好了，见 `:43-118`）。三种都不在契约里时 → 判无效（现在是判无效，
这一点不变）。

**代价**：会更严格，1.0 benchmark 里通过的画像可能被判无效。
所以这条要**同时**改验收定义并重跑 1.0 的画像样本（`person_portraits.py` 的 8 条测试）。

### 第 3 条 · 确定性兜底画像：要修

实跑证据（在 153 上直接调 `deterministic_portrait`）：

```
TEXT: 从照片看，他常照顾孩子，他常做饭，他常散步。他从照片看常出现在这些瞬间里，让人感到熟悉和安心。
hanzi= 41 themes= 3
validate: (False, ['portrait too short: 41 hanzi < 80'])
EMPTY validate: (False, ['portrait too short: 35 hanzi < 80', 'themes must be 2-6, got 0'])
```

两条返回路径都必然违反 80 汉字下限，无证据路径还额外违反 2–6 themes。
`person_insights.py:232-241` 的三级降级因此**必然落到最后的 `continue`**：

```python
portrait = self.gamma.write_person_portrait(pack, role="writer")
ok, errors = validate_portrait(pack, portrait)
if not ok:
    portrait = self.gamma.write_person_portrait(pack, role="repair")
    ok, errors = validate_portrait(pack, portrait)
if not ok:
    portrait = deterministic_portrait(pack)
    ok, errors = validate_portrait(pack, portrait)
if not ok:
    continue            # ← 任何情况都会走到这里
```

判决：**修**。两条候选：把兜底文案写到 80 字以上；或把「兜底」定义为**不受长度校验约束**
（在 `validate_portrait` 上加一个 `source="deterministic"` 的分支）。
**建议后者**——理由是长度下限是给模型输出设的护栏，不是给确定性模板设的；
但这条**要用户点头**，因为它改的是校验器的语义。

### 第 4 条 · 校验失败静默：要修

`person_insights.py:232-241` 的 `errors` 从不被消费。1.0 的后果：
「这个人物这一轮没有画像」这件事在 `person_insight_runs` 里**没有任何痕迹**，
`status` 仍是 `completed`。

判决：**修**——把 `errors` 计数写进 run 的 `stats`（例如 `portrait_failures`），
并在 `s5-deviations.md` 实施期章节登记。理由：这是 2.0 的一条既有纪律
（`vector/ports.py` 的 `VectorIndexStatus.degraded/reason`：「降级原因必须能一路传到响应」）。

### 第 5 条 · `_seed_face_detect` 的第三级与 `m_role`：要修

两条都是**静态可判的死路/崩溃**（证据 8/9）。判决：
- 第三级整图兜底**要么不搬，要么接到真实的向量适配器**——**不要照搬一条永远返回 `[]` 的分支**
  （S4 的先例：FTS5 已实测可用，就不留一条永不执行的回退分支，理由写在计划文档第 312 行）。
- `m_role` 改成 `family_role`（形参名见 `app.py:2315-2320` 的签名）。

### 第 6 条 · `select_representatives` 的空向量字典：待裁决

`person_insights.py:271` 传 `{}`。修它要先把 `vector_by_asset` 从
`memory_vectors(space='visual', source_type='face_instance')` 读出来——
那是一个新增的读路径。**这条不修也能跑**，只是 `duplicate_threshold=0.94` 形同虚设。
**建议修**（代价小、行为可解释），但标成「待裁决」以免自作主张。

---

## 2b. Task 69 实施期实测到的偏离与发现

Task 69 已在隔离副本落地（commit `2824a5f`，`make ci` 全绿）。它带出四条要登记的东西：

| # | 事项 | 判决 |
|---|---|---|
| 36 | **`zip(...)` 加了 `strict=True`** —— 唯一一处字节级偏离 1.0 原文（`face_clustering.py:15`）。理由：ruff 的 B905 是 `make ci` 的硬门禁；长度在上一行已经判断过，行为不变。 | **改**（工具链强制，无行为影响） |
| 37 | **`docs/architecture/module-map.md` 有一行重复**：`sentrix/application/retrieval/retrieve_evidence.py` 在表里出现了**两次**（真实仓库 `bd3af31` 上第 66 / 67 行，两行**逐字节相同**）。隔离副本里 Task 69 重写表格后是 1 行。`check_module_map.py` 两种状态都报 0 问题。 | **保留修正**（生成器的必然结果；若要零无关改动可以加回去，但下次再跑生成器还会掉）。**根因见 §2c 第 40 条**——这是 checker 的一个洞，不是偶然 |
| 38 | **`continue` vs `break` 是一条杀不掉的变异** —— 32 处变异里 31 处被杀，存活的就是它（候选簇已降序排过，两者等价）。 | **登记为「未被门禁覆盖」**，不写测试硬凑 |
| 39 | **`recluster_faces` 的口径与 `FaceClusterer` 默认值不一致**：`db.py:4956` 的 `minimum_quality=0.55`，而 `face_clustering.py:67` 的类默认是 `0.30`。Task 69 只搬了 domain 的默认值，那个 `0.55` 属于被判死的 benchmark-only 调用方。 | **登记**；若第 4 条裁决改判「要全局重聚类」，这个 0.55 要一起搬 |

**同时实测确认了两件事**（不影响判决，只加证据）：
`pairwise_metrics` 与 `AdaFaceAdapter` / `MagFaceAdapter` 在全仓**确实没有生产调用者**（证据 1/33 复核通过）；
1.0 的四个纯函数里**没有任何 `os.getenv`**，所以「读 env 的落成模块级常量」这条预设
在这个范围里不适用（那些读取全在被判死的适配器里）。

---

## 2c. 2026-09-23 用户裁决与新增证据

上一轮在第 18 个任务处按约定停下报告，提出 7 条待裁决。**用户已裁决**，逐条登记如下。
这一节与 §2b 的区别：§2b 是 Task 69 实测带出来的，本节是**裁决本身**加 Task 70 实测带出来的。

### 2c.1 四条裁决（照办）

| # | 问题 | 裁决 | 落实位置 |
|---|---|---|---|
| ① | 计划做多大 | **先 S5a + S5b**（Task 69–83）；S5c（84–86）这次不做，另写 | 计划文档「任务拆分」 |
| ② | 画像三张表的冻结 | **解冻 `person_portrait_revisions` + `person_moments`，仍冻结 `person_insight_runs`** | 迁移 `0006_identity.sql`、`frozen.md`、`capability-map.jsonl`（见下「落点判断」） |
| ③ | 全局重聚类 | **搬，作为显式的维护入口**（1.0 的 `recluster_faces` 只在 benchmarks 调用；2.0 做成维护端点或命令，**不自动跑**） | 计划文档 Task 83 之后新增的维护入口一节 |
| ④ | facts 候选来源 | **按 1.0 原样：模型自报**（`analyze_image` 输出里的 `facts` 数组） | 计划文档 Task 82；**并推翻本文件第 30 条**（见下） |

**①的附带记录**：上一轮「本切片 18 个任务 > 约定的 15」这条被裁决成两段，S5a（69–76，
不需要 GPU）与 S5b（77–83，需要 GPU / ONNX）。S5c（84–86，需要 VLM）另写。

**③为什么是「搬」而不是「按证据走」**：证据面（`recluster_faces` 的生产调用者只有
`scripts/benchmarks/` 两个脚本）**没有变**，第 4 条证据仍然成立。裁决改变的是它的**结论**：
不搬的代价是「一册照片导完后，本该合并的簇只能靠用户手点合并，聚类质量差了没有补救手段」，
而这个代价用户不接受。所以 2.0 的做法是**把 1.0 那条只在 benchmark 里的路径，显式做成一个
维护入口**——不是恢复自动调用。**这是明确的用户决定，不是「按证据走」的结果**，
记在这里以免下一次审计拿第 4 条证据来反问为什么搬了。

**②的落点判断（用户把这一条留给我判断，理由如下）**：**两张解冻的表随迁移 0006 一起建**
（而不是留到 S5c 再开 0007）。判据是 0005 那条推迟理由的**性质**：

- 0005 的注释写「参与者与实体都依赖 S5 的 entities，那两张表随 S5 一起建，**不在这里留空壳**」。
  那次推迟**是被悬空的外键目标逼出来的**——`event_participants REFERENCES entities(id)` 而
  `entities` 当时根本不存在，想建也建不了。「不留空壳」是那条约束的**后果**，不是独立的规矩。
- 到 0006，`entities` / `face_clusters` / `events` / `observations` 全在同一文件里，
  **全族的外键目标都闭合了**，没有任何一条表需要推迟。
- 拆开的代价是真的：`person_moments` 引用 `events` / `observations` / `assets` /
  `face_instances`，`person_portrait_revisions` 引用 `entities`——留到 0007 意味着那份迁移要重述
  一堆跨切片的引用，且「建表」与「接功能」挤在同一次提交里，回滚粒度变粗。

**代价如实登记**：S5a 与 S5b 期间这两张表**没有任何写入者**——这正是 S4 栽过两次的形状
（「零调用的纯规则」、「零调用的 `SummarizeEvent`」）。区别写在计划文档里，并且做成了
**一条机械检查**：新模块必须能在生产装配里找到一个调用者，找不到就红。这条检查**只覆盖模块**，
不覆盖表——理由是表没有「能力」语义，它是 schema 声明，而模块有。**如果用户认为这个区分不成立，
处置是把这两张表挪到 S5c 的 0007 再从 0006 删掉**，代价是重跑一次 Task 70 的门禁。

**④推翻了本文件第 30 条。** 第 30 条原文（`s4-deviations.md`）：

> 没有 `detail` 字段）；事实由 `maintain_fact` 规则决定，**不由模型自报（S5）**。

裁决④明确要求 facts 的候选来源**按 1.0 原样由模型自报**，所以第 30 条括号里的
「（S5）」这个记法**作废**。`s4-deviations.md` 第 30 条与第 608/619 行已经按裁决就地改正并
注明依据。**注意这不改变第 30 条的主体**（2.0 的观察器确实不搬 `detail` 的四个字段），
作废的只是它对 `facts` 来源去向的记法。

### 2c.2 三条按证据定下的（上一轮已定，本轮照办）

| # | 事项 | 依据 |
|---|---|---|
| 甲 | 身份向量按实测走 **buffalo_l / `w600k_r50`（512 维）**，不接 AdaFace | 证据 1 / 2 / 3（AdaFace 在 1.0 是死代码，1.0 自己的测试标了 `skip`） |
| 乙 | 全局重聚类**不做自动调用** | 证据 4（调用点全在 benchmarks）。注意：**这条与裁决③不冲突**——裁决③要的是「显式维护入口」，不是「恢复自动调用」 |
| 丙 | `entity` 检索通道**留在 S5b**（Task 83） | `s4-implementation-plan.md:1632` 把 `entity` / `text_ann` 两路记在 S5 / S6；`person_bridge` 的写侧依赖 S5 的 mentions，没有身份层它无从填 |

### 2c.3 新增证据

| # | 事项 | 证据 | 判决 |
|---|---|---|---|
| 40 | **`check_module_map.py` 抓不到重复行，这是 checker 的一个洞** | 见「第 40 条的完整根因」一节 | **报告 + 补检查** |
| 41 | `frozen.md` 与 `capability-map.jsonl` 的冻结清单是**单向校验** | `check_capability_map.py` 只查「map 里 frozen 的条目有没有在 frozen.md 里写理由」，**不查反向**。实测：`frozen.md` 列了「画像修订 person_portrait_revisions」，而 `capability-map.jsonl` 里**根本没有这条条目**（`grep '"frozen"'` 15 条，无画像修订），两种状态都放行。 | **补上条目**（裁决②已解冻它，按 `migrated` / `pending` 记），并登记这个单向性 |
| 42 | `person_moments.run_id` 指向被冻结的表 | 1.0 的 DDL 里 `run_id TEXT REFERENCES person_insight_runs(id)`（`db.py:1148`）。裁决②仍冻结 `person_insight_runs`（不建表），所以 0006 里**这一列取消**——留着它就是一条指向不存在表的外键。 | **取消该列**（已在 `0006_identity.sql` 的注释里写明） |
| 43 | 迁移 0006 是**一次建对**，1.0 的三处运行期 ALTER 不搬 | 1.0 的 `entities.identity_state/role_state/name_state`（`db.py:1240-1243`）、`face_instances.validity`（`:1238`）、`relationships.inverse_predicate`（`:1244-1246`）都是 `_ensure_columns` 事后补的列 | 写进 `CREATE TABLE`（结构性偏离，见证据 22/23） |

#### 第 40 条的完整根因（`check_module_map.py` 为什么放行重复行）

**现象**：真实仓库 `bd3af31` 的 `docs/architecture/module-map.md` 第 66 / 67 行是
**逐字节相同的两行**（`md5sum` 相同），第一列都是
`sentrix/application/retrieval/retrieve_evidence.py`。全表 109 行、108 个唯一路径，
**这是唯一一处重复**（`grep -oE '^\| \`[^\`]+\.py\`' | sort | uniq -d` 只命中它一个）。
`check_module_map.py` 在「有重复」与「删掉重复」两种状态下都报 `0 个问题`。

**来历**（`git log -S` 定位）：`ee690e1`（Task 64）把它从 0 加到 1，
**`9f14c5d`（Task 65）从 1 加到 2**——是一条手工编辑造成的重复，不是生成器的产物。
它随 `f663522` 一路带到 `bd3af31`。隔离副本里 Task 69 重写表格时它掉了（现在 1 行）。

**根因不是「判据不覆盖重复行」，而是「判据的容器把重复行吃掉了」**——两处，各自独立：

1. **文件集合那一侧用的是 `set`。** `documented_paths()` 返回
   `{match.group(1) for line in ...}`，重复路径在集合里**自然只留一份**。而 `main()`
   的比对是 `in_code - in_doc` / `in_doc - in_code`，两个 `set` 做差集——
   **重复行在这个模型里根本不是一个可观测量**，无论多少行都一样。
2. **依赖边那一侧用的是 `dict`。** `documented_edges()` 写
   `deps[path] = parse_cell(...)`，**后一行覆盖前一行**。两条完全相同的行 → 覆盖后结果不变，
   仍然相等。所以 `compare()` 也无话可说。

**要注意的不对称**：这两个容器**不是同样瞎**。如果重复的两行内容**不同**，
第 2 条（`dict` 覆盖）反而会报红——它比的是最后一次覆盖的结果，写错的那一份会被抓。
第 1 条（`set`）则连这都抓不到。**所以这个洞的准确形状是：
「逐字节相同的重复行完全不可见；内容不同的重复行只能被依赖列那一半偶然抓到。」**

**为什么它属于「声称在检查、其实没有」那一族**：`test_module_map.py` 为这个脚本写了
**四条反向测试**（未记录模块、`--src` 传错基准、依赖列写错、行少了列），
唯独**没有一条针对重复行**。而 `check_module_map.py` 的开头注释专门强调
「列数不足的行会被报错而不是跳过：跳过等于给依赖校验开后门——那行写什么都不会红」——
它已经想到了「一行被整行跳过」这一类，**但没想到「两行塌成一行」这一类**。
两者是同一个病的两个变体：**校验模型里不存在的东西，就是没被校验的东西。**

**修法**（已实施，见计划文档）：在 `main()` 的路径比对之前加一条
「表格行数 == 唯一路径数」的断言，并把重复的路径名逐个报出来；同时在
`test_module_map.py` 里加一条**反向测试**（造一份含重复行的表，断言 checker 报红），
保证这条检查自己也有门禁。

### 2c.4 顾虑①的处置：`make ci` 真假红灯判据

限制条件（实测）：153 同机在跑 1.0 的 8091（RSS 11.7 GB）、vLLM 8500、photobench runner、
2.0 的 9598/4274，`make ci` 的 `coverage` 那一步被 SIGKILL 过，e2e 抖动过两次，
而**单独重跑失败文件是绿的**。如果不把判据固化，就会拿假红灯去改代码。

**判据固化成一个脚本** `/home/asus/s5verify/ci.sh`（不写在人脑里），顺序执行：

1. **跑之前先看环境**：1 分钟负载 ≤ 2.0 且 `MemAvailable` ≥ 12 GB，否则**不跑**、
   等窗口（最多 30 分钟）后报 `SKIPPED`。**绝不在高负载下跑**——那种红灯没有信息量。
   实测这条真的会挡下来：Task 70 的一次调用就打印了
   `### 环境不满足（load1=2.19 需<=2.0 ...），等 60s` 然后才跑。
2. 跑 `make ci`，**原始输出整份落在 `logs/ci-<时间戳>-run<N>.log`**，跑几次留几次。
3. 绿 → `CI_OK`。
4. 红且日志里有 `已杀死 / Killed / SIGKILL` → 判**疑似假红**（`CI_KILLED`），
   重新等窗口后重跑，最多 2 次；两次都带这个指纹才按真红处理。
5. 红且是测试失败 → **先单独重跑那些失败文件**。单跑绿 → `CI_FLAKE`，整跑重试；
   单跑也红 → 真红，必须修。
6. **任何情况下都不在「红灯未过第 4/5 条」时改代码。**

这条协议自己也被实测过一次：Task 70 的第一次 `make ci` 是 `CI_RED_UNKNOWN`，
日志尾部是 `make: *** [Makefile:22：lint] 错误 1` 加 4 条 ruff 报错（3 条 E501 + 1 条 SIM117）
——**是真红，且判据正确地没有把它误判成抖动**（它没有 SIGKILL 指纹，也没有测试失败可单跑）。

## 2d. Task 70 / 71 实施期实测到的偏离与发现

两颗提交（`b18c571` Task 70、`67030f0` Task 71）都在隔离副本里落地，`make ci` 全绿。
带出六条要登记的东西。

| # | 事项 | 判决 |
|---|---|---|
| 44 | **两张解冻的表在 S5a / S5b 期间没有写入者**。`person_moments` 与 `person_portrait_revisions` 随 `0006_identity.sql` 建了出来，而它们的写入者在 S5c（Task 85 / 86）。**这正是 S4 栽过两次的形状**（「零调用的纯规则」「零调用的 `SummarizeEvent`」）。 | **保留**，代价与判据见 §2c.1「②的落点判断」。**区别在于：这条机械检查只覆盖模块，不覆盖表**——新增的 `scripts/check_reachability.py` 会拦住「模块没有生产调用者」，但一张没人写的表它看不见。把这个不对称写在这里，不假装它不存在。**用户若认为这个区分不成立，处置是把两张表挪到 S5c 的 0007 并从 0006 删掉**（代价：重跑一次 Task 70 的门禁） |
| 45 | **`link_confirmed_mention` 把一条规则从调用点下沉进了仓储**：1.0 在调用点用 `if entity.get("status") == "confirmed"` 挡「只有已确认的实体才写提及」，2.0 挡在仓储里并改成抛 `ValueError`（`repositories/entities.py:494-498`）。 | **改**（Task 71 唯一一处结构偏离）。理由写在代码注释里：这条规则一旦漏判，观察的人物快照里就会混进一个聚类猜出来的候选人，而它是 `person_score` 与 `person_bridge` 的输入——**一处漏判，后面全是事实**。1.0 的调用点从不违反它，所以生产路径上行为等价；变化的是漏判的代价从「静默污染」变成「一眼可见」。**风险如实记**：规则换了执行位置，Task 72/73 的用例必须各有一条「非 confirmed 不写提及」的断言，否则这条防线在调用侧就没有门禁 |
| 46 | **`maintain_entity_property_values`（多值合并）不移植** | 1.0 的调用点全在语义实体抽取（`db.py:3840-4032` 的 atmosphere / `semantic_details`），那是 S7；本切片没有调用者，收进来就是「零调用的能力」。理由写进了 `repositories/entities.py` 的模块 docstring |
| 47 | `list_clusters` 沿用 1.0 的 `INNER JOIN memory_spaces` | **照搬，不改**。后果是：scope 在 `memory_spaces` 里没有行时，该 scope 的簇**一条都查不到**（不是查全量）。这是 1.0 的实际行为，测试的 fixture 因此必须建 `memory_spaces` 行。改成 `LEFT JOIN` 会让「没登记的空间」与「没有簇」这两个状态混起来 |
| 48 | **Task 70 / 71 的红灯 A 都是收集期错误，不合格** | 建新模块时 `ModuleNotFoundError` 是 TDD 的天然红灯，但它**只证明「测试先于实现落地」，不证明任何行为被钉住**。**处置：Task 72 起每个任务在提交后追加红灯 B（定点变异）**——把实现里的要害那一行改掉、重跑门禁，让红灯**指向具体的断言**。这条纪律已写进计划文档的「逐任务定义」开头 |
| 49 | `get_face_instance` 带出四个 1.0 也带、但本切片暂时用不上的字段（`asset_path` / `file_name` / `mime_type` / `media_type`） | **保留**。它们来自 1.0 的同一条 SQL，Task 75 的裁剪端点要 `asset_path`；删掉另外三个反而要多写一行注释解释「为什么单独摘掉」。**不是投机**——是「照抄一条已经存在的查询」与「按当前需要裁剪它」之间的选择，这里选前者 |

**另外两条如实记录**：

- **Task 70 的红灯 / 绿灯原文在计划文档里是后补的**。落地当时跑过（10 条全红 → 10 条全绿），
  但原始日志在重跑时被覆盖掉了；文档里贴的是一次**刻意复现**的原文
  （把 `0006_identity.sql` 临时移走再跑），不是落地当时那一跑的字节。**这一点必须写明**，
  因为「红灯原文」的可信度全在它是不是当时那一跑。Task 71 起日志一次一存，不再有这个问题。
- **Task 71 的 19 条门禁里没有一条是只断言「不抛异常」的**：每一条都断言了具体的
  字段值、行数或异常类型（含一条 `ValueError` 与两条 `IntegrityError` 语义的对照）。

## 2e. Task 72 / 73 实施期实测到的偏离与发现

两颗提交（`3e469ab` Task 72、`5c30554` Task 73）落地，`make ci` 全绿，红灯 B 有原文。
带出五条，其中**第一条与第二条推翻了本计划自己的写法**。

| # | 事项 | 判决 |
|---|---|---|
| 50 | **计划的「全部四件都写 `entity_revisions`」是错的。** 实读 1.0 `db.py:5152-5193` 的 `confirm_face_cluster`：它**不调** `_record_entity_revision`。写审计的只有三处——`_merge_cluster_into_person`（`user_name_merge`，同名合并那一路）、`merge_face_clusters`（`user_merge`，默认参数写在签名 `db.py:4776`）、`split_face_instance`（`user_split`，`db.py:4797`）。**reject 不可能写**——它删的正是那个实体的 `entity_revisions` 行。 | **按 1.0 实现**（Task 73 没有自造第四条修订）。计划文档那一行是**规划期的错误**，已在 Task 73 一节改正。若产品真要「每次确认都留一条 `user_confirm` 修订」，那是一处**有意的偏离**，需要用户点头 + 同时改这里 |
| 51 | **一处从 1.0 继承来的地雷（探针实测，未改）**：对一个**已经 confirmed** 的簇，用**同名**再确认一次，会走同名合并分支，而目标实体就是它自己 → `clu_1.status = 'rejected'`（`member_count` 随后被 refresh 写回 1）。根因在 `db.py:5157-5159`：`existing = self.find_confirmed_person_by_name(scope_id, name)` 先查、命中即合并，**没有排除「命中的就是本簇的实体」**。1.0 同样如此。 | **~~照搬 + 登记，未改~~ 已按用户裁决①修掉（Task 74，`11ab962`）**。改法是目标命中本簇时短路。原文的裁决记录与红灯证据见 §2f。 |
| 52 | **异常带 `status_code` 是一个新约定**：application 层不许 import `api/errors`（`application-purity` 契约），所以 400/404/409 定在 `review_cluster.py` 的三个异常类上（`InvalidReviewRequestError` / `ClusterNotFoundError` / `ReviewConflictError`）。 | **登记 + 待用户确认统一口径**。现有的 `TurnNotFoundError` 不带码、由路由硬编码 404；两种做法都不算错，但**同一个仓库里两种并存会让「状态码从哪来」变成看运气**。Task 75 落地前需要定一个 |
| 53 | **`delete_candidate` 比 1.0 多删三处**：`entity_observations` / `event_participants` / `event_entities`（外加 0006 里已建、S5c 才写的 `person_moments` / `person_appearance_evidence`）。 | **改，理由不是「顺手帮 Task 81 干活」**：2.0 每次连接都 `PRAGMA foreign_keys = ON`（`database.py` 的 `connect()`），1.0 没开。少删一行，某天这条 DELETE 会整体失败。**投影刷新仍留给 Task 81**——删干净与重建是两件事 |
| 54 | **`check_reachability` 的判据有假阴性**：第一版把「所有 `ast.Name` 载入名」与该模块公开符号求交，于是别处一个**形参**叫 `pose_bucket`，就让 `domain/identity/faces.py` 的同名公开函数被当成「已经用过」，模块被判成已接生产；而 `PENDING_WIRING` 的反向规则还会因此报错要求删掉一条本来正确的登记。同族还有常量名撞车（`VISUAL_SPACE`）。 | **收紧判据**（`ee2ddad`）：改成「这个名字是**从那个模块 import 进来的**，而且被读用过」——名字空间由导入关系给出，不由拼写给出。**并补两条反向测试**（形参撞名必须仍然报红 / `from ... import` 后用过的必须算数），否则收紧后的判据自己又变成一条没人验的规则。**这一条值得单独记**：它是「声称在检查、其实没有」那一族的又一个实例，而且是**实施期被撞出来的**，不是审出来的 |

**红灯 B（定点变异）实测结果**——每个任务挑 3 条要害行为，改掉实现里那一行，重跑门禁，
让红灯**指向具体断言**，然后改回：

| 任务 | 变异 | 红的那条用例 |
|---|---|---|
| 72 | `quality < MINIMUM_MATCH_QUALITY` → `<=` | `test_quality_exactly_at_the_gate_still_matches`（`assert 'cluster_5299834c33b2' == 'clu_home'`） |
| 72 | 资格闸门去掉 `validity == "verified"` 这一半 | `test_a_face_that_is_not_identity_eligible_is_stored_without_a_cluster`、`test_an_ineligible_face_still_writes_its_vector_without_a_cluster`（2 failed） |
| 72 | 删掉签名比对整段 | `test_a_cluster_whose_prototypes_carry_another_signature_is_skipped` |
| 73 | `confidence = MAX(confidence, 1)` → `confidence = confidence` | `test_confirm_marks_the_person_and_the_cluster`（`assert 0.4 == 1.0`） |
| 73 | reject 的判据 `== CONFIRMED` → `!=` | 三条（含仓储兜底的 `ValueError: confirmed person entities cannot be deleted`） |
| 73 | 拆分的实例查找 `find_instance(cluster_id, fiid)` → `get_instance(fiid)` | `test_split_of_an_instance_from_another_cluster_is_a_404`（`DID NOT RAISE`） |

**这六条才是「行为真的被钉住」的证据**——红灯 A（收集期 `ModuleNotFoundError`）只证明
「测试先于实现落地」，不证明任何一条断言有牙齿。原文在
`/home/asus/s5verify/logs/task72-redB.txt` 与 `task73-redB.txt`。

**另外一条实测到的环境事实**：Task 73 的第一次 `make ci` 触发了**真实的 SIGKILL 假红**
（`make: *** [Makefile:56: coverage] 已杀死`），`ci.sh` 按判据重跑 run1 后全绿。
两段原始输出都留在 `logs/ci-20260923-122113-run0.log` 与 `-run1.log`。
**这是顾虑①那条判据第一次在真实工作里救下一次误判**——不重跑就会拿假红灯去改代码。

---

## 2f. Task 74 实施期实测到的偏离与发现（含用户裁决①的落地）

`11ab962`（Task 74）落地，`make ci` 全绿（869 passed / 16 skipped / coverage 97.69%），
红灯 B 有原文。

### 第 51 条的处置：修掉，并且这一次根因比规划期写得更硬

规划期只说了「1.0 同样如此，照搬 + 登记」。实施期把 1.0 的两条同名确认路径并排读，
发现**这条守卫是 1.0 自己写过的，只是漏了一处**：

| 1.0 位置 | 同名查询命中后 | 有没有排除「命中的就是本簇/本人」 |
|---|---|---|
| `db.py:5157` `confirm_face_cluster`（簇入口） | 直接 `_merge_cluster_into_person` | **没有** ← 缺陷 |
| `db.py:5251` `confirm_person_entity`（人入口） | `if existing and existing["entity_id"] != entity_id:` | **有** |

所以修复不是「给 1.0 加一条新判据」，而是**把 1.0 自己写在人入口的那条判据补到簇入口**
（`review_cluster.py:138`）。这个根因改变了这条修复的性质：它不再是一个产品行为的选择，
而是**同一份代码里两处写法不一致**——按用户的判断修掉它是恢复一致性，不是改变设计。

**缺陷的实际后果**（定点变异实测原文）：对一个已确认的簇用同名再确认，返回的
`ConfirmResult.merged_into` 是**它自己**（`assert 'ent_1' is None` 失败），
簇被置成 `rejected`、`member_count = 0`——**这个人的脸全部从人物视图里消失**，
而且界面看到的是「这个簇没了」，没有任何一处日志或审计能指出发生了什么。

**三条测试钉住它**（`test_review_cluster.py`），缺一条都不够：

| 用例 | 钉的是什么 |
|---|---|
| `test_reconfirming_a_cluster_with_its_own_name_keeps_it` | 精确名自命中：簇保持 confirmed、`member_count` 不变、不留「并入了自己」的审计 |
| `test_reconfirming_through_one_of_its_own_aliases_keeps_it` | 别名自命中——走的是 `find_confirmed_person_by_name` 的**第二段**（扫 aliases），与精确名是两条不同的路径 |
| `test_a_different_cluster_with_the_same_name_still_merges` | **反向**：守卫只排除本簇自己的实体，别人的同名簇照样并。没有这一条，修复可以被实现成「同名一律不合并」而测试仍然全绿——那等于把同名合并整个功能关掉 |

**红灯 B 原文**：把 `if existing is not None and existing.entity_id != cluster.entity_id:`
改回 `if existing is not None:` → 前两条红（`assert 'ent_1' is None`）、第三条仍绿。

### 第 55 条 · `make ci` 在 153 上被 SIGKILL 的**真实根因**：glibc arena 膨胀

顾虑①原来只把「coverage 会被 OOM」当成环境噪声并固化成重跑判据。实施期读到了内核日志，
根因是可复现、可修的：

```
oom-kill:constraint=CONSTRAINT_NONE,nodemask=(null),global_oom,...task=pytest,pid=148543
Out of memory: Killed process 148543 (pytest) total-vm:39128280kB, anon-rss:18174300kB
```

**pytest 自己涨到 18 GB**（全机 61 GB，同期 1.0 的 8091 + llama-server + cursor-server
已占 43 GB，swap 打满）。同样的测试在同一台机器上**限住 glibc arena 后峰值只有 10.1 GB**：

```
MALLOC_ARENA_MAX=2 OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 .venv/bin/pytest
→ 846 passed, 16 skipped in 124.64s
   Maximum resident set size (kbytes): 10146904
```

torch / onnxruntime 会按核数开线程，每个线程一个 64 MiB 的 glibc arena，**默认不限**，
于是多线程测试进程的 RSS 随线程数线性膨胀，与测试内容无关。所以：

- 顾虑①的重跑判据**仍然有效且必须保留**（真实的内存竞争还会来自别的服务）；
- 但 `make ci` 在 153 上应当**默认带 `MALLOC_ARENA_MAX=2`**，把假红的来源去掉，
  而不是每次都靠重跑消化它。这是环境配置，不是代码问题。

### 第 56 条 · `onnxruntime-gpu` 的 CUDA EP 需要**先 `import torch`**，否则静默退化成 CPU

依赖试装（S5b 的闸门）实测：装了 `onnxruntime-gpu==1.30.0` 之后
`ort.get_available_providers()` 里有 `CUDAExecutionProvider`，但**真建 session 时失败**：

```
Failed to load library .../onnxruntime/capi/libonnxruntime_providers_cuda.so with error:
libcublasLt.so.13: cannot open shared object file: No such file or directory
Failed to create CUDAExecutionProvider. Require cuDNN 9.* and CUDA 13.*.
```

而且 **`InferenceSession` 不报错**——它退回 CPU 并正常返回结果。这是「看起来在跑、
其实在 CPU 上跑」那一族的又一例：`get_available_providers()` 只说明 EP 编译进去了，
**不说明运行期能加载**，必须真建一个 session 并读 `sess.get_providers()` 才算验过。

根因与修法：venv 里 cu13 的 `libcublasLt.so.13` 在
`.venv/lib/python3.11/site-packages/nvidia/cu13/lib/`，不在动态加载路径上。而 **torch 启动时
会把它们预载进进程**，所以：

| 做法 | 结果 |
|---|---|
| 不设 `LD_LIBRARY_PATH`，先 `import onnxruntime` 再建 session | **退化成 CPU** |
| 不设 `LD_LIBRARY_PATH`，**先 `import torch`** 再建 session | `['CUDAExecutionProvider', 'CPUExecutionProvider']` ✔ |
| 设 `LD_LIBRARY_PATH` 指向 `nvidia/cu13/lib` 等 | ✔（但要靠启动环境，1.0 不这么做） |

Task 77 的 `onnx_runtime.py` 因此**必须自己负责预载**（不能依赖启动脚本设环境变量，
也不能依赖「调用方恰好先 import 过 torch」——那是隐式顺序依赖，正是本仓库一路在防的东西）。

### 第 57 条 · 第 52 条（`status_code` 口径统一）本轮**没有做**

裁决③要求把异常 `status_code` 口径统一到「领域错误带码、路由只映射」，这会动 S3 的
`TurnNotFoundError`（目前不带码、由路由硬编码 404），属跨切片改动。Task 74 不碰它，
留给 Task 75（路由落地时一并改）——**它仍然是一条未结的账**。

### 第 58 条 · 裁决②（两张解冻的表）——**【2026-09-24 订正】原文与迁移直接矛盾**

> **订正说明。** 本条原文写的是「`person_portrait_revisions` / `person_moments`
> **不在 0006 里建**，计划 Task 85/86 才落地」。**这句是错的**，且与它同一轮落地的迁移
> 直接矛盾：`0006_identity.sql:238` 建了 `person_moments`、`:267` 建了
> `person_portrait_revisions`（0006 自己的文件头注释 `:10` 也写着「S5c 才写」，指的是
> **写入者**在 S5c，不是**建表**在 S5c）。订正是把句子改成实际情况，不是改行为：
> 两张表当时**确实建了**，只是没有写入者。

裁决②的真实内容与落点：

- **解冻** `person_moments` + `person_portrait_revisions`，**仍冻结**
  `person_insight_runs`。迁移 0006 **建了**前两张、**没有建**第三张；
  `person_moments.run_id` 这一列随 `person_insight_runs` 一起取消
  （1.0 那列的外键指向那张被冻结的表）。
- 两张表在 S5a/S5b 期间**有表无写入者**——写入者在 S5c（原计划 Task 85/86，
  本轮改为 Task 86）。S5a/S5b 的机械门禁查的是「模块有没有生产调用者」
  （`scripts/check_reachability.py`），**它看不见一张没人写的表**；这个不对称已登记在
  本文件第 44 条。
- `frozen.md` 与 `capability-map.jsonl` 的相应改记随 Task 70 落地。

---

## 2g. Task 77 实施期实测到的偏离与发现（真权重 + 53 张真照片）

`9193a71`（Task 77）落地，`make ci` 全绿（898 passed / 16 skipped / coverage 97.69%），
红灯 B 四条有原文。

**真数据实测**（`album3-v2-source/photos` 53 张，RetinaFace-R50 分块）：
检出 **142 张脸**、**46 张照片有人脸**、单张最多 **19 张**、7 张无人脸；
session 的 provider 链是 `['CUDAExecutionProvider', 'CPUExecutionProvider']`——**CUDA
真的生效了**（这是「先 import torch 预载」那条修法在真链路上的验证）。显存没被撑爆：
单 session 预算 256 MiB + 并发闸门默认 1。

### 第 59 条 · `detect()` 返回的是**候选**不是人脸，而且 RetinaFace 的关键点过不了几何判据

两件事都是实测出来的，都不是推测：

1. **细pass 的入选阈值是 0.1。** 实测 `2017-11-05 151427.jpg` 上有一张脸的置信度是
   **0.282**。所以调用方**不能**按粗pass的 0.3 过滤——那会把细pass的收获整个丢掉，
   而细pass正是为「粗pass全军覆没」准备的。
2. **低分 anchor 的偏移量可以解出任意离谱的框。** 实测 `2017-11-05 151451.jpg`
   （**11394×2944 的全景**）上有一个候选：框 `[9730, 1680, 10490, 2548]` 落在图内，
   而它的 5 个关键点整批落在 `x ≈ -400..-60`——框与关键点互相矛盾。
3. 推论：**RetinaFace 自己的关键点在 `landmark_sanity` 下一个都过不了。** 实测前 6 张
   真照片里，所有 ≥64px 的图内候选**全部被拒**（`rejected > 0 且 passed == 0`）。
   这正是 1.0 `model_clients.py:1788` 那句注释的实测依据：

   > Use SCRFD's own (reliable) landmarks, not RetinaFace's, whose geometry is
   > unreliable even on large faces.

   **所以 Task 78 的「用 SCRFD 的关键点替换掉 RetinaFace 的」是必要步骤，不是优化。**
   少了它，每一张脸都会停在 `face_validity='uncertain'`、`identity_eligible` 恒假，
   一张脸也进不了簇。

**判决：照搬 1.0，不在检测层裁剪或修关键点。** 1.0 靠 `_expand_crop` 与 SCRFD 复核两道
下游防线丢这些候选，2.0 保持同一分层。契约写在 `detect()` 的返回值说明里，由 Task 78 的
适配器负责筛。**在这一层加裁剪会改变与 1.0 的可比性**，所以没有做。

### 第 60 条 · 一次**没变红的变异**暴露了测试自己的洞

第一版 `test_nms_threshold_of_one_keeps_everything` 用阈值 `1.0` 去测「`iou <= threshold`
是含等号的上界」。变异（`<=` → `<`）**没有变红**——因为那一对框的 IoU 只有 0.68，
`<= 1.0` 与 `< 1.0` 结果完全一样。用例看起来在钉边界，**其实一次都没走到边界上**。

改成 IoU **精确等于**阈值的构造才真的钉住：A `[0,0,10,10]`（面积 100）与
B `[0,0,5,10]`（面积 50），并集 100、交集 50，IoU = 0.5 在二进制浮点下**精确**。

**同一条族里的第二个洞**：变异脚本用 `sed` 改写实现，而 `sed` 不匹配时**静默成功**——
那会把「红灯」变成一次没人验的空操作，与 Task 72 那次的 `check_reachability` 假阴性同类。
脚本因此加了 `apply()`：变异后先 `grep` 确认那一行真的变成了变异后的形态，否则中止。

这两条合起来是一句话：**变异测试本身也要有反向门禁**——没变红的变异要先怀疑变异没落上去
或者用例没走到边界，而不是直接宣布「这个实现点不重要」。

---

## 2h. Task 78 实施期实测到的偏离与发现（真链路第一次跑通时撞出来的六个坑）

``c0518e6``（Task 78）落地，`make ci` 全绿（`920 passed / 16 skipped / coverage 98%`）。这一节的六条里，**前四条全是
「不报错、只是结果不对」那一族**——没有一条会让调用方看到异常，全部是跑真权重 + 真照片时
撞出来的；它们没有一条能靠读 1.0 的源码发现，因为 1.0 的代码在同样的位置也是这么写的。

| # | 事项 | 判决 / 处置 |
|---|---|---|
| 61 | **`FACE_VALIDITY_VERIFIED` / `FACE_VALIDITY_UNCERTAIN` 落在 `domain/identity/models.py`**，不在适配器里。计划与 1.0 都把这两个串写成适配器里的裸字面量。 | **改**。理由是 Task 79 的 seed 用例也要写同一个值，而 `application-purity` 契约不许 application 层 import infrastructure——常量留在适配器里，用例就只能再抄一遍字面量，那正是「一个值散落三处」的开头 |
| 62 | **`FACE_MODEL_ROOT` 在 1.0 是两个意思。** `FaceAnalysis(root=...)` 收的是 insightface 的**根**（模型在 `<root>/models/<name>/`），而 `_load_buffalo_recognition` 把同一个变量直接当**模型目录**用；两者只在默认值下碰巧一致（`~/.insightface` vs `~/.insightface/models`）。**实测代价**：把模型目录当 root 传进去，insightface 找不到模型会**去 GitHub 下载**——进程挂在一条到 github.com 的 HTTPS 连接上，`utime` 只有 1.55 秒、`pytest` 一个字都不打，直到超时。第一次真跑就是这么卡住的。 | **改**：`FACE_MODEL_ROOT` 只保留一个意思（insightface 根），模型目录由 `MODELS_SUBDIR` 派生；并在 `_build_analysis` 加一条 **fail-fast**（模型目录不存在直接抛）。「静默联网挂死」于是变成一条 `error` 字段里的降级 |
| 63 | **insightface 的 `providers` 必须是裸字符串列表**，与 `provider_options` 是两个**平行**列表。给它 onnxruntime 那种 `(name, options)` 元组，它打一行 `EP Error ... Falling back to ['CPUExecutionProvider']` 然后**在 CPU 上把整条链路跑完**。 | **改**（`provider_chain(...)` + `face_onnx_provider_options(...)`）。这是第 56 条「静默退化成 CPU」的同族第二例：上一例是运行库没预载，这一例是入参形态不对，**两例的观测点都是 `session.get_providers()`** |
| 64 | **第三级（整图兜底）接活**——处置证据 8 那条死路径：1.0 调的是恒为 `None` 的 `identity_adapter`，`AttributeError` 被裸 `except` 吞掉，永远返回 `[]`。2.0 把它接在真实的识别 session 上。**照搬的四个常数（置信度 0.99 / 质量 0.8 / 面积比 1.0 / frontal）从来没有被任何一张真照片检验过**——它们随死路径一起躺了不知道多久。 | **改 + 如实登记**：常数原样照搬（这样以后想调的人有一个可比的对象），但它们**不是实测值**，注释里写明了 |
| 65 | **`detect()` 在权重缺席时直接返回空表**（1.0 判的是 `FACE_ENABLED`）。 | **改**。缺权重是允许的部署形态，不是异常；判 `available` 让「这台机器没有人脸能力」与「这张图没有脸」在调用方是两件事 |
| 66 | **真权重那层用例的取样从 6 张改成 10 张。** 实测：这一册相册**前 6 张（远景合影）一张 ≥64px 的脸都没有**，取 6 张会得到「一张脸都没有」——用例红了，而红的是**测试的假设**不是代码。 | **改**，并把「这一批里必须有脸」写成一条显式的失败信息（同 `test_face_detector.py` 那条全景断言）。**这是 S4「只断言数量」那条教训的另一面**：数量写死会随数据变红，完全不写又会漏掉「一张脸都没有」这种真故障 |

### 真数据实测（2026-09-23，整册 53 张，与 Task 77 用的是同一批）

| 量 | 实测 |
|---|---|
| RetinaFace 候选 | **315** |
| 过 `FACE_MIN_SIZE=64` 的候选 | **117** |
| 适配器输出的人脸（每张都带 512 维向量） | **117** |
| 其中 `verified` | **30** |
| 有候选的照片 | 46 / 53 |
| 适配器有输出的照片 | **32 / 53** |
| 整册耗时 | 69 秒（一次加载 session） |

provider 链实测是 `['CUDAExecutionProvider', 'CPUExecutionProvider']`——第 62/63 两条修完之后，
检测与识别**都真的在 GPU 上**。

**`FACE_MIN_SIZE=64` 是这条链路上真正的瓶颈，而它是照搬来的**：1.0 的部署脚本
（`start_sentrix_api_8091.sh`）里没有设这个变量，所以默认值 64 一直在生效。一册远景合影
里 315 个候选有 198 个小于 64px，全被丢掉。**这一条不改**——它属于「多小的脸才算一个人」
的**产品口径**，不是 bug；但「检出了多少张脸」这个数字对它是敏感的，所以记在这里。

### 两条判定差一个量级（第 59 条的验收）

真照片上 **117 张脸带向量、只有 30 张 `verified`**。所以「有向量」与「够格进簇」必须分成
两条断言：Task 77 那条 `test_retinaface_landmarks_do_not_survive_the_geometry_gate` 钉的是
「照搬 RetinaFace 的关键点会全灭」，Task 78 的
`test_scrfd_landmarks_are_what_lets_a_real_face_be_verified` 钉的是「换成 SCRFD 之后真的有
30 张活下来」——**两条合起来才构成「替换是必要的、而且有效」**。

### 红灯 B（定点变异）实测结果

每个任务挑要害行为改掉实现里那一行、重跑门禁，让红灯**指向具体的断言**，然后改回。
脚本带 `apply()`：变异后先确认那一行真的变成了变异后的形态，否则中止
（`sed` 不匹配时静默成功，会把红灯变成一次没人验的空操作——第 60 条的教训）。

| 变异 | 红的那条用例 |
|---|---|
| 分数闸门 `score >= 0.7` → `score > 0.7` | `test_the_score_gate_is_seventy_hundredths` |
| 资格闸门去掉质量那一半（`... and quality >= min_quality`） | `test_the_quality_gate_is_what_separates_verified_from_eligible` |
| 面积闸门整条删掉（`... and area_ratio >= 0.003`） | `test_the_area_gate_is_three_thousandths` |
| 放大重试的框不换算回原图（`value / scale` → `value`） | `test_an_identity_photo_falls_back_to_an_upscaled_retry` |

原文在 `/home/asus/s5verify/logs/task78-redB.txt`。

---

## 2i. Task 79 实施期实测到的偏离与发现（导入路径接线 + 身份预置）

``d328878``（Task 79）落地，`make ci` 全绿（`928 passed / 16 skipped / coverage 98%`）。这一节记四条**结构性**偏离
（计划的文件清单与实施时落点不一致，逐条写清为什么）与一条**杀不掉的变异**。

### 计划 vs 实施：文件清单的四处偏离

| # | 计划写的 | 实际落的 | 为什么 |
|---|---|---|---|
| 67 | `application/media/import_album.py`【改】 | `application/media/import_assets.py`【改】 + `api/routes/media.py`【改】 | **顺序约束在 `ImportAssets.execute` 里**，不在 `ImportAlbum` 里——后者只按文件循环调前者（``ImportAlbum.execute`` 的循环体只有一行 ``self._importer.execute(...)``）。人脸登记必须插在「写观察」与「事件归并」之间，那两件事都在 `ImportAssets` 里；改 `import_album.py` 改不出这个顺序。接线点仍在 `build_import_assets`（单张上传与整册导入共用同一处，这是它的原话） |
| 68 | 三级降级落在 `application/identity/seed_identity.py` | 落在 `infrastructure/models/face_adapter.py` 的 `detect_identity_photo` | 后两级要**解码 / 缩放 / 重编码图片**（`application-purity` 契约禁掉 PIL），第三级还要**识别 session**（那是适配器私有状态）。1.0 能把它写在 `app.py` 里，是因为 `pipeline.face.identity_adapter` 当时是全局可达的；2.0 没有那个全局。用例（`SeedIdentity`）只负责「拿这些脸去建什么」 |
| 69 | 未提 `domain/identity/ports.py` | 新增 `FaceDetector` 端口 | 容器字段**必须按 domain 端口声明**（`test_container_wiring.py` 强制），而人脸适配器此前没有任何端口。两个入口（`detect` / `detect_identity_photo`）的差别写在端口 docstring 里：**不是两种能力，是一套能力的两档召回策略** |
| 70 | 未提 `test_face_embedding.py` | 三级降级的六条用例加在**适配器自己的**用例文件里 | 它们是适配器的行为，不是用例的行为。放进 `test_import_identity.py` 就要在那里重建一整套假件（StubFace / StubAnalysis / 假 session），而 `test_face_embedding.py` 里已经有现成的、且**判据表本身就是用它验的** |

### 第 71 条 · 一条杀不掉的变异：`SeedIdentity` 选出的「最好的那张脸」在终态上不可观测

红灯 B 的第三次变异（`max(chosen, key=…)` → `min(chosen, key=…)`，即代表向量改取**质量最低**
的那张）**没有变红**。按纪律先怀疑「变异没落上去」与「用例没走到边界」，逐条排查：

- 变异**确实落上去了**（`apply()` 的 `assert count == 1` 通过，且事后 `diff` 确认文件被改写）；
- 用例**确实跑到了**那一行（临时插 `print` 实测，两条路径都执行）；
- 排除了 `__pycache__` 陈旧的假说（删掉 `__pycache__` 重跑，结果不变）。

**根因**：`FaceRepository.refresh_prototypes` 在重算原型时**顺带把代表向量也重写了**
（`repositories/faces.py:389-400`，取当前实例里质量最高的那一张）。而 `SeedIdentity` 落实例时
写的 `quality` 就是它选代表时的那个键——**两条路径用的是同一条规则，所以终态永远一致**，
`create_cluster` 那一刻传进去的向量在两行之后就被覆盖了。

**判决：照搬 1.0，不改，如实登记。** 1.0 的 `seed_person_identity` 同样是「先按质量选出代表
向量写进簇，再 `_refresh_face_prototypes`」；去掉前一步会让 2.0 与 1.0 在「簇建出来到刷新
原型之间」有一个可观测的中间态差异，而那个窗口在单线程用例里根本不存在。
**代价如实记**：这条选脸逻辑没有任何门禁用例能钉住它，它是一行**等价但不可观测**的代码。

### 第 72 条 · 预置出来的簇 `revision` 是 1，1.0 是 0

1.0 用一条 `INSERT ... status='confirmed'` 直接把簇建成已确认（`revision` 取 DDL 默认 0）。
2.0 走 `create_cluster` → `confirm_cluster`，后者带 `revision = revision + 1`。
**终态的语义一致**（都是「一个已确认的、挂着人的簇」），差的是那一个计数器。
不为了对齐它去改 `confirm_cluster`——那个 `+1` 是簇审阅路径（Task 73）真实需要的修订号，
为预置这一条路径把它去掉，等于让审阅路径少一次修订。

### 第 73 条 · 预置实例的 `validity` 写死 `uncertain`

1.0 的 `seed_person_identity` 那条 INSERT **不含 `validity` 列**，落的是 DDL 默认值
`'uncertain'`（`db.py:1238` 的 `_ensure_columns`）。2.0 的仓储 `add_instance` 要求显式传，
所以这里**显式传 `uncertain`**——不是为了「更安全」，是为了**与 1.0 的落库结果一致**：
预置走的是「用户说这是谁」，不是 SCRFD 复核，两种证据强度不该在同一个字段上抹平。
**这一条有用例钉住**（`test_seeded_instances_carry_the_uncertain_validity`）。

### 第 74 条 · 导入路径的对照用例断言的是「内容」不是「调用序」

`test_a_matching_face_is_already_a_mention_when_the_event_is_decided` 里，事件归并那一步
**去库里查 `entity_mentions`**，断言查到 1 行——而不是断言「`faces` 排在 `events` 前面」。
两种写法在正常实现下都绿，但把顺序倒过来时：前者红在「这一刻还没有提及」（这正是
`person_score` 恒 0 的成因），后者只红在「列表顺序不对」。**要钉的是后果，不是顺序本身**。
（顺序也顺带断了一条 `order == ["faces", "events"]`，作为失败时的第一手线索。）

---

## 3. 这份文档没有做的事（如实记录）

1. **没有跑 1.0 的测试套件。** 所有 1.0 的行为都是从**源码 + 行号**读出来的，
   只有第 3 条（兜底画像）做了实跑验证。S5 执行时每个任务的门禁才真正把这些行为钉住。
2. **没有查运行中进程的实际环境**（`/proc/<pid>/environ` 在这一轮里读不出内容，
   `sudo` 需要密码）。所以 `SENTRIX_PERSON_INSIGHT_SCOPES` 的「默认关闭」是**从代码默认值推断的**，
   不是从 153 的部署实况确认的。这一条在第 20 号证据里已经标明。
3. **Task 71–83 尚未实施完。** 阶段 3–5（逐任务红/绿、跨任务回归、每 commit `make ci`）
   到目前为止对 **Task 69 与 Task 70** 执行完毕，其余按计划继续进行；每个落地任务的
   红灯 / 绿灯 / `make ci` 原始输出都在计划文档的验证记录里。**没有实施的任务不写推测的
   2.0 代码块**——S2 的教训是「13–14% 的错误率全部发生在计划生成这一步」。
4. **没有碰 1.0 仓库的任何文件**，没有重启 8091 / 8100 / 8101 / 8500 / 11434 / 9598 / 4274。
5. **没有决定任何一条「待裁决」**——7 条都原样列在计划文档里。

---

## 4. 与 1.0 逐条对回的位置索引

| 2.0 落点 | 1.0 来源 |
|---|---|
| `domain/identity/faces.py` | `backend/face_clustering.py:12-147`、`backend/face_embeddings.py:19-24,224-244` |
| `domain/identity/models.py` | `backend/db.py:560-699` 的表语义 |
| `domain/identity/appearance.py` | `backend/person_appearance.py:4-25` |
| `domain/identity/portrait.py` | `backend/person_portraits.py:14-181` |
| `domain/identity/insights.py` | `backend/person_insights.py:31-111` |
| `domain/spaces/models.py` / `ports.py` | 1.0 无（设计 §2.3） |
| `application/identity/enroll_face.py` | `backend/db.py:4815-4911` |
| `application/identity/review_cluster.py` | `backend/db.py:4776-4797,5152-5245,5290-5341,5579-5589` |
| `application/identity/name_person.py` | `backend/db.py:4399-4466` |
| `application/identity/seed_identity.py` | `backend/app.py:2253-2310`、`backend/db.py:5343-5396` |
| `application/identity/person_directory.py` | `backend/agent_runtime/tools.py` 的人物工具（1.0 无独立端口） |
| `application/identity/run_insights.py` | `backend/person_insights.py:159-253` |
| `application/memories/assign_events.py` | `backend/db.py:2093-2095,2241-2260` |
| `application/memories/maintain_facts.py` | `backend/db.py:1494-1503,3433-3447`、`backend/pipeline.py:348` |
| `application/retrieval/retrieve_entity.py` | `backend/retrieval/entity.py:1-60` |
| `domain/retrieval/lexical.py`（person_bridge） | `backend/retrieval_indexes.py:20,180` |
| `infrastructure/models/face_detector.py` | `backend/face_detector.py:1-181` |
| `infrastructure/models/face_adapter.py` | `backend/model_clients.py:1616-1938` |
| `infrastructure/models/onnx_runtime.py` | `backend/onnx_runtime.py:1-90` |
| `infrastructure/persistence/migrations/0006_identity.sql` | `backend/db.py:400-1400` 的 DDL 段 |
| `infrastructure/persistence/repositories/faces.py` | `backend/db.py:4467-4955` |
| `infrastructure/persistence/repositories/entities.py` | `backend/db.py:3679-3760,4041-4070,4400-4760,5590-5610` |
| `infrastructure/persistence/repositories/participants.py` | `backend/db.py:2486,3287,5549-5578` |
| `infrastructure/persistence/repositories/appearance.py` | `backend/db.py:4482-4560` |
| `api/routes/identity.py` | `backend/app.py:1969-2100,1498-1800,2205-2420,2508-2533` |

---

## 5. 环境事实（规划时实测）

| 项 | 实测值 |
|---|---|
| 隔离副本 | `/home/asus/s5verify/repo`（`git clone --local`，18 MB），分支 `s5-tdd` |
| 基线 `make ci` | lint / format / mypy（111 files）/ import-linter（5 kept）/ check-docs 全绿；`pytest --cov` 被 **SIGKILL**（`EXIT=2`） |
| 不带 cov 的全量测试 | 660 passed / 25 skipped / 0 failed（第一次）；第二次跑出现 2 条 e2e 抖动（`e2e/test_assistant_turn.py`），单文件重跑 `6 passed` |
| 内存 | 60 GB 总 / 39 GB 已用 / 19 GB 可用（`free -g`） |
| GPU | 13124 MiB / 24576 MiB 已用（`nvidia-smi`） |
| 2.0 venv 人脸依赖 | **全缺**（无 onnxruntime / insightface / opencv） |
| 1.0 venv 人脸依赖 | `insightface 1.0.1` / `onnxruntime-gpu 1.23.2` / `opencv-contrib-python 4.10.0.84` |
| AdaFace 权重 | 700,286,703 B（`/home/asus/models/AdaFace/pretrained/adaface_ir50_ms1mv2.ckpt`） |
| 不稳定的根因 | 同机在跑 1.0 的 8091（RSS 11.7 GB）、vLLM 8500、photobench runner、2.0 的 9598/4274 |

---

## 2j. Task 75 / 76 / 80 实施期实测（含裁决③的落地）

三个任务逐任务提交：`272ea20`（Task 75）、`fef1da5`（Task 76）、`a1e29c7`（Task 80）。
用例总数从 936（920 passed + 16 skipped）涨到 **991 collected**。

### 裁决③的落点：领域错误带码、路由只映射

**做法**：新增 `apps/api/src/sentrix/domain/errors.py` 的 `DomainError`（一个 `code` 字符串
+ 一个 `status_code` 整数），`api/errors.py` 只留四个 api 层自己的子类与**唯一**那个处理器。
用例抛带码的错误，路由原样放行。

**为什么基类必须落在 domain**：`application-purity` 契约不许 application import api，
而 api 可以 import domain——「两边都看得见」的那一层只有 domain。这不是新设计，
是裁决③那句话在现有分层下的唯一落点。

**动到的 S3 代码（两处，都是裁决点名的）**：

1. `application/assistant/run_turn.py` 的 `TurnNotFoundError(LookupError)` →
   `TurnNotFoundError(DomainError, LookupError)`，带 `code="not_found"` / `status_code=404`；
   构造时的实参从「会话 id / 回合 id」改成固定文案（`conversation not found` /
   `turn not found`）——**id 从响应里消失了**，这是行为变更，如实登记。
2. `api/routes/assistant.py` 的 `start_turn` 去掉 `try/except TurnNotFoundError` 与那句
   硬编码 404，改由统一处理器出。

**连带改掉的断言**：`apps/api/tests/contract/test_assistant_routes.py` 断言的是
`response.json()["detail"] == "conversation not found"`（`HTTPException` 的形状）。
统一之后响应体是 `{"code", "message", "detail"}`，断言改成
`["code"] == "not_found"` + `["message"] == "conversation not found"`。
**状态码与文案没变，变的是「谁决定它」。**

**没动的 HTTPException**：`media.py` / `timeline.py` / `search.py` / `assistant.py` 里还有
若干路由**自己在边界上**发现的问题（找不到 asset、目录不存在、result set 越界）仍用
`HTTPException`。它们是「请求在这里就没法继续」，不是用例的结论；把它们一起换掉会改动
四条既有契约的响应体，属于另一次机械变更。**这是一条未结的账**，不是遗漏。

### 第 64 条 · 身份路由的实际范围比计划窄

计划写的是移植 `app.py:1512-1800` 的「`/api/people` 系列」。实际搬的是**身份层能回答的
那几个**：列表 / 详情 / 改名 / 批量确认 / 预置 / 人脸裁剪 + 簇列表与四审阅。
`profile` / `claims` / `event_memory` / `patterns` / `portrait` 五块读的是
`semantic_claims` / `person_event_memory` / `person_portraits` 那几张表，**它们是 S5c
（Task 85/86）的东西**——现在搬等于照抄一个查不到数据的投影。

`/people/{id}` 的详情同理：1.0 的 `get_entity_detail` 还带 relationships / facts /
entity_observations，2.0 只回**实体 + 别名 + 头像脸 + 簇（带样本）**。少的那三块不是
这一层的事实来源（关系与事实各有自己的读侧），登记为**未搬**。

### 第 65 条 · 新增三个读方法与一个端口方法（计划清单里没有）

| 新增 | 为什么绕不开 |
|---|---|
| `EntityRepository.list_people` | `/api/people` 要列人物，而端口上只有 `get`。空间规则（`include_in_people = 0` 的不出现）与 `list_clusters` 同一条，写在这一层只写一次 |
| `FaceRepository.list_clusters_for_entity` | 人物详情要「这个人的簇」。`list_clusters` 是「进人物视图的全部簇」，按实体过滤它做不了 |
| `PreviewEncoder.crop_jpeg` | 裁剪要 PIL，而 `api-transport-only` 契约不许 api 直接 import PIL。端口是唯一合法的落点（与 `encode_jpeg` 同一条理由：同一张图的两种取景） |
| `EntityRepository.confirmed_people_for_observation` / `_for_event` | Task 80 的两个来源（见第 67 条） |

### 第 66 条 · 删掉了 S3 的占位实现

`infrastructure/assistant/identity.py`（`NoIdentityDirectory`）**删除**，不是留着不用。
留着的代价是 `check_reachability` 会把它报成「零生产调用者的模块」——那正是这条检查要抓的
形状。计划清单里没写这一条删除，但它是不变量推出来的必然后果。

capability-map 里那条指向它的记录同步改成新实现（`person_directory.py`），
否则「登记的落点」会指向一个已删除的文件。

### 第 67 条 · Task 80 的两处偏离：装配点与「删掉默认值」

1. **装配点不在 `bootstrap/container.py`，在 `api/routes/media.py` 的
   `build_import_assets`。** 那是 `AssignEvents` 在全仓**唯一**的构造点（该函数的注释
   自己写着「只在这里接一次」），container 里根本没有 `assign_events` 字段。计划把这一行
   记在 container 是记错了位置，不是记错了事。

2. **`confirmed_people` / `confirmed_event_people` 删掉了默认值。**
   S4 给它们留的是 `lambda _: frozenset()`，理由是「那时没有 `entity_mentions`」。
   代价是**「忘了接线」与「真的没有人」在类型上完全一样**，而 `person_score` 恒为 0
   不报错、只让事件分错——与本仓库一路在防的那类静默同形。删掉默认值之后，装配点少写
   一个参数就是 `TypeError`。**这是一次行为面收紧**：任何未更新的构造点会立刻红。

3. **`EventAnchorSet.people` 原来是写死的 `frozenset()`。** 计划只写了移植两个
   `_confirmed_entity_ids_for_*`，但**只接观察侧那一半是没有用的**：`_person_score` 算的
   是 `anchor.people & event.people`，事件侧恒空 → 交集恒空 → 分数恒 0。
   事件侧那一半必须一起接。这条是实施期才看清的，不是计划里的。

### 第 68 条 · 一条没变红的「不变量」被自己的对照臂抓出来

`test_the_person_component_moves_the_total_by_exactly_its_weight` 第一版用**同一对 5 分钟
间隔的照片**跑两条臂，断言 `总分差 == WEIGHTS['person'] * 0.8`。实测**差值恒为 0**：
那对照片的原始总分超过 1.0，被 `max(0.0, min(1.0, total))` 截断，两条臂都等于 1.0。

它没有报错、也没有变红——因为 0.0 != 0.16 会红，它**确实红了**，但红的原因不是「实现错了」
而是「这对输入饱和了」。改法是两臂都用 4 小时的时间差（仍在 6 小时窗内），并在断言前
**显式加一条 `total < 1.0` 的前置**——饱和时先报「换一对不饱和的输入」，而不是报一个
看起来像权重算错的差值。

同族的前一例是第 60 条（变异的 `<=` 没走到边界）。**两次都是「用例看起来在钉一个数，
其实那个数在这组输入下是常数」。**

### 第 69 条 · `split_guard` 一旦触发会把 `total` 直接置 0

`domain/memories/events.py:352` 的 `if split_guard: total = 0.0`。所以「有共同确认人物时
不再拆事件」在**产品上**的表现不是「分数从 0.65 涨到 0.81」，而是**从「另起一个事件」变成
「归并进已有事件」**。Task 80 的那条对照用例因此断言的是 `created`，不是分数——
分数只是中间量。S4 实测的「2017-11-05 被拆成 4 个事件」走的正是 `created=True` 这条路。

### 第 70 条 · 簇样本的排序与 1.0 不同（登记，未改）

1.0 `list_face_clusters` 的样本是 `ORDER BY fi.created_at DESC LIMIT 12`；2.0 走
`FaceRepository.list_instances`（`quality DESC, created_at ASC`）再取前 12。
**同一批样本、不同顺序**，界面上是「先看到最清楚的」还是「先看到最新的」的差别。
改它要动 Task 71 已交付并被门禁钉住的排序契约，本轮不动。

### 本轮**没有做**的

> **两条都已由下一轮（§2k 的 E 节）收掉**：定点变异做了四个任务、`make ci` 逐 commit
> 各跑一次。下面两段是当时的状态，保留原样以便追溯。

- **定点变异（红灯 B）这一轮没做。** 前两轮每个任务挑三条要害改一行、验证红灯指向具体
  断言；本轮三个任务的用例里有对照臂（Task 80 的六条全是），但没有做「改掉实现那一行
  看门禁红不红」的动作。**这是一条未结的账**，下一轮补。
- **`make ci` 的逐 commit 复核这一轮有缺口。** Task 75 提交后启动的那次 `make ci` 在
  Task 76 的改动落地之后才跑到 pytest（`CI_OK`，日志
  `logs/ci-20260923-204812-run0.log`），所以它验证的是 75+76 的合并树而不是 75 单独一棵。
  76/80 之后的完整 `make ci` 见下一节。**如实登记，不当作「每个 commit 都跑过」。**

---

## 2k. Task 81 / 82 / 83 / 84 实施期实测

上一轮 §2j 末尾那两条未结的账，本轮的处理写在本节末尾。

### A. Task 81（事件参与者投影 + 确认后重分段）的偏离

#### 第 75 条 · `captured_by` 那一路没有搬，因为 2.0 没有它的数据来源

1.0 `_refresh_event_participants`（`db.py:5567-5574`）写两种角色：`visible_subject`
（0.75）与 `captured_by`（取 `asset.source_confidence`，缺省 0.5）。后者的取值链是
`assets.source_owner_id` → `entities` 里那个已确认的人。

**2.0 的 `assets` 表没有 `source_owner_id` / `source_confidence` 两列**，也没有任何
写入者：1.0 那两个值来自导入侧的 metadata（`db.py:1467-1473` 的
`metadata.get("source_owner_id")`），而 2.0 的 `ImportRequest` 没有 metadata 通道。
**本轮不建这两列**——建出来就是两个零写入者的列，而 `check_reachability.py` 只覆盖模块、
看不见列（这条不对称上一轮已经登记在第 44 条）。计划的「必须钉住的行为」里写着
`captured_by` 的取值口径，那条口径在 2.0 暂时无处可施；**等哪条导入路径真的带来
「谁拍的」这个信息时再连列一起补。**

#### 第 76 条 · 关系候选（`_maintain_event_cooccurrence_candidates`）没有搬

1.0 `upsert_event_participant` 的最后一句是
`self._maintain_event_cooccurrence_candidates(event_id)`（`db.py:2508-2525`）：同一个
事件里两个 `visible_subject` 会往 `relationships` 写一条 `共同出现` 的 **pending** 关系。

2.0 里**没有任何地方读 `relationships`**（人物详情与家庭图谱是 S6）。写进去就是一张
只有写入者、没有读者的表——S4 已在「零调用的能力」上栽过两次。搬它的那一天，
`create_relationship` 也要一起搬，否则这条候选连落库都做不到。

**（2026-10-02 了结）S7a · T205 已搬。** 这条登记的两个前提**都消失了**：
`create_relationship` 早就落了（`repositories/relationships.py`，S6 之前）；
「没有读者」被 S7a 自己消掉——T201 的实体详情 `relationships[]`、T202 的实体目录
`relationship_count`、T203 的 `/api/relationships` 关系图。这是本仓库第三次
「延期的理由被后续任务自己追上」（前两次：`check_capability_map.py` 的反向规则、
`test_maintain_entities.py` 的「不建关系」）。

补上的是 `ConfirmedPersonEvents._suggest_cooccurrence`（`assign_events.py`）；
1.0 那条自连接的 `SELECT DISTINCT ep.event_id` 落在
`SqliteEventParticipantRepository.cooccurrence_event_count`。
**它是建议不是判定**：写进去的边恒 `pending`（1.0 的 docstring 原话
*Suggest co-occurrence only; users decide whether it represents a real relationship*）。

#### 第 77 条 · `refresh_event_summary` 的两处缩减与一次合并

参与者的摘要重写（1.0 `db.py:2585-2610`）在 2.0 少了三样东西：

1. **地点**直接用 `event.place`，不再走 `_resolved_event_place` 的反查表：2.0 的事件在
   归属时就把 `asset_places` 那一行写进 `place` 了，再解析一次等于把同一条规则实现两遍。
2. **GPS 前缀**（`_gps_prefix_for_event`）没有搬：2.0 的 `events` 表没有 GPS 列。
3. 1.0 那条 `source_type != 'video_scene'` 的摘要豁免在 2.0 **恒真**——`events` 表没有
   `source_type` 列，也就没有视频事件。等视频事件进来时要连列一起补。

另外，1.0 在 `_refresh_event_participants` 里发两条 UPDATE（先 `participants_json`、再
`refresh_event_summary`），**事件 `revision` 因此跳两格**。2.0 没有 `participants_json`
这一列（参与者就是 `event_participants` 表），第一条 UPDATE 的剩余效果只有 revision，
所以合成一次 `apply_summary`：**每个受影响事件恰好 +1**，与计划的判据一致。

#### 第 78 条 · 参与者的 `revision` 是**逐观察**递增的（实测 3，不是 2）

1.0 的循环是 `for observation_id in event_observation_ids: ... upsert_event_participant(...)`
（`db.py:5569-5571`），**每条观察各调一次 upsert**。所以一个事件里同一个人的两条证据
会让参与者行的 `revision` 从 1 涨到 3，而不是 2。用例的第一版断言写的是 2，实测 3——
实现是对的，**断言写错了**，按 1.0 的实际行为改正并把这个数写进注释。

#### 第 79 条 · `event_entities` 的 upsert 没有公开方法，但换家那一段照样搬

计划的移植清单里有 `upsert_event_entity`（`db.py:3287`）。2.0 现在**没有任何一处写
`event_entities`**（1.0 的写入者是 agent runtime 与实体抽取，属 S6），搬一个零调用者的
公开方法正是本仓库一路在防的那个形状。但 `rehome` **仍然搬这张表的行**：它的
`event_id` 有指向 `events(id)` 的外键，而连接上开着 `PRAGMA foreign_keys`，
留着就会挡住 `events.merge` 的 `DELETE`。合并语义（同键合并证据、抬置信度）以私有形态
存在，等 S6 有了写入者再开口。

#### 第 80 条 · 两处 2.0 新增的端口方法（计划里没有）

- `EventRepository.events_for_observations`：1.0 是**逐条观察**查一次再在 Python 里分组
  （`db.py:5550-5554`），2.0 一次 `IN` 查完。行为一样，往返次数从 N 降到 1。
- `EventParticipantRepository` 整个端口，以及 `ConfirmedPersonEvents` 这个用例类落在
  `application/memories/assign_events.py` 里（计划把 `participants.py`【新】记在
  infrastructure，application 侧只写了 `assign_events.py`【改】——按计划的文件清单落）。

### B. Task 82（facts 门禁 + 候选来源）的偏离

#### 第 81 条 · `facts_json` 新开 0007，不并进 0006

计划把「加进 0006 或新开 0007」留给了执行时判断。**取 0007**：0006 已经作为一次提交
发出去过，而迁移器只记版本号、不记内容——任何跑过 0006 的库都不会再执行一遍，
把它改宽只会得到「老库少一列、新库多一列」，那种差异**不报错**，只在写事实时炸。

#### 第 82 条 · `FactCandidate` 被 `ObservationFact` 取代（删掉一个重复类型）

`application/memories/maintain_facts.py` 原本有一个 `FactCandidate`（四个字段与
模型自报的 facts 元素完全一样）。本轮把形状收到 `domain/media/models.py` 的
`ObservationFact`——它**是观察的一部分**（跟着 `ObservationSemantics` 从观察器出来、
跟着 `Observation` 落库），而 `domain/memories/events.py` 反过来要 import
`domain/media/models.py` 的 `GpsPoint`，把形状定义在 memories 会成环。
`maintain_facts.py` 的 `FactCandidate` 与 `DEFAULT_FACT_CONFIDENCE` 一并删除。

**顺带登记一处 1.0 的死兜底**：`pipeline.py:350` 的
`normalize_confidence(fact.get("confidence"), result.get("confidence", 0.5))` 那个 0.5
在 1.0 就到不了——`analyze_image` 已经用 0.65 归一过一遍（`model_clients.py:1190`）。
2.0 的 `ObservationFact.confidence` 默认值因此是 **0.65**。

#### 第 83 条 · `MaintainFacts.confirmed_people` 删掉了默认值

S4 给它留的是 `lambda _scope_id: frozenset()`。代价与 Task 80 删掉 `AssignEvents` 那两个
默认值时完全一样：**「忘了接线」与「真的没有已确认人物」在类型上长得一模一样**，
而事实恒空不报错、只是永远查不到。删掉之后装配点少写一个参数就是 `TypeError`。

#### 第 84 条 · `purge_unanchored` 在 S5 没有生产调用者

1.0 `db.py:3433-3447` 的 `purge_unanchored_facts` 唯一调用点是
`scripts/maintenance/rebuild_memory.py:128`，2.0 不复刻脚本。本轮把它做成
`FactRepository.purge_unanchored` + `MaintainFacts.purge_unanchored` 并有两条用例
（正例 / 反例），**但不接任何用例**——接了就是自己发明一个触发时机（1.0 的
cluster reject 与 rename 都不调它）。**如实登记为「有能力、无调用者」**，
等 S6 的维护入口出现再接。

#### 第 85 条 · 一条**同构装配**的用例证明不了生产装配（本轮最有价值的一条实测）

Task 82 的端到端用例第一版是自己拼一个 `MaintainFacts`（「同构装配」），
把生产 `api/routes/media.py` 里那句 `candidates=lambda observation: observation.facts`
换成 S4 的恒空元组 —— **它照样全绿**。用例验的是一个自己拼出来的装配。

改法是加一条**走 `build_import_assets(container, observe=True)`** 的用例。加上之后，
同一个变异立刻变红（红点落在 `test_the_production_wiring_writes_the_observed_fact`）。
**这条教训适用于所有「装配点 + 用例各自拼一遍」的地方**：同构不等于同一条路径。

### C. Task 83（`person_bridge` + 实体检索通道）的偏离

#### 第 86 条 · 1.0 的 `person_bridge` 投影写的是 `str(dict)`，2.0 写名字

1.0 `retrieval_indexes.py:74-80` 的 `_terms_from_field` 对列表元素一律
`_normalize(item)`，而 `observations.people` 的元素是
`{"entity_id":…, "name":…, "status":…}` 这样的 dict —— `str(dict)` 写进
`person_bridge` 之后，`WHERE normalized_value = '妈妈'` **永远查不中**。
1.0 里真正干活的是 `EntityRetriever._resolve_asset_ids_for_person` 那条
「投影查不到就退回直接读观察」的兜底（`entity.py:50-59`）。

**2.0 的值源是名字**（`Observation.people` 已经是名字元组）。投影因此真的能命中；
兜底那条路照搬，但它现在管的是「投影还没建起来的库」而不是「投影建坏了」。
测试 `test_confirming_a_person_projects_the_name_not_the_raw_snapshot` 钉的是
`normalized_value` 的**值**——把名字换回 `str(dict)`，它变红。

#### 第 87 条 · `EvidenceRequest.person` 是 2.0 新增字段（计划文件清单里没有）

计划的 Task 83 文件清单只有 `retrieve_entity.py`【新】+ `lexical.py` / `ports.py` /
`search_terms.py`【改】。但 entity 通道**只在有人物条件时才给召回信号**：不把
`retrieve_evidence.py` 的 `EvidenceRequest` 加一格 `person`，那条通道在类型上就没法被
喂过——它会永远返回空，而不是「被接上了」。这与 S4 给 `metadata` 通道加
`media_types` / `time_bounds` / `place` 是同一个处境（那几个字段的 `http` 入口同样要等
S6 的查询解析）。所以 `retrieve_evidence.py` 也在本轮改了，登记在此。

#### 第 88 条 · 兜底那条路不支持跨空间查询

`RetrieveEntity` 的兜底读 `ObservationRepository.list_scope(scope_id, limit)`，而它要求
一个具体的 scope；1.0 的 `list_observations(limit=100_000)` 可以不带。`scope_id` 为空时
兜底**不跑**（投影那条路照跑）。2.0 目前没有跨空间的人名查询调用方
（`EvidenceRequest` 总有 scope），所以这里是「不支持」而不是「随便挑一个空间」。

#### 第 89 条 · 兜底的扫描上限 100_000 写在常量里

1.0 `entity.py:52` 的 `list_observations(limit=100_000)`。写成
`FALLBACK_SCAN_LIMIT` 而不是一个看起来无穷大的默认值：**超过它的照片会静默召回不到**，
这个数必须可见。

### D. Task 84（全局重聚类维护入口）的偏离

#### 第 90 条 · 仓储补了 **6 个**方法（计划只列了 application + route + 测试）

计划的任务文件清单里，Task 84 只有 `recluster.py`【新】、`routes/maintenance.py`【新】、
`main.py` / `container.py`【改】+ 两个测试。**实际至少要给仓储补方法**（上一轮已经预告）：

| 端口 | 新方法 | 1.0 对应 |
|---|---|---|
| `FaceRepository` | `list_verified_instances` | `db.py:4961-4967` 那条 SELECT |
| `FaceRepository` | `assign_instances`（批量改归属） | `db.py:4993-4996` 的 `executemany` |
| `FaceRepository` | `set_cluster_state`（状态 + member_count） | `db.py:4997-5001` |
| `FaceRepository` | `list_uncertain_unclustered` | `db.py:5054-5060` |
| `FaceRepository` | `list_confirmed_clusters`（认领目标） | `db.py:5085-5088`（`member_count >= 2`） |
| `EntityRepository` | `retire_unclustered_candidates` | `db.py:5118-5132`（`_sync_face_entity_statuses`） |
| `VectorRepository` | `update_metadata` | `db.py:5028-5038`（改向量 metadata 的 `cluster_id`） |

合计 **7 个**。计划的清单低估了这条路径的读写面，这一行是如实登记。

#### 第 91 条 · `_sync_face_entity_statuses` 写成一条 SQL 而不是逐实体循环

1.0 是「列出所有 person 实体 → 逐个查活跃簇数 → 逐个 UPDATE」。2.0 写成**一条带
`NOT EXISTS` 的 UPDATE**：同一件事，但少了 N 次往返，而且判据只有一处。
唯一的语义差别是「已确认的人不动」从 Python 的 `if` 变成 SQL 的 `status != 'confirmed'`——
两条用例（孤儿被撤 / 已确认的人不动）分别钉住两侧。

#### 第 92 条 · 认领阈值 0.75 不读环境变量

1.0 读 `os.getenv("FACE_ATTACH_THRESHOLD", "0.75")`。2.0 写成常数：`domain-purity`
禁止 domain 碰 `os`，而且这类阈值一旦能从环境改，**同一个库在两台机器上会聚出不同的
结果，差别只藏在部署脚本里**。同组的另外两个数（0.08 的领先余量、`>= 2` 的最小成员数）
1.0 本来就是硬编码的。

#### 第 93 条 · 新增 `api/schemas/maintenance.py`（计划只列了 routes）

与其余端点同构：请求 / 响应模型放 `api/schemas/`，路由只做映射。
`threshold` / `minimum_quality` 的默认值取 `FaceClusterer` 的类默认（`0.30` / `0.30`），
放在**请求模型**上是为了让默认值出现在 OpenAPI 契约里——调用方看得见自己不传时会按
什么口径跑。**不取** 1.0 两个 benchmark 调用方的 `0.30` / `0.55`（后者与类默认不一致，
已登记在证据 39）。

#### 第 94 条 · `bootstrap/container.py` 没有改（计划列了它）

计划的文件清单里有 `container.py`【改】。实际不需要：维护路由与身份路由一样，
在路由里从已有的 `container.*` 字段构造用例（那两处是「用例装配点」，
`container.py` 的职责是**适配器**装配）。为一条不需要新适配器的路由加一个 container
字段，只会让「谁是装配点」多一处。

#### 第 95 条 · 认领时的同分排序改成确定性

1.0 `ranked.sort(reverse=True)` 只按分数排，同分时名次由簇 id 的降序决定——
同一份数据换个插入顺序就换一个答案，而这里**挂错人是会留痕的**。
2.0 用 `(-score, cluster_id)`；0.08 的领先余量本来就会挡掉近似同分，
这条是为了让「真的同分」也确定。

### E. 上一轮两条未结的账，本轮的处理

- **70c（定点变异）本轮补上了。** 四个任务各挑了要害改一行验证红灯指向具体断言：
  Task 81——把 `confirm` 里那两句接线注释掉（红灯精确落在
  `test_confirming_a_cluster_merges_the_two_events_its_photos_created`）；
  Task 82——①候选来源退回恒空、②门禁退回恒空、③解析器丢掉 facts，
  三次都红且红在对应的断言上；Task 83——①通道顺序排到 lexical 之后（分数从
  `1.0 + 2/61` 掉到 `1/61`）、②退回 S4 的不建 `person_bridge`、③把名字换成
  `str(dict)`（重现 1.0 的缺陷），三次都红。
- **70d（每个 commit 单独一次完整 `make ci`）本轮每棵树各跑一次**，日志见交付记录；
  本轮不再有「验证的是合并树」的缺口。

#### 第 96 条 · 1.0 只撤「有实例、但实例被并走」的簇；一张脸都不挂的簇不会被清

`recluster_faces` 的 `stale_ids` 是 `{row["cluster_id"] for row in instances ...} - active`
（`db.py:5019-5021`）——它的来源是**实例表**，所以一个 `member_count = 0`、一张脸都不挂
的簇**根本不在那次重算的视野里**，状态保持原样。2.0 照抄了这个行为
（`test_a_cluster_with_no_instances_at_all_is_not_retired` 把它钉住）。

真正的清理落在实体那一侧：那种簇对应的候选人物会在
`retire_unclustered_candidates` 里因为没有活着的簇被转成 rejected。所以「空簇」这件事
在 2.0 里是**一半被清理、一半留着**——留着的那个是历史审计，转掉的那个是人物列表的
显示面。这不是我们的设计选择，是 1.0 的行为；登记在此，免得后来人把它当成 bug 顺手
删掉一个簇。

### F. 真数据 e2e 撞出来的两件事

#### 第 97 条 · 全新部署里没有一个地方建 `memory_spaces` 行，人物页因此恒空

真数据 e2e 的第一次跑法（导入 53 张 → 读 `/api/v1/face-clusters`）拿到的是
**`clusters: []`**，尽管人脸检测确实跑过（insightface 的四个模型都加载了）。
根因不在人脸那一路：`FaceRepository.list_clusters` 与 `EntityRepository.list_people`
都内连 `memory_spaces`（`ms.include_in_people = 1` / `include_in_people = 0`），
而**2.0 没有任何一处写这张表**——`SpaceRepository.ensure` 有实现、有端口、在
`Container` 上装配着，`grep` 全仓**零调用者**。

后果是产品级的：一个全新部署导入一整册照片之后，**簇审阅页一个簇都不显示、人物列表
一个人都没有**，而库里 30 个簇、117 张脸都在。这不是「少一条数据」，是整块身份能力
在默认路径上不可见。

`check_reachability.py` 看不见它——那条检查只覆盖**模块**，而 `spaces.py` 是被
`container.py` import 的（模块可达），零调用的是它上面的一个**方法**。
这与第 44 条登记的不对称是同一类（检查覆盖模块、不覆盖表/方法），
**本轮如实登记，不改**：「哪条入口负责建记忆空间」是一个新的产品决定
（导入时建？建空间时建？默认空间预置？），按纪律不由实施者自己发明。

e2e 的跑法因此在**测试脚本里**显式调了一次 `SpaceRepository.ensure(scope_id, scope_id)`
——那一行是测试夹具，不是产品路径。

**（2026-09-23 裁决一已关闭本条。）** 落点、`name`/`kind` 与控制组的形态见第 102 条；
其中「`list_people` 也内连这张表」半句**不成立**，已在第 103 条纠正。本轮之后的 e2e
脚本里**一次 `spaces.ensure` 都不调**——那一行从夹具变成了产品路径的一部分。

#### 第 98 条 · 70b（路由自己的 `HTTPException`）本轮不做，理由与后续动作

上一轮列的 70b：`media.py` / `timeline.py` / `assistant.py` 里路由自己抛的
`HTTPException` 没换成带码错误。本轮**判断不做**，三条理由：

1. **它不是「收一条尾」，是一次跨四个功能的响应体迁移**：全仓 16 处
   （`assistant.py` 10、`media.py` 4、`timeline.py` 2），会动到助手 SSE / 结果集交付 /
   媒体交付 / 时间线四条**既有契约**的响应体（`{"detail": …}` → `{"code", "message", "detail"}`）。
   把它们塞进 S5b 的任一 commit 里，会让那份补丁同时做两件互不相干的事。
2. **其中一处需要一个新的错误类**：`assistant.py:389` 的 403（`scope mismatch`）在
   统一族里没有对应物（现有四个是 400 / 404 / 409 / 422）。加一个 `ForbiddenError`
   是一次设计决定，不该顺手做。
3. **S5 自己的面已经是一致的**：Task 75 起的身份路由用的是统一形状。
   剩下的是 S3 / S4 的旧面。

**后续动作（已想清楚，可以直接做）**：加 `ForbiddenError(status_code=403)`；
把 16 处逐个换成 `NotFoundError` / `InvalidRequestError` / `ForbiddenError` /
`UnprocessableError`；同步改 `test_assistant_routes.py` / `test_assistant_events.py` /
`test_timeline_routes.py` / `test_result_set_photo.py` 里断言 `["detail"]` 的那几处。
一次独立的 commit，跑一次完整 `make ci`。

#### 第 99 条 · C 项（真数据 e2e 的前置问题）的解法：新开 scope，不清库

上一轮指出的问题：真实 `data/` 里的 53 张照片是**去重后**的——`find_by_hash` 命中时
返回 `deduplicated=True, observation_id=None`，**跳过事件归并**，所以重跑导入不会重建
事件，也不能靠清库绕。

**解法取「换个 scope 导入同一册」**（用户给的第一个选项）：去重是**按 scope 隔离**的
（`ImportAssets.execute` 的 `find_by_hash(request.scope_id, …)`），换一个 scope 就是一批
全新的 asset / observation / event，事件归并从零跑一遍。代价是同一批照片在磁盘上存两份
（53 张约 146 MB）。**这条代价落在 e2e 的临时数据目录里**，真实仓库的 `data/` 一个字节
都没动。

**没有走「只重算事件」那条路**：那是一个新能力（缺的是「按 scope 重建事件」的入口与
它自己的门禁），按纪律要先报告，不在本轮自己发明。

#### 第 100 条 · 真数据 e2e 的实测结果（含一处与 S4 基线的逐字复现）

夹具：53 张真照片 + 真 EXIF + 真地点解析 + 真 CLIP 向量 + 真 RetinaFace + 真 VLM（8100）
+ 真 buffalo_l 嵌入。两个 scope、同一批输入，差别只有「导入之前有没有一个已确认的人」。

| 臂 | 做法 | 事件总数 | 2017-11-05 | person_score > 0 |
|---|---|---:|---:|---:|
| A | 直接导入，没有任何已确认人物 | 17 | **4** | 0 / 17 |
| A' | A 之后确认最大那个簇（11 张脸，认成「妈妈」） | 17 | **4** | 0 / 17 |
| B | 预置身份（取该簇 `samples[:2]`）后再导入 | 17 | **4** | 0 / 17 |
| C | 预置身份（取该簇**两张不同照片**）后导入这个人出现过的 9 张 | 3 | — | 0 / 3 |

**A 臂逐字复现了 S4 的基线**：`2017-11-05` = 4 个事件（S4 记的就是 4）。
`person_score` 全 0，也复现了「S5 之前恒为 0」。

**A' 说明 Task 81 的重分段确实跑了、但在这批数据上一对事件都没合**：确认之后
`event_participants(visible_subject)` 有 3 行、3 个事件的摘要被改写成「妈妈在…参与…」，
而 `2017-11-05` 仍然是 4 个事件——重分段的两道门（6 小时窗、八分量 `>= 0.50`）
在那几对事件上没有同时成立。

**B / C 两臂的 `person_score` 仍然是 0**，根因不在接线，在**预置照片的选脸语义**：

- `SeedIdentity` 走的是「整张照片 → 检出的脸里挑最好的那张」（1.0 的口径）；
- 在一张多人的家庭照里，**最好的那张脸通常不是用户想预置的那个人**。
  实测 `IMG_6342.JPG`：预置取到的那张脸（q=0.88）与 A 臂导入路径检出的同一张脸
  余弦 1.000、框逐位相同，而**那张脸在 A 臂里根本没有进任何簇**（`validity='uncertain'`）；
  A 臂那个 11 张的簇在这张照片里用的是另外两张脸。
- 于是「预置出来的妈妈」是一个**只在一张照片里出现过的人**：该簇原型与全册其它 14 张脸的
  最大余弦只有 **0.106**（匹配阈值 0.30），enrollment 正确地一条提及都没写。

顺带两处登记：

- **预置实例的 `pose_bucket` 是 `unknown`**（导入路径同一张脸是 `frontal`）。
  第 73 条登记过预置的 `validity` 写死 `uncertain`，`pose_bucket` 是同一族的第二处；
  它会让预置簇的原型落在 `unknown` 桶上。
- 这册照片里有**像素级重复、字节不同**的两张（`2019-01-10 132226.jpg` 与 `IMG_6342.JPG`
  的检测结果逐位相同），`find_by_hash` 的 SHA-256 去重抓不到它们。

**结论**：`person_score` 的接线是通的（用例 + 定点变异都证明了），
**但「有了真数据它就不再恒为 0」这一条本轮没有被证实**——因为在这册照片上，
能被预置成一个「在别处也出现的人」的条件没有凑齐。要把这条验收做实，
需要的是「按人脸实例预置身份」（而不是「按整张照片」）或者一个「这个人出现过的照片」
的选取入口；那是新的产品决定，登记在此，不在本轮自己发明。

#### 第 101 条 · 隔离副本的 `make ci` 比真实仓库**少跑 16 条用例**

真实仓库有 `data/geo`（`CN.zip` / `cities15000.zip`），隔离副本的 `data/geo` 是空的。
`tests/e2e/test_album_timeline.py` 的夹具在缺 geo 数据时 **skip**（那条 skip 是设计好的），
于是隔离副本的 `make ci` 是 `1034 passed, 16 skipped`，而真实仓库同一棵树会**真跑**那 16 条
——真照片、真地点解析，并且从 S5b 起还会连带跑真人脸检测。

实测代价：真实仓库那次 `make ci` 的 pytest 跑了 30 分钟以上、峰值 2433% CPU，
而隔离副本同一棵树是 4 分钟。**「两边树一模一样」不等于「两边跑的东西一样」**——
这条不对称登记在此，免得后来人把隔离副本的绿灯当成全量证据。

### G. 裁决一 / 裁决二的实施记录（2026-09-23 第二轮裁决）

#### 第 102 条 · 裁决一的落点是 `ImportAssets.execute`，不是 `ImportAlbum`

1.0 建记忆空间的地方在 `db.py:1462` 的 `create_asset`：

```python
self.create_memory_space(scope_id, scope_id) if not self._row("SELECT id FROM memory_spaces WHERE id = ?", (scope_id,)) else None
```

单张 `/api/ingest` 与批量 `/api/import` **都从它过**——它是两条入口的公共落点，不是某一条的。
2.0 的同一个公共落点是 `ImportAssets.execute`（`ImportAlbum` 只是它的调用方），所以 ensure
接在那里，两条入口一次覆盖；接在 `ImportAlbum` 上会漏掉单张上传那条。

**`name` / `kind` 取的是 1.0 的哪一份。** `name = scope_id`（`create_asset` 传进去的就是
scope_id 自己），`kind` 取 `create_memory_space` 的**签名默认** `household`。1.0 的两个批量端点
显式传 `kind="benchmark"`（`app.py:3602` / `:3700`），那是评测旁路——按第 6 / 30 条不移植进
生产代码。

**协作者按本文件既有的形状做成可选的**（不传 `spaces` 就静默跳过）。这不是留后门：模块注释里
「协作者全是可选的」是这份文件对单元测试的既有承诺，而代价由控制组用例分开钉住
（`test_a_cluster_can_exist_in_the_database_and_still_be_invisible_on_the_people_page`）。

`SpaceRepository.ensure` 在此之前**全仓零调用者**。`check_reachability.py` 看不见它——那条检查
覆盖**模块**，而 `spaces.py` 一直被 `container.py` import 着（模块可达），零调用的是它上面的
**方法**。这一条与第 44 条登记的是同一类不对称，本轮仍未修检查本身（那是一个独立的决定）。

#### 第 103 条 · 纠正第 97 条的一半：`list_people` 并没有内连 `memory_spaces`

第 97 条写「`FaceRepository.list_clusters` 与 `EntityRepository.list_people` 都内连这张表」。
**后半句不成立。** `list_people` 用的是

```sql
scope_id NOT IN (SELECT id FROM memory_spaces WHERE include_in_people = 0)
```

（`entities.py:171`，1.0 `db.py:4043` 的 `list_entities` 原样）——一个**子查询**，
没登记过的 scope **照常列出来**。这一轮写控制组用例时才撞出来：断言「两个人读端点都读不到」
的那一行红了，红的原因是断言写错了，不是代码错了。

所以缺空间行真正打掉的面是**簇**：`/api/v1/face-clusters`（审阅页的「待命名成员」列表）恒空，
`/api/v1/people`（已确认的人）不受影响；`/api/v1/people/{id}` 里的 `list_clusters_for_entity`
也不过滤空间，同样不受影响。**受影响的面比第 97 条说的小，但仍然是产品级的**——审阅页
的半边是空的，而用户在全新部署上第一步就是去那里确认人。

这条不对称（簇内连、人不内连）是 1.0 的原样，不是 2.0 引入的，本轮**不改**；只把第 97 条
的措辞纠正过来，并在
`test_a_cluster_can_exist_in_the_database_and_still_be_invisible_on_the_people_page`
里钉成断言，免得后来人照着那句描述去「修」一个不存在的对称。

#### 第 104 条 · 预置路由（`/people/seed`）仍然不建空间行——而 1.0 是建的

**（2026-09-24 裁决三已关闭本条：「补，与 1.0 一致」。落点与门禁见第 109 条。）**

1.0 的预置路由走 `store.create_asset`（`app.py:2337`），那里面就有 ensure；2.0 的
`_store_seed_photo` 直接 `asset_repository.add`（模块注释写明「不走导入链」）。于是
**往一个全新 scope 预置身份、又不导入任何照片时，那个簇在 `/face-clusters` 里看不见**。

本轮**不改**：裁决一只说「整册导入 / 单张导入」，把落点扩到预置路由是一条新的产品决定
（「预置时建空间」还是「空间由部署方预置」）。登记在此，等裁决。

顺带一条**不是问题**的确认：`entities` / `face_clusters` 的 `scope_id` **没有**指向
`memory_spaces` 的外键（`0006_identity.sql:25` / `:79`），所以往一个还没有空间行的 scope 预置
不会违反外键——上面那条是「看不见」，不是「写不进去」。

#### 第 105 条 · 裁决二的实现：`_SeedSample` 把两条入口合流，实例路径不重跑检测

`SeedIdentity.execute` 新增 `face_instance_ids`，与 `photos` **可混用**。两条入口只在
**决定用哪张脸**那一步分家：

- 照片：`detect_identity_photo` → 取质量最高的一张（1.0 的口径，**原样保留**）；
- 实例：`FaceRepository.get_instance` → 逐字段照搬（向量 / 框 / 姿态桶 / 质量 / 模型签名），
  `_detection_of` 一个字段都不重算。

从 `_SeedSample(asset_id, observation_id)` 往后是同一段落库 + 刷原型，所以两条路径产出的簇
完全同形。实例路径**不调检测器**——契约测试把容器里的检测器换成「一调就炸」的替身
（`ExplodingDetector`），这条路径仍然是 201，而照片那条路在同一台没有权重的机器上会退化成空表。

**全部实例 id 在任何写库之前一次校验完**，一个不存在就整体失败、一行不写
（`SeedInstanceNotFoundError` → 404）。与「预置照片里一张脸都没有」（422）**分成两个码**：
一个要换 id，一个要换图。这两条是路由上新的边界，也是这条路唯一新增的错误面。

契约的扩展是**请求侧**：`files` 从必填变可选，新增表单字段 `face_instance_ids`
（顿号或逗号分隔，与 `aliases` 共用同一条拆分规则）。响应模型**没有改**——`face_count`
本来就是「这次预置进去几张脸」，两条入口共用它。OpenAPI 已按 `scripts/export_openapi.py` 重导。

#### 第 106 条 · 两条遗留（`pose_bucket='unknown'` / `validity='uncertain'`）**不是**「对不上」的原因

第 100 条登记过：预置实例落的是 `pose_bucket='unknown'`（导入路径同一张脸是 `frontal`）、
`validity='uncertain'`，并猜这两条「可能就是『对不上』的另一个原因」。**实测两条都不成立。**

- **姿态桶不参与匹配。** 在线匹配读的是 `list_prototypes(cluster_id)` 的**全部向量**——
  `EnrollFace._representatives`（`enroll_face.py`）只按 `(model_name, model_version)` 签名
  过滤，**不按姿态桶过滤**。桶只决定「同一个桶里留哪一张当原型」（`refresh_prototypes` 的
  `best.setdefault(bucket, row)`），不决定「哪些原型参与比对」。
- **`validity` 不在在线匹配的路径上。** 它只出现在两个地方：`list_verified_instances`
  （全局重聚类那座「已复核」的门）与 `list_uncertain_unclustered`（认领）。

`test_the_stored_pose_bucket_and_validity_do_not_decide_the_match` 把结论钉成可失败的断言：
预置出来的实例确实是 `unknown` + `uncertain`（两条都断言了），而同一张脸导入时仍然落进那个簇、
写下提及。**真正的原因只有一个：选脸语义**（第 100 条查到的那个），这两个字段是被牵连的。

#### 第 107 条 · 定点变异：四条新门禁各自被「故意写错」的一版抓住

每条门禁都做了定点变异（把实现改坏一处、确认对应用例变红、再还原）。原始输出见
交付报告；四条与它们抓住的用例：

| 变异 | 改坏的地方 | 变红的用例 |
|---|---|---|
| M1 | 删掉 `ImportAssets.execute` 里的 `_ensure_space` 调用 | `test_import_memory_space.py` 3 条 |
| M2 | 删掉装配点 `build_import_assets` 里的 `spaces=container.spaces` | `test_media_routes.py` 2 条（单张 + 整册两条入口） |
| M3 | `_detection_of` 丢掉库里那条实例的向量 | `test_import_identity.py` 2 条 |
| M4 | 未知 `face_instance_id` 的 404 被改成 422 | `test_identity_routes.py` 1 条 |

**M2 是本轮最该有的一条。** 集成用例自己构造 `ImportAssets` 并显式传协作者，装配点漏了
它看不出来——这正是第 85 条「一条同构装配的用例证明不了生产装配」的另一个实例，
所以单张与整册两条入口各补了一条只走 HTTP 的用例。

**一条自己撞出来的教训（过程记录）。** 第一版变异脚本没有备份 `identity.py`，M4 的改动因此
留在了工作树里、并且被提交进去。它不是被人工复核发现的，是被**全量 `make ci` 抓住的**：
单跑 `test_identity_routes.py` 时那条用例通过（同一个文件里的其它用例先跑），跑整个
`contract/` 目录时才红。两件事记在这里：一是变异脚本的 `restore` 必须覆盖**每一处**改动的
文件（漏一处就是往提交里塞 bug）；二是**只跑改动文件对应的用例不足以证明整棵树是绿的**。

#### 第 108 条 · 真数据 e2e 的结论：`person_score` 不再恒为 0，但事件归并**没有**变好

夹具与上一轮同一批（53 张真照片、真 EXIF / GPS / 地点解析 / CLIP / RetinaFace /
VLM（8100）/ buffalo_l）。两个 scope、同一份输入，差别只有「导入之前有没有一个按实例预置的
已确认的人」。脚本里**一次 `spaces.ensure` 都不调**——第 102 条之后那一行已经是产品路径的
一部分，不再是夹具。

| 臂 | 做法 | 事件总数 | 2017-11-05 | person_score>0 | 提及 |
|---|---|---:|---:|---:|---:|
| before | 直接导入，没有任何已确认的人 | 17 | 4 | 0 / 17 | 0 |
| after | 用最大簇的 `face_instance_id` 预置「妈妈」后导入 | 17 | **4** | **2 / 17（最大 0.8）** | **11** |

**裁决二成立，`person_score` 不再恒为 0。** 预置簇 `cluster_4616c4bfafcc` 的代表向量与所选
实例的余弦 = **1.000**；导入时 11 张同人的脸落进那个簇（成员 1 → 12），写下 11 条
`entity_mentions`。2019-01-10 与 2019-07-22 两个事件的 `confirmed_people` 由 0 变 0.8，
总分各 +0.16（权重 0.20 × 0.8），与权重表逐位对得上。

同一份 e2e 顺手量到了裁决二的前提有多硬：所选实例所在的 `2019-01-10 132226.jpg` 里有
**7 张脸**，而 `detect_identity_photo` 按「质量最高」挑出来的那张与所选实例的余弦是
**0.013**——照片那条路会预置成另一个人。

**「事件是否因此归并得更好」没有实现**，根因是数据分布，不是接线：

1. **预置的那个人在 2017-11-05 一张照片里都没出现**（那天 0 条提及）。他在册里的 12 张脸
   全落在 2019-01-10 / 2019-07-22 / 2023-08-06 三天。于是 2017-11-05 的每一次 fold 分数在
   两条臂里**逐位相同**（`0.6979861145853539` / `0.703147410795947` / `0.716123735392072`），
   分组自然一模一样——**person 分量在那天是 0，加 0 不改变任何一次判定**。
2. **他出现的那三天，before 臂本来就各自只有一个事件。** 总分被抬起来了
   （0.7374 → 0.8974、0.7252 → 0.8852），但没有可合并的对象。
3. 2017-11-05 那 4 个事件之间的分隔**不是「缺一个人」造成的**：那天检出的 33 张脸里
   **30 张 `validity='uncertain'`**，按 enrollment 的资格闸门（`identity_eligible`）根本不参与
   匹配；剩下 3 张属于别的簇。person 分量在那些 fold 上本就是 0。
   （**这一组数带口径**：33/30 是那次跑的原始读法，**第 110 条**复核时复现不出来、并把它
   与另一套可复现的口径 35/33 并列登记了。引用这天时连口径一起写。）

**一处必须记住的口径**：`events.aggregation_breakdown_json` 存的是**最后一次 fold 的分数**，
不是事件级聚合（`_fold` 每次覆盖写）。所以「person_score > 0 的事件数」是一个**快照**，
不是「person 参与过的事件数」：2023-08-06 那个事件的最后一条观察
（`IMG_20230806_205817.jpg`）没有这个人，存下来的就是 `0.0`，而它前面两条
（`204143` / `204342`）带着提及折进来时 person 分量是算过的。上一轮的 `0/17` 与本轮的 `2/17`
用的是同一个口径，两条都成立，但都只说明「最后一折」。

**结论与后续**：要让「事件归并得更好」在这一册上可见，缺的不是入口，是
**数据分布**——最大那个人不覆盖 2017-11-05。这一条是数据事实，本轮到此为止，
**不据此调任何阈值**（调阈值是另一条产品决定，需要它自己的对照）。


## 2l. 收尾（2026-09-24）：第 104 条的裁决落地 + 三条登记

### A. 裁决三：`/people/seed` 也建空间行

#### 第 109 条 · 落点是 `seed_person_identity` 路由，不是 `_store_seed_photo`——预置有**两条**入口

第 104 条登记的问题是「2.0 的预置路由不建空间行，而 1.0 建」。2026-09-24 用户裁决
**「补，与 1.0 一致」**，本条是它的实施记录。

**1.0 的落点，与它在 2.0 为什么不对应。** 1.0 的预置路由走 `create_asset`（`app.py:2337`），
ensure 在那个函数里（`db.py:1462`）——而 `create_asset` 是 1.0 **唯一的资产写入点**，所以
在 1.0「落点在 `create_asset`」与「落点在预置路由」是同一句话。2.0 之后不再同一：裁决二
给预置加了**实例入口**，那条路径一张资产都不写。把 ensure 接在 `_store_seed_photo` 上，
往全新 scope 按实例预置（不传 `files`）出来的簇**仍然看不见**——与第 104 条要修的是同一个
缺陷，只是换了一条入口。

落点因此取**两条入口的公共点**：`seed_person_identity` 里、任何写库之前的一行
`container.spaces.ensure(scope_id, scope_id)`。理由与第 102 条「接在 `ImportAssets.execute`
而不是 `ImportAlbum` 上」逐字相同（公共落点，不是某一条入口的落点）；`name = scope_id`、
`kind` 取签名默认 `household`，也与第 102 条同一份取值。

**一处已知副作用，登记不改。** ensure 排在写库之前（1.0 的顺序），所以一个**被拒**的请求
（`face_instance_ids` 指向不存在的实例 → 404）会留下那一行空间。它与 1.0 的 `create_asset`
同形（1.0 同样是先 ensure 再写），而 2.0 的预置路由本来就有失败残留——照片入口在
`_seed_identity` 判 422 之前就已经把资产落了库。把 ensure 挪到成功之后能消掉这一行，代价是
与 1.0 的顺序不再一致。两害相权取「与 1.0 一致」。

**门禁两条，一正一反，都只走 HTTP**（理由与第 107 条 M2 相同：同构构造的用例证明不了
生产装配）：

| 用例 | 钉的东西 |
|---|---|
| `test_seeding_into_a_fresh_scope_registers_the_space_so_the_cluster_is_visible` | 往全新 scope 按实例预置 → `/face-clusters` 看得见这个簇 |
| `test_a_cluster_in_a_scope_without_a_space_row_is_invisible_on_the_review_page` | 反向那一半：同一行簇，加空间行之前读不出来、之后读得出来 |

**定点变异 M5：注释掉 `container.spaces.ensure(scope_id, scope_id)` → 第一条变红**（原始
输出见交付报告）。

**另一条量的是门禁的边界，记在这里免得后来人白改。** M6 把 `list_clusters` 的
`INNER JOIN memory_spaces` 改成 `LEFT JOIN`，反向那条**不会**红——`ms.include_in_people = 1`
那个谓词自己就够把没登记过的 scope 挡掉（`NULL = 1` 不成立）；M6b 把整条约束（JOIN +
谓词）一起拿掉才会红。这条边界写进了用例的 docstring。

### B. 登记不查

#### 第 110 条 · 2017-11-05 的 33 张脸里 30 张 `validity='uncertain'`（待办，本轮不查）

**实测数字**（取自第 108 条那次真数据 e2e）：2017-11-05 那天检出**33 张脸**，其中
**30 张 `validity='uncertain'`**。

**两套口径，各记各的（2026-09-25 钉死）。** 上一轮写的是「这个数字复现不出来」——现在
把那句话也钉死：**不是有两个口径，是可复现的口径只有一个。**

| 口径 | 表与连接 | 过滤 | 结果 |
|---|---|---|---|
| **乙 · 本轮复核（可复现）** | `face_instances fi JOIN assets a ON a.id = fi.asset_id` | `a.scope_id = <scope>` ∧ `substr(a.captured_at, 1, 10) = '2017-11-05'` | **35 张脸 / 33 `uncertain`**，另 **2 张 `verified`** |
| **甲 · 第 108 条转述（不可复现）** | 第 108 条**没有写** | 第 108 条**没有写** | 33 张脸 / 30 `uncertain` |

**口径乙的原始输出**（`/tmp/count_calib.py`，只读，`/tmp/s5e2e-verdict` 与
`/tmp/s5e2e-identity` 两个库；**四个 scope 逐位相同**）：

```
--- scope=s5-after ---
  A file_name LIKE '2017-11-05%'      : faces=35 uncertain=33
  B substr(captured_at,1,10)     : faces=35 uncertain=33
  observations(captured_at=那天) : 16，其中带脸的 5
  只进 A 的资产（文件名前缀命中、captured_at 不是那天）: 0
  只进 B 的资产（captured_at 是那天、文件名前缀不命中）: 0

--- scope=s5-before ---（同上，逐位相同）
--- scope=album-a ---（同上）
--- scope=album-b ---（同上）
```

**口径甲为什么复现不出来——这一条也是量过的，不是猜的。** 把四种自然口径在**现存的全部
三个 e2e 库、四个 scope** 上各跑一遍（`/tmp/probe_calib.py`）：

```
/tmp/s5e2e-verdict/sentrix.db  scope=s5-after
  1 assets.captured_at              faces= 35 uncertain= 33 verified=  2
  2 observations.captured_at        faces= 35 uncertain= 33 verified=  2
  3 file_name LIKE                  faces= 35 uncertain= 33 verified=  2
  4 观察落在事件里(event_observations)  faces= 35 uncertain= 33 verified=  2
```

**四种全部是 35 / 33**（s5-before、album-a、album-b 同样）。所以「33 / 30」不落在任何一条
自然口径上；它更可能来自一次中间状态的运行，而那次运行的库已经不在盘上。**注意不是要把它
凑成 35 / 33**：两个数都留在这里，各自带口径，谁也不覆盖谁。

**同一件事的另两个数（口径乙）**：那天 **16 条观察**，其中**只有 5 条带脸**；35 张脸全部
来自这 5 张合影。下面「那天能进匹配池的只有 3 张」那句用的是**口径甲**，换到口径乙是
**2 张 `verified`**——两个数说的是同一件事（绝大多数脸进不了池），但**引用时要把口径带上**。

**将来引用这一天的脸数，必须连口径一起写**：写「35 / 33（口径乙）」或直接给出 SQL。
**口径没钉死的数字就是下一个争议**——这一条自己就是这句话的实例。

**为什么它是那天的大杠杆。** `identity_eligible` 是「够格进簇」那道门，它要求
`validity == 'verified'` 且 `quality >= 0.55`（`face_adapter.py:578`）。uncertain 的那些脸
**根本不参与匹配**：按第 108 条记的那组数字，那天能进匹配池的只有 3 张，而这 3 张还属于
别的簇。于是那天每一次 fold 的 person 分量本来就是 0，加不加预置的人都一样
（按本轮复核的那组数字，能进池的比例同样是极少数：35 张里 33 张被挡）。

**它与「事件归并没变好」的关系——不是主因，是同一天的第二条线索。** 第 108 条查到的主因是
**数据分布**：预置的那个人在 2017-11-05 一张照片里都没出现（那天 0 条提及）。两条叠起来才是
「那天分不出人」的完整图景：一条是「他不在」，一条是「就算他在，那天绝大多数脸也进不了
池子」（口径甲 30/33、口径乙 33/35，见上面的表）。
**只解掉一条，那天仍然分不出人**——这也是「它不是唯一原因」的确切意思。

**成因方向（未查）。** `validity` 全仓只有一个判断点（`face_adapter.py:549`）：

```python
validity = FACE_VALIDITY_VERIFIED if (
    score >= verified_score and agreed and sane and area_ratio >= verified_min_area
) else FACE_VALIDITY_UNCERTAIN
```

四个合取项是 SCRFD 的置信度（默认 0.7）、与 RetinaFace 的**框一致**、**关键点合理**、
人脸面积占比（默认 0.003）。下面这些比例都取自**口径甲**（第 108 条那组数；口径乙是
33/35，见上面的表——两套说的是同一件事）：**30/33 落进 uncertain**，意味着至少有一项在那
天的照片上普遍不成立——是检测质量（小脸 / 模糊 / 侧脸）、姿态分桶、还是光照，**未查**。

**待办（用户 2026-09-24 裁决：先记下不查）**：把那天的脸逐张落一份四元组
（`score` / `agreed` / `sane` / `area_ratio`），看是哪一项在挡。**脸数按口径乙取 35 张**
（口径甲那个 33 复现不出来，拿它去对会永远差 2）。**不据此调任何阈值**——与第 108 条结尾
是同一条纪律：调阈值是另一条产品决定，需要它自己的对照。

### C. 第 101 条那条 2.6 倍

#### 第 111 条 · 实测成因：真实仓库 venv 里的 onnxruntime 是 **CPU 版**（GPU 版被覆盖了）

**两次运行的原始数字**（日志都还在盘上）：

| 运行 | 日志 | pytest 结果 |
|---|---|---|
| 隔离副本 | `/tmp/ci-isolated.log` | `1064 passed ... in 1272.17s (0:21:12)` |
| 真实仓库 | `/tmp/ci-real.log` | `1064 passed ... in 3326.84s (0:55:26)` |

同一批 1064 条、同一棵树，差 **2.62 倍**。真实仓库上还有更早的一轮（`/tmp/real-ci.log`：
`1050 passed ... in 3496.72s (0:58:16)`，那是第 108 条之前的树），同样在 55 分钟这一档。

**根因：两个 venv 里的 `onnxruntime` 不是同一个发行包。**

| | 真实仓库 `.venv` | 隔离副本 `.venv` |
|---|---|---|
| `onnxruntime.get_device()` | `CPU` | `GPU` |
| `get_available_providers()` | `['AzureExecutionProvider', 'CPUExecutionProvider']` | `['TensorrtExecutionProvider', 'CUDAExecutionProvider', 'CPUExecutionProvider']` |
| `libonnxruntime.so.1.30.0` | 28,985,152 B · md5 `579ada21…` · mtime `09-23 21:43` | 29,062,976 B · md5 `bfea1df5…` · mtime `09-23 18:58` |
| 5 张真照片（预载 torch 后） | 2.41 s，session providers = `['CPUExecutionProvider']` | 1.15 s，session providers = `['CUDAExecutionProvider', …]` |

`apps/api/pyproject.toml:29-33` 自己写着这条陷阱：「insightface 依赖未加约束的
``onnxruntime``（CPU 版），两个发行包共用同一个 ``onnxruntime`` 命名空间，所以两个都钉在
**同一个版本**、且 GPU 那个最后装」。**「最后装」这一条在真实仓库的 venv 里没有成立**：
那个 venv 里整个 `onnxruntime/` 包目录的 mtime 是 `09-23 21:43`（副本是 18:58），
一次依赖安装把 CPU 版铺在了后面，`libonnxruntime.so` 因此被覆盖成 CPU 构建。此后每一次
`make ci` 里 `FaceAnalysis` 都请求 CUDA、实际落在 CPU 上——**静默退化**，正是
`onnx_runtime.py` 模块注释第 56 条描述的那一种（「日志里看不出来」）。

耗时差因此落在**跑真人脸检测的那几条**上：`make ci` 里走 onnxruntime 的真推理只有人脸检测
这一条链（VLM 观察器与文本模型在契约与 e2e 里都换成了假件；`chinese_clip.py` 那条走 torch，
两个 venv 都吃 GPU，不受这条影响）。它在真实仓库上落 CPU、在副本上落 GPU；其余一千多条
用例两边同样是 CPU，差别不在那里。

**处置：只登记，不动。** 这是**机器上的 venv 状态**，不是仓库里的代码；修法是让 GPU 版最后落
（`pip install --force-reinstall --no-deps onnxruntime-gpu==1.30.0`）。本条写下时按
「查清就好」的裁决**没有动真实仓库的 `.venv`**——那个状态正是那两次 55 分钟运行的复现条件。

**后记（同日 10:48）：那台 venv 已经被重装成 GPU 版。** `libonnxruntime.so.1.30.0` 换成了
`bfea1df5…` 那一份（与副本同一份），`onnxruntime.get_device()` 现在返回 `GPU`。所以上面那句
「没有动」说的只是本条写下时的状态。**修好之后，同一棵树的全量 `make ci` 是 20:08 /
`1066 passed`**（`/tmp/s5c/ci_real3.log`，`CI_EXIT=0`；比基线多出的 2 条是第 109 条新增的），
与副本的 21:12 同一档。

**给后来人留一条**：第 109 条落地期间那两次被外部信号杀掉的 `make ci`（都跑在 CPU 版下）
**不是基线**——55 分钟那一档是「`onnxruntime` 被覆盖成 CPU 版」的产物，不是这棵树应有的耗时。


## 2m. 收尾（2026-09-25）：CUDA 防线 + 三处尾

### A. 裁决一：建完人脸 session 就拒绝启动

#### 第 112 条 · CUDA 静默退化的防线：读 `session.get_providers()`，不一致就抛错拒启

**用户 2026-09-25 裁决**：「建完人脸 session 后，如果『请求了 CUDA 而实际只有 CPU』→
**直接报错拒启**」。理由是这条退化已经咬过三次：① `get_available_providers()` 里照样有
CUDA（第 56 条）；② insightface 的 `providers` 传元组被静默降级成 CPU（一行警告，
`_build_analysis` 的注释）；③ 第 111 条那次真实仓库的 venv 被依赖安装覆盖成 CPU 版，
`make ci` 21 分钟 → 55 分钟，而**外衣是「卡住」**（进度条过期 + `futex` 是 starlette portal
在等结果 + 172 线程是 onnxruntime 按 32 核开的正常池）。

**判据只能是 `session.get_providers()`。** `get_available_providers()` 在退化时给出**相反的
答案**，所以它既不能用来判、也不能用来「先看一眼就算修好了」。防线本体是
`onnx_runtime.require_requested_providers`：实际链里没有 `CUDAExecutionProvider` 而请求的链
里有 → 抛 `CudaProviderMissingError`。

**三个装配点都接了**（这是「落点在 session 建立处」的确切落法）：

| 落点 | session 从哪来 | 为什么不能只接一个 |
|---|---|---|
| `face_detector.RetinaFaceTiledDetector._load` | `onnxruntime.InferenceSession(model_path, providers=...)` | 检测器不经过 `FaceAdapter` 的 `_ensure_loaded` |
| `face_adapter._build_analysis` | `FaceAnalysis` 里**每个模块各自**的 `InferenceSession` | `FaceAnalysis` 自己**没有** `get_providers`——退化发生在两个模块会话上 |
| `face_adapter._load_buffalo_recognition` | w600k_r50 的独立 `InferenceSession` | 它的失败路径是 `return None`（合法降级），不显式放过就会被折进去 |

**两条容易被漏掉、都补了用例的副作用：**

1. **裸 `except Exception` 会把这道理吞掉。** 人脸链路上一共有六处，存在理由是「人脸是
   可选能力，不该让整条导入链失败」。这一条**不在那个理由里**——它说的是依赖装错了，不是
   这台机器没有 GPU。所以 `detect` 与 `_load_buffalo_recognition` 各加了一条**显式重抛**；
   吞掉的后果与没加防线一模一样（`detect` 返回空表，调用方看到「这张图里没有人脸」）。
2. **硬报错必须是黏的。** 两处「先赋值再守卫」会让报错只响一次、之后静默降级：
   `RetinaFaceTiledDetector._load` 先写 `self._session` 再守卫 → 第二次调用在
   `self._session is not None` 处 return；`_ensure_loaded` 先建 `_app` 再建识别 session →
   `_app` 非空后第二次直接返回、`_recognition_session` 永远是 `None`（而 `_uncertain` 对
   `None` 的处置正是返回空表）。两处都改成**过完守卫才赋值 / 识别 session 先建**。

**逃生阀：不留新的 env，就用 `provider_env` 自己。** 理由——「没有 GPU 的机器」与「这台机器
没请求 GPU」是两件事，而后者已经有一个显式开关了：把 `FACE_PROVIDERS` /
`RETINAFACE_PROVIDERS` 设成 `CPUExecutionProvider`，那时压根没有「请求了 CUDA」这回事，
防线不参与。**再加一个 `SENTRIX_..._ALLOW_CPU` 只会多一条「请求了 CUDA 但允许退化」的岔路，
而那正是要禁掉的组合**；而「纯 CPU 部署」这个合法形态并没有被堵死，只是必须由部署者写出来。
报错正文里直接给了这两条指示（装 GPU 版的 `pip install --force-reinstall --no-deps
onnxruntime-gpu==<当前版本>`，或显式声明纯 CPU），因为**只说「CUDA 没生效」的报错，下一个
人还得再查一遍**。

**`pyproject.toml` 同步。** 那条注释原来只描述陷阱（「两个发行包共用同一个命名空间、GPU 那个
最后装」），现在点明它同时是**报错里修复命令的版本来源**，并写明两处必须一起改——改一处，
另一处就成了谎话。

**门禁 14 条**（`apps/api/tests/unit/test_face_provider_guard.py`，不碰权重、不碰 GPU）：
判据本身 5 条（正向拿到 CUDA 不抛 / 反向 CPU 抛 / 报错可执行 / 逃生阀生效 / 两个变量不互相
顶替），三个装配点各正反两条，外加「黏」与「不被裸 except 吞」两条。

**定点变异（每一条都跑在 `MALLOC_ARENA_MAX=2 OMP_NUM_THREADS=4` 下，脚本 `/tmp/s5mutants.py`，
还原覆盖每一个被改的文件）：**

| 变异 | 改什么 | 指定用例 |
|---|---|---|
| M8 | 删掉 RetinaFace `_load` 里的守卫 | `test_the_detector_refuses_to_start_when_the_session_degrades_to_cpu` → **failed** |
| M9 | 删掉 `_build_analysis` 里的守卫 | `test_the_buffalo_analysis_refuses_to_start_when_a_module_session_degrades` → **failed** |
| M10 | 删掉 `_load_buffalo_recognition` 的守卫（退回 `return None`） | `test_the_standalone_recognition_session_refuses_instead_of_returning_none` → **failed** |
| M11 | 删掉 `detect` 里那条显式重抛 | `test_the_adapter_does_not_swallow_the_guard_in_its_broad_except` → **failed** |
| M12 | 删掉 `_load_buffalo_recognition` 里那条显式重抛 | `test_the_standalone_recognition_session_refuses_instead_of_returning_none` → **failed** |
| M13 | 把 `_app` 排回识别 session 之前 | `test_a_degraded_chain_keeps_refusing_on_every_later_call` → **failed** |
| M14 | 退化时不抛、改成记 `error` 后照常返回 | `test_the_detector_refuses_to_start_when_the_session_degrades_to_cpu` → **failed** |

原始输出：

```
### M8 · 去掉 RetinaFace 检测 session 的守卫
FAILED apps/api/tests/unit/test_face_provider_guard.py::test_the_detector_refuses_to_start_when_the_session_degrades_to_cpu
### pytest exit=1
### M11 · 让 detect 的裸 except 把守卫吞掉（删掉那条显式重抛）
FAILED apps/api/tests/unit/test_face_provider_guard.py::test_the_adapter_does_not_swallow_the_guard_in_its_broad_except
### pytest exit=1
### M13 · _ensure_loaded 里把 _app 排到识别 session 之前（报错不再黏）
FAILED apps/api/tests/unit/test_face_provider_guard.py::test_a_degraded_chain_keeps_refusing_on_every_later_call
### pytest exit=1
### M14 · 退化成 CPU 时不抛，改成记 error 后照常返回
FAILED apps/api/tests/unit/test_face_provider_guard.py::test_the_detector_refuses_to_start_when_the_session_degrades_to_cpu
### pytest exit=1
未达预期的变异数 = 0
```

**正向也在真机器上量过**（权重真、CUDA 真，不是假件）：

```
RetinaFace session providers = ['CUDAExecutionProvider', 'CPUExecutionProvider']
Applied providers: ['CUDAExecutionProvider', 'CPUExecutionProvider'], with options: {…}
find model: /home/asus/.insightface/models/buffalo_l/det_10g.onnx detection
find model: /home/asus/.insightface/models/buffalo_l/w600k_r50.onnx recognition
```

`test_face_detector.py` + `test_face_embedding.py` 共 22 条全绿（`PYTEST_EXIT=0`）。
`CUDAExecutionProvider` 那一行是 `session.get_providers()` 的实测输出——**这道理没有把
好机器拦在门外**，这正是「不许一律抛」那条正向用例要钉的东西。

### B. 订正一处撒谎的 docstring

#### 第 113 条 · `test_import_memory_space` 的反向用例红在 **M6b**，不是 M6

原 docstring 写「把内连接换成左连接，它会立刻变红」——**实测不成立**。`list_clusters` 的约束
是**两半**：`JOIN memory_spaces ms ON ms.id = fc.scope_id` ∧ `ms.include_in_people = 1`。
只换成 `LEFT JOIN`（M6）时，没匹配上的行被补 `NULL`、`NULL = 1` 不成立，**谓词自己就把它们
挡掉了**——两半在这个查询里**等价**，所以没有任何用例能把它们分开；要 M6b（整条约束一起
拿掉）才红。

原始输出（`/tmp/s5mutants.py`）：

```
### M6 · list_clusters 的 INNER JOIN 换成 LEFT JOIN（docstring 原来说它「立刻变红」）
### 文件 apps/api/src/sentrix/infrastructure/persistence/repositories/faces.py，期望 1 passed
### pytest exit=0
### M6b · 把整条 memory_spaces 约束（JOIN + 谓词）一起拿掉
FAILED apps/api/tests/integration/test_import_memory_space.py::test_a_cluster_can_exist_in_the_database_and_still_be_invisible_on_the_people_page
### pytest exit=1
```

**没有补新用例，理由写在订正后的 docstring 里**：想钉的是**那句话**，不是 `INNER JOIN` 这个
写法；而谓词那一半已经各有门禁（`test_identity_repositories.test_list_clusters_honours_include_in_people`
与 `test_identity_routes.test_clusters_from_a_hidden_space_never_enter_the_people_view`），
两半合起来才是那条约束。**再补一条「钉住内连接」的用例是做不到的**——那两半等价，任何用例
都分不开它们；硬写一条只会在下次换实现时变成一个假门禁。

### C. 裁决三：`ensure` 挪到校验之后

#### 第 114 条 · 被拒的预置请求**不留空间行**——位置按 1.0 的流程位置定，不按它的单行顺序

第 109 条把 `container.spaces.ensure(scope_id, scope_id)` 放在路由**开头**（任何写库之前），
并在那里登记了一处「已知副作用：404 会留下一行空空间」。**用户 2026-09-24/25 裁决：挪到校验
之后。**

**为什么这不是「与 1.0 说岔了」。** 第 109 条当时看的是一行代码的**单行顺序**（1.0 里
ensure 确实排在写之前），而它该看的是**流程位置**：1.0 的预置路由走 `create_asset`
（`app.py:2337`），ensure 在那个函数里（`db.py:1462`）——`create_asset` 是「建空间 + 建资产」
**在同一个流程里**，所以 1.0 里**一个什么都没建的请求根本走不到那一行**。照抄单行顺序反而
把这条语义丢了：404（`face_instance_ids` 指向不存在的实例）与 422（检不出人脸）都是在
**写任何东西之前**判出来的，把它们排到 ensure 之后，就等于「被拒的请求也建了空间」。
1.0 那行之所以在写之前，是因为它**后面紧接着就在写**，不是因为它该排在所有校验之前。

**落点仍然是两条入口的公共点**（第 102 条的同一条理由）：实例那条入口一张资产都不写，
接在 `_store_seed_photo` 上会漏掉它。挪动只改变它相对**校验**的位置。

**门禁**：`test_identity_routes.test_a_rejected_seed_leaves_no_space_row`（只走 HTTP：404 之后
`container.spaces.get(FRESH) is None` 且该 scope 一条簇都没有）。它与第 109 条那条正向用例
**成对**——只留正向的话，把 ensure 挪回开头同样全绿。

**定点变异 M7：把 ensure 挪回路由开头 → 这条变红**（原始输出见 `/tmp/s5mutants.py`）：

```
### M7 · 把 ensure 挪回路由开头（第 114 条之前的位置）
### 文件 apps/api/src/sentrix/api/routes/identity.py，期望 1 failed
FAILED apps/api/tests/contract/test_identity_routes.py::test_a_rejected_seed_leaves_no_space_row
### pytest exit=1
```

**仍未消掉的一处失败残留（登记不改）**：照片入口在 `SeedIdentity.execute` 判 422 之前就已经
把资产与观察落了库（`_store_seed_photo` 在路由里、在 `execute` 之前）。那一处的修法要么把
照片写进用例层、要么给整个请求开事务——两者都超出这一条的边界，且与 1.0 的「先写资产再
判」同形。**这一条只消掉空间行那一处。**

### D. 本轮 `make ci` 的原始状态：**环境红**，不是代码红

第一次完整 `make ci` 的原始结果（`/tmp/s5c/ci_real-20260925-001811.log`，
`MALLOC_ARENA_MAX=2 OMP_NUM_THREADS=4`，跑在真实仓库）：

```
### 起跑 2026-09-25T00:19:41+08:00 load1=0.55
ruff check → All checks passed!   ruff format --check → 223 files already formatted
mypy → Success: no issues found in 137 source files
lint-imports → Contracts: 5 kept, 0 broken.
check_reachability → 0 个问题   check_capability_map → 0 个问题
check_module_map → 0 个问题     export_openapi --check → openapi 契约一致
14 failed, 1051 passed, 16 skipped, 11 warnings in 452.14s (0:07:32)
make: *** [Makefile:56：coverage] 错误 1
### CI_EXIT=2
### 耗时 457 秒（7分37秒）
```

**30 条异常分两族，同一个环境变化：**

| 族 | 条数 | 文件 | 直接原因 |
|---|---:|---|---|
| 失败 | 14 | `e2e/test_visual_search.py`(6) · `integration/test_face_detector.py`(6) · `integration/test_face_embedding.py`(2) | `torch.OutOfMemoryError` / `onnxruntime ... Fail: Failed to allocate memory` —— GPU0 满了 |
| 跳过 | 10 | `integration/test_chinese_clip.py` | 同一个 OOM：`ChineseClipVisualEmbedder` 建不起来 → `available` 假 → 夹具 `pytest.skip("chinese-clip 权重不可用: OutOfMemoryError…")` |
| 跳过 | 6 | `e2e/test_assistant_turn.py` | 8100 的 `/v1/chat/completions` 返回 **404**（同期 `/v1/models` 仍 200） |

**GPU0 的实况**（`nvidia-smi`，跑 CI 时与事后各看一次，逐位相同）：24576 MiB 里用了 22718，
只剩 **1409 MiB**。占用者：

| 进程 | 身份 | 占用 |
|---|---|---:|
| 2022870 `VLLM::EngineCore`（父 2022624 = 监听 8100 的 `vllm`） | 8100 / 8500 | 19092 MiB |
| 1507974 | 1.0 的 8091 | 2458 MiB |
| 3623789 | 2.0 的 9598 | 1134 MiB |

**基线那一次（第 111 条后记的 20:08 / `1066 passed` / 0 skipped）跑在 vLLM 起来之前**：
`VLLM::EngineCore` 的 `etime` 起算于 2026-09-24 14:47，而基线是 11:33。所以 7 分 32 秒 / 14 red
**不是「这棵树该有的另一档」**，是机器状态变了——第 111 条那条「55 分钟不是基线」的提醒，
这里是同一个形状。

**证明它与本次改动无关（量出来的，不是推出来的）**：把工作树 `git stash push -u` 回
`dcfb5cf`（未改动），跑同一批文件：

```
### 在**未改动**的树上跑同一批文件
### BEFORE_TREE_EXIT=1  耗时 24s
FAILED apps/api/tests/e2e/test_visual_search.py::… (5 条)
FAILED apps/api/tests/integration/test_face_detector.py::… (6 条)
FAILED apps/api/tests/integration/test_face_embedding.py::… (2 条)
### 还原 OK（git status 与 stash 前逐行相同）
```

**逐条同样红。** 反过来，把单个文件放进**全新的进程**跑（没有同进程里 CLIP 先抢显存）就绿：

```
apps/api/tests/integration/test_face_detector.py → 6 passed
    RetinaFace session providers = ['CUDAExecutionProvider', 'CPUExecutionProvider']
apps/api/tests/integration/test_chinese_clip.py  → 10 passed, 0 skipped
apps/api/tests/unit + tests/contract + test_import_memory_space + test_identity_repositories
                                                 → 全绿（TOUCHED_EXIT=0）
```

第一条那个 `['CUDAExecutionProvider', …]` 还顺带正面证明了第 112 条的防线**没有把好机器
拦在门外**：真拿到 CUDA 时它放行。

**处置：不动。** 8100 / 8500 / 8091 都不在可动范围内（用户明令），而且这不是这棵树的缺陷。
**下一次拿「1066 passed / 0 skipped / 20 分钟」当基线之前，先看 `nvidia-smi` 的
`memory.free` 与 8100 的 `/v1/chat/completions`。**

## 2n. 收尾（2026-09-25）：vLLM 与 llama.cpp 都当后端 + 规划失败的文案分岔

用户 2026-09-25 的两条裁决，起因是一次**实测**：8100 上跑的已经从 llama.cpp 换成 vLLM
（`gemma4-12b-it`，有人 09-24 14:47 起，占 19092 MiB），2.0 配的 `chat_model_name =
"gemma4-qat-iq3xxs"` 于是 404：

```
{"error":{"message":"The model `gemma4-qat-iq3xxs` does not exist.","type":"NotFoundError",
"param":"model","code":404}}
```

`planner_call_error` → `planner_blocked` → 用户看到「当前问题的证据需求无法可靠规划，因此
暂时无法确认。」

**日志把真因留全了**（`ChatModelError: 模型返回 404：{...}` + `规划失败: reason=planner_call_error`）。
问题不在日志，在两处：**常量绑定了后端**，以及**给用户的文案把基础设施故障说成了理解能力
不足**。

### 裁决一：同一份客户端，vLLM 与 llama.cpp 都要能用

两个后端的差别只有一处：**校不校验模型名**。llama.cpp 不校验（随便传都能用），vLLM 校验
（名字对不上 404）。所以写死的常量等价于「把这个客户端钉在写常量时的那个后端上」，而
换后端是**运维动作**，代码这边一点动静都没有。

**1.0 已经有先例，照它的形状做**：

| 位置 | 做法 | 这次用/不用 |
| --- | --- | --- |
| `backend/model_clients.py:522-570`（`GammaClient.__init__`） | `SENTRIX_LLM_BACKEND` 开关 + 每个后端一份默认名字（`openai` → `gemma4-12b-it`，`ollama` → `gemma4:12b`） | **不用**。它解决的是「协议不同」（Ollama 走 `/api/chat`、payload 形状也不同）。这两个后端协议完全相同，再用开关就是拿配置追运行时状态——`config.py` 自己点名的坏法 |
| `backend/runtime_providers.py:416-439`（`OpenAICompatibleInferenceProvider.health` / `list_models`） | 打 `GET {base_url}/models`，取 `body["data"][].id` | **用**。形状逐条照搬（含去空 id），但 1.0 只把它当健康检查的副产物、用哪个名字仍由配置决定；2.0 把它当**唯一**的名字来源 |
| `backend/model_clients.py:295-360`（`OllamaBackend`，`/api/chat`） | 另一套协议 | 不碰（Ollama 在「不碰」名单里） |
| `backend/model_clients.py:455-500`（`E2BBackend`） | 已退役（冻结） | 不碰 |

**落地**：新增 `infrastructure/assistant/model_endpoint.py` 的 `ServedModel`——问端点
`GET /v1/models`，只认 `data[].id`，去重（多副本 vLLM 同名字不算「多个模型」）。

- **探测失败一律抛 `ModelServiceError`**（domain 的 `domain/assistant/ports.py` 新声明），
  不猜、不退回常量、不静默：端点不可达 / 非 200 / 不是 JSON / 一个模型都没有 / 多于一个，
  五种全报，文案里带端点地址。多于一个**不挑一个「看起来对」的**——挑错了就是 404，而且
  没有依据说该挑哪个。
- **`chat_model_name` / `vlm_model_name` 两个配置项删掉**。留着它们等于留一个「看起来
  能配、其实请求不看它」的口子——正是 `config.py` 开头写的那种「配置与代码互相矛盾」。
  反向门禁：`apps/api/tests/unit/test_config.py::test_no_model_name_is_configurable`。
- **视觉观察器同样接上**（`OpenAIVlmObserver`，与对话同一个 8100、同一个常量）。它的
  失败本来就按「观察是增强」降级成 `None`，但那条 `log.warning` 里现在带着端点地址与原因
  ——降级是设计好的，静默不是。

**缓存策略（成本在这儿）**：每个回合至少一次规划 + N 次主循环，每次都探会把请求数翻倍。
所以进程内缓存，TTL 60 秒；**TTL 不是唯一的失效路径**——请求撞上 404 时作废缓存重探一次，
**只有重探拿回来的名字真的变了才重发**（没变说明这个 404 是别的意思，拿同一个名字再问一次
只会拿到同一个错）。于是「换后端」的可见窗口不是 60 秒，而是**换完之后的第一个请求**。
对话与视觉**共用同一个 `ServedModel`**（同一端点，分两份会存两个可能不一致的答案）。

### 裁决二：`planner_call_error` 下的两种情形，文案必须分开

原来是**一条**文案：`PLANNER_BLOCKED_ANSWER = "当前问题的证据需求无法可靠规划，因此暂时
无法确认。"`（1.0 生产库 2,126 条 `planner_blocked` 的 answer，逐字）。

现在按 `fallback_reason` 分岔（`run_turn._planner_failure_answer`）：

| 失败原因 | 用户看到 | 说明 |
| --- | --- | --- |
| `planner_call_error`（端点不可达 / 超时 / 名字对不上 / 多个模型） | 「模型服务当前不可用，因此这一轮没有开始检索。（模型端点 http://… 不可达：…）」 | 基础设施故障。带上端点自己的说明（含地址），用户与运维据此能直接去查 |
| `invalid_planner_action` / `scope_mismatch` / `no_requirements` | 原句不动 | 模型答了，但规划结果不可用——这才是规划问题 |

**`termination_reason` 仍是 `planner_blocked`**（两条都是）：裁决的原话是「在
`planner_call_error` 这个内部原因下，两种情形给不同的用户可见文案」，而且 1.0 的库拿
`planner_blocked` 记这类失败，保持可比。变的只有用户可见文案。

**反向门禁**：`test_the_two_planner_failures_do_not_share_a_message` 不看具体措辞，只断言
两者**不是同一句**——将来谁把它们并回去，无论改成什么字都会红。


### 本次交付的 `make ci`（改动的树上，2026-09-25T09:52:48+08:00 起跑，load1=1.85）

```
All checks passed!                    # ruff
225 files already formatted           # ruff format --check
Success: no issues found in 138 source files   # mypy strict
Contracts: 5 kept, 0 broken.          # lint-imports
reachability: 0 个问题
capability-map: 0 个问题
module-map: 0 个问题
openapi 契约一致
18 failed, 1075 passed, 10 skipped, 11 warnings in 495.97s (0:08:15)   # pytest --cov
make: *** [Makefile:56：coverage] 错误 1
```

**CI_EXIT=2，耗时 499 秒（8分19秒）。** 非 pytest 的门禁全绿；红只红在 pytest，而且
**18 条全是环境红，没有一条是代码红**。链子停在 `coverage`，`web-check` 没跑到。

#### 14 条：GPU 被 vLLM 占满（与前一轮同因，机器状态，不是这棵树）

```
FAILED apps/api/tests/e2e/test_visual_search.py::…                (6 条)
FAILED apps/api/tests/integration/test_face_detector.py::…        (6 条)
FAILED apps/api/tests/integration/test_face_embedding.py::…       (2 条)
```

报错是被测代码**引擎内部**的分配失败，不是断言失败：

```
[E:onnxruntime:, inference_session.cc:3106 operator()] … CUBLAS failure 3:
the resource allocation failed ; GPU=0 ; expr=cublasCreate(&cublas_handle_);
```

`nvidia-smi` 实测：`24111 MiB / 24576 MiB` 已被占用，其中 19092 MiB 是**别人**在 8100 上
起的 vLLM（2026-09-24 14:47，`vllm serve … --gpu-memory-utilization 0.76`）。这三个文件
与本次改动没有任何交集——`git diff --name-only` 里没有它们依赖的任何生产文件。

#### 4 条：`test_assistant_turn` 从**跳过**变成**失败**——是本次改动把掩护撤掉了

```
FAILED apps/api/tests/e2e/test_assistant_turn.py::test_asking_for_photos_returns_openable_originals
FAILED apps/api/tests/e2e/test_assistant_turn.py::test_the_answer_never_leaks_internal_identifiers
FAILED apps/api/tests/e2e/test_assistant_turn.py::test_the_turn_records_the_whole_reasoning_chain
FAILED apps/api/tests/e2e/test_assistant_turn.py::test_a_follow_up_question_sees_the_previous_turn
```

这四个文件的 module 级守卫原本拿 `Settings().chat_model_name`（写死的旧名字）去打 8100，
必然 404，于是**整层 e2e 被静默跳过**——第 112-114 条那次 CI 记录的「6 条因为 404 而跳过」
就是它们。守卫改成与生产同一条路（`ServedModel`）之后名字对了，守卫放行，这四条**第一次
真的跑起来**，于是暴露出一个此前没人看见的、**比模型名更硬的 vLLM 差异**：

```
WARNING sentrix.assistant.run_turn: 模型调用失败: 模型端点 http://127.0.0.1:8100/v1 上的
gemma4-12b-it 返回 400：{"error":{"message":"\"auto\" tool choice requires
--enable-auto-tool-choice and --tool-call-parser to be set","type":"BadRequestError",…}}
```

直接对 8100 实测（`.venv/bin/python /tmp/probe_tools.py`）：

```
无 tools                  : 200
tools + tool_choice=auto  : 400  "auto" tool choice requires --enable-auto-tool-choice …
tools，不给 tool_choice    : 400  （同上——vLLM 在带 tools 时默认就是 auto，省略躲不掉）
tools + tool_choice=required: 400  tool_choice="required" requires --tool-call-parser to be set
```

**只要请求里带 `tools` 就 400**，与客户端怎么填 `tool_choice` 无关。8100 的实际启动命令
（`/proc/2022624/cmdline`）里没有这两个 flag：

```
vllm serve /home/asus/models/gemma-4-12B-it --host 0.0.0.0 --port 8100
  --served-model-name gemma4-12b-it --dtype bfloat16 --quantization bitsandbytes
  --load-format bitsandbytes --tensor-parallel-size 1 --gpu-memory-utilization 0.76
  --max-model-len 8192 --max-num-seqs 12 --trust-remote-code --mm-processor-cache-gb 0
  --limit-mm-per-prompt {"image":5,"video":0,"audio":1}
```

llama.cpp 是**原生**支持 tools 的（1.0 ``chat_client`` 的模块注释：「实测 8100 的 llama.cpp
原生支持 tools，``finish_reason="tool_calls"``」），所以这一条在 llama.cpp 时代不存在。它是
裁决一那份「两个后端都要能用」清单里的**第三处差异**（前两处：模型名校不校验、launcher
参数），而且**客户端修不了**：vLLM 没带 tool parser 就是不能吐 tool_calls，2.0 又不管理
8100 的进程（「2.0 只连它，不管理它」）。

**处置：不动。** 8100 在用户明令的「不要碰」名单里，重启它不在本次授权内。**要让 vLLM 这条
后端真正可用，8100 必须以 `--enable-auto-tool-choice --tool-call-parser <parser>` 重启**
（parser 按模型选，gemma 系通常 `pythonic`/`hermes` 之一，需实测确认）。在那之前，2.0 的助手
**能规划、不能跑工具循环**——见下面那次实测。
### 实测：9598（旧进程）vs 8193（本次改动的代码）问同一句话

9598 是 09-23 起就挂着的进程，跑的是改动前的代码，**不能重启**（用户明令）。所以另起一个
实例（8193）用**本次改动的工作树**、`SENTRIX_DATA_DIR` 指向 `data/` 的副本（不动真 `data/`，
用完即删），两边问同一句「给我看看海边的照片」：

```
=== 改动前的进程 9598（09-23 起，跑的是旧代码） ===
### BEFORE (http://127.0.0.1:9598) conversation=conv_9645ccf2f8ed turn=turn_20260925020240609107
status            : partial
termination_reason: planner_blocked
answer            : 当前问题的证据需求无法可靠规划，因此暂时无法确认。
attachments       : 0

=== 改动后的代码 8193 ===
### AFTER (http://127.0.0.1:8193) conversation=conv_0cd8b7a9c876 turn=turn_20260925020242716828
status            : failed
termination_reason: model_error
answer            : 这一轮没有检索到照片。还差这些没能确认：包含海边场景的照片集合；将海边的照片展示给用户。（本轮因「模型不可用」提前结束。）
attachments       : 0
```

**9598 的日志（真因一直是留全的）**：

```
sentrix.infrastructure.assistant.chat_client.ChatModelError: 模型返回 404：
  {"error":{"message":"The model `gemma4-qat-iq3xxs` does not exist.","type":"NotFoundError",…}}
规划失败: turn=turn_20260925020240609107 reason=planner_call_error
```

**8193 的日志（规划那一步已经过去了）**：

```
模型调用失败: 模型端点 http://127.0.0.1:8100/v1 上的 gemma4-12b-it 返回 400：
  {"error":{"message":"\"auto\" tool choice requires --enable-auto-tool-choice and
   --tool-call-parser to be set",…}}
```

**读法：**

- `规划失败` / `planner_call_error` **不再出现** → 裁决一在真环境里成立：名字是问来的，
  404 消失。8193 的回答里那两条「还差这些没能确认：包含海边场景的照片集合；将海边的照片
  展示给用户」正是**规划器的输出**——规划成功地跑完了，这一点由它自己证明。
- 失败点从**规划**移到了**工具循环**，原因是 8100 缺 tool-call 启动参数（见上一节），
  不是本次改动的缺陷，也不是 2.0 能在客户端修的。
- 9598 那句「当前问题的证据需求无法可靠规划」正是裁决二要改掉的**基础设施故障被说成理解
  能力不足**；改动的树上这条路径由 `test_a_planner_that_cannot_be_reached_says_the_model_
  service_is_down` 与契约测试 `test_a_dead_endpoint_is_reported_as_the_model_service` 钉住
  （都带端点地址）。

**遗留（如实）**：工具循环那条收尾走的还是 1.0 的 `MODEL_ERROR` 文案「这次没能完成检索：
模型不可用。」——它说的是模型服务（口径正确），但**没带端点、没带服务端给的原因**，不如
规划那条可执行。裁决二的原文只点了 `planner_call_error`，所以本次没有顺手改它；要不要按
同一条理由推广到 `MODEL_ERROR`，留给你定。

---

## 2o. 收尾（2026-09-25 续）：8100 的 vLLM 打开 tool calling + 工具循环失败文案可执行

§2n 末尾那条「遗留（如实）」里的两件事，用户 2026-09-25 裁决落地：

1. 8100 以 `--enable-auto-tool-choice --tool-call-parser <parser>` 重启（§2n 写明的必要条件）；
2. `MODEL_ERROR` 那条收尾文案按与裁决二同一条理由改成可执行的。

### 影响面：8100 上还有谁（动共享资源前先查）

```
8100  vLLM gemma4-12b-it，pid 2022624（重启前）→ 2596792（重启后）
8091  1.0 后端（Sentrix-Home-Web/.venv，env：SENTRIX_OPENAI_API_MODE=vllm、
      SENTRIX_VLLM_API_URL=http://127.0.0.1:8500、SENTRIX_VLLM_REGISTRY=/home/asus/sentrix-vllm/registry.json）
9598  2.0 后端（SENTRIX_DATA_DIR=<2.0 仓库>/data）
8192  s5verify 的 2.0 实例（SENTRIX_DATA_DIR=/home/asus/s5verify/data-web-verify）
```

**没有远程调用方**：重启前翻 8100 的访问日志（`log/gemma4-12b-it.log`），所有
`POST /v1/chat/completions` 的 peer 都是 `127.0.0.1`，没有一条来自别的机器；`ss -tn` 也没有
到 8100 的连接。所以影响面就是上面三个本机后端 + 本机上临时跑的脚本。

**1.0 的 8091 与 2.0 的 9598、8192 在重启窗口内拿不到模型**（不是它们的代码问题：它们都只是
HTTP 客户端，端点回来即恢复）。用户已裁决重启，故执行；`8091` 事后仍在（见文末）。

重启走 **manager**（`8500` 的 `POST /switch`），不自己 `kill` + 手起 vLLM：2.0 不管理模型
进程生命周期，这是 manager 的职责（`domain/assistant/ports.py` 的 `ChatModel` 注释：
「2.0 只连它，不管理它的进程」）。

### flag 落到 registry 的哪一步（含仓库版 vs 部署版的处置）

**结论：只改 registry，两边的 manager 代码一行都不用动。**

`build_command` 早就有 `extra_args` 这条通路（部署版 `sentrix_vllm_manager.py:280`、
1.0 仓库版 `services/vllm_manager/manager.py:283`，两份同一行）：

```python
command += [str(item) for item in profile.get("extra_args", [])]
```

两份的差异只有 4 处、全是环境相关（registry 默认路径、CUDA 的 `PATH`/`CPATH`、 
`external_url_hint` 里的主机名），`extra_args` 的使用**逐字相同**。所以不存在「改哪个副本
才对」的问题——需要改的是**它们共同读的那个文件**。

**实际跑的是哪一份**：`8500` 的 `sentrix_vllm_api.py` 起 `subprocess` 调
`sentrix_vllm_manager.py`（`MANAGER` 常量，可用 `SENTRIX_VLLM_MANAGER` 覆盖；pid 1268 的
environ 里没有这个覆盖），而 `REGISTRY` 默认 `/home/asus/sentrix-vllm/registry.json`，
pid 1268 也没有 `SENTRIX_VLLM_REGISTRY` 覆盖 → **读的就是部署版 registry**。

**仓库版 registry 是死副本，且不在 2.0 仓库里**：

| 位置 | 谁读 | 本次动没动 |
| --- | --- | --- |
| `/home/asus/sentrix-vllm/registry.json`（15 profiles，部署版） | 8500 → manager；8091 也显式指这里 | **动**（备份 `registry.json.bak-before-toolcall-20260925`） |
| `Sentrix-Home-Web/configs/sentrix_vllm_registry_192_168_0_153.json`（13 profiles） | 只有「没设 `SENTRIX_VLLM_REGISTRY` 时」的 manager 默认值会用 | **不动**——1.0 仓库只读 |
| 2.0 仓库 | —— | **`services/vllm_manager/` 在 2.0 仓库里不存在**（任务书假设它在，实测没有）；2.0 侧的 `apps/photobench/config/vllm_targets.json` 是 benchmark 的**客户端目标表**，与模型启动参数无关 |

所以本次的落地物是**部署版 registry 的一个字段**，没有任何仓库文件承载它。这是要说的
顾虑（见文末）：**`registry.json` 不进版本控制**，下次谁把 1.0 仓库那 13-profile 快照当权威
铺回去，这两个 flag 就又没了。

改动（`profile.gemma4-12b-it`）：

```json
"extra_args": ["--enable-auto-tool-choice", "--tool-call-parser", "gemma4"]
```

`notes` 里同时写上了为什么——这个字段是给人看的，不然下次有人清理「没见过的字段」时会顺手
删掉。落到了 registry 而不是一次性命令行，所以**下次 manager 重启/switch 仍然带着**：

```
$ .venv/bin/python sentrix_vllm_manager.py cmd gemma4-12b-it
... --limit-mm-per-prompt '{"image":5,"video":0,"audio":1}' --enable-auto-tool-choice --tool-call-parser gemma4
```

重启后的 `state/current.json` 里 `command` 数组末尾也带着这两个 flag（`8500/state` 可直接查）。

### parser 实测选择：`gemma4`，一次选中

**没有猜，也没有挨个试。** 先读模型的 `chat_template.jinja`：它自己声明吐的是
`<|tool_call>call:name{args}<tool_call|>`（`template:247-264`）；再读 vLLM 0.26.0 的
tool parser 目录，`gemma4_engine_tool_parser`（注册名 `gemma4`）的 `gemma4_utils` 文档字符串
逐字写着 `Standard format: <|tool_call>call:name{args}<tool_call|>`——为这个模板而写。
在此之前先用 `gemma4_utils.parse_tool_calls` 离线喂了两条该格式的样本，确认能解析出
`{'name': ..., 'arguments': {...}}`，才去重启。所以 `pythonic` / `hermes` / `functiongemma`
一次都没用上。

重启后的实测（`/tmp/probe_tools_2.py`，四个组合与 §2n 那张表一一对应）：

```
model = gemma4-12b-it
无 tools                         : 200
tools + tool_choice=auto        : 200   finish_reason=tool_calls
    tool_calls = [{"id": "chatcmpl-tool-b38a6ed32907d2e1", "type": "function",
                   "function": {"name": "search_memories", "arguments": "{\"query\": \"海边\"}"}}]
    content    = None
tools，不给 tool_choice          : 200   （同上，形状一致）
tools + tool_choice=required    : 200   （同上，content 是 "" 而不是 None）
```

`id` / `type` / `function.name` / `function.arguments` 四件齐全，`arguments` 是 **JSON 字符串**
（OpenAI 约定的形状，`chat_client._read_tool_calls` 正是按字符串收的）。

再把两个更硬的地方也测了（`/tmp/probe_loop_2.py`）：

```
=== A. 强制命名工具 get_original_photos（数组参数）===
  HTTP 200  finish_reason = stop
  arguments repr = '{"handles": ["photo_1", "photo_2", "photo_3"]}'   # 是 JSON，不是 Python 的 repr
=== B. 完整往返：search → tool 结果 → 模型收尾 ===
  第 1 轮  finish_reason=tool_calls  content=None   search_memories{"query": "海边的照片"}
  第 2 轮  finish_reason=tool_calls  content=None   get_original_photos{"handles": ["photo_1", "photo_2"]}
```

第 2 轮是**带着 `content=null` 的 assistant tool_calls + 一条 role=tool 消息**回去问的，
正是 `chat_client._dump_message` 拼的形状（§2n 记过：`content` 写成 `""` 会让 gemma4 模板
在工具结果回填后直接吐 EOS）。它没吐 EOS，而且自己接着调了第二个工具。

### 2.0 助手的完整回合（8193，本次工作树 + `data/` 的副本）

```
### http://127.0.0.1:8193  conversation=conv_76afdb1cb109 turn=turn_20260925022031499325
question          : 给我看看海边的照片
status            : complete
reason(in result) : answered
answer            : <|channel>thought
                    <channel|>这是为您找到的关于“海边”的照片。
delivered photos  : 6
    - handle=photo_1 asset=asset_75916c8eae1d file=2017-11-05 150755.jpg
    - handle=photo_2 asset=asset_e3798af44527 file=2017-11-05 161103.jpg
    - handle=photo_3 asset=asset_ed4863389a07 file=2017-11-05 164457.jpg
    - handle=photo_4 asset=asset_f0c6509d525f file=IMG_8654.JPG
    - handle=photo_5 asset=asset_1984dc3ad382 file=IMG_20230806_205817.jpg
    - handle=photo_6 asset=asset_71615f8e6aaf file=IMG_20230806_205755.jpg
requirements      :
    - [satisfied] 包含海边场景的照片集合
    - [satisfied] 将海边的照片展示给用户
progress (tool steps):
    - planning/complete [plan]      已确认需要哪些证据。
    - action/running     [model_action] 正在执行 search_memories…
    - tool_result/ok     [tool_result]  结果集里共有 12 张候选照片。
    - action/running     [model_action] 正在执行 get_original_photos…
    - tool_result/ok     [tool_result]  已把 6 张照片交给用户。
```

这是 §2n 那次 `failed` / `model_error` 的**直接对照**：同一条链路（规划 → 工具循环 →
证据账本 → 答案门禁 → 回答）这次跑完了，两条需求都 `satisfied`，6 张照片真的交付了。
`9598` 与 `8192` 都没重启（用户明令），8193 用的是本次工作树的代码、`SENTRIX_DATA_DIR`
指向 `/tmp/sentrix-8193-data`（`data/` 的 `cp -a` 副本，真 `data/` 全程没动）。

**这条实测把 §2n 的判断正面证伪了一半**：§2n 写「在那之前，2.0 的助手能规划、不能跑工具
循环」——parser 一挂上，工具循环立刻走通，客户端那边一个字都没改，再次说明缺的是
**服务端的启动参数**，不是 2.0 的代码。

### 裁决二：工具循环的失败文案改成可执行的

§2n 末尾那句中肯的自陈是这次改动的起点：

> 工具循环那条收尾走的还是 1.0 的 `MODEL_ERROR` 文案「这次没能完成检索：模型不可用。」——
> 它说的是模型服务（口径正确），但**没带端点、没带服务端给的原因**，不如规划那条可执行。

改动三处，与规划那条 `_planner_failure_answer` 同一条边界：

| 位置 | 改了什么 |
| --- | --- |
| `domain/assistant/emergency.py` 的 `_REASON_LABEL[MODEL_ERROR]` | `"模型不可用"` → `"模型服务不可用"`。底下是 `ModelServiceError`（端点不可达 / 超时 / 名字对不上 / 服务端 400），「模型不可用」听着像模型能力不行 |
| `domain/assistant/emergency.py` 的 `fallback_answer` | 新增 `detail: str = ""`，非空时接在尾句里：`（本轮因「模型服务不可用」提前结束：<detail>。）` |
| `application/assistant/run_turn.py` 的 `_converse` / `_stop` | except 里按 `isinstance(error, ModelServiceError)` 取 `str(error)` 交给 `_stop(..., detail=...)` |

**只收 `ModelServiceError`**，与 `goal_planner.py:97` 给 `PlannerResult.detail` 设的边界是同
一条：别的原因（2.0 自己拼错了请求）不往里塞，免得把内部形状说给用户听。

真实文案（用上面那个真 400 串渲的，`emergency.fallback_answer` 直出）：

```
BEFORE 这一轮没有检索到照片。还差这些没能确认：包含海边场景的照片集合；将海边的照片展示给用户。（本轮因「模型不可用」提前结束。）
AFTER  这一轮没有检索到照片。还差这些没能确认：包含海边场景的照片集合；将海边的照片展示给用户。（本轮因「模型服务不可用」提前结束：模型端点 http://127.0.0.1:8100/v1 上的 gemma4-12b-it 返回 400：{"error":{"message":"\"auto\" tool choice requires --enable-auto-tool-choice and --tool-call-parser to be set","type":"BadRequestError","param":null,"code":400}}。）
```

端点、状态码、**服务端给的原因**（连缺哪个 flag 都写着）三样都在。

**钉住它的用例**（都带端点断言，与规划那条 `test_the_model_service_message_carries_the_endpoint` 对齐）：

- `unit/test_assistant_guard.py::test_the_fallback_carries_the_service_side_reason_when_there_is_one`
- `unit/test_assistant_guard.py::test_the_fallback_invents_no_endpoint_when_there_is_no_detail`
- `integration/test_run_turn.py::test_a_tool_loop_model_service_failure_carries_the_endpoint`
- `integration/test_run_turn.py::test_an_internal_failure_does_not_leak_its_wording_into_the_answer`
- `contract/test_assistant_events.py::test_a_model_that_dies_after_planning_is_reported_as_failed`
  （假件 `DiesAfterPlanning` 跟着改成抛 `ModelServiceError`，文案就是那句真 400——与
  `FailingModel` 同一条理由：假件比真件宽松，就验不出「上层只认那个类型」这类问题）

**定点变异带反向门禁**（两条都改坏、跑完改回来）：

```
### M1 · 兜底忽略 detail（服务侧说明又掉回日志）
    FAILED unit/…::test_the_fallback_carries_the_service_side_reason_when_there_is_one
    FAILED integration/…::test_a_tool_loop_model_service_failure_carries_the_endpoint
    FAILED contract/…::test_a_model_that_dies_after_planning_is_reported_as_failed
    exit=1  红（拦住了）
### M2 · 反向门禁：任何异常都往文案里塞（内部形状泄漏给用户）
    FAILED integration/…::test_an_internal_failure_does_not_leak_its_wording_into_the_answer
    exit=1  红（拦住了）
```

### 顾虑（如实）

**`<|channel>thought\n<channel|>` 漏进了用户可见的答案里。** 上面那次完整回合的 `answer`
头一行就是这个空思考通道。原因是 8100 没有配 `--reasoning-parser`，gemma4 模板的
`<|channel>thought … <channel|>` 就留在 `content` 里（vLLM 侧确有对应的
`gemma4_engine_reasoning_parser`，注册名同为 `gemma4`）。**本次没有顺手加**：用户的裁决是
「带上那两个 flag」，而这第三个 flag 会改掉 **8091 那边 1.0 的答案**（思考通道一并被摘掉），
影响面比 tool calling 大，且要再付一次 8100 的重启窗口。**留给用户裁决**——一行 flag 的事，
但它已经超出本次授权。

**`registry.json` 不进版本控制。** 这两个 flag 目前只活在 153 的
`/home/asus/sentrix-vllm/registry.json` 里；1.0 仓库那份 13-profile 的
`configs/sentrix_vllm_registry_192_168_0_153.json` 是只读的、本次没同步。将来谁拿那份快照
覆盖部署版，tool calling 就又没了，而且症状与 §2n 一模一样（带 tools 的请求 400），
排查过一遍的人不用再排查第二遍——但没排查过的人会。要根治得让 registry 有版本控制或
让 manager 读 2.0 仓库里的一份。

### 动过共享资源之后的必查项：1.0 的 8091 仍然活着

8100 是 1.0 的 8091 与 2.0 的 9598 共用的。重启窗口结束后复测：

```
$ curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8091/api/health
200
$ curl -s -o /dev/null -w '%{http_code}' 'http://127.0.0.1:8091/api/family-graph?scope_id=home-default'
200
$ curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:9598/api/v1/health
200
$ curl -s http://127.0.0.1:8100/v1/models | grep -o gemma4-12b-it
gemma4-12b-it
```

`8091` / `8101` / `11434` 全程没有重启，9598 也没有（用户明令）。8100 的进程号从
2022624 换成 2596792，启动命令末尾多了那两个 flag（上面 `cmd` 的输出）。

### 本次交付的 `make ci`（`bf022d4` 的树上，2026-09-25T10:41:47+08:00 起跑，load1=1.0）

```
All checks passed!                              # ruff check
225 files already formatted                     # ruff format --check
Success: no issues found in 138 source files    # mypy strict
Contracts: 5 kept, 0 broken.                    # lint-imports
reachability: 0 个问题
capability-map: 0 个问题
module-map: 0 个问题
openapi 契约一致
19 failed, 1078 passed, 10 skipped, 11 warnings in 554.89s (0:09:14)   # pytest --cov
make: *** [Makefile:56：coverage] 错误 1
```

**`CI_EXIT=2`，耗时 554.89 秒（9分14秒）。** 计数对照上一次（同一个 §2n，未改动前）：
`18 failed, 1075 passed, 10 skipped`——**passed +3 正是本次新增的三条用例**（1080 里绿的
那批），红的一条没多。

**这 19 条红是环境红，不是代码红**（`git stash` 对照，判据是「同一批红」）：

```
=== git status (改动前) ===  6 个文件 M
stash_rc=0
=== git status (stash 后，应为空) ===
=== 未改动树上的同一批文件 ===
BEFORE_TREE_EXIT=1
19
=== 改动树 vs 未改动树的失败集合 ===
IDENTICAL（逐行相同，19 条）
=== git status (还原后) ===  6 个文件 M
```

19 条全在两个 GPU 依赖的层（`e2e/test_visual_search.py` 7 条、`e2e/test_assistant_turn.py`
4 条、`integration/test_face_detector.py` 6 条、`integration/test_face_embedding.py` 2 条），
根因同一条：

```
onnxruntime ... CUBLAS failure 3: the resource allocation failed ;
GPU=0 ; ... expr=cublasCreate(&cublas_handle_);
```

**vLLM 起来以后一直占着 ~19 GB / 24 GB**（本次重启前后都是），onnxruntime 的 CUDA provider
拿不到显存。与 §2n 那次是同一个环境状态，与本次改动无关。

**一处要如实说**：`make ci` 在 `coverage` 这步就退出了（make 遇错即停），所以链尾的
`web-check`（前端 tsc/oxlint/vitest/vite build）**没有在 ci 里跑到**。单独跑过，是绿的：

```
$ make web-check      # rc=0
✓ 29 modules transformed.
dist/assets/index-CX99wMss.js   240.85 kB │ gzip: 75.79 kB
✓ built in 80ms
```

---

## 2p. 收尾（2026-09-25 续二）：思考通道漏进用户答案 —— 在 2.0 客户端剥，不动 1.0

用户裁决：**「2.0 加就行，别动老代码」**。

### 现象与根因

工具往返两轮之后，模型给出的**最终 content** 里带着 Gemma 4 的思考通道标记：

```
=== turn3 finish=stop tc=[]
   content: '<|channel>thought\n<channel|>为您找到了去年在海边的照片，请查看。'
```

8100 是 vLLM（`gemma-4-12B-it`，`--served-model-name gemma4-12b-it`），**没开
`--reasoning-parser`**，于是模型自己写出来的通道标记原样落在 `content` 上，一路端到用户。
复现的是 2.0 自己的客户端（`OpenAIChatModel.complete`）打真端点，不是伪造响应。

### 候选 a 被实测否掉（这一步必须先做）

`chat_template_kwargs` **确实被 vLLM 0.26.0 支持**——但**修不了这个 bug**。

先证明字段被吃到（`vllm/entrypoints/openai/chat_completion/protocol.py:341` 有它）。
`enable_thinking=True` 让输出发生变化 —— 字段生效：

```
--- baseline: HTTP 200   content: '1+1等于2。'
--- enable_thinking=True: HTTP 200   content: None          <- 行为变了＝字段被吃到
--- enable_thinking=False: HTTP 200  content: '1+1等于2。'
```

再证明**它对这个 bug 是空操作**。gemma-4 模板第 186 行是
`{%- set enable_thinking = enable_thinking | default(false) -%}`——默认本来就是 `False`，
所以「传 False」与「不传」逐字相同。同一段工具循环跑三种参数：

```
--- baseline: turn3 finish=stop
    content: '<|channel>thought\n<channel|>为您找到了去年在海边的照片，请查看。'
--- chat_template_kwargs={enable_thinking:False}: turn3
    content: '<|channel>thought\n<channel|>为您找到了去年在海边的照片，请查看。'   <- 一字不差
--- chat_template_kwargs={enable_thinking:True}: turn2
    content: '<channel|>我为您找到了去年在海边的照片。请问您想看这些照片吗？'      <- 仍然脏
```

**`chat_template_kwargs` 不能当反向门禁用**：传一个不存在的键，端点照样 200、照样静默
忽略 —— 「200 就当它被接受了」是假绿。判据只能是「输出是否真的变了」。

于是走**候选 b：在 2.0 客户端剥**。

### 为什么不加服务端 `--reasoning-parser`

8100 是 1.0 与 2.0 **共用**的端点，一个服务端 flag 会把 8091 那边 1.0 的答案也一起改掉
—— 正是用户裁决要避免的事。所以剥的动作落在 2.0 自己这一侧
（`infrastructure/assistant/chat_client.py`），1.0 仓库与 8091 进程一个字没碰。

### 剥在哪、按什么语义

`split_thought_channel()` 在**响应进入 2.0 的唯一那道口子**上剥（`complete()` 的
`ModelReply` 构造处），于是**规划器与工具循环两条路一起干净**，没有哪个调用方需要记得剥。

语义**照抄模板自己的 `strip_thinking` 宏**（`chat_template.jinja:156`）——那是模型作者对
这段标记的定义：闭合通道整体删除、两边正文都留下；只有开始符没有闭合符时它后面的一切
都不算答案；只有闭合符没有开始符（实测于 `enable_thinking=True`）同样删除；
**除此之外一个字都不动**（连首尾空白都原样透传，`run_turn` 自己会 strip）。

**思考内容不丢，但也不给用户**：它落在 `ModelReply.thought` 上，另留一条 DEBUG 日志。
本回合唯一能解释「模型为什么这么答」的东西是它，无声丢掉是错的；用户问的是照片，
不是模型的独白，塞进答案是错的。

### 红 → 绿

先写用例（6 条，`integration/test_chat_client.py`），**全部红**（`AttributeError:
'ModelReply' object has no attribute 'thought'` + 内容仍带标记），再改代码，**22 passed
（该文件）**。

改后用**同一个打真端点的复现**核对：

```
=== turn3 finish=stop tc=[]
   content: '为您找到了去年在海边的照片，请查看。'
```

同一段工具循环重复 5 次：

```
run0..run4: dirty=False answer='为您找到了去年在海边的照片，请查看。'
带标记的次数: 0 / 5
```

### 定点变异（带反向门禁）

| 变异 | 结果 |
|---|---|
| **M1** 完全不剥（`return text, ""`） | 5 条通道用例转红；**`test_an_answer_without_any_channel_is_untouched` 保持绿** |
| **M2** 未闭合通道把尾巴当答案（反向门禁） | `test_an_unterminated_thought_channel_never_becomes_the_answer` 转红；无通道用例保持绿 |
| 还原 | `EXIT=0`，且 `md5sum` 与变异前逐字节相同 |

M1/M2 里那条**始终绿**的用例是反向门禁：它证明这组用例钉的是具体行为，不是
「只要出现标记就报红」——把剥的动作整个删掉它也不会红，只有真去动它钉的那件事才红。

### 1.0 的 8091 答案：改前 / 改后逐字相同

固定三问（`scope_id=album_b04cfeaca0c3`，`include_debug=true`），改前存快照、改后重打：

```
=== DIFF 1.0 改前 vs 改后 ===
IDENTICAL: 1.0 答案一字未变
```

**一处要如实说**：1.0 这三问答的都是 `planner_blocked` + 同一句固定文案，**不是模型产出
的答案** —— 它的 LLM 名字写死成 `gemma4-qat-iq3xxs`，而 8100 现在服务的是
`gemma4-12b-it`（后端从 llama.cpp 换成 vLLM 时改了名）。所以这次「一字未变」只证明了
没有回归，**没有**证明「1.0 走模型那条路也没变」。真正的结构性保证是：本次改动只落在
2.0 工作树的三个文件里，8091/8100 的进程、1.0 仓库、8100 的启动参数全部没碰
（见下面「动过什么」）。**这个写死的名字本身就是 §2n 那条「模型名问端点要」的活证据。**

### 9598 换新代码重启（裁决二）

旧进程是 **2026-09-23 10:40** 起的，跑的是旧代码（模型名还写死）。停掉，用**本次工作树**
重起，`SENTRIX_DATA_DIR` 沿用旧进程那一份：

```
=== 旧进程 ===  3623789  Wed Sep 23 10:40:41 2026
=== 新进程 ===  2922328  Fri Sep 25 13:31:09 2026
LISTEN 0.0.0.0:9598
```

`/api/v1/health` → **200**。4274 的 Web 没重启（它只是前端，HMR 自己跟）。

**重启前**（旧代码，同一句话）：

```
status=partial  termination_reason=planner_blocked
answer="当前问题的证据需求无法可靠规划，因此暂时无法确认。"
```

**重启后**（本次工作树，`scope_id=album1`）：

```
status=partial  termination_reason=answered_with_gap
answer="我是你的家庭相册助手，可以帮你检索照片并查询已确认人物的档案。"
```

**交付回合实测**（`album1`，53 张）：

```
question="把水坝和河流的那几张照片直接发给我"
status=complete   termination_reason=answered
answer="这是为您找到的关于水坝和河流的照片。"
attachment_count=6
photo_1 asset_1a0e7c379fe5 '2017-11-05 150808.jpg'   ... photo_6 asset_e3798af44527
```

交付端点核对（真实可打开，且越界 handle 被拦）：

```
result_set_id=rs_00a2c3684999
交付原图 photo_1  http=200 type=image/jpeg bytes=128134   -> JPEG 1600x1200
越界 handle       http=404
```

重启前那句固定文案 vs 重启后的真答案，说明「模型名写死」在 9598 上是**实际发生**过的坏法。

### 顾虑（如实）

1. **`thought` 现在没有下游消费者。** 它挂在 `ModelReply` 上、留一条 DEBUG 日志，但没有
   任何一层把它落库或展示。这是有意的（先把「不静默丢」做到），但它确实是一条
   「有值、无人读」的字段——**如果后续要展示推理过程，接它，不要另外去解析 content。**
2. **被测的是「模型这次没产生思考内容」这一幕。** 真端点重复 5 次都拿到空思考通道
   （`thought` 为空），所以「非空思考被保下来」这条只在假端点上钉住过，没在真模型上见过
   一次非空样本。语义按模板宏照抄，但样本不足。
3. **`thought` 的通道名是靠第一行切的**（`<|channel>名字\n内容`）。模型只用 `thought`
   一个名字，所以够用；真出现别的通道名，通道体会被当思考收走。
4. **`enable_thinking=True` 那一路没被修**（输出变成 `'<channel|>我为您找到了…'`）。它不在
   生产路径上（模板默认 False），本次只是在实测里见到，如实记一笔。

### 本次交付的 `make ci`（改动树上，2026-09-25T13:11 起跑，load1=0.54）

```
All checks passed!                              # ruff check
224 files already formatted                     # ruff format --check
Success: no issues found in 138 source files    # mypy strict
Contracts: 5 kept, 0 broken.                    # lint-imports
reachability: 0 个问题
capability-map: 0 个问题
module-map: 0 个问题
openapi 契约一致
19 failed, 1084 passed, 10 skipped, 11 warnings in 588.53s (0:09:48)   # pytest --cov
make: *** [Makefile:56：coverage] 错误 1
```

**`CI_EXIT=2`，耗时 593 秒（9分53秒）。** 红的是**同一批 19 条 GPU 依赖用例**
（`test_visual_search` 7 / `test_assistant_turn` 4 / `test_face_detector` 6 /
`test_face_embedding` 2），`git stash` 到未改动的 HEAD 上跑同一批文件，**失败集合逐行
相同**：

```
CONTROL_EXIT=1        # 未改动树，同一批文件
19 条 FAILED（与改动树逐条相同）
```

根因在日志里：

```
11 OutOfMemoryError / CUDA out of memory
ERROR sentrix.chinese_clip: chinese-clip 加载失败: /home/asus/.cache/clip/clip_cn_vit-l-14.pt
```

**vLLM 占着 23.7 GB / 24.5 GB**，CLIP 与人脸模型加载不到显存 —— 环境红，不是代码红。
把这 19 条 deselect 后重跑 `pytest --cov`：**EXIT=0，覆盖率 97.52%（阈值 85%）**。

`make ci` 在 `coverage` 这步就退出（make 遇错即停），链尾 `web-check` 没跑到，单独跑过：

```
$ make web-check      # rc=0
8 passed (vitest)
dist/assets/index-CX99wMss.js   240.85 kB │ gzip: 75.79 kB
✓ built in 67ms
```

### 动过什么（共享资源清单）

| 资源 | 动作 |
|---|---|
| 1.0 仓库 `/home/asus/Github/Sentrix-Home-Web` | **只读**，未改（本次只 `grep`/`cat` 过） |
| 8091（1.0 进程 1507974） | 未重启、未改环境 |
| 8100（vLLM 2596792） | 未重启、未改启动参数、**未加任何 flag** |
| 8101 / 11434 | 未碰 |
| 4274（Web 3529372） | 未重启 |
| `data/` | 未清；本次实测产生的会话与结果集是正常写入 |
| 9598 | **停旧进程、用本次工作树重起**（用户裁决二要求） |

---

## 2q. S5c 实施期（外观证据 / 人物画像）的发现与偏离（2026-09-24）

> **【搬运时重编号，2026-09-26】** 这一段原在隔离副本
> `/home/asus/s5verify/repo`（分支 `s5-tdd`）的 `s5-deviations.md` 里，段号 `## 2l`、
> 条目号 `109–119`。主仓库分叉后已占用 `## 2l`（第 104 条裁决落地）且编号条目排到
> `第 114 条`（提交信息里用到 115–118），两边同号不同事。故搬回时**整段重编号**为
> `## 2q` / `第 119–129 条`，一一对应关系是 +10。正文里对更早条目的交叉引用
> （如第 58 条、第 104 条）**未改**，那些号两边一致。

本轮实际落地的是计划里预留的 Task 85/86 中的**两条纯 domain 子任务**
（`domain/identity/appearance.py` 与 `domain/identity/portrait.py` 及 `moments.py`），
编排、仓储、路由、e2e 未落地。状态与原始输出见
`docs/refactor/2026-09-24-s5c-implementation-plan.md`。**下面每条都是实施期实测出来的，
不是读源码推出来的。**

### 第 119 条 · 变异测试的第二类假阴性：**`.pyc` 缓存把「变红」变成了「变绿」**

第一版变异脚本跑出 **4 条「没变红」**，手验却有 3 条确实是红的。根因不是实现也不是
用例，是**字节码缓存**：CPython 只按 `(源文件 mtime 的秒, 源文件大小)` 判断 `.pyc` 是否
过期，而定点变异几乎总是**等长改写**（`0.35` -> `0.30`、`24` -> `20`），同一秒内写完，
旧 `.pyc` 被判定有效，pytest 导入的仍是**变异前**的代码，于是「绿」。

这与第 60 条是同一族（那次是 `sed` 不匹配却静默成功），但**触发条件相反**：第 60 条是
变异没落上去，本条是变异落上去了、**跑的却还是旧代码**。两条都指向同一句话——
**「没变红」的第一嫌疑人永远是这个测量装置本身。**

**修法（两条一起上）**：跑之前清掉源码树下的全部 `__pycache__`，并设
`PYTHONDONTWRITEBYTECODE=1` 不让它在窗口期再写一份。

**通用结论**：等长变异配上字节码缓存，会产生**静默的假绿**。这不是本仓库特有的，
任何做定点变异的地方都要处理它。

### 第 120 条 · 边界用例**从实现里读回常量**，于是跟着变异一起移

修完第 119 条之后，`MIN_HANZI = 80 -> 70` 与 `MAX_HANZI = 500 -> 400` **仍然不变红**。

第一版的边界用例写的是 `verdict(MIN_HANZI)` / `verdict(MIN_HANZI - 1)`——常量是从被测
模块 **import 回来**的。变异把 `MIN_HANZI` 改成 70 之后，用例的断言**也跟着变成**
「`verdict(70)` 该通过、`verdict(69)` 该报 `69 < 70`」，自洽地全绿。

这与第 60 条的「IoU 阈值 1.0 配一对 IoU 0.68 的框」是同一个形状的**第二个变体**：
那次是输入没走到边界，这次是**边界本身跟着被测对象一起漂**。判据是——**边界值必须是
字面量**。修法：写成 `assert (MIN_HANZI, MAX_HANZI) == (80, 500)` 加
`verdict(80) == (True, [])` / `verdict(79)` / `verdict(500)` / `verdict(501)`。

### 第 121 条 · `ROLE_OPTIONS` 随裁决① 重新找家（落 `domain/identity/portrait.py`）

1.0 `person_portraits.py:11` 是 `from .person_graph import ROLE_OPTIONS`。裁决① 判死
`person_graph.py`，词表要重新落点。核过全部三个消费方（`person_portraits.py:35`、
`db.py:6471`、`person_graph.py:121`）之后：后两处都在角色假设功能里，随 person_graph
一起判死；**唯一的 2.0 消费方是画像那条「未确认角色的模糊措辞」判据**，所以它住在
`portrait.py`。**这不是新设计，是把一个已有词表放到它唯一的去处。**

### 第 122 条 · **计划缺口**：裁决① 保留的 `moments` 阶段，计划里没有归属

裁决① 保留 5 阶段（rank / select / **moments** / compile / write）。但
`2026-09-23-s5-implementation-plan.md` 的移植清单**从未给
`person_moments.py` 的 `PersonMomentExtractor` 派过任务**：第 176 行把它与另外三张表
一起列进「冻结」格，而裁决② 只解冻了**表**。

实测确认：`grep -rn "moment" apps/api/src/` 在 2.0 没有一处相关命中，也没有任何模型端口
方法对应 `model_clients.py:1316` 的 `analyze_person_moments`。**所以 `moments` 阶段是
净新增的三块**（词表 / 带编号预览渲染 / 抽取用例），其中后两块分别落 infrastructure
与 application。**这是本轮相对原计划多出来的工作量**，登记在此。

### 第 123 条 · `suggested_relationships` 从证据包里**去掉**（不是留空）

1.0 证据包该键的来源是 `list_relationship_hypotheses`，写入者是 `infer_graph`；
裁决① 判死它，且 0006 **没有建** `relationship_hypotheses` 表（已核对 0006 的 14 张表）。
这个键在 2.0 因此恒为 `[]`。**处置：整个键去掉。** 留一个恒空的键，会让读提示词的人
以为那条链路是通的。

**连带：关系假设的读方法不补。** 无表、无写入者——补了就是死代码，与裁决③ 判
`apply_relationship_threshold` 死代码的判据同形。

### 第 124 条 · `normalize_writer_output` 对非字典主题**跳过**（偏离 1.0 的加严）

1.0 `person_portraits.py:186-193` 直接 `theme.get(...)`。实测：给它
`{"themes": ["不是字典"]}` 会抛 `AttributeError: 'str' object has no attribute 'get'`，
异常一路冒到编排层把整次生成判失败。

2.0 加了一条 `isinstance(theme, dict)` 守卫，跳过该条。理由：**少一条主题比整次生成
失败好**，而且少的那条会被「themes 必须 2-6 条」这条校验接住（条数不够就不过，
不会静默降级成一张残缺的画像）。这是**有意偏离 1.0 的一处加严**，不是照搬。

### 第 125 条 · 缺陷 2 只修掉了一半：兜底画像仍可能被敏感词拦下

裁决给的修法（`validate_portrait` 加 `source="deterministic"` 分支）解决的是**长度与
主题条数**这两条「由模板决定」的约束。实测发现还有第三条路会让兜底落不了库：

`deterministic_portrait` 把 `action_text` **原样**搬进模板（`f"他常{action}"`），而
`action_text` 是模型写的、**不过 `SENSITIVE_TERMS` 这道滤网**
（`normalize_person_moments` 只清 `social_role_cues` 与 `visible_affect`）。所以一条
「生病住院」的瞬间会让兜底文案含敏感词，**兜底照样被自己的校验拒掉**。

**本轮处置：保留这个行为，不修。** 理由是它**不是同一类缺陷**：长度那条是「把两个
不同的东西塞进一条校验」（模板长度 vs 模型长度），而这一条是「兜底不等于豁免产品
规则」——敏感词黑名单是硬线，兜底文案含敏感词就该被拦。修它要么得在
`normalize_person_moments` 里也清 `action_text`（会改变已落库瞬间的语义，属另一条
产品决定），要么得让兜底对敏感词豁免（那是开一个真的后门）。

**但它有一个必须记下来的后果**：对这类输入，三级降级（writer → repair → 兜底）
**仍然会整条落空**，这个人这一轮就没有画像。这不是「不可能发生」——实测的真数据里
就有含敏感词的瞬间描述。**它是本轮已知的、有意的残余缺口**，不是待办事项。

### 第 126 条 · 真数据端到端**没有做**，所以两个关键数字还没有

本轮最想要的两个数字——**兜底占比**（模型的画像通过率）与**缺陷 1 的拦截数**——
都来自阶段 9 的真数据实测。阶段 9 的前提是阶段 8（真实仓库执行）完成，而阶段 8 的前提
是真实仓库那个 `make ci`（PID 1515611）跑完。**本轮没走到，所以这两个数字一个都没有。**
不编造、不用假件顶替——假件跑出来的通过率与真实模型的通过率不是一回事。

### 第 127 条 · 用例里的「敏感词」**不在词表里**——过滤器整段删掉都不变红

给 `normalize_person_moments` 补用例时，`social_role_cues` 那条我写的是
``["父亲", "有糖尿病", "照顾者", ""]``，断言「有糖尿病」被丢。定点变异把
`if value and not contains_any(value, SENSITIVE_TERMS):` 改成 `if value:`（**整段删掉
敏感词过滤**）——**没有变红**。

根因：**「糖尿病」根本不在 `SENSITIVE_TERMS` 里**。词表是笼统词表（「生病」「疾病」
「癌症」），没有具体病名。所以那条用例一次都没走到过滤分支上，它看起来在钉过滤，
其实只钉了「非空就保留」。改成「常年生病」之后立刻变红。

这是第 60 条／第 120 条的**第三个变体**，形状又不一样：那次是输入没到边界、那次是边界
跟着变异漂，**这次是构造用例的素材不在被测集合里**。

### 第 128 条 · `interaction_labels` 里的 `null` 会变成字符串 `"None"`（1.0 原样，照搬）

模型把 `interaction_labels` 写成 `["P2", null]` 时，1.0 的
`str(raw).strip()` 把 `None` 变成 **字符串 `"None"`**，它是真值于是被留下。实测 1.0
同样给出 `['P2', 'P3', 'None']`。

**本轮照搬，不修。** 依据：它的下游是 `PersonMomentExtractor` 的
`label_map.get(target_label)`，而 `"None"` 在 `label_map` 里查不到 -> 静默被丢，
落库的 `interaction_target_ids` 里不会出现垃圾。**无下游影响的脏值不值得为它偏离 1.0**。
已用一条用例把实际行为钉住（而不是钉我以为的行为）。

### 第 129 条 · `SENSITIVE_TERMS` 是笼统词表，这是**已知边界**不是待修缺陷

承第 127 条：词表拦「生病」不拦「糖尿病」，拦「收入」不拦「月薪三万」。
已加一条用例把这个边界**显式钉住**（`test_the_sensitive_vocabulary_is_coarse_by_design`），
免得后来人以为漏的是 bug。真要收紧，该动的是提示词与审核策略，不是把词表改成穷举
具体病名——那既列不完，也不属本切片该改的产品规则。

### 第 130 条 · 我往模型元数据里多加了一个键，还给它编了个理由

``ObserveAppearance._metadata`` 第一版返回**四个**键，比 1.0 ``app.py:2046-2050`` 多一个
``crop_bbox``。这从来不是无害的补充：这三个键会 ``json.dumps`` 之后拼进提示词
（``person_vision_model.analyze_person_appearance`` 的最后一行），**多一个键就是改了模型
看到的输入**。

更值得记的是我当时的推理过程：我先写出那个键，然后写了一段读起来挺顺的 docstring
（「告诉它这一块是从哪里裁出来的，是它判断哪部分是目标的全部依据」）来支持它。
理由是在动作之后编的——**那不是证据，是辩护**。这个项目对偏离的要求是「证据足够强
就按证据走、只在报告里标注」，而我手上一条证据都没有。

已删，现在是三个键；并加了一条用例直接对着键集合下断言
（``test_the_metadata_is_the_three_fields_1_0_sent``，变异「多一个键」会变红）。
提示词模板那条用例挡不住这种改动：模板没变，变的是拼在它后面的数据。

### 第 131 条 · 1.0 的三重守卫**在生产路径上永远不会浮出来**

T85b 移植外观证据仓储时，把 1.0 ``db.py:4485-4497`` 的三重守卫逐条搬了过来，其中第三重
（这张脸真的经 ``entity_mentions`` 确认给了这个人）挡的是「把 A 的衣服记到 B 头上」。

两处都是核过的：守卫抛 ``ValueError``；而唯一的调用方 ``_analyze_confirmed_person_appearance``
（``app.py:2058``）把每一张脸包在 ``except (OSError, ValueError, RuntimeError)`` 里。
**两条合起来意味着**：在 1.0 里守卫就算拦下了，外面看到的也只是「这张照片跳过了」——
与「图读不了」长得一模一样。守卫是有的，它的**失败**是隐形的。

2.0 收紧：只捕获 ``OSError`` / ``ValueError`` 中**来自媒体那几步**的那些，仓储抛出来的
照常往上走。做法是把 ``record`` 放在 ``try`` 之外——不是把 ``except`` 写窄（那做不到，
媒体失败与守卫失败抛的是同一个异常类型）。

代价照写：确认一个未确认的人，现在会**整个请求 500**，而不是静默地少一条外观证据。
换来的是这类错一定会被人看见。用例 ``test_a_repository_guard_is_not_swallowed`` 钉住它，
变异「把所有异常一起吞掉」会变红。

媒体失败是**数据问题**（这张照片用不了），守卫失败是**代码问题**（有人在把 A 的衣服记到
B 头上），两件事不该共用一个 ``catch``。

### 第 132 条 · 变异测试的第二类假阴性：**语法错误被记成「变红」**

定点变异第一版报「15 条全部变红」，其中一条的实际输出是 ``变红 ✓ (0)``——括号里是
失败用例数，**0 条失败**。那是变异把源码改成了语法错误，pytest 收集不到用例、退出码非 0，
而我的脚本只看了退出码。

一个「把源码改坏」的变异会被记成「用例咬住了这条规则」，但它什么都没证明。已改成：
退出码非 0 **且** 有 ``FAILED`` 行才算变红；否则单列成「没跑」。

这是第 119 条（``.pyc`` 缓存把变红变成变绿）的**镜像**：那次是假绿，这次是假红。两次的
共同点是**用一个代理量（退出码 / 缓存）代替了真正要问的那个问题（那条用例到底跑了没有）**。

同一次还暴露了另外两条**空操作**变异，它们不是用例的漏洞，记在下面：

- 「去掉 SQL 的 ``LIMIT 1``」——结果集不变（``ORDER BY`` 还在），而 Python 那边取的是
  ``fetchone()``，仍然只有一行。改了等于没改；
- 「删掉 ``is_file()`` 那一句」——文件不存在时 ``Image.open`` 抛的
  ``FileNotFoundError`` 本来就是 ``OSError`` 子类，被同一个 ``except`` 接住，结果一样。
  **1.0 那一句因此是快路径，不是判据。** 保留照搬，但不假装用例验到了那一行；
  真正守「缺文件的照片被跳过」这条行为的是把 ``OSError`` 从 ``except`` 里拿掉那条变异
  （它变红了）。

### 第 133 条 · 又一条「声称在检查、其实没有」：断言在错误的时刻求值

``ScriptedVision`` 里那个记录「模型调用时临时图还在不在」的辅助方法，第一版把**路径**
存下来、在断言时才 ``is_file()``。而那时临时图已经被删掉了——于是它**恒为 ``False``**，
用例想验的「模型看到文件时它还在」与它实际验的「文件最后被删了」完全不是一件事。

是那条用例自己红了我才发现的（``assert [False] == [True]``）。根因是我给方法起名叫
``image_existed_at_call_time``、给辅助行写了「在调用当场记下」的注释，**而代码做的是另一
件事**——名字和注释描述的是意图，不是行为。

已改成在 ``analyze_person_appearance`` 里当场求值并存布尔值。这与第 60 条／第 120 条／
第 127 条同族，但形状又新了一号：那三次是被测对象没被真的碰到，**这次是断言本身在错的
时刻取值**。

### 第 134 条 · 我手写的门禁清单漏了 `lint-imports`，分层违规一路绿灯到全量测试

T85d 让 `application/identity/observe_appearance.py` 直接
`from sentrix.infrastructure.media.person_vision import render_person_crop`。这违反
`application-purity` 契约（application 不许 import infrastructure，也不许 import PIL）。

**我自己的检查没有拦下它**，因为那一轮我跑的是 `ruff` / `ruff format` / `mypy` /
`check_reachability` / `check_module_map` / `check_capability_map`——**一份手写的清单，
里面没有 `lint-imports`**。而 mypy 不查分层：它只问类型对不对，不问这一层许不许 import
那一层。于是六道门禁全绿，直到全量测试跑到 `test_architecture_rules.py` 才红。

根因不是「忘了 lint-imports 这一条」，是**用手写清单代替了项目的门禁**。项目有 `make ci`，
它枚举的就是完整的那一套；我为了省时间挑了其中几条来跑，于是清单必然会漂——这次漂掉的
恰好是分层契约那条。这和第 60 条那个家族同形，只是代理物换成了「我记得要跑的那几条」，
被替代的问题是「门禁到底要跑哪几条」。

修法是按项目既有形状补端口（`domain/identity/ports.py` 的 `PersonCropRenderer` +
`infrastructure/media/person_vision.py` 的 `PillowPersonCropRenderer`），**不是**延迟
import 或 `TYPE_CHECKING` 绕过——那样只是把违规藏进类型检查看不见的地方。顺带说一句：
`PillowMetadataReader` / `PillowPreviewEncoder` 这两个类的名字里带 Pillow 不是啰嗦，
存在的唯一理由就是这条契约；我是自己撞上去才真正看懂它们为什么那么叫。

**这一轮两次都是项目的门禁抓到的，不是我**：上一次是 pre-commit 的 doc-checks 抓到
module-map 漏了两条边（T85c），这一次是全量测试抓到分层违规（T85d）。这恰好说明那两道
门禁值得留着——它们抓的正是作者本人看不见的那类错。

### 第 135 条 · `selection` 那一包元数据**从来没落过库**：两端各自都对，断的是键名

1.0 的 `select` 阶段专门算了这包东西（`person_insights.py:103`）：

```python
item["selection_json"] = {
    "version": "representative-v1",
    "quality": ...,
    "body_visibility": ...,
    "duplicate_threshold": ...,
    "event_coverage": ...,
}
```

而写入端读的是另一个名字（`db.py:6204`）：

```python
data.get("narrative_note") or "", json_value(data.get("selection"), {}),
```

`person_moments.py` 全文没有往 payload 里放过 `selection` 键（`grep` 只命中 `extract`
的循环变量名），于是 `data.get("selection")` 恒为 `None`、`json_value(None, {})` 恒为
`{}`。**那一列在 1.0 里永远是空对象。**

**真库对拍不了**：`data/sentrix.db` 的 `person_moments` 是 **0 行**——这个功能在 1.0
生产上从没跑过（与「`SENTRIX_PERSON_INSIGHT_SCOPES` allowlist 从没生效过」吻合）。
所以判据只能是结构的：select 阶段算了它、表里有这一列、写入端在读它，**唯一断的是
键名**。三处合起来足够强，T86f 按证据接上。

**这一条与本项目那个反复出现的家族同形，但形态又新了一号。** 前几次是「判据本身是空
的」（代理条件、恒真断言、素材不在被测集合里）；这次两端**各自都是对的**：select
阶段老实算了一包数据，写入端老实地读一个键、老实地 `json_value` 兜底、老实地写进
`ON CONFLICT DO UPDATE`。没有任何一行代码是错的，也没有任何门禁能发现——因为要发现
它，得同时看两个文件的两个字符串，而它们各自读起来都通顺。**唯一断掉的地方是一个
词。**

**顺带补完的读路径。** T86b 的 `moments.py` docstring 事先把这件事挂在了消费方名下
（原话：「消费方（T86f 的瞬间抽取）一旦需要它，就得先给记录加字段再改读路径」）。
T86f 现在正是那个消费方，所以一并做了：`PersonMomentRecord` 加 `selection` 字段、
`selection_json` 补进 `_COLUMNS`、`_record` 还原它。留一个**只写不读**的列等于「接了一半」
——数据进去了，谁都拿不出来，而那与「压根没写」在外部看来完全一样。

### 第 136 条 · 冗余判据：定点变异改不红它，**而那不等于用例漏了**

S5c 这一轮撞出三条这样的行，都是 1.0 原样搬过来的：

| 位置 | 那一行 | 为什么改它不改变行为 |
|---|---|---|
| `repositories/faces.py` | SQL 的 `LIMIT 1` | 结果集不变（`ORDER BY` 还在），Python 那边取的是 `fetchone()` |
| `media/person_vision.py` | `is_file()` 那一句 | 文件不存在时 `Image.open` 抛的 `FileNotFoundError` 本来就是 `OSError`，被同一个 `except` 接住 |
| `application/identity/extract_moments.py` | `if result is None: return 0` | 下一句 `isinstance(raw, list)` 对 `None` 给出同样的结果 |

**共同形状**：某一行的效果已经由**相邻的另一行**产生了。删掉它、或把它改成相反的
分支，外部行为一模一样——所以没有任何用例能变红，**而这与用例写得好不好无关**。

**为什么值得单独记一条。** 一个只看「变异有没有变红」的流程会把这三条报成
「用例没咬住这条规则」，而接下来有两条**都更糟**的应对：

1. **当成死代码删掉**——它们不是死代码。`LIMIT 1` 让意图写在 SQL 里、`is_file()` 省掉
   一次异常往返、`result is None` 把「模型不可用」这条端口契约写在读它的地方。删掉
   之后代码仍然对，但**读的人少了一条线索**。判据与「被不可变 schema 挡住的分支」
   （portraits 里去掉的那条 `or "home-default"`）不同：那一条是**到不了**，这三条是
   **到得了但另有同效路径**。
2. **造一个不可能的输入把它弄红**——「在库里种一个 `id=""` 的退化实体」就是这个路子。
   它能让变异变红，但代价是引入一个生产上不存在的状态。**这一条在这一轮真的发生了**
   （`test_a_label_whose_person_id_is_empty_is_dropped_while_the_others_land`），
   处理方式是可接受的：那条用例的 docstring 把「为什么必须种这个退化实体」写清楚了
   ——不种的话 `str("")` 会被下游 `_in_scope` 顺手挡掉，`person_id` 那半判据与
   「查不到这个人」就没有可观察的区别。**说清楚了就不是伪装。**

**口径**：变异脚本现在把这类单列成「已知空操作」，并写明原因，**不计进「变红」那一格**
也不计进「没变红」。数字分开报，读的人才知道那一条到底证明了什么。把空操作混进「变红」
就是这一轮修掉的另一个假象（见第 132 条：语法错误被记成变红，是同一类账目问题的另一面）。

**顺带一条**：`extract_moments` 里 `not info.get("person_id")` 这半判据在 2.0 被
`_in_scope` 双重覆盖了——任何空值折成字符串之后都查不到实体。1.0 没有 `_in_scope`
的前置过滤，所以那半在 1.0 是真的在挡东西；2.0 里它是纵深防御，留着（判据同上：它是
1.0 的原文，且说明「没有归属的人不产生瞬间」这条意图），但**不假装它单独被验到了**。

### 第 137 条 · 余弦去重那条分支，被调用方一个 `{}` 关掉了

``insights.select_representatives`` 的第二条约束是**去重**：与已选中的任一张余弦
≥ ``DUPLICATE_THRESHOLD``（0.94）就跳过。它的 docstring 写明这条的理由——「同一套
衣服、同一个角度连拍十几张时，去重比排序更决定最终观感」。

而 1.0 唯一的调用点（``person_insights.py:271``，2.0 对应 ``run_insights._select``）
传的是：

```python
select_representatives(candidates, {}, limit=12, per_event=2)
```

``vector_by_asset`` 是空表 → ``vector = vector_by_asset.get(...) or []`` → 空 →
``if vector and any(...)`` 恒假。**那条分支在生产上从不执行。**

**这一条与第 135 条形状不同，所以处置也不同。**

| | 第 135 条（``selection_json``） | 第 137 条（向量去重） |
|---|---|---|
| 断在哪 | 键名：算的是 ``selection_json``，读的是 ``selection`` | 取值：参数留着，调用方显式传空表 |
| 写入端的态度 | **在读那个键**，只是名字对不上 | 无写入端，纯读参数 |
| 读起来像什么 | 明显的笔误 | 像**一个选择** |

第 135 条那条线，证据是「两端各自都对、唯一断的是名字」，所以接上了。这一条**没有
那么强的证据**：把参数留着、把值给成 ``{}``，是一个看得懂的决定，不是打错的字。
可能是「当时没把向量接进那个 service」，也可能是「量过之后觉得去重在这批数据上没必要」。
两种都说得通，而**我手上没有能分开它们的证据**。

所以**照搬，不自己接上**——并在 ``run_insights._select`` 的 docstring 里写明这件事，
以及一条反向门禁：``test_select_gets_an_empty_vector_table_so_twins_are_not_deduped``
先证明「这两张向量真的相同、接上向量就会被去重」，再断言管线里两张都选中。有人顺手
把向量接上，这条会红，然后他必须来读这段理由。

**它不是没测**：``test_insights.py:196`` 直接喂真实向量覆盖了去重分支（T86d 的 19 条
定点变异包含它）。所以这是「**规则测了，生产不走**」，与「规则没人测」是两件事——
混为一谈会让人去补一份早就有的用例，或者更糟，把没测的那条当成有测的。

**到这里这个家族已经有三个成员了**，形状各不相同，值得放在一起看：

1. 第 135 条：算了一包数据，写入端读的是**另一个名字**；
2. 第 137 条：写了一条规则，调用方用**一个空字面量**把它关掉；
3. 第 136 条那三条冗余判据：写了一行判据，**相邻的另一行**已经产生同样的效果。

共同点还是那一个：**每一处单独读起来都是通的**。要发现它们，只能把两端放在一起看，
或者（更可靠）让一个会真的去改源码的流程去问「改掉这一行，有东西会红吗」。

### 第 138 条 · 裁决④ 的钩子：1.0 有三道闸，第一道让后两道**永远走不到**

计划里写的是「1.0 的 ``SENTRIX_PERSON_INSIGHT_SCOPES`` allowlist 从没生效过，画像在
1.0 里没有自动触发过」。落地 T86i 时把那个函数（``pipeline.py:758-793``）整个读了
一遍，实际情况比那句话多一层——**它不是一道闸，是三道**：

```python
def _maybe_trigger_person_insight(self, scope_id):
    allowlist = {SENTRIX_PERSON_INSIGHT_SCOPES 拆出来的}   # 默认空集
    if scope_id not in allowlist:
        return None                                        # ← 永远在这里返回
    event_count = COUNT(*) FROM events WHERE scope_id = ?
    latest = latest_person_insight_run(scope_id)
    watermark = latest.stats["event_watermark"]
    if latest is not None and event_count <= watermark:
        return None                                        # ← 没有新事件就不重跑
    run = create_person_insight_run(...)
    threading.Thread(target=execute, daemon=True).start()  # ← 后台跑
```

第一道让后两道从未执行过，但它们**是写下来的设计意图**，不是随手加的。三道的处置：

| 闸 | 1.0 | 2.0 |
|---|---|---|
| allowlist | 默认空 | **判掉**。这正是裁决④ 本身——「导入后自动跑」与「只对白名单跑」是互斥的 |
| watermark | 事件数 ≤ 上次 run 的水位就不跑 | **换判据**：``imported > 0``（这次真的新导进了照片） |
| 后台线程 | 起线程、返回 202 | **不搬**。2.0 没有 run 表也没有任务队列，替代品是同步跑 |

**第二道为什么要救回来。** 它的意图是「没有新内容就别重跑」，而一趟画像最多几十次模型
调用（每个核心人物至少一次，加 moments 阶段每人每资产一次）。不救的话，**一次全被去重
的重复导入也会触发一整趟**——那是纯粹的浪费，而且用户看不出为什么点一次导入要等那么久。

2.0 没有 run 表，也就没有 ``event_watermark``。判据取 ``imported > 0`` 而不是
``events > 0``：新照片可能落在**既有**事件里（那时 ``events`` 照样计到），也可能还没
形成事件（那时 ``events`` 是 0，但画面上的人可能是新的）。**只有 ``imported == 0``
（整批都被去重）是能确定「什么新东西都没有」的情形。**

**为什么不用一个 2.0 现成的量去近似 watermark。** 比如「最新一版画像的 ``created_at``
与最新事件的 ``created_at`` 比一比」。那看起来更接近 1.0，但它是**一条新规则**：1.0 比
的是「事件总数 vs 上一次 run 记下的水位」，两者不等价——画像可能在没有新事件的情况下被
手工重跑，此后 watermark 那套会认为「没新事件、不跑」，而我的近似会认为「有新事件」。
既然要换，就换成一个说得出、且与手上的数据严格对应的判据，并把它写进 docstring；
**不要造一个「看起来更像 1.0」的近似**。

**代价照写。** 第三道不搬 + 第二道换成 ``imported``，合起来意味着：**一次真正有新照片的
导入，会在本来就长的请求末尾再加一整趟画像流水线**，而这一趟是同步的、没有进度可查。
这是裁决④ 记账里已经接受的那笔（计划第 0 节），这里只是把它落到了实处。

**可观测性是有意加的。** 那一趟的六个计数回在 ``ImportBatchResponse.insights`` 里，
而不是只落日志：一个会走几十次模型调用、让请求长一截的副作用，**"到底跑了没有、跑了
多少"必须能从响应里看见**，否则它就是一个没人能观测的副作用。用 ``null`` 表示「没跑」、
用全 0 的对象表示「跑了但一个人都没认出来」——这两件事在排查时是完全不同的结论，
所以不能都用 ``null``，也不能都用全 0。

### 第 139 条 · 我给 e2e 加了一条模型路径，却**没去读那个文件的 docstring**

T86i 让导入批末自动跑画像（裁决④），那是这条链路上的**第四条**模型路径
（``PersonVision``）。而 ``tests/e2e/test_album_timeline.py`` 的 docstring 第 8 行就写着:

    **模型不在这里调**：8100 的 VLM 是 1.0 的服务，生命周期不归 2.0 管……
    观察器换成确定性的假件……

夹具下面把三个适配器换成了假件（``visual_embedder`` / ``vlm_observer`` /
``chat_model``）。我加了第四条，**却没有回去读那个文件**——于是 e2e 的导入开始打 8100
的真端点。

**这不是编码错误，是没读懂自己改的是哪条链路。** 我当时想的是「我加的是一个批末钩子」，
而正确的想法是「我给这条链路加了一个新的外部依赖」。两句话描述同一件事，但只有后者会
让我去查：谁在跑这条链路、它假设了什么。

**为什么现有门禁抓不到。** ``module-map`` 的「依赖谁 / 被谁依赖」两列正是为了强迫人回答
后者——但它覆盖的是**模块级 import**，而 ``routes/timeline.py`` 并没有 import
``person_vision``：画像是在 ``routes/insights.py`` 里装配、由 ``Container`` 提供的。
**依赖是从容器里取出来的，不是 import 进来的**，所以那张图上看不见这条边。
``check_reachability`` 同理（它跟的是 import）。

**可操作的形式**：改任何一条现有链路时，先读那条链路最外层测试文件的 docstring。
这个项目里那些文件的开头通常有一句话写明了「这条链路不碰什么」——**那句话就是契约**，
而它不会出现在任何门禁的检查项里，只能靠人去读。

**代价**：这一趟 e2e 失败是环境与代码各占一半——8100 那天正好不在（见第 140 条），
所以「缺桩」与「端点不可达」叠在一起。分开看，**缺桩是实打实的缺陷**：就算 8100 在，
e2e 也会去打真推理，而真推理不属于 ``make ci``。

### 第 140 条 · 8100 是被 GPU 显存挤死的，不是被这一轮的调用打死的

我看到 e2e 报 ``8100 不可达`` 时的第一反应是「是不是我新增的模型调用把它打挂了」——
时间线看起来吻合。**去读日志之后否掉了这个猜测**：

```
torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 114.00 MiB.
GPU 0 has a total capacity of 23.56 GiB of which 28.75 MiB is free.
Process 1507974 has 2.40 GiB memory in use.    <- 8091，1.0 的生产后端
Process 4045075 has 2.96 GiB memory in use.
```

而且那一份日志的最后写入是**当天 09:48**，比这一轮 CI（20:02）早了十几个小时。

**同一张卡上跑不下两样东西**：vLLM 的 ``gemma4-12b-it`` 按 ``gpu_memory_utilization
0.76`` 占掉约 18 GiB，而 e2e 里 ``test_assistant_turn`` / ``test_visual_search`` 会把
真 chinese-clip 也 ``model.to(device)``。两个 docstring 里早就写了这条实测
（「8100 的 vLLM 占住 24 GB 里的 22 GB，CLIP 在 ``model.to(device)``……」）——
**所以这不是新问题，是那台机器上 ``make ci`` 的已知张力**：一部分 e2e 要 8100 在，
而跑另一部分 e2e 又会把 8100 挤死。重启之后再跑一趟全量 CI，8100 在 22:05 又死了一次，
日志里同样是 ``torch.OutOfMemoryError``。

**处置**：8100 起回来（8091 的 1.0 生产依赖它），把这 3 条 setup 报错如实记为环境失败，
不假装 CI 全绿。**要不要把这条张力消除（比如给 e2e 加显存闸门、或让 vLLM 让出显存）
是另一个决定，不在 S5c 范围内。**

**顺带一条自省**：我第一反应是自责，第二反应才是去读日志。**先读证据再定性** ——
这一轮如果停在自责上，就会去「修」一个我没弄坏的东西。

### 第 141 条 · 环境守卫写着「其余照常红」，做的是「一律跳过」

T87 的 e2e 要在真照片上跑真人脸检测，而那块单卡上还跑着 8100 的 vLLM（实测占
18.5 GB / 24 GB）。显存不够时会话建不起来，而 ``FaceAdapter.detect`` **把自己的异常
吞掉、返回空表**——于是「检测器整个坏了」与「这台机器显存不够」在夹具看来是同一件事。

夹具里写的是：

```python
for path in photos[:PROBE_PHOTO_COUNT]:
    if detector.detect(str(path)):
        return detector
pytest.skip(f"当前部署的人脸检测检不出任何脸（{detector.error or '未报错'}），这条链路验不了")
```

**一旦检测器坏掉，这 6 条用例会静默地不跑，而输出读起来完全正常。**
这正是本项目反复出现的那一家族，而这次的形状是我自己记过的第 133 条：
**docstring 描述的是意图，代码做的是另一件事。** 那段 docstring 的原话是
「环境原因跳过、**其余照常红**」——后半句从来没有实现过。

同一个仓库里**已经有正确的判据**：``conftest.py`` 的 ``skip_or_fail_on_load_error``
（环境标记 → skip，其余 → fail），而 ``integration/test_face_detector.py`` 用的就是它。
新文件既没用它，又声称「判据与 ``test_face_detector.py`` 一致」——**那句声称是不成立的**，
写它的人没有回去看那个文件到底怎么判。

**已修**，并且**验证过它真的会分**：把 ``FaceAdapter.detect`` 改成永远返回空表且不设
``error``（模拟「检测器坏了、异常被吞掉」），改之前是 6 skipped，改之后是 **6 ERROR**，
每个都带着一句话说明为什么这不是部署形态：

```
Failed: 人脸检测没报错，却在 8 张确认有人脸的真照片上一张都没检出来
        ——这不是部署形态，是代码问题（相册里 46/53 张有人脸，探针取的又是头几张）
```

**推论比这一条本身重要**：凡是「环境不满足就跳过」的守卫，都要问一句
**「那实现坏掉时会怎样」**。如果两者的表现相同，那个守卫就一定会在某一天替一个真 bug
打掩护——而它打掩护的方式是让用例不跑，不是让用例变红，所以**没有任何东西会提醒你**。
