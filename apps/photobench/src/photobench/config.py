"""PhotoBench 的目录与运行参数。对应 1.0 文件头那批 ``PROJECT_ROOT / ...`` 常量。

1.0（``:63-67``）：项目根 ``services/photobench``，数据在 ``data/``、结果在
``results/``、配置在 ``config/``、前端产物在 ``frontend/dist/``。2.0 的 PhotoBench 是
``apps/photobench``，于是数据根是 ``datasets/``（2.0 已有的那 1.4 GB 数据集），
配置与结果同名。

**三个环境变量是新增的**（1.0 全写死）：``PHOTOBENCH_CONFIG_ROOT`` /
``PHOTOBENCH_RESULTS_ROOT`` / ``PHOTOBENCH_DATA_ROOT``。理由不是「灵活」，是**部署参数**：
结果目录该跟数据盘走、数据集根要能换、配置根要能隔离。命名与 S9a 的
``PHOTOBENCH_SYSTEM_PROCESS_*`` 同一族。

（迁移期它们曾被用来指向 1.0 的 ``results/``；2026-10-06 起不再如此——评测历史属
「旧数据」不迁，见 ``docs/refactor/2026-10-06-eval-data-migration.md``。）

**S9c 又加了五个**（``sentrix_*``）：换模型 = 重启 2.0（裁决③，T923）要 PhotoBench
知道「2.0 怎么起、起在哪个端口、pid 记在哪」。它们**全是部署参数**，默认值只对
「2.0 与 PhotoBench 同机、走 ``make dev``」这一种部署成立——换部署形态就是改这五个
环境变量，不改代码。这正是把它们抽出来的理由：那套知识不该长在业务逻辑里。

**S11 再加第六个**（``sentrix_service_unit``，T1103）：进程托管形态从裸进程换成
``systemd --user`` 时，填上单元名（如 ``sentrix-api.service``），重启就走
``systemctl --user restart``、运行端点读单元的有效环境；留空则维持裸进程那套
（``SubprocessSentrixProcess``）。**迁移期两种形态都要在**，所以它默认留空——默认
仍然是「同机 ``make dev``」，与上面五个字段保持一致的口径。

**T1101 还加了一个**（``PHOTOBENCH_WEB_DIST_ROOT``）：前端产物由 PhotoBench 后端自己
serve（裁决③，「dist 挂到各自的 FastAPI」）。它同样是部署参数，而且**默认允许不
存在**——开发时只跑 vite、从没构建过 dist。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

#: ``apps/photobench``：``config.py`` 的上三级是 ``photobench`` 包 → ``src`` → 应用根。
APP_ROOT = Path(__file__).resolve().parents[2]


def _root(env_name: str, default: Path) -> Path:
    raw = os.environ.get(env_name)
    return Path(raw).expanduser() if raw else default


VALID_DEPLOYMENT_PROFILES = frozenset({"server", "mac"})


def deployment_profile() -> str:
    """部署形态：``server``（默认）或 ``mac``。

    与主产品读同一个 ``SENTRIX_DEPLOYMENT_PROFILE``。它只提供**评测侧默认值**：
    Mac 上 QA 并发 2、整册导入超时 86400s；server 维持端点探测与 7200s。
    显式的 ``PHOTOBENCH_QA_CONCURRENCY`` / ``payload.import_timeout`` 永远优先。
    """
    raw = os.environ.get("SENTRIX_DEPLOYMENT_PROFILE", "server").strip().lower()
    if raw not in VALID_DEPLOYMENT_PROFILES:
        raise ValueError(
            f"SENTRIX_DEPLOYMENT_PROFILE={raw!r} 未知；可选：{', '.join(sorted(VALID_DEPLOYMENT_PROFILES))}"
        )
    return raw


def default_qa_concurrency() -> int | None:
    return 2 if deployment_profile() == "mac" else None


def default_import_timeout() -> int:
    return 86400 if deployment_profile() == "mac" else 7200


#: 2.0 主 API 的默认启动命令。与仓库 ``Makefile`` 的 ``dev`` 目标逐字一致——那一条就是
#: 2.0 的进程入口（``uvicorn --factory sentrix.main:create_app --port 8191
#: --app-dir apps/api/src``）。换部署形态（容器 / systemd / 另一个端口）只需覆盖
#: ``PHOTOBENCH_SENTRIX_START_COMMAND``，**不改这里、也不改重启模块**。
DEFAULT_SENTRIX_START_COMMAND = (
    ".venv/bin/python -m uvicorn --factory sentrix.main:create_app "
    "--host 0.0.0.0 --port 8191 --app-dir apps/api/src"
)


def _sentrix_environment() -> tuple[tuple[str, str], ...]:
    """Collect the deployment environment that a PhotoBench restart must preserve.

    The 2.0 API reads its embedding scheme and provider selection at process start.
    A PhotoBench-triggered restart therefore has to pass those values along with
    ``SENTRIX_VLM_BASE_URL``; otherwise a Mac legacy process silently falls back to
    the WEMM defaults even though the sidecars on 8101/8103 are healthy.
    """
    names = (
        "SENTRIX_EMBEDDING_SCHEME",
        "SENTRIX_TEXT_EMBEDDER_URL",
        "SENTRIX_LEGACY_CLIP_URL",
        "FACE_PROVIDERS",
        "RETINAFACE_PROVIDERS",
    )
    values: list[tuple[str, str]] = []
    for name in names:
        value = os.environ.get(f"PHOTOBENCH_{name}", "").strip()
        if value:
            values.append((name, value))
    return tuple(values)


@dataclass(frozen=True)
class Settings:
    config_root: Path
    results_root: Path
    data_root: Path
    #: PhotoBench 前端的构建产物（``apps/photobench/web/dist``，``vite build`` 的产物）。
    #: 与上面三个根同一条口径——可配，且**允许不存在**（开发时只跑 vite）。默认值必须
    #: 在 data_root 之后：前三个没有默认值，dataclass 不允许无默认字段跟在有默认字段后面。
    web_dist_root: Path = APP_ROOT / "web" / "dist"
    #: 2.0 主 API 的健康检查基址（不含 ``/api/v1``）。重启后拿它确认真的起来了。
    sentrix_api_url: str = "http://127.0.0.1:8191"
    #: 2.0 的检出根：启动命令的 ``cwd``（``--app-dir apps/api/src`` 是相对的）。
    #: 默认取 ``APP_ROOT.parents[1]``——``apps/photobench`` 上两级就是仓库根。
    sentrix_repo_root: Path = APP_ROOT.parents[1]
    #: 启动命令。默认等于 ``make dev``，见上面的常量。
    sentrix_start_command: str = DEFAULT_SENTRIX_START_COMMAND
    #: Environment passed to a PhotoBench-triggered API restart. The prefix keeps
    #: deployment choices out of the ``photobench`` package's own ``SENTRIX_*``
    #: runtime while still making the restart reproducible on Mac legacy.
    sentrix_environment: tuple[tuple[str, str], ...] = ()
    #: 上一次由 PhotoBench 启动的 2.0 的 pid 与端点记录。**它同时是「当前端点」的
    #: 真相来源**——重启前要拿它判断「是不是已经指着这里了，不用动」。
    sentrix_pid_file: Path = APP_ROOT / "logs" / "sentrix-process.json"
    #: 2.0 的 stdout/stderr 落在这里（``start_new_session`` + 重定向，**不用 shell**）。
    sentrix_log_path: Path = APP_ROOT / "logs" / "sentrix.log"
    #: S11 · T1103：托管 2.0 的 ``systemd --user`` 单元名。**留空 = 用裸进程形态**
    #: （``SubprocessSentrixProcess``，默认，``make dev`` 那条路）；填了就用
    #: ``SystemdSentrixProcess``——重启走 ``systemctl --user``，运行端点读单元环境。
    #: 生产（``deploy/systemd/`` 的单元）填 ``sentrix-api.service``。
    sentrix_service_unit: str = ""
    #: 部署形态（``server`` / ``mac``）。只读，用于诊断；默认值见上面的 helpers。
    deployment_profile: str = "server"
    qa_concurrency: int = 0
    import_timeout: int = 7200

    agent_wall_time_s: float = 180.0
    chat_timeout_s: float = 120.0
    face_providers: str = "CPUExecutionProvider"
    retinaface_providers: str = "CPUExecutionProvider"

    def __post_init__(self) -> None:
        if self.deployment_profile not in {"server", "mac"}:
            raise ValueError("SENTRIX_DEPLOYMENT_PROFILE must be server or mac")

    def budget_defaults(self) -> dict[str, object]:
        product = {
            "deployment_profile": self.deployment_profile,
            "agent_wall_time_s": self.agent_wall_time_s,
            "chat_timeout_s": self.chat_timeout_s,
            "face_providers": self.face_providers,
            "retinaface_providers": self.retinaface_providers,
        }
        return {
            "qa_concurrency": self.qa_concurrency,
            "import_timeout": self.import_timeout,
            "product_settings": product,
        }

    @classmethod
    def load(cls) -> Settings:
        profile = os.environ.get("SENTRIX_DEPLOYMENT_PROFILE", "server").strip().lower()
        return cls(
            deployment_profile=profile,
            agent_wall_time_s=float(
                os.environ.get("SENTRIX_AGENT_WALL_TIME_S", "600" if profile == "mac" else "180")
            ),
            chat_timeout_s=float(
                os.environ.get("SENTRIX_CHAT_TIMEOUT_S", "180" if profile == "mac" else "120")
            ),
            face_providers=os.environ.get("FACE_PROVIDERS", "CPUExecutionProvider"),
            retinaface_providers=os.environ.get("RETINAFACE_PROVIDERS", "CPUExecutionProvider"),
            qa_concurrency=int(
                os.environ.get("PHOTOBENCH_QA_CONCURRENCY") or ("2" if profile == "mac" else "0")
            ),
            import_timeout=int(
                os.environ.get("PHOTOBENCH_IMPORT_TIMEOUT") or ("86400" if profile == "mac" else "7200")
            ),
            config_root=_root("PHOTOBENCH_CONFIG_ROOT", APP_ROOT / "config"),
            results_root=_root("PHOTOBENCH_RESULTS_ROOT", APP_ROOT / "results"),
            data_root=_root("PHOTOBENCH_DATA_ROOT", APP_ROOT / "datasets"),
            web_dist_root=_root("PHOTOBENCH_WEB_DIST_ROOT", APP_ROOT / "web" / "dist"),
            sentrix_api_url=os.environ.get("PHOTOBENCH_SENTRIX_API_URL", "http://127.0.0.1:8191").strip(),
            sentrix_repo_root=_root("PHOTOBENCH_SENTRIX_REPO_ROOT", APP_ROOT.parents[1]),
            sentrix_start_command=os.environ.get(
                "PHOTOBENCH_SENTRIX_START_COMMAND", DEFAULT_SENTRIX_START_COMMAND
            ).strip(),
            sentrix_environment=_sentrix_environment(),
            sentrix_pid_file=_root("PHOTOBENCH_SENTRIX_PID_FILE", APP_ROOT / "logs" / "sentrix-process.json"),
            sentrix_log_path=_root("PHOTOBENCH_SENTRIX_LOG_PATH", APP_ROOT / "logs" / "sentrix.log"),
            sentrix_service_unit=os.environ.get("PHOTOBENCH_SENTRIX_SERVICE_UNIT", "").strip(),
        )
