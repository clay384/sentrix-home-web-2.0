"""证据账本与结果集的单元测试。

账本的两条纪律来自 1.0：需求必须有显式的证据来源（``task_state.py:243`` 的
``mark_satisfied`` 在 refs 为空时直接 raise），以及「仅共享 evidence type 不算满足需求」
（``requirement_completion.py:63``）。结果集的三条不变量来自 1.0 已经跑通的分页
（``result_set.py:116``）。
"""

from __future__ import annotations

import pytest

from sentrix.domain.assistant.evidence_ledger import (
    EvidenceLedger,
    EvidenceRequirement,
    EvidenceType,
    RequirementStatus,
)
from sentrix.domain.assistant.result_set import PAGE_SIZE, ResultSet


def ledger_with(*evidence_types: EvidenceType) -> EvidenceLedger:
    return EvidenceLedger(
        scope_id="album1",
        requirements=[
            EvidenceRequirement(id=f"r{index}", evidence_type=evidence_type, description=f"需求 {index}")
            for index, evidence_type in enumerate(evidence_types)
        ],
    )


# ---- 账本 -------------------------------------------------------------------


def test_a_ledger_must_be_bound_to_a_scope():
    """混用两个 scope 的记忆是数据泄漏，不是业务错误，必须在记账那一刻就拒绝。"""
    with pytest.raises(ValueError):
        EvidenceLedger(scope_id="")


def test_satisfying_requires_an_evidence_source():
    """1.0 的 ``mark_satisfied`` 在 ``evidence_refs`` 为空时直接 raise。"""
    ledger = ledger_with(EvidenceType.PHOTO_SET)

    with pytest.raises(ValueError):
        ledger.satisfy(EvidenceType.PHOTO_SET, call_id="")


def test_satisfying_records_the_call_that_produced_the_evidence():
    ledger = ledger_with(EvidenceType.PHOTO_SET)

    satisfied = ledger.satisfy(EvidenceType.PHOTO_SET, call_id="call_1")

    assert satisfied == ("r0",)
    assert ledger.requirement("r0").satisfied_by == ("call_1",)
    assert ledger.is_complete is True


def test_a_second_call_is_appended_not_replaced():
    """同一类证据可以来自多次调用（首页 + 翻页），来源全部留痕。"""
    ledger = ledger_with(EvidenceType.PHOTO_SET)

    ledger.satisfy(EvidenceType.PHOTO_SET, call_id="call_1")
    ledger.satisfy(EvidenceType.PHOTO_SET, call_id="call_2")

    assert ledger.requirement("r0").satisfied_by == ("call_1", "call_2")


def test_satisfying_an_unrelated_type_changes_nothing():
    """反向验证：只声明同一类证据的工具才有资格满足它，别的调用不能顺带把需求划掉。"""
    ledger = ledger_with(EvidenceType.DELIVERED_PHOTO)

    assert ledger.satisfy(EvidenceType.PHOTO_SET, call_id="call_1") == ()
    assert ledger.is_complete is False


def test_blocking_closes_a_requirement_with_a_reason():
    ledger = ledger_with(EvidenceType.PERSON_PROFILE)

    blocked = ledger.block(EvidenceType.PERSON_PROFILE, reason="identity_not_available")

    assert blocked == ("r0",)
    assert ledger.requirement("r0").status is RequirementStatus.BLOCKED
    assert ledger.requirement("r0").unmet_reason == "identity_not_available"
    # 封口不是满足：门禁不该再逼模型去取，但收尾要如实说明缺什么。
    assert ledger.is_complete is False
    assert ledger.open_requirements() == ()


def test_blocking_requires_a_reason():
    with pytest.raises(ValueError):
        ledger_with(EvidenceType.PERSON_PROFILE).block(EvidenceType.PERSON_PROFILE, reason="")


def test_open_requirements_only_lists_required_ones():
    ledger = EvidenceLedger(
        scope_id="album1",
        requirements=[
            EvidenceRequirement(
                id="optional",
                evidence_type=EvidenceType.PERSON_PROFILE,
                description="可选的",
                required=False,
            )
        ],
    )

    assert ledger.open_requirements() == ()
    assert ledger.is_complete is True


