"""助手端点。只做 HTTP 映射，回合的一切规则在 domain / application 层。

SSE 的形状与设计 §4.3 一致，沿用 1.0 已验证的两类事件：``progress`` 与 ``complete``。
与 1.0 的三处刻意差异：

1. **事件带 ``id:``，且断线可从 ``Last-Event-ID`` 续。** 1.0 的 ``sent`` 是闭包局部变量，
   每次重连都归零并把全部历史重发一遍（``app.py:2982``）。2.0 的 ``step_index`` 在回合内
   单调递增，正好当事件 id 用。
2. **``complete`` 帧不再重发整条进度数组。** 1.0 把它又塞了一遍（``app.py:2989``）。
3. **回合终态是真实的。** 1.0 把模型失败、超时、guard 拦截全部压成 ``status="complete"``
   （``app.py:3433``），只有未捕获的 Python 异常才是 ``error``，前端拿到的每一个失败都是
   一个「成功」的帧。2.0 的 ``status`` 直接来自 ``TurnStatus``。

**轮询端点是 1.0 的公开契约，不是它前端走歪了。** S3 曾经把 ``GET /api/assistant/turn/{id}``
删掉，理由写的是「1.0 的前端一 onerror 就永久降级成 700ms 轮询，于是轮询成了主路径」。
审计推翻了这条理由：SSE 端点在 1.0 是**能用的**（同一份 SSE 契约 2.0 原样沿用），而 66 万次
轮询的调用方不是浏览器——``services/photobench`` 的 ``wait_for_assistant_turn``
（``benchmark_orchestrator.py:881``）是个 ``time.sleep(0.5)`` 的轮询循环，``EventSource``
对它不可用。8091 访问日志里 ``GET /api/assistant/turn/{id}`` 668,552 次、
``.../events`` 只有 62 次，而且轮询请求是十几个 ``turn_id`` 交替紧密打出来的，中间没有任何
浏览器 UI 流量——那是评测机的形状，不是人的形状。于是这里把端点搬回来。
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import AsyncIterator
from dataclasses import replace

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

from sentrix.api.deps import get_container
from sentrix.api.routes.media import deliver_asset
from sentrix.api.routes.search import build_retrieve_evidence
from sentrix.api.schemas.assistant import (
    AttachmentSummary,
    CompleteEventPayload,
    ConversationDetailResponse,
    ConversationListResponse,
    ConversationSummary,
    CreateConversationRequest,
    MessageSummary,
    ProgressEventPayload,
    RenameConversationRequest,
    StartTurnRequest,
    StartTurnResponse,
    TurnPollResponse,
    TurnSummary,
)
from sentrix.application.assistant.manage_conversations import ManageConversations
from sentrix.application.assistant.query_memory_facts import QueryMemoryFacts
from sentrix.application.assistant.run_turn import (
    RunTurn,
    TurnRequest,
)
from sentrix.application.assistant.tools import AssistantToolbox
from sentrix.bootstrap.container import Container
from sentrix.domain.assistant.models import Conversation, ProgressEvent, TurnRecord
from sentrix.domain.assistant.profile import DEFAULT_PROFILE

log = logging.getLogger("sentrix.api.assistant")

router = APIRouter(tags=["assistant"])

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def _conversation_summary(conversation: Conversation) -> ConversationSummary:
    return ConversationSummary(
        id=conversation.id,
        scope_id=conversation.scope_id,
        title=conversation.title,
        created_at=conversation.created_at.isoformat(),
        updated_at=conversation.updated_at.isoformat(),
        last_message_at=conversation.last_message_at.isoformat(),
    )


def _attachments(record: TurnRecord) -> list[AttachmentSummary]:
    return [
        AttachmentSummary(handle=item.handle, asset_id=item.asset_id, file_name=item.file_name)
        for item in record.attachments
    ]


def _turn_summary(record: TurnRecord) -> TurnSummary:
    return TurnSummary(
        id=record.id,
        question=record.question,
        status=record.status.value,
        termination_reason=record.termination_reason,
        answer=record.answer,
        attachments=_attachments(record),
        created_at=record.created_at.isoformat(),
    )


def _run_turn(container: Container) -> RunTurn:
    return RunTurn(
        model=container.chat_model,
        conversations=container.conversation_repository,
        turns=container.turn_repository,
        # ``search_memories`` 走的是与 ``GET /api/v1/search`` 同一套多路召回：
        # 1.0 的两个入口（HTTP 侧与 ``agent_runtime/tools.py``）都是同一个
        # ``EvidenceRetrievalKernel``，只有一份融合排名。
        toolbox=AssistantToolbox(
            retrieval=build_retrieve_evidence(container),
            assets=container.asset_repository,
            result_sets=container.result_set_repository,
            people=container.person_directory,
            memory_facts=QueryMemoryFacts(
                structured=container.structured_query_repository,
                entities=container.entity_repository,
            ),
            # `query_photo_people` 的两条读路径（T122）：按 asset 的已绑定人脸，
            # 与 people_json 里带 entity_id 的那一份快照。
            faces=container.face_repository,
            observations=container.observation_repository,
            # `inspect_photo` 的两件（T123）：把问题问给图的那一层，与目标人物的裁剪件。
            photo_inspector=container.photo_inspector,
            person_crops=container.person_crop_renderer,
            # preview 的 ``place`` 取自这里（导入期 EXIF GPS 反解出来的行政地名），
            # 不是 ``observations.place``（VLM 的场景描述）——理由见 ``_place_label``。
            asset_places=container.asset_place_repository,
        ),
        # 墙钟是部署事实：Mac/CPU 上 4 并发时一次 inspect 就可能 30-60s，
        # 180s 的默认值会把正常回合截成 timeout。由 Settings 注入，153 仍用 180。
        profile=replace(
            DEFAULT_PROFILE,
            wall_time_s=container.settings.agent_wall_time_s,
            max_steps=container.settings.agent_max_steps,
            strict_model_step_budget=container.settings.vlm_native_output_budget,
            max_tool_calls=container.settings.agent_max_tool_calls,
            max_search_calls=container.settings.agent_max_search_calls,
            max_page_calls=container.settings.agent_max_page_calls,
            max_inspections=container.settings.agent_max_inspections,
        ),
    )


@router.post("/assistant/conversations", status_code=201)
def create_conversation(
    payload: CreateConversationRequest, container: Container = Depends(get_container)
) -> ConversationSummary:
    use_case = ManageConversations(
        conversations=container.conversation_repository, turns=container.turn_repository
    )
    return _conversation_summary(use_case.create(scope_id=payload.scope_id, title=payload.title))


@router.get("/assistant/conversations")
def list_conversations(
    scope_id: str = "home-default", container: Container = Depends(get_container)
) -> ConversationListResponse:
    use_case = ManageConversations(
        conversations=container.conversation_repository, turns=container.turn_repository
    )
    return ConversationListResponse(
        items=[_conversation_summary(item) for item in use_case.list_for_scope(scope_id)]
    )


@router.get("/assistant/conversations/{conversation_id}")
def get_conversation(
    conversation_id: str, container: Container = Depends(get_container)
) -> ConversationDetailResponse:
    use_case = ManageConversations(
        conversations=container.conversation_repository, turns=container.turn_repository
    )
    detail = use_case.detail(conversation_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return ConversationDetailResponse(
        conversation=_conversation_summary(detail.conversation),
        messages=[
            MessageSummary(
                id=message.id,
                role=message.role.value,
                content=message.content,
                turn_id=message.turn_id,
                created_at=message.created_at.isoformat(),
            )
            for message in detail.messages
        ],
        turns=[_turn_summary(turn) for turn in detail.turns],
    )


@router.patch("/assistant/conversations/{conversation_id}")
def rename_conversation(
    conversation_id: str,
    payload: RenameConversationRequest,
    container: Container = Depends(get_container),
) -> ConversationSummary:
    use_case = ManageConversations(
        conversations=container.conversation_repository, turns=container.turn_repository
    )
    if not use_case.rename(conversation_id, payload.title):
        raise HTTPException(status_code=404, detail="conversation not found")
    detail = use_case.detail(conversation_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return _conversation_summary(detail.conversation)


@router.delete("/assistant/conversations/{conversation_id}", status_code=204)
def delete_conversation(conversation_id: str, container: Container = Depends(get_container)) -> Response:
    use_case = ManageConversations(
        conversations=container.conversation_repository, turns=container.turn_repository
    )
    if not use_case.delete(conversation_id):
        raise HTTPException(status_code=404, detail="conversation not found")
    return Response(status_code=204)


@router.post("/assistant/conversations/{conversation_id}/turns", status_code=202)
def start_turn(
    conversation_id: str,
    payload: StartTurnRequest,
    container: Container = Depends(get_container),
) -> StartTurnResponse:
    """建一个回合并立刻返回 ``turn_id``，执行放在工作线程里。

    回合先落库（``pending``）、再由 SSE 订阅，是「前端不轮询」能成立的前提：SSE 端点
    永远有东西可订阅，哪怕连接晚到几百毫秒。

    scope 不在请求里：回合继承会话的 scope。调用方多传的 ``scope_id`` 从 S3 交付起
    就被忽略，现在连字段都不存在了。
    """
    manage = ManageConversations(
        conversations=container.conversation_repository, turns=container.turn_repository
    )
    run_turn = _run_turn(container)
    # 回合的 scope 由会话决定，请求里没有这个字段可传。会话不存在时 ``start`` 抛
    # ``TurnNotFoundError``（带 404 的码），路由**只放行**——映射在 ``api/errors.py``。
    record = run_turn.start(TurnRequest(conversation_id=conversation_id, question=payload.message))
    manage.ensure_title(conversation_id, payload.message)

    broker = container.turn_event_broker
    # 先登记再起飞：SSE 可能比工作线程的第一条事件更早连上来。
    broker.open(record.id)

    def worker() -> None:
        try:
            run_turn.run(record.id, emit=lambda event: broker.publish(record.id, event))
        except Exception:
            log.exception("回合执行失败: %s", record.id)
        finally:
            broker.finish(record.id)

    threading.Thread(target=worker, name=f"turn-{record.id}", daemon=True).start()
    return StartTurnResponse(turn_id=record.id, conversation_id=conversation_id, status=record.status.value)


def _progress_payload(event: ProgressEvent) -> ProgressEventPayload:
    """进度事件的载荷。SSE 的 ``progress`` 帧与轮询端的 ``public_progress`` 共用一份——
    两条传输路径给出的事件形状必须逐字相同，否则「回退到轮询」就不再是等价降级。"""
    return ProgressEventPayload(
        text=event.text,
        status=event.status.value,
        stage=event.stage.value,
        step_index=event.step_index,
        timestamp=event.timestamp,
        kind=event.kind,
        payload=dict(event.payload),
    )


def _complete_payload(record: TurnRecord) -> CompleteEventPayload:
    """终态载荷。SSE 的 ``complete`` 帧与轮询端的 ``result`` 共用。"""
    return CompleteEventPayload(
        turn_id=record.id,
        status=record.status.value,
        termination_reason=record.termination_reason,
        answer=record.answer,
        attachments=[
            AttachmentSummary(handle=item.handle, asset_id=item.asset_id, file_name=item.file_name)
            for item in record.attachments
        ],
        requirements=[dict(item) for item in record.requirements],
        retrieved_candidates=list(record.retrieved_candidates),
        trace=dict(record.trace),
    )


def _format_progress(event: ProgressEvent) -> str:
    data = _progress_payload(event).model_dump()
    return f"id: {event.step_index}\nevent: progress\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _format_complete(record: TurnRecord) -> str:
    data = _complete_payload(record).model_dump()
    return f"event: complete\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _resume_index(last_event_id: str) -> int:
    try:
        return max(0, int(last_event_id))
    except (TypeError, ValueError):
        return 0


@router.get("/assistant/turns/{turn_id}/events")
async def turn_events(
    turn_id: str,
    request: Request,
    last_event_id: str = Header(default="", alias="Last-Event-ID"),
    container: Container = Depends(get_container),
) -> StreamingResponse:
    """回合事件的 SSE 流。跑完即关，不需要前端再问一次状态。

    订阅顺序是**先读库、再订阅内存流**：库里是所有已经落盘的事件（断线重连与迟到加入
    都能补），内存流是此刻还没落盘的增量。两边按 ``step_index`` 去重。
    """
    if container.turn_repository.get(turn_id) is None:
        raise HTTPException(status_code=404, detail="turn not found")

    turns = container.turn_repository
    broker = container.turn_event_broker
    cursor = _resume_index(last_event_id)

    async def body() -> AsyncIterator[str]:
        nonlocal cursor
        for stored in turns.events_after(turn_id, cursor):
            cursor = stored.step_index
            yield _format_progress(stored)

        record = turns.get(turn_id)
        if record is not None and record.status.is_terminal:
            # The worker may have persisted every event between the first read
            # and this terminal check. Catch up before announcing completion.
            for stored in turns.events_after(turn_id, cursor):
                cursor = stored.step_index
                yield _format_progress(stored)
            yield _format_complete(record)
            return

        async for event in broker.subscribe(turn_id):
            if await request.is_disconnected():
                return
            if event is None:
                break
            if event.step_index <= cursor:
                # 数据库那一轮已经发过了。broker 的 backlog 从回合一开始就记，
                # 重叠是正常的，靠 step_index 去重。
                continue
            cursor = event.step_index
            yield _format_progress(event)

        # A closed channel can be retired before subscription begins. Durable
        # events bridge that handover; cursor still prevents duplicate delivery.
        for stored in turns.events_after(turn_id, cursor):
            cursor = stored.step_index
            yield _format_progress(stored)
        record = turns.get(turn_id)
        if record is not None:
            yield _format_complete(record)

    return StreamingResponse(body(), media_type="text/event-stream", headers=SSE_HEADERS)


# 失败态在轮询响应里也要给出一句原因——1.0 的调用方读的就是 ``error`` 或 ``reason``
# （``benchmark_orchestrator.py:900``）。2.0 没有单独的 error 字段可读，用终止原因代替。
_FAILED_STATUSES = frozenset({"failed", "blocked"})


@router.get("/assistant/turns/{turn_id}")
def get_turn(turn_id: str, container: Container = Depends(get_container)) -> TurnPollResponse:
    """B3.4：轮询异步回合的实时进度与最终结果（1.0 ``app.py:2938`` 的契约）。

    这条端点给的是**与 SSE 完全等价**的信息：同样的进度事件、同样的终态载荷。浏览器的
    主路径仍然是那条 SSE（``useTurnStream`` 先开 ``EventSource``，断了才回退到这里，
    与 1.0 ``src/app.js:1482`` 的顺序一致）；``EventSource`` 用不了的调用方——评测机、
    脚本、CI——只有这一条路。
    """
    record = container.turn_repository.get(turn_id)
    if record is None:
        raise HTTPException(status_code=404, detail="turn not found")

    terminal = record.status.is_terminal
    return TurnPollResponse(
        turn_id=turn_id,
        status=record.status.value,
        public_progress=[
            _progress_payload(event) for event in container.turn_repository.events_after(turn_id, 0)
        ],
        result=_complete_payload(record) if terminal else None,
        error=record.termination_reason if record.status.value in _FAILED_STATUSES else "",
    )


@router.get("/assistant/result-sets/{result_set_id}/photo")
def result_set_photo(
    result_set_id: str,
    handle: str = Query(default=""),
    scope_id: str = Query(default="home-default"),
    original: bool = Query(default=False),
    container: Container = Depends(get_container),
) -> Response:
    """B3.2：结果集授权原图交付。``handle`` 必须属于该结果集，且 scope 必须匹配。

    1.0 ``app.py:2484`` 的忠实移植，四条判断逐条对齐（顺序也一样）：

    - 结果集不存在 → 404 ``result set not found``（1.0 还带「或已过期」；2.0 的
      ``ResultSet`` 没有 expires_at 这一格，见下述）；
    - ``rs.scope_id != scope_id`` → 403 ``scope mismatch``；
    - 给了 ``handle`` 但它不在该结果集里 → 404 ``handle not in result set``；
    - 没给 ``handle`` → 取结果集第一张；结果集为空 → 404 ``empty result set``。

    为什么值得搬：它是**唯一**一条把「模型给的 ``photo_N``」和「真实 ``asset_id``」隔开的
    交付路径——``resolve`` 只在结果集自己那一串 asset_id 上做下标映射，越界返回 ``None``。
    前端拿到的 URL 里从头到尾没有 asset_id（1.0 ``src/api.js:67``），于是「只交付它真的
    看过的图」是个结构性事实，而不是提示词里的一句请求。

    与 1.0 的两处差异，都是 2.0 这边缺的那一格数据：

    1. **没有 ``expires_at``。** 1.0 的 ``ResultSet`` 有这一列，2.0 的没有，于是「过期」
       在 2.0 表现为「查不到」，同样是 404。过期本身属于 S4 的维护面。
    2. **``original`` 的语义沿用 2.0 媒体端点的既有映射**：``original=1`` 给原文件，
       默认给服务端转码的 JPEG 预览（1.0 默认也走归一化转码，只有 ``original=1``
       才给原图）。
    """
    result_set = container.result_set_repository.get(result_set_id)
    if result_set is None:
        raise HTTPException(status_code=404, detail="result set not found")
    if result_set.scope_id != scope_id:
        raise HTTPException(status_code=403, detail="scope mismatch")

    asset_id = result_set.resolve(handle) if handle else None
    if handle and asset_id is None:
        raise HTTPException(status_code=404, detail="handle not in result set")
    if asset_id is None:
        asset_id = result_set.asset_ids[0] if result_set.asset_ids else None
        if asset_id is None:
            raise HTTPException(status_code=404, detail="empty result set")

    return deliver_asset(asset_id, original=original, container=container)
