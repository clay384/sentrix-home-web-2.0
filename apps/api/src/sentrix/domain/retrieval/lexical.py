"""词法召回的纯规则：预分词、字段映射、命中计分。零 IO。

对应 1.0 ``backend/retrieval_indexes.py:31-80``（``_normalize`` / ``pre_tokenize`` /
``_fts_content`` / ``_terms_from_field`` / ``_terms_from_detail``）与
``:296-334``（``search_fts`` 的计分）。

**这是命中计数，不是 BM25。** 记分规则只有一条：

    每条 query token 独立匹配，按**命中的不同 token 数**累加；
    当命中数恰好为 1 且那一个 token 正好等于整串 query 且库里有 ``normalized_value``
    完全相等的行时，额外 +2.0。

1.0 把 FTS5 当**倒排索引**用——``observation_search_fts`` 只索引 ``tokens`` 一列，
排序由 Python 侧算，``bm25()`` 一次都没被调用过。2.0 保持这个分工：FTS5 负责
「哪些 asset 含这个 token」，分数由本模块算。**不要引入 BM25**——它会换掉一套已经
在真实相册上校准过的排序，而校准数据不在 2.0 里。

单字永远不是 token（``pre_tokenize`` 里 ``len(value) > 1`` 才收整串，中文只取 bigram），
所以一个「色」字查询不可能被当成一条证据。这条是 1.0 Phase R P0-1 的明确决定。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 1.0 ``retrieval_indexes.py:20``。七个字段全都建——``person_bridge`` 在 S5b 之前
# 是唯一缺席的一个（身份层属于 S5），现在 ``Observation.people`` 有了值源。
#
# **值源那一格与 1.0 有一处不同，是修 1.0 的缺陷**：1.0 的 ``_terms_from_field`` 对
# 列表元素一律 ``_normalize(item)``，而 ``people`` 的元素是
# ``{"entity_id":…, "name":…, "status":…}`` 这样的 dict——``str(dict)`` 写进
# ``person_bridge`` 之后，按人名去查（``normalized_value = '妈妈'``）永远查不中。
# 1.0 里真正干活的是 ``EntityRetriever`` 那条「投影查不到就退回直接读观察」的兜底。
# 2.0 的值源是**名字**（``Observation.people`` 已经是名字元组），投影查得中，兜底那条
# 路仍然照搬（它还有别的用处：投影还没建起来的库）。见 ``s5-deviations.md``。
FIELD_SOURCES: tuple[tuple[str, str], ...] = (
    ("place", "place"),
    ("activity", "activity"),
    ("caption", "caption"),
    ("source_text", "source_text"),
    ("object", "objects"),
    ("clothing", "clothing"),
    ("ocr", "ocr_text"),
    ("person_bridge", "people"),
)

# 1.0 ``retrieval_indexes.py:225-245`` 的 asset 级锚点：观察文本里没有地名/时间/文件名
# （caption 是场景描述），但它们又必须能被词法召回命中。
ANCHOR_FIELD_TYPES: tuple[str, ...] = ("geo", "time", "file")

# 1.0 ``:318``：``exact_boost = 2.0 if count == 1 and whole and self._whole_is_exact(...)``。
EXACT_BOOST = 2.0

# 1.0 ``:226``／``:242``：锚点列的 source_type 与置信度。
ANCHOR_CONFIDENCE = 0.9
ANCHOR_SOURCE_TYPES: dict[str, str] = {
    "geo": "asset_geo",
    "time": "asset_time",
    "file": "asset_file",
}

_WHITESPACE_RE = re.compile(r"\s+")
_LATIN_RE = re.compile(r"[a-zA-Z0-9]+")

# 1.0 ``:245``：事件封面与视频关键帧的文件名不是用户内容，写进索引只会污染召回。
_FILE_STEM_EXCLUDED_PREFIXES = ("event_event_", "kf_")


def normalize_text(text: object) -> str:
    """1.0 ``_normalize``：折叠空白 + 去首尾 + 小写。"""
    return _WHITESPACE_RE.sub(" ", str(text or "").strip().lower())


def pre_tokenize(value: object) -> tuple[str, ...]:
    """整串 + 拉丁词 + 中文 bigram，排序去重。**单字不是 token。**

    ``浅黄色毛绒睡衣`` → ``["浅黄", "色毛", "毛绒", "浅黄色毛绒睡衣", "绒睡", "睡衣", "黄色"]``
    （``sorted`` 之后按码位排列）。单个汉字不会出现，所以孤立的「色」/「毛」
    永远不会被算作支持。
    """
    text = normalize_text(value)
    if not text:
        return ()
    tokens: set[str] = set()
    if len(text) > 1:
        tokens.add(text)
    tokens.update(_LATIN_RE.findall(text))
    chars = [char for char in text if not char.isspace()]
    for index in range(len(chars) - 1):
        tokens.add(chars[index] + chars[index + 1])
    return tuple(sorted(tokens))


def fts_content(value: object) -> str:
    """写进 FTS5 ``tokens`` 列的形态：空格分隔的预分词结果。"""
    return " ".join(pre_tokenize(value))


def query_tokens(query: str) -> tuple[str, ...]:
    """一次查询展开成的 token 列表。``pre_tokenize`` 的第一个 token 就是整串。"""
    return pre_tokenize(query)


def score_token_hits(
    token_hits: dict[str, int], *, whole_query: str, exact_assets: frozenset[str]
) -> list[tuple[str, float]]:
    """按命中 token 数排序。

    ``exact_assets`` 是「库里存在与整串 query 完全相等的 ``normalized_value``」的
    asset 集合——那是唯一需要查库的一步，所以由调用方算好传进来。

    ``whole_query`` 为空（查询本身分不出 token）时不给 boost：1.0 的
    ``whole and ...`` 短路条件就是为这个。
    """
    whole = normalize_text(whole_query)
    scored: list[tuple[str, float]] = []
    for asset_id, count in token_hits.items():
        boost = EXACT_BOOST if (count == 1 and whole and asset_id in exact_assets) else 0.0
        scored.append((asset_id, count + boost))
    # 同分按 asset_id 升序——1.0 用 ``sort(reverse=True)`` 只排分数，
    # 同分顺序取决于 dict 插入序，同一份数据两次跑可能不同。见 s4-deviations.md §2 第 8 项。
    return sorted(scored, key=lambda item: (-item[1], item[0]))


def file_stem_is_indexable(file_name: str) -> bool:
    """1.0 ``:245``：``stem`` 非空且不以事件封面/关键帧前缀开头。"""
    stem = str(file_name or "").rsplit(".", 1)[0] if "." in str(file_name or "") else str(file_name or "")
    if not stem:
        return False
    return not stem.lower().startswith(_FILE_STEM_EXCLUDED_PREFIXES)


def time_anchor_tokens(captured_at: str) -> tuple[str, ...]:
    """1.0 ``:236-239``：``captured_at`` 的 ``YYYY`` / ``YYYY-MM`` / ``YYYY-MM-DD``。

    「按日期找照片」的查询要能命中，而这三段在观察文本里都不存在。少于 7 个字符
    （``YYYY-MM``）的时间串整段跳过——1.0 的阈值就是 ``len(captured) >= 7``。
    """
    captured = str(captured_at or "")
    if len(captured) < 7:
        return ()
    parts: list[str] = []
    for value in (captured[:4], captured[:7], captured[:10]):
        if value and value not in parts:
            parts.append(value)
    return tuple(parts)


@dataclass(frozen=True, slots=True)
class AnchorTerm:
    """一条 asset 级锚点：观察文本里不存在、但必须能被词法命中的那几列。"""

    field_type: str
    normalized_value: str
    source_type: str
    confidence: float


def anchor_terms(*, file_name: str, captured_at: str, place_text: str) -> tuple[AnchorTerm, ...]:
    """1.0 ``retrieval_indexes.py:225-245`` 的 ``_asset_anchor_rows``。

    这三类锚点是**同一件事的三面**：``caption`` 是场景描述，里面既没有地名、也没有日期、
    也没有文件名，于是「按地名找照片」「按日期找照片」在词法通道上无从命中。1.0 为此
    在写投影时额外插了 ``geo`` / ``time`` / ``file`` 三列（``:198-222`` 那条调用链）。

    三处的取值与 1.0 一致：

    - ``geo`` 取 ``Place.searchable_text()`` 的每一段（省 / 市 / 区县 / 乡镇 / 村庄），
      **不含 GPS 坐标**——1.0 的 ``label`` 在 PyGeoCN 那条路上可能退化成坐标串，
      2.0 不给坐标进入这个词的机会；
    - ``time`` 取 ``time_anchor_tokens``（整串短于 7 个字符就不给）；
    - ``file`` 取文件名去扩展名，``event_event_*`` / ``kf_*`` 前缀不建（那不是用户内容）。
    """
    terms: list[AnchorTerm] = []
    for value in dict.fromkeys(part for part in str(place_text or "").split() if part):
        terms.append(AnchorTerm("geo", normalize_text(value), ANCHOR_SOURCE_TYPES["geo"], ANCHOR_CONFIDENCE))
    for value in time_anchor_tokens(captured_at):
        terms.append(
            AnchorTerm("time", normalize_text(value), ANCHOR_SOURCE_TYPES["time"], ANCHOR_CONFIDENCE)
        )
    name = str(file_name or "")
    if file_stem_is_indexable(name):
        stem = name.rsplit(".", 1)[0] if "." in name else name
        terms.append(AnchorTerm("file", normalize_text(stem), ANCHOR_SOURCE_TYPES["file"], ANCHOR_CONFIDENCE))
    return tuple(terms)
