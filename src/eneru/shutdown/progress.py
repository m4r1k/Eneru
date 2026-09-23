"""Thread-safe, failure-isolated shutdown progress snapshots."""

import copy
import threading
import time
from typing import Any, Dict, Optional

from eneru.shutdown.plan import PHASE_ORDER


class ShutdownProgress:
    """Keep the current or most recent shutdown run in memory for the API."""

    def __init__(self, kind: str, name: str):
        self._lock = threading.Lock()
        self._kind = kind
        self._name = name
        self._run_id = 0
        self._data = self._idle_snapshot()

    def _idle_snapshot(self) -> Dict[str, Any]:
        return {
            "scope": {"kind": self._kind, "name": self._name},
            "runId": self._run_id,
            "state": "idle",
            "reason": "",
            "startedAt": None,
            "finishedAt": None,
            "phases": [
                {"id": phase_id, "state": "pending", "startedAt": None,
                 "finishedAt": None, "detail": ""}
                for phase_id in PHASE_ORDER
            ],
            "remotes": [],
        }

    def start(self, reason: str) -> None:
        """Start a fresh progress run."""
        try:
            with self._lock:
                self._run_id += 1
                self._data = self._idle_snapshot()
                self._data.update({
                    "runId": self._run_id,
                    "state": "running",
                    "reason": str(reason or "Shutdown conditions met")[:500],
                    "startedAt": time.time(),
                })
        except Exception:
            pass

    def phase_start(self, phase_id: str) -> None:
        self._set_phase(phase_id, "running", started=True)

    def phase_finish(self, phase_id: str, state: str = "succeeded",
                     detail: str = "",
                     generation: Optional[int] = None) -> None:
        self._set_phase(
            phase_id, state, detail=detail, finished=True,
            generation=generation,
        )

    def phase_skip(self, phase_id: str, detail: str) -> None:
        self._set_phase(phase_id, "skipped", detail=detail, finished=True)

    def _set_phase(self, phase_id: str, state: str, *, detail: str = "",
                   started: bool = False, finished: bool = False,
                   generation: Optional[int] = None) -> None:
        try:
            with self._lock:
                if generation is not None and generation != self._run_id:
                    return
                row = next(
                    (phase for phase in self._data["phases"]
                     if phase["id"] == phase_id), None)
                if row is None:
                    return
                now = time.time()
                row["state"] = state
                if started and row["startedAt"] is None:
                    row["startedAt"] = now
                if finished:
                    row["finishedAt"] = now
                    if row["startedAt"] is None and state != "skipped":
                        row["startedAt"] = now
                if state == "failed":
                    row["detail"] = "Phase failed; see service logs"
                elif state == "timed-out":
                    row["detail"] = "Phase timed out; see service logs"
                else:
                    row["detail"] = str(detail or "")[:300]
        except Exception:
            pass

    def remote_start(self, server: str, host: str) -> Optional[int]:
        """Mark one remote worker running and return its run generation."""
        try:
            with self._lock:
                row = self._remote_row(server, host)
                row.update({
                    "state": "running",
                    "startedAt": row.get("startedAt") or time.time(),
                    "finishedAt": None,
                    "outcome": "",
                    "error": "",
                })
                return self._run_id
        except Exception:
            return None

    def remote_finish(self, result: Any, generation: Optional[int]) -> None:
        """Publish a sanitized remote result."""
        try:
            with self._lock:
                # A timed-out worker can outlive its shutdown run. Never let a
                # late result rewrite a newer run's row for the same host.
                if generation != self._run_id:
                    return
                row = self._remote_row(result.server, result.host)
                # The orchestrator's deadline is authoritative. A worker may
                # return after its join timed out; do not rewrite that timeout
                # as success after the shutdown sequence has moved on.
                if row.get("state") == "timed-out" and not getattr(
                        result, "timed_out", False):
                    return
                if getattr(result, "timed_out", False):
                    state, outcome = "timed-out", "timeout"
                elif getattr(result, "shutdown_sent", False):
                    state = (
                        "succeeded" if getattr(result, "success", False)
                        else "failed"
                    )
                    outcome = (
                        "dry-run" if getattr(result, "dry_run", False)
                        else "command-sent"
                    )
                else:
                    state, outcome = "failed", "not-sent"
                row.update({
                    "state": state,
                    "finishedAt": time.time(),
                    "outcome": outcome,
                    "preCommandsAttempted": getattr(
                        getattr(result, "pre_commands", None), "attempted", 0),
                    "preCommandsFailed": getattr(
                        getattr(result, "pre_commands", None), "failed", 0),
                    "error": (
                        "Remote shutdown timed out; see service logs"
                        if state == "timed-out"
                        else "Remote shutdown failed; see service logs"
                        if state == "failed"
                        else ""
                    ),
                })
        except Exception:
            pass

    def _remote_row(self, server: str, host: str) -> Dict[str, Any]:
        row = next(
            (item for item in self._data["remotes"]
             if item["server"] == server and item["host"] == host), None)
        if row is None:
            row = {
                "server": str(server), "host": str(host), "state": "pending",
                "startedAt": None, "finishedAt": None, "outcome": "",
                "error": "", "preCommandsAttempted": 0,
                "preCommandsFailed": 0,
            }
            self._data["remotes"].append(row)
        return row

    def finish(self, state: str = "succeeded",
               generation: Optional[int] = None) -> None:
        """Finish the overall run while retaining it for post-event review."""
        try:
            with self._lock:
                if generation is not None and generation != self._run_id:
                    return
                self._data["state"] = state
                self._data["finishedAt"] = time.time()
        except Exception:
            pass

    def snapshot(self) -> Dict[str, Any]:
        """Return a detached JSON-safe copy."""
        try:
            with self._lock:
                return copy.deepcopy(self._data)
        except Exception:
            return self._idle_snapshot()
