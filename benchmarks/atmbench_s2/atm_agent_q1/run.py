"""Single-question Q1 entry point; importable run_question also supports a bounded outer pool."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from .adapters import MemOSHTTPAdapter, SentrixHTTPAdapter, SentrixS2HTTPAdapter
from .core import AgentConfig, CommonAgent, GatewayModel
from .sources import SourceStore
from .transport import LedgerRecoveringTransport


def run_question(config: dict, question: dict, *, attempt_id: str, trace_dir: Path):
    if config.get("framework") == "sentrix" and (
        config.get("graph_enabled") is not False or config.get("search_endpoint") != "/benchmark/search"
    ):
        raise ValueError("sentrix_explicit_graph_disabled_config_required")
    sources = SourceStore.from_jsonl(
        Path(config["sources_jsonl"]), path_prefixes=[tuple(pair) for pair in config.get("path_prefixes", [])]
    )
    captions = {}
    if config.get("caption_cache"):
        with Path(config["caption_cache"]).open(encoding="utf-8") as stream:
            for line in stream:
                row = json.loads(line)
                if row["source_id"] not in sources.sources:
                    continue
                status = row.get(
                    "strict_qwen_reuse_status", row.get("provenance", {}).get("strict_qwen_reuse_status")
                )
                if status == "regenerate_required":
                    continue
                if row.get("source_sha256") != sources.row(row["source_id"]).get("source_sha256"):
                    raise ValueError("caption_source_hash_mismatch")
                if hashlib.sha256(row["caption"].encode()).hexdigest() != row.get("caption_sha256"):
                    raise ValueError("caption_text_hash_mismatch")
                captions[row["source_id"]] = row["caption"]
    sources.captions = captions
    if config.get("caption_cache"):
        sources.provenance.update(
            caption_path=config["caption_cache"],
            caption_cache_sha256=hashlib.sha256(Path(config["caption_cache"]).read_bytes()).hexdigest(),
        )
    mapping = (
        json.loads(Path(config["native_source_map"]).read_text()) if config.get("native_source_map") else {}
    )
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in mapping.items()):
        raise ValueError("native_source_map_must_be_native_id_to_canonical_source_id")
    if config.get("native_source_map"):
        sources.provenance.update(
            native_source_map_path=config["native_source_map"],
            native_source_map_sha256=hashlib.sha256(
                Path(config["native_source_map"]).read_bytes()
            ).hexdigest(),
        )
    headers = {"X-Trace-Id": attempt_id, "X-User-Name": question["id"]}
    if config["framework"] == "sentrix_s2":
        adapter = SentrixS2HTTPAdapter(config["memory_base_url"], headers=headers)
    elif config["framework"] == "sentrix":
        adapter = SentrixHTTPAdapter(
            config["memory_base_url"],
            config["scope_id"],
            mapping,
            graph_enabled=config["graph_enabled"],
            headers=headers,
        )
    elif config["framework"] == "memos":
        adapter = MemOSHTTPAdapter(
            config["memory_base_url"], config["user_id"], config["cube_id"], mapping, headers=headers
        )
    else:
        raise ValueError("unknown_framework")
    gateway_url = config.get("gateway_url", "http://127.0.0.1:19600")
    agent_config = AgentConfig(system=config["system"], run_id=config.get("run_id", "atmbench-20261116"))
    if config.get("gateway_ledger"):
        if (
            gateway_url.rstrip("/") != "http://127.0.0.1:19600"
            or agent_config.run_id != "atmbench-20261116"
            or agent_config.system not in ("S1-Q0", "S2-Q1")
        ):
            raise ValueError("gateway_recovery_requires_frozen_benchmark_profile")
        ledger = Path(config["gateway_ledger"]).resolve()
        if ledger.name != "requests.sqlite3" or ledger.parent.name != "gateway" or not ledger.is_file():
            raise ValueError("gateway_recovery_requires_existing_gateway_ledger")
        model = GatewayModel(gateway_url, transport=LedgerRecoveringTransport(ledger, trace_dir))
    else:
        model = GatewayModel(gateway_url)
    agent = CommonAgent(adapter, sources, model, agent_config)
    return agent.run(question["id"], question["question"], attempt_id=attempt_id, trace_root=trace_dir)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--question-json", type=Path, required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--trace-dir", type=Path, required=True)
    parser.add_argument("--allow-cloud", action="store_true")
    args = parser.parse_args()
    if not args.allow_cloud:
        parser.error("Cloud calls require --allow-cloud; contract tests do not use this entry point.")
    config = json.loads(args.config.read_text())
    question = json.loads(args.question_json.read_text())
    # Question runner accepts only public question text, never an answer/evidence record.
    if set(question) != {"id", "question"}:
        raise ValueError("question_must_only_contain_id_and_question")
    print(
        json.dumps(
            run_question(config, question, attempt_id=args.attempt_id, trace_dir=args.trace_dir),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
