"""配置用例：1.0 ``benchmark_orchestrator.py`` 里那批「导入即解析」的全局配置。

**对应 1.0 的哪几段**：``_load_runtime_connection_config``（``:71``）、
``_configured_url``（``:101``）、``_load_judge_providers``（``:239``）、
``resolve_judge_provider``（``:270``）、``load_vllm_targets``（``:333``）、
``resolve_vllm_target``（``:352``）、两个 URL 归一化（``:368``/``:379``）、
``public_runtime_connection_config``（``:399``）、``persist_runtime_connection_config``
（``:419``）。

**形状变化只有一处，但它是这一层存在的理由**：1.0 把解析结果摊在**模块级全局变量**上，
``persist_runtime_connection_config`` 用 ``global`` 就地改它们（``:448-453``），于是
「当前生效的配置」是一个进程级单例、测试之间会互相污染。2.0 把同一批值收进这个对象的
实例属性，装配点（``bootstrap.container``）决定它的生命周期。**取值优先级逐条照搬**：
环境变量 > 配置文件 > 兜底默认值，且「环境变量被显式设成空串」表示**未配置**，
不许再往配置文件的机器上落（1.0 ``_configured_url`` 的注释就是这句话）。

**两处刻意没搬，都属 judge 域（S9b-3）**：``.env.local`` 的读取/写入
（``_load_dotenv_file`` ``:79`` / ``_write_local_env_secret`` ``:317``）与默认 judge
提示词常量。前者只服务 ``BENCH_JUDGE_API_KEY``，后者是打分逻辑的一部分。
"""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlparse

from photobench.domain.config.ports import ConfigStore, SecretEnvStore
from photobench.domain.errors import InvalidRequestError
from photobench.domain.runs.judge import JUDGE_PROMPT, JUDGE_PROMPT_KINDS

RUNTIME_CONNECTION_NAME = "runtime_connection.json"
JUDGE_PROVIDERS_NAME = "judge_providers.json"
VLLM_TARGETS_NAME = "vllm_targets.json"
CUSTOM_JUDGE_PROMPT_NAME = "custom_judge_prompt.json"

#: 1.0 ``:207`` 的 ``EVIDENCE_JUDGE_ENABLED``。证据判分默认关：它要比对图片，
#: 每道题都要额外取图 + 一次多模态调用，成本是另外两个判分的数倍。
EVIDENCE_JUDGE_ENABLED = os.environ.get("BENCH_EVIDENCE_JUDGE", "0") == "1"

