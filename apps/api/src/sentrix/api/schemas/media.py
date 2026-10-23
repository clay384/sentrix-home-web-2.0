from __future__ import annotations

from pydantic import BaseModel, Field


class ImportResponse(BaseModel):
    asset_id: str
    observation_id: str | None
    media_type: str
    status: str
    deduplicated: bool
    # 有没有真的写成视觉向量。1.0 的「导入成功但没有向量」在接口上完全不可见，
    # 只能在事后手工清点库里少了多少条才发现。
    indexed: bool
    # 有没有真的解析出地点（落 asset_places 一行）。与 indexed 同一条理由：
    # 「导入成功但地点没落」在接口上必须是可见的。
    placed: bool = False
    # 折进的 / 新建的事件 id。事件归属没接上时为 None。
    event_id: str | None = None
    # 视频有没有真的走到 ffprobe（S8a T505）。与 indexed / placed 同一个可观测面：
    # 原先「视频收下了但什么都没发生」在响应里完全看不出来。图片恒为 False。
    video_probed: bool = False
    # 这次导入之后建起来的视频场景数（S8b T512）。与 ``video_probed`` 同一条理由：
    # 「视频导入成功但一个场景都没有」也要看得见。图片恒为 0。
    video_scenes: int = 0
    # 音频有没有真的走到 ffprobe（S8c T521）。与 ``video_probed`` 同一个可观测面：
    # 「音频收下了但什么都没发生」原先在响应里完全看不出来。音频以外恒为 False。
    audio_probed: bool = False


class AssetSummary(BaseModel):
    id: str
    file_name: str
    media_type: str
    mime_type: str | None
    size_bytes: int
    status: str
    captured_at: str | None
    gps: dict[str, float] | None
    # 关键帧血缘（迁移 0010），普通资产为 None。1.0 的 ``/api/assets`` 是
    # ``SELECT * FROM assets``（``db.py:1505``），这几列顺带就回了；2.0 的窄投影漏了
    # 它们，于是 PhotoBench 的关键帧分析（``benchmark_orchestrator.py:5712/5724/5798``）
    # 在 2.0 的数据上认不出派生帧、也算不出采样间隔。**列表与单条都带**（同一个 schema）。
    derived_kind: str | None = None
    parent_asset_id: str | None = None
    source_timestamp_sec: float | None = None
    external_source_id: str | None = None
    source_timestamp: str | None = None
    source_location: str = ""
    source_metadata: dict[str, object] = Field(default_factory=dict)


class AssetListResponse(BaseModel):
    items: list[AssetSummary]
