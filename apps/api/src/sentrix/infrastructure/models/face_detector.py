"""RetinaFace-R50 分块检测器，以及它那一组纯几何函数。

1.0 ``backend/face_detector.py`` 全文（181 行）+ ``model_clients.py`` 的三个纯几何
helper（``_bbox_agreement`` :1841、``_landmark_sanity`` :1878、``_expand_crop`` :1918）
的移植。之所以把三个 helper 收在这里：它们是**检测侧的几何**，而 Task 78 的适配器
（SCRFD 复核 + 向量）要用它们；放在适配器里会让「检测器自己的判据」与「复核者的判据」
分家，而 1.0 的 ``_bbox_agreement`` 正是同时读两个检测器的框。

**为什么要分块。** SCRFD-10G 在整图缩到 640 之后会漏掉家庭照里的小脸。这个检测器把图切成
1024px 的块（重叠 128），每块单独检测（小脸相对块变大），再把结果映射回原图坐标做 NMS。
粗pass没找到才走细pass（640px、重叠 256、阈值降到 0.1）——**两趟是召回率的兜底，不是
精度的**，所以细pass的阈值比粗pass低得多。

模型是 ``biubug6/Pytorch_Retinaface``（MIT），BGR + mean 104/117/123，按 priors
（``min_sizes``）+ variance 解码，**不是** InsightFace 的 SCRFD 格式——同一个仓库里两种
ONNX 人脸模型的解码方式完全不同，混了会得到一堆位置离谱的框而不是报错。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import numpy as np

from sentrix.infrastructure.models.onnx_runtime import (
    RETINAFACE_PROVIDERS_ENV,
    face_onnx_providers,
    require_requested_providers,
)
from sentrix.infrastructure.thread_limits import ort_session_options

STEPS = (8, 16, 32)
MIN_SIZES = ((16, 32), (64, 128), (256, 512))
INPUT_SIZE = (640, 640)
VARIANCES = (0.1, 0.2)

# 按 $HOME 推导，不写死某个用户名——写死会让代码换台机器就指向不存在的文件，而失败方式
# 是启动时那句 "Missing retrieval/face weights" 直接退出，很难一眼看出是路径问题。
DEFAULT_MODEL_PATH = str(Path.home() / "benchmarks" / "retinaface" / "retinaface_r50.onnx")

# 分块参数：粗pass / 细pass。数字是 1.0 实测调出来的，不是随手取的。
COARSE_TILE = (1024, 128)
FINE_TILE = (640, 256)
COARSE_THRESHOLD = 0.3
FINE_THRESHOLD = 0.1
SCORE_THRESHOLD = 0.05
NMS_THRESHOLD = 0.4


def generate_priors(input_size: tuple[int, int] = INPUT_SIZE) -> np.ndarray:
    """三个步长上的候选框中心与边长，形状 ``(N, 4)``：``[cx, cy, w, h]``。

    这是 RetinaFace 的 anchor 表，与 SCRFD 的 anchor 生成方式不同——照抄 1.0 的循环。
    """
    priors = []
    for step, min_size in zip(STEPS, MIN_SIZES, strict=True):
        height = input_size[1] // step
        width = input_size[0] // step
        for row in range(height):
            for column in range(width):
                cx = (column + 0.5) * step
                cy = (row + 0.5) * step
                for size in min_size:
                    priors.append([cx, cy, size, size])
    return np.array(priors, dtype=np.float32)


def decode_boxes(loc: np.ndarray, priors: np.ndarray) -> np.ndarray:
    """``loc`` 的偏移量按 variance 解码成角点坐标 ``[x1, y1, x2, y2]``。

    ``variance[0]`` 同时用在中心与边长上——这是 RetinaFace 的格式，照抄。
    """
    boxes = np.concatenate(
        (
            priors[:, :2] + loc[:, :2] * VARIANCES[0] * priors[:, 2:],
            priors[:, 2:] * np.exp(loc[:, 2:] * VARIANCES[0]),
        ),
        1,
    )
    boxes[:, :2] -= boxes[:, 2:] / 2
    boxes[:, 2:] += boxes[:, :2]
    return boxes


def decode_landmarks(landmarks: np.ndarray, priors: np.ndarray) -> np.ndarray:
    """5 个关键点的偏移量解码成坐标，形状 ``(N, 5, 2)``。

    用 ``variance[1]`` 而不是 ``variance[0]``——两套 variance 是模型训练时就定下的，
    用错不会报错，只会让关键点整体偏移。
    """
    parts = []
    for index in range(5):
        parts.append(priors[:, :2] + landmarks[:, index * 2 : index * 2 + 2] * VARIANCES[1] * priors[:, 2:])
    return np.concatenate(parts, 1).reshape(-1, 5, 2)


def nms(boxes: np.ndarray, threshold: float = NMS_THRESHOLD) -> np.ndarray:
    """按分数贪心抑制重叠框，返回保留的下标。

    ``boxes`` 是 ``(N, 5)``：四个角点 + 分数。分块检测必然在重叠区把同一张脸检出多次，
    这一步是让叠起来的分块不产生重复人脸。
    """
    x1, y1, x2, y2, scores = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3], boxes[:, 4]
    areas = (x2 - x1) * (y2 - y1)
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while order.size > 0:
        current = int(order[0])
        keep.append(current)
        xx1 = np.maximum(x1[current], x1[order[1:]])
        yy1 = np.maximum(y1[current], y1[order[1:]])
        xx2 = np.minimum(x2[current], x2[order[1:]])
        yy2 = np.minimum(y2[current], y2[order[1:]])
        inter = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
        union = areas[current] + areas[order[1:]] - inter
        iou = inter / np.maximum(union, 1e-9)
        order = order[1:][iou <= threshold]
    return np.array(keep, dtype=int)


def bbox_agreement(
    retina_bbox: list[float] | tuple[float, ...],
    scrfd_bbox: list[float] | tuple[float, ...],
    center_ratio: float = 1.0,
    iou_threshold: float = 0.05,
) -> bool:
    """两个检测器必须大致同意这张脸在哪儿（1.0 ``_bbox_agreement``）。

    判据是**中心**为主、IoU 只做一个很弱的下限。故意松：小脸上两个检测器给出的框大小
    差别很大（SCRFD 是在放大的子图上重跑的），严格的 IoU 会把真实的小脸全杀掉——那正是
    这个复核想保住的那一批。
    """
    rw = max(1.0, retina_bbox[2] - retina_bbox[0])
    rh = max(1.0, retina_bbox[3] - retina_bbox[1])
    rcx = (retina_bbox[0] + retina_bbox[2]) / 2.0
    rcy = (retina_bbox[1] + retina_bbox[3]) / 2.0
    scx = (scrfd_bbox[0] + scrfd_bbox[2]) / 2.0
    scy = (scrfd_bbox[1] + scrfd_bbox[3]) / 2.0
    center_dist = ((rcx - scx) ** 2 + (rcy - scy) ** 2) ** 0.5
    if center_dist > (rw**2 + rh**2) ** 0.5 * center_ratio:
        return False
    ix1 = max(retina_bbox[0], scrfd_bbox[0])
    iy1 = max(retina_bbox[1], scrfd_bbox[1])
    ix2 = min(retina_bbox[2], scrfd_bbox[2])
    iy2 = min(retina_bbox[3], scrfd_bbox[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    area_r = rw * rh
    area_s = max(1.0, scrfd_bbox[2] - scrfd_bbox[0]) * max(1.0, scrfd_bbox[3] - scrfd_bbox[1])
    iou = inter / max(1e-9, area_r + area_s - inter)
    return bool(iou >= iou_threshold)


def landmark_sanity(
    landmarks: list[list[float]] | tuple[tuple[float, ...], ...],
    bbox: list[float] | tuple[float, ...],
    min_eye_ratio: float = 0.1,
) -> bool:
    """关键点看起来像一张脸吗（1.0 ``_landmark_sanity``）。

    两条：两眼距离不能小于框宽的 ``min_eye_ratio``；至少 3 个点在框外扩 20% 的范围里。
    **框小于 64 时直接放行**——小脸上关键点本来就抖，几何判据在那里只会误杀。
    """
    if not landmarks or len(landmarks) < 5:
        return False
    bbox_w = max(1.0, bbox[2] - bbox[0])
    bbox_h = max(1.0, bbox[3] - bbox[1])
    if bbox_w < 64 or bbox_h < 64:
        return True  # 太小，几何判据不可靠，交给检测器自己的分数
    eye_dist = ((landmarks[0][0] - landmarks[1][0]) ** 2 + (landmarks[0][1] - landmarks[1][1]) ** 2) ** 0.5
    if eye_dist < bbox_w * min_eye_ratio:
        return False
    pad_x = bbox_w * 0.2
    pad_y = bbox_h * 0.2
    inside = sum(
        1
        for x, y in landmarks
        if (bbox[0] - pad_x) <= x <= (bbox[2] + pad_x) and (bbox[1] - pad_y) <= y <= (bbox[3] + pad_y)
    )
    return inside >= 3


def expand_crop(
    image: np.ndarray,
    bbox: list[float] | tuple[float, ...],
    margin: float = 0.75,
    min_side: int = 256,
) -> tuple[np.ndarray | None, int, int]:
    """按人脸框外扩出一块子图，返回 ``(子图, 左上角 x, 左上角 y)``。

    外扩 ``margin`` 倍是给 SCRFD 复核用的：贴着人脸框切会把下巴与额头切掉，SCRFD 在那种
    图上会漏检。子图最短边不足 ``min_side`` 时以**中心**放大到 ``min_side``——小脸复核
    失败的主因是分辨率，不是模型。框非法（宽或高 ≤ 0、裁剪后为空）返回
    ``(None, 0, 0)``，由调用方跳过这张脸而不是抛错。
    """
    image_height, image_width = image.shape[:2]
    x1, y1, x2, y2 = (int(round(value)) for value in bbox)
    width, height = x2 - x1, y2 - y1
    if width <= 0 or height <= 0:
        return None, 0, 0
    margin_x, margin_y = int(width * margin), int(height * margin)
    x1, y1 = x1 - margin_x, y1 - margin_y
    x2, y2 = x2 + margin_x, y2 + margin_y
    sub_w, sub_h = x2 - x1, y2 - y1
    if sub_w < min_side or sub_h < min_side:
        scale = min_side / min(sub_w, sub_h)
        center_x, center_y = (x1 + x2) / 2.0, (y1 + y2) / 2.0
        new_w, new_h = int(sub_w * scale), int(sub_h * scale)
        x1, y1 = int(center_x - new_w / 2.0), int(center_y - new_h / 2.0)
        x2, y2 = x1 + new_w, y1 + new_h
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(image_width, x2), min(image_height, y2)
    if x2 <= x1 or y2 <= y1:
        return None, 0, 0
    return image[y1:y2, x1:x2], x1, y1


class RetinaFaceTiledDetector:
    """分块 RetinaFace-R50，返回**原图坐标**的人脸。

    session 惰性建、进程内单例（容器里也只存一份）：权重 240MB，每次导入一张相册照片
    重建一次会让导入路径的时间全花在加载上。
    """

    def __init__(self, model_path: str | None = None) -> None:
        self.model_path = os.getenv("RETINAFACE_MODEL_PATH", "") or model_path or DEFAULT_MODEL_PATH
        self._session: Any = None
        self._input_name = ""
        self._lock = threading.Lock()
        self.error: str | None = None

    @property
    def available(self) -> bool:
        """权重在不在。缺权重是**允许的部署形态**（没跑人脸的机器），不是异常。"""
        return Path(self.model_path).is_file()

    def _load(self) -> None:
        if self._session is not None:
            return
        with self._lock:
            if self._session is None:
                try:
                    import onnxruntime

                    providers = face_onnx_providers(RETINAFACE_PROVIDERS_ENV)
                    # sess_options 带线程上限：onnxruntime 的线程数只能在这里设，环境变量无效。
                    session = onnxruntime.InferenceSession(
                        self.model_path, providers=providers, sess_options=ort_session_options()
                    )
                    # 请求了 CUDA 而落在 CPU 上时在这里抛。**异常原样穿出 ``_load``**：上面那个
                    # ``except`` 改写 ``error`` 之后重抛，所以调用方拿到的是同一个异常，不是
                    # 一句日志（第 111 条那道防线，见 onnx_runtime）。
                    #
                    # ``self._session`` **过完守卫才赋值**：先赋值的话，抛错之后它已经非空，
                    # 下一次 ``_load`` 在这里就 return 了——硬报错只响一次，之后静默降级。
                    require_requested_providers(session, RETINAFACE_PROVIDERS_ENV, label="RetinaFace 检测")
                    self._input_name = str(session.get_inputs()[0].name)
                    self._session = session
                except Exception as error:  # 权重缺失/损坏：记下来，由调用方决定降级
                    self.error = str(error)
                    raise

    def _detect_tile(self, image: np.ndarray) -> list[dict[str, Any]]:
        import cv2

        image_height, image_width = image.shape[:2]
        ratio = image_height / image_width
        model_ratio = INPUT_SIZE[1] / INPUT_SIZE[0]
        if ratio > model_ratio:
            new_height = INPUT_SIZE[1]
            new_width = int(new_height / ratio)
        else:
            new_width = INPUT_SIZE[0]
            new_height = int(new_width * ratio)
        scale = new_height / image_height
        resized = cv2.resize(image, (new_width, new_height))
        canvas = np.zeros((INPUT_SIZE[1], INPUT_SIZE[0], 3), dtype=np.uint8)
        canvas[:new_height, :new_width] = resized
        blob = cv2.dnn.blobFromImage(canvas, 1.0, INPUT_SIZE, (104, 117, 123), swapRB=False)
        loc, conf, landms = self._session.run(None, {self._input_name: blob})
        priors = generate_priors(INPUT_SIZE)
        boxes = decode_boxes(loc[0], priors)
        scores = conf[0][:, 1]
        keypoints = decode_landmarks(landms[0], priors)
        detections = []
        for index in np.where(scores >= SCORE_THRESHOLD)[0]:
            x1, y1, x2, y2 = boxes[index] / scale
            face_keypoints = keypoints[index] / scale  # retinaface 的顺序与 arcface 一致
            detections.append(
                {
                    "bbox": [float(x1), float(y1), float(x2), float(y2)],
                    "confidence": float(scores[index]),
                    "landmarks": [[float(x), float(y)] for x, y in face_keypoints],
                }
            )
        return detections

    def _detect_tiled(
        self, image: np.ndarray, tile: int, overlap: int, threshold: float
    ) -> list[dict[str, Any]]:
        image_height, image_width = image.shape[:2]
        step = tile - overlap
        collected: list[tuple[float, float, float, float, float, list[list[float]]]] = []
        for y in range(0, image_height, step):
            for x in range(0, image_width, step):
                patch = image[y : min(y + tile, image_height), x : min(x + tile, image_width)]
                if patch.shape[0] < 16 or patch.shape[1] < 16:
                    continue
                for face in self._detect_tile(patch):
                    if face["confidence"] < threshold:
                        continue
                    bbox = face["bbox"]
                    collected.append(
                        (
                            bbox[0] + x,
                            bbox[1] + y,
                            bbox[2] + x,
                            bbox[3] + y,
                            face["confidence"],
                            face["landmarks"],
                        )
                    )
        if not collected:
            return []
        boxes = np.array([[face[0], face[1], face[2], face[3], face[4]] for face in collected])
        keep = nms(boxes, NMS_THRESHOLD)
        return [
            {
                "bbox": [
                    float(collected[index][0]),
                    float(collected[index][1]),
                    float(collected[index][2]),
                    float(collected[index][3]),
                ],
                "confidence": float(collected[index][4]),
                "landmarks": [[float(v) for v in point] for point in collected[index][5]],
            }
            for index in keep
        ]

    def detect(self, image: np.ndarray, threshold: float = COARSE_THRESHOLD) -> list[dict[str, Any]]:
        """两趟分块：粗pass 1024px；**粗pass一张都没找到**才走细pass 640px。

        细pass的阈值是 ``0.1``，比传入的 ``threshold`` 低得多，这是故意的——走到这一步说明
        粗pass已经全军覆没，这时宁可多收几张可疑的脸（后续 SCRFD 复核会把它们筛掉），
        也不要漏掉整张照片里的人。

        **返回值是候选，不是人脸。** 两件事让这句话是字面意思而不是修辞，都是实测出来的
        （见 ``s5-deviations.md``）：

        1. 细pass 入选的框，置信度可以低到 ``0.1``（实测有一张照片给出 0.28）——所以
           调用方**不能**按粗pass的 ``0.3`` 去过滤，那会把细pass的收获全部丢掉；
        2. 低分 anchor 的偏移量可以解出**越界**的框（实测有一张照片给出 x≈9730、关键点
           为负的框）。1.0 不在这一层拦它：下游的 ``expand_crop`` 对越界框返回
           ``(None, 0, 0)``、SCRFD 复核也认不出它，两道都会把它丢掉。

        在这一层加越界裁剪会**改变与 1.0 的可比性**，所以照搬不动；这条契约写在返回值
        的说明里，由调用方（Task 78 的适配器）负责筛。
        """
        self._load()
        faces = self._detect_tiled(image, *COARSE_TILE, threshold=threshold)
        if not faces:
            faces = self._detect_tiled(image, *FINE_TILE, threshold=FINE_THRESHOLD)
        return faces
