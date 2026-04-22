/**
 * AgentWatch — popup.js (v1.4)
 *
 * v1.4 fixes:
 *  - Reply button visible on ALL event types except ERROR / RATE_LIMITED.
 *  - heuristicNeedsReply() mirrors background.js to detect questions in snippet.
 *  - Prominent (amber) reply button when action needed, ghost when optional.
 *  - Double-badge fix: only show category badge when it adds new information.
 *  - Event delegation handles all interactions; no more listener races.
 *  - jumpToTab() closes popup after focusing tab (window.close()).
 */

// ─── Constants ────────────────────────────────────────────────────────────────
const SITES = [
  { id: 'chatgpt', name: 'ChatGPT', domain: 'chat.openai.com' },
  { id: 'claude', name: 'Claude', domain: 'claude.ai' },
  { id: 'gemini', name: 'Gemini', domain: 'gemini.google.com' },
  { id: 'perplexity', name: 'Perplexity', domain: 'perplexity.ai' },
  { id: 'copilot', name: 'Copilot', domain: 'copilot.microsoft.com' },
  { id: 'grok', name: 'Grok', domain: 'grok.com' },
  { id: 'meta', name: 'Meta AI', domain: 'meta.ai' },
  { id: 'poe', name: 'Poe', domain: 'poe.com' },
  { id: 'phind', name: 'Phind', domain: 'phind.com' },
  { id: 'you', name: 'You.com', domain: 'you.com' },
  { id: 'hf', name: 'HuggingFace Chat', domain: 'huggingface.co' },
  { id: 'mistral', name: 'Mistral', domain: 'chat.mistral.ai' },
  { id: 'groq', name: 'Groq', domain: 'groq.com' },
  { id: 'deepseek', name: 'DeepSeek', domain: 'chat.deepseek.com' },
  { id: 'pi', name: 'Pi AI', domain: 'pi.ai' },
  { id: 'character', name: 'Character.ai', domain: 'character.ai' },
  { id: 'cohere', name: 'Cohere', domain: 'coral.cohere.com' },
  { id: 'bing', name: 'Bing Copilot', domain: 'bing.com' },
  { id: 'cli', name: 'Terminal', domain: 'terminal://local', readOnly: true },
];

// Event types where replying makes zero sense — no reply button at all
const NEVER_REPLY_TYPES = new Set(['ERROR', 'RATE_LIMITED']);

// Event types where reply is prominently highlighted by default
const PROMINENT_REPLY_TYPES = new Set(['DECISION', 'BLOCKED', 'PERMISSION']);

// ─── State ────────────────────────────────────────────────────────────────────
let currentTab = 'monitor';
let timerHandle = null;
let isReplyMode = false;

// ─── Heuristic (mirrors background.js exactly) ────────────────────────────────
function heuristicNeedsReply(snippet) {
  if (!snippet || typeof snippet !== 'string') return false;
  const s = snippet.toLowerCase().trim();

  const tail = s.slice(-300);
  if (tail.includes('?')) return true;

  const phrases = [
    'would you like',
    'do you want',
    'shall i',
    'should i',
    'let me know',
    'which option',
    'what would you',
    'how would you',
    'please clarify',
    'can you confirm',
    'could you clarify',
    'please let me know',
    'which would you prefer',
    'what do you think',
    'do you need',
    'would you prefer',
    'is that correct',
    'does that work',
    'does this help',
    'anything else',
    'feel free to ask',
    'happy to help',
    'let me know if',
    'please specify',
    'which one',
    'option 1',
    'option 2',
    'option a',
    'option b',
  ];

  return phrases.some(p => s.includes(p));
}

// ─── Determine reply prominence for a history event ───────────────────────────
// Returns: 'never' | 'prominent' | 'ghost'
function replyLevel(ev) {
  if (NEVER_REPLY_TYPES.has(ev.eventType)) return 'never';

  // Explicit flag from background.js (most reliable)
  if (ev.needsReply === true) return 'prominent';

  // Traditional action types
  if (PROMINENT_REPLY_TYPES.has(ev.eventType)) return 'prominent';

  // Smart classification
  if (ev.category === 'ACTION_REQUIRED') return 'prominent';

  // Heuristic on response text
  if (heuristicNeedsReply(ev.messageSnippet)) return 'prominent';

  // All other types (COMPLETED, INFORMATION, etc.) get a ghost reply option
  return 'ghost';
}

