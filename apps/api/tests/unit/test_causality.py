"""hpq rules_vlm 的纯规则、解析与故障语义。"""

from __future__ import annotations

import httpx
import pytest

from sentrix.domain.assistant.ports import ModelServiceError
from sentrix.domain.memories.causality import CausalFrame, CausalParseError, extract_causal_pairs
from sentrix.infrastructure.memories.causal_judge import CausalInferenceError, OpenAICausalJudge


def _frame(asset_id: str, parent: str, second: float, relation: str) -> CausalFrame:
    return CausalFrame(
        asset_id=asset_id,
        parent_asset_id=parent,
        timestamp_sec=second,
        frame_index=0,
        objects=("person", "bottle", "table"),
        relations=(relation,),
    )


def test_causal_candidates_require_same_video_stable_scene_and_relation_change():
    frames = (
        _frame("a", "video-1", 1.0, "person holds bottle"),
        _frame("b", "video-1", 4.0, "person puts bottle"),
        _frame("c", "video-2", 5.0, "person opens bottle"),
        _frame("d", "video-1", 45.0, "person closes bottle"),
    )

    pairs = extract_causal_pairs(frames)

    assert [(pair.before.asset_id, pair.after.asset_id) for pair in pairs] == [("a", "b")]
    assert pairs[0].signals.rel_gone == ("person holds bottle",)
    assert pairs[0].signals.rel_new == ("person puts bottle",)


def test_object_appearance_alone_and_unstable_scene_are_not_causality():
    first = _frame("a", "video", 1.0, "person holds bottle")
    appeared = CausalFrame("b", "video", 2.0, 0, (*first.objects, "cup"), first.relations)
    switched = CausalFrame("c", "video", 3.0, 0, ("car", "road"), ("car on road",))

    assert extract_causal_pairs((first, appeared, switched)) == ()


def test_causal_response_requires_every_pair_and_valid_confidence():
    parse = OpenAICausalJudge._parse
    verdicts = parse(
        '{"results":[{"pair_id":0,"has_causal":true,"edge_type":"ENABLES",'
        '"confidence":0.81,"reason":"打开门"}]}',
        1,
    )
    assert verdicts[0].edge_type == "ENABLES"
    with pytest.raises(ValueError, match="missing"):
        parse('{"results":[]}', 1)
    with pytest.raises(ValueError, match="confidence"):
        parse(
            '{"results":[{"pair_id":0,"has_causal":true,"edge_type":"LEADS_TO",'
            '"confidence":1.5,"reason":"x"}]}',
            1,
        )


def test_causal_adapter_sends_text_only_and_fails_closed(monkeypatch):
    class _Served:
        def current(self):
            return "local-model"

        def generation_parameters(self):
            return {"temperature": 0.7, "seed": 42}

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": '{"results":[{"pair_id":0,"has_causal":false,'
                            '"edge_type":"NONE","confidence":0.1,"reason":"无"}]}'
                        }
                    }
                ]
            }

    calls = []

    def _post(url, *, json, timeout):
        calls.append((url, json, timeout))
        return _Response()

    monkeypatch.setattr(httpx, "post", _post)
    judge = OpenAICausalJudge("http://127.0.0.1:8100/v1", _Served(), timeout_s=30)
    result = judge.judge("signals only", expected_count=1)
    assert not result[0].has_causal
    assert calls[0][1]["messages"] == [{"role": "user", "content": "signals only"}]
    assert calls[0][1]["model"] == "local-model"
    assert calls[0][2] == 30
    assert "max_tokens" not in calls[0][1]
    assert calls[0][1]["seed"] == 42

    def _fail(url, *, json, timeout):
        raise httpx.ConnectError("unavailable")

    monkeypatch.setattr(httpx, "post", _fail)
    with pytest.raises(CausalInferenceError):
        judge.judge("signals only", expected_count=1)


class _PolicyModel:
    def current(self):
        return "local-model"

    def generation_parameters(self):
        return {"temperature": 0.7, "seed": 42}


@pytest.mark.parametrize("status", [400, 429, 503])
def test_causal_http_rejection_preserves_operational_status_without_retry(monkeypatch, status):
    calls = []

    def post(url, **kwargs):
        calls.append(kwargs)
        return httpx.Response(
            status, json={"error": "PRIVATE_PROVIDER_BODY"}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(CausalInferenceError) as caught:
        OpenAICausalJudge("http://local/v1", _PolicyModel(), timeout_s=1800).judge(
            "signals", expected_count=1
        )
    assert isinstance(caught.value, ModelServiceError)
    assert caught.value.error_code == "provider_rejected"
    assert caught.value.http_status == status
    assert caught.value.request_policy["timeout_s"] == 1800
    assert "PRIVATE" not in str(caught.value)
    assert len(calls) == 1


def test_causal_timeout_remains_unknown_and_is_not_a_parse_error(monkeypatch):
    def post(*_args, **_kwargs):
        raise httpx.ReadTimeout("PRIVATE_REQUEST")

    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(CausalInferenceError) as caught:
        OpenAICausalJudge("http://local/v1", _PolicyModel(), timeout_s=1800).judge(
            "signals", expected_count=1
        )
    assert caught.value.error_code == "time_limit"
    assert caught.value.request_policy["request_outcome_unknown"] is True


def test_causal_length_is_operational_even_with_valid_relationship_json(monkeypatch):
    content = '{"results":[{"pair_id":0,"has_causal":false,"edge_type":"NONE","confidence":0.1}]}'

    def post(url, **_kwargs):
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "length", "message": {"content": content}}],
                "usage": {"prompt_tokens": 123, "completion_tokens": 456},
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(CausalInferenceError) as caught:
        OpenAICausalJudge("http://local/v1", _PolicyModel(), timeout_s=1800).judge(
            "signals", expected_count=1
        )
    assert caught.value.error_code == "output_limit"
    assert caught.value.finish_reason == "length"
    assert caught.value.prompt_tokens == 123
    assert caught.value.completion_tokens == 456


def test_causal_completed_invalid_model_json_remains_quality_parse_error(monkeypatch):
    def post(url, **_kwargs):
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {"content": '{"results":[]}'}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(CausalParseError):
        OpenAICausalJudge("http://local/v1", _PolicyModel(), timeout_s=1800).judge(
            "signals", expected_count=1
        )
