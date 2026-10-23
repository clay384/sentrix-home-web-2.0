"""PhotoBench 的模型运行时提供者：推理（必需）+ 生命周期 / 遥测（可选）。

移植自 1.0 ``backend/runtime_providers.py``（869 行，基线提交 2d2cee7c）。
**照搬全量**是用户 2026-10-04 的裁决①——1.0 有哪些 provider、各自的探活口径、超时、
失败形态，这里逐条对齐，不按「2.0 不管理模型进程生命周期」自行裁剪。

**整份落在 infrastructure 不是偏好。** 这些类要起子进程（``nvidia-smi`` / ``ssh``）、
读 ``/proc`` 与 ``/sys``、打 HTTP 探活——``domain-purity`` / ``application-purity`` /
``infrastructure-purity`` 三条 import-linter 契约都不允许它们出现在 domain 或
application。协议在 ``photobench.domain.runtime.ports``。

**与 apps/api 的关系：没有关系。** 实施设计 §145 定死「PhotoBench 不得读取主产品
Python 内部模块」，所以这份文件里一个 ``sentrix.*`` 的 import 都没有。

**1.0 的基类在这里不用了。** 1.0 让实现类继承 ``InferenceProvider`` /
``TelemetryProvider`` 之类的 ABC；2.0 换成 domain 的 Protocol，实现类**不继承**
（结构化类型，与 ``apps/api`` 的仓储 / 适配器同一条口径）。ABC 上那些
``raise NotImplementedError`` 的空实现随之消失——协议本身没有默认实现。

**1.0 的两个 import 没有搬过来**：``backend.jetson_telemetry`` 的
``LocalJetsonLlamaCppTelemetryProvider`` / ``is_jetson_host`` 在 1.0 的
``runtime_providers.py`` 里**只被 import、从未被使用**（ruff 的 F401 会当场报红）。
Jetson 那一路提供者是另一个 1.0 模块（``backend/jetson_telemetry.py``，660 行），
它由编排侧的 ``select_runtime_providers`` 直接构造，不经本文件。
"""

from __future__ import annotations

import contextlib
import csv
import io
import os
import re
import subprocess
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from photobench.domain.runtime.ports import LifecycleProvider, RuntimeProviders, TelemetryProvider
from photobench.infrastructure.runtime.hardware import read_cpu_temperature_c
from photobench.infrastructure.runtime.jetson_telemetry import unified_model_memory


def normalize_service_url(value: str | None) -> str:
    """把 ``host:port`` 补成 ``http://host:port`` 并去掉尾斜杠。空值原样返回空串。"""
    value = str(value or "").strip().rstrip("/")
    if value and not value.startswith(("http://", "https://")):
        value = f"http://{value}"
    return value


def normalize_openai_base_url(value: str | None) -> str:
    """补上 OpenAI 约定的 ``/v1`` 段；已经有 ``/v2`` 之类版本段就不再补。"""
    value = normalize_service_url(value)
    if not value:
        return ""
    return value if re.search(r"/v\d+$", value, flags=re.IGNORECASE) else f"{value}/v1"


def _configured_set(env_name: str, defaults: tuple[str, ...]) -> set[str]:
    """env 里的逗号分隔集合；**没设或设成空串都取默认值**（1.0 的语义，照搬）。

    空串在这里**不是**「关掉这条判据」的开关——1.0 的 ``if not raw`` 把两者并成
    同一件事。照抄这个口径：改掉它会让一个部署里留着的空变量拥有它从来没有过的含义。
    """
    raw = os.environ.get(env_name)
    if not raw:
        return set(defaults)
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


# 判定「这个进程属于 Sentrix 这套系统」的两个信号：监听端口，或命令行里的关键词。
# 两个都可以由部署用环境变量整体替换（见 _configured_set）。
# **默认值在 S9b-1 改过：8771 → 9597、8091 → 9598。** S9a 照搬的是 1.0 的部署
# （PhotoBench 跑 8771、主 API 跑 8091）；2.0 的端口表（设计文档 §5.1）给 PhotoBench
# 定的是 9597，主 API 是 9598。不改的话这条判据在 2.0 上是**过期**的——进程名关键词
# 还能兜住一部分，但「按监听端口认进程」那一路会认不出自己派生的进程。
# 8500/8501 与 8100/8101/6333 是模型服务与 Qdrant 的端口，由 S11 随部署形态再定，
# 本切片不动（2.0 的 config 里模型服务仍在 8100，照旧命中）。
SYSTEM_PROCESS_PORTS = _configured_set(
    "PHOTOBENCH_SYSTEM_PROCESS_PORTS",
    ("9597", "9598", "8500", "8501", "8100", "8101", "6333"),
)
SYSTEM_PROCESS_KEYWORDS = _configured_set(
    "PHOTOBENCH_SYSTEM_PROCESS_KEYWORDS",
    (
        "photobench",
        "benchmark_orchestrator.py",
        "sentrix",
        "vllm",
        "llama-server",
        "ollama",
        "qdrant",
    ),
)


def _process_cmdline(pid: int) -> str:
    """``/proc/<pid>/cmdline``（NUL 分隔 → 空格）；空了再退回 ``comm``。读不到返回空串。"""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        text = raw.replace(bytes([0]), bytes([32])).decode("utf-8", "replace").strip()
        if text:
            return text
    except (OSError, ValueError):
        pass
    try:
        return Path(f"/proc/{pid}/comm").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _process_rss_mib(pid: int) -> float | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text(encoding="ascii").splitlines():
            if line.startswith("VmRSS:"):
                return round(float(line.split()[1]) / 1024.0, 2)
    except (OSError, ValueError, IndexError):
        return None
    return None


