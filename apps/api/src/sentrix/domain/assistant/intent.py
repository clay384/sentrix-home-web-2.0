"""规划：模型声明「回答这个问题最少需要哪些证据」，本模块只做校验与归一。

这是 1.0 ``backend/agent_runtime/goal_planner.py`` 的忠实移植。S3 曾经把它改成纯函数
（按问句里的关键词判规则），理由是「``planner_call_error`` 在生产轨迹里出现 1,974 次」。
审计推翻了这条理由，数据在 ``docs/refactor/s3-deviations.md`` 里，摘要：

- ``planner_call_error`` 只在 ``chat_fn`` **抛异常**时产生（1.0 ``goal_planner.py:135``
  的裸 ``except``）。1,974 条里 1,672 条 ``latency_s`` 落在 0.12 秒——那是连接被拒的
  形状，不是模型输出不好的形状；
- 它们全部挤在 15 个「一分钟内**所有**回合都失败」的黑障窗口里（439/439、96/96、
  393/396……），窗口之外整个 25,112 条的语料里只剩 47 条；
- 同样的窗口里，别的模型调用记的是 ``model_call_error: ...
  [Errno 111] Connection refused`` 与 ``timed out``；
- 非 PhotoBench 的 ``album3-v2`` 口径：0 / 763 = 0.0%。

那些窗口是模型端点不可用（PhotoBench 每跑一轮就 ``lifecycle.stop()`` → ``start()`` →
``bind-runtime`` 换一次模型），不是规划器设计有问题。所以规划还是一次模型调用，照 1.0。

**证据类型表是「有工具能满足的类型」那个集合。** S3 收窄到它那四个工具能产出的三类
（``photo_set`` / ``delivered_photo`` / ``person_profile``），理由写在下面那段历史里；
S6 随着工具补齐又加回四类（``structured_fact`` / ``photo_identity`` /
``visual_observation`` / ``visible_text``），现在七类。**收窄与放宽的都是词表**，
机制（模型声明 → 校验 → 带错误回灌重试）从 S3 起没动过。

1.0 的注册表有九类，因为 1.0 有十来个工具；**声明一个没有工具能满足的类型会让那一轮
必然以缺口收场**——所以这个表跟着工具走，不跟着 1.0 的公共表走。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from sentrix.domain.assistant.evidence_ledger import (
    REQUIREMENT_SCOPE_KEYS,
    EvidenceRequirement,
    EvidenceType,
    RequirementValueType,
)

# **一处对 1.0 与对 2.0 自己的双重偏离（2026-10-10 回退）。**
#
# 2026-10-09 曾把 ``delivered_photo`` 的声明条件收紧成「**只有**用户明确说要看图
# （给我看看 / 我想看 / 有没有…的照片）时才声明」——实测它把声明数从 212 道压到 **51 道**。
# 但那个方向是错的：**判据不是「用户有没有说要图」，而是「这是不是一道关于相册的真问题」**。
# 用户对着一张婚礼合影问「这是在哪儿拍的」，答案就该配着那张照片给出来——他没说
# 「给我看看」，但他显然要的是那张照片的答案。
#
# 现在退回的判据是 1.0 也没有的（1.0 是「只要声明 photo_set 就无条件同时声明」，
# 实测 212:3 说明那个太宽）：**非普通聊天就声明**。普通聊天的判据交给模型自己判——
# 它读得到整句话，比在这里跑关键词正则准。
#
# 1.0 ``goal_planner.py:228`` 允许「零需求」。S3 不允许：零需求会让模型一次工具都不调
# 直接作答——1.0 实测 29.3% 的回合零工具调用，JIT 提示词正是靠「当前没有待确认的证据
# 需求……可直接输出 final」把它们放过去的。这条是 S3 唯一一处对 1.0 规划行为的收紧，
# 理由是可验证的失败率，不是偏好。
MIN_REQUIREMENTS = 1

# 规划提示词。结构与 1.0 ``goal_planner.py:42-83`` 的 ``_DECLARATION_PROMPT`` 一一对应，
# 四段防御原样保留，因为它们各自对应一个实测出来的失败：
#
# 1. **示例里不出现真实的 scope_id。** 1.0 早期把 ``"scope_id":"{scope_id}"`` 写进示例，
#    小模型整段照抄，16 位十六进制的值抄错一位就触发 ``scope_mismatch``——实测长问题里
#    约 70% 因此失败。scope 是系统注入的，模型不产出它，示例里也不该出现。
# 2. **明确要求把尖括号占位符替换成真实内容。** 同上，模型会原样吐出 ``<用户目标>``。
# 3. **goal 必须用简体中文。** 英文目标会让语义检索与中文图片描述失配，召回直接失败。
# 4. **要求 JSON 完整闭合、不要围栏。** 解析器的修复能力有限（见 ``parse_json``），
#    提示词里先说清楚比事后补括号便宜。
_DECLARATION_PROMPT = """你正在规划家庭记忆任务。只返回一个 JSON 对象，不要解释，不要 Markdown 代码块围栏。

