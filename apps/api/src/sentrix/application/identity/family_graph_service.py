"""家庭图谱推断 ``FamilyGraphService``（S7b2 · T312）。

逐行移植 1.0 ``backend/family_graph_service.py``（242 行）。四个阶段：取文本证据 →
问模型 → 落图 → 写画像。

**整条链只喂已落库的文字，绝不看图。** 1.0 的提示词第一句就是「只能使用输入中已经
落库的文字描述、事件与人物绑定 ID；绝不查看、索取或假设原始图片」——这不是省事，
是**这条链的定位**：它跑在导入之后，手里根本没有原图（`images=None`）。
（1.0 另有一条**传图**的 `PersonInsightService._infer_graph`，那条属裁决① 判死的
假设支线，与这里无关。）

**两条静默降级路径，不要改成抛错**（改成抛错会让「模型有点问题」变成「整个导入失败」）：

1. 没有模型（``model is None``）→ 直接返回空结果，run 照样 ``completed``；
2. 模型回了不是 JSON 的东西 → 同样返回空结果。

只有 SQL / 写库这类真异常才会让 run 落到 ``failed``。

**失败不回滚。** ``_persist`` 是边写边推进的：写到第 5 个人崩了，前 4 个的成员归属
已经 commit。而且 ``failed`` 时 ``stats`` 保持空 → ``event_watermark`` 缺失 →
下次触发的去抖判据（``event_count <= watermark``）不成立 → **一定重跑**。这是有意的。

**两处与 1.0 的适配**（都是 2.0 的既有形状逼出来的，不是选择）：

- 1.0 拼的 ``canonical_json`` / ``detail_json`` 两列在 2.0 **都没有写入者**，但 1.0
  装在 canonical 里的那批语义在 2.0 是**平铺列且有值**。``_semantic_description``
  于是**重建**那个块（见该函数 docstring）——它一度只拼 ``caption`` + ``activity``，
  实测 46/53 行的 ``objects_json`` 在喂模型前就被丢掉了；
- 1.0 把提示词与 JSON 拼成**一条** user 消息；2.0 的 ``OpenAIChatModel`` 硬性要求
  ``messages[0]`` 是 system 且**只能有一条** system，所以拆成 system（提示词）+
  user（JSON）。``SummarizeEvent`` 是同一种拆法。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sentrix.domain.assistant.models import ChatMessage, ChatRole
from sentrix.domain.assistant.ports import ChatModel, ModelServiceError
from sentrix.domain.identity.family import (
    FAMILY_MEMBERSHIPS,
    SOURCE_MODEL,
    FamilyAnalysisRunRecord,
    FamilyEvidenceRow,
)
from sentrix.domain.identity.family_graph import validate_relationship_pair
from sentrix.domain.identity.ports import EntityRepository, FamilyGraphRepository
from sentrix.domain.memories.ports import EventRepository

log = logging.getLogger(__name__)

# 1.0 ``family_graph_service.py:6-13`` 的提示词，**逐字**。它是这条链的产品定义：
# 只准用已落库文字、membership 只有三个取值、低频证据必须 unknown、单次同框不算家人。
FAMILY_GRAPH_PROMPT = """你是家庭相册关系推断器。只能使用输入中已经落库的文字描述、事件与人物绑定 ID；
绝不查看、索取或假设原始图片。请判断人物是否为家庭成员，并在有足够文本证据时输出直接中文关系。
成员 membership 只能是 family、friend、unknown；低频或证据不足必须为 unknown。
共现记录表示稳定人物在同一张已描述照片中出现；反复的成人-儿童家庭场景、家庭合影和长期共同出游是家庭成员及亲子/配偶关系的强证据。不要因一次活动同框就认定家庭。
关系只能在证据充分时输出，必须填写合法的正反向标签。只返回 JSON：
{"memberships":[{"person_id":"","membership":"family|friend|unknown","confidence":0.0}],
 "relationships":[{"subject_entity_id":"","predicate":"父亲","object_entity_id":"","inverse_predicate":"女儿","confidence":0.0}]}。
