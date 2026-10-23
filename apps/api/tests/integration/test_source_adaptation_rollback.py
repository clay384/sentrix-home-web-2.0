import importlib.util
import sqlite3
from pathlib import Path

import pytest

from sentrix.infrastructure.persistence.database import Database

ROOT = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location(
    "rollback_source_adaptation", ROOT / "scripts" / "rollback_source_adaptation.py"
)
assert spec is not None and spec.loader is not None
rollback = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rollback)


def test_rollback_copy_preserves_old_rows_and_can_upgrade_again(tmp_path):
    migrations = ROOT / "apps/api/src/sentrix/infrastructure/persistence/migrations"
    old_migrations = tmp_path / "old-migrations"
    old_migrations.mkdir()
    for path in migrations.glob("*.sql"):
        if path.stem[:4] <= "0013":
            (old_migrations / path.name).write_bytes(path.read_bytes())
    original = tmp_path / "original.db"
    db = Database(original, old_migrations)
    db.migrate()
    with db.connect() as connection, connection:
        connection.execute(
            "INSERT INTO memory_spaces(id,name,created_at,updated_at) "
            "VALUES ('existing','Existing library','2020-01-01','2020-01-01')"
        )
    Database(original, migrations).migrate()
    before = original.read_bytes()
    output = tmp_path / "rolled-back.db"
    rollback.rollback_copy(original, output)
    assert original.read_bytes() == before
    with sqlite3.connect(output) as connection:
        assert connection.execute("SELECT name FROM memory_spaces WHERE id='existing'").fetchone() == (
            "Existing library",
        )
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == ("0013",)
        assert "source_text" not in {r[1] for r in connection.execute("PRAGMA table_info(observations)")}
    assert Database(output, migrations).migrate() == ["0014", "0015"]


@pytest.mark.parametrize(
    "case", ["used-fields", "future-schema", "missing-migration", "existing-output", "existing-sidecar"]
)
def test_rollback_refuses_data_loss_or_overwrite(tmp_path, case):
    original = tmp_path / "original.db"
    db = Database(original, ROOT / "apps/api/src/sentrix/infrastructure/persistence/migrations")
    db.migrate()
    with db.connect() as connection, connection:
        if case == "used-fields":
            # A minimal source row is enough to prove that dropping the fields would lose data.
            connection.execute(
                "INSERT INTO assets(id,scope_id,file_name,path,media_type,content_sha256,size_bytes,"
                "created_at,updated_at,external_source_id) VALUES "
                "('source','default','source.txt','source.txt','text',"
                "'synthetic-hash',3,'2020-01-01','2020-01-01','source-a')"
            )
        elif case == "future-schema":
            connection.execute("INSERT INTO schema_migrations(version) VALUES ('0016')")
        elif case == "missing-migration":
            connection.execute("DELETE FROM schema_migrations WHERE version = '0013'")
    output = tmp_path / "rolled-back.db"
    if case == "existing-output":
        output.write_bytes(b"keep this file")
    sidecar = Path(str(output) + "-wal")
    if case == "existing-sidecar":
        sidecar.write_bytes(b"keep sidecar")
    with pytest.raises((ValueError, FileExistsError)):
        rollback.rollback_copy(original, output)
    if case == "existing-output":
        assert output.read_bytes() == b"keep this file"
    else:
        assert not output.exists()
    if case == "existing-sidecar":
        assert sidecar.read_bytes() == b"keep sidecar"
    else:
        assert not sidecar.exists()


def test_rollback_reads_committed_wal_and_closes_standalone_output(tmp_path):
    original = tmp_path / "live.db"
    Database(original, ROOT / "apps/api/src/sentrix/infrastructure/persistence/migrations").migrate()
    with sqlite3.connect(original) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "INSERT INTO memory_spaces(id,name,created_at,updated_at) "
            "VALUES ('wal-row','Committed in WAL','2020-01-01','2020-01-01')"
        )
        writer.commit()
        assert Path(str(original) + "-wal").stat().st_size > 0
        output = tmp_path / "standalone.db"
        rollback.rollback_copy(original, output)
        for suffix in ("-wal", "-shm", "-journal"):
            assert not Path(str(output) + suffix).exists()
        with sqlite3.connect(output) as restored:
            assert restored.execute("SELECT name FROM memory_spaces WHERE id='wal-row'").fetchone() == (
                "Committed in WAL",
            )
            assert restored.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        # No source connection or write lock remains in the utility.
        writer.execute("BEGIN EXCLUSIVE")
        writer.rollback()
    writer.close()


def test_rollback_cleans_reserved_output_when_permissions_fail(tmp_path, monkeypatch):
    original = tmp_path / "original.db"
    Database(original, ROOT / "apps/api/src/sentrix/infrastructure/persistence/migrations").migrate()
    output = tmp_path / "new-copy.db"
    before = original.read_bytes()
    chmod = Path.chmod

    def fail_on_output(path, mode):
        if path == output:
            raise PermissionError("synthetic permission failure")
        chmod(path, mode)

    monkeypatch.setattr(Path, "chmod", fail_on_output)
    with pytest.raises(PermissionError, match="synthetic permission failure"):
        rollback.rollback_copy(original, output)
    assert not output.exists()
    assert original.read_bytes() == before


def test_rollback_preserves_preexisting_dangling_sidecar_link(tmp_path):
    original = tmp_path / "original.db"
    Database(original, ROOT / "apps/api/src/sentrix/infrastructure/persistence/migrations").migrate()
    output = tmp_path / "new-copy.db"
    sidecar = Path(str(output) + "-wal")
    target = tmp_path / "missing-sidecar"
    sidecar.symlink_to(target)
    with pytest.raises(FileExistsError):
        rollback.rollback_copy(original, output)
    assert sidecar.is_symlink() and sidecar.readlink() == target
    assert not output.exists()
