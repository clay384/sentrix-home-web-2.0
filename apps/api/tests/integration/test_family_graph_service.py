"""家庭图谱推断的真 SQLite 用例（S7b2 · T312）。

对应 1.0 ``backend/family_graph_service.py``。**模型是假的**——这里验的是状态机、
两道文本证据闸、以及两条静默降级；真实推断质量由使用者实测（项目规则：模型与真实
功能验证由使用者负责）。

**两条静默降级是这一份里最要紧的**：没有模型、模型回了不是 JSON 的东西——两条都
返回空结果、run 照样 ``completed``。改成抛错会让「模型有点问题」变成「整个导入失败」。
"""

from __future__ import annotations

import json

import pytest

from sentrix.application.identity.family_graph_service import (
    FamilyGraphService,
    has_explicit_membership_evidence,
    has_explicit_relation_evidence,
)
from sentrix.domain.assistant.models import ModelReply
from sentrix.domain.assistant.ports import ModelServiceError
from sentrix.infrastructure.persistence.database import Database
from sentrix.infrastructure.persistence.repositories.entities import SqliteEntityRepository
from sentrix.infrastructure.persistence.repositories.events import SqliteEventRepository
from sentrix.infrastructure.persistence.repositories.family import (
    SqliteFamilyGraphRepository,
)

NOW = "2026-10-02T10:00:00+00:00"
HOME = "album1"


class FakeModel:
    """一个只回固定内容的模型。**记录调用**，因为「提示词怎么拼」也是行为。"""

    def __init__(self, content: str = "", error: Exception | None = None) -> None:
        self.content = content
        self.error = error
        # 记 **(角色, 内容)** 而不是只记内容：只记内容的话，「把提示词从 system 挪到
        # user」这个变异不会被抓到——它是按位置解包的。
        self.calls: list[tuple[tuple[str, str], ...]] = []

    def complete(self, messages, tools):  # noqa: ANN001, ANN201
        self.calls.append(tuple((str(message.role), message.content) for message in messages))
        if self.error is not None:
            raise self.error
        return ModelReply(content=self.content)


@pytest.fixture
def service(migrations_ready_db: Database):
    """一个空间 · 两个人（各一张脸、各一条观察）· 一条事件。"""
    db = migrations_ready_db
    with db.connect() as connection, connection:
        connection.execute(
            "INSERT INTO memory_spaces (id, name, include_in_people, created_at, updated_at)"
            " VALUES (?, ?, 1, ?, ?)",
            (HOME, HOME, NOW, NOW),
        )
        for index in (1, 2):
            connection.execute(
                "INSERT INTO assets (id, scope_id, file_name, media_type, path, created_at,"
                " updated_at) VALUES (?, ?, ?, 'image', ?, ?, ?)",
                (f"asset_{index}", HOME, f"a{index}.jpg", f"/tmp/a{index}.jpg", NOW, NOW),
            )
            connection.execute(
                "INSERT INTO observations (id, scope_id, asset_id, source_type, captured_at,"
                " caption, activity, created_at, updated_at)"
                " VALUES (?, ?, ?, 'vlm', ?, ?, ?, ?, ?)",
                (
                    f"obs_{index}",
                    HOME,
                    f"asset_{index}",
                    NOW,
                    "一家人在公园野餐" if index == 1 else "孩子在旁边玩",
                    "野餐",
                    NOW,
                    NOW,
                ),
            )
            connection.execute(
                "INSERT INTO entities (id, scope_id, entity_type, canonical_name, status,"
                " summary, confidence, created_at, updated_at)"
                " VALUES (?, ?, 'person', ?, 'confirmed', '', 0.0, ?, ?)",
                (f"ent_{index}", HOME, f"人物{index}", NOW, NOW),
            )
            connection.execute(
                "INSERT INTO face_clusters (id, scope_id, status, entity_id,"
                " representative_embedding_json, member_count, revision, created_at, updated_at)"
                " VALUES (?, ?, 'confirmed', ?, '[]', 1, 1, ?, ?)",
                (f"cluster_{index}", HOME, f"ent_{index}", NOW, NOW),
            )
            connection.execute(
                "INSERT INTO face_instances (id, asset_id, observation_id, cluster_id,"
                " bbox_json, embedding_json, detection_confidence, quality, pose_json,"
                " pose_bucket, embedding_model, embedding_version, embedding_quality_signal,"
                " validity, area_ratio, sharpness, created_at)"
                " VALUES (?, ?, ?, ?, '[0,0,1,1]', '[]', 0.9, 0.8, '[]', 'frontal',"
                " 'buffalo_l', 'w600k_r50', 0.5, 'verified', 0.1, 0.0, ?)",
                (f"face_{index}", f"asset_{index}", f"obs_{index}", f"cluster_{index}", NOW),
            )
        connection.execute(
            "INSERT INTO events (id, scope_id, title, event_type, place, created_at, updated_at)"
            " VALUES ('evt_1', ?, '野餐', 'outing', '公园', ?, ?)",
            (HOME, NOW, NOW),
        )
        connection.execute(
            "INSERT INTO event_observations (event_id, observation_id, created_at)"
            " VALUES ('evt_1', 'obs_1', ?)",
            (NOW,),
        )

    def build(model: FakeModel | None) -> FamilyGraphService:
        return FamilyGraphService(
            family=SqliteFamilyGraphRepository(db, SqliteEntityRepository(db)),
            events=SqliteEventRepository(db),
            model=model,
        )

    repo = SqliteFamilyGraphRepository(db, SqliteEntityRepository(db))
    return {"db": db, "repo": repo, "build": build, "run": repo.create_run(HOME, {"t": "x"})}


