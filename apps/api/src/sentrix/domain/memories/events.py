"""事件聚合的纯规则：候选打分、归并决定。零 IO。

对应 1.0 ``backend/db.py`` 的 ``_event_candidate_score``（:2000-2080）、
``_event_pair_score``（:2192-2220）、``_event_candidates``（:1962-1978）。
数值一条没改，取的是终值。

**八分量，不是四个。** ``behaviour-baseline.md`` 的「事件」一节原文写的是
「信号：时间 / GPS 距离线性评分（2km 衰减）/ 视觉相似 / 人物」——四个。实测
``db.py:2065-2070`` 的加权和是八项：

    0.25·time + 0.25·location + 0.15·visual_place + 0.05·event_type
  + 0.20·activity + 0.05·objects + 0.20·person + 0.05·visual_boost + bonus

按四个信号的读法去实现，``visual_place`` / ``event_type`` / ``activity`` / ``objects``
这四路共 0.45 的权重会整块丢掉，``visual_boost`` 与 ``spatio_temporal_bonus``
（合计另 0.25）也会一起丢。那份基线文档已同步修正。

事件候选的两级结构保持不变：
- ``candidate_window_ok`` —— 6 小时硬窗，先筛掉时间上不可能的；
- ``score_event_candidate`` —— 剩下的按八分量打分，``>= 0.50`` 才算候选。

**GPS 的 2 公里上限只属于候选打分。** ``_event_pair_score`` 里另有一套「距离 1000 米
线性衰减」的算法，用于事件已成型之后的全局两两归并（``consolidate_events``）。那套
不移植：它比候选打分同一数据更严，两个函数对同一个距离给两个答案，而 1.0 的生产
导入路径只调候选打分（``pipeline.py`` 的 ``merge_observation_into_event``），
``consolidate_events`` 只在四个 benchmark / maintenance 脚本里出现。见
``s4-deviations.md`` §2 第 2 项。
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sentrix.domain.media.models import GpsPoint

# 1.0 ``db.py:1967``：候选只在这条时间窗内的活动事件里找。6 小时是硬窗，
# 与后面的 360 分钟线性衰减不是一个东西——窗是「完全不可能」，衰减是「越远越不像」。
CANDIDATE_WINDOW_HOURS = 6.0

# 1.0 ``db.py:1969``：``score["total"] >= 0.50``。
CANDIDATE_THRESHOLD = 0.50

# 1.0 ``db.py:2004``：时间线性衰减 360 分钟到底。
TIME_DECAY_MINUTES = 360.0
# 1.0 ``db.py:2015``：GPS 线性衰减 2 公里到底。
LOCATION_DECAY_KM = 2.0

# 1.0 ``db.py:2076``：视觉相似度 0.70 以下不给分，1.00 给满。
VISUAL_BOOST_FLOOR = 0.70
VISUAL_BOOST_CEILING = 1.00

# 1.0 ``db.py:2078``：时空几乎重合时的强先验加成。
SPATIO_TEMPORAL_BONUS = 0.20
SPATIO_TEMPORAL_AT = 0.90

# 1.0 ``db.py:2058``：语义冲突且无第三方佐证时，视觉低于此值才拆。
SPLIT_GUARD_VISUAL = 0.45

# 缺失时间时的中性分（1.0 ``db.py:2006`` 的 ``else: time_score = 0.25``）。
MISSING_TIME_SCORE = 0.25

# 1.0 的固定分量分。名字写出来是因为它们各自是一个「匹配上了给多少」的决定，
# 不是可以随手调的系数。
ACTIVITY_MATCH = 0.9
OBJECT_MATCH = 0.8
PERSON_MATCH = 0.8
EVENT_TYPE_MATCH = 0.8

WEIGHTS = {
    "time": 0.25,
    "location": 0.25,
    "visual_place": 0.15,
    "event_type": 0.05,
    "activity": 0.20,
    "objects": 0.05,
    "person": 0.20,
    "visual_boost": 0.05,
}

SPLIT_GUARD_SEMANTIC_VISUAL_CONFLICT = "semantic_visual_conflict"

# 1.0 ``db.py:5570``：人脸簇被确认之后，这个人在事件里的角色是「画面里的人」。
# 它与 ``captured_by`` 是两个角色而不是一个：同一个人可以既是画面里的人又是拍摄者，
# ``event_participants`` 的 ``UNIQUE(event_id, person_id, role)`` 正是为此留的。
PARTICIPANT_ROLE_VISIBLE_SUBJECT = "visible_subject"
# 1.0 ``db.py:5570`` 的字面量。0.75 低于用户确认的 1.0（``confirm_face_cluster`` 的家
# 庭角色事实）——「这簇脸是这个人」是用户按下的，而「这张照片里出现了这个人」是聚类
# 推断出来的，两者不是同一种确定度。
VISIBLE_SUBJECT_CONFIDENCE = 0.75


def normalise_tokens(value: object) -> frozenset[str]:
    """1.0 ``db.py:1992-1998`` 的 ``_tokens``。

    ``、`` 与 ``，`` 都当分隔符——中文标签列表在 1.0 里两种写法都出现过，只切一个
    会把 ``"花、草"`` 当成一个 token。
    """
    if isinstance(value, (list, tuple, set)):
        values: Iterable[object] = value
    else:
        values = str(value or "").lower().replace("、", " ").replace("，", " ").split()
    return frozenset(str(item).strip().lower() for item in values if str(item).strip())


def _decay(distance: float, ceiling: float) -> float:
    """线性衰减：0 给 1.0，``ceiling`` 及以上给 0.0。"""
    return max(0.0, 1.0 - min(distance, ceiling) / ceiling)


@dataclass(frozen=True, slots=True)
class EventRecord:
    """落库的事件一行。字段取自 1.0 ``events`` 表，去掉 S4 用不到的参与者/实体投影。"""

    id: str
    scope_id: str
    title: str
    event_type: str
    time_start: datetime | None
    time_end: datetime | None
    place: str
    admin_district: str
    activity: str
    summary: str
    confidence: float
    aggregation_score: float
    aggregation_breakdown: dict[str, object]
    status: str = "active"
    revision: int = 1
    # 事件的落库时间。``time_start`` 缺失时按日期分组要退回它——回退规则属于后端，
    # 否则两个前端会把同一份数据分到不同的天。
    created_at: datetime | None = None
    # 视频场景事件的血缘（迁移 0010）。普通事件这几位都是空——``source_type`` 只在
    # ``= 'video_scene'`` 时有意义。S8b 写它，S8a 读它（``GET /video-scenes/{id}``
    # 要靠 ``source_asset_id`` / ``source_scene_index`` 找出这个场景的派生物）。
    source_type: str = ""
    source_asset_id: str = ""
    source_scene_index: int | None = None
    source_start_sec: float | None = None
    source_end_sec: float | None = None


@dataclass(frozen=True, slots=True)
class EventParticipantRecord:
    """``event_participants`` 一行，带出这个人当前的名字与状态（1.0 ``list_event_participants``
    的 JOIN）。

    ``person_name`` / ``person_status`` 是**读时快照**，不是这张表的列：它们是投影，
    改一次名字不该回头重写历史参与记录。事件摘要要拿名字去拼，所以这一层就带出来，
    免得调用方再逐条回查实体。
    """

    id: str
    event_id: str
    person_id: str
    role: str
    evidence_ids: tuple[str, ...]
    confidence: float
    revision: int = 1
    person_name: str = ""
    person_status: str = ""


@dataclass(frozen=True, slots=True)
class EventEntityRecord:
    """``event_entities`` 一行：事件与非人物实体的结构化关联。

    这张投影由观察实体维护阶段写入，图构建只需要实体 id、关系、证据和置信度；
    实体的展示属性仍由 ``EntityRepository`` 统一提供。
    """

    event_id: str
    entity_id: str
    relation: str
    evidence_ids: tuple[str, ...] = ()
    confidence: float = 0.0


@dataclass(frozen=True, slots=True)
class VisualSignal:
    """候选观察与目标事件之间最强的视觉相似度。

    ``available`` 与 ``similarity`` 必须分开：1.0 用 ``(0.0, False)`` 表示「没有
    向量可比」，用 ``(x, True)`` 表示「比出来了，就是 x」。合成一个 0.0 之后
    ``split_guard`` 会把「没向量」误当成「向量很不像」。见
    ``s4-deviations.md`` §2 第 1 项。
    """

    similarity: float = 0.0
    available: bool = False

    def __post_init__(self) -> None:
        if not math.isfinite(self.similarity):
            raise ValueError(f"相似度必须是有限值: {self.similarity!r}")


@dataclass(frozen=True, slots=True)
class EventAnchor:
    """已归属某事件的一条观察的时序/地点锚点。"""

    captured_at: datetime | None = None
    location: str = ""
    gps: GpsPoint | None = None
    visual_place: str = ""
    visual_event_type: str = ""


@dataclass(frozen=True, slots=True)
class ObservationAnchor:
    """待归属观察自己的锚点 + 语义字段。"""

    captured_at: datetime | None = None
    location: str = ""
    gps: GpsPoint | None = None
    visual_place: str = ""
    visual_event_type: str = ""
    activity: str = ""
    objects: frozenset[str] = frozenset()
    people: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class EventAnchorSet:
    """候选事件的既有证据。"""

    event_id: str
    time_start: datetime | None
    activity: str
    anchors: tuple[EventAnchor, ...]
    object_sets: tuple[frozenset[str], ...] = ()
    people: frozenset[str] = frozenset()

    def locations(self) -> frozenset[str]:
        return frozenset(item.location for item in self.anchors if item.location)

    def visual_places(self) -> frozenset[str]:
        return frozenset(item.visual_place for item in self.anchors if item.visual_place)

    def event_types(self) -> frozenset[str]:
        return frozenset(item.visual_event_type for item in self.anchors if item.visual_event_type)


@dataclass(frozen=True, slots=True)
class EventCandidateScore:
    """八分量明细。``total`` 已经 clamp 到 [0, 1]，且 ``split_guard`` 命中时为 0。"""

    total: float
    time: float
    location: float
    visual_place: float
    event_type: float
    activity: float
    objects: float
    person: float
    visual_similarity: float
    visual_available: bool
    visual_boost: float
    spatio_temporal_bonus: float
    semantic_conflict: bool
    split_guard: str | None

    def is_candidate(self) -> bool:
        return self.total >= CANDIDATE_THRESHOLD

    def breakdown(self) -> dict[str, object]:
        """落库形态。1.0 把它塞进 ``aggregation_breakdown_json``，字段名保持一致。"""
        return {
            "total": self.total,
            "time": self.time,
            "location": self.location,
            "visual_place": self.visual_place,
            "event_type": self.event_type,
            "activity": self.activity,
            "objects": self.objects,
            "confirmed_people": self.person,
            "visual_similarity": self.visual_similarity,
            "visual_available": self.visual_available,
            "visual_boost": self.visual_boost,
            "spatio_temporal_bonus": self.spatio_temporal_bonus,
            "semantic_conflict": self.semantic_conflict,
            "split_guard": self.split_guard,
        }


def within_candidate_window(anchor_at: datetime | None, event_at: datetime | None) -> bool:
    """1.0 ``db.py:1967``：时间差超过 6 小时的活动事件连打分都不打。"""
    if anchor_at is None or event_at is None:
        return True
    return abs((anchor_at - event_at).total_seconds()) <= CANDIDATE_WINDOW_HOURS * 3600


def _time_score(anchor_at: datetime | None, event_at: datetime | None) -> float:
    if anchor_at and event_at:
        minutes = abs((anchor_at - event_at).total_seconds()) / 60.0
        return _decay(minutes, TIME_DECAY_MINUTES)
    return MISSING_TIME_SCORE


def _location_score(anchor: ObservationAnchor, event: EventAnchorSet) -> float:
    """GPS 优先；没有 GPS 才退回字符串地点相等。

    1.0 的分支是 ``if anchor["gps"]: ... elif anchor["location"] and anchor["location"]
    in locations: 1.0``。有 GPS 而事件侧一条 GPS 都没有时，结果是 0.0 而不是退回
    字符串比较——保持原样：那个 elif 只在**候选观察本身**没有 GPS 时才走。
    """
    if anchor.gps is not None:
        best = 0.0
        for item in event.anchors:
            if item.gps is None:
                continue
            best = max(best, _decay(anchor.gps.distance_km(item.gps), LOCATION_DECAY_KM))
        return best
    if anchor.location and anchor.location in event.locations():
        return 1.0
    return 0.0


def _activity_score(activity: str, existing: str) -> float:
    """相同 / 互为子串都算命中。1.0 注释：同一事件的措辞不同是常态，不惩罚。"""
    if activity and existing and (activity == existing or activity in existing or existing in activity):
        return ACTIVITY_MATCH
    return 0.0


def _object_score(objects: frozenset[str], event_object_sets: tuple[frozenset[str], ...]) -> float:
    if objects and any(objects & values for values in event_object_sets):
        return OBJECT_MATCH
    return 0.0


def _person_score(people: frozenset[str], event_people: frozenset[str]) -> float:
    if people and event_people and (people & event_people):
        return PERSON_MATCH
    return 0.0


def score_event_candidate(
    *,
    anchor: ObservationAnchor,
    event: EventAnchorSet,
    visual: VisualSignal,
) -> EventCandidateScore:
    """八分量打分。纯函数——视觉相似度由调用方查好传进来。"""
    activity = anchor.activity.strip().lower()
    existing_activity = event.activity.strip().lower()

    time_score = _time_score(anchor.captured_at, event.time_start)
    location_score = _location_score(anchor, event)
    visual_place_score = 1.0 if anchor.visual_place and anchor.visual_place in event.visual_places() else 0.0
    event_type_score = (
        EVENT_TYPE_MATCH
        if anchor.visual_event_type and anchor.visual_event_type in event.event_types()
        else 0.0
    )
    activity_score = _activity_score(activity, existing_activity)
    object_score = _object_score(anchor.objects, event.object_sets)
    person_score = _person_score(anchor.people, event.people)

    event_types = event.event_types()
    semantic_conflict = bool(
        activity
        and existing_activity
        and activity != existing_activity
        and anchor.visual_event_type
        and event_types
        and anchor.visual_event_type not in event_types
    )
    corroborated = bool(object_score or person_score)
    split_guard: str | None = None
    # 家人的同一场活动里，特写、合影、不同机位本来就不像——视觉差异单独不足以拆。
    # 只有语义也冲突、又没有已知人物/物件做桥时，才认为该拆成两个事件。
    if (
        semantic_conflict
        and not corroborated
        and (not visual.available or visual.similarity < SPLIT_GUARD_VISUAL)
    ):
        split_guard = SPLIT_GUARD_SEMANTIC_VISUAL_CONFLICT

    if visual.available:
        span = VISUAL_BOOST_CEILING - VISUAL_BOOST_FLOOR
        visual_boost = max(0.0, min(1.0, (visual.similarity - VISUAL_BOOST_FLOOR) / span))
    else:
        visual_boost = 0.0

    spatio_temporal_bonus = (
        SPATIO_TEMPORAL_BONUS
        if time_score >= SPATIO_TEMPORAL_AT and location_score >= SPATIO_TEMPORAL_AT
        else 0.0
    )

    total = (
        WEIGHTS["time"] * time_score
        + WEIGHTS["location"] * location_score
        + WEIGHTS["visual_place"] * visual_place_score
        + WEIGHTS["event_type"] * event_type_score
        + WEIGHTS["activity"] * activity_score
        + WEIGHTS["objects"] * object_score
        + WEIGHTS["person"] * person_score
        + WEIGHTS["visual_boost"] * visual_boost
        + spatio_temporal_bonus
    )
    if split_guard:
        total = 0.0

    return EventCandidateScore(
        total=max(0.0, min(1.0, total)),
        time=time_score,
        location=location_score,
        visual_place=visual_place_score,
        event_type=event_type_score,
        activity=activity_score,
        objects=object_score,
        person=person_score,
        visual_similarity=visual.similarity,
        visual_available=visual.available,
        visual_boost=visual_boost,
        spatio_temporal_bonus=spatio_temporal_bonus,
        semantic_conflict=semantic_conflict,
        split_guard=split_guard,
    )


def rank_candidates(
    scored: Iterable[tuple[EventAnchorSet, EventCandidateScore]],
) -> list[tuple[EventAnchorSet, EventCandidateScore]]:
    """1.0 ``db.py:1974-1975`` 的排序：分数降序 → 时间降序 → id 升序。

    后两级键不是为了好看：1.0 只按分数排，同分时顺序取决于 SQL 返回顺序，
    ``ORDER BY time_start DESC`` 一旦被换掉，同一批数据就归到不同事件里。
    """
    return sorted(
        scored,
        key=lambda item: (
            -item[1].total,
            _descending_text(item[0].time_start.isoformat() if item[0].time_start else ""),
            item[0].event_id,
        ),
    )


def _descending_text(value: str) -> str:
    """把字符串的降序变成可与其他升序键并列的键。

    纯 ASCII 取反在中文/数字混合上不可靠，所以用「逐字符取补」的等价做法：
    这里的时间只会是 ISO-8601（纯 ASCII），直接按字符取反即可。
    """
    return "".join(chr(0x10FFFF - ord(ch)) for ch in value)
