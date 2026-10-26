"""视觉观察器的纯函数部分：回复解析、字段收敛、以及三段式的调用上限。

http 那一层不在这里打真端点（8100 是 1.0 的服务，生命周期不归 2.0 管）；
``_chat`` 用 monkeypatch 换成脚本化的假回复，验的是 1.0 ``analyze_image`` 的三段结构：
首轮 → 只回分类时补一次 → 出现拉丁文字时中文化，**三段三调用，一次不多**。

「端点挂了要返回 ``None``」那两条打的是**本文件自己起的**本地 HTTP 服务：探测与调用
都是真 HTTP，只是端点归测试管，不打 8100。
"""

from __future__ import annotations

import base64
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from sentrix.domain.assistant.ports import ModelServiceError
from sentrix.infrastructure.assistant.model_endpoint import ServedModel
from sentrix.infrastructure.media.vlm_observer import (
    MAX_MODEL_CALLS,
    OBSERVATION_CONFIDENCE,
    OpenAIVlmObserver,
    parse_json_object,
    semantics_from_payload,
)


class _Endpoint(BaseHTTPRequestHandler):
    """应答 ``/v1/models``；**记录每个 POST 的请求体**；对话端点默认答 503。

    ``reply`` 不设时故意答 503——``test_a_chat_failure_returns_none`` 要的就是那条路。
    记录请求体的目的是让「两处是不是共用同一个常量」**可断言**：原先那条用例是拿
    ``== '{"max": 1501}'`` 断言的，那是把常量抄了一遍，守的是值不是关系。
    """

    received: list[dict] = []
    reply: dict | None = None
    reply_status = 200

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的接口
        payload = b'{"object":"list","data":[{"id":"fake-vlm","object":"model"}]}'
        self._send(payload, 200)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的接口
        length = int(self.headers.get("Content-Length") or 0)
        type(self).received.append(json.loads(self.rfile.read(length) or b"{}"))
        if type(self).reply is None:
            self._send(b'{"error":"boom"}', 503)
            return
        self._send(json.dumps(type(self).reply).encode(), type(self).reply_status)

    def _send(self, payload: bytes, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture(autouse=True)
def _reset_endpoint() -> None:
    _Endpoint.received = []
    _Endpoint.reply = None
    _Endpoint.reply_status = 200


@pytest.fixture
def endpoint() -> Iterator[str]:
    httpd = HTTPServer(("127.0.0.1", 0), _Endpoint)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}/v1"
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"caption": "海边"}', {"caption": "海边"}),
        ('```json\n{"caption": "海边"}\n```', {"caption": "海边"}),
        ('前面一段话 {"caption": "海边"} 后面一段话', {"caption": "海边"}),
        ("不是 JSON", None),
        ("[1, 2, 3]", None),
        ("", None),
    ],
)
def test_reply_parsing(text: str, expected):
    assert parse_json_object(text) == expected


def test_semantics_only_keeps_the_s7_fields():
    """1.0 的 semantic 受控词表与 detail 不属于 S4，多余字段必须被丢掉。"""
    payload = {
        "caption": " 一家人在公园 ",
        "activity": "拍照",
        "place": "公园",
        "event_type": "户外活动",
        "ocr_text": "生日快乐",
        "objects": ["相机", "", " 长椅 "],
        "clothing": "外套",
        "scene_type": "户外",
        "detail": {"visible_details": ["树"]},
        "facts": [{"subject": "妈妈", "predicate": "去过", "object": "赵县", "confidence": 0.8}],
    }

    semantics = semantics_from_payload(payload)

    assert semantics.caption == "一家人在公园"
    assert semantics.objects == ("相机", "长椅")
    assert semantics.clothing == ("外套",)
    assert semantics.confidence == pytest.approx(OBSERVATION_CONFIDENCE)
    assert [(item.subject, item.predicate, item.object, item.confidence) for item in semantics.facts] == [
        ("妈妈", "去过", "赵县", 0.8)
    ]
    assert not hasattr(semantics, "detail")


