"""人物视觉的三个模型入口：画像 / 瞬间 / 外观。

逐行移植 1.0 ``backend/model_clients.py`` 的三个方法：

- ``write_person_portrait``（``:1291-1297``）——证据包 → 一段有界画像；
- ``analyze_person_moments``（``:1316-1330``）——带编号预览图 → 人物瞬间；
- ``analyze_person_appearance``（``:1332-1351``）——头肩裁剪件 → 衣物。

**提示词只有一条住在本模块里。** 画像与瞬间的提示词连同各自的归一化分别住在
``domain/identity/portrait.py``（``PERSON_PORTRAIT_PROMPT`` /
``normalize_writer_output``）与 ``domain/identity/moments.py``
（``PERSON_MOMENT_PROMPT`` / ``normalize_person_moments``），本模块只负责拼上证据再打
出去。外观的提示词是 1.0 写在方法体里的内联串（``:1337-1340``），2.0 跟着方法一起搬成
模块常量 ``_APPEARANCE_PROMPT``，**逐字节相同**。

**HTTP 件一份都不重写。** 一次往返的组装（提示词 + 图 → JSON 对象）走
``vlm_observer.chat_json``，图片编码走它的 ``_encode_image``——那两件在 2.0 只有一份实现。

**三处相对 1.0 的差别，前两处继承 ``vlm_observer`` 已落地的决定，不是本模块新做的：**

1. **图像按 ``IMAGE_MAX_SIDE`` 缩放，与观察器/精读同一条。** 1.0 的
   ``analyze_person_moments`` 走 ``encode_vision_image``（``:1107-1125``：最长边 896、
   JPEG q90、``VISION_CORE_MAX_DIMENSION`` 可配）。2.0 早先直接发原图，158 上实测
   album3-14 的 ``person_insights`` 因此要 418s、moments 每次 ~28s：预览图是**整张原图**，
   一张 3264×2448 就是 4,034 个图片 token。现在与 ``OpenAIVlmObserver`` /
   ``OpenAIPhotoInspector`` 共用 1280 上限——比 1.0 的 896 大，是质量优先的折中。
   ``analyze_person_appearance`` 的入图是头肩裁剪件（本来就小），走同一条不影响结果。
2. **``_core_vision_options`` 不搬**（1.0 ``:1097-1105`` 的 ``think`` / ``num_ctx`` /
   ``num_predict`` 是 llama.cpp 与 ollama 的私有字段）。``chat_json`` 发的是
   原生剩余窗口策略，省略 ``max_tokens/max_completion_tokens``，与 ``VlmObserver`` 一致。
3. **返回里没有 ``"model"``。** 1.0 的 ``analyze_person_appearance`` 回 ``self.model``
   （``:1350``），2.0 把它提到端口的 ``model_id`` 上——与 ``VlmObserver.model_id``
   同一条口径（1.0 ``analyze_image`` 的 ``parsed["model"]`` 在 2.0 也是这么处理的）。

无法读取本地图片时保留 ``None``；合法空对象在默认模式仍走既有归一化。
完整响应无法解析 JSON 或不符合显式 ``json_schema`` 业务契约时抛结构化输出格式错误；
传输/供应商/预算故障同样由调用方报告，不能伪装成已完成的空画像或零条瞬间。
画像、瞬间、外观各自传入自己的输出契约，不使用观察器的八字段契约。

**``role`` 形参不搬。** 1.0 的 ``role="writer"`` / ``role="verify"`` 是按角色路由到不同
模型（``_endpoint_for``，``:697-719``），2.0 只有一台端点、一个 ``ServedModel``。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from sentrix.domain.identity.moments import PERSON_MOMENT_PROMPT, normalize_person_moments
from sentrix.domain.identity.portrait import PERSON_PORTRAIT_PROMPT, normalize_writer_output
from sentrix.infrastructure.assistant.model_endpoint import ServedModel
from sentrix.infrastructure.media.json_output_contracts import (
    APPEARANCE_SCHEMA,
    MOMENTS_SCHEMA,
    PORTRAIT_SCHEMA,
)
from sentrix.infrastructure.media.vlm_observer import IMAGE_MAX_SIDE, _encode_image, chat_json

# 1.0 ``model_clients.py:1350`` 的 ``normalize_confidence(..., 0.55)`` 里的那个默认值。
# 提示词只列了 clothing 与 confidence 两个字段，没有约束取值范围，所以拿不到数时是一个
# 常数而不是模型输出。
APPEARANCE_CONFIDENCE = 0.55

# 1.0 ``model_clients.py:1337-1340`` 的提示词原文，**逐字节相同**。
_APPEARANCE_PROMPT = (
    "你是家庭记忆人物外观观察器。输入图片是从一个已检测人脸向下扩展得到的同一目标人物裁剪。\n"
    "只描述这个目标人物能够明确看见的衣物、颜色、款式和配饰；不得描述背景或其他人物，不得猜测姓名、性别、关系或看不清的细节。\n"
    "严格只返回简体中文 JSON：clothing（数组）、confidence。无法可靠归属给目标人物时 clothing 返回空数组。\n"
    "目标人物裁剪元数据："
)


def _as_list(value: object) -> list[object]:
    """1.0 ``model_clients.py:129-134``：``None`` 空表、列表原样、其余包成单元素表。"""
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    return [value]


def _as_text(value: object) -> str:
    """1.0 ``model_clients.py:137-146``。

    列表用「、」连接而不是 ``str(list)``：模型偶尔把 clothing 回成嵌套数组，``"、".join``
    得到的是「上衣、围巾」，``str`` 得到的是 ``"['上衣', '围巾']"``——后者会原样落进外观证据。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "、".join(_as_text(item) for item in value)
    return json.dumps(value, ensure_ascii=False)


