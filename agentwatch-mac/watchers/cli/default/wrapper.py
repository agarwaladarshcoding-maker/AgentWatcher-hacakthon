#!/usr/bin/env python3
"""
AgentWatch — default PTY wrapper  v8.0

Fixes vs v7.0:
  1. DEDUPLICATION: MD5 hash of last-fired snippet — never repeat same notification
  2. INPUT TRACKING: user keystrokes captured to detect turn boundaries accurately
  3. BUFFER MANAGEMENT: buffer cleared on user input so next response starts fresh
  4. PERMISSION DETECTION: immediate fire on (Y/n) / confirm? patterns, no timer wait
  5. CARRIAGE RETURN handling: \r resets current line (terminal overwrite pattern)
  6. QUIET TIMER: reset only on meaningful output, not on every byte
  7. COOLDOWN enforced per content hash, not just per timestamp
"""

import argparse
import asyncio
import datetime
import fcntl
import hashlib
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

# ── Tuning ─────────────────────────────────────────────────────────────────────
QUIET_SECS          = 3.0
MIN_RESPONSE_CHARS  = 40
SNIPPET_MAX_CHARS   = 3000
READ_CHUNK          = 4096
NOTIFY_COOLDOWN_S   = 4.0
LOG_PATH            = os.path.expanduser("~/.agentwatch/notify.log")

# ── ANSI stripper ───────────────────────────────────────────────────────────────
_ANSI_RE = re.compile(
    r'(\x9B|\x1B\[)[0-?]*[ -/]*[@-~]'
    r'|\x1B[@-_]'
    r'|\x1B\[[0-9;]*[mGKHFABCDJsuhl]'
    r'|\x1B\(B|\x1B=|\x1B>',
    re.VERBOSE,
)

def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub('', text)


# ── Permission patterns (immediate fire) ────────────────────────────────────────
_PERMISSION_RE = re.compile(
    r'(\[y/n\]|\(y/n\)|\(yes/no\)|confirm\?|y/N\b|Y/n\b'
    r'|do you want to|allow this|proceed\?|are you sure'
    r'|press enter to)',
    re.IGNORECASE,
)


def _log(msg: str):
    stamp = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[pty_wrap {stamp}] {msg}"
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── Output buffer with CR-aware line tracking ───────────────────────────────────
class OutputBuffer:
    """
    Tracks terminal output correctly, including \r (carriage return) overwrites.
    Collects content lines, discards pure decoration lines.
    """

    def __init__(self):
        self._lines: list[str] = []
        self._current_line: str = ""
        self._pending: str = ""

    def feed(self, raw: bytes) -> str | None:
        """
        Feed raw bytes. Returns event_type string if immediate notification
        should fire ("ACTION_REQUIRED", "ERROR"), else None.
        """
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            return None

        text = _strip_ansi(text)

        for ch in text:
            if ch == '\r':
                # Carriage return: overwrite current line (common in spinners/progress)
                self._current_line = ""
            elif ch == '\n':
                line = self._current_line.strip()
                if line:
                    self._lines.append(line)
                    trigger = self._check_immediate(line)
                    if trigger:
                        self._current_line = ""
                        return trigger
                self._current_line = ""
            else:
                self._current_line += ch

        # Check incomplete current line for permission patterns
        cur = self._current_line.strip()
        if cur and _PERMISSION_RE.search(cur):
            self._lines.append(cur)
            self._current_line = ""
            return "ACTION_REQUIRED"

        return None

    def _check_immediate(self, line: str) -> str | None:
        if _PERMISSION_RE.search(line):
            return "ACTION_REQUIRED"
        # Error patterns
        if re.search(r'^(error|api error|rate.?limit|connection refused|fatal)[::\s]',
                     line, re.IGNORECASE):
            return "ERROR"
        return None

    def get_content(self) -> str:
        """Return meaningful content lines, stripping pure decoration."""
        result = []
        for line in self._lines:
            stripped = line.strip()
            if not stripped:
                continue
            # Skip pure-decoration lines (only symbols/spaces)
            if re.match(r'^[\s\-=_*#~▸▹◆●◉○✓✗⚠⎿⏎✱⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏─━│╭╰╯╮┌┐└┘\|]+$', stripped):
                continue
            result.append(stripped)
        return "\n".join(result)

    def clear(self):
        self._lines.clear()
        self._current_line = ""
        self._pending = ""


