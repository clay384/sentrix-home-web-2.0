"""「拿一个问题去问这张图」（T123/T124 共用的上游件）——提示词原文、图的次序、两处降级。

这个端口存在的理由只有一句话：**1.0 的 ``inspect_photo`` 与 ``read_photo_text`` 对模型的
那一问是同一件事**（§4 Task 123/124）。写两遍的话，最容易写错的两处——**图片编码、超时
与「模型不可用」的降级**——就会有两份实现，各自测各自的。所以判据集中在两件事上：
「1.0 的形状没走样」与「那两处只实现了一遍」。

不打真端点：``_chat`` 换成脚本（与 ``test_person_vision_model.py`` / ``test_vlm_observer.py``
同一个手法），另有一组打**本文件自己起的**本地 HTTP 服务——请求体的形状（``content`` 是
块数组、两张图的次序）只有真的发出去才验得到，而「裁剪件在前」这件事正是 1.0 提示词里
「第一张图（如有）是该人物的定位裁剪图」那句话的全部依据。

**这个文件里抄了三段 1.0 的原文**（提示词、目标人物那两句、人脸清单的前缀）。抄而不是
引用，是这条用例唯一的力气所在：引用模块自己的常量，改一个字它照样绿。
"""

from __future__ import annotations

import base64
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest
from PIL import Image

from sentrix.domain.assistant.ports import ModelServiceError
from sentrix.domain.media.inspection import (
    CERTAINTY_SUPPORTED,
    CERTAINTY_UNCERTAIN,
    FACE_MANIFEST_PREFIX,
    INSPECT_PROMPT,
    TARGET_INSTRUCTION,
    build_inspect_prompt,
    render_face_manifest,
)
from sentrix.infrastructure.assistant.model_endpoint import ServedModel
from sentrix.infrastructure.media.photo_inspector import OpenAIPhotoInspector
from sentrix.infrastructure.media.vlm_observer import _OBSERVE_PROMPT, IMAGE_MAX_SIDE

# ---- 1.0 原文（逐字节抄的，不是转述）------------------------------------------------
#
# 提示词是产品行为：改一个字都要重新验一遍真实模型的输出，所以它对用例的意义与常量
# 本身一样大。以下三段来自 ``backend/agent_runtime/tools.py``。

# ``:2616``
INSPECT_PROMPT_1_0 = """观察这张照片，输出 JSON：
{{"observation": "一句话描述", "certainty": "supported|uncertain", "target_face_id": "face_N或空"}}
{target_instruction}
问题：{question}
如果问题指向照片中的某个人但没有姓名，请根据人脸清单选择对应的 face_id；无法确定时返回空，不要猜姓名。"""

# ``:2197-2198``（f-string 的 ``{target_person}`` 已还原成槽）
TARGET_INSTRUCTION_1_0 = (
    "目标人物是“{target_person}”。第一张图（如有）是该人物的定位裁剪图；"
    "只回答目标人物，不要把同图其他人的外观或动作归给目标人物。"
)

# ``:2206``
FACE_MANIFEST_PREFIX_1_0 = "\n照片中的人脸清单（face_id 与位置一一对应；未确认者不得猜姓名）："

QUESTION = "照片里有人戴帽子吗？"
# 一张已确认、一张未确认——1.0 ``_photo_face_manifest`` 只给双确认的那张填名字。
FACES: list[dict[str, object]] = [
    {
        "face_id": "face_1",
        "face_instance_id": "fi_1",
        "asset_id": "a1",
        "bbox": [10, 20, 30, 40],
        "identity_status": "confirmed",
        "person_name": "妈妈",
        "family_role": "母亲",
        "detection_confidence": 0.97,
    },
    {
        "face_id": "face_2",
        "face_instance_id": "fi_2",
        "asset_id": "a1",
        "bbox": [50, 60, 70, 80],
        "identity_status": "unconfirmed",
        "person_name": "",
        "family_role": "",
        "detection_confidence": 0.88,
    },
]


def _b64(payload: bytes) -> str:
    """这张图的字节在 data URL 里长什么样——用来断言发出去的到底是哪一张。"""
    return base64.b64encode(payload).decode("ascii")


