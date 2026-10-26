"""Typed model operational failures cross the real API boundary safely."""

import pytest

from sentrix.application.media.import_album import ProcessAlbumPipeline
from sentrix.domain.assistant.ports import ModelServiceError


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("time_limit", 504),
        ("provider_unavailable", 503),
        ("provider_rejected", 502),
        ("context_limit", 422),
        ("output_limit", 502),
        ("provider_response_invalid", 502),
        ("model_output_invalid", 502),
    ],
)
def test_pipeline_model_failure_returns_safe_typed_detail(client, monkeypatch, code, status):
    def fail(*_args, **_kwargs):
        raise ModelServiceError(
            "PRIVATE_PROVIDER_BODY",
            error_code=code,
            http_status=429,
            finish_reason="length",
            prompt_tokens=123,
            completion_tokens=456,
            request_policy={
                "request_outcome_unknown": True,
                "output_budget": "native_remaining_context",
                "request_timeout_s": 1800,
                "seed": 42,
                "json_output_mode": "prompt_only",
                "prompt": "PRIVATE_PROMPT",
                "image": "PRIVATE_IMAGE",
                "api_key": "PRIVATE_KEY",
                "chat_template_kwargs": {"enable_thinking": False, "secret": "PRIVATE"},
                "response_format": {"type": "json_object", "schema": "PRIVATE"},
                "temperature": "PRIVATE",
                "top_p": float("nan"),
            },
        )

    monkeypatch.setattr(ProcessAlbumPipeline, "execute", fail)
    response = client.post("/api/v1/pipeline/process", json={"scope_id": "test", "asset_ids": []})
    assert response.status_code == status
    body = response.json()
    assert body["code"] == code
    assert body["detail"]["error_code"] == code
    assert body["detail"]["http_status"] == 429
    assert body["detail"]["completion_tokens"] == 456
    policy = body["detail"]["request_policy"]
    assert policy["request_outcome_unknown"] is True
    assert policy["seed"] == 42
    assert policy["json_output_mode"] == "prompt_only"
    assert policy["chat_template_kwargs"] == {"enable_thinking": False}
    assert policy["response_format"] == {"type": "json_object"}
    assert "PRIVATE" not in response.text
    assert "temperature" not in policy
    assert "top_p" not in policy


def test_unknown_model_error_code_and_metadata_cannot_leak_to_http(client, monkeypatch):
    def fail(*_args, **_kwargs):
        raise ModelServiceError(
            "PRIVATE_MESSAGE",
            error_code="PRIVATE_CODE",
            http_status=999,
            finish_reason="PRIVATE_FINISH",
            prompt_tokens=-1,
            completion_tokens=True,
        )

    monkeypatch.setattr(ProcessAlbumPipeline, "execute", fail)
    response = client.post("/api/v1/pipeline/process", json={"scope_id": "test", "asset_ids": []})
    assert response.status_code == 503
    assert response.json()["code"] == "provider_unavailable"
    assert response.json()["detail"] == {"error_code": "provider_unavailable", "request_policy": {}}
    assert "PRIVATE" not in response.text
