"""run-execution 主链的用例：建空间 → 种身份 → 导入 → 处理 → 问答。

**这一份钉的是「主链真的按 2.0 的接口走完了」**，不是「跑得通」：每一相位都断言
**具体的调用形状**（调了谁、带什么参数、几次），因为一个只断言「状态是 completed」的
用例对下面这些错误全都没有区分力——少调一次 seed、把 limit 填错、把 partial 回合
当成失败、把停滞判据退化成轮询计数。

``sleep`` 一律注入 ``lambda _: None``：主链里有冷却、退避、轮询三处等待，**真睡一秒
都会让这一份用例从秒级变成分钟级**，而它们与任何断言都无关。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import pytest
from fakes_deploy import FakeSentrixProcess
from fakes_execution import (
    FakeAlbums,
    FakeInference,
    FakeLifecycle,
    FakeResultWriter,
    FakeSentrixClient,
    FakeTelemetry,
)

from photobench.application.deploy.service import SentrixModelSwitch
from photobench.application.run_execution.service import RunExecution, RunRequest
from photobench.application.run_execution.state import RunState
from photobench.domain.runs.execution import RunCancelledError

ALBUM = "album-x"
QA_SET = "qa-1"


def _manifest() -> dict[str, Any]:
    return {
        "album_id": ALBUM,
        "album_name": "测试相册",
        "photos": ["photos/a.jpg", "photos/b.jpg"],
        "videos": [],
        "faces": [
            {"canonical_name": "明明", "family_role": "大宝", "aliases": ["小明"], "ref_image": "faces/1.jpg"}
        ],
        "family_relationships": [{"subject": "我", "predicate": "妻子", "object": "芳芳"}],
    }


def _rows() -> list[dict[str, Any]]:
    return [
        {
            "qa_id": "q1",
            "question": "在海边的照片有哪些？",
            "answer": "a.jpg",
            "retrieval_image_ids": ["a.jpg"],
            "answerability": "answerable",
        }
    ]


def _assets() -> list[dict[str, Any]]:
    return [
        {"id": "asset_a", "file_name": "a.jpg", "media_type": "image", "status": "processed"},
        {"id": "asset_b", "file_name": "b.jpg", "media_type": "image", "status": "processed"},
    ]


def _completed_turn(
    turn_id: str = "turn_1", *, attachments: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "turn_id": turn_id,
        "status": "complete",
        "public_progress": [
            {
                "text": "已确认需要哪些证据。",
                "status": "complete",
                "stage": "planning",
                "step_index": 1,
                "timestamp": "t",
                "kind": "plan",
                "payload": {"requirements": [{"id": "req_1"}]},
            },
            {
                "text": "正在执行 search_memories…",
                "status": "running",
                "stage": "action",
                "step_index": 2,
                "timestamp": "t",
                "kind": "model_action",
                "payload": {"tool": "search_memories", "tool_call_id": "c1"},
            },
            {
                "text": "工具执行完成。",
                "status": "ok",
                "stage": "tool_result",
                "step_index": 3,
                "timestamp": "t",
                "kind": "tool_result",
                "payload": {"tool": "search_memories", "tool_call_id": "c1"},
            },
            {
                "text": "完成。",
                "status": "complete",
                "stage": "terminal",
                "step_index": 4,
                "timestamp": "t",
                "kind": "terminal",
                "payload": {"status": "complete", "termination_reason": "answered"},
            },
        ],
        "result": {
            "type": "complete",
            "turn_id": turn_id,
            "status": "complete",
            "termination_reason": "answered",
            "answer": "海边的是 a.jpg",
            "attachments": attachments
            if attachments is not None
            else [{"handle": "h1", "asset_id": "asset_a", "file_name": "a.jpg"}],
            "requirements": [{"id": "req_1"}],
            "retrieved_candidates": ["asset_a", "asset_b"],
            # 助手的**回合级事实**。形状由 ``run_turn._turn_trace`` 组装，这里照抄一份
            # 生产上真的会出现的——三个投影都读它。
            "trace": {
                "include_debug": True,
                "model_calls": [
                    {
                        "step_id": "step_1",
                        "call_type": "agent",
                        "prompt_tokens": 700,
                        "completion_tokens": 40,
                        "duration_ms": 400.0,
                        "tool_calls": ["search_memories"],
                        "finish_reason": "tool_calls",
                    }
                ],
                # 评测侧 include_debug=true 时的完整模型输入/输出。
                "debug_calls": [
                    {
                        "seq": 0,
                        "step_id": "step_1",
                        "call_type": "agent",
                        "status": "complete",
                        "messages": [
                            {"role": "system", "content": "你是相册助手"},
                            {"role": "user", "content": "看海边的照片"},
                        ],
                        "tools": [
                            {
                                "type": "function",
                                "function": {"name": "search_memories", "description": "检索"},
                            }
                        ],
                        "response": {
                            "content": "",
                            "thought": "先检索",
                            "tool_calls": [
                                {"id": "c1", "name": "search_memories", "arguments": "{}"}
                            ],
                            "finish_reason": "tool_calls",
                        },
                    }
                ],
                "planner": {"status": "ok", "fallback_reason": "", "goal": "看海边的照片"},
                "task_state": {
                    "requirements": [
                        {"id": "req_1", "evidence_type": "photo_set", "status": "satisfied"},
                        {
                            "id": "req_2",
                            "evidence_type": "visible_text",
                            "status": "open",
                            "unmet_reason": "not_attempted",
                        },
                    ]
                },
                "evidence_ledger": {"entries": []},
                "guard": [{"code": "internal_id_leak", "hard": True}],
                "budget": {"model_steps": 5, "tool_round_trips": 2},
            },
            "error": "",
        },
        "error": "",
    }


def _polls(*snapshots: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """给「连续两次零待处理才收工」留足轮次：最后一张快照会被重复返回。"""
    return [*snapshots, snapshots[-1], snapshots[-1]]


def _execution(
    *,
    client: FakeSentrixClient | None = None,
    albums: FakeAlbums | None = None,
    telemetry: FakeTelemetry | None = None,
    lifecycle: FakeLifecycle | None = None,
    inference: FakeInference | None = None,
    writer: FakeResultWriter | None = None,
    sentrix_switch: SentrixModelSwitch | None = None,
) -> tuple[RunExecution, FakeSentrixClient, FakeResultWriter, FakeLifecycle, FakeLifecycle]:
    albums = (
        albums
        if albums is not None
        else FakeAlbums(
            manifest=_manifest(),
            qa_rows=_rows(),
            files={"faces/1.jpg": b"J"},
            directory_entries={"photos": 2},
        )
    )
    client = (
        client
        if client is not None
        else FakeSentrixClient(
            asset_polls=_polls(_assets()),
            turns={"turn_1": [_completed_turn()]},
            import_result={"imported": 2, "deduplicated": 0, "events": 1, "summarized": 1, "failures": []},
        )
    )
    writer = writer if writer is not None else FakeResultWriter()
    lifecycle = lifecycle if lifecycle is not None else FakeLifecycle()
    execution = RunExecution(
        client=client,
        albums=albums,
        writer=writer,
        lifecycle=lifecycle,
        inference=inference if inference is not None else FakeInference(),
        telemetry=telemetry if telemetry is not None else FakeTelemetry(),
        sleep=lambda _seconds: None,
        sentrix_switch=sentrix_switch,
    )
    return execution, client, writer, lifecycle, albums  # type: ignore[return-value]


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


@pytest.mark.parametrize("outcome", ["zero", "failed", "exception"])
def test_judge_results_and_progress_are_published_while_an_agent_is_still_running(monkeypatch, outcome):
    rows = [{"qa_id": "fast"}, {"qa_id": "slow"}]
    execution, _, _, _, _ = _execution(albums=FakeAlbums(manifest=_manifest(), qa_rows=rows))
    release = threading.Event()
    published = threading.Event()
    state = RunState({"scope_id": "s", "album_id": ALBUM})

    def evaluate(row, *_):
        if row["qa_id"] == "slow":
            assert release.wait(5), "slow Agent was not released"
        return {"qa_id": row["qa_id"], "execution_status": "completed"}

    def judge_item(item, *_):
        if outcome == "exception":
            raise RuntimeError("external judge unavailable")
        return {
            **item,
            "judge_status": "completed" if outcome == "zero" else "failed",
            "judge": {"score": 0 if outcome == "zero" else None},
        }

    def persist(*_, **__):
        with state.lock:
            phase = state.data.get("phases", {}).get("qa_eval", {})
            if phase.get("progress", {}).get("judge_completed") == 1:
                published.set()

    monkeypatch.setattr(execution, "_evaluate_one", evaluate)
    monkeypatch.setattr(execution, "_judge", SimpleNamespace(judge_item=judge_item))
    monkeypatch.setattr(state, "persist", persist)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(execution._phase_qa_eval, state, _request(qa_concurrency=2))
        try:
            assert published.wait(2), "Judge completion was withheld until all Agents finished"
            with state.lock:
                phase = state.data["phases"]["qa_eval"]
                assert phase["judge_completed"] == phase["progress"]["judge_completed"] == 1
                assert phase["progress"]["judge_total"] == 2
                assert len(state.data["items"]) == 1
                assert state.data["items"][0]["judge"]["score"] == (0 if outcome == "zero" else None)
                assert state.data["items"][0]["judge_status"] == (
                    "completed" if outcome == "zero" else "failed"
                )
                assert "agent_phase_finished_at" not in phase
        finally:
            release.set()
        future.result(timeout=5)
    assert state.data["phases"]["qa_eval"]["progress"]["judge_completed"] == 2


def test_resume_judge_progress_counts_prior_terminal_results_and_new_skips(monkeypatch):
    rows = [{"qa_id": key} for key in ("prior-zero", "prior-failed", "new-failed")]
    execution, _, _, _, _ = _execution(albums=FakeAlbums(manifest=_manifest(), qa_rows=rows))
    monkeypatch.setattr(
        execution, "_judge", SimpleNamespace(judge_item=lambda *_: pytest.fail("skipped QA judged"))
    )
    monkeypatch.setattr(
        execution,
        "_evaluate_one",
        lambda row, *_: {
            "qa_id": row["qa_id"],
            "execution_status": "failed",
            "failed": True,
            "error": "task failed",
        },
    )
    prior = [
        {
            "qa_id": "prior-zero",
            "execution_status": "completed",
            "judge_status": "completed",
            "judge": {"score": 0},
        },
        {
            "qa_id": "prior-failed",
            "execution_status": "completed",
            "judge_status": "failed",
            "judge": {"score": None},
        },
    ]
    state = RunState({"scope_id": "s", "album_id": ALBUM})
    execution._phase_qa_eval(state, _request(prior_items=prior))
    phase = state.data["phases"]["qa_eval"]
    assert phase["judge_completed"] == phase["progress"]["judge_completed"] == 2
    assert phase["progress"]["judge_skipped"] == 1
    assert phase["progress"]["judge_total"] == 3
    assert len(state.data["items"]) == 3


def test_cancelled_unscheduled_qa_is_not_reported_as_a_completed_agent_or_judge(monkeypatch):
    execution, _, _, _, _ = _execution()

    def cancelled(*_):
        raise RunCancelledError("cancelled before starting")

    monkeypatch.setattr(execution, "_evaluate_one", cancelled)
    monkeypatch.setattr(
        execution, "_judge", SimpleNamespace(judge_item=lambda *_: pytest.fail("cancelled QA judged"))
    )
    state = RunState({"scope_id": "s", "album_id": ALBUM})
    execution._phase_qa_eval(state, _request())
    progress = state.data["phases"]["qa_eval"]["progress"]
    assert progress["agent_completed"] == progress["completed"] == progress["judge_completed"] == 0
    assert progress["judge_skipped"] == 1
    assert state.data["items"][0]["execution_status"] == "scheduled"


def test_the_chain_runs_every_phase_in_order_and_completes():
    execution, _client, writer, _lifecycle, _albums = _execution()
    state = execution.execute(_request())
    phases = state["phases"]
    # S9b-3 补上遥测聚合的两个收尾相位（1.0 ``:2356`` 的 ``all_phases`` 末两项）。
    assert list(phases) == [
        "model_deploy",
        "scope_setup",
        "identity_seed",
        "photo_import",
        "pipeline_processing",
        "qa_eval",
        "gpu_metrics",
        "aggregate",
    ]
    statuses = {name: bucket["status"] for name, bucket in phases.items()}
    assert statuses == {
        "model_deploy": "done",
        "scope_setup": "done",
        "identity_seed": "done",
        "photo_import": "done",
        "pipeline_processing": "done",
        "qa_eval": "done",
        "gpu_metrics": "done",
        "aggregate": "done",
    }
    # 汇总相位真的写了 summary（不是留一个空 dict 让读侧去猜）。这一轮**没有配 judge**
    # （`_execution()` 默认 `judge_settings=None`），所以有效分数是 0——`completed`
    # 数的是「判分走到终态的题」，而不是「答过的题」（1.0 的口径，``:4591``）。
    assert state["summary"]["total"] == 1
    assert state["summary"]["judge_valid_count"] == 0
    assert state["items"][0]["judge_status"] == "pending"
    assert state["status"] == "completed"
    assert writer.last_state()["run_id"] == "run_test"


def test_the_scope_is_named_after_the_run_and_never_created_by_its_own_endpoint():
    """2.0 **没有** ``POST /api/memory-spaces``：空间由导入路径内部的 ``ensure`` 建。

    这条断言必须是「没有那个调用」而不是「scope_id 非空」——后者在一个仍然偷偷
    建了空间的实现里同样成立（那个实现会让 2.0 多出一个没人用的空间）。
    """
    execution, client, _writer, _lifecycle, _albums = _execution()
    state = execution.execute(_request())
    assert not any("memory-space" in str(kwargs) for _name, kwargs in client.calls)
    assert state["scope_id"].startswith("PhotoBench-")
    assert state["scope_id"] == state["scope_name"]
    assert state["scope_source"] == "created_by_import"
    assert state["phases"]["scope_setup"]["created_by"] == "import_ensure_space"


def test_identity_seed_sends_each_face_and_the_declared_relationships():
    execution, client, _writer, _lifecycle, _albums = _execution()
    execution.execute(_request())
    seeds = [kwargs for name, kwargs in client.calls if name == "seed_person"]
    assert len(seeds) == 1
    assert seeds[0]["name"] == "明明"
    assert seeds[0]["aliases"] == ["小明"]
    assert seeds[0]["file_name"] == "1.jpg"
    assert seeds[0]["content"] == b"J"
    assert seeds[0]["scope_id"] == "PhotoBench-" + _scope_suffix(client)
    relationships = [kwargs for name, kwargs in client.calls if name == "import_relationships"]
    assert len(relationships) == 1
    assert relationships[0]["entity_by_name"] == {"明明": "ent_明明"}


def _scope_suffix(client: FakeSentrixClient) -> str:
    seed = next(kwargs for name, kwargs in client.calls if name == "seed_person")
    return str(seed["scope_id"])[len("PhotoBench-") :]


def test_one_unseedable_face_does_not_abort_the_run_but_is_recorded():
    """1.0 的 ``seed-batch`` 会从结果里滤掉带 ``error`` 的条目、整段不抛——照搬这条
    语义意味着**一个人种不上不该让整轮评测没有结果**，但那条错误必须留在 run.json 里。
    """
    albums = FakeAlbums(
        manifest={
            **_manifest(),
            "faces": [
                {"canonical_name": "明明", "ref_image": "faces/1.jpg"},
                {"canonical_name": "芳芳", "ref_image": "faces/2.jpg"},
            ],
        },
        qa_rows=_rows(),
        # 两张参考图都在盘上（读得到字节），失败的是 2.0 那一端——这才对得上
        # 「一个人种不上」这句话。文件读不到是另一种失败，它同样被接住，
        # 但那不是这条用例要钉的东西。
        files={"faces/1.jpg": b"J", "faces/2.jpg": b"F"},
        directory_entries={"photos": 2},
    )
    client = FakeSentrixClient(
        asset_polls=_polls(_assets()),
        turns={"turn_1": [_completed_turn()]},
        seed_error={"芳芳": RuntimeError("HTTP 422: 图里没有人脸")},
    )
    execution, _client, _writer, _lifecycle, _albums = _execution(client=client, albums=albums)
    state = execution.execute(_request())
    phase = state["phases"]["identity_seed"]
    assert phase["status"] == "done"
    assert phase["seeded_count"] == 1
    assert phase["failed_count"] == 1
    failed = [item for item in phase["details"] if item.get("error")]
    assert failed[0]["name"] == "芳芳"
    assert "422" in failed[0]["error"]


def test_photo_import_hands_the_server_side_directory_and_the_real_entry_count_as_limit():
    """2.0 的 ``POST /api/v1/imports`` 收**服务端目录**，而 ``limit`` 填小了会**静默**
    少导几张（``ImportAlbum.execute`` 的 ``paths[:limit]``）。

    所以这条要同时钉住路径与那个数字：只钉路径的话，``limit`` 写死 1 也能过，
    而那个实现的后果是整册只导进第一张、没有任何报错。
    """
    execution, client, _writer, _lifecycle, albums = _execution()
    state = execution.execute(_request())
    imports = [kwargs for name, kwargs in client.calls if name == "import_album"]
    assert imports == [
        {
            "scope_id": state["scope_id"],
            "directory": f"/srv/albums/{ALBUM}/photos",
            "limit": 2,
            "timeout": 7200,
        }
    ]
    assert state["phases"]["photo_import"]["accepted_count"] == 2
    assert state["phases"]["photo_import"]["event_count"] == 1
    # manifest 里两个文件落在同一个目录 → 只导一次（``import_directories`` 去重）
    assert ("import_target", "photos") in albums.calls


def test_a_directory_that_loses_files_is_not_silently_ignored():
    """导入返回的 ``failures`` 必须一路走到相位的 ``error_details`` 并把相位压成 partial。

    没有这条，一个「导了但失败了两张」的 run 与一个全成功的 run 在 run.json 里
    完全一样——那正是 1.0 的 ``_phase_partial``（``:2299``）存在的理由。
    """
    client = FakeSentrixClient(
        asset_polls=_polls(_assets()),
        turns={"turn_1": [_completed_turn()]},
        import_result={
            "imported": 1,
            "deduplicated": 0,
            "events": 1,
            "summarized": 1,
            "failures": [{"file_name": "b.jpg", "reason": "不支持的编码"}],
        },
    )
    execution, _client, _writer, _lifecycle, _albums = _execution(client=client)
    state = execution.execute(_request())
    phase = state["phases"]["photo_import"]
    assert phase["status"] == "partial"
    assert phase["failed_count"] == 1
    assert phase["error_details"][0]["sample_id"] == "b.jpg"
    assert state["status"] == "completed_with_errors"


def test_processing_requires_two_consecutive_zero_pending_snapshots():
    """1.0 ``:3010`` 的注释：资产状态行可能晚于批次翻转落地。

    这里**第一个快照就已经是零待处理**——一个只等一次的实现会当场收工，而正确的
    实现会再问一次。夹具让第 2、3 次返回同一份数据，所以「等两次」与「等一次」在
    调用次数上可区分。
    """
    client = FakeSentrixClient(
        asset_polls=[[], []],
        turns={"turn_1": [_completed_turn()]},
        import_result={"imported": 2, "events": 1, "summarized": 1, "failures": []},
    )
    execution, _client, _writer, _lifecycle, _albums = _execution(client=client)
    execution.execute(_request())
    polls = [name for name in client.call_names() if name == "list_assets"]
    # 2 次是收工判据，1 次是 qa_eval 取资产表
    assert len(polls) == 3


class _TickingTime:
    """每次 ``monotonic()`` 前进一秒：让 1200 秒的停滞阈值在一个用例里跑得完。"""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        self.now += 1.0
        return self.now

    def perf_counter(self) -> float:
        return self.now

    def strftime(self, _format: str) -> str:
        return "20260101-000000"


def test_a_scope_with_no_assets_is_partial_not_a_silent_success():
    """导入之后 scope 里没有资产 → 相位压 partial，而不是「处理完了，0 张」。

    1.0 的判据是「这一轮一张都没收进来」（``:2913``）；2.0 的导入会报 ``deduplicated``，
    所以换成本条更准的判据（scope 里到底有没有资产）——**它同时覆盖了 1.0 那条的用场**：
    在空相册上把题全问一遍，产出的是一份全错的 run.json，而那正是这条要拦的事。
    """
    client = FakeSentrixClient(
        asset_polls=[[]],
        turns={"turn_1": [_completed_turn()]},
        import_result={"imported": 0, "deduplicated": 2, "events": 0, "summarized": 0, "failures": []},
    )
    execution, _client, _writer, _lifecycle, _albums = _execution(client=client)
    state = execution.execute(_request())
    phase = state["phases"]["pipeline_processing"]
    assert phase["status"] == "partial"
    assert phase["progress"]["total"] == 0
    assert phase["error_details"][0]["error_type"] == "empty_import"
    assert state["status"] == "completed_with_errors"


def test_the_stall_detector_ignores_status_flapping_inside_the_pending_set(
    monkeypatch: pytest.MonkeyPatch,
):
    """停滞判据用**稳定签名**（聚合计数 + 已离开待处理集合的 (id, 终态) 对）。

    夹具让唯一一个资产在 ``queued`` 与 ``processing`` 之间来回——**状态字符串每次都
    在变，但它从没离开过待处理集合**。按状态字符串判进展的实现会把这个抖动当成
    持续进展，于是永远不触发停滞检测，轮询一直打到夹具的 ``poll_limit`` 上炸掉
    （用例红，且不会挂起）；按稳定签名判的实现会看到「零进展」，到阈值就收工并把
    相位压成 partial。

    这正是 1.0 ``:2988-2996`` 那段注释点名的坑，不是这里发明的新判据。
    """
    # 同一个资产在两种**待处理**状态之间来回：状态字符串每次都变，
    # 但它从没离开 ``PIPELINE_PENDING_STATUSES``。
    flapping = [
        [{"id": "asset_a", "file_name": "a.jpg", "media_type": "image", "status": "queued"}],
        [{"id": "asset_a", "file_name": "a.jpg", "media_type": "image", "status": "processing"}],
    ]
    client = FakeSentrixClient(
        asset_cycle=flapping,
        turns={"turn_1": [_completed_turn()]},
        import_result={"imported": 2, "events": 1, "summarized": 1, "failures": []},
        poll_limit=400,
    )
    execution, _client, _writer, _lifecycle, _albums = _execution(client=client)
    import photobench.application.run_execution.service as service

    monkeypatch.setattr(service, "time", _TickingTime())
    monkeypatch.setenv("PHOTOBENCH_PIPELINE_STALL_TIMEOUT_SECONDS", "300")
    state = execution.execute(_request())
    phase = state["phases"]["pipeline_processing"]
    assert phase["status"] == "partial"
    # ``stalled`` 这个 status 会被随后的 ``phase_partial`` 覆盖成 ``partial``
    # （1.0 亦然：那里的 ``_record_phase(..., "status", "stalled")`` 也只活到收尾前）。
    # 留在 run.json 里的是 ``stalled_at`` 与那句 error——一个不做停滞检测的实现
    # 两样都没有。
    assert phase["stalled_at"]
    assert "没有资产状态变化" in str(phase["error"])
    assert phase["skipped_asset_count"] == 1
    # 301 次 = 阈值 300 秒 + 第 1 次建立基线。**用相位自己的计数**而不是假件的
    # 总调用数：后者还包含 ``qa_eval`` 取资产表的那一次，两者混起来会让这条断言
    # 在无关改动下变红。
    assert phase["poll_iterations"] == 301


def test_a_partial_turn_is_a_completed_item_not_a_failure():
    """2.0 把 ``partial`` 做成了**终态**（``TurnStatus.is_terminal``）。

    1.0 的判据是列举成功值（``{"complete","completed","done","success"}``），照搬它
    会让每一个业务上「答不全」的回合都变成一次轮询超时——而那正是 2.0 用显式终态
    修掉的东西。夹具给的就是 ``partial``，且 ``result`` 里有一句完整的答案。
    """
    partial = _completed_turn()
    partial["status"] = "partial"
    partial["result"]["status"] = "partial"
    partial["result"]["termination_reason"] = "answered_with_gap"
    client = FakeSentrixClient(
        asset_polls=_polls(_assets()),
        turns={"turn_1": [partial]},
        import_result={"imported": 2, "events": 1, "summarized": 1, "failures": []},
    )
    execution, _client, _writer, _lifecycle, _albums = _execution(client=client)
    # ``turn_timeout=0``：**判据必须是「看到终态就收」，不能是「轮询到超时」。** 夹具
    # 对同一个 turn 永远回同一份 ``partial``，所以一个把 ``partial`` 当非终态的实现
    # 会在超时上炸掉（红），而不是把这条用例挂死。正确的实现根本进不了那个循环。
    state = execution.execute(_request(turn_timeout=0))
    item = state["items"][0]
    assert item["execution_status"] == "completed"
    assert item["answer"] == "海边的是 a.jpg"
    assert item["termination_reason"] == "answered_with_gap"
    assert len([name for name in client.call_names() if name == "get_turn"]) == 1


def test_the_delivery_set_is_the_attachments_and_is_scored_against_the_ground_truth():
    """交付集 = ``result.attachments[].asset_id``，GT = 题面里的 ``retrieval_image_ids``。

    这条同时钉住三件事：附件被当成交付集（不是候选集）、asset_id 被解成文件名、
    以及指标确实按这个交集算。改坏任何一环，``image_retrieval_recall`` 就不再是 1.0。
    """
    execution, _client, _writer, _lifecycle, _albums = _execution()
    state = execution.execute(_request())
    item = state["items"][0]
    assert item["selected_asset_ids"] == ["asset_a"]
    assert item["predicted_file_names"] == ["a.jpg"]
    assert item["image_retrieval_recall"] == 1.0
    assert item["image_retrieval_precision"] == 1.0
    assert item["delivery_matched_file_names"] == ["a.jpg"]
    assert item["gt_media"] == [
        {
            "media_type": "image",
            "media_id": "a.jpg",
            "image_id": "a.jpg",
            "video_id": None,
            "file_name": "a.jpg",
            "asset_id": "asset_a",
            "matched": True,
            "mapping_status": "ok",
        }
    ]


def test_a_question_without_an_answer_in_the_library_costs_recall():
    """反面口径：交付集为空时召回必须是 0 而不是 1。

    **没有这条，上面那条「召回 == 1.0」对一个恒返回 1.0 的实现也成立。**
    """
    client = FakeSentrixClient(
        asset_polls=_polls(_assets()),
        turns={"turn_1": [_completed_turn(attachments=[])]},
        import_result={"imported": 2, "events": 1, "summarized": 1, "failures": []},
    )
    execution, _client, _writer, _lifecycle, _albums = _execution(client=client)
    state = execution.execute(_request())
    item = state["items"][0]
    assert item["selected_asset_ids"] == []
    assert item["image_retrieval_recall"] == 0.0
    assert item["image_retrieval_counts"] == {
        "precision": 0.0,
        "recall": 0.0,
        "f1": 0.0,
        "matched": 0,
        "predicted": 0,
        "gt": 1,
    }


def test_runtime_turns_carry_the_gt_expected_action():
    """回合记录必须带上 GT 的 ``expected_action``，否则**一次判分都不会发生**。

    这条用例的来历是一次真机验收：主链 29 秒跑完、八个相位全 done、遥测样本 38 行，
    但每个 item 的 judge 都是 ``{"score": None, "reason": "not_labeled"}``——
    ``JudgeRunner.judge_item`` 从**回合**上读 expected_action（1.0 的 runtime_turns
    带这个字段），而 2.0 的 ``_evaluate_one`` 没把它放进去。**失败的样子是「一切正常、
    只是没有分数」**，所以只能靠把字段本身钉住。多轮题按题面每一轮取，单轮题退回行级。
    """
    rows = [dict(_rows()[0], expected_action="refuse")]
    execution, _client, _writer, _lifecycle, _albums = _execution(
        albums=FakeAlbums(
            manifest=_manifest(),
            qa_rows=rows,
            files={"faces/1.jpg": b"J"},
            directory_entries={"photos": 2},
        )
    )
    state = execution.execute(_request())
    turn = state["items"][0]["runtime_turns"][0]
    assert turn["expected_action"] == "refuse"
    # 「回合上有」才算接线：只断言 item 上有，对一个没往回合里放的实现同样成立。
    assert state["items"][0].get("expected_action") == "refuse"


def test_the_trace_is_persisted_verbatim_and_digested():
    """轨迹是 2.0 的 ``public_progress`` 逐条原样，外加**两条投影**与一份计数摘要。

    **这条用例的方向在 2026-10-10 反过来了。** 它此前断言 ``"tool_trace" not in item``，
    理由是「2.0 的 payload 里只有工具名，编一个 ``tool_trace`` 会让读侧以为拿得到结果」。
    那个理由当时是真的——但它把**缺口**钉成了契约：前端五处读 ``tool_trace`` 的地方
    （Agent Loop 树、工具耗时、工具性能卡、记忆有效性条数、链路图）因此永远显示「未记录」。

    现在助手侧的 payload 带上了 ``step_id`` / ``parent_step_id`` / ``arguments`` /
    ``duration_ms`` / ``result``，本层从**同一条事件流**投影出 1.0 那两条轨迹。
    这条用例改成钉那个投影：一次工具调用在流里是**两条**，轨迹里必须合**一条**。
    """
    execution, _client, _writer, _lifecycle, _albums = _execution()
    state = execution.execute(_request())
    item = state["items"][0]
    turn = item["runtime_turns"][0]
    assert [event["kind"] for event in turn["trace"]] == [
        "plan",
        "model_action",
        "tool_result",
        "terminal",
    ]
    assert turn["trace"][1]["payload"] == {"tool": "search_memories", "tool_call_id": "c1"}
    assert turn["trace_summary"] == {
        "steps": 4,
        "stages": {"planning": 1, "action": 1, "tool_result": 1, "terminal": 1},
        "kinds": {"plan": 1, "model_action": 1, "tool_result": 1, "terminal": 1},
        "tools": {"search_memories": 2},
        "executed_tools": {"search_memories": 1},
        "error_codes": {},
    }
    # **两条事件合成一条轨迹行。** 分开记会让读侧把一次调用数成两次
    # （``MemoryEffectivenessTable`` 的「工具轨迹 N 条」就会翻倍）。
    assert len(item["tool_trace"]) == 1
    row = item["tool_trace"][0]
    assert row["tool"] == "search_memories"
    assert row["conversation_turn"] == 0
    # 状态与文本取**收尾**那条：发起那条的 ``running`` 不是这次调用的结论。
    assert row["status"] == "ok"
    assert row["text"] == "工具执行完成。"
    # 执行步骤脊柱：事件流逐条一个，供 ``bind_tool_calls_to_model_rounds`` 对齐轮次。
    # 事件流逐条一条，**外加每次模型调用一条**（``stage="model"``，供
    # ``analysis.agent_stability`` 计数）。后者没有 ``text``——它不是用户看到的事件。
    assert [step.get("text") for step in item["execution_trace"]] == [
        "已确认需要哪些证据。",
        "正在执行 search_memories…",
        "工具执行完成。",
        "完成。",
        None,
    ]
    assert item["execution_trace"][1]["conversation_turn"] == 0


def test_the_turn_facts_are_projected_into_the_three_panels():
    """助手报的**一份事实**，摆成三块面板各自的形状。

    这三处此前在 8772 上分别是：``AgentLoopTree`` 整棵树只有一个空文案、
    ``Agent2SummaryPanel`` 整块不出现、``DebugTracePanel`` 整块不出现——
    而读侧的代码早就写好了，缺的一直是写侧。
    """
    execution, _client, _writer, _lifecycle, _albums = _execution()
    item = execution.execute(_request())["items"][0]

    # ① Agent Loop 调用树：每次模型调用一条，摊平到整题。
    calls = item["model_call_metrics"]
    assert len(calls) == 1
    assert calls[0]["prompt_tokens"] == 700
    assert calls[0]["conversation_turn"] == 0

    # ② Agent 2.0 面板：``getAgent2Trace`` 要这三样，缺一样整块就不渲染。
    agent2 = item["runtime_turns"][0]["agent2_trace"]
    assert agent2["task_declaration"]["goal"] == "看海边的照片"
    assert agent2["task_state"]["requirements"]
    assert agent2["evidence_ledger"] == {"entries": []}
    # 需求状态计数与「没凑齐」的条数：后者用 ``unmet_reason`` 判——
    # 它把「取了没取到」与「根本没去取」分开，正是这个面板要看的东西。
    assert agent2["requirement_status_counts"] == {"satisfied": 1, "open": 1}
    assert agent2["evidence_coverage"] == {"entries": 2, "partial_entries": 1}

    # ③ 调试详情：逐步轨迹，带工具入参与产出规模。
    steps = item["runtime_turns"][0]["debug_trace"]
    assert [step["type"] for step in steps] == [
        "plan",
        "model_action",
        "tool_result",
        "terminal",
    ]

    # ⑤ 完整模型轨迹原样落进 ``trace_bundle``（8772 的两块面板都从这里取）。
    debug_calls = item["runtime_turns"][0]["trace_bundle"]["debug_calls"]
    assert debug_calls[0]["call_type"] == "agent"
    assert debug_calls[0]["messages"][-1]["content"] == "看海边的照片"
    assert debug_calls[0]["response"]["tool_calls"][0]["name"] == "search_memories"

    # ④ 稳定性卡只认 ``execution_trace`` 里 ``stage == "model"`` 的步骤——模型调用
    #    的事实本来就在手上，不补成那种步骤，那张卡恒显示「未记录」。
    model_steps = [s for s in item["execution_trace"] if s.get("stage") == "model"]
    assert len(model_steps) == 1
    assert model_steps[0]["call_type"] == "agent"
    assert model_steps[0]["action"] == "search_memories"


def test_a_multi_turn_question_reuses_one_conversation():
    """多轮题的每一轮都进同一个会话（1.0 ``:3406`` 每题一个 conversation_id）。

    错误实现（每题每轮各建一个会话）会让第二轮的追问失去上下文，**而在指标上完全
    看不出来**——所以只能靠调用形状钉。
    """
    row = _rows()[0]
    row["conversation"] = [{"message": "第一轮"}, {"message": "第二轮"}]
    albums = FakeAlbums(
        manifest=_manifest(),
        qa_rows=[row],
        files={"faces/1.jpg": b"J"},
        directory_entries={"photos": 2},
    )
    client = FakeSentrixClient(
        asset_polls=_polls(_assets()),
        turns={"turn_1": [_completed_turn("turn_1")], "turn_2": [_completed_turn("turn_2")]},
        import_result={"imported": 2, "events": 1, "summarized": 1, "failures": []},
    )
    execution, _client, _writer, _lifecycle, _albums = _execution(client=client, albums=albums)
    state = execution.execute(_request())
    conversations = [kwargs for name, kwargs in client.calls if name == "start_conversation"]
    turns = [kwargs for name, kwargs in client.calls if name == "start_turn"]
    assert len(conversations) == 1
    assert [turn["conversation_id"] for turn in turns] == ["conv_1", "conv_1"]
    assert [turn["message"] for turn in turns] == ["第一轮", "第二轮"]
    # 评测每轮都显式要求完整模型轨迹（8772 的「模型看到什么」数据源）。
    assert all(turn["include_debug"] is True for turn in turns)
    item = state["items"][0]
    assert item["question"] == "第二轮"
    assert item["conversation_context_mode"] == "shared_conversation_id"
    assert item["conversation_turn_count"] == 2
    assert item["wall_clock_ms"] is not None


def test_a_turn_that_never_reaches_a_terminal_state_fails_its_item():
    """非终态一直轮询下去必须收在一个可判定的失败上，而不是把答案写成空串。"""
    client = FakeSentrixClient(
        asset_polls=_polls(_assets()),
        turns={"turn_1": [{"turn_id": "turn_1", "status": "running", "public_progress": []}]},
        import_result={"imported": 2, "events": 1, "summarized": 1, "failures": []},
    )
    execution, _client, _writer, _lifecycle, _albums = _execution(client=client)
    state = execution.execute(_request(turn_timeout=0))
    item = state["items"][0]
    assert item["execution_status"] == "timeout"
    assert "did not complete" in item["error"]
    assert state["run_valid"] is False


def test_a_failed_item_is_skipped_for_the_judge_and_a_good_one_is_pending():
    """「没有答案可打分」与「还没打分」是两件事（1.0 ``:3220``）。

    本切片没有 judge，但这条区分必须现在就写对：S9b-3 接上 judge 时，
    ``pending`` 是「去打分」的队列，``skipped`` 是「别碰」。
    """
    client = FakeSentrixClient(
        asset_polls=_polls(_assets()),
        turns={"turn_1": [{"turn_id": "turn_1", "status": "running", "public_progress": []}]},
        import_result={"imported": 2, "events": 1, "summarized": 1, "failures": []},
    )
    execution, _client, _writer, _lifecycle, _albums = _execution(client=client)
    state = execution.execute(_request(turn_timeout=0))
    assert state["items"][0]["judge_status"] == "skipped"

    execution, _client, _writer, _lifecycle, _albums = _execution()
    state = execution.execute(_request())
    assert state["items"][0]["judge_status"] == "pending"
    assert state["items"][0]["judge"] == {"score": None, "reason": "pending_judge"}


def test_run_valid_requires_every_question_to_complete():
    execution, _client, _writer, _lifecycle, _albums = _execution()
    state = execution.execute(_request())
    assert state["run_valid"] is True
    assert state["qa_count"] == 1


def test_a_failing_phase_stops_the_chain_and_records_where_it_died():
    """1.0 ``:2404``：任何相位异常都收成 ``failed`` + ``failed_phase``。

    导入炸了就不该再往下跑问答——在空相册上问 100 道题只会产出一份全错的 run.json。
    """
    client = FakeSentrixClient(
        asset_polls=_polls(_assets()),
        turns={"turn_1": [_completed_turn()]},
    )

    def boom(**kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("HTTP 500: 导入服务炸了")

    client.import_album = boom  # type: ignore[method-assign]
    execution, _client, _writer, _lifecycle, _albums = _execution(client=client)
    state = execution.execute(_request())
    assert state["status"] == "failed"
    assert state["failed_phase"] == "photo_import"
    assert "导入服务炸了" in state["fatal_error"]
    assert state["phases"]["photo_import"]["status"] == "failed"
    assert "qa_eval" not in state["phases"]
    assert state["finished_at"] is not None


# ---- 模型起停（LifecycleProvider.start / .stop 的接线证据） -------------------


def test_without_a_manager_the_model_is_never_started_or_stopped():
    """管理器没配时**一次都不能调** start/stop——那是 1.0 的 ``use_cloud_model`` 分支。

    ``lifecycle.state()`` 仍会被调（``_hardware_snapshot`` 用它记 run 元数据），
    所以这条断言的是「start/stop 不在调用表里」，不是「lifecycle 没被碰过」。
    """
    execution, _client, _writer, lifecycle, _albums = _execution()
    state = execution.execute(_request())
    assert "start" not in lifecycle.call_names()
    assert "stop" not in lifecycle.call_names()
    assert state["phases"]["model_deploy"]["manager_status"] == "not_applicable"


def test_with_a_manager_the_model_is_stopped_then_started_then_probed():
    """**这是 ``LifecycleProvider.start`` / ``.stop`` 的唯一生产调用者。**

    三件事一起钉：先停后起（顺序反了会在显存没释放时装载）、``wait_ready=False``
    （1.0 ``:2567``：fire-and-poll，否则一个取消请求要等完整次装载）、以及起完之后
    真的去探了端点（``chat``）与测了一次吞吐（``chat_stream``）。
    """
    execution, _client, _writer, lifecycle, _albums = _execution()
    execution.execute(_request(manager_url="http://manager:8500", model_base_url="http://m:8100"))
    names = lifecycle.call_names()
    # 顺序：先 stop 再 start。反过来的实现在显存没释放时装载，而那正是 1.0 ``:2544``
    # 先卸载的理由。start/stop 各恰好一次——重试是给 start 失败准备的，不是常态。
    assert names.index("stop") < names.index("start")
    assert names.count("stop") == 1
    assert names.count("start") == 1
    stopped = next(kwargs for name, kwargs in lifecycle.calls if name == "stop")
    started = next(kwargs for name, kwargs in lifecycle.calls if name == "start")
    assert stopped == {"timeout": 60}
    assert started == {"profile": "qwen", "wait_ready": False}
    # 两次 ``chat`` 都是 1.0 的流程：等待就绪时探一次（``:2484``）、装载完健康检查
    # 再探一次（``:2593``）；``chat_stream`` 是随后的吞吐探测（``model_probe``）。
    # **不是一次多余的调用**——把它们合成一次的实现会让「端点到底通没通」少一个证据。
    # 末尾两次都来自 ``qa_eval``，**顺序有意义**：先探端点健不健康（1.0 ``:3394``），
    # 不健康就根本不该开始答题；再问它同时能服务几个请求（1.0 ``:3667``）。
    assert execution.inference.calls == [
        "chat",
        "chat",
        "list_models",
        "chat_stream",
        "health",
        "max_concurrency",
    ]


def test_model_deploy_points_sentrix_at_the_requested_endpoint():
    """1.0 第 4 步 ``bind-runtime`` 的 2.0 落地（T923）：**外部端点那条分支也要做**。

    153 上跑的正是这一支（llama-server，没有 Manager），而 2.0 的助手用哪个模型
    只由它自己的 ``vlm_base_url`` 决定——所以这一支漏了重启，整轮评测的答案都来自
    另一个模型。断言三件：真的问了当前端点、端点相同就不重启、``gamma_sync`` 落盘。

    **区分力**：把这一步退回 1.0 那句占位（``ok=False``）的实现在这里红——
    ``endpoint`` / ``restarted`` 两个键根本不存在。
    """
    process = FakeSentrixProcess(current="http://127.0.0.1:8100/v1")
    execution, _client, _writer, _lifecycle, _albums = _execution(sentrix_switch=SentrixModelSwitch(process))
    state = execution.execute(_request(model_base_url="http://127.0.0.1:8100/v1"))
    gamma = state["phases"]["model_deploy"]["gamma_sync"]
    assert process.endpoint_reads == 1
    assert gamma["endpoint"] == "http://127.0.0.1:8100/v1"
    assert gamma["restarted"] is False
    assert gamma["ok"] is True
    assert process.restarts == []


def test_the_managed_branch_also_points_sentrix_at_the_new_model():
    """有 Manager 那一支同样要换端点（1.0 的 bind-runtime 原本就只在这一支里）。"""
    process = FakeSentrixProcess(current="http://old:8100/v1")
    execution, _client, _writer, _lifecycle, _albums = _execution(sentrix_switch=SentrixModelSwitch(process))
    state = execution.execute(
        _request(manager_url="http://manager:8500", model_base_url="http://127.0.0.1:8100/v1")
    )
    phase = state["phases"]["model_deploy"]
    assert phase["status"] == "done"
    assert phase["gamma_sync"]["restarted"] is True
    assert process.restarts == ["http://127.0.0.1:8100/v1"]


def test_a_restart_that_fails_fails_the_model_deploy_phase():
    """重启失败 = 这一轮一定会答错模型，**必须红在相位上**（与 1.0 不同的收口）。

    1.0 的 bind-runtime 只是同步一个缓存，失败不致命（``:2613`` 吞掉）。2.0 不是：
    2.0 自己用那个端点答题，失败意味着答案来自另一个模型——继续跑只会产出
    「标着 A 模型、其实是 B 模型」的结果。**区分力**：忽略 ``outcome.ok`` 的实现
    在这里会让 run 收成 ``completed``。
    """
    process = FakeSentrixProcess(current="", restart_ok=False, restart_error="port 8191 busy")
    execution, _client, _writer, _lifecycle, _albums = _execution(sentrix_switch=SentrixModelSwitch(process))
    state = execution.execute(_request(model_base_url="http://127.0.0.1:8100/v1"))
    assert process.restarts == ["http://127.0.0.1:8100/v1"]
    assert state["status"] == "failed"
    assert state["failed_phase"] == "model_deploy"


def test_without_a_controller_the_step_is_recorded_as_not_wired():
    """没装配控制器时如实记「未装配」——**不假装同步过**（S9b-2 的占位就是这么处理的）。"""
    execution, _client, _writer, _lifecycle, _albums = _execution()
    state = execution.execute(_request(model_base_url="http://127.0.0.1:8100/v1"))
    gamma = state["phases"]["model_deploy"]["gamma_sync"]
    assert gamma["ok"] is False
    assert "not wired" in gamma["error"]


def test_a_transient_start_failure_is_retried_before_giving_up():
    """1.0 ``:2560`` 的退避重试：第一次 502 是常态（显存还没释放完）。"""
    lifecycle = FakeLifecycle(start_failures=1)
    execution, _client, _writer, _lifecycle, _albums = _execution(lifecycle=lifecycle)
    state = execution.execute(_request(manager_url="http://manager:8500"))
    assert lifecycle.call_names().count("start") == 2
    assert state["phases"]["model_deploy"]["status"] == "done"


def test_a_model_that_never_becomes_ready_fails_the_phase():
    """反面：起不来就必须红在这一相位，而不是带着一个没装载的模型继续跑完整轮。"""
    lifecycle = FakeLifecycle(state={"profile": "other", "served_model_name": "other"})
    execution, _client, _writer, _lifecycle, _albums = _execution(lifecycle=lifecycle)
    import photobench.application.run_execution.service as service

    original_backoff = service.MODEL_START_BACKOFF_SECONDS
    original_ready = service.MODEL_READY_TIMEOUT_SECONDS
    # 就绪等待是**真实等待**（``time.monotonic`` 不走假表），跑满 600 秒要十分钟。
    # 这是它作为模块常量存在的唯一理由。
    service.MODEL_START_BACKOFF_SECONDS = (0,)
    service.MODEL_READY_TIMEOUT_SECONDS = 0
    try:
        state = execution.execute(_request(manager_url="http://manager:8500"))
    finally:
        service.MODEL_START_BACKOFF_SECONDS = original_backoff
        service.MODEL_READY_TIMEOUT_SECONDS = original_ready
    assert state["status"] == "failed"
    assert state["failed_phase"] == "model_deploy"
    assert "not ready" in state["fatal_error"]


# ---- GPU 采样 --------------------------------------------------------------


def test_sampling_writes_labelled_samples_and_is_stopped_at_the_end():
    """采样器是 ``TelemetryProvider`` 三个方法的唯一调用者。

    ``on_sample`` 是本片唯一入口，所以这里用一个**同步假件**（真实采样线程要跑 0.5s
    才出第一个点，用例等不起）：把 ``GpuSampler.start`` 换成「立刻采一次」。
    """
    telemetry = FakeTelemetry(
        gpus=[{"index": 0, "temperature_c": 60.0}],
        process_memory={"process_memory_used_mib": 1234, "processes": [{"pid": 1}]},
        system_memory={"system_memory_used_mib": 4096},
    )
    execution, _client, writer, _lifecycle, _albums = _execution(telemetry=telemetry)
    import photobench.application.run_execution.gpu_sampler as sampler_module

    original = sampler_module.GpuSampler.start

    def start_and_sample(self: Any) -> None:
        self._thread = None
        self.sample_once()

    sampler_module.GpuSampler.start = start_and_sample  # type: ignore[method-assign]
    try:
        result = execution.execute(_request())
    finally:
        sampler_module.GpuSampler.start = original  # type: ignore[method-assign]
    assert writer.resets == ["run_test"]
    assert len(writer.gpu_samples) == 4
    assert {s["phase"] for _, s in writer.gpu_samples} >= {"photo_import", "pipeline_processing", "qa_eval"}
    assert result["phases"]["gpu_metrics"]["samples_count"] == len(writer.gpu_samples)
    _run_id, sample = writer.gpu_samples[0]
    assert sample["temperature_c"] == 60.0
    assert sample["model_process_memory_used_mib"] == 1234
    assert sample["system_memory_used_mib"] == 4096
    # 这个假件的采样发生在相位循环开始之前（真的采样器跑在线程里），所以走的是
    # 「还没有当前相位」那条兜底——相位标签本身在下面那条用例里单独钉。
    assert sample["phase"] == "unassigned"
    assert sample["phase_label"] == "未分配阶段"
    assert telemetry.calls.count("gpu_stats") >= 2  # 采样一次 + 起止硬件快照


def test_real_execution_constructor_publishes_metrics_before_qa_finishes(monkeypatch):
    """A late-bound callback must reach the sampler built by execute(), not an empty holder."""
    import photobench.application.run_execution.service as service

    execution, _client, _writer, _lifecycle, _albums = _execution()
    entered, published, release = threading.Event(), threading.Event(), threading.Event()
    original = execution._publish_partial_gpu_metrics

    def publish(state, metrics):
        original(state, metrics)
        if entered.is_set() and (state.get("phases") or {}).get("gpu_metrics", {}).get("status") == "running":
            published.set()

    def qa(state, request):
        entered.set()
        assert release.wait(3)
        state.phase_done("qa_eval", {})

    monkeypatch.setattr(service, "GPU_METRICS_PUBLISH_SECONDS", 0.01)
    monkeypatch.setattr(execution, "_publish_partial_gpu_metrics", publish)
    monkeypatch.setattr(execution, "_phase_qa_eval", qa)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(execution.execute, _request())
        try:
            assert entered.wait(3)
            assert published.wait(3), "metrics must be published while the run is active"
            assert not future.done()
        finally:
            release.set()
        result = future.result(timeout=3)
    assert result["phases"]["gpu_metrics"]["status"] == "done"
    assert result["phases"]["gpu_metrics"]["sampling_integrity"]["stop_pending_threads"] == []
    assert result["telemetry_live"]["status"] == "stopped"


@pytest.mark.parametrize("status", ["done", "skipped"])
def test_partial_publication_does_not_replace_final_metrics(status):
    from photobench.application.run_execution.state import RunState

    execution, *_ = _execution()
    state = RunState({"phases": {"gpu_metrics": {"status": status, "samples_count": 7}}}, run_id="run_test")
    execution._publish_partial_gpu_metrics(state, {"samples_count": 2})
    assert state.get("phases")["gpu_metrics"] == {"status": status, "samples_count": 7}


def test_cancelled_run_keeps_partial_telemetry_and_persistence_failure(monkeypatch):
    execution, _client, writer, *_ = _execution()

    def fail_write(*args):
        raise OSError("disk full")

    def cancel(state, request):
        raise RunCancelledError("user requested stop")

    monkeypatch.setattr(writer, "append_gpu_sample", fail_write)
    monkeypatch.setattr(execution, "_phase_qa_eval", cancel)
    result = execution.execute(_request())
    assert result["status"] == "cancelled"
    metrics = result["phases"]["gpu_metrics"]
    assert metrics["partial"] is True
    assert metrics["status"] == "partial"
    assert metrics["collection_end_reason"] == "cancelled"
    assert metrics["sampling_integrity"]["callback_failures"] > 0
    assert "disk full" in metrics["sampling_integrity"]["last_callback_failure"]


def test_a_sample_carries_the_label_of_the_phase_that_was_running():
    """相位标签是采样行的一部分（1.0 ``:2113``），读侧靠它把曲线切成段。

    直接调 ``_sample_writer`` 是因为标签取自 ``state["current_phase"]``，
    而起止硬件快照与相位循环的时序关系让「在某相位中间采样」这件事只有这里能摆出来。
    """
    from photobench.application.run_execution.state import RunState

    execution, _client, writer, _lifecycle, _albums = _execution()
    state = RunState({"current_phase": "qa_eval"}, run_id="run_test")
    execution._sample_writer(state)({"index": 0, "temperature_c": 50.0})
    assert writer.gpu_samples == [
        ("run_test", {"index": 0, "temperature_c": 50.0, "phase": "qa_eval", "phase_label": "QA 测评"})
    ]


def test_sampling_can_be_disabled_for_an_environment_without_telemetry(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PHOTOBENCH_DISABLE_GPU_SAMPLING", "1")
    execution, _client, writer, _lifecycle, _albums = _execution()
    execution.execute(_request())
    assert writer.resets == []
    assert writer.gpu_samples == []


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
def test_terminal_status_is_not_published_until_final_hardware_observation_finishes(
    monkeypatch: pytest.MonkeyPatch, outcome: str
):
    """读侧看到终态时，采样/末尾观测和scope收尾必须已经完成。"""
    execution, _client, writer, _lifecycle, _albums = _execution()
    entered, release = threading.Event(), threading.Event()
    snapshot = execution._hardware_snapshot
    calls = 0

    def blocked_final_snapshot():
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
            assert release.wait(5), "test must release final observation"
        return snapshot()

    monkeypatch.setattr(execution, "_hardware_snapshot", blocked_final_snapshot)
    if outcome != "completed":

        def fail_phase(*_args):
            if outcome == "cancelled":
                raise RunCancelledError("cancelled during aggregate")
            raise RuntimeError("aggregate failed")

        monkeypatch.setattr(execution, "_phase_aggregate", fail_phase)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(execution.execute, _request())
        try:
            assert entered.wait(5)
            state = execution.state
            assert state is not None
            state.persist(wait=True)
            assert state.get("status") in {"running", "cancelling"}
            assert state.get("finished_at") is None
            assert writer.last_state()["status"] in {"running", "cancelling"}
        finally:
            release.set()
        result = future.result(timeout=5)
    assert result["status"] == outcome
    assert result["finished_at"]
    assert result["current_phase"] is None
    assert result["hardware_snapshots"]["end"] is not None
    assert writer.last_state() == result


def test_hardware_snapshots_are_written_at_both_ends():
    """``hardware_snapshots`` 是 1.0 run.json 的形状（``:2345`` / ``:2455``）。

    遥测探不到时必须是 ``status`` 而不是 0——0 是一个合法的测量结果，缺失不是
    （``domain/runtime/ports.py`` 的口径）。
    """
    telemetry = FakeTelemetry(gpu_status="unavailable")
    execution, _client, _writer, _lifecycle, _albums = _execution(telemetry=telemetry)
    state = execution.execute(_request())
    snapshots = state["hardware_snapshots"]
    assert snapshots["start"]["captured_at"]
    assert snapshots["end"]["captured_at"]
    assert snapshots["end"]["gpu"] == []
    assert snapshots["end"]["gpu_status"]["status"] == "unavailable"


# ---- QA 并发：跟随端点自报的槽位（1.0 ``benchmark_orchestrator.py:3632-3690``）------


def test_qa_concurrency_follows_the_endpoint_slots():
    """并发跟随端点自报的槽位，而不是恒定 1。

    1.0 ``:3667-3688``：llama.cpp 问 ``/props`` 的 ``total_slots``、托管 vLLM 问 manager
    ``/state`` 的 ``max_num_seqs``、ninfer 问 ``/stats``。恒定 1 的代价是可测的：
    2026-10-10 那轮 qa_eval 用了 6570s，而 1.0 同题集同模型是 2876s。
    """
    execution, *_ = _execution(inference=FakeInference(slots=4))

    state = execution.execute(_request())

    assert state["qa_concurrency"] == 4
    assert state["qa_concurrency_requested"] == 4
    assert state["qa_concurrency_runtime_limit"] == 4
    # 这一轮的夹具没配 judge，所以 judge 池不建（``_phase_qa_eval`` 的既有口径：
    # ``judge_concurrency if judge is not None else 0``）。judge 池自己的上限另测。
    assert state["judge_concurrency"] == 0


def test_qa_eval_refuses_to_start_when_the_endpoint_is_unhealthy():
    """入 QA 前先探一次端点；不健康就抛（1.0 ``:3394-3397``）。

    487 道题跑在一个不回话的端点上，产出的是一份**看起来正常**的废数据——比直接失败
    更糟，因为它会被当成一轮有效结果。2026-10-10 那轮从 18:13 起端点连续 13 题
    503 / Connection refused，而整轮照常往下跑，只在每题上留一句「模型服务当前不可用」。

    ``execute`` 把相位异常收成 ``status=failed``（不往外抛，1.0 ``:2404``），
    所以这里钉的是终态与 ``failed_phase``。
    """
    execution, *_ = _execution(inference=FakeInference(health_status="error"))

    state = execution.execute(_request())

    assert state["status"] == "failed"
    assert state["failed_phase"] == "qa_eval"
    assert "unhealthy before QA evaluation" in state["fatal_error"]


def test_a_degraded_endpoint_still_starts_the_run():
    """``degraded`` 算健康——它表示端点活着但自报状态不全。

    与 ``infrastructure/deploy/sentrix_process.py`` 的启动判据同一条口径
    （「``ok`` / ``degraded`` 都算起来了」）。要拦的是不可达与显式 error。
    """
    execution, *_ = _execution(inference=FakeInference(health_status="degraded"))

    state = execution.execute(_request())

    assert state["status"] == "completed"


def test_judge_concurrency_is_capped_at_eight():
    """judge 是**独立的池**，上限 8，不随 QA 并发无限放大（1.0 ``:3833-3841``）。

    两个池分开的理由：judge 打的是另一个端点（云 API 或本机另一个模型），把它塞进
    Agent 的并发预算会让「评测吞吐」与「打分吞吐」互相拖住。
    """
    assert RunExecution._resolve_judge_concurrency(4) == 4
    assert RunExecution._resolve_judge_concurrency(32) == 8
    assert RunExecution._resolve_judge_concurrency(0) == 1


def test_qa_concurrency_falls_back_to_one_when_the_endpoint_says_nothing():
    """问不到就退回 1 —— 那是 1.0 ``:3662`` 的**最后一级**，不是 2.0 的默认值。"""
    execution, *_ = _execution(inference=FakeInference(slots=None))

    state = execution.execute(_request())

    assert state["qa_concurrency"] == 1
    assert state["qa_concurrency_runtime_limit"] is None


def test_qa_concurrency_never_exceeds_the_endpoint_limit(monkeypatch):
    """操作员覆盖值被端点实际容量夹住（1.0 ``:3665`` 的 ``min(requested, runtime_limit)``）。"""
    monkeypatch.setenv("PHOTOBENCH_QA_CONCURRENCY", "8")
    execution, *_ = _execution(inference=FakeInference(slots=2))

    state = execution.execute(_request())

    assert state["qa_concurrency"] == 2
    assert state["qa_concurrency_requested"] == 8
    assert state["qa_concurrency_runtime_limit"] == 2


# ---- reuse：复用已存在的 scope（2026-10-15 恢复）----------------------------


def test_reuse_skips_the_import_phases_and_keeps_the_existing_scope():
    """reuse 只跑绑定与问答收尾——导入那三个相位的产物上一次已经落在那个 scope 里了。

    1.0 的 reuse 复用已存在的 scope（读侧 ``_build_reuse_bases``），相位是
    ``scope_attach / qa_eval / aggregate``。2.0 自 S9b-2 起只接 full，
    ``RUN_MODES`` 直接拒绝 reuse——而**66 分钟的导入**正是复用要省掉的那件事。
    """
    execution, *_ = _execution()

    state = execution.execute(_request(mode="reuse", scope_id="PhotoBench-20260101-000000-album-qwen"))

    assert state["mode"] == "reuse"
    assert state["scope_source"] == "reused"
    assert state["scope_id"] == "PhotoBench-20260101-000000-album-qwen"
    # 1.0 的形状：``existing_scope_id`` 是调用方要求复用的那个，与实际的 ``scope_id`` 分开记。
    assert state["existing_scope_id"] == "PhotoBench-20260101-000000-album-qwen"
    # 导入三个相位**一个都不该出现**；绑定与收尾四个都在。
    for skipped in ("identity_seed", "photo_import", "pipeline_processing"):
        assert skipped not in state["phases"], skipped
    for run_phase in ("scope_setup", "qa_eval", "gpu_metrics", "aggregate"):
        assert run_phase in state["phases"], run_phase


def test_a_full_run_still_records_mode_and_scope_source():
    """full 那一侧不能被 reuse 的改动带偏——它必须仍然建新 scope。"""
    execution, *_ = _execution()

    state = execution.execute(_request())

    assert state["mode"] == "full"
    assert state["scope_source"] == "created_by_import"
    assert state["existing_scope_id"] is None
    assert "photo_import" in state["phases"]


def test_reuse_synchronizes_product_without_touching_model_lifecycle():
    process = FakeSentrixProcess(current="http://127.0.0.1:8100/v1")
    execution, _client, _writer, lifecycle, _albums = _execution(sentrix_switch=SentrixModelSwitch(process))
    state = execution.execute(
        _request(
            mode="reuse",
            scope_id="existing",
            model_base_url="http://127.0.0.1:8106/v1",
            manager_url="http://manager",
        )
    )
    assert process.restarts == ["http://127.0.0.1:8106/v1"]
    assert state["phases"]["model_deploy"]["gamma_sync"]["ok"] is True
    assert not any(name in {"start", "stop"} for name, _ in lifecycle.calls)


def test_build_does_not_read_qa_or_construct_judge(monkeypatch):
    execution, client, *_ = _execution()
    monkeypatch.setattr(execution.albums, "qa_rows", lambda *_: pytest.fail("build read QA"))
    monkeypatch.setattr(execution, "_build_judge", lambda *_: pytest.fail("build created Judge"))
    state = execution.execute(_request(mode="build", qa_set=""))
    assert state["status"] == "completed"
    assert state["qa_count"] == 0 and state["items"] == []
    assert "qa_eval" not in state["phases"]
    assert state["run_valid"] is None
    assert state["summary"]["run_valid"] is None
    assert not any(call[0] == "start_turn" for call in client.calls)


@pytest.mark.parametrize(
    "mismatch", ["vlm_base_url", "legacy_clip_url", "agent_wall_time_s", "build_revision"]
)
def test_frozen_configuration_is_verified_before_import_or_qa(mismatch):
    import json

    from photobench.domain.runtime.ports import RunRuntimeSnapshot

    execution, client, *_ = _execution()
    connection = {
        "model_base_url": "http://local:8100/v1",
        "sentrix_url": "http://local:8291",
        "embedding": {"mode": "split_text_image", "image_url": "http://local:8103"},
        "product_settings": {"agent_wall_time_s": 600},
        "required_build_revision": "fixed",
    }
    effective = {
        "vlm_base_url": "http://local:8100/v1",
        "embedding_scheme": "legacy",
        "legacy_clip_url": "http://local:8103",
        "agent_wall_time_s": 600,
    }
    observed = {"runtime_configuration": effective, "build_revision": "fixed"}
    if mismatch == "build_revision":
        observed[mismatch] = "old"
    else:
        effective[mismatch] = "wrong"
    execution.runtime_snapshot = RunRuntimeSnapshot(json.dumps(connection), "fixed")
    execution.client.runtime_diagnostics = observed
    execution.sentrix_switch = SentrixModelSwitch(FakeSentrixProcess(current="http://local:8100/v1"))
    state = execution.execute(_request(model_base_url="http://local:8100/v1"))
    assert state["status"] == "failed"
    assert state["failed_phase"] == "model_deploy"
    assert mismatch in state["fatal_error"]
    assert "import_album" not in execution.client.call_names()


def test_cancel_before_execute_is_retained_and_makes_no_product_calls():
    execution, client, *_ = _execution()
    execution.cancel(source="queued_cancel")
    state = execution.execute(_request())
    assert state["status"] == "cancelled"
    assert not client.calls


@pytest.mark.parametrize("failure", ["manifest", "qa_rows", "integrity"])
def test_initialization_failure_persists_a_terminal_run(failure, monkeypatch):
    execution, client, writer, _, albums = _execution()

    def broken(*args, **kwargs):
        raise ValueError(f"invalid input: {failure}")

    monkeypatch.setattr(albums, failure, broken)
    state = execution.execute(_request(run_id="bad-input"))
    assert state["run_id"] == "bad-input"
    assert state["status"] == "failed"
    assert state["failed_phase"] == "initialization"
    assert state["fatal_error"] == f"invalid input: {failure}"
    assert state["finished_at"]
    assert client.calls == []
    assert writer.snapshots[-1][1]["status"] == "failed"


def test_late_sample_callback_does_not_reopen_or_append_to_a_finished_run():
    from photobench.application.run_execution.state import RunState

    execution, _client, writer, *_ = _execution()
    state = RunState({"finished_at": "2026-10-22T00:00:00", "status": "cancelled"}, run_id="run_test")
    execution._sample_writer(state)({"temperature_c": 50})
    assert writer.gpu_samples == []
    assert "telemetry_live" not in state.data


def test_import_final_scan_count_uses_product_outcomes_not_manifest_expectation():
    albums = FakeAlbums(
        manifest=_manifest(),
        qa_rows=_rows(),
        files={"faces/1.jpg": b"J"},
        directory_entries={"photos": 5},
    )
    client = FakeSentrixClient(
        asset_polls=_polls(_assets()),
        import_result={
            "imported": 1,
            "deduplicated": 1,
            "events": 1,
            "summarized": 1,
            "failures": [{"file_name": "extra.png", "reason": "bad encoding"}],
        },
    )
    execution, *_ = _execution(albums=albums, client=client)
    phase = execution.execute(_request(mode="build"))["phases"]["photo_import"]
    assert phase["total_media"] == 2  # immutable manifest expectation remains traceable
    assert phase["scanned_count"] == 3  # five entries, three supported media actually settled
    assert (phase["accepted_count"], phase["deduplicated_count"], phase["failed_count"]) == (1, 1, 1)
    assert phase["directories"][0]["progress"]["request_status"] == "returned"
