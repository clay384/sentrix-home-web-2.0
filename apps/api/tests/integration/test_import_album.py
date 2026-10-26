"""整册导入：**推理并行、提交串行**、可恢复、按后缀扫描。

GPU 是单卡，所以**并行的只有推理**（``ImportAssets.prepare_image``）；提交必须按
``sorted(paths)`` 串行——事件归属读的是同批前几张刚写进去的事件、向量与提及，
换一个提交顺序会改变「折进哪个事件」的结果，而它**不报错**。

这个文件钉三件事：

1. **提交按路径序**，与推理的完成序无关（``test_commits_follow_path_order_*``）；
2. 「可恢复」是几小时跑批的基本要求：一张坏图不该让整批归零；
3. 批末的事件总结一个事件只跑一次。
"""

from __future__ import annotations

import dataclasses
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from sentrix.application.media.import_album import (
    AlbumImportRequest,
    ImportAlbum,
)
from sentrix.application.media.import_assets import (
    ImportResult,
    PreparedImage,
    StagedImport,
)
from sentrix.domain.assistant.ports import ModelServiceError
from sentrix.domain.media.models import AssetStatus, MediaType


class StubImporter:
    """两段式的假导入器：``stage`` / ``prepare_image`` / ``finish``。

    **它记三本账，断言钉的是 ``finished``。** ``staged`` 是落库序（串行、按路径），
    ``prepared`` 是推理完成序（并行，**不保证**与路径序一致），``finished`` 是提交序。
    原先这里只有一本 ``calls`` 并写着「证明是串行的」——它证明的是调用序；而现在
    真正有语义的是**提交序**：事件归属读同批前面照片刚写进去的事件。
    """

    def __init__(
        self,
        *,
        failing: frozenset[str] = frozenset(),
        dedup: frozenset[str] = frozenset(),
        slow: dict[str, float] | None = None,
    ) -> None:
        self.staged: list[str] = []
        self.prepared: list[str] = []
        self.finished: list[str] = []
        self._failing = failing
        self._dedup = dedup
        # 让某几张的推理故意慢下来——用来证明**完成序不影响提交序**。
        self._slow = slow or {}
        self._lock = threading.Lock()

    def stage(self, request):  # noqa: ANN001
        name = request.file_name
        self.staged.append(name)
        if name in self._failing:
            raise RuntimeError(f"坏图: {name}")
        if name in self._dedup:
            # 去重命中：没有可并行的一段，直接给出结果（与真实现的分支一致）。
            return ImportResult(
                asset_id=f"asset_{name}",
                observation_id=None,
                media_type=MediaType.IMAGE,
                status=AssetStatus.PROCESSED,
                deduplicated=True,
                indexed=True,
                placed=True,
            )
        return StagedImport(
            # 假件只需要这两格：``finish`` 用它辨认是哪一张，``prepare_image`` 只看文件名。
            asset=SimpleNamespace(id=f"asset_{name}", file_name=name),  # type: ignore[arg-type]
            media_type=MediaType.IMAGE,
        )

    def prepare_image(self, asset):  # noqa: ANN001
        time.sleep(self._slow.get(asset.file_name, 0.0))
        with self._lock:
            self.prepared.append(asset.file_name)
        return PreparedImage()

    def finish(self, staged, prepared):  # noqa: ANN001
        name = staged.asset.file_name
        self.finished.append(name)
        return ImportResult(
            asset_id=f"asset_{name}",
            observation_id="obs_1",
            media_type=MediaType.IMAGE,
            status=AssetStatus.PROCESSED,
            deduplicated=False,
            indexed=True,
            placed=True,
            event_id="evt_1",
            lexical_terms=4,
        )


@pytest.fixture
def album(tmp_path: Path) -> Path:
    root = tmp_path / "album"
    root.mkdir()
    for name in ("b.jpg", "a.JPG", "c.png", "notes.txt"):
        (root / name).write_bytes(b"content-" + name.encode())
    return root


