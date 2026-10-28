"""8100 上的视觉观察器（OpenAI 兼容 ``/v1``，multimodal）。

三段结构逐段对应 1.0 ``backend/model_clients.py:1130-1198`` 的 ``analyze_image``：

1. **首轮**：要 caption / activity / place / event_type 这些**自然语言**观察，
   加上 objects / clothing / ocr_text；
2. **补全**：首轮只回了分类字段、没有任何自然语言字段时，补问一次；
3. **中文化**：结果里出现拉丁文字时，把整包观察规范化成简体中文。

**上限 3 次模型调用，串行，同一段不重试**（1.0 的 ``analyze_image`` 也是三次
``self.chat``）。单卡 GPU 上整册导入是串行的，这个上限就是「一张图最多占用多少次
推理」的答案。

**明确不搬**（见 ``s4-deviations.md``）：

- 1.0 的 semantic 受控词表（地点 / 物品 / 氛围三个主类体系）——那是 S6 的精读；
- ``detail``（visible_details / regions / text_blocks / uncertainties）——同上，
  2.0 的 ``Observation`` 没有 ``detail`` 字段（S4 不建 ``_terms_from_detail``）。

**模型自报的 ``facts`` 在 S5b 搬进来了**（用户裁决④）。S4 曾把它判给 S5 并写下
「事实由 ``maintain_fact`` 规则决定，不由模型自报」——那句话把两件事混成了一件：
**候选由模型自报，写法由规则决定**。规则层（``domain/memories/facts.py``）只能决定
「这条候选怎么写」，它变不出候选；少了这一路，``MaintainFacts`` 的 ``candidates``
在生产装配下恒为空元组，门禁全开也写不出一条事实。

所有 JSON 调用复用 ServedModel 的显式采样策略；默认请求 response_format=json_object，
显式 prompt_only 模式仅省略该字段；json_schema 模式使用调用方的既有业务契约，
派发前核对契约，完整响应再次校验形状。不自动降级或重试。
不指定 max_tokens/max_completion_tokens，让后端按实际输入计算原生剩余窗口。
不支持该模式的端点不会被静默重试或降为非约束生成。结构化错误与诊断日志均不含原文。

无法读取本地图片时仍返回 ``None``；合法空 JSON 对象保留既有语义。
完整响应无法解析 JSON 对象时抛 ``model_output_invalid``，明确结果已知、格式不可用。
HTTP 拒绝、超时、连接故障、输出截断或响应协议不完整同样抛结构化 ``ModelServiceError``，
由流水线/工具层向用户报告；不能把运行失败伪装为已完成的空观察。

**模型名同样不写死**：观察与对话打的是同一个 8100，写死名字的后果与对话侧一样（换后端
就 404）。名字由 ``ServedModel`` 问端点要，见 ``assistant/model_endpoint.py``。
"""

from __future__ import annotations

import base64
import io
import json
import logging
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

from sentrix.domain.assistant.ports import ModelServiceError
from sentrix.domain.media.models import MODEL_FACT_CONFIDENCE, ObservationFact
from sentrix.domain.media.ports import ObservationSemantics
from sentrix.infrastructure.assistant.model_endpoint import ServedModel
from sentrix.infrastructure.media.image_io import ensure_heif_support
from sentrix.infrastructure.media.json_output_contracts import (
    OBSERVATION_SCHEMA,
    matches_output_contract,
    prepare_output_contract,
)
from sentrix.infrastructure.media.vlm_json_stream import stream_json_response

log = logging.getLogger(__name__)

# 1.0 ``model_clients.py:1130-1198``：首轮 + 补全 + 中文化，三段三调用。
MAX_MODEL_CALLS = 3

# 1.0 ``normalize_confidence(parsed.get("confidence"), 0.65)``——提示词不要求模型给
# confidence，所以它是一个常数而不是模型输出。
OBSERVATION_CONFIDENCE = 0.65

