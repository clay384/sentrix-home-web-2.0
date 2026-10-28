"""回合与回合进度事件的 SQLite 仓储。

``agent_turn_events`` 是 2.0 对 1.0 最直接的一处修正。1.0 的进度只存在 ``app.py`` 的
进程内 dict ``_TURN_JOBS`` 里：永不清理（内存泄漏）、重启即丢（历史 turn 全部 404）、
多 worker 不可用（轮询/SSE 随机 404）、页面刷新就再也拿不到那次回合。前端不得不用 700ms
轮询来兜住这些，668,490 次请求（占全部流量 73%）是这么来的。把事件落库之后，「实时」
由 broker 负责、「迟到加入」由 ``events_after`` 负责，两条路都不需要前端反复问。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sentrix.domain.assistant.models import (
    Attachment,
    ProgressEvent,
    ProgressStage,
    ProgressStatus,
    TurnRecord,
    TurnStatus,
)
from sentrix.infrastructure.persistence.database import Database

_TURN_COLUMNS = (
    "id, conversation_id, scope_id, question, status, termination_reason, answer,"
    " attachments_json, requirements_json, candidates_json, trace_json, created_at, updated_at"
)


def _row_to_turn(row: sqlite3.Row) -> TurnRecord:
    attachments = json.loads(row["attachments_json"] or "[]")
    return TurnRecord(
        id=row["id"],
        conversation_id=row["conversation_id"],
        scope_id=row["scope_id"],
        question=row["question"],
        status=TurnStatus(row["status"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        answer=row["answer"],
        termination_reason=row["termination_reason"],
        attachments=tuple(
            Attachment(handle=item["handle"], asset_id=item["asset_id"], file_name=item["file_name"])
            for item in attachments
        ),
        requirements=tuple(json.loads(row["requirements_json"] or "[]")),
        retrieved_candidates=tuple(json.loads(row["candidates_json"] or "[]")),
        trace=dict(json.loads(row["trace_json"] or "{}")),
    )


def _event_to_json(event: ProgressEvent) -> str:
    return json.dumps(
        {
            "step_index": event.step_index,
            "stage": event.stage.value,
            "status": event.status.value,
            "text": event.text,
            "kind": event.kind,
            "timestamp": event.timestamp,
            "payload": dict(event.payload),
        },
        ensure_ascii=False,
    )


def _json_to_event(payload: str) -> ProgressEvent:
    data: Mapping[str, Any] = json.loads(payload)
    return ProgressEvent(
        step_index=int(data["step_index"]),
        stage=ProgressStage(data["stage"]),
        status=ProgressStatus(data["status"]),
        text=str(data["text"]),
        kind=str(data.get("kind") or "status"),
        timestamp=str(data.get("timestamp") or ""),
        payload=data.get("payload") or {},
    )


class SqliteTurnRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def create(self, record: TurnRecord) -> None:
        with self._database.connect() as connection, connection:
            connection.execute(
                "INSERT INTO agent_turns"
                " (id, conversation_id, scope_id, question, status, termination_reason, answer,"
                " attachments_json, requirements_json, trace_json, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.id,
                    record.conversation_id,
                    record.scope_id,
                    record.question,
                    record.status.value,
                    record.termination_reason,
                    record.answer,
                    "[]",
                    "[]",
                    # ``include_debug`` 要在 ``start`` 与 ``run`` 之间活下来；两者的
                    # 唯一交接物就是这条记录，所以建回合时就把它写进去。
                    json.dumps(dict(record.trace), ensure_ascii=False),
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                ),
            )

    def get(self, turn_id: str) -> TurnRecord | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {_TURN_COLUMNS} FROM agent_turns WHERE id = ?", (turn_id,)
            ).fetchone()
        return _row_to_turn(row) if row else None

    def claim(self, turn_id: str, updated_at: datetime) -> bool:
        """``pending`` → ``running``，返回是否抢到。

        用一条带条件的 UPDATE 做 CAS：SQLite 的单条语句是原子的，两个并发请求里只有一个
        的 ``rowcount`` 会是 1。1.0 靠 ``_TURN_JOBS`` 里的内存字段，多进程下直接失效。
        """
        with self._database.connect() as connection, connection:
            cursor = connection.execute(
                "UPDATE agent_turns SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                (
                    TurnStatus.RUNNING.value,
                    updated_at.isoformat(),
                    turn_id,
                    TurnStatus.PENDING.value,
                ),
            )
        return cursor.rowcount > 0

    def append_event(self, turn_id: str, event: ProgressEvent) -> None:
        with self._database.connect() as connection, connection:
            connection.execute(
                "INSERT OR REPLACE INTO agent_turn_events (turn_id, step_index, payload_json) VALUES (?,?,?)",
                (turn_id, event.step_index, _event_to_json(event)),
            )

    def events_after(self, turn_id: str, step_index: int) -> Sequence[ProgressEvent]:
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM agent_turn_events WHERE turn_id = ? AND step_index > ?"
                " ORDER BY step_index ASC",
                (turn_id, step_index),
            ).fetchall()
        return [_json_to_event(row["payload_json"]) for row in rows]

    def finish(
        self,
        turn_id: str,
        *,
        status: str,
        termination_reason: str,
        answer: str,
        attachments: Sequence[Attachment],
        requirements: Sequence[Mapping[str, Any]],
        retrieved_candidates: Sequence[str],
        trace: Mapping[str, Any],
        updated_at: datetime,
    ) -> None:
        payload = json.dumps(
            [
                {"handle": item.handle, "asset_id": item.asset_id, "file_name": item.file_name}
                for item in attachments
            ],
            ensure_ascii=False,
        )
        ledger_payload = json.dumps([dict(item) for item in requirements], ensure_ascii=False)
        candidates_payload = json.dumps(list(retrieved_candidates), ensure_ascii=False)
        trace_payload = json.dumps(dict(trace), ensure_ascii=False)
        with self._database.connect() as connection, connection:
            connection.execute(
                "UPDATE agent_turns SET status = ?, termination_reason = ?, answer = ?,"
                " attachments_json = ?, requirements_json = ?, candidates_json = ?,"
                " trace_json = ?, updated_at = ? WHERE id = ?",
                (
                    status,
                    termination_reason,
                    answer,
                    payload,
                    ledger_payload,
                    candidates_payload,
                    trace_payload,
                    updated_at.isoformat(),
                    turn_id,
                ),
            )

    def list_for_conversation(self, conversation_id: str, limit: int) -> Sequence[TurnRecord]:
        with self._database.connect() as connection:
            rows = connection.execute(
                f"SELECT {_TURN_COLUMNS} FROM agent_turns WHERE conversation_id = ?"
                " ORDER BY created_at ASC, rowid ASC LIMIT ?",
                (conversation_id, max(1, limit)),
            ).fetchall()
        return [_row_to_turn(row) for row in rows]
