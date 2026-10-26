"""PhotoBench 的装配点。

**字段按端口声明，不写具体类**——与 ``apps/api`` 的 ``sentrix/bootstrap/container.py``
同一条不变量（那里由 ``test_container_wiring.py`` 强制）。理由也一样：字段写成
``HttpxJsonClient`` 之类的具体类，可替换性就只存在于 ``build()`` 的那一行里，
调用方在类型上看不见，于是很容易顺手用上协议之外的方法——**结构上换得掉、
类型上看不出来，等于没换**。

运行时的三个提供者（推理 / 生命周期 / 遥测）在这里是**两个按端口声明的工厂**，
不是具体类：

- ``lifecycle_provider``：``manager_url -> LifecycleProvider``，服务 ``/api/profiles``
  与 ``/api/current-model`` 对 Manager 的查询；
- ``inference_provider``：``(model_base_url, manager_url) -> InferenceProvider``，
  服务「这个端点到底在跑哪个模型」的核对。

**为什么是工厂而不是一个 ``RuntimeProviders`` 字段**：1.0 的这两条查询是**按请求**构造
提供者的（``query_profiles`` ``:5069`` / ``query_current_model`` ``:5078`` 各自
``ManagerLifecycleProvider(vllm_api_url)`` / ``OpenAICompatibleInferenceProvider(...)``），
端点来自请求体而不是启动参数。``RuntimeProviders``（S9a 的聚合值对象）仍然是
``create_runtime_providers`` 的返回类型，只是不加进 Container ——本切片没有常驻的
「当前提供者」可用（那要等 S9b-2 的 run-execution 把选定的端点落下来）。
"""

from __future__ import annotations

import shlex
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from photobench.application.config.service import ConfigService
from photobench.application.datasets.service import DatasetCatalog
from photobench.application.deploy.service import SentrixModelSwitch
from photobench.application.model_profiles.service import ModelProfiles
from photobench.application.run_execution.service import (
    JudgeSettings,
    RunExecution,
    RunRequest,
    freeze_runtime_request,
)
from photobench.application.runs.index import RunIndex
from photobench.application.runs.read_runs import ReadRuns
from photobench.application.runs.reports import RunReports
from photobench.application.runs.suites import RunSuites
from photobench.application.spaces.service import MemorySpaces
from photobench.config import Settings
from photobench.domain.config.ports import ConfigStore, SecretEnvStore
from photobench.domain.datasets.ports import AlbumSource, QaMetadataSource
from photobench.domain.deploy.ports import SentrixProcess
from photobench.domain.http.ports import JsonHttpClient
from photobench.domain.results.ports import RunResultWriter, RunStartupRecovery, RunStore
from photobench.domain.runtime.ports import (
    InferenceProvider,
    LifecycleProvider,
    RunRuntimeSnapshot,
    RuntimeProviders,
)
from photobench.domain.sentrix.ports import SentrixClient
from photobench.infrastructure.config.env import DotenvSecretStore
from photobench.infrastructure.config.files import JsonConfigStore, read_build_revision
from photobench.infrastructure.datasets.albums import JsonAlbumSource
from photobench.infrastructure.datasets.qa_metadata import JsonQaMetadataSource
from photobench.infrastructure.deploy.sentrix_process import ObservedSentrixProcess, SubprocessSentrixProcess
from photobench.infrastructure.deploy.systemd_process import SystemdSentrixProcess
from photobench.infrastructure.http.client import HttpxJsonClient
from photobench.infrastructure.http.judge_images import inline_judge_images
from photobench.infrastructure.results.recovery import FileRunStartupRecovery
from photobench.infrastructure.results.store import FileResultWriter, FileRunStore
from photobench.infrastructure.runtime.providers import (
    create_runtime_providers,
    manager_lifecycle,
    manager_telemetry,
)
from photobench.infrastructure.runtime.selection import detect_runtime_framework, select_runtime_providers
from photobench.infrastructure.runtime.telemetry_scope import services_from_connection
from photobench.infrastructure.sentrix.client import HttpxSentrixClient

#: 1.0 建 provider 时用的超时：``query_current_model`` 里是 15 秒（``:5095``）。
#: ``query_profiles`` 那一路由 provider 自己的默认值管（1.0 也没传）。
_CURRENT_MODEL_TIMEOUT = 15


