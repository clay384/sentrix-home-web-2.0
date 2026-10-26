"""整册导入：把一个目录里的照片串行走一遍完整导入链。

对应 1.0 ``pipeline.process_batch`` 与批量导入端点的时序，三条性质来自 1.0 的实测：

- **两段式：推理并行、提交串行。** 1.0 的批量路径本来就是 4 个 worker
  （``app.py:483`` 的 ``ThreadPoolExecutor``，实测 ``effective_workers: 4``、11.78 秒/张），
  而 2.0 原先逐张串行（实测 49.1 秒/张，49.1 ÷ 4 ≈ 12.3）。原先这里写的是「串行是产品
  约束」，被这两组数直接否掉：全过程 GPU 88% 忙，差距就是并行度。
  **并行的只有推理**（``ImportAssets.prepare_image``：视觉嵌入 / VLM 观察 / 人脸检测），
  提交仍按 ``sorted(paths)`` 串行——事件归属读的是同批前几张刚写进去的事件、向量与
  提及，**提交顺序是语义的一部分**（见 ``ImportAssets.commit_image``）。
- **可恢复**：一张失败不中断整批，失败的文件名与原因随结果一起返回。重跑同一批时
  已导入的照片走 SHA-256 去重（只重跑视觉向量与地点，不重复建事件，见 ``ImportAssets``）。
- **每张最多 3 次模型调用**：那个上限属于观察器（首轮 / 补全 / 中文化，见
  ``infrastructure/media/vlm_observer.py``）。批末的事件总结另算：**每个事件一次**
  （第 48 条），整册 53 张折成 17 个事件就是 17 次——不是每张一次。

2.0 **不管理模型进程的生命周期**：8100/8101 由部署方起，这里只是它们的客户端。

**事件总结在批末，不是在每张之后**——这是 1.0 的时机，不是这里挑的。1.0 的共享
批量路径（``app.py:616-660`` 的 ``process_ingest_batch``）走的是
``commit_semantic_image(summarize_event=False)``，逐张**不总结**；等所有照片都
``complete`` 之后，``claim_ingest_batch_summary(batch_id)`` → ``batch_event_ids(batch_id)``
→ 逐个 ``_summarize_event_worker(event_id)``（``app.py:647-653``，同样的收尾也在
``_process_ingest_asset_group(finalize_batch=True)``，``app.py:577-581``）。
1.0 只总结**这一批碰过的事件**，不是整个 scope 的全部事件——所以这里收集的是本次
循环里出现过的 ``event_id``，去重后逐个总结。

逐张那条路（``pipeline.process()``，``pipeline.py:355-356``）的 ``summarize_event=True``
是另一个入口的默认值，整册导入不走它。**一张照片折叠进已有事件时，前面那张照片
触发的总结不会被重跑**——1.0 亦然（它按事件 id 总结，一个事件只跑一次）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from sentrix.application.media.import_assets import (
    ImportAssets,
    ImportRequest,
    ImportResult,
    PreparedImage,
    StagedImport,
)
from sentrix.domain.media.models import Asset, AssetStatus
from sentrix.domain.media.ports import AssetRepository

log = logging.getLogger("sentrix.media.import_album")

# 与 ``MediaType.from_upload`` 认得的图片后缀一致。目录扫描按后缀，不看内容类型——
# 整册导入拿到的是磁盘上的文件，没有 upload 的 content-type 可依。
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"})

# 视频后缀，与 ``MediaType.from_file_name`` 认得的那一组逐字一致（S8a T506）。
# **原先只看图片**：目录里的视频被静默跳过，而结果里看不出少过东西——
# 「导入了一整册，视频一个都不在」与「这一册真的没有视频」是同一个输出。
VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".m4v", ".avi", ".mkv"})

# 音频后缀，与 ``MediaType.from_file_name`` 认得的音频那一组逐字一致（S8c T521）。
# 与 T506 放行视频是同一条理由，只是这次轮到音频：目录里的录音被静默跳过时，
# 「导了一整册，录音一个都不在」与「这一册真的没有录音」是同一个输出。
AUDIO_SUFFIXES = frozenset({".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"})

# 扫描的实际判据 = 图片 ∪ 视频 ∪ 音频。
SCAN_SUFFIXES = IMAGE_SUFFIXES | VIDEO_SUFFIXES | AUDIO_SUFFIXES

DEFAULT_ALBUM_LIMIT = 200

# 预取窗口的默认宽度 = 同时在飞的推理数。
# **1 就是「不并行」**：单张、或者模型端点只有单槽时（``SENTRIX_ALBUM_WORKERS=1``）
# 退回原先的逐张路径，行为与拆分前一致。路由按 ``Settings.album_workers`` 传。
DEFAULT_ALBUM_WORKERS = 1


@dataclass(frozen=True, slots=True)
class AlbumImportRequest:
    scope_id: str
    directory: str
    limit: int = DEFAULT_ALBUM_LIMIT


@dataclass(frozen=True, slots=True)
class AlbumFailure:
    file_name: str
    reason: str


@dataclass(frozen=True, slots=True)
class AlbumImportResult:
    scope_id: str
    directory: str
    imported: int = 0
    deduplicated: int = 0
    events: int = 0
    lexical_terms: int = 0
    summarized: int = 0
    failures: tuple[AlbumFailure, ...] = field(default_factory=tuple)
    #: **分项耗时**（秒）。2.0 的导入是同步的——VLM 观察、事件归属都在
    #: `POST /imports` 这一个请求里跑完，所以不拆的话 64 分钟全记在「导入」上，
    #: 读起来像「复制文件花了 64 分钟」。键：`stage`（落库+读文件）、
    #: `prepare`（VLM 观察，并行）、`finish`（归属+写入）、`summarize`（事件总结）。
    timings: dict[str, float] = field(default_factory=dict)

    @property
    def scanned(self) -> int:
        return self.imported + self.deduplicated + len(self.failures)


@dataclass(frozen=True, slots=True)
class AlbumStageResult:
    """只负责把目录里的媒体落盘并登记为 ``queued``，不做任何记忆推理。"""

    scope_id: str
    directory: str
    imported: int = 0
    deduplicated: int = 0
    asset_ids: tuple[str, ...] = ()
    failures: tuple[AlbumFailure, ...] = ()
    timings: dict[str, float] = field(default_factory=dict)

    @property
    def scanned(self) -> int:
        return self.imported + self.deduplicated + len(self.failures)


@dataclass(frozen=True, slots=True)
class AlbumProcessRequest:
    scope_id: str
    # 顺序就是目录扫描顺序；事件提交必须严格遵守它，不能按推理完成顺序提交。
    asset_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AlbumProcessResult:
    scope_id: str
    processed: int = 0
    skipped: int = 0
    events: int = 0
    lexical_terms: int = 0
    video_scenes: int = 0
    summarized: int = 0
    failures: tuple[AlbumFailure, ...] = ()
    timings: dict[str, float] = field(default_factory=dict)


class ImportAlbum:
    def __init__(
        self,
        *,
        importer: ImportAssets,
        summarize_event: Callable[[str], object] | None = None,
        workers: int = DEFAULT_ALBUM_WORKERS,
        progress: Callable[..., None] | None = None,
    ) -> None:
        self._progress = progress
        self._importer = importer
        self._workers = max(1, int(workers))
        # 不传就退化成纯导入（与 ``ImportAssets`` 的其余可选协作者同一个形状）：
        # 事件留在 ``待总结事件`` / ``待判断`` 两个占位值上，与 1.0 没跑收尾时一样。
        self._summarize_event = summarize_event

    def execute(self, request: AlbumImportRequest) -> AlbumImportResult:
        root, paths = self._paths(request)

        self._report(stage="media_processing", total=len(paths))
        imported = deduplicated = events = lexical_terms = 0
        # 分项计时：同步导入里这些阶段是**交织**的（prepare 在池里跑、finish 按序收），
        # 所以 prepare 取「总墙钟 − 其余三项」，保证四项相加就是这次导入的真实耗时。
        t_start = time.perf_counter()
        stage_seconds = 0.0
        finish_seconds = 0.0
        prepare_seconds = 0.0
        event_ids: list[str] = []
        # 按**下标**归集失败，最后按序输出——原先逐张跑时它们天然是路径序，
        # 现在 stage 与 finish 两处都可能失败，混着 append 会把顺序打乱。
        failures: dict[int, AlbumFailure] = {}
        staged: dict[int, StagedImport | ImportResult | None] = {}
        pending: dict[int, Future[PreparedImage]] = {}
        next_submit = 0
        next_finish = 0

        with ThreadPoolExecutor(max_workers=self._workers, thread_name_prefix="sentrix-album") as pool:
            while next_finish < len(paths):
                # 预取：串行地「落库 + 起一次推理」。窗口按**下标距离**算，不按在飞的
                # 任务数——去重命中的那些不进池，按任务数算会让整册都是去重时一次把
                # 全部文件读进内存。
                while next_submit < len(paths) and next_submit - next_finish < self._workers:
                    index = next_submit
                    next_submit += 1
                    self._report(stage="media_staging", current_file=paths[index].name)
                    stage_started = time.perf_counter()
                    try:
                        outcome = self._importer.stage(self._request_for(request, paths[index]))
                    except Exception as error:  # 一张失败不中断整批——整册导入是几小时的事
                        staged[index] = None
                        failures[index] = AlbumFailure(
                            file_name=paths[index].name, reason=f"{type(error).__name__}: {error}"
                        )
                        continue
                    stage_seconds += time.perf_counter() - stage_started
                    staged[index] = outcome
                    if isinstance(outcome, StagedImport):
                        pending[index] = pool.submit(self._importer.prepare_image, outcome.asset)

                # **按 ``sorted(paths)`` 的顺序提交，不能用完成序。** 事件归属读的是同 scope
                # 的活动事件与它们的向量 / 提及，其中包含同批前几张照片刚写进去的那些；
                # 换一个顺序会改变「折进哪个事件」的结果，而它**不报错**。
                index = next_finish
                next_finish += 1
                # 换一个名字：``outcome`` 在预取那一段里已被推成非 None 的类型。
                ready = staged.pop(index)
                self._report(
                    stage="media_processing",
                    current_file=paths[index].name,
                    completed=index,
                    imported=imported,
                    deduplicated=deduplicated,
                    failed=index - imported - deduplicated,
                )
                if ready is None:
                    self._report(completed=index + 1, failed=index + 1 - imported - deduplicated)
                    continue
                future = pending.pop(index, None)
                # **等模型 ≠ 归属。** `future.result()` 是 VLM 观察的等待时间，把它
                # 算进 finish 会让「归属」看起来吃掉整段导入（实测 3175s vs 真实的
                # 几十秒）。等待单独计到 prepare，finish 只计落库归属本身。
                prepared = None
                finish_started = time.perf_counter()
                try:
                    if future is not None:
                        wait_started = time.perf_counter()
                        prepared = future.result()
                        prepare_seconds += time.perf_counter() - wait_started
                        finish_started = time.perf_counter()
                    if future is None:
                        assert isinstance(ready, ImportResult)
                        result = ready
                    else:
                        assert isinstance(ready, StagedImport)
                        assert prepared is not None
                        self._report(stage="event_assignment")
                        result = self._importer.finish(ready, prepared)
                except Exception as error:
                    failures[index] = AlbumFailure(
                        file_name=paths[index].name, reason=f"{type(error).__name__}: {error}"
                    )
                    self._report(completed=index + 1, failed=index + 1 - imported - deduplicated)
                    continue
                finish_seconds += time.perf_counter() - finish_started
                if result.deduplicated:
                    deduplicated += 1
                else:
                    imported += 1
                if result.event_id is not None:
                    events += 1
                    if result.event_id not in event_ids:  # 保序去重：一个事件只总结一次
                        event_ids.append(result.event_id)
                lexical_terms += result.lexical_terms
                self._report(
                    completed=index + 1,
                    imported=imported,
                    deduplicated=deduplicated,
                    failed=index + 1 - imported - deduplicated,
                )

        # 事件总结在批末，单独计时——它是一次模型调用/事件，跟导入/观察不是一回事。
        self._report(stage="event_summarization", current_file=None)
        summarize_started = time.perf_counter()
        summarize_count = self._summarize_events(event_ids)
        summarize_seconds = time.perf_counter() - summarize_started
        total_seconds = time.perf_counter() - t_start
        timings = {
            "stage": round(stage_seconds, 2),
            "finish": round(finish_seconds, 2),
            "summarize": round(summarize_seconds, 2),
            # 剩余 = VLM 观察（在池里并行跑，按墙钟算才有意义）
            # 并行等待的累计（可能大于墙钟，因为多个 worker 同时在等）。
            "prepare": round(prepare_seconds, 2),
            # 墙钟里没被三段时间覆盖的部分（线程调度、文件 IO 等）。
            "other": round(
                max(
                    0.0,
                    total_seconds - stage_seconds - finish_seconds - summarize_seconds - prepare_seconds,
                ),
                2,
            ),
            "total": round(total_seconds, 2),
        }

        return AlbumImportResult(
            timings=timings,
            scope_id=request.scope_id,
            directory=str(root),
            imported=imported,
            deduplicated=deduplicated,
            events=events,
            lexical_terms=lexical_terms,
            summarized=summarize_count,
            failures=tuple(failures[index] for index in sorted(failures)),
        )

    def stage_only(self, request: AlbumImportRequest) -> AlbumStageResult:
        """只导入资产：目录扫描、去重、落盘、EXIF/ffprobe 与资产登记。

        新资产状态写成 ``queued``；VLM/OCR、人脸、事件、事实、实体、关键帧与图结构都
        不在这里执行。既有 ``execute`` 完整导入入口保持不变，兼容产品端原调用方。
        """
        root, paths = self._paths(request)
        total_started = time.perf_counter()
        imported = deduplicated = 0
        asset_ids: list[str] = []
        failures: list[AlbumFailure] = []
        for path in paths:
            try:
                outcome = self._importer.stage(self._request_for(request, path, defer_processing=True))
            except Exception as error:  # 一张坏媒体不阻断整册
                failures.append(AlbumFailure(file_name=path.name, reason=f"{type(error).__name__}: {error}"))
                continue
            asset_id = outcome.asset_id if isinstance(outcome, ImportResult) else outcome.asset.id
            asset_ids.append(asset_id)
            if isinstance(outcome, ImportResult):
                deduplicated += 1
            else:
                imported += 1
        total_seconds = time.perf_counter() - total_started
        return AlbumStageResult(
            scope_id=request.scope_id,
            directory=str(root),
            imported=imported,
            deduplicated=deduplicated,
            asset_ids=tuple(asset_ids),
            failures=tuple(failures),
            timings={"asset_import": round(total_seconds, 3), "total": round(total_seconds, 3)},
        )

    def _report(self, **changes: Any) -> None:
        if self._progress is not None:
            self._progress(**changes)

    @staticmethod
    def _paths(request: AlbumImportRequest) -> tuple[Path, list[Path]]:
        root = Path(request.directory)
        if not root.is_dir():
            raise NotADirectoryError(str(root))
        paths = sorted(
            path for path in root.iterdir() if path.is_file() and path.suffix.lower() in SCAN_SUFFIXES
        )[: max(1, int(request.limit))]
        return root, paths

    @staticmethod
    def _request_for(
        request: AlbumImportRequest, path: Path, *, defer_processing: bool = False
    ) -> ImportRequest:
        """磁盘上的一张图 → 一次导入请求。整册导入没有 upload 的 content-type 可依。"""
        return ImportRequest(
            scope_id=request.scope_id,
            file_name=path.name,
            content_type=None,
            content=path.read_bytes(),
            defer_processing=defer_processing,
        )

    def _summarize_events(self, event_ids: list[str]) -> int:
        """批末逐个总结。**一个事件的失败不中断整批**——与逐张那条同样的性质。

        总结失败不回滚任何东西：事件仍在，只是 title/event_type 还是占位值，
        重跑同一个 scope（``SummarizeEvent.execute`` 幂等，1.0 亦然）就能补上。
        """
        if self._summarize_event is None:
            return 0
        summarized = 0
        for event_id in event_ids:
            try:
                self._summarize_event(event_id)
            except Exception as error:  # noqa: BLE001 - 一个事件总结失败不该让整个相册归零
                log.warning("事件总结失败，事件保留占位内容: %s: %s", event_id, error)
                continue
            summarized += 1
        return summarized


class ProcessAlbumPipeline:
    """对已导入的源资产执行真实流水线：推理并行、提交按请求顺序串行。"""

    def __init__(
        self,
        *,
        importer: ImportAssets,
        assets: AssetRepository,
        summarize_event: Callable[[str], object] | None = None,
        workers: int = DEFAULT_ALBUM_WORKERS,
    ) -> None:
        self._importer = importer
        self._assets = assets
        self._summarize_event = summarize_event
        self._workers = max(1, int(workers))

    def execute(self, request: AlbumProcessRequest) -> AlbumProcessResult:
        total_started = time.perf_counter()
        # 保序去重，防止客户端拼多个目录时把同一资产重复提交。
        ordered_ids = list(dict.fromkeys(request.asset_ids))
        targets: list[Asset | None] = []
        failures: dict[int, AlbumFailure] = {}
        skipped = 0
        for index, asset_id in enumerate(ordered_ids):
            asset = self._assets.get(asset_id)
            if asset is None:
                failures[index] = AlbumFailure(asset_id, "asset not found")
                targets.append(None)
                continue
            if asset.scope_id != request.scope_id:
                failures[index] = AlbumFailure(asset.file_name, "asset scope mismatch")
                targets.append(None)
                continue
            if asset.parent_asset_id is not None:
                failures[index] = AlbumFailure(asset.file_name, "derived asset cannot be a pipeline source")
                targets.append(None)
                continue
            if asset.status is AssetStatus.PROCESSED:
                skipped += 1
                targets.append(None)
                continue
            if asset.status is not AssetStatus.QUEUED:
                failures[index] = AlbumFailure(asset.file_name, f"asset is not queued: {asset.status.value}")
                targets.append(None)
                continue
            targets.append(asset)

        processed = events = lexical_terms = video_scenes = 0
        event_ids: list[str] = []
        timing_totals: dict[str, float] = {}
        prepared: dict[int, Future[PreparedImage]] = {}
        next_submit = next_finish = 0

        with ThreadPoolExecutor(max_workers=self._workers, thread_name_prefix="sentrix-pipeline") as pool:
            while next_finish < len(targets):
                while next_submit < len(targets) and next_submit - next_finish < self._workers:
                    index = next_submit
                    next_submit += 1
                    asset = targets[index]
                    if asset is None:
                        continue
                    self._assets.update_status(asset.id, AssetStatus.PROCESSING)
                    prepared[index] = pool.submit(self._importer.prepare_image, asset)

                index = next_finish
                next_finish += 1
                asset = targets[index]
                if asset is None:
                    continue
                future = prepared.pop(index)
                try:
                    inference = future.result()
                    result = self._importer.finish(self._importer.resume(asset), inference)
                except Exception as error:  # 一张失败不中断后续资产
                    self._assets.update_status(asset.id, AssetStatus.FAILED)
                    failures[index] = AlbumFailure(asset.file_name, f"{type(error).__name__}: {error}")
                    continue
                self._assets.update_status(asset.id, AssetStatus.PROCESSED)
                result = replace(result, status=AssetStatus.PROCESSED)
                processed += 1
                lexical_terms += result.lexical_terms
                video_scenes += result.video_scenes
                if result.event_id is not None:
                    events += 1
                    if result.event_id not in event_ids:
                        event_ids.append(result.event_id)
                for key, value in result.timings.items():
                    if key == "total":
                        continue
                    timing_totals[key] = timing_totals.get(key, 0.0) + float(value)

        summarize_started = time.perf_counter()
        summarized = self._summarize_events(event_ids)
        timing_totals["event_summarize"] = time.perf_counter() - summarize_started
        total_seconds = time.perf_counter() - total_started
        timing_totals["pipeline_core"] = total_seconds
        return AlbumProcessResult(
            scope_id=request.scope_id,
            processed=processed,
            skipped=skipped,
            events=events,
            lexical_terms=lexical_terms,
            video_scenes=video_scenes,
            summarized=summarized,
            failures=tuple(failures[index] for index in sorted(failures)),
            timings={key: round(value, 3) for key, value in timing_totals.items()},
        )

    def _summarize_events(self, event_ids: list[str]) -> int:
        if self._summarize_event is None:
            return 0
        summarized = 0
        for event_id in event_ids:
            try:
                self._summarize_event(event_id)
            except Exception as error:  # noqa: BLE001 - 总结失败不回滚已构建记忆
                log.warning("流水线事件总结失败，保留占位内容: %s: %s", event_id, error)
                continue
            summarized += 1
        return summarized