输出格式（字段值必须替换成这道题的实际内容，不要把示例原样输出）：
{{"action":"declare","declaration":{{"goal":"用户目标","requirements":[{{"id":"req_1","evidence_type":"证据类型","description":"具体要确认什么","predicate":"要查的谓词","value_type":"值类型","scope":{{}},"required":true}}]}}}}

不要输出 scope_id 字段 —— 相册范围由系统注入，你不需要也不应该填写；填任何值都会被判为错误。

evidence_type 只能来自系统注册表，不得创造新类型：
{evidence_types}

value_type 是「用户最终想要的值」，不是 evidence_type。只能取：
place / time / duration / amount / count / person / media_set / description / boolean / value
判定表（按用户问题的主要诉求选一个）：
- 是否 / 有没有 / 禁止 / 允许 / 是不是 -> boolean
- 哪一年 / 哪一天 / 什么时候 / 日期 / 几点 / 季节 -> time
- 在哪里 / 哪儿 / 地点 / 位置 / 举办地 / 拍摄地 -> place
- 多少钱 / 金额 / 价格 / 费用 / 花费 / 门票 / 小费 -> amount
- 几张 / 几个 / 几次 / 多少人 / 数量 / 总数 -> count
- 多久 / 多长时间 / 时长 / 停留 -> duration
- 谁 / 哪些人 / 姓名 / 身份 / 关系 -> person
- 全部记录 / 所有照片 / 找照片 / 找视频 / 合影 / 留影 -> media_set
- 做了什么 / 什么活动 / 描述 / 画面 / 场景 / 穿着 / 布置 / 动作 -> description
- 住院号 / 号码 / 客户 / 产品 / 结果 / 数值 / CRP -> value
- 哪几类 / 哪几种 / 哪些类别 / 哪些种类 / 什么类型 / 哪次活动 / 哪场活动 -> description
  即使句子里有地点，地点只放 scope，value_type 仍必须是 description。
问句里同时出现时间/地点和真正诉求时，value_type 由诉求决定，时间和地点只进 scope。
例如“2017年那场婚礼一共花了多少钱”的 value_type 是 amount，不是 time。
scope 的键只能是 event / time / place / person；能从问题确定的事件、地点、人物、时间必须写上，
不确定就输出空对象 {{}}；time 只放时间表达式（年份/日期/季节），地名不能塞进 time。
任何字段都禁止输出 <...> 占位符；scope 里禁止写“未知”“unknown”“n/a”。

只声明回答问题所必需的最小证据集合，不要把检索步骤本身当成答案证据重复声明。规则：
- goal 必须用简体中文描述用户目标（家庭记忆系统使用中文，英文目标会导致语义检索
  与中文图片描述失配、召回失败）；描述要保留完整语义，不要压缩成几个词。
