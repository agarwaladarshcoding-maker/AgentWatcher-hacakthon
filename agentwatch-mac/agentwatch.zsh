# ─────────────────────────────────────────────────────────────────────────────
# AgentWatch CLI Plugin — agentwatch.zsh  v7.0
#
# v7.0 changes:
#   • Claude CLI: "claude" and "claude-code" commands routed to the new
#     dedicated claude/wrapper.py with accurate permission + response detection.
#   • Fixed agent detection: claude binary is now correctly identified whether
#     invoked as `claude`, `claude-code`, or via npx.
#   • precmd dedup: track last-fired (cmd, exit_code) to prevent re-fires when
#     shell redraws prompt without running a new command.
#   • Minimum duration check moved to ms level (AW_MIN_DURATION_MS) so very
#     fast commands are always suppressed even if threshold is 0.
# ─────────────────────────────────────────────────────────────────────────────

AW_VERSION="7.0"

# ── Config ────────────────────────────────────────────────────────────────────
: ${AW_MIN_DURATION_SECS:=1}
: ${AW_MIN_DURATION_MS:=400}
: ${AW_MAC_APP_PORT:=59452}
: ${AW_MAX_CMD_LEN:=80}
: ${AW_PYTHON:=python3}
: ${AW_TERMINAL_NAME:=""}

# ── Locate scripts ────────────────────────────────────────────────────────────
_AW_SELF="${${(%):-%x}:A}"
_AW_DIR="${_AW_SELF:h}"

_AW_NOTIFY=""
for _aw_try in \
    "${_AW_DIR}/ui/notify.py" \
    "${_AW_DIR}/notify.py" \
    "$HOME/.agentwatch/ui/notify.py" \
    "$HOME/.agentwatch/notify.py"
do
    if [[ -f "$_aw_try" ]]; then _AW_NOTIFY="$_aw_try"; break; fi
done

_AW_PTY_WRAPPER=""
for _aw_try in \
    "${_AW_DIR}/watchers/cli/pty_wrapper.py" \
    "${_AW_DIR}/pty_wrapper.py" \
    "$HOME/.agentwatch/watchers/cli/pty_wrapper.py" \
    "$HOME/.agentwatch/pty_wrapper.py"
do
    if [[ -f "$_aw_try" ]]; then _AW_PTY_WRAPPER="$_aw_try"; break; fi
done

# ── Session Registry ──────────────────────────────────────────────────────────
_AW_SESSIONS_DIR="$HOME/.agentwatch/sessions"
mkdir -p "$_AW_SESSIONS_DIR" 2>/dev/null

_AW_SID=$($AW_PYTHON -c "import os; print(os.urandom(4).hex())" 2>/dev/null || echo "p$$")

_aw_write_registry() {
    local tty_now="${TTY:-$(tty 2>/dev/null)}"
    $AW_PYTHON - "$_AW_SID" "$tty_now" "${TERM_PROGRAM:-}" \
                "${TERM_SESSION_ID:-}" "$$" "${AW_TERMINAL_NAME:-}" 2>/dev/null <<'PYEOF'
import json, os, sys, time
sid, tty, tp, tsi, pid, name = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6]
d = os.path.expanduser("~/.agentwatch/sessions")
os.makedirs(d, exist_ok=True)
with open(os.path.join(d, sid), "w") as f:
    json.dump({"tty": tty, "term_program": tp, "term_session_id": tsi,
               "pid": int(pid), "name": name, "created": time.time()}, f)
PYEOF
}
_aw_write_registry

