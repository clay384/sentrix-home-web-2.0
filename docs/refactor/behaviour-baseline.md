# 1.0 行为基线

基线提交 `2d2cee7cbf1023f5019b9aaa9061af9c4cf45afa`。
2.0 重写时必须保持这些数值与规则不变，除非明确决策要改。

## 人脸

| 项 | 值 | 位置 |
|---|---|---|
| 在线匹配余弦阈值 | 0.30（硬编码，无 env 可配） | `backend/db.py:4815` |
| 全局重聚类阈值 | 0.30 | `backend/face_clustering.py:67` |
| 质量阈值 | 0.30 | `backend/face_clustering.py:67` |
| **身份向量模型** | **buffalo_l / `w600k_r50.onnx`，112×112，`(x−127.5)/128.0`，512 维** | `backend/model_clients.py:1690-1716`、`:1772-1787`、`:1799-1840` |
| 人脸检测 | RetinaFace r50 ONNX，BGR + mean 104/117/123，priors `STEPS=[8,16,32]`、NMS 0.4、置信筛 0.05；**两趟**（粗 1024/128，空了才细 640/256 且阈值降到 0.1） | `backend/face_detector.py:1-181` |
| SCRFD 复核闸门 | `FACE_MIN_SIZE=64`、`FACE_VERIFIED_SCRFD_SCORE=0.7`、`FACE_VERIFIED_MIN_AREA=0.003`、`FACE_IDENTITY_MIN_QUALITY=0.55`；**只有 verified 才准建簇** | `backend/model_clients.py:1718-1840` |
| 贪心多视角聚类的六个默认参数 | `threshold=0.30` / `prototype_threshold=0.30` / `prototype_limit=6` / `model_identity="all"` / `max_prototypes_per_view=3` / `minimum_quality=0.1` | `backend/face_clustering.py:59-66` |

> **2026-09-23 实测补齐（S5 Task 69）**。上一版这张表只记了三个阈值，而实施时逐行读
> `face_clustering.py` / `face_embeddings.py` 并做变异检查（32 处变异，31 处被测试杀掉）
> 之后，实测出下面九条**表里没写、但不写就会做错**的行为。前八条是 1.0 的真实语义，
> 第九条是 1.0 的缺陷，移植时照抄并单独登记。
>
> **0. 身份向量是 buffalo_l，不是 AdaFace——这一条是更正。**
> 本文件从写下第一版起就没有出现过 AdaFace（`git log -p --all -- docs/refactor/behaviour-baseline.md`
> 对 `adaface` 零命中），但项目里一直流传着「AdaFace 是人脸身份向量」的说法
> （`scripts/runtime/start_sentrix_api_8091.sh` 里导出 `FACE_EMBEDDING_MODE=adaface`
> 与四个 `ADAFACE_*`，环境的 brief 也照抄了它）。**实测那是死配置**：
> `face_embedding_is_adaface()` 全仓零调用；`FaceAdapter.__init__` 把 `identity_adapter`
> 设为 `None`（`model_clients.py:1629`）之后再无第二处赋值，唯一调用点 `app.py:2299`
> 必抛 `AttributeError` 并被上一行的裸 `except Exception: return []` 吞掉；
> 1.0 自己的测试把它标成
> `@unittest.skip("legacy identity_adapter contract removed; identity is fixed to buffalo_l")`
> （`tests/test_face_clustering.py:126`）。真正写出 `embedding_model` 的是
> `_detect_retina_tiled_unlocked` 的两个分支，**都写死 `"buffalo_l"`**。所以这张表现在
> 有了一行确切的值，而那一行不是 AdaFace。见 `s5-deviations.md` 证据 1/2/3。
>
> 1. **`continue` 与 `break` 等价。** `_best_compatible_cluster` 的候选簇已按
>    `(-score, cluster.id)` 降序排过，第一个低于阈值的后面必然都低于阈值。1.0 用的是
>    `continue`，照抄。**这条写不成测试**——把 `continue` 改成 `break` 没有任何用例会红，
>    变异检查里它是 32 处变异中唯一存活的那处。只能靠读代码，如实登记为「未被门禁覆盖」。
> 2. **「同姿态桶为空 → 直接命中」不是无门槛的。** 阈值检查（`cluster.best_score` 对
>    **原型**取最大）发生在 `_same_view_members` **之前**；空桶分支绕过的只是 `_matches`
>    （逐成员比对），不是阈值本身。
> 3. **承 2，后果比想象的强。** 一个 `profile_left` 新样本只要与簇里任一**原型**够像，
>    就能无条件并入，**哪怕它与簇里某些成员的余弦是 −0.64**。1.0 自带的
>    `test_frontal_and_profile_views_share_multi_view_cluster` 正是靠这条把侧脸并进只有
>    正脸的簇。这同时划定了「`unknown` 必须匹配全部成员」这条防线的**真实适用范围**：
>    它只对 `pose_bucket == "unknown"` 的样本生效；姿态桶确实存在、只是不等于新样本姿态的
>    样本，走的是另一条路。**这是本表里最容易做错的一条**——按「相似度必须过人脸级阈值」
>    去重写，会把多视角簇拆成两个。
> 4. **`noise=True` ⟺ 单例 + 低于质量门。** 低于质量门的样本进不了匹配，而 noise 簇又被
>    排除出候选。它仍然会进 `result.clusters` 与 `labels`，**不是被丢掉**。
> 5. **`_update_prototypes` 的排序 / 截断只在追加新桶时跑。** 同桶原地替换直接返回、不重排
>    （替换只在 quality 变大时发生，而排序键首项就是 `-quality`，所以行为等价）。
> 6. **`p10` 的退化条件是 `len(sims) <= 2`**——两个成员也退化，不是 `< 2`。
> 7. **模型身份与 `protected_cluster` 都是整簇否决**，不是按成员过滤：签名对不上就整簇跳过。
> 8. **1.0 的四个纯函数里一行 `os` 都没有。** `os.getenv("ADAFACE_*")` 全在被判死的
>    适配器里，不在 `normalize_embedding` / `pose_bucket` / `compute_face_quality` /
>    `FaceClusterer` 里。所以移植这四个函数**不会产生任何 env 派生常量**。
> 9. **`compute_face_quality` 最后那次 `_clamp` 是恒等。** 四项都非负、权重和为 1.0，
>    上界恰好 1.0——那次 clamp **不可达**。照抄保留（改动它等于改掉一个不可达分支，
>    收益为零而差异真实存在）。
> 10. **`recluster_faces(threshold=0.30, minimum_quality=0.55)` 的 `minimum_quality`
>     与 `FaceClusterer` 自身默认的 `0.30` 不一致。** 1.0 的「两个 0.30」之外还有第三个
>     数值 0.55，只出现在那个 benchmark-only 的函数里，别把它当成人脸质量门的值。
>
> **移植时唯一一处字节级偏离**：2.0 的 `cosine` 给 `zip(...)` 加了 `strict=True`
> （ruff 的 B905 要求；长度已在上一步判断过，行为不变）。

## 检索融合

| 项 | 值 | 位置 |
|---|---|---|
| 默认策略 | `weighted_rrf` | `configs/retrieval/defaults.json` |
| RRF_K | 60 | `backend/retrieval/fusion.py:21` |
| anchor 加成 | +1.0 | `backend/retrieval/fusion.py:22` |
| 通道权重 | visual 2.5 / lexical 1.0 / metadata 1.0 / entity 1.0 / text 0.5 / adjacency 0.5 | `backend/retrieval/fusion.py:37` |
| adjacency | `kind="expander"`，默认关闭，`retrieve()` 恒返回空 | `backend/retrieval/adjacency.py:34`、`:47` |

> **2026-09-22 核对（S4c Task 64）**：接线时把这张表逐条对回 1.0，**没有发现不符**——
> 七个权重、`RRF_K=60`、anchor `+1.0` 与 `backend/retrieval/fusion.py:21-22`、`:37-44`
> 逐值相同，2.0 的 `domain/retrieval/fusion.py` 只是把 `enumerate` 换成 `hit.rank`、
> 同分按 `asset_id` 稳定排序（两处都已在文件注释里记明）。补记三条这张表原先没写、
> 但接线必须照搬的：
>
> - **通道进融合的顺序**是 `metadata → entity → lexical → visual_ann → text_ann`
>   （`backend/retrieval/__init__.py:79`）。顺序是语义：`fusion.fuse` 在候选第一次
>   出现时按那个通道定 `evidence_class`（`backend/retrieval/fusion.py:86-95`），
>   metadata 不是第一个就会丢掉 anchor 的 `+1.0`。
> - **adjacency 不进并行召回**：`_retrieve_multi` 的召回循环跳过
>   `kind == "expander"` 的通道（`backend/evidence_retrieval.py:198-201`），
>   Kernel 拿 primary 融合结果的 exact/strong 当种子再 `expand`
>   （`:246-273`），扩展结果 append 在主结果之后。默认配置里它连实例都不建
>   （`configs/retrieval/defaults.json:10` 的 `"adjacency": false`）。
> - **缺通道的形态是「不在列表里」**（`backend/retrieval/__init__.py:79-84` 只对启用
>   的通道调工厂），不是「构造一个恒空的通道」。`channel_hits` 里没有那个键。
> - **每条通道的召回条数**抬到 `max(requested_limit, authorized_count)`
>   （`backend/evidence_retrieval.py:176-181`），融合前不按交付上限裁
>   （理由写在 `:171-176`）。2.0 的等价物见 `s4-deviations.md` §8。

> **2026-10-03 W1 追记（换 wemm 之后）**：上面这组通道权重（`visual_ann` 2.5 / `lexical` 1.0 / `text_ann` 0.5）来自 **chinese-clip 时代**的 R7 消融（当时视觉 r10=0.887、词法 0.373、文本 0.158）。**换成 wemm 后没有重跑那次消融**——1.0 自己也
> 没重跑（`backend/retrieval/fusion.py:33-42` 开了 wemm 后权重一字未改，连 R7 的注释都
> 原样留着）。所以这里照搬权重 = 1.0 现状 = **用户验证过的那个配置**；但要清楚它是
> **继承来的未验证假设**，不是「在 wemm 下重新验过的结论」。真要让权重与 wemm 匹配，
> 得单跑一次消融——那是独立的一件事，不在 W1 范围内。改任何一个数字前先改这里。

## 助手

| 项 | 值 | 位置 |
|---|---|---|
| 默认 profile | `goal_driven_candidate` | `backend/agent_runtime/profile.py` |
| profile 预算 | 8 步 / 6 次调用 / 180s | `backend/agent_runtime/profile.py` |
| `max_search_calls` 硬收敛 | 1 | `backend/agent_runtime/runtime.py:1895` |
| 本地模型 max_tokens 上限 | min(1501, 4400 - prompt_tokens)，下限 64 | `backend/app.py:2680-2692` |

> **2026-10-09 修正（§3.3）：`4400` 那个上限不再照搬，下限 64 被删掉。**
> `4400` 是照 **1.0 自己**的 `max_model_len = 4501` 算的（1.0 注释原文：「Phase H H4：
> max_model_len=4501 的硬上限保护」）。2.0 换了端点，窗口是 **6144**
> （`--ctx-size 24576 ÷ --parallel 4`），常量没跟着换——于是 `prompt > 4336` 时公式给
> 负数、被下限 64 抬起来，**发出一个「窗口其实装得下、但输出只够写半个 JSON」的请求**，
> 端点回 500（解析失败）或 400（超窗），而用户看到的是「模型服务不可用」。实测两轮
> 487 题里 22 道是这么来的。现在窗口由 `Settings.vlm_context_tokens` 给（**部署事实**，
> 与「模型名问端点」同一条理由），剩余不足 `MIN_USABLE_OUTPUT_TOKENS = 256` 时**不
> 发请求**，抛 `PromptTooLongError` 让调用方压缩历史再试。

> **2026-10-09 修正（§3.4A）：工具计价口径**。1.0 只把**执行成功**的调用计入工具预算
> （`tool_policy.py:61-62`），2.0 此前照抄。**那条口径的前提是 1.0 在被拒时
> `break` 结束回合**（`runtime.py:3318`），拒绝不会再来一次；2.0 把拒绝当普通工具结果
> 发回、允许重试，于是同一件事**照常烧掉一个模型步**（实测 762 次被拒里 426 步是
> 「整步全被拒」，占 2667 步的 16%）。2.0 改成数**所有工具往返**（`tool_round_trips`），
> 由此 `6(往返) + 1(作答) + 1(L2) = 8 = max_steps` 这条不变量才真的成立——此前只是
> 假成立。**这是一处有意的偏离，不是对齐 1.0。**

### 2026-10-09：助手链路的七处修正

依据 `docs/refactor/2026-10-08-assistant-fixes-implementation-plan.md`。**只有这一节是
2.0 的当前行为**，上面各表是 1.0 的记录。

