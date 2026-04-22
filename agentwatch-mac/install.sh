#!/bin/bash
# AgentWatch macOS — install.sh  v6.0
# Installs AppKit notification card (notify.py) + zsh plugin + PTY wrapper.
set -e

echo ""
echo "▶ AgentWatch — Install v6.0"
echo "──────────────────────────"

# ── Python check ──────────────────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    echo "✗ Python 3 not found. Install from https://python.org"
    exit 1
fi
PY_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
echo "✓ Python $PY_VERSION"

# ── pyobjc ────────────────────────────────────────────────────────────────────
_need_pyobjc=0
python3 -c "import AppKit, Quartz" 2>/dev/null || _need_pyobjc=1
if [ "$_need_pyobjc" -eq 1 ]; then
    echo "▶ Installing pyobjc (AppKit + Quartz) …"
    pip3 install --quiet \
        'pyobjc-framework-Cocoa>=10.0' \
        'pyobjc-framework-Quartz>=10.0' \
        || { echo "✗ pip install pyobjc failed."; exit 1; }
fi
echo "✓ pyobjc (AppKit + Quartz) ready"

# ── websockets ────────────────────────────────────────────────────────────────
if ! python3 -c "import websockets" 2>/dev/null; then
    echo "▶ Installing websockets..."
    pip3 install --quiet websockets
fi
echo "✓ websockets ready"

# ── rumps ─────────────────────────────────────────────────────────────────────
if ! python3 -c "import rumps" 2>/dev/null; then
    echo "▶ Installing rumps..."
    pip3 install --quiet rumps
fi
echo "✓ rumps ready"

# ── Install directory layout ──────────────────────────────────────────────────
AW_DIR="$HOME/.agentwatch"
AW_UI_DIR="$AW_DIR/ui"
AW_WATCHERS_DIR="$AW_DIR/watchers/cli"
AW_SESSIONS_DIR="$AW_DIR/sessions"

mkdir -p "$AW_DIR" "$AW_UI_DIR" "$AW_WATCHERS_DIR" "$AW_SESSIONS_DIR"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Copy agentwatch.zsh ───────────────────────────────────────────────────────
cp "$SCRIPT_DIR/agentwatch.zsh" "$AW_DIR/agentwatch.zsh"
echo "✓ agentwatch.zsh → $AW_DIR/agentwatch.zsh"

# ── Copy notify.py — support both flat and ui/ subdirectory layouts ───────────
NOTIFY_SRC=""
for _try in \
    "$SCRIPT_DIR/ui/notify.py" \
    "$SCRIPT_DIR/notify.py"
do
    if [[ -f "$_try" ]]; then NOTIFY_SRC="$_try"; break; fi
done

if [[ -n "$NOTIFY_SRC" ]]; then
    cp "$NOTIFY_SRC" "$AW_UI_DIR/notify.py"
    chmod +x "$AW_UI_DIR/notify.py"
    # Also keep a copy at the old flat path for backwards compat
    cp "$NOTIFY_SRC" "$AW_DIR/notify.py"
    chmod +x "$AW_DIR/notify.py"
    echo "✓ notify.py → $AW_UI_DIR/notify.py  (+ $AW_DIR/notify.py)"
else
    echo "⚠️  notify.py not found in source tree — skipping"
fi

# ── Copy pty_wrapper.py ───────────────────────────────────────────────────────
PTY_SRC=""
for _try in \
    "$SCRIPT_DIR/watchers/cli/pty_wrapper.py" \
    "$SCRIPT_DIR/pty_wrapper.py"
do
    if [[ -f "$_try" ]]; then PTY_SRC="$_try"; break; fi
done

if [[ -n "$PTY_SRC" ]]; then
    cp "$PTY_SRC" "$AW_WATCHERS_DIR/pty_wrapper.py"
    chmod +x "$AW_WATCHERS_DIR/pty_wrapper.py"
    echo "✓ pty_wrapper.py → $AW_WATCHERS_DIR/pty_wrapper.py"
else
    echo "⚠️  pty_wrapper.py not found — agent wrapping will be disabled"
fi

# ── Copy optional mac app files ───────────────────────────────────────────────
for f in main.py llm_router.py notification_card.py requirements.txt; do
    [[ -f "$SCRIPT_DIR/$f" ]] && cp "$SCRIPT_DIR/$f" "$AW_DIR/$f" && echo "✓ $f → $AW_DIR/$f"
