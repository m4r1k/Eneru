"""SSH-based remote-server shutdown phase.

Owns the multi-server orchestration (sequential vs parallel batching by
``shutdown_order``) plus the per-server lifecycle: ``pre_shutdown_commands``
followed by the final shutdown command.
"""

import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from eneru.actions import REMOTE_ACTIONS, render_action, serialize_umount_targets
from eneru.config import RemoteServerConfig
from eneru import utils as eneru_utils
from eneru.utils import run_command

# Per-command wall-clock buffer added on top of the configured timeout to absorb
# SSH connection/teardown overhead. _run_remote_command spends this on every
# command. _shutdown_remote_server reserves command_timeout + this buffer for
# the final poweroff before spending the deadline on pre-shutdown commands, so a
# slow pre-phase can't starve the poweroff (H8).
_SSH_OVERHEAD_BUFFER = 30

# ELI5: when you log in at a terminal, your shell reads its "startup notes"
# (/etc/profile, ~/.profile) that say where all the tools live -- that's how
# your interactive `sudo which synoshutdown` finds /usr/syno/sbin on a
# Synology NAS. But `ssh host "cmd"` runs cmd non-interactively, so those
# notes are NEVER read: the shell starts with a bare, minimal PATH and a bare
# command name like `synoshutdown` looks like it doesn't exist ("command not
# found"), even though the file is sitting right there. We fix it by handing
# the remote shell a note of our own FIRST -- prepend an `export PATH=...`
# that re-adds the standard privileged dirs plus Synology's /usr/syno/sbin
# and /usr/syno/bin. We append them AFTER $PATH so the remote's own PATH still
# wins and any dir that doesn't exist on that host is simply ignored. sudo
# inherits this augmented PATH (the user's `sudo which` succeeding proves
# their sudoers has no restrictive secure_path), so `sudo -n synoshutdown`
# resolves the bare name exactly like an interactive login would. Real
# incident: DS1821 remote_server target, Eneru 6.1.6 power outage —
# "sudo: synoshutdown: command not found".
REMOTE_PATH_PREFIX = (
    'export PATH="$PATH:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:'
    '/sbin:/bin:/usr/syno/sbin:/usr/syno/bin"; '
)

# ELI5: you phone a warehouse and shout "SHIP IT!" — mid-sentence the line goes
# dead. Did they hear you? On a small box (BusyBox/Alpine/dropbear/sysvinit) the
# `poweroff` you just sent kills sshd BEFORE it can mail you back an exit status,
# so ssh gives up with code 255 and a "connection dropped" complaint. That is
# NOT the shutdown command failing — it's the shutdown WORKING so fast it hung up
# on us. We only trust this reading for the FINAL poweroff (a mid-sequence
# command that drops the line really did fail) and only when the stderr looks
# like the transport tore down, never a real "Permission denied" 255.
_SSH_TRANSPORT_TEARDOWN_SIGNATURES = (
    "closed by remote host",
    "broken pipe",
    "connection reset",
)


def _is_ssh_transport_teardown(stderr: str) -> bool:
    """True when ssh stderr looks like the session was cut mid-command.

    Covers the fixed BusyBox/dropbear phrasings plus OpenSSH's
    "Connection to <host> closed." (variable host in the middle).
    """
    low = stderr.lower()
    if any(sig in low for sig in _SSH_TRANSPORT_TEARDOWN_SIGNATURES):
        return True
    return "connection to" in low and "closed" in low


@dataclass
class RemotePreShutdownResult:
    """Best-effort result summary for remote pre-shutdown commands."""

    attempted: int = 0
    failed: int = 0
    timed_out: bool = False
    error: str = ""


@dataclass
class RemoteShutdownResult:
    """Structured result for one remote shutdown worker."""

    server: str
    host: str
    completed: bool = True
    shutdown_sent: bool = False
    dry_run: bool = False
    pre_commands: RemotePreShutdownResult = field(default_factory=RemotePreShutdownResult)
    error: str = ""
    timed_out: bool = False
    crashed: bool = False

    @property
    def success(self) -> bool:
        """Return True only when the final shutdown command was accepted.

        L6 (evaluated, intentional): for a loopback, ``crashed`` (a Phase-A
        pre-action crash) and ``shutdown_sent`` (the Phase-C poweroff succeeded)
        can BOTH be true -- the host drain partially failed but the poweroff
        still went out. Both flags are accurate; ``success`` deliberately treats
        a Phase-A crash as "not fully successful" so the summary surfaces the
        drain failure, even though the host did power off. This is a reporting
        nuance, not a shutdown defect, so it is left as-is.
        """
        return (
            self.completed
            and self.shutdown_sent
            and not self.timed_out
            and not self.crashed
            and not self.error
        )


