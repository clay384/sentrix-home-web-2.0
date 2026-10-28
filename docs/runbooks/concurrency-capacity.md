# Sentrix / PhotoBench concurrency capacity

> 当前交付状态与实测配置见[2026-10-29小样本性能报告](2026-10-29-small-sample-performance.md)。下列完整容量认证流程未在本轮全部执行。

This procedure keeps the production instance untouched. Model capacity, client
CPU capacity, and cloud Judge capacity are separate measurements.

## Reproducible inputs

Pin the Sentrix commit, the complete model weight SHA256, quantization config,
tokenizer and chat template, engine version, actual server context window,
KV budget, scheduler limit, image limits, and generation parameters. A server
advertising a context length is not a successful long-context test.

Use one immutable dataset version. Capacity runs are diagnostic repetitions,
not official model scores. The final evaluation uses the complete fixed QA set.
Judge starts with an independent pool of 4; actual HTTP attempts, including
retries and multiple judgement dimensions, must be reported separately from QA
count.

## Independent instance

Use an independent checkout, data root, configuration root, results, logs and PID
records. Set `SENTRIX_DATA_DIR`, `PHOTOBENCH_CONFIG_ROOT`,
`PHOTOBENCH_DATA_ROOT`, `PHOTOBENCH_RESULTS_ROOT` and the process control paths.
The runtime connection JSON is authoritative: changing only a fallback
environment variable is insufficient. Verify `/api/v1/config` and Sentrix
`/api/v1/diagnostics` before issuing writes.

Use one API process per data root. Embedded Qdrant has an exclusive directory
lock, so adding Uvicorn workers is not a concurrency optimization. Each simulated
client must have a separate SQLite/Qdrant copy and distinct ports. Record the
actual client hardware; a Linux server run does not validate M4 Mac capacity.

Use the existing `mac` deployment profile for legacy BGE-M3 + Chinese-CLIP
and CPU face inference. This experiment explicitly overrides its old 600 s / 180 s
Agent/chat limits: Agent wall time 3600 s, chat and visual HTTP timeout 1800 s,
30 model steps, with tool/search/page/inspection caps explicitly disabled by
the four nullable `SENTRIX_AGENT_MAX_*` settings (`null` as the environment value).
The 30 steps include main-loop, internal review, recovery and forced-final calls;
no hidden 28-call tool ceiling applies to this native experiment. Record these overrides; the 200 client is
not evidence that an M4 has passed the same load.

Set `SENTRIX_VLM_CONTEXT_TOKENS=262144`,
`SENTRIX_VLM_NATIVE_OUTPUT_BUDGET=true`, and
`SENTRIX_VLM_GENERATION_PROFILE=qwen35_nonthinking`. The profile pins nonthinking,
temperature 0.7, top-p 0.8, top-k 20, min-p 0, presence penalty 1.5 and seed 42.
Native mode omits output token caps so the server's actual tokenizer determines
the input-plus-output budget. The 30-step budget is the Agent turn's execution
loop (including its internal review), not a cap of 30 HTTP requests: planning
and visual/tool subcalls are measured separately. The engine's `max_num_batched_tokens=2048` is a
prefill scheduling chunk, **not** a 2048-token response or context limit.
Likewise the synthetic native-window test's deliberate 512-token output is only
its controlled measurement workload, not the real QA or build policy.

Verify actual embedding model IDs/dimensions and face weights before building.
Keep imported observation settings and library thread limits fixed throughout
a comparison. Every timeout, round limit or provider rejection remains an explicit
operational issue to repair in a new experiment; it is not a wrong-answer score.

## Build and freeze memory once

Run PhotoBench with `mode=build` against the new instance and confirm all
assets, observations and vectors are complete. A historical mixed database is
not an accepted baseline. Stop the owning Sentrix API gracefully before creating
the snapshot; preserve its PID record for the offline check.

Prepare a JSON identity with these required fields:

