"""把一次观察的语义文本变成文本向量：嵌入 → 写进 semantic / episodic 两个空间。

对应 1.0 ``backend/pipeline.py:333-344``。**它是 text_ann 通道的存在前提**：
没有写进去的文本向量，`RetrieveTextAnn` 接上去只会永远返回空，而「空」与「没接」
在读数上分不开。

## 三条照抄 1.0

**一、同一条向量写三行。** 1.0 ``pipeline.py:342-344``：

    episodic / observation   metadata={"asset_id":…, "event_id":…}
    episodic / event         metadata={"observation_id":…}
    semantic / observation   metadata={"asset_id":…, "event_id":…}

`asset_id` 那一条是**召回能不能落地**的关键——ANN 命中的是 observation，
而候选要按 Asset 交付，`text_ann` 就是靠这个 metadata 映回去的（1.0
``retrieval/text_ann.py`` 的模块注释）。

**二、写在事件归并之后。** 1.0 在 ``pipeline.py:324`` 归并完事件才写向量，
注释写着「Update the same vector with its final event link **after selection**」——
先写会导致 `event_id` 恒为空，而那条 metadata 只在召回时用得到，
**错了不会有任何报错**，只是事件维度的扩展永远失效。

**三、空向量不写。** 与 ``IndexAssetVisual`` 同一条：1.0 拿到空向量照样 upsert，
于是「这条观察没有文本向量」在整条链路上不可见。

## 一处与 1.0 的差异（记在 s6-deviations）

1.0 拼的是八个字段：``caption`` / ``activity`` / ``place`` / ``ocr_text`` /
``transcript`` / ``clothing`` / ``facts`` / ``detail.visible_details``。
2.0 的 ``Observation`` 缺两个：

- ``transcript`` —— 视频/音频的转写，属于 S8（设计 §8）。**不是遗漏，是那一侧还没做**；
- ``detail.visible_details`` —— 2.0 把它提升成了结构化的 ``objects`` 元组
  （``Observation`` 的字段注释：S4 把散在 ``*_json`` 里的语义字段提升成正式成员）。
  取 ``objects`` 而不是重新解析 JSON blob，正是那次提升要解决的问题。

**所以文本内容与 1.0 不完全相同**，这会让同一张图的向量与 1.0 不可比。
本切片接受这一点：2.0 与 1.0 的向量本来就不该混用（模型与维度都不同），
可比的只有「同一版 2.0 内部写入与查询一致」。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sentrix.domain.media.models import Observation
from sentrix.domain.retrieval import scheme
from sentrix.domain.retrieval.ports import TextEmbedder, VectorRecord, VectorRepository

log = logging.getLogger("sentrix.retrieval.index_observation_text")

OBSERVATION_SOURCE = "observation"
EVENT_SOURCE = "event"


@dataclass(frozen=True, slots=True)
class TextIndexOutcome:
    """写没写成、写了几行、为什么没成。

    ``rows`` 与 ``indexed`` 分开：幂等命中时 ``indexed=True`` 而 ``rows=0``，
    这两件事在日志里必须分得开——前者是「早就有」，后者是「刚写上」。
    """

    indexed: bool
    rows: int = 0
    reason: str = ""


def compose_text(observation: Observation) -> str:
    """观察 → 一段可嵌入的文本。**字段顺序是 1.0 的顺序**（见模块注释）。

    顺序本身不影响向量质量，但它决定了「同一份数据两次跑出来的文本是否逐字相同」——
    而幂等判据（``has_vector``）判的是 model_name，不是文本；文本变了而 model_name
    没变的话，第二次跑会被当成「已经有了」跳过。所以顺序要固定，改它就是一次迁移。
    """
    facts_text = " ".join(f"{fact.subject} {fact.predicate} {fact.object}" for fact in observation.facts)
    return " ".join(
        part
        for part in (
            observation.caption,
            observation.source_text,
            observation.activity,
            observation.place,
            observation.ocr_text,
            " ".join(observation.clothing),
            facts_text,
            " ".join(observation.objects),
        )
        if part
    ).strip()


class IndexObservationText:
    """写入侧。构造参数与 ``IndexAssetVisual`` 对称：embedder + 权威仓储。"""

    def __init__(self, *, embedder: TextEmbedder, vectors: VectorRepository) -> None:
        self._embedder = embedder
        self._vectors = vectors

    def index(self, observation: Observation, event_id: str | None) -> TextIndexOutcome:
        text = compose_text(observation)
        if not text:
            # 1.0 拼完可能得到空串（一条什么都没观察到的观察）。空串不该去打一次
            # sidecar——那是一次注定拿到零向量的往返。
            return TextIndexOutcome(indexed=False, reason="empty_text")

        if self._already_indexed(observation):
            return TextIndexOutcome(indexed=True, reason="already_indexed")

        vector = self._embedder.embed_query(text)
        if not vector:
            # 到这里说明适配器已经记过 load_error 了；这里只负责不让它变成一次
            # 「导入成功但没有文本向量」的静默成功。
            return TextIndexOutcome(indexed=False, reason="embedding_unavailable")
        if len(vector) != self._embedder.dimension:
            # 维度不一致**在检索侧不报错**，只是永远算不出相似度——所以在这里挡掉。
            return TextIndexOutcome(
                indexed=False,
                reason=f"dimension_mismatch:{len(vector)}!={self._embedder.dimension}",
            )

        values = tuple(float(value) for value in vector)
        records = self._records(observation, event_id, values)
        for record in records:
            self._vectors.upsert(record)
        # ``event_id`` 为 ``None`` 时少写一行。**1.0 里这条路走不到**——它总是先把观察
        # 归并进一个事件再写向量（``pipeline.py:324``）。2.0 的事件归并是可选的协作者，
        # 所以这里留一条退路：没有事件就只写观察那两行，语义空间照样可检索。
        return TextIndexOutcome(indexed=True, rows=len(records), reason="" if event_id else "no_event")

    def _already_indexed(self, observation: Observation) -> bool:
        """幂等：``semantic/observation`` 那一行在就算写过。

        **只查一行**：三行是同一次调用写的，任何一行在就说明整批写过。
        三行分别查会让判定依赖写入顺序，而写入顺序不是契约。
        """
        return self._vectors.has_vector(
            space=scheme.SEMANTIC_SPACE,
            source_type=OBSERVATION_SOURCE,
            source_id=observation.id,
            model_name=self._embedder.model_id,
        )

    def _records(
        self, observation: Observation, event_id: str | None, vector: tuple[float, ...]
    ) -> tuple[VectorRecord, ...]:
        # 字段逐个写出来而不用 ``**common``：字典展开会让 mypy 把值推成 ``object``，
        # 于是 ``VectorRecord`` 的每个字段都成了 arg-type 错误——而那条错误指向的是
        # 展开处，不是真正写错的那个字段名。
        def record(space: str, source_type: str, source_id: str, metadata: dict[str, str]) -> VectorRecord:
            return VectorRecord(
                scope_id=observation.scope_id,
                space=space,
                source_type=source_type,
                source_id=source_id,
                model_name=self._embedder.model_id,
                vector=vector,
                metadata=metadata,
            )

        observation_meta = {"asset_id": observation.asset_id, "event_id": event_id or ""}
        records = [
            record(scheme.EPISODIC_SPACE, OBSERVATION_SOURCE, observation.id, dict(observation_meta)),
            record(scheme.SEMANTIC_SPACE, OBSERVATION_SOURCE, observation.id, dict(observation_meta)),
        ]
        if event_id:
            # 事件那一行不带 asset_id——1.0 只给了 observation_id，而它的用途是
            # 「按事件扩展」（adjacency 那条边，1.0 里关着）。照抄形状，不顺手加。
            records.insert(
                1,
                record(
                    scheme.EPISODIC_SPACE,
                    EVENT_SOURCE,
                    event_id,
                    {"observation_id": observation.id},
                ),
            )
        return tuple(records)
