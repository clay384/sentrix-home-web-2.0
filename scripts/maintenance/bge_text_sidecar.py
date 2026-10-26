"""BGE-M3 text embedding sidecar for the legacy Mac deployment.

The API process deliberately does not import torch.  This small process owns the
model and exposes the historical contract used by ``BgeSidecarEmbedder``:
``GET /health`` and ``POST /embed {"text": "..."}``, returning a 1024d vector.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL_PATH = REPO_ROOT / "data" / "models" / "bge-m3"
MODEL_ID = "BAAI/bge-m3"
DIMENSION = 1024

_model = None
_tokenizer = None
_device = "cpu"
_error = ""
_lock = threading.Lock()


def _model_path() -> Path:
    return Path(os.getenv("BGE_MODEL_PATH", str(DEFAULT_MODEL_PATH))).expanduser().resolve()


def _load() -> None:
    global _model, _tokenizer, _error
    try:
        import torch
        from transformers import AutoModel, AutoTokenizer

        threads = int(os.getenv("SENTRIX_BGE_THREADS", "6"))
        if threads > 0:
            torch.set_num_threads(threads)
        path = _model_path()
        _tokenizer = AutoTokenizer.from_pretrained(  # type: ignore[no-untyped-call]
            str(path), local_files_only=True
        )
        _model = AutoModel.from_pretrained(
            str(path),
            local_files_only=True,
            torch_dtype=torch.float32,
        )
        _model.eval()
        _error = ""
    except Exception as error:  # noqa: BLE001 - health endpoint reports startup failure
        _error = f"{type(error).__name__}: {error}"


def _embed(text: str) -> list[float]:
    import torch

    if _model is None or _tokenizer is None:
        raise RuntimeError(_error or "model not loaded")
    max_length = int(os.getenv("SENTRIX_BGE_MAX_LENGTH", "512"))
    inputs = _tokenizer(
        [text],
        padding=True,
        truncation=True,
        max_length=max_length,
        return_tensors="pt",
    )
    with _lock, torch.inference_mode():
        outputs = _model(**inputs)
        hidden = outputs.last_hidden_state
        mask = inputs["attention_mask"].unsqueeze(-1).to(hidden.dtype)
        vector = (hidden * mask).sum(dim=1) / mask.sum(dim=1).clamp_min(1e-9)
        vector = torch.nn.functional.normalize(vector, p=2, dim=1)
    values = [float(value) for value in vector[0].cpu().tolist()]
    if len(values) != DIMENSION:
        raise RuntimeError(f"model returned {len(values)} dimensions; expected {DIMENSION}")
    return values


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
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
        return cast(dict[str, object], json.loads(self.rfile.read(length) or b"{}"))

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        if self.path == "/health":
            ok = _model is not None and _tokenizer is not None
            self._send(
                200 if ok else 503,
                {
                    "status": "ok" if ok else "error",
                    "model": MODEL_ID,
                    "dimension": DIMENSION,
                    "device": _device,
                    "error": _error,
                },
            )
        elif self.path == "/stats":
            self._send(
                200,
                {
                    "model": MODEL_ID,
                    "dimension": DIMENSION,
                    "device": _device,
                    "loaded": _model is not None and _tokenizer is not None,
                    "error": _error,
                },
            )
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        try:
            body = self._body()
            if self.path != "/embed":
                self._send(404, {"error": "not found"})
                return
            text = str(body.get("text") or "").strip()
            if not text:
                self._send(400, {"error": "text 不能为空"})
                return
            vector = _embed(text)
            self._send(200, {"vector": vector, "dimension": len(vector)})
        except Exception as error:  # noqa: BLE001 - adapter expects HTTP failure semantics
            self._send(500, {"error": f"{type(error).__name__}: {error}"})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.getenv("SENTRIX_BGE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("SENTRIX_BGE_PORT", "8101")))
    args = parser.parse_args()
    _load()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(
        f"BGE-M3 sidecar ready on {args.host}:{args.port} dim={DIMENSION} error={_error!r}",
        flush=True,
    )
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()


if __name__ == "__main__":
    main()
