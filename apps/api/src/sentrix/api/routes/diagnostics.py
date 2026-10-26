"""只读的评测诊断投影。

PhotoBench 通过 HTTP 取这里的图结构质量；benchmark GT 仍只留在 PhotoBench，主产品
不会因为打开测评页面而读取数据集标注。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from sentrix.api.deps import get_container
from sentrix.bootstrap.container import Container

router = APIRouter(tags=["diagnostics"])


@router.get("/memory-graph/quality")
def memory_graph_quality(
    scope_id: str = Query(..., min_length=1),
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """返回当前 scope 图的结构契约审计，不读取任何 PhotoBench GT。"""

    return dict(container.memory_graph.quality(scope_id.strip()))
