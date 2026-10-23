# 可复用资料导入与评测接口

本文约定通用的文本、图片和混合资料接入行为。ScriptMem 的纯文本场景与 ATM 的图片、邮件混合场景暴露了这些接口需求；数据集转换、题目选择、标准答案、裁判、费用台账和实验调度由调用方维护，不进入产品提示词或资料库。

## 1. 原文和来源身份

`POST /api/v1/media/import` 接受 multipart 表单：

| 字段 | 类型、默认值 | 行为 |
| --- | --- | --- |
| `file` | 必填文件 | 原始文件字节；文本资料使用 UTF-8，可用 `text/plain` |
| `scope_id` | 字符串，沿用既有默认范围 | 来源和检索的资料范围 |
| `external_source_id` | 可选字符串，默认 `null` | 调用方提供的稳定来源身份；非空表单值不得只含空白；空表单按未提供处理 |
| `source_timestamp` | 可选字符串，默认 `null` | 保留来源的原始时间字符串，当前只接受不带时区的 ISO 可解析时间 |
| `source_location` | 字符串，默认空串 | 来源提供的地点，独立于模型观察的地点 |
| `source_metadata` | JSON 对象字符串，默认 `{}` | 调用方的来源元数据；拒绝数组、非法 JSON、NaN/Infinity |

成功返回 HTTP 201，沿用 `ImportResponse`，包含 `asset_id`、`observation_id`、`media_type`、`status`、`deduplicated` 及已有索引状态。`GET /api/v1/assets/{asset_id}` 的资产摘要增加上述四个来源字段，字段默认值使旧资产可继续读取。

提供外部身份后，以 `(scope_id, external_source_id)` 幂等：

- 相同内容哈希、媒体类型、文件名、来源时间、地点和元数据再次导入，返回原资产和已有观察，`deduplicated=true`，不再调用准备或重新索引。
- 相同身份对应不同内容或上述元数据，返回 HTTP 409，错误 `code=external_source_conflict`，保留原资产。并发首次导入也执行同一幂等或冲突判定，唯一索引竞争不返回500，不吞掉其他完整性错误。
- 内容相同但外部身份不同，仍保留两个独立来源。相同身份在不同 scope 也独立。
- 未提供外部身份时，继续使用既有的 scope 内内容哈希去重。

`source_timestamp` 作为原字符串保存；可解析值用于 `captured_at`，不推断主机时区。带 `Z` 或偏移的输入当前返回 HTTP 400，不在导入时静默转换。无时区来源与旧带时区记录无法安全计算时间距离：事件候选不据此归并，带时间窗的元数据检索不据此命中；没有时间条件时仍可通过其他检索通道使用。相同时间类型保留原比较行为。带外部身份但没有来源时间时，不用导入时刻补造来源时间；既有图片 EXIF 元数据仍按既有流程读取。

元数据是来源提供的信息，不代表模型已经确认。地点可进入已有词法、元数据检索；正文中不拼入地点、时间或身份来伪造原文。

## 2. 文本保存、检索和证据

观察记录增加 `source_text: str = ""`。文本导入从原文件按 UTF-8 读取，非空白正文逐字存入该字段，包括换行；不转为图片，不调用视觉模型生成摘要，不把正文写成 OCR。正文接入现有文本嵌入与词法索引，嵌入模型仍由既有配置决定。非UTF-8或空白文本沿用观察失败不阻断原件归档的语义，HTTP可能返回201，但没有可读正文；`read_source_text` 返回 `source_text_unavailable`。调用方不能仅凭上传成功或 `processed` 就认定来源可用于问答。

PDF、压缩包或邮件容器的解析不属于这一合同：调用方若先提取文本，应保留其来源、页码和转换哈希到自己的资料清单。产品收到的文件就是本次导入的原始资料。

助手工具 `read_source_text` 接受：

```json
{"result_set_id":"result-set-from-this-turn","asset_handle":"handle-from-that-result-set"}
```

成功的 `model_visible` 结果包含 `asset_handle`、`full_text`、`source="imported_source_text"`、`certainty="supported"`、`external_source_id`、`source_timestamp`、`source_location`。`full_text` 返回保存的完整正文；搜索预览只是节选，不能作为正文完整性的证明。

工具要求结果集属于当前 scope，handle 能在该结果集中解析，资产属于该 scope、属于已发布来源集合且类型为 `text`。分别使用 `unknown_result_set`、`scope_mismatch`、`bad_handle`、`source_not_frozen`、`unsupported_media_type`、`source_text_unavailable` 等失败原因。工具失败不返回正文，不调用图片读取或视觉模型。

文本与混合资料原生回合还验证 `result_set_id` 是当前回合实际取得的结果集，不能把同 scope 的另一回合结果集带进来绕过工具流程。