// ─── Init ─────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  // ── Floating reply-mode detection ───────────────────────────────────────
  const urlParams = new URLSearchParams(window.location.search);
  isReplyMode = urlParams.get('mode') === 'reply';

  if (isReplyMode) {
    // Hide full UI chrome — show only the reply panel
    const tabsEl = document.querySelector('.tabs');
    const headerEl = document.querySelector('.header');
    const footerEl = document.querySelector('.footer');
    if (tabsEl) tabsEl.style.display = 'none';
    if (headerEl) headerEl.style.display = 'none';
    if (footerEl) footerEl.style.display = 'none';
    // Compact floating appearance
    document.body.style.borderRadius = '12px';
    document.body.style.overflow = 'hidden';
    // Force monitor tab (contains reply panel)
    currentTab = 'monitor';
    await renderTab('monitor');
    setTimeout(() => document.getElementById('reply-input')?.focus(), 100);
    // Still set up delegation so Send/Dismiss work
    initContentDelegation();
    return; // skip full init — no tabs, no footer, no timers needed
  }

  await initGlobalToggle();
  await renderTab(currentTab);
  startLiveTimers();
  initContentDelegation();

  document.querySelectorAll('.tab').forEach(btn => {
    btn.addEventListener('click', async () => {
      currentTab = btn.dataset.tab;
      document.querySelectorAll('.tab').forEach(t => {
        t.classList.remove('active');
        t.setAttribute('aria-selected', 'false');
      });
      btn.classList.add('active');
      btn.setAttribute('aria-selected', 'true');
      await renderTab(currentTab);
    });
  });

  document.getElementById('global-toggle').addEventListener('change', async (e) => {
    const s = await getSettings();
    s.globalDisabled = !e.target.checked;
    await chrome.storage.local.set({ settings: s });
    document.body.classList.toggle('disabled', s.globalDisabled);
    updateFooter();
  });

  chrome.storage.onChanged.addListener(async (changes, area) => {
    if (area !== 'local') return;
    if (changes.activeSessions || changes.history || changes.pendingReply) {
      await renderTab(currentTab);
    }
    if (changes.settings) updateFooter();
  });

  updateFooter();
});

