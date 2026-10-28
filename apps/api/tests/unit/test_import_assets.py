import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sentrix.application.media.import_assets import ImportAssets, ImportRequest
from sentrix.domain.assistant.ports import ModelServiceError
from sentrix.domain.media.models import (
    Asset,
    AssetStatus,
    AudioMetadata,
    GpsPoint,
    MediaType,
    VideoMetadata,
)


class FakeAssets:
    def __init__(self) -> None:
        self.rows: dict[str, Asset] = {}

    def add(self, asset):
        self.rows[asset.id] = asset

    def get(self, asset_id):
        return self.rows.get(asset_id)

    def list_by_scope(self, scope_id, limit):
        return [a for a in self.rows.values() if a.scope_id == scope_id][:limit]

    def find_by_hash(self, scope_id, content_sha256):
        return next(
            (a for a in self.rows.values() if a.scope_id == scope_id and a.content_sha256 == content_sha256),
            None,
        )

    def update_status(self, asset_id, status):
        # Asset 是 frozen+slots dataclass，没有 __dict__，必须用 dataclasses.replace
        self.rows[asset_id] = dataclasses.replace(self.rows[asset_id], status=status)


class FakeObservations:
    def __init__(self) -> None:
        self.rows = {}

    def add(self, observation):
        self.rows[observation.id] = observation

    def get(self, observation_id):
        return self.rows.get(observation_id)


class FakeStorage:
    def __init__(self) -> None:
        self.stored: dict[str, bytes] = {}

    def store(self, asset_id, file_name, content):
        from pathlib import Path

        self.stored[asset_id] = content
        return Path(f"/media/{asset_id}_{file_name}")

    def read(self, asset_id):
        return self.stored[asset_id]

    def path_for(self, asset):
        from pathlib import Path

        return Path(asset.path)


class FakeMetadata:
    def __init__(self, captured_at=None, gps=None, device=None) -> None:
        self._captured_at, self._gps, self._device = captured_at, gps, device

    def read(self, path):
        return self._captured_at, self._gps, self._device


def build(metadata=None):
    assets, observations, storage = FakeAssets(), FakeObservations(), FakeStorage()
    use_case = ImportAssets(
        assets=assets,
        observations=observations,
        storage=storage,
        metadata=metadata or FakeMetadata(),
    )
    return use_case, assets, observations, storage


def test_import_creates_asset_and_observation():
    use_case, assets, observations, storage = build()
    request = ImportRequest(
        scope_id="album1",
        file_name="IMG_0001.HEIC",
        content_type="image/heic",
        content=b"raw bytes",
    )

    result = use_case.execute(request)

    assert result.status is AssetStatus.PROCESSED
    assert result.media_type is MediaType.IMAGE
    asset = assets.get(result.asset_id)
    assert asset is not None
    assert asset.size_bytes == len(b"raw bytes")
    assert storage.stored[result.asset_id] == b"raw bytes"
    assert observations.get(result.observation_id) is not None


def test_identical_content_in_same_scope_is_deduplicated():
    use_case, assets, _, storage = build()
    request = ImportRequest(
        scope_id="album1",
        file_name="a.jpg",
        content_type="image/jpeg",
        content=b"same",
    )

    first = use_case.execute(request)
    second = use_case.execute(request)

    assert second.deduplicated is True
    assert second.asset_id == first.asset_id
    assert len(assets.rows) == 1
    assert len(storage.stored) == 1


def test_same_content_in_different_scopes_is_kept_separately():
    """1.0 的去重按 scope 隔离（fix(api): scope dedup in /api/ingest）。"""
    use_case, assets, _, _ = build()
    use_case.execute(
        ImportRequest(scope_id="album1", file_name="a.jpg", content_type="image/jpeg", content=b"same")
    )
    use_case.execute(
        ImportRequest(scope_id="album2", file_name="a.jpg", content_type="image/jpeg", content=b"same")
    )

    assert len(assets.rows) == 2


def test_metadata_is_carried_onto_the_asset():
    gps = GpsPoint(18.7883, 98.9853)
    captured = datetime(2018, 4, 1, 21, 5, 47, tzinfo=UTC)
    use_case, assets, _, _ = build(FakeMetadata(captured, gps, "iPhone X"))

    result = use_case.execute(
        ImportRequest(scope_id="album1", file_name="a.jpg", content_type="image/jpeg", content=b"x")
    )

    asset = assets.get(result.asset_id)
    assert asset.captured_at == captured
    assert asset.captured_location == gps


