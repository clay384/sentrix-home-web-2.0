"""Synthetic source manifests and fake HTTP verify optional visual adapters."""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
from pathlib import Path

import httpx
import pytest
from PIL import Image
from pydantic import ValidationError

from sentrix.config import Settings
from sentrix.infrastructure.media.person_vision import render_numbered_preview, render_person_crop
from sentrix.infrastructure.media.person_vision_model import OpenAIPersonVision
from sentrix.infrastructure.media.photo_inspector import OpenAIPhotoInspector
from sentrix.infrastructure.media.visual_provenance import (
    canonical_image_bytes,
    configure_visual_provenance,
    encode_visual_image,
    image_provenance,
)
from sentrix.infrastructure.media.vlm_observer import OpenAIVlmObserver, chat_json


@pytest.fixture(autouse=True)
def isolated_visual_configuration():
    configure_visual_provenance(None)
    yield
    configure_visual_provenance(None)


def image_file(path, *, size=(1200, 800), color="blue"):
    Image.new("RGB", size, color).save(path, format="JPEG")
    return path


def freeze_images(tmp_path, sources):
    manifest = {
        identity: {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for identity, path in sources.items()
    }
    path = tmp_path / "source-manifest.json"
    path.write_text(json.dumps(manifest))
    configure_visual_provenance(path)
    return manifest


def decoded(url):
    return base64.b64decode(url.split(",", 1)[1], validate=True)


@pytest.mark.parametrize(
    "size,mode,orientation,expected",
    [
        ((70, 30), "RGB", 6, (30, 70)),
        ((1200, 600), "RGBA", None, (512, 256)),
        ((20, 10), "L", None, (20, 10)),
    ],
)
def test_canonical_encoding_exif_rgb_bounded_size_without_upscale(size, mode, orientation, expected):
    image = Image.new(mode, size)
    raw = io.BytesIO()
    exif = Image.Exif()
    if orientation is not None:
        exif[274] = orientation
    image.save(raw, format="PNG", exif=exif)
    with Image.open(io.BytesIO(canonical_image_bytes(raw.getvalue()))) as encoded:
        assert encoded.size == expected and encoded.mode == "RGB" and encoded.format == "JPEG"


def test_absent_manifest_preserves_original_bytes_and_omits_extension(tmp_path, monkeypatch):
    source = tmp_path / "original.png"
    Image.new("RGBA", (900, 700)).save(source)
    url = encode_visual_image(source)
    assert url.startswith("data:image/png;base64,") and decoded(url) == source.read_bytes()
    assert image_provenance([url]) is None
    sent = []

    def post(endpoint, *, json, timeout):
        sent.append(json)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "{}"}}]}, request=httpx.Request("POST", endpoint)
        )

    monkeypatch.setattr(httpx, "post", post)
    chat_json("https://model.invalid/v1/chat/completions", Model(), "Describe", url, 180)
    assert "sentrix_image_provenance" not in sent[0]
    assert "enable_thinking" not in sent[0]
    assert sent[0]["max_tokens"] == 2048


def test_frozen_wire_proof_matches_sent_bytes_and_preserves_multi_image_order(tmp_path, monkeypatch):
    first = image_file(tmp_path / "one.jpg", color="red")
    second = image_file(tmp_path / "two.jpg", color="green")
    manifest = freeze_images(tmp_path, {"source-one": first, "source-two": second})
    urls = [encode_visual_image(second), encode_visual_image(first)]
    sent = []

    def post(endpoint, *, json, timeout):
        sent.append(json)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "{}"}}]}, request=httpx.Request("POST", endpoint)
        )

    monkeypatch.setattr(httpx, "post", post)
    assert chat_json("https://model.invalid/v1/chat/completions", Model(), "Describe", urls, 180) == {}
    body = sent[0]
    proofs = body["sentrix_image_provenance"]
    assert [proof["source_id"] for proof in proofs] == ["source-two", "source-one"]
    actual_urls = [
        part["image_url"]["url"] for part in body["messages"][0]["content"] if part["type"] == "image_url"
    ]
    assert actual_urls == urls
    for proof, url in zip(proofs, actual_urls, strict=True):
        assert proof["source_sha256"] == manifest[proof["source_id"]]["sha256"]
        assert proof["encoded_sha256"] == hashlib.sha256(decoded(url)).hexdigest()
        assert proof["policy"] == "exif-RGB-max512-no-upscale-JPEG90-v1"
        assert "path" not in proof and "file_name" not in proof


def test_unknown_original_or_changed_source_is_rejected(tmp_path):
    source = image_file(tmp_path / "original.jpg")
    freeze_images(tmp_path, {"source-one": source})
    unknown = image_file(tmp_path / "unpublished.jpg", color="red")
    with pytest.raises(ValueError, match="outside"):
        encode_visual_image(unknown)
    image_file(source, color="yellow")
    with pytest.raises(ValueError, match="outside"):
        encode_visual_image(source)


def test_equal_bytes_keep_distinct_occurrence_proofs_and_reject_ambiguous_plain_string(tmp_path):
    first = image_file(tmp_path / "source-one.jpg")
    second = tmp_path / "source-two.jpg"
    second.write_bytes(first.read_bytes())
    freeze_images(tmp_path, {"source-one": first, "source-two": second})
    aliases = []
    for source in [first, second]:
        alias = tmp_path / ("asset_random_" + source.name)
        alias.write_bytes(source.read_bytes())
        aliases.append(alias)
    urls = [encode_visual_image(path) for path in aliases]
    assert urls[0] == urls[1]
    assert [proof["source_id"] for proof in image_provenance(copy.deepcopy(urls))] == [
        "source-one",
        "source-two",
    ]
    with pytest.raises(ValueError, match="ambiguous"):
        image_provenance([str(urls[0])])


