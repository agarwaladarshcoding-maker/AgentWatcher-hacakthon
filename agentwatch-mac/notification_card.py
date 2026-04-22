"""
AgentWatch — notification_card.py  (Phase 1+2 redesign)

Matches the target screenshot design:
  ┌──────────────────────────────────────────────┐
  │ 👁  Terminal      COMPLETED              ✕   │
  ├──────────────────────────────────────────────┤
  │  > sleep 11                                  │  ← scrollable if long
  │  > Command completed in 11s                  │
  ├──────────────────────────────────────────────┤
  │   Reply  │  Show  │  Dismiss  │  Show All    │
  └──────────────────────────────────────────────┘

Key properties:
  - NSNonactivatingPanelMask → NEVER steals focus from whatever you are doing
  - NSWindowCollectionBehaviorCanJoinAllSpaces → appears over fullscreen apps
  - Appears top-RIGHT of screen, below menu bar
  - Show All / Preview toggle to expand/collapse full message
  - Scrollbar appears automatically when text is too long
  - on_action callback: 'reply' | 'show' | 'dismiss'  (called on bg thread)
  - Auto-dismisses after 90 seconds
"""

import threading
import objc

from Foundation import (
    NSObject, NSOperationQueue, NSMakeRect, NSMakeSize,
    NSString, NSTimer, NSRunLoop, NSDefaultRunLoopMode,
)
from AppKit import (
    NSPanel, NSScrollView, NSTextView, NSTextField, NSButton,
    NSView, NSColor, NSFont, NSScreen,
    NSBorderlessWindowMask, NSNonactivatingPanelMask,
    NSBackingStoreBuffered,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorStationary,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSAttributedString,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSMutableParagraphStyle,
    NSParagraphStyleAttributeName,
)
import Quartz


# ── Window positioning ────────────────────────────────────────────────────────
_W          = 400
_H_COMPACT  = 182   # header(46) + body(96) + buttons(40)
_H_EXPANDED = 342   # header(46) + body(256) + buttons(40)
_MARGIN     = 16    # distance from screen right/top edge
_CORNER     = 12.0

# ── Visual rhythm ─────────────────────────────────────────────────────────────
_PAD_H      = 14    # horizontal inner padding
_PAD_V      = 10    # vertical inner padding
_BTN_H      = 40    # button row height
_HEADER_H   = 46    # header row height
_BODY_COMPACT  = _H_COMPACT  - _HEADER_H - _BTN_H   # 96
_BODY_EXPANDED = _H_EXPANDED - _HEADER_H - _BTN_H   # 256

# Window level above status bar items
_WINDOW_LEVEL = 25 + 1   # kCGStatusWindowLevelKey + 1

# ── Color helpers ─────────────────────────────────────────────────────────────
def _rgb(r, g, b, a=1.0):
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(r/255, g/255, b/255, a)

def _hex(h, a=1.0):
    h = h.lstrip('#')
    r, g, b = (int(h[i:i+2], 16) for i in (0, 2, 4))
    return _rgb(r, g, b, a)

# ── Palette ───────────────────────────────────────────────────────────────────
_BG         = _hex("#1a1a24")   # deep dark, slight blue
_BG_HEADER  = _hex("#222230")
_BG_BODY    = _hex("#14141c")
_BORDER     = _hex("#2e2e48")
_TEXT       = _hex("#eaeaf2")
_TEXT_DIM   = _hex("#8888a8")
_ACCENT     = _hex("#00d4ff")

_BADGE_COLORS = {
    "COMPLETED":    (_hex("#00ff9d"),       _hex("#00ff9d", 0.14)),
    "ERROR":        (_hex("#ff3366"),       _hex("#ff3366", 0.14)),
    "BLOCKED":      (_hex("#ffb800"),       _hex("#ffb800", 0.14)),
    "PERMISSION":   (_hex("#b026ff"),       _hex("#b026ff", 0.14)),
    "DECISION":     (_hex("#2684ff"),       _hex("#2684ff", 0.14)),
    "RATE_LIMITED": (_hex("#ff3366"),       _hex("#ff3366", 0.14)),
    "INFORMATION":  (_hex("#64748b"),       _hex("#64748b", 0.14)),
}