文本证据使用 `source_text` 证据类型，满足后不要求附带照片。混合资料场景按候选的 `media_kind` 阅读正文或检查图片，保留原生检索、融合、规划、工具预算、`final_answer` 和内部审查；最终图片附件只取模型明确选择的结果，不把所有检索候选自动附上。

## 3. 冻结读取范围

以下 Settings 均为可选部署输入，使用 `SENTRIX_` 前缀：

| 设置 | 默认值 | 合同 |
| --- | --- | --- |
| `corpus_read_only` | `false` | HTTP 层拒绝资料写入；仍允许读请求及创建助手会话、提交回合 |
| `corpus_source_allowlist` | `null` | 指向 JSON 文件 `{"source_ids":["source-a","source-b"]}` |

提供 allowlist 后，助手工具按资产的 `external_source_id` 限制可见来源；空数组表示不允许任何来源，`null` 表示沿用普通上传行为。非法文件内容拒绝创建回合执行器，不回退为全库访问。结果集中的伪造候选也要再次检查 scope、类型和已发布来源身份。

这两个开关不是数据库文件只读模式或新的认证系统。只读开关作用于 HTTP 修改入口，助手会话及轨迹仍可写；allowlist 是助手工具的发布范围，不能代替调用方对部署、文件和直接资产接口的访问控制。调用方需在发布后固定 allowlist 和语料文件，并单独保存其哈希。

## 4. 视觉请求的可追溯编码

`visual_source_manifest=null` 保留既有编码行为。显式提供清单后，视觉客户端启用固定的 `exif-RGB-max512-no-upscale-JPEG90-v1` 编码：EXIF 方向校正、RGB、最长边不超过512像素、不放大小图、JPEG质量90。

清单推荐使用对象格式：

```json
{"source-a":{"path":"original-image.jpg","sha256":"<original-bytes-sha256>"}}
```

清单在部署时加载，清单中的相对路径按进程当前目录解析；部署时建议提供原件绝对路径。视觉清单配置是进程级状态，适用于单Container部署；重新配置会清空派生图和编码收据，不承诺同进程多个Container使用不同清单的隔离。每次发图根据实际原始字节哈希绑定来源，编码结果附 `encoded_sha256`。同字节的不同来源身份不能折叠；路径、原文件名或存储别名必须能唯一确定该次来源，歧义拒绝。

只允许原件，以及现有受信任渲染器登记的 `crop`、`numbered_preview` 派生图。派生收据保留父来源、操作、参数和派生字节哈希；未登记或之后被篡改的图片拒绝。多张图片顺序保持调用方给定顺序。

启用时请求增加 `sentrix_image_provenance`，与图片块逐项对应，包含来源身份、原件哈希、操作/参数、编码哈希和策略名；本机路径不进入请求。该字段是适配器与其上游端点的显式扩展合同；端点必须支持校验并在转发供应商前处理它，普通直连端点可不启用清单。

固定编码不保证不同 Pillow/libjpeg 版本产生逐字一致 JPEG。对实际发出字节的哈希与编码依赖版本应由调用方留档。

## 5. 模型和运行预算

预算由 Settings 注入，不写死模型名称或供应商：

- `chat_output_tokens`：主助手输出上限，默认2048，可配置256至32768；实际发送值仍受端点上下文余量约束。
- `agent_max_steps`：默认8，可配置1至64；`agent_wall_time_s`、`chat_timeout_s` 沿用部署配置。
- `vlm_output_tokens`：三个视觉客户端共用的输出上限，默认2048，可配置256至32768。
- `vlm_enable_thinking`：默认 `null`，不发送该扩展字段；显式布尔值原样发送给支持它的端点。
- `vlm_timeout_s`：三个视觉客户端共用的单次超时，默认180秒。

视觉三个客户端是导入观察、照片复核、人物视觉识别。主助手和内部审查使用 `chat_model`；当前 `vlm_enable_thinking` 不配置主助手或内部审查的思考策略。供应商的完整输入计量、流式留存、费用、全流程调用计数和未知请求去重由外部执行层实现，不能由这些产品参数推断已经具备。

## 6. 原子导入与可复用资料快照

应用层保留三段式导入：`ImportAssets.stage(request)` 保存资产，`prepare_image(asset)` 执行准备计算，`finish(staged, prepared)` 提交派生记忆。`prepare_image` 是现有方法名，文本也走这一准备路径；准备结果可由调用方归档，但序列化格式、收据与恢复校验由调用方维护。

`Database.transaction()` 在同一 Database 实例和调用上下文中，为仓储的 `connect()` 提供同一 SQLite 连接。仓储内部 `with connection` 不得提前提交外层事务；调用方可将 `finish` 与自己的来源收据写入放在同一事务中。异常回滚整组 SQLite 写入；内部仓储异常即使被调用方捕获，也会标记事务只能回滚。嵌套事务使用 savepoint。

