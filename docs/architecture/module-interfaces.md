# Sentrix 2.0 模块接口与代码修改规则

> 版本：2.0　|　日期：2026-10-20　|　适用分支：`psh2.0`
> **强制阅读：任何修改本项目代码的人，在动手前必须完整读完本文档。**
> 适用范围：新增功能、修 bug、重构、换模型、换嵌入、换存储、改评测、改前端契约——**没有例外**。

---

## 0. 先读这一页

### 0.1 为什么这份文档是强制的

2.0 的设计目标之一是“替换而生”：模型、嵌入、向量库、地理数据、评测器、存储都可能被换掉。但“能换”不等于“随便换”。一次不合格的改动通常长这样：

- 在 `application/` 里直接 `import` 了某个具体客户端；
- 在 `Container` 里写了具体类而不是端口类型；
- 新适配器失败时静默返回空列表，检索悄悄降级；
- 换了嵌入模型却没换索引的 `model_name`，旧向量和新查询混算；
- 改了 `run.json` 字段，前端和评测报告一起坏掉；
- 为了绕过一个模型问题，在出口加了一段清洗；
- 新增一个 `SENTRIX_FOO` 环境变量，让同一份代码在不同机器上跑出不同语义；
- 随手修改端口方法签名，调用方在运行时才炸。

这些问题的共同点是：**单看 diff 都“很小”，合进主干后却破坏了可替换性和可回归性。**

本文档给出**强制规则 + 端口清单 + 改动流程 + 验收标准 + 评审清单**。它约束的不是“替换模块”这一种改动，而是**所有改动**。

### 0.2 读到什么程度才算“读过”

你应当能回答：

1. 你的改动属于哪一层？依赖方向允许吗？
2. 你依赖的能力有没有端口？没有的话，应该先加端口还是先改调用方？
3. 你的失败语义是 fail-open 还是 fail-closed？与端口契约一致吗？
4. 你的改动需要补哪几层测试？需要更新哪些登记文件？
5. 你的改动会不会让 153（WeMM）和 130（legacy）跑出不同语义？如果会，是否是有意的方案切换？

### 0.3 三个角色怎么用

| 角色 | 先看 | 再查 |
|---|---|---|
| 替换实现的人 | §1 六步流程 | §3 端口表 → §4 对应能力 → §5 标准 |
| 评审者 | §5 验收矩阵 | §7 PR 清单 → §8 反模式 |
| 集成/运维 | §6 配置矩阵 | §4.12 评测侧 → §9 已知边界 |

### 0.4 术语

| 术语 | 含义 |
|---|---|
| **端口（Port）** | `domain/**/ports.py` 里的 `Protocol`，定义“能力要什么”，不定义“怎么实现” |
| **适配器（Adapter）** | `infrastructure/**` 里实现端口的类 |
| **装配点** | `bootstrap/container.py` 的 `Container.build()`，全仓唯一 new 适配器的地方 |
| **契约测试** | 验证适配器满足端口语义的测试（不是“能跑通”就算） |
| **行为基线** | `docs/refactor/behaviour-baseline.md`，外部可观察行为的权威记录 |
| **替换** | 保持端口不变，换一个适配器；**改端口不叫替换，叫接口变更** |

### 0.5 所有代码改动必须遵守的十条硬规则

> 下面每一条都是 **MUST / MUST NOT**。违反其中任何一条，PR 不应合并。

| # | 规则 | 为什么 | 谁在抓 |
|---|---|---|---|
| 1 | **MUST 保持依赖方向**：`domain` 不依赖框架/IO/上层；`application` 只依赖 domain 端口；`api` 只做传输；`infrastructure` 只实现端口。 | 一旦反向依赖，替换点消失，业务规则无法脱离框架测试。 | 11 条 `import-linter` 契约 |
| 2 | **MUST 只在 `bootstrap/container.py` 构造适配器**，且 `Container` 字段按端口类型声明。 | 替换点必须唯一、可见；写在 `build()` 里的能力调用方看不见。 | `test_container_wiring.py` |
| 3 | **MUST NOT 新增散落的 env flag**。只有“部署方决定、运行时会变”的事实才进 `Settings`；数据路径必须由 `SENTRIX_DATA_DIR` 派生。 | 1.0 的 126 个 env flag 互相矛盾，是主要故障源。 | Code Review + `config.py` |
| 4 | **MUST 端口先行**：缺能力先在 `domain/<area>/ports.py` 定义 `Protocol`，再实现适配器；**MUST NOT** 在 `application/` 直接 import infrastructure。 | 没有端口就没有替换能力，也没有契约测试的落点。 | `application-purity` + Review |
| 5 | **MUST 遵守端口的失败语义**：fail-open（返回 `None`/空表）与 fail-closed（抛异常）不得自行翻转；静默降级必须能在 `/health` 或日志里看见。 | 翻转会让导入整批失败，或让检索悄悄变空。 | 契约测试 + Review |
| 6 | **MUST NOT 擅自修改外部契约**：`/api/v1` schema、错误体 `{code,message,detail}`、SSE 事件、`run.json`/`results.jsonl` 字段。要改就走接口变更流程（§7.4），并同步 OpenAPI、前端 `types.ts` 与测试。 | 评测报告、前端、历史数据都依赖这些字段。 | contract 测试 + `check-docs` |
| 7 | **MUST NOT 增加出口清洗**。不得在最终答案输出前过滤 `photo_N`、工具名、字段名来“看起来正常”。 | 清洗会掩盖真实模型问题，违背“先定位根因再修”。 | Review（明确禁止） |
| 8 | **MUST NOT 让 benchmark 内容进入产品代码或提示词**：答案、题号、评测地标、固定人名不得硬编码。 | 会让评测失去意义，也会污染真实用户数据。 | `frozen.md` + Review |
| 9 | **MUST 保持顺序与幂等**：`prepare` 只算不写、`commit` 才写；事件归属按 `sorted(paths)` 提交；关系双边先 supersede 后插入；重复导入/重复调用必须幂等。 | 这些顺序不报错，但会静默产生重复事件、错分事件或错误关系。 | integration 测试 + Review |
| 10 | **MUST 完成登记并跑门禁**：新增/移动 `.py` 更新 `module-map.md`；新能力更新 `capability-map.jsonl`；偏离 1.0 写 `*-deviations.md`；公开 schema 变化重导 OpenAPI；最后 `make ci` 全绿。 | “代码能跑”不等于“改动完成”；登记缺失会让下一个人踩坑。 | `make ci` |

**一句话记法**：**依赖不许倒、装配只一处、配置不许散、端口先于实现、失败照契约、外部不许乱改、出口不许洗、评测不许漏、顺序不许换、登记不许少。**

---

## 1. 改动流程（六步）

这套流程对三类改动都适用：

| 改动类型 | 重点步骤 | 说明 |
|---|---|---|
| **A. 换/加外部能力**（模型、嵌入、OCR、存储、评测器） | 全部六步 | 必须走端口 + 装配 + 契约测试 |
| **B. 修 bug** | 第 1、4、5、6 步 | 先定位根因；不得用出口清洗或静默降级“修好” |
| **C. 新功能** | 第 1、4、5 步 | 先确认落层与端口，再写用例与测试；对外契约变化走接口变更流程 |

### 第 1 步：确认改动落在哪一层、依赖哪个端口

先查 §3 的端口表。如果目标能力没有端口，只有两种合法做法：

1. **先加端口**（推荐）：在对应 `domain/<area>/ports.py` 写 `Protocol`，把调用方改成依赖端口；
2. **确认它属于 application 编排**（如 `IndexAssetVisual`），那就换 application 用例内部的协作者，不要新造端口。

不允许的做法：在 `application/` 里直接实例化/导入 infrastructure 类。

### 第 2 步：写适配器

放在 `apps/api/src/sentrix/infrastructure/<area>/<name>.py`。

硬要求：

- 只实现端口方法，**不新增调用方会依赖的公开方法**；
- 不 import `api` / `application` / `bootstrap`；
- 构造参数通过 `__init__` 注入，不在模块级读环境变量；
- 外部 IO 必须设超时；
- 失败语义照端口契约（见 §5.3），不得自行把“抛异常”改成“返回空”。

### 第 3 步：在唯一装配点接线

只改 `apps/api/src/sentrix/bootstrap/container.py` 的 `build()`，并在 `Container` 字段上保持**端口类型**。

```python
# Container 字段（声明层）——必须是端口
chat_model: ChatModel

# build()（装配层）——这里才知道具体类
chat_model=MyChatModel(
    base_url=settings.vlm_base_url,
    timeout_s=settings.chat_timeout_s,
),
```

PhotoBench 侧同理，改 `apps/photobench/src/photobench/bootstrap/container.py`。

### 第 4 步：补测试

最低要求：

- **unit**：适配器纯逻辑、错误映射、边界条件；
- **integration**：真实后端契约（临时目录 / 本地 HTTP / 合成数据）；
- 若新实现暴露了新端点，补 **contract** 测试。

### 第 5 步：登记文档

| 改动 | 必须登记 |
|---|---|
| 新增/移动/删除 `.py` | `docs/architecture/module-map.md`（或 photobench 版） |
| 新增能力或改变能力状态 | `docs/refactor/capability-map.jsonl` |
| 偏离 1.0 行为 / 冻结某项 | `frozen.md` 或对应 `*-deviations.md` |
| 公开 API/schema 变化 | 重导 `packages/contracts/openapi/*.yaml` |

### 第 6 步：跑门禁

```bash
make ci
```

至少确认这几项单独通过：

```bash
make lint format-check typecheck arch photobench-check check-docs coverage
```

---

## 2. 不可谈判的架构约束

以下每一条都有门禁或测试兜底，不是“建议”。