# ---- S8a：视频不再静默空转 ----------------------------------------------------------
#
# 这一组只验用例本身的判定：探测到了什么、失败被记到哪里、图片那条路有没有被带跑。
# 真 ffprobe 的抽取口径在 tests/integration/test_video_metadata.py，
# 装配点接线（build_import_assets 有没有把三件接上）在 tests/contract/test_video_routes.py。


class FakeVideoMetadata:
    """返回一条预置元数据；``error`` 非空时按真适配器的契约抛 ``ValueError``。"""

    def __init__(self, metadata=None, error: str = "") -> None:
        self._metadata = metadata
        self._error = error
        self.calls: list[str] = []

    def read(self, path):
        self.calls.append(str(path))
        if self._error:
            raise ValueError(self._error)
        return self._metadata


class FakeVideoTranscoder:
    """``target`` 给 None 表示「不用转」；``error`` 非空表示转码失败。"""

    def __init__(self, target=None, error: str = "") -> None:
        self._target, self._error = target, error
        self.calls: list[str] = []

    def browser_preview(self, asset_id, source, codec):
        self.calls.append(codec)
        if self._error:
            raise RuntimeError(self._error)
        return self._target


class FakeVideoRepository:
    def __init__(self) -> None:
        self.probes: dict[str, object] = {}
        self.processing_seconds: dict[str, float] = {}

    def save_probe(self, asset_id, probe):
        self.probes[asset_id] = probe

    def save_processing_seconds(self, asset_id, seconds):
        self.processing_seconds[asset_id] = seconds

    def probe(self, asset_id):
        return self.probes.get(asset_id)


class SteppingClock:
    """可替换的 ``clock``：每次调用前进 ``step``，并记下每次返回值。

    用例据此算期望耗时——这样「用 ``time.perf_counter`` 而不是注入时钟」的实现会被
    钉住（落在 ``processing_seconds`` 上的会是一个真实的小数，与 clock 的差对不上）。
    """

    def __init__(self, start: datetime, step_seconds: float) -> None:
        self._value = start
        self._step = timedelta(seconds=step_seconds)
        self.calls: list[datetime] = []

    def __call__(self) -> datetime:
        current = self._value
        self.calls.append(current)
        self._value = self._value + self._step
        return current


VIDEO_METADATA = VideoMetadata(
    captured_at="2026-08-02T14:55:56+08:00",
    latitude=22.4846,
    longitude=114.5438,
    captured_location="22.484600,114.543800",
    duration_sec=11.735,
    codec="hevc",
    device="Apple iPhone 14 Plus",
)


def build_video(*, metadata_reader, transcoder=None, clock=None, scene_extractor=None):
    assets, observations, storage = FakeAssets(), FakeObservations(), FakeStorage()
    videos = FakeVideoRepository()
    use_case = ImportAssets(
        assets=assets,
        observations=observations,
        storage=storage,
        metadata=FakeMetadata(),
        video_metadata=metadata_reader,
        video_transcoder=transcoder,
        video_repository=videos,
        video_scene_extractor=scene_extractor,
        clock=clock or (lambda: datetime.now(UTC)),
    )
    return use_case, assets, videos


def test_video_import_reads_metadata_and_records_the_probe():
    """T505：视频要读 ffprobe 元数据、写进 metadata_json，**不能收下了什么都不发生**。"""
    reader = FakeVideoMetadata(VIDEO_METADATA)
    use_case, assets, videos = build_video(
        metadata_reader=reader, transcoder=FakeVideoTranscoder(target=Path("/derived/preview.mp4"))
    )

    result = use_case.execute(
        ImportRequest(scope_id="album1", file_name="clip.mov", content_type=None, content=b"mov")
    )

    assert reader.calls, "视频必须真的走一次探测"
    assert result.video_probed is True
    probe = videos.probes[result.asset_id]
    assert probe.metadata is VIDEO_METADATA
    assert probe.browser_preview_path == "/derived/preview.mp4"
    # 拍摄时间/GPS 接到与 EXIF 同一条路上：否则视频的 captured_at 只会是导入时刻。
    asset = assets.get(result.asset_id)
    # 视频/音频的时间来自 ffprobe，带偏移；落库前统一转成本地 naive
    # （与 1.0 存的 `2017-09-27 11:36:15` 同口径，见 domain/media/models.py）。
    expected = datetime.fromisoformat("2026-08-02T06:55:56+00:00").astimezone().replace(tzinfo=None)
    assert asset.captured_at == expected
    assert asset.captured_location == GpsPoint(22.4846, 114.5438)


