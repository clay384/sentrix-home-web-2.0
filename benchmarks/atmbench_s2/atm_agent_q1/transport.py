"""Recover one timed-out gateway request from its owned journal; never resend it.

Socket timeout and recovery_wait_s are observation bounds, not a promise that an
upstream request has ended. Exiting recovery never changes the gateway ledger.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
from contextlib import closing
from pathlib import Path

from .adapters import AdapterError, http_json

CONTEXT_KEYS = {"run_id", "system", "stage", "entity_id", "attempt_id", "request_id"}
MODEL = "qwen3.7-flash"


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def require(ok, code):
    if not ok:
        raise AdapterError("transport_recovery_integrity:" + code)


def is_timeout(error):
    return (
        isinstance(error, TimeoutError)
        or isinstance(error, urllib.error.URLError)
        and isinstance(error.reason, TimeoutError)
        or isinstance(error, AdapterError)
        and str(error) == "TimeoutError_outcome_unknown"
    )


def read_health(url):
    # Only reached after a timeout. This is a read-only local liveness probe.
    with urllib.request.urlopen(url, timeout=2) as response:
        return json.load(response)


class LedgerRecoveringTransport:
    def __init__(
        self,
        ledger_path,
        trace_dir,
        *,
        initial=http_json,
        health=read_health,
        clock=time.monotonic,
        sleep=time.sleep,
        recovery_wait_s=2400.0,
        poll_s=2.0,
        unavailable_limit=3,
    ):
        self.ledger = Path(ledger_path)
        self.trace_dir = Path(trace_dir)
        self.initial, self.health = initial, health
        self.clock, self.sleep = clock, sleep
        require(math.isfinite(recovery_wait_s) and recovery_wait_s > 0, "recovery_wait_invalid")
        require(math.isfinite(poll_s) and poll_s > 0, "poll_invalid")
        require(type(unavailable_limit) is int and unavailable_limit >= 1, "unavailable_limit_invalid")
        self.wait_s, self.poll_s, self.unavailable_limit = recovery_wait_s, poll_s, unavailable_limit

    def __call__(self, method, url, body, headers, timeout):
        # The successful path is exactly the original transport: no extra reads,
        # requests, normalization, or replacement of the provider's response.
        try:
            return self.initial(method, url, body, headers, timeout)
        except Exception as error:
            if not is_timeout(error):
                raise
            # Q0's package is also used by the existing MemOS runner. Preserve
            # its original timeout behavior; this repair authorizes only S1/S2.
            if headers.get("x-atm-system") == "M1-Q0":
                raise
            return self._recover(method, url, body, headers, timeout, error)

    def _recover(self, method, url, body, headers, timeout, original_error):
        require(method == "POST" and url == "http://127.0.0.1:19600/v1/chat/completions", "gateway_endpoint")
        context = {key: headers.get("x-atm-" + key.replace("_", "-")) for key in CONTEXT_KEYS}
        require(all(isinstance(v, str) and v for v in context.values()), "context_fields")
        require(
            context["run_id"] == "atmbench-20261116"
            and context["system"] in ("S1-Q0", "S2-Q1")
            and context["stage"] == "answer",
            "context_scope",
        )
        require(set(headers) == {"x-atm-" + key.replace("_", "-") for key in CONTEXT_KEYS}, "context_headers")
        require(isinstance(body, dict) and body.get("stream") is False, "explicit_stream_false_required")
        normalized = dict(body)
        del normalized["stream"]  # This is the sole normalization used by the frozen Agent.
        require(
            normalized.get("model") == MODEL
            and normalized.get("max_tokens") == 131072
            and normalized.get("enable_thinking") is True,
            "model_profile",
        )
        expected_hash = fingerprint({"body": normalized, "context": context})
        rid = context["request_id"]
        require(len(rid) == 32 and all(c in "0123456789abcdef" for c in rid), "request_id_shape")

        # Locate only the current call's already-persisted request, not other QA.
        matches = []
        for round_no in range(1, 33):
            identity = "/".join(
                (
                    context["run_id"],
                    context["system"],
                    context["entity_id"],
                    context["attempt_id"],
                    str(round_no),
                )
            )
            if uuid.uuid5(uuid.NAMESPACE_URL, identity).hex == rid:
                matches.append(self.trace_dir / ("decision-%02d-request.json" % round_no))
        require(len(matches) == 1, "request_id_round_binding")
        trace_path = matches[0]
        require(
            trace_path.resolve(strict=True) == self.trace_dir.resolve(strict=True) / trace_path.name,
            "trace_path_escape",
        )
        trace_bytes = trace_path.read_bytes()
        trace = json.loads(trace_bytes)
        require(trace.get("request") == body and trace.get("context") == context, "original_trace_binding")
        trace_sha = hashlib.sha256(trace_bytes).hexdigest()
        run_path = self.trace_dir / "run.json"
        require(
            run_path.resolve(strict=True) == self.trace_dir.resolve(strict=True) / "run.json",
            "run_path_escape",
        )
        run_bytes = run_path.read_bytes()
        run = json.loads(run_bytes)
        require(
            run.get("question_id") == context["entity_id"]
            and run.get("attempt_id") == context["attempt_id"]
            and run.get("config", {}).get("run_id") == context["run_id"]
            and run.get("config", {}).get("system") == context["system"],
            "question_attempt_binding",
        )
        run_sha = hashlib.sha256(run_bytes).hexdigest()
        ledger = self.ledger.resolve(strict=True)
        require(ledger.name == "requests.sqlite3" and ledger.parent.name == "gateway", "owned_ledger_layout")
        require(self.ledger.is_file(), "ledger_missing")
        root = ledger.parent
        name = fingerprint(rid) + ".json"
        payload_path, response_path = root / "payloads" / name, root / "responses" / name
        audit_path = self.trace_dir / ("transport-recovery-" + rid + ".jsonl")
        audit = audit_path.open("x", encoding="utf-8")
        os.chmod(audit_path, 0o600)

        def emit(event, **fields):
            record = {
                "event": event,
                "request_id": rid,
                "context": context,
                "observed_at": time.time(),
                **fields,
            }
            audit.write(canonical(record) + "\n")
            audit.flush()
            os.fsync(audit.fileno())

        started = self.clock()
        previous, unavailable, stop_reason = None, 0, None
        try:
            emit(
                "timeout_observed",
                original_error_type=type(original_error).__name__,
                original_error=str(original_error),
                initial_socket_timeout_s=timeout,
                recovery_wait_s=self.wait_s,
                ledger_path=str(ledger),
                request_payload_hash=expected_hash,
                trace_path=str(trace_path),
                trace_sha256=trace_sha,
                run_path=str(run_path),
                run_sha256=run_sha,
                post_count=1,
                policy="read_only_same_request_no_post_retry",
            )
            while True:
                require(
                    hashlib.sha256(trace_path.read_bytes()).hexdigest() == trace_sha, "original_trace_changed"
                )
                require(hashlib.sha256(run_path.read_bytes()).hexdigest() == run_sha, "original_run_changed")
                # Never instantiate Ledger: its constructor/connection writes pragmas.
                with closing(sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
                    db.row_factory = sqlite3.Row
                    db.execute("PRAGMA query_only=ON")
                    row = db.execute("SELECT * FROM requests WHERE request_id=?", (rid,)).fetchone()
                row = dict(row) if row is not None else None
                state = row["status"] if row else "not_yet_journaled"
                if state != previous:
                    emit("ledger_state", status=state)
                    previous = state
                if row is not None:
                    require(json.loads(row["context_json"]) == context, "ledger_context")
                    require(row["payload_hash"] == expected_hash, "ledger_payload_hash")
                    require(
                        type(row["reserved_micro"]) is int and row["reserved_micro"] >= 1829146,
                        "full_cost_hold_missing",
                    )
                    require(
                        state
                        in (
                            "reserved",
                            "dispatched",
                            "completed",
                            "unknown",
                            "failed",
                            "cancelled_before_dispatch",
                        ),
                        "ledger_status",
                    )
                    if state not in ("reserved", "cancelled_before_dispatch"):
                        self._file_binding(row["payload_path"], payload_path)
                        payload_bytes = payload_path.read_bytes()
                        payload = json.loads(payload_bytes)
                        require(
                            payload.get("request") == normalized and payload.get("context") == context,
                            "payload_file_binding",
                        )
                        require(
                            fingerprint({"body": payload["request"], "context": payload["context"]})
                            == expected_hash,
                            "payload_file_hash",
                        )
                        require(
                            self._number(row["dispatched_at"])
                            and self._number(row["created_at"])
                            and row["created_at"] <= row["dispatched_at"],
                            "dispatch_time",
                        )
                    if state in ("reserved", "dispatched"):
                        require(
                            row["ended_at"] is None and row["charged_micro"] is None,
                            "inflight_terminal_fields",
                        )
                    elif state == "completed":
                        response, evidence = self._completed(row, response_path)
                        emit(
                            "completed_response_recovered",
                            payload_file_sha256=hashlib.sha256(payload_bytes).hexdigest(),
                            elapsed_recovery_s=self.clock() - started,
                            **evidence,
                        )
                        return response
                    else:
                        require(self._number(row["ended_at"]), "terminal_time_missing")
                        if state == "unknown":
                            require(row["charged_micro"] is None, "unknown_hold_released")
                        emit(
                            "original_failure_preserved",
                            reason="gateway_terminal_" + state,
                            reserved_micro=row["reserved_micro"],
                            charged_micro=row["charged_micro"],
                            ledger_unchanged=True,
                        )
                        raise original_error
                if stop_reason or self.clock() - started >= self.wait_s:
                    emit(
                        "original_failure_preserved",
                        reason=stop_reason or "recovery_observation_deadline",
                        status=state,
                        ledger_unchanged=True,
                        upstream_outcome_not_inferred=True,
                    )
                    raise original_error
                try:
                    health = self.health("http://127.0.0.1:19600/health")
                except Exception as health_error:
                    unavailable += 1
                    emit(
                        "gateway_probe_failed",
                        consecutive=unavailable,
                        error_type=type(health_error).__name__,
                    )
                    if unavailable >= self.unavailable_limit:
                        # A connection failure is evidence of unreachability, not
                        # evidence that the provider stopped or that billing is zero.
                        # Read the journal once more before exiting: completion
                        # may have committed just before the gateway went away.
                        stop_reason = "gateway_unreachable"
                        continue
                else:
                    require(
                        health.get("status") == "ready"
                        and health.get("run_id") == context["run_id"]
                        and health.get("model") == MODEL,
                        "gateway_identity",
                    )
                    unavailable = 0
                self.sleep(min(self.poll_s, max(0.0, self.wait_s - (self.clock() - started))))
        except Exception as error:
            if error is not original_error:
                emit(
                    "recovery_rejected",
                    error_type=type(error).__name__,
                    error=str(error),
                    ledger_unchanged=True,
                )
            raise
        finally:
            audit.close()

    @staticmethod
    def _number(value):
        return type(value) in (int, float) and math.isfinite(value) and value >= 0

    @staticmethod
    def _file_binding(actual, expected):
        require(isinstance(actual, str) and Path(actual).is_absolute(), "journal_path_not_absolute")
        require(
            Path(actual).resolve(strict=True) == expected and expected.resolve(strict=True) == expected,
            "journal_path_escape",
        )

    def _completed(self, row, response_path):
        require(
            row["http_status"] == 200
            and self._number(row["ended_at"])
            and row["ended_at"] >= row["dispatched_at"],
            "completed_terminal_fields",
        )
        self._file_binding(row["response_path"], response_path)
        data = response_path.read_bytes()
        response = json.loads(data)
        require(response == json.loads(row["response_json"]), "response_file_ledger_mismatch")
        require(isinstance(response.get("choices"), list) and bool(response["choices"]), "response_choices")
        usage = response.get("usage")
        require(isinstance(usage, dict) and usage == json.loads(row["usage_json"]), "response_usage")
        p, c = usage.get("prompt_tokens"), usage.get("completion_tokens")
        require(type(p) is int and type(c) is int and min(p, c) >= 0, "usage_not_settled")
        a, b = (0.2, 0.8) if p <= 32768 else (0.6, 2.4) if p <= 262144 else (1.2, 4.8)
        require(
            type(row["charged_micro"]) is int
            and row["charged_micro"] == math.ceil(p * a + c * b)
            and type(row["actual_tokens"]) is int
            and row["actual_tokens"] == p + c,
            "charged_usage_mismatch",
        )
        require(
            row["provider_request_id"] == response.get("id") and bool(response.get("id")),
            "provider_id_mismatch",
        )
        return response, {
            "response_path": str(response_path),
            "response_file_sha256": hashlib.sha256(data).hexdigest(),
            "response_canonical_sha256": fingerprint(response),
            "charged_micro": row["charged_micro"],
            "actual_tokens": row["actual_tokens"],
            "ledger_unchanged": True,
        }
