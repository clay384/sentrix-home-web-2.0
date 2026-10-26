"""``inspect_photo``（T123）：把一个问题问给一张图，外加目标人物的裁剪。

这个工具在 1.0 被调了 **10,721 次**，是助手最主要的一次取证手段。它做的事比另外几个
工具多一层：**它可能临时改一次图**（把目标人物裁出来放大，再连同原图一起发出去）。
所以本文件的两条主线是——

1. **失败码**：五个失败各回各的码，且**封不封口是分开的**（``file_unavailable`` 与
   ``model_unavailable`` 封 ``VISUAL_OBSERVATION``，``bad_handle`` 不封——
   换一张图还有机会）；
2. **裁剪的三条路**：定位到 → 发两张图；裁不出来 → **照发原图**、标 ``not_located``；
   没指名目标 → 一次都不裁。中间那条是最容易写错的：1.0 的裁剪块自带 except，
   裁不出来**不让整次检查失败**。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sentrix.application.assistant.tools import AssistantToolbox
from sentrix.domain.assistant.result_set import ResultSet
from sentrix.domain.identity.models import ENTITY_STATUS_CONFIRMED, FACE_VALIDITY_VERIFIED
from sentrix.domain.media.models import Asset, AssetStatus, MediaType, Observation
from sentrix.infrastructure.persistence.repositories.assets import (
    SqliteAssetRepository,
    SqliteObservationRepository,
)
from sentrix.infrastructure.persistence.repositories.entities import SqliteEntityRepository
from sentrix.infrastructure.persistence.repositories.faces import SqliteFaceRepository
from sentrix.infrastructure.persistence.repositories.result_sets import SqliteResultSetRepository

NOW = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)
SCOPE = "album-inspect"
ASSET_ID = "asset_inspect"


class RecordingInspector:
    """记下每一问；按脚本回复。``PhotoInspector`` 的结构化回答就是那包 JSON。"""

    DEFAULT: Mapping[str, object] = {"observation": "戴了一顶蓝帽子", "certainty": "supported"}

    def __init__(self, reply: Mapping[str, object] | None = None, *, silent: bool = False) -> None:
        # **三态要分开**：没给（用默认答复）、给了（用给的）、``silent=True``（模型没答，
        # 返回 ``None``）。写成 ``reply or 默认`` 会把 ``None`` 也当成没给，
        # 于是「模型不可用」那条用例永远走不到——第一版就是这个错。
        self.reply: Mapping[str, object] | None = None if silent else dict(reply or self.DEFAULT)
        self.calls: list[dict[str, object]] = []

    def inspect(
        self,
        path: str,
        question: str,
        *,
        faces: Sequence[Mapping[str, object]] = (),
        target_person: str = "",
        crop: str | None = None,
    ) -> dict[str, object] | None:
        self.calls.append(
            {
                "path": str(path),
                "question": question,
                "faces": list(faces),
                "target_person": target_person,
                "crop": crop,
            }
        )
        return dict(self.reply) if self.reply is not None else None


class RecordingCrops:
    """写一个临时件并回一个框；``boom`` 时抛 ``ValueError``（模拟退化框/裁太小）。"""

    def __init__(self, *, boom: bool = False) -> None:
        self.boom = boom
        self.calls: list[tuple[str, tuple[float, ...]]] = []
        self.made: list[str] = []

    def render(self, path: str, bbox: Sequence[float]) -> tuple[str, list[float]]:
        self.calls.append((str(path), tuple(bbox)))
        if self.boom:
            raise ValueError("too small")
        target = Path(path).parent / f"crop_{len(self.made)}.jpg"
        target.write_bytes(b"crop")
        self.made.append(str(target))
        return str(target), [1.0, 2.0, 3.0, 4.0]


class StubRetrieval:
    def execute(self, request: object) -> None:  # pragma: no cover
        raise AssertionError("本用例不该走检索")


class StubPeople:
    def lookup(self, name: str, scope_id: str) -> None:
        return None


@pytest.fixture
def bundle(migrations_ready_db, tmp_path):
    assets = SqliteAssetRepository(migrations_ready_db)
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"jpeg")
    assets.add(
        Asset(
            id=ASSET_ID,
            scope_id=SCOPE,
            file_name="photo.jpg",
            media_type=MediaType.IMAGE,
            path=str(photo),
            mime_type="image/jpeg",
            size_bytes=4,
            status=AssetStatus.PROCESSED,
            content_sha256="h",
            captured_at=NOW,
            captured_location=None,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    observations = SqliteObservationRepository(migrations_ready_db)
    observations.add(
        Observation(
            id="obs_i",
            scope_id=SCOPE,
            asset_id=ASSET_ID,
            source_type="import",
            captured_at=NOW,
            confidence=1.0,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    result_sets = SqliteResultSetRepository(migrations_ready_db)
    result_sets.add(ResultSet(id="rs_i", scope_id=SCOPE, query="海边", asset_ids=(ASSET_ID,), created_at=NOW))
    return {
        "db": migrations_ready_db,
        "assets": assets,
        "observations": observations,
        "result_sets": result_sets,
        "faces": SqliteFaceRepository(migrations_ready_db),
        "entities": SqliteEntityRepository(migrations_ready_db),
        "photo": photo,
    }


def build(bundle, inspector, crops) -> AssistantToolbox:
    return AssistantToolbox(
        retrieval=StubRetrieval(),
        assets=bundle["assets"],
        result_sets=bundle["result_sets"],
        people=StubPeople(),
        memory_facts=None,
        faces=bundle["faces"],
        observations=bundle["observations"],
        photo_inspector=inspector,
        person_crops=crops,
    )


def run(bundle, inspector, crops, **arguments):
    return build(bundle, inspector, crops).execute(
        "inspect_photo",
        {"result_set_id": "rs_i", **arguments},
        scope_id=SCOPE,
        now=NOW,
        question=str(arguments.get("question") or ""),
    )


def add_face(bundle, *, cluster_id: str | None, quality: float = 0.9) -> str:
    return (
        bundle["faces"]
        .add_instance(
            asset_id=ASSET_ID,
            observation_id="obs_i",
            cluster_id=cluster_id,
            bbox=(10.0, 20.0, 30.0, 40.0),
            embedding=[0.0] * 4,
            detection_confidence=0.95,
            quality=quality,
            pose=[0.0] * 3,
            pose_bucket_name="front",
            embedding_model="buffalo_l",
            embedding_version="1",
            embedding_quality_signal=quality,
            validity=FACE_VALIDITY_VERIFIED,
            area_ratio=0.05,
            sharpness=100.0,
        )
        .id
    )


def bind_person(bundle, *, name: str, role: str | None = None, status: str = ENTITY_STATUS_CONFIRMED):
    cluster = bundle["faces"].create_cluster([0.0] * 4, confidence=0.9, scope_id=SCOPE)
    entity = bundle["entities"].create_person(name, status=status, family_role=role, scope_id=SCOPE)
    bundle["faces"].confirm_cluster(cluster.id, entity.id)
    return cluster.id


# ---- 五条失败路，五条各自的码 -------------------------------------------------------


def test_an_unknown_result_set_is_reported_as_such(bundle):
    outcome = build(bundle, RecordingInspector(), RecordingCrops()).execute(
        "inspect_photo", {"result_set_id": "rs_nope", "question": "有帽子吗"}, scope_id=SCOPE, now=NOW
    )

    assert outcome.failure_reason == "unknown_result_set"


def test_a_result_set_from_another_scope_is_refused(bundle):
    bundle["result_sets"].add(
        ResultSet(id="rs_other", scope_id="别的相册", query="x", asset_ids=(ASSET_ID,), created_at=NOW)
    )
    outcome = build(bundle, RecordingInspector(), RecordingCrops()).execute(
        "inspect_photo", {"result_set_id": "rs_other", "question": "有帽子吗"}, scope_id=SCOPE, now=NOW
    )

    assert outcome.failure_reason == "scope_mismatch"


def test_a_handle_outside_the_result_set_is_a_bad_handle(bundle):
    outcome = run(bundle, RecordingInspector(), RecordingCrops(), asset_handle="photo_9", question="有帽子吗")

    assert outcome.failure_reason == "bad_handle"


def test_a_bad_handle_does_not_seal_the_evidence(bundle):
    """**换一张图还有机会，所以不封口。** 封口是「这一轮拿不到」的意思（见下一条），
    而 handle 打错是模型自己的参数问题——封了它会顺手把「换个 handle 再试」也堵死。"""
    outcome = run(bundle, RecordingInspector(), RecordingCrops(), asset_handle="photo_9", question="有帽子吗")

    assert outcome.blocks is None


def test_a_missing_file_seals_the_evidence(bundle):
    """**文件没了换一张也一样**——这一轮就是拿不到视觉观察，封口好让门禁别再逼它重试。"""
    bundle["assets"].update_status(ASSET_ID, AssetStatus.PROCESSED)
    with bundle["db"].connect() as connection, connection:
        connection.execute("UPDATE assets SET path = ? WHERE id = ?", ("/nope/missing.jpg", ASSET_ID))

    outcome = run(bundle, RecordingInspector(), RecordingCrops(), question="有帽子吗")

    assert outcome.failure_reason == "file_unavailable"
    assert outcome.blocks is not None


def test_a_dead_model_seals_the_evidence_and_never_raises(bundle):
    outcome = run(bundle, RecordingInspector(silent=True), RecordingCrops(), question="有帽子吗")

    assert outcome.failure_reason == "model_unavailable"
    assert outcome.blocks is not None


# ---- 默认 handle 与问题 ------------------------------------------------------------


def test_the_handle_defaults_to_the_first_preview_item(bundle):
    """1.0 的描述写着 asset_handle「可省略（默认用预览第一张）」，而它的
    ``required_inputs`` 又把它列成必填——**两者矛盾**。2.0 保留描述里那条行为。"""
    inspector = RecordingInspector()

    outcome = run(bundle, inspector, RecordingCrops(), question="有帽子吗")

    assert outcome.ok
    assert outcome.observation["asset_handle"] == "photo_1"
    assert inspector.calls[0]["path"] == str(bundle["photo"])


def test_the_question_defaults_to_the_1_0_sentence(bundle):
    """1.0 ``tools.py:2125`` 的 ``arguments.get("question") or "请描述这张照片"``。"""
    inspector = RecordingInspector()

    run(bundle, inspector, RecordingCrops(), question="")

    assert inspector.calls[0]["question"] == "请描述这张照片"


def test_the_question_reaches_the_inspector_verbatim(bundle):
    inspector = RecordingInspector()

    run(bundle, inspector, RecordingCrops(), question="他戴帽子了吗")

    assert inspector.calls[0]["question"] == "他戴帽子了吗"


# ---- 目标人物与裁剪 ----------------------------------------------------------------


def test_the_face_manifest_goes_to_the_inspector(bundle):
    """人脸清单要进提示词——模型靠 ``face_id`` 在「问题指向某个人但没给名字」时指认。"""
    cluster = bind_person(bundle, name="妈妈", role="母亲")
    add_face(bundle, cluster_id=cluster)
    inspector = RecordingInspector()

    run(bundle, inspector, RecordingCrops(), question="有帽子吗")

    faces = inspector.calls[0]["faces"]
    assert [item["face_id"] for item in faces] == ["face_1"]
    assert faces[0]["person_name"] == "妈妈"
    assert faces[0]["identity_status"] == "confirmed"


def test_a_person_named_in_the_question_becomes_the_target(bundle):
    """1.0 ``:2162-2165``：问题里提到的**已确认**人名就是裁剪目标。

    这是 ``target_person`` 最常用的那一路——模型不必显式填参数。
    """
    cluster = bind_person(bundle, name="妈妈")
    add_face(bundle, cluster_id=cluster)
    crops = RecordingCrops()
    inspector = RecordingInspector()

    outcome = run(bundle, inspector, crops, question="妈妈戴帽子了吗")

    assert inspector.calls[0]["target_person"] == "妈妈"
    assert inspector.calls[0]["crop"] is not None, "定位到了就该发两张图"
    assert outcome.observation["target_face_status"] == "located"
    assert outcome.observation["target_bbox"] == [1.0, 2.0, 3.0, 4.0]


def test_a_person_the_question_does_not_mention_is_not_the_target(bundle):
    cluster = bind_person(bundle, name="妈妈")
    add_face(bundle, cluster_id=cluster)
    inspector = RecordingInspector()

    run(bundle, inspector, RecordingCrops(), question="他戴帽子了吗")

    assert inspector.calls[0]["target_person"] == ""
    assert inspector.calls[0]["crop"] is None


def test_an_explicit_target_person_wins_over_the_question(bundle):
    """显式参数优先——1.0 的顺序是 ``arguments.get("target_person") or task_state...``。"""
    cluster = bind_person(bundle, name="妈妈")
    add_face(bundle, cluster_id=cluster)
    other = bind_person(bundle, name="爸爸")
    add_face(bundle, cluster_id=other)
    inspector = RecordingInspector()

    run(bundle, inspector, RecordingCrops(), question="妈妈戴帽子了吗", target_person="爸爸")

    assert inspector.calls[0]["target_person"] == "爸爸"


def test_a_failed_crop_still_inspects_the_whole_photo(bundle):
    """**裁不出来不让整次检查失败。** 1.0 的裁剪块自带 ``except (OSError, ValueError,
    TypeError)``，落到 ``not_located`` 之后照常把原图发出去（``:2186-2200``）。

    同时钉住：提示词里那句 ``target_instruction`` 仍然填着——1.0 判它填不填看的是
    「有没有指名目标人物」，不是「裁剪图做出来没有」（那句话说「（如有）」）。
    """
    cluster = bind_person(bundle, name="妈妈")
    add_face(bundle, cluster_id=cluster)
    inspector = RecordingInspector()

    outcome = run(bundle, inspector, RecordingCrops(boom=True), question="妈妈戴帽子了吗")

    assert outcome.ok, "裁不出来不该让整次检查失败"
    assert outcome.observation["target_face_status"] == "not_located"
    assert inspector.calls[0]["crop"] is None
    assert inspector.calls[0]["target_person"] == "妈妈", "目标人物仍然是妈妈，只是没定位到"


def test_a_named_person_who_is_not_in_this_photo_is_not_located(bundle):
    """指名了人但**这张照片里没有他**：不发裁剪件，也不报错。"""
    bind_person(bundle, name="妈妈")
    inspector = RecordingInspector()

    outcome = run(bundle, inspector, RecordingCrops(), question="x", target_person="妈妈")

    assert outcome.observation["target_face_status"] == "not_located"
    assert inspector.calls[0]["crop"] is None


def test_nobody_named_means_no_crop_at_all(bundle):
    """没指名目标 → 连裁剪都不该试（``target_status`` 是 ``not_requested``，不是
    ``not_located``——后者是「试过了没成」，两者在轨迹里要分得开）。"""
    crops = RecordingCrops()

    outcome = run(bundle, RecordingInspector(), crops, question="有帽子吗")

    assert outcome.observation["target_face_status"] == "not_requested"
    assert crops.calls == []


def test_the_temporary_crop_is_deleted(bundle):
    """临时件由调用方删（``render_person_crop`` 的契约）——不删的话每次复核都在磁盘上
    留一张图，而「每次导入/每轮对话」这个量级下它会累积。"""
    cluster = bind_person(bundle, name="妈妈")
    add_face(bundle, cluster_id=cluster)
    crops = RecordingCrops()

    run(bundle, RecordingInspector(), crops, question="妈妈戴帽子了吗")

    assert crops.made, "这条用例的前提：确实裁了一个件"
    assert not any(Path(item).exists() for item in crops.made), "裁出来的临时件必须被删掉"


# ---- 未确认同行者 ------------------------------------------------------------------


def test_unconfirmed_is_the_manifest_minus_the_bound_faces(bundle):
    """1.0 ``:2242-2248``：有清单就以清单为准（``len(face_manifest) - len(identity_rows)``）。
    清单是**这次真的检出来的脸**，比模型当时写的描述更可信。"""
    cluster = bind_person(bundle, name="妈妈")
    add_face(bundle, cluster_id=cluster)
    add_face(bundle, cluster_id=None)
    add_face(bundle, cluster_id=None)

    outcome = run(bundle, RecordingInspector(), RecordingCrops(), question="有帽子吗")

    assert outcome.observation["unconfirmed_people_count"] == 2


def test_without_a_manifest_it_falls_back_to_the_people_snapshot(bundle):
    """**一张脸也没检出来**时退回快照里「说不上名字的条目数」——1.0 的
    ``if not face_manifest:`` 那一路。不退回的话，一张检测器漏掉的脸会让这个数恒为 0，
    读起来像「这张照片里没有别人」。"""
    with bundle["db"].connect() as connection, connection:
        connection.execute(
            "UPDATE observations SET people_json = ? WHERE id = ?",
            (json.dumps([{"name": "同伴甲"}, {"name": "同伴乙"}], ensure_ascii=False), "obs_i"),
        )

    outcome = run(bundle, RecordingInspector(), RecordingCrops(), question="有帽子吗")

    assert outcome.observation["unconfirmed_people_count"] == 2


# ---- 两份投影的边界 ----------------------------------------------------------------


def test_the_answer_and_the_face_manifest_reach_the_model(bundle):
    cluster = bind_person(bundle, name="妈妈", role="母亲")
    # **质量要拉开。** 同分同置信度时顺序由第三键（随机的 face id）定，
    # 断言 face_1 是谁就会飘——第一版就是这么写的，跑出来才发现。
    add_face(bundle, cluster_id=cluster, quality=0.9)
    add_face(bundle, cluster_id=None, quality=0.5)

    outcome = run(bundle, RecordingInspector(), RecordingCrops(), question="有帽子吗")

    visible = outcome.model_visible
    assert visible["observation"] == "戴了一顶蓝帽子"
    assert visible["certainty"] == "supported"
    assert visible["asset_handle"] == "photo_1"
    assert visible["unconfirmed_people_count"] == 1
    assert visible["face_candidates"] == [
        {"face_id": "face_1", "identity_status": "confirmed", "person_name": "妈妈"},
        {"face_id": "face_2", "identity_status": "unconfirmed", "person_name": ""},
    ]


def test_the_asset_id_and_the_bbox_stay_internal(bundle):
    """``asset_id`` 与裁剪框是内部标识，不进模型可见的那一份——1.0 的
    ``_sanitize`` 白名单也是这么划的。"""
    cluster = bind_person(bundle, name="妈妈")
    add_face(bundle, cluster_id=cluster)

    outcome = run(bundle, RecordingInspector(), RecordingCrops(), question="妈妈戴帽子了吗")

    assert outcome.observation["asset_id"] == ASSET_ID
    assert "asset_id" not in outcome.model_visible
    assert "target_bbox" not in outcome.model_visible


def test_the_selected_face_id_comes_back_from_the_model(bundle):
    """模型回指的那个人脸编号要原样带出来——调用方靠它把「问题里的那个人」落到具体一张脸上。"""
    cluster = bind_person(bundle, name="妈妈")
    add_face(bundle, cluster_id=cluster)
    inspector = RecordingInspector(
        {"observation": "戴了", "certainty": "uncertain", "target_face_id": "face_1"}
    )

    outcome = run(bundle, inspector, RecordingCrops(), question="她戴帽子了吗")

    assert outcome.observation["selected_face_id"] == "face_1"
    assert outcome.model_visible["certainty"] == "uncertain"


def test_target_face_id_is_the_named_targets_face_not_the_models_pick(bundle):
    """**两个 face_id 不是一回事**（1.0 ``:2259`` 与 ``:2264`` 是两个字段）：

    - ``selected_face_id``：**模型**回的 ``target_face_id``——它说「你问的是这张脸」；
    - ``target_face_id``：**我们**按 ``target_person`` 的名字在清单里找到的那张脸。

    没指名目标人物时后者是空串，哪怕模型回了一个编号——那时它的回指是猜的，
    而我们没有依据替它认下来。
    """
    cluster = bind_person(bundle, name="妈妈")
    add_face(bundle, cluster_id=cluster)
    inspector = RecordingInspector(
        {"observation": "戴了", "certainty": "supported", "target_face_id": "face_1"}
    )

    picked_only = run(bundle, inspector, RecordingCrops(), question="她戴帽子了吗")
    named = run(bundle, inspector, RecordingCrops(), question="她戴帽子了吗", target_person="妈妈")

    assert picked_only.observation["selected_face_id"] == "face_1"
    assert picked_only.observation["target_face_id"] == "", "没指名目标，我们不该认下模型的回指"
    assert named.observation["target_face_id"] == "face_1"


def test_certainty_defaults_to_supported_when_the_model_is_silent(bundle):
    """1.0 ``:2255`` 的 ``parsed.get("certainty") or "supported"``。**两档，没有第三档。**"""
    outcome = run(bundle, RecordingInspector({"observation": "看不出来"}), RecordingCrops(), question="有吗")

    assert outcome.model_visible["certainty"] == "supported"


def test_confirms_visual_only_is_true_when_no_face_is_bound(bundle):
    """1.0 ``:2262`` 的 ``"confirms_visual_only": not bool(identity_rows)``：
    没有任何已绑定的人脸时，这次观察**只**是视觉上的，不构成身份证据。"""
    add_face(bundle, cluster_id=None)
    with_face = run(bundle, RecordingInspector(), RecordingCrops(), question="有吗")

    assert with_face.observation["confirms_visual_only"] is True


def test_confirms_visual_only_is_false_when_a_face_is_bound(bundle):
    cluster = bind_person(bundle, name="妈妈")
    add_face(bundle, cluster_id=cluster)

    outcome = run(bundle, RecordingInspector(), RecordingCrops(), question="有吗")

    assert outcome.observation["confirms_visual_only"] is False


@pytest.mark.parametrize(
    "code,status", [("provider_rejected", 429), ("output_limit", 200), ("time_limit", None)]
)
def test_operational_visual_error_reaches_tool_evidence_without_becoming_an_answer(bundle, code, status):
    from sentrix.domain.assistant.ports import ModelServiceError

    class LimitedInspector(RecordingInspector):
        def inspect(self, *args, **kwargs):
            raise ModelServiceError(
                "private provider response",
                error_code=code,
                http_status=status,
                request_policy={"max_model_len": 262144, "max_output_tokens": None},
                finish_reason="length" if code == "output_limit" else None,
            )

    outcome = run(bundle, LimitedInspector(), RecordingCrops(), asset_handle="photo_1", question="看到了什么")
    assert outcome.failure_reason == code
    assert outcome.observation["operational_issue"]["kind"] == code
    assert outcome.observation["operational_issue"]["http_status"] == status
    assert outcome.observation["operational_issue"]["request_policy"]["max_model_len"] == 262144
    assert outcome.model_visible["error"] == code
    assert "private provider response" not in str(outcome.model_visible)