def test_video_probe_failure_is_recorded_not_swallowed():
    """探测失败照收，但原因要落在资产上——「收下了但没读出来」必须看得见。"""
    use_case, _, videos = build_video(metadata_reader=FakeVideoMetadata(error="ffprobe failed: boom"))

    result = use_case.execute(
        ImportRequest(scope_id="album1", file_name="clip.mov", content_type=None, content=b"mov")
    )

    assert result.video_probed is False
    probe = videos.probes[result.asset_id]
    assert probe.metadata is None
    assert "ffprobe failed: boom" in probe.probe_error


def test_transcode_failure_keeps_the_import_and_records_the_reason():
    """转码是增强：失败回落原片，但失败原因要留下来（1.0 的 browser_preview_error）。"""
    use_case, _, videos = build_video(
        metadata_reader=FakeVideoMetadata(VIDEO_METADATA),
        transcoder=FakeVideoTranscoder(error="ffmpeg exploded"),
    )

    result = use_case.execute(
        ImportRequest(scope_id="album1", file_name="clip.mov", content_type=None, content=b"mov")
    )

    assert result.video_probed is True
    probe = videos.probes[result.asset_id]
    assert probe.browser_preview_path == ""
    assert "ffmpeg exploded" in probe.browser_preview_error


def test_already_playable_video_is_not_transcoded():
    """H.264 直出：转码器返回 None 不是失败，preview_path 为空、也没有 error。"""
    transcoder = FakeVideoTranscoder(target=None)
    use_case, _, videos = build_video(
        metadata_reader=FakeVideoMetadata(VIDEO_METADATA), transcoder=transcoder
    )

    result = use_case.execute(
        ImportRequest(scope_id="album1", file_name="clip.mp4", content_type=None, content=b"mp4")
    )

    probe = videos.probes[result.asset_id]
    assert transcoder.calls == ["hevc"]
    assert probe.browser_preview_path == ""
    assert probe.browser_preview_error == ""


def test_image_import_never_touches_the_video_chain():
    """图片走 EXIF，一步都不进视频那条路——否则「图片也被 ffprobe 了一遍」不会报错。"""
    reader = FakeVideoMetadata(VIDEO_METADATA)
    transcoder = FakeVideoTranscoder(target=Path("/derived/preview.mp4"))
    use_case, _, videos = build_video(metadata_reader=reader, transcoder=transcoder)

    result = use_case.execute(
        ImportRequest(scope_id="album1", file_name="a.jpg", content_type="image/jpeg", content=b"jpg")
    )

    assert result.video_probed is False
    assert reader.calls == []
    assert transcoder.calls == []
    assert videos.probes == {}


def test_video_import_without_the_probe_chain_still_works():
    """协作者全缺时退化成旧行为（不报错）——与其余可选协作者同一形状。"""
    use_case, _, _ = build_video(metadata_reader=None)

    result = use_case.execute(
        ImportRequest(scope_id="album1", file_name="clip.mov", content_type=None, content=b"mov")
    )

    assert result.media_type is MediaType.VIDEO
    assert result.video_probed is False


def test_video_processing_seconds_cover_the_whole_stage_including_scene_extraction():
    """整个视频阶段的秒数要落库，且范围覆盖到**场景抽取之后**。

    1.0 的 ``video_processing_seconds`` 是整个视频处理阶段的耗时
    （``processor.py:205/420``），PhotoBench 的关键帧分析读它算 ``realtime_factor``。
    计时必须用注入的 ``clock``：这里比对「落库值」与「起止两次 clock 调用之差」——
    若实现改用 ``time.perf_counter``，落库值会是真实耗时而与这个差对不上。
    让场景抽取也耗掉一个 step，则「计时在抽帧前就结束」的实现会少算一截。
    """
    clock = SteppingClock(datetime(2026, 1, 1, tzinfo=UTC), step_seconds=1.25)

    def extract_scene(_asset_id: str) -> int:
        clock()  # 场景抽取这一步也消耗时间
        return 1

    use_case, _, videos = build_video(
        metadata_reader=FakeVideoMetadata(VIDEO_METADATA), clock=clock, scene_extractor=extract_scene
    )

    result = use_case.execute(
        ImportRequest(scope_id="album1", file_name="clip.mov", content_type=None, content=b"mov")
    )

    recorded = videos.processing_seconds[result.asset_id]
    expected = (clock.calls[-1] - clock.calls[0]).total_seconds()
    assert expected > 0
    assert recorded == pytest.approx(expected)