def test_a_fact_without_a_confidence_gets_the_model_default():
    """1.0 ``model_clients.py:1190``：``normalize_fact_confidences(facts, 0.65)``。

    断言的是那个**具体的数**：默认值换成 0.5 或 1.0 时这条要红——它是事实落库时的
    置信度，会直接影响 ``decide_fact_write`` 的 ``MAX(confidence, ?)``。
    """
    semantics = semantics_from_payload(
        {"caption": "海边", "facts": [{"subject": "妈妈", "predicate": "去过", "object": "赵县"}]}
    )

    assert [item.confidence for item in semantics.facts] == [0.65]


def test_a_fact_with_an_out_of_range_confidence_is_clamped_and_a_junk_one_falls_back():
    semantics = semantics_from_payload(
        {
            "caption": "海边",
            "facts": [
                {"subject": "妈妈", "predicate": "去过", "object": "赵县", "confidence": 7},
                {"subject": "爸爸", "predicate": "去过", "object": "保定", "confidence": "很高"},
                "这不是一条事实",
            ],
        }
    )

    assert [(item.subject, item.confidence) for item in semantics.facts] == [
        ("妈妈", 1.0),
        ("爸爸", 0.65),
    ]


def test_a_fact_missing_a_field_survives_the_parser():
    """**字段齐不齐是 ``is_writable_fact`` 的判据，不是解析器的。**

    解析器在这里替规则判一次，那条规则就有一半永远走不到——而「哪一半被谁判掉了」
    在出问题时是看不见的。
    """
    semantics = semantics_from_payload({"caption": "海边", "facts": [{"subject": "妈妈", "object": "赵县"}]})

    assert len(semantics.facts) == 1
    assert semantics.facts[0].predicate == ""


class ScriptedObserver(OpenAIVlmObserver):
    """把 ``_chat`` 换成脚本：按调用序返回预置回复，并记录次数。"""

    def __init__(self, replies: list[dict | None]) -> None:
        # ``_chat`` 被整个替换掉了，探测永远不会发生。这里故意给一个连不上的地址：
        # 哪天有人把这个覆盖删了，测试会立刻炸，而不是悄悄去打真的 8100。
        super().__init__("http://127.0.0.1:1/v1", ServedModel("http://127.0.0.1:1/v1", timeout_s=1.0))
        self.replies = replies
        self.calls: list[object] = []
        self.prompts: list[str] = []

    def _chat(self, prompt: str, image: str | None):  # noqa: ANN201
        self.calls.append(image)
        self.prompts.append(prompt)
        return self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "a.jpg"
    path.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    return path


def test_a_good_first_reply_costs_exactly_one_call(image):
    observer = ScriptedObserver(
        [{"caption": "海边", "activity": "散步", "place": "海边", "event_type": "出行"}]
    )

    semantics = observer.observe(image)

    assert semantics is not None
    assert len(observer.calls) == 1


def test_a_classification_only_reply_triggers_the_recovery_call(image):
    """1.0 ``model_clients.py:1150``：首轮只给了分类字段时补一次。"""
    observer = ScriptedObserver(
        [
            {"scene_type": "户外"},
            {"caption": "海边", "activity": "散步", "place": "海边", "event_type": "出行"},
        ]
    )

    semantics = observer.observe(image)

    assert semantics is not None
    assert semantics.caption == "海边"
    assert len(observer.calls) == 2


def test_latin_text_triggers_the_canonicalization_call(image):
    observer = ScriptedObserver(
        [
            {"caption": "Beach walk", "activity": "walking", "place": "beach", "event_type": "trip"},
            {"caption": "海边散步", "activity": "散步", "place": "海边", "event_type": "出行"},
        ]
    )

    semantics = observer.observe(image)

    assert semantics is not None
    assert semantics.caption == "海边散步"
    assert len(observer.calls) == 2


