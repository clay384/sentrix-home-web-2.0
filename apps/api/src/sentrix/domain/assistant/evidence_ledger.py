"""证据账本：把「回答这个问题最少需要哪些证据」变成可判定的状态机。

对应 1.0 ``agent_runtime/task_state.py`` 的 ``RequirementState`` +
``evidence_ledger.py``，但只保留 S3 需要的那部分。

一条 1.0 里最干净的纪律原样保留：**工具调用不等于证据**。1.0 的
``requirement_completion.py:63-67`` 写得最直白——「仅共享 evidence type 的证据是有效
遥测，但它不能隐式满足需求」。2.0 把它落成工具契约里的显式声明：工具的 ``ToolSpec``
列出它能满足哪几类证据（``satisfies``），账本只认这个映射，不按工具名猜。
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class EvidenceType(StrEnum):
    """S3 用到的公共证据类型。

    1.0 的九类公共证据里，S3 只需要三类：

    - ``photo_set``：取到了一组候选照片（``search_memories`` / ``get_result_page``）；
    - ``delivered_photo``：把其中某几张标记为「要交付给用户」（``final_answer`` 的
      ``selected_image_handles`` 字段）；
    - ``person_profile``：读到了已确认人物的档案（``get_person_profile``）。

    1.0 的一个真实缺口正好在 ``delivered_photo`` 这一格：``completion.py`` 有
    ``DELIVER_MEDIA`` 需求码，但 ``requirement_completion.py`` 的
    ``_AGENT2_EVIDENCE_CODES`` 只映射了检索/视觉/OCR 三个，而生产默认 profile
    （``agent2_authoritative``）下 ``completion.blocking()`` 被旁路——于是「用户要看图」
    这条需求在权威模式下**没有任何门禁保护**。2.0 把它变成一等证据类型。
    """

    PHOTO_SET = "photo_set"
    DELIVERED_PHOTO = "delivered_photo"
    PERSON_PROFILE = "person_profile"
    # ``query_memory_facts`` 的产物。1.0 的 ``produces_evidence=("structured_fact",)``。
    #
    # **它是一条「答案」而不是一条「材料」**：检索工具产出的候选池是用来筛的，
    # 而这个工具产出的数字本身就是回答。所以它不需要被某个需求码映射——
    # 账本记下「这一轮拿到过一条精确统计」，门禁不会因为它去逼模型再检索一次。
    STRUCTURED_FACT = "structured_fact"
    # ``query_photo_people`` 的产物（1.0 ``tools.py:2727`` 的 ``produces_evidence``）。
    #
    # **1.0 在这一格上有两类，2.0 只补一类，这是有意的。** 1.0 声明的是
    # ``("photo_identity", "confirmed_identity")``，两个都在
    # ``evidence_contract.py`` 的公共表里、也都发给规划器。对到 2.0：
    #
    # - ``confirmed_identity`` **已经有了，就是下面的 ``PERSON_PROFILE``**——
    #   ``intent.py`` 把 ``person`` / ``identity`` / ``profile`` 三个轴词都映射到它，
    #   而 1.0 ``goal_planner.py:66-67`` 把前两个映射到 ``confirmed_identity``：
    #   同一个轴，2.0 是改名加收窄；
    # - ``photo_identity`` **是另一类**，2.0 没有，所以在这里补上。
    #
    # 1.0 的规划器为它单独写过一句纪律（``goal_planner.py:49``：「身份问题只有在需要
    # 确认**照片中的人名**时才声明 photo_identity」），``runtime.py:1011`` 还为它单独
    # 做工具准入——它不是 ``confirmed_identity`` 的同义词。
    PHOTO_IDENTITY = "photo_identity"
    # ``inspect_photo`` 的产物（1.0 ``tools.py:2756``：``produces_evidence=("visual_observation",
    # "photo_identity")``）。1.0 的公共表里有它（``evidence_contract.py:15``），
    # 而且规划器的轴词表把它当作 ``visual`` / ``detail`` 两个词的目标
    # （``goal_planner.py:68-69``）——**「看细节」与「这是谁」在 1.0 是两类需求**，
    # 前者由 ``inspect_photo`` 满足，后者由 ``query_photo_people`` / ``inspect_photo``
    # 一起满足，这个区别在 T123 的 ``satisfies`` 上是两个条目。
    VISUAL_OBSERVATION = "visual_observation"
    # ``read_photo_text`` 的产物（1.0 ``tools.py:2768``）。1.0 的公共表里有它
    # （``evidence_contract.py:16``），轴词表里 ``text`` / ``ocr`` / ``price`` 三个词
    # 都指向它（``goal_planner.py:70-72``）。
    #
    # **S6 补的第三类，也是最后一类。** 三类加起来是「看细节 / 这是谁 / 写着什么」，
    # 正好对应 1.0 那三个视觉工具的分工。
    VISIBLE_TEXT = "visible_text"


class RequirementStatus(StrEnum):
    OPEN = "open"
    SATISFIED = "satisfied"
    BLOCKED = "blocked"


class RequirementValueType(StrEnum):
    """用户最终想要的值，而不是「用哪类证据回答」。

    ``EvidenceType`` 回答的是「去哪个工具取材料」，``RequirementValueType`` 回答的是
    「答案本身是什么形态」。两者分开之后，``photo_set`` 里翻到 95 张候选不能自动等于
    「婚宴花了多少钱」——金额要么由 ``structured_fact`` 给出，要么如实标成 unsupported。
    """

    PLACE = "place"
    TIME = "time"
    DURATION = "duration"
    AMOUNT = "amount"
    COUNT = "count"
    PERSON = "person"
    MEDIA_SET = "media_set"
    DESCRIPTION = "description"
    BOOLEAN = "boolean"
    VALUE = "value"


#: scope 只允许这四个键；时间/地点/人物是限定条件，不是需求本身。
REQUIREMENT_SCOPE_KEYS = frozenset({"event", "time", "place", "person"})


@dataclass
class EvidenceRequirement:
    id: str
    evidence_type: EvidenceType
    description: str
    required: bool = True
    status: RequirementStatus = RequirementStatus.OPEN
    satisfied_by: tuple[str, ...] = ()
    unmet_reason: str = ""
    #: 规划器输出的结构化绑定字段。旧形状（没有这三格）仍然合法：
    #: ``value_type`` 为 ``None`` 表示「这条需求没有声明答案形态」，
    #: 绑定器会退化成只按 evidence_type 判定，不假装它满足任何具体值类型。
    predicate: str = ""
    value_type: RequirementValueType | None = None
    scope: tuple[tuple[str, str], ...] = ()


@dataclass
class EvidenceLedger:
    """一次回合的证据需求清单。

    ``scope_id`` 与 1.0 一样进构造函数并被强校验：账本混用两个 scope 的记忆是
    数据泄漏，不是业务错误，必须在记账那一刻就拒绝。
    """

    scope_id: str
    requirements: list[EvidenceRequirement] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.scope_id:
            raise ValueError("证据账本必须绑定一个 scope_id")

    def requirement(self, requirement_id: str) -> EvidenceRequirement | None:
        for item in self.requirements:
            if item.id == requirement_id:
                return item
        return None

    def open_requirements(self) -> tuple[EvidenceRequirement, ...]:
        return tuple(
            item for item in self.requirements if item.required and item.status is RequirementStatus.OPEN
        )

    @property
    def is_complete(self) -> bool:
        """**所有**必填需求都被满足。被 blocked 的不算——封口不是满足。

        这一条与 ``open_requirements`` 是两个问题：前者问「门禁还要不要逼模型取证」，
        后者问「这一轮证据齐了吗」。
        """
        return all(item.status is RequirementStatus.SATISFIED for item in self.requirements if item.required)

    def satisfy(self, evidence_type: EvidenceType, *, call_id: str) -> tuple[str, ...]:
        """一次成功的工具调用满足了哪些需求。返回被满足的需求 id。

        ``call_id`` 是必填的：没有证据来源的「已满足」在 1.0 里被显式禁止
        （``task_state.py:243-253`` 的 ``mark_satisfied`` 在 refs 为空时直接 raise）。
        一个需求可以被多次调用满足，来源全部留痕。
        """
        if not call_id:
            raise ValueError("满足一个证据需求必须带上产生它的工具调用 id")
        satisfied: list[str] = []
        for item in self.requirements:
            if item.evidence_type is not evidence_type:
                continue
            # 已经满足的需求也继续记来源：一次检索 + 一次翻页都产出了照片证据，
            # 审计时该看得到两次。上限由工具预算兜住（每回合最多 6 次调用）。
            item.status = RequirementStatus.SATISFIED
            item.satisfied_by = (*item.satisfied_by, call_id)
            item.unmet_reason = ""
            satisfied.append(item.id)
        return tuple(satisfied)

    def block(self, evidence_type: EvidenceType, *, reason: str) -> tuple[str, ...]:
        """把某类证据标成「本轮拿不到」，并记下原因。

        ``blocked`` 与「还开着」是两件事：开着意味着还可以再试，blocked 意味着已经
        确认这一轮拿不到——门禁只对前者逼模型继续取证。
        """
        if not reason:
            raise ValueError("封口一个证据需求必须写明原因")
        blocked: list[str] = []
        for item in self.requirements:
            if item.evidence_type is evidence_type and item.status is RequirementStatus.OPEN:
                item.status = RequirementStatus.BLOCKED
                item.unmet_reason = reason
                blocked.append(item.id)
        return tuple(blocked)

    def mark_unmet(self, *, attempted: Collection[EvidenceType]) -> None:
        """收尾时给还开着的需求补一个**能区分两种缺陷**的原因。

        1.0 与 2.0 此前都留空串，于是结果里分不出「取过但没取到」与「声明了根本没去取」。
        实测 ``visible_text`` 有 155 道开着，其中 **147 道从头到尾没调用过
        ``read_photo_text``**——那不是检索失败，是**规划与执行脱节**。两种缺陷的修法
        完全不同，指标上就必须分得开。

        只填没填过的：``block()`` 写下的具体原因（工具自己说了这类证据本轮拿不到）
        比这两句泛化的话更准。
        """
        for item in self.requirements:
            if item.status is RequirementStatus.SATISFIED or item.unmet_reason:
                continue
            item.unmet_reason = "attempt_failed" if item.evidence_type in attempted else "not_attempted"

    def as_payload(self) -> Sequence[Mapping[str, Any]]:
        """进 ``progress`` 事件与轨迹的投影。"""
        return tuple(
            {
                "id": item.id,
                "evidence_type": item.evidence_type.value,
                "description": item.description,
                "status": item.status.value,
                "satisfied_by": list(item.satisfied_by),
                "unmet_reason": item.unmet_reason,
                "predicate": item.predicate,
                "value_type": item.value_type.value if item.value_type else "",
                "scope": dict(item.scope),
            }
            for item in self.requirements
        )