// ─── Event Delegation on #content ─────────────────────────────────────────────
function initContentDelegation() {
  const root = document.getElementById('content');
  if (!root) return;

  root.addEventListener('click', async (e) => {
    const btn = e.target.closest('button');

    // ── Reply Send ──────────────────────────────────────────────────────────
    if (btn?.id === 'reply-send') {
      const text = document.getElementById('reply-input')?.value?.trim();
      if (!text) return;
      const tabId = parseInt(btn.dataset.tabId);
      const windowId = parseInt(btn.dataset.windowId) || undefined;
      await chrome.runtime.sendMessage({ type: 'SEND_REPLY', tabId, windowId, text });
      // In floating reply mode, close the window after sending
      if (isReplyMode) {
        try { chrome.windows.getCurrent(w => chrome.windows.remove(w.id)); } catch { /* noop */ }
        return;
      }
      await renderTab(currentTab);
      return;
    }

    // ── Reply Dismiss ───────────────────────────────────────────────────────
    if (btn?.id === 'reply-dismiss') {
      await chrome.storage.local.remove(['pendingReply']);
      // In floating reply mode, close the window on dismiss too
      if (isReplyMode) {
        try { chrome.windows.getCurrent(w => chrome.windows.remove(w.id)); } catch { /* noop */ }
        return;
      }
      await renderTab(currentTab);
      return;
    }

    // ── Details Panel Toggle ────────────────────────────────────────────────
    if (btn?.classList.contains('details-btn')) {
      e.stopPropagation();
      const id     = btn.dataset.id;
      const panel  = document.getElementById(`detail-${id}`);
      const svg    = btn.querySelector('svg polyline');
      if (!panel) return;
      const isOpen = panel.style.display !== 'none';
      panel.style.display = isOpen ? 'none' : 'block';
      btn.classList.toggle('active', !isOpen);
      if (svg) svg.setAttribute('points', 
        isOpen ? '6 9 12 15 18 9' : '18 15 12 9 6 15');
      return;
    }

    // ── Show / Jump-to-tab ──────────────────────────────────────────────────
    if (btn?.classList.contains('show-btn')) {
      e.stopPropagation();
      await jumpToTab(
        parseInt(btn.dataset.tabId),
        parseInt(btn.dataset.windowId),
        btn.dataset.url,
      );
      return;
    }

    // ── Reply button on history / monitor cards ─────────────────────────────
    if (btn?.classList.contains('reply-btn')) {
      e.stopPropagation();
      const pendingReply = {
        tabId: parseInt(btn.dataset.tabId) || null,
        windowId: parseInt(btn.dataset.windowId) || null,
        site: btn.dataset.site,
        siteName: btn.dataset.siteName,
        eventType: btn.dataset.eventType,
        category: btn.dataset.category || null,
        messageSnippet: btn.dataset.snippet || '',
        timestamp: new Date().toISOString(),
      };
      await chrome.storage.local.set({ pendingReply });
      currentTab = 'monitor';
      document.querySelectorAll('.tab').forEach(t => {
        t.classList.toggle('active', t.dataset.tab === 'monitor');
        t.setAttribute('aria-selected', t.dataset.tab === 'monitor' ? 'true' : 'false');
      });
      await renderTab(currentTab);
      setTimeout(() => document.getElementById('reply-input')?.focus(), 50);
      return;
    }
    if (btn?.classList.contains('stop-monitoring-btn')) {
      e.stopPropagation();
      const sessionId  = btn.dataset.sessionId;
      const siteName   = btn.dataset.siteName;
 
      const confirmed = await confirmModal({
        title: `Stop monitoring "${siteName}"?`,
        message: 'This will send a stop signal to the PTY session. '
               + 'Use this if the session is stuck or notifications are misfiring.',
        confirmText: 'Stop Session',
        danger: true,
      });
 
      if (!confirmed) return;
 
      // Disable button immediately for visual feedback
      btn.disabled = true;
      btn.textContent = 'Stopping…';
 
      try {
        await chrome.runtime.sendMessage({
          type: 'STOP_CLI_SESSION',
          sessionId,
          siteName,
        });
      } catch {}
 
      // Remove the card from history display after a beat
      setTimeout(() => renderTab(currentTab), 600);
      return;
    }


    // ── Card-level click — jump to tab ──────────────────────────────────────
    if (!btn) {
      const card = e.target.closest('.session-card, .event-card');
      if (!card) return;
      await jumpToTab(
        parseInt(card.dataset.tabId),
        parseInt(card.dataset.windowId),
        card.dataset.url,
      );
    }
  });

  // Ctrl/Cmd + Enter to send reply
  root.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      document.getElementById('reply-send')?.click();
    }
  });
}

// ─── Settings Init ────────────────────────────────────────────────────────────
async function initGlobalToggle() {
  const s = await getSettings();
  document.getElementById('global-toggle').checked = !s.globalDisabled;
  document.body.classList.toggle('disabled', !!s.globalDisabled);
}

// ─── Tab Renderer ─────────────────────────────────────────────────────────────
async function renderTab(tab) {
  const content = document.getElementById('content');
  if (!content) return;
  if (tab === 'monitor') await renderMonitor(content);
  else if (tab === 'history') await renderHistory(content);
  else if (tab === 'settings') await renderSettings(content);
}

