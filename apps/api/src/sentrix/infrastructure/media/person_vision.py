"""人物视觉：带编号的预览图渲染。

逐行移植 1.0 ``backend/person_moments.py:91-124`` 的 ``render_numbered_preview``。

**一处订正，有实测数据支撑。** 1.0 那一版把 bbox 当成 ``(x, y, width, height)`` 来画：

.. code-block:: python

    x, y, width, height = bbox[0], bbox[1], bbox[2], bbox[3]
    draw.rectangle([x, y, x + width, y + height], ...)

而落库的 bbox 是 ``(x1, y1, x2, y2)``——1.0 自己的 ``face_detector.py:136`` 就是这么产的，
2.0 的 ``expanded_person_crop`` 也是这么解读的。**读 1.0 的真实库判定**（30317 条实例）：

- 按 ``(x1,y1,x2,y2)`` 解释：宽度 64.1 – 2147.0 px，**最小值 64.1 正好是 ``FACE_MIN_SIZE``**
  （人脸准入下限，这个巧合不可能出现在错的解释里）；
- 按 ``(x,y,w,h)`` 解释：宽度 76.3 – **11335.5 px**——一个 11335 像素宽的人脸；
- 0 条出现 ``x2 <= x1``。

所以 2.0 按 ``(x1, y1, x2, y2)`` 画。**影响面**：标签位置两种解释下都是对的（都画在
``bbox[0], bbox[1]`` 上方 14px，也就是人脸的左上角），错的是**矩形本身**——模型看到的
框比真脸大一倍左右。它仍然能靠标签完成归属，但那是在一个错框上做的判断。

**临时文件由调用方删除**（1.0 的 docstring 写明：预览图从不作为 Asset 或长期文件保存）。

同一个文件里还有 :func:`render_person_crop`：同一层边界的另一件事——那里裁的是
**一个人**（外观证据），这里画的是**一屋子人**（瞬间抽取）。
"""

from __future__ import annotations

import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from sentrix.domain.identity.appearance import expanded_person_crop
from sentrix.infrastructure.media.vlm_observer import IMAGE_MAX_SIDE

# 1.0 ``person_moments.py:100-113`` 的原值。红框、线宽 3、标签画在框上方 14px。
BOX_OUTLINE = "red"
BOX_WIDTH = 3
LABEL_DY = 14
LABEL_MARGIN = 10
LABEL_LINE_HEIGHT = 14
JPEG_QUALITY = 90


