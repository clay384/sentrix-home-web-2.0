"""工具契约：名字、描述、参数 schema、能满足哪类证据、贵不贵（``cost_class``）。

S3 上线四个，S6 加了 ``query_memory_facts``（T121）、``query_photo_people``（T122）
与 ``inspect_photo``（T123），现在七个取证工具 + 一个终止动作（``final_answer``）。

对应 1.0 ``agent_runtime/tool_registry.py`` + ``tools.py`` 的注册段（``tools.py:2681``）。
两处与 1.0 的**结构性差异**：

1. **参数是标准 JSON Schema，走原生 ``tools`` 字段。** 1.0 的 ``ToolSpec.input_schema``
   是「示例默认值」的 dict（``{"query": "", "mode": "best|all|representative"}``），被
   ``json.dumps`` 之后拼进提示词的文本里（``runtime.py:1544``）——模型看到的是一段不像
   schema 的文字，而真正的契约藏在 handler 的手写 ``arguments.get(...)`` 里。
   实测 8100 的 llama.cpp（gemma4 模板）原生支持 ``tools`` / ``finish_reason="tool_calls"``，
   所以 2.0 直接用原生协议：没有 JSON 修复器，没有「输出无法解析」的收尾。
2. **``satisfies`` 是必填的。** 1.0 的 ``produces_evidence`` 只是声明，从没有任何地方
   校验工具真的产出了它声称的证据。2.0 用它驱动账本（见 ``evidence_ledger.py``）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sentrix.domain.assistant.evidence_ledger import EvidenceType


class CostClass(StrEnum):
    """一次工具调用有多贵。**它不是一个标签，是一条预算的开关。**

    1.0 用的是裸字符串（``cost_class="expensive"``，``tools.py:2755`` 与 ``:2769``），
    而它唯一的用途是喂给 ``BudgetManager``：

        self.budget.can_tool_call(inspection=spec.cost_class == "expensive")   # tool_policy.py:38
        self.budget.record_tool_call(inspection=spec.cost_class == "expensive")  # :62

    而 ``BudgetState.max_inspections = 1``（``budget_manager.py:17``）——所以
    ``"expensive"`` 的实际含义是「**它算一次精读，而一轮总共只准精读一次**」。
    2.0 收成枚举（同 ``EvidenceType`` 的理由）：拼错的 ``"expensiv"`` 在 1.0 里会
    静默退化成``cheap``、那条预算永不触发，而这里会当场报错。
    """

    CHEAP = "cheap"
    EXPENSIVE = "expensive"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    # **JIT 剪枝时发出去的那一句**，一行说清「这个工具干什么、什么时候用」，≤40 字。
    # 对应 1.0 ``jit_prompt.py:33`` 的 ``LITE_TOOL_SCHEMAS``（每工具 40 token 以内）。
    #
    # 两句话都要维护是有意的：`description` 是**唯一一次**把边界讲清楚的地方
    # （信息边界、失败语义、反例），`brief` 是**每一转**都要付的固定成本。把两者
    # 合成一句，要么边界讲不清，要么每转多花几百 token。
    #
    # 没有默认值：漏写一句 brief 会让那个工具在剪枝模式下失去描述，而那是静默的。
    brief: str
    parameters: Mapping[str, Any]
    satisfies: tuple[EvidenceType, ...]
    required: tuple[str, ...] = ()
    # 默认便宜：1.0 的 ``ToolSpec`` 默认值就是 ``"cheap"``，只有两个视觉工具是昂贵的。
    cost_class: CostClass = CostClass.CHEAP

    def as_openai_tool(self) -> Mapping[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.parameters),
            },
        }


def _object_schema(properties: Mapping[str, Any], required: Sequence[str]) -> Mapping[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


SEARCH_MEMORIES = ToolSpec(
    name="search_memories",
    brief="按内容词检索照片，返回带地点/时间/人物/描述的候选预览。",
    description=(
        "按内容词检索家庭照片（人/物/场景/衣着/颜色/地点特征）。"
        "返回一个结果集摘要，其中 preview 是排在最前的几张。"
        "每条 preview 自带 captured_at（拍摄时间）/ place（**拍摄地的行政区划**，"
        "形如「河北省石家庄市正定县 · 正定北门里村附近」，由照片的 GPS 反解而来；"
        "没有 GPS 的照片这一格为空）/ people（已确认人物）/ "
        "people_count（这张照片上一共几个人，含未确认的同行者）/ "
        "media_kind（image 还是 video）/ source_kind（photo 是照片，video_keyframe "
        "是**从视频里抽出来的一帧**——用户问视频时，命中的就是这一类）/ "
        "level（相关强度：exact 命中确定性条件、"
        "strong 语义相近、approximate 由邻近扩展）/ scene_hint（画面里的场景描述，"
        "如「户外水泥地面」——**它是看到的画面，不是地点，别拿它当地名回答**）/"
        "evidence_summary（画面内容、物品、衣着与图内文字）——**单张照片的地点、时间、"
        "人物、画面内容直接在 preview 里读，不要为此再调用其他工具**。"
        "问「在哪里拍的」就用 place 里的行政区名回答，不要用 scene_hint。"
        "返回值里另有 group_photo_count / group_photo_sizes / group_photo_rows："
        "**这一页里有几张多人照片、各是几人**，已经替你数好了，问「几张合影/各是几人」"
        "直接引用它们，不要自己数 preview。"
        "一次对话只需要检索一次；检索按融合排序返回最多 24 张候选，本页显示前 12 张。"
        "total 是本次检索的候选数（上限 24）——**它不是相册里该对象的真实数量，"
        "不要拿它回答数量题**。候选超过 12 张时可用 get_result_page 看第 13 张起的部分"
        "（每回合只能翻一次）；不超过 12 张时不会出现翻页工具。不要改词重搜。"
        "要把照片展示给用户，在 final_answer 的 selected_image_handles 里填这些 handle。"
    ),
    parameters=_object_schema(
        {
            "query": {
                "type": "string",
                "description": "检索用的内容词，例如 海边、桥、猫、红色。用用户问题里的实词，不要写整句。",
            },
            "limit": {
                "type": "integer",
                "description": "本次检索保留多少张候选，默认 24、最多 24。",
                "minimum": 1,
                "maximum": 24,
            },
        },
        ["query"],
    ),
    satisfies=(EvidenceType.PHOTO_SET,),
    required=("query",),
)

GET_RESULT_PAGE = ToolSpec(
    name="get_result_page",
    brief="看结果集第 13 张起的部分；只有一次机会，候选不超过 12 张时没有这个工具。",
    description=(
        "查看结果集的第 2 页（**从第 13 张开始直到最后**）。只有本次检索的候选超过 12 张时"
        "才有第 2 页；不超过 12 张时这个工具不会出现。"
        "**每回合只能翻一次**：成功返回后工具就会消失，所以 page 必须是 2，"
        "不要用它重读第 1 页（第 1 页就是 search_memories 的返回）。"
        # **2026-10-09 的引导保留一句。** 那一轮 56 个以 ``step_budget_exhausted``
        # 收尾的回合里 53 个最后动作都是翻页——模型被 L2 打回之后不知道说什么就翻页，
        # 而越界页返回空仍是 ``ok``，它拿不到「到底了」的信号。
        "翻到没有内容的页说明已经到底，不要换页码再试，也不要靠翻页找「更合适的」。"
        "翻页次数每轮有上限，超出的调用会被拒绝。"
    ),
    parameters=_object_schema(
        {
            "result_set_id": {"type": "string", "description": "search_memories 返回的结果集 id。"},
            "page": {"type": "integer", "description": "页码，从 1 开始。", "minimum": 1},
        },
        ["result_set_id", "page"],
    ),
    satisfies=(EvidenceType.PHOTO_SET,),
    required=("result_set_id", "page"),
)

#: 终止动作的工具名。**交付是它的一个字段，不是一次单独的工具调用**——这是 1.0 的
#: 形状（``runtime.py:309`` 的 ``{"action":"final","answer":...,"selected_image_handles":[...]}``）。
#: 单独放一个 ``get_original_photos`` 会多花一个模型步，而 1.0 的实测是：它被调用了 **0 次**，
#: 239 道图片题全部走 final 字段交付。留成常量是因为 ``final_guard`` 与 ``run_turn``
#: 都要按名字认它。
FINAL_ANSWER_TOOL = "final_answer"

#: 一轮最多交付几张。**1.0 ``_normalize_selected_image_handles`` 是硬截断的**
#: （``runtime.py:248-249``：凑够 ``limit`` 就 ``break``），2.0 此前只在 JSON Schema
#: 里写了 ``maxItems``——那依赖端点遵守 schema，而 ``deliver`` 自己不校验。
#: 常量放这里是因为它同时约束两处：schema 的 ``maxItems`` 与交付时的截断。
MAX_DELIVERED_PHOTOS = 6

FINAL_ANSWER = ToolSpec(
    name=FINAL_ANSWER_TOOL,
    brief="给出最终回答并结束本轮；要展示的照片填进 handles。",
    description=(
        "给出最终回答并结束本轮。answer 是要说给用户的那段话；"
        "要把照片展示给用户，就在**同一次调用**里把 handle 填进 selected_image_handles。"
        "handle 必须来自当前结果集（例如 photo_1），最多 6 张；"
        "搜索返回的全部候选不能直接当作要展示的图片。不展示照片就留空数组。"
    ),
    parameters=_object_schema(
        {
            "answer": {
                "type": "string",
                "description": "给用户的最终回答，用中文。",
            },
            "selected_image_handles": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    f"最终确实要展示给用户的 handle（photo_1…），最多 {MAX_DELIVERED_PHOTOS} 张，"
                    "必须来自当前结果集；不展示照片就填空数组。"
                ),
                "maxItems": MAX_DELIVERED_PHOTOS,
            },
        },
        # **``selected_image_handles`` 是必填的，这是一个 1.0 做不到的收紧。**
        #
        # 1.0 的交付字段长在模型的正文 JSON 里，dict 缺键不报错——实测它的 final 动作
        # 有 **239/487（49%）** 根本不带这个字段，而零交付的 210 道里 82% 是"候选里
        # 已经有正确答案、模型却没选"。2.0 走标准 JSON Schema，可以把"交不交图"变成
        # 一个**必须显式回答**的问题：不交就写 ``[]``。
        #
        # 允许空数组是这条收紧能成立的前提（见 ``tool_policy`` 里「缺」的判据）：
        # 产品口径是「模型没选 → 交付为空合法」，不是「必须至少交一张」。
        ["answer", "selected_image_handles"],
    ),
    # **声明了 ``DELIVERED_PHOTO``，但 ``run_turn`` 只在真的交出图时才销账**：
    # 「声明要交付」与「交付了」不是一回事，一张都没交时销账会让那一轮假装不缺证据。
    satisfies=(EvidenceType.DELIVERED_PHOTO,),
    required=("answer", "selected_image_handles"),
)

GET_PERSON_PROFILE = ToolSpec(
    name="get_person_profile",
    brief="读取一位已确认人物的家庭归属、人物关系与画像。",
    description=(
        "读取一位**已确认人物**的家庭归属、人物关系与已有画像。"
        "关系来自家庭图谱，逐条标了来源（模型推断或用户确认）。"
        "没有已记录的关系时 relationships 是空数组——那只表示这个人还没有关系数据，"
        "**不要**据此说查不到这个人。"
        "常去地点、行为规律、近期事件与语义声明尚未建立，问到时如实说明数据不足，不要编造。"
    ),
    parameters=_object_schema(
        {"person": {"type": "string", "description": "人物名字，例如 妈妈、浦思航。"}},
        ["person"],
    ),
    satisfies=(EvidenceType.PERSON_PROFILE,),
    required=("person",),
)

QUERY_MEMORY_FACTS = ToolSpec(
    name="query_memory_facts",
    brief="精确统计相册（数量/最早最晚/按月分组），不走相似度检索。",
    description=(
        "精确统计当前相册，答案是一个确定的数字或时间，不经过相似度检索。"
        "能回答：有多少张原始照片/视频、某个已确认人物出现在多少张照片里、"
        "最早或最近一次拍摄是什么时候、按月/地点/媒体/处理状态分组各有多少。"
        "颜色/物体/场景/活动/关系/金额/桌数这类条件本工具答不了，不要拿它硬套；"
        "也不要把 search_memories 返回的候选数当成统计答案——那个数是候选池的大小，"
        "不是总数。当前相册范围由系统注入。"
    ),
    parameters=_object_schema(
        {
            "operation": {
                "type": "string",
                "enum": ["count", "exists", "first", "last", "group"],
                "description": "count 数量；exists 有没有；first 最早一次；last 最近一次；group 分组计数。",
            },
            "subject": {
                "type": "string",
                "enum": ["asset", "person_appearance", "processing"],
                "description": (
                    "统计什么：asset 原始照片/视频；"
                    "person_appearance 某位已确认人物出现过的照片；processing 导入处理状态。"
                ),
            },
            "group_by": {
                "type": "string",
                "enum": ["month", "place", "media", "status"],
                "description": "只在 operation=group 时需要。status 只能配 subject=processing。",
            },
            "filters": _object_schema(
                {
                    "time": {
                        "type": "string",
                        "description": (
                            "时间说法，例如 去年、2023年7月、前年夏天。"
                            "相对时间原样填，由系统换算，不要自己算年份。"
                        ),
                    },
                    "media": {
                        "type": "string",
                        "enum": ["image", "video"],
                        "description": "只统计照片或只统计视频；不区分就省略。",
                    },
                    "place": {
                        "type": "string",
                        "description": (
                            "结构化地名（城市/区县/景区/地标），例如 杭州、邓村。"
                            "活动、主题、目标不要填进来，不确定就留空。"
                        ),
                    },
                    "person": {
                        "type": "string",
                        "description": "用户明确提到的已确认人名。角色词（伴娘/兄弟/亲戚）不要填。",
                    },
                },
                [],
            ),
        },
        ["operation", "subject"],
    ),
    satisfies=(EvidenceType.STRUCTURED_FACT,),
    required=("operation", "subject"),
)


QUERY_PHOTO_PEOPLE = ToolSpec(
    name="query_photo_people",
    brief="读一张候选照片的已确认人物与未确认同行者。",
    description=(
        "读取一张已检索照片自己的已确认人物和未确认同行者。"
        "asset_handle 必须来自当前 search_memories preview；不会把其他候选照片的人名移植过来。"
        "未确认的人**没有名字**，只报个数，不要给他们安名字。"
        # 1.0 这里逐个人回的是 `get_effective_family_membership`（家庭图谱，S7）。
        # 2.0 没有图谱，只有实体上的 family_role——描述里如实说清楚，
        # 而不是让模型把 family_role 当成「这一家的人」去用。
        "membership 一栏来自人物实体上的家庭角色，**尚无关系图谱**，"
        "不要据此推断两个人之间的亲属关系。"
    ),
    parameters=_object_schema(
        {
            "result_set_id": {
                "type": "string",
                "description": "search_memories 或 get_result_page 返回的结果集 id。",
            },
            "asset_handle": {
                "type": "string",
                "description": "要查的那张照片的 handle（photo_1…），必须来自当前结果集的 preview。",
            },
        },
        ["result_set_id", "asset_handle"],
    ),
    # 两条都声明，理由见 ``evidence_ledger.EvidenceType.PHOTO_IDENTITY`` 的注释：
    # ``PHOTO_IDENTITY`` 是本工具自己的产物，``PERSON_PROFILE`` 是 1.0 那条
    # ``confirmed_identity`` 在 2.0 的名字——只声明前者的话，「问照片里的人是谁」
    # 这类需求在本工具被调用之后依然开着，门禁会逼模型再去找一个查不到的东西。
    # **只销 ``photo_identity`` 的账，不销 ``person_profile``。**
    #
    # 它给的是「这张照片里有谁」加上每个人的**角色标签**（``family_role``：大宝 /
    # 侄子 / 母亲），而 ``person_profile`` 要的是**人与人的关系边**（谁是谁的谁）与
    # 档案摘要——它自己的描述里还明说「不要据此推断两个人之间的亲属关系」。
    # 此前两者都销，于是模型一调它，``person_profile`` 那一格就被关掉，
    # ``get_person_profile`` 从此不在可用工具里。实测 2026-10-16 那轮：14 个声明了
    # ``person_profile`` 的回合里 **13 个**是这么被抢先销账的（有一回它返回
    # ``people: []`` 也照样销），**全 487 题 ``get_person_profile`` 调用 0 次**。
    satisfies=(EvidenceType.PHOTO_IDENTITY,),
    required=("result_set_id", "asset_handle"),
)


INSPECT_PHOTO = ToolSpec(
    name="inspect_photo",
    brief="复核单张照片的视觉细节（物体/衣着/颜色/场景）。昂贵。",
    description=(
        "复核已检索照片的视觉细节（物体/衣着/颜色/场景）。"
        "asset_handle 使用 search_memories preview 里的 handle（photo_1…），可省略（默认用预览第一张）。"
        "当前 handle 不含目标时换下一张（photo_2、photo_3…）复核，同一张图最多复核一次，不要反复检查同一张。"
        # **这里刻意不写具体次数。** 1.0（与本文件此前）写的是「默认每轮最多 1 次」，
        # 而生产 profile 的额度是 3——**指示与现实矛盾**，模型只能靠撞墙发现边界，
        # 每撞一次烧一个模型步（实测这类拒绝 309 次）。次数是会随 profile 变的运行时
        # 事实，schema 是静态的；把它写在 schema 里就注定要漂。真实额度改由
        # ``ToolPolicy`` 在拒绝时按 ``state.profile`` 现渲染。
        "昂贵：本轮有次数上限，超出的调用会被拒绝，那时请基于已看过的内容作答。"
    ),
    parameters=_object_schema(
        {
            "result_set_id": {
                "type": "string",
                "description": "search_memories 或 get_result_page 返回的结果集 id。",
            },
            "question": {
                "type": "string",
                "description": "要用这张图回答的问题，用用户问题里的实词，不要改写成别的意思。",
            },
            "asset_handle": {
                "type": "string",
                "description": "要复核的那张照片的 handle（photo_1…）。省略则用结果集第一张。",
            },
            "target_person": {
                "type": "string",
                "description": "问题只针对某一位已确认人物时填他的名字；不针对某个人就省略。",
            },
        },
        ["result_set_id", "question"],
    ),
    # 1.0 ``tools.py:2756`` 的两条。``visual_observation`` 是「看细节」，
    # ``photo_identity`` 是「这是谁」——两个不同的需求都可能被这一次调用满足。
    satisfies=(EvidenceType.VISUAL_OBSERVATION, EvidenceType.PHOTO_IDENTITY),
    # 1.0 的 ``required_inputs=("asset_handle", "question")`` 与它自己的描述
    # （「asset_handle …可省略」）**互相矛盾**。2.0 保留描述里那条行为（省略就用第一张），
    # 所以这里只把 ``question`` 与 ``result_set_id`` 列为必填——照 required_inputs 抄会
    # 让「可省略」当场失效（策略层会以 ``missing_argument`` 拒掉）。
    required=("result_set_id", "question"),
    cost_class=CostClass.EXPENSIVE,
)


READ_PHOTO_TEXT = ToolSpec(
    name="read_photo_text",
    brief="读取这张照片已存下的文字（招牌/价格/店名/年份）。昂贵。",
    description=(
        "读取这张照片**已经存下来的**文字内容（菜单/价格/招牌/店名/电话/年份/小字）。"
        "适用于「多少钱/价格/售价/店名/招牌/电话/写了什么/什么字/哪一年」等需要看照片文字的题。"
        # **这一句是 T124 裁决的落点。** 1.0 的描述写着「内部会把照片切块放大后 OCR」，
        # 而那条路在 2.0 不存在（1.0 靠 PaddleOCR，见 behaviour-baseline 的
        # 「照片人物取证」一节）。描述里不如实说明，模型就会把「没读到」当成「图上没有」。
        "文字是导入时由视觉模型抽取并落库的，本工具**不再重新识别**："
        "没存下就如实说读不到，不要据此断言照片里没有字。"
        "asset_handle 用 search_memories preview 里的 handle，可省略（默认预览第一张）。"
    ),
    parameters=_object_schema(
        {
            "result_set_id": {
                "type": "string",
                "description": "search_memories 或 get_result_page 返回的结果集 id。",
            },
            "asset_handle": {
                "type": "string",
                "description": "要读的那张照片的 handle（photo_1…）。省略则用结果集第一张。",
            },
        },
        ["result_set_id"],
    ),
    # 1.0 ``tools.py:2768`` 的 ``produces_evidence=("visible_text",)``。
    satisfies=(EvidenceType.VISIBLE_TEXT,),
    # **一处与 1.0 不同：不收 ``question``。** 1.0 的 ``required_inputs`` 里有它，而它
    # 在 1.0 唯一的用途是决定要不要对小模型的结果做一次补偿重识别
    # （``_HARD_VALUE_QUESTION_RE``，``ocr_tool.py:584-593``）。2.0 这条路不重识别，
    # 那个参数没有任何用处——收一个用不上的参数只会让模型多花几个 token 去填它。
    #
    # **``cost_class`` 回到 1.0 的 ``EXPENSIVE``。** S6/T124 曾把它降成 ``CHEAP``，
    # 理由是"2.0 读的是已落库的一列文本，照抄 expensive 会吃掉 ``inspect_photo``
    # 的精读预算"。**那条理由的前提是 ``max_inspections = 1``。** 现在是 3，而代价
    # 变成了实测的这一面：CHEAP 的工具**不受任何单轮次数约束**，只受 6 次工具总预算
    # 管——连读 6 张就把工具预算与模型步一起用光（`album3v4-026` 连调 6 次）。
    #
    # 恢复昂贵之后两者共用「每轮 3 次照片级取证」，这不是两件事被硬凑到一起：
    # 它们本来就是同一件事——**对某一张照片做一次取证**。1.0 的 ``budget_unit="image"``
    # 说的就是这句话。
    required=("result_set_id",),
    cost_class=CostClass.EXPENSIVE,
)


TOOL_SPECS: tuple[ToolSpec, ...] = (
    SEARCH_MEMORIES,
    QUERY_MEMORY_FACTS,
    QUERY_PHOTO_PEOPLE,
    INSPECT_PHOTO,
    READ_PHOTO_TEXT,
    GET_RESULT_PAGE,
    GET_PERSON_PROFILE,
    FINAL_ANSWER,
)

TOOLS_BY_NAME: Mapping[str, ToolSpec] = {spec.name: spec for spec in TOOL_SPECS}

# 能产出一组照片的检索工具。门禁用它判断「有没有检索过」。
RETRIEVAL_TOOLS = frozenset({"search_memories", "get_result_page"})


def openai_tools(specs: Sequence[ToolSpec] = TOOL_SPECS) -> tuple[Mapping[str, Any], ...]:
    """把契约渲染成端点的 ``tools`` 字段。

    **没有 ``brief`` 开关**：精简与否在 ``jit.select_tools`` 就定好了（它返回的 spec
    其 ``description`` 已经是 ``brief``）。多一个开关等于给「发哪一份描述」留两个
    真相，而选错的那一个不会报错，只会让每一转多付两千多 token。
    """
    return tuple(spec.as_openai_tool() for spec in specs)
