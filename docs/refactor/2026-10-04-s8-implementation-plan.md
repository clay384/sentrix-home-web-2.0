# S8 实施计划：音频与视频

> 路线图出处：设计文档 `docs/architecture/2026-09-21-sentrix-home-web-2-implementation-design.md:513`
> ——「**S8 | 音频与视频 | FunASR 音频、关键帧/场景切分、派生帧可回溯原视频秒数 |
> 导入视频，场景成型、能跳回原片**」。它是 **M3（S8–S11）** 的第一段。
>
> 前置：S0–S7 全部完成；W1（检索换 wemm 统一嵌入）完成。

> **状态（2026-10-04）：S8 已收口。** S8a（T501–T506）· S8b（T511–T514）· S8c（T521–T523）
> 三段全部落地，迁移 `0010_video.sql` 一个字未改、全程零新依赖。S8c 的施工记录在 §4.7。

---

## 0. 动工前双侧核查（2026-10-04）

### 0.1 1.0 里 S8 是四块，**体量极不均匀**

| 块 | 内容 | 体量 |
|---|---|---|
| ① 视频元数据 + 入库 | ffprobe、浏览器预览转码、拍摄时间/GPS 继承、状态机 | 中 |
| ② **抽帧 + 场景切分** | YOLO/Pose 预筛 → 事件合并 → Katna 质量门 → 每场景落代表帧 | **最大** |
| ③ **场景入库与回溯** | `video_scene` 事件 + 派生帧资产 + `source_timestamp_sec` 血缘 + 前端 seek | 中，**验收核心** |
| ④ FunASR 音频转写 | 进程内 paraformer-zh → `observations.transcript` → 拼进文本向量 | 很薄 |

**主导设计（也是最省力的复用路径）：派生关键帧是 `media_type="image"` 的新 Asset，
直接喂进现有图片流水线**（1.0 `video/processor.py:188,401`）。于是白得
Observation、向量、人脸、事件归并。**推论：wemm 不需要 `embed_video`**——关键帧是图片。

### 0.2 2.0 现状：视频**能进库但完全静默空转**

- `MediaType` 已有 `VIDEO` / `AUDIO`，分类是通的（`domain/media/models.py:15-43`）；
- `POST /media/import` **没有任何 media_type 白名单**：视频被收下、落盘、建
  Asset + Observation（`application/media/import_assets.py:161-213`），
  **但不读时长、不抽帧、不转写**，`Observation` 语义全空；
- `GET /assets/{id}/preview` 对视频 **500**——`PillowPreviewEncoder` 打不开视频；
- 只有 IMAGE 才读 EXIF（`import_assets.py:179-180`），视频拿 `(None,None,None)`；
- 整册导入更早就过滤掉了视频（`import_album.py:42` 的 `IMAGE_SUFFIXES`）；
- **2.0 源码里 `keyframe` / `ffmpeg` / `FunASR` 零命中**，没有任何视频代码。

**这是与 W1 那次同类的状态**：看着正常（能导入、能列出），实际什么都没发生，且不报错。

### 0.3 环境（153 实测）

| 项 | 状态 |
|---|---|
| `ffmpeg` / `ffprobe` | ✅ 4.4.2（`/usr/bin`） |
| `funasr` | 1.4.1，在 **1.0 的 venv** 里；**2.0 的 `.venv` 没有** |
| 别的 ASR | **没有**（whisper 系无、`~/models` 无 ASR 模型、8100 的 qwen35 不含音频） |

### 0.4 1.0 的已知缺陷（**都不照搬**）

1. **前端关键帧计数漏算**：导入页统计用 `derived_kind === "video_keyframe"`，而默认算法
   产出的是 `video_keyframe_webp` → 那个面板**永远显示 0**（`src/app.js:742`）。
2. **唯一血缘索引只覆盖 legacy 一种 `derived_kind`**（`db.py:1054-1056` 的
   `WHERE derived_kind='video_keyframe'`）→ 当前默认算法**没有 DB 级幂等保证**。
