# 评测数据的迁移（2026-10-06）

> **用户裁决**：① 「所有旧的数据都不需要放到 2.0，唯一需要的只有测评需要的问题和正确答案
> 这类数据」；② 主产品评测标注「标注 + 照片一起搬」；③ PhotoBench results「现在就改指 2.0 空目录」。

本文记：**哪些数据搬了、为什么是这些、怎么验的、还剩什么**。

---

## 1. 先纠正一个前提：PhotoBench 的评测数据**早就搬好了**

这一条本来不需要动作，但值得写下来，因为它改变了整个问题的形状。

`apps/photobench/datasets/` 是 2026-09-21 从 1.0 复制的。本次**逐字节复核**：

| 项 | 结论 |
|---|---|
| 20 个 `qa/*.jsonl`（问题 + 正确答案 + 预期动作） | 内容完全相同 |
| 5 个 `manifest.json` | 相同 |
| `photos/` `faces/` `videos/`（1.3 GB） | 文件数与字节数全等 |

**而且 9597 读的一直是这份**：`photobench/config.py:87` 的 `data_root` 缺省值是
`APP_ROOT / "datasets"`，`sentrix-photobench.service` 从来没设过 `PHOTOBENCH_DATA_ROOT`。
（单元里那条 `PHOTOBENCH_RESULTS_ROOT` 是另一个配置项，不干扰。）

所以「评测需要的问题和正确答案」在 2.0 侧**不依赖 1.0**。

## 2. 但 1.0 里还有**另一批**同类的数据，2.0 一份都没有

`docs/baseline/` 在 2.0 根本不存在。那是 1.0 主产品的评测标注，全部是**人工标注、
不可再生**。本次搬入 2.0 的是其中**真·评测输入**的部分：

| 文件 | case 数 | 含什么 |
|---|---|---|
| `docs/baseline/benchmark_annotations.json` | 60 | `exact_asset_ids` / `acceptable_approximate_asset_ids` / `forbidden_asset_ids`，且审过（记着 2 处 `gt_conflict`） |
| `docs/baseline/development_set.json` | 45 | 按 12 个 category 分层 |
| `docs/baseline/hidden_predictions.json` | 16 | 预测；**注释写明 full GT held by the user** |
| `docs/baseline/hidden_set_manifest.json` | 16 + 44 | `hidden_predictions` 的配套清单（seed 20260806） |
| `docs/baseline/structured-qa-e2e.json` | 40 | query + 期望 SQL 值 |
| `docs/baseline/e2e_r9_cases.json` | 10 | 全链路用例 + 预期 `evidence_status` |
| `docs/baseline/rx-experience-cases.json` | — | 体验用例 |
| `docs/baseline/rx-replay-pairs.json` | — | 人工盲测包（新旧回答成对 + 评分说明） |
| `docs/baseline/sentrix-12b-full-chain-cases.json` | — | 12B 全链路用例 |
| `scripts/benchmarks/agent2_gold_set.json` | 12 | Requirement Gold Set：最小证据要求 + 预期终态 |
| `scripts/benchmarks/agent2_shadow_cases.json` | — | 分层 shadow case |
| `scripts/benchmarks/agent2_gate_config.json` | — | 配套 gate 配置 |
| `scripts/benchmarks/structured_qa_cases.json` | 40 | TFPE v2；每例断言结构化路由 + 精确 SQL 值 |

**没搬的是报告/审计产物**，不是评测输入：`rx0-field-inventory` / `rx0-leak-inventory` /
`embedding_quality_*` / `retrieval_R7_*` / `retrieval_R8_*` / `parser_slots_*` /
`latency_report` / 各种 `*.md` 报告。它们虽然也含 `query` 字样，但那是**输出**。

### 2.1 照片夹具也搬了（裁决②）

| 夹具 | 来源 | 去处 | 大小 |
|---|---|---|---|
| **主夹具** | `1.0/data/album3-v2-source/` | `data/album3-v2-source/` | 144 MB |
| **第二夹具** | `/home/asus/samples/album{1,2,3}` | `data/household-benchmark-source/` | 725 MB |

两者都是**设计文档 §9.2 点名的黄金样本**。第二夹具在 1.0 里是指向 `/home/asus/samples/`
的**软链**，这里复制成实体，让 2.0 独立。

搬完用 `rsync -ain --checksum` 复核，**输出为空**（逐字节一致）。

`data/` 在 2.0 的 `.gitignore:25`（`/data/`）里，所以夹具不进 Git——与
`apps/photobench/datasets/` 同一处理。

