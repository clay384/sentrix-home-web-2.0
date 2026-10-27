"""结果集：一次检索的有序候选，以及它在对话里的分页视图。

对应 1.0 ``agent_runtime/result_set.py``。三条不变量原样保留，它们在 1.0 里被证明是
对的：

1. **每页 12 张、1-based、无游标、``page_size`` 硬顶 12**；
2. **``handle`` 是全局序号**（第 2 页从 ``photo_13`` 开始），跨页稳定——模型只需要记住
   ``photo_N``，分页不会让同一个 handle 指向不同的图；
3. **``asset_id`` 只存在于结果集里**，不进模型上下文。模型看到的是 handle，真正可交付
   的资产由运行时解引用。1.0 为此在 ``runtime.py:177-179`` 专门把 ``asset_id`` 从模型
   可见的观察里剥掉。

``handle`` → ``asset_id`` 的映射保留在结果集这一侧，于是「模型只能交付它真的看过的图」
是一个结构性事实，而不是靠提示词约束。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

#: 每页 12 张。**结果集上限是 24**（``tools.DEFAULT_SEARCH_LIMIT``）：第 1 页前 12 张，
#: 第 2 页从第 13 张开始直到结束。翻页工具是一次性的（成功一次后 JIT 剪掉），
#: 所以两页就是模型能看到的上限。
PAGE_SIZE = 12


@dataclass(frozen=True, slots=True)
class ResultSetEntry:
    handle: str
    asset_id: str
    # 融合层给这条候选的证据分级（``anchor`` / ``semantic`` / ``expander``）。
    # 空串的意思是「这个结果集没有带分级」——老结果集与测试直接造的结果集就是这种，
    # 它与 ``semantic`` 不是一回事：前者是「不知道」，后者是「判过、是相似度命中」。
    evidence_class: str = ""


@dataclass(frozen=True, slots=True)
class ResultSetPage:
    page: int
    page_size: int
    total: int
    has_more: bool
    remaining: int
    entries: tuple[ResultSetEntry, ...]


@dataclass(frozen=True, slots=True)
class ResultSet:
    id: str
    scope_id: str
    query: str
    asset_ids: tuple[str, ...]
    created_at: datetime
    # ``asset_id`` -> 证据分级。**与 ``asset_ids`` 平行，但用映射而不是平行数组**：
    # 平行数组在增删候选时会错位，而这里的键（``asset_id``）在结果集里本来就唯一。
    #
    # 存在结果集这一侧而不是翻页时重算：结果集是**一次检索的冻结快照**，而
    # ``get_result_page`` 读的是库里那一份——重算会得到与首次预览不同的分级，
    # 同一张图在第一页是 ``exact``、翻回来变成 ``strong``。
    evidence_classes: Mapping[str, str] = field(default_factory=dict)

    def page(self, page_no: int, page_size: int = PAGE_SIZE) -> ResultSetPage:
        """第 ``page_no`` 页（1-based）。页码越界返回空页，不抛异常。

        1.0 也是这个行为（``tools.py:2073``）：越界不是错误，是「没有更多了」。抛异常
        只会让模型把一次正常的翻到底当成系统故障。

        游标分页：第 ``n`` 页从 ``(n-1)*size`` 开始，越界页为空。候选不超过一页时
        第 2 页为空——工具层会直接拒绝这种请求（``single_page_only``）。
        """
        size = min(PAGE_SIZE, max(1, page_size))
        page_no = max(1, page_no)
        total = len(self.asset_ids)
        start = max(0, (page_no - 1) * size)
        end = min(total, start + size)
        entries = tuple(
            ResultSetEntry(
                handle=f"photo_{start + index + 1}",
                asset_id=asset_id,
                evidence_class=self.evidence_classes.get(asset_id, ""),
            )
            for index, asset_id in enumerate(self.asset_ids[start:end])
        )
        return ResultSetPage(
            page=page_no,
            page_size=size,
            total=total,
            has_more=end < total,
            remaining=max(0, total - end),
            entries=entries,
        )

    def first_page(self) -> ResultSetPage:
        return self.page(1)

    def resolve(self, handle: str) -> str | None:
        """``photo_N`` → ``asset_id``；不在本结果集里返回 ``None``。

        1.0 的授权端点正是靠这一步把「模型给的 handle」与「真实资产」隔开
        （``app.py:2484``，handle 必须属于该结果集且 scope 匹配）。
        """
        if not handle.startswith("photo_"):
            return None
        suffix = handle[len("photo_") :]
        if not suffix.isdigit():
            return None
        index = int(suffix) - 1
        if 0 <= index < len(self.asset_ids):
            return self.asset_ids[index]
        return None

    def handle_for(self, asset_id: str) -> str | None:
        for index, candidate in enumerate(self.asset_ids):
            if candidate == asset_id:
                return f"photo_{index + 1}"
        return None
