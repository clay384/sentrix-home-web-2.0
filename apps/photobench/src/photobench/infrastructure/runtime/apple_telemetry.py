"""Apple Silicon 统一内存遥测——Mac mini M4 上「模型占了多少内存」的唯一口径。

移植自 1.0 ``backend/apple_telemetry.py``（418 行，基线提交 2d2cee7c）。

**为什么不能用 RSS。** M4 没有独立显存，``ps`` 的 RSS 会漏掉 Metal/GPU 的分配，
所以逐进程占用取 ``proc_pid_rusage`` 的 ``phys_footprint``。**这是进程的物理占用，
不是「只算 GPU」的内存**——两者的差别要说清，否则读表的人会把它当成显存。
产品总量是相关进程 footprint 的和，**不做** Orin 那套「VmRSS 减 Rss」的修正。

**宿主已用内存跟活动监视器的口径一致**：active + wired + 被压缩器占用的页。
inactive 与 speculative 的文件缓存不算在内。

**GPU 利用率**优先取 macmon 的 ``gpu_active_ratio``，其次才是 AGXAccelerator 的
``Device Utilization %``。CPU/GPU 温度与系统功耗来自 ``macmon pipe -i 1000``
——它在 Apple Silicon 上不需要 root。**没装 macmon 时这几位就缺席，不用 0 顶替**。
``macmon pipe -s 10`` 只是一次有限探测；本模块把**一个 pipe 开到进程生命周期结束**。

**落在 infrastructure 的理由与 providers 同：** 起子进程（``macmon`` / ``ps`` /
``lsof`` / ``ioreg`` / ``vm_stat`` / ``sysctl``）、``ctypes`` 调 ``libproc``、读
``/proc`` 之外的 macOS 接口——``domain-purity`` 与 ``application-purity`` 都禁止
domain/application 碰这些。协议在 ``photobench.domain.runtime.ports``。
"""

from __future__ import annotations

import atexit
import ctypes
import json
import platform
import re
import shutil
import struct
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# 与 providers.py 的同名常量**刻意不同源**：1.0 这里是一份写死的 set/tuple，
# 不读 PHOTOBENCH_SYSTEM_PROCESS_* 环境变量，而且关键词多一个 "mlx"。照搬这个差别，
# 不合并——合并会悄悄改变 Apple 主机上的进程归属结果。
# **端口跟着 S9b-1 改过（8771 → 9597、8091 → 9598）**，理由与 ``providers.py`` 的同名
# 常量一样：2.0 的 PhotoBench 是 9597、主 API 是 9598（设计文档 §5.1）。「不与
# providers 共用」这条差别说的是**关键词**（多一个 "mlx"、不读环境变量），端口不是
# 差别的一部分——留 1.0 的端口号只会让 Apple 主机上的归属判据在 2.0 上过期。
SYSTEM_PROCESS_PORTS = {"9597", "9598", "8500", "8501", "8100", "8101", "6333"}
SYSTEM_PROCESS_KEYWORDS = (
    "photobench",
    "benchmark_orchestrator.py",
    "sentrix",
    "vllm",
    "llama-server",
    "ollama",
    "qdrant",
    "mlx",
)


def is_apple_silicon() -> bool:
    return platform.system() == "Darwin" and platform.machine() in {"arm64", "aarch64"}


def parse_vm_stat(text: str, page_size: int, total_bytes: int | None = None) -> dict[str, Any] | None:
    """``vm_stat`` 输出 → 已用内存。三项缺一就返回 ``None``，**不补 0**。"""
    pages: dict[str, int] = {}
    for line in text.splitlines():
        name, sep, rest = line.partition(":")
        if not sep:
            continue
        token = rest.strip().rstrip(".").split()
        if not token:
            continue
        try:
            pages[name.strip().lower()] = int(token[0])
        except ValueError:
            continue
    needed = ("pages active", "pages wired down", "pages occupied by compressor")
    if any(key not in pages for key in needed) or page_size <= 0:
        return None
    used_bytes = (
        pages["pages active"] + pages["pages wired down"] + pages["pages occupied by compressor"]
    ) * page_size
    data: dict[str, Any] = {
        "system_memory_used_mib": round(used_bytes / 1048576, 2),
        "system_memory_scope": "apple_app_wired_compressed",
    }
    if total_bytes and total_bytes > 0:
        data["system_memory_total_mib"] = round(total_bytes / 1048576, 2)
        data["system_memory_available_mib"] = round(max(0, total_bytes - used_bytes) / 1048576, 2)
    return data


