# Sentrix Home Web 2.0 — S0–S1 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 搭起 2.0 的工程骨架与真相基线（S0），并打通第一条端到端竖切——导入一张真实照片，在浏览器里看到原图（S1）。

**Architecture:** FastAPI 模块化单体，严格四层（domain / application / infrastructure / api），依赖方向由 `import-linter` 约束。SQLite 是业务事实源，用带版本号的 SQL 迁移管理 schema。2.0 独立于 1.0 运行（端口 9598/4274），共用 1.0 已在跑的模型服务（8100 llama-server、8101 bge-m3 sidecar）。

**Tech Stack:** Python 3.11 / FastAPI / uvicorn / pydantic-settings / 标准库 sqlite3 / Pillow + pillow-heif / pytest / ruff / mypy / import-linter · React 19 / TypeScript / Vite / Vitest

**依据文档:** `docs/architecture/2026-09-21-sentrix-home-web-2-implementation-design.md`

---

## 前置条件

远端仓库 `/home/asus/Github/sentrix-home-web2.0`，当前分支 `psh2.0`，工作树干净。

Python 3.11 可用性先确认（1.0 用的是 3.10）：

```bash
uv --version || echo "需要先装 uv"
```

> **通用规则**：从 Task 9 建立 `docs/architecture/module-map.md` **之后**，凡是新增或删除 `apps/api/src/sentrix/` 下 `.py` 文件的任务，都必须同步更新该表。`test_module_map.py` 双向强校验——代码里有而文档里没有、文档里有而代码里没有，都会失败。（Task 9 之前没有这张表，不适用。）
>
> **通用规则（回归）**：每个任务完成后，除了跑本任务的验收，还要确认**没有破坏更早任务的验收**。凡是改动或替换了更早任务所建文件的步骤（例如 Task 20 用「替换为」改 `bootstrap/container.py`、`main.py`，Task 15 用「替换为」改 `tests/conftest.py`），必须回头把那些任务的验收命令再跑一遍。理由：这些文件的消费者散落在前面的任务里，本任务的测试绿了不代表它们还绿。

## 文件结构

本计划锁定的文件（后续切片在此基础上生长）：

```text
sentrix-home-web2.0/
├── Makefile                                 统一入口
├── .pre-commit-config.yaml
├── pyproject.toml                           工作区级工具配置（ruff/mypy/import-linter）
├── apps/
│   ├── api/
│   │   ├── pyproject.toml                   包定义与依赖
│   │   ├── src/sentrix/
│   │   │   ├── __init__.py
│   │   │   ├── config.py                    类型化配置
│   │   │   ├── main.py                      FastAPI 装配 + v1 路由注册
│   │   │   ├── bootstrap/container.py       唯一依赖装配点
│   │   │   ├── api/
│   │   │   │   ├── deps.py                  依赖注入
│   │   │   │   ├── errors.py                领域错误 → HTTP 映射
│   │   │   │   └── routes/{health,media}.py 只做 HTTP 映射
│   │   │   ├── domain/media/
│   │   │   │   ├── models.py                Asset / Observation
│   │   │   │   └── ports.py                 仓储 / 存储 / 元数据 / 预览端口
│   │   │   ├── application/media/import_assets.py
│   │   │   └── infrastructure/
│   │   │       ├── persistence/
│   │   │       │   ├── database.py          连接 + 迁移执行器
│   │   │       │   ├── migrations/0001_core.sql
│   │   │       │   ├── migrations/0002_media.sql
│   │   │       │   └── repositories/{spaces,assets}.py
│   │   │       └── media/{storage,metadata,image_io}.py
│   │   └── tests/{unit,contract,integration}/
│   └── web/                                 React + TS + Vite
├── packages/contracts/openapi/sentrix-v1.yaml
├── docs/
│   ├── architecture/module-map.md
│   └── refactor/{capability-map.jsonl,behaviour-baseline.md,frozen.md}
└── scripts/{check_capability_map.py,check_module_map.py}
```

**职责边界（本计划必须守住）**

| 层 | 可以依赖 | 绝对不可以 |
|---|---|---|
| `domain/` | 只依赖 `domain/` 自己 | fastapi、sqlite3、PIL、httpx、os.getenv |
| `application/` | `domain/` | 上面那些 + 具体适配器类 |
| `infrastructure/` | `domain/` 的 port（实现它） | 被 domain 反向依赖 |
| `api/` | `application/` + `domain/` 的 DTO | SQL、文件系统、模型客户端 |
| `bootstrap/` | 全部（唯一组装点） | — |

---

# S0 — 骨架与真相基线

## Task 1: Python 项目骨架

**Files:**
- Create: `apps/api/pyproject.toml`
- Create: `apps/api/src/sentrix/__init__.py`
- Create: `apps/api/src/sentrix/domain/__init__.py`（空文件）
- Create: `apps/api/src/sentrix/application/__init__.py`（空文件）
- Create: `apps/api/src/sentrix/infrastructure/__init__.py`（空文件）
- Create: `apps/api/src/sentrix/api/__init__.py`（空文件）
- Create: `apps/api/src/sentrix/bootstrap/__init__.py`（空文件）
- Create: `apps/api/tests/unit/.gitkeep`
- Create: `apps/api/tests/contract/.gitkeep`
- Create: `apps/api/tests/integration/.gitkeep`
- Create: `apps/api/tests/e2e/.gitkeep`
- Create: `pyproject.toml`（工作区级工具配置）

- [ ] **Step 1: 创建包定义**

`apps/api/pyproject.toml`:

```toml
[project]
name = "sentrix"
version = "0.1.0"
description = "Sentrix Home 2.0 product API"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.115,<1",
    "uvicorn[standard]>=0.32,<1",
    "pydantic-settings>=2.6,<3",
    "python-multipart>=0.0.12,<1",
    "pillow>=11,<12",
    "pillow-heif>=0.18,<1",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.3,<9",
    "pytest-cov>=6,<7",
    "httpx>=0.28,<1",
    "ruff>=0.8,<1",
    "mypy>=1.13,<2",
    "import-linter>=2.1,<3",
    "pre-commit>=4,<5",
]

[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

# 迁移脚本不是 .py，不加这条不会进 wheel。editable 安装读源码树看不出来，
# 部署装 wheel 时 MIGRATIONS_DIR 会变成空目录——那正是 migrate() 过去静默 no-op 的分支。
[tool.setuptools.package-data]
sentrix = ["infrastructure/persistence/migrations/*.sql"]
```

`apps/api/src/sentrix/__init__.py`:

```python
"""Sentrix Home 2.0 product API."""

__version__ = "0.1.0"
```

- [ ] **Step 2: 创建工作区级工具配置**

`pyproject.toml`（仓库根，只放跨包工具配置，不放依赖）:

```toml
[tool.ruff]
line-length = 110
target-version = "py311"
extend-exclude = ["docs/"]

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM"]

[tool.ruff.lint.isort]
known-first-party = ["sentrix"]

[tool.ruff.lint.flake8-bugbear]
extend-immutable-calls = ["fastapi.Depends", "fastapi.File", "fastapi.Form", "fastapi.Query", "fastapi.Body", "fastapi.Header", "fastapi.Path"]

# 边界扩到全包 + scripts：实测 infrastructure / api / bootstrap / main / config 在
# --strict 下本来就是全绿，只查 domain + application 是把已经干净的两层漏在门外。
# 这是对设计 §6.2 边界决定的变更（原定只查 domain + application）。
[tool.mypy]
python_version = "3.11"
strict = true
files = ["apps/api/src/sentrix", "scripts"]

# 第三方包缺 stub / py.typed 是环境事实，不是本仓库代码的问题：集中在配置里表达，
# 而不是在每个 import 上散落 # type: ignore——那会把「这里真的有问题」也一起静音。
[[tool.mypy.overrides]]
module = ["pillow_heif", "yaml"]
ignore_missing_imports = true

[tool.pytest.ini_options]
testpaths = ["apps/api/tests"]
addopts = "-q"

[tool.coverage.run]
source = ["sentrix.domain", "sentrix.application"]

[tool.coverage.report]
fail_under = 85
show_missing = true

[tool.importlinter]
root_packages = ["sentrix"]
# domain-purity 的 forbidden_modules 里有 fastapi/sqlite3/PIL 等外部包，
# 不开这个开关 import-linter 会直接拒绝加载配置（契约一个都注册不上）。
include_external_packages = true

[[tool.importlinter.contracts]]
name = "domain-purity"
id = "domain-purity"
type = "forbidden"
source_modules = ["sentrix.domain"]
# os / qdrant_client 在设计 §2.3 里被点名禁止，这里必须补齐——少一条就等于那张表
# 只约束住了它列出的那几个包。
forbidden_modules = [
    "fastapi",
    "sqlite3",
    "PIL",
    "httpx",
    "uvicorn",
    "os",
    "qdrant_client",
    "sentrix.infrastructure",
    "sentrix.api",
    "sentrix.application",
    "sentrix.bootstrap",
]

[[tool.importlinter.contracts]]
name = "application-purity"
id = "application-purity"
type = "forbidden"
source_modules = ["sentrix.application"]
forbidden_modules = [
    "fastapi",
    "sqlite3",
    "PIL",
    "httpx",
    "sentrix.infrastructure",
    "sentrix.api",
    "sentrix.bootstrap",
]

[[tool.importlinter.contracts]]
name = "api-transport-only"
id = "api-transport-only"
type = "forbidden"
# 只查直接 import：api 必须经 bootstrap.container 取依赖，
# 而 container 必然间接摸到 sqlite3/PIL/infrastructure——不关掉传递检查这条契约永远红。
allow_indirect_imports = true
source_modules = ["sentrix.api"]
forbidden_modules = ["sqlite3", "PIL", "sentrix.infrastructure"]

# infrastructure 与 bootstrap 原先不在任何契约里，等于两层无人约束。
# infrastructure 只实现 domain 的 port，不许反向依赖上层。
[[tool.importlinter.contracts]]
name = "infrastructure-purity"
id = "infrastructure-purity"
type = "forbidden"
source_modules = ["sentrix.infrastructure"]
forbidden_modules = ["sentrix.api", "sentrix.application", "sentrix.bootstrap", "sentrix.main"]

[[tool.importlinter.contracts]]
name = "layers"
id = "layers"
type = "layers"
# sentrix.main 也要在链上：它是进程入口，只该向下调用，不许被下层反向 import。
layers = [
    "sentrix.main",
    "sentrix.api",
    "sentrix.application",
    "sentrix.domain",
]
```

- [ ] **Step 2b: 确认 `.gitignore` 内容**

`.gitignore` 必须包含（2.0 运行期产物 + 构建缓存 + 编辑器残留）：

```gitignore
.DS_Store
.venv/
__pycache__/
*.pyc
apps/photobench/datasets/
apps/photobench/results/
apps/photobench/logs/
apps/photobench/config/judge_providers.json
apps/photobench/config/runtime_connection.json
apps/photobench/config/*.bak-*
apps/photobench/config/vllm_targets.json

.pytest_cache/
.ruff_cache/
.mypy_cache/
.coverage
htmlcov/
apps/web/node_modules/
apps/web/dist/
apps/api/build/
/data/
*.egg-info/
```

注意 `data/` 必须写成 `/data/`（锚定到仓库根）。未锚定的 `data/` 会匹配任意层级的同名目录，将来 `apps/api/tests/fixtures/data/` 这类夹具会被静默忽略且 `git status` 无任何提示。

- [ ] **Step 3: 建 venv 并安装**

```bash
cd /home/asus/Github/sentrix-home-web2.0
uv python install 3.11
uv venv --python 3.11 --python-preference only-managed --clear --seed .venv
.venv/bin/pip install -q -e "apps/api[dev]"
cd apps/web && npm ci
```

前端依赖也在这里装，理由见 Task 10 的 `install` 目标注释。

- [ ] **Step 4: 验证导入**

Run: `.venv/bin/python -c "import sentrix; print(sentrix.__version__)"`
Expected: `0.1.0`

- [ ] **Step 4b: 验证刚配置的工具能跑**

Run: `cd /home/asus/Github/sentrix-home-web2.0 && .venv/bin/ruff check . && .venv/bin/mypy && .venv/bin/pytest`
Expected: `ruff` exit=0（`All checks passed!`）、`mypy` exit=0（`Success: no issues found in 15 source files`）、`pytest` exit=5（`no tests ran`）——此时只有 `.gitkeep`，尚无测试用例，Task 2 起转绿。
说明：写配置的任务必须运行配置。仅验证 `import sentrix` 会绕过 `files` / `source` / `testpaths` 三个路径配置，而 mypy 对不存在的路径是**硬失败**（`errors prevented further checking`，exit=2），会让整个 S0 的类型检查形同虚设。

- [ ] **Step 5: 提交**

```bash
git add pyproject.toml apps/api/pyproject.toml apps/api/src/sentrix apps/api/tests .gitignore
git commit -m "chore(api): scaffold python package and workspace tooling"
```

---

## Task 2: 类型化配置

**Files:**
- Create: `apps/api/src/sentrix/config.py`
- Test: `apps/api/tests/unit/test_config.py`

- [ ] **Step 1: 写失败测试**

`apps/api/tests/unit/test_config.py`:

```python
from sentrix.config import Settings


def test_defaults_point_at_2_0_ports_and_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTRIX_DATA_DIR", str(tmp_path))
    settings = Settings()

    assert settings.api_port == 9598
    assert settings.data_dir == tmp_path
    assert settings.database_path == tmp_path / "sentrix.db"
    assert settings.media_dir == tmp_path / "media"
    assert settings.preview_dir == tmp_path / "previews"


def test_env_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTRIX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SENTRIX_API_PORT", "9001")

    assert Settings().api_port == 9001


def test_required_dirs_are_derived_not_read_from_env(tmp_path, monkeypatch):
    """data_dir 是唯一可配的根，其余路径都由它派生，避免 1.0 那种路径散落。"""
    monkeypatch.setenv("SENTRIX_DATA_DIR", str(tmp_path))
    settings = Settings()

    assert settings.media_dir.parent == settings.data_dir
    assert settings.preview_dir.parent == settings.data_dir
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/pytest apps/api/tests/unit/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sentrix.config'`

- [ ] **Step 3: 实现**

`apps/api/src/sentrix/config.py`:

```python
"""Typed configuration.

Only ``SENTRIX_DATA_DIR`` and a handful of connection settings are configurable.
Everything else is derived, because 1.0's failure mode was ~126 env flags whose
values disagreed with each other and with the code.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SENTRIX_", extra="forbid")

    data_dir: Path = Field(default=Path("data"))

    api_port: int = 9598
    api_host: str = "0.0.0.0"

    # 2.0 reuses 1.0's already-running model services; it never manages them.
    vlm_base_url: str = "http://127.0.0.1:8100/v1"
    text_embedder_url: str = "http://127.0.0.1:8101"

    def model_post_init(self, _context: Any, /) -> None:  # noqa: D105
        self.data_dir = self.data_dir.expanduser().resolve()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_path(self) -> Path:
        return self.data_dir / "sentrix.db"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def preview_dir(self) -> Path:
        return self.data_dir / "previews"

    def ensure_directories(self) -> None:
        for path in (self.data_dir, self.media_dir, self.preview_dir):
            path.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    return Settings()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/pytest apps/api/tests/unit/test_config.py -v`
Expected: 3 passed

> 注意：本任务写下的 `files = [".../domain", ".../application"]` 只覆盖两层。该边界后来在本轮已扩到**整个 `sentrix` 包 + `scripts`**（见 Task 1 的 `pyproject.toml` 终稿与 Task 10 的 `make typecheck`）：实测 `infrastructure` / `api` / `bootstrap` / `main` / `config` 在 `--strict` 下本来就是全绿，把已经干净的两层漏在门外没有收益。这是对设计 §6.2 边界决定的变更。

- [ ] **Step 5: 提交**

```bash
git add apps/api/src/sentrix/config.py apps/api/tests/unit/test_config.py
git commit -m "feat(config): add typed settings with derived paths"
```

---

## Task 3: SQLite 连接与迁移执行器

**Files:**
- Create: `apps/api/src/sentrix/infrastructure/persistence/database.py`
- Create: `apps/api/src/sentrix/infrastructure/persistence/__init__.py`（空文件）
- Create: `apps/api/src/sentrix/infrastructure/__init__.py`（空文件）
- Test: `apps/api/tests/integration/test_migrations.py`

- [ ] **Step 1: 写失败测试**

`apps/api/tests/integration/test_migrations.py`:

```python
import sqlite3

import pytest

from sentrix.infrastructure.persistence.database import Database, MigrationError


def test_migrations_are_applied_once_and_recorded(tmp_path):
    db = Database(tmp_path / "t.db", migrations_dir=tmp_path / "migrations")
    (tmp_path / "migrations").mkdir()
    (tmp_path / "migrations" / "0001_a.sql").write_text("CREATE TABLE a (id TEXT PRIMARY KEY);")
    (tmp_path / "migrations" / "0002_b.sql").write_text("CREATE TABLE b (id TEXT PRIMARY KEY);")

    db.migrate()
    db.migrate()  # 幂等

    with db.connect() as conn:
        applied = [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    assert applied == ["0001", "0002"]
    assert {"a", "b", "schema_migrations"} <= tables


def test_failed_migration_rolls_back(tmp_path):
    db = Database(tmp_path / "t.db", migrations_dir=tmp_path / "migrations")
    (tmp_path / "migrations").mkdir()
    (tmp_path / "migrations" / "0001_bad.sql").write_text(
        "CREATE TABLE ok (id TEXT); CREATE TABLE ok (id TEXT);"  # 第二次建表必失败
    )

    try:
        db.migrate()
    except sqlite3.OperationalError:
        pass
    else:
        raise AssertionError("expected OperationalError")

    with db.connect() as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        applied = list(conn.execute("SELECT version FROM schema_migrations"))

    assert "ok" not in tables
    assert applied == []


def test_migrate_fails_when_no_migrations_are_found(tmp_path):
    """有迁移执行器却一个 .sql 都没有，是硬错误，不是合法的空库。

    静默 no-op 会建出一个只有 ``schema_migrations`` 的空库，让 ``database_healthy()``
    那种「表能查通就算好」的判断报 ok。
    """
    db = Database(tmp_path / "t.db", migrations_dir=tmp_path / "migrations")
    (tmp_path / "migrations").mkdir()

    with pytest.raises(MigrationError):
        db.migrate()


def test_write_connection_uses_wal_and_busy_timeout(tmp_path):
    db = Database(tmp_path / "t.db", migrations_dir=tmp_path / "migrations")
    (tmp_path / "migrations").mkdir()
    (tmp_path / "migrations" / "0001_a.sql").write_text("CREATE TABLE a (id TEXT PRIMARY KEY);")
    db.migrate()

    with db.connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/pytest apps/api/tests/integration/test_migrations.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 实现**

`apps/api/src/sentrix/infrastructure/persistence/database.py`:

```python
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
from pathlib import Path

BUSY_TIMEOUT_MS = 30_000


class MigrationError(RuntimeError):
    """迁移不可执行——宁可启动失败，也不静默跑出一个没有 schema 的库。"""


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

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        try:
            yield connection
        finally:
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/pytest apps/api/tests/integration/test_migrations.py -v`
Expected: 4 passed

- [ ] **Step 5: 提交**

```bash
git add apps/api/src/sentrix/infrastructure apps/api/tests/integration/test_migrations.py
git commit -m "feat(persistence): add sqlite database with versioned migrations"
```

---

## Task 4: 迁移 0001 与依赖装配容器

**Files:**
- Create: `apps/api/src/sentrix/infrastructure/persistence/migrations/0001_core.sql`
- Create: `apps/api/src/sentrix/bootstrap/container.py`
- Create: `apps/api/src/sentrix/bootstrap/__init__.py`（空文件）
- Test: `apps/api/tests/integration/test_container.py`

> 先建容器、后建应用：`main.py`（Task 5）依赖 `bootstrap.container`，反过来建会让 Task 4 收不了尾。

- [ ] **Step 1: 写失败测试**

`apps/api/tests/integration/test_container.py`:

```python
import pytest