# ── Notification firing ─────────────────────────────────────────────────────────
def _fire_notification(notify_py: str, title: str, site: str, ev_type: str,
                        snippet: str, sid: str, tty_dev: str,
                        term_prog: str, term_sess: str, ws_port: str = "59452"):
    if not notify_py or not os.path.exists(notify_py):
        _log(f"notify.py not found: {notify_py!r}")
        return
    snip = snippet.strip()[-SNIPPET_MAX_CHARS:] if snippet else ""
    cmd = [
        sys.executable, notify_py,
        title, site, ev_type, snip,
        ws_port, "", "",
        tty_dev, term_prog, term_sess, sid,
    ]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        _log(f"fired: ev={ev_type} title={title!r} snip_len={len(snip)}")
    except Exception as e:
        _log(f"fire failed: {e}")


# ── WebSocket reply listener ─────────────────────────────────────────────────────
class _ReplyListener(threading.Thread):
    def __init__(self, ws_port: str, sid: str, master_fd_getter, stop_event):
        super().__init__(daemon=True)
        self._port = ws_port
        self._sid = sid
        self._get_master_fd = master_fd_getter
        self._stop_event = stop_event

    def run(self):
        try:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(self._listen())
        except Exception as e:
            _log(f"ReplyListener crashed: {e}")

    async def _listen(self):
        try:
            import websockets
        except ImportError:
            _log("websockets missing — reply injection disabled")
            return

        url = f"ws://localhost:{self._port}"
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(url, open_timeout=2) as ws:
                    await ws.send(json.dumps({
                        "type": "CLI_SESSION_REGISTER",
                        "sessionId": self._sid,
                    }))
                    _log(f"ReplyListener connected (sid={self._sid})")
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        if msg.get("type") == "REPLY_INJECT":
                            text = msg.get("text", "").strip()
                            if text:
                                self._inject_reply(text)
                        elif msg.get("type") == "STOP_MONITORING":
                            target = msg.get("sessionId", "")
                            if not target or target == self._sid:
                                _log("STOP_MONITORING received")
                                self._stop_event.set()
                                return
            except Exception as e:
                _log(f"ReplyListener ws error: {e}")
                if not self._stop_event.is_set():
                    await asyncio.sleep(3)

    def _inject_reply(self, text: str):
        fd = self._get_master_fd()
        if fd is None:
            _log("inject_reply: no master_fd")
            return
        try:
            payload = (text + "\n").encode("utf-8")
            os.write(fd, payload)
            _log(f"injected {len(payload)} bytes")
        except OSError as e:
            _log(f"inject error: {e}")


