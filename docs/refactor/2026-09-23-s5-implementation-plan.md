# Sentrix Home Web 2.0 — S5 实施计划（人脸与身份层 · 收尾与画像）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 打通离线链路的最后一块——**人脸与身份层**（人脸聚类 → 簇审阅 → 身份确认 → 写实体与人名，
实体与照片建「提及」关系作为身份证据桥梁）与**收尾与画像**（人物外观证据、人物画像）。
它同时解锁三样卡着的东西：事件八分量的 `person_score`（权重 0.20）、`facts` 层、`MemorySpace` 的层次错位。

**Architecture:** 沿用四层（domain / application / infrastructure / api），依赖方向由 `import-linter`
的 5 条契约强制。本切片新增域 `domain/identity`（人脸样本 / 多视角原型 / 贪心聚类 / 身份实体 /
提及关系，纯规则）；人脸检测与向量是 infrastructure 适配器（ONNX + InsightFace，**S5b 才需要**）；
簇审阅与命名是 application 用例；HTTP 在 api 层。

**Tech Stack:** Python 3.11 / FastAPI / 标准库 `sqlite3` · pytest / ruff / mypy --strict / import-linter
· **S5b 新增**：`onnxruntime-gpu`、`insightface`、`opencv-python-headless`（2.0 venv 实测一个都没有）

**依据文档:** `docs/architecture/2026-09-21-sentrix-home-web-2-implementation-design.md`、
`docs/refactor/behaviour-baseline.md`（人脸一节）、`docs/refactor/frozen.md`、
`docs/refactor/s4-deviations.md`（§4 第 287/609 行把身份层记在 S5）、
`docs/refactor/2026-09-22-s4-implementation-plan.md:308,309,313`（三处明确「留给 S5」的能力）

---

## 本文档是怎么写出来的

做法与 S2/S3/S4 相同，**不是照着意图写的**。本文档有**两轮**，两轮的做法分开写：

### 第一轮（规划 · 2026-09-23 上午）

1. 在 153 上建了隔离副本 `/home/asus/s5verify/repo`（真实仓库一个字节都没动），
   跑通 `make install` 与基线 `make ci`；
2. **只读**精读 1.0 的九个文件：`backend/face_clustering.py`（182 行，全文）、
   `backend/face_embeddings.py`（245 行，全文）、`backend/face_detector.py`（181 行，全文）、
   `backend/model_clients.py` 的 `FaceAdapter` 全段（`:1616-1938`）、
   `backend/db.py` 的人脸与身份段（`:400-1400` 的 DDL、`:4400-5600` 的实体/人脸/关系函数、
   `:1494` 的 facts 门禁）、`backend/app.py` 的簇审阅四端点（`:1969-2100`）、
   people 路由（`:1498-2420`）、`_seed_face_detect`（`:2253`）、
   `_refresh_confirmed_person` / `_analyze_confirmed_person_appearance`（`:1997/2022`）、
   `person_insights.py` / `person_portraits.py` / `person_moments.py` / `person_graph.py` /
   `person_appearance.py` 五个模块全文、`scripts/runtime/start_sentrix_api_8091.sh`；
3. 把 **Task 69**（人脸聚类纯规则）在副本里真的写出来：先红灯、再绿灯、再 `make ci`；
4. **按约定在第 18 个任务处停下**（「超过约 15 个任务就停下报告，提议拆分」），
   提议拆成 S5a / S5b / S5c，并列了 7 条待裁决。

### 第二轮（实施 · 2026-09-23 下午，用户裁决之后）

5. 用户裁决 7 条（4 条明确裁决 + 3 条按证据定），本文档按裁决改写：编号调整
   （裁决③新增 Task 84）、画像三张表的解冻与落点（裁决②）、facts 来源反转（裁决④）、
   计划范围定成 S5a + S5b（裁决①）；
6. 对第一轮报告的**五条顾虑**逐条处置：顾虑①固化成 CI 真假红灯判据脚本、
   顾虑②做了实测复核并定下「先可回滚试装」的处置、顾虑③给出设计确认、
   顾虑④按证据走并登记、顾虑⑤定位成 checker 的一个洞并补检查；
7. **逐任务实施 S5a + S5b，每个任务一条红灯、一条绿灯、一次完整 `make ci`**，
   全部原始输出留在本文档与 `/home/asus/s5verify/logs/`。

### 由此产生的硬保证

- 移植清单里每一条的 **1.0 位置（文件 + 行号 + 符号）都是实际读过的**；
- 每条「必须钉住的行为」都是 1.0 源码里的**常量、分支或边界**，不是转述；
- **已落地任务**的红灯 / 绿灯 / `make ci` 是**实际观察到的输出**，且文档里的代码块
  是**从真实文件按行区间读出来的**（`expand.py`），不是手抄的——
  所以「文档里的代码」与「仓库里的代码」逐字节相同，这条由 `verify_plan.py` 机械校验。

---

## 与 S0–S1 / S2 / S3 / S4 的关系

### 任务编号

S0–S1 用了 Task 1–24，S2 用了 25–33，S3 用了 34–45，S4 用了 46–68，全局连续。
S5 顺着往下排，**第二轮按裁决③调整过一次**（新增「全局重聚类的维护入口」）：
S5a = **69–76**，S5b = **77–84**，S5c = **85–87**。变更原因见「用户裁决」一节的「编号变更」。

### S4 明确留给 S5 的三处（本切片必须收口）

| 出处 | 原文 | S5 怎么收 |
|---|---|---|
| `s4-implementation-plan.md:308` | `event_participants` / `event_entities` / `select_event_cover`「强依赖 `entities`」→ **S5** | Task 81 |
| `s4-implementation-plan.md:309` | `person_bridge` 词法字段「没有 `entity_mentions` 可桥」→ **S5** | Task 83 |
| `s4-implementation-plan.md:313` | `facts` 的 `purge_unanchored_facts`「按 `entities` 里 confirmed person 的名字过滤」→ **S5** | Task 82 |
| `s4-implementation-plan.md:761-766` | `AssignEvents.__init__` 的 `confirmed_people` 默认恒空集，注释写明「这是 S5 的输入」 | Task 80 |
| `application/memories/maintain_facts.py:7-27` | `MaintainFacts` 的 `confirmed_people` 默认恒空集 + `candidates` 在生产装配下也空 | Task 82 |
| `infrastructure/assistant/identity.py:11` | 「S5 落地时替换的是这个适配器（实现同一个 `PersonDirectory` 端口）」 | Task 75 |
| `0005_timeline.sql:49-51` | 「参与者与实体都依赖 S5 的 `entities`，那两张表随 S5 一起建，不在这里留空壳」 | Task 70 |

---

## 前置条件

远端仓库 `/home/asus/Github/sentrix-home-web2.0`，分支 `psh2.0`，起点提交 `f663522`
（S4d 收尾，工作树干净）。隔离副本 `/home/asus/s5verify/repo`（`git clone --local` + `make install`）。

### 外部条件（规划时实测）

| 项 | 实测值 | 怎么来的 |
|---|---|---|
| RetinaFace 权重 | `~/benchmarks/retinaface/retinaface_r50.onnx`（按 `$HOME` 推导，不写死用户名） | `face_detector.py:30-32` |
| buffalo_l 权重 | `~/.insightface/models/buffalo_l/w600k_r50.onnx` | `model_clients.py:1690-1697` |
| AdaFace 权重 | `/home/asus/models/AdaFace/pretrained/adaface_ir50_ms1mv2.ckpt`（700,286,703 B，`ls -la` 实见） | 见「不移植」第 1 条 |
| GPU | 1 张 3090。**第一轮**实测 13124 MiB / 24576 MiB 已用；**2026-09-23 复核是 5856 MiB / 24576 MiB 已用**（剩 18.7 GB） | `nvidia-smi --query-gpu=memory.used,memory.total --format=csv` |
| 2.0 venv 人脸依赖 | **`onnxruntime` / `insightface` / `opencv` 一个都没有**；只有 `torch 2.14.0` / `cn-clip 1.6.0` / `numpy 2.4.6` | `.venv/bin/pip list` 实测 |
| 1.0 venv 人脸依赖 | `insightface 1.0.1` / `onnxruntime-gpu 1.23.2` / `opencv-contrib-python 4.10.0.84` | `.venv/bin/pip list` 实测 |
| `memory_spaces.include_in_people` | **列已存在**（`0001_core.sql:12`，`MemorySpace` 数据类没映射它） | 读迁移原文 |
| `observations.people_json` | **2.0 没有**（1.0 有） | `grep` 迁移目录无命中 |
| `event_participants` / `event_entities` / `event_revisions` | **2.0 没有**（0005 注释明确留给 S5） | `0005_timeline.sql:49-51` |

### 2.0 侧现成的落点（**不用新建的部分**）

| 落点 | 位置 | 现在的样子 |
|---|---|---|
| `person_score` 打分 | `domain/memories/events.py:79,280,306` | `WEIGHTS["person"]=0.20`、`PERSON_MATCH=0.8`、`_person_score(people, event_people)`、`ObservationAnchor.people` / `EventAnchorSet.people` **都已就位**，只缺生产者 |
| 事件归属的注入点 | `application/memories/assign_events.py:123,188` | `confirmed_people: Callable[[str], frozenset[str]] = lambda _: frozenset()`；`:188` 已经把它喂进 `ObservationAnchor`（`people=self._confirmed_people(observation.id)`）——**接线只剩「给一个真的实现」** |
| facts 门禁的注入点 | `application/memories/maintain_facts.py:65,78` | `confirmed_people: Callable[[str], frozenset[str]]`（**scope_id → 名字集合，注意是名字不是 id**） |
| 人物目录端口 | `domain/assistant/ports.py:98` + `models.PersonProfile` | `PersonDirectory.lookup(name, scope_id)`；空实现 `NoIdentityDirectory`（`infrastructure/assistant/identity.py`） |
| 容器装配点 | `bootstrap/container.py:84-87,134` | `spaces: SqliteSpaceRepository` 是全仓**唯一**「字段类型落在 infrastructure」的例外，由 `tests/unit/test_container_wiring.py` 的 `EXCEPTIONS` 表强制 |
| 迁移执行器 | `infrastructure/persistence/database.py` | `0006_*.sql` 直接放进 `migrations/`；**注意 `_statements()` 用朴素 `split(";")`，脚本里不能有含分号的字符串常量** |
| 词法投影 | `application/retrieval/retrieve_lexical.py` + `repositories/search_terms.py` | `person_bridge` 字段类型**故意没建**，注释写明属于 S5 |

---

## 移植清单：1.0 有什么、S5 要哪些

### A. 移植（S5 需要）

| # | 能力 | 1.0 的确切位置 | 2.0 落点 | 移植方式 |
|---|---|---|---|---|
| 1 | 向量归一化 / 余弦 | `face_embeddings.py:19-24` `normalize_embedding`、`face_clustering.py:12-15` `cosine` | `domain/identity/faces.py` | **原样**：空或长度不等 → `0.0`；范数 0 → `[]` |
| 2 | 人脸质量 | `face_embeddings.py:233-244` `compute_face_quality` | 同上 | **原样**：`0.40·conf + 0.20·clamp(area/0.08) + 0.20·clamp(sharp) + 0.20·max(0.15, 1−min(90,|yaw|)/90)`，末了再 clamp |
| 3 | 姿态桶 | `face_embeddings.py:224-230` `pose_bucket` | 同上 | **原样**：取 `pose[1]`，`|yaw| <= 20.0` → `frontal`，正负分左右；`pose` 为 `None`/不足 → `yaw=0` → frontal |
| 4 | 贪心多视角聚类 | `face_clustering.py:56-147` `FaceClusterer` | 同上 | **原样**：默认 `0.30/0.30/6/"all"/3/0.1`；`fit` 按 `(-quality, id)` 排序；`noise = quality < minimum_quality`；簇 id `cluster-{n+1}` |
| 5 | 原型维护 | `face_clustering.py:129-147` `_update_prototypes` | 同上 | **原样**：每姿态桶一条、同桶只在新样本 quality 更高时替换、按 `(-quality, pose_bucket, sample_id)` 排序后截断到 `prototype_limit` |
| 6 | 跨模型保护 | `face_clustering.py:95-96`（簇内签名不一致 → 跳过）；`db.py:4876-4884`（原型签名比对） | 同上 + Task 72 | **原样**：`(embedding_model, embedding_version)` 必须与簇内原型之一完全相等，否则整个簇跳过 |
| 7 | 人脸检测（高召回） | `face_detector.py:1-181` `RetinaFaceTiledDetector` | `infrastructure/models/face_detector.py` | **原样**：BGR + mean 104/117/123、priors `STEPS=[8,16,32]`/`MIN_SIZES=[[16,32],[64,128],[256,512]]`、`VARIANCES=(0.1,0.2)`、NMS `0.4`、置信筛 `0.05`；**两趟**：粗 1024/128 过，空了才细 640/256 且阈值降到 `0.1` |
| 8 | SCRFD 复核闸门 | `model_clients.py:1718-1840` `_detect_retina_tiled` | `infrastructure/models/face_adapter.py` | **原样**：`FACE_MIN_SIZE=64`、`FACE_VERIFIED_SCRFD_SCORE=0.7`、`FACE_VERIFIED_MIN_AREA=0.003`、`FACE_IDENTITY_MIN_QUALITY=0.55`；`_bbox_agreement(iou>=0.05)` + `_landmark_sanity`；**只有 verified 才准建簇，uncertain 只做证据** |
| 9 | 人脸向量 | `model_clients.py:1690-1710` `_load_buffalo_recognition` / `:1711-1716` `_recognition_embed` | `infrastructure/models/face_adapter.py` | **原样**：`w600k_r50.onnx`、`((x−127.5)/128.0)`、112×112、512 维 |
| 10 | 在线匹配 | `db.py:4815-4911` `add_face_instance` | `application/identity/enroll_face.py` + 仓储 | **原样**：资格闸门 → 同 scope 且 `status IN ('pending','confirmed')` 的簇 → 原型签名过滤 → 代表向量 max 余弦 → `< 0.30` 建新簇；`quality >= 0.30` 才去匹配已有簇 |
| 11 | 原型刷新 | `db.py:4922-4955` `_refresh_face_prototypes` | 仓储 | **原样**：先删后插；每 `pose_bucket` 一条（`quality DESC, created_at ASC` 取首）；回写 `member_count = 该簇实例总数` |
| 12 | 提及桥 | `db.py:4912-4921` `_link_confirmed_entity_mention` + `:5435` `_add_entity_to_observation` | 仓储 | **原样**：**只有 `status='confirmed'` 的实体才写 mention**；`observations.people_json` 追加 `{entity_id, name, status}` |
| 13 | 簇审阅四件 | `db.py:5152` `confirm_face_cluster` / `:5579` `reject_face_cluster` / `:4776` `merge_face_clusters` / `:4797` `split_face_instance`；HTTP `app.py:1981/2061/2072/2087` | `application/identity/review_cluster.py` + 路由 | **原样**：confirm 写 `entities(status='confirmed', confidence=MAX(…,1))` + 簇 `confirmed` + 逐实例写 mention + `family_role` 非空时写事实 `("家庭角色", role, 1.0)`；**confirmed 簇拒绝 → 409；两个 confirmed 人物合并 → 409；跨 scope 合并 → 409**；merge 只改 `face_instances.cluster_id`；split 用该实例的 embedding 建新簇 |
| 14 | 同名合并 | `db.py:5194-5245` `_merge_cluster_into_person` + `:4407` `find_confirmed_person_by_name` | 同上 | **原样**：同名 → 并入已确认人物，源簇 `rejected` 且 `member_count=0`；`find_confirmed_person_by_name` **先精确名、再遍历 aliases** |
| 15 | 命名 | `db.py:4436-4466` `rename_person` + `:4399` `person_aliases` + `:4428` `set_person_aliases` | `application/identity/name_person.py` + 路由 | **原样**：改名时**把旧名折进 aliases**、`name_state='confirmed'`、写 `entity_revisions(source='user_rename')` |
| 16 | 候选拒绝 | `db.py:5290-5341` `delete_person_candidate` | 仓储 | **原样**：confirmed 抛错；**删实体与全部派生关联，但 `face_instances.cluster_id = NULL` 保留人脸证据** |
| 17 | 预置身份 | `app.py:2253-2310` `_seed_face_detect` + `db.py:5343-5396` `seed_person_identity` | `application/identity/seed_identity.py` | **原样**：三级降级（检测 → 2× 放大重试 → 整图 embedding 兜底）；建 confirmed entity + confirmed cluster + 实例 + 原型；**只在 `/api/people/seed*` 上用，不在相册导入链路上** |
| 18 | `person_score` 输入 | `db.py:2241-2260` `_confirmed_entity_ids_for_observation` / `_for_event`；`db.py:2093-2095` | `AssignEvents` 的生产方（Task 80） | **原样**：确认实体的交集非空 → `0.8`；喂进已有的 `ObservationAnchor.people` / `EventAnchorSet.people` |
| 19 | 事件参与者投影 | `db.py:5549-5578` `_refresh_event_participants` + `:2486` `upsert_event_participant` | `application/memories/…` + 仓储（Task 81） | **原样**：`visible_subject` 置信度 `0.75`、`captured_by` 取 asset 的 `source_confidence`（缺省 `0.5`）；每个受影响事件 `revision+1` 并重算摘要 |
| 20 | 确认后重分段 | `db.py:5450-5481` `resegment_events_for_confirmed_entity` | 同上（Task 81） | **原样**：只在新确认人物**桥起的两场事件**之间合并；6 小时硬窗 + 重跑八分量 + `total >= 0.50` |
| 21 | facts 门禁 | `db.py:1494-1503` `is_confirmed_person_name` + `pipeline.py:348` | `MaintainFacts.confirmed_people`（Task 82） | **原样**：`entities` 里 `entity_type='person' AND status='confirmed' AND canonical_name = ?`；**只校验 subject** |
| 22 | facts 清理 | `db.py:3433-3447` `purge_unanchored_facts` | 仓储（Task 82） | **原样**：删掉 subject 不在已确认人物名集合里的事实（限本 scope） |
| 23 | `person_bridge` 投影 | `retrieval_indexes.py:20,180` | `domain/retrieval/lexical.py` + 写侧（Task 83） | **原样**：字段类型 `person_bridge`，值源是观察的 `people` |
| 24 | 实体检索通道 | `retrieval/entity.py:1-60` | `application/retrieval/retrieve_entity.py`（Task 83） | **原样**：人条件是**硬条件**；优先读 `person_bridge` 投影；**从不宣布身份**，只给候选 |
| 25 | 外观证据裁剪 | `person_appearance.py:4-25` `expanded_person_crop` | `domain/identity/appearance.py`（Task 84） | **原样**：`head_scale=1.35` / `body_height_scale=4.5` / 上扩 `face_height*0.35`；框非法或结果 `< 24×48` → **抛错，不回退整图** |
| 26 | 外观观察 | `app.py:2022-2058` `_analyze_confirmed_person_appearance` + `db.py:4482` | 用例 + `VlmObserver`（Task 84） | **原样**：每人每事件只取 `quality DESC, detection_confidence DESC LIMIT 1` 一张；写 `person_appearance_evidence`，唯一键 `(person, face_instance, model)`；**确认人脸簇时同步触发** |
| 27 | 人物画像证据包 | `person_portraits.py:43-118` `compile_portrait_evidence` | `domain/identity/portrait.py`（Task 85） | **原样**：`max_moments=12`、`per_event=2`、每主题最多 2 条引用、高频动作 `most_common(3)` |
| 28 | 画像强制校验 | `person_portraits.py:119-153` `_evidence_ref_valid` / `validate_portrait` | 同上 | **见「偏离」第 2 条**：三条规则原样搬（80 ≤ 汉字 ≤ 500、themes 2–6、每个 theme 的 `evidence_refs` 非空），但 `_evidence_ref_valid` 的白名单后门**要修** |
| 29 | 确定性兜底画像 | `person_portraits.py:156-181` `deterministic_portrait` | 同上 | **见「偏离」第 3 条**：1.0 的兜底**必然通不过自己的校验**（实测 41 字 < 80），是缺陷 |
| 30 | 核心人物排序 / 代表照 | `person_insights.py:31-111` `core_person_score` / `rank_core_people` / `select_representatives` | `domain/identity/insights.py`（Task 85） | **原样**：六项权重 `0.30/0.20/0.15/0.10/0.10/0.15`、归一化除数 `8.0/6.0/20.0/4.0/4.0`、confirmed 加成 `0.20`、单日惩罚 `0.35`、`rank_core` 硬上限 10、core 门槛 `date_count >= 2 或 confirmed` |
| 31 | 六阶段编排 | `person_insights.py:11-18,159-253` | `application/identity/run_insights.py`（Task 85） | **改**：见「需裁决」第 1 条（三张表被冻结） |
| 32 | 簇列表过滤 | `db.py:5133-5151` `list_face_clusters` | 仓储 | **原样**：强制 `memory_spaces.include_in_people = 1`；`pending` 簇的名字对外显示成「待命名成员」 |
| 33 | 人脸裁剪端点 | `app.py:2508-2533` | 路由（Task 75） | **原样**：`thumbnail((256,256))`、JPEG `quality=88`、`Cache-Control: private, max-age=86400`、bbox 非法 → 422 |

### B. 明确不移植（附证据）

