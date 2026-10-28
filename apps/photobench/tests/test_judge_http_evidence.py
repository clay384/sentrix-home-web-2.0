"""Opt-in Judge transport metadata: mock HTTP only, no requests to any real service."""

import concurrent.futures
import json
import threading

import httpx
import pytest

import photobench.application.run_execution.judge as judge_module
from photobench.api.metrics_schema import JudgeRequestMetrics
from photobench.application.run_execution.judge import JudgeRunner
from photobench.domain.errors import RequestOutcomeUnknownError
from photobench.infrastructure.http.client import HttpxJsonClient

PRIVATE_PROMPT = "PRIVATE_PROMPT_SENTINEL"
PRIVATE_KEY = "sk-private-key-sentinel"
PRIVATE_ANSWER = "PRIVATE_ANSWER_SENTINEL"
GOOD = {"choices": [{"message": {"content": '{"score":2,"reason":"正确"}'}, "finish_reason": "stop"}]}


@pytest.fixture(autouse=True)
def fixed_retry(monkeypatch):
    monkeypatch.setattr(judge_module, "JUDGE_REQUEST_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(judge_module, "JUDGE_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr(judge_module, "JUDGE_RETRY_BACKOFF_SECONDS", 5)
    monkeypatch.setattr(judge_module, "JUDGE_RETRY_BACKOFF_MAX_SECONDS", 60)


def runner_for(handler, *, enabled=True):
    sleeps = []
    client = HttpxJsonClient(httpx.Client(transport=httpx.MockTransport(handler)), sleep=sleeps.append)
    runner = JudgeRunner(
        http=client,
        judge_url="https://unit.invalid/v1",
        judge_model="judge",
        judge_api_key=PRIVATE_KEY,
        http_attempt_evidence=enabled,
        sleep=sleeps.append,
        rand=lambda low, high: 0,
    )
    return runner, sleeps


def assert_safe(metrics):
    text = json.dumps(metrics)
    for secret in (PRIVATE_PROMPT, PRIVATE_KEY, PRIVATE_ANSWER, "PRIVATE_BODY_MESSAGE", "PRIVATE_HEADER"):
        assert secret not in text
    for forbidden in (
        "Authorization",
        "messages",
        "raw_response",
        "raw_text",
        "response_body",
        "request_body",
    ):
        assert forbidden not in text


def test_failed_429_attempt_survives_success_and_serializes_in_existing_metrics():
    sent = []

    def handler(request):
        sent.append(request.content)
        if len(sent) == 1:
            return httpx.Response(
                429,
                json={
                    "error": {
                        "code": "EndpointRPMExceeded",
                        "type": "rate_limit_error",
                        "message": "PRIVATE_BODY_MESSAGE " + PRIVATE_KEY,
                    }
                },
                headers={
                    "Retry-After": "7",
                    "x-ratelimit-limit-requests": "60",
                    "x-ratelimit-remaining-tokens": "1000",
                    "x-private-echo": "PRIVATE_HEADER",
                    "x-request-id": PRIVATE_KEY,
                },
            )
        return httpx.Response(200, json=GOOD)

    runner, sleeps = runner_for(handler)
    result = runner.judge(PRIVATE_PROMPT, "reference", PRIVATE_ANSWER)
    assert result["judge_status"] == "completed" and result["score"] == 2
    assert result["attempts"][0]["raw_response"] == GOOD
    assert result["judge_retry_attempts"] == 2
    metrics = runner.request_metrics()
    attempts = metrics["http_attempts"]
    assert (
        metrics["requests_started"] == 2 and metrics["responses_received"] == 1 and metrics["http_429"] == 1
    )
    assert len(attempts) == 2 and [a["index"] for a in attempts] == [1, 2]
    first, last = attempts
    assert first["http_status"] == 429 and first["exception_class"] == "HTTPStatusError"
    assert first["provider_error_code"] == "EndpointRPMExceeded"
    assert first["provider_error_type"] == "rate_limit_error"
    assert first["retry_after"] == 7 and first["judge_backoff_seconds"] == 7 and first["judge_retry_planned"]
    assert first["judge_backoff_elapsed_ms"] >= 0 and first["judge_backoff_cancelled"] is False
    assert first["request_group"] == last["request_group"]
    assert first["rate_limit_headers"] == {
        "x-ratelimit-limit-requests": 60.0,
        "x-ratelimit-remaining-tokens": 1000.0,
    }
    assert last["http_status"] == 200 and not last["judge_retry_planned"]
    assert first["finished_at"] >= first["started_at"] and last["finished_at"] >= last["started_at"]
    assert sent[0] == sent[1] and sleeps == [7]
    assert_safe(metrics)
    serialized = JudgeRequestMetrics.model_validate(metrics).model_dump(exclude_unset=True)
    assert serialized["http_attempts"] == attempts and serialized["http_attempt_evidence_enabled"] is True
    first["provider_error_code"] = "mutated caller copy"
    assert runner.request_metrics()["http_attempts"][0]["provider_error_code"] == "EndpointRPMExceeded"


@pytest.mark.parametrize(
    "code", [PRIVATE_PROMPT, PRIVATE_KEY, "free prose message", "https://echo.invalid/private", "x" * 97]
)
def test_private_echo_and_non_identifiers_are_never_metadata(code):
    def handler(request):
        return httpx.Response(
            400,
            json={"error": {"code": code, "type": PRIVATE_KEY, "message": PRIVATE_ANSWER}},
            headers={"Retry-After": "PRIVATE_HEADER", "x-ratelimit-limit-tokens": "PRIVATE_HEADER"},
        )

    runner, _ = runner_for(handler)
    with pytest.raises(RuntimeError):
        runner._request({"messages": [{"role": "user", "content": PRIVATE_PROMPT}], "answer": PRIVATE_ANSWER})
    metrics = runner.request_metrics()
    attempt = metrics["http_attempts"][0]
    assert len(metrics["http_attempts"]) == 1 and attempt["http_status"] == 400
    assert "provider_error_code" not in attempt and "provider_error_type" not in attempt
    assert "retry_after" not in attempt and "rate_limit_headers" not in attempt
    assert not attempt["judge_retry_planned"]
    assert_safe(metrics)


def test_http_date_is_canonical_metadata_without_changing_existing_backoff():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(
                429,
                json={"error": {"code": "EndpointTPMExceeded"}},
                headers={"Retry-After": "Wed, 28 Oct 2026 10:10:00 GMT"},
            )
        return httpx.Response(200, json=GOOD)

    runner, sleeps = runner_for(handler)
    raw, attempts = runner._request({"messages": []})
    assert raw == GOOD and attempts == 2
    first = runner.request_metrics()["http_attempts"][0]
    assert first["retry_after"] == "Wed, 28 Oct 2026 10:10:00 GMT"
    # Existing Judge retry accepts numeric Retry-After only; observation must not change it.
    assert first["judge_backoff_seconds"] == 5 and sleeps == [5]


@pytest.mark.parametrize("enabled", [False, True])
def test_opt_in_keeps_payload_attempt_counts_and_retry_behavior(enabled):
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return (
            httpx.Response(429, json={"error": {"code": "RequestBurstTooFast"}})
            if len(sent) == 1
            else httpx.Response(200, json=GOOD)
        )

    runner, sleeps = runner_for(handler, enabled=enabled)
    payload = {"model": "judge", "messages": [{"role": "user", "content": PRIVATE_PROMPT}]}
    raw, attempts = runner._request(payload)
    metrics = runner.request_metrics()
    assert raw == GOOD and attempts == 2 and sent == [payload, payload] and sleeps == [5]
    assert metrics["requests_started"] == 2 and metrics["request_errors"] == 1
    assert metrics["http_attempt_evidence_enabled"] is enabled
    assert ("http_attempts" in metrics) is enabled


def test_single_success_running_checkpoint_and_later_request_keep_all_attempts():
    running = []
    holder = {}

    def handler(request):
        running.append(holder["runner"].request_metrics())
        return httpx.Response(200, json=GOOD)

    runner, _ = runner_for(handler)
    holder["runner"] = runner
    assert runner._request({"messages": []}) == (GOOD, 1)
    assert running[0]["http_attempts"][0]["finished_at"] is None
    assert_safe(running[0])
    assert runner._request({"messages": []}) == (GOOD, 1)
    metrics = runner.request_metrics()
    assert [a["index"] for a in metrics["http_attempts"]] == [1, 2]
    assert [a["request_index"] for a in metrics["http_attempts"]] == [1, 2]
    assert all(a["http_status"] == 200 for a in metrics["http_attempts"])


def test_actual_connection_attempts_are_separate_without_changing_judge_counter():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError(PRIVATE_KEY, request=request)
        return httpx.Response(200, json=GOOD)

    runner, sleeps = runner_for(handler)
    assert runner._request({"messages": []}) == (GOOD, 1)
    metrics = runner.request_metrics()
    assert metrics["requests_started"] == 1 and len(metrics["http_attempts"]) == 2
    first, last = metrics["http_attempts"]
    assert first["exception_class"] == "ConnectError" and first["http_status"] is None
    assert first["transport_backoff_seconds"] == 0.2 and sleeps == [0.2]
    assert last["http_status"] == 200
    assert_safe(metrics)


def test_uncertain_post_outcome_is_never_retried_by_observer():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ReadTimeout(PRIVATE_ANSWER, request=request)

    runner, sleeps = runner_for(handler)
    with pytest.raises(RequestOutcomeUnknownError):
        runner._request({"messages": []})
    metrics = runner.request_metrics()
    assert len(calls) == 1 and sleeps == []
    assert (
        len(metrics["http_attempts"]) == 1 and metrics["http_attempts"][0]["exception_class"] == "ReadTimeout"
    )
    assert not metrics["http_attempts"][0]["judge_retry_planned"]
    assert_safe(metrics)


def test_observer_failure_does_not_change_http_result():
    client = HttpxJsonClient(
        httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=GOOD)))
    )

    def broken(_):
        raise ValueError(PRIVATE_KEY)

    assert client.request_json_observed("https://unit.invalid", observer=broken) == GOOD


def test_unsupported_optional_client_stays_compatible_and_records_flag():
    class Basic:
        def request_json(self, *args):
            return GOOD

    runner = JudgeRunner(
        http=Basic(), judge_url="http://unit.invalid", judge_model="judge", http_attempt_evidence=True
    )
    assert runner._request({}) == (GOOD, 1)
    metrics = runner.request_metrics()
    assert metrics["http_attempt_evidence_supported"] is False and metrics["http_attempts"] == []


def test_concurrent_requests_keep_separate_groups_and_complete_attempts():
    barrier = threading.Barrier(4)

    def handler(request):
        barrier.wait(timeout=2)
        return httpx.Response(200, json=GOOD)

    runner, _ = runner_for(handler)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda i: runner._request({"item": i}), range(4)))
    assert results == [(GOOD, 1)] * 4
    attempts = runner.request_metrics()["http_attempts"]
    assert len(attempts) == 4 and sorted(a["index"] for a in attempts) == [1, 2, 3, 4]
    assert len({a["request_group"] for a in attempts}) == 4
    assert all(a["finished_at"] is not None and a["http_status"] == 200 for a in attempts)
