"""Original wall clocks coexist with legacy instants without inventing a timezone."""

from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from sentrix.application.assistant.tools import AssistantToolbox
from sentrix.application.media.import_assets import ImportAssets, ImportRequest
from sentrix.application.media.observe_asset import ObserveAsset
from sentrix.application.memories.assign_events import AssignEvents, EventVisualSimilarity
from sentrix.application.retrieval.index_observation_text import IndexObservationText
from sentrix.application.retrieval.retrieve_metadata import RetrieveMetadata
from sentrix.domain.assistant.result_set import ResultSet
from sentrix.domain.media.models import MediaType
from sentrix.domain.media.ports import ObservationSemantics
from sentrix.domain.memories.events import (
    EventAnchorSet,
    ObservationAnchor,
    VisualSignal,
    score_event_candidate,
    within_candidate_window,
)
from sentrix.domain.places.aliases import EMPTY_ALIASES
from sentrix.infrastructure.media.storage import FileSystemMediaStorage
from sentrix.infrastructure.persistence.repositories.asset_places import SqliteAssetPlaceRepository
from sentrix.infrastructure.persistence.repositories.assets import (
    SqliteAssetRepository,
    SqliteObservationRepository,
)
from sentrix.infrastructure.persistence.repositories.events import SqliteEventRepository
from sentrix.infrastructure.persistence.repositories.result_sets import SqliteResultSetRepository
from sentrix.infrastructure.persistence.repositories.search_terms import SqliteLexicalIndex
from sentrix.infrastructure.persistence.repositories.vectors import SqliteVectorRepository

INSTANT = datetime(2022, 6, 1, 12, tzinfo=UTC)
WALL_CLOCK = "2022-06-01 12:34:56"
BODY = "Original source text with its original line break.\nSecond line."


class SyntheticImageObserver:
    def observe(self, path):
        return ObservationSemantics(
            caption="A picnic in the garden",
            place="garden",
            activity="picnic",
            event_type="gathering",
            objects=("basket",),
            confidence=1.0,
        )


class SyntheticTextEmbedder:
    dimension = 4
    model_id = "synthetic-text"

    def embed_query(self, text):
        return [1.0, 0.5, 0.25, 0.125]


@pytest.fixture
def pipeline(migrations_ready_db, tmp_path):
    db = migrations_ready_db
    assets, observations = SqliteAssetRepository(db), SqliteObservationRepository(db)
    events, places = SqliteEventRepository(db), SqliteAssetPlaceRepository(db)
    vectors = SqliteVectorRepository(db)
    assignment = AssignEvents(
        events=events,
        observations=observations,
        assets=assets,
        places=places,
        visual_similarity=EventVisualSimilarity(vectors=vectors, observations=observations, assets=assets),
        confirmed_people=lambda _: frozenset(),
        confirmed_event_people=lambda _: frozenset(),
    )
    storage = FileSystemMediaStorage(tmp_path / "media")
    metadata = SimpleNamespace(captured_at=INSTANT)
    metadata.read = lambda _: (metadata.captured_at, None, None)
    text_index = IndexObservationText(embedder=SyntheticTextEmbedder(), vectors=vectors)
    lexical = SqliteLexicalIndex(db, observations, assets=assets, places=places)
    importer = ImportAssets(
        assets=assets,
        observations=observations,
        storage=storage,
        metadata=metadata,
        observer=ObserveAsset(observer=SyntheticImageObserver(), observations=observations, storage=storage),
        event_assigner=lambda observation: assignment.execute(observation).event.id,
        text_indexer=lambda observation, event_id: text_index.index(observation, event_id).rows,
        lexical_index=lexical,
    )
    return SimpleNamespace(
        db=db,
        importer=importer,
        assets=assets,
        observations=observations,
        events=events,
        places=places,
        metadata=metadata,
    )


def import_image(pipeline, suffix="one"):
    return pipeline.importer.execute(
        ImportRequest("mixed", f"legacy-{suffix}.jpg", "image/jpeg", f"image-{suffix}".encode())
    )


def import_source(pipeline):
    return pipeline.importer.execute(
        ImportRequest(
            "mixed",
            "source.txt",
            "text/plain",
            BODY.encode(),
            external_source_id="source-text",
            source_timestamp=WALL_CLOCK,
        )
    )