# ── Rounded NSView ────────────────────────────────────────────────────────────
class _RoundedView(NSView):
    def initWithFrame_color_radius_(self, frame, color, radius):
        self = objc.super(_RoundedView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.setWantsLayer_(True)
        layer = self.layer()
        layer.setCornerRadius_(radius)
        layer.setMasksToBounds_(True)
        r = color.redComponent()
        g = color.greenComponent()
        b = color.blueComponent()
        a = color.alphaComponent()
        layer.setBackgroundColor_(Quartz.CGColorCreateGenericRGB(r, g, b, a))
        return self

    def isFlipped(self):
        return True   # top-left origin


# ── Main Card ─────────────────────────────────────────────────────────────────
class NotificationCard(NSObject):
    """
    Non-activating floating notification card.
    Call from ANY thread — schedules itself on the main AppKit thread.
    """

    # ── Public factory ────────────────────────────────────────────────────────
    # NOTE: NOT named 'show' — that clashes with NSView's ObjC -show selector
    # and raises BadPrototypeError on pyobjc / Python 3.14.
    @classmethod
    @objc.python_method
    def create(cls, title, site_name, event_type, preview, on_action):
        card = cls.alloc().init()
        card._title      = title
        card._site_name  = site_name
        card._event_type = event_type
        card._preview    = (preview or '').strip()
        card._on_action  = on_action
        card._expanded   = False
        card._panel      = None
        card._timer      = None
        card._expand_btn = None
        card._scroll     = None
        card._container  = None
        NSOperationQueue.mainQueue().addOperationWithBlock_(card._build)
        return card

    # ── Build on main thread ──────────────────────────────────────────────────
    def _build(self):
        screen = NSScreen.mainScreen().visibleFrame()
        sx = screen.origin.x
        sy = screen.origin.y
        sw = screen.size.width
        sh = screen.size.height
        x  = sx + sw - _W - _MARGIN
        y  = sy + sh - _H_COMPACT - _MARGIN

        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, _W, _H_COMPACT),
            NSBorderlessWindowMask | NSNonactivatingPanelMask,
            NSBackingStoreBuffered,
            False,
        )
        panel.setLevel_(_WINDOW_LEVEL)
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces |
            NSWindowCollectionBehaviorStationary |
            NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        panel.setOpaque_(False)
        panel.setHasShadow_(True)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setMovableByWindowBackground_(True)

        content = panel.contentView()
        content.setWantsLayer_(True)

        # Outer container with rounded corners + border
        container = _RoundedView.alloc().initWithFrame_color_radius_(
            NSMakeRect(0, 0, _W, _H_COMPACT), _BG, _CORNER
        )
        container.layer().setBorderWidth_(0.5)
        container.layer().setBorderColor_(
            Quartz.CGColorCreateGenericRGB(0.18, 0.18, 0.30, 1.0)
        )
        content.addSubview_(container)

        self._panel     = panel
        self._container = container
        self._compact_h = _H_COMPACT
        self._expand_h  = _H_EXPANDED

        self._build_header(container)
        self._build_body(container)
        self._build_buttons(container)

        # Auto-dismiss after 90 s
        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            90.0, self, "_autoClose:", None, False
        )

        panel.orderFrontRegardless()

    # ── Header ────────────────────────────────────────────────────────────────
    def _build_header(self, parent):
        H = _HEADER_H
        W = _W

        hdr = _RoundedView.alloc().initWithFrame_color_radius_(
            NSMakeRect(0, _H_COMPACT - H, W, H), _BG_HEADER, 0
        )
        hdr.layer().setCornerRadius_(_CORNER)
        parent.addSubview_(hdr)
        self._header_view = hdr

        # Eye emoji
        eye = NSTextField.alloc().initWithFrame_(NSMakeRect(_PAD_H, 10, 26, 26))
        eye.setStringValue_("👁")
        eye.setEditable_(False); eye.setBezeled_(False); eye.setDrawsBackground_(False)
        eye.setFont_(NSFont.systemFontOfSize_(15))
        hdr.addSubview_(eye)

        # Site name
        name = NSTextField.alloc().initWithFrame_(NSMakeRect(_PAD_H + 28, 13, 150, 22))
        name.setStringValue_(self._site_name)
        name.setEditable_(False); name.setBezeled_(False); name.setDrawsBackground_(False)
        name.setFont_(NSFont.boldSystemFontOfSize_(13))
        name.setTextColor_(_TEXT)
        hdr.addSubview_(name)

        # Badge
        et = self._event_type
        badge_fg, badge_bg_c = _BADGE_COLORS.get(et, (_TEXT_DIM, _hex("#64748b", 0.14)))
        badge_w = min(len(et) * 7 + 20, 130)
        badge_x = _PAD_H + 28 + 155
        badge_container = _RoundedView.alloc().initWithFrame_color_radius_(
            NSMakeRect(badge_x, 14, badge_w, 20), badge_bg_c, 10
        )
        hdr.addSubview_(badge_container)

        badge_lbl = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 4, badge_w, 16))
        badge_lbl.setStringValue_(et)
        badge_lbl.setEditable_(False); badge_lbl.setBezeled_(False)
        badge_lbl.setDrawsBackground_(False)
        badge_lbl.setFont_(NSFont.monospacedSystemFontOfSize_weight_(9, 0.6))
        badge_lbl.setTextColor_(badge_fg)
        badge_lbl.setAlignment_(2)  # center
        badge_container.addSubview_(badge_lbl)

        # ✕ close button
        close_x = W - 28
        close = NSButton.alloc().initWithFrame_(NSMakeRect(close_x, 12, 18, 22))
        close.setTitle_("✕")
        close.setBezelStyle_(0); close.setBordered_(False)
        close.setFont_(NSFont.systemFontOfSize_(11))
        close.setTarget_(self); close.setAction_("_close:")
        hdr.addSubview_(close)

    # ── Body: scrollable monospace text ──────────────────────────────────────
    def _build_body(self, parent):
        self._refresh_body(parent, _BODY_COMPACT)

    def _refresh_body(self, parent, body_h):
        # Remove old scroll if rebuilding
        if self._scroll is not None:
            self._scroll.removeFromSuperview()

        body_y = _BTN_H
        body_w = _W - _PAD_H * 2

        scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(_PAD_H, body_y, body_w, body_h)
        )
        scroll.setHasVerticalScroller_(True)
        scroll.setAutohidesScrollers_(True)
        scroll.setBorderType_(0)
        scroll.setDrawsBackground_(False)
        scroll.setWantsLayer_(True)

        tv = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, body_w, body_h))
        tv.setEditable_(False); tv.setSelectable_(True)
        tv.setDrawsBackground_(False)
        tv.setFont_(NSFont.monospacedSystemFontOfSize_weight_(11.5, 0.4))
        tv.setTextColor_(_TEXT)
        tv.textContainer().setWidthTracksTextView_(True)
        tv.textContainer().setContainerSize_(NSMakeSize(body_w, float('inf')))
        tv.setTextContainerInset_(NSMakeSize(14, 12))
        tv.setVerticallyResizable_(True)
        tv.setHorizontallyResizable_(False)

        raw = self._preview if self._preview else self._title
        lines = raw.split('\n') if raw else [self._title]
        formatted = '\n'.join(
            f"> {line}" if not line.startswith('>') else line
            for line in lines if line.strip()
        ) or f"> {self._title}"
        tv.setString_(formatted)

        scroll.setDocumentView_(tv)
        parent.addSubview_(scroll)
        self._scroll = scroll
        self._text_view = tv

    # ── Buttons ───────────────────────────────────────────────────────────────
    def _build_buttons(self, parent):
        # Separator line above buttons
        sep = NSView.alloc().initWithFrame_(NSMakeRect(0, _BTN_H, _W, 0.5))
        sep.setWantsLayer_(True)
        sep.layer().setBackgroundColor_(
            Quartz.CGColorCreateGenericRGB(0.18, 0.18, 0.30, 1.0)
        )
        parent.addSubview_(sep)

        labels  = ["Reply",    "Show",    "Dismiss",   "Show All"]
        actions = ["_reply:",  "_show:",  "_dismiss:", "_toggleExpand:"]
        n = len(labels)
        btn_w = _W / n

        for i, (lbl, sel) in enumerate(zip(labels, actions)):
            bx  = i * btn_w
            btn = NSButton.alloc().initWithFrame_(NSMakeRect(bx, 0, btn_w, _BTN_H))
            btn.setTitle_(lbl)
            btn.setBezelStyle_(0); btn.setBordered_(False)
            btn.setFont_(NSFont.systemFontOfSize_(12.5))
            btn.setTarget_(self); btn.setAction_(sel)
            btn.setWantsLayer_(True)

            fg = _ACCENT if lbl == "Reply" else _TEXT
            btn.setAttributedTitle_(
                NSAttributedString.alloc().initWithString_attributes_(
                    lbl,
                    {
                        NSFontAttributeName: NSFont.systemFontOfSize_(12.5),
                        NSForegroundColorAttributeName: fg,
                    }
                )
            )
            parent.addSubview_(btn)

            # Vertical separator
            if i > 0:
                vs = NSView.alloc().initWithFrame_(NSMakeRect(bx, 6, 0.5, _BTN_H - 12))
                vs.setWantsLayer_(True)
                vs.layer().setBackgroundColor_(
                    Quartz.CGColorCreateGenericRGB(0.18, 0.18, 0.30, 1.0)
                )
                parent.addSubview_(vs)

            if lbl == "Show All":
                self._expand_btn = btn

    # ── Button actions ────────────────────────────────────────────────────────
    def _fire(self, action):
        self._close_panel()
        if self._on_action and action not in ("close",):
            threading.Thread(target=self._on_action, args=(action,), daemon=True).start()

    def _reply_(self, sender):      self._fire("reply")
    def _show_(self, sender):       self._fire("show")
    def _dismiss_(self, sender):    self._fire("dismiss")
    def _close_(self, sender):      self._fire("close")
    def _autoClose_(self, timer):   self._close_panel()

    def _toggleExpand_(self, sender):
        self._expanded = not self._expanded
        new_h = _H_EXPANDED if self._expanded else _H_COMPACT
        new_label = "Preview" if self._expanded else "Show All"
        new_body_h = _BODY_EXPANDED if self._expanded else _BODY_COMPACT

        # Update button label
        sender.setAttributedTitle_(
            NSAttributedString.alloc().initWithString_attributes_(
                new_label,
                {
                    NSFontAttributeName: NSFont.systemFontOfSize_(12.5),
                    NSForegroundColorAttributeName: _TEXT,
                }
            )
        )

        if self._panel is None:
            return

        # Resize panel — grow upward (keep bottom edge fixed)
        old_frame = self._panel.frame()
        delta = new_h - old_frame.size.height
        new_frame = NSMakeRect(
            old_frame.origin.x,
            old_frame.origin.y - delta,
            _W, new_h
        )
        self._panel.setFrame_display_animate_(new_frame, True, True)

        # Resize container
        self._container.setFrame_(NSMakeRect(0, 0, _W, new_h))

        # Reposition header (always at top)
        self._header_view.setFrame_(NSMakeRect(0, new_h - _HEADER_H, _W, _HEADER_H))

        # Rebuild scroll body at new size
        self._refresh_body(self._container, new_body_h)

    def _close_panel(self):
        if self._timer:
            self._timer.invalidate()
            self._timer = None
        if self._panel:
            p = self._panel
            self._panel = None
            NSOperationQueue.mainQueue().addOperationWithBlock_(lambda: p.close())


# ── Drop-in wrapper matching existing API ────────────────────────────────────
# main.py currently does: NotificationCard(title, site, ev_type, preview, cb)
# This class keeps that interface identical.
class NotificationCardWrapper:
    def __init__(self, title, site_name, event_type, preview, on_action):
        NotificationCard.create(title, site_name, event_type, preview, on_action)