_aw_refresh_registry() {
    $AW_PYTHON - "$_AW_SID" "${TTY:-}" "${TERM_PROGRAM:-}" \
                "${TERM_SESSION_ID:-}" "${AW_TERMINAL_NAME:-}" 2>/dev/null <<'PYEOF'
import json, os, sys
sid, tty, tp, tsi, name = sys.argv[1:]
path = os.path.join(os.path.expanduser("~/.agentwatch/sessions"), sid)
try:
    with open(path) as f: d = json.load(f)
    if d.get("tty") != tty or d.get("term_session_id") != tsi:
        d.update({"tty": tty, "term_program": tp, "term_session_id": tsi, "name": name})
        with open(path, "w") as f: json.dump(d, f)
except Exception: pass
PYEOF
}

_aw_cleanup() { rm -f "$_AW_SESSIONS_DIR/$_AW_SID" 2>/dev/null; }
trap '_aw_cleanup' EXIT HUP INT TERM

# ── Per-shell state ───────────────────────────────────────────────────────────
typeset -A _AW_CMD_MAP _AW_START_MAP _AW_TERM_PROG_MAP
typeset -A _AW_TERM_SESS_MAP _AW_TTY_MAP _AW_FIRED_MAP
typeset -A _AW_LAST_CMD_MAP   # dedup: track last (cmd, exitcode) per shell
_AW_ENABLED=1
_AW_WARNED_MISSING_NOTIFY=0

# ── Helpers ───────────────────────────────────────────────────────────────────
_aw_ms() { $AW_PYTHON -c "import time; print(int(time.time()*1000))" 2>/dev/null || echo 0; }
_aw_truncate() { local s="$1" max="${2:-$AW_MAX_CMD_LEN}"; (( ${#s} > max )) && echo "${s:0:$max}…" || echo "$s"; }
_aw_mac_app_up() {
    $AW_PYTHON -c "
import socket, sys
try:
    s = socket.create_connection(('localhost', $AW_MAC_APP_PORT), timeout=0.3)
    s.close(); sys.exit(0)
except: sys.exit(1)" 2>/dev/null
}
_aw_warn_missing_notify() {
    [[ $_AW_WARNED_MISSING_NOTIFY -eq 0 ]] && { _AW_WARNED_MISSING_NOTIFY=1; print -u2 "[AgentWatch] notify.py not found. Re-run install.sh"; }
}

# ── Agent detector ────────────────────────────────────────────────────────────
_aw_detect_agent() {
    local cmd="$1" base="${${1%% *}##*/}"
    # Strip common path prefixes
    base="${base##*/}"

    case "$base" in
        # Claude CLI — must come before generic "claude" check
        claude)         echo "claude";       return ;;
        claude-code)    echo "claude-code";  return ;;
        # Other agents
        ollama)         echo "ollama";       return ;;
        gemini)         echo "gemini";       return ;;
        gemini-cli)     echo "gemini-cli";   return ;;
        aider)          echo "aider";        return ;;
        codex)          echo "codex";        return ;;
        sgpt|shell_gpt) echo "sgpt";         return ;;
        mods)           echo "mods";         return ;;
        llm)            echo "llm";          return ;;
        cursor)         echo "cursor";       return ;;
        gpt)            echo "gpt";          return ;;
        openai)         echo "openai-cli";   return ;;
        continue)       echo "continue";     return ;;
        cody)           echo "cody";         return ;;
        gh)
            [[ "$cmd" == *"copilot"* ]] && { echo "gh-copilot"; return; } ;;
    esac

    # npx / bunx / pnpm dlx invocations
    case "$cmd" in
        *"@anthropic-ai/claude"*|*"claude-code"*|*"bunx claude"*|*"pnpm dlx claude"*)
            echo "claude"; return ;;
        *"@google/gemini"*|*"npx gemini"*|*"bunx gemini"*) echo "gemini"; return ;;
        *"ollama run "*) echo "ollama"; return ;;
        *"npx aider"*|*"uvx aider"*) echo "aider"; return ;;
    esac
    echo ""
}

