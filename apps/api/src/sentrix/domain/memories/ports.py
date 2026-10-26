"""记忆域端口：事件仓储与事实仓储。domain 只声明形状，SQL 在 infrastructure。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol

from sentrix.domain.memories.causality import CausalVerdict
from sentrix.domain.memories.events import EventEntityRecord, EventParticipantRecord, EventRecord
from sentrix.domain.memories.facts import Fact
from sentrix.domain.memories.graph import (
    GraphBuildStats,
    GraphEdge,
    GraphExpansionHit,
    GraphNode,
    GraphStatus,
)


class EventRepository(Protocol):
    def create(
        self,
        *,
        event_id: str,
        scope_id: str,
        time_start: datetime | None,
        time_end: datetime | None,
        place: str,
        admin_district: str,
        activity: str,
        summary: str,
        confidence: float,
        aggregation_score: float,
        aggregation_breakdown: dict[str, object],
    ) -> EventRecord: ...

    def get(self, event_id: str) -> EventRecord | None: ...

    def list_active(self, scope_id: str, *, limit: int) -> Sequence[EventRecord]: ...

    def list_by_scope(
        self, scope_id: str | None, *, limit: int, offset: int = 0, place: str = ""
    ) -> Sequence[EventRecord]:
        """``scope_id=None`` 表示**跨全部相册**（1.0 ``db.py:1514`` 的 ``if scope_id:``）。

        空与 ``"home-default"`` 是两件事：前者是用户选了「全部相册」，后者是一个
        真叫 home-default 的空间。
        """
        ...

    def count_by_scope(self, scope_id: str | None, *, place: str = "") -> int: ...

    def apply_summary(
        self,
        event_id: str,
        *,
        title: str,
        event_type: str,
        activity: str,
        summary: str,
    ) -> EventRecord: ...

    def observation_ids(self, event_id: str) -> Sequence[str]: ...

    def events_for_observations(self, observation_ids: Sequence[str]) -> dict[str, tuple[str, ...]]:
        """这些观察各自落在哪些事件上，反着查一次。

        1.0 ``db.py:5550-5554`` 的 ``_refresh_event_participants`` 开头那段：确认一个人
        之后手里只有一串 ``observation_id``，而参与者是**事件**级的投影，所以要先按
        事件把它们分好组——一次确认通常只碰一两个事件，逐条观察各开一次事务是纯浪费。

        返回 ``{event_id: (observation_id, ...)}``，观察按 id 升序去重（同一条观察不会
        在同一个事件里出现两次），**事件本身没有固定顺序**——调用方按事件逐个写，
        顺序不影响结果。
        """
        ...

    def attach(self, event_id: str, observation_id: str) -> None: ...

    def fold_observation(
        self,
        event_id: str,
        *,
        time_start: datetime | None,
        time_end: datetime | None,
        place: str,
        admin_district: str,
        activity: str,
        summary: str,
        confidence: float,
        aggregation_score: float,
        aggregation_breakdown: dict[str, object],
    ) -> EventRecord: ...

    def merge(self, source_event_id: str, target_event_id: str) -> None: ...

    def dissolve(self, event_id: str) -> None: ...

    def list_video_scenes(self, source_asset_id: str) -> Sequence[EventRecord]:
        """某个源视频的场景事件，按场景序号升序。

        1.0 ``db.py:2427`` 的 ``list_video_scene_events``：``source_type = 'video_scene'
        AND source_asset_id = ? AND status = 'active' ORDER BY source_scene_index``。
        **它不区分 ``derived_kind``**——场景是事件，派生物是资产，两者靠
        ``(source_asset_id, source_scene_index)`` 在两边各自成键（迁移 0010 的两个唯一索引）。
        """
        ...

    def create_video_scene(
        self,
        *,
        event_id: str,
        scope_id: str,
        title: str,
        summary: str,
        place: str,
        time_start: datetime | None,
        time_end: datetime | None,
        source_asset_id: str,
        source_scene_index: int,
        source_start_sec: float,
        source_end_sec: float,
    ) -> EventRecord:
        """建一条 ``source_type='video_scene'`` 的事件，**同一 (源片, 场景序号) 幂等**。

        对应 1.0 ``db.py:2397`` 的 ``create_video_scene_event``。写成端口上的一个方法
        而不是复用 ``create`` + 一次 UPDATE：血缘那五列必须在**插入时**一起写，
        分两步的话中间那一刻的事件是一行没有来源的场景（``list_video_scenes`` 看不见它，
        而它已经占了 ``source_asset_id`` 这对键）。

        **``time_start`` / ``time_end`` 与血缘的 ``source_start_sec`` / ``source_end_sec``
        是两套坐标**：前者是墙钟时间（拍摄时间 + 偏移，给时间线与事件归并），后者是原片
        内秒数（给前端 seek）。两套都要，谁也不能替代谁。

        已存在时返回那一行**原样**（不覆盖 title/summary）：重跑同一个视频是正常操作
        （``POST /videos/{id}/reprocess``），先清场再重来的顺序在 ``ExtractVideoScenes``
        那一侧。
        """
        ...


class EventParticipantRepository(Protocol):
    """事件参与者与事件-实体两张投影表。

    **它为什么与 ``EventRepository`` 分开。** 这两张表存的不是事件本身，而是「谁出现在
    这个事件里」——写它们的是身份侧的确认动作（一个人被认出来 → 事件里补一条参与者），
    读它们的是摘要与后续的检索投影。合成一个端口会让「谁改了事件」在类型上消失，
    与 ``domain/identity/ports.py`` 把 ``FaceRepository`` / ``EntityRepository`` 拆开
    是同一条理由。

    **``rehome`` 不是可有可无的搬运。** ``event_participants.event_id`` 上有指向
    ``events(id)`` 的外键，而每次连接都 ``PRAGMA foreign_keys = ON``
    （``infrastructure/persistence/database.py``）。1.0 的 ``_merge_events``（``db.py:5530``）
    自己写过一句注释记这件事：「SQLite correctly rejects deleting an event that still has
    audit, entity, participant-memory, or feedback rows pointing at it」。所以重分段合并
    两个事件之前，必须先把源事件的两张投影行改挂到目标事件上，否则 ``merge`` 的
    ``DELETE FROM events`` 会整体失败。
    """

    def upsert_participant(
        self,
        event_id: str,
        person_id: str,
        *,
        role: str,
        evidence_ids: Sequence[str] = (),
        confidence: float = 0.0,
    ) -> EventParticipantRecord | None:
        """写一条参与者：已有同一 ``(event, person, role)`` 就合并证据、只抬置信度、``revision + 1``。

        合并而不是覆盖，是因为同一个人的两条证据（两张不同的照片）都要能被审计到。
        人不存在于 ``entities`` 时返回 ``None`` 且不写任何行——1.0
        ``upsert_event_participant`` 的第一句就是这个判据（``db.py:2487``）。
        """
        ...

    def upsert_entity(
        self,
        event_id: str,
        entity_id: str,
        relation: str,
        evidence_ids: Sequence[str],
        confidence: float = 0.0,
    ) -> None:
        """把「这个事件里有这个实体」记进 ``event_entities``。1.0 ``db.py:3287`` 的
        ``upsert_event_entity``。

        同键（``event_id`` + ``entity_id`` + ``relation``）合并证据、置信度取 ``MAX``——
        与 ``participants.py`` 的 ``rehome`` 里那段私有形态是同一套语义。那段注释写着
        「2.0 现在没有任何一处写 ``event_entities``……**等 S6 有了写入者再开口**」，
        **T127 就是那个写入者**。

        与 ``upsert_participant``（人）分开：这张表装的是**非人物实体**
        （地点 / 物件 / 氛围 / 时间）。``relation`` 是中文关系名，由抽取那一层给
        （「地点」「时间」「包含物件」「画面氛围」）。
        """
        ...

    def list_participants(self, event_id: str) -> Sequence[EventParticipantRecord]: ...

    def list_event_entities(self, event_id: str) -> Sequence[EventEntityRecord]:
        """读取事件的非人物实体投影，供记忆图等派生读侧使用。"""
        ...

    def cooccurrence_event_count(self, left_person_id: str, right_person_id: str) -> int:
        """两人**同时**以 ``visible_subject`` 出现在同一个事件里的**事件数**。

        1.0 ``db.py:2517-2522`` 的 ``SELECT DISTINCT ep.event_id``。两条边界：它跨
        **全部**事件统计（不是只看当前那一个），而 ``DISTINCT`` 让「同一个人在一个事件里
        有多行参与者记录」只算一次。共现边的 ``confidence`` 是这个数的函数
        （``min(0.9, 0.45 + 0.1 * N)``），所以它算错的后果是**所有共现边的置信度一起偏**，
        而且不报错。
        """
        ...

    def rehome(self, source_event_id: str, target_event_id: str) -> int:
        """把源事件的两张投影行搬到目标事件上（按 1.0 的合并语义），返回搬动的参与者行数。

        ``event_participants`` 上有 ``UNIQUE(event_id, person_id, role)``：直接 UPDATE 会
        在两边都有同一角色时撞唯一约束，所以按行读出来走 ``upsert_participant`` 的合并
        语义再删源行。``event_entities`` 的主键是 ``(event_id, entity_id, relation)``，
        同样处理。

        **只搬不复制**：搬完之后源行必须消失，否则外键会挡住调用方的
        ``DELETE FROM events``——那正是本方法存在的理由。
        """
        ...


class FactRepository(Protocol):
    def active_for(self, *, scope_id: str, subject: str, predicate: str) -> Fact | None: ...

    def insert(self, fact: Fact) -> Fact: ...

    def reinforce(self, fact_id: str, *, evidence_ids: tuple[str, ...], confidence: float) -> Fact: ...

    def set_status(self, fact_id: str, status: str) -> Fact: ...

    def get(self, fact_id: str) -> Fact | None: ...

    # ``scope_id`` 为 ``None`` 表示**全部相册**。1.0 的 ``list_facts(1000)``
    # 根本没有 scope 参数；2.0 一度写成必填，是在端口层面收窄了 1.0 的行为。
    # 接线时改回可选，与 ``AssetRepository.list_by_scope`` 同一口径。
    def list_facts(self, scope_id: str | None, *, limit: int) -> Sequence[Fact]: ...

    def purge_unanchored(self, scope_id: str) -> int:
        """删掉 subject 不再是本 scope 已确认人物本名的事实，返回删了几条。

        与写入门禁同一条判据（``canonical_name`` 精确相等、不看别名）：放行得进、就该
        删得掉；判据一旦漂开，事实表里会留下永远清不掉的孤儿行。
        """
        ...

    def supersede_others(self, *, scope_id: str, subject: str, predicate: str, keep_fact_id: str) -> int: ...


class MemoryGraphRepository(Protocol):
    """记忆图的派生存储与有限遍历。

    这个端口只接收已经结构化的节点/边，因此图模块不依赖关键帧抽取、VLM 观察或
    事件归并的具体实现。图是可重建的派生投影，原始事件/观察/实体表仍然是权威来源。
    ``expand_assets`` 在图不存在时返回空结果并由 ``status`` 报出原因，不能把图缺席
    伪装成「检索无结果」。
    """

    def replace_scope(
        self,
        scope_id: str,
        *,
        nodes: Sequence[GraphNode],
        edges: Sequence[GraphEdge],
        build_revision: str,
    ) -> GraphBuildStats: ...

    def expand_assets(
        self,
        seed_asset_ids: Sequence[str],
        *,
        scope_id: str,
        max_hops: int,
        limit: int,
        intent: str = "general",
        direction: str = "",
    ) -> Sequence[GraphExpansionHit]: ...

    def status(self, scope_id: str) -> GraphStatus: ...

    def quality(self, scope_id: str) -> Mapping[str, object]:
        """返回不读取 benchmark GT 的图结构审计指标。"""
        ...


class CausalJudge(Protocol):
    """对规则筛出的帧对做文本因果判定。失败必须抛异常，不能把故障当无因果边。"""

    def judge(self, prompt: str, *, expected_count: int) -> Sequence[CausalVerdict]: ...
