"""Source provenance and atomic import use real SQLite, without model services."""

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from sentrix.application.media.import_assets import (
    ImportAssets,
    ImportRequest,
    SourceConflictError,
    StagedImport,
)
from sentrix.application.media.observe_asset import ObserveAsset
from sentrix.application.retrieval.index_observation_text import IndexObservationText
from sentrix.application.retrieval.retrieve_lexical import RetrieveLexical
from sentrix.application.retrieval.retrieve_metadata import RetrieveMetadata
from sentrix.domain.media.models import AssetStatus
from sentrix.domain.places.aliases import EMPTY_ALIASES
from sentrix.infrastructure.media.storage import FileSystemMediaStorage
from sentrix.infrastructure.persistence.database import TransactionError
from sentrix.infrastructure.persistence.repositories.asset_places import SqliteAssetPlaceRepository
from sentrix.infrastructure.persistence.repositories.assets import (
    SqliteAssetRepository,
    SqliteObservationRepository,
)
from sentrix.infrastructure.persistence.repositories.search_terms import SqliteLexicalIndex
from sentrix.infrastructure.persistence.repositories.vectors import SqliteVectorRepository


class Metadata:
    def read(self, path):
        return datetime(1999, 1, 1), None, None


class TextEmbedder:
    model_id = "synthetic-text"
    dimension = 4

    def embed_query(self, text):
        return [1.0, 0.5, 0.25, 0.125]


@pytest.fixture
def source_setup(migrations_ready_db, tmp_path):
    db = migrations_ready_db
    assets = SqliteAssetRepository(db)
    observations = SqliteObservationRepository(db)
    places = SqliteAssetPlaceRepository(db)
    lexical = SqliteLexicalIndex(db, observations, assets=assets, places=places)
    text = IndexObservationText(embedder=TextEmbedder(), vectors=SqliteVectorRepository(db))
    storage = FileSystemMediaStorage(tmp_path / "media")
    importer = ImportAssets(
        assets=assets,
        observations=observations,
        storage=storage,
        metadata=Metadata(),
        observer=ObserveAsset(observer=None, observations=observations, storage=storage),
        text_indexer=lambda observation, event_id: text.index(observation, event_id).rows,
        lexical_index=lexical,
        clock=lambda: datetime(2026, 10, 23, tzinfo=UTC),
    )
    return db, assets, observations, places, lexical, storage, importer


def source_request(identity="source-1", **kwargs):
    return ImportRequest(
        scope_id="sources",
        file_name="original.txt",
        content_type="text/plain",
        content=b"The original source body.",
        external_source_id=identity,
        source_timestamp="2017-10-05 12:34:56",
        source_location="Cambridge, Massachusetts",
        source_metadata={"sender": "sender@example.invalid", "subject": "Original subject"},
        **kwargs,
    )


def test_source_identity_preserves_original_fields_and_is_idempotent(source_setup):
    db, assets, observations, _, _, storage, importer = source_setup
    request = source_request()
    result = importer.execute(request)
    loaded = assets.get(result.asset_id)
    assert loaded.external_source_id == request.external_source_id
    assert loaded.source_timestamp == "2017-10-05 12:34:56"
    assert loaded.captured_at == datetime(2017, 10, 5, 12, 34, 56)
    assert loaded.captured_at.tzinfo is None
    assert loaded.source_location == request.source_location
    assert loaded.source_metadata == request.source_metadata
    assert storage.read(loaded.id) == request.content
    duplicate = importer.execute(request)
    assert duplicate.deduplicated and duplicate.asset_id == result.asset_id
    assert duplicate.observation_id == result.observation_id
    assert len(observations.list_by_asset(result.asset_id)) == 1
    with db.connect() as connection:
        assert connection.execute("SELECT count(*) FROM assets").fetchone()[0] == 1