| # | 项 | 1.0 | 2.0 现在 | 为什么 |
|---|---|---|---|---|
| §3.1 | 检索 preview 的字段 | `_RETRIEVAL_PREVIEW_KEEP` 7 个字段 + 复合 `evidence_summary`（caption → activity → place → objects[:8] → clothing[:8] → 文字：…，单段 120 字 / 整串 300 字上限） | **补齐同一套**，另加 `media_kind` / `level` / 顶层 `can_inspect` | 2.0 只暴露了 `caption` 一个列，而 `Observation` 上有八个。`objects` / `clothing` / `ocr_text` 恰恰是模型要调 `inspect_photo` / `read_photo_text` 才拿得到的东西——精读从 0.97 次/题涨到 3.4 次/题，撞上 `max_inspections = 3` 之后成片被拒（拒绝率 1.4% → 26.7%） |
| §3.1 | preview 的 `level` | `exact` / `strong` / `approximate`，判据是「是不是事件成员」（`tools.py:1770`） | **同三个词，判据换成 `fusion.evidence_class_for`**：`anchor → exact`、`semantic → strong`、`expander → approximate` | 1.0 那个判据在 2.0 没有对应物；而 2.0 的融合层一直在算 `evidence_class`，只是 `RetrieveEvidence` 转 `EvidenceHit` 时把它丢了 |
| §3.1 | preview 每页条数 | `preview[:5]` | **12**（`ResultSet.PAGE_SIZE`，等于 `MAX_SEARCH_LIMIT`） | 1.0 那个 5 是它自己的上限裁切；2026-10-27 由 6 调到 12：检索上限就是 12，一页给全后模型不再需要翻页（6 张一页时几乎每题多一次 `get_result_page` 模型步，Mac 实测约 16.9s） |
| §3.1 | preview 缺失时的键 | 键恒在、值取空 | **同** | 空值的意思是「这张图没记录到」，缺键会让模型以为链路坏了 |
| §3.2 | 每一转发几份工具契约 | JIT，`LITE_TOOL_SCHEMAS` 每工具 <40 token，单步 150–300 token | **JIT**，`ToolSpec.brief`（≤40 字）+ `jit.select_tools`。实测第一转 863 字符 / 检索后 1839 / 交付题 1246（全量是 5509） | 2.0 原先每转发全量八份完整 schema = 5509 字符 ≈ 2704 token；1.0 的真实 `prompt_tokens` 中位数是 1437 |
| §3.2 | JIT 剪枝的效力 | **只剪提示词渲染**，准入白名单是 `profile.tools`——模型看不见但仍能调 | **剪掉即不能调**（原生 `tools` 字段既是广告也是约束） | 所以 2.0 的规则一律**比 1.0 保守**：只剪「现在根本用不上」的。`get_result_page` 只要已有结果集就一直在——`photo_set` 销账不等于候选看完了（一页 6 条、结果集最多 12 条） |
| §3.3 | max_tokens | `min(1501, 4400 - prompt)`，下限 64 | **`min(1501, context - prompt)`**，`context` 由 `Settings.vlm_context_tokens` 给（生产 6144）；剩余 < 256 时抛 `PromptTooLongError`，调用方压缩一整轮工具往返后再试一次 | 见上面 §3.3 那段 |
| §3.4A | 工具计价 | 只数**成功**的调用 | **数所有工具往返**（`tool_round_trips`） | 见上面 §3.4A 那段 |
| §3.4C | 工具额度用尽时 | 1.0 被拒即 `break` 结束回合 | **一个工具都不发**（`select_tools` 返回空集），模型只剩「说话」 | 实测 `step_budget_exhausted` 的 123 道里 **102 道判 0、2 分一道没有**——根子是模型到最后几步仍在撞墙，从没被逼着开口 |
| §3.5 | 收尾前的一次性提醒 | 三条（`structured_fact_reminder` / `gate_unattempted` / `fabrication_check`），各一个 `*_prompted` 标志 | **两条**：`required_evidence_missing`（仅 required + 未尝试 + 绑定不是 unsupported/partial）与 `fabrication_check`；`structured_fact_reminder` 已删除 | `structured_fact_reminder` 只按 evidence_type 判断「没查过」，对金额/时长/住院号这类工具根本没有的值类型也会逼模型去查 `query_memory_facts`；实测制造 9 道「362/95 张照片」式跑偏。`unsupported` / `partial` 现在允许如实报缺口 |
| §3.9 | requirement 的结构化字段 | 1.0 只有 `id/evidence_type/description/required` | **增加** `predicate/value_type/scope`；planner 用 JSON Schema 约束解码，`enable_thinking=false`，非法字段一次纠正重试 | 只有 evidence_type 时，「照片候选数」与「婚宴金额」在账本层没有区别，守卫只能用类型打勾 + 全局 aggregate |
| §3.10 | 证据绑定 | 无 | **`EvidenceBindingEvaluator`**：按 value_type + scope + coverage 给出 `satisfies/partial/unsupported/scope_mismatch/conflict/unknown`，进轨迹 | 让「工具没这个能力」与「取过没取到」分开：前者不提醒、不逼编造；后者才定向重试 |
| §3.11 | 语义评审 | 每回合一次 L2（整段回答 + 最近 8 条消息） | **L3 claim-level**：只对高风险且未绑定的数字/日期/金额/地点断言调用；`SENTRIX_CLAIM_VERIFIER=0` 可回退旧 L2 | 旧 L2 531 次调用 / 1.19M prompt tokens，224 次 no-change；claim-level 把输入收窄到一条 claim + 相关 facts |
| §3.12 | final_answer 结果集 | 只认 `current_result_set_id`；空 result_set 时 `unknown_result_set` | **按本轮出现过的结果集依次回退**；无结果集时空交付合法 | 实测 41 次 / 34 题 `unknown_result_set`，是 result set 生命周期而非模型错误 |
| §3.6 | 规划提示词的 `delivered_photo` | 无条件：「声明 photo_set……**同时声明** delivered_photo」 | **有条件**：只有用户明确要看图时才同时声明 | **有意的偏离。** 实测 487 道里 212 道声明了 `delivered_photo`，而问题文本里真正要求看图的只有 **3 道** |
| §3.7 | `delivery_contradiction` 规则 | 有（`final_guard.py:106-112`） | **已删除**，连同它的输入 `delivery_state` / `delivered_count` | 判据取不到（`deliver()` 成功恒 ≥ 1、失败记成 `error` 被 `from_state` 跳过），且前提变了：W6 之后交付字段必填，零交付是**明确写下的 `[]`** 而不是遗漏。让它活起来会在约 300 道题上各花一个模型步，打回的是模型的显式选择 |
| §3.8 | 候选集 | item 里有 `retrieved_candidate_media` | **有**（`TurnRecord.retrieved_candidates` → `agent_turns.candidates_json` → 终态载荷 → `item["retrieved_candidate_media"]` + `candidate_retrieval_*`） | 见「2.0 不再暴露的轨迹」那一节 |

`all_requested_but_has_more` **保留**：它读的是 `result_mode == "all"`，而 2.0 的工具契约里
没有 `mode` 参数，所以那条规则照旧够不着——但它**不是死代码**，它与 §3.7 那条的区别在于
它的输入是「将来会重新引入的东西」（1.0 的 `search_memories(mode=...)`），而 §3.7 那条的
输入是被 2.0 的**设计**排除掉的。

## 事件

> **2026-09-22 修正**：本节的「信号」一行原文写的是**四个**信号（时间 / GPS 2km /
> 视觉相似 / 人物）。实测 `backend/db.py:2065-2070` 的加权和是**八项**：

```
0.25·time + 0.25·location + 0.15·visual_place + 0.05·event_type
+ 0.20·activity + 0.05·objects + 0.20·person + 0.05·visual_boost
+ spatio_temporal_bonus（时空几乎重合时 +0.20）
```

按四个信号去实现会丢掉 `visual_place` / `event_type` / `activity` / `objects`
四路共 **0.45** 的权重，另加 `visual_boost`（0.05）与时空加成（0.20）——
**合计 0.70**。八项权重之和是 1.20，不是 1.0；`total` 最后靠
`max(0.0, min(1.0, total))` 收口。照「权重必须归一」去改会把饱和区的形状改掉。

| 项 | 值 | 位置 |
|---|---|---|
| 信号（八项） | 时间 0.25 / 位置 0.25 / 视觉地点 0.15 / 事件类型 0.05 / 活动 0.20 / 物件 0.05 / 人物 0.20 / 视觉加成 0.05 | `backend/db.py:2065-2070` |
| 时空加成 | 时间与位置都 ≥ 0.90 时 +0.20 | `backend/db.py:2078` |
| 时间衰减 | 线性，360 分钟到底 | `backend/db.py:2004` |
| 位置衰减 | GPS 线性，**2 公里**到底（`max(0, 1 - km/2)`） | `backend/db.py:2015` |
| 视觉加成 | 相似度 0.70 以下不给分，1.00 给满 | `backend/db.py:2071-2074` |
| 拆事件护栏 | 语义冲突 + 无物件/人物佐证 + 视觉 < 0.45 → `semantic_visual_conflict`，总分归零 | `backend/db.py:2055-2063` |
| 候选时间窗 | 6 小时（硬窗，与 360 分钟衰减不是一回事） | `backend/db.py:1967` |
| 候选阈值 | `total >= 0.50` | `backend/db.py:1969` |
| 活动匹配 | 相同或互为子串 → 0.9（措辞不同不惩罚） | `backend/db.py:2030-2033` |
| 物件/人物/事件类型匹配 | 0.8 / 0.8 / 0.8 | `backend/db.py:2043,2047,2027` |
| 归并时机 | 导入时同步 | `backend/db.py:2287` |
| 全局两两归并 | `consolidate_events`，GPS 线性 **1000 米**衰减 + 时间 ≥ 0.70 + 三条 merge 分支 | `backend/db.py:2192-2220` |
| 全局归并的调用点 | **只在 4 个 benchmark / maintenance 脚本里**；生产导入路径只调候选打分 | `scripts/benchmarks/ingest_album3_v2.py:64` 等 |

> **2.0 的取舍**：候选打分（2km 那套）移植；`_event_pair_score`（1000 米那套）
> **不移植**。两个函数对同一个距离给两个答案，而 1.0 的生产路径只用前者。
> 见 `docs/refactor/s4-deviations.md` §2 第 2 项。

## 地点

| 项 | 值 | 位置 |
|---|---|---|
| 国内精度 | 区县级（PyGeoCN，面） | `backend/geocoding.py:103-140` |
| PyGeoCN confidence | 0.90（1.0 记作 `source="tianditu"`；2.0 改名 `pygeocn`） | `backend/geocoding.py:134` |
| GeoNames confidence | 0.70 | `backend/geocoding.py:190` |
| `precision` | `"district" if district else "city"`（国内）；国外恒 `"city"` | `backend/geocoding.py:121,172` |
| 两个来源的关系 | **二选一**：PyGeoCN 有结果就返回，`reverse_geocoder` 根本不会被调用 | `backend/geocoding.py:196-216` |
| 距离 | GeoNames 那一路算了 `distance_km`，但**从不读取**（既不展示也不参与判定） | `backend/geocoding.py:186` |
| 地点匹配 | 三条确定性规则：整串子串 / 行政区去后缀双向子串 / 中英别名 | `backend/geocoding.py:269-308` |
| 行政区后缀 | `省 市 区 县 地区 自治州 盟 特别行政区`，按序取第一个命中，**只砍一次** | `backend/geocoding.py:222-230` |
| `_PLACE_ALIASES` | 32 条中英桥；其中 `"赵州桥"` / `"三峡坝址"` 两条是 benchmark 泄漏，见 `frozen.md` | `backend/geocoding.py:234-258` |

> **2.0 的扩展（NEW-2.0）**：国内精度从「区县」下探到「乡镇 + 村庄/居民点」，
> 数据源 GeoNames `ADM4`（11,450 条）+ `PPL`（860,831 条），最近邻点索引。
> 1.0 零参考。见 `docs/refactor/s4-deviations.md` §1 第 1 项。

## 外观证据 / 人物画像 / 画像流水线（S5c）

| 项 | 值 | 位置 |
|---|---|---|
| 头肩裁剪的三个松紧系数 | `HEAD_SCALE=1.35` / `BODY_HEIGHT_SCALE=4.5` / `FACE_TOP_EXPANSION=0.35` | `backend/person_appearance.py:4,21,23` |
| 裁剪尺寸下限 | 宽 `<24` 或高 `<48` → **抛错，不回退整图** | `backend/person_appearance.py:23` |
| 外观证据唯一键 | `(person_id, face_instance_id, model_name)`——**换模型是另一条证据** | `backend/db.py:4482` |
| 外观置信度兜底 | `0.55` | `backend/model_clients.py:1352` |
| 画像长度 | 汉字数 80–500 | `backend/person_portraits.py`（提示词原文即写死） |
| 画像主题数 | 2–6 | 同上 |
| 证据包抽样 | `MAX_MOMENTS=12` / 每事件 `PER_EVENT=2` / 每主题引用 `MAX_REFS_PER_THEME=2` / `TOP_ACTIONS=3` | 同上 |
| 未确认角色的措辞 | 必须含 `似乎/可能/从照片看/看起来/像是` 之一 | 同上（`_mentions_role`） |
| 核心人物排序六项权重 | 日期 `0.30` / 事件 `0.20` / 覆盖 `0.15` / 同伴 `0.10` / 场景 `0.10` / 质量 `0.15`（**和恰好 1.0**） | `backend/person_insights.py:31-57` |
| 排序的满值档 | `DATE_FULL=8.0` / `EVENT_FULL=6.0` / `COVERAGE_FULL=20.0` / `CO_PERSON_FULL=4.0` / `SCENE_FULL=4.0` | 同上 |
| 确认加成 / 单日惩罚 | `CONFIRMED_BOOST=0.20` / `SINGLE_DAY_PENALTY=0.35` | 同上 |
| 核心人物门槛 | 上限 `10`，且跨够 `2` 天（`CORE_MIN_DATE_COUNT`） | 同上 |
| 代表照 | 上限 `12` / 每事件 `2` / 去重阈值 `0.94` | 同上 |
| 提示词版本 | `person-moment-v1`（`person_moments.py:186`）/ `person-portrait-v1`（`person_insights.py:251`） | 两者都是 upsert 键或画像行的组成部分 |

> **2026-09-29 补记（S5c / Task 87 时才发现缺）**。这一节是 T87 验收时才补进来的——
> T85a（裁剪几何）、T86c（画像三层）、T86d（排序与代表照）落地时都**没有回来更新这份
> 基线**。值本身没错（都是从 2.0 已落地的模块里取的，那些模块的 docstring 逐个标了
> 1.0 行号），但**漏记这件事本身是个缺陷**：基线的用途是「下一次有人要动这些数时，
> 能查到它们属于 1.0 的行为、动它们就是偏离」，而查不到就会按自己的判断改掉。
>
> 三条与「值」同等重要的行为，也一并记在这里：
>
> 1. **`select_representatives` 的余弦去重分支在生产上从不执行。** 1.0 唯一的调用点
>    传的是字面量 `{}`（`person_insights.py:271`），所以「与已选中的任一张余弦 ≥ 0.94
>    就跳过」那条永远走不到。**2.0 照搬了这个空表**（`run_insights._select`），
>    把发现记在 `s5-deviations.md` 第 137 条。**动这一条等于改产品行为**，不是修 bug。
> 2. **`person_portrait_revisions` 的 `user_locked` 是写入口的判据**，不是界面上的
>    禁用按钮：当前版本被锁住时 `create` 直接返回、不写新行（`db.py:6380`）。
> 3. **外观证据的三重守卫**（人已确认 / 实例存在 / 该实例经 `entity_mentions` 确认给了
>    这个人）。1.0 的调用点把守卫的错误连同媒体错误一起 `except` 掉了，所以**第三重
>    在 1.0 里从来没浮出来过**；2.0 只捕获媒体失败，守卫的错误往外抛
>    （`s5-deviations.md` 第 131 条）。

## 模型与嵌入

| 项 | 值 | 位置 |
|---|---|---|
| 视觉嵌入 | tencent/WeMM-Embedding-2B，2048 维，HTTP sidecar `127.0.0.1:8102`（视觉 / 文本**统一空间**） | `backend/embeddings/wemm_sidecar.py` |
| 文本嵌入 | 同上——同一个模型、同一个实例（统一空间，两个槽共用一份 client）；1.0 的 wemm 是**半接线**（只有查询侧跟随方案），2.0 由 W1 把写入侧一并接上 | `backend/embeddings/wemm_sidecar.py` |
| 方案冻结 | `embeddings/scheme.py` 已钉死，不读 env | `backend/embeddings/scheme.py` |
| VLM 端点 | `http://127.0.0.1:8100/v1`（llama.cpp，OpenAI 兼容） | `backend/model_clients.py:537` |

## 照片复核 / 读字（S6）

