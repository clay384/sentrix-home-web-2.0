"""Typed configuration.

Only ``SENTRIX_DATA_DIR`` and a handful of connection settings are configurable.
Everything else is derived, because 1.0's failure mode was ~126 env flags whose
values disagreed with each other and with the code.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import Field, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from sentrix.domain.retrieval.scheme import SCHEMES as EMBEDDING_SCHEMES

#: ``config.py`` 的上四级是 ``sentrix`` → ``src`` → ``api`` → ``apps``，再上一级是仓库根。
REPO_ROOT = Path(__file__).resolve().parents[4]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SENTRIX_", extra="forbid", populate_by_name=True)

    face_providers: str = Field(
        default="CUDAExecutionProvider,CPUExecutionProvider", validation_alias="FACE_PROVIDERS"
    )
    retinaface_providers: str = Field(
        default="CUDAExecutionProvider,CPUExecutionProvider", validation_alias="RETINAFACE_PROVIDERS"
    )
    data_dir: Path = Field(default=Path("data"))
    # 部署形态的**唯一开关**。只提供一组默认值，显式 env 永远优先：
    # - server：wemm / 180s / 120s
    # - mac：legacy / 600s / 180s（CPU 人脸另见 onnx_runtime 的 provider 默认）
    # 数据根、模型/侧车地址、judge 配置不归它管。
    deployment_profile: Literal["server", "mac"] = "server"

    @field_validator("face_providers", "retinaface_providers")
    @classmethod
    def validate_provider_chain(cls, raw: str) -> str:
        chain = [part.strip() for part in raw.split(",")]
        allowed = {"CUDAExecutionProvider", "CPUExecutionProvider", "CoreMLExecutionProvider"}
        if not chain or any(part not in allowed for part in chain):
            raise ValueError("unsupported or empty ONNX provider chain")
        return ",".join(chain)

    @model_validator(mode="after")
    def apply_deployment_profile(self) -> Settings:
        if self.deployment_profile == "mac":
            for key, value in {
                "embedding_scheme": "legacy",
                "agent_wall_time_s": 600.0,
                "chat_timeout_s": 180.0,
                "face_providers": "CPUExecutionProvider",
                "retinaface_providers": "CPUExecutionProvider",
            }.items():
                if key not in self.model_fields_set:
                    object.__setattr__(self, key, value)
        return self

    @model_validator(mode="after")
    def validate_agent_limits(self) -> Settings:
        if self.agent_max_tool_calls > self.agent_max_steps - 2:
            raise ValueError("agent tool budget must reserve two model steps for answer and review")
        for key in ("agent_max_search_calls", "agent_max_page_calls", "agent_max_inspections"):
            if getattr(self, key) > self.agent_max_tool_calls:
                raise ValueError(f"{key} cannot exceed agent_max_tool_calls")
        return self

    def runtime_configuration(self) -> dict[str, Any]:
        """Non-secret startup facts, never a benchmark policy."""
        return {
            key: getattr(self, key)
            for key in (
                "deployment_profile",
                "vlm_base_url",
                "embedding_scheme",
                "text_embedder_url",
                "text_embedder_timeout_s",
                "legacy_clip_timeout_s",
                "legacy_clip_url",
                "wemm_url",
                "agent_wall_time_s",
                "chat_timeout_s",
                "vlm_timeout_s",
                "vlm_context_tokens",
                "vlm_generation_profile",
                "vlm_native_output_budget",
                "agent_max_steps",
                "agent_max_tool_calls",
                "agent_max_search_calls",
                "agent_max_page_calls",
                "agent_max_inspections",
                "face_providers",
                "retinaface_providers",
            )
        }

    # Web 的构建产物（``apps/web/dist``，``vite build`` 的产物，``.gitignore`` 里）。
    # **它是本模块里第二个可配的根，和开头那句「data_dir 是唯一可配的根」是有出入
    # 的，这是有意的**：那条约束针对**数据**——数据根换掉时索引 / 媒体 / 缓存必须整体
    # 跟着走，散开配置就会各指一处。dist 不是数据，它是构建出来的只读产物，位置由
    # 「谁构建、构建到哪」决定，而且**允许不存在**（开发时只跑 vite）。可配是为了让
    # 「有产物 / 没产物」两种情形都能被摆到门禁里（见 ``main.py`` 的挂载逻辑）。
    web_dist_dir: Path = Field(default=REPO_ROOT / "apps" / "web" / "dist")

    api_port: int = 8191
    api_host: str = "0.0.0.0"

    # 2.0 reuses 1.0's already-running model services; it never manages them.
    vlm_base_url: str = "http://127.0.0.1:8100/v1"
    text_embedder_url: str = "http://127.0.0.1:8101"
    # W1：检索用的统一多模态嵌入侧车（WeMM-Embedding-2B，2048 维）。与 8100 的 VLM、
    # 8101 的 bge 同一条口径——2.0 不管理模型进程的生命周期，只连它。
    wemm_url: str = "http://127.0.0.1:8102"

    # ---- 嵌入方案（2026-10-20：为 16 GB Mac mini 恢复 legacy 回退） ----------------
    # **整方案切换**，不是两个槽各切各的。默认 wemm，153 不设此变量即行为不变。
    # 可选值与模型/维度见 ``domain/retrieval/scheme.py`` 的 ``SCHEMES``。
    embedding_scheme: str = "wemm"
    # legacy 方案的视觉侧车（chinese-clip ViT-L-14，768 维）。模型已在 130 上，
    # 由 ``scripts/maintenance/legacy_clip_sidecar.py`` 拉起（默认 8103）。
    legacy_clip_url: str = "http://127.0.0.1:8103"
    # 侧车超时：chinese-clip 是单卡/单 MPS 进程内的编码，比 WeMM 的 2B 快，
    # 但首次加载权重需要时间；30s 覆盖冷启动后的单张编码。
    legacy_clip_timeout_s: float = 30.0
    # bge-m3 侧车（8101）的查询超时，照 1.0 ``bge_text.py`` 的 10 秒。
    text_embedder_timeout_s: float = 10.0
    # 单个 Agent 回合的墙钟上限。180 是 153（GPU）的默认；Mac/CPU 上 4 并发时
    # 一次 inspect_photo 就可能 30-60s，正常回合会被截成 timeout，可放宽到 600。
    agent_wall_time_s: float = Field(default=180.0, gt=0)
    # Explicit execution limits are deployment facts, not answer-quality verdicts.
    agent_max_steps: int = Field(default=8, ge=3)
    agent_max_tool_calls: int = Field(default=6, ge=0)
    agent_max_search_calls: int = Field(default=1, ge=0)
    agent_max_page_calls: int = Field(default=2, ge=0)
    agent_max_inspections: int = Field(default=3, ge=0)
    vlm_generation_profile: Literal["legacy", "qwen35_nonthinking"] = "legacy"
    # vLLM computes remaining context using its actual tokenizer and chat template.
    vlm_native_output_budget: bool = False

    # **模型名不在这里。** 8100 上跑 llama.cpp 还是 vLLM、名字叫什么，由部署方决定，而且
    # 会在 2.0 运行期间被换掉——2026-09-24 实测：8100 从 llama.cpp 换成 vLLM，2.0 配的
    # ``gemma4-qat-iq3xxs`` 立刻 404，助手整条链路停摆。写一个常量在这儿，就是拿配置去追
    # 运行时的状态，正是本模块开头点名的那种坏法（「1.0 的失败模式是 ~126 env flags whose
    # values disagreed with each other and with the code」）。
    # 名字由 infrastructure/assistant/model_endpoint.py 问端点自己的 ``GET /v1/models``
    # 得到；探测失败就大声报错，不退回任何默认值。
    chat_timeout_s: float = 120.0

    # 8100 上**一个并发槽**的上下文窗口。llama.cpp 的 ``--ctx-size`` 是**整台服务**的
    # 总窗口，要除以 ``--parallel`` 才是单次请求能用的那一份：生产是
    # ``--ctx-size 24576 --parallel 4`` → **6144 / 槽**（2026-10-07 实测）。
    #
    # **为什么它是配置而不是常量。** 1.0 的 ``4400`` 是照它自己的
    # ``max_model_len = 4501`` 算的（``app.py:2734`` 的注释：「Phase H H4：
    # max_model_len=4501 的硬上限保护」）。2.0 换了端点、窗口变成 6144，而那个常量
    # 没跟着改——于是 ``4400 - prompt`` 在 ``prompt > 4336`` 时给负数、被下限抬成 64，
    # 发出去一个注定被截断的请求。这和「模型名不写死」是同一条理由：**窗口是部署事实，
    # 不是代码事实**（见本模块开头对 126 个 env flag 的那段）。
    vlm_context_tokens: int = 6144

    # 视觉观察走同一个端点（multimodal 可用），模型名同样来自探测。
    # 一张图最多 3 次调用（首轮 / 补全 / 中文化），每次是一次带图的推理，比纯文本慢得多；
    # 整册导入是**串行**的（单卡 GPU），所以这个超时按单次调用算，不是按一张图算。
    vlm_timeout_s: float = 180.0

    # 整册导入的预取窗口：同时在飞的**推理**数（视觉嵌入 / VLM 观察 / 人脸检测）。
    # 1 就是原先的逐张串行。默认 4 是 1.0 在同一类机器上的实测值
    # （``app.py:366`` 的 ``effective_workers``，11.78 秒/张；2.0 串行时 49.1 秒/张）。
    # **提交永远串行**，与这个数无关——事件的归属顺序是语义的一部分。
    album_workers: int = 4

    def model_post_init(self, _context: Any, /) -> None:  # noqa: D105
        self.data_dir = self.data_dir.expanduser().resolve()
        self.web_dist_dir = self.web_dist_dir.expanduser().resolve()
        # **profile 只填默认值，显式环境变量永远优先。** ``model_fields_set`` 含 env
        # 解析出来的字段；显式设了就不再覆盖。
        mac = self.deployment_profile == "mac"
        if "embedding_scheme" not in self.model_fields_set:
            self.embedding_scheme = "legacy" if mac else "wemm"
        if "agent_wall_time_s" not in self.model_fields_set:
            self.agent_wall_time_s = 600.0 if mac else 180.0
        if "chat_timeout_s" not in self.model_fields_set:
            self.chat_timeout_s = 180.0 if mac else 120.0
        # **未知方案直接启动失败。** 静默回退到默认会让「配错了」变成「跑的是另一套
        # 模型」——两套模型的向量互不兼容，而检索侧不会报错，只会算不出相似度。
        if self.embedding_scheme not in EMBEDDING_SCHEMES:
            available = ", ".join(sorted(EMBEDDING_SCHEMES))
            raise ValueError(f"SENTRIX_EMBEDDING_SCHEME={self.embedding_scheme!r} 未知；可选：{available}")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_path(self) -> Path:
        return self.data_dir / "sentrix.db"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def preview_dir(self) -> Path:
        return self.data_dir / "previews"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def qdrant_dir(self) -> Path:
        """派生向量索引的目录。

        **必须由 data_dir 派生**，不允许单独配路径。1.0 的 ``data/qdrant`` 被 8091
        进程以 flock 独占（嵌入式 Qdrant 的目录只能一个进程用），2.0 若指过去，
        第二个实例起不来——而且是那种「起了、只是检索悄悄降级成全表扫」的坏法。
        派生自 data_dir 意味着换数据目录就整体换，不存在「只把索引指到 1.0」的口子。
        """
        return self.data_dir / "qdrant"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def geo_dir(self) -> Path:
        """GeoNames dump 的目录（``*.txt`` / ``*.zip``）。

        **同样由 data_dir 派生**，且**不进版本控制**：官方子集 35 MB、全量 402 MB，
        放进仓库是拿源码托管当数据分发。部署方按 ``docs/runbooks/geonames-data.md``
        把文件放到这里，``GeoNamesNearestResolver`` 扫该目录。
        """
        return self.data_dir / "geo"

    def ensure_directories(self) -> None:
        for path in (self.data_dir, self.media_dir, self.preview_dir, self.geo_dir):
            path.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    return Settings()
