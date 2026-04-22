#!/usr/bin/env python3
"""
AgentWatch — notify.py  v5.0

Key changes from v4.0:
  • MULTI-TERMINAL: Accepts terminal info (tty, term_prog, term_session, aw_session_id)
    as args 8-11. Reads ~/.agentwatch/sessions/{aw_session_id} for full terminal context.
    Paste is done DIRECTLY in Python — no more zsh callback middleman.
  • FULL TEXT: Body shows the complete response text without truncation or ">" prefixes.
    Scrollable. Proper monospace rendering.
  • PASTE ARCHITECTURE: notify.py calls AppleScript itself for Apple Terminal, iTerm2,
    and VSCode. For each paste:
      - Apple Terminal: do script in tab matched by TTY
      - iTerm2: write text in session matched by UUID
      - VSCode/others: activate → ⌘V → Enter → restore focus
  • SESSION REGISTRY: Reads ~/.agentwatch/sessions/{sid}.json for tty + term details,
    so if the user has 5 terminal windows, each reply goes to the right one.
"""

import sys
import os
import subprocess
import threading
import json
import datetime

# ── Args ──────────────────────────────────────────────────────────────────────
def _arg(n, default=""):
    return sys.argv[n] if len(sys.argv) > n else default

TITLE       = _arg(1, "AgentWatch")
SITE        = _arg(2, "Terminal")
EV_TYPE     = _arg(3, "COMPLETED")
PREVIEW     = _arg(4, "")
WS_PORT     = _arg(5, "59452")
TAB_ID      = _arg(6, "")
WINDOW_ID   = _arg(7, "")
# v5.0: terminal routing args
ARG_TTY     = _arg(8, "")
ARG_TPROG   = _arg(9, "")
ARG_TSESS   = _arg(10, "")
ARG_SID     = _arg(11, "")
ARG_UPROMPT = _arg(12, "")          

AUTO_CLOSE_SECS = 90.0

# ── Dimensions ────────────────────────────────────────────────────────────────
W          = 460
MARGIN     = 18
HEADER_H   = 60
SEP_H      = 1
BTN_H      = 50
BODY_C     = 130
BODY_E     = 320
REPLY_H    = 180
CORNER     = 18.0

def _total(body_h):
    return HEADER_H + SEP_H + body_h + SEP_H + BTN_H

H_COMPACT  = _total(BODY_C)
H_EXPANDED = _total(BODY_E)
H_REPLY    = HEADER_H + SEP_H + REPLY_H


_PROMPT_MAX = 50        # hard trim for the Q: line
_ELLIPSIS   = "…"
 
 
def build_display_text(user_prompt: str, response_text: str, title: str) -> str:
    """
    Format the card body as:
 
        Q: <first 50 chars of user_prompt>…
        
        <full response_text>
 
    Falls back to plain response/title when user_prompt is absent.
    """
    raw_response = (response_text or "").strip()
    raw_prompt   = (user_prompt   or "").strip()
 
    # Truncate prompt to 50 chars
    if raw_prompt:
        if len(raw_prompt) > _PROMPT_MAX:
            prompt_line = raw_prompt[:_PROMPT_MAX] + _ELLIPSIS
        else:
            prompt_line = raw_prompt
        q_line = f"Q: {prompt_line}"
    else:
        q_line = ""
 
    if raw_response:
        if q_line:
            return f"{q_line}\n\n{raw_response}"
        return raw_response
 
    # No response yet — fall back to title
    if q_line:
        return f"{q_line}\n\n{title}"
    return title


# ── Logging ───────────────────────────────────────────────────────────────────
def _log(msg: str):
    stamp = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[notify {stamp}] {msg}"
    try:
        if sys.stderr.isatty():
            print(line, file=sys.stderr, flush=True)
    except Exception:
        pass
    try:
        log_dir = os.path.expanduser("~/.agentwatch")
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "notify.log"), "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── Session registry ──────────────────────────────────────────────────────────
def _read_session(sid: str) -> dict:
    """Read terminal info from session registry. Falls back to CLI args."""
    result = {
        "tty":             ARG_TTY,
        "term_program":    ARG_TPROG,
        "term_session_id": ARG_TSESS,
        "name":            "",
    }
    if not sid:
        return result
    path = os.path.expanduser(f"~/.agentwatch/sessions/{sid}")
    try:
        with open(path) as f:
            data = json.load(f)
        result.update({
            "tty":             data.get("tty", ARG_TTY),
            "term_program":    data.get("term_program", ARG_TPROG),
            "term_session_id": data.get("term_session_id", ARG_TSESS),
            "name":            data.get("name", ""),
        })
    except Exception:
        pass  # registry not found — use CLI args
    return result