# ── Show card (non-agent commands) ────────────────────────────────────────────
_aw_show_card() {
    local title="$1" site="$2" ev_type="$3" preview="$4"
    local term_prog="${5:-${TERM_PROGRAM:-}}"
    local sess_id="${6:-${TERM_SESSION_ID:-}}"
    local tty_dev="${7:-${TTY:-$(tty 2>/dev/null)}}"
    local aw_sid="${8:-$_AW_SID}"

    if [[ -z "$_AW_NOTIFY" || ! -f "$_AW_NOTIFY" ]]; then _aw_warn_missing_notify; return; fi

    local aw_log="${AW_LOG:-$HOME/.agentwatch/notify.log}"
    mkdir -p "${aw_log:h}" 2>/dev/null

    {
        local output
        output=$(
            $AW_PYTHON "$_AW_NOTIFY" \
                "$title" "$site" "$ev_type" "$preview" \
                "$AW_MAC_APP_PORT" "" "" \
                "$tty_dev" "$term_prog" "$sess_id" "$aw_sid" \
            2>>"$aw_log"
        )
        if [[ "$output" == reply_text:* ]]; then
            echo "[zsh ${$(date '+%H:%M:%S')}] reply dispatched (${#${output#reply_text:}} chars)" >>"$aw_log"
        fi
    } &!
}

# ── PTY agent wrapper ─────────────────────────────────────────────────────────
_aw_run_with_pty() {
    local agent_name="$1"; shift
    local term_prog="${TERM_PROGRAM:-}"
    local sess_id="${TERM_SESSION_ID:-}"
    local tty_dev="${TTY:-$(tty 2>/dev/null)}"
    local cwd_label; cwd_label=$(basename "$PWD" 2>/dev/null || echo "shell")

    local site_name
    if [[ -n "$AW_TERMINAL_NAME" ]]; then
        site_name="${AW_TERMINAL_NAME} · ${cwd_label}"
    else
        case "$term_prog" in
            vscode)         site_name="VSCode · $cwd_label" ;;
            Apple_Terminal) site_name="Terminal · $cwd_label" ;;
            iTerm.app)      site_name="iTerm · $cwd_label" ;;
            WarpTerminal)   site_name="Warp · $cwd_label" ;;
            WezTerm)        site_name="WezTerm · $cwd_label" ;;
            "")             site_name="Terminal · $cwd_label" ;;
            *)              site_name="$term_prog · $cwd_label" ;;
        esac
    fi

    if [[ -n "$_AW_PTY_WRAPPER" ]]; then
        $AW_PYTHON "$_AW_PTY_WRAPPER" \
            --sid    "$_AW_SID" \
            --tty    "$tty_dev" \
            --term   "$term_prog" \
            --sess   "$sess_id" \
            --notify "$_AW_NOTIFY" \
            --site   "$site_name" \
            --port   "$AW_MAC_APP_PORT" \
            --agent  "$agent_name" \
            -- "$@"
    else
        # Fallback: run directly, notify on exit
        "$@"
        local ec=$?
        _aw_show_card "${agent_name} · finished" "$site_name" "COMPLETED" \
            "Session ended" "$term_prog" "$sess_id" "$tty_dev" "$_AW_SID"
        return $ec
    fi
}

# ── Relay to mac app ──────────────────────────────────────────────────────────
_aw_relay() {
    local payload="$1"
    {
        $AW_PYTHON -c "
import asyncio, websockets, sys
async def go(p):
    try:
        async with websockets.connect('ws://localhost:$AW_MAC_APP_PORT', open_timeout=1) as ws:
            await ws.send(p)
    except Exception: pass
asyncio.run(go(sys.argv[1]))
" "$payload" 2>/dev/null
    } &!
}

_aw_payload() {
    $AW_PYTHON -c "
import json, sys
print(json.dumps({'type':'AGENT_EVENT','eventType':sys.argv[1],'site':'cli',
    'siteName':sys.argv[5],'url':'terminal://local','title':sys.argv[2],
    'responseLength':0,'durationMs':int(sys.argv[3]),'timestamp':sys.argv[4],
    'messageSnippet':sys.argv[2],'termProgram':sys.argv[6],'termSessionId':sys.argv[7]}))
" "$1" "$2" "$3" "$4" "$5" "$6" "${7:-}" 2>/dev/null
}

