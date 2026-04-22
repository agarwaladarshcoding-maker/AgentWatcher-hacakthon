# AgentWatch — PRD

## Original problem statement

AgentWatch = Chrome extension + macOS menu-bar app (`agentwatch-mac`) + zsh CLI
plugin. Phase 1 in this session had two remaining items:

1. **Reply button doesn't accept keyboard input** — NSPanel focus issue.
2. **Design polish** — tighter iOS-style card, bigger pill badge, cleaner
   spacing, SF Pro body.

## Architecture

- **macOS notifier** — `agentwatch-mac/notify.py`: standalone AppKit NSPanel
  (no tkinter, no osascript). Accessory activation policy so it never steals
  focus from VSCode / Chrome / etc.
- **Extension** — `extension/*`: content/background/llm_router for tab-state
  tracking and WebSocket reply injection.
- **Menu-bar app** — `agentwatch-mac/main.py`: orchestrator + WS server on
  port `59452`.
- **CLI plugin** — `agentwatch-mac/agentwatch.zsh`: post-exec hook triggering
  `notify.py`.

## Implemented this session (Jan 2026)

### Phase 1 — Reply focus + design polish  *(done, pending Mac verification)*

- Introduced `_FocusablePanel(NSPanel)` subclass overriding
  `canBecomeKeyWindow`, `canBecomeMainWindow`, `acceptsFirstResponder` — fixes
  the root cause of the reply text view receiving no keystrokes (borderless
  NSPanel defaults to `canBecomeKeyWindow = NO`).
- On Reply click: upgrade `NSApplicationActivationPolicy` Accessory → Regular,
  then `activateIgnoringOtherApps_` → `makeKeyAndOrderFront_` →
  `makeFirstResponder_(reply_tv)`. Added 50 ms retry (`_RefocusTarget`) as a
  window-server timing safety net.
- On Send / Cancel: downgrade back to Accessory before quit.
- `⌘↵` sends, `Esc` cancels (already present, verified wiring intact).
- Design: W 440→460, HEADER 54→58, BTN_H 48→50, CORNER 16→18, reply area
  150→170, margin 16→18.

### Phase 2 — Reply→terminal paste hardening  *(done, pending Mac verification)*

Audit first showed the codebase *already* had: no osascript banners, per-PID
multi-terminal keying, basic Reply→paste, 5s threshold, VSCode label. Actual
remaining issues — all four fixed:

- **Fix 2.1** — VSCode paste: send `⌘\`` (focus integrated terminal) before
  `⌘V` so replies can't land in the source editor. Applied in both
  `agentwatch.zsh _aw_paste_reply_into_terminal` and
  `main.py _paste_into_terminal`.
- **Fix 2.2** — osascript `delay 0.15 → 0.25` for reliable window-server
  hand-off after `activate`.
- **Fix 2.3** — `TERM_SESSION_ID` is now captured in preexec, forwarded to
  `_aw_show_card`, embedded in the AGENT_EVENT payload, and consumed by the
  paste helper. iTerm2 uses it to `select t` the exact session before paste,
  so multi-tab replies never go to the wrong tab.
- **Fix 2.4** — Code Runner prettifier: `…tempCodeRunnerFile…` commands
  become `Run C++ / Python / Node / Go / Rust / TS / Java / C (Code Runner)`
  in the card body (detected by extension in the command string).
- **Fix 2.5** — `aw-status` in VSCode now surfaces a hint about
  `code-runner.runInTerminal` (the main reason Code Runner notifications
  don't fire out-of-the-box). Hidden in other terminals.

Known limits (documented in `PHASE2_TEST_STEPS.md`):
- Code Runner with default `runInTerminal: false` cannot be hooked
  (Output panel is not a shell).
- Terminal.app has no AppleScript-queryable tab id → activates only the app,
  paste goes to frontmost tab.
- Reply after the originating terminal tab is closed → paste goes to
  whichever tab is frontmost. Not detected.

## What's **not** validated

Code was authored in a Linux sandbox — AppKit cannot run here. Python syntax
and static analysis pass; runtime validation is pending user Mac testing per
`agentwatch-mac/PHASE1_TEST_STEPS.md`.

## Prioritised backlog (remaining)

| Prio | Bug | Summary |
|------|-----|---------|
| P1   | #2      | 3–4 min fallback — actively re-check DOM/tab state instead of firing PENDING |
| P1   | #6      | Show → search all Chrome tabs across windows; else open new tab. SPA chat-switch state hardening |
| P1   | #4      | Further card design iteration (Gemini/ChatGPT/Claude badge variants, Show All polish) |
| P2   | #3      | Terminal.app multi-window targeting (currently only iTerm2 has reliable session-id routing) |
| P2   | #5      | CLI agent detection (ollama, claude-code, gemini-cli) + replicable test |
| P2   | —       | Reply after originating terminal tab closed → detect & warn instead of pasting to wrong tab |

## Next action items

1. User runs the Mac test steps in `PHASE1_TEST_STEPS.md`, sends screenshot +
   `~/.agentwatch/notify.log` tail.
2. If the caret blinks & text registers → green-light Phase 2 starting with
   Bug #1 + #7.
3. If `isKey=0` appears in the log, add `panel.setFloatingPanel_(False)`
   during reply mode as a secondary guard.
