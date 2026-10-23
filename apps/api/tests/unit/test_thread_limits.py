"""#5 算子线程上限(2026-10-20 xy 取证)的单元测试。

事故原型:153 上 241 线程抢 32 核 → 健康检查超时 → 评测被 SIGTERM。
cv2/torch 缺席用 ``sys.modules[name] = None`` 模拟(import 会抛 ImportError,
与「真没装」同路径);session 工厂用假 onnxruntime 验证预算落位。
"""

from __future__ import annotations

import os
import sys
from types import ModuleType, SimpleNamespace

import pytest

from sentrix.infrastructure import thread_limits


@pytest.fixture(autouse=True)
def _reset_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(thread_limits, "_APPLIED", False)
    monkeypatch.delenv("SENTRIX_LIB_THREADS", raising=False)


def test_lib_threads_defaults_to_a_quarter_of_the_cpus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "cpu_count", lambda: 32)
    assert thread_limits.lib_threads() == 8


def test_lib_threads_env_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTRIX_LIB_THREADS", "3")
    assert thread_limits.lib_threads() == 3


def test_apply_is_tolerant_to_missing_heavy_libs_and_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cv2/torch 都缺席时不抛、不残留半套状态;第二次调用不再重复设。"""
    monkeypatch.setitem(sys.modules, "cv2", None)
    monkeypatch.setitem(sys.modules, "torch", None)
    assert thread_limits.apply_process_thread_limits() == {}

    calls: list[int] = []
    fake_cv2 = ModuleType("cv2")
    fake_cv2.setNumThreads = calls.append  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)
    monkeypatch.setenv("SENTRIX_LIB_THREADS", "5")
    monkeypatch.setattr(thread_limits, "_APPLIED", False)
    assert thread_limits.apply_process_thread_limits() == {"cv2": 5}
    assert calls == [5]
    # 幂等:第二次直接空手而归,cv2.setNumThreads 不会被再调。
    assert thread_limits.apply_process_thread_limits() == {}
    assert calls == [5]


def test_ort_session_options_carries_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """线程数只能从 session options 进 onnxruntime(环境变量无效,xy 的结论)。"""
    entries: dict[str, str] = {}

    def fake_session_options() -> SimpleNamespace:
        options = SimpleNamespace(intra_op_num_threads=0, inter_op_num_threads=0)
        options.add_session_config_entry = lambda key, value: entries.__setitem__(key, value)  # type: ignore[attr-defined]
        return options

    fake_ort = ModuleType("onnxruntime")
    fake_ort.SessionOptions = fake_session_options  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)
    monkeypatch.setenv("SENTRIX_LIB_THREADS", "4")
    options = thread_limits.ort_session_options()
    assert options.intra_op_num_threads == 4
    assert options.inter_op_num_threads == 4
    assert entries["session.intra_op.allow_spinning"] == "0"
    assert entries["session.inter_op.allow_spinning"] == "0"
