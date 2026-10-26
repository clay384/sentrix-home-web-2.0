"""Real HTTP tests for optional JSON SSE transport and cancellation."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from sentrix.domain.assistant.ports import ModelServiceError
from sentrix.infrastructure.assistant.model_endpoint import ServedModel
from sentrix.infrastructure.media import vlm_json_stream
from sentrix.infrastructure.media.vlm_observer import chat_json


def event(content: str = "", finish: str | None = None) -> bytes:
    return (
        "data: "
        + json.dumps({"choices": [{"index": 0, "delta": {"content": content}, "finish_reason": finish}]})
        + "\n\n"
    ).encode()


DONE = b"data: [DONE]\n\n"
USAGE = b'data: {"choices":[],"usage":{"prompt_tokens":17,"completion_tokens":29,"total_tokens":46}}\n\n'


@pytest.fixture
def server() -> Iterator[Any]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = b'{"data":[{"id":"test-vlm"}]}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            state.posts.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            state.request_ids.append(self.headers.get("X-Request-Id"))
            if state.json_response is not None:
                payload = json.dumps(state.json_response).encode()
                self.send_response(state.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_response(state.status)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                if state.endless:
                    for _ in range(300):
                        time.sleep(0.01)
                        self.wfile.write(event(" "))
                        self.wfile.flush()
                elif state.silent:
                    time.sleep(0.6)
                    for _ in range(3):
                        self.wfile.write(event(" "))
                        self.wfile.flush()
                        time.sleep(0.01)
                else:
                    for chunk in state.chunks:
                        self.wfile.write(chunk)
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                state.connection_closed.set()

        def log_message(self, *_args: object) -> None:
            pass

    class State:
        posts: list[dict[str, Any]]
        request_ids: list[str | None]

    state = State()
    state.posts, state.request_ids = [], []
    state.status, state.endless, state.silent = 200, False, False
    state.json_response = None
    state.chunks = [event('{"caption":"ok"}'), event(finish="stop"), USAGE, DONE]
    state.connection_closed = threading.Event()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    state.url = f"http://127.0.0.1:{httpd.server_port}/v1"
    worker = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.01), daemon=True)
    worker.start()
    try:
        yield state
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join()


def call(
    server: Any,
    tmp_path: Path,
    *,
    private: bool = False,
    timeout: float = 2.0,
    streamed: bool = True,
    mode: str = "json_object",
) -> Any:
    model = ServedModel(
        server.url,
        generation_profile="qwen35_nonthinking",
        native_output_budget=True,
        json_streaming=streamed,
        json_output_mode=mode,
        json_evidence_dir=tmp_path,
        json_private_content=private,
    )
    image = "data:image/jpeg;base64," + base64.b64encode(b"secret-image").decode()
    return chat_json(server.url + "/chat/completions", model, "private-prompt", image, timeout)


def status_rows(tmp_path: Path) -> list[dict[str, Any]]:
    files = [p for p in tmp_path.glob("*.jsonl") if not p.name.endswith(".content.jsonl")]
    assert len(files) == 1
    return [json.loads(line) for line in files[0].read_text().splitlines()]


def test_aggregates_sse_without_caps_or_sampling_changes_and_keeps_content_private(server, tmp_path):
    assert call(server, tmp_path) == {"caption": "ok"}
    assert len(server.posts) == 1
    sent = server.posts[0]
    assert sent["stream"] is True and sent["stream_options"] == {"include_usage": True}
    assert "max_tokens" not in sent and "max_completion_tokens" not in sent
    assert sent["temperature"] == 0.7 and sent["seed"] == 42 and sent["presence_penalty"] == 1.5
    rows = status_rows(tmp_path)
    assert rows[0]["state"] == "dispatched_outcome_unknown"
    assert rows[-1]["state"] == "completed" and rows[-1]["usage"]["completion_tokens"] == 29
    assert rows[-1]["images"] == [{"kind": "content", "sha256": hashlib.sha256(b"secret-image").hexdigest()}]
    assert rows[-1]["request_id"] == server.request_ids[0]
    assert rows[-1]["ttft_s"] is not None
    assert rows[-1]["characters_after_complete_json"] == 0
    text = "".join(p.read_text() for p in tmp_path.iterdir())
    assert "private-prompt" not in text and "secret-image" not in text and '"caption"' not in text
    assert not list(tmp_path.glob("*.content.jsonl"))
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in tmp_path.iterdir())


def test_private_content_is_explicit_and_still_never_records_prompt_or_images(server, tmp_path):
    assert call(server, tmp_path, private=True) == {"caption": "ok"}
    text = next(tmp_path.glob("*.content.jsonl")).read_text()
    assert "caption" in text
    assert "private-prompt" not in text and "secret-image" not in text and "data:image" not in text


def test_complete_json_then_whitespace_is_observed_without_truncation(server, tmp_path):
    server.chunks = [event('{"caption":"ok"}'), event(" " * 200), event(finish="stop"), USAGE, DONE]
    assert call(server, tmp_path) == {"caption": "ok"}
    last = status_rows(tmp_path)[-1]
    assert last["characters_after_complete_json"] == 200
    assert last["whitespace_after_complete_json"] == 200
    assert last["nonwhitespace_after_complete_json"] == 0
    assert last["longest_identical_character_run"] == 200
    assert last["max_repeated_12gram_occurrences"] >= 180


@pytest.mark.parametrize(
    "chunks",
    [
        [event('{"caption":"ok"}'), DONE],
        [event('{"caption":"ok"}'), event(finish="stop")],
        [b'data: {"error":{"message":"private-provider-body"}}\n\n'],
        [b"data: invalid\n\n"],
        [b'event: error\ndata: {"message":"private-provider-body"}\n\n'],
        [b'data: {"choices":[{"delta":{},"finish_reason":{}}]}\n\n'],
    ],
)
def test_incomplete_or_error_stream_is_typed_unknown_without_retry(server, tmp_path, chunks):
    server.chunks = chunks
    with pytest.raises(ModelServiceError) as caught:
        call(server, tmp_path)
    assert caught.value.error_code == "provider_response_invalid"
    assert caught.value.request_policy["request_outcome_unknown"] is True
    assert len(server.posts) == 1
    last = status_rows(tmp_path)[-1]
    assert last["state"] == "unknown"
    assert "private-provider-body" not in str(caught.value)
    assert "private-provider-body" not in "".join(p.read_text() for p in tmp_path.iterdir())


def test_length_is_an_explicit_known_output_limit(server, tmp_path):
    server.chunks = [event('{"caption":"partial'), event(finish="length"), USAGE, DONE]
    with pytest.raises(ModelServiceError) as caught:
        call(server, tmp_path)
    assert caught.value.error_code == "output_limit"
    assert caught.value.request_policy["request_outcome_unknown"] is False
    assert caught.value.prompt_tokens == 17 and caught.value.completion_tokens == 29
    assert status_rows(tmp_path)[-1]["state"] == "failed"


@pytest.mark.parametrize("mode", ["endless", "silent"])
def test_total_deadline_cancels_connection_even_if_tokens_keep_arriving(server, tmp_path, mode):
    setattr(server, mode, True)
    started = time.monotonic()
    with pytest.raises(ModelServiceError) as caught:
        call(server, tmp_path, timeout=0.2)
    assert time.monotonic() - started < 0.8
    assert caught.value.error_code == "time_limit"
    assert caught.value.request_policy["request_outcome_unknown"] is True
    assert caught.value.request_policy["total_deadline_s"] == 0.2
    assert status_rows(tmp_path)[-1]["state"] == "unknown"
    assert len(server.posts) == 1
    assert server.connection_closed.wait(1.0)


def test_non_json_completed_transport_is_known_format_failure(server, tmp_path):
    server.chunks = [event("not json"), event(finish="stop"), USAGE, DONE]
    with pytest.raises(ModelServiceError) as caught:
        call(server, tmp_path)
    assert caught.value.error_code == "model_output_invalid"
    assert caught.value.request_policy["request_outcome_unknown"] is False
    # Transport completed is distinct from usable semantic output; never relabel it unknown.
    assert status_rows(tmp_path)[-1]["state"] == "completed"


def test_http_rejection_is_known_not_unknown(server, tmp_path):
    server.status = 429
    with pytest.raises(ModelServiceError) as caught:
        call(server, tmp_path)
    assert caught.value.error_code == "provider_rejected"
    assert caught.value.http_status == 429
    assert caught.value.request_policy["request_outcome_unknown"] is False
    assert status_rows(tmp_path)[-1]["state"] == "http_failed"


def test_existing_event_loop_uses_safe_worker_and_finishes(server, tmp_path):
    async def invoke():
        return call(server, tmp_path)

    assert asyncio.run(invoke()) == {"caption": "ok"}
    assert len(server.posts) == 1


def test_io_failure_warns_without_discarding_or_retrying_completed_response(server, tmp_path, caplog):
    target = tmp_path / "not-a-directory"
    target.write_text("occupied")
    assert call(server, target) == {"caption": "ok"}
    assert len(server.posts) == 1
    assert "evidence write failed" in caplog.text


def test_identity_redacts_credentials_and_query():
    value = vlm_json_stream._request_identity(
        "http://user:secret@example.test:80/v1/chat/completions?token=private",
        {"model": "safe", "messages": []},
    )
    assert value["endpoint"] == "http://example.test:80/v1/chat/completions"
    assert "secret" not in json.dumps(value) and "private" not in json.dumps(value)


@pytest.mark.parametrize("streamed", [True, False])
def test_content_filter_is_operational_rejection_even_with_valid_json(server, tmp_path, streamed):
    server.chunks = [event('{"caption":"partial"}'), event(finish="content_filter"), DONE]
    if not streamed:
        server.json_response = {
            "choices": [{"message": {"content": '{"caption":"partial"}'}, "finish_reason": "content_filter"}]
        }
    with pytest.raises(ModelServiceError) as caught:
        call(server, tmp_path, streamed=streamed)
    assert caught.value.error_code == "provider_rejected"
    assert caught.value.finish_reason == "content_filter"
    assert caught.value.request_policy["request_outcome_unknown"] is False
    assert len(server.posts) == 1
    if streamed:
        assert status_rows(tmp_path)[-1]["state"] == "failed"


@pytest.mark.parametrize("finish", ["tool_calls", "function_call"])
@pytest.mark.parametrize("streamed", [True, False])
def test_tool_finishes_are_protocol_errors_in_json_only_requests(server, tmp_path, finish, streamed):
    server.chunks = [event('{"caption":"partial"}'), event(finish=finish), DONE]
    if not streamed:
        server.json_response = {
            "choices": [{"message": {"content": '{"caption":"partial"}'}, "finish_reason": finish}]
        }
    with pytest.raises(ModelServiceError) as caught:
        call(server, tmp_path, streamed=streamed)
    assert caught.value.error_code == "provider_response_invalid"
    assert caught.value.request_policy["request_outcome_unknown"] is True
    assert len(server.posts) == 1


@pytest.mark.parametrize("delta", [{"content": "extra"}, {"reasoning": "extra"}])
def test_content_after_finish_is_protocol_error(server, tmp_path, delta):
    extra = ("data: " + json.dumps({"choices": [{"index": 0, "delta": delta}]}) + "\n\n").encode()
    server.chunks = [event('{"caption":"ok"}'), event(finish="stop"), extra, DONE]
    with pytest.raises(ModelServiceError) as caught:
        call(server, tmp_path)
    assert caught.value.error_code == "provider_response_invalid"
    assert caught.value.request_policy["stream_protocol_error"] == "content_after_finish"
    assert status_rows(tmp_path)[-1]["state"] == "unknown"


def test_sse_comments_and_multiline_data_are_supported(server, tmp_path):
    server.chunks = [
        b': keepalive\n\ndata: {"choices": [\n'
        b'data: {"index":0,"delta":{"content":"{\\"caption\\":\\"ok\\"}"}}]}\n\n',
        event(finish="stop"),
        USAGE,
        DONE,
    ]
    assert call(server, tmp_path) == {"caption": "ok"}


def test_periodic_status_survives_until_timeout(server, tmp_path, monkeypatch):
    monkeypatch.setattr(vlm_json_stream, "_STATUS_INTERVAL_S", 0.03)
    server.endless = True
    with pytest.raises(ModelServiceError):
        call(server, tmp_path, timeout=0.2)
    rows = status_rows(tmp_path)
    assert len(rows) >= 3
    assert any(row["content_chars"] > 0 for row in rows[:-1])
    assert rows[-1]["state"] == "unknown"


@pytest.mark.parametrize("mode", ["json_object", "prompt_only"])
def test_container_wires_streaming_to_real_observer_without_changing_default_transport(
    server, tmp_path, mode
):
    from sentrix.bootstrap.container import Container
    from sentrix.config import Settings

    server.chunks = [
        event('{"caption":"中文","activity":"中文","place":"中文","event_type":"中文"}'),
        event(finish="stop"),
        USAGE,
        DONE,
    ]
    settings = Settings(
        data_dir=tmp_path, vlm_base_url=server.url, vlm_json_streaming=True, vlm_json_output_mode=mode
    )
    container = Container.build(settings)
    image = tmp_path / "test.jpg"
    image.write_bytes(b"local-private-image")
    result = container.vlm_observer.observe(image)
    assert result is not None and result.caption == "中文"
    assert len(server.posts) == 1 and server.posts[0]["stream"] is True
    assert server.posts[0].get("response_format") == (
        {"type": "json_object"} if mode == "json_object" else None
    )
    rows = status_rows(settings.vlm_json_evidence_dir)
    assert rows[-1]["json_output_mode"] == mode
    assert rows[-1]["stage"] == "initial_observation"
    assert rows[-1]["state"] == "completed"
    assert rows[-1]["private_content_enabled"] is False


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("mode", ["json_object", "prompt_only"])
def test_explicit_json_mode_changes_only_response_format(server, tmp_path, streamed, mode):
    if not streamed:
        server.json_response = {
            "choices": [{"message": {"content": '{"caption":"ok"}'}, "finish_reason": "stop"}]
        }
    assert call(server, tmp_path, streamed=streamed, mode=mode) == {"caption": "ok"}
    assert len(server.posts) == 1
    body = server.posts[0]
    assert body.get("response_format") == ({"type": "json_object"} if mode == "json_object" else None)
    assert ("response_format" in body) is (mode == "json_object")
    assert "max_tokens" not in body and "max_completion_tokens" not in body
    assert body["seed"] == 42 and body["temperature"] == 0.7
    if streamed:
        assert status_rows(tmp_path)[-1]["json_output_mode"] == mode


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("content", ["not JSON PRIVATE_BODY", "", "[1,2]", '{"caption":"broken'])
def test_completed_bad_json_is_known_format_failure_without_retry(server, tmp_path, streamed, content):
    server.chunks = [event(content), event(finish="stop"), USAGE, DONE]
    if not streamed:
        server.json_response = {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}
    with pytest.raises(ModelServiceError) as caught:
        call(server, tmp_path, streamed=streamed, mode="prompt_only")
    error = caught.value
    assert error.error_code == "model_output_invalid"
    assert error.http_status == 200 and error.finish_reason == "stop"
    assert error.request_policy["request_outcome_unknown"] is False
    assert error.request_policy["json_output_mode"] == "prompt_only"
    assert "PRIVATE" not in str(error)
    assert len(server.posts) == 1


@pytest.mark.parametrize(
    "response",
    [
        {"choices": []},
        {"choices": [{"message": {"content": "{}"}}]},
        {"choices": [{"message": None, "finish_reason": "stop"}]},
    ],
)
def test_nonstream_incomplete_envelope_is_unknown_not_a_format_quality_failure(server, tmp_path, response):
    server.json_response = response
    with pytest.raises(ModelServiceError) as caught:
        call(server, tmp_path, streamed=False)
    assert caught.value.error_code == "provider_response_invalid"
    assert caught.value.request_policy["request_outcome_unknown"] is True
    assert len(server.posts) == 1


@pytest.mark.parametrize("streamed", [False, True])
def test_valid_empty_object_is_preserved(server, tmp_path, streamed):
    server.chunks = [event("{}"), event(finish="stop"), DONE]
    if not streamed:
        server.json_response = {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}
    assert call(server, tmp_path, streamed=streamed, mode="prompt_only") == {}
    assert len(server.posts) == 1
