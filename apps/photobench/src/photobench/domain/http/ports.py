"""PhotoBench 打向主 API 的 HTTP 客户端端口。

对应 1.0 ``benchmark_orchestrator.py:731`` 的 ``request_json``、``:799`` 的
``request_bytes`` 与 ``:884`` 的 ``upload_files``。1.0 直接用 stdlib
``urllib.request``；2.0 换 httpx（沿用 ``apps/photobench/pyproject.toml`` 已有的依赖，
不新增）。

**``request_multipart`` 是 S9b-2 补的第三个方法。** 它的生产调用者是
``infrastructure.sentrix.client`` 的 ``seed_person``：2.0 的
``POST /people/seed`` 是 multipart 单人端点（1.0 的 ``seed-batch`` 也是 multipart，
所以这不是新形状，只是从「一批」变成「一个」）。**1.0 的 ``upload_files`` 就是它的
原型**——同样是把 fields 与 files 拼成 ``multipart/form-data`` 发出去、同样返回解析后的
JSON。没有它，种身份那一步就得在适配器里自己拼一次 HTTP，那段行为（边界串、字段顺序、
文件名与 mime 的取法）就会有两份实现。

**接口在这里、实现在 infrastructure，理由与 runtime 那几个端口一样**：httpx 在
``domain-purity`` 的 forbidden 名单上（与 ``sentrix.domain`` 逐条对齐），而
``application-purity`` 也不许 application 直接 import httpx。

**三处形状差异，全部由分层与「不泄漏实现类型」逼出来，不是口味：**

1. **返回值是解析后的对象，不是 HTTP 响应。** 1.0 的 ``request_json`` 直接
   ``json.loads(resp.read())`` 返回 Python 对象，形状不变；``request_bytes`` 返回
   ``bytes``，形状也不变。**所以这两个方法的行为与 1.0 完全一致**，只是把
   ``urllib`` 换成了 httpx。
2. **失败仍然抛异常，不返回错误值。** 1.0 只捕获 ``HTTPError`` 并转成
   ``RuntimeError(f"HTTP {code} {reason}: {detail}")``；连接失败 / 超时由 urllib
   原样抛出。2.0 照搬这条分工：非 2xx 由适配器转成同一条 ``RuntimeError`` 文案。
3. **没有重试。** 1.0 没有。加一层「看起来更健壮」的重试会改变调用方观察到的失败
   次数与时序（评测里的 judge 调用自己有一套重试，见 1.0 ``_judge_retry_delay``），
   那不属于本切片。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class HttpStream:
    """一次**流式** GET 的结果：状态码 + 响应头 + body 迭代器 + 关闭钩子。

    **非 2xx 不抛。** 资产代理要把上游的状态码原样转给调用方（1.0 ``:8012`` 的
    ``asset proxy failed: {code}``）；把它变成异常就得在异常里回传状态码，那才是
    多加一层。其余失败（连不上、超时）照旧抛——那两件事没有状态码可回。

    ``body`` 与 ``close`` 都保留下来而不是只给 ``bytes``：视频要按 Range 边读边发，
    整段读进内存会让「拖动进度条」变成一次几十 MB 的分配。关不关由调用方负责
    （HTTP 层用 ``BackgroundTask``）。
    """

    status_code: int
    headers: dict[str, str]
    body: Iterator[bytes]
    close: Callable[[], None]


@runtime_checkable
class JsonHttpClient(Protocol):
    """主 API 的只读/写 JSON 客户端。方法名沿用 1.0，便于逐行对照。"""

    def request_json(
        self,
        url: str,
        payload: Mapping[str, Any] | None = None,
        method: str = "GET",
        timeout: int = 120,
        extra_headers: Mapping[str, str] | None = None,
    ) -> Any: ...

    def request_bytes(self, url: str, timeout: int = 120) -> bytes: ...

    def request_multipart(
        self,
        url: str,
        fields: Mapping[str, str],
        files: Sequence[tuple[str, str, bytes]],
        timeout: int = 300,
    ) -> Any:
        """1.0 ``upload_files``（``:884``）。``files`` 是 ``(字段名, 文件名, 内容)`` 三元组。

        字段名可以是同一个（1.0 的 ``seed-batch`` 与 ``/api/import`` 都用 ``"files"``
        一次带多张）——**不要在这里去重或改名**，那是 1.0 的行为，2.0 的调用方按需决定。
        """
        ...

    def stream_get(
        self, url: str, headers: Mapping[str, str] | None = None, timeout: int = 120
    ) -> HttpStream:
        """1.0 ``_proxy_sentrix_asset``（``:7992``）里那段 ``urlopen`` + ``copyfileobj``。

        ``headers`` 是**调用方给的**（资产代理转发 ``Range``）。返回体不落盘、不进内存，
        直接交出去——这正是 ``request_bytes`` 做不到的那件事。
        """
        ...


HttpAttemptObserver = Callable[[Mapping[str, Any]], None]


@runtime_checkable
class ObservedJsonHttpClient(Protocol):
    """Optional safe transport metadata; ordinary JsonHttpClient remains unchanged."""

    def request_json_observed(
        self,
        url: str,
        payload: Mapping[str, Any] | None = None,
        method: str = "GET",
        timeout: int = 120,
        extra_headers: Mapping[str, str] | None = None,
        *,
        observer: HttpAttemptObserver,
    ) -> Any: ...
