# PhotoBench 本地基准资产

该目录是 PhotoBench 的独立评测数据根，不进入 Git，也不与主产品的
`<仓库根>/data/` 共用。`photobench/config.py` 默认使用
`apps/photobench/datasets/`，本机没有额外设置 `PHOTOBENCH_DATA_ROOT`，因此 8772
直接读取这里。

## 当前数据集（2026-10-21）

| 数据集 | 内容 | QA | 来源 |
|---|---|---:|---|
| `album3-max-video10` | 334 张图片 + 10 个视频 + 8 张身份参考图 | 1115 / 52 / 1167 / 487 | `192.168.0.153:/home/asus/Github/Sentrix-Home-Web/services/photobench/data/album3-max-video10` |
| `album3-ai-video13` | 13 条 AI 生成视频 + 23 张源图元数据 | 74 | `192.168.0.153:/home/asus/Github/sentrix-home-web2.0/apps/photobench/datasets/album3-ai-video13` |
| `human-daily-video-30-v12` | 30 条连续无剪辑视频 | 224 | `192.168.0.153:/home/asus/Github/sentrix-home-web2.0/apps/photobench/datasets/human-daily-video-30-v12` |

两个最新数据集的 README 明确标注 QA 当前为 `pending`，因此可以用于导入、检索、
视频理解和运行链路验收，但不能把未人工审核的 QA 结果当作最终产品指标。

已按 `AlbumSource` 契约完成本地核验：

- 8772 `/api/v1/manifests` 可列出以上 3 个数据集；
- `import_target` 对应数量为 334/10、0/13、0/30；
- 四个旧数据集 QA 集、两个新数据集 QA 集均可读，缺失字段和缺失文件检查通过；
- 路径穿越返回空结果，`integrity()` 返回 `missing_files: []`；
- `album3-ai-video13` 的媒体完整性摘要为 15 个文件，`human-daily-video-30-v12`
  为 32 个文件，均为 SHA-256 分块校验。

## 本地目录关系

| 路径 | 是什么 | 谁读 |
|---|---|---|
| `apps/photobench/datasets/` | **评测夹具**：manifest、QA、照片、视频、参考图 | 8772 的 `/api/v1/manifests`、`/api/v1/qa-dataset`、评测运行 |
| `apps/photobench/results/` | **评测结果** | 8772 的 `/api/v1/runs` |
| `<仓库根>/data/` | **主产品数据空间** | 8191；API/e2e 用例 |

PhotoBench 到 Mac legacy 的连接配置必须保持在
`apps/photobench/config/runtime_connection.json`：

```json
{
  "sentrix_url": "http://127.0.0.1:8191",
  "model_base_url": "http://127.0.0.1:8100/v1"
}
```