事务只覆盖 SQLite。原始媒体、外部模型请求台账、准备结果文件和外部向量索引是独立资源；不能把回滚解释为撤销已付费调用。调用方需先归档准备结果及调用身份，顺序提交来源，发布时校验数据库、媒体和向量镜像，再冻结资料并把会话/轨迹另行留存。

当前没有新增通用 HTTP 快照导出/导入、云请求台账或“未知调用自动恢复”接口。复用快照的最低验收应由调用方执行：原件哈希和来源身份一致、派生记忆与向量模型/维度一致、外部索引可重建、失败来源没有半提交派生物、迁移前后来源清单与哈希一致、恢复不再次触发原请求。

## 7. 兼容和仓库同步

`source_text` 迁移默认空串；来源字段迁移允许旧记录为空；旧图片导入、没有外部身份时的去重、未启用冻结限制/视觉清单时的行为保持原状。公开 schema、前端资产类型、接口说明与测试已同步；新增模块和能力已登记模块图及能力映射。行为基线与[偏离及回退记录](../refactor/2026-10-23-comindx-adapter-deviations.md)记载新增行为、原因和schema回退边界。

---

# 改动原因和验证依据

本次把纯文本和混合资料评测需要的产品能力提取为可复用接口。ScriptMem 触发原文读写与文本证据需求，ATM 触发图片/文本并存、外部来源元数据及可追溯视觉输入需求。数据集名称只用于解释改动动机；产品接口和提示词不包含数据集题号、标签、人物、答案或特定机器配置。

## 改动说明

| 类别 | 原问题和触发条件 | 新行为 | 兼容性 | 已有测试依据 |
| --- | --- | --- | --- | --- |
| 文本原文 | 文本作为普通文件导入后，缺少可检索、可精读的原文观察；纯文本任务被图片证据要求阻断 | `source_text` 保存UTF-8原文，接既有文本向量和词法索引；`read_source_text` 按结果集读取；文本证据无需照片附件 | 默认空串，图片观察字段和原图片规划器保留 | `integration/test_text_sources.py`：原文/索引/幂等、scope+handle、JIT、原生回合与L2 |
| 来源身份 | 内容哈希去重会合并同字节但时间、地点、来源身份不同的记录；重跑无法判断冲突 | 可选外部ID，按scope+ID幂等；内容或原始元数据变化409；同字节不同ID独立 | 没有外部ID继续旧内容去重；新增字段为可选值 | `contract/test_import_sources.py`；`integration/test_import_source_transactions.py`：身份、同字节不同ID、元数据冲突、无重准备/重索引；`integration/test_source_identity_races.py`覆盖独立连接并发及外层事务 |
| 时间地点 | 来源提供时间地点可能被导入时刻、主机时区或VLM描述代替 | 原始时间字符串和地点单独保存、返回与索引；不推断时区，不篡改正文 | 旧无身份上传仍保留旧回退行为；当前明确拒绝带时区时间 | 同一事务集成文件中的时间覆盖EXIF、未知时间、拒绝时区、时间/地点召回测试 |
| 混合资料与发布范围 | 单scope含正文和图片时，规划会遗漏另一模态；伪造结果集可能访问跨scope或未发布来源 | 根据资产类型选工具，保留互补取证；按scope/类型/已发布ID再校验；回合只能用自己实际取得的结果集 | 未提供allowlist保持普通上传行为；空allowlist拒绝所有来源 | `integration/test_mixed_sources.py`：元数据与模态、伪造候选、类型检查、JIT、原生混合回合、跨回合结果集、配置拒绝 |
| 冻结资料 | 评测答题期间通过写接口改动语料，会改变后续题的检索范围 | 可选HTTP资料只读模式，仍允许会话与回合；独立来源发布清单约束助手工具 | 默认关闭；不是SQLite只读或新增认证机制 | `integration/test_text_sources.py::test_read_only_corpus_denies_import_but_keeps_conversations`；`integration/test_imported_source_boundaries.py`覆盖全部已注册HTTP写操作，创建会话/回合除外 |
| 视觉字节与预算 | 不同图片编码、裁剪及隐含客户端默认值使同名配置的实际输入不同 | 可选固定编码与来源/裁剪血缘；三个视觉客户端接输出、思考、超时参数 | 不启用清单时沿用旧编码；预算默认值保持；供应商扩展仅在显式启用时发送 | `integration/test_visual_source_provenance.py`：三视觉客户端wire、固定编码、血缘、同字节独立身份、默认兼容；完整代理和台账仍属外部执行层 |
| 原子提交与复用 | 仓储独立提交会让来源记忆已写而成功收据未写；恢复可能重复准备或留下半份派生物 | 共享SQLite事务，prepare/finish分离；调用方将finish与收据原子提交 | 普通connect行为保留；媒体和外部索引不假装纳入SQLite事务 | `integration/test_import_source_transactions.py`：回滚、提交、savepoint、捕获错误后rollback-only；外部执行层另有恢复/发布测试 |