`inspect_photo` 与 `read_photo_text` 的提示词与取值口径。1.0 的调用量分别是
10,721 次与 2,867 次，是助手的主要取证手段（设计文档 §1.3）。

| 项 | 值 | 位置 |
|---|---|---|
| `inspect_photo` 提示词 | 两个槽 `{target_instruction}` / `{question}`，固定要 `observation` / `certainty` / `target_face_id` 三个字段 | `backend/agent_runtime/tools.py:2616-2622` |
| `certainty` 的取值 | **只有 `supported` / `uncertain` 两档**；模型没给时默认 `supported`。**没有「确定没有」那一档** | `:2619`、`:2255` |
| 目标人物那句 | 只在**指名了目标人物**时才填；与裁剪图做没做出来无关（裁剪失败照样填，因为那句话说「（如有）」） | `:2196-2198`、`:2186-2200` |
| 人脸清单 | 拼在提示词**之后**（不是槽）；未确认的人**不填 `person_name`**，模型据此不许猜姓名 | `:2204-2208`、`:2343-2355` |
| 发几张图 | 指名目标人物且定位到裁剪件时发**两张**：裁剪件在前、原图在后 | `:2192` |
| 问题缺席时的默认问法 | `请描述这张照片` | `:2125` |
| **`read_photo_text` 的第一条路** | **任何 OCR 后端之前**先读导入期已落库的 `observations.ocr_text`，命中即返回 `source: ingestion_ocr` / `certainty: supported` / `persisted: true`（零推理开销） | `backend/agent_runtime/ocr_tool.py:544-559` |
| `read_photo_text` 的后端顺序 | PaddleOCR 小模型优先（CPU、零显存、进程内），读不到回退 VLM；`paddleocr` 装不上时自己退化成 VLM-only | `ocr_tool.py:3`、`:134-142` |
| 密集文字的两条处理 | 3×3 切块放大（`rows=3, cols=3, scale=2.0`）、按 y 聚行（`y_tol=26`） | `ocr_tool.py` 的 `_tile_images` / `_small_row_cluster` |

> **`paddleocr` 从来没进过 2.0。** 它不是「移植时丢了」，是那个可选后端一开始就没加：
> 2.0 的唯一 OCR 来源是 `OpenAIVlmObserver` 写入的 `ocr_text`。**所以 2.0 现在所处的状态，
> 正是 1.0 在 `paddleocr` 装不上的环境里自己退化到的那个状态。**
>
> **2026-09-29 实测（153 的 8100，qwen3.5-4B）**：这个模型**答得比问的多**——问
> 「这张图里画的是什么形状？只回答形状名称。」，它回「这张照片里画的是一个红色圆和一个
> 蓝色正方形」。这正是 1.0 在 `read_photo_text` 的工具描述里专门警告模型不要拿它去统计
> 「OCR 金额、桌数」的那件事的两面：**这个工具的用途是「读出真的写着什么」，而 VLM
> 会读出图上没有的字、且不会自报**。T124 的工具描述必须按裁决如实收窄。

## 照片人物取证（S6 · T122）

`query_photo_people`——1.0 调用 887 次。它只回答「**这张照片里有谁**」，
不做跨照片的人名搬运。

| 项 | 值 | 位置 |
|---|---|---|
| 工具描述原文 | 「读取一张已检索照片自己的已确认人物和未确认同行者。asset_handle 必须来自当前 search_memories preview；不会把其他候选照片的人名移植过来。」 | `backend/agent_runtime/tools.py:2723-2724` |
| 声明的证据类型 | `photo_identity` + `confirmed_identity`（**两个都是**公共类型） | `tools.py:2727`、`evidence_contract.py:13-14` |
| 已绑定人脸的判据 | `face_instances ⋈ face_clusters ⋈ entities`，唯一那条过滤是 `e.entity_type = 'person'`。**没有任何状态过滤** | `tools.py:2281-2300` |
| 该查询的排序 | `quality DESC, detection_confidence DESC`（只有两个键，同分时取到哪行由 SQLite 定） | 同上 |
| 未确认同行者 | `people_json` 里**没有 `entity_id`** 的条目数 **减去**已绑定的脸数，下界 0 | `tools.py:2582-2588` |
| 人名快照取哪一条 | `observations` 里 `ORDER BY updated_at DESC LIMIT 1` | `tools.py:2578-2580` |
| 空结果那句话 | `"没有已确认姓名"`（不是空串——空串会让模型以为字段坏了） | `tools.py:2602` |
| 两个失败码 | `bad_handle`（handle 不在结果集）与 `scope_mismatch`（结果集不属于当前相册）分开 | `tools.py:2566-2576` |

> **三处「名字/直觉与代码不一致」，都是 1.0 的，照搬不修。**
>
> 1. **`_confirmed_photo_identities` 判的是「绑没绑」，不是「确认没确认」。** 函数名与
>    docstring 都写 *confirmed*，而 SQL 两个 JOIN 都是 INNER、一个状态过滤都没有。
>    2.0 里簇可以带 pending 实体（`enroll_face` 就是「建簇带 pending 实体」）。
>    **顺手补一个 `status = 'confirmed'` 是在改产品行为。**
> 2. **`people` 是照着人脸实例逐行生成的，同一个人有两张脸就出现两次。**
>    摘要会念成「爸爸、爸爸」（`tools.py:2589-2593`）。**去重会同时改掉 `people` 的长度**，
>    而那个长度是证据。
> 3. **`membership` 在 1.0 来自家庭图谱**（`get_effective_family_membership`，
>    `tools.py:2295`），**不是实体上的字段**。2.0 只有 `entities.family_role`，
>    所以工具描述里如实写明「尚无关系图谱」——不要据此推断两个人之间的亲属关系。
>
> **另外：那条 SQL 有三重互相覆盖的过滤**（簇 INNER、实体 INNER、`entity_type='person'`），
> 单独把任一 INNER 换成 LEFT JOIN 都观察不到行为差别——`NULL = 'person'` 不为真，
> WHERE 那一句自己就把未绑定的行挡掉了。2026-09-30 用定点变异实测确认过。


> **2026-09-30 补：`read_photo_text` 的 VLM 回退是死代码。** 上面那张表里
> 「后端顺序」与「密集文字的两条处理」两行描述的是 **PaddleOCR 那一路**，而模块注释
> 承诺的「读不到回退 VLM」**在 1.0 里没有实现**：`_OCR_PROMPT` / `_OCR_PROMPT_FULL`
> 全仓从未被引用，`ocr_tool.py` 整条链上一次模型调用都没有。
> 1.0 生产 venv 里装着 `paddleocr`，所以 2,867 次调用跑的都是小模型。
> **2.0 的 `read_photo_text` 因此只有一条路：读导入期已落库的 `observations.ocr_text`**
> （那是 1.0 的第一条路，见上表最后一行）。裁决与理由记在 S6 计划 §8.8。

## 实体目录（S6 · T128a，2026-09-30）

`GET /api/entities`（1.0 `app.py:1858-1868`）是**全部实体**的目录；`/api/people` 是
**家庭成员**，两个端点。下面这些数值、字面量与边界照搬，不改。

| 项 | 值 | 位置 |
|---|---|---|
| `includePeople` 默认 | `false`（不给人物） | `backend/app.py:1859` |
| pending 人物在**这个**端点上的名字 | `待命名成员`（**不带序号**） | `backend/app.py:1865` |
| pending 人物在 `/api/people` 上的名字 | `待命名成员 1`、`待命名成员 2`（按列表顺序递增） | `backend/app.py:1517-1519` |
| pending 人物的接口说明 | `由人脸聚类生成，等待用户确认` | `backend/app.py:1867` |
| 同一件事在**库里**的原文 | `由 buffalo_l embedding 生成，等待用户确认` | `backend/db.py:4639` |
| `status` 为空时排除的取值 | `rejected` / `superseded` | `backend/db.py:4047` |
| 空间规则 | 恒有 `scope_id NOT IN (SELECT id FROM memory_spaces WHERE include_in_people = 0)` | `backend/db.py:4043-4045` |
| 排序与上限 | `updated_at DESC`，**无 LIMIT** | `backend/db.py:4052` |
| `cluster_count` | `face_clusters` 计数，**没有状态过滤**（被拒的簇也算） | `backend/db.py:4060-4062` |
| `photo_count` | `COUNT(DISTINCT a.id)` 且 `fc.status != 'rejected'` | `backend/db.py:4067-4072` |
| `mention_count` / `evidence_count` | `entity_mentions` / `entity_observations` 的行数——**两张不同的表** | `backend/db.py:4078-4086` |
| `relationship_count` | subject 侧 + object 侧两轮相加，`status != 'retracted'`；**2.0 留 S7，不出现在响应里** | `backend/db.py:4088-4100` |
| `avatar_face_instance_id` | 非 rejected 簇里按 `detection_confidence DESC, created_at ASC` 取第一 | `backend/db.py:4102-4110` |
| `preview_asset_id` / `preview_file_name` / `preview_media_type` | `entity_observations ⋈ observations ⋈ assets`，按 `captured_at DESC, o.id DESC` 取第一 | `backend/db.py:4111-4120` |
| `reviewable`（person） | `status == 'confirmed'` 或 存在 `status != 'rejected'` 且 `member_count > 0` 的簇 | `backend/db.py:4128-4131` |
| `single_sample`（person） | `status != 'confirmed'` 且 所有簇 `member_count <= 1` | `backend/db.py:4132-4133` |
| `reviewable`（非 person） | `evidence_count > 0`——与簇无关，**也不看 status** | `backend/db.py:4135` |
| 私密地点 | 仅 `place`：`private_flag` **`is True`** 时名字换成 `alias`（缺省 `私密地点`）并给 `private: true` | `backend/db.py:4674-4685` |
| 私密地点的两个属性键 | `private_flag` 与 **`alias`（单数）**——人物别名用的是 `aliases`（复数），差一个字母 | `backend/db.py:4679-4681` |

> **三处反直觉的边界，都不要「修好」。**
>
> 1. **`private_flag` 判的是 `is True`，不是真值。** 属性值是从 JSON 解出来的任意类型，
>    整数 `1` 与字符串 `"true"` 都能通过真值判断，而 1.0 只认真正的布尔。
> 2. **`single_sample` 不看簇的 `status`。** 一个**被拒**的、有两张脸的簇照样让它不是
>    孤证——而 `reviewable` 是看 status 的。两处规则不同，都是 1.0 的原样。
> 3. **空簇列表下 `all()` 为真**，所以一个还没聚出任何簇的 pending 人物判为
>    `single_sample`。
>
> **一条死检查，别当字段搬过来。** 1.0 前端 `src/app.js:695` 有
> `if (ent?.is_self) return`——`entities` 表**没有 `is_self` 列**（`SELECT *` 的列里没有，
> `_decorate_entities` 也不补），这个 `if` 永远不成立。
>
> **一条在生产里是死路径的默认值。** `includePeople` 默认 `false`，但 1.0 前端
> **永远传 `true`**（`src/api.js:42` 把它写死在请求里）。默认值照搬——它是 API 契约——
> 但没有任何 1.0 的界面场景走那条默认路径，**不要为它编造消费场景**。
>
> **装饰字段在前端是承重的**（这是 128a 不做减法、全量搬的理由）：
> `reviewable` 是 `src/app.js:675` 实体网格的入口过滤谓词；`avatar_face_instance_id` 与
> `mention_count` / `cluster_count` / `relationship_count` 在 `:678` 的实体卡上；
> `evidence_count` 在 `:876` / `:910` / `:929`；`photo_count` / `single_sample` 驱动
> `:939` / `:2112` 的批量确认。
> 例外：`preview_*` 在**实体列表上**产出了但前端当前没有读者——它被读的地方是
> `entityGroups`（`:785-786`，T129 的投影）。如实记下，不夸大成承重。
>
> **一处已存在的 2.0 与 1.0 的差异（不在 T128a 范围内，未改）：** 1.0 的 `/api/people`
> 给 pending 人物的是**带序号**的 `待命名成员 {n}`（`backend/app.py:1517-1519`，按列表
> 位置递增），2.0 的 `_person_payload` 用的是不带序号的 `PENDING_CLUSTER_NAME`。
> 本次只把两边的 **summary** 对齐了（那是同一个改写块里漏掉的一条），序号这件事需要
> 单独裁决：序号是**列表位置**的函数，放进 `_person_payload` 意味着那个函数要知道
> 自己在列表里的下标。

## 实体详情（S6 · T128b，2026-09-30）

`GET /api/entities/{id}`（1.0 `app.py:1948-1956` + `db.py:4570-4627`）。**是十二个键，
不是计划里写的「九个子查询」**；2.0 搬九个，三个不进响应（各有理由，见
`application/identity/read_entity_detail.py` 的模块 docstring）。

| 项 | 值 | 位置 |
|---|---|---|
| 实体不存在 | 返回 `None` → 404 `entity not found` | `backend/db.py:4571-4573`、`app.py:1951-1952` |
| pending 人物的改写 | **只换 `canonical_name` 成 `待命名成员`**——`family_role` 与 `summary` **不改** | `backend/app.py:1954-1955` |
| 对照：`/api/people/{id}/profile` | 同一件事**三件都改**（名字 + `family_role=None` + `summary`） | `backend/app.py:1545-1549` |
| 对照：`/api/people/{id}/evidence` | 同上，三件都改 | `backend/app.py:1561-1565` |
| `facts` 的匹配方式 | 按 **`canonical_name` 字符串**匹配 `subject` 或 `object`，**不按 id** | `backend/db.py:4588` |
| `facts` 的取数顺序 | **先取 1000 条（全库、无 scope 过滤）再过滤**——两步的顺序是行为的一部分 | `backend/db.py:4588` |
| `facts` / `relationships` 的状态过滤 | **都没有**（`retracted` 的关系也会返回）；而 `properties` **有** | `backend/db.py:4588` / `:5590` 对 `:4667` |
| `clusters` 的状态过滤 | **没有**——被拒的簇也在详情里 | `backend/db.py:4580` |
| `avatar_face_instance_id` 的状态过滤 | **有**——`fc.status != 'rejected'` | `backend/db.py:4576` |
| 每簇样本数 | `LIMIT 12` | `backend/db.py:4583` |
| `observations` 的取数 | `captured_at DESC LIMIT 100`，**没有第二排序键** | `backend/db.py:4590-4594` |
| `evidence_count`（详情） | **取回来的观察条数**（上限 100） | `backend/db.py:4601` |
| `evidence_count`（列表） | **`entity_observations` 的原始行数** | `backend/db.py:4083` |
| `evidence_files` 的合并 | 观察先、外观后，**按 id 去重且后写覆盖先写**（所以撞车时外观那条赢） | `backend/db.py:4602-4613` |
| `evidence_files` 的 kind | `observation` / `appearance` | `backend/db.py:4607` / `:4612` |
| `file_name` 的兜底 | 取不到就用 `asset_id` | `backend/db.py:4606` / `:4611` |

