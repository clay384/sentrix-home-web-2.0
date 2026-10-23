"""``GET /api/assets/{id}/file``：主 API 资产文件的代理（S10a · T1002 的另一半）。

1.0 ``_proxy_sentrix_asset``（``:7992``）的四件事：转发 ``Range``、回传那五个头、
**保留上游状态码**、流式转发 body。夹具用一个返回 ``HttpStream`` 的假件把四件都摆出来；
``close`` 也记下来，因为「读完不关」在真机上表现为连接池慢慢漏，而不是一个报错。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from photobench.config import Settings
from photobench.domain.http.ports import HttpStream
from photobench.main import create_app


class FakeSentrixAssets:
    """只实现 ``open_asset_file``：这条路由不会再调别的。"""

    def __init__(self, stream: HttpStream | None) -> None:
        self._stream = stream
        self.calls: list[tuple[str, str | None]] = []

    def open_asset_file(
        self, asset_id: str, *, range_header: str | None = None, timeout: int = 60
    ) -> HttpStream:
        self.calls.append((asset_id, range_header))
        assert self._stream is not None
        return self._stream


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    config_root = tmp_path / "config"
    config_root.mkdir()
    (config_root / "runtime_connection.json").write_text(
        '{"sentrix_url": "http://sentrix:9598"}', encoding="utf-8"
    )
    (config_root / "judge_providers.json").write_text(
        '{"default_provider_id": "p", "providers": {"p": {"url": "http://j/v1", "model": "m"}}}',
        encoding="utf-8",
    )
    (config_root / "vllm_targets.json").write_text(
        '{"default_target_id": "local", "targets": {"local": {}}}', encoding="utf-8"
    )
    (tmp_path / "datasets").mkdir()
    (tmp_path / "results").mkdir()
    settings = Settings(
        config_root=config_root, results_root=tmp_path / "results", data_root=tmp_path / "datasets"
    )
    app = create_app(settings, scan_in_background=False)
    with TestClient(app) as test_client:
        yield test_client


def _with_sentrix(client: TestClient, stream: HttpStream | None) -> FakeSentrixAssets:
    sentrix = FakeSentrixAssets(stream)
    container = client.app.state.container
    client.app.state.container = dataclasses.replace(container, sentrix=sentrix)
    return sentrix


def _stream(**overrides: object) -> tuple[HttpStream, list[bool]]:
    closed: list[bool] = []
    values: dict[str, object] = {
        "status_code": 206,
        "headers": {
            "content-type": "video/mp4",
            "content-range": "bytes 0-3/100",
            "accept-ranges": "bytes",
            "cache-control": "private, max-age=3600",
            "content-length": "4",
        },
        "body": iter([b"AB", b"CD"]),
        "close": lambda: closed.append(True),
    }
    values.update(overrides)
    return HttpStream(**values), closed  # type: ignore[arg-type]


def test_asset_proxy_rejects_an_id_with_a_slash(client: TestClient):
    """``/api/v1/assets/a/b/file`` 在 1.0 是 400（id 里有 ``/``），不是 404。"""
    sentrix = _with_sentrix(client, None)
    response = client.get("/api/v1/assets/a/b/file")
    assert response.status_code == 400
    assert response.json() == {"error": "invalid asset id"}
    assert sentrix.calls == []


def test_asset_proxy_forwards_range_headers_and_status(client: TestClient):
    stream, closed = _stream()
    sentrix = _with_sentrix(client, stream)
    response = client.get("/api/v1/assets/asset-1/file", headers={"Range": "bytes=0-3"})
    assert sentrix.calls == [("asset-1", "bytes=0-3")]
    assert response.status_code == 206
    assert response.content == b"ABCD"
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["content-range"] == "bytes 0-3/100"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["cache-control"] == "private, max-age=3600"
    assert closed == [True]


def test_asset_proxy_keeps_the_upstream_error_code(client: TestClient):
    """上游错误**保留状态码**，只把 body 换成 1.0 那句。"""
    stream, closed = _stream(status_code=503, headers={}, body=iter([]))
    _with_sentrix(client, stream)
    response = client.get("/api/v1/assets/asset-1/file")
    assert response.status_code == 503
    assert response.json() == {"error": "asset proxy failed: 503"}
    assert closed == [True]