# 1.0 ``:301-303`` 的云端大模型 profile：它不是一个 target，因为它不需要 Manager、
# 也没有本地 GPU 指标。这三条是**代码里的常量 + 环境覆盖**，与配置文件无关。
BIG_MODEL_PROFILE_ID = "big_model"
BIG_MODEL_BASE_URL = os.environ.get(
    "BENCH_BIG_MODEL_BASE_URL", "https://ark.cn-beijing.volces.com/api/plan/v3"
)
BIG_MODEL_MODEL = os.environ.get("BENCH_BIG_MODEL_MODEL", "doubao-seed-2.0-lite")
BIG_MODEL_ENABLED = os.environ.get("BENCH_BIG_MODEL_ENABLED", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def normalize_model_base_url(value: str | None) -> str:
    """1.0 ``:368``。把用户输入的模型端点补成 OpenAI ``/v1`` base URL。"""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if not raw.startswith(("http://", "https://")):
        raw = "http://" + raw
    raw = raw.rstrip("/")
    return raw if re.search(r"/v\d+$", raw, flags=re.IGNORECASE) else f"{raw}/v1"


def normalize_service_url(value: str | None) -> str:
    """1.0 ``:379``。补协议头，但**不**加 API 路径。"""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if not raw.startswith(("http://", "https://")):
        raw = "http://" + raw
    return raw.rstrip("/")


def secret_hint(value: str | None) -> str:
    """1.0 ``:308``。给界面看的密钥指纹：首尾各两位、中间打星。"""
    secret = str(value or "")
    if not secret:
        return ""
    if len(secret) <= 4:
        return "*" * len(secret)
    return f"{secret[:2]}{'*' * min(8, len(secret) - 4)}{secret[-2:]}"


class ConfigService:
    """进程内**当前生效**的配置。写回之后就地更新，与 1.0 的 ``global`` 等价。"""

    def __init__(self, store: ConfigStore, secrets: SecretEnvStore | None = None) -> None:
        self._store = store
        self._secrets = secrets
        # 1.0 在模块导入时就 ``_load_dotenv_file(PROJECT_ROOT / ".env.local")``
        # （``:116``）——**必须先于读 judge_api_key**，否则文件里的密钥不会被看到。
        self._load_dotenv()
        self._runtime = self._load_runtime_connection()
        self.judge_provider_id, self.judge_providers = self._load_judge_providers()
        self.vllm_target_id, self.vllm_targets = self._load_vllm_targets()

        self.sentrix_url = self._configured_url(
            "BENCH_SENTRIX_URL", "sentrix_url", fallback="http://127.0.0.1:9598"
        )
        # 2.0 不建 model-manager：8500 由 1.0 的 sentrix-vllm-api.service 提供，
        # 这里只是可选的 HTTP 消费（设计文档 §2.1「2.0 不管理模型进程生命周期」）。
        self.vllm_manager_url = self._configured_url("BENCH_VLLM_API_URL", "vllm_manager_url")
        self.model_base_url = self._configured_url("BENCH_VLLM_BASE_URL", "model_base_url")
        # 1.0 ``:345-350``：没配 base_url 时退回默认 target 的地址（去掉尾斜杠）。
        if not self.model_base_url and "model_base_url" not in self._runtime:
            target = self.vllm_targets.get(self.vllm_target_id, {})
            self.model_base_url = str(target.get("model_base_url") or "").rstrip("/")

        _, default_url, default_model, default_key = self.resolve_judge_provider(None)
        self.judge_url = (
            os.environ.get("BENCH_JUDGE_URL") or str(self._runtime.get("judge_url") or "") or default_url
        )
        self.judge_model = (
            os.environ.get("BENCH_JUDGE_MODEL")
            or str(self._runtime.get("judge_model") or "")
            or default_model
        )
        # 环境里**显式设了空串**也算「已配置」——1.0 ``:305`` 的三元表达式就是这个意思，
        # 而 ``os.environ.get(k, default)`` 正好是同一个语义（设了空串拿到的是空串）。
        self.judge_api_key = os.environ.get("BENCH_JUDGE_API_KEY", default_key)

    # -- 读取 ---------------------------------------------------------------

    def _load_dotenv(self) -> None:
        """1.0 ``_load_dotenv_file``（``:79``）：把 ``.env.local`` 的键补进 ``os.environ``。

        **已经在环境里的键不覆盖**（1.0 的 ``if key in os.environ: continue``）——运维
        临时 ``export`` 一个密钥试一次时，不该被上次写进文件的旧值盖回去。
        """
        if self._secrets is None:
            return
        for name, value in self._secrets.read_all().items():
            if name not in os.environ:
                os.environ[name] = value

    def _load_runtime_connection(self) -> dict[str, Any]:
        """1.0 ``:71``：文件缺失或不是对象时当空配置，**不报错**。

        坏 JSON 也当空——1.0 把 ``JSONDecodeError`` 和 ``OSError`` 一起吞了。
        这里保留该行为：一个损坏的连接配置文件不该让服务起不来（它是运行参数，
        改错了要能进去改回来）。
        """
        try:
            value = self._store.read_json(RUNTIME_CONNECTION_NAME)
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    def _load_judge_providers(self) -> tuple[str, dict[str, dict[str, Any]]]:
        """1.0 ``:239``：``judge_providers.json`` 缺失时退回 ``.example.json``。

        **与 runtime connection 相反，这里坏配置要报错**——1.0 如此：judge provider
        决定了打分的口径，静默退回默认值会让一整轮评测的分都算在另一个模型上。
        """
        for name in (JUDGE_PROVIDERS_NAME, "judge_providers.example.json"):
            try:
                value = self._store.read_json(name)
            except ValueError as exc:
                # 1.0 的文案带**完整路径**（``:250``），不是逻辑名——这里用 path_for 还原。
                raise InvalidRequestError(
                    f"invalid judge provider config {self._store.path_for(name)}: {exc}"
                ) from exc
            if value is None:
                continue
            providers = value.get("providers") or {}
            default_id = str(value.get("default_provider_id") or next(iter(providers), ""))
            if default_id not in providers:
                raise InvalidRequestError(f"default judge provider not found: {default_id}")
            configured = str(self._runtime.get("judge_provider_id") or "").strip()
            if configured in providers:
                default_id = configured
            return default_id, providers
        raise InvalidRequestError(
            f"invalid judge provider config {self._store.path_for(JUDGE_PROVIDERS_NAME)}: "
            "文件不存在，且没有 .example.json 兜底"
        )

    def _load_vllm_targets(self) -> tuple[str, dict[str, dict[str, Any]]]:
        """1.0 ``:333``：同样有 ``.example.json`` 兜底，坏配置同样报错。"""
        for name in (VLLM_TARGETS_NAME, "vllm_targets.example.json"):
            try:
                value = self._store.read_json(name)
            except ValueError as exc:
                raise InvalidRequestError(
                    f"invalid vLLM target config {self._store.path_for(name)}: {exc}"
                ) from exc
            if value is None:
                continue
            targets = value.get("targets") or {}
            default_id = str(value.get("default_target_id") or next(iter(targets), ""))
            if default_id not in targets:
                raise InvalidRequestError(f"default vLLM target not found: {default_id}")
            return default_id, targets
        raise InvalidRequestError(
            f"invalid vLLM target config {self._store.path_for(VLLM_TARGETS_NAME)}: "
            "文件不存在，且没有 .example.json 兜底"
        )

    def _configured_url(self, env_name: str, config_key: str, *, fallback: str = "") -> str:
        """1.0 ``:101``。环境变量 > 配置文件 > 兜底。"""
        if env_name in os.environ:
            return str(os.environ.get(env_name) or "").strip()
        if config_key in self._runtime:
            return str(self._runtime.get(config_key) or "").strip()
        return fallback

    def resolve_judge_provider(self, provider_id: str | None) -> tuple[str, str, str, str]:
        """1.0 ``:270``：``(id, url, model, api_key)``。

        ``runtime_connection.json`` 里的 ``judge_url``/``judge_model`` 只在「没有被
        ``judge_provider_id`` 钉住别的 provider」时才盖过 provider 自己的值——这是
        1.0 ``use_runtime_override`` 那两行的意思。
        """
        selected = str(provider_id or self.judge_provider_id)
        provider = self.judge_providers.get(selected)
        if not provider:
            raise InvalidRequestError(f"unknown judge provider: {selected}")
        configured_id = str(self._runtime.get("judge_provider_id") or "").strip()
        use_runtime_override = not configured_id or selected == configured_id
        url = str(provider.get("url") or "")
        model = str(provider.get("model") or "")
        if use_runtime_override:
            url = str(self._runtime.get("judge_url") or url)
            model = str(self._runtime.get("judge_model") or model)
        api_key = os.getenv("BENCH_JUDGE_API_KEY")
        if api_key is None:
            api_key = str(provider.get("api_key") or "")
        return selected, url, model, api_key

    # -- judge 提示词（1.0 ``:666``–``:699``） ---------------------------------

    def _read_custom_judge_prompt_file(self) -> dict[str, Any]:
        """读 ``custom_judge_prompt.json``。**坏文件当空**（1.0 ``:666`` 的
        ``except (json.JSONDecodeError, OSError)``）——一个写坏的提示词文件不该让
        ``GET /api/config`` 打不开；提示词是可改的运行参数。"""
        try:
            value = self._store.read_json(CUSTOM_JUDGE_PROMPT_NAME)
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    def load_custom_judge_prompts(self) -> dict[str, str | None]:
        """1.0 ``load_custom_judge_prompts``（``:676``），含旧键 ``judge_prompt`` 的兼容。"""
        data = self._read_custom_judge_prompt_file()
        legacy = data.get("judge_prompt") or None
        return {
            "answer_quality": data.get("answer_quality") or legacy,
            "task_decision": data.get("task_decision") or None,
            "evidence": data.get("evidence") or None,
        }

    def save_custom_judge_prompt(self, prompt: str, kind: str = "answer_quality") -> None:
        """1.0 ``save_custom_judge_prompt``（``:691``）。

        **空字符串表示恢复默认**（写 ``None`` 而不是空串）——一个空串会被判分器当成
        system 提示词发出去，模型于是收不到任何评分规则。
        """
        if kind not in JUDGE_PROMPT_KINDS:
            raise InvalidRequestError(f"unknown judge prompt kind: {kind}")
        data = self._read_custom_judge_prompt_file()
        if "judge_prompt" in data:  # 旧版单提示词键迁到 answer_quality
            data.setdefault("answer_quality", data.pop("judge_prompt"))
        data[kind] = prompt.strip() or None
        self._store.write_json(CUSTOM_JUDGE_PROMPT_NAME, data)

    def judge_prompt_storage_path(self) -> str:
        return self._store.path_for(CUSTOM_JUDGE_PROMPT_NAME)

    def public_judge_prompts(self) -> dict[str, Any]:
        """``GET /api/judge-prompts`` 的响应体（1.0 ``:7719``）。"""
        custom = self.load_custom_judge_prompts()
        labels = {
            "answer_quality": "回答质量 Judge",
            "task_decision": "任务判断 Judge",
            "evidence": "证据核验 Judge",
        }
        return {
            "kinds": [
                {
                    "kind": kind,
                    "label": labels[kind],
                    "default": JUDGE_PROMPT_KINDS[kind],
                    "custom": custom.get(kind),
                }
                for kind in ("answer_quality", "task_decision", "evidence")
            ],
            "storage_path": self.judge_prompt_storage_path(),
        }

    def public_judge_providers(self) -> list[dict[str, Any]]:
        """1.0 ``:254``：给界面看的 provider 列表，**不带密钥本体**。"""
        result = []
        for pid, provider in sorted(self.judge_providers.items()):
            _, url, model, api_key = self.resolve_judge_provider(pid)
            result.append(
                {
                    "id": pid,
                    "label": provider.get("label") or pid,
                    "model": model,
                    "url": url,
                    "supports_vision": provider.get("supports_vision", False),
                    "api_key_set": bool(api_key),
                    "api_key_hint": secret_hint(api_key),
                }
            )
        return result

    def resolve_vllm_target(
        self, target_id: str | None, *, manager_url: str = "", model_base_url: str = ""
    ) -> tuple[str, dict[str, Any]]:
        """1.0 ``:352``。"""
        selected = str(target_id or self.vllm_target_id).strip()
        target = self.vllm_targets.get(selected)
        if target:
            return selected, target
        if manager_url or model_base_url:
            return selected or "external", {
                "label": selected or "external",
                "manager_url": manager_url,
                "model_base_url": model_base_url,
                "description": "ad-hoc endpoint from the evaluation form",
            }
        known = ", ".join(sorted(self.vllm_targets)) or "(none)"
        raise InvalidRequestError(f"unknown vLLM target: {selected or self.vllm_target_id}; known: {known}")

    def public_runtime_connection_config(self) -> dict[str, Any]:
        """1.0 ``:399``。"""
        return {
            "sentrix_url": self.sentrix_url,
            "judge_url": self.judge_url,
            "judge_model": self.judge_model,
            "judge_api_key_set": bool(self.judge_api_key),
            "judge_api_key_hint": secret_hint(self.judge_api_key),
            "vllm_manager_url": (
                str(self._runtime["vllm_manager_url"])
                if "vllm_manager_url" in self._runtime
                else self.vllm_manager_url
            ),
            "model_base_url": (
                str(self._runtime["model_base_url"])
                if "model_base_url" in self._runtime
                else self.model_base_url
            ),
            "endpoint_model": str(self._runtime.get("endpoint_model") or ""),
            "judge_provider_id": self.judge_provider_id,
        }

    def public_config(self) -> dict[str, Any]:
        """``GET /api/config`` 的响应体（1.0 ``do_GET`` ``:7536``）。

        S9b-1 刻意少了三项（``judge_prompt`` / ``custom_judge_prompt`` /
        ``evidence_judge_enabled``），S9b-3 补齐——它们是打分口径的一部分，与本切片的
        judge 域一起落位。上一轮那条「没搬」的用例（``test_api_routes``）按实情改掉了。
        """
        return {
            "default_sentrix_url": self.sentrix_url,
            "default_judge_url": self.judge_url,
            "default_vllm_api_url": self.vllm_manager_url,
            "default_vllm_model_base_url": self.model_base_url,
            "default_vllm_target_id": self.vllm_target_id,
            "vllm_targets": self.vllm_targets,
            "runtime_config": self.public_runtime_connection_config(),
            "runtime_config_file": self._store.path_for(RUNTIME_CONNECTION_NAME),
            "judge_model": self.judge_model,
            "judge_prompt": JUDGE_PROMPT,
            "custom_judge_prompt": self.load_custom_judge_prompts().get("answer_quality"),
            "evidence_judge_enabled": EVIDENCE_JUDGE_ENABLED,
            "judge_providers": self.public_judge_providers(),
            "default_judge_provider_id": self.judge_provider_id,
            "cloud_model_profile": {
                "id": BIG_MODEL_PROFILE_ID,
                "label": "big_model（云端 API）",
                "source": "cloud_api",
                "available": BIG_MODEL_ENABLED,
                "base_url": BIG_MODEL_BASE_URL,
                "model": BIG_MODEL_MODEL,
                "manager_required": False,
                "local_token_preflight": False,
                "local_gpu_metrics": False,
            },
        }

    # -- 写入 ---------------------------------------------------------------

    def persist_runtime_connection(self, payload: dict[str, Any]) -> dict[str, Any]:
        """1.0 ``:419`` ``persist_runtime_connection_config``。

        **只落非密钥的连接参数**，并就地更新「当前生效值」——与 1.0 的 ``global`` 等价。
        """
        provider_id = str(payload.get("judge_provider_id") or self.judge_provider_id).strip()
        if provider_id not in self.judge_providers:
            raise InvalidRequestError(f"unknown judge provider: {provider_id}")
        values = {
            "sentrix_url": _validated_service_url(payload.get("sentrix_url"), "Sentrix URL"),
            "judge_url": _validated_service_url(payload.get("judge_url"), "Judge URL"),
            "judge_model": str(payload.get("judge_model") or "").strip(),
            "vllm_manager_url": _validated_service_url(
                payload.get("vllm_manager_url"), "vLLM Manager URL", optional=True
            ),
            "model_base_url": normalize_model_base_url(payload.get("model_base_url")),
            "endpoint_model": str(payload.get("endpoint_model") or "").strip(),
            "judge_provider_id": provider_id,
        }
        if not values["judge_model"]:
            raise InvalidRequestError("Judge model is required")
        self._store.write_json(RUNTIME_CONNECTION_NAME, values)
        self._runtime = dict(values)
        self.sentrix_url = values["sentrix_url"]
        self.judge_url = values["judge_url"]
        self.vllm_manager_url = values["vllm_manager_url"]
        self.model_base_url = values["model_base_url"]
        self.judge_provider_id = values["judge_provider_id"]
        self.judge_model = values["judge_model"]
        if "judge_api_key" in payload:
            # 1.0 ``:449``：密钥写 ``.env.local`` **并同时改 os.environ**——两件都要，
            # 前者让它活过重启，后者让同一个进程里已构造好的 judge 立刻用上新的。
            secret = str(payload.get("judge_api_key") or "")
            if self._secrets is None:
                raise InvalidRequestError("judge_api_key storage is not configured")
            self._secrets.write("BENCH_JUDGE_API_KEY", secret)
            os.environ["BENCH_JUDGE_API_KEY"] = secret
            self.judge_api_key = secret
        return self.public_runtime_connection_config()


def _validated_service_url(value: str | None, label: str, *, optional: bool = False) -> str:
    """1.0 ``:389``。"""
    normalized = normalize_service_url(value)
    if not normalized and optional:
        return ""
    parsed = urlparse(normalized)
    if not parsed.netloc:
        raise InvalidRequestError(f"{label} must be a host:port or HTTP(S) URL")
    return normalized