## 3. 顺手断掉的一个静默陷阱：8 个测试曾指向 1.0

2.0 有 8 个 e2e / 集成用例硬编码 `1.0/data/album3-v2-source/photos`。它们**每个都有
`pytest.skip` 保护**——所以 1.0 一归档，它们**不会红，会静默 skip**。而用例自己的
docstring 写着合成夹具替代不了这类覆盖：

> 「合成夹具抓不住这一类：地点锚点的值来自**真实反地理编码的结果**，只有真数据才会
> 出现「GeoNames 的最近邻点带的是汉字名」这种形状。」

**绿着丢掉覆盖，比红掉危险。** 现在改成从仓库根算起：

```python
ALBUM = Path(__file__).resolve().parents[4] / "data/album3-v2-source/photos"
```

（`apps/api/tests/{e2e,integration}/x.py` 往上第 5 层即仓库根。）原先写死
`/home/asus/Github/...`，换机器就废。

改的 8 个文件：`test_face_embedding` / `test_face_detector` / `test_identity_album` /
`test_assistant_turn` / `test_search_channels` / `test_import_and_deliver` /
`test_album_timeline` / `test_visual_search`。两处 docstring 里「1.0 仓库里」一并改掉。

**一个保留的引用**：`test_qdrant_index.py:179` 断言 2.0 的 `qdrant_dir` **不等于**
`1.0/data/qdrant`。那不是依赖，是守卫（防共用 qdrant 导致检索静默降级回 SQLite 全扫，
见 runbook §6.1）。**故意留着。**

## 4. results 改指 2.0（裁决③）

做法是**整行删掉** `Environment=PHOTOBENCH_RESULTS_ROOT=...`，而不是写死另一条路径：
`config.py:86` 的缺省值 `APP_ROOT / "results"` 就是 2.0 自己的 `apps/photobench/results`。
少一行配置少一处会漂的地方。

改完实测：

| 探活 | 结果 |
|---|---|
| `systemctl --user show sentrix-photobench -p Environment` | **空** |
| `GET /api/runs` | `total: 0` |
| `GET /api/manifests` | **仍列出 5 个 dataset**（album3 53 张照片、5 个 qa set） |

**最后一行是安全边界**：`results_root` 与 `data_root` 是两个独立配置项，清掉前者没伤到
评测数据集。空目录不崩，因为 `FileRunStore.iter_entries` 头一句是
`if not self._root.exists(): return`。

> 代价（有意的）：PhotoBench 界面现在列出 **0 个 run**。1.0 那 281 个 run（20 GB）留在原处，
> 要回头对照就把那一行加回去。

## 5. 遗留

| # | 事项 | 说明 |
|---|---|---|
| 1 | **`benchmark_annotations.json` 的 `exact_asset_ids` 指向 1.0 的库** | 值是 `asset_203b5b507c95` 这种 1.0 资产 id。2.0 空库重新导入后 id 是否重现，取决于 id 生成是否纯内容哈希——**未核**。在那之前这批标注只能当「问题 + 答案描述」用，不能按 id 直接打分 |
| 2 | **`hidden_predictions.json` 的 GT 不在仓库里** | 文件自己写着 `full GT held by the user. Grade with score_hidden.py --gt <gt.json>`。**那份 GT 在用户手里**，本次未涉及 |
| 3 | **2.0 没有任何评测器读这批标注** | 搬进来是**存档**，不是接上了管线。1.0 的读取方（`backend/tests/test_agent2_gold_set.py`、`scripts/benchmarks/evaluate_agent2_shadow.py`）没有对应物 |
| 4 | **第二夹具没有代码引用** | 设计文档 §9.2 点名它是「第二夹具」，但 2.0 全仓只有那一处提及。搬是备着 |
| 5 | **1.0 的 `scripts/benchmarks/` 评测脚本没搬** | 只搬了 4 个 json 数据。那批 `evaluate_*` / `audit_*` 脚本是代码不是数据，按「只搬评测数据」的口径留在 1.0 |

## 6. 复跑

```sh
cd /home/asus/Github/sentrix-home-web2.0

# 数据集与 1.0 是否仍一致
diff -rq ~/Github/Sentrix-Home-Web/services/photobench/data/album3-max/qa \
         apps/photobench/datasets/album3-max/qa

# 夹具是否仍逐字节一致
rsync -ain --checksum ~/Github/Sentrix-Home-Web/data/album3-v2-source/ data/album3-v2-source/

# 9597 现在读的是哪
systemctl --user show sentrix-photobench -p Environment
curl -s http://127.0.0.1:9597/api/v1/manifests | head -c 200
```