def parse_macmon_sample(payload: Any) -> dict[str, Any]:
    """一行 ``macmon pipe`` 的 JSON → PhotoBench 的采样字段。非 dict 输入返回空表。"""
    if not isinstance(payload, dict):
        return {}
    data: dict[str, Any] = {}
    temp = payload.get("temp")
    if not isinstance(temp, dict):
        temp = {}
    cpu_temp = temp.get("cpu_temp_avg")
    gpu_temp = temp.get("gpu_temp_avg")
    if isinstance(cpu_temp, (int, float)):
        data["cpu_temperature_c"] = round(float(cpu_temp), 3)
    if isinstance(gpu_temp, (int, float)):
        data["temperature_c"] = round(float(gpu_temp), 3)
    sys_power = payload.get("sys_power")
    if isinstance(sys_power, (int, float)):
        data["power_draw_w"] = round(float(sys_power), 3)
        data["power_scope"] = "macmon_sys_power"
    for source_key, target_key in (("cpu_power", "cpu_power_w"), ("gpu_power", "gpu_power_w")):
        value = payload.get(source_key)
        if isinstance(value, (int, float)):
            data[target_key] = round(float(value), 3)
    ratio = payload.get("gpu_active_ratio")
    if isinstance(ratio, (int, float)):
        data["gpu_utilization_pct"] = round(float(ratio) * 100, 3)
    return data