_LANGUAGE_FIELDS = ("caption", "activity", "place", "event_type")
_TEXT_FIELDS = ("caption", "activity", "place", "event_type", "ocr_text")
_LIST_FIELDS = ("objects", "clothing")
# ``facts`` 是**结构化**字段，不是字符串列表：补全那一步按 ``_COPY_FIELDS`` 逐个搬
# 标量/列表，搬不动它，所以它单列一份、由 ``semantics_from_payload`` 统一解析。
_COPY_FIELDS = (*_TEXT_FIELDS, *_LIST_FIELDS)

_LATIN_RE = re.compile(r"[A-Za-z]{2,}")
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

_OBSERVE_PROMPT = """你是家庭记忆观察器。仅根据图片抽取可验证的观察，不猜测姓名。
严格返回简体中文 JSON 对象，不要解释。不要输出 GPS 坐标、经纬度、文件名或路径。
字段固定为：caption、activity、place、event_type、objects、clothing、ocr_text、facts。
caption 是画面里看到什么（160 字内）；activity 是正在发生什么（40 字内）；
place 是语义地点，例如家中客厅、餐厅、公园（40 字内）；event_type 40 字内；
objects 是可见物品（最多 40 项）；clothing 是可见衣物（最多 12 项）；
ocr_text 是图中文字（1000 字内）；facts 最多 8 项，仅含 subject、predicate、object、confidence。
caption、activity、place、event_type 必须**同时**给出自然语言描述，画面能判断时不要留空；
确实看不清才用空字符串或空数组。"""

_RECOVERY_PROMPT = """首轮结果缺少自然语言观察，请补齐可验证的观察。只根据图片，不猜测姓名，不输出坐标。
严格返回简体中文 JSON：caption（160 字内）、activity（40 字内）、place（语义地点，40 字内）、
event_type（40 字内）、objects（最多 40 项）、clothing（最多 12 项）、ocr_text（1000 字内）、
facts（最多 8 项，仅含 subject、predicate、object、confidence）。
画面确实看不清才留空；不要只返回分类字段。"""

_CANONICAL_PROMPT = """把下面的家庭图片观察规范化为简体中文 JSON。只翻译和整理已有内容，
不新增人物、物体、活动或事实，不猜测姓名。保留字段 caption、activity、place、event_type、
objects、clothing、ocr_text、facts（每项仍只含 subject、predicate、object、confidence）。
\n原始观察："""


def _encode_image(path: Path) -> str | None:
    try:
        payload = path.read_bytes()
    except OSError as error:
        log.warning("观察用的图片读不了: %s (%s)", path, error)
        return None
    # Detect the actual image container, not its filename: MP4 bytes must never
    # become an image/jpeg data URL. Validation does not resize or re-encode.
    heif = payload[4:8] == b"ftyp" and payload[8:12] in (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1")
    if heif and not ensure_heif_support():
        raise ModelServiceError(
            "本地 HEIF 图片解码器不可用；未发送模型请求。",
            error_code="request_configuration_invalid",
            request_policy={
                "request_phase": "media_validation",
                "request_outcome_unknown": False,
                "input_error": "image_decoder_unavailable",
            },
        )
    try:
        with Image.open(io.BytesIO(payload)) as image:
            image_format = image.format or ""
            mime = Image.MIME.get(image_format) or {"HEIF": "image/heif"}.get(image_format)
            if not mime or not mime.startswith("image/"):
                raise ValueError("not an image container")
            image.verify()
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as error:
        raise ModelServiceError(
            "输入不是可读取的图片或图片文件已损坏；未发送模型请求。",
            error_code="request_configuration_invalid",
            request_policy={
                "request_phase": "media_validation",
                "request_outcome_unknown": False,
                "input_error": "invalid_image_data",
            },
        ) from error
    return f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"


def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _as_text_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, (list, tuple, set)):
        items = [str(item).strip() for item in value]
    else:
        items = [str(value).strip()] if value else []
    return tuple(item for item in items if item)


def parse_json_object(text: str) -> dict[str, Any] | None:
    """从模型回复里取出一个 JSON 对象；取不到返回 ``None``。

    只做两件事：先试整串，再试第一个 ``{...}`` 块。1.0 的
    ``parse_json_response`` 还要补花括号、补引号、删尾随逗号（``runtime.py:1586-1661``），
    那套修复器是为「让模型在正文里写 action JSON」服务的；这里少一层，
    解析不出来返回 None，由调用方报告结构化格式错误。
    """
    stripped = text.strip()
    for candidate in (stripped, *(_JSON_OBJECT_RE.findall(stripped))):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _confidence(value: object) -> float:
    """1.0 ``normalize_confidence``：拿不到数就用 ``MODEL_FACT_CONFIDENCE``，拿得到就 clamp。"""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return MODEL_FACT_CONFIDENCE
    return min(1.0, max(0.0, number))


