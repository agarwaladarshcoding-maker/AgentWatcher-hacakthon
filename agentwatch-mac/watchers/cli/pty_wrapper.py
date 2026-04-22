#!/usr/bin/env python3
"""
AgentWatch — pty_wrapper.py  v6.0

Wraps a CLI AI agent (gemini, claude, aider, ollama, etc.) in a PTY so we can:
  1. Transparently pass all keyboard input / terminal output to the real process.
  2. Detect when the agent has finished producing a response (quiescence detection).
  3. Capture the last N characters of that response as a snippet.
  4. Fire notify.py to show the notification card.

Usage (called by agentwatch.zsh _aw_agent_wrap):
  python3 pty_wrapper.py --sid SID --tty TTY --term TERM --sess SESS \
                         --notify PATH_TO_NOTIFY \
                         --site "iTerm · myproject" \
                         -- gemini --model gemini-2.0-flash "hello"

Design:
  • Pure stdlib — no extra deps.
  • Works on macOS (BSD pty) and Linux.
  • Quiescence = no new output bytes for QUIET_SECS seconds AND at least
    MIN_RESPONSE_CHARS chars received since last user input.
  • Fires at most once per "agent turn" (resets on new stdin activity).
  • After firing, keeps watching — multi-turn conversations work.
  • Exit: when child exits, fire a final notification if there's unfired output.
"""

import argparse
import datetime
import fcntl
import json
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import tty

# ── Tuning constants ──────────────────────────────────────────────────────────
QUIET_SECS          = 2.0   # silence after which we consider agent "done"
MIN_RESPONSE_CHARS  = 40    # minimum chars before we fire
SNIPPET_MAX_CHARS   = 3000  # how much response text to send to notify.py
READ_CHUNK          = 4096
NOTIFY_COOLDOWN_S   = 3.0   # don't re-fire within this many seconds
LOG_PATH            = os.path.expanduser("~/.agentwatch/notify.log")

# ── ANSI escape stripper ──────────────────────────────────────────────────────
_ANSI_RE = re.compile(
    r'(\x9B|\x1B\[)[0-?]*[ -/]*[@-~]'    # CSI sequences
    r'|\x1B[@-_]'                          # 2-char escapes
    r'|\x1B\[[0-9;]*[mGKHFABCDJsu]'       # SGR + cursor
    r'|\r',                                # bare CR
    re.VERBOSE
)

def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub('', text)


def _log(msg: str):
    stamp = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[pty_wrap {stamp}] {msg}"
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── Notification firing ───────────────────────────────────────────────────────
def _fire_notification(notify_py: str, title: str, site: str, ev_type: str,
                        snippet: str, sid: str, tty_dev: str,
                        term_prog: str, term_sess: str, ws_port: str = "59452"):
    """Launch notify.py in background. Non-blocking."""
    if not notify_py or not os.path.exists(notify_py):
        _log(f"notify.py not found at {notify_py!r}")
        return

    # Trim snippet to avoid huge args
    snip = snippet.strip()[-SNIPPET_MAX_CHARS:] if snippet else ""

    cmd = [
        sys.executable, notify_py,
        title, site, ev_type, snip,
        ws_port, "", "",           # ws_port, tab_id, window_id
        tty_dev, term_prog, term_sess, sid,
    ]
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        _log(f"fired notify: title={title!r} ev={ev_type} snippet_len={len(snip)}")
    except Exception as e:
        _log(f"failed to fire notify: {e}")


