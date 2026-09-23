#!/usr/bin/env python3
"""Drive `eneru config` (curses TUI) through a pseudo-terminal for E2E.

Usage: config-tui-driver.py KEYS -- eneru config -c FILE --basic

KEYS is a Python string literal of keystrokes (escapes allowed, e.g.
"2\\r" or "\\x15TestUPS\\r"); "|" pauses between chunks so the TUI can
redraw and run its checks. Stdlib only: runs on the plain CI runner.
Exits with the editor's own exit code (124 if it never quit).
"""

import os
import pty
import select
import signal
import struct
import sys
import time


def main() -> int:
    if len(sys.argv) < 4 or sys.argv[2] != "--":
        print(__doc__)
        return 2
    keys = sys.argv[1].encode("utf-8").decode("unicode_escape")
    argv = sys.argv[3:]
    pid, fd = pty.fork()
    if pid == 0:
        os.environ["TERM"] = "xterm-256color"
        os.execvp(argv[0], argv)
    import fcntl
    import termios
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 140, 0, 0))

    def pump(seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            ready, _, _ = select.select([fd], [], [], 0.05)
            if ready:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    return
                if not data:
                    return
                sys.stdout.buffer.write(data)

    pump(3.0)
    for chunk in keys.split("|"):
        os.write(fd, chunk.encode())
        pump(2.0)
    deadline = time.time() + 15
    while time.time() < deadline:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            pump(0.2)
            return os.waitstatus_to_exitcode(status) if hasattr(
                os, "waitstatus_to_exitcode") else (status >> 8)
        pump(0.5)
    os.kill(pid, signal.SIGKILL)
    return 124


if __name__ == "__main__":
    sys.exit(main())
