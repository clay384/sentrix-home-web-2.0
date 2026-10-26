# PhotoBench metrics contract v2

本契约是 T006 指标正确性修复的兼容性基线；不补齐 vLLM 专项指标。

## 2026-10-22 采样与分量观测补充（memory_observation_version=1）

新增可选 `memory_observations`（来源ID到观测事实）、`memory_dependencies`（指标到必要来源ID列表）、
`memory_observation_version=1` 与 `sampling_diagnostics`。来源事实包含 `source/status/reason`、
`observed_at_monotonic`（开始读取时刻，保守计龄）、`finished_at_monotonic`、`ttl_seconds`、
`cache_hit`，进程来源另含PID及启动标识。nvmap缓存复用原观测时间，不以返回时间刷新。
合计依赖最老的必要分量；过期/缺失/进程身份变化使该分量及其依赖合计为null，其他有效分量保留。
明确不存在的辅助分量可为0，但仍依赖新鲜topology观测；空依赖不可证明有效；历史无version不补造时间，新采集严格验证在domain集中执行，
写入、读取及汇总共用。系统内存独立观测，不因GPU来源失效而清空。

`sampling_diagnostics`记录锁等待、三个provider耗时、采集总耗时、计划延迟、错过周期及上次回调耗时。
采样按单调时钟计划，超预算跳过错过的周期，不忙等追赶；所有实际点及超时事实保留。
阶段内滚动聚合由单个可停止的发布线程读取全样本快照执行，不在采样锁中计算，不建立无界队列。
原始JSONL仍逐点写入；写回调失败记录原因，不能静默宣称保存完整。`gpu_metrics.sampling_integrity`记录callback/publisher/collection失败及原因、missed_intervals和stop_pending_threads；正常停止后才发布任务终态，若共同等待10秒仍有线程则明确记录停止未确认（telemetry_live.status=stop_pending），严格验收失败，不把遥测增强改为终止生产任务。成功停止为stopped。
可选探针缩短预算，进程扫描利用冻结拓扑；内核单次不可抢占IO仍如实记录超预算，不宣称硬实时保证。
失败/取消未走完采样阶段时以gpu_metrics.status=partial保留已有统计，前端不得清掉partial标记。四方法端口和生产fail-open语义不变，无新部署开关；3秒覆盖门槛不放宽。本轮仅本地修复，不启动真实测评。

## Additive interfaces

- New runs persist `metrics_contract_version=2`, `build_revision` and an immutable,
  secret-free `runtime_snapshot`. Model, Sentrix and telemetry clients use this
  snapshot; changes to saved configuration affect subsequent runs only.
- TelemetryProvider retains its four existing methods and status envelope.
  Optional data describes `memory_accounting`, `memory_complete`, raw memory
  components, power rail/scope, observation age and freshness. Unknown numeric
  components remain null; incomplete totals remain null. Explicit absence is 0.
- Samples preserve independent sources when another source fails. Stream values
  expire after three source periods and are never re-stamped as new observations.
- Product model-call traces add optional tool-argument parse counts and outcomes.
  They contain execution facts, not benchmark judgments or raw prompts.
- Native tool-argument JSON parsing has its own denominator; plain-text answers
  are not JSON attempts. Legacy JSON-action metrics remain available for legacy
  structured-action traces. Missing observations are not inferred successes.
- v2 item `wall_clock_ms` is Agent elapsed time excluding Judge. Phase throughput
  uses measured monotonic wall time, including overlap, never sums item latency.
- Optional build identifiers are exposed through existing config/diagnostics
  responses. Existing URLs, error envelopes and required fields remain unchanged.
- New answer-quality Judge results add optional `judge.consistency_version=2`.
  Consistency checks must stay within punctuation-delimited clauses and must not
  mistake question answerability or the reference answer for praise of the model.
  A legitimate zero remains zero; contradictory praise still triggers one retry.
  Unversioned historical Judge records retain version 1 semantics. Missing scores
  cannot be recovered from an earlier raw response when retry facts are absent.

## Accounting and historical compatibility

Jetson: system=MemTotal-MemAvailable; model=smaps RSS+nvmap; product=related PSS+
main/auxiliary nvmap. These are attribution/allocation estimates, not a proof of
physical-page deduplication. Positive VmRSS residual is a labeled fallback only.
NVIDIA host RAM and device VRAM remain distinct. Apple footprint and mapped
weights are separately retained and their sum is a capacity estimate; system
active+wired+compressor is platform-specific.

