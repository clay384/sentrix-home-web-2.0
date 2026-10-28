"""Per-business JSON output contracts; only existing output rules are constrained.

The small supported schema vocabulary is intentional: no remote references,
string/token budgets, or new array caps. Legacy format modes do not validate
against these contracts. Parsed-object shape validation applies only to json_schema.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from typing import Any

from sentrix.domain.assistant.ports import ModelServiceError

Schema = dict[str, Any]
_SCHEMA_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
_SCHEMA_HASH_FORMAT = "sorted_compact_ascii_json_v1"


def _object(properties: dict[str, Schema]) -> Schema:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _strings(**limits: int) -> Schema:
    return {"type": "array", "items": {"type": "string"}, **limits}


# The initial/recovery prompts explicitly specify 40/12/8; canonicalization
# preserves that same observation. No maxLength is introduced here.
OBSERVATION_SCHEMA = _object(
    {
        "caption": {"type": "string"},
        "activity": {"type": "string"},
        "place": {"type": "string"},
        "event_type": {"type": "string"},
        "objects": _strings(maxItems=40),
        "clothing": _strings(maxItems=12),
        "ocr_text": {"type": "string"},
        "facts": {
            "type": "array",
            "maxItems": 8,
            "items": _object(
                {
                    "subject": {"type": "string"},
                    "predicate": {"type": "string"},
                    "object": {"type": "string"},
                    "confidence": {"type": "number"},
                }
            ),
        },
    }
)

# validate_portrait already requires 2–6 model themes and at least one ref
# per theme. Its 80–500 *Hanzi* check is not a JSON string-length constraint.
PORTRAIT_SCHEMA = _object(
    {
        "portrait_text": {"type": "string"},
        "themes": {
            "type": "array",
            "minItems": 2,
            "maxItems": 6,
            "items": _object(
                {
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "evidence_refs": {
                        "type": "array",
                        "minItems": 1,
                        "items": _object(
                            {
                                "kind": {"type": "string", "enum": ["person_moment", "relationship"]},
                                "id": {"type": "string"},
                            }
                        ),
                    },
                }
            ),
        },
    }
)

# There is no existing moment-count or appearance clothing-count budget.
MOMENTS_SCHEMA = _object(
    {
        "moments": {
            "type": "array",
            "items": _object(
                {
                    "label": {"type": "string"},
                    "action_text": {"type": "string"},
                    "interaction_labels": _strings(),
                    "interaction_text": {"type": "string"},
                    "participation_style": {"type": "string"},
                    "visible_affect": {"type": "string"},
                    "social_role_cues": _strings(),
                    "narrative_note": {"type": "string"},
                    "confidence": {"type": "number"},
                }
            ),
        }
    }
)
APPEARANCE_SCHEMA = _object({"clothing": _strings(), "confidence": {"type": "number"}})
INSPECTION_SCHEMA = _object(
    {
        "observation": {"type": "string"},
        "certainty": {"type": "string", "enum": ["supported", "uncertain"]},
        "target_face_id": {"type": "string"},
    }
)


def _check_schema(schema: object) -> None:
    if not isinstance(schema, dict):
        raise ValueError("schema must be an object")
    kind = schema.get("type")
    allowed = {"type", "enum"}
    if kind == "object":
        allowed |= {"properties", "required", "additionalProperties"}
        properties, required = schema.get("properties"), schema.get("required", [])
        if not isinstance(properties, dict) or not all(isinstance(key, str) for key in properties):
            raise ValueError("object properties missing")
        if not isinstance(required, list) or not all(
            isinstance(key, str) and key in properties for key in required
        ):
            raise ValueError("invalid required fields")
        if schema.get("additionalProperties") is not False:
            raise ValueError("closed object required")
        for value in properties.values():
            _check_schema(value)
    elif kind == "array":
        allowed |= {"items", "minItems", "maxItems"}
        _check_schema(schema.get("items"))
        for key in ("minItems", "maxItems"):
            if key in schema and (type(schema[key]) is not int or schema[key] < 0):
                raise ValueError("invalid array item limit")
        if schema.get("minItems", 0) > schema.get("maxItems", math.inf):
            raise ValueError("contradictory array item limits")
    elif kind not in ("string", "number"):
        raise ValueError("unsupported contract type")
    if set(schema) - allowed:
        raise ValueError("unsupported contract keyword")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        raise ValueError("invalid enum")


def schema_identity(name: str, schema: Mapping[str, Any]) -> dict[str, str]:
    canonical = json.dumps(schema, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    return {
        "json_schema_name": name,
        "json_schema_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        "json_schema_hash_format": _SCHEMA_HASH_FORMAT,
    }


def response_format_identity(body: Mapping[str, Any]) -> dict[str, Any]:
    """Describe the final wire format without logging schema contents."""
    value = body.get("response_format")
    if value is None:
        return {"json_output_mode": "prompt_only"}
    if value == {"type": "json_object"}:
        return {"json_output_mode": "json_object"}
    if isinstance(value, dict) and value.get("type") == "json_schema":
        contract = value.get("json_schema")
        if (
            isinstance(contract, dict)
            and isinstance(contract.get("name"), str)
            and isinstance(contract.get("schema"), dict)
        ):
            return {
                "json_output_mode": "json_schema",
                **schema_identity(contract["name"], contract["schema"]),
            }
    return {"json_output_mode": "unknown"}


def prepare_output_contract(
    mode: str,
    output_schema: Mapping[str, Any] | None,
    schema_name: str | None,
) -> tuple[dict[str, Any], dict[str, Any], Schema | None]:
    if mode != "json_schema":
        parameters: dict[str, Any] = (
            {"response_format": {"type": "json_object"}} if mode == "json_object" else {}
        )
        return parameters, {**parameters, "json_output_mode": mode}, None
    policy: dict[str, Any] = {
        "json_output_mode": mode,
        "request_phase": "request_validation",
        "request_outcome_unknown": False,
    }
    try:
        if not isinstance(schema_name, str) or _SCHEMA_NAME.fullmatch(schema_name) is None:
            raise ValueError("schema name missing or invalid")
        if not isinstance(output_schema, Mapping):
            raise ValueError("output schema missing")
        # Copy only after JSON serialization succeeds; no caller mutation can
        # change a wire contract after its identity has been computed.
        schema = json.loads(json.dumps(dict(output_schema), allow_nan=False))
        _check_schema(schema)
        if schema.get("type") != "object":
            raise ValueError("root JSON object required")
        identity = schema_identity(schema_name, schema)
    except (ValueError, TypeError, RecursionError) as error:
        raise ModelServiceError(
            "请求配置错误（request_configuration_invalid）：JSON业务契约缺失或不受支持；未派发模型请求。",
            error_code="request_configuration_invalid",
            request_policy=policy,
        ) from error
    parameters = {
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "schema": schema,
                "strict": True,
            },
        }
    }
    # The policy is safe even when a caller's schema includes private enum data.
    return (
        parameters,
        {"json_output_mode": mode, "response_format": {"type": "json_schema"}, **identity},
        schema,
    )


def matches_output_contract(value: object, schema: Schema) -> bool:
    """Validate only the vocabulary checked before dispatch; never repair output."""
    kind = schema["type"]
    if kind == "object":
        if not isinstance(value, dict) or set(value) - schema["properties"].keys():
            return False
        if not set(schema.get("required", [])) <= value.keys():
            return False
        valid = all(matches_output_contract(item, schema["properties"][key]) for key, item in value.items())
    elif kind == "array":
        if not isinstance(value, list):
            return False
        valid = schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", math.inf)
        if valid:
            valid = all(matches_output_contract(item, schema["items"]) for item in value)
    elif kind == "string":
        valid = isinstance(value, str)
    else:
        valid = type(value) is int or (type(value) is float and math.isfinite(value))
    return valid and ("enum" not in schema or value in schema["enum"])