@pytest.fixture
def photo(tmp_path: Path) -> Path:
    path = tmp_path / "photo.jpg"
    Image.new("RGB", (12, 8), "red").save(path, format="JPEG")
    return path


@pytest.fixture
def crop(tmp_path: Path) -> Path:
    path = tmp_path / "crop.jpg"
    Image.new("RGB", (6, 4), "blue").save(path, format="JPEG")
    return path


class ScriptedInspector(OpenAIPhotoInspector):
    """把 ``_chat`` 换成脚本：记录提示词与图的次序，回一个预置 JSON。"""

    def __init__(self, reply: dict[str, Any] | None) -> None:
        # ``_chat`` 被整个替换掉了，探测永远不会发生。故意给一个连不上的地址：哪天有人
        # 把这个覆盖删了，用例会立刻炸，而不是悄悄去打真的 8100。
        super().__init__("http://127.0.0.1:1/v1", ServedModel("http://127.0.0.1:1/v1", timeout_s=1.0))
        self.reply = reply
        self.prompts: list[str] = []
        self.images: list[list[str]] = []

    def _chat(self, prompt: str, images: Any) -> dict[str, Any] | None:
        self.prompts.append(prompt)
        self.images.append(list(images))
        return self.reply


# ---- 提示词原文：三段各对一次 --------------------------------------------------------


def test_the_inspect_prompt_is_the_1_0_text_verbatim() -> None:
    """1.0 ``tools.py:2616``。注意双花括号——1.0 是拿它做 ``str.format`` 模板的。"""
    assert INSPECT_PROMPT == INSPECT_PROMPT_1_0


def test_the_target_instruction_is_the_1_0_text_verbatim() -> None:
    """1.0 ``tools.py:2197-2198``。"""
    assert TARGET_INSTRUCTION == TARGET_INSTRUCTION_1_0


def test_the_face_manifest_prefix_is_the_1_0_text_verbatim() -> None:
    """1.0 ``tools.py:2206``。前缀里那句「未确认者不得猜姓名」是这条链路的纪律本身。"""
    assert FACE_MANIFEST_PREFIX == FACE_MANIFEST_PREFIX_1_0


def test_the_certainty_vocabulary_has_exactly_the_two_levels_the_prompt_names() -> None:
    """提示词写死 ``supported|uncertain``，**没有第三档**。

    模型答不上来时报 ``uncertain``，不是 ``contradicted``——与 ``conditions.py`` 里
    UNKNOWN 与 CONTRADICTED 分开是同一个道理。这条钉住的是「不要顺手加一档」。
    """
    assert (CERTAINTY_SUPPORTED, CERTAINTY_UNCERTAIN) == ("supported", "uncertain")


# ---- 槽位：整串与 1.0 逐字节相等 ------------------------------------------------------


def test_the_whole_prompt_with_a_question_is_the_1_0_string() -> None:
    """没有目标人物、没有人脸时，整串就是 1.0 的 ``.format`` 结果——多一个空格都算走样。"""
    expected = INSPECT_PROMPT_1_0.format(question=QUESTION, target_instruction="")
    assert build_inspect_prompt(QUESTION) == expected


def test_a_target_person_fills_the_instruction_line() -> None:
    """1.0 ``tools.py:2196-2198`` 的 ``if target_person else ""``。"""
    expected = INSPECT_PROMPT_1_0.format(
        question=QUESTION,
        target_instruction=TARGET_INSTRUCTION_1_0.format(target_person="爸爸"),
    )
    assert build_inspect_prompt(QUESTION, target_person="爸爸") == expected


def test_the_face_manifest_is_appended_after_the_prompt_not_inside_it() -> None:
    """1.0 ``:2209-2213``：先 ``.format()``、再 ``+ face_context``。

    次序是有后果的：清单里那张表带着 bbox 与置信度，放进槽里会被 ``str.format``
    的花括号规则咬到（``json.dumps`` 的产物全是花括号）。
    """
    expected = (
        INSPECT_PROMPT_1_0.format(question=QUESTION, target_instruction="")
        + FACE_MANIFEST_PREFIX_1_0
        + json.dumps(FACES, ensure_ascii=False)
    )
    assert build_inspect_prompt(QUESTION, faces=FACES) == expected


