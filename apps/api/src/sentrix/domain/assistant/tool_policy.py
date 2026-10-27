"""工具策略：一次工具调用要么被执行，要么被拒绝，要么命中缓存。

对应 1.0 ``agent_runtime/tool_policy.py``。1.0 的 ``authorize`` 只做四件事（白名单 /
只读 / readiness / 预算），其余收敛全部散在 runtime 主循环的十几个裸计数器里。2.0 把
它们**全部收进这一个决定**——于是「为什么这次调用没执行」有唯一答案，而不是要去主循环
里逐行找是哪一个 ``continue`` 拦下的。

1.0 实测出的四条收敛规则，逐条保留：

- ``search_memories`` 每回合最多 1 次（``runtime.py:1895``）。1.0 的实现有个边界漏洞：
  拦截条件里带了 ``and turn.budget.can_model_step()``，预算耗尽后拦截失效，**第二次
  search 会被真的执行**。这里把计数与预算分开，不存在这个短路。
- 同一工具连续 2 次没成功（失败或被拒，**不问原因**）→ 拦下并逼模型换动作
  （``runtime.py:3013`` 数的是「同一种失败原因」；这里的判据更宽，理由见 ``decide`` 里
  那段注释——预算类拒绝每次的原因都合法，但连试六次就是卡住）。
- 完全相同的调用走缓存回放，不重复执行、不计预算（``runtime.py:3035``）。
- 预算耗尽 → 拒绝（``tool_policy.py:37``）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from sentrix.domain.assistant.models import ToolArgumentError, ToolCall
from sentrix.domain.assistant.task_state import TaskState
from sentrix.domain.assistant.tools import TOOLS_BY_NAME, CostClass

# 同一工具连续几次没成功之后强制换动作。**数的是「没成功」，不是「同一个失败原因」**：
# 预算类拒绝每次的原因都完全合法，可连试六次就是卡住，而每次烧一个模型步。
MAX_CONSECUTIVE_FAILURES = 2


class ToolAction(StrEnum):
    EXECUTE = "execute"
    REPLAY = "replay"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class ToolDecision:
    action: ToolAction
    code: str = "ok"
    reason: str = ""
    arguments: Mapping[str, Any] = field(default_factory=dict)
    signature: str = ""
    replay: Mapping[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.action is not ToolAction.DENY


class ToolPolicy:
    """无状态：所有计数都在 ``TaskState`` 上。"""

    def decide(self, call: ToolCall, state: TaskState) -> ToolDecision:
        spec = TOOLS_BY_NAME.get(call.name)
        if spec is None:
            return ToolDecision(
                action=ToolAction.DENY,
                code="unknown_tool",
                reason=(f"没有名为 {call.name} 的工具。可用工具：" + "、".join(sorted(TOOLS_BY_NAME))),
            )

        try:
            arguments = call.parse()
        except ToolArgumentError as error:
            return ToolDecision(
                action=ToolAction.DENY, code="bad_arguments", reason=f"参数不是合法 JSON：{error}"
            )

        # **「缺」的判据是「键不在 / 是 null / 是空串」，不是「falsy」。**
        #
        # 原来的写法是 ``not arguments.get(name)``，于是**空数组也算缺**。这对
        # ``final_answer.selected_image_handles`` 是错的：``[]`` 是模型在明确回答
        # 「这一轮不交图」，那是一个有效的答案；把它判成「参数缺失」会让必填字段
        # 变成「必须交至少一张图」，而 1.0 的交付口径恰恰是**不做回填、空交付合法**。
        missing = [name for name in spec.required if arguments.get(name) is None or arguments.get(name) == ""]
        if missing:
            return ToolDecision(
                action=ToolAction.DENY,
                code="missing_argument",
                reason=f"{call.name} 缺少必填参数：{'、'.join(missing)}",
            )

        signature = TaskState.signature(call.name, arguments)
        cached = state.cached(signature)
        if cached is not None and state.replayed(signature):
            return ToolDecision(
                action=ToolAction.DENY,
                code="duplicate_call_stopped",
                reason="同一个调用已经回放过一次，结果不会变，请基于它作答。",
            )
        if cached is not None:
            # 1.0 也是回缓存 + 提示直接作答（``runtime.py:3035``）：重复调用不该白烧预算，
            # 但也不能让模型以为工具返回了「新东西」。
            return ToolDecision(
                action=ToolAction.REPLAY,
                code="duplicate_call",
                reason="这个调用刚刚已经执行过，结果与上次相同，请直接作答或换个动作。",
                arguments=arguments,
                signature=signature,
                replay=cached.model_visible,
            )

        # **「别再拿同一个工具重试」排在所有预算检查之前。**
        #
        # 这条顺序是有代价的实测教训：预算一旦用尽，每次调用都会命中同一个预算码返回，
        # 于是排在后面的 ``repeated_failure`` **永远走不到**——实测 309 次
        # ``inspection_budget_exhausted`` 而 ``repeated_failure`` 只 24 次。顺序反了，
        # 规则等于不存在。
        #
        # 判据用 ``trailing_failures``（**不管失败原因**）而不是 ``consecutive_failures``：
        # 后者要求「同一种错别犯第二次」，而这里问的是另一个问题——**这个工具是不是已经
        # 卡住了**。预算类拒绝每次的原因都完全合法，但连试六次就是卡住。
        if state.trailing_failures(call.name) >= MAX_CONSECUTIVE_FAILURES:
            return ToolDecision(
                action=ToolAction.DENY,
                code="repeated_failure",
                reason=(
                    f"{call.name} 已经连续 {MAX_CONSECUTIVE_FAILURES + 1} 次没有成功"
                    "（被预算拒绝或本身失败），再调用也不会有不同结果。"
                    "换一个动作，或者基于已经取到的内容作答。"
                ),
            )

        if call.name == "search_memories" and state.search_calls >= state.profile.max_search_calls:
            return ToolDecision(
                action=ToolAction.DENY,
                code="search_call_limit",
                reason=(
                    "search_memories 每轮只需调用一次。要看更多候选用 get_result_page 翻页"
                    "（result_set_id 用上一次返回的），不要改词重搜。"
                ),
            )

        # 翻页额度。**排在工具总预算之前**，与 ``search_memories`` 那条同一个位置与理由：
        # 命中总预算时回的是「别再调工具了」，而这里要说的是「这一份候选你已经看完了」——
        # 两句话让模型做的下一件事不一样。
        if call.name == "get_result_page" and state.page_calls >= state.profile.max_page_calls:
            return ToolDecision(
                action=ToolAction.DENY,
                code="page_call_limit",
                # 文案要说清**为什么够了**：结果集最多 12 张、每页 12 张，search 已经把
                # 全部候选给过。只说「到上限了」的话，模型会以为再等等就能翻——实测它
                # 正是这么一直翻到模型步耗尽的（56 个 step_budget_exhausted 里 53 个以
                # get_result_page 收尾）。
                reason=(
                    f"翻页额度已经用完（每轮最多 {state.profile.max_page_calls} 次）。"
                    "结果集最多 12 张、每页 12 张，search 已经把所有候选给过你，"
                    "这一份候选你已经看完了。"
                    "请基于已经看到的内容作答；确实需要别的照片就说明拿不到，不要继续翻页。"
                ),
            )

        if not state.can_call_tool():
            return ToolDecision(
                action=ToolAction.DENY,
                code="tool_budget_exhausted",
                reason="本轮的工具调用预算已经用完，请基于已有结果作答。",
            )

        # 1.0 ``tool_policy.py:37-38`` 的 ``can_tool_call(inspection=...)``。**单独一条、
        # 单独一个码**：它与上面那条是两笔预算（工具有 6 次，精读只有 1 次），
        # 合在一起拒就只能说一句「预算用完了」，而模型分不出是「别再调工具了」
        # 还是「这次可以换张图但不能再精读」。1.0 正是合着写的。
        if spec.cost_class is CostClass.EXPENSIVE and not state.can_inspect():
            return ToolDecision(
                action=ToolAction.DENY,
                code="inspection_budget_exhausted",
                # **次数从 ``state.profile`` 现渲染，不写死。** 这里原本写的是
                # 「一轮最多精读一张图」，而生产 profile 的额度是 3——额度从 1 改成 3
                # 时只改了值、没改文案，模型于是被一个与现实矛盾的告示误导，只能靠撞墙
                # 发现边界（实测这类拒绝 309 次）。会随 profile 变的数字不该出现在常量里。
                reason=(
                    f"本轮的照片级取证额度已经用完（每轮最多 {state.profile.max_inspections} 次，"
                    "精读与读字共用）。请基于已经看过的内容作答，不要反复检查同一张照片。"
                ),
            )

        return ToolDecision(
            action=ToolAction.EXECUTE,
            code="ok",
            arguments=arguments,
            signature=signature,
        )