| # | 约束 | 为什么 | 谁来抓 |
|---|---|---|---|
| 1 | `domain` 不依赖 fastapi/sqlite3/PIL/httpx/os/qdrant，也不依赖上层 | 业务规则必须能脱离框架测试 | `domain-purity` 契约 |
| 2 | `application` 不依赖 fastapi/sqlite3/PIL/httpx/infrastructure/api/bootstrap | 用例只编排端口 | `application-purity` |
| 3 | `infrastructure` 反向依赖 api/application/bootstrap 即失败 | 适配器不能反过来调用用例 | `infrastructure-purity` |
| 4 | `api` 只做传输映射 | 路由里不允许 SQL/文件/模型客户端 | `api-transport-only` |
| 5 | 只有 `Container.build()` 构造适配器 | 替换点必须唯一、可见 | `test_container_wiring.py` |
| 6 | `Container` 字段必须声明为端口类型 | 写在类型上的替换能力才算数 | 同上 |
| 7 | `photobench` 不得 import `sentrix` | 评测不能污染产品，只能走 HTTP | `photobench-isolation` |
| 8 | 禁止 benchmark 泄漏进 agent | 提示词/代码不得出现评测地名、答案、题号 | `frozen.md` + Review |
| 9 | 禁止出口清洗 | 清洗会掩盖真实模型问题，违反“先定位根因” | Review |
| 10 | 应用不管理模型进程生命周期 | 只经 HTTP 消费端点；不建 model-manager | s11 裁决 |
| 11 | 导入是同步长请求，没有任务队列 | 不假装异步、不藏进度 | s9 裁决 |
| 12 | 配置最小化：只有部署事实才进 `Settings` | 1.0 的 126 个 env flag 互相矛盾 | `config.py` + Review |

> **第 12 条的判据**：这个值是不是“部署方决定、运行时会变”的事实？是 → 可以进 `Settings`；只是代码常量 → 放 domain，不要加环境变量。

---

## 3. 端口总览

### 3.1 可替换性分级

| 级别 | 含义 | 典型端口 | 替换难度 |
|---|---|---|---|
| **A：Provider** | 设计上鼓励替换，换模型/服务/库 | `ChatModel`、`VlmObserver`、`VisualEmbedder`、`VectorIndex`、`AdminResolver` | 低–中 |
| **B：编排** | 端口在 domain，实现固定在 application，换的是内部协作者 | `AssetObserver`、`AssetVisualIndexer`、`FaceEnroller` | 中 |
| **C：仓储** | 语义固定，只允许换存储引擎，必须逐方法保持契约 | 全部 `*Repository` | 高 |
| **D：空端口** | 无实现/无调用，不要假设可用 | `ToolExecutor`、`MediaTypeResolver` | 先接线再说 |

### 3.1.1 新增端口的设计规范

当现有端口表里没有你要的能力时，按这五条设计：

1. **按能力命名，不按实现命名**：`TextEmbedder` 而不是 `BgeClient`；`PlaceResolver` 而不是 `PyGeoCNWrapper`。
2. **只读成员用 `@property`**：mypy 的 `arg-type` 不允许只读 property 满足可写变量协议。
3. **失败语义写进 docstring**：返回 `None`、返回空集合、抛哪个异常，三选一并写清楚。
4. **端口不放业务决策**：适配器只回答“是什么/怎么做”，不决定“要不要写库、要不要重试、算不算成功”。
5. **方法集合最小**：只放调用方真实需要的方法；未接线方法会被方法级可达性检查标出来。

端口文件必须放在 `domain/<area>/ports.py`，且不得 import 任何 infrastructure。

### 3.2 Assistant

| 端口 | 定义 | 方法摘要 | 当前实现 | Container 字段 |
|---|---|---|---|---|
| `ChatModel` | `domain/assistant/ports.py:74` | `complete(messages, tools) -> ModelReply` | `OpenAIChatModel` | `chat_model` |
| `ConversationRepository` | 同文件 `:84` | `create/get/list_for_scope/rename/delete/add_message/list_messages/history_for_prompt` | `SqliteConversationRepository` | `conversation_repository` |
| `TurnRepository` | `:111` | `create/get/claim/append_event/events_after/finish/list_for_conversation` | `SqliteTurnRepository` | `turn_repository` |
| `ResultSetRepository` | `:144` | `add/get` | `SqliteResultSetRepository` | `result_set_repository` |
| `PersonDirectory` | `:150` | `lookup(name, scope_id) -> PersonProfile \| None` | `EntityPersonDirectory` | `person_directory` |
| `TurnEventBroker` | `:161` | `open/publish/finish/subscribe` | `InProcessTurnEventBroker` | `turn_event_broker` |
| `ToolExecutor` | `:180` | `execute(call, arguments)` | **无实现/未装配** | — |

**关键契约**

- `ChatModel.complete` 必须抛 `ModelServiceError` 族；`PromptTooLongError` / `OutputTruncatedError` 是控制流，不是普通错误。
- `TurnRepository.claim` 是并发闸门：`pending → running` 只能有一个执行者抢到。
- `TurnEventBroker` 是进程内实时流；历史补偿必须由 `TurnRepository.events_after` 提供，两半合起来才是完整 SSE。

### 3.3 Media

| 端口 | 定义 | 方法摘要 | 当前实现 |
|---|---|---|---|
| `AssetRepository` | `domain/media/ports.py:71` | `add/get/list_by_scope/find_by_hash/update_status` | `SqliteAssetRepository` |
| `ObservationRepository` | `:89` | `add/get/list_by_asset/list_scope/enrich/latest_ocr_text/latest_people_snapshot` | `SqliteObservationRepository` |
| `VlmObserver` | `:184` | `model_id`、`observe(path) -> ObservationSemantics \| None` | `OpenAIVlmObserver` |
| `PhotoInspector` | `:201` | `model_id`、`inspect(path, question, faces, target_person, crop)` | `OpenAIPhotoInspector` |
| `AssetObserver` | `:242` | `prepare/commit/observe` | `ObserveAsset`（application） |
| `MediaStorage` | `:264` | `store/read/path_for` | `FileSystemMediaStorage` |
| `MetadataReader` | `:272` | `read(path) -> (time, gps, camera)` | `PillowMetadataReader` |
| `PreviewEncoder` | `:278` | `encode_jpeg/crop_jpeg` | `PillowPreviewEncoder` |
| `VideoMetadataReader` | `:297` | `read(path) -> VideoMetadata` | `FfprobeVideoMetadataReader` |
| `VideoTranscoder` | `:320` | `browser_preview(asset_id, source, codec) -> Path \| None` | `FfmpegVideoTranscoder` |
| `VideoKeyframeExtractor` | `:332` | `extract(source, asset_id, scene_index, timestamp_sec) -> Path` | `FfmpegVideoKeyframeExtractor` |
| `VideoRepository` | `:378` | `save_probe/save_processing_seconds/probe/list_derived/add_derived/cleanup_derivatives` | `SqliteVideoRepository` |
| `AudioMetadataReader` | `:452` | `read(path) -> AudioMetadata` | `FfprobeAudioMetadataReader` |
| `AudioRepository` | `:489` | `save_probe` | `SqliteAudioRepository` |
| `MediaTypeResolver` | `:293` | `resolve(content_type, file_name)` | **无实现/未装配** |

**关键契约**

- `VlmObserver.observe` 失败一律返回 `None`，**不抛**；导入不能因为观察失败而中断。
- `VideoMetadataReader.read` / `AudioMetadataReader.read` 失败必须抛 `ValueError`，调用方落 `video_probe_error`。
- `VideoTranscoder` 返回 `None` 表示“已是 H.264，无需转码”，不是失败。
- `VideoKeyframeExtractor.extract` 失败必须抛；半套场景不可接受。
- `AssetObserver` 的 `prepare` 只推理不写库，`commit` 才写库；顺序反了会产生重复事件/重复簇。

### 3.4 Retrieval

| 端口 | 定义 | 方法摘要 | 当前实现 |
|---|---|---|---|
| `LexicalIndex` | `domain/retrieval/ports.py:76` | `refresh_from_observation/rebuild_scope/count` | `SqliteLexicalIndex` |
| `LexicalSearch` | `:93` | `search_fts/exact_assets/assets_for_person` | 同一实例 |
| `VectorRepository` | `:113` | `upsert/has_vector/update_metadata/get/search_exact/count` | `SqliteVectorRepository` |
| `VectorIndex` | `:168` | `upsert/search/count/status` | `QdrantVectorIndex` |
| `AssetVisualIndexer` | `:218` | `prepare/commit/index` | `IndexAssetVisual`（application） |
| `VisualEmbedder` | `:234` | `model_id`、`dimension`、`embed_image`、`embed_query` | `WeMMSidecarEmbedder` |
| `TextEmbedder` | `:255` | `model_id`、`dimension`、`embed_query` | 同一 WeMM 实例 |
| `StructuredQueryRepository` | `:277` | `count/captured_bound/group_counts/sample_observations` | `SqliteStructuredQueryRepository` |
| `EmbeddingHealthProbe` | `:322` | `available`、`load_error` | 同一 WeMM 实例 |

**关键契约**

- `VectorRepository` 是**权威存储**，写入必须先到它；`VectorIndex` 是派生镜像，可以缺席。
- `VectorIndex` 不可用时抛 `VectorIndexUnavailable`，**禁止静默返回空列表**；由 application 决定回退并上报 `degraded`。
- `model_id` / `dimension` 是索引兼容性依据：换模型必须视为换索引空间。
- `visual_embedder` 与 `text_embedder` 当前指向同一实例；拆成两个实例前要先想清楚显存与熔断计数。

### 3.5 Places

