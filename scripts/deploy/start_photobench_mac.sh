#!/bin/bash
# Start PhotoBench with the Mac legacy environment it must pass to API restarts.
set -euo pipefail

PROJ="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$PROJ"
mkdir -p logs

# PhotoBench itself must use the Mac budget/profile. Its model-switch worker
# inherits this environment when it restarts the product API during a run.
export SENTRIX_DEPLOYMENT_PROFILE="${SENTRIX_DEPLOYMENT_PROFILE:-mac}"
export FACE_PROVIDERS="${FACE_PROVIDERS:-CPUExecutionProvider}"
export RETINAFACE_PROVIDERS="${RETINAFACE_PROVIDERS:-CPUExecutionProvider}"

PORT="${PHOTOBENCH_PORT:-8772}"
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "PhotoBench 已在 :$PORT 监听；本脚本不会重启服务或停止正在运行的测评"
  exit 0
fi

export PHOTOBENCH_SENTRIX_API_URL="${PHOTOBENCH_SENTRIX_API_URL:-http://127.0.0.1:8191}"
export PHOTOBENCH_SENTRIX_EMBEDDING_SCHEME="${PHOTOBENCH_SENTRIX_EMBEDDING_SCHEME:-legacy}"
export PHOTOBENCH_SENTRIX_TEXT_EMBEDDER_URL="${PHOTOBENCH_SENTRIX_TEXT_EMBEDDER_URL:-http://127.0.0.1:8101}"
export PHOTOBENCH_SENTRIX_LEGACY_CLIP_URL="${PHOTOBENCH_SENTRIX_LEGACY_CLIP_URL:-http://127.0.0.1:8103}"
export PHOTOBENCH_SENTRIX_FACE_PROVIDERS="${PHOTOBENCH_SENTRIX_FACE_PROVIDERS:-CPUExecutionProvider}"
export PHOTOBENCH_SENTRIX_RETINAFACE_PROVIDERS="${PHOTOBENCH_SENTRIX_RETINAFACE_PROVIDERS:-CPUExecutionProvider}"

nohup .venv/bin/python -m uvicorn --factory photobench.main:create_app \
  --host 127.0.0.1 --port "$PORT" --app-dir apps/photobench/src \
  > logs/photobench.log 2>&1 < /dev/null &

for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:$PORT/api/v1/manifests" >/dev/null 2>&1; then
    echo "PhotoBench 已就绪 :$PORT"
    exit 0
  fi
  sleep 1
done

echo "PhotoBench 未就绪，日志：" >&2
tail -60 logs/photobench.log >&2
exit 1