// ─── Monitor Tab ──────────────────────────────────────────────────────────────
async function renderMonitor(el) {
  const { activeSessions = {}, pendingReply } = await chrome.storage.local.get(['activeSessions', 'pendingReply']);
  const sessions = Object.values(activeSessions);
  const { settings } = await chrome.storage.local.get(['settings']);
  const disabled = settings?.globalDisabled;

  let html = '';

  // Reply panel
  const replyAge = pendingReply
    ? Date.now() - new Date(pendingReply.timestamp).getTime()
    : Infinity;

  if (pendingReply && replyAge < 30 * 60 * 1000) {
    html += `
      <div class="reply-panel" data-testid="reply-panel">
        <div class="reply-header">
          <span class="badge badge-${esc(pendingReply.eventType)}">${esc(pendingReply.eventType)}</span>
          <span class="reply-site">${esc(pendingReply.siteName)}</span>
          <span class="reply-label">is waiting for your reply</span>
        </div>
        ${pendingReply.messageSnippet
        ? `<div class="reply-snippet" data-testid="reply-snippet">${esc(pendingReply.messageSnippet)}</div>`
        : ''}
        <textarea id="reply-input" class="reply-input" data-testid="reply-input"
          placeholder="Type your reply here…" rows="3"></textarea>
        <div class="reply-actions">
          <button class="btn btn-ghost" id="reply-dismiss" data-testid="reply-dismiss">Dismiss</button>
          <button class="btn btn-primary" id="reply-send"
            data-tab-id="${pendingReply.tabId}"
            data-window-id="${pendingReply.windowId || ''}"
            data-testid="reply-send">
            Send Reply
          </button>
        </div>
        <div class="reply-hint"><kbd>Ctrl</kbd>/<kbd>⌘</kbd> + <kbd>Enter</kbd> to send</div>
      </div>`;
  }

  if (disabled) {
    html += `
      <div class="disabled-notice" data-testid="disabled-notice">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/>
          <line x1="12" y1="16" x2="12.01" y2="16"/>
        </svg>
        Notifications are disabled. Toggle ON above to enable.
      </div>`;
  }

  if (sessions.length === 0) {
    html += `
      <div class="empty-state" data-testid="monitor-empty">
        <div class="empty-icon">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#6b7592" stroke-width="1.5" stroke-linecap="round">
            <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>
          </svg>
        </div>
        <div class="empty-title">All agents idle</div>
        <div class="empty-desc">Open an AI tool and start a conversation — AgentWatch will notify you the moment it's done.</div>
      </div>`;
  } else {
    html += `<div class="section-label" data-testid="active-count">Active · ${sessions.length}</div>`;
    for (const s of sessions) {
      html += `
        <div class="session-card"
          data-tab-id="${s.tabId}"
          data-window-id="${s.windowId || ''}"
          data-url="${esc(s.url || '')}"
          data-testid="session-card-${esc(s.site)}">
          <div class="session-row">
            <div class="session-site">
              <div class="pulse-dot"></div>
              ${esc(s.siteName)}
            </div>
            <div class="session-timer"
              data-start="${s.startTime}"
              data-testid="session-timer-${esc(s.site)}">
              ${fmtTimer(Date.now() - s.startTime)}
            </div>
          </div>
          <div class="session-title">${esc(shortenTitle(s.title || s.url))}</div>
          <div class="card-actions">
            <button class="btn btn-secondary btn-sm show-btn"
              data-tab-id="${s.tabId}"
              data-window-id="${s.windowId || ''}"
              data-url="${esc(s.url || '')}"
              data-testid="show-btn-${esc(s.site)}">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>
              </svg>
              Show
            </button>
            <button class="btn btn-ghost btn-sm reply-btn"
              data-tab-id="${s.tabId}"
              data-window-id="${s.windowId || ''}"
              data-site="${esc(s.site)}"
              data-site-name="${esc(s.siteName)}"
              data-event-type="COMPLETED"
              data-snippet=""
              data-testid="monitor-reply-${esc(s.site)}">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <polyline points="9 17 4 12 9 7"/><path d="M20 18v-2a4 4 0 0 0-4-4H4"/>
              </svg>
              Reply
            </button>
          </div>
        </div>`;
    }
  }

  el.innerHTML = html;
}