> **两处「同名不同义」不要统一。** ① `evidence_count` 在**列表**里是
> `entity_observations` 的原始行数、在**详情**里是取回来的观察条数（上限 100）——
> 1.0 就是两个数（`db.py:4083` 对 `:4601`）。② 同一个响应里 `properties` 排
> `superseded`、`clusters` 不排 `rejected`，两种相反的规矩并存。
>
> **`clusters` 的样本排序在 2.0 与 1.0 不同**（已存在的差异，不在 T128b 范围内）：
> 1.0 是 `ORDER BY fi.created_at DESC`（`db.py:4584`），2.0 的
> `FaceRepository.list_instances` 是 `quality DESC, created_at ASC`。
> 两处都会给出「最多 12 张」，但**取到哪 12 张可能不同**。
>
> **`transcript` 不在响应里**：1.0 的 `observations` 表有这一列，2.0 没有——
> 视频/音频的转写属于 **S8**（`application/retrieval/index_observation_text.py` 的
> docstring 已写明「不是遗漏，是那一侧还没做」）。1.0 前端拿它当 caption 的兜底，
> 所以少一个键的后果是那句话落到常量，不会渲染出错。

## 家庭图谱（S7b1 · T301–T306，2026-10-02）

六张表与五条写路由里的常量与判据，逐条指着 1.0。**这些值只存在于 1.0 的代码里、无文档**，
而它们中的每一个都决定了「用户改过的会不会被模型覆盖」。

| 项 | 值 | 出处 |
|---|---|---|
| membership 合法值 | `family` / `friend` / `unknown`——**`unknown` 是合法值不是缺失** | `backend/db.py:5617` |
| 来源合法值 | `model` / `user_override`，**只有两个** | `backend/db.py:5618` |
| `state` 取值 | `effective` / `superseded` / `retracted`（**`retracted` 只出现在关系侧**） | 六张表 DDL（`db.py:702-781`） |
| `locked` | `1 if source == "user_override" else 0`；**block 表恒 1，且那是它的默认值** | `db.py:5658` / `:5766` |
| 用户写死的两个值 | `source="user_override"`、`confidence=1.0`（**五个写路由全部如此**） | `app.py:1668` / `:1684` / `:1700` |
| 关系标签白名单 | **31 条**；一对多是常态（`父亲` 的反向是 `{儿子, 女儿}`，由孩子的性别决定用哪个） | `family_graph.py:1-34` |
| 校验话术 | `关系标签不能为空` / `关系标签不匹配: X -> Y`——两句不同的话 | `family_graph.py:38-41` |
| 一条边 | **落两行**（正 + 反），同 timestamp、同 source、同证据 | `db.py:5793-5805` |
| 关系新行的 `family_graph_id` / `supersedes_id` | **恒 NULL** | `db.py:5797` |
| membership 取当前 | `ORDER BY locked DESC, revision DESC, updated_at DESC LIMIT 1` | `db.py:5636` |
| portrait 取当前 | `ORDER BY created_at DESC LIMIT 1` | `db.py:5913` |
| 头像排序 | `fi.quality DESC, fi.detection_confidence DESC, fi.created_at ASC LIMIT 1` | `db.py:5659` |
| 代表照 | 走**人脸**（`face_instances⋈face_clusters⋈assets`），`ORDER BY a.id DESC LIMIT 3` | `db.py:5662-5669` |
| `appearance_count` | `COUNT(*)` 人脸实例行数——**不是不同资产数** | `db.py:5671-5675` |
| pending 人物的占位 | **「待命名人物」** | `db.py:5677` |

| 触发去抖 | **event watermark**：`COUNT(*) FROM events WHERE scope_id = ?` 与上一次 run 的 `stats['event_watermark']` 比，**没涨就不跑**；`latest is None` 时**不跳过** | `backend/pipeline.py:746-749` |
| `trigger_type` | 手工路由写 `user_rerun`、批末触发写 `ingest`；`input_mode` 恒 `semantic_text_only` | `app.py:1719` / `pipeline.py:750` |
| run 初始态 | `status='queued'`、`current_stage='queued'`、`stats={}` | `db.py:5869` |
| run 完成 | `status='completed'` 时额外写 `completed_at` | `db.py:5895` |
| run 失败 | 只写 `error`，**`stats` 保持空** → watermark 缺失 → 下次一定重跑 | `family_graph_service.py:43` |
| 模型截断（送进去的） | 人 `[:8]`（按 observation 数降序、同数按 id 升序）、每人 observation_ids `[:6]`、descriptions `[:2]` 每条 `[:300]`、共现 `[:30]` 且**只留两端都在 top-8 内的对** | `family_graph_service.py:56/63/64/66-67` |
| 证据里的描述上限 | 每人 `[:20]`（**模型只看前 2 条，两道闸看这 20 条**） | `family_graph_service.py:214` |
| `_has_explicit_relation_evidence` 的词表 | **只覆盖 13 个谓词**；表外的退化成「字面匹配谓词自己」 | `family_graph_service.py:132-145` |

> **三处「1.0 就是这样」不要统一。** ① `get_active_family_portrait` 用 `created_at DESC`，
> 而 `get_effective_family_membership` 用三个键——同一批「取当前有效行」的语义，两个排序。
> ② `set_family_relationship` 的新行 `supersedes_id` **恒 NULL**（历史链断在关系侧），
> 而 `set_family_membership` **会回填** `supersedes_id`。③ `write_family_portrait`
> **不校验 `source`**，另两个写方法都校验 `FAMILY_SOURCES`。
>
> **三条「静默跳过」，都不抛异常——调用方分不出「刚写成功」与「被跳过」：**
> 用户写下的行模型不许覆盖（membership / portrait 直接返回当前行）；撤回过的边在
> `family_relationship_blocks` 里挡着模型；用户重新建边时**删掉**那一行 block。
>
> **`GET /family-graph` 的 `run` 键 b1 不返回。** 1.0 的响应里有它，但 b1 阶段**没有任何
> 东西会创建 run**（自动触发与手动路由都归 S7b2）——恒 `null` 读起来像「确实没跑过」，
> 而真相是「b1 还没有会跑的路径」。S7b2 连创建路径一起补。


### 助手引用家庭图谱（S7c · 2026-10-02）

助手工具 `get_person_profile` 从「只回 `family_role`」扩到家庭图谱那三张表的实际内容。
**下面这些值同样只存在于 1.0 的代码里、无文档。**

| 项 | 值 | 出处 |
|---|---|---|
| 工具新增的四个数据键 | `membership` / `membership_source` / `summary` / `relationships` | 1.0 `db.py:2875-2881` |
| `membership` 缺省 | `"unknown"`——**没有归属行与「归属不明」是同一件事** | 1.0 `db.py:2875` 的 `or "unknown"` |
| `membership_source` 缺省 | `""` | 1.0 `db.py:2876` |
| `summary` 来自 | `family_person_portraits.portrait_text`（1.0 那格叫 `summary_zh`） | 1.0 `db.py:2877` |
| 关系的三个键 | `predicate` / `other_name` / `source`，一个不多一个不少 | 1.0 `db.py:2855-2859` |
| `other_name` 兜底 | `未命名成员` | 1.0 `db.py:2857` |
| 关系方向 | **只收此人为 `subject` 的边**——`predicate` 才是「此人是对方的什么」 | 1.0 `db.py:2852-2858` |
| **`family_role` 不再出现在工具结果里** | 1.0 的 digest **全文没有这个键**；它是 2.0 在 S3 的占位。`entities.family_role` 列保留，`GET /people/{id}` 照旧返回 | 1.0 `db.py:2840-2889` |

**1.0 的方向过滤是残的，2.0 因为双向存储反而不残。** 1.0 一段关系只按一个方向存，
配上「只收出边」之后，查「女儿」的档案**拿不到父亲**——而 1.0 的测试恰好只测了
父为 subject 的方向，把这个缺口盖住了。2.0 的 `set_relationship` 一次写正向 + 反向
两行，所以同一句过滤拿到的是**完整**的关系集。契约测试
`test_a_relationship_appears_on_both_ends_with_its_own_predicate` 钉的就是这件事：
同一条边，妈妈看到 `母亲`、小明看到 `儿子`，各恰好一条。

**「没有关系」不是「查不到这个人」。** `relationships` 为空数组时 `found` 仍为真。
实测那份 53 张照片的相册就是 8 个人 0 条关系——把空关系当成查不到，助手会对着一个
明明存在的人说找不到。

**这一格与 `entities.family_role` 是两条不同的轴。** 1.0 真实库里 298 对
「`family_role` × `membership`」的配对**一致率 0**：一条是自由文本角色，一条是三值分类。

## 视频与音频（S8a · T501–T506，2026-10-04）

| 项 | 值 | 出处 |
|---|---|---|
| 浏览器可直接播的编码 | **只有** `h264` / `avc1`（判 `codec_name`，不看后缀） | 1.0 `video/processor.py:59` |
| 预览转码参数 | `libx264` / `-preset veryfast` / `-crf 23` / `-pix_fmt yuv420p` / `-c:a aac` / `-movflags +faststart` | 1.0 `video/processor.py:62-67` |
| 转码映射 | `-map 0:v:0` 与 **可选** `-map 0:a:0?`（无音轨不该让整次转码失败） | 同上 |
| 转码超时 | 3600 秒（读元数据的 ffprobe 超时是 60 秒，两件事不共用一个上限） | 1.0 `video/processor.py:68` / `metadata.py:43` |
| 拍摄时间优先级 | `com.apple.quicktime.creationdate` → `format.creation_time` → `stream.creation_time`（`creation_source` 记下用的是哪个） | 1.0 `video/metadata.py:60-63` |
| GPS 来源 | `com.apple.quicktime.location.ISO6709`，退 `location`；**ISO6709 格式**（`+22.4846+114.5438+005.469/`） | 1.0 `video/metadata.py:9,31-36` |
| 时区写法 | 紧凑写法 `+0800` 补成 `+08:00`；`Z` 换成 `+00:00` | 1.0 `video/metadata.py:10,44-45` |
| 旋转角 | 先 `side_data_list[].rotation`，退流标签 `rotate`；都没有是 0 | 1.0 `video/metadata.py:66-74` |
| 探测结果的落库键 | `assets.metadata_json` 的 `video_metadata` / `browser_preview_path` / `browser_preview_error`，2.0 另加 `video_probe_error` | 1.0 `video/processor.py:118-121` |
| `/assets/{id}/file` | 视频且非 `?original=1` 且转码件在 → `preview.mp4`；否则原片。**图片那条路一直交原文件**（预览在 `/preview`） | 1.0 `app.py:2448-2478`（2.0 的 `/file` 行为见 `routes/media.py` 注释） |
| 场景事件的类型标记 | `events.source_type = 'video_scene'`；场景列表只取 `status = 'active'`、按 `source_scene_index` 升序 | 1.0 `db.py:2427-2433` |
| 派生物的清场顺序 | 先删 `event_observations` / `observations`，再删 `events` / `assets`（外键开着） | 1.0 `db.py:2435-2448`（2.0 的顺序约束写在仓储 docstring） |
| `reprocess` 的返回 | `202` + `{accepted: true, asset_id, status: "video-queued"}`；资产行状态回到 `queued` | 1.0 `app.py:1472-1481` |
| 整册导入认的视频后缀 | `.mp4` / `.mov` / `.m4v` / `.avi` / `.mkv`（与 `MediaType.from_file_name` 同一组） | 1.0 `app.py` 批量目录扫描（2.0 原先只有 `IMAGE_SUFFIXES`） |

**音频/视频的转写不在 S8a**，也不在 S8 的任何一段：用户 2026-10-04 裁决「不要 FunASR」，
`observations` 不加 `transcript` 列，`compose_text` 保持七字段。理由与代价逐条登记在
`docs/refactor/s8-deviations.md` §1.1。音频的入库/元数据/播放属 S8c（T521–T523）。

**抽帧参数（间隔、质量门）不在本表**：那是 S8b 的 T511，落地时才登记。

## 视频抽帧与场景（S8b · T511–T514，2026-10-04）

**前四条是 2.0 新定的值，1.0 没有对应物**——1.0 的抽帧是 YOLO/Pose 预筛 + Katna 质量门
+ WorldMM 聚类，没有「固定时长窗」这个概念（用户 2026-10-04 裁决①「先做简化闭环」，
见 `s8-deviations.md` §3 第 1 条）。登记在这里是为了让它们可被质疑，而不是当成 1.0 的口径。

| 项 | 值 | 出处 |
|---|---|---|
| **场景 = 固定时长的时间窗** | `SCENE_WINDOW_SEC = 60.0` 秒 | **不是 1.0 的值**（S8b 新定） |
| **每个场景抽几帧** | 一帧，取**窗口中点**那一秒 | **不是 1.0 的值**（S8b 新定） |
| **总场景数封顶** | `MAX_SCENES = 40`；超过时**按比例放大窗口**，不是无限抽帧 | **不是 1.0 的值**（S8b 新定） |
| **`derived_kind` 取值** | 只有一个：`video_keyframe` | 用户裁决⑧（不搬双算法并存）；1.0 有两个（`video_keyframe` / `video_keyframe_webp`） |
| 抽帧的 ffmpeg 参数 | `-ss <输入侧> -frames:v 1 -q:v 2`，产物 `scenes/{n:03d}/kf_0001.jpg` | **不是 1.0 的值**（1.0 出的是 webp/jpg 混合） |
| 场景事件的标题 / 摘要 | `视频场景 N` / `视频场景 · {start:.1f}s~{end:.1f}s` | 1.0 `video/processor.py:203` |
| 场景事件的时间区间 | `source_start_sec` / `source_end_sec` 是**原片秒数**；`time_start` / `time_end` 是拍摄时间 + 偏移 | 1.0 `processor.py:183-184` |
| 关键帧的拍摄时间 | 源片 `captured_at` + `source_timestamp_sec` | 1.0 `processor.py:186` |
| 关键帧的事件归属 | **强制**挂到它那一秒的场景事件（不走候选打分） | 1.0 `pipeline.py:333` |
| 幂等键 | `(parent_asset_id, source_frame_index)`；**不是 timestamp** | 迁移 0010 表头；1.0 那条按 timestamp 的索引在默认路径上从未生效 |
| 场景摘要是否豁免刷新 | **不豁免**（1.0 `db.py:5576` 豁免 `video_scene`，是缺陷） | `s8-deviations.md` §2 第 2 条 |
| 关键帧是否付 VLM 观察 | **付**（走完整图片流水线，含观察） | 用户裁决④「派生帧走图片流水线」 |
| `POST /videos/{id}/reprocess` 的 `status` | `processed`（同步跑完后的真实状态） | **S8b 变更**：S8a 曾按 1.0 回 `video-queued`，那时确实只是排队；S8b 起抽取链同步跑，回那个词是假话 |

