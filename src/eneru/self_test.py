"""Scheduled UPS battery self-test (v6.1).

A write surface like nut_control: issuing a test goes through the SAME
allowlist check the API control path uses (``nut_control.command_allowed``),
so a scheduled test can never bypass the v6.0 control allowlist. The design
adapts to whatever ``upscmd -l`` exposes -- if the configured command isn't
offered, the feature self-disables.

Unit-tested only in CI: the NUT dummy driver has no INSTCMD, so this can't be
exercised end-to-end (the maintainer validates it against real hardware).
Everything here keeps I/O behind ``nut_control`` so tests mock it.
"""

from typing import Dict, Optional

from eneru import nut_control as nutctl
from eneru.scheduler import Schedule

__all__ = [
    "RESULT_ENUMS",
    "SelfTestUnavailable",
    "discover_self_test_command",
    "issue_self_test",
    "normalize_result",
    "parse_schedule",
    "record_self_test_result",
]


class SelfTestUnavailable(Exception):
    """``upscmd -l`` could not be queried (transient NUT error), as distinct
    from the command being genuinely unsupported. Callers should retry rather
    than treating it as 'not exposed'."""

# The normalized result vocabulary the API / Prometheus / UI consume. The raw
# ``ups.test.result`` string is unbounded and vendor-specific, so it is stored
# alongside but never used as a label/enum directly.
RESULT_ENUMS = ("passed", "failed", "running", "unknown", "unsupported")


def normalize_result(raw: Optional[str]) -> str:
    """Map a raw ``ups.test.result`` string to a small stable enum."""
    if not raw:
        return "unknown"
    t = str(raw).strip().lower()
    if not t or t in ("n/a", "na", "-"):
        return "unknown"
    if "no test initiated" in t or "not supported" in t or "unsupported" in t:
        return "unsupported"
    if "in progress" in t or "inprogress" in t or "progress" in t \
            or "running" in t or "pending" in t:
        return "running"
    if "fail" in t or "bad" in t or "error" in t:
        return "failed"
    if "pass" in t or t == "ok" or t == "done" or "done and passed" in t:
        return "passed"
    return "unknown"


def discover_self_test_command(ups_name: str, command: str, *,
                               timeout: int = 10) -> Optional[str]:
    """Return ``command`` if ``upscmd -l`` actually exposes it, else ``None``.

    ``None`` means the command is genuinely not offered (self-disable for this
    UPS). A *transient* ``upscmd -l`` failure raises ``SelfTestUnavailable`` so
    the caller can retry instead of mistaking a dropped connection for an
    unsupported command (which on a 30-day cadence would skip a whole cycle).
    """
    ok, commands, err = nutctl.list_commands(ups_name, timeout=timeout)
    if not ok:
        raise SelfTestUnavailable(err or "upscmd -l failed")
    return command if command in commands else None


def issue_self_test(ups_name: str, command: str, nut_control, store, *,
                    source: str = "scheduler") -> Dict:
    """Issue a self-test, enforcing the shared allowlist first.

    Records a ``running`` self_tests row (so a poll can finalise it) and
    returns ``{ok, error, test_id}``. The allowlist check is the SAME one the
    API control path uses -- the scheduled path is not exempt.
    """
    if not nutctl.command_allowed(command, nut_control.allowed_commands):
        return {"ok": False, "test_id": None,
                "error": f"command {command!r} is not in nut_control.allowed_commands"}

    test_id = None
    if store is not None:
        test_id = store.record_self_test(command, source, result_enum="running")

    ok, _out, err = nutctl.run_instant_command(
        ups_name, command, nut_control.username, nut_control.password,
        timeout=nut_control.timeout)

    if not ok:
        if store is not None and test_id is not None:
            store.update_self_test_result(
                test_id, result_raw=err, result_enum="failed")
        return {"ok": False, "test_id": test_id, "error": err}
    return {"ok": True, "test_id": test_id, "error": ""}


def record_self_test_result(store, test_id: Optional[int],
                            raw_result: Optional[str],
                            raw_date: Optional[str]) -> str:
    """Normalize a polled ``ups.test.result`` and update the row. Returns the
    enum. Kept I/O-free (the caller reads the raw values via upsc) so it is
    trivially testable."""
    enum = normalize_result(raw_result)
    if store is not None and test_id is not None:
        store.update_self_test_result(
            test_id, result_raw=raw_result, result_enum=enum,
            result_date=raw_date)
    return enum


def parse_schedule(schedule: str, time_str: str = "03:00",
                   *, weekday: str = "monday", monthly_day: int = 1) -> Schedule:
    """Build a Schedule from the self_test config.

    ``daily`` / ``weekly`` / ``monthly`` use ``time_str`` (and weekday /
    monthly_day); ``every <N>{d|h|m}`` (e.g. ``every 30d``) is an interval.
    Self-test never fires on first sight (``fire_on_first=False``) so a daemon
    restart never kicks off an unscheduled test.
    """
    s = str(schedule).strip().lower()
    if s == "daily":
        return Schedule.daily(time_str, fire_on_first=False)
    if s == "weekly":
        return Schedule.weekly(weekday, time_str, fire_on_first=False)
    if s == "monthly":
        return Schedule.monthly(monthly_day, time_str, fire_on_first=False)
    if s.startswith("every "):
        spec = s[len("every "):].strip()
        unit = spec[-1:] if spec else ""
        try:
            n = int(spec[:-1])
        except (ValueError, TypeError):
            raise ValueError(f"invalid self_test.schedule interval {schedule!r}")
        mult = {"d": 86400, "h": 3600, "m": 60}.get(unit)
        if mult is None or n <= 0:
            raise ValueError(f"invalid self_test.schedule interval {schedule!r}")
        return Schedule.interval(n * mult, fire_on_first=False)
    raise ValueError(f"invalid self_test.schedule {schedule!r}")
