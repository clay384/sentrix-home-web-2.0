"""写侧用例：建 run / 取消 / 删除 / 评审写入 / 重判 / 内存画像。

对应 1.0 ``OrchestratorRepository`` 里**会写**的那几个入口：``start_suite``
（``:7306``）/ ``cancel_run``（``:6879``）/ ``cancel_active_suite``（``:6975``）/
``delete_run``（``:7010``）/ ``save_reviews``（``:4988``）/ ``get_run_judge_prompt``
（``:6543``）/ ``start_rejudge``（``:7061``）/ ``start_memory_profile``（``:5159``）。

**为什么是独立的一个「注册表」而不是 ``RunExecution`` 上的方法。** 1.0 的
``OrchestratorRepository`` 同时是「磁盘读侧」（270 个 run 的索引）与「活跃 run 的
注册表」，2.0 把读侧拆给了 ``ReadRuns`` + ``RunIndex``（S9b-1，理由是启动阻塞），
写侧就落在这里。它**只持有正在跑的 run**——磁盘上的 run 由 ``RunStore`` 按需读，
不再常驻内存。

**一处与 1.0 的刻意差异：``mode``。** 1.0 支持 ``full`` / ``reuse`` / ``build``：
``reuse`` 复用历史 scope（读侧 ``_build_reuse_bases`` 那套名字解析），``build`` 只
导入不问答。2.0 的 ``scope_attach`` 相位在 S9b-2 就删了（它依赖写侧路由，而 2.0 的
scope 由导入内部 ensure），所以 **2.0 只接 ``full``**。其余两种明确报错而不是静默
降级：静默把 ``reuse`` 当成 ``full`` 会建出一个新 scope，而那正是调用方想避免的。
这条登记在行为基线的 S9b-3 一节里。

**内存画像（``start_memory_profile``）的一处诚实留空**：1.0 在装载模型之后调
``POST /api/model-profiles/bind-runtime`` 把 Sentrix 指到新模型；2.0 没有那个端点
（换模型=重启 2.0，裁决③，S9c 的 T923）。这里照 ``_phase_model_deploy`` 的先例
**跳过并写明原因**，不假装同步过。
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import hashlib
import json
import logging
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from photobench.application.config.service import (
    ConfigService,
    normalize_model_base_url,
    normalize_service_url,
)
from photobench.application.run_execution.gpu_sampler import GpuSampler
from photobench.application.run_execution.judge import InlineImages, JudgeRunner
from photobench.application.run_execution.service import (
    JudgeSettings,
    RunExecution,
    RunRequest,
    freeze_runtime_request,
)
from photobench.application.runs.index import RunIndex
from photobench.config import default_import_timeout
from photobench.domain.datasets.ports import AlbumSource
from photobench.domain.errors import InvalidRequestError
from photobench.domain.http.ports import JsonHttpClient
from photobench.domain.results.ports import RunResultWriter, RunStore
from photobench.domain.runs import analysis
from photobench.domain.runs.analysis import now_iso
from photobench.domain.runs.execution import safe_slug
from photobench.domain.runtime.ports import LifecycleProvider, RunRuntimeSnapshot, TelemetryProvider
from photobench.domain.sentrix.ports import SentrixClient

#: 2.0 支持的模式。``build`` 仍不在其中（只导入不问答，尚无调用方要它）。
#:
#: **``reuse`` 于 2026-10-15 恢复。** S9b-2 删掉 ``scope_attach`` 时它一并被移除，
#: 理由是「scope 由导入内部 ensure，没有可复用的入口」。但 2.0 自己跑过的每一轮都会
#: 在库里留下一个 scope（``_default_scope_id`` 生成的名字同时是 id），
#: ``GET /api/v1/memory-spaces`` 的 ``reuse_bases`` 一直在列它们、前端 ``useReuseMode``
#: 也一直在让用户选——**只有写侧不接**。代价是一轮评测必须重跑 66 分钟的导入。
RUN_MODES = ("full", "reuse", "build")

#: 1.0 ``:2043`` 的 ``dirty_statuses``。
DIRTY_STATUSES = {"running", "pending", "cancelling"}
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _SuiteDeps:
    """一次 suite 用到的装配件。收成一个值对象只为让 ``start_suite`` 的签名可读。"""

    sentrix_url: str
    manager_url: str
    model_base_url: str
    runtime_framework: str
    judge: JudgeSettings


class RunSuites:
    """活跃 run 的注册表 + 写侧用例。"""

    def __init__(
        self,
        *,
        store: RunStore,
        writer: RunResultWriter,
        index: RunIndex,
        albums: AlbumSource,
        config: ConfigService,
        sentrix: SentrixClient,
        http: JsonHttpClient,
        new_execution: Callable[[RunRequest], RunExecution],
        lifecycle_for: Callable[[str], LifecycleProvider],
        manager_telemetry_for: Callable[[str], TelemetryProvider],
        detect_framework: Callable[[str, str], str],
        build_revision: str = "development",
        budget_defaults: Mapping[str, Any] | None = None,
        inline_images: InlineImages | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._build_revision = build_revision
        self._budget_defaults = dict(budget_defaults or {})
        self._store = store
        self._writer = writer
        self._index = index
        self._albums = albums
        self._config = config
        self._sentrix = sentrix
        self._http = http
        self._new_execution = new_execution
        self._lifecycle_for = lifecycle_for
        #: 内存画像**只要 Manager 那一路遥测**（1.0 是 `ManagerTelemetryProvider(manager_url)`）
        #: ——它测的就是被管模型的内存，端点是不是本机与这件事无关。所以这里不走
        #: ``select_runtime_providers``（那条在端点非本机时会回 ``Unavailable``）。
        self._manager_telemetry_for = manager_telemetry_for
        self._detect_framework = detect_framework
        #: 证据判分要用的「召回图 → data URL」（PIL 在 infrastructure，见 JudgeRunner）。
        self._inline_images = inline_images
        self._sleep = sleep
        self._lock = threading.RLock()
        self._active: dict[str, RunExecution] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._memory_thread: threading.Thread | None = None

    # ---- 内部 ---------------------------------------------------------------

    def _active_ids(self) -> list[str]:
        return [
            rid
            for rid, run in self._active.items()
            if run.state is None or run.state.get("status") in DIRTY_STATUSES
        ]

    @staticmethod
    def _stop_pending(state: Mapping[str, Any]) -> bool:
        return bool(
            (state.get("persistence") or {}).get("status") == "stop_pending"
            or (state.get("phases") or {})
            .get("photo_import", {})
            .get("progress", {})
            .get("observer_stop_pending")
            or (state.get("phases") or {})
            .get("gpu_metrics", {})
            .get("sampling_integrity", {})
            .get("stop_pending_threads")
        )

    def _execute_and_release(self, run_id: str, request: RunRequest) -> None:
        with self._lock:
            execution = self._active.get(run_id)
        if execution is None:
            return
        finished: dict[str, Any] = {}
        try:
            finished = execution.execute(request)
        except Exception as exc:  # noqa: BLE001 — execution/storage failure stays visible
            log.exception("Run execution/cleanup failed: %s", run_id)
            if execution.state is not None:
                with execution.state.lock:
                    execution.state.data.update(status="failed", fatal_error=str(exc), finished_at=now_iso())
                    finished = dict(execution.state.data)
            else:
                finished = {"run_id": run_id, "status": "failed", "fatal_error": str(exc)}
        finally:
            try:
                self._index.upsert(analysis.run_list_entry(finished))
            finally:
                with self._lock:
                    if not self._stop_pending(finished):
                        self._active.pop(run_id, None)

    def _require_inactive(self) -> None:
        busy = self._active_ids()
        if (
            busy
            or any(thread.is_alive() for thread in self._threads.values())
            or any(
                run.state is not None and self._stop_pending(run.state.data) for run in self._active.values()
            )
        ):
            raise InvalidRequestError(f"another benchmark suite is still active: {', '.join(busy)}")

    def _state_if_exists(self, run_id: str) -> dict[str, Any] | None:
        try:
            return self._store.load_state(run_id)
        except KeyError:
            return None

    def _judge_settings(self, payload: Mapping[str, Any]) -> JudgeSettings:
        """1.0 ``start_suite`` 的 judge 解析（``:7330``）：请求体 > provider 解析出的值。"""
        custom = self._config.load_custom_judge_prompts()
        provider_id = str(payload.get("judge_provider_id") or self._config.judge_provider_id)
        _, provider_url, provider_model, provider_key = self._config.resolve_judge_provider(provider_id)
        return JudgeSettings(
            judge_url=str(payload.get("judge_url") or provider_url).rstrip("/"),
            judge_model=str(payload.get("judge_model") or provider_model),
            judge_api_key=str(payload.get("judge_api_key") or provider_key),
            answer_quality_prompt=custom.get("answer_quality"),
            task_judge_prompt=custom.get("task_decision"),
            evidence_judge_prompt=custom.get("evidence"),
        )

    # ---- start_suite --------------------------------------------------------

    def start_suite(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """``POST /api/runs``（1.0 ``start_suite`` ``:7306``）。"""
        mode = str(payload.get("mode") or "full").strip().lower()
        if mode not in RUN_MODES:
            raise InvalidRequestError(f"mode must be one of {sorted(RUN_MODES)}, got: {mode!r}")
        # reuse 复用的是**一个已经导入好的 scope**。没有 scope id 就没有可复用的对象——
        # 静默降级成 full 会建出一个新 scope，而那正是调用方想避免的（1.0 的
        # ``scope_attach`` 相位做的也正是「绑定」这一件事）。
        existing_scope_id = str(payload.get("existing_scope_id") or "").strip()
        if mode == "reuse" and not existing_scope_id:
            raise InvalidRequestError("mode=reuse requires existing_scope_id")
        album_id = str(payload.get("album_id") or "album3-14")
        try:
            manifest = self._albums.manifest(album_id)
        except KeyError as exc:
            raise InvalidRequestError(f"manifest not found for album: {album_id}") from exc
        qa_set = (
            ""
            if mode == "build"
            else str(payload.get("qa_set") or next(iter(manifest.get("qa_sets") or {}), ""))
        )
        if not qa_set and mode != "build":
            raise InvalidRequestError(f"album {album_id} has no qa_sets in manifest")
        if mode != "build" and qa_set not in (manifest.get("qa_sets") or {}):
            raise InvalidRequestError(f"unknown qa_set for album {album_id}: {qa_set}")
        models = payload.get("models", [])
        if not isinstance(models, list) or not models:
            raise InvalidRequestError("models must contain at least one model")
        model_base_url = normalize_model_base_url(str(payload.get("model_base_url") or ""))
        manager_url = normalize_service_url(str(payload.get("vllm_manager_url") or ""))
        runtime_framework = self._detect_framework(
            str(payload.get("runtime_framework") or ""), model_base_url
        )
        endpoint_model = str(payload.get("endpoint_model") or "").strip()
        if not manager_url and not model_base_url:
            # 没有 Manager 也没有直连端点时退回配置里的端点（与 container 的 run_execution
            # 同一条兜底）——但**不**替调用方填 Manager：那是「要不要管模型」的决定。
            model_base_url = self._config.model_base_url
        sentrix_url = str(payload.get("sentrix_url") or self._config.sentrix_url)
        judge = JudgeSettings() if mode == "build" else self._judge_settings(payload)
        self._require_inactive()
        deps = _SuiteDeps(
            sentrix_url=sentrix_url,
            manager_url=manager_url,
            model_base_url=model_base_url,
            runtime_framework=runtime_framework,
            judge=judge,
        )
        suite_id = f"suite-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        created: list[str] = []
        requests: dict[str, RunRequest] = {}
        connection = self._config.public_runtime_connection_config()
        connection["product_settings"] = connection.get("product_settings") or dict(
            self._budget_defaults.get("product_settings", {})
        )
        if mode == "reuse":
            builds = [
                entry.get("scope_build")
                for entry in self._index.entries()
                if entry.get("scope_id") == existing_scope_id and entry.get("scope_build")
            ]
            for build in builds:
                if not isinstance(build, dict):
                    continue
                if build.get("status") != "ready":
                    raise InvalidRequestError("selected scope has an incomplete build")
                if build.get("embedding") is not None and build["embedding"] != connection.get("embedding"):
                    raise InvalidRequestError(
                        "embedding configuration changed; build a new scope before reuse"
                    )
        if payload.get("endpoint_model"):
            connection["endpoint_model"] = str(payload["endpoint_model"])
            connection["model_base_url"] = model_base_url
        with self._lock:
            self._require_inactive()
            for model in models:
                run_id = (
                    f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{safe_slug(album_id)}-"
                    f"{safe_slug(str(model))}-{uuid.uuid4().hex[:6]}"
                )
                request = RunRequest(
                    album_id=album_id,
                    qa_set=qa_set,
                    model_profile=str(model),
                    sentrix_url=sentrix_url,
                    manager_url=deps.manager_url,
                    model_base_url=deps.model_base_url,
                    endpoint_model=endpoint_model,
                    run_id=run_id,
                    mode=mode,
                    qa_concurrency=int(
                        payload.get("qa_concurrency", self._budget_defaults.get("qa_concurrency", 0))
                    ),
                    turn_timeout=int(payload.get("turn_timeout") or 900),
                    # 整册导入在 Mac/CPU 上可能远超默认 2h；允许按 run 放宽客户端超时。
                    import_timeout=int(
                        payload.get("import_timeout") or self._budget_defaults.get("import_timeout") or 0
                    )
                    or default_import_timeout(),
                    # reuse 才传；full 留空让 ``execute`` 走 ``_default_scope_id``。
                    scope_id=existing_scope_id,
                    runtime_framework=deps.runtime_framework,
                    judge_url=deps.judge.judge_url,
                    judge_model=deps.judge.judge_model,
                    judge_api_key=deps.judge.judge_api_key,
                    judge_prompts={
                        "answer_quality": deps.judge.answer_quality_prompt,
                        "task_decision": deps.judge.task_judge_prompt,
                        "evidence": deps.judge.evidence_judge_prompt,
                    },
                    evidence_judge_enabled=deps.judge.evidence_enabled,
                )
                request = freeze_runtime_request(request, connection, self._build_revision)
                requests[run_id] = request
                self._active[run_id] = self._new_execution(request)
                created.append(run_id)

        def _run_sequentially() -> None:
            try:
                for run_id in created:
                    self._execute_and_release(run_id, requests[run_id])
            finally:
                with self._lock:
                    self._threads.pop(suite_id, None)

        thread = threading.Thread(target=_run_sequentially, name=f"suite-{suite_id}", daemon=True)
        with self._lock:
            self._threads[suite_id] = thread
        thread.start()
        return {
            "suite_id": suite_id,
            "run_ids": created,
            "album_id": album_id,
            "models": [str(model) for model in models],
            "qa_set": qa_set,
            "mode": mode,
            # 回显复用的那个 scope（1.0 ``start_suite`` 的响应形状）。full 模式为 None。
            "existing_scope_id": existing_scope_id or None,
            "current_model_snapshot": None,
        }

    # ---- resume -------------------------------------------------------------

    def resume_run(self, run_id: str) -> dict[str, Any]:
        """``POST /api/runs/{id}/resume``（1.0 ``resume_run`` ``:7488``）。

        **与 ``reuse`` 共用同一块地基**：两者都是「绑到一个已经存在的 scope 上、
        不重新导入」。差别只在题目集——``reuse`` 从头跑全部，``resume`` 跳过已跑完的。

        **只续跑「没跑过」的题**，判据逐条照 1.0 ``:3414-3417``：
        ``execution_status not in {"scheduled", "running"}`` 的算已完成。
        这也正是取消掉的题必须标 ``scheduled`` 而不能标 ``failed`` 的原因——
        标 failed 的话它们会被当成「已跑完」，续跑时永远跳过（2026-10-15 改的）。

        2.0 此前直接回 501（``api/routes/suites.py``），理由是「scope 由导入内部 ensure，
        没有复用已存在 scope 这条路」。那条理由随 ``mode=reuse`` 一起消失了。
        """
        self._require_inactive()
        try:
            saved = self._store.load_state(run_id)
        except KeyError as exc:
            raise InvalidRequestError(f"run not found: {run_id}") from exc
        status = str(saved.get("status") or "")
        if status not in {"interrupted", "failed", "cancelled", "completed_with_errors"}:
            raise InvalidRequestError(f"run cannot be resumed while status is {status or 'unknown'}")
        scope_id = str(saved.get("scope_id") or "").strip()
        if not scope_id:
            raise InvalidRequestError("the saved run has no memory-space checkpoint")
        album_id = str(saved.get("album_id") or "")
        qa_set = str(saved.get("qa_set") or "")
        if not album_id or not qa_set:
            raise InvalidRequestError("the saved run is missing album_id or qa_set")
        try:
            self._albums.manifest(album_id)
        except KeyError as exc:
            raise InvalidRequestError(f"manifest not found for album: {album_id}") from exc

        prior_items = tuple(
            item
            for item in (saved.get("items") or [])
            if isinstance(item, dict)
            and str(item.get("execution_status") or "") not in {"scheduled", "running"}
        )
        model_profile = str(saved.get("model_profile") or "")
        new_run_id = (
            f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{safe_slug(album_id)}-"
            f"{safe_slug(model_profile)}-resume-{uuid.uuid4().hex[:6]}"
        )
        request = RunRequest(
            album_id=album_id,
            qa_set=qa_set,
            model_profile=model_profile,
            sentrix_url=str(saved.get("sentrix_url") or self._config.sentrix_url),
            manager_url=str(saved.get("manager_url") or ""),
            model_base_url=str(saved.get("model_base_url") or self._config.model_base_url),
            endpoint_model=str(saved.get("endpoint_model") or ""),
            run_id=new_run_id,
            mode="reuse",
            scope_id=scope_id,
            runtime_framework=str(saved.get("runtime_framework") or ""),
            judge_url=str(saved.get("judge_url") or ""),
            judge_model=str(saved.get("judge_model") or ""),
            prior_items=prior_items,
        )
        prior_snapshot = saved.get("runtime_snapshot")
        if (
            isinstance(prior_snapshot, dict)
            and prior_snapshot.get("model_base_url")
            and prior_snapshot.get("sentrix_url")
        ):
            budgets = prior_snapshot.get("execution_budgets") or {}
            # A resumed run is a new execution/build, with the original dependency/budget facts.
            inherited = dict(
                prior_snapshot,
                required_build_revision=self._build_revision
                if self._build_revision != "development"
                else None,
            )
            request = replace(
                request,
                sentrix_url=str(inherited["sentrix_url"]),
                model_base_url=str(inherited["model_base_url"]),
                manager_url=str(inherited.get("vllm_manager_url") or ""),
                import_timeout=int(budgets.get("import_timeout") or 7200),
                turn_timeout=int(budgets.get("turn_timeout") or 900),
                qa_concurrency=int(budgets.get("qa_concurrency") or 0),
                runtime_snapshot=RunRuntimeSnapshot(json.dumps(inherited), self._build_revision),
                snapshot_source="resumed_run",
            )
        else:
            request = freeze_runtime_request(
                replace(request, snapshot_source="legacy_resume_current_config"),
                self._config.public_runtime_connection_config(),
                self._build_revision,
            )
        with self._lock:
            self._active[new_run_id] = self._new_execution(request)

        def _run_resumed() -> None:
            try:
                self._execute_and_release(new_run_id, request)
            finally:
                with self._lock:
                    self._threads.pop(new_run_id, None)

        thread = threading.Thread(target=_run_resumed, name=f"resume-{new_run_id}", daemon=True)
        with self._lock:
            self._threads[new_run_id] = thread
        thread.start()
        return {
            "run_id": new_run_id,
            "resumed_from": run_id,
            "mode": "reuse",
            "scope_id": scope_id,
            # 带回来的题数——调用方据此判断续跑到底省了多少。
            "resumed_items": len(prior_items),
        }

    # ---- 取消 / 删除 --------------------------------------------------------

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        """``POST /api/runs/{id}/cancel``（1.0 ``cancel_run`` ``:6879``）。"""
        with self._lock:
            execution = self._active.get(run_id)
            if execution is not None and execution.state is not None:
                status = execution.state.get("status")
                if status not in DIRTY_STATUSES:
                    return {"cancelled": False, "run_id": run_id, "reason": f"run status is {status}"}
        if execution is not None:
            execution.cancel(source="run_api")
            return {"cancelled": True, "run_id": run_id}
        if self._state_if_exists(run_id) is None:
            raise KeyError(run_id)
        return {"cancelled": False, "run_id": run_id, "reason": "run not active (loaded from disk)"}

    def cancel_active_suite(self) -> dict[str, Any]:
        """``POST /api/cancel-active``（1.0 ``cancel_active_suite`` ``:6975``）。

        取消**所有**还在跑的 run，不只最近一个 suite——1.0 的注释说明了为什么：
        崩溃重启后会留下孤儿 run，而运维点「全部取消」时想要的是全部。
        """
        cancelled: list[str] = []
        skipped: list[str] = []
        with self._lock:
            targets = list(self._active.items())
        for run_id, execution in targets:
            state = execution.state
            if state is None or state.get("status") in DIRTY_STATUSES:
                execution.cancel(source="cancel_all")
                cancelled.append(run_id)
            else:
                skipped.append(run_id)
        return {"cancelled": cancelled, "skipped": skipped, "total": len(targets)}

    def delete_run(self, run_id: str) -> dict[str, Any]:
        """``DELETE /api/runs/{id}``（1.0 ``delete_run`` ``:7010``）。"""
        state = self._state_if_exists(run_id)
        if state is None and run_id not in self._active:
            raise KeyError(run_id)
        if isinstance(state, dict) and (state.get("rejudge") or {}).get("status") == "running":
            raise InvalidRequestError("cannot delete a run while rejudge is running")
        with self._lock:
            execution = self._active.get(run_id)
            if execution is not None:
                raise InvalidRequestError("cannot delete a run until its workers have stopped")
        if state is not None and state.get("status") in DIRTY_STATUSES:
            raise InvalidRequestError("cannot delete an active run")
        self._writer.delete_run(run_id)
        self._index.remove(run_id)
        return {"deleted": True, "run_id": run_id}

    # ---- 评审 ---------------------------------------------------------------

    def save_reviews(self, run_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """``POST /api/runs/{id}/reviews``（1.0 ``save_reviews`` ``:4988``）。

        **筛选规则照搬**：未知 ``qa_id``、非对象的条目、白名单外的 ``verdict`` 一律
        丢弃而不是报错——界面会整份提交，其中一条写坏不该让整批评审丢失。
        """
        state = self._state_if_exists(run_id)
        if state is None:
            raise KeyError(run_id)
        if state.get("status") in {"running", "pending"}:
            raise InvalidRequestError("cannot change reviews while benchmark is running")
        provided = payload.get("reviews")
        if not isinstance(provided, dict):
            raise InvalidRequestError("reviews must be an object keyed by qa_id")
        valid_ids = {str(item.get("qa_id")) for item in state.get("items") or [] if item.get("qa_id")}
        reviews: dict[str, Any] = {}
        for qa_id, review in provided.items():
            if str(qa_id) not in valid_ids or not isinstance(review, dict):
                continue
            verdict = str(review.get("verdict") or "")
            note = str(review.get("note") or "").strip()
            if verdict not in {"correct", "partial", "wrong"}:
                continue
            reviews[str(qa_id)] = {"verdict": verdict, "note": note, "updated_at": now_iso()}
        stored = {"updated_at": now_iso(), "reviews": reviews}
        self._writer.save_reviews(run_id, stored)
        return {"saved": len(reviews), **stored}

    def get_run_judge_prompt(self, run_id: str) -> dict[str, Any]:
        """``GET /api/runs/{id}/judge-prompt``（1.0 ``:6543``）：从落盘的判分输入里
        反查当时真正发出去的 system 提示词。"""
        state = self._state_if_exists(run_id)
        if state is None:
            raise KeyError(run_id)
        for item in state.get("items") or []:
            messages = ((item.get("judge") or {}).get("input") or {}).get("messages")
            if not isinstance(messages, list):
                continue
            for message in messages:
                if isinstance(message, dict) and message.get("role") == "system":
                    return {"system_prompt": str(message.get("content") or "")}
        return {"system_prompt": None}

    # ---- 重判 ---------------------------------------------------------------

    @staticmethod
    def _rejudge_targets(items: Sequence[dict[str, Any]]) -> list[tuple[int, int | None]]:
        """1.0 ``_rejudge_targets``（``:4906``）：每个可判分的 (item, turn)。

        **一处与 1.0 的形状差异**：1.0 数的是 ``item["conversation"]``（题面声明的轮次），
        2.0 数的是 ``item["runtime_turns"]``（实际跑过的轮次）。2.0 的 item 不保存
        ``conversation``——它是 QA 行的输入，不是 run 的产物；而重判要判的是**当时
        实际发生的那些回合**。两者混用会让 item 级分数取到一个没有 judge 的轮次
        （见 ``_run_rejudge`` 里 ``turns[-1]`` 那一行）。
        """
        targets: list[tuple[int, int | None]] = []
        for item_index, item in enumerate(items):
            if item.get("question") is None or item.get("reference_answer") is None or "answer" not in item:
                continue
            turns = item.get("runtime_turns") or []
            targets.extend(
                (item_index, turn_index) for turn_index in (range(len(turns)) if turns else [None])
            )
        return targets

    def start_rejudge(self, run_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """``POST /api/runs/{id}/rejudge``（1.0 ``start_rejudge`` ``:7061``）。

        **它重判的是已落盘的答案，不重跑 Agent**——所以能在一轮很贵的评测跑完之后，
        换一个 judge 模型/提示词重新打分。这也意味着它必须**归档旧分数**
        （``judge_history``），否则「改一个字看看分数怎么变」就变成不可逆的。
        """
        system_prompt = str(payload.get("system_prompt") or "").strip()
        if not system_prompt:
            raise InvalidRequestError("system_prompt is required")
        if len(system_prompt) > 50000:
            raise InvalidRequestError("system_prompt is too long")
        provider_id = str(payload.get("judge_provider_id") or self._config.judge_provider_id)
        _, resolved_url, resolved_model, resolved_key = self._config.resolve_judge_provider(provider_id)
        judge_url = str(payload.get("judge_url") or resolved_url).rstrip("/")
        judge_model = str(payload.get("judge_model") or resolved_model)
        judge_api_key = str(payload.get("judge_api_key") or resolved_key)
        saved_custom = self._config.load_custom_judge_prompts()
        task_prompt = str(payload.get("task_system_prompt") or "").strip() or saved_custom.get(
            "task_decision"
        )
        evidence_prompt = str(payload.get("evidence_system_prompt") or "").strip() or saved_custom.get(
            "evidence"
        )

        with self._lock:
            state = self._state_if_exists(run_id)
            if state is None:
                raise KeyError(run_id)
            if state.get("status") in {"running", "pending"}:
                raise InvalidRequestError("cannot rejudge while the benchmark run is active")
            if (state.get("rejudge") or {}).get("status") == "running":
                raise InvalidRequestError("rejudge is already running")
            targets = self._rejudge_targets(state.get("items") or [])
            if not targets:
                raise InvalidRequestError("run has no completed agent answers to rejudge")
            rejudge_id = f"rejudge-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
            previous = state.get("rejudge")
            if previous:
                state.setdefault("rejudge_history", []).append(previous)
            state["rejudge"] = {
                "rejudge_id": rejudge_id,
                "status": "running",
                "started_at": now_iso(),
                "finished_at": None,
                "total": len(targets),
                "completed": 0,
                "failed": 0,
                "unit": "conversation_turn",
                "judge_model": judge_model,
                "judge_url": judge_url,
                "current_item_index": None,
                "current_turn_index": None,
            }
            for index in sorted({item_index for item_index, _ in targets}):
                item = state["items"][index]
                prior = item.get("judge")
                if prior:
                    item.setdefault("judge_history", []).append({"archived_at": now_iso(), "judge": prior})
                item["judge"] = {"score": None, "reason": "", "status": "pending", "rejudge_id": rejudge_id}
                for turn in item.get("runtime_turns") or []:
                    for field in ("judge", "task_judge", "evidence_judge"):
                        prior_turn = turn.get(field)
                        if prior_turn:
                            turn.setdefault(f"{field}_history", []).append(
                                {"archived_at": now_iso(), field: prior_turn}
                            )
                    turn["judge"] = {
                        "score": None,
                        "reason": "",
                        "status": "pending",
                        "rejudge_id": rejudge_id,
                    }
            state["summary"] = analysis.effective_summary(state)
            self._writer.write_snapshot(run_id, state)

        runner = JudgeRunner(
            http=self._http,
            judge_url=judge_url,
            judge_model=judge_model,
            judge_api_key=judge_api_key,
            answer_quality_prompt=system_prompt,
            task_judge_prompt=task_prompt,
            evidence_judge_prompt=evidence_prompt,
            sentrix_url=str(state.get("sentrix_url") or self._config.sentrix_url),
            inline_images=self._inline_images,
        )

        #: 跨条目并发的上限，与 ``_resolve_judge_concurrency`` 同一个口径
        #: （``min(8, qa_concurrency)``）。**rejudge 此前是纯串行**：qa_eval 阶段按
        #: 这个数并行判，重判却一条一条来，561 个 turn 要跑近两小时。
        #:
        #: **worker 只算不写。** ``_state_if_exists`` 每次读整个 run.json（本轮 34MB），
        #: 让 561 个 worker 各读一遍既慢、又可能读到别人写了一半的状态。所以输入在
        #: 提交前一次性拍平成不可变元组，**状态回写全部留在主线程**——与并发前的
        #: 「先读当前 state、改、再整份写回」保持同一个形状。
        concurrency = max(1, min(8, int(state.get("qa_concurrency") or 4)))

        def _payload(index: int, turn_index: int | None) -> tuple[Any, ...]:
            item = (state.get("items") or [])[index]
            turns = item.get("runtime_turns") or []
            record = turns[turn_index] if turn_index is not None else item
            expected = record.get("expected_action") or item.get("expected_action")
            reference = str(record.get("reference_answer") or item.get("reference_answer") or "")
            if not reference and expected == "clarify":
                reference = "应要求用户提供至少一个具体、可用于后续检索的锚点；询问形式不限。"
            return (
                index,
                turn_index,
                str(record.get("message") or item.get("question") or ""),
                reference,
                str(record.get("answer") or ""),
                expected,
                item.get("task_type"),
                item.get("question_type"),
                item.get("answerability"),
                turns[: turn_index + 1] if turn_index is not None else None,
                record.get("agent_status") or item.get("agent_status"),
                record.get("termination_reason") or item.get("termination_reason") or "",
            )

        def _judge_one(payload: tuple[Any, ...]) -> tuple[int, int | None, dict[str, Any], dict[str, Any]]:
            (
                index,
                turn_index,
                message,
                reference,
                answer,
                expected,
                task_type,
                question_type,
                answerability,
                conversation,
                agent_status,
                termination,
            ) = payload
            judge_result = runner.judge(
                message,
                reference,
                answer,
                expected_action=expected,
                task_type=task_type,
                question_type=question_type,
                answerability=answerability,
                conversation=conversation,
            )
            # 任务行为判分跟着一起重判（1.0 ``:7200`` 的两个 future）——只重判
            # 回答质量会让 ``task_judges`` 停在上一次的结论上，而界面上它们
            # 看起来是同一批次的产物。
            task_result = runner.judge_task_action(
                message,
                answer,
                expected,
                agent_status,
                termination,
                task_type=task_type,
                question_type=question_type,
                answerability=answerability,
                reference=reference,
                conversation=conversation,
            )
            return index, turn_index, judge_result, task_result

        def _run_rejudge() -> None:
            try:
                prepared = [_payload(index, turn_index) for index, turn_index in targets]
                with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
                    futures = {pool.submit(_judge_one, payload): payload for payload in prepared}
                    for completed, future in enumerate(concurrent.futures.as_completed(futures), 1):
                        current = self._state_if_exists(run_id)
                        if current is None:
                            return
                        payload = futures[future]
                        index, turn_index = int(payload[0]), payload[1]
                        try:
                            _, _, judge_result, task_result = future.result()
                        except Exception as exc:  # noqa: BLE001
                            # **单条失败不拖垮整轮**：串行版里一个异常会直接让整个
                            # rejudge 记 failed，而 561 条里挂一条不该是这个结局。
                            judge_result = {
                                "score": None,
                                "reason": f"judge_error: {exc}",
                                "status": "failed",
                            }
                            task_result = {
                                "actual_action": None,
                                "correct": None,
                                "reason": f"judge_error: {exc}",
                            }
                        item = (current.get("items") or [])[index]
                        turns = item.get("runtime_turns") or []
                        record = turns[turn_index] if turn_index is not None else item
                        judge_result["ground_truth_source"] = "current_qa_metadata"
                        judge_result["judged_at"] = now_iso()
                        judge_result["rejudge_id"] = rejudge_id
                        judge_result["status"] = (
                            "completed" if judge_result.get("score") in {0, 1, 2} else "failed"
                        )
                        record["judge"] = judge_result
                        task_result["ground_truth_source"] = "current_qa_metadata"
                        record["task_judge"] = task_result
                        if turns:
                            item["judge"] = turns[-1].get("judge") or {}
                            item["task_judges"] = [turn.get("task_judge") or {} for turn in turns]
                            item["task_judge"] = turns[-1].get("task_judge") or {}
                            # **证据判分不重判**：它要按 asset_id 取回图片字节，而那条路径依赖
                            # 一个活着的 2.0（1.0 在 ``:7226`` 现取资产表）。``EVIDENCE_JUDGE``
                            # 默认关；这里如实把它标成 not_applicable 而不是留一个上次的值。
                            item["evidence_judge"] = {"score": None, "reason": "not_applicable"}
                        else:
                            item["judge"] = record.get("judge") or {}
                            item["task_judge"] = record.get("task_judge") or {}
                        current["rejudge"]["completed"] = completed
                        if judge_result.get("score") not in {0, 1, 2}:
                            current["rejudge"]["failed"] += 1
                        current["summary"] = analysis.effective_summary(current)
                        self._writer.write_snapshot(run_id, current)
                final = self._state_if_exists(run_id)
                if final is not None:
                    final["rejudge"]["status"] = "completed"
                    final["rejudge"]["finished_at"] = now_iso()
                    self._writer.write_snapshot(run_id, final)
                    self._index.upsert(analysis.run_list_entry(final))
            except Exception as exc:  # noqa: BLE001 —— 重判线程的兜底：状态要能被读出来
                failed = self._state_if_exists(run_id)
                if failed is not None:
                    failed.setdefault("rejudge", {})["status"] = "failed"
                    failed["rejudge"]["finished_at"] = now_iso()
                    failed["rejudge"]["error"] = str(exc)
                    self._writer.write_snapshot(run_id, failed)

        thread = threading.Thread(target=_run_rejudge, name=rejudge_id, daemon=True)
        thread.start()
        return {"run_id": run_id, "rejudge_id": rejudge_id, "total": len(targets), "status": "running"}

    # ---- 内存画像 -----------------------------------------------------------

    def start_memory_profile(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """``POST /api/memory-profile``（1.0 ``start_memory_profile`` ``:5159``）。

        重放已存的题目来测「同等负载下模型占多少内存」，**不改动任何问答结果**——
        1.0 用 ``items_sha256_before/after`` 两个哈希把这条不变量钉在 run.json 里。

        **与 1.0 的两处差异**：

        1. 1.0 在装载前调 ``bind-runtime`` 把 Sentrix 指向新模型；2.0 没有那个端点
           （换模型=重启 2.0），所以**留空并记原因**——与 ``_phase_model_deploy`` 的
           ``gamma_sync`` 同一条处置。
        2. 1.0 的 ``model_source == "cloud_api"`` 检查没有了（2.0 没有云端模型）。
        """
        run_ids = [str(value) for value in payload.get("run_ids") or []]
        if not run_ids:
            raise InvalidRequestError("run_ids is required")
        with self._lock:
            self._require_inactive()
            if self._memory_thread is not None and self._memory_thread.is_alive():
                raise InvalidRequestError("memory profiling is already running")
            states: dict[str, dict[str, Any]] = {}
            for rid in run_ids:
                state = self._state_if_exists(rid)
                if state is None:
                    raise InvalidRequestError(f"run not found: {rid}")
                states[rid] = state
            manager_url = str(payload.get("vllm_manager_url") or self._config.vllm_manager_url or "")
            if not manager_url:
                raise InvalidRequestError("memory profiling requires a model manager")
            task_id = f"memory-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
            for rid, state in states.items():
                state["memory_profile"] = {
                    "task_id": task_id,
                    "status": "pending",
                    "source_run_id": rid,
                    "method": "saved_question_replay",
                    "created_at": now_iso(),
                    "answers_persisted": False,
                    "album_reused": True,
                    "items_sha256_before": _items_digest(state.get("items") or []),
                }
                self._writer.write_snapshot(rid, state)

        def execute() -> None:
            lifecycle = self._lifecycle_for(manager_url)
            telemetry = self._manager_telemetry_for(manager_url)
            sampler = GpuSampler(telemetry, interval=0.1)
            for rid, state in states.items():
                record = state["memory_profile"]
                record.update({"status": "running", "started_at": now_iso()})
                completed = failed = request_count = 0
                try:
                    # 没在跑的模型本来就停不掉——卸载失败不阻断装载（1.0 ``:2547`` 的
                    # 同一条处置）。
                    with contextlib.suppress(Exception):
                        lifecycle.stop({"timeout": 60}, timeout=90)
                    self._sleep(5)
                    lifecycle.start(
                        {"profile": state.get("model_profile"), "wait_ready": True, "ready_timeout": 600},
                        timeout=700,
                    )
                    model_state = lifecycle.state()
                    # 1.0 在这里调 bind-runtime；2.0 的等价物是「重启 2.0」
                    # （``application.deploy.SentrixModelSwitch``，S9c · T923）。
                    # **这里仍然留空**：内存画像要指向哪个端点，1.0 是从 target 配置推的，
                    # 而这条路径的请求体里只有 ``vllm_manager_url``，没有可用的端点来源。
                    # 与其猜一个端点去重启主 API（代价是可能把正在跑的评测指歪），不如
                    # 如实记下「没做」，并把模块指向真正的实现点。
                    record["gamma_sync"] = {
                        "ok": False,
                        "error": "not_applicable: 内存画像路径没有端点来源；换模型重启见 application.deploy",
                    }
                    sampler.start()
                    self._sleep(2)
                    for item in state.get("items") or []:
                        messages = [
                            str(turn.get("message") or "")
                            for turn in (item.get("conversation") or [])
                            if turn.get("message")
                        ]
                        if not messages and item.get("question"):
                            messages = [str(item["question"])]
                        conversation_id = ""
                        item_ok = True
                        for message in messages:
                            request_count += 1
                            try:
                                if not conversation_id:
                                    conversation_id = str(
                                        self._sentrix.start_conversation(
                                            scope_id=str(state.get("scope_id") or ""),
                                            title=str(item.get("qa_id") or "memory"),
                                        ).get("id")
                                        or ""
                                    )
                                started = self._sentrix.start_turn(
                                    conversation_id=conversation_id, message=message, timeout=300
                                )
                                _await_turn(
                                    self._sentrix, str(started.get("turn_id") or ""), 900, self._sleep
                                )
                            except Exception:  # noqa: BLE001 —— 单题失败不中断整段画像
                                item_ok = False
                                failed += 1
                        if item_ok:
                            completed += 1
                        record.update(
                            {
                                "questions_completed": completed,
                                "questions_total": len(state.get("items") or []),
                                "requests_completed": request_count - failed,
                                "requests_total_so_far": request_count,
                                "failed_requests": failed,
                            }
                        )
                        self._writer.write_snapshot(rid, state)
                    self._sleep(1)
                    sampler.stop()
                    record.update(
                        {
                            **sampler.aggregate(),
                            "status": "completed",
                            "finished_at": now_iso(),
                            "model_profile": state.get("model_profile"),
                            "model_state": {
                                key: model_state.get(key)
                                for key in (
                                    "dtype",
                                    "quantization",
                                    "load_format",
                                    "gpu_memory_utilization",
                                    "max_model_len",
                                    "max_num_seqs",
                                    "default_max_tokens",
                                    "tensor_parallel_size",
                                    "enable_lora",
                                )
                            },
                            "answers_persisted": False,
                            "original_items_untouched": True,
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    sampler.stop()
                    record.update(
                        {
                            **sampler.aggregate(),
                            "status": "failed",
                            "finished_at": now_iso(),
                            "error": str(exc),
                            "answers_persisted": False,
                        }
                    )
                finally:
                    record["items_sha256_after"] = _items_digest(state.get("items") or [])
                    record["items_integrity_ok"] = record.get("items_sha256_before") == record.get(
                        "items_sha256_after"
                    )
                    self._writer.write_snapshot(rid, state)

        thread = threading.Thread(target=execute, name=task_id, daemon=True)
        with self._lock:
            self._memory_thread = thread
        thread.start()
        return {"task_id": task_id, "run_ids": run_ids, "status": "running"}

    # ---- 供路由用的只读投影 -------------------------------------------------

    def active_states(self) -> dict[str, dict[str, Any]]:
        """还在跑的 run 的**活状态**，给 ``GET /api/runs`` 的列表叠加用。

        **索引里没有它们。** 写侧只在 run 结束时 ``upsert``（``RunIndex.upsert``：
        「一次 suite 只会新增几个 run，重扫 267 个 ``run.json`` 是 75 秒的工作」），
        而在那之前索引根本不知道这一轮存在。于是整轮评测期间列表页是空的——而前端
        **没有 URL 路由**（``App.tsx`` 用 ``useState`` 选 run，没有 deep link），
        所以那一轮在界面上**完全够不着**：看不到进度，也点不到取消。

        实测 2026-10-09：一轮 487 题的评测跑了一个多小时，界面上一直什么都没有。
        """
        with self._lock:
            states: dict[str, dict[str, Any]] = {}
            for run_id in self.active_run_ids():
                execution = self._active.get(run_id)
                if execution is None or execution.state is None:
                    continue
                # 浅拷贝：调用方（列表页）不该拿到执行体正在改的那只 dict。
                # 取 ``.data`` 而不是 ``dict(state)``——``RunState`` 是带锁的**包装**，
                # 它本身不是 dict。
                with execution.state.lock:
                    states[run_id] = analysis.run_list_entry(execution.state.data)
            return states

    def active_run_ids(self) -> list[str]:
        """Still executing runs; stopped executions are released, stop-pending blocks admission."""
        with self._lock:
            return [
                run_id
                for run_id, execution in self._active.items()
                if execution.state is not None and execution.state.get("status") in DIRTY_STATUSES
            ]


def _items_digest(items: Sequence[dict[str, Any]]) -> str:
    """1.0 ``:5175`` 的 ``items_sha256``：排序键 + 紧凑分隔符，跨进程可比。"""
    return hashlib.sha256(
        json.dumps(list(items), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _await_turn(
    client: SentrixClient, turn_id: str, timeout: int, sleep: Callable[[float], None]
) -> dict[str, Any]:
    """内存画像用的回合等待。与 ``RunExecution._await_turn`` 同一条判据
    （非 ``pending`` / ``running`` 即终态），但**不共享实现**：那条挂在有状态的主链上，
    这条只服务画像重放，硬凑一个共享函数会把两条生命周期绑在一起。"""
    deadline = time.monotonic() + timeout
    polled = client.get_turn(turn_id=turn_id)
    while str(polled.get("status") or "") in {"pending", "running"}:
        if time.monotonic() >= deadline:
            raise TimeoutError(f"assistant turn {turn_id} did not complete within {timeout}s")
        sleep(0.5)
        polled = client.get_turn(turn_id=turn_id)
    return polled


__all__ = ["DIRTY_STATUSES", "RUN_MODES", "RunSuites"]
