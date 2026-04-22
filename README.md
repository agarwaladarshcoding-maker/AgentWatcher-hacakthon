# AgentWatch

AgentWatch monitors AI-agent tabs (ChatGPT, Claude, Gemini, Perplexity, Grok,
Copilot, Poe, Mistral, Groq, DeepSeek, and 10+ more) and pings you the instant
they finish streaming, error out, or need your input — with a **visible Reply
button** inside the popup and (optionally) a native macOS menu-bar companion.

---

## Features

### Phase 1 — UI / interaction
- Three-tab popup (Monitor / History / Settings) with a cohesive dark theme,
  DM Sans + JetBrains Mono typography, and accessible focus rings.
- Prominent **Reply** panel + per-event **Reply** buttons in History.
- Explicit **Show** buttons on both session and event cards (jump to the tab,
  falling back to opening the URL if the tab has been closed).
- Custom confirm modal (MV3 popups block `window.confirm`).
- Bundled 300 ms notification chime played via MV3 offscreen document.

### Phase 2 — SPA navigation tracking
SPAs like Gemini, ChatGPT, and Claude switch chats/projects without a full
page reload. Previously only body-level DOM mutations triggered a URL recheck,
which missed thread switches. The new tracker listens to:
- `history.pushState` / `replaceState` (patched in the MAIN world so React
  Router / Next.js-style navigations are captured).
- `popstate`, `hashchange`.
- `<title>` mutations on `<head>`.

Any of these fires an `AGENT_CONTEXT_SWITCH` message → the background service
worker drops the stale session for that tab → the badge and Monitor tab stay
accurate.

### Phase 3 — Local LLM router
The old pipeline labeled every finished generation as `COMPLETED`. Now every
event is routed through a tiny local LLM that reads the last assistant
message snippet and picks one of:
- `ACTION_REQUIRED` — user needs to reply/approve (→ Reply button lights up)
- `INFORMATION` — informational answer, no user action
- `PENDING` — mid-task / waiting on external step
- `COMPLETED` — task finished cleanly

The router lives in **two places** (they can operate independently):
- `chrome-extension/llm_router.js` — called from the service worker before
  the notification fires. 1.5 s hard timeout, graceful fallback to the
  heuristic derived from the raw eventType on any error.
- `agentwatch-mac/llm_router.py` — pure-stdlib (`urllib`), thread-safe,
  non-blocking. The blocking HTTP call is dispatched via `asyncio.to_thread`
  so neither the asyncio websocket loop nor the NSRunLoop that drives the
  menu-bar UI is ever blocked.

Both speak Ollama's `/api/generate` with `format: json`. Default model is
`llama3.2:1b` (2 GB, ~instant on Apple Silicon); `phi3:mini` and
`qwen2.5:1.5b` also work.

### Reply-button wiring
With categorization in place, `onAgentEvent` sets `pendingReply` whenever
the router returns `needsReply: true` (or the classic DECISION/BLOCKED/
PERMISSION signals fire). The reply panel then shows the message snippet
in context, the textarea focuses automatically, and `Ctrl/⌘+Enter` ships
the reply back to the tab via `chrome.tabs.sendMessage → INJECT_REPLY`.

---

## Install

### Chrome extension
1. Unzip `agentwatch-chrome-extension.zip`.
2. Open `chrome://extensions`, enable **Developer mode**, click **Load unpacked**, and select the unzipped folder.
3. Pin the toolbar icon. Click it to open the popup.

### macOS menu-bar companion (optional)
1. Unzip `agentwatch-mac.zip`.
2. `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
3. `python3 main.py`
4. Grant macOS Notifications permission when prompted (System Settings → Notifications → Script Editor / osascript).

### Local LLM (optional — enables smart categorization)
1. `brew install ollama` (or download from <https://ollama.com>).
2. `ollama serve` (starts the daemon at `http://localhost:11434`).
3. `ollama pull llama3.2:1b` (one-time, ~2 GB).
4. In the AgentWatch popup → Settings → Smart Categorization → toggle
   **Local LLM router** ON → click **Test connection** (should report
   `Connected · Ollama vX.Y.Z`).

When the router is off (or Ollama is unreachable), AgentWatch silently
falls back to the eventType-based heuristic — it never blocks.

---

## Build a zip
Two archives live in `/dist`:
- `agentwatch-chrome-extension.zip` — drag-and-drop into `chrome://extensions`.
- `agentwatch-mac.zip` — the Python menu-bar companion.

Re-create them with:
```bash
./scripts/package.sh
```
