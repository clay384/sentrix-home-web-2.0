from sentrix.config import Settings


def test_defaults_point_at_2_0_ports_and_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTRIX_DATA_DIR", str(tmp_path))
    settings = Settings()

    assert settings.api_port == 8191
    assert settings.data_dir == tmp_path
    assert settings.database_path == tmp_path / "sentrix.db"
    assert settings.media_dir == tmp_path / "media"
    assert settings.preview_dir == tmp_path / "previews"


def test_env_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTRIX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SENTRIX_API_PORT", "9001")

    assert Settings().api_port == 9001


def test_required_dirs_are_derived_not_read_from_env(tmp_path, monkeypatch):
    """data_dir 是唯一可配的根，其余路径都由它派生，避免 1.0 那种路径散落。"""
    monkeypatch.setenv("SENTRIX_DATA_DIR", str(tmp_path))
    settings = Settings()

    assert settings.media_dir.parent == settings.data_dir
    assert settings.preview_dir.parent == settings.data_dir


def test_no_model_name_is_configurable(tmp_path, monkeypatch):
    """反向门禁（用户 2026-09-25 裁决一）：模型名不许回到配置里。

    2026-09-24 的事故就是这一条：``chat_model_name`` 是 llama.cpp 时代的别名，8100 被换成
    vLLM（服务 ``gemma4-12b-it``）之后它变成 404，而 2.0 的代码一点动静都没有——写死的常量
    与运行时事实脱节，只有用户看得见（「无法可靠规划」）。

    所以模型名只有一个来源：问端点的 ``GET /v1/models``（``ServedModel``）。配置里一旦又能
    配，就又能漂回去。这条用例一旦变红，说明有人把那个口子重新打开了。
    """
    monkeypatch.setenv("SENTRIX_DATA_DIR", str(tmp_path))
    settings = Settings()

    assert not hasattr(settings, "chat_model_name")
    assert not hasattr(settings, "vlm_model_name")


def test_mac_profile_defaults_and_explicit_values(monkeypatch, tmp_path):
    from sentrix.config import Settings

    monkeypatch.delenv("SENTRIX_EMBEDDING_SCHEME", raising=False)
    monkeypatch.delenv("SENTRIX_CHAT_TIMEOUT_S", raising=False)
    monkeypatch.delenv("SENTRIX_AGENT_WALL_TIME_S", raising=False)
    settings = Settings(deployment_profile="mac", data_dir=tmp_path)
    assert settings.embedding_scheme == "legacy"
    assert settings.agent_wall_time_s == 600
    assert settings.chat_timeout_s == 180
    explicit = Settings(
        deployment_profile="mac", embedding_scheme="wemm", agent_wall_time_s=77, chat_timeout_s=88
    )
    assert (explicit.embedding_scheme, explicit.agent_wall_time_s, explicit.chat_timeout_s) == (
        "wemm",
        77,
        88,
    )


def test_invalid_deployment_profile_rejected():
    import pytest

    from sentrix.config import Settings

    with pytest.raises(ValueError):
        Settings(deployment_profile="unknown")


def test_explicit_30_step_native_budget_is_reported_without_changing_legacy_defaults(tmp_path):
    baseline = Settings(data_dir=tmp_path)
    assert baseline.agent_max_steps == 8
    settings = Settings(
        data_dir=tmp_path,
        agent_max_steps=30,
        agent_max_tool_calls=28,
        agent_max_search_calls=28,
        agent_max_page_calls=28,
        agent_max_inspections=28,
        vlm_context_tokens=262144,
        vlm_native_output_budget=True,
        vlm_generation_profile="qwen35_nonthinking",
    )
    actual = settings.runtime_configuration()
    assert actual["agent_max_steps"] == 30 and actual["agent_max_tool_calls"] == 28
    assert actual["vlm_native_output_budget"] is True
    assert actual["vlm_context_tokens"] == 262144
    assert actual["vlm_generation_profile"] == "qwen35_nonthinking"


def test_contradictory_agent_limits_are_rejected():
    import pytest

    with pytest.raises(ValueError, match="reserve"):
        Settings(agent_max_steps=8, agent_max_tool_calls=30)
    with pytest.raises(ValueError, match="agent_max_search_calls"):
        Settings(agent_max_search_calls=7)