def _membership(repo, entity_id: str):
    return repo.get_effective_membership(HOME, entity_id)


# ---- 两条静默降级 ----------------------------------------------------------------


def test_without_a_model_the_run_still_completes_with_zero_counts(service) -> None:
    """**静默降级之一**：没有模型 → 空结果、run ``completed``、stats 全 0。

    定点变异：把它改成抛错，这条变红——而那样「模型没配好」会变成「整个导入失败」。
    注意 ``event_watermark`` **照样写入**：那是下一趟要不要跑的依据，
    与这趟问了没问模型无关。
    """
    result = service["build"](None).run(service["run"].id, HOME)

    assert result.status == "completed"
    assert result.current_stage == "done"
    assert result.stats["people"] == 2
    # **每人都会写一条归属**（默认 ``unknown``）——``memberships`` 数的是「写了几个
    # 人」，不是「模型说了几个」。所以没有模型时它照样是 2。
    assert result.stats["memberships"] == 2
    assert result.stats["relationships"] == 0
    assert {_membership(service["repo"], f"ent_{i}").membership for i in (1, 2)} == {"unknown"}
    assert result.stats["portraits"] == 2  # 画像不依赖模型（正文只用已落库文本）
    assert result.stats["event_watermark"] == 1
    assert result.stats["input_mode"] == "semantic_text_only"


def test_a_non_json_reply_is_also_silently_empty(service) -> None:
    """**静默降级之二**：模型回了人话不是 JSON → 同样空结果、照样 ``completed``。"""
    result = service["build"](FakeModel("我觉得他们是家人。")).run(service["run"].id, HOME)

    assert result.status == "completed"
    assert result.stats["memberships"] == 2  # 同上：写的是 unknown
    assert result.stats["relationships"] == 0


def test_a_model_failure_marks_the_run_failed_without_raising(service) -> None:
    """**真异常才落 ``failed``**，而且**不往外抛**——批末触发那条链不能因为模型挂了就炸。

    ``failed`` 时 stats 保持空 → ``event_watermark`` 缺失 → 下次去抖判据不成立 →
    一定重跑。这是有意的。
    """
    result = service["build"](FakeModel(error=RuntimeError("端点不可达"))).run(service["run"].id, HOME)

    assert result.status == "failed"
    assert result.error == "端点不可达"
    assert dict(result.stats) == {}


# ---- 状态机的前置校验 --------------------------------------------------------------


def test_an_unknown_run_id_raises_key_error_instead_of_failing_it(service) -> None:
    """run 不存在抛 ``KeyError``——**不能把它标成 failed**，它根本不存在。"""
    with pytest.raises(KeyError):
        service["build"](None).run("run_nope", HOME)


def test_a_running_run_raises_instead_of_starting_a_second_one(service) -> None:
    """已经在跑的 run 抛 ``RuntimeError``。1.0 ``:26-27``。"""
    service["repo"].update_run(service["run"].id, status="running")

    with pytest.raises(RuntimeError, match="run already running"):
        service["build"](None).run(service["run"].id, HOME)


# ---- 两道文本证据闸 ---------------------------------------------------------------


