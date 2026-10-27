"""run 的三份报告：关键帧分析 / 记忆有效性 / trace 导出。

对应 1.0 ``OrchestratorRepository`` 的 ``get_keyframe_analysis``（``:5600``）、
``get_memory_effectiveness``（``:5889``）、``export_traces``（``:6724``）、
``export_sft``（``:6447``）。

**这一层的活是取数，不是算数**：三个 ``domain.runs.*`` 模块是纯函数，这里负责把
它们要的东西凑齐并调过去。

**为什么直接拿 ``JsonHttpClient`` 而不是走 ``SentrixClient`` 端口。** 端口声明的是
「评测主链要主产品做的那几件事」（种人、导目录、问一句），而这里要的是两件**只读的
展示层**调用：按 scope 列资产、按 id 问视频元数据。往端口上加方法会把「主链」与
「控制台面板」混成一张接口。``ReadRuns._hydrate_legacy_gt_images`` 已经开了同一个
先例——它也是直接打 ``/api/v1/assets``。

**缓存的是「同一份 run 状态」下两个面板的结果。** 原先这里刻意不做缓存，前提是
「面板按需打开，没有同一个 run 反复读的模式」。158 的实测推翻了这个前提：面板开着时
浏览器以约 0.55 次/秒轮询 ``/memory-effectiveness``（``/keyframe-analysis`` 也有
0.27 次/秒），而每次都要解析 45MB 的 run.json、再打一次 Sentrix 资产清单，单次
0.6–1.2s CPU——一个空转的标签页就能吃掉一个核。
缓存键是 ``(run_id, store.revision(run_id))``：run.json 一变 revision 就变，缓存
自然失效；30s TTL 兜住「run.json 没变但 scope 的资产清单变了」这种 revision 看不见
的情况。

**路径前缀是 ``/api/v1``**（1.0 是 ``/api``），与 ``infrastructure.sentrix.client``
的 ``API_PREFIX`` 同一条。``limit`` 的差异见下面的常量。
"""

from __future__ import annotations

import copy
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import quote

from photobench.domain.datasets.ports import AlbumSource
from photobench.domain.http.ports import JsonHttpClient
from photobench.domain.results.ports import RunStore
from photobench.domain.runs import exports, graph_quality, keyframe_analysis, memory_effectiveness

#: 2.0 的 REST 前缀。1.0 是 ``/api``。
API_PREFIX = "/api/v1"

#: 资产清单的上限。**这是一处行为差异，不是笔误。**
#: 1.0 的关键帧分析写 ``limit=2000``、记忆有效性写 ``limit=5000``（``:5611`` / ``:5648``），
#: 而 2.0 的 ``GET /api/v1/assets`` 在路由里做 ``min(limit, 1000)``（``api/routes/media.py:301``）
#: ——给更大的数不会报错，只会被**静默截断**。benchmark 相册实测最大的一个是
#: album3-max-video10（334 照片 + 10 视频 + 派生帧 ≈ 400 条），1000 够用；
#: 但「相册大到超过 1000 条时这两个面板会悄悄少算」是已知代价，登记在 ``s10-deviations.md``。
ASSET_LIMIT = 1000

#: 逐视频取元数据的超时。它是本地调用，但视频多的时候是 N 次串行。
VIDEO_TIMEOUT_S = 30

#: 记忆有效性结果的缓存。见模块 docstring：浏览器会以约 0.55 次/秒轮询这个面板。
#: 条目小（几十 KB），留 8 条足够覆盖「刚看过的那几个 run」。
MEMO_TTL_S = 30.0
MEMO_MAX_ENTRIES = 8


def _quote(value: str) -> str:
    """与 ``infrastructure.sentrix.client._quote`` 同一条：``safe=""``。

    默认的 ``quote`` 把 ``/`` 留在安全字符里，让带斜杠的 id 在**路径**上被切成两段。
    """
    return quote(value, safe="")


