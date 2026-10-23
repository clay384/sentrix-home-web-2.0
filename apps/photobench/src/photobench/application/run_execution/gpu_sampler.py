"""GPU 采样线程：1.0 ``GpuSampler``（``:1688``）的照搬。

**它为什么在 application 而不是 infrastructure。** 它自己没有 IO——所有系统调用都在
``TelemetryProvider`` 端口后面（那个端口由 ``infrastructure.runtime.providers`` 实现）。
它做的是一件用例层的事：**在 run 进行期间按固定间隔把遥测拉下来并交给回调**。
``photobench-application-purity`` 禁的是 import infrastructure，不是禁线程。

**它是 ``TelemetryProvider.gpu_stats`` / ``process_memory`` / ``system_memory`` 三个
方法的唯一生产调用者**——S9a 落地这三个方法时没有任何调用者，S9b-1 复查时它们仍然
是 method-reachability 的盲区（因为 provider 自己的实现里有同名 ``self.*`` 调用，
门禁判成「已接线」）。这里接上之后，改坏这三个方法名会让 ``tests/test_gpu_sampler.py``
红。

**样本的字段映射逐行照搬，不做表驱动**：那 40 多行里混着三种东西——同名直取、
改名映射（``processes`` → ``model_processes``）、以及 ``vllm_metrics`` /
``memory_profile`` 两层嵌套的展开，还有 `or []` 与 `or sample[...]` 两种兜底。
抽成一张表会把这些差异压成一个形状，而它们恰好是差分对照要逐字段比对的东西。

**三处与 1.0 的差别，都是「2.0 没有了那个前提」：**

1. ``hasattr(self.provider, "system_memory")`` 的那层探测去掉——2.0 的
   ``TelemetryProvider`` 协议里它一定在（S9a 的端口定义），留一个 hasattr 只会让
   「协议少了一个方法」这件事在运行时静默通过。
2. ``aggregate()`` 在 S9b-3 补上：它**委托给纯函数**
   ``domain.runs.analysis.aggregate_gpu_samples``（1.0 ``:1785``），自己只负责把
   ``self.samples`` 递过去。留在采样器上的理由只有一个——调用方
   （``_phase_gpu_metrics``）手上就是采样器对象，让它去 import 一个 domain 函数会
   把「采样的状态」与「聚合的算法」的边界画反。
3. ``samples`` 累积在内存里（1.0 亦然），聚合与实时视图都读它。
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from typing import Any

from photobench.domain.runs.analysis import aggregate_gpu_samples
from photobench.domain.runtime.ports import TelemetryProvider

#: 采样行会带上相位标签，读侧就不必去 run.json 里反查「这条样本属于哪一段」。
#: 1.0 ``_persist_gpu_sample``（``:2113``）的那张表，逐字照搬（含中文标签）。
PHASE_LABELS: dict[str, str] = {
    "model_deploy": "模型加载",
    "scope_setup": "创建相册",
    "scope_attach": "绑定相册",
    "identity_seed": "身份预置",
    "photo_import": "数据导入",
    "pipeline_processing": "相册流水线预处理",
    "qa_eval": "QA 测评",
    "gpu_metrics": "资源指标收尾",
    "aggregate": "结果汇总",
    "unassigned": "未分配阶段",
}


class GpuSampler:
    """按间隔轮询设备与被管模型的内存；一个采样点一次回调。

    ``on_sample`` 的异常**被吞掉**（1.0 ``:1767`` 的 ``try/except``）：采样是增强，
    一次写盘失败不该让整条采样线程死掉——那会让后面的曲线整个缺一段，而缺一段的
    原因（磁盘满）与被测对象毫无关系。
    """

    def __init__(
        self,
        provider: TelemetryProvider,
        interval: float = 0.5,
        on_sample: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.provider = provider
        self.interval = interval
        self.on_sample = on_sample
        self.samples: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop.clear()
        self.samples = []
        self._thread = threading.Thread(target=self._run, daemon=True, name="gpu-sampler")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def sample_once(self) -> None:
        """采一个点。

        **抽出来只为让用例不必起线程**：没有它，这个类的用例只能靠 ``sleep`` 等线程
        跑一轮——那种用例既慢，又在负载高的机器上随机红，而它要断言的东西（样本的
        字段映射）与线程毫无关系。``_run`` 与它共用同一段逻辑，不存在第二条路径。
        """
        gpu_result = self.provider.gpu_stats()
        memory_result = self.provider.process_memory()
        system_result = self.provider.system_memory()
        if gpu_result.get("status") != "available":
            return
        data = gpu_result.get("data") or {}
        # 进程归属拿不到时**仍然保留进程与系统内存**——绝不把「没找到模型进程」
        # 变成一个看起来有意义的 0（1.0 ``:1713-1716`` 的注释）。
        process_memory = memory_result.get("data") or {}
        system_memory = system_result.get("data") or {}
        timestamp = time.perf_counter()
        for gpu in data.get("gpus", []):
            sample = dict(gpu)
            if sample.get("cpu_temperature_c") is None and data.get("cpu_temperature_c") is not None:
                sample["cpu_temperature_c"] = data["cpu_temperature_c"]
            sample["_t"] = timestamp
            sample["model_process_memory_used_mib"] = process_memory.get("process_memory_used_mib")
            sample["model_process_system_memory_used_mib"] = process_memory.get(
                "model_process_system_memory_used_mib"
            )
            sample["benchmark_process_memory_used_mib"] = process_memory.get(
                "benchmark_process_memory_used_mib"
            )
            sample["benchmark_process_gpu_memory_mib"] = process_memory.get(
                "benchmark_process_gpu_memory_mib"
            )
            sample["other_processes_memory_mib"] = process_memory.get("other_processes_memory_mib")
            sample["all_processes_memory_mib"] = process_memory.get("all_processes_memory_mib")
            sample["system_memory_used_mib"] = system_memory.get("system_memory_used_mib")
            sample["system_memory_total_mib"] = system_memory.get("system_memory_total_mib")
            sample["system_memory_available_mib"] = system_memory.get("system_memory_available_mib")
            sample["system_memory_scope"] = system_memory.get("system_memory_scope")
            sample["system_memory_delta_mib"] = process_memory.get("system_memory_delta_mib")
            sample["system_memory_baseline_mib"] = process_memory.get("system_memory_baseline_mib")
            sample["llama_server_args"] = process_memory.get("llama_server_args")
            sample["sentrix_stack_pss_mib"] = process_memory.get("sentrix_stack_pss_mib")
            sample["product_stack_memory_mib"] = process_memory.get("product_stack_memory_mib")
            sample["memory_scope"] = process_memory.get("memory_scope") or sample.get("memory_scope")
            sample["process_memory_scope"] = process_memory.get("process_memory_scope")
            sample["model_process_system_memory_scope"] = process_memory.get(
                "model_process_system_memory_scope"
            )
            sample["benchmark_process_memory_scope"] = process_memory.get("benchmark_process_memory_scope")
            sample["benchmark_process_gpu_memory_scope"] = process_memory.get(
                "benchmark_process_gpu_memory_scope"
            )
            sample["process_attribution"] = process_memory.get("process_attribution")
            sample["all_processes_scope"] = process_memory.get("all_processes_scope")
            sample["model_processes"] = process_memory.get("processes") or []
            sample["model_system_processes"] = process_memory.get("model_system_processes") or []
            sample["benchmark_processes"] = process_memory.get("benchmark_processes") or []
            sample["benchmark_gpu_processes"] = process_memory.get("benchmark_gpu_processes") or []
            sample["other_processes"] = process_memory.get("other_processes") or []
            sample["all_processes"] = process_memory.get("all_processes") or []
            sample["model_process_memory_limit_mib"] = process_memory.get("process_memory_limit_mib")
            sample["model_process_memory_over_limit"] = process_memory.get("process_memory_over_limit")
            sample["model_process_pid"] = process_memory.get("root_pid")
            # Jetson 的 CPU/GPU 拆分三字段(xy 2026-10-20 #2):jetson_telemetry 已产出,
            # 但这里不接住就在存盘时被丢弃——46 上模型总占用会凭空少 79%(2617 MiB)。
            # 三处清单的另一处在 analysis 的曲线清单,少一处=静默丢字段。
            sample["model_process_host_memory_mib"] = process_memory.get("model_process_host_memory_mib")
            sample["model_process_gpu_memory_mib"] = process_memory.get("model_process_gpu_memory_mib")
            sample["model_process_gpu_memory_source"] = process_memory.get("model_process_gpu_memory_source")
            sample["memory_unit"] = process_memory.get("memory_unit") or sample.get("memory_unit")
            sample["source"] = gpu_result.get("source") or self.provider.__class__.__name__
            sample["pss_sampled_at_monotonic"] = process_memory.get("pss_sampled_at_monotonic")
            sample["kv_cache_used_tokens"] = process_memory.get("kv_cache_used_tokens")
            vllm_metrics = process_memory.get("vllm_metrics") or {}
            sample["kv_cache_usage_pct"] = vllm_metrics.get("kv_cache_usage_pct")
            memory_profile = process_memory.get("memory_profile") or {}
            sample["kv_cache_capacity_gib"] = memory_profile.get("kv_cache_capacity_gib")
            sample["kv_cache_capacity_tokens"] = memory_profile.get("kv_cache_capacity_tokens")
            sample["weight_gib"] = memory_profile.get("weight_gib")
            sample["peak_activation_gib"] = memory_profile.get("peak_activation_gib")
            sample["non_torch_gib"] = memory_profile.get("non_torch_gib")
            sample["cuda_graph_gib"] = memory_profile.get("cuda_graph_gib")
            self.samples.append(sample)
            if self.on_sample:
                # 回调失败吞掉：采样是增强，见类注释（1.0 ``:1767``）。
                with contextlib.suppress(Exception):
                    self.on_sample(sample)

    def aggregate(self) -> dict[str, Any]:
        """把已累积的样本收成曲线统计（1.0 ``GpuSampler.aggregate`` ``:1785``）。

        算法在 ``domain.runs.analysis.aggregate_gpu_samples``——**采样器只做转发**，
        这样聚合逻辑能在一个没有线程、没有 provider 的用例里逐字段被钉住。
        """
        return aggregate_gpu_samples(self.samples)

    def _run(self) -> None:
        while not self._stop.is_set():
            # 1.0 ``:1782``：一次探不到不该终止采样。
            with contextlib.suppress(Exception):
                self.sample_once()
            self._stop.wait(self.interval)
