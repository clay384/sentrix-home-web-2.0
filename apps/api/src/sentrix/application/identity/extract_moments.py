"""``moments`` 阶段：从一张照片的带编号预览里抽出每个人的瞬间。

逐行移植 1.0 ``backend/person_moments.py:125-210`` 的 ``PersonMomentExtractor.extract``。

**每人每资产一次模型调用，不是每张脸一次。** 先把选中的脸按 ``asset_id`` 分组，一张
照片渲染一次预览、调一次模型，回来的 ``moments`` 数组里每一项用 ``label``（``P1`` /
``P2``）指回是哪个人。所以「一屋子五个人」是**一次**调用，模型在一张图上同时看几个人
之间的互动——那正是这个功能要的：``interaction_labels`` 描述的是**人与人之间**发生了
什么，逐个裁出来单独问是问不出来的（那正是 ``ObserveAppearance`` 的做法，两件事）。

**输入是 ``select`` 阶段的产物**（``domain/identity/insights.select_representatives``
的返回）。1.0 只从每项里读六个 id，本模块同样，外加那包 ``selection_json``（见下）。

**两处与 1.0 不同，都记在 ``s5-deviations.md``**：

1. ``selection`` 这一包**真的落库了**。1.0 的 ``select`` 阶段算了
   ``item["selection_json"]``（``person_insights.py:103``），而写入端读的是
   ``data.get("selection")``（``db.py:6204``）——**键名对不上，那包数据从来没进过库**。
   结构上可以确认：``person_moments.py`` 全文没往 payload 里放过 ``selection`` 键。
   真实库里也无从对拍（``data/sentrix.db`` 的 ``person_moments`` 是 0 行——这个功能在
   1.0 生产上从没跑过）。判据是「select 阶段专门算了它、表里有这一列、写入端在读它，
   唯一断的是键名」，所以按证据接上。
2. **仓储的错误不吞**（同 T85d 的理由）：媒体与模型那几步的失败逐张跳过、整批继续；
   ``MomentRepository.upsert`` 抛出来的照常往上走。1.0 那个 ``except Exception`` 会把
   跨空间守卫一起吞掉，而那条守卫挡的正是「一次瞬间牵扯两个记忆空间」——那意味着上游
   归属算错了。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sentrix.domain.identity.moments import PERSON_MOMENT_PROMPT_VERSION
from sentrix.domain.identity.ports import (
    EntityRepository,
    FaceRepository,
    MomentRepository,
    PersonPreviewRenderer,
    PersonVision,
)
from sentrix.domain.media.ports import AssetRepository

# 1.0 ``person_moments.py:185`` 的 ``getattr(self.gamma, "model", "unknown")`` 兜底。
UNKNOWN_MODEL = "unknown"


@dataclass(frozen=True, slots=True)
class MomentExtraction:
    """这一趟的结果。``failures`` 数的是**照片**，不是瞬间。

    1.0 把它累进 ``person_insight_runs.stats["moment_failures"]``（``:207``）；2.0 没有
    run 表（裁决① 的连带），所以去处由调用方 ``RunInsights`` 决定——本用例只负责数出来。
    """

    moments: int
    failures: int


class ExtractMoments:
    def __init__(
        self,
        *,
        assets: AssetRepository,
        faces: FaceRepository,
        entities: EntityRepository,
        moments: MomentRepository,
        previews: PersonPreviewRenderer,
        vision: PersonVision,
        workers: int = 1,
    ) -> None:
        self._assets = assets
        self._faces = faces
        self._entities = entities
        self._moments = moments
        self._previews = previews
        self._vision = vision
        #: 每张资产是一次**独立**的视觉调用 + 一张唯一临时预览，写库也按资产独立，
        #: 所以并发不改变结果，只把模型等待叠起来。生产由路由按 ``album_workers``
        #: （与 8100 的 slot 数对齐）传入；默认 1 保持逐张顺序，测试与不关心的调用方不变。
        self._workers = max(1, int(workers))

    def __call__(self, *, scope_id: str, selections: Sequence[Mapping[str, Any]]) -> MomentExtraction:
        by_asset: dict[str, list[Mapping[str, Any]]] = {}
        for selection in selections:
            # 不用 ``.get``：``asset_id`` 缺了是上游（select 阶段）算错了，静默归进一个
            # 空桶只会让那张照片凭空消失。1.0 也是直接下标取。
            by_asset.setdefault(str(selection["asset_id"]), []).append(selection)

        created = 0
        failures = 0
        if self._workers <= 1 or len(by_asset) <= 1:
            for asset_id, items in by_asset.items():
                try:
                    created += self._extract_asset(scope_id, asset_id, items)
                except (OSError, ValueError):
                    # 图读不了 / 预览渲染失败。都只是**这一张照片**的事，整批继续——
                    # 一个人一次可能入选十几张，一张坏图不该让其余的都不落库。
                    failures += 1
            return MomentExtraction(moments=created, failures=failures)

        # 并发只并行「每张资产」这一层；单张内部仍是一次模型调用（interaction_labels
        # 要的是一张图上多人之间的关系，不能拆成每人一次）。
        with ThreadPoolExecutor(max_workers=self._workers, thread_name_prefix="sentrix-moments") as pool:
            futures = {
                pool.submit(self._extract_asset, scope_id, asset_id, items): asset_id
                for asset_id, items in by_asset.items()
            }
            for future in as_completed(futures):
                try:
                    created += future.result()
                except (OSError, ValueError):
                    failures += 1
        return MomentExtraction(moments=created, failures=failures)

    def _extract_asset(self, scope_id: str, asset_id: str, items: Sequence[Mapping[str, Any]]) -> int:
        asset = self._assets.get(asset_id)
        if asset is None or not asset.path:
            # 1.0 ``:138`` 的原样。这里**不计 failures**：没有资产或没有路径是数据问题，
            # 与「这张照片处理失败了」不是同一件事（后者才进 failures）。
            return 0

        faces = []
        for item in items:
            instance = self._faces.get_instance(str(item["face_instance_id"]))
            faces.append({**item, "bbox": list(instance.bbox) if instance else []})

        preview_path, label_map = self._previews.render(asset.path, faces)
        try:
            result = self._vision.analyze_person_moments(
                preview_path,
                sorted(label_map),
                {
                    "asset_id": asset_id,
                    "scope_id": scope_id,
                    # 1.0 给的是库里那串原文（``asset.get("captured_at")``）；2.0 的
                    # ``Asset.captured_at`` 是 datetime，序列化成同一种 ISO 串。
                    "captured_at": asset.captured_at.isoformat() if asset.captured_at else None,
                },
            )
            return self._store(scope_id, asset_id, items, label_map, result)
        finally:
            # 预览图从不作为 Asset 或长期文件保存（``person_vision`` 的 docstring）。
            # 放在 finally：模型调用抛出去时也不能把它留在盘上。
            Path(preview_path).unlink(missing_ok=True)

    def _store(
        self,
        scope_id: str,
        asset_id: str,
        items: Sequence[Mapping[str, Any]],
        label_map: Mapping[str, Mapping[str, Any]],
        result: Mapping[str, object] | None,
    ) -> int:
        if result is None:
            # 模型不可用 / 回复不是 JSON。外观与瞬间都是增强，不是确认的前置条件。
            return 0
        raw = result.get("moments")
        if not isinstance(raw, list):
            return 0

        # ``P1`` 是哪个选项，**以 ``label_map`` 为准**（编号规则住在
        # ``render_numbered_preview`` 里，不在这里重算一遍）；原始选项按
        # ``face_instance_id`` 反查——那包 ``selection_json`` 只有原始选项上有，
        # ``label_map`` 的值只带五个 id（1.0 原样）。
        source = {str(item["face_instance_id"]): item for item in items}

        created = 0
        for moment in raw:
            if not isinstance(moment, Mapping):
                continue
            info = label_map.get(str(moment.get("label") or ""))
            # 模型编了一个没画出来的编号（``P9``），或者那一项没归属到人。
            if info is None or not info.get("person_id"):
                continue
            person_id = str(info["person_id"])
            if not self._in_scope(scope_id, person_id):
                continue
            # ``person_moments`` 的六个引用列都是 NOT NULL + 外键，缺一列就落不了库。
            # 原先这里用 ``_text`` 取值，而它把 ``None`` 给成空串——**空串不享有 NULL
            # 的外键豁免**，INSERT 抛 ``FOREIGN KEY constraint failed``，``_store`` 的
            # 调用方逐张 catch，症状于是变成「这批照片静默少了瞬间」而不是一声响。
            #
            # 最常见的一种是没聚上簇的脸：``face_instances.cluster_id`` 为空**不是坏
            # 数据**，是「这张脸还没归属到某个人」的正常状态（1.0 库里 32884 张脸有
            # 19802 张如此；2.0 导入 album3-14 后 35 张里 7 张）。该丢的是那一条瞬间。
            refs = {
                "cluster_id": info.get("cluster_id"),
                "event_id": info.get("event_id"),
                "observation_id": info.get("observation_id"),
                "face_instance_id": info.get("face_instance_id"),
            }
            if not all(refs.values()):
                continue
            origin = source.get(str(refs["face_instance_id"]), {})
            self._moments.upsert(
                person_id=person_id,
                observation_id=str(refs["observation_id"]),
                prompt_version=PERSON_MOMENT_PROMPT_VERSION,
                cluster_id=str(refs["cluster_id"]),
                event_id=str(refs["event_id"]),
                asset_id=asset_id,
                face_instance_id=str(refs["face_instance_id"]),
                action_text=str(moment.get("action_text") or ""),
                interaction_target_ids=self._targets(scope_id, label_map, moment),
                interaction_text=str(moment.get("interaction_text") or ""),
                participation_style=str(moment.get("participation_style") or ""),
                visible_affect=str(moment.get("visible_affect") or ""),
                social_role_cues=[str(cue) for cue in _list(moment.get("social_role_cues"))],
                narrative_note=str(moment.get("narrative_note") or ""),
                selection=_selection(origin),
                confidence=_confidence(moment.get("confidence")),
                model_name=self._vision.model_id or UNKNOWN_MODEL,
            )
            created += 1
        return created

    def _targets(
        self,
        scope_id: str,
        label_map: Mapping[str, Mapping[str, Any]],
        moment: Mapping[str, object],
    ) -> list[str]:
        """``interaction_labels`` 折成 ``person_id``。

        查不到的标签在这里被静默丢掉——那是 1.0 的既有行为
        （``normalize_person_moments`` 会把 JSON 里的 ``null`` 变成字符串 ``"None"``，见
        ``s5-deviations.md`` 第 128 条），落库的 ``interaction_target_ids`` 里不会有垃圾。
        **跨空间的目标也丢掉**（1.0 ``:177`` 的 ``_same_scope`` 判据）。
        """
        targets: list[str] = []
        for raw in _list(moment.get("interaction_labels")):
            target = label_map.get(str(raw))
            if target is None or not target.get("person_id"):
                continue
            person_id = str(target["person_id"])
            if self._in_scope(scope_id, person_id):
                targets.append(person_id)
        return targets

    def _in_scope(self, scope_id: str, person_id: str) -> bool:
        """1.0 ``_same_scope``（``:203``，走 ``store._scope_for("entity", ...)``）。

        这是**前置过滤**，不是对 ``upsert`` 那条跨空间守卫的替代：守卫在仓储里，
        这里只是为了不把明知跨空间的行递进去（1.0 原样）。
        """
        entity = self._entities.get(person_id)
        return entity is not None and entity.scope_id == scope_id


def _list(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def _selection(item: Mapping[str, Any]) -> Mapping[str, object] | None:
    """``select`` 阶段写在选项上的那包元数据（模块 docstring 的第 1 条差异）。"""
    value = item.get("selection_json")
    return value if isinstance(value, Mapping) else None


def _confidence(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0