@dataclass(frozen=True)
class Container:
    settings: Settings
    config_store: ConfigStore
    secrets: SecretEnvStore
    run_store: RunStore
    startup_recovery: RunStartupRecovery
    result_writer: RunResultWriter
    qa_metadata: QaMetadataSource
    albums: AlbumSource
    http: JsonHttpClient
    sentrix: SentrixClient
    lifecycle_provider: Callable[[str], LifecycleProvider]
    inference_provider: Callable[[str, str], InferenceProvider]
    #: S9b-3：``(manager_url, endpoint_url, framework) -> RuntimeProviders``。
    #: **替换掉 S9b-2 的 ``telemetry_provider``**——那一个只会「有 Manager 就用
    #: ManagerTelemetry」，于是 153 上的 llama-server（没有 Manager）永远拿不到遥测。
    runtime_providers: Callable[[str, str, str], RuntimeProviders]
    config: ConfigService
    build_revision: str
    runtime_providers_for_run: Callable[[RunRuntimeSnapshot], RuntimeProviders]
    run_index: RunIndex
    runs: ReadRuns
    #: S10a：三份报告读侧（关键帧分析 / 记忆有效性 / trace 导出）。与 ``runs``
    #: **分开**：那三个都要打 Sentrix 取数，而 ``runs`` 是纯本地读。
    reports: RunReports
    model_profiles: ModelProfiles
    #: S10a：数据集展示层（``/api/manifests*`` / ``/api/qa-dataset`` / ``/api/albums/*``）
    #: 与记忆空间列表（``/api/memory-spaces``）。两者都只读，且都吃 ``JsonHttpClient``
    #: ——``memory-spaces`` 是 PhotoBench 唯一一处主动读 2.0 主 API 的展示层接口。
    datasets: DatasetCatalog
    spaces: MemorySpaces
    #: S9c · T923：换模型 = 重启 2.0 的进程控制面。**按 domain 端口声明**——
    #: S11 接管服务部署时替换的就是它的实现（见 ``domain/deploy/__init__.py``）。
    sentrix_process: SentrixProcess
    run_execution: Callable[[RunRequest], RunExecution]
    run_suites: RunSuites

    @staticmethod
    def build(settings: Settings) -> Container:
        config_store: ConfigStore = JsonConfigStore(settings.config_root)
        secrets: SecretEnvStore = DotenvSecretStore(settings.config_root)
        config = ConfigService(config_store, secrets)
        build_revision = read_build_revision(settings.sentrix_repo_root)
        run_store: RunStore = FileRunStore(settings.results_root)
        run_index = RunIndex()
        qa_metadata: QaMetadataSource = JsonQaMetadataSource(
            settings.data_root, settings.config_root / "qa_task_actions.json"
        )

        def lifecycle_provider(manager_url: str) -> LifecycleProvider:
            """有 Manager 就是 ``ManagerLifecycleProvider``，没有就是 ``Unavailable``。

            **不能直接 ``ManagerLifecycleProvider(manager_url)``**：那个类的构造器在
            空串上抛 ``ValueError("manager URL is required")``（``providers.py:576``）。
            S9b-1 的调用方（``ModelProfiles._manager_profiles``）先自判 ``if not manager_url``
            再调，所以没撞上；S9b-2 的 ``run_execution`` 是**无条件**构造三件套的
            （「没有管理器」本身就是一种合法的运行形态——1.0 的
            ``use_cloud_model`` / 外部端点两条路都不需要 Manager）。

            实现走 ``manager_lifecycle``（S9b-2 从 ``create_runtime_providers`` 里拆出来的
            那一支）：空 URL 判 ``Unavailable`` 的口径**只有那一处**，工厂与这里共用它，
            两份默认行为不会各自漂移。
            """
            return manager_lifecycle(manager_url)

        def inference_provider(model_base_url: str, manager_url: str) -> InferenceProvider:
            return create_runtime_providers(
                model_base_url,
                manager_url=manager_url,
                api_mode="generic",
                timeout=_CURRENT_MODEL_TIMEOUT,
            ).inference

        def runtime_providers(manager_url: str, endpoint_url: str, framework: str) -> RuntimeProviders:
            """一 run 的三件套，按主机形态选（1.0 ``select_runtime_providers`` ``:176``）。

            端点为空时退回配置里的那个：1.0 的 ``self.vllm_model_base_url`` 本来就是
            ``_configured_url`` 读出来的，不由调用方每次传。**不退的话
            ``OpenAICompatibleInferenceProvider`` 会在空串上抛 ``ValueError``**
            （``providers.py:453``）——那是把「配置缺失」报成「参数缺失」。
            """
            resolved = endpoint_url or config.model_base_url
            selection = select_runtime_providers(manager_url, resolved, framework or "generic")
            inference = create_runtime_providers(
                resolved,
                manager_url=manager_url,
                api_mode="generic",
                timeout=_CURRENT_MODEL_TIMEOUT,
            ).inference
            return RuntimeProviders(
                inference=inference,
                lifecycle=selection.lifecycle,
                telemetry=selection.telemetry,
                source=selection.source,
            )

        def runtime_providers_for_run(snapshot: RunRuntimeSnapshot) -> RuntimeProviders:
            connection = snapshot.connection()
            endpoint = str(connection["model_base_url"])
            manager = str(connection.get("vllm_manager_url") or "")
            framework = str(connection.get("runtime_framework") or "generic")
            selected = select_runtime_providers(
                manager,
                endpoint,
                framework,
                telemetry_services=services_from_connection(connection),
            )
            return RuntimeProviders(
                inference=create_runtime_providers(endpoint, manager_url=manager).inference,
                lifecycle=selected.lifecycle,
                telemetry=selected.telemetry,
                source=selected.source,
            )

        def judge_images(sentrix_url: str, images: Any, assets_by_name: Any) -> list[dict[str, Any]]:
            """证据判分的图内联。**PIL 只能在 infrastructure**（``photobench-application-purity``
            的 forbidden 名单里有它，惰性 import 也照样判红）——所以这段代码住在这里，
            由 application 以可注入回调的形式收下。"""
            return inline_judge_images(http, sentrix_url, images, assets_by_name)

        album_source: AlbumSource = JsonAlbumSource(
            settings.data_root, settings.config_root / "qa_task_actions.json"
        )
        http: JsonHttpClient = HttpxJsonClient(retry_connections=True, keep_alive=False)
        judge_http: JsonHttpClient = HttpxJsonClient()
        sentrix: SentrixClient = HttpxSentrixClient(http, lambda: config.sentrix_url)
        result_writer: RunResultWriter = FileResultWriter(settings.results_root)

        # 换模型 = 重启 2.0（T923）。健康探测复用同一个 ``JsonHttpClient``——那一层
        # 本来就管「怎么打一个 HTTP 并解 JSON」，不必为部署另造一个。
        #
        # S11 · T1103：托管形态按 ``sentrix_service_unit`` 选。填了单元就走 systemd
        # （``systemctl --user`` + 单元环境即权威记录）；留空维持裸进程（``make dev``
        # 那条路，pid 记录 + ``lsof`` 反查）。**两种形态都留着**——迁移期同机既有
        # ``make dev`` 的开发机、也有 ``systemd --user`` 的部署机，删除任何一种都是
        # 砍掉一条仍然成立的部署方式。端口类型不变，业务侧（``ensure_endpoint``）
        # 看不见这个分叉。
        def sentrix_process_for_run(snapshot: RunRuntimeSnapshot | None = None) -> SentrixProcess:
            api_url = str(snapshot.connection()["sentrix_url"]) if snapshot else settings.sentrix_api_url
            requested = urlparse(api_url)
            registered = urlparse(settings.sentrix_api_url)
            local = {"localhost", "127.0.0.1", "::1"}
            same_host = requested.hostname == registered.hostname or (
                requested.hostname in local and registered.hostname in local
            )
            if not (
                same_host
                and requested.hostname in local
                and registered.hostname in local
                and requested.port == registered.port
                and requested.scheme == registered.scheme
            ):
                return ObservedSentrixProcess(api_url=api_url, http=http)
            return (
                SystemdSentrixProcess(
                    api_url=api_url,
                    unit=settings.sentrix_service_unit,
                    http=http,
                    runtime_snapshot=snapshot,
                )
                if settings.sentrix_service_unit
                else SubprocessSentrixProcess(
                    api_url=api_url,
                    start_command=shlex.split(settings.sentrix_start_command),
                    cwd=settings.sentrix_repo_root,
                    pid_file=settings.sentrix_pid_file,
                    log_path=settings.sentrix_log_path,
                    http=http,
                    runtime_snapshot=snapshot,
                    environment=dict(settings.sentrix_environment),
                )
            )

        sentrix_process = sentrix_process_for_run()

        def run_execution(request: RunRequest) -> RunExecution:
            """一 run 一个 ``RunExecution``：它是**有状态**的（跑一轮带一份 item 表），
            所以是工厂不是常驻字段——与 provider 工厂同一个理由。

            S9b-3 起它收整个 ``RunRequest``：遥测要按请求里的端点与框架选
            （``runtime_providers``），judge 要按请求里的覆盖项装配。**取消回收模型**
            也走这条装配出来的生命周期提供者。
            """
            if request.runtime_snapshot is None:
                request = freeze_runtime_request(
                    request, config.public_runtime_connection_config(), build_revision
                )
            snapshot = request.runtime_snapshot
            assert snapshot is not None
            providers = runtime_providers_for_run(snapshot)
            run_client: SentrixClient = HttpxSentrixClient(
                http, lambda: str(snapshot.connection()["sentrix_url"])
            )
            judge = JudgeSettings(
                judge_url=config.judge_url,
                judge_model=config.judge_model,
                judge_api_key=config.judge_api_key,
                answer_quality_prompt=None,
                task_judge_prompt=None,
                evidence_judge_prompt=None,
            )
            return RunExecution(
                client=run_client,
                runtime_snapshot=snapshot,
                albums=album_source,
                writer=result_writer,
                lifecycle=providers.lifecycle,
                inference=providers.inference,
                telemetry=providers.telemetry,
                http=judge_http,
                judge_settings=judge,
                telemetry_source=providers.source,
                inline_images=judge_images,
                # 一 run 一个 switch：它只是 ``sentrix_process`` 的薄包装，无状态。
                sentrix_switch=SentrixModelSwitch(sentrix_process_for_run(snapshot)),
            )

        # **``run_suites`` 先建、``runs`` 后建**：列表页要叠加「还在跑的 run」，
        # 而那些 run 只在 ``RunSuites._active`` 里（索引里没有它们，见
        # ``RunIndex.upsert``）。顺序反过来就拿不到那个回调。
        run_suites = RunSuites(
            store=run_store,
            writer=result_writer,
            index=run_index,
            build_revision=build_revision,
            budget_defaults=settings.budget_defaults(),
            albums=album_source,
            config=config,
            sentrix=sentrix,
            http=judge_http,
            new_execution=run_execution,
            lifecycle_for=lifecycle_provider,
            manager_telemetry_for=manager_telemetry,
            detect_framework=detect_runtime_framework,
            inline_images=judge_images,
        )

        runs = ReadRuns(
            store=run_store,
            index=run_index,
            qa_metadata=qa_metadata,
            http=http,
            # 1.0 的 ``_hydrate_legacy_gt_images`` 读的是模块级 ``DEFAULT_SENTRIX_URL``，
            # 而 ``POST /api/config`` 会就地改那个全局量。传取值函数而不是当前值，
            # 才会在配置被写回之后用上新的地址——与 1.0 的 ``global`` 等价。
            sentrix_url=lambda: config.sentrix_url,
            live_states=run_suites.active_states,
        )
        reports = RunReports(
            store=run_store,
            http=http,
            # 与 ``ReadRuns`` 同一个取值函数：1.0 的 ``DEFAULT_SENTRIX_URL`` 是
            # 可被 ``POST /api/config`` 就地改写的全局量，传当前值会在配置写回后
            # 仍打旧地址。
            sentrix_url=lambda: config.sentrix_url,
            albums=album_source,
        )
        model_profiles = ModelProfiles(
            config=config,
            lifecycle_for=lifecycle_provider,
            inference_for=inference_provider,
        )
        # 展示层的两条：都复用同一个 ``http``（主链与 judge 用的那个）——
        # 再建一个客户端只会多一份连接池与一套超时默认值。
        datasets = DatasetCatalog(albums=album_source, http=http, config=config)
        spaces = MemorySpaces(http=http, config=config, index=run_index)
        return Container(
            settings=settings,
            config_store=config_store,
            secrets=secrets,
            run_store=run_store,
            startup_recovery=FileRunStartupRecovery(settings.results_root),
            result_writer=result_writer,
            qa_metadata=qa_metadata,
            albums=album_source,
            http=http,
            sentrix=sentrix,
            lifecycle_provider=lifecycle_provider,
            inference_provider=inference_provider,
            runtime_providers=runtime_providers,
            config=config,
            build_revision=build_revision,
            runtime_providers_for_run=runtime_providers_for_run,
            run_index=run_index,
            runs=runs,
            reports=reports,
            model_profiles=model_profiles,
            datasets=datasets,
            spaces=spaces,
            sentrix_process=sentrix_process,
            run_execution=run_execution,
            run_suites=run_suites,
        )