done

# ── .zshrc ────────────────────────────────────────────────────────────────────
ZSHRC="$HOME/.zshrc"
SOURCE_LINE='source ~/.agentwatch/agentwatch.zsh'

if [ -f "$ZSHRC" ] && grep -qF "$SOURCE_LINE" "$ZSHRC"; then
    echo "✓ .zshrc already has source line"
else
    {
        echo ""
        echo "# AgentWatch CLI monitoring"
        echo "$SOURCE_LINE"
    } >> "$ZSHRC"
    echo "✓ Added source line to ~/.zshrc"
fi

# ── Smoke test notify.py ──────────────────────────────────────────────────────
NOTIFY_BIN="$AW_UI_DIR/notify.py"
[[ ! -f "$NOTIFY_BIN" ]] && NOTIFY_BIN="$AW_DIR/notify.py"

if [[ -f "$NOTIFY_BIN" ]]; then
    echo ""
    echo "▶ Quick smoke test (notify.py)..."
    python3 "$NOTIFY_BIN" "Install Test" "Terminal" "COMPLETED" \
        "AgentWatch v6.0 installed! This card will auto-close in 90s." &
    NOTIFY_PID=$!
    sleep 3
    kill $NOTIFY_PID 2>/dev/null || true
    echo "✓ Notification displayed"
else
    echo "⚠️  Skipping smoke test — notify.py not available"
fi

# ── Smoke test pty_wrapper.py ─────────────────────────────────────────────────
PTY_BIN="$AW_WATCHERS_DIR/pty_wrapper.py"
if [[ -f "$PTY_BIN" ]]; then
    echo "▶ Quick pty_wrapper check..."
    if python3 -c "import pty, select, termios, tty, fcntl, struct, signal; print('ok')" 2>/dev/null | grep -q ok; then
        echo "✓ pty_wrapper dependencies OK"
    else
        echo "⚠️  pty_wrapper may not work — missing stdlib modules (unusual)"
    fi
fi

# ── launchd plist ─────────────────────────────────────────────────────────────
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/com.agentwatch.app.plist"
mkdir -p "$PLIST_DIR"

cat > "$PLIST_PATH" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.agentwatch.app</string>
    <key>ProgramArguments</key>
    <array>
        <string>$(which python3)</string>
        <string>$AW_DIR/main.py</string>
    </array>
    <key>RunAtLoad</key><false/>
    <key>KeepAlive</key><false/>
    <key>StandardOutPath</key><string>$AW_DIR/agentwatch.log</string>
    <key>StandardErrorPath</key><string>$AW_DIR/agentwatch.err</string>
</dict>
</plist>
PLIST
echo "✓ launchd plist → $PLIST_PATH"

echo ""
echo "──────────────────────────"
echo "✅ Install complete!  (v6.0)"
echo ""
echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║  IMPORTANT — the new plugin is NOT active in this shell yet.     ║"
echo "║  Open a NEW terminal window, then confirm:                        ║"
echo "║                                                                  ║"
echo "║    aw-status | head -3   → must show v6.0                        ║"
echo "║    aw-diagnose           → full health check                     ║"
echo "╚══════════════════════════════════════════════════════════════════╝"
echo ""
echo "▶ Test notification card:"
echo "   aw-test"
echo ""
echo "▶ Test CLI agent wrapping (gemini / claude / ollama):"
echo "   aw-agent gemini 'hello world'"
echo "   aw-agent ollama run llama3"
echo "   aw-agent claude 'what time is it'"
echo ""
echo "▶ Agent commands also auto-wrap when called directly if detected:"
echo "   gemini 'hello'      ← precmd skips it; pty_wrapper watches it"
echo "   (see aw-diagnose for agent detection list)"
echo ""
echo "▶ Optional: start mac app for history + dashboard:"
echo "   python3 ~/.agentwatch/main.py"
echo ""
echo "▶ Directory layout:"
echo "   ~/.agentwatch/agentwatch.zsh         ← zsh plugin"
echo "   ~/.agentwatch/ui/notify.py           ← AppKit notification card"
echo "   ~/.agentwatch/watchers/cli/pty_wrapper.py  ← agent PTY wrapper"
echo "   ~/.agentwatch/sessions/              ← terminal session registry"
echo "   ~/.agentwatch/notify.log             ← debug log"