from sentrix.bootstrap.container import MIGRATIONS_DIR, Container
from sentrix.config import Settings
from sentrix.infrastructure.persistence.database import MigrationError


def test_container_builds_all_adapters(tmp_path, monkeypatch):
    # conftest.py 要到 Task 5 才建，这里就地构造 Settings，不依赖 settings fixture
    monkeypatch.setenv("SENTRIX_DATA_DIR", str(tmp_path))
    settings = Settings()
    container = Container.build(settings)

    assert container.settings is settings
    assert container.database.path == settings.database_path
    assert container.database.migrations_dir.name == "migrations"


def test_database_healthy_reports_false_when_dir_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTRIX_DATA_DIR", str(tmp_path / "gone"))
    settings = Settings()
    container = Container.build(settings)

    assert container.database_healthy() is False


def test_migrate_fails_loudly_when_migrations_dir_is_missing(tmp_path, monkeypatch):
    """定位不到迁移目录时必须炸。

    静默 no-op 会让服务带着空 schema 起来，而 health 还会报 ok——1.0 那种
    「起得来、第一次写入才崩」的坏法。
    """
    monkeypatch.setattr("sentrix.bootstrap.container.MIGRATIONS_DIR", tmp_path / "gone")
    monkeypatch.setenv("SENTRIX_DATA_DIR", str(tmp_path / "data"))
    container = Container.build(Settings())

    with pytest.raises(MigrationError):
        container.database.migrate()


def test_database_healthy_reports_false_when_migrations_are_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTRIX_DATA_DIR", str(tmp_path))
    container = Container.build(Settings())

    # 还没跑 migrate()：表都不存在，健康检查不能报 ok
    assert container.database_healthy() is False

    container.database.migrate()
    assert container.database_healthy() is True


def test_shipped_migrations_directory_is_not_empty():
    """打包漏掉 .sql 时 MIGRATIONS_DIR 会变成空目录——那正是静默 no-op 的分支。

    editable 安装读的是源码树，所以这条拦不住 wheel 漏文件；真实验证见构建 wheel 的步骤。
    """
    assert sorted(MIGRATIONS_DIR.glob("*.sql")), f"{MIGRATIONS_DIR} 下没有任何 .sql 迁移"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/pytest apps/api/tests/integration/test_container.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sentrix.bootstrap.container'`

- [ ] **Step 3: 实现迁移 0001**

`apps/api/src/sentrix/infrastructure/persistence/migrations/0001_core.sql`:

```sql
-- 记忆空间（相册域）。字段语义取自 1.0 backend/db.py:403 的 memory_spaces。
-- 2.0 是空库起步，不迁移 1.0 数据。
--
-- 少了 1.0 的 source_path 列，是有意的：它在 1.0 只被写入（db.py:1392 的 INSERT /
-- UPSERT），全仓没有任何 SELECT 读它，属于只写不读的死列；而且导入来源本就不该进记忆
-- 模型（设计 §1.1 的硬约束「导入源不得写入记忆」）。
CREATE TABLE memory_spaces (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'household',
    status TEXT NOT NULL DEFAULT 'active',
    include_in_people INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_memory_spaces_status ON memory_spaces(status);
```

- [ ] **Step 4: 实现容器**

`apps/api/src/sentrix/bootstrap/container.py`:

```python
"""The single wiring point.

Nothing else in the codebase constructs an adapter.  Domain and application code
receive ports; only this module knows which concrete class implements them.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from sentrix.config import Settings
from sentrix.infrastructure.persistence.database import Database, MigrationError

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "infrastructure" / "persistence" / "migrations"


@dataclass(frozen=True)
class Container:
    settings: Settings
    database: Database

    @classmethod
    def build(cls, settings: Settings) -> Container:
        return cls(
            settings=settings,
            database=Database(settings.database_path, MIGRATIONS_DIR),
        )

    def database_healthy(self) -> bool:
        """能连上不等于健康：schema 没建齐同样是坏状态。

        只查 ``SELECT 1 FROM schema_migrations`` 会在「迁移目录没打进包、migrate()
        静默 no-op」时返回 ok——那正是 1.0 起得来、接口有响应、第一次写入才炸的坏法。
        所以要比对已应用集合与磁盘上可用的迁移集合。
        """
        try:
            expected = {version for version, _ in self.database.migrations()}
            applied = self.database.applied_versions()
        except (sqlite3.Error, MigrationError):
            return False
        return applied == expected
```

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv/bin/pytest apps/api/tests/integration/test_container.py -v`
Expected: 5 passed

- [ ] **Step 6: 提交**

```bash
git add apps/api/src/sentrix/bootstrap \
        apps/api/src/sentrix/infrastructure/persistence/migrations/0001_core.sql \
        apps/api/tests/integration/test_container.py
git commit -m "feat(bootstrap): add dependency container and core migration"
```

---

## Task 5: 应用装配与 /api/v1/health

**Files:**
- Create: `apps/api/src/sentrix/main.py`
- Create: `apps/api/src/sentrix/api/routes/health.py`
- Create: `apps/api/src/sentrix/api/routes/__init__.py`、`apps/api/src/sentrix/api/__init__.py`（空文件）
- Create: `apps/api/tests/conftest.py`
- Test: `apps/api/tests/contract/test_health.py`

- [ ] **Step 1: 写失败测试**

`apps/api/tests/conftest.py`:

```python
import pytest
from fastapi.testclient import TestClient

from sentrix.config import Settings
from sentrix.main import create_app


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    monkeypatch.setenv("SENTRIX_DATA_DIR", str(tmp_path))
    return Settings()


@pytest.fixture
def client(settings) -> TestClient:
    return TestClient(create_app(settings))
```

`apps/api/tests/contract/test_health.py`:

```python
from fastapi.testclient import TestClient


def test_health_returns_ok(client: TestClient):
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "0.1.0", "database": "ok"}


def test_unknown_v1_route_is_404(client: TestClient):
    assert client.get("/api/v1/nope").status_code == 404


def test_no_unversioned_public_routes(client: TestClient):
    """公开 API 只有 /api/v1；/api/health 这种 1.0 风格路径必须不存在。"""
    assert client.get("/api/health").status_code == 404
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/pytest apps/api/tests/contract/test_health.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sentrix.main'`

- [ ] **Step 3: 实现 health 路由与应用装配**

`apps/api/src/sentrix/api/routes/health.py`:

```python
from fastapi import APIRouter, Request

from sentrix import __version__

router = APIRouter(tags=["health"])


@router.get("/health")
def health(request: Request) -> dict[str, str]:
    database_ok = request.app.state.container.database_healthy()
    return {
        "status": "ok" if database_ok else "degraded",
        "version": __version__,
        "database": "ok" if database_ok else "error",
    }
```

`apps/api/src/sentrix/main.py`:

```python
"""FastAPI assembly. Route modules contain transport mapping only."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI

from sentrix import __version__
from sentrix.api.routes import health
from sentrix.bootstrap.container import Container
from sentrix.config import Settings, load_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    settings.ensure_directories()

    container = Container.build(settings)
    container.database.migrate()

    app = FastAPI(title="Sentrix Home API", version=__version__, docs_url=None, redoc_url=None)
    app.state.container = container

    v1 = APIRouter(prefix="/api/v1")
    v1.include_router(health.router)
    app.include_router(v1)
    return app


# 这里刻意没有模块级 ``app = create_app()``：那会让任何 ``import sentrix.main``
# （pytest 收集、脚本、类型检查）都带上副作用——在进程 CWD 下建出 data/、跑一次迁移。
# 进程入口走工厂：``uvicorn --factory sentrix.main:create_app``（见 Makefile 的 dev）。
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/pytest apps/api/tests/contract/test_health.py -v`
Expected: 3 passed

- [ ] **Step 5: 回归：容器未被破坏**

Run: `.venv/bin/pytest apps/api/tests/integration/test_container.py apps/api/tests/contract/test_health.py -v`
Expected: 8 passed

- [ ] **Step 6: 提交**

```bash
git add apps/api/src/sentrix/api apps/api/src/sentrix/main.py \
        apps/api/tests/contract apps/api/tests/conftest.py
git commit -m "feat(api): add v1 app assembly and health route"
```

---

## Task 6: 分层规则声明（import-linter）

**Files:**
- Modify: `pyproject.toml`
- Test: `apps/api/tests/unit/test_architecture_rules.py`

> **注意**：设计 §6.2 原定「S0 只声明、M1 结束才在 CI 打开全量强制」，**已改为声明即启用**——终态实测五个契约 `5 kept, 0 broken`，代码本来就合规，强制不带来任何摩擦，没必要把这条防线推迟到 M1。本任务把规则写进配置，并**立即进 `make ci`**。

- [ ] **Step 1: 写失败测试（验证规则本身存在且可跑）**

`apps/api/tests/unit/test_architecture_rules.py`:

```python
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
CONTRACT_COUNT = 5


def test_layering_contracts_all_hold():
    """跑全量契约，并要求「一条都没坏」。

    以前写的是 ``assert result.returncode in (0, 1)``——契约 BROKEN 时 returncode 正是 1，
    三条断言全过，于是这条测试永远不会因分层被破坏而失败。
    """
    result = subprocess.run(
        [".venv/bin/lint-imports"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert f"Contracts: {CONTRACT_COUNT} kept, 0 broken" in output, output


def test_domain_purity_contract_is_addressable_by_name():
    """契约能被按名寻址——配置读不到 / 契约名写错时上面那条会红，但这里定位更直接。"""
    result = subprocess.run(
        [".venv/bin/lint-imports", "--contract", "domain-purity"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "domain-purity KEPT" in output, output


def test_domain_does_not_import_infrastructure():
    """domain 若依赖 infrastructure，下面这条断言会先失败。"""
    domain = REPO_ROOT / "apps/api/src/sentrix/domain"
    offenders = [
        path
        for path in domain.rglob("*.py")
        if "from sentrix.infrastructure" in path.read_text(encoding="utf-8")
        or "import sentrix.infrastructure" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/pytest apps/api/tests/unit/test_architecture_rules.py -v`
Expected: FAIL — `AssertionError: Could not read any configuration.`（`pyproject.toml` 里还没有 `[tool.importlinter]`，契约一个都没注册，`lint-imports` 因此退出码非 0，断言 `returncode == 0` 先失败）

- [ ] **Step 3: 声明规则**

追加到根 `pyproject.toml`：

```toml
[tool.importlinter]
root_packages = ["sentrix"]
# domain-purity 的 forbidden_modules 里有 fastapi/sqlite3/PIL 等外部包，
# 不开这个开关 import-linter 会直接拒绝加载配置（契约一个都注册不上）。
include_external_packages = true

[[tool.importlinter.contracts]]
name = "domain-purity"
id = "domain-purity"
type = "forbidden"
source_modules = ["sentrix.domain"]
forbidden_modules = [
    "fastapi",
    "sqlite3",
    "PIL",
    "httpx",
    "uvicorn",
    "sentrix.infrastructure",
    "sentrix.api",
    "sentrix.application",
    "sentrix.bootstrap",
]

[[tool.importlinter.contracts]]
name = "application-purity"
id = "application-purity"
type = "forbidden"
source_modules = ["sentrix.application"]
forbidden_modules = [
    "fastapi",
    "sqlite3",
    "PIL",
    "httpx",
    "sentrix.infrastructure",
    "sentrix.api",
    "sentrix.bootstrap",
]

[[tool.importlinter.contracts]]
name = "api-transport-only"
id = "api-transport-only"
type = "forbidden"
# 只查直接 import：api 必须经 bootstrap.container 取依赖，
# 而 container 必然间接摸到 sqlite3/PIL/infrastructure——不关掉传递检查这条契约永远红。
allow_indirect_imports = true
source_modules = ["sentrix.api"]
forbidden_modules = ["sqlite3", "PIL", "sentrix.infrastructure"]

[[tool.importlinter.contracts]]
name = "layers"
id = "layers"
type = "layers"
layers = [
    "sentrix.api",
    "sentrix.application",
    "sentrix.domain",
]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/pytest apps/api/tests/unit/test_architecture_rules.py -v`
Expected: 3 passed

- [ ] **Step 5: 提交**

```bash
git add pyproject.toml apps/api/tests/unit/test_architecture_rules.py
git commit -m "chore(arch): declare layering contracts without enforcing them yet"
```

---

## Task 7: capability-map 与校验脚本

**Files:**
- Create: `docs/refactor/capability-map.jsonl`
- Create: `docs/refactor/frozen.md`
- Create: `scripts/check_capability_map.py`
- Test: `apps/api/tests/unit/test_capability_map.py`

- [ ] **Step 1: 写失败测试**

`apps/api/tests/unit/test_capability_map.py`:

```python
import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
CAPABILITY_MAP = REPO_ROOT / "docs" / "refactor" / "capability-map.jsonl"


def test_capability_map_entries_are_well_formed():
    lines = [
        json.loads(line) for line in CAPABILITY_MAP.read_text(encoding="utf-8").splitlines() if line.strip()
    ]

    assert lines, "capability-map 不能为空"
    for entry in lines:
        assert entry["origin_kind"] in {"migrated", "adapted", "NEW-2.0", "frozen"}
        assert entry["status"] in {"verified", "pending", "frozen"}
        assert entry["capability"]


def test_frozen_capabilities_are_documented():
    frozen = [
        json.loads(line)
        for line in CAPABILITY_MAP.read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line)["origin_kind"] == "frozen"
    ]
    frozen_doc = (REPO_ROOT / "docs/refactor/frozen.md").read_text(encoding="utf-8")

    for entry in frozen:
        assert entry["capability"] in frozen_doc, f"{entry['capability']} 未在 frozen.md 说明理由"


def test_checker_rejects_entry_with_missing_verification_file(tmp_path):
    bad = tmp_path / "capability-map.jsonl"
    bad.write_text(
        json.dumps(
            {
                "capability": "不存在的验证",
                "origin_kind": "migrated",
                "source_commit": "2d2cee7c",
                "source_path": "backend/app.py",
                "source_symbol": "health",
                "target_path": "apps/api/src/sentrix/api/routes/health.py",
                "verification": "tests/contract/does_not_exist.py",
                "status": "verified",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [".venv/bin/python", "scripts/check_capability_map.py", "--map", str(bad)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "does_not_exist.py" in result.stdout


def test_real_capability_map_has_no_problems():
    """拿仓库真实的 capability-map 跑一遍 checker——规则要在 pytest 里也生效。

    上面那条用的是 tmp_path 造的假 map，只覆盖单条规则；没有这条，checker 的其它
    规则就只在 `make check-docs` 里才执行，单元测试永远是绿的。
    """
    result = subprocess.run(
        [".venv/bin/python", "scripts/check_capability_map.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/pytest apps/api/tests/unit/test_capability_map.py -v`
Expected: FAIL — 文件不存在

- [ ] **Step 3: 写 frozen.md**

`docs/refactor/frozen.md`:

