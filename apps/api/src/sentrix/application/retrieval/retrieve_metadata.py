"""元数据召回用例：结构化条件（时间窗 / 媒体类型 / 地点）的确定性候选。

对应 1.0 ``backend/retrieval/metadata.py`` 的 ``MetadataRetriever.retrieve``。

它是**唯一一个在没有正向结构化条件时返回空的通道**（1.0 ``metadata.py:28-31``，
原文注释保留在下面）。理由是它同时充当「硬预筛的候选全集」：把 scope 内每一个 asset
都当 anchor 回传，会让一个纯语义查询的融合结果被插入顺序污染——1.0 在 R2 之前就是
让 Kernel 逐个走完全部 asset，这条通道只是把那趟遍历写出来。

三处与 1.0 的差异：

1. **地点预筛用 ``Place`` 而不是解析 JSON**。1.0 ``metadata.py:59-67`` 拿到
   ``metadata_json`` 后只处理 dict，而 ``MemoryStore`` 返回的是 **JSON 字符串**，
   于是 ``geo`` 恒为 falsy → 「没有 geocode」→ 放行，纯地点查询把整个 scope 灌进来。
   见 ``s4-deviations.md`` §1.3 第 16 项。
2. **「没解析出地点」不等于「地点不匹配」**。前者由调用方显式选择放行或剔除，
   不再藏在一个布尔值里。
3. **召回上限是常量**，不是 ``SENTRIX_METADATA_RECALL_LIMIT`` 环境变量
   （1.0 ``metadata.py:36-37`` 的默认值就是 200）。见 ``s4-deviations.md`` §2.5。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime

from sentrix.domain.media.models import Asset, MediaType
from sentrix.domain.media.ports import AssetRepository
from sentrix.domain.places.aliases import PlaceAliases
from sentrix.domain.places.matching import place_text_matches
from sentrix.domain.places.models import Place
from sentrix.domain.places.ports import AssetPlaceRepository
from sentrix.domain.retrieval.lexical import normalize_text
from sentrix.domain.retrieval.models import CandidateHit

CHANNEL = "metadata"

# 1.0 把召回上限做成了 SENTRIX_METADATA_RECALL_LIMIT 环境变量（默认 "200"）。
# 2.0 落成常量：这个值没有任何生产改动记录，而 1.0 的配置纪律是「~126 个 env flag
# 的值互相矛盾」（见 2.0 config.py 的开头注释）。做成 env 只会多一个没人动过的开关。
# 它同时是「纯结构化查询最多返回多少张」的唯一上限，改它等于改 metadata 通道的强度，
# 应当连同证据一起改，而不是在部署时改。
METADATA_RECALL_LIMIT = 200

# 1.0 ``metadata.py:38`` 的 ``list_assets(limit=100_000)``。它是个「一次扫完」的
# 上界，不是一个有语义的配额；比召回上限大两个数量级，写出来是为了说明这一点。
ASSET_SCAN_LIMIT = 100_000

DEFAULT_SCOPE_ID = "home-default"


class RetrieveMetadata:
    def __init__(
        self,
        *,
        assets: AssetRepository,
        places: AssetPlaceRepository,
        aliases: PlaceAliases,
        recognize_place: Callable[[str], bool] = lambda _asset_id: False,
    ) -> None:
        self._assets = assets
        self._places = places
        self._aliases = aliases
        # 「这个 asset 本该有地点但解析不出来」的判据由调用方给：S4 里它就是
        # 「GPS 存在」；写成回调是为了不必让应用层去读 asset 的 GPS 字段。
        self._recognize_place = recognize_place

    def execute(
        self,
        *,
        scope_id: str | None,
        time_bounds: tuple[datetime, datetime] | None = None,
        media_types: Sequence[MediaType] = (),
        negated_media: Sequence[MediaType] = (),
        place: str = "",
        limit: int = METADATA_RECALL_LIMIT,
    ) -> list[CandidateHit]:
        # Metadata 只在有正向结构化条件时才给召回信号。没有条件就返回空，
        # 否则一个纯语义查询会被「scope 内的每张图」当 anchor 污染。
        if not time_bounds and not media_types and not place:
            return []
        internal_limit = max(int(limit or 20), METADATA_RECALL_LIMIT)

        # ``scope_id=None`` = 跨全部相册（1.0 的 ``if scope_id:``）。**不兜底成某个空间**：
        # 那会把「用户在界面上选了全部相册」与「查一个叫 home-default 的空间」当成同一件事，
        # 而后者在 2.0 的空库里根本不存在——表现是检索一个东西都搜不到。
        candidates: list[Asset] = list(self._assets.list_by_scope(scope_id, limit=ASSET_SCAN_LIMIT))
        places = self._places.get_many([asset.id for asset in candidates])
        allowed_media = set(media_types)
        denied_media = set(negated_media)

        hits: list[CandidateHit] = []
        for asset in candidates:
            # 二次过滤：``list_by_scope`` 已经按 scope 筛过了，这里只是不让上游改动
            # 悄悄放过跨空间的候选。**但 None 时不能过滤**——那正是「全部相册」。
            if scope_id is not None and asset.scope_id != scope_id:
                continue
            if allowed_media and asset.media_type not in allowed_media:
                continue
            if asset.media_type in denied_media:
                continue
            if time_bounds and not _within(time_bounds, asset.captured_at):
                continue
            if place and not self._place_ok(place, asset, places.get(asset.id)):
                continue
            hits.append(
                CandidateHit(
                    asset_id=asset.id,
                    channel=CHANNEL,
                    rank=len(hits) + 1,
                    # metadata 是**锚点**通道：命中即确定性成立，没有「有多像」。
                    # 融合按 domain/retrieval/fusion.py 的 ANCHOR_CHANNELS 给它加固定加成。
                    raw_score=0.0,
                    score_kind="discrete",
                    higher_is_better=True,
                )
            )
            if len(hits) >= internal_limit:
                break
        return hits

    def _place_ok(self, place: str, asset: Asset, resolved: Place | None) -> bool:
        """地点约束的放行规则。

        「解析出了地点」→ 按三条确定性规则匹配；
        「本该有地点但没解析出来」→ 交给 ``recognize_place`` 决定（S4 里为真时**剔除**，
        因为「有 GPS 却解析不出」意味着坐标在数据覆盖之外，拿它去支持一个地点查询是猜）；
        「本来就没有 GPS」→ 放行。1.0 对这三种情况一视同仁地放行，这才是它把整个
        scope 灌进 metadata 通道的根因。
        """
        if asset.source_location:
            return normalize_text(place) in normalize_text(asset.source_location)
        if resolved is not None:
            return place_text_matches(place, resolved, self._aliases)
        return not self._recognize_place(asset.id)


def _within(bounds: tuple[datetime, datetime], captured_at: datetime | None) -> bool:
    """1.0 ``metadata.py:50-53``：**拍摄时间未知的行放行**。

    这条很反直觉但必须保留：`captured_at` 为空是导入时的常见状态，把它当「不匹配」
    会让时间窗查询把一大批本该出现的照片排除掉。1.0 的写法就是
    ``if captured is not None and not (start <= captured < end): continue``。
    """
    if captured_at is None:
        return True
    # A source wall clock and a known instant have no safe ordering until the
    # source timezone is known. Do not invent one or discard either offset.
    if len({value.utcoffset() is not None for value in (*bounds, captured_at)}) > 1:
        return False
    return bounds[0] <= captured_at < bounds[1]


__all__ = ["ASSET_SCAN_LIMIT", "CHANNEL", "METADATA_RECALL_LIMIT", "RetrieveMetadata"]
