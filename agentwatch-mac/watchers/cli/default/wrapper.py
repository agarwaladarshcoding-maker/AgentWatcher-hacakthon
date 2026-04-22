#!/usr/bin/env python3
"""
AgentWatch — pty_wrapper.py  v7.0

Changes from v6.0:
  • Reply injection: when the user types a reply in the Mac notification card,
    the text is written directly into the agent's PTY stdin — so Gemini, Claude,
    Aider etc. receive it just as if the user typed it.
  • STOP_MONITORING WebSocket message: the wrapper now subscribes to the Mac
    App WS and exits cleanly when asked.
  • Quiescence tuning: Gemini CLI buffers slowly; QUIET_SECS raised to 3.0 s.
  • Multi-turn support: after firing a notification the wrapper keeps running
    and re-arms for the next agent response.
  • Reply is written to PTY master as raw bytes + newline so agent sees Enter.
"""

import argparse
import asyncio
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

# ── Tuning ────────────────────────────────────────────────────────────────────
QUIET_SECS         = 3.0    # silence after which agent is "done"
MIN_RESPONSE_CHARS = 40     # minimum chars before firing notification
SNIPPET_MAX_CHARS  = 3000   # chars sent to notify.py
READ_CHUNK         = 4096
NOTIFY_COOLDOWN_S  = 3.0    # don't re-fire within N seconds
LOG_PATH           = os.path.expanduser("~/.agentwatch/notify.log")