```markdown
# 2.0 冻结能力

冻结 = 不实现、不建表、不迁移代码。依据见设计文档 §1.3-D。

| 能力 | 冻结理由 |
|---|---|
| 行程 trips | 表 0 行；`db.py:3235 derive_trip_candidates` 无生产调用者，只在 `test_entities.py` 跑 |
| 故事 stories | 表 0 行；`POST/PATCH/DELETE /api/stories` 零调用 |
| 反馈 memory_feedback | 表 0 行 |
| 关系假设 relationship_hypotheses | 表 0 行；decision 端点零调用 |
| 人物角色假设 person_role_hypotheses | 表 0 行 |
| 实体合并候选 entity_merge_candidates | 表 0 行；confirm/reject 零调用 |
| 人物瞬间 person_moments | 表 0 行 |
| 洞察运行 person_insight_runs | 表 0 行 |
| 画像修订 person_portrait_revisions | 表 0 行 |
| 家庭范围组 family_scope_groups | 表 0 行 |
| 关系阻断 family_relationship_blocks | 表 0 行 |
| 邀请 invites | 表 0 行；而 8091 是 0.0.0.0 无鉴权 |
| 主动建议 | agent_proactivity_preferences / agent_impressions / agent_scene_cooldowns 全 0 |
| maintenance 运维接口 | `maintenance/*`（recheck / reindex-entities / summarize-events / entity-merge-candidates）零调用 |
| agent_contracts 校验 | `agent_contracts.py` 392 行，静态不可达 |
| E2B LoRA 兼容服务 | 进程不存在，8101 已被 bge-m3 sidecar 占用 |
| 硬编码地点别名表 `_PLACE_ALIASES` | **benchmark 泄漏**：含 `"赵州桥": ["赵县"]`、`"三峡坝址": ["夷陵区"]` —— PhotoBench QA 里有题「…是在河北哪个景点拍的呀？」，答案「河北赵县的赵州桥景区」，而 1.0 数据库里 "赵州桥" 出现 **0 次**、"三峡坝址" 0 次，系统只可能从 GPS 反编码得到 "赵县"。这张表唯一作用就是让该评测题能匹配。代码注释自称"这是通用双语地名知识，不是测评答案"，辩解本身即佐证。**2.0 不迁移该表**；S4 实现地点时只保留真正通用的中英地名桥接，放独立数据文件 `data/places/aliases.json`，可审查、可替换、可测试，且不得被 agent 提示词或评判逻辑引用 |

**重新启用的条件**：出现真实使用需求时，按新切片单独设计，不沿用 1.0 实现。
```

- [ ] **Step 4: 写初始 capability-map**

`docs/refactor/capability-map.jsonl`（S0 先登记健康检查与迁移两条，随切片增长）：

```jsonl
{"capability":"健康检查","origin_kind":"adapted","source_commit":"2d2cee7c","source_path":"backend/app.py","source_symbol":"health","target_path":"apps/api/src/sentrix/api/routes/health.py","verification":"apps/api/tests/contract/test_health.py","status":"verified"}
{"capability":"SQLite schema 版本管理","origin_kind":"NEW-2.0","source_commit":"2d2cee7c","source_path":"backend/db.py","source_symbol":"MemoryStore._init_schema","target_path":"apps/api/src/sentrix/infrastructure/persistence/database.py","verification":"apps/api/tests/integration/test_migrations.py","status":"verified"}
{"capability":"行程 trips","origin_kind":"frozen","source_commit":"2d2cee7c","source_path":"backend/db.py","source_symbol":"derive_trip_candidates","target_path":null,"verification":null,"status":"frozen"}
{"capability":"故事 stories","origin_kind":"frozen","source_commit":"2d2cee7c","source_path":"backend/app.py","source_symbol":"create_story","target_path":null,"verification":null,"status":"frozen"}
{"capability":"反馈 memory_feedback","origin_kind":"frozen","source_commit":"2d2cee7c","source_path":"backend/db.py","source_symbol":"memory_feedback","target_path":null,"verification":null,"status":"frozen"}
{"capability":"关系假设 relationship_hypotheses","origin_kind":"frozen","source_commit":"2d2cee7c","source_path":"backend/person_insights.py","source_symbol":"relationship_hypotheses","target_path":null,"verification":null,"status":"frozen"}
{"capability":"实体合并候选 entity_merge_candidates","origin_kind":"frozen","source_commit":"2d2cee7c","source_path":"backend/db.py","source_symbol":"entity_merge_candidates","target_path":null,"verification":null,"status":"frozen"}
{"capability":"人物瞬间 person_moments","origin_kind":"frozen","source_commit":"2d2cee7c","source_path":"backend/person_moments.py","source_symbol":"person_moments","target_path":null,"verification":null,"status":"frozen"}
{"capability":"洞察运行 person_insight_runs","origin_kind":"frozen","source_commit":"2d2cee7c","source_path":"backend/person_insights.py","source_symbol":"person_insight_runs","target_path":null,"verification":null,"status":"frozen"}
{"capability":"邀请 invites","origin_kind":"frozen","source_commit":"2d2cee7c","source_path":"backend/app.py","source_symbol":"create_invite","target_path":null,"verification":null,"status":"frozen"}
{"capability":"主动建议","origin_kind":"frozen","source_commit":"2d2cee7c","source_path":"backend/db.py","source_symbol":"agent_proactivity_preferences","target_path":null,"verification":null,"status":"frozen"}
{"capability":"maintenance 运维接口","origin_kind":"frozen","source_commit":"2d2cee7c","source_path":"backend/app.py","source_symbol":"maintenance_recheck","target_path":null,"verification":null,"status":"frozen"}
{"capability":"E2B LoRA 兼容服务","origin_kind":"frozen","source_commit":"2d2cee7c","source_path":"services/e2b_server/app.py","source_symbol":"app","target_path":null,"verification":null,"status":"frozen"}
{"capability":"agent_contracts 校验","origin_kind":"frozen","source_commit":"2d2cee7c","source_path":"backend/agent_contracts.py","source_symbol":"validate_turn_plan","target_path":null,"verification":null,"status":"frozen"}
{"capability":"硬编码地点别名表","origin_kind":"frozen","source_commit":"2d2cee7c","source_path":"backend/geocoding.py","source_symbol":"_PLACE_ALIASES","target_path":null,"verification":null,"status":"frozen"}
```

- [ ] **Step 5: 写校验脚本**

`scripts/check_capability_map.py`:

```python
#!/usr/bin/env python3
"""校验 capability-map.jsonl。

强制三件事（设计 §7）：
1. 每条 status=verified 的记录，verification 指向的文件必须真实存在
2. capability 名唯一：同一能力不能登记两次（一个文件实现多个能力是允许的）
3. origin_kind=frozen 的记录必须在 frozen.md 里有理由
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
VALID_KINDS = {"migrated", "adapted", "NEW-2.0", "frozen"}
VALID_STATUS = {"verified", "pending", "frozen"}
FROZEN_DOC = REPO_ROOT / "docs" / "refactor" / "frozen.md"


def load(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def check(entries: list[dict[str, Any]]) -> list[str]:
    problems: list[str] = []
    frozen_text = FROZEN_DOC.read_text(encoding="utf-8") if FROZEN_DOC.exists() else ""
    seen_names: set[str] = set()

    for entry in entries:
        name = entry.get("capability", "<unnamed>")
        if entry.get("origin_kind") not in VALID_KINDS:
            problems.append(f"{name}: origin_kind 非法 {entry.get('origin_kind')!r}")
        if entry.get("status") not in VALID_STATUS:
            problems.append(f"{name}: status 非法 {entry.get('status')!r}")

        if name in seen_names:
            problems.append(f"{name}: capability 名重复，已在别处登记")
        seen_names.add(name)

        if entry.get("status") == "verified":
            verification = entry.get("verification")
            if not verification:
                problems.append(f"{name}: status=verified 但没有 verification")
            elif not (REPO_ROOT / verification).exists():
                problems.append(f"{name}: verification 文件不存在 -> {verification}")

        if entry.get("origin_kind") == "frozen" and name not in frozen_text:
            problems.append(f"{name}: 已冻结但未在 docs/refactor/frozen.md 说明理由")

    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--map", default=str(REPO_ROOT / "docs/refactor/capability-map.jsonl"))
    args = parser.parse_args()

    problems = check(load(Path(args.map)))
    for problem in problems:
        print(problem)
    print(f"capability-map: {len(problems)} 个问题")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6: 跑测试确认通过**

Run: `.venv/bin/pytest apps/api/tests/unit/test_capability_map.py -v`
Expected: 4 passed

- [ ] **Step 7: 提交**

```bash
git add docs/refactor scripts/check_capability_map.py apps/api/tests/unit/test_capability_map.py
git commit -m "feat(refactor): add capability map, frozen list and validator"
```

---

## Task 8: 行为基线

**Files:**
- Create: `docs/refactor/behaviour-baseline.md`

> 这不是代码任务，但**必须由人核对**。这些常量只存在于 1.0 代码里、没有文档，是重写最容易丢的东西。

- [ ] **Step 1: 抄写并从 1.0 逐条核实**

`docs/refactor/behaviour-baseline.md`:

```markdown
# 1.0 行为基线

基线提交 `2d2cee7cbf1023f5019b9aaa9061af9c4cf45afa`。
2.0 重写时必须保持这些数值与规则不变，除非明确决策要改。

## 人脸

| 项 | 值 | 位置 |
|---|---|---|
| 在线匹配余弦阈值 | 0.30（硬编码，无 env 可配） | `backend/db.py:4815` |
| 全局重聚类阈值 | 0.30 | `backend/face_clustering.py:67` |
| 质量阈值 | 0.30 | `backend/face_clustering.py:67` |

## 检索融合

| 项 | 值 | 位置 |
|---|---|---|
| 默认策略 | `weighted_rrf` | `configs/retrieval/defaults.json` |
| RRF_K | 60 | `backend/retrieval/fusion.py:21` |
| anchor 加成 | +1.0 | `backend/retrieval/fusion.py:22` |
| 通道权重 | visual 2.5 / lexical 1.0 / metadata 1.0 / entity 1.0 / text 0.5 / adjacency 0.5 | `backend/retrieval/fusion.py:37` |
| adjacency | `kind="expander"`，默认关闭，`retrieve()` 恒返回空 | `backend/retrieval/adjacency.py:34`、`:47` |

## 助手

| 项 | 值 | 位置 |
|---|---|---|
| 默认 profile | `goal_driven_candidate` | `backend/agent_runtime/profile.py` |
| profile 预算 | 8 步 / 6 次调用 / 180s | `backend/agent_runtime/profile.py` |
| `max_search_calls` 硬收敛 | 1 | `backend/agent_runtime/runtime.py:1895` |
| 本地模型 max_tokens 上限 | min(1501, 4400 - prompt_tokens)，下限 64 | `backend/app.py:2680-2692` |

## 事件

| 项 | 值 | 位置 |
|---|---|---|
| 信号 | 时间 / GPS 距离线性评分（2km 衰减）/ 视觉相似 / 人物 | `backend/db.py:2000` 附近 |
| 归并时机 | 导入时同步 | `backend/db.py:2287` |

## 地点

| 项 | 值 | 位置 |
|---|---|---|
| 国内精度 | 区县级 | `backend/geocoding.py` |
| tianditu confidence | 0.90 | `backend/geocoding.py` |
| geonames confidence | 0.70 | `backend/geocoding.py` |

## 模型与嵌入

| 项 | 值 | 位置 |
|---|---|---|
| 视觉嵌入 | chinese-clip-ViT-L-14，768 维，进程内 GPU | `backend/embeddings/chinese_clip_visual.py` |
| 文本嵌入 | BAAI/bge-m3，1024 维，HTTP sidecar `127.0.0.1:8101`（CPU） | `backend/embeddings/bge_text.py` |
| 方案冻结 | `embeddings/scheme.py` 已钉死，不读 env | `backend/embeddings/scheme.py` |
| VLM 端点 | `http://127.0.0.1:8100/v1`（llama.cpp，OpenAI 兼容） | `backend/model_clients.py:537` |

## 待核实项

上表数值来自 2026-09-21 的代码阅读，实施时需逐条回到 1.0 源码确认，确认后把本行删掉。
```

- [ ] **Step 2: 逐条核实并勾掉「待核实项」**

对每一条执行：

```bash
cd /home/asus/Github/Sentrix-Home-Web
sed -n '4815p' backend/db.py        # 人脸阈值
sed -n '22p;37p' backend/retrieval/fusion.py   # RRF_K / 通道权重
```

确认无误后从 `behaviour-baseline.md` 删除「待核实项」小节。

- [ ] **Step 3: 提交**

```bash
git add docs/refactor/behaviour-baseline.md
git commit -m "docs(refactor): record 1.0 behaviour baseline constants"
```

---

## Task 9: module-map 脚手架与防腐校验

**Files:**
- Create: `scripts/check_module_map.py`
- Create: `docs/architecture/module-map.md`
- Test: `apps/api/tests/unit/test_module_map.py`

- [ ] **Step 1: 写失败测试**

`apps/api/tests/unit/test_module_map.py`:

```python
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
HEADER = "| 路径 | 职责 | 层 | 依赖谁 | 被谁依赖 |\n|---|---|---|---|---|\n"


def run_checker(src_root: Path, doc: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [".venv/bin/python", "scripts/check_module_map.py", "--src", str(src_root), "--doc", str(doc)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


def test_module_map_matches_real_imports():
    result = subprocess.run(
        [".venv/bin/python", "scripts/check_module_map.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_checker_flags_undocumented_module(tmp_path):
    """--src 是 src/ 根目录，表格路径也相对它——断言钉住完整路径，别只钉文件名。"""
    src_root = tmp_path / "src"
    package = src_root / "sentrix"
    package.mkdir(parents=True)
    (package / "orphan.py").write_text("x = 1\n")
    doc = tmp_path / "module-map.md"
    doc.write_text(HEADER, encoding="utf-8")

    result = run_checker(src_root, doc)

    assert result.returncode == 1
    assert "代码里有但 module-map 未记录: sentrix/orphan.py" in result.stdout


def test_checker_rejects_the_package_dir_as_src_root(tmp_path):
    """按旧约定把包目录当 --src 传，必须报错，不能静默换一套基准。

    这是本脚本最危险的失败模式：基准换掉了，结论照给，人以为校验通过了。
    传错时路径会整体丢掉 `sentrix/` 前缀，下面两条断言钉的就是这个错位形态。
    """
    src_root = tmp_path / "src"
    package = src_root / "sentrix"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "config.py").write_text('"""Config."""\n', encoding="utf-8")
    doc = tmp_path / "module-map.md"
    doc.write_text(
        HEADER
        + "| `sentrix/__init__.py` | 包标记 | — | — | — |\n"
        + "| `sentrix/config.py` | 配置 | — | — | — |\n",
        encoding="utf-8",
    )

    correct = run_checker(src_root, doc)
    assert correct.returncode == 0, correct.stdout + correct.stderr

    wrong_base = run_checker(package, doc)

    assert wrong_base.returncode == 1, wrong_base.stdout + wrong_base.stderr
    assert "代码里有但 module-map 未记录: __init__.py" in wrong_base.stdout
    assert "module-map 有但代码里不存在: sentrix/__init__.py" in wrong_base.stdout
    assert "module-map 有但代码里不存在: sentrix/config.py" in wrong_base.stdout


def test_checker_flags_wrong_dependency_columns(tmp_path):
    """反向验证：依赖列写错必须报红。

    没有这一条，上面那条只证明了「文件集合」是真的在查，「依赖谁 / 被谁依赖」两列
    仍可能是恒真的摆设——而那正是本脚本存在的理由之一。
    """
    src_root = tmp_path / "src"
    package = src_root / "sentrix"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "config.py").write_text('"""Config."""\n', encoding="utf-8")
    (package / "main.py").write_text("from sentrix.config import Settings\n", encoding="utf-8")
    doc = tmp_path / "module-map.md"
    doc.write_text(
        HEADER
        + "| `sentrix/__init__.py` | 包标记 | — | — | — |\n"
        + "| `sentrix/config.py` | 配置 | — | — | — |\n"
        + "| `sentrix/main.py` | 入口 | — | — | — |\n",
        encoding="utf-8",
    )

    result = run_checker(src_root, doc)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "sentrix/main.py: 「依赖谁」缺少 sentrix/config.py" in result.stdout
    assert "sentrix/config.py: 「被谁依赖」缺少 sentrix/main.py" in result.stdout


def test_checker_flags_row_with_missing_dependency_columns(tmp_path):
    """列数不足的行必须报错，不能被整行跳过。

    跳过等于给依赖校验留后门：那行写什么都不会红，而文档又照样接受它。
    """
    src_root = tmp_path / "src"
    package = src_root / "sentrix"
    package.mkdir(parents=True)
    (package / "config.py").write_text('"""Config."""\n', encoding="utf-8")
    doc = tmp_path / "module-map.md"
    doc.write_text(HEADER + "| `sentrix/config.py` | 配置 | — |\n", encoding="utf-8")

    result = run_checker(src_root, doc)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "sentrix/config.py: 行有 3 列，与表头的 5 列不一致" in result.stdout
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/pytest apps/api/tests/unit/test_module_map.py -v`
Expected: FAIL — 脚本不存在

- [ ] **Step 3: 实现校验脚本**

`scripts/check_module_map.py`:

```python
#!/usr/bin/env python3
"""校验 module-map.md 与真实代码一致。

1.0 的 docs/PROJECT_MEMORY.md 列了 16 个根本不存在的模块文件。这个脚本确保同样的事
不会再发生：源码里每个 .py 文件必须在文档里有条目，反之亦然。

依赖边同样被校验，而不是只写在文档的说明文字里：「依赖谁」由 ast 从真实的包内 import
推出，「被谁依赖」是它的转置——两列都由脚本从同一份解析结果推导后比对，所以不存在
「文档自称校验了、实际没人读」的列。只统计 `sentrix.*` 包内 import；stdlib 与第三方
（fastapi / sqlite3 / PIL 等）不进这两列。

列数不足的行会被报错而不是跳过：跳过等于给依赖校验开后门——那行写什么都不会红。

路径基准只有一份：``--src`` 传 **`src/` 根目录**（如 ``apps/api/src``），表格里写的也正是
相对它的路径（如 ``sentrix/config.py``）。以前 ``--src`` 传的是包目录而输出相对其父目录，
同一件事两个基准，调用方很容易传错而看不出来。
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SRC = REPO_ROOT / "apps" / "api" / "src"
DEFAULT_DOC = REPO_ROOT / "docs" / "architecture" / "module-map.md"
EMPTY_CELL = "—"
MIN_COLUMNS = 5  # 路径 / 职责 / 层 / 依赖谁 / 被谁依赖
HEADER_RE = re.compile(r"^\|\s*路径\s*\|")
ROW_RE = re.compile(r"^\|\s*`([^`]+\.py)`\s*\|(.*)\|\s*$")
CELL_PATH_RE = re.compile(r"`([^`]+)`")


def relative(path: Path, src: Path) -> str:
    """表格里写的路径形态：相对 ``src/`` 根目录，如 ``sentrix/config.py``。"""
    return str(path.relative_to(src)).replace("\\", "/")


def module_name(path: Path, src: Path) -> str:
    """文件路径 -> 绝对模块名；``__init__.py`` 归到包本身。"""
    parts = list(path.relative_to(src).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def package_of(path: Path, src: Path) -> str:
    """相对 import 的解析基准：普通模块取所在包，``__init__.py`` 取包自身。"""
    module = module_name(path, src)
    return module if path.name == "__init__.py" else module.rpartition(".")[0]


def source_paths(src: Path) -> set[str]:
    return {relative(path, src) for path in src.rglob("*.py") if "__pycache__" not in path.parts}


def imported_modules(path: Path, src: Path) -> set[str]:
    """文件里所有 import 涉及的绝对模块名（含相对 import 解析后的结果）。"""
    package = package_of(path, src)
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package
                for _ in range(node.level - 1):
                    base = base.rpartition(".")[0]
                module = f"{base}.{node.module}" if node.module else base
            else:
                module = node.module or ""
            names.add(module)
            # ``from pkg import mod`` 在 mod 是子模块时会把 pkg.mod 也导进来
            names.update(f"{module}.{alias.name}" for alias in node.names)
    return names


def dependency_edges(src: Path) -> dict[str, set[str]]:
    """每个文件 -> 它真实依赖的包内文件集合。"""
    files = {path: module_name(path, src) for path in src.rglob("*.py") if "__pycache__" not in path.parts}
    by_module = {module: path for path, module in files.items()}

    edges: dict[str, set[str]] = {}
    for path in files:
        edges[relative(path, src)] = {
            relative(target, src)
            for name in imported_modules(path, src)
            if (target := by_module.get(name)) is not None and target != path
        }
    return edges


def documented_paths(doc: Path) -> set[str]:
    """从 markdown 表格的第一列提取路径。"""
    text = doc.read_text(encoding="utf-8") if doc.exists() else ""
    paths = set()
    for line in text.splitlines():
        match = ROW_RE.match(line)
        if match:
            paths.add(match.group(1))
    return paths


def row_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def parse_cell(path: str, column: str, cell: str, problems: list[str]) -> set[str]:
    """一格依赖列：要么是 `—`，要么是反引号路径用 `、` 分隔。"""
    if cell == EMPTY_CELL:
        return set()
    names = CELL_PATH_RE.findall(cell)
    leftover = CELL_PATH_RE.sub("", cell).strip(" 、")
    if not names or leftover:
        problems.append(f"{path}: 「{column}」列格式非法，应为 {EMPTY_CELL} 或反引号路径 -> {cell!r}")
        return set()
    return set(names)


def documented_edges(doc: Path) -> tuple[dict[str, set[str]], dict[str, set[str]], list[str]]:
    """从表格解析「依赖谁」「被谁依赖」两列（0 基下标 3 / 4）。"""
    deps: dict[str, set[str]] = {}
    dependents: dict[str, set[str]] = {}
    problems: list[str] = []
    text = doc.read_text(encoding="utf-8") if doc.exists() else ""

    expected = MIN_COLUMNS
    for line in text.splitlines():
        if HEADER_RE.match(line):
            expected = len(row_cells(line))
            if expected < MIN_COLUMNS:
                problems.append(f"表头只有 {expected} 列，缺少「依赖谁」/「被谁依赖」——这两列不能被省略")
            continue

        match = ROW_RE.match(line)
        if not match:
            continue

        path = match.group(1)
        cells = row_cells(line)
        if len(cells) != expected:
            problems.append(f"{path}: 行有 {len(cells)} 列，与表头的 {expected} 列不一致")
            continue
        if expected < MIN_COLUMNS:
            continue  # 表头已经报错，这一行没有依赖列可读

        deps[path] = parse_cell(path, "依赖谁", cells[3], problems)
        dependents[path] = parse_cell(path, "被谁依赖", cells[4], problems)
    return deps, dependents, problems


def compare(path: str, column: str, real: set[str], documented: set[str], problems: list[str]) -> None:
    for missing in sorted(real - documented):
        problems.append(f"{path}: 「{column}」缺少 {missing}")
    for extra in sorted(documented - real):
        problems.append(f"{path}: 「{column}」多出 {extra}")


def main() -> int:
    parser = argparse.ArgumentParser(description="校验 module-map.md 与真实代码一致")
    parser.add_argument(
        "--src",
        default=str(DEFAULT_SRC),
        help="src/ 根目录（默认 apps/api/src）。表格里的路径相对它书写，如 sentrix/config.py",
    )
    parser.add_argument(
        "--doc",
        default=str(DEFAULT_DOC),
        help="module-map.md 路径（默认 docs/architecture/module-map.md）",
    )
    args = parser.parse_args()

    src = Path(args.src)
    doc = Path(args.doc)

    in_code = source_paths(src)
    in_doc = documented_paths(doc)

    problems: list[str] = []
    for missing in sorted(in_code - in_doc):
        problems.append(f"代码里有但 module-map 未记录: {missing}")
    for stale in sorted(in_doc - in_code):
        problems.append(f"module-map 有但代码里不存在: {stale}")

    edges = dependency_edges(src)
    dependents: dict[str, set[str]] = {path: set() for path in edges}
    for path, deps in edges.items():
        for dep in deps:
            dependents[dep].add(path)

    doc_deps, doc_dependents, cell_problems = documented_edges(doc)
    problems.extend(cell_problems)

    for path in sorted(set(edges) & set(doc_deps)):
        compare(path, "依赖谁", edges[path], doc_deps[path], problems)
    for path in sorted(set(dependents) & set(doc_dependents)):
        compare(path, "被谁依赖", dependents[path], doc_dependents[path], problems)

    for problem in sorted(problems):
        print(problem)
    print(f"module-map: {len(problems)} 个问题")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 写 module-map 初版**

`docs/architecture/module-map.md`:

```markdown
# 模块地图

**本文件由 `scripts/check_module_map.py` 校验**，脚本实际查两件事：

1. **文件集合双向一致**——源码里每个 `.py` 必须在下面有一行，文档里有但代码里不存在的
   条目会让 CI 失败。
2. **依赖边一致**——脚本用 `ast` 解析每个文件的包内 import，映射成文件路径后与「依赖谁」
   列比对；「被谁依赖」是同一次解析的转置，由脚本推导后比对。少写、多写、与转置不符
   都会失败。

两列只记 `sentrix` 包内的模块关系；stdlib 与第三方（fastapi / sqlite3 / pydantic 等）
不进这两列。

列含义：**层** = domain / application / infrastructure / api / bootstrap ·
**依赖谁** 与 **被谁依赖** 写 `src/` 下的相对路径，空集写 `—`，多项用 `、` 分隔。

| 路径 | 职责 | 层 | 依赖谁 | 被谁依赖 |
|---|---|---|---|---|
| `sentrix/__init__.py` | 包版本号 | — | — | `sentrix/api/routes/health.py`、`sentrix/main.py` |
| `sentrix/config.py` | 类型化配置；`data_dir` 是唯一可配根，其余路径派生 | — | — | `sentrix/bootstrap/container.py`、`sentrix/main.py` |
| `sentrix/main.py` | FastAPI 装配与 v1 路由注册；进程入口 | — | `sentrix/__init__.py`、`sentrix/api/routes/__init__.py`、`sentrix/api/routes/health.py`、`sentrix/bootstrap/container.py`、`sentrix/config.py` | — |
| `sentrix/bootstrap/__init__.py` | 包标记 | bootstrap | — | — |
| `sentrix/bootstrap/container.py` | 唯一依赖装配点；只有这里构造适配器 | bootstrap | `sentrix/config.py`、`sentrix/infrastructure/persistence/database.py` | `sentrix/main.py` |
| `sentrix/api/__init__.py` | 包标记 | api | — | — |
| `sentrix/api/routes/__init__.py` | 包标记 | api | — | `sentrix/main.py` |
| `sentrix/api/routes/health.py` | `GET /api/v1/health`，只做 HTTP 映射 | api | `sentrix/__init__.py` | `sentrix/main.py` |
| `sentrix/domain/__init__.py` | 包标记 | domain | — | — |
| `sentrix/application/__init__.py` | 包标记 | application | — | — |
| `sentrix/infrastructure/__init__.py` | 包标记 | infrastructure | — | — |
| `sentrix/infrastructure/persistence/__init__.py` | 包标记 | infrastructure | — | — |
| `sentrix/infrastructure/persistence/database.py` | SQLite 连接工厂与版本化迁移执行器 | infrastructure | — | `sentrix/bootstrap/container.py` |
```

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv/bin/pytest apps/api/tests/unit/test_module_map.py -v`
Expected: 5 passed

- [ ] **Step 6: 提交**

```bash
git add scripts/check_module_map.py docs/architecture/module-map.md apps/api/tests/unit/test_module_map.py
git commit -m "feat(docs): add self-validating module map"
```

---

## Task 10: Makefile 与 CI 门禁

**Files:**
- Create: `Makefile`

- [ ] **Step 1: 写 Makefile**

```makefile
PY := .venv/bin/python
PIP := .venv/bin/pip

.PHONY: help install lint format format-check typecheck arch test test-unit test-contract test-integration coverage check-docs web-check ci dev web-dev

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-18s %s\n", $$1, $$2}'

# 前端依赖必须在这里装。只装 Python 依赖时，全新克隆跑 `make ci` 的 web-check 会在
# `npx tsc -b` 处去 npm 下载冒名包 tsc@2.0.4，打印 "This is not the tsc command you are
# looking for" —— 它确实 exit 1（不是静默通过），但报错指向的是 npx 而不是缺依赖，
# 后来人极易当成 npx 抖动而把前端检查删掉，于是又造出一个"看起来在检查、其实没有"。
# 用 `npm ci` 而不是 `npm install`：apps/web/package-lock.json 在版本控制里，
# `npm ci` 按 lock 装、lock 与 package.json 不一致时直接报错，比 install 可复现。
install: ## 建 venv 并安装全部依赖
	uv python install 3.11
	uv venv --python 3.11 --python-preference only-managed --clear --seed .venv
	$(PIP) install -q -e "apps/api[dev]"
	cd apps/web && npm ci

lint: ## ruff 检查
	.venv/bin/ruff check apps/api/src apps/api/tests scripts

format: ## ruff 自动修复
	.venv/bin/ruff check --fix apps/api/src apps/api/tests scripts
	.venv/bin/ruff format apps/api/src apps/api/tests scripts

format-check: ## ruff 格式检查（只读）
	.venv/bin/ruff format --check apps/api/src apps/api/tests scripts

typecheck: ## mypy strict（全包 + scripts）
	.venv/bin/mypy

arch: ## 分层契约检查（已在 ci 链上）
	.venv/bin/lint-imports

test-unit: ## 单元测试
	.venv/bin/pytest apps/api/tests/unit -q

test-contract: ## 契约测试
	.venv/bin/pytest apps/api/tests/contract -q

test-integration: ## 集成测试
	.venv/bin/pytest apps/api/tests/integration -q

test: ## 全部测试
	.venv/bin/pytest -q

# S1 Task 13 已合入：domain/application 有代码，本目标实测 95.6%（阈值 85%），门禁为绿。
coverage: ## domain + application 覆盖率门禁（设计 §6.4：≥85%）
	.venv/bin/pytest --cov --cov-report=term-missing

check-docs: ## 文档防腐校验
	$(PY) scripts/check_capability_map.py
	$(PY) scripts/check_module_map.py

# 注意：所有目标必须从仓库根执行。从 apps/api/ 下跑 pytest 时 coverage 配置
# 不会被加载，fail_under=85 会静默失效（该目录下有独立的 pyproject.toml）。
# coverage 本身跑的就是全量测试，所以链上不再单列 test——同一套测试不必跑两遍。
ci: lint format-check typecheck arch check-docs coverage web-check ## CI 门禁
	@echo "ci: 全部通过"

dev: ## 起 2.0 API（9598）
	$(PY) -m uvicorn --factory sentrix.main:create_app --host 0.0.0.0 --port 9598 --app-dir apps/api/src

web-dev: ## 起 2.0 Web（4274）
	cd apps/web && npm run dev -- --port 4274

# 前端门禁。类型检查必须用 `tsc -b`，不能用 `tsc --noEmit`：apps/web/tsconfig.json 是
# {"files": [], "references": [...]} 结构，普通 tsc 不遍历 project references，
# `npx tsc --noEmit --listFiles` 命中 0 个文件、恒 exit 0——看起来在检查，其实永远通过。
web-check: ## 前端门禁：tsc -b + oxlint + vite build
	@test -d apps/web/node_modules || { echo "缺少 apps/web/node_modules，请先跑 make install"; exit 1; }
	cd apps/web && npx tsc -b
	cd apps/web && npx oxlint
	cd apps/web && npx vite build
```

> **门禁完整性（2026-09-22 补）**：`web-check` 是本计划写成之后补上的，`ci` 链当时全是后端，
> `apps/web` 挂掉了 `make ci` 照样全绿——这是设计 §6.5 明确要求「前端 `tsc + eslint + vitest`」
> 却一直没落地的一环。同时修掉一个空检查：类型检查**必须用 `tsc -b`**，`tsc --noEmit` 在本仓库
> 因为 `apps/web/tsconfig.json` 是 `{"files": [], "references": [...]}` 结构、不遍历 project
> references，实测命中 0 个文件、恒 exit 0。详见 Task 23 Step 5。
>
> **`node_modules` 断言（2026-09-22 补）**：`web-check` 第一条是存在性检查。人工跳过 `make install`
> 或 `node_modules` 被删时，`npx tsc -b` 会去 npm 下载冒名包 `tsc@2.0.4`，打印
> `This is not the tsc command you are looking for`——exit 1 不是静默通过，但报错指向 npx 而不是
> 缺依赖。断言把它换成一句「请先跑 make install」。反向验证：把 `apps/web/node_modules` 改名后
> 跑 `make web-check`，输出正是这句断言、exit 非 0；改回后 `make ci` 全绿。

- [ ] **Step 2: 验证每个目标能跑**

Run: `make lint && make typecheck && make test && make check-docs`
Expected: 全部通过，无报错

- [ ] **Step 3: 验证 arch（已在 ci 链上）**

Run: `make arch`
Expected: 输出各 contract 的检查结果，五个契约全部 PASS。`arch` 已是 `make ci` 的一环，Step 2 的 `make ci` 也会跑到它。

- [ ] **Step 4: 提交**

```bash
git add Makefile
git commit -m "chore: add make targets for lint, typecheck, test and doc checks"
```

---

## Task 11: 协作基础设施（pre-commit / CODEOWNERS / ADR）

**Files:**
- Create: `.pre-commit-config.yaml`
- Create: `.github/CODEOWNERS`
- Create: `docs/adr/0001-four-layer-architecture.md`
- Create: `docs/adr/0002-enable-layering-checks-from-s0.md`
- Create: `docs/adr/README.md`

- [ ] **Step 1: 写 pre-commit 配置**

```yaml
repos:
  - repo: local
    hooks:
      - id: ruff-check
        name: ruff check
        entry: .venv/bin/ruff check --fix
        language: system
        types: [python]
      - id: ruff-format
        name: ruff format
        entry: .venv/bin/ruff format
        language: system
        types: [python]
      - id: doc-checks
        name: capability-map + module-map
        entry: bash -c '.venv/bin/python scripts/check_capability_map.py && .venv/bin/python scripts/check_module_map.py'
        language: system
        pass_filenames: false
```

- [ ] **Step 2: 安装并验证**

```bash
.venv/bin/pre-commit install
.venv/bin/pre-commit run --all-files
```

Expected: 三个 hook 全部通过

- [ ] **Step 3: 写 CODEOWNERS**

`.github/CODEOWNERS`（按切片划分 owner；先全部指向仓库主，协作开始后替换为具体人）：

```
# 默认
*                       @asus

# 按模块划分（设计 §6.6）
apps/api/src/sentrix/domain/media/         @asus
apps/api/src/sentrix/domain/identities/    @asus
apps/api/src/sentrix/domain/retrieval/     @asus
apps/api/src/sentrix/domain/assistant/     @asus
apps/api/src/sentrix/domain/family/        @asus
apps/api/src/sentrix/domain/places/        @asus
apps/api/src/sentrix/infrastructure/       @asus
apps/api/src/sentrix/api/                  @asus
apps/web/                                  @asus
apps/photobench/                           @asus
docs/adr/                                  @asus
docs/refactor/capability-map.jsonl         @asus
```

- [ ] **Step 4: 写 ADR 目录与前两条决策记录**

`docs/adr/README.md`:

```markdown
# 架构决策记录（ADR）

**只增不改**：已经写下的 ADR 不被修改。要推翻一个决策，新增一条 ADR 并在其中引用被
取代的编号。

编号规则 `NNNN-短横线标题.md`，依次递增，不复用编号。取代关系在头部用 `取代：NNNN`
标注（见 `0002-enable-layering-checks-from-s0.md`）。
```

`docs/adr/0001-four-layer-architecture.md`:

```markdown
# 0001. 四层架构与 import-linter 强制

日期：2026-09-21
状态：已被 0002 部分取代（「分阶段启用」部分）

## 背景

1.0 在 7 周内从干净原型长成 5.4 万行后端，其中 `app.py` 3,915 行里混着 SQL、后台线程、
人脸检测降级策略和事件聚类；`photo-threads` 路由绕过 store 契约手工 `commit()`；
`db.py` 6,660 行里既有 schema 又有领域规则。这不是一次写坏的，是每一次"先临时这么写、
回头再抽"而从未回头累积出来的。

## 决策

采用 domain / application / infrastructure / api 四层，依赖方向单向：

- `domain` 只依赖自己，禁止 fastapi / sqlite3 / qdrant_client / PIL / httpx / uvicorn / `os`
  （**2026-09-21 校正：契约集实际范围**。此处原写「os.getenv」，实际契约禁的是**整个 `os`**，比原表述更宽——domain 取环境变量本身就是这条决策要禁的模式，留半个口子没有意义，纯类型用法走 `collections.abc`。同段所列也不是全集，完整范围见设计 §2.3。）
- `application` 只依赖 `domain`，自己拥有事务
- `infrastructure` 实现 domain 的 port，禁止被 domain 反向依赖
- `api` 只做 HTTP 映射，禁止 SQL、文件系统、模型客户端
- `bootstrap` 是唯一组装点

规则由 `import-linter` 在 CI 强制，违反即失败。**分阶段启用**：S0 只声明，
M1（S0–S3）结束后并入 `make ci`。理由：前期架构仍在试，规则需要跟着调；
而"增量加能力"的切片推进恰恰是最不容易被约束摩擦的场景。

例外通过 `ignore_imports` 声明，每条必须带注释说明理由。

（**2026-09-21 校正**：本段原写「CI 打印例外条数，数量上涨即为架构腐化信号」——`import-linter` 不提供该输出，没有任何检查统计例外条数，那是一句空头承诺。当前 `ignore_imports` 为空；若将来例外增多需要可观测性，应先实现统计再写进文档。）

## 后果

- 正面：架构不再随开发速度衰减；新成员（或新 agent）能从配置读出边界，不必口口相传；
  domain 测试是纯函数调用、毫秒级——1.0 的测试因为要起真环境而"为避免与生产 vLLM 争用"
  长期不跑，昂贵的测试必然烂掉
- 负面：写新能力时每个约多花 10–30 分钟组织代码；存在"为分层而分层"的风险
  （1.0 的病是文件过大，不是抽象不足，2.0 要避免撞向另一个极端）
```

`docs/adr/0002-enable-layering-checks-from-s0.md`:

```markdown
# 0002. 分层检查从 S0 即启用，不再推迟到 M1

日期：2026-09-21
状态：已接受
取代：0001 中的「分阶段启用」部分

## 背景

ADR 0001 决定分层规则「分阶段启用」：S0 只声明，M1（S0–S3）结束后才并入
`make ci` 强制。当时的理由是：前期架构仍在探索，规则需要跟着调，过早强制会带来摩擦。

## 变更原因

该前提不再成立。S0–S1 的全部代码在落地前已经过完整验证，`make arch` 在终态即为
`5 kept, 0 broken`——**代码本来就是合规的，强制不带来任何摩擦**。

而推迟强制有实际代价：验证过程中发现的三个缺陷（import-linter 配置因缺
`include_external_packages` 而根本加载不了、契约因 TOML 里无 `id` 而无法按名寻址、
`api-transport-only` 因默认追传递链而永不可能满足）**没有一处**能被 `pytest` 或
`make ci` 抓到——只有手工跑 `make arch` 才暴露。把这条防线推迟到 M1，意味着在盲区
最大的时期把它关掉。

## 决策

`make ci` 从 S0 起即包含 `arch`。分层契约内容一字未改：四条契约、`ignore_imports`
留空，全部与 ADR 0001 所述一致。

## 后果

- 正面：每一次 CI 都校验分层，架构约束不再依赖任何人记得手工跑 `make arch`
- 负面：无。代码已验证合规，不存在需要适应期的存量违规
```

- [ ] **Step 5: 提交**

```bash
git add .pre-commit-config.yaml .github/CODEOWNERS docs/adr
git commit -m "chore: add pre-commit hooks, code owners and first ADR"
```

---

## Task 12: 把 8091 拉回 systemd 托管

> 这不是 2.0 的代码，但**必须现在做**：8091 是 2026-09-18 手动起的裸进程（pid 4188847），
> `systemctl is-active sentrix-api.service` 返回 `inactive (dead)`。机器重启即失去生产环境，
> 而整个 2.0 开发期都以 1.0 为回退线。

**Files:**
- Modify: `/etc/systemd/system/sentrix-api.service`（153 上）

- [ ] **Step 1: 先看现状，确认服务定义与真实进程一致**

```bash
sshpass -p 'Abc123' ssh asus@192.168.0.153 '
systemctl cat sentrix-api.service
echo "--- 真实进程:"
ps -o pid,lstart,cmd -p 4188847 | cut -c1-200
'
```

期望：ExecStart 指向 `scripts/runtime/start_sentrix_api_8091.sh`，与真实进程的启动方式一致。

- [ ] **Step 2: 先备份现有 unit**

```bash
sshpass -p 'Abc123' ssh asus@192.168.0.153 \
  'echo "Abc123" | sudo -S cp /etc/systemd/system/sentrix-api.service /etc/systemd/system/sentrix-api.service.bak-20260921'
```

- [ ] **Step 3: 停掉裸进程，由 systemd 接管**

```bash
sshpass -p 'Abc123' ssh asus@192.168.0.153 '
kill 4188847
sleep 8
systemctl restart sentrix-api.service
sleep 15
systemctl is-active sentrix-api.service
curl -sf -m 5 http://127.0.0.1:8091/api/health | head -c 300
'
```

期望：`active`，且 `/api/health` 返回 200 与 JSON。

- [ ] **Step 4: 验证重启存活**

```bash
sshpass -p 'Abc123' ssh asus@192.168.0.153 '
systemctl is-enabled sentrix-api.service   # 应为 enabled
systemctl show sentrix-api.service -p Restart -p RestartSec
'
```

期望：`enabled`，且 `Restart` 不是 `no`（若是 `no`，补一个 `Restart=on-failure` 并 `daemon-reload`）。

- [ ] **Step 5: 记录到 runbook**

新建 `docs/runbooks/153-services.md`：

```markdown
# 153 服务清单与重启 SOP（2026-09-21）

| 端口 | 服务 | 托管方式 | 重启命令 |
|---|---|---|---|
| 8091 | 1.0 生产 API | systemd `sentrix-api.service` | `sudo systemctl restart sentrix-api.service` |
| 4174 | 1.0 Web | 手动 npm start | `cd Sentrix-Home-Web && npm start` |
| 8100 | llama-server | 手动 | 见 `.codex-tmp-start-cloud-services.sh` |
| 8101 | bge-m3 sidecar | 由 8091 启动脚本拉起 | `bash scripts/runtime/start_sentrix_api_8091.sh` |
| 8500 | model-manager API | systemd --user `sentrix-vllm-api.service` | `systemctl --user restart sentrix-vllm-api.service` |
| 8771 | 1.0 PhotoBench | nohup + pid | `services/photobench/scripts/start.sh` |

**不要碰**：11434（ollama，其他项目）、5173（FMA，无关项目）。

历史问题：8091 在 2026-09-18 曾被手动拉起为裸进程，systemd 显示 dead 但端口被占，
重启机器会静默失去生产环境。2026-09-21 已拉回 systemd 托管。
```

- [ ] **Step 6: 提交**

```bash
git add docs/runbooks/153-services.md
git commit -m "docs(runbooks): record 153 service inventory after systemd takeover"
```

---

# S1 — 媒体入库竖切

> **目标验收**：导入一张真实照片，在浏览器里点开看到**原图**。

## Task 13: 领域模型 Asset / Observation

**Files:**
- Create: `apps/api/src/sentrix/domain/__init__.py`（空文件）
- Create: `apps/api/src/sentrix/domain/media/__init__.py`（空文件）
- Create: `apps/api/src/sentrix/domain/media/models.py`
- Test: `apps/api/tests/unit/test_media_models.py`

- [ ] **Step 1: 写失败测试**

`apps/api/tests/unit/test_media_models.py`:

```python
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from sentrix.domain.media.models import Asset, AssetStatus, GpsPoint, MediaType


def test_asset_requires_known_media_type():
    with pytest.raises(ValueError, match="media_type"):
        Asset(
            id="asset_1",
            scope_id="home-default",
            file_name="a.bin",
            media_type="application",
            path="/tmp/a.bin",
            mime_type=None,
            size_bytes=1,
            status=AssetStatus.QUEUED,
            content_sha256="x",
            captured_at=None,
            captured_location=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )


def test_gps_point_rejects_out_of_range_latitude():
    with pytest.raises(ValueError, match="latitude"):
        GpsPoint(latitude=91.0, longitude=0.0)


def test_media_type_from_mime_prefers_mime_over_filename():
    assert MediaType.from_upload("image/heic", "photo.jpg") is MediaType.IMAGE
    assert MediaType.from_upload("audio/mpeg", None) is MediaType.AUDIO
    assert MediaType.from_upload(None, "clip.MOV") is MediaType.VIDEO


def test_asset_is_immutable():
    asset = Asset(
        id="asset_1",
        scope_id="home-default",
        file_name="a.jpg",
        media_type=MediaType.IMAGE,
        path="/tmp/a.jpg",
        mime_type="image/jpeg",
        size_bytes=1,
        status=AssetStatus.QUEUED,
        content_sha256="x",
        captured_at=None,
        captured_location=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    with pytest.raises(FrozenInstanceError):
        asset.status = AssetStatus.PROCESSED  # type: ignore[misc]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/pytest apps/api/tests/unit/test_media_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sentrix.domain.media'`

- [ ] **Step 3: 实现**

`apps/api/src/sentrix/domain/media/models.py`:

```python
"""Media domain model.

纯数据与规则，零 IO。字段语义取自 1.0 ``backend/db.py`` 的 assets / observations
建表语句，但去掉了 1.0 混在里面的大量派生字段（那些属于观察结果，不属于资产）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class MediaType(StrEnum):
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    TEXT = "text"

    @classmethod
    def from_upload(cls, content_type: str | None, file_name: str | None) -> MediaType:
        mime = (content_type or "").split(";", 1)[0].strip().lower()
        for prefix, media in (
            ("image/", cls.IMAGE),
            ("audio/", cls.AUDIO),
            ("video/", cls.VIDEO),
            ("text/", cls.TEXT),
        ):
            if mime.startswith(prefix):
                return media
        return cls.from_file_name(file_name or "")

    @classmethod
    def from_file_name(cls, file_name: str) -> MediaType:
        suffix = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
        if suffix in {"jpg", "jpeg", "png", "heic", "heif", "webp", "gif", "tif", "tiff"}:
            return cls.IMAGE
        if suffix in {"mov", "mp4", "m4v", "avi", "mkv"}:
            return cls.VIDEO
        if suffix in {"wav", "mp3", "m4a", "aac", "flac", "ogg"}:
            return cls.AUDIO
        return cls.TEXT


class AssetStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class GpsPoint:
    latitude: float
    longitude: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.latitude) or not -90 <= self.latitude <= 90:
            raise ValueError(f"latitude 越界: {self.latitude}")
        if not math.isfinite(self.longitude) or not -180 <= self.longitude <= 180:
            raise ValueError(f"longitude 越界: {self.longitude}")

    def as_text(self) -> str:
        """1.0 把 GPS 存成 ``captured_location`` 文本列，沿用该表示以兼容检索。"""
        return f"{self.latitude:.6f},{self.longitude:.6f}"


@dataclass(frozen=True, slots=True)
class Asset:
    id: str
    scope_id: str
    file_name: str
    media_type: MediaType
    path: str
    mime_type: str | None
    size_bytes: int
    status: AssetStatus
    content_sha256: str
    captured_at: datetime | None
    captured_location: GpsPoint | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.media_type, MediaType):
            raise ValueError(f"未知 media_type: {self.media_type!r}")


@dataclass(frozen=True, slots=True)
class Observation:
    """一次导入对资产抽取出的原始观察结果。"""

    id: str
    scope_id: str
    asset_id: str
    source_type: str
    captured_at: datetime | None
    confidence: float
    created_at: datetime
    updated_at: datetime
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/pytest apps/api/tests/unit/test_media_models.py -v`
Expected: 4 passed

- [ ] **Step 5: 验证 domain 没有被污染**

Run: `make arch`
Expected: `domain-purity` PASS（`domain/media/models.py` 只 import 标准库）

- [ ] **Step 6: 更新 module-map**

把本任务新建的每个 `.py` 按 Task 9 的表格格式补进 `docs/architecture/module-map.md`（路径 / 职责 / 层 / 依赖谁 / 被谁依赖）。`test_module_map.py` 会强校验，漏一个文件就红；**「依赖谁 / 被谁依赖」两列还必须与该文件真实的包内 import 一致**——`check_module_map.py` 用 `ast` 解析后逐条比对，少写、多写、以及与转置不符都会红。所以新文件的「依赖谁」，以及所有反过来依赖它的行的「被谁依赖」，要一起改对。

Run: `.venv/bin/pytest apps/api/tests/unit/test_module_map.py -v`
Expected: 5 passed

- [ ] **Step 7: 提交**

```bash
git add apps/api/src/sentrix/domain apps/api/tests/unit/test_media_models.py docs/architecture/module-map.md
git commit -m "feat(domain): add asset and observation models"
```

---

## Task 14: 领域端口

**Files:**
- Create: `apps/api/src/sentrix/domain/media/ports.py`
- Test: `apps/api/tests/unit/test_media_ports.py`

- [ ] **Step 1: 写失败测试**

`apps/api/tests/unit/test_media_ports.py`:

```python
from sentrix.domain.media import ports


def test_ports_are_protocols_not_concrete_classes():
    for name in ("AssetRepository", "MediaStorage", "MetadataReader", "PreviewEncoder"):
        port = getattr(ports, name)
        assert getattr(port, "_is_protocol", False), f"{name} 必须是 Protocol"


def test_supported_image_suffixes_are_declared_in_domain():
    """支持哪些格式是领域规则，不是适配器细节。"""
    assert ".heic" in ports.SUPPORTED_IMAGE_SUFFIXES
    assert ".jpg" in ports.SUPPORTED_IMAGE_SUFFIXES


def test_heic_needs_transcode_rule_lives_in_domain():
    assert ports.needs_transcode("a.heic", "image/heic") is True
    assert ports.needs_transcode("a.jpg", "image/jpeg") is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/pytest apps/api/tests/unit/test_media_ports.py -v`
Expected: FAIL — `ImportError: cannot import name 'ports' from 'sentrix.domain.media'`（包已存在，只是还没有 `ports.py`）

- [ ] **Step 3: 实现**

`apps/api/src/sentrix/domain/media/ports.py`:

```python
"""Domain ports and format rules.

格式支持与转码判定是**领域规则**（产品要求浏览器能打开原图），所以规则本身留在
domain；具体怎么解码由 infrastructure 实现。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Protocol

from sentrix.domain.media.models import Asset, AssetStatus, GpsPoint, MediaType, Observation

SUPPORTED_IMAGE_SUFFIXES = frozenset(
    {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".tif", ".tiff"}
)
# 浏览器无法原生渲染，必须服务端转码后再交付。
BROWSER_UNRENDERABLE_SUFFIXES = frozenset({".heic", ".heif"})
BROWSER_UNRENDERABLE_MIMES = frozenset({"image/heic", "image/heif"})


def needs_transcode(file_name: str, mime_type: str | None) -> bool:
    suffix = Path(file_name).suffix.lower()
    mime = (mime_type or "").split(";", 1)[0].strip().lower()
    return suffix in BROWSER_UNRENDERABLE_SUFFIXES or mime in BROWSER_UNRENDERABLE_MIMES


class AssetRepository(Protocol):
    def add(self, asset: Asset) -> None: ...

    def get(self, asset_id: str) -> Asset | None: ...

    def list_by_scope(self, scope_id: str, limit: int) -> Sequence[Asset]: ...

    def find_by_hash(self, scope_id: str, content_sha256: str) -> Asset | None: ...

    def update_status(self, asset_id: str, status: AssetStatus) -> None: ...


class ObservationRepository(Protocol):
    def add(self, observation: Observation) -> None: ...

    def get(self, observation_id: str) -> Observation | None: ...


class MediaStorage(Protocol):
    def store(self, asset_id: str, file_name: str, content: bytes) -> Path: ...

    def read(self, asset_id: str) -> bytes: ...

    def path_for(self, asset: Asset) -> Path: ...


class MetadataReader(Protocol):
    """从原始文件中抽取拍摄时间、GPS 与设备。"""

    def read(self, path: Path) -> tuple[datetime | None, GpsPoint | None, str | None]: ...


class PreviewEncoder(Protocol):
    """把原图编码成浏览器可直接渲染的 JPEG。"""

    def encode_jpeg(self, path: Path, *, max_dimension: int, quality: int) -> bytes: ...


class MediaTypeResolver(Protocol):
    def resolve(self, content_type: str | None, file_name: str | None) -> MediaType: ...
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/pytest apps/api/tests/unit/test_media_ports.py -v`
Expected: 3 passed

- [ ] **Step 5: 更新 module-map**

把本任务新建的每个 `.py` 按 Task 9 的表格格式补进 `docs/architecture/module-map.md`（路径 / 职责 / 层 / 依赖谁 / 被谁依赖）。`test_module_map.py` 会强校验，漏一个文件就红；**「依赖谁 / 被谁依赖」两列还必须与该文件真实的包内 import 一致**——`check_module_map.py` 用 `ast` 解析后逐条比对，少写、多写、以及与转置不符都会红。所以新文件的「依赖谁」，以及所有反过来依赖它的行的「被谁依赖」，要一起改对。

Run: `.venv/bin/pytest apps/api/tests/unit/test_module_map.py -v`
Expected: 5 passed

- [ ] **Step 6: 提交**

```bash
git add apps/api/src/sentrix/domain/media/ports.py apps/api/tests/unit/test_media_ports.py docs/architecture/module-map.md
git commit -m "feat(domain): add media ports and browser transcode rules"
```

---

## Task 15: 迁移 0002 与仓储

**Files:**
- Create: `apps/api/src/sentrix/infrastructure/persistence/migrations/0002_media.sql`
- Create: `apps/api/src/sentrix/infrastructure/persistence/repositories/__init__.py`（空文件）
- Create: `apps/api/src/sentrix/infrastructure/persistence/repositories/assets.py`
- Create: `apps/api/src/sentrix/infrastructure/persistence/repositories/spaces.py`
- Modify: `apps/api/tests/conftest.py`（追加 `migrations_ready_db` fixture）
- Test: `apps/api/tests/integration/test_asset_repository.py`

- [ ] **Step 1: 写失败测试**

`apps/api/tests/integration/test_asset_repository.py`:

```python
from datetime import UTC, datetime

from sentrix.domain.media.models import Asset, AssetStatus, GpsPoint, MediaType
from sentrix.infrastructure.persistence.database import Database
from sentrix.infrastructure.persistence.repositories.assets import SqliteAssetRepository
from sentrix.infrastructure.persistence.repositories.spaces import SqliteSpaceRepository


def make_asset(asset_id="asset_1", sha="abc123", scope="home-default") -> Asset:
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    return Asset(
        id=asset_id,
        scope_id=scope,
        file_name="IMG_0001.HEIC",
        media_type=MediaType.IMAGE,
        path=f"/data/media/{asset_id}_IMG_0001.HEIC",
        mime_type="image/heic",
        size_bytes=1234,
        status=AssetStatus.QUEUED,
        content_sha256=sha,
        captured_at=datetime(2018, 4, 1, 21, 5, 47, tzinfo=UTC),
        captured_location=GpsPoint(18.7883, 98.9853),
        created_at=now,
        updated_at=now,
    )


def test_roundtrip_preserves_gps_and_captured_at(migrations_ready_db: Database):
    repo = SqliteAssetRepository(migrations_ready_db)
    asset = make_asset()

    repo.add(asset)
    loaded = repo.get(asset.id)

    assert loaded is not None
    assert loaded.captured_location == GpsPoint(18.7883, 98.9853)
    assert loaded.captured_at == asset.captured_at
    assert loaded.content_sha256 == "abc123"


def test_find_by_hash_is_scoped(migrations_ready_db: Database):
    repo = SqliteAssetRepository(migrations_ready_db)
    repo.add(make_asset(asset_id="asset_a", sha="same", scope="album1"))

    assert repo.find_by_hash("album1", "same") is not None
    assert repo.find_by_hash("album2", "same") is None


def test_update_status_persists(migrations_ready_db: Database):
    repo = SqliteAssetRepository(migrations_ready_db)
    repo.add(make_asset())

    repo.update_status("asset_1", AssetStatus.PROCESSED)

    assert repo.get("asset_1").status is AssetStatus.PROCESSED


def test_memory_space_roundtrip(migrations_ready_db: Database):
    spaces = SqliteSpaceRepository(migrations_ready_db)
    scope_id = spaces.ensure("album1", "第一个相册")

    loaded = spaces.get(scope_id)

    assert loaded is not None
    assert loaded.name == "第一个相册"
    assert [space.id for space in spaces.list_all()] == [scope_id]


def test_ensure_is_idempotent(migrations_ready_db: Database):
    spaces = SqliteSpaceRepository(migrations_ready_db)

    spaces.ensure("album1", "第一个相册")
    spaces.ensure("album1", "改个名字")

    assert len(spaces.list_all()) == 1
    assert spaces.get("album1").name == "第一个相册"
```

`apps/api/tests/conftest.py` 替换为：

```python
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sentrix.config import Settings
from sentrix.infrastructure.persistence.database import Database
from sentrix.main import create_app


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    monkeypatch.setenv("SENTRIX_DATA_DIR", str(tmp_path))
    return Settings()


@pytest.fixture
def client(settings) -> TestClient:
    return TestClient(create_app(settings))


@pytest.fixture
def migrations_ready_db(tmp_path) -> Database:
    """真 SQLite + 真迁移文件，跑在临时目录里。"""
    migrations = Path(__file__).resolve().parents[1] / "src/sentrix/infrastructure/persistence/migrations"
    db = Database(tmp_path / "sentrix.db", migrations)
    db.migrate()
    return db
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/pytest apps/api/tests/integration/test_asset_repository.py -v`
Expected: FAIL — `ModuleNotFoundError: sentrix.infrastructure.persistence.repositories`

- [ ] **Step 3: 写迁移 0002**

`apps/api/src/sentrix/infrastructure/persistence/migrations/0002_media.sql`:

```sql
-- assets / observations / ingest_batches。
-- 字段语义取自 1.0 backend/db.py:422/:444/:413，但做了两处结构改进（设计 §3.2）：
--   1. 1.0 把 EXIF/GPS/反地理/派生数据全塞进 metadata_json，检索只能 LIKE '%…%' 全表扫。
--      2.0 把需要检索的字段提升为正式列：gps_latitude / gps_longitude / device。
--   2. 1.0 的 video 派生字段（parent_asset_id 等）本切片用不到，先不建。
CREATE TABLE assets (
    id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL DEFAULT 'home-default',
    batch_id TEXT,
    file_name TEXT NOT NULL,
    media_type TEXT NOT NULL,
    path TEXT NOT NULL,
    mime_type TEXT,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'queued',
    content_sha256 TEXT,
    captured_at TEXT,
    gps_latitude REAL,
    gps_longitude REAL,
    device TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_assets_scope ON assets(scope_id);
CREATE INDEX idx_assets_content_sha256 ON assets(content_sha256);
CREATE INDEX idx_assets_batch ON assets(batch_id);
CREATE INDEX idx_assets_captured_at ON assets(captured_at);

CREATE TABLE observations (
    id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL DEFAULT 'home-default',
    asset_id TEXT NOT NULL REFERENCES assets(id),
    source_type TEXT NOT NULL,
    captured_at TEXT,
    confidence REAL NOT NULL DEFAULT 0,
    caption TEXT,
    ocr_text TEXT NOT NULL DEFAULT '',
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_observations_asset ON observations(asset_id);
CREATE INDEX idx_observations_scope ON observations(scope_id);

CREATE TABLE ingest_batches (
    id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL DEFAULT 'home-default',
    status TEXT NOT NULL DEFAULT 'open',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);
```

- [ ] **Step 4: 实现仓储**

`apps/api/src/sentrix/infrastructure/persistence/repositories/spaces.py`:

```python
"""MemorySpace 仓储。2.0 是空库，只需 ensure/list。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sentrix.infrastructure.persistence.database import Database


@dataclass(frozen=True, slots=True)
class MemorySpace:
    id: str
    name: str
    kind: str
    status: str


class SqliteSpaceRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def ensure(self, scope_id: str, name: str, kind: str = "household") -> str:
        now = datetime.now(UTC).isoformat()
        with self._database.connect() as connection, connection:
            connection.execute(
                "INSERT INTO memory_spaces (id, name, kind, status, created_at, updated_at)"
                " VALUES (?, ?, ?, 'active', ?, ?)"
                " ON CONFLICT(id) DO NOTHING",
                (scope_id, name, kind, now, now),
            )
        return scope_id

    def list_all(self) -> list[MemorySpace]:
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT id, name, kind, status FROM memory_spaces ORDER BY created_at"
            ).fetchall()
        return [MemorySpace(row["id"], row["name"], row["kind"], row["status"]) for row in rows]

    def get(self, scope_id: str) -> MemorySpace | None:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT id, name, kind, status FROM memory_spaces WHERE id = ?", (scope_id,)
            ).fetchone()
        return MemorySpace(row["id"], row["name"], row["kind"], row["status"]) if row else None
```

`apps/api/src/sentrix/infrastructure/persistence/repositories/assets.py`:

```python
"""Asset / Observation 仓储。SQL 只出现在这一层。"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime

from sentrix.domain.media.models import Asset, AssetStatus, GpsPoint, MediaType, Observation
from sentrix.infrastructure.persistence.database import Database

_ASSET_COLUMNS = (
    "id, scope_id, file_name, media_type, path, mime_type, size_bytes, status,"
    " content_sha256, captured_at, gps_latitude, gps_longitude, created_at, updated_at"
)


def _parse_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _row_to_asset(row: sqlite3.Row) -> Asset:
    latitude, longitude = row["gps_latitude"], row["gps_longitude"]
    return Asset(
        id=row["id"],
        scope_id=row["scope_id"],
        file_name=row["file_name"],
        media_type=MediaType(row["media_type"]),
        path=row["path"],
        mime_type=row["mime_type"],
        size_bytes=row["size_bytes"],
        status=AssetStatus(row["status"]),
        content_sha256=row["content_sha256"] or "",
        captured_at=_parse_datetime(row["captured_at"]),
        captured_location=(
            GpsPoint(latitude, longitude) if latitude is not None and longitude is not None else None
        ),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


class SqliteAssetRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def add(self, asset: Asset) -> None:
        with self._database.connect() as connection, connection:
            connection.execute(
                "INSERT INTO assets (id, scope_id, file_name, media_type, path, mime_type,"
                " size_bytes, status, content_sha256, captured_at, gps_latitude, gps_longitude,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    asset.id,
                    asset.scope_id,
                    asset.file_name,
                    asset.media_type.value,
                    asset.path,
                    asset.mime_type,
                    asset.size_bytes,
                    asset.status.value,
                    asset.content_sha256,
                    asset.captured_at.isoformat() if asset.captured_at else None,
                    asset.captured_location.latitude if asset.captured_location else None,
                    asset.captured_location.longitude if asset.captured_location else None,
                    asset.created_at.isoformat(),
                    asset.updated_at.isoformat(),
                ),
            )

    def get(self, asset_id: str) -> Asset | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {_ASSET_COLUMNS} FROM assets WHERE id = ?", (asset_id,)
            ).fetchone()
        return _row_to_asset(row) if row else None

    def list_by_scope(self, scope_id: str, limit: int = 200) -> Sequence[Asset]:
        with self._database.connect() as connection:
            rows = connection.execute(
                f"SELECT {_ASSET_COLUMNS} FROM assets WHERE scope_id = ?"
                " ORDER BY COALESCE(captured_at, created_at) DESC LIMIT ?",
                (scope_id, limit),
            ).fetchall()
        return [_row_to_asset(row) for row in rows]

    def find_by_hash(self, scope_id: str, content_sha256: str) -> Asset | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {_ASSET_COLUMNS} FROM assets WHERE scope_id = ? AND content_sha256 = ? LIMIT 1",
                (scope_id, content_sha256),
            ).fetchone()
        return _row_to_asset(row) if row else None

    def update_status(self, asset_id: str, status: AssetStatus) -> None:
        with self._database.connect() as connection, connection:
            connection.execute(
                "UPDATE assets SET status = ?, updated_at = datetime('now') WHERE id = ?",
                (status.value, asset_id),
            )


class SqliteObservationRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def add(self, observation: Observation) -> None:
        with self._database.connect() as connection, connection:
            connection.execute(
                "INSERT INTO observations (id, scope_id, asset_id, source_type, captured_at,"
                " confidence, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    observation.id,
                    observation.scope_id,
                    observation.asset_id,
                    observation.source_type,
                    observation.captured_at.isoformat() if observation.captured_at else None,
                    observation.confidence,
                    observation.created_at.isoformat(),
                    observation.updated_at.isoformat(),
                ),
            )

    def get(self, observation_id: str) -> Observation | None:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT id, scope_id, asset_id, source_type, captured_at, confidence,"
                " created_at, updated_at FROM observations WHERE id = ?",
                (observation_id,),
            ).fetchone()
        if not row:
            return None
        return Observation(
            id=row["id"],
            scope_id=row["scope_id"],
            asset_id=row["asset_id"],
            source_type=row["source_type"],
            captured_at=_parse_datetime(row["captured_at"]),
            confidence=row["confidence"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
```

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv/bin/pytest apps/api/tests/integration/test_asset_repository.py -v`
Expected: 5 passed

- [ ] **Step 6: 更新 module-map**

把本任务新建的每个 `.py` 按 Task 9 的表格格式补进 `docs/architecture/module-map.md`（路径 / 职责 / 层 / 依赖谁 / 被谁依赖）。`test_module_map.py` 会强校验，漏一个文件就红；**「依赖谁 / 被谁依赖」两列还必须与该文件真实的包内 import 一致**——`check_module_map.py` 用 `ast` 解析后逐条比对，少写、多写、以及与转置不符都会红。所以新文件的「依赖谁」，以及所有反过来依赖它的行的「被谁依赖」，要一起改对。

Run: `.venv/bin/pytest apps/api/tests/unit/test_module_map.py -v`
Expected: 5 passed

- [ ] **Step 7: 提交**

```bash
git add apps/api/src/sentrix/infrastructure/persistence apps/api/tests/integration/test_asset_repository.py apps/api/tests/conftest.py docs/architecture/module-map.md
git commit -m "feat(persistence): add media migration and sqlite repositories"
```

---

## Task 16: 文件存储适配

**Files:**
- Create: `apps/api/src/sentrix/infrastructure/media/__init__.py`（空文件）
- Create: `apps/api/src/sentrix/infrastructure/media/storage.py`
- Test: `apps/api/tests/integration/test_media_storage.py`

- [ ] **Step 1: 写失败测试**

`apps/api/tests/integration/test_media_storage.py`:

```python
from sentrix.infrastructure.media.storage import FileSystemMediaStorage


def test_store_writes_under_media_dir_with_asset_prefix(tmp_path):
    storage = FileSystemMediaStorage(tmp_path / "media")

    path = storage.store("asset_1", "IMG_0001.HEIC", b"\x00\x01binary")

    assert path.parent == tmp_path / "media"
    assert path.name == "asset_1_IMG_0001.HEIC"
    assert path.read_bytes() == b"\x00\x01binary"


def test_store_sanitises_path_traversal_in_file_name(tmp_path):
    storage = FileSystemMediaStorage(tmp_path / "media")

    path = storage.store("asset_1", "../../etc/passwd", b"x")

    assert path.parent == tmp_path / "media"
    assert ".." not in path.name


def test_read_returns_original_bytes(tmp_path):
    storage = FileSystemMediaStorage(tmp_path / "media")
    storage.store("asset_1", "a.jpg", b"payload")

    assert storage.read("asset_1") == b"payload"


def test_read_is_deterministic_across_calls(tmp_path):
    """相同 asset_id 重复读取必须稳定，1.0 的 glob 匹配在多文件时会取错。"""
    storage = FileSystemMediaStorage(tmp_path / "media")
    storage.store("asset_1", "a.jpg", b"first")

    assert storage.read("asset_1") == storage.read("asset_1")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/pytest apps/api/tests/integration/test_media_storage.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 实现**

`apps/api/src/sentrix/infrastructure/media/storage.py`:

```python
"""文件系统媒体存储。

文件名规则 ``{asset_id}_{原始文件名}`` 沿用 1.0（``app.py:117 _upload_destination``），
但原始文件名先过 :func:`sanitise`——1.0 只用 ``Path.name``，遇到 ``../`` 会写到媒体目录之外。
"""

from __future__ import annotations

from pathlib import Path

from sentrix.domain.media.models import Asset


def sanitise(file_name: str) -> str:
    """只保留基础名，并剔除会破坏 ``{asset_id}_{name}`` 拼接的字符。"""
    base = Path(file_name or "unnamed").name
    cleaned = "".join(char for char in base if char not in {"/", "\\", "\x00"}).lstrip(".")
    return cleaned or "unnamed"


class FileSystemMediaStorage:
    def __init__(self, media_dir: Path) -> None:
        self._media_dir = Path(media_dir)
        self._media_dir.mkdir(parents=True, exist_ok=True)

    def store(self, asset_id: str, file_name: str, content: bytes) -> Path:
        destination = self._media_dir / f"{asset_id}_{sanitise(file_name)}"
        destination.write_bytes(content)
        return destination

    def path_for(self, asset: Asset) -> Path:
        return Path(asset.path)

    def read(self, asset_id: str) -> bytes:
        matches = sorted(self._media_dir.glob(f"{asset_id}_*"))
        if not matches:
            raise FileNotFoundError(f"未找到 asset {asset_id} 的媒体文件")
        return matches[0].read_bytes()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/pytest apps/api/tests/integration/test_media_storage.py -v`
Expected: 4 passed

- [ ] **Step 5: 更新 module-map**

把本任务新建的每个 `.py` 按 Task 9 的表格格式补进 `docs/architecture/module-map.md`（路径 / 职责 / 层 / 依赖谁 / 被谁依赖）。`test_module_map.py` 会强校验，漏一个文件就红；**「依赖谁 / 被谁依赖」两列还必须与该文件真实的包内 import 一致**——`check_module_map.py` 用 `ast` 解析后逐条比对，少写、多写、以及与转置不符都会红。所以新文件的「依赖谁」，以及所有反过来依赖它的行的「被谁依赖」，要一起改对。

Run: `.venv/bin/pytest apps/api/tests/unit/test_module_map.py -v`
Expected: 5 passed

- [ ] **Step 6: 提交**

```bash
git add apps/api/src/sentrix/infrastructure/media apps/api/tests/integration/test_media_storage.py docs/architecture/module-map.md
git commit -m "feat(media): add filesystem storage adapter"
```

---

## Task 17: 图片 IO 适配（HEIC + 预览）
> 先建 `image_io.py` 再建 `metadata.py`：`metadata.py` 要在模块顶层 import `image_io.ensure_heif_support`，反过来建会让 Task 17 连测试都收集不了。


**Files:**
- Create: `apps/api/src/sentrix/infrastructure/media/image_io.py`
- Test: `apps/api/tests/integration/test_image_io.py`

> 移植自 1.0 `backend/image_io.py`，保留 `exif_transpose`（否则竖拍照片会躺倒）。

- [ ] **Step 1: 写失败测试**

`apps/api/tests/integration/test_image_io.py`:

```python
import io

from PIL import Image

from sentrix.infrastructure.media.image_io import encode_jpeg_preview, ensure_heif_support


def test_preview_is_jpeg_and_downscaled(tmp_path):
    path = tmp_path / "big.jpg"
    Image.new("RGB", (4000, 3000), "red").save(path, format="JPEG")

    payload = encode_jpeg_preview(path, max_dimension=800, quality=80)

    assert payload[:2] == b"\xff\xd8"  # JPEG SOI
    with Image.open(io.BytesIO(payload)) as image:
        assert max(image.size) == 800


def test_preview_preserves_small_images(tmp_path):
    path = tmp_path / "small.jpg"
    Image.new("RGB", (120, 90), "blue").save(path, format="JPEG")

    payload = encode_jpeg_preview(path, max_dimension=1600, quality=85)

    with Image.open(io.BytesIO(payload)) as image:
        assert image.size == (120, 90)


def test_transparency_is_flattened_to_rgb(tmp_path):
    path = tmp_path / "alpha.png"
    Image.new("RGBA", (64, 64), (255, 0, 0, 128)).save(path, format="PNG")

    payload = encode_jpeg_preview(path, max_dimension=1600, quality=85)

    with Image.open(io.BytesIO(payload)) as image:
        assert image.mode == "RGB"


def test_heif_registration_is_idempotent():
    assert ensure_heif_support() in (True, False)
    assert ensure_heif_support() in (True, False)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/pytest apps/api/tests/integration/test_image_io.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 实现**

`apps/api/src/sentrix/infrastructure/media/image_io.py`:

```python
"""图片打开与预览编码。移植自 1.0 ``backend/image_io.py``。

保留 ``ImageOps.exif_transpose``：真实相册里大量竖拍照片靠 EXIF Orientation 标记，
不转置的话预览会躺倒。
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_HEIF_REGISTERED = False


def ensure_heif_support() -> bool:
    """注册 pillow-heif，使 PIL 能打开 HEIC/HEIF。幂等。"""
    global _HEIF_REGISTERED
    if _HEIF_REGISTERED:
        return True
    try:
        from pillow_heif import register_heif_opener

        register_heif_opener()
        _HEIF_REGISTERED = True
        return True
    except Exception:
        log.warning("pillow_heif_unavailable heic 将无法导入")
        return False


def encode_jpeg_preview(path: Path, *, max_dimension: int = 1600, quality: int = 85) -> bytes:
    from PIL import Image, ImageOps

    ensure_heif_support()
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        if max(image.size) > max_dimension:
            image.thumbnail((max_dimension, max_dimension))
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=quality, optimize=True)
        return output.getvalue()


class PillowPreviewEncoder:
    """把模块级函数适配成 domain 的 :class:`PreviewEncoder` 端口。"""

    def encode_jpeg(self, path: Path, *, max_dimension: int, quality: int) -> bytes:
        return encode_jpeg_preview(path, max_dimension=max_dimension, quality=quality)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/pytest apps/api/tests/integration/test_image_io.py -v`
Expected: 4 passed

- [ ] **Step 5: 更新 module-map**

把本任务新建的每个 `.py` 按 Task 9 的表格格式补进 `docs/architecture/module-map.md`（路径 / 职责 / 层 / 依赖谁 / 被谁依赖）。`test_module_map.py` 会强校验，漏一个文件就红；**「依赖谁 / 被谁依赖」两列还必须与该文件真实的包内 import 一致**——`check_module_map.py` 用 `ast` 解析后逐条比对，少写、多写、以及与转置不符都会红。所以新文件的「依赖谁」，以及所有反过来依赖它的行的「被谁依赖」，要一起改对。

Run: `.venv/bin/pytest apps/api/tests/unit/test_module_map.py -v`
Expected: 5 passed

- [ ] **Step 6: 提交**

```bash
git add apps/api/src/sentrix/infrastructure/media/image_io.py apps/api/tests/integration/test_image_io.py docs/architecture/module-map.md
git commit -m "feat(media): port image io with heic support and exif transpose"
```


---

## Task 18: EXIF / GPS 元数据适配

**Files:**
- Create: `apps/api/src/sentrix/infrastructure/media/metadata.py`
- Test: `apps/api/tests/integration/test_metadata_reader.py`

> 移植自 1.0 `backend/pipeline.py:251 _extract_exif`。转换逻辑逐行保留，因为它处理了
> 真实相册里的各种脏数据（度分秒有理数、S/W 负号、无 OffsetTime）。

- [ ] **Step 1: 写失败测试**

`apps/api/tests/integration/test_metadata_reader.py`:

```python
from datetime import datetime

import pytest
from PIL import Image
from PIL.TiffImagePlugin import IFDRational

from sentrix.infrastructure.media.metadata import PillowMetadataReader


@pytest.fixture
def jpeg_without_exif(tmp_path):
    path = tmp_path / "plain.jpg"
    Image.new("RGB", (16, 16), "white").save(path, format="JPEG")
    return path


def test_missing_exif_returns_empty(tmp_path, jpeg_without_exif):
    captured_at, gps, _ = PillowMetadataReader().read(jpeg_without_exif)

    assert captured_at is None
    assert gps is None


def test_gps_degrees_minutes_seconds_are_converted(tmp_path):
    """1.0 的度分秒转换：sum(value / 60**index)，S/W 取负。"""
    path = tmp_path / "geo.jpg"
    image = Image.new("RGB", (16, 16), "white")
    exif = image.getexif()
    exif[34853] = {  # GPSInfo；度分秒在 EXIF 里是有理数，必须用 IFDRational
        1: "N",
        2: (IFDRational(18, 1), IFDRational(47, 1), IFDRational(1788, 100)),
        3: "E",
        4: (IFDRational(98, 1), IFDRational(59, 1), IFDRational(700, 100)),
    }
    image.save(path, format="JPEG", exif=exif)

    captured_at, gps, _ = PillowMetadataReader().read(path)

    assert gps is not None
    assert gps.latitude == pytest.approx(18.7883, abs=1e-4)
    assert gps.longitude == pytest.approx(98.9853, abs=1e-4)


def test_southern_and_western_hemispheres_are_negated(tmp_path):
    path = tmp_path / "south.jpg"
    image = Image.new("RGB", (16, 16), "white")
    exif = image.getexif()
    exif[34853] = {
        1: "S",
        2: (IFDRational(33, 1), IFDRational(52, 1), IFDRational(0, 1)),
        3: "W",
        4: (IFDRational(151, 1), IFDRational(12, 1), IFDRational(0, 1)),
    }
    image.save(path, format="JPEG", exif=exif)

    _, gps, _ = PillowMetadataReader().read(path)

    assert gps.latitude == pytest.approx(-33.8667, abs=1e-3)
    assert gps.longitude == pytest.approx(-151.2, abs=1e-3)


def test_datetime_original_is_normalised_to_iso(tmp_path):
    path = tmp_path / "dated.jpg"
    image = Image.new("RGB", (16, 16), "white")
    exif = image.getexif()
    exif[36867] = "2018:04:01 21:05:47"  # DateTimeOriginal
    image.save(path, format="JPEG", exif=exif)

    captured_at, _, _ = PillowMetadataReader().read(path)

    assert captured_at == datetime(2018, 4, 1, 21, 5, 47)


def test_corrupt_file_degrades_to_empty(tmp_path):
    path = tmp_path / "broken.jpg"
    path.write_bytes(b"not an image at all")

    captured_at, gps, _ = PillowMetadataReader().read(path)

    assert captured_at is None
    assert gps is None


def test_device_prefers_model_over_make(tmp_path):
    """设备型号来自 EXIF；Make(271)/Model(272) 都在时取 Model（device = model or make）。"""
    path = tmp_path / "device.jpg"
    image = Image.new("RGB", (16, 16), "white")
    exif = image.getexif()
    exif[271] = "Apple"  # Make
    exif[272] = "iPhone 12"  # Model
    image.save(path, format="JPEG", exif=exif)

    _, _, device = PillowMetadataReader().read(path)

    assert device == "iPhone 12"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/pytest apps/api/tests/integration/test_metadata_reader.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 实现**

`apps/api/src/sentrix/infrastructure/media/metadata.py`:

```python
"""EXIF / GPS 抽取。移植自 1.0 ``backend/pipeline.py:251``。

1.0 静默吞掉一切异常并返回 ``{}``。2.0 保留"坏文件不阻断导入"的行为，
但把失败记进日志，避免 1.0 那种"字段悄悄没了没人知道"的问题。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from sentrix.domain.media.models import GpsPoint
from sentrix.infrastructure.media.image_io import ensure_heif_support

log = logging.getLogger(__name__)

GPS_INFO_TAG = 34853
DATETIME_ORIGINAL_TAG = 36867
DATETIME_TAG = 306
OFFSET_TIME_ORIGINAL_TAG = 36881
OFFSET_TIME_TAG = 36880
MAKE_TAG = 271
MODEL_TAG = 272

GPS_LATITUDE_REF, GPS_LATITUDE = 1, 2
GPS_LONGITUDE_REF, GPS_LONGITUDE = 3, 4


def _rational(value: Any) -> float:
    try:
        return float(value[0]) / float(value[1])
    except (TypeError, ValueError, ZeroDivisionError, IndexError):
        return float(value)


def _degrees_minutes_seconds(values: Sequence[object], *, negative_ref: str, ref_value: str) -> float:
    # 60**index 在 typeshed 里是 Any（int.__pow__ 要覆盖负指数返回 float 的情形），
    # 所以给 degrees 落一个显式标注，把实数类型钉住；表达式与运行结果都不变。
    degrees: float = sum(_rational(item) / (60**index) for index, item in enumerate(values))
    if str(ref_value).upper() == negative_ref:
        degrees *= -1
    return degrees


# EXIF 的时间格式不统一：有无 OffsetTime、有无小数秒都要能解析。
_DATETIME_FORMATS = (
    "%Y:%m:%d %H:%M:%S.%f%z",
    "%Y:%m:%d %H:%M:%S%z",
    "%Y:%m:%d %H:%M:%S",
)


def _parse_exif_datetime(tags: dict[int, object]) -> datetime | None:
    raw = tags.get(DATETIME_ORIGINAL_TAG) or tags.get(DATETIME_TAG)
    if not raw:
        return None
    text = str(raw).strip()
    offset = str(tags.get(OFFSET_TIME_ORIGINAL_TAG) or tags.get(OFFSET_TIME_TAG) or "").strip()
    # EXIF 的 offset 写作 "+0800"，strptime 的 %z 接受 "+08:00" 也接受 "+0800"，直接拼接即可
    if offset.startswith(("+", "-")) and len(offset) >= 5:
        text = f"{text}{offset}"
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    log.warning("exif_datetime_unparseable raw=%r", raw)
    return None


class PillowMetadataReader:
    def read(self, path: Path) -> tuple[datetime | None, GpsPoint | None, str | None]:
        """返回 (拍摄时间, GPS, 设备型号)；任何一步失败都降级为 None。"""
        try:
            from PIL import ExifTags, Image

            ensure_heif_support()
            with Image.open(path) as image:
                exif = image.getexif()
                tags = {key: value for key, value in exif.items()}
                try:
                    gps_tags = exif.get_ifd(ExifTags.IFD.GPSInfo) or {}
                except Exception:
                    gps_tags = tags.get(GPS_INFO_TAG) or {}
        except Exception:
            log.warning("exif_read_failed path=%s", path, exc_info=True)
            return None, None, None

        captured_at = _parse_exif_datetime(tags)

        point: GpsPoint | None = None
        latitude_values = gps_tags.get(GPS_LATITUDE)
        longitude_values = gps_tags.get(GPS_LONGITUDE)
        if latitude_values and longitude_values:
            try:
                point = GpsPoint(
                    latitude=_degrees_minutes_seconds(
                        latitude_values,
                        negative_ref="S",
                        ref_value=gps_tags.get(GPS_LATITUDE_REF, "N"),
                    ),
                    longitude=_degrees_minutes_seconds(
                        longitude_values,
                        negative_ref="W",
                        ref_value=gps_tags.get(GPS_LONGITUDE_REF, "E"),
                    ),
                )
            except (ValueError, TypeError):
                log.warning("exif_gps_invalid path=%s", path, exc_info=True)
                point = None

        make = str(tags.get(MAKE_TAG) or "").strip()
        model = str(tags.get(MODEL_TAG) or "").strip()
        device = model or make or None

        return captured_at, point, device
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/pytest apps/api/tests/integration/test_metadata_reader.py -v`
Expected: 6 passed

- [ ] **Step 5: 更新 module-map**

把本任务新建的每个 `.py` 按 Task 9 的表格格式补进 `docs/architecture/module-map.md`（路径 / 职责 / 层 / 依赖谁 / 被谁依赖）。`test_module_map.py` 会强校验，漏一个文件就红；**「依赖谁 / 被谁依赖」两列还必须与该文件真实的包内 import 一致**——`check_module_map.py` 用 `ast` 解析后逐条比对，少写、多写、以及与转置不符都会红。所以新文件的「依赖谁」，以及所有反过来依赖它的行的「被谁依赖」，要一起改对。

Run: `.venv/bin/pytest apps/api/tests/unit/test_module_map.py -v`
Expected: 5 passed

- [ ] **Step 6: 提交**

```bash
git add apps/api/src/sentrix/infrastructure/media/metadata.py apps/api/tests/integration/test_metadata_reader.py docs/architecture/module-map.md
git commit -m "feat(media): port exif/gps extraction with logging on failure"
```


---

## Task 19: 导入用例

**Files:**
- Create: `apps/api/src/sentrix/application/__init__.py`（空文件）
- Create: `apps/api/src/sentrix/application/media/__init__.py`（空文件）
- Create: `apps/api/src/sentrix/application/media/import_assets.py`
- Test: `apps/api/tests/unit/test_import_assets.py`

- [ ] **Step 1: 写失败测试**

`apps/api/tests/unit/test_import_assets.py`:

```python
import dataclasses
from datetime import UTC, datetime

from sentrix.application.media.import_assets import ImportAssets, ImportRequest
from sentrix.domain.media.models import Asset, AssetStatus, GpsPoint, MediaType


class FakeAssets:
    def __init__(self) -> None:
        self.rows: dict[str, Asset] = {}

    def add(self, asset):
        self.rows[asset.id] = asset

    def get(self, asset_id):
        return self.rows.get(asset_id)

    def list_by_scope(self, scope_id, limit):
        return [a for a in self.rows.values() if a.scope_id == scope_id][:limit]

    def find_by_hash(self, scope_id, content_sha256):
        return next(
            (a for a in self.rows.values() if a.scope_id == scope_id and a.content_sha256 == content_sha256),
            None,
        )

    def update_status(self, asset_id, status):
        # Asset 是 frozen+slots dataclass，没有 __dict__，必须用 dataclasses.replace
        self.rows[asset_id] = dataclasses.replace(self.rows[asset_id], status=status)


class FakeObservations:
    def __init__(self) -> None:
        self.rows = {}

    def add(self, observation):
        self.rows[observation.id] = observation

    def get(self, observation_id):
        return self.rows.get(observation_id)


class FakeStorage:
    def __init__(self) -> None:
        self.stored: dict[str, bytes] = {}

    def store(self, asset_id, file_name, content):
        from pathlib import Path

        self.stored[asset_id] = content
        return Path(f"/media/{asset_id}_{file_name}")

    def read(self, asset_id):
        return self.stored[asset_id]

    def path_for(self, asset):
        from pathlib import Path

        return Path(asset.path)


class FakeMetadata:
    def __init__(self, captured_at=None, gps=None, device=None) -> None:
        self._captured_at, self._gps, self._device = captured_at, gps, device

    def read(self, path):
        return self._captured_at, self._gps, self._device


def build(metadata=None):
    assets, observations, storage = FakeAssets(), FakeObservations(), FakeStorage()
    use_case = ImportAssets(
        assets=assets,
        observations=observations,
        storage=storage,
        metadata=metadata or FakeMetadata(),
    )
    return use_case, assets, observations, storage


def test_import_creates_asset_and_observation():
    use_case, assets, observations, storage = build()
    request = ImportRequest(
        scope_id="album1",
        file_name="IMG_0001.HEIC",
        content_type="image/heic",
        content=b"raw bytes",
    )

    result = use_case.execute(request)

    assert result.status is AssetStatus.PROCESSED
    assert result.media_type is MediaType.IMAGE
    asset = assets.get(result.asset_id)
    assert asset is not None
    assert asset.size_bytes == len(b"raw bytes")
    assert storage.stored[result.asset_id] == b"raw bytes"
    assert observations.get(result.observation_id) is not None


def test_identical_content_in_same_scope_is_deduplicated():
    use_case, assets, _, storage = build()
    request = ImportRequest(
        scope_id="album1",
        file_name="a.jpg",
        content_type="image/jpeg",
        content=b"same",
    )

    first = use_case.execute(request)
    second = use_case.execute(request)

    assert second.deduplicated is True
    assert second.asset_id == first.asset_id
    assert len(assets.rows) == 1
    assert len(storage.stored) == 1


def test_same_content_in_different_scopes_is_kept_separately():
    """1.0 的去重按 scope 隔离（fix(api): scope dedup in /api/ingest）。"""
    use_case, assets, _, _ = build()
    use_case.execute(
        ImportRequest(scope_id="album1", file_name="a.jpg", content_type="image/jpeg", content=b"same")
    )
    use_case.execute(
        ImportRequest(scope_id="album2", file_name="a.jpg", content_type="image/jpeg", content=b"same")
    )

    assert len(assets.rows) == 2


def test_metadata_is_carried_onto_the_asset():
    gps = GpsPoint(18.7883, 98.9853)
    captured = datetime(2018, 4, 1, 21, 5, 47, tzinfo=UTC)
    use_case, assets, _, _ = build(FakeMetadata(captured, gps, "iPhone X"))

    result = use_case.execute(
        ImportRequest(scope_id="album1", file_name="a.jpg", content_type="image/jpeg", content=b"x")
    )

    asset = assets.get(result.asset_id)
    assert asset.captured_at == captured
    assert asset.captured_location == gps
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/pytest apps/api/tests/unit/test_import_assets.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 实现**

`apps/api/src/sentrix/application/media/import_assets.py`:

```python
"""导入用例：把一次上传变成 Asset + Observation。

业务规则（都来自 1.0 的既有行为）：
- 内容 SHA-256 去重，**按 scope 隔离**，不跨相册去重
- 去重命中时不重复落盘
- 拍摄时间与 GPS 来自 EXIF，抽不到就留空，**不阻断导入**
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sentrix.domain.media.models import Asset, AssetStatus, MediaType, Observation
from sentrix.domain.media.ports import (
    AssetRepository,
    MediaStorage,
    MetadataReader,
    ObservationRepository,
)


@dataclass(frozen=True, slots=True)
class ImportRequest:
    scope_id: str
    file_name: str
    content_type: str | None
    content: bytes


@dataclass(frozen=True, slots=True)
class ImportResult:
    asset_id: str
    observation_id: str | None
    media_type: MediaType
    status: AssetStatus
    deduplicated: bool


def _new_id(prefix: str) -> str:
    import uuid

    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class ImportAssets:
    def __init__(
        self,
        *,
        assets: AssetRepository,
        observations: ObservationRepository,
        storage: MediaStorage,
        metadata: MetadataReader,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._assets = assets
        self._observations = observations
        self._storage = storage
        self._metadata = metadata
        self._clock = clock

    def execute(self, request: ImportRequest) -> ImportResult:
        content_sha256 = hashlib.sha256(request.content).hexdigest()
        media_type = MediaType.from_upload(request.content_type, request.file_name)

        existing = self._assets.find_by_hash(request.scope_id, content_sha256)
        if existing is not None:
            return ImportResult(
                asset_id=existing.id,
                observation_id=None,
                media_type=existing.media_type,
                status=existing.status,
                deduplicated=True,
            )

        asset_id = _new_id("asset")
        stored_path = self._storage.store(asset_id, request.file_name, request.content)

        captured_at, gps, device = (None, None, None)
        if media_type is MediaType.IMAGE:
            captured_at, gps, device = self._metadata.read(stored_path)

        now = self._clock()
        asset = Asset(
            id=asset_id,
            scope_id=request.scope_id,
            file_name=request.file_name,
            media_type=media_type,
            path=str(stored_path),
            mime_type=request.content_type,
            size_bytes=len(request.content),
            status=AssetStatus.PROCESSED,
            content_sha256=content_sha256,
            captured_at=captured_at or now,
            captured_location=gps,
            created_at=now,
            updated_at=now,
        )
        self._assets.add(asset)

        observation = Observation(
            id=_new_id("obs"),
            scope_id=request.scope_id,
            asset_id=asset_id,
            source_type="import",
            captured_at=asset.captured_at,
            confidence=0.0,
            created_at=now,
            updated_at=now,
        )
        self._observations.add(observation)

        return ImportResult(
            asset_id=asset_id,
            observation_id=observation.id,
            media_type=media_type,
            status=asset.status,
            deduplicated=False,
        )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/pytest apps/api/tests/unit/test_import_assets.py -v`
Expected: 4 passed

- [ ] **Step 5: 更新 module-map**

把本任务新建的每个 `.py` 按 Task 9 的表格格式补进 `docs/architecture/module-map.md`（路径 / 职责 / 层 / 依赖谁 / 被谁依赖）。`test_module_map.py` 会强校验，漏一个文件就红；**「依赖谁 / 被谁依赖」两列还必须与该文件真实的包内 import 一致**——`check_module_map.py` 用 `ast` 解析后逐条比对，少写、多写、以及与转置不符都会红。所以新文件的「依赖谁」，以及所有反过来依赖它的行的「被谁依赖」，要一起改对。

Run: `.venv/bin/pytest apps/api/tests/unit/test_module_map.py -v`
Expected: 5 passed

- [ ] **Step 6: 提交**

```bash
git add apps/api/src/sentrix/application apps/api/tests/unit/test_import_assets.py docs/architecture/module-map.md
git commit -m "feat(application): add import use case with scoped deduplication"
```

---

## Task 20: 导入 API 与媒体查询 API

**Files:**
- Create: `apps/api/src/sentrix/api/routes/media.py`
- Create: `apps/api/src/sentrix/api/schemas/media.py`
- Create: `apps/api/src/sentrix/api/schemas/__init__.py`（空文件）
- Create: `apps/api/src/sentrix/api/deps.py`
- Create: `apps/api/src/sentrix/api/errors.py`
- Modify: `apps/api/src/sentrix/bootstrap/container.py`、`apps/api/src/sentrix/main.py`
- Test: `apps/api/tests/contract/test_media_routes.py`

- [ ] **Step 1: 写失败测试**

`apps/api/tests/contract/test_media_routes.py`:

```python
import io

from fastapi.testclient import TestClient
from PIL import Image


def jpeg_bytes(size=(64, 48), color="green") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="JPEG")
    return buffer.getvalue()


def test_import_returns_201_and_asset_id(client: TestClient):
    response = client.post(
        "/api/v1/media/import",
        files={"file": ("IMG_0001.jpg", jpeg_bytes(), "image/jpeg")},
        data={"scope_id": "album1"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["asset_id"].startswith("asset_")
    assert body["media_type"] == "image"
    assert body["deduplicated"] is False


def test_reimport_same_bytes_is_deduplicated(client: TestClient):
    payload = jpeg_bytes()
    for _ in range(2):
        response = client.post(
            "/api/v1/media/import",
            files={"file": ("a.jpg", payload, "image/jpeg")},
            data={"scope_id": "album1"},
        )

    assert response.status_code == 201
    assert response.json()["deduplicated"] is True


def test_list_assets_for_scope(client: TestClient):
    client.post(
        "/api/v1/media/import",
        files={"file": ("a.jpg", jpeg_bytes(), "image/jpeg")},
        data={"scope_id": "album1"},
    )
    client.post(
        "/api/v1/media/import",
        files={"file": ("b.jpg", jpeg_bytes(color="red"), "image/jpeg")},
        data={"scope_id": "album2"},
    )

    response = client.get("/api/v1/assets", params={"scope_id": "album1"})

    assert response.status_code == 200
    assert len(response.json()["items"]) == 1


def test_asset_file_returns_original_bytes(client: TestClient):
    payload = jpeg_bytes()
    asset_id = client.post(
        "/api/v1/media/import", files={"file": ("a.jpg", payload, "image/jpeg")}, data={"scope_id": "album1"}
    ).json()["asset_id"]

    response = client.get(f"/api/v1/assets/{asset_id}/file")

    assert response.status_code == 200
    assert response.content == payload


def test_asset_preview_is_jpeg(client: TestClient):
    asset_id = client.post(
        "/api/v1/media/import",
        files={"file": ("a.jpg", jpeg_bytes(), "image/jpeg")},
        data={"scope_id": "album1"},
    ).json()["asset_id"]

    response = client.get(f"/api/v1/assets/{asset_id}/preview")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content[:2] == b"\xff\xd8"


def test_missing_asset_is_404(client: TestClient):
    assert client.get("/api/v1/assets/asset_nope").status_code == 404
    assert client.get("/api/v1/assets/asset_nope/file").status_code == 404
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/pytest apps/api/tests/contract/test_media_routes.py -v`
Expected: FAIL — 404（路由还不存在）

- [ ] **Step 3: 实现错误映射与依赖**

`apps/api/src/sentrix/api/errors.py`:

```python
"""领域错误 → HTTP 状态。所有 v1 端点共用同一结构。"""

from __future__ import annotations

from typing import cast

from fastapi import Request
from fastapi.responses import JSONResponse


class DomainError(Exception):
    code = "domain_error"
    status_code = 400

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.code)
        self.message = message or self.code


class NotFoundError(DomainError):
    code = "not_found"
    status_code = 404


class InvalidRequestError(DomainError):
    code = "invalid_request"
    status_code = 400


async def domain_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    # Starlette 的 add_exception_handler 要求第二参数是 Exception（函数参数逆变），
    # 而这里注册的就是 DomainError；cast 零开销，运行时要拿到的仍然是 DomainError。
    error = cast(DomainError, exc)
    return JSONResponse(
        status_code=error.status_code,
        content={"code": error.code, "message": error.message, "detail": {}},
    )
```

`apps/api/src/sentrix/api/deps.py`:

```python
"""从 app.state 取依赖。路由只从这里拿东西，不自己构造。"""

from __future__ import annotations

from typing import cast

from fastapi import Request

from sentrix.bootstrap.container import Container


def get_container(request: Request) -> Container:
    # app.state 是 Starlette 的动态属性，mypy 只看得见 Any；装进去的就是 Container。
    return cast(Container, request.app.state.container)
```

- [ ] **Step 4: 实现 schema 与路由**

`apps/api/src/sentrix/api/schemas/media.py`:

```python
from __future__ import annotations

from pydantic import BaseModel


class ImportResponse(BaseModel):
    asset_id: str
    observation_id: str | None
    media_type: str
    status: str
    deduplicated: bool


class AssetSummary(BaseModel):
    id: str
    file_name: str
    media_type: str
    mime_type: str | None
    size_bytes: int
    status: str
    captured_at: str | None
    gps: dict[str, float] | None


class AssetListResponse(BaseModel):
    items: list[AssetSummary]
```

`apps/api/src/sentrix/api/routes/media.py`:

```python
"""媒体导入与交付。只做 HTTP 映射，业务在 application 层。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response

from sentrix.api.deps import get_container
from sentrix.api.schemas.media import AssetListResponse, AssetSummary, ImportResponse
from sentrix.application.media.import_assets import ImportAssets, ImportRequest
from sentrix.bootstrap.container import Container
from sentrix.domain.media.models import Asset

router = APIRouter(tags=["media"])

PREVIEW_MAX_DIMENSION = 1600
PREVIEW_QUALITY = 85


def _to_summary(asset: Asset) -> AssetSummary:
    return AssetSummary(
        id=asset.id,
        file_name=asset.file_name,
        media_type=asset.media_type.value,
        mime_type=asset.mime_type,
        size_bytes=asset.size_bytes,
        status=asset.status.value,
        captured_at=asset.captured_at.isoformat() if asset.captured_at else None,
        gps=(
            {"latitude": asset.captured_location.latitude, "longitude": asset.captured_location.longitude}
            if asset.captured_location
            else None
        ),
    )


@router.post("/media/import", status_code=201)
async def import_media(
    file: UploadFile = File(...),
    scope_id: str = Form("home-default"),
    container: Container = Depends(get_container),
) -> ImportResponse:
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="empty file")

    use_case = ImportAssets(
        assets=container.asset_repository,
        observations=container.observation_repository,
        storage=container.media_storage,
        metadata=container.metadata_reader,
    )
    result = use_case.execute(
        ImportRequest(
            scope_id=scope_id,
            file_name=file.filename or "unnamed",
            content_type=file.content_type,
            content=content,
        )
    )
    return ImportResponse(
        asset_id=result.asset_id,
        observation_id=result.observation_id,
        media_type=result.media_type.value,
        status=result.status.value,
        deduplicated=result.deduplicated,
    )


@router.get("/assets")
def list_assets(
    scope_id: str = "home-default",
    limit: int = 200,
    container: Container = Depends(get_container),
) -> AssetListResponse:
    assets = container.asset_repository.list_by_scope(scope_id, min(limit, 1000))
    return AssetListResponse(items=[_to_summary(asset) for asset in assets])


@router.get("/assets/{asset_id}")
def get_asset(asset_id: str, container: Container = Depends(get_container)) -> AssetSummary:
    asset = container.asset_repository.get(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="asset not found")
    return _to_summary(asset)


@router.get("/assets/{asset_id}/file")
def get_asset_file(asset_id: str, container: Container = Depends(get_container)) -> FileResponse:
    asset = container.asset_repository.get(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="asset not found")
    path = Path(asset.path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="media file missing on disk")
    return FileResponse(path, media_type=asset.mime_type or "application/octet-stream")


@router.get("/assets/{asset_id}/preview")
def get_asset_preview(asset_id: str, container: Container = Depends(get_container)) -> Response:
    asset = container.asset_repository.get(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="asset not found")
    payload = container.preview_encoder.encode_jpeg(
        Path(asset.path), max_dimension=PREVIEW_MAX_DIMENSION, quality=PREVIEW_QUALITY
    )
    return Response(content=payload, media_type="image/jpeg")
```

- [ ] **Step 5: 扩展容器**

`apps/api/src/sentrix/bootstrap/container.py` 替换为：

```python
"""The single wiring point.

Nothing else in the codebase constructs an adapter.  Domain and application code
receive ports; only this module knows which concrete class implements them.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from sentrix.config import Settings
from sentrix.domain.media.ports import PreviewEncoder
from sentrix.infrastructure.media.image_io import PillowPreviewEncoder
from sentrix.infrastructure.media.metadata import PillowMetadataReader
from sentrix.infrastructure.media.storage import FileSystemMediaStorage
from sentrix.infrastructure.persistence.database import Database
from sentrix.infrastructure.persistence.repositories.assets import (
    SqliteAssetRepository,
    SqliteObservationRepository,
)
from sentrix.infrastructure.persistence.repositories.spaces import SqliteSpaceRepository

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "infrastructure" / "persistence" / "migrations"


@dataclass(frozen=True)
class Container:
    settings: Settings
    database: Database
    spaces: SqliteSpaceRepository
    asset_repository: SqliteAssetRepository
    observation_repository: SqliteObservationRepository
    media_storage: FileSystemMediaStorage
    metadata_reader: PillowMetadataReader
    preview_encoder: PreviewEncoder

    @classmethod
    def build(cls, settings: Settings) -> Container:
        database = Database(settings.database_path, MIGRATIONS_DIR)
        return cls(
            settings=settings,
            database=database,
            spaces=SqliteSpaceRepository(database),
            asset_repository=SqliteAssetRepository(database),
            observation_repository=SqliteObservationRepository(database),
            media_storage=FileSystemMediaStorage(settings.media_dir),
            metadata_reader=PillowMetadataReader(),
            preview_encoder=PillowPreviewEncoder(),
        )

    def database_healthy(self) -> bool:
        try:
            with self.database.connect() as connection:
                connection.execute("SELECT 1 FROM schema_migrations LIMIT 1").fetchone()
        except sqlite3.Error:
            return False
        return True
```

- [ ] **Step 6: 注册路由与错误处理**

`apps/api/src/sentrix/main.py` 替换为：

```python
"""FastAPI assembly. Route modules contain transport mapping only."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI

from sentrix import __version__
from sentrix.api.errors import DomainError, domain_error_handler
from sentrix.api.routes import health, media
from sentrix.bootstrap.container import Container
from sentrix.config import Settings, load_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    settings.ensure_directories()

    container = Container.build(settings)
    container.database.migrate()

    app = FastAPI(title="Sentrix Home API", version=__version__, docs_url=None, redoc_url=None)
    app.state.container = container
    app.add_exception_handler(DomainError, domain_error_handler)

    v1 = APIRouter(prefix="/api/v1")
    v1.include_router(health.router)
    v1.include_router(media.router)
    app.include_router(v1)
    return app


# 这里刻意没有模块级 ``app = create_app()``：它会让任何 ``import sentrix.main``
# （pytest 收集、脚本、类型检查）都带上副作用——在进程 CWD 下建出 data/、跑一次迁移。
# 进程入口走工厂：``uvicorn --factory sentrix.main:create_app``（见 Makefile 的 dev）。
# 理由与 Task 5 的定稿一致；本块曾残留该行（1076a7f 只改了 Task 5，漏改这里）。
```

> **注意：本块刻意没有模块级 `app = create_app()`**——import 不该有副作用（理由见 Task 5 定稿：任何 `import sentrix.main` 都会在进程 CWD 下建出 `data/` 并跑一次迁移）。原稿残留在本块的该行是 `1076a7f` 漏改所致，执行时已按 Task 5 对齐删除。**不要补回去。**

- [ ] **Step 7: 跑测试确认通过**

Run: `.venv/bin/pytest apps/api/tests/contract -v`
Expected: 9 passed

- [ ] **Step 8: 验证分层未被破坏**

Run: `make arch`
Expected: 全部 PASS。若有 `api-transport-only` 失败，说明路由里出现了 SQL 或直接构造适配器

- [ ] **Step 9: 更新 module-map**

把本任务新建的每个 `.py` 按 Task 9 的表格格式补进 `docs/architecture/module-map.md`（路径 / 职责 / 层 / 依赖谁 / 被谁依赖）。`test_module_map.py` 会强校验，漏一个文件就红；**「依赖谁 / 被谁依赖」两列还必须与该文件真实的包内 import 一致**——`check_module_map.py` 用 `ast` 解析后逐条比对，少写、多写、以及与转置不符都会红。所以新文件的「依赖谁」，以及所有反过来依赖它的行的「被谁依赖」，要一起改对。

Run: `.venv/bin/pytest apps/api/tests/unit/test_module_map.py -v`
Expected: 5 passed

- [ ] **Step 10: 提交**

```bash
git add apps/api/src/sentrix/api apps/api/src/sentrix/bootstrap apps/api/src/sentrix/main.py apps/api/tests/contract/test_media_routes.py docs/architecture/module-map.md
git commit -m "feat(api): add media import, listing and delivery endpoints"
```

---

## Task 21: OpenAPI 契约与生成 client

**Files:**
- Create: `packages/contracts/openapi/sentrix-v1.yaml`
- Create: `scripts/export_openapi.py`

- [ ] **Step 1: 写导出脚本**

`scripts/export_openapi.py`:

```python
#!/usr/bin/env python3
"""把 FastAPI 的 openapi.json 落到 packages/contracts/openapi/sentrix-v1.yaml。

契约是唯一真相：这个脚本产出的文件提交进仓库，前端 client 从它生成。
CI 会用 --check 比对，防止实现偷偷偏离契约。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
TARGET = REPO_ROOT / "packages" / "contracts" / "openapi" / "sentrix-v1.yaml"


def current_spec() -> dict[str, Any]:
    sys.path.insert(0, str(REPO_ROOT / "apps" / "api" / "src"))
    from sentrix.main import create_app

    return create_app().openapi()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="只比对，不写文件")
    args = parser.parse_args()

    spec = current_spec()
    rendered = yaml.safe_dump(spec, allow_unicode=True, sort_keys=False)

    if args.check:
        existing = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if existing != rendered:
            print("openapi 契约与实现不一致，请运行 scripts/export_openapi.py 更新")
            return 1
        print("openapi 契约一致")
        return 0

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(rendered, encoding="utf-8")
    print(f"已写入 {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: 加依赖并生成**

```bash
.venv/bin/pip install -q pyyaml
echo "pyyaml>=6,<7" >> /dev/null   # 手工加入 apps/api/pyproject.toml 的 dev 依赖
.venv/bin/python scripts/export_openapi.py
```

把 `"pyyaml>=6,<7"` 加到 `apps/api/pyproject.toml` 的 `dev` 列表，然后：

```bash
.venv/bin/pip install -q -e "apps/api[dev]"
.venv/bin/python scripts/export_openapi.py
head -20 packages/contracts/openapi/sentrix-v1.yaml
```

Expected: 文件生成，含 `/api/v1/media/import` 等路径

- [ ] **Step 3: 把 check 加进 Makefile 的 check-docs**

```makefile
check-docs: ## 文档防腐校验
	$(PY) scripts/check_capability_map.py
	$(PY) scripts/check_module_map.py
	$(PY) scripts/export_openapi.py --check
```

- [ ] **Step 4: 验证**

Run: `make check-docs`
Expected: `openapi 契约一致`

- [ ] **Step 5: 提交**

```bash
git add scripts/export_openapi.py packages/contracts/openapi/sentrix-v1.yaml Makefile apps/api/pyproject.toml
git commit -m "feat(contracts): export openapi v1 spec with drift check"
```

---

## Task 22: Web 脚手架

**Files:**
- Create: `apps/web/package.json`、`apps/web/tsconfig.json`、`apps/web/index.html`（由 `npm create vite` 生成）
- Create: `apps/web/vite.config.ts`（固定端口与 API 代理，见 Step 2）
- Create: `apps/web/src/App.tsx`（**最小应用壳**；带样式的完整版在 Task 23 替换）
- 其余入口（`apps/web/src/main.tsx`、`index.css` 等）沿用脚手架生成物，Task 23 再替换 `main.tsx`

- [ ] **Step 1: 建脚手架**

```bash
cd /home/asus/Github/sentrix-home-web2.0/apps
npm create vite@latest web -- --template react-ts
cd web && npm install
```

> 脚手架会带出 `src/App.css`、`src/index.css` 与 `src/assets/*`。Task 23 把 `App.tsx` /
> `main.tsx` 换成自带样式表的版本后，这三者不再有任何 import 边（`vite build` 会忽略它们，
> 但它们会进 git），**由 Task 23 一并删除**。

- [ ] **Step 2: 固定端口与 API 代理**

`apps/web/vite.config.ts`:

```ts
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 4274,
    proxy: {
      "/api": { target: "http://127.0.0.1:9598", changeOrigin: true },
    },
  },
});
```

- [ ] **Step 3: 写最小应用壳**

`apps/web/src/App.tsx`（**自包含**：不引用 Task 23 才建的 `AssetGrid` / `client`，否则此时 Vite 会报 unresolved import）：

```tsx
export default function App() {
  return (
    <main className="app">
      <h1>Sentrix Home 2.0</h1>
      <p>脚手架就绪。</p>
    </main>
  );
}
```

入口 `apps/web/src/main.tsx` **本任务不动**：沿用 Vite 脚手架生成的默认版本（它 import 的 `./index.css` 与 `./App` 都已存在）。带样式的入口在 Task 23 一并替换。

- [ ] **Step 4: 验证能起（且真的渲染）**

```bash
cd apps/web && npm run dev -- --port 4274 > /tmp/vite.log 2>&1 &
sleep 5
curl -sf -o /dev/null -w "index: %{http_code}\n" http://127.0.0.1:4274/
curl -s http://127.0.0.1:4274/src/App.tsx | grep -q "Sentrix Home 2.0" && echo "App.tsx 已编译且含标题"
grep -q "Failed to resolve import" /tmp/vite.log && echo "存在未解析 import" || echo "无未解析 import"
kill %1
```

Expected: `index: 200`、`App.tsx 已编译且含标题`、`无未解析 import`

> **为什么不去 grep `/` 返回的 HTML**：这是 SPA——Vite 对 `/` 返回的 index.html 里只有一个空的 `<div id="root">`，标题是 React 运行时渲染进去的，静态 HTML 里没有。真正能证明模块图解析成功的是 Vite 对 `/src/App.tsx` 的转换结果：有未解析 import 时它返回 500 而不是编译后的模块。

- [ ] **Step 5: 提交**

```bash
git add apps/web
git commit -m "feat(web): scaffold react + typescript + vite app on port 4274"
```

---

## Task 23: API client 与资产网格

**Files:**
- Create: `apps/web/src/shared/api/client.ts`
- Create: `apps/web/src/features/library/AssetGrid.tsx`
- Create: `apps/web/src/features/library/AssetDetail.tsx`
- Create: `apps/web/src/styles.css`
- Modify: `apps/web/src/App.tsx`（替换为接上 client 与 AssetGrid 的完整版）
- Modify: `apps/web/src/main.tsx`（替换为引入 `styles.css` 的版本）

- [ ] **Step 1: 写 client（保留 SSE 能力供 S3 使用）**

`apps/web/src/shared/api/client.ts`:

```ts
const BASE = "/api/v1";

export interface AssetSummary {
  id: string;
  file_name: string;
  media_type: string;
  mime_type: string | null;
  size_bytes: number;
  status: string;
  captured_at: string | null;
  gps: { latitude: number; longitude: number } | null;
}

interface AssetListResponse {
  items: AssetSummary[];
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, init);
  if (!response.ok) {
    const body = await response.text();
    throw new Error(`Sentrix API ${response.status}: ${body}`);
  }
  return (await response.json()) as T;
}

export async function listAssets(scopeId: string): Promise<AssetSummary[]> {
  const body = await request<AssetListResponse>(`/assets?scope_id=${encodeURIComponent(scopeId)}`);
  return body.items;
}

export function assetFileUrl(assetId: string): string {
  return `${BASE}/assets/${assetId}/file`;
}

export function assetPreviewUrl(assetId: string): string {
  return `${BASE}/assets/${assetId}/preview`;
}

/** 浏览器能直接渲染的格式走原图，其余（HEIC 等）走服务端转码预览。 */
export function assetDisplayUrl(asset: AssetSummary): string {
  if (asset.mime_type === "image/heic" || asset.mime_type === "image/heif") {
    return assetPreviewUrl(asset.id);
  }
  return assetFileUrl(asset.id);
}
```

- [ ] **Step 2: 写资产网格**

`apps/web/src/features/library/AssetGrid.tsx`:

```tsx
import { useState } from "react";