def test_scan_is_sorted_and_ignores_non_images(album: Path):
    importer = StubImporter()
    result = ImportAlbum(importer=importer).execute(
        AlbumImportRequest(scope_id="album1", directory=str(album))
    )

    assert importer.finished == ["a.JPG", "b.jpg", "c.png"]
    assert result.imported == 3
    assert result.events == 3
    assert result.lexical_terms == 12


def test_commits_follow_path_order_even_when_preparation_finishes_out_of_order(tmp_path: Path) -> None:
    """**提交序 = 路径序，与推理完成序无关。**

    让第一张的推理故意慢过后面几张：完成序因此是 b、c、a，而提交仍必须是 a、b、c。
    事件归属读的是同 scope 的活动事件与它们的向量 / 提及，其中包含同批前几张刚写
    进去的那些——换一个提交顺序会改变「折进哪个事件」的结果，而它**不报错**。
    对照 1.0 ``app.py:490-496``：提交在驱动线程上按 ``image_ids`` 的下标推进，
    不是 ``as_completed``。
    """
    root = tmp_path / "ordered"
    root.mkdir()
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        (root / name).write_bytes(b"x")
    importer = StubImporter(slow={"a.jpg": 0.3})

    result = ImportAlbum(importer=importer, workers=3).execute(
        AlbumImportRequest(scope_id="album1", directory=str(root))
    )

    assert result.imported == 3
    # 先确认这条用例真的把完成序打乱了——否则它在测别的东西。
    assert importer.prepared == ["b.jpg", "c.jpg", "a.jpg"]
    assert importer.finished == ["a.jpg", "b.jpg", "c.jpg"]


def test_one_bad_photo_does_not_stop_the_batch(album: Path):
    importer = StubImporter(failing=frozenset({"b.jpg"}))
    result = ImportAlbum(importer=importer).execute(
        AlbumImportRequest(scope_id="album1", directory=str(album))
    )

    # 断言用 ``staged``（尝试序）而不是 ``finished``（提交序）：坏图在落库那一步
    # 就抛了，根本到不了提交——「不中断整批」说的是后面那张照样被尝试过。
    assert importer.staged == ["a.JPG", "b.jpg", "c.png"]
    assert importer.finished == ["a.JPG", "c.png"]
    assert result.imported == 2
    assert [item.file_name for item in result.failures] == ["b.jpg"]
    assert "坏图" in result.failures[0].reason


def test_failures_carry_the_exception_type(album: Path):
    importer = StubImporter(failing=frozenset({"c.png"}))

    result = ImportAlbum(importer=importer).execute(
        AlbumImportRequest(scope_id="album1", directory=str(album))
    )

    assert result.failures[0].reason.startswith("RuntimeError:")


def test_rerunning_the_same_album_deduplicates(album: Path):
    importer = StubImporter(dedup=frozenset({"a.JPG", "b.jpg", "c.png"}))

    result = ImportAlbum(importer=importer).execute(
        AlbumImportRequest(scope_id="album1", directory=str(album))
    )

    assert result.imported == 0
    assert result.deduplicated == 3
    assert result.events == 0
    assert result.scanned == 3


def test_limit_caps_the_batch(album: Path):
    importer = StubImporter()

    result = ImportAlbum(importer=importer).execute(
        AlbumImportRequest(scope_id="album1", directory=str(album), limit=1)
    )

    assert importer.finished == ["a.JPG"]
    assert result.imported == 1


def test_missing_directory_is_an_error_not_an_empty_result(tmp_path: Path):
    """目录不存在必须报错：空结果会让「路径写错了」看起来像「相册是空的」。"""
    with pytest.raises(NotADirectoryError):
        ImportAlbum(importer=StubImporter()).execute(
            AlbumImportRequest(scope_id="album1", directory=str(tmp_path / "gone"))
        )


# ---- 批末事件总结（1.0 ``process_ingest_batch`` 的收尾时机） ------------------------


