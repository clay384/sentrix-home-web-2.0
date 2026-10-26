#!/bin/bash
# Backward-compatible entry point for the complete local Mac deployment.
set -euo pipefail

PROJ="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$PROJ"
mkdir -p logs data

# 算子线程上限必须在 Python 启动前导入；留空时由 thread_limits 自动推导。
if [ -f deploy/thread-limits.env ]; then
  # shellcheck disable=SC1091
  . deploy/thread-limits.env
fi
export SENTRIX_LIB_THREADS="${SENTRIX_LIB_THREADS:-}"
if [ -n "$SENTRIX_LIB_THREADS" ] && [ "$SENTRIX_LIB_THREADS" != "0" ]; then
  export OMP_NUM_THREADS="$SENTRIX_LIB_THREADS"
  export MKL_NUM_THREADS="$SENTRIX_LIB_THREADS"
  export OPENBLAS_NUM_THREADS="$SENTRIX_LIB_THREADS"
  export NUMEXPR_NUM_THREADS="$SENTRIX_LIB_THREADS"
fi

# 平台默认值（legacy / 墙钟 600s / chat 180s / 人脸 CPU）由 profile 提供；
# 显式 env 仍可逐项覆盖。
export SENTRIX_DEPLOYMENT_PROFILE="${SENTRIX_DEPLOYMENT_PROFILE:-mac}"
exec "$PROJ/scripts/deploy/start_local_models_mac.sh" "$@"