def test_existing_crop_and_numbered_preview_renderers_bind_parent_and_detect_tampering(tmp_path):
    source = image_file(tmp_path / "original.jpg")
    freeze_images(tmp_path, {"source-one": source})
    crop_path, bbox = render_person_crop(source, [200, 100, 400, 350])
    preview_path, _ = render_numbered_preview(source, [{"bbox": [200, 100, 400, 350]}])
    try:
        proofs = image_provenance(
            [encode_visual_image(Path(crop_path)), encode_visual_image(Path(preview_path))]
        )
        assert [proof["operation"] for proof in proofs] == ["crop", "numbered_preview"]
        assert {proof["source_id"] for proof in proofs} == {"source-one"}
        assert proofs[0]["parameters"] == {"bbox": bbox}
        assert proofs[1]["parameters"] == {"bboxes": [[200, 100, 400, 350]]}
        image_file(Path(crop_path), size=(40, 30), color="red")
        with pytest.raises(ValueError, match="outside"):
            encode_visual_image(Path(crop_path))
    finally:
        Path(crop_path).unlink(missing_ok=True)
        Path(preview_path).unlink(missing_ok=True)


class Model:
    def current(self):
        return "synthetic-visual-model"


@pytest.mark.parametrize("client_type", [OpenAIVlmObserver, OpenAIPhotoInspector, OpenAIPersonVision])
@pytest.mark.parametrize("thinking", [None, True, False])
def test_visual_budget_thinking_timeout_reach_every_wire_client(monkeypatch, client_type, thinking):
    sent = []

    def post(endpoint, *, json, timeout):
        sent.append((json, timeout))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "{}"}}]}, request=httpx.Request("POST", endpoint)
        )

    monkeypatch.setattr(httpx, "post", post)
    client = client_type(
        "https://model.invalid/v1", Model(), timeout_s=600, output_tokens=32768, enable_thinking=thinking
    )
    assert client._chat("Describe synthetic input", None) == {}
    body, timeout = sent[0]
    assert body["max_tokens"] == 32768 and timeout == 600
    if thinking is None:
        assert "enable_thinking" not in body
    else:
        assert body["enable_thinking"] is thinking


@pytest.mark.parametrize("client_type", [OpenAIVlmObserver, OpenAIPhotoInspector, OpenAIPersonVision])
def test_visual_clients_keep_legacy_default_wire_budget(monkeypatch, client_type):
    sent = []

    def post(endpoint, *, json, timeout):
        sent.append((json, timeout))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "{}"}}]},
            request=httpx.Request("POST", endpoint),
        )

    monkeypatch.setattr(httpx, "post", post)
    client_type("https://model.invalid/v1", Model())._chat("Describe", None)
    body, timeout = sent[0]
    assert body["max_tokens"] == 2048 and timeout == 180
    assert "enable_thinking" not in body and "sentrix_image_provenance" not in body


def test_settings_preserve_defaults_and_accept_explicit_visual_overrides(tmp_path, monkeypatch):
    for suffix in [
        "VLM_OUTPUT_TOKENS",
        "VLM_ENABLE_THINKING",
        "VLM_TIMEOUT_S",
        "VISUAL_SOURCE_MANIFEST",
        "CORPUS_SOURCE_ALLOWLIST",
    ]:
        monkeypatch.delenv("SENTRIX_" + suffix, raising=False)
    defaults = Settings(data_dir=tmp_path)
    assert defaults.vlm_output_tokens == 2048 and defaults.vlm_enable_thinking is None
    assert defaults.vlm_timeout_s == 180 and defaults.visual_source_manifest is None
    assert defaults.corpus_source_allowlist is None
    monkeypatch.setenv("SENTRIX_VLM_OUTPUT_TOKENS", "32768")
    monkeypatch.setenv("SENTRIX_VLM_ENABLE_THINKING", "true")
    monkeypatch.setenv("SENTRIX_VLM_TIMEOUT_S", "600")
    monkeypatch.setenv("SENTRIX_VISUAL_SOURCE_MANIFEST", str(tmp_path / "images.json"))
    monkeypatch.setenv("SENTRIX_CORPUS_SOURCE_ALLOWLIST", str(tmp_path / "sources.json"))
    configured = Settings(data_dir=tmp_path)
    assert configured.vlm_output_tokens == 32768 and configured.vlm_enable_thinking is True
    assert configured.vlm_timeout_s == 600
    assert configured.visual_source_manifest == tmp_path / "images.json"
    assert configured.corpus_source_allowlist == tmp_path / "sources.json"


@pytest.mark.parametrize(
    "field,value",
    [
        ("vlm_output_tokens", 255),
        ("vlm_output_tokens", 32769),
        ("chat_output_tokens", 255),
        ("agent_max_steps", 65),
    ],
)
def test_settings_reject_out_of_range_budgets(tmp_path, field, value):
    with pytest.raises(ValidationError):
        Settings(data_dir=tmp_path, **{field: value})
