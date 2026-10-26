"""WeMM-Embedding-2B：统一多模态嵌入的 HTTP 客户端（W1）。

对应 1.0 ``backend/embeddings/wemm_sidecar.py``。**模型不在这个进程里**——侧车跑在
8102，与 8100 的 VLM、8101 的 bge 同一条口径：2.0 不管理模型进程的生命周期（设计 §2.1），
只连它。本进程因此不 import ``torch``。

**侧车的启动脚本在 1.0 的仓库里**：``Sentrix-Home-Web/scripts/maintenance/
wemm_embedder_sidecar.py``，用那个仓库的隔离 venv 跑（``.venv-wemm``；WeMM 要
``transformers==5.2.0``，与 2.0 venv 里的版本不兼容，所以它必须是独立环境）：

    cd ~/Github/Sentrix-Home-Web
    PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \\
      nohup .venv-wemm/bin/python scripts/maintenance/wemm_embedder_sidecar.py --port 8102 &

**这条路径原先写的是 2.0 自己的 ``scripts/maintenance/wemm_embedder_sidecar.py``，那个
文件不存在**——2026-10-09 侧车挂掉（10-08 10:45）、要把它拉起来时，按这句话去找是找不到
的。``PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`` 不是可选的调优：不设时 PyTorch
的缓存分配器会攥住约 4.3 GB 不放，而那部分显存对同一张卡上的 8100 VLM 是不可见的。

**它是统一空间，所以一个类同时满足两个端口。** 文本、图像、视频落在**同一个 2048 维
空间**里，跨模态检索原生成立——不再需要「文本塔去对齐图像空间」那种间接关系。
``VisualEmbedder`` 要 ``embed_image`` + ``embed_query``，``TextEmbedder`` 只要
``embed_query``，这里四个成员一次给全。容器里**两个槽指向同一个实例**：分别建两个
不只是浪费（侧车那侧显存翻倍），还会让同一个模型有两套健康状态与熔断计数。

**失败要降级，不能抛、不能挂。** 与 ``BgeM3TextEmbedder`` 同一条：连续 5 次失败断开
30 秒，期间直接返回空表；健康检查带 10 秒 TTL，不是每次调用都打一次。返回空表让上层
按「这台机器没有嵌入能力」降级——**抛出去会让每一次查询都挂在这个可选依赖上**。

**超时 60 秒，比 bge 的 10 秒长得多，这是照 1.0 的值**（``wemm_sidecar.py:55``）：
2B 参数的模型，一次图像编码本来就慢一个量级。

**``available`` 与 ``load_error`` 的分工同 ``BgeM3TextEmbedder``**：能不能用是一个可问的
问题，为什么不能用是另一个；合成一个布尔值，排查时只剩「不能用」三个字。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import httpx

from sentrix.domain.retrieval import scheme

# 模型名与维度只在 ``scheme`` 里写一遍——那正是 ``scheme.py`` 存在的理由
# （写入 / 查询 / 重建索引三处唯一的来源）。**维度不一致在检索侧不报错**，
# 只是永远算不出相似度，所以这里绝不能出现第二个字面量。
DEFAULT_MODEL_ID = scheme.WEMM_MODEL
DIMENSION = scheme.WEMM_DIMENSION

# 1.0 ``wemm_sidecar.py:55`` 的超时。
DEFAULT_TIMEOUT_S = 60.0
# 1.0 ``wemm_sidecar.py:61`` 的健康检查 TTL。
HEALTH_TTL_S = 10.0
# 1.0 ``wemm_sidecar.py:110-112``：连续失败到这个数就断开。
FAILURE_THRESHOLD = 5
# 1.0 ``wemm_sidecar.py:72`` 的断开时长。
CIRCUIT_OPEN_S = 30.0


class WeMMSidecarEmbedder:
    """实现 ``domain.retrieval.ports.VisualEmbedder`` 与 ``TextEmbedder``。"""

    def __init__(
        self,
        base_url: str,
        *,
        model_id: str = DEFAULT_MODEL_ID,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model_id = model_id
        self._timeout_s = timeout_s
        self._client = client or httpx.Client(timeout=timeout_s)
        # 熔断器与健康缓存都是进程内可变状态，而 uvicorn 的同步端点在线程池里跑。
        self._lock = threading.Lock()
        self._failures = 0
        self._tripped_at = 0.0
        self._health_checked_at = 0.0
        self._health_value = False
        self._error = ""

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dimension(self) -> int:
        return DIMENSION

    @property
    def load_error(self) -> str:
        """最近一次失败的原因。**与 ``available`` 分开**：合成一个布尔值，
        「侧车没起」与「侧车起了但模型没加载」就分不开了。"""
        return self._error

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
            error = "" if healthy else (self._error or f"health returned HTTP {response.status_code}")
        except Exception as exc:  # noqa: BLE001 - 探活失败就是不可用，不该让调用方处理异常
            healthy = False
            error = self._error or str(exc)
        with self._lock:
            self._health_checked_at = time.monotonic()
            self._health_value = healthy
            self._error = error
        return healthy

    def embed_query(self, text: str) -> list[float]:
        """一段文本 → 2048 维向量；不可用时返回空表（不抛）。

        用 ``POST /embed_text {"text": ...}``——1.0 ``wemm_sidecar.py:139`` 的形状。
        侧车另有一个 ``/embed_batch``，但 1.0 的客户端没用它（服务端注释说明批处理会
        因每张图 patch 数不同而 pad，不值得），这里也不引入。
        """
        return self._embed("/embed_text", {"text": str(text or "")}, "embed_text")

    def embed_image(self, path: Path) -> list[float]:
        """一张图 → 2048 维向量；不可用时返回空表（不抛）。

        ``POST /embed_image {"path": ...}``（1.0 ``wemm_sidecar.py:145``）。
        **传的是路径不是字节**：侧车与后端在同一台机器上，1.0 就是这么传的；
        文件不存在时侧车返回 400，这里按普通失败计数。
        """
        return self._embed("/embed_image", {"path": str(path or "")}, "embed_image")

    # ---- 内部 -----------------------------------------------------------------

    def _embed(self, endpoint: str, payload: dict[str, str], what: str) -> list[float]:
        """空输入直接返回空表——**不计入失败**。

        与两条降级路径分开：调用方给了空文本不是侧车的问题，记进熔断计数会让
        「用户问了个空问题」把整条嵌入通道断开。
        """
        if not any(value.strip() for value in payload.values()):
            return []
        with self._lock:
            if self._tripped_at and time.monotonic() - self._tripped_at < CIRCUIT_OPEN_S:
                return []
        try:
            response = self._client.post(f"{self._base_url}{endpoint}", json=payload)
            response.raise_for_status()
            values = [float(item) for item in (response.json().get("vector") or [])]
        except Exception as error:  # noqa: BLE001 - 见模块注释：降级，不抛
            self._record_failure(f"{type(error).__name__}: {error}", what)
            return []
        if not values:
            self._record_failure("侧车返回了空向量", what)
            return []
        if len(values) != DIMENSION:
            # **维度不对是配置错，不是抖动。** 放过去的话，向量会带着正确的
            # model_name 落库，而检索侧算不出相似度——1.0 ``bge_text.py`` 所在
            # 那一族的模块注释把这种情形列为最难查的一类。
            self._record_failure(f"侧车返回 {len(values)} 维，方案声明 {DIMENSION} 维", what)
            return []
        self._record_success()
        return values

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