def test_the_prompt_asks_the_model_for_facts(image):
    """候选来源是**提示词**的一半：解析器认 facts、提示词不问，模型就不会给。

    只断言「解析器认得 facts」是不够的——那样把提示词里的 facts 删掉，整条路径安静地
    退回「恒无候选」，而事实表恒空不报错。
    """
    observer = ScriptedObserver([{"caption": "海边", "activity": "散步", "place": "海边"}])

    observer.observe(image)

    prompt = observer.prompts[0]
    assert "facts" in prompt
    assert "subject" in prompt and "predicate" in prompt and "object" in prompt
    assert "confidence" in prompt


def test_the_canonicalization_prompt_keeps_facts(image):
    """中文化那一轮要**保留** facts，否则 `翻译一遍` 这个动作会把候选事实整段吃掉。"""
    observer = ScriptedObserver(
        [
            {"caption": "Beach walk", "activity": "walking", "place": "beach"},
            {"caption": "海边散步", "activity": "散步", "place": "海边"},
        ]
    )

    observer.observe(image)

    assert "facts" in observer.prompts[1]


def test_facts_from_the_recovery_call_are_kept(image):
    """首轮一个字都没说画面内容时，补全那一轮给的事实候选同样要留下。"""
    observer = ScriptedObserver(
        [
            {"scene_type": "户外"},
            {
                "caption": "海边",
                "activity": "散步",
                "place": "海边",
                "event_type": "出行",
                "facts": [{"subject": "妈妈", "predicate": "去过", "object": "赵县"}],
            },
        ]
    )

    semantics = observer.observe(image)

    assert semantics is not None
    assert [item.subject for item in semantics.facts] == ["妈妈"]


def test_a_reply_without_facts_yields_an_empty_tuple_not_none(image):
    observer = ScriptedObserver([{"caption": "海边", "activity": "散步", "place": "海边"}])

    semantics = observer.observe(image)

    assert semantics is not None
    assert semantics.facts == ()


def test_the_ceiling_is_three_calls(image):
    """三段全走也只调三次——单卡 GPU 上串行跑批时这个上限就是「一张图的代价」。"""
    observer = ScriptedObserver([{"scene_type": "户外"}, {"caption": "Beach"}, {"caption": "海边"}])

    observer.observe(image)

    assert len(observer.calls) == MAX_MODEL_CALLS == 3


def test_an_unreadable_image_costs_no_calls(tmp_path):
    observer = ScriptedObserver([{"caption": "不该被用到"}])

    assert observer.observe(tmp_path / "gone.jpg") is None
    assert observer.calls == []


def test_the_first_call_carries_the_image_and_the_canonicalization_does_not(image):
    observer = ScriptedObserver([{"scene_type": "分类"}, {"caption": "海边"}])

    observer.observe(image)

    assert observer.calls[0] is not None, "首轮必须带图"
    assert observer.calls[1] is not None, "补全那一轮同样要看图"


def test_a_chat_failure_is_a_typed_operational_error(endpoint, image):
    observer = OpenAIVlmObserver(endpoint, ServedModel(endpoint))
    with pytest.raises(ModelServiceError) as caught:
        observer.observe(image)
    assert caught.value.error_code == "provider_rejected"
    assert caught.value.http_status == 503
    assert len(_Endpoint.received) == 1


def test_an_unreachable_model_discovery_propagates_without_completion_post(image):
    dead = "http://127.0.0.1:1/v1"
    observer = OpenAIVlmObserver(dead, ServedModel(dead, timeout_s=1.0))
    with pytest.raises(ModelServiceError) as caught:
        observer.observe(image)
    assert caught.value.error_code == "provider_unavailable"
    assert caught.value.request_policy["request_outcome_unknown"] is False