- `dataset_sha256`: full corpus identity, including referenced media.
- `embedding`: versioned semantic vector-space identity; never connection URLs.
- `source_revision`: provenance of the build, not the candidate QA branch.
- `observation_model_sha256`: exact weights used to produce observations.
- `pipeline_config`: build policy plus hashes/version of memory-producing code.
- `scope_id`: the completed build scope.

Reuse requires the same memory-producing semantics, dataset and embedding space.
A QA-only branch keeps the baseline producer provenance. The QA consumer can use
a different VLM node/engine: for example, memory produced on 123 can be queried
through 100 or 118 without rewriting the producer identity. New memory produced
with changed observation/indexing/embedding semantics needs its own identity and
build; querying existing memory does not retroactively change its producer. Do not
silently change the expected identity just to bypass an incompatibility.

```sh
python scripts/benchmarks/memory_snapshot.py create \
  --source /private/build-data --destination /private/snapshots/baseline \
  --identity /private/memory-identity.json \
  --producer-migrations /private/build-source/apps/api/src/sentrix/infrastructure/persistence/migrations \
  --service-pid-file /private/runtime/sentrix-process.json

python scripts/benchmarks/memory_snapshot.py restore \
  --source /private/snapshots/baseline --destination /private/runs/c4/data \
  --identity /private/memory-identity.json \
  --consumer-migrations /private/qa-checkout/apps/api/src/sentrix/infrastructure/persistence/migrations
```

Creation holds the Qdrant lock, rejects live owners and symlinks, checks SQLite
integrity and migrations, and verifies source stability across the copy.
Snapshot schema 3 records the original absolute `source_root` as private provenance
and the complete producer migration SQL file set (filename, version, raw-byte SHA256
and canonical contract SHA256). Creation requires its versions to match the DB
ledger. The current SQLite ledger has no SQL checksums: its `applied_at` timestamps
remain integrity evidence, never a compatibility key.
Every `assets.path` must be a canonical path inside that root's `media` subtree
and name a listed, existing media file. Root escapes, traversal, missing media
and symlinks fail verification. Schema 1/2 snapshots support the explicit `inspect`
action for historical integrity only; `verify`, `restore` and `verify-restored`
reject missing SQL contracts. Recreate from a stopped producer and its exact source
checkout; never alter the old manifest or guess its original root/SQL contract.

Before restore, the consumer checkout must contain the exact same SQL filenames,
versions and bytes. Added, deleted, renamed or modified migration files fail closed
before destination publication. Documentation and non-migration source changes do
not affect SQL compatibility. This does not claim that arbitrary semantic changes
in application code are compatible with the frozen memory producer.

Restore checks every source and copied file, then updates only `assets.path` in
the independent SQLite copy to the destination root. It consolidates the copied
WAL, rechecks SQLite integrity and migration rows, and requires all media/Qdrant
bytes to remain identical. Database validation uses private DB/WAL copies because
even SQLite `mode=ro` can modify a shared-memory WAL index. The original snapshot
and manifest remain untouched.

A separate `data.restore-receipt.json` is published beside the new `data` directory
(or choose `--receipt /private/evidence/restore.json`). It binds the original
manifest byte SHA256 and identity to the old/new roots, relocated asset count,
resulting database SHA256/SQLite file inventory, migration contract SHA256 and
consumer migration directory provenance. It cannot overwrite a file
or live inside the source, snapshot or restored data. Roots and media paths are
private evidence; publish sanitized summaries only.

Before starting the API, perform the full offline verification:

```sh
python scripts/benchmarks/memory_snapshot.py verify-restored \
  --source /private/snapshots/baseline --destination /private/runs/c4/data \
  --identity /private/memory-identity.json \
  --consumer-migrations /private/qa-checkout/apps/api/src/sentrix/infrastructure/persistence/migrations
```