def render_numbered_preview(
    asset_path: str | Path,
    faces: Sequence[Mapping[str, Any]],
) -> tuple[str, dict[str, dict[str, Any]]]:
    """在原图上画出人脸框与 ``P1`` / ``P2`` 标签，返回 ``(临时图路径, 标签 → 身份)``。

    ``faces`` 每项至少要有 ``bbox``（``(x1, y1, x2, y2)``）；没有框的按 1.0 的做法
    退化成「把标签画在左上角依次排下来」，**而不是丢掉这一项**——丢了他，模型会看到
    一个编号跳号（P1、P3），然后开始猜 P2 去哪了。
    """
    from PIL import Image, ImageDraw, ImageOps

    with Image.open(asset_path) as source:
        # EXIF 旋转必须在这一步做掉：手机照片的 bbox 是在**转正之后**的坐标系里算的，
        # 不转的话框会整体错位——而那种错看起来像「模型认错人」。
        image = ImageOps.exif_transpose(source).convert("RGB")

    # **先缩到模型上限，再画框与标签。** 反过来（先画后缩）会让默认字体（约 11px）
    # 跟着图片一起缩：3264px 的原图缩到 1280 时标签只剩 ~4px，模型读不到 P1/P2，
    # 就认不出这张图上的 moment 属于谁——158 实测两轮都因此稳定少 1 条 moments。
    scale = min(1.0, IMAGE_MAX_SIDE / max(image.size))
    if scale < 1.0:
        image = image.resize(
            (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
            Image.LANCZOS,
        )
    draw = ImageDraw.Draw(image)
    label_map: dict[str, dict[str, Any]] = {}
    for index, face in enumerate(faces, start=1):
        label = f"P{index}"
        bbox = list(face.get("bbox") or [])
        if len(bbox) >= 4:
            # bbox 来自**转正后的原图**坐标系，画之前要与图片一起缩。
            left, top, right, bottom = (float(value) * scale for value in bbox[:4])
            draw.rectangle([left, top, right, bottom], outline=BOX_OUTLINE, width=BOX_WIDTH)
            draw.text((left, max(0.0, top - LABEL_DY)), label, fill=BOX_OUTLINE)
        else:
            draw.text((LABEL_MARGIN, LABEL_MARGIN + index * LABEL_LINE_HEIGHT), label, fill=BOX_OUTLINE)
        label_map[label] = {
            "face_instance_id": face.get("face_instance_id"),
            "person_id": face.get("person_id"),
            "cluster_id": face.get("cluster_id"),
            "observation_id": face.get("observation_id"),
            "event_id": face.get("event_id"),
        }
    # 用 with 而不是 1.0 的「取出名字再 close」：`delete=False` 保证退出 with 之后
    # 文件仍在（这是这条的全部作用——不是 with 会把它删掉），而 with 让「异常路径上
    # 也关掉句柄」由语言保证，不靠调用顺序。
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as handle:
        image.save(handle.name, format="JPEG", quality=JPEG_QUALITY)
        return handle.name, label_map


class PillowPersonCropRenderer:
    """``PersonCropRenderer`` 的实现。方法体就一行——**这一层本身没有逻辑**。

    单独一个类而不是把 ``render_person_crop`` 直接当实现：application 只认端口，
    而端口的方法叫 ``render``。这个类的全部职责是让 ``PIL`` 与 ``Image.open`` 的名字
    停在 infrastructure 这一侧（同 ``PillowMetadataReader`` / ``PillowPreviewEncoder``）。
    """

    def render(self, asset_path: str | Path, bbox: Sequence[float] | None) -> tuple[str, list[int]]:
        return render_person_crop(asset_path, bbox)


class PillowPersonPreviewRenderer:
    """``PersonPreviewRenderer`` 的实现。方法体一行——**这一层本身没有逻辑**。

    同 ``PillowPersonCropRenderer``：application 只认端口，而端口的方法叫 ``render``。
    这个类的全部职责是让 ``PIL`` 与 ``ImageDraw`` 的名字停在 infrastructure 这一侧。
    """

    def render(
        self, asset_path: str | Path, faces: Sequence[Mapping[str, Any]]
    ) -> tuple[str, dict[str, dict[str, Any]]]:
        return render_numbered_preview(asset_path, faces)


def render_person_crop(
    asset_path: str | Path,
    bbox: Sequence[float] | None,
) -> tuple[str, list[int]]:
    """按人脸框裁出「头 + 上半身」并落成 JPEG，返回 ``(临时图路径, 源坐标)``。

    逐行移植 1.0 ``app.py:2038-2042``。它自己不做几何——``expanded_person_crop``
    （``domain/identity/appearance.py``）算，这里只负责把 PIL 挡在这一层里。

    **EXIF 必须先转正**，与 :func:`render_numbered_preview` 同一条理由：手机照片的
    bbox 是在转正后的坐标系里算的，不转的话框会整体错位——而那种错看起来像「模型
    认不出这个人穿了什么」，不像坐标系错了。

    **``ValueError`` 不吞。** 框退化、图太小、裁出来不够大，``expanded_person_crop``
    一律抛，这里让它穿过去。吞掉就等于「这张脸没有外观证据」，而调用方从返回值上
    分不出它与「模型说没看清」——前者是这张照片不能用，后者是这个人的外观本来就没
    有可描述之处，两件事在「兜底占比」这类统计里必须分得开。1.0 是在更外层的循环里
    统一 catch 的，那一层在 2.0 是 ``application/identity/observe_appearance.py``。

    **临时文件由调用方删除。** 1.0 把临时图落在 ``DATA_DIR``（那是当时的既定做法），
    2.0 落在系统临时目录——位置不是产品行为，落哪都行，但删不删是。
    """
    # 延迟 import：``PIL`` 只有真跑到这一步才需要，而模块顶层的 import 会让
    # 「只想画预览图」的路径也背上它（1.0 也是在函数里 import 的）。
    from PIL import Image, ImageOps

    with Image.open(asset_path) as source:
        # ``exif_transpose`` 与 ``convert`` 各返回一张**新图**，与 ``source`` 无关，
        # 所以下面把 ``source`` 关掉不影响后面 crop。
        image = ImageOps.exif_transpose(source).convert("RGB")
    crop, crop_bbox = expanded_person_crop(image, bbox)
    # 用 with 的理由同 ``render_numbered_preview``：`delete=False` 保证退出之后文件
    # 还在，with 保证异常路径上句柄也关得掉。
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as handle:
        crop.save(handle.name, format="JPEG", quality=JPEG_QUALITY)
        return handle.name, crop_bbox