class RecordingSummarizer:
    """记录被总结的事件 id 与调用顺序。"""

    def __init__(self, *, failing: frozenset[str] = frozenset()) -> None:
        self.calls: list[str] = []
        self._failing = failing

    def __call__(self, event_id: str) -> str:
        self.calls.append(event_id)
        if event_id in self._failing:
            raise RuntimeError(f"总结挂了: {event_id}")
        return event_id


class PerFileEventImporter(StubImporter):
    """每个文件开一个自己的事件，用来验「一个事件只总结一次」。

    挂点从 ``execute`` 挪到了 ``finish``：整册导入现在只调 ``stage`` /
    ``prepare_image`` / ``finish``，而事件 id 是**提交**那一步产出的。
    """

    def finish(self, staged, prepared):  # noqa: ANN001
        result = super().finish(staged, prepared)
        return dataclasses.replace(result, event_id=f"evt_{staged.asset.file_name}")


def test_events_are_summarized_once_each_after_the_whole_batch(album: Path):
    """1.0 的时机：所有照片都进完之后，按事件 id 逐个总结（``app.py:647-653``）。"""
    summarizer = RecordingSummarizer()
    importer = StubImporter()
    album_import = ImportAlbum(importer=importer, summarize_event=summarizer)

    result = album_import.execute(AlbumImportRequest(scope_id="album1", directory=str(album)))

    # 三张图折进同一个事件 —— 总结只跑一次，且跑在导入之后。
    assert summarizer.calls == ["evt_1"]
    assert result.summarized == 1
    assert importer.finished == ["a.JPG", "b.jpg", "c.png"]


def test_summary_covers_every_distinct_event_and_nothing_else(album: Path):
    summarizer = RecordingSummarizer()

    result = ImportAlbum(importer=PerFileEventImporter(), summarize_event=summarizer).execute(
        AlbumImportRequest(scope_id="album1", directory=str(album))
    )

    assert summarizer.calls == ["evt_a.JPG", "evt_b.jpg", "evt_c.png"]
    assert result.summarized == 3


def test_one_failing_summary_does_not_stop_the_rest(album: Path):
    """与逐张那条同样的性质：一个事件的收尾失败不该让别的照片没有标题。"""
    summarizer = RecordingSummarizer(failing=frozenset({"evt_b.jpg"}))

    result = ImportAlbum(importer=PerFileEventImporter(), summarize_event=summarizer).execute(
        AlbumImportRequest(scope_id="album1", directory=str(album))
    )

    assert summarizer.calls == ["evt_a.JPG", "evt_b.jpg", "evt_c.png"]  # 失败之后继续
    assert result.summarized == 2
    assert result.imported == 3


def test_without_a_summarizer_events_keep_their_placeholder(album: Path):
    """不传总结器就退化成纯导入——事件留在 ``待总结事件``，与 1.0 没跑收尾时一样。"""
    result = ImportAlbum(importer=StubImporter()).execute(
        AlbumImportRequest(scope_id="album1", directory=str(album))
    )

    assert result.summarized == 0
    assert result.events == 3


# ---- S8a T506：整册导入放行视频 ------------------------------------------------------


def test_videos_are_scanned_alongside_photos(tmp_path: Path):
    """目录里的视频原先被静默跳过：`IMAGE_SUFFIXES` 只看图片，而结果里看不出少过东西。

    这一条钉的是 `SCAN_SUFFIXES = IMAGE_SUFFIXES | VIDEO_SUFFIXES`：把视频那半边删掉，
    `b.mov` / `c.MP4` / `d.mkv` 立刻从 `calls` 里消失（大小写不敏感也一起钉住）。
    """
    root = tmp_path / "album"
    root.mkdir()
    for name in ("a.jpg", "b.mov", "c.MP4", "d.mkv", "notes.txt"):
        (root / name).write_bytes(b"content-" + name.encode())

    importer = StubImporter()
    result = ImportAlbum(importer=importer).execute(
        AlbumImportRequest(scope_id="album1", directory=str(root))
    )

    assert importer.finished == ["a.jpg", "b.mov", "c.MP4", "d.mkv"]
    assert result.imported == 4


# ---- S8c T521：整册导入放行音频 ------------------------------------------------------


