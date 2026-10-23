"""算子线程上限——治「重负载下几百个线程抢几十个核」。

**事故原型(1.0,xy 2026-10-20 取证)：** 153 上实测 241 个线程抢 32 核 → 健康检查
超 6 秒 → 被看门狗判死 → 重启 → **正在跑的评测被 SIGTERM 杀掉**。torch /
onnxruntime / opencv 默认都按核数开满线程，三个库叠在同一天进程里就是三倍超订。

**两层防线(都要上，缺一不可)：**

1. **环境变量层**(``deploy/thread-limits.env``,systemd ``EnvironmentFile``)：
   OMP/MKL/OPENBLAS/NUMEXPR 这些读环境变量的库，必须在进程启动前就位。
2. **代码层**(本模块)：xy 的结论——**``OMP_NUM_THREADS`` 对 opencv /
   onnxruntime 无效**，这两个必须显式设：``cv2.setNumThreads`` 与
   ``SessionOptions.intra_op_num_threads``。所以除了环境变量，调用点必须真调。

线程预算的口径：一个进程里 cv2 + torch + onnx 各开 N 线程，总数 ≈ 3N + 开销。
默认取核数的 1/4(下限 2)——32 核机器上三库合计约 24 线程，留得住调度余量；
``SENTRIX_LIB_THREADS`` 环境变量可覆盖，不猜设备。

落在 infrastructure 的理由：它 import cv2/torch/onnxruntime 这类重库(惰性)、
读进程环境——domain/application 禁止碰这些。
"""

from __future__ import annotations

import os
from typing import Any

_LIB_THREADS_ENV = "SENTRIX_LIB_THREADS"

_APPLIED = False


def lib_threads() -> int:
    """单库线程预算。``SENTRIX_LIB_THREADS`` 优先；默认核数的 1/4，下限 2。"""
    raw = (os.environ.get(_LIB_THREADS_ENV) or "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    cpus = os.cpu_count() or 4
    return max(2, cpus // 4)


def apply_process_thread_limits() -> dict[str, int]:
    """进程级线程上限。幂等：create_app 可能被测试多次调用。

    **每个库单独 try**：cv2/torch 任一缺席(比如 photobench 编排容器里没装)只跳过
    那一个，不拖垮其他库，也不抛——线程上限是保险丝，保险丝自己不能成为新故障点。
    """
    global _APPLIED
    applied: dict[str, int] = {}
    threads = lib_threads()
    if _APPLIED:
        return applied
    _APPLIED = True
    try:
        import cv2

        cv2.setNumThreads(threads)
        applied["cv2"] = threads
    except Exception:  # noqa: BLE001 - 缺库/任何加载失败都按「没这个库」处理
        pass
    try:
        import torch

        torch.set_num_threads(threads)
        applied["torch"] = threads
    except Exception:  # noqa: BLE001
        pass
    return applied


def ort_session_options() -> Any:
    """带线程上限的 ``onnxruntime.SessionOptions``。

    onnxruntime 的线程数**只能**在 session 构造时通过 options 传入(环境变量无效)，
    所以所有 ``InferenceSession`` 构造点都必须用这个工厂，不能裸建 session。
    ``intra_op``(单算子并行)与 ``inter_op``(算子间并行)同给预算；
    ``spinning`` 关掉——线程等活时自旋在「健康检查超时」事故里是帮凶。
    """
    import onnxruntime

    options = onnxruntime.SessionOptions()
    threads = lib_threads()
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = threads
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    options.add_session_config_entry("session.inter_op.allow_spinning", "0")
    return options