def test_no_faces_appends_nothing_at_all() -> None:
    """1.0 ``:2204`` 的 ``if face_manifest:``——空表不是「一个空清单」，是**没有这一段**。"""
    assert build_inspect_prompt(QUESTION, faces=[]) == INSPECT_PROMPT_1_0.format(
        question=QUESTION, target_instruction=""
    )
    assert render_face_manifest([]) == ""


def test_the_manifest_rows_pass_through_without_a_single_added_key() -> None:
    """**未确认的人不许有名字，这条纪律不许在这一层被「帮忙」补上。**

    1.0 只在 cluster 与 entity 双确认时才填 ``person_name``（``:2343-2355``）；这里的
    ``render_face_manifest`` 原样 dump 给它的每一行。替它补一个名字，等于把那条纪律从
    「查过两次状态」降级成「猜的」——而模块 docstring 说它恰恰是为这件事存在的。

    两个方向各验一次：**给的键一个不少、没给的键一个不多**。少一个键模型就看不到那个
    人的状态，多一个键就是这里替它编的。
    """
    rows = json.loads(render_face_manifest(FACES).removeprefix(FACE_MANIFEST_PREFIX_1_0))

    assert rows == FACES
    given = {key for row in FACES for key in row}
    assert {key for row in rows for key in row} == given
    assert FACES[1]["person_name"] == "", "这一条的前提：未确认那张本来就没有名字"


# ---- 图的次序与降级（走 :class:`ScriptedInspector`）----------------------------------


def test_the_question_reaches_the_model_in_the_prompt(photo: Path) -> None:
    inspector = ScriptedInspector({"observation": "有一只猫"})

    inspector.inspect(str(photo), QUESTION)

    assert inspector.prompts == [build_inspect_prompt(QUESTION)]
    assert QUESTION in inspector.prompts[0]


def test_the_crop_is_sent_before_the_photo(photo: Path, crop: Path) -> None:
    """**这条是提示词那句话的全部依据。** 1.0 ``tools.py:2192`` 的
    ``inspect_images.insert(0, crop)``——裁剪件必须是第一张，提示词里才说得出
    「第一张图（如有）是该人物的定位裁剪图」。反过来发，模型会拿原图当裁剪件。
    """
    inspector = ScriptedInspector({"observation": "戴了"})

    inspector.inspect(str(photo), QUESTION, target_person="爸爸", crop=str(crop))

    assert len(inspector.images[0]) == 2
    assert inspector.images[0][0].endswith(_b64(crop.read_bytes()))
    assert inspector.images[0][1].endswith(_b64(photo.read_bytes()))


def test_without_a_crop_only_the_photo_is_sent(photo: Path) -> None:
    inspector = ScriptedInspector({"observation": "没有"})

    inspector.inspect(str(photo), QUESTION)

    assert len(inspector.images[0]) == 1
    assert inspector.images[0][0].endswith(_b64(photo.read_bytes()))


def test_an_unreadable_crop_falls_back_to_the_photo_alone(photo: Path, tmp_path: Path) -> None:
    """裁剪件读不了**不让整次检查失败**：1.0 的裁剪块自带
    ``except (OSError, ValueError, TypeError)``，落到 ``target_status = "not_located"``
    之后照常把原图发出去（``tools.py:2186-2200``）。

    同时钉住另一件容易顺手改错的事：**``target_instruction`` 仍然填着**。1.0 判它填不填
    看的是「有没有指名目标人物」，不是「裁剪图做出来没有」——那句话里本来就写着「（如有）」。
    """
    inspector = ScriptedInspector({"observation": "戴了"})

    result = inspector.inspect(str(photo), QUESTION, target_person="爸爸", crop=str(tmp_path / "missing.jpg"))

    assert result is not None, "裁剪件缺席不该让整次检查失败"
    assert len(inspector.images[0]) == 1
    assert TARGET_INSTRUCTION_1_0.format(target_person="爸爸") in inspector.prompts[0]


def test_an_unreadable_photo_returns_none_without_calling_the_model(tmp_path: Path) -> None:
    """**整次失败**（1.0 在 ``encode_vision_image`` 抛错时落进外层 except，回「图片复核失败」）。

    读不了图就不是一次模型调用：``prompts`` 必须还是空的。
    """
    inspector = ScriptedInspector({"observation": "不该出现"})

    assert inspector.inspect(str(tmp_path / "missing.jpg"), QUESTION) is None
    assert inspector.prompts == []