| 端口 | 定义 | 方法摘要 | 当前实现 |
|---|---|---|---|
| `AdminResolver` | `domain/places/ports.py:22` | `resolve_admin(gps) -> AdminArea \| None`、`available` | `PyGeoCNAdminResolver` |
| `NearestResolver` | `:32` | `resolve_nearest(gps) -> NearestPlace \| None`、`available` | `GeoNamesNearestResolver` |
| `AssetPlaceRepository` | `:41` | `save/get/get_many/list_scope/count` | `SqliteAssetPlaceRepository` |
| `AssetPlaceIndex` | `:59` | `index(asset) -> bool` | `IndexAssetPlace`（application） |
| `PlaceResolver` | `:69` | `resolve(gps) -> Place \| None` | `OfflinePlaceResolver`（组合） |

**关键契约**

- 行政区（面）与最近地点（点）是两槽，**不可互换**；
- 纯离线、零网络；数据源缺席返回 `None`，不抛、不逐条重试；
- `AssetPlaceRepository.save` 幂等：同一 asset 覆盖不追加。

### 3.6 Memories / Identity / Spaces

| 端口 | 定义 | 方法数 | 当前实现 |
|---|---|---|---|
| `EventRepository` | `domain/memories/ports.py:13` | 15 | `SqliteEventRepository` |
| `EventParticipantRepository` | `:135` | 5 | `SqliteEventParticipantRepository` |
| `FactRepository` | `:219` | 8 | `SqliteFactRepository` |
| `MemoryGraphRepository` | `domain/memories/ports.py` | 4 | `SqliteMemoryGraphRepository` |
| `CausalJudge` | `domain/memories/ports.py` | `judge(prompt, expected_count)` | `OpenAICausalJudge`（8100 文本判定，失败抛异常） |
| `FaceDetector` | `domain/identity/ports.py:55` | `detect` / `detect_identity_photo` | `FaceAdapter` |
| `FaceEnroller` | `:88` | `prepare/commit` | `EnrollFaces`（application） |
| `FaceRepository` | `:105` | 28 | `SqliteFaceRepository` |
| `EntityRepository` | `:373` | ~25 | `SqliteEntityRepository` |
| `PortraitRepository` | `:687` | 6 | `SqlitePortraitRepository` |
| `MomentRepository` | `:738` | 2 | `SqliteMomentRepository` |
| `RelationshipRepository` | `:786` | 6 | `SqliteRelationshipRepository` |
| `PersonCropRenderer` | `:849` | `render` | `PillowPersonCropRenderer` |
| `PersonPreviewRenderer` | `:870` | `render` | `PillowPersonPreviewRenderer` |
| `PersonVision` | `:891` | 4 | `OpenAIPersonVision` |
| `AppearanceRepository` | `:935` | 3 | `SqliteAppearanceRepository` |
| `SemanticRepository` | `:976` | 3 | `SqliteSemanticRepository` |
| `FamilyGraphRepository` | `:1039` | ~16 | `SqliteFamilyGraphRepository` |
| `SpaceRepository` | `domain/spaces/ports.py:19` | `ensure/list_all/get` | `SqliteSpaceRepository` |

**必须保持的顺序/事务语义**

| 端口 | 规则 |
|---|---|
| `EventParticipantRepository.rehome` | 必须在事件合并的 `DELETE` 之前完成（FK） |
| `FamilyGraphRepository.set_relationship` | 先 supersede 旧边，再一次写正反两行 |
| `FamilyGraphRepository` locked | 遇到 `locked=1` 静默跳过，不报错、不覆盖 |
| `EntityRepository` | 派生值永不覆盖用户值 |
| `VideoRepository.save_probe` | 必须在资产行落库之后 |
| `VideoRepository.cleanup_derivatives` | 先删随行，再删主行 |
| `VectorRepository.update_metadata` | 整份替换，避免并发写入互相覆盖 |
| `FaceEnroller.commit` | 匹配建簇必须在 commit 内完成（要读同批刚写的簇） |

**结构化记忆图的独立接入约束**

- 记忆生成只用 2.0 原有流水线。`BuildMemoryGraph` 消费 `AssetRepository`、`ObservationRepository`、`EventRepository`、参与者、实体、事实、关系及权威 `VectorRepository` 端口；不读取关键帧文件、Qdrant payload 或旧版 `backend` 存储。视觉近邻只比较同一模型/维度的向量，top-3 互选且余弦 ≥0.80。
- 关键帧时序、同视频对象/关系边（高频阈值 30 转枢纽；关系窗口 60 秒）和地点/日期上下文边来自标准来源字段。因果/视觉推断边不算进确定性“可验边”的参考集；参考边须由节点来源字段独立重建，不能从已落库的边复制。无人工边 GT 时 P/R/F1 只能叫 source-consistency，不能叫真实图准确率。
- 因果候选只从同一源视频的关键帧观察产生：最多后继 2 帧、30 秒；规则要求稳定场景和关系变化，文本判定器只接收结构信号，不接图片。当前 Observation 无 bbox 时按 hpq 的 label-only 回退，不得把时间邻接当成因果边。
- `CausalJudge` 通过独立端口装配，共用 8100 的模型名探测与超时；失败抛异常，图构建用例保留其它派生边并报告 `causal_status=degraded`。`LEADS_TO` / `ENABLES` 带置信度、理由、推断来源；检索只使用置信度 ≥0.60 的边，默认沿因到果，明确询问原因时可逆向追溯，但不得把逆向遍历描述为“结果导致原因”。
- 图是 SQLite 中的可重建派生投影，按 `scope_id` 原子替换；权威记忆仍在标准记忆表里，图构建失败不得阻断导入主链路。
- `RetrieveGraph` 是独立的 `graph` 拓展通道：先用 primary 召回结果作种子，再按 object/action/temporal/scene/causal/multi-hop 意图做最多 4 hop 的有界遍历；明确的“之前/之后”问句决定时序方向，“为什么”可沿因果边反查原因。没有明确 scope 或图尚未构建时必须在 channel trace 中显式降级。
- 冻结的 `adjacency` 通道不被重新启用；`graph` 只在应用层覆盖新能力的 0.5 权重，不修改 `DEFAULT_CHANNEL_WEIGHTS` 行为基线。

### 3.7 PhotoBench

| 端口 | 定义 | 方法摘要 |
|---|---|---|
| `AlbumSource` | `domain/datasets/ports.py:33` | `manifest/qa_rows/read_file/import_target/integrity/list_album_ids/media_file` |
| `QaMetadataSource` | `:18` | QA 元数据读取 |
| `SentrixClient` | `domain/sentrix/ports.py:59` | 主产品 HTTP 语义端口（`API_PREFIX="/api/v1"`） |
| `JsonHttpClient` | `domain/http/ports.py:61` | 原始 JSON HTTP |
| `InferenceProvider` | `domain/runtime/ports.py:37` | `health/list_models/chat/chat_stream/token_count/capabilities/max_concurrency` |
| `LifecycleProvider` | `:75` | `profiles/start/stop/state` |
| `TelemetryProvider` | `:88` | `gpu_stats/process_memory/kv_cache/system_memory` |
| `SentrixProcess` | `domain/deploy/ports.py:55` | `running_endpoint/restart` |
| `RunStore` | `domain/results/ports.py:26` | 运行索引读写 |
| `RunResultWriter` | `:65` | `run.json/results.jsonl/gpu_samples.jsonl` 写面 |
| `ConfigStore` | `domain/config/ports.py:27` | 评测配置读写 |
| `SecretEnvStore` | `:51` | 密钥环境变量 |

---

## 4. 逐能力替换指南

### 4.1 对话 / 文本模型（最常换）

**端口**

```python
class ChatModel(Protocol):
    def complete(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
    ) -> ModelReply: ...
```

**必须保持的行为**

| 项 | 要求 |
|---|---|
| 消息顺序 | system 必须是且只能是第一条 |
| 工具调用 | 原生 `tools/tool_calls`，不接受正文 JSON |
| 思考通道 | 剥离 `<|channel>thought` 等内容 |
| 温度 | `temperature=0` |
| 流式 | 非流式（当前实现 `stream=False`） |
| 错误映射 | `ModelServiceError` / `PromptTooLongError` / `OutputTruncatedError` |
| 重试 | 适配器内部不重试 4xx；404 可重新探测一次模型名 |
| 预算 | 生成上限 2048、最小可用 256；预算不足不发请求 |
| 模型名 | 由 `ServedModel` 探测 `/v1/models`，禁止写死 |
| 上下文窗口 | 优先 `probe_context_tokens`，失败才用 `SENTRIX_VLM_CONTEXT_TOKENS` |

**替换步骤**

1. 新建 `infrastructure/assistant/my_chat_client.py`，实现 `complete`；
2. 保留 `ServedModel` 探测与 `probe_context_tokens`（否则换端点即 404）；
3. 改 `Container.build()` 的 `chat_model=`；
4. 补 `tests/integration/test_chat_client.py` 等价用例；
5. 用 487qa 的 reuse 模式跑一轮，对比 `parse_failure`、`step_budget`、`agent_task_latency`。

**常见坑**

- 直接把模型名写成常量 → 换部署即 404；
- 在适配器里做“JSON 修复” → 2.0 已改为原生工具调用，修复器会掩盖协议问题；
- 把 `PromptTooLongError` 吞掉 → 上层无法压缩历史重试，整轮变空答案。

---

### 4.2 VLM 观察

**端口**

```python
class VlmObserver(Protocol):
    @property
    def model_id(self) -> str: ...
    def observe(self, path: Path) -> ObservationSemantics | None: ...
```

**必须保持的行为**

- 返回固定七字段的 `ObservationSemantics`；
- 不猜姓名、不输出坐标、不做关系推断；
- 失败（端点不可达、非 JSON、读图失败）返回 `None`，不抛；
- 最多 3 次调用：首轮 → 缺自然语言补全 → 出现拉丁文则中文化；
- 单次超时 `SENTRIX_VLM_TIMEOUT_S`（默认 180s）。

**替换验收**

