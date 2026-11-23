"""Native HTTP search adapters. No local retrieval/reranking or fallback corpus scan."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol


class AdapterError(RuntimeError):
    pass


@dataclass(frozen=True)
class Candidate:
    source_id: str
    native_id: str
    preview: str = ""
    score: float | None = None
    provenance: dict = field(default_factory=dict)


@dataclass
class SearchResult:
    candidates: list[Candidate]
    request: dict
    response: dict
    warnings: list[str] = field(default_factory=list)
    derived_memories: list[dict] = field(default_factory=list)


class MemoryAdapter(Protocol):
    def search(self, query: str, top_k: int) -> SearchResult: ...


def http_json(method: str, url: str, body: dict | None, headers: dict, timeout: float) -> dict:
    data = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
    request = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json", **headers}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        # Caller records the safe operational classification; do not echo unknown service secrets.
        raise AdapterError(f"http_{error.code}") from None
    except (urllib.error.URLError, TimeoutError) as error:
        raise AdapterError(type(error).__name__ + "_outcome_unknown") from None
    if not isinstance(result, dict):
        raise AdapterError("response_not_object")
    return result


class SentrixHTTPAdapter:
    def __init__(
        self,
        base_url: str,
        scope_id: str,
        source_map: dict[str, str],
        *,
        graph_enabled: bool,
        transport: Callable = http_json,
        timeout: float = 180,
        headers: dict | None = None,
    ):
        if graph_enabled is not False:
            raise AdapterError("sentrix_graph_enabled_must_be_explicit_false")
        self.base_url, self.scope_id, self.source_map = base_url.rstrip("/"), scope_id, source_map
        self.graph_enabled = graph_enabled
        self.transport, self.timeout, self.headers = transport, timeout, headers or {}

    def search(self, query: str, top_k: int) -> SearchResult:
        if not 1 <= len(query) <= 200 or not 1 <= top_k <= 100:
            raise AdapterError("sentrix_native_query_or_limit_constraint")
        params = {"q": query, "scope_id": self.scope_id, "limit": top_k, "graph_enabled": "false"}
        url = self.base_url + "/benchmark/search?" + urllib.parse.urlencode(params)
        response = self.transport("GET", url, None, self.headers, self.timeout)
        diagnostics = response.get("benchmark_diagnostics")
        if not isinstance(diagnostics, dict) or any(
            diagnostics.get(key) is not False
            for key in ("graph_enabled", "graph_channel_invoked", "utility_channel_invoked")
        ):
            raise AdapterError("sentrix_graph_disabled_not_confirmed")
        if not isinstance(response.get("items"), list):
            raise AdapterError("sentrix_items_missing")
        if any(row.get("channel") in ("graph", "utility") for row in response.get("channel_trace", [])):
            raise AdapterError("sentrix_disabled_graph_channel_returned")
        candidates, warnings = [], []
        for item in response["items"]:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("asset"), dict)
                or not isinstance(item["asset"].get("id"), str)
            ):
                raise AdapterError("sentrix_native_hit_schema_invalid")
            native = str(item["asset"]["id"])
            if any(channel in item.get("channels", {}) for channel in ("graph", "utility")):
                raise AdapterError("sentrix_disabled_graph_candidate_returned")
            source_id = self.source_map.get(native)
            if not source_id:
                warnings.append("unmapped_native_id:" + native)
                continue
            candidates.append(
                Candidate(
                    source_id,
                    native,
                    score=item.get("score"),
                    provenance={
                        "channels": item.get("channels", {}),
                        "raw_scores": item.get("raw_scores", {}),
                        "native_asset": item["asset"],
                    },
                )
            )
        if warnings and not candidates:
            raise AdapterError("sentrix_all_hits_unmapped")
        return SearchResult(candidates, {"method": "GET", "url": url}, response, warnings)


def _declared_source_ids(metadata: dict) -> list[str]:
    """Only explicit source identity fields; never infer IDs from generated memory prose."""
    found = []

    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ("source_id", "atm_source_id") and isinstance(child, str):
                    found.append(child)
                elif key in ("source_ids", "atm_source_id", "atm_source_ids") and isinstance(child, list):
                    found.extend(item for item in child if isinstance(item, str))
                elif key in ("info", "sources", "internal_info", "metadata"):
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(metadata)
    return list(dict.fromkeys(found))


class MemOSHTTPAdapter:
    def __init__(
        self,
        base_url: str,
        user_id: str,
        cube_id: str,
        source_map: dict[str, str] | None = None,
        *,
        transport: Callable = http_json,
        timeout: float = 600,
        headers: dict | None = None,
    ):
        self.base_url, self.user_id, self.cube_id = base_url.rstrip("/"), user_id, cube_id
        self.source_map = source_map or {}
        self.transport, self.timeout, self.headers = transport, timeout, headers or {}

    def search(self, query: str, top_k: int) -> SearchResult:
        if not query.strip() or not 1 <= top_k <= 100:
            raise AdapterError("memos_query_or_limit_constraint")
        payload = {
            "user_id": self.user_id,
            "readable_cube_ids": [self.cube_id],
            "query": query,
            "mode": "fine",
            "top_k": top_k,
            "rerank": True,
            "relativity": 0,
            "dedup": "no",
            "include_preference": False,
            "search_tool_memory": False,
            "chat_history": [],
        }
        url = self.base_url + "/product/search"
        response = self.transport("POST", url, payload, self.headers, self.timeout)
        if response.get("code") not in (None, 0, 200):
            raise AdapterError("memos_search_code_" + str(response["code"]))
        data = response.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("text_mem"), list):
            raise AdapterError("memos_text_mem_missing")
        candidates, warnings, derived = [], [], []
        for cube in data["text_mem"]:
            if not isinstance(cube, dict) or not isinstance(cube.get("memories"), list):
                raise AdapterError("memos_native_cube_schema_invalid")
            if cube.get("cube_id") not in (None, self.cube_id):
                raise AdapterError("memos_cube_scope_mismatch")
            for memory in cube.get("memories", []):
                if (
                    not isinstance(memory, dict)
                    or not isinstance(memory.get("id"), str)
                    or not memory["id"]
                    or not isinstance(memory.get("memory"), str)
                ):
                    raise AdapterError("memos_native_memory_schema_invalid")
                native = memory["id"]
                metadata = memory.get("metadata") or {}
                if not isinstance(metadata, dict):
                    raise AdapterError("memos_native_metadata_schema_invalid")
                source_ids = _declared_source_ids(metadata)
                if native in self.source_map:
                    source_ids.append(self.source_map[native])
                if not source_ids:
                    warnings.append("unmapped_native_id:" + native)
                    derived.append(
                        {
                            "kind": "derived/native_memory",
                            "native_memory_id": native,
                            "text": memory["memory"],
                            "provenance_state": "unmapped",
                            "source_refs": [],
                            "native_metadata": metadata,
                        }
                    )
                for source_id in dict.fromkeys(source_ids):
                    candidates.append(
                        Candidate(
                            source_id,
                            native,
                            str(memory.get("memory", "")),
                            memory.get("score", metadata.get("relativity")),
                            {"cube_id": cube.get("cube_id"), "metadata": metadata},
                        )
                    )
        return SearchResult(
            candidates, {"method": "POST", "url": url, "body": payload}, response, warnings, derived
        )


class SentrixS2HTTPAdapter:
    """S2 is an explicit independent Sentrix extension, not an invisible Q0 bypass."""

    def __init__(self, base_url, *, headers=None, transport=http_json, timeout=900):
        self.base_url = base_url.rstrip("/")
        self.headers = headers or {}
        self.transport = transport
        self.timeout = timeout

    def search(self, query, top_k):
        return self.search_plan(query, top_k)

    def search_plan(self, query, top_k, *, queries=None, mode="hybrid", offset=0, constraints=None):
        payload = {
            "query": query,
            "queries": queries or [],
            "mode": mode,
            "top_k": top_k,
            "offset": offset,
            "constraints": constraints or {},
        }
        try:
            response = self.transport("POST", self.base_url + "/search", payload, self.headers, self.timeout)
        except AdapterError as error:
            if str(error) == "http_422":
                raise AdapterError("s2_query_parameter_constraint") from None
            raise
        if not isinstance(response.get("sources"), list):
            raise AdapterError("s2_sources_missing")
        candidates = []
        for item in response["sources"]:
            if not isinstance(item.get("source_id"), str) or not isinstance(
                item.get("matched_source_ids"), list
            ):
                raise AdapterError("s2_source_provenance_invalid")
            candidates.append(
                Candidate(item["source_id"], item["native_id"], item["preview"]["text"], item["score"], item)
            )
        return SearchResult(
            candidates, {"method": "POST", "url": self.base_url + "/search", "body": payload}, response
        )
