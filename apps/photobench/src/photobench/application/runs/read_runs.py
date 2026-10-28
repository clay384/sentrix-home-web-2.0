"""run 读侧用例：列表 / 详情 / 题目分页 / 单题 / 评审。

对应 1.0 ``OrchestratorRepository`` 的 ``list_runs``（``:5343``）、``get_run``（``:5354``）、
``get_run_items``（``:6441``）、``get_run_item``（``:6528``）、``get_reviews``（``:4976``），
以及它们用到的 ``_hydrate_legacy_gt_images``（``:6851``）。

**写侧一条都没有**：``cancel_run`` / ``resume_run`` / ``delete_run`` / ``save_reviews`` /
``start_suite`` / ``start_rejudge`` 属 S9b-2 / S9b-3。所以这一层对状态是**只读**的，
唯一的例外是 1.0 启动时会改写「脏状态」（``_load_existing`` ``:5013-5043`` 把 ``running``
改成 ``interrupted`` 并把整份 ``run.json`` 落盘）——**那不搬**：让读侧去写盘，与「读侧」
这个边界不符。代价是重启后 ``/api/runs`` 会把上次被杀掉的 run 仍标成 ``running``
（``active_count`` 偏高）——这是本切片**已知的行为差异**，S9b-2 的写侧接上时一并收。
"""

from __future__ import annotations

import copy
import math
import sys
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from pathlib import Path
from typing import Any
from urllib.parse import quote

from photobench.application.runs.index import RunIndex
from photobench.domain.datasets.ports import QaMetadataSource
from photobench.domain.http.ports import JsonHttpClient
from photobench.domain.results.ports import RunReadViews, RunRevisions, RunStore
from photobench.domain.runs import analysis

#: 详情/题目两条路径会各解析一次同一个 run.json。最大的一个 447 MB、解析 2.03 s，
#: 前端翻页会把同一份反复解析成几十次。1.0 靠「全部常驻内存」绕过这件事（代价是
#: 11 GB）；2.0 最多保留最近两个且总对象预算64MiB，超过预算的完整状态只按需读取，不驻留缓存。
_STATE_CACHE_SIZE = 2
_STATE_CACHE_BYTES = 64 * 1024 * 1024


def _object_bytes(value: Any) -> int:
    """Account object graphs once; avoid serializing another full copy for sizing."""
    seen: set[int] = set()
    pending = [value]
    size = 0
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        size += sys.getsizeof(item)
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend(item)
    return size


#: **缓存只对终态的 run 生效。** 这条不是性能取舍，是正确性：还在跑的 run 每读一次
#: 都必须重新解析——否则调用方会一直看到**第一次读到的那份快照**。
#: 这是 S9c 真机验收撞出来的：``run.json`` 已经 ``completed``、八个相位全 done，而
#: ``GET /api/runs/{id}`` 一直回 ``running``（驱动脚本按它轮询，于是白等了十分钟）。
#: 1.0 没有这个问题——它对活跃 run 持有**活的** ``BenchmarkRun`` 对象，次次读的都是当前值。
#: 代价可控：非终态的 run 体量随跑随长（不是那 447 MB 的历史 run），重解析远比
#: 「界面卡在旧状态」便宜。
NON_TERMINAL_STATUSES = frozenset({"pending", "running", "cancelling"})


def _run_sort_key(run: Mapping[str, Any]) -> str:
    """列表排序键。**活 run 与索引条目必须用同一个**——混排时不共用会得到两种顺序。"""
    return str(run.get("started_at") or run.get("created_at") or "")