def test_payload_is_the_projection_that_goes_into_progress_events():
    ledger = ledger_with(EvidenceType.PHOTO_SET)
    ledger.satisfy(EvidenceType.PHOTO_SET, call_id="call_1")

    payload = list(ledger.as_payload())

    assert payload[0]["evidence_type"] == "photo_set"
    assert payload[0]["status"] == "satisfied"
    assert payload[0]["satisfied_by"] == ["call_1"]


# ---- 结果集 -----------------------------------------------------------------


def make_result_set(count: int) -> ResultSet:
    from datetime import UTC, datetime

    return ResultSet(
        id="rs_1",
        scope_id="album1",
        query="海边",
        asset_ids=tuple(f"asset_{index}" for index in range(count)),
        created_at=datetime(2026, 9, 22, tzinfo=UTC),
    )


def test_the_first_page_holds_twelve_photos():
    """2026-10-26：6 → 12。检索上限就是 12，一页给全后模型不再需要翻页。"""
    page = make_result_set(20).first_page()

    assert PAGE_SIZE == 12
    assert page.page == 1
    assert [entry.handle for entry in page.entries] == [f"photo_{i}" for i in range(1, 13)]
    assert page.total == 20
    assert page.has_more is True
    assert page.remaining == 8


def test_a_ten_photo_result_set_fits_on_one_page():
    """候选不足一页时 ``has_more`` 必须是假——工具层据此拒绝翻页。"""
    page = make_result_set(10).first_page()

    assert len(page.entries) == 10
    assert page.has_more is False
    assert page.remaining == 0


def test_handles_are_global_ordinals_not_page_ordinals():
    """第 2 页从 ``photo_13`` 开始。模型只需要记住 ``photo_N``，跨页稳定。"""
    result_set = make_result_set(20)

    assert [entry.handle for entry in result_set.page(2).entries] == [f"photo_{i}" for i in range(13, 21)]


def test_the_second_page_starts_at_thirteen_and_runs_to_the_end():
    """18 张候选：第 1 页 photo_1..12，第 2 页 photo_13..18（只到结尾，不补齐）。"""
    result_set = make_result_set(18)

    first = result_set.page(1)
    assert [entry.handle for entry in first.entries] == [f"photo_{i}" for i in range(1, 13)]
    assert first.has_more is True
    assert first.remaining == 6

    second = result_set.page(2)
    assert [entry.handle for entry in second.entries] == [f"photo_{i}" for i in range(13, 19)]
    assert second.has_more is False
    assert second.remaining == 0


def test_a_single_page_result_set_has_no_second_page():
    assert make_result_set(PAGE_SIZE).page(2).entries == ()


def test_page_size_is_capped_at_twelve():
    """``page_size`` 传多大都只给 12 张——与 ``min(_RESULT_PAGE_SIZE, ...)`` 同一条。"""
    page = make_result_set(20).page(1, page_size=100)

    assert page.page_size == PAGE_SIZE
    assert len(page.entries) == PAGE_SIZE


def test_a_page_past_the_end_is_empty_and_not_an_error():
    """越界不是错误，是「没有更多了」。抛异常会让模型把翻到底当成系统故障。"""
    page = make_result_set(3).page(9)

    assert page.entries == ()
    assert page.has_more is False
    assert page.remaining == 0


def test_resolve_maps_a_handle_back_to_its_asset():
    result_set = make_result_set(3)

    assert result_set.resolve("photo_2") == "asset_1"
    assert result_set.handle_for("asset_1") == "photo_2"


def test_resolve_rejects_a_handle_outside_the_result_set():
    """1.0 的授权端点正是靠这一步把「模型给的 handle」与「真实资产」隔开。"""
    result_set = make_result_set(3)

    assert result_set.resolve("photo_9") is None
    assert result_set.resolve("asset_1") is None
    assert result_set.resolve("photo_x") is None