class _MacmonStream:
    """一个长命的 ``macmon pipe -i 1000``。

    用 ``-s`` 起一次有限采样不行：那个进程会自己结束，之后的读数就永远是最后一行。
    读数线程只把**最新**一行留下——采样是「当前值」不是「历史」。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: dict[str, Any] = {}
        self._proc: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        binary = shutil.which("macmon") or "/opt/homebrew/bin/macmon"
        if not Path(binary).is_file() or self._proc is not None:
            return
        self._proc = subprocess.Popen(
            [binary, "pipe", "-i", "1000"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self._thread = threading.Thread(target=self._read, name="macmon-pipe", daemon=True)
        self._thread.start()
        atexit.register(self.stop)

    def stop(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()

    def latest(self) -> dict[str, Any]:
        # 首次取数时懒启动：没装 macmon 的机器一次子进程都不会起。
        if self._proc is None:
            self.start()
        with self._lock:
            return dict(self._latest)

    def _read(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            parsed = parse_macmon_sample(payload)
            if not parsed:
                continue
            with self._lock:
                self._latest = parsed


_MACMON = _MacmonStream()


def parse_agx_performance(text: str) -> dict[str, Any]:
    """读集成 GPU 的计数器。**缺席的计数器就是不写**，不是 0。"""
    data: dict[str, Any] = {}
    util = re.search(r'Device Utilization %"=(\d+(?:\.\d+)?)', text)
    if util:
        data["gpu_utilization_pct"] = round(float(util.group(1)), 3)
    allocated = re.search(r'"Alloc system memory"=(\d+)', text)
    if allocated:
        data["gpu_allocated_unified_memory_mib"] = round(int(allocated.group(1)) / 1048576, 2)
    in_use = re.search(r'"In use system memory"=(\d+)', text)
    if in_use:
        data["gpu_in_use_unified_memory_mib"] = round(int(in_use.group(1)) / 1048576, 2)
    return data


class _RUsageInfoV0(ctypes.Structure):
    _fields_ = [
        ("ri_uuid", ctypes.c_uint8 * 16),
        ("ri_user_time", ctypes.c_uint64),
        ("ri_system_time", ctypes.c_uint64),
        ("ri_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_interrupt_wkups", ctypes.c_uint64),
        ("ri_pageins", ctypes.c_uint64),
        ("ri_wired_size", ctypes.c_uint64),
        ("ri_resident_size", ctypes.c_uint64),
        ("ri_phys_footprint", ctypes.c_uint64),
        ("ri_proc_start_abstime", ctypes.c_uint64),
        ("ri_proc_exit_abstime", ctypes.c_uint64),
    ]


_LIBPROC: Any = None
_LIBPROC_LOCK = threading.Lock()


def _libproc() -> Any:
    global _LIBPROC
    if _LIBPROC is None:
        library = ctypes.CDLL("/usr/lib/libproc.dylib")
        library.proc_pid_rusage.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(_RUsageInfoV0),
        ]
        library.proc_pid_rusage.restype = ctypes.c_int
        library.proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        library.proc_pidinfo.restype = ctypes.c_int
        _LIBPROC = library
    return _LIBPROC


# ---- 模型权重映射(phys_footprint 的盲区) ----
#
# **为什么 footprint 不是模型的全部。** llama.cpp 默认 mmap 加载 ``.gguf``：权重页是
# 文件后备页，macOS 把它们记进文件缓存，**不计入 ``phys_footprint``**。1.0 在 M4 上
# 实测：footprint 1612.8 MiB + 权重映射 2572.9 MiB ≈ 真实 4186 MiB——只报 footprint
# 就是把模型占用报成它的 39%，且比例随模型变，**无法用系数校正**。
#
# 量法：``proc_pidinfo(PROC_PIDREGIONPATHINFO)`` 逐 region 枚举进程地址空间，对映射
# 到权重文件的 region 累计 ``pro_user_size``。模型加载后权重页很快全部驻留，region
# 大小即驻留量的合理近似。
#
# **结构体尺寸的坑(macOS 27 实测)：** ``proc_regionwithpathinfo`` 是 1272 字节
# (proc_regioninfo 128 + vnode_info 120 + vip_path[MAXPATHLEN] 1024)，
# ``vip_path`` 在偏移 248。缓冲区给小了 ``proc_pidinfo`` 返回 0 且 errno=ENOMEM，
# 不报错、只静默拿不到路径——所以尺寸必须写死 1272，并在返回值上显式校验。
_PROC_PIDREGIONPATHINFO = 9
_REGION_WITH_PATH_SIZE = 1272
_VIP_PATH_OFFSET = 248
_PRO_USER_ADDR_OFFSET = 8
_PRO_USER_SIZE_OFFSET = 16
# 权重文件的判据：路径后缀。``.bin`` 故意不收——它太宽，任意数据文件都可能叫 .bin。
_WEIGHT_SUFFIXES = (".gguf", ".safetensors", ".pt", ".pth", ".ckpt", ".onnx", ".mlmodel")
# 权重映射在模型加载完成后基本不变，而逐 region 枚举是几百次系统调用——缓存它，
# TTL 兜底进程重启(换了 PID 缓存自然失配)与极端的动态加载。
_WEIGHT_MAP_TTL_SECONDS = 30.0
_WEIGHT_MAP_CACHE: dict[int, tuple[float, int]] = {}
_WEIGHT_MAP_CACHE_LOCK = threading.Lock()


class _RegionWithPath(ctypes.Structure):
    _fields_ = [
        ("prinfo", ctypes.c_uint8 * 128),
        ("vnode_info", ctypes.c_uint8 * 120),
        ("vip_path", ctypes.c_char * 1024),
    ]


def _weight_map_bytes(pid: int) -> int:
    """进程映射到权重文件的地址空间总量；读不到就返回 0(不伪造数字)。"""
    now = time.monotonic()
    with _WEIGHT_MAP_CACHE_LOCK:
        cached = _WEIGHT_MAP_CACHE.get(pid)
        if cached and now - cached[0] < _WEIGHT_MAP_TTL_SECONDS:
            return cached[1]
    total = 0
    try:
        region = _RegionWithPath()
        library = _libproc()
        address = 0
        while True:
            got = library.proc_pidinfo(
                int(pid),
                _PROC_PIDREGIONPATHINFO,
                ctypes.c_uint64(address),
                ctypes.byref(region),
                _REGION_WITH_PATH_SIZE,
            )
            if got != _REGION_WITH_PATH_SIZE:
                break
            user_addr = struct.unpack_from("<Q", region.prinfo, _PRO_USER_ADDR_OFFSET)[0]
            user_size = struct.unpack_from("<Q", region.prinfo, _PRO_USER_SIZE_OFFSET)[0]
            if user_addr <= 0 or user_size <= 0:
                break
            path = region.vip_path.split(b"\x00", 1)[0].lower()
            if path.endswith(_WEIGHT_SUFFIXES):
                total += user_size
            address = user_addr + user_size
    except (OSError, AttributeError, ValueError):
        total = 0
    with _WEIGHT_MAP_CACHE_LOCK:
        _WEIGHT_MAP_CACHE[pid] = (now, total)
    return total


def _process_bytes(pid: int) -> dict[str, int] | None:
    """footprint(非文件页) + 权重映射(文件后备页) = 模型进程真实占用。

    两者不重叠：权重页不进 footprint(上面的注释)，所以直接相加。
    footprint 读不到(进程已退出/权限)时整体返回 ``None``——与 1.0 的缺席口径一致，
    上层「少一个就整段不写」的逻辑依赖它。
    """
    footprint = _phys_footprint_bytes(pid)
    if footprint is None:
        return None
    weight = _weight_map_bytes(pid)
    return {"footprint": footprint, "weight_map": weight, "total": footprint + weight}


def _phys_footprint_bytes(pid: int) -> int | None:
    """活动监视器口径的进程 footprint，不是「只算 GPU」的内存。

    flavor 0 是 ``rusage_info_v0``，它本身就已经含 ``phys_footprint``；用更新的
    flavor 会写一个更大的结构体、撑爆这里的缓冲区。**结构体与库句柄留在模块级**：
    每次调用都重新定义它们，在 macOS 上第二次调用会崩（1.0 的注释，照搬）。
    """
    try:
        info = _RUsageInfoV0()
        with _LIBPROC_LOCK:
            rc = _libproc().proc_pid_rusage(int(pid), 0, ctypes.byref(info))
            footprint = int(info.ri_phys_footprint)
        if rc != 0 or footprint <= 0:
            return None
        return footprint
    except (OSError, AttributeError, ValueError):
        return None


def _run_text(command: list[str], timeout: float = 3) -> str:
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout or ""


def _page_size() -> int:
    text = _run_text(["sysctl", "-n", "hw.pagesize"], timeout=2).strip()
    try:
        return int(text)
    except ValueError:
        return 16384


def _total_bytes() -> int | None:
    text = _run_text(["sysctl", "-n", "hw.memsize"], timeout=2).strip()
    try:
        return int(text)
    except ValueError:
        return None


def _process_rows() -> list[dict[str, Any]]:
    text = _run_text(["ps", "-ax", "-o", "pid=", "-o", "command="], timeout=3)
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, _, command = stripped.partition(" ")
        if not pid_text.isdigit() or not command:
            continue
        rows.append(
            {
                "pid": int(pid_text),
                "command": command,
                "identity": command.lower()[:1000],
                "process_name": Path(command.split(" ", 1)[0]).name,
                "listening_ports": [],
            }
        )
    return rows


def _listening_ports(ports: set[str]) -> dict[int, set[str]]:
    """``lsof`` 的解析。

    ``-Fpn`` 是机器可读格式：``p<pid>`` 开一段，随后的 ``n<地址>`` 是它持有的一项。
    只认端口在本集合里的那些，其余地址行直接跳过。
    """
    command = ["lsof", "-nP", "-sTCP:LISTEN", "-Fpn"]
    for port in sorted(ports):
        command.extend(["-iTCP:" + port])
    text = _run_text(command, timeout=4)
    found: dict[int, set[str]] = {}
    pid: int | None = None
    for line in text.splitlines():
        if line.startswith("p"):
            pid = int(line[1:]) if line[1:].isdigit() else None
        elif pid is not None and line.startswith("n"):
            match = re.search(r":(\d+)$", line[1:])
            if match and match.group(1) in ports:
                found.setdefault(pid, set()).add(match.group(1))
    return found


def _is_related(row: dict[str, Any]) -> bool:
    ports = {str(port) for port in row.get("listening_ports") or []}
    if ports & SYSTEM_PROCESS_PORTS:
        return True
    identity = str(row.get("identity") or "")
    return any(keyword in identity for keyword in SYSTEM_PROCESS_KEYWORDS)


def _is_model_process(row: dict[str, Any], endpoint_port: str) -> bool:
    identity = str(row.get("identity") or "")
    ports = {str(port) for port in row.get("listening_ports") or []}
    if (
        endpoint_port
        and endpoint_port in ports
        and ("llama-server" in identity or "mlx" in identity or "vllm" in identity)
    ):
        return True
    return bool(endpoint_port and f"--port {endpoint_port}" in identity and "llama-server" in identity)


class LocalAppleUnifiedTelemetryProvider:
    """本机 Apple Silicon 遥测。三个方法的 ``status`` 口径见 domain 的 ``TelemetryProvider``。

    两处缓存（1.0 的：设备采样 1 秒、端口扫描 2 秒）都是**为了不被采样本身拖慢评测**：
    ``ioreg`` / ``vm_stat`` / ``lsof`` 都不便宜，而 GpuSampler 是 0.5 秒一跳。
    """

    source = "apple_unified"

    def __init__(self, endpoint_url: str = ""):
        parsed = urlparse(endpoint_url or "")
        self.port = str(parsed.port or "")
        self._last_memory: dict[str, Any] = {"status": "unavailable", "reason": "not_sampled"}
        self._device_sample: dict[str, Any] = {}
        self._last_device_probe = 0.0
        self._ports_by_pid: dict[int, set[str]] = {}
        self._last_port_probe = 0.0
        self._system_baseline_mib: float | None = None

    def _device(self) -> dict[str, Any]:
        if self._device_sample and time.monotonic() - self._last_device_probe < 1.0:
            return self._device_sample
        self._last_device_probe = time.monotonic()
        sample = parse_agx_performance(_run_text(["ioreg", "-r", "-c", "AGXAccelerator", "-l"], timeout=3))
        sample.update(_MACMON.latest())
        memory = parse_vm_stat(_run_text(["vm_stat"], timeout=3), _page_size(), _total_bytes())
        if memory:
            sample.update(memory)
        self._device_sample = sample
        return sample

    def _ports(self) -> dict[int, set[str]]:
        if time.monotonic() - self._last_port_probe < 2.0:
            return self._ports_by_pid
        self._last_port_probe = time.monotonic()
        self._ports_by_pid = _listening_ports(SYSTEM_PROCESS_PORTS)
        return self._ports_by_pid

    def _augment(self) -> dict[str, Any]:
        device = self._device()
        ports = self._ports()
        rows = _process_rows()
        for row in rows:
            row["listening_ports"] = sorted(ports.get(row["pid"], set()), key=int)
        model_rows = [row for row in rows if _is_model_process(row, self.port)]
        related_rows = [row for row in rows if _is_related(row)]
        model_pids = {row["pid"] for row in model_rows}
        sentrix_rows = [row for row in related_rows if row["pid"] not in model_pids]
        for row in sentrix_rows + model_rows:
            memory = _process_bytes(row["pid"])
            row["footprint_mib"] = None if memory is None else round(memory["footprint"] / 1048576, 2)
            row["weight_map_mib"] = None if memory is None else round(memory["weight_map"] / 1048576, 2)
            # 计费口径：footprint + 权重映射。权重映射缺席(读不到)时按 0 计，
            # 不把整段判缺席——权重读不到只影响模型进程，不该拖垮 sentrix 侧求和。
            row["process_bytes_mib"] = None if memory is None else round(memory["total"] / 1048576, 2)
        data: dict[str, Any] = {}
        # 只在**恰好一个**模型进程且占用读得到时才认它是模型：多个候选时选谁
        # 都是猜，猜错比不报更糟。
        if len(model_rows) == 1 and model_rows[0].get("process_bytes_mib") is not None:
            model = model_rows[0]
            data["root_pid"] = model["pid"]
            data["model_process_system_memory_used_mib"] = model["process_bytes_mib"]
            data["model_process_system_memory_scope"] = "apple_phys_footprint_plus_weight_map"
            data["model_process_phys_footprint_mib"] = model["footprint_mib"]
            data["model_process_weight_map_mib"] = model["weight_map_mib"]
            data["process_memory_used_mib"] = model["process_bytes_mib"]
            data["process_memory_scope"] = "apple_phys_footprint_plus_weight_map"
            data["model_system_processes"] = [
                {
                    "pid": model["pid"],
                    "process_name": model["process_name"],
                    "footprint_mib": model["footprint_mib"],
                    "weight_map_mib": model["weight_map_mib"],
                    "process_bytes_mib": model["process_bytes_mib"],
                    "listening_ports": model["listening_ports"],
                }
            ]
        # 求和前要求**每一个**相关进程的占用都在：少一个就整段不写，
        # 因为「和」在缺项时是一个看起来正常、实际偏小的假数字。
        sentrix_complete = bool(sentrix_rows) and all(
            row.get("process_bytes_mib") is not None for row in sentrix_rows
        )
        model_complete = bool(model_rows) and all(row.get("process_bytes_mib") is not None for row in model_rows)
        if sentrix_complete:
            sentrix = round(sum(row["process_bytes_mib"] for row in sentrix_rows), 2)
            data["sentrix_stack_pss_mib"] = sentrix
            data["sentrix_stack_pss_scope"] = "apple_phys_footprint_plus_weight_map_excluding_model"
        if sentrix_complete and model_complete and len(model_rows) == 1:
            product = round(sum(row["process_bytes_mib"] for row in sentrix_rows + model_rows), 2)
            data["product_stack_memory_mib"] = product
            data["benchmark_process_memory_used_mib"] = product
            data["benchmark_process_memory_scope"] = "apple_phys_footprint_plus_weight_map_related"
            data["benchmark_processes"] = [
                {
                    "pid": row["pid"],
                    "process_name": row["process_name"],
                    "footprint_mib": row["footprint_mib"],
                    "listening_ports": row["listening_ports"],
                }
                for row in (sentrix_rows + model_rows)[:50]
            ]
        used = device.get("system_memory_used_mib")
        # 基线取**第一次采到的**已用量：之后的 delta 才是「这次评测让机器多用了多少」。
        if self._system_baseline_mib is None and isinstance(used, (int, float)):
            self._system_baseline_mib = float(used)
        if self._system_baseline_mib is not None and isinstance(used, (int, float)):
            data["system_memory_baseline_mib"] = round(self._system_baseline_mib, 2)
            data["system_memory_delta_mib"] = round(float(used) - self._system_baseline_mib, 2)
        data["memory_unit"] = "apple_phys_footprint_plus_weight_map_mib"
        data["memory_profile"] = {"method": "apple_phys_footprint_plus_weight_map_v1"}
        return data

    def gpu_stats(self) -> dict[str, Any]:
        device = self._device()
        data = self._augment()
        # 设备采样里的字段只在 data 没有同名字段时补进来（进程归属的字段优先）。
        data.update({key: value for key, value in device.items() if key not in data})
        self._last_memory = {"status": "available", "source": self.source, "data": data}
        gpu: dict[str, Any] = {"index": 0, "memory_unit": "apple_phys_footprint_plus_weight_map_mib"}
        for key in (
            "gpu_utilization_pct",
            "gpu_allocated_unified_memory_mib",
            "gpu_in_use_unified_memory_mib",
            "temperature_c",
            "cpu_temperature_c",
            "power_draw_w",
            "power_scope",
            "cpu_power_w",
            "gpu_power_w",
        ):
            if key in device:
                gpu[key] = device[key]
        return {"status": "available", "source": self.source, "data": {"gpus": [gpu]}}

    def process_memory(self) -> dict[str, Any]:
        # 还没采过就先采一次：调用方只问进程内存时不该拿到 not_sampled。
        if self._last_memory.get("status") != "available":
            self.gpu_stats()
        return self._last_memory

    def system_memory(self) -> dict[str, Any]:
        data = self._device()
        if "system_memory_used_mib" not in data:
            return {"status": "unavailable", "source": "vm_stat", "reason": "vm_stat_unavailable"}
        keys = (
            "system_memory_used_mib",
            "system_memory_total_mib",
            "system_memory_available_mib",
            "system_memory_scope",
        )
        return {
            "status": "available",
            "source": "vm_stat",
            "data": {key: data[key] for key in keys if key in data},
        }

    def kv_cache(self) -> dict[str, Any]:
        return {
            "status": "not_applicable",
            "source": "apple_unified",
            "reason": "unified_memory_has_no_discrete_kv_pool",
        }
