"""Opt-in same-Linux-host Judge pacing and in-flight leases; no proxy or work queue."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import json
import math
import os
import re
import stat
import struct
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from photobench.domain.errors import SharedJudgeGateError
from photobench.domain.runs.execution import RunCancelledError


class SharedAccountGate:
    def __init__(
        self,
        root: Path,
        scope: str,
        *,
        interval_seconds: float = 2.0,
        max_in_flight: int = 2,
        wait_timeout_seconds: float = 300,
    ) -> None:
        if not root.is_absolute() or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", scope):
            raise SharedJudgeGateError("configuration_error")
        if (
            not math.isfinite(interval_seconds)
            or interval_seconds <= 0
            or type(max_in_flight) is not int
            or max_in_flight < 1
            or not math.isfinite(wait_timeout_seconds)
            or wait_timeout_seconds <= 0
        ):
            raise SharedJudgeGateError("configuration_error")
        self.root, self.scope = root, scope
        self.interval_seconds, self.max_in_flight, self.wait_timeout_seconds = (
            interval_seconds,
            max_in_flight,
            wait_timeout_seconds,
        )
        try:
            self._private_directory(root)
            self.directory = root / scope
            self.directory.mkdir(mode=0o700, exist_ok=True)
            self._private_directory(self.directory)
            self.boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            self._owner()
        except (OSError, ValueError, IndexError) as exc:
            raise SharedJudgeGateError("unavailable_or_not_same_Linux_host") from exc

    @classmethod
    def from_env(cls) -> SharedAccountGate | None:
        prefix = "PHOTOBENCH_JUDGE_SHARED_GATE_"
        enabled = os.environ.get(prefix + "ENABLED", "0")
        configured = any(key.startswith(prefix) and key != prefix + "ENABLED" for key in os.environ)
        if enabled == "0" and not configured:
            return None
        if enabled != "1":
            raise SharedJudgeGateError("configuration_error")
        try:
            return cls(
                Path(os.environ[prefix + "DIR"]),
                os.environ[prefix + "SCOPE"],
                interval_seconds=float(os.environ.get(prefix + "INTERVAL_SECONDS", "2")),
                max_in_flight=int(os.environ.get(prefix + "MAX_IN_FLIGHT", "2")),
                wait_timeout_seconds=float(os.environ.get(prefix + "WAIT_TIMEOUT_SECONDS", "300")),
            )
        except (KeyError, ValueError) as exc:
            raise SharedJudgeGateError("configuration_error") from exc

    @staticmethod
    def _private_directory(path: Path) -> None:
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise SharedJudgeGateError("directory_not_private")

    @staticmethod
    def _owner() -> dict[str, Any]:
        raw = Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()
        return {"pid": os.getpid(), "start_ticks": raw[19]}

    @staticmethod
    def _alive(owner: dict[str, Any]) -> bool:
        try:
            raw = Path(f"/proc/{int(owner['pid'])}/stat").read_text().rsplit(")", 1)[1].split()
            return raw[0] not in {"Z", "X"} and raw[19] == str(owner["start_ticks"])
        except (OSError, KeyError, ValueError, IndexError):
            return False

    def _open(self, name: str, flags: int) -> int:
        fd = os.open(self.directory / name, flags | os.O_NOFOLLOW, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            os.close(fd)
            raise SharedJudgeGateError("file_not_private")
        return fd

    def _read(self, name: str) -> dict[str, Any] | None:
        try:
            fd = self._open(name, os.O_RDONLY)
        except FileNotFoundError:
            return None
        with os.fdopen(fd) as stream:
            value = json.load(stream)
        if not isinstance(value, dict):
            raise SharedJudgeGateError("state_corrupt")
        return value

    def _write(self, name: str, value: dict[str, Any]) -> None:
        temp = "." + uuid.uuid4().hex + ".tmp"
        fd = self._open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(value, stream, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(self.directory / temp, self.directory / name)
            directory = os.open(self.directory, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            with contextlib.suppress(FileNotFoundError):
                (self.directory / temp).unlink()

    def _audit(self, event: str, **fields: Any) -> None:
        fd = self._open("journal.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        with os.fdopen(fd, "a") as stream:
            stream.write(json.dumps({"event": event, "at": time.time(), **fields}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    @contextlib.contextmanager
    def _locked(self, deadline: float, cancelled: Callable[[], bool]) -> Iterator[os.stat_result]:
        fd = -1
        try:
            self._private_directory(self.root)
            self._private_directory(self.directory)
            fd = self._open("lock", os.O_RDWR | os.O_CREAT)
            while True:
                if cancelled():
                    raise RunCancelledError("cancelled before shared Judge gate permit")
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise SharedJudgeGateError("lock_timeout") from None
                    time.sleep(0.02)
            yield os.fstat(fd)
        except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
            raise SharedJudgeGateError("unavailable_or_corrupt") from exc
        finally:
            if fd >= 0:
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def _existing(self, lock_info: os.stat_result) -> tuple[dict[str, Any], dict[str, Any]]:
        identity, state = self._read("identity.json"), self._read("state.json")
        if identity is None or state is None:
            raise SharedJudgeGateError("state_missing")
        if (
            identity.get("lock") != [lock_info.st_dev, lock_info.st_ino]
            or identity.get("boot_id") != self.boot_id
        ):
            raise SharedJudgeGateError("host_or_lock_identity_changed")
        if (
            state.get("version") not in {1, 2}
            or not isinstance(state.get("active"), dict)
            or (
                state["version"] == 1
                and (
                    not isinstance(state.get("next_allowed"), (int, float))
                    or not math.isfinite(state["next_allowed"])
                )
            )
            or (
                state["version"] == 2
                and (
                    not isinstance(state.get("last_granted_monotonic"), (int, float))
                    or not math.isfinite(state["last_granted_monotonic"])
                    or state["last_granted_monotonic"] < 0
                )
            )
        ):
            raise SharedJudgeGateError("state_corrupt")
        return identity, state

    def _ready_clock(self, lock_info: os.stat_result, *, mark: bool = False, fresh: bool = False) -> float:
        # Same-boot pacing metadata, protected by flock. Critical lease/state/journal
        # fsyncs finish BEFORE this stamp; adding an fsync after it would move the
        # dispatch boundary back behind unbounded storage latency. A dead owner is
        # retained/blocked by its durable lease; a reboot rejects the boot identity.
        fd = self._open("lock", os.O_RDWR)
        try:
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) != (lock_info.st_dev, lock_info.st_ino):
                raise SharedJudgeGateError("lock_identity_changed")
            if mark:
                ready = 0.0 if fresh else time.monotonic()
                if os.pwrite(fd, struct.pack("!d", ready), 32) != 8:
                    raise SharedJudgeGateError("pacing_clock_unavailable")
                return ready
            raw = os.pread(fd, 8, 32)
            if len(raw) != 8:
                raise SharedJudgeGateError("pacing_clock_missing")
            ready = float(struct.unpack("!d", raw)[0])
            if not math.isfinite(ready) or ready < 0 or ready > time.monotonic():
                raise SharedJudgeGateError("pacing_clock_corrupt")
            return ready
        finally:
            os.close(fd)

    def _block(self, state: dict[str, Any], reason: str, lease_id: str | None = None) -> None:
        if not state.get("blocked"):
            state["blocked"] = {"reason": reason, "lease_id": lease_id, "at": time.time()}
        self._write("state.json", state)
        self._audit("blocked", reason=reason, lease_id=lease_id)

    def acquire(
        self, url: str, authorization: str, *, cancelled: Callable[[], bool] = lambda: False
    ) -> dict[str, Any]:
        try:
            parsed = urlsplit(url)
        except ValueError as exc:
            raise SharedJudgeGateError("provider_identity_invalid") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not authorization.startswith("Bearer ")
            or not authorization[7:].strip()
            or self.scope.casefold() == authorization[7:].strip().casefold()
        ):
            raise SharedJudgeGateError("provider_identity_invalid")
        deadline = time.monotonic() + self.wait_timeout_seconds
        owner = self._owner()
        while True:
            with self._locked(deadline, cancelled) as info:
                identity, state = self._read("identity.json"), self._read("state.json")
                fresh = identity is None and state is None
                if fresh:
                    if info.st_size:
                        raise SharedJudgeGateError("state_missing_no_reset")
                    marker = self._open("lock", os.O_RDWR)
                    try:
                        current = os.fstat(marker)
                        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                            raise SharedJudgeGateError("lock_identity_changed")
                        os.write(marker, b"initialized\n")
                        os.fsync(marker)
                    finally:
                        os.close(marker)
                    salt = os.urandom(32).hex()
                    fingerprint = hmac.new(
                        bytes.fromhex(salt), (url.rstrip("/") + "\0" + authorization).encode(), hashlib.sha256
                    ).hexdigest()
                    identity = {
                        "version": 1,
                        "salt": salt,
                        "provider_key_fingerprint": fingerprint,
                        "interval_seconds": self.interval_seconds,
                        "max_in_flight": self.max_in_flight,
                        "boot_id": self.boot_id,
                        "lock": [info.st_dev, info.st_ino],
                    }
                    state = {"version": 1, "next_allowed": 0.0, "active": {}, "blocked": None}
                    self._write("identity.json", identity)
                    self._write("state.json", state)
                    self._audit("created")
                identity, state = self._existing(info)
                expected = hmac.new(
                    bytes.fromhex(identity["salt"]),
                    (url.rstrip("/") + "\0" + authorization).encode(),
                    hashlib.sha256,
                ).hexdigest()
                if (
                    not hmac.compare_digest(expected, identity["provider_key_fingerprint"])
                    or identity.get("interval_seconds") != self.interval_seconds
                    or identity.get("max_in_flight") != self.max_in_flight
                ):
                    self._block(state, "group_identity_mismatch")
                    raise SharedJudgeGateError("group_identity_mismatch")
                for lease_id, lease in state["active"].items():
                    if lease.get("status") == "unknown" or not self._alive(lease["owner"]):
                        lease["status"] = "unknown"
                        self._block(state, "owner_or_outcome_unknown", lease_id)
                if state.get("blocked"):
                    raise SharedJudgeGateError("group_blocked")
                if state["version"] == 1:
                    # Never upgrade across old in-flight work. Version 2 also makes
                    # old clients reject this scope instead of ignoring the clock.
                    if state["active"]:
                        raise SharedJudgeGateError("legacy_active_lease_requires_idle_upgrade")
                    state["version"] = 2
                    state.pop("next_allowed")
                    state["last_granted_monotonic"] = 0.0
                    self._write("state.json", state)
                    self._audit("pacing_upgraded", from_version=1, to_version=2)
                    self._ready_clock(info, mark=True, fresh=fresh)
                ready = self._ready_clock(info)
                if ready < state["last_granted_monotonic"]:
                    raise SharedJudgeGateError("pacing_clock_stale")
                next_allowed = ready + self.interval_seconds if ready else 0.0
                now = time.monotonic()
                if len(state["active"]) < self.max_in_flight and now >= next_allowed:
                    lease = {
                        "lease_id": uuid.uuid4().hex,
                        "owner": owner,
                        "status": "in_flight",
                        "granted_at": time.time(),
                        "granted_monotonic": now,
                    }
                    state["active"][lease["lease_id"]] = lease
                    state["last_granted_monotonic"] = now
                    self._write("state.json", state)
                    self._audit("acquired", **lease)
                    ready = self._ready_clock(info, mark=True)
                    return {
                        **lease,
                        "permit_ready_monotonic": ready,
                        "permit_ready_at": time.time(),
                    }
                delay = max(0.005, min(0.05, next_allowed - now))
            if cancelled():
                raise RunCancelledError("cancelled waiting for shared Judge gate")
            if time.monotonic() >= deadline:
                raise SharedJudgeGateError("wait_timeout")
            time.sleep(delay)

    def finish(self, lease_id: str, outcome: str, *, http_status: int | None = None) -> None:
        if outcome not in {"http_response", "not_sent", "unknown", "configuration_error"}:
            raise SharedJudgeGateError("invalid_release")
        with self._locked(time.monotonic() + 5, lambda: False) as info:
            _, state = self._existing(info)
            lease = state["active"].get(lease_id)
            if lease is None or lease.get("owner") != self._owner():
                raise SharedJudgeGateError("lease_identity_changed")
            if lease.get("status") == "unknown":
                raise SharedJudgeGateError("unknown_lease_retained")
            if outcome == "unknown":
                lease["status"] = "unknown"
                self._block(state, "request_outcome_unknown", lease_id)
            else:
                state["active"].pop(lease_id)
                if outcome == "configuration_error":
                    self._block(state, "configuration_error", lease_id)
                else:
                    self._write("state.json", state)
            self._audit(
                "finished", lease_id=lease_id, owner=lease["owner"], outcome=outcome, http_status=http_status
            )