# ── ANSI escape stripper ──────────────────────────────────────────────────────
_ANSI_RE = re.compile(
    r'(\x9B|\x1B\[)[0-?]*[ -/]*[@-~]'
    r'|\x1B[@-_]'
    r'|\x1B\[[0-9;]*[mGKHFABCDJsu]'
    r'|\r',
    re.VERBOSE,
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
    if not notify_py or not os.path.exists(notify_py):
        _log(f"notify.py not found at {notify_py!r}")
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
        _log(f"fired notify: title={title!r} ev={ev_type} snippet_len={len(snip)}")
    except Exception as e:
        _log(f"failed to fire notify: {e}")


# ── WebSocket reply listener ──────────────────────────────────────────────────
class _ReplyListener(threading.Thread):
    """
    Opens a WS connection to the Mac App and listens for:
      - REPLY_INJECT  → write reply text into agent's PTY stdin
      - STOP_MONITORING → set stop event
    """

    def __init__(self, ws_port: str, sid: str, master_fd_getter, stop_event):
        super().__init__(daemon=True)
        self._port = ws_port
        self._sid = sid
        self._get_master_fd = master_fd_getter   # callable → int | None
        self._stop_event = stop_event

    def run(self):
        try:
            import asyncio as _aio
            loop = _aio.new_event_loop()
            loop.run_until_complete(self._listen())
        except Exception as e:
            _log(f"ReplyListener crashed: {e}")

    async def _listen(self):
        try:
            import websockets
        except ImportError:
            _log("websockets not installed — reply injection disabled")
            return

        url = f"ws://localhost:{self._port}"
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(url, open_timeout=2) as ws:
                    # Announce ourselves so Mac App knows our session
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

                        mtype = msg.get("type", "")

                        if mtype == "REPLY_INJECT":
                            # Only handle if this message targets our session
                            # (Mac App may not set sessionId, accept all for CLI)
                            text = msg.get("text", "").strip()
                            if text:
                                self._inject_reply(text)

                        elif mtype == "STOP_MONITORING":
                            target = msg.get("sessionId", "")
                            if not target or target == self._sid:
                                _log("ReplyListener: STOP_MONITORING received")
                                self._stop_event.set()
                                return

            except Exception as e:
                _log(f"ReplyListener ws error: {e}")
                if not self._stop_event.is_set():
                    await asyncio.sleep(3)  # retry

    def _inject_reply(self, text: str):
        fd = self._get_master_fd()
        if fd is None:
            _log("inject_reply: master_fd not available")
            return
        try:
            # Write text + newline to PTY master = agent sees it as typed input
            payload = (text + "\n").encode("utf-8")
            os.write(fd, payload)
            _log(f"inject_reply: wrote {len(payload)} bytes to PTY")
        except OSError as e:
            _log(f"inject_reply: write error: {e}")


# ── PTY wrapper core ──────────────────────────────────────────────────────────
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

        self._response_buf    = []
        self._last_output     = 0.0
        self._last_input      = 0.0
        self._last_fired      = 0.0
        self._fired_this_turn = False
        self._child_pid       = None
        self._master_fd       = None
        self._child_exit_code = 0

        self._quiet_timer = None
        self._timer_lock  = threading.Lock()
        self._stop_event  = threading.Event()

    def _get_master_fd(self):
        return self._master_fd

    def run(self) -> int:
        old_tty_settings = None
        stdin_fd = sys.stdin.fileno() if sys.stdin.isatty() else None

        master_fd, slave_fd = pty.openpty()
        self._master_fd = master_fd

        if stdin_fd is not None:
            try:
                winsize = struct.pack('HHHH', 0, 0, 0, 0)
                winsize = fcntl.ioctl(stdin_fd, termios.TIOCGWINSZ, winsize)
                fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)
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
                    winsize = fcntl.ioctl(stdin_fd, termios.TIOCGWINSZ, b'\x00' * 8)
                    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
                except Exception:
                    pass
        signal.signal(signal.SIGWINCH, _sigwinch)

        if stdin_fd is not None:
            try:
                old_tty_settings = termios.tcgetattr(stdin_fd)
                tty.setraw(stdin_fd)
            except Exception:
                old_tty_settings = None

        # Start reply listener thread
        listener = _ReplyListener(
            ws_port=self.ws_port,
            sid=self.sid,
            master_fd_getter=self._get_master_fd,
            stop_event=self._stop_event,
        )
        listener.start()

        exit_code = 0
        try:
            exit_code = self._proxy_loop(master_fd, stdin_fd)
        finally:
            self._stop_event.set()
            if old_tty_settings is not None:
                try: termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_tty_settings)
                except Exception: pass
            self._cancel_quiet_timer()
            
            is_error = exit_code != 0
            ev_type = "ERROR" if is_error else "COMPLETED"
            self._maybe_fire(ev_type, force=True, is_error=is_error)

        return exit_code

    def _proxy_loop(self, master_fd: int, stdin_fd) -> int:
        fds = [master_fd]
        if stdin_fd is not None:
            fds.append(stdin_fd)

        while not self._stop_event.is_set():
            try:
                ready, _, _ = select.select(fds, [], [], 0.5)
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
                    self._on_child_output(data)

                elif fd == stdin_fd:
                    try:
                        data = os.read(stdin_fd, READ_CHUNK)
                    except OSError:
                        data = b''
                    if data:
                        try: os.write(master_fd, data)
                        except OSError: pass
                        self._on_user_input(data)

            try:
                wpid, wstatus = os.waitpid(self._child_pid, os.WNOHANG)
                if wpid == self._child_pid:
                    ec = os.waitstatus_to_exitcode(wstatus) if hasattr(os, 'waitstatus_to_exitcode') else (wstatus >> 8)
                    self._child_exit_code = ec
                    # drain
                    try:
                        while True:
                            r, _, _ = select.select([master_fd], [], [], 0.1)
                            if not r: break
                            chunk = os.read(master_fd, READ_CHUNK)
                            if not chunk: break
                            os.write(sys.stdout.fileno(), chunk)
                            self._on_child_output(chunk)
                    except OSError:
                        pass
                    return self._child_exit_code
            except (ChildProcessError, OSError):
                return getattr(self, '_child_exit_code', 0)

        # stop_event was set (STOP_MONITORING)
        try:
            os.kill(self._child_pid, signal.SIGTERM)
        except OSError:
            pass
        return 0

    def _wait_child(self):
        try:
            _, wstatus = os.waitpid(self._child_pid, 0)
            self._child_exit_code = os.waitstatus_to_exitcode(wstatus) if hasattr(os, 'waitstatus_to_exitcode') else (wstatus >> 8)
        except Exception:
            self._child_exit_code = 0

    def _on_child_output(self, raw: bytes):
        self._last_output = time.monotonic()
        try:
            text = _strip_ansi(raw.decode("utf-8", errors="replace"))
        except Exception:
            text = ""
        if text:
            self._response_buf.append(text)
            
            # Instantly detect permission requests (e.g. [y/N], Confirm?)
            if re.search(r'(\[y/n\]|\(y/n\)|\(yes/no\)|confirm\?|y/N|Y/n)\s*$', text, re.IGNORECASE):
                self._maybe_fire("ACTION_REQUIRED", force=True, title_override=f"{self.agent_name} · needs permission")
                
        self._reset_quiet_timer()

    def _on_user_input(self, raw: bytes):
        self._last_input = time.monotonic()
        self._cancel_quiet_timer()
        self._response_buf.clear()
        self._fired_this_turn = False

    def _reset_quiet_timer(self):
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
        self._maybe_fire("COMPLETED", force=False)

    def _maybe_fire(self, ev_type: str, force: bool, is_error: bool = False, title_override: str = None):
        snippet = "".join(self._response_buf)
        clean = snippet.strip()
        
        if not clean and not is_error:
            return
            
        if self._fired_this_turn and not force:
            return
            
        if len(clean) < MIN_RESPONSE_CHARS and not force and not is_error:
            return
            
        now = time.monotonic()
        if now - self._last_fired < NOTIFY_COOLDOWN_S and not force:
            return
            
        if not force and (now - self._last_output) < QUIET_SECS * 0.9:
            return

        self._fired_this_turn = True
        self._last_fired = now
        self._response_buf.clear()

        if title_override:
            title = title_override
        else:
            title = f"{self.agent_name} · errored" if is_error else f"{self.agent_name} · responded"

        _fire_notification(
            notify_py=self.notify_py, title=title, site=self.site,
            ev_type=ev_type, snippet=clean, sid=self.sid,
            tty_dev=self.tty_dev, term_prog=self.term_prog,
            term_sess=self.term_sess, ws_port=self.ws_port,
        )


# ── Entry point ───────────────────────────────────────────────────────────────
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
    parser.add_argument("--", dest="rest", nargs=argparse.REMAINDER)

    if "--" in sys.argv:
        split_idx = sys.argv.index("--")
        my_args = sys.argv[1:split_idx]
        cmd = sys.argv[split_idx + 1:]
    else:
        my_args = sys.argv[1:]
        cmd = []

    args = parser.parse_args(my_args)

    if not cmd:
        print("[pty_wrapper] No command to wrap.", file=sys.stderr)
        sys.exit(1)

    _log(f"v7.0 wrapping cmd={cmd} agent={args.agent!r} site={args.site!r} sid={args.sid!r}")

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