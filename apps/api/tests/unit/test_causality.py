"""hpq rules_vlm 的纯规则、解析与故障语义。"""

from __future__ import annotations

import httpx
import pytest

from sentrix.domain.memories.causality import CausalFrame, extract_causal_pairs
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

    def _fail(url, *, json, timeout):
        raise httpx.ConnectError("unavailable")

    monkeypatch.setattr(httpx, "post", _fail)
    with pytest.raises(CausalInferenceError):
        judge.judge("signals only", expected_count=1)