class ReadRuns:
    """读侧用例。依赖四个端口，全部由装配点注入（``bootstrap.container``）。"""

    def __init__(
        self,
        store: RunStore,
        index: RunIndex,
        qa_metadata: QaMetadataSource,
        http: JsonHttpClient,
        sentrix_url: Callable[[], str],
        live_states: Callable[[], Mapping[str, dict[str, Any]]] | None = None,
    ) -> None:
        self._store = store
        self._versions = store if isinstance(store, RunRevisions) else None
        self._views = store if isinstance(store, RunReadViews) else None
        self._state_weights: dict[str, int] = {}
        self._projections: OrderedDict[tuple[str, str, str], dict[str, Any]] = OrderedDict()
        self._state_versions: dict[str, str] = {}
        self._index = index
        self._qa_source = qa_metadata
        self._http = http
        self._sentrix_url = sentrix_url
        # 「还在跑的 run」的活状态。**传取值函数而不是快照**，与上面那个
        # ``sentrix_url`` 同一条理由：列表页每次来问都要拿当下的，不是装配那时的。
        self._live_states = live_states
        self._lock = threading.RLock()
        self._states: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._item_locks = tuple(threading.RLock() for _ in range(64))
        self._run_reads: dict[tuple[str, bool], Future[dict[str, Any]]] = {}
        self._qa_metadata: dict[str, dict[str, Any]] | None = None

    # -- 内部 ---------------------------------------------------------------

    def _state(self, run_id: str) -> dict[str, Any]:
        """按需解析一个 run 的完整状态（含 items），**只把终态的放进缓存**。

        **非终态的不入缓存**（S9c 真机验收撞到的缺陷）：一个 run 在跑的时候被读过
        一次就会进 LRU，之后再没有东西把它摘掉——于是它跑完之后 ``GET /api/runs/{id}``
        仍然回那份 ``running`` 快照。判据就落在这**一处**写侧：入缓存的先决条件是
        「这一份是终态」。用例 ``test_a_running_run_is_not_served_from_a_stale_cache``
        钉住它（把 ``not in`` 去掉，用例立刻红）。
        """
        revision = self._versions.revision(run_id) if self._versions is not None else "legacy"
        with self._lock:
            if not revision or self._state_versions.get(run_id) != revision:
                self._states.pop(run_id, None)
                self._state_weights.pop(run_id, None)
                self._state_versions.pop(run_id, None)
            cached = self._states.get(run_id)
            if cached is not None:
                self._states.move_to_end(run_id)
                return cached
        state = self._store.load_state(run_id)  # 不存在 -> KeyError，HTTP 层映射成 404
        weight = _object_bytes(state)
        if (
            state.get("status") not in NON_TERMINAL_STATUSES
            and weight <= _STATE_CACHE_BYTES
            and (self._versions is None or bool(revision) and self._versions.revision(run_id) == revision)
        ):
            with self._lock:
                self._states[run_id] = state
                self._state_weights[run_id] = weight
                self._state_versions[run_id] = revision
                self._states.move_to_end(run_id)
                while (
                    len(self._states) > _STATE_CACHE_SIZE
                    or sum(self._state_weights.values()) > _STATE_CACHE_BYTES
                ):
                    removed, _ = self._states.popitem(last=False)
                    self._state_versions.pop(removed, None)
                    self._state_weights.pop(removed, None)
        return state

    def _qa_index(self) -> dict[str, dict[str, Any]]:
        """数据集侧元信息，进程内建一次（1.0 ``:4884`` 的 ``self.qa_metadata``）。

        **惰性而不是装配时建**：只有「题目」那几条路由用得上它，而它要扫一遍数据集
        目录。列表页不该为此付一次全库扫描。
        """
        with self._lock:
            if self._qa_metadata is None:
                self._qa_metadata = self._qa_source.metadata_by_qa_id()
            return self._qa_metadata

    def _hydrate_legacy_gt_images(self, state: dict[str, Any]) -> None:
        """1.0 ``:6851``：给早于 ``gt_images`` 字段的结果补展示用的 GT 资产映射。

        **它会打一次主 API。** 1.0 的口径是「补不上就算了」——整段 ``except Exception:
        return``，因为这是展示增强，不是读 run 的前提。2.0 照搬这条分工，并且它是
        本切片里 ``JsonHttpClient.request_json`` 的**唯一生产调用者**。
        """
        items = state.get("items") or []
        missing = [item for item in items if "gt_images" not in item]
        scope_id = state.get("scope_id")
        if not missing or not scope_id:
            return
        try:
            sentrix_url = str(state.get("sentrix_url") or self._sentrix_url()).rstrip("/")
            data = self._http.request_json(
                f"{sentrix_url}/api/assets?scope_id={quote(str(scope_id))}&limit=2000",
                timeout=15,
            )
            assets_by_name: dict[str, list[dict[str, Any]]] = {}
            for asset in (data or {}).get("assets", []):
                assets_by_name.setdefault(Path(str(asset.get("file_name") or "")).name, []).append(asset)
        except Exception:  # noqa: BLE001 —— 1.0 如此：GT 图片只是展示增强，补不上不影响读 run
            return
        for item in missing:
            gt_images = []
            for image_id in item.get("retrieval_image_ids") or []:
                file_name = Path(str(image_id)).name
                candidates = assets_by_name.get(file_name, [])
                gt_images.append(
                    {
                        "image_id": image_id,
                        "file_name": file_name,
                        "asset_id": candidates[0].get("id") if len(candidates) == 1 else None,
                        "matched": file_name in set(item.get("matched_file_names") or []),
                        "mapping_status": "ok"
                        if len(candidates) == 1
                        else "missing"
                        if not candidates
                        else "ambiguous",
                    }
                )
            item["gt_images"] = gt_images

    # -- 用例 ---------------------------------------------------------------

    def list_runs(self, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        """``GET /api/runs``（1.0 ``:7648`` 的分支 + ``list_runs`` ``:5343``）。

        **还在跑的 run 要叠上去。** 索引里没有它们——写侧只在 run 结束时 ``upsert``，
        而在那之前索引不知道这一轮存在。不叠的话，整轮评测期间列表页是空的，而前端
        没有 URL 路由，于是那一轮在界面上完全够不着（看不到进度，也点不到取消）。
        实测 2026-10-09：一轮 487 题的评测跑了一个多小时，界面上一直什么都没有。
        """
        rows = sorted(self._index.entries(), key=_run_sort_key, reverse=True)
        live = self._live_states() if self._live_states is not None else {}
        if live:
            known = {str(row.get("run_id") or "") for row in rows}
            rows = [
                (
                    dict(state)
                    if "items" not in state and "item_count" in state
                    else analysis.run_list_entry(dict(state))
                )
                for run_id, state in live.items()
                if run_id not in known
            ] + rows
            rows.sort(key=_run_sort_key, reverse=True)
        response = analysis.paginate_rows(rows, page, page_size)
        response["active_count"] = sum(
            1
            for run in rows
            if run.get("status") in RunIndex._ACTIVE_STATUSES
            or (run.get("rejudge") or {}).get("status") == "running"
        )
        # 1.0 没有这个字段：它扫完才肯回答，没有「答案是残缺的」这种状态可说。
        # 后台扫描是 2.0 的取舍，这个字段让取舍可观测（见 ``RunIndex.status``）。
        response["scan"] = self._index.status()
        return response

    def get_run(self, run_id: str, *, include_history: bool = True) -> dict[str, Any]:
        """``GET /api/runs/{id}``（1.0 ``get_run`` ``:5354``）。"""
        # 多个浏览器/轮询请求共享正在生成的同一份投影；不保留活跃 run 的
        # 完成态缓存。否则每个 HTTP worker 都解析整份 JSONL，读侧内存可随
        # 并发倍增并挤压实际执行及采样。不同 run 不互相等待。
        key = (run_id, include_history)
        with self._lock:
            pending = self._run_reads.get(key)
            leader = pending is None
            if pending is None:
                pending = Future()
                self._run_reads[key] = pending
        if not leader:
            return pending.result()
        try:
            result = self._project_run(run_id, include_history=include_history)
            entry = self._index.entry(run_id) or {}
            persistence = entry.get("persistence") or {}
            if persistence.get("status") in {"failed", "stop_pending"}:
                # The failed write cannot put its own diagnosis on a full disk.
                # Keep the checkpoint provenance, and identify the process-local overlay.
                result = {
                    **result,
                    "status": entry.get("status", result.get("status")),
                    **{
                        key: entry[key]
                        for key in ("finished_at", "failed_phase", "fatal_error")
                        if key in entry
                    },
                    "persistence": {
                        **persistence,
                        "diagnostic_source": "current_process",
                        "checkpoint_status": result.get("status"),
                    },
                }
            pending.set_result(result)
            return result
        except BaseException as error:
            pending.set_exception(error)
            raise
        finally:
            with self._lock:
                self._run_reads.pop(key, None)

    def _project_run(self, run_id: str, *, include_history: bool = True) -> dict[str, Any]:
        revision = self._versions.revision(run_id) if self._versions is not None else ""
        telemetry_revision = (
            self._views.telemetry_revision(run_id) if include_history and self._views is not None else ""
        )
        cache_key = (run_id, revision, telemetry_revision)
        if include_history and revision and telemetry_revision:
            with self._lock:
                cached = self._projections.get(cache_key)
                if cached is not None:
                    self._projections.move_to_end(cache_key)
                    return copy.deepcopy(cached)
        if self._views is not None:
            result = self._views.load_overview(run_id)
        else:
            state = self._state(run_id)
            result = {key: value for key, value in state.items() if key != "items"}
            result["item_count"] = len(state.get("items") or [])
            result["summary"] = analysis.effective_summary(state)
        result.setdefault("read_revision", revision or None)
        result["history_mode"] = "full" if include_history else "checkpoint"
        live = result.get("telemetry_live")
        if isinstance(live, dict):
            # ``_public_run`` 故意不做深拷贝，而下面要把 JSONL 采样并进 live；
            # 直接改会把内存里那份运行状态改脏（1.0 的注释就是这个意思，``:5370``）。
            result["telemetry_live"] = copy.deepcopy(live)
        live = result.get("telemetry_live")
        if include_history and isinstance(live, dict):
            analysis.attach_gpu_sample_history(live, self._store.read_gpu_sample_lines(run_id))
            analysis.publish_full_sample_phase_stats(result, live)
        if (
            include_history
            and revision
            and telemetry_revision
            and self._versions is not None
            and self._views is not None
            and self._versions.revision(run_id) == revision
            and self._views.telemetry_revision(run_id) == telemetry_revision
            and _object_bytes(result) <= 4 * 1024 * 1024
        ):
            with self._lock:
                self._projections[cache_key] = copy.deepcopy(result)
                while len(self._projections) > 2:
                    self._projections.popitem(last=False)
        return result

    def get_reviews(self, run_id: str) -> dict[str, Any]:
        """``GET /api/runs/{id}/reviews``（1.0 ``get_reviews`` ``:4976``）。"""
        return self._store.read_reviews(run_id)

    def get_run_items(
        self,
        run_id: str,
        page: int = 1,
        page_size: int = 20,
        search: str = "",
        score: str = "",
        task_type: str = "",
        tag: str = "",
        agent_status: str = "",
        angle: str = "",
        difficulty: str = "",
        answerability: str = "",
        primary: str = "",
    ) -> dict[str, Any]:
        """``GET /api/runs/{id}/items``（1.0 ``get_run_items`` ``:6441``）。"""
        with self._item_locks[hash(run_id) % len(self._item_locks)]:
            cached = self._state(run_id)
            state = {**cached, "items": [dict(item) for item in cached.get("items") or []]}
            self._hydrate_legacy_gt_images(state)
            qa_metadata = self._qa_index()
            all_items = [
                analysis.hydrate_qa_metadata(item, qa_metadata) for item in (state.get("items") or [])
            ]
            search = str(search or "").strip().lower()
            task_type = str(task_type or "").strip()
            tag = str(tag or "").strip()
            agent_status = str(agent_status or "").strip()
            angle = str(angle or "").strip()
            difficulty = str(difficulty or "").strip()
            answerability = str(answerability or "").strip()
            primary = str(primary or "").strip().upper()
            score_filter = None
            if str(score).strip() in {"0", "1", "2"}:
                score_filter = int(score)
            items = []
            source_indexes = []
            facets = {
                "task_types": sorted(
                    {str(item.get("task_type")) for item in all_items if item.get("task_type")}
                ),
                "tags": sorted(
                    {str(tag) for item in all_items for tag in (item.get("tags") or []) if str(tag).strip()}
                ),
                "agent_statuses": sorted(
                    {str(item.get("agent_status")) for item in all_items if item.get("agent_status")}
                ),
                "angles": sorted({str(item.get("angle")) for item in all_items if item.get("angle")}),
                "difficulties": sorted(
                    {str(item.get("difficulty")) for item in all_items if item.get("difficulty")}
                ),
                "answerabilities": sorted(
                    {str(item.get("answerability")) for item in all_items if item.get("answerability")}
                ),
                "attribution_layers": sorted(
                    {
                        str((item.get("attribution") or {}).get("primary"))
                        for item in all_items
                        if (item.get("attribution") or {}).get("primary")
                    }
                ),
            }
            for source_index, item in enumerate(all_items):
                if (
                    search
                    and search not in str(item.get("question") or "").lower()
                    and search not in str(item.get("qa_id") or "").lower()
                ):
                    continue
                if (
                    score_filter is not None
                    and analysis.judge_score_for_summary(item.get("judge")) != score_filter
                ):
                    continue
                if task_type and item.get("task_type") != task_type:
                    continue
                if tag and tag not in (item.get("tags") or []):
                    continue
                if agent_status and item.get("agent_status") != agent_status:
                    continue
                if angle and item.get("angle") != angle:
                    continue
                if difficulty and item.get("difficulty") != difficulty:
                    continue
                if answerability and item.get("answerability") != answerability:
                    continue
                if primary and str((item.get("attribution") or {}).get("primary") or "").upper() != primary:
                    continue
                items.append(item)
                source_indexes.append(source_index)
            page_size = max(1, min(int(page_size), 100))
            total = len(items)
            pages = max(1, math.ceil(total / page_size))
            page = max(1, min(int(page), pages))
            start = (page - 1) * page_size
            reviews = self.get_reviews(run_id).get("reviews") or {}
            # 两个切片等长（source_indexes 与 items 一一对应），strict=False 与 1.0 的
            # 隐式截断语义相同；写成 strict=True 只会在将来真的错位时换一种报错。
            summaries = [
                analysis.item_summary(item, source_index, reviews.get(str(item.get("qa_id"))))
                for item, source_index in zip(
                    items[start : start + page_size],
                    source_indexes[start : start + page_size],
                    strict=False,
                )
            ]
            return {
                "items": summaries,
                "page": page,
                "page_size": page_size,
                "total": total,
                "pages": pages,
                "has_previous": page > 1,
                "has_next": page < pages,
                "unfiltered_total": len(all_items),
                "filters": {
                    "search": search,
                    "score": score_filter,
                    "task_type": task_type,
                    "tag": tag,
                    "agent_status": agent_status,
                    "angle": angle,
                    "difficulty": difficulty,
                    "answerability": answerability,
                    "primary": primary,
                },
                "facets": facets,
            }

    def get_run_item(self, run_id: str, item_index: int) -> dict[str, Any]:
        """``GET /api/runs/{id}/items/{index}``（1.0 ``get_run_item`` ``:6528``）。"""
        with self._item_locks[hash(run_id) % len(self._item_locks)]:
            cached = self._state(run_id)
            state = {**cached, "items": [dict(item) for item in cached.get("items") or []]}
            self._hydrate_legacy_gt_images(state)
            items = state.get("items") or []
            if item_index < 0 or item_index >= len(items):
                raise KeyError(f"item index out of range: {item_index}")
            item = analysis.hydrate_qa_metadata(items[item_index], self._qa_index())
            item["review"] = (self.get_reviews(run_id).get("reviews") or {}).get(str(item.get("qa_id")))
            return {"index": item_index, "item": item}
