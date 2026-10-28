"""瞬间抽取（T86f）：把 ``select`` 阶段的选项变成 ``person_moments`` 里的行。

``ExtractMoments`` 的形态是**一张照片一次模型调用**——把选中的脸按 ``asset_id`` 分组，
一张照片渲染一次带编号预览、调一次模型，回来的 ``moments`` 里每一项用 ``label``（``P1``
/ ``P2``）指回是哪个人。所以这份用例的重心在四处：

- 分组的粒度（十一张脸一次调用、两张照片两次调用）；
- ``label`` / ``interaction_labels`` 到 ``person_id`` 的**折算**，以及折算不出来、
  或折到另一个记忆空间时的丢弃；
- 逐张失败不拖垮整批（坏图算一次 ``failures``，没有资产 / 没有路径则连失败都不算）；
- 落库的那一包 ``selection``——1.0 因为键名对不上从来没写进去过，这条用例专门钉它。

``selection_json`` 那条只能直读列：``PersonMomentRecord`` 没有这个字段、``_COLUMNS``
也不含它，仓储模块 docstring 第 2 条差异写明了「写得进、读不回」。断言它落库因此绕开
记录类型，直接查 ``person_moments.selection_json``。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from sentrix.application.identity.extract_moments import ExtractMoments
from sentrix.domain.identity.insights import SELECTION_VERSION, select_representatives
from sentrix.domain.identity.moments import PERSON_MOMENT_PROMPT_VERSION
from sentrix.infrastructure.persistence.database import Database
from sentrix.infrastructure.persistence.repositories.assets import SqliteAssetRepository
from sentrix.infrastructure.persistence.repositories.entities import SqliteEntityRepository
from sentrix.infrastructure.persistence.repositories.faces import SqliteFaceRepository
from sentrix.infrastructure.persistence.repositories.moments import SqliteMomentRepository

NOW = "2026-09-28T10:00:00+00:00"
CAPTURED = "2026-09-27T18:30:00+00:00"
HOME = "album1"
OTHER_HOME = "album2"

PERSON = "person_mom"
OTHER = "person_dad"
# 这个人住在 album2：他出现在 album1 的照片里时，会被跨空间过滤掉。
GUEST = "person_guest"

HOME_PEOPLE = ("person_mom", "person_dad", "person_kid", "person_grandma", "person_uncle")

# 空 id 的退化实体：``label_map`` 里 ``person_id`` 为空时，模型照样会为那个编号回一项，
# 那一项必须丢。这个 id 要真在库里，断言才分得出「判据挡下了它」与「下游顺手挡下了它」。
UNNAMED = ""

# 超过 9 张才看得出 ``sorted(label_map)``：单位数编号的字符串序与数值序一致，一旦到
# ``P10`` 就分叉成 P1、P10、P11、P2……用 5 张时两种顺序完全相同，那条断言就退化成
# 「标签都在」，把 ``sorted`` 删掉也照样绿。
FACE_COUNT = 11

ENTITIES = (
    (PERSON, HOME, "妈妈"),
    (OTHER, HOME, "爸爸"),
    ("person_kid", HOME, "孩子"),
    ("person_grandma", HOME, "奶奶"),
    ("person_uncle", HOME, "叔叔"),
    (GUEST, OTHER_HOME, "客人"),
)


class ScriptedVision:
    """记下每次调用，按队列回话。队列空了回 ``{"moments": []}``（没有人被归属到）。"""

    def __init__(self, replies: list[dict[str, object] | None] | None = None) -> None:
        self.model_id = "scripted-vlm"
        self.calls: list[tuple[str, list[str], dict[str, object]]] = []
        self._replies = list(replies or [])
        self._alive: list[bool] = []

    def analyze_person_moments(self, path, labels, context=None):
        # **当场**求值并存布尔值（照 test_observe_appearance 的写法）：断言时临时图已经
        # 被删掉，那时再查 Path(path).exists() 永远是 False，与「一开始就没写出来」是
        # 同一种结果，用例就恒真了。临时图必须在模型看完之后才删，所以这一刻它一定在。
        self._alive.append(Path(path).is_file())
        self.calls.append((str(path), list(labels), dict(context or {})))
        return self._replies.pop(0) if self._replies else {"moments": []}

    def analyze_person_appearance(self, path, metadata=None):
        raise AssertionError("这份用例不该碰外观")

    def write_person_portrait(self, pack):
        raise AssertionError("这份用例不该碰画像")

    def image_existed_at_call_time(self) -> list[bool]:
        return self._alive


class FakePreviewRenderer:
    """假的 ``PersonPreviewRenderer``：写一个真文件到 ``tmp_path``，按 ``faces`` 的顺序编号。

    **不真画图**（真实现要 PIL 渲染整张预览，慢且脆），但保留真实现的两条要紧行为：

    1. ``label_map`` 是归属的唯一依据，所以它逐项编号 ``P1`` / ``P2``……，每项带全
       五个 id——``ExtractMoments`` 拿它填 upsert 的六个引用列，缺一个都落不了库；
    2. 读不了图时抛 ``OSError``：真实现走 PIL，坏图那一族异常正是它。
    """

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self._n = 0
        self._lock = threading.Lock()
        self.calls: list[tuple[str, list[dict[str, Any]]]] = []

    def render(
        self, asset_path: str | Path, faces: list[dict[str, Any]]
    ) -> tuple[str, dict[str, dict[str, Any]]]:
        data = Path(asset_path).read_bytes()
        if not data.startswith(b"\xff\xd8"):
            raise OSError(f"cannot identify image file {asset_path!r}")
        self.calls.append((str(asset_path), [dict(face) for face in faces]))
        with self._lock:
            self._n += 1
            number = self._n
        preview = self.tmp_path / f"preview_{number}.jpg"
        preview.write_bytes(b"\xff\xd8\xff\xd9")
        label_map = {
            f"P{index}": {
                "face_instance_id": face["face_instance_id"],
                "person_id": face["person_id"],
                "cluster_id": face["cluster_id"],
                "observation_id": face["observation_id"],
                "event_id": face["event_id"],
            }
            for index, face in enumerate(faces, start=1)
        }
        return str(preview), label_map


class Album:
    """一个装了真照片、真人、真簇的库：造一张照片、在它上面造脸，产出选项。"""

    def __init__(self, db: Database, tmp_path: Path) -> None:
        self.db = db
        self.tmp_path = tmp_path
        self._n = 0
        self._observations: dict[str, str] = {}
        self._scopes: dict[str, str] = {}
        with db.connect() as connection, connection:
            for space in (HOME, OTHER_HOME):
                connection.execute(
                    "INSERT INTO memory_spaces (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (space, space, NOW, NOW),
                )
            for entity_id, scope_id, name in ENTITIES:
                connection.execute(
                    "INSERT INTO entities (id, scope_id, entity_type, canonical_name, status,"
                    " summary, confidence, created_at, updated_at)"
                    " VALUES (?, ?, 'person', ?, 'confirmed', '', 1.0, ?, ?)",
                    (entity_id, scope_id, name, NOW, NOW),
                )

    def _next(self) -> int:
        self._n += 1
        return self._n

    def photo(self, *, scope_id: str = HOME, broken: bool = False, captured_at: str | None = CAPTURED) -> str:
        """造一张照片（asset + observation），返回 ``asset_id``。

        ``broken`` 写一个不是图的文件——留给「坏图算一次 failures」那条用例。
        """
        n = self._next()
        asset_id, observation_id = f"asset_{n}", f"obs_{n}"
        path = self.tmp_path / f"p{n}.jpg"
        if broken:
            path.write_bytes(b"not an image at all")
        else:
            Image.new("RGB", (4, 4), (200, 120, 40)).save(path, format="JPEG")
        with self.db.connect() as connection, connection:
            connection.execute(
                "INSERT INTO assets (id, scope_id, file_name, media_type, path, created_at, updated_at)"
                " VALUES (?, ?, ?, 'image', ?, ?, ?)",
                (asset_id, scope_id, f"p{n}.jpg", str(path), NOW, NOW),
            )
            connection.execute(
                "INSERT INTO observations (id, scope_id, asset_id, source_type, captured_at,"
                " created_at, updated_at) VALUES (?, ?, ?, 'vlm', ?, ?, ?)",
                (observation_id, scope_id, asset_id, captured_at, NOW, NOW),
            )
        self._observations[asset_id] = observation_id
        self._scopes[asset_id] = scope_id
        return asset_id

    def pathless_photo(self, *, scope_id: str = HOME) -> str:
        """一行 ``path`` 为空的 asset：``Asset.path`` 空 → 跳过，且不计 failures。"""
        n = self._next()
        asset_id = f"asset_{n}"
        with self.db.connect() as connection, connection:
            connection.execute(
                "INSERT INTO assets (id, scope_id, file_name, media_type, path, created_at, updated_at)"
                " VALUES (?, ?, ?, 'image', '', ?, ?)",
                (asset_id, scope_id, f"p{n}.jpg", NOW, NOW),
            )
        return asset_id

    def entity(self, entity_id: str, *, scope_id: str = HOME, name: str = "未归属") -> None:
        """单独补一个实体——给 ``ENTITIES`` 装不下的 id 用（本文件里是那个空 id）。"""
        with self.db.connect() as connection, connection:
            connection.execute(
                "INSERT INTO entities (id, scope_id, entity_type, canonical_name, status,"
                " summary, confidence, created_at, updated_at)"
                " VALUES (?, ?, 'person', ?, 'confirmed', '', 1.0, ?, ?)",
                (entity_id, scope_id, name, NOW, NOW),
            )

    def face(self, asset_id: str, *, person_id: str = PERSON, event_id: str | None = None) -> dict[str, Any]:
        """在这张照片上造一张脸（簇 + 实例 + 提及 + 事件归属），返回一条 selection。

        ``scope_id`` 跟着照片走：簇 / 事件 / 观察 / 实例 / 资产全在这一个空间里；只有
        ``person_id`` 指向的实体可以住在别处——那正是跨空间过滤要处理的情形。
        """
        n = self._next()
        instance_id, cluster_id = f"ins_{n}", f"cluster_{n}"
        event_id = event_id or f"event_{n}"
        observation_id = self._observations[asset_id]
        scope_id = self._scopes[asset_id]
        with self.db.connect() as connection, connection:
            connection.execute(
                "INSERT INTO face_clusters (id, scope_id, status, created_at, updated_at)"
                " VALUES (?, ?, 'confirmed', ?, ?)",
                (cluster_id, scope_id, NOW, NOW),
            )
            connection.execute(
                "INSERT OR IGNORE INTO events (id, scope_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (event_id, scope_id, NOW, NOW),
            )
            connection.execute(
                "INSERT OR IGNORE INTO event_observations (event_id, observation_id, created_at)"
                " VALUES (?, ?, ?)",
                (event_id, observation_id, NOW),
            )
            connection.execute(
                "INSERT INTO face_instances (id, asset_id, observation_id, cluster_id, bbox_json,"
                " detection_confidence, quality, pose_json, pose_bucket, embedding_model,"
                " embedding_version, created_at)"
                " VALUES (?, ?, ?, ?, ?, 0.9, 0.9, '{}', 'front', 'buffalo_l', 'w600k_r50', ?)",
                (
                    instance_id,
                    asset_id,
                    observation_id,
                    cluster_id,
                    json.dumps([10.0, 20.0, 40.0, 60.0]),
                    NOW,
                ),
            )
            connection.execute(
                "INSERT INTO entity_mentions (id, entity_id, observation_id, face_instance_id,"
                " confidence, created_at) VALUES (?, ?, ?, ?, 0.9, ?)",
                (f"men_{n}", person_id, observation_id, instance_id, NOW),
            )
        return {
            "asset_id": asset_id,
            "face_instance_id": instance_id,
            "person_id": person_id,
            "cluster_id": cluster_id,
            "observation_id": observation_id,
            "event_id": event_id,
        }

    def add(self, *, scope_id: str = HOME, person_id: str = PERSON, broken: bool = False) -> dict[str, Any]:
        """一张新照片 + 上面的第一张脸。"""
        return self.face(self.photo(scope_id=scope_id, broken=broken), person_id=person_id)


def selection(asset_id: str, **overrides: Any) -> dict[str, Any]:
    """手搓一条选项，用来喂那些落在库外的 asset（不存在的 id / 空 path）。"""
    item: dict[str, Any] = {
        "asset_id": asset_id,
        "face_instance_id": f"ins_for_{asset_id}",
        "person_id": PERSON,
        "cluster_id": f"cluster_for_{asset_id}",
        "observation_id": f"obs_for_{asset_id}",
        "event_id": f"event_for_{asset_id}",
    }
    item.update(overrides)
    return item


def moment(label: str, *, interaction_labels: list[str] | None = None, **extra: object) -> dict[str, object]:
    """模型回复里的 ``moments`` 一项。``extra`` 让用例能塞进实现不该读的键。"""
    entry: dict[str, object] = {
        "label": label,
        "action_text": "在餐桌旁",
        "interaction_labels": list(interaction_labels or []),
        "interaction_text": "",
        "participation_style": "陪伴",
        "visible_affect": "微笑",
        "social_role_cues": [],
        "narrative_note": "",
        "confidence": 0.8,
    }
    entry.update(extra)
    return entry


def reply(*moments: dict[str, object]) -> dict[str, object]:
    return {"moments": list(moments)}


@pytest.fixture
def album(migrations_ready_db: Database, tmp_path: Path) -> Album:
    return Album(migrations_ready_db, tmp_path)


@pytest.fixture
def db(album: Album) -> Database:
    return album.db


@pytest.fixture
def previews(album: Album) -> FakePreviewRenderer:
    return FakePreviewRenderer(album.tmp_path)


def build(db: Database, previews: FakePreviewRenderer, vision: ScriptedVision) -> ExtractMoments:
    return ExtractMoments(
        assets=SqliteAssetRepository(db),
        faces=SqliteFaceRepository(db),
        entities=SqliteEntityRepository(db),
        moments=SqliteMomentRepository(db),
        previews=previews,
        vision=vision,
    )


def stored_selection_json(db: Database, moment_id: str) -> dict[str, object]:
    """直读 ``person_moments.selection_json``。

    ``PersonMomentRecord`` 没有这个字段、``_COLUMNS`` 也不含它——仓储模块 docstring
    第 2 条差异写明「写得进、读不回」。所以这条断言绕开 ``list_for``，直接查那一列；
    而「它到底有没有写进去」正是这条用例唯一要问的。
    """
    with db.connect() as connection:
        row = connection.execute(
            "SELECT selection_json FROM person_moments WHERE id = ?", (moment_id,)
        ).fetchone()
    return json.loads(row["selection_json"])


# ---- 调用粒度：一张照片一次 -----------------------------------------------------------


def test_one_photo_however_many_faces_is_one_model_call(album: Album, db: Database, previews) -> None:
    """一张照片上十一张脸 → 一次调用，传给模型的是 ``sorted(label_map)``。

    这是本功能存在的理由：``interaction_labels`` 描述的是人与人之间发生了什么，把每张
    脸裁出来单独问是问不出来的（那是 ``ObserveAppearance``）。逐张调用既是语义错误，
    也会把导入的模型调用次数乘以每张照片的人脸数。

    **为什么是十一张**：``label_map`` 是按 ``faces`` 的顺序插进去的，所以 ``list(...)``
    就是 ``P1``..``P11``；只有 ``sorted`` 才会给出 ``P1、P10、P11、P2、…``。九张以下
    两种顺序完全一样，把源码里那一下 ``sorted`` 删掉这条断言也照样绿——那就成了「标签
    都在」，而不是「标签是排过序的」。
    """
    asset_id = album.photo()
    selections = [
        album.face(asset_id, person_id=HOME_PEOPLE[index % len(HOME_PEOPLE)]) for index in range(FACE_COUNT)
    ]
    vision = ScriptedVision([reply(moment("P1"))])

    result = build(db, previews, vision)(scope_id=HOME, selections=selections)

    assert len(vision.calls) == 1
    assert vision.calls[0][1] == [
        "P1",
        "P10",
        "P11",
        "P2",
        "P3",
        "P4",
        "P5",
        "P6",
        "P7",
        "P8",
        "P9",
    ]
    assert result.moments == 1


def test_two_photos_are_two_calls(album: Album, db: Database, previews) -> None:
    """两张照片 → 两次调用。分组键是 ``asset_id``，不是整批。"""
    vision = ScriptedVision([reply(moment("P1")), reply(moment("P1"))])

    result = build(db, previews, vision)(scope_id=HOME, selections=[album.add(), album.add()])

    assert len(vision.calls) == 2
    assert result.moments == 2


def test_two_photos_are_extracted_concurrently_when_workers_is_two(
    album: Album, db: Database, previews
) -> None:
    """``workers>1`` 时两张资产**同时在飞**——用 Barrier 证明并发，而不是只看结果。

    顺序实现会在第一次调用里等满 10s 然后抛 ``BrokenBarrierError``（它不在
    ``OSError/ValueError`` 的吞掉名单里），用例因此红；只有真正并发的两条线程能一起
    越过 Barrier。
    """
    barrier = threading.Barrier(2, timeout=10)

    class BarrierVision(ScriptedVision):
        def analyze_person_moments(self, path, labels, context=None):
            barrier.wait()
            return super().analyze_person_moments(path, labels, context)

    vision = BarrierVision([reply(moment("P1")), reply(moment("P1"))])
    extract = ExtractMoments(
        assets=SqliteAssetRepository(db),
        faces=SqliteFaceRepository(db),
        entities=SqliteEntityRepository(db),
        moments=SqliteMomentRepository(db),
        previews=previews,
        vision=vision,
        workers=2,
    )

    result = extract(scope_id=HOME, selections=[album.add(), album.add()])

    assert len(vision.calls) == 2
    assert result.moments == 2


# ---- label 折算 -----------------------------------------------------------------------


def test_a_label_points_back_at_the_right_person(album: Album, db: Database, previews) -> None:
    """模型回的 ``P2`` 必须落到第 2 个选项的那个人身上。

    归属以 ``label_map`` 为准，不是按数组下标猜、也不是按模型回的顺序。落错人在这里
    是可见的：第 1 个选项的妈妈会多出一条不属于她的瞬间。
    """
    asset_id = album.photo()
    first = album.face(asset_id, person_id=PERSON)
    second = album.face(asset_id, person_id=OTHER)
    vision = ScriptedVision([reply(moment("P2"))])

    result = build(db, previews, vision)(scope_id=HOME, selections=[first, second])

    assert result.moments == 1
    moments = SqliteMomentRepository(db)
    assert moments.list_for(person_id=PERSON) == []
    rows = moments.list_for(person_id=OTHER)
    assert len(rows) == 1
    assert rows[0].face_instance_id == second["face_instance_id"]


def test_interaction_labels_fold_to_person_ids_and_cross_space_targets_are_dropped(
    album: Album, db: Database, previews
) -> None:
    """``interaction_labels`` 折成 ``person_id``；折到**另一个记忆空间**的目标丢掉。

    两张照片各两个人：album1 那张的 ``P2`` 是爸爸（同空间 → 保留），album1 另一张的
    ``P2`` 是住在 album2 的客人（跨空间 → 丢）。两条都在，才能证明丢的是「跨空间」
    而不是「所有互动对象」。
    """
    in_scope_asset = album.photo()
    owner_a = album.face(in_scope_asset, person_id=PERSON)
    target_a = album.face(in_scope_asset, person_id=OTHER)

    cross_asset = album.photo()
    owner_b = album.face(cross_asset, person_id=PERSON)
    target_b = album.face(cross_asset, person_id=GUEST)

    vision = ScriptedVision(
        [
            reply(moment("P1", interaction_labels=["P2"])),
            reply(moment("P1", interaction_labels=["P2"])),
        ]
    )

    result = build(db, previews, vision)(scope_id=HOME, selections=[owner_a, target_a, owner_b, target_b])

    assert result.moments == 2
    rows = {row.asset_id: row for row in SqliteMomentRepository(db).list_for(person_id=PERSON)}
    assert rows[in_scope_asset].interaction_target_ids == (OTHER,)
    assert rows[cross_asset].interaction_target_ids == ()


def test_a_label_the_model_invented_is_dropped(album: Album, db: Database, previews) -> None:
    """模型编了一个没画出来的编号（``P9``）→ 那一项丢掉，同一回复里合法的照存。

    同一回复里放一条合法的 ``P1``，是为了让「丢掉 P9」有一个真的会被写错的机会：
    少了标签校验的实现要么把它当成一个 ``person_id`` 去写（外键直接炸），要么凭空
    多出一条记录。
    """
    asset_id = album.photo()
    owner = album.face(asset_id, person_id=PERSON)
    vision = ScriptedVision([reply(moment("P9"), moment("P1"))])

    result = build(db, previews, vision)(scope_id=HOME, selections=[owner])

    assert result.moments == 1
    rows = SqliteMomentRepository(db).list_for()
    assert [row.person_id for row in rows] == [PERSON]


def test_a_label_whose_person_id_is_empty_is_dropped_while_the_others_land(
    album: Album, db: Database, previews
) -> None:
    """标签在图上、模型也回了它，但那一项 ``person_id`` 为空 → 丢掉；同回复照常落库。

    判据 ``info is None or not info.get("person_id")`` 有两半。
    ``test_a_label_the_model_invented_is_dropped``（``P9``）只钉住前半——标签根本不在
    ``label_map`` 里；把后半删掉，那些用例全绿，因为模型回 ``P1`` 时根本不经过它。

    这里钉的是后半：``P2`` 在 ``label_map`` 里，只是一个没归属到人的人。同一回复放一条
    正常的 ``P1``，是为了让「丢掉这一条」与「整条链路断了」分得开。

    空 id 的实体必须**真在库里**（``UNNAMED``）：否则 ``str("")`` 会被下游 ``_in_scope``
    顺手挡掉，后半与「查不到这个人」就没有可观察的区别，删掉后半仍然全绿。
    """
    asset_id = album.photo()
    album.entity(UNNAMED)
    named = album.face(asset_id, person_id=PERSON)
    nameless = album.face(asset_id, person_id=UNNAMED)
    vision = ScriptedVision([reply(moment("P2"), moment("P1"))])

    result = build(db, previews, vision)(scope_id=HOME, selections=[named, nameless])

    assert result.moments == 1
    rows = SqliteMomentRepository(db).list_for()
    assert [(row.person_id, row.face_instance_id) for row in rows] == [(PERSON, named["face_instance_id"])]


def test_an_unresolvable_interaction_label_is_dropped(album: Album, db: Database, previews) -> None:
    """查不到的互动标签（如字符串 ``"None"``）被丢掉，能查到的照留。

    归一化会把 JSON 里的 ``null`` 变成字符串 ``"None"``（见 ``s5-deviations.md``），
    这不是一个 ``person_id``，不该进 ``interaction_target_ids``。同一个列表里再放一个
    合法的 ``P2``，保证「丢掉」不是把整列表清空。
    """
    asset_id = album.photo()
    owner = album.face(asset_id, person_id=PERSON)
    target = album.face(asset_id, person_id=OTHER)
    vision = ScriptedVision([reply(moment("P1", interaction_labels=["None", "P2"]))])

    result = build(db, previews, vision)(scope_id=HOME, selections=[owner, target])

    assert result.moments == 1
    rows = SqliteMomentRepository(db).list_for(person_id=PERSON)
    assert len(rows) == 1
    assert rows[0].interaction_target_ids == (OTHER,)


# ---- 逐张失败：坏图算一次，缺数据不算 -------------------------------------------------


def test_a_model_that_returns_none_records_nothing_but_is_not_a_failure(
    album: Album, db: Database, previews
) -> None:
    """模型返回 ``None``（不可用 / 不是 JSON）→ 这张不落库，**但不是 failures**。

    模型不可用是增强缺失，不是「这张照片坏了」；``failures`` 数的是照片。
    """
    vision = ScriptedVision([None, reply(moment("P1"))])

    result = build(db, previews, vision)(scope_id=HOME, selections=[album.add(), album.add()])

    assert result.moments == 1
    assert result.failures == 0
    assert len(SqliteMomentRepository(db).list_for()) == 1


def test_a_broken_image_is_a_failure_and_the_batch_continues(album: Album, db: Database, previews) -> None:
    """文件在、但不是图 → 渲染抛 ``OSError`` → ``failures == 1``，同一批其余照做。"""
    broken = album.add(broken=True)
    good = album.add()
    vision = ScriptedVision([reply(moment("P1"))])

    result = build(db, previews, vision)(scope_id=HOME, selections=[broken, good])

    assert result.failures == 1
    assert result.moments == 1
    assert len(vision.calls) == 1


def test_a_missing_asset_or_an_empty_path_is_skipped_without_a_failure(
    album: Album, db: Database, previews
) -> None:
    """asset 不存在 / ``Asset.path`` 为空 → 跳过，**不计 failures**。

    那两种是数据问题（上游归属算错或资产没落盘），与「这张照片处理失败」不是同一件事。
    同批放一张好照片，证明另外两条只是被跳过、没有把整批带停。
    """
    good = album.add()
    pathless = album.pathless_photo()
    vision = ScriptedVision([reply(moment("P1"))])

    result = build(db, previews, vision)(
        scope_id=HOME,
        selections=[selection("asset_missing"), selection(pathless), good],
    )

    assert result.moments == 1
    assert result.failures == 0
    assert len(vision.calls) == 1


# ---- 落库内容 ------------------------------------------------------------------------


def test_the_prompt_version_is_the_domain_constant(album: Album, db: Database, previews) -> None:
    """``prompt_version`` 是 ``PERSON_MOMENT_PROMPT_VERSION``。

    它是 upsert 唯一键的一部分：写成一个随手字面量，改了提示词重跑时就会把两版结论
    混进同一行。
    """
    vision = ScriptedVision([reply(moment("P1"))])

    build(db, previews, vision)(scope_id=HOME, selections=[album.add()])

    rows = SqliteMomentRepository(db).list_for(person_id=PERSON)
    assert len(rows) == 1
    assert rows[0].prompt_version == PERSON_MOMENT_PROMPT_VERSION


def test_the_selection_pack_actually_lands_in_the_database(album: Album, db: Database, previews) -> None:
    """``select_representatives`` 写在选项上的 ``selection_json`` 必须传进 upsert。

    1.0 的 ``select`` 算了它、表里有这一列、写入端也在读它，断的只是键名
    （``selection_json`` vs ``selection``），所以那包数据从来没进过库。这里让
    ``select_representatives`` 真的产出它，再读回那一列对拍。
    """
    candidate = album.add()
    candidate.update({"quality": 0.9, "body_visibility": 0.8, "captured_at": CAPTURED})
    selected = select_representatives([candidate], {})
    written = selected[0]["selection_json"]
    assert written["version"] == SELECTION_VERSION

    vision = ScriptedVision([reply(moment("P1"))])
    result = build(db, previews, vision)(scope_id=HOME, selections=selected)

    assert result.moments == 1
    record = SqliteMomentRepository(db).list_for(person_id=PERSON)[0]
    assert stored_selection_json(db, record.id) == written


def test_the_model_name_comes_from_the_port_not_from_the_reply(album: Album, db: Database, previews) -> None:
    """``model_name`` 取自 ``vision.model_id``，不是模型回复里的那个键。

    让回复塞一个假模型名，落库的必须仍是端口那个——这次请求用哪个模型不该由一次
    回复的内容决定。
    """
    vision = ScriptedVision([reply(moment("P1", model="冒牌模型"))])

    build(db, previews, vision)(scope_id=HOME, selections=[album.add()])

    assert SqliteMomentRepository(db).list_for(person_id=PERSON)[0].model_name == "scripted-vlm"


def test_the_temporary_preview_is_deleted_after_the_model_sees_it(
    album: Album, db: Database, previews
) -> None:
    """临时预览图**在模型看过之后**才删，且一定删掉。

    删早了模型拿到一个不存在的路径（两边都不报错，只有那次调用会失败）；不删就把每次
    抽取都变成一次磁盘泄漏。所以调用当场记下「文件在不在」，再看调用之后它没了。
    """
    vision = ScriptedVision([reply(moment("P1"))])

    build(db, previews, vision)(scope_id=HOME, selections=[album.add()])

    assert vision.image_existed_at_call_time() == [True]
    assert not Path(vision.calls[0][0]).exists()


# ---- 引用不全的瞬间：跳过它，不拖垮整张照片 -------------------------------------------


@pytest.mark.parametrize(
    "missing",
    ["cluster_id", "event_id", "observation_id", "face_instance_id"],
)
def test_a_moment_missing_a_required_reference_is_skipped(
    album: Album, db: Database, previews, missing: str
) -> None:
    """六个引用列任一为空 → 丢掉这一条，**不抛**。

    ``person_moments`` 那六列是 NOT NULL + 外键，而 ``_text(None)`` 给出的是空串——
    空串**不享有 NULL 的外键豁免**，INSERT 直接 ``FOREIGN KEY constraint failed``；
    ``_store`` 的调用方逐张 catch，于是症状是「这批照片静默少了瞬间」，而不是一声响。

    真实数据里这**不是坏数据**：没聚上簇的脸，``face_instances.cluster_id`` 就是空的
    （1.0 库里 32884 张脸有 19802 张如此；2.0 导入 album3-14 后是 35 张里的 7 张）。
    该丢的是那一条瞬间，不是那张照片。

    两张脸、模型对两张都回了瞬间：**只有引用不全的那条消失**。若整张照片一起失败，
    这里会拿到 0 而不是 1——两个数字分得开「跳一条」与「全丢」。
    """
    asset_id = album.photo()
    whole = album.face(asset_id, person_id=PERSON)
    broken = album.face(asset_id, person_id=OTHER)
    broken[missing] = None
    vision = ScriptedVision([reply(moment("P1"), moment("P2"))])

    result = build(db, previews, vision)(scope_id=HOME, selections=[whole, broken])

    assert result.moments == 1
    assert len(SqliteMomentRepository(db).list_for(person_id=PERSON)) == 1
    assert SqliteMomentRepository(db).list_for(person_id=OTHER) == []