def test_audio_is_scanned_alongside_photos_and_videos(tmp_path: Path):
    """与放行视频（T506）同一条理由，这次轮到音频：目录里的录音被静默跳过时，
    「导了一整册，录音一个都不在」与「这一册真的没有录音」是同一个输出。

    这一条钉的是 `SCAN_SUFFIXES` 里 `AUDIO_SUFFIXES` 那一半：删掉它，
    `b.m4a` / `c.MP3` / `d.flac` 立刻从 `calls` 里消失（大小写不敏感一起钉住）。
    """
    root = tmp_path / "album"
    root.mkdir()
    for name in ("a.jpg", "b.m4a", "c.MP3", "d.flac", "notes.txt"):
        (root / name).write_bytes(b"content-" + name.encode())

    importer = StubImporter()
    result = ImportAlbum(importer=importer).execute(
        AlbumImportRequest(scope_id="album1", directory=str(root))
    )

    assert importer.finished == ["a.jpg", "b.m4a", "c.MP3", "d.flac"]
    assert result.imported == 4


def test_progress_counts_settled_media_and_keeps_batch_summary_running(album: Path):
    """One new, one duplicate, one bad media; summary is not a completed import."""
    from sentrix.application.media.track_import import TrackImport
    from sentrix.infrastructure.media.import_progress import InProcessImportProgressStore

    store = InProcessImportProgressStore()
    tracker = TrackImport(store, "op", "scope")
    observations = []

    def report(**changes):
        tracker.update(**changes)
        observations.append(store.get("op"))

    def summarize(event_id):
        observed = store.get("op")
        assert observed.status == "running"
        assert observed.stage == "event_summarization"
        assert observed.completed == observed.total == 3

    result = ImportAlbum(
        importer=StubImporter(failing=frozenset({"c.png"}), dedup=frozenset({"a.JPG"})),
        summarize_event=summarize,
        workers=3,
        progress=report,
    ).execute(AlbumImportRequest(scope_id="scope", directory=str(album)))
    assert (result.imported, result.deduplicated, len(result.failures)) == (1, 1, 1)
    assert all(p.completed == p.imported + p.deduplicated + p.failed for p in observations)
    assert [p.completed for p in observations] == sorted(p.completed for p in observations)
    assert store.get("op").status == "running"  # route still has participants / insights to execute


def test_album_summary_operational_failure_is_not_counted_as_success(album):
    failure = ModelServiceError("output limit", error_code="output_limit")
    calls = []

    def summarize(event_id):
        calls.append(event_id)
        raise failure

    importer = StubImporter()
    with pytest.raises(ModelServiceError) as caught:
        ImportAlbum(importer=importer, summarize_event=summarize).execute(
            AlbumImportRequest(scope_id="album1", directory=str(album))
        )
    assert caught.value is failure
    assert len(importer.finished) == 3
    assert calls == ["evt_1"]


def test_explicit_pipeline_summary_operational_failure_propagates():
    from sentrix.application.media.import_album import AlbumProcessRequest, ProcessAlbumPipeline

    asset = SimpleNamespace(
        id="asset_one",
        file_name="one.jpg",
        scope_id="album1",
        parent_asset_id=None,
        status=AssetStatus.QUEUED,
    )

    class Assets:
        def get(self, asset_id):
            return asset if asset_id == asset.id else None

        def update_status(self, _asset_id, status):
            asset.status = status

    class Importer(StubImporter):
        def resume(self, source):
            return StagedImport(asset=source, media_type=MediaType.IMAGE)

    failure = ModelServiceError("time limit", error_code="time_limit")

    def summarize(_event_id):
        raise failure

    with pytest.raises(ModelServiceError) as caught:
        ProcessAlbumPipeline(importer=Importer(), assets=Assets(), summarize_event=summarize).execute(
            AlbumProcessRequest(scope_id="album1", asset_ids=(asset.id,))
        )
    assert caught.value is failure
    assert asset.status is AssetStatus.PROCESSED  # persisted media retained; batch operation failed