def test_a_membership_the_text_does_not_support_is_downgraded_to_unknown(service) -> None:
    """**闸一**：模型说 family，但描述里一个「家人/爸爸/全家」都没有 → 落 ``unknown``。

    夹具的描述是「一家人在公园野餐」——含「家人」，所以**这条会过闸**。所以这里改用
    ``friend``：描述里没有「朋友/同学/闺蜜/好友/同事」，于是降级。

    定点变异：去掉那道闸，``friend`` 会被原样落库。
    """
    model = FakeModel(
        json.dumps({"memberships": [{"person_id": "ent_1", "membership": "friend", "confidence": 0.9}]})
    )

    service["build"](model).run(service["run"].id, HOME)

    assert _membership(service["repo"], "ent_1").membership == "unknown"


def test_a_membership_the_text_supports_is_kept(service) -> None:
    """对照组：描述里有「家人」，``family`` 落库。"""
    model = FakeModel(
        json.dumps({"memberships": [{"person_id": "ent_1", "membership": "family", "confidence": 0.9}]})
    )

    service["build"](model).run(service["run"].id, HOME)

    assert _membership(service["repo"], "ent_1").membership == "family"


def test_a_relationship_the_text_does_not_support_is_not_written(service) -> None:
    """**闸二**：谓词是 ``父亲``，而两人的描述里没有「父亲/爸爸/父女/父子」→ 不落边。

    定点变异：去掉这道闸，模型说建就建——而「同框一次就是父子」正是提示词第一条
    要挡的东西。
    """
    model = FakeModel(
        json.dumps(
            {
                "relationships": [
                    {
                        "subject_entity_id": "ent_1",
                        "predicate": "父亲",
                        "object_entity_id": "ent_2",
                        "inverse_predicate": "女儿",
                        "confidence": 0.9,
                    }
                ]
            }
        )
    )

    service["build"](model).run(service["run"].id, HOME)

    assert service["repo"].list_effective_relationships(HOME) == []


def test_a_supported_relationship_is_written_and_forces_both_ends_to_family(service) -> None:
    """正道 + **边落库后两端强制升 ``family``**（1.0 ``:122-128``）。

    那一句**绕过**闸一：描述里没有「家人」的 ``ent_2`` 也会被写成 ``family``——
    因为「一旦认定两人有亲属关系，他们两个就是家里人」。
    """
    model = FakeModel(
        json.dumps(
            {
                "relationships": [
                    {
                        "subject_entity_id": "ent_1",
                        "predicate": "父亲",
                        "object_entity_id": "ent_2",
                        "inverse_predicate": "女儿",
                        "confidence": 0.9,
                    }
                ]
            }
        )
    )

    # 描述里加「爸爸」，让闸二放行。
    with service["db"].connect() as connection, connection:
        connection.execute("UPDATE observations SET caption = '爸爸带着女儿在公园' WHERE id = 'obs_1'")

    result = service["build"](model).run(service["run"].id, HOME)

    assert result.stats["relationships"] == 1
    edges = service["repo"].list_effective_relationships(HOME)
    assert {item.predicate for item in edges} == {"父亲", "女儿"}
    assert all(item.source == "model" for item in edges)
    # 两端都被强制成 family。
    assert _membership(service["repo"], "ent_1").membership == "family"
    assert _membership(service["repo"], "ent_2").membership == "family"


def test_a_relationship_naming_someone_outside_the_evidence_is_dropped(service) -> None:
    """结构闸：两端都必须**在 evidence 里**。编造一个不存在的 id，那条边丢掉。"""
    model = FakeModel(
        json.dumps(
            {
                "relationships": [
                    {
                        "subject_entity_id": "ent_1",
                        "predicate": "父亲",
                        "object_entity_id": "ent_不存在",
                        "inverse_predicate": "女儿",
                        "confidence": 0.9,
                    }
                ]
            }
        )
    )

    result = service["build"](model).run(service["run"].id, HOME)

    assert result.stats["relationships"] == 0


# ---- 清场与画像 -------------------------------------------------------------------


def test_a_model_run_supersedes_the_previous_model_edges_first(service) -> None:
    """**先清场再写**：上一轮模型写的边在这一轮开始前就被置 superseded。

    夹具手工写一条 model 边（`set_relationship` 一次落两行），跑一趟空结果之后
    那两行都不该还是 ``effective``。
    """
    service["repo"].set_relationship(HOME, "ent_1", "朋友", "ent_2", "朋友", source="model")
    assert len(service["repo"].list_effective_relationships(HOME)) == 2

    service["build"](None).run(service["run"].id, HOME)

    assert service["repo"].list_effective_relationships(HOME) == []


