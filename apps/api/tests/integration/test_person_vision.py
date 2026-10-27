"""带编号预览图的渲染。

**这份用例的首要职责是把 bbox 的解释钉死。** 1.0 的 ``render_numbered_preview`` 把
``(x1,y1,x2,y2)`` 当成 ``(x,y,w,h)`` 画，框会大一倍左右；而两种解释下**标签位置都是
对的**，所以只看「画出来没有红框」的用例两种实现都能过。

分辨办法：**回读像素**，找到红框左右两条边的实际 x 坐标。

同一个文件的另一半是 ``render_person_crop``（Task 85c，外观证据的头肩裁剪件）——
两个函数都住在这个源模块里，所以两份用例也住在这个文件里（见下方分隔）。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from PIL import Image

from sentrix.domain.identity.appearance import expanded_person_crop
from sentrix.infrastructure.media.person_vision import (
    render_numbered_preview,
    render_person_crop,
)

BOX = [50, 60, 150, 160]  # (x1, y1, x2, y2)：一个 100x100 的框


@pytest.fixture
def photo(tmp_path: Path) -> Path:
    """一张纯白底图。用真图而不是 mock，是因为这条用例要读像素。"""
    path = tmp_path / "photo.jpg"
    Image.new("RGB", (300, 300), "white").save(path, format="JPEG", quality=100)
    return path


@pytest.fixture
def face() -> dict[str, object]:
    return {
        "face_instance_id": "face_1",
        "person_id": "person_1",
        "cluster_id": "cluster_1",
        "observation_id": "obs_1",
        "event_id": "event_1",
        "bbox": BOX,
    }


def reddish(pixel: tuple[int, ...]) -> bool:
    red, green, blue = pixel[:3]
    return red > 120 and green < 110 and blue < 110


def red_columns(image: Image.Image, row: int) -> list[int]:
    """某一行上所有偏红像素的 x。"""
    return [x for x in range(image.width) if reddish(image.getpixel((x, row)))]


def test_the_box_edges_land_on_x1_and_x2(tmp_path: Path, photo: Path, face: dict[str, object]) -> None:
    """**这条就是那个 bug 的门禁。**

    按 ``(x1,y1,x2,y2)`` 画：左右边在 x=50 与 x=150。
    按 ``(x,y,w,h)`` 画：会画到 ``[50, 60, 50+150, 60+160] = [50,60,200,220]``，
    右边跑到 200——这条断言当场变红。
    """
    preview, _ = render_numbered_preview(photo, [face])
    try:
        with Image.open(preview) as drawn:
            columns = red_columns(drawn, 110)  # 框的竖直中段，避开上下边与标签
        assert columns, "这一行上一个红像素都没有——框根本没画出来"
        assert min(columns) == pytest.approx(BOX[0], abs=3), f"左边不在 x1: {min(columns)}"
        assert max(columns) == pytest.approx(BOX[2], abs=3), f"右边不在 x2: {max(columns)}"
    finally:
        os.unlink(preview)


def test_the_box_edges_land_on_y1_and_y2(photo: Path, face: dict[str, object]) -> None:
    """**扫框的中间列而不是左边。** 标签 ``P1`` 是红字、画在 ``(x1, y1 - 14)``，
    与左边框共用一段 x——扫左边会把标签的 46 当成「上边」，于是断言红在一个
    与实现无关的理由上（第一版就是这么错的）。"""
    preview, _ = render_numbered_preview(photo, [face])
    try:
        middle = (BOX[0] + BOX[2]) // 2
        with Image.open(preview) as drawn:
            rows = [y for y in range(drawn.height) if reddish(drawn.getpixel((middle, y)))]
        assert min(rows) == pytest.approx(BOX[1], abs=3), f"上边不在 y1: {min(rows)}"
        assert max(rows) == pytest.approx(BOX[3], abs=3), f"下边不在 y2: {max(rows)}"
    finally:
        os.unlink(preview)


def test_the_label_map_carries_every_identity_field(photo: Path, face: dict[str, object]) -> None:
    """标签映射要带全五个身份字段——模型回的是 ``P1``，调用方要据此落库，
    少一个字段那条瞬间就缺一个外键。"""
    preview, labels = render_numbered_preview(photo, [face])
    try:
        assert labels["P1"] == {
            "face_instance_id": "face_1",
            "person_id": "person_1",
            "cluster_id": "cluster_1",
            "observation_id": "obs_1",
            "event_id": "event_1",
        }
    finally:
        os.unlink(preview)


def test_labels_are_numbered_from_one_in_order(photo: Path) -> None:
    faces = [{"face_instance_id": f"face_{index}", "bbox": BOX} for index in range(1, 4)]
    preview, labels = render_numbered_preview(photo, faces)
    try:
        assert list(labels) == ["P1", "P2", "P3"]
        assert labels["P2"]["face_instance_id"] == "face_2"
    finally:
        os.unlink(preview)


def test_a_face_without_a_box_still_gets_a_label(photo: Path) -> None:
    """没有框的人**不丢**，只是没有矩形。丢了的话模型会看到一个跳号的序列
    （P1、P3），然后开始猜 P2 去哪了。"""
    preview, labels = render_numbered_preview(
        photo, [{"face_instance_id": "face_1"}, {"face_instance_id": "face_2", "bbox": BOX}]
    )
    try:
        assert list(labels) == ["P1", "P2"]
        with Image.open(preview) as drawn:
            # P1 退化成左上角的文字，框是 P2 的——所以左侧那一列没有红
            assert not red_columns(drawn, 110) or min(red_columns(drawn, 110)) >= BOX[0] - 3
    finally:
        os.unlink(preview)


def test_the_preview_is_a_real_jpeg_on_disk(photo: Path, face: dict[str, object]) -> None:
    """1.0 的 docstring 写明：预览图是临时 JPEG，**从不作为 Asset 或长期文件保存**，
    且**必须由调用方删除**。所以这里既验它落成了 JPEG，也验路径是调用方能删的普通文件。"""
    preview, _ = render_numbered_preview(photo, [face])
    try:
        assert Path(preview).suffix == ".jpg"
        with Image.open(preview) as drawn:
            assert drawn.format == "JPEG"
    finally:
        os.unlink(preview)


# ---- render_person_crop（T85c）：外观证据的头肩裁剪件 ------------------------------

# 这一半验的是**另一个人一件事**：``render_numbered_preview`` 画的是「一屋子人」，
# ``render_person_crop`` 裁的是「一个人」。它做的是 domain 不许做的事——
# domain-purity 契约把 PIL 点名禁掉，所以 ``expanded_person_crop`` 收的是一个只有
# ``width`` / ``height`` / ``crop`` 的 Protocol，真正的 ``Image.open`` 只出现在这一层。
#
# 三条规则各有用例：
# 1. **EXIF 转正在裁剪之前**——不转，手机照片的框会整体错位，而错出来的样子像
#    「模型认不出这个人穿了什么」，与真正的模型失败长得一样；
# 2. **``ValueError`` 穿过去**，不回退整图——回退会让模型看到一整张全景，然后把背景里
#    别人的衣服记到这个人的外观上；
# 3. **落的是裁剪件本身**，不是原图——尺寸与内容两边都验。

FACE = [190.0, 100.0, 212.0, 132.0]


@pytest.fixture
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 ``tempfile`` 的落点收进 tmp_path，好断言「临时文件建了没有」。

    ``tempfile`` 会把首次算出的目录缓存进模块级的 ``tempdir``，所以设环境变量不管用，
    得直接改那个属性。
    """
    directory = tmp_path / "scratch"
    directory.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(directory))
    return directory