def _confidence(value: object) -> float:
    """拿不到数就用 ``APPEARANCE_CONFIDENCE``，拿得到就 clamp。

    1.0 的 ``normalize_confidence``（``:179-203``）还认「很高 / 中 / 低」这类中文标签与
    百分号；2.0 已经为 ``analyze_image`` 定过一条口径（``vlm_observer._confidence``，
    用例把「很高」钉在默认值上），这里跟着同一条：只认数字，其余走默认。
    """
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return APPEARANCE_CONFIDENCE
    return min(1.0, max(0.0, number))


class OpenAIPersonVision:
    """实现 ``PersonVision`` 端口。模型进程的生命周期不归 2.0 管。"""

    def __init__(self, base_url: str, model: ServedModel, *, timeout_s: float = 180.0) -> None:
        self._endpoint = f"{base_url.rstrip('/')}/chat/completions"
        self._model = model
        self._timeout = timeout_s

    @property
    def model_id(self) -> str:
        """这个入口实际会请求的模型名。探不到就如实抛 ``ModelServiceError``——不编一个。"""
        return self._model.current()

    def write_person_portrait(self, pack: Mapping[str, object]) -> dict[str, object] | None:
        """1.0 ``model_clients.py:1291-1297``。提示词与归一化都在 ``domain/identity/portrait``。"""
        prompt = PERSON_PORTRAIT_PROMPT + "\n证据包：" + json.dumps(dict(pack), ensure_ascii=False)
        parsed = self._chat(
            prompt, None, output_schema=PORTRAIT_SCHEMA, schema_name="sentrix_person_portrait_v1"
        )
        if parsed is None:
            return None
        return normalize_writer_output(parsed)

    def analyze_person_moments(
        self, path: str, labels: Sequence[str], context: Mapping[str, object] | None = None
    ) -> dict[str, object] | None:
        """1.0 ``model_clients.py:1316-1330``。

        ``labels`` 是这张预览图上真实画出的 P 编号。``normalize_person_moments`` 只认这个
        集合里的标签，模型认领一个不存在的编号时整条被丢掉——那一步在 domain 里，不在这里。
        """
        image = _encode_image(Path(path), max_side=IMAGE_MAX_SIDE)
        if image is None:
            return None
        prompt = PERSON_MOMENT_PROMPT
        if context:
            prompt += "\n图片上下文：" + json.dumps(dict(context), ensure_ascii=False)
        parsed = self._chat(
            prompt, image, output_schema=MOMENTS_SCHEMA, schema_name="sentrix_person_moments_v1"
        )
        if parsed is None:
            return None
        return {"moments": normalize_person_moments(parsed, list(labels))}

    def analyze_person_appearance(
        self, path: str, metadata: Mapping[str, object] | None = None
    ) -> dict[str, object] | None:
        """1.0 ``model_clients.py:1332-1351``。

        入图是 ``expanded_person_crop`` 的产物（头 + 上半身），提示词里的三条禁令
        （不描述背景与他人、不猜姓名性别关系）与那个裁剪框是一对：框放宽一点，背景里
        别人的衣服就会被算到这个头上。
        """
        image = _encode_image(Path(path), max_side=IMAGE_MAX_SIDE)
        if image is None:
            return None
        prompt = _APPEARANCE_PROMPT + json.dumps(dict(metadata or {}), ensure_ascii=False)
        parsed = self._chat(
            prompt, image, output_schema=APPEARANCE_SCHEMA, schema_name="sentrix_person_appearance_v1"
        )
        if parsed is None:
            return None
        clothing: list[str] = []
        for item in _as_list(parsed.get("clothing")):
            value = _as_text(item).strip()
            if value:
                clothing.append(value)
        return {
            "clothing": list(dict.fromkeys(clothing)),
            "confidence": _confidence(parsed.get("confidence")),
        }

    def _chat(
        self,
        prompt: str,
        image: str | None,
        *,
        output_schema: Mapping[str, Any],
        schema_name: str,
    ) -> dict[str, Any] | None:
        return chat_json(
            self._endpoint,
            self._model,
            prompt,
            image,
            self._timeout,
            output_schema=output_schema,
            schema_name=schema_name,
        )
