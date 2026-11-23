"""Exercise Q1's real loop and source tools with offline model responses."""

import hashlib
import tempfile
import unittest
from pathlib import Path

from .contract_fixtures import PNG, FakeModel, NativeSearchFixture, ref
from .core import TOOLS, CommonAgent
from .sources import SourceStore
from .test_contracts import reply, slot


def plain_answer():
    return {
        "choices": [
            {"message": {"role": "assistant", "content": "A provisional answer."}, "finish_reason": "stop"}
        ],
        "usage": {},
    }


def final(source="image"):
    return reply(
        (
            "final_answer",
            {
                "answer": "Supported answer.",
                "source_refs": [ref(source)] if source else [],
                "selected_source_refs": [],
            },
        )
    )


class FinalContractRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        image = self.root / "image.png"
        image.write_bytes(PNG)
        self.sources = SourceStore(
            {
                "image": {
                    "source_id": "image",
                    "parent_id": "image",
                    "modality": "image",
                    "asset_path": str(image),
                    "source_sha256": hashlib.sha256(PNG).hexdigest(),
                },
                "mail": {
                    "source_id": "mail",
                    "parent_id": "mail",
                    "modality": "email",
                    "detail": "Reservation ABC-73",
                },
            }
        )

    def tearDown(self):
        self.tmp.cleanup()

    def run_agent(self, responses, ids, name="trace"):
        model = FakeModel(responses)
        agent = CommonAgent(NativeSearchFixture(ids), self.sources, model)
        result = agent.run(
            "synthetic", "Synthetic question", attempt_id="offline", trace_root=self.root / name
        )
        return result, model

    def assert_final_only(self, model, decision):
        request = model.requests[decision - 1][0]
        self.assertEqual([t["function"]["name"] for t in request["tools"]], ["final_answer"])
        self.assertEqual(request["tool_choice"], {"type": "function", "function": {"name": "final_answer"}})

    def assert_full_tools(self, model, decision):
        request = model.requests[decision - 1][0]
        self.assertEqual(request["tools"], TOOLS)
        self.assertEqual(request["tool_choice"], "auto")

    def test_plain_answer_then_failed_final_can_inspect_and_submit(self):
        responses = [
            reply(slot(), ("search_memories", {"query": "image"})),
            plain_answer(),
            final(),
            reply(("inspect_images", {"source_refs": [ref("image")], "question": "color"})),
            reply(slot("supported", "pixels", [ref("image")])),
            final(),
        ]
        result, model = self.run_agent(responses, ["image"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["decision_rounds"], 6)
        self.assertEqual(result["inspected_source_ids"], ["image"])
        self.assertEqual(result["source_ids"], ["image"])
        self.assertEqual(len(result["tool_errors"]), 1)
        self.assertEqual(
            result["tool_errors"][0]["message"],
            "cited_visual_source_requires_original_pixels_or_explicit_metadata_basis",
        )
        self.assert_final_only(model, 3)
        self.assert_full_tools(model, 4)
        parts = [
            p
            for m in model.requests[4][0]["messages"]
            if isinstance(m.get("content"), list)
            for p in m["content"]
            if p.get("type") == "image_url"
        ]
        self.assertEqual(len(parts), 1)
        self.assertTrue(parts[0]["image_url"]["url"].startswith("data:image/"))

    def test_plain_answer_then_video_contract_can_browse_and_inspect_frame(self):
        self.sources.sources["video"] = {"source_id": "video", "parent_id": "video", "modality": "video"}
        self.sources.sources["frame"] = {
            **self.sources.sources["image"],
            "source_id": "frame",
            "parent_id": "video",
            "source_timestamp_sec": 1.25,
        }
        responses = [
            reply(slot(), ("search_memories", {"query": "video"})),
            plain_answer(),
            final("video"),
            reply(("list_video_frames", {"source_ref": ref("video")})),
            reply(("inspect_images", {"source_refs": [ref("frame")], "question": "scene"})),
            reply(slot("supported", "pixels", [ref("frame")])),
            final("video"),
        ]
        result, model = self.run_agent(responses, ["video"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["decision_rounds"], 7)
        self.assertEqual(result["source_ids"], ["video"])
        self.assertEqual(result["inspected_source_ids"], ["frame"])
        self.assert_final_only(model, 3)
        self.assert_full_tools(model, 4)

    def test_other_repairable_final_contracts_restore_tools(self):
        malformed_json = final("mail")
        malformed_json["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "{"
        cases = [
            (
                "missing_slots",
                reply(("search_memories", {"query": "mail"})),
                final("mail"),
                "update_evidence_slots_before_final_answer",
            ),
            (
                "unknown_ref",
                reply(slot(), ("search_memories", {"query": "mail"})),
                final("unreturned"),
                "source_ref_was_not_returned_by_framework",
            ),
            ("malformed_json", reply(slot(), ("search_memories", {"query": "mail"})), malformed_json, None),
            (
                "missing_answer",
                reply(slot(), ("search_memories", {"query": "mail"})),
                reply(("final_answer", {"source_refs": [], "selected_source_refs": []})),
                None,
            ),
        ]
        for name, first, invalid, error in cases:
            with self.subTest(name=name):
                responses = [
                    first,
                    plain_answer(),
                    invalid,
                    reply(
                        ("read_source_text", {"source_ref": ref("mail")}),
                        slot("supported", "source_text", [ref("mail")]),
                    ),
                    final("mail"),
                ]
                result, model = self.run_agent(responses, ["mail"], name)
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["decision_rounds"], 5)
                self.assertEqual(len(result["tool_errors"]), 1)
                self.assertFalse(result["tool_errors"][0]["operational"])
                if error:
                    self.assertEqual(result["tool_errors"][0]["message"], error)
                self.assert_final_only(model, 3)
                self.assert_full_tools(model, 4)

    def test_round_32_remains_final_only_after_round_31_contract_error(self):
        responses = [reply(slot(), ("search_memories", {"query": "image"}))]
        responses += [reply(slot()) for _ in range(28)]
        responses += [
            plain_answer(),
            final(),
            reply(("inspect_images", {"source_refs": [ref("image")], "question": "color"})),
        ]
        result, model = self.run_agent(responses, ["image"])
        self.assertEqual(result["status"], "decision_budget_exhausted")
        self.assertEqual(result["decision_rounds"], 32)
        self.assertEqual(len(model.requests), 32)
        self.assert_final_only(model, 31)
        self.assert_final_only(model, 32)
        self.assertEqual(result["inspected_source_ids"], [])
        self.assertEqual(result["tool_errors"][-1]["message"], "decision_budget_only_final_answer_allowed")

    def test_recovery_does_not_suppress_source_integrity_failure(self):
        self.sources.sources["image"]["source_sha256"] = "0" * 64
        responses = [
            reply(slot(), ("search_memories", {"query": "image"})),
            plain_answer(),
            final(),
            reply(("inspect_images", {"source_refs": [ref("image")], "question": "color"})),
        ]
        result, model = self.run_agent(responses, ["image"])
        self.assertEqual(result["status"], "operational_error")
        self.assertEqual(result["decision_rounds"], 4)
        self.assertEqual(len(model.requests), 4)
        self.assertEqual(result["tool_errors"][-1]["message"], "image_sha256_mismatch")
        self.assertTrue(result["tool_errors"][-1]["operational"])

    def test_successful_format_conversion_keeps_original_final_only_behavior(self):
        result, model = self.run_agent(
            [reply(slot(), ("search_memories", {"query": "mail"})), plain_answer(), final("mail")], ["mail"]
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["decision_rounds"], 3)
        self.assertEqual(result["tool_errors"], [])
        self.assert_final_only(model, 3)


if __name__ == "__main__":
    unittest.main()