# ── Terminal paste ─────────────────────────────────────────────────────────────
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

def _paste_to_terminal(text: str, session_info: dict):
    """
    Paste `text` into the originating terminal and press Enter.
    Uses session_info (from registry) for accurate routing.
    
    Strategy per terminal:
      Apple Terminal  → do script in tab matched by tty (no focus change)
      iTerm2          → write text in session matched by UUID (no focus change)
      VSCode/others   → save frontmost → activate → ⌘V → Enter → restore focus
    """
    tty_dev    = session_info.get("tty", "")
    term_prog  = session_info.get("term_program", "")
    term_sess  = session_info.get("term_session_id", "")
    app_name   = _TERM_PROG_TO_APP.get(term_prog, "")

    stamp = datetime.datetime.now().strftime("%H:%M:%S")
    log_path = os.path.expanduser("~/.agentwatch/notify.log")

    def _plog(msg):
        try:
            with open(log_path, "a") as f:
                f.write(f"[paste {stamp}] {msg}\n")
        except Exception:
            pass

    _plog(f"sid={ARG_SID} tty={tty_dev or '<empty>'} term={term_prog} app={app_name} len={len(text)}")

    # Copy to clipboard as safety net
    try:
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), capture_output=True, timeout=2)
    except Exception:
        pass

    if not app_name:
        _plog("unknown terminal → clipboard only")
        return

    esc = text.replace("\\", "\\\\").replace('"', '\\"')

    # ── Apple Terminal ─────────────────────────────────────────────────────────
    if app_name == "Terminal":
        if tty_dev:
            script = f'''
tell application "Terminal"
    set foundTab to missing value
    repeat with w in windows
        repeat with t in tabs of w
            try
                if (tty of t as string) is "{tty_dev}" then
                    set foundTab to t
                    exit repeat
                end if
            end try
        end repeat
        if foundTab is not missing value then exit repeat
    end repeat
    if foundTab is missing value then
        do script "{esc}" in selected tab of front window
        return "ok:fallback_front"
    else
        do script "{esc}" in foundTab
        return "ok:tabtargeted"
    end if
end tell
'''
        else:
            script = f'''
tell application "Terminal"
    do script "{esc}" in selected tab of front window
    return "ok:no_tty_fallback"
end tell
'''
        try:
            r = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, timeout=8)
            out = r.stdout.decode(errors="replace").strip()
            err = r.stderr.decode(errors="replace").strip()
            _plog(f"Terminal osascript rc={r.returncode} out={out[:160]}")
            if err: _plog(f"Terminal osascript err={err[:160]}")
        except Exception as e:
            _plog(f"Terminal osascript error: {e}")
        return

    # ── iTerm2 ─────────────────────────────────────────────────────────────────
    if app_name == "iTerm2":
        if term_sess:
            iterm_uuid = term_sess.split(":")[-1]
            script = f'''
tell application "iTerm2"
    set foundSess to missing value
    repeat with w in windows
        repeat with t in tabs of w
            repeat with s in sessions of t
                if (unique id of s as string) contains "{iterm_uuid}" then
                    set foundSess to s
                    exit repeat
                end if
            end repeat
            if foundSess is not missing value then exit repeat
        end repeat
        if foundSess is not missing value then exit repeat
    end repeat
    if foundSess is missing value then
        tell current session of current window to write text "{esc}"
        return "ok:current"
    else
        tell foundSess to write text "{esc}"
        return "ok:targeted"
    end if
end tell
'''
        else:
            script = f'''
tell application "iTerm2"
    tell current session of current window to write text "{esc}"
    return "ok:current"
end tell
'''
        try:
            r = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, timeout=8)
            out = r.stdout.decode(errors="replace").strip()
            _plog(f"iTerm2 osascript rc={r.returncode} out={out[:160]}")
        except Exception as e:
            _plog(f"iTerm2 osascript error: {e}")
        return

    # ── VSCode / Warp / WezTerm / Hyper / Tabby / kitty ──────────────────────
    pre_keystroke = ""
    if app_name == "Code":
        pre_keystroke = f'tell application "System Events" to keystroke "`" using {{command down}}\ndelay 0.1\n'

    script = f'''
set prevFront to ""
try
    tell application "System Events"
        set prevFront to name of first application process whose frontmost is true
    end tell
end try

tell application "{app_name}" to activate

repeat 25 times
    tell application "System Events"
        try
            if (name of first application process whose frontmost is true) is "{app_name}" then exit repeat
        end try
    end tell
    delay 0.1
end repeat
delay 0.15

{pre_keystroke}tell application "System Events" to keystroke "v" using {{command down}}
delay 0.05
tell application "System Events" to key code 36

delay 0.2
if prevFront is not "" and prevFront is not "{app_name}" then
    try
        tell application prevFront to activate
    end try
    return "ok:restored:" & prevFront
else
    return "ok"
end if
'''
    try:
        r = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, timeout=10)
        out = r.stdout.decode(errors="replace").strip()
        err = r.stderr.decode(errors="replace").strip()
        _plog(f"{app_name} osascript rc={r.returncode} out={out[:200]}")
        if "not allowed" in err or "1002" in err or "-25211" in err:
            _plog(f"ACCESSIBILITY DENIED for {app_name} — System Settings ▸ Privacy & Security ▸ Accessibility ▸ enable '{app_name}' ▸ relaunch")
    except Exception as e:
        _plog(f"{app_name} osascript error: {e}")