Historical files are not rewritten. Deterministic read projections may be
recomputed from recorded facts, using the declared contract version. Missing
attribution, parse observations, timing or samples cannot be reconstructed.
P95 uses nearest-rank; mean is sample mean and peak is sampled peak. Statistics
use valid full-resolution observations before display downsampling. Read and
write projections apply the same value validation.

## Acceptance

The regression report distinguishes metric correctness from full-chain success;
it does not redefine `run_valid`. Four devices run the same committed release,
dataset and 10 QA in full mode. Every service is local except cloud Judge. Each
required dependency is exercised before starting; all recorded failures and
Judge denominators remain visible. General telemetry remains optional.


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

2026-10-22设备源新鲜度补充（device_freshness_version=1，可选投影）：
设备读数在采样持久化时按同一单调时钟重新计算age，不能沿用gpu_stats返回时的age。
新增可选device_observation_ttl_seconds（流源为三个真实刷新周期），并保留device_observed_at_monotonic。
TTL失效仅清空设备数值及轨道读数并标source_status.device=unavailable，保留有效进程/系统内存。
历史已有完整单调时间戳时允许按记录TTL派生；旧缺TTL用明确标注的3秒兼容门槛，
派生结果标device_freshness_version=1，原始文件不改写。没有时间事实时不补造新鲜度。
独立验收从采样时间减源观测时间复核age和TTL，禁止仅信任已保存的age。

## Judge score标准化v1与验收分维（2026-10-22）

新增可选字段`score_normalization_version=1`、`score_normalization`（rule_version/input_type/status/converted/score）、`attempts`（每次input/raw_response/raw_text/解析及一致性事实）。只从score严格解析ASCII十进制数字字符串，接受有限整数0/1/2；bool/null/空串/非有限/小数/越界/混合文本拒绝。reason不用于猜分。真正矛盾最多重问一次；失败保留全部已收到响应并无分，不能用首次分数回退。

历史原文不改。只凭明确保存的score或无歧义raw_text生成`score_derivation`（规则版本、源、源文本SHA256及派生分），磁盘原score/status不改；API派生投影score为标准分并附original_score，缺失重问事实或错误收尾不补造。API派生/汇总/前端采用domain同一规则，派生不代表原执行评分成功。

独立验收分服务/指标、任务表现、评分证据：受控恢复须有既定异常类别和后续成功轨迹；真实任务失败单列，未知终态、漏题、丢记录、算分错误归服务缺陷；OOM/断连/重启单报基础设施。10QA全部有终态，评分完整仍要求10/10，不补0；均值注明有效覆盖分母。错误率报告分子/分母和对照版本，不为通过调整阈值。3秒内存覆盖及TTL门槛保持。

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


## 2026-10-22 执行与构建契约（新增可选字段）

创建时冻结拓扑与预算，实际 HTTP 与遥测共用同一快照，排队后全局配置修改只影响新 run。reuse 也执行产品同步与诊断核验；resume 使用原运行依赖事实并记录 `snapshot_source=resumed_run`，兼容旧记录时明确 `legacy_resume_current_config`。自动重启限部署登记的本机API。

build 不解析题库或 Judge 配置，题集为空、题数0、结果空列表；评分和运行评分有效性未适用，`run_valid=null`（保存、读取、列表、前端一致）。`scope_build={status,embedding,source_run_id}` 为新增来源事实，只有身份种子/导入/处理全部完成才 ready；已知嵌入配置变更或构建未完成的 scope 不得 reuse。历史无该字段的 scope 保留兼容语义，不补造构建证据。

`model_identity={status,raw_id,display_name,candidates,reason,observed_at}` 保存实际端点身份；只去除明确权重文件扩展名，不截断 Qwen3.5 等型号。多候选且无明确 ID 时保留未知，不选第一项。

`execution_budgets`、`product_settings` 和部署诊断中的有效预算/provider 都可追溯。POST 发送后的结果不确定会记录 `fatal_error_details`/相位 `request_failure`，不自动重放；旧服务无进度协议时，同步导入未返回期间只展示目录、目录计数和等待时长，媒体完成数与失败数未知时保持 `?`，不得用 `0` 伪造进度。

