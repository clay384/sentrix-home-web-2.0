#!/bin/bash
# 在 Mac mini（16 GB，MPS/CPU）上启动 2.0 的 legacy 嵌入方案。
#
# 前置（部署方负责，2.0 不管理模型进程）：
#   - 8100 llama-server（qwen35-q4km，multimodal）
#   - 8101 bge-m3 侧车（1.0 的 text_embedder_sidecar.py）
#   - ~/.cache/clip/clip_cn_vit-l-14.pt
#   - .venv 里有 cn_clip / torch（可从 1.0 的 .venv 复制）
#
# 与 server 部署的区别只有一处 profile + clip 侧车 + 部署地址；
# embedding/wall/chat/人脸 provider 都由 SENTRIX_DEPLOYMENT_PROFILE=mac 提供默认值。
set -eu

PROJ="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$PROJ"
mkdir -p logs data

# 平台默认值（legacy / 墙钟 600s / chat 180s / 人脸 CPU）由 profile 提供；
# 显式 env 仍可逐项覆盖。
export SENTRIX_DEPLOYMENT_PROFILE="${SENTRIX_DEPLOYMENT_PROFILE:-mac}"

DATA_DIR="${SENTRIX_DATA_DIR:-$PROJ/data}"
CLIP_PORT="${SENTRIX_LEGACY_CLIP_PORT:-8103}"
API_PORT="${SENTRIX_API_PORT:-9598}"

if ! pgrep -f "legacy_clip_sidecar.py" >/dev/null; then
  echo "启动 chinese-clip 侧车 :$CLIP_PORT"
  nohup .venv/bin/python scripts/maintenance/legacy_clip_sidecar.py \
    --port "$CLIP_PORT" > logs/legacy-clip.log 2>&1 < /dev/null &
  for _ in $(seq 1 60); do
    sleep 1
    if curl -s --max-time 2 "http://127.0.0.1:$CLIP_PORT/health" | grep -q '"status": "ok"'; then
      break
    fi
  done
fi

if pgrep -f "sentrix.main:create_app" >/dev/null; then
  echo "API 已在运行"
else
  echo "启动 2.0 API :$API_PORT（legacy）"
  env \
    SENTRIX_DEPLOYMENT_PROFILE="$SENTRIX_DEPLOYMENT_PROFILE" \
    SENTRIX_LEGACY_CLIP_URL="http://127.0.0.1:$CLIP_PORT" \
    SENTRIX_TEXT_EMBEDDER_URL="${SENTRIX_TEXT_EMBEDDER_URL:-http://127.0.0.1:8101}" \
    SENTRIX_VLM_BASE_URL="${SENTRIX_VLM_BASE_URL:-http://127.0.0.1:8100/v1}" \
    SENTRIX_DATA_DIR="$DATA_DIR" \
    nohup .venv/bin/python -m uvicorn --factory sentrix.main:create_app \
      --host 0.0.0.0 --port "$API_PORT" --app-dir apps/api/src \
      > logs/api.log 2>&1 < /dev/null &
fi

for _ in $(seq 1 40); do
  sleep 1
  if curl -s --max-time 2 "http://127.0.0.1:$API_PORT/api/v1/health" | grep -q '"status"'; then
    curl -s "http://127.0.0.1:$API_PORT/api/v1/health"
    echo
    exit 0
  fi
done

echo "API 未在 40 秒内就绪，日志：" >&2
tail -30 logs/api.log >&2
exit 1