# ── WebSocket relay ───────────────────────────────────────────────────────────
def _ws_send(payload_dict):
    script = f"""
import asyncio, sys
try:
    import websockets
    async def go():
        try:
            async with websockets.connect('ws://localhost:{WS_PORT}', open_timeout=2) as ws:
                await ws.send(sys.argv[1])
        except Exception: pass
    asyncio.run(go())
except Exception: pass
"""
    try:
        subprocess.Popen(
            [sys.executable, "-c", script, json.dumps(payload_dict)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


# ── Entry ─────────────────────────────────────────────────────────────────────
def main():
    _log(f"=== notify.py v5.0 === title={TITLE!r} site={SITE!r} ev={EV_TYPE!r} sid={ARG_SID!r}")
    try:
        _run_appkit()
    except ImportError as e:
        _log(f"ImportError: {e} — Fix: pip3 install pyobjc-framework-Cocoa pyobjc-framework-Quartz")
        print("close", flush=True)
    except Exception as e:
        import traceback
        _log(f"FATAL: {type(e).__name__}: {e}\n{traceback.format_exc()}")
        print("close", flush=True)


def _run_appkit():
    _log("phase: importing AppKit/Foundation/Quartz")
    from AppKit import (
        NSApplication, NSPanel, NSScrollView, NSTextView,
        NSTextField, NSButton, NSView, NSColor, NSFont, NSScreen,
        NSBorderlessWindowMask, NSNonactivatingPanelMask,
        NSBackingStoreBuffered, NSAttributedString,
        NSFontAttributeName, NSForegroundColorAttributeName,
        NSWindowCollectionBehaviorCanJoinAllSpaces,
        NSWindowCollectionBehaviorStationary,
        NSWindowCollectionBehaviorFullScreenAuxiliary,
    )
    from Foundation import (
        NSObject, NSMakeRect, NSMakeSize, NSTimer,
        NSRunLoop, NSDefaultRunLoopMode,
    )
    import objc
    import Quartz
    _log("phase: imports ok")

    # ── Read session info early ────────────────────────────────────────────────
    session_info = _read_session(ARG_SID)
    _log(f"session info: tty={session_info['tty']!r} term={session_info['term_program']!r} "
         f"sess={session_info['term_session_id']!r}")

    # ── Focusable panel subclass ──────────────────────────────────────────────
    class _FocusablePanel(NSPanel):
        def canBecomeKeyWindow(self):    return True
        def canBecomeMainWindow(self):   return True
        def acceptsFirstResponder(self): return True

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(2)  # Accessory — no focus steal on startup

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _hex(h, a=1.0):
        h = h.lstrip('#')
        r, g, b = (int(h[i:i+2], 16)/255.0 for i in (0, 2, 4))
        return NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, a)

    def _cgcol(nscol):
        return Quartz.CGColorCreateGenericRGB(
            nscol.redComponent(), nscol.greenComponent(),
            nscol.blueComponent(), nscol.alphaComponent())

    # ── Palette ───────────────────────────────────────────────────────────────
    BG       = _hex("#16161E")
    BG2      = _hex("#20202C")
    BG_BTN   = _hex("#1A1A24")
    TEXT     = _hex("#F2F2F7")
    DIM      = _hex("#6E6E8F")
    ACCENT   = _hex("#00D4FF")
    DIM_TEXT = _hex("#9797B8")

    BADGE_COLORS = {
        "COMPLETED":    _hex("#00ff9d"),
        "ERROR":        _hex("#ff3366"),
        "BLOCKED":      _hex("#ffb800"),
        "PERMISSION":   _hex("#b026ff"),
        "DECISION":     _hex("#2f88ff"),
        "RATE_LIMITED": _hex("#ff3366"),
        "INFORMATION":  _hex("#64748b"),
        "PENDING":      _hex("#ffb800"),
    }
    badge_color = BADGE_COLORS.get(EV_TYPE, DIM)
    WIN_LEVEL = 25 + 1

    # ── Position ──────────────────────────────────────────────────────────────
    screen = NSScreen.mainScreen().visibleFrame()
    sx, sy = screen.origin.x, screen.origin.y
    sw, sh = screen.size.width, screen.size.height
    px = sx + sw - W - MARGIN
    py = sy + sh - H_COMPACT - MARGIN

    # ── Panel ─────────────────────────────────────────────────────────────────
    panel = _FocusablePanel.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(px, py, W, H_COMPACT),
        NSBorderlessWindowMask | NSNonactivatingPanelMask,
        NSBackingStoreBuffered, False,
    )
    panel.setLevel_(WIN_LEVEL)
    panel.setCollectionBehavior_(
        NSWindowCollectionBehaviorCanJoinAllSpaces |
        NSWindowCollectionBehaviorStationary |
        NSWindowCollectionBehaviorFullScreenAuxiliary
    )
    panel.setOpaque_(False)
    panel.setHasShadow_(True)
    panel.setBackgroundColor_(NSColor.clearColor())
    panel.setMovableByWindowBackground_(True)
    panel.setBecomesKeyOnlyIfNeeded_(False)
    panel.setWorksWhenModal_(True)

    content = panel.contentView()
    content.setWantsLayer_(True)

    container = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, W, H_COMPACT))
    container.setWantsLayer_(True)
    container.layer().setBackgroundColor_(_cgcol(BG))
    container.layer().setCornerRadius_(CORNER)
    container.layer().setMasksToBounds_(True)
    container.layer().setBorderWidth_(0.5)
    container.layer().setBorderColor_(
        Quartz.CGColorCreateGenericRGB(0.22, 0.22, 0.34, 1.0))
    content.addSubview_(container)

    # ── State ─────────────────────────────────────────────────────────────────
    state = {"result": None, "expanded": False, "reply_mode": False, "reply_tv": None}
    timer_ref  = [None]
    scroll_ref = [None]
    sep_mid    = [None]
    btn_views  = [None]
    reply_view = [None]
    expand_btn = [None]

    def _quit(result_val):
        if timer_ref[0]:
            try: timer_ref[0].invalidate()
            except Exception: pass
            timer_ref[0] = None
        state["result"] = result_val
        _log(f"phase: _quit result={result_val!r} — flushing stdout + hard exit")
        try:
            sys.stdout.write(result_val + "\n")
            sys.stdout.flush()
        except Exception as e:
            _log(f"stdout flush error: {e}")
        if result_val == "show" and TAB_ID:
            try:
                _ws_send({"type": "FOCUS_TAB", "tabId": int(TAB_ID),
                          "windowId": int(WINDOW_ID) if WINDOW_ID else None})
            except Exception as e:
                _log(f"ws_send focus error: {e}")
        try: panel.close()
        except Exception: pass
        os._exit(0)

    def _resize(new_h):
        old = panel.frame()
        delta = new_h - old.size.height
        panel.setFrame_display_animate_(
            NSMakeRect(old.origin.x, old.origin.y - delta, W, new_h), True, True)
        container.setFrame_(NSMakeRect(0, 0, W, new_h))
        _hdr.setFrame_(NSMakeRect(0, new_h - HEADER_H, W, HEADER_H))
        _sep_top.setFrame_(NSMakeRect(0, new_h - HEADER_H - SEP_H, W, SEP_H))

    def _label(text, x, y, w, h, font, color, parent, align=0):
        lbl = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
        lbl.setStringValue_(text)
        lbl.setEditable_(False); lbl.setBezeled_(False); lbl.setDrawsBackground_(False)
        lbl.setFont_(font); lbl.setTextColor_(color); lbl.setAlignment_(align)
        parent.addSubview_(lbl)
        return lbl

    def _btn(title, x, y, w, h, fg, parent, action):
        b = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
        b.setBezelStyle_(0); b.setBordered_(False); b.setWantsLayer_(True)
        b.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(
            title, {NSFontAttributeName: NSFont.systemFontOfSize_(12.5),
                    NSForegroundColorAttributeName: fg}))
        b.setTarget_(action_handler); b.setAction_(action)
        parent.addSubview_(b)
        return b

    def _vsep(x, y, h, parent):
        v = NSView.alloc().initWithFrame_(NSMakeRect(x, y, 0.5, h))
        v.setWantsLayer_(True)
        v.layer().setBackgroundColor_(Quartz.CGColorCreateGenericRGB(0.22, 0.22, 0.36, 1.0))
        parent.addSubview_(v)

    def _hsep(y, parent, w=W):
        s = NSView.alloc().initWithFrame_(NSMakeRect(0, y, w, SEP_H))
        s.setWantsLayer_(True)
        s.layer().setBackgroundColor_(Quartz.CGColorCreateGenericRGB(0.22, 0.22, 0.36, 1.0))
        parent.addSubview_(s)
        return s

    # ── Header ────────────────────────────────────────────────────────────────
    hdr = NSView.alloc().initWithFrame_(NSMakeRect(0, H_COMPACT - HEADER_H, W, HEADER_H))
    hdr.setWantsLayer_(True)
    hdr.layer().setBackgroundColor_(_cgcol(BG2))
    container.addSubview_(hdr)
    _hdr = hdr

    ICON_X = 8; ICON_SZ = 22
    ICON_Y  = (HEADER_H - ICON_SZ) // 2

    icon_bg = NSView.alloc().initWithFrame_(NSMakeRect(ICON_X, ICON_Y, ICON_SZ, ICON_SZ))
    icon_bg.setWantsLayer_(True)
    icon_bg.layer().setBackgroundColor_(Quartz.CGColorCreateGenericRGB(
        ACCENT.redComponent(), ACCENT.greenComponent(), ACCENT.blueComponent(), 0.18))
    icon_bg.layer().setCornerRadius_(ICON_SZ / 2)
    hdr.addSubview_(icon_bg)

    eye_lbl = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, ICON_SZ, ICON_SZ))
    eye_lbl.setStringValue_("👁")
    eye_lbl.setEditable_(False); eye_lbl.setBezeled_(False); eye_lbl.setDrawsBackground_(False)
    eye_lbl.setFont_(NSFont.systemFontOfSize_(14)); eye_lbl.setAlignment_(2)
    icon_bg.addSubview_(eye_lbl)

    CLOSE_W = 20; CLOSE_H = 20
    CLOSE_X = W - CLOSE_W - 12
    CLOSE_Y = (HEADER_H - CLOSE_H) // 2
    close_b = NSButton.alloc().initWithFrame_(NSMakeRect(CLOSE_X, CLOSE_Y, CLOSE_W, CLOSE_H))
    close_b.setTitle_("✕"); close_b.setBezelStyle_(0); close_b.setBordered_(False)
    close_b.setFont_(NSFont.systemFontOfSize_(11)); close_b.setWantsLayer_(True)
    hdr.addSubview_(close_b)

    badge_fg = BADGE_COLORS.get(EV_TYPE, DIM)
    et_display = EV_TYPE.replace("_", " ")
    badge_w = min(len(et_display) * 7 + 20, 120); badge_h = 22
    badge_x = CLOSE_X - 8 - badge_w
    badge_y = (HEADER_H - badge_h) // 2

    badge_bg = NSView.alloc().initWithFrame_(NSMakeRect(badge_x, badge_y, badge_w, badge_h))
    badge_bg.setWantsLayer_(True)
    badge_bg.layer().setBackgroundColor_(Quartz.CGColorCreateGenericRGB(
        badge_fg.redComponent(), badge_fg.greenComponent(), badge_fg.blueComponent(), 0.18))
    badge_bg.layer().setCornerRadius_(badge_h / 2)
    hdr.addSubview_(badge_bg)

    badge_lbl = NSTextField.alloc().initWithFrame_(NSMakeRect(0, (badge_h-14)//2, badge_w, 14))
    badge_lbl.setStringValue_(et_display)
    badge_lbl.setEditable_(False); badge_lbl.setBezeled_(False); badge_lbl.setDrawsBackground_(False)
    badge_lbl.setFont_(NSFont.monospacedSystemFontOfSize_weight_(9.5, 0.7))
    badge_lbl.setTextColor_(badge_fg); badge_lbl.setAlignment_(2)
    badge_bg.addSubview_(badge_lbl)

    name_x = ICON_X + ICON_SZ + 10
    name_w = badge_x - name_x - 8
    name_h = 20; name_y = (HEADER_H - name_h) // 2
    site_disp = SITE if len(SITE) <= 28 else (SITE[:27] + "…")
    _label(site_disp, name_x, name_y, name_w, name_h,
           NSFont.boldSystemFontOfSize_(14), TEXT, hdr)

    # ── Action handler ────────────────────────────────────────────────────────
    class _Handler(NSObject):
        def close_(self, _):   _quit("close")
        def dismiss_(self, _): _quit("dismiss")
        def show_(self, _):    _quit("show")

        def toggleExpand_(self, sender):
            if state["reply_mode"]: return
            state["expanded"] = not state["expanded"]
            new_h  = H_EXPANDED if state["expanded"] else H_COMPACT
            new_bh = BODY_E     if state["expanded"] else BODY_C
            new_lbl = "Preview" if state["expanded"] else "Show All"
            if expand_btn[0]:
                expand_btn[0].setAttributedTitle_(
                    NSAttributedString.alloc().initWithString_attributes_(
                        new_lbl, {NSFontAttributeName: NSFont.systemFontOfSize_(12.5),
                                  NSForegroundColorAttributeName: DIM_TEXT}))
            _resize(new_h)
            if sep_mid[0]:
                sep_mid[0].setFrame_(NSMakeRect(0, BTN_H + new_bh, W, SEP_H))
            if scroll_ref[0]:
                scroll_ref[0].setFrame_(NSMakeRect(16, BTN_H + 4, W - 32, new_bh - 8))

        def reply_(self, sender):
            if state["reply_mode"]: return
            state["reply_mode"] = True
            _log("action: reply (entering reply mode)")

            if scroll_ref[0]: scroll_ref[0].setHidden_(True)
            if sep_mid[0]:    sep_mid[0].setHidden_(True)
            if btn_views[0]:  btn_views[0].setHidden_(True)

            _resize(H_REPLY)

            rview = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, W, REPLY_H))
            rview.setWantsLayer_(True)
            rview.layer().setBackgroundColor_(_cgcol(BG))
            container.addSubview_(rview)
            reply_view[0] = rview

            _hsep(REPLY_H - SEP_H, rview)

            prompt_lbl = NSTextField.alloc().initWithFrame_(NSMakeRect(16, REPLY_H - 30, W - 32, 20))
            prompt_lbl.setStringValue_(f"Reply to {SITE}")
            prompt_lbl.setEditable_(False); prompt_lbl.setBezeled_(False)
            prompt_lbl.setDrawsBackground_(False)
            prompt_lbl.setFont_(NSFont.boldSystemFontOfSize_(12))
            prompt_lbl.setTextColor_(TEXT)
            rview.addSubview_(prompt_lbl)

            TF_Y = 60; TF_H = 70
            tf_scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(16, TF_Y, W - 32, TF_H))
            tf_scroll.setHasVerticalScroller_(True)
            tf_scroll.setAutohidesScrollers_(True)
            tf_scroll.setBorderType_(0)
            tf_scroll.setWantsLayer_(True)
            tf_scroll.layer().setBackgroundColor_(_cgcol(BG2))
            tf_scroll.layer().setCornerRadius_(8)
            tf_scroll.layer().setBorderWidth_(0.5)
            tf_scroll.layer().setBorderColor_(
                Quartz.CGColorCreateGenericRGB(0.28, 0.28, 0.46, 1.0))
            rview.addSubview_(tf_scroll)

            reply_tv = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, W - 32, TF_H))
            reply_tv.setEditable_(True); reply_tv.setSelectable_(True)
            reply_tv.setDrawsBackground_(False)
            reply_tv.setFont_(NSFont.monospacedSystemFontOfSize_weight_(13, 0.4))
            reply_tv.setTextColor_(TEXT)
            reply_tv.setInsertionPointColor_(ACCENT)
            reply_tv.setRichText_(False)
            reply_tv.setAutomaticQuoteSubstitutionEnabled_(False)
            reply_tv.setAutomaticDashSubstitutionEnabled_(False)
            reply_tv.textContainer().setWidthTracksTextView_(True)
            reply_tv.textContainer().setContainerSize_(NSMakeSize(W - 32, float("inf")))
            reply_tv.setVerticallyResizable_(True)
            reply_tv.setHorizontallyResizable_(False)
            reply_tv.setTextContainerInset_(NSMakeSize(6, 4))
            tf_scroll.setDocumentView_(reply_tv)
            state["reply_tv"] = reply_tv

            app.setActivationPolicy_(0)
            app.activateIgnoringOtherApps_(True)
            panel.makeKeyAndOrderFront_(None)
            panel.setInitialFirstResponder_(reply_tv)
            made_key = panel.makeFirstResponder_(reply_tv)
            _log(f"action: reply → makeFirstResponder={made_key}, isKey={panel.isKeyWindow()}")

            def _refocus():
                try:
                    app.activateIgnoringOtherApps_(True)
                    panel.makeKeyAndOrderFront_(None)
                    panel.makeFirstResponder_(reply_tv)
                    _log(f"action: reply refocus → isKey={panel.isKeyWindow()}")
                except Exception as e:
                    _log(f"refocus err: {e}")

            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                0.05, _RefocusTarget.alloc().initWithCallback_(_refocus), "fire:", None, False)

            SEND_W = 110; CANCEL_W = 78; BTN_Y = 12; BTN_H2 = 30

            cancel_b = NSButton.alloc().initWithFrame_(NSMakeRect(16, BTN_Y, CANCEL_W, BTN_H2))
            cancel_b.setBezelStyle_(0); cancel_b.setBordered_(False); cancel_b.setWantsLayer_(True)
            cancel_b.layer().setBackgroundColor_(Quartz.CGColorCreateGenericRGB(0.18, 0.18, 0.28, 1.0))
            cancel_b.layer().setCornerRadius_(7)
            cancel_b.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(
                "Cancel", {NSFontAttributeName: NSFont.systemFontOfSize_(12.5),
                           NSForegroundColorAttributeName: DIM_TEXT}))
            cancel_b.setTarget_(action_handler); cancel_b.setAction_("cancelReply:")
            cancel_b.setKeyEquivalent_("\x1b")
            rview.addSubview_(cancel_b)

            send_b = NSButton.alloc().initWithFrame_(
                NSMakeRect(16 + CANCEL_W + 8, BTN_Y, SEND_W, BTN_H2))
            send_b.setBezelStyle_(0); send_b.setBordered_(False); send_b.setWantsLayer_(True)
            send_b.layer().setBackgroundColor_(Quartz.CGColorCreateGenericRGB(
                ACCENT.redComponent() * 0.22, ACCENT.greenComponent() * 0.22,
                ACCENT.blueComponent() * 0.22, 1.0))
            send_b.layer().setCornerRadius_(7)
            send_b.layer().setBorderWidth_(0.5)
            send_b.layer().setBorderColor_(Quartz.CGColorCreateGenericRGB(
                ACCENT.redComponent() * 0.7, ACCENT.greenComponent() * 0.7,
                ACCENT.blueComponent() * 0.7, 1.0))
            send_b.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(
                "Send Reply", {NSFontAttributeName: NSFont.boldSystemFontOfSize_(12.5),
                               NSForegroundColorAttributeName: ACCENT}))
            send_b.setTarget_(action_handler); send_b.setAction_("sendReply:")
            send_b.setKeyEquivalent_("\r")
            send_b.setKeyEquivalentModifierMask_(1 << 20)
            rview.addSubview_(send_b)

            hint_lbl = NSTextField.alloc().initWithFrame_(
                NSMakeRect(16 + CANCEL_W + SEND_W + 20, BTN_Y + 8, 160, 16))
            hint_lbl.setStringValue_("⌘↵ send · esc cancel")
            hint_lbl.setEditable_(False); hint_lbl.setBezeled_(False)
            hint_lbl.setDrawsBackground_(False)
            hint_lbl.setFont_(NSFont.monospacedSystemFontOfSize_weight_(10, 0.3))
            hint_lbl.setTextColor_(DIM)
            rview.addSubview_(hint_lbl)

        def cancelReply_(self, _):
            try: app.setActivationPolicy_(2)
            except Exception: pass
            _quit("dismiss")

        def sendReply_(self, _):
            tv = state.get("reply_tv")
            if tv:
                text = (tv.string() or "").strip()
                _log(f"action: sendReply text_len={len(text)}")
                if text:
                    # ── PASTE DIRECTLY IN PYTHON ───────────────────────────────
                    # This is the key fix: no more zsh callback needed.
                    # notify.py has session_info and pastes directly here.
                    if not TAB_ID:  # CLI path
                        try:
                            app.setActivationPolicy_(2)
                        except Exception:
                            pass
                        _paste_to_terminal(text, session_info)
                    else:  # Web AI path — inject via WebSocket
                        _ws_send({
                            "type": "REPLY_INJECT",
                            "tabId": int(TAB_ID),
                            "windowId": int(WINDOW_ID) if WINDOW_ID else None,
                            "text": text,
                        })
                        try: app.setActivationPolicy_(2)
                        except Exception: pass
                    _quit(f"reply_text:{text}")
                    return
            try: app.setActivationPolicy_(2)
            except Exception: pass
            _quit("dismiss")

    action_handler = _Handler.alloc().init()

    close_b.setTarget_(action_handler)
    close_b.setAction_("close:")
    close_b.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(
        "✕", {NSFontAttributeName: NSFont.systemFontOfSize_(12),
              NSForegroundColorAttributeName: DIM}))

    # ── Separator under header ────────────────────────────────────────────────
    _sep_top = _hsep(H_COMPACT - HEADER_H - SEP_H, container)

    # ── Body: FULL text, no ">" prefix, no truncation ─────────────────────────
    raw = PREVIEW.strip() if PREVIEW else TITLE
    # Don't add ">" prefixes — show the actual response text as-is
    # This is what the user asked for: "display the entire text given as reply by agents"
    display_text = build_display_text(ARG_UPROMPT, PREVIEW, TITLE)

    body_y = BTN_H + SEP_H
    BODY_PAD = 16
    scroll = NSScrollView.alloc().initWithFrame_(
        NSMakeRect(BODY_PAD, body_y + 4, W - BODY_PAD * 2, BODY_C - 8))
    scroll.setHasVerticalScroller_(True)
    scroll.setAutohidesScrollers_(True)
    scroll.setBorderType_(0)
    scroll.setDrawsBackground_(False)
    container.addSubview_(scroll)
    scroll_ref[0] = scroll

    tv = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, W - BODY_PAD * 2, BODY_C - 8))
    tv.setEditable_(False); tv.setSelectable_(True)
    tv.setDrawsBackground_(False)
    tv.setFont_(NSFont.monospacedSystemFontOfSize_weight_(12, 0.35))
    tv.setTextColor_(TEXT)
    tv.textContainer().setWidthTracksTextView_(True)
    tv.textContainer().setContainerSize_(NSMakeSize(W - BODY_PAD * 2, float("inf")))
    tv.setVerticallyResizable_(True); tv.setHorizontallyResizable_(False)
    tv.setString_(display_text)
    scroll.setDocumentView_(tv)

    # ── Separator above buttons ───────────────────────────────────────────────
    s_mid = _hsep(BTN_H + BODY_C, container)
    sep_mid[0] = s_mid

    # ── Button row ────────────────────────────────────────────────────────────
    brow = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, W, BTN_H))
    brow.setWantsLayer_(True)
    brow.layer().setBackgroundColor_(_cgcol(BG_BTN))
    container.addSubview_(brow)
    btn_views[0] = brow

    BW = W / 4

    reply_bg = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, BW, BTN_H))
    reply_bg.setWantsLayer_(True)
    reply_bg.layer().setBackgroundColor_(Quartz.CGColorCreateGenericRGB(
        ACCENT.redComponent(), ACCENT.greenComponent(), ACCENT.blueComponent(), 0.08))
    brow.addSubview_(reply_bg)
    _btn("Reply", 0, 0, BW, BTN_H, ACCENT, brow, "reply:")
    _vsep(BW, 8, BTN_H - 16, brow)
    _btn("Show", BW, 0, BW, BTN_H, TEXT, brow, "show:")
    _vsep(BW * 2, 8, BTN_H - 16, brow)
    _btn("Dismiss", BW * 2, 0, BW, BTN_H, DIM_TEXT, brow, "dismiss:")
    _vsep(BW * 3, 8, BTN_H - 16, brow)
    eb = _btn("Show All", BW * 3, 0, BW, BTN_H, DIM_TEXT, brow, "toggleExpand:")
    expand_btn[0] = eb

    # ── Timer / Refocus targets ───────────────────────────────────────────────
    class _TimerTarget(NSObject):
        def fire_(self, _): _quit("close")

    class _RefocusTarget(NSObject):
        def initWithCallback_(self, cb):
            self = objc.super(_RefocusTarget, self).init()
            if self is None: return None
            self._cb = cb
            return self
        def fire_(self, _):
            try:
                if self._cb: self._cb()
            except Exception: pass

    timer_target = _TimerTarget.alloc().init()
    t = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        AUTO_CLOSE_SECS, timer_target, "fire:", None, False)
    timer_ref[0] = t

    # ── Show panel ────────────────────────────────────────────────────────────
    class _ShowOnceTarget(NSObject):
        def showPanel_(self, _):
            _log("phase: timer fired — showing panel")
            try:
                panel.orderFrontRegardless()
                panel.makeKeyWindow()
                _log("phase: panel drawn (focus preserved)")
            except Exception as e:
                _log(f"show-timer error: {e}")

    class _AppDelegate(NSObject):
        def applicationDidFinishLaunching_(self, note):
            _log("phase: applicationDidFinishLaunching")
            try:
                panel.orderFrontRegardless()
                panel.makeKeyWindow()
            except Exception as e:
                _log(f"delegate error: {e}")

    show_target = _ShowOnceTarget.alloc().init()
    delegate    = _AppDelegate.alloc().init()
    app.setDelegate_(delegate)

    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        0.01, show_target, "showPanel:", None, False)
    _log("phase: 0.01s show-timer scheduled")
    _log("phase: app.run()")
    app.run()
    _log("phase: app.run() returned — safety net")

    result = state.get("result") or "close"
    try:
        sys.stdout.write(result + "\n")
        sys.stdout.flush()
    except Exception:
        pass


if __name__ == "__main__":
    main()