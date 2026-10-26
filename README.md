# Sentrix Home Web 2.0

家庭相册记忆系统：照片/视频导入 → 记忆构建 → 自然语言检索与问答 → PhotoBench 评测。

---

## ⚠️ 修改代码前必读（强制）

**任何人在修改本项目代码之前，必须先完整阅读：**

### [`docs/architecture/module-interfaces.md`](docs/architecture/module-interfaces.md)

这份文档不是“参考资料”，是**强制规则**。它规定了：

- 四层架构与 11 条 import 契约；
- 所有可替换端口（模型、嵌入、OCR、地理、存储、评测）的接口与失败语义；
- **所有代码改动**（不只是换模块）必须遵守的十条硬规则；
- 改动流程、验收标准（Definition of Done）、PR 检查清单与反模式；
- 153（WeMM）与 130（legacy）两套部署的差异和迁移检查项。

**没有读完就开始改，最常见的后果**：反向依赖、静默降级、外部契约被改坏、
换了嵌入却没重建索引、PhotoBench 迁移后请求仍发往旧机器。

如果你只想快速判断自己的改动是否合规，先看文档的 **§0.5 十条硬规则** 与 **§5 验收标准**。

---

## 快速开始

### 153（默认，WeMM）

```bash
make dev            # 主 API 8191
make pb-dev         # PhotoBench 8772
```

或单独启动：

```bash
./scripts/deploy/start_api.sh
./scripts/deploy/start_photobench.sh
```

### Mac mini / 16 GB 机器（legacy）

```bash
SENTRIX_DEPLOYMENT_PROFILE=mac ./scripts/deploy/start_legacy_mac.sh
./scripts/deploy/start_photobench_mac.sh  # PhotoBench 8772；保留 Mac legacy 重启环境
```

详见 [`docs/runbooks/mac-mini-legacy.md`](docs/runbooks/mac-mini-legacy.md)。

### 部署 profile（同一份代码，server / mac）

平台差异**不写成代码里的 `if macOS`**。用 `SENTRIX_DEPLOYMENT_PROFILE=server|mac`
提供一组默认值；**显式 env / run payload 永远优先**。

| 默认项 | `server`（153） | `mac`（158） |
|---|---|---|
| `embedding_scheme` | `wemm` | `legacy` |
| `agent_wall_time_s` | 180 | 600 |
| `chat_timeout_s` | 120 | 180 |
| QA 并发 | `None`（按端点槽位探测） | 2 |
| `import_timeout` | 7200 | 86400 |
| `FACE_PROVIDERS` / `RETINAFACE_PROVIDERS` | CUDA,CPU | CPU |

显式覆盖示例：

```bash
# 一台 Mac 仍想用 WeMM
SENTRIX_DEPLOYMENT_PROFILE=mac SENTRIX_EMBEDDING_SCHEME=wemm ...
# 临时把 Mac 的 QA 并发提到 4
PHOTOBENCH_QA_CONCURRENCY=4 ...
# 单次 run 覆盖导入超时
{"import_timeout": 7200}
```

**profile 不负责的部署项（仍需显式设置）：**

- 数据/结果/配置/前端产物根：`SENTRIX_DATA_DIR`、`PHOTOBENCH_{DATA,RESULTS,CONFIG,WEB_DIST}_ROOT`
- 模型与侧车地址：`SENTRIX_VLM_BASE_URL`、`SENTRIX_WEMM_URL`、
  `SENTRIX_TEXT_EMBEDDER_URL`、`SENTRIX_LEGACY_CLIP_URL`
- llama-server 的 `--ctx-size/--parallel`（决定每个槽的上下文窗口）
- judge 端点/模型/密钥（`judge_providers.json` 或 run payload）
- 数据集根（158 的 `PHOTOBENCH_DATA_ROOT` 指向 1.0 的 `services/photobench/data`）

153 不设 profile（默认 `server`）；158 只需在启动时设
`SENTRIX_DEPLOYMENT_PROFILE=mac`，再补齐上面这些部署项。

---

## 目录

```
apps/api/          主产品（sentrix.*）：api → application → domain，infrastructure 实现端口
apps/web/          主前端（8191 挂载）
apps/photobench/   独立评测包（photobench.*），只经 /api/v1 HTTP 访问主产品
docs/architecture/ 设计、模块图、**强制规则与端口文档**
docs/refactor/     重构计划、裁决、能力登记、偏离记录
docs/runbooks/     部署与运维手册
scripts/           开发、部署、维护脚本
```

记忆构建沿用 2.0 原有资产/观察/事件/实体/事实/向量流水线，不引入旧版记忆生成器。可插拔的 `BuildMemoryGraph` 从这些标准端口构建事件、上下文、同视频关键帧时序、对象/关系枢纽、互选视觉近邻和 `rules_vlm` 因果边；`RetrieveGraph` 按问题类型有界拓展，原回答提示词和生成器不变。图效果的参考边从节点来源字段重建，不能把构图边自身当真值；详见 `docs/refactor/hpq-causal-deviations.md`。当前观察没有 bbox，因果规则仍使用 hpq 的 label-only 回退。

---

## 改动前检查（最低要求）

```bash
make lint format-check typecheck arch photobench-check check-docs coverage
# 或一次跑完
make ci
```

新增/移动 Python 文件必须同步 `docs/architecture/module-map.md`；新增能力必须同步
`docs/refactor/capability-map.jsonl`；偏离 1.0 行为必须写 `*-deviations.md`。

---

## 评测

PhotoBench 运行结果在 `apps/photobench/results/<run_id>/`，页面在 8772。
QA 数据集在 `apps/photobench/datasets/`。

---

## 许可与数据

数据集、模型权重、GeoNames dump 不进版本控制；部署方按 `docs/runbooks/` 放置。
