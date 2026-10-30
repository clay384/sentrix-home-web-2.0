"""Inference overlap regression with synthetic encoders; no model/server load test."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest


def test_bge_inference_calls_can_overlap(monkeypatch):
    torch = pytest.importorskip("torch")
    from scripts.maintenance import bge_text_sidecar as sidecar

    entered = Barrier(2)

    def encode(**inputs):
        entered.wait(timeout=5)
        return SimpleNamespace(last_hidden_state=torch.ones((1, 2, 1024)))

    monkeypatch.setattr(sidecar, "_model", encode)
    monkeypatch.setattr(
        sidecar,
        "_tokenizer",
        lambda *args, **kwargs: {
            "input_ids": torch.ones((1, 2), dtype=torch.int64),
            "attention_mask": torch.ones((1, 2), dtype=torch.int64),
        },
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(sidecar._embed, ["first", "second"]))
    assert results[0] == results[1]
    assert len(results[0]) == 1024
    assert sum(v * v for v in results[0]) == pytest.approx(1.0)


def test_clip_text_inference_calls_can_overlap(monkeypatch):
    torch = pytest.importorskip("torch")
    clip = pytest.importorskip("cn_clip.clip")
    from scripts.maintenance import legacy_clip_sidecar as sidecar

    entered = Barrier(2)

    def encode(tokens):
        entered.wait(timeout=5)
        return torch.ones((1, 768))

    monkeypatch.setattr(sidecar, "_torch_model", SimpleNamespace(encode_text=encode))
    monkeypatch.setattr(sidecar, "_device", "cpu")
    monkeypatch.setattr(clip, "tokenize", lambda *args, **kwargs: torch.ones((1, 52), dtype=torch.int64))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(sidecar._embed_query_torch, ["first", "second"]))
    assert results[0] == results[1]
    assert len(results[0]) == 768
    assert sum(v * v for v in results[0]) == pytest.approx(1.0)