// ─── History Tab ──────────────────────────────────────────────────────────────
async function renderHistory(el) {
  const { history = [] } = await chrome.storage.local.get(['history']);

  if (history.length === 0) {
    el.innerHTML = `
      <div class="empty-state" data-testid="history-empty">
        <div class="empty-icon">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#6b7592" stroke-width="1.5" stroke-linecap="round">
            <circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>
          </svg>
        </div>
        <div class="empty-title">No events yet</div>
        <div class="empty-desc">Events will appear here once your AI agents start generating responses.</div>
      </div>`;
    return;
  }

  const todayCount = history.filter(e => isToday(e.timestamp)).length;
  let html = `<div class="section-label">Today · ${todayCount} event${todayCount !== 1 ? 's' : ''}</div>`;

  for (const ev of history.slice(0, 30)) {
    const level = replyLevel(ev);

    // ── Badge logic: only show category if it adds new info ──────────────────
    const showCategory = ev.category
      && ev.category !== ev.eventType
      && ev.classificationSource
      && !ev.classificationSource.startsWith('fallback')
      && !ev.classificationSource.startsWith('heuristic');

    // ── Reply button HTML ────────────────────────────────────────────────────
    let replyBtnHtml = '';
    if (level !== 'never') {
      const isCLI = ev.isCLI || ev.site === 'cli';
      const btnClass = level === 'prominent' ? 'btn-warn' : 'btn-ghost';
      const btnLabel = isCLI ? '📋 Copy Reply' : 'Reply';
      const btnTabId = isCLI ? '' : ev.tabId;
      const btnWindowId = isCLI ? '' : (ev.windowId || '');
      replyBtnHtml = `
        <button class="btn ${btnClass} btn-sm reply-btn"
          data-tab-id="${btnTabId}"
          data-window-id="${btnWindowId}"
          data-site="${esc(ev.site)}"
          data-site-name="${esc(ev.siteName || ev.site)}"
          data-event-type="${esc(ev.eventType)}"
          data-category="${esc(ev.category || '')}"
          data-snippet="${esc((ev.messageSnippet || '').slice(0, 280))}"
          data-testid="event-reply-${esc(ev.id)}">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="9 17 4 12 9 7"/><path d="M20 18v-2a4 4 0 0 0-4-4H4"/>
          </svg>
          ${btnLabel}
        </button>`;
    }
    let stopBtnHtml = '';
    const isCLIEvent = ev.isCLI || ev.site === 'cli';
    if (isCLIEvent) {
      stopBtnHtml = `
        <button class="btn btn-stop btn-sm stop-monitoring-btn"
          data-session-id="${esc(ev.id)}"
          data-site-name="${esc(ev.siteName || 'Terminal')}"
          data-testid="stop-monitoring-${esc(ev.id)}"
          title="Force-stop the PTY session if it's stuck">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor">
            <rect x="3" y="3" width="18" height="18" rx="2" ry="2"/>
          </svg>
          Stop
        </button>`;
    }

    let siteIcon = '';
    if (ev.site === 'cli') {
      siteIcon = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="margin-right: 6px; vertical-align: middle; position: relative; top: -1px;"><polyline points="4 17 10 11 4 5"></polyline><line x1="12" y1="19" x2="20" y2="19"></line></svg>`;
    } else {
      const domain = SITES.find(s => s.id === ev.site)?.domain;
      if (domain) {
        siteIcon = `<img src="https://www.google.com/s2/favicons?domain=${domain}&sz=16" width="14" height="14" style="margin-right: 6px; vertical-align: middle; border-radius: 2px;" onerror="this.style.display='none'">`;
      }
    }

    html += `
      <div class="event-card ${esc(ev.category || ev.eventType)}"
        data-tab-id="${ev.tabId}"
        data-window-id="${ev.windowId || ''}"
        data-url="${esc(ev.url || '')}"
        data-testid="event-card-${esc(ev.id)}">
        <div class="event-row">
          <div class="event-site" style="display: flex; align-items: center;">${siteIcon}${esc(ev.siteName || ev.site)}</div>
          <div class="event-time" data-testid="event-time">${timeAgo(ev.timestamp)}</div>
        </div>
        <div class="event-meta">
          ${showCategory
        ? `<span class="badge badge-${esc(ev.category)}" data-testid="event-category">
                ${prettyCategory(ev.category)}
               </span>`
        : ''}
          <span class="badge badge-${esc(ev.eventType)}" data-testid="event-badge">
            ${esc(ev.eventType)}
          </span>
          ${ev.responseLength > 0 ? `<span class="event-chars">${fmtBytes(ev.responseLength)}</span>` : ''}
          ${ev.durationMs > 0 ? `<span class="event-duration">${fmtDuration(ev.durationMs)}</span>` : ''}
        </div>
        ${ev.messageSnippet
        ? `<div class="event-snippet" data-testid="event-snippet-${esc(ev.id)}">${esc(ev.messageSnippet)}</div>`
        : ''}
        div class="card-actions">
          <button class="btn btn-ghost btn-sm details-btn"
            data-id="\${esc(ev.id)}"
            data-testid="event-details-\${esc(ev.id)}">
            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <polyline points="6 9 12 15 18 9"/>
            </svg>
            Details
          </button>
          <button class="btn btn-secondary btn-sm show-btn"
            data-tab-id="\${ev.tabId}"
            data-window-id="\${ev.windowId || ''}"
            data-url="\${esc(ev.url || '')}"
            data-testid="event-show-\${esc(ev.id)}">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>
            </svg>
            Show
          </button>
          \${replyBtnHtml}
          \${stopBtnHtml}
        </div>
      </div>
      <div class="event-detail-panel" id="detail-${esc(ev.id)}" 
           style="display:none"
           data-testid="event-detail-${esc(ev.id)}">
        <div class="detail-row">
          <span class="detail-key">Time</span>
          <span class="detail-val">${new Date(ev.timestamp || 0).toLocaleString()}</span>
        </div>
        ${ev.url ? `
        <div class="detail-row">
          <span class="detail-key">URL</span>
          <span class="detail-val detail-url">${esc(ev.url)}</span>
        </div>` : ''}
        ${ev.messageSnippet ? `
        <div class="detail-row">
          <span class="detail-key">Response</span>
          <span class="detail-val detail-snippet">${esc(ev.messageSnippet)}</span>
        </div>` : ''}
        ${ev.classificationSource ? `
        <div class="detail-row">
          <span class="detail-key">Classified by</span>
          <span class="detail-val">${esc(ev.classificationSource)}</span>
        </div>` : ''}
        ${ev.responseLength ? `
        <div class="detail-row">
          <span class="detail-key">Size</span>
          <span class="detail-val">${fmtBytes(ev.responseLength)}</span>
        </div>` : ''}
      </div>`;
  }

  el.innerHTML = html;
}

// ─── Settings Tab ─────────────────────────────────────────────────────────────
async function renderSettings(el) {
  const s = await getSettings();

  let html = `
    <div class="section-label">Notifications</div>
    <div class="settings-row" data-testid="global-notifications-row">
      <div class="settings-label">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/>
          <path d="M13.73 21a2 2 0 0 1-3.46 0"/>
        </svg>
        Desktop Notifications
      </div>
      <label class="toggle">
        <input type="checkbox" class="site-toggle" data-site="__global__"
          data-testid="settings-global-toggle" ${!s.globalDisabled ? 'checked' : ''}>
        <span class="toggle-track"></span>
      </label>
    </div>
    <div class="settings-row" data-testid="sound-row">
      <div class="settings-label">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>
          <path d="M15.54 8.46a5 5 0 0 1 0 7.07"/>
          <path d="M19.07 4.93a10 10 0 0 1 0 14.14"/>
        </svg>
        Notification Sound
      </div>
      <label class="toggle">
        <input type="checkbox" id="sound-toggle"
          data-testid="sound-toggle" ${!s.soundDisabled ? 'checked' : ''}>
        <span class="toggle-track"></span>
      </label>
    </div>

    <div class="section-label" style="margin-top:16px;">Monitored Sites</div>`;

  for (const site of SITES) {
    const enabled = !s.sites?.[site.id]?.disabled;
    
    let toggleHtml;
    if (site.readOnly) {
      toggleHtml = `<span class="settings-readonly">Tracked via Mac App</span>`;
    } else {
      toggleHtml = `
        <label class="toggle">
          <input type="checkbox" class="site-toggle"
            data-site="${site.id}"
            data-testid="site-toggle-${site.id}"
            ${enabled ? 'checked' : ''}>
          <span class="toggle-track"></span>
        </label>`;
    }

    let iconHtml;
    if (site.id === 'cli') {
      iconHtml = `<svg class="site-favicon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="padding: 2px;"><polyline points="4 17 10 11 4 5"></polyline><line x1="12" y1="19" x2="20" y2="19"></line></svg>`;
    } else {
      iconHtml = `<img class="site-favicon"
            src="https://www.google.com/s2/favicons?domain=${site.domain}&sz=32"
            alt="${esc(site.name)}"
            onerror="this.style.visibility='hidden'">`;
    }

    html += `
      <div class="settings-row" data-testid="site-row-${site.id}">
        <div class="settings-label">
          ${iconHtml}
          <span>${esc(site.name)}</span>
        </div>
        ${toggleHtml}
      </div>`;
  }

  html += `
    <div class="divider"></div>
    <button class="btn btn-primary btn-block" id="test-notif-btn" data-testid="test-notification-btn">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/>
        <path d="M13.73 21a2 2 0 0 1-3.46 0"/>
      </svg>
      Send Test Notification
    </button>
    <div style="margin-top:8px;">
      <button class="btn btn-warn btn-block" id="test-reply-btn" data-testid="test-reply-btn">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <polyline points="9 17 4 12 9 7"/><path d="M20 18v-2a4 4 0 0 0-4-4H4"/>
        </svg>
        Test Reply Panel
      </button>
    </div>
    <div style="margin-top:8px;">
      <button class="btn btn-danger btn-block" id="clear-history-btn" data-testid="clear-history-btn">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <polyline points="3 6 5 6 21 6"/>
          <path d="M19 6l-1 14H6L5 6"/>
          <path d="M10 11v6"/><path d="M14 11v6"/>
          <path d="M9 6V4h6v2"/>
        </svg>
        Clear All History
      </button>
    </div>`;

  el.innerHTML = html;

  // Site / global toggles
  el.querySelectorAll('.site-toggle').forEach(toggle => {
    toggle.addEventListener('change', async (e) => {
      const siteId = e.target.dataset.site;
      const checked = e.target.checked;
      const settings = await getSettings();
      if (siteId === '__global__') {
        settings.globalDisabled = !checked;
        document.getElementById('global-toggle').checked = checked;
        document.body.classList.toggle('disabled', !checked);
      } else {
        if (!settings.sites) settings.sites = {};
        if (!settings.sites[siteId]) settings.sites[siteId] = {};
        settings.sites[siteId].disabled = !checked;
      }
      await chrome.storage.local.set({ settings });
      updateFooter();
    });
  });

  // Sound toggle
  document.getElementById('sound-toggle')?.addEventListener('change', async (e) => {
    const settings = await getSettings();
    settings.soundDisabled = !e.target.checked;
    await chrome.storage.local.set({ settings });
    if (!settings.soundDisabled) {
      chrome.runtime.sendMessage({ type: 'PLAY_CHIME_PREVIEW' });
    }
  });

  // Test notification
  document.getElementById('test-notif-btn')?.addEventListener('click', () => {
    chrome.runtime.sendMessage({ type: 'TEST_NOTIFICATION' });
  });

  // Test reply panel — inject synthetic pendingReply
  document.getElementById('test-reply-btn')?.addEventListener('click', async () => {
    await chrome.storage.local.set({
      pendingReply: {
        tabId: null,
        windowId: null,
        site: 'test',
        siteName: 'Test Agent',
        eventType: 'DECISION',
        category: 'ACTION_REQUIRED',
        messageSnippet: 'This is a test reply panel. Would you like to continue with option A or option B?',
        timestamp: new Date().toISOString(),
      },
    });
    currentTab = 'monitor';
    document.querySelectorAll('.tab').forEach(t => {
      t.classList.toggle('active', t.dataset.tab === 'monitor');
      t.setAttribute('aria-selected', t.dataset.tab === 'monitor' ? 'true' : 'false');
    });
    await renderTab(currentTab);
    setTimeout(() => document.getElementById('reply-input')?.focus(), 50);
  });

  // Clear history — custom confirm (native confirm() blocked in MV3)
  document.getElementById('clear-history-btn')?.addEventListener('click', async () => {
    const ok = await confirmModal({
      title: 'Clear all history?',
      message: 'This permanently removes every recorded AgentWatch event.',
      confirmText: 'Clear History',
      danger: true,
    });
    if (ok) {
      await chrome.storage.local.set({ history: [] });
      await renderSettings(el);
      updateFooter();
    }
  });
}