import { assetDisplayUrl, type AssetSummary } from "../../shared/api/client";
import { AssetDetail } from "./AssetDetail";

export function AssetGrid({ assets }: { assets: AssetSummary[] }) {
  const [selected, setSelected] = useState<AssetSummary | null>(null);

  if (assets.length === 0) {
    return <p className="empty">还没有照片。用 curl 导入一张：<code>curl -F file=@photo.jpg -F scope_id=album1 http://127.0.0.1:9598/api/v1/media/import</code></p>;
  }

  return (
    <>
      <ul className="grid">
        {assets.map((asset) => (
          <li key={asset.id}>
            <button type="button" onClick={() => setSelected(asset)}>
              <img src={assetDisplayUrl(asset)} alt={asset.file_name} loading="lazy" />
              <span>{asset.file_name}</span>
            </button>
          </li>
        ))}
      </ul>
      {selected ? <AssetDetail asset={selected} onClose={() => setSelected(null)} /> : null}
    </>
  );
}
```

- [ ] **Step 3: 写详情（原图）**

`apps/web/src/features/library/AssetDetail.tsx`:

```tsx
import { assetFileUrl, type AssetSummary } from "../../shared/api/client";

export function AssetDetail({ asset, onClose }: { asset: AssetSummary; onClose: () => void }) {
  return (
    <div className="detail" role="dialog" aria-modal="true">
      <button type="button" className="close" onClick={onClose}>关闭</button>
      {/* 详情一律走 /file 原图，符合产品硬约束：查询必须返回可打开的原始 Asset */}
      <img src={assetFileUrl(asset.id)} alt={asset.file_name} />
      <dl>
        <dt>文件名</dt><dd>{asset.file_name}</dd>
        <dt>拍摄时间</dt><dd>{asset.captured_at ?? "未知"}</dd>
        <dt>地点</dt>
        <dd>{asset.gps ? `${asset.gps.latitude.toFixed(6)}, ${asset.gps.longitude.toFixed(6)}` : "无 GPS"}</dd>
        <dt>原图</dt><dd><a href={assetFileUrl(asset.id)} target="_blank" rel="noreferrer">打开原图</a></dd>
      </dl>
    </div>
  );
}
```

- [ ] **Step 4: 写样式与应用入口**

`apps/web/src/App.tsx`（**替换为**完整版：接上本任务刚建的 client 与 AssetGrid）：

```tsx
import { useEffect, useState } from "react";