Use the same `--receipt` if a custom path was chosen. Once the API starts, its DB
may legitimately change: the capacity CLI validates receipt binding and active
process data-root ownership, without hashing an active SQLite database. The CLI
also requires `--consumer-source-root /private/qa-checkout` and the actual Sentrix
PID record. On the Linux test client it binds PID/start time, command, cwd and
explicit `PYTHONPATH` to that checkout, then checks its migration directory before
any HTTP request. An arbitrary producer directory cannot substitute for the running
consumer. Start Sentrix with that checkout's `apps/api/src` first in `PYTHONPATH`,
including when reusing a virtualenv installed from another checkout. This capacity
certification CLI must run on the tested Linux API host; missing `/proc` is an
explicit unsupported certification error, not a successful check. The independent
`memory_snapshot.py` create/restore path remains usable on macOS with its explicit
producer/consumer migration directory. Reusing memory on a Mac does not require
this Linux capacity CLI, and each M4 still needs its own capacity validation. Keep data in the LAN artifact store. Commit only
the sanitized identity, manifest hashes, deployment instructions and evidence
summaries; never credentials, model weights, private media or full QA answers.

## Test ordering and stop rules

1. Verify text, image, native tool calls and streaming on each endpoint.
2. Test 32K, 64K, 128K, 192K and native 262144 total tokens. Count template and
   image tokens using the endpoint tokenizer; reserve and generate 512 output
   tokens. Preserve failed attempts and use separately labeled fallback windows.
3. Test long-window concurrency independently from short real QA. Record any
   different KV or scheduler setting as a different experiment.
4. Run the same representative QA cohort at 1, 2, 4, 8, ... concurrent Agents.
   Every stage needs at least 32 questions and twice its concurrency. Keep
   the context, generation, memory and Judge rules fixed. Refine a failure
   bracket at integer concurrency values.
5. Repeat stable candidate stages twice and sustain at least 30 minutes. Test
   2 and 4 independent clients sharing each model; finally run the fixed full
   QA set at the recommended configuration.

Stop new dispatch on OOM, process exit, GPU error, database lock failures or new
resource timeouts. On 118 also stop escalation below 8 GiB available host memory
or sustained additional swap-out. Persist evidence before rollback. A request
with unknown outcome is not automatically replayed.

Maximum stable concurrency requires no resource faults, no sustained growing
queue and no repeated KV preemption within the fixed budget. Recommended
concurrency maximizes completed QA per second; choose the lower value when
throughput differs by at most 5%. Without a measured failing boundary, report
“verified at least N”, never “maximum N”.

## Recovery and evidence

The QA and Judge pools are independent. Resume only submits never-dispatched QA
and missing Judge work explicitly marked `not_started`. Completed work is
preserved. Interrupted or legacy unjournaled work remains `outcome_unknown`;
explicit rejudge retains its existing all-selected-items semantics.

Save raw per-stage model metrics, queue sizes, KV occupancy/preemptions, first
token and completion timings, CPU/RAM samples, Judge backlog and HTTP 429s.
Model-server readings must carry the remote host identity; client resources must
not be labeled model memory. Distinguish end-to-end phase wall time from summed
worker duration, since QA and Judge overlap.

The run overview API omits `items` in both history modes. The capacity CLI fetches
item detail endpoints with at most four simultaneous reads, validates indexes and
QA identities against the fixed source, and checks the final revision/count before
saving `final-run.json`. During polling it reads new or still-in-flight item details
so structured model/turn resource errors stop further dispatch promptly. It never
scans QA prose or answers for error keywords. Non-resource model errors remain
separate counts from resource failures and answer-quality scores; missing traces
produce unknown counts, not zero errors. Final evidence repeats the resource check.
The selected Sentrix PID sample covers that API process only; embedding sidecars
need their own PID evidence to describe full client CPU consumption.

## Portable legacy embedding identity

