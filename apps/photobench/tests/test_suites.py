"""写侧用例：建 run / 取消 / 删除 / 评审 / 重判 / 内存画像。

对应 1.0 ``OrchestratorRepository`` 的 ``start_suite``（``:7306``）/ ``cancel_run``
（``:6879``）/ ``delete_run``（``:7010``）/ ``save_reviews``（``:4988``）/
``start_rejudge``（``:7061``）/ ``start_memory_profile``（``:5159``）。

**这一层的假件比主链那两份更"薄"**：``start_suite`` 起的是后台线程，所以
``new_execution`` 注入一个**可控的假执行体**——它让「run 还在跑的时候取消」这件事
可复现（真实主链 1 秒跑完，取消永远赢不了那个竞态）。
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fakes_execution import FakeAlbums, FakeHttpClient, FakeResultWriter, judge_reply

from photobench.application.config.service import ConfigService
from photobench.application.run_execution.service import RunRequest
from photobench.application.runs.index import RunIndex
from photobench.application.runs.suites import RunSuites
from photobench.domain.errors import InvalidRequestError
from photobench.infrastructure.config.env import DotenvSecretStore
from photobench.infrastructure.config.files import JsonConfigStore

PROVIDERS = {
    "default_provider_id": "cloud",
    "providers": {"cloud": {"url": "http://judge:1/v1", "model": "judge-model", "api_key": ""}},
}
TARGETS = {
    "default_target_id": "local",
    "targets": {
        "local": {
            "label": "本机",
            "manager_url": "http://manager:8500",
            "model_base_url": "http://127.0.0.1:8100/v1",
        }
    },
}
MANIFEST = {
    "album_id": "album-x",
    "album_name": "测试相册",
    "photos": ["photos/a.jpg"],
    "videos": [],
    "faces": [],
    "qa_sets": {"qa-1": "qa1.jsonl"},
}


class FakeRunStore:
    """``RunStore`` 的替身：只在内存里放一份 state。"""

    def __init__(self, states: dict[str, dict[str, Any]] | None = None) -> None:
        self.states = states or {}

    def iter_entries(self) -> Iterator[dict[str, Any]]:
        return iter([])

    def load_state(self, run_id: str) -> dict[str, Any]:
        if run_id not in self.states:
            raise KeyError(run_id)
        return self.states[run_id]

    def read_reviews(self, run_id: str) -> dict[str, Any]:
        if run_id not in self.states:
            raise KeyError(run_id)
        return {"reviews": {}}

    def read_gpu_sample_lines(self, run_id: str) -> list[str]:
        return []


class FakeExecution:
    """``RunExecution`` 的替身：``execute`` 可以被一个闸门卡住，好让取消有窗口。"""

    def __init__(self, request: RunRequest, gate: threading.Event | None = None) -> None:
        self.request = request
        self.gate = gate
        self.cancels: list[str] = []
        self.state = _State({"run_id": request.run_id, "status": "running", "items": [], "phases": {}})

    def cancel(self, source: str = "api") -> None:
        self.cancels.append(source)
        self.state.request_cancel(source)

    def execute(self, request: RunRequest) -> dict[str, Any]:
        if self.gate is not None:
            self.gate.wait(timeout=5)
        if self.state.cancelled:
            self.state.set("status", "cancelled")
        else:
            self.state.set("status", "completed")
        return self.state.data


class _State:
    """``RunState`` 的最小替身（只用到状态读写与取消信号）。"""

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.cancel_event = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def request_cancel(self, source: str = "api") -> None:
        self.cancel_event.set()
        self.data["cancel_source"] = source
        self.data["status"] = "cancelling" if self.data.get("status") else "cancelled"


def _suites(
    tmp_path: Path,
    *,
    store: FakeRunStore | None = None,
    writer: FakeResultWriter | None = None,
    http: FakeHttpClient | None = None,
    gate: threading.Event | None = None,
    **overrides: Any,
) -> RunSuites:
    config_root = tmp_path / "config"
    config_root.mkdir(exist_ok=True)
    (config_root / "runtime_connection.json").write_text(
        json.dumps({"sentrix_url": "http://sentrix:9598", "judge_provider_id": "cloud"}), encoding="utf-8"
    )
    (config_root / "judge_providers.json").write_text(json.dumps(PROVIDERS), encoding="utf-8")
    (config_root / "vllm_targets.json").write_text(json.dumps(TARGETS), encoding="utf-8")
    config = ConfigService(JsonConfigStore(config_root), DotenvSecretStore(config_root))
    albums = FakeAlbums(manifest=MANIFEST)
    kwargs: dict[str, Any] = {
        "store": store or FakeRunStore(),
        "writer": writer or FakeResultWriter(),
        "index": RunIndex(),
        "albums": albums,
        "config": config,
        "sentrix": _FakeSentrix(),
        "http": http or FakeHttpClient(),
        "new_execution": lambda request: FakeExecution(request, gate),
        "lifecycle_for": lambda _url: _FakeLifecycle(),
        "manager_telemetry_for": lambda _url: _FakeTelemetry(),
        "detect_framework": lambda _value, _url: "generic",
        "sleep": lambda _seconds: None,
    }
    kwargs.update(overrides)
    return RunSuites(**kwargs)


class _FakeSentrix:
    def start_conversation(self, **_kwargs: Any) -> dict[str, Any]:
        return {"id": "conv_1"}

    def start_turn(self, **_kwargs: Any) -> dict[str, Any]:
        return {"turn_id": "turn_1", "status": "running"}

    def get_turn(self, **_kwargs: Any) -> dict[str, Any]:
        return {"turn_id": "turn_1", "status": "complete", "result": {"answer": "ok"}}


class _FakeLifecycle:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def stop(self, _payload: Any = None, *, timeout: float = 90) -> dict[str, Any]:
        self.calls.append("stop")
        return {"status": "stopped"}

    def start(self, _payload: Any, *, timeout: float = 120) -> dict[str, Any]:
        self.calls.append("start")
        return {"status": "starting"}

    def state(self) -> dict[str, Any]:
        return {"profile": "p", "served_model_name": "p"}


class _FakeTelemetry:
    def gpu_stats(self) -> dict[str, Any]:
        return {"status": "available", "data": {"gpus": [{"index": 0, "temperature_c": 50.0}]}}

    def process_memory(self) -> dict[str, Any]:
        return {"status": "available", "data": {}}

    def system_memory(self) -> dict[str, Any]:
        return {"status": "available", "data": {}}

    def kv_cache(self) -> dict[str, Any]:
        return {"status": "not_applicable", "data": {}}


def _payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"album_id": "album-x", "qa_set": "qa-1", "models": ["qwen"]}
    base.update(overrides)
    return base


def _wait_until(predicate: Any, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# ---- 建 run ---------------------------------------------------------------


def test_start_suite_rejects_a_mode_2_0_does_not_implement(tmp_path: Path):
    """``build``（只导入不问答）在 2.0 仍然没有对应能力。

    **显式报错**而不是静默降级：把一个不认识的 mode 当成 ``full`` 跑，会建出一个
    调用方想避免的新 scope、还做了一整套它没要的问答。

    2026-10-15：``reuse`` 从这条断言里移出去了——它已经接上生产（``RUN_MODES`` 现在
    是 ``("full", "reuse")``），改由 ``test_reuse_requires_an_existing_scope``
    钉它的参数校验。
    """
    with pytest.raises(InvalidRequestError) as excinfo:
        _suites(tmp_path).start_suite(_payload(mode="build"))
    assert "mode must be" in str(excinfo.value)


def test_reuse_requires_an_existing_scope(tmp_path: Path):
    """``mode=reuse`` 没有 ``existing_scope_id`` 就是 400。

    没有 scope id 就没有可复用的对象，而静默降级成 ``full`` 会建出一个新 scope
    ——那正是调用方要避免的那件事（也是 1.0 ``scope_attach`` 存在的全部理由）。
    """
    with pytest.raises(InvalidRequestError) as excinfo:
        _suites(tmp_path).start_suite(_payload(mode="reuse"))

    assert "existing_scope_id" in str(excinfo.value)


def test_reuse_is_accepted_with_an_existing_scope(tmp_path: Path):
    """给了 scope id 就接受，并把 mode 与 scope 一路传下去。"""
    suite = _suites(tmp_path).start_suite(
        _payload(mode="reuse", existing_scope_id="PhotoBench-20260101-000000-album-qwen")
    )

    assert suite["mode"] == "reuse"
    assert suite["existing_scope_id"] == "PhotoBench-20260101-000000-album-qwen"


class _StrictAlbums:
    """只认识一个 album 的 ``AlbumSource``：manifest 查不到就 ``KeyError``。

    ``FakeAlbums``（fakes_execution 那份）对任何 album_id 都返回同一份 manifest——
    用它来测「未知 album 要报错」的用例**没有区分力**（实现完全不做校验也会通过）。
    """

    def __init__(self, album_id: str) -> None:
        self.album_id = album_id

    def manifest(self, album_id: str) -> dict[str, Any]:
        if album_id != self.album_id:
            raise KeyError(album_id)
        return {**MANIFEST, "qa_sets": dict(MANIFEST["qa_sets"])}

    def qa_rows(self, _album_id: str, _qa_set: str) -> list[dict[str, Any]]:
        return []


def test_start_suite_rejects_an_unknown_album(tmp_path: Path):
    suites = _suites(tmp_path, albums=_StrictAlbums("album-x"))
    with pytest.raises(InvalidRequestError) as excinfo:
        suites.start_suite(_payload(album_id="nope"))
    assert "manifest not found" in str(excinfo.value)


def test_start_suite_requires_at_least_one_model(tmp_path: Path):
    with pytest.raises(InvalidRequestError):
        _suites(tmp_path).start_suite(_payload(models=[]))


def test_start_suite_registers_the_run_and_returns_its_id(tmp_path: Path):
    # 闸门让 run 停在 running：否则后台线程会抢在断言之前把它跑完，而这条断言
    # 要的是「刚建出来时它在注册表里」（用 ``_active`` 而不是 ``active_run_ids()``，
    # 后者按状态过滤，会与那个竞态赛跑）。
    gate = threading.Event()
    suites = _suites(tmp_path, gate=gate)
    result = suites.start_suite(_payload())
    assert result["mode"] == "full"
    assert len(result["run_ids"]) == 1
    assert suites.active_run_ids() == result["run_ids"]
    gate.set()


# ---- 取消 -----------------------------------------------------------------


def test_cancel_run_cancels_a_running_run_and_the_chain_sees_it(tmp_path: Path):
    """取消是**两个动作**：置信号（主链下一次检查就抛 ``RunCancelledError``）+ 回收模型。

    **区分力**：只把 ``state["status"]`` 改成 ``cancelled`` 而没置事件的实现，会让
    ``cancel_event.is_set()`` 为假——主链里正在等的那一段永远醒不过来。
    """
    gate = threading.Event()
    suites = _suites(tmp_path, gate=gate)
    run_id = suites.start_suite(_payload())["run_ids"][0]
    assert suites.cancel_run(run_id) == {"cancelled": True, "run_id": run_id}
    execution = suites._active[run_id]
    assert execution.state.cancel_event.is_set() is True  # type: ignore[attr-defined]
    assert execution.cancels == ["run_api"]  # type: ignore[attr-defined]
    gate.set()


def test_cancel_run_on_a_disk_only_run_says_it_is_not_active(tmp_path: Path):
    """不在跑的 run 返回「没取消」而不是 404——1.0 对从磁盘加载的 run 就是这么答的。"""
    store = FakeRunStore({"run-old": {"run_id": "run-old", "status": "completed"}})
    suites = _suites(tmp_path, store=store)
    assert suites.cancel_run("run-old") == {
        "cancelled": False,
        "run_id": "run-old",
        "reason": "run not active (loaded from disk)",
    }
    with pytest.raises(KeyError):
        suites.cancel_run("nope")


def test_cancel_active_suite_cancels_every_running_run(tmp_path: Path):
    gate = threading.Event()
    suites = _suites(tmp_path, gate=gate)
    run_ids = suites.start_suite(_payload(models=["a", "b"]))["run_ids"]
    result = suites.cancel_active_suite()
    assert sorted(result["cancelled"]) == sorted(run_ids)
    assert result["total"] == 2
    gate.set()
    assert _wait_until(lambda: not suites.active_run_ids())


# ---- 删除与评审 -----------------------------------------------------------


def test_delete_run_removes_the_directory_and_the_index_entry(tmp_path: Path):
    store = FakeRunStore({"run-old": {"run_id": "run-old", "status": "completed"}})
    writer = FakeResultWriter()
    suites = _suites(tmp_path, store=store, writer=writer)
    assert suites.delete_run("run-old") == {"deleted": True, "run_id": "run-old"}
    assert writer.deleted == ["run-old"]
    with pytest.raises(KeyError):
        suites.delete_run("nope")


def test_save_reviews_filters_unknown_ids_and_bad_verdicts(tmp_path: Path):
    """界面会整份提交；一条写坏不该让整批评审丢失（1.0 ``:5001``）。

    **区分力**：断言 ``saved`` 计数与**留下的键**——一个不过滤的实现会把 3 条都存下去。
    """
    store = FakeRunStore(
        {"run-1": {"run_id": "run-1", "status": "completed", "items": [{"qa_id": "q1"}, {"qa_id": "q2"}]}}
    )
    writer = FakeResultWriter()
    suites = _suites(tmp_path, store=store, writer=writer)
    result = suites.save_reviews(
        "run-1",
        {
            "reviews": {
                "q1": {"verdict": "correct", "note": "对"},
                "q2": {"verdict": "bogus"},
                "q9": {"verdict": "wrong"},
            }
        },
    )
    assert result["saved"] == 1
    _run_id, stored = writer.reviews[-1]
    assert set(stored["reviews"]) == {"q1"}


def test_reviews_are_refused_while_the_run_is_running(tmp_path: Path):
    store = FakeRunStore({"run-1": {"run_id": "run-1", "status": "running", "items": []}})
    suites = _suites(tmp_path, store=store)
    with pytest.raises(InvalidRequestError) as excinfo:
        suites.save_reviews("run-1", {"reviews": {}})
    assert "while benchmark is running" in str(excinfo.value)


def test_get_run_judge_prompt_returns_what_was_actually_sent(tmp_path: Path):
    store = FakeRunStore(
        {
            "run-1": {
                "run_id": "run-1",
                "status": "completed",
                "items": [
                    {
                        "qa_id": "q1",
                        "judge": {
                            "input": {
                                "messages": [
                                    {"role": "system", "content": "当时的提示词"},
                                    {"role": "user", "content": "x"},
                                ]
                            }
                        },
                    },
                ],
            }
        }
    )
    suites = _suites(tmp_path, store=store)
    assert suites.get_run_judge_prompt("run-1") == {"system_prompt": "当时的提示词"}


def test_get_run_judge_prompt_is_null_when_nothing_was_scored(tmp_path: Path):
    store = FakeRunStore({"run-1": {"run_id": "run-1", "status": "completed", "items": [{"qa_id": "q1"}]}})
    suites = _suites(tmp_path, store=store)
    assert suites.get_run_judge_prompt("run-1") == {"system_prompt": None}


# ---- 重判 -----------------------------------------------------------------


def _judged_state() -> dict[str, Any]:
    return {
        "run_id": "run-1",
        "status": "completed",
        "scope_id": "PhotoBench-x",
        "sentrix_url": "http://sentrix:9598",
        "items": [
            {
                "qa_id": "q1",
                "question": "海边的是哪张？",
                "reference_answer": "a.jpg",
                "answer": "a.jpg",
                "expected_action": "answer",
                "judge": {"score": 0, "reason": "旧的"},
                "runtime_turns": [
                    {
                        "message": "海边的是哪张？",
                        "answer": "a.jpg",
                        "expected_action": "answer",
                        "judge": {"score": 0, "reason": "旧的"},
                    },
                ],
            },
        ],
    }


def test_rejudge_archives_the_old_score_and_writes_a_new_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """重判**必须归档旧分**（``judge_history``）——否则「换个提示词看看分怎么变」不可逆。

    **区分力**：只断言「新分数写进去了」的用例对一个不归档的实现同样成立；
    这条同时断言旧分还在 ``judge_history`` 里。
    """
    state = _judged_state()
    store = FakeRunStore({"run-1": state})
    writer = FakeResultWriter()
    # 调用顺序是「回答质量 → 任务行为」：先质量、后任务（见 ``_run_rejudge``）。
    http = FakeHttpClient(
        [
            judge_reply('{"score":2,"reason":"正确"}'),
            judge_reply('{"actual_action":"answer","reason":"直接回答"}'),
        ]
    )
    suites = _suites(tmp_path, store=store, writer=writer, http=http)
    result = suites.start_rejudge("run-1", {"system_prompt": "新提示词"})
    assert result["total"] == 1
    assert _wait_until(lambda: (state.get("rejudge") or {}).get("status") == "completed")
    item = state["items"][0]
    assert item["judge"]["score"] == 2
    assert item["judge_history"][0]["judge"]["score"] == 0
    assert item["judge"]["rejudge_id"] == result["rejudge_id"]
    # 任务行为判分跟着重判，item 级取最后一轮。
    assert item["task_judge"]["actual_action"] == "answer"
    assert item["runtime_turns"][0]["task_judge"]["actual_action"] == "answer"


def test_rejudge_refuses_while_the_run_is_active(tmp_path: Path):
    store = FakeRunStore({"run-1": {**_judged_state(), "status": "running"}})
    suites = _suites(tmp_path, store=store)
    with pytest.raises(InvalidRequestError):
        suites.start_rejudge("run-1", {"system_prompt": "x"})


def test_rejudge_requires_a_prompt_and_at_least_one_answer(tmp_path: Path):
    store = FakeRunStore(
        {"run-1": _judged_state(), "run-2": {"run_id": "run-2", "status": "completed", "items": []}}
    )
    suites = _suites(tmp_path, store=store)
    with pytest.raises(InvalidRequestError) as excinfo:
        suites.start_rejudge("run-1", {})
    assert "system_prompt is required" in str(excinfo.value)
    with pytest.raises(InvalidRequestError) as excinfo:
        suites.start_rejudge("run-2", {"system_prompt": "x"})
    assert "no completed agent answers" in str(excinfo.value)


# ---- 内存画像 -------------------------------------------------------------


def test_memory_profile_requires_a_manager_and_known_runs(tmp_path: Path):
    store = FakeRunStore({"run-1": _judged_state()})
    suites = _suites(tmp_path, store=store)
    with pytest.raises(InvalidRequestError) as excinfo:
        suites.start_memory_profile({})
    assert "run_ids is required" in str(excinfo.value)
    with pytest.raises(InvalidRequestError):
        suites.start_memory_profile({"run_ids": ["nope"]})
    # 配置里的 vllm_manager_url 是空的（runtime_connection.json 没写），而 payload 也没给
    with pytest.raises(InvalidRequestError) as excinfo:
        suites.start_memory_profile({"run_ids": ["run-1"]})
    assert "model manager" in str(excinfo.value)


def test_memory_profile_replays_questions_without_touching_the_answers(tmp_path: Path):
    """``items_sha256_before/after`` 相等是这条不变量的机械证据（1.0 ``:5175``）。

    **区分力**：一个顺手把重放答案写回 items 的实现会让两个哈希不同——那时
    ``items_integrity_ok`` 是 False，而这正是界面上要看到的那件事。
    """
    state = _judged_state()
    store = FakeRunStore({"run-1": state})
    writer = FakeResultWriter()
    suites = _suites(tmp_path, store=store, writer=writer)
    result = suites.start_memory_profile({"run_ids": ["run-1"], "vllm_manager_url": "http://manager:8500"})
    assert result["status"] == "running"
    assert _wait_until(lambda: (state.get("memory_profile") or {}).get("status") == "completed")
    record = state["memory_profile"]
    assert record["items_integrity_ok"] is True
    assert record["answers_persisted"] is False
    # 内存画像这条路径仍然不换模型（1.0 的 bind-runtime 在 2.0 是「重启 2.0」，
    # 见 application.deploy；这条路径没有端点来源），如实记 false 并写明原因。
    assert record["gamma_sync"]["ok"] is False
    assert "application.deploy" in record["gamma_sync"]["error"]


# ---- resume（2026-10-15 恢复）-----------------------------------------------


def _cancelled_state(**over: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "status": "cancelled",
        "scope_id": "PhotoBench-20260101-000000-album-x-qwen",
        "album_id": "album-x",
        "qa_set": "qa-1",
        "model_profile": "qwen",
        "sentrix_url": "http://sentrix:9598",
        "items": [
            {"qa_id": "q1", "execution_status": "completed"},
            {"qa_id": "q2", "execution_status": "scheduled"},
            {"qa_id": "q3", "execution_status": "running"},
        ],
    }
    state.update(over)
    return state


def test_resume_rejects_a_run_that_is_not_resumable(tmp_path: Path):
    """只有 ``interrupted`` / ``failed`` / ``cancelled`` / ``completed_with_errors``
    能续跑（1.0 ``:7495``）。正在跑的 run 不该被再起一个。"""
    store = FakeRunStore({"run-1": _cancelled_state(status="running")})

    with pytest.raises(InvalidRequestError) as excinfo:
        _suites(tmp_path, store=store).resume_run("run-1")

    assert "cannot be resumed" in str(excinfo.value)


def test_resume_rejects_a_run_without_a_scope_checkpoint(tmp_path: Path):
    """没有 scope 就没有可复用的对象——1.0 ``:7513`` 同样拒绝。"""
    store = FakeRunStore({"run-1": _cancelled_state(scope_id="")})

    with pytest.raises(InvalidRequestError) as excinfo:
        _suites(tmp_path, store=store).resume_run("run-1")

    assert "no memory-space checkpoint" in str(excinfo.value)


def test_resume_carries_only_the_finished_items_and_reuses_the_scope(tmp_path: Path):
    """续跑只带**跑完的**题；``scheduled`` / ``running`` 的留给这一轮重跑。

    判据逐条照 1.0 ``:3414-3417`` 的 ``execution_status not in {"scheduled", "running"}``。
    这也正是取消掉的题必须标 ``scheduled`` 而不能标 ``failed`` 的原因——标 failed
    会被当成「已跑完」，续跑时永远跳过。
    """
    store = FakeRunStore({"run-1": _cancelled_state()})

    result = _suites(tmp_path, store=store, gate=threading.Event()).resume_run("run-1")

    assert result["mode"] == "reuse"
    assert result["scope_id"] == "PhotoBench-20260101-000000-album-x-qwen"
    assert result["resumed_from"] == "run-1"
    # 三题里只有 q1 是 completed；另外两题留给这一轮。
    assert result["resumed_items"] == 1
