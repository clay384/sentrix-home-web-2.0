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

VERSION = 2
SQLITE_FILES = frozenset({"sentrix.db", "sentrix.db-wal", "sentrix.db-shm"})
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


@contextlib.contextmanager
def _read_database(root: Path) -> Iterator[sqlite3.Connection]:
    # Even SQLite mode=ro can rewrite the shared-memory WAL index. Inspect a
    # private DB+WAL copy so verification never changes immutable source bytes.
    with tempfile.TemporaryDirectory(prefix=".snapshot-sqlite-") as temporary:
        copied = Path(temporary)
        for name in ("sentrix.db", "sentrix.db-wal"):
            source = root / name
            if source.exists():
                shutil.copyfile(source, copied / name)
        with contextlib.closing(
            sqlite3.connect((copied / "sentrix.db").as_uri() + "?mode=ro", uri=True)
        ) as db:
            yield db


def database_facts(root: Path) -> dict[str, Any]:
    with _read_database(root) as db:
        check = db.execute("PRAGMA integrity_check").fetchall()
        if check != [("ok",)]:
            raise ValueError("SQLite integrity_check failed")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "schema_migrations" not in tables:
            raise ValueError("missing migration ledger")
        columns = [r[1] for r in db.execute("PRAGMA table_info(schema_migrations)")]
        rows = [list(r) for r in db.execute("SELECT * FROM schema_migrations ORDER BY 1")]
        return {"integrity": "ok", "migration_columns": columns, "migrations": rows}


def _canonical_root(value: Any) -> Path:
    if not isinstance(value, str):
        raise ValueError("snapshot source_root is missing; recreate a portable snapshot")
    root = Path(value)
    if not root.is_absolute() or ".." in root.parts or str(root) != value:
        raise ValueError("source_root must be a canonical absolute path")
    return root


def _asset_paths(root: Path, source_root: Path, files: dict[str, Any]) -> list[tuple[Any, str, str]]:
    """Validate lexical provenance without consulting the old host's filesystem."""
    with _read_database(root) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(assets)")}
        if not {"id", "path"}.issubset(columns):
            raise ValueError("missing assets path ledger")
        rows = db.execute("SELECT id, path FROM assets ORDER BY id").fetchall()
    result = []
    for asset_id, raw in rows:
        if not isinstance(raw, str):
            raise ValueError("asset path must be a canonical absolute media path")
        path = Path(raw)
        if not path.is_absolute() or ".." in path.parts or str(path) != raw:
            raise ValueError("asset path traversal or noncanonical path")
        try:
            relative = path.relative_to(source_root)
        except ValueError as exc:
            raise ValueError("asset path is outside source_root") from exc
        if len(relative.parts) < 2 or relative.parts[0] != "media":
            raise ValueError("asset path is outside the media subtree")
        name = relative.as_posix()
        if name not in files or not (root / relative).is_file():
            raise ValueError("asset media file is missing from the snapshot")
        result.append((asset_id, raw, name))
    return result


def receipt_path_for(destination: Path) -> Path:
    return destination.parent / (destination.name + ".restore-receipt.json")


def _sqlite_files(files: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in files.items() if key in SQLITE_FILES}


def _media_and_indexes(files: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in files.items() if key not in SQLITE_FILES}


