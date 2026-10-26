# Mac mini（16 GB）legacy 检索部署

目标机器：`saas@192.168.0.158`（`saass-Mac-mini.local`，arm64，macOS 27.0.1，16 GB）。
用途：在没有 10 GB 显存跑 WeMM 的机器上，用 1.0 的 chinese-clip + bge-m3 方案验证 2.0。

## 1. 依赖

| 能力 | 来源 | 端口 |
|---|---|---|
| 对话 / VLM | 本机 llama-server `qwen35-q4km` + `qwen35-mmproj-f16` | 8100 |
| 文本嵌入 | 本仓库 `scripts/maintenance/bge_text_sidecar.py`（BGE-M3 CPU） | 8101 |
| 视觉嵌入 | 本仓库 `scripts/maintenance/legacy_clip_sidecar.py`（Chinese-CLIP ViT-L/14） | 8103 |
| 人脸 | onnxruntime + insightface + RetinaFace（CPU） | 进程内 |
| 模型权重 | `data/models/qwen35-q4km/`、`data/models/bge-m3/`、`data/models/chinese-clip/`、`~/.insightface`、`~/benchmarks/retinaface` | — |

2.0 的 `.venv` 可以从 1.0 的 `.venv` 复制（已含 cn_clip / torch / onnxruntime / insightface），
再补 `pydantic-settings`：

```bash
cp -R ~/GitHub/Sentrix-Home-Web/.venv ~/GitHub/sentrix-home-web2.0/.venv
.venv/bin/python -m pip install pydantic-settings
```

## 2. 启动

```bash
cd ~/GitHub/sentrix-home-web2.0
./scripts/deploy/start_legacy_mac.sh
```

脚本会拉起本机完整模型栈和 API：

1. 设置 `SENTRIX_DEPLOYMENT_PROFILE=mac`（profile 默认：legacy / 墙钟 600s /
   chat 180s / 人脸 CPU）；
2. 设置 `SENTRIX_EMBEDDING_SCHEME=legacy`，并显式使用
   `FACE_PROVIDERS=CPUExecutionProvider`、`RETINAFACE_PROVIDERS=CPUExecutionProvider`；
3. 拉起 8100 llama-server、8101 BGE-M3 侧车、8103 Chinese-CLIP 侧车；
4. 拉起 8191 API；
5. 等三个模型健康检查和 `/api/v1/health` 返回。

`start_legacy_mac.sh` 是兼容入口，实际调用 `start_local_models_mac.sh`。默认选择
原项目使用的 `qwen35-q4km` 和 `qwen35-mmproj-f16`；模型路径、端口都可以用对应的
`SENTRIX_*` 环境变量覆盖。VLM 启动时固定使用 `--image-min-tokens 1024`，满足该模型的
视觉 grounding 最低 token 要求。

profile 不负责数据根、模型/侧车地址、llama-server 上下文和 judge 配置，仍需显式设置。

### 2.1 PhotoBench 启动

PhotoBench 使用同一台 Mac 的服务时可直接启动：

```bash
./scripts/deploy/start_photobench_mac.sh
```

该入口固定使用本机的 8191 API、8100 VLM、8101 BGE 和 8103 Chinese-CLIP，避免
PhotoBench 重启评测服务时丢失 Mac legacy 配置。

同样设 `SENTRIX_DEPLOYMENT_PROFILE=mac`，profile 会把 QA 并发设为 2、整册导入超时设为
86400s；`PHOTOBENCH_QA_CONCURRENCY` / run payload 的 `import_timeout` 仍可覆盖。
数据集根用 158 本机的 1.0 data：

```bash
SENTRIX_DEPLOYMENT_PROFILE=mac \
PHOTOBENCH_DATA_ROOT=$HOME/GitHub/Sentrix-Home-Web/services/photobench/data \
.venv/bin/python -m uvicorn --factory photobench.main:create_app \
  --host 0.0.0.0 --port 8772 --app-dir apps/photobench/src
```

### 2.2 PhotoBench 迁移（必做）

从 153 复制 PhotoBench 配置后，**必须**改 `apps/photobench/config/runtime_connection.json`：

```json
{"sentrix_url": "http://127.0.0.1:8191", "model_base_url": "http://127.0.0.1:8100/v1", ...}
```

PhotoBench 的 `SentrixClient` 读的是这个文件，不是 `PHOTOBENCH_SENTRIX_API_URL`。
不改的话，评测会把导入请求发回 153，表现为 `photo_import` 立即失败
`directory not found`，且 130 的 2.0 日志里看不到 `POST /api/v1/imports`。

PhotoBench 自己也要用 Mac legacy 环境启动，避免评测在切换模型端点时重启 8191
后丢失 `SENTRIX_EMBEDDING_SCHEME=legacy` 和 CPU provider：

```bash
./scripts/deploy/start_photobench_mac.sh
```

该入口固定传递 8101 BGE、8103 Chinese-CLIP、`CPUExecutionProvider`，并让
PhotoBench 后续重启主 API 时沿用同一组配置。

## 3. 验证

```bash
curl -s http://127.0.0.1:8191/api/v1/health
# {"status":"ok",...,"embeddings":"ok"}

curl -s "http://127.0.0.1:8191/api/v1/search?q=街道&scope_id=<scope>&limit=5"
# channel_trace 里 visual_ann / text_ann 应为 ok，backend=qdrant / sqlite_exact
```

向量落库应看到：

```
visual   chinese-clip-ViT-L-14   768d
semantic BAAI/bge-m3             1024d
episodic BAAI/bge-m3             1024d
```

## 4. 与 153 的差异

| 项 | 153 | 158 |
|---|---|---|
| 方案 | wemm（默认） | legacy |
| 视觉 | WeMM 2048d / 8102 | chinese-clip 768d / 8103 |
| 文本 | WeMM 2048d / 8102 | bge-m3 1024d / 8101 |
| 人脸 | CUDA | CPU |
| 显存/内存 | 24 GB GPU | 16 GB 统一内存 |

## 5. 已知限制

- 130 没有 2.0 的 GeoNames dump（`data/geo`），最近地点解析为空；行政区走 PyGeoCN
  （venv 里已可用）。
- 当前 Mac 已安装 `~/.insightface/models/buffalo_l/` 的识别权重，但缺少项目要求的
  `~/benchmarks/retinaface/retinaface_r50.onnx`；因此人脸检测/身份聚类仍是可选降级，
  图谱中的人脸关系验收必须在补齐该权重后再做。服务器上未找到这份精确的 RetinaFace-R50
  权重，不能用 `det_10g.onnx` 冒充（两者检测框格式不同）。
- 首次导入每张图要等 VLM 观察（实测 2 张约 45 秒），随后 finish/summarize 约 16 秒。
- 切换方案（wemm ↔ legacy）必须重建向量；旧 `model_name` 的行不会命中。
- 测试数据：`~/tmp/mac-smoke`，scope `mac-legacy-smoke2`（2 张图、2 个事件、
  visual/text 两类向量均已落库）。
