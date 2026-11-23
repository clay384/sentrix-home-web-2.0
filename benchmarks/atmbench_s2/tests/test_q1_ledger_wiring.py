"""Checks the optional ledger recovery wiring without contacting the gateway."""

import json

import pytest
from atm_agent_q1 import run
from atm_agent_q1.adapters import http_json
from atm_agent_q1.transport import LedgerRecoveringTransport


@pytest.fixture
def config(tmp_path):
    source = tmp_path / "sources.jsonl"
    source.write_text(json.dumps({"source_id": "fixture", "modality": "email", "detail": "test"}) + "\n")
    return {
        "framework": "sentrix_s2",
        "system": "S2-Q1",
        "sources_jsonl": str(source),
        "memory_base_url": "http://127.0.0.1:19720",
    }


def test_config_preserves_direct_gateway_by_default(config, tmp_path, monkeypatch):
    monkeypatch.setattr(run.CommonAgent, "run", lambda self, *_args, **_kwargs: self.model)
    config["gateway_url"] = "http://127.0.0.1:12345/"
    model = run.run_question(
        config, {"id": "q", "question": "Sample"}, attempt_id="a", trace_dir=tmp_path / "trace"
    )
    assert model.url == "http://127.0.0.1:12345/v1/chat/completions"
    assert model.transport is http_json


def test_explicit_gateway_ledger_uses_same_trace_dir_for_recovery(config, tmp_path, monkeypatch):
    monkeypatch.setattr(run.CommonAgent, "run", lambda self, *_args, **_kwargs: (self.model, self.config))
    ledger = tmp_path / "gateway" / "requests.sqlite3"
    ledger.parent.mkdir()
    ledger.touch()
    trace_dir = tmp_path / "trace"
    config["gateway_ledger"] = str(ledger)
    model, profile = run.run_question(
        config, {"id": "q", "question": "Sample"}, attempt_id="a", trace_dir=trace_dir
    )
    assert model.url == "http://127.0.0.1:19600/v1/chat/completions"
    assert isinstance(model.transport, LedgerRecoveringTransport)
    assert model.transport.ledger == ledger and model.transport.trace_dir == trace_dir
    assert model.transport.initial is http_json
    assert profile.run_id == "atmbench-20261116" and profile.system == "S2-Q1"


@pytest.mark.parametrize(
    "incompatible",
    [
        {"gateway_url": "http://127.0.0.1:12345"},
        {"run_id": "other-run"},
        {"system": "M1-Q0"},
    ],
)
def test_unrecognized_gateway_profile_refused_before_dispatch(config, tmp_path, incompatible):
    config.update(gateway_ledger=str(tmp_path / "gateway" / "requests.sqlite3"), **incompatible)
    trace_dir = tmp_path / "trace"
    with pytest.raises(ValueError, match="gateway_recovery_requires_frozen_benchmark_profile"):
        run.run_question(config, {"id": "q", "question": "Sample"}, attempt_id="a", trace_dir=trace_dir)
    assert not trace_dir.exists()


def test_missing_gateway_ledger_refused_before_dispatch(config, tmp_path):
    config["gateway_ledger"] = str(tmp_path / "gateway" / "requests.sqlite3")
    with pytest.raises(ValueError, match="gateway_recovery_requires_existing_gateway_ledger"):
        run.run_question(
            config, {"id": "q", "question": "Sample"}, attempt_id="a", trace_dir=tmp_path / "trace"
        )
    assert not (tmp_path / "trace").exists()