// ─── Custom Confirm Modal ─────────────────────────────────────────────────────
function confirmModal({ title, message, confirmText = 'Confirm', cancelText = 'Cancel', danger = false }) {
  return new Promise((resolve) => {
    const root = document.getElementById('modal-root');
    if (!root) return resolve(false);

    root.innerHTML = `
      <div class="modal" role="dialog" aria-modal="true" data-testid="confirm-modal">
        <div class="modal-header">${esc(title)}</div>
        <div class="modal-body">${esc(message)}</div>
        <div class="modal-footer">
          <button class="btn btn-ghost" id="__cm_cancel">${esc(cancelText)}</button>
          <button class="btn ${danger ? 'btn-danger' : 'btn-primary'}" id="__cm_ok">${esc(confirmText)}</button>
        </div>
      </div>`;
    root.classList.add('open');
    root.setAttribute('aria-hidden', 'false');

    const close = (value) => {
      root.classList.remove('open');
      root.setAttribute('aria-hidden', 'true');
      root.innerHTML = '';
      document.removeEventListener('keydown', onKey);
      resolve(value);
    };

    const onKey = (e) => {
      if (e.key === 'Escape') close(false);
      if (e.key === 'Enter') close(true);
    };
    document.addEventListener('keydown', onKey);

    document.getElementById('__cm_cancel').addEventListener('click', () => close(false));
    document.getElementById('__cm_ok').addEventListener('click', () => close(true));
    root.addEventListener('click', (e) => { if (e.target === root) close(false); }, { once: true });

    setTimeout(() => document.getElementById('__cm_ok')?.focus(), 20);
  });
}

