"""Local lifecycle/failure regressions: no device or inference dependencies."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_run_read import _NoQaMetadata, _state, _write
from test_suites import FakeRunStore, _cancelled_state, _suites, _wait_until

from photobench.api.routes.suites import save_reviews
from photobench.application.run_execution.state import RunState
from photobench.application.runs.index import RunIndex
from photobench.application.runs.read_runs import ReadRuns
from photobench.infrastructure.results.store import FileResultWriter, FileRunStore


@pytest.mark.parametrize("mode", ["full", "build", "reuse"])
def test_completed_suites_release_execution_and_thread_objects(tmp_path, mode):
    suites = _suites(tmp_path)
    for _ in range(100):
        suites.start_suite(
            {
                "album_id": "album-x",
                "qa_set": "qa-1",
                "models": ["qwen"],
                "mode": mode,
                "existing_scope_id": "scope-old" if mode == "reuse" else "",
            }
        )
        assert _wait_until(lambda: not suites.active_run_ids())
    assert _wait_until(lambda: not suites._threads)
    assert not suites._active


def test_terminal_cache_detects_rejudge_updates_and_deletion(tmp_path):
    _write(tmp_path, "r", _state("r"))
    store = FileRunStore(tmp_path)
    reader = ReadRuns(store, RunIndex(), _NoQaMetadata(), SimpleNamespace(), lambda: "unused")
    assert reader.get_run("r")["summary"]["answer_quality_mean"] == 1
    state = _state("r")
    state["items"][0]["judge"]["score"] = 0
    # 走生产写侧（临时文件 + replace）：同 inode 的原地重写在某些文件系统上会与
    # 上一次共享 ``st_mtime_ns``，revision 于是不变，缓存看起来「没失效」。
    FileResultWriter(tmp_path).write_snapshot("r", state)
    assert reader.get_run("r")["summary"]["answer_quality_mean"] == 0
    FileResultWriter(tmp_path).delete_run("r")
    with pytest.raises(KeyError):
        reader.get_run("r")


def test_snapshot_uses_state_lock_and_writer_error_still_stops():
    entered, release = threading.Event(), threading.Event()
    state = RunState({}, run_id="r", debounce=0)

    class Probe:
        def __deepcopy__(self, memo):
            entered.set()
            assert state.lock._is_owned()
            return "consistent"

    class Writer:
        def write_snapshot(self, run_id, snapshot):
            assert snapshot["probe"] == "consistent"
            raise OSError("synthetic disk full")

    state.data["probe"] = Probe()
    state._writer = Writer()
    state.start_persist_writer()
    try:
        with pytest.raises(RuntimeError, match="synthetic disk full"):
            state.persist(wait=True)
        assert entered.is_set()
        with pytest.raises(RuntimeError, match="synthetic disk full"):
            state.stop_persist_writer()
        assert state._persist_thread is not None
        assert not state._persist_thread.is_alive()
    finally:
        release.set()
        with state._persist_condition:
            state._persist_stopping = True
            state._persist_condition.notify_all()
        state._persist_thread.join(2)


def test_result_pair_failure_leaves_previous_committed_state(tmp_path, monkeypatch):
    writer = FileResultWriter(tmp_path)
    writer.write_snapshot("r", {"run_id": "r", "items": [{"answer": "before"}]})
    original = Path.write_text

    def fail_results(path, *args, **kwargs):
        if "results" in path.name:
            raise OSError("synthetic second-file failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_results)
    with pytest.raises(OSError):
        writer.write_snapshot("r", {"run_id": "r", "items": [{"answer": "after"}]})
    assert FileRunStore(tmp_path).load_state("r")["items"] == [{"answer": "before"}]
    assert json.loads((tmp_path / "r/results.jsonl").read_text()) == {"answer": "before"}


def test_async_write_route_does_not_block_event_loop():
    async def check():
        entered, release = threading.Event(), threading.Event()

        class Service:
            def save_reviews(self, run_id, payload):
                entered.set()
                assert release.wait(2)
                return {"saved": True}

        class Request:
            app = SimpleNamespace(state=SimpleNamespace(container=SimpleNamespace(run_suites=Service())))

            async def body(self):
                return b"{}"

        task = asyncio.create_task(save_reviews(Request(), "r"))
        try:
            # A separate thread releases even a broken synchronous route.
            timer = threading.Timer(0.5, release.set)
            timer.start()
            began = time.monotonic()
            while not entered.is_set() and time.monotonic() - began < 1:
                await asyncio.sleep(0.001)
            assert time.monotonic() - began < 0.25
        finally:
            release.set()
            await task
            timer.cancel()

    asyncio.run(check())


def test_stalled_writer_has_bounded_stop_and_reports_pending():
    entered, release = threading.Event(), threading.Event()

    class Writer:
        def write_snapshot(self, run_id, snapshot):
            entered.set()
            assert release.wait(2)

    state = RunState({}, run_id="slow", writer=Writer(), debounce=0, persist_timeout=0.05)
    state.start_persist_writer()
    state.persist()
    assert entered.wait(1)
    began = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="did not stop"):
            state.stop_persist_writer()
        assert time.monotonic() - began < 0.5
        assert state.get("persistence")["status"] == "stop_pending"
        assert state._persist_written == 0
        assert state._persist_stopping
    finally:
        release.set()
        state._persist_thread.join(1)
    assert not state._persist_thread.is_alive()


def test_new_checkpoint_reads_skip_item_traces_and_raw_telemetry(tmp_path, monkeypatch):
    writer = FileResultWriter(tmp_path)
    saved = _state("r")
    saved["telemetry_live"] = {"history": [], "latest": {"memory_used_mib": None}}
    writer.write_snapshot("r", saved)
    store = FileRunStore(tmp_path)
    reader = ReadRuns(store, RunIndex(), _NoQaMetadata(), SimpleNamespace(), lambda: "unused")
    original = Path.read_text

    def guarded(path, *args, **kwargs):
        assert path.name not in {"run.json", "gpu_samples.jsonl"}, path
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded)
    view = reader.get_run("r", include_history=False)
    assert view["item_count"] == len(saved["items"])
    assert view["summary"]["answer_quality_mean"] == 1
    assert view["telemetry_live"]["latest"]["memory_used_mib"] is None
    assert view["history_mode"] == "checkpoint"
    assert view["read_revision"]
    assert "items" not in view


def test_full_history_projection_cache_tracks_appends_and_rejudge(tmp_path, monkeypatch):
    writer = FileResultWriter(tmp_path)
    saved = _state("r")
    saved["telemetry_live"] = {"history": []}
    writer.write_snapshot("r", saved)
    store = FileRunStore(tmp_path)
    reader = ReadRuns(store, RunIndex(), _NoQaMetadata(), SimpleNamespace(), lambda: "unused")
    read_lines = store.read_gpu_sample_lines
    calls = []

    def read(run_id):
        calls.append(run_id)
        return read_lines(run_id)

    monkeypatch.setattr(store, "read_gpu_sample_lines", read)
    first = reader.get_run("r")
    first["summary"]["answer_quality_mean"] = 999  # Returned objects must not poison cached projections.
    assert reader.get_run("r")["summary"]["answer_quality_mean"] == 1
    assert len(calls) == 1
    writer.append_gpu_sample("r", {"_t": 1, "memory_used_mib": None})
    reader.get_run("r")
    assert len(calls) == 2
    saved["items"][0]["judge"]["score"] = 0
    writer.write_snapshot("r", saved)
    assert reader.get_run("r")["summary"]["answer_quality_mean"] == 0
    assert len(calls) == 3


def test_large_state_is_readable_but_does_not_enter_object_cache(tmp_path, monkeypatch):
    import photobench.application.runs.read_runs as reads

    _write(tmp_path, "r", _state("r"))
    reader = ReadRuns(
        FileRunStore(tmp_path), RunIndex(), _NoQaMetadata(), SimpleNamespace(), lambda: "unused"
    )
    monkeypatch.setattr(reads, "_STATE_CACHE_BYTES", 1)
    assert reader.get_run_item("r", 0)["item"]["qa_id"]
    assert not reader._states
    assert not reader._state_weights


def test_concurrent_generations_are_complete_and_old_stages_are_bounded(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from photobench.infrastructure.results.store import committed_path, run_lock

    writer, store = FileResultWriter(tmp_path), FileRunStore(tmp_path)
    writer.write_snapshot("r", {"run_id": "r", "items": [{"version": 0}]})

    def update(version):
        writer.write_snapshot("r", {"run_id": "r", "items": [{"version": version}]})
        with run_lock(tmp_path / "r"):
            saved = store.load_state("r")
            rows = [
                json.loads(line)
                for line in committed_path(tmp_path / "r", "results.jsonl").read_text().splitlines()
            ]
            assert rows == saved["items"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(update, range(1, 25)))
    assert len(list((tmp_path / "r/.snapshots").iterdir())) == 2
    assert not list((tmp_path / "r").rglob("*.tmp"))


def test_recovery_uses_commit_instead_of_stale_legacy_export(tmp_path):
    from photobench.infrastructure.results.recovery import FileRunStartupRecovery

    writer = FileResultWriter(tmp_path)
    writer.write_snapshot("r", {"run_id": "r", "status": "running", "items": []})
    # A crash may leave the export stale. It must neither override the commit nor
    # mutate immutable generation bytes (exports are independent copies).
    (tmp_path / "r/run.json").write_text('{"run_id":"r","status":"completed"}')
    lease = FileRunStartupRecovery(tmp_path)
    try:
        assert lease.acquire_and_recover() == 1
        state = FileRunStore(tmp_path).load_state("r")
        assert state["status"] == "interrupted"
        backup = tmp_path / "r" / state["recovery"]["backup"]
        assert json.loads(backup.read_text())["status"] == "running"
    finally:
        lease.close()


def test_many_legacy_records_are_not_reparsed_on_warm_start(tmp_path, monkeypatch):
    from photobench.infrastructure.results.recovery import FileRunStartupRecovery

    for index in range(1000):
        _write(tmp_path, str(index), {"run_id": str(index), "status": "completed", "items": []})
    store = FileRunStore(tmp_path)
    assert len(list(store.iter_entries())) == 1000
    original = Path.read_text
    reads = []

    def track(path, *args, **kwargs):
        if path.name == "run.json":
            reads.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", track)
    lease = FileRunStartupRecovery(tmp_path)
    try:
        assert lease.acquire_and_recover() == 0
        assert len(list(store.iter_entries())) == 1000
        assert not reads
    finally:
        lease.close()


def test_persistence_failure_is_visible_even_when_its_diagnosis_cannot_be_written(tmp_path):
    saved = {**_state("r"), "status": "running"}
    FileResultWriter(tmp_path).write_snapshot("r", saved)
    index = RunIndex()
    index.upsert(
        {
            "run_id": "r",
            "status": "failed",
            "finished_at": "2026-10-23T01:00:00Z",
            "fatal_error": "save failed",
            "persistence": {"status": "failed", "error": "disk full"},
        }
    )
    reader = ReadRuns(FileRunStore(tmp_path), index, _NoQaMetadata(), SimpleNamespace(), lambda: "unused")
    result = reader.get_run("r", include_history=False)
    assert result["status"] == "failed"
    assert result["finished_at"] == "2026-10-23T01:00:00Z"
    assert result["fatal_error"] == "save failed"
    assert result["persistence"] == {
        "status": "failed",
        "error": "disk full",
        "diagnostic_source": "current_process",
        "checkpoint_status": "running",
    }
    assert FileRunStore(tmp_path).load_state("r")["status"] == "running"


def test_resumed_execution_also_releases_registry(tmp_path):
    suites = _suites(tmp_path, store=FakeRunStore({"old": _cancelled_state()}))
    resumed = suites.resume_run("old")
    assert _wait_until(lambda: not suites._threads)
    assert resumed["run_id"] not in suites._active


def test_active_workers_prevent_deletion(tmp_path):
    from photobench.domain.errors import InvalidRequestError

    gate = threading.Event()
    suites = _suites(tmp_path, gate=gate)
    run_id = suites.start_suite({"album_id": "album-x", "qa_set": "qa-1", "models": ["qwen"]})["run_ids"][0]
    try:
        with pytest.raises(InvalidRequestError, match="workers have stopped"):
            suites.delete_run(run_id)
    finally:
        gate.set()
    assert _wait_until(lambda: not suites._threads)


def test_failed_legacy_export_does_not_accumulate_obsolete_checkpoints(tmp_path, monkeypatch):
    from photobench.infrastructure.results import store

    writer = store.FileResultWriter(tmp_path)

    def fail_copy(*args, **kwargs):
        raise OSError("legacy export unwritable")

    monkeypatch.setattr(store.shutil, "copyfile", fail_copy)
    for index in range(5):
        writer.write_snapshot("r", {"run_id": "r", "status": "completed", "items": [], "counter": index})
    assert store.FileRunStore(tmp_path).load_state("r")["counter"] == 4
    assert len(list((tmp_path / "r" / ".snapshots").iterdir())) == 2


def test_pointer_directory_sync_failure_is_reported_without_deleting_published_data(tmp_path, monkeypatch):
    from photobench.infrastructure.results import store

    writer = store.FileResultWriter(tmp_path)
    writer.write_snapshot("r", {"run_id": "r", "status": "completed", "items": [], "counter": 0})
    original = store._sync_directory

    def fail_root(path):
        if path == tmp_path / "r":
            raise OSError("directory sync failed")
        original(path)

    monkeypatch.setattr(store, "_sync_directory", fail_root)
    with pytest.raises(OSError, match="directory sync failed"):
        writer.write_snapshot("r", {"run_id": "r", "status": "completed", "items": [], "counter": 1})
    assert store.FileRunStore(tmp_path).load_state("r")["counter"] == 1
    assert json.loads((tmp_path / "r" / "run.json").read_text())["counter"] == 0
