"""Read-only export of finalized Sentrix sources, registered frame lineage and real vectors."""

import argparse
import hashlib
import json
import math
import sqlite3
from contextlib import closing
from pathlib import Path


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def project_native_sources(db, scope, sources, mapping):
    """Read source rows; empty native video observations remain metadata only."""
    assets = {r["id"]: dict(r) for r in db.execute("SELECT * FROM assets WHERE scope_id=?", (scope,))}
    observation_rows = [dict(r) for r in db.execute("SELECT * FROM observations WHERE scope_id=?", (scope,))]
    observations = {r["asset_id"]: r for r in observation_rows}
    if len(mapping) != len(set(mapping.values())) or not set(mapping.values()).issubset(sources):
        raise ValueError("native_mapping_not_one_to_one_source_coverage")
    if set(assets) != set(mapping) or set(observations) != set(mapping):
        raise ValueError("native_asset_observation_coverage_not_exact")
    if len(observations) != len(observation_rows) or len({r["id"] for r in observation_rows}) != len(
        observation_rows
    ):
        raise ValueError("one_observation_per_native_asset_required")
    reverse = {sid: aid for aid, sid in mapping.items()}
    observation_map = {}
    rows = []
    metadata_parents = set()
    frame_parents = set()
    for aid, sid in sorted(mapping.items()):
        source = sources[sid]
        obs = observations[aid]
        asset = assets[aid]
        parent = source.get("parent_id") or sid
        if source["modality"] == "video":
            if (
                parent != sid
                or asset["media_type"] != "video"
                or asset["parent_asset_id"] is not None
                or asset["content_sha256"] != source["source_sha256"]
            ):
                raise ValueError("native_video_parent_hash_or_lineage_changed:" + sid)
            if (
                obs["source_type"] != "import"
                or any(
                    obs[k] != ""
                    for k in ("caption", "source_text", "place", "activity", "event_type", "ocr_text")
                )
                or any(
                    json.loads(obs[k]) != []
                    for k in ("objects_json", "clothing_json", "people_json", "facts_json")
                )
                or json.loads(obs["detail_json"]) != {}
                or obs["confidence"] != 0
                or obs["revision"] != 1
            ):
                raise ValueError("native_video_parent_not_empty_placeholder:" + sid)
            text = ""
            metadata_parents.add(sid)
        else:
            text = obs["source_text"] if source["modality"] in ("email", "text") else obs["caption"]
            expected_type = "text" if source["modality"] in ("email", "text") else "image"
            if asset["media_type"] != expected_type:
                raise ValueError("native_source_media_type_changed:" + sid)
            if source["modality"] in ("email", "text") and text != source["detail"]:
                raise ValueError("native_published_text_mismatch:" + sid)
            if not text:
                raise ValueError("native_text_missing:" + sid)
            if source["modality"] == "image" and asset["content_sha256"] != source["source_sha256"]:
                raise ValueError("native_image_hash_changed:" + sid)
            if parent != sid:
                if (
                    source["modality"] != "image"
                    or parent not in reverse
                    or sources[parent]["modality"] != "video"
                    or source.get("parent_source_sha256") != sources[parent]["source_sha256"]
                    or asset["parent_asset_id"] != reverse[parent]
                ):
                    raise ValueError("native_frame_parent_changed:" + sid)
                actual, expected = asset["source_timestamp_sec"], source["source_timestamp_sec"]
                if (
                    type(actual) not in (int, float)
                    or type(expected) not in (int, float)
                    or not math.isfinite(actual)
                    or not math.isfinite(expected)
                    or min(actual, expected) < 0
                    or abs(actual - expected) >= 1e-8
                ):
                    raise ValueError("native_frame_timestamp_changed:" + sid)
                frame_parents.add(parent)
            elif asset["parent_asset_id"]:
                raise ValueError("native_original_source_has_parent:" + sid)
        # Keep native event lineage even for metadata-only parents. No parent text
        # span is generated; direct vectors on empty parent observations are rejected.
        observation_map[obs["id"]] = sid
        rows.append(
            {
                **source,
                "native_asset_id": aid,
                "native_observation_id": obs["id"],
                "text": text,
                "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "index_eligible": bool(text),
                "index_text_basis": "metadata_parent_only"
                if not text
                else "native_source_text"
                if source["modality"] in ("email", "text")
                else "native_shared_caption",
            }
        )
    if metadata_parents != frame_parents:
        raise ValueError("video_parent_has_no_indexed_registered_frames")
    return rows, observation_map, metadata_parents


