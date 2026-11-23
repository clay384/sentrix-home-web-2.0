"""Q1 additions verified without cloud calls; Q0's own contracts remain unchanged."""

import hashlib
import tempfile
import unittest
from pathlib import Path

from .contract_fixtures import PNG, FakeModel, NativeSearchFixture, ref
from .contract_fixtures import reply as q0_reply
from .core import AgentConfig, CommonAgent
from .sources import SourceError, SourceStore


def reply(*calls):
    normalized = []
    for name, args in calls:
        if name == "search_memories":
            args = {"missing_field": "requested detail", "changed_constraint": "test query variation", **args}
        normalized.append((name, args))
    return q0_reply(*normalized)


def slot(status="unknown", basis="source_text", refs=None):
    return (
        "update_evidence",
        {
            "slots": [
                {"field": "requested detail", "status": status, "basis": basis, "source_refs": refs or []}
            ]
        },
    )


class Q1Contracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        path = self.root / "x.png"
        path.write_bytes(PNG)
        self.sources = SourceStore(
            {
                "mail": {
                    "source_id": "mail",
                    "parent_id": "mail",
                    "modality": "email",
                    "detail": "Reservation ABC-73",
                },
                "image": {
                    "source_id": "image",
                    "parent_id": "image",
                    "modality": "image",
                    "asset_path": str(path),
                    "source_sha256": hashlib.sha256(PNG).hexdigest(),
                },
            }
        )

    def tearDown(self):
        self.tmp.cleanup()

    def run_agent(self, responses, ids):
        self.model = FakeModel(responses)
        self.agent = CommonAgent(NativeSearchFixture(ids), self.sources, self.model)
        return self.agent.run(
            "synthetic", "Synthetic question", attempt_id="offline", trace_root=self.root / "trace"
        )

    def test_32_total_decisions_with_empty_queries_no_soft_stop(self):
        responses = [reply(slot(), ("search_memories", {"query": "first"}))]
        responses += [reply(("search_memories", {"query": "missing " + str(i)})) for i in range(30)]
        responses += [
            reply(("final_answer", {"answer": "Unknown", "source_refs": [], "selected_source_refs": []}))
        ]
        r = self.run_agent(responses, [])
        self.assertEqual(r["decision_rounds"], 32)
        self.assertEqual(r["status"], "completed")
        self.assertTrue(r["first_search_empty"])

    def test_visual_final_blocked_until_original_payload_delivered(self):
        responses = [
            reply(slot(), ("search_memories", {"query": "image"})),
            reply(
                ("final_answer", {"answer": "red", "source_refs": [ref("image")], "selected_source_refs": []})
            ),
            reply(("inspect_images", {"source_refs": [ref("image")], "question": "color"})),
            reply(slot("supported", "pixels", [ref("image")])),
            reply(
                ("final_answer", {"answer": "red", "source_refs": [ref("image")], "selected_source_refs": []})
            ),
        ]
        r = self.run_agent(responses, ["image"])
        self.assertEqual(r["status"], "completed")
        self.assertEqual(r["inspected_source_ids"], ["image"])
        self.assertTrue(any("original_pixels" in x["message"] for x in r["tool_errors"]))
        self.assertTrue(
            any(isinstance(m.get("content"), list) for m in self.model.requests[3][0]["messages"])
        )

    def test_cannot_mark_pixels_supported_before_delivery(self):
        agent = CommonAgent(NativeSearchFixture([]), self.sources, FakeModel([]))
        agent.evidence_slots = {}
        agent.delivered_pixels = set()
        with self.assertRaises(SourceError):
            agent._tool(*slot("supported", "pixels", [ref("image")]), {ref("image"): "image"})

    def test_exact_email_read_then_slot_and_final(self):
        responses = [
            reply(slot(), ("search_memories", {"query": "ABC-73"})),
            reply(
                ("read_source_text", {"source_ref": ref("mail")}),
                slot("supported", "source_text", [ref("mail")]),
            ),
            reply(
                (
                    "final_answer",
                    {"answer": "ABC-73", "source_refs": [ref("mail")], "selected_source_refs": []},
                )
            ),
        ]
        r = self.run_agent(responses, ["mail"])
        self.assertEqual(r["status"], "completed")
        self.assertEqual(r["first_retrieval_source_ids"], ["mail"])

    def test_unknown_sources_never_opened(self):
        agent = CommonAgent(NativeSearchFixture([]), self.sources, FakeModel([]))
        with self.assertRaises(SourceError):
            agent._tool("read_source_text", {"source_ref": ref("mail")}, {})

    def test_video_lineage_page_authorization_and_parent_scoring(self):
        self.sources.sources["video"] = {"source_id": "video", "parent_id": "video", "modality": "video"}
        for i in range(3):
            self.sources.sources["frame" + str(i)] = {
                **self.sources.sources["image"],
                "source_id": "frame" + str(i),
                "parent_id": "video",
                "source_timestamp_sec": i * 1.25,
            }
        responses = [
            reply(slot(), ("search_memories", {"query": "video"})),
            reply(("list_video_frames", {"source_ref": ref("video"), "limit": 2})),
            reply(("list_video_frames", {"source_ref": ref("frame0"), "offset": 2, "limit": 2})),
            reply(("inspect_images", {"source_refs": [ref("frame2")], "question": "what is visible"})),
            reply(slot("supported", "pixels", [ref("frame2")])),
            reply(
                (
                    "final_answer",
                    {
                        "answer": "Limited to this selected frame.",
                        "source_refs": [ref("frame2")],
                        "selected_source_refs": [ref("video")],
                    },
                )
            ),
        ]
        r = self.run_agent(responses, ["video"])
        self.assertEqual(r["status"], "completed")
        self.assertEqual(r["source_ids"], ["video"])
        self.assertEqual(r["first_retrieval_source_ids"], ["video"])
        self.assertEqual(r["retrieved_source_ids"], ["video"])
        self.assertEqual(r["inspected_source_ids"], ["frame2"])
        self.assertFalse(self.sources.image("frame2")["original"])

    def test_video_browse_refuses_unreturned_parent(self):
        agent = CommonAgent(NativeSearchFixture([]), self.sources, FakeModel([]))
        with self.assertRaises(SourceError):
            agent._tool("list_video_frames", {"source_ref": "invented"}, {})
        with self.assertRaises(SourceError):
            self.sources.video_frames("image", 0, 20)

    def test_query_rewrite_reason_is_runtime_required(self):
        agent = CommonAgent(NativeSearchFixture([]), self.sources, FakeModel([]))
        with self.assertRaises(SourceError):
            agent._tool("search_memories", {"query": "missing reason"}, {})

    def test_first_search_preserves_original_question_and_records_actual_query(self):
        class Planned(NativeSearchFixture):
            def search_plan(self, query, top_k, **kwargs):
                self.plan = (query, kwargs)
                result = self.search(query, top_k)
                result.response["queries"] = [query] + kwargs["queries"]
                return result

        adapter = Planned(["mail"])
        model = FakeModel(
            [
                reply(slot(), ("search_memories", {"query": "ABC-73"})),
                reply(
                    (
                        "final_answer",
                        {"answer": "Insufficient evidence", "source_refs": [], "selected_source_refs": []},
                    )
                ),
            ]
        )
        result = CommonAgent(adapter, self.sources, model).run(
            "synth", "What is the reservation reference?", attempt_id="a", trace_root=self.root / "trace"
        )
        self.assertEqual(adapter.plan[0], "What is the reservation reference?")
        self.assertEqual(adapter.plan[1]["queries"], ["ABC-73"])
        self.assertEqual(result["first_search_query"], "What is the reservation reference?")

    def test_eight_image_payload_and_delivery_no_six_source_cap(self):
        ids = ["visual" + str(i) for i in range(8)]
        for sid in ids:
            self.sources.sources[sid] = {**self.sources.sources["image"], "source_id": sid, "parent_id": sid}
        refs = [ref(sid) for sid in ids]
        r = self.run_agent(
            [
                reply(slot(), ("search_memories", {"query": "visuals"})),
                reply(("inspect_images", {"source_refs": refs, "question": "compare"})),
                reply(slot("supported", "pixels", refs)),
                reply(
                    (
                        "final_answer",
                        {
                            "answer": "Compared selected images.",
                            "source_refs": refs,
                            "selected_source_refs": refs,
                        },
                    )
                ),
            ],
            ids,
        )
        self.assertEqual(r["status"], "completed")
        self.assertEqual(len(r["selected_source_ids"]), 8)
        image_parts = [
            part
            for m in self.model.requests[2][0]["messages"]
            if isinstance(m.get("content"), list)
            for part in m["content"]
            if part.get("type") == "image_url"
        ]
        self.assertEqual(len(image_parts), 8)

    def test_config_frozen_max_output_and_thinking(self):
        self.assertEqual(AgentConfig().max_output_tokens, 131072)
        with self.assertRaises(ValueError):
            AgentConfig(enable_thinking=False)
        with self.assertRaises(ValueError):
            AgentConfig(max_decisions=33)


if __name__ == "__main__":
    unittest.main()
