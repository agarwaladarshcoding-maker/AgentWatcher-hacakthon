#!/usr/bin/env python3
"""
AgentWatch macOS Menu Bar App  v3.0

All notifications go through notify.py (AppKit NSPanel).
No inline tkinter. No osascript display notification banners.
"""

import sys
import rumps
import asyncio
import threading
import websockets
import websockets.exceptions
import json
import subprocess
import sqlite3
import os
import logging
from datetime import datetime

# Silence benign websocket handshake-drop tracebacks. Chrome's MV3 service
# worker goes to sleep every ~30 s when idle, which drops the WS without a
# close frame; websockets ≥ 12 logs a full traceback for this. Harmless, but
# it floods ~/.agentwatch/agentwatch.err and looks alarming in the terminal.
logging.getLogger("websockets.server").setLevel(logging.CRITICAL)
logging.getLogger("websockets").setLevel(logging.ERROR)

from llm_router import LLMConfig, LLMResult, classify as llm_classify

PORT     = 59452
DB_DIR   = os.path.expanduser("~/.agentwatch")
DB_PATH  = os.path.join(DB_DIR, "history.db")
CFG_PATH = os.path.join(DB_DIR, "config.json")

# Locate notify.py: same dir as this script, or ~/.agentwatch/
_HERE = os.path.dirname(os.path.abspath(__file__))
NOTIFY_PY = os.path.join(_HERE, "notify.py")
if not os.path.exists(NOTIFY_PY):
    NOTIFY_PY = os.path.join(DB_DIR, "notify.py")

os.makedirs(DB_DIR, exist_ok=True)