| 能力 | 1.0 位置 | 理由（可摆出来的证据） |
|---|---|---|
| **AdaFace / MagFace 适配器** | `face_embeddings.py:72-221`、`platform_profile.py:257-274` | **它在 1.0 已经是死代码。** `FaceAdapter.__init__` 把 `identity_adapter = None`（`model_clients.py:1629`），全仓**再无第二处赋值**（`grep -rn "\.identity_adapter\s*="` 只命中 `model_clients.py:1629` 与 `tests/test_face_clustering.py` 的 6 处），而唯一的调用点 `app.py:2299` 是 `pipeline.face.identity_adapter.embed(_crop)` → 必抛 `AttributeError` → 被它上面那行裸 `except Exception: return []` 吞掉。1.0 的测试自己写了 `@unittest.skip("legacy identity_adapter contract removed; identity is fixed to buffalo_l")`（`tests/test_face_clustering.py:126`）。**实际生效的身份向量是 buffalo_l 的 w600k_r50（512 维）**（`_detect_retina_tiled_unlocked` 两个分支都把 `embedding_model` 写成 `"buffalo_l"`）。启动脚本里那串 `FACE_EMBEDDING_MODE=adaface ADAFACE_* ` 没有任何读取者（`face_embedding_is_adaface()` 全仓零调用）。 |
| **全局重聚类** `recluster_faces` + `_attach_uncertain_faces` + `_sync_face_entity_statuses` | `db.py:4956` / `:5048` / `:5118` | **调用点全在 benchmark 脚本里**：`scripts/benchmarks/ingest_album3_v2.py:67`、`scripts/benchmarks/ingest_household_face_benchmark.py:57`；生产导入路径只走在线匹配。与 S4 不移植 `_event_pair_score` 的判据同形（那个的两个调用点也都在 benchmark）。**注意 `_attach_uncertain_faces` 与 `_sync_face_entity_statuses` 只被 `recluster_faces` 调**（`db.py:5038-5039`），所以它们也一起不移植。 |
| **benchmark 空间名黑名单** | `db.py:1313-1324` `_migrate_evaluation_spaces` | 它把 `'PhotoBench%' / 'photobench%' / '%_e2b'` 硬编码进生产代码（原文：`WHERE name LIKE 'PhotoBench%' OR name LIKE 'photobench%' OR id LIKE '%_e2b' OR id LIKE 'photobench%'`）。**同一判据已写进 `frozen.md`**（冻结硬编码泄漏条目）。要移植的是 `include_in_people` 这个**机制**（2.0 的列已存在），不是名字黑名单——谁建测试空间谁自己置 0。 |
| **`tools.py` 绕过 include_in_people 的查人物** | `agent_runtime/tools.py:688-698` | 注释自陈「绕过 `list_entities` 的 `include_in_people` 过滤，benchmark scope 也能用」——为评测开的旁路。 |
| **`persons` 遗留表与 `/api/persons`** | `db.py:515`（DDL）/ `:1327` `_migrate_legacy_persons` / `:3461` `update_person` / `:3499` `upsert_person`；`app.py:1498,1505` | 无生产写入者（`upsert_person` 全仓零调用）；真数据全在 `entities`。1.0 自己把 legacy person 迁进 native entity。 |
| **`pairwise_metrics` 及聚类评估脚本** | `face_clustering.py:149-182`、`scripts/benchmarks/evaluate_lfw_clusters.py`、`scan_cluster_params.py` | 只在 benchmark / 测试里用。 |
| **`apply_relationship_threshold`** | `person_graph.py:104-116` | **死代码**：`normalize_person_graph` 里没调它，全仓只有测试调。 |
| **`find_graph_violations` 的结果** | `person_graph.py:187` 返回，`person_insights.py:327/346` 从不读 | 算完即弃。随 S7 一起处理。 |
| **根目录的 `pipeline.py` / `tools.py` / `qdrant_memory.py` 等** | 仓库根 | **过期副本**，无任何模块 import（`app.py:36` 是 `from .pipeline import IngestionPipeline`）。不要照它写。 |
| **人物瞬间 / 关系推断 / 洞察运行 / 画像修订四张表** | `person_moments.py`、`person_graph.py`、`db.py:1127/1165/1184/1204` | **在 `frozen.md` 冻结清单里（表 0 行）**。见「需裁决」第 1 条。 |

### C. 需要用户裁决（1.0 没有对应实现的，先报不自作主张）

见文末「需要用户裁决的点」，共 7 条。

---

## 文件结构

本切片新增 / 改动的文件（其余保持 S4d 终态的原样）：

```text
sentrix-home-web2.0/
├── apps/api/
│   ├── pyproject.toml                      【S5b】+onnxruntime-gpu / insightface / opencv-python-headless
│   └── src/sentrix/
│       ├── domain/
│       │   ├── identity/                   【新包】
│       │   │   ├── __init__.py             包标记（空）
│       │   │   ├── faces.py                归一化 / 余弦 / 质量 / 姿态桶 / 贪心多视角聚类   ← Task 69 已落地
│       │   │   ├── models.py               身份实体 / 提及 / 簇 / 实例 / 原型的领域模型       ← Task 70
│       │   │   ├── ports.py                FaceRepository / EntityRepository / FaceDetector / FaceEmbedder  ← Task 71
│       │   │   ├── appearance.py           expanded_person_crop（纯几何）                  ← Task 84
│       │   │   ├── portrait.py             证据包 / 强制引用校验 / 确定性兜底                ← Task 85
│       │   │   └── insights.py             核心人物排序 / 代表照选择（纯规则）               ← Task 85
│       │   ├── spaces/                     【新包】MemorySpace 归位                        ← Task 76
│       │   │   ├── __init__.py
│       │   │   ├── models.py               MemorySpace（从 infrastructure 迁来）
│       │   │   └── ports.py                SpaceRepository
│       │   ├── memories/ports.py           +EventParticipantRepository（Task 81）
│       │   ├── retrieval/lexical.py        +person_bridge 字段类型（Task 83）
│       │   └── retrieval/ports.py          +EntitySearch（Task 83）
│       ├── application/
│       │   ├── identity/                   【新包】
│       │   │   ├── enroll_face.py          在线匹配（db.py:4815 的等价物）                 ← Task 72
│       │   │   ├── review_cluster.py       confirm / reject / merge / split                ← Task 73
│       │   │   ├── name_person.py          rename / aliases / 同名合并                     ← Task 74
│       │   │   ├── seed_identity.py        _seed_face_detect 三级降级                      ← Task 79
│       │   │   ├── person_directory.py     PersonDirectory 实现（替掉 NoIdentityDirectory）← Task 75
│       │   │   └── run_insights.py         六阶段编排                                      ← Task 85
│       │   ├── memories/assign_events.py   +confirmed_people 的真实来源                     ← Task 80
│       │   ├── memories/maintain_facts.py  +confirmed_people / candidates 的真实来源        ← Task 82
│       │   └── retrieval/retrieve_entity.py 实体通道                                        ← Task 83
│       ├── infrastructure/
│       │   ├── models/face_detector.py     RetinaFaceTiledDetector（照搬 face_detector.py）  ← Task 77
│       │   ├── models/face_adapter.py      SCRFD 复核 + w600k_r50 向量 + quality            ← Task 77/78
│       │   ├── models/onnx_runtime.py      provider 链与显存上限（照搬）                     ← Task 77
│       │   └── persistence/
│       │       ├── migrations/0006_identity.sql                                             ← Task 70
│       │       └── repositories/
│       │           ├── faces.py            face_clusters / face_instances / face_prototypes  ← Task 71
│       │           ├── entities.py         entities / entity_* / relationships               ← Task 71
│       │           ├── participants.py     event_participants / event_entities（Task 81）
│       │           ├── appearance.py       person_appearance_evidence（Task 84）
│       │           ├── spaces.py           改：SQL 保留，模型/端口搬走（Task 76）
│       │           └── search_terms.py    +person_bridge 投影（Task 83）
│       ├── api/routes/identity.py          face-clusters / people / face-instances crop 路由 ← Task 75
│       ├── api/routes/insights.py          画像与人物洞察路由（视裁决）                       ← Task 85
│       └── bootstrap/container.py          +identity 端口装配；**删掉 spaces 例外**（Task 76）
└── docs/{architecture/module-map.md, refactor/{behaviour-baseline,frozen,capability-map.jsonl,s5-deviations.md}}
```

---

## 依赖图与执行顺序

**先画图再排任务。** S0/S1 各踩过一次循环依赖（单独看都对、顺序错了），所以下面这张图里
每一条箭头都要求「上面先落地」。

```text
Task 69  domain/identity 纯规则（归一化 / 余弦 / 质量 / 姿态 / 贪心多视角聚类）
   │                                                        ← 已落地，隔离副本 s5-tdd
   ├── Task 70  迁移 0006：identity 表族 + observations.people_json + events 参与者投影表
   │      │
   │      └── Task 71  身份仓储与端口（faces / entities / PersonDirectory 的实现）
   │             │
   │             ├── Task 72  在线匹配用例（资格闸门 → 匹配 → 建簇 → 原型 → 向量 → mention）
   │             │      │
   │             │      └── Task 73  簇审阅用例（confirm / reject / merge / split）
   │             │             │
   │             │             ├── Task 74  命名与同名合并
   │             │             │      │
   │             │             │      └── Task 75  HTTP 路由（face-clusters / people / crop）+ PersonDirectory 实现
   │             │             │
   │             │             └── Task 80  person_score 接线（entity_mentions → AssignEvents）
   │             │                    │
   │             │                    ├── Task 81  事件参与者 / 实体投影 + 确认后重分段
   │             │                    └── Task 82  facts 门禁开通 + purge_unanchored_facts
   │             │
   │             └── Task 76  MemorySpace 迁进 domain + SpaceRepository 端口（可与 72–75 并行）
   │
   ├── Task 84  全局重聚类的维护入口（recluster_faces + 两个从属函数）  ← 依赖 71；不需要 GPU
   │
   ├── Task 77  人脸检测适配器（RetinaFace tiled + SCRFD 复核）
   │      │
   │      └── Task 78  人脸向量适配器（buffalo_l w600k_r50）+ 质量/姿态归属
   │             │
   │             └── Task 79  导入路径接线（人脸先于事件；容器装配；seed 三级降级）
   │                    │
   │                    └── Task 83  person_bridge 词法投影 + 实体检索通道
   │
   ├── Task 85  外观证据（裁剪 + 观察者 + 确认时触发）        ← 依赖 73 / 75
   └── Task 86  人物画像（证据包 + 强制校验 + 同步编排）      ← 依赖 75 / 81 / 82 / 85

Task 87  e2e 验收（真照片 → 人脸簇 → 确认 → 事件 / 事实 / 画像）  ← 依赖全部
```

### Task 84 的位置（裁决③新增）

Task 84 依赖 Task 71（仓储）与 Task 69（`FaceClusterer`），**不依赖任何模型适配器**，
所以它既可以排在 S5b 的开头，也可以留在 S5a 末尾。**本计划把它放在 S5b**，
理由是它的验收要拿真实的人脸实例（`face_instances` 里有真 embedding 才谈得上重聚类），
而那些实例来自 S5b 的导入链路。**但它的代码可以更早写**——如果执行时想提前，
把 Task 84 挪到 Task 76 之后即可，依赖图上没有任何一条箭头会被违反。

### 为什么不能换序（三条，都是「单独看都对、顺序错了就出错」）

1. **Task 73 不写 `_refresh_event_participants`，它属于 Task 81。**
   1.0 的 `confirm_face_cluster`（`db.py:5152`）在同一个函数里既写身份（entities / mentions）
   又刷事件投影（`_refresh_event_participants` → `resegment_events_for_confirmed_entity` → `rebuild_person_memory`）。
   2.0 的两半落在不同任务里，**如果 73 直接调用 81 的模块，就是一个循环**：
   81 的参与者投影要读 mentions，而 mentions 由 73 写。
   **处置**：73 落地时 `confirm` 只做身份层四件事（实体 / 簇状态 / mentions / 原型）；
   **81 落地时回头改 `confirm` 那一处**（同一个文件的后继编辑）。
   **不要**为它留一个空的回调钩子——那正是 S4 栽过的「零调用的纯规则」。
2. **Task 70 的迁移必须先于 71/72。** `observations.people_json`、`event_participants`、
   `event_entities` 三处的列/表在 0006 里一次建齐；1.0 是 `CREATE TABLE IF NOT EXISTS`
   + 运行期 `ALTER`（`db.py:1236-1249`），2.0 是编号迁移，加列只能改 0006 或新开 0007。
3. **Task 79 的人脸必须先于事件归并。** 1.0 在 `pipeline.py:316-320` 用注释钉过这件事：
   「A matching confirmed face writes an EntityMention above, so it is available as a real
   identity bridge during this event decision.」顺序写反**不报错**——`_confirmed_entity_ids_for_observation`
   返回空集，`person_score` 恒 0、`corroborated` 更容易为假，
   事件照分照建，只是**分错了**。2.0 用一条对照测试钉（同两张照片，人脸在前 / 在后）。

### 另一条不是顺序、但同样会静默出错的约束

**`quality >= 0.30`（`db.py:4868`）与 `threshold=0.30`（`db.py:4815`）是两个不同的 0.30。**
前者是「低质量人脸不去匹配已有簇」，后者是余弦阈值。数值相同、语义无关，
改一个不影响另一个。移植时**保持两个独立的常量**，并在测试里各钉一条。

---

## 任务拆分

**本切片 19 个任务**（第一轮排到 86，裁决③新增「全局重聚类的维护入口」后是 69–87），
超过「约 15 个」的上限。第一轮按约定停下报告，**用户裁决①：先做 S5a + S5b，S5c 另写。**

| 子切片 | 任务 | 内容 | 是否需要 GPU / LLM | 能不能独立验收 |
|---|---|---|---|---|
| **S5a** | 69–76（8 个） | 身份层地基：纯规则、表、仓储、在线匹配、簇审阅、命名、HTTP、MemorySpace 归位 | **不需要**（端口用假件，模型适配器在 S5b） | 能：簇的生命周期（建 → 审 → 命名）可以端到端跑，只是人脸来自假检测器 |
| **S5b** | 77–84（8 个） | 模型适配与接线：检测 / 向量 / 导入接线 / `person_score` / 事件投影 / facts 门禁 / 实体通道 / **全局重聚类的维护入口** | **需要**（ONNX + InsightFace + 一张 3090）；**Task 84 例外，它只用仓储，不需要 GPU** | 能：真照片导入 → 自动建簇 → 确认 → 事件与事实变化；维护入口可单独触发重聚类 |
| **S5c** | 85–87（3 个） | 收尾与画像：外观证据 / 人物画像 / e2e | **需要**（VLM） | 能：确认人物后画像落库且引用可核 |

**S5a 不含模型适配器，所以它交付时「人脸从哪来」是端口注入的假件。**
这一点必须在 S5a 的验收里写明：**S5a 结束时产品上还不能用**，它是 S5b 的地基。
（S4 的教训：Task 66/67 交付了两块零生产调用的能力，被用户点名为两次栽跟头的其中一次。
S5a 不重复这个形状——它的每个模块都会被 S5b 的导入路径调用，只是 S5b 才接线。
用户裁决①已经选了 S5a + S5b 一起做，所以这一段的「S5a 单独不可用」在实际执行里
不会成为一个交付形态。）

**每个新模块都要过一条机械检查**（S4 栽过两次「零调用的纯规则」与「零调用的
`SummarizeEvent`」，所以这条不靠人记）：**新模块必须在生产装配路径上找得到一个调用者**——
即从 `Container.build()` 或某个 `api/routes/` 出发能走到它。检查写成脚本
`scripts/check_reachability.py`，挂进 `make ci` 的 `arch` 之后。**它只覆盖模块，不覆盖表**：
表没有「能力」语义，它是 schema 声明；这个区分与它的代价写在 `s5-deviations.md` §2c.1。

---

## 逐任务定义

> 本节的每一项都写成「**移植 1.0 `backend/X.py` 的 `Y`**」。
>
> **三条写作纪律**：
>
> 1. **已落地的任务写「验证过的代码块」，未落地的任务不写 2.0 侧代码。**
>    第一轮本文档一个 2.0 代码块都没有，理由是「没跑过的代码写出来就是 S2 那种
>    『错误率全部发生在计划生成阶段』的重演」。这个理由对**未落地**的任务仍然成立，
>    所以它们保持「只写从 1.0 的哪一段搬什么、必须钉住哪些常量」的形态；
>    而**已落地**的任务写出的每一个代码块都是**某个真实文件的精确行区间**，
>    由 `expand.py` 从文件里读出来内联，**不是手抄的**——所以文档里的代码与仓库里的代码
>    逐字节相同，这一点由 `verify_plan.py` 机械校验（见「生成侧对拍」）。
> 2. **每个 `Run` 都真跑过。** 红灯 / 绿灯 / `make ci` 三段都是**原始输出**，
>    由 `expand.py` 从日志文件读进来，不做裁剪与美化。
> 3. **红灯分两层。** 红灯 A 是「先写测试」，对从零新建的模块必然是收集期报错——
>    它只证明「测试先于实现落地」，**不证明任何行为被钉住**。所以 Task 72 起每个任务
>    在提交后追加**红灯 B（定点变异）**：把实现里的要害那一行改掉、重跑门禁，
>    让红灯**指向具体的断言**。Task 69/70/71 落地时还没有这条纪律（69 例外，
>    它做过一轮 32 处的变异检查，见该任务一节）。

### Task 69（S5a）· 人脸聚类纯规则 ✅ 已落地

**commit:** `2824a5f`

**Files:**

```text
apps/api/src/sentrix/domain/identity/__init__.py     【新】包标记（空）
apps/api/src/sentrix/domain/identity/faces.py        【新】归一化 / 余弦 / 质量 / 姿态桶 / 贪心多视角聚类
apps/api/tests/unit/test_identity_faces.py           【新】门禁 62 条
docs/architecture/module-map.md                      【改】+2 行
docs/refactor/capability-map.jsonl                   【改】+5 条
```

**移植**：1.0 `backend/face_clustering.py` 全文（除 `pairwise_metrics`）+ `backend/face_embeddings.py`
的四个纯函数。逐符号对回：

| 2.0 的符号 | 1.0 的出处 |
|---|---|
| `normalize_embedding` | `face_embeddings.py:19-24` |
| `cosine` | `face_clustering.py:12-15` |
| `_clamp` | `face_embeddings.py:220-221` |
| `pose_bucket` | `face_embeddings.py:224-230` |
| `compute_face_quality` | `face_embeddings.py:233-244` |
| `FaceSample` / `FacePrototype` / `FaceCluster` / `ClusterResult` | `face_clustering.py:17-56` |
| `FaceClusterer.fit` / `_best_compatible_cluster` / `_same_view_members` / `_matches` / `_update_prototypes` | `face_clustering.py:59-147` |

**没搬的两样**（都写进了 `faces.py` 的模块 docstring）：

- `pairwise_metrics`（`face_clustering.py:149-182`）：评的是「预测标签 vs 真值标签」，
  只在 `scripts/benchmarks/evaluate_lfw_clusters.py` / `scan_cluster_params.py` 里出现，
  2.0 也没有真值来源；
- `FaceEmbeddingAdapter` / `AdaFaceAdapter` / `MagFaceAdapter`（`face_embeddings.py:72-221`）：
  死代码，且要 `os.getenv` —— **`domain-purity` 契约明令禁止 domain import `os`**。

**核心代码** —— `apps/api/src/sentrix/domain/identity/faces.py:46-125`（节选）：

```python
DEFAULT_MATCH_THRESHOLD = 0.30
DEFAULT_MINIMUM_QUALITY = 0.30
PROTOTYPE_LIMIT = 6
DEFAULT_MATCH_STRATEGY = "all"
DEFAULT_TOPK = 3
DEFAULT_PERCENTILE = 0.1

# 1.0 ``face_embeddings.py`` :237-238 的质量公式常数。
QUALITY_WEIGHT_DETECTION = 0.40
QUALITY_WEIGHT_AREA = 0.20
QUALITY_WEIGHT_SHARPNESS = 0.20
QUALITY_WEIGHT_POSE = 0.20
# 人脸框面积占整图 0.08 时面积项给满。
FACE_AREA_RATIO_FULL = 0.08
# pose_score 的地板与满值点：``max(0.15, 1 - min(90, |yaw|) / 90)``。
POSE_SCORE_FLOOR = 0.15
POSE_SCORE_FULL_YAW = 90.0


def normalize_embedding(values: Sequence[float] | None) -> list[float]:
    """L2 归一化。零向量返回空表——空表是「没有可用向量」，不是「和谁都相似度为 0」。"""
    numbers = [float(value) for value in (values or [])]
    norm = math.sqrt(sum(value * value for value in numbers))
    if norm == 0:
        return []
    return [value / norm for value in numbers]


def cosine(left: Sequence[float] | None, right: Sequence[float] | None) -> float:
    """裸点积，**不在这里归一化**。

    ``FaceSample`` 在构造期就把向量归一化了，所以点积即余弦，「阈值 0.30」才是一个
    余弦阈值。这层归一化依赖写在构造期就只证一次，而不是每次比对再补一遍。
    """
    if not left or not right or len(left) != len(right):
        return 0.0
    # strict=True 只是把上面那个长度判断再声明一次，行为不变（ruff B905）。
    return sum(float(a) * float(b) for a, b in zip(left, right, strict=True))


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, float(value)))


def pose_bucket(pose: Sequence[float] | None) -> str:
    """InsightFace 的 pose 是 (pitch, yaw, roll)，桶只看 yaw，即 ``pose[1]``。

    pose 缺失或长度不足时 yaw 取 0.0，也就是落到 frontal：不知道姿态的样本不该被预先
    判成侧脸，否则它连正脸簇的门都进不去。
    """
    values = [float(value) for value in (pose if pose is not None else [])]
    yaw = values[1] if len(values) > 1 else 0.0
    if abs(yaw) <= 20.0:
        return "frontal"
    return "profile_right" if yaw > 0 else "profile_left"


def compute_face_quality(
    detection_confidence: float,
    area_ratio: float,
    sharpness: float,
    pose: Sequence[float] | None,
) -> float:
    """四项加权：检测置信度 0.40 / 面积 0.20 / 清晰度 0.20 / 姿态 0.20，结果 clamp 到 [0,1]。

    姿态项有 0.15 的地板：侧脸是**弱证据**，不是没有证据。整项归零会把侧脸从质量门里
    直接筛掉，而质量门同时还兼任「这个样本能不能参与匹配」——那是两道不同的门。
    ``pose`` 为 None / 空时 yaw = 0 → 姿态项满分 1.0。
    """
    values = [float(value) for value in (pose if pose is not None else [])]
    yaw = abs(values[1]) if len(values) > 1 else 0.0
    pose_score = max(POSE_SCORE_FLOOR, 1.0 - min(POSE_SCORE_FULL_YAW, yaw) / POSE_SCORE_FULL_YAW)
    area_score = _clamp(float(area_ratio) / FACE_AREA_RATIO_FULL)
    sharpness_score = _clamp(sharpness)
    return _clamp(
        QUALITY_WEIGHT_DETECTION * _clamp(detection_confidence)
        + QUALITY_WEIGHT_AREA * area_score
        + QUALITY_WEIGHT_SHARPNESS * sharpness_score
        + QUALITY_WEIGHT_POSE * pose_score
    )
```

上半段：六个构造函数默认值常量（`0.30` / `0.30` / `6` / `"all"` / `3` / `0.1`）+
四个纯函数（`normalize_embedding` / `cosine` / `_clamp` / `pose_bucket` / `compute_face_quality`）。

**核心代码** —— `apps/api/src/sentrix/domain/identity/faces.py:201-288`（节选）：

