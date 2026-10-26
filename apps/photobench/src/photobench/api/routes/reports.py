"""三份报告的路由（1.0 ``do_GET`` 里 ``/api/runs/{id}/{keyframe-analysis,memory-effectiveness,
export-trace,export-sft}`` 那四条，``:7724-7819``）。

**两条计算路由与两条下载路由混在一个模块里**，因为它们读同一份 run、由同一个用例类
提供，1.0 里也是挨着的连续四个分支。分开挂只会让「这个 run 有哪些报告」散成两处。

``export-*`` 两条**返回的是下载响应而不是 JSON**（``Content-Disposition: attachment``，
前端用 ``window.open`` 触发）。形状见 ``_download`` 的 docstring。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request, Response

from photobench.api.deps import get_container
from photobench.domain.runs import exports

router = APIRouter(tags=["reports"])


@router.get("/runs/{run_id}/keyframe-analysis")
def keyframe_analysis(request: Request, run_id: str) -> dict[str, Any]:
    """1.0 ``:7724-7727``。"""
    return get_container(request).reports.keyframe_analysis(run_id)


@router.get("/runs/{run_id}/memory-effectiveness")
def memory_effectiveness(request: Request, run_id: str) -> dict[str, Any]:
    """1.0 ``:7728-7731``。"""
    return get_container(request).reports.memory_effectiveness(run_id)


@router.get("/runs/{run_id}/graph-quality")
def graph_quality(request: Request, run_id: str) -> dict[str, Any]:
    """可验边、人脸身份聚类和分题型图 QA 的只读汇总。"""

    return get_container(request).reports.graph_quality(run_id)


def _download(payload: Any, file_name: str) -> Response:
    """1.0 的下载响应：JSON 字节 + ``Content-Disposition``。

    **刻意不用 FastAPI 的 ``JSONResponse``。** 它走 ``json.dumps`` 的默认参数，
    ``ensure_ascii=True`` 会把中文转义成 ``\\uXXXX``。1.0 用的是
    ``json.dumps(payload, ensure_ascii=False)``（``:7800`` / ``:7821``），而导出的
    trace 里全是中文提示词与中文工具结果——转义之后这份文件是给机器看的，
    而它本来就是给人拿去标注的。

    ``run_id`` 能走到这里就说明盘上真有这个 run 目录（``RunStore.load_state`` 对不
    存在的 id 抛 ``KeyError``，HTTP 层映射成 404），所以文件名里不会有路径分隔符。
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return Response(
        content=body,
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
    )


@router.get("/runs/{run_id}/export-trace")
def export_trace(request: Request, run_id: str, scores: str = "", min_score: str = "") -> Response:
    """1.0 ``:7789-7803``。``scores`` 与 ``min_score`` 同时生效（1.0 是两个独立的 ``if``）。"""
    payload = get_container(request).reports.export_traces(
        run_id,
        scores=exports.parse_filter(scores) or None,
        min_score=exports.parse_min_score(min_score),
    )
    return _download(payload, f"{run_id}-trace.json")


@router.get("/runs/{run_id}/export-sft")
def export_sft(request: Request, run_id: str, scores: str = "", min_score: str = "") -> Response:
    """1.0 ``:7804-7819``。**给了 ``scores`` 就忽略 ``min_score``**（1.0 是 ``if/elif``）。"""
    payload = get_container(request).reports.export_sft(
        run_id,
        scores=exports.parse_filter(scores) or None,
        min_score=exports.parse_min_score(min_score),
    )
    return _download(payload, f"{run_id}-sft-traces.json")