# ── Zsh hooks ─────────────────────────────────────────────────────────────────
_aw_preexec() {
    [[ $_AW_ENABLED -eq 0 ]] && return
    _aw_refresh_registry
    _AW_CMD_MAP[$$]="$1"
    _AW_START_MAP[$$]=$(_aw_ms)
    _AW_TERM_PROG_MAP[$$]="${TERM_PROGRAM:-}"
    _AW_TERM_SESS_MAP[$$]="${TERM_SESSION_ID:-}"
    _AW_TTY_MAP[$$]="${TTY:-$(tty 2>/dev/null)}"
}

_aw_precmd() {
    local exit_code=$?
    [[ -z "${_AW_CMD_MAP[$$]}" ]] && return $exit_code
    [[ $_AW_ENABLED -eq 0 ]] && { _AW_CMD_MAP[$$]=""; return $exit_code; }

    local start_ms="${_AW_START_MAP[$$]:-0}"
    local now_ms;  now_ms=$(_aw_ms)
    local duration_ms=$(( now_ms - start_ms ))
    local duration_s=$(( duration_ms / 1000 ))

    local cmd="${_AW_CMD_MAP[$$]}"
    local term_prog="${_AW_TERM_PROG_MAP[$$]:-${TERM_PROGRAM:-}}"
    local sess_id="${_AW_TERM_SESS_MAP[$$]:-${TERM_SESSION_ID:-}}"
    local tty_dev="${_AW_TTY_MAP[$$]:-${TTY:-$(tty 2>/dev/null)}}"
    local agent_kind; agent_kind=$(_aw_detect_agent "$cmd")

    _AW_CMD_MAP[$$]=""

    # ── Skip agent commands — handled by pty_wrapper ──────────────────────
    if [[ -n "$agent_kind" ]]; then
        return $exit_code
    fi

    # Skip interactive programs that have their own TUI
    case "${cmd%% *}" in
        top|htop|vim|vi|nano|emacs|less|more|man|watch|tail|ssh|mysql|psql|python|python3|node|irb|pry|iex)
            return $exit_code ;;
    esac

    # Duration threshold
    if (( duration_ms < AW_MIN_DURATION_MS )); then return $exit_code; fi
    (( duration_s < AW_MIN_DURATION_SECS )) && return $exit_code

    # Dedup: don't re-fire if same (cmd, exitcode) fired in last 2 seconds
    local dedup_key="${cmd:0:40}_${exit_code}"
    local last_fired="${_AW_FIRED_MAP[$dedup_key]:-0}"
    if (( now_ms - last_fired < 2000 )); then return $exit_code; fi
    _AW_FIRED_MAP[$dedup_key]=$now_ms
    # Prune map if too large
    (( ${#_AW_FIRED_MAP} > 100 )) && _AW_FIRED_MAP=()

    local short_cmd; short_cmd=$(_aw_truncate "$cmd")
    local cwd_label; cwd_label=$(basename "$PWD" 2>/dev/null || echo "shell")

    # Code Runner prettifier
    if [[ "$cmd" == *tempCodeRunnerFile* ]]; then
        case "$cmd" in
            *tempCodeRunnerFile.cpp*)  short_cmd="Run C++ (Code Runner)" ;;
            *tempCodeRunnerFile.c*)    short_cmd="Run C (Code Runner)" ;;
            *tempCodeRunnerFile.py*)   short_cmd="Run Python (Code Runner)" ;;
            *tempCodeRunnerFile.js*)   short_cmd="Run Node (Code Runner)" ;;
            *tempCodeRunnerFile.ts*)   short_cmd="Run TS (Code Runner)" ;;
            *tempCodeRunnerFile.java*) short_cmd="Run Java (Code Runner)" ;;
            *tempCodeRunnerFile.rs*)   short_cmd="Run Rust (Code Runner)" ;;
            *tempCodeRunnerFile.go*)   short_cmd="Run Go (Code Runner)" ;;
            *)                         short_cmd="Run (Code Runner)" ;;
        esac
    fi

    local site_name
    if [[ -n "$AW_TERMINAL_NAME" ]]; then
        site_name="${AW_TERMINAL_NAME} · ${cwd_label}"
    else
        case "$term_prog" in
            vscode)         site_name="VSCode · $cwd_label" ;;
            Apple_Terminal) site_name="Terminal · $cwd_label" ;;
            iTerm.app)      site_name="iTerm · $cwd_label" ;;
            WarpTerminal)   site_name="Warp · $cwd_label" ;;
            WezTerm)        site_name="WezTerm · $cwd_label" ;;
            "")             site_name="Terminal · $cwd_label" ;;
            *)              site_name="$term_prog · $cwd_label" ;;
        esac
    fi

    local dur_label
    (( duration_s >= 60 )) && dur_label="$((duration_s/60))m $((duration_s%60))s" || dur_label="${duration_s}s"

    local ev_type title
    if [[ $exit_code -eq 0 ]]; then
        ev_type="COMPLETED"; title="Done (${dur_label})"
    else
        ev_type="ERROR"; title="Failed [exit $exit_code] (${dur_label})"
    fi

    _aw_show_card "$title" "$site_name" "$ev_type" "$short_cmd" "$term_prog" "$sess_id" "$tty_dev" "$_AW_SID"

    if _aw_mac_app_up; then
        local ts; ts=$($AW_PYTHON -c "
from datetime import datetime,timezone
print(datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
" 2>/dev/null || date -u +"%Y-%m-%dT%H:%M:%SZ")
        local payload; payload=$(_aw_payload "$ev_type" "$cmd" "$duration_ms" "$ts" "$site_name" "$term_prog" "$sess_id")
        [[ -n "$payload" ]] && _aw_relay "$payload"
    fi

    return $exit_code
}

autoload -Uz add-zsh-hook
add-zsh-hook preexec _aw_preexec
add-zsh-hook precmd  _aw_precmd

# ── Control commands ──────────────────────────────────────────────────────────
aw-on()  { _AW_ENABLED=1; echo "[AgentWatch] ON  (threshold: ${AW_MIN_DURATION_SECS}s)"; }
aw-off() { _AW_ENABLED=0; echo "[AgentWatch] OFF"; }

aw-status() {
    local state="ON"; [[ $_AW_ENABLED -eq 0 ]] && state="OFF"
    echo "[AgentWatch] Version:     v${AW_VERSION:-?}  (source: $_AW_SELF)"
    if [[ -f "$HOME/.agentwatch/agentwatch.zsh" ]]; then
        local iv; iv=$(grep -m1 '^AW_VERSION=' "$HOME/.agentwatch/agentwatch.zsh" | cut -d'"' -f2)
        if [[ -n "$iv" && "$iv" != "$AW_VERSION" ]]; then
            echo "  ⚠️  STALE SHELL: loaded=v$AW_VERSION  installed=v$iv"
            echo "      Fix: open a new terminal, OR: source ~/.zshrc"
        fi
    fi
    echo "[AgentWatch] Status:      $state"
    echo "[AgentWatch] Threshold:   ${AW_MIN_DURATION_SECS}s"
    echo "[AgentWatch] Session ID:  $_AW_SID"
    echo "[AgentWatch] Terminal:    ${AW_TERMINAL_NAME:-(unnamed)}"
    echo "[AgentWatch] TTY:         ${TTY:-$(tty 2>/dev/null)}"
    echo "[AgentWatch] TERM_PROGRAM: ${TERM_PROGRAM:-(unset)}"
    if [[ -n "$_AW_NOTIFY" && -f "$_AW_NOTIFY" ]]; then
        echo "[AgentWatch] notify.py:   ✓  $_AW_NOTIFY"
    else
        echo "[AgentWatch] notify.py:   ✗  NOT FOUND (run install.sh)"
    fi
    if [[ -n "$_AW_PTY_WRAPPER" && -f "$_AW_PTY_WRAPPER" ]]; then
        echo "[AgentWatch] pty_wrapper: ✓  $_AW_PTY_WRAPPER"
    else
        echo "[AgentWatch] pty_wrapper: ✗  NOT FOUND — agent wrapping disabled"
    fi
    if _aw_mac_app_up; then
        echo "[AgentWatch] Mac App:     ✓  connected (port $AW_MAC_APP_PORT)"
    else
        echo "[AgentWatch] Mac App:     –  offline"
    fi
}

# ── aw-sessions ───────────────────────────────────────────────────────────────
aw-sessions() {
    local dir="$HOME/.agentwatch/sessions"
    echo "[AgentWatch] Active terminal sessions:"
    echo "──────────────────────────────────────────"
    if [[ -z "$(ls "$dir" 2>/dev/null)" ]]; then echo "  (no sessions found)"; return; fi
    $AW_PYTHON - "$dir" "$_AW_SID" 2>/dev/null <<'PYEOF'
import json, os, sys, time
d, my_sid = sys.argv[1], sys.argv[2]
for f in sorted(os.listdir(d)):
    path = os.path.join(d, f)
    try:
        with open(path) as fh: info = json.load(fh)
        age  = int(time.time() - info.get("created", 0))
        name = info.get("name") or "(unnamed)"
        tty  = info.get("tty") or "?"
        tp   = info.get("term_program") or "?"
        me   = " ← YOU" if f == my_sid else ""
        print(f"  [{f}]  {name}  |  {tty}  |  {tp}  |  age: {age}s{me}")
    except Exception:
        print(f"  [{f}]  (unreadable)")
PYEOF
}

# ── aw: explicit wrap for non-agent commands ──────────────────────────────────
aw() {
    local start_ms end_ms dur_ms dur_s exit_code cmd="$*"
    local term_prog="${TERM_PROGRAM:-}" sess_id="${TERM_SESSION_ID:-}"
    local tty_dev="${TTY:-$(tty 2>/dev/null)}"
    start_ms=$(_aw_ms)
    "$@"; exit_code=$?
    end_ms=$(_aw_ms); dur_ms=$(( end_ms - start_ms )); dur_s=$(( dur_ms / 1000 ))
    local short_cmd; short_cmd=$(_aw_truncate "$cmd")
    local cwd_label; cwd_label=$(basename "$PWD" 2>/dev/null || echo "shell")
    local site_name
    [[ -n "$AW_TERMINAL_NAME" ]] && site_name="${AW_TERMINAL_NAME} · ${cwd_label}" || {
        case "$term_prog" in
            vscode) site_name="VSCode · $cwd_label" ;;
            Apple_Terminal) site_name="Terminal · $cwd_label" ;;
            iTerm.app) site_name="iTerm · $cwd_label" ;;
            *) site_name="Terminal · $cwd_label" ;;
        esac
    }
    local dur_label; (( dur_s >= 60 )) && dur_label="$((dur_s/60))m $((dur_s%60))s" || dur_label="${dur_s}s"
    local ev_type title
    [[ $exit_code -eq 0 ]] && { ev_type="COMPLETED"; title="Done (${dur_label})"; } \
                            || { ev_type="ERROR"; title="Failed [exit $exit_code] (${dur_label})"; }
    _aw_show_card "$title" "$site_name" "$ev_type" "$short_cmd" "$term_prog" "$sess_id" "$tty_dev" "$_AW_SID"
    return $exit_code
}