```python
    def fit(self, samples: Sequence[FaceSample]) -> ClusterResult:
        clusters: list[FaceCluster] = []
        labels: dict[str, str] = {}
        # 先高质量后低质量：簇由最可信的样本先占住，低质量样本只能去附和既有簇，
        # 不能先开一个簇把后面的人拉偏。id 是平局时的确定性次级键。
        ordered = sorted(samples, key=lambda item: (-item.quality, item.id))
        for sample in ordered:
            candidate = self._best_compatible_cluster(sample, clusters)
            if candidate is None:
                candidate = FaceCluster(
                    id=f"cluster-{len(clusters) + 1}",
                    noise=sample.quality < self.minimum_quality,
                )
                clusters.append(candidate)
            candidate.members.append(sample)
            self._update_prototypes(candidate, sample)
            labels[sample.id] = candidate.id
        return ClusterResult(clusters=clusters, labels=labels)

    def _best_compatible_cluster(
        self, sample: FaceSample, clusters: Sequence[FaceCluster]
    ) -> FaceCluster | None:
        # 质量门在相似度之前：不够格的样本根本不进入匹配，见类 docstring 第 1 条。
        if sample.quality < self.minimum_quality:
            return None
        ranked = sorted(
            ((cluster.best_score(sample), cluster) for cluster in clusters if not cluster.noise),
            key=lambda item: (-item[0], item[1].id),
        )
        for score, cluster in ranked:
            if score < self.match_threshold:
                # 列表已按分数降序，这里写 continue 只是照抄 1.0：它的语义是「否掉这一个
                # 簇」，而不是「后面都不可能了」。
                continue
            # 模型身份不同就不能同簇：不同模型的向量不可比，混进同一簇等于拿两个坐标系
            # 的余弦互相比较。整簇否决，不是只忽略这几个成员。
            if any(
                item.model_name != sample.model_name or item.model_version != sample.model_version
                for item in cluster.members
            ):
                continue
            protected = {item.protected_cluster for item in cluster.members if item.protected_cluster}
            if sample.protected_cluster and protected and sample.protected_cluster not in protected:
                continue
            same_view = self._same_view_members(cluster, sample)
            if not same_view or self._matches(sample, same_view):
                return cluster
        return None

    def _same_view_members(self, cluster: FaceCluster, sample: FaceSample) -> list[FaceSample]:
        """参与相似度比对的成员。unknown 姿态用全部成员——它没有自己的桶可比。"""
        if sample.pose_bucket == "unknown":
            return cluster.members
        return [member for member in cluster.members if member.pose_bucket == sample.pose_bucket]

    def _matches(self, sample: FaceSample, members: Sequence[FaceSample]) -> bool:
        # 升序：sims[0] 是最差的那个成员，下面每个策略分支都建立在「下标 0 最差」上。
        sims = sorted(cosine(sample.embedding, member.embedding) for member in members)
        threshold = self.match_threshold
        if self.match_strategy == "p10":
            if len(sims) <= 2:
                return sims[0] >= threshold
            return sims[int(len(sims) * self.percentile)] >= threshold
        if self.match_strategy == "topk":
            k = max(1, min(self.topk, len(sims)))
            top = sims[-k:]
            return sum(top) / len(top) >= threshold
        if self.match_strategy == "mean":
            return sum(sims) / len(sims) >= threshold
        # 默认（含 "all"）：最差的那个成员也要过阈值，即全票通过。
        return sims[0] >= threshold

    def _update_prototypes(self, cluster: FaceCluster, sample: FaceSample) -> None:
        same_view = [item for item in cluster.prototypes if item.pose_bucket == sample.pose_bucket]
        if same_view:
            # 每个姿态桶只留一张，而且**就地**换掉：位置本身携带桶的语义，追加会让同一个
            # 桶占掉多个 prototype_limit 名额，把别的视角挤出去。
            current = max(same_view, key=lambda item: item.quality)
            if sample.quality > current.quality:
                current.sample_id = sample.id
                current.embedding = sample.embedding
                current.quality = sample.quality
            return
        cluster.prototypes.append(
            FacePrototype(sample.id, sample.embedding, sample.quality, sample.pose_bucket)
        )
        cluster.prototypes.sort(key=lambda item: (-item.quality, item.pose_bucket, item.sample_id))
        del cluster.prototypes[self.prototype_limit :]
```

下半段：`fit` 的贪心主循环、`_best_compatible_cluster` 的整簇否决与「空姿态桶直接命中」、
`_same_view_members` / `_matches` 的五种策略、`_update_prototypes` 的按桶留优。

**门禁** —— `apps/api/tests/unit/test_identity_faces.py`：62 条，逐条对应 1.0 源码里的
一个常量或分支。文件较长（625 行），这里不整体内联；它的**断言清单**按 1.0 的符号分组：

| 组 | 条数 | 钉住的东西 |
|---|---|---|
| `cosine` / `normalize_embedding` | 7 | 空或长度不等 → `0.0`；范数 0 → `[]`；裸点积（**不**再除模） |
| `FaceSample` | 5 | 构造时归一化、quality 夹到 `[0,1]`、1.0 的字段默认值、frozen |
| `pose_bucket` | 3 | 取 `pose[1]`；`|yaw| <= 20.0` 是 frontal 且正负分左右；pitch/roll 不参与；`None`/不足 → yaw=0 |
| `compute_face_quality` | 8 | 四项权重、逐项隔离、`0.15` 姿态地板、面积除数 `0.08`、输入先 clamp |
| 六个默认参数常量 | 3 | `0.30/0.30/6/"all"/3/0.1` 逐个钉 |
| `fit` 主循环 | 9 | 按 `(-quality, id)` 排序、簇从 1 编号、noise ⟺ 单例+低于质量门、`labels` 覆盖每个样本 |
| 匹配策略 | 12 | `all`/`mean`/`topk`/`p10`/`unknown` 五种、`topk` 的 k 夹取、`p10` 的 `<= 2` 退化 |
| 整簇否决 | 6 | `embedding_model` / `embedding_version` 不一致 → 整簇跳过；`protected` 值不一致 → 整簇跳过 |
| 多视角 | 2 | **空姿态桶直接命中**（含它绕过的只是 `_matches`、不是阈值这一条） |
| `_update_prototypes` | 4 | 每桶一条、同桶只被更高 quality 替换、排序截断、同 quality 按桶名 |
| `best_score` | 2 | 无原型 → 0；否则取原型上的最大值 |

**Run:**（仓库根，隔离副本 `/home/asus/s5verify/repo`）

```bash
# 红灯：只落测试，不落实现
.venv/bin/pytest apps/api/tests/unit/test_identity_faces.py -q
```

**Expected（红灯）**：收集期报错（实现模块不存在）。

**红灯原文**：

```text

==================================== ERRORS ====================================
_________ ERROR collecting apps/api/tests/unit/test_identity_faces.py __________
ImportError while importing test module '/home/asus/s5verify/repo/apps/api/tests/unit/test_identity_faces.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
../../.local/share/uv/python/cpython-3.11.16-linux-x86_64-gnu/lib/python3.11/importlib/__init__.py:126: in import_module
    return _bootstrap._gcd_import(name[level:], package, level)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
apps/api/tests/unit/test_identity_faces.py:12: in <module>
    from sentrix.domain.identity.faces import (
E   ModuleNotFoundError: No module named 'sentrix.domain.identity'
=============================== warnings summary ===============================
.venv/lib/python3.11/site-packages/fastapi/testclient.py:1
  /home/asus/s5verify/repo/.venv/lib/python3.11/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

.venv/lib/python3.11/site-packages/starlette/testclient.py:53
  /home/asus/s5verify/repo/.venv/lib/python3.11/site-packages/starlette/testclient.py:53: DeprecationWarning: The anyio.abc.BlockingPortal alias is deprecated, use anyio.from_thread.BlockingPortal instead.
    _PortalFactoryType = Callable[[], AbstractContextManager[anyio.abc.BlockingPortal]]

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ============================
ERROR apps/api/tests/unit/test_identity_faces.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
```

> **一处如实说明**：这次的红灯是「模块不存在导致的收集错误」，它证明的是
> **「测试文件先于实现落地」**，**不证明任何一条行为被钉住**。真正证明行为被钉住的是
> 绿灯的 62 条用例——每一条都对应 1.0 源码里的一个常量或分支。
> **S5 后续任务的红灯要求更高**：实现先落地、再删掉那一处，让红灯指向具体断言。

**Run:**（绿灯）

```bash
.venv/bin/pytest apps/api/tests/unit/test_identity_faces.py -q
```

**Expected（绿灯）**：`62 passed`。

**绿灯原文**：

```text
..............................................................           [100%]
=============================== warnings summary ===============================
.venv/lib/python3.11/site-packages/fastapi/testclient.py:1
  /home/asus/s5verify/repo/.venv/lib/python3.11/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

.venv/lib/python3.11/site-packages/starlette/testclient.py:53
  /home/asus/s5verify/repo/.venv/lib/python3.11/site-packages/starlette/testclient.py:53: DeprecationWarning: The anyio.abc.BlockingPortal alias is deprecated, use anyio.from_thread.BlockingPortal instead.
    _PortalFactoryType = Callable[[], AbstractContextManager[anyio.abc.BlockingPortal]]

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
62 passed, 2 warnings in 0.06s
```

**Run:**（完整门禁）

```bash
bash /home/asus/s5verify/ci.sh
```

**Expected**：`CI_OK`，`ci: 全部通过`。

**`make ci` 原文**（关键行）：

```text
All checks passed!
Success: no issues found in 113 source files
Analyzed 145 files, 579 dependencies.
Contracts: 5 kept, 0 broken.
capability-map: 0 个问题
module-map: 0 个问题
openapi 契约一致
TOTAL                                                                 2844     68    98%
762 passed, 16 skipped, 2 warnings in 112.82s (0:01:52)
Found 0 warnings and 0 errors.
ci: 全部通过
```

**改动文件集**（`git show --stat`）：

```text
 apps/api/src/sentrix/domain/identity/__init__.py |   5 +
 apps/api/src/sentrix/domain/identity/faces.py    | 288 +++++++++++
 apps/api/tests/unit/test_identity_faces.py       | 625 +++++++++++++++++++++++
 docs/architecture/module-map.md                  |   3 +-
 docs/refactor/capability-map.jsonl               |   3 +
 5 files changed, 923 insertions(+), 1 deletion(-)
```

**这一跑同时给出基线的确切条数**：`762 passed − 62`（本任务新增）= **700 passed**；
`mypy` 从 **111** 个文件长到 **113**（`identity/__init__.py` + `identity/faces.py`）；
import-linter 从 143 files / 575 deps 长到 145 / 579。
`16 skipped` 是**本机环境决定的**（缺 GeoNames / Qdrant / 真权重），不是新增的。

**变异检查**：对实现做了 32 处变异，**31 处被测试杀掉**。存活的那一处是
`_best_compatible_cluster` 里把 `continue` 改成 `break`——候选簇已按 `(-score, cluster.id)`
降序排过，第一个低于阈值的后面必然都低于阈值，两者**行为等价**，任何测试都不可能杀掉它。
**如实登记为「未被门禁覆盖的一处」**，不为它硬凑一条测试。

**本任务实测出来的 1.0 行为**已经写进 `docs/refactor/behaviour-baseline.md` 的「人脸」一节
（那一节原先只有三个阈值，现在补齐到十条，见该文件的 2026-09-23 条目）。

---

### Task 70（S5a）· 迁移 0006：identity 表族 ✅ 已落地

**commit:** `b18c571`

**Files:**

```text
apps/api/src/sentrix/infrastructure/persistence/migrations/0006_identity.sql   【新】
apps/api/tests/integration/test_identity_schema.py                             【新】
docs/refactor/frozen.md                                                        【改】
docs/refactor/capability-map.jsonl                                             【改】
```

**移植**（逐段对回 1.0 的位置，全部是实读的）：

| 2.0 的表 | 1.0 的出处 |
|---|---|
| `entities` | `backend/db.py:560-570` + `:1240-1243` 的运行期 ALTER 三列 |
| `entity_revisions` | `backend/db.py:572-580` |
| `entity_properties` | `backend/db.py:582-594` |
| `face_clusters` | `backend/db.py:612-622` |
| `face_instances` | `backend/db.py:624-641` + `:1238` 的 `validity` |
| `face_prototypes` | `backend/db.py:643-654`（`UNIQUE(cluster_id, pose_bucket)`） |
| `person_appearance_evidence` | `backend/db.py:656-669` |
| `entity_mentions` | `backend/db.py:671-678` |
| `entity_observations` | `backend/db.py:680-686` |
| `relationships` | `backend/db.py:688-699` + `:1244-1246` 的 `inverse_predicate` |
| `event_participants` | `backend/db.py:503-511`（0005 明确留给 S5） |
| `event_entities` | `backend/db.py:487-501`（同上） |
| `person_moments` | `backend/db.py:1145-1172`（裁决②解冻；**`run_id` 取消**） |
| `person_portrait_revisions` | `backend/db.py:1195-1219`（裁决②解冻） |
| `observations.people_json` | 1.0 在 `_ensure_columns` 里追加（`db.py:1239`），2.0 的 0005 没建 |

**不建的**：`persons`（遗留，无生产写入者）、`entity_merge_candidates` / `person_role_hypotheses` /
`relationship_hypotheses`（仍冻结）、`family_*`（S7）、
**`person_insight_runs`（裁决②仍冻结——所以 `person_moments.run_id` 这一列随之取消，
它本来是指向那张不存在表的外键）**。

**为什么两张解冻的表建在 0006 而不是 S5c 的 0007**：判据与代价见
`s5-deviations.md` §2c.1 的「②的落点判断」。一句话：0005 的推迟是**被悬空的外键目标逼出来的**，
到 0006 全族的引用都闭合了，拆开只会让 0007 重述一堆跨切片引用。

**核心代码** —— `apps/api/src/sentrix/infrastructure/persistence/migrations/0006_identity.sql`（全文）：

```sql
-- S5 人脸与身份层：实体、人脸簇/实例/原型、提及桥、事件参与者投影。
-- 字段语义取自 1.0 backend/db.py 的 DDL 段，对应关系写在 s5 实施计划的任务表里：
--   entities :560-570（+ :1240-1243 运行期 ALTER 追加的三列）
--   entity_revisions :572-580 / entity_properties :582-594
--   face_clusters :612-622 / face_instances :624-641（+ :1238 的 validity）
--   face_prototypes :643-654 / person_appearance_evidence :656-669
--   entity_mentions :671-678 / entity_observations :680-686
--   relationships :688-699（+ :1244-1246 的 inverse_predicate）
--   event_participants :503-511 / event_entities :487-501
--   person_moments :1145-1172 / person_portrait_revisions :1195-1219（S5c 才写，见下）
--
-- 1.0 是 CREATE TABLE IF NOT EXISTS + 运行期 _ensure_columns 打补丁，2.0 一次建对：
-- entities 的 identity_state/role_state/name_state、face_instances 的 validity、
-- relationships 的 inverse_predicate 三处 ALTER 在 1.0 是事后补的列，这里直接写进建表语句。
-- face_instances.cluster_id 可空是**必须一次建对**的：1.0 为它写过一次整表重建
-- （db.py:1257-1311 _migrate_face_instances_cluster_nullable），因为 SQLite 不能就地
-- 去掉 NOT NULL。低置信度人脸要能作为证据落库而不被塞进某个簇，这个可空性就是那条路径。
--
-- 0005 的注释说「参与者与实体都依赖 S5 的 entities，那两张表随 S5 一起建，不在这里留空壳」——
-- 那一次的推迟是**被依赖逼出来的**（entities 当时不存在）。到 0006，entities 就在本文件里，
-- 全家的外键目标都闭合了，所以整族一次建齐。

CREATE TABLE entities (
    id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL DEFAULT 'home-default',
    entity_type TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    family_role TEXT,
    summary TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0,
    identity_state TEXT NOT NULL DEFAULT 'clustered',
    role_state TEXT NOT NULL DEFAULT 'unknown',
    name_state TEXT NOT NULL DEFAULT 'anonymous',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_entities_scope ON entities(scope_id);
CREATE INDEX idx_entities_name ON entities(scope_id, entity_type, status, canonical_name);

-- 审计表。1.0 的簇审阅四件（confirm / reject / merge / split）与改名都往这里写，
-- 是「每一次身份变更都留痕」这个产品硬约束的落点，所以它属于 S5a 而不是可选项。
CREATE TABLE entity_revisions (
    id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL REFERENCES entities(id),
    field_name TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    source TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE INDEX idx_entity_revisions_entity ON entity_revisions(entity_id, created_at DESC);

-- 实体属性。source='user' 的当前值永不被 derived 覆盖（db.py:4695-4727），
-- derived 写出不同值时落 status='pending' 等确认。
CREATE TABLE entity_properties (
    id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL REFERENCES entities(id),
    property_key TEXT NOT NULL,
    value_json TEXT NOT NULL DEFAULT 'null',
    source TEXT NOT NULL DEFAULT 'derived',
    confidence REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    supersedes_property_id TEXT REFERENCES entity_properties(id),
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_entity_properties_entity_key ON entity_properties(entity_id, property_key, updated_at);
CREATE INDEX idx_entity_properties_status ON entity_properties(status);

CREATE TABLE face_clusters (
    id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL DEFAULT 'home-default',
    status TEXT NOT NULL DEFAULT 'pending',
    entity_id TEXT REFERENCES entities(id),
    representative_embedding_json TEXT NOT NULL DEFAULT '[]',
    member_count INTEGER NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_face_clusters_scope ON face_clusters(scope_id);

CREATE TABLE face_instances (
    id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES assets(id),
    observation_id TEXT NOT NULL REFERENCES observations(id),
    cluster_id TEXT REFERENCES face_clusters(id),
    bbox_json TEXT NOT NULL DEFAULT '[]',
    embedding_json TEXT NOT NULL DEFAULT '[]',
    detection_confidence REAL NOT NULL DEFAULT 0,
    quality REAL NOT NULL DEFAULT 0,
    pose_json TEXT NOT NULL DEFAULT '[]',
    area_ratio REAL NOT NULL DEFAULT 0,
    sharpness REAL NOT NULL DEFAULT 0,
    pose_bucket TEXT NOT NULL DEFAULT 'unknown',
    embedding_model TEXT NOT NULL DEFAULT 'unknown',
    embedding_version TEXT NOT NULL DEFAULT 'unknown',
    embedding_quality_signal REAL NOT NULL DEFAULT 0,
    validity TEXT NOT NULL DEFAULT 'uncertain',
    created_at TEXT NOT NULL
);

CREATE INDEX idx_face_instances_cluster ON face_instances(cluster_id);
CREATE INDEX idx_face_instances_asset ON face_instances(asset_id);
CREATE INDEX idx_face_instances_observation ON face_instances(observation_id);

-- 每个姿态桶一条。UNIQUE 是「同桶只留最好的那一张」在 SQL 层的凭证，
-- 不是可以由写入方自己保证的约定。
CREATE TABLE face_prototypes (
    id TEXT PRIMARY KEY,
    cluster_id TEXT NOT NULL REFERENCES face_clusters(id),
    face_instance_id TEXT NOT NULL REFERENCES face_instances(id),
    pose_bucket TEXT NOT NULL DEFAULT 'unknown',
    embedding_json TEXT NOT NULL DEFAULT '[]',
    quality REAL NOT NULL DEFAULT 0,
    model_name TEXT NOT NULL DEFAULT 'unknown',
    model_version TEXT NOT NULL DEFAULT 'unknown',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(cluster_id, pose_bucket)
);

CREATE INDEX idx_face_prototypes_cluster ON face_prototypes(cluster_id);

CREATE TABLE person_appearance_evidence (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL REFERENCES entities(id),
    face_instance_id TEXT NOT NULL REFERENCES face_instances(id),
    observation_id TEXT NOT NULL REFERENCES observations(id),
    asset_id TEXT NOT NULL REFERENCES assets(id),
    crop_bbox_json TEXT NOT NULL DEFAULT '[]',
    clothing_json TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL DEFAULT 0,
    model_name TEXT NOT NULL DEFAULT 'unknown',
    status TEXT NOT NULL DEFAULT 'confirmed',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(person_id, face_instance_id, model_name)
);

CREATE INDEX idx_person_appearance_person ON person_appearance_evidence(person_id);
CREATE INDEX idx_person_appearance_face ON person_appearance_evidence(face_instance_id);

-- 提及桥。UNIQUE 里的 face_instance_id 可空——SQLite 的 UNIQUE 允许多个 NULL，
-- 所以「没有对应人脸实例的提及」不会互相顶掉，这是有意的。
CREATE TABLE entity_mentions (
    id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL REFERENCES entities(id),
    observation_id TEXT NOT NULL REFERENCES observations(id),
    face_instance_id TEXT REFERENCES face_instances(id),
    confidence REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(entity_id, observation_id, face_instance_id)
);

CREATE INDEX idx_entity_mentions_observation ON entity_mentions(observation_id);
CREATE INDEX idx_entity_mentions_entity ON entity_mentions(entity_id);

CREATE TABLE entity_observations (
    entity_id TEXT NOT NULL REFERENCES entities(id),
    observation_id TEXT NOT NULL REFERENCES observations(id),
    confidence REAL NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'observation_extraction',
    created_at TEXT NOT NULL,
    PRIMARY KEY(entity_id, observation_id)
);

CREATE TABLE relationships (
    id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL DEFAULT 'home-default',
    subject_entity_id TEXT NOT NULL REFERENCES entities(id),
    predicate TEXT NOT NULL,
    object_entity_id TEXT NOT NULL REFERENCES entities(id),
    inverse_predicate TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    confidence REAL NOT NULL DEFAULT 0,
    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    supersedes_relationship_id TEXT REFERENCES relationships(id),
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_relationship_subject_object ON relationships(subject_entity_id, object_entity_id);
CREATE INDEX idx_relationships_scope ON relationships(scope_id);

-- 事件参与者与实体投影（0005 明确留给 S5 的两张）。
-- event_participants.person_id 指向 entities：这就是 0005 当时不能建它的原因。
CREATE TABLE event_participants (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(id),
    person_id TEXT NOT NULL REFERENCES entities(id),
    role TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(event_id, person_id, role)
);

CREATE INDEX idx_event_participants_event ON event_participants(event_id);
CREATE INDEX idx_event_participants_person ON event_participants(person_id);

CREATE TABLE event_entities (
    event_id TEXT NOT NULL REFERENCES events(id),
    entity_id TEXT NOT NULL REFERENCES entities(id),
    relation TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(event_id, entity_id, relation)
);

CREATE INDEX idx_event_entities_event ON event_entities(event_id);
CREATE INDEX idx_event_entities_entity ON event_entities(entity_id);

-- 观察上的「已确认人物」快照。1.0 是 observations.people_json（db.py:1239 的
-- _ensure_columns 追加），2.0 的 0005 没建。事件八分量的 person_score 与
-- person_bridge 词法投影都读它，所以它属于 S5a。
ALTER TABLE observations ADD COLUMN people_json TEXT NOT NULL DEFAULT '[]';

-- 人物瞬间与画像修订：2026-09-23 用户裁决解冻这两张（person_insight_runs 仍冻结，
-- 所以本文件**没有** person_insight_runs，person_moments.run_id 也就不存在——
-- 1.0 的 run_id 是指向那张冻结表的外键，本切片不建那张表，这一列随之取消）。
-- 它们在 S5c（Task 84/85）才有写入者；建在这里的理由与代价写在
-- docs/refactor/s5-deviations.md 的裁决②条目里。
CREATE TABLE person_moments (
    id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL REFERENCES memory_spaces(id),
    person_id TEXT NOT NULL REFERENCES entities(id),
    cluster_id TEXT NOT NULL REFERENCES face_clusters(id),
    event_id TEXT NOT NULL REFERENCES events(id),
    observation_id TEXT NOT NULL REFERENCES observations(id),
    asset_id TEXT NOT NULL REFERENCES assets(id),
    face_instance_id TEXT NOT NULL REFERENCES face_instances(id),
    action_text TEXT NOT NULL DEFAULT '',
    interaction_target_ids_json TEXT NOT NULL DEFAULT '[]',
    interaction_text TEXT NOT NULL DEFAULT '',
    participation_style TEXT NOT NULL DEFAULT '',
    visible_affect TEXT NOT NULL DEFAULT '',
    social_role_cues_json TEXT NOT NULL DEFAULT '[]',
    narrative_note TEXT NOT NULL DEFAULT '',
    selection_json TEXT NOT NULL DEFAULT '{}',
    confidence REAL NOT NULL DEFAULT 0,
    model_name TEXT NOT NULL DEFAULT 'unknown',
    prompt_version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(person_id, observation_id, prompt_version)
);

CREATE INDEX idx_person_moments_person ON person_moments(person_id, status, created_at DESC);
CREATE INDEX idx_person_moments_event ON person_moments(event_id, person_id);

CREATE TABLE person_portrait_revisions (
    id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL REFERENCES memory_spaces(id),
    person_id TEXT NOT NULL REFERENCES entities(id),
    revision INTEGER NOT NULL,
    portrait_text TEXT NOT NULL,
    themes_json TEXT NOT NULL DEFAULT '[]',
    evidence_refs_json TEXT NOT NULL DEFAULT '[]',
    trigger_type TEXT NOT NULL,
    model_name TEXT NOT NULL DEFAULT 'unknown',
    prompt_version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    user_feedback TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(person_id, revision)
);

CREATE INDEX idx_person_portraits_person ON person_portrait_revisions(person_id, revision DESC);

```