# ── PTY wrapper core ──────────────────────────────────────────────────────────
class PTYWrapper:
    def __init__(self, cmd: list, notify_py: str, site: str, sid: str,
                 tty_dev: str, term_prog: str, term_sess: str, ws_port: str,
                 agent_name: str):
        self.cmd       = cmd
        self.notify_py = notify_py
        self.site      = site
        self.sid       = sid
        self.tty_dev   = tty_dev
        self.term_prog = term_prog
        self.term_sess = term_sess
        self.ws_port   = ws_port
        self.agent_name = agent_name

        # State machine
        self._response_buf  = []     # accumulated clean text since last input
        self._last_output   = 0.0   # time of last byte from child
        self._last_input    = 0.0   # time of last byte from user
        self._last_fired    = 0.0   # time of last notification
        self._fired_this_turn = False
        self._child_pid     = None
        self._master_fd     = None

        # Quiescence timer
        self._quiet_timer   = None
        self._timer_lock    = threading.Lock()

    def run(self) -> int:
        """Run child in PTY, proxy I/O, return exit code."""
        # Save terminal state so we can restore on exit
        old_tty_settings = None
        stdin_fd = sys.stdin.fileno() if sys.stdin.isatty() else None

        master_fd, slave_fd = pty.openpty()
        self._master_fd = master_fd

        # Set slave PTY size to match real terminal
        if stdin_fd is not None:
            try:
                winsize = struct.pack('HHHH', 0, 0, 0, 0)
                winsize = fcntl.ioctl(stdin_fd, termios.TIOCGWINSZ, winsize)
                fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)
            except Exception:
                pass

        pid = os.fork()
        if pid == 0:
            # ── Child ──────────────────────────────────────────────────────
            os.close(master_fd)
            os.setsid()
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
            os.dup2(slave_fd, 0)
            os.dup2(slave_fd, 1)
            os.dup2(slave_fd, 2)
            if slave_fd > 2:
                os.close(slave_fd)
            os.execvp(self.cmd[0], self.cmd)
            os._exit(1)

        # ── Parent ─────────────────────────────────────────────────────────
        self._child_pid = pid
        os.close(slave_fd)

        # Forward SIGWINCH (terminal resize) to child
        def _sigwinch(sig, frame):
            if stdin_fd is not None:
                try:
                    winsize = fcntl.ioctl(stdin_fd, termios.TIOCGWINSZ, b'\x00' * 8)
                    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
                except Exception:
                    pass
        signal.signal(signal.SIGWINCH, _sigwinch)

        # Put stdin in raw mode
        if stdin_fd is not None:
            try:
                old_tty_settings = termios.tcgetattr(stdin_fd)
                tty.setraw(stdin_fd)
            except Exception:
                old_tty_settings = None

        exit_code = 0
        try:
            exit_code = self._proxy_loop(master_fd, stdin_fd)
        finally:
            if old_tty_settings is not None:
                try:
                    termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_tty_settings)
                except Exception:
                    pass
            self._cancel_quiet_timer()
            # Fire any remaining buffered response
            self._maybe_fire("COMPLETED", force=True)

        return exit_code

    def _proxy_loop(self, master_fd: int, stdin_fd) -> int:
        fds = [master_fd]
        if stdin_fd is not None:
            fds.append(stdin_fd)

        while True:
            try:
                ready, _, _ = select.select(fds, [], [], 0.5)
            except (OSError, ValueError):
                break

            for fd in ready:
                if fd == master_fd:
                    # Output from child
                    try:
                        data = os.read(master_fd, READ_CHUNK)
                    except OSError:
                        # Child closed its end — it's done
                        self._wait_child()
                        return self._child_exit_code

                    if not data:
                        self._wait_child()
                        return self._child_exit_code

                    # Write to real stdout
                    os.write(sys.stdout.fileno(), data)
                    self._on_child_output(data)

                elif fd == stdin_fd:
                    # Input from user
                    try:
                        data = os.read(stdin_fd, READ_CHUNK)
                    except OSError:
                        data = b''

                    if data:
                        try:
                            os.write(master_fd, data)
                        except OSError:
                            pass
                        self._on_user_input(data)
                    else:
                        # stdin closed (e.g. piped input finished)
                        pass

            # Check if child has exited
            try:
                wpid, wstatus = os.waitpid(self._child_pid, os.WNOHANG)
                if wpid == self._child_pid:
                    self._child_exit_code = os.waitstatus_to_exitcode(wstatus) if hasattr(os, 'waitstatus_to_exitcode') else (wstatus >> 8)
                    # Drain remaining output
                    try:
                        while True:
                            r, _, _ = select.select([master_fd], [], [], 0.1)
                            if not r:
                                break
                            chunk = os.read(master_fd, READ_CHUNK)
                            if not chunk:
                                break
                            os.write(sys.stdout.fileno(), chunk)
                            self._on_child_output(chunk)
                    except OSError:
                        pass
                    return self._child_exit_code
            except (ChildProcessError, OSError):
                return getattr(self, '_child_exit_code', 0)

        return getattr(self, '_child_exit_code', 0)

    def _wait_child(self):
        try:
            _, wstatus = os.waitpid(self._child_pid, 0)
            self._child_exit_code = os.waitstatus_to_exitcode(wstatus) if hasattr(os, 'waitstatus_to_exitcode') else (wstatus >> 8)
        except Exception:
            self._child_exit_code = 0

    def _on_child_output(self, raw: bytes):
        now = time.monotonic()
        self._last_output = now

        try:
            text = _strip_ansi(raw.decode("utf-8", errors="replace"))
        except Exception:
            text = ""

        if text:
            self._response_buf.append(text)

        # Reset quiescence timer
        self._reset_quiet_timer()

    def _on_user_input(self, raw: bytes):
        now = time.monotonic()
        self._last_input = now

        # New user input = start of a new turn; reset state
        self._cancel_quiet_timer()
        self._response_buf.clear()
        self._fired_this_turn = False

    def _reset_quiet_timer(self):
        """Cancel any existing timer and start a fresh QUIET_SECS countdown."""
        with self._timer_lock:
            if self._quiet_timer is not None:
                self._quiet_timer.cancel()
            self._quiet_timer = threading.Timer(QUIET_SECS, self._on_quiet)
            self._quiet_timer.daemon = True
            self._quiet_timer.start()

    def _cancel_quiet_timer(self):
        with self._timer_lock:
            if self._quiet_timer is not None:
                self._quiet_timer.cancel()
                self._quiet_timer = None

    def _on_quiet(self):
        """Called QUIET_SECS after the last byte from child."""
        self._maybe_fire("COMPLETED", force=False)

    def _maybe_fire(self, ev_type: str, force: bool):
        if self._fired_this_turn and not force:
            return

        snippet = "".join(self._response_buf)
        clean = snippet.strip()

        if len(clean) < MIN_RESPONSE_CHARS and not force:
            return

        now = time.monotonic()
        if now - self._last_fired < NOTIFY_COOLDOWN_S:
            return

        # Don't fire if there's been very recent output (still streaming)
        if not force and (now - self._last_output) < QUIET_SECS * 0.9:
            return

        self._fired_this_turn = True
        self._last_fired = now
        self._response_buf.clear()

        title = f"{self.agent_name} · responded"
        _fire_notification(
            notify_py  = self.notify_py,
            title      = title,
            site       = self.site,
            ev_type    = ev_type,
            snippet    = clean,
            sid        = self.sid,
            tty_dev    = self.tty_dev,
            term_prog  = self.term_prog,
            term_sess  = self.term_sess,
            ws_port    = self.ws_port,
        )


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="AgentWatch PTY wrapper for CLI agents",
        add_help=False,
    )
    parser.add_argument("--sid",       default="")
    parser.add_argument("--tty",       default="")
    parser.add_argument("--term",      default="")
    parser.add_argument("--sess",      default="")
    parser.add_argument("--notify",    default="")
    parser.add_argument("--site",      default="Terminal")
    parser.add_argument("--port",      default="59452")
    parser.add_argument("--agent",     default="agent")
    parser.add_argument("--", dest="rest", nargs=argparse.REMAINDER)

    # Split on '--' manually to get the wrapped command
    if "--" in sys.argv:
        split_idx = sys.argv.index("--")
        my_args   = sys.argv[1:split_idx]
        cmd       = sys.argv[split_idx + 1:]
    else:
        my_args = sys.argv[1:]
        cmd     = []

    args = parser.parse_args(my_args)

    if not cmd:
        print("[pty_wrapper] No command to wrap.", file=sys.stderr)
        sys.exit(1)

    _log(f"wrapping cmd={cmd} agent={args.agent!r} site={args.site!r} sid={args.sid!r}")

    wrapper = PTYWrapper(
        cmd        = cmd,
        notify_py  = args.notify,
        site       = args.site,
        sid        = args.sid,
        tty_dev    = args.tty,
        term_prog  = args.term,
        term_sess  = args.sess,
        ws_port    = args.port,
        agent_name = args.agent,
    )

    try:
        exit_code = wrapper.run()
    except Exception as e:
        _log(f"wrapper crashed: {e}")
        import traceback
        _log(traceback.format_exc())
        exit_code = 1

    sys.exit(exit_code)


if __name__ == "__main__":
    main()