# Remote vLLM capacity evidence

`scripts/benchmarks/remote_vllm_capacity.py` is an explicit deployment experiment,
not a production runtime provider. It never starts/stops a model and never writes
Sentrix databases. Its load unit is one OpenAI chat request, **not one complete
Sentrix QA task**. Use PhotoBench for the real QA/CPU/Judge concurrency experiment.

Create an operator-supplied identity JSON containing weight SHA256, tokenizer and
template hashes, quantization, engine version, GPU identity, active launch settings
and the actual server context window. Preserve separate identities for different
KV budgets, engine versions, windows or quantization. The probe saves this document
as declared deployment evidence; it does not rehash remote weights. `/v1/models`
must confirm the exact alias and `max_model_len` before load starts.

Provide a fixed-order JSONL workload. Each line is an OpenAI chat request object
with `messages`, explicit `max_tokens`, and fixed sampling/tool/image parameters.
The probe only sets the selected model and streaming usage reporting. Workloads
must cover the modalities and tool patterns needed by the particular experiment;
the script does not label a synthetic workload as representative QA. Content is
not copied to result logs: request/response hashes, counters and timings are saved.

Example (paths are deployment-specific):

```bash
python scripts/benchmarks/remote_vllm_capacity.py \
  --base-url http://192.168.0.100:18104/v1 --allow-model-host 192.168.0.100 \
  --model qwen35-4b-awq --window 262144 \
  --identity /absolute/runtime/identity.json --requests /absolute/runtime/workload.jsonl \
  --output /absolute/results/model100-c1-c2-c4 \
  --concurrencies 1 2 4 --samples 32 --timeout-s 180
```

Each round uses at least `max(32, 2 * concurrency)` requests, in the same cyclic
order. The dispatcher has at most N active futures; it stops new dispatch on a
request failure, missing critical metrics, model restart or counter reset. Two or
more KV preemptions accumulated since the first valid sample stop dispatch; a single
preemption remains evidence without independently failing the round. Queue growth
across three consecutive sampling intervals or swap growth across two consecutive
intervals also stops dispatch. The policy version, thresholds, baseline definition
and sampling interval are frozen into `manifest.json`; differing historical node
protocols cannot qualify as the same experiment. Each raw sample includes the
policy and observed cumulative preemptions/growth intervals. Active requests are allowed to resolve within their network timeout;
timeout/disconnect outcomes remain unknown and are never retried. The stream also
checks elapsed wall time whenever it receives a line; this is not a hard real-time
cancellation guarantee for a stalled socket. SIGINT/SIGTERM stops new dispatch.

After a failure, use a new output directory to test adjacent integer levels.
`--resume` verifies the frozen plan and skips completed request IDs. Any previously
dispatched request with unknown outcome quarantines that round: resume cannot replay
it. Existing result directories require explicit `--resume`; logs are never cleared.
A nonblocking process lease protects the whole output directory, including resume;
a second process is rejected before dispatch. Independent clients need independent
output directories, each with a fresh UUID run ID.
A run resumed partway through a round cannot qualify as uninterrupted stability evidence.

For candidate stability verification, use `--rounds 2 --duration-s 1800` and an adequate
`--max-requests` cap. At least one uninterrupted round must last 30 minutes; every
round needs the minimum sample count. Hitting the cap before the requested duration
is a failure, not a 30-minute pass. Short exploratory rounds only establish an
observed lower bound. Reports recommend the highest qualified completion rate,
choosing the lower concurrency when it is within 5% of the best. They claim a
maximum only when a failed trial at the immediately higher integer has an explicit,
evidence-backed capacity-boundary assessment. Raw failures are reported as
`failed_trial_concurrencies` / `inconclusive_failed_concurrencies`; HTTP 429, invalid
payloads, SSH/sampler failures, operator stops and competing-process swap activity
do not automatically prove capacity. `maximum_stable_concurrency` stays null and
`qualified_stable_at_least` is used without a confirmed adjacent boundary.

After reviewing attribution and raw evidence, a **separate derived report** may pass
an annotated copy of a failed round to `capacity_report` with
`capacity_boundary_confirmed=true`, a nonempty `capacity_boundary_reason`, and
`capacity_boundary_evidence` containing nonempty source references/hashes. Preserve
the raw round unchanged. A flag without reason/evidence cannot establish a maximum.
A confirmed nonadjacent failure also cannot establish the immediately lower maximum.
The reporter includes these assessments in `capacity_boundary_confirmations`;
it never generates them from an error status.

`*.metrics.jsonl` preserves raw Prometheus text plus normalized running/waiting,
KV use, preemptions, token counters and TTFT/queue/e2e aggregates. Version-specific
missing values remain null. Client CPU/RSS is labelled as the **load-generator
process**, never model GPU memory and never proof of an M4 machine's capacity.
Optional `--ssh-host user@model-host` executes a fixed read-only host probe; its
address must resolve to the model host. Those samples describe the entire remote
host, not exclusive model-process ownership. GPU UUID attribution belongs in the
identity document. For Jetson use `--min-remote-available-gib 8`; low/unavailable
memory or sustained swap growth stops dispatch. No SSH password is accepted or logged.

`--observe-only-seconds 30` collects GET/optional read-only SSH telemetry without
sending any generation POSTs (the same identity/workload arguments still freeze the
experiment metadata). HTTP redirects and proxies are disabled. The allow-host must
match explicitly, and the protected 192.168.0.153 host is rejected, including DNS
aliases resolving to it.

Artifacts: `manifest.json`, durable `requests.jsonl`, per-round JSON and metric JSONL,
and `report.json`. These are capacity evidence, not official model scores. Retain
independent PhotoBench results for frozen-memory QA, cloud-Judge requests/429s,
shared-client tests and the fixed-version full dataset acceptance.
