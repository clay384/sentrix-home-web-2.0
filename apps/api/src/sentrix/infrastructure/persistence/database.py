"""SQLite connection factory and versioned migration runner.

1.0 had 59 tables built by ``CREATE TABLE IF NOT EXISTS`` plus ad-hoc
``ALTER TABLE ADD COLUMN`` patching at runtime, with no version record.  This
runner replaces that: every schema change is a numbered .sql file applied once,
inside a transaction, and recorded in ``schema_migrations``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from pathlib import Path
from types import TracebackType
from typing import Literal

BUSY_TIMEOUT_MS = 30_000


class MigrationError(RuntimeError):
    """迁移不可执行——宁可启动失败，也不静默跑出一个没有 schema 的库。"""


class TransactionError(RuntimeError):
    """A repository failure prevents the enclosing unit of work from committing."""


class _ManagedConnection(sqlite3.Connection):
    """Repository ``with connection`` blocks cannot commit an enclosing unit of work."""

    managed = False
    rollback_only = False

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        if self.managed:
            if exc_type is not None:
                self.rollback_only = True
            return False
        return super().__exit__(exc_type, exc_value, traceback)


def _statements(sql_text: str) -> list[str]:
    """把迁移脚本切成语句，并丢掉整行注释。

    不用 ``Connection.executescript``：它会先把挂起事务隐式 COMMIT 掉，那样
    ``BEGIN IMMEDIATE`` 拿到的写锁会在脚本执行前就释放，串行化与回滚保护同时失效。
    代价是这里不解析字符串字面量——迁移脚本里不要写含 ``;`` 的字符串常量。
    """
    lines = [line for line in sql_text.splitlines() if not line.strip().startswith("--")]
    return [statement.strip() for statement in "\n".join(lines).split(";") if statement.strip()]


class Database:
    def __init__(self, path: Path, migrations_dir: Path) -> None:
        self.path = Path(path)
        self.migrations_dir = Path(migrations_dir)
        self._transaction_connection: ContextVar[_ManagedConnection | None] = ContextVar(
            f"sentrix_transaction_{id(self)}", default=None
        )
        self._savepoint_sequence = 0

    def _open(self) -> _ManagedConnection:
        connection = sqlite3.connect(self.path, factory=_ManagedConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        return connection

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        active = self._transaction_connection.get()
        if active is not None:
            try:
                yield active
            except BaseException:
                active.rollback_only = True
                raise
            return
        connection = self._open()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Share one connection across repositories, with nested savepoint isolation.

        This covers SQLite writes only. Media files, cloud journals and derived
        indexes remain the caller's separately managed resources.
        """
        active = self._transaction_connection.get()
        if active is not None:
            self._savepoint_sequence += 1
            name = f"sentrix_uow_{self._savepoint_sequence}"
            previous_rollback_only = active.rollback_only
            active.execute(f"SAVEPOINT {name}")
            try:
                yield active
                if active.rollback_only and not previous_rollback_only:
                    raise TransactionError("A nested repository operation failed")
                active.execute(f"RELEASE SAVEPOINT {name}")
            except BaseException:
                active.execute(f"ROLLBACK TO SAVEPOINT {name}")
                active.execute(f"RELEASE SAVEPOINT {name}")
                active.rollback_only = previous_rollback_only
                raise
            return

        connection = self._open()
        connection.managed = True
        token = self._transaction_connection.set(connection)
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            if connection.rollback_only:
                raise TransactionError("A repository operation failed; the unit of work was rolled back")
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            self._transaction_connection.reset(token)
            connection.close()

    def migrations(self) -> list[tuple[str, Path]]:
        """磁盘上的 ``(版本号, 脚本路径)``，按版本号排序。

        目录不存在、或一个 ``.sql`` 都没有，都是硬错误：那说明部署漏了 package-data，
        或迁移根本没被带上。放过去只会让服务带着空 schema 起来，直到第一次写入才炸。
        """
        if not self.migrations_dir.is_dir():
            raise MigrationError(f"迁移目录不存在: {self.migrations_dir}")
        scripts = sorted(self.migrations_dir.glob("*.sql"))
        if not scripts:
            raise MigrationError(f"迁移目录里没有任何 .sql: {self.migrations_dir}")
        return [(script.stem.split("_", 1)[0], script) for script in scripts]

    def applied_versions(self) -> set[str]:
        with self.connect() as connection:
            return {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}

    def migrate(self) -> list[str]:
        pending = self.migrations()
        self.path.parent.mkdir(parents=True, exist_ok=True)

        applied: list[str] = []
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version TEXT PRIMARY KEY,"
                " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
            )
            for version, script in pending:
                if self._apply(connection, script, version):
                    applied.append(version)
        return applied

    @staticmethod
    def _apply(connection: sqlite3.Connection, script: Path, version: str) -> bool:
        """一个迁移一个事务，且「读已应用集合」也在同一事务里。

        ``BEGIN IMMEDIATE`` 先拿写锁再读：两个 worker 同时启动时，后到的会阻塞在锁上，
        拿到锁时前一个已经提交，于是看见版本号已存在并跳过，而不是重复执行 DDL。
        若改成在 autocommit 下先读、再另起事务写，就留下 TOCTOU 窗口，撞上
        ``table ... already exists``。
        """
        previous = connection.isolation_level
        connection.isolation_level = None  # 关掉 sqlite3 的隐式事务，边界由本函数管
        try:
            connection.execute("BEGIN IMMEDIATE")
            done = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
            ).fetchone()
            if done is not None:
                connection.execute("COMMIT")
                return False
            for statement in _statements(script.read_text(encoding="utf-8")):
                connection.execute(statement)
            connection.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
            connection.execute("COMMIT")
            return True
        except BaseException:
            with suppress(sqlite3.OperationalError):  # BEGIN 就没成功时没有事务可回滚
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.isolation_level = previous