def facts_from_payload(value: object) -> tuple[ObservationFact, ...]:
    """1.0 ``normalize_fact_confidences``（``model_clients.py:221-226``）。

    **只做两件事**：丢掉不是 dict 的元素、给缺 confidence 的补上默认值。字段缺失
    （subject / predicate / object 有空串）的项**照留**——「这条候选写不写得出来」
    是 ``is_writable_fact`` 的判据，解析器在这里多判一次，那条规则就有一半永远走不到。
    """
    items = value if isinstance(value, (list, tuple)) else ([value] if value else [])
    facts = [
        ObservationFact(
            subject=_text(item.get("subject")),
            predicate=_text(item.get("predicate")),
            object=_text(item.get("object")),
            confidence=_confidence(item.get("confidence")),
        )
        for item in items
        if isinstance(item, dict)
    ]
    # 1.0 的提示词写的是「facts 最多8项」，那条上限在提示词里；解析侧不再截断——
    # 模型多回了一条不比少回一条更值得丢。
    return tuple(facts)


def semantics_from_payload(payload: dict[str, Any]) -> ObservationSemantics:
    """把模型返回的对象收敛成 ``ObservationSemantics``。纯函数，便于单独测。"""
    return ObservationSemantics(
        caption=_text(payload.get("caption")),
        place=_text(payload.get("place")),
        activity=_text(payload.get("activity")),
        event_type=_text(payload.get("event_type")),
        ocr_text=_text(payload.get("ocr_text")),
        objects=_as_text_tuple(payload.get("objects")),
        clothing=_as_text_tuple(payload.get("clothing")),
        facts=facts_from_payload(payload.get("facts")),
        confidence=OBSERVATION_CONFIDENCE,
    )