def test_a_user_edge_survives_the_clearing(service) -> None:
    """**用户写的边一条都不动**——那是清场那三重过滤的意义。"""
    service["repo"].set_relationship(HOME, "ent_1", "朋友", "ent_2", "朋友", source="user_override")

    service["build"](None).run(service["run"].id, HOME)

    assert len(service["repo"].list_effective_relationships(HOME)) == 2


def test_a_portrait_is_written_from_the_stored_text_alone(service) -> None:
    """画像正文三段：家庭归属 / 关系 / 近期记忆。**没有模型也会写**。"""
    service["build"](None).run(service["run"].id, HOME)

    portrait = service["repo"].get_active_portrait(HOME, "ent_1")

    assert portrait is not None
    assert portrait.portrait_text.startswith("家庭归属：")
    assert "近期记忆：一家人在公园野餐；野餐" in portrait.portrait_text
    assert portrait.source == "model"


# ---- 提示词与截断 -----------------------------------------------------------------


def test_the_prompt_is_sent_as_a_system_message_and_the_json_as_the_user_one(service) -> None:
    """2.0 的 ``OpenAIChatModel`` 硬性要求 ``messages[0]`` 是 system 且只能有一条。

    1.0 把提示词与 JSON **拼成一条** user 消息；2.0 拆成 system + user——同一段文字、
    不同的角色标记，是适配不是选择。
    """
    model = FakeModel("{}")

    service["build"](model).run(service["run"].id, HOME)

    (system_role, system), (user_role, user) = model.calls[0]
    assert system_role == "system"
    assert user_role == "user"
    assert system.startswith("你是家庭相册关系推断器")
    assert json.loads(user)["input_mode"] == "semantic_text_only"
    assert {item["person_id"] for item in json.loads(user)["people"]} == {"ent_1", "ent_2"}


def test_the_cooccurrence_list_keeps_only_pairs_inside_the_top_people(service) -> None:
    """共现**只保留两端都在 top-N 之内的对**——模型看到的人与看到的共现是同一批。"""
    model = FakeModel("{}")

    service["build"](model).run(service["run"].id, HOME)

    payload = json.loads(model.calls[0][1][1])
    assert payload["cooccurrences"] == []  # 两人没有出现在同一条观察里


# ---- 两道闸是纯函数，单独钉它们的边界 -----------------------------------------------


def test_the_gates_are_substring_matches_over_all_descriptions() -> None:
    """闸的判据是**子串**，而且用的是 **evidence 里的全量描述**（每人最多 20 条），
    不是模型看到的那 2 条——**闸口的文本比模型输入宽**。

    表外的谓词退化成「匹配谓词自己」（1.0 的 ``signals.get(predicate, (predicate,))``）。
    """
    from sentrix.application.identity.family_graph_service import FamilyPersonEvidence

    person_a = FamilyPersonEvidence("a", (), (), ("第十条才提到爸爸",), 0.0)
    person_b = FamilyPersonEvidence("b", (), (), ("无关描述",), 0.0)

    assert has_explicit_relation_evidence("父亲", person_a, person_b)
    assert not has_explicit_relation_evidence("祖父", person_a, person_b)
    assert has_explicit_relation_evidence("祖父", person_a, FamilyPersonEvidence("c", (), (), ("祖父",), 0.0))
    assert has_explicit_membership_evidence("family", person_a)
    assert not has_explicit_membership_evidence("friend", person_a)
    # ``unknown`` 的表是空的——调用方先判掉它，所以这里恒假。
    assert not has_explicit_membership_evidence("unknown", person_a)


# ---- 平铺列 → canonical 块（实测逼出来的回归）--------------------------------------


def _user_message(service, model) -> str:  # noqa: ANN001
    """跑一趟，把送给模型的 user 消息取回来。**它就是这条链的全部输入。**"""
    service["build"](model).run(service["run"].id, HOME)
    return json.loads(model.calls[0][1][1])


