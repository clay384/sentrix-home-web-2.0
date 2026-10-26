"""Portable embedding-space identities; no model imports, network or global state."""

from __future__ import annotations

import ast
import hashlib
import importlib.metadata
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

Json = dict[str, Any]


def canonical_sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def runtime_versions(names: Sequence[str]) -> Json:
    versions: Json = {}
    for name in names:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def file_identity(path: Path) -> Json:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return {"bytes": size, "sha256": digest.hexdigest()}


def implementation_sha(path: Path, names: Sequence[str]) -> str:
    """Hash only inference/preprocessing ASTs, independent of paths and health code."""
    tree = ast.parse(path.read_text())
    selected = {}
    for name in names:
        body = tree.body
        node: ast.AST | None = None
        for part in name.split("."):
            node = next(
                (
                    value
                    for value in body
                    if isinstance(value, (ast.FunctionDef, ast.ClassDef)) and value.name == part
                ),
                None,
            )
            if node is None:
                raise ValueError("embedding implementation function missing")
            body = node.body
        if node is None:
            raise ValueError("empty embedding implementation function")
        selected[name] = ast.dump(node, include_attributes=False)
    return canonical_sha(selected)


def _seal(body: Json) -> Json:
    return {**body, "semantic_sha256": canonical_sha(body)}


def bge_identity(root: Path, implementation: str, *, max_length: int = 512) -> Json:
    if max_length < 1:
        raise ValueError("BGE max_length must be positive")
    # Match AutoModel's default safetensors preference, including sharded weights.
    if (root / "model.safetensors").is_file():
        names = ["model.safetensors"]
    elif (root / "model.safetensors.index.json").is_file():
        names = ["model.safetensors.index.json"]
    elif (root / "pytorch_model.bin").is_file():
        names = ["pytorch_model.bin"]
    elif (root / "pytorch_model.bin.index.json").is_file():
        names = ["pytorch_model.bin.index.json"]
    else:
        raise ValueError("BGE loaded weight files cannot be identified")
    if names[0].endswith(".index.json"):
        index = json.loads((root / names[0]).read_text())
        shards = set(index["weight_map"].values())
        if not shards or any(
            not isinstance(name, str) or Path(name).is_absolute() or ".." in Path(name).parts
            for name in shards
        ):
            raise ValueError("invalid BGE weight shard mapping")
        names.extend(sorted(shards))
    tokenizer_names = [
        name
        for name in (
            "tokenizer.json",
            "tokenizer_config.json",
            "special_tokens_map.json",
            "sentencepiece.bpe.model",
            "added_tokens.json",
            "vocab.json",
            "merges.txt",
            "vocab.txt",
        )
        if (root / name).is_file()
    ]
    if not {"tokenizer.json", "sentencepiece.bpe.model"}.intersection(tokenizer_names):
        raise ValueError("BGE tokenizer artifacts are missing")
    return _seal(
        {
            "identity_version": 1,
            "kind": "text",
            "model": "BAAI/bge-m3",
            "dimension": 1024,
            "weights": {name: file_identity(root / name) for name in names},
            "tokenizer": {name: file_identity(root / name) for name in tokenizer_names},
            "config": {"config.json": file_identity(root / "config.json")},
            "algorithm": {
                "revision": "sentrix-bge-attention-mask-mean-l2-v1",
                "implementation_sha256": implementation,
                "pooling": "attention_mask_mean",
                "normalization": "l2",
                "max_length": max_length,
                "truncation": True,
                "text_input": "strip",
                "model_dtype": "float32",
            },
        }
    )


def clip_identity(
    checkpoint: Path, model_name: str, package_root: Path, spec: Mapping[str, Any], implementation: str
) -> Json:
    """spec comes from the actual loaded cn_clip _MODELS/_MODEL_INFO mapping."""
    filename = str(spec["checkpoint_filename"])
    if Path(filename).name != filename:
        raise ValueError("invalid CLIP checkpoint mapping")
    actual = checkpoint.parent / filename
    vision, text = str(spec["struct"]).split("@")
    configuration = {
        f"{name.replace('/', '-')}.json": file_identity(
            package_root / "clip/model_configs" / f"{name.replace('/', '-')}.json"
        )
        for name in (vision, text)
    }
    dependencies = {
        name: file_identity(package_root / "clip" / name)
        for name in (
            "__init__.py",
            "utils.py",
            "model.py",
            "modeling_bert.py",
            "configuration_bert.py",
            "bert_tokenizer.py",
        )
    }
    return _seal(
        {
            "identity_version": 1,
            "kind": "visual",
            "model": model_name,
            "dimension": 768,
            "weights": {"checkpoint": file_identity(actual)},
            "tokenizer": {"vocab.txt": file_identity(package_root / "clip/vocab.txt")},
            "config": configuration,
            "algorithm": {
                "revision": "sentrix-cnclip-encode-image-text-l2-v1",
                "implementation_sha256": implementation,
                "dependency_files": dependencies,
                "pooling": "cnclip_encode_image_text",
                "normalization": "l2",
                "text_context_length": 52,
                "image_preprocessing": {
                    "resize": [spec["input_resolution"], spec["input_resolution"]],
                    "interpolation": "bicubic",
                    "color": "RGB",
                    "center_crop": False,
                    "mean": [0.48145466, 0.4578275, 0.40821073],
                    "std": [0.26862954, 0.26130258, 0.27577711],
                },
            },
        }
    )


def validate_space(space: Any) -> None:
    if not isinstance(space, dict) or space.get("identity_version") != 1 or space.get("scheme") != "legacy":
        raise ValueError(
            "a semantic legacy embedding identity is required; connection-only identities are unverified"
        )
    for role, dimension in (("text", 1024), ("visual", 768)):
        component = space.get(role)
        if (
            not isinstance(component, dict)
            or component.get("identity_version") != 1
            or component.get("kind") != role
        ):
            raise ValueError("missing embedding component identity")
        if component.get("dimension") != dimension or not component.get("model"):
            raise ValueError("invalid embedding model or dimension")
        if not all(
            isinstance(component.get(key), dict) and component[key]
            for key in ("weights", "tokenizer", "config", "algorithm")
        ):
            raise ValueError("incomplete embedding artifact or algorithm identity")
        if component.get("semantic_sha256") != canonical_sha(
            {key: value for key, value in component.items() if key != "semantic_sha256"}
        ):
            raise ValueError("embedding semantic identity checksum mismatch")


def validate_health(space: Any, health: Mapping[str, Any]) -> None:
    validate_space(space)
    for role in ("text", "visual"):
        observed = health.get(role)
        expected = space[role]
        if not isinstance(observed, dict) or observed.get("status") != "ok":
            raise ValueError("embedding endpoint is unavailable")
        if observed.get("embedding_identity") != expected:
            raise ValueError("runtime embedding semantic identity is missing or incompatible")
        if observed.get("model") != expected["model"] or observed.get("dimension") != expected["dimension"]:
            raise ValueError("embedding health model/dimension contradicts its identity")
