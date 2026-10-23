#!/usr/bin/env python3
"""Create a schema-0013 copy; never modify an existing database or discard source data."""

from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path


def rollback_copy(source: Path, output: Path) -> None:
    """Reverse only unused additive migrations 0014/0015 in a new SQLite backup."""
    if not source.is_file():
        raise ValueError("Source database does not exist")
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(output) + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            raise FileExistsError("Output database sidecar already exists")
    # Reserve the output atomically: SQLite's normal connect would overwrite it.
    with output.open("xb"):
        pass
    try:
        output.chmod(0o600)
        with (
            closing(sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)) as original,
            closing(sqlite3.connect(output)) as copy,
        ):
            original.backup(copy)
            versions = {row[0] for row in copy.execute("SELECT version FROM schema_migrations")}
            if versions != {f"{version:04d}" for version in range(1, 16)}:
                raise ValueError("Expected migrations through 0015, without later migrations")
            used = copy.execute(
                "SELECT EXISTS(SELECT 1 FROM assets WHERE external_source_id IS NOT NULL "
                "OR source_timestamp IS NOT NULL OR source_location != '' "
                "OR source_metadata_json != '{}') "
                "OR EXISTS(SELECT 1 FROM observations WHERE source_text != '')"
            ).fetchone()
            if used is None or used[0]:
                raise ValueError("Source fields contain data; restore the pre-upgrade backup instead")
            copy.execute("BEGIN IMMEDIATE")
            copy.execute("DROP INDEX idx_assets_external_source_id")
            for column in (
                "external_source_id",
                "source_timestamp",
                "source_location",
                "source_metadata_json",
            ):
                copy.execute(f"ALTER TABLE assets DROP COLUMN {column}")
            copy.execute("ALTER TABLE observations DROP COLUMN source_text")
            copy.execute("DELETE FROM schema_migrations WHERE version IN ('0014', '0015')")
            if list(copy.execute("PRAGMA foreign_key_check")):
                raise ValueError("Foreign-key verification failed")
            if copy.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise ValueError("Database integrity verification failed")
            copy.commit()
            checkpoint = copy.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is None or checkpoint[0] != 0:
                raise ValueError("Output database checkpoint failed")
            if copy.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
                raise ValueError("Output database could not be made self-contained")
    except BaseException:
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(str(output) + suffix).unlink(missing_ok=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path, help="New output path; must not exist")
    args = parser.parse_args()
    rollback_copy(args.source, args.output)
    print("Created and verified schema-0013 copy; original database unchanged")


if __name__ == "__main__":
    main()