- 只要用户问的是**关于这个相册/照片的真问题**（不是打招呼、闲聊、让写东西这类与相册
  无关的话），就声明 photo_set。**同时声明 delivered_photo** 表示「要把照片真的交到
  用户手上」，只取到候选不算交付。
  只有**与相册无关的普通聊天**（你好、谢谢、帮我写段文案）才不声明这两条。
- 问某个已确认人物是谁、什么家庭角色、要人物档案时，声明 person_profile。
  **问人与人的关系时同样要声明**：「A 和 B 是什么关系」「谁是谁的孩子/爸爸/妻子」
  「那次是谁陪着她」「都有谁一起去了」——只声明 photo_set 拿到的是照片，
  照片里没有「谁是谁的谁」这条信息，答不出来。
- **只有**问**全相册层面的数量/统计**（一共几张、多少次、哪些媒体类型、处理状态）
  时才声明 structured_fact。单张照片的拍摄时间、地点已经在检索结果的 preview 里
  （``captured_at`` / ``place``），**不要**为它们声明 structured_fact——先声明
  photo_set，必要时再声明 visual_observation 确认画面。
- `photo_set` 与 `delivered_photo` 是**一对基础需求**：只要声明其中一条，就同时声明
  另一条；这一对**不计入**下面的额度。除此之外，同一道题最多再声明 2 条证据需求，
  不要为了「找照片 + 查元数据 + 看细节」把预算堆满。
- **只有**问画面里看得见的**具体属性**——颜色、服饰、动作/道具、物体、建筑或雕塑外观——
  且检索结果的 preview 摘要不足以回答时，才声明 visual_observation。
  拍摄时间、地点和事件概述**优先用 photo_set 的 preview**（``captured_at`` / ``place`` /
  ``evidence_summary``），不要默认精读；preview 没写清楚时再声明 visual_observation。
- **只有**问画面中出现的**文字、数字、价格、招牌或标签内容**时才声明 visible_text。
  不要把地点、颜色、服饰、物体、场景这类视觉信息当成 visible_text——那会让系统只去读
  OCR，看不到照片本身。
- **只有**问照片里的人是谁、身份归属时才声明 photo_identity；不要用它去识别物体、
  场景、建筑或雕塑。
- 一条需求只对应一种证据类型。「先把照片找出来」和「看清照片里的细节」是两条需求，
  例如同时声明 photo_set 与 visual_observation；只声明 visible_text 通常答不了视觉细节。
- 同一 evidence_type 下可以按 value_type 拆成多条需求；但不要输出两条内容完全相同的 requirement。
每个 requirement 都必须能由注册表中的工具直接获得。
- JSON 示例中的字段值仅用于说明，不是要输出的固定值。
  goal、evidence_type、description、predicate、value_type、scope 必须替换为当前用户问题对应的真实内容；
  禁止原样输出示例里的任何占位内容或尖括号占位符。

