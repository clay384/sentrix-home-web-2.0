"""``event_participants`` / ``event_entities`` 两张事件投影表。SQL 只出现在这一层。

对应 1.0 ``backend/db.py`` 的 ``upsert_event_participant`` :2486、``_merge_events``
:5482 里那一段「换家」（把源事件的投影行改挂到目标事件上）、``list_event_participants``
:2528。

**1.0 的 ``upsert_event_entity``（``:3287``）没有被搬成一个公开方法。** 它的调用点是
agent runtime 与实体抽取（S6），2.0 现在没有任何一处写 ``event_entities``；搬一个
零调用者的公开方法正是本仓库一路在防的那个形状。但 ``rehome`` **仍然要搬这张表的行**
——它有指向 ``events(id)`` 的外键，留着就会挡住 ``events.merge`` 的 ``DELETE``。
所以合并语义（同键合并证据、抬置信度）在这里以私有形态存在，等 S6 有了写入者再开口。

**``rehome`` 为什么不能写成一句 UPDATE。** ``event_participants`` 上是
``UNIQUE(event_id, person_id, role)``、``event_entities`` 上是
``PRIMARY KEY(event_id, entity_id, relation)``：源与目标两边都有同一行时，直接改
``event_id`` 会撞唯一约束。1.0 的做法是逐行读出来、按 ``upsert`` 的合并语义写进目标、
再删源行；这里照做，而且**全程一条连接、一个事务**——两张表的搬迁必须同生共死，
否则中途失败会留下搬了一半的投影。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sentrix.domain.memories.events import (
    PARTICIPANT_ROLE_VISIBLE_SUBJECT,
    EventEntityRecord,
    EventParticipantRecord,
)
from sentrix.infrastructure.persistence.database import Database

_PARTICIPANT_COLUMNS = (
    "ep.id, ep.event_id, ep.person_id, ep.role, ep.evidence_ids_json, ep.confidence,"
    " ep.revision, e.canonical_name AS person_name, e.status AS person_status"
)


def _new_id(prefix: str) -> str:
    """1.0 ``make_id``：``f"{prefix}_{uuid4().hex[:12]}"``。"""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _dedupe(values: Sequence[str]) -> list[str]:
    """1.0 ``list(dict.fromkeys(evidence_ids or []))``：保序去重。"""
    return list(dict.fromkeys(value for value in values if value))


def _evidence(raw: str | None) -> tuple[str, ...]:
    try:
        items = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return ()
    return tuple(str(item) for item in items) if isinstance(items, list) else ()


def _row_to_participant(row: sqlite3.Row) -> EventParticipantRecord:
    return EventParticipantRecord(
        id=row["id"],
        event_id=row["event_id"],
        person_id=row["person_id"],
        role=row["role"],
        evidence_ids=_evidence(row["evidence_ids_json"]),
        confidence=row["confidence"],
        revision=row["revision"],
        person_name=row["person_name"] or "",
        person_status=row["person_status"] or "",
    )


class SqliteEventParticipantRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    # ---- 参与者 --------------------------------------------------------------

    def upsert_participant(
        self,
        event_id: str,
        person_id: str,
        *,
        role: str,
        evidence_ids: Sequence[str] = (),
        confidence: float = 0.0,
    ) -> EventParticipantRecord | None:
        with self._database.connect() as connection, connection:
            if not self._person_exists(connection, person_id):
                return None
            self._upsert_participant(connection, event_id, person_id, role, evidence_ids, confidence)
        return self._find(event_id, person_id, role)

    def upsert_entity(
        self,
        event_id: str,
        entity_id: str,
        relation: str,
        evidence_ids: Sequence[str],
        confidence: float = 0.0,
    ) -> None:
        """1.0 ``db.py:3287`` 的 ``upsert_event_entity``。

        **写入者到 S6 才出现**——本模块的 docstring 写着「2.0 现在没有任何一处写
        ``event_entities``……等 S6 有了写入者再开口」。T127 的语义实体抽取就是那个写入者，
        所以这段从 ``_rehome_entities`` 里那段私有形态提升成公开方法：**合并语义只留一份**
        （同键合并证据、置信度取 ``MAX``），搬迁与写入走同一套。
        """
        with self._database.connect() as connection, connection:
            timestamp = _now()
            existing = connection.execute(
                "SELECT evidence_ids_json, confidence FROM event_entities"
                " WHERE event_id = ? AND entity_id = ? AND relation = ?",
                (event_id, entity_id, relation),
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO event_entities"
                    " (event_id, entity_id, relation, evidence_ids_json, confidence,"
                    "  created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        event_id,
                        entity_id,
                        relation,
                        json.dumps(_dedupe(evidence_ids), ensure_ascii=False),
                        float(confidence or 0),
                        timestamp,
                        timestamp,
                    ),
                )
                return
            merged = _dedupe([*_evidence(existing["evidence_ids_json"]), *evidence_ids])
            connection.execute(
                "UPDATE event_entities SET evidence_ids_json = ?, confidence = MAX(confidence, ?),"
                " updated_at = ? WHERE event_id = ? AND entity_id = ? AND relation = ?",
                (
                    json.dumps(merged, ensure_ascii=False),
                    float(confidence or 0),
                    timestamp,
                    event_id,
                    entity_id,
                    relation,
                ),
            )

    def list_participants(self, event_id: str) -> Sequence[EventParticipantRecord]:
        with self._database.connect() as connection:
            rows = connection.execute(
                f"SELECT {_PARTICIPANT_COLUMNS} FROM event_participants ep"
                " JOIN entities e ON e.id = ep.person_id"
                " WHERE ep.event_id = ? ORDER BY ep.updated_at DESC",
                (event_id,),
            ).fetchall()
        return [_row_to_participant(row) for row in rows]

    def list_event_entities(self, event_id: str) -> Sequence[EventEntityRecord]:
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT event_id, entity_id, relation, evidence_ids_json, confidence"
                " FROM event_entities WHERE event_id = ? ORDER BY relation, entity_id",
                (event_id,),
            ).fetchall()
        return [
            EventEntityRecord(
                event_id=str(row["event_id"]),
                entity_id=str(row["entity_id"]),
                relation=str(row["relation"] or ""),
                evidence_ids=_evidence(row["evidence_ids_json"]),
                confidence=float(row["confidence"] or 0.0),
            )
            for row in rows
        ]

    def cooccurrence_event_count(self, left_person_id: str, right_person_id: str) -> int:
        """1.0 ``db.py:2517-2522`` 那条自连接的 ``SELECT DISTINCT``，逐字搬。"""
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT ep.event_id FROM event_participants ep"
                " JOIN event_participants other ON other.event_id = ep.event_id"
                " WHERE ep.person_id = ? AND other.person_id = ?"
                " AND ep.role = ? AND other.role = ?",
                (
                    left_person_id,
                    right_person_id,
                    PARTICIPANT_ROLE_VISIBLE_SUBJECT,
                    PARTICIPANT_ROLE_VISIBLE_SUBJECT,
                ),
            ).fetchall()
        return len(rows)

    # ---- 合并时的换家 ----------------------------------------------------------

    def rehome(self, source_event_id: str, target_event_id: str) -> int:
        if source_event_id == target_event_id:
            return 0
        with self._database.connect() as connection, connection:
            moved = self._rehome(connection, source_event_id, target_event_id)
        return moved

    # ---- 连接内的原语（供本文件的公开方法共用同一条事务） -------------------------

    @staticmethod
    def _person_exists(connection: sqlite3.Connection, person_id: str) -> bool:
        row = connection.execute("SELECT 1 FROM entities WHERE id = ?", (person_id,)).fetchone()
        return row is not None

    @classmethod
    def _upsert_participant(
        cls,
        connection: sqlite3.Connection,
        event_id: str,
        person_id: str,
        role: str,
        evidence_ids: Sequence[str],
        confidence: float,
    ) -> None:
        """1.0 ``db.py:2486-2506``：同键合并证据、``MAX(confidence, ?)``、``revision + 1``。

        ``MAX`` 而不是覆盖：已经确信的事不该因为又来了一条低置信证据而变得可疑——
        与事实侧 ``decide_fact_write`` 的 REINFORCE 是同一条口径。
        """
        evidence = _dedupe(evidence_ids)
        row = connection.execute(
            "SELECT id, evidence_ids_json FROM event_participants"
            " WHERE event_id = ? AND person_id = ? AND role = ?",
            (event_id, person_id, role),
        ).fetchone()
        timestamp = _now()
        if row is not None:
            merged = _dedupe([*_evidence(row["evidence_ids_json"]), *evidence])
            connection.execute(
                "UPDATE event_participants SET evidence_ids_json = ?, confidence = MAX(confidence, ?),"
                " revision = revision + 1, updated_at = ? WHERE id = ?",
                (
                    json.dumps(merged, ensure_ascii=False),
                    float(confidence or 0),
                    timestamp,
                    row["id"],
                ),
            )
            return
        connection.execute(
            "INSERT INTO event_participants"
            " (id, event_id, person_id, role, evidence_ids_json, confidence, revision,"
            "  created_at, updated_at) VALUES (?,?,?,?,?,?,1,?,?)",
            (
                _new_id("event_person"),
                event_id,
                person_id,
                role,
                json.dumps(evidence, ensure_ascii=False),
                float(confidence or 0),
                timestamp,
                timestamp,
            ),
        )

    @classmethod
    def _rehome(cls, connection: sqlite3.Connection, source_event_id: str, target_event_id: str) -> int:
        rows = connection.execute(
            "SELECT * FROM event_participants WHERE event_id = ?", (source_event_id,)
        ).fetchall()
        for row in rows:
            cls._upsert_participant(
                connection,
                target_event_id,
                row["person_id"],
                row["role"],
                _evidence(row["evidence_ids_json"]),
                row["confidence"],
            )
        connection.execute("DELETE FROM event_participants WHERE event_id = ?", (source_event_id,))
        cls._rehome_entities(connection, source_event_id, target_event_id)
        return len(rows)

    @staticmethod
    def _rehome_entities(connection: sqlite3.Connection, source_event_id: str, target_event_id: str) -> None:
        """1.0 ``db.py:5537-5545`` 的 ``event_entities`` 那一段。

        S5 没有任何一处写这张表（1.0 的写入者属 S6），所以现在它恒为零行——**但这段
        不能省**：外键在，哪天 S6 接上写入者，缺了它的 ``events.merge`` 会当场炸。
        """
        rows = connection.execute(
            "SELECT * FROM event_entities WHERE event_id = ?", (source_event_id,)
        ).fetchall()
        timestamp = _now()
        for row in rows:
            existing = connection.execute(
                "SELECT evidence_ids_json, confidence FROM event_entities"
                " WHERE event_id = ? AND entity_id = ? AND relation = ?",
                (target_event_id, row["entity_id"], row["relation"]),
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO event_entities"
                    " (event_id, entity_id, relation, evidence_ids_json, confidence,"
                    "  created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        target_event_id,
                        row["entity_id"],
                        row["relation"],
                        row["evidence_ids_json"],
                        row["confidence"],
                        timestamp,
                        timestamp,
                    ),
                )
                continue
            merged = _dedupe(
                [*_evidence(existing["evidence_ids_json"]), *_evidence(row["evidence_ids_json"])]
            )
            connection.execute(
                "UPDATE event_entities SET evidence_ids_json = ?, confidence = MAX(confidence, ?),"
                " updated_at = ? WHERE event_id = ? AND entity_id = ? AND relation = ?",
                (
                    json.dumps(merged, ensure_ascii=False),
                    row["confidence"],
                    timestamp,
                    target_event_id,
                    row["entity_id"],
                    row["relation"],
                ),
            )
        connection.execute("DELETE FROM event_entities WHERE event_id = ?", (source_event_id,))

    def _find(self, event_id: str, person_id: str, role: str) -> EventParticipantRecord | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {_PARTICIPANT_COLUMNS} FROM event_participants ep"
                " JOIN entities e ON e.id = ep.person_id"
                " WHERE ep.event_id = ? AND ep.person_id = ? AND ep.role = ?",
                (event_id, person_id, role),
            ).fetchone()
        return _row_to_participant(row) if row else None