def test_a_model_failure_returns_none(photo: Path) -> None:
    """端点挂了、回复不是 JSON——``chat_json`` 回 ``None``，这一层原样传下去，不抛。"""
    assert ScriptedInspector(None).inspect(str(photo), QUESTION) is None


# ---- 请求体的形状：真的发一次 HTTP --------------------------------------------------
#
# 上面那组断的是「交给 ``_chat`` 的东西」，这一组断的是「真的发到线上的东西」。
# ``chat_json`` 的 ``image`` 形参为这个端口从 ``str`` 放宽成了 ``str | Sequence[str]``，
# 而放宽一处共用的函数是有代价的：得有人证明多图真的发成了多块 ``image_url``。


class _RecordingEndpoint(BaseHTTPRequestHandler):
    """记录每一次 POST 的请求体；``/models`` 回一个模型名（``ServedModel`` 要探一次）。"""

    posts: ClassVar[list[dict[str, Any]]] = []
    reply: ClassVar[dict[str, Any]] = {"observation": "戴了", "certainty": "supported"}
    finish_reason: ClassVar[str | None] = "stop"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的接口
        self._send(b'{"object":"list","data":[{"id":"fake-vlm","object":"model"}]}', 200)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的接口
        length = int(self.headers.get("Content-Length") or 0)
        self.posts.append(json.loads(self.rfile.read(length)))
        choice: dict[str, Any] = {"message": {"content": json.dumps(self.reply, ensure_ascii=False)}}
        if self.finish_reason is not None:
            choice["finish_reason"] = self.finish_reason
        body = {"choices": [choice]}
        self._send(json.dumps(body).encode("utf-8"), 200)

    def _send(self, payload: bytes, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture
def inspector_factory() -> Iterator[Any]:
    """一个打到本文件自己起的端点上的真适配器（探测与调用都是真 HTTP）。"""
    _RecordingEndpoint.posts = []
    _RecordingEndpoint.finish_reason = "stop"
    httpd = HTTPServer(("127.0.0.1", 0), _RecordingEndpoint)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{httpd.server_port}/v1"
    try:
        yield lambda: OpenAIPhotoInspector(base_url, ServedModel(base_url, timeout_s=5.0), timeout_s=5.0)
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_wire_body_carries_the_question_and_two_image_blocks(
    inspector_factory: Any, photo: Path, crop: Path
) -> None:
    """``content`` 是块数组：一个 text 块 + 每个图一个 ``image_url`` 块，**次序就是传进来的次序**。"""
    result = inspector_factory().inspect(
        str(photo), QUESTION, target_person="爸爸", crop=str(crop), faces=FACES
    )

    assert result == {"observation": "戴了", "certainty": "supported"}
    assert len(_RecordingEndpoint.posts) == 1
    body = _RecordingEndpoint.posts[0]
    content = body["messages"][0]["content"]
    assert [block["type"] for block in content] == ["text", "image_url", "image_url"]
    assert QUESTION in content[0]["text"]
    assert FACE_MANIFEST_PREFIX_1_0 in content[0]["text"]
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert content[1]["image_url"]["url"].endswith(_b64(crop.read_bytes()))
    assert content[2]["image_url"]["url"].endswith(_b64(photo.read_bytes()))
    # 模型名是**问端点要的**，不是写死的常量。
    assert body["model"] == "fake-vlm"


def test_the_wire_body_has_a_single_image_block_without_a_crop(inspector_factory: Any, photo: Path) -> None:
    inspector_factory().inspect(str(photo), QUESTION)

    content = _RecordingEndpoint.posts[0]["messages"][0]["content"]
    assert [block["type"] for block in content] == ["text", "image_url"]


def test_wire_reply_without_finish_is_unknown_and_never_retried(
    inspector_factory: Any, photo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Valid answer JSON alone does not prove that the generation completed."""
    monkeypatch.setattr(_RecordingEndpoint, "finish_reason", None)
    with pytest.raises(ModelServiceError) as caught:
        inspector_factory().inspect(str(photo), QUESTION)
    assert caught.value.error_code == "provider_response_invalid"
    assert caught.value.request_policy["request_outcome_unknown"] is True
    assert len(_RecordingEndpoint.posts) == 1
    assert QUESTION in _RecordingEndpoint.posts[0]["messages"][0]["content"][0]["text"]


# ---- 对照臂：这个端口做的事，``VlmObserver`` 结构上做不到 ---------------------------


def test_the_observer_has_no_question_slot_which_is_why_this_port_exists() -> None:
    """**这是「先落端口」这件事的对照臂。**

    ``VlmObserver`` 的提示词是七个字段的固定 schema，**一个字的问题槽都没有**——它对
    任何输入的回复形状都一样。所以「这一问」不是给它加个形参就能长的：它问的是
    「这张图里有什么」，而这里问的是「用户想知道的那件事」。同端点、不同的两件事。
    """
    assert "{question}" not in _OBSERVE_PROMPT
    assert "{question}" in INSPECT_PROMPT


# ---- 送图前的缩放：精读与观察共用同一档 ------------------------------------------


def test_the_inspector_downscales_both_the_photo_and_the_crop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, photo: Path, crop: Path
) -> None:
    """精读的 36s 里绝大部分是 4,034 个图片 token；原图与裁剪件都要过同一个缩放。"""
    seen: list[tuple[str, int | None]] = []

    def fake_encode(path: Path, *, max_side: int | None = None) -> str:
        seen.append((path.name, max_side))
        return "data:image/jpeg;base64,eA=="

    monkeypatch.setattr("sentrix.infrastructure.media.photo_inspector._encode_image", fake_encode)
    ScriptedInspector({"observation": "戴了", "certainty": "supported"}).inspect(
        str(photo), QUESTION, crop=str(crop)
    )

    assert [name for name, _ in seen] == [photo.name, crop.name]
    assert [max_side for _, max_side in seen] == [IMAGE_MAX_SIDE, IMAGE_MAX_SIDE]


@pytest.mark.parametrize("suffix", [".mp4", ".jpg", ".png"])
def test_mp4_magic_never_becomes_image_url_or_discovers_model(tmp_path, suffix, monkeypatch):
    path = tmp_path / ("not-an-image" + suffix)
    path.write_bytes(bytes.fromhex("000000206674797069736f6d0000020069736f6d69736f32617663316d703431"))
    inspector = ScriptedInspector({"observation": "must not be used"})

    def no_discovery(*_args):
        raise AssertionError("invalid media must fail before model discovery")

    monkeypatch.setattr(ServedModel, "current", no_discovery)
    with pytest.raises(ModelServiceError) as caught:
        inspector.inspect(path, QUESTION)
    assert caught.value.error_code == "request_configuration_invalid"
    assert caught.value.request_policy["request_outcome_unknown"] is False
    assert caught.value.request_policy["request_phase"] == "media_validation"
    assert inspector.images == [] and inspector.prompts == []


@pytest.mark.parametrize(
    ("image_format", "mime"),
    [
        ("JPEG", "image/jpeg"),
        ("PNG", "image/png"),
        ("WEBP", "image/webp"),
        ("GIF", "image/gif"),
        ("BMP", "image/bmp"),
        ("TIFF", "image/tiff"),
    ],
)
def test_valid_image_magic_preserves_exact_bytes_and_actual_mime(tmp_path, image_format, mime):
    # A misleading extension must not affect a valid image container's MIME.
    path = tmp_path / "photo.unrelated"
    Image.new("RGB", (12, 8), "red").save(path, format=image_format)
    original = path.read_bytes()
    inspector = ScriptedInspector({"observation": "fixture"})
    inspector.inspect(path, QUESTION)
    url = inspector.images[0][0]
    assert url.startswith(f"data:{mime};base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == original
    assert path.read_bytes() == original


def test_corrupt_image_is_an_explicit_local_error_without_a_model_call(tmp_path):
    path = tmp_path / "corrupt.jpg"
    path.write_bytes(b"\xff\xd8\xff\xe0truncated-jpeg")
    inspector = ScriptedInspector({"observation": "must not be used"})
    with pytest.raises(ModelServiceError) as caught:
        inspector.inspect(path, QUESTION)
    assert caught.value.request_policy["input_error"] == "invalid_image_data"
    assert caught.value.error_code != "provider_rejected"
    assert inspector.images == []
