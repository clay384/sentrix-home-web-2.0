"""``application/runs/reports.py`` 的用例。

与 ``test_run_read.py`` 同一条纪律：run 数据是**临时目录里的真文件**（这一层的职责
就是「把 1.0 那批 ``run.json`` 读出来」），HTTP 走 ``FakeJsonHttpClient``，
所以用例不依赖任何服务。

**最值钱的一条是「两种失败要分得开」**：资产清单取不到时，关键帧分析回
``unavailable``，而记忆有效性要**吞掉**它并回落到字段比对。1.0 就是这么分的
（``:5613`` 抛出 vs ``:6005`` 吞掉），而这两条路只差几行代码——夹具必须让它们给出
不同的答案，否则「吞掉」这件事根本没人测。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from fakes_read import FakeJsonHttpClient

from photobench.application.runs.reports import RunReports
from photobench.infrastructure.results.store import FileResultWriter, FileRunStore

SENTRIX = "http://sentrix.test"


def _write(tmp_path: Path, run_id: str, state: dict) -> FileRunStore:
    run_dir = tmp_path / run_id
    run_dir.mkdir(parents=True)
    # Exercise the production immutable generation/revision contract.
    FileResultWriter(tmp_path).write_snapshot(run_id, state)
    return FileRunStore(tmp_path)


def _state(run_id: str = "run-1", **over: object) -> dict:
    state: dict = {"run_id": run_id, "scope_id": "scope-1", "items": []}
    state.update(over)
    return state


def _reports(store: FileRunStore, http: FakeJsonHttpClient) -> RunReports:
    base: Callable[[], str] = lambda: SENTRIX  # noqa: E731 —— 与容器里那个取值函数同形
    return RunReports(store=store, http=http, sentrix_url=base)


def _video(asset_id: str) -> dict:
    return {"id": asset_id, "file_name": f"{asset_id}.mp4", "media_type": "video"}


def _image(asset_id: str) -> dict:
    return {"id": asset_id, "file_name": f"{asset_id}.jpg", "media_type": "image"}


# ---- 关键帧分析：取数 -------------------------------------------------------


def test_assets_are_fetched_for_the_runs_scope_and_passed_through(tmp_path: Path) -> None:
    store = _write(
        tmp_path,
        "run-1",
        _state(
            items=[
                {
                    "gt_media": [{"asset_id": "v1", "media_type": "video"}],
                    "retrieved_candidate_media": [{"asset_id": "k1"}],
                    "judge": {"score": 2},
                }
            ]
        ),
    )
    http = FakeJsonHttpClient({"/assets?": {"items": []}})
    out = _reports(store, http).keyframe_analysis("run-1")

    assert out["status"] == "ready"
    assert out["scope_id"] == "scope-1"
    assert http.calls[0][0] == f"{SENTRIX}/api/v1/assets?scope_id=scope-1&limit=1000"


def test_video_facts_are_fetched_only_for_video_assets(tmp_path: Path) -> None:
    """逐视频取元数据，但**只对视频**——对每张照片也打一次会白付 N 次往返。"""
    store = _write(tmp_path, "run-1", _state(items=[]))
    http = FakeJsonHttpClient(
        {
            "/assets?": {"items": [_image("a1"), _video("v1"), _image("a2")]},
            "/videos/v1": {
                "video": {
                    "id": "v1",
                    "metadata": {"duration_sec": 90.0, "frame_count": 2700},
                    "processing_seconds": 45.0,
                }
            },
        }
    )
    out = _reports(store, http).keyframe_analysis("run-1")

    video_calls = [url for url, *_ in http.calls if "/videos/" in url]
    assert video_calls == [f"{SENTRIX}/api/v1/videos/v1"]
    assert out["processing"]["source_duration_sec"] == 90.0
    assert out["processing"]["raw_frame_count"] == 2700
    assert out["processing"]["realtime_factor"] == 2.0


def test_a_video_whose_metadata_call_fails_is_skipped_not_fatal(tmp_path: Path) -> None:
    """一个视频取不到元数据 = 少一个数，**不是**整个面板变成错误页（1.0 亦然）。"""
    store = _write(tmp_path, "run-1", _state(items=[]))
    http = FakeJsonHttpClient({"/assets?": {"items": [_video("v1")]}})
    out = _reports(store, http).keyframe_analysis("run-1")

    assert out["status"] == "ready"
    assert out["assets"]["source_videos"] == 1
    assert out["processing"]["source_duration_sec"] == 0.0
    assert out["processing"]["realtime_factor"] is None


def test_no_scope_means_no_http_at_all(tmp_path: Path) -> None:
    """没有 scope 就没有可取的资产——不该先打一次注定空手而归的请求。"""
    store = _write(tmp_path, "run-1", {"run_id": "run-1", "items": []})
    http = FakeJsonHttpClient()
    out = _reports(store, http).keyframe_analysis("run-1")

    assert out == {"status": "pending", "reason": "scope_id_missing", "run_id": "run-1"}
    assert http.calls == []


# ---- 两种失败必须分得开 -----------------------------------------------------


def test_the_two_reports_treat_a_dead_sentrix_differently(tmp_path: Path) -> None:
    """**同一个坏掉的 Sentrix，两个报告的反应必须不同。**

    关键帧分析把「取不到」当 ``unavailable`` 报出来（1.0 ``:5613``）；记忆有效性
    把它吞掉、回落到 ``media_id`` / ``file_name`` 直接比对（1.0 ``:6005``）。
    夹具在这里让两条路给出不同答案——只测其中一条的话，另一条的 except 写没写都看不出来。
    """
    store = _write(tmp_path, "run-1", _state(items=[]))
    http = FakeJsonHttpClient()
    http.fail_with = RuntimeError("connection refused")
    reports = _reports(store, http)

    keyframe = reports.keyframe_analysis("run-1")
    assert keyframe["status"] == "unavailable"
    assert keyframe["error"] == "connection refused"
    assert keyframe["scope_id"] == "scope-1"

    memory = reports.memory_effectiveness("run-1")
    assert memory["status"] == "ready"
    assert "无资产清单" in memory["data_quality"]["hit_match_source"]


def test_memory_effectiveness_without_a_scope_says_so(tmp_path: Path) -> None:
    """没有 scope 时那句话不一样——「run 没记 scope」与「scope 取不到」是两回事。"""
    store = _write(tmp_path, "run-1", {"run_id": "run-1", "items": []})
    out = _reports(store, FakeJsonHttpClient()).memory_effectiveness("run-1")
    assert out["data_quality"]["hit_match_source"] == "run 未记录 scope：命中匹配仅用字段直接比对"


def test_a_missing_run_is_a_key_error(tmp_path: Path) -> None:
    """不存在 → ``KeyError``，HTTP 层映射成 404（``api/errors.py``）。"""
    store = FileRunStore(tmp_path)
    reports = _reports(store, FakeJsonHttpClient())
    with pytest.raises(KeyError):
        reports.keyframe_analysis("nope")
    with pytest.raises(KeyError):
        reports.export_sft("nope")


# ---- 导出 -------------------------------------------------------------------


def _export_state() -> dict:
    return _state(
        items=[
            {
                "qa_id": "q0",
                "question": "零分题",
                "judge": {"score": 0},
                "runtime_turns": [
                    {
                        "index": 0,
                        "debug_trace": [
                            {
                                "type": "model",
                                "step_id": "s1",
                                "prompt": [{"role": "user", "content": "a"}],
                                "raw_full": "A",
                            }
                        ],
                    }
                ],
            },
            {"qa_id": "q1", "question": "满分题", "judge": {"score": 2}},
        ]
    )


def test_exports_read_the_run_and_apply_the_filters(tmp_path: Path) -> None:
    store = _write(tmp_path, "run-1", _export_state())
    reports = _reports(store, FakeJsonHttpClient())

    assert [row["qa_id"] for row in reports.export_traces("run-1")] == ["q0", "q1"]
    assert [row["qa_id"] for row in reports.export_traces("run-1", scores=[2])] == ["q1"]
    assert [row["qa_id"] for row in reports.export_traces("run-1", min_score=1)] == ["q1"]

    sft = reports.export_sft("run-1", scores=[0])
    assert [row["qa_id"] for row in sft["items"]] == ["q0"]
    assert sft["count"] == 1


# ---- 记忆有效性：按 (run, revision) 缓存 ------------------------------------


def test_memory_effectiveness_is_cached_until_the_run_changes(tmp_path: Path) -> None:
    """同一份 run.json 的第二次请求不再解析文件、也不再打 Sentrix。

    158 实测：面板开着时浏览器约 0.55 次/秒轮询它，每次 0.6–1.2s CPU。这里钉住
    「命中缓存」与「run 变了就失效」两种行为——只测前一种的话，把 revision 写死也能绿。
    """
    store = _write(tmp_path, "run-1", _state(items=[]))
    http = FakeJsonHttpClient({"/assets?": {"items": [_image("a1")]}})
    reports = _reports(store, http)

    first = reports.memory_effectiveness("run-1")
    second = reports.memory_effectiveness("run-1")

    assert first == second
    assert len(http.calls) == 1

    # 返回的必须是副本：调用方改脏了缓存里的那份，下一次读到的东西就变了。
    second["data_quality"]["hit_match_source"] = "被调用方改脏"
    assert reports.memory_effectiveness("run-1")["data_quality"]["hit_match_source"] != "被调用方改脏"

    # run.json 变了 → revision 变 → 必须重算，资产清单要再取一次。
    FileResultWriter(tmp_path).write_snapshot("run-1", _state(items=[{"qa_id": "q0"}, {"qa_id": "q1"}]))
    reports.memory_effectiveness("run-1")
    assert len(http.calls) == 2


def test_memory_effectiveness_cache_expires_after_the_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """revision 没变、但资产清单可能变了：30s 后必须重算。"""
    import types

    import photobench.application.runs.reports as reports_module

    clock = [1000.0]
    # 只替换 reports 模块自己的 ``time`` 引用，不去改全局 time 模块。
    monkeypatch.setattr(reports_module, "time", types.SimpleNamespace(monotonic=lambda: clock[0]))
    store = _write(tmp_path, "run-1", _state(items=[]))
    http = FakeJsonHttpClient({"/assets?": {"items": [_image("a1")]}})
    reports = _reports(store, http)

    reports.memory_effectiveness("run-1")
    clock[0] += 29
    reports.memory_effectiveness("run-1")
    assert len(http.calls) == 1

    clock[0] += 2  # 累计 31s，越过 TTL
    reports.memory_effectiveness("run-1")
    assert len(http.calls) == 2


def test_keyframe_analysis_uses_the_same_memo(tmp_path: Path) -> None:
    """关键帧面板同样会被轮询（158 实测 0.27 次/秒），它也要走同一份缓存。"""
    store = _write(tmp_path, "run-1", _state(items=[]))
    http = FakeJsonHttpClient({"/assets?": {"items": [_image("a1")]}})
    reports = _reports(store, http)

    first = reports.keyframe_analysis("run-1")
    second = reports.keyframe_analysis("run-1")

    assert first == second
    assert len(http.calls) == 1

    # 两个面板的缓存键必须分开：同一次修订下，记忆有效性不能把关键帧的结果顶掉。
    reports.memory_effectiveness("run-1")
    assert len(http.calls) == 2


def test_legacy_store_without_revision_is_supported_without_caching():
    class LegacyReader:
        def load_state(self, run_id):
            assert run_id == "run-1"
            return _state(run_id)

    http = FakeJsonHttpClient({"/assets?": {"items": []}})
    reports = RunReports(store=LegacyReader(), http=http, sentrix_url=lambda: SENTRIX)

    reports.memory_effectiveness("run-1")
    reports.memory_effectiveness("run-1")

    assert len(http.calls) == 2  # No revision means no stale memoized projection.


def test_unversioned_legacy_run_json_is_read_fresh_instead_of_cached(tmp_path):
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps(_state()), encoding="utf-8")
    store = FileRunStore(tmp_path)
    assert store.revision("run-1") == ""
    http = FakeJsonHttpClient({"/assets?": {"items": []}})
    reports = _reports(store, http)

    reports.memory_effectiveness("run-1")
    reports.memory_effectiveness("run-1")

    assert len(http.calls) == 2