# ─── DB ───────────────────────────────────────────────────────────────────────
def init_db():
    with sqlite3.connect(DB_PATH) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT, site TEXT, site_name TEXT, event_type TEXT,
                response_length INTEGER DEFAULT 0, duration_ms INTEGER DEFAULT 0,
                tab_id INTEGER, window_id INTEGER, user_reply TEXT
            )
        """)
        existing = {row[1] for row in c.execute("PRAGMA table_info(events)")}
        for col, sql in {
            "url":             "ALTER TABLE events ADD COLUMN url TEXT",
            "response_length": "ALTER TABLE events ADD COLUMN response_length INTEGER DEFAULT 0",
            "duration_ms":     "ALTER TABLE events ADD COLUMN duration_ms INTEGER DEFAULT 0",
            "tab_id":          "ALTER TABLE events ADD COLUMN tab_id INTEGER",
            "window_id":       "ALTER TABLE events ADD COLUMN window_id INTEGER",
            "user_reply":      "ALTER TABLE events ADD COLUMN user_reply TEXT",
            "category":        "ALTER TABLE events ADD COLUMN category TEXT",
            "category_reason": "ALTER TABLE events ADD COLUMN category_reason TEXT",
            "category_source": "ALTER TABLE events ADD COLUMN category_source TEXT",
            "message_snippet": "ALTER TABLE events ADD COLUMN message_snippet TEXT",
            "user_prompt":     "ALTER TABLE events ADD COLUMN user_prompt TEXT",   # NEW
        }.items():
            if col not in existing:
                c.execute(sql)
 

        c.commit()

init_db()


def load_config() -> dict:
    try:
        with open(CFG_PATH) as f: return json.load(f) or {}
    except Exception: return {}

def save_config(cfg: dict):
    try:
        tmp = CFG_PATH + ".tmp"
        with open(tmp, "w") as f: json.dump(cfg, f, indent=2)
        os.replace(tmp, CFG_PATH)
    except Exception as e:
        print(f"[AgentWatch] config save error: {e}")

def save_event(event: dict, user_reply: str = None):
    try:
        with sqlite3.connect(DB_PATH) as c:
            c.execute(
                "INSERT INTO events (timestamp,site,site_name,event_type,url,"
                "response_length,duration_ms,tab_id,window_id,user_reply,"
                "category,category_reason,category_source,message_snippet,user_prompt) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event.get("timestamp", datetime.now().isoformat()),
                    event.get("site",""), event.get("siteName",""),
                    event.get("eventType",""), event.get("url",""),
                    event.get("responseLength",0), event.get("durationMs",0),
                    event.get("tabId"), event.get("windowId"), user_reply,
                    event.get("category"), event.get("categoryReason"),
                    event.get("categorySource"),
                    (event.get("messageText") or event.get("messageSnippet") or "")[:2000] or None,
                    (event.get("userPrompt") or "")[:200] or None,   # NEW
                ),
            )
            c.commit()
    except Exception as e:
        print(f"[AgentWatch] DB error: {e}")


# ─── Notification Card (via notify.py subprocess) ─────────────────────────────
# ---- CLI reply → paste into originating terminal ----------------------------
_TERM_PROG_TO_APP = {
    "Apple_Terminal": "Terminal",
    "iTerm.app":      "iTerm2",
    "vscode":         "Code",
    "Hyper":          "Hyper",
    "WezTerm":        "WezTerm",
    "tabby":          "Tabby",
    "kitty":          "kitty",
    "WarpTerminal":   "Warp",
}

def _paste_into_terminal(text: str, term_program: str, session_id: str = ""):
    """
    Paste `text` into the originating terminal AND press Enter (execute),
    preserving the user's current frontmost app where possible.

    Strategy (matches agentwatch.zsh v3.5):
      • Apple Terminal  → `do script "<text>" in tab t`      (no focus change)
      • iTerm2          → `tell session s to write text …`   (no focus change)
      • VSCode / Warp / etc. → save frontmost → activate → ⌘V → Enter
        → restore frontmost (brief flash, ~300 ms)
    """
    log_path = os.path.join(DB_DIR, "notify.log")

    def _log(msg):
        try:
            with open(log_path, "a") as f:
                f.write(f"[paste-main {datetime.now().strftime('%H:%M:%S')}] {msg}\n")
        except Exception:
            pass

    try:
        subprocess.run(["pbcopy"], input=text.encode("utf-8"),
                       capture_output=True, timeout=2)
    except Exception as e:
        _log(f"pbcopy failed: {e}")

    app_name = _TERM_PROG_TO_APP.get(term_program or "", "")
    _log(f"term_prog={term_program} app={app_name} sess_id={session_id or '<empty>'} text_len={len(text)}")

    if not app_name:
        _log("unknown terminal → clipboard only (no auto-execute)")
        return

    # Escape for AppleScript (backslash first, then double-quote)
    esc = text.replace("\\", "\\\\").replace('"', '\\"')

    # ── Apple Terminal: do script (no focus change, auto-executes) ───────────
    # Note: the extension-relay path doesn't carry $TTY, so we fall back to
    # the frontmost tab of the front window. For tab-accurate targeting,
    # use the zsh path (CLI-originated events).
    if app_name == "Terminal":
        script = (
            f'tell application "Terminal"\n'
            f'    do script "{esc}" in selected tab of front window\n'
            f'    return "ok:fallback_front"\n'
            f'end tell\n'
        )
        try:
            r = subprocess.run(["/usr/bin/osascript", "-e", script],
                               capture_output=True, timeout=5)
            _log(f"Terminal osascript rc={r.returncode} "
                 f"out={r.stdout.decode(errors='replace').strip()[:160]}")
        except Exception as e:
            _log(f"Terminal osascript error: {e}")
        return

    # ── iTerm2: write text (no activate, auto-execute) ──────────────────────
    if app_name == "iTerm2":
        if session_id:
            iterm_uuid = session_id.split(":")[-1]
            script = (
                'tell application "iTerm2"\n'
                '    set foundSess to missing value\n'
                '    repeat with w in windows\n'
                '        repeat with t in tabs of w\n'
                '            repeat with s in sessions of t\n'
                '                if (unique id of s as string) contains '
                f'"{iterm_uuid}" then\n'
                '                    set foundSess to s\n'
                '                    exit repeat\n'
                '                end if\n'
                '            end repeat\n'
                '            if foundSess is not missing value then exit repeat\n'
                '        end repeat\n'
                '        if foundSess is not missing value then exit repeat\n'
                '    end repeat\n'
                '    if foundSess is missing value then\n'
                '        tell current session of current window to '
                f'write text "{esc}"\n'
                '        return "ok:current"\n'
                '    else\n'
                f'        tell foundSess to write text "{esc}"\n'
                '        return "ok:targeted"\n'
                '    end if\n'
                'end tell\n'
            )
        else:
            script = (
                'tell application "iTerm2"\n'
                '    tell current session of current window to '
                f'write text "{esc}"\n'
                '    return "ok:current"\n'
                'end tell\n'
            )
        try:
            r = subprocess.run(["/usr/bin/osascript", "-e", script],
                               capture_output=True, timeout=5)
            _log(f"iTerm2 osascript rc={r.returncode} "
                 f"out={r.stdout.decode(errors='replace').strip()[:160]}")
        except Exception as e:
            _log(f"iTerm2 osascript error: {e}")
        return

    # ── VSCode / Warp / …: save frontmost → activate → ⌘V → Enter → restore ─
    pre_keystroke = ""
    if app_name == "Code":
        pre_keystroke = (
            'tell application "System Events" to keystroke "`" '
            'using {command down}\n'
            'delay 0.1\n'
        )

    script = (
        'set prevFront to ""\n'
        'try\n'
        '    tell application "System Events"\n'
        '        set prevFront to name of first application process '
        'whose frontmost is true\n'
        '    end tell\n'
        'end try\n'
        f'tell application "{app_name}" to activate\n'
        'repeat 25 times\n'
        '    tell application "System Events"\n'
        '        try\n'
        '            if (name of first application process whose '
        f'frontmost is true) is "{app_name}" then exit repeat\n'
        '        end try\n'
        '    end tell\n'
        '    delay 0.1\n'
        'end repeat\n'
        'delay 0.15\n'
        f'{pre_keystroke}'
        'tell application "System Events" to keystroke "v" '
        'using {command down}\n'
        'delay 0.05\n'
        'tell application "System Events" to key code 36\n'
        'delay 0.15\n'
        f'if prevFront is not "" and prevFront is not "{app_name}" then\n'
        '    try\n'
        '        tell application prevFront to activate\n'
        '    end try\n'
        '    return "ok:restored:" & prevFront\n'
        'else\n'
        '    return "ok"\n'
        'end if\n'
    )
    try:
        r = subprocess.run(["/usr/bin/osascript", "-e", script],
                           capture_output=True, timeout=6)
        out = r.stdout.decode(errors='replace').strip()
        err = r.stderr.decode(errors='replace').strip()
        _log(f"{app_name} osascript rc={r.returncode} out={out[:160]} err={err[:160]}")
        if "not allowed" in err or "1002" in err or "-25211" in err:
            _log(f"ACCESSIBILITY DENIED for {app_name} — "
                 "System Settings ▸ Privacy & Security ▸ Accessibility")
    except Exception as e:
        _log(f"{app_name} osascript error: {e}")


class NotificationCard:
    """
    Launches notify.py as a subprocess.
    notify.py creates an AppKit NSPanel that floats above ALL apps including
    VSCode, Chrome, and fullscreen Mission Control spaces.
    on_action is called on a daemon thread with the result string.
    """

    def __init__(self, title, site_name, event_type, preview, on_action,
                 tab_id=None, window_id=None):
        self._args = [
            sys.executable, NOTIFY_PY,
            str(title),
            str(site_name),
            str(event_type),
            str(preview or ""),
            str(PORT),
            str(tab_id) if tab_id is not None else "",
            str(window_id) if window_id is not None else "",
        ]
        self._on_action = on_action
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        result = "close"
        try:
            proc = subprocess.Popen(
                self._args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, stderr = proc.communicate(timeout=120)
            result = stdout.decode("utf-8", errors="replace").strip()
            if stderr:
                err = stderr.decode("utf-8", errors="replace").strip()
                if err and "NSApplicationDelegate" not in err:
                    print(f"[AgentWatch] notify stderr: {err[:300]}")
        except subprocess.TimeoutExpired:
            proc.kill()
        except Exception as e:
            print(f"[AgentWatch] notify launch error: {e}")

        if result and self._on_action and result not in ("close", ""):
            try:
                self._on_action(result)
            except Exception as e:
                print(f"[AgentWatch] on_action error: {e}")


# ─── Menu Bar App ──────────────────────────────────────────────────────────────
class AgentWatchApp(rumps.App):
    def __init__(self):
        super().__init__("AgentWatch", title="👁")
        self.event_loop = None
        self._clients: set = set()
        self._enabled = True
        self._active_count = 0
        self._event_websockets: dict = {}
        self._active_sessions: dict = {}
        self._session_timers: dict = {}

        cfg = load_config()
        llm = cfg.get("llm", {}) if isinstance(cfg, dict) else {}
        self._llm_cfg = LLMConfig(
            enabled=bool(llm.get("enabled", False)),
            endpoint=str(llm.get("endpoint", "http://localhost:11434")),
            model=str(llm.get("model", "llama3.2:1b")),
            timeout_ms=int(llm.get("timeout_ms", 1500)),
        )

        self._build_menu()
        threading.Thread(target=self._run_ws_server, daemon=True).start()

    def _build_menu(self):
        self.menu = [
            rumps.MenuItem("Status: Idle", callback=None), None,
            rumps.MenuItem("Notifications: ON", callback=self._toggle_enabled), None,
            rumps.MenuItem("Test Notification",   callback=self._test_notification),
            rumps.MenuItem("Open History Folder", callback=self._open_history),
            rumps.MenuItem("Open Dashboard",      callback=self._open_dashboard), None,
            rumps.MenuItem("Quit AgentWatch",     callback=self._quit),
        ]

    def _update_icon(self):
        self.title = f"👁 {self._active_count}" if self._active_count else "👁"

    # ─── WebSocket ────────────────────────────────────────────────────────────
    def _run_ws_server(self):
        self.event_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.event_loop)
        self.event_loop.run_until_complete(self._ws_serve())

    async def _ws_serve(self):
        print(f"[AgentWatch] WS on ws://localhost:{PORT}")
        import re
        origins = [
            re.compile(r"^chrome-extension://.*$"),
            re.compile(r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"),
            None,
        ]
        async with websockets.serve(self._ws_handler, "localhost", PORT,
                                    origins=origins):
            await asyncio.Future()

    async def _ws_handler(self, websocket):
        self._clients.add(websocket)
        self._active_count = len(self._clients)
        self._update_icon()
        try:
            async for raw in websocket:
                try:
                    await self._dispatch(json.loads(raw), websocket)
                except json.JSONDecodeError:
                    pass
        except (websockets.exceptions.ConnectionClosed,
                websockets.exceptions.ConnectionClosedError,
                websockets.exceptions.ConnectionClosedOK,
                websockets.exceptions.InvalidMessage,
                ConnectionResetError,
                OSError):
            # Chrome MV3 service worker goes to sleep → drops WS without
            # close frame. This is expected; skip the traceback.
            pass
        finally:
            self._clients.discard(websocket)
            self._active_count = max(0, self._active_count - 1)
            self._update_icon()

    async def _dispatch(self, data: dict, ws):
        t = data.get("type", "")
        if t == "AGENT_EVENT":
            key = f"{data.get('tabId','cli')}_{data.get('site','unknown')}"
            self._cancel_timer(key)
            self._active_sessions.pop(key, None)
            await self._on_agent_event(data, ws)
        elif t == "AGENT_GENERATING":
            self.title = f"⟳ {data.get('siteName','AI')}"
            key = f"{data.get('tabId','cli')}_{data.get('site','unknown')}"
            self._active_sessions[key] = {
                "site_name": data.get("siteName", "AI"),
                "tab_id":    data.get("tabId"),
                "window_id": data.get("windowId"),
            }
            self._start_timer(key)
        elif t == "AGENT_CONTEXT_SWITCH":
            self.title = f"👁 {self._active_count}" if self._active_count else "👁"
        elif t == "TEST":
            self._show_test_card()
        elif t == "LLM_CONFIG_UPDATE":
            self._update_llm_config(data)
        elif t == "STOP_MONITORING":
            sid  = data.get("sessionId", "")
            name = data.get("siteName",  "unknown")
            print(f"[AgentWatch] Stop monitoring requested: {name} ({sid})")
            # PTY wrapper checks this flag via the relay socket; the wrapper's
            # _stop_event is set when it receives STOP_MONITORING matching its
            # session. For a full implementation, maintain a registry of active
            # PTYWrapper instances keyed by sessionId in main.py.
            # Minimal implementation: just log — the user can Ctrl-C the wrapper.


    # ─── Session fallback timer (Phase 3 minimal) ────────────────────────────
    # If a web AI agent (ChatGPT / Claude / Gemini …) says "generating" but
    # we never see the "done" event within 3 minutes, fire a PENDING card so
    # the user isn't left wondering. 3 min was chosen as a compromise: long
    # enough to cover real streaming replies, short enough to surface a
    # stuck tab. CLI commands do NOT use this timer — they fire their own
    # card on precmd exit via agentwatch.zsh.
    _FALLBACK_SECS = 3 * 60

    def _start_timer(self, key: str):
        self._cancel_timer(key)
        t = threading.Timer(self._FALLBACK_SECS, self._fallback_notify, args=(key,))
        t.daemon = True; t.start()
        self._session_timers[key] = t

    def _cancel_timer(self, key: str):
        if key in self._session_timers:
            self._session_timers[key].cancel()
            del self._session_timers[key]

    def _fallback_notify(self, key: str):
        sess = self._active_sessions.pop(key, None)
        self._session_timers.pop(key, None)
        if not sess or not self._enabled:
            return
        sn = sess.get("site_name", "AI")
        print(f"[AgentWatch] Fallback timer: {sn}")
        NotificationCard(
            title=f"{sn} · check status",
            site_name=sn,
            event_type="PENDING",
            preview="Task running >3 min — click Show to check.",
            on_action=lambda a: (
                asyncio.run_coroutine_threadsafe(
                    self._relay_focus(sess.get("tab_id"), sess.get("window_id")),
                    self.event_loop,
                ) if a == "show" else None
            ),
            tab_id=sess.get("tab_id"),
            window_id=sess.get("window_id"),
        )

    # ─── Agent event ──────────────────────────────────────────────────────────
    async def _on_agent_event(self, data: dict, ws):
        self.title = "👁"
        if not self._enabled:
            return
        result = await self._resolve_classification(data)
        data["category"]       = result.category
        data["categoryReason"] = result.reason
        data["categorySource"] = result.source
        save_event(data)

        if data.get("site") == "cli":
            await self._relay_to_clients(data)

        eid = f"{data.get('tabId','cli')}_{data.get('timestamp','')}"
        self._event_websockets[eid] = ws
        threading.Thread(
            target=self._fire_card,
            args=(data, result, eid),
            daemon=True,
        ).start()

    async def _relay_to_clients(self, data: dict):
        dead = set()
        for c in self._clients:
            try:
                await c.send(json.dumps({
                    **data, "type": "AGENT_EVENT", "relayedFromMacApp": True
                }))
            except Exception:
                dead.add(c)
        self._clients -= dead

    async def _resolve_classification(self, data: dict) -> LLMResult:
        if data.get("category") in ("ACTION_REQUIRED","INFORMATION","PENDING","COMPLETED"):
            return LLMResult(
                category=data["category"],
                needs_reply=bool(data.get("needsReply", False)),
                reason=str(data.get("classificationReason", "")),
                source=str(data.get("classificationSource", "extension")),
            )
        return await llm_classify(data, self._llm_cfg)

    def _fire_card(self, data: dict, result: LLMResult, eid: str):
        site_name = data.get("siteName", "AI")
        ev_type   = data.get("eventType", "COMPLETED")
        snippet   = data.get("messageText") or data.get("messageSnippet") or ""
        is_cli    = data.get("site") == "cli"

        def on_action(action: str):
            ws = self._event_websockets.pop(eid, None)

            if action.startswith("reply_text:"):
                text = action[len("reply_text:"):]
                if not text:
                    return
                if is_cli:
                    # CLI reply → paste directly into the originating terminal.
                    # Clipboard is still set (for safety / manual ⌘V).
                    _paste_into_terminal(
                        text,
                        data.get("termProgram", ""),
                        data.get("termSessionId", ""),
                    )
                elif ws and self.event_loop:
                    asyncio.run_coroutine_threadsafe(
                        self._send_to(ws, json.dumps({
                            "type": "REPLY_INJECT",
                            "tabId":    data.get("tabId"),
                            "windowId": data.get("windowId"),
                            "text":     text,
                        })),
                        self.event_loop,
                    )
                    try:
                        with sqlite3.connect(DB_PATH) as c:
                            c.execute(
                                "UPDATE events SET user_reply=? WHERE id="
                                "(SELECT id FROM events WHERE tab_id=? "
                                "ORDER BY id DESC LIMIT 1)",
                                (text, data.get("tabId")),
                            )
                            c.commit()
                    except Exception:
                        pass

            elif action == "show":
                if self.event_loop and data.get("tabId"):
                    asyncio.run_coroutine_threadsafe(
                        self._relay_focus(data.get("tabId"), data.get("windowId")),
                        self.event_loop,
                    )

        NotificationCard(
            title=f"{site_name} · {result.category}",
            site_name=site_name,
            event_type=ev_type,
            preview=snippet,
            on_action=on_action,
            tab_id=data.get("tabId"),
            window_id=data.get("windowId"),
        )

    async def _relay_focus(self, tab_id, window_id):
        msg = json.dumps({"type": "FOCUS_TAB",
                          "tabId": tab_id, "windowId": window_id})
        for c in list(self._clients):
            try: await c.send(msg)
            except Exception: pass

    async def _send_to(self, ws, msg: str):
        try: await ws.send(msg)
        except Exception: pass

    def _update_llm_config(self, data: dict):
        llm = data.get("llm", {}) or {}
        self._llm_cfg = LLMConfig(
            enabled=bool(llm.get("enabled", self._llm_cfg.enabled)),
            endpoint=str(llm.get("endpoint", self._llm_cfg.endpoint)),
            model=str(llm.get("model", self._llm_cfg.model)),
            timeout_ms=int(llm.get("timeout_ms", self._llm_cfg.timeout_ms)),
        )
        save_config({"llm": {
            "enabled":    self._llm_cfg.enabled,
            "endpoint":   self._llm_cfg.endpoint,
            "model":      self._llm_cfg.model,
            "timeout_ms": self._llm_cfg.timeout_ms,
        }})

    # ─── Menu actions ──────────────────────────────────────────────────────────
    def _show_test_card(self):
        NotificationCard(
            title="AgentWatch · TEST",
            site_name="AgentWatch",
            event_type="COMPLETED",
            preview=(
                "Mac App connected! notify.py is working.\n"
                "> AppKit NSPanel — floats above VSCode and all apps\n"
                "> Click Reply to test inline reply input\n"
                "> Click Show All to expand this message\n"
                "> Auto-dismisses in 90 seconds"
            ),
            on_action=lambda a: None,
        )

    def _toggle_enabled(self, sender):
        self._enabled = not self._enabled
        sender.title = "Notifications: ON" if self._enabled else "Notifications: OFF"

    def _test_notification(self, _):
        self._show_test_card()

    def _open_history(self, _):
        subprocess.Popen(["open", DB_DIR])

    def _open_dashboard(self, _):
        p = os.path.join(DB_DIR, "dashboard.html")
        _generate_dashboard(p)
        subprocess.Popen(["open", p])

    def _quit(self, _):
        rumps.quit_application()


# ─── Dashboard ────────────────────────────────────────────────────────────────
def _generate_dashboard(path: str):
    try:
        with sqlite3.connect(DB_PATH) as c:
            rows  = c.execute(
                "SELECT site_name,event_type,timestamp,duration_ms,url,"
                "message_snippet,user_reply,category,site,response_length "
                "FROM events ORDER BY id DESC LIMIT 500"
            ).fetchall()
            total = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    except Exception:
        rows, total = [], 0

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    C = sum(1 for r in rows if r[1]=="COMPLETED")
    E = sum(1 for r in rows if r[1] in ("ERROR","RATE_LIMITED"))
    CLI = sum(1 for r in rows if r[8]=="cli")
    AI  = len(rows) - CLI

    EC = {"COMPLETED":"#00ff9d","ERROR":"#ff3366","BLOCKED":"#ffb800",
          "PERMISSION":"#b026ff","DECISION":"#2684ff","RATE_LIMITED":"#ff3366"}
    SI = {"cli":"⌨","claude":"✦","chatgpt":"⊕","gemini":"✦","perplexity":"◎","grok":"⟡"}

    body = ""
    for (sn,et,ts,dur,url,snip,rep,cat,site,rl) in rows:
        co  = EC.get(et,"#64748b")
        d_  = f"{round(dur/1000)}s" if dur else "—"
        t_  = ts[:19].replace("T"," ") if ts else "—"
        ic  = SI.get(site or "","◈")
        sh  = f'<div class="snippet">"{(snip or "")[:120]}…"</div>' if snip else ""
        rh  = f'<div class="ur">↩ {rep}</div>' if rep else ""
        lh  = (f'<a href="{url}" target="_blank" class="ul">{url.split("?")[0][:50]}</a>'
               if url and url.startswith("http")
               else '<span class="ct">⌨ Terminal</span>' if site=="cli" else "")
        rsh = f'<span class="rs">{rl//1000}k</span>' if rl and rl>=1000 else ""
        body += (
            f'<tr class="er" data-t="{et}" data-s="{site or ""}">'
            f'<td><span class="ic">{ic}</span> {sn}</td>'
            f'<td><span class="bx" style="color:{co};border-color:{co}22;background:{co}11">{et}</span></td>'
            f'<td class="dm">{t_}</td><td class="dm">{d_} {rsh}</td>'
            f'<td>{lh}{sh}{rh}</td></tr>'
        )

    html = (
        f'<!DOCTYPE html><html><head><meta charset="UTF-8"><title>AgentWatch</title>'
        f'<style>*{{box-sizing:border-box;margin:0;padding:0}}'
        f':root{{--bg:#05080f;--s:#0d1117;--b:#1e3a4a;--t:#e2e8f0;--d:#64748b;--a:#00d4ff}}'
        f'html,body{{background:var(--bg);color:var(--t);font-family:system-ui,sans-serif;font-size:13px}}'
        f'.hdr{{padding:24px 28px;border-bottom:1px solid var(--b)}}'
        f'h1{{font-size:18px;color:var(--a);margin-bottom:3px}}.meta{{font-size:11px;color:var(--d)}}'
        f'.stats{{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-top:14px}}'
        f'.st{{background:var(--s);border:1px solid var(--b);border-radius:7px;padding:10px}}'
        f'.sv{{font-size:20px;font-weight:700;font-family:monospace}}.sl{{font-size:10px;color:var(--d);text-transform:uppercase;margin-top:2px}}'
        f'.tb{{padding:10px 28px;display:flex;gap:7px;background:var(--s);border-bottom:1px solid var(--b);position:sticky;top:0}}'
        f'.fb{{font-family:monospace;font-size:11px;padding:3px 10px;border-radius:999px;border:1px solid var(--b);background:transparent;color:var(--d);cursor:pointer}}'
        f'.fb.a,.fb:hover{{border-color:var(--a);color:var(--a)}}'
        f'.tw{{padding:0 28px 32px;overflow-x:auto}}table{{width:100%;border-collapse:collapse;margin-top:14px}}'
        f'th{{font-family:monospace;font-size:10px;text-transform:uppercase;color:var(--d);text-align:left;padding:7px 10px;border-bottom:1px solid var(--b)}}'
        f'td{{padding:9px 10px;border-bottom:1px solid rgba(30,58,74,0.3);vertical-align:top}}'
        f'tr:hover td{{background:#0d1117}}.hidden{{display:none}}'
        f'.ic{{width:22px;height:22px;display:inline-flex;align-items:center;justify-content:center;'
        f'background:var(--s);border:1px solid var(--b);border-radius:4px;font-size:12px;vertical-align:middle}}'
        f'.bx{{font-family:monospace;font-size:10px;font-weight:600;text-transform:uppercase;'
        f'padding:2px 7px;border-radius:999px;border:1px solid;white-space:nowrap}}'
        f'.dm{{font-family:monospace;font-size:11px;color:var(--d);white-space:nowrap}}'
        f'.snippet{{font-family:monospace;font-size:11px;color:#475569;font-style:italic;margin-top:3px}}'
        f'.ul{{font-family:monospace;font-size:11px;color:var(--a);text-decoration:none;display:block;margin-bottom:3px}}'
        f'.ct{{font-family:monospace;font-size:11px;color:#00ff9d;display:block;margin-bottom:3px}}'
        f'.ur{{font-size:11px;color:var(--a);margin-top:3px;font-style:italic}}'
        f'.rs{{font-family:monospace;font-size:10px;color:#334155}}'
        f'</style></head><body>'
        f'<div class="hdr"><h1>👁 AgentWatch</h1>'
        f'<div class="meta">{now_str} · {total} total</div>'
        f'<div class="stats">'
        f'<div class="st"><div class="sv" style="color:var(--a)">{len(rows)}</div><div class="sl">Loaded</div></div>'
        f'<div class="st"><div class="sv" style="color:#00ff9d">{C}</div><div class="sl">Done</div></div>'
        f'<div class="st"><div class="sv" style="color:#ff3366">{E}</div><div class="sl">Errors</div></div>'
        f'<div class="st"><div class="sv">{AI}</div><div class="sl">AI</div></div>'
        f'<div class="st"><div class="sv">{CLI}</div><div class="sl">CLI</div></div>'
        f'</div></div>'
        f'<div class="tb">'
        f'<button class="fb a" onclick="f(\'all\',this)">All</button>'
        f'<button class="fb" onclick="f(\'COMPLETED\',this)">Done</button>'
        f'<button class="fb" onclick="f(\'ERROR\',this)">Errors</button>'
        f'<button class="fb" onclick="f(\'DECISION\',this)">Decisions</button>'
        f'<button class="fb" onclick="f(\'cli\',this)">Terminal</button>'
        f'<button class="fb" onclick="location.reload()" style="margin-left:auto">⟳</button>'
        f'</div>'
        f'<div class="tw"><table>'
        f'<thead><tr><th>Source</th><th>Event</th><th>Time</th><th>Duration</th><th>Details</th></tr></thead>'
        f'<tbody>{body or "<tr><td colspan=5 style=text-align:center;padding:40px;color:var(--d)>No events.</td></tr>"}</tbody>'
        f'</table></div>'
        f'<script>let af="all";'
        f'function f(t,b){{af=t;document.querySelectorAll(".fb").forEach(x=>x.classList.remove("a"));b.classList.add("a");g();}}'
        f'function g(){{document.querySelectorAll(".er").forEach(r=>{{const ok=af==="all"||r.dataset.t===af||(af==="cli"&&r.dataset.s==="cli");r.classList.toggle("hidden",!ok);}});}}'
        f'</script></body></html>'
    )
    with open(path, "w") as f:
        f.write(html)


# ─── Entry ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("[AgentWatch] Starting v3.0...")
    print(f"[AgentWatch] WS: ws://localhost:{PORT}")
    print(f"[AgentWatch] DB: {DB_PATH}")
    print(f"[AgentWatch] notify.py: {NOTIFY_PY}")
    if not os.path.exists(NOTIFY_PY):
        print(f"[AgentWatch] WARNING: notify.py not found at {NOTIFY_PY}")
    AgentWatchApp().run()