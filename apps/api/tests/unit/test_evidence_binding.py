from __future__ import annotations

from sentrix.domain.assistant.evidence_binding import (
    BindingVerdict,
    Fact,
    bind_requirement,
    facts_from_state,
)
from sentrix.domain.assistant.evidence_ledger import EvidenceRequirement, EvidenceType, RequirementValueType
from sentrix.domain.assistant.task_state import TaskState, ToolCallRecord
from sentrix.domain.assistant.profile import DEFAULT_PROFILE


def requirement(
    value_type: RequirementValueType,
    *,
    scope: dict[str, str] | None = None,
) -> EvidenceRequirement:
    return EvidenceRequirement(
        id="req_1",
        evidence_type=EvidenceType.STRUCTURED_FACT,
        description="需要证据",
        predicate=value_type.value,
        value_type=value_type,
        scope=tuple(sorted((scope or {}).items())),
    )


def retrieval_fact(**kwargs) -> Fact:
    return Fact(
        fact_type="retrieval_media_set",
        value_type=RequirementValueType.MEDIA_SET,
        value=("asset_1", "asset_2"),
        **kwargs,
    )


def test_media_set_is_satisfied_by_a_non_empty_retrieval():
    assert bind_requirement(requirement(RequirementValueType.MEDIA_SET), [retrieval_fact()]).verdict is (
        BindingVerdict.SATISFIES
    )


def test_an_empty_retrieval_does_not_satisfy_media_set():
    empty = retrieval_fact(value=())
    assert bind_requirement(requirement(RequirementValueType.MEDIA_SET), [empty]).verdict is (
        BindingVerdict.UNSUPPORTED
    )


def test_amount_is_not_satisfied_by_photo_candidates():
    """95 张候选不能当「婚宴花了多少钱」的证据。"""
    binding = bind_requirement(requirement(RequirementValueType.AMOUNT), [retrieval_fact()])
    assert binding.verdict is BindingVerdict.UNSUPPORTED


def test_count_is_satisfied_by_a_structured_count_fact():
    fact = Fact(
        fact_type="asset_count",
        value_type=RequirementValueType.COUNT,
        value=3,
        provenance="call_1",
    )
    binding = bind_requirement(requirement(RequirementValueType.COUNT), [fact])
    assert binding.verdict is BindingVerdict.SATISFIES
    assert binding.fact_ids == ("call_1",)


def test_conflicting_counts_in_the_same_scope_are_a_conflict():
    first = Fact("asset_count", RequirementValueType.COUNT, 3, provenance="a")
    second = Fact("asset_count", RequirementValueType.COUNT, 5, provenance="b")
    assert bind_requirement(requirement(RequirementValueType.COUNT), [first, second]).verdict is (
        BindingVerdict.CONFLICT
    )


def test_counts_with_different_scopes_are_not_conflicts():
    first = Fact("asset_count", RequirementValueType.COUNT, 3, scope={"time": "2017"}, provenance="a")
    second = Fact("asset_count", RequirementValueType.COUNT, 5, scope={"time": "2018"}, provenance="b")
    binding = bind_requirement(
        requirement(RequirementValueType.COUNT), [first, second]
    )
    assert binding.verdict is BindingVerdict.SATISFIES


def test_a_dropped_filter_is_a_scope_mismatch_not_a_partial():
    fact = Fact(
        "asset_count",
        RequirementValueType.COUNT,
        3,
        filters_not_applied=("time",),
        provenance="a",
    )
    binding = bind_requirement(
        requirement(RequirementValueType.COUNT, scope={"time": "2017"}),
        [fact],
    )
    assert binding.verdict is BindingVerdict.SCOPE_MISMATCH


def test_missing_scope_on_the_fact_is_partial():
    fact = Fact("asset_count", RequirementValueType.COUNT, 3, provenance="a")
    binding = bind_requirement(
        requirement(RequirementValueType.COUNT, scope={"place": "宜昌"}),
        [fact],
    )
    assert binding.verdict is BindingVerdict.PARTIAL


def test_facts_from_state_projects_structured_and_preview_facts():
    state = TaskState(
        turn_id="t",
        conversation_id="c",
        scope_id="s",
        question="q",
        profile=DEFAULT_PROFILE,
    )
    state.record(
        ToolCallRecord(
            call_id="call_facts",
            tool="query_memory_facts",
            signature="s1",
            status="ok",
            observation={
                "fact_type": "asset_count",
                "value": 4,
                "filters_applied": {"time": ("2017-01-01", "2018-01-01")},
                "filters_not_applied": ["place"],
            },
            model_visible={"status": "ok"},
        )
    )
    state.record(
        ToolCallRecord(
            call_id="call_search",
            tool="search_memories",
            signature="s2",
            status="ok",
            observation={"total": 2, "asset_ids": ["a", "b"]},
            model_visible={
                "preview": [
                    {
                        "handle": "photo_1",
                        "captured_at": "2017-10-01",
                        "place": "河北省石家庄市",
                        "people": ["明明"],
                        "evidence_summary": "婚礼现场",
                    }
                ]
            },
        )
    )

    facts = facts_from_state(state)
    assert any(fact.value_type is RequirementValueType.COUNT for fact in facts)
    assert any(fact.value_type is RequirementValueType.TIME for fact in facts)
    assert any(fact.value_type is RequirementValueType.PLACE for fact in facts)
    assert any(fact.value_type is RequirementValueType.PERSON for fact in facts)
    assert any(fact.value_type is RequirementValueType.DESCRIPTION for fact in facts)
    structured = next(fact for fact in facts if fact.value_type is RequirementValueType.COUNT)
    assert structured.filters_not_applied == ("place",)