def _read_linux_system_memory() -> dict[str, Any]:
    values: dict[str, float] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            key, _, raw = line.partition(":")
            if key in {"MemTotal", "MemAvailable"}:
                # /proc/meminfo 的单位是 kB。在这里一次性换成 MiB，好让每个后缀写着
                # _mib 的字段真的是 MiB（1.0 的注释，照搬）。
                values[key] = float(raw.strip().split()[0]) / 1024.0
        total = values.get("MemTotal")
        available = values.get("MemAvailable")
        if total is None or available is None:
            raise ValueError("MemTotal/MemAvailable missing")
        return {
            "status": "available",
            "source": "proc_meminfo",
            "data": {
                "system_memory_used_mib": round(max(0.0, total - available), 2),
                "system_memory_total_mib": round(total, 2),
                "system_memory_available_mib": round(available, 2),
                "system_memory_scope": "host_all_processes",
            },
        }
    except (OSError, ValueError, IndexError) as exc:
        return {"status": "unavailable", "source": "proc_meminfo", "reason": str(exc)}


def _listening_ports_by_pid(target_ports: set[str] | None = None) -> dict[int, set[str]]:
    """``/proc/net/tcp{,6}`` 里状态为 ``0A``（LISTEN）的 socket → 持有它的 pid。

    先按 inode 建表，再扫 ``/proc/*/fd`` 的符号链接认领——**这是唯一能拿到「谁在听」
    的办法**，因为 ``/proc/net/tcp`` 只有 inode 没有 pid。
    """
    target_ports = {str(port) for port in (target_ports or set())}
    socket_ports: dict[str, str] = {}
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(table).read_text(encoding="ascii").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) < 10 or parts[3] != "0A":
                continue
            try:
                port = str(int(parts[1].rsplit(":", 1)[1], 16))
            except (ValueError, IndexError):
                continue
            if target_ports and port not in target_ports:
                continue
            socket_ports[parts[9]] = port
    if not socket_ports:
        return {}
    result: dict[int, set[str]] = {}
    for proc in Path("/proc").iterdir():
        if not proc.name.isdecimal():
            continue
        fd_dir = proc / "fd"
        try:
            fds = list(fd_dir.iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            match = re.fullmatch(r"socket:\[(\d+)\]", target)
            if match and match.group(1) in socket_ports:
                result.setdefault(int(proc.name), set()).add(socket_ports[match.group(1)])
    return result


def _host_process_rows() -> list[dict[str, Any]]:
    ports_by_pid = _listening_ports_by_pid(SYSTEM_PROCESS_PORTS)
    rows: list[dict[str, Any]] = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdecimal():
            continue
        pid = int(proc.name)
        rss_mib = _process_rss_mib(pid)
        cmdline = _process_cmdline(pid)
        # 两个都读不到的行直接丢掉：那多半是已经退出的进程留下的目录项。
        if rss_mib is None and not cmdline:
            continue
        rows.append(
            {
                "pid": pid,
                "process_name": Path(cmdline.split(" ", 1)[0]).name if cmdline else "",
                "rss_mib": rss_mib,
                "listening_ports": sorted(ports_by_pid.get(pid, set()), key=lambda value: int(value)),
                "identity": cmdline.lower()[:1000],
            }
        )
    return rows


def _is_system_related_process(row: Mapping[str, Any]) -> bool:
    identity = str(row.get("identity") or "").lower()
    ports = {str(port) for port in row.get("listening_ports") or []}
    if ports & SYSTEM_PROCESS_PORTS:
        return True
    return any(keyword and keyword in identity for keyword in SYSTEM_PROCESS_KEYWORDS)


def _query_compute_apps() -> list[dict[str, Any]]:
    """``nvidia-smi --query-compute-apps``。**非零退出直接抛**，由调用方兜底成空表。"""
    result = subprocess.run(
        [
            "nvidia-smi",
            "--query-compute-apps=pid,process_name,used_memory",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    rows: list[dict[str, Any]] = []
    for row in csv.reader(io.StringIO(result.stdout), skipinitialspace=True):
        if len(row) < 3:
            continue
        try:
            rows.append(
                {
                    "pid": int(float(row[0])),
                    "process_name": row[1],
                    "used_memory_mib": float(row[2]),
                }
            )
        except (ValueError, TypeError):
            continue
    return rows


def _process_pss_mib(pid: int) -> float | None:
    """比例集大小（PSS），先读 ``smaps_rollup``，读不到再退回逐行累加 ``smaps``。

    **缺失不是 0，也绝不用 RSS 顶替。** 有些内核只部分暴露 ``smaps_rollup``，
    而逐 map 的 ``smaps`` 仍然可读，所以留了第二条路。
    """
    try:
        with open(f"/proc/{pid}/smaps_rollup", encoding="ascii") as file:
            for line in file:
                if line.startswith("Pss:"):
                    return round(int(line.split()[1]) / 1024, 2)
    except (OSError, ValueError, IndexError):
        pass
    total_kib = 0
    found = False
    try:
        with open(f"/proc/{pid}/smaps", encoding="ascii") as file:
            for line in file:
                if line.startswith("Pss:"):
                    total_kib += int(line.split()[1])
                    found = True
    except (OSError, ValueError, IndexError):
        return None
    return round(total_kib / 1024, 2) if found else None


def _process_smaps_field_mib(pid: int, field: str) -> float | None:
    """读 ``smaps_rollup`` 的某一个字段，读不到再退回累加 ``smaps``。口径同 PSS。"""
    try:
        with open(f"/proc/{pid}/smaps_rollup", encoding="ascii") as file:
            for line in file:
                if line.startswith(field):
                    return round(int(line.split()[1]) / 1024, 2)
    except (OSError, ValueError, IndexError):
        pass
    total_kib = 0
    found = False
    try:
        with open(f"/proc/{pid}/smaps", encoding="ascii") as file:
            for line in file:
                if line.startswith(field):
                    total_kib += int(line.split()[1])
                    found = True
    except (OSError, ValueError, IndexError):
        return None
    return round(total_kib / 1024, 2) if found else None


def _sum_mib(rows: Sequence[Mapping[str, Any]], key: str) -> float | None:
    """有值才求和；一个都没有返回 ``None``（不是 0.0）。"""
    values = [float(row[key]) for row in rows if isinstance(row.get(key), (int, float))]
    return round(sum(values), 2) if values else None


def _augment_host_process_memory(
    data: Mapping[str, Any] | None,
    *,
    process_hint: str = "",
    endpoint_port: str = "",
    model_pids: set[int] | None = None,
    compute_apps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """给遥测数据补上「宿主 RAM 归属」与「PhotoBench 相关进程的 GPU 归属」。

    这些字段全部是**尽力而为**：拿不到就不写，**缺失绝不能被读成 0**。

    归属规则（1.0 的原文，照搬）：有 ``model_pids`` 就**只认这批 PID**，否则才退回
    用 ``process_hint`` / ``endpoint_port`` 去命令行里模糊匹配。后半条是给「没有
    Manager、不知道 PID」的端点用的；前者是权威，不能被模糊匹配冲淡。
    """
    data = dict(data or {})
    # 上一轮可能留下过这几个字段（同一个 data 被反复增强），先清掉再重算。
    for key in (
        "product_stack_memory_mib",
        "benchmark_process_memory_used_mib",
        "benchmark_process_memory_scope",
        "sentrix_stack_pss_mib",
        "sentrix_stack_pss_scope",
    ):
        data.pop(key, None)
    try:
        process_rows = _host_process_rows()
    except Exception:
        process_rows = []
    try:
        compute_apps = list(compute_apps) if compute_apps is not None else _query_compute_apps()
    except Exception:
        compute_apps = []

    known_model_pids = {int(pid) for pid in (model_pids or set()) if str(pid).isdigit()}
    for key in ("root_pid",):
        if str(data.get(key) or "").isdigit():
            known_model_pids.add(int(str(data[key])))
    for pid in data.get("tracked_pids") or []:
        if str(pid).isdigit():
            known_model_pids.add(int(str(pid)))
    for process in data.get("processes") or data.get("model_processes") or []:
        if str((process or {}).get("pid") or "").isdigit():
            known_model_pids.add(int(str(process["pid"])))

    hint = str(process_hint or "").lower()
    port = str(endpoint_port or "")
    model_rows: list[dict[str, Any]] = []
    for row in process_rows:
        identity = str(row.get("identity") or "")
        if known_model_pids:
            is_model = row["pid"] in known_model_pids
        else:
            is_model = bool((hint and hint in identity) or (port and port in identity))
        if is_model:
            model_rows.append(row)

    system_rows = [row for row in process_rows if _is_system_related_process(row)]
    system_pids = {row["pid"] for row in system_rows}
    system_gpu_rows = [row for row in compute_apps if row.get("pid") in system_pids]

    # 调用方给了确切的模型 PID 就地认它，**不许把 PID 选中的模型重新摊成所有匹配
    # 通用 hint 的进程**（1.0 的注释，照搬）。
    model_pids_found = {row["pid"] for row in model_rows}
    sentrix_rows = [row for row in system_rows if row["pid"] not in model_pids_found]
    for row in system_rows:
        row["pss_mib"] = _process_pss_mib(int(str(row["pid"])))
        row["smaps_rss_mib"] = _process_smaps_field_mib(int(str(row["pid"])), "Rss:")
    model_rss = _sum_mib(model_rows, "rss_mib")
    sentrix_pss = _sum_mib(sentrix_rows, "pss_mib")
    product_pss = _sum_mib(system_rows, "pss_mib")
    model_smaps_rss = _sum_mib(model_rows, "smaps_rss_mib")
    system_gpu = _sum_mib(system_gpu_rows, "used_memory_mib")
    model_memory = unified_model_memory(model_pids_found, rss_mib=model_rss, host_rss_mib=model_smaps_rss)
    if model_memory:
        data.update(model_memory)
        data["model_system_processes"] = [
            {key: row.get(key) for key in ("pid", "process_name", "rss_mib", "pss_mib", "listening_ports")}
            for row in model_rows[:20]
        ]
    if sentrix_pss is not None:
        data["sentrix_stack_pss_mib"] = sentrix_pss
        data["sentrix_stack_pss_scope"] = "sentrix_related_pss_excluding_model"
    # 产品总量只有在**每个相关进程的 PSS 都在**、且模型的 VmRSS / smaps-Rss 两个
    # 补偿输入也都在时才写。RSS 仍然是独立的「模型 VmRSS」指标，但**不许悄悄变成
    # 产品总量**（1.0 的注释，照搬）。
    pss_complete = bool(system_rows) and all(row.get("pss_mib") is not None for row in system_rows)
    model_pss_complete = bool(model_rows) and all(row.get("pss_mib") is not None for row in model_rows)
    if (
        product_pss is not None
        and pss_complete
        and model_rss is not None
        and model_smaps_rss is not None
        and model_pss_complete
        and model_memory.get("model_process_gpu_memory_mib") is not None
    ):
        uma_extra = float(model_memory["model_process_gpu_memory_mib"])
        product_memory = round(product_pss + uma_extra, 2)
        data["product_stack_memory_mib"] = product_memory
        data["benchmark_process_memory_used_mib"] = product_memory
        data["benchmark_process_memory_scope"] = "all_related_pss_plus_model_uma_extra"
        data["model_uma_extra_mib"] = round(uma_extra, 2)
        data["benchmark_processes"] = [
            {key: row.get(key) for key in ("pid", "process_name", "rss_mib", "pss_mib", "listening_ports")}
            for row in system_rows[:50]
        ]
    if system_gpu is not None:
        data["benchmark_process_gpu_memory_mib"] = system_gpu
        data["benchmark_process_gpu_memory_scope"] = "photobench_related_gpu_compute_processes"
        data["benchmark_gpu_processes"] = system_gpu_rows[:50]
    if compute_apps and data.get("all_processes_memory_mib") is None:
        data["all_processes_memory_mib"] = round(
            sum(float(row["used_memory_mib"]) for row in compute_apps), 2
        )
        data["all_processes"] = compute_apps
        data["all_processes_scope"] = "gpu_compute_processes"
    return data


class OpenAICompatibleInferenceProvider:
    """任何 OpenAI 兼容端点：``/v1/models`` + ``/v1/chat/completions``。

    ``api_mode="vllm"`` 时保留 vLLM 扩展（``chat_template_kwargs``），
    ``"generic"`` 时**删掉扩展键与值为 None 的键**——严格的 OpenAI 实现会因为多出来的
    键直接 400（1.0 的注释：一个普通的 llama.cpp / Ollama 端点不该因为 vLLM 的形状而挂）。
    """

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str = "",
        api_mode: str = "generic",
        manager_url: str = "",
        timeout: float = 180,
    ):
        self.base_url = normalize_openai_base_url(base_url)
        if not self.base_url:
            raise ValueError("model base URL is required")
        self.api_key = str(api_key or "")
        self.api_mode = str(api_mode or "generic").strip().lower()
        self.manager_url = normalize_service_url(manager_url)
        self.timeout = float(timeout)

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def health(self) -> dict[str, Any]:
        """先问 ``/models``；它不通再试 ``/health`` 与 ``/api/health``（llama.cpp / Ollama）。"""
        try:
            models = self.list_models()
            return {"status": "available", "source": "openai_models", **models}
        except Exception as models_error:
            root = self.base_url.removesuffix("/v1")
            for suffix in ("/health", "/api/health"):
                try:
                    response = httpx.get(
                        f"{root}{suffix}", headers=self.headers, timeout=min(10, self.timeout)
                    )
                    response.raise_for_status()
                    body = response.json() if response.content else {}
                    return {"status": "available", "source": suffix, "detail": body}
                except Exception:
                    continue
            return {"status": "unavailable", "error": str(models_error)}

    def list_models(self) -> dict[str, Any]:
        response = httpx.get(f"{self.base_url}/models", headers=self.headers, timeout=min(15, self.timeout))
        response.raise_for_status()
        body = response.json()
        models = [
            str(item.get("id"))
            for item in body.get("data") or []
            if isinstance(item, dict) and item.get("id")
        ]
        return {"models": models, "raw": body}

    def _body(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        body = dict(payload or {})
        if self.api_mode == "generic":
            body.pop("chat_template_kwargs", None)
            body = {key: value for key, value in body.items() if value is not None}
        return body

    def chat(self, payload: Mapping[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        """非流式对话，**返回已解析的 JSON**。非 2xx 抛异常（``raise_for_status``）。

        1.0 返回的是 ``httpx.Response``，调用方自己 ``.json()``。端口不能把 httpx
        摆到 domain 上，所以在适配器里就解析掉——调用方看到的失败行为不变。
        """
        response = httpx.post(
            f"{self.base_url}/chat/completions",
            json=self._body(payload),
            headers=self.headers,
            timeout=timeout or self.timeout,
        )
        response.raise_for_status()
        return dict(response.json())

    def chat_stream(self, payload: Mapping[str, Any], *, timeout: float | None = None) -> Iterator[str]:
        """流式对话，**逐行产出 SSE 文本**（不含行尾的换行符）。

        1.0 返回 httpx 的 stream 上下文管理器，由调用方 ``with`` 住；端口只能给出
        迭代器，于是「打开—读取—关闭」收在适配器的生成器里：``with`` 块的退出即
        连接的关闭，迭代器被垃圾回收时也会走完。
        """
        with httpx.stream(
            "POST",
            f"{self.base_url}/chat/completions",
            json=self._body(payload),
            headers=self.headers,
            timeout=timeout or self.timeout,
        ) as response:
            response.raise_for_status()
            yield from response.iter_lines()

    def token_count(
        self, messages: Sequence[Mapping[str, Any]], *, timeout: float = 15
    ) -> dict[str, Any] | None:
        """精确 token 数只有 Sentrix vLLM Manager 的 ``/tokenize-current`` 给得出。

        没有 Manager 返回 ``None``（1.0 基类的默认值，见 ports 的注释）；响应缺
        ``prompt_tokens`` / ``max_model_len`` 时抛 ``ValueError``——**一个看起来成功
        但内容是空的预算比失败更危险**，调用方拿它去截断提示词会静默截错。
        """
        if not self.manager_url:
            return None
        response = httpx.post(
            f"{self.manager_url}/tokenize-current",
            json={"messages": list(messages), "add_generation_prompt": True},
            timeout=min(timeout, self.timeout),
        )
        response.raise_for_status()
        value = response.json()
        if int(value.get("prompt_tokens") or 0) < 1 or int(value.get("max_model_len") or 0) < 1:
            raise ValueError("invalid tokenizer budget response")
        return dict(value)

    def capabilities(self) -> dict[str, Any]:
        managed = bool(self.manager_url)
        return {
            "provider": "openai_compatible",
            "api_mode": self.api_mode,
            "chat": True,
            "chat_stream": True,
            "list_models": True,
            "token_count": managed,
            "vision": "unknown",
            "json_object": "optional",
            "stream_usage": "optional",
            "vllm_extensions": self.api_mode == "vllm",
        }

    def max_concurrency(self) -> int | None:
        """这个端点同时能服务几个请求。1.0 ``benchmark_orchestrator.py:3667-3713``。

        两条来源按 1.0 的顺序：**先问端点自己**（llama.cpp 自报槽位），再问 manager
        （托管 vLLM 的实例槽位）。

        **读不到一律返回 ``None``。** 它不是错误，是「这台机器没告诉我们」——两个
        HTTP 探测各自可以因为端点不是那个类型、没开、或超时而失败，而调用方对它们的
        处置是同一条：退回串行。把异常抛出去会让每一次检索都挂在这个可选能力上。
        """
        slots = self._llamacpp_slots()
        if slots is not None:
            return slots
        return self._manager_slots()

    def _llamacpp_slots(self) -> int | None:
        """llama-server 在 ``GET /props`` 里自报 ``total_slots``（= 启动时的 ``--parallel N``）。

        **这是外部端点唯一问得到的容量**：llama.cpp 不是 manager 拉起来的，manager 的
        ``/state`` 对它一无所知——而 153 的 8100 正是这个形态。
        """
        root = self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url
        try:
            response = httpx.get(f"{root}/props", headers=self.headers, timeout=3)
            response.raise_for_status()
            payload = response.json()
        except Exception:  # noqa: BLE001 —— 见 max_concurrency 的 docstring
            return None
        if not isinstance(payload, dict):
            return None
        raw = payload.get("total_slots")
        if raw is None:
            return None
        try:
            slots = int(raw)
        except (TypeError, ValueError):
            return None
        return slots if slots > 0 else None

    def _manager_slots(self) -> int | None:
        """托管 vLLM 实例的真实槽位（``--max-num-seqs``）。1.0 ``:3715-3748``。"""
        if not self.manager_url:
            return None
        try:
            response = httpx.get(f"{self.manager_url}/state", headers=self.headers, timeout=3)
            response.raise_for_status()
            payload = response.json()
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(payload, dict):
            return None
        # 1.0 依次看这三个键，取第一个 > 0 的。
        for key in ("max_num_seqs", "max_num_seqs_running", "max_concurrency"):
            raw = payload.get(key)
            if raw is None:
                continue
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return None


class ManagerLifecycleProvider:
    """Sentrix vLLM Manager 的 ``/profiles`` ``/start`` ``/stop`` ``/state``。"""

    def __init__(self, manager_url: str):
        self.base_url = normalize_service_url(manager_url)
        if not self.base_url:
            raise ValueError("manager URL is required")

    def _request(
        self,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        method: str = "GET",
        timeout: float = 30,
    ) -> Any:
        response = httpx.request(method, f"{self.base_url}{path}", json=payload, timeout=timeout)
        response.raise_for_status()
        return response.json() if response.content else {}

    def profiles(self) -> dict[str, Any]:
        # Manager 的旧版本直接返回数组，新版本包在 {"profiles": [...]} 里，两种都认。
        value = self._request("/profiles", timeout=15)
        profiles = value if isinstance(value, list) else value.get("profiles") or []
        return {"status": "available", "profiles": profiles}

    def start(self, payload: Mapping[str, Any], *, timeout: float = 120) -> dict[str, Any]:
        return dict(self._request("/start", payload=dict(payload), method="POST", timeout=timeout))

    def stop(self, payload: Mapping[str, Any] | None = None, *, timeout: float = 90) -> dict[str, Any]:
        return dict(
            self._request("/stop", payload=dict(payload or {"timeout": 60}), method="POST", timeout=timeout)
        )

    def state(self) -> dict[str, Any]:
        return dict(self._request("/state", timeout=15))


class ManagerTelemetryProvider:
    """Manager 侧的遥测：``/gpu-stats``（vLLM 自己的数）与 ``/process-memory``（宿主归属）。"""

    def __init__(self, manager_url: str):
        self.base_url = normalize_service_url(manager_url)
        if not self.base_url:
            raise ValueError("manager URL is required")

    def _optional(self, path: str) -> dict[str, Any]:
        """**失败不抛**，落成 ``unavailable`` + ``error``：遥测是增强，不是前置条件。"""
        try:
            response = httpx.get(f"{self.base_url}{path}", timeout=10)
            response.raise_for_status()
            value = response.json() if response.content else {}
            return {"status": "available", "data": value}
        except Exception as exc:
            return {"status": "unavailable", "error": str(exc)}

    def gpu_stats(self) -> dict[str, Any]:
        return self._optional("/gpu-stats")

    def process_memory(self) -> dict[str, Any]:
        value = self._optional("/process-memory")
        if value.get("status") == "available":
            data = value.get("data") or {}
            model_pids: set[int] = set()
            for pid in data.get("tracked_pids") or []:
                with contextlib.suppress(TypeError, ValueError):
                    model_pids.add(int(pid))
            if data.get("root_pid") is not None:
                with contextlib.suppress(TypeError, ValueError):
                    model_pids.add(int(data["root_pid"]))
            value["data"] = _augment_host_process_memory(data, process_hint="vllm", model_pids=model_pids)
        return value

    def system_memory(self) -> dict[str, Any]:
        # Manager 跑在本机（153），所以宿主的 /proc/meminfo 就是它的内存。
        return _read_linux_system_memory()

    def kv_cache(self) -> dict[str, Any]:
        memory = self.process_memory()
        if memory.get("status") != "available":
            return memory
        metrics = (memory.get("data") or {}).get("vllm_metrics") or {}
        return {"status": "available", "data": metrics}


class UnavailableLifecycleProvider:
    """没有 Manager 时的显式占位。**是 ``not_applicable``，不是 ``unavailable``**：

    「这台机器上没配 Manager」与「Manager 配了但没答」是两件事，1.0 用两个不同的
    状态词区分，照搬——把它们并成一个词会让「配置漏了」消失在「探活失败」里。
    """

    _result: dict[str, Any] = {"status": "not_applicable", "reason": "model_manager_not_configured"}

    def profiles(self) -> dict[str, Any]:
        return {**self._result, "profiles": []}

    def start(self, payload: Mapping[str, Any], *, timeout: float = 120) -> dict[str, Any]:
        return dict(self._result)

    def stop(self, payload: Mapping[str, Any] | None = None, *, timeout: float = 90) -> dict[str, Any]:
        return dict(self._result)

    def state(self) -> dict[str, Any]:
        return dict(self._result)


class UnavailableTelemetryProvider:
    """没有可用的本机遥测实现时的显式占位。状态词的理由同 ``UnavailableLifecycleProvider``。"""

    _result: dict[str, Any] = {"status": "not_applicable", "reason": "telemetry_provider_not_configured"}

    def gpu_stats(self) -> dict[str, Any]:
        return dict(self._result)

    def process_memory(self) -> dict[str, Any]:
        return dict(self._result)

    def kv_cache(self) -> dict[str, Any]:
        return dict(self._result)

    def system_memory(self) -> dict[str, Any]:
        return dict(self._result)


class HostNvidiaTelemetryProvider:
    """本机 NVIDIA 遥测，**不假设任何框架**（外部运行时的兜底）。

    GPU 数字来自 ``nvidia-smi``；进程归属靠「nvidia-smi 的 compute 进程 ∩ 本机
    /proc 里身份相关/匹配 hint 的进程」。**匹配不到就说匹配不到**，不把全部 compute
    进程当成模型（那会把别的任务的内存算进模型）。
    """

    framework = "generic"

    def __init__(self, process_hint: str = "", endpoint_url: str = ""):
        self.process_hint = str(process_hint or "").strip().lower()
        self.endpoint_url = normalize_service_url(endpoint_url)

    @property
    def endpoint_port(self) -> str:
        parsed = urlparse(self.endpoint_url)
        return str(parsed.port or "")

    @staticmethod
    def _query(query: str) -> list[list[str]]:
        result = subprocess.run(
            ["nvidia-smi", f"--query-{query}", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return list(csv.reader(io.StringIO(result.stdout), skipinitialspace=True))

    def gpu_stats(self) -> dict[str, Any]:
        try:
            rows = self._query(
                "gpu=index,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,clocks.sm"
            )
            gpus = []
            for row in rows:
                if len(row) < 7:
                    continue
                v = [float(x) for x in row[1:]]
                gpus.append(
                    {
                        "index": int(float(row[0])),
                        "gpu_utilization_pct": v[0],
                        "memory_used_mib": v[1],
                        "memory_total_mib": v[2],
                        "temperature_c": v[3],
                        "power_draw_w": v[4],
                        "sm_clock_mhz": v[5],
                    }
                )
            data: dict[str, Any] = {"gpus": gpus, "memory_scope": "gpu_device"}
            cpu_temperature_c = read_cpu_temperature_c()
            if cpu_temperature_c is not None:
                data["cpu_temperature_c"] = cpu_temperature_c
            return {"status": "available", "source": "host_nvidia_smi", "data": data}
        except Exception as exc:
            return {"status": "unavailable", "source": "host_nvidia_smi", "error": str(exc)}

    @staticmethod
    def _read_system_memory() -> dict[str, Any]:
        return _read_linux_system_memory()

    def system_memory(self) -> dict[str, Any]:
        return self._read_system_memory()

    def process_memory(self) -> dict[str, Any]:
        try:
            candidates: list[dict[str, Any]] = []
            for row in self._query("compute-apps=pid,process_name,used_memory"):
                if len(row) < 3:
                    continue
                try:
                    candidates.append(
                        {
                            "pid": int(float(row[0])),
                            "process_name": row[1],
                            "used_memory_mib": float(row[2]),
                        }
                    )
                except (ValueError, TypeError):
                    continue
            hints = [self.process_hint]
            matches: list[dict[str, Any]] = []
            for item in candidates:
                process_name = str(item["process_name"]).lower()
                identity = process_name
                try:
                    raw_cmdline = Path(f"/proc/{item['pid']}/cmdline").read_bytes()
                    identity = (
                        f"{identity} "
                        f"{raw_cmdline.replace(bytes([0]), bytes([32])).decode('utf-8', 'replace').lower()}"
                    )
                except (OSError, ValueError):
                    pass
                item["identity"] = identity[:500]
                if any(hint and hint in identity for hint in hints):
                    matches.append(item)
                elif self.endpoint_port and self.endpoint_port in identity:
                    # 一个通用名字的可执行文件（python）会把运行时的名字藏起来；
                    # 命令行绑在这个端口的进程仍然是「尽力而为」的有效信号
                    # （1.0 的注释，照搬）。
                    matches.append(item)
            if not matches:
                data = _augment_host_process_memory(
                    {
                        "processes": [],
                        "model_processes": [],
                        "all_processes": candidates,
                        "all_processes_memory_mib": sum(float(x["used_memory_mib"]) for x in candidates),
                        "process_attribution": "unavailable",
                        "process_attribution_reason": "matching_model_process_not_found",
                    },
                    process_hint=self.process_hint,
                    endpoint_port=self.endpoint_port,
                    compute_apps=candidates,
                )
                return {
                    "status": "unavailable",
                    "source": "host_nvidia_smi",
                    "reason": "matching_model_process_not_found",
                    "data": data,
                }
            others = [item for item in candidates if item not in matches]
            model_gpu_memory_mib = sum(float(x["used_memory_mib"]) for x in matches)
            data = _augment_host_process_memory(
                {
                    "process_memory_used_mib": model_gpu_memory_mib,
                    "processes": matches,
                    "model_processes": matches,
                    "model_process_scope": "gpu_compute_processes",
                    "other_processes_memory_mib": sum(float(x["used_memory_mib"]) for x in others),
                    "other_processes": others,
                    "all_processes_memory_mib": sum(float(x["used_memory_mib"]) for x in candidates),
                    "all_processes": candidates,
                    "all_processes_scope": "gpu_compute_processes",
                },
                process_hint=self.process_hint,
                endpoint_port=self.endpoint_port,
                model_pids={int(item["pid"]) for item in matches},
                compute_apps=candidates,
            )
            # _augment_host_process_memory 可能补上 RAM 归属，但「模型 GPU 内存」这个
            # 指标**仍然是上面那份 nvidia-smi 的 PID 求和**，不许被增强改写。
            data["process_memory_used_mib"] = model_gpu_memory_mib
            return {"status": "available", "source": "host_nvidia_smi", "data": data}
        except Exception as exc:
            return {"status": "unavailable", "source": "host_nvidia_smi", "error": str(exc)}

    def kv_cache(self) -> dict[str, Any]:
        return {
            "status": "not_applicable",
            "source": "host_nvidia_smi",
            "runtime_framework": self.framework,
            "reason": "framework_does_not_expose_kv_cache",
        }


class LlamaCppTelemetryProvider(HostNvidiaTelemetryProvider):
    framework = "llama.cpp"

    def __init__(self, endpoint_url: str = ""):
        super().__init__(process_hint="llama-server", endpoint_url=endpoint_url)


class VllmTelemetryProvider(HostNvidiaTelemetryProvider):
    """本地**非受管** vLLM 端点的尽力进程归属。"""

    framework = "vllm"

    def __init__(self, endpoint_url: str = ""):
        super().__init__(process_hint="vllm", endpoint_url=endpoint_url)


class OllamaTelemetryProvider(HostNvidiaTelemetryProvider):
    framework = "ollama"

    def __init__(self, endpoint_url: str = ""):
        super().__init__(process_hint="ollama", endpoint_url=endpoint_url)


class OrinLlamaCppTelemetryProvider:
    """读 **118 上那一个特定的** llama-server 进程；Orin 是 UMA，没有独立 VRAM。

    远端身份、PID 文件与命令都是**写死的，不从用户给的 URL 推**：这是一个只对
    ``192.168.0.118:8100`` 成立的提供者，端点对不上直接 ``ValueError``。
    **进程找不到/变了绝不报 0 MB**——0 是一个合法的测量值，用它表示「没读到」会让
    评测把一次探活失败记成「模型不占内存」。
    """

    endpoint = "http://192.168.0.118:8100"
    remote = "orin@192.168.0.118"
    pid_file = "/home/orin/VLM/gemma4/results/orin/server_photobench_8100.pid"

    def __init__(self, endpoint_url: str = ""):
        parsed = urlparse(normalize_service_url(endpoint_url))
        if parsed.hostname != "192.168.0.118" or parsed.port != 8100:
            raise ValueError("Orin telemetry is only configured for the 118:8100 endpoint")
        self._last_memory: dict[str, Any] = {"status": "unavailable", "reason": "not_sampled"}
        self._last_pss_probe = 0.0

    def _metrics(self) -> dict[str, Any]:
        """llama.cpp 的 ``/metrics`` 里那几个 ``llamacpp:kv_cache_*``。探不到就什么都不加。"""
        data: dict[str, Any] = {}
        try:
            response = httpx.get(f"{self.endpoint}/metrics", timeout=2)
            response.raise_for_status()
            values: dict[str, float] = {}
            for line in response.text.splitlines():
                if line.startswith("llamacpp:kv_cache_") and "{" not in line:
                    name, _, value = line.partition(" ")
                    try:
                        values[name] = float(value.strip().split()[0])
                    except (ValueError, IndexError):
                        continue
            ratio = values.get("llamacpp:kv_cache_usage_ratio")
            if ratio is not None and 0 <= ratio <= 1:
                data["vllm_metrics"] = {"kv_cache_usage_pct": round(ratio * 100, 2)}
            tokens = values.get("llamacpp:kv_cache_tokens")
            if tokens is not None and tokens >= 0:
                data["kv_cache_used_tokens"] = int(tokens)
        except (httpx.HTTPError, ValueError):
            pass
        return data

    def gpu_stats(self) -> dict[str, Any]:
        data = dict(self._last_memory.get("data") or {})
        data.setdefault("memory_unit", "process_pss_uma_mib")
        data.setdefault("memory_profile", {"method": "orin_process_pss_uma_v1"})
        data.update(self._metrics())
        # PSS 走 ssh，比 /metrics 贵得多，所以 5 秒一次；这中间的调用复用上一次的读数。
        if time.monotonic() - self._last_pss_probe < 5:
            self._last_memory = {"status": "available", "source": "orin_ssh_pss", "data": data}
            return {
                "status": "available",
                "source": "orin_ssh_pss",
                "data": {"gpus": [{"index": 0, "memory_unit": "process_pss_uma_mib"}]},
            }
        self._last_pss_probe = time.monotonic()
        # 远端命令自带四道闸门：PID 文件可读且是数字、comm 必须是 llama-server、
        # 命令行必须绑在 8100、PSS 必须读得到。任何一道不过就非零退出，
        # **宁可不报也不报错数字**。
        command = (
            f"pid=$(cat {self.pid_file}) || exit 1; "
            'case "$pid" in *[!0-9]*|"") exit 2;; esac; '
            'ps -p "$pid" -o comm= | grep -Fxq "llama-server" || exit 3; '
            'ps -p "$pid" -o args= | grep -Fq -- "--port 8100" || exit 3; '
            "pss=$(awk '/^Pss:/{print $2}' \"/proc/$pid/smaps_rollup\" 2>/dev/null); "
            'if [ -z "$pss" ]; then '
            "pss=$(awk '/^Pss:/{s+=$2} END{if(s) print s}' \"/proc/$pid/smaps\" 2>/dev/null); "
            "fi; "
            '[ -n "$pss" ] || exit 4; printf "%s\\n" "$pss"; '
            "awk '/^VmRSS:/{print $2}' \"/proc/$pid/status\"; "
            'printf "pid=%s\\n" "$pid"'
        )
        try:
            completed = subprocess.run(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=3",
                    "-o",
                    "ConnectionAttempts=1",
                    self.remote,
                    command,
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
            lines = completed.stdout.strip().splitlines()
            pss_mib = int(lines[0]) / 1024
            rss_mib = int(lines[1]) / 1024
            pid = int(lines[2].removeprefix("pid="))
            data["root_pid"] = pid
            data["process_memory_used_mib"] = round(pss_mib, 2)
            data["model_process_system_memory_used_mib"] = round(rss_mib, 2)
            data["model_process_system_memory_scope"] = "remote_host_process_vmrss"
            data["pss_sampled_at_monotonic"] = time.monotonic()
            self._last_memory = {"status": "available", "source": "orin_ssh_pss", "data": data}
            return {
                "status": "available",
                "source": "orin_ssh_pss",
                "data": {"gpus": [{"index": 0, "memory_unit": "process_pss_uma_mib"}]},
            }
        except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
            # 探不到就把上一次的进程内存指标**撤掉**并写明原因——留着旧值会被读成当前值。
            data.pop("process_memory_used_mib", None)
            data["pss_error"] = str(exc)
            self._last_memory = {"status": "available", "source": "orin_ssh_pss", "data": data}
            return {
                "status": "available",
                "source": "orin_ssh_pss",
                "data": {"gpus": [{"index": 0, "memory_unit": "process_pss_uma_mib"}]},
            }

    def process_memory(self) -> dict[str, Any]:
        # 提供者跑在 153、模型进程在 118。**永远不要扫 153 的 /proc 然后把这些进程
        # 标成 Orin 的**（1.0 的注释，照搬）。
        return self._last_memory

    def system_memory(self) -> dict[str, Any]:
        """读**远端 Orin 主机**的总量/可用量。

        提供者跑在 153 而模型跑在 118，读本机 ``/proc`` 会报一台错的机器。
        ``MemAvailable`` 是内核自己的可回收估计，给的是整机口径而**不假装**那是模型
        专属内存。
        """
        command = (
            "awk '/^MemTotal:/{t=$2} /^MemAvailable:/{a=$2} "
            'END{if(t && a) printf "%.3f %.3f\\n", t/1024, a/1024}\' /proc/meminfo'
        )
        try:
            completed = subprocess.run(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=3",
                    "-o",
                    "ConnectionAttempts=1",
                    self.remote,
                    command,
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
            total_text, available_text = completed.stdout.strip().split()
            total = float(total_text)
            available = float(available_text)
            return {
                "status": "available",
                "source": "orin_remote_proc_meminfo",
                "data": {
                    "system_memory_used_mib": round(max(0.0, total - available), 3),
                    "system_memory_total_mib": round(total, 3),
                    "system_memory_available_mib": round(max(0.0, available), 3),
                    "system_memory_scope": "host_all_processes",
                },
            }
        except (OSError, subprocess.SubprocessError, ValueError):
            return {
                "status": "unavailable",
                "source": "orin_remote_proc_meminfo",
                "reason": "remote_meminfo_unavailable",
            }

    def kv_cache(self) -> dict[str, Any]:
        data = self._last_memory.get("data") or {}
        if "kv_cache_used_tokens" not in data and "vllm_metrics" not in data:
            return {
                "status": "unavailable",
                "source": "llamacpp_metrics",
                "reason": "metrics_missing_or_disabled",
            }
        return {
            "status": "available",
            "source": "llamacpp_metrics",
            "data": {
                "used_tokens": data.get("kv_cache_used_tokens"),
                "usage_pct": (data.get("vllm_metrics") or {}).get("kv_cache_usage_pct"),
                "used_bytes": None,
                "bytes_status": "unavailable_without_model_kv_allocation_bytes",
            },
        }


def manager_lifecycle(manager_url: str) -> LifecycleProvider:
    """有 Manager → ``ManagerLifecycleProvider``；没有 → ``UnavailableLifecycleProvider``。

    **S9b-2 把这一支从 ``create_runtime_providers`` 里拆出来，只因为主链要单独用它。**
    run-execution 的三件套是**按 run 构造**的，而「这一轮没有模型管理器」是一种合法的
    运行形态（1.0 的外部端点与云端模型两条路都不需要 Manager）。原来那个工厂必须连
    ``InferenceProvider`` 一起构造，而后者在空 base URL 上抛
    ``ValueError("model base URL is required")``（``:453``）——于是「没有管理器」这个
    正常情形反而起不来。

    空 URL 判 `Unavailable` 的那条口径**只有这一处**：``create_runtime_providers``
    现在也走它，两份默认行为不会各自漂移。
    """
    normalized = normalize_service_url(manager_url)
    return ManagerLifecycleProvider(normalized) if normalized else UnavailableLifecycleProvider()


def manager_telemetry(manager_url: str) -> TelemetryProvider:
    """与 ``manager_lifecycle`` 同形的那一半。

    没有 Manager 时是 ``UnavailableTelemetryProvider`` 而**不是**宿主 nvidia-smi
    那一路——按主机形态选 telemetry 是 1.0 ``select_runtime_providers``
    （``benchmark_orchestrator.py:176``）的活，尚未搬（见 S9b-2 施工记录的偏离登记）。
    """
    normalized = normalize_service_url(manager_url)
    return ManagerTelemetryProvider(normalized) if normalized else UnavailableTelemetryProvider()


def create_runtime_providers(
    model_base_url: str,
    *,
    manager_url: str = "",
    api_key: str = "",
    api_mode: str = "generic",
    timeout: float = 180,
) -> RuntimeProviders:
    """「有 Manager / 没有 Manager」两条装配路（1.0 的工厂，照搬）。

    **它只装配 Manager 那一路**。Ollama / llama.cpp / Jetson / Apple / Orin 那几条
    分支不在这个函数里——1.0 把它们放在编排侧的 ``select_runtime_providers``
    （``benchmark_orchestrator.py:176``），那属于 S9b 的 T911。
    """
    inference = OpenAICompatibleInferenceProvider(
        model_base_url,
        api_key=api_key,
        api_mode=api_mode,
        manager_url=manager_url,
        timeout=timeout,
    )
    return RuntimeProviders(
        inference=inference,
        lifecycle=manager_lifecycle(manager_url),
        telemetry=manager_telemetry(manager_url),
    )
