import pytest
from fastapi.testclient import TestClient


def test_source_identity_metadata_roundtrip_and_conflict(client: TestClient):
    fields = {
        "scope_id": "mixed-documents",
        "external_source_id": "neutral-source-1",
        "source_timestamp": "2022-04-30 13:22:12",
        "source_location": "Cambridge",
        "source_metadata": '{"ordinal":7}',
    }
    response = client.post(
        "/api/v1/media/import", files={"file": ("one.txt", b"Original mail text", "text/plain")}, data=fields
    )
    assert response.status_code == 201
    asset_id = response.json()["asset_id"]
    asset = client.get("/api/v1/assets/" + asset_id).json()
    assert asset["external_source_id"] == "neutral-source-1"
    assert asset["source_timestamp"] == "2022-04-30 13:22:12"
    assert asset["captured_at"] == "2022-04-30T13:22:12"
    assert asset["source_location"] == "Cambridge"
    assert asset["source_metadata"] == {"ordinal": 7}
    repeated = client.post(
        "/api/v1/media/import", files={"file": ("one.txt", b"Original mail text", "text/plain")}, data=fields
    )
    assert repeated.status_code == 201 and repeated.json()["asset_id"] == asset_id
    conflict = client.post(
        "/api/v1/media/import", files={"file": ("one.txt", b"Changed text", "text/plain")}, data=fields
    )
    assert conflict.status_code == 409
    distinct = client.post(
        "/api/v1/media/import",
        files={"file": ("one.txt", b"Original mail text", "text/plain")},
        data={**fields, "external_source_id": "neutral-source-2"},
    )
    assert distinct.status_code == 201 and distinct.json()["asset_id"] != asset_id


@pytest.mark.parametrize(
    "fields",
    [
        {"source_timestamp": "2022-04-30T13:22:12+08:00"},
        {"source_metadata": "[]"},
        {"source_metadata": "invalid json"},
        {"source_metadata": '{"not_finite":NaN}'},
    ],
)
def test_invalid_source_metadata_rejected_without_upload(client: TestClient, fields):
    response = client.post(
        "/api/v1/media/import", files={"file": ("one.txt", b"content", "text/plain")}, data=fields
    )
    assert response.status_code == 400
    assert client.get("/api/v1/assets").json()["items"] == []