// ─── Live Timers ──────────────────────────────────────────────────────────────
function startLiveTimers() {
  if (timerHandle) clearInterval(timerHandle);
  timerHandle = setInterval(() => {
    if (currentTab !== 'monitor') return;
    document.querySelectorAll('.session-timer[data-start]').forEach(el => {
      el.textContent = fmtTimer(Date.now() - parseInt(el.dataset.start));
    });
  }, 1000);
}

// ─── Footer ───────────────────────────────────────────────────────────────────
async function updateFooter() {
  const { history = [], activeSessions = {}, settings, macAppConnected } =
    await chrome.storage.local.get(['history', 'activeSessions', 'settings', 'macAppConnected']);

  const todayCount = history.filter(e => isToday(e.timestamp)).length;
  const activeCount = Object.keys(activeSessions).length;

  const footerEl = document.getElementById('footer-stat');
  const macDot = document.getElementById('mac-dot');
  const macLabel = document.getElementById('mac-label');

  if (footerEl) {
    if (settings?.globalDisabled) {
      footerEl.textContent = 'Notifications OFF';
      footerEl.style.color = 'var(--amber)';
    } else {
      const parts = [];
      if (activeCount > 0) parts.push(`${activeCount} active`);
      if (todayCount > 0) parts.push(`${todayCount} today`);
      footerEl.textContent = parts.length > 0 ? parts.join(' · ') : 'Watching AI agents';
      footerEl.style.color = '';
    }
  }

  if (macDot && macLabel) {
    if (macAppConnected) {
      macDot.classList.add('connected');
      macLabel.textContent = 'Mac App';
    } else {
      macDot.classList.remove('connected');
      macLabel.textContent = 'Chrome only';
    }
  }
}

