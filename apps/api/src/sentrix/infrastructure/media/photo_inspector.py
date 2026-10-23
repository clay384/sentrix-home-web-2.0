"""``PhotoInspector`` 的实现：把一个问题问给 8100 上的视觉端点。

**HTTP 件一份都不重写。** 请求组装走 ``vlm_observer.chat_json``，图片编码走它的
``_encode_image``——与 ``OpenAIVlmObserver`` / ``OpenAIPersonVision`` 同一份实现
（``person_vision_model`` 的模块 docstring 立的就是这条规矩）。本模块只做三件事：
定发哪几张图、填提示词的槽、把端点与超时接上。

**多图是这里第一个真需求**，也是 ``chat_json`` 的 ``image`` 形参从 ``str`` 放宽成
``str | Sequence[str]`` 的原因：1.0 的 ``inspect_photo`` 指名了目标人物时发两张
（裁剪件在前、原图在后，``tools.py:2192``）。**没有**在本模块另写一份请求组装——
那一份要重复的正是「图片编码、超时、模型不可用的降级」这三处，而它们正是这个端口
存在的理由。

**两处降级，口径都照 1.0，且都不抛：**

1. **原图读不了 → 整次返回 ``None``。** 1.0 在 ``gamma.encode_vision_image(row["path"])``
   抛错时落进外层 ``except``，回一句「图片复核失败」并带上 ``certainty="uncertain"``
   （``tools.py:2216-2221``）。2.0 的 ``_encode_image`` 自己吞掉 ``OSError`` 并返回
   ``None``，所以这一层只需把 ``None`` 传下去。
2. **裁剪件读不了 → 退回只发原图，不算失败。** 1.0 的裁剪块自带
   ``except (OSError, ValueError, TypeError)``，落到 ``target_status = "not_located"``
   之后**照常**把原图发出去（``tools.py:2186-2200``）。注意此时提示词里那句
   ``target_instruction`` **仍然填着**——``TARGET_INSTRUCTION`` 自己写的是「第一张图
   （如有）」，「如有」就是为这一路准备的。

**模型名不写死**：由 ``ServedModel`` 问端点要，与 ``vlm_observer`` / ``person_vision_model``
同一条口径（换后端写死的名字就 404）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from sentrix.domain.media.inspection import build_inspect_prompt
from sentrix.infrastructure.assistant.chat_client import MAX_ANSWER_TOKENS
from sentrix.infrastructure.assistant.model_endpoint import ServedModel
from sentrix.infrastructure.media.vlm_observer import _encode_image, chat_json


class OpenAIPhotoInspector:
    """实现 ``PhotoInspector`` 端口。模型进程的生命周期不归 2.0 管。"""

    def __init__(
        self,
        base_url: str,
        model: ServedModel,
        *,
        timeout_s: float = 180.0,
        output_tokens: int = MAX_ANSWER_TOKENS,
        enable_thinking: bool | None = None,
    ) -> None:
        self._endpoint = f"{base_url.rstrip('/')}/chat/completions"
        self._model = model
        self._timeout = timeout_s
        self._output_tokens = output_tokens
        self._enable_thinking = enable_thinking

    @property
    def model_id(self) -> str:
        """这次会请求的模型名。探不到就如实抛 ``ModelServiceError``——不编一个。"""
        return self._model.current()

    def inspect(
        self,
        path: str | Path,
        question: str,
        *,
        faces: Sequence[Mapping[str, object]] = (),
        target_person: str = "",
        crop: str | Path | None = None,
    ) -> dict[str, object] | None:
        image = _encode_image(Path(path))
        if image is None:
            return None

        images = [image]
        if crop is not None:
            located = _encode_image(Path(crop))
            if located is not None:
                images.insert(0, located)

        prompt = build_inspect_prompt(question, target_person=target_person, faces=faces)
        return self._chat(prompt, images)

    def _chat(self, prompt: str, images: Sequence[str]) -> dict[str, Any] | None:
        """一次往返。**单独一个方法是为了能被换成脚本**——与 ``OpenAIVlmObserver._chat``
        和 ``OpenAIPersonVision._chat`` 同一个手法，用例据此断言发出去的提示词与图的次序，
        而不必起 HTTP 服务。
        """
        return chat_json(
            self._endpoint,
            self._model,
            prompt,
            images,
            self._timeout,
            output_tokens=self._output_tokens,
            enable_thinking=self._enable_thinking,
        )