- [ ] 坏图、非 JSON、端点 500 三种失败都返回 `None`，导入继续；
- [ ] 观察结果能落 `observations`，且 `source_type="import"`；
- [ ] 视频关键帧走同一条观察链；
- [ ] 用 20 张黄金图对比 `caption/activity/place/people` 的非空率。

---

### 4.3 照片问询（`PhotoInspector`）

与 `VlmObserver` 的区别：观察是导入期固定 schema；问询是运行期“拿一个问题问一张图”。

**必须保持**

- `fail -> None`，不抛；
- 图序 = 语义：裁剪件在前、原图在后；
- `crop` 读不了时退回原图；
- 结果交给 application 判断，适配器不做业务决策。

---

### 4.4 人物视觉（`PersonVision`）

三条链路：`write_person_portrait`、`analyze_person_moments`、`analyze_person_appearance`。

**必须保持**

- 一切失败返回 `None`；
- 提示词在 domain/application，适配器不拼业务提示词；
- `clothing` 去重保序；
- 不在导入前置路径上阻塞主链路。

---

### 4.5 嵌入（`VisualEmbedder` / `TextEmbedder` / `EmbeddingHealthProbe`）

**端口**

```python
class VisualEmbedder(Protocol):
    @property
    def model_id(self) -> str: ...
    @property
    def dimension(self) -> int: ...
    def embed_image(self, path: Path) -> Sequence[float]: ...
    def embed_query(self, text: str) -> Sequence[float]: ...
```

**必须保持**

| 项 | 要求 |
|---|---|
| 维度 | 当前 2048，由 `domain/retrieval/scheme.py` 冻结；换维度=重建索引 |
| 失败 | 返回 `[]`，不抛；空输入不打网络 |
| 健康 | `available` 与 `load_error` 分离；连续 5 次失败熔断 30s |
| 降级可见 | `/api/v1/health` 输出 `embeddings` / `embeddings_error`，状态是 `degraded` |
| 实例 | 视觉与文本当前共用同一实例，避免显存翻倍与熔断计数漂移 |

**换嵌入模型的完整步骤**

1. 确认新模型维度与归一化方式；
2. 更新 `scheme.py` 的 `model_id`/维度（这会让旧向量自然失效）；
3. 实现适配器，保持 `embed_image` / `embed_query` 同一空间；
4. 装配 `visual_embedder` / `text_embedder` / `embedding_health`；
5. **重建向量**：`vector_repository` 与 `vector_index` 都要按新 `model_name` 重写；
6. 跑检索回归：macro/micro 召回、R@1/R@5、视频召回。

> 不清空旧向量的后果：查询向量与库内向量不同空间，分数看似正常但全是噪声。

#### 4.5.1 当前的两个具名方案

| 方案 | 视觉 | 文本 | 适用 |
|---|---|---|---|
| `wemm`（默认） | WeMM-Embedding-2B / 2048d / 8102 | 同左（统一空间） | 有 ≥10 GB 显存/内存的机器（153） |
| `legacy` | chinese-clip ViT-L-14 / 768d / 8103 | bge-m3 / 1024d / 8101 | 16 GB Mac mini 等小内存机器 |

- 方案常量在 `domain/retrieval/scheme.py` 的 `SCHEMES`；适配器在
  `infrastructure/models/wemm.py` 与 `legacy.py`。
- **切换方案 = 重建向量**。`memory_vectors` 按 `model_name` 分套，Qdrant collection
  按 `(space, model, dimension)` 分套；不重建就是“索引为空”。
- 2.0 不提供“某个槽单独换模型”的能力。要换就改 `SCHEMES` 并整方案迁移。

legacy文本HTTP协议：优先`POST /embed`，仅404时协商`/v1/embeddings`；
仅对BGE-M3及服务明确返回的`pooling=none`错误，使用原生`/embedding`的首token（CLS）
并在客户端L2归一化。不是mean pooling，不依赖原生接口忽略的`embd_normalize`。
维度/非有限/零CLS/多输入响应必须失败并保留`load_error`；端口和失败空表语义不变。
这是同一模型的传输协议兼容，不切换嵌入方案。量化差异记录在运行部署证据中，完整回归使用新scope。

---

### 4.6 向量索引（`VectorIndex`）

**必须保持**

- 权威存储是 SQLite `VectorRepository`；索引只是镜像；
- `upsert` 幂等；拒绝空向量；
- 按 `scope_id` 过滤；
- 不可用抛 `VectorIndexUnavailable`，禁止返回空列表；
- `status()` 必须能报告 `backend/degraded/reason`；
- collection 命名按 `(space, model, dimension)`。

**替换为 Milvus/pgvector/内存索引时**

1. 实现四个方法；
2. 保留 `VectorRepository` 写入路径不变（先权威、后镜像）；
3. 在 `application/retrieval` 里确认回退逻辑仍走权威存储；
4. 补 `tests/integration/test_qdrant_index.py` 的等价用例（含目录/连接锁、重启恢复）；
5. 运行 `test_visual_search.py` 端到端。

---

### 4.7 人脸检测（`FaceDetector`）

**必须保持**

- `detect` 只返回带向量的人脸；
- `detect_identity_photo` 三级降级：放大重试 → 整图兜底；
- `verified` 四项合取；`uncertain` 仍是证据但不进簇；
- 对齐 112×112、ArcFace 归一化；
- 权重缺失 → `available=False` → 空表；普通异常 → warning + 空表；
- CUDA 请求实得 CPU 必须抛 `CudaProviderMissingError`，不能吞。

**替换**

1. 实现 `FaceDetector`；
2. 保持 `FaceDetection` 的字段（bbox、quality、embedding、model_name）；
3. 装配 `face_detector=`；
4. 跑 `test_face_detector.py` + `test_face_manifest.py`（face_id 稳定性）。

---

### 4.8 媒体探测 / 存储 / 渲染

| 能力 | 失败语义 | 替换注意 |
|---|---|---|
| `MetadataReader` | 返回 `(None, None, None)` | EXIF 缺失不能阻断导入 |
| `PreviewEncoder` | 坏框抛 `ValueError` | 裁剪与预览共用 EXIF 转置口径 |
| `VideoMetadataReader` | 抛 `ValueError` | ffprobe 非零退出/非 JSON/缺流都算失败 |
| `VideoTranscoder` | 抛 `RuntimeError` | 已是 H.264 返回 `None`；零退出但无产物也算失败 |
| `VideoKeyframeExtractor` | 抛 `RuntimeError` | `-ss` 前置、单帧；失败整条场景放弃 |
| `MediaStorage` | 缺失抛 `FileNotFoundError` | 文件名 `{asset_id}_{sanitise(name)}` |
| `PersonCropRenderer` | 退化框/图太小抛 `ValueError` | 临时文件由调用方删除 |

---

### 4.9 地理

**必须保持**

- `AdminResolver` 与 `NearestResolver` 互补，不可互换；
- `OfflinePlaceResolver` 纯离线、零网络；
- 数据源缺席返回 `None`，不逐条重试加载；
- 置信度：行政区 0.90 / 最近地点 0.70；
- 最近地点半径 50km，国内池优先；
- 事件同时写 `place.display()` 与 `place.admin.district`。

**替换数据源**

1. 实现 `AdminResolver` 或 `NearestResolver`；
2. 数据文件放 `data_dir/geo`（不得单独配路径）；
3. 装配 `OfflinePlaceResolver(admin=..., nearest=...)`；
4. 跑 `test_offline_place_resolver.py` + `test_geonames_resolver.py`；
5. 抽查 50 个 GPS 点的行政区与“附近”文案。

**红线**：不得加入评测地标别名（赵州桥、三峡坝址等）；别名只保留通用中英桥接。

---

### 4.10 持久化仓储（换数据库）

这是**最高风险**的替换。端口语义不只是方法签名，还包括：

- `None` 表示“不存在”，不是“出错”；
- 部分方法要求幂等（见 §3.6）；
- 部分写入有顺序/外键约束；
- `update_metadata` 必须整份替换；
- `scope_id=None` 表示跨相册，不等于默认相册。

**建议路径**

1. 先保持 SQLite 为权威，新增一个只读副本/影子写；不要一步切换；
2. 为每个 Repository 写契约测试（现有 `tests/integration/test_*_repository.py` 可直接复用）；
3. 先切一个低风险仓储（如 `SpaceRepository`），验证装配与迁移；
4. 再切高频写入（`AssetRepository`、`ObservationRepository`、`EventRepository`）；
5. 最后切 `VectorRepository`。

**必须补**

- 事务边界测试；
- 并发写入测试；
- 迁移/回滚脚本；
- 双写一致性校验（如果做过渡期）。

---

### 4.11 事件流（`TurnEventBroker`）

**必须保持**

- `open/publish/finish/subscribe` 四方法；
- 每个订阅者独立队列；
- 跨线程发布用 `call_soon_threadsafe` 等价机制；
- 回合收尾且无人订阅时清理；
- 历史事件不靠 broker，靠 `TurnRepository.events_after`。

换成 Redis/Kafka 时，**不要**把历史事件也塞进 broker：SSE 续传的权威是数据库。

---

### 4.12 评测侧

#### 4.12.1 换 Judge 模型（通常只改配置）

`judge_providers.json` 或 run 请求覆盖：

```json
{
  "url": "https://ark.cn-beijing.volces.com/api/plan/v3",
  "model": "doubao-seed-2.1-lite",
  "api_key_env": "ARK_API_KEY"
}
```

必须确认：

- [ ] URL 以 `/v3` 结尾时自动补 `/chat/completions`；
- [ ] `volces.com` 自动关闭 thinking；
- [ ] `temperature=0`、`max_tokens` 固定；
- [ ] 4xx 不重试；5xx 指数退避 6 次；
- [ ] 失败返回 `score=None`，**绝不写 0**；
- [ ] 评分分布与 1.0 同 rubric 对比。