class RunReports:
    """三个报告读侧用例。依赖与 ``ReadRuns`` 同一组（装配点注入）。"""

    def __init__(
        self,
        store: RunStore,
        http: JsonHttpClient,
        sentrix_url: Callable[[], str],
        albums: AlbumSource | None = None,
    ) -> None:
        self._store = store
        self._http = http
        self._sentrix_url = sentrix_url
        self._albums = albums
        self._memo_lock = threading.Lock()
        self._memo: OrderedDict[tuple[str, ...], tuple[float, dict[str, Any]]] = OrderedDict()

    def _memo_get(self, key: tuple[str, ...]) -> dict[str, Any] | None:
        now = time.monotonic()
        with self._memo_lock:
            cached = self._memo.get(key)
            if cached is None or now - cached[0] > MEMO_TTL_S:
                return None
            self._memo.move_to_end(key)
            return copy.deepcopy(cached[1])

    def _memo_put(self, key: tuple[str, ...], value: dict[str, Any]) -> None:
        now = time.monotonic()
        with self._memo_lock:
            self._memo[key] = (now, copy.deepcopy(value))
            while len(self._memo) > MEMO_MAX_ENTRIES:
                self._memo.popitem(last=False)

    def _base(self) -> str:
        return self._sentrix_url().rstrip("/")

    def _state(self, run_id: str) -> dict[str, Any]:
        """不存在抛 ``KeyError``（HTTP 层映射成 404）。"""
        return self._store.load_state(run_id)

    def _assets(self, scope_id: str) -> tuple[list[dict[str, Any]] | None, str]:
        """取 scope 的资产清单。

        ``None`` 表示**这一步失败了**，与「取到了但是空的」是两种结果——关键帧分析
        对这两种的处理不同（``unavailable`` vs 工具轨迹回退），所以不能在这里合并。
        """
        try:
            payload = self._http.request_json(
                f"{self._base()}{API_PREFIX}/assets?scope_id={_quote(scope_id)}&limit={ASSET_LIMIT}",
                method="GET",
                timeout=60,
            )
        except Exception as exc:  # noqa: BLE001 —— 1.0 把这一步的异常变成 status，不是 500
            return None, str(exc)
        items = payload.get("items") if isinstance(payload, dict) else None
        return [dict(item) for item in (items or []) if isinstance(item, dict)], ""

    def _video_facts(self, assets: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """``{asset_id: {duration_sec, frame_count, processing_seconds}}``，只对视频。

        1.0 从资产行的 ``metadata_json`` 里直接翻这三个数（它的 ``/api/assets`` 是
        ``SELECT *``）；2.0 把它们留在 ``GET /videos/{id}`` 上，所以这里逐个取。

        **取失败就跳过那个视频**，与 1.0 遇到字段缺失时的行为一致——它有一整套
        ``isinstance(...)`` 判断，缺哪个数就当哪个数没有。少一个视频的时长不该让
        整个面板变成错误页。
        """
        facts: dict[str, dict[str, Any]] = {}
        for asset in assets:
            if asset.get("media_type") != "video":
                continue
            asset_id = str(asset.get("id") or "")
            if not asset_id:
                continue
            try:
                payload = self._http.request_json(
                    f"{self._base()}{API_PREFIX}/videos/{_quote(asset_id)}",
                    method="GET",
                    timeout=VIDEO_TIMEOUT_S,
                )
            except Exception:  # noqa: BLE001 —— 见 docstring：缺一个数是缺一个数
                continue
            video = payload.get("video") if isinstance(payload, dict) else None
            if not isinstance(video, dict):
                continue
            metadata = video.get("metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            facts[asset_id] = {
                "duration_sec": metadata.get("duration_sec"),
                "frame_count": metadata.get("frame_count"),
                "processing_seconds": video.get("processing_seconds"),
            }
        return facts

    def keyframe_analysis(self, run_id: str) -> dict[str, Any]:
        # 先取 revision：不存在时与 ``_state`` 同一个出口抛 ``KeyError``（HTTP 404）。
        cache_key = ("keyframe", run_id, self._store.revision(run_id))
        cached = self._memo_get(cache_key)
        if cached is not None:
            return cached

        state = self._state(run_id)
        scope_id = str(state.get("scope_id") or "").strip()
        if not scope_id:
            # 没 scope 就不用打 Sentrix 了——纯函数那边会回 ``pending``。
            result = keyframe_analysis.keyframe_analysis(state, [])
        else:
            assets, error = self._assets(scope_id)
            facts = self._video_facts(assets) if assets else {}
            result = keyframe_analysis.keyframe_analysis(state, assets, error=error, video_facts=facts)
        self._memo_put(cache_key, result)
        return result

    def memory_effectiveness(self, run_id: str) -> dict[str, Any]:
        cache_key = ("memory", run_id, self._store.revision(run_id))
        cached = self._memo_get(cache_key)
        if cached is not None:
            return cached

        state = self._state(run_id)
        scope_id = str(state.get("scope_id") or "").strip()
        # 1.0 在 ``:6005`` 用 ``except Exception: assets = []`` 把失败吞掉，让命中匹配
        # 回落到字段直接比对——所以这里失败要传空表，**不能**传 ``None``。
        assets: list[dict[str, Any]] = []
        if scope_id:
            fetched, _ = self._assets(scope_id)
            assets = fetched or []
        result = memory_effectiveness.memory_effectiveness(state, assets)
        self._memo_put(cache_key, result)
        return result

    def _graph_snapshot(self, scope_id: str) -> dict[str, Any]:
        try:
            payload = self._http.request_json(
                f"{self._base()}{API_PREFIX}/memory-graph/quality?scope_id={_quote(scope_id)}",
                method="GET",
                timeout=60,
            )
        except Exception as exc:  # noqa: BLE001 - 图诊断不应让整页 500
            return {"available": False, "reason": str(exc)}
        return (
            dict(payload) if isinstance(payload, dict) else {"available": False, "reason": "invalid response"}
        )

    def _face_clusters(self, scope_id: str) -> list[dict[str, Any]]:
        try:
            payload = self._http.request_json(
                f"{self._base()}{API_PREFIX}/face-clusters?scope_id={_quote(scope_id)}&include_all_samples=true",
                method="GET",
                timeout=60,
            )
        except Exception:
            return []
        clusters = payload.get("clusters") if isinstance(payload, dict) else None
        return [dict(cluster) for cluster in (clusters or []) if isinstance(cluster, dict)]

    def _identity_truth(self, album_id: str) -> dict[str, list[str]] | None:
        """读取 benchmark sidecar；生产 API 永远看不到这份 GT。"""

        if self._albums is None or not album_id:
            return None
        for relative in ("identity/face_info_cn.json", "faceid/face_info_cn.json", "face_info_cn.json"):
            try:
                value = json.loads(self._albums.read_file(album_id, relative).decode("utf-8"))
            except (KeyError, OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
                continue
            raw = value.get("image_to_face_ids") if isinstance(value, dict) else None
            if not isinstance(raw, dict):
                continue
            return {
                str(name): [str(identity) for identity in labels]
                for name, labels in raw.items()
                if isinstance(labels, list)
            }
        return None

    def graph_quality(self, run_id: str) -> dict[str, Any]:
        """1.0 图效果面板的 2.0 HTTP-only 读侧。"""

        state = self._state(run_id)
        scope_id = str(state.get("scope_id") or "").strip()
        items = [item for item in (state.get("items") or []) if isinstance(item, dict)]
        report: dict[str, Any] = {
            "status": "available" if scope_id else "unavailable",
            "run_id": run_id,
            "scope_id": scope_id,
            "graph": self._graph_snapshot(scope_id)
            if scope_id
            else {"available": False, "reason": "scope_missing"},
            "face_clustering": {"available": False, "reason": "scope_missing"},
            "graph_qa_by_type": graph_quality.aggregate_graph_qa_by_type(items),
        }
        if scope_id:
            truth = self._identity_truth(str(state.get("album_id") or ""))
            assets, _ = self._assets(scope_id)
            asset_names = (
                {str(asset.get("file_name") or "") for asset in assets if asset.get("file_name")}
                if assets is not None
                else None
            )
            report["face_clustering"] = graph_quality.face_clustering_quality(
                self._face_clusters(scope_id), truth, asset_names=asset_names
            )
        return report

    def export_traces(
        self, run_id: str, *, scores: Sequence[int] | None = None, min_score: int | None = None
    ) -> list[dict[str, Any]]:
        return exports.export_traces(self._state(run_id), scores=scores, min_score=min_score)

    def export_sft(
        self, run_id: str, *, scores: Sequence[int] | None = None, min_score: int | None = None
    ) -> dict[str, Any]:
        return exports.export_sft(self._state(run_id), scores=scores, min_score=min_score)