def loopback_poweroff_sent(result: "RemoteShutdownResult") -> bool:
    """True when the Phase-C delegated host poweroff was actually delivered.

    ISS-005: unlike ``RemoteShutdownResult.success``, this deliberately IGNORES
    Phase-A drain failures (``crashed`` / ``error``). When a loopback delegate's
    pre-action crashes but the poweroff command still went out, the host IS
    powering off, so the sequence must be treated as complete (write the
    SHUTDOWN_SEQUENCE_COMPLETE marker, no failure notification) — the drain
    failure is a reporting nuance, not a missed shutdown. This is the shared
    predicate for the monitor and redundancy loopback paths, which previously
    diverged (monitor used ``all(r.success)`` and misclassified a delivered
    poweroff as failed).
    """
    return bool(
        result.completed
        and result.shutdown_sent
        and not result.timed_out
    )


def select_loopback_results(
    remote_servers: List[RemoteServerConfig],
    results: List["RemoteShutdownResult"],
) -> List["RemoteShutdownResult"]:
    """Return the subset of remote-shutdown ``results`` that belong to an
    enabled ``is_host_loopback`` server.

    A loopback entry's executed shutdown_command is what actually powers
    off THIS host, so the delegated-shutdown paths (single-UPS monitor and
    the redundancy executor) key their "did the poweroff go out?" decision
    on exactly these results. Matched by the ``(name-or-host, host)`` pair
    against the configured servers.

    ISS-013: previously duplicated verbatim as an inline list-comprehension
    in both ``monitor.py`` and ``redundancy.py`` — one source of truth now.
    """
    return [
        result for result in results
        if any(
            server.enabled
            and server.is_host_loopback is True
            and (server.name or server.host) == result.server
            and server.host == result.host
            for server in remote_servers
        )
    ]


