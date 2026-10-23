"""Canonical visual wire encoding and optional frozen-source provenance.

The manifest is a deployment input, never a question-specific candidate list.
Derived images are restricted to the existing crop and numbered-preview renderers.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import threading
from pathlib import Path
from typing import Any

_lock = threading.RLock()
_sources: dict[str, list[dict[str, str]]] | None = None
_derived: dict[str, dict[str, Any]] = {}
_encoded: dict[str, list[dict[str, Any]]] = {}


class _EncodedVisual(str):
    proof: dict[str, Any]

    def __new__(cls, value: str, proof: dict[str, Any] | None = None) -> _EncodedVisual:
        instance = super().__new__(cls, value)
        instance.proof = dict(proof or {})
        return instance


def _source_for_path(path: Path, sha: str) -> dict[str, str] | None:
    entries = (_sources or {}).get(sha, [])
    if len(entries) == 1:
        return entries[0]
    matches = [
        entry
        for entry in entries
        if (
            str(path.resolve()) == entry["path"]
            or path.name == entry["file_name"]
            or path.name.endswith("_" + entry["file_name"])
        )
    ]
    if len(matches) == 1:
        return matches[0]
    if entries:
        raise ValueError("Ambiguous visual source needs its original filename or storage alias")
    return None


def canonical_image_bytes(raw: bytes) -> bytes:
    """EXIF-correct RGB, at most 512px without upscaling, JPEG quality 90."""
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(raw)) as original:
        image = ImageOps.exif_transpose(original).convert("RGB")
        image.thumbnail((512, 512))
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=90)
    return output.getvalue()


def configure_visual_provenance(manifest: Path | None) -> None:
    """Enable strict source binding for this process; ordinary deployments opt out."""
    global _sources
    with _lock:
        _derived.clear()
        _encoded.clear()
        if manifest is None:
            _sources = None
            return
        rows = json.loads(manifest.read_text(encoding="utf-8"))
        if isinstance(rows, list):
            rows = {row["record_id"]: row for row in rows}
        if not isinstance(rows, dict) or not rows:
            raise ValueError("Visual source manifest must be a nonempty mapping")
        sources: dict[str, list[dict[str, str]]] = {}
        for identity, row in rows.items():
            if not isinstance(row, dict):
                raise ValueError("Visual source manifest rows must be objects")
            sha = row.get("sha256", row.get("source_hash"))
            if not isinstance(identity, str) or not isinstance(sha, str) or len(sha) != 64:
                raise ValueError("Visual source manifest needs source IDs and SHA256")
            raw_path = row.get("path", row.get("asset_path"))
            if not isinstance(raw_path, str) or not raw_path:
                raise ValueError("Visual source manifest needs original file paths")
            path = Path(raw_path)
            sources.setdefault(sha, []).append(
                {
                    "source_id": identity,
                    "source_sha256": sha,
                    "path": str(path.resolve()),
                    "file_name": path.name,
                }
            )
        _sources = sources


def register_visual_derivation(
    path: str | Path, parent: str | Path, operation: str, parameters: dict[str, Any]
) -> None:
    """Bind a trusted renderer's result to a frozen original, never an arbitrary path."""
    with _lock:
        if _sources is None:
            return
        parent_sha = hashlib.sha256(Path(parent).read_bytes()).hexdigest()
        source = _source_for_path(Path(parent), parent_sha)
        if source is None or operation not in {"crop", "numbered_preview"}:
            raise ValueError("Derived visual input lacks a frozen original")
        target = Path(path).resolve(strict=True)
        _derived[str(target)] = {
            **source,
            "operation": operation,
            "parameters": parameters,
            "derived_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        }


def encode_visual_image(path: Path) -> str:
    raw = path.read_bytes()
    with _lock:
        if _sources is None:
            mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
            return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")
    sha = hashlib.sha256(raw).hexdigest()
    encoded = canonical_image_bytes(raw)
    encoded_sha = hashlib.sha256(encoded).hexdigest()
    with _lock:
        if _sources is None:
            raise ValueError("Visual provenance configuration changed during encoding")
        if _sources is not None:
            source = _source_for_path(path, sha)
            proof: dict[str, Any]
            if source is not None:
                proof = {**source, "operation": "original", "parameters": {}}
            else:
                receipt = _derived.get(str(path.resolve(strict=True)))
                if receipt is None or receipt["derived_sha256"] != sha:
                    raise ValueError("Visual image is outside the frozen source manifest")
                proof = dict(receipt)
            proof = {**proof, "encoded_sha256": encoded_sha, "policy": "exif-RGB-max512-no-upscale-JPEG90-v1"}
            # Paths help select a stored source locally but never leave this process.
            proof.pop("path", None)
            proof.pop("file_name", None)
            entries = _encoded.setdefault(encoded_sha, [])
            if proof not in entries:
                entries.append(proof)
    return _EncodedVisual("data:image/jpeg;base64," + base64.b64encode(encoded).decode("ascii"), proof)


def image_provenance(urls: list[str]) -> list[dict[str, Any]] | None:
    with _lock:
        if _sources is None:
            return None
        result = []
        for url in urls:
            raw = base64.b64decode(url.split(",", 1)[1], validate=True)
            sha = hashlib.sha256(raw).hexdigest()
            proof = getattr(url, "proof", None)
            if proof is None:
                entries = _encoded.get(sha, [])
                if len(entries) != 1:
                    raise ValueError("Encoded visual image has missing or ambiguous source provenance")
                proof = entries[0]
            if proof.get("encoded_sha256") != sha:
                raise ValueError("Encoded visual image provenance hash mismatch")
            result.append(dict(proof))
        return result
