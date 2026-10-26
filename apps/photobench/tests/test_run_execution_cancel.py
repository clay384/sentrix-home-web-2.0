"""取消感知与后台落盘的用例（``RunState`` + ``RunExecution``）。

S9b-2 如实记过两笔账：**后台 persist 写线程没有搬**（1.0 ``_persist_loop``），
**等待不是取消感知的**（``_wait_model_ready`` / ``_await_turn`` 里的 ``sleep`` 换掉了
``self._cancel.wait``）。这一份把那两笔账钉住——它们都是「没有异常、只是慢/只是停不下来」
的失败，靠别处看不出来。
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

import pytest
from fakes_execution import (
    FakeAlbums,
    FakeInference,
    FakeLifecycle,
    FakeResultWriter,
    FakeSentrixClient,
    FakeTelemetry,
)

from photobench.application.run_execution.service import RunExecution, RunRequest
from photobench.application.run_execution.state import RunState
from photobench.domain.runs.execution import RunCancelledError

ALBUM = "album-x"
QA_SET = "qa-1"


def _execution(*, sleep: Any = None) -> tuple[RunExecution, FakeResultWriter]:
    writer = FakeResultWriter()
    execution = RunExecution(
        client=FakeSentrixClient(),
        albums=FakeAlbums(
            manifest={"album_id": ALBUM, "album_name": "x", "photos": [], "videos": [], "faces": []},
            qa_rows=[],
        ),
        writer=writer,
        lifecycle=FakeLifecycle(),
        inference=FakeInference(),
        telemetry=FakeTelemetry(),
        sleep=sleep if sleep is not None else (lambda _seconds: None),
    )
    return execution, writer


def _request(**overrides: Any) -> RunRequest:
    base: dict[str, Any] = {
        "album_id": ALBUM,
        "qa_set": QA_SET,
        "model_profile": "qwen",
        "sentrix_url": "http://sentrix:8191",
        "run_id": "run_test",
    }
    base.update(overrides)
    return RunRequest(**base)


# ---- RunState：后台落盘 ---------------------------------------------------


def test_persist_wait_writes_the_current_snapshot_through_the_writer():
    writer = FakeResultWriter()
    state = RunState({"run_id": "run_x", "status": "pending"}, run_id="run_x", writer=writer)
    state.start_persist_writer()
    try:
        state.set("status", "completed")
        state.persist(wait=True)
        assert writer.last_state()["status"] == "completed"
    finally:
        state.stop_persist_writer()


def test_persist_without_a_writer_is_a_noop():
    """用例里的 ``RunState({"current_phase": ...})`` 没有写线程——它不该要求一个。"""
    state = RunState({"current_phase": "qa_eval"})
    state.persist()
    state.persist(wait=True)
    state.start_persist_writer()
    state.stop_persist_writer()


def test_stop_persist_writer_flushes_the_last_requested_snapshot():
    """收尾必须**等到最后一份落盘**（1.0 ``_stop_persist_writer`` ``:2095``）。

    **区分力**：不等就直接停线程的实现会让最后几次 ``persist()`` 丢在队列里——
    run.json 停在倒数第二个相位，而 run 看起来是成功的。
    """
    writer = FakeResultWriter()
    state = RunState({"run_id": "run_x", "status": "running"}, run_id="run_x", writer=writer)
    state.start_persist_writer()
    state.set("status", "completed")
    state.set("finished_at", "now")
    state.persist()
    state.stop_persist_writer()
    assert writer.last_state()["status"] == "completed"
    assert writer.last_state()["finished_at"] == "now"


def test_a_write_failure_surfaces_on_the_next_waiting_persist():
    """磁盘写失败要能被下一个 ``persist(wait=True)`` 看见——静默丢快照比报错糟得多。"""

    class _BrokenWriter(FakeResultWriter):
        def write_snapshot(self, run_id: str, state: dict[str, Any]) -> None:
            raise OSError("disk full")

    writer = _BrokenWriter()
    state = RunState({"run_id": "run_x"}, run_id="run_x", writer=writer)
    state.start_persist_writer()
    try:
        with pytest.raises(RuntimeError, match="persistence failed"):
            state.persist(wait=True)
    finally:
        # 收尾也会去等那份坏快照并同样抛出——这里吞掉它，断言的是上面那一次。
        with contextlib.suppress(RuntimeError):
            state.stop_persist_writer()


def test_a_completed_chain_leaves_the_final_state_on_disk():
    """整条主链跑完之后，落在 writer 上的最后一份就是返回的那份状态（不收尾就丢）。

    空相册那一轮会是 ``completed_with_errors``（``pipeline_processing`` 压成 partial），
    所以断言的是**两份一致**而不是某个具体的状态词。
    """
    execution, writer = _execution()
    state = execution.execute(_request())
    assert writer.last_state()["status"] == state["status"]
    assert writer.last_state()["run_id"] == "run_test"
    assert writer.last_state()["finished_at"]


# ---- 取消感知 -------------------------------------------------------------


def test_cancel_wait_returns_immediately_when_the_event_is_already_set():
    """真等待路径（``Event.wait``）：事件已置位时**不许睡满**。

    **区分力**：一个写成 ``sleep(seconds)`` 的实现会在这里真的睡 30 秒——用例会超时，
    而不是断言失败；这正是「等待不是取消感知的」的形态。
    """
    execution, _writer = _execution(sleep=None)  # 不注入：走 time.sleep 那一支
    execution._state = RunState({"status": "running"})
    execution._state.cancel_event.set()
    with pytest.raises(RunCancelledError):
        execution._cancel_wait(30)


def test_cancel_wait_raises_after_an_injected_wait_too():
    """注入假等待的路径走的是「等完再看事件」——两种模式下的取消语义必须一致。"""
    execution, _writer = _execution()
    execution._state = RunState({"status": "running"})
    execution._state.cancel_event.set()
    with pytest.raises(RunCancelledError):
        execution._cancel_wait(30)


def test_await_turn_stops_polling_when_the_run_is_cancelled():
    """``_await_turn`` 的轮询必须能被取消打断（S9b-2 记的那笔账）。"""
    execution, _writer = _execution()
    execution._state = RunState({"status": "running"})
    execution._state.cancel_event.set()
    execution.client = FakeSentrixClient(turns={"turn_1": [{"turn_id": "turn_1", "status": "pending"}]})
    with pytest.raises(RunCancelledError):
        execution._await_turn("turn_1", _request())


def test_a_cancel_arriving_mid_run_lands_as_cancelled_not_failed():
    """跑着的时候取消：主链收成 ``cancelled``，**不是** ``failed``。

    **怎么让取消落在确定的位置**：让主 API 客户端在 ``list_assets``（``pipeline_processing``
    相位里的第一次调用）里置取消信号——下一个 ``_cancel_wait`` 就抛
    ``RunCancelledError``。这走的是**真的 execute**，不是把判据在用例里重写一遍。

    **区分力**：不分「取消」与「失败」的实现会落成 ``status="failed"`` 并写一个
    ``fatal_error``——读侧于是把一次主动叫停显示成「这轮评测炸了」。
    """
    execution, writer = _execution()
    client = _CancelOnListAssets()
    client.execution = execution
    execution.client = client
    state = execution.execute(_request())
    assert state["status"] == "cancelled"
    assert state["cancelled_phase"] == "pipeline_processing"
    assert writer.last_state()["status"] == "cancelled"


class _CancelOnListAssets(FakeSentrixClient):
    """第一次 ``list_assets`` 时对当前 run 置取消信号（模拟「跑着的时候有人点了取消」）。"""

    execution: Any = None

    def list_assets(self, **kwargs: Any) -> list[dict[str, Any]]:
        if self.execution is not None and self.execution.state is not None:
            self.execution.state.request_cancel("test")
        return super().list_assets(**kwargs)


def test_cancel_never_calls_a_remote_batch_endpoint():
    """1.0 的 ``_cancel_remote_batch``（``:2264``）打 ``/api/v1/ingest-batches/{id}/cancel``
    ——**1.0 与 2.0 都不存在的死端点**，1.0 那边还被 ``except Exception`` 吞成静默 404。

    2.0 的导入是**同步**的（``POST /api/v1/imports`` 一个请求跑完整条链），既没有批量
    实体也没有可取消的后台任务。所以取消路径**一次主 API 调用都不该发**——那条调用
    只会往 run.json 里写一个假的 ``batch_cancel_requested``。

    **区分力**：把 1.0 那段照搬回来的实现会在这里留下一次 ``client`` 调用。
    """
    execution, writer = _execution()
    client = FakeSentrixClient()
    execution.client = client
    execution._state = RunState({"status": "running"}, run_id="run_test", writer=writer)
    execution._request = _request()
    execution.cancel("api")
    assert client.calls == []


def test_cancel_is_recorded_on_the_state_with_its_source():
    state = RunState({"status": "running"})
    state.request_cancel("run_api")
    assert state.cancelled is True
    assert state.get("cancel_source") == "run_api"
    assert state.get("status") == "cancelling"
    assert state.get("cancel_requested_at")


def test_cancelling_a_pending_run_lands_it_as_cancelled_not_cancelling():
    """还没开跑的 run 被取消后就是 ``cancelled``（1.0 ``:2237``）——没有「正在停」可言。"""
    state = RunState({"status": "pending"})
    state.request_cancel("api")
    assert state.get("status") == "cancelled"
    assert state.get("finished_at")


def test_cancel_reclaims_a_model_this_run_started():
    """取消时尽力回收这一轮起过的模型：正在装载的模型不该留在 GPU 上（1.0 ``:2245``）。"""
    lifecycle = FakeLifecycle(state={"profile": "qwen", "served_model_name": "qwen"})
    writer = FakeResultWriter()
    execution = RunExecution(
        client=FakeSentrixClient(),
        albums=FakeAlbums(manifest={"album_id": ALBUM, "photos": [], "videos": [], "faces": []}, qa_rows=[]),
        writer=writer,
        lifecycle=lifecycle,
        inference=FakeInference(),
        telemetry=FakeTelemetry(),
        sleep=lambda _seconds: None,
    )
    state = RunState({"status": "running"}, run_id="run_test", writer=writer)
    execution._state = state
    execution._request = _request(manager_url="http://manager:8500")
    execution.cancel("api")
    assert "stop" in lifecycle.call_names()
    assert state.get("cancel_vllm_stopped")


def test_cancel_does_not_stop_a_model_another_run_started():
    """Manager 报的 profile 与本轮不一致 → 那个模型不是这一轮起的，停了会踩到别人。"""
    lifecycle = FakeLifecycle(state={"profile": "other", "served_model_name": "other"})
    writer = FakeResultWriter()
    execution = RunExecution(
        client=FakeSentrixClient(),
        albums=FakeAlbums(manifest={"album_id": ALBUM, "photos": [], "videos": [], "faces": []}, qa_rows=[]),
        writer=writer,
        lifecycle=lifecycle,
        inference=FakeInference(),
        telemetry=FakeTelemetry(),
        sleep=lambda _seconds: None,
    )
    execution._state = RunState({"status": "running"}, run_id="run_test", writer=writer)
    execution._request = _request(manager_url="http://manager:8500")
    execution.cancel("api")
    assert "stop" not in lifecycle.call_names()


def test_cancel_without_a_manager_does_not_touch_the_lifecycle():
    lifecycle = FakeLifecycle()
    writer = FakeResultWriter()
    execution = RunExecution(
        client=FakeSentrixClient(),
        albums=FakeAlbums(manifest={"album_id": ALBUM, "photos": [], "videos": [], "faces": []}, qa_rows=[]),
        writer=writer,
        lifecycle=lifecycle,
        inference=FakeInference(),
        telemetry=FakeTelemetry(),
        sleep=lambda _seconds: None,
    )
    execution._state = RunState({"status": "running"}, run_id="run_test", writer=writer)
    execution._request = _request()
    execution.cancel("api")
    assert lifecycle.calls == []


def test_a_real_writer_gets_a_serializable_state(tmp_path):
    """**state 里不许有 ``json.dumps`` 写不出去的东西。**

    这条用例的来历是一次真机验收：``execute`` 曾把 ``RunRequest`` 放进 ``state``
    （好让 ``cancel()`` 读得到 manager_url），于是**每一次**快照写都抛 ``TypeError``。
    失败被后台写线程收进 ``_persist_error``，外面的表现只是「run.json 一直没出现」
    ——而 ``gpu_samples.jsonl`` 照常增长，看起来像「跑得好好的」。所以这条直接跑
    真写入器，断言 run.json 真在盘上、且能读回来。
    """
    from photobench.infrastructure.results.store import FileResultWriter

    writer = FileResultWriter(tmp_path)
    execution, _fake = _execution()
    execution.writer = writer
    state = execution.execute(_request())
    path = tmp_path / "run_test" / "run.json"
    assert path.is_file(), "后台写线程没能把 run.json 写出来"
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["run_id"] == "run_test"
    assert on_disk["status"] == state["status"]


@pytest.mark.parametrize(
    "cancel_at",
    ["before_conversation", "after_conversation", "between_turns", "first_in_flight", "second_in_flight"],
)
def test_item_cancellation_preserves_dispatched_turns_without_new_requests(cancel_at):
    execution, _ = _execution()
    state = RunState({"status": "running"})
    execution._state = state

    class Client(FakeSentrixClient):
        conversations_started = 0
        turns_started = 0

        def start_conversation(self, **kwargs):
            self.conversations_started += 1
            if cancel_at == "after_conversation":
                state.request_cancel("test")
            return {"id": "conversation"}

        def start_turn(self, **kwargs):
            self.turns_started += 1
            return {"turn_id": f"turn-{self.turns_started}"}

        def get_turn(self, **kwargs):
            in_flight = cancel_at == "first_in_flight" or (
                cancel_at == "second_in_flight" and self.turns_started == 2
            )
            if in_flight or cancel_at == "between_turns":
                state.request_cancel("test")
            return (
                {"status": "running"}
                if in_flight
                else {
                    "status": "completed",
                    "result": {"answer": "fixture", "termination_reason": "completed"},
                }
            )

    client = Client()
    execution.client = client
    if cancel_at == "before_conversation":
        state.request_cancel("test")
    result = execution._evaluate_one(
        {"qa_id": "qa", "question": "fixture", "conversation": [{"message": "one"}, {"message": "two"}]},
        "scope",
        {},
        _request(turn_timeout=4200),
    )
    expected_calls = {
        "before_conversation": 0,
        "after_conversation": 0,
        "between_turns": 1,
        "first_in_flight": 1,
        "second_in_flight": 2,
    }[cancel_at]
    unknown = cancel_at.endswith("in_flight")
    assert client.turns_started == expected_calls
    assert client.conversations_started == int(cancel_at != "before_conversation")
    assert result["execution_status"] == ("outcome_unknown" if unknown else "cancelled")
    assert result["request_failure"]["request_outcome_unknown"] is unknown
    assert result["request_failure"]["automatic_replay"] is False
    completed_count = int(cancel_at in {"between_turns", "second_in_flight"})
    assert result["request_failure"]["completed_turn_count"] == completed_count
    assert len(result["runtime_turns"]) == completed_count
    if completed_count:
        assert result["runtime_turns"][0]["turn_id"] == "turn-1"
        assert result["runtime_turns"][0]["answer"] == "fixture"
    if unknown:
        assert result["request_failure"]["turn_id"] == f"turn-{expected_calls}"
    assert result["retrieval_recall"] is None
    assert result["media_retrieval_recall"] is None
    assert result["judge"]["score"] is None
    assert result["judge_status"] == "skipped"
    assert RunExecution.resume_item(result)["execution_status"] == result["execution_status"]