@pytest.mark.parametrize(
    ("error_type", "code", "unknown"),
    [
        (httpx.ReadTimeout, "time_limit", True),
        (httpx.ConnectTimeout, "time_limit", False),
        (httpx.ConnectError, "provider_unavailable", False),
    ],
)
def test_transport_failure_propagates_unknown_state_without_retry(
    endpoint, image, monkeypatch, error_type, code, unknown
):
    calls = []

    def fail_post(url, **kwargs):
        calls.append(url)
        raise error_type("PRIVATE_TRANSPORT_DETAIL")

    monkeypatch.setattr(httpx, "post", fail_post)
    with pytest.raises(ModelServiceError) as caught:
        OpenAIVlmObserver(endpoint, ServedModel(endpoint), timeout_s=600).observe(image)
    assert caught.value.error_code == code
    assert caught.value.request_policy["request_outcome_unknown"] is unknown
    assert caught.value.request_policy["timeout_s"] == 600
    assert caught.value.request_policy["output_budget_policy"] == "native_remaining_context"
    assert "PRIVATE" not in str(caught.value)
    assert len(calls) == 1


def test_visual_request_leaves_output_budget_to_provider_native_remaining_context(endpoint, image):
    _Endpoint.reply = {
        "choices": [{"message": {"content": '{"caption": "海边"}'}, "finish_reason": "stop"}],
        "usage": {"completion_tokens": 4096},
    }
    result = OpenAIVlmObserver(endpoint, ServedModel(endpoint)).observe(image)
    assert result is not None
    assert len(_Endpoint.received) == 1
    assert "max_tokens" not in _Endpoint.received[0]
    assert "max_completion_tokens" not in _Endpoint.received[0]


def test_visual_request_shares_the_selected_qwen_non_thinking_profile(endpoint, image):
    _Endpoint.reply = {"choices": [{"message": {"content": '{"caption": "室内"}'}, "finish_reason": "stop"}]}
    model = ServedModel(endpoint, generation_profile="qwen35_nonthinking", native_output_budget=True)
    result = OpenAIVlmObserver(endpoint, model).observe(image)
    assert result is not None
    body = _Endpoint.received[0]
    assert body["temperature"] == 0.7
    assert body["top_p"] == 0.8
    assert body["top_k"] == 20
    assert body["min_p"] == 0.0
    assert body["presence_penalty"] == 1.5
    assert body["seed"] == 42
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["response_format"] == {"type": "json_object"}
    assert "max_tokens" not in body
    assert "max_completion_tokens" not in body


def test_json_observation_request_fixes_sampling_and_constrains_format(endpoint, image):
    _Endpoint.reply = {"choices": [{"message": {"content": '{"caption": "室内"}'}, "finish_reason": "stop"}]}
    semantics = OpenAIVlmObserver(endpoint, ServedModel(endpoint)).observe(image)
    assert semantics is not None
    assert len(_Endpoint.received) == 1
    assert _Endpoint.received[0]["temperature"] == 0
    assert _Endpoint.received[0]["response_format"] == {"type": "json_object"}


def test_invalid_json_is_reported_without_repair_retry_or_private_content(endpoint, image, caplog):
    # Minimal reproduction of the observed mixed-quote error; no real image content.
    content = """{"caption":"PRIVATE_RAW_CONTENT","facts":[{"object":"text', 'confidence":0.8}]}"""
    _Endpoint.reply = {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"completion_tokens": 50},
    }
    with pytest.raises(ModelServiceError) as caught:
        OpenAIVlmObserver(endpoint, ServedModel(endpoint)).observe(image)
    assert len(_Endpoint.received) == 1
    assert caught.value.error_code == "model_output_invalid"
    assert caught.value.finish_reason == "stop"
    assert caught.value.completion_tokens == 50
    assert caught.value.request_policy["request_outcome_unknown"] is False
    assert "PRIVATE_RAW_CONTENT" not in str(caught.value) + caplog.text


def test_truncated_json_is_a_typed_limit_even_when_prefix_object_parses(endpoint, image):
    _Endpoint.reply = {
        "choices": [{"message": {"content": '{"caption":"PRIVATE_IMAGE_TEXT"}'}, "finish_reason": "length"}],
        "usage": {"prompt_tokens": 200, "completion_tokens": 2048},
    }
    with pytest.raises(ModelServiceError) as caught:
        OpenAIVlmObserver(endpoint, ServedModel(endpoint)).observe(image)
    assert len(_Endpoint.received) == 1
    assert caught.value.error_code == "output_limit"
    assert caught.value.finish_reason == "length"
    assert caught.value.prompt_tokens == 200
    assert caught.value.completion_tokens == 2048
    assert "PRIVATE" not in str(caught.value)


