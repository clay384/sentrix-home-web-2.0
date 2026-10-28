"""``HttpxJsonClient`` 的用例：把 1.0 ``request_json`` / ``request_bytes`` 的行为钉住。

用 ``httpx.MockTransport`` 而不是真网络：状态码、错误体、超时都是从**这个** handler
摆出来的，用例不依赖任何服务的存活。
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from photobench.domain.errors import RequestOutcomeUnknownError
from photobench.infrastructure.http.client import HttpxJsonClient, build_multipart

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.mark.parametrize(
    "method,error,retries",
    [
        ("GET", httpx.RemoteProtocolError, 3),
        ("POST", httpx.ConnectError, 3),
        ("POST", httpx.ReadError, 1),
        ("POST", httpx.WriteError, 1),
        ("POST", httpx.RemoteProtocolError, 1),
        ("POST", httpx.ReadTimeout, 1),
        ("POST", httpx.WriteTimeout, 1),
    ],
)
def test_retries_are_bounded_and_never_replay_an_uncertain_post(method, error, retries):
    calls = []

    def handler(request):
        calls.append(request)
        raise error("injected disconnect", request=request)

    client = HttpxJsonClient(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        retry_connections=True,
        sleep=lambda _: None,
    )
    expected = RequestOutcomeUnknownError if method == "POST" and retries == 1 else error
    with pytest.raises(expected):
        client.request_json("http://local/imports", {}, method)
    assert len(calls) == retries


def _client(handler: Handler) -> HttpxJsonClient:
    return HttpxJsonClient(client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True))


def test_get_returns_the_parsed_body():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["content_type"] = request.headers.get("content-type")
        seen["accepts"] = True
        return httpx.Response(200, json={"ok": True})

    assert _client(handler).request_json("http://sentrix/api/x") == {"ok": True}
    assert seen["method"] == "GET"
    assert seen["content_type"] == "application/json"


def test_post_sends_utf8_json_without_ascii_escaping():
    """1.0 ``:732`` 用的是 ``ensure_ascii=False``——中文原样进 body，不是 \\uXXXX。"""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        return httpx.Response(200, json={})

    _client(handler).request_json("http://sentrix/api/x", {"name": "相册"}, "POST")
    assert json.loads(seen["body"]) == {"name": "相册"}
    assert "相册".encode() in seen["body"]


def test_a_http_error_becomes_the_same_runtime_error_message_as_1_0():
    """1.0 ``:744-760``：非 2xx 转成 ``RuntimeError("HTTP {code} {reason}: {detail}")``。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "没有这个 scope"})

    with pytest.raises(RuntimeError) as excinfo:
        _client(handler).request_json("http://sentrix/api/x")
    message = str(excinfo.value)
    assert message.startswith("HTTP 404 ")
    assert message.endswith(": 没有这个 scope")


def test_a_non_json_error_body_is_used_verbatim():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="  boom  ")

    with pytest.raises(RuntimeError) as excinfo:
        _client(handler).request_json("http://sentrix/api/x")
    assert str(excinfo.value).endswith(": boom")


def test_an_error_body_falls_back_to_error_then_message_then_the_whole_object():
    cases = [
        ({"error": "e"}, "e"),
        ({"message": "m"}, "m"),
        ({"other": 1}, '{"other": 1}'),
    ]
    for payload, expected in cases:

        def handler(request: httpx.Request, payload: dict = payload) -> httpx.Response:
            return httpx.Response(400, json=payload)

        with pytest.raises(RuntimeError) as excinfo:
            _client(handler).request_json("http://sentrix/api/x")
        assert str(excinfo.value).endswith(f": {expected}")


def test_extra_headers_override_the_default_content_type():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("x-token")
        return httpx.Response(200, json={})

    _client(handler).request_json("http://sentrix/api/x", None, "GET", 5, {"x-token": "t"})
    assert seen["auth"] == "t"


def test_timeout_is_passed_through_per_call():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json={})

    _client(handler).request_json("http://sentrix/api/x", None, "GET", 7)
    assert seen["timeout"]["read"] == 7


def test_request_bytes_returns_the_raw_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\x00\x01JPEG")

    assert _client(handler).request_bytes("http://sentrix/api/assets/1/file") == b"\x00\x01JPEG"


def test_request_bytes_does_not_rewrite_the_error():
    """1.0 的 ``request_bytes``（``:799``）没有 except，``HTTPError`` 原样冒出。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="nope")

    with pytest.raises(httpx.HTTPStatusError):
        _client(handler).request_bytes("http://sentrix/api/assets/1/file")


def test_redirects_are_followed_like_urllib():
    """``urllib.request`` 默认跟随重定向，``httpx.Client`` 默认不跟；这里要跟。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/old"):
            return httpx.Response(302, headers={"location": "http://sentrix/api/new"})
        return httpx.Response(200, json={"followed": True})

    assert _client(handler).request_json("http://sentrix/api/old") == {"followed": True}


