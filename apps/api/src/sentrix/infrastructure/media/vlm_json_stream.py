"""Optional SSE transport for VLM JSON, with bounded, content-free status evidence.

The timeout covers the entire generation POST, including connection and every
stream read. No output token limit or repetition-based cancellation is applied.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import io
import json
import logging
import os
import time
import uuid
from collections import Counter, deque
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from sentrix.domain.assistant.ports import ModelServiceError

log = logging.getLogger(__name__)
_STATUS_INTERVAL_S = 5.0
_REPEAT_WINDOW_CHARS = 16384
_FINISH_REASONS = {"stop", "length", "content_filter"}


def _request_identity(endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
    text_parts: list[dict[str, str]] = []
    image_hashes: list[dict[str, str]] = []
    for message in body.get("messages", []):
        for part in message.get("content", []):
            if part.get("type") == "text":
                text_parts.append({"role": message["role"], "text": part["text"]})
            elif part.get("type") == "image_url":
                url = part["image_url"]["url"]
                kind, value = "reference", url.encode()
                if url.startswith("data:") and ";base64," in url:
                    try:
                        value = base64.b64decode(url.split(",", 1)[1], validate=True)
                        kind = "content"
                    except (ValueError, binascii.Error):
                        pass
                image_hashes.append({"kind": kind, "sha256": hashlib.sha256(value).hexdigest()})
    parsed = urlsplit(endpoint)
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{host}:{parsed.port}" if parsed.port is not None else host
    return {
        "model": body.get("model"),
        "endpoint": urlunsplit((parsed.scheme, netloc, parsed.path, "", "")),
        "prompt_sha256": hashlib.sha256(
            json.dumps(text_parts, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest(),
        "prompt_hash_format": "ordered_role_text_json_v1",
        "images": image_hashes,
    }


class _ContentStats:
    def __init__(self) -> None:
        self.content = io.StringIO()
        self.chars = self.whitespace = self.longest_run = self._run = 0
        self._last = ""
        self._recent: deque[str] = deque()
        self._recent_chars = 0
        self._depth = 0
        self._inside = self._escaped = False
        self._start: int | None = None
        self.first_json_end: int | None = None
        self.first_json_s: float | None = None
        self.after_json_whitespace = 0

    def add(self, text: str, elapsed: float) -> None:
        offset = self.chars
        self.content.write(text)
        self._recent.append(text)
        self._recent_chars += len(text)
        while self._recent_chars > _REPEAT_WINDOW_CHARS:
            excess = self._recent_chars - _REPEAT_WINDOW_CHARS
            first = self._recent.popleft()
            if len(first) > excess:
                self._recent.appendleft(first[excess:])
                self._recent_chars -= excess
            else:
                self._recent_chars -= len(first)
        for index, char in enumerate(text, offset):
            self.chars += 1
            self.whitespace += int(char.isspace())
            self._run = self._run + 1 if char == self._last else 1
            self._last = char
            self.longest_run = max(self.longest_run, self._run)
            if self.first_json_end is not None:
                self.after_json_whitespace += int(char.isspace())
                continue
            if self._start is None:
                if char != "{":
                    continue
                self._start = index
            if self._inside:
                if self._escaped:
                    self._escaped = False
                elif char == "\\":
                    self._escaped = True
                elif char == '"':
                    self._inside = False
                continue
            if char == '"':
                self._inside = True
            elif char in "[{":
                self._depth += 1
            elif char in "]}":
                self._depth -= 1
                if self._depth == 0:
                    try:
                        value = json.loads(self.content.getvalue()[self._start : index + 1])
                    except ValueError:
                        continue
                    if isinstance(value, dict):
                        self.first_json_end = index + 1
                        self.first_json_s = elapsed

    def snapshot(self) -> dict[str, Any]:
        recent = "".join(self._recent)
        grams = Counter(recent[index : index + 12] for index in range(max(0, len(recent) - 11)))
        trailing = self.chars - self.first_json_end if self.first_json_end is not None else None
        return {
            "content_chars": self.chars,
            "whitespace_chars": self.whitespace,
            "whitespace_fraction": self.whitespace / max(1, self.chars),
            "first_complete_json_s": self.first_json_s,
            "characters_after_complete_json": trailing,
            "whitespace_after_complete_json": self.after_json_whitespace if trailing is not None else None,
            "nonwhitespace_after_complete_json": trailing - self.after_json_whitespace
            if trailing is not None
            else None,
            "longest_identical_character_run": self.longest_run,
            "repetition_window_chars": len(recent),
            "max_repeated_12gram_occurrences": max(grams.values(), default=0),
            "json_depth": self._depth,
            "inside_json_string": self._inside,
        }


class _Evidence:
    def __init__(
        self, directory: Path | None, private: bool, stage: str, identity: dict[str, Any], timeout_s: float
    ) -> None:
        self.request_id = f"sentrix-vlm-{uuid.uuid4().hex}"
        self.started = time.monotonic()
        self.stats = _ContentStats()
        self.reasoning = io.StringIO()
        self.first_event_s: float | None = None
        self.ttft_s: float | None = None
        self.finish: str | None = None
        self.usage: dict[str, int] | None = None
        self.stage = stage
        self.identity = identity
        self.private_content = private
        self.timeout_s = timeout_s
        self.state = "dispatched_outcome_unknown"
        self.error_code: str | None = None
        self.error_type: str | None = None
        self.protocol_error: str | None = None
        self.http_status: int | None = None
        self.io_failed = False
        self._status: io.TextIOWrapper | None = None
        self._private: io.TextIOWrapper | None = None
        if directory is not None:
            try:
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                directory.chmod(0o700)
                self._status = self._open(directory / f"{self.request_id}.jsonl")
                if private:
                    self._private = self._open(directory / f"{self.request_id}.content.jsonl")
            except OSError as error:
                self._io_error(error)

    @staticmethod
    def _open(path: Path) -> io.TextIOWrapper:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        target = os.fdopen(descriptor, "w", encoding="utf-8")
        try:
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            target.close()
            raise
        return target

    def _io_error(self, error: OSError) -> None:
        if not self.io_failed:
            log.error(
                "VLM evidence write failed request_id=%s error_type=%s", self.request_id, type(error).__name__
            )
        self.io_failed = True

    def snapshot(self) -> dict[str, Any]:
        return {
            **self.identity,
            "request_id": self.request_id,
            "private_content_enabled": self.private_content,
            "total_deadline_s": self.timeout_s,
            "deadline_scope": "generation_post",
            "status_interval_s": _STATUS_INTERVAL_S,
            "stage": self.stage,
            "state": self.state,
            "elapsed_s": time.monotonic() - self.started,
            "first_event_s": self.first_event_s,
            "ttft_s": self.ttft_s,
            "finish_reason": self.finish,
            "usage": self.usage,
            "reasoning_chars": self.reasoning.tell(),
            "error_code": self.error_code,
            "error_type": self.error_type,
            "stream_protocol_error": self.protocol_error,
            "http_status": self.http_status,
            "evidence_io_failed": self.io_failed,
            **self.stats.snapshot(),
        }

    def emit(self) -> None:
        value = self.snapshot()
        log.info("VLM JSON stream status %s", json.dumps(value, ensure_ascii=False))
        self._write(self._status, value)

    def _write(self, target: io.TextIOWrapper | None, value: dict[str, Any]) -> None:
        if target is None:
            return
        try:
            target.write(json.dumps(value, ensure_ascii=False) + "\n")
            target.flush()
            os.fsync(target.fileno())
        except OSError as error:
            self._io_error(error)

    def delta(self, content: str, reasoning: str) -> None:
        elapsed = time.monotonic() - self.started
        if (content or reasoning) and self.ttft_s is None:
            self.ttft_s = elapsed
        self.stats.add(content, elapsed)
        self.reasoning.write(reasoning)
        # Opt-in local diagnostic data only; never save prompts, images or provider error bodies.
        self._write(self._private, {"elapsed_s": elapsed, "content": content, "reasoning": reasoning})

    def close(self) -> None:
        for target in (self._status, self._private):
            if target is not None:
                try:
                    target.close()
                except OSError as error:
                    self._io_error(error)


def _protocol_error(reason: str, policy: dict[str, Any]) -> ModelServiceError:
    return ModelServiceError(
        "视觉模型流式响应未完整结束（provider_response_invalid）；未自动重试。",
        error_code="provider_response_invalid",
        request_policy={**policy, "stream_protocol_error": reason, "request_outcome_unknown": True},
    )


def _consume_event(data: str, event_name: str, evidence: _Evidence, policy: dict[str, Any]) -> bool:
    if event_name == "error":
        raise _protocol_error("stream_error", policy)
    if data == "[DONE]":
        if evidence.finish is None:
            raise _protocol_error("done_without_finish", policy)
        return True
    try:
        event = json.loads(data)
    except ValueError as error:
        raise _protocol_error("invalid_sse_json", policy) from error
    if not isinstance(event, dict) or event_name == "error" or "error" in event:
        raise _protocol_error("stream_error", policy)
    if evidence.first_event_s is None:
        evidence.first_event_s = time.monotonic() - evidence.started
    usage = event.get("usage")
    if isinstance(usage, dict):
        evidence.usage = {
            key: value
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            if type(value := usage.get(key)) is int and value >= 0
        }
    choices = event.get("choices")
    if not isinstance(choices, list):
        raise _protocol_error("missing_choices", policy)
    for choice in choices:
        if not isinstance(choice, dict) or choice.get("index", 0) != 0:
            raise _protocol_error("invalid_choice", policy)
        delta = choice.get("delta") or {}
        if not isinstance(delta, dict):
            raise _protocol_error("invalid_delta", policy)
        content = delta.get("content") or ""
        reasoning = delta.get("reasoning") or delta.get("reasoning_content") or ""
        if not isinstance(content, str) or not isinstance(reasoning, str):
            raise _protocol_error("invalid_content_type", policy)
        if evidence.finish is not None and (content or reasoning):
            raise _protocol_error("content_after_finish", policy)
        evidence.delta(content, reasoning)
        finish = choice.get("finish_reason")
        if finish is not None:
            if not isinstance(finish, str) or finish not in _FINISH_REASONS or evidence.finish is not None:
                raise _protocol_error("invalid_finish", policy)
            evidence.finish = finish
    return False


async def _stream(
    endpoint: str, body: dict[str, Any], timeout_s: float, evidence: _Evidence, policy: dict[str, Any]
) -> dict[str, Any]:
    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(_STATUS_INTERVAL_S)
            evidence.emit()

    task = asyncio.create_task(heartbeat())
    try:
        async with asyncio.timeout(max(0.0, timeout_s - (time.monotonic() - evidence.started))):
            async with (
                httpx.AsyncClient(timeout=timeout_s) as client,
                client.stream(
                    "POST",
                    endpoint,
                    json={**body, "stream": True, "stream_options": {"include_usage": True}},
                    headers={"X-Request-Id": evidence.request_id},
                ) as response,
            ):
                evidence.http_status = response.status_code
                response.raise_for_status()
                data: list[str] = []
                event_name = ""
                done = False
                async for line in response.aiter_lines():
                    if time.monotonic() - evidence.started >= timeout_s:
                        raise TimeoutError("generation total deadline exceeded")
                    if not line:
                        if data:
                            done = _consume_event("\n".join(data), event_name, evidence, policy)
                            data = []
                        event_name = ""
                        if done:
                            break
                    elif line.startswith("data:"):
                        data.append(line[5:].lstrip(" "))
                    elif line.startswith("event:"):
                        event_name = line[6:].strip()
                if data and not done:
                    done = _consume_event("\n".join(data), event_name, evidence, policy)
                if not done or evidence.finish is None:
                    raise _protocol_error("premature_eof", policy)
        if evidence.finish == "content_filter":
            raise ModelServiceError(
                "视觉模型服务拒绝返回内容（provider_rejected，content_filter）；未自动重试。",
                error_code="provider_rejected",
                http_status=200,
                finish_reason="content_filter",
                request_policy={**policy, "request_outcome_unknown": False},
            )
        if evidence.finish == "length":
            raise ModelServiceError(
                "视觉模型达到本次可用输出/上下文上限（output_limit），回复未完成。",
                error_code="output_limit",
                finish_reason="length",
                http_status=200,
                request_policy={**policy, "request_outcome_unknown": False},
                prompt_tokens=(evidence.usage or {}).get("prompt_tokens"),
                completion_tokens=(evidence.usage or {}).get("completion_tokens"),
            )
        return {
            "choices": [
                {
                    "message": {
                        "content": evidence.stats.content.getvalue(),
                        "reasoning": evidence.reasoning.getvalue(),
                    },
                    "finish_reason": evidence.finish,
                }
            ],
            "usage": evidence.usage,
        }
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _run(coroutine: Coroutine[Any, Any, dict[str, Any]]) -> dict[str, Any]:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # Normal production path: synchronous API worker, no extra transport thread.
        return asyncio.run(coroutine)
    # A caller already inside an event loop cannot nest asyncio.run(). Only that
    # exceptional path needs one temporary thread; it is joined before returning.
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="vlm-json-stream") as pool:
        return pool.submit(asyncio.run, coroutine).result()


def stream_json_response(
    endpoint: str,
    body: dict[str, Any],
    timeout_s: float,
    *,
    evidence_dir: Path | None,
    private_content: bool,
    stage: str,
    request_policy: dict[str, Any],
) -> dict[str, Any]:
    evidence = _Evidence(evidence_dir, private_content, stage, _request_identity(endpoint, body), timeout_s)
    policy = {
        **request_policy,
        "request_id": evidence.request_id,
        "transport": "sse",
        "total_deadline_s": timeout_s,
        "deadline_scope": "generation_post",
    }
    evidence.emit()
    try:
        payload = _run(_stream(endpoint, body, timeout_s, evidence, policy))
        evidence.state = "completed"
        return payload
    except (TimeoutError, httpx.TimeoutException) as error:
        evidence.state, evidence.error_code = "unknown", "time_limit"
        evidence.error_type = type(error).__name__
        raise ModelServiceError(
            "视觉模型请求达到总时限（time_limit）；连接已关闭，未自动重试。",
            error_code="time_limit",
            request_policy={**policy, "request_outcome_unknown": not isinstance(error, httpx.ConnectTimeout)},
        ) from error
    except httpx.HTTPStatusError as error:
        evidence.state, evidence.error_code = "http_failed", "provider_rejected"
        evidence.error_type = type(error).__name__
        raise ModelServiceError(
            f"视觉模型请求被服务拒绝（provider_rejected，HTTP {error.response.status_code}）。",
            error_code="provider_rejected",
            http_status=error.response.status_code,
            request_policy={**policy, "request_outcome_unknown": False},
        ) from error
    except httpx.HTTPError as error:
        evidence.state, evidence.error_code = "unknown", "provider_unavailable"
        evidence.error_type = type(error).__name__
        raise ModelServiceError(
            "视觉模型流式连接未能完成（provider_unavailable）；未自动重试。",
            error_code="provider_unavailable",
            request_policy={**policy, "request_outcome_unknown": not isinstance(error, httpx.ConnectError)},
        ) from error
    except ModelServiceError as error:
        evidence.state = "unknown" if error.request_policy.get("request_outcome_unknown") else "failed"
        evidence.error_code = error.error_code
        evidence.error_type = type(error).__name__
        evidence.protocol_error = error.request_policy.get("stream_protocol_error")
        raise
    finally:
        evidence.emit()
        evidence.close()