以上测试路径均相对 `apps/api/tests/`，明确标为外部执行层的测试除外。测试应全部使用合成资料和本地假模型；不依赖题库、裁判、云凭据或运行机器。

## 补充边界验证

| 边界 | 已提交的合成测试 |
| --- | --- |
| HTTP导入和旧库迁移 | `test_imported_source_boundaries.py`：时间/地点/元数据/文件名冲突、JSON键序、空白ID、跨scope独立、旧库升级后默认值；`test_import_sources.py`：内容冲突和非法JSON/NaN/带时区输入 |
| 全文与结果集 | `test_imported_source_boundaries.py`：非UTF-8、空白、缺观察的稳定失败；超过2万字符的Unicode/CRLF完整保存和读取；纯文本回合拒绝另一回合结果集。混合回合对应验证在 `test_mixed_sources.py` |
| 冻结范围 | `test_imported_source_boundaries.py`：空清单拒绝所有来源、缺失/非法清单fail-closed、全部已注册HTTP写操作403、读和问答继续；实际数据库和直接资产接口访问边界见第3节 |
| 视觉与预算 | `test_visual_source_provenance.py`：三个视觉客户端的实际wire参数、默认兼容、EXIF/尺寸/编码哈希、受限派生图及篡改拒绝、同字节来源身份歧义、发图顺序、不泄露本机路径、配置范围和环境变量 |
| 时间兼容 | `test_source_time_compatibility.py`：旧带时区与新无时区来源按正反序导入，不发生隐式时区转换或半提交；相同时间类型原生归并保留；时间窗检索不比较未知时区 |
| 并发来源身份 | `test_source_identity_races.py`：两个独立SQLite连接在读取身份后同时提交；相同输入幂等、不同内容或元数据冲突、同内容异ID独立、竞争不污染外层事务，无关完整性错误照常失败 |
| SQLite原子提交 | `test_import_source_transactions.py`：多仓储共享提交、整体回滚、savepoint、捕获仓储异常后仍rollback-only；只覆盖SQLite，不把媒体或付费调用算进事务 |
| schema回退 | `test_source_adaptation_rollback.py`：新副本回退和再次升级、旧记录保留、拒绝覆盖输出、拒绝丢失已使用来源字段、拒绝越过后续迁移 |

所有路径均相对 `apps/api/tests/integration/`，表中明确标为contract的文件除外。
来源快照导出、原请求台账及未知请求不重发由调用方验证；本PR没有复制实验控制器或数据集，
不宣称提供了通用HTTP快照或云预算服务。正文完整存储及工具完整返回也不代表整篇一定能进入
某一模型窗口；输入上下文不足时仍受既有压缩/超窗失败机制约束。

## 结构和公开契约同步

新迁移、域字段、仓储、HTTP摘要、OpenAPI与前端资产类型同时更新；工具声明、JIT、证据账本、
L1/L2、行为基线和偏离记录同步。配置经Settings和bootstrap装配，保留上游最新的Mac预设、
上下文压缩、安全余量、遥测及导入进度。新增预算与资料只读状态进入非密钥运行诊断。

本次使用离线自动测试验证接口，不启动已停止的资料构建或真实模型。真实环境专项、487QA与
多设备发布验证按仓库既有要求在合入/发布前执行，不能将合成测试通过当作模型质量对比结果。


## 本次提交验证（2026-10-23）

- 日常后端回归：`make coverage`，3043项通过，49项真实环境专项未运行；domain/application覆盖率94.28%，阈值85%保持。
- Sentrix前端：143项测试、类型检查、lint和生产构建通过；PhotoBench前端：478项及对应检查/构建通过。
- Ruff及格式、11项分层契约、两应用可达性/端口可达性、模块图、能力地图、两份OpenAPI一致性均通过。
- 严格类型检查未全绿：317份源码检查中，上游未改动的 `infrastructure/models/face_detector.py`
  第86、98行在NumPy2.3.5/mypy1.20.2环境报 `no-any-return`。已将同一配置和依赖用于
  `origin/main` 对应模块对照，复现相同两处错误；该文件与基底逐字一致，本次未修改人脸实现或放宽类型门禁。

自动测试运行于Python3.12.14的独立环境，复用的旧依赖只读，未修改实验冻结包。
真实模型、49项真实资源专项、487QA及多设备发布回归未执行；本PR保持草稿，不将离线验证当作模型质量结论。