3. **视频场景摘要不刷新**（`db.py:5576` 对 `source_type='video_scene'` 跳过重建）。
4. **双抽帧算法并存**（`worldmm` legacy vs `hybrid_webp` 现用）、两套 `derived_kind`、
   两套输出格式、一堆兼容代码。

---

## 1. 裁决

### 用户 2026-10-04 定的三条

| # | 问题 | 裁决 |
|---|---|---|
| ① | 抽帧那整套怎么移 | **先做简化闭环**——用 ffmpeg 抽帧 + 按时间间隔切场景，把「场景成型 + 能跳回原片」这条验收链跑通。**血缘三件套与「派生帧走图片流水线」照搬**，日后换更好的抽帧算法时不动其他部分 |
| ② | FunASR 在 2.0 跑在哪 | **不要 FunASR**。进一步确认：**音频不转写，只做入库与播放** |
| ③ | S8 怎么切 | **切三段**：S8a 血缘 → S8b 抽帧 → S8c 音频 |

### ②产生的偏离（**必须登记**）

设计文档 §8 把 S8 定义为「**FunASR 音频**、关键帧/场景切分、派生帧可回溯原视频秒数」。
本计划**去掉转写**：

- `observations` **不加 `transcript` 列**；
- 文本向量拼接（`index_observation_text.compose_text`）**不含转写**——
  1.0 那一版是八字段含 `transcript`，2.0 保持七字段；
- 音频的价值面收窄为：能导入、能读元数据（时长等）、能播、能按元数据/文件名检索。

**登记在 `docs/refactor/s8-deviations.md`**（与 s3/s4/s5 同体例），并在
`behaviour-baseline.md` 的 S8 段写明「转写不在本切片」。

### 我按证据定的（沿用 1.0 的设计，不另问）

| # | 决定 | 依据 |
|---|---|---|
| ④ | **派生帧做成 `media_type="image"` 的新 Asset，走现有图片流水线** | 1.0 就是这么做的（`processor.py:188,401`）。这是视频白得人物/OCR/向量/事件能力的唯一原因，也是最省力的路径 |
| ⑤ | **血缘三件套照搬**：`assets.source_timestamp_sec` + `parent_asset_id` + `derived_kind`，配 `events.source_start_sec` / `source_end_sec` + `(source_asset_id, source_scene_index)` 唯一约束 | 这是「派生帧可回溯原视频秒数」的唯一实现，前端 seek 与助手交付原片都靠它 |
| ⑥ | **唯一血缘索引要覆盖所有 `derived_kind`** | 1.0 那个索引只覆盖 legacy 一种（§0.4 缺陷 2），默认算法实际没有幂等保证。这是**修缺陷不是偏离** |
| ⑦ | `/api/assets/{id}/file` 的 **preview 分流**照搬 | H.264 直出、其余 ffmpeg 转码（1.0 `app.py:2448-2478`）。浏览器能播的前提 |
| ⑧ | **只保留一条抽帧路径** | 不要 worldmm/hybrid 双算法并存的包袱（§0.4 缺陷 4） |
| ⑨ | **wemm 不加 `embed_video`** | 关键帧是图片，走 `embed_image` 即可（④的直接推论） |

---

## 2. 三段

### S8a 视频入库与血缘

**验收**：导入一个真实视频，能读到时元数据、能在浏览器里播、`/videos/{id}` 返回得出来。

