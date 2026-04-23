#!/usr/bin/env python3
"""
AgentWatch — Claude CLI wrapper  v1.0

Specifically tuned for `claude` (Claude Code CLI) output patterns.

Claude CLI characteristics:
  • Uses rich TUI with spinners, box-drawing chars, color escape codes
  • Tool use lines: "● Tool: bash" / "◆ Tool:" / "✓ Tool:" / "⎿ Tool result:"
  • Permission prompts: "Do you want to proceed?" / "Allow this action?" / "(Y/n)"
  • Response turns end with:  "> " prompt return, or a cost/token summary line
  • Thinking indicator:  spinner + "Thinking…" / "Processing…"
  • Error lines: "Error:" / "API Error:" / "Rate limited"
  • Multi-turn: each user prompt resets the buffer

Key fixes vs default wrapper:
  1. Deduplication: track last-fired content hash → never repeat same notification
  2. Accurate turn boundary: detect Claude's ">" prompt re-appearance
  3. Permission detection: fire ACTION_REQUIRED immediately, not after quiet timer
  4. Tool-use tracking: suppress intermediate tool notifications (only final answer)
  5. Cost/token line as definitive "turn done" signal
  6. Reply injection via PTY stdin (works with Claude Code's readline)
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
QUIET_SECS          = 4.0    # Claude streams slowly; wait longer before "done"
MIN_RESPONSE_CHARS  = 30     # min chars to consider a real response
SNIPPET_MAX_CHARS   = 3000
READ_CHUNK          = 4096
NOTIFY_COOLDOWN_S   = 5.0    # prevent rapid re-fires
LOG_PATH            = os.path.expanduser("~/.agentwatch/notify.log")

# ── ANSI / TUI escape stripper ─────────────────────────────────────────────────
_ANSI_RE = re.compile(
    r'(\x9B|\x1B\[)[0-?]*[ -/]*[@-~]'   # CSI sequences
    r'|\x1B[@-_]'                         # two-char ESC sequences
    r'|\x1B\[[0-9;]*[mGKHFABCDJsuhl]'    # SGR / cursor
    r'|\x1B\(B'                           # charset
    r'|\x1B=|\x1B>'                       # keypad mode
    r'|\r',                               # bare CR
    re.VERBOSE,
)

# Box-drawing / spinner chars Claude uses in its TUI
_JUNK_RE = re.compile(
    r'^[─━│╭╰╯╮┌┐└┘├┤┬┴┼▸▹◆●◉○✓✗⚠️⎿⏎✱⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏\s]*$'
)

def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub('', text)


def _is_junk_line(line: str) -> bool:
    """Lines that are purely TUI decoration, not response content."""
    stripped = line.strip()
    if not stripped:
        return True
    if _JUNK_RE.match(stripped):
        return True
    # Progress/spinner lines
    if re.match(r'^[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]', stripped):
        return True
    return False


# ── Claude-specific pattern matchers ──────────────────────────────────────────

# Permission / confirmation prompts — fire ACTION_REQUIRED immediately
_PERMISSION_PATTERNS = [
    re.compile(r'\(Y/n\)', re.IGNORECASE),
    re.compile(r'\(y/N\)', re.IGNORECASE),
    re.compile(r'\(yes/no\)', re.IGNORECASE),
    re.compile(r'Do you want to (proceed|continue|allow|run|execute)', re.IGNORECASE),
    re.compile(r'Allow (this|the) (action|command|tool|operation)', re.IGNORECASE),
    re.compile(r'Press Enter to confirm', re.IGNORECASE),
    re.compile(r'Proceed\?', re.IGNORECASE),
    re.compile(r'Are you sure', re.IGNORECASE),
    re.compile(r'Approve (running|executing|this)', re.IGNORECASE),
    # Claude Code specific
    re.compile(r'Run (bash|shell|python|node|command)\?', re.IGNORECASE),
    re.compile(r'Write to (file|path)', re.IGNORECASE),
    re.compile(r'Edit (file|path)', re.IGNORECASE),
    re.compile(r'\[1\] Yes.*\[2\] No'),   # numbered choice menus
]

# Definitive "turn is done" signals
_TURN_DONE_PATTERNS = [
    # Cost/token summary that Claude always prints at end of turn
    re.compile(r'Cost:\s*\$[\d.]+'),
    re.compile(r'\d+\s+tokens?.*\$[\d.]+'),
    re.compile(r'Input tokens?:\s*\d+'),
    # Prompt return
    re.compile(r'^>\s*$'),
    re.compile(r'^\$\s*$'),
    # Session end
    re.compile(r'Claude Code session complete', re.IGNORECASE),
]

# Error patterns
_ERROR_PATTERNS = [
    re.compile(r'^(API\s+)?Error:', re.IGNORECASE | re.MULTILINE),
    re.compile(r'rate.?limit', re.IGNORECASE),
    re.compile(r'Request failed', re.IGNORECASE),
    re.compile(r'Authentication failed', re.IGNORECASE),
    re.compile(r'Invalid API key', re.IGNORECASE),
    re.compile(r'Connection (refused|timed? out|error)', re.IGNORECASE),
    re.compile(r'ENOENT|EACCES|EPERM', re.IGNORECASE),
]

# Tool-use lines (intermediate, suppress notification until done)
_TOOL_LINE_RE = re.compile(
    r'^[●◆✓⎿▸]\s*(Tool|bash|python|node|read_file|write_file|edit_file|search|grep|ls|cd)',
    re.IGNORECASE,
)

# Lines that are clearly Claude's response text (not TUI chrome)
_CONTENT_LINE_RE = re.compile(r'^[A-Za-z0-9`*#\-\+\[\(\'"]')


def _log(msg: str):
    stamp = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[claude_wrap {stamp}] {msg}"
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── Notification ───────────────────────────────────────────────────────────────
def _fire_notification(notify_py: str, title: str, site: str, ev_type: str,
                        snippet: str, sid: str, tty_dev: str,
                        term_prog: str, term_sess: str, ws_port: str = "59452",
                        user_prompt: str = ""):
    if not notify_py or not os.path.exists(notify_py):
        _log(f"notify.py not found: {notify_py!r}")
        return
    snip = snippet.strip()[-SNIPPET_MAX_CHARS:] if snippet else ""
    cmd = [
        sys.executable, notify_py,
        title, site, ev_type, snip,
        ws_port, "", "",
        tty_dev, term_prog, term_sess, sid,
        user_prompt[:200] if user_prompt else "",
    ]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        _log(f"fired: ev={ev_type} title={title!r} snip_len={len(snip)}")
    except Exception as e:
        _log(f"fire failed: {e}")


# ── WebSocket reply listener ───────────────────────────────────────────────────
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
            _log(f"injected {len(payload)} bytes to PTY")
        except OSError as e:
            _log(f"inject error: {e}")


# ── Claude Output Buffer ───────────────────────────────────────────────────────
class ClaudeOutputBuffer:
    """
    Accumulates Claude CLI output, separating:
      - TUI chrome (spinners, tool lines, box chars)  → discarded
      - Response content                               → kept for snippet
      - Permission prompts                             → triggers immediate notify
      - Turn-done signals                              → triggers final notify
    """

    def __init__(self):
        self._raw_lines: list[str] = []
        self._content_lines: list[str] = []
        self._pending_raw = ""
        self.last_user_prompt: str = ""
        self._tool_active = False
        self._tool_count = 0

    def feed(self, raw_bytes: bytes) -> tuple[str | None, str | None]:
        """
        Feed raw PTY bytes. Returns (event_type, reason) if an immediate
        notification should fire, otherwise (None, None).
        """
        try:
            text = raw_bytes.decode("utf-8", errors="replace")
        except Exception:
            return None, None

        text = _strip_ansi(text)
        self._pending_raw += text

        # Process complete lines
        lines = self._pending_raw.split("\n")
        self._pending_raw = lines[-1]   # keep incomplete last line
        complete_lines = lines[:-1]

        for line in complete_lines:
            result = self._process_line(line)
            if result != (None, None):
                return result

        # Check pending (incomplete) line for immediate triggers
        pending_clean = self._pending_raw.strip()
        if pending_clean:
            for pat in _PERMISSION_PATTERNS:
                if pat.search(pending_clean):
                    _log(f"permission detected in pending: {pending_clean[:80]!r}")
                    return "ACTION_REQUIRED", f"permission: {pending_clean[:80]}"

        return None, None

    def _process_line(self, raw_line: str) -> tuple[str | None, str | None]:
        line = raw_line.strip()
        if not line:
            return None, None

        # ── Permission / confirmation ──────────────────────────────────────
        for pat in _PERMISSION_PATTERNS:
            if pat.search(line):
                _log(f"permission line: {line[:100]!r}")
                self._content_lines.append(line)
                return "ACTION_REQUIRED", f"permission: {line[:100]}"

        # ── Error ──────────────────────────────────────────────────────────
        for pat in _ERROR_PATTERNS:
            if pat.search(line):
                _log(f"error line: {line[:100]!r}")
                self._content_lines.append(line)
                return "ERROR", f"error: {line[:100]}"

        # ── Turn-done signal ───────────────────────────────────────────────
        for pat in _TURN_DONE_PATTERNS:
            if pat.search(line):
                _log(f"turn-done signal: {line[:100]!r}")
                # Don't add cost lines to content
                if not re.search(r'Cost:|tokens?:', line, re.IGNORECASE):
                    self._content_lines.append(line)
                return "TURN_DONE", line[:100]

        # ── Tool use tracking ──────────────────────────────────────────────
        if _TOOL_LINE_RE.match(line):
            self._tool_active = True
            self._tool_count += 1
            self._raw_lines.append(line)
            return None, None

        # Tool result line
        if line.startswith("⎿") or line.startswith("Tool result:"):
            self._raw_lines.append(line)
            return None, None

        # ── TUI junk ───────────────────────────────────────────────────────
        if _is_junk_line(line):
            return None, None

        # ── Actual content ─────────────────────────────────────────────────
        self._tool_active = False
        self._content_lines.append(line)
        self._raw_lines.append(line)
        return None, None

    def get_content(self) -> str:
        """Return cleaned response content for the notification snippet."""
        lines = [l for l in self._content_lines if l.strip()]
        return "\n".join(lines)

    def get_tool_summary(self) -> str:
        if self._tool_count > 0:
            return f"[Used {self._tool_count} tool(s)] "
        return ""

    def clear(self):
        self._raw_lines.clear()
        self._content_lines.clear()
        self._pending_raw = ""
        self._tool_active = False
        self._tool_count = 0

    def set_user_prompt(self, text: str):
        self.last_user_prompt = text.strip()[:200]


# ── PTY Wrapper ────────────────────────────────────────────────────────────────
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

        self._buf             = ClaudeOutputBuffer()
        self._last_output     = 0.0
        self._last_fired      = 0.0
        self._last_hash       = ""          # dedup: don't re-fire same content
        self._fired_this_turn = False
        self._waiting_permission = False
        self._child_pid       = None
        self._master_fd       = None
        self._child_exit_code = 0
        self._input_buf       = []          # collect user keystrokes for prompt

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
            # Final notification on exit
            is_error = exit_code != 0
            self._fire("ERROR" if is_error else "COMPLETED", force=True)

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

            # Check child exit
            try:
                wpid, wstatus = os.waitpid(self._child_pid, os.WNOHANG)
                if wpid == self._child_pid:
                    ec = os.waitstatus_to_exitcode(wstatus) if hasattr(os, 'waitstatus_to_exitcode') else (wstatus >> 8)
                    self._child_exit_code = ec
                    # Drain remaining output
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

        # STOP_MONITORING
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

        # Feed buffer — may return immediate event type
        ev_type, reason = self._buf.feed(raw)

        if ev_type == "ACTION_REQUIRED":
            # Permission/confirmation: fire RIGHT NOW, don't wait for quiet
            self._cancel_quiet_timer()
            self._fire("ACTION_REQUIRED", force=True,
                       title=f"{self.agent_name} · needs your approval")
            self._waiting_permission = True
            return

        if ev_type == "ERROR":
            self._cancel_quiet_timer()
            self._fire("ERROR", force=True,
                       title=f"{self.agent_name} · error")
            return

        if ev_type == "TURN_DONE":
            # Definitive done signal — fire immediately
            self._cancel_quiet_timer()
            self._fire("COMPLETED", force=True,
                       title=f"{self.agent_name} · responded")
            return

        # Reset quiet timer on any output
        self._reset_quiet_timer()

    def _on_input(self, raw: bytes):
        # Collect user input for prompt extraction
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            text = ""

        # Enter key = end of user prompt
        if "\r" in text or "\n" in text:
            prompt = "".join(self._input_buf).strip()
            if prompt:
                self._buf.set_user_prompt(prompt)
                _log(f"user prompt captured: {prompt[:60]!r}")
            self._input_buf.clear()
        else:
            # Backspace
            if "\x7f" in text or "\x08" in text:
                if self._input_buf:
                    self._input_buf.pop()
            else:
                self._input_buf.append(text)

        # Reset on user input (new turn)
        self._cancel_quiet_timer()
        self._buf.clear()
        self._fired_this_turn = False
        self._waiting_permission = False
        _log("user input → buffer cleared, turn reset")

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
        """Called after QUIET_SECS of silence — Claude has finished streaming."""
        self._fire("COMPLETED", force=False,
                   title=f"{self.agent_name} · responded")

    def _fire(self, ev_type: str, force: bool = False,
              title: str = None, is_error: bool = False):
        content = self._buf.get_content()
        clean = content.strip()

        # Need minimum content (unless error/force)
        if not clean and not is_error and ev_type != "ACTION_REQUIRED":
            if not force:
                return

        # Deduplication: don't re-fire the exact same content
        content_hash = hashlib.md5(clean[:500].encode()).hexdigest() if clean else ""
        if content_hash and content_hash == self._last_hash and not force:
            _log(f"dedup: same content hash {content_hash[:8]}, skipping")
            return

        # Cooldown (unless force)
        now = time.monotonic()
        if not force and now - self._last_fired < NOTIFY_COOLDOWN_S:
            _log(f"cooldown: {now - self._last_fired:.1f}s since last fire")
            return

        if not force and self._fired_this_turn and ev_type == "COMPLETED":
            _log("already fired this turn, skipping")
            return

        # Check output is actually quiet (for non-force COMPLETED)
        if not force and ev_type == "COMPLETED":
            if (time.monotonic() - self._last_output) < QUIET_SECS * 0.8:
                return

        self._fired_this_turn = True
        self._last_fired = now
        self._last_hash = content_hash
        self._buf.clear()   # reset buffer for next turn

        tool_pfx = self._buf.get_tool_summary()
        snippet = (tool_pfx + clean) if clean else ""
        user_prompt = self._buf.last_user_prompt

        if title is None:
            if ev_type == "ERROR":
                title = f"{self.agent_name} · error"
            elif ev_type == "ACTION_REQUIRED":
                title = f"{self.agent_name} · needs your approval"
            else:
                title = f"{self.agent_name} · responded"

        _log(f"firing: ev={ev_type} title={title!r} content_len={len(clean)} hash={content_hash[:8]}")

        _fire_notification(
            notify_py=self.notify_py, title=title, site=self.site,
            ev_type=ev_type, snippet=snippet or title,
            sid=self.sid, tty_dev=self.tty_dev,
            term_prog=self.term_prog, term_sess=self.term_sess,
            ws_port=self.ws_port, user_prompt=user_prompt,
        )


# ── Entry ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="AgentWatch Claude CLI wrapper", add_help=False)
    parser.add_argument("--sid",    default="")
    parser.add_argument("--tty",    default="")
    parser.add_argument("--term",   default="")
    parser.add_argument("--sess",   default="")
    parser.add_argument("--notify", default="")
    parser.add_argument("--site",   default="Terminal")
    parser.add_argument("--port",   default="59452")
    parser.add_argument("--agent",  default="claude")

    if "--" in sys.argv:
        split_idx = sys.argv.index("--")
        my_args = sys.argv[1:split_idx]
        cmd = sys.argv[split_idx + 1:]
    else:
        my_args = sys.argv[1:]
        cmd = []

    args = parser.parse_args(my_args)

    if not cmd:
        print("[claude_wrapper] No command specified.", file=sys.stderr)
        sys.exit(1)

    _log(f"v1.0 Claude CLI wrapper: cmd={cmd} site={args.site!r} sid={args.sid!r}")

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