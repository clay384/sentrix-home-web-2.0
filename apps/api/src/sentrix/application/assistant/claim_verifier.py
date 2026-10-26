"""L3 claim-level 核验的调用侧。一次模型调用，失败按 ambiguous 放行。"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sentrix.application.assistant.debug_trace import (
    chat_messages_payload,
    model_reply_payload,
)
from sentrix.domain.assistant.claim_verifier import (
    Claim,
    ClaimReview,
    ClaimVerdict,
    build_claim_messages,
    parse_claim_review,
)
from sentrix.domain.assistant.evidence_binding import Fact
from sentrix.domain.assistant.evidence_ledger import EvidenceRequirement
from sentrix.domain.assistant.models import ChatMessage, ChatRole
from sentrix.domain.assistant.ports import ChatModel

log = logging.getLogger("sentrix.assistant.claim_verifier")


class ClaimVerifier:
    def __init__(self, model: ChatModel) -> None:
        self._model = model

    def review(
        self,
        *,
        query: str,
        claim: Claim,
        facts: Sequence[Fact],
        requirement: EvidenceRequirement | None,
        include_debug: bool = False,
        step_id: str = "claim_verifier",
    ) -> ClaimReview:
        raw_messages = build_claim_messages(query=query, claim=claim, facts=facts, requirement=requirement)
        messages = [ChatMessage(ChatRole(str(item["role"])), str(item["content"])) for item in raw_messages]
        started = time.perf_counter()
        debug_call: dict[str, Any] | None = None
        if include_debug:
            debug_call = {
                "step_id": step_id,
                "call_type": "claim_verifier",
                "attempt": 1,
                "started_at": datetime.now(UTC).isoformat(),
                "status": "error",
                "messages": chat_messages_payload(messages),
                "tools": [],
                "response": None,
            }
        try:
            reply = self._model.complete(messages, ())
        except Exception:
            log.warning("L3 claim 核验调用失败，按 ambiguous 放行", exc_info=True)
            return ClaimReview(claim=claim, debug_call=debug_call)
        if debug_call is not None:
            debug_call.update(
                status="complete",
                prompt_tokens=reply.prompt_tokens,
                completion_tokens=reply.completion_tokens,
                finish_reason=reply.finish_reason,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
                response=model_reply_payload(reply),
            )
        verdict, reason = parse_claim_review(reply.content or "")
        return ClaimReview(verdict=verdict, reason=reason, claim=claim, debug_call=debug_call)


__all__ = ["ClaimReview", "ClaimVerdict", "ClaimVerifier"]