**封顶的代价与理由**：每个关键帧要走完整图片流水线（VLM 观察 + wemm 嵌入 + 人脸），
**帧数就是 VLM 调用数**；10 分钟视频 ≈ 10 次，不封顶的话长视频会把 GPU 打满。

**音频/视频的转写不在 S8b**（用户裁决②「不要 FunASR」）。音频属 S8c。

## 音频（S8c · T521–T523，2026-10-04）

**这一段的值大多是 2.0 新定的。** 1.0 对音频只有一个动作——FunASR 转写
（`pipeline.py:880` 的 `_audio_observation`），元数据与交付类型都不管：它的
`probe_video_metadata` 遇到没有视频流的文件直接抛 `ValueError`
（`video/metadata.py:52`），音频永远走不到元数据那条路。用户 2026-10-04 裁决
「音频不转写，只做入库与播放」，于是下面每一项都是**新的交付面**，而转写那一项是
**明确不做**。

| 项 | 值 | 出处 |
|---|---|---|
| **转写** | **不做**。`observations` 不加 `transcript` 列，`compose_text` 保持七字段 | 用户裁决②；`s8-deviations.md` §1.1 |
| 音频元数据字段 | 时长 / 编码 / 采样率 / 声道 / 码率（另带拍摄时间、GPS、`creation_source`） | **不是 1.0 的值**（1.0 的 ffprobe 遇到音频直接抛错，音频元数据从没落过库） |
| 时长与码率的取法 | 优先容器（`format`），退回流（`streams`） | **不是 1.0 的值**（与视频适配器同一个取法） |
| 拍摄时间优先级 | `com.apple.quicktime.creationdate` → `format.creation_time` → `stream.creation_time` | 与视频共用 `ffprobe.py`；1.0 `video/metadata.py:60-63` |
| GPS 来源 | `com.apple.quicktime.location.ISO6709`，退 `location`（ISO6709 格式） | 同上；1.0 `video/metadata.py:9,31-36` |
| ffprobe 超时 | 60 秒（与视频同一个常量 `DEFAULT_PROBE_TIMEOUT_S`） | 1.0 `video/metadata.py:43` |
| 探测结果的落库键 | `assets.metadata_json` 的 `audio_metadata` / `audio_probe_error` | **不是 1.0 的值**（键名照视频 `video_metadata` / `video_probe_error` 的体例起） |
| 探测失败 | **不阻断导入**，原因写进 `audio_probe_error`（`audio_metadata` 键则不写） | 与 EXIF / 视频同一条既有口径 |
| 交付的 content-type | 上传方给了具体的 `audio/*` 就用它；否则按后缀定：mp3→`audio/mpeg`、wav→`audio/wav`、m4a→`audio/mp4`、aac→`audio/aac`、flac→`audio/flac`、ogg→`audio/ogg` | **2.0 变更**：1.0 直接交 `mime_type or application/octet-stream`（`app.py:2463`），上传方不给类型时 `<audio>` 拿不到可播的类型 |
| 音频转码 | **不做**。浏览器原生放得了这六种容器 | 用户裁决②（与视频 H.264 直出同一条思路） |
| `/assets/{id}/file` | 音频交原文件（与 `?original=1` 同一条路）；`content-type` 同上 | 1.0 的 `asset_file` 没有音频分支（`app.py:2458` 只判 video） |
| `/assets/{id}/preview` | 音频交原文件（**不是 500**）——音频没有服务端预览件 | **S8c 修**：原先无条件走 `PillowPreviewEncoder`，而 PIL 打不开录音 |
| 整册导入认的音频后缀 | `.wav` / `.mp3` / `.m4a` / `.aac` / `.flac` / `.ogg`（与 `MediaType.from_file_name` 同一组） | 与 T506 放行视频同一条理由 |

**转写不在本切片**（用户裁决②「不要 FunASR」）：音频的交付面只有「能导入、能读元数据、
能播、能按元数据/文件名检索」。理由与代价逐条登记在 `docs/refactor/s8-deviations.md` §1.1。

## PhotoBench 运行时提供者与遥测（S9a · T901–T904，2026-10-04）

1.0 的 `backend/runtime_providers.py`（869 行）与 `backend/apple_telemetry.py`（418 行）
**没有自己的行为文档**——口径只存在于代码里。S9a 把它们搬进 `apps/photobench`，这张表
是逐行读出来的那份口径，2.0 侧实现见
`apps/photobench/src/photobench/infrastructure/runtime/`。

| 项 | 值 | 出处 |
|---|---|---|
| 系统进程判定·端口 | `8771, 8091, 8500, 8501, 8100, 8101, 6333` | `runtime_providers.py:45-48` |
| 系统进程判定·关键词 | `photobench, benchmark_orchestrator.py, sentrix, vllm, llama-server, ollama, qdrant` | `runtime_providers.py:49-55` |
| 上面两条的 env 覆盖 | `PHOTOBENCH_SYSTEM_PROCESS_PORTS` / `PHOTOBENCH_SYSTEM_PROCESS_KEYWORDS`；**空串与没设同义**（都回到默认值，不是「关掉判据」） | `runtime_providers.py:38-42` |
| Apple 侧的同一组常量 | **独立一份**：多一个 `mlx`，且**不读**上面两个 env | `apple_telemetry.py:35-39` |
| 端点归一化 | `host:port` → `http://host:port`；无 `/vN` 后缀则补 `/v1` | `runtime_providers.py:24-35` |
| 推理探活回退链 | `/v1/models` → `/health` → `/api/health`；全失败报 `unavailable` 并带 `/models` 那次的错因 | `:417-431` |
| 推理超时 | `list_models` `min(15, timeout)`；`health` 的 `/models` 用同一个、回退探活用 `min(10, timeout)`；`chat` = 传入值或构造时的 `timeout`（工厂默认 **180**）；`token_count` `min(15, timeout)` 且不超过构造时的 timeout | `:434, :425, :443-453, :465-472` |
| token 预算的合法性 | `prompt_tokens < 1` 或 `max_model_len < 1` → 抛 `ValueError`（不接受「成功但为空」的预算） | `:475-476` |
| Manager 生命周期 | `_request` 默认超时 **30**；`/profiles` `/state` **15**；`/start` **120**；`/stop` **90** | `:501-518` |
| `/stop` 的默认体 | 调用方不给 payload 时发 `{"timeout": 60}` | `:515` |
| `/profiles` 两种响应形状 | 顶层数组或 `{"profiles": [...]}`，都认 | `:507-509` |
| Manager 遥测超时 | `/gpu-stats` `/process-memory` 各 **10 秒**，失败落 `unavailable` + `error`，**不抛** | `:527-534` |
| 进程内存归属的 PID 规则 | 给了 `model_pids` 就**只认这批 PID**（另吸收 `root_pid` / `tracked_pids` / `processes[].pid`），否则才用 `process_hint` / `endpoint_port` 去命令行模糊匹配 | `:275-296` |
| 产品内存总量（Linux） | 只有「每个相关进程 PSS 都在」且模型的 VmRSS 与 smaps-Rss 两个补偿输入都在时才写；值 = 相关进程 PSS 和 + `max(0, 模型VmRSS − 模型smapsRss)`（UMA 补偿） | `:329-338` |
| 产品内存总量（Apple） | 相关进程 footprint 之和（含模型），**不做** UMA 补偿；仅当**恰好一个**模型进程且它的 footprint 读得到时才写 `root_pid` / 模型内存那几位 | `apple_telemetry.py:352-381` |
| 缺失值的口径 | 一律**缺席或 `unavailable`**，绝不用 0 / RSS 顶替（0 是合法测量值） | 两文件的注释与 `:329`、`:365` |
| 状态词 | `available` / `unavailable`（探了但没探到）/ `not_applicable`（这台机器上不适用，如无 Manager、UMA 无独立 KV 池） | `:570-583, :832-842`，`apple_telemetry.py:417` |
| Apple 设备采样缓存 TTL | **1.0 秒** | `apple_telemetry.py:319-322` |
| Apple 端口扫描缓存 TTL | **2.0 秒** | `apple_telemetry.py:331-336` |
| Orin PSS 采样 TTL | **5 秒**（ssh 比 `/metrics` 贵得多） | `runtime_providers.py:742-745` |
| Orin 端点的绑定 | 写死 `http://192.168.0.118:8100` + `orin@192.168.0.118` + PID 文件 `/home/orin/VLM/gemma4/results/orin/server_photobench_8100.pid`；端点对不上直接 `ValueError` | `:708-715` |
| Orin 的 ssh 口径 | `BatchMode=yes`、`ConnectTimeout=3`、`ConnectionAttempts=1`、子进程超时 **5 秒** | `:758-762, :797-801` |
| Orin 的远端闸门 | PID 文件非空且为数字 → comm 必须是 `llama-server` → 命令行必须含 `--port 8100` → PSS 必须读得到；任一不过即撤掉 `process_memory_used_mib` 并写 `pss_error` | `:746-778` |
| macmon 口径 | `macmon pipe -i 1000` 一个长命 pipe（`-s` 是有限探测，取不到后续样本）；`gpu_active_ratio` → 利用率，`sys_power`/`cpu_power`/`gpu_power` → 功耗 | `apple_telemetry.py:101-159, :75-98` |
| vm_stat 已用内存 | `pages active + pages wired down + pages occupied by compressor`，scope `apple_app_wired_compressed`；三项缺一返回 `None` | `apple_telemetry.py:46-72` |
| 子进程超时（Apple） | `_run_text` 默认 **3 秒**；`sysctl` **2 秒**、`lsof` **4 秒** | `apple_telemetry.py:227-285` |
| 子进程超时（Linux 本机） | `nvidia-smi` **5 秒**（`check=True`，非零退出走 `unavailable`） | `runtime_providers.py:176-180, :598-601` |
| 工厂的两条装配路 | 有 `manager_url` → Manager 生命周期 + Manager 遥测；没有 → 两个 `Unavailable*` 占位。**别的 host 形态（Ollama / llama.cpp / Jetson / Apple / Orin）不在工厂里** | `:852-869`（分支逻辑在编排侧 `benchmark_orchestrator.py:176` 的 `select_runtime_providers`，归 S9b） |

## PhotoBench HTTP 层与结果读侧（S9b-1，2026-10-04）

S9b-1 把 1.0 `benchmark_orchestrator.py` 的 **HTTP 层、配置域与结果读侧**搬进
`apps/photobench`（`application` / `api` 两层 + 进程入口）。这份表记的是那些
「不写下来就会做错」的数值与形状；2.0 侧实现见
`apps/photobench/src/photobench/{application,api,infrastructure}/`。

| 项 | 值 | 出处 |
|---|---|---|
| PhotoBench 端口 | **2.0 = 8772**（1.0 = 8771）；主 API **2.0 = 8191**（1.0 = 8091） | 设计文档 §5.1；`SYSTEM_PROCESS_PORTS` 默认值在 S9b-1 改为 `8772, 8191, 8500, 8501, 8100, 8101, 6333` |
| Apple 侧同一组端口 | 同步改为 `8772, 8191, ...`（数字变了，「多一个 mlx、不读 env」那条差别**不变**） | `apple_telemetry.py` |
| API 前缀 | 本切片仍是 `/api`（与 1.0 相同）；改 `/api/v1` 是 S9c · T924 | `photobench/main.py` |
| HTTP 客户端超时 | 按调用传；1.0 的默认 120 s，老 run 的资产查询用 15 s | 1.0 `:731`, `:6870` |
| 非 2xx 的失败形状 | `RuntimeError("HTTP {code} {reason}: {detail}")`；`detail` 依次取 `detail` / `error` / `message` / 整个对象，正文先截 2000 字符、还原后截 1000 | 1.0 `:744-760` |
| HTTP 重试 | **没有**（judge 自己的重试是另一套，属 S9b-3） | 1.0 `:731-760` |
| TLS 校验 | `request_json` / `request_bytes` **不校验证书**（`check_hostname=False`、`CERT_NONE`）；`request_text` 那一路不这么做，但它不在本切片 | 1.0 `:742-743`, `:802-803` |
| 重定向 | 跟随（`urllib` 默认跟随；httpx 默认不跟，2.0 显式开） | 1.0 默认行为 |
| 请求体编码 | `json.dumps(..., ensure_ascii=False)`（中文原样进 body，不是 `\uXXXX`） | 1.0 `:732` |
| 配置取值优先级 | 环境变量 > `config/runtime_connection.json` > 兜底；**环境变量被显式设为空串 = 未配置**，不再落回文件 | 1.0 `:101-111` |
| 配置加载顺序 | 先读 runtime connection，再读 judge provider（其中 `judge_provider_id` 若在表内则盖过文件的 `default_provider_id`），再读 vLLM target | 1.0 `:288-299` |
| `model_base_url` 的兜底 | 环境与配置文件都没有时，取默认 target 的 `model_base_url` 并去尾斜杠 | 1.0 `:345-350` |
| 缺 `judge_providers.json` | 退回 `judge_providers.example.json`；都没有 / 坏 JSON / 默认 id 不在表里 → **报错**（服务起不来） | 1.0 `:239-252` |
| 缺 `runtime_connection.json` | 当空配置，**不报错**（坏 JSON 同样当空） | 1.0 `:71-76` |
| 缺 `vllm_targets.json` | 与 judge provider 同：退回 `.example.json`，都没有则报错 | 1.0 `:333-345` |
| 模型端点归一化 | 无协议头补 `http://`；无 `/vN` 后缀补 `/v1`；服务 URL 只去尾斜杠、不补路径 | 1.0 `:368-385` |
| judge 密钥遮蔽 | 首 2 位 + 最多 8 个 `*` + 末 2 位；长度 ≤4 时全是 `*` | 1.0 `:308-315` |
| `POST /api/config` 的必填 | `judge_model` 非空、`judge_provider_id` 命中已登记 provider；**只落非密钥参数** | 1.0 `:419-455` |
| 列表分页 | `page` ≥ 1；`page_size` 夹在 1..100；`pages = max(1, ceil(total/page_size))`；`page` 再夹到 `pages` | 1.0 `:779-792` |
| 列表排序 | `started_at`（缺则 `created_at`）降序，缺失按空串 | 1.0 `:5352` |
| `active_count` | `status ∈ {running, pending, cancelling}` **或** `rejudge.status == "running"` 的条数 | 1.0 `:7650-7654` |
| 列表条目带的字段 | 20 个顶层标量里**非 None** 的那些 + `item_count` + 8 项 summary + `phases.qa_eval.{status,progress}` + `rejudge` 五项 | 1.0 `:5309-5341` |
| `GET /api/runs/{id}` | 去掉 `items`、补 `item_count` 与**重算**的 `summary`；`telemetry_live` 深拷贝后并入 `gpu_samples.jsonl` 的采样 | 1.0 `:5354-5385` |
| 题目分页 | `page_size` 夹 1..100；筛选 search（问题或 qa_id 子串，小写）/score/task_type/tag/agent_status/angle/difficulty/answerability/primary；`unfiltered_total` 是筛选前的条数 | 1.0 `:6441-6526` |
| 采样降采样 | 上限 **240** 点，**必保首尾与所有阶段切换点** | 1.0 `:5378-5393` |
| 错误响应形状 | `{"error": <str>}`；`KeyError` → **404**（`str(KeyError("x"))` 是 `'x'`，带引号）；其余异常 → **400** | 1.0 `:7757-7762` |
| `GET /api/current-model` | 回 **405** + `{"error": "current-model requires POST"}`，不是 FastAPI 的 405 形状 | 1.0 `:7530-7532` |
| 启动扫描成本（实测） | 267 个 `run.json` / 11.07 GB：**只读字节 2.9 s，`json.loads` 75.0 s**；单个最大 447 MB（解析 2.03 s） | 2026-10-04 在 153 上实测 |
| 「跳过 items 只读元信息」的代价（实测） | 对那个 447 MB 的文件：`json.loads` **2.03 s**，纯 Python 逐字节跳过 `items` 要 **24.8 s**——C 解析器比 Python 循环快一个数量级，**跳过反而更贵**。所以 2.0 照常解析、只是丢掉 items | 同上 |