def validate_restore_receipt(
    receipt: dict[str, Any], manifest: dict[str, Any], destination: Path, manifest_sha256: str
) -> None:
    """Bind offline restore evidence to a live run without reading its active DB."""
    if manifest.get("schema_version") != VERSION:
        raise ValueError("legacy snapshot is not portable; recreate before restore")
    expected = {
        "schema_version": 1,
        "manifest_sha256": manifest_sha256,
        "identity_sha256": manifest["identity_sha256"],
        "from_root": str(_canonical_root(manifest.get("source_root"))),
        "to_root": str(destination.resolve()),
        "database": manifest["database"],
    }
    if any(receipt.get(key) != value for key, value in expected.items()):
        raise ValueError("restore receipt does not match snapshot or destination")
    count = receipt.get("relocated_assets")
    sha = receipt.get("post_restore_database_sha256")
    files = receipt.get("sqlite_files")
    if (
        not isinstance(count, int)
        or isinstance(count, bool)
        or count < 0
        or not isinstance(sha, str)
        or len(sha) != 64
        or any(c not in "0123456789abcdef" for c in sha)
        or not isinstance(files, dict)
        or set(files) - SQLITE_FILES
        or not isinstance(files.get("sentrix.db"), dict)
        or files["sentrix.db"].get("sha256") != sha
    ):
        raise ValueError("restore receipt has invalid relocation or database facts")