def test_build_multipart_matches_the_1_0_envelope():
    """1.0 ``build_multipart``（``:864``）：字段在文件之前、字段与文件各自一段、
    文件名与 mime 写进 ``Content-Disposition``、边界串带 ``----BenchmarkOrch`` 前缀。

    **这条断言钉的不是「能发出去」而是「发出去的是 1.0 那个信封」**：字段与文件的
    顺序、每段之间的空行、结尾的 ``--`` 都是服务端解析的依据，任何一处走样都会让
    一个 2.0 的 multipart 解析器读到不同的字段。钉成整段字节是为了让变异无处可藏
    ——只断言「含 name=\"name\"」的话，字段顺序换了、缺了结尾边界都不会红。
    """
    body, content_type = build_multipart(
        {"name": "明明", "scope_id": "s1"}, [("files", "ref.jpg", b"\xff\xd8JPEG")]
    )
    assert content_type.startswith("multipart/form-data; boundary=----BenchmarkOrch")
    boundary = content_type.split("boundary=")[1].encode()
    assert body == (
        b"--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="name"\r\n\r\n' + "明明".encode() + b"\r\n"
        b"--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="scope_id"\r\n\r\n'
        b"s1\r\n"
        b"--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="files"; filename="ref.jpg"\r\n'
        b"Content-Type: image/jpeg\r\n\r\n"
        b"\xff\xd8JPEG\r\n"
        b"--" + boundary + b"--\r\n"
    )


def test_build_multipart_uses_a_fresh_boundary_per_call():
    """边界串是要在正文里排除的字符串，两次调用共用一个是自找的解析错误。"""
    _, first = build_multipart({}, [])
    _, second = build_multipart({}, [])
    assert first != second


def test_build_multipart_falls_back_to_octet_stream_for_unknown_suffixes():
    """1.0 ``:875``：``mimetypes.guess_type`` 猜不出时用 ``application/octet-stream``。"""
    body, _ = build_multipart({}, [("files", "face.bin", b"x")])
    assert b"Content-Type: application/octet-stream" in body


def test_request_multipart_posts_the_envelope_and_parses_the_body():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["content_type"] = request.headers.get("content-type")
        seen["body"] = request.content
        return httpx.Response(201, json={"entity_id": "e1"})

    result = _client(handler).request_multipart(
        "http://sentrix/api/v1/people/seed", {"name": "明明"}, [("files", "a.jpg", b"J")]
    )
    assert result == {"entity_id": "e1"}
    assert seen["content_type"].startswith("multipart/form-data; boundary=")
    assert b'name="name"' in seen["body"]


def test_request_multipart_rewrites_a_non_2xx_into_the_shared_message():
    """**这条与 1.0 的 ``upload_files`` 不同，是刻意选的**：1.0 的 ``upload_files``
    没有 except，``HTTPError`` 原样冒（调用方只看到一句 ``str(exc)``）。S9b-2 的调用方
    ``seed_person`` 要从 2.0 的 ``400 {"detail": ...}`` 里读出「是哪一步拒绝了」，
    所以走 ``request_json`` 那条转换。**没有这条用例，两种行为在测试里完全等价。**
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "文件里没有检测到人脸"})

    with pytest.raises(RuntimeError) as excinfo:
        _client(handler).request_multipart("http://sentrix/api/v1/people/seed", {}, [])
    assert str(excinfo.value).endswith(": 文件里没有检测到人脸")


def test_retry_uses_one_total_budget_and_backoff_is_bounded():
    now = [0.0]
    observed = []

    def sleep(seconds):
        now[0] += seconds

    def handler(request):
        observed.append(request.extensions["timeout"]["read"])
        now[0] += 0.3
        raise httpx.ReadError("injected", request=request)

    client = HttpxJsonClient(
        httpx.Client(transport=httpx.MockTransport(handler)),
        retry_connections=True,
        clock=lambda: now[0],
        sleep=sleep,
    )
    with pytest.raises(httpx.ReadError):
        client.request_json("http://local/read", timeout=1)
    assert observed == [1, 0.5]
    assert now[0] == pytest.approx(0.8)


def test_a_transient_disconnect_is_retried(monkeypatch):
    """2026-10-21 的 158 全流程里 131/487 题死在 ``Server disconnected``。

    进程级 client 复用了 uvicorn 已关闭的 keep-alive 连接，且没有重试。这里钉住
    「换一条连接后能恢复」：第一次抛 ``RemoteProtocolError``，第二次正常返回。
    """
    monkeypatch.setattr("photobench.infrastructure.http.client.time.sleep", lambda _: None)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return httpx.Response(200, json={"ok": True})

    assert _client(handler).request_json("http://sentrix/api/x") == {"ok": True}
    assert len(calls) == 2


def test_a_persistent_disconnect_is_raised_after_the_retry_budget(monkeypatch):
    monkeypatch.setattr("photobench.infrastructure.http.client.time.sleep", lambda _: None)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.RemoteProtocolError("Server disconnected without sending a response.")

    with pytest.raises(httpx.RemoteProtocolError):
        _client(handler).request_json("http://sentrix/api/x")
    assert len(calls) == 3