def test_the_model_sees_the_flat_columns_that_stand_in_for_canonical_json(service) -> None:
    """**2026-10-02 实测逼出来的回归。**

    153 上的真实数据里 ``objects_json`` 46/53 行有值、``clothing_json`` 33/53、
    ``place`` 与 ``event_type`` 各 46/53，而当时的证据查询**一列都没选**——
    于是模型只看到 ``caption`` + ``activity``。1.0 是把这些（连同更多）拼进
    ``canonical_json`` 一起喂的。

    这条从**模型的 user 消息**倒着断言：只要 SELECT 少一列、或拼接少一段，它就红。
    只测 ``_canonical_block`` 的函数级用例抓不到 SELECT 那一半。
    """
    with service["db"].connect() as connection, connection:
        connection.execute(
            "UPDATE observations SET place = '公园', event_type = '家庭出游',"
            " objects_json = ?, clothing_json = ?, ocr_text = '生日快乐' WHERE id = 'obs_1'",
            ('["帐篷", "野餐垫"]', '["红色外套"]'),
        )

    model = FakeModel(content=json.dumps({"memberships": [], "relationships": []}))
    evidence = _user_message(service, model)
    blobs = " ".join(" ".join(person["descriptions"]) for person in evidence["people"])

    assert "帐篷" in blobs
    assert "野餐垫" in blobs
    assert "红色外套" in blobs
    assert "家庭出游" in blobs
    assert "生日快乐" in blobs
    assert '"place"' in blobs


def test_a_malformed_json_column_does_not_break_the_run(service) -> None:
    """列是 TEXT，脏值是可能的。**一条脏数据不该让整个 run 落 failed**——
    这条链是挂在导入批末的，抛出去会连累导入响应（T314 的教训）。

    定点变异：去掉 ``_json_list`` 的 ``except``，这条变红（run 落 failed）。
    """
    with service["db"].connect() as connection, connection:
        connection.execute("UPDATE observations SET objects_json = '不是 JSON' WHERE id = 'obs_1'")

    model = FakeModel(content=json.dumps({"memberships": [], "relationships": []}))
    finished = service["build"](model).run(service["run"].id, HOME)

    assert finished is not None
    assert finished.status == "completed"


def test_the_block_is_omitted_when_every_column_is_empty(service) -> None:
    """全空时**不加空块**。1.0 用 ``if payload:`` 挡掉空对象；这里是同一条意图的
    延续——每段都拖一个 ``{}`` 只会白占 300 字的截断预算。

    定点变异：把 ``_canonical_block`` 的「空则返回空串」去掉、恒返回一个 ``{}``
    字符串，这条变红。
    """
    with service["db"].connect() as connection, connection:
        connection.execute(
            "UPDATE observations SET place = '', event_type = '', ocr_text = '',"
            " objects_json = '[]', clothing_json = '[]', people_json = '[]',"
            " facts_json = '[]' WHERE id = 'obs_1'"
        )

    model = FakeModel(content=json.dumps({"memberships": [], "relationships": []}))
    evidence = _user_message(service, model)
    descriptions = evidence["people"][0]["descriptions"]

    assert descriptions
    assert all("{}" not in text for text in descriptions)


def test_caption_and_activity_are_not_repeated_inside_the_block(service) -> None:
    """**与 1.0 的有意差异**：1.0 的 canonical 里含 caption / activity，于是同一段
    文本里它们出现两次；每段描述被截到 300 字，重复会把新补的 place / objects 挤出去。
    2.0 是重建而不是转储，所以不保留这份冗余。

    定点变异：往 ``_canonical_block`` 的 payload 里塞回 ``caption``，这条变红。
    """
    model = FakeModel(content=json.dumps({"memberships": [], "relationships": []}))
    evidence = _user_message(service, model)
    descriptions = evidence["people"][0]["descriptions"]

    assert descriptions
    for text in descriptions:
        assert text.count("一家人在公园野餐") == 1


def test_typed_model_failure_is_persisted_in_existing_run_stats(service):
    failure = ModelServiceError(
        "PRIVATE_PROVIDER_BODY",
        error_code="time_limit",
        http_status=504,
        request_policy={"request_outcome_unknown": True, "timeout_s": 1800},
    )
    result = service["build"](FakeModel(error=failure)).run(service["run"].id, HOME)
    assert result.status == "failed"
    issue = result.stats["operational_issue"]
    assert issue["error_code"] == "time_limit"
    assert issue["http_status"] == 504
    assert issue["request_policy"]["request_outcome_unknown"] is True
    assert "event_watermark" not in result.stats
    assert "PRIVATE" not in result.error
    stored = service["repo"].get_run(result.id)
    assert stored.stats["operational_issue"] == issue