| # | 内容 |
|---|---|
| T501 | 迁移 `0010_video.sql`：`assets` 加 `parent_asset_id` / `derived_kind` / `source_timestamp_sec` / `source_frame_index` / `source_scene_index`；`events` 加 `source_type` / `source_asset_id` / `source_scene_index` / `source_start_sec` / `source_end_sec`；**唯一血缘索引覆盖所有 `derived_kind`**（裁决⑥） |
| T502 | ffprobe 元数据适配器（`infrastructure/media/video_metadata.py`）。**落 infrastructure**：`application-purity` 契约禁止 application import 子进程/PIL 那类 |
| T503 | 浏览器预览转码（非 H.264 才转，libx264/crf23/+faststart），与 `/assets/{id}/file` 的 preview 分流（裁决⑦） |
| T504 | 视频 4 条读路由 + `reprocess`：`GET /videos/{id}`、`GET /videos/{id}/scenes`、`GET /video-scenes/{id}`、`POST /videos/{id}/reprocess`。**照 1.0 的响应形状**（`{video, scenes}` / `{video_asset_id, scenes}` / 事件详情 / `{accepted, asset_id, status}`） |
| T505 | `POST /media/import` 遇到视频**不再静默空转**：至少读元数据 + 进状态机。同时修掉 `GET /assets/{id}/preview` 对视频 500 那条（走 T503 的分流） |
| T506 | 导入页/整册导入放行视频（`import_album.py` 的 `IMAGE_SUFFIXES`） |

### S8b 抽帧与场景

**验收**：导入视频后**场景成型**，派生关键帧能在时间线/检索里出现，**每张能定位回原片秒数**。

| # | 内容 |
|---|---|
| T511 | 关键帧抽取（**简化闭环**，裁决①）：ffmpeg 按固定间隔或场景检测抽帧。抽帧参数登记进 `behaviour-baseline.md` |
| T512 | 场景切分：先按时间间隔切，每个场景建一条 `source_type='video_scene'` 的事件，区间写 `source_start_sec` / `source_end_sec` |
| T513 | 派生帧落库为 `image` 类型的 Asset（带 `parent_asset_id` / `derived_kind` / `source_timestamp_sec` / `source_scene_index`），**送进现有图片流水线**（裁决④） |
| T514 | 幂等与清理：重复导入靠唯一血缘索引 + `cleanup_video_derivatives`；**索引覆盖所有 kind**（裁决⑥） |
| T515 | 视频与音频的媒体类型过滤在检索侧已通（`search.py:127`、`query_contracts.py:321-324`、`fact_query.py:47`）——只做验证，不改 |

### S8c 音频

**验收**：导入一个音频文件，能读时长、能在浏览器里播、能按文件名/元数据检索到。

| # | 内容 |
|---|---|
| T521 | 音频走同一条导入链：ffprobe 读元数据（时长/码率/采样率）、预览与播放分流 |
| T522 | 确认检索侧 `media=audio` 过滤可用；**不引入转写**（裁决②） |
| T523 | 收口：capability-map / module-map / behaviour-baseline / `s8-deviations.md` / openapi |

---

## 3. 门禁与交付物

每个新 `.py` 都要登 `docs/architecture/module-map.md`；新能力登
`docs/refactor/capability-map.jsonl`（现在 150 行，**零条视频/音频**）；新常量登
`behaviour-baseline.md`；加了路由必须重跑 `export_openapi.py`。

**不能破的契约**：`domain-purity` 禁 PIL/httpx/子进程 —— **视频解码与 ffmpeg 只能落
infrastructure**；`application-purity` 同理。

---

## 4. 施工记录

### 4.1 S8a 完成（2026-10-04）

T502–T506 全部落地。新增 9 个文件（ffprobe 适配器、转码器、血缘仓储、4 条路由 + schema、
4 个测试文件），修改 21 个（含 module-map / capability-map / behaviour-baseline /
`s8-deviations.md` §4 / 重跑 openapi）。**迁移 `0010_video.sql` 一个字未改**，
未引入任何新依赖。

**定点变异 11/11 被杀**：creationdate 标签优先级 · ISO6709 解析 · H.264 判据 ·
`?original=1` 分流 · 探测结果不落库 · 探测失败改成抛 · 整册放行视频 ·
reprocess 不清派生数据 · 场景过滤 · 血缘列读错 · `/preview` 回退 Pillow。

**`make ci`**：`1 failed / 2039 passed`，唯一失败是那条已知的模型侧 flaky；
coverage 97.79%（门 ≥85%）；web-check 单独补跑绿。

### 4.2 独立验收（我自己跑的，不复用实施方的结论）

真 ffmpeg 造视频 → 真 app：