class RemoteShutdownMixin:
    """Mixin: SSH-based remote-server orchestration and shutdown."""

    @staticmethod
    def _with_sudo(command: str, use_sudo: bool) -> str:
        """Prefix a remote command with sudo -n when configured.

        The check is intentionally idempotent so existing configs that
        already spell out ``sudo shutdown ...`` keep their exact command.
        """
        stripped = command.lstrip()
        if not use_sudo or stripped.startswith("sudo "):
            return command
        return f"sudo -n {command}"

    def _shutdown_remote_servers(self) -> List[RemoteShutdownResult]:
        """Shutdown all enabled remote servers via SSH.

        v5.5: ``is_host_loopback: true`` delegates always **bracket** the
        other remotes regardless of their ``shutdown_order``:

        1. Loopback ``pre_shutdown_commands`` (stop local VMs/containers,
           sync, unmount NFS that targets a peer remote) run FIRST so the
           local drain finishes while peer remotes are still alive.
        2. Non-loopback remotes (NAS, secondary host, …) run in the middle,
           grouped by ``shutdown_order`` exactly as in v5.4.
        3. Loopback ``shutdown_command`` (host poweroff) runs LAST so the
           eneru host outlives every remote it might have depended on.

        Why intrinsic and not user-configurable: pre-v5.5 the local-host
        phases (VMs/containers/sync/unmount) ran before any remote and the
        host poweroff ran after. v5.5 collapses the local work into a
        single loopback ``remote_servers`` entry; honouring its
        ``shutdown_order`` like a regular remote would mean the local
        drain races the peer remote shutdowns — that bug ate the v5.5.0-rc7
        controlled test on 2026-05-18 (NFS unmount hung 32s while the NAS
        was already powering off). See AGENTS.md "Loopback ordering".

        Non-loopback remotes follow v5.4 ordering: ``shutdown_order``
        ascending, ties broken by the legacy ``parallel`` flag:
        - parallel: true  (default) -> effective order 0
        - parallel: false -> unique negative orders (run before order 0)
        """
        # Imported lazily to avoid a circular import (monitor.py imports
        # this mixin).
        from eneru.monitor import compute_effective_order

        enabled_servers = [s for s in self.config.remote_servers if s.enabled]

        if not enabled_servers:
            return []

        # Partition: loopback delegates bracket the regulars.
        loopbacks = [s for s in enabled_servers if s.is_host_loopback is True]
        regulars = [s for s in enabled_servers if s.is_host_loopback is not True]

        # Phase plan for the header log.
        regular_phases: Dict[int, List[RemoteServerConfig]] = {}
        if regulars:
            ordered = compute_effective_order(regulars)
            for effective, server in ordered:
                regular_phases.setdefault(effective, []).append(server)
        sorted_regular_keys = sorted(regular_phases.keys())

        has_loopback_pre = any(lb.pre_shutdown_commands for lb in loopbacks)
        has_loopback_post = bool(loopbacks)
        num_phases = (
            (1 if has_loopback_pre else 0)
            + len(sorted_regular_keys)
            + (1 if has_loopback_post else 0)
        )

        server_count = len(enabled_servers)
        server_word = "server" if server_count == 1 else "servers"

        if num_phases > 1:
            self._log_message(
                f"🌐  Shutting down {server_count} remote {server_word} in {num_phases} phases..."
            )
        elif server_count > 1 and regulars:
            # "in parallel" is only honest when at least one regular is
            # in the batch — _shutdown_servers_parallel actually threads
            # them. A multi-loopback-only batch (rare, K8s-only) runs
            # Phase C with a plain for-loop over loopbacks, so don't
            # advertise parallelism that won't happen.
            self._log_message(
                f"🌐  Shutting down {server_count} remote servers in parallel..."
            )
        elif server_count > 1:
            self._log_message(
                f"🌐  Shutting down {server_count} remote servers..."
            )
        else:
            self._log_message("🌐  Shutting down 1 remote server...")

        # Pre-allocate one result per loopback so the pre-phase and the
        # post-phase write back into the same record (pre_commands +
        # shutdown_sent live on the same object).
        loopback_results: Dict[int, RemoteShutdownResult] = {}
        for lb in loopbacks:
            loopback_results[id(lb)] = RemoteShutdownResult(
                server=lb.name or lb.host,
                host=lb.host,
                pre_commands=RemotePreShutdownResult(),
            )

        # M2 NOTE: an earlier rc10 iteration ran a fresh, blocking
        # run_loopback_identity_probe() here before the destructive phases. It
        # was reverted: the probe is an SSH round-trip (bounded, but up to
        # connect_timeout+10s) that would delay the poweroff during an outage,
        # and we PROCEED regardless of its result, so it added latency on the
        # critical path for purely-informational value. Loopback identity is
        # already verified by the background RemoteHealthManager loop (which
        # logs + notifies a mismatch) and gated by /ready, so the misconfig is
        # surfaced there without blocking shutdown.
        phase_idx = 0
        regular_results: List[RemoteShutdownResult] = []

        # Phase A: loopback pre-actions — drain local state while peers live.
        if has_loopback_pre:
            # has_loopback_pre implies loopbacks exist, which implies
            # has_loopback_post is True, which means num_phases ≥ 2 by
            # construction. No need to gate the Phase header.
            phase_idx += 1
            names = ", ".join(
                lb.name or lb.host for lb in loopbacks if lb.pre_shutdown_commands
            )
            self._log_message(
                f"  📋  Phase {phase_idx}/{num_phases} (loopback pre-actions): {names}"
            )
            for lb in loopbacks:
                if not lb.pre_shutdown_commands:
                    continue
                display = lb.name or lb.host
                self._log_message(
                    f"🌐  Initiating loopback pre-actions: {display} ({lb.host})..."
                )
                # No phase-deadline here: loopback pre-actions are
                # best-effort and the per-command timeouts already cap
                # each step. A hung NFS unmount must not abort the
                # later peer-remote phase.
                #
                # try/except mirrors the thread-level guard in
                # _shutdown_servers_parallel: a bubbling Python
                # exception (SSH lib OOM, AttributeError, etc.) must
                # NOT skip the host poweroff in Phase C. Worst case
                # the operator loses the local drain on that loopback;
                # the host still goes down cleanly.
                try:
                    loopback_results[id(lb)].pre_commands = (
                        self._execute_remote_pre_shutdown(lb, collect_result=True)
                    )
                except Exception as exc:
                    self._log_message(
                        f"  ❌  Loopback pre-actions thread for {display} crashed: {exc}"
                    )
                    result = loopback_results[id(lb)]
                    result.pre_commands.error = str(exc)
                    result.pre_commands.failed += 1
                    result.crashed = True

        # Phase B: non-loopback remotes (existing parallel phased path).
        for key in sorted_regular_keys:
            phase_idx += 1
            phase_servers = regular_phases[key]
            names = ", ".join(s.name or s.host for s in phase_servers)
            if num_phases > 1:
                self._log_message(
                    f"  📋  Phase {phase_idx}/{num_phases} (order={key}): {names}"
                )
            regular_results.extend(self._shutdown_servers_parallel(phase_servers))

        # Phase C: loopback poweroff — host goes down LAST.
        if has_loopback_post:
            phase_idx += 1
            names = ", ".join(lb.name or lb.host for lb in loopbacks)
            if num_phases > 1:
                self._log_message(
                    f"  📋  Phase {phase_idx}/{num_phases} (loopback poweroff): {names}"
                )
            for lb in loopbacks:
                # Same try/except discipline as Phase A: a crash in one
                # loopback's poweroff must not skip the others (rare,
                # but possible in K8s multi-pod with several loopbacks).
                display = lb.name or lb.host
                try:
                    self._shutdown_loopback_command(lb, loopback_results[id(lb)])
                except Exception as exc:
                    self._log_message(
                        f"  ❌  Loopback poweroff thread for {display} crashed: {exc}"
                    )
                    result = loopback_results[id(lb)]
                    result.error = str(exc)
                    result.crashed = True

        results: List[RemoteShutdownResult] = (
            list(loopback_results.values()) + regular_results
        )

        # Log summary
        succeeded = sum(1 for result in results if result.success)
        timed_out = sum(1 for result in results if result.timed_out)
        crashed = sum(1 for result in results if result.crashed)
        failed = server_count - succeeded - timed_out - crashed
        icon = "✅" if succeeded == server_count else "⚠️"
        details = [f"{succeeded}/{server_count} succeeded"]
        if failed:
            details.append(f"{failed} failed")
        if timed_out:
            details.append(f"{timed_out} timed out")
        if crashed:
            details.append(f"{crashed} crashed")
        self._log_message(f"  {icon} Remote shutdown complete ({', '.join(details)})")
        return results

    def _shutdown_loopback_command(
        self,
        server: RemoteServerConfig,
        result: RemoteShutdownResult,
    ) -> None:
        """Execute ONLY the loopback's shutdown_command (no pre-actions).

        v5.5 loopback orchestration runs pre_shutdown_commands in an
        earlier phase (so peer remotes can outlive the local drain) and
        defers the host poweroff to this final phase. This helper
        completes the pre-allocated result record with the poweroff
        outcome.

        Why mutate the caller-provided ``result`` instead of returning
        a fresh one: a loopback's lifecycle spans two phases (A:
        pre-actions, C: poweroff) but produces ONE summary row in
        ``Remote shutdown complete (N/M succeeded)``. The caller
        pre-allocates ``result`` before Phase A so ``pre_commands`` and
        ``shutdown_sent`` land on the same object; returning a fresh
        partial here would split the record across two rows and
        confuse the summary. Mirror for regular remotes:
        ``_shutdown_remote_server`` does both pre and poweroff in a
        single call, so it can return its own result.
        """
        display = server.name or server.host
        self._log_message(
            f"🌐  Initiating loopback poweroff: {display} ({server.host})..."
        )

        # Fire the per-server notification BEFORE issuing the SSH
        # command. Symmetry with regular remotes (see
        # _shutdown_remote_server) and — critically for the loopback
        # case — the notification has to leave the host before the
        # host powers off. Notification recipients (Discord, Slack,
        # PagerDuty, …) live outside the host, so they receive it just
        # fine; the only thing they would miss is a notification fired
        # AFTER the kernel halt syscall.
        self._send_notification(
            f"🌐  **Remote Shutdown Starting:** {display}\n"
            f"Host: {server.host}",
            self.config.NOTIFY_INFO,
            category="shutdown",
        )

        shutdown_command = self._with_sudo(server.shutdown_command, server.use_sudo)
        self._log_message(f"  🔌  Sending shutdown command: {shutdown_command}")

        if self.config.behavior.dry_run:
            result.shutdown_sent = True
            result.dry_run = True
            self._log_message(
                f"  🧪  [DRY-RUN] Would send command '{shutdown_command}' to "
                f"{server.user}@{server.host}"
            )
            return

        success, error_msg = self._run_remote_command(
            server,
            shutdown_command,
            server.command_timeout,
            "shutdown",
            is_final_shutdown=True,
        )

        if success:
            result.shutdown_sent = True
            if error_msg:
                # F-077: sent but unconfirmed (SSH transport tore down before
                # the exit status came back). shutdown_sent is still True so
                # the marker is written and no false-incomplete alert fires.
                self._log_message(
                    f"  ✅  {display} shutdown command sent ({error_msg})"
                )
            else:
                self._log_message(f"  ✅  {display} shutdown command sent successfully")
        else:
            result.error = error_msg
            self._log_message(
                f"  ❌  WARNING: Failed to execute shutdown command on {display}: {error_msg}"
            )
            self._send_notification(
                f"❌  **Remote Shutdown Failed:** {display}\nError: {error_msg}",
                self.config.NOTIFY_FAILURE,
                category="shutdown",
            )

    def _shutdown_servers_parallel(
        self, servers: List[RemoteServerConfig]
    ) -> List[RemoteShutdownResult]:
        """Shutdown multiple remote servers in parallel using threads.

        Returns one structured result per server.  The join is deadline-based
        so a stuck SSH call cannot multiply the wait by the number of servers.
        """
        def calc_server_timeout(server: RemoteServerConfig) -> int:
            # Explicit None check so a per-command timeout of 0 (e.g. for
            # a command the user wants to fire-and-forget) isn't promoted
            # to server.command_timeout via Python's truthiness.
            pre_cmd_time = sum(
                (cmd.timeout if cmd.timeout is not None else server.command_timeout)
                for cmd in server.pre_shutdown_commands
            )
            # _run_remote_command spends `timeout + _SSH_OVERHEAD_BUFFER` on EACH
            # command (every pre-shutdown command plus the final shutdown). The
            # phase-deadline budget must reserve that buffer per command too
            # (CodeRabbit), otherwise the parallel join deadline can expire while
            # a worker is still legitimately inside its own SSH timeout and the
            # server is wrongly reported timed-out.
            num_commands = len(server.pre_shutdown_commands) + 1
            return (
                pre_cmd_time
                + server.command_timeout
                + server.connect_timeout
                + server.shutdown_safety_margin
                + num_commands * _SSH_OVERHEAD_BUFFER
            )

        max_timeout = max(calc_server_timeout(s) for s in servers)

        results: Dict[threading.Thread, RemoteShutdownResult] = {}
        lock = threading.Lock()

        def default_result(
            server: RemoteServerConfig,
            *,
            completed: bool = True,
            error: str = "",
            timed_out: bool = False,
            crashed: bool = False,
        ) -> RemoteShutdownResult:
            return RemoteShutdownResult(
                server=server.name or server.host,
                host=server.host,
                completed=completed,
                shutdown_sent=False,
                pre_commands=RemotePreShutdownResult(),
                error=error,
                timed_out=timed_out,
                crashed=crashed,
            )

        def coerce_result(server: RemoteServerConfig, result) -> RemoteShutdownResult:
            if isinstance(result, RemoteShutdownResult):
                return result
            # Backward-compatible test hook path: older tests monkeypatch
            # _shutdown_remote_server with a function that returns None.
            coerced = default_result(server)
            coerced.shutdown_sent = True
            return coerced

        def shutdown_server_thread(server: RemoteServerConfig):
            """Thread worker for shutting down a single server."""
            result = None
            try:
                try:
                    result = self._shutdown_remote_server(server, deadline=deadline)
                except TypeError as exc:
                    if "unexpected keyword argument 'deadline'" not in str(exc):
                        raise
                    # Backward-compatible test hook path: older tests
                    # monkeypatch _shutdown_remote_server with a function
                    # that accepts only the server argument.
                    result = self._shutdown_remote_server(server)
                result = coerce_result(server, result)
            except Exception as exc:
                # _shutdown_remote_server only catches SSH-style errors;
                # bubbling exceptions (network, AttributeError, OOM…) would
                # otherwise vanish silently in the worker thread.
                display = server.name or server.host
                self._log_message(
                    f"  ❌  Remote shutdown thread for {display} crashed: {exc}"
                )
                result = default_result(server, error=str(exc), crashed=True)
            with lock:
                results[threading.current_thread()] = result

        deadline = time.monotonic() + max_timeout
        threads: List[threading.Thread] = []
        for server in servers:
            t = threading.Thread(
                target=shutdown_server_thread,
                args=(server,),
                name=f"remote-shutdown-{server.name or server.host}",
                daemon=True,
            )
            t.start()
            threads.append(t)

        # Deadline-based join: cap total wait at max_timeout regardless of
        # how many threads are stuck. Per-thread join() with the same
        # max_timeout would stack to N × max_timeout in the worst case.
        for t in threads:
            remaining = max(0.0, deadline - time.monotonic())
            t.join(timeout=remaining)

        still_running = [t for t in threads if t.is_alive()]
        if still_running:
            self._log_message(
                f"  ⚠️  {len(still_running)} remote shutdown(s) still in progress "
                "(continuing with next phase)"
            )

        final_results: List[RemoteShutdownResult] = []
        with lock:
            for thread, server in zip(threads, servers):
                result = results.get(thread)
                if result is None:
                    result = default_result(
                        server,
                        completed=False,
                        timed_out=True,
                        error="remote shutdown worker timed out",
                    )
                final_results.append(result)
        return final_results

    def _run_remote_command(
        self,
        server: RemoteServerConfig,
        command: str,
        timeout: int,
        description: str,
        *,
        deadline: Optional[float] = None,
        is_final_shutdown: bool = False,
    ) -> Tuple[bool, str]:
        """Run a single command on a remote server via SSH.

        ``is_final_shutdown`` marks the ONE poweroff command (not
        pre_shutdown_commands): only then is an exit-255 transport teardown
        treated as "sent (unconfirmed)" rather than a failure (F-077). On
        success the second tuple element is normally "" but carries a
        human-readable note ("SSH transport ended (result unknown)") for that
        unconfirmed case so callers can log it honestly.

        Returns:
            Tuple of (success, error_or_note)
        """
        display_name = server.name or server.host

        ssh_cmd = ["ssh"]

        if server.ssh_key_path:
            ssh_cmd.extend(["-i", server.ssh_key_path])

        # Add configured SSH options. Three cases:
        #   1. "-o KEY=VALUE" / "-o KEY VALUE" (single string with space):
        #      split into two argv entries so ssh's getopt parser sees
        #      flag and value separately.
        #   2. Any other "-flag …" form (e.g. "-i", "-p"): pass through
        #      unchanged. Multi-token flags like "-i /path/key" must be
        #      provided as separate ssh_options entries by the user.
        #   3. Bare "KEY=VALUE": prepend "-o" as the implicit form.
        for opt in [*eneru_utils.runtime_default_ssh_options(server.ssh_options),
                    *server.ssh_options]:
            if opt.startswith("-o "):
                ssh_cmd.extend(opt.split(None, 1))
            elif opt.startswith("-"):
                ssh_cmd.append(opt)
            else:
                ssh_cmd.extend(["-o", opt])

        # Single lowest common injection point: BOTH the final shutdown_command
        # and every pre_shutdown_commands entry (custom commands AND the
        # REMOTE_ACTIONS templates), across the normal remote path AND the
        # loopback-to-127.0.0.1 path, funnel through here before ssh. Prepend
        # the PATH augmentation to the ONE argv element that carries the remote
        # command string so it stays a single element (argv structure intact:
        # `ssh <opts> user@host "<prefix><command>"`). The LOCAL, non-SSH
        # execution paths never reach _run_remote_command, so they are
        # correctly left untouched.
        #
        # F-080: the prefix is a POSIX-sh `export PATH=...` statement. On a
        # csh/tcsh remote (FreeBSD/TrueNAS CORE root) it emits noisy
        # "export: Command not found." lines; on a cmd.exe/Windows remote `;`
        # isn't a separator, so the ENTIRE line becomes args to a nonexistent
        # `export` and the real shutdown silently never runs. Per-server
        # `augment_remote_path` is opt-in because Eneru cannot reliably infer a
        # remote login shell before it sends the command. POSIX targets that
        # need bare commands can enable it; every other shell stays verbatim.
        remote_command = (
            REMOTE_PATH_PREFIX + command
            if server.augment_remote_path
            else command
        )
        ssh_cmd.extend([
            "-o", f"ConnectTimeout={server.connect_timeout}",
            "-o", "BatchMode=yes",  # Prevent password prompts from hanging
            f"{server.user}@{server.host}",
            remote_command,
        ])

        # Add buffer to account for SSH connection overhead, unless a
        # shutdown-phase deadline requires a tighter cap. This prevents a
        # hung pre-shutdown command from outliving the phase and later
        # drifting into the final shutdown command.
        command_timeout = timeout + _SSH_OVERHEAD_BUFFER
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False, "remote shutdown deadline exceeded"
            command_timeout = max(1, min(command_timeout, int(remaining)))
        exit_code, stdout, stderr = run_command(ssh_cmd, timeout=command_timeout)

        if exit_code == 0:
            return True, ""
        elif exit_code == 124:
            # ``command_timeout`` is the value actually handed to
            # ``run_command`` (timeout + SSH overhead buffer, capped by the
            # phase deadline). Reporting just ``timeout`` would mislead
            # operators when deadline-capping shrinks the effective
            # window below what was configured per-command.
            if command_timeout != timeout + _SSH_OVERHEAD_BUFFER:
                return (
                    False,
                    f"timed out after {command_timeout}s "
                    f"(configured {timeout}s, capped by phase deadline)",
                )
            return False, f"timed out after {timeout}s"
        elif (
            is_final_shutdown
            and exit_code == 255
            and _is_ssh_transport_teardown(stderr)
        ):
            # F-077: the remote accepted the poweroff and sshd died before
            # returning status. Report "sent (unconfirmed)" so the
            # loopback-delegate path writes the completion marker and skips the
            # false "sequence incomplete" alert. Scoped to the final poweroff
            # and a transport-teardown stderr — a mid-sequence drop, a non-255
            # code, or "Permission denied" all stay failures.
            return True, "SSH transport ended (result unknown)"
        else:
            error_msg = stderr.strip() if stderr.strip() else f"exit code {exit_code}"
            return False, error_msg

    @staticmethod
    def _remote_deadline_exceeded(deadline: Optional[float] = None) -> bool:
        """Return True when a remote shutdown phase deadline has expired."""
        return deadline is not None and time.monotonic() >= deadline

    def _loopback_skip_ids(self):
        """Return the set of container IDs the host must skip during a
        loopback-delegated container/compose shutdown.

        Source of truth is ``ContainerShutdownMixin._current_container_ids``
        (also mixed into ``UPSGroupMonitor``). When the loopback host runs
        ``stop_containers`` or ``stop_compose``, these IDs are filtered out
        so Eneru's own container isn't killed mid-sequence. Returns an
        empty set on bare-metal (the mixin returns ``set()`` outside a
        container).
        """
        get_ids = getattr(self, "_current_container_ids", None)
        if get_ids is None:
            return set()
        try:
            return get_ids()
        except Exception:
            return set()

    def _loopback_umount_targets(self):
        """Return the unmount-mounts config for the local owner group.

        Loopback delegation: ``unmount_filesystems`` runs on the host with
        the operator's per-mount options. We pull the list straight from
        the local group's filesystems config — the operator declared it
        once in the normal place.
        """
        group = self.config.ups_groups[0] if self.config.ups_groups else None
        if group is None or not group.filesystems.unmount.enabled:
            return []
        return list(group.filesystems.unmount.mounts)

    def _execute_remote_pre_shutdown(
        self,
        server: RemoteServerConfig,
        *,
        collect_result: bool = False,
        deadline: Optional[float] = None,
    ):
        """Execute pre-shutdown commands on a remote server.

        Returns:
            True once the loop has iterated through every command.
            Per-command failures are logged and execution continues
            (best-effort) — there is no current code path that returns
            False.
        """
        if not server.pre_shutdown_commands:
            if collect_result:
                return RemotePreShutdownResult()
            return True

        display_name = server.name or server.host
        cmd_count = len(server.pre_shutdown_commands)
        result = RemotePreShutdownResult()

        self._log_message(f"  📋  Executing {cmd_count} pre-shutdown command(s)...")

        for idx, cmd_config in enumerate(server.pre_shutdown_commands, 1):
            if self._remote_deadline_exceeded(deadline):
                result.timed_out = True
                result.error = "remote shutdown deadline exceeded before pre-shutdown completed"
                self._log_message(
                    f"    ⚠️  [{idx}/{cmd_count}] Skipping remaining pre-shutdown "
                    "commands: remote shutdown deadline exceeded"
                )
                break

            # Determine timeout
            timeout = cmd_config.timeout
            if timeout is None:
                timeout = server.command_timeout

            # Handle predefined action
            if cmd_config.action:
                action_name = cmd_config.action.lower()

                if action_name not in REMOTE_ACTIONS:
                    self._log_message(
                        f"    ⚠️  [{idx}/{cmd_count}] Unknown action: {action_name} (skipping)"
                    )
                    result.failed += 1
                    continue

                # Validate stop_compose has path BEFORE rendering the
                # template; otherwise the precondition warning becomes
                # dead code; a future template change might let the bad
                # command slip through.
                if action_name == "stop_compose" and not cmd_config.path:
                    self._log_message(
                        f"    ⚠️  [{idx}/{cmd_count}] stop_compose requires 'path' parameter (skipping)"
                    )
                    result.failed += 1
                    continue

                # Get command template and substitute placeholders.
                # render_action() owns shell quoting for path-bearing
                # templates before they enter the remote shell.
                # v5.5: loopback delegate gets extra context — the
                # mandatory self-skip set so 'stop_containers' /
                # 'stop_compose' don't kill Eneru's own container, and
                # the umount targets serialized for 'unmount_filesystems'.
                skip_ids = ""
                umount_targets = ""
                if server.is_host_loopback is True:
                    skip_ids = ",".join(sorted(self._loopback_skip_ids()))
                    if action_name == "unmount_filesystems":
                        umount_targets = serialize_umount_targets(
                            self._loopback_umount_targets()
                        )
                elif action_name == "unmount_filesystems":
                    umount_targets = serialize_umount_targets(cmd_config.mounts)
                    if not umount_targets:
                        self._log_message(
                            f"    ⚠️  [{idx}/{cmd_count}] unmount_filesystems "
                            "requires 'mounts' on regular remote servers (skipping)"
                        )
                        result.failed += 1
                        continue
                command = render_action(
                    action_name,
                    timeout=timeout,
                    path=cmd_config.path or "",
                    skip_ids=skip_ids,
                    umount_targets=umount_targets,
                    use_sudo=server.use_sudo,
                )
                description = action_name

            # Handle custom command
            elif cmd_config.command:
                command = cmd_config.command
                # Truncate long commands for display
                if len(command) > 50:
                    description = command[:47] + "..."
                else:
                    description = command

            else:
                self._log_message(
                    f"    ⚠️  [{idx}/{cmd_count}] No action or command specified (skipping)"
                )
                result.failed += 1
                continue

            # Log what we're about to do
            self._log_message(f"    ➡️  [{idx}/{cmd_count}] {description} (timeout: {timeout}s)")
            result.attempted += 1

            if self.config.behavior.dry_run:
                self._log_message(f"    🧪  [DRY-RUN] Would execute on {display_name}")
                continue

            # Execute the command
            success, error_msg = self._run_remote_command(
                server, command, timeout, description, deadline=deadline
            )

            if success:
                self._log_message(f"    ✅  [{idx}/{cmd_count}] {description} completed")
            else:
                result.failed += 1
                self._log_message(
                    f"    ⚠️  [{idx}/{cmd_count}] {description} failed: {error_msg} (continuing)"
                )

            if self._remote_deadline_exceeded(deadline):
                result.timed_out = True
                result.error = "remote shutdown deadline exceeded during pre-shutdown"
                self._log_message(
                    "    ⚠️  Remote shutdown deadline reached during pre-shutdown; "
                    "final shutdown command will not be sent"
                )
                break

        if collect_result:
            return result
        return True

    def _shutdown_remote_server(
        self,
        server: RemoteServerConfig,
        *,
        deadline: Optional[float] = None,
    ) -> RemoteShutdownResult:
        """Shutdown a single remote server via SSH.

        Execution order:
        1. Execute pre_shutdown_commands (if any) - best effort
        2. Execute shutdown_command
        """
        display_name = server.name or server.host
        has_pre_cmds = len(server.pre_shutdown_commands) > 0
        result = RemoteShutdownResult(
            server=display_name,
            host=server.host,
            pre_commands=RemotePreShutdownResult(),
        )

        self._log_message(f"🌐  Initiating remote shutdown: {display_name} ({server.host})...")

        # Send notification for remote server shutdown start
        self._send_notification(
            f"🌐  **Remote Shutdown Starting:** {display_name}\n"
            f"Host: {server.host}",
            self.config.NOTIFY_INFO,
            category="shutdown",
        )

        # Execute pre-shutdown commands first. RESERVE enough of the phase
        # deadline for the final shutdown command (its own timeout + the SSH
        # overhead buffer _run_remote_command adds) so a slow pre-shutdown phase
        # can NEVER starve or skip the actual poweroff (H8). Pre-commands run
        # against the reduced deadline; the poweroff then runs against the full
        # deadline, spending the reserved slice. When the reserve exceeds the
        # whole budget, pre-commands get no time at all -- the poweroff wins.
        if has_pre_cmds:
            pre_deadline = deadline
            if deadline is not None:
                final_reserve = server.command_timeout + _SSH_OVERHEAD_BUFFER
                pre_deadline = deadline - final_reserve
            result.pre_commands = self._execute_remote_pre_shutdown(
                server, collect_result=True, deadline=pre_deadline,
            )

        # Skip the poweroff ONLY when the FULL phase deadline is blown. A
        # pre-phase that merely exhausted its reserved slice (pre_commands
        # .timed_out) must NOT cancel the poweroff -- reserving its budget is
        # precisely what stops the starvation H8 fixes. Pre-phase failures are
        # still recorded in result.pre_commands for the summary.
        if self._remote_deadline_exceeded(deadline):
            result.completed = False
            result.timed_out = True
            result.error = (
                result.pre_commands.error
                or "remote shutdown deadline exceeded before final shutdown command"
            )
            self._log_message(
                f"  ⚠️  Skipping final shutdown command for {display_name}: {result.error}"
            )
            return result

        # Execute final shutdown command
        shutdown_command = self._with_sudo(
            server.shutdown_command,
            server.use_sudo,
        )
        self._log_message(f"  🔌  Sending shutdown command: {shutdown_command}")

        if self.config.behavior.dry_run:
            result.shutdown_sent = True
            result.dry_run = True
            self._log_message(
                f"  🧪  [DRY-RUN] Would send command '{shutdown_command}' to "
                f"{server.user}@{server.host}"
            )
            return result

        success, error_msg = self._run_remote_command(
            server,
            shutdown_command,
            server.command_timeout,
            "shutdown",
            deadline=deadline,
            is_final_shutdown=True,
        )

        if success:
            result.shutdown_sent = True
            if error_msg:
                # F-077: sent but unconfirmed (SSH transport tore down before
                # the exit status came back). Treated as sent, not failed.
                self._log_message(
                    f"  ✅  {display_name} shutdown command sent ({error_msg})"
                )
            else:
                self._log_message(f"  ✅  {display_name} shutdown command sent successfully")
            # Per-server success used to fire a notification too; dropped in
            # v5.2 — the "Starting" notification is the per-server signal,
            # the aggregate "Sequence Complete" rolls up the result, and
            # journalctl carries the full trace. Failures still notify
            # because they're the only thing the user actually needs to act
            # on individually.
        else:
            result.error = error_msg
            self._log_message(
                f"  ❌  WARNING: Failed to execute shutdown command on {display_name}: {error_msg}"
            )
            self._send_notification(
                f"❌  **Remote Shutdown Failed:** {display_name}\n"
                f"Error: {error_msg}",
                self.config.NOTIFY_FAILURE,
                category="shutdown",
            )
        return result