def test_distinct_source_ids_with_identical_bytes_remain_distinct(source_setup):
    _, assets, _, _, _, _, importer = source_setup
    first = importer.execute(source_request("source-1"))
    second = importer.execute(source_request("source-2"))
    assert first.asset_id != second.asset_id
    assert not second.deduplicated
    assert assets.get(first.asset_id).content_sha256 == assets.get(second.asset_id).content_sha256
    scoped = importer.execute(replace(source_request(), scope_id="other-scope"))
    assert scoped.asset_id != first.asset_id


@pytest.mark.parametrize(
    "changes",
    [
        {"content": b"Different content"},
        {"source_timestamp": "2017-10-06 12:34:56"},
        {"source_location": "Boston"},
        {"source_metadata": {"sender": "another@example.invalid"}},
        {"file_name": "another.txt"},
    ],
)
def test_same_source_id_with_changed_content_or_metadata_is_rejected(source_setup, changes):
    _, assets, observations, _, _, _, importer = source_setup
    first = importer.execute(source_request())
    with pytest.raises(SourceConflictError):
        importer.stage(replace(source_request(), **changes))
    assert assets.get(first.asset_id).source_location == "Cambridge, Massachusetts"
    assert len(observations.list_by_asset(first.asset_id)) == 1


def test_explicit_naive_time_overrides_exif_without_host_timezone_conversion(source_setup):
    _, assets, _, _, _, _, importer = source_setup
    image = replace(source_request(), file_name="image.jpg", content_type="image/jpeg")
    staged = importer.stage(image)
    assert isinstance(staged, StagedImport)
    assert assets.get(staged.asset.id).captured_at == datetime(2017, 10, 5, 12, 34, 56)


def test_unknown_source_time_remains_null_and_legacy_upload_is_compatible(source_setup):
    _, assets, _, _, _, _, importer = source_setup
    identified = importer.execute(replace(source_request(), source_timestamp=None))
    assert assets.get(identified.asset_id).captured_at is None
    legacy = replace(source_request(), scope_id="legacy", external_source_id=None, source_timestamp=None)
    first = importer.execute(legacy)
    second = importer.execute(legacy)
    assert assets.get(first.asset_id).captured_at == datetime(2026, 10, 23, tzinfo=UTC)
    assert first.asset_id == second.asset_id and second.deduplicated


@pytest.mark.parametrize("timestamp", ["2017-10-05T12:34:56+08:00", "2017-10-05T04:34:56Z", "invalid"])
def test_source_timestamps_are_rejected_instead_of_inventing_timezone(timestamp):
    with pytest.raises(ValueError):
        replace(source_request(), source_timestamp=timestamp)


def test_original_location_and_time_are_retrievable_without_changing_source_body(source_setup):
    _, assets, observations, places, lexical, _, importer = source_setup
    result = importer.execute(source_request())
    body = observations.list_by_asset(result.asset_id)[0].source_text
    assert body == "The original source body."
    assert "Cambridge" not in body
    retrieval = RetrieveLexical(index=lexical)
    assert [x.asset_id for x in retrieval.execute(whole_query="Cambridge", scope_id="sources", limit=10)] == [
        result.asset_id
    ]
    time_hits = retrieval.execute(whole_query="2017-10-05", scope_id="sources", limit=10)
    assert [x.asset_id for x in time_hits] == [result.asset_id]
    metadata = RetrieveMetadata(assets=assets, places=places, aliases=EMPTY_ALIASES)
    assert [x.asset_id for x in metadata.execute(scope_id="sources", place="Cambridge")] == [result.asset_id]
    assert metadata.execute(scope_id="sources", place="Boston") == []
    assert (
        len(metadata.execute(scope_id="sources", time_bounds=(datetime(2017, 10, 5), datetime(2017, 10, 6))))
        == 1
    )
    assert (
        metadata.execute(scope_id="sources", time_bounds=(datetime(2017, 10, 6), datetime(2017, 10, 7))) == []
    )