## PhotoBench run-execution 主链（S9b-2，2026-10-04）

S9b-2 把 1.0 `BenchmarkRun`（`benchmark_orchestrator.py:1895`）的六个相位搬进
`apps/photobench/src/photobench/application/run_execution/`，并**直接按 2.0 的接口写**
（用户 2026-10-04 的范围调整：端点适配不再留给 S9c 做第二遍）。这份表记的是那些
「不写下来就会做错」的数值与形状差异；2.0 侧实现见对应文件。

### 端点对照（1.0 → 2.0）

| 项 | 值 | 出处 |
|---|---|---|
| 全局前缀 | **2.0 = `/api/v1`**（1.0 = `/api`）。本切片一次到位，S9c 的 T924 只剩收口 | `infrastructure/sentrix/client.py` 的 `API_PREFIX` |
| 建空间 | 2.0 **没有** `POST /api/memory-spaces`。空间由导入路径内部的 `_ensure_space` 建（`import_assets.py:249` / `:495`），`people/seed` 那条路也先 `spaces.ensure`（`identity.py:963`）。**`id` 与 `name` 都是同一个 `scope_id`**（`ensure(id, id)`），而 1.0 的服务端返回 `id` 与 `name` 两个值 | `application/run_execution/service.py::_phase_scope_setup` |
| 导入的单位 | **一个服务端扁平目录**（`{directory, scope_id, limit}`），`ImportAlbum.execute` 用 `root.iterdir()` **非递归**扫后缀（`import_album.py:100`）。1.0 是逐文件 multipart 上传到 `/api/import`。所以「导了哪些文件」的权威从 manifest 变成目录内容 | `_phase_photo_import` |
| 导入的 `limit` | 默认 **200**、上限 **1000**，**超出静默截断**（`paths[:limit]`）。1.0 没有这个参数。主链按目录里的**媒体后缀文件数**填它（填小了没有任何报错） | `infrastructure/datasets/albums.py::import_target` |
| 导入是否同步 | **同步长请求**：一个请求串行走完落盘→EXIF→地理→视觉向量→观察→人物→事件→事实→词法，再跑批末总结（`timeline.py:130-142`）。1.0 是「上传 + 轮询 ingest-batch 两段」 | 同上 |
| 导入的收尾 | 2.0 **没有** `POST /api/ingest-batches/{id}/complete` 与 `/cancel`——语义并入 imports | — |
| 种身份 | `POST /api/v1/people/seed` 是**单人 multipart**；`aliases` 是**顿号或逗号分隔的一个字符串**（`identity.py:895`），不是数组。1.0 是 `seed-batch` 一次 N 人 N 图 | `client.py::seed_person` |
| 资产列表 | 2.0 的信封是 **`{"items": [...]}`**（1.0 是 `{"assets": [...]}`）；**没有 offset、没有分页元数据**；条目**没有 `created_at`**（只有可空的 `captured_at`，字段名是 `file_name`） | `api/schemas/media.py:125-137` |
| 资产列表的 limit | 路由里做 `min(limit, 1000)`。1.0 用 `limit=2000`，所以 2.0 侧超过 1000 会被**静默截断** | `api/routes/media.py:290` |
| 资产状态 | 2.0 只有 **四态**：`queued` / `processing` / `processed` / `failed`（`domain/media/models.py:46`）。1.0 那张表里的 `semantic_enriching` / `video-keyframe-extracting` 等**一个都没有**，所以主链的「待处理集合」= `{queued, processing}`、失败集合 = `{failed}` | `service.py::PIPELINE_PENDING_STATUSES` |
| 问答 | **两段式**：先 `POST /api/v1/assistant/conversations`（拿 `id`），再 `POST .../{cid}/turns`（202，返回 `turn_id`）。1.0 一次 `POST /api/assistant/turn` 自带 `conversation_id`（本地 `uuid4()`，`:3406`） | `client.py::start_conversation` / `start_turn` |
| 回合的 scope | **不在请求体里**——`StartTurnRequest` 只有 `message`，scope 继承会话（`assistant.py:213`）。请求体里的 `conversation_id` 字段存在但**被路由忽略** | 同上 |
| 回合的终态 | `TurnStatus.is_terminal = not in {pending, running}`，即 `complete` / `partial` / `failed` / `blocked` / `cancelled` **都是终态**。1.0 的判据是列举成功值 `{complete, completed, done, success}`——照搬它会把每一个业务失败都变成一次轮询超时 | `domain/assistant/models.py:40`；`service.py::_await_turn` |
| 回合轮询路径 | **复数** `/api/v1/assistant/turns/{turn_id}`（1.0 是单数 `/api/assistant/turn/{id}`） | `client.py::get_turn` |
| 轮询间隔 | **0.5 s**（1.0 `:931` 同值） | `service.py::_await_turn` |

### 2.0 不再暴露的轨迹（本切片留空并说明原因）

| 1.0 的字段 | 2.0 的状况 |
|---|---|
| `model_call_metrics`（每次调用的 ttft / token） | **没有**。`run_turn` 从不读 `ModelReply` 的 token 数，也不落任何表 |
| `debug_trace` / `retrieval_trace` / `execution_trace` | **没有**。`ProgressEvent` 的 payload 里只有 `tool` 与 `tool_call_id`，**没有入参、没有结果** |
| `tool_trace`（含 `debug_asset_ids` / observation） | **没有**。工具结果只进 `agent_turns.attachments_json`（且只有交付那几张） |
| `answer_grounding` / `tool_perf` / `agent2_trace` | **不存在** |
| `retrieved_candidate_media` / `evidence_source_media` / `image_ranking` | **2026-10-09 起部分收回（§3.8）。** 原先「拿不到候选集」——2.0 只给交付集（`result.attachments[].asset_id`），于是 PhotoBench 只能把 `retrieval_media_refs` 直接赋成 GT：分母是 GT、分子是**交付集**，394 道零交付的题 recall 一道不差全是 0.0，**分不出「检索没找到」与「没交付」**。现在 `agent_turns.candidates_json` 落库、终态载荷里 `retrieved_candidates` 与 `attachments` 分开，PhotoBench 新增 `candidate_retrieval_*` 一族，归因表的 R 层改读**候选级**口径并用 `recall_source` 标出来源。`evidence_source_media` / `image_ranking` 仍**没有**——前者要工具级观察（2.0 的 `ProgressEvent` 仍然只带 `tool` 与 `tool_call_id`），后者要候选**顺序**，而 `retrieved_candidates` 是有序的、可以补，属于下一件事 |
| 交付集的媒体类型 | `AttachmentSummary` 只有 `handle` / `asset_id` / `file_name`，**没有 media_type**——类型由 `assets` 表回查 |
| `agent_turn_events` 表 | 存在的，但**唯一读取者**是轮询与 SSE 两个端点，没有别的指标端点 |

> **2026-10-26 收回「模型输入输出不可得」（仅评测侧显式开启）。**
> 1.0 的 `include_debug` 会把每次模型调用的完整 `prompt` / `raw_full` 放进
> `runtime_turns[*].debug_trace`；2.0 此前没有任何等价物。现在
> `POST /assistant/conversations/{id}/turns` 接受 `include_debug: true`（默认 `false`），
> 打开后终态载荷的 `trace.debug_calls` 按发生顺序带 planner / Agent / judge 每次调用
> 的 **实际 `messages`、实际 `tools`、原始 `content` / `thought` / `tool_calls`**。
> 产品对话不打开这个开关，所以 `agent_turns.trace_json` 的体积不随日常使用增长。
> PhotoBench 的 QA 主链固定打开它，并原样落进 `run.json` 的
> `runtime_turns[*].trace_bundle.debug_calls`；8772 的「全流程完整轨迹」与 Agent Loop
> 调用树都从这里取数。旧 run 没有这一格，界面明确显示「未记录」，不做推断。
> 验证：`apps/api/tests/integration/test_run_turn.py::test_include_debug_records_planner_agent_and_judge_with_exact_messages`、
> `apps/photobench/web/src/features/qa/DebugTracePanel.test.tsx`。

### 参数与常量

| 项 | 值 | 出处 |
|---|---|---|
| 模型冷却 | `stop` 之后等 **5 s** 再 `start`（1.0 `:2557`） | `service.py::MODEL_COOLDOWN_SECONDS` |
| 装载退避 | **[10, 30, 60, 180] s**，最多 4 次尝试（1.0 `:2559`） | `MODEL_START_BACKOFF_SECONDS` |
| 等模型就绪 | **600 s**（1.0 `_wait_model_ready` 的默认值） | `MODEL_READY_TIMEOUT_SECONDS` |
| `start` 的参数 | `{"profile": <model>, "wait_ready": False}`，超时 120（1.0 `:2567`，fire-and-poll） | `_phase_model_deploy` |
| `stop` 的参数 | `{"timeout": 60}`，超时 90（1.0 `:2546`） | 同上 |
| 管道轮询间隔 | **3 s**（1.0 `:3031`） | `PIPELINE_POLL_SECONDS` |
| 管道停滞阈值 | `PHOTOBENCH_PIPELINE_STALL_TIMEOUT_SECONDS`，默认 **1200 s**；非数字退回默认值 | `_stall_timeout` |
| 停滞判据 | **稳定签名**：`(待处理数, 已处理数, 失败数, 已离开待处理集合的 (id, 终态) 对)`。待处理集合**内部**的状态抖动不算进展——否则重试环里的状态翻转会永久击败停滞检测（1.0 `:2988-2996`） | `_phase_processing` |
| 收工判据 | 连续 **2** 次零待处理（1.0 `:3010`：状态行可能晚于批次翻转落地） | 同上 |
| 导入超时 | **7200 s**（2.0 的导入是一个同步长请求） | `RunRequest.import_timeout` |
| 回合超时 | **900 s**（1.0 `wait_for_assistant_turn` 的默认值 `:909`） | `RunRequest.turn_timeout` |
| QA 并发 | `PHOTOBENCH_QA_CONCURRENCY`，**默认 1**。1.0 默认读被服务模型的 `max_num_seqs`，2.0 没有那个快照可读——读不到时 1.0 也退回 1（`:3338`） | `_resolve_qa_concurrency` |
| `execution_status` 的分类 | `timeout` / `failed` / `completed`。**判据比 1.0 多一条 `isinstance(exc, TimeoutError)`**：1.0 只按文案里的 `timed out`/`timeout` 判，而它自己的 `TimeoutError` 文案是 "did not complete within Ns"——**里面没有那个词**，于是每次超时都被记成 `failed` | `_evaluate_one` |
| 三个 judge 占位 | `judge` / `task_judge` / `evidence_judge` 都写 `{"score": None, "reason": "pending_judge"}`（1.0 同一位置 `:3555-3557`）。**不打分时不能用 0**——0 是一个合法分数，会污染读侧算出的分布 | 同上 |
| `judge_status` | 成功的题 `pending`、失败的题 **`skipped`**（1.0 `:3220`）。S9b-3 接上 judge 时，前者是「去打分」的队列，后者是「别碰」 | `_phase_qa_eval` |
| 采样相位标签 | 每个采样点带 `phase` 与 `phase_label`（中文，1.0 `:2113` 那张表逐字照搬） | `gpu_sampler.py::PHASE_LABELS` |
| 采样间隔 | **0.5 s**（1.0 `:1691`）；`PHOTOBENCH_DISABLE_GPU_SAMPLING=1` 可整段关掉（2.0 新增，给没有遥测的机器） | `GpuSampler(interval=...)` |
| 样本字段映射 | 逐字段照搬 1.0 `:1717-1775`，含 `processes` → `model_processes` 的**改名**、`vllm_metrics` / `memory_profile` 的**两层展开**、以及那六个进程列表的 ``or []`` 兜底 | `gpu_sampler.py::sample_once` |
| 探不到时不产出样本 | `gpu_stats().status != "available"` → 该轮**不产出样本**，不是产一条全 0 的（0 是合法测量值） | 同上 |
| 结果文件 | `results/<run_id>/{run.json, results.jsonl, gpu_samples.jsonl}`，**形状与 1.0 逐字节兼容**（2.0 仍要能读 1.0 那 19 GB 的 results/） | `infrastructure/results/store.py` |
| `run.json` 的写 | **原子**（临时文件 + `replace`，1.0 `atomic_json` `:576`）：写到一半被读到的 run.json 就是「整个 run 损坏」 | `FileResultWriter.write_snapshot` |
| `results.jsonl` | 每次快照**重写**（不是追加），一行一题 | 同上 |
| `gpu_samples.jsonl` | run 开始时**截空**（1.0 `:2105`），之后**追加**且立即落盘 | `reset_gpu_samples` / `append_gpu_sample` |
| `run_valid` | `len(items) == qa_count` 且**每题** `execution_status == "completed"`（1.0 `:3318`） | `_phase_qa_eval` |
| 相位失败 | 任一相位抛异常 → `status=failed` + `failed_phase` + `fatal_error`，**后续相位不再跑**（1.0 `:2404`） | `execute` |
| 有 partial 相位 | 跑完但存在 `status == "partial"` 的相位 → `completed_with_errors`（1.0 `:2389`） | 同上 |

### 本切片偏离 1.0 的三处（都刻意，且都有用例钉）

