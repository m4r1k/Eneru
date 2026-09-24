"""Thread-safe, failure-isolated shutdown progress snapshots."""

import copy
import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import re

from eneru.logger import redact_sensitive_text
from eneru.shutdown.plan import PHASE_ORDER

# Remote command output can be long (a chatty shutdown script). Keep the
# tail, where errors usually land, and cap it so one host can't bloat the API.
REMOTE_DETAIL_MAX_CHARS = 8000


# Script output uses more secret shapes than log lines do. Best-effort: these
# cover `Authorization: Bearer x`, `password: x`, `--password x` and JSON
# `"token": "x"` on top of the logger's key=value / URL-userinfo rules.
_SECRET_KEY = r"(?:password|passwd|secret|token|api[_-]?key)"
_OUTPUT_SECRET_PATTERNS = (
    # Whole header value: covers Bearer, Basic, and any other scheme.
    (re.compile(r"(\bauthorization\s*:\s*)[^\r\n]+", re.IGNORECASE),
     r"\1<redacted>"),
    (re.compile(r"(\bbearer\s+)(?!<redacted>)\S+", re.IGNORECASE), r"\1<redacted>"),
    (re.compile(r'("' + _SECRET_KEY + r'"\s*:\s*")[^"]*', re.IGNORECASE),
     r"\1<redacted>"),
    (re.compile(r"(--?" + _SECRET_KEY + r"[=\s]+)\S+", re.IGNORECASE),
     r"\1<redacted>"),
    (re.compile(r"(\b" + _SECRET_KEY + r"\s*:\s*)(?!<redacted>)\S+",
                re.IGNORECASE), r"\1<redacted>"),
)


def progress_sidecar_path(state_file_path: Any) -> Path:
    """Where the progress snapshot for a state file lives (TUI reads it)."""
    path = Path(state_file_path)
    return path.with_name(path.name + ".shutdown-progress.json")


def read_progress_sidecar(path: Any) -> Optional[Dict[str, Any]]:
    """Read a progress sidecar; None when missing or unreadable."""
    try:
        data = json.loads(Path(path).read_text())
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _detail_text(value: Any) -> str:
    """Redact credentials and keep the last REMOTE_DETAIL_MAX_CHARS chars."""
    text = redact_sensitive_text(str(value or ""))
    for pattern, replacement in _OUTPUT_SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    text = text.strip()
    if len(text) > REMOTE_DETAIL_MAX_CHARS:
        text = "…(truncated)\n" + text[-REMOTE_DETAIL_MAX_CHARS:]
    return text


class ShutdownProgress:
    """Keep the current or most recent shutdown run in memory for the API."""

    def __init__(self, kind: str, name: str,
                 sidecar_path: Optional[Path] = None):
        self._lock = threading.Lock()
        # Optional JSON mirror of the ANONYMOUS snapshot so an out-of-process
        # reader (the `eneru monitor` TUI) can follow a shutdown without the
        # API. ELI5: the whiteboard in the hallway, copied from the one in the
        # control room -- only the public bits, never raw command output.
        # Writes are best-effort and can never raise into the shutdown path.
        self.sidecar_path: Optional[Path] = (
            Path(sidecar_path) if sidecar_path is not None else None)
        self._write_lock = threading.Lock()
        self._kind = kind
        self._name = name
        self._run_id = 0
        self._data = self._idle_snapshot()
        # Raw per-remote command output lives outside ``_data`` so the default
        # (anonymous) snapshot can never include it; see snapshot().
        self._remote_details: Dict[tuple, Dict[str, Any]] = {}

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
                self._remote_details = {}
                self._data.update({
                    "runId": self._run_id,
                    "state": "running",
                    "reason": str(reason or "Shutdown conditions met")[:500],
                    "startedAt": time.time(),
                })
        except Exception:
            pass
        self.persist()

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
        self.persist()

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
                generation = self._run_id
        except Exception:
            return None
        self.persist()
        return generation

    def remote_finish(self, result: Any, generation: Optional[int]) -> None:
        """Publish a sanitized remote result."""
        try:
            with self._lock:
                # A timed-out worker can outlive its shutdown run. Never let a
                # late result rewrite a newer run's row for the same host.
                if generation != self._run_id:
                    return
                row = self._remote_row(result.server, result.host)
                late = row.get("state") == "timed-out" and not getattr(
                    result, "timed_out", False)
                exit_code = getattr(result, "exit_code", None)
                detail = {
                    "exitCode": exit_code if isinstance(exit_code, int) else None,
                    "response": _detail_text(getattr(result, "response", "")),
                    "error": _detail_text(getattr(result, "error", "")),
                    "preCommandsError": _detail_text(getattr(
                        getattr(result, "pre_commands", None), "error", "")),
                }
                key = (row["server"], row["host"])
                # The orchestrator's deadline is authoritative. A worker may
                # return after its join timed out; do not rewrite that timeout
                # as success after the shutdown sequence has moved on. Its
                # output is still kept, since a timeout is when it helps most.
                if late:
                    self._remote_details[key] = detail
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
                self._remote_details[key] = detail
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
        self.persist()

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
        self.persist()

    def snapshot(self, include_detail: bool = False) -> Dict[str, Any]:
        """Return a detached JSON-safe copy.

        ``include_detail`` adds each finished remote's redacted command
        response, exit code, and error text. Callers pass it only for
        authenticated API readers.
        """
        try:
            with self._lock:
                data = copy.deepcopy(self._data)
                if include_detail:
                    for row in data["remotes"]:
                        detail = self._remote_details.get(
                            (row["server"], row["host"]))
                        row["detail"] = copy.deepcopy(detail)
                return data
        except Exception:
            return self._idle_snapshot()

    def persist(self) -> None:
        """Mirror the anonymous snapshot to ``sidecar_path`` (best-effort)."""
        path = self.sidecar_path
        if path is None:
            return
        try:
            payload = self.snapshot()
            payload["writtenAt"] = time.time()
            with self._write_lock:
                tmp = path.with_name(path.name + ".tmp")
                tmp.write_text(json.dumps(payload, sort_keys=True))
                tmp.replace(path)
        except Exception:
            pass
