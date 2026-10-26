"""legacy 嵌入方案的两个 HTTP 适配器：chinese-clip（视觉）+ bge-m3（文本）。

这是 1.0 的历史方案，2026-10-20 为 16 GB Mac mini 恢复。它**不是**「把 wemm 换掉」
的同一空间方案：视觉 768 维、文本 1024 维，分别落在 ``visual`` 与
``semantic``/``episodic`` 两个 space 里。检索仍成立，因为每个 space 内部自洽。

对应 1.0：

- ``backend/embeddings/chinese_clip_visual.py``（in-process，``cn_clip`` + checkpoint）
- ``backend/embeddings/bge_text.py``（HTTP 8101，``POST /embed``）

2.0 把 chinese-clip 也放进侧车（``scripts/maintenance/legacy_clip_sidecar.py``），
理由与 wemm 相同：**API 进程不 import torch**。侧车默认 8103，bge 仍复用 8101。

**失败语义与 wemm 完全一致**：任何失败返回空表、不抛；连续 5 次失败熔断 10 秒；
健康检查 10 秒 TTL。上层据此把通道标记为 ``degraded``，而不是让每次查询都挂在一个
可选依赖上。

**维度校验是必须的**：1.0 的 bge 是 1024、chinese-clip 是 768，而 2.0 的 wemm 是
2048。放行错维度会让向量带着**正确的 model_name** 落库，检索侧不报错、只是永远
算不出相似度——这是这一层最贵的失败形态（见 ``wemm.py`` 的同一条注释）。
"""

from __future__ import annotations

import math
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from sentrix.domain.retrieval import scheme

#: 1.0 的 10 秒断开（``bge_text.py:98-102``）。
CIRCUIT_OPEN_S = 10.0
HEALTH_TTL_S = 10.0
FAILURE_THRESHOLD = 5


class _SidecarEmbedder:
    """两个适配器共用的熔断器 / 健康探针 / HTTP 失败口径。"""

    def __init__(
        self,
        base_url: str,
        *,
        model_id: str,
        dimension: int,
        timeout_s: float,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model_id = model_id
        self._dimension = dimension
        self._timeout_s = timeout_s
        self._client = client or httpx.Client(timeout=timeout_s)
        # 熔断器是进程内可变状态；uvicorn 的同步端点在线程池里跑。
        self._lock = threading.Lock()
        self._failures = 0
        self._tripped_at = 0.0
        self._health_checked_at = 0.0
        self._health_value = False
        self._error = ""
        # Evidence belongs to this caller, never to another concurrent query.
        self._attempt = threading.local()

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def load_error(self) -> str:
        """最近一次失败的原因。与 ``available`` 分开，理由同 ``wemm.py``。"""
        return self._error

    @property
    def last_operational_issue(self) -> dict[str, Any] | None:
        """Structured evidence for the most recent embedding call on this thread."""
        value = getattr(self._attempt, "issue", None)
        return {**value, "request_policy": dict(value["request_policy"])} if value else None

    def _attempt_failed(self, code: str, cause: str, **facts: Any) -> None:
        self._attempt.issue = {
            "error_code": code,
            "failure_code": cause,
            "request_policy": {"request_timeout_s": self._timeout_s},
            **facts,
        }

    @property
    def available(self) -> bool:
        with self._lock:
            if self._tripped_at and time.monotonic() - self._tripped_at < CIRCUIT_OPEN_S:
                return False
            now = time.monotonic()
            if now - self._health_checked_at < HEALTH_TTL_S:
                return self._health_value
        try:
            response = self._client.get(f"{self._base_url}/health")
            healthy = response.status_code == 200
        except Exception:  # noqa: BLE001 - 探活失败就是不可用，不该让调用方处理异常
            healthy = False
        with self._lock:
            self._health_checked_at = time.monotonic()
            self._health_value = healthy
        return healthy

    def _embed(self, endpoint: str, payload: dict[str, str], what: str) -> list[float]:
        self._attempt.issue = None
        if not any(value.strip() for value in payload.values()):
            # 空输入不是侧车的问题；记进熔断计数会让空查询把整条通道断开。
            return []
        with self._lock:
            if self._tripped_at and time.monotonic() - self._tripped_at < CIRCUIT_OPEN_S:
                self._attempt_failed("provider_unavailable", "circuit_open")
                return []
        try:
            values = self._request_values(endpoint, payload)
        except Exception as error:  # noqa: BLE001 - 见模块注释：降级，不抛
            code, cause = "resource_error", "embedding_invalid_response"
            facts: dict[str, Any] = {"error_type": type(error).__name__}
            if isinstance(error, httpx.TimeoutException):
                code, cause = "time_limit", "embedding_timeout"
                facts["request_outcome_unknown"] = not isinstance(
                    error, (httpx.ConnectTimeout, httpx.PoolTimeout)
                )
            elif isinstance(error, httpx.HTTPStatusError):
                code, cause = "provider_rejected", "embedding_http_status"
                facts["http_status"] = error.response.status_code
            elif isinstance(error, httpx.RequestError):
                code, cause = "provider_unavailable", "embedding_transport_error"
                facts["request_outcome_unknown"] = not isinstance(error, httpx.ConnectError)
            self._attempt_failed(code, cause, **facts)
            self._record_failure(f"{type(error).__name__}: {error}", what)
            return []
        if not values:
            self._attempt_failed("resource_error", "empty_vector")
            self._record_failure("侧车返回了空向量", what)
            return []
        if len(values) != self._dimension:
            self._attempt_failed("resource_error", "dimension_mismatch")
            self._record_failure(f"侧车返回 {len(values)} 维，方案声明 {self._dimension} 维", what)
            return []
        if not all(math.isfinite(value) for value in values):
            self._attempt_failed("resource_error", "non_finite_vector")
            self._record_failure("嵌入含非有限数值", what)
            return []
        self._record_success()
        return values

    def _request_values(self, endpoint: str, payload: dict[str, str]) -> list[float]:
        response = self._client.post(f"{self._base_url}{endpoint}", json=payload)
        response.raise_for_status()
        return [float(item) for item in (response.json().get("vector") or [])]

    def _record_failure(self, reason: str, what: str) -> None:
        with self._lock:
            self._failures += 1
            self._error = f"{what}: {reason}"
            self._health_value = False
            self._health_checked_at = 0.0
            if self._failures >= FAILURE_THRESHOLD:
                self._tripped_at = time.monotonic()
                self._failures = 0

    def _record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._tripped_at = 0.0
            self._health_value = True


class ChineseClipSidecarEmbedder(_SidecarEmbedder):
    """实现 ``VisualEmbedder``：``embed_image`` + ``embed_query``（768 维）。"""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 30.0,
        model_id: str = scheme.LEGACY_VISUAL_MODEL,
        client: httpx.Client | None = None,
    ) -> None:
        super().__init__(
            base_url,
            model_id=model_id,
            dimension=scheme.LEGACY_VISUAL_DIMENSION,
            timeout_s=timeout_s,
            client=client,
        )

    def embed_image(self, path: Path) -> list[float]:
        return self._embed("/embed_image", {"path": str(path or "")}, "embed_image")

    def embed_query(self, text: str) -> list[float]:
        return self._embed("/embed_query", {"text": str(text or "")}, "embed_query")


