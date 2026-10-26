"""Cold snapshot integrity and compatibility are enforced before any reuse."""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

import portalocker
import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location(
    "memory_snapshot", ROOT / "scripts/benchmarks/memory_snapshot.py"
)
assert spec and spec.loader
snapshot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snapshot)


@pytest.fixture
def memory(tmp_path):
    data = tmp_path / "source"
    (data / "qdrant").mkdir(parents=True)
    (data / "qdrant/.lock").touch()
    (data / "qdrant/index.bin").write_bytes(b"vectors")
    with sqlite3.connect(data / "sentrix.db") as db:
        db.execute("CREATE TABLE schema_migrations(version TEXT)")
        db.execute("INSERT INTO schema_migrations VALUES ('0001')")
    pid = tmp_path / "pid.json"
    pid.write_text(json.dumps({"pid": 2147483647}))
    identity = {
        "dataset_sha256": "dataset",
        "embedding": {"scheme": "legacy"},
        "source_revision": "revision",
        "observation_model_sha256": "weights",
        "pipeline_config": {"workers": 1},
        "scope_id": "scope",
    }
    return data, pid, identity


def test_roundtrip_is_independent_and_checks_compatibility(tmp_path, memory):
    data, pid, identity = memory
    frozen = tmp_path / "frozen"
    snapshot.create(data, frozen, identity, pid)
    run = tmp_path / "run"
    snapshot.restore(frozen, run, identity)
    assert (run / "sentrix.db").stat().st_ino != (frozen / "data/sentrix.db").stat().st_ino
    (run / "qdrant/index.bin").write_bytes(b"changed")
    snapshot.verify(frozen, identity)
    with pytest.raises(ValueError, match="incompatible"):
        snapshot.restore(frozen, tmp_path / "bad", {**identity, "embedding": {"scheme": "wemm"}})
    assert not (tmp_path / "bad").exists()
    with pytest.raises(ValueError, match="already exists"):
        snapshot.restore(frozen, run, identity)


def test_tampering_and_unlisted_files_rejected(tmp_path, memory):
    data, pid, identity = memory
    frozen = tmp_path / "frozen"
    snapshot.create(data, frozen, identity, pid)
    (frozen / "data/qdrant/index.bin").write_bytes(b"wrong")
    with pytest.raises(ValueError, match="integrity"):
        snapshot.verify(frozen, identity)


def test_running_owner_and_qdrant_lock_rejected(tmp_path, memory):
    data, pid, identity = memory
    pid.write_text(json.dumps({"pid": os.getpid()}))
    with pytest.raises(ValueError, match="stop the source"):
        snapshot.create(data, tmp_path / "frozen", identity, pid)
    pid.write_text(json.dumps({"pid": 2147483647}))
    with (data / "qdrant/.lock").open("rb+") as locked:
        portalocker.lock(locked, portalocker.LOCK_EX | portalocker.LOCK_NB)
        with pytest.raises(ValueError, match="Qdrant"):
            snapshot.create(data, tmp_path / "frozen", identity, pid)


def test_source_mutation_cleans_partial_copy(tmp_path, memory):
    data, pid, identity = memory
    original = snapshot.shutil.copytree

    def mutate(src, dst, *args, **kwargs):
        result = original(src, dst, *args, **kwargs)
        (data / "qdrant/index.bin").write_bytes(b"mutation")
        return result

    with (
        patch.object(snapshot.shutil, "copytree", side_effect=mutate),
        pytest.raises(ValueError, match="source changed"),
    ):
        snapshot.create(data, tmp_path / "frozen", identity, pid)
    assert not (tmp_path / "frozen").exists()
    assert not list(tmp_path.glob(".snapshot-*"))


def test_symlinks_and_nested_destination_rejected(tmp_path, memory):
    data, pid, identity = memory
    with pytest.raises(ValueError, match="separate"):
        snapshot.create(data, data / "snapshot", identity, pid)
    (data / "link").symlink_to(data / "sentrix.db")
    with pytest.raises(ValueError, match="symlink"):
        snapshot.create(data, tmp_path / "frozen", identity, pid)
