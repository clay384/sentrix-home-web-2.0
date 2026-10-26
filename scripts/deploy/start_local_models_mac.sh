#!/bin/bash
# Start the complete local model stack for the 16 GB Apple Silicon deployment.
#
# Ports are intentionally kept compatible with the application contract:
#   8100  llama.cpp OpenAI-compatible VLM (qwen35-q4km + mmproj)
#   8101  BGE-M3 text embeddings (1024d)
#   8103  Chinese-CLIP ViT-L/14 image embeddings (768d)
#   8191  Sentrix API (legacy embedding scheme)
set -euo pipefail

PROJ="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$PROJ"
mkdir -p logs data/model-runtime

DATA_DIR="${SENTRIX_DATA_DIR:-$PROJ/data}"
export SENTRIX_DEPLOYMENT_PROFILE="${SENTRIX_DEPLOYMENT_PROFILE:-mac}"
VLM_PORT="${SENTRIX_VLM_PORT:-8100}"
BGE_PORT="${SENTRIX_BGE_PORT:-8101}"
CLIP_PORT="${SENTRIX_LEGACY_CLIP_PORT:-8103}"
API_PORT="${SENTRIX_API_PORT:-8191}"
VLM_MODEL="${SENTRIX_VLM_MODEL:-$PROJ/data/models/qwen35-q4km/qwen35-q4km.gguf}"
VLM_MMPROJ="${SENTRIX_VLM_MMPROJ:-$PROJ/data/models/qwen35-q4km/qwen35-mmproj-f16.gguf}"
CLIP_CHECKPOINT="${SENTRIX_CHINESE_CLIP_CHECKPOINT:-$PROJ/data/models/chinese-clip/clip_cn_vit-l-14.pt}"

export FACE_PROVIDERS="${FACE_PROVIDERS:-CPUExecutionProvider}"
export RETINAFACE_PROVIDERS="${RETINAFACE_PROVIDERS:-CPUExecutionProvider}"

port_in_use() {
  lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
}

wait_http() {
  local url="$1"
  local tries="${2:-120}"
  for _ in $(seq 1 "$tries"); do
    if curl -fsS --max-time 2 "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

if [ ! -f "$VLM_MODEL" ] || [ ! -f "$VLM_MMPROJ" ] || [ ! -f "$CLIP_CHECKPOINT" ]; then
  echo "模型权重缺失：VLM=$VLM_MODEL / mmproj=$VLM_MMPROJ / CLIP=$CLIP_CHECKPOINT" >&2
  exit 1
fi

if ! port_in_use "$VLM_PORT"; then
  echo "启动 VLM :$VLM_PORT"
  nohup llama-server \
    --model "$VLM_MODEL" \
    --mmproj "$VLM_MMPROJ" \
    --alias qwen35-q4km \
    --host 127.0.0.1 \
    --port "$VLM_PORT" \
    --ctx-size "${SENTRIX_VLM_CTX_SIZE:-8192}" \
    --parallel "${SENTRIX_VLM_PARALLEL:-1}" \
    --n-gpu-layers "${SENTRIX_VLM_GPU_LAYERS:-999}" \
    --image-min-tokens 1024 \
    --reasoning off \
    > logs/vlm.log 2>&1 < /dev/null &
fi
wait_http "http://127.0.0.1:$VLM_PORT/v1/models" 180 || {
  echo "VLM 未就绪，日志：" >&2
  tail -60 logs/vlm.log >&2
  exit 1
}

if ! port_in_use "$BGE_PORT"; then
  echo "启动 BGE-M3 :$BGE_PORT"
  nohup .venv/bin/python scripts/maintenance/bge_text_sidecar.py \
    --port "$BGE_PORT" > logs/bge-m3.log 2>&1 < /dev/null &
fi
wait_http "http://127.0.0.1:$BGE_PORT/health" 180 || {
  echo "BGE-M3 未就绪，日志：" >&2
  tail -60 logs/bge-m3.log >&2
  exit 1
}

if ! port_in_use "$CLIP_PORT"; then
  echo "启动 Chinese-CLIP :$CLIP_PORT"
  CHINESE_CLIP_CHECKPOINT="$CLIP_CHECKPOINT" nohup .venv/bin/python scripts/maintenance/legacy_clip_sidecar.py \
    --port "$CLIP_PORT" > logs/legacy-clip.log 2>&1 < /dev/null &
fi
wait_http "http://127.0.0.1:$CLIP_PORT/health" 180 || {
  echo "Chinese-CLIP 未就绪，日志：" >&2
  tail -60 logs/legacy-clip.log >&2
  exit 1
}

if ! port_in_use "$API_PORT"; then
  echo "启动 Sentrix API :$API_PORT"
  env \
    SENTRIX_EMBEDDING_SCHEME=legacy \
    SENTRIX_LEGACY_CLIP_URL="http://127.0.0.1:$CLIP_PORT" \
    SENTRIX_TEXT_EMBEDDER_URL="http://127.0.0.1:$BGE_PORT" \
    SENTRIX_VLM_BASE_URL="http://127.0.0.1:$VLM_PORT/v1" \
    SENTRIX_DATA_DIR="$DATA_DIR" \
    nohup .venv/bin/python -m uvicorn --factory sentrix.main:create_app \
      --host 127.0.0.1 --port "$API_PORT" --app-dir apps/api/src \
      > logs/api.log 2>&1 < /dev/null &
fi
wait_http "http://127.0.0.1:$API_PORT/api/v1/health" 60 || {
  echo "Sentrix API 未就绪，日志：" >&2
  tail -60 logs/api.log >&2
  exit 1
}

curl -fsS "http://127.0.0.1:$API_PORT/api/v1/health"
echo