| 项 | 1.0 | 2.0 | 为什么 |
|---|---|---|---|
| id 的 URL 转义 | `GET /api/assets?scope_id={scope_id}` **完全不转义**（`:2952` / `:3111`）；`:1439` / `:2456` 用默认的 `quote`（把 `/` 当安全字符） | 一律 `quote(value, safe="")` | 查询串里斜杠合法，但**路径**里会把 `/assistant/turns/a/b` 切成两段。一次收紧，让两种位置只有一种编码 |
| `upload_files` 的非 2xx | 没有 `except`，`HTTPError` 原样冒（调用方只看到 `str(exc)`） | 转成与 `request_json` 同一条 `RuntimeError` 文案 | S9b-2 的调用方是 `seed_person`，它要从 2.0 的 `400 {"detail": ...}` 里读出「哪一步拒绝了」。1.0 的两个上传端点（`seed-batch` / `import`）在 2.0 都不再由这个方法服务，所以不改变任何既有失败语义的观测 |
| `execution_status` 的超时分类 | 只按文案 | 多一条 `isinstance(exc, TimeoutError)` | 见上表——1.0 自己的超时文案里没有那个词 |

---

## S9b-3：judge / 遥测聚合 / 写侧（2026-10-04）

### 新增的行为（1.0 有对应）

| 项 | 值 | 出处 |
|---|---|---|
| judge 重试 | `PHOTOBENCH_JUDGE_RETRY_ATTEMPTS` 默认 **6**；退避 `PHOTOBENCH_JUDGE_RETRY_BACKOFF_SECONDS` 默认 **5 s**，上限 `..._MAX_SECONDS` 默认 **60 s**，外加 `+U(0, min(2, 0.2*delay))` 抖动（1.0 `:206-215` / `:4508`） | `application/run_execution/judge.py` |
| judge 4xx 不重试 | 400/401/403/404/422…**立刻抛**；**408 与 429 排除**（它们重试才对症，`Retry-After` 就是给它们的） | `JudgeRunner._client_error_code` |
| judge 请求间隔 | `PHOTOBENCH_JUDGE_REQUEST_INTERVAL_SECONDS` 默认 **0.5 s**，多个 worker 之间错开 | `_wait_for_request_slot` |
| 分数—理由一致性 | `score=0` 而理由明确肯定回答正确 → **重问一次**；仍矛盾则`score=None`，原失败语义保持。新结果`judge.consistency_version=2`，褒义匹配限同一标点分句，排除问题“可回答/可以回答”，不能把下一句的正确参考事实当成模型回答正确；未带版本历史保留v1，不从缺失重问事实回填分数 | `JudgeRunner.judge` / `analysis.judge_score_consistency` |
| 判分失败的形状 | `judge_status="failed"` 且 `score=None`——**判分失败不是 0 分**（0 是合法分数） | 同上 |
| 证据判分开关 | `BENCH_EVIDENCE_JUDGE=1` 才开（默认关）；判分前把召回的图缩到 **896 px** 再转 JPEG data URL（1.0 `:1549`） | `JudgeRunner._inline_images` |
| 一个 item 的判分粒度 | **每一轮各判一次**（任务行为 + 回答质量并发），item 级取**最后一轮**；任一子判分 failed → item `judge_status="failed"` | `JudgeRunner.judge_item` |
| 遥测聚合的分位数 | `peak` / `mean` / `p50` / `p95`；**p95 在 n<20 时退化成 peak**（1.0 `:1800`）。n=21 起按秩取值（`sorted[int(n*0.95)]`） | `analysis.aggregate_gpu_samples` |
| 三套 memory_profile | UMA（`process_pss_uma_mib`，按样本 `source` 报 jetson/orin）／Apple（`apple_phys_footprint_mib`）／默认（`fixed_base = idle - kv_capacity`，`comparable = fixed_base + capacity*peak%`）。**没有 kv_capacity 就不给估算** | 同上 |
| idle 基线 | 「KV 占用为 0」的样本里**最小**的进程内存；没有这种样本时退回 p50 | 同上 |
| 实时视图尾部 | `PHOTOBENCH_LIVE_TELEMETRY_TAIL_POINTS` 默认 **240**；完整曲线始终在 `gpu_samples.jsonl` | `service.py` / `analysis.apply_live_sample` |
| 滚动聚合节流 | 每 **2 s** 发布一次 `phases.gpu_metrics`（`partial=true`）；已 `done`/`skipped` 的相位不再被覆盖 | `service._publish_partial_gpu_metrics` |
| 后台落盘 | 快照由后台线程写，**debounce 合并** `PHOTOBENCH_PERSIST_DEBOUNCE_SECONDS` 默认 **0.25 s**；`persist(wait=True)` 等到落盘（1.0 `:211` / `:2056` / `:2080`） | `state.py` |
| 取消语义 | 跑着 → `cancelling`（相位循环发现后收成 `cancelled` + `cancelled_phase`）；还没跑 → `cancelled`。取消**回收本轮起过的模型**（Manager 报的 profile 与本轮一致才停） | `state.request_cancel` / `RunExecution.cancel` |
| 取消感知的等待 | 模型就绪轮询、冷却、退避、管道轮询、回合轮询、judge 退避与限流**全部**可被取消打断 | `service._cancel_wait` / `JudgeRunner._wait` |
| judge 提示词的存储 | `config/custom_judge_prompt.json`（`answer_quality` / `task_decision` / `evidence` 三键，旧的单键 `judge_prompt` 迁到 `answer_quality`）；空串表示恢复默认 | `application/config/service.py` |
| judge 密钥的存储 | `config/.env.local` 的 `BENCH_JUDGE_API_KEY=`，写后 `chmod 0600`；**不进 `runtime_connection.json`** | `infrastructure/config/env.py` |
| 写入长度上限 | judge 提示词 **50000** 字符（1.0 `:7786`） | `api/routes/config.py` |

### 本切片偏离 1.0 的地方（都刻意）

| 项 | 1.0 | 2.0 | 为什么 |
|---|---|---|---|
| `mode` | `full` / `reuse` / `build` | **只接 `full`**，其余显式 400 | 2.0 删掉了 `scope_attach`（scope 由导入内部 ensure），`reuse` 没有可复用的入口；静默降级成 `full` 会建出一个调用方想避免的新 scope |
| `POST /api/runs/{id}/resume` | 断点续跑 | **501** | 要重建一个 `mode=resume` 的 run + `existing_scope_id`，而 2.0 没有「复用已存在 scope」那条路。返回 501 而不是假装成功 |
| 重判的轮次来源 | `item["conversation"]`（题面声明） | `item["runtime_turns"]`（实际跑过） | 2.0 的 item 不保存 `conversation`；混用会让 item 级分数取到一个没有 judge 的轮次 |
| 重判的判分范围 | 回答质量 + 任务行为 + 证据 | 回答质量 + 任务行为；**证据判分不重判**，标 `not_applicable` | 证据判分要按 asset_id 现取图片字节（1.0 `:7226` 打活着的 2.0）；`EVIDENCE_JUDGE` 默认关，留一个上次的值比标 not_applicable 更容易误读 |
| 内存画像的模型同步 | 装载后调 `bind-runtime` 把 Sentrix 指向新模型 | **留空**并记 `gamma_sync.ok=false` | 2.0 没有该端点（换模型=重启 2.0，S9c 的 T923）；不假装同步过 |
| 云端大模型 | `use_cloud_model` / `big_model` profile | 分支保留、**没有生产入口** | 2.0 没有云端模型这个概念；`select_runtime_providers` 的 `cloud=` 参数照搬留白 |
| `OrinLlamaCppTelemetryProvider` | 在选择逻辑里**没有任何分支构造它**（import 了但没用） | 同样不构造 | 照搬 1.0 的分支，不替它补一条没人验证过的 ssh 采样路 |

---

## S9c：两个死端点 / 换模型重启 / openapi 收口（2026-10-04）

### 新增的行为（1.0 有对应）

| 项 | 值 | 出处 |
|---|---|---|
| 换模型 = 重启 2.0 | 本轮请求的 `model_base_url` 与 2.0 记着的端点**相同 → 不动进程**（`already_pointing_here`）；不同 → 停掉旧进程、以 `SENTRIX_VLM_BASE_URL=<新端点>` 起一个新进程、等 `/api/v1/health`。它是 1.0 `POST /api/model-profiles/bind-runtime`（`:2611`，注释写明 "no restart"）在 2.0 的形态 | `application/deploy/service.py` · `infrastructure/deploy/sentrix_process.py` |
| 端点的传递媒介 | **环境变量 `SENTRIX_VLM_BASE_URL`**——2.0 的 `Settings`（`env_prefix="SENTRIX_"`）在**启动时**读死它（`Container.build` 的 `ServedModel(settings.vlm_base_url)`），不是可热改的配置 | `sentrix_process.py::VLM_BASE_URL_ENV` |
| 记录当前端点 | pid 与端点写在 `apps/photobench/logs/sentrix-process.json`（`{"pid", "endpoint"}`）。**pid 已死 → 当前端点视为空**（崩溃之后不会一直说「已经指着这里」） | 同上 |
| 停旧进程 | 两个来源：pid 记录里的；`lsof -ti tcp:<port>` 反查到的（`make dev` 起的那个不在记录里）。**先停后起**，否则两个进程抢同一个端口 | 同上 |
| 健康判据 | `GET /api/v1/health` 回 `ok` 或 `degraded` 都算起来（`degraded` 只表示库有问题，进程是活的） | 同上 |
| 部署参数 | `PHOTOBENCH_SENTRIX_API_URL` / `_REPO_ROOT` / `_START_COMMAND` / `_PID_FILE` / `_LOG_PATH` 五个环境变量，默认值只对「2.0 与 PhotoBench 同机、走 `make dev`」成立 | `photobench/config.py` |
| `gamma_sync` 的落盘形状 | `phases.model_deploy.gamma_sync = {ok, error, restarted, endpoint, previous_endpoint, pid, reason}`。**保留 1.0 的 `ok` / `error` 两个键**，其余是诊断字段 | `domain/deploy/ports.py::RestartOutcome.to_state` |
| PhotoBench 自己的 openapi 契约 | 导出到 `packages/contracts/openapi/photobench.yaml`；`make check-docs` 用 `--check` 比对。**文件名没有 `-v1`**——PhotoBench 的路由前缀仍是 `/api`（改它要动前端，属 S10） | `scripts/export_photobench_openapi.py` |

### 本切片偏离 1.0 的地方（都刻意）

| 项 | 1.0 | 2.0 | 为什么 |
|---|---|---|---|
| `bind-runtime` 失败的后果 | **非致命**：异常收进 `gamma_sync.error`，run 继续跑（`:2613`） | **致命**：真的重启过却没起来 → `model_deploy` 相位 `failed`，run 收成 `failed` | 1.0 的 `bind-runtime` 只是同步一个**缓存**，Sentrix 答题用自己直连的端点；2.0 的助手用**自己的** `vlm_base_url` 答题——重启失败意味着这一轮的每个答案都来自另一个模型，继续跑只会产出「标着 A 模型、其实是 B 模型」的结果 |
| 内存画像的模型同步 | 装载后调 `bind-runtime` | **仍然留空**（`gamma_sync.ok=false`），但原因改了：内存画像那条路径的请求体里只有 `vllm_manager_url`，**没有端点来源**。与其猜一个端点去重启主 API（代价是可能把正在跑的评测指歪），如实记没做 | `application/runs/suites.py` |
| `POST /api/runs/{id}/resume` | 断点续跑 | **501**（未变，S9b-3 的处置沿用） | 同 S9b-3 |
| 批量导入的取消 | `POST /api/ingest-batches/{id}/cancel`（1.0 主 API **从不存在**，被 `except Exception` 吞成静默 404） | **2.0 没有对应能力，删除**：`POST /api/v1/imports` 是同步请求，没有批量实体可取消 | 见 `docs/refactor/s9-deviations.md` |
| arbiter 状态 | `GET /api/arbiter/status`（1.0/2.0 主 API **从不存在**；1.0 的 PhotoBench 前端轮询它，占 8091 流量 17%） | **2.0 没有对应能力，删除**：S9b-1 只建了真正需要的路由，这条从未加过 | 同上 |

### S9c 真机验收抓到的两个缺陷（已修，行为已回到「1.0 没有这个问题」）

| 项 | 缺陷 | 2.0 修后 |
|---|---|---|
| 重启时的进程发现 | `lsof -ti tcp:PORT` 会把**客户端**也列出来（httpx 默认 keep-alive），照单 SIGTERM 会误杀 Web 前端 / PhotoBench 自己 / 任何在调 2.0 的进程。真机验收里驱动脚本就这样被自己触发的重启杀了 | 只认**监听**进程（`-sTCP:LISTEN`） |
| 读侧状态缓存 | `ReadRuns._state` 的 LRU 2 永不失效，run 完成之后 `GET /api/runs/{id}` 仍回那份「running」旧快照（1.0 对活跃 run 持有活对象，没有这个问题） | 缓存**只对终态生效**；非终态每次独立读取重新解析，同批并发详情共享在途投影，结束即清理 |

## 2026-10-21 T006 指标正确性基线 v2

运行拓扑冻结；实际HTTP和遥测共用快照。Agent墙钟已排除Judge，不重复扣除；吞吐使用独立单调计时阶段墙钟。原生工具参数JSON只以真实解析尝试为分母，普通文本不计；旧JSON动作不适用为null。完成率依据终止原因、非空答案及真实步数预算。

NX/AGX共用RSS+nvmap与产品PSS+主/辅助nvmap估计；NVIDIA的RAM与VRAM分开，端口明确不扩大进程名匹配；Mac拆footprint和映射权重容量估计。必要分量未知时总量null，确认不存在的辅助才为0。功耗保留电源轨与范围，采集流退出/超期失效。nearest-rank P95，完整有效样本统计后降采样。

生产继续允许缺失遥测；独立四机验收检查新鲜度/完整性/算术与全链路，详见架构指标契约和xy部署手册。历史原文件不改写，仅据现存事实派生；不得补造归属或解析事实。

NX真实预检暴露BGE-M3的llama.cpp接口与Python侧车不同：`/embed`404，
`/v1/embeddings`明确拒绝pooling=none。legacy适配器按404及特定错误做有界协议协商；
原生token矩阵取CLS并L2归一化，成功后缓存协议。普通400/401/429/500不触发切换，
空/错维度/非有限/零CLS/多输入返回失败空表。独立验收器重新计算向量并登记协议，
真实本地HTTP测试覆盖此故障，不重启或改变模型服务。

真实Mac全链路进一步暴露进程快照自包含：`ps`将自身列为PhotoBench子进程，
随后退出令全部产品合计不可用。只按实际启动的观察进程PID排除它，不能按进程名过滤真实工作进程；
其他相关PID消失仍保留不可用语义。固定release发布将API的PID/端点登记到独立runtime路径，
PhotoBench的控制器复用该记录，端点一致的首个full run不得再次重启API；控制日志同样绑定runtime。


运行拓扑冻结同样覆盖产品重启：bootstrap按run快照构造subprocess/systemd控制器，
以内容哈希寻址、原子发布的只读runtime_connection.json作为既有PHOTOBENCH_CONFIG_ROOT。
实际启动脚本不能重新读取后续全局配置。控制记录同时保留无密钥拓扑；同模型但嵌入拓扑不同
不得视为already_pointing_here。旧未注入快照入口保留；端口方法和公开schema不变。