# ---- S8c：音频入库（T521） -----------------------------------------------------------
#
# 与视频那一组同形，只验用例本身的判定。真 ffprobe 的抽取口径在
# tests/integration/test_audio_metadata.py，装配点接线在 tests/contract/test_media_routes.py。


class FakeAudioMetadata:
    """返回一条预置元数据；``error`` 非空时按真适配器的契约抛 ``ValueError``。"""

    def __init__(self, metadata=None, error: str = "") -> None:
        self._metadata = metadata
        self._error = error
        self.calls: list[str] = []

    def read(self, path):
        self.calls.append(str(path))
        if self._error:
            raise ValueError(self._error)
        return self._metadata


class FakeAudioRepository:
    def __init__(self) -> None:
        self.probes: dict[str, object] = {}

    def save_probe(self, asset_id, probe):
        self.probes[asset_id] = probe


AUDIO_METADATA = AudioMetadata(
    captured_at="2026-08-02T14:55:56+08:00",
    latitude=22.4846,
    longitude=114.5438,
    captured_location="22.484600,114.543800",
    duration_sec=5.0,
    codec="aac",
    sample_rate=44100,
    channels=2,
    bit_rate=128000,
)


def build_audio(*, metadata_reader):
    assets, observations, storage = FakeAssets(), FakeObservations(), FakeStorage()
    audios = FakeAudioRepository()
    use_case = ImportAssets(
        assets=assets,
        observations=observations,
        storage=storage,
        metadata=FakeMetadata(),
        audio_metadata=metadata_reader,
        audio_repository=audios,
    )
    return use_case, assets, audios


def test_audio_import_reads_metadata_and_records_the_probe():
    """T521：音频要读 ffprobe 元数据、写进 metadata_json，**不能收下了什么都不发生**。"""
    reader = FakeAudioMetadata(AUDIO_METADATA)
    use_case, assets, audios = build_audio(metadata_reader=reader)

    result = use_case.execute(
        ImportRequest(scope_id="album1", file_name="clip.m4a", content_type=None, content=b"m4a")
    )

    assert result.media_type is MediaType.AUDIO
    assert reader.calls, "音频必须真的走一次探测"
    assert result.audio_probed is True
    assert audios.probes[result.asset_id].metadata is AUDIO_METADATA
    # 拍摄时间/GPS 接到与 EXIF/视频同一条路上：否则音频的 captured_at 只会是导入时刻。
    asset = assets.get(result.asset_id)
    # 视频/音频的时间来自 ffprobe，带偏移；落库前统一转成本地 naive
    # （与 1.0 存的 `2017-09-27 11:36:15` 同口径，见 domain/media/models.py）。
    expected = datetime.fromisoformat("2026-08-02T06:55:56+00:00").astimezone().replace(tzinfo=None)
    assert asset.captured_at == expected
    assert asset.captured_location == GpsPoint(22.4846, 114.5438)


def test_audio_probe_failure_is_recorded_not_swallowed():
    """探测失败照收，但原因要落在资产上——与视频 ``video_probe_error`` 同一条口径。"""
    use_case, _, audios = build_audio(metadata_reader=FakeAudioMetadata(error="ffprobe failed: boom"))

    result = use_case.execute(
        ImportRequest(scope_id="album1", file_name="clip.m4a", content_type=None, content=b"m4a")
    )

    assert result.audio_probed is False
    probe = audios.probes[result.asset_id]
    assert probe.metadata is None
    assert "ffprobe failed: boom" in probe.probe_error


