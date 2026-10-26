"""run-execution 主链用例的假件。

与 ``fakes.py`` / ``fakes_read.py`` 同一条纪律——**未预设的调用直接失败，不回落真实现**。
这条纪律在本切片尤其要紧：主链的每一步都会打主 API，一个漏配的假件回落成真调用，
就会让用例变成「这台机器上 8191 起没起」的函数。

**这一份的假件比前两份"全"**：``FakeLifecycleProvider`` 在这里实现了
``start`` / ``stop``（``fakes_read.py`` 那份故意不实现，因为 S9b-1 只查询）。它们现在是
被测代码真的会调的东西——一个不实现这两个方法的假件会让
``_phase_model_deploy`` 的用例根本跑不起来，那才是漏报。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from photobench.domain.runs.analysis import now_iso

# ---------------------------------------------------------------------------
# 主 API
# ---------------------------------------------------------------------------


class FakeSentrixClient:
    """``SentrixClient`` 的替身：记录调用顺序 + 按预设作答。

    ``asset_polls`` 是一个**按次消费**的队列：``pipeline_processing`` 的语义就是
    「同一句话问多次、每次答案不同」，只给一个固定返回值的话，轮询逻辑根本跑不起来。
    队列耗尽后固定返回最后一个，这样「轮询没有按预期收敛」会以断言失败而不是
    索引错误的形式出现。
    """

    def __init__(
        self,
        *,
        assets: list[dict[str, Any]] | None = None,
        asset_polls: list[list[dict[str, Any]]] | None = None,
        asset_cycle: list[list[dict[str, Any]]] | None = None,
        import_result: dict[str, Any] | None = None,
        pipeline_result: dict[str, Any] | None = None,
        explicit_pipeline: bool = False,
        entity_ids: dict[str, str] | None = None,
        turns: dict[str, list[dict[str, Any]]] | None = None,
        turn_errors: dict[str, list[Exception]] | None = None,
        seed_error: dict[str, Exception] | None = None,
        poll_limit: int = 0,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        #: ``list_assets`` 的最大调用次数（0 = 不限）。轮询一旦不收敛就是死循环，
        #: 而这个假件是唯一能把它变成一个**失败**而不是一次挂起的地方——CI 里挂起
        #: 会烧满超时预算，失败会立刻指出是哪一条断言没成立。
        self.poll_limit = poll_limit
        self.poll_count = 0
        self.assets = assets or []
        self.asset_polls = list(asset_polls or [])
        #: 每轮换一张的无限序列：模拟「同一个资产的状态一直在变」。
        #: ``asset_polls`` 只能给有限张，模拟不了「一直抖」——那正是停滞检测的
        #: 判据要区分的情形。
        self.asset_cycle = [list(item) for item in (asset_cycle or [])]
        self.import_result = import_result or {
            "scope_id": "",
            "directory": "",
            "imported": 0,
            "deduplicated": 0,
            "events": 0,
            "lexical_terms": 0,
            "summarized": 0,
            "failures": [],
        }
        self.pipeline_result = pipeline_result or {
            "scope_id": "",
            "processed": 0,
            "skipped": 0,
            "events": 0,
            "lexical_terms": 0,
            "video_scenes": 0,
            "summarized": 0,
            "timings": {},
            "failures": [],
            "memory_graph": None,
        }
        self.supports_explicit_pipeline = explicit_pipeline
        self.entity_ids = entity_ids or {}
        #: turn_id → 该回合按次消费的 ``get_turn`` 响应队列。
        self.turns = turns or {}
        #: turn_id → ``get_turn`` 要先抛出的瞬态网络错误。用来钉住评测端的重试语义。
        self.turn_errors = {key: list(value) for key, value in (turn_errors or {}).items()}
        self.seed_error = seed_error or {}
        self._turn_seq = 0
        self._conversation_seq = 0
        self.turn_polls: dict[str, int] = {}

    def _record(self, call: str, **kwargs: Any) -> None:
        """``call`` 不能叫 ``name``：``seed_person`` 自己有一个 ``name=`` 关键字，
        参数名撞上就会被 Python 报成 "got multiple values for argument"。"""
        self.calls.append((call, kwargs))

    def call_names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def diagnostics(self, *, timeout: int = 10) -> dict[str, Any]:
        self._record("diagnostics", timeout=timeout)
        return getattr(self, "runtime_diagnostics", {})

    def seed_person(
        self,
        *,
        scope_id: str,
        name: str,
        family_role: str,
        aliases: Sequence[str],
        file_name: str,
        content: bytes,
        timeout: int = 120,
    ) -> dict[str, Any]:
        self._record(
            "seed_person",
            scope_id=scope_id,
            name=name,
            family_role=family_role,
            aliases=list(aliases),
            file_name=file_name,
            content=content,
        )
        if name in self.seed_error:
            raise self.seed_error[name]
        return {
            "entity_id": self.entity_ids.get(name, f"ent_{name}"),
            "cluster_id": f"clu_{name}",
            "name": name,
            "family_role": family_role or None,
            "aliases": list(aliases),
            "face_count": 1,
            "photos_without_faces": 0,
        }

    def import_relationships(
        self,
        *,
        scope_id: str,
        relationships: Sequence[Mapping[str, Any]],
        entity_by_name: Mapping[str, str],
        timeout: int = 60,
    ) -> dict[str, Any]:
        self._record(
            "import_relationships",
            scope_id=scope_id,
            relationships=list(relationships),
            entity_by_name=dict(entity_by_name),
        )
        return {"requested": len(relationships), "imported": len(relationships), "results": []}

    def import_album(self, *, scope_id: str, directory: str, limit: int, timeout: int) -> dict[str, Any]:
        self._record("import_album", scope_id=scope_id, directory=directory, limit=limit, timeout=timeout)
        return {**self.import_result, "scope_id": scope_id, "directory": directory}

    def import_assets(self, *, scope_id: str, directory: str, limit: int, timeout: int) -> dict[str, Any]:
        self._record("import_assets", scope_id=scope_id, directory=directory, limit=limit, timeout=timeout)
        return {**self.import_result, "scope_id": scope_id, "directory": directory}

    def process_pipeline(self, *, scope_id: str, asset_ids: Sequence[str], timeout: int) -> dict[str, Any]:
        self._record("process_pipeline", scope_id=scope_id, asset_ids=list(asset_ids), timeout=timeout)
        return {**self.pipeline_result, "scope_id": scope_id}

    def list_assets(self, *, scope_id: str, limit: int, timeout: int = 60) -> list[dict[str, Any]]:
        self._record("list_assets", scope_id=scope_id, limit=limit)
        self.poll_count += 1
        if self.poll_limit and self.poll_count > self.poll_limit:
            raise AssertionError(f"list_assets 被调了 {self.poll_count} 次仍未收敛（上限 {self.poll_limit}）")
        if self.asset_cycle:
            return list(self.asset_cycle[(self.poll_count - 1) % len(self.asset_cycle)])
        if self.asset_polls:
            return self.asset_polls.pop(0) if len(self.asset_polls) > 1 else self.asset_polls[0]
        return self.assets

    def start_conversation(self, *, scope_id: str, title: str = "", timeout: int = 30) -> dict[str, Any]:
        self._record("start_conversation", scope_id=scope_id, title=title)
        self._conversation_seq += 1
        return {
            "id": f"conv_{self._conversation_seq}",
            "scope_id": scope_id,
            "title": title,
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "last_message_at": now_iso(),
        }

    def start_turn(self, *, conversation_id: str, message: str, timeout: int = 300) -> dict[str, Any]:
        self._record("start_turn", conversation_id=conversation_id, message=message, timeout=timeout)
        self._turn_seq += 1
        return {"turn_id": f"turn_{self._turn_seq}", "conversation_id": conversation_id, "status": "running"}

    def get_turn(self, *, turn_id: str, timeout: int = 30) -> dict[str, Any]:
        self._record("get_turn", turn_id=turn_id, timeout=timeout)
        errors = self.turn_errors.get(turn_id) or []
        if errors:
            raise errors.pop(0)
        queue = list(self.turns.get(turn_id) or [])
        index = self.turn_polls.get(turn_id, 0)
        self.turn_polls[turn_id] = index + 1
        if not queue:
            raise AssertionError(f"用例没有预设 turn {turn_id} 的轮询响应")
        return queue[index] if index < len(queue) else queue[-1]


# ---------------------------------------------------------------------------
# 运行时三件套
# ---------------------------------------------------------------------------


class FakeTelemetry:
    """``TelemetryProvider`` 的替身。每一项都返回带 ``status`` 的字典（端口契约）。"""

    def __init__(
        self,
        *,
        gpus: list[dict[str, Any]] | None = None,
        gpu_status: str = "available",
        process_memory: dict[str, Any] | None = None,
        system_memory: dict[str, Any] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.gpus = gpus if gpus is not None else [{"index": 0, "temperature_c": 55.0}]
        self.gpu_status = gpu_status
        self.process_memory_data = process_memory or {}
        self.system_memory_data = system_memory or {}

    def gpu_stats(self) -> dict[str, Any]:
        self.calls.append("gpu_stats")
        # 探不到时**不返回 gpus**——真的 ``UnavailableTelemetryProvider`` 就是这样
        # （端口的口径：探不到报 unavailable，绝不用 0 顶替）。假件在这里多给一份
        # 数据，会让「探不到时硬件快照里不该有 GPU 数据」这条断言失去区分力。
        data = {"gpus": list(self.gpus)} if self.gpu_status == "available" else {}
        return {"status": self.gpu_status, "data": data, "source": "fake"}

    def process_memory(self) -> dict[str, Any]:
        self.calls.append("process_memory")
        return {"status": "available", "data": dict(self.process_memory_data)}

    def kv_cache(self) -> dict[str, Any]:
        self.calls.append("kv_cache")
        return {"status": "not_applicable", "data": {}}

    def system_memory(self) -> dict[str, Any]:
        self.calls.append("system_memory")
        return {"status": "available", "data": dict(self.system_memory_data)}


class FakeLifecycle:
    """``LifecycleProvider`` 的替身：起停都记账（本切片第一次真的调它们）。"""

    def __init__(
        self,
        *,
        state: dict[str, Any] | None = None,
        start_failures: int = 0,
        state_missing_ready: int = 0,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        # 默认就绪：``_wait_model_ready`` 只在「Manager 报的 profile/served_model_name
        # 与本轮要的模型对得上」时才算就绪，所以这份默认值得跟
        # ``test_run_execution._request()`` 的 ``model_profile`` 一致——
        # 不一致时那是一个**真实等待 600 秒**的循环，用例会挂住而不是失败。
        self.state_data = state if state is not None else {"profile": "qwen", "served_model_name": "qwen"}
        self.start_failures = start_failures
        self.state_missing_ready = state_missing_ready

    def profiles(self) -> dict[str, Any]:
        self.calls.append(("profiles", {}))
        return {"profiles": []}

    def start(self, payload: Mapping[str, Any], *, timeout: float = 120) -> dict[str, Any]:
        self.calls.append(("start", dict(payload)))
        if self.start_failures > 0:
            self.start_failures -= 1
            raise RuntimeError("502 bad gateway")
        return {"status": "starting"}

    def stop(self, payload: Mapping[str, Any] | None = None, *, timeout: float = 90) -> dict[str, Any]:
        self.calls.append(("stop", dict(payload or {})))
        return {"status": "stopped"}

    def state(self) -> dict[str, Any]:
        self.calls.append(("state", {}))
        if self.state_missing_ready > 0:
            self.state_missing_ready -= 1
            return {"profile": "other", "served_model_name": "other"}
        return dict(self.state_data)

    def call_names(self) -> list[str]:
        return [name for name, _ in self.calls]


class FakeInference:
    """``InferenceProvider`` 的替身。``chat_stream`` 给一段能算出 TTFT 的 SSE。"""

    def __init__(
        self,
        *,
        chat_error: Exception | None = None,
        stream_chunks: list[str] | None = None,
        slots: int | None = None,
        health_status: str = "ok",
    ) -> None:
        self.calls: list[str] = []
        self.chat_error = chat_error
        # 端点探活自报的状态。``"ok"`` 之外的任何值都表示「现在不值得开一轮 487 题」。
        self.health_status = health_status
        # 端点自报的并发槽位。``None`` = 问不到（llama.cpp 之外的端点、或端点没起来），
        # 那是 ``_resolve_qa_concurrency`` 退回 1 的那条路。
        self.slots = slots
        self.stream_chunks = (
            stream_chunks
            if stream_chunks is not None
            else [
                'data: {"choices":[{"delta":{"content":"你"}}]}',
                'data: {"choices":[{"delta":{"content":"好"}}]}',
                'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":2}}',
                "data: [DONE]",
            ]
        )

    def health(self) -> dict[str, Any]:
        self.calls.append("health")
        return {"status": self.health_status}

    def list_models(self) -> dict[str, Any]:
        self.calls.append("list_models")
        return {"models": ["m"]}

    def chat(self, payload: Mapping[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        self.calls.append("chat")
        if self.chat_error is not None:
            raise self.chat_error
        return {"choices": [{"message": {"content": "hi"}}]}

    def chat_stream(self, payload: Mapping[str, Any], *, timeout: float | None = None) -> Any:
        self.calls.append("chat_stream")
        if self.chat_error is not None:
            raise self.chat_error
        return iter(self.stream_chunks)

    def token_count(self, messages: Sequence[Mapping[str, Any]], *, timeout: float = 15) -> Any:
        self.calls.append("token_count")
        return None

    def capabilities(self) -> dict[str, Any]:
        self.calls.append("capabilities")
        return {"modes": ["generic"]}

    def max_concurrency(self) -> int | None:
        self.calls.append("max_concurrency")
        return self.slots


# ---------------------------------------------------------------------------
# 数据集与结果写入
# ---------------------------------------------------------------------------


class FakeAlbums:
    """``AlbumSource`` 的替身：album 内容全在内存里，不碰盘。"""

    def __init__(
        self,
        *,
        manifest: dict[str, Any],
        qa_rows: list[dict[str, Any]] | None = None,
        files: dict[str, bytes] | None = None,
        directory_entries: dict[str, int] | None = None,
        integrity: dict[str, Any] | None = None,
    ) -> None:
        self.manifest_data = manifest
        self.rows = qa_rows if qa_rows is not None else []
        self.files = files or {}
        self.directory_entries = directory_entries or {}
        self.calls: list[tuple[str, str]] = []
        #: S10a 加的两个展示层方法：album 名字表与「media 相对路径 -> 绝对路径」表。
        self.album_ids: list[str] = []
        self.media_files: dict[str, str] = {}
        self.integrity_data = integrity or {
            "algorithm": "sha256",
            "dataset_sha256": "deadbeef",
            "files_checked": 0,
            "missing_files": [],
        }

    def manifest(self, album_id: str) -> dict[str, Any]:
        self.calls.append(("manifest", album_id))
        return dict(self.manifest_data)

    def qa_rows(self, album_id: str, qa_set: str) -> list[dict[str, Any]]:
        self.calls.append(("qa_rows", qa_set))
        return [dict(row) for row in self.rows]

    def read_file(self, album_id: str, relative: str) -> bytes:
        self.calls.append(("read_file", relative))
        if relative not in self.files:
            raise KeyError(relative)
        return self.files[relative]

    def import_target(self, album_id: str, relative: str) -> tuple[str, int]:
        self.calls.append(("import_target", relative))
        return f"/srv/albums/{album_id}/{relative}".rstrip("/"), self.directory_entries.get(relative, 0)

    def integrity(self, album_id: str, manifest: dict[str, Any], qa_set: str) -> dict[str, Any]:
        self.calls.append(("integrity", qa_set))
        return dict(self.integrity_data)

    def list_album_ids(self) -> list[str]:
        self.calls.append(("list_album_ids", ""))
        return list(self.album_ids)

    def media_file(self, album_id: str, collection: str, file_name: str) -> str | None:
        self.calls.append(("media_file", f"{collection}/{file_name}"))
        return self.media_files.get(f"{album_id}/{collection}/{file_name}")


class FakeResultWriter:
    """``RunResultWriter`` 的替身：把每次快照留下来，答案是「落盘了吗、落了什么」。"""

    def __init__(self) -> None:
        self.snapshots: list[tuple[str, dict[str, Any]]] = []
        self.gpu_samples: list[tuple[str, dict[str, Any]]] = []
        self.resets: list[str] = []
        #: S9b-3 的写侧面：评审与删除。
        self.reviews: list[tuple[str, dict[str, Any]]] = []
        self.deleted: list[str] = []

    def write_snapshot(self, run_id: str, state: dict[str, Any]) -> None:
        self.snapshots.append((run_id, state))

    def reset_gpu_samples(self, run_id: str) -> None:
        self.resets.append(run_id)

    def append_gpu_sample(self, run_id: str, sample: dict[str, Any]) -> None:
        self.gpu_samples.append((run_id, sample))

    def save_reviews(self, run_id: str, stored: dict[str, Any]) -> None:
        self.reviews.append((run_id, stored))

    def delete_run(self, run_id: str) -> None:
        self.deleted.append(run_id)

    def last_state(self) -> dict[str, Any]:
        return self.snapshots[-1][1]

    def phase(self, name: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
        return (state or self.last_state()).get("phases", {}).get(name, {})


class FakeHttpClient:
    """``JsonHttpClient`` 的替身：按队列作答，队列耗尽即失败。

    **队列耗尽失败而不是回落默认值**：judge 的重试逻辑会让「第几次调用」变成断言
    对象，一个安静地重复上一条响应的假件会把「重试了几次」这件事藏起来。
    队列里放 ``Exception`` 就是让那一次调用抛它。
    """

    def __init__(self, responses: list[Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses = list(responses or [])

    def _next(self) -> Any:
        if not self.responses:
            raise AssertionError("FakeHttpClient 的响应队列空了——用例没有预设这一次调用")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def request_json(
        self,
        url: str,
        payload: Any = None,
        method: str = "GET",
        timeout: int = 120,
        extra_headers: Any = None,
    ) -> Any:
        self.calls.append(
            (
                "request_json",
                {
                    "url": url,
                    "payload": payload,
                    "method": method,
                    "timeout": timeout,
                    "headers": dict(extra_headers or {}),
                },
            )
        )
        return self._next()

    def request_bytes(self, url: str, timeout: int = 120) -> bytes:
        self.calls.append(("request_bytes", {"url": url, "timeout": timeout}))
        return self._next()

    def request_multipart(self, url: str, fields: Any, files: Any, timeout: int = 300) -> Any:
        self.calls.append(("request_multipart", {"url": url, "fields": dict(fields), "files": list(files)}))
        return self._next()

    def payloads(self) -> list[Any]:
        return [kwargs["payload"] for name, kwargs in self.calls if name == "request_json"]


def judge_reply(content: str) -> dict[str, Any]:
    """judge 端点的最小响应信封：``choices[0].message.content`` 里放那段 JSON。"""
    return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": content}}]}
