"""Source IDs resolve races using independent real SQLite connections."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from threading import Barrier

import pytest

from sentrix.application.media.import_assets import (
    ImportAssets,
    ImportRequest,
    ImportResult,
    SourceConflictError,
    StagedImport,
)
from sentrix.infrastructure.media.storage import FileSystemMediaStorage
from sentrix.infrastructure.persistence.database import Database
from sentrix.infrastructure.persistence.repositories.assets import (
    SqliteAssetRepository,
    SqliteObservationRepository,
)


class Metadata:
    def read(self, path):
        return None, None, None


class RacingAssets(SqliteAssetRepository):
    def __init__(self, database, barrier):
        super().__init__(database)
        self.barrier = barrier

    def find_by_external_source_id(self, scope_id, external_source_id):
        result = super().find_by_external_source_id(scope_id, external_source_id)
        assert result is None, "the test must force both imports past the initial miss"
        self.barrier.wait(timeout=10)
        return result


def request():
    return ImportRequest(
        scope_id="documents",
        file_name="document.txt",
        content_type="text/plain",
        content=b"Original source body",
        external_source_id="source-1",
        source_timestamp="2022-06-01 12:34:56",
        source_location="Source location",
        source_metadata={"ordinal": 1},
    )


def competing_imports(database, tmp_path, second_request):
    barrier = Barrier(2)
    importers = []
    for _ in range(2):
        # Same database file, different Database objects and connections; no
        # Python lock or shared repository can accidentally serialize the race.
        db = Database(database.path, database.migrations_dir)
        importers.append(
            ImportAssets(
                assets=RacingAssets(db, barrier),
                observations=SqliteObservationRepository(db),
                storage=FileSystemMediaStorage(tmp_path / "media"),
                metadata=Metadata(),
                clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(importer.stage, item)
            for importer, item in zip(importers, (request(), second_request), strict=True)
        ]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result(timeout=15))
            except SourceConflictError as error:
                outcomes.append(error)
    return outcomes


def test_same_source_and_content_race_returns_one_asset(migrations_ready_db, tmp_path):
    outcomes = competing_imports(migrations_ready_db, tmp_path, request())
    staged = next(item for item in outcomes if isinstance(item, StagedImport))
    duplicate = next(item for item in outcomes if isinstance(item, ImportResult))
    assert duplicate.deduplicated and duplicate.asset_id == staged.asset.id
    with migrations_ready_db.connect() as connection:
        assert connection.execute("SELECT count(*) FROM assets").fetchone()[0] == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"content": b"Different source body"},
        {"file_name": "different.txt"},
        {"source_timestamp": "2022-06-02 12:34:56"},
        {"source_location": "Different location"},
        {"source_metadata": {"ordinal": 2}},
    ],
)
def test_same_source_with_conflicting_content_or_metadata_is_409(migrations_ready_db, tmp_path, changes):
    outcomes = competing_imports(migrations_ready_db, tmp_path, replace(request(), **changes))
    assert sum(isinstance(item, StagedImport) for item in outcomes) == 1
    conflict = next(item for item in outcomes if isinstance(item, SourceConflictError))
    assert conflict.status_code == 409 and conflict.code == "external_source_conflict"
    with migrations_ready_db.connect() as connection:
        assert connection.execute("SELECT count(*) FROM assets").fetchone()[0] == 1


def test_identical_content_under_different_source_ids_remains_independent(migrations_ready_db, tmp_path):
    outcomes = competing_imports(
        migrations_ready_db, tmp_path, replace(request(), external_source_id="source-2")
    )
    assert all(isinstance(item, StagedImport) for item in outcomes)
    assert len({item.asset.id for item in outcomes}) == 2
    with migrations_ready_db.connect() as connection:
        assert connection.execute("SELECT count(*) FROM assets").fetchone()[0] == 2


def test_recovered_source_collision_does_not_poison_enclosing_transaction(migrations_ready_db, tmp_path):
    assets = SqliteAssetRepository(migrations_ready_db)
    importer = ImportAssets(
        assets=assets,
        observations=SqliteObservationRepository(migrations_ready_db),
        storage=FileSystemMediaStorage(tmp_path / "media"),
        metadata=Metadata(),
    )
    first = importer.stage(request())
    assert isinstance(first, StagedImport)
    # Simulate the initial lookup's stale miss without changing the real INSERT.
    assets.find_by_external_source_id = lambda *args: None
    with migrations_ready_db.transaction() as connection:
        duplicate = importer.stage(request())
        assert duplicate.deduplicated and duplicate.asset_id == first.asset.id
        connection.execute("UPDATE assets SET source_location='Committed' WHERE id=?", (first.asset.id,))
    assert assets.get(first.asset.id).source_location == "Committed"


def test_unrelated_asset_constraints_are_not_converted_into_source_revisits(migrations_ready_db, tmp_path):
    import sqlite3

    assets = SqliteAssetRepository(migrations_ready_db)
    importer = ImportAssets(
        assets=assets,
        observations=SqliteObservationRepository(migrations_ready_db),
        storage=FileSystemMediaStorage(tmp_path / "media"),
        metadata=Metadata(),
    )
    first = importer.stage(request())
    with pytest.raises(sqlite3.IntegrityError):
        assets.add(replace(first.asset, external_source_id="different-source"))