def verify_restored(
    snapshot: Path, destination: Path, expected: dict[str, Any], receipt_path: Path | None = None
) -> dict[str, Any]:
    """Full offline check; call before starting the API, never on an active DB."""
    snapshot, destination = snapshot.resolve(), destination.resolve()
    manifest = verify(snapshot, expected)
    receipt: dict[str, Any] = json.loads((receipt_path or receipt_path_for(destination)).read_text())
    validate_restore_receipt(receipt, manifest, destination, digest(snapshot / "manifest.json"))
    facts = database_facts(destination)
    files = inventory(destination)
    if facts != manifest["database"] or _sqlite_files(files) != receipt["sqlite_files"]:
        raise ValueError("restored database integrity or receipt mismatch")
    if _media_and_indexes(files) != _media_and_indexes(manifest["files"]):
        raise ValueError("restored media or index integrity mismatch")
    if len(_asset_paths(destination, destination, files)) != receipt["relocated_assets"]:
        raise ValueError("restored asset path count mismatch")
    return receipt


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
        _asset_paths(source, source, before)
        temporary = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=destination.parent))
        try:
            shutil.copytree(source, temporary / "data")
            copied = inventory(temporary / "data")
            if copied != before or inventory(source) != before:
                raise ValueError("source changed during copy")
            manifest = {
                "schema_version": VERSION,
                "source_root": str(source),
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
    if manifest.get("schema_version") not in {1, VERSION}:
        raise ValueError("unsupported snapshot version")
    if manifest.get("identity") != expected or manifest.get("identity_sha256") != identity_digest(expected):
        raise ValueError("incompatible memory snapshot")
    if inventory(snapshot / "data") != manifest.get("files"):
        raise ValueError("snapshot file integrity mismatch")
    if database_facts(snapshot / "data") != manifest.get("database"):
        raise ValueError("snapshot schema or database integrity mismatch")
    if manifest["schema_version"] == VERSION:
        _asset_paths(snapshot / "data", _canonical_root(manifest.get("source_root")), manifest["files"])
    return manifest


def restore(
    snapshot: Path, destination: Path, expected: dict[str, Any], receipt_path: Path | None = None
) -> dict[str, Any]:
    snapshot, destination = snapshot.resolve(), destination.resolve()
    _destination(snapshot, destination)
    manifest = verify(snapshot, expected)
    if manifest["schema_version"] != VERSION:
        raise ValueError("legacy snapshot has no source_root; recreate before portable restore")
    source_root = _canonical_root(manifest.get("source_root"))
    manifest_sha = digest(snapshot / "manifest.json")
    receipt_path = (receipt_path or receipt_path_for(destination)).resolve()
    if receipt_path.exists():
        raise ValueError("restore receipt already exists")
    for tree in (snapshot, destination, source_root):
        if receipt_path == tree or tree in receipt_path.parents:
            raise ValueError("restore receipt must be outside the source and destination trees")
    destination.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".restore-", dir=destination.parent))
    published_receipt = False
    try:
        copied_root = temporary / "data"
        shutil.copytree(snapshot / "data", copied_root)
        if inventory(copied_root) != manifest["files"]:
            raise ValueError("restored files failed verification")
        if (
            inventory(snapshot / "data") != manifest["files"]
            or digest(snapshot / "manifest.json") != manifest_sha
        ):
            raise ValueError("snapshot changed during restore")
        paths = _asset_paths(copied_root, source_root, manifest["files"])
        with contextlib.closing(sqlite3.connect(copied_root / "sentrix.db")) as db:
            with db:
                for asset_id, old, relative in paths:
                    result = db.execute(
                        "UPDATE assets SET path=? WHERE id=? AND path=?",
                        (str(destination / relative), asset_id, old),
                    )
                    if result.rowcount != 1:
                        raise ValueError("asset path changed during relocation")
            # Consolidate copied WAL into this independent DB; never touch the source.
            checkpoint = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint and checkpoint[0] != 0:
                raise ValueError("restored database WAL checkpoint is busy")
        facts = database_facts(copied_root)
        files = inventory(copied_root)
        if facts != manifest["database"]:
            raise ValueError("restored schema or SQLite integrity changed")
        if _media_and_indexes(files) != _media_and_indexes(manifest["files"]):
            raise ValueError("restore changed media or index files")
        relocated = _asset_paths(copied_root, destination, files)
        if [(i, name) for i, _, name in relocated] != [(i, name) for i, _, name in paths]:
            raise ValueError("restored asset mapping differs from snapshot")
        receipt = {
            "schema_version": 1,
            "manifest_sha256": manifest_sha,
            "identity_sha256": manifest["identity_sha256"],
            "from_root": str(source_root),
            "to_root": str(destination),
            "relocated_assets": len(paths),
            "post_restore_database_sha256": files["sentrix.db"]["sha256"],
            "database": facts,
            "sqlite_files": _sqlite_files(files),
        }
        validate_restore_receipt(receipt, manifest, destination, manifest_sha)
        # Publish a complete receipt before the directory becomes available. Both
        # live on this filesystem by default; link refuses to overwrite evidence.
        staged_receipt = temporary / "receipt.json"
        with staged_receipt.open("w") as handle:
            json.dump(receipt, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        # A custom receipt may live on a different filesystem.
        with tempfile.NamedTemporaryFile(dir=receipt_path.parent, delete=False) as receipt_file:
            receipt_temp = Path(receipt_file.name)
            receipt_file.write(staged_receipt.read_bytes())
            receipt_file.flush()
            os.fsync(receipt_file.fileno())
        try:
            os.link(receipt_temp, receipt_path)
            published_receipt = True
        finally:
            receipt_temp.unlink()
        copied_root.rename(destination)
    except Exception:
        if published_receipt:
            receipt_path.unlink()
        raise
    finally:
        shutil.rmtree(temporary)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["create", "verify", "restore", "verify-restored"])
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--service-pid-file", type=Path)
    parser.add_argument("--receipt", type=Path, help="restore receipt outside the data tree")
    args = parser.parse_args()
    identity = json.loads(args.identity.read_text())
    if args.action == "create":
        if args.destination is None or args.service_pid_file is None:
            parser.error("create needs --destination and --service-pid-file")
        result = create(args.source, args.destination, identity, args.service_pid_file)
    elif args.action == "restore":
        if args.destination is None:
            parser.error("restore needs --destination")
        result = restore(args.source, args.destination, identity, args.receipt)
    elif args.action == "verify-restored":
        if args.destination is None:
            parser.error("verify-restored needs --destination")
        verify_restored(args.source, args.destination, identity, args.receipt)
        result = verify(args.source, identity)
    else:
        result = verify(args.source, identity)
    print(
        json.dumps(
            {
                "status": "verified",
                "identity_sha256": result["identity_sha256"],
                "files": len(result["files"]),
                "schema_version": result["schema_version"],
                "portable": result["schema_version"] == VERSION,
                "receipt": str((args.receipt or receipt_path_for(args.destination.resolve())).resolve())
                if args.action in {"restore", "verify-restored"}
                else None,
            }
        )
    )


if __name__ == "__main__":
    main()