**门禁** —— `apps/api/tests/integration/test_identity_schema.py`（全文）：

```python
"""迁移 0006 的 schema：身份表族 + 观察的人物快照列。

对齐 ``test_timeline_schema.py`` 的理由：``test_migrations.py`` 验的是迁移**执行器**
（应用一次、失败回滚、没有 .sql 时报错），它在 0006 写出来之前就是绿的，验不了 0006
建了什么。这里断言的是「建出来的东西真的能用」——三条 UNIQUE 约束、一处可空性、
一列在 1.0 需要整表重建才拿到的可空外键。

``test_face_instances_cluster_id_is_nullable`` 是这套里最贵的一条：1.0 为把
``cluster_id`` 从 NOT NULL 改成可空，写了一整段 ``CREATE TABLE _migrated`` /
``INSERT SELECT`` / ``DROP`` / ``RENAME``（``db.py:1257-1311``），因为 SQLite 不能
就地摘掉 NOT NULL。2.0 的迁移是编号的、一次建对，这条断言把「一次建对」钉住——
写成 NOT NULL 时它立刻红，而不是等到真有一张低置信度人脸要落库才炸。
"""

from __future__ import annotations

import sqlite3

import pytest

from sentrix.infrastructure.persistence.database import Database

EXPECTED_TABLES = {
    "entities",
    "entity_revisions",
    "entity_properties",
    "face_clusters",
    "face_instances",
    "face_prototypes",
    "person_appearance_evidence",
    "entity_mentions",
    "entity_observations",
    "relationships",
    "event_participants",
    "event_entities",
}

# 2026-09-23 用户裁决解的冻（person_moments / person_portrait_revisions）与仍冻的
# person_insight_runs 都在这条断言里：三张表的去留是一个决定，不能只断言去的那半。
UNFROZEN_BY_RULING = {"person_moments", "person_portrait_revisions"}
STILL_FROZEN = {"person_insight_runs"}


@pytest.fixture
def identity_db(migrations_ready_db: Database):
    """迁移就绪的库 + 一行最小可引用的父数据（assets / observations / events）。

    外键是打开的（``Database.connect`` 每次都 ``PRAGMA foreign_keys = ON``），
    所以 UNIQUE 与 FK 两类断言都必须有真父行才谈得上「约束在生效」。
    """
    with migrations_ready_db.connect() as connection, connection:
        connection.execute(
            "INSERT INTO assets (id, scope_id, file_name, media_type, path, created_at, updated_at)"
            " VALUES ('asset_1', 'album1', 'a.jpg', 'image', '/tmp/a.jpg', '2026-01-01', '2026-01-01')"
        )
        connection.execute(
            "INSERT INTO observations (id, scope_id, asset_id, source_type, created_at, updated_at)"
            " VALUES ('obs_1', 'album1', 'asset_1', 'vlm', '2026-01-01', '2026-01-01')"
        )
        connection.execute(
            "INSERT INTO events (id, scope_id, title, created_at, updated_at)"
            " VALUES ('evt_1', 'album1', '事件', '2026-01-01', '2026-01-01')"
        )
        connection.execute(
            "INSERT INTO memory_spaces (id, name, created_at, updated_at)"
            " VALUES ('album1', '测试空间', '2026-01-01', '2026-01-01')"
        )
    return migrations_ready_db


def _insert_entity(connection: sqlite3.Connection, entity_id: str, name: str) -> None:
    connection.execute(
        "INSERT INTO entities (id, scope_id, entity_type, canonical_name, status, created_at, updated_at)"
        " VALUES (?, 'album1', 'person', ?, 'pending', '2026-01-01', '2026-01-01')",
        (entity_id, name),
    )


def _insert_cluster(connection: sqlite3.Connection, cluster_id: str) -> None:
    connection.execute(
        "INSERT INTO face_clusters (id, scope_id, status, created_at, updated_at)"
        " VALUES (?, 'album1', 'pending', '2026-01-01', '2026-01-01')",
        (cluster_id,),
    )


def _insert_face_instance(connection: sqlite3.Connection, instance_id: str, cluster_id: str | None) -> None:
    connection.execute(
        "INSERT INTO face_instances"
        " (id, asset_id, observation_id, cluster_id, created_at)"
        " VALUES (?, 'asset_1', 'obs_1', ?, '2026-01-01')",
        (instance_id, cluster_id),
    )


def test_0006_creates_the_identity_tables(identity_db: Database):
    with identity_db.connect() as connection:
        names = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master")}

    assert names >= EXPECTED_TABLES


def test_portrait_tables_follow_the_ruling(identity_db: Database):
    """解冻的两张要建，冻结的那张不许建。

    只断言「解冻的两张在」会让「顺手把 person_insight_runs 也建了」静默通过——
    那等于绕开裁决把那张表的冻结撤了，而 frozen.md 还写着它冻结。
    """
    with identity_db.connect() as connection:
        names = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master")}

    assert names >= UNFROZEN_BY_RULING
    assert not (names & STILL_FROZEN)


def test_face_instances_cluster_id_is_nullable(identity_db: Database):
    """1.0 为这一列写过一次整表重建；2.0 必须一次建对。

    可空不是为了好看：低置信度人脸要能作为**证据**落库而不被塞进某个簇。
    """
    with identity_db.connect() as connection:
        columns = {row["name"]: row for row in connection.execute("PRAGMA table_info(face_instances)")}

    assert columns["cluster_id"]["notnull"] == 0


def test_low_confidence_face_lands_without_a_cluster(identity_db: Database):
    """承上条：可空性要在真写入里成立，而不只是 PRAGMA 里那个 0。"""
    with identity_db.connect() as connection, connection:
        _insert_face_instance(connection, "inst_orphan", None)
        stored = connection.execute(
            "SELECT cluster_id FROM face_instances WHERE id = 'inst_orphan'"
        ).fetchone()

    assert stored["cluster_id"] is None


def test_face_prototypes_reject_a_second_row_for_the_same_pose_bucket(identity_db: Database):
    """UNIQUE(cluster_id, pose_bucket) 是「同桶只留最好那张」在 SQL 层的凭证。

    只写在写入方的字典逻辑里，别的写入路径绕过去就没人拦——所以这条直接撞约束。
    """
    with identity_db.connect() as connection, connection:
        _insert_cluster(connection, "clu_1")
        _insert_face_instance(connection, "inst_1", "clu_1")
        _insert_face_instance(connection, "inst_2", "clu_1")
        connection.execute(
            "INSERT INTO face_prototypes"
            " (id, cluster_id, face_instance_id, pose_bucket, created_at, updated_at)"
            " VALUES ('pro_1', 'clu_1', 'inst_1', 'frontal', '2026-01-01', '2026-01-01')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO face_prototypes"
                " (id, cluster_id, face_instance_id, pose_bucket, created_at, updated_at)"
                " VALUES ('pro_2', 'clu_1', 'inst_2', 'frontal', '2026-01-01', '2026-01-01')"
            )


def test_entity_mentions_reject_a_duplicate_but_allow_many_null_face_instances(identity_db: Database):
    """UNIQUE 里的 face_instance_id 可空，而 SQLite 的 UNIQUE 允许多个 NULL。

    「同一条观察里同一个实体被提了两次」要被拦，但「同一实体在同一条观察里有多条
    没有对应人脸实例的提及」不能被拦——1.0 的 DDL 就是这个语义，照搬要照搬到这里。
    """
    with identity_db.connect() as connection, connection:
        _insert_entity(connection, "ent_1", "妈妈")
        _insert_face_instance(connection, "inst_1", None)
        connection.execute(
            "INSERT INTO entity_mentions (id, entity_id, observation_id, face_instance_id, created_at)"
            " VALUES ('men_1', 'ent_1', 'obs_1', 'inst_1', '2026-01-01')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO entity_mentions (id, entity_id, observation_id, face_instance_id, created_at)"
                " VALUES ('men_2', 'ent_1', 'obs_1', 'inst_1', '2026-01-01')"
            )
        connection.execute(
            "INSERT INTO entity_mentions (id, entity_id, observation_id, face_instance_id, created_at)"
            " VALUES ('men_3', 'ent_1', 'obs_1', NULL, '2026-01-01')"
        )
        connection.execute(
            "INSERT INTO entity_mentions (id, entity_id, observation_id, face_instance_id, created_at)"
            " VALUES ('men_4', 'ent_1', 'obs_1', NULL, '2026-01-01')"
        )
        rows = connection.execute(
            "SELECT count(*) AS n FROM entity_mentions WHERE entity_id = 'ent_1'"
        ).fetchone()

    assert rows["n"] == 3


def test_person_appearance_evidence_is_unique_per_person_face_and_model(identity_db: Database):
    with identity_db.connect() as connection, connection:
        _insert_entity(connection, "ent_1", "妈妈")
        _insert_face_instance(connection, "inst_1", None)
        connection.execute(
            "INSERT INTO person_appearance_evidence"
            " (id, person_id, face_instance_id, observation_id, asset_id, model_name, created_at, updated_at)"
            " VALUES ('app_1', 'ent_1', 'inst_1', 'obs_1', 'asset_1', 'vlm-a', '2026-01-01', '2026-01-01')"
        )
        # 换一个模型名是合法的：同一张脸可以被两个模型各描述一次。
        connection.execute(
            "INSERT INTO person_appearance_evidence"
            " (id, person_id, face_instance_id, observation_id, asset_id, model_name, created_at, updated_at)"
            " VALUES ('app_2', 'ent_1', 'inst_1', 'obs_1', 'asset_1', 'vlm-b', '2026-01-01', '2026-01-01')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO person_appearance_evidence"
                " (id, person_id, face_instance_id, observation_id, asset_id,"
                "  model_name, created_at, updated_at)"
                " VALUES ('app_3', 'ent_1', 'inst_1', 'obs_1', 'asset_1',"
                " 'vlm-a', '2026-01-01', '2026-01-01')"
            )


def test_observations_gained_people_json(identity_db: Database):
    with identity_db.connect() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(observations)")}

    assert "people_json" in columns


def test_entities_carries_the_three_state_columns_inline(identity_db: Database):
    """1.0 的三列是运行期 ALTER 补的（``db.py:1240-1243``），2.0 一次建对。

    列的**默认值**也要钉：``name_state`` 的 ``anonymous`` 与 ``identity_state`` 的
    ``clustered`` 是簇审阅状态机的初态，写错不会报错，只会让「待命名」判定失效。
    """
    with identity_db.connect() as connection, connection:
        _insert_entity(connection, "ent_1", "妈妈")
        row = connection.execute(
            "SELECT identity_state, role_state, name_state FROM entities WHERE id = 'ent_1'"
        ).fetchone()

    assert (row["identity_state"], row["role_state"], row["name_state"]) == (
        "clustered",
        "unknown",
        "anonymous",
    )


def test_event_participants_references_entities(identity_db: Database):
    """0005 不建这张表的理由就是它 REFERENCES entities；这条钉住那条依赖真的闭合了。"""
    with (
        identity_db.connect() as connection,
        connection,
        pytest.raises(sqlite3.IntegrityError),
    ):
        connection.execute(
            "INSERT INTO event_participants"
            " (id, event_id, person_id, role, created_at, updated_at)"
            " VALUES ('par_1', 'evt_1', 'ent_missing', 'visible_subject', '2026-01-01', '2026-01-01')"
        )

```

**为什么不能只验迁移执行器**：`tests/integration/test_migrations.py` 验的是迁移**执行器**
（应用一次、失败回滚、一条 `.sql` 都没有时报错），它在 `0006` 写出来之前就是绿的——
拿它当 0006 的门禁是一条**恒真门禁**。这条纪律是 S4 的 `test_timeline_schema.py`
开头那段注释立的，本任务照办。

**Run:**（仓库根，隔离副本 `/home/asus/s5verify/repo`）

```bash
# 红灯：只落测试，不落迁移
.venv/bin/pytest apps/api/tests/integration/test_identity_schema.py -q
```

**Expected（红灯）**：10 条全部失败，且失败信息指向**具体的表或列**（不是收集错误）。

**红灯原文**：

```text
FFFFFFFFFF                                                               [100%]
=================================== FAILURES ===================================
____________________ test_0006_creates_the_identity_tables _____________________

identity_db = <sentrix.infrastructure.persistence.database.Database object at 0x7cb4440f30d0>

    def test_0006_creates_the_identity_tables(identity_db: Database):
        with identity_db.connect() as connection:
            names = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master")}
    
>       assert names >= EXPECTED_TABLES
E       AssertionError: assert {'agent_conve..._places', ...} >= {'entities', ...ntities', ...}
E         
E         Extra items in the right set:
E         'entity_revisions'
E         'person_appearance_evidence'
E         'event_participants'
E         'face_instances'
E         'entity_mentions'...
E         
E         ...Full output truncated (7 lines hidden), use '-vv' to show

apps/api/tests/integration/test_identity_schema.py:100: AssertionError
____________________ test_portrait_tables_follow_the_ruling ____________________

identity_db = <sentrix.infrastructure.persistence.database.Database object at 0x7cb4440e6d50>

    def test_portrait_tables_follow_the_ruling(identity_db: Database):
        """解冻的两张要建，冻结的那张不许建。
    
        只断言「解冻的两张在」会让「顺手把 person_insight_runs 也建了」静默通过——
        那等于绕开裁决把那张表的冻结撤了，而 frozen.md 还写着它冻结。
        """
        with identity_db.connect() as connection:
            names = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master")}
    
>       assert names >= UNFROZEN_BY_RULING
E       AssertionError: assert {'agent_conve..._places', ...} >= {'person_mome...it_revisions'}
E         
E         Extra items in the right set:
E         'person_portrait_revisions'
E         'person_moments'

apps/api/tests/integration/test_identity_schema.py:112: AssertionError
__________________ test_face_instances_cluster_id_is_nullable __________________

identity_db = <sentrix.infrastructure.persistence.database.Database object at 0x7cb443f50f50>

    def test_face_instances_cluster_id_is_nullable(identity_db: Database):
        """1.0 为这一列写过一次整表重建；2.0 必须一次建对。
    
        可空不是为了好看：低置信度人脸要能作为**证据**落库而不被塞进某个簇。
        """
        with identity_db.connect() as connection:
            columns = {row["name"]: row for row in connection.execute("PRAGMA table_info(face_instances)")}
    
>       assert columns["cluster_id"]["notnull"] == 0
               ^^^^^^^^^^^^^^^^^^^^^
E       KeyError: 'cluster_id'

apps/api/tests/integration/test_identity_schema.py:124: KeyError
_______________ test_low_confidence_face_lands_without_a_cluster _______________

identity_db = <sentrix.infrastructure.persistence.database.Database object at 0x7cb443f24dd0>

    def test_low_confidence_face_lands_without_a_cluster(identity_db: Database):
        """承上条：可空性要在真写入里成立，而不只是 PRAGMA 里那个 0。"""
        with identity_db.connect() as connection, connection:
>           _insert_face_instance(connection, "inst_orphan", None)

apps/api/tests/integration/test_identity_schema.py:130: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 

connection = <sqlite3.Connection object at 0x7cb4440a25c0>
instance_id = 'inst_orphan', cluster_id = None

    def _insert_face_instance(connection: sqlite3.Connection, instance_id: str, cluster_id: str | None) -> None:
>       connection.execute(
            "INSERT INTO face_instances"
            " (id, asset_id, observation_id, cluster_id, created_at)"
            " VALUES (?, 'asset_1', 'obs_1', ?, '2026-01-01')",
            (instance_id, cluster_id),
        )
E       sqlite3.OperationalError: no such table: face_instances

apps/api/tests/integration/test_identity_schema.py:88: OperationalError
______ test_face_prototypes_reject_a_second_row_for_the_same_pose_bucket _______

identity_db = <sentrix.infrastructure.persistence.database.Database object at 0x7cb4440f30d0>

    def test_face_prototypes_reject_a_second_row_for_the_same_pose_bucket(identity_db: Database):
        """UNIQUE(cluster_id, pose_bucket) 是「同桶只留最好那张」在 SQL 层的凭证。
    
        只写在写入方的字典逻辑里，别的写入路径绕过去就没人拦——所以这条直接撞约束。
        """
        with identity_db.connect() as connection, connection:
>           _insert_cluster(connection, "clu_1")

apps/api/tests/integration/test_identity_schema.py:144: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 

connection = <sqlite3.Connection object at 0x7cb4440a31f0>, cluster_id = 'clu_1'

    def _insert_cluster(connection: sqlite3.Connection, cluster_id: str) -> None:
>       connection.execute(
            "INSERT INTO face_clusters (id, scope_id, status, created_at, updated_at)"
            " VALUES (?, 'album1', 'pending', '2026-01-01', '2026-01-01')",
            (cluster_id,),
        )
E       sqlite3.OperationalError: no such table: face_clusters

apps/api/tests/integration/test_identity_schema.py:80: OperationalError
__ test_entity_mentions_reject_a_duplicate_but_allow_many_null_face_instances __

identity_db = <sentrix.infrastructure.persistence.database.Database object at 0x7cb443f52050>

    def test_entity_mentions_reject_a_duplicate_but_allow_many_null_face_instances(identity_db: Database):
        """UNIQUE 里的 face_instance_id 可空，而 SQLite 的 UNIQUE 允许多个 NULL。
    
        「同一条观察里同一个实体被提了两次」要被拦，但「同一实体在同一条观察里有多条
        没有对应人脸实例的提及」不能被拦——1.0 的 DDL 就是这个语义，照搬要照搬到这里。
        """
        with identity_db.connect() as connection, connection:
>           _insert_entity(connection, "ent_1", "妈妈")

apps/api/tests/integration/test_identity_schema.py:167: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 

connection = <sqlite3.Connection object at 0x7cb4440a3880>, entity_id = 'ent_1'
name = '妈妈'

    def _insert_entity(connection: sqlite3.Connection, entity_id: str, name: str) -> None:
>       connection.execute(
            "INSERT INTO entities (id, scope_id, entity_type, canonical_name, status, created_at, updated_at)"
            " VALUES (?, 'album1', 'person', ?, 'pending', '2026-01-01', '2026-01-01')",
            (entity_id, name),
        )
E       sqlite3.OperationalError: no such table: entities

apps/api/tests/integration/test_identity_schema.py:72: OperationalError
_____ test_person_appearance_evidence_is_unique_per_person_face_and_model ______

identity_db = <sentrix.infrastructure.persistence.database.Database object at 0x7cb4440f09d0>

    def test_person_appearance_evidence_is_unique_per_person_face_and_model(identity_db: Database):
        with identity_db.connect() as connection, connection:
>           _insert_entity(connection, "ent_1", "妈妈")

apps/api/tests/integration/test_identity_schema.py:195: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 

connection = <sqlite3.Connection object at 0x7cb4440a3d30>, entity_id = 'ent_1'
name = '妈妈'

    def _insert_entity(connection: sqlite3.Connection, entity_id: str, name: str) -> None:
>       connection.execute(
            "INSERT INTO entities (id, scope_id, entity_type, canonical_name, status, created_at, updated_at)"
            " VALUES (?, 'album1', 'person', ?, 'pending', '2026-01-01', '2026-01-01')",
            (entity_id, name),
        )
E       sqlite3.OperationalError: no such table: entities

apps/api/tests/integration/test_identity_schema.py:72: OperationalError
_____________________ test_observations_gained_people_json _____________________

identity_db = <sentrix.infrastructure.persistence.database.Database object at 0x7cb443f59a10>

    def test_observations_gained_people_json(identity_db: Database):
        with identity_db.connect() as connection:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(observations)")}
    
>       assert "people_json" in columns
E       AssertionError: assert 'people_json' in {'activity', 'asset_id', 'caption', 'captured_at', 'clothing_json', 'confidence', ...}

apps/api/tests/integration/test_identity_schema.py:222: AssertionError
_____________ test_entities_carries_the_three_state_columns_inline _____________

identity_db = <sentrix.infrastructure.persistence.database.Database object at 0x7cb4440fbe10>

    def test_entities_carries_the_three_state_columns_inline(identity_db: Database):
        """1.0 的三列是运行期 ALTER 补的（``db.py:1240-1243``），2.0 一次建对。
    
        列的**默认值**也要钉：``name_state`` 的 ``anonymous`` 与 ``identity_state`` 的
        ``clustered`` 是簇审阅状态机的初态，写错不会报错，只会让「待命名」判定失效。
        """
        with identity_db.connect() as connection, connection:
>           _insert_entity(connection, "ent_1", "妈妈")

apps/api/tests/integration/test_identity_schema.py:232: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 

connection = <sqlite3.Connection object at 0x7cb443f546d0>, entity_id = 'ent_1'
name = '妈妈'

    def _insert_entity(connection: sqlite3.Connection, entity_id: str, name: str) -> None:
>       connection.execute(
            "INSERT INTO entities (id, scope_id, entity_type, canonical_name, status, created_at, updated_at)"
            " VALUES (?, 'album1', 'person', ?, 'pending', '2026-01-01', '2026-01-01')",
            (entity_id, name),
        )
E       sqlite3.OperationalError: no such table: entities

apps/api/tests/integration/test_identity_schema.py:72: OperationalError
_________________ test_event_participants_references_entities __________________

identity_db = <sentrix.infrastructure.persistence.database.Database object at 0x7cb443f5ba10>

    def test_event_participants_references_entities(identity_db: Database):
        """0005 不建这张表的理由就是它 REFERENCES entities；这条钉住那条依赖真的闭合了。"""
        with (
            identity_db.connect() as connection,
            connection,
            pytest.raises(sqlite3.IntegrityError),
        ):
>           connection.execute(
                "INSERT INTO event_participants"
                " (id, event_id, person_id, role, created_at, updated_at)"
                " VALUES ('par_1', 'evt_1', 'ent_missing', 'visible_subject', '2026-01-01', '2026-01-01')"
            )
E           sqlite3.OperationalError: no such table: event_participants

apps/api/tests/integration/test_identity_schema.py:251: OperationalError
=============================== warnings summary ===============================
.venv/lib/python3.11/site-packages/fastapi/testclient.py:1
  /home/asus/s5verify/repo/.venv/lib/python3.11/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

.venv/lib/python3.11/site-packages/starlette/testclient.py:53
  /home/asus/s5verify/repo/.venv/lib/python3.11/site-packages/starlette/testclient.py:53: DeprecationWarning: The anyio.abc.BlockingPortal alias is deprecated, use anyio.from_thread.BlockingPortal instead.
    _PortalFactoryType = Callable[[], AbstractContextManager[anyio.abc.BlockingPortal]]

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ============================
FAILED apps/api/tests/integration/test_identity_schema.py::test_0006_creates_the_identity_tables
FAILED apps/api/tests/integration/test_identity_schema.py::test_portrait_tables_follow_the_ruling
FAILED apps/api/tests/integration/test_identity_schema.py::test_face_instances_cluster_id_is_nullable
FAILED apps/api/tests/integration/test_identity_schema.py::test_low_confidence_face_lands_without_a_cluster
FAILED apps/api/tests/integration/test_identity_schema.py::test_face_prototypes_reject_a_second_row_for_the_same_pose_bucket
FAILED apps/api/tests/integration/test_identity_schema.py::test_entity_mentions_reject_a_duplicate_but_allow_many_null_face_instances
FAILED apps/api/tests/integration/test_identity_schema.py::test_person_appearance_evidence_is_unique_per_person_face_and_model
FAILED apps/api/tests/integration/test_identity_schema.py::test_observations_gained_people_json
FAILED apps/api/tests/integration/test_identity_schema.py::test_entities_carries_the_three_state_columns_inline
FAILED apps/api/tests/integration/test_identity_schema.py::test_event_participants_references_entities
```