换非 OpenAI 响应形状才需要改 `JudgeRunner._request` 与三个 judge 方法。

T006 Judge一致性v2：新增可选`judge.consistency_version=2`，规则仍在domain，
重问编排仍在application；只检查同一标点分句中的回答褒义，不能把问题可回答性或参考事实
当成模型回答正确。合法0分保留，真正矛盾的单次重问与None失败语义不变。
历史无版本继续v1，不回填未保存的重问分数。数字字符串score按domain统一v1规则确定转换，拒绝bool及非有限/小数/越界/混合文本；记录score_normalization_version/score_normalization及attempts原响应。历史明确原文可生成带源哈希的score_derivation，保留原分/状态；执行、读取、汇总共用规则。接口契约、列表/详情投影及前端类型同步。

Judge实时进度：`phases.qa_eval.progress`为规范计数，阶段外层同值兼容；Judge终态包含失败、跳过单列，有效评分数独立。Agent/Judge任一完成即由application提交同一检查点，不等全部Agent结束。前端缺失计数显示未知，不回写历史。详见指标契约的T006 Judge实时进度。

#### 4.12.2 换推理后端

实现 `InferenceProvider` + `LifecycleProvider` + `TelemetryProvider`，替换：

- `apps/photobench/src/photobench/bootstrap/container.py:146-167`（按主机选三件套）
- `infrastructure/runtime/selection.py:171`
- `infrastructure/runtime/providers.py:1162`

探活用 `chat`，计时用 `chat_stream`。

#### 4.12.3 新增一个 phase

1. 在 `RunExecution` 加 `_phase_xxx(self, state, request)`；
2. 在 `service.py:337-346` 的列表注册（reuse 模式另行决定是否跳过）；
3. 用 `state.record_phase/phase_done` 写 `phases.<name>.{status,started_at,finished_at,total_seconds,...}`；
4. 需要新输入 → `RunRequest` 加字段，并在 `suites.start_suite` 透传；
5. 异常必须抛出（统一落 `failed`）；可恢复用 `phase_partial`；
6. 更新 `photobench-module-map.md`；
7. 补 `test_run_execution.py` 的顺序/失败中止/partial 用例。

#### 4.12.4 新增数据集

实现 `AlbumSource`：

```python
class AlbumSource(Protocol):
    def manifest(self, album_id: str) -> dict[str, Any]: ...
    def qa_rows(self, album_id: str, qa_set: str) -> list[dict[str, Any]]: ...
    def read_file(self, album_id: str, relative: str) -> bytes: ...
    def import_target(self, album_id: str, relative: str) -> tuple[str, int]: ...
    def integrity(self, album_id: str, manifest: dict[str, Any], qa_set: str) -> dict[str, Any]: ...
    def list_album_ids(self) -> list[str]: ...
    def media_file(self, album_id: str, collection: str, file_name: str) -> str | None: ...
```

要点：

- `manifest` / `read_file` 读不到必须抛 `KeyError`（由上层映射 404），不要返回空 dict/空 bytes；
- `import_target` 返回 `(服务端绝对路径, 媒体文件数)`，文件数直接作为 `POST /imports` 的 `limit`，**必须数实**；
- `media_file` 返回绝对路径而不是字节（HTTP 层要按 Range 分段读）；三段路径都必须做穿越拦截；
- `integrity` 要覆盖 manifest、QA、每张媒体、每张参考图。

装配 `apps/photobench/src/photobench/bootstrap/container.py:175-177`。

必测：路径穿越、导入 limit、QA 行字段、完整性哈希。

#### 4.12.5 新增指标

1. 在 `domain/runs/analysis.py` 写纯函数；
2. 在 `_evaluate_one` 写 item 级字段；
3. 在 `capability_summary` 输出汇总；
4. 在 `effective_summary` 补偿旧 run；
5. 无样本用 `None`，不要用 0；
6. 候选/交付两套指标不得混算；
7. 前端 `types.ts` + 后端投影 + 测试同步。

---

## 5. 改动验收标准（Definition of Done，核心）

**任何改动**（替换、bugfix、新功能、重构）只有在以下 9 类标准全部满足时才算完成。替换类改动必须全勾；bugfix/新功能按 §5.10 的附加说明裁剪，但**失败语义、外部契约、测试、登记**四项不可裁剪。

### 5.1 接口标准

- [ ] 适配器实现端口全部方法，签名逐字一致；
- [ ] 不新增调用方依赖的公开方法；
- [ ] `mypy --strict` 通过；
- [ ] `Container` 字段仍声明为端口类型；
- [ ] 没有从 `application`/`domain` 反向 import 适配器。

### 5.2 行为等价标准

- [ ] 对外 API schema 不变（除非走接口变更流程）；
- [ ] 错误码与错误体 `{code,message,detail}` 不变；
- [ ] `None` 的语义不变（见 §5.3）；
- [ ] 幂等性不变（同一输入重复调用结果一致）；
- [ ] 顺序/事务约束不变；
- [ ] 分页、排序、过滤语义不变。

### 5.3 失败语义标准

| 语义 | 端口 | 规则 |
|---|---|---|
| **fail-open（返回 None/空）** | `VlmObserver`、`PhotoInspector`、`PersonVision`、`AssetObserver`、`PlaceResolver`、`Admin/NearestResolver`、`MetadataReader` | 失败不能阻断主链路；必须 warning |
| **fail-closed（抛异常）** | `ChatModel`、`VideoMetadataReader`、`AudioMetadataReader`、`VideoTranscoder`、`VideoKeyframeExtractor`、`VectorIndex`、`FaceDetector`（CUDA 缺失） | 失败必须让调用方看见 |
| **返回 None = 不存在** | 各 Repository 的 `get`、`find_*`、`lookup` | 不得用 None 表示错误 |
| **返回 False/空集 = 查无结果** | `AssetPlaceIndex`、`LexicalSearch` | 不等于“人没有照片” |

**红线**：不得把 fail-open 改成 fail-closed（会让导入整批失败），也不得把 fail-closed 改成 fail-open（会静默降级）。

### 5.4 性能与并发标准

- [ ] 所有外部 IO 有超时；
- [ ] 重试有上限；4xx 不重试；
- [ ] 并发上限可配置或在适配器内固定；
- [ ] `prepare` 可并行、`commit` 必须串行（媒体/视觉链）；
- [ ] 不引入无界队列/无界线程池；
- [ ] 大文件走流式，不整块读入内存；
- [ ] 取消/超时能向上传播。

### 5.5 可观测性标准

- [ ] 失败有 `warning` 日志，包含可定位的标识（asset_id / endpoint / model）；
- [ ] 降级能在 `/api/v1/health` 看见；
- [ ] 不泄露密钥、完整 prompt、用户原始内容；
- [ ] 新增指标遵循 `None` 回退口径；
- [ ] 关键路径有耗时字段（item 级或 phase 级）。

### 5.6 安全与合规标准

- [ ] API key 只从环境变量/配置存储读取，不进代码、不进日志；
- [ ] 不把 benchmark 内容写入代码或提示词；
- [ ] 不新增出口清洗；
- [ ] 外部数据源许可证明确；
- [ ] 用户数据不离开部署边界（除非明确裁决）。

### 5.7 配置标准

- [ ] 新增配置必须回答：“这是部署事实吗？”是才进 `Settings`；
- [ ] 数据路径必须由 `SENTRIX_DATA_DIR` 派生，不允许单独配置；
- [ ] 不新增零散 env flag；
- [ ] 默认值要能在本地开发环境跑通；
- [ ] 配置变化写进 §6 矩阵。

### 5.8 文档标准

- [ ] `module-map.md` 登记新文件与依赖边（双向）；
- [ ] `capability-map.jsonl` 增/改记录，`verified` 指向真实用例；
- [ ] 偏离 1.0 或冻结行为写入 `deviations.md` / `frozen.md`；
- [ ] 公开 schema 变化重导 OpenAPI；
- [ ] 本文件 §3/§6 同步更新（如果端口或配置变了）。

### 5.9 测试标准

| 层 | 必须覆盖 | 命令 |
|---|---|---|
| unit | 纯逻辑、错误映射、边界 | `pytest apps/api/tests/unit` |
| contract | 每个 v1 端点 | `pytest apps/api/tests/contract` |
| integration | 真后端契约（临时目录/本地 HTTP/合成数据） | `make test-integration` |
| 真实环境专项 | 真相册、GeoNames、人脸权重、真实模型 | `make test-real`，按影响指定 `REAL_TEST_PATHS` |
| PhotoBench | 适配器替换、相位、指标 | `pytest apps/photobench/tests` |

**A 类 Provider 至少补 integration；C 类仓储至少补 integration + 并发用例。**

2026-10-23 用户确认三层验证：日常 `make ci` 执行全部未标记 `real_environment` 的自动测试，
继续检查严格类型、分层/可达性、文档、两份 OpenAPI、≥85% 覆盖率及两个前端的测试与构建。
真实资源用例保留全部原断言，明确标记后独立运行；同文件内的纯算术/阈值用例不得随真实权重测试一起移出。
专项入口 `scripts/run_real_tests.py` 保留 JUnit，零用例、缺依赖 skip、失败或错误均不能通过；
`make ci-real` 为日常门禁加全套真实专项。直接 pytest 的 skip 仅是未执行记录，不能作专项通过证据。
真机10QA另验固定版本部署、本机依赖、执行终态、遥测与评分，不替代专项中的地点/人脸/重复导入断言。
涉及公共执行或指标的发布验证受影响四机，平台专用变更按影响验证对应设备；纯测试编排不触发额外模型回归。
详见 [测试分层与证据](../runbooks/testing.md)。


失败语义的契约测试模板：