def chat_json(
    endpoint: str,
    model: ServedModel,
    prompt: str,
    image: str | Sequence[str] | None,
    timeout_s: float,
    *,
    output_schema: Mapping[str, Any] | None = None,
    schema_name: str | None = None,
) -> dict[str, Any] | None:
    """一次「提示词（+ 一张或多张图）→ JSON 对象」的往返。格式/运行失败抛结构化类型错误。

    ``OpenAIVlmObserver`` / ``person_vision_model.OpenAIPersonVision`` /
    ``photo_inspector.OpenAIPhotoInspector`` 共用这一段：请求体的形状（``content`` 是块
    数组、图走 ``image_url`` 的 data URL）与失败口径只在这一处，改一次三边都动——三条
    链路都区分内容质量降级与运行限制；操作失败必须由各自调用方报告。

    **为什么会收一串图。** 1.0 的 ``inspect_photo`` 在指名了目标人物时发**两张**：
    裁剪件在前、原图在后（``tools.py:2192``）。多图在这里只是多几个 ``image_url`` 块，
    **顺序即语义**（提示词说「第一张图（如有）是该人物的定位裁剪图」），所以照传进来的
    次序发，不排序、不去重。单张仍然照旧传字符串——``OpenAIVlmObserver`` 与
    ``OpenAIPersonVision`` 的调用点一个字都不用改。
    """
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for url in [image] if isinstance(image, str) else list(image or ()):
        content.append({"type": "image_url", "image_url": {"url": url}})
    name = "unresolved"
    dispatched = False
    # Validate the explicit business contract before model discovery or generation.
    format_parameters, format_policy, contract = prepare_output_contract(
        model.json_output_mode, output_schema, schema_name
    )
    policy: dict[str, Any] = {
        **model.generation_parameters(),
        **format_policy,
        "output_budget_policy": "native_remaining_context",
        "timeout_s": timeout_s,
    }
    try:
        name = model.current()
        body = {
            "model": name,
            "messages": [{"role": "user", "content": content}],
            **model.generation_parameters(),
            # Omit output caps: the provider computes native remaining context
            # after tokenizing the full prompt, including images and template.
            # The explicit format mode is independent of sampling and native budgets.
            # Unsupported endpoints fail visibly once; never retry unconstrained.
            **format_parameters,
        }
        dispatched = True
        status_code = 200
        if model.json_streaming:
            stage = (
                "initial_observation"
                if prompt == _OBSERVE_PROMPT
                else "recovery_observation"
                if prompt == _RECOVERY_PROMPT
                else "chinese_canonicalization"
                if prompt.startswith(_CANONICAL_PROMPT)
                else "json_call"
            )
            payload = stream_json_response(
                endpoint,
                body,
                timeout_s,
                evidence_dir=model.json_evidence_dir,
                private_content=model.json_private_content,
                stage=stage,
                request_policy=policy,
            )
        else:
            response = httpx.post(endpoint, json=body, timeout=timeout_s)
            response.raise_for_status()
            status_code = response.status_code
            payload = response.json()
    except httpx.HTTPStatusError as error:
        raise ModelServiceError(
            f"视觉模型请求被服务拒绝（provider_rejected，HTTP {error.response.status_code}）。",
            error_code="provider_rejected",
            http_status=error.response.status_code,
            request_policy=policy,
        ) from error
    except httpx.HTTPError as error:
        code = "time_limit" if isinstance(error, httpx.TimeoutException) else "provider_unavailable"
        raise ModelServiceError(
            f"视觉模型请求未能完成（{code}，{type(error).__name__}）；未自动重试。",
            error_code=code,
            request_policy={
                **policy,
                "request_outcome_unknown": not isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout)),
            },
        ) from error
    except ModelServiceError as error:
        # Keep transport evidence intact; discovery failures precede any POST.
        if error.error_code is None:
            error.error_code = (
                "time_limit"
                if isinstance(error.__cause__, httpx.TimeoutException)
                else "provider_unavailable"
            )
        error.request_policy = {
            **policy,
            **error.request_policy,
            "request_outcome_unknown": error.request_policy.get("request_outcome_unknown", dispatched),
        }
        raise
    except ValueError as error:
        raise ModelServiceError(
            "视觉模型响应协议结构不可识别（provider_response_invalid）；未自动重试。",
            error_code="provider_response_invalid",
            http_status=status_code,
            request_policy={**policy, "request_outcome_unknown": True},
        ) from error
    text = _first_content(payload)
    choices = payload.get("choices") if isinstance(payload, dict) else None
    first = choices[0] if isinstance(choices, list) and choices else None
    raw_finish_reason = first.get("finish_reason") if isinstance(first, dict) else None
    finish_reason = (
        raw_finish_reason
        if raw_finish_reason in (None, "stop", "length", "tool_calls", "content_filter", "function_call")
        else "unknown"
    )
    usage = payload.get("usage") if isinstance(payload, dict) else None
    raw_tokens = usage.get("completion_tokens") if isinstance(usage, dict) else None
    completion_tokens = raw_tokens if type(raw_tokens) is int and raw_tokens >= 0 else None
    parsed = parse_json_object(text)
    if finish_reason == "content_filter":
        raise ModelServiceError(
            "视觉模型服务拒绝返回内容（provider_rejected，content_filter）；未自动重试。",
            error_code="provider_rejected",
            http_status=status_code,
            finish_reason="content_filter",
            request_policy={**policy, "request_outcome_unknown": False},
        )
    if finish_reason in {"tool_calls", "function_call"}:
        raise ModelServiceError(
            "视觉JSON请求收到不支持的工具响应（provider_response_invalid）；未自动重试。",
            error_code="provider_response_invalid",
            http_status=status_code,
            finish_reason=finish_reason,
            request_policy={**policy, "request_outcome_unknown": True},
        )
    if finish_reason == "length":
        raw_prompt_tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
        raise ModelServiceError(
            "视觉模型达到本次可用输出/上下文上限（output_limit），回复未完成。",
            error_code="output_limit",
            http_status=status_code,
            request_policy=policy,
            finish_reason="length",
            prompt_tokens=raw_prompt_tokens
            if type(raw_prompt_tokens) is int and raw_prompt_tokens >= 0
            else None,
            completion_tokens=completion_tokens,
        )
    if finish_reason != "stop":
        raise ModelServiceError(
            "视觉模型响应缺少完整结束证据（provider_response_invalid）；未自动重试。",
            error_code="provider_response_invalid",
            http_status=status_code,
            request_policy={**policy, "request_outcome_unknown": True},
        )
    message = first.get("message") if isinstance(first, dict) else None
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise ModelServiceError(
            "视觉模型响应协议结构不可识别（provider_response_invalid）；未自动重试。",
            error_code="provider_response_invalid",
            http_status=status_code,
            request_policy={**policy, "request_outcome_unknown": True},
        )
    if message.get("tool_calls") or message.get("function_call"):
        raise ModelServiceError(
            "视觉JSON请求收到未请求的工具调用（provider_response_invalid）；未自动重试。",
            error_code="provider_response_invalid",
            http_status=status_code,
            request_policy={**policy, "request_outcome_unknown": True},
        )
    if parsed is None or (contract is not None and not matches_output_contract(parsed, contract)):
        # A known completed generation with unusable structure is not a lost request.
        raw_prompt_tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
        raise ModelServiceError(
            "模型输出格式错误（model_output_invalid）：完整响应不是可用的业务 JSON 对象；未自动重试。",
            error_code="model_output_invalid",
            http_status=status_code,
            request_policy={**policy, "request_outcome_unknown": False},
            finish_reason="stop",
            prompt_tokens=raw_prompt_tokens
            if type(raw_prompt_tokens) is int and raw_prompt_tokens >= 0
            else None,
            completion_tokens=completion_tokens,
        )
    return parsed


