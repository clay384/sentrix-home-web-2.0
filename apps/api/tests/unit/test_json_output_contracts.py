"""Existing business shapes are separate; native response budgets are unchanged."""

import copy
import json

import pytest

from sentrix.domain.assistant.ports import ModelServiceError
from sentrix.infrastructure.media.json_output_contracts import (
    APPEARANCE_SCHEMA,
    INSPECTION_SCHEMA,
    MOMENTS_SCHEMA,
    OBSERVATION_SCHEMA,
    PORTRAIT_SCHEMA,
    matches_output_contract,
    prepare_output_contract,
    schema_identity,
)


@pytest.mark.parametrize(
    "schema", [OBSERVATION_SCHEMA, PORTRAIT_SCHEMA, MOMENTS_SCHEMA, APPEARANCE_SCHEMA, INSPECTION_SCHEMA]
)
def test_builtin_contracts_are_valid_closed_shapes_without_token_or_string_budgets(schema):
    wire, policy, validated = prepare_output_contract("json_schema", schema, "business_v1")
    assert wire["response_format"]["json_schema"]["strict"] is True
    assert validated == schema and validated is not schema
    assert policy["json_output_mode"] == "json_schema"
    assert "schema" not in policy["response_format"]
    text = json.dumps(wire)
    assert "maxLength" not in text and "minLength" not in text
    assert "max_tokens" not in text and "max_completion_tokens" not in text


def test_existing_observation_list_limit_is_enforced_without_truncating_output():
    value = {
        "caption": "中文",
        "activity": "",
        "place": "",
        "event_type": "",
        "ocr_text": "",
        "objects": ["同类物品"] * 40,
        "clothing": [],
        "facts": [],
    }
    assert matches_output_contract(value, OBSERVATION_SCHEMA)
    value["objects"].append("第41项")
    before = copy.deepcopy(value)
    assert not matches_output_contract(value, OBSERVATION_SCHEMA)
    assert value == before


def test_appearance_and_moments_do_not_inherit_observer_or_portrait_input_caps():
    assert matches_output_contract({"clothing": ["衣物"] * 50, "confidence": 0.8}, APPEARANCE_SCHEMA)
    moment = {
        "label": "P1",
        "action_text": "活动",
        "interaction_labels": [],
        "interaction_text": "",
        "participation_style": "",
        "visible_affect": "",
        "social_role_cues": [],
        "narrative_note": "",
        "confidence": 0.9,
    }
    assert matches_output_contract({"moments": [moment] * 50}, MOMENTS_SCHEMA)
    assert "maxItems" not in json.dumps(APPEARANCE_SCHEMA)
    assert "maxItems" not in json.dumps(MOMENTS_SCHEMA)


def test_schema_identity_is_order_independent_but_contract_specific():
    reversed_schema = dict(reversed(list(OBSERVATION_SCHEMA.items())))
    assert schema_identity("observation", reversed_schema) == schema_identity(
        "observation", OBSERVATION_SCHEMA
    )
    assert (
        schema_identity("observation", OBSERVATION_SCHEMA)["json_schema_sha256"]
        != schema_identity("appearance", APPEARANCE_SCHEMA)["json_schema_sha256"]
    )


@pytest.mark.parametrize(
    "schema,name",
    [
        (None, "x"),
        (INSPECTION_SCHEMA, None),
        (
            {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
                "$ref": "https://private.test",
            },
            "x",
        ),
    ],
)
def test_missing_or_unsupported_contract_is_known_configuration_error(schema, name):
    with pytest.raises(ModelServiceError) as caught:
        prepare_output_contract("json_schema", schema, name)
    assert caught.value.error_code == "request_configuration_invalid"
    assert caught.value.request_policy["request_outcome_unknown"] is False
    assert caught.value.request_policy["request_phase"] == "request_validation"
    assert "private.test" not in str(caught.value)


@pytest.mark.parametrize("mode", ["json_object", "prompt_only"])
def test_legacy_modes_do_not_apply_schema_validation(mode):
    _, policy, contract = prepare_output_contract(mode, None, None)
    assert contract is None and policy["json_output_mode"] == mode