```python
def test_observer_swallows_failure(tmp_path):
    observer = MyVlmObserver(endpoint="http://127.0.0.1:1")  # 不可达
    assert observer.observe(tmp_path / "missing.jpg") is None  # 不抛

def test_vector_index_reports_unavailable(tmp_path):
    index = MyVectorIndex(tmp_path / "broken")
    with pytest.raises(VectorIndexUnavailable):
        index.search(space="image", vector=[0.1] * 2048, limit=10)

def test_chat_model_maps_prompt_too_long():
    model = MyChatModel(...)
    with pytest.raises(PromptTooLongError):
        model.complete(too_long_messages(), [])
```

### 5.10 非替换类改动的附加标准

| 改动类型 | 必须额外回答的问题 |
|---|---|
| **bugfix** | 根因是什么？为什么不是 symptom-patch？有没有一条会复现该 bug 的测试？修复后失败语义是否变化？ |
| **新功能** | 落层是否正确（domain 规则 / application 用例 / infrastructure 适配器）？是否引入了新的外部契约？是否需要新的端口？失败时是 fail-open 还是 fail-closed？ |
| **重构** | 外部可观察行为是否逐字保持？是否更新了 `behaviour-baseline.md`？是否有“重构顺手改行为”的夹带？ |
| **提示词改动** | 是否引入了 benchmark 内容？是否改变了固定字段/内部词的约束？是否在 487qa 或 10qa 上验证过？ |
| **前端契约改动** | 后端投影、`types.ts`、前端测试、`run.json` 兼容性是否同时改？ |

**通用红线**：任何改动都不得以“先让它看起来对”为目标。禁止出口清洗、禁止静默降级、禁止把评测数据写进代码。若确实需要临时例外，必须在 PR 描述里写明**原因、影响面、移除时间**，并由模块 owner 与架构 owner 共同批准。

---

## 6. 配置与环境变量矩阵

### 6.1 产品侧（`Settings`）

| 变量 | 作用 | 默认 | 影响 | 必填 |
|---|---|---|---|---|
| `SENTRIX_DEPLOYMENT_PROFILE` | 集中部署预设server/mac；显式值优先 | `server` | 嵌入方案、Agent/Chat预算、人脸provider默认 | 否 |
| `SENTRIX_AGENT_WALL_TIME_S` | Agent墙钟预算；mac缺省600 | `180` | Agent预算 | 否 |
| `SENTRIX_DATA_DIR` | 数据根，派生 db/media/previews/qdrant/geo | `data` | 全部持久化/索引 | 生产必设 |
| `SENTRIX_WEB_DIST_DIR` | 前端构建产物 | `apps/web/dist` | Web 挂载 | 否 |
| `SENTRIX_API_HOST` | 监听地址 | `0.0.0.0` | API | 否 |
| `SENTRIX_API_PORT` | 端口 | `8191` | API | 否 |
| `SENTRIX_VLM_BASE_URL` | OpenAI 兼容端点 | `http://127.0.0.1:8100/v1` | Chat/VLM/PersonVision/PhotoInspector | 否（缺失则助手不可用） |
| `SENTRIX_WEMM_URL` | WeMM 侧车 | `http://127.0.0.1:8102` | wemm 方案 | 否（缺失降级） |
| `SENTRIX_EMBEDDING_SCHEME` | **嵌入方案**：`wemm`（默认）/ `legacy` | `wemm` | 视觉+文本两条向量通道 | 否 |
| `SENTRIX_TEXT_EMBEDDER_URL` | bge-m3 侧车 | `http://127.0.0.1:8101` | legacy 方案文本 | 否 |
| `SENTRIX_LEGACY_CLIP_URL` | chinese-clip 侧车 | `http://127.0.0.1:8103` | legacy 方案视觉 | 否 |
| `SENTRIX_LEGACY_CLIP_TIMEOUT_S` | chinese-clip 超时 | `30` | legacy 视觉 | 否 |
| `SENTRIX_TEXT_EMBEDDER_TIMEOUT_S` | bge 超时 | `10` | legacy 文本 | 否 |
| `SENTRIX_CHAT_TIMEOUT_S` | 对话超时 | `120` | ChatModel | 否 |
| `SENTRIX_VLM_TIMEOUT_S` | 视觉超时 | `180` | VLM/PersonVision/PhotoInspector | 否 |
| `SENTRIX_VLM_CONTEXT_TOKENS` | 单槽窗口兜底 | `6144` | ChatModel | 否（探测优先） |
| `SENTRIX_ALBUM_WORKERS` | 导入预取并发 | `4` | 导入流水线 | 否 |

> **`SENTRIX_EMBEDDING_SCHEME` 是唯一的“方案级”开关，不是“槽位级”开关。**
> 它一次切换整套方案；不存在 `visual_embedder` / `text_embedder` 这类独立变量。
> 切换方案必须重建向量（旧 `model_name` 的行不会命中）。新增任何同类开关前，
> 先回答 §5.7 的问题：它是不是部署事实？

T006 指标 v2 的可选契约见 [photobench-metrics-contract.md](photobench-metrics-contract.md)。
运行拓扑值对象 `RunRuntimeSnapshot` 定义于 `photobench/domain/runtime/ports.py`；
由配置端口读出并在application建run时冻结，bootstrap的 `runtime_providers_for_run` 注入HTTP与遥测。
按run构造的SentrixProcess同样注入快照；重启脚本通过既有PHOTOBENCH_CONFIG_ROOT读取原子发布的只读快照文件，模型及嵌入均不得被后续全局配置覆盖。
旧 `runtime_providers` 装配入口保留一个发布周期。`TelemetryProvider` 四方法签名不变；
失败继续返回unavailable/not_applicable并附原因。严格遥测仅由独立回归验收器判定。
产品调用轨迹新增解析尝试/成功数与错误耗时等通用事实，不在产品层定义benchmark规则。

### 6.2 非 `Settings` 的环境变量

| 变量 | 作用 | 默认 | 所属 |
|---|---|---|---|
| `FACE_MODEL_NAME` | buffalo_l 目录名/embedding_version | `buffalo_l` | FaceAdapter |
| `FACE_MODEL_ROOT` | insightface 根 | `~/.insightface` | FaceAdapter |
| `RETINAFACE_MODEL_PATH` | RetinaFace 权重 | `~/benchmarks/retinaface/retinaface_r50.onnx` | RetinaFace |
| `FACE_MIN_SIZE` | 最小脸边长 | `64` | FaceAdapter |
| `FACE_VERIFIED_SCRFD_SCORE` | 复核对分阈 | `0.7` | FaceAdapter |
| `FACE_VERIFIED_MIN_AREA` | 最小面积比 | `0.003` | FaceAdapter |
| `FACE_IDENTITY_MIN_QUALITY` | 进簇质量阈 | `0.55` | FaceAdapter |
| `FACE_DET_SIZE` | SCRFD det_size | `640` | FaceAdapter |
| `FACE_PROVIDERS` | buffalo provider 链 | `CUDAExecutionProvider,CPUExecutionProvider` | FaceAdapter |
| `RETINAFACE_PROVIDERS` | RetinaFace provider 链 | 同上 | RetinaFace |
| `SENTRIX_FACE_GPU_DEVICE_ID` | GPU 编号 | `0` | 人脸 |
| `SENTRIX_FACE_GPU_SESSION_LIMIT_MIB` | buffalo 单 session 显存 | `256` | FaceAdapter |
| `SENTRIX_RETINAFACE_GPU_SESSION_LIMIT_MIB` | RetinaFace 单 session 显存 | `256` | RetinaFace |
| `SENTRIX_FACE_GPU_MAX_CONCURRENCY` | 人脸 GPU 并发 | `1` | 人脸闸门 |
| `SENTRIX_PLANNER_MAX_ATTEMPTS` | 规划重试 | `3` | application |
| `SENTRIX_PERSON_INSIGHT_SCOPES` | 画像允许列表 | 空 | application |
| `SENTRIX_LIB_THREADS` | 数值库/ORT/torch/OpenCV线程上限；入口先设置OMP等 | 空=自动，0=不限制 | process entry / infrastructure |
| `SENTRIX_PIPELINE_MAX_WORKERS` | 自动线程推导的并发分母 | 4 | thread limits |

### 6.3 评测侧

| 变量/配置 | 作用 |
|---|---|
| `PHOTOBENCH_QA_CONCURRENCY` | QA并发；server默认0自动，mac默认2；显式0仍自动 |
| `PHOTOBENCH_IMPORT_TIMEOUT` | 同步导入预算；server7200秒、mac86400秒，显式值优先 |
| `PHOTOBENCH_JUDGE_CONCURRENCY` | Judge 并发覆盖（默认 `min(8, qa_concurrency)`） |
| `judge_providers.json` | Judge 端点/模型/密钥 env 名 |
| `runtime_connection.json` | 主模型、Sentrix、嵌入及产品服务拓扑唯一配置源；每run冻结，已运行快照不变 |
| `PHOTOBENCH_CONFIG_ROOT` | 配置根；优先于旧单文件路径覆盖 |
| `PHOTOBENCH_RUNTIME_CONNECTION` / `PHOTOBENCH_RUNTIME_SERVICES` | 旧单文件路径入口，未指定CONFIG_ROOT时兼容 |
| `PHOTOBENCH_DATA_ROOT` / `PHOTOBENCH_RESULTS_ROOT` | 数据集及结果根；发布显式绑定原路径 |
| `PHOTOBENCH_SENTRIX_PID_FILE` / `PHOTOBENCH_SENTRIX_LOG_PATH` | 既有进程控制参数；固定release绑定runtime内控制记录和日志，登记新API避免首轮无必要重启 |
| `build-info.json` | 固定release的SHA与指标契约版本；缺文件为development |
| `SENTRIX_WEB_DIST_DIR` / `PHOTOBENCH_WEB_DIST_ROOT` | 统一构建的前端dist根 |
| `HF_HUB_OFFLINE` / `TRANSFORMERS_OFFLINE` | 回归发布设置为1；缺权重在preflight暴露 |
| `custom_judge_prompt.json` | 自定义评分提示词 |
| run 请求字段 | 单轮覆盖 judge URL/model |

