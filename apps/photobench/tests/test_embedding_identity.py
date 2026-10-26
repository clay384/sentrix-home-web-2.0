"""Semantic identity checks never load torch or call a real model endpoint."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts/maintenance" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Standalone sidecars bootstrap their script imports. Keep that local to loading
    # these test subjects; pytest-console must not depend on an ambient repo-root path.
    previous_path = sys.path[:]
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = previous_path
    return module


identity = _load_script("embedding_identity")
bge_text_sidecar = _load_script("bge_text_sidecar")
legacy_clip_sidecar = _load_script("legacy_clip_sidecar")


@pytest.fixture
def artifacts(tmp_path):
    bge = tmp_path / "bge"
    bge.mkdir()
    for name, data in (
        ("pytorch_model.bin", b"weights"),
        ("config.json", b'{"hidden_size":1024}'),
        ("tokenizer.json", b"tokens"),
        ("tokenizer_config.json", b"token-config"),
    ):
        (bge / name).write_bytes(data)
    package = tmp_path / "cn_clip"
    configs = package / "clip/model_configs"
    configs.mkdir(parents=True)
    for name in ("ViT-L-14", "RoBERTa-wwm-ext-base-chinese"):
        (configs / (name + ".json")).write_text('{"embed_dim":768}')
    for name in (
        "__init__.py",
        "utils.py",
        "model.py",
        "modeling_bert.py",
        "configuration_bert.py",
        "bert_tokenizer.py",
        "vocab.txt",
    ):
        (package / "clip" / name).write_text(name)
    checkpoint = tmp_path / "weights/clip_cn_vit-l-14.pt"
    checkpoint.parent.mkdir()
    checkpoint.write_bytes(b"actual-clip-weights")
    spec = {
        "checkpoint_filename": checkpoint.name,
        "struct": "ViT-L-14@RoBERTa-wwm-ext-base-chinese",
        "input_resolution": 224,
    }
    return bge, package, checkpoint, spec


def space_from(artifacts):
    bge, package, checkpoint, spec = artifacts
    return {
        "identity_version": 1,
        "scheme": "legacy",
        "text": identity.bge_identity(bge, "a" * 64),
        "visual": identity.clip_identity(checkpoint, "ViT-L-14", package, spec, "b" * 64),
    }


def health_from(space):
    return {
        role: {
            "status": "ok",
            "model": space[role]["model"],
            "dimension": space[role]["dimension"],
            "embedding_identity": space[role],
            "embedding_runtime": {"device": "cpu", "threads": 6},
        }
        for role in ("text", "visual")
    }


def test_same_artifacts_at_different_directories_are_identical(tmp_path, artifacts):
    expected = space_from(artifacts)
    relocated = tmp_path / "relocated"
    relocated.mkdir()
    bge, package, checkpoint, spec = artifacts
    shutil.copytree(bge, relocated / "bge")
    shutil.copytree(package, relocated / "cn_clip")
    shutil.copytree(checkpoint.parent, relocated / "weights")
    observed = space_from(
        (relocated / "bge", relocated / "cn_clip", relocated / "weights" / checkpoint.name, spec)
    )
    assert observed == expected
    health = health_from(observed)
    health["visual"]["embedding_runtime"] = {"device": "mps", "threads": 2, "listen_port": 18103}
    identity.validate_health(expected, health)
    assert str(tmp_path) not in json.dumps(expected)


@pytest.mark.parametrize("change", ["weights", "tokenizer", "config", "pooling", "dimension", "preprocess"])
def test_changed_vector_space_is_rejected(artifacts, change):
    expected = space_from(artifacts)
    actual = json.loads(json.dumps(expected))
    target = actual["text"]
    if change in {"weights", "tokenizer", "config"}:
        target[change] = {"changed": {"sha256": "0" * 64, "bytes": 1}}
    elif change == "pooling":
        target["algorithm"]["pooling"] = "cls"
    elif change == "dimension":
        target["dimension"] = 512
    else:
        target = actual["visual"]
        target["algorithm"]["image_preprocessing"]["center_crop"] = True
    target["semantic_sha256"] = identity.canonical_sha(
        {key: value for key, value in target.items() if key != "semantic_sha256"}
    )
    with pytest.raises(ValueError, match="incompatible"):
        identity.validate_health(expected, health_from(actual))


def test_clip_hashes_actual_mapped_checkpoint_not_environment_basename(artifacts):
    bge, package, checkpoint, spec = artifacts
    decoy = checkpoint.with_name("custom-checkpoint.pt")
    decoy.write_bytes(b"not-actually-loaded")
    actual = identity.clip_identity(decoy, "ViT-L-14", package, spec, "b" * 64)
    assert actual["weights"]["checkpoint"] == identity.file_identity(checkpoint)
    assert actual["weights"]["checkpoint"] != identity.file_identity(decoy)


def test_missing_identity_and_health_dimension_mismatch_are_rejected(artifacts):
    space = space_from(artifacts)
    health = health_from(space)
    health["text"].pop("embedding_identity")
    with pytest.raises(ValueError, match="missing or incompatible"):
        identity.validate_health(space, health)
    health = health_from(space)
    health["visual"]["dimension"] = 1024
    with pytest.raises(ValueError, match="contradicts"):
        identity.validate_health(space, health)


@pytest.mark.parametrize("module,role", [(bge_text_sidecar, "text"), (legacy_clip_sidecar, "visual")])
def test_health_is_additive_and_uses_cached_identity_without_rehashing(artifacts, monkeypatch, module, role):
    space = space_from(artifacts)
    monkeypatch.setattr(module, "_model", object())
    if module is bge_text_sidecar:
        monkeypatch.setattr(module, "_tokenizer", object())
    monkeypatch.setattr(module, "_identity", space[role])
    monkeypatch.setattr(module, "_identity_runtime", {"device": "cpu", "threads": 6})
    monkeypatch.setattr(module, "_current_identity", lambda *a: pytest.fail("health must not hash artifacts"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/health"
        for _ in range(2):
            with urllib.request.urlopen(url, timeout=2) as response:
                health = json.load(response)
            assert health["status"] == "ok"
            assert health["embedding_identity"] == space[role]
            assert health["embedding_runtime"]["listen_port"] == server.server_port
            assert "dimension" in health and "device" in health and "error" in health
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_implementation_digest_ignores_directory_health_and_line_numbers(tmp_path):
    one = tmp_path / "one.py"
    two = tmp_path / "two.py"
    one.write_text("def embed(x):\n return x * 2\ndef health():\n return 'old'\n")
    two.write_text("\n\ndef embed(x):\n return x * 2\ndef health():\n return 'new'\n")
    assert identity.implementation_sha(one, ["embed"]) == identity.implementation_sha(two, ["embed"])
    two.write_text("def embed(x):\n return x * 3\n")
    assert identity.implementation_sha(one, ["embed"]) != identity.implementation_sha(two, ["embed"])


def test_bge_truncation_and_sharded_weights_are_part_of_identity(artifacts):
    bge, *_ = artifacts
    first = identity.bge_identity(bge, "a" * 64)
    assert identity.bge_identity(bge, "a" * 64, max_length=1024) != first
    (bge / "model.safetensors.index.json").write_text(
        json.dumps({"weight_map": {"layer": "shard.safetensors"}})
    )
    (bge / "shard.safetensors").write_bytes(b"actual-safetensors-preferred")
    second = identity.bge_identity(bge, "a" * 64)
    assert "shard.safetensors" in second["weights"]
    assert "pytorch_model.bin" not in second["weights"]


@pytest.mark.parametrize("filename", ["pytorch_model.bin", "tokenizer.json", "config.json"])
def test_real_artifact_byte_change_invalidates_space(artifacts, filename):
    expected = space_from(artifacts)
    bge, *_ = artifacts
    (bge / filename).write_bytes(b"different-content")
    actual = space_from(artifacts)
    assert expected["text"]["semantic_sha256"] != actual["text"]["semantic_sha256"]
    with pytest.raises(ValueError, match="incompatible"):
        identity.validate_health(expected, health_from(actual))


@pytest.mark.parametrize("changed", [False, True])
def test_startup_caches_only_identity_stable_across_model_load(monkeypatch, artifacts, changed):
    import sys
    from types import SimpleNamespace

    component = space_from(artifacts)["text"]
    after = {**component, "semantic_sha256": "changed"} if changed else component
    values = iter([component, after])
    monkeypatch.setattr(bge_text_sidecar, "_current_identity", lambda: next(values))
    monkeypatch.setattr(bge_text_sidecar, "_identity", None)
    monkeypatch.setattr(bge_text_sidecar, "_identity_error", "")
    monkeypatch.setattr(bge_text_sidecar, "_identity_runtime", {})
    monkeypatch.setattr(bge_text_sidecar, "_model", None)
    monkeypatch.setattr(bge_text_sidecar, "_tokenizer", None)
    monkeypatch.setattr(bge_text_sidecar, "_error", "")
    model = SimpleNamespace(eval=lambda: None, parameters=lambda: iter([SimpleNamespace(dtype="float32")]))
    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(set_num_threads=lambda _: None, get_num_threads=lambda: 6, float32="float32"),
    )
    monkeypatch.setitem(
        sys.modules,
        "transformers",
        SimpleNamespace(
            AutoModel=SimpleNamespace(from_pretrained=lambda *a, **kw: model),
            AutoTokenizer=SimpleNamespace(from_pretrained=lambda *a, **kw: object()),
        ),
    )
    bge_text_sidecar._load()
    assert bge_text_sidecar._model is model
    assert bge_text_sidecar._error == ""  # Health availability/math behavior is unchanged.
    assert bge_text_sidecar._identity == (None if changed else component)
    assert bool(bge_text_sidecar._identity_error) == changed


@pytest.mark.parametrize("name", ["modeling_bert.py", "configuration_bert.py"])
def test_clip_real_cn_clip_bert_layout_is_hashed_and_required(artifacts, name):
    bge, package, checkpoint, spec = artifacts
    assert not (package / "clip/bert_model.py").exists()
    expected = space_from(artifacts)
    dependencies = expected["visual"]["algorithm"]["dependency_files"]
    assert {"modeling_bert.py", "configuration_bert.py"} <= dependencies.keys()
    source = package / "clip" / name
    source.write_text(source.read_text() + "\nchanged actual BERT implementation")
    changed = space_from(artifacts)
    with pytest.raises(ValueError, match="incompatible"):
        identity.validate_health(expected, health_from(changed))
    source.unlink()
    with pytest.raises(FileNotFoundError):
        identity.clip_identity(checkpoint, "ViT-L-14", package, spec, "b" * 64)