class OpenAIVlmObserver:
    """实现 ``VlmObserver`` 端口。模型进程的生命周期不归 2.0 管。"""

    def __init__(self, base_url: str, model: ServedModel, *, timeout_s: float = 180.0) -> None:
        self._endpoint = f"{base_url.rstrip('/')}/chat/completions"
        self._model = model
        self._timeout = timeout_s

    @property
    def model_id(self) -> str:
        """这个观察器实际会请求的模型名。探不到就如实抛 ``ModelServiceError``——不编一个。"""
        return self._model.current()

    def observe(self, path: Path) -> ObservationSemantics | None:
        image = _encode_image(Path(path))
        if image is None:
            return None

        parsed = self._chat(_OBSERVE_PROMPT, image)
        calls = 1
        if parsed is None:
            return None

        if not any(_text(parsed.get(field)) for field in _LANGUAGE_FIELDS) and not parsed.get("objects"):
            recovered = self._chat(_RECOVERY_PROMPT, image)
            calls += 1
            if recovered is not None:
                for key in _COPY_FIELDS:
                    if recovered.get(key) not in (None, "", []):
                        parsed[key] = recovered[key]
                # facts 是结构化字段，进不了上面那张标量/列表的表；首轮一个字都没说
                # 画面里有什么的时候，补全这一轮的事实候选同样是唯一来源。
                if recovered.get("facts"):
                    parsed["facts"] = recovered["facts"]

        scalar = " ".join(_text(parsed.get(field)) for field in _TEXT_FIELDS)
        if calls < MAX_MODEL_CALLS and _LATIN_RE.search(scalar):
            canonical = self._chat(_CANONICAL_PROMPT + json.dumps(parsed, ensure_ascii=False), None)
            if canonical is not None:
                parsed = {
                    **parsed,
                    **{key: value for key, value in canonical.items() if value not in (None, "", [])},
                }

        return semantics_from_payload(parsed)

    def _chat(self, prompt: str, image: str | None) -> dict[str, Any] | None:
        return chat_json(
            self._endpoint,
            self._model,
            prompt,
            image,
            self._timeout,
            output_schema=OBSERVATION_SCHEMA,
            schema_name="sentrix_observation_v1",
        )


def _first_content(payload: object) -> str:
    """取 ``choices[0].message.content``。取不到返回空串——由 ``parse_json_object`` 判负。"""
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if not isinstance(message, dict):
        return ""
    return _text(message.get("content"))