**Run:**（绿灯）

```bash
.venv/bin/pytest apps/api/tests/integration/test_identity_schema.py -q
```

**Expected（绿灯）**：10 passed。

**绿灯原文**：

```text
..........                                                               [100%]
=============================== warnings summary ===============================
.venv/lib/python3.11/site-packages/fastapi/testclient.py:1
  /home/asus/s5verify/repo/.venv/lib/python3.11/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

.venv/lib/python3.11/site-packages/starlette/testclient.py:53
  /home/asus/s5verify/repo/.venv/lib/python3.11/site-packages/starlette/testclient.py:53: DeprecationWarning: The anyio.abc.BlockingPortal alias is deprecated, use anyio.from_thread.BlockingPortal instead.
    _PortalFactoryType = Callable[[], AbstractContextManager[anyio.abc.BlockingPortal]]

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
```

**Run:**（完整门禁，用判据脚本而不是裸 `make ci`——理由见「顾虑①的处置」）

```bash
bash /home/asus/s5verify/ci.sh
```

**Expected**：`CI_OK`，且 `make ci` 输出末尾是 `ci: 全部通过`。

**`make ci` 原文**（关键行）：

```text
All checks passed!
Success: no issues found in 113 source files
Analyzed 145 files, 579 dependencies.
Contracts: 5 kept, 0 broken.
capability-map: 0 个问题
module-map: 0 个问题
openapi 契约一致
TOTAL                                                                 2844     68    98%
772 passed, 16 skipped, 2 warnings in 123.66s (0:02:03)
Found 0 warnings and 0 errors.
✓ built in 71ms
ci: 全部通过
```

**改动文件集**（`git show --stat`）：

```text
 .../persistence/migrations/0006_identity.sql       | 285 +++++++++++++++++++++
 apps/api/tests/integration/test_identity_schema.py | 255 ++++++++++++++++++
 docs/refactor/capability-map.jsonl                 |   4 +-
 docs/refactor/frozen.md                            |  10 +-
 4 files changed, 551 insertions(+), 3 deletions(-)
```

**零成本算术不变量**：本任务门禁 10 条，上一任务（Task 69）门禁 62 条。
`772 passed − 762 passed = 10` ✔（差值与本任务新增的绿灯条数相等）。

**这一跑同时给出的其它基线量**：`mypy` 仍是 **113** 个源文件（本任务只加 `.sql` 与测试，
测试不在 mypy 的 `files` 里）；import-linter 仍是 145 files / 579 deps；
`TOTAL 2844 68 98%` 与 Task 69 那一跑逐字相同（本任务没有新增任何被 coverage 统计的
`sentrix.domain` / `sentrix.application` 代码）。

---

### Task 71（S5a）· 身份仓储与端口 ✅ 已落地

**commit:** `67030f0`

**Files:**

```text
apps/api/src/sentrix/domain/identity/models.py                              【新】
apps/api/src/sentrix/domain/identity/ports.py                               【新】
apps/api/src/sentrix/infrastructure/persistence/repositories/faces.py       【新】
apps/api/src/sentrix/infrastructure/persistence/repositories/entities.py    【新】
apps/api/src/sentrix/bootstrap/container.py                                 【改】+2 个端口字段
apps/api/tests/integration/test_identity_repositories.py                    【新】门禁 19 条
docs/architecture/module-map.md                                             【改】+4 行
docs/refactor/capability-map.jsonl                                          【改】+15 条
```

**移植**（逐个符号对回 1.0，全是实读的）：

| 2.0 的符号 | 1.0 的出处 |
|---|---|
| `list_clusters` | `db.py:5133-5151` `list_face_clusters` |
| `get_instance` | `db.py:4498-4534` `get_face_instance` |
| `list_prototypes` / `refresh_prototypes` | `db.py:4535-4560` `list_face_prototypes` / `:4922-4955` `_refresh_face_prototypes` |
| `find_confirmed_person_by_name` | `db.py:4407-4435` |
| `person_aliases` / `set_person_aliases` | `db.py:4399-4435` |
| `rename_person` | `db.py:4436-4466` |
| `record_revision` | `db.py:4644-4665` `_record_entity_revision` |
| `list_properties` / `maintain_property` / `set_property` | `db.py:4666-4750` 的实体属性层 |
| `link_confirmed_mention` | `db.py:4912-4921` `_link_confirmed_entity_mention` + `:5435` `_add_entity_to_observation` |
| `event_ids` | `db.py:5402-5430` `entity_event_ids` |

**不移植的**：`record_person_appearance_evidence` / `list_person_appearance_evidence`（调用者在
Task 85 / S5c）、`list_relationships` / `get_person_evidence`（调用者在 Task 86 / S5c）、
`maintain_entity_property_values`（多值合并；1.0 的调用点全在语义实体抽取
`db.py:3840-4032`，属 S7）。**这四条如果现在写进来就是「零调用的能力」**——
S4 为此栽过两次，`scripts/check_reachability.py` 现在会直接把它们标红。

**核心代码** —— `apps/api/src/sentrix/domain/identity/ports.py:32-58`（节选）：

```python


class FaceRepository(Protocol):
    """人脸簇 / 实例 / 原型。

    ``list_clusters`` 强制 ``memory_spaces.include_in_people = 1``——这是端口的契约，
    不是某个调用方的过滤：不给这一条，任何人写一次 ``/api/people`` 都会把评测空间
    聚出来的人脸当家庭成员展示。
    """

    def list_clusters(self, *, status: str | None = None) -> Sequence[FaceClusterRecord]:
        """``status`` 为空时返回除 ``rejected`` 外的全部簇，且只含进人物视图的空间。"""
        ...

    def list_candidates(self, scope_id: str) -> Sequence[FaceClusterRecord]:
        """在线匹配的候选：同 scope、``status IN ('pending','confirmed')``。

        与 ``list_clusters`` 有两处**故意**不同，两处都照抄 1.0 ``db.py:4867``：
        按 scope 过滤（匹配是空间内的事），且**不过滤** ``include_in_people``
        （评测空间也要聚类，只是它的簇不进人物视图）。
        """
        ...

    def get_cluster(self, cluster_id: str) -> FaceClusterRecord | None:
        """簇审阅的入口：四个动作都先按 id 找它，找不到就是 404。"""
        ...

```

`FaceRepository` 是簇 / 实例 / 原型三个读写的端口。两个新字段按端口类型声明，
`test_container_wiring.py` 的例外表**没有增加**——这一点本身就是验收的一部分。

**核心代码** —— `apps/api/src/sentrix/infrastructure/persistence/repositories/faces.py:159-205`（节选）：

```python
                " LEFT JOIN entities e ON e.id = fc.entity_id WHERE fc.id = ?",
                (cluster_id,),
            ).fetchone()
        return _row_to_cluster(row) if row else None

    def create_cluster(
        self, embedding: Sequence[float], *, confidence: float, scope_id: str
    ) -> FaceClusterRecord:
        cluster_id = _new_id("cluster")
        now = datetime.now(UTC).isoformat()
        with self._database.connect() as connection, connection:
            connection.execute(
                "INSERT INTO face_clusters"
                " (id, scope_id, representative_embedding_json, member_count, confidence,"
                "  created_at, updated_at)"
                " VALUES (?, ?, ?, 0, ?, ?, ?)",
                (
                    cluster_id,
                    scope_id or DEFAULT_SCOPE_ID,
                    json.dumps([float(value) for value in embedding]),
                    float(confidence or 0),
                    now,
                    now,
                ),
            )
        cluster = self.get_cluster(cluster_id)
        assert cluster is not None
        return cluster

    def link_entity(self, cluster_id: str, entity_id: str) -> FaceClusterRecord:
        now = datetime.now(UTC).isoformat()
        with self._database.connect() as connection, connection:
            connection.execute(
                "UPDATE face_clusters SET entity_id = ?, updated_at = ? WHERE id = ?",
                (entity_id, now, cluster_id),
            )
        cluster = self.get_cluster(cluster_id)
        assert cluster is not None
        return cluster

    def confirm_cluster(self, cluster_id: str, entity_id: str) -> FaceClusterRecord:
        now = datetime.now(UTC).isoformat()
        with self._database.connect() as connection, connection:
            connection.execute(
                "UPDATE face_clusters SET status = ?, entity_id = ?, updated_at = ?,"
                " revision = revision + 1 WHERE id = ?",
                (CLUSTER_STATUS_CONFIRMED, entity_id, now, cluster_id),
```

这一段是 1.0 `_refresh_face_prototypes` 的逐行对应——**包括那条容易被漏掉的
`confidence = MAX(confidence, ?)` 与 `revision = revision + 1`**。它们不是可有可无的装饰：
漏掉 `revision+1`，簇的修订号就不再反映原型变化，而「每个受影响事件 `revision+1`」
在下游（Task 81）是要被依赖的。

**核心代码** —— `apps/api/src/sentrix/infrastructure/persistence/repositories/entities.py:483-520`（节选）：

```python
                status,
                _encode(list(evidence)),
                supersedes,
                revision,
                now,
                now,
            ),
        )
        row = connection.execute(
            f"SELECT {_PROPERTY_COLUMNS} FROM entity_properties WHERE id = ?", (property_id,)
        ).fetchone()
        assert row is not None
        return _row_to_property(row)

    @staticmethod
    def _property_by_id(connection: sqlite3.Connection, property_id: str) -> EntityPropertyRecord:
        row = connection.execute(
            f"SELECT {_PROPERTY_COLUMNS} FROM entity_properties WHERE id = ?", (property_id,)
        ).fetchone()
        assert row is not None
        return _row_to_property(row)

    @staticmethod
    def _current_property(
        connection: sqlite3.Connection, entity_id: str, property_key: str
    ) -> sqlite3.Row | None:
        """当前生效的一行：``active`` 优先于 ``pending``，再按 ``revision`` 降序。

        ``active`` 优先是这条 SQL 的全部要点：派生写出的新值转 ``pending`` 之后，
        「当前值」仍然是那条已确认的旧值，直到有人确认新值为止。
        """
        row = connection.execute(
            f"SELECT {_PROPERTY_COLUMNS} FROM entity_properties"
            " WHERE entity_id = ? AND property_key = ? AND status IN ('active', 'pending')"
            " ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, revision DESC,"
            " updated_at DESC LIMIT 1",
            (entity_id, property_key),
        ).fetchone()
```

`link_confirmed_mention`。**这是本任务唯一一处偏离 1.0 结构的地方**：1.0 把
「只有 confirmed 的实体才写 mention」这条规则放在**调用点**
（`if entity.get("status") == "confirmed"`），2.0 把它下沉进仓储并改成抛 `ValueError`。
理由写在代码注释里：这条规则一旦漏判，观察的人物快照里就会混进一个聚类猜出来的候选人，
而它是 `person_score` 与 `person_bridge` 的输入——**一处漏判，后面全是事实**。
1.0 的调用点从不违反它，所以生产路径上行为等价；变化的是漏判的代价从「静默污染」
变成「一眼可见」。已登记在 `s5-deviations.md`。

**必须钉住的行为**（19 条门禁逐条对应）：

| 组 | 条数 | 钉住的东西 |
|---|---|---|
| 簇列表 | 4 | 强制 `include_in_people = 1`（`include_in_people = 0` 的空间查不到）；`pending` 簇对外名字是 `待命名成员`；`list_clusters` 沿用 1.0 的 INNER JOIN `memory_spaces`（scope 没有空间行时查不到任何簇） |
| 原型刷新 | 4 | 每 `pose_bucket` 一条、`quality DESC, created_at ASC` 取首；`member_count = 该簇实例总数`；`confidence = MAX(confidence, ?)`；`revision+1`；先删后插（重复调用幂等） |
| 实体属性 | 5 | `source='user'` 的当前值**永不被 derived 覆盖**；derived 写出不同值 → 落 `status='pending'`；`list_properties` 默认只给 `active`/`pending`，历史要显式要 |
| 改名与别名 | 3 | 改名后**旧名进 aliases**；`find_confirmed_person_by_name` 先精确名、再遍历 aliases；**同名只在同 scope 内**匹配 |
| 修订 | 2 | 每次 `record_revision` 落一行 `entity_revisions`（字段名 / 旧值 / 新值 / source） |
| 提及 | 1 | 非 confirmed 实体写 mention → `ValueError` |

**Run:**（仓库根，隔离副本 `/home/asus/s5verify/repo`）

```bash
# 红灯 A：只落测试，不落实现
.venv/bin/pytest apps/api/tests/integration/test_identity_repositories.py -q
```

**红灯 A 原文**：

```text

==================================== ERRORS ====================================
__ ERROR collecting apps/api/tests/integration/test_identity_repositories.py ___
ImportError while importing test module '/home/asus/s5verify/repo/apps/api/tests/integration/test_identity_repositories.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
../../.local/share/uv/python/cpython-3.11.16-linux-x86_64-gnu/lib/python3.11/importlib/__init__.py:126: in import_module
    return _bootstrap._gcd_import(name[level:], package, level)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
apps/api/tests/integration/test_identity_repositories.py:26: in <module>
    from sentrix.domain.identity.models import PENDING_CLUSTER_NAME
E   ModuleNotFoundError: No module named 'sentrix.domain.identity.models'
=============================== warnings summary ===============================
.venv/lib/python3.11/site-packages/fastapi/testclient.py:1
  /home/asus/s5verify/repo/.venv/lib/python3.11/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

.venv/lib/python3.11/site-packages/starlette/testclient.py:53
  /home/asus/s5verify/repo/.venv/lib/python3.11/site-packages/starlette/testclient.py:53: DeprecationWarning: The anyio.abc.BlockingPortal alias is deprecated, use anyio.from_thread.BlockingPortal instead.
    _PortalFactoryType = Callable[[], AbstractContextManager[anyio.abc.BlockingPortal]]

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ============================
ERROR apps/api/tests/integration/test_identity_repositories.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
```

> **红灯 A 的弱点（如实说明）**：它是收集期的 `ModuleNotFoundError`，**没有断言失败文字**。
> 新建模块时这是 TDD 的天然红灯——模块不存在就不可能有断言层失败。
> 想要「失败断言」形态的红灯，只能先写一个空的实现骨架，那与「先写测试」是相反的顺序。
> **这条弱点在 Task 72 起被补上**：每个任务在提交后追加一条**红灯 B（定点变异）**——
> 把实现里的要害那一行改掉，重跑门禁，让红灯**指向具体的断言**。

**Run:**（绿灯）

```bash
.venv/bin/pytest apps/api/tests/integration/test_identity_repositories.py -q
```

**绿灯原文**：

```text
...................                                                      [100%]
=============================== warnings summary ===============================
.venv/lib/python3.11/site-packages/fastapi/testclient.py:1
  /home/asus/s5verify/repo/.venv/lib/python3.11/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

.venv/lib/python3.11/site-packages/starlette/testclient.py:53
  /home/asus/s5verify/repo/.venv/lib/python3.11/site-packages/starlette/testclient.py:53: DeprecationWarning: The anyio.abc.BlockingPortal alias is deprecated, use anyio.from_thread.BlockingPortal instead.
    _PortalFactoryType = Callable[[], AbstractContextManager[anyio.abc.BlockingPortal]]

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
```

**Run:**（完整门禁）

```bash
bash /home/asus/s5verify/ci.sh
```

**`make ci` 原文**（关键行）：

```text
All checks passed!
Success: no issues found in 117 source files
Analyzed 149 files, 605 dependencies.
Contracts: 5 kept, 0 broken.
capability-map: 0 个问题
module-map: 0 个问题
openapi 契约一致
TOTAL                                                                 2979     68    98%
791 passed, 16 skipped, 2 warnings in 118.52s (0:01:58)
Found 0 warnings and 0 errors.
ci: 全部通过
```

**改动文件集**（`git show --stat`）：

```text
 apps/api/src/sentrix/bootstrap/container.py        |  10 +
 apps/api/src/sentrix/domain/identity/models.py     | 219 ++++++++
 apps/api/src/sentrix/domain/identity/ports.py      | 153 ++++++
 .../persistence/repositories/entities.py           | 563 +++++++++++++++++++++
 .../persistence/repositories/faces.py              | 205 ++++++++
 .../integration/test_identity_repositories.py      | 507 +++++++++++++++++++
 docs/architecture/module-map.md                    |   8 +-
 docs/refactor/capability-map.jsonl                 |  15 +
 8 files changed, 1678 insertions(+), 2 deletions(-)
```

**零成本算术不变量**：`791 passed − 772 passed = 19`，与本任务门禁的 19 条相等 ✔

**这一跑同时给出的其它量**：`mypy` 从 **113** 长到 **117** 个源文件
（`models.py` / `ports.py` / `repositories/faces.py` / `repositories/entities.py` 四个）；
import-linter 从 145 files / 579 deps 长到 **149 / 605**；
`TOTAL 2979`（Task 70 是 2844）。

**实施期被逼出来的四条决定**（都已登记或写在代码注释里）：

1. **测试写得欠定、不是实现错**：`rename_person` 会先写一条 `property:aliases` 修订再写
   `canonical_name`，第一版断言用无 `ORDER BY` 的 `fetchone()` 取行，拿错了。
   改成按 `field_name` 过滤。
2. `list_properties` 的默认视图照 1.0 过滤 `status IN ('active','pending')`，
   `superseded` 要显式要历史才看得到——改的是测试，实现不动。
3. `maintain_entity_property_values`（多值合并）**不移植**，理由见上。
4. 提及桥收进了 `EntityRepository`（`link_confirmed_mention`）。它不在原计划 Task 71 的
   符号清单里，但它是 `EntityRepository` 的职责、且 Task 72/73 确实会调它——
   **如果不收进来，Task 72 就会自己实现一份，那是两份真相。**

---

### Task 72（S5a）· 在线匹配用例 ✅ 已落地

**commit:** `3e469ab`

**Files:**

```text
apps/api/src/sentrix/application/identity/__init__.py           【新】包标记
apps/api/src/sentrix/application/identity/enroll_face.py        【新】在线匹配用例
apps/api/src/sentrix/domain/identity/models.py                  【改】+FaceDetection / 默认 scope / 待确认簇文案
apps/api/src/sentrix/domain/identity/ports.py                   【改】+FaceRepository 12 个方法 / EntityRepository 3 个
apps/api/src/sentrix/infrastructure/persistence/repositories/faces.py     【改】候选查询 / 建簇 / 实例落库 / 确认 / 退休 / 改挂
apps/api/src/sentrix/infrastructure/persistence/repositories/entities.py  【改】create_person / confirm_person / delete_candidate
apps/api/tests/integration/test_enroll_face.py                  【新】门禁 22 条
docs/architecture/module-map.md                                 【改】
docs/refactor/capability-map.jsonl                              【改】+4 条
scripts/check_reachability.py                                   【改】PENDING_WIRING 登记（Task 75 接线前）
```

**移植**：1.0 `db.py:4815-4911` `add_face_instance` 的九个分支，逐条对回：

| 分支 | 1.0 的位置 |
|---|---|
| `scope_id` 由 asset 决定，调用方传不进来 | `db.py:4830-4834` |
| 资格闸门（`identity_eligible and validity == "verified"`） | `db.py:4862-4868` |
| `quality >= 0.30` 才进候选循环 | `db.py:4868` |
| 候选 = 同 scope 且 `status IN ('pending','confirmed')` | `db.py:4870-4875` |
| 原型签名不匹配 → 跳过该簇 | `db.py:4876-4884` |
| 分数 = 与所有原型余弦的 max；无原型退回代表向量 | `db.py:4885-4900` |
| `< 0.30` → 建新簇（同时建 pending 实体） | `db.py:4901-4910`、`:4629-4663` |
| 落实例 → 刷原型 → 写 `memory_vectors(space='visual')` | `db.py:4905-4911`、`:4922-4955` |
| 只有 `status='confirmed'` 的实体才写提及 | `db.py:4912-4921` |

**核心代码** —— `apps/api/src/sentrix/application/identity/enroll_face.py:48-57`（节选）：

```python
MATCH_THRESHOLD = 0.30
# 质量门槛：低于它的人脸根本不参与匹配。1.0 ``db.py:4868`` 的字面量。
# 与上一行数值相同纯属巧合——一个是「像不像」，一个是「这张脸够不够清楚」。
MINIMUM_MATCH_QUALITY = 0.30

# 1.0 ``add_face_instance(model_name="buffalo_l")`` 的默认值。
DEFAULT_EMBEDDING_MODEL = "buffalo_l"
# 检测器没说版本时的兜底。1.0 ``db.py:4846`` 的字面量：``"legacy"`` 是一个**真实**的
# 签名取值，它会参与 (model_name, model_version) 比对，不是一个占位符。
LEGACY_EMBEDDING_VERSION = "legacy"
```

**这两个 `0.30` 是两个不同的常量**，数值相同、语义无关（1.0 里也是两处独立的字面量：
`db.py:4815` 的函数默认参数与 `db.py:4868` 的 `if quality >= 0.30`）。
`MATCH_THRESHOLD` 是**余弦阈值**，`MINIMUM_MATCH_QUALITY` 是**质量门槛**（低于它的
已 verified 人脸不去匹配已有簇，直接建新簇）。**各有一条对照用例，且互相不可替代——
红灯 B 的变异 1 只打中质量门那一条。**

**核心代码** —— `apps/api/src/sentrix/application/identity/enroll_face.py:175-201`（节选）：

```python
    def _best_cluster(
        self, scope_id: str, resolved: _ResolvedFace, threshold: float
    ) -> tuple[str | None, float]:
        """同 scope 的候选簇里挑一个最像的；没有够像的就给 ``None``。

        质量门在循环**外面**（1.0 ``db.py:4867`` 的 ``if quality >= 0.30:``）：它省掉的
        是整轮匹配，不只是某一次比对。
        """
        best_id: str | None = None
        best_score = 0.0
        if resolved.quality < MINIMUM_MATCH_QUALITY:
            return None, 0.0
        for cluster in self._faces.list_candidates(scope_id):
            representatives = self._representatives(cluster.id, resolved)
            if representatives is None:
                continue
            score = max(
                (cosine(resolved.embedding, item) for item in representatives),
                default=0.0,
            )
            # 严格大于：并列时保留先到的那个，结果与候选顺序无关。
            if score > best_score:
                best_id, best_score = cluster.id, score
        if best_id is None or best_score < threshold:
            return None, best_score
        return best_id, best_score

```

