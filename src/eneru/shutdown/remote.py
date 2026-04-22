"""SSH-based remote-server shutdown phase.

Owns the multi-server orchestration (sequential vs parallel batching by
``shutdown_order``) plus the per-server lifecycle: ``pre_shutdown_commands``
followed by the final shutdown command.
"""

import shlex
import threading
import time
from typing import Dict, List, Tuple

from eneru.actions import REMOTE_ACTIONS
from eneru.config import RemoteServerConfig
from eneru.utils import run_command


class RemoteShutdownMixin:
    """Mixin: SSH-based remote-server orchestration and shutdown."""

    def _shutdown_remote_servers(self):
        """Shutdown all enabled remote servers via SSH.

        Servers are grouped by their effective shutdown order and processed
        in ascending order.  All servers within a group run in parallel.
        A server alone in its group effectively runs sequentially.

        When shutdown_order is not set, the legacy parallel flag determines
        effective order:
        - parallel: true  (default) -> effective order 0
        - parallel: false -> unique negative orders (run before order 0)
        This preserves exact backward compatibility with existing configs.
        """
        # Imported lazily to avoid a circular import (monitor.py imports
        # this mixin).
        from eneru.monitor import compute_effective_order

        enabled_servers = [s for s in self.config.remote_servers if s.enabled]

        if not enabled_servers:
            return

        # Group servers by effective shutdown order
        ordered = compute_effective_order(enabled_servers)
        phases: Dict[int, List[RemoteServerConfig]] = {}
        for effective, server in ordered:
            phases.setdefault(effective, []).append(server)
        sorted_keys = sorted(phases.keys())

        server_count = len(enabled_servers)
        num_phases = len(sorted_keys)

        if num_phases > 1:
            self._log_message(
                f"🌐 Shutting down {server_count} remote server(s) in {num_phases} phases..."
            )
        elif server_count > 1:
            self._log_message(f"🌐 Shutting down {server_count} remote server(s) in parallel...")
        else:
            self._log_message(f"🌐 Shutting down 1 remote server...")

        completed = 0

        for phase_idx, key in enumerate(sorted_keys, 1):
            phase_servers = phases[key]
            names = ", ".join(s.name or s.host for s in phase_servers)

            if num_phases > 1:
                self._log_message(f"  📋 Phase {phase_idx}/{num_phases} (order={key}): {names}")

            if len(phase_servers) == 1:
                server = phase_servers[0]
                display_name = server.name or server.host
                try:
                    self._shutdown_remote_server(server)
                    completed += 1
                except Exception as e:
                    self._log_message(f"  ❌ {display_name} shutdown failed: {e}")
            else:
                completed += self._shutdown_servers_parallel(phase_servers)

        # Log summary
        self._log_message(f"  ✅ Remote shutdown complete ({completed}/{server_count} servers)")

    def _shutdown_servers_parallel(self, servers: List[RemoteServerConfig]) -> int:
        """Shutdown multiple remote servers in parallel using threads.

        Returns the number of servers whose threads finished within the
        timeout window (regardless of individual success/failure — per-server
        errors are logged inside _shutdown_remote_server).
        """
        def calc_server_timeout(server: RemoteServerConfig) -> int:
            # Explicit None check so a per-command timeout of 0 (e.g. for
            # a command the user wants to fire-and-forget) isn't promoted
            # to server.command_timeout via Python's truthiness.
            pre_cmd_time = sum(
                (cmd.timeout if cmd.timeout is not None else server.command_timeout)
                for cmd in server.pre_shutdown_commands
            )
            return (
                pre_cmd_time
                + server.command_timeout
                + server.connect_timeout
                + server.shutdown_safety_margin
            )

        max_timeout = max(calc_server_timeout(s) for s in servers)

        def shutdown_server_thread(server: RemoteServerConfig):
            """Thread worker for shutting down a single server."""
            try:
                self._shutdown_remote_server(server)
            except Exception as exc:
                # _shutdown_remote_server only catches SSH-style errors;
                # bubbling exceptions (network, AttributeError, OOM…) would
                # otherwise vanish silently in the worker thread.
                display = server.name or server.host
                self._log_message(
                    f"  ❌ Remote shutdown thread for {display} crashed: {exc}"
                )

        threads: List[threading.Thread] = []
        for server in servers:
            t = threading.Thread(
                target=shutdown_server_thread,
                args=(server,),
                name=f"remote-shutdown-{server.name or server.host}"
            )
            t.start()
            threads.append(t)

        # Deadline-based join: cap total wait at max_timeout regardless of
        # how many threads are stuck. Per-thread join() with the same
        # max_timeout would stack to N × max_timeout in the worst case.
        deadline = time.monotonic() + max_timeout
        for t in threads:
            remaining = max(0.0, deadline - time.monotonic())
            t.join(timeout=remaining)

        still_running = [t for t in threads if t.is_alive()]
        if still_running:
            self._log_message(
                f"  ⚠️ {len(still_running)} remote shutdown(s) still in progress "
                "(continuing with next phase)"
            )

        return len(servers) - len(still_running)

    def _run_remote_command(
        self,
        server: RemoteServerConfig,
        command: str,
        timeout: int,
        description: str
    ) -> Tuple[bool, str]:
        """Run a single command on a remote server via SSH.

        Returns:
            Tuple of (success, error_message)
        """
        display_name = server.name or server.host

        ssh_cmd = ["ssh"]

        # Add configured SSH options. Three cases:
        #   1. "-o KEY=VALUE" / "-o KEY VALUE" (single string with space):
        #      split into two argv entries so ssh's getopt parser sees
        #      flag and value separately.
        #   2. Any other "-flag …" form (e.g. "-i", "-p"): pass through
        #      unchanged. Multi-token flags like "-i /path/key" must be
        #      provided as separate ssh_options entries by the user.
        #   3. Bare "KEY=VALUE": prepend "-o" as the implicit form.
        for opt in server.ssh_options:
            if opt.startswith("-o "):
                ssh_cmd.extend(opt.split(None, 1))
            elif opt.startswith("-"):
                ssh_cmd.append(opt)
            else:
                ssh_cmd.extend(["-o", opt])

        ssh_cmd.extend([
            "-o", f"ConnectTimeout={server.connect_timeout}",
            "-o", "BatchMode=yes",  # Prevent password prompts from hanging
            f"{server.user}@{server.host}",
            command
        ])

        # Add buffer to timeout to account for SSH connection overhead
        exit_code, stdout, stderr = run_command(ssh_cmd, timeout=timeout + 30)

        if exit_code == 0:
            return True, ""
        elif exit_code == 124:
            return False, f"timed out after {timeout}s"
        else:
            error_msg = stderr.strip() if stderr.strip() else f"exit code {exit_code}"
            return False, error_msg

    def _execute_remote_pre_shutdown(self, server: RemoteServerConfig) -> bool:
        """Execute pre-shutdown commands on a remote server.

        Returns:
            True once the loop has iterated through every command.
            Per-command failures are logged and execution continues
            (best-effort) — there is no current code path that returns
            False.
        """
        if not server.pre_shutdown_commands:
            return True

        display_name = server.name or server.host
        cmd_count = len(server.pre_shutdown_commands)

        self._log_message(f"  📋 Executing {cmd_count} pre-shutdown command(s)...")

        for idx, cmd_config in enumerate(server.pre_shutdown_commands, 1):
            # Determine timeout
            timeout = cmd_config.timeout
            if timeout is None:
                timeout = server.command_timeout

            # Handle predefined action
            if cmd_config.action:
                action_name = cmd_config.action.lower()

                if action_name not in REMOTE_ACTIONS:
                    self._log_message(
                        f"    ⚠️ [{idx}/{cmd_count}] Unknown action: {action_name} (skipping)"
                    )
                    continue

                # Get command template and substitute placeholders.
                # `path` is shlex-quoted because the template embeds it
                # directly into the remote shell — without quoting, a
                # malicious or malformed path could expand $(), `…`, or
                # ${…} on the remote host.
                command_template = REMOTE_ACTIONS[action_name]
                command = command_template.format(
                    timeout=timeout,
                    path=shlex.quote(cmd_config.path or "")
                )
                description = action_name

                # Validate stop_compose has path
                if action_name == "stop_compose" and not cmd_config.path:
                    self._log_message(
                        f"    ⚠️ [{idx}/{cmd_count}] stop_compose requires 'path' parameter (skipping)"
                    )
                    continue

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
                    f"    ⚠️ [{idx}/{cmd_count}] No action or command specified (skipping)"
                )
                continue

            # Log what we're about to do
            self._log_message(f"    ➡️ [{idx}/{cmd_count}] {description} (timeout: {timeout}s)")

            if self.config.behavior.dry_run:
                self._log_message(f"    🧪 [DRY-RUN] Would execute on {display_name}")
                continue

            # Execute the command
            success, error_msg = self._run_remote_command(
                server, command, timeout, description
            )

            if success:
                self._log_message(f"    ✅ [{idx}/{cmd_count}] {description} completed")
            else:
                self._log_message(
                    f"    ⚠️ [{idx}/{cmd_count}] {description} failed: {error_msg} (continuing)"
                )

        return True

    def _shutdown_remote_server(self, server: RemoteServerConfig):
        """Shutdown a single remote server via SSH.

        Execution order:
        1. Execute pre_shutdown_commands (if any) - best effort
        2. Execute shutdown_command
        """
        display_name = server.name or server.host
        has_pre_cmds = len(server.pre_shutdown_commands) > 0

        self._log_message(f"🌐 Initiating remote shutdown: {display_name} ({server.host})...")

        # Send notification for remote server shutdown start
        self._send_notification(
            f"🌐 **Remote Shutdown Starting:** {display_name}\n"
            f"Host: {server.host}",
            self.config.NOTIFY_INFO
        )

        # Execute pre-shutdown commands first
        if has_pre_cmds:
            self._execute_remote_pre_shutdown(server)

        # Execute final shutdown command
        self._log_message(f"  🔌 Sending shutdown command: {server.shutdown_command}")

        if self.config.behavior.dry_run:
            self._log_message(
                f"  🧪 [DRY-RUN] Would send command '{server.shutdown_command}' to "
                f"{server.user}@{server.host}"
            )
            return

        success, error_msg = self._run_remote_command(
            server,
            server.shutdown_command,
            server.command_timeout,
            "shutdown"
        )

        if success:
            self._log_message(f"  ✅ {display_name} shutdown command sent successfully")
            self._send_notification(
                f"✅ **Remote Shutdown Sent:** {display_name}\n"
                f"Server is shutting down.",
                self.config.NOTIFY_SUCCESS
            )
        else:
            self._log_message(
                f"  ❌ WARNING: Failed to execute shutdown command on {display_name}: {error_msg}"
            )
            self._send_notification(
                f"❌ **Remote Shutdown Failed:** {display_name}\n"
                f"Error: {error_msg}",
                self.config.NOTIFY_FAILURE
            )
