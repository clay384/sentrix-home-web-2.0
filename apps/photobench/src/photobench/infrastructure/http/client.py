"""``JsonHttpClient`` 的 httpx 实现：1.0 ``request_json`` / ``request_bytes`` 的替代。

**行为对照表（逐条核对 1.0，``:731`` / ``:799``）：**

===============================  =======================================================
1.0 ``request_json``             2.0
===============================  =======================================================
``json.dumps(payload, ensure_ascii=False)`` 同（字节体、UTF-8 原样）
``Content-Type: application/json``          同（``extra_headers`` 可覆盖）
``Content-Length`` 显式写            httpx 自动写，值相同
``timeout`` 按调用传                 同
非 2xx → ``RuntimeError("HTTP {code} {reason}: {detail}")``  同（同一段 detail 提取逻辑）
连接失败 / 超时 → 原样抛              原样抛（类型变成 httpx 的，见下）
重试                                 无（1.0 没有）
===============================  =======================================================

**三处必须说清楚的差异：**

1. **TLS 校验关掉。** 1.0 建的是 ``ssl.create_default_context()`` 然后把
   ``check_hostname`` 与 ``verify_mode`` 都改掉（``:742-743``）——即**不校验证书**。
   内网自签证书的部署依赖这一点，所以 2.0 用 ``verify=False`` 照搬。
2. **异常类型换了名字。** ``urllib.error.HTTPError`` → ``httpx.HTTPStatusError``；
   连接失败从 ``URLError`` → ``httpx.ConnectError``。**「非 2xx 转成同一条
   RuntimeError 文案」这条照搬**（那是 1.0 唯一显式约定的失败形状）；
   其余失败两边都是「抛异常」，只是类型不同。本切片唯一的调用方
   （``_hydrate_legacy_gt_images``）用的是 ``except Exception``，两边等价。
3. **跟随重定向。** ``urllib.request`` 默认跟随 301/302，而 ``httpx.Client`` 默认
   **不**跟随。这里显式开成 ``follow_redirects=True``——否则一个被重定向的端点
   在 1.0 能通、在 2.0 会变成 302 错误体。
"""

from __future__ import annotations

import contextlib
import json
import math
import mimetypes
import re
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC
from email.utils import format_datetime, parsedate_to_datetime
from typing import Any

import httpx

from photobench.domain.errors import RequestOutcomeUnknownError, SharedJudgeGateError
from photobench.domain.http.ports import HttpAttemptObserver, HttpStream
from photobench.domain.runs.execution import RunCancelledError
from photobench.infrastructure.http.shared_gate import SharedAccountGate

_RETRYABLE = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
)
_RETRIES = 3
_RETRY_BACKOFF_S = 0.2


_RATE_HEADERS = (
    "x-ratelimit-limit-requests",
    "x-ratelimit-remaining-requests",
    "x-ratelimit-reset-requests",
    "x-ratelimit-limit-tokens",
    "x-ratelimit-remaining-tokens",
    "x-ratelimit-reset-tokens",
    "x-ratelimit-limit-rpm",
    "x-ratelimit-limit-tpm",
    "ratelimit-limit",
    "ratelimit-remaining",
    "ratelimit-reset",
)