2026-10-22真实10QA进一步确认预算事实位于runtime_turns[*].agent2_trace.budget_outcome，
题目顶层无该字段。步数内完成率必须按实际回合读取；多轮题全部回合满足终止原因、非空答案、
状态与预算才算完成，已知失败优先于其他回合的缺失。只有事实缺失时保留null，不回填预算。
独立验收从回合原始事实复核完成率、工具参数JSON分母及阶段墙钟吞吐，防止顶层夹具掩盖真实结构差异。

2026-10-22补齐前端实际投影：阶段快照的峰值回退只接受有限数值，null/布尔/空串不转0；
降采样历史不得冒充缺失的完整阶段峰值。实时行与阶段行优先使用memory_accounting，
Jetson显示RSS/nvmap/PSS各分量及板级/轨级功耗，Mac显示容量估计与active+wired+compressor；
旧无指标定义的历史source仅用于兼容。生产schema与失败语义不变。

### T006 Judge标准化与分维验收（2026-10-22）

score只接收0/1/2及严格有限整数数字字符串，domain集中v1规则，bool/空/非有限/小数/越界/文字拒绝；保留每次原响应及转换事实，一致性最多重问一次。执行和读取汇总一致；历史仅显式事实可作带源哈希派生，不覆盖原文或将原执行改成功。独立验收不再因任意error直接判服务失败：终态证据、受控恢复、任务失败、评分覆盖及基础设施分别报告；遥测公式/null/3秒覆盖门槛不变。回归按影响，共用语义四机完整10QA，人工取消保留替代原因。

Judge一致性v3进一步限定为明确“模型/助手的回答/答案/结论”或分句开头针对回答的肯定断言；参考答案已回答正确地点而模型未能作答的合法0分不重问。真正模型回答正确却给0仍重问一次；v1/v2历史语义保留，新执行consistency_version=3，标准化v1不变。


### Jetson 核心采样关键路径（2026-10-22）

9dad22b 实际 AGX 导入仍出现3.493秒缺口。继续定位发现gpu_stats先读主模型PSS，
process_memory又对同PID读完整PSS/RSS；设备探针不再重复遍历smaps，唯一的新鲜进程
观测归process_memory。可选llama.cpp /metrics各IO超时从2秒收紧至0.2秒，繁忙时KV为
不可用，不保留前次值；不改变主模型HTTP请求超时、内存公式、源TTL或严格3秒覆盖门槛。
这是Jetson适配器内部修复，无公开schema、端口、装配点或部署配置变化。真实HTTP繁忙
测试确认在服务端任务结束前返回不可用；完整采样测试确认每PID仅一次smaps遍历。
影响范围仅NX/AGX；固定新提交、完整CI后这两机新scope full10QA。3090/Mac继续使用
9dad22b正式证据及该版本前端产物，不将旧run改标新SHA，版本分别追踪。


## 2026-10-22 PhotoBench运行/构建基线

- full/build/reuse 创建队列前冻结快照，reuse不得绕过产品配置同步；resume继承原依赖/预算，标注来源。
- build 不依赖QA/Judge，0题和评分未适用；构建完整性与scope嵌入配置独立记录，不复用不完整/已变更索引。
- Sentrix安全HTTP重试有总预算且不重放不确定POST；Judge策略独立。排队取消在execute前保留，后续run不可逃过取消。
- Mac集中profile缺省600/180秒、QA2、导入86400秒，显式覆盖及QA0语义保留；provider从Settings注入，不静默CPU回退。
- 模型名记录原始端点ID，Qwen3.5不截断，未记录不以__current__伪装名称；同步导入等待期间媒体完成数/失败数未知显示 `?`，不以 `0` 伪造媒体进度。
- 导入“阶段墙钟”来自相位总耗时（兼容历史上传耗时/完整起止时间），“后端计时”来自内部timings.total，分别标注；阶段耗时不把null、布尔、空字符串或负数显示为0。

### 2026-10-22 内存展示口径补核

主指标卡按显式memory_accounting识别平台，Mac/Jetson使用“主模型内存占用”标签；Mac的footprint+映射权重明确为容量估计，映射不保证驻留。Jetson现行说明为nvmap优先、不可读时仅正残差作注明来源的估计，未知分量使合计不可用；不承诺跨驱动和共享页物理去重。无现行accounting元数据的历史记录不套新公式。只修前端标签/说明及元数据传递，不改变采集、公式数值、执行、接口或原始记录；必须以真实run核验新显示，实际10QA证据保留原应用SHA。


### T006 详情读取与轮询并发修正（2026-10-22）

App重建QA桥接函数会改变loadActiveRun引用；该引用不得决定轮询effect生命周期。
轮询始终在当前请求完成后等待2秒（失败5秒），下一次使用最新loader；重新渲染不立即追加请求。
ReadRuns合并同一run的并发详情计算，不创建新线程或永久活跃状态缓存；下一批重新读取。
不同run不互相阻塞，首个读取的异常原样传播给等待者，错误后仍可重新观测。
API/端口/指标公式、null、全样本统计和3秒验收阈值不变，不减少原始采样或分母。
验证：useRuns.test.ts回调变更/最新loader；test_run_read.py八并发、错误传播、完成态刷新及不同run隔离。

### T006 终态发布顺序（2026-10-22）

完成、失败、取消均先保存待发布终态，采样停止、最终统计和末尾硬件快照完成前保持running/cancelling。
随后统一写hardware_snapshots.end/current_phase=null/finished_at/status，再等待最终快照落盘。
旧版提前completed会让读侧停止轮询、套件误以为空闲，raw在终态后仍变化；新规则不改变QA结果、评分或公式。
覆盖完成/失败/取消三种阻塞末尾观测场景，验证中途非终态及最后落盘与返回一致；不增加API字段。


## 2026-10-22 PhotoBench采样诊断与分量新鲜度

相对02d1883，修复空holder导致的实时阶段聚合从未发布；采样从“操作完成再等间隔”改为单调绝对周期，超时跳过周期。新增独立发布、每操作计时和采样/写盘/发布失败事实，取消仍保留partial统计，不将增强遥测失败改为终止生产任务。

Jetson新增memory_observation_version=1：缓存nvmap保持原时间，RSS/PSS与身份/拓扑分别记录；必要来源缺失/过期时分量和依赖合计为null，不影响独立系统内存。确认无辅助服务的0也必须有新鲜拓扑。历史无该版本保留旧事实，既不补时间，也不把旧NX采样缺口改判通过。采集预算和有界停止均不承诺内核IO硬实时；停止未完成须显式报告。


### 2026-10-22 Jetson监听缓存按端口选择隔离

NX新版实测发现同一采样周期先查询主模型端口、再查询完整产品端口集合；旧单槽缓存相互覆盖，持续重复全量扫描/proc/fd。改为按排序后的端口集合保存独立条目，每次仍核验监听socket和PID启动标识，TTL保持2秒；清除过期条目且最多保留32份，避免配置变化造成无界缓存。不改变端口、公式、null/TTL或3秒验收阈值。回归测试交替查询单端口/多端口并复用PID，先红后绿。仅Jetson基础设施路径受影响；发布后NX重新full→build→reuse，AGX待主模型恢复再回归。153/Mac不调用此实现，原实际SHA证据继续保留。真实时序效果须由新版本运行确认，不能由缓存测试提前记为通过。


## 2026-10-23 验证分层

按用户决定，日常make ci不再强制真实权重/整册/在线模型专项；49例以real_environment标记保留，
独立test-real要求实际执行且零skip。纯算术、阈值、接口、采样/缓存契约仍在日常CI，覆盖率≥85%不变。
10QA独立验收部署与全链路，不能替代所有专项功能，证据分别记录真实版本和覆盖范围。
本次只改测试编排、地理检索fixture与文档，不改变生产失败语义、公开schema或指标公式。

HTTP契约的默认人脸检测使用确定性无脸假件；具体身份测试自行注入检测结果，
真人脸权重验证留在专项。assistant/search共享此隔离装配，不依赖本机是否装有权重。


### 2026-10-23 Jetson进程发现缩小读取范围

NX c3dbd09实测拓扑最慢2.295秒，核心内存有效缺口6.078秒，未通过3秒门槛；同时存在严重内存/交换压力，不能将全部延迟归因代码。端口回退发现改为只核验实际监听候选；进程清单每PID先单读stat建父子图，再仅给选中监听及当前子孙读取RSS/命令行并复核start_ticks。新子进程每轮发现、共享子孙去重；PID复用或消失不沿用旧值。无scope的旧关键词规则、本机显式PID或无端口关键词配置仍全量发现，远端local=false不触发本机扩展。公式、预算、null、缓存TTL、接口及3秒阈值不变；无新增模块、端口或装配点。专项覆盖候选过滤、新子进程、PID复用及兼容分支，真实改善须由新固定版本NX full→build→reuse验证；AGX仍等待模型恢复，153/Mac不调用该路径。

## 2026-10-23 PhotoBench 长期运行修复

六项持久化/生命周期缺陷修复及轻量读取见 [本轮偏差](2026-10-23-long-running-deviations.md)。旧读写端口保留，新增可选修订与overview能力，API新增include_history参数且旧默认不变。普通前端轮询使用检查点；全曲线显式加载、分析展开加载；缓存删除/重判失效。提交以generation清单为权威，旧根文件为兼容导出；恢复留备份，旧历史和原始遥测不删。null/TTL/P95/内存公式及验收门槛不变。与全存储拆分、全进程统一shutdown的后续边界明确区分；未开展真机或24小时专项。

### 2026-10-23 同步导入的真实进度

旧POST契约及导入顺序保留，新增可选operation_id及独立GET进度。新增/去重/失败互斥计数，扫描前total=null；收尾期间不得以媒体100%标整段完成。异常、不可用和重启404均显式展示；PB最后观测写入run检查点与各目录结果，既有原文不补写。产品进度是单进程有界观测，不是可取消的异步作业；取消仍在目录边界生效。协议、保留上限与测试见本轮偏差及module-interfaces。

CI暴露并确定性复现的既有SSE交接竞态：首次历史查询后任务恰好终态，旧路由直接返回complete，漏掉两次读取之间的新事件。现在终态与broker退出时按cursor补读持久事件再complete；关闭channel后订阅不再等待不会发生的下一次唤醒，结束时补发尚未交付的backlog。原事件顺序、去重、断线续传和“首条计划”断言保留，新增确定性竞态测试。只修复事件交付，不修改Agent决策、模型调用或质量规则。


### T006 Judge实时进度（2026-10-23）

QA阶段的规范进度存于`phases.qa_eval.progress`：`agent_completed`为已终态题数（含明确任务失败，不含scheduled/running），`completed`保留成功完成题数；`judge_completed`为已结束的Judge题数（含评分失败，不含跳过），`judge_total`为应评题数，`judge_skipped`单列。有效分数仍由统一评分规则统计为`summary.judge_valid_count`，合法0分有效，失败不补0。新执行在持锁检查点同步items和进度；阶段外层agent/judge计数保留兼容并与内层相同。未配置Judge时内层Judge计数为null、并发为0，前端显示未配置。

Agent和Judge仍使用独立线程池，协调器等待任一池完成即提交结果，不再等全部Agent结束才回收Judge。恢复运行从已有明确Judge终态初始化计数，跳过事实单列，不重新请求既有评分。Agent阶段墙钟在最后Agent结果返回时结束，不包含等待Judge尾部。前端阶段卡和列表共用同一读取函数，优先内层、缺失时兼容外层；没有记录显示?，不以0替代未知。历史原文不改写，旧运行自身未更新的计数仍属于旧检查点事实，不能据有效评分数补造完整Judge终态。

复现测试覆盖一题Agent阻塞时另一题Judge的合法0分、返回失败和抛异常及时可见；resume既有评分、失败/跳过、取消未开始题不假报完成、可空投影及历史外层字段UI。仅本地验证，未触碰四机或启动真实测评；后续发布按公共执行路径影响验证。

## 2026-10-27 158 性能修正（页面容量 / 图像上限 / 建库并发 / 索引自愈）

依据 `~/Downloads/sentrix-158-性能调查.md`（158 Mac 全链路实测）。四下改动的共同点是
**只改被实测证明的路径，不引入可选档位**：数值全部落成常量。

| # | 项 | 旧行为 | 现在 | 为什么 |
|---|---|---|---|---|
| P1 | 结果集每页条数 | 6 | **12**（`ResultSet.PAGE_SIZE`，与 `MAX_SEARCH_LIMIT` 相同） | 检索上限就是 12，一页给全；6 张一页让几乎每题多一次 `get_result_page` 模型步（Mac 实测约 16.9s） |
| P2 | 发给视觉模型的图片长边 | 原图，无上限 | **1280**（`vlm_observer.IMAGE_MAX_SIDE`），人物预览先缩再画框 | 3264px 原图观察 4,260 prompt token / 48s，1280 为 1,445 / 24s；精读 ~4,100 → ~1,295；观察/精读语义抽检 8/8 一致 |
| P3 | 人物瞬间抽取并发 | 串行 | **4**（`settings.album_workers`） | 建库 `person_insights` 418s → 236s；已确认人物的 moments 条数不变 |
| P4 | 派生索引补写 | `already_indexed` 直接返回 `mirrored=True` | 从权威向量**幂等补写**镜像 | 嵌入式 Qdrant 写失败只记警告；旧路径永远补不回来（158 实测缺 344 条，`_staleness_reason` 全局比较，拖累所有 scope） |
| P5 | 检索结果集容量 | 12 | **24**（`DEFAULT_SEARCH_LIMIT`/`MAX_SEARCH_LIMIT`）；`total` 进 `model_visible` | 让 RRF 的前 24 名有用武之地；不分页的两页候选只能看到前 12。`total` 明写「这是本次检索的候选数，不是相册真实数量」 |
| P6 | 翻页语义 | 游标式第 2 页 = 剩余候选，工具随时可调 | **游标式第 2 页（第 13 张起直到结束）**；候选 ≤12 张时工具不出现；**成功翻一次后 JIT 剪掉**；page<2 直接拒绝 | 158 实测 15 次翻页里 14 次要的是第 1 页，每次白花一个约 22s 的模型步。翻页最多只需一次，由代码收口而不是靠模型自觉 |
| P7 | 旧 preview 压缩 | 全文保留到回合结束 | **不做压缩**；改由一次性翻页从源头消除重复调用 | 实测压缩会让模型反复重读（单题 4–5 次翻页、254s vs 118s）；两页内容互补，压缩必然丢信息。一次成功翻页后工具消失，重复调用不再可能 |
| P8 | planner 提示词 | 1,435 tok | **868 tok（−40%）** | 删掉 JSON Schema 已强制的字段形状/scope_id 禁令（合计 ~106 tok），合并证据声明规则 777→287 tok；value_type 判定表 465 tok 原样保留 |