---

## 7. 多人协作流程

### 7.1 建议 ownership

| 模块 | 主责 | 评审必须包含 |
|---|---|---|
| `domain/assistant` + agent 提示词 | Agent 负责人 | 评测负责人 |
| `domain/media` + 导入链 | 记忆/媒体负责人 | 性能负责人 |
| `domain/identity` + 家庭图谱 | 身份负责人 | 数据负责人 |
| `domain/retrieval` + 嵌入/索引 | 检索负责人 | 算法负责人 |
| `domain/places` | 数据负责人 | 记忆负责人 |
| `apps/photobench` | 评测负责人 | 产品负责人 |
| `bootstrap/container.py` | 架构负责人 | 对应模块负责人 |

### 7.2 替换提案（RFC）模板

任何 A/B 级替换先提一个 issue/PR 描述，包含：

```markdown
## 替换目标
端口：ChatModel
现状：OpenAIChatModel @ 8100
候选：<新实现>

## 动机
<为什么换；量化目标>

## 影响面
- 装配点：
- 配置：
- API/schema：
- 索引/数据迁移：
- 前端/评测：

## 失败语义
<fail-open / fail-closed；与端口契约是否一致>

## 验证计划
- unit：
- integration：
- 487qa reuse 对比：
- 回滚方式：

## 风险
<显存、延迟、许可证、数据兼容>
```

### 7.3 PR 检查清单

- [ ] 只改了必要的文件；
- [ ] `Container` 字段是端口类型；
- [ ] 没有新增反向依赖；
- [ ] 没有新增零散 env flag；
- [ ] 失败语义与端口契约一致；
- [ ] 有 warning 日志/健康上报；
- [ ] 补了对应层测试；
- [ ] 更新 module-map / capability-map / deviations；
- [ ] `make ci` 通过；
- [ ] 附 487qa reuse 前后对比（模型/检索类替换必填）。

### 7.4 接口变更（改端口）

“替换”不允许改端口。确实需要改时：

1. 先加**新方法/新端口**，保留旧方法并标注 deprecated；
2. 所有调用方迁移；
3. 至少一个发布周期后才删除旧方法；
4. 更新 `behaviour-baseline.md`；
5. 如果是 API/schema 变更，先改 OpenAPI，再改实现。

### 7.5 回滚标准

每次替换必须能在一分钟内回滚：

- 适配器实现保留到新实现稳定；
- 装配点用**配置开关或代码回滚**（推荐直接 revert 装配行）；
- 数据迁移必须有反向脚本；
- 索引变更必须能重建。

### 7.6 能力接入优先级建议（多人排期用）

| 优先级 | 能力 | 端口 | 改动面 | 风险 | 说明 |
|---|---|---|---|---|---|
| P0 | 对话模型 | `ChatModel` | 1 个适配器 + 装配 | 中 | 影响全部 Agent 行为，必须跑 487qa reuse 对比 |
| P0 | 嵌入模型 | `VisualEmbedder`/`TextEmbedder` | 适配器 + 索引重建 | 高 | 换维度=新索引空间，必须重建 |
| P1 | Judge 模型 | 配置 | 无代码 | 低 | 先换配置，非 OpenAI 形状才写代码 |
| P1 | VLM 观察 | `VlmObserver` | 1 个适配器 | 中 | 必须保持 fail-open |
| P2 | 向量索引 | `VectorIndex` | 1 个适配器 | 中 | 权威存储不变，先做影子索引 |
| P2 | 地理数据源 | `AdminResolver`/`NearestResolver` | 1–2 个适配器 | 中 | 必须离线；禁止评测地标别名 |
| P2 | 人脸检测 | `FaceDetector` | 1 个适配器 | 高 | 影响聚类与身份题，需重建人脸向量 |
| P3 | OCR 独立能力 | **无现成端口** | 需先加端口 | 高 | 当前只有导入期 `ocr_text` |
| P3 | 持久化 | 全部 `*Repository` | 20+ 适配器 | 极高 | 建议先只读影子库，逐仓储切换 |
| P3 | 事件流 | `TurnEventBroker` | 1 个适配器 | 低 | 历史事件仍走数据库 |

排期原则：**先换“可回滚且无数据迁移”的，再换“要重建索引/迁移数据”的**。

### 7.7 Definition of Ready / Done

**Ready**

- 端口已存在且契约清楚；
- 失败语义已确定；
- 测试数据可用；
- 影响面已列全。

**Done**

- §5 九类标准全部勾选；
- `make ci` 绿；
- 一次真实 run 验证（模型/检索类）；
- 文档已登记；
- 评审通过。

---

## 8. 反模式清单

| 反模式 | 后果 | 正确做法 |
|---|---|---|
| 在 `application/` import infrastructure | 分层契约红，替换点失控 | 只依赖 domain 端口 |
| 在 `Container` 字段写具体类 | `test_container_wiring` 红，调用方可能用协议外方法 | 写端口类型 |
| 新增 `SENTRIX_FOO` 开关 | 重蹈 1.0 的 126 个 env flag | 部署事实才进 `Settings` |
| 失败返回 `[]` 当“没结果” | 检索静默降级，线上不可见 | 抛 `VectorIndexUnavailable` 或上报 `degraded` |
| 换嵌入模型不换 `model_name` | 新旧向量混算，召回看似正常 | 视为新索引空间，重建 |
| `prepare` 里写库 | 并发下重复建事件/簇 | `prepare` 只算，`commit` 写 |
| 在适配器里拼业务提示词 | 提示词漂移、无法审查 | 提示词放 domain/application |
| 出口清洗内部词 | 掩盖根因 | 提示词约束 + L1 + 保留原答 |
| 把评测地名写进别名/提示词 | benchmark 泄漏 | 只保留通用桥接 |
| 在 photobench 里 import sentrix | 隔离契约红，评测污染产品 | 走 `/api/v1` HTTP |
| 改 `run.json` 字段不同步前端 | 评测页空白/崩溃 | 后端投影 + `types.ts` + 测试 |
| 用 0 代替“无样本” | 均值被拉低，误导判断 | 用 `None` 并排除分母 |
| 在 judge 失败时写 0 分 | 把系统故障算成模型错误 | `score=None, judge_status=failed` |
| 在适配器内重试 4xx | 放大故障、拖长回合 | 4xx 不重试 |
| 把历史事件塞进 broker | 断线续传不可靠 | 历史走 `TurnRepository` |

---

## 9. 已知边界（替换前必须确认）

### 9.1 未接线/未使用

- `ToolExecutor`（`domain/assistant/ports.py:180`）：无实现、无调用；
- `MediaTypeResolver`（`domain/media/ports.py:293`）：无实现、无调用；
- `Settings.text_embedder_url`：未被消费（W1 起统一走 WeMM）。

**不要**把它们当作现成扩展点。

### 9.2 方法级 PENDING

以下端口方法当前无生产调用者，替换时不要假设有调用链：

`EventRepository.dissolve`、`PortraitRepository.list_for_person`、`PortraitRepository.set_lock`、`MediaStorage.path_for`、`LexicalIndex.rebuild_scope`、`InferenceProvider.token_count`、`TelemetryProvider.kv_cache`。

### 9.3 冻结/不实现能力

- ASR / 转写 / `transcript` 列不存在；
- OCR 只有导入期 `ocr_text` 一条路，没有独立 PaddleOCR 适配器或 VLM 回退；
- 无导入取消、无 batch/job；
- 无云端大模型入口（评测可配，产品侧不内置）；
- 记忆画像的模型同步留空；
- 关系假设 / 角色假设 / 实体合并 / 洞察运行 / 主动建议等能力冻结。

### 9.4 依赖声明风险

根 `pyproject.toml` **只有工具配置，没有 `[project]` 段**；运行依赖实际散落在部署镜像与陈旧的 `egg-info` 中。新增适配器引入新库时：

- 必须同时在部署镜像/Dockerfile 里声明；
- 不能假设 `pip install -e .` 会带上依赖；
- 建议在本文件 §4 对应能力处记录新增依赖。

### 9.5 两套部署形态（153 WeMM / 130 legacy）

| 项 | 153（默认） | 130 Mac mini |
|---|---|---|
| `SENTRIX_EMBEDDING_SCHEME` | 不设（= `wemm`） | `legacy` |
| 视觉嵌入 | WeMM 2048d / 8102 | chinese-clip 768d / 8103 |
| 文本嵌入 | WeMM 2048d / 8102 | bge-m3 1024d / 8101 |
| 人脸 provider | CUDA | **必须显式 `FACE_PROVIDERS=CPUExecutionProvider`** |
| 启动脚本 | `scripts/deploy/start_api.sh` | `scripts/deploy/start_legacy_mac.sh` |

**PhotoBench 迁移到另一台机器时，最容易漏的一处是
`apps/photobench/config/runtime_connection.json`**：PhotoBench 的
`SentrixClient` 读的是这个文件里的 `sentrix_url`，**不是**环境变量
`PHOTOBENCH_SENTRIX_API_URL`。把 PhotoBench 从 153 复制到 130 时若不改它，
评测会把导入请求发回 153，而路径是 130 的——表现为
`photo_import` 立即失败、`directory not found`，且本机 2.0 日志里看不到任何
`POST /api/v1/imports`。迁移检查清单里必须包含这一项。

---

## 10. 附录

### 10.1 关键文件地图