# ── aw-agent: explicit agent wrap ────────────────────────────────────────────
aw-agent() {
    if [[ $# -eq 0 ]]; then
        echo "Usage: aw-agent <command> [args…]"
        echo "Example: aw-agent claude"
        echo "         aw-agent gemini 'hello world'"
        echo "         aw-agent ollama run llama3"
        return 1
    fi
    local agent_name; agent_name=$(_aw_detect_agent "$*")
    [[ -z "$agent_name" ]] && agent_name="${1##*/}"
    _aw_run_with_pty "$agent_name" "$@"
}

# ── aw-test ───────────────────────────────────────────────────────────────────
aw-test() {
    echo "[AgentWatch] Firing test card (session: $_AW_SID)..."
    if [[ -z "$_AW_NOTIFY" || ! -f "$_AW_NOTIFY" ]]; then
        echo "[AgentWatch] ERROR: notify.py not found! Run install.sh first."; return 1
    fi
    _aw_show_card \
        "Test card" \
        "${AW_TERMINAL_NAME:-Terminal} · test" \
        "COMPLETED" \
        "Notification working! Session: $_AW_SID\n> TTY: ${TTY:-unknown}\n> Click Reply to test inline reply"
    echo "[AgentWatch] Card launched (should appear top-right)"
    echo ""
    echo "  pty_wrapper: ${_AW_PTY_WRAPPER:-(NOT FOUND)}"
    echo "  Claude CLI test: aw-agent claude"
}

aw-test-reply() {
    echo "[AgentWatch] Firing test card with Reply..."
    _aw_show_card \
        "Reply test" "${AW_TERMINAL_NAME:-Terminal} · test" "DECISION" \
        "Test reply card. Session: $_AW_SID\n> Type something and click Reply\n> It should paste back here"
    echo "[AgentWatch] Card launched — click Reply, type text, press ⌘↵"
}

# ── aw-diagnose ───────────────────────────────────────────────────────────────
aw-diagnose() {
    echo "▶ AgentWatch diagnose v${AW_VERSION}"
    echo "─────────────────────"
    echo "• Loaded version : v${AW_VERSION:-?}"
    echo "• Source file    : $_AW_SELF"
    echo "• Session ID     : $_AW_SID"
    echo "• Terminal name  : ${AW_TERMINAL_NAME:-(unset)}"
    echo "• TTY            : ${TTY:-$(tty 2>/dev/null)}"
    if [[ -f "$HOME/.agentwatch/agentwatch.zsh" ]]; then
        local iv; iv=$(grep -m1 '^AW_VERSION=' "$HOME/.agentwatch/agentwatch.zsh" | cut -d'"' -f2)
        echo "• Installed ver  : v${iv:-<unset>}"
        [[ "$iv" != "$AW_VERSION" ]] && echo "  ⚠️  MISMATCH: open a new terminal"
    fi
    echo ""
    echo "▶ Files"
    echo "  notify.py    : ${_AW_NOTIFY:-(NOT FOUND)}"
    echo "  pty_wrapper  : ${_AW_PTY_WRAPPER:-(NOT FOUND)}"
    echo ""
    echo "▶ Agent detection test"
    for _tc in "claude" "claude-code" "ollama run llama3" "gemini hello" "npx @anthropic-ai/claude-code" "aider"; do
        local _k; _k=$(_aw_detect_agent "$_tc")
        [[ -n "$_k" ]] && echo "  ✓  '$_tc' → agent: $_k" || echo "  –  '$_tc' → (not agent)"
    done
    echo ""
    echo "▶ Recent notify.log (last 15 lines)"
    if [[ -f "$HOME/.agentwatch/notify.log" ]]; then
        tail -n 15 "$HOME/.agentwatch/notify.log" | sed 's/^/  /'
    else
        echo "  (no log yet)"
    fi
}