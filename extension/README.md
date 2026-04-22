# AgentWatch — Chrome Extension

> Stop babysitting AI agents. Get notified the moment they finish, get stuck, or need your input.

---

## What it does

AgentWatch passively monitors 20 popular AI chat sites and fires a **native macOS / Chrome notification** the instant your AI agent:

| Event | Meaning |
|-------|---------|
| **COMPLETED** | Response is ready |
| **ERROR** | Something went wrong |
| **BLOCKED** | Agent appears stuck |
| **PERMISSION** | Approval required |
| **DECISION** | Agent asks a question |

**Zero babysitting. Zero API keys. No data leaves your machine.**

---

## Supported Sites (v1.0)

| Site | Domain |
|------|--------|
| ChatGPT | chat.openai.com / chatgpt.com |
| Claude | claude.ai |
| Gemini | gemini.google.com |
| Perplexity | perplexity.ai |
| Microsoft Copilot | copilot.microsoft.com |
| Grok | grok.com |
| Meta AI | meta.ai |
| Poe | poe.com |
| Phind | phind.com |
| You.com | you.com |
| HuggingFace Chat | huggingface.co/chat |
| Mistral | chat.mistral.ai |
| Groq | groq.com |
| DeepSeek | chat.deepseek.com |
| Pi AI | pi.ai |
| Character.ai | character.ai |
| Cohere | coral.cohere.com |
| Bing Copilot | bing.com/chat |

---

## Installation (Developer Mode)

### Step 1 — Get the extension files

Download the `chrome-extension` folder to your Mac. You should have:
```
chrome-extension/
├── manifest.json
├── background.js
├── content.js
├── fetch_patcher.js
├── popup.html
├── popup.js
├── popup.css
└── icons/
    ├── icon16.png
    ├── icon32.png
    ├── icon48.png
    └── icon128.png
```

### Step 2 — Generate icons (one-time setup)

```bash
cd chrome-extension
python3 generate_icons.py
```

This creates the PNG icons in `icons/`. You only need to do this once.

### Step 3 — Load in Chrome

1. Open Chrome
2. Go to: `chrome://extensions`
3. Enable **Developer mode** (toggle in the top-right corner)
4. Click **"Load unpacked"**
5. Select the `chrome-extension` folder (the one containing `manifest.json`)
6. Click **Open** / **Select Folder**

### Step 4 — Grant Notification Permission

When you open the popup for the first time, Chrome may prompt you to allow notifications. **Click Allow.**

Or go to: `chrome://settings/content/notifications` and ensure Chrome notifications are allowed.

### Step 5 — Pin the extension

1. Click the puzzle-piece **Extensions** icon in Chrome toolbar
2. Find **AgentWatch** and click the **pin** icon

---

## Usage

1. Open any supported AI site (e.g., `claude.ai`, `chat.openai.com`)
2. Send a message to the AI
3. Switch to another app or tab — AgentWatch is watching
4. When the AI finishes, you'll get a **desktop notification**
5. Click the notification (or "Jump to Tab") to return exactly to that tab

### Popup UI

Click the **AgentWatch icon** in your Chrome toolbar to open the popup:

- **Monitor tab** — Shows currently generating sessions with live timers
- **History tab** — Recent events with event type, duration, response size
- **Settings tab** — Enable/disable per-site, test notifications, clear history

---

## How Detection Works

AgentWatch uses three passive detection layers — **no account login, no API keys needed**:

1. **fetch/XHR Interceptor** — Patches `window.fetch` in MAIN world to detect SSE/streaming AI responses starting and completing
2. **WebSocket Watcher** — Monitors WebSocket connections for sites like Perplexity and Character.ai
3. **DOM Observer (Stop Button)** — Uses MutationObserver to watch for the stop button appearing/disappearing on AI chat pages

All three run silently in the background with configurable per-site hold-off timers to avoid false positives.

---

## Privacy

- All processing is **100% local** — your messages never leave your browser
- No analytics, no telemetry, no server
- History stored in Chrome's local storage (never synced)

---

## Packaging for Distribution

To create a `.crx` or submit to Chrome Web Store:

```bash
# In Chrome: Extensions → Pack Extension
# Or use the Chrome Extensions developer tools
```

---

## Roadmap

- [ ] macOS native Menu Bar app (receive events via WebSocket from extension)
- [ ] Native macOS notifications (pierce full-screen apps)
- [ ] Reply injection (type reply from notification)
- [ ] CLI/terminal agent monitoring (shell plugin)
- [ ] Log file watcher for IDE integrations
- [ ] Mobile push notifications

---

## License

MIT