The accepted snapshot's `embedding` is `{ "identity_version": 1, "scheme": "legacy",
"text": <BGE health.embedding_identity>, "visual": <CLIP health.embedding_identity> }`.
Components include actual loaded weight hashes, tokenizer/config artifacts, dimensions,
pooling/normalization/truncation policy and a digest of inference/preprocessing code.
Health caches these at startup after requiring matching pre-load and post-load hashes;
normal health requests never scan weights. CLIP records its actual model-name-mapped
checkpoint, not merely the basename in `CHINESE_CLIP_CHECKPOINT`. Its preprocessing
is 224x224 bicubic resize, RGB and the Chinese-CLIP normalization; it does not center crop.

Put deployment-specific `text_embedder_url` and `legacy_clip_url` in expected-runtime
alongside `embedding_scheme=legacy`. The CLI requires those addresses to agree with
both Sentrix diagnostics and PhotoBench's `split_text_image` connection, then GETs
the actual `/health` endpoints and requires exact semantic identity matches before QA.
Different directories, hosts or ports can reuse the same vector space. Missing identity,
changed weights/tokenizers/configuration/pooling/dimension are rejected, even if a
sidecar reports the same model name. Device, PID/port, threads, parameter dtypes and
library versions are execution evidence, not URLs or paths inside the compatibility key;
cross-device vectors are not claimed to be bitwise identical.

Existing schema-1/schema-2 snapshots remain available through read-only `inspect`. A legacy
connection-only identity cannot pass this CLI's semantic or SQL compatibility gates. Do not edit its manifest or silently rename
its identity. Create a new accepted snapshot with verified semantic provenance. For a
build made before additive health identity existed, retain its original implementation
SHA and complete model artifact hashes, compare unchanged inference ASTs/configuration,
and validate equivalent sidecar identity before approving a new snapshot. Restart sidecars
only at an explicit deployment boundary; adding identity does not justify interrupting an
active build.

## 运行限制与质量评分分开

PhotoBench 只有结构化证据才标记 `operational_issue`：输出、上下文、轮次、时间预算，供应商拒绝/不可用，以及资源错误。来源为模型调用的 typed error、HTTP状态、`finish_reason=length`，确切的终止原因，或工具返回的明确 `observation.operational_issue`。回答文本、普通模型工具参数错误和合法拒答不能触发这一分类。

此类题目 `evaluation_status=not_evaluable`、`score=null`，不发云质量评分；有未知在途结果时继续保留 `outcome_unknown`，否则记录 `operational_failure`。本题后续对话轮次停止派发。恢复不重发已执行、预算失败或未知请求；轮询超时仅表示本地停止等待，远端回合是否完成仍未知，记录原turn ID。原始运行轨迹不修改，已有错误零分仅作为 `excluded_observation` 审计保留。质量、检索和架构归因分母排除此类样本，运行耗时/模型和工具调用计数不排除。

云Judge自己的截断单列 `judge_issue`，Agent执行仍可完成；即便截断响应内的JSON合法也不能当有效评分。`operational.judge_incomplete_count/judge_by_kind` 与Agent的 `not_evaluable_count/by_kind` 分开。历史记录有原始 `finish_reason=length` 时同样按未完成投影，不触发请求。显式“全部重评分”仍可重评正常答案和失败Judge；它不会重新运行Agent，也不会把预算失败答案重新纳入质量成绩。

容量CLI遇到运行限制或云Judge预算问题停止新派发，标为 `operational_issue` 的无效实验；真正资源故障仍记 `resource_failure`。这些失败都不会自动证明最大安全并发边界。先修复预算或供应商配置，再用新的独立运行ID实验；不要通过改旧结果或恢复未知请求绕过失败证据。

建库验收额外要求：`scope_build.status=ready` 且所有构建相位均为done。流水线非2xx或资产failures、memory_graph缺失、非空causal_issues或causal_status=degraded、family_graph.run.status=failed或run.stats.operational_issue，均应拒绝验收。PB会保存原memory_graph/family_graph，返回partial/failed和incomplete。离线快照工具的完整性校验本身不证明业务建库成功；只有部署验收方核对原始run证据后才能写入accepted身份。

CN-CLIP身份清单按实际包的模型导入覆盖 `modeling_bert.py` 和 `configuration_bert.py`；不存在的旧假定文件名不能用空hash替代。缺身份时推理服务可继续报告运行状态，但容量/基线认证必须拒绝，直至在部署边界重新加载并验证完整身份。

### 嵌入检索故障与运行预算

容量实验的 expected-runtime 必须包含 text_embedder_timeout_s 和 legacy_clip_timeout_s，与 Sentrix /diagnostics.runtime_configuration 相等。本轮显式环境建议为 SENTRIX_TEXT_EMBEDDER_TIMEOUT_S=120、SENTRIX_LEGACY_CLIP_TIMEOUT_S=120；产品默认仍是 10 / 30 秒。修改预算形成新的实验条件，不能把旧超时样本改判为质量错误。

BGE / CLIP 请求超时、HTTP 拒绝、不可用、空或非法向量现在有线程隔离的结构化证据，沿内部 retrieval_channels[].operational_issue 保留。PhotoBench 对这种样本记不可评估 / 空分、跳过 Judge；容量入口停止新派发。本轮失败并不自动成为已确认容量边界。正常检索无命中、合法 SQLite 索引回退和历史缺少 typed 证据的降级不猜测故障原因；后者需单独检查原始通道状态。

### Judge 终态与容量资格

judge_completed 计数包含 completed 和 failed，不能当作成功评分数。容量入口遇到明确 judge_status=failed（含较早对话轮次的子 Judge）即停止派发并将本轮标记 judge_failed；原因没有结构化记录时明确为 unknown，云请求总数及429仅保留运行级事实，不能猜成某一道题的失败原因。终态完整采集与仅观察恢复也执行此判定，不重发 QA 或 Judge。有效0分、not_labeled / not_applicable 空分不会仅因分数为空被判失败；已恢复的429也不会单独否定一轮。此类失败不自动证明本地模型容量边界。

## 本次隔离部署的服务回滚

100、118、123模型服务及200独立应用的操作目录、身份核验、停止确认和旧服务恢复顺序见[隔离部署回滚](2026-10-27-capacity-rollback.md)。成功交付保留新vLLM，100的WeMM保持停止；数据、结果和未知请求证据不随服务回滚删除。153主服务不在本轮写入范围。


### 可选 Judge HTTP 尝试证据

仅在新启动的 PhotoBench 环境设置 `PHOTOBENCH_JUDGE_HTTP_ATTEMPT_EVIDENCE=1` 开启。默认关闭；`qa_eval.judge_requests.http_attempt_evidence_enabled` 记录开关状态。开启后同一运行的 `http_attempts` 保留成功和失败尝试，并沿现有进度快照提供运行中的证据。使用不支持此可选能力的 HTTP 客户端时，`http_attempt_evidence_supported=false`，请求仍按原方式执行。

证据只包含尝试索引、请求分组、开始/结束时间、HTTP 状态、异常类、实际选择的退避及取消状态，以及校验后的 `error.code/type`、数字或 HTTP 日期形式的 `Retry-After` 和数值限流头。错误标识必须是短标识符，且不能回显请求正文或认证头中的值；不保存自由错误消息、URL、请求头、提示词或响应正文。供应商 request ID 不额外记录。已有请求体、评分、重试次数、限速和成功 `raw_response` 不变；HTTP 日期形式的 `Retry-After` 仅记录，现有重试逻辑仍只解析数字形式。连接建立重试会形成独立 transport attempt，不能将缺少 HTTP 状态的尝试冒称为供应商已接收的请求。

容量报告必须注明观测开关与同机辅助测试窗口，避免将不同观测配置或辅助负载混为同一容量结论。


### 同机多 PhotoBench 的共享 Judge gate（默认关闭）

独立 PhotoBench 的本地 `PHOTOBENCH_JUDGE_REQUEST_INTERVAL_SECONDS=2` 不构成全局限速。仅同一 Linux 主机、同一用户的进程可选择共用私有目录和明确的账号预算 scope：

```sh
# 目录应由操作者预建，权限 0700；各进程使用同一 DIR/SCOPE。
PHOTOBENCH_JUDGE_SHARED_GATE_ENABLED=1
PHOTOBENCH_JUDGE_SHARED_GATE_DIR=/absolute/private/judge-gates
PHOTOBENCH_JUDGE_SHARED_GATE_SCOPE=company_coding_plan
PHOTOBENCH_JUDGE_SHARED_GATE_INTERVAL_SECONDS=2
PHOTOBENCH_JUDGE_SHARED_GATE_MAX_IN_FLIGHT=2
PHOTOBENCH_JUDGE_SHARED_GATE_WAIT_TIMEOUT_SECONDS=300
PHOTOBENCH_JUDGE_CONCURRENCY=4
PHOTOBENCH_JUDGE_REQUEST_INTERVAL_SECONDS=2
PHOTOBENCH_JUDGE_HTTP_ATTEMPT_EVIDENCE=1
```

同 scope 强制相同 endpoint/key 及节拍/在途配置；私有 HMAC 指纹仅在 0600 identity 文件，安全指标和 journal 不记录凭据、指纹、提示词或响应正文。所有同时消费同一预算的进程必须共用 scope；换 scope 不能证明获得独立额度。未显式启用时不改变原请求行为；部分配置、权限/符号链接、身份、状态错误一律拒绝发送，不降级为不限速。

每个实际 transport 尝试，包括安全连接重试，先取得全局发送许可；退避前释放已知完整 HTTP 响应（含 429）和明确连接建立失败的槽。HTTP 在途数、已发请求与 gate 等待/失败分开计量。完整响应的 JSON/模型格式错误不伪称云请求仍在途；评分和未知结果仍沿原规则记录，不能自动重放。启用 gate 后拒绝自动重定向，防止一个许可发出多个隐蔽 HTTP 请求；收到重定向会释放已知槽并将组标为配置错误，需改为明确直连端点。

Read/Write/Protocol 等未取得完整 HTTP 终态、PID/start/boot 丢失或复用会保留 unknown lease 并封组。不能把进程退出或文件锁释放当作云端空闲。封组没有自动清理、自动重置或重放入口；保存私有 state/journal 后由操作者处理。已用 gate 丢失 identity/state 也拒绝重新初始化。等待可取消且默认最多 300 秒；取得许可前取消不算云请求。

此实现不是跨主机文件锁。未来 Sentrix 各自在 M4 时，可将独立 PhotoBench worker 集中在 200，共享 gate 并各自访问所属 Sentrix；不声称各台 Mac 本地 PhotoBench 已实现跨机共享限速。

发送节拍从所有关键 lease/state/journal 持久化完成后的 `permit_ready_monotonic` 起算，不从较早的 `granted_monotonic` 起算。同一 boot、同一 flock 保护的 lock 文件槽保存此节拍时钟；它只用于同机调度，不替代已 fsync 的在途证据。v1 scope 仅在没有在途 lease 且未封组时升级为 v2；升级先完成持久化再等待一个配置间隔，旧客户端拒绝 v2，不能混跑。节拍时钟缺失/损坏、boot 或 lock 身份变化均拒绝发送。

`acquired` journal 表示持久 lease 的准备过程；HTTP 尝试证据的 `gate_permit_ready_*` 表示持久化后的许可就绪，`started_*` 表示初始尝试通知时间，`gate_transport_dispatch_*` 表示初始 observer/快照 IO 返回后紧贴 HTTP 客户端调用的派发边界（终态通知保存）。`gate_durable_preparation_ms` 与 `gate_ready_to_dispatch_ms` 分别记录前段持久化和后段调度延迟。配置保证许可就绪间隔，不承诺网络线上硬实时发送间隔；就绪后的进程调度或 HTTP 库延迟仍可能压缩相邻实际 HTTP 开始间隔，真实实验必须同时报告实际开始间隔及这段误差，不能仅凭许可间隔宣称 wire 间隔通过。