不要调用工具，不要输出 SQL，不要直接回答用户。"""

# 1.0 ``goal_planner.py:85-103`` 的 ``_TYPE_MAP``：小模型常写的近义词 → 注册表里的类型。
# 1.0 的九类在 S3 收窄成三类，于是别名也一并映射到这三类（1.0 也是这么做的：它把所有
# 照片类别名都收进 ``memory_asset``）。认不出来的别名保持原样，交给校验去报
# ``invalid_planner_action``——静默改写成某个类型等于替模型编需求。
_TYPE_ALIASES: Mapping[str, str] = {
    "photo": EvidenceType.PHOTO_SET.value,
    "image": EvidenceType.PHOTO_SET.value,
    "video": EvidenceType.PHOTO_SET.value,
    "asset": EvidenceType.PHOTO_SET.value,
    "photo_set": EvidenceType.PHOTO_SET.value,
    "memory_asset": EvidenceType.PHOTO_SET.value,
    "delivery": EvidenceType.DELIVERED_PHOTO.value,
    "deliver": EvidenceType.DELIVERED_PHOTO.value,
    "delivered": EvidenceType.DELIVERED_PHOTO.value,
    "delivered_photo": EvidenceType.DELIVERED_PHOTO.value,
    "person": EvidenceType.PERSON_PROFILE.value,
    "identity": EvidenceType.PERSON_PROFILE.value,
    "profile": EvidenceType.PERSON_PROFILE.value,
    "person_profile": EvidenceType.PERSON_PROFILE.value,
    # S6（T123）补上的两个轴词。1.0 ``goal_planner.py:68-69`` 的
    # ``"visual" / "detail" → "visual_observation"``——收窄期（S3 只有三类）它们无处可去，
    # 现在 ``VISUAL_OBSERVATION`` 落地了，照 1.0 映射回来。
    "visual": EvidenceType.VISUAL_OBSERVATION.value,
    "detail": EvidenceType.VISUAL_OBSERVATION.value,
    "visual_observation": EvidenceType.VISUAL_OBSERVATION.value,
    # S6（T124）补的三个轴词。1.0 ``goal_planner.py:70-72``。**在这之前它们是
    # 认不出来的别名**，于是「招牌上写了什么」这类问题会让规划器报
    # ``invalid_planner_action`` 并重规划——`_TYPE_ALIASES` 的注释写着「认不出来的别名
    # 保持原样，交给校验去报」，这三条正是那种情况。
    "text": EvidenceType.VISIBLE_TEXT.value,
    "ocr": EvidenceType.VISIBLE_TEXT.value,
    "price": EvidenceType.VISIBLE_TEXT.value,
    "visible_text": EvidenceType.VISIBLE_TEXT.value,
}

_HEX_DIGITS = re.compile(r"^[0-9a-f]+$")
_ANGLE_PLACEHOLDER = re.compile(r"<[^>]*>")
_UNKNOWN_SCOPE_VALUES = frozenset({"未知", "unknown", "n/a", "na", "none", "null", "-"})


def planner_evidence_types() -> tuple[str, ...]:
    """给提示词用的证据类型清单。1.0 ``evidence_contract.planner_evidence_types`` 同形：
    稳定顺序，集合本身是唯一事实来源。"""
    return tuple(sorted(item.value for item in EvidenceType))


def build_prompt() -> str:
    """规划提示词的最终形态（把注册表渲染进去）。1.0 ``goal_planner.py:113`` 同形。"""
    evidence_lines = "\n".join(f"- {item}" for item in planner_evidence_types())
    return _DECLARATION_PROMPT.format(evidence_types=evidence_lines)


def planner_response_format() -> dict[str, Any]:
    """规划器的 JSON Schema。

    llama.cpp 的 schema 语法对 ``pattern`` / ``minLength`` 支持不稳定：复杂 schema 会直接
    500（实测 2026-10-26）。因此这里只约束**结构**（必填字段、枚举、布尔），文本层面的
    占位符与“未知”由 ``parse_declaration`` 再校验一次。``enable_thinking=false`` 同样
    必须一起发：思考 token 与语法约束冲突时，llama.cpp 会回
    “does not match the expected peg-native format”。
    """
    string = {"type": "string"}
    scope = {
        "type": "object",
        "properties": {
            "event": string,
            "time": string,
            "place": string,
            "person": string,
        },
        "additionalProperties": False,
    }
    requirement = {
        "type": "object",
        "properties": {
            "id": string,
            "evidence_type": string,
            "description": string,
            "predicate": string,
            "value_type": {"type": "string", "enum": [item.value for item in RequirementValueType]},
            "scope": scope,
            "required": {"type": "boolean"},
        },
        "required": [
            "id",
            "evidence_type",
            "description",
            "predicate",
            "value_type",
            "scope",
            "required",
        ],
        "additionalProperties": False,
    }
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "planner_declaration",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["declare", "revise"]},
                    "declaration": {
                        "type": "object",
                        "properties": {
                            "goal": string,
                            "requirements": {
                                "type": "array",
                                "minItems": MIN_REQUIREMENTS,
                                "items": requirement,
                            },
                        },
                        "required": ["goal", "requirements"],
                        "additionalProperties": False,
                    },
                },
                "required": ["action", "declaration"],
                "additionalProperties": False,
            },
        },
    }


def planner_chat_template_kwargs() -> dict[str, Any]:
    """规划调用关闭 thinking。其他调用方不受影响。"""
    return {"enable_thinking": False}


def history_prompt_suffix(history: str) -> str:
    """历史对话折进 system 文本，**不要再插一条 system 消息**。

    1.0 ``goal_planner.py:115-117`` 的注释：严格 chat template（Qwen3.5/3.8、llama.cpp
    的 Jinja）要求 system 必须在开头，插入第二条会直接 400/500
    （"System message must be at the beginning"）。实测 24 题（4.9%）因此失败。
    2.0 的 ``OpenAIChatModel`` 把这条约束做成了硬检查，所以这里必须折进正文。
    """
    return "\n\n历史对话背景：\n" + history if history else ""


def correction_hint(reason: str) -> str:
    """把上一轮的失败原因翻译成给模型的修正指令（重规划用）。1.0 ``_correction_hint``。"""
    by_reason = {
        "scope_mismatch": "不要输出 scope_id 字段。相册范围由系统注入，你填任何值都是错的。",
        "invalid_planner_action": (
            '输出的 JSON 结构必须是 {"action":"declare","declaration":{"goal":"...","requirements":'
            '[{"id":"req_1","evidence_type":"...","description":"..."}]}}，并且必须完整闭合'
            "（每个 { 都要有配对的 }）。"
        ),
        "no_requirements": (
            "至少要声明一条证据需求。零需求会让这一轮一次检索都不做——"
            "即使你认为可以直接回答，也要声明取到哪些证据才算数。"
        ),
        "invalid_requirement": (
            "每条 requirement 必须带 predicate、value_type、scope、required。"
            "value_type 只能取 place/time/duration/amount/count/person/media_set/"
            "description/boolean/value；required 必须是 true 或 false；"
            "scope 必须是对象且只能有 event/time/place/person 四个键，"
            "不确定就写空对象 {}；任何字段都不能保留 <...> 占位符或写“未知”。"
        ),
    }
    return (
        "你上一次的输出无法被解析（原因：{reason}）。{hint} "
        "另外：只输出一个 JSON 对象，不要解释、不要 Markdown 代码块围栏（```）；"
        "尖括号 <...> 是占位符，必须替换成这道题的实际内容，不要把占位符原样写出来。"
        "请重新输出修正后的 JSON。"
    ).format(reason=reason, hint=by_reason.get(reason, ""))


def parse_json(raw: str) -> dict[str, Any]:
    """把模型输出解析成一个对象；对常见畸形做**有限**修复。1.0 ``_parse_json`` 逐行同构。

    三处修复各自对应一个实测失败（1.0 的注释）：

    - 剥开头/结尾的 ``` 围栏。结尾那步必须独立：模型常给出「只有结尾 ```、没有开头」
      的输出。旧写法把两步嵌在同一个 if 里，结尾的 ``` 残留 → ``json.loads`` 在非末尾
      位置报错 → 下面「补一个收尾 }」的修复路径被跳过 → 直接判 invalid_planner_action。
      实测 5 题里 4 题因此失败（模型只是漏了根对象的 ``}``，本可自动补上）。
    - 从第一个 ``{`` 开始截。
    - 只在「错误位置就在最后一个字符附近」且根括号差一个时才补 ``}``——不能在任意
      畸形上乱补，那会把一个坏输出修补成一个看似合法的错输出。
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()
    start = text.find("{")
    if start < 0:
        raise ValueError("planner did not return JSON")
    candidate = text[start:]
    # **先取第一个完整对象。** 实测 2026-10-26：模型会在 ``</think>`` 后再重复输出一个
    # JSON，``json.loads`` 因 “Extra data” 整段失败，而第一个对象本身是合法的。
    # ``raw_decode`` 只读第一个；它失败（例如缺少收尾 ``}``）时再走下面那条有限修复。
    try:
        first, _ = json.JSONDecoder().raw_decode(candidate)
    except json.JSONDecodeError:
        first = None
    if isinstance(first, dict):
        return first
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        if exc.pos < len(candidate) - 1 or root_brace_balance(candidate) != 1:
            raise
        payload = json.loads(candidate + "}")
    if not isinstance(payload, dict):
        raise ValueError("planner action must be an object")
    return payload


def root_brace_balance(text: str) -> int:
    """未配对的对象花括号数——引号里的花括号不算语法（1.0 ``_root_brace_balance``）。"""
    balance = 0
    quoted = False
    escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == "{":
            balance += 1
        elif char == "}":
            balance -= 1
    return balance


def normalize_payload(payload: Mapping[str, Any], *, scope_id: str, default_goal: str) -> dict[str, Any]:
    """把小模型可能简化的 declaration 结构归一为严格 schema。1.0 ``_normalize_payload``。

    归一的三件事：补 ``scope_id``（模型不产出它）、补 ``goal``、把 ``requirements`` 里
    的字符串/近义字段拉平。去重按 ``(evidence_type, value_type, description)``：
    同一 evidence_type 下按 value_type 拆成多条是允许的，只有内容完全相同的才丢。
    """
    if not isinstance(payload, Mapping):
        return dict(payload) if isinstance(payload, Mapping) else {}
    data = dict(payload)

    decl = data.get("declaration")
    if not isinstance(decl, dict):
        if data.get("action") == "declare" and ("requirements" in data or "needs" in data):
            decl = dict(data)
            data = {"action": "declare", "declaration": decl}
        else:
            return data
    else:
        decl = dict(decl)
        data["declaration"] = decl

    if not decl.get("scope_id"):
        decl["scope_id"] = scope_id
    if not decl.get("goal"):
        decl["goal"] = default_goal

    raw_requirements = decl.get("requirements") or decl.get("needs") or []
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for index, item in enumerate(raw_requirements if isinstance(raw_requirements, list) else []):
        if isinstance(item, str):
            evidence_type = _TYPE_ALIASES.get(item.lower(), "photo_set")
            # 旧形状（字符串需求）没有 value_type，仍按 evidence_type 去重：
            # ["photo", "image"] 是同一个意思，不应变成两条。
            fingerprint = (evidence_type, "__legacy__", "")
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            normalized.append(
                {
                    "id": f"req_{index + 1}",
                    "evidence_type": evidence_type,
                    "description": item,
                    "required": True,
                }
            )
        elif isinstance(item, Mapping):
            evidence_type = str(item.get("evidence_type") or item.get("type") or "photo_set").strip()
            evidence_type = _TYPE_ALIASES.get(evidence_type.lower(), evidence_type)
            description = str(item.get("description") or item.get("desc") or "")
            value_type = str(item.get("value_type") or "").strip()
            fingerprint = (evidence_type, value_type, description)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            normalized.append(
                {
                    "id": str(item.get("id") or f"req_{index + 1}"),
                    "evidence_type": evidence_type,
                    "description": description,
                    # **不要在这里 ``bool()``。** ``None``/字符串会被悄悄变成
                    # ``False``/``True``，而 schema 要的是显式布尔；归一阶段保留原值，
                    # 由 ``parse_declaration`` 判非法并触发一次纠正。
                    "required": item.get("required", True),
                    "predicate": str(item.get("predicate") or ""),
                    "value_type": value_type,
                    "scope": item.get("scope", {}),
                }
            )
    decl["requirements"] = normalized
    return data


def parse_declaration(
    raw: str, *, scope_id: str, question: str
) -> tuple[tuple[EvidenceRequirement, ...] | None, str]:
    """解析并校验一次规划输出，返回 ``(需求, 失败原因)``。1.0 ``_parse_declaration``。

    失败原因就是重规划时回灌给模型的那句话的键，取值与 1.0 一致：
    ``invalid_planner_action`` / ``scope_mismatch`` / ``no_requirements``。
    """
    try:
        payload = parse_json(raw)
        payload = normalize_payload(payload, scope_id=scope_id, default_goal=question)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, "invalid_planner_action"

    declaration = payload.get("declaration")
    if payload.get("action") not in {"declare", "revise"} or not isinstance(declaration, Mapping):
        return None, "invalid_planner_action"
    if str(declaration.get("scope_id") or "") != scope_id:
        return None, "scope_mismatch"

    requirements: list[EvidenceRequirement] = []
    for item in declaration.get("requirements") or []:
        if not isinstance(item, Mapping):
            return None, "invalid_planner_action"
        try:
            evidence_type = EvidenceType(str(item.get("evidence_type") or ""))
        except ValueError:
            return None, "invalid_planner_action"

        description = str(item.get("description") or "").strip()
        predicate = str(item.get("predicate") or "").strip()
        raw_value_type = str(item.get("value_type") or "").strip()
        required = item.get("required", True)
        if not description or not predicate or _ANGLE_PLACEHOLDER.search(description):
            return None, "invalid_requirement"
        try:
            value_type = RequirementValueType(raw_value_type)
        except ValueError:
            return None, "invalid_requirement"
        if not isinstance(required, bool):
            return None, "invalid_requirement"

        raw_scope = item.get("scope", {})
        if not isinstance(raw_scope, Mapping):
            return None, "invalid_requirement"
        scope: list[tuple[str, str]] = []
        for key, value in raw_scope.items():
            key_text = str(key)
            if key_text not in REQUIREMENT_SCOPE_KEYS:
                return None, "invalid_requirement"
            value_text = str(value or "").strip()
            # **空值/未知/占位符的 scope 键直接丢弃，不让整条规划失败。**
            # 这些键本来就是可选的；模型写 ``person: ""`` 或 ``time: "未知"`` 只说明
            # 它没有这条信息，重试三次仍会得到同样的输出，最后变成 planner_blocked。
            # 丢弃不丢语义，保留其余可用的 scope。
            if not value_text or _ANGLE_PLACEHOLDER.search(value_text):
                continue
            if value_text.lower() in _UNKNOWN_SCOPE_VALUES:
                continue
            scope.append((key_text, value_text))

        requirements.append(
            EvidenceRequirement(
                id=str(item.get("id") or f"req_{len(requirements) + 1}"),
                evidence_type=evidence_type,
                description=description,
                required=required,
                predicate=predicate,
                value_type=value_type,
                scope=tuple(sorted(scope)),
            )
        )
    if len(requirements) < MIN_REQUIREMENTS:
        return None, "no_requirements"
    return tuple(requirements), ""


def requirements_of(goal_type: EvidenceType) -> EvidenceRequirement:
    """按类型造一条需求——给「规划失败但必须给出一条」的收尾用。"""
    descriptions = {
        EvidenceType.PHOTO_SET: "从相册里取到与问题相关的照片。",
        EvidenceType.DELIVERED_PHOTO: "把要展示的照片交接给用户，让它们真的能打开。",
        EvidenceType.PERSON_PROFILE: "读取这位已确认人物的家庭角色。",
    }
    return EvidenceRequirement(
        id=f"req_{goal_type.value}",
        evidence_type=goal_type,
        description=descriptions[goal_type],
    )
