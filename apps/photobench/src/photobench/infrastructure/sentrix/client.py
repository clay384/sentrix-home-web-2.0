"""``SentrixClient`` 的 2.0 实现：把主链要的那几件事翻译成 2.0 的端点。

**为什么不做成 domain 层的纯函数拼接**：URL 拼接、multipart 信封、``{"items": [...]}``
的解封都是「怎么和那个服务说话」，属于基础设施。domain 的 ``SentrixClient`` 只声明
「种一个人」「导一个目录」「问一句」这几件事。

**base_url 是取值函数而不是一个字符串**：1.0 的 ``DEFAULT_SENTRIX_URL`` 是模块级全局量，
``POST /api/config`` 会就地改它（``persist_runtime_connection_config`` ``:419``），而
1.0 的主链是在**跑起来之后**读它的。传一个 ``Callable[[], str]`` 才与那条语义等价——
传当前值会在配置被改回之后仍打旧地址。这与 ``application.runs.read_runs.ReadRuns``
的 ``sentrix_url`` 参数是同一条口径。

**2.0 的路径前缀是 ``/api/v1``**（1.0 是 ``/api``）。这条差异只出现在这个文件里——
本切片的范围调整就是「直接按 2.0 的接口写」，S9c 不再逐处返工。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import quote

from photobench.domain.http.ports import HttpStream, JsonHttpClient
from photobench.domain.sentrix.ports import API_PREFIX


def _quote(value: str) -> str:
    """``quote`` 的 **``safe=""``** 版本。

    ``urllib.parse.quote`` 默认把 ``/`` 留在安全字符里，于是 ``scope_id="a/b"``
    拼出来的是 ``?scope_id=a/b``——服务端读到的仍是 ``a/b``（查询串里斜杠合法），
    但放在**路径**里（``/assistant/turns/a/b``）就会把路径切成两段，打到 404 上。

    1.0 这里更松：``GET /api/assets`` 那条是**直接插值、完全不转义**
    （``:2952`` / ``:3111`` 的 ``f"...?scope_id={scope_id}&limit=2000"``），
    只有 ``:1439`` 与 ``:2456`` 用了默认的 ``quote``。2.0 一律收紧到 ``safe=""``，
    让路径与查询串上的 id 只有一种编码方式——**这是偏离 1.0 的一处，登记在行为基线**。
    """
    return quote(value, safe="")


#: 2.0 的 REST 前缀（``/api/v1``）现在定义在 ``domain.sentrix.ports``：打主 API 的
#: 还有展示层那两条（``/api/memory-spaces`` 的转发与 ``/api/qa-dataset`` 的媒体解析），
#: 它们用的是请求里带的 ``sentrix_url``，复用不了这个适配器，但必须用同一个前缀。


class HttpxSentrixClient:
    """``SentrixClient`` 的组合实现：一个 ``JsonHttpClient`` + 一个 base_url 取值函数。"""

    def __init__(self, http: JsonHttpClient, base_url: Callable[[], str]) -> None:
        self._http = http
        self._base_url = base_url

    def _url(self, path: str) -> str:
        return f"{self._base_url().rstrip('/')}{API_PREFIX}{path}"

    def diagnostics(self, *, timeout: int = 10) -> dict[str, Any]:
        body: dict[str, Any] = self._http.request_json(self._url("/diagnostics"), timeout=timeout)
        return body

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
        """``POST /api/v1/people/seed``（``api/routes/identity.py:904``）。

        ``aliases`` 在 2.0 是**一个用顿号或逗号分隔的字符串**（``_split_terms``
        ``identity.py:895``）。这里用 ``"、"`` 拼：manifest 里的别名本身就带中文，
        用逗号会和别名内部的英文逗号打架，而 2.0 两种分隔符都认。

        ``family_role`` 传空串而不是省略：2.0 的字段是 ``str | None``，空串与缺字段
        在 ``_split_terms`` 那一路等价（都得到空），省略字段只是让 multipart 少一段，
        没有行为差异，但显式传空更接近 1.0 逐个人都带 ``family_role`` 的形状。
        """
        return dict(
            self._http.request_multipart(
                self._url("/people/seed"),
                {
                    "name": name,
                    "family_role": family_role,
                    "aliases": "、".join(aliases),
                    "scope_id": scope_id,
                },
                [("files", file_name, content)],
                timeout,
            )
        )

    def import_relationships(
        self,
        *,
        scope_id: str,
        relationships: Sequence[Mapping[str, Any]],
        entity_by_name: Mapping[str, str],
        timeout: int = 60,
    ) -> dict[str, Any]:
        """``POST /api/v1/relationships/batch``（``api/routes/relationships.py:181``）。

        三个字段都可以省略，但这里与 1.0 一样全给：``entity_by_name`` 是**优先于**
        服务端内部名字索引的显式映射（``schemas/relationships.py:100-109``），
        不给的话「我」这类代称在库里重名时会被解析到别人身上。
        """
        return dict(
            self._http.request_json(
                self._url("/relationships/batch"),
                {
                    "scope_id": scope_id,
                    "relationships": [dict(item) for item in relationships],
                    "entity_by_name": dict(entity_by_name),
                },
                "POST",
                timeout,
            )
        )

    def import_album(self, *, scope_id: str, directory: str, limit: int, timeout: int) -> dict[str, Any]:
        """``POST /api/v1/imports``（``api/routes/timeline.py:125``）。

        **同步长请求**，超时由调用方给足（1.0 里这条链被拆成上传 + 轮询两段，
        2.0 里它是一整个请求）。
        """
        return dict(
            self._http.request_json(
                self._url("/imports"),
                {"scope_id": scope_id, "directory": directory, "limit": limit},
                "POST",
                timeout,
            )
        )

    def import_album_tracked(
        self,
        *,
        scope_id: str,
        directory: str,
        limit: int,
        timeout: int,
        operation_id: str,
    ) -> dict[str, Any]:
        return dict(
            self._http.request_json(
                self._url("/imports"),
                {"scope_id": scope_id, "directory": directory, "limit": limit, "operation_id": operation_id},
                "POST",
                timeout,
            )
        )

    def import_progress(self, *, operation_id: str, timeout: int = 3) -> dict[str, Any]:
        return dict(
            self._http.request_json(
                self._url(f"/imports/{_quote(operation_id)}/progress"),
                timeout=timeout,
            )
        )

    def list_assets(self, *, scope_id: str, limit: int, timeout: int = 60) -> list[dict[str, Any]]:
        """``GET /api/v1/assets``（``api/routes/media.py:290``），解封 ``{"items": [...]}``。

        2.0 的上限是 ``min(limit, 1000)``（路由里做，不在 schema 上），所以调用方给
        超过 1000 的数不会报错、只会被静默截断——这一点与 1.0 的 ``limit=2000``
        行为不同，记在行为基线里。
        """
        payload = self._http.request_json(
            self._url(f"/assets?scope_id={_quote(scope_id)}&limit={int(limit)}"),
            method="GET",
            timeout=timeout,
        )
        items = payload.get("items") if isinstance(payload, dict) else None
        return [dict(item) for item in (items or []) if isinstance(item, dict)]

    def start_conversation(self, *, scope_id: str, title: str = "", timeout: int = 30) -> dict[str, Any]:
        """``POST /api/v1/assistant/conversations``（``api/routes/assistant.py:127``）。

        回合的 scope 由会话承载，所以这个调用是「这一轮问答在哪个相册里问」的唯一入口。
        """
        return dict(
            self._http.request_json(
                self._url("/assistant/conversations"),
                {"scope_id": scope_id, "title": title},
                "POST",
                timeout,
            )
        )

    def start_turn(
        self,
        *,
        conversation_id: str,
        message: str,
        include_debug: bool = False,
        timeout: int = 300,
    ) -> dict[str, Any]:
        """``POST /api/v1/assistant/conversations/{cid}/turns``（``assistant.py:202``）。

        请求体只有 ``message`` 与 ``include_debug``：``StartTurnRequest`` 里的
        ``conversation_id`` **被路由忽略**（``assistant.py:213`` 的注释写明路径参数才是
        权威），所以这里也不发它——发一个会被忽略的字段只会让下一个人以为它有用。
        """
        payload: dict[str, Any] = {"message": message}
        if include_debug:
            # 只在需要时发这个键：老版主 API 不认识它也不会因为多一个字段而变，但
            # 「默认请求体长什么样」保持原样更省解释成本。
            payload["include_debug"] = True
        return dict(
            self._http.request_json(
                self._url(f"/assistant/conversations/{_quote(conversation_id)}/turns"),
                payload,
                "POST",
                timeout,
            )
        )

    def get_turn(self, *, turn_id: str, timeout: int = 30) -> dict[str, Any]:
        """``GET /api/v1/assistant/turns/{turn_id}``（**复数**，``assistant.py:341``）。"""
        return dict(
            self._http.request_json(
                self._url(f"/assistant/turns/{_quote(turn_id)}"), method="GET", timeout=timeout
            )
        )

    def open_asset_file(
        self, asset_id: str, *, range_header: str | None = None, timeout: int = 60
    ) -> HttpStream:
        """``GET /api/v1/assets/{id}/file``（``api/routes/media.py:290`` 一带）。

        ``Range`` 原样转发——视频的拖动条靠它；超时 60 秒照 1.0 的代理（``:8000``）。
        状态码**不在这里判**：4xx/5xx 也是合法结果，要原样转给调用方。
        """
        headers = {"Range": range_header} if range_header else None
        return self._http.stream_get(
            self._url(f"/assets/{_quote(asset_id)}/file"), headers=headers, timeout=timeout
        )
