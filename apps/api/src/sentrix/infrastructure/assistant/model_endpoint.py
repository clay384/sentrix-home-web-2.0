"""问 OpenAI 兼容端点：你现在服务的是哪个模型名。

**为什么必须问。** 2.0 连的是 8100 上的对话/视觉端点，而 8100 上跑什么由部署方决定：
llama.cpp 的 ``--alias`` 与 vLLM 的 ``--served-model-name`` 各写各的名字。两个后端**协议
完全相同**（都是 ``/v1/chat/completions``），差别只在一处——**名字校不校验**：

- llama.cpp **不校验**模型名，随便传一个都能用；
- vLLM **校验**，名字对不上直接 404 ``The model `X` does not exist.``

于是把名字写成常量，等于把这个客户端钉死在「写常量时的那个后端」上，而**换后端是运维
动作，代码这边一点动静都没有**。2026-09-24 实测：有人把 8100 从 llama.cpp 换成 vLLM
（``gemma4-12b-it``），2.0 配的 ``gemma4-qat-iq3xxs`` 立刻 404 → 规划失败 → 用户看到
「当前问题的证据需求无法可靠规划」。

**1.0 怎么做的，以及为什么这里不照抄。** 1.0 的答案是 ``SENTRIX_LLM_BACKEND`` 开关加每个
后端一份默认名字：``backend/model_clients.py:523`` 读这个开关，``:539`` 是 ``openai`` 的默认
（``gemma4-12b-it``），``:547`` 是 ``ollama`` 的默认（``gemma4:12b``）。那解决的是**协议不同**
的问题——``OllamaBackend``（``:295``）走 ``/api/chat``（``:342``），payload 形状也不一样。
这里的两个后端协议一模一样，再用开关就是拿配置去追运行时的状态——正是 ``config.py`` 点名
的那种坏法
（「1.0 的失败模式是 ~126 env flags whose values disagreed with each other and with the
code」）。所以问端点自己：两者都实现了 OpenAI 的 ``GET /v1/models``。

**形状照 1.0 已有的先例**：``OpenAICompatibleInferenceProvider.list_models``
（``backend/runtime_providers.py:432-440``）取的就是 ``body["data"][].id``。区别在于 1.0 只把
它当健康检查的副产物列出来、用哪个名字仍由配置决定；这里把它当作**唯一**的名字来源。

**探测失败一律抛 ``ModelServiceError``，不猜、退回常量、不静默。** 拿一个猜出来的名字去
问，得到的还是一个 404，只是把真因埋深一层；静默降级则会让「端点挂了」变成「模型不会」。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import httpx

from sentrix.domain.assistant.ports import ModelServiceError

# 探测是一次本地 HTTP 往返，不是推理：10 秒足够。超时本身就说明端点没在正常服务。
PROBE_TIMEOUT_S = 10.0

# 缓存有效期。取 60 秒是因为两边都不亏：
# - 每个回合至少一次规划 + N 次主循环，每次都探会把请求数翻倍，纯浪费；
# - 换后端是运维动作，不是每秒发生的事。
# TTL 不是唯一的失效路径——真换后端的那一刻，撞上 404 的请求会 ``invalidate()`` 后重探
# （见 ``chat_client.OpenAIChatModel.complete``），所以「换后端」的可见窗口不是 60 秒，
# 而是「后端换完之后的第一个请求」。
PROBE_TTL_S = 60.0


class ServedModel:
    """端点当前服务的模型名，带进程内缓存。

    **线程安全**：uvicorn 的同步端点在线程池里跑，多个回合会并发调用 ``current()``。
    锁只保护缓存字段，HTTP 探测在锁外做——探测最坏要 10 秒，握着锁等会把并发请求串起来。
    代价是缓存未命中时可能有两个线程同时探一次；结果一样，多花的是一次本地往返，
    比串行化整个端点便宜。
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = PROBE_TIMEOUT_S,
        ttl_s: float = PROBE_TTL_S,
        monotonic: Callable[[], float] = time.monotonic,
        generation_profile: str = "legacy",
        native_output_budget: bool = False,
    ) -> None:
        if generation_profile not in {"legacy", "qwen35_nonthinking"}:
            raise ValueError("unknown generation profile")
        self._generation_profile = generation_profile
        self.native_output_budget = native_output_budget
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._ttl_s = ttl_s
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._name = ""
        self._expires_at = 0.0

    def generation_parameters(self) -> dict[str, object]:
        """Explicit sampling policy shared by every local model adapter."""
        if self._generation_profile == "qwen35_nonthinking":
            return {
                "temperature": 0.7,
                "seed": 42,
                "top_p": 0.8,
                "top_k": 20,
                "min_p": 0.0,
                "presence_penalty": 1.5,
                "chat_template_kwargs": {"enable_thinking": False},
            }
        return {"temperature": 0.0}

    @property
    def base_url(self) -> str:
        return self._base_url

    def current(self) -> str:
        """当前在服务的模型名。缓存有效就直接给，否则重新探。"""
        with self._lock:
            if self._name and self._monotonic() < self._expires_at:
                return self._name

        name = self._probe()
        with self._lock:
            self._name = name
            self._expires_at = self._monotonic() + self._ttl_s
        return name

    def invalidate(self) -> None:
        """作废缓存。

        请求撞上「模型名不存在」时调用：那个 404 是**端点自己**在说「你手里的名字过时了」，
        比 TTL 到点更可信。作废之后下一次 ``current()`` 重新探。
        """
        with self._lock:
            self._name = ""
            self._expires_at = 0.0

    def _probe(self) -> str:
        try:
            response = httpx.get(f"{self._base_url}/models", timeout=self._timeout_s)
        except httpx.HTTPError as error:
            raise ModelServiceError(f"模型端点 {self._base_url} 不可达：{error}") from error

        if response.status_code != 200:
            raise ModelServiceError(
                f"模型端点 {self._base_url} 的 /models 返回 {response.status_code}：{response.text[:200]}"
            )
        try:
            body = response.json()
        except ValueError as error:
            raise ModelServiceError(
                f"模型端点 {self._base_url} 的 /models 不是 JSON：{response.text[:200]}"
            ) from error

        names = served_names(body)
        if not names:
            raise ModelServiceError(f"模型端点 {self._base_url} 上没有任何模型在服务")
        if len(names) > 1:
            # 不挑一个「看起来对」的：挑错了就是 404，而且没有任何依据说该挑哪个。
            raise ModelServiceError(
                f"模型端点 {self._base_url} 同时服务多个模型（{'、'.join(names)}），无法判断该用哪一个"
            )
        return names[0]