def _safe_response_evidence(
    response: httpx.Response, content: bytes | None, headers: Mapping[str, str] | None
) -> dict[str, Any]:
    """Identifiers/numbers only: never exception prose, request/response bodies or credentials."""
    result: dict[str, Any] = {}
    sensitive = [(content or b"").decode(errors="replace").casefold()]
    sensitive.extend(str(v).casefold() for v in (headers or {}).values())

    def identifier(value: Any) -> str | None:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,95}", value):
            return None
        # A provider may echo a private identifier from the request into error.code/type.
        if any(value.casefold() in text for text in sensitive):
            return None
        return value

    def number(value: str) -> float | None:
        if not re.fullmatch(r"[0-9]{1,16}(?:\.[0-9]{1,6})?", value):
            return None
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None

    retry_after = response.headers.get("Retry-After", "").strip()
    if retry_after:
        seconds = number(retry_after)
        if seconds is not None:
            result["retry_after"] = seconds
        elif len(retry_after) <= 40:
            try:
                date = parsedate_to_datetime(retry_after)
                if date.tzinfo is not None:
                    result["retry_after"] = format_datetime(date.astimezone(UTC), usegmt=True)
            except (TypeError, ValueError, OverflowError):
                pass
    rate_headers = {}
    for name in _RATE_HEADERS:
        raw = response.headers.get(name, "").strip()
        value = number(raw)
        if value is not None:
            rate_headers[name] = value
    if rate_headers:
        result["rate_limit_headers"] = rate_headers
    if response.status_code >= 400 and len(response.content) <= 65536:
        try:
            parsed = response.json()
            error = parsed.get("error") if isinstance(parsed, dict) else None
            if isinstance(error, dict):
                for key in ("code", "type"):
                    identifier_value = identifier(error.get(key))
                    if identifier_value is not None:
                        result["provider_error_" + key] = identifier_value
        except (ValueError, UnicodeDecodeError):
            pass
    return result


def _notify_attempt(observer: HttpAttemptObserver | None, evidence: Mapping[str, Any]) -> None:
    if observer is not None:
        # Diagnostics cannot turn a successful HTTP call into an error or trigger a retry.
        with contextlib.suppress(Exception):
            observer(dict(evidence))


def build_multipart(fields: Mapping[str, str], files: Sequence[tuple[str, str, bytes]]) -> tuple[bytes, str]:
    """1.0 ``build_multipart``（``:864``）逐行照搬。

    保留手工拼接而不是交给 httpx 的 ``files=``，理由是**可差分**：这条函数的输出
    （边界串的构造、字段在文件之前、``guessed mime or application/octet-stream``）
    能与 1.0 的同一函数逐字节比对，而 httpx 的编码器给不出同一份字节。
    边界串的前缀也照搬 1.0（``----BenchmarkOrch``），便于在服务端日志里认出是谁发的。
    """
    boundary = f"----BenchmarkOrch{uuid.uuid4().hex[:16]}"
    parts = []
    for name, value in fields.items():
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        parts.append(f"{value}\r\n".encode())
    for name, filename, content in files:
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode())
        parts.append(f"Content-Type: {mime}\r\n\r\n".encode())
        parts.append(content)
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    return body, f"multipart/form-data; boundary={boundary}"


def _error_detail(response: httpx.Response) -> str:
    """1.0 ``:745-760``：从错误体里掏出给人看的 detail。"""
    raw = response.text[:2000]
    detail: Any = raw.strip()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            detail = parsed.get("detail") or parsed.get("error") or parsed.get("message") or parsed
        else:
            detail = parsed
    except (json.JSONDecodeError, TypeError):
        pass
    if isinstance(detail, (dict, list)):
        detail = json.dumps(detail, ensure_ascii=False)
    return str(detail or response.reason_phrase or "request failed")[:1000]