导入阶段墙钟以 `photo_import.total_seconds` 为准，兼容历史 `upload_seconds` 或完整起止时间；`timings.total` 只作为“后端计时”单列，不能替代请求墙钟。真实 Mac 记录为墙钟1208秒、内部计时738.1秒，二者范围不同。前端阶段耗时缺少完整起止事实时，`null`、布尔、空字符串和负数保持不可用；真实数字0保留原有起止时间回退语义。本次只改前端投影，不改原始记录、后端公式、执行或公开schema。

本轮共用执行语义变化，固定版本后四机 full10QA，并在 NX/Mac 各做独立 build→reuse10QA。build 的严格遥测验收只覆盖导入/处理；reuse 只覆盖 QA，未适用阶段不补点。单次及首尾3秒门槛、null与TTL、三类验收结论、历史保全均沿用。

输入初始化失败：`failed_phase="initialization"`，`qa_count=null`、`input_integrity=null`表示无法获得输入事实；保存真实failed记录且不调用产品。build正常初始化的题目数仍为0，验收不得要求10题；评分不适用不豁免遥测覆盖。

## 长期运行读取与提交（2026-10-23）

新增可选 `RunRevisions` / `RunReadViews` 端口，旧 `RunStore` 和写端口保留。
`GET /runs/{id}` 增加 `include_history`（默认 true，兼容旧客户端）；false 只返回最近成功提交的检查点摘要、实时窗口，不读原始遥测全文件。响应可带 `read_revision` 和 `history_mode=full|checkpoint`，表示本次读取版本和统计来源；checkpoint 不能冒称全历史重算。前端普通轮询用 checkpoint，完整曲线可手动读取；分析展开才加载。

新快照先写 `.snapshots/<generation>/{run.json,results.jsonl,metadata.json}`，文件和目录 fsync 后原子发布 `snapshot-commit.json`。该指针选择的一对文件为权威；两个根文件保留旧格式兼容导出，不能把两次 rename 当事务。提交前失败保留旧提交；提交后导出/清理失败记录日志，须从指针选中的版本修复，不回滚已成功提交的数据。根目录兼容导出只适合离线工具，运行中一致读取必须遵循提交指针。保留当前与上一提交；旧历史原文不迁移，旧目录仅新增可重建的文件版本索引。进程内按 run 的有界条带锁协调读写、删除与清理；仍要求单实例目录租约。

持久化快照持状态锁复制，IO 在锁外。等待提交/停止有共同截止，失败不记成成功写入；即使磁盘失败也发退出信号。未退出明确 `persistence.status=stop_pending` 并阻止新轮和删除；写盘异常为 failed，日志保留原因。正常结束释放执行体与线程注册。终态缓存按修订失效，同时限制条数/字节；删除后不得继续返回缓存。

写盘失败的诊断可能无法写回同一块已满磁盘。此时进程内瘦索引保留失败原因，详情以 `persistence.diagnostic_source=current_process` 和 `checkpoint_status` 明示运行状态比最近提交更新；`read_revision` 仍指最后成功的检查点，原文件不伪改成已保存。进程退出后的状态由原有备份恢复规则接管。首次历史索引尚在构建时前端展示 scan 状态并继续轮询，不把部分列表当全部结果。

## 导入观测投影（2026-10-23）

`phases.photo_import.progress`可选operation_id、availability、observation、last_received_at、last_checked_at、query_error、request_status/request_error、observer_stop_pending；observation为产品通用原事实（operation_id/scope_id/status/stage/total/completed/imported/deduplicated/failed/current_file/sequence/updated_at/observed_at/error），不从manifest补数。每目录最终progress保存在directories条目。availability不可用时保留旧观测和原接收时间，前端明确为上次事实，不冒充实时。total=null不转0，媒体100%不代表批末或整个阶段完成。同步POST返回成功与独立观测状态单列；POST结果不确定时不盲重发，也不宣称产品已停止。查询故障不自动使已返回的导入失败；观察记录不能保存或观察线程停不下属于测评服务故障。

前端进行中阶段按started_at显示持续墙钟，终态仍用保存的真实墙钟。产品步骤updated_at不等于采样心跳，query observed_at/本地last_received_at用于区分查询断连与长步骤；遥测值龄门槛不套用到已结算计数。产品进度内存有界、重启不可用；PB已保存观测不删除。

终态导入新增可选`scanned_count`，只在产品响应提供完整合法的新增/去重/失败计数时计算；它表示实际已结算的扫描媒体总数，`total_media`仍保留manifest预期数量。前端新记录显示完成/实际扫描、新增、去重及失败，旧记录缺事实时不重算分母。进程内保存失败投影同时携带真实结束时间/错误，终态页面不持续累计旧running检查点的墙钟。