@pytest.mark.parametrize("source_first", [False, True])
def test_legacy_instant_and_source_wall_clock_import_in_either_order(pipeline, source_first):
    if source_first:
        source, image = import_source(pipeline), import_image(pipeline)
    else:
        image, source = import_image(pipeline), import_source(pipeline)
    assert source.event_id != image.event_id
    assert len(pipeline.events.list_active("mixed", limit=10)) == 2
    original = pipeline.assets.get(source.asset_id)
    assert original.source_timestamp == WALL_CLOCK
    assert original.captured_at == datetime.fromisoformat(WALL_CLOCK)
    assert original.captured_at.tzinfo is None
    assert pipeline.assets.get(image.asset_id).captured_at == INSTANT
    assert pipeline.observations.get(source.observation_id).source_text == BODY
    assert source.text_rows == 3
    assert source.lexical_terms > 0
    result_sets = SqliteResultSetRepository(pipeline.db)
    result_sets.add(
        ResultSet(
            id="source-result",
            scope_id="mixed",
            query="original",
            asset_ids=(source.asset_id,),
            created_at=INSTANT,
        )
    )
    toolbox = AssistantToolbox(
        retrieval=None,
        assets=pipeline.assets,
        result_sets=result_sets,
        people=None,
        memory_facts=None,
        faces=None,
        observations=pipeline.observations,
        photo_inspector=None,
        person_crops=None,
    )
    outcome = toolbox.execute(
        "read_source_text",
        {"result_set_id": "source-result", "asset_handle": "photo_1"},
        scope_id="mixed",
        now=INSTANT,
    )
    assert outcome.ok and outcome.model_visible["full_text"] == BODY


@pytest.mark.parametrize("tz", [None, UTC, timezone(timedelta(hours=2))])
def test_same_awareness_keeps_native_event_merge_and_original_timestamps(pipeline, tz):
    first_time = datetime(2022, 6, 1, 12, tzinfo=tz)
    pipeline.metadata.captured_at = first_time
    first = import_image(pipeline)
    pipeline.metadata.captured_at = first_time + timedelta(minutes=5)
    second = import_image(pipeline, "two")
    assert first.event_id == second.event_id
    event = pipeline.events.get(first.event_id)
    assert event.time_start == first_time
    assert event.time_end == first_time + timedelta(minutes=5)
    assert pipeline.assets.get(first.asset_id).captured_at.isoformat() == first_time.isoformat()


def test_metadata_time_windows_never_compare_wall_clocks_to_instants(pipeline):
    image, source = import_image(pipeline), import_source(pipeline)
    retrieval = RetrieveMetadata(assets=pipeline.assets, places=pipeline.places, aliases=EMPTY_ALIASES)
    for start, expected in (
        (datetime(2022, 6, 1), source.asset_id),
        (datetime(2022, 6, 1, tzinfo=UTC), image.asset_id),
        (datetime(2022, 6, 1, tzinfo=timezone(timedelta(hours=2))), image.asset_id),
    ):
        found = retrieval.execute(scope_id="mixed", time_bounds=(start, start + timedelta(days=1)))
        assert [item.asset_id for item in found] == [expected]
    all_media = retrieval.execute(scope_id="mixed", media_types=[MediaType.IMAGE, MediaType.TEXT])
    assert {item.asset_id for item in all_media} == {image.asset_id, source.asset_id}


def test_incompatible_time_signal_is_zero_and_cannot_open_the_candidate_window():
    naive = INSTANT.replace(tzinfo=None)
    for incoming, existing in ((INSTANT, naive), (naive, INSTANT)):
        assert not within_candidate_window(incoming, existing)
        score = score_event_candidate(
            anchor=ObservationAnchor(captured_at=incoming),
            event=EventAnchorSet(event_id="event", time_start=existing, activity="", anchors=()),
            visual=VisualSignal(),
        )
        assert score.time == 0


def test_repository_rejects_incompatible_fold_and_merge_before_writing(pipeline):
    image, source = import_image(pipeline), import_source(pipeline)
    original = pipeline.events.get(image.event_id)
    with pytest.raises(ValueError, match="known timezone"):
        pipeline.events.fold_observation(
            image.event_id,
            time_start=datetime.fromisoformat(WALL_CLOCK),
            time_end=datetime.fromisoformat(WALL_CLOCK),
            place="changed",
            admin_district="",
            activity="changed",
            summary="changed",
            confidence=1.0,
            aggregation_score=1.0,
            aggregation_breakdown={},
        )
    with pytest.raises(ValueError, match="known timezone"):
        pipeline.events.merge(source.event_id, image.event_id)
    assert pipeline.events.get(image.event_id) == original
    assert pipeline.events.observation_ids(source.event_id) == [source.observation_id]
    assert pipeline.events.observation_ids(image.event_id) == [image.observation_id]


def test_event_creation_rejects_a_range_without_a_known_common_timezone(pipeline):
    with pytest.raises(ValueError, match="known timezone"):
        pipeline.events.create(
            event_id="invalid-time-range",
            scope_id="mixed",
            time_start=INSTANT,
            time_end=datetime.fromisoformat(WALL_CLOCK),
            place="",
            admin_district="",
            activity="",
            summary="",
            confidence=1.0,
            aggregation_score=0.0,
            aggregation_breakdown={},
        )
    assert pipeline.events.get("invalid-time-range") is None