# ── PTY Wrapper ──────────────────────────────────────────────────────────────────
class PTYWrapper:
    def __init__(self, cmd: list, notify_py: str, site: str, sid: str,
                 tty_dev: str, term_prog: str, term_sess: str, ws_port: str,
                 agent_name: str):
        self.cmd        = cmd
        self.notify_py  = notify_py
        self.site       = site
        self.sid        = sid
        self.tty_dev    = tty_dev
        self.term_prog  = term_prog
        self.term_sess  = term_sess
        self.ws_port    = ws_port
        self.agent_name = agent_name

        self._buf             = OutputBuffer()
        self._last_output     = 0.0
        self._last_fired      = 0.0
        self._last_hash       = ""       # dedup
        self._fired_this_turn = False
        self._child_pid       = None
        self._master_fd       = None
        self._child_exit_code = 0
        self._input_buf       = []       # user keystrokes

        self._quiet_timer = None
        self._timer_lock  = threading.Lock()
        self._stop_event  = threading.Event()

    def _get_master_fd(self):
        return self._master_fd

    def run(self) -> int:
        old_tty = None
        stdin_fd = sys.stdin.fileno() if sys.stdin.isatty() else None

        master_fd, slave_fd = pty.openpty()
        self._master_fd = master_fd

        if stdin_fd is not None:
            try:
                ws = struct.pack('HHHH', 0, 0, 0, 0)
                ws = fcntl.ioctl(stdin_fd, termios.TIOCGWINSZ, ws)
                fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, ws)
            except Exception:
                pass

        pid = os.fork()
        if pid == 0:
            os.close(master_fd)
            os.setsid()
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
            os.dup2(slave_fd, 0); os.dup2(slave_fd, 1); os.dup2(slave_fd, 2)
            if slave_fd > 2: os.close(slave_fd)
            os.execvp(self.cmd[0], self.cmd)
            os._exit(1)

        self._child_pid = pid
        os.close(slave_fd)

        def _sigwinch(sig, frame):
            if stdin_fd is not None:
                try:
                    ws = fcntl.ioctl(stdin_fd, termios.TIOCGWINSZ, b'\x00' * 8)
                    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, ws)
                except Exception:
                    pass
        signal.signal(signal.SIGWINCH, _sigwinch)

        if stdin_fd is not None:
            try:
                old_tty = termios.tcgetattr(stdin_fd)
                tty.setraw(stdin_fd)
            except Exception:
                old_tty = None

        listener = _ReplyListener(
            ws_port=self.ws_port, sid=self.sid,
            master_fd_getter=self._get_master_fd,
            stop_event=self._stop_event,
        )
        listener.start()

        exit_code = 0
        try:
            exit_code = self._proxy_loop(master_fd, stdin_fd)
        finally:
            self._stop_event.set()
            if old_tty is not None:
                try: termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_tty)
                except Exception: pass
            self._cancel_quiet_timer()
            is_error = exit_code != 0
            self._maybe_fire("ERROR" if is_error else "COMPLETED", force=True, is_error=is_error)

        return exit_code

    def _proxy_loop(self, master_fd: int, stdin_fd) -> int:
        fds = [master_fd]
        if stdin_fd is not None:
            fds.append(stdin_fd)

        while not self._stop_event.is_set():
            try:
                ready, _, _ = select.select(fds, [], [], 0.3)
            except (OSError, ValueError):
                break

            for fd in ready:
                if fd == master_fd:
                    try:
                        data = os.read(master_fd, READ_CHUNK)
                    except OSError:
                        self._wait_child(); return self._child_exit_code
                    if not data:
                        self._wait_child(); return self._child_exit_code
                    os.write(sys.stdout.fileno(), data)
                    self._on_output(data)

                elif fd == stdin_fd:
                    try:
                        data = os.read(stdin_fd, READ_CHUNK)
                    except OSError:
                        data = b''
                    if data:
                        try: os.write(master_fd, data)
                        except OSError: pass
                        self._on_input(data)

            try:
                wpid, wstatus = os.waitpid(self._child_pid, os.WNOHANG)
                if wpid == self._child_pid:
                    ec = os.waitstatus_to_exitcode(wstatus) if hasattr(os, 'waitstatus_to_exitcode') else (wstatus >> 8)
                    self._child_exit_code = ec
                    try:
                        while True:
                            r, _, _ = select.select([master_fd], [], [], 0.1)
                            if not r: break
                            chunk = os.read(master_fd, READ_CHUNK)
                            if not chunk: break
                            os.write(sys.stdout.fileno(), chunk)
                            self._on_output(chunk)
                    except OSError:
                        pass
                    return self._child_exit_code
            except (ChildProcessError, OSError):
                return getattr(self, '_child_exit_code', 0)

        try: os.kill(self._child_pid, signal.SIGTERM)
        except OSError: pass
        return 0

    def _wait_child(self):
        try:
            _, ws = os.waitpid(self._child_pid, 0)
            self._child_exit_code = os.waitstatus_to_exitcode(ws) if hasattr(os, 'waitstatus_to_exitcode') else (ws >> 8)
        except Exception:
            self._child_exit_code = 0

    def _on_output(self, raw: bytes):
        self._last_output = time.monotonic()
        ev_type = self._buf.feed(raw)

        if ev_type == "ACTION_REQUIRED":
            self._cancel_quiet_timer()
            content = self._buf.get_content()
            self._fire_now("ACTION_REQUIRED",
                           title=f"{self.agent_name} · needs your input",
                           snippet=content)
            return

        if ev_type == "ERROR":
            self._cancel_quiet_timer()
            content = self._buf.get_content()
            self._fire_now("ERROR",
                           title=f"{self.agent_name} · error",
                           snippet=content)
            return

        self._reset_quiet_timer()

    def _on_input(self, raw: bytes):
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            text = ""

        if "\r" in text or "\n" in text:
            self._input_buf.clear()
        else:
            if "\x7f" in text or "\x08" in text:
                if self._input_buf:
                    self._input_buf.pop()
            else:
                self._input_buf.append(text)

        # New user input = new turn
        self._cancel_quiet_timer()
        self._buf.clear()
        self._fired_this_turn = False

    def _reset_quiet_timer(self):
        with self._timer_lock:
            if self._quiet_timer:
                self._quiet_timer.cancel()
            self._quiet_timer = threading.Timer(QUIET_SECS, self._on_quiet)
            self._quiet_timer.daemon = True
            self._quiet_timer.start()

    def _cancel_quiet_timer(self):
        with self._timer_lock:
            if self._quiet_timer:
                self._quiet_timer.cancel()
                self._quiet_timer = None

    def _on_quiet(self):
        self._maybe_fire("COMPLETED", force=False)

    def _fire_now(self, ev_type: str, title: str, snippet: str):
        """Fire immediately, bypassing most checks. Used for permissions/errors."""
        now = time.monotonic()
        content_hash = hashlib.md5(snippet[:500].encode()).hexdigest() if snippet else ""

        # Still dedup: don't re-fire if we just fired the exact same content
        if content_hash and content_hash == self._last_hash:
            if now - self._last_fired < 10.0:
                _log(f"dedup _fire_now: same hash {content_hash[:8]}")
                return

        self._last_fired = now
        self._last_hash = content_hash
        self._fired_this_turn = True
        self._buf.clear()

        _fire_notification(
            notify_py=self.notify_py, title=title, site=self.site,
            ev_type=ev_type, snippet=snippet or title,
            sid=self.sid, tty_dev=self.tty_dev,
            term_prog=self.term_prog, term_sess=self.term_sess,
            ws_port=self.ws_port,
        )

    def _maybe_fire(self, ev_type: str, force: bool, is_error: bool = False):
        content = self._buf.get_content()
        clean = content.strip()

        if not clean and not is_error:
            return

        if not force and self._fired_this_turn:
            _log("already fired this turn, skipping quiet-timer fire")
            return

        if not force and len(clean) < MIN_RESPONSE_CHARS:
            _log(f"too short ({len(clean)} chars), skipping")
            return

        now = time.monotonic()

        # Dedup by content hash
        content_hash = hashlib.md5(clean[:500].encode()).hexdigest() if clean else ""
        if content_hash and content_hash == self._last_hash and not force:
            _log(f"dedup: same content hash {content_hash[:8]}")
            return

        # Cooldown
        if not force and now - self._last_fired < NOTIFY_COOLDOWN_S:
            _log(f"cooldown: {now - self._last_fired:.1f}s since last")
            return

        # Must be actually quiet
        if not force and (now - self._last_output) < QUIET_SECS * 0.8:
            _log("output too recent, not quiet yet")
            return

        self._last_fired = now
        self._last_hash = content_hash
        self._fired_this_turn = True
        self._buf.clear()

        title = f"{self.agent_name} · errored" if is_error else f"{self.agent_name} · responded"

        _fire_notification(
            notify_py=self.notify_py, title=title, site=self.site,
            ev_type=ev_type, snippet=clean or title,
            sid=self.sid, tty_dev=self.tty_dev,
            term_prog=self.term_prog, term_sess=self.term_sess,
            ws_port=self.ws_port,
        )