def _painted(path: Path, bbox: list[float], *, size: tuple[int, int] = (400, 600)) -> list[int]:
    """造一张图：底色纯蓝，**把 ``expanded_person_crop`` 会裁走的那一块**涂成纯红。

    涂哪一块由 domain 的纯函数现场算出来，不写死坐标——所以下面那条用例问的是
    「存下来的东西是不是那一块」，而不是把几何再验一遍。反过来若涂的是人脸框，
    这条用例就退化成「裁的不是人脸框」，什么也没验到。
    """
    blank = Image.new("RGB", size, (0, 0, 255))
    _, crop_bbox = expanded_person_crop(blank, bbox)
    image = blank.copy()
    image.paste((255, 0, 0), tuple(int(value) for value in crop_bbox))
    image.save(path, format="JPEG", quality=100)
    return crop_bbox


def test_the_saved_file_is_the_crop_not_the_whole_photo(scratch: Path, tmp_path: Path) -> None:
    """落下来的必须是裁剪件：尺寸等于裁剪框，内容是被标红的那一块。

    **尺寸与内容两边都验**，因为它们挡的是不同的错法——尺寸对而内容是整图，说明
    ``crop`` 的返回值被丢掉了（存了 ``image``）；内容对而尺寸不对，说明存的时候
    又缩放了。只验其中一边，这两种都过得去。
    """
    source = tmp_path / "album.jpg"
    crop_bbox = _painted(source, FACE)

    path, returned_bbox = render_person_crop(source, FACE)

    assert returned_bbox == crop_bbox, "返回的源坐标必须与 domain 算出来的一致——它要落库"
    with Image.open(path) as result:
        assert result.size == (crop_bbox[2] - crop_bbox[0], crop_bbox[3] - crop_bbox[1])
        assert result.format == "JPEG"
        # 取正中一格：JPEG 在涂色边界上有振铃，取边缘会验到压缩噪声而不是内容。
        center = result.getpixel((result.width // 2, result.height // 2))
    assert center[0] > 200 and center[1] < 60, f"裁到的不是标红那一块: {center}"


def test_the_box_is_read_in_the_exif_corrected_frame(scratch: Path, tmp_path: Path) -> None:
    """**这份用例真正守的那条。** 旋转 90° 的照片，框是在转正后的坐标系里算的。

    底图 100×200、EXIF Orientation=6（PIL 转正后是 200×100）。下面这个框在转正后的
    坐标系里合法（``x`` 到 140），在原图坐标系里横向只剩 7px——**同一个框，转了能裁、
    不转就抛**。所以这一条同时断言了两件事，缺一半就不成立：

    - 转正的那条路必须成功，且给出按 200 宽算出来的坐标；
    - 把 EXIF 抹掉之后同一个框必须抛 ``ValueError``。

    只断言前半条是不够的：一个「什么都不转、但恰好也不抛」的实现（比如偷偷回退整图）
    同样能过——而它正是这个适配器最该挡掉的那种实现。
    """
    rotated = tmp_path / "rotated.jpg"
    image = Image.new("RGB", (100, 200), (255, 0, 0))
    exif = Image.Exif()
    exif[0x0112] = 6
    image.save(rotated, format="JPEG", exif=exif)

    # 期望值按转正后的 200×100 手算：face 40×40，center_x 120，crop_width 54；
    #   left = round(120-27) = 93    right = round(120+27) = 147
    #   top  = round(20-14)  = 6     bottom = round(60+180) = 240，被图高 100 钳住
    path, crop_bbox = render_person_crop(rotated, [100.0, 20.0, 140.0, 60.0])

    assert crop_bbox == [93, 6, 147, 100], "坐标是按转正后的 200×100 算的，不是按原图的 100×200"
    with Image.open(path) as result:
        assert result.size == (54, 94)

    stripped = tmp_path / "stripped.jpg"
    image.save(stripped, format="JPEG")  # 同样的像素，只是没有 EXIF
    with pytest.raises(ValueError, match="too small"):
        render_person_crop(stripped, [100.0, 20.0, 140.0, 60.0])


def test_a_crop_too_small_raises_instead_of_falling_back(scratch: Path, tmp_path: Path) -> None:
    """图太小 → 抛，**不回退整图**。

    回退整图的后果不是报错，是模型看到一整张全景、然后把背景里别人的衣服记到这个
    人的外观上——与 ``expanded_person_crop`` 的 docstring 同一条理由。1.0 是在更外层
    的循环里 catch 的（``app.py:2058``），所以这里必须抛到那一层去。
    """
    tiny = tmp_path / "tiny.jpg"
    Image.new("RGB", (30, 40), (0, 0, 255)).save(tiny, format="JPEG")

    with pytest.raises(ValueError, match="too small"):
        render_person_crop(tiny, [0.0, 0.0, 30.0, 40.0])
    assert list(scratch.iterdir()) == [], "抛之前不该留下临时文件"


def test_a_degenerate_box_raises(scratch: Path, tmp_path: Path) -> None:
    """退化框（宽或高 < 2px）与缺框，都由 ``expanded_person_crop`` 判、由这里放行。"""
    source = tmp_path / "album.jpg"
    Image.new("RGB", (400, 600), (0, 0, 255)).save(source, format="JPEG")

    with pytest.raises(ValueError, match="too small"):
        render_person_crop(source, [100.0, 100.0, 101.0, 130.0])
    with pytest.raises(ValueError, match="four coordinates"):
        render_person_crop(source, None)
    assert list(scratch.iterdir()) == []


def test_the_temporary_file_is_left_for_the_caller_to_delete(scratch: Path, tmp_path: Path) -> None:
    """临时文件**不自己删**：调用方还要把它喂给模型，用完才 unlink。

    这一条钉的是「别顺手加个 try/finally 在这里删掉」——删了的话，调用方拿到的是一个
    已经不存在的路径，而两边都不报错，只有模型调用会失败。
    """
    source = tmp_path / "album.jpg"
    Image.new("RGB", (400, 600), (0, 0, 255)).save(source, format="JPEG")

    path, _ = render_person_crop(source, FACE)

    assert Path(path).is_file()
    assert Path(path).parent == scratch, "落点跟着 tempfile 走，不写死在 DATA_DIR"


def test_a_large_photo_is_downscaled_before_labels_are_drawn(tmp_path: Path) -> None:
    """先缩到 ``IMAGE_MAX_SIDE`` 再画：否则默认字体的 P 标签会跟着图一起缩到 ~4px。

    158 实测：先画后缩时 moments 两轮都少 1 条（模型读不到 P 编号，无法归属）。
    这条钉住「预览图不超过上限」这一半；另一半（标签保持可读）靠真实导入的 moments
    条数验证。
    """
    from sentrix.infrastructure.media.vlm_observer import IMAGE_MAX_SIDE

    path = tmp_path / "big.jpg"
    Image.new("RGB", (2000, 1500), "white").save(path, format="JPEG", quality=100)
    face = {
        "bbox": [100, 100, 300, 400],
        "face_instance_id": "f1",
        "person_id": "p1",
        "cluster_id": "c1",
        "observation_id": "o1",
        "event_id": "e1",
    }

    preview, labels = render_numbered_preview(path, [face])
    try:
        with Image.open(preview) as image:
            assert max(image.size) == IMAGE_MAX_SIDE
            assert image.size == (1280, 960)
    finally:
        os.unlink(preview)
    assert list(labels) == ["P1"]
