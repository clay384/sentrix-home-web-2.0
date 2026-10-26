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
    (data / "media").mkdir()
    (data / "media/one.jpg").write_bytes(b"photo-one")
    (data / "media/two.jpg").write_bytes(b"photo-two")
    with sqlite3.connect(data / "sentrix.db") as db:
        db.execute("CREATE TABLE schema_migrations(version TEXT)")
        db.execute("INSERT INTO schema_migrations VALUES ('0001')")
        db.execute("CREATE TABLE assets(id TEXT PRIMARY KEY, path TEXT, metadata TEXT)")
        db.executemany(
            "INSERT INTO assets VALUES (?, ?, ?)",
            [
                ("one", str(data / "media/one.jpg"), str(data / "media/one.jpg")),
                ("two", str(data / "media/two.jpg"), "preserve-metadata"),
            ],
        )
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


def test_restore_relocates_only_media_paths_and_survives_source_removal(tmp_path, memory):
    data, pid, identity = memory
    frozen = tmp_path / "frozen"
    manifest = snapshot.create(data, frozen, identity, pid)
    original = (frozen / "manifest.json").read_bytes()
    first, second = tmp_path / "first", tmp_path / "second"
    snapshot.restore(frozen, first, identity)
    custom_receipt = tmp_path / "evidence/custom.json"
    snapshot.restore(frozen, second, identity, custom_receipt)
    assert manifest["source_root"] == str(data)
    data.rename(tmp_path / "source-removed")
    for target, receipt_path in ((first, None), (second, custom_receipt)):
        receipt = snapshot.verify_restored(frozen, target, identity, receipt_path)
        assert receipt["relocated_assets"] == 2
        assert receipt["manifest_sha256"] == snapshot.digest(frozen / "manifest.json")
        assert receipt["post_restore_database_sha256"] == snapshot.digest(target / "sentrix.db")
        assert receipt["from_root"] == str(data)
        assert receipt["to_root"] == str(target)
        with sqlite3.connect(target / "sentrix.db") as db:
            rows = db.execute("SELECT id,path,metadata FROM assets ORDER BY id").fetchall()
            assert [Path(row[1]).read_bytes() for row in rows] == [b"photo-one", b"photo-two"]
            assert rows[0][2] == str(data / "media/one.jpg")  # Not arbitrary text replacement.
            assert all(Path(row[1]).parent == target / "media" for row in rows)
            assert db.execute("SELECT * FROM schema_migrations").fetchall() == [("0001",)]
    assert (first / "sentrix.db").stat().st_ino != (second / "sentrix.db").stat().st_ino
    assert snapshot.digest(first / "sentrix.db") != snapshot.digest(second / "sentrix.db")
    assert (frozen / "manifest.json").read_bytes() == original
    snapshot.verify(frozen, identity)


@pytest.mark.parametrize(
    "bad", ["external", "prefix-lookalike", "traversal", "missing", "relative", "database"]
)
def test_invalid_asset_paths_reject_snapshot_and_restore(tmp_path, memory, bad):
    data, pid, identity = memory
    frozen = tmp_path / "frozen"
    snapshot.create(data, frozen, identity, pid)
    paths = {
        "external": str(tmp_path / "elsewhere/image.jpg"),
        "prefix-lookalike": str(data) + "-other/media/one.jpg",
        "traversal": str(data) + "/media/../media/one.jpg",
        "missing": str(data / "media/missing.jpg"),
        "relative": "media/one.jpg",
        "database": str(data / "sentrix.db"),
    }
    # Even a recomputed outer file inventory must not bypass path provenance.
    for root in (data, frozen / "data"):
        with sqlite3.connect(root / "sentrix.db") as db:
            db.execute("UPDATE assets SET path=? WHERE id='one'", (paths[bad],))
    manifest = json.loads((frozen / "manifest.json").read_text())
    manifest["files"] = snapshot.inventory(frozen / "data")
    (frozen / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="asset path|media file"):
        snapshot.create(data, tmp_path / "new", identity, pid)
    with pytest.raises(ValueError, match="asset path|media file"):
        snapshot.restore(frozen, tmp_path / "run", identity)
    assert not (tmp_path / "run").exists()
    assert not snapshot.receipt_path_for(tmp_path / "run").exists()


def test_legacy_snapshot_can_verify_but_cannot_guess_a_source_root(tmp_path, memory):
    data, pid, identity = memory
    frozen = tmp_path / "frozen"
    manifest = snapshot.create(data, frozen, identity, pid)
    manifest["schema_version"] = 1
    manifest.pop("source_root")
    (frozen / "manifest.json").write_text(json.dumps(manifest))
    assert snapshot.verify(frozen, identity)["schema_version"] == 1
    with pytest.raises(ValueError, match="recreate"):
        snapshot.restore(frozen, tmp_path / "run", identity)