// ─── Helpers ──────────────────────────────────────────────────────────────────
async function getSettings() {
  const { settings = {} } = await chrome.storage.local.get(['settings']);
  return settings;
}

async function jumpToTab(tabId, windowId, fallbackUrl) {
  const tid = Number.isFinite(tabId) ? tabId : NaN;
  const wid = Number.isFinite(windowId) ? windowId : NaN;
  try {
    if (Number.isNaN(tid)) throw new Error('invalid tabId');
    await chrome.tabs.update(tid, { active: true });
    if (!Number.isNaN(wid)) await chrome.windows.update(wid, { focused: true });
    window.close?.();
  } catch {
    if (fallbackUrl) {
      chrome.tabs.create({ url: fallbackUrl });
      window.close?.();
    }
  }
}

function fmtTimer(ms) {
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60);
  const h = Math.floor(m / 60);
  if (h > 0) return `${h}:${pad(m % 60)}:${pad(s % 60)}`;
  return `${pad(m)}:${pad(s % 60)}`;
}

function pad(n) { return String(n).padStart(2, '0'); }

function timeAgo(ts) {
  if (!ts) return '';
  const diff = Date.now() - new Date(ts).getTime();
  const s = Math.floor(diff / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return new Date(ts).toLocaleDateString();
}

function fmtBytes(n) {
  if (!n) return '';
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k chars` : `${n} chars`;
}

function fmtDuration(ms) {
  if (!ms || ms < 100) return '';
  const s = Math.round(ms / 1000);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

function isToday(ts) {
  if (!ts) return false;
  return new Date(ts).toDateString() === new Date().toDateString();
}

function shortenTitle(t) {
  if (!t) return 'Untitled';
  return t.length > 50 ? t.slice(0, 50) + '…' : t;
}

function esc(s) {
  if (s === null || s === undefined) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function prettyCategory(cat) {
  const map = {
    ACTION_REQUIRED: 'Action Required',
    INFORMATION: 'Information',
    PENDING: 'Pending',
    COMPLETED: 'Completed',
  };
  return map[cat] || esc(cat || '');
}