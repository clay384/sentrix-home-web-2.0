"""人物视觉的三个模型入口（T86e）——提示词原文、字段收敛、失败口径。

不打真端点：``_chat`` 换成脚本化的假回复（与 ``test_vlm_observer.py`` 同一个手法），
验的是三件在 1.0 里都能指出行号的事：

1. **提示词逐字节相同**——画像与瞬间那两条直接对着 domain 的常量比，外观那条
   （1.0 ``model_clients.py:1337-1340`` 的内联串）在下面重抄了一遍原文再比，
   抄错一个字这条就红；
2. **归一化复用 domain 的函数**，本模块不重写一遍词表（瞬间那条专门用一条越界回复
   把它钉住）；
3. **模型不可用 / 图读不了 / 回复不是 JSON 一律返回 ``None``**，一次都不抛。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sentrix.domain.identity.moments import PERSON_MOMENT_PROMPT
from sentrix.domain.identity.portrait import PERSON_PORTRAIT_PROMPT
from sentrix.infrastructure.assistant.model_endpoint import ServedModel
from sentrix.infrastructure.media.person_vision_model import (
    APPEARANCE_CONFIDENCE,
    OpenAIPersonVision,
)

# 1.0 ``backend/model_clients.py:1337-1340`` 的提示词原文。**逐字节抄的**，不是转述：
# 提示词是产品行为，改一个字都要重新验一遍真实模型的输出。
APPEARANCE_PROMPT_1_0 = (
    "你是家庭记忆人物外观观察器。输入图片是从一个已检测人脸向下扩展得到的同一目标人物裁剪。\n"
    "只描述这个目标人物能够明确看见的衣物、颜色、款式和配饰；不得描述背景或其他人物，不得猜测姓名、性别、关系或看不清的细节。\n"
    "严格只返回简体中文 JSON：clothing（数组）、confidence。无法可靠归属给目标人物时 clothing 返回空数组。\n"
    "目标人物裁剪元数据："
)

# 完整契约字段的假回复：一个瞬间有九项，画像主题有三项，外观有衣橱与置信度。
FULL_MOMENT = {
    "label": "P1",
    "action_text": "在餐桌旁递碗",
    "interaction_labels": ["P2", "P2", "P9"],
    "interaction_text": "把碗递给 P2",
    "participation_style": "照顾",
    "visible_affect": "微笑",
    "social_role_cues": ["照护者", "生病"],
    "narrative_note": "饭桌上的日常",
    "confidence": 0.7,
}
FULL_PORTRAIT = {
    "portrait_text": "从照片看，他似乎常在饭桌上照顾别人。",
    "themes": [
        {
            "title": "反复递碗",
            "summary": "从照片看，他常递碗。",
            "evidence_refs": [{"kind": "person_moment", "id": "m1"}],
        }
    ],
}
FULL_APPEARANCE = {"clothing": ["蓝色外套"], "confidence": 0.8}


class ScriptedVision(OpenAIPersonVision):
    """把 ``_chat`` 换成脚本：按调用序返回预置回复，并记录提示词与图片。"""

    def __init__(self, replies: list[dict | None]) -> None:
        # ``_chat`` 被整个替换掉了，探测永远不会发生。故意给一个连不上的地址：哪天有人
        # 把这个覆盖删了，测试会立刻炸，而不是悄悄去打真的 8100。
        super().__init__("http://127.0.0.1:1/v1", ServedModel("http://127.0.0.1:1/v1", timeout_s=1.0))
        self.replies = replies
        self.prompts: list[str] = []
        self.images: list[str | None] = []

    def _chat(self, prompt: str, image: str | None, **_contract: Any) -> dict | None:
        self.prompts.append(prompt)
        self.images.append(image)
        return self.replies[min(len(self.prompts) - 1, len(self.replies) - 1)]


@pytest.fixture
def image(tmp_path: Path) -> Path:
    path = tmp_path / "p.jpg"
    path.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    return path


def test_the_appearance_prompt_is_the_1_0_text_verbatim(image: Path) -> None:
    """1.0 ``model_clients.py:1337-1340``。提示词 + 元数据 JSON，**没有别的修饰**。"""
    vision = ScriptedVision([FULL_APPEARANCE])
    metadata = {"face_instance_id": "f1", "crop_bbox": [10, 20, 30, 40]}

    vision.analyze_person_appearance(str(image), metadata)

    assert vision.prompts == [
        APPEARANCE_PROMPT_1_0 + json.dumps(metadata, ensure_ascii=False),
    ]


def test_the_appearance_prompt_without_metadata_ends_with_an_empty_object(image: Path) -> None:
    """1.0 的 ``json.dumps(metadata or {})``：不给元数据发的是一个 ``{}``，不是空串。"""
    vision = ScriptedVision([FULL_APPEARANCE])

    vision.analyze_person_appearance(str(image))

    assert vision.prompts == [APPEARANCE_PROMPT_1_0 + "{}"]


def test_the_appearance_call_sends_the_image_as_a_data_url(image: Path) -> None:
    vision = ScriptedVision([FULL_APPEARANCE])

    vision.analyze_person_appearance(str(image))

    assert vision.images[0] is not None
    assert vision.images[0].startswith("data:image/jpeg;base64,")


def test_clothing_is_deduplicated_and_the_unusable_entries_dropped(image: Path) -> None:
    """1.0 ``model_clients.py:1342-1347`` 的 ``as_list`` / ``as_text`` / ``dict.fromkeys``。

    模型把 clothing 回成标量、空串、``None``、嵌套数组时都要按 1.0 的口径收：空串与
    ``None`` 丢掉，嵌套数组用「、」连接（不是 ``str(list)``），重复项只留第一次出现的。
    """
    vision = ScriptedVision(
        [
            {
                "clothing": ["蓝色外套", "", "   ", "蓝色外套", None, ["上衣", "围巾"], {"饰": "胸针"}],
                "confidence": 0.8,
            }
        ]
    )

    result = vision.analyze_person_appearance(str(image))

    assert result is not None
    assert result["clothing"] == ["蓝色外套", "上衣、围巾", '{"饰": "胸针"}']


def test_a_scalar_clothing_field_is_wrapped_not_dropped(image: Path) -> None:
    """1.0 ``as_list``（``:129-134``）：不是列表就包成单元素表，而不是当空表。"""
    vision = ScriptedVision([{"clothing": "外套", "confidence": 0.8}])

    result = vision.analyze_person_appearance(str(image))

    assert result is not None
    assert result["clothing"] == ["外套"]


@pytest.mark.parametrize(
    ("confidence", "expected"),
    [
        (None, APPEARANCE_CONFIDENCE),
        (0.8, 0.8),
        (7, 1.0),
        (-1, 0.0),
        ("很高", APPEARANCE_CONFIDENCE),
        ("0.5", 0.5),
    ],
)
def test_the_appearance_confidence_defaults_to_the_1_0_number(
    image: Path, confidence: object, expected: float
) -> None:
    """断言的是那个**具体的数 0.55**：它是 1.0 ``normalize_confidence(..., 0.55)``
    （``:1350``）的默认值，会直接落进 ``person_appearance_evidence`` 的置信度。"""
    vision = ScriptedVision([{"clothing": ["外套"], "confidence": confidence}])

    result = vision.analyze_person_appearance(str(image))

    assert result is not None
    assert result["confidence"] == pytest.approx(expected)


def test_the_moments_prompt_is_the_domain_constant(image: Path) -> None:
    """1.0 ``model_clients.py:1321``：``prompt = PERSON_MOMENT_PROMPT``，没有前缀。"""
    vision = ScriptedVision([{"moments": []}])

    vision.analyze_person_moments(str(image), ["P1", "P2"])

    assert vision.prompts == [PERSON_MOMENT_PROMPT]


def test_the_moments_context_is_appended_as_json(image: Path) -> None:
    """1.0 ``model_clients.py:1322-1323``：``if context:`` 才拼，拼的是「\\n图片上下文：」。"""
    vision = ScriptedVision([{"moments": []}, {"moments": []}])
    context = {"asset_id": "a1", "scope_id": "home-default", "captured_at": "2024-01-01 08:00:00"}

    vision.analyze_person_moments(str(image), ["P1"], context)
    vision.analyze_person_moments(str(image), ["P1"], {})

    expected = PERSON_MOMENT_PROMPT + "\n图片上下文：" + json.dumps(context, ensure_ascii=False)
    assert vision.prompts == [expected, PERSON_MOMENT_PROMPT]


def test_moments_go_through_the_domain_normalizer(image: Path) -> None:
    """本模块**不重写**瞬间的清洗规则：越界的回复必须被 ``normalize_person_moments``
    （``domain/identity/moments.py``）按同一套词表处理。

    这条假回复同时踩四条线：认领不存在的 ``P9``、动作留空、参与方式不在受控词表、
    ``social_role_cues`` 里混了敏感词。
    """
    vision = ScriptedVision(
        [
            {
                "moments": [
                    FULL_MOMENT,
                    {**FULL_MOMENT, "label": "P9"},
                    {**FULL_MOMENT, "action_text": ""},
                    {**FULL_MOMENT, "label": "P2", "participation_style": "随口一说"},
                ]
            }
        ]
    )

    result = vision.analyze_person_moments(str(image), ["P1", "P2"])

    assert result is not None
    assert [moment["label"] for moment in result["moments"]] == ["P1", "P2"]
    first = result["moments"][0]
    assert first["participation_style"] == "照顾"
    assert first["interaction_labels"] == ["P2", "P9"]
    assert first["social_role_cues"] == ["照护者"]
    assert first["confidence"] == pytest.approx(0.7)
    assert result["moments"][1]["participation_style"] == "无法判断"


def test_the_portrait_prompt_appends_the_evidence_pack() -> None:
    """1.0 ``model_clients.py:1295``：``PERSON_PORTRAIT_PROMPT + "\\n证据包：" + json``。"""
    vision = ScriptedVision([FULL_PORTRAIT])
    pack = {"person": {"id": "e1", "confirmed_name": "妈妈"}, "moments": [], "appearance_themes": []}

    result = vision.write_person_portrait(pack)

    assert vision.prompts == [PERSON_PORTRAIT_PROMPT + "\n证据包：" + json.dumps(pack, ensure_ascii=False)]
    assert result == {
        "portrait_text": FULL_PORTRAIT["portrait_text"],
        "themes": [
            {
                "title": "反复递碗",
                "summary": "从照片看，他常递碗。",
                "evidence_refs": [{"kind": "person_moment", "id": "m1"}],
            }
        ],
    }


def test_the_portrait_call_sends_no_image() -> None:
    """画像收到的是一个证据包，不是图：``image`` 必须是 ``None``，否则调用方把
    ``content`` 里塞了一个空 ``image_url``。"""
    vision = ScriptedVision([FULL_PORTRAIT])

    vision.write_person_portrait({"person": {}, "moments": [], "appearance_themes": []})

    assert vision.images == [None]


def test_a_model_failure_returns_none_for_all_three(image: Path) -> None:
    """端点挂了、回复不是 JSON——1.0 的 ``parse_json_response`` 回 ``{}``（画像收成空画像
    后必然被 ``validate_portrait`` 拒掉），2.0 统一成 ``None``。三条一次都不抛。"""
    vision = ScriptedVision([None])

    assert vision.write_person_portrait({"person": {}, "moments": [], "appearance_themes": []}) is None
    assert vision.analyze_person_moments(str(image), ["P1"]) is None
    assert vision.analyze_person_appearance(str(image)) is None


def test_an_unreadable_image_returns_none_without_calling_the_model(tmp_path: Path) -> None:
    """读不了图就不是一次模型调用：``prompts`` 必须还是空的（连探测都不该发生）。"""
    vision = ScriptedVision([FULL_APPEARANCE])

    assert vision.analyze_person_appearance(str(tmp_path / "missing.jpg")) is None
    assert vision.analyze_person_moments(str(tmp_path / "missing.jpg"), ["P1"]) is None
    assert vision.prompts == []