class BgeSidecarEmbedder(_SidecarEmbedder):
    """实现 ``TextEmbedder``：``embed_query``（1024 维）。

    优先1.0 ``POST /embed``；仅404时协商OpenAI兼容接口。若服务明确报告
    pooling=none，BGE-M3原生token矩阵按官方CLS + L2规则转换为dense向量。
    协议协商有界，其他HTTP错误不触发二次请求，成功后缓存协议。
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 10.0,
        model_id: str = scheme.LEGACY_TEXT_MODEL,
        client: httpx.Client | None = None,
    ) -> None:
        super().__init__(
            base_url,
            model_id=model_id,
            dimension=scheme.LEGACY_TEXT_DIMENSION,
            timeout_s=timeout_s,
            client=client,
        )
        self._protocol = "sidecar"

    def _request_values(self, endpoint: str, payload: dict[str, str]) -> list[float]:
        if self._protocol == "llama_cpp_cls":
            return self._request_native_cls(payload["text"])
        if self._protocol == "sidecar":
            try:
                return super()._request_values(endpoint, payload)
            except httpx.HTTPStatusError as error:
                if error.response.status_code != 404:
                    raise
        response = self._client.post(f"{self._base_url}/v1/embeddings", json={"input": payload["text"]})
        if (
            response.status_code == 400
            and self._model_id == scheme.LEGACY_TEXT_MODEL
            and (response.json().get("error") or {}).get("message")
            == "Pooling type 'none' is not OAI compatible. Please use a different pooling type"
        ):
            values = self._request_native_cls(payload["text"])
            self._protocol = "llama_cpp_cls"
            return values
        response.raise_for_status()
        rows = response.json().get("data") or []
        values = [float(value) for value in (rows[0].get("embedding") or [])] if len(rows) == 1 else []
        if len(values) == self._dimension and all(math.isfinite(value) for value in values):
            self._protocol = "openai"
        return values

    def _request_native_cls(self, text: str) -> list[float]:
        response = self._client.post(f"{self._base_url}/embedding", json={"content": text})
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list) or len(rows) != 1 or rows[0].get("index") != 0:
            raise ValueError("BGE-M3原生接口必须返回一条输入的token矩阵")
        tokens = rows[0].get("embedding") or []
        if not tokens or not isinstance(tokens[0], list) or len(tokens[0]) != self._dimension:
            raise ValueError("BGE-M3原生CLS维度与方案不符")
        # FlagEmbedding M3: last_hidden_state[:, 0], then F.normalize(dim=-1).
        # llama.cpp explicitly ignores embd_normalize for pooling=none.
        cls = [float(value) for value in tokens[0]]
        norm = math.hypot(*cls)
        if not math.isfinite(norm) or norm == 0:
            raise ValueError("BGE-M3原生CLS必须为有限非零向量")
        return [value / norm for value in cls]

    def embed_query(self, text: str) -> list[float]:
        return self._embed("/embed", {"text": str(text or "")}, "embed_query")


class LegacyEmbeddingHealthProbe:
    """两段都可用才算可用。

    **不能用「任一可用」**：legacy 方案的图文是两个独立空间，缺任何一段都意味着
    一半检索通道消失。报成 ok 会让「图片检索全空」在运维侧不可见——这正是
    ``/health`` 引入嵌入探针时要解决的那类问题。
    """

    def __init__(self, visual: _SidecarEmbedder, text: _SidecarEmbedder) -> None:
        self._visual = visual
        self._text = text

    @property
    def available(self) -> bool:
        return self._visual.available and self._text.available

    @property
    def load_error(self) -> str:
        errors = []
        if not self._visual.available:
            errors.append(f"visual: {self._visual.load_error or 'unavailable'}")
        if not self._text.available:
            errors.append(f"text: {self._text.load_error or 'unavailable'}")
        return "; ".join(errors)
