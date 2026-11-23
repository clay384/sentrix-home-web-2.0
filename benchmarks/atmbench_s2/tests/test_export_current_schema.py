import json
from pathlib import Path

from sentrix_s2.export_native import digest, export

from sentrix.infrastructure.persistence.database import Database

SCOPED_FIELDS = {
    "assets": {"id", "scope_id", "media_type", "content_sha256", "parent_asset_id", "source_timestamp_sec"},
    "observations": {
        "id",
        "scope_id",
        "asset_id",
        "source_type",
        "caption",
        "source_text",
        "place",
        "activity",
        "event_type",
        "ocr_text",
        "objects_json",
        "clothing_json",
        "people_json",
        "facts_json",
        "detail_json",
        "confidence",
        "revision",
    },
    "events": {"id", "scope_id", "title", "summary", "activity", "event_type"},
    "memory_graph_nodes": {"id", "scope_id", "label", "attributes_json"},
    "family_analysis_runs": {"scope_id", "status", "created_at"},
    "event_observations": {"event_id", "observation_id"},
}
VECTOR_FIELDS = {"id", "scope_id", "source_id", "model_name", "dimension", "space", "vector_json"}
SCOPED_FIELDS.update(memory_vectors=VECTOR_FIELDS, text_vectors=VECTOR_FIELDS)


def put_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def test_export_fresh_main_is_scoped_and_read_only(tmp_path):
    scope = "atm-pilot-synthetic"
    root = tmp_path / "isolated benchmark #1?"
    source_db = root / "runtime/sentrix-s1/data/sentrix.db"
    source_db.parent.mkdir(parents=True)
    migration_dir = (
        Path(__file__).resolve().parents[3] / "apps/api/src/sentrix/infrastructure/persistence/migrations"
    )
    db = Database(source_db, migration_dir)
    db.migrate()
    assert "0036" in db.applied_versions()
    sources = {
        "video": {"source_id": "video", "modality": "video", "source_sha256": "v"},
        "frame": {
            "source_id": "frame",
            "modality": "image",
            "source_sha256": "f",
            "parent_id": "video",
            "parent_source_sha256": "v",
            "source_timestamp_sec": 1.25,
        },
        "mail": {
            "source_id": "mail",
            "modality": "email",
            "source_sha256": "m",
            "detail": "Synthetic confirmation",
        },
    }
    with db.connect() as conn, conn:
        for table, expected_fields in SCOPED_FIELDS.items():
            fields = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            assert not (expected_fields - fields), (table, expected_fields - fields)
        for identity, media_type, checksum, parent, timestamp in (
            ("avideo", "video", "v", None, None),
            ("aframe", "image", "f", "avideo", 1.25),
            ("amail", "text", "m", None, None),
        ):
            conn.execute(
                "INSERT INTO assets (id,scope_id,file_name,media_type,path,content_sha256,"
                "created_at,updated_at,parent_asset_id,source_timestamp_sec) VALUES "
                "(?,?,?,?,'only-benchmark-data',?,'2026-11-20','2026-11-20',?,?)",
                (identity, scope, identity, media_type, checksum, parent, timestamp),
            )
        conn.execute(
            "INSERT INTO assets (id,scope_id,file_name,media_type,path,created_at,updated_at)"
            " VALUES ('foreign','other-scope','foreign','image','isolated-flag','2026-11-20','2026-11-20')"
        )
        for identity, source_type, caption, text, confidence in (
            ("video", "import", "", "", 0),
            ("frame", "import", "Synthetic frame caption", "", 0.8),
            ("mail", "text_import", "", "Synthetic confirmation", 1),
        ):
            conn.execute(
                "INSERT INTO observations (id,scope_id,asset_id,source_type,confidence,caption,"
                "source_text,created_at,updated_at) VALUES (?,?,?,?,?,?,?,'2026-11-20','2026-11-20')",
                ("o" + identity, scope, "a" + identity, source_type, confidence, caption, text),
            )
        for table, vector_id, space, source_type, source_id in (
            ("memory_vectors", "vf", "visual", "asset", "aframe"),
            ("text_vectors", "vm", "semantic", "observation", "omail"),
        ):
            conn.execute(
                f"INSERT INTO {table} (id,scope_id,space,source_type,source_id,model_name,dimension,"
                "vector_json,created_at,updated_at) VALUES (?,?,?,?,?,'Qwen/Qwen3-VL-Embedding-2B',"
                "2048,?,'2026-11-20','2026-11-20')",
                (vector_id, scope, space, source_type, source_id, json.dumps([1.0] + [0.0] * 2047)),
            )
    source_path = root / "assets/protocol/ingest/sources.jsonl"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("".join(json.dumps(row) + "\n" for row in sources.values()))
    receipts_root = root / "results/ingest" / scope
    put_json(
        receipts_root / "receipts/video.json",
        {
            "source_id": "video",
            "result": {"receipt": {"asset_id": "avideo"}, "derived_source_map": {"aframe": "frame"}},
        },
    )
    put_json(
        receipts_root / "receipts/mail.json",
        {
            "source_id": "mail",
            "result": {"receipt": {"asset_id": "amail"}},
        },
    )
    put_json(
        receipts_root / "native-source-map.json", {"avideo": "video", "aframe": "frame", "amail": "mail"}
    )
    before_mtime = source_db.stat().st_mtime_ns
    before_hash = digest(source_db)
    output = root / "outputs/snapshot"
    manifest = export(root, scope, output, expected_parent_count=2)
    assert manifest["native_vector_count"] == 2
    assert manifest["metadata_only_video_parents"] == 1
    assert manifest["source_count"] == 3
    assert digest(source_db) == before_hash
    assert source_db.stat().st_mtime_ns == before_mtime
    rows = {
        row["source_id"]: row for row in map(json.loads, (output / "sources.jsonl").read_text().splitlines())
    }
    assert rows["mail"]["text"] == "Synthetic confirmation"
    assert rows["video"]["index_eligible"] is False
    assert rows["frame"]["parent_id"] == "video"
    vectors = {
        row["native_vector_id"]: row
        for row in map(json.loads, (output / "native-vectors.jsonl").read_text().splitlines())
    }
    assert vectors["vf"]["source_ids"] == ["frame"]
    assert vectors["vm"]["source_ids"] == ["mail"]
    assert set(manifest["files"]) == {"sources.jsonl", "native-vectors.jsonl"}
    assert sorted(path.name for path in output.iterdir()) == [
        "manifest.json",
        "native-vectors.jsonl",
        "sources.jsonl",
    ]
    assert not (tmp_path / "isolated benchmark ").exists()
