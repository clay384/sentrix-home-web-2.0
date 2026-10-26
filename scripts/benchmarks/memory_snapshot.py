#!/usr/bin/env python3
"""Offline, verified Sentrix memory snapshots; data stays outside Git.

Stop the owning API before create. Each restore gets its own SQLite and Qdrant
files; a source path, a running service or an incompatible identity is rejected.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import portalocker

VERSION = 1
IDENTITY_KEYS = frozenset(
    {
        "dataset_sha256",
        "embedding",
        "source_revision",
        "observation_model_sha256",
        "pipeline_config",
        "scope_id",
    }
)


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def identity_digest(identity: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_identity(identity: dict[str, Any]) -> None:
    if IDENTITY_KEYS - identity.keys():
        raise ValueError(f"missing compatibility fields: {sorted(IDENTITY_KEYS - identity.keys())}")
    if any(not identity[key] for key in IDENTITY_KEYS):
        raise ValueError("compatibility fields must not be empty")


def require_stopped(pid_file: Path) -> None:
    record = json.loads(pid_file.read_text())
    pid = record.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise ValueError("invalid service pid record")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    raise ValueError(f"stop the source API first (pid {pid} is still present)")


@contextlib.contextmanager
def offline_guard(source: Path, pid_file: Path) -> Iterator[None]:
    require_stopped(pid_file)
    lock = source / "qdrant" / ".lock"
    if not lock.is_file():
        raise ValueError("source has no Qdrant lock file; baseline has not been verified")
    with lock.open("rb+") as handle:
        try:
            portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
        except portalocker.LockException as exc:
            raise ValueError("Qdrant is still owned by a running process") from exc
        try:
            yield
        finally:
            portalocker.unlock(handle)


def inventory(root: Path) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"symlink is not allowed in a snapshot: {path.relative_to(root)}")
        if path.is_file():
            entries[path.relative_to(root).as_posix()] = {
                "bytes": path.stat().st_size,
                "sha256": digest(path),
            }
    if "sentrix.db" not in entries:
        raise ValueError("missing sentrix.db")
    return entries


def database_facts(root: Path) -> dict[str, Any]:
    with sqlite3.connect((root / "sentrix.db").as_uri() + "?mode=ro", uri=True) as db:
        check = db.execute("PRAGMA integrity_check").fetchall()
        if check != [("ok",)]:
            raise ValueError("SQLite integrity_check failed")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "schema_migrations" not in tables:
            raise ValueError("missing migration ledger")
        columns = [r[1] for r in db.execute("PRAGMA table_info(schema_migrations)")]
        rows = [list(r) for r in db.execute("SELECT * FROM schema_migrations ORDER BY 1")]
        return {"integrity": "ok", "migration_columns": columns, "migrations": rows}


def _destination(source: Path, destination: Path) -> None:
    if destination.exists():
        raise ValueError("destination already exists; snapshots and run copies are immutable")
    if destination == source or source in destination.parents or destination in source.parents:
        raise ValueError("source and destination must be separate directory trees")


def create(source: Path, destination: Path, identity: dict[str, Any], pid_file: Path) -> dict[str, Any]:
    source, destination = source.resolve(), destination.resolve()
    validate_identity(identity)
    _destination(source, destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with offline_guard(source, pid_file):
        before = inventory(source)
        facts = database_facts(source)
        temporary = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=destination.parent))
        try:
            shutil.copytree(source, temporary / "data")
            copied = inventory(temporary / "data")
            if copied != before or inventory(source) != before:
                raise ValueError("source changed during copy")
            manifest = {
                "schema_version": VERSION,
                "identity": identity,
                "identity_sha256": identity_digest(identity),
                "database": facts,
                "files": copied,
            }
            path = temporary / "manifest.json"
            with path.open("w") as handle:
                json.dump(manifest, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.rename(destination)
            return manifest
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)


def verify(snapshot: Path, expected: dict[str, Any]) -> dict[str, Any]:
    snapshot = snapshot.resolve()
    validate_identity(expected)
    manifest: dict[str, Any] = json.loads((snapshot / "manifest.json").read_text())
    if manifest.get("schema_version") != VERSION:
        raise ValueError("unsupported snapshot version")
    if manifest.get("identity") != expected or manifest.get("identity_sha256") != identity_digest(expected):
        raise ValueError("incompatible memory snapshot")
    if inventory(snapshot / "data") != manifest.get("files"):
        raise ValueError("snapshot file integrity mismatch")
    if database_facts(snapshot / "data") != manifest.get("database"):
        raise ValueError("snapshot schema or database integrity mismatch")
    return manifest


def restore(snapshot: Path, destination: Path, expected: dict[str, Any]) -> dict[str, Any]:
    snapshot, destination = snapshot.resolve(), destination.resolve()
    _destination(snapshot, destination)
    manifest = verify(snapshot, expected)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".restore-", dir=destination.parent))
    try:
        shutil.copytree(snapshot / "data", temporary / "data")
        if inventory(temporary / "data") != manifest["files"]:
            raise ValueError("restored files failed verification")
        (temporary / "data").rename(destination)
    finally:
        shutil.rmtree(temporary)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["create", "verify", "restore"])
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--service-pid-file", type=Path)
    args = parser.parse_args()
    identity = json.loads(args.identity.read_text())
    if args.action == "create":
        if args.destination is None or args.service_pid_file is None:
            parser.error("create needs --destination and --service-pid-file")
        result = create(args.source, args.destination, identity, args.service_pid_file)
    elif args.action == "restore":
        if args.destination is None:
            parser.error("restore needs --destination")
        result = restore(args.source, args.destination, identity)
    else:
        result = verify(args.source, identity)
    print(
        json.dumps(
            {
                "status": "verified",
                "identity_sha256": result["identity_sha256"],
                "files": len(result["files"]),
                "schema_version": VERSION,
            }
        )
    )


if __name__ == "__main__":
    main()
