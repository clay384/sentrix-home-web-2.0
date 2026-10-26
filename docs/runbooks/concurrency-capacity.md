# Sentrix / PhotoBench concurrency capacity

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

For the Mac deployment budget, set the existing `mac` deployment profile:
legacy BGE-M3 + Chinese-CLIP, Agent 600 s, chat 180 s, CPU face inference.
Verify actual embedding model IDs/dimensions and face weights before building.
Keep imported observation settings and library thread limits fixed throughout
a comparison.

## Build and freeze memory once

Run PhotoBench with `mode=build` against the new instance and confirm all
assets, observations and vectors are complete. A historical mixed database is
not an accepted baseline. Stop the owning Sentrix API gracefully before creating
the snapshot; preserve its PID record for the offline check.

Prepare a JSON identity with these required fields:

- `dataset_sha256`: full corpus identity, including referenced media.
- `embedding`: scheme, model identities, dimensions and normalization version.
- `source_revision`: provenance of the build, not the candidate QA branch.
- `observation_model_sha256`: exact weights used to produce observations.
- `pipeline_config`: build policy plus hashes/version of memory-producing code.
- `scope_id`: the completed build scope.

Reuse requires the same memory-producing semantics, dataset and embedding space.
A QA-only branch can keep the baseline provenance; a changed observation,
indexing or embedding pipeline requires a new identity and new build. Do not
silently change the expected identity just to bypass an incompatibility.

```sh
python scripts/benchmarks/memory_snapshot.py create \
  --source /private/build-data --destination /private/snapshots/baseline \
  --identity /private/memory-identity.json \
  --service-pid-file /private/runtime/sentrix-process.json

python scripts/benchmarks/memory_snapshot.py restore \
  --source /private/snapshots/baseline --destination /private/runs/c4/data \
  --identity /private/memory-identity.json
```

Creation holds the Qdrant lock, rejects live owners and symlinks, checks SQLite
integrity and migrations, and verifies source stability across the copy.
Snapshot schema 2 records the original absolute `source_root` as private provenance.
Every `assets.path` must be a canonical path inside that root's `media` subtree
and name a listed, existing media file. Root escapes, traversal, missing media
and symlinks fail verification. Schema 1 can still be verified for historical
integrity, but restore rejects it: recreate from the stopped source, never guess
its original root.

Restore checks every source and copied file, then updates only `assets.path` in
the independent SQLite copy to the destination root. It consolidates the copied
WAL, rechecks SQLite integrity and migration rows, and requires all media/Qdrant
bytes to remain identical. Database validation uses private DB/WAL copies because
even SQLite `mode=ro` can modify a shared-memory WAL index. The original snapshot
and manifest remain untouched.

A separate `data.restore-receipt.json` is published beside the new `data` directory
(or choose `--receipt /private/evidence/restore.json`). It binds the original
manifest byte SHA256 and identity to the old/new roots, relocated asset count,
and resulting database SHA256/SQLite file inventory. It cannot overwrite a file
or live inside the source, snapshot or restored data. Roots and media paths are
private evidence; publish sanitized summaries only.

Before starting the API, perform the full offline verification:

```sh
python scripts/benchmarks/memory_snapshot.py verify-restored \
  --source /private/snapshots/baseline --destination /private/runs/c4/data \
  --identity /private/memory-identity.json
```

Use the same `--receipt` if a custom path was chosen. Once the API starts, its DB
may legitimately change: the capacity CLI validates receipt binding and active
process data-root ownership, without hashing an active SQLite database. Keep data in the LAN artifact store. Commit only
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
