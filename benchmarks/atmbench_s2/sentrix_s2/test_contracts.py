"""Offline retrieval verification. Model fixtures test wiring only, never semantic quality."""

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import numpy as np

from .export_native import export
from .index import SearchIndex, build
from .models import LocalModels, OperationalError
from .text import chunks, preview, tokens


class FixtureModels:
    def __init__(self):
        self.calls = []

    def embed(self, texts, input_type):
        self.calls.append({"input_type": input_type, "count": len(texts)})
        result = []
        for text in texts:
            vector = np.zeros(2048, dtype=np.float32)
            for token in tokens(text):
                vector[int(hashlib.sha256(token.encode()).hexdigest()[:8], 16) % 2048] += 1
            vector /= max(float(np.linalg.norm(vector)), 1)
            result.append(vector.tolist())
        return result

    def rerank(self, query, documents):
        self.calls.append({"rerank_count": len(documents)})
        q = set(tokens(query))
        return sorted(
            [
                {"index": i, "relevance_score": len(q & set(tokens(t))) / max(len(q), 1)}
                for i, t in enumerate(documents)
            ],
            key=lambda x: -x["relevance_score"],
        )


class RetrievalContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        snapshot = self.root / "snapshot"
        snapshot.mkdir()
        rows = [
            (
                "mail-long",
                "email",
                "Routine notes. " * 900
                + "\nBooking ZX-927 was cancelled; new date 2027-04-18 at Willow Hall.\n退款金额 88.40 元，原订单不再有效。",
            ),
            ("mail-noise", "email", "Booking references in unrelated old mail."),
            ("cn", "email", "订单 ZX-123 改期至下周，在博物馆东门集合。"),
            ("frame-a", "image", "A striped red umbrella at the museum."),
            ("frame-b", "image", "The same red umbrella opens near a doorway."),
        ]
        values = []
        for sid, modality, text in rows:
            values.append(
                {
                    "source_id": sid,
                    "parent_id": "video-1" if sid.startswith("frame") else sid,
                    "modality": modality,
                    "text": text,
                    "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "timestamp": "2026-01-02",
                    "timestamp_role": "published_record_timestamp",
                    "native_asset_id": "native_" + sid,
                }
            )
        (snapshot / "sources.jsonl").write_text(
            "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in values)
        )
        (snapshot / "native-vectors.jsonl").write_text("")
        (snapshot / "manifest.json").write_text("{}")
        self.models = FixtureModels()
        self.db = self.root / "index.db"
        build(snapshot, self.db, self.models)
        self.index = SearchIndex(self.db, self.models)

    def tearDown(self):
        self.tmp.cleanup()

    def test_exact_tail_hit_and_lossless_offsets(self):
        result = self.index.search("ZX-927", mode="bm25")
        self.assertEqual(result["sources"][0]["source_id"], "mail-long")
        hit = result["sources"][0]
        self.assertIn("ZX-927", hit["preview"]["text"])
        self.assertGreater(hit["preview"]["offset"], 300)
        for span in hit["matched_spans"]:
            self.assertEqual(
                span["text"], self.index.sources[span["source_id"]]["text"][span["offset"] : span["end"]]
            )

    def test_true_bm25_scoring_logged_not_overlap_count(self):
        result = self.index.search("Booking", mode="bm25")
        hits = result["trace"]["channels"][0]["hits"]
        self.assertTrue(all(h["score"] < 0 for h in hits))
        self.assertGreater(len({h["score"] for h in hits}), 1)

    def test_chinese_and_short_numbers_preserved(self):
        self.assertIn("88.40", tokens("退款88.40元"))
        result = self.index.search("博物馆东门", mode="bm25")
        self.assertEqual(result["sources"][0]["source_id"], "cn")

    def test_parent_dedup_and_no_cross_source_read_authorization(self):
        r = self.index.search("red umbrella", mode="hybrid")
        ids = [x["source_id"] for x in r["sources"]]
        self.assertEqual(ids.count("video-1"), 1)
        card = next(x for x in r["sources"] if x["source_id"] == "video-1")
        self.assertTrue(set(card["matched_source_ids"]).issubset({"frame-a", "frame-b"}))

    def test_soft_date_hints_never_remove_but_hard_filter_does(self):
        soft = self.index.search("ZX-927", mode="bm25", constraints={"source_time_after": "2030-01-01"})
        hard = self.index.search(
            "ZX-927", mode="bm25", constraints={"source_time_after": "2030-01-01", "hard": True}
        )
        self.assertTrue(soft["sources"])
        self.assertEqual(hard["sources"], [])

    def test_queries_pagination_and_query_document_instructions(self):
        result = self.index.search("booking", queries=["ZX-927", "改期"], top_k=1)
        self.assertEqual(len(result["sources"]), 1)
        self.assertEqual(len(result["queries"]), 3)
        self.assertTrue(any(x.get("input_type") == "query" for x in self.models.calls))
        self.assertTrue(any(x.get("input_type") == "document" for x in self.models.calls))
        later = self.index.search("booking", queries=["ZX-927", "改期"], top_k=1, offset=1)
        self.assertEqual(result["result_set_id"], later["result_set_id"])
        self.assertNotEqual(result["sources"][0]["source_id"], later["sources"][0]["source_id"])

    def test_failed_reranker_is_operational_not_zero_scores(self):
        class Broken(FixtureModels):
            def rerank(self, *args):
                raise OperationalError("reranker_unavailable")

        with self.assertRaises(OperationalError):
            SearchIndex(self.db, Broken()).search("booking")

    def test_no_silent_query_truncation_or_overwrite(self):
        with self.assertRaises(ValueError):
            self.index.search("x" * 201)
        with self.assertRaises(ValueError):
            build(self.root / "snapshot", self.db, self.models)
        for constraint in [
            {"hard": "false"},
            {"source_time_after": "2026-02-30"},
            {"modality": "photo"},
            {"source_time_after": "2030-01-01", "source_time_before": "2029-01-01"},
        ]:
            with self.assertRaises(ValueError):
                self.index.search("x", constraints=constraint)

    def test_chunk_boundaries_cover_tail_and_preview(self):
        text = "说明。" * 4000 + "\nreference END-73"
        spans = chunks(text)
        self.assertGreater(len(spans), 1)
        self.assertEqual(spans[0][0], 0)
        self.assertEqual(spans[-1][1], len(text))
        self.assertIn("END-73", preview(text, "END-73")["text"])

    def test_interrupted_build_resumes_only_missing_vectors(self):
        class InterruptAfterOne(FixtureModels):
            def embed(self, texts, input_type):
                if self.calls:
                    raise OperationalError("intentional_offline_interruption")
                return super().embed(texts, input_type)

        target = self.root / "resume.db"
        with self.assertRaises(OperationalError):
            build(self.root / "snapshot", target, InterruptAfterOne())
        with self.assertRaises(ValueError):
            SearchIndex(target, FixtureModels())
        recovered = FixtureModels()
        result = build(self.root / "snapshot", target, recovered, resume=True)
        self.assertEqual(result["span_count"], len(self.index.spans))
        self.assertEqual(sum(x.get("count", 0) for x in recovered.calls), len(self.index.spans) - 8)
        no_calls = FixtureModels()
        again = build(self.root / "snapshot", target, no_calls, resume=True)
        self.assertTrue(again["already_ready"])
        self.assertEqual(no_calls.calls, [])

    def test_video_parent_receipt_and_frame_mapping_export(self):
        root = self.root / "native"
        data = root / "runtime/sentrix-s1/data"
        data.mkdir(parents=True)
        public = root / "assets/protocol/ingest"
        public.mkdir(parents=True)
        records = [
            {"source_id": "video", "parent_id": "video", "modality": "video", "source_sha256": "video-hash"},
            {
                "source_id": "frame",
                "parent_id": "video",
                "modality": "image",
                "source_timestamp_sec": 1.25,
                "source_sha256": "frame-hash",
                "parent_source_sha256": "video-hash",
            },
            {"source_id": "mail", "parent_id": "mail", "modality": "email", "detail": "Published text"},
        ]
        (public / "sources.jsonl").write_text("".join(json.dumps(x) + "\n" for x in records))
        db = sqlite3.connect(data / "sentrix.db")
        db.executescript(
            "CREATE TABLE assets(id,scope_id,media_type,content_sha256,parent_asset_id,source_timestamp_sec); CREATE TABLE observations(scope_id,asset_id,id,caption,source_text,source_type,place,activity,event_type,ocr_text,objects_json,clothing_json,people_json,facts_json,detail_json,confidence,revision); CREATE TABLE event_observations(event_id,observation_id); CREATE TABLE memory_vectors(scope_id,model_name); CREATE TABLE text_vectors(scope_id,model_name);"
        )
        db.executemany(
            "INSERT INTO assets VALUES(?,?,?,?,?,?)",
            [
                ("av", "pilot", "video", "video-hash", None, None),
                ("af", "pilot", "image", "frame-hash", "av", 1.25),
                ("am", "pilot", "text", "mail-hash", None, None),
            ],
        )
        db.executemany(
            "INSERT INTO observations VALUES(" + ",".join("?" * 17) + ")",
            [
                ("pilot", "av", "ov", "", "", "import", "", "", "", "", "[]", "[]", "[]", "[]", "{}", 0, 1),
                (
                    "pilot",
                    "af",
                    "of",
                    "A registered frame.",
                    "",
                    "import",
                    "",
                    "",
                    "",
                    "",
                    "[]",
                    "[]",
                    "[]",
                    "[]",
                    "{}",
                    0.8,
                    1,
                ),
                (
                    "pilot",
                    "am",
                    "om",
                    "",
                    "Published text",
                    "text_import",
                    "",
                    "",
                    "",
                    "",
                    "[]",
                    "[]",
                    "[]",
                    "[]",
                    "{}",
                    0.8,
                    1,
                ),
            ],
        )
        db.commit()
        db.close()
        receipts = root / "results/ingest/pilot/receipts"
        receipts.mkdir(parents=True)
        (receipts / "video.json").write_text(
            json.dumps(
                {
                    "source_id": "video",
                    "result": {"receipt": {"asset_id": "av"}, "derived_source_map": {"af": "frame"}},
                }
            )
        )
        (receipts / "mail.json").write_text(
            json.dumps({"source_id": "mail", "result": {"receipt": {"asset_id": "am"}}})
        )
        (receipts.parent / "native-source-map.json").write_text(
            json.dumps({"av": "video", "af": "frame", "am": "mail"})
        )
        out = self.root / "exported"
        manifest = export(root, "pilot", out, expected_parent_count=2)
        self.assertEqual(manifest["source_count"], 3)
        self.assertEqual(manifest["parent_count"], 2)
        self.assertEqual(manifest["metadata_only_video_parents"], 1)
        exported = {
            r["source_id"]: r for r in map(json.loads, (out / "sources.jsonl").read_text().splitlines())
        }
        self.assertEqual(exported["video"]["native_observation_id"], "ov")
        self.assertEqual(exported["video"]["text"], "")
        self.assertFalse(exported["video"]["index_eligible"])
        self.assertFalse(any(x.endswith(("-wal", "-shm")) for x in manifest["files"]))
        with self.assertRaises(ValueError):
            export(root, "atm-main-s1-v1", self.root / "blocked-main")

    def test_reranker_rejects_duplicate_indices(self):
        model = LocalModels()
        model.post = lambda *args: [
            {"index": 0, "relevance_score": 0.9},
            {"index": 0, "relevance_score": 0.8},
        ]
        with self.assertRaises(OperationalError):
            model.rerank("x", ["a", "b"])


if __name__ == "__main__":
    unittest.main()