# ── Entry ────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="AgentWatch PTY wrapper", add_help=False)
    parser.add_argument("--sid",    default="")
    parser.add_argument("--tty",    default="")
    parser.add_argument("--term",   default="")
    parser.add_argument("--sess",   default="")
    parser.add_argument("--notify", default="")
    parser.add_argument("--site",   default="Terminal")
    parser.add_argument("--port",   default="59452")
    parser.add_argument("--agent",  default="agent")

    if "--" in sys.argv:
        split_idx = sys.argv.index("--")
        my_args = sys.argv[1:split_idx]
        cmd = sys.argv[split_idx + 1:]
    else:
        my_args = sys.argv[1:]
        cmd = []

    args = parser.parse_args(my_args)

    if not cmd:
        print("[pty_wrapper] No command specified.", file=sys.stderr)
        sys.exit(1)

    _log(f"v8.0 wrapping cmd={cmd} agent={args.agent!r} site={args.site!r} sid={args.sid!r}")

    wrapper = PTYWrapper(
        cmd=cmd, notify_py=args.notify, site=args.site, sid=args.sid,
        tty_dev=args.tty, term_prog=args.term, term_sess=args.sess,
        ws_port=args.port, agent_name=args.agent,
    )
    try:
        exit_code = wrapper.run()
    except Exception as e:
        _log(f"wrapper crashed: {e}")
        import traceback; _log(traceback.format_exc())
        exit_code = 1

    sys.exit(exit_code)


if __name__ == "__main__":
    main()