import { AssetGrid } from "./features/library/AssetGrid";
import { listAssets, type AssetSummary } from "./shared/api/client";

export default function App() {
  const [assets, setAssets] = useState<AssetSummary[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listAssets("album1")
      .then(setAssets)
      .catch((cause: Error) => setError(cause.message));
  }, []);

  return (
    <main className="app">
      <h1>Sentrix Home 2.0</h1>
      {error ? <p className="error">{error}</p> : null}
      <AssetGrid assets={assets} />
    </main>
  );
}
```

`apps/web/src/main.tsx`（**替换为**：改引本项目样式表）：

```tsx
import React from "react";
import ReactDOM from "react-dom/client";

import App from "./App";
import "./styles.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
```

`apps/web/src/styles.css`:

```css
:root { color-scheme: light dark; font-family: system-ui, sans-serif; }
body { margin: 0; }
.app { padding: 1.5rem; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 0.75rem; list-style: none; padding: 0; }
.grid button { width: 100%; background: none; border: 1px solid #8884; border-radius: 8px; padding: 0.5rem; cursor: pointer; }
.grid img { width: 100%; height: 140px; object-fit: cover; border-radius: 4px; display: block; }
.grid span { display: block; font-size: 0.75rem; margin-top: 0.35rem; overflow-wrap: anywhere; }
.detail { position: fixed; inset: 0; background: #000c; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 1rem; padding: 2rem; }
.detail img { max-width: min(90vw, 1200px); max-height: 62vh; object-fit: contain; }
.detail dl { display: grid; grid-template-columns: auto auto; gap: 0.25rem 1rem; color: white; margin: 0; }
.detail dt { opacity: 0.7; }
.error { color: #c33; }
.empty { opacity: 0.7; }
```

- [ ] **Step 5: 类型检查与构建**

```bash
cd apps/web && npx tsc -b && npx oxlint && npx vite build
```

> **为什么不是 `tsc --noEmit`（2026-09-22 修正）**：本仓库的 `apps/web/tsconfig.json` 是
> `{"files": [], "references": ["./tsconfig.app.json", "./tsconfig.node.json"]}` 结构，
> 普通 `tsc` **不遍历 project references**，`npx tsc --noEmit --listFiles` 实测命中 **0 个文件**、
> 恒 exit 0——它看着在检查，其实永远通过。真正的类型检查只在 `tsc -b` 里（`apps/web` 的
> `build` 脚本用的就是它）。本仓库今后一律用 `tsc -b`，`make web-check` 已把它固化进门禁。

Expected: 无错误

- [ ] **Step 6: 提交**

```bash
git add apps/web/src
git commit -m "feat(web): add api client, asset grid and original-photo detail"
```

---

## Task 24: 端到端验收

**Files:**
- Create: `apps/api/tests/e2e/test_import_and_deliver.py`

- [ ] **Step 1: 写 e2e 测试（用真实照片夹具）**

`apps/api/tests/e2e/test_import_and_deliver.py`:

```python
"""端到端：导入真实照片 → 列表可见 → 能取到与原文件逐字节相同的原图。"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

FIXTURE_DIR = Path("/home/asus/Github/Sentrix-Home-Web/data/album3-v2-source/photos")


@pytest.fixture
def real_photo() -> Path:
    candidates = sorted(FIXTURE_DIR.glob("*.jpg")) + sorted(FIXTURE_DIR.glob("*.JPG"))
    if not candidates:
        pytest.skip(f"真实照片夹具不可用: {FIXTURE_DIR}")
    return candidates[0]


def test_real_photo_survives_full_roundtrip(client: TestClient, real_photo: Path):
    original = real_photo.read_bytes()

    imported = client.post(
        "/api/v1/media/import",
        files={"file": (real_photo.name, original, "image/jpeg")},
        data={"scope_id": "album1"},
    )
    assert imported.status_code == 201
    asset_id = imported.json()["asset_id"]

    listed = client.get("/api/v1/assets", params={"scope_id": "album1"}).json()["items"]
    assert any(item["id"] == asset_id for item in listed)

    served = client.get(f"/api/v1/assets/{asset_id}/file")
    assert served.content == original, "原图必须逐字节一致——这是产品硬约束第 5 条"

    preview = client.get(f"/api/v1/assets/{asset_id}/preview")
    assert preview.status_code == 200
    assert preview.content[:2] == b"\xff\xd8"


def test_captured_at_is_derived_when_exif_present(client: TestClient, real_photo: Path):
    imported = client.post(
        "/api/v1/media/import",
        files={"file": (real_photo.name, real_photo.read_bytes(), "image/jpeg")},
        data={"scope_id": "album1"},
    ).json()

    detail = client.get(f"/api/v1/assets/{imported['asset_id']}").json()

    assert detail["captured_at"] is not None
```

- [ ] **Step 2: 跑 e2e**

Run: `.venv/bin/pytest apps/api/tests/e2e -v`
Expected: 2 passed（若夹具路径不可用会 skip，需确认不是 skip）

- [ ] **Step 3: 手工浏览器验收**

```bash
cd /home/asus/Github/sentrix-home-web2.0
make dev &          # API on 9598
make web-dev &      # Web on 4274

# 导入一张真实照片
curl -s -F "file=@/home/asus/Github/Sentrix-Home-Web/data/album3-v2-source/photos/$(ls /home/asus/Github/Sentrix-Home-Web/data/album3-v2-source/photos | head -1)" \
     -F "scope_id=album1" \
     http://127.0.0.1:9598/api/v1/media/import
```

浏览器打开 `http://192.168.0.153:4274/`，确认：
1. 网格里出现刚导入的照片缩略图
2. 点击后弹出详情，显示**原图**
3. 点"打开原图"链接，浏览器直接渲染原图
4. 详情里的拍摄时间与 GPS 与照片实际 EXIF 一致

- [ ] **Step 4: 更新 capability-map**

往 `docs/refactor/capability-map.jsonl` 追加（每条替换 `verification` 为实际通过的测试路径）：

```jsonl
{"capability":"媒体导入（图片）","origin_kind":"migrated","source_commit":"2d2cee7c","source_path":"backend/app.py","source_symbol":"ingest_asset","target_path":"apps/api/src/sentrix/application/media/import_assets.py","verification":"apps/api/tests/contract/test_media_routes.py","status":"verified"}
{"capability":"按 scope 内容去重","origin_kind":"migrated","source_commit":"2d2cee7c","source_path":"backend/db.py","source_symbol":"find_asset_by_hash","target_path":"apps/api/src/sentrix/application/media/import_assets.py","verification":"apps/api/tests/unit/test_import_assets.py","status":"verified"}
{"capability":"EXIF 拍摄时间与 GPS","origin_kind":"migrated","source_commit":"2d2cee7c","source_path":"backend/pipeline.py","source_symbol":"IngestionPipeline._extract_exif","target_path":"apps/api/src/sentrix/infrastructure/media/metadata.py","verification":"apps/api/tests/integration/test_metadata_reader.py","status":"verified"}
{"capability":"HEIC 支持与浏览器转码","origin_kind":"migrated","source_commit":"2d2cee7c","source_path":"backend/image_io.py","source_symbol":"encode_jpeg_preview","target_path":"apps/api/src/sentrix/infrastructure/media/image_io.py","verification":"apps/api/tests/integration/test_image_io.py","status":"verified"}
{"capability":"原图交付","origin_kind":"migrated","source_commit":"2d2cee7c","source_path":"backend/app.py","source_symbol":"get_asset_file","target_path":"apps/api/src/sentrix/api/routes/media.py","verification":"apps/api/tests/e2e/test_import_and_deliver.py","status":"verified"}
```

- [ ] **Step 5: 补 module-map**

把 S1 新增的每个 `.py` 按 Task 9 的表格格式补进 `docs/architecture/module-map.md`。

- [ ] **Step 6: 全量门禁**

Run: `make ci`
Expected: 全部通过

- [ ] **Step 7: 提交**

```bash
git add apps/api/tests/e2e docs/refactor/capability-map.jsonl docs/architecture/module-map.md
git commit -m "test(e2e): verify import-to-original-photo roundtrip on real fixture"
```

---

## M1 前置检查（S0–S1 完成判据）

- [ ] `make ci` 全绿
- [ ] `make arch` 全绿
- [ ] 浏览器能导入真实照片并看到原图（Task 24 Step 3）
- [ ] `capability-map.jsonl` 里 S0–S1 覆盖的能力全部 `status=verified`
- [ ] `docs/refactor/behaviour-baseline.md` 的「待核实项」已清空
- [ ] 8091 已由 systemd 托管（Task 12 Step 4）
- [ ] `docs/runbooks/153-services.md` 已提交

**M1 完成后**：按设计 §6.2 把 `make arch` 并入 `make ci`，打开全量分层强制。
