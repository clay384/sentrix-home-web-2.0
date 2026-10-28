"""JIT 剪枝：这一步到底把哪几份工具契约发给模型。

对应 1.0 ``agent_runtime/jit_prompt.py`` 的 ``select_jit_tool_specs``。**这里断言的是
关系，不是抄一份工具名单**——名单抄一遍，规则改了测试不会红，那等于没有门禁。
"""

from __future__ import annotations

import json

from sentrix.domain.assistant.evidence_ledger import (
    EvidenceLedger,
    EvidenceRequirement,
    EvidenceType,
)
from sentrix.domain.assistant.jit import select_tools
from sentrix.domain.assistant.tools import FINAL_ANSWER_TOOL, TOOL_SPECS


def ledger_with(*evidence_types: EvidenceType) -> EvidenceLedger:
    ledger = EvidenceLedger(scope_id="album1")
    for index, evidence_type in enumerate(evidence_types):
        ledger.requirements.append(
            EvidenceRequirement(id=f"req_{index}", evidence_type=evidence_type, description="")
        )
    return ledger


def names(specs) -> set[str]:
    return {spec.name for spec in specs}


def test_only_the_tools_the_open_requirements_need_are_offered():
    """需求没开的工具不发。``structured_fact`` 没声明，就不该看见 ``query_memory_facts``。"""
    offered = names(
        select_tools(
            ledger_with(EvidenceType.PHOTO_SET, EvidenceType.VISUAL_OBSERVATION),
            has_result_set=True,
            tools_exhausted=False,
        )
    )

    assert "inspect_photo" in offered
    assert "query_memory_facts" not in offered
    assert "get_person_profile" not in offered
    assert "read_photo_text" not in offered


def test_the_terminal_action_is_always_offered():
    """**空需求也要有终止动作。** 剪到没有 ``final_answer``，模型就没有收口的形状了。"""
    offered = names(select_tools(ledger_with(), has_result_set=False, tools_exhausted=False))

    assert FINAL_ANSWER_TOOL in offered


def test_a_missing_result_set_always_offers_search():
    """反向：不能把检索也剪掉。

    1.0 专门补过这一条——规划声明的需求可能**漏掉前置工具**，所以它会把「当前待解析的
    那一步需要的工具」补回可用集（``jit_prompt.py:196``）。没有结果集时，
    ``search_memories`` 是唯一能打开局面的东西。
    """
    offered = names(select_tools(ledger_with(), has_result_set=False, tools_exhausted=False))

    assert "search_memories" in offered


def test_the_photo_level_tools_wait_for_a_result_set():
    """三个照片级工具都要一个**来自当前结果集**的 handle。

    没有结果集时给出它们，等于发一个注定以 ``unknown_result_set`` 失败的调用——
    而每一次失败都要烧一个模型步。1.0 的判据是同一条
    （``jit_prompt.py:113`` 的 ``asset_handle_in_current_preview``）。
    """
    wanted = (EvidenceType.VISUAL_OBSERVATION, EvidenceType.VISIBLE_TEXT, EvidenceType.PHOTO_IDENTITY)

    before = names(select_tools(ledger_with(*wanted), has_result_set=False, tools_exhausted=False))
    after = names(select_tools(ledger_with(*wanted), has_result_set=True, tools_exhausted=False))

    assert {"inspect_photo", "read_photo_text", "query_photo_people"} & before == set()
    assert {"inspect_photo", "read_photo_text", "query_photo_people"} <= after


def test_the_page_tool_disappears_after_it_has_been_used_once():
    """翻页是一次性的：成功一次后调用方把 page_available 置假，工具物理上消失。"""
    ledger = ledger_with(EvidenceType.PHOTO_SET)

    offered = names(select_tools(ledger, has_result_set=True, tools_exhausted=False, page_available=False))

    assert "get_result_page" not in offered


def test_paging_survives_a_satisfied_photo_set():
    """翻页**不按需求剪**：``photo_set`` 销账不等于候选看完了，第一次翻页仍要可用。

    但它是**一次性**的：成功翻过一次后调用方把 ``page_available`` 置假，工具物理上
    消失（见下一条）；候选不足一页时从一开始就不出现。
    """
    ledger = ledger_with(EvidenceType.PHOTO_SET)
    ledger.satisfy(EvidenceType.PHOTO_SET, call_id="call_1")

    offered = names(select_tools(ledger, has_result_set=True, tools_exhausted=False))

    assert "get_result_page" in offered
    # 检索本身反而该退场：``max_search_calls = 1``，留着它只是留一个必然被拒的工具。
    assert "search_memories" not in offered


def test_a_spent_budget_offers_nothing_at_all():
    """额度用尽 → **一个工具都不发**，模型只能说话。

    这是 ``step_budget_exhausted`` 那 123 道题里 102 道判 0 的解药：模型此前每一转都在
    调一个必然被拒的工具，从来没被逼着开口。
    """
    offered = select_tools(ledger_with(EvidenceType.PHOTO_SET), has_result_set=True, tools_exhausted=True)

    assert offered == ()


def test_the_brief_listing_is_smaller_than_the_full_one():
    """断言的是**字符数的关系**，不是抄一个具体数。

    ``query_memory_facts`` 一份完整 schema 就 1344 字符；每工具不到 40 字的 brief 换掉
    它，是一条 2704 token/转 的固定成本。留 1/3 的余量是给规则变化的空间——真跌破这条线
    说明 brief 又写回成长描述了。
    """
    offered = select_tools(ledger_with(), has_result_set=False, tools_exhausted=False)

    brief_chars = len(json.dumps(_dump(offered), ensure_ascii=False))
    full_chars = len(json.dumps(_dump(TOOL_SPECS), ensure_ascii=False))

    assert brief_chars * 3 < full_chars


def test_every_tool_has_a_brief_no_longer_than_40():
    """每个工具都要有一句 ≤40 字的 brief。

    上限不是审美：1.0 的 ``LITE_TOOL_SCHEMAS`` 每工具不到 40 token，而八份 schema
    全量的固定成本是 2704 token/转。``ToolSpec.brief`` 没有默认值，漏写会在构造处就
    报错——这条守的是**长度**，那个守的是**存在**。
    """
    too_long = {spec.name: len(spec.brief) for spec in TOOL_SPECS if len(spec.brief) > 40}

    assert too_long == {}
    assert all(spec.brief.strip() for spec in TOOL_SPECS)


def _dump(specs) -> list[dict[str, object]]:
    return [dict(spec.as_openai_tool()["function"]) for spec in specs]