**Run:**（仓库根）

```bash
.venv/bin/pytest apps/api/tests/integration/test_enroll_face.py -q
```

**Expected（红灯 A）**：收集期 `ModuleNotFoundError`（`sentrix.application.identity` 不存在）。

**红灯 A 原文**：

```text

==================================== ERRORS ====================================
_______ ERROR collecting apps/api/tests/integration/test_enroll_face.py ________
ImportError while importing test module '/home/asus/s5verify/repo/apps/api/tests/integration/test_enroll_face.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
../../.local/share/uv/python/cpython-3.11.16-linux-x86_64-gnu/lib/python3.11/importlib/__init__.py:126: in import_module
    return _bootstrap._gcd_import(name[level:], package, level)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
apps/api/tests/integration/test_enroll_face.py:32: in <module>
    from sentrix.application.identity.enroll_face import (
E   ModuleNotFoundError: No module named 'sentrix.application.identity'
=============================== warnings summary ===============================
.venv/lib/python3.11/site-packages/fastapi/testclient.py:1
  /home/asus/s5verify/repo/.venv/lib/python3.11/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

.venv/lib/python3.11/site-packages/starlette/testclient.py:53
  /home/asus/s5verify/repo/.venv/lib/python3.11/site-packages/starlette/testclient.py:53: DeprecationWarning: The anyio.abc.BlockingPortal alias is deprecated, use anyio.from_thread.BlockingPortal instead.
    _PortalFactoryType = Callable[[], AbstractContextManager[anyio.abc.BlockingPortal]]

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ============================
ERROR apps/api/tests/integration/test_enroll_face.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
```

**红灯 B（定点变异）**——见本节末尾的表格，原文在
`/home/asus/s5verify/logs/task72-redB.txt`。这一次红灯**指向具体断言**，
不再是「模块不存在」。

**Expected（绿灯）**：`22 passed`。

**绿灯原文**：

```text
......................                                                   [100%]
=============================== warnings summary ===============================
.venv/lib/python3.11/site-packages/fastapi/testclient.py:1
  /home/asus/s5verify/repo/.venv/lib/python3.11/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

.venv/lib/python3.11/site-packages/starlette/testclient.py:53
  /home/asus/s5verify/repo/.venv/lib/python3.11/site-packages/starlette/testclient.py:53: DeprecationWarning: The anyio.abc.BlockingPortal alias is deprecated, use anyio.from_thread.BlockingPortal instead.
    _PortalFactoryType = Callable[[], AbstractContextManager[anyio.abc.BlockingPortal]]

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
22 passed, 2 warnings in 8.59s
```

**`make ci` 原文**（关键行）：

```text
All checks passed!
Success: no issues found in 120 source files
Analyzed 151 files, 614 dependencies.
Contracts: 5 kept, 0 broken.
reachability: 0 个问题
capability-map: 0 个问题
module-map: 0 个问题
openapi 契约一致
TOTAL                                                                 3127     70    98%
821 passed, 16 skipped, 2 warnings in 141.62s (0:02:21)
Found 0 warnings and 0 errors.
ci: 全部通过
```

**改动文件集**（`git show --stat`）：

```text
 .../src/sentrix/application/identity/__init__.py   |   1 +
 .../sentrix/application/identity/enroll_face.py    | 340 ++++++++++++
 apps/api/src/sentrix/domain/identity/models.py     |  62 +++
 apps/api/src/sentrix/domain/identity/ports.py      | 134 +++++
 .../persistence/repositories/entities.py           | 106 ++++
 .../persistence/repositories/faces.py              | 186 ++++++-
 apps/api/tests/integration/test_enroll_face.py     | 609 +++++++++++++++++++++
 docs/architecture/module-map.md                    |  22 +-
 docs/refactor/capability-map.jsonl                 |   4 +
 scripts/check_reachability.py                      |   9 +-
 10 files changed, 1460 insertions(+), 13 deletions(-)
```

**零成本算术不变量**：`821 passed − 799 passed = 22`，与本任务门禁的 22 条相等 ✔

**实施期的一处更正**：`EnrollFace` 的异常类与 `ReviewCluster` 共用一套带 `status_code`
的层次（见 Task 73），这是本仓库的**新约定**，登记在 `s5-deviations.md` 证据 52，
**统一口径待用户确认**。

---

### Task 73（S5a）· 簇审阅用例 ✅ 已落地

**commit:** `5c30554`

**Files:**

```text
apps/api/src/sentrix/application/identity/review_cluster.py    【新】confirm / reject / merge / split
apps/api/tests/integration/test_review_cluster.py              【新】门禁 23 条
docs/architecture/module-map.md                                【改】
docs/refactor/capability-map.jsonl                             【改】+6 条
scripts/check_reachability.py                                  【改】PENDING_WIRING 登记（Task 75 接线前）
```

**移植**：`db.py:5152-5193` `confirm_face_cluster`、`:5579-5589` `reject_face_cluster`、
`:4776-4796` `merge_face_clusters`、`:4797-4814` `split_face_instance`、
`:5290-5341` `delete_person_candidate`、`:5194-5245` `_merge_cluster_into_person`。

**核心代码** —— `apps/api/src/sentrix/application/identity/review_cluster.py:57-79`（节选）：

```python
class ClusterReviewError(Exception):
    """簇审阅的领域错误。

    ``status_code`` 是**产品契约**的一部分（400 / 404 / 409），Task 75 的路由按它映射
    HTTP。数字定在这里而不是路由里：它描述的是产品行为（「已确认的人不能被拒绝」是
    冲突，不是参数错），定在路由里就意味着只有起了 HTTP 才验得到它。
    """

    status_code = 400


class InvalidReviewRequestError(ClusterReviewError, ValueError):
    status_code = 400


class ClusterNotFoundError(ClusterReviewError, LookupError):
    status_code = 404


class ReviewConflictError(ClusterReviewError, ValueError):
    status_code = 409


```

三个异常带 `status_code`，**这是本仓库的一个新约定**：application 层不许 import
`api/errors`（`application-purity` 契约），所以 400 / 404 / 409 只能定在用例自己的异常上。
现有的 `TurnNotFoundError` 不带码、由路由硬编码 404——**两种做法并存意味着
「状态码从哪来」变成看运气**，统一口径已登记为待裁决项（`s5-deviations.md` 证据 52）。

**核心代码** —— `apps/api/src/sentrix/application/identity/review_cluster.py:117-164`（节选）：

```python
    def confirm(self, cluster_id: str, *, name: str, family_role: str | None = None) -> ConfirmResult:
        """确认一簇脸是某个人。

        名字先判（400 早于 404）：1.0 在路由里就是这个顺序，两处都错时报的是名字错。
        ``family_role`` 会 ``strip`` 后把空串折成 ``None``——那是 1.0 路由做的事
        （``app.py:1978``），2.0 收进用例：它是一个关于值的规则，不是传输格式。
        """
        clean_name = str(name or "").strip()
        if not clean_name:
            raise InvalidReviewRequestError("entity name is required")
        role = str(family_role or "").strip() or None
        cluster = self._require_cluster(cluster_id)
        scope_id = cluster.scope_id or DEFAULT_SCOPE_ID

        existing = self._entities.find_confirmed_person_by_name(scope_id, clean_name)
        if existing is not None:
            return self._merge_into_person(cluster, existing.entity_id)

        instances = self._faces.list_instances(cluster.id)
        observation_ids = _dedupe([item.observation_id for item in instances])
        summary = CONFIRMED_SUMMARY.format(len(set(observation_ids)))

        entity = self._entities.get(cluster.entity_id) if cluster.entity_id else None
        if entity is None:
            entity = self._entities.create_person(
                clean_name,
                status=ENTITY_STATUS_CONFIRMED,
                family_role=role,
                confidence=1.0,
                summary=summary,
                scope_id=scope_id,
            )
        else:
            entity = self._entities.confirm_person(
                entity.id, name=clean_name, family_role=role, summary=summary
            )
        self._faces.confirm_cluster(cluster.id, entity.id)
        for instance in instances:
            self._entities.link_confirmed_mention(
                entity.id,
                instance.observation_id,
                face_instance_id=instance.id,
                confidence=instance.detection_confidence,
            )
        if role:
            self._write_family_role(scope_id, clean_name, role, observation_ids)
        return ConfirmResult(entity=entity, merged_into=None)

```

**必须钉住的行为**（23 条门禁）：

| 组 | 条数 | 钉住的东西 |
|---|---|---|
| confirm | 7 | 空名字 → 400；`entities.status='confirmed'` + `confidence = MAX(…,1)`；簇 `status='confirmed'` + `revision+1`；**簇内每个实例都写 mention**；`family_role` 非空 → 事实 `("家庭角色", role, 1.0)`；同名已确认人物 → 走合并分支并回 `merged_into` |
| reject | 4 | confirmed 簇 → **409**；否则删候选实体，**人脸实例保留且 `cluster_id = NULL`**；即使簇不是 confirmed，实体是 confirmed 时也要拒（仓储兜底 `ValueError`） |
| merge | 6 | 跨 scope → **409**；两个 confirmed → **409**；只改 `face_instances.cluster_id`；源簇 `rejected` + `member_count=0`；刷目标原型；写 `entity_revisions(face_cluster_merge)` |
| split | 4 | 实例不属于该簇 → 404；写 `entity_revisions(face_cluster_split)`；新簇的归属 |
| 审计 | 2 | merge / split 各写一条 `entity_revisions`（**修订只在这两处 + 同名合并那一路**，见下） |

**一处对计划的更正**：计划原文写「全部四件都写 `entity_revisions`（审计要求，产品硬约束）」——
**那是错的**。实读 1.0 `db.py:5152-5193`：`confirm_face_cluster` **不调**
`_record_entity_revision`；写审计的只有 `_merge_cluster_into_person`（`user_name_merge`）、
`merge_face_clusters`（`user_merge`）、`split_face_instance`（`user_split`）三处；
**reject 不可能写**——它删的正是那个实体的 `entity_revisions` 行。
**Task 73 按 1.0 实现，没有自造第四条**。已登记在 `s5-deviations.md` 证据 50。

**绝对没有写的**（「为什么不能换序」第 1 条）：`_refresh_event_participants` /
`resegment_events_for_confirmed_entity` / `rebuild_person_memory`——`grep` 两个新模块，
这三个名字**只出现在 docstring 里**，不在代码里。**也没有留空的回调钩子**——
那正是 S4 栽过的「零调用的纯规则」。Task 81 落地时会回头改 `confirm` 那一处。

**Run:**（仓库根）

```bash
.venv/bin/pytest apps/api/tests/integration/test_review_cluster.py -q
```

**Expected（红灯 A）**：收集期 `ModuleNotFoundError`。

**红灯 A 原文**：

```text

==================================== ERRORS ====================================
______ ERROR collecting apps/api/tests/integration/test_review_cluster.py ______
ImportError while importing test module '/home/asus/s5verify/repo/apps/api/tests/integration/test_review_cluster.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
../../.local/share/uv/python/cpython-3.11.16-linux-x86_64-gnu/lib/python3.11/importlib/__init__.py:126: in import_module
    return _bootstrap._gcd_import(name[level:], package, level)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
apps/api/tests/integration/test_review_cluster.py:23: in <module>
    from sentrix.application.identity.review_cluster import (
E   ModuleNotFoundError: No module named 'sentrix.application.identity.review_cluster'
=============================== warnings summary ===============================
.venv/lib/python3.11/site-packages/fastapi/testclient.py:1
  /home/asus/s5verify/repo/.venv/lib/python3.11/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

.venv/lib/python3.11/site-packages/starlette/testclient.py:53
  /home/asus/s5verify/repo/.venv/lib/python3.11/site-packages/starlette/testclient.py:53: DeprecationWarning: The anyio.abc.BlockingPortal alias is deprecated, use anyio.from_thread.BlockingPortal instead.
    _PortalFactoryType = Callable[[], AbstractContextManager[anyio.abc.BlockingPortal]]

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ============================
ERROR apps/api/tests/integration/test_review_cluster.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
2 warnings, 1 error in 0.06s
```

**Expected（绿灯）**：`23 passed`。

**绿灯原文**：

```text
.......................                                                  [100%]
=============================== warnings summary ===============================
.venv/lib/python3.11/site-packages/fastapi/testclient.py:1
  /home/asus/s5verify/repo/.venv/lib/python3.11/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

.venv/lib/python3.11/site-packages/starlette/testclient.py:53
  /home/asus/s5verify/repo/.venv/lib/python3.11/site-packages/starlette/testclient.py:53: DeprecationWarning: The anyio.abc.BlockingPortal alias is deprecated, use anyio.from_thread.BlockingPortal instead.
    _PortalFactoryType = Callable[[], AbstractContextManager[anyio.abc.BlockingPortal]]

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
23 passed, 2 warnings in 5.19s
```

**`make ci` 原文**（关键行）：

```text
All checks passed!
Success: no issues found in 121 source files
Analyzed 152 files, 621 dependencies.
Contracts: 5 kept, 0 broken.
reachability: 0 个问题
capability-map: 0 个问题
module-map: 0 个问题
openapi 契约一致
TOTAL                                                                 3259     76    98%
844 passed, 16 skipped, 2 warnings in 149.57s (0:02:29)
Found 0 warnings and 0 errors.
ci: 全部通过
```

> **这一跑里有一次真实的 SIGKILL 假红**：第一次 `make ci` 死在
> `make: *** [Makefile:56: coverage] 已杀死`，`ci.sh` 按判据重跑 run1 后全绿。
> 两段原始输出都在 `logs/ci-20260923-122113-run0.log` 与 `-run1.log`。
> **这是顾虑①那条判据第一次在真实工作里救下一次误判**——不重跑就会拿假红灯去改代码。

**改动文件集**（`git show --stat`）：

```text
 .../sentrix/application/identity/review_cluster.py | 353 +++++++++++++
 apps/api/tests/integration/test_review_cluster.py  | 573 +++++++++++++++++++++
 docs/architecture/module-map.md                    |   9 +-
 docs/refactor/capability-map.jsonl                 |   6 +
 scripts/check_reachability.py                      |   4 +
 5 files changed, 941 insertions(+), 4 deletions(-)
```

**零成本算术不变量**：`844 passed − 821 passed = 23`，与本任务门禁的 23 条相等 ✔

**红灯 B（定点变异）**——Task 72 与 73 各三条，改一行 → 重跑门禁 → 原文 → 改回：

| 任务 | 变异 | 红的那条用例 |
|---|---|---|
| 72 | `quality < MINIMUM_MATCH_QUALITY` → `<=` | `test_quality_exactly_at_the_gate_still_matches` |
| 72 | 资格闸门去掉 `validity == "verified"` 这一半 | 2 条（含「不合格也别丢向量」那条） |
| 72 | 删掉原型签名比对整段 | `test_a_cluster_whose_prototypes_carry_another_signature_is_skipped` |
| 73 | `confidence = MAX(confidence, 1)` → `confidence = confidence` | `test_confirm_marks_the_person_and_the_cluster`（`assert 0.4 == 1.0`） |
| 73 | reject 的判据 `== CONFIRMED` → `!=` | 3 条（含仓储兜底的 `ValueError: confirmed person entities cannot be deleted`） |
| 73 | 拆分的实例查找 `find_instance(cluster_id, fiid)` → `get_instance(fiid)` | `test_split_of_an_instance_from_another_cluster_is_a_404`（`DID NOT RAISE`） |

**这六条才是「行为真的被钉住」的证据**。红灯 A（收集期 `ModuleNotFoundError`）只证明
「测试先于实现落地」，**不证明任何一条断言有牙齿**——Task 69/70/71 的红灯都停在这一层。
原文：`/home/asus/s5verify/logs/task72-redB.txt`、`task73-redB.txt`。

**一处从 1.0 继承来的地雷（探针实测，未改，待裁决）**：对一个**已经 confirmed** 的簇
用**同名**再确认一次，会走同名合并分支，而目标实体就是它自己 → 该簇 `status='rejected'`。
根因在 `db.py:5157-5159`：`existing = find_confirmed_person_by_name(scope_id, name)`
命中即合并，**没有排除「命中的就是本簇的实体」**。1.0 同样如此。
**照搬 + 登记，未改**——改法简单但是产品可见的行为变更。见 `s5-deviations.md` 证据 51。

---

### Task 74（S5a）· 命名与同名合并

**Files:**

```text
apps/api/src/sentrix/application/identity/name_person.py    【新】
apps/api/tests/integration/test_name_person.py    【新】
```

- **移植**：`db.py:4407-4435`（`find_confirmed_person_by_name` / `person_aliases` /
  `set_person_aliases`）、`:4436-4466` `rename_person`。
- **必须钉住的行为**：改名后**旧名进 aliases**；`name_state='confirmed'`；
  写 `entity_revisions(source='user_rename')`；`find_confirmed_person_by_name` 先精确名再扫 aliases；
  **同名只在同 scope 内合并**（1.0 的 `test_cross_scope_same_name_does_not_merge` 钉的就是这条）。
- **门禁**：`apps/api/tests/integration/test_name_person.py`。

### Task 75（S5a）· HTTP 路由 + PersonDirectory 实现

**Files:**

```text
apps/api/src/sentrix/api/routes/identity.py    【新】
apps/api/src/sentrix/api/schemas/identity.py    【新】
apps/api/src/sentrix/application/identity/person_directory.py    【新】
apps/api/src/sentrix/main.py    【改】
apps/api/src/sentrix/bootstrap/container.py    【改】
apps/api/tests/contract/test_identity_routes.py    【新】
packages/contracts/openapi/openapi.json    【改】
docs/architecture/module-map.md    【改】
docs/refactor/capability-map.jsonl    【改】
```

- **移植**：`app.py:1969-2100`（簇列表与四审阅端点）、`:1512-1800`（`/api/people` 系列）、
  `:2205`（batch-confirm）、`:2237-2250`（rename）、`:2508-2533`（人脸裁剪）。
- **2.0 约定**：路由文件放 `api/routes/identity.py`，在 `main.py` 的 `v1` 上 `include_router`；
  **改了路由必须重跑 `scripts/export_openapi.py`**（`make ci` 的 `--check` 会红）。
- **同时**：把 `NoIdentityDirectory` 换成真实实现（`PersonDirectory.lookup`），
  工具契约 / 账本 / 门禁一行都不动（`infrastructure/assistant/identity.py:11` 的约定）。
- **必须钉住的行为**：四审阅端点的状态码（400 / 404 / 409）；裁剪端点的
  `thumbnail((256,256))` + `quality=88` + `Cache-Control`；bbox 非法 → 422。
- **门禁**：`apps/api/tests/contract/test_identity_routes.py`。

### Task 76（S5a）· MemorySpace 归位

**Files:**

```text
apps/api/src/sentrix/domain/spaces/__init__.py    【新】
apps/api/src/sentrix/domain/spaces/models.py    【新】
apps/api/src/sentrix/domain/spaces/ports.py    【新】
apps/api/src/sentrix/infrastructure/persistence/repositories/spaces.py    【改】
apps/api/src/sentrix/bootstrap/container.py    【改】
apps/api/tests/unit/test_container_wiring.py    【改】
apps/api/tests/unit/test_spaces_ports.py    【新】
docs/architecture/module-map.md    【改】
```

- **现状**：`MemorySpace` 定义在 `infrastructure/persistence/repositories/spaces.py:11-18`，
  `Container.spaces` 是**全仓唯一**字段类型落在 infrastructure 的例外，
  由 `tests/unit/test_container_wiring.py` 的 `EXCEPTIONS` 字典强制。
- **要做**：新建 `domain/spaces/models.py`（`MemorySpace`）与 `domain/spaces/ports.py`
  （`SpaceRepository` Protocol）；`spaces.py` 只留 SQL；`container.py` 字段类型改成端口并
  **删掉例外**；`module-map.md` 里「装配点按端口声明」一节的两个例外改成**一个**（`database`）。
- **这一条 1.0 没有对应实现**（1.0 没有分层），属于设计 §2.3 的既定目标，**不是新设计**。
  用户 brief 把它列进 S5（「顺便看看是否该一起把它迁进 domain」），本计划**建议做**，
  理由是它让 `test_container_wiring.py` 的例外表少一条，而不是多一条。
- **风险**：`EXCEPTIONS` 字典是**两张表**（`container.py` 注释 + `test_container_wiring.py` 的字典
  + `module-map.md` 的说明文），三处要同时改，漏一处 `make ci` 红。
- **门禁**：`apps/api/tests/unit/test_container_wiring.py`（改完后仍绿）+ `test_spaces_ports.py`。

### Task 77（S5b）· 人脸检测适配器

**Files:**

```text
apps/api/src/sentrix/infrastructure/models/face_detector.py    【新】
apps/api/src/sentrix/infrastructure/models/onnx_runtime.py    【新】
apps/api/pyproject.toml    【改】
apps/api/tests/unit/test_face_geometry.py    【新】
apps/api/tests/integration/test_face_detector.py    【新】
docs/architecture/module-map.md    【改】
```

- **移植**：`backend/face_detector.py:1-181` 全文 + `model_clients.py:1718-1840`
  `_detect_retina_tiled_unlocked` 的 SCRFD 复核段 + `backend/onnx_runtime.py` 全文。
- **新增依赖**：`onnxruntime-gpu`、`insightface`、`opencv-python-headless`（2.0 venv 实测都没有）。
- **必须钉住的行为**：priors / decode / NMS `0.4` / 两趟 tiling 参数；
  `_bbox_agreement` 的 `center_ratio=1.0` + `iou_threshold=0.05`；
  `_landmark_sanity` 的 `min_eye_ratio=0.1` + 「框小于 64 时直接放行」；
  `_expand_crop` 的 `margin=0.75` / `min_side=256`；
  provider 显存上限默认 `256 MiB`、`SENTRIX_FACE_GPU_MAX_CONCURRENCY` 默认 `1`（8091 生产设 4）。
- **门禁**：`apps/api/tests/unit/test_face_geometry.py`（priors/decode/NMS 可纯 CPU 验）
  + `apps/api/tests/integration/test_face_detector.py`（需要真权重，缺失时 `skip`）。

### Task 78（S5b）· 人脸向量适配器

**Files:**

```text
apps/api/src/sentrix/infrastructure/models/face_adapter.py    【新】
apps/api/tests/integration/test_face_embedding.py    【新】
docs/architecture/module-map.md    【改】
```

- **移植**：`model_clients.py:1690-1716`（`_load_buffalo_recognition` / `_recognition_embed`）。
- **必须钉住的行为**：112×112、`(x-127.5)/128.0`、512 维、`embedding_model="buffalo_l"`、
  `embedding_version = FACE_MODEL_NAME`（**1.0 的变量名与语义不符但有真实后果**：
  它参与签名比对，换模型包时旧簇会被整体跳过——照搬，不改名）。
- **门禁**：`apps/api/tests/integration/test_face_embedding.py`（权重缺席时 skip）。

### Task 79（S5b）· 导入路径接线

**Files:**