def export(root, scope, output, extra_sources=None, expected_parent_count=None, finalized=False):
    if scope.startswith("atm-main-") and not finalized:
        raise ValueError("main_export_requires_explicit_finalized_flag_after_owner_verification")
    source_path = Path(extra_sources) if extra_sources else root / "assets/protocol/ingest/sources.jsonl"
    with source_path.open() as stream:
        sources = {r["source_id"]: r for r in map(json.loads, stream)}
    records = []
    for path in (root / "results/ingest" / scope).glob("**/receipts/*.json"):
        records.append(json.loads(path.read_text()))
    mapping = {}
    for record in records:
        result = record["result"]
        receipt = result["receipt"]
        additions = {
            receipt["asset_id"]: record["source_id"],
            **result.get("derived_source_map", {}),
            **receipt.get("derived_source_map", {}),
            **record.get("derived_source_map", {}),
        }
        for aid, sid in additions.items():
            if aid in mapping and mapping[aid] != sid:
                raise ValueError("conflicting_native_mapping")
            mapping[aid] = sid
    if not mapping:
        raise ValueError("no_native_ingest_receipts")
    map_file = root / "results/ingest" / scope / "native-source-map.json"
    if not map_file.exists() or json.loads(map_file.read_text()) != mapping:
        raise ValueError("frozen_native_map_disagrees_with_receipts_or_missing")
    if any(sid not in sources for sid in mapping.values()):
        raise ValueError("mapped_source_missing_from_public_manifest")
    parent_ids = {sources[sid].get("parent_id") or sid for sid in mapping.values()}
    if expected_parent_count is not None and len(parent_ids) != expected_parent_count:
        raise ValueError("native_parent_coverage_incomplete")
    if scope.startswith("atm-main-"):
        if expected_parent_count != 11034 or set(mapping.values()) != set(sources):
            raise ValueError("main_requires_all_11034_parents_and_exact_public_frame_source_coverage")
    output.mkdir(parents=True, exist_ok=False)
    native_db = (root / "runtime/sentrix-s1/data/sentrix.db").resolve()
    # One read transaction provides a consistent view without copying unrelated scopes.
    with closing(sqlite3.connect(native_db.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        if scope.startswith("atm-main-"):
            for event in db.execute(
                "SELECT id,title,summary,activity,event_type FROM events WHERE scope_id=?", (scope,)
            ).fetchall():
                graph = db.execute(
                    "SELECT label,attributes_json FROM memory_graph_nodes WHERE scope_id=? AND id=?",
                    (scope, "event:" + event["id"]),
                ).fetchone()
                if graph is None:
                    raise ValueError("main_memory_graph_missing_event")
                attrs = json.loads(graph["attributes_json"])
                if graph["label"] != event["title"] or any(
                    attrs.get(k) != event[k] for k in ("summary", "activity", "event_type")
                ):
                    raise ValueError("main_memory_graph_not_finalized")
            family = db.execute(
                "SELECT status FROM family_analysis_runs WHERE scope_id=? ORDER BY created_at DESC LIMIT 1",
                (scope,),
            ).fetchone()
            if family is None or family["status"] != "completed":
                raise ValueError("main_family_stage_not_completed")
        rows, observation_map, metadata_parents = project_native_sources(db, scope, sources, mapping)
        empty_parent_ids = {
            identity
            for row in rows
            if row["source_id"] in metadata_parents
            for identity in (row["native_asset_id"], row["native_observation_id"])
        }
        event_sources = {}
        for row in db.execute("SELECT event_id,observation_id FROM event_observations"):
            if row["observation_id"] in observation_map:
                event_sources.setdefault(row["event_id"], []).append(observation_map[row["observation_id"]])
        count = 0
        with (output / "native-vectors.jsonl").open("x") as stream:
            for table in ("memory_vectors", "text_vectors"):
                for row in db.execute(
                    "SELECT * FROM " + table + " WHERE scope_id=? AND model_name=?",
                    (scope, "Qwen/Qwen3-VL-Embedding-2B"),
                ):
                    if row["source_id"] in empty_parent_ids:
                        raise ValueError("native_empty_video_parent_has_qwen_vector")
                    ids = (
                        [mapping[row["source_id"]]]
                        if row["source_id"] in mapping
                        else [observation_map[row["source_id"]]]
                        if row["source_id"] in observation_map
                        else event_sources.get(row["source_id"], [])
                    )
                    if not ids:
                        raise ValueError("unmapped_native_qwen_vector:" + row["source_id"])
                    if row["dimension"] != 2048:
                        raise ValueError("native_dimension_mismatch")
                    value = {
                        "native_vector_id": row["id"],
                        "native_source_id": row["source_id"],
                        "source_ids": sorted(set(ids)),
                        "channel": "native_" + row["space"],
                        "model": row["model_name"],
                        "vector": json.loads(row["vector_json"]),
                    }
                    stream.write(json.dumps(value, ensure_ascii=False) + "\n")
                    count += 1
    (output / "sources.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows))
    manifest = {
        "scope_id": scope,
        "source_count": len(rows),
        "parent_count": len(parent_ids),
        "metadata_only_video_parents": len(metadata_parents),
        "native_vector_count": count,
        "native_scope_finalized_declared": finalized,
        "input_policy": "only_native_ingested_source_observations; shared caption; no questions/gold",
        "public_manifest_path": str(source_path),
        "public_manifest_sha256": digest(source_path),
        "native_source_map_sha256": digest(map_file),
        "files": {p.name: digest(p) for p in (output / "sources.jsonl", output / "native-vectors.jsonl")},
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--scope", required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--extra-sources", type=Path)
    p.add_argument("--expected-parent-count", type=int)
    p.add_argument("--finalized", action="store_true")
    a = p.parse_args()
    print(
        json.dumps(export(a.root, a.scope, a.output, a.extra_sources, a.expected_parent_count, a.finalized))
    )