不要编造姓名、关系或不存在的人物 ID。输入："""

# 1.0 的截断上限，逐个照搬（``:56`` / ``:63`` / ``:64`` / ``:66-67`` / ``:214``）。
# 它们不是「性能优化」：512 token 的输出预算下，人多了就答不完。
MODEL_PEOPLE_LIMIT = 8
MODEL_OBSERVATIONS_PER_PERSON = 6
MODEL_DESCRIPTIONS_PER_PERSON = 2
MODEL_DESCRIPTION_CHARS = 300
MODEL_COOCCURRENCES_LIMIT = 30
EVIDENCE_DESCRIPTIONS_PER_PERSON = 20
EVIDENCE_COOCCURRENCE_OBSERVATIONS = 8

RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
INPUT_MODE = "semantic_text_only"

#: 关系边的来源：由已确认人物的 `family_role` 确定性补出来的。**不是模型推断**，
#: 所以图谱页可以把它和 `source=model` 的边分开显示。
SOURCE_ROLE_SEED = "role_seed"

# 1.0 ``:132-145`` 的关系证据词表，**只覆盖 13 个 predicate**。表外的谓词退化成
# 「字面匹配谓词自己」——所以祖父母、叔伯姑舅姨、侄甥那几类**没有同义词**，
# 只能靠描述里出现原词才过闸。这是 1.0 原样，别顺手补全。
RELATION_SIGNALS: Mapping[str, tuple[str, ...]] = {
    "父亲": ("父亲", "爸爸", "父女", "父子"),
    "母亲": ("母亲", "妈妈", "母女", "母子"),
    "儿子": ("儿子", "父子", "母子"),
    "女儿": ("女儿", "父女", "母女"),
    "丈夫": ("丈夫", "夫妻", "夫妇"),
    "妻子": ("妻子", "夫妻", "夫妇"),
    "哥哥": ("哥哥", "兄妹", "兄弟", "姐弟"),
    "姐姐": ("姐姐", "兄妹", "姐弟", "姐妹"),
    "弟弟": ("弟弟", "兄弟", "姐弟"),
    "妹妹": ("妹妹", "兄妹", "姐妹"),
    "朋友": ("朋友", "同学", "闺蜜", "好友"),
    "密友": ("密友", "闺蜜", "挚友"),
    "其他亲属": ("亲属", "家人", "家庭成员"),
}

MEMBERSHIP_SIGNALS: Mapping[str, tuple[str, ...]] = {
    "family": ("家人", "全家", "亲属", "家庭成员", "父亲", "母亲", "爸爸", "妈妈", "儿子", "女儿", "夫妻"),
    "friend": ("朋友", "同学", "闺蜜", "好友", "同事"),
}


@dataclass(frozen=True, slots=True)
class FamilyPersonEvidence:
    """一个人在这条 scope 下的文本证据。**模型只看它的一部分**（见 ``_infer``）。"""

    person_id: str
    face_instance_ids: tuple[str, ...]
    observation_ids: tuple[str, ...]
    descriptions: tuple[str, ...]
    confidence: float


@dataclass(frozen=True, slots=True)
class FamilyCooccurrence:
    person_a: str
    person_b: str
    count: int
    observation_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FamilyTextEvidence:
    scope_id: str
    people: tuple[FamilyPersonEvidence, ...]
    cooccurrences: tuple[FamilyCooccurrence, ...]


@dataclass(frozen=True, slots=True)
class FamilyGraphCounts:
    people: int = 0
    memberships: int = 0
    relationships: int = 0
    portraits: int = 0
    event_watermark: int = 0


class FamilyGraphService:
    def __init__(
        self,
        *,
        family: FamilyGraphRepository,
        events: EventRepository,
        entities: EntityRepository | None = None,
        model: ChatModel | None = None,
    ) -> None:
        self._family = family
        self._events = events
        # **角色播种**：已确认人物的 `family_role` 是身份确认时写入的 ground truth
        # （「父亲」「母亲」「妻子」…），不是模型猜的。模型那条推断被证据闸挡住了
        # ——VLM 描述里只会写「一名男子」，不会写「父亲」——于是关系图谱一条边都没有。
        # 这里以「自己」为中心，把已知角色补成确定性的边。可空是为了不强制测试造实体仓储。
        self._entities = entities
        self._model = model

    def run(self, run_id: str, scope_id: str) -> FamilyAnalysisRunRecord | None:
        """跑一趟，返回**收尾那次** ``update_run`` 的结果。

        两条前置校验**在 try 之外**：run 不存在抛 ``KeyError``、已在跑抛 ``RuntimeError``
        ——它们都不该把 run 标成 ``failed``（那时它要么不存在、要么正被另一个人跑着）。
        1.0 ``:24-28`` 原样。
        """
        run = self._family.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if run.status == RUNNING:
            raise RuntimeError("run already running")
        self._family.update_run(run_id, status=RUNNING, stage="build_text_evidence")
        try:
            evidence = self._build_text_evidence(scope_id)
            self._family.update_run(run_id, status=RUNNING, stage="infer_graph")
            output = self._infer(evidence)
            self._family.update_run(run_id, status=RUNNING, stage="write_graph")
            counts = self._persist(scope_id, run_id, evidence, output)
            # **已知角色补边**：模型那条推断被证据闸挡住时（VLM 只会写「一名男子」），
            # 关系图谱不会一条边都没有——已确认角色的可靠程度高于模型推断。
            counts["relationships"] += self._seed_relationships_from_roles(scope_id, run_id)
            self._family.update_run(run_id, status=RUNNING, stage="write_portraits")
            counts["portraits"] = self._write_portraits(scope_id, run_id, evidence)
            counts["event_watermark"] = self._event_count(scope_id)
            return self._family.update_run(
                run_id,
                status=COMPLETED,
                stage="done",
                stats={**counts, "input_mode": INPUT_MODE},
            )
        except ModelServiceError as error:
            issue = {
                "error_code": error.error_code or "provider_unavailable",
                "http_status": error.http_status,
                "request_policy": dict(error.request_policy),
                "finish_reason": error.finish_reason,
                "prompt_tokens": error.prompt_tokens,
                "completion_tokens": error.completion_tokens,
            }
            return self._family.update_run(
                run_id,
                status=FAILED,
                error=f"模型运行失败（{issue['error_code']}），本次家庭关系推断未完成。",
                stats={**run.stats, "operational_issue": issue},
            )
        except Exception as error:  # noqa: BLE001 —— 1.0 就是全兜，落 failed 而不是炸掉导入
            log.warning("家庭图谱推断失败（run=%s scope=%s）: %s", run_id, scope_id, error)
            return self._family.update_run(run_id, status=FAILED, error=str(error))

    def _event_count(self, scope_id: str) -> int:
        """1.0 ``:46-48`` 的 ``SELECT COUNT(*) FROM events WHERE scope_id = ?``。"""
        return self._events.count_by_scope(scope_id)

    # ---- 阶段 1：取证据 --------------------------------------------------------

    def _build_text_evidence(self, scope_id: str) -> FamilyTextEvidence:
        """把「脸 × 观察」的原始行折成**每人一份**的文本证据 + **两两共现**。

        三件事都是 1.0 原样：描述按 ``caption`` / ``activity`` / 两个 JSON 拼（2.0 只
        拼得出前两样，见模块 docstring）；每人的描述**去重后截到 20 条**；
        共现是「同一条观察里出现过的两个人」，按次数降序、同次按 id 升序。
        """
        rows: Sequence[FamilyEvidenceRow] = self._family.family_text_evidence(scope_id)
        order: list[str] = []
        face_ids: dict[str, list[str]] = {}
        observation_ids: dict[str, list[str]] = {}
        descriptions: dict[str, list[str]] = {}
        confidence: dict[str, float] = {}
        by_observation: dict[str, set[str]] = {}
        for row in rows:
            if row.entity_id not in face_ids:
                order.append(row.entity_id)
                face_ids[row.entity_id] = []
                observation_ids[row.entity_id] = []
                descriptions[row.entity_id] = []
                confidence[row.entity_id] = 0.0
            by_observation.setdefault(row.observation_id, set()).add(row.entity_id)
            if row.face_instance_id:
                face_ids[row.entity_id].append(row.face_instance_id)
            observation_ids[row.entity_id].append(row.observation_id)
            confidence[row.entity_id] = max(confidence[row.entity_id], row.confidence)
            description = _semantic_description(row)
            if description:
                descriptions[row.entity_id].append(description)

        values = [
            FamilyPersonEvidence(
                person_id=person_id,
                face_instance_ids=tuple(dict.fromkeys(face_ids[person_id])),
                observation_ids=tuple(dict.fromkeys(observation_ids[person_id])),
                # 1.0 ``:214``：去重之后**截到 20 条**。模型只看前 2 条，
                # 但两道证据闸看的是这 20 条。
                descriptions=tuple(
                    list(dict.fromkeys(descriptions[person_id]))[:EVIDENCE_DESCRIPTIONS_PER_PERSON]
                ),
                confidence=confidence[person_id],
            )
            for person_id in order
        ]

        # 共现：同一条观察里出现过的两个人。次数 = 这样的观察有几条。
        pairs: dict[tuple[str, str], list[str]] = {}
        for observation_id, entity_ids in by_observation.items():
            ordered = sorted(entity_ids)
            for index, left in enumerate(ordered):
                for right in ordered[index + 1 :]:
                    pairs.setdefault((left, right), []).append(observation_id)

        cooccurrences = [
            FamilyCooccurrence(
                person_a=left,
                person_b=right,
                count=len(seen),
                observation_ids=tuple(seen[:EVIDENCE_COOCCURRENCE_OBSERVATIONS]),
            )
            for (left, right), seen in pairs.items()
        ]
        cooccurrences.sort(key=lambda item: (-item.count, item.person_a, item.person_b))
        return FamilyTextEvidence(scope_id=scope_id, people=tuple(values), cooccurrences=tuple(cooccurrences))

    # ---- 阶段 1.5：用已知角色补关系边 -----------------------------------------

    def _seed_relationships_from_roles(self, scope_id: str, run_id: str) -> int:
        """把已确认人物的 `family_role` 补成以「自己」为中心的关系边。

        **这不是猜。** `family_role` 是身份确认时写进 `entities` 的（「父亲」「母亲」
        「妻子」「侄子」），来源是用户在待确认簇里填的表单，比 VLM 描述可靠得多。

        为什么需要它：模型那条推断要过 `has_explicit_relation_evidence` 闸——证据是
        观察描述，而 VLM 只会写「一名男子」「一位女士」，永远不会写「父亲」。结果是
        `family_relationships` 恒为 0，图谱只有孤立的点。

        只映射**语义明确**的四种角色；「大宝」「小宝」这种乳名不猜性别、不建边。
        """
        entities = self._entities
        if entities is None:
            return 0
        people = [
            item
            for item in entities.list_entities(status="confirmed", scope_id=scope_id)
            if item.entity_type == "person"
        ]
        center = next((item for item in people if (item.family_role or "") == "自己"), None)
        if center is None:
            return 0
        # role 是「这个人相对中心是什么」。反向谓词取表里第一个合法的。
        mapping = {
            "父亲": ("父亲", "儿子"),
            "母亲": ("母亲", "儿子"),
            "妻子": ("妻子", "丈夫"),
            "丈夫": ("丈夫", "妻子"),
            "侄子": ("侄子", "叔叔"),
        }
        created = 0
        for person in people:
            role = (person.family_role or "").strip()
            if person.id == center.id or role not in mapping:
                continue
            predicate, inverse = mapping[role]
            try:
                validate_relationship_pair(predicate, inverse)
            except ValueError:
                continue
            self._family.set_relationship(
                scope_id,
                person.id,
                predicate,
                center.id,
                inverse,
                source=SOURCE_ROLE_SEED,
                confidence=0.9,
                evidence_refs=(),
                inference_run_id=run_id,
            )
            for related in (person.id, center.id):
                self._family.set_membership(
                    scope_id,
                    related,
                    "family",
                    source=SOURCE_ROLE_SEED,
                    confidence=0.9,
                    evidence_refs=(),
                    inference_run_id=run_id,
                )
            created += 1
        return created

    # ---- 阶段 2：问模型 --------------------------------------------------------

    def _infer(self, evidence: FamilyTextEvidence) -> Mapping[str, object]:
        """截断 → 调模型 → 解析。**两条静默降级都在这里**（见模块 docstring）。

        截断的四个上限与 1.0 逐个对齐；共现**只保留两端都在 top-N 之内的对**——
        所以「模型看到的人」与「模型看到的共现」是同一批人。
        """
        if self._model is None:
            return {"memberships": [], "relationships": []}
        model_people = sorted(
            evidence.people,
            key=lambda item: (-len(item.observation_ids), item.person_id),
        )[:MODEL_PEOPLE_LIMIT]
        core_ids = {item.person_id for item in model_people}
        model_evidence = {
            "scope_id": evidence.scope_id,
            "input_mode": INPUT_MODE,
            "people": [
                {
                    "person_id": item.person_id,
                    "observation_ids": list(item.observation_ids[:MODEL_OBSERVATIONS_PER_PERSON]),
                    "descriptions": [
                        str(text)[:MODEL_DESCRIPTION_CHARS]
                        for text in item.descriptions[:MODEL_DESCRIPTIONS_PER_PERSON]
                    ],
                }
                for item in model_people
            ],
            "cooccurrences": [
                {
                    "person_a": item.person_a,
                    "person_b": item.person_b,
                    "count": item.count,
                    "observation_ids": list(item.observation_ids),
                }
                for item in evidence.cooccurrences
                if item.person_a in core_ids and item.person_b in core_ids
            ][:MODEL_COOCCURRENCES_LIMIT],
        }
        reply = self._model.complete(
            (
                ChatMessage(role=ChatRole.SYSTEM, content=FAMILY_GRAPH_PROMPT),
                ChatMessage(
                    role=ChatRole.USER,
                    content=json.dumps(model_evidence, ensure_ascii=False),
                ),
            ),
            (),
        )
        return _parse_object(reply.content)

    # ---- 阶段 3：落图 ----------------------------------------------------------

    def _persist(
        self,
        scope_id: str,
        run_id: str,
        evidence: FamilyTextEvidence,
        output: Mapping[str, object],
    ) -> dict[str, int]:
        """**先清场、再写**。两处「模型说是、文本里没证据就不落」的闸也在这里。

        第一条：**清场**（``supersede_model_relationships``）——只清「模型写过且未锁定」
        的边，用户写的一条都不动。

        第二条：**边落库后把两端强制升为 ``family``**（``:122-128``）——这一句
        **绕过**了下面那道 membership 闸，直接写死 ``"family"``。1.0 原样：一旦认定
        两人有亲属关系，他们两个就是家里人。
        """
        people = {item.person_id: item for item in evidence.people}
        self._family.supersede_model_relationships(scope_id)
        memberships = 0
        by_person = {
            str(item.get("person_id") or ""): item
            for item in _items(output.get("memberships"))
            if isinstance(item, dict)
        }
        for person_id, person in people.items():
            reported = by_person.get(person_id) or {}
            membership = reported.get("membership")
            if membership not in FAMILY_MEMBERSHIPS:
                membership = "unknown"
            if membership != "unknown" and not has_explicit_membership_evidence(str(membership), person):
                membership = "unknown"
            self._family.set_membership(
                scope_id,
                person_id,
                str(membership),
                source=SOURCE_MODEL,
                confidence=float(reported.get("confidence") or 0),
                evidence_refs=person.observation_ids,
                inference_run_id=run_id,
            )
            memberships += 1

        relationships = 0
        for item in _items(output.get("relationships")):
            if not isinstance(item, dict):
                continue
            subject = str(item.get("subject_entity_id") or "")
            object_id = str(item.get("object_entity_id") or "")
            predicate = str(item.get("predicate") or "")
            inverse = str(item.get("inverse_predicate") or "")
            # 结构闸：两端必须都在 evidence 里、且不是同一个人。
            if subject not in people or object_id not in people or subject == object_id:
                continue
            try:
                validate_relationship_pair(predicate, inverse)
            except ValueError:
                continue
            if not has_explicit_relation_evidence(predicate, people[subject], people[object_id]):
                continue
            self._family.set_relationship(
                scope_id,
                subject,
                predicate,
                object_id,
                inverse,
                source=SOURCE_MODEL,
                confidence=float(item.get("confidence") or 0),
                evidence_refs=tuple(
                    dict.fromkeys((*people[subject].observation_ids, *people[object_id].observation_ids))
                ),
                inference_run_id=run_id,
            )
            for related_person_id in (subject, object_id):
                self._family.set_membership(
                    scope_id,
                    related_person_id,
                    "family",
                    source=SOURCE_MODEL,
                    confidence=float(item.get("confidence") or 0),
                    evidence_refs=people[related_person_id].observation_ids,
                    inference_run_id=run_id,
                )
            relationships += 1
        return {
            "people": len(people),
            "memberships": memberships,
            "relationships": relationships,
        }

    # ---- 阶段 4：写画像 --------------------------------------------------------

    def _write_portraits(self, scope_id: str, run_id: str, evidence: FamilyTextEvidence) -> int:
        """每人一段固定句式的画像。**空文本不写**（1.0 ``:171``）。

        正文三段：家庭归属、**他作为 subject 的关系谓词**、前三条描述。注意关系只取
        ``subject_entity_id == person_id`` 那一侧——因为一条边落两行，
        两行都取会把同一个关系写两遍（一行是「母亲」一行是「儿子」）。
        """
        relationships = self._family.list_effective_relationships(scope_id)
        count = 0
        for person in evidence.people:
            membership = self._family.get_effective_membership(scope_id, person.person_id)
            predicates = [
                item.predicate for item in relationships if item.subject_entity_id == person.person_id
            ]
            text = "；".join(
                part
                for part in (
                    f"家庭归属：{membership.membership if membership else 'unknown'}",
                    ("关系：" + "、".join(predicates)) if predicates else "",
                    ("近期记忆：" + "；".join(person.descriptions[:3])) if person.descriptions else "",
                )
                if part
            )
            if text:
                self._family.write_portrait(
                    scope_id,
                    person.person_id,
                    text,
                    source=SOURCE_MODEL,
                    evidence_refs=person.observation_ids,
                    inference_run_id=run_id,
                )
                count += 1
        return count


# ---- 两道文本证据闸（纯函数，1.0 是 ``FamilyGraphService`` 上的 staticmethod） ---------


def has_explicit_relation_evidence(
    predicate: str, subject: FamilyPersonEvidence, object_person: FamilyPersonEvidence
) -> bool:
    """「模型说要建这条边，但两人的描述里有没有证据？」

    判据：把两人的描述拼成一个空格分隔的串，按词表查这个谓词的同义词元组，
    **任一 token 是子串就算过**。表外的谓词退化成「匹配谓词自己」。

    **两条容易看漏的性质：**

    - 这里用的是 **evidence 里的全量描述（每人最多 20 条）**，而**模型只看前 2 条**
      ——闸口的文本比模型输入**宽**；
    - 空格连接（不是换行、不是顿号）意味着**跨描述的相邻字符会拼到一起**，
      理论上能凑出一个谁都没写过的词。1.0 原样，照搬。
    """
    text = " ".join((*subject.descriptions, *object_person.descriptions))
    return any(token in text for token in RELATION_SIGNALS.get(predicate, (predicate,)))


def has_explicit_membership_evidence(membership: str, person: FamilyPersonEvidence) -> bool:
    """同上，但只看**一个人**的描述。``unknown`` 不进这道闸（调用方先判掉）。"""
    text = " ".join(person.descriptions)
    return any(token in text for token in MEMBERSHIP_SIGNALS.get(membership, ()))


def _semantic_description(row: FamilyEvidenceRow) -> str:
    """一条观察的文本：``caption`` / ``activity`` / 语义块。

    1.0 ``:232-242`` 拼四段——``caption`` / ``activity`` / ``canonical_json`` /
    ``detail_json``。2.0 那两列**都没有写入者**（见模块 docstring），但 1.0 装在
    canonical 里的语义在 2.0 是平铺列。所以这里**重建**那个块，而不是照抄一个恒空的列。

    与 1.0 的两处有意差异：

    1. **不把 caption / activity 再放进块里。** 1.0 的 canonical 含这两键，于是同一段
       文本里它们出现两次；而每段描述要被 ``MODEL_DESCRIPTION_CHARS``（300）截断，
       重复会把真正新增的 place / objects 挤出预算。1.0 是转储一个落库的 blob，
       2.0 是重建，没有理由保留这份冗余。
    2. **空值不进块。** 1.0 转储 ``analysis.get(key)``，缺的键写成 ``null``；这里按
       「有值才收」过滤——``facts_json`` 实测 53 行全空，收进去只是每段多个 ``"facts": []``。
    """
    parts = [str(row.caption or "").strip(), str(row.activity or "").strip()]
    canonical = _canonical_block(row)
    if canonical:
        parts.append(canonical)
    return "；".join(part for part in parts if part)


def _canonical_block(row: FamilyEvidenceRow) -> str:
    """按 1.0 ``pipeline.py:529`` 那个 canonical 的**键名**重建语义块。

    键名沿用 1.0（``place`` / ``event_type`` / ``objects`` / ``clothing`` / ``people``
    / ``ocr_text``），值来自 2.0 的同义列。序列化方式也与 1.0 一致
    （``ensure_ascii=False, sort_keys=True``），这样同一批数据在两版产出的文本形状相同。

    2.0 没有对应列的键（``scene_type`` / ``semantic`` / ``raw_labels`` / ``emotions``
    / ``spatial_relations``）**不补空占位**——补了就是每段多一串 ``null``，纯噪声。
    """
    payload: dict[str, object] = {
        "place": str(row.place or "").strip(),
        "event_type": str(row.event_type or "").strip(),
        "ocr_text": str(row.ocr_text or "").strip(),
    }
    for key, raw in (
        ("objects", row.objects_json),
        ("clothing", row.clothing_json),
        ("people", row.people_json),
        ("facts", row.facts_json),
    ):
        value = _json_list(raw)
        if value:
            payload[key] = value
    filled = {key: value for key, value in payload.items() if value}
    if not filled:
        return ""
    return json.dumps(filled, ensure_ascii=False, sort_keys=True)


def _json_list(raw: str) -> list[object]:
    """列里存的是 JSON 数组文本；坏值当空处理，不让一条脏数据毁掉整段证据。"""
    try:
        parsed = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return list(parsed) if isinstance(parsed, list) else []


def _parse_object(text: str) -> Mapping[str, object]:
    """1.0 ``:74-78`` 的解析：整串先试，再试「第一个 ``{`` 到最后一个 ``}``」。

    两条都失败就返回**空结果**——那是本模块 docstring 里第二条静默降级。
    与 ``summarize_event._parse_json_object`` 是同一套容错（1.0 那边也是各写一份）。
    """
    stripped = text.strip()
    candidates = [stripped]
    start, end = stripped.find("{"), stripped.rfind("}")
    if 0 <= start < end:
        candidates.append(stripped[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {"memberships": [], "relationships": []}


def _items(raw: object) -> list[object]:
    return list(raw) if isinstance(raw, list) else []


def _strings(raw: object) -> list[str]:
    return [str(item) for item in raw] if isinstance(raw, list) else []