def served_names(body: object) -> list[str]:
    """``/v1/models`` 响应里的模型 id 列表：保持端点给的顺序，去重。

    只有 ``data[].id`` 参与判断，其余字段一律不看（1.0 ``runtime_providers.py:436-439``
    同一条口径）。去重是因为同一个名字出现多次（多副本 vLLM）不该被当成「多于一个模型」。
    """
    if not isinstance(body, dict):
        return []
    data = body.get("data")
    if not isinstance(data, list):
        return []

    names: list[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = item.get("id")
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    return names


def probe_context_tokens(base_url: str, *, timeout: float = PROBE_TIMEOUT_S) -> int | None:
    """端点上**一个并发槽**能吃多少 token。读不到返回 ``None``。

    **为什么必须问，而不是写常量。** 窗口是**部署事实**：``Settings.vlm_context_tokens``
    里那个 6144 是照某一次 ``--ctx-size 24576 ÷ --parallel 4`` 抄的，部署一改它就过期。
    代价实测过——2026-10-10 那次 full 导入里**家庭图谱推断崩了两次**
    （``family_analysis_runs``：``提示词约 6568 token，单个并发槽的上下文上限 6144``），
    三张 ``family_*`` 表**一个字都没写进库**，关系类题因此全线失守；同一轮另有 42 道题
    报 ``prompt_too_long``。部署侧后来提到了 8192/槽，而代码里还是 6144。

    两条路，因为两个后端把它报在不同地方：

    - **vLLM**：``GET /v1/models`` 的 ``data[].max_model_len``——它本来就是**每请求**的上限；
    - **llama.cpp**：``GET /props`` 的 ``default_generation_settings.n_ctx``。
      llama.cpp 的 ``--ctx-size`` 是整台服务的总窗口，要除以 ``--parallel`` 才是单槽的，
      而 ``/props`` 报的**已经是除过的那个数**（2026-10-16 在 153 实测：
      ``--ctx-size 32768 --parallel 4`` → ``n_ctx = 8192``，同时 ``total_slots = 4``）。

    **读不到返回 ``None`` 而不是抛错**：调用方拿配置值兜底并留一行警告即可——
    这与 ``ServedModel`` 不同，那个探测失败会让请求 404，是硬故障；窗口读不到只是
    退回一个可能过期的保守值。
    """
    base = base_url.rstrip("/")
    # vLLM 那一支。
    try:
        body = httpx.get(f"{base}/models", timeout=timeout).json()
        for item in (body or {}).get("data") or []:
            if not isinstance(item, dict):
                continue
            value = item.get("max_model_len")
            if isinstance(value, int) and value > 0:
                return value
    except (httpx.HTTPError, ValueError, TypeError):
        pass

    # llama.cpp 那一支。``/props`` 挂在根上，不在 ``/v1`` 下。
    root = base[:-3] if base.endswith("/v1") else base
    try:
        props = httpx.get(f"{root}/props", timeout=timeout).json()
        settings = (props or {}).get("default_generation_settings") or {}
        value = settings.get("n_ctx")
        if isinstance(value, int) and value > 0:
            return value
    except (httpx.HTTPError, ValueError, TypeError):
        pass
    return None