### T006 Judge实时进度（2026-10-23）

QA阶段的规范进度存于`phases.qa_eval.progress`：`agent_completed`为已终态题数（含明确任务失败，不含scheduled/running），`completed`保留成功完成题数；`judge_completed`为已结束的Judge题数（含评分失败，不含跳过），`judge_total`为应评题数，`judge_skipped`单列。有效分数仍由统一评分规则统计为`summary.judge_valid_count`，合法0分有效，失败不补0。新执行在持锁检查点同步items和进度；阶段外层agent/judge计数保留兼容并与内层相同。未配置Judge时内层Judge计数为null、并发为0，前端显示未配置。

Agent和Judge仍使用独立线程池，协调器等待任一池完成即提交结果，不再等全部Agent结束才回收Judge。恢复运行从已有明确Judge终态初始化计数，跳过事实单列，不重新请求既有评分。Agent阶段墙钟在最后Agent结果返回时结束，不包含等待Judge尾部。前端阶段卡和列表共用同一读取函数，优先内层、缺失时兼容外层；没有记录显示?，不以0替代未知。历史原文不改写，旧运行自身未更新的计数仍属于旧检查点事实，不能据有效评分数补造完整Judge终态。

复现测试覆盖一题Agent阻塞时另一题Judge的合法0分、返回失败和抛异常及时可见；resume既有评分、失败/跳过、取消未开始题不假报完成、可空投影及历史外层字段UI。仅本地验证，未触碰四机或启动真实测评；后续发布按公共执行路径影响验证。


### 2026-10-26 有界派发、评分补齐与请求计数

- QA 与 Judge 使用独立有界池；`qa_concurrency` 与 `PHOTOBENCH_JUDGE_CONCURRENCY` 保持原含义。每次派发前同步保存检查点，成功落盘才允许外部调用。取消后不再派发新 QA 或 Judge；在途调用仍按原超时结束。
- 新 item 的 `judge_dispatch_status` 为 `not_started` / `in_flight` / `finished` / `outcome_unknown`。恢复保留已完成 QA、已完成评分（包括合法 0 分）及评分失败；仅对 QA 已完成且 `judge_status=pending, judge_dispatch_status=not_started` 的题补评分。`running` / `started` QA 或 Judge 被恢复为 `outcome_unknown`，不得自动重发。旧版 pending 无派发日志，同样视为未知；显式全量 rejudge 的目标、归档及覆盖语义不变。
- `phases.qa_eval.progress` 增加可选 `agent_waiting` / `agent_running` / `judge_waiting` / `judge_running` / `outcome_unknown`。running 表示已交执行池、结果尚未回收；waiting 为尚未派发。未知子评分保持空值，不作0分；已确认的独立子评分保留，item标未知并使相位 partial。
- `phases.qa_eval.judge_requests` 版本 1：`requests_started` 为该次运行真正交给 HTTP 端口的请求尝试数，包含重试、各回合子评分及一致性复问；`responses_received` 为收到并返回 JSON 的调用数；`request_errors` 为 HTTP 调用异常数；`http_429` 为异常链上明确收到的 429 数。`in_flight` / `rate_waiting` 为当前数量，`rate_wait_ms` 是各请求累计限流等待墙钟毫秒。不是云账号配额、不是一题一次请求，也不追溯补造历史值。恢复新 run 仅统计本次补齐请求，旧 run 证据保留。
- 新状态与指标均为可选字段，API 投影和前端类型同步；缺失表示旧记录未采集，不当作 0。Judge 明确收到 429/5xx 的既有有界重试保留；`RequestOutcomeUnknownError`（已发出但响应未知，包括读/写超时）不重试，三个子评分及 item 均保留 outcome_unknown 与失败事实，并停止该题尚未派发的后续子评分。恢复也不会重放旧 run 的未知请求。


结果读取：`read_revision` 对新格式为 `snapshot:<generation>`，同一次不可变提交保持稳定；没有提交清单的legacy可变文件不具可靠revision，返回空值且不做跨请求终态/投影或stat-only磁盘索引缓存。读性能取舍见 `2026-10-26-concurrency-recovery-deviations.md`；新格式无需hash或重解析大payload，公开字段及分数语义不变。