```
apps/api/src/sentrix/
├── config.py                       # 10 个可配字段 + 派生路径
├── bootstrap/container.py          # 唯一装配点
├── domain/*/ports.py               # 全部端口
├── application/                    # 用例编排
├── infrastructure/                 # 适配器
│   ├── assistant/                  # chat client / event broker / endpoint probe
│   ├── media/                      # VLM / inspector / ffmpeg / pillow
│   ├── models/                     # WeMM / face adapter
│   ├── places/                     # pygeocn / geonames / composite
│   ├── persistence/repositories/   # 全部 SQLite 仓储
│   └── vector/                     # qdrant
└── api/routes/                     # 传输映射 + 契约

apps/photobench/src/photobench/
├── domain/{datasets,runtime,deploy,results,sentrix,http,config}/ports.py
├── application/run_execution/      # 8 相位 + judge
├── application/runs/suites.py      # 运行注册/套件
├── domain/runs/analysis.py         # 指标聚合
└── bootstrap/container.py          # 评测装配点
```

### 10.2 常用命令

```bash
# 全量门禁
make ci

# 分层跑
pytest apps/api/tests/unit
pytest apps/api/tests/contract
pytest apps/api/tests/integration
pytest apps/photobench/tests

# 架构
lint-imports
python scripts/check_reachability.py
python scripts/check_method_reachability.py
python scripts/check_module_map.py
python scripts/check_capability_map.py

# 前端
cd apps/web && npm run typecheck && npm run test
cd apps/photobench/web && npm run typecheck && npm run test
```

### 10.3 装配示例：换一个 ChatModel

```python
# 1) infrastructure/assistant/my_chat_client.py
class MyChatModel:
    def __init__(self, base_url: str, served: ServedModel, *, timeout_s: float, context_tokens: int) -> None:
        ...

    def complete(self, messages, tools) -> ModelReply:
        # 保持：system 第一条、原生 tools、temperature=0、非流式
        # 错误映射：ModelServiceError / PromptTooLongError / OutputTruncatedError
        ...

# 2) bootstrap/container.py
chat_model=MyChatModel(
    settings.vlm_base_url,
    served_model,
    timeout_s=settings.chat_timeout_s,
    context_tokens=served_context or settings.vlm_context_tokens,
),
```

### 10.4 替换评审的一页纸

```text
端口是否闭合？         □ 是
失败语义是否一致？     □ 是
幂等/顺序是否保持？   □ 是
降级是否可见？         □ 是
测试是否补齐？         □ 是
文档是否登记？         □ 是
make ci 是否绿？       □ 是
能否一分钟回滚？       □ 是
```

---

## 11. 文档维护

- 本文档与代码同步责任：**改动端口或装配点的 PR 必须同时改本文档**；
- 端口新增/删除 → 更新 §3；
- 配置新增/删除 → 更新 §6；
- 新增已知边界 → 更新 §9；
- 版本记录：

| 版本 | 日期 | 变更 |
|---|---|---|
| 1.0 | 2026-10-19 | 初版：端口清单、替换流程、验收标准、协作规范 |

---

*本文档中的行号以 2026-10-19 的 `psh2.0` 工作树为准；代码移动后请同步更新。*

Judge一致性v3进一步限定为明确“模型/助手的回答/答案/结论”或分句开头针对回答的肯定断言；参考答案已回答正确地点而模型未能作答的合法0分不重问。真正模型回答正确却给0仍重问一次；v1/v2历史语义保留，新执行consistency_version=3，标准化v1不变。


### 2026-10-22 运行配置与硬件核验补充

- `SentrixClient.diagnostics(timeout=10)` 为新增语义端口，返回非密钥的有效启动事实；旧方法不变。`TelemetryProvider` 四方法签名不变。
- `RunSuites` 创建队列前冻结 `RunRuntimeSnapshot`；full/build/reuse 均先同步并验证产品主模型、嵌入、产品预算/provider 和构建版本。resume 继承原快照/预算并记录来源，旧记录无快照时显式注明采用当前配置。只有登记的本机产品 API 可自动重启，其他 API 只核验。
- build 不读 QA、不构造 Judge，`qa_set=""`、`qa_count=0`、`items=[]`、`run_valid=null`；`scope_build` 记录完整导入状态和嵌入配置，配置改变须新建 scope，不能冒称旧索引已重建。
- 模型身份保存部署后 `/models` 的原始 ID、展示名、候选及原因；多模型无明确匹配不盲选，名称保留小数点。
- Sentrix 独立 HTTP 策略：GET/HEAD 瞬时断连最多三次，退避0.2/0.4秒，共享总预算；POST 仅连接建立失败可重试。发出后读写/协议超时结果未确认，不重放，保存 `request_outcome_unknown`。Judge 独立既有重试策略不变。
- 产品 `Settings` 集中解析 `SENTRIX_DEPLOYMENT_PROFILE=server|mac`；Mac 缺省 legacy、Agent600秒、Chat180秒、人脸CPU，显式设置优先。PhotoBench 同一启动入口解析 QA2/导入86400秒，显式 QA0表示自动。profile 不在 application/ONNX 适配器重复读取；provider 经 bootstrap 注入。
- 新可选事实：`model_identity`、`execution_budgets`、`snapshot_source`、`scope_build`；诊断附 `runtime_configuration`；配置 API 保留并校验白名单 `product_settings`。同步导入显示目录、已完成目录、等待起点，不伪造逐媒体进度。两份 OpenAPI/投影/前端同步。
- 硬件诊断属部署层：`runtime_acceleration_probe.py` 在有界子进程记录实际导入路径、包版本/来源记录哈希、用户 site、动态库、真实矩阵/卷积算子，以及本地 ONNX 推理的实际 kernel provider。CUDA 请求退为CPU必须失败；明确CPU合法。诊断不安装、不下载、不改服务。
- 安装入口拒绝覆盖现存 venv；Jetson 需完整带哈希依赖锁，在独立环境验证。禁止以软链接伪装 CUDA 大版本、盲改全局 LD_LIBRARY_PATH、复制不匹配的 Python/JetPack 环境。现用CPU路径可保留并报告限制。

输入初始化失败仍保存failed终态并标记failed_phase=initialization；无法读取的qa_count/input_integrity为null，前端展示未记录，不冒称0题。非法qa_set创建前拒绝。build验收expected_count=0且评分不适用；导入、处理和遥测门槛不变。


### 2026-10-22 采样调度与分量观测补充

- 仍经 `TelemetryProvider` 四方法采集，不改签名或装配；application内部的采样/发布协作者按单调时钟安排周期，跳过已错过周期，不无界追赶。全样本滚动聚合由单个独立发布线程执行，不持采样锁，不建立无界发布队列。
- 新可选契约 `memory_observation_version=1`、`memory_observations`、`memory_dependencies`、`sampling_diagnostics`；`gpu_metrics.sampling_integrity`记录采集、写入、发布异常和有界停止后尚存线程。公式和有效性仍归domain，系统文件/进程身份/缓存IO归infrastructure。API只投影、前端展示来源和值龄；缓存保持原观测起止，PID与start_ticks共同核验，不写死PID。
- Jetson每轮共享一次nvmap账本；监听socket缓存按端口集合隔离、最多32份且最长2秒，每次核验原持有者身份/socket，变化即重查；相关PID单次smaps同时取RSS/PSS，总探测时间达到1秒后不再读后续PID，未读分量保持null。单次内核文件读取无法由线程硬抢占，该预算不是硬实时承诺。 端口发现仅检查真实监听候选；进程清单先以stat建身份/父子图，仅对端口根及当前子孙读取详情并复核身份。本机PID/无端口关键词及旧无scope配置保留全量发现，远端服务不扩大本机归属。
- 正常生产保持fail-open；严格验收独立重算分量值龄及最老必要来源，仍以3秒门槛判断。确认无辅助服务的0也依赖新鲜拓扑；不完整不得伪装完整。停止最长共同等待10秒，未退出线程明确记异常，不能据此宣称资源已释放。
- 本轮用户要求仅本地修代码、暂不启动测评；不改驱动、CUDA/ORT环境或远端服务。新代码的真机时序与覆盖效果待后续授权验证，历史无逐分量时间不补造。

### PhotoBench 长期运行读写补充（2026-10-23）

结果新增能力先定义 `RunRevisions` / `RunReadViews`，旧 `RunStore` / `RunResultWriter` 不变；具体文件读写、提交清单、派生历史索引仍在 infrastructure，由既有 bootstrap 装配。application 只协调一致快照、缓存失效/预算、线程释放和轻量投影；API 收包后将同步用例交线程池，不在事件循环直接执行文件或 HTTP IO。指标公式未变，`include_history=false` 明确表示检查点/窗口展示，不冒称全轮重算。提交、兼容导出及失败语义见 [指标契约](photobench-metrics-contract.md) 和 [本轮偏差](../refactor/2026-10-23-long-running-deviations.md)。新增预算为内部有界策略，没有散落环境变量；旧格式及入口至少保留一个发布周期。

### 2026-10-23 导入进度协议

保留同步 `POST /api/v1/imports`，新增可选 `operation_id` 和独立 `GET /api/v1/imports/{operation_id}/progress`。产品仅输出通用处理事实；`completed = imported + deduplicated + failed`，`total` 为实际扫描并应用 limit 后的支持媒体数，扫描前为 null。媒体计数完成后仍依次进行事件总结、参与者刷新、家庭图谱、人物画像，只有同步请求完成才终态。updated_at 是步骤更新时间，observed_at 是查询时间，不能互换。查询不会重放导入。

由 domain ImportProgressStore 端口、application TrackImport 和 bootstrap 装配的有界进程内适配器实现；最多32个活跃跟踪、128条终态，重启或淘汰后404。重复在册ID409，容量不足429，均发生在副作用前。此能力不是持久作业队列，不承诺跨进程查询或取消产品执行。PhotoBench 使用运行快照中的同一客户端，每秒轮询、单次3秒超时，保存最后事实及查询故障；不盲重试导入POST。旧服务无进度时明确不可用。