```
导入        media_type=video · status=processed · video_probed=true · deduplicated=false
GET /videos/{id}        {video, scenes}；元数据读对：3.0 秒 / mpeg4 / 12fps / 352x288
GET /videos/{id}/scenes {video_asset_id, scenes: []}   ← 空是对的，场景写入者属 S8b
/file 默认              200 video/mp4  109078B   ← 转码出的预览
/file?original=1        200            166443B   ← 原片；大小不同即证明分流生效
/preview                200 video/mp4  109078B   ← **原先这里是 500**
reprocess               202 {accepted, asset_id, status: "video-queued"}
```

**计划书 §2 里 S8a 的三条验收（读到时元数据 / 能在浏览器里播 / `/videos/{id}`
返回得出来）都成立**，另外顺手修掉了那条存量 500。

**一处小观察（不阻塞）**：`?original=1` 这次回的 `content-type` 是
`application/octet-stream` 而不是 `video/quicktime`——它取决于上传时探测到的
`mime_type`。浏览器仍能播（默认那条路径给的就是 `video/mp4`），`?original=1`
是逃生口，影响面小。记在这里备查。

### 4.3 一处门禁的反向规则又生效了

`scripts/check_method_reachability.py` 里有一条 PENDING 登记：

```
"AssetRepository.update_status": "S6 · 依赖核查（资产状态流转，归属未定）"
```

S8a 的 `POST /videos/{id}/reprocess` 认领了它（`routes/videos.py` 重置资产状态），
按反向规则删掉。**这是「登记过的未接线方法」被后续切片追上的一次**——
与 S7 那几次同一个形状。

### 4.4 S8b 完成（2026-10-04）

T511–T514 落地。新增 3 个源文件（`domain/media/scenes.py` 切窗规则、
`infrastructure/media/video_keyframes.py` 抽帧器、`application/media/extract_video_scenes.py`
用例）与 3 个测试文件，修改 17 个。**迁移零改动、零新依赖。**

**两条新常量——它们不是 1.0 的值**（算法按裁决①换了，1.0 没有对应物）：
`SCENE_WINDOW_SEC = 60.0`（场景窗口）、`MAX_SCENES = 40`（总场景数封顶，
超限按比例放大窗口而不是无限抽）。封顶的理由是代价：**每个关键帧要走完整图片
流水线（VLM 观察 + wemm 嵌入 + 人脸），帧数就是 VLM 调用数**。

**定点变异 6/6 被杀**。`make ci`：1 failed（已知模型侧 flaky）/ 2056 passed，
coverage 97.73%；web-check 单独补跑绿。

**三处值得记的实现决定**：

1. **视频场景不豁免摘要刷新。** 1.0 `db.py:5576` 的
   `source_type != 'video_scene'` 判据**没有搬**——它导致场景摘要只在导入时算一次、
   后续补齐无法更新。2.0 走 `refresh_participants` + `apply_summary`，视频场景照常刷新，
   有专门用例钉住。
2. **关键帧固定 `observe=True`，且用另一个 `ImportAssets` 实例。** 视频的语义整个都在
   关键帧上，跟随单张上传的快路径会让视频一条语义都不进库；用独立实例则从根上
   避免「关键帧再抽关键帧」的递归。
3. **清理级联补全。** S8a 只清到「S8a 能产生的那几行」（当时那些行根本不存在），
   S8b 把关键帧真的走完流水线后多出的人脸/提及/地点/向量/词法投影那几层一起接上。

### 4.5 独立验收（我自己跑的）

真 ffmpeg 造 **170 秒**视频（60 秒窗口应切 3 个场景）→ 真 app：

```
导入    video_probed=True  video_scenes=3  deduplicated=False
场景    0–60s / 60–120s / 120–170s          ← 正是 60 秒窗口的算法

真库里的血缘：
  派生帧 ts=30.0 / 90.0 / 145.0   frame=0/1/2   scene=0/1/2
         parent 全部指向同一个源片
```

**中点取帧算得准**：三个窗口 [0,60] [60,120] [120,170] 的中点正是 30 / 90 / 145。
**计划书 S8b 的验收「场景成型、能跳回原片」成立。**

