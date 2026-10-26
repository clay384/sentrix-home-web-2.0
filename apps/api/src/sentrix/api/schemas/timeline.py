"""时间线端点的传输模型。字段与 ``application/memories/read_timeline.py`` 一一对应。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from sentrix.api.schemas.family import FamilyRunResponse


class TimelinePhoto(BaseModel):
    asset_id: str
    file_name: str
    captured_at: str | None
    # ``Place.display()`` 的原样输出。**后端算好的最终字符串**——前端不做任何拼接，
    # 否则「附近」加给谁、什么时候加就成了两套规则。
    place: str


class TimelineEvent(BaseModel):
    id: str
    title: str
    event_type: str
    time_start: str | None
    time_end: str | None
    # 事件的行政区归属（可做硬条件过滤的那一级，可能为空）。``place`` 是给展示的整串。
    admin_district: str
    place: str
    activity: str
    summary: str
    confidence: float
    photos: list[TimelinePhoto]


class TimelineDay(BaseModel):
    date: str
    events: list[TimelineEvent]


class TimelineResponse(BaseModel):
    scope_id: str
    # scope 内的事件总数（不是本页条数），分页据此判断还有没有下一段。
    total: int
    offset: int
    limit: int
    days: list[TimelineDay]


class ImportBatchRequest(BaseModel):
    """整册导入。``directory`` 是**服务端**路径——相册在服务器上，不在浏览器里。"""

    directory: str = Field(..., min_length=1, max_length=4096)
    scope_id: str = "home-default"
    limit: int = Field(200, ge=1, le=1000)
    operation_id: str | None = Field(None, pattern=r"^[A-Za-z0-9_-]{1,96}$")


class ImportProgressResponse(BaseModel):
    operation_id: str
    scope_id: str
    status: str
    stage: str
    total: int | None
    completed: int
    imported: int
    deduplicated: int
    failed: int
    current_file: str | None
    updated_at: str
    sequence: int
    error: str | None
    observed_at: str


class ImportFailure(BaseModel):
    file_name: str
    reason: str


class InsightCounts(BaseModel):
    """批末自动跑画像那一趟的计数（Task 86i，裁决④）。

    六个字段就是 ``application/identity/run_insights.py`` 的 ``InsightRun``。直接回给
    调用方而不是只落日志：这一趟会走几十次模型调用、让本来就长的导入请求再长一截，
    **"到底跑了没有、跑了多少"必须能从响应里看见**——否则它就是一个没人能观测的副作用。
    """

    ranked: int
    core: int
    selections: int
    moments: int
    moment_failures: int
    portraits: int


class ImportBatchResponse(BaseModel):
    scope_id: str
    directory: str
    imported: int
    deduplicated: int
    events: int
    lexical_terms: int
    # 批末被总结过的事件数（去重后）。不为 0 是「事件拿到了真标题」的可观测面——
    # 这个数小于 ``events`` 说明有事件被折叠进了已总结过的那个（1.0 亦然）。
    summarized: int
    #: 分项耗时（秒）：stage 落库/读文件、prepare VLM 观察、finish 归属写入、
    #: summarize 事件总结、total 合计。2.0 的导入是同步的，不拆的话整段都记在导入上。
    timings: dict[str, float] = {}
    failures: list[ImportFailure]
    # 批末自动跑的那一趟画像。**没有新照片时是 None**（判据在路由里，理由写在那儿）。
    # 用 ``None`` 而不是全 0 的计数对象：那两件事不一样——「没跑」与「跑了但一个人都
    # 没认出来」在排查时是完全不同的结论。
    insights: InsightCounts | None = None
    # **2.0 自加的第二个键**（``insights`` 是第一个，同一条理由）：批末触发是**同步**的，
    # 装作异步只会把「跑没跑、跑成没成」藏起来。1.0 那边它是后台线程，响应里什么都没有。
    family_graph: FamilyRunResponse | None = None


class AssetImportBatchResponse(BaseModel):
    """资产导入阶段响应：只落盘/登记，不包含关键帧、记忆或图构建。"""

    scope_id: str
    directory: str
    imported: int
    deduplicated: int
    asset_ids: list[str]
    timings: dict[str, float] = {}
    failures: list[ImportFailure]


class PipelineProcessRequest(BaseModel):
    """显式流水线请求。``asset_ids`` 的顺序是语义提交顺序。"""

    scope_id: str = Field(..., min_length=1, max_length=255)
    asset_ids: list[str] = Field(default_factory=list, max_length=1000)


class MemoryGraphBuildResponse(BaseModel):
    scope_id: str
    node_count: int
    edge_count: int
    causal_candidate_pairs: int = 0
    causal_edges: int = 0
    causal_status: str = "not_configured"
    causal_seconds: float = 0.0
    keyframe_count: int = 0
    keyframe_temporal_edges: int = 0
    keyframe_object_edges: int = 0
    keyframe_relation_edges: int = 0
    keyframe_visual_edges: int = 0
    keyframe_visual_status: str = "no_frames"


class PipelineProcessResponse(BaseModel):
    scope_id: str
    processed: int
    skipped: int
    events: int
    lexical_terms: int
    video_scenes: int
    summarized: int
    timings: dict[str, float] = {}
    failures: list[ImportFailure]
    memory_graph: MemoryGraphBuildResponse | None = None
    insights: InsightCounts | None = None
    family_graph: FamilyRunResponse | None = None
