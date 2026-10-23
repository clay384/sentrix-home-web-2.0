# PhotoBench 本地基准资产

来源：`/home/asus/Github/Sentrix-Home-Web/services/photobench/data/`（复制日期：2026-09-21）。

该目录为独立评测应用的本地数据，不进入 Git（`.gitignore:5`），不与主产品数据空间共用。

## 2026-10-06 复核

与 1.0 原件**逐字节一致**：20 个 `qa/*.jsonl`（问题 + 正确答案 + 预期动作）、
5 个 `manifest.json` 内容相同，`photos/` `faces/` `videos/` 的文件数与字节数全等。
1.0 侧在复制日之后**未再改动**过这批数据。

**这里就是评测数据集的家。** `photobench/config.py:87` 的 `data_root` 缺省值是
`APP_ROOT / "datasets"`，`sentrix-photobench.service` 从未设过 `PHOTOBENCH_DATA_ROOT`
去覆盖它——所以 9597 读的一直是这份，不依赖 1.0。

## 2.0 的三处本地数据，别搞混

| 路径 | 是什么 | 进 Git | 谁读 |
|---|---|---|---|
| `apps/photobench/datasets/` | **评测夹具**（本文档）——问题、答案、照片 | 否 | 9597 的 `/api/v1/manifests`、`/api/v1/datasets` |
| `apps/photobench/results/` | **评测结果**（当前为空） | 否 | 9597 的 `/api/v1/runs` |
| `<仓库根>/data/` | **主产品的数据空间**，另含两个黄金样本夹具 | 否（`/data/`） | 9598；e2e 用例读其中的夹具 |

第三行的两个夹具（主夹具 `album3-v2-source`、第二夹具 `household-benchmark-source`）
是 2026-10-06 从 1.0 搬来的，清单见
`docs/refactor/2026-10-06-eval-data-migration.md`。
