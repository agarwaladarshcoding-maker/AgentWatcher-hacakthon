# AgentWatch — macOS Companion App + CLI Plugin

Native macOS menu bar app that receives events from the Chrome extension and fires **real macOS notifications** — the kind that pierce full-screen apps, appear in Notification Centre, and support click-to-focus.

---

## Architecture

```
Chrome Extension (content.js)
        │ detects AI event
        ▼
Chrome Extension (background.js)
        │ WebSocket client → ws://localhost:59452
        ▼
macOS Menu Bar App (main.py)  ←──────── zsh CLI plugin (agentwatch.zsh)
        │ fires native notification        │ wraps long terminal commands
        │ shows dialog for reply           │ notifies on complete/error
        │ stores in SQLite                 │
        ▼
Native macOS Notification (osascript)
        │ user replies via dialog
        ▼
Chrome Extension → content.js → injectReply() → AI input field
```

---

## Requirements

- macOS 10.14+
- Python 3.8+
- zsh (default on macOS 10.15+)

---

## Installation

### Step 1 — Install

```bash
cd agentwatch-mac
chmod +x install.sh
./install.sh
```

This will:
- Install Python dependencies (`rumps`, `websockets`)
- Copy `agentwatch.zsh` to `~/.agentwatch/agentwatch.zsh`
- Add `source ~/.agentwatch/agentwatch.zsh` to your `~/.zshrc`
- Create a launchd plist for optional auto-start at login

### Step 2 — Start the Menu Bar App

```bash
cd agentwatch-mac
python3 main.py
```

You'll see the **👁 eye icon** appear in your Mac menu bar (top-right area).

### Step 3 — Auto-start at Login (optional)

```bash
launchctl load ~/Library/LaunchAgents/com.agentwatch.app.plist
```

To stop:
```bash
launchctl unload ~/Library/LaunchAgents/com.agentwatch.app.plist
```

### Step 4 — Reload CLI Plugin

```bash
source ~/.zshrc
```

---

## Notification Permissions

The first time a notification fires, macOS will ask for permission.
If it doesn't appear:
1. System Preferences → Notifications
2. Find **Script Editor** or **Terminal** in the list
3. Set to **Allow Notifications**

---

## Features

### macOS Menu Bar App (`main.py`)
- **Menu bar icon** (👁) with live status
- **Native macOS notifications** via `osascript` — pierce full-screen apps
- **Reply dialog** — for DECISION/BLOCKED/PERMISSION events, shows a native macOS dialog to collect your reply and injects it back into the AI's input field
- **SQLite history** stored at `~/.agentwatch/history.db`
- **WebSocket server** on `localhost:59452` — Chrome extension connects automatically

### CLI Shell Plugin (`agentwatch.zsh`)
- **Automatic monitoring** — any command that takes longer than 10 seconds triggers a notification on completion/failure
- **Configurable threshold**: `export AW_MIN_DURATION_SECS=5` in your `.zshrc`
- **Control commands**:
  ```zsh
  aw-on      # enable monitoring (default)
  aw-off     # disable monitoring
  aw-status  # show current status
  ```
- **Manual wrap** — run any command with `aw` prefix for instant notification:
  ```zsh
  aw python3 train_model.py
  aw npm run build
  aw pytest
  ```
- Also sends events to Mac app when running (shows CLI events in notification history)

---

## How Reply Injection Works

1. AI agent fires DECISION/BLOCKED/PERMISSION event
2. Chrome extension notifies Mac app via WebSocket
3. Mac app fires native notification + shows **reply dialog**:
   ```
   ┌─────────────────────────────────────┐
   │ AgentWatch — Reply                  │
   │                                     │
   │ Claude is waiting for your answer:  │
   │ ┌─────────────────────────────────┐ │
   │ │ Yes, go with option A           │ │
   │ └─────────────────────────────────┘ │
   │              [Cancel] [Send Reply]  │
   └─────────────────────────────────────┘
   ```
4. Your reply is sent back via WebSocket to the Chrome extension
5. Extension uses React-compatible native setter to inject text into the AI's input field
6. The AI tab gets focused automatically

---

## History Database

Events are stored in SQLite at `~/.agentwatch/history.db`:

```sql
SELECT site_name, event_type, timestamp, duration_ms, user_reply
FROM events
ORDER BY id DESC
LIMIT 20;
```

Open the folder:
```bash
open ~/.agentwatch
```

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| App not starting | `pip3 install rumps websockets` |
| Notifications not showing | System Preferences → Notifications → Allow for Terminal/Script Editor |
| Extension not connecting | Check Mac app is running, port 59452 free: `lsof -i :59452` |
| Reply injection not working | Try clicking into the AI tab first, then reply |
| CLI plugin not working | `source ~/.zshrc` then test with `sleep 11` |
