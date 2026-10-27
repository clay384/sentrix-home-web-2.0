"""回合预算。数值取自 ``docs/refactor/behaviour-baseline.md`` 的冻结项。

1.0 的 ``ProfileConfig``（``agent_runtime/profile.py:13``）把这些值连同 6 套 profile
放在一个 dict 里，由 ``SENTRIX_AGENT_PROFILE`` 环境变量选；未知名字**静默回退**到默认
profile，不打日志。2.0 只保留生产在跑的那一套，写死成常量——没有 env、没有「选错了也
不报错」的入口。

两处与 1.0 的刻意差异：

1. ``wall_time_s`` 不再被就地改写。1.0 在检测到 OCR / 多图意图时把预算对象里的墙钟
   放宽到 240s，而轨迹里记的仍是 profile 的 180s（``runtime.py:2130``）——事后看数据
   永远对不上。要改预算就改这个文件，那是一次有版本、可审查的变更。
2. ``final_reserve_s`` 只留给「写答案」。1.0 的 ``can_tool_call`` 检查它、
   ``can_model_step`` 不检查（``budget_manager.py:38-51``），这个非对称是对的，保留。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TurnProfile:
    name: str
    max_steps: int
    max_tool_calls: int
    max_search_calls: int
    #: 一轮里最多翻几次页（``get_result_page``）。**1.0 没有这条上限。**
    #
    # 数是照着**结果集本身的容量**定的，不是拍的：``search_memories`` 的 ``limit``
    # 上限是 12（``application/assistant/tools.py`` 的 ``MAX_SEARCH_LIMIT``），每页
    # 12 张（``result_set.PAGE_SIZE``）——**一页看完**。正常路径根本不需要翻页；
    # 2 次是「一次失败重试 + 一次保险」的余量（见
    # ``test_a_single_failure_does_not_block_the_retry``），不是给两页用的。
    #
    # **为什么需要它。** 实测 20261009 那一轮：56 个以 ``step_budget_exhausted`` 收尾的
    # 回合里 **53 个最后动作都是 ``get_result_page``**——模型被 L2 打回之后不知道说什么，
    # 就翻页；而**翻过头不报错**（越界返回空页仍是 ``ok``，那是 1.0 的行为），于是它
    # 拿不到「已经到底了」这个信号，一直翻到步数耗尽。一次翻页是一个工具往返、一个模型步。
    #
    # 与 ``max_search_calls`` 并列而不是合并：那是「检索几次」，这是「同一份检索看几页」。
    max_page_calls: int
    # 一轮里最多几次「精读」（``ToolSpec.cost_class == EXPENSIVE``）。1.0
    # ``budget_manager.py:17`` 的 ``max_inspections = 1``。
    #
    # **它是 T123 与 T124 共用的**：1.0 的 ``inspect_photo`` 与 ``read_photo_text``
    # 都是 ``cost_class="expensive"``，共用一个 ``inspections`` 计数器——所以这条
    # 约束是「一轮总共只能精读一张图一次」，不是「每个工具各一次」。
    # 1.0 的工具描述里那句「同一张图最多复核一次，不要反复检查同一张」的**执行机构**
    # 就是它；少了它，那句话只是一句劝告。
    max_inspections: int
    wall_time_s: float
    final_reserve_s: float


# goal_driven_candidate：1.0 的生产默认 profile。
DEFAULT_PROFILE = TurnProfile(
    name="goal_driven_candidate",
    max_steps=8,
    max_tool_calls=6,
    # 1.0 runtime.py:1895 的硬收敛。实测小模型会反复改 query 重搜，把工具预算耗光后
    # 以空答案收场（e8c9d4 的 35 题里 52%）；收敛到 1 次后逼它翻页或直接作答。
    max_search_calls=1,
    # 见 ``max_page_calls`` 的注释：结果集最多 12 张、每页 12 张 → 一页看完；
    # 2 次是失败重试的余量。
    max_page_calls=2,
    # 1.0 生产 profile 的值：``PROFILES["goal_driven_candidate"].max_inspections = 3``
    # （``backend/agent_runtime/profile.py``）。**不是** ``budget_manager.py:17`` 的
    # dataclass 默认值 1 —— 那个只在没有 profile 覆盖时才生效，而 1.0 的
    # ``start_sentrix_api_8091.sh:66`` 明确设了 ``SENTRIX_AGENT_PROFILE=goal_driven_candidate``。
    #
    # 2.0 一开始抄成 1，真机 487 题的后果：一轮只能精读一张图，而 233 个
    # ``answered_with_gap`` 里未满足的需求集中在 ``visible_text``(103) 与
    # ``delivered_photo``(82)——都是要看多张图细节的类型。1.0 干净完成 96.7%，2.0 22.2%。
    #
    # ``inspect_photo`` 与 ``read_photo_text`` 都是 ``cost_class=EXPENSIVE``，共用一个
    # 计数器（1.0 亦然）。注意它约束的是**一轮总共几次**，不是「同一张图几次」——
    # 后者是工具描述里那句劝告，两者不是一回事。
    max_inspections=3,
    wall_time_s=180.0,
    final_reserve_s=10.0,
)
