# Sentrix S2-Q1 · ATM-Bench

S2 从冻结的 Sentrix S1 入库数据导出指定测评范围的来源和向量。它把 SQLite FTS5 BM25 与文本、视觉及事件向量的召回结果按父来源去重，通过 RRF 融合后交给 Qwen3-VL-Reranker-2B。视频父项用于归并已登记的子帧。无可用文本的父项保留召回记录，排在重排候选之后。

Q1 最多使用 32 次模型决策。首轮优先保留原问题，后续记录缺失证据、调整检索条件，按需读取邮件原文、原图、视频帧和时间地点元数据。答题与检索分别写入指定的轨迹目录。S2 在回环地址上独立运行，导出器在一个 SQLite 只读事务中读取冻结快照。Sentrix 与 PhotoBench 的应用代码、路由和数据库迁移保持原样。

## 离线验证

Python 3.11+，SQLite 需支持 FTS5。以下命令从仓库根目录执行。测试使用当前主干的 Sentrix 迁移、合成邮件及视频帧，并通过本地 HTTP 接上模拟推理服务。

```sh
python -m pip install -r benchmarks/atmbench_s2/requirements.txt 'pytest>=8.3,<9'
export PYTHONPATH="$PWD/benchmarks/atmbench_s2:$PWD/apps/api/src"
export NO_PROXY=127.0.0.1,localhost
export no_proxy="$NO_PROXY"
python -m pytest -o addopts='' -q benchmarks/atmbench_s2
```

## 复跑

### 1. 准备冻结输入

优先使用已冻结的 S2 索引和配套来源清单。需要重新建索引时，可使用已导出的 `manifest.json`、`sources.jsonl` 与 `native-vectors.jsonl`，或用原测评资料做只读导出。原测评根目录所需输入如下。

| 路径 | 内容 |
|---|---|
| `runtime/sentrix-s1/data/sentrix.db` | 冻结的 S1 SQLite 测评库 |
| `results/ingest/<scope>/` | 各批次 `receipts/*.json` 与 `native-source-map.json` |
| `assets/protocol/ingest/sources-with-video-frames.jsonl` | 来源清单，包括原始来源、父级映射和视频帧时间 |

当前 Sentrix 主干的表结构可以读取该导出契约。构建历史质量可比的输入，还需要当时的 S1 Qwen3-VL 原生向量、冻结的 C1 描述及测评入库回执。全量导出会检查 11,034 个父来源与 13,375 条来源记录，还会核对原生事件和已完成的家庭分析记录。以下环境变量指向授权的测评目录和本机新的私有输出目录。

```sh
python -m sentrix_s2.export_native \
  --root "$ATM_INPUT" --scope atm-main-s1-v1 \
  --extra-sources "$ATM_INPUT/assets/protocol/ingest/sources-with-video-frames.jsonl" \
  --expected-parent-count 11034 --finalized --output "$ATM_OUT/snapshot"
```

### 2. 构建并启动 S2

模型服务必须提供 Qwen3-VL-Embedding-2B 的 `POST /v1/embeddings` 和 Qwen3-VL-Reranker-2B 的 `POST /rerank`。向量须为归一化的 2048 维，重排返回完整候选排列。沿用冻结索引时，模型服务通过 `X-ATM-Model-Fingerprint` 报告的版本须匹配建库记录。

```sh
python -m sentrix_s2.index --snapshot "$ATM_OUT/snapshot" \
  --output "$ATM_OUT/index.sqlite3" --models http://127.0.0.1:19601
python -m sentrix_s2.server --index "$ATM_OUT/index.sqlite3" \
  --models http://127.0.0.1:19601 --port 19720 --evidence "$ATM_OUT/retrieval"
```

用 `GET /health` 核对索引修订号和来源数。相同快照的中断建库可用 `--resume` 接续。

### 3. 运行一题

Q1 需要兼容 Qwen3.7-Flash 的聊天网关，支持工具调用、图片、thinking 和 131,072 输出 token 配置。运行配置示例使用本地测评文件。

```json
{
  "framework": "sentrix_s2",
  "system": "S2-Q1",
  "run_id": "atmbench-20261116",
  "sources_jsonl": "inputs/sources-with-video-frames.jsonl",
  "caption_cache": "inputs/captions-C1.jsonl",
  "memory_base_url": "http://127.0.0.1:19720",
  "gateway_url": "http://127.0.0.1:19600"
}
```

媒体换路径时可配置 `path_prefixes`，其值为旧、新路径前缀对的列表。视频容器元数据读取需要 `ffprobe`。若沿用历史测评网关，可配置 `gateway_ledger` 指向自己的 `gateway/requests.sqlite3`。超时恢复会读取该任务已发出的请求回执，并保留原请求 ID，避免重复派发。该恢复模式要求历史的网关地址、运行 ID 和模型配置。

题目 JSON 只包含 `id` 和 `question`。每次运行使用独立的尝试 ID 和输出目录。实际推理需要显式传入 `--allow-cloud`。

```sh
python -m atm_agent_q1.run --config config.json --question-json question.json \
  --attempt-id s2-replay-001 --trace-dir "$ATM_OUT/s2-replay-001" --allow-cloud
```

## 上周版本与同题结果

2026-11-19 归档中，S1-Q0 与 S2-Q1 使用 ATM-Bench 主集相同的 1,013 道题和同轮裁判。两种官方规则轨均覆盖全部 1,013 题。ID 修正轨使用校正后的列表来源 ID。正式运行保留了早期已经接受的答卷，1,013 份 S2-Q1 答卷中有 793 份来自带网关超时恢复的最终 Q1 入口。下表反映检索与答题策略的组合效果。

| 评分轨 | S1-Q0 | S2-Q1 |
|---|---:|---:|
| 官方原始规则 | 41.69% | 67.18% |
| 官方规则，修正列表来源 ID | 44.30% | 69.31% |

历史冻结索引 SHA-256 为 `a3874b03f7eaeddef010e2f633b9fa6323003225212c3c93b30ccc8f91f3d813`。修复空文本候选后的 S2 服务修订为 `59383e4eddd31ec4c77590ce5034b0818dd7a9700308fd79bc6b2408f9f29f57`。最终 Q1 的核心、入口和网关恢复模块对应以下归档 SHA-256。

| 文件 | 归档 SHA-256 |
|---|---|
| `core.py` | `6fb8da88e7f8e5f2426f2c850ad1ca93a7c6aaa35f2982ca5eef5943331d4878` |
| `run.py` | `1877adac81367dbdb0e9dd0d73df5359fe60c6be74f21f47f399a55ad5f2db07` |
| `transport.py` | `77735c9c8d6d90af10cc03fb1ddc6c9d8e1a698d02333a09ec158b3ff39b818a` |

本仓库整理版沿用冻结检索与答题逻辑，并调整了只读导出的隔离方式、SQLite 文件路径解析以及可配置的网关账本路径。新导出产物的清单和代码哈希会对应本次打包版本。
