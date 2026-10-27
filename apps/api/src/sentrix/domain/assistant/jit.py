"""JIT 工具剪枝：只把**这一步真用得上的工具契约**发给模型。纯函数、零 IO。

对应 1.0 ``agent_runtime/jit_prompt.py`` 的 ``select_jit_tool_specs``
（``jit_prompt.py:88-125``）。2.0 原先每次模型调用都发全量八份完整 schema——
实测 **5408 字符 ≈ 2704 token**（``query_memory_facts`` 一份就 1344 字符），
而 1.0 的 ``LITE_TOOL_SCHEMAS`` 每工具不到 40 token，真实 ``prompt_tokens``
中位数只有 **1437**。差的那 2700 token 是每一转都付一次的固定成本。

**一处与 1.0 的关键差异，它决定了下面每条规则的松紧。** 1.0 的剪枝只作用于
**提示词的渲染**（``runtime.py:1877`` 的 ``build_jit_system_prompt``），真正的准入
白名单是 ``profile.tools``（``runtime.py:1764``）——所以 1.0 剪掉一个工具，模型
**看不见它，但仍然能调它**。2.0 走原生 ``tools`` 字段，那份数组既是广告也是约束：
**剪掉 = 物理上不能调**。

于是这里的规则**一律比 1.0 的渲染清单保守**：只剪「现在根本用不上」的，
不剪「这一轮后面可能还要用」的。判据落在**能力**上（有没有结果集、能不能取证），
不落在**需求**上（某个需求是不是已经销账）——``photo_set`` 被满足不等于候选看完了，
``get_result_page`` 因此只要已经有结果集就一直在。
"""

from __future__ import annotations

from dataclasses import replace

from sentrix.domain.assistant.evidence_ledger import EvidenceLedger, EvidenceType
from sentrix.domain.assistant.tools import FINAL_ANSWER_TOOL, TOOL_SPECS, ToolSpec

#: 三个照片级工具都要一个**来自当前结果集**的 handle（``result_set_id`` 是它们的必填
#: 参数）。没有结果集时给出它们，等于发一个注定以 ``unknown_result_set`` 失败的调用。
#: 1.0 用同一条判据（``jit_prompt.py:113`` 的 ``asset_handle_in_current_preview``）。
_PHOTO_LEVEL_TOOLS = frozenset({"inspect_photo", "read_photo_text", "query_photo_people"})

#: 需求类型 -> 能满足它的工具。与 ``ToolSpec.satisfies`` 是同一条映射的两个方向，
#: 但**不能反过来从 ``satisfies`` 现算**：``final_answer`` 也声明了 ``delivered_photo``，
#: 而它是终止动作、永远在表里，让它出现在「满足这个需求的工具」里只会把规则绕回去。
_TOOLS_BY_REQUIREMENT: dict[EvidenceType, frozenset[str]] = {
    EvidenceType.VISIBLE_TEXT: frozenset({"read_photo_text"}),
    EvidenceType.VISUAL_OBSERVATION: frozenset({"inspect_photo"}),
    EvidenceType.PHOTO_IDENTITY: frozenset({"query_photo_people"}),
    EvidenceType.STRUCTURED_FACT: frozenset({"query_memory_facts"}),
    EvidenceType.PERSON_PROFILE: frozenset({"query_photo_people", "get_person_profile"}),
}


def select_tools(
    ledger: EvidenceLedger,
    *,
    has_result_set: bool,
    tools_exhausted: bool,
    page_available: bool = True,
) -> tuple[ToolSpec, ...]:
    """这一步发给模型的工具集，**描述已经换成 ``brief``**。返回空元组是合法结果。

    ``tools_exhausted`` 为真时返回空集：工具额度用尽，模型的物理出路只剩「说话」。
    这正是要修的病灶——实测 ``step_budget_exhausted`` 的 123 道里 **102 道判 0、
    2 分一道没有**，根子是**模型没机会说话**，而它每一转都在调一个必然被拒的工具。

    **换 brief 这件事在这里做，不留给调用方。** 调用方忘了换，发出去的就是那份
    全量 ``description``——也就是这次要修掉的 2704 token/转，而且失败是静默的
    （模型照答，只是每次都多付两千多 token）。
    """
    return tuple(
        replace(spec, description=spec.brief)
        for spec in _select_offered(
            ledger,
            has_result_set=has_result_set,
            tools_exhausted=tools_exhausted,
            page_available=page_available,
        )
    )


def _select_offered(
    ledger: EvidenceLedger,
    *,
    has_result_set: bool,
    tools_exhausted: bool,
    page_available: bool,
) -> tuple[ToolSpec, ...]:
    if tools_exhausted:
        return ()

    open_types = {item.evidence_type for item in ledger.open_requirements()}
    offered = {FINAL_ANSWER_TOOL}

    # 检索是唯一的入口，而 ``max_search_calls = 1``：一次成功的检索之后，
    # ``ToolPolicy`` 会拒掉第二次——那时把 ``search_memories`` 留在表里就是留一个
    # 明知会被拒的工具。但它也不能更早剪掉：**还没有结果集时它是唯一能打开局面的东西**
    # （1.0 ``jit_prompt.py:196`` 在没有未决需求时也专门把 ``search_memories`` 留着，
    # 理由是「避免模型在事实看似足够时凭空编数字」）。
    if not has_result_set or EvidenceType.PHOTO_SET in open_types:
        offered.add("search_memories")

    for evidence_type, tools in _TOOLS_BY_REQUIREMENT.items():
        if evidence_type not in open_types:
            continue
        offered.update(tools if has_result_set else tools - _PHOTO_LEVEL_TOOLS)

    if has_result_set and page_available:
        # **翻页是一次性能力。** 结果集最多 24 张、第 1 页只给前 12，第 2 页从第 13 张
        # 开始直到结束——一次就能看完。成功翻过一次后由调用方把 ``page_available``
        # 置假，工具从表里消失（原生 tools 剪掉 = 物理上不能调）；候选不足一页时它
        # 从一开始就不出现。
        offered.add("get_result_page")

    return tuple(spec for spec in TOOL_SPECS if spec.name in offered)
