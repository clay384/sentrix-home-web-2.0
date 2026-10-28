"""导入路径的**归并顺序**：存储 → EXIF/GPS → 地点解析 → 视觉向量 → 观察 → 语义富集
→ 事件归属 → 词法投影。

顺序写反不会报错，只会算错分——这正是它需要一条断言而不是一句注释的原因
（``s4-deviations.md`` §2.3；1.0 在 ``pipeline.py:315-325`` 用注释钉过同一件事，
注释拦不住任何人）。这里用记录调用序的假件把它钉成可失败的门禁。
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from sentrix.application.media.import_assets import ImportAssets, ImportRequest
from sentrix.domain.media.models import GpsPoint, MediaType, Observation
from sentrix.domain.retrieval.ports import PreparedVisual, VisualIndexOutcome
from sentrix.infrastructure.persistence.database import Database
from sentrix.infrastructure.persistence.repositories.assets import (
    SqliteAssetRepository,
    SqliteObservationRepository,
)

NOW = GpsPoint(30.1839, 120.2642)


class FakeStorage:
    def __init__(self, root: Path) -> None:
        self._root = root

    def store(self, asset_id: str, file_name: str, content: bytes) -> Path:
        path = self._root / f"{asset_id}-{file_name}"
        path.write_bytes(content)
        return path


class FakeMetadata:
    def __init__(self, *, gps: GpsPoint | None) -> None:
        self._gps = gps

    def read(self, path: Path):  # noqa: ANN201 - 与 MetadataReader 的元组返回一致
        from datetime import UTC, datetime

        return (datetime(2017, 10, 5, 12, 0, tzinfo=UTC), self._gps, None)


class Recorder:
    def __init__(self) -> None:
        self.order: list[str] = []


class RecordingVisualIndexer:
    """两段式（``AssetVisualIndexer``）。

    **"visual" 记在 ``commit``**：那一步才是写向量，顺序约束管的正是它。记在
    ``prepare`` 会让「视觉向量先于事件归属」那条断言失去意义——``prepare`` 无论顺序
    怎么排都在 ``commit`` 之前。
    """

    def __init__(self, recorder: Recorder, *, indexed: bool = True) -> None:
        self._recorder = recorder
        self._indexed = indexed

    def prepare(self, asset) -> PreparedVisual:  # noqa: ANN001
        return PreparedVisual(vector=(1.0,))

    def commit(self, asset, prepared) -> VisualIndexOutcome:  # noqa: ANN001
        self._recorder.order.append("visual")
        return VisualIndexOutcome(indexed=self._indexed, mirrored=self._indexed)

    def index(self, asset) -> VisualIndexOutcome:  # noqa: ANN001
        return self.commit(asset, self.prepare(asset))


class RecordingPlaceIndexer:
    """与 ``IndexAssetPlace`` 同形：**没有 GPS 就没有地点行**，连调用都不记。"""

    def __init__(self, recorder: Recorder) -> None:
        self._recorder = recorder

    def index(self, asset) -> bool:  # noqa: ANN001
        if asset.captured_location is None:
            return False
        self._recorder.order.append("place")
        return True


class RecordingObserver:
    """两段式（``AssetObserver``）。**"observe" 记在 ``commit``**——那是写回观察的一步。"""

    def __init__(self, recorder: Recorder) -> None:
        self._recorder = recorder

    def prepare(self, asset) -> None:  # noqa: ANN001
        return None

    def commit(self, observation, semantics) -> Observation | None:  # noqa: ANN001
        self._recorder.order.append("observe")
        return None

    def observe(self, asset, observation) -> Observation | None:  # noqa: ANN001
        return self.commit(observation, self.prepare(asset))


class RecordingLexicalIndex:
    def __init__(self, recorder: Recorder) -> None:
        self._recorder = recorder

    def refresh_from_observation(self, observation: Observation) -> int:
        self._recorder.order.append("lexical")
        return 3

    def rebuild_scope(self, scope_id: str) -> int:  # pragma: no cover - 未在导入路径上调用
        return 0

    def count(self, scope_id: str | None = None) -> int:  # pragma: no cover
        return 0


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def repos(migrations_ready_db: Database) -> tuple[SqliteAssetRepository, SqliteObservationRepository]:
    return SqliteAssetRepository(migrations_ready_db), SqliteObservationRepository(migrations_ready_db)


def build(tmp_path: Path, recorder: Recorder, repos, **overrides) -> ImportAssets:  # noqa: ANN001
    assets, observations = repos
    recorder.order = []
    defaults = {
        "visual_indexer": RecordingVisualIndexer(recorder),
        "place_indexer": RecordingPlaceIndexer(recorder),
        "observer": RecordingObserver(recorder),
        "event_assigner": lambda observation: recorder.order.append("events") or "evt_1",
        "lexical_index": RecordingLexicalIndex(recorder),
    }
    defaults.update(overrides)
    return ImportAssets(
        assets=assets,
        observations=observations,
        storage=FakeStorage(tmp_path),
        metadata=FakeMetadata(gps=NOW),
        **defaults,
    )


def _request(name: str = "one.jpg", content: bytes = b"abc") -> ImportRequest:
    return ImportRequest(scope_id="album1", file_name=name, content_type="image/jpeg", content=content)


def test_the_merge_order_is_storage_gps_place_visual_observation_events_lexical(tmp_path, recorder, repos):
    result = build(tmp_path, recorder, repos).execute(_request())

    assert recorder.order == ["place", "visual", "observe", "events", "lexical"]
    assert result.event_id == "evt_1"
    assert result.lexical_terms == 3
    assert result.placed is True


def test_visual_vector_is_written_before_event_assignment(tmp_path, recorder, repos):
    """归并顺序里最要紧的一条：视觉向量先写，事件归属才读得到 ``visual_boost``。

    顺序反了**不报错**，只是 ``visual_boost`` 恒 0、``split_guard`` 更易命中——
    事件照分照建，只是分错了。
    """
    build(tmp_path, recorder, repos).execute(_request())

    assert recorder.order.index("visual") < recorder.order.index("events")


def test_place_resolution_precedes_event_assignment(tmp_path, recorder, repos):
    """事件的 ``place`` / ``admin_district`` 取的是 ``asset_places`` 那一行。"""
    build(tmp_path, recorder, repos).execute(_request())

    assert recorder.order.index("place") < recorder.order.index("events")


def test_lexical_projection_precedes_the_next_photo(tmp_path, recorder, repos):
    """词法投影挂在观察写入之后**逐条**写（1.0 的 refresh 存在但生产零调用）。"""
    build(tmp_path, recorder, repos).execute(_request())

    assert recorder.order.index("observe") < recorder.order.index("lexical")


def test_without_collaborators_the_import_is_still_an_import(tmp_path, recorder, repos):
    """协作者全是可选的：不传就退化成纯导入，事件与投影都不发生。"""
    result = build(
        tmp_path,
        recorder,
        repos,
        visual_indexer=None,
        place_indexer=None,
        observer=None,
        event_assigner=None,
        lexical_index=None,
    ).execute(_request())

    assert recorder.order == []
    assert result.event_id is None
    assert result.lexical_terms == 0
    assert result.status.value == "processed"


def test_deduplicated_import_reruns_only_the_idempotent_steps(tmp_path, recorder, repos):
    """重复导入是使用者唯一的补救动作，但只有幂等的那几步能重跑。

    视觉向量与地点是 asset 的**属性**（重算就地覆盖），事件归属不是——对同一条
    Observation 再跑一次可能给它开出第二个事件。
    """
    importer = build(tmp_path, recorder, repos)
    importer.execute(_request())
    recorder.order = []

    again = importer.execute(_request())

    assert again.deduplicated is True
    # 去重路径上两个幂等的协作方都会重跑；事件与词法不会（见模块 docstring）
    assert sorted(recorder.order) == ["place", "visual"]
    assert again.event_id is None


def test_non_image_upload_skips_metadata_and_place(tmp_path, recorder, repos):
    result = build(tmp_path, recorder, repos).execute(
        ImportRequest(scope_id="album1", file_name="note.txt", content_type="text/plain", content=b"hi")
    )

    assert result.media_type is MediaType.TEXT
    assert result.placed is False
    assert "place" not in recorder.order


def test_completed_invalid_json_marks_pipeline_asset_failed_without_empty_memory(
    tmp_path, recorder, repos, monkeypatch
):
    from dataclasses import replace

    import httpx

    from sentrix.application.media.import_album import AlbumProcessRequest, ProcessAlbumPipeline
    from sentrix.application.media.observe_asset import ObserveAsset
    from sentrix.domain.media.models import AssetStatus
    from sentrix.infrastructure.assistant.model_endpoint import ServedModel
    from sentrix.infrastructure.media.vlm_observer import OpenAIVlmObserver

    assets, observations = repos
    calls = []
    monkeypatch.setattr(ServedModel, "current", lambda self: "test-vlm")

    def received(*args, **kwargs):
        calls.append(kwargs["json"])
        return httpx.Response(
            200,
            request=httpx.Request("POST", "http://model.invalid/v1/chat/completions"),
            json={"choices": [{"message": {"content": "PRIVATE_INVALID_JSON"}, "finish_reason": "stop"}]},
        )

    monkeypatch.setattr(httpx, "post", received)
    observer = ObserveAsset(
        observations=observations,
        observer=OpenAIVlmObserver(
            "http://model.invalid/v1", ServedModel("http://model.invalid/v1", json_output_mode="prompt_only")
        ),
    )
    importer = build(tmp_path, recorder, repos, observer=observer)
    image = BytesIO()
    Image.new("RGB", (12, 8), "red").save(image, format="JPEG")
    staged = importer.stage(replace(_request(content=image.getvalue()), defer_processing=True))
    pipeline = ProcessAlbumPipeline(importer=importer, assets=assets, workers=1)
    result = pipeline.execute(AlbumProcessRequest(scope_id="album1", asset_ids=(staged.asset.id,)))
    assert result.processed == 0 and len(result.failures) == 1
    assert "model_output_invalid" in result.failures[0].reason
    assert "PRIVATE" not in result.failures[0].reason
    assert assets.get(staged.asset.id).status is AssetStatus.FAILED
    assert recorder.order == []  # No empty observation or derived memory was committed.
    assert len(calls) == 1 and "response_format" not in calls[0]
