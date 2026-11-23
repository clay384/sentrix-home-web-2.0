"""Synthetic model replies, source references and image data shared by Q1 contract tests."""

import base64
import hashlib
import json

from .adapters import Candidate, SearchResult

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)


def ref(source_id):
    return "src_" + hashlib.sha256(source_id.encode()).hexdigest()[:20]


def reply(*calls):
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"call_{i}",
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                        for i, (name, args) in enumerate(calls)
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    }


class FakeModel:
    def __init__(self, responses):
        self.responses, self.requests = iter(responses), []

    def complete(self, body, context):
        self.requests.append((body, context))
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


class NativeSearchFixture:
    def __init__(self, ids):
        self.ids, self.calls = ids, []

    def search(self, query, top_k):
        self.calls.append((query, top_k))
        return SearchResult(
            [Candidate(source_id, "native_" + source_id) for source_id in self.ids],
            {"query": query, "top_k": top_k},
            {"fixture": True},
        )