```text
apps/api/src/sentrix/application/identity/seed_identity.py    【新】
apps/api/src/sentrix/application/media/import_album.py    【改】
apps/api/src/sentrix/bootstrap/container.py    【改】
apps/api/tests/integration/test_import_identity.py    【新】
docs/architecture/module-map.md    【改】
```

- **移植**：`pipeline.py:316-320`（人脸先于事件）与 `pipeline.py:375`（fast-evidence 路径）；
  `app.py:2253-2310` `_seed_face_detect` 的三级降级。
- **必须钉住的行为**：人脸写入必须早于事件归并（对照测试）；三级降级的三个触发条件
  （`max(w,h) > 600`、`scale = min(2.0, 1280/max_dim) > 1.05`、整图兜底）；
  **1.0 的整图兜底是死路径**（见「偏离」第 4 条）。
- **门禁**：`apps/api/tests/integration/test_import_identity.py`。

### Task 80（S5b）· `person_score` 接线

**Files:**

```text
apps/api/src/sentrix/application/memories/assign_events.py    【改】
apps/api/src/sentrix/bootstrap/container.py    【改】
apps/api/tests/integration/test_assign_events.py    【改】
```

- **移植**：`db.py:2241-2260` `_confirmed_entity_ids_for_observation` / `_for_event` → 喂进
  **已经存在的** `AssignEvents.confirmed_people`。
- **必须钉住的行为**：交集非空 → `0.8`（`PERSON_MATCH`）；`person_score` 参与
  `corroborated`（`domain/memories/events.py:317`），因此会改变 `split_guard` 的结果——
  **一条「有共同确认人物时不再拆事件」的对照用例**（这条比分数本身更接近产品行为）。
- **门禁**：`apps/api/tests/integration/test_assign_events.py` 加用例（文件已存在）。

### Task 81（S5b）· 事件参与者 / 实体投影 + 确认后重分段

**Files:**

```text
apps/api/src/sentrix/application/identity/review_cluster.py    【改】
apps/api/src/sentrix/application/memories/assign_events.py    【改】
apps/api/src/sentrix/infrastructure/persistence/repositories/participants.py    【新】
apps/api/src/sentrix/domain/memories/ports.py    【改】
apps/api/tests/integration/test_event_participants.py    【新】
docs/architecture/module-map.md    【改】
```

- **移植**：`db.py:5549-5578` `_refresh_event_participants`、`:2486` `upsert_event_participant`、
  `:3287` `upsert_event_entity`、`:5450-5481` `resegment_events_for_confirmed_entity`。
- **同时改** Task 73 的 `confirm`：接上这三件事（顺序见「为什么不能换序」第 1 条）。
- **必须钉住的行为**：`visible_subject` 置信度 `0.75`；`captured_by` 取 `source_confidence`（缺省 `0.5`）；
  每个受影响事件 `revision+1`；`video_scene` 事件**不重算摘要**（`db.py:5575`）；
  重分段只在 **6 小时窗内 + 八分量 `total >= 0.50`** 时合并。
- **门禁**：`apps/api/tests/integration/test_event_participants.py`。

### Task 82（S5b）· facts 门禁开通 + 候选来源接线 【裁决④】

**Files:**

```text
apps/api/src/sentrix/application/memories/maintain_facts.py    【改】
apps/api/src/sentrix/domain/media/ports.py    【改】
apps/api/src/sentrix/infrastructure/media/vlm_observer.py    【改】
apps/api/src/sentrix/infrastructure/persistence/migrations/0007_fact_candidates.sql    【新】
apps/api/tests/integration/test_maintain_facts.py    【改】
apps/api/tests/unit/test_vlm_observer.py    【改】
docs/architecture/module-map.md    【改】
```

**两件事要一起做**——`s4-deviations.md` 的 §9 已经写明「只放宽门禁不够，只补候选来源也不够，
两条都要动才出得了东西」。用户裁决④选了「补候选来源」这一条，所以本任务**两半都做**。

**前半 · 门禁**：

- **移植**：`db.py:1494-1503` `is_confirmed_person_name`（**只校验 subject，精确名，不查 alias**）
  → `MaintainFacts.confirmed_people`；`db.py:3433-3447` `purge_unanchored_facts`。
- **注意类型**：`MaintainFacts.confirmed_people` 的入参是 `scope_id`、回的是**名字集合**
  （不是 id）——见 `application/memories/maintain_facts.py:65,78` 的现有签名。

**后半 · 候选来源（按 1.0 原样：模型自报）**：

- **移植**：`model_clients.py:1130-1198` 的 `analyze_image` 输出里的 `facts` 数组 +
  `pipeline.py:347` 的 `result.get("facts", [])`。
- 2.0 的落点：`domain/media/ports.py` 的 `ObservationSemantics` **加一个字段**（facts），
  `infrastructure/media/vlm_observer.py` 的提示词与解析按 1.0 原文搬，
  `application/media/observe_asset.py` 落库到观察的语义列（0006 已加 `people_json`，
  **facts 的落库列如果 2.0 没有，本任务一并加进 0006 或新开 0007**——执行时按当时的迁移状态定，
  但**不允许把它塞进 `detail_json` 那种无结构 blob**）。
- **1.0 的 facts 数组元素形状**（`model_clients.py` 的解析段）：`{subject, predicate, object, confidence}`；
  落库走既有的 `decide_fact_write` 两阶段规则（`domain/memories/facts.py`），本任务**不改那条规则**。
- **为什么要回到模型自报**：`maintain_fact` 的规则层只能决定「这条候选怎么写」，
  它变不出候选。1.0 的候选来自 VLM 的 `facts` 字段，2.0 在 S4 没搬它，
  于是 `candidates` 在生产装配下恒为空元组——**门禁全开也写不出一条事实**。
- **一处必须一起改的地方**：`s4-deviations.md` 第 30 条结尾写的「事实由 `maintain_fact`
  规则决定，**不由模型自报（S5）**」**已按裁决④作废并就地改正**（见该文件）。
  实现时按改后的记法走：**候选由模型自报，写法由规则决定**。这两句不矛盾，是两件事。

- **门禁**：`apps/api/tests/integration/test_maintain_facts.py` 加用例（文件已存在）
  + `apps/api/tests/unit/test_vlm_observer.py` 加「提示词要求 facts、解析器认 facts」的用例
  + 一条**端到端**：观察带 `facts` → 门禁开着 → `facts` 表里出现该人物的行。

### Task 83（S5b）· `person_bridge` 投影 + 实体检索通道

**Files:**

```text
apps/api/src/sentrix/application/retrieval/retrieve_entity.py    【新】
apps/api/src/sentrix/domain/retrieval/lexical.py    【改】
apps/api/src/sentrix/domain/retrieval/ports.py    【改】
apps/api/src/sentrix/infrastructure/persistence/repositories/search_terms.py    【改】
apps/api/tests/integration/test_retrieve_entity.py    【新】
docs/architecture/module-map.md    【改】
```

- **移植**：`retrieval_indexes.py:20,180`（字段类型 `person_bridge`，值源是观察的 `people`）
  + `retrieval/entity.py:1-60` 全文。
- **必须钉住的行为**：人条件是**硬条件**（不是加分）；检索器**从不宣布身份**，
  只返回候选（`entity.py:1-8` 的 docstring 原文）；缺 `person_bridge` 投影时退回直接读观察的 people。
- **门禁**：`apps/api/tests/integration/test_retrieve_entity.py`。

### Task 84（S5b）· 全局重聚类的维护入口 【裁决③新增】

**Files:**

```text
apps/api/src/sentrix/application/identity/recluster.py    【新】
apps/api/src/sentrix/api/routes/maintenance.py    【新】
apps/api/src/sentrix/main.py    【改】
apps/api/src/sentrix/bootstrap/container.py    【改】
apps/api/tests/integration/test_recluster_faces.py    【新】
apps/api/tests/contract/test_maintenance_routes.py    【新】
packages/contracts/openapi/openapi.json    【改】
docs/architecture/module-map.md    【改】
```

- **移植**：`db.py:4956-5047` `recluster_faces` + `:5048-5117` `_attach_uncertain_faces`
  + `:5118-5132` `_sync_face_entity_statuses`。三者是一组：后两个**只被
  `recluster_faces` 调**（`db.py:5038-5039` 是它们唯一的调用点），所以要么一起搬，要么一起不搬。
- **2.0 的形态与 1.0 的区别（这就是裁决③的落点）**：
  1. **入口是一条 HTTP 维护路由**（`POST /api/v1/maintenance/recluster-faces`），
     不是导入路径的一部分——**门禁里必须有一条「导入路径不会触发它」的对照断言**；
  2. 1.0 的调用点在 `scripts/benchmarks/ingest_album3_v2.py:67` 与
     `ingest_household_face_benchmark.py:57`，2.0 不复刻脚本，只提供入口；
  3. 入参（`threshold` / `minimum_quality`）**不取 1.0 那两个 benchmark 调用方的实参**
     （`threshold=0.30` / `minimum_quality=0.55`），而是取 `FaceClusterer` 的类默认值
     （`0.30` / `0.30`）——那对实参属于被判死的 benchmark 调用方，
     且 `0.55` 与类默认的 `0.30` 不一致这件事已登记在 `s5-deviations.md` 证据 39。
     **默认值必须能在请求里覆盖**，否则「维护入口」就只能按一个写死的口径跑。
- **必须钉住的行为**：`_attach_uncertain_faces` 认领的是 `validity='uncertain'` 且
  `cluster_id IS NULL` 的实例；`_sync_face_entity_statuses` 按簇状态回写
  `entities.identity_state`；重聚类是**整表重算**（建新簇 → 迁移归属 → 删空簇），
  不是增量；返回里必须带**改了什么的计数**（否则维护入口跑完没有任何可核的东西）。
- **门禁**：`apps/api/tests/integration/test_recluster_faces.py`
  + `apps/api/tests/contract/test_maintenance_routes.py`。
- **注意**：这条路径**只在显式请求时跑**，所以它不能挂在导入链上，也不能挂在
  `Container.build()` 的构造期。用户裁决的理由是「聚类质量差了没有补救手段」，
  **补救手段 = 一个可以手动触发的入口**，不是「让它自动跑」。

### Task 85（S5c）· 外观证据

**Files:**

```text
apps/api/src/sentrix/domain/identity/appearance.py    【新】
apps/api/src/sentrix/application/identity/observe_appearance.py    【新】
apps/api/src/sentrix/infrastructure/persistence/repositories/appearance.py    【新】
apps/api/src/sentrix/application/identity/review_cluster.py    【改】
apps/api/tests/unit/test_person_appearance.py    【新】
apps/api/tests/integration/test_appearance_evidence.py    【新】
docs/architecture/module-map.md    【改】
```

- **移植**：`person_appearance.py:4-25` `expanded_person_crop`（纯几何，无 IO）+
  `app.py:2022-2058` `_analyze_confirmed_person_appearance` +
  `db.py:4482-4534` `record_person_appearance_evidence`。
- **必须钉住的行为**：`1.35` / `4.5` / `0.35` 三个系数；框非法或 `< 24×48` → **抛错不回退整图**；
  每人每事件只取一张（`quality DESC, detection_confidence DESC LIMIT 1`）；
  写 `person_appearance_evidence` 时 person 必须已 confirmed 且实例已 link，否则抛错；
  触发时机 = **确认人脸簇时同步**。
- **门禁**：`apps/api/tests/unit/test_person_appearance.py` + `integration/test_appearance_evidence.py`。
- **注**：`expanded_person_crop` 是 S5 里**唯一一处 1.0 有第二个生产调用者**的纯函数
  （另一个是 `agent_runtime/tools.py:2184` 的 agent 工具，属 S6）。

### Task 86（S5c）· 人物画像

**Files:**

```text
apps/api/src/sentrix/domain/identity/portrait.py    【新】
apps/api/src/sentrix/domain/identity/insights.py    【新】
apps/api/src/sentrix/application/identity/run_insights.py    【新】
apps/api/src/sentrix/api/routes/insights.py    【新】
apps/api/src/sentrix/main.py    【改】
apps/api/tests/unit/test_portrait.py    【新】
apps/api/tests/integration/test_run_insights.py    【新】
packages/contracts/openapi/openapi.json    【改】
docs/architecture/module-map.md    【改】
```

- **移植**：`person_portraits.py` 全文（`compile_portrait_evidence` / `_evidence_ref_valid` /
  `validate_portrait` / `deterministic_portrait` / `normalize_writer_output` +
  `PERSON_PORTRAIT_PROMPT` 原文）、`person_insights.py:31-111`（排序与代表照，纯规则）、
  `person_insights.py:159-253`（六阶段编排的 1/2/5/6 阶段）。
- **六阶段编排要改**：1.0 把状态写 `person_insight_runs`（裁决②**仍冻结**这张表），
  2.0 改成**同步跑、不落运行记录**，所以 `person_moments.run_id` 与
  `person_portrait_revisions` 的来源链要重新对齐（迁移 0006 里已经取消了 `run_id` 列）。
- **必须钉住的行为**：80 ≤ 汉字 ≤ 500；themes 2–6；每 theme `evidence_refs` 非空；
  敏感词黑名单；未确认角色必须带模糊措辞（`似乎/可能/从照片看/看起来/像是`）；
  三级降级 writer → repair → 确定性兜底。
- **三处 1.0 缺陷必须先报告再决定**（见「偏离」第 2/3 条）：`_evidence_ref_valid` 的
  `relationship/semantic_claim/appearance` 白名单后门、`deterministic_portrait` 通不过自己的校验、
  校验失败完全静默（`errors` 从不落日志/落库）。
- **门禁**：`apps/api/tests/unit/test_portrait.py` + `integration/test_run_insights.py`。

- **移植**：`person_appearance.py:4-25` `expanded_person_crop`（纯几何，无 IO）+
  `app.py:2022-2058` `_analyze_confirmed_person_appearance` +
  `db.py:4482-4534` `record_person_appearance_evidence`。
- **必须钉住的行为**：`1.35` / `4.5` / `0.35` 三个系数；框非法或 `< 24×48` → **抛错不回退整图**；
  每人每事件只取一张（`quality DESC, detection_confidence DESC LIMIT 1`）；
  写 `person_appearance_evidence` 时 person 必须已 confirmed 且实例已 link，否则抛错；
  触发时机 = **确认人脸簇时同步**。
- **门禁**：`apps/api/tests/unit/test_person_appearance.py` + `integration/test_appearance_evidence.py`。
- **注**：`expanded_person_crop` 是 S5 里**唯一一处 1.0 有第二个生产调用者**的纯函数
  （另一个是 `agent_runtime/tools.py:2184` 的 agent 工具，属 S6）。

### Task 87（S5c）· e2e 验收

**Files:**

```text
apps/api/tests/e2e/test_identity_album.py    【新】
docs/refactor/behaviour-baseline.md    【改】
```

- **移植**：无（这是 2.0 的验收）。
- **要断言内容，不只断言数量**（S4 栽在 `events: 53` 上）：导入一册真照片后，
  断言「至少一个簇的 `member_count >= 2`」且「该簇确认后，`entity_mentions` 的行数等于该簇实例数」、
  「确认前后同一对照片的事件归属从两个变成一个」、「`facts` 里出现该人物的家庭角色事实且
  subject 等于确认的名字」。
- **门禁**：`apps/api/tests/e2e/test_identity_album.py`。

---

## 生成侧对拍

「文档里写的」与「仓库里实际是什么」之间有四类可能对不上的地方。**每一类都做成了
机械检查**，不靠人眼——S2 的教训是「13–14% 的错误率全部发生在计划生成这一步」，
而人对着一份 300 KB 的文档校对代码块是做不到的。

### 工具

| 工具 | 作用 |
|---|---|
| `/home/asus/s5verify/expand.py` | 把任务片段里的三种占位符（`file:` / `log:` / `stat:`，写成双花括号）展开成正文。**代码块永远是从文件按行区间读出来的，不手抄**——手抄一次就漂移一次 |
| `/home/asus/s5verify/verify_plan.py` | 校验 (a) 与 (b) 两项对拍 |
| `/home/asus/s5verify/ci.sh` | 每个 commit 的完整 `make ci`，带真假红灯判据（见「顾虑的处置」顾虑①） |

### 四项对拍

**(a) 块逐字节。** 文档里每一处「核心代码 / 门禁」标记（格式为标记词 + 破折号 +
反引号包起来的 `路径:起始-结束`）后面的代码块，必须与那个文件的第 `起始`–`结束` 行**逐字节相同**。
`verify_plan.py` 对每个块做整段相等比较（不是「找相似」——相似度会让「手抄漏掉一行」
静默通过）。**为什么要求整段相等而不是「块是文件的子串」**：子串检查放过「抄了两段、
中间漏一行」这种漂移，而那种漂移恰恰是最容易发生的。

**(b) 改动文件集 ⊆ Files 清单。** 每个任务声明一个 `**commit:** \`sha\``；
`verify_plan.py` 跑 `git show --name-only <sha>` 取真实改动文件集，
断言它 ⊆ 该任务 `**Files:**` 块里列的文件。**多出来的文件就是「顺手改了没登记」**——
这一类在 S3/S4 都出现过（改了 `container.py` 却没写进 Files）。

**(c) 文档行块。** 1.0 的行号引用（`db.py:4815` 这类）在本文档里出现几百次，
其中一部分是**从上一轮的计划抄过来的**，抄错一次就会让后来的人照着错的去读。
处置：**每一处行号都在本轮重新 `sed -n` 读过一遍**；`s5-deviations.md` 证据 18/19
记的就是上一轮抄错的两处（`retrieval_indexes.py:150` → 实际 `:20,180`；
`db.py:3439-3453` → 实际 `:3433-3447`）。

**(d) 每条 `Run` 真跑。** 每个 `**Run:**` 块下面必须紧跟一段**非空**的原文输出
（红灯 / 绿灯 / `make ci`），由 `expand.py` 从 `/home/asus/s5verify/logs/` 的日志文件
读进来，**不做裁剪与美化**。日志文件本身留在隔离副本的 `logs/` 下（含被 SIGKILL 的那几次）。

### 每 commit 一次完整 `make ci`

已落地的每个任务一个 commit，每个 commit 跑一次完整 `make ci`（用 `ci.sh`）。
**假红灯不许用来改代码**——判据与它自己被实测挡下一次的记录见「顾虑的处置」顾虑①。

## 逐任务增量验证表（红 / 绿两层）

**已落地的五个任务**（本轮实际执行到 Task 73；Task 74–87 仍是计划，见下一节）。
每一行的数字都来自 `logs/` 下的原始日志，不是估算。

| 任务 | commit | 门禁文件 | 条数 | 红灯 A 形态 | 红灯 B（定点变异） | 绿灯 | 累计 passed | `make ci` |
|---|---|---|---|---|---|---|---|---|
| 69 | `2824a5f` | `unit/test_identity_faces.py` | 62 | 收集期 `ModuleNotFoundError` | 32 处变异，31 处被杀（见下） | `62 passed` | 700 → **762** | 一次过 |
| 70 | `b18c571` | `integration/test_identity_schema.py` | 10 | **断言级**（10 条 FAILED，指向具体表/列） | 未做 | `10 passed` | 762 → **772** | 一次过 |
| 71 | `67030f0` | `integration/test_identity_repositories.py` | 19 | 收集期 `ModuleNotFoundError` | 未做 | `19 passed` | 772 → **791** | 一次过 |
| — | `e7a7dcf` | `unit/test_module_map.py` +2 / `unit/test_reachability.py` +6 | 8 | — | — | `13 passed`（连同既有） | 791 → **799** | 一次过 |
| 72 | `3e469ab` | `integration/test_enroll_face.py` | 22 | 收集期 `ModuleNotFoundError` | **3 处**，各指名一条用例 | `22 passed` | 799 → **821** | 一次过 |
| 73 | `5c30554` | `integration/test_review_cluster.py` | 23 | 收集期 `ModuleNotFoundError` | **3 处**，各指名 1–3 条用例 | `23 passed` | 821 → **844** | **run0 被 SIGKILL，run1 绿** |
| — | `ee2ddad` | `unit/test_reachability.py` +2 | 2 | — | — | `8 passed` | 844 → **846** | 一次过 |

**两条如实说明**：

1. **Task 69/71/72/73 的红灯 A 都是收集期 `ModuleNotFoundError`。** 从零新建一个模块时
   这是 TDD 的天然红灯——模块不存在就不可能有断言层失败。它证明的是
   **「测试文件先于实现落地」**，**不证明任何一条断言有牙齿**。
   **Task 70 是唯一一个红灯 A 就到断言层的**（它的实现是一份 `.sql`，测试文件先落地时
   schema 是空的，10 条断言逐条 FAILED，失败信息直接指向缺哪张表、哪个列）。
2. **红灯 B 是从 Task 72 起才有的纪律**（Task 69 例外，它做过一轮 32 处的变异检查）。
   六条红灯 B 的原文在 `logs/task72-redB.txt` / `task73-redB.txt`，
   逐条对应哪一行变异、哪条用例红了，见 `s5-deviations.md` §2e 的表。
   **没有红灯 B 的任务，其门禁的「牙齿」只有间接证据**（零成本算术不变量 + 逐条对回 1.0 的
   符号表），这一点不掩饰。

**零成本算术不变量逐条成立**：

```
700 + 62 = 762   (Task 69)
762 + 10 = 772   (Task 70)
772 + 19 = 791   (Task 71)
791 +  8 = 799   (checkers 补洞)
799 + 22 = 821   (Task 72)
821 + 23 = 844   (Task 73)
844 +  2 = 846   (check_reachability 收紧)
```

每一次的「本次 passed − 上次 passed」都**恰好等于该次新增的绿灯条数**，
没有一条用例是在别处被悄悄删掉来凑数的。

---

## 跨任务回归

**在终态树上（`ee2ddad`）把全部 S5 门禁一起重跑**——不是分别跑，是一次跑完，
用来抓「单个任务绿、合起来红」那种只在组合下出现的问题：

```bash
$ .venv/bin/pytest \
    apps/api/tests/unit/test_identity_faces.py \
    apps/api/tests/unit/test_reachability.py \
    apps/api/tests/unit/test_module_map.py \
    apps/api/tests/integration/test_identity_schema.py \
    apps/api/tests/integration/test_identity_repositories.py \
    apps/api/tests/integration/test_enroll_face.py \
    apps/api/tests/integration/test_review_cluster.py
151 passed, 2 warnings in 13.15s
```

`151 = 62 + 8 + 7 + 10 + 19 + 22 + 23`，与逐任务条数之和相等 ✔

**还有一层组合测试是整跑 `make ci` 自带的**：终态 `846 passed` 包含全部 S5 门禁，
且 import-linter 的 5 条契约、`check_module_map`、`check_capability_map`、
`check_reachability`、`export_openapi --check` 全在这一跑里。

---

## 终态 `make ci` 原始输出

在终态提交 `ee2ddad` 上跑 `bash /home/asus/s5verify/ci.sh`，判据判 `CI_OK`，
一次通过、没有 SIGKILL、没有抖动：