class HttpxJsonClient:
    """``JsonHttpClient`` 的实现。

    ``client`` 可注入是为了**用例不碰网络**：测试传一个
    ``httpx.Client(transport=httpx.MockTransport(handler))`` 就能把状态码、超时、
    错误体逐条摆出来。默认的那个是进程级的——httpx 复用连接，而 1.0 的
    ``urlopen`` 每次新建连接；这不改变任何返回值，只是少几次握手。
    """

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        retry_connections: bool = True,
        keep_alive: bool = False,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        shared_gate: SharedAccountGate | None = None,
    ) -> None:
        # **默认不保留 keep-alive 连接。** 2026-10-21 的 158 全流程里 131/487 题以
        # ``Server disconnected without sending a response`` / ``Bad file descriptor``
        # 失败：进程级 ``httpx.Client`` 复用了 uvicorn 已关闭的连接（默认 keep-alive 5s），
        # 而长回合让连接大段空闲。本机回环上每次重新握手几乎无成本，换掉这个失败面。
        # 调用方仍可显式传 keep_alive=True 复用连接。
        self._client = (
            client
            if client is not None
            else httpx.Client(
                verify=False,
                follow_redirects=True,
                timeout=120,
                limits=httpx.Limits(
                    max_connections=32,
                    max_keepalive_connections=20 if keep_alive else 0,
                    keepalive_expiry=5.0 if keep_alive else 0.0,
                ),
            )
        )
        self._retry_connections = retry_connections
        self._sleep = sleep or time.sleep
        self._clock = clock
        self._shared_gate = shared_gate
        self.shared_gate_enabled = shared_gate is not None

    def _request(
        self,
        method: str,
        url: str,
        *,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: int = 120,
        attempt_observer: HttpAttemptObserver | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> httpx.Response:
        """Only connection establishment failures are safe to retry for POST."""
        deadline = self._clock() + timeout
        for attempt in range(_RETRIES):
            lease = None
            lease_finished = False
            if self._shared_gate is not None:
                waiting_started = time.perf_counter()
                wait_budget_started = self._clock()
                _notify_attempt(attempt_observer, {"event": "gate_wait_started"})
                try:
                    if method.upper() != "POST":
                        raise SharedJudgeGateError("only_Judge_POST_supported")
                    lease = self._shared_gate.acquire(
                        url, (headers or {}).get("Authorization", ""), cancelled=cancelled or (lambda: False)
                    )
                except Exception as exc:
                    _notify_attempt(
                        attempt_observer,
                        {
                            "event": "gate_wait_finished",
                            "gate_wait_ms": (time.perf_counter() - waiting_started) * 1000,
                            "gate_error": type(exc).__name__,
                        },
                    )
                    raise
                deadline += self._clock() - wait_budget_started
                _notify_attempt(
                    attempt_observer,
                    {
                        "event": "gate_wait_finished",
                        "gate_wait_ms": (time.perf_counter() - waiting_started) * 1000,
                    },
                )
                if cancelled is not None and cancelled():
                    self._shared_gate.finish(lease["lease_id"], "not_sent")
                    raise RunCancelledError("cancelled after shared Judge permit before HTTP")
            budget = timeout if attempt == 0 else deadline - self._clock()
            evidence: dict[str, Any] = (
                {
                    "transport_attempt": attempt + 1,
                    "started_at": time.time(),
                    "started_monotonic": self._clock(),
                    "finished_at": None,
                    "http_status": None,
                    "exception_class": None,
                }
                if attempt_observer is not None
                else {}
            )
            if lease is not None:
                evidence.update(shared_gate=True, dispatch_started=True, gate_lease_id=lease["lease_id"])
            _notify_attempt(attempt_observer, evidence)
            try:
                if self.shared_gate_enabled:
                    # A redirect must not issue hidden HTTP requests outside the shared permits.
                    response = self._client.request(
                        method, url, content=content, headers=headers, timeout=budget, follow_redirects=False
                    )
                else:
                    response = self._client.request(
                        method, url, content=content, headers=headers, timeout=budget
                    )
                if attempt_observer is not None:
                    evidence.update(
                        finished_at=time.time(),
                        elapsed_ms=(self._clock() - evidence["started_monotonic"]) * 1000,
                        http_status=response.status_code,
                        exception_class="HTTPStatusError" if response.is_error else None,
                    )
                    try:
                        evidence.update(_safe_response_evidence(response, content, headers))
                    except Exception:
                        evidence["metadata_unavailable"] = True
                    _notify_attempt(attempt_observer, evidence)
                if self._shared_gate is not None and lease is not None:
                    lease_finished = True
                    redirect = 300 <= response.status_code < 400
                    self._shared_gate.finish(
                        lease["lease_id"],
                        "configuration_error" if redirect else "http_response",
                        http_status=response.status_code,
                    )
                    if redirect:
                        raise SharedJudgeGateError("redirect_requires_explicit_endpoint")
                response.extensions["photobench_transport_attempt"] = attempt + 1
                return response
            except _RETRYABLE as exc:
                if attempt_observer is not None:
                    evidence.update(
                        finished_at=time.time(),
                        elapsed_ms=(self._clock() - evidence["started_monotonic"]) * 1000,
                        exception_class=type(exc).__name__,
                    )
                    _notify_attempt(attempt_observer, evidence)
                safe = method.upper() in {"GET", "HEAD"} or isinstance(
                    exc, (httpx.ConnectError, httpx.ConnectTimeout)
                )
                if self._shared_gate is not None and lease is not None:
                    lease_finished = True
                    self._shared_gate.finish(lease["lease_id"], "not_sent" if safe else "unknown")
                if (self._retry_connections or self.shared_gate_enabled) and not safe:
                    raise RequestOutcomeUnknownError(
                        method=method, cause_type=type(exc).__name__, attempts=attempt + 1
                    ) from exc
                remaining = deadline - self._clock()
                if (
                    not self._retry_connections
                    or not safe
                    or attempt == _RETRIES - 1
                    or remaining <= _RETRY_BACKOFF_S * (attempt + 1)
                ):
                    raise
                if attempt_observer is not None:
                    evidence.update(
                        transport_retry=True, transport_backoff_seconds=_RETRY_BACKOFF_S * (attempt + 1)
                    )
                    _notify_attempt(attempt_observer, evidence)
                self._sleep(_RETRY_BACKOFF_S * (attempt + 1))
            except Exception as exc:
                if self._shared_gate is not None and lease is not None and not lease_finished:
                    lease_finished = True
                    self._shared_gate.finish(lease["lease_id"], "unknown")
                if isinstance(exc, SharedJudgeGateError):
                    _notify_attempt(
                        attempt_observer, {"event": "gate_error", "gate_error": type(exc).__name__}
                    )
                if attempt_observer is not None:
                    evidence.update(
                        finished_at=time.time(),
                        elapsed_ms=(self._clock() - evidence["started_monotonic"]) * 1000,
                        exception_class=type(exc).__name__,
                    )
                    _notify_attempt(attempt_observer, evidence)
                raise
        raise AssertionError("bounded retry exhausted")

    def request_json(
        self,
        url: str,
        payload: Mapping[str, Any] | None = None,
        method: str = "GET",
        timeout: int = 120,
        extra_headers: Mapping[str, str] | None = None,
    ) -> Any:
        return self._request_json(url, payload, method, timeout, extra_headers, None, None)

    def request_json_observed(
        self,
        url: str,
        payload: Mapping[str, Any] | None = None,
        method: str = "GET",
        timeout: int = 120,
        extra_headers: Mapping[str, str] | None = None,
        *,
        observer: HttpAttemptObserver,
        cancelled: Callable[[], bool] | None = None,
    ) -> Any:
        return self._request_json(url, payload, method, timeout, extra_headers, observer, cancelled)

    def _request_json(
        self,
        url: str,
        payload: Mapping[str, Any] | None,
        method: str,
        timeout: int,
        extra_headers: Mapping[str, str] | None,
        observer: HttpAttemptObserver | None,
        cancelled: Callable[[], bool] | None,
    ) -> Any:
        content = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        headers = {"Content-Type": "application/json"}
        headers.update(extra_headers or {})
        # 连接错误重试统一由 _request 处理；POST 的不确定结果由 RequestOutcomeUnknownError 表达。
        try:
            response = self._request(
                method,
                url,
                content=content,
                headers=headers,
                timeout=timeout,
                attempt_observer=observer,
                cancelled=cancelled,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            response = exc.response
            raise RuntimeError(
                f"HTTP {response.status_code} {response.reason_phrase}: {_error_detail(response)}"
            ) from exc
        try:
            value = response.json()
        except (ValueError, UnicodeDecodeError):
            if self.shared_gate_enabled:
                _notify_attempt(observer, {"event": "json_invalid"})
            raise
        if self.shared_gate_enabled:
            _notify_attempt(observer, {"event": "json_valid"})
        return value

    def request_bytes(self, url: str, timeout: int = 120) -> bytes:
        """1.0 ``:799``。**非 2xx 不做文案转换**——1.0 这一段没有 except，``HTTPError``
        原样冒出去；这里保持同一分工，让 ``raise_for_status`` 的异常原样冒。"""
        response = self._request("GET", url, timeout=timeout)
        response.raise_for_status()
        return response.content

    def request_multipart(
        self,
        url: str,
        fields: Mapping[str, str],
        files: Sequence[tuple[str, str, bytes]],
        timeout: int = 300,
    ) -> Any:
        """1.0 ``upload_files``（``:884``）。

        **非 2xx 的结局与 1.0 不同，这里刻意选了 ``request_json`` 那一支**：1.0 的
        ``upload_files`` 没有 except，``HTTPError`` 原样冒（那句 ``str(exc)`` 会被
        ``_phase_photo_import`` 写进失败明细）。而 S9b-2 的调用方是 ``seed_person``，
        它要的是**能读懂的原因**（2.0 的 ``400 {"detail": "..."}`` 说了是哪一步拒绝了）
        ——所以这里转成与 ``request_json`` 同一条 RuntimeError 文案。这条差异登记在
        行为基线里：**1.0 的两个上传端点（``seed-batch`` / ``import``）在 2.0 都不再由
        这个方法服务**（前者是单人 seed、后者是服务端目录导入），所以不会改变任何
        1.0 已有失败语义的观测。
        """
        body, content_type = build_multipart(fields, files)
        try:
            response = self._request(
                "POST", url, content=body, headers={"Content-Type": content_type}, timeout=timeout
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            response = exc.response
            raise RuntimeError(
                f"HTTP {response.status_code} {response.reason_phrase}: {_error_detail(response)}"
            ) from exc
        return response.json()

    def stream_get(
        self, url: str, headers: Mapping[str, str] | None = None, timeout: int = 120
    ) -> HttpStream:
        """1.0 ``_proxy_sentrix_asset`` 的 ``urlopen`` + ``shutil.copyfileobj``（``:7992``）。

        **不用 ``client.stream(...)`` 的上下文管理器**：那要求 body 在本方法里读完，
        而这里要把迭代器交出去（HTTP 层的 ``StreamingResponse`` 边发边读）。所以手工
        ``build_request`` + ``send(stream=True)``，再把关闭挂在两处——body 生成器的
        ``finally``（调用方提前断开时走这条）与 ``close`` 本身（正常发完之后走那条）。

        **不 ``raise_for_status``**：代理要把上游的状态码与头原样转出去（1.0 的
        ``except urllib.error.HTTPError`` 那一条只换 body）。连接失败照旧抛——那种
        失败没有状态码可转，1.0 也让它冒到外层变成 400。
        """
        request = self._client.build_request("GET", url, headers=dict(headers or {}), timeout=timeout)
        response = self._client.send(request, stream=True)

        def body() -> Iterator[bytes]:
            try:
                yield from response.iter_bytes()
            finally:
                response.close()

        # 头名统一成小写：httpx 本来就给小写，但用例的假传输可能给别的写法，
        # 而 HTTP 层要按名字挑那几个头，大小写不一致就会**静默漏转发**。
        return HttpStream(
            status_code=response.status_code,
            headers={name.lower(): value for name, value in response.headers.items()},
            body=body(),
            close=response.close,
        )