def test_the_audio_delivery_mime_comes_from_the_suffix_not_the_uploader():
    """交付类型由后端按后缀定。

    上传方给不出具体类型（curl / 整册导入都是 ``application/octet-stream``）时，
    照抄它会让 ``<audio>`` 拿到一个播不了的类型，而音频没有转码件这条退路。
    """
    use_case, assets, _ = build_audio(metadata_reader=FakeAudioMetadata(AUDIO_METADATA))

    result = use_case.execute(
        ImportRequest(
            scope_id="album1",
            file_name="clip.m4a",
            content_type="application/octet-stream",
            content=b"m4a",
        )
    )

    assert assets.get(result.asset_id).mime_type == "audio/mp4"


def test_a_specific_audio_content_type_is_kept_as_is():
    """上传方给了具体的 ``audio/*`` 就用它的——**后缀映射盖不过已声明的类型**。

    故意选一个「后缀映射会给别的值」的组合（.m4a 的默认是 ``audio/mp4``，这里声明
    ``audio/x-m4a``），否则这条断言在「永远走后缀映射」的变异下也是绿的。
    """
    use_case, assets, _ = build_audio(metadata_reader=FakeAudioMetadata(AUDIO_METADATA))

    result = use_case.execute(
        ImportRequest(
            scope_id="album1",
            file_name="clip.m4a",
            content_type="audio/x-m4a",
            content=b"m4a",
        )
    )

    assert assets.get(result.asset_id).mime_type == "audio/x-m4a"


def test_image_and_video_never_touch_the_audio_chain():
    """图片与视频一步都不进音频那条路——否则「图片也被 ffprobe 了一遍」不会报错。"""
    reader = FakeAudioMetadata(AUDIO_METADATA)
    use_case, assets, audios = build_audio(metadata_reader=reader)

    image = use_case.execute(
        ImportRequest(scope_id="album1", file_name="a.jpg", content_type="image/jpeg", content=b"jpg")
    )
    video = use_case.execute(
        ImportRequest(scope_id="album1", file_name="clip.mov", content_type="video/quicktime", content=b"mov")
    )

    assert image.audio_probed is False
    assert video.audio_probed is False
    assert reader.calls == []
    assert audios.probes == {}
    # 视频的 mime 照旧来自上传方，不被音频那条后缀规则改掉。
    assert assets.get(video.asset_id).mime_type == "video/quicktime"


def test_audio_import_without_the_probe_chain_still_works():
    """协作者全缺时退化成旧行为（不报错）——与其余可选协作者同一形状。"""
    use_case, assets, _ = build_audio(metadata_reader=None)

    result = use_case.execute(
        ImportRequest(scope_id="album1", file_name="clip.m4a", content_type=None, content=b"m4a")
    )

    assert result.media_type is MediaType.AUDIO
    assert result.audio_probed is False
    assert assets.get(result.asset_id).mime_type == "audio/mp4"


def test_video_keyframe_model_operation_failure_propagates_without_quality_fallback():
    failure = ModelServiceError("time_limit", error_code="time_limit")

    def fail_scene(_asset_id):
        raise failure

    use_case, _, _ = build_video(
        metadata_reader=FakeVideoMetadata(VIDEO_METADATA), scene_extractor=fail_scene
    )
    with pytest.raises(ModelServiceError) as caught:
        use_case.execute(
            ImportRequest(scope_id="album1", file_name="clip.mov", content_type=None, content=b"mov")
        )
    assert caught.value is failure


def test_pipeline_marks_image_model_operation_failure_failed_and_reports_reason():
    from sentrix.application.media.import_album import AlbumProcessRequest, ProcessAlbumPipeline
    from sentrix.application.media.observe_asset import ObserveAsset

    class FailedVision:
        def observe(self, _path):
            raise ModelServiceError(
                "provider_rejected HTTP429", error_code="provider_rejected", http_status=429
            )

    use_case, assets, observations, _ = build()
    staged = use_case.execute(
        ImportRequest(
            scope_id="album1",
            file_name="one.jpg",
            content_type="image/jpeg",
            content=b"one",
            defer_processing=True,
        )
    )
    observer = ObserveAsset(observer=FailedVision(), observations=observations)
    use_case._observer = observer
    result = ProcessAlbumPipeline(importer=use_case, assets=assets, workers=1).execute(
        AlbumProcessRequest(scope_id="album1", asset_ids=(staged.asset_id,))
    )
    assert result.processed == 0
    assert assets.get(staged.asset_id).status is AssetStatus.FAILED
    assert len(result.failures) == 1
    assert "provider_rejected" in result.failures[0].reason
    assert "HTTP429" in result.failures[0].reason
