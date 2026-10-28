"""Chinese-CLIP（ViT-L-14，768 维）视觉嵌入侧车，端口 8103。

legacy 嵌入方案的视觉半边。文本半边仍走 1.0 的 bge-m3 侧车（8101）。

**为什么是侧车，不是进程内。** 与 WeMM 同一条设计约束：2.0 的 API 进程不 import
torch（设计 §2.1）。2.0 自己也不管理模型生命周期——这个脚本由部署方拉起，等价于
1.0 的 ``text_embedder_sidecar.py``。

**双后端自动选择。** 启动时按优先级探测可用后端：

1. **ONNX Runtime**（推荐，CUDA 设备上更快）
   需要 FP16 ONNX 模型文件。默认查找 ``~/.cache/clip/`` 下的
   ``clip_cn_vit-l-14.img.fp16.onnx`` + ``clip_cn_vit-l-14.txt.fp16.onnx``。
   可由 ``SENTRIX_CLIP_IMG_ONNX`` / ``SENTRIX_CLIP_TXT_ONNX`` 覆盖。
   CUDA 设备加载失败时自动重试（最多 3 次），应对瞬态显存不足。

2. **PyTorch + cn_clip**（通用，Mac / CPU / 无 ONNX 模型时使用）
   需要 ``cn_clip`` + ``torch``。默认查找 ``~/.cache/clip/clip_cn_vit-l-14.pt``。
   可由 ``CHINESE_CLIP_CHECKPOINT`` 覆盖。
   设备选择：CUDA → MPS (Apple Silicon) → CPU。

可通过 ``SENTRIX_CLIP_BACKEND=onnx|torch|auto`` 强制指定后端（默认 auto）。

用法::

    .venv/bin/python scripts/maintenance/legacy_clip_sidecar.py --port 8103
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np
    from PIL import Image

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.maintenance.embedding_identity import clip_identity, implementation_sha, runtime_versions  # noqa: E402

DIMENSION = 768
CONTEXT_LENGTH = 52

# ONNX 模型默认路径（~/.cache/clip/）
_DEFAULT_IMG_ONNX = Path.home() / ".cache" / "clip" / "clip_cn_vit-l-14.img.fp16.onnx"
_DEFAULT_TXT_ONNX = Path.home() / ".cache" / "clip" / "clip_cn_vit-l-14.txt.fp16.onnx"

# PyTorch checkpoint 默认路径（与远程 origin/main 一致）
_DEFAULT_CHECKPOINT = Path.home() / ".cache" / "clip" / "clip_cn_vit-l-14.pt"

# 后端标识
BACKEND_ONNX = "onnxruntime"
BACKEND_TORCH = "pytorch"

# ── ONNX 后端状态 ──
_img_session = None
_txt_session = None

# ── PyTorch 后端状态 ──
_torch_model = None
_torch_preprocess = None

# ── 公共状态 ──
_backend = ""  # BACKEND_ONNX | BACKEND_TORCH
_device = "cpu"
_error = ""
_lock = threading.Lock()
_identity: dict[str, object] | None = None
_identity_error = ""
_identity_runtime: dict[str, object] = {}


# ═══════════════════════════════════════════════════════════
# 后端探测
# ═══════════════════════════════════════════════════════════


def _resolve_onnx_paths() -> tuple[Path, Path]:
    """返回 ONNX 模型路径（环境变量 > 默认路径）。"""
    img = Path(os.getenv("SENTRIX_CLIP_IMG_ONNX", str(_DEFAULT_IMG_ONNX))).expanduser()
    txt = Path(os.getenv("SENTRIX_CLIP_TXT_ONNX", str(_DEFAULT_TXT_ONNX))).expanduser()
    return img, txt


def _resolve_torch_checkpoint() -> Path:
    """返回 PyTorch checkpoint 路径（环境变量 > 默认路径）。"""
    return Path(os.getenv("CHINESE_CLIP_CHECKPOINT", str(_DEFAULT_CHECKPOINT))).expanduser()


def _detect_backend() -> str:
    """自动探测可用后端。优先级：ONNX > PyTorch。

    可通过 ``SENTRIX_CLIP_BACKEND`` 环境变量强制指定。
    """
    requested = os.getenv("SENTRIX_CLIP_BACKEND", "auto").strip().lower()
    if requested == "onnx":
        return BACKEND_ONNX
    if requested == "torch":
        return BACKEND_TORCH

    # auto: 优先 ONNX（更快），回退 PyTorch（更通用）
    img_path, txt_path = _resolve_onnx_paths()
    if img_path.is_file() and txt_path.is_file():
        return BACKEND_ONNX

    checkpoint = _resolve_torch_checkpoint()
    if checkpoint.is_file():
        return BACKEND_TORCH

    # 都没有——返回 ONNX 让加载时给出明确的 missing 错误
    return BACKEND_ONNX


# ═══════════════════════════════════════════════════════════
# ONNX 后端
# ═══════════════════════════════════════════════════════════


def _pick_onnx_providers() -> list[str]:
    """根据环境变量或可用 EP 选择 ONNX Runtime execution providers。"""
    requested = os.getenv("SENTRIX_LEGACY_CLIP_DEVICE", "auto").strip().lower()
    if requested == "cpu":
        return ["CPUExecutionProvider"]
    import onnxruntime as ort

    available = ort.get_available_providers()
    if "CUDAExecutionProvider" in available:
        return ["CUDAExecutionProvider", "CPUExecutionProvider"]
    return ["CPUExecutionProvider"]


def _preprocess_image_numpy(img: Image.Image) -> np.ndarray:
    """PIL Image → FP16 numpy array，shape (1, 3, 224, 224)。

    等价于 torchvision.transforms.Compose([
        Resize((224, 224), BICUBIC),
        ToTensor,
        Normalize(mean, std),
    ]) 但用纯 numpy 实现，不依赖 torch。
    """
    import numpy as np
    from PIL import Image

    _INPUT_SIZE = 224
    _MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
    _STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)

    img = img.convert("RGB").resize((_INPUT_SIZE, _INPUT_SIZE), Image.Resampling.BICUBIC)
    arr = np.asarray(img, dtype=np.float32) / 255.0
    arr = np.transpose(arr, (2, 0, 1))
    arr = (arr - _MEAN[:, None, None]) / _STD[:, None, None]
    return arr[np.newaxis].astype(np.float16)


def _load_onnx() -> None:
    """加载 ONNX session。失败时自动重试（最多 3 次），应对瞬态显存不足。"""
    global _img_session, _txt_session, _device, _error

    img_path, txt_path = _resolve_onnx_paths()
    providers = _pick_onnx_providers()
    _device = "cuda" if "CUDAExecutionProvider" in providers else "cpu"

    if not img_path.is_file():
        _error = f"vision ONNX missing: {img_path}"
        return
    if not txt_path.is_file():
        _error = f"text ONNX missing: {txt_path}"
        return

    max_retries = 3
    retry_delays = [5, 10]

    for attempt in range(max_retries):
        try:
            import onnxruntime as ort

            sess_opts = ort.SessionOptions()
            sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

            if attempt > 0:
                print(f"[CLIP] 第 {attempt + 1} 次尝试加载 ONNX 模型...")

            _img_session = ort.InferenceSession(str(img_path), sess_options=sess_opts, providers=providers)
            _txt_session = ort.InferenceSession(str(txt_path), sess_options=sess_opts, providers=providers)
            _error = ""

            if attempt > 0:
                print(f"[CLIP] ONNX 模型加载成功（第 {attempt + 1} 次尝试）")
            else:
                print("[CLIP] ONNX 模型加载成功")
            return
        except Exception as error:  # noqa: BLE001
            _error = f"{type(error).__name__}: {error}"
            if attempt < max_retries - 1:
                delay = retry_delays[attempt]
                print(f"[CLIP] ONNX 加载失败（{type(error).__name__}），{delay}s 后重试...")
                print(f"[CLIP] 错误详情: {str(error)[:200]}")
                time.sleep(delay)
            else:
                print(f"[CLIP] ONNX 加载失败，已重试 {max_retries} 次")
                print(f"[CLIP] 最终错误: {str(error)[:200]}")
                if "CUBLAS" in str(error) or "ALLOC_FAILED" in str(error):
                    print("[CLIP] 提示: 可能是 GPU 显存不足。")
                    print("[CLIP] 建议: 设置 SENTRIX_LEGACY_CLIP_DEVICE=cpu 使用 CPU 模式")


def _embed_image_onnx(path: str) -> list[float]:
    import numpy as np
    from PIL import Image

    if _img_session is None:
        raise RuntimeError(_error or "vision model not loaded")
    if not Path(path).is_file():
        raise FileNotFoundError(path)
    img = _preprocess_image_numpy(Image.open(path))
    with _lock:
        out = _img_session.run(None, {"image": img})[0]
    return _normalize_numpy(out.astype(np.float32).flatten())


def _embed_query_onnx(text: str) -> list[float]:
    import numpy as np

    if _txt_session is None:
        raise RuntimeError(_error or "text model not loaded")
    from cn_clip.clip import tokenize

    tokens = tokenize([str(text)], context_length=CONTEXT_LENGTH).numpy()
    with _lock:
        out = _txt_session.run(None, {"text": tokens})[0]
    return _normalize_numpy(out.astype(np.float32).flatten())


def _normalize_numpy(vec: np.ndarray) -> list[float]:
    import numpy as np

    norm = float(np.linalg.norm(vec))
    if norm > 0:
        vec = vec / norm
    return [float(value) for value in vec.tolist()]


# ═══════════════════════════════════════════════════════════
# PyTorch 后端
# ═══════════════════════════════════════════════════════════


def _pick_torch_device() -> str:
    """PyTorch 设备选择：CUDA → MPS → CPU。"""
    requested = os.getenv("SENTRIX_LEGACY_CLIP_DEVICE", "auto").strip().lower()
    if requested and requested != "auto":
        return requested
    import torch

    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _current_identity(checkpoint: Path, model_name: str) -> dict[str, object]:
    from cn_clip.clip import utils

    spec = {**utils._MODEL_INFO[model_name], "checkpoint_filename": utils._MODELS[model_name][1]}
    return clip_identity(
        checkpoint,
        model_name,
        Path(utils.__file__).resolve().parents[1],
        spec,
        implementation_sha(
            Path(__file__),
            [
                "_normalize_list",
                "_embed_image_torch",
                "_embed_query_torch",
                "_embed_image",
                "_embed_query",
                "Handler.do_POST",
            ],
        ),
    )


def _load_torch() -> None:
    """Load PyTorch; certify only artifacts stable across the actual model load."""
    global _torch_model, _torch_preprocess, _device, _error
    global _identity, _identity_error, _identity_runtime

    _identity, _identity_error, _identity_runtime = None, "", {}
    checkpoint = _resolve_torch_checkpoint()
    model_name = os.getenv("CHINESE_CLIP_MODEL", "ViT-L-14")
    _device = _pick_torch_device()

    if not checkpoint.is_file():
        _error = f"checkpoint missing: {checkpoint}"
        return
    try:
        import torch
        from cn_clip.clip import load_from_name

        before = None
        try:
            before = _current_identity(checkpoint, model_name)
        except Exception as error:  # noqa: BLE001 - identity failure does not alter embedding behavior
            _identity_error = f"{type(error).__name__}: {error}"

        # cn_clip 按 ``download_root`` 找 ``clip_cn_vit-l-14.pt``。指向已有权重的
        # 目录，避免它尝试联网下载。
        model, preprocess = load_from_name(
            model_name,
            device=_device,
            download_root=str(checkpoint.parent),
        )
        model.eval()
        _torch_model = model
        _torch_preprocess = preprocess
        _error = ""
        print(f"[CLIP] PyTorch 模型加载成功 (device={_device})")
        _identity_runtime = {
            "device": _device,
            "threads": torch.get_num_threads(),
            "parameter_dtypes": sorted({str(p.dtype) for p in _torch_model.parameters()}),
            "versions": runtime_versions(["torch", "torchvision", "Pillow", "cn-clip"]),
        }
        try:
            after = _current_identity(checkpoint, model_name)
            if before is None or before != after:
                raise ValueError("embedding artifacts changed or were unidentified during model load")
            _identity, _identity_error = after, ""
        except Exception as error:  # noqa: BLE001 - loaded model can serve; capacity identity remains unavailable
            _identity = None
            _identity_error = f"{type(error).__name__}: {error}"
    except Exception as error:  # noqa: BLE001 - 启动失败要让 /health 说出来
        _error = f"{type(error).__name__}: {error}"


def _embed_image_torch(path: str) -> list[float]:
    import torch
    from PIL import Image

    if _torch_model is None or _torch_preprocess is None:
        raise RuntimeError(_error or "model not loaded")
    if not Path(path).is_file():
        raise FileNotFoundError(path)
    image = _torch_preprocess(Image.open(path).convert("RGB")).unsqueeze(0).to(_device)
    with _lock, torch.no_grad():
        vector = _torch_model.encode_image(image)
    return _normalize_list([float(v) for v in vector[0].cpu().tolist()])


def _embed_query_torch(text: str) -> list[float]:
    import torch
    from cn_clip.clip import tokenize

    if _torch_model is None:
        raise RuntimeError(_error or "model not loaded")
    tokens = tokenize([str(text)]).to(_device)
    with _lock, torch.no_grad():
        vector = _torch_model.encode_text(tokens)
    return _normalize_list([float(v) for v in vector[0].cpu().tolist()])


def _normalize_list(values: list[float]) -> list[float]:
    norm = sum(v * v for v in values) ** 0.5
    if norm <= 0:
        return values
    return [v / norm for v in values]


# ═══════════════════════════════════════════════════════════
# 统一入口（根据后端分发）
# ═══════════════════════════════════════════════════════════


def _load() -> None:
    """根据探测结果加载对应后端。ONNX 失败时尝试回退到 PyTorch。"""
    global _backend, _error, _identity, _identity_error, _identity_runtime

    _identity, _identity_error, _identity_runtime = None, "", {}
    _backend = _detect_backend()
    print(f"[CLIP] 选择后端: {_backend}")

    if _backend == BACKEND_ONNX:
        _identity_error = "ONNX embedding capacity identity is not certified; use the pinned torch backend"
        _load_onnx()
        # ONNX 加载失败 → 尝试回退到 PyTorch
        if _img_session is None:
            onnx_error = _error
            checkpoint = _resolve_torch_checkpoint()
            if checkpoint.is_file():
                print("[CLIP] ONNX 失败，回退到 PyTorch 后端...")
                _backend = BACKEND_TORCH
                _load_torch()
            else:
                # 两个后端都不可用，保留 ONNX 的错误信息
                _error = onnx_error
    elif _backend == BACKEND_TORCH:
        _load_torch()


def _embed_image(path: str) -> list[float]:
    if _backend == BACKEND_ONNX:
        return _embed_image_onnx(path)
    return _embed_image_torch(path)


def _embed_query(text: str) -> list[float]:
    if _backend == BACKEND_ONNX:
        return _embed_query_onnx(text)
    return _embed_query_torch(text)


def _is_loaded() -> bool:
    if _backend == BACKEND_ONNX:
        return _img_session is not None and _txt_session is not None
    return _torch_model is not None and _torch_preprocess is not None


# ═══════════════════════════════════════════════════════════
# HTTP Handler
# ═══════════════════════════════════════════════════════════


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def _send(self, code: int, payload: dict[str, object]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(raw, dict):
            return {}
        return {str(key): value for key, value in raw.items()}

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            ok = _is_loaded()
            self._send(
                200 if ok else 503,
                {
                    "status": "ok" if ok else "error",
                    "model": os.getenv("CHINESE_CLIP_MODEL", "ViT-L-14"),
                    "backend": _backend,
                    "dimension": DIMENSION,
                    "device": _device,
                    "error": _error,
                    "embedding_identity": _identity if _backend == BACKEND_TORCH else None,
                    "embedding_identity_error": (
                        _identity_error
                        if _backend == BACKEND_TORCH
                        else "ONNX embedding capacity identity is not certified; use the pinned torch backend"
                    ),
                    "embedding_runtime": {
                        **_identity_runtime,
                        "listen_port": getattr(self.server, "server_port", None),
                    },
                },
            )
        elif self.path == "/stats":
            self._send(
                200,
                {
                    "backend": _backend,
                    "device": _device,
                    "dimension": DIMENSION,
                    "loaded": _is_loaded(),
                    "error": _error,
                },
            )
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            body = self._body()
            if self.path == "/embed_image":
                path = str(body.get("path") or "")
                if not path:
                    self._send(400, {"error": "path 不能为空"})
                    return
                vector = _embed_image(path)
            elif self.path in ("/embed_query", "/embed_text"):
                text = str(str(body.get("text") or ""))
                if not text.strip():
                    self._send(400, {"error": "text 不能为空"})
                    return
                vector = _embed_query(text)
            else:
                self._send(404, {"error": "not found"})
                return
            self._send(200, {"vector": vector, "dimension": len(vector)})
        except Exception as error:  # noqa: BLE001
            self._send(500, {"error": f"{type(error).__name__}: {error}"})


# ═══════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.getenv("SENTRIX_LEGACY_CLIP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("SENTRIX_LEGACY_CLIP_PORT", "8103")))
    args = parser.parse_args()
    _load()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(
        f"legacy chinese-clip sidecar ready on {args.host}:{args.port} "
        f"backend={_backend} device={_device} dim={DIMENSION} error={_error!r}",
        flush=True,
    )
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()


if __name__ == "__main__":
    main()