```text
$ bash /home/asus/s5verify/ci.sh
### 环境就绪: load1=0.43 MemAvailable=18GB
### make ci [run0] -> /home/asus/s5verify/logs/ci-20260923-124140-run0.log
### 运行前: load1=0.43 MemAvailable=18GB
### 运行前: load1=0.43 MemAvailable=18GB
.venv/bin/ruff check apps/api/src apps/api/tests scripts
All checks passed!
.venv/bin/ruff format --check apps/api/src apps/api/tests scripts
194 files already formatted
.venv/bin/mypy
Success: no issues found in 121 source files
.venv/bin/lint-imports

╔══╗─────────▶╔╗ ╔╗      ╔╗◀───┐
╚╣╠╝◀─────┐  ╔╝╚╗║║────▶╔╝╚╗   │
 ║║   ╔══╦══╦╩╗╔╝║║  ╔╦═╩╗╔╝╔═╦══╗
 ║║╔══╣╔╗║╔╗║╔╣║ ║║ ╔╬╣╔╗║║ ║│║╔═╝
╔╣╠╣║║║╚╝║╚╝║║║╚╗║╚═╝║║║║║╚╗║═╣║
╚══╩╩╩╣╔═╩══╩╝╚═╝╚═══╩╩╝╚╩═╩╩═╩╝
  └──▶║║                    ▲ 
      ╚╝────────────────────┘


---------
Contracts
---------

Analyzed 152 files, 621 dependencies.
-------------------------------------

domain-purity KEPT
application-purity KEPT
api-transport-only KEPT
infrastructure-purity KEPT
layers KEPT

Contracts: 5 kept, 0 broken.
.venv/bin/python scripts/check_reachability.py
reachability: 0 个问题
.venv/bin/python scripts/check_capability_map.py
capability-map: 0 个问题
.venv/bin/python scripts/check_module_map.py
module-map: 0 个问题
.venv/bin/python scripts/export_openapi.py --check
openapi 契约一致
.venv/bin/pytest --cov --cov-report=term-missing
........................................................................ [  8%]
......sssssssssssss........sss.......................................... [ 16%]
........................................................................ [ 25%]
........................................................................ [ 33%]
........................................................................ [ 41%]
........................................................................ [ 50%]
........................................................................ [ 58%]
........................................................................ [ 66%]
........................................................................ [ 75%]
........................................................................ [ 83%]
........................................................................ [ 91%]
......................................................................   [100%]
=============================== warnings summary ===============================
.venv/lib/python3.11/site-packages/fastapi/testclient.py:1
  /home/asus/s5verify/repo/.venv/lib/python3.11/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

.venv/lib/python3.11/site-packages/starlette/testclient.py:53
  /home/asus/s5verify/repo/.venv/lib/python3.11/site-packages/starlette/testclient.py:53: DeprecationWarning: The anyio.abc.BlockingPortal alias is deprecated, use anyio.from_thread.BlockingPortal instead.
    _PortalFactoryType = Callable[[], AbstractContextManager[anyio.abc.BlockingPortal]]

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
================================ tests coverage ================================
_______________ coverage: platform linux, python 3.11.16-final-0 _______________

Name                                                                 Stmts   Miss  Cover   Missing
--------------------------------------------------------------------------------------------------
apps/api/src/sentrix/application/__init__.py                             0      0   100%
apps/api/src/sentrix/application/assistant/__init__.py                   0      0   100%
apps/api/src/sentrix/application/assistant/goal_planner.py              43      0   100%
apps/api/src/sentrix/application/assistant/judge.py                     20      0   100%
apps/api/src/sentrix/application/assistant/manage_conversations.py      40      0   100%
apps/api/src/sentrix/application/assistant/run_turn.py                 231      9    96%   201, 399-400, 507-508, 581, 595-597
apps/api/src/sentrix/application/assistant/tools.py                    131     13    90%   72, 138, 141-142, 187, 204-206, 255-258, 282
apps/api/src/sentrix/application/identity/__init__.py                    0      0   100%
apps/api/src/sentrix/application/identity/enroll_face.py               113      2    98%   218, 331
apps/api/src/sentrix/application/identity/review_cluster.py            132      6    95%   173, 175, 214-216, 242
apps/api/src/sentrix/application/media/__init__.py                       0      0   100%
apps/api/src/sentrix/application/media/import_album.py                  70      0   100%
apps/api/src/sentrix/application/media/import_assets.py                 90      0   100%
apps/api/src/sentrix/application/media/index_place.py                   16      1    94%   34
apps/api/src/sentrix/application/media/observe_asset.py                 15      2    87%   27, 30
apps/api/src/sentrix/application/memories/__init__.py                    0      0   100%
apps/api/src/sentrix/application/memories/assign_events.py             102      5    95%   97, 105, 109, 196, 199
apps/api/src/sentrix/application/memories/maintain_facts.py             40      0   100%
apps/api/src/sentrix/application/memories/read_timeline.py              94      4    96%   145, 175, 178, 191
apps/api/src/sentrix/application/memories/summarize_event.py           103      1    99%   240
apps/api/src/sentrix/application/retrieval/__init__.py                   0      0   100%
apps/api/src/sentrix/application/retrieval/index_visual.py              36      0   100%
apps/api/src/sentrix/application/retrieval/retrieve_evidence.py         53      0   100%
apps/api/src/sentrix/application/retrieval/retrieve_lexical.py          26      0   100%
apps/api/src/sentrix/application/retrieval/retrieve_metadata.py         54      4    93%   92, 94, 114, 139
apps/api/src/sentrix/application/retrieval/retrieve_visual.py           80      2    98%   183-184
apps/api/src/sentrix/domain/__init__.py                                  0      0   100%
apps/api/src/sentrix/domain/assistant/__init__.py                        0      0   100%
apps/api/src/sentrix/domain/assistant/emergency.py                      27      3    89%   27, 45, 76
apps/api/src/sentrix/domain/assistant/evidence_ledger.py                63      1    98%   77
apps/api/src/sentrix/domain/assistant/final_guard.py                   110      1    99%   249
apps/api/src/sentrix/domain/assistant/intent.py                        121      6    95%   173, 207, 247, 284, 304-309
apps/api/src/sentrix/domain/assistant/judge.py                          77      0   100%
apps/api/src/sentrix/domain/assistant/models.py                        133      0   100%
apps/api/src/sentrix/domain/assistant/policy.py                          9      0   100%
apps/api/src/sentrix/domain/assistant/ports.py                          16      0   100%
apps/api/src/sentrix/domain/assistant/profile.py                        11      0   100%
apps/api/src/sentrix/domain/assistant/result_set.py                     46      1    98%   94
apps/api/src/sentrix/domain/assistant/task_state.py                     84      0   100%
apps/api/src/sentrix/domain/assistant/tool_policy.py                    53      1    98%   50
apps/api/src/sentrix/domain/assistant/tools.py                          25      0   100%
apps/api/src/sentrix/domain/identity/__init__.py                         0      0   100%
apps/api/src/sentrix/domain/identity/faces.py                          141      0   100%
apps/api/src/sentrix/domain/identity/models.py                         138      0   100%
apps/api/src/sentrix/domain/identity/ports.py                           32      0   100%
apps/api/src/sentrix/domain/media/__init__.py                            0      0   100%
apps/api/src/sentrix/domain/media/models.py                             92      6    93%   41-43, 62, 137, 142
apps/api/src/sentrix/domain/media/ports.py                              32      0   100%
apps/api/src/sentrix/domain/memories/__init__.py                         0      0   100%
apps/api/src/sentrix/domain/memories/events.py                         166      4    98%   143, 183, 259, 263
apps/api/src/sentrix/domain/memories/facts.py                           54      0   100%
apps/api/src/sentrix/domain/memories/ports.py                            8      0   100%
apps/api/src/sentrix/domain/places/__init__.py                           0      0   100%
apps/api/src/sentrix/domain/places/aliases.py                           24      1    96%   44
apps/api/src/sentrix/domain/places/matching.py                          22      1    95%   51
apps/api/src/sentrix/domain/places/models.py                            70      0   100%
apps/api/src/sentrix/domain/places/ports.py                             13      0   100%
apps/api/src/sentrix/domain/retrieval/__init__.py                        5      0   100%
apps/api/src/sentrix/domain/retrieval/fusion.py                         32      0   100%
apps/api/src/sentrix/domain/retrieval/lexical.py                        67      1    99%   75
apps/api/src/sentrix/domain/retrieval/models.py                         21      1    95%   28
apps/api/src/sentrix/domain/retrieval/ports.py                          55      0   100%
apps/api/src/sentrix/domain/retrieval/scheme.py                          5      0   100%
apps/api/src/sentrix/domain/retrieval/similarity.py                     18      0   100%
--------------------------------------------------------------------------------------------------
TOTAL                                                                 3259     76    98%
Required test coverage of 85.0% reached. Total coverage: 97.67%
846 passed, 16 skipped, 2 warnings in 124.14s (0:02:04)
cd apps/web && npx tsc -b
cd apps/web && npx oxlint
Found 0 warnings and 0 errors.
Finished in 24ms on 11 files with 116 rules using 32 threads.
cd apps/web && npx vite build
vite v8.3.0 building client environment for production...
transforming...
✓ 24 modules transformed.
rendering chunks...
computing gzip size...
dist/index.html                   0.45 kB │ gzip:  0.29 kB
dist/assets/index-DZTshfUF.css    2.11 kB │ gzip:  0.83 kB
dist/assets/index-hZ30bQL6.js   230.98 kB │ gzip: 72.69 kB

✓ built in 61ms
ci: 全部通过
### exit=0
CI_OK /home/asus/s5verify/logs/ci-20260923-124140-run0.log
```

（上面是日志全文，包含 import-linter 的 ASCII 头、coverage 的逐文件表与 web-check 的
vite 输出——**没有裁剪**。原始文件在隔离副本的 `logs/ci-20260923-124140-run0.log`。）

---

## 用户裁决（2026-09-23）与三条按证据定的

第一轮在 Task 69 落地后按「超过约 15 个任务就停下报告」的约定停在第 18 个任务处，
提出 7 条待裁决。**用户已裁决**，本节是记录；逐条的依据与代价见
`docs/refactor/s5-deviations.md` §2c。

### 四条裁决

| # | 问题 | 裁决 | 对本计划的影响 |
|---|---|---|---|
| ① | 计划做多大 | **先 S5a + S5b**（Task 69–84）；**S5c 这次不做**，另写 | 本文档「阶段 3–5」的执行范围 = S5a + S5b |
| ② | 画像三张表的冻结 | **解冻 `person_portrait_revisions` + `person_moments`；`person_insight_runs` 仍冻结** | 迁移 0006 建两张、不建那张；`frozen.md` 与 `capability-map.jsonl` 相应改记（Task 70 已落地） |
| ③ | 全局重聚类 | **搬，作为显式的维护入口**（维护端点或命令，**不自动跑**） | **新增一个任务**（Task 84），S5b 从 7 个任务变成 8 个；S5c 编号顺延为 85–87 |
| ④ | facts 候选来源 | **按 1.0 原样：模型自报**（`analyze_image` 输出里的 `facts` 数组） | Task 82 的候选来源从「待裁决」变成「照搬 1.0」；**推翻 `s4-deviations.md` 第 30 条的记法**（已就地改正并注明依据） |

**③为什么是「搬」而不是「按证据走」**：证据面没有变——`recluster_faces` 的生产调用者
仍然只有 `scripts/benchmarks/` 的两个脚本（`s5-deviations.md` 证据 4 成立）。
裁决改变的是**结论**：不搬的代价是「聚类质量差了没有补救手段」，用户不接受。
做法是把那条只在 benchmark 里的路径**显式做成维护入口**——**不是恢复自动调用**。
这句话要跟着 Task 84 的验收走：**门禁里必须有一条「这条路径不会在导入时被自动触发」**。

**④推翻的那条**：`s4-deviations.md` 第 30 条原文结尾写「事实由 `maintain_fact` 规则决定，
**不由模型自报（S5）**」。括号里的「（S5）」这个去向记法作废，依据是用户裁决。
第 30 条的**主体不变**（2.0 的观察器确实不搬 semantic 受控词表与 `detail` 的四个字段）。

### 三条按证据定的（上一轮已定，本轮照办）

| 事项 | 结论 | 依据 |
|---|---|---|
| AdaFace 要不要 | **按实测走 buffalo_l / `w600k_r50`（512 维）** | 证据 1/2/3：AdaFace 在 1.0 是死代码，1.0 自己的测试标了 `skip` |
| 全局重聚类自动调用 | **不做**（但按裁决③做维护入口） | 证据 4：调用点全在 benchmarks。**与裁决③不冲突**——要的是显式入口，不是自动触发 |
| `entity` 检索通道归属 | **留在 S5b**（Task 83） | `s4-implementation-plan.md:1632` 记在 S5/S6；`person_bridge` 的写侧依赖 S5 的 mentions |

### 编号变更（裁决③的后果，如实登记）

第一轮的编号是 S5a = 69–76、S5b = 77–83、S5c = 84–86。
裁决③要求新增「全局重聚类的维护入口」，它不属于 69–83 里的任何一个任务，因此：

| 子切片 | 第一轮 | 现在 |
|---|---|---|
| S5a | 69–76 | **69–76**（不变） |
| S5b | 77–83 | **77–84**（新增 Task 84 = 维护入口） |
| S5c | 84–86 | **85–87**（外观证据 / 人物画像 / e2e，顺延） |

---

## 顾虑的处置（第一轮报告的五条）

### 顾虑① · `make ci` 在 153 上不稳 → **已固化成判据脚本**

**现象（实测）**：基线那一跑 `coverage` 被**信号 9** 杀掉
（`make: *** [Makefile:51: coverage] 已杀死`，`EXIT=2`），另有两次 e2e 抖动
（`e2e/test_assistant_turn.py` 的两条各失败一次），而**单独重跑该文件是 `6 passed`**。
根因是同机的资源竞争（1.0 的 8091 RSS 11.7 GB、vLLM 8500、photobench runner、
2.0 的 9598/4274 同时在这台机器上），**不是代码缺陷**。

**「每个 commit 跑一次完整 `make ci`」这条要求如果不配判据，就会拿假红灯去改代码**——
而那正是 S2/S3 反复栽过的那类错误。所以判据不写在人脑里，固化成一个脚本
`/home/asus/s5verify/ci.sh`，顺序执行六条（完整规则与它自己被实测挡下一次的记录见
`s5-deviations.md` §2c.4）：

1. 跑之前看环境：1 分钟负载 ≤ 2.0 且 `MemAvailable` ≥ 12 GB，否则**不跑**、等窗口；
2. 原始输出整份落 `logs/ci-<时间戳>-run<N>.log`，跑几次留几次；
3. 绿 → `CI_OK`；
4. 红且带 `已杀死 / Killed / SIGKILL` 指纹 → 判**疑似假红**，重新等窗口重跑，最多 2 次；
5. 红且是测试失败 → **先单独重跑那些失败文件**，单跑绿 → 判抖动、整跑重试；单跑也红 → 真红；
6. **红灯未过第 4/5 条时，不改代码。**

### 顾虑② · S5b 的三个重型依赖 + GPU → **实测可行，但要先做一次可回滚的试装**

**第一轮的顾虑是「GPU 只剩 11 GB、1.0 的 8091 还在跑」。实测这三个前提都要修正**：

| 项 | 第一轮记的 | 2026-09-23 实测 |
|---|---|---|
| GPU | 13124 MiB / 24576 MiB 已用（剩 11 GB） | **5856 MiB / 24576 MiB 已用（剩 18.7 GB）** |
| 内存 | 39 GB 已用 / 19 GB 可用 | 40 GB 已用 / **19 GB 可用**（不变） |
| 磁盘 | `/home/asus` 可用 1.1 T | 不变 |
| **装依赖会不会扰动 9598** | 未查 | **不会**：`readlink -f /home/asus/s5verify/repo/.venv` 是它自己的目录（不是符号链接），而 9598 的 `/proc/<pid>/cwd` 指向 `/home/asus/Github/sentrix-home-web2.0`，用的是**那个仓库的 venv**。两个 venv 互不相干 |

**所以 S5b 的依赖可以装**，但有一处**必须先验证再决定**的风险：
`onnxruntime-gpu` 与已有的 `torch 2.14.0` 要共处同一个 venv，两者的 CUDA/cuDNN 运行库
可能冲突。**处置**：Task 77 的第一步是一次**可回滚的试装**——
`pip install onnxruntime-gpu insightface opencv-python-headless`，
装完立刻跑 `.venv/bin/python -c "import torch, onnxruntime"` 与既有的
`tests/integration/test_chinese_clip.py`；任何一处坏掉就 `pip uninstall` 回滚，
**并在本计划里把 S5b 标成阻塞**（不带着一个坏掉的 venv 往下做）。
**不预先把结论写成「能装」**——这一步没跑之前，它是待验证项。

另有一条**不能忘的**：新增依赖要进 `apps/api/pyproject.toml` 的 `dependencies`（照
`torch` / `cn-clip` 的写法带理由注释），否则全新克隆跑 `make install` 装不出来，
而那些在 `import` 期缺库的模块会让 `make ci` 在**收集期**就红。

### 顾虑③ · 人脸模型是进程内单例 → **设计确认：容器内单例、每进程一份，惰性加载**

第一轮把这条列成「需要设计确认」。**结论与理由如下**（这是本轮唯一一处设计决定）：

1. **用容器单例，不用模块级单例。** `bootstrap/container.py` 的第一句 docstring 就是
   「Nothing else in the codebase constructs an adapter」——本仓库的**唯一装配点**这条纪律。
   1.0 的 `app.py:61` 在 import 期构造 `FaceAdapter()` 是**模块级单例**，也就是**第二条装配路径**；
   照搬它，`tests/unit/test_container_wiring.py` 钉的那条不变量就失去意义。
2. **现成的同类先例**：`ChineseClipVisualEmbedder` 是**同一个形状**——进程内 GPU 推理、
   权重 1.6 GB、容器字段、每进程一份——它跑完了整个 S2–S4，没有任何问题。
   人脸适配器的差别只是它多一个 ONNX session。
3. **「每进程一份」的真实代价，在 2.0 的当前形态下是零**：实测 2.0 **没有任何**
   worker / 线程池配置（`grep -rn "MAX_WORKERS|ThreadPool|multiprocessing" apps/api/src/sentrix/`
   零命中），`make dev` 起的是单进程 uvicorn。第一轮提到的
   `SENTRIX_PIPELINE_MAX_WORKERS` 是 **1.0 的**设置，2.0 没有搬它。
4. **权重必须惰性加载。** `build()` 里构造的只是壳，权重在第一次 `detect()` / `embed()` 时才加载——
   这正是 1.0 的形状（`app.py:61` 构造、首次 `detect()` 才 `_load_buffalo_recognition`）。
   不照搬这一条，容器构造就会在**每次** `create_app()` 时把模型拉起来，而
   `make ci` 的契约测试会构造几十次 app。
5. **GPU 并发闸门是线程级的、进程内的**：1.0 的 `onnx_runtime.py:77`
   是 `threading.BoundedSemaphore(max(1, int(os.getenv("SENTRIX_FACE_GPU_MAX_CONCURRENCY", "1"))))`。
   照搬（`SENTRIX_FACE_GPU_MAX_CONCURRENCY` **是 1.0 真读的变量**，不像 `ADAFACE_*`
   那一串无人读取的），默认 1、8091 生产设 4。**这一条与「每进程一份」是自洽的**——
   闸门管的就是同一进程里的并发推理。

### 顾虑④ · `_seed_face_detect` 的两处 1.0 缺陷 → **按证据走，登记在 deviations**

| 缺陷 | 证据 | 处置 |
|---|---|---|
| 第三级「整图 embedding 兜底」是死路径 | `app.py:2296-2310` 调 `identity_adapter.embed`，而它恒 `None`（`model_clients.py:1629`）→ 该级**永远返回 `[]`** | **不搬这一级**。理由与 S4 的 FTS5 同类：不留一条永不执行的分支（已实测 FTS5 可用，就不留回退）。**不是**「照抄一条永远返回 `[]` 的路径」 |
| `seed_person_identity` 的 `m_role` 未定义 | `app.py:2349` 用了 `m_role`；`grep -n "m_role" app.py` 只有 `:2373`（在 `seed_persons_batch` 内）定义过它 → 走到 `return` 即 `NameError` | **改成 `family_role`**（形参名见 `app.py:2315-2320` 的签名）。这是把一处**静态可判的崩溃**修掉，不是设计选择 |

两条的完整依据在 `s5-deviations.md` 证据 8/9 与「第 5 条」。

### 顾虑⑤ · `module-map.md` 的重复行为什么没被校验抓到 → **是 checker 的洞，已定位并补检查**

**现象**：真实仓库 `bd3af31` 的 `docs/architecture/module-map.md` 第 66 / 67 行
**逐字节相同**（`md5sum` 相同），第一列都是
`sentrix/application/retrieval/retrieve_evidence.py`。全表 109 行、108 个唯一路径，
**这是唯一一处重复**。`check_module_map.py` 在「有重复」与「删掉重复」两种状态下都是
`0 个问题`。**来历**：`ee690e1`（Task 64）把它从 0 加到 1，**`9f14c5d`（Task 65）
从 1 加到 2**——手工编辑造成的，不是生成器的产物，随 `f663522` 带到了 `bd3af31`。

**根因不是「判据不覆盖重复行」，而是「判据的容器把重复行吃掉了」**，两处、各自独立：

1. **文件集合那一侧用的是 `set`。** `documented_paths()` 返回
   `{match.group(1) for line in ...}`——重复路径在集合里**自然只留一份**。而 `main()`
   的比对是 `in_code - in_doc` / `in_doc - in_code` 两个 `set` 做差集，
   **重复行在这个模型里根本不是一个可观测量**。
2. **依赖边那一侧用的是 `dict`。** `documented_edges()` 写 `deps[path] = parse_cell(...)`，
   **后一行覆盖前一行**。两条相同的行 → 覆盖后结果不变，`compare()` 无话可说。

**准确形状（两者不对称，这一点值得记）**：**逐字节相同的重复行完全不可见；
内容不同的重复行只能被依赖列那一半偶然抓到**（`dict` 覆盖后比的是最后一份）。

**为什么它属于「声称在检查、其实没有」那一族**：`test_module_map.py` 为这个脚本写了
**四条反向测试**（未记录模块 / `--src` 传错基准 / 依赖列写错 / 行少了列），
**唯独没有一条针对重复行**；而 `check_module_map.py` 的开头注释专门强调
「列数不足的行会被报错而不是跳过：跳过等于给依赖校验开后门」——它想到了「一行被整行跳过」，
**没想到「两行塌成一行」**。两者是同一个病的两个变体：
**校验模型里不存在的东西，就是没被校验的东西。**

**处置**（已在隔离副本实施）：在 `main()` 的路径比对之前加一条
「表格行数 == 唯一路径数」的断言并逐个报出重复的路径名；同时在 `test_module_map.py` 里
加一条**反向测试**（造一份含重复行的表，断言 checker 报红），保证这条检查自己也有门禁。
真实仓库那一行的删除随本计划的代码一起落地（**不单独改真实仓库的 `module-map.md`**，
理由与 `capability-map.jsonl` 相同：文档必须与代码同一次提交）。

---

## 仍需用户裁决的点

第一轮的 7 条已被裁决 4 条、按证据定 3 条。**当前没有新的待裁决项**，
新的发现（如 §2c.3 的第 40/41 条）都已按「报告 + 就地按既有纪律处置」处理，
处置依据逐条写在 `s5-deviations.md` 里。若执行中再出现需要用户点头的，追加在本节。