**一次自己的失误值得记**：第一次验收报告 `video_scenes=None`——因为 **9598 跑的还是
S8a 那版代码**（我重启过它做 S8a 验收，S8b 改完没重启）。第二次又撞上内容哈希去重
（`deduplicated=true`，同一个视频导过两遍）。**真机验收必须用当前代码 + 每次换视频内容**，
这两条以后直接写进验收脚本。

### 4.6 测试数据清理

S8a/S8b 的验收都往**真实库**里写过测试视频（用 `SENTRIX_DATA_DIR=$PWD/data` 跑真 app）。
已清理并验证：

```
assets 57 → 53 · events 21 → 17 · observations 57 → 53
PRAGMA foreign_key_check: 干净 ✓    残留视频 0 · 残留派生 0
```

清理前先核过「这 4 个测试视频各自建了自己的事件、每个只挂自己那一条观察」，
所以删它们碰不到真实事件——脚本里也留了这道安全闸（发现事件上还有非测试观察就拒绝删）。
引用方清单来自 `PRAGMA foreign_key_list`，不是靠猜。

### 4.7 S8c 完成（2026-10-04）

T521–T523 落地。新增 6 个文件（`infrastructure/media/ffprobe.py` 视频/音频共用的 ffprobe
子进程与标签解析、`infrastructure/media/audio_metadata.py` 音频元数据适配器、
`infrastructure/persistence/repositories/audio.py` 探测结果落库、3 个测试文件），修改 14 个
（含 module-map / capability-map / behaviour-baseline / `s8-deviations.md` §5 / 重跑 openapi）。
**迁移零改动、零新依赖。**

**音频只做入库与播放，不转写**（用户裁决②）：`observations` 不加 `transcript` 列，
`compose_text` 保持七字段。元数据（时长/编码/采样率/声道/码率）走 ffprobe，拍摄时间/GPS
接到与 EXIF/视频同一条路，结果写进 `assets.metadata_json` 的 `audio_metadata`。交付不转码
（浏览器原生放 mp3/wav/m4a/aac/flac/ogg），**content-type 由后端按后缀定**——音频没有转码件
这条退路，不能重复 S8a 观察到的 `application/octet-stream` 问题。

**定点变异 8/8 被杀**（清单见 §4.8）。**`make ci` 全绿**（那条已知的模型侧 flaky 除外）。

### 4.8 S8c 的定点变异

| # | 改坏什么 | 红了哪条 |
|---|---|---|
| 1 | 删掉 `execute` 里的 `audio_probed` 赋值 | `test_importing_audio_reports_that_it_was_probed` |
| 2 | `_probe_audio` 的异常改成往上抛（阻断导入） | `test_audio_probe_failure_is_recorded_not_swallowed` |
| 3 | 探测失败也写 `audio_metadata` 键 | `test_a_failed_probe_keeps_the_reason_and_no_metadata` |
| 4 | `audio_mime_type` 直接返回 `declared` | `test_the_delivery_content_type_is_playable_not_octet_stream` |
| 5 | `audio_mime_type` 对具体 `audio/*` 也按后缀覆盖 | `test_a_specific_audio_content_type_is_kept_as_is` |
| 6 | `AudioMetadata` 的时长改取流而非容器 | `test_reads_duration_codec_sample_rate_channels_and_bit_rate` |
| 7 | 音频分支写成 `media_type is not IMAGE`（图片也进） | `test_image_and_video_never_touch_the_audio_chain` |
| 8 | `SCAN_SUFFIXES` 去掉 `AUDIO_SUFFIXES` | `test_audio_is_scanned_alongside_photos_and_videos` |

### 4.9 S8 收口

S8a/S8b/S8c 三段全部完成，S8 的四条验收（读到时元数据 / 能在浏览器里播 / 场景成型能跳回
原片 / 音频能导入能播）都由真机端到端独立验过。**唯一未交付的一项是按用户裁决去掉的
FunASR 转写**（`s8-deviations.md` §1.1），它不是遗漏，是裁决。
