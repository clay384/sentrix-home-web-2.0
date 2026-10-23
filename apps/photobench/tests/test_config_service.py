"""``ConfigService`` + ``JsonConfigStore`` 的用例。

钉住三件事：**取值优先级**（环境 > 文件 > 兜底）、**哪些配置坏了要报错哪些要吞**、
**写回之后就地生效**（1.0 的 ``global`` 语义）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from photobench.application.config.service import (
    ConfigService,
    normalize_model_base_url,
    normalize_service_url,
)
from photobench.domain.errors import InvalidRequestError
from photobench.infrastructure.config.env import DotenvSecretStore
from photobench.infrastructure.config.files import JsonConfigStore

PROVIDERS = {
    "default_provider_id": "cloud",
    "providers": {
        "cloud": {"label": "云", "url": "http://judge:1/v1", "model": "j1", "api_key": "secret-key"},
        "local": {"label": "本地", "url": "http://judge:2/v1", "model": "j2", "supports_vision": True},
    },
}
TARGETS = {
    "default_target_id": "local",
    "targets": {
        "local": {"label": "本机", "manager_url": "", "model_base_url": "http://127.0.0.1:8100/v1"},
        "managed": {"label": "153", "manager_url": "http://153:8500", "model_base_url": "http://153:8100/v1"},
    },
}


@pytest.fixture
def config_root(tmp_path: Path) -> Path:
    (tmp_path / "runtime_connection.json").write_text(
        json.dumps({"sentrix_url": "http://from-file:8091", "judge_provider_id": "local"}),
        encoding="utf-8",
    )
    (tmp_path / "judge_providers.json").write_text(json.dumps(PROVIDERS), encoding="utf-8")
    (tmp_path / "vllm_targets.json").write_text(json.dumps(TARGETS), encoding="utf-8")
    return tmp_path


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """BENCH_* 会盖过配置文件；用例要断言文件的值，就得先把环境清干净。"""
    for name in (
        "BENCH_SENTRIX_URL",
        "BENCH_JUDGE_URL",
        "BENCH_JUDGE_MODEL",
        "BENCH_JUDGE_API_KEY",
        "BENCH_VLLM_API_URL",
        "BENCH_VLLM_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


def _service(root: Path) -> ConfigService:
    # S9b-3 起 ConfigService 还收一个 ``.env.local`` 的读写端口（judge 密钥）。
    return ConfigService(JsonConfigStore(root), DotenvSecretStore(root))


def test_file_values_win_over_the_builtin_fallbacks(config_root: Path):
    config = _service(config_root)
    assert config.sentrix_url == "http://from-file:8091"
    assert config.judge_provider_id == "local"


def test_environment_wins_over_the_file(config_root: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BENCH_SENTRIX_URL", "http://from-env:9598")
    assert _service(config_root).sentrix_url == "http://from-env:9598"


def test_an_explicitly_empty_environment_value_means_unset(
    config_root: Path, monkeypatch: pytest.MonkeyPatch
):
    """1.0 ``:107`` 的注释：显式空串表示「没配」，**不许**再落到别的机器上。"""
    monkeypatch.setenv("BENCH_SENTRIX_URL", "")
    assert _service(config_root).sentrix_url == ""


def test_model_base_url_falls_back_to_the_default_target(config_root: Path):
    """1.0 ``:345-350``：没配 base_url 时退回默认 target 的地址。"""
    assert _service(config_root).model_base_url == "http://127.0.0.1:8100/v1"


def test_the_judge_api_key_comes_from_the_environment_first(
    config_root: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("BENCH_JUDGE_API_KEY", "from-env")
    _, _, _, key = _service(config_root).resolve_judge_provider("cloud")
    assert key == "from-env"
    monkeypatch.delenv("BENCH_JUDGE_API_KEY")
    _, _, _, key = _service(config_root).resolve_judge_provider("cloud")
    assert key == "secret-key"


def test_a_missing_provider_config_falls_back_to_the_example(tmp_path: Path):
    (tmp_path / "judge_providers.example.json").write_text(json.dumps(PROVIDERS), encoding="utf-8")
    (tmp_path / "vllm_targets.example.json").write_text(json.dumps(TARGETS), encoding="utf-8")
    config = _service(tmp_path)
    assert config.judge_provider_id == "cloud"
    assert config.vllm_target_id == "local"


def test_a_missing_provider_config_without_an_example_is_an_error(tmp_path: Path):
    with pytest.raises(InvalidRequestError) as excinfo:
        _service(tmp_path)
    assert "invalid judge provider config" in str(excinfo.value)
    assert str(tmp_path) in str(excinfo.value)  # 1.0 的文案带完整路径


def test_a_broken_provider_config_is_an_error_not_a_silent_fallback(config_root: Path):
    """judge provider 决定了打分的口径——静默退回默认值会把分算在另一个模型上。"""
    (config_root / "judge_providers.json").write_text("{ not json", encoding="utf-8")
    with pytest.raises(InvalidRequestError) as excinfo:
        _service(config_root)
    assert "judge_providers.json" in str(excinfo.value)


def test_a_broken_runtime_connection_is_swallowed(config_root: Path):
    """1.0 ``:71``：连接配置是运行参数，坏了当空（改错了要能进去改回来）。"""
    (config_root / "runtime_connection.json").write_text("{ not json", encoding="utf-8")
    config = _service(config_root)
    assert config.sentrix_url == "http://127.0.0.1:9598"  # 兜底


def test_public_judge_providers_never_returns_the_whole_key(config_root: Path):
    providers = {entry["id"]: entry for entry in _service(config_root).public_judge_providers()}
    assert providers["cloud"]["api_key_set"] is True
    assert providers["cloud"]["api_key_hint"] == "se******ey"
    assert "secret-key" not in json.dumps(providers)
    assert providers["local"]["api_key_set"] is False


def test_resolve_judge_provider_rejects_an_unknown_id(config_root: Path):
    with pytest.raises(InvalidRequestError):
        _service(config_root).resolve_judge_provider("nope")


def test_persist_writes_the_file_and_takes_effect_immediately(config_root: Path):
    """写回之后就地生效——1.0 用 ``global`` 改模块级变量，等价物在这里。"""
    config = _service(config_root)
    saved = config.persist_runtime_connection(
        {
            "sentrix_url": "http://new-host:9598",
            "judge_url": "http://judge-new/v1",
            "judge_model": "m",
            "model_base_url": "http://new-host:8100",
            "judge_provider_id": "cloud",
        }
    )
    assert saved["sentrix_url"] == "http://new-host:9598"
    assert config.sentrix_url == "http://new-host:9598"
    on_disk = json.loads((config_root / "runtime_connection.json").read_text(encoding="utf-8"))
    assert on_disk["sentrix_url"] == "http://new-host:9598"
    assert on_disk["model_base_url"] == "http://new-host:8100/v1"  # 归一化后落盘


def test_persist_requires_a_judge_model(config_root: Path):
    with pytest.raises(InvalidRequestError) as excinfo:
        _service(config_root).persist_runtime_connection(
            {
                "sentrix_url": "http://x:9598",
                "judge_url": "http://j/v1",
                "judge_model": "",
                "judge_provider_id": "cloud",
            }
        )
    assert "Judge model is required" in str(excinfo.value)


def test_persist_validates_the_sentrix_url_before_the_judge_model(config_root: Path):
    """1.0 的校验顺序如此（先建整个 values 再查 judge_model，``:424-441``）。

    一条**顺序**断言：把两个校验调换位置就会红——而「先报哪一条」是前端显示给用户的
    那句话，不是实现细节。
    """
    with pytest.raises(InvalidRequestError) as excinfo:
        _service(config_root).persist_runtime_connection({"judge_model": "", "judge_provider_id": "cloud"})
    assert "Sentrix URL must be a host:port" in str(excinfo.value)


def test_persist_rejects_an_unknown_provider(config_root: Path):
    with pytest.raises(InvalidRequestError):
        _service(config_root).persist_runtime_connection({"judge_model": "m", "judge_provider_id": "nope"})


def test_persist_writes_the_api_key_into_dotenv_not_the_json(config_root: Path):
    """S9b-1 的这条用例钉的是「密钥写入尚未接线」；S9b-3 接线了，按实情改写。

    **它现在钉的是落点**，不是「有没有拒绝」：密钥进 ``.env.local``（0600），
    不进 ``runtime_connection.json``——后者会被复制、会被贴进工单、会进版本库。
    只断言「保存成功」的用例对一个把密钥写进 JSON 的实现同样成立。
    """
    config = _service(config_root)
    config.persist_runtime_connection(
        {
            "sentrix_url": "http://x:9598",
            "judge_url": "http://j/v1",
            "judge_model": "m",
            "judge_provider_id": "cloud",
            "judge_api_key": "sekret-value",
        }
    )
    assert config.judge_api_key == "sekret-value"
    env_text = (config_root / ".env.local").read_text(encoding="utf-8")
    assert "BENCH_JUDGE_API_KEY=sekret-value" in env_text
    saved = json.loads((config_root / "runtime_connection.json").read_text(encoding="utf-8"))
    assert "judge_api_key" not in saved


def test_a_multiline_secret_is_refused(config_root: Path):
    """``.env.local`` 是 ``KEY=VALUE`` 的行格式；多行值会被 ``shlex`` 解析成别的东西。"""
    config = _service(config_root)
    with pytest.raises(ValueError):
        config.persist_runtime_connection(
            {
                "sentrix_url": "http://x:9598",
                "judge_url": "http://j/v1",
                "judge_model": "m",
                "judge_provider_id": "cloud",
                "judge_api_key": "line1\nline2",
            }
        )


def test_persist_rejects_a_url_without_a_host(config_root: Path):
    with pytest.raises(InvalidRequestError):
        _service(config_root).persist_runtime_connection(
            {"judge_model": "m", "judge_provider_id": "cloud", "sentrix_url": "http://"}
        )


def test_resolve_vllm_target_falls_back_to_an_ad_hoc_endpoint(config_root: Path):
    target_id, target = _service(config_root).resolve_vllm_target(
        None, manager_url="", model_base_url="http://x:1/v1"
    )
    assert target_id == "local"  # 默认 target 命中，就不算 ad-hoc
    target_id, target = _service(config_root).resolve_vllm_target(
        "unknown", manager_url="http://m", model_base_url="http://x:1/v1"
    )
    assert target_id == "unknown" and target["manager_url"] == "http://m"


def test_normalize_helpers():
    assert normalize_model_base_url("1.2.3.4:8100") == "http://1.2.3.4:8100/v1"
    assert normalize_model_base_url("http://x/v1/") == "http://x/v1"
    assert normalize_model_base_url("") == ""
    assert normalize_service_url("x:8500/") == "http://x:8500"
