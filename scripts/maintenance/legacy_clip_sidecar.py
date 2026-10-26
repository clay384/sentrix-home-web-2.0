"""Chinese-CLIP（ViT-L-14，768 维）视觉嵌入侧车，端口 8103。

legacy 嵌入方案的视觉半边。文本半边仍走 1.0 的 bge-m3 侧车（8101）。

**为什么是侧车，不是进程内。** 与 WeMM 同一条设计约束：2.0 的 API 进程不 import
torch（设计 §2.1）。2.0 自己也不管理模型生命周期——这个脚本由部署方拉起，等价于
1.0 的 ``text_embedder_sidecar.py``。

模型与依赖来自 1.0 环境：

- 权重：``~/.cache/clip/clip_cn_vit-l-14.pt``
- 依赖：``cn_clip`` + ``torch``（1.0 的 ``.venv`` 里已装；130 Mac mini 上已验证）

用法::

    .venv/bin/python scripts/maintenance/legacy_clip_sidecar.py --port 8103

设备默认自动选择：CUDA → MPS → CPU。Mac mini 上是 MPS；侧车启动时会打印实际设备。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.maintenance.embedding_identity import clip_identity, implementation_sha, runtime_versions  # noqa: E402

DIMENSION = 768
DEFAULT_CHECKPOINT = Path.home() / ".cache" / "clip" / "clip_cn_vit-l-14.pt"

_model = None
_preprocess = None
_device = "cpu"
_error = ""
_lock = threading.Lock()
_identity: dict[str, object] | None = None
_identity_error = ""
_identity_runtime: dict[str, object] = {}


def _pick_device() -> str:
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
        implementation_sha(Path(__file__), ["_normalize", "_embed_image", "_embed_query", "Handler.do_POST"]),
    )


def _load() -> None:
    """启动时加载一次。失败保留 ``_error``，``/health`` 报 error，进程不退出。"""
    global _model, _preprocess, _device, _error, _identity, _identity_error, _identity_runtime
    checkpoint = Path(os.getenv("CHINESE_CLIP_CHECKPOINT", str(DEFAULT_CHECKPOINT))).expanduser()
    model_name = os.getenv("CHINESE_CLIP_MODEL", "ViT-L-14")
    _device = _pick_device()
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
        _model = model
        _preprocess = preprocess
        _error = ""
        _identity_runtime = {
            "device": _device,
            "threads": torch.get_num_threads(),
            "parameter_dtypes": sorted({str(p.dtype) for p in _model.parameters()}),
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


def _normalize(values: list[float]) -> list[float]:
    norm = sum(value * value for value in values) ** 0.5
    if norm <= 0:
        return values
    return [value / norm for value in values]


def _embed_image(path: str) -> list[float]:
    import torch
    from PIL import Image

    if _model is None or _preprocess is None:
        raise RuntimeError(_error or "model not loaded")
    if not Path(path).is_file():
        raise FileNotFoundError(path)
    image = _preprocess(Image.open(path).convert("RGB")).unsqueeze(0).to(_device)
    with _lock, torch.no_grad():
        vector = _model.encode_image(image)
    return _normalize([float(value) for value in vector[0].cpu().tolist()])


def _embed_query(text: str) -> list[float]:
    import torch
    from cn_clip.clip import tokenize

    if _model is None:
        raise RuntimeError(_error or "model not loaded")
    tokens = tokenize([str(text)]).to(_device)
    with _lock, torch.no_grad():
        vector = _model.encode_text(tokens)
    return _normalize([float(value) for value in vector[0].cpu().tolist()])


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

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定
        if self.path == "/health":
            ok = _model is not None
            self._send(
                200 if ok else 503,
                {
                    "status": "ok" if ok else "error",
                    "model": os.getenv("CHINESE_CLIP_MODEL", "ViT-L-14"),
                    "dimension": DIMENSION,
                    "device": _device,
                    "error": _error,
                    "embedding_identity": _identity,
                    "embedding_identity_error": _identity_error,
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
                    "device": _device,
                    "dimension": DIMENSION,
                    "loaded": _model is not None,
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
                text = str(body.get("text") or "")
                if not text.strip():
                    self._send(400, {"error": "text 不能为空"})
                    return
                vector = _embed_query(text)
            else:
                self._send(404, {"error": "not found"})
                return
            self._send(200, {"vector": vector, "dimension": len(vector)})
        except Exception as error:  # noqa: BLE001 - 统一转成 HTTP 500
            self._send(500, {"error": f"{type(error).__name__}: {error}"})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.getenv("SENTRIX_LEGACY_CLIP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("SENTRIX_LEGACY_CLIP_PORT", "8103")))
    args = parser.parse_args()
    _load()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(
        f"legacy chinese-clip sidecar ready on {args.host}:{args.port} "
        f"device={_device} dim={DIMENSION} error={_error!r}",
        flush=True,
    )
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()


if __name__ == "__main__":
    main()
