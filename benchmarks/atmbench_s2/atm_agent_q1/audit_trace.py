"""Read-only check against the gateway's real dispatched payloads and billing ledger."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path


def audit(root: Path, ledger: Path) -> dict:
    checks = []
    missing_results = [
        str(path.parent.name)
        for path in root.glob("*/run.json")
        if not (path.parent / "result.json").exists()
    ]
    with closing(sqlite3.connect(ledger.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        for result_path in sorted(root.glob("*/result.json")):
            folder = result_path.parent
            result = json.loads(result_path.read_text())
            dispatches, delivered, metadata_rows, derived_visible = [], set(), [], 0
            for request_path in sorted(folder.glob("decision-*-request.json")):
                request = json.loads(request_path.read_text())
                request_id = request["context"]["request_id"]
                row = db.execute("SELECT * FROM requests WHERE request_id=?", (request_id,)).fetchone()
                if row is None or not row["payload_path"]:
                    dispatches.append(
                        {
                            "request_id": request_id,
                            "payload_contract_pass": False,
                            "error": "gateway_dispatched_payload_not_found",
                        }
                    )
                    continue
                forwarded = json.loads(Path(row["payload_path"]).read_text())["request"]
                image_hashes = []
                for message in forwarded["messages"]:
                    if isinstance(message.get("content"), list):
                        for part in message["content"]:
                            if part.get("type") == "image_url":
                                image = base64.b64decode(
                                    part["image_url"]["url"].split(",", 1)[1], validate=True
                                )
                                image_hashes.append(hashlib.sha256(image).hexdigest())
                if row["status"] == "completed":
                    delivered.update(image_hashes)
                dispatches.append(
                    {
                        "request_id": request_id,
                        "gateway_status": row["status"],
                        "provider_request_id": row["provider_request_id"],
                        "image_sha256": image_hashes,
                        "max_tokens": forwarded.get("max_tokens"),
                        "enable_thinking": forwarded.get("enable_thinking"),
                        "payload_contract_pass": row["status"] == "completed"
                        and forwarded.get("model") == "qwen3.7-flash"
                        and forwarded.get("max_tokens") == 131072
                        and forwarded.get("enable_thinking") is True,
                        "usage_priced_cny": row["charged_micro"] / 1_000_000
                        if row["charged_micro"] is not None
                        else None,
                        "usage": json.loads(row["usage_json"]) if row["usage_json"] else None,
                    }
                )
            expected = set()
            for tool_path in folder.glob("decision-*-tool-*.json"):
                tool = json.loads(tool_path.read_text())
                if tool.get("status") != "ok":
                    continue
                value = tool.get("result", {})
                if tool["tool"] == "inspect_images":
                    expected.update(item["sha256"] for item in value.get("images", []))
                if tool["tool"] == "read_source_metadata":
                    metadata_rows.extend(value.get("sources", []))
                if tool["tool"] == "search_memories":
                    derived_visible += len(value.get("derived_memories", []))
            metadata_ok = all(
                item.get("gps_read_status") in (None, "present", "absent_in_source")
                and (
                    item.get("gps") is None
                    or -90 <= item["gps"]["latitude"] <= 90
                    and -180 <= item["gps"]["longitude"] <= 180
                )
                for item in metadata_rows
            )
            checks.append(
                {
                    "system": result["system"],
                    "question_id": result["question_id"],
                    "status": result["status"],
                    "decision_rounds": result["decision_rounds"],
                    "tool_counts": result["tool_counts"],
                    "payload_contract_pass": all(row["payload_contract_pass"] for row in dispatches),
                    "original_image_delivery_pass": expected.issubset(delivered) if expected else None,
                    "expected_image_sha256": sorted(expected),
                    "metadata_parse_pass": metadata_ok if metadata_rows else None,
                    "metadata": metadata_rows,
                    "derived_cards_visible": derived_visible,
                    "usage_priced_cny_answer_only": sum(row["usage_priced_cny"] for row in dispatches)
                    if all(row.get("usage_priced_cny") is not None for row in dispatches)
                    else None,
                    "dispatches": dispatches,
                }
            )
    return {
        "scope": "Q1 answer calls only; framework-internal model calls are billed separately by the same gateway",
        "trace_root": str(root),
        "ledger": str(ledger),
        "checks": checks,
        "missing_results": missing_results,
        "passed": bool(checks)
        and not missing_results
        and all(
            item["status"] == "completed"
            and item["payload_contract_pass"]
            and item["original_image_delivery_pass"] is not False
            and item["metadata_parse_pass"] is not False
            for item in checks
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = audit(args.root, args.ledger)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(text)
    print(text)


if __name__ == "__main__":
    main()