def test_atomic_finish_and_controller_receipt_rollback_preserves_archived_source(source_setup):
    db, assets, observations, _, _, storage, importer = source_setup
    staged = importer.stage(source_request())
    assert isinstance(staged, StagedImport)
    prepared = importer.prepare_image(staged.asset)
    with db.connect() as connection, connection:
        connection.execute("CREATE TABLE import_receipts(source_id TEXT PRIMARY KEY)")
    with pytest.raises(RuntimeError, match="injected after receipt"), db.transaction() as connection:
        with db.connect() as same:
            assert same is connection
        result = importer.finish(staged, prepared)
        assert observations.get(result.observation_id) is not None
        assets.update_status(result.asset_id, AssetStatus.FAILED)
        with connection:
            connection.execute("INSERT INTO import_receipts VALUES (?)", ("source-1",))
        with sqlite3.connect(db.path) as outside:
            assert outside.execute("SELECT count(*) FROM observations").fetchone()[0] == 0
            assert outside.execute("SELECT count(*) FROM import_receipts").fetchone()[0] == 0
        raise RuntimeError("injected after receipt")
    assert storage.read(staged.asset.id) == source_request().content
    assert assets.get(staged.asset.id).status is AssetStatus.PROCESSED
    assert observations.list_by_asset(staged.asset.id) == []
    with db.connect() as connection:
        for table in (
            "observations",
            "memory_vectors",
            "observation_search_terms",
            "observation_search_fts",
            "import_receipts",
        ):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_successful_finish_and_receipt_commit_together(source_setup):
    db, _, observations, _, _, _, importer = source_setup
    staged = importer.stage(source_request())
    prepared = importer.prepare_image(staged.asset)
    with db.connect() as connection, connection:
        connection.execute("CREATE TABLE import_receipts(source_id TEXT PRIMARY KEY)")
    with db.transaction() as connection:
        result = importer.finish(staged, prepared)
        with connection:
            connection.execute("INSERT INTO import_receipts VALUES (?)", ("source-1",))
    assert observations.get(result.observation_id) is not None
    with db.connect() as connection:
        assert connection.execute("SELECT count(*) FROM import_receipts").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM memory_vectors").fetchone()[0] == 2


def test_nested_transactions_use_savepoints_and_do_not_commit_outer_write(source_setup):
    db, _, _, _, _, _, _ = source_setup
    with db.connect() as connection, connection:
        connection.execute("CREATE TABLE unit_rows(value TEXT)")
    with db.transaction() as connection:
        connection.execute("INSERT INTO unit_rows VALUES ('outer')")
        with pytest.raises(ValueError), db.transaction() as nested:
            with nested:
                nested.execute("INSERT INTO unit_rows VALUES ('inner')")
            raise ValueError("inner failed")
        assert [x[0] for x in connection.execute("SELECT value FROM unit_rows")] == ["outer"]
    with db.connect() as connection:
        assert [x[0] for x in connection.execute("SELECT value FROM unit_rows")] == ["outer"]


def test_caught_repository_error_marks_transaction_rollback_only(source_setup):
    db, _, _, _, _, _, _ = source_setup
    with db.connect() as connection, connection:
        connection.execute("CREATE TABLE unit_rows(value TEXT UNIQUE)")
    with pytest.raises(TransactionError), db.transaction() as connection:
        connection.execute("INSERT INTO unit_rows VALUES ('outer')")
        try:
            with db.connect() as repository, repository:
                repository.execute("INSERT INTO unit_rows VALUES ('outer')")
        except sqlite3.IntegrityError:
            pass
    with db.connect() as connection:
        assert connection.execute("SELECT count(*) FROM unit_rows").fetchone()[0] == 0


def test_identified_source_revisit_never_reinvokes_preparation_or_reindex(source_setup):
    _, _, _, _, _, _, importer = source_setup
    first = importer.execute(source_request())

    class NeverReindex:
        def index(self, asset):
            raise AssertionError("an existing source must not dispatch another embedding")

        def prepare(self, asset):
            raise AssertionError("an existing source must not dispatch another embedding")

    importer._visual_indexer = NeverReindex()
    duplicate = importer.execute(source_request())
    assert duplicate.deduplicated and duplicate.asset_id == first.asset_id
