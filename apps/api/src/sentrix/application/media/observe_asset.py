"""语义观察：把一张已导入的 Asset 交给视觉观察器，把结果写回 Observation。

1.0 把这件事拆成两段提交：``_commit_fast_image``（导入即落盘 + 向量）与
``_commit_semantic_image``（VLM 语义 + 事件 + 事实 + 词法）。2.0 保留同一个分界，
只是把语义那一段做成可选的协作者：单张上传走快路径，整册导入（``ImportAlbum``）把它接上。

**缺席与模型失败分开。** 本地图片不可读或该媒体类型不适用时仍可返回 ``None``，
这里不写语义。模型的传输、预算和输出格式错误通过结构化异常上抛，流水线将资产标为
失败；不能把模型坏 JSON 当作空观察继续标 ``processed``。
"""

from __future__ import annotations

from pathlib import Path

from sentrix.domain.media.models import Asset, MediaType, Observation
from sentrix.domain.media.ports import (
    AssetObserver,
    ObservationRepository,
    ObservationSemantics,
    VlmObserver,
)


class ObserveAsset(AssetObserver):
    def __init__(self, *, observer: VlmObserver, observations: ObservationRepository) -> None:
        self._observer = observer
        self._observations = observations

    def prepare(self, asset: Asset) -> ObservationSemantics | None:
        """第一段：只打模型。**不写库**，所以它可以离开串行侧（见 ``AssetObserver``）。"""
        if asset.media_type is not MediaType.IMAGE:
            return None
        return self._observer.observe(Path(asset.path))

    def commit(self, observation: Observation, semantics: ObservationSemantics | None) -> Observation | None:
        """第二段：写回。``semantics`` 为空就什么都不写。"""
        if semantics is None:
            return None
        return self._observations.enrich(observation.id, semantics=semantics)

    def observe(self, asset: Asset, observation: Observation) -> Observation | None:
        """两段的复合。**行为与拆分前逐字一致。"""
        return self.commit(observation, self.prepare(asset))
