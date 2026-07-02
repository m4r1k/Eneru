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

from dataclasses import replace
from typing import Dict, Optional, Tuple

from eneru import nut_control as nutctl
from eneru.scheduler import Schedule

__all__ = [
    "RESULT_ENUMS",
    "SelfTestUnavailable",
    "discover_self_test_command",
    "issue_self_test",
    "list_supported_commands",
    "normalize_result",
    "parse_schedule",
    "record_self_test_result",
    "self_test_control",
    "test_command_candidates",
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
    # "no test initiated" means no test has RUN yet (unknown), NOT that self-test
    # is unsupported — keep the two distinct.
    if "no test initiated" in t:
        return "unknown"
    if "not supported" in t or "unsupported" in t:
        return "unsupported"
    if "in progress" in t or "inprogress" in t or "progress" in t \
            or "running" in t or "pending" in t:
        return "running"
    if "fail" in t or "bad" in t or "error" in t:
        return "failed"
    if "pass" in t or t == "ok" or t == "done" or "done and passed" in t:
        return "passed"
    return "unknown"


def list_supported_commands(ups_name: str, *,
                            username: str = "", password: str = "",
                            timeout: int = 10) -> list:
    """Return the instant commands ``upscmd -l`` exposes for this UPS.

    A *transient* ``upscmd -l`` failure raises ``SelfTestUnavailable`` (distinct
    from an empty-but-successful list) so callers can retry instead of mistaking
    a dropped connection for "nothing supported".

    Credentials are forwarded because some upsd setups only return the command
    list to a logged-in client — without them the list comes back empty and a
    supported command looks unsupported.
    """
    ok, commands, err = nutctl.list_commands(
        ups_name, username=username, password=password, timeout=timeout)
    if not ok:
        raise SelfTestUnavailable(err or "upscmd -l failed")
    return commands


def test_command_candidates(commands) -> list:
    """The startable battery-test commands from an ``upscmd -l`` list.

    Powers a "did you mean" hint when the configured ``self_test.command`` isn't
    offered: many UPSes (e.g. APC via usbhid-ups) expose
    ``test.battery.start.quick`` / ``test.battery.start.deep`` but NOT the bare
    ``test.battery.start`` default. ``test.battery.stop`` is excluded — it ends a
    test, it doesn't start one.
    """
    return sorted(c for c in (commands or [])
                  if c.startswith("test.") and "start" in c)


def discover_self_test_command(ups_name: str, command: str, *,
                               username: str = "", password: str = "",
                               timeout: int = 10) -> Optional[str]:
    """Return ``command`` if ``upscmd -l`` actually exposes it, else ``None``.

    ``None`` means the command is genuinely not offered (self-disable for this
    UPS). A *transient* ``upscmd -l`` failure raises ``SelfTestUnavailable`` so
    the caller can retry instead of mistaking a dropped connection for an
    unsupported command (which on a 30-day cadence would skip a whole cycle).
    """
    commands = list_supported_commands(
        ups_name, username=username, password=password, timeout=timeout)
    return command if command in commands else None


def _with_command_allowed(nut_control, command: str):
    """A copy of ``nut_control`` with ``command`` guaranteed on its allowlist."""
    allowed = list(getattr(nut_control, "allowed_commands", None) or [])
    if command and command not in allowed:
        allowed.append(command)
        return replace(nut_control, allowed_commands=allowed)
    return nut_control


def self_test_control(nut_control, self_test_cfg,
                      command: str) -> Tuple[bool, object]:
    """Decide whether ``command`` may be issued as a self-test, and under what
    effective ``nut_control``. Returns ``(permitted, effective_nut_control)``.

    ELI5: a self-test used to need a full key ring — flip nut_control on AND put
    the test command on its allowlist AND turn auth on. That was three keys for
    one door. From v6.1.2, turning ``self_test`` on is its own key that opens
    exactly ONE door: the single command you configured in ``self_test.command``.
    Every other control door (arbitrary commands, variable writes) still needs
    the old key ring. Authentication is still mandatory — that lock never comes
    off — but it is enforced by the caller / config validation, not here.

    Precedence:
      * ``self_test.enabled`` → permitted; the returned ``nut_control`` has
        ``command`` guaranteed on its allowlist (so :func:`issue_self_test`'s
        shared allowlist check still passes) while inheriting the configured
        credentials / timeout unchanged.
      * else if the GENERAL control surface already allows it
        (``nut_control.enabled`` AND ``command`` on the allowlist) → permitted,
        ``nut_control`` returned unchanged (v6.0/v6.1 behavior).
      * else → not permitted.
    """
    if bool(getattr(self_test_cfg, "enabled", False)):
        return True, _with_command_allowed(nut_control, command)
    if (bool(getattr(nut_control, "enabled", False))
            and nutctl.command_allowed(command,
                                       getattr(nut_control, "allowed_commands",
                                               None))):
        return True, nut_control
    return False, nut_control


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
        except (ValueError, TypeError) as exc:
            raise ValueError(
                f"invalid self_test.schedule interval {schedule!r}") from exc
        mult = {"d": 86400, "h": 3600, "m": 60}.get(unit)
        if mult is None or n <= 0:
            raise ValueError(f"invalid self_test.schedule interval {schedule!r}")
        return Schedule.interval(n * mult, fire_on_first=False)
    raise ValueError(f"invalid self_test.schedule {schedule!r}")