def test_unsupported_json_mode_propagates_once_without_private_provider_body(endpoint, image):
    _Endpoint.reply_status = 400
    _Endpoint.reply = {"error": {"message": "PRIVATE_PROMPT response_format unsupported"}}
    with pytest.raises(ModelServiceError) as caught:
        OpenAIVlmObserver(endpoint, ServedModel(endpoint)).observe(image)
    assert caught.value.error_code == "provider_rejected"
    assert caught.value.http_status == 400
    assert caught.value.request_policy["response_format"] == {"type": "json_object"}
    assert len(_Endpoint.received) == 1
    assert _Endpoint.received[0]["response_format"] == {"type": "json_object"}
    assert "PRIVATE" not in str(caught.value)


def test_shared_inspector_and_person_vision_propagate_operational_errors(endpoint, image):
    from sentrix.infrastructure.media.person_vision_model import OpenAIPersonVision
    from sentrix.infrastructure.media.photo_inspector import OpenAIPhotoInspector

    with pytest.raises(ModelServiceError, match="provider_rejected"):
        OpenAIPhotoInspector(endpoint, ServedModel(endpoint)).inspect(image, "衣着如何？")
    with pytest.raises(ModelServiceError, match="provider_rejected"):
        OpenAIPersonVision(endpoint, ServedModel(endpoint)).analyze_person_appearance(str(image))
    assert len(_Endpoint.received) == 2


def test_shared_json_protocol_preserves_inspection_image_order_and_person_appearance(
    endpoint, image, tmp_path
):
    from sentrix.infrastructure.media.person_vision_model import OpenAIPersonVision
    from sentrix.infrastructure.media.photo_inspector import OpenAIPhotoInspector

    crop = tmp_path / "crop.jpg"
    crop.write_bytes(b"synthetic-crop")
    _Endpoint.reply = {
        "choices": [
            {
                "message": {"content": '{"observation":"戴帽子","certainty":"supported"}'},
                "finish_reason": "stop",
            }
        ]
    }
    result = OpenAIPhotoInspector(endpoint, ServedModel(endpoint)).inspect(image, "衣着如何？", crop=crop)
    assert result == {"observation": "戴帽子", "certainty": "supported"}
    images = _Endpoint.received[0]["messages"][0]["content"][1:]
    assert [base64.b64decode(item["image_url"]["url"].split(",", 1)[1]) for item in images] == [
        crop.read_bytes(),
        image.read_bytes(),
    ]

    _Endpoint.reply = {
        "choices": [
            {"message": {"content": '{"clothing":["帽子"],"confidence":0.8}'}, "finish_reason": "stop"}
        ]
    }
    result = OpenAIPersonVision(endpoint, ServedModel(endpoint)).analyze_person_appearance(str(crop))
    assert result == {"clothing": ["帽子"], "confidence": 0.8}
    assert len(_Endpoint.received) == 2
    assert len(_Endpoint.received[1]["messages"][0]["content"]) == 2
    for body in _Endpoint.received:
        assert body["temperature"] == 0
        assert body["response_format"] == {"type": "json_object"}
        assert "max_tokens" not in body
        assert "max_completion_tokens" not in body


def test_error_warning_does_not_echo_untrusted_response_metadata(endpoint, image, caplog):
    _Endpoint.reply = {
        "choices": [{"message": {"content": "not JSON"}, "finish_reason": "PRIVATE_METADATA"}],
        "usage": {"completion_tokens": "PRIVATE_USAGE"},
    }
    with pytest.raises(ModelServiceError) as caught:
        OpenAIVlmObserver(endpoint, ServedModel(endpoint)).observe(image)
    assert caught.value.error_code == "provider_response_invalid"
    assert caught.value.request_policy["request_outcome_unknown"] is True
    assert "PRIVATE" not in str(caught.value) + caplog.text
