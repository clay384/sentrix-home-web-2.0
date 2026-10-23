# 2026-10-20：恢复 legacy 嵌入回退（chinese-clip + bge-m3）

## 背景

2.0 在 W1（2026-10-03）把检索嵌入钉死为 WeMM-Embedding-2B（2048 维，侧车 8102）。
153 上实测：权重 5.1 GB（bf16），侧车常驻显存 **9.9 GB**、进程 RSS **11.1 GB**。
16 GB 统一内存的 Mac mini 上，WeMM + 8100 对话模型（约 5 GB）+ 系统与 API 无法同时
装下。

用户 2026-10-20 裁决：**恢复 1.0 的 legacy 方案作为可选回退**，用于 130 Mac mini；
153 保持 WeMM，行为不变。

## 方案

**整方案原子切换，不是两个槽各切各的。**

```bash
SENTRIX_EMBEDDING_SCHEME=legacy   # 默认 wemm
```

| 方案 | 视觉 | 文本 | 空间 |
|---|---|---|---|
| `wemm`（默认） | WeMM-Embedding-2B / 2048d / 8102 | 同左 | 统一空间 |
| `legacy` | chinese-clip ViT-L-14 / 768d / 8103 | bge-m3 / 1024d / 8101 | 两个独立空间 |

保留 1.0 的两条纪律：

1. **一次只选一个具名方案**；不存在 `visual_embedder` / `text_embedder` 这类独立开关。
2. **切换必须伴随重建索引**；向量按 `model_name` 分套，旧向量不会命中新查询。

## 改动

| 文件 | 内容 |
|---|---|
| `apps/api/src/sentrix/domain/retrieval/scheme.py` | 新增 `EmbeddingScheme`、`SCHEMES`、`get_scheme()`；默认 `wemm` |
| `apps/api/src/sentrix/config.py` | 新增 `SENTRIX_EMBEDDING_SCHEME`、`SENTRIX_LEGACY_CLIP_URL`、`SENTRIX_LEGACY_CLIP_TIMEOUT_S`、`SENTRIX_TEXT_EMBEDDER_TIMEOUT_S`；未知方案启动失败 |
| `apps/api/src/sentrix/infrastructure/models/legacy.py` | `ChineseClipSidecarEmbedder`（768d）、`BgeSidecarEmbedder`（1024d）、`LegacyEmbeddingHealthProbe`；失败返回空表、5 次熔断 10s、维度校验 |
| `apps/api/src/sentrix/bootstrap/container.py` | 按 `settings.embedding_scheme` 分支装配，启动日志打印实际方案 |
| `scripts/maintenance/legacy_clip_sidecar.py` | chinese-clip 侧车（默认 8103，CUDA → MPS → CPU） |
| `apps/api/tests/unit/test_scheme.py` | 断言默认 wemm、legacy 维度、未知方案失败、只允许原子开关 |
| `apps/api/tests/unit/test_legacy_embedders.py` | 端点、维度校验、降级、熔断、健康探针 |

## 为什么 chinese-clip 也走侧车

与 WeMM 同一条设计约束：**2.0 的 API 进程不 import torch**。1.0 的 chinese-clip 是
进程内加载，2.0 改为 HTTP 侧车（8103），部署方负责拉起，应用不管理模型生命周期。

bge-m3 复用 1.0 的 8101 侧车（`POST /embed`，1024 维）。

## 部署

### 153（保持不变）

不设置 `SENTRIX_EMBEDDING_SCHEME`，默认 `wemm`；无需重启，行为与之前一致。

### 130 Mac mini（legacy）

依赖 1.0 已有资产：

- `~/.cache/clip/clip_cn_vit-l-14.pt`
- `cn_clip` + `torch`（1.0 的 `.venv` 已装）
- bge-m3 侧车 8101
- llama-server 8100

启动：

```bash
# 1) chinese-clip 侧车（8103）
.venv/bin/python scripts/maintenance/legacy_clip_sidecar.py --port 8103

# 2) 2.0 API（legacy 方案）
SENTRIX_EMBEDDING_SCHEME=legacy \
SENTRIX_LEGACY_CLIP_URL=http://127.0.0.1:8103 \
SENTRIX_TEXT_EMBEDDER_URL=http://127.0.0.1:8101 \
SENTRIX_VLM_BASE_URL=http://127.0.0.1:8100/v1 \
.venv/bin/python -m uvicorn --factory sentrix.main:create_app \
  --host 0.0.0.0 --port 9598 --app-dir apps/api/src
```

首次导入前必须确保向量表是空的，或者用新 `model_name` 重嵌。切回 wemm 时同理。

## 验证

- `ruff check` / `ruff format --check`：通过
- `mypy`（4 个源文件）：通过
- `pytest test_scheme.py test_legacy_embedders.py`：11 passed
- `check_module_map.py`：0 问题
- `check_capability_map.py`：0 问题
- `check_reachability.py` / `check_method_reachability.py`：0 问题

## 风险与边界

- legacy 是**非统一空间**：跨模态检索仍成立（各自 space 内自洽），但没有 WeMM 的
  原生图文同空间能力。
- 130 的 chinese-clip 在 MPS 上运行，首次加载与单张编码速度未做基准；侧车启动日志
  会打印实际设备。
- 从 wemm 切到 legacy（或反向）必须重建索引，否则新查询命中不到旧向量。
- 153 的默认值未变；只要不设置 `SENTRIX_EMBEDDING_SCHEME`，现有部署不受影响。
