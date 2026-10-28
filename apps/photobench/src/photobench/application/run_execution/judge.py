"""Judge 打分：1.0 ``BenchmarkRun`` 上那组 ``_judge*`` 方法的 2.0 重建。

**逐项对应**：``_judge``（``:4238``，回答质量）/ ``_judge_task_action``（``:4317``，
任务行为）/ ``_judge_evidence``（``:4373``，图片证据）/ ``_judge_request``（``:4433``，
带退避与限流的重试）/ ``_wait_for_judge_request_slot``（``:4467``）/ ``_judge_headers``
（``:4527``）/ ``_judge_item``（``:3588``，一个 item 的编排）/ ``_inline_judge_images``
（``:1549``，2.0 把它挪到 infrastructure，见 ``InlineImages``）。

**为什么单开一个文件而不是塞进 ``service.py``。** 打分与「跑一轮评测」是两件事：
前者只依赖一个 HTTP 客户端与几个提示词常量，后者依赖主 API 客户端、数据集、写入器。
分开之后打分这一层能在没有 run、没有数据集、没有主 API 的用例里被逐条钉住——而
1.0 把它们全挂在 ``BenchmarkRun`` 上，只能靠起一整轮评测才能碰。

**它是 ``JsonHttpClient`` 的第二个生产调用者**（第一个是读侧的 GT 图片补全）。judge
端点不是主 API，但两者都是「发一段 JSON、取回一段 JSON」，1.0 用的也是同一个
``request_json``——这里沿用同一条，不新开端口。

**取消感知**：1.0 的两处等待（重试退避、限流）都走 ``self._cancel.wait(...)``，被取消
时抛 ``RunCancelledError``。S9b-2 如实记过「等待不是取消感知的」，这里接上——退避最长
60 秒，一次取消要等它跑完才生效是不可接受的。
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import copy
import json
import os
import random
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from photobench.domain.errors import RequestOutcomeUnknownError, SharedJudgeGateError
from photobench.domain.http.ports import JsonHttpClient, ObservedJsonHttpClient, SharedGateHttpClient
from photobench.domain.runs.analysis import (
    JUDGE_CONSISTENCY_VERSION,
    judge_score_consistency,
    now_iso,
)
from photobench.domain.runs.execution import RunCancelledError
from photobench.domain.runs.judge import (
    ANSWER_QUALITY_RUBRICS,
    EVIDENCE_JUDGE_PROMPT,
    JUDGE_PROMPT,
    JUDGE_SCORE_NORMALIZATION_VERSION,
    TASK_JUDGE_PROMPT,
    execution_failure,
    judge_chat_url,
    normalize_judge_score,
    parse_judge_json,
    thinking_kwargs,
)
from photobench.domain.runs.operational import (
    exclude_operational_quality,
    judge_format_issue,
    operational_issue,
)

#: 1.0 的四个模块级常量（``:206-229``），同样由环境变量覆盖。
JUDGE_HTTP_ATTEMPT_EVIDENCE_ENABLED = os.environ.get("PHOTOBENCH_JUDGE_HTTP_ATTEMPT_EVIDENCE", "0") == "1"
JUDGE_RETRY_ATTEMPTS = max(1, int(os.environ.get("PHOTOBENCH_JUDGE_RETRY_ATTEMPTS", "6")))
JUDGE_RETRY_BACKOFF_SECONDS = max(0.1, float(os.environ.get("PHOTOBENCH_JUDGE_RETRY_BACKOFF_SECONDS", "5.0")))
JUDGE_RETRY_BACKOFF_MAX_SECONDS = max(
    JUDGE_RETRY_BACKOFF_SECONDS,
    float(os.environ.get("PHOTOBENCH_JUDGE_RETRY_BACKOFF_MAX_SECONDS", "60.0")),
)
JUDGE_REQUEST_INTERVAL_SECONDS = max(
    0.0, float(os.environ.get("PHOTOBENCH_JUDGE_REQUEST_INTERVAL_SECONDS", "0.5"))
)

#: 把一个 item 的召回图取回并转成 data URL 的可注入实现。**它由装配点提供**
#: （``infrastructure/http/judge_images.py``）：那段代码要用 PIL，而
#: ``photobench-application-purity`` 的 forbidden 名单里有 PIL——**惰性 import 也照样
#: 判红**（import-linter 看 AST，不看运行时机）。与 ``httpx`` 的处理同一口径。
InlineImages = Callable[
    [str, Sequence[Mapping[str, Any]], Mapping[str, list[dict[str, Any]]]], list[dict[str, Any]]
]


def _judge_response_problem(raw: Any, payload: Mapping[str, Any]) -> dict[str, Any] | None:
    choices = raw.get("choices") if isinstance(raw, Mapping) else None
    choice = choices[0] if isinstance(choices, list) and len(choices) == 1 else None
    message = choice.get("message") if isinstance(choice, Mapping) else None
    finish_reason = choice.get("finish_reason") if isinstance(choice, Mapping) else None
    content = message.get("content") if isinstance(message, Mapping) else None
    if finish_reason in ("length", "content_filter"):
        code = kind = "output_limit" if finish_reason == "length" else "provider_rejected"
        field, value, unknown = "finish_reason", finish_reason, False
    elif (
        not isinstance(message, Mapping)
        or not isinstance(content, str)
        or finish_reason != "stop"
        or message.get("tool_calls")
        or message.get("function_call")
    ):
        code, kind = "provider_response_invalid", "provider_rejected"
        field, value, unknown = "response_contract", "invalid_grading_envelope", True
    else:
        return None
    # Valid-looking JSON cannot prove completion of an invalid/truncated protocol.
    return {
        "score": None,
        "correct": None,
        "actual_action": None,
        "judge_status": "outcome_unknown" if unknown else "failed",
        "reason": "judge_" + code,
        "operational_issue": {
            "version": 1,
            "scope": "judge",
            "kind": kind,
            "evidence": {
                "source": "judge_response",
                "field": field,
                "value": value,
                "error_code": code,
                "request_outcome_unknown": unknown,
                "max_tokens": payload.get("max_tokens"),
            },
        },
        "input": dict(payload),
        "raw_response": raw,
        "raw_text": content if isinstance(content, str) else "",
        "attempts": [{"input": dict(payload), "raw_response": raw}],
    }


def _judge_request_failure(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, SharedJudgeGateError):
        return {
            "request_failure": exc.facts,
            "operational_issue": {
                "version": 1,
                "scope": "judge",
                "kind": "configuration_error",
                "evidence": {
                    "source": "shared_judge_gate",
                    "field": "gate",
                    "error_code": exc.facts["code"],
                    "request_outcome_unknown": False,
                },
            },
        }
    if not isinstance(exc, RequestOutcomeUnknownError):
        return {}
    result: dict[str, Any] = {"request_failure": exc.facts}
    if exc.facts.get("error_code") == "provider_response_invalid":
        result["operational_issue"] = {
            "version": 1,
            "scope": "judge",
            "kind": "provider_rejected",
            "evidence": {
                "source": "judge_response",
                "field": "response_contract",
                "value": "invalid_json_envelope",
                "error_code": "provider_response_invalid",
                "request_outcome_unknown": True,
            },
        }
    return result


def _invalid_judge_output(
    raw: Mapping[str, Any], payload: Mapping[str, Any], text: str, judge_kind: str
) -> dict[str, Any] | None:
    if issue := judge_format_issue(text, judge_kind):
        return {
            "score": None,
            "correct": None,
            "actual_action": None,
            "judge_status": "failed",
            "reason": "judge_model_output_invalid",
            "operational_issue": issue,
            "input": dict(payload),
            "raw_response": dict(raw),
            "raw_text": text,
            "attempts": [{"input": dict(payload), "raw_response": dict(raw), "raw_text": text}],
        }
    return None


class JudgeRunner:
    """一次 run 的 judge 配置 + 调用。与 ``RunExecution`` 一样是**按 run 构造**的。"""

    def __init__(
        self,
        *,
        http: JsonHttpClient,
        judge_url: str,
        judge_model: str,
        judge_api_key: str = "",
        answer_quality_prompt: str | None = None,
        task_judge_prompt: str | None = None,
        evidence_judge_prompt: str | None = None,
        evidence_enabled: bool = False,
        sentrix_url: str = "",
        inline_images: InlineImages | None = None,
        cancel: threading.Event | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rand: Callable[[float, float], float] = random.uniform,
        http_attempt_evidence: bool | None = None,
    ) -> None:
        self.http = http
        self.judge_url = str(judge_url or "").rstrip("/")
        self.judge_model = str(judge_model or "")
        self.judge_api_key = str(judge_api_key or "")
        self.answer_quality_prompt = answer_quality_prompt or JUDGE_PROMPT
        self.task_judge_prompt = task_judge_prompt or TASK_JUDGE_PROMPT
        self.evidence_judge_prompt = evidence_judge_prompt or EVIDENCE_JUDGE_PROMPT
        self.evidence_enabled = bool(evidence_enabled)
        self.sentrix_url = str(sentrix_url or "")
        self.inline_images = inline_images
        #: 取消信号。**没有它时退化成 ``sleep``**——但主链的 judge 永远拿得到
        #: 一个 Event（``RunState.cancel_event``），所以这条退化分支只服务用例。
        self.cancel = cancel
        self.sleep = sleep
        self.rand = rand
        self.http_attempt_evidence = (
            JUDGE_HTTP_ATTEMPT_EVIDENCE_ENABLED if http_attempt_evidence is None else http_attempt_evidence
        )
        self._http_attempts: list[dict[str, Any]] = []
        self._http_request_groups = 0
        self.shared_gate_enabled = isinstance(http, SharedGateHttpClient) and http.shared_gate_enabled
        if self.shared_gate_enabled and not isinstance(http, ObservedJsonHttpClient):
            raise SharedJudgeGateError("observed_transport_required")
        #: 限流锁与下一次可发时刻。惰性创建（1.0 也是第一次用到才建）。
        self._rate_lock = threading.Lock()
        self._next_request_at = 0.0
        self._metrics_lock = threading.Lock()
        self._request_metrics: dict[str, int | float] = {
            "version": 1,
            "requests_started": 0,
            "responses_received": 0,
            "request_errors": 0,
            "http_429": 0,
            "in_flight": 0,
            "rate_waiting": 0,
            "rate_wait_ms": 0.0,
            "gate_waiting": 0,
            "gate_wait_ms": 0.0,
            "gate_failures": 0,
        }

    def request_metrics(self) -> dict[str, Any]:
        """Keep safe failed-attempt evidence after success, alongside original scalar counters."""
        with self._metrics_lock:
            result: dict[str, Any] = dict(self._request_metrics)
            result["http_attempt_evidence_enabled"] = self.http_attempt_evidence
            result["shared_gate_enabled"] = self.shared_gate_enabled
            if self.http_attempt_evidence:
                result["http_attempt_evidence_supported"] = isinstance(self.http, ObservedJsonHttpClient)
                result["http_attempts"] = copy.deepcopy(self._http_attempts)
            return result

    def _request_metric(self, key: str, amount: int | float = 1) -> int | float:
        with self._metrics_lock:
            self._request_metrics[key] += amount
            return self._request_metrics[key]

    # ---- HTTP ---------------------------------------------------------------

    def _wait(self, seconds: float) -> bool:
        """等一段时间；被取消返回 True。1.0 的 ``self._cancel.wait(...)``。"""
        if self.cancel is not None:
            return bool(self.cancel.wait(seconds))
        self.sleep(seconds)
        return False

    def _wait_for_request_slot(self) -> None:
        """把 judge 请求在多个 worker 之间摊开，避免同步突发（1.0 ``:4467``）。"""
        if JUDGE_REQUEST_INTERVAL_SECONDS <= 0:
            return
        with self._rate_lock:
            current = time.monotonic()
            wait_seconds = max(0.0, self._next_request_at - current)
            self._next_request_at = max(current, self._next_request_at) + JUDGE_REQUEST_INTERVAL_SECONDS
        if wait_seconds and self._wait(wait_seconds):
            raise RunCancelledError("cancelled while waiting for Judge rate limit")

    @staticmethod
    def _client_error_code(error: BaseException) -> int | None:
        """不可重试的 4xx 状态码；否则 ``None``。

        ``HttpxJsonClient.request_json`` 把 ``HTTPStatusError`` 包成 ``RuntimeError``，
        原异常挂在 ``__cause__`` 上——所以沿异常链找带状态码的那一层。``urllib`` 的
        ``HTTPError.code`` 与 ``httpx`` 的 ``response.status_code`` 两种都认，因为
        1.0 的这段代码就是按 ``.code`` 写的。

        **408 与 429 必须排除**：它们重试才对症，而 ``_retry_delay`` 读的
        ``Retry-After`` 就是给它们用的。1.0 的注释记着实测代价：豆包订阅失效时每次
        0.2s 返回 400，被 6 次退避拖成约 195 秒/题。
        """
        current: BaseException | None = error
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            code = getattr(current, "code", None)
            if code is None:
                response = getattr(current, "response", None)
                code = getattr(response, "status_code", None)
            if isinstance(code, int) and 400 <= code < 500 and code not in (408, 429):
                return code
            current = current.__cause__ or current.__context__
        return None

    def _retry_delay(self, error: Exception, attempt: int) -> float:
        """指数退避 + ``Retry-After`` 优先 + 抖动（1.0 ``:4508``）。"""
        delay = min(
            JUDGE_RETRY_BACKOFF_MAX_SECONDS,
            JUDGE_RETRY_BACKOFF_SECONDS * (2 ** max(0, attempt - 1)),
        )
        current: BaseException | None = error
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            headers = getattr(current, "headers", None)
            if headers is None:
                headers = getattr(getattr(current, "response", None), "headers", None)
            if headers is not None:
                with contextlib.suppress(Exception):
                    raw = str(headers.get("Retry-After") or "").strip()
                    if raw:
                        delay = max(delay, float(raw))
                break
            current = current.__cause__ or current.__context__
        return float(delay + self.rand(0.0, min(2.0, delay * 0.2)))

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.judge_api_key}"} if self.judge_api_key else {}

    def _request(self, payload: dict[str, Any], timeout: int = 180) -> tuple[Any, int]:
        """1.0 ``_judge_request``（``:4433``）：带退避的重试；4xx 立刻抛。"""
        last_error: Exception | None = None
        request_group = 0
        if self.http_attempt_evidence:
            with self._metrics_lock:
                self._http_request_groups += 1
                request_group = self._http_request_groups
        for attempt in range(1, JUDGE_RETRY_ATTEMPTS + 1):
            positions: dict[int, int] = {}
            request_index = [0]
            transport_done: set[int] = set()
            gate_error_seen = [False]

            def observe(
                metadata: Mapping[str, Any],
                positions: dict[int, int] = positions,
                request_index: list[int] = request_index,
                judge_attempt: int = attempt,
                transport_done: set[int] = transport_done,
                gate_error_seen: list[bool] = gate_error_seen,
            ) -> None:
                event = metadata.get("event")
                if self.shared_gate_enabled:
                    if event == "gate_wait_started":
                        self._request_metric("gate_waiting")
                        return
                    if event == "gate_wait_finished":
                        self._request_metric("gate_waiting", -1)
                        self._request_metric("gate_wait_ms", float(metadata.get("gate_wait_ms", 0)))
                        if metadata.get("gate_error") and metadata["gate_error"] != "RunCancelledError":
                            self._request_metric("gate_failures")
                            gate_error_seen[0] = True
                        return
                    if event == "gate_error":
                        self._request_metric("gate_failures")
                        gate_error_seen[0] = True
                        return
                    if event == "json_valid":
                        self._request_metric("responses_received")
                        return
                    if event == "json_invalid":
                        self._request_metric("request_errors")
                        return
                transport = metadata.get("transport_attempt")
                if type(transport) is not int or transport < 1:
                    return
                if self.shared_gate_enabled:
                    if metadata.get("dispatch_started") and transport not in positions:
                        request_index[0] = int(self._request_metric("requests_started"))
                        self._request_metric("in_flight")
                    if metadata.get("finished_at") is not None and transport not in transport_done:
                        transport_done.add(transport)
                        self._request_metric("in_flight", -1)
                        status = metadata.get("http_status")
                        if metadata.get("exception_class") or (isinstance(status, int) and status >= 300):
                            self._request_metric("request_errors")
                            if status == 429:
                                self._request_metric("http_429")
                allowed = {
                    "transport_attempt",
                    "started_at",
                    "started_monotonic",
                    "finished_at",
                    "elapsed_ms",
                    "http_status",
                    "exception_class",
                    "retry_after",
                    "rate_limit_headers",
                    "provider_error_code",
                    "provider_error_type",
                    "transport_retry",
                    "transport_backoff_seconds",
                    "metadata_unavailable",
                    "shared_gate",
                    "gate_lease_id",
                }
                with self._metrics_lock:
                    if transport not in positions:
                        positions[transport] = len(self._http_attempts)
                        self._http_attempts.append(
                            {
                                "index": len(self._http_attempts) + 1,
                                "request_index": request_index[0],
                                "request_group": request_group,
                                "judge_retry_attempt": judge_attempt,
                            }
                        )
                    self._http_attempts[positions[transport]].update(
                        copy.deepcopy({key: value for key, value in metadata.items() if key in allowed})
                    )

            def annotate(_positions: dict[int, int] = positions, **facts: Any) -> None:
                if _positions:
                    with self._metrics_lock:
                        self._http_attempts[_positions[max(_positions)]].update(facts)

            try:
                if self.cancel is not None and self.cancel.is_set():
                    raise RunCancelledError("cancelled before Judge request dispatch")
                wait_started = time.perf_counter()
                self._request_metric("rate_waiting")
                try:
                    self._wait_for_request_slot()
                finally:
                    self._request_metric("rate_waiting", -1)
                    self._request_metric("rate_wait_ms", (time.perf_counter() - wait_started) * 1000)
                if self.cancel is not None and self.cancel.is_set():
                    raise RunCancelledError("cancelled before Judge request dispatch")
                if not self.shared_gate_enabled:
                    request_index[0] = int(self._request_metric("requests_started"))
                    self._request_metric("in_flight")
                try:
                    if (self.http_attempt_evidence or self.shared_gate_enabled) and isinstance(
                        self.http, ObservedJsonHttpClient
                    ):
                        response = self.http.request_json_observed(
                            judge_chat_url(self.judge_url),
                            payload,
                            "POST",
                            timeout,
                            self._headers(),
                            observer=observe,
                            **(
                                {
                                    "cancelled": self.cancel.is_set
                                    if self.cancel is not None
                                    else (lambda: False)
                                }
                                if self.shared_gate_enabled
                                else {}
                            ),
                        )
                    else:
                        response = self.http.request_json(
                            judge_chat_url(self.judge_url), payload, "POST", timeout, self._headers()
                        )
                    annotate(judge_retry_planned=False)
                    if not self.shared_gate_enabled:
                        self._request_metric("responses_received")
                except Exception as exc:
                    if not self.shared_gate_enabled:
                        self._request_metric("request_errors")
                    current: BaseException | None = exc
                    seen: set[int] = set()
                    while current is not None and id(current) not in seen:
                        seen.add(id(current))
                        code = getattr(current, "code", None)
                        if code is None:
                            code = getattr(getattr(current, "response", None), "status_code", None)
                        if code == 429:
                            if not self.shared_gate_enabled:
                                self._request_metric("http_429")
                            break
                        current = current.__cause__ or current.__context__
                    raise
                finally:
                    if not self.shared_gate_enabled:
                        self._request_metric("in_flight", -1)
                return response, attempt
            except SharedJudgeGateError:
                if not gate_error_seen[0]:
                    self._request_metric("gate_failures")
                annotate(judge_retry_planned=False, retry_reason="shared_gate_failed_closed")
                raise
            except (RunCancelledError, RequestOutcomeUnknownError) as exc:
                annotate(judge_retry_planned=False, retry_reason=type(exc).__name__)
                raise
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                annotate(
                    judge_retry_planned=False,
                    retry_reason="provider_response_invalid",
                    request_exception_class=type(exc).__name__,
                )
                error = RequestOutcomeUnknownError(
                    method="POST", cause_type=type(exc).__name__, attempts=attempt
                )
                error.facts.update(error_code="provider_response_invalid", request_outcome_unknown=True)
                raise error from exc
            except Exception as exc:  # noqa: BLE001 —— 1.0 在这里把一切收成「重试或放弃」
                last_error = exc
                annotate(request_exception_class=type(exc).__name__)
                client_code = self._client_error_code(exc)
                if client_code is not None:
                    annotate(judge_retry_planned=False, retry_reason="non_retryable_http")
                    raise RuntimeError(
                        f"judge request rejected with HTTP {client_code} (client error, not retryable): {exc}"
                    ) from exc
                if attempt >= JUDGE_RETRY_ATTEMPTS:
                    annotate(judge_retry_planned=False, retry_reason="retry_exhausted")
                    break
                delay = self._retry_delay(exc, attempt)
                annotate(
                    judge_retry_planned=True,
                    judge_backoff_seconds=delay,
                    retry_reason="retryable_request_error",
                )
                backoff_started = time.perf_counter()
                cancelled_backoff = self._wait(delay)
                annotate(
                    judge_backoff_elapsed_ms=(time.perf_counter() - backoff_started) * 1000,
                    judge_backoff_cancelled=bool(cancelled_backoff),
                )
                if cancelled_backoff:
                    raise RunCancelledError("cancelled while retrying Judge request") from exc
        raise RuntimeError(
            f"judge request failed after {JUDGE_RETRY_ATTEMPTS} attempts: {last_error}"
        ) from last_error

    # ---- 三个判分器 ---------------------------------------------------------

    @staticmethod
    def _conversation_context(conversation: Sequence[Mapping[str, Any]] | None) -> str:
        if not conversation:
            return ""
        return (
            "**截至本轮的对话**：\n"
            + "\n".join(
                f"第{idx + 1}轮用户：{turn.get('message', '')}\n第{idx + 1}轮回答：{turn.get('answer', '')}"
                for idx, turn in enumerate(conversation)
            )
            + "\n"
        )

    def judge(
        self,
        question: str,
        reference: str,
        answer: str,
        system_prompt: str | None = None,
        conversation: Sequence[Mapping[str, Any]] | None = None,
        expected_action: str | None = "answer",
        task_type: str | None = None,
        question_type: str | None = None,
        answerability: str | None = None,
    ) -> dict[str, Any]:
        """1.0 ``_judge``（``:4238``）：回答质量 0/1/2。

        **分数—理由一致性保护照搬**：模型经常给出「理由说正确」但分数 0，或反之。
        1.0 发现不一致就用一段追加的 system 提示重问一次，仍不一致则记
        ``judge_inconsistent_score_reason`` 并把分数置 ``None``（不是 0——0 是合法分数）。
        """
        prompt = system_prompt or self.answer_quality_prompt
        rubric = ANSWER_QUALITY_RUBRICS.get(str(expected_action), ANSWER_QUALITY_RUBRICS["answer"])
        context = self._conversation_context(conversation)
        judge_text = (
            f"{context}**当前问题**：{question}\n**模型回答**：【{answer}】\n"
            f"**预期行为（GT）**：{expected_action or '未标注'}\n"
            f"**任务类型（GT）**：{task_type or '未标注'}\n"
            f"**问题类型（GT）**：{question_type or '未标注'}\n"
            f"**可回答性（GT）**：{answerability or '未标注'}\n"
            f"**参考答案（GT）**：【{reference}】\n"
            f"**本题评分规则**：\n{rubric}\n"
            "仅评价模型回答是否按预期行为有效完成本轮任务。核心信息由截至本轮的用户实际要求决定；参考答案中用户未要求的附加描述不得自动成为扣分项。"
            "评分前必须专门检查：模型是否否定了当前问题或已确认对话明确给出的事件、对象、记录存在性等前提；若最终结论正确但同时否定了这些已知前提，应按相关事实错误降为1分。"
            "只输出JSON："
            '{"score":0|1|2,"reason":"简短中文理由"}'
        )
        payload = {
            "model": self.judge_model,
            "temperature": 0,
            "max_tokens": 1024,
            **thinking_kwargs(self.judge_url),
            "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": judge_text}],
        }
        attempts: list[dict[str, Any]] = []
        text = ""
        normalization = normalize_judge_score(None)
        retry_attempts = 0
        try:
            score: int | None = None
            reason = ""
            for consistency_attempt in range(2):
                raw, request_attempts = self._request(payload)
                retry_attempts += request_attempts
                if truncated := _judge_response_problem(raw, payload):
                    return {
                        **truncated,
                        "judge_retry_attempts": retry_attempts,
                        "attempts": [*attempts, *truncated["attempts"]],
                    }
                text = str(raw.get("choices", [{}])[0].get("message", {}).get("content") or "")
                if invalid := _invalid_judge_output(raw, payload, text, "judge"):
                    return {
                        **invalid,
                        "score_normalization_version": JUDGE_SCORE_NORMALIZATION_VERSION,
                        "score_normalization": normalize_judge_score(parse_judge_json(text).get("score")),
                        "judge_retry_attempts": retry_attempts,
                        "attempts": [*attempts, *invalid["attempts"]],
                    }
                parsed = parse_judge_json(text)
                normalization = normalize_judge_score(parsed.get("score"))
                score = normalization["score"]
                reason = str(parsed.get("reason") or text[:200])
                consistent = judge_score_consistency(score, reason)
                attempts.append(
                    {
                        "input": payload,
                        "raw_response": raw,
                        "raw_text": text,
                        "score_normalization": normalization,
                        "consistent": consistent,
                        "request_attempts": request_attempts,
                    }
                )
                if consistent:
                    break
                if consistency_attempt == 1:
                    return {
                        "score": None,
                        "reason": "judge_inconsistent_score_reason",
                        "judge_status": "failed",
                        "operational_issue": {
                            "version": 1,
                            "scope": "judge",
                            "kind": "model_output_invalid",
                            "evidence": {
                                "source": "judge_response",
                                "field": "response_contract",
                                "value": "score_reason_inconsistent",
                                "error_code": "model_output_invalid",
                                "request_outcome_unknown": False,
                            },
                        },
                        "input": payload,
                        "raw_text": text,
                        "attempts": attempts,
                        "score_normalization_version": JUDGE_SCORE_NORMALIZATION_VERSION,
                        "score_normalization": normalization,
                        "consistency_version": JUDGE_CONSISTENCY_VERSION,
                        "judge_retry_attempts": retry_attempts,
                    }
                payload = {
                    **payload,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                f"{prompt}\n\n一致性保护：你刚才的评分理由与分数矛盾。"
                                "如果理由确认回答正确、与参考答案一致、符合预期，分数不能是 0；"
                                "请重新核对后只输出一个分数与理由一致的 JSON。"
                            ),
                        },
                        payload["messages"][1],
                    ],
                }
            return {
                "score": score,
                "score_normalization_version": JUDGE_SCORE_NORMALIZATION_VERSION,
                "score_normalization": normalization,
                "attempts": attempts,
                "consistency_version": JUDGE_CONSISTENCY_VERSION,
                "reason": reason,
                "input": payload,
                "raw_text": text,
                "judge_status": "completed",
                "judge_retry_attempts": retry_attempts,
            }
        except Exception as exc:  # noqa: BLE001 — preserve received observations even if retry fails
            attempts.append({"input": payload, "error": str(exc)})
            return {
                "score": None,
                "score_normalization_version": JUDGE_SCORE_NORMALIZATION_VERSION,
                "score_normalization": normalization,
                "attempts": attempts,
                "consistency_version": JUDGE_CONSISTENCY_VERSION,
                "reason": "judge_inconsistent_score_reason" if len(attempts) > 1 else f"judge_error: {exc}",
                "input": payload,
                "raw_text": text,
                "judge_status": "outcome_unknown"
                if isinstance(exc, RequestOutcomeUnknownError)
                else "failed",
                **_judge_request_failure(exc),
            }

    def judge_task_action(
        self,
        question: str,
        answer: str,
        expected_action: str | None,
        agent_status: str | None,
        termination_reason: str,
        task_type: str | None = None,
        question_type: str | None = None,
        answerability: str | None = None,
        reference: str | None = None,
        conversation: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """1.0 ``_judge_task_action``（``:4317``）：模型实际做的是 answer/refuse/clarify/none。"""
        if expected_action not in {"answer", "refuse", "clarify"}:
            return {"actual_action": None, "correct": None, "reason": "not_labeled"}
        if not str(answer or "").strip():
            return {
                "expected_action": expected_action,
                "actual_action": "none",
                "correct": False,
                "execution_failure": execution_failure(agent_status, termination_reason),
                "reason": "empty_answer",
                "input": None,
                "raw_text": "",
            }
        context = self._conversation_context(conversation)
        prompt = (
            f"{context}**当前用户问题**：{question}\n**当前模型回答**：【{answer}】\n"
            f"**预期行为（GT）**：{expected_action}\n"
            f"**任务类型（GT）**：{task_type or '未标注'}\n"
            f"**问题类型（GT）**：{question_type or '未标注'}\n"
            f"**可回答性（GT）**：{answerability or '未标注'}\n"
            f"**参考答案（GT）**：【{reference or '未提供'}】\n"
            "结合 GT 能力边界，只判断当前模型回答实际表现为直接回答、拒答还是澄清。只输出 JSON。"
        )
        payload = {
            "model": self.judge_model,
            "temperature": 0,
            "max_tokens": 512,
            **thinking_kwargs(self.judge_url),
            "messages": [
                {"role": "system", "content": self.task_judge_prompt},
                {"role": "user", "content": prompt},
            ],
        }
        try:
            raw, _ = self._request(payload)
            if truncated := _judge_response_problem(raw, payload):
                return truncated
            text = str(raw.get("choices", [{}])[0].get("message", {}).get("content") or "")
            if invalid := _invalid_judge_output(raw, payload, text, "task_judge"):
                return {**invalid, "expected_action": expected_action}
            parsed = parse_judge_json(text)
            actual = (
                parsed.get("actual_action")
                if parsed.get("actual_action") in {"answer", "refuse", "clarify", "none"}
                else None
            )
            return {
                "expected_action": expected_action,
                "actual_action": actual,
                "correct": actual == expected_action if actual else None,
                "execution_failure": execution_failure(agent_status, termination_reason),
                "reason": str(parsed.get("reason") or text[:200]),
                "input": payload,
                "raw_text": text,
                "judge_status": "completed",
            }
        except Exception as exc:  # noqa: BLE001 —— 1.0 ``:4368``
            return {
                "expected_action": expected_action,
                "actual_action": None,
                "correct": None,
                "reason": f"judge_error: {exc}",
                "input": payload,
                "judge_status": "outcome_unknown"
                if isinstance(exc, RequestOutcomeUnknownError)
                else "failed",
                **_judge_request_failure(exc),
            }

    def _inline_images(
        self, images: Sequence[Mapping[str, Any]], assets_by_name: Mapping[str, list[dict[str, Any]]]
    ) -> list[dict[str, Any]]:
        """把召回图转成 data URL——实现由装配点注入（见 ``InlineImages``）。

        没有注入实现时返回空表：证据判分于是走「有召回图但一张都没附上」那条
        （``image_attachment_error``，score 为 ``None`` 而不是 0）。
        """
        if self.inline_images is None:
            return []
        return self.inline_images(self.sentrix_url, images, assets_by_name)

    def judge_evidence(
        self,
        question: str,
        answer: str,
        predicted_images: Sequence[Mapping[str, Any]],
        assets_by_name: Mapping[str, list[dict[str, Any]]],
        conversation: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """1.0 ``_judge_evidence``（``:4373``）：说出的视觉事实有没有图片支持。"""
        if not answer:
            return {"score": None, "reason": "no_answer"}
        context = self._conversation_context(conversation)
        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    f"{context}**用户问题**：{question}\n**模型最终回答**：【{answer}】\n"
                    "以下是本轮实际召回图片。请只根据这些图片评分；若没有图片，模型仍断言具体图片事实则为 0。"
                ),
            }
        ]
        content.extend(self._inline_images(predicted_images, assets_by_name))
        if len(content) == 1:
            return (
                {"score": None, "reason": "image_attachment_error", "input": None}
                if predicted_images
                else {"score": 0, "reason": "no_image_evidence", "input": None}
            )
        payload = {
            "model": self.judge_model,
            "temperature": 0,
            "max_tokens": 512,
            **thinking_kwargs(self.judge_url),
            "messages": [
                {"role": "system", "content": self.evidence_judge_prompt},
                {"role": "user", "content": content},
            ],
        }
        try:
            raw, _ = self._request(payload)
            if truncated := _judge_response_problem(raw, payload):
                return truncated
            text = str(raw.get("choices", [{}])[0].get("message", {}).get("content") or "")
            if invalid := _invalid_judge_output(raw, payload, text, "evidence_judge"):
                return invalid
            parsed = parse_judge_json(text)
            applicable = parsed.get("applicable") is not False
            if not applicable:
                return {
                    "score": None,
                    "applicable": False,
                    "score_normalization_version": JUDGE_SCORE_NORMALIZATION_VERSION,
                    "attempts": [{"input": payload, "raw_response": raw, "raw_text": text}],
                    "reason": str(parsed.get("reason") or "no_visual_claims"),
                    "input": payload,
                    "raw_text": text,
                    "judge_status": "completed",
                }
            normalization = normalize_judge_score(parsed.get("score"))
            score = normalization["score"]
            return {
                "score": score,
                "applicable": True,
                "score_normalization_version": JUDGE_SCORE_NORMALIZATION_VERSION,
                "score_normalization": normalization,
                "attempts": [
                    {
                        "input": payload,
                        "raw_response": raw,
                        "raw_text": text,
                        "score_normalization": normalization,
                    }
                ],
                "reason": str(parsed.get("reason") or text[:200]),
                "input": payload,
                "raw_text": text,
                "judge_status": "completed",
            }
        except Exception as exc:  # noqa: BLE001 —— 1.0 ``:4414``
            return {
                "score": None,
                "reason": f"judge_error: {exc}",
                "input": payload,
                "judge_status": "outcome_unknown"
                if isinstance(exc, RequestOutcomeUnknownError)
                else "failed",
                **_judge_request_failure(exc),
            }

    # ---- 一个 item 的编排 ---------------------------------------------------

    def judge_item(
        self,
        item: dict[str, Any],
        row: Mapping[str, Any],
        assets_by_name: Mapping[str, list[dict[str, Any]]],
    ) -> dict[str, Any]:
        """1.0 ``_judge_item``（``:3588``）去掉 2.0 没有的计时段。

        **2.0 与 1.0 的两处形状差异**：(a) 1.0 的 item 带 ``timing_breakdown`` /
        ``model_call_metrics``（Agent 回合里逐次模型调用与工具往返的计时），2.0 的
        ``public_progress`` 里没有这些，所以这里不重建 ``timing_breakdown``；
        (b) 2.0 的题目字段已经在 ``_evaluate_one`` 里透传进 item（``PASSTHROUGH_FIELDS``），
        所以 GT 侧只从 ``row`` 取 ``conversation``。
        """
        if operational_issue(item):
            return exclude_operational_quality(item)
        started = time.perf_counter()
        conversation = row.get("conversation") or []
        reference = str(item.get("reference_answer") or row.get("answer") or "")
        turn_records = item.get("runtime_turns") or []
        for index, record in enumerate(turn_records):
            expected = record.get("expected_action")
            turn_definition = (
                conversation[index]
                if index < len(conversation) and isinstance(conversation[index], dict)
                else {}
            )
            turn_reference = str(turn_definition.get("reference_answer") or "")
            if not turn_reference:
                turn_reference = (
                    "应要求用户提供至少一个具体、可用于后续检索的锚点；询问形式不限。"
                    if expected == "clarify"
                    else reference
                )
            conv_ctx = turn_records[: index + 1]
            record["task_judge"] = self.judge_task_action(
                record["message"],
                record["answer"],
                expected,
                record.get("agent_status"),
                record.get("termination_reason"),
                task_type=row.get("task_type"),
                question_type=row.get("question_type"),
                answerability=row.get("answerability"),
                reference=turn_reference,
                conversation=conv_ctx,
            )
            if record["task_judge"].get("judge_status") == "outcome_unknown" or record["task_judge"].get(
                "operational_issue"
            ):
                break  # No new sub-requests after an uncertain dispatch for this item.
            # 同一轮的回答质量与图片证据两个调用**并行**（1.0 的注释）。
            should_judge_evidence = (
                self.evidence_enabled
                and record.get("predicted_images")
                and record.get("answer")
                and record["task_judge"].get("actual_action") != "clarify"
            )
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                quality_future = (
                    pool.submit(
                        self.judge,
                        record["message"],
                        turn_reference,
                        record["answer"],
                        expected_action=expected,
                        task_type=row.get("task_type"),
                        question_type=row.get("question_type"),
                        answerability=row.get("answerability"),
                        conversation=conv_ctx,
                    )
                    if expected in ANSWER_QUALITY_RUBRICS
                    else None
                )
                evidence_future = (
                    pool.submit(
                        self.judge_evidence,
                        record["message"],
                        record["answer"],
                        record.get("predicted_images") or [],
                        assets_by_name,
                        conversation=conv_ctx,
                    )
                    if should_judge_evidence
                    else None
                )
            record["judge"] = (
                quality_future.result() if quality_future else {"score": None, "reason": "not_labeled"}
            )
            record["evidence_judge"] = (
                evidence_future.result() if evidence_future else {"score": None, "reason": "not_applicable"}
            )

            if any(
                (record.get(key) or {}).get("judge_status") == "outcome_unknown"
                or (record.get(key) or {}).get("operational_issue")
                for key in ("judge", "evidence_judge")
            ):
                break

        task_judges = [record.get("task_judge") or {} for record in turn_records]
        task_judge = task_judges[-1] if task_judges else {"actual_action": None, "correct": None}
        judge = turn_records[-1].get("judge") if turn_records else {"score": None, "reason": "not_applicable"}
        evidence_judge = (
            turn_records[-1].get("evidence_judge")
            if turn_records
            else {"score": None, "reason": "not_applicable"}
        )
        judge_ms = round((time.perf_counter() - started) * 1000, 1)
        all_judge_outputs = [
            record.get(key) for record in turn_records for key in ("judge", "task_judge", "evidence_judge")
        ]
        # 任何一个子判分 failed，item 的 judge_status 就是 failed——读侧靠它区分
        # 「真的打了 0 分」与「根本没打成」。
        judge_terminal_status = (
            "failed"
            if any(
                isinstance(value, dict) and value.get("judge_status") == "failed"
                for value in all_judge_outputs
            )
            else "completed"
        )
        if any(
            isinstance(value, dict) and value.get("judge_status") == "outcome_unknown"
            for value in all_judge_outputs
        ):
            judge_terminal_status = "outcome_unknown"
        judge_issue = next(
            (
                value["operational_issue"]
                for value in all_judge_outputs
                if isinstance(value, dict) and value.get("operational_issue")
            ),
            None,
        )
        if judge_issue:
            item["judge_issue"] = judge_issue
            judge = {
                "score": None,
                "reason": "judge_output_limit",
                "judge_status": "failed",
                "operational_issue": judge_issue,
                "excluded_observation": judge,
            }
        item.update(
            {
                "judge": judge or {"score": None, "reason": "not_applicable"},
                "task_judge": task_judge,
                "task_judges": task_judges,
                "evidence_judge": evidence_judge or {"score": None, "reason": "not_applicable"},
                "judge_ms": judge_ms,
                "judge_status": judge_terminal_status,
                "judged_at": now_iso(),
            }
        )
        return item


__all__ = [
    "JUDGE_REQUEST_INTERVAL_SECONDS",
    "JUDGE_RETRY_ATTEMPTS",
    "JUDGE_RETRY_BACKOFF_MAX_SECONDS",
    "JUDGE_RETRY_BACKOFF_SECONDS",
    "JudgeRunner",
]
