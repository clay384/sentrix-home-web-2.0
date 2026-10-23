"""buffalo_l 人脸链路：检测 → SCRFD 复核 → 身份向量。

1.0 ``model_clients.py:1616-1938`` 的 ``FaceAdapter`` 移植。检测器本身（RetinaFace 分块）
与它那三个复核几何在 ``face_detector.py``（Task 77）；本模块负责**它之后的那一段**：
把候选送进 buffalo_l 的 SCRFD 复核，过判据的落 ``verified``、其余落 ``uncertain``，
两种都算好向量与质量交给在线匹配。

**判据是四项合取**（1.0 ``model_clients.py:1781-1790``，一项都没有合并）：

    ``scrfd_score >= 0.7`` ∧ ``bbox_agreement`` ∧ ``landmark_sanity`` ∧ ``area_ratio >= 0.003``

``landmark_sanity`` 读的是 **SCRFD 自己的关键点**。这不是优化：实测（``s5-deviations``
第 59 条）RetinaFace 自己的关键点在这条判据下**一个都过不了**——照搬不做替换，每一张脸
都会停在 ``uncertain``、``identity_eligible`` 恒假、一张脸也进不了簇。1.0 的注释写在
``model_clients.py:1788``。

**``identity_eligible`` 比 ``verified`` 多一道质量门（0.55）。** 「够格进簇」与「够格代表
一个人去匹配」是两件事：低质量的合格脸仍然落库（它是证据），只是不去匹配已有簇。

**``embedding_version`` 取的是 ``FACE_MODEL_NAME``。** 变量名与语义不符（名字里写的是
model，存进去的位置是 version），但**有真实后果**：它参与 ``(model_name, model_version)``
签名比对，换模型包时旧簇会被整体跳过。照搬，不改名——见 ``s5-deviations`` 证据 17。

**为什么对齐走 insightface 的 ``norm_crop`` 而不是按框裁。** 按五个关键点仿射对齐出来的
112×112 才是 ArcFace 训练时的输入分布；按框裁出来的图识别率会掉，而且掉的方式是
「余弦都差不多」，不是报错——那是这一层最贵的失败形态。

**这一层的三个 session 建完都要过 CUDA 防线**（``onnx_runtime.require_requested_providers``）：
insightface 的两个模块会话在 ``_build_analysis`` 里逐个读、独立加载的 w600k_r50 在
``_load_buffalo_recognition`` 里读。**只有 ``CudaProviderMissingError`` 不被本模块的
``except Exception`` 吞掉**——那些 ``except`` 的成立条件是「人脸是可选能力」，而依赖装错
不是这个条件（第 111 条）。
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

from sentrix.domain.identity.faces import compute_face_quality
from sentrix.domain.identity.models import (
    FACE_VALIDITY_UNCERTAIN,
    FACE_VALIDITY_VERIFIED,
    FaceDetection,
)
from sentrix.infrastructure.models.face_detector import (
    RetinaFaceTiledDetector,
    bbox_agreement,
    expand_crop,
    landmark_sanity,
)
from sentrix.infrastructure.models.onnx_runtime import (
    CUDA_EXECUTION_PROVIDER,
    FACE_PROVIDERS_ENV,
    CudaProviderMissingError,
    face_gpu_inference_gate,
    face_onnx_provider_options,
    face_onnx_providers,
    provider_chain,
    require_requested_providers,
)
from sentrix.infrastructure.thread_limits import ort_session_options

log = logging.getLogger(__name__)

# 身份向量走 buffalo_l 的 w600k_r50：1.0 的两个返回分支都把 ``embedding_model`` 写死成
# 它（``face_embeddings.py`` 的 AdaFace 那条路在 1.0 是死代码，见证据 1）。
EMBEDDING_MODEL = "buffalo_l"
RECOGNITION_WEIGHTS = "w600k_r50.onnx"

# 识别模型的输入：112×112、RGB、``(x - 127.5) / 128.0``。三个数都是 ArcFace 训练时定下的，
# 改一个不会报错，只会让所有余弦一起漂。
RECOGNITION_SIZE = 112
RECOGNITION_MEAN = 127.5
RECOGNITION_SCALE = 128.0

# 锐度归一化的两个参考点，在 ``log1p`` 尺度上（1.0 ``_normalize_sharpness`` 的默认实参）。
SHARPNESS_LOW = 2.5
SHARPNESS_HIGH = 7.0

# 1.0 的七个 ``os.getenv`` 与它们的默认值，一字不差。
MODEL_NAME_ENV = "FACE_MODEL_NAME"
MODEL_ROOT_ENV = "FACE_MODEL_ROOT"
MIN_SIZE_ENV = "FACE_MIN_SIZE"
VERIFIED_SCORE_ENV = "FACE_VERIFIED_SCRFD_SCORE"
VERIFIED_MIN_AREA_ENV = "FACE_VERIFIED_MIN_AREA"
IDENTITY_MIN_QUALITY_ENV = "FACE_IDENTITY_MIN_QUALITY"
DET_SIZE_ENV = "FACE_DET_SIZE"

DEFAULT_MODEL_NAME = "buffalo_l"
# **这是 insightface 的根，不是模型目录**：它的目录约定是 ``<root>/models/<name>/``。
# 1.0 把 ``FACE_MODEL_ROOT`` 用成了两个意思（``FaceAnalysis(root=...)`` 收到的是一层，
# 而 ``_load_buffalo_recognition`` 把同一层直接当模型目录用），两者只在默认路径下碰巧
# 一致。2.0 只保留一个意思：``FACE_MODEL_ROOT`` 是 insightface 根，模型目录由它派生。
DEFAULT_MODEL_ROOT = str(Path.home() / ".insightface")
MODELS_SUBDIR = "models"
DEFAULT_MIN_SIZE = 64
DEFAULT_VERIFIED_SCORE = 0.7
DEFAULT_VERIFIED_MIN_AREA = 0.003
DEFAULT_IDENTITY_MIN_QUALITY = 0.55
DEFAULT_DET_SIZE = 640

# 只装这条链路用得上的两个模块。insightface 默认会把 3D 关键点（143 MB）与性别年龄
# 一起加载，而这里只消费 SCRFD 的关键点与 ArcFace 的向量（1.0 ``:1910`` 同此）。
ALLOWED_MODULES = ["detection", "recognition"]

# 预置照片三级降级的三个参数，1.0 ``app.py:2270-2295`` 的字面量。
UPSCALE_MIN_DIM = 600
UPSCALE_TARGET = 1280.0
UPSCALE_MAX = 2.0
UPSCALE_MIN_SCALE = 1.05


def align_face_crop(
    image: Any,
    bbox: Any,
    landmarks: Any = None,
) -> Any:
    """按五个关键点仿射对齐出 112×112，退化成按框裁剪。

    1.0 ``model_clients.py:19-43``。两条路都返回 **RGB** 的 PIL 图——OpenCV 读进来的是
    BGR，所以两条路都要 ``[:, :, ::-1]`` 翻转；少翻一次不报错，只是所有向量都变得没用。

    关键点缺席、或 insightface 的对齐工具不可用时退到按框裁：这是一条**降级**，不是失败。
    框裁不出任何像素时抛 ``ValueError``——那说明调用方给的框本身是坏的，静默返回一张
    空图只会把问题推到更远处。
    """
    from PIL import Image

    if landmarks:
        try:
            import numpy as np
            from insightface.utils import face_align

            aligned_bgr = face_align.norm_crop(
                image, np.asarray(landmarks, dtype="float32"), image_size=RECOGNITION_SIZE
            )
            return Image.fromarray(aligned_bgr[:, :, ::-1])
        except (ImportError, TypeError, ValueError):
            pass
    left, top, right, bottom = (int(round(value)) for value in bbox)
    image_height, image_width = image.shape[:2]
    left, top = max(0, left), max(0, top)
    right, bottom = min(image_width, right), min(image_height, bottom)
    if right <= left or bottom <= top:
        raise ValueError("人脸框裁不出任何像素")
    return Image.fromarray(image[top:bottom, left:right][:, :, ::-1])


def _laplacian_variance(pil_image: Any) -> float:
    """灰度 Laplacian 方差：清晰度信号。1.0 ``model_clients.py:45-54``。

    任何失败都返回 ``0.0``——它是**加权项之一**，不是判据：算不出来时按「最不清晰」计，
    与「这张脸很糊」得到同一个结果。
    """
    import cv2
    import numpy as np

    try:
        gray = cv2.cvtColor(np.asarray(pil_image.convert("RGB")), cv2.COLOR_RGB2GRAY)
        return float(cv2.Laplacian(gray, cv2.CV_64F).var())
    except Exception:
        return 0.0


def _normalize_sharpness(raw: float, low: float = SHARPNESS_LOW, high: float = SHARPNESS_HIGH) -> float:
    """把 Laplacian 方差对数压缩到 0..1。1.0 ``model_clients.py:57-66``。

    ``low`` / ``high`` 是 ``log1p`` 尺度上的两个参考点，默认值对应「对齐好的 112×112
    清晰人脸裁剪」的实测分布；低于 ``low`` 记 0、高于 ``high`` 记 1。
    """
    import math

    value = (math.log1p(max(0.0, raw)) - low) / (high - low)
    return max(0.0, min(1.0, value))


class FaceAdapter:
    """人脸链路的生产入口：``detect(path)`` 给出一张图的全部人脸。

    **session 惰性建、进程内一份**（容器里也只存一份）：buffalo_l 的检测 + 识别两个
    ONNX 加起来 190 MB，每次导入一张照片重建一次会把导入时间全花在加载上。

    **权重缺席是允许的部署形态，不是异常**：``available`` 为假时 ``detect`` 直接返回
    空表，调用方按「这台机器没有人脸能力」降级。这条与
    ``RetinaFaceTiledDetector.available`` 同一个口径，由容器决定要不要把这条链路接上。
    """

    def __init__(
        self,
        detector: RetinaFaceTiledDetector | None = None,
        *,
        model_name: str | None = None,
        model_root: str | None = None,
    ) -> None:
        self._detector = detector if detector is not None else RetinaFaceTiledDetector()
        self._model_name = model_name or os.getenv(MODEL_NAME_ENV) or DEFAULT_MODEL_NAME
        self._model_root = model_root or os.getenv(MODEL_ROOT_ENV) or DEFAULT_MODEL_ROOT
        self._app: Any = None
        self._recognition_session: Any = None
        self._load_lock = threading.Lock()
        self._analysis_lock = threading.Lock()
        self._recognition_lock = threading.Lock()
        self.error: str | None = None

    @property
    def model_name(self) -> str:
        """识别模型的目录名。**它同时是写进 ``embedding_version`` 的那个值**。"""
        return self._model_name

    @property
    def recognition_path(self) -> str:
        """独立加载的 w600k_r50 权重路径：``<root>/models/<model_name>/w600k_r50.onnx``。

        **这一层不能写错。** insightface 的 ``FaceAnalysis`` 收的 ``root`` 是它的根，
        模型目录是它自己拼的 ``<root>/models/<name>``；把模型目录当成 ``root`` 传进去，
        它会在 ``<root>/models/models/<name>`` 找不到模型，然后**去 GitHub 下载**
        （实测：进程挂在一条到 github.com 的 HTTPS 连接上，CPU 为空转、日志什么都没有）。
        """
        return os.path.join(self._model_root, MODELS_SUBDIR, self._model_name, RECOGNITION_WEIGHTS)

    @property
    def available(self) -> bool:
        """检测权重与识别权重都在。缺任何一个，这条链路都给不出可用的向量。"""
        return self._detector.available and Path(self.recognition_path).is_file()

    def detect(self, path: str | Path) -> list[FaceDetection]:
        """一张图 → 人脸表。**返回的每一条都已经带好了向量**（没有向量的候选被丢掉）。

        与 ``RetinaFaceTiledDetector.detect`` 的两处分工要说清楚：那一层返回的是**候选**
        （细pass 的入选门槛 0.1、越界框不裁剪），**筛选在这一层**——``expand_crop`` 对
        越界框返回 ``(None, 0, 0)``、``min_size`` 挡掉小框、SCRFD 复核与 ``landmark_sanity``
        决定 ``verified`` 还是 ``uncertain``。分层与 1.0 一致，不在这两层之外再加一道。
        """
        if not self.available:
            return []
        try:
            image = self._read_image(path)
            if image is None:
                return []
            with face_gpu_inference_gate():
                return self._detect_unlocked(image)
        except CudaProviderMissingError:
            # 「人脸是可选能力」不覆盖这一条：它说的是依赖装错了，不是这台机器没有 GPU。
            # 吞掉它，下一个看到的仍然是「导入很慢 / 一张脸都没有」那个外表（第 111 条）。
            raise
        except Exception as error:
            # 人脸是可选能力，不该让整条导入链失败——**但这个兜底要留痕**。
            # 2026-10-07 实测：整册导入并行化后，`self._detector.detect()`（RetinaFace 的
            # ONNX 会话）被多个 worker 并发调用时抛异常，全部落进这里被吞掉，于是 14 张
            # 照片零张脸，而导入报告是 `imported: 14, failures: []`。那种「导入成功、
            # 人脸全丢」没有任何外部迹象，只有日志能拆穿。
            # 「没装权重」在更上面的 `if not self.available` 已经早退，不走这里。
            log.warning("face detect failed for %s: %s", path, error)
            self.error = str(error)
            return []

    def detect_identity_photo(self, path: str | Path) -> list[FaceDetection]:
        """身份预置照片的检测：三级降级。1.0 ``app.py:2253-2310``。

        1. **常规检测**——绝大多数照片在这一级就结束；
        2. **放大重试**：最长边 > 600、且放大倍数 > 1.05 时，把图放大到最长边 1280
           （上限 2 倍）重新检测。家庭照里的小脸在整图上会漏，放大后才检得出；
           框按同一个比例换算回原图坐标；
        3. **整图兜底**：把整张图当成一张脸——用户上传的预裁证件照就是这种。

        三个条件是**合取**的，缺一条就退到下一级：`max_dim > 600` 保证放大不会把
        小图吹成大图，`scale > 1.05` 保证放大真的在放大（1600px 的图算出来的 scale
        是 0.8，那是缩小，1.0 明确不要）。两条边界各自有用例骑住。

        **第三级在 1.0 是死路径**（``identity_adapter`` 恒 ``None``，见 ``s5-deviations``
        证据 8），2.0 把它接到真实的识别 session 上——见 ``_whole_image_face``。
        """
        faces = self.detect(path)
        if faces:
            return faces
        image = self._read_image(path)
        if image is None:
            return []
        height, width = image.shape[:2]
        max_dim = max(width, height)
        if max_dim > UPSCALE_MIN_DIM:
            scale = min(UPSCALE_MAX, UPSCALE_TARGET / max_dim)
            if scale > UPSCALE_MIN_SCALE:
                upscaled = self._detect_upscaled(image, path, scale)
                if upscaled:
                    return upscaled
        return self._whole_image_face(image)

    def _detect_upscaled(self, image: Any, path: str | Path, scale: float) -> list[FaceDetection]:
        """放大到临时文件再检测一次，框换算回原图坐标。

        走临时文件而不是把内存里的数组直接喂给检测器：``detect`` 的入口是**路径**
        （它要按 EXIF 转置、要兜住 HEIC），给它加一条「也可以收数组」的分支等于让
        「这张图从哪来」有两个答案。1.0 也是写临时文件。
        """
        from dataclasses import replace

        import cv2

        height, width = image.shape[:2]
        resized = cv2.resize(image, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_CUBIC)
        temporary = Path(f"{path}.upscaled.jpg")
        try:
            cv2.imwrite(str(temporary), resized)
            faces = self.detect(temporary)
        finally:
            temporary.unlink(missing_ok=True)
        # 只有框要换算：质量、面积比这些是在放大图上算的，1.0 同样如此。
        return [replace(face, bbox=tuple(value / scale for value in face.bbox)) for face in faces]

    def _whole_image_face(self, image: Any) -> list[FaceDetection]:
        """整张图当成一张脸。**1.0 这条路径是死的，2.0 把它接在真实的识别 session 上。**

        1.0 这里调的是 ``identity_adapter.embed``，而 ``identity_adapter`` 恒为 ``None``
        → ``AttributeError`` → 被裸 ``except`` 吞掉 → 永远返回空表。所以下面那些常数
        （置信度 0.99、质量 0.8、面积比 1.0、锐度 0、正脸）**从来没有被任何一张真照片
        检验过**——照搬它们是为了让以后想调的人有一个可比的对象，不是为了说它们是对的。

        取不到向量就返回空表：这一级的存在理由是「这张图本来就是一张脸」，没有向量就
        没有可用的东西。
        """
        self._ensure_loaded()
        height, width = image.shape[:2]
        try:
            embedding = self._recognition_embed(align_face_crop(image, [0, 0, width, height]))
        except Exception:
            return []
        if not embedding:
            return []
        return [
            FaceDetection(
                embedding=tuple(embedding),
                bbox=(0.0, 0.0, float(width), float(height)),
                confidence=0.99,
                quality=0.8,
                pose_bucket="frontal",
                area_ratio=1.0,
                sharpness=0.0,
                embedding_model=EMBEDDING_MODEL,
                embedding_version=self._model_name,
                validity=FACE_VALIDITY_VERIFIED,
                identity_ready=True,
                identity_eligible=True,
            )
        ]

    @staticmethod
    def _read_image(path: str | Path) -> Any:
        """OpenCV 读图；读不出来再用 PIL 试一次（HEIC/HEIF 与部分 PNG）。

        1.0 ``model_clients.py:1673-1685``。``cv2.imread`` 会按 EXIF 转置，所以 PIL 那一支
        也要 ``exif_transpose``——否则 HEIC 的框与 JPEG 的框不在同一个坐标系里，
        而两者混在一个相册里是常态。
        """
        import cv2
        import numpy as np

        image = cv2.imread(str(path))
        if image is not None:
            return image
        from PIL import Image, ImageOps

        from sentrix.infrastructure.media.image_io import ensure_heif_support

        ensure_heif_support()
        with Image.open(path) as pil_image:
            oriented = ImageOps.exif_transpose(pil_image).convert("RGB")
            return cv2.cvtColor(np.array(oriented), cv2.COLOR_RGB2BGR)

    # ---- 推理 --------------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        """第一次用到时才建两个 session。

        **不用 1.0 那种「``self._retina is None`` 时顺带加载」的写法**：那是模块级单例的
        产物（1.0 在 import 期就构造 ``FaceAdapter``，加载只可能发生一次）。2.0 的适配器
        由容器构造，这里要立住的是另一件事——检测器、分析器、识别 session **要么都还没有，
        要么都有**，否则第一条导入会看到半建好的状态。

        **识别 session 先建。** 它是这里唯一会抛错的步骤（检测器那一步不在这里：它的
        ``_session`` 自己守懒加载，抛错后仍是 ``None``，所以下一次调用会再抛一次，是黏的）。
        而这道守卫写的是 ``self._app is not None``，先建 ``_app`` 的话：一次退化抛错之后
        ``_app`` 已经非空，第二次调用直接从这里返回、``_recognition_session`` 永远是
        ``None``——**硬报错只响一次，之后整条链路静默降级成「一张脸都没有」**，而
        ``_uncertain`` 对 ``_recognition_session is None`` 的处置正是返回空表。
        """
        if self._app is not None:
            return
        with self._load_lock:
            if self._app is None:
                self._recognition_session = self._load_buffalo_recognition()
                self._app = self._build_analysis()

    def _build_analysis(self) -> Any:
        """建 buffalo_l 的 ``FaceAnalysis``：SCRFD 检测 + ArcFace 识别两个模块。

        1.0 ``model_clients.py:1901-1919``。``ctx_id`` 由 provider 链推出来，而不是另读一个
        环境变量：provider 是「请求什么」，``ctx_id`` 是「在哪跑」，两者说岔了就是
        「请求了 CUDA、实际在 CPU 上跑」那一族静默退化（见 ``onnx_runtime`` 的模块注释）。
        """
        from insightface.app import FaceAnalysis

        # insightface 在模型目录不存在时会**去 GitHub 下载**，而下载失败的表现是
        # 「进程挂住、CPU 空转、日志一个字没有」——实测过。缺模型是部署形态，不是运行时
        # 异常，所以在这里直接失败，由 ``detect`` 记进 ``error`` 后返回空表。
        model_dir = Path(self._model_root) / MODELS_SUBDIR / self._model_name
        if not model_dir.is_dir():
            raise FileNotFoundError(f"缺人脸模型目录 {model_dir}")

        analysis = FaceAnalysis(
            name=self._model_name,
            # **insightface 收的是两个平行列表**（provider 名 + options），不是
            # onnxruntime 那种 ``(name, options)`` 元组：给元组它直接退化成 CPU，报的是
            # 「Only string values for 'providers' are supported」——一行警告，然后
            # 整条链路在 CPU 上跑，日志里看不出区别。实测踩过。
            providers=provider_chain(FACE_PROVIDERS_ENV),
            provider_options=face_onnx_provider_options(FACE_PROVIDERS_ENV),
            allowed_modules=list(ALLOWED_MODULES),
            root=self._model_root,
        )
        det_size = int(os.getenv(DET_SIZE_ENV, str(DEFAULT_DET_SIZE)))
        use_cuda = CUDA_EXECUTION_PROVIDER in provider_chain(FACE_PROVIDERS_ENV)
        analysis.prepare(ctx_id=0 if use_cuda else -1, det_size=(det_size, det_size))
        # 逐个模块读实际链路。``FaceAnalysis`` 自己**没有** ``get_providers``——provider 是
        # 传给每个模型各自的 ``InferenceSession`` 的，退化也发生在那一个个 session 上，所以
        # 判据只能逐个模块读；读这个对象的任何属性都测不出退化。
        for taskname, model in analysis.models.items():
            session = getattr(model, "session", None)
            if session is None:
                raise RuntimeError(f"buffalo_l 的 {taskname} 模块没有建出 ONNX session")
            require_requested_providers(session, FACE_PROVIDERS_ENV, label=f"buffalo_l {taskname}")
        return analysis

    def _load_buffalo_recognition(self) -> Any:
        """为「SCRFD 在放大子图上也没找到脸」那一支单独建一个识别 session。

        1.0 ``model_clients.py:1690-1716``。**权重缺失返回 ``None``、不抛**——那一支本来
        就是兜底，没有它整条链路仍按「不确定但留证据」走（只是那一支会被整条丢掉，
        因为它的向量正来自这个 session）。
        """
        try:
            import onnxruntime

            if not Path(self.recognition_path).is_file():
                return None
            providers = face_onnx_providers(FACE_PROVIDERS_ENV)
            # sess_options 带线程上限：onnxruntime 的线程数只能在这里设，环境变量无效。
            session = onnxruntime.InferenceSession(
                self.recognition_path, providers=providers, sess_options=ort_session_options()
            )
            require_requested_providers(session, FACE_PROVIDERS_ENV, label="buffalo_l w600k_r50")
            return session
        except CudaProviderMissingError:
            # 下面那个 ``return None`` 是「权重缺失」的合法降级。退化不是——把它折进 None，
            # 上层看到的是「这条兜底路没有向量」，与真实原因无关。
            raise
        except Exception:
            return None

    def _recognition_embed(self, crop: Any) -> list[float]:
        """一张对齐好的脸 → 512 维向量。任何失败都返回空表。

        1.0 ``model_clients.py:1704-1716``。**空表是「没有可用向量」，不是「和谁都不像」**：
        调用方按前者把这张脸整个丢掉，按后者会把它当成一个与所有簇相似度为 0 的样本——
        那会给它单开一个簇，而它其实连一张可比的图都没有。
        """
        try:
            import numpy as np

            crop = crop.resize((RECOGNITION_SIZE, RECOGNITION_SIZE))
            image = np.asarray(crop.convert("RGB"), dtype=np.float32)
            blob = ((image - RECOGNITION_MEAN) / RECOGNITION_SCALE).transpose(2, 0, 1)[None, ...]
            with self._recognition_lock:
                output = self._recognition_session.run(
                    None, {self._recognition_session.get_inputs()[0].name: blob}
                )[0]
            return [float(value) for value in output[0]]
        except Exception:
            return []

    # ---- 复核与落形 --------------------------------------------------------------

    def _detect_unlocked(self, image: Any) -> list[FaceDetection]:
        self._ensure_loaded()
        if self._app is None:
            return []
        image_height, image_width = image.shape[:2]
        min_size = int(os.getenv(MIN_SIZE_ENV, str(DEFAULT_MIN_SIZE)))
        verified_score = float(os.getenv(VERIFIED_SCORE_ENV, str(DEFAULT_VERIFIED_SCORE)))
        verified_min_area = float(os.getenv(VERIFIED_MIN_AREA_ENV, str(DEFAULT_VERIFIED_MIN_AREA)))
        min_quality = float(os.getenv(IDENTITY_MIN_QUALITY_ENV, str(DEFAULT_IDENTITY_MIN_QUALITY)))
        results: list[FaceDetection] = []
        for candidate in self._detector.detect(image):
            bbox = [float(value) for value in candidate["bbox"]]
            width = max(0.0, bbox[2] - bbox[0])
            height = max(0.0, bbox[3] - bbox[1])
            if min(width, height) < min_size:
                continue
            sub, sub_x, sub_y = expand_crop(image, bbox)
            if sub is None:
                continue
            with self._analysis_lock:
                sub_faces = self._app.get(sub)
            area_ratio = min(1.0, (width * height) / max(1.0, image_width * image_height))
            if not sub_faces:
                face = self._uncertain(image, bbox, candidate, area_ratio)
            else:
                face = self._reviewed(
                    sub,
                    sub_x,
                    sub_y,
                    bbox,
                    sub_faces,
                    area_ratio,
                    verified_score=verified_score,
                    verified_min_area=verified_min_area,
                    min_quality=min_quality,
                )
            if face is not None:
                results.append(face)
        return results

    def _uncertain(
        self, image: Any, bbox: list[float], candidate: Any, area_ratio: float
    ) -> FaceDetection | None:
        """SCRFD 在放大子图上也没找到脸：**只留证据，不进簇**。

        1.0 ``model_clients.py:1755-1785``。这一支的向量来自独立加载的 w600k_r50，
        用的是 **RetinaFace 的关键点**对齐出来的裁剪——它的关键点几何不可靠，所以这张脸
        没有聚类资格，但它仍然是一张「这里有张脸」的证据。向量拿不到就整条丢掉：
        没有向量的脸连证据都算不上。
        """
        if self._recognition_session is None:
            return None
        try:
            crop = align_face_crop(image, bbox, candidate["landmarks"])
            raw_sharpness = _laplacian_variance(crop)
        except Exception:
            return None
        embedding = self._recognition_embed(crop)
        if not embedding:
            return None
        score = float(candidate["confidence"])
        sharpness = _normalize_sharpness(raw_sharpness)
        return FaceDetection(
            embedding=tuple(embedding),
            bbox=tuple(bbox),
            confidence=score,
            quality=compute_face_quality(score, area_ratio, sharpness, []),
            area_ratio=area_ratio,
            sharpness=sharpness,
            embedding_model=EMBEDDING_MODEL,
            embedding_version=self._model_name,
            validity=FACE_VALIDITY_UNCERTAIN,
            identity_ready=True,
            identity_eligible=False,
        )

    def _reviewed(
        self,
        sub: Any,
        sub_x: int,
        sub_y: int,
        bbox: list[float],
        sub_faces: Any,
        area_ratio: float,
        *,
        verified_score: float,
        verified_min_area: float,
        min_quality: float,
    ) -> FaceDetection:
        """SCRFD 认出了这张脸：按四项合取定它够不够格进簇。1.0 ``:1786-1840``。

        ``bbox`` 仍是 **RetinaFace 的框**（复核只改判定，不改这条候选的几何）——
        它是证据的坐标，改它等于让同一张脸在两次导入里落到不同的框上。
        """
        best = max(sub_faces, key=lambda face: float(face.det_score))
        score = float(best.det_score)
        kps = getattr(best, "kps", None)
        scrfd_bbox = [
            float(best.bbox[0]) + sub_x,
            float(best.bbox[1]) + sub_y,
            float(best.bbox[2]) + sub_x,
            float(best.bbox[3]) + sub_y,
        ]
        # 判据读的是 **SCRFD 的关键点**，不是 RetinaFace 的（见模块注释与第 59 条）。
        scrfd_landmarks = (
            [[float(point[0]) + sub_x, float(point[1]) + sub_y] for point in kps] if kps is not None else []
        )
        agreed = bbox_agreement(bbox, scrfd_bbox)
        sane = landmark_sanity(scrfd_landmarks, bbox) if scrfd_landmarks else False
        validity = (
            FACE_VALIDITY_VERIFIED
            if score >= verified_score and agreed and sane and area_ratio >= verified_min_area
            else FACE_VALIDITY_UNCERTAIN
        )
        pose = [float(value) for value in best.pose] if getattr(best, "pose", None) is not None else []
        embedding = (
            [float(value) for value in best.embedding] if getattr(best, "embedding", None) is not None else []
        )
        try:
            # 裁剪与关键点都在**子图坐标系**里（``sub_bbox`` / ``kps`` 同源），
            # 拿全图坐标的关键点去裁子图会得到一张错位的对齐图。
            landmarks = [[float(value) for value in point] for point in kps] if kps is not None else None
            raw_sharpness = _laplacian_variance(align_face_crop(sub, best.bbox, landmarks))
        except Exception:
            raw_sharpness = 0.0
        sharpness = _normalize_sharpness(raw_sharpness)
        quality = compute_face_quality(score, area_ratio, sharpness, pose)
        return FaceDetection(
            embedding=tuple(embedding),
            bbox=tuple(bbox),
            confidence=score,
            quality=quality,
            pose=tuple(pose),
            area_ratio=area_ratio,
            sharpness=sharpness,
            embedding_model=EMBEDDING_MODEL,
            embedding_version=self._model_name,
            validity=validity,
            identity_ready=bool(embedding),
            identity_eligible=validity == FACE_VALIDITY_VERIFIED and quality >= min_quality,
        )
