# S10a 真机验收

在这台 153 上跑。**先重启服务再验**——S8b 踩过：拿旧代码验了半天，结论全是假的。

## 0. 起点

| 服务 | 端口 | 说明 |
|---|---|---|
| 2.0 主 API | 9598 | 已在跑 |
| 8100 / 8102 | 模型与 WeMM 侧车 | 已在跑 |
| 1.0 PhotoBench | 8771 | **差分对照，验收时才起** |
| 2.0 PhotoBench | 9597 | 被验对象 |

1.0 的 `results/` 有 270 个 run（19 GB），2.0 用 `PHOTOBENCH_RESULTS_ROOT` 指过去即可
——目录形状两边一致，`FileRunStore` 的设计就是为了这个。

## 1. 重启 2.0 PhotoBench

```sh
cd /home/asus/Github/sentrix-home-web2.0
PID=$(ss -ltnp 2>/dev/null | sed -n 's/.*:9597 .*pid=\([0-9]*\).*/\1/p' | head -1)
[ -n "$PID" ] && kill "$PID" && sleep 2

PHOTOBENCH_RESULTS_ROOT=/home/asus/Github/Sentrix-Home-Web/services/photobench/results \
nohup .venv/bin/python -m uvicorn --factory photobench.main:create_app \
  --host 0.0.0.0 --port 9597 --app-dir apps/photobench/src \
  >> apps/photobench/logs/uvicorn.log 2>&1 &
```

**不要用 `pkill -f`**：模式串会匹配到自己的 shell（本仓库踩过）。上面是从 `ss` 里取
pid 的写法。

等它扫完 `results/`（270 个 run 的后台扫描，进度见 `GET /api/runs` 的 `scan` 字段）。

## 2. 起 1.0 PhotoBench（差分对照）

```sh
cd /home/asus/Github/Sentrix-Home-Web/services/photobench
nohup python3 backend/benchmark_orchestrator.py --host 0.0.0.0 --port 8771 \
  >> logs/orchestrator.log 2>&1 &
```

它同步扫完 270 个 run 才绑端口，实测约 75 秒。轮询 `/api/config` 直到 200。

## 3. 差分比对

```sh
cd /home/asus/Github/sentrix-home-web2.0
.venv/bin/python /tmp/accept_s10a.py \
  20261002-162809-album3-max-video10-qwen35-q4km-reuse-478e58 \
  20260930-195355-album3-max-video10-qwen35-q4km-reuse-167...
```

**验收目标是「每条差异都可解释」**，不是「零差异」。已知会不同的见
`s10-deviations.md` §4.1 / §4.4；脚本里那张 `KNOWN_DIFFERENCES` 表就是它的机器可读版。

## 4. 这一轮实测到的两个既有事实

1. **1.0 那几个 video10 run 的 scope（`album_fdef329852e5`）在 2.0 里不存在**
   （`GET /api/v1/assets?scope_id=…` 回 `{"items": []}`）。所以这两个分析在 2.0 侧会走
   **工具轨迹回退**那条路——那正好是 `_recover_from_traces` 的真实用例，值钱。
2. **血缘检索那条路径**（`video_parent_recall` / `candidate_precision`）要先把
   album3-max-video10 导进 2.0、跑一轮小规模评测，让 scope 真的存在，才验得到。

## 5. 其余端点的验收（T1001–T1004）

| 端点 | 怎么验 |
|---|---|
| `GET /api/manifests` | 应列出 `apps/photobench/datasets/` 下 5 个相册及其计数 |
| `GET /api/manifests/{id}` | 与 `datasets/<id>/manifest.json` 原文逐字节相同 |
| `GET /api/albums/{album}/photos/{file}` | 200 + 正确 MIME；带 `Range: bytes=0-9` → 206 + `Content-Range: bytes 0-9/<size>` |
| `GET /api/assets/{id}/file` | 代理到 9598；上游 404 时状态码与错误体同上游 |
| `GET /api/memory-spaces` | `{"spaces": [...]}`，每条带 `created_at`；`reuse_bases` 非空（有历史 run 时） |
| `POST /api/test-model` | 指向在跑的端点 → `{"ok": true, …}`；不选模型 → 400 那句原文 |
| `GET /api/runs/{id}/export-trace` | `Content-Disposition` 正确；**中文不被转义成 `\uXXXX`**（`ensure_ascii=False`） |

## 6. 收口

- `capability-map.jsonl` / `photobench-module-map.md` / `behaviour-baseline.md` 重跑
- 两个 openapi 契约重跑（`scripts/export_openapi.py` / `export_photobench_openapi.py`）
- `make ci` 全绿