def test_restore_receipt_is_bound_to_manifest_destination_and_sqlite(tmp_path, memory):
    data, pid, identity = memory
    frozen, target = tmp_path / "frozen", tmp_path / "target"
    manifest = snapshot.create(data, frozen, identity, pid)
    snapshot.restore(frozen, target, identity)
    receipt = snapshot.verify_restored(frozen, target, identity)
    sha = snapshot.digest(frozen / "manifest.json")
    snapshot.validate_restore_receipt(receipt, manifest, target, sha)
    with pytest.raises(ValueError, match="receipt"):
        snapshot.validate_restore_receipt(receipt, manifest, tmp_path / "other", sha)
    with pytest.raises(ValueError, match="receipt"):
        snapshot.validate_restore_receipt(receipt, manifest, target, "0" * 64)
    with sqlite3.connect(target / "sentrix.db") as db:
        db.execute("UPDATE assets SET metadata='tampered'")
    with pytest.raises(ValueError, match="receipt"):
        snapshot.verify_restored(frozen, target, identity)


def test_receipt_cannot_overwrite_source_or_existing_evidence(tmp_path, memory):
    data, pid, identity = memory
    frozen = tmp_path / "frozen"
    snapshot.create(data, frozen, identity, pid)
    for receipt in (frozen / "new.json", data / "new.json", tmp_path / "run/new.json"):
        with pytest.raises(ValueError, match="outside"):
            snapshot.restore(frozen, tmp_path / "run", identity, receipt)
    existing = tmp_path / "previous.json"
    existing.write_text("retain")
    with pytest.raises(ValueError, match="already exists"):
        snapshot.restore(frozen, tmp_path / "run", identity, existing)
    assert existing.read_text() == "retain"


def test_wal_snapshot_restores_committed_rows_into_independent_database(tmp_path, memory):
    data, pid, identity = memory
    with sqlite3.connect(data / "sentrix.db") as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE assets SET metadata='committed-in-wal' WHERE id='two'")
        writer.commit()
        frozen = tmp_path / "frozen"
        snapshot.create(data, frozen, identity, pid)
        original_files = snapshot.inventory(frozen / "data")
        target = tmp_path / "run"
        snapshot.restore(frozen, target, identity)
        snapshot.verify_restored(frozen, target, identity)
        snapshot.verify(frozen, identity)
        assert snapshot.inventory(frozen / "data") == original_files
        with sqlite3.connect(target / "sentrix.db") as db:
            assert db.execute("SELECT metadata FROM assets WHERE id='two'").fetchone() == (
                "committed-in-wal",
            )


def test_snapshot_mutation_during_restore_is_rejected_and_cleans_output(tmp_path, memory):
    data, pid, identity = memory
    frozen = tmp_path / "frozen"
    snapshot.create(data, frozen, identity, pid)
    original = snapshot.shutil.copytree

    def mutate(src, dst, *args, **kwargs):
        result = original(src, dst, *args, **kwargs)
        if Path(src) == frozen / "data":
            (frozen / "data/media/one.jpg").write_bytes(b"changed-during-copy")
        return result

    with (
        patch.object(snapshot.shutil, "copytree", side_effect=mutate),
        pytest.raises(ValueError, match="changed"),
    ):
        snapshot.restore(frozen, tmp_path / "run", identity)
    assert not (tmp_path / "run").exists()
    assert not snapshot.receipt_path_for(tmp_path / "run").exists()
    assert not list(tmp_path.glob(".restore-*"))


def test_portable_version_never_guesses_missing_provenance(tmp_path, memory):
    data, pid, identity = memory
    frozen = tmp_path / "frozen"
    manifest = snapshot.create(data, frozen, identity, pid)
    manifest.pop("source_root")
    (frozen / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="source_root"):
        snapshot.verify(frozen, identity)


def test_restore_cli_publishes_and_offline_verifies_receipt(tmp_path, memory):
    import subprocess
    import sys

    data, pid, identity = memory
    frozen = tmp_path / "frozen"
    snapshot.create(data, frozen, identity, pid)
    identity_path = tmp_path / "identity.json"
    identity_path.write_text(json.dumps(identity))
    command = [sys.executable, str(ROOT / "scripts/benchmarks/memory_snapshot.py")]
    options = [
        "--source",
        str(frozen),
        "--destination",
        str(tmp_path / "run"),
        "--identity",
        str(identity_path),
    ]
    restored = subprocess.run([*command, "restore", *options], check=True, capture_output=True, text=True)
    result = json.loads(restored.stdout)
    assert result["portable"] is True
    assert Path(result["receipt"]).is_file()
    verified = subprocess.run(
        [*command, "verify-restored", *options], check=True, capture_output=True, text=True
    )
    assert json.loads(verified.stdout)["status"] == "verified"
