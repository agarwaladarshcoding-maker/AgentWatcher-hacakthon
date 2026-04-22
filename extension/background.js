/**
 * AgentWatch - background.js (Service Worker) v3.4
 *
 * v3.4 changes vs v3.3:
 *   FIX-1: FALLBACK_ALARM_MS → 10 minutes (was 4 min; too aggressive)
 *   FIX-2: Fallback events go ONLY to Chrome extension history — NO native OS
 *          notification popup. The alarm fires only if still actually generating
 *          after 10 min, which is a genuine "did we miss the event?" scenario.
 *   FIX-3: ChatGPT/Claude: AGENT_EVENT from content.js now properly cancels the
 *          fallback alarm. Added explicit alarm.clear on any AGENT_EVENT.
 *   FIX-4: onAgentGenerating resets the alarm on each new GENERATING message
 *          (deduplicates rapid re-fires from SPA navigation).
 */
try { importScripts('llm_router.js'); } catch (e) { console.error('[AgentWatch] llm_router load failed:', e); }

const MAX_HISTORY = 100;
const STALE_SESSION_MS = 15 * 60 * 1000;  // 15 min hard limit
const FALLBACK_ALARM_MS = 10 * 60 * 1000; // 10 min — only "are you still there?"
const MAC_APP_WS = 'ws://localhost:59452';

let macAppIsConnected = false;
const NEVER_REPLY_TYPES = new Set(['ERROR', 'RATE_LIMITED']);

// ─── Heuristic ────────────────────────────────────────────────────────────────
function heuristicNeedsReply(snippet) {
  if (!snippet || typeof snippet !== 'string') return false;
  const s = snippet.toLowerCase().trim();
  const tail = s.slice(-300);
  if (tail.includes('?')) return true;
  const phrases = [
    'would you like','do you want','shall i','should i','let me know',
    'which option','what would you','how would you','please clarify',
    'can you confirm','could you clarify','please let me know',
    'which would you prefer','what do you think','do you need',
    'would you prefer','is that correct','does that work','does this help',
    'anything else','feel free to ask','happy to help','let me know if',
    'please specify','which one','option 1','option 2','option a','option b',
  ];
  return phrases.some(p => s.includes(p));
}

// ─── Offscreen Audio ──────────────────────────────────────────────────────────
let creatingOffscreen = null;
async function ensureOffscreen() {
  if (!chrome.offscreen) return false;
  try { const ex = await chrome.offscreen.hasDocument?.(); if (ex) return true; } catch {}
  if (creatingOffscreen) { await creatingOffscreen; return true; }
  creatingOffscreen = chrome.offscreen.createDocument({
    url: 'offscreen.html',
    reasons: ['AUDIO_PLAYBACK','CLIPBOARD'],
    justification: 'Play AgentWatch notification chime and copy to clipboard',
  }).catch(() => {});
  await creatingOffscreen;
  creatingOffscreen = null;
  return true;
}
async function handleStopCLISession(msg) {
  const { sessionId, siteName } = msg;
 
  // 1. Remove from history so the stuck entry is gone
  const { history = [] } = await chrome.storage.local.get(['history']);
  const filtered = history.filter(e => e.id !== sessionId);
  await chrome.storage.local.set({ history: filtered });
 
  // 2. Relay STOP_MONITORING to the mac app / core bus
  sendToMacApp({
    type: 'STOP_MONITORING',
    sessionId,
    siteName,
    timestamp: new Date().toISOString(),
  });
 
  // 3. Show brief confirmation notification
  try {
    await chrome.notifications.create(`aw_stop_${Date.now()}`, {
      type: 'basic',
      iconUrl: 'icons/icon128.png',
      title: 'AgentWatch — session stopped',
      message: `Monitoring stopped for "${siteName || 'Terminal'}".`,
      priority: 0,
    });
  } catch {}
 
  console.log('[AgentWatch] CLI session stopped:', sessionId);
}

async function playChime(volume = 0.7) {
  try {
    const { settings } = await chrome.storage.local.get(['settings']);
    if (settings?.soundDisabled) return;
    await ensureOffscreen();
    chrome.runtime.sendMessage({ target: 'offscreen', type: 'PLAY_CHIME', volume });
  } catch {}
}

// ─── Mac App WebSocket ────────────────────────────────────────────────────────
let macAppWs = null;
let wsReconnectTimer = null;
let wsConnecting = false;

function connectToMacApp() {
  if (macAppWs?.readyState === WebSocket.OPEN || wsConnecting) return;
  wsConnecting = true;
  clearTimeout(wsReconnectTimer);
  try {
    macAppWs = new WebSocket(MAC_APP_WS);
    macAppWs.addEventListener('open', () => {
      wsConnecting = false; macAppIsConnected = true;
      chrome.storage.local.set({ macAppConnected: true });
    });
    macAppWs.addEventListener('message', async (event) => {
      try {
        const msg = JSON.parse(event.data);
        if (msg.type === 'REPLY_INJECT' && msg.tabId)
          await chrome.tabs.sendMessage(msg.tabId, { type: 'INJECT_REPLY', text: msg.text });
        if (msg.type === 'AGENT_EVENT' && msg.relayedFromMacApp)
          await onRelayedEvent(msg);
        if (msg.type === 'FOCUS_TAB' && msg.tabId) {
          chrome.tabs.update(msg.tabId, { active: true }).catch(() => {});
          if (msg.windowId) chrome.windows.update(msg.windowId, { focused: true }).catch(() => {});
        }
      } catch {}
    });
    macAppWs.addEventListener('close', () => {
      wsConnecting = false; macAppWs = null; macAppIsConnected = false;
      chrome.storage.local.set({ macAppConnected: false });
      wsReconnectTimer = setTimeout(connectToMacApp, 5000);
    });
    macAppWs.addEventListener('error', () => { wsConnecting = false; macAppWs?.close(); });
  } catch {
    wsConnecting = false;
    wsReconnectTimer = setTimeout(connectToMacApp, 5000);
  }
}

function sendToMacApp(data) {
  if (macAppWs?.readyState === WebSocket.OPEN) {
    try { macAppWs.send(JSON.stringify(data)); return true; } catch { return false; }
  }
  return false;
}

connectToMacApp();
setInterval(() => {
  if (!macAppWs || macAppWs.readyState !== WebSocket.OPEN) connectToMacApp();
}, 15000);

// ─── Message Router ───────────────────────────────────────────────────────────
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    switch (msg.type) {
      case 'AGENT_GENERATING':     await onAgentGenerating(msg, sender); break;
      case 'AGENT_EVENT':          await onAgentEvent(msg, sender); break;
      case 'AGENT_CONTEXT_SWITCH': await onContextSwitch(msg, sender); break;
      case 'TEST_NOTIFICATION':    await sendTestNotification(); break;
      case 'PLAY_CHIME_PREVIEW':   await playChime(0.6); break;
       case 'STOP_CLI_SESSION':
        await handleStopCLISession(msg);
        break;
      case 'SEND_REPLY':           await handleSendReply(msg); break;
      case 'LLM_PING': {
        const cfg = await getLLMSettings();
        const result = await (globalThis.AgentWatchLLM?.ping?.(msg.endpoint || cfg.endpoint)
          ?? { ok: false, error: 'router-missing' });
        sendResponse(result); return;
      }
      case 'FOCUS_TAB':
        if (msg.tabId) {
          chrome.tabs.update(msg.tabId, { active: true }).catch(() => {});
          if (msg.windowId) chrome.windows.update(msg.windowId, { focused: true }).catch(() => {});
        }
        break;
      case 'GET_MAC_STATUS':
        sendResponse({ connected: macAppWs?.readyState === WebSocket.OPEN }); break;
    }
  })();
  return true;
});

// ─── Fallback alarms ──────────────────────────────────────────────────────────
// Only fires after 10 minutes. Adds to history ONLY — no popup notification.
// This covers the edge case where the AI site's "done" event was missed.
if (chrome.alarms && chrome.alarms.onAlarm) {
  chrome.alarms.onAlarm.addListener(async (alarm) => {
    if (!alarm.name.startsWith('aw_fallback_')) return;
    const tabId = parseInt(alarm.name.replace('aw_fallback_', ''));
    if (!Number.isFinite(tabId)) return;

    const sessions = await getSessions();
    if (!sessions[tabId]) return; // already completed normally

    console.log(`[AgentWatch] 10-min fallback alarm for tabId=${tabId}`);

    try {
      const resp = await chrome.tabs.sendMessage(tabId, { type: 'CHECK_GENERATING' });
      if (resp && resp.generating === false) {
        // Task finished but we missed the event — add to history ONLY, no popup
        await _recordRecoveredInHistory(tabId, sessions[tabId]);
      }
      // If still generating: leave it, won't fire again
    } catch {
      // Tab closed — clean up silently
      delete sessions[tabId];
      await chrome.storage.local.set({ activeSessions: sessions });
      updateBadge(Object.keys(sessions).length);
      if (chrome.alarms) chrome.alarms.clear(`aw_fallback_${tabId}`);
    }
  });
} else {
  console.warn('[AgentWatch] chrome.alarms unavailable — 10-min active re-check disabled.');
}

// History-only recovery — NO notification popup fired
async function _recordRecoveredInHistory(tabId, session) {
  const sessions = await getSessions();
  delete sessions[tabId];
  await chrome.storage.local.set({ activeSessions: sessions });
  updateBadge(Object.keys(sessions).length);

  const event = {
    id: `recovered_${tabId}_${Date.now()}`,
    tabId,
    windowId: session?.windowId,
    eventType: 'COMPLETED',
    category: 'COMPLETED',
    classificationSource: 'fallback-10min',
    needsReply: false,
    site: session?.site || 'unknown',
    siteName: session?.siteName || 'AI',
    url: session?.url || '',
    title: session?.title || '',
    responseLength: 0,
    durationMs: Date.now() - (session?.startTime || Date.now()),
    messageSnippet: '(auto-recovered after 10 min — completion event was missed)',
    timestamp: new Date().toISOString(),
    isRecovered: true,
  };

  const { history = [] } = await chrome.storage.local.get(['history']);
  history.unshift(event);
  if (history.length > MAX_HISTORY) history.length = MAX_HISTORY;
  await chrome.storage.local.set({ history });
  // ↑ No fireNotification() call here — history only
  console.log('[AgentWatch] Recovery recorded in history (no popup):', event.siteName);
}

// ─── LLM Settings ─────────────────────────────────────────────────────────────
async function getLLMSettings() {
  const { settings = {} } = await chrome.storage.local.get(['settings']);
  const defaults = globalThis.AgentWatchLLM?.getDefaults?.() || {
    enabled: false, endpoint: 'http://localhost:11434', model: 'llama3.2:1b', timeoutMs: 1500,
  };
  return { ...defaults, ...(settings.llm || {}) };
}

// ─── Context Switch ────────────────────────────────────────────────────────────
async function onContextSwitch(msg, sender) {
  const tabId = sender.tab?.id;
  if (!tabId) return;
  const sessions = await getSessions();
  if (sessions[tabId]) {
    const age = Date.now() - (sessions[tabId].startTime || 0);
    if (age > 30_000) {
      delete sessions[tabId];
      await chrome.storage.local.set({ activeSessions: sessions });
      updateBadge(Object.keys(sessions).length);
    }
  }
  sendToMacApp({ type: 'AGENT_CONTEXT_SWITCH', ...msg });
}

// ─── Session Tracking ─────────────────────────────────────────────────────────
async function onAgentGenerating(msg, sender) {
  const sessions = await getSessions();
  const tabId = sender.tab?.id;
  if (!tabId) return;

  const isNew = !sessions[tabId];
  sessions[tabId] = {
    tabId, windowId: sender.tab?.windowId,
    site: msg.site, siteName: msg.siteName,
    url: msg.url, title: msg.title || msg.url,
    startTime: isNew ? Date.now() : sessions[tabId].startTime,
    updatedAt: Date.now(),
  };

  await chrome.storage.local.set({ activeSessions: sessions });
  updateBadge(Object.keys(sessions).length);
  sendToMacApp({ type: 'AGENT_GENERATING', site: msg.site, siteName: msg.siteName, tabId });

  // Only set alarm if this is a genuinely new generation
  if (chrome.alarms && isNew) {
    chrome.alarms.clear(`aw_fallback_${tabId}`);
    chrome.alarms.create(`aw_fallback_${tabId}`, {
      delayInMinutes: FALLBACK_ALARM_MS / 60_000,  // 10 minutes
    });
  }
}

// ─── Agent Event ──────────────────────────────────────────────────────────────
async function onAgentEvent(msg, sender) {
  const tabId = sender.tab?.id;

  // FIX-3: Always cancel fallback alarm when we get a real AGENT_EVENT
  if (tabId && chrome.alarms) {
    chrome.alarms.clear(`aw_fallback_${tabId}`);
  }

  const sessions = await getSessions();
  delete sessions[tabId];
  await chrome.storage.local.set({ activeSessions: sessions });
  updateBadge(Object.keys(sessions).length);

  const { settings } = await chrome.storage.local.get(['settings']);
  if (settings?.globalDisabled) return;
  if (settings?.sites?.[msg.site]?.disabled) return;

  const llmCfg = await getLLMSettings();
  const classification = await (
    globalThis.AgentWatchLLM?.classify?.(msg, llmCfg)
    ?? Promise.resolve({ category: msg.eventType, needsReply: false, reason: 'router-missing', source: 'fallback' })
  );

  const isErrorType = NEVER_REPLY_TYPES.has(msg.eventType);
  const needsReply = !isErrorType && (
    !!classification.needsReply
    || ['DECISION','BLOCKED','PERMISSION'].includes(msg.eventType)
    || heuristicNeedsReply(msg.messageText || msg.messageSnippet || '')
  );

  const { history = [] } = await chrome.storage.local.get(['history']);
  const event = {
    id: `${Date.now()}_${tabId}`,
    tabId, windowId: sender.tab?.windowId,
    eventType: msg.eventType,
    category: classification.category,
    classificationReason: classification.reason,
    classificationSource: classification.source,
    needsReply,
    site: msg.site, siteName: msg.siteName,
    url: msg.url, title: msg.title || msg.url,
    responseLength: msg.responseLength || 0,
    durationMs: msg.durationMs || 0,
    messageSnippet: (msg.messageText || '').slice(0, 2000),
    timestamp: msg.timestamp || new Date().toISOString(),
  };

  history.unshift(event);
  if (history.length > MAX_HISTORY) history.length = MAX_HISTORY;

  const updates = { history };
  if (needsReply) {
    updates.pendingReply = {
      tabId: event.tabId, windowId: event.windowId, url: event.url,
      site: event.site, siteName: event.siteName, eventType: event.eventType,
      category: event.category, messageSnippet: event.messageSnippet,
      timestamp: event.timestamp,
    };
  }
  await chrome.storage.local.set(updates);

  sendToMacApp({ ...event, type: 'AGENT_EVENT', needsReply });
  await fireNotification(event, needsReply);
}

// ─── CLI Relay — history only, NO Chrome notification ────────────────────────
async function onRelayedEvent(msg) {
  const { settings } = await chrome.storage.local.get(['settings']);
  if (settings?.globalDisabled) return;

  const { history = [] } = await chrome.storage.local.get(['history']);
  const event = {
    id: `cli_${Date.now()}`,
    tabId: null, windowId: null,
    eventType: msg.eventType || 'COMPLETED',
    category: msg.eventType === 'ERROR' ? 'ACTION_REQUIRED' : 'COMPLETED',
    classificationSource: 'relay', needsReply: false,
    site: msg.site || 'cli', siteName: msg.siteName || 'Terminal',
    url: msg.url || 'terminal://local', title: msg.title || msg.url || 'Terminal',
    responseLength: msg.responseLength || 0, durationMs: msg.durationMs || 0,
    messageSnippet: msg.title || msg.messageSnippet || '',
    timestamp: msg.timestamp || new Date().toISOString(),
    isCLI: true,
  };

  history.unshift(event);
  if (history.length > MAX_HISTORY) history.length = MAX_HISTORY;
  await chrome.storage.local.set({ history });
  // Mac App already showed the AppKit card — don't double-notify via Chrome
  if (!macAppIsConnected) await fireNotification(event, false);
}

// ─── Reply Injection ──────────────────────────────────────────────────────────
async function handleSendReply(msg) {
  try {
    if (!msg.tabId || !Number.isFinite(msg.tabId)) {
      await chrome.notifications.create('aw_cli_reply', {
        type: 'basic', iconUrl: 'icons/icon128.png',
        title: 'Reply copied',
        message: `"${(msg.text || '').slice(0, 100)}" — copied to clipboard`,
        priority: 1,
      });
      await ensureOffscreen();
      chrome.runtime.sendMessage({ target: 'offscreen', type: 'COPY_TO_CLIPBOARD', text: msg.text });
      await chrome.storage.local.remove(['pendingReply']);
      return;
    }
    await chrome.tabs.sendMessage(msg.tabId, { type: 'INJECT_REPLY', text: msg.text });
    sendToMacApp({ type: 'REPLY_RECORDED', text: msg.text, tabId: msg.tabId });
    await chrome.storage.local.remove(['pendingReply']);
  } catch (e) {
    console.error('[AgentWatch] Reply failed:', e);
  }
}

// ─── Notifications ────────────────────────────────────────────────────────────
async function fireNotification(event, needsReply) {
  const siteName = event.siteName;
  const categoryTitles = {
    ACTION_REQUIRED: `${siteName} needs your input`,
    INFORMATION:     `${siteName} has a response`,
    PENDING:         `${siteName} is still working`,
    COMPLETED:       `${siteName} finished`,
  };
  const eventTypeTitles = {
    COMPLETED:    `${siteName} finished`,
    ERROR:        `${siteName} error`,
    BLOCKED:      `${siteName} needs attention`,
    PERMISSION:   `${siteName} requires permission`,
    DECISION:     `${siteName} asks a question`,
  };
  let title = categoryTitles[event.category] || eventTypeTitles[event.eventType] || `${siteName} update`;
  let message;

  if (event.isCLI || event.site === 'cli') {
    title = `Terminal — ${event.eventType}`;
    message = event.messageSnippet || event.title || 'Command finished';
  } else if (event.messageSnippet && needsReply) {
    message = event.messageSnippet.slice(0, 160);
  } else {
    const bodies = {
      COMPLETED:    `Response ready${event.responseLength > 0 ? ` · ${fmtBytes(event.responseLength)}` : ''} · ${fmtDuration(event.durationMs)}`,
      ERROR:        'Something went wrong. Click to check.',
      BLOCKED:      'Agent stuck — click to review or reply.',
      PERMISSION:   'Approval required. Click to respond.',
      DECISION:     'Agent is waiting for your answer.',
      RATE_LIMITED: 'Rate limit reached. Try again later.',
    };
    message = bodies[event.eventType] || 'New AI agent event.';
  }

  const notifId = `aw_${event.id}`;
  await chrome.notifications.create(notifId, {
    type: 'basic', iconUrl: 'icons/icon128.png', title, message,
    priority: event.eventType === 'ERROR' ? 2 : 1,
    buttons: needsReply ? [{ title: 'Reply' }, { title: 'Jump to Tab' }] : [{ title: 'Jump to Tab' }],
  });

  await chrome.storage.local.set({
    [`notif_${notifId}`]: {
      tabId: event.tabId, windowId: event.windowId, url: event.url,
      site: event.site, siteName: event.siteName, eventType: event.eventType,
      category: event.category, messageSnippet: event.messageSnippet, needsReply,
    },
  });

  playChime(event.eventType === 'ERROR' ? 0.85 : 0.7);
}

chrome.notifications.onClicked.addListener(async (notifId) => {
  chrome.notifications.clear(notifId);
  await jumpToTabFromNotif(notifId);
});

chrome.notifications.onButtonClicked.addListener(async (notifId, btnIdx) => {
  chrome.notifications.clear(notifId);
  const data = await chrome.storage.local.get([`notif_${notifId}`]);
  const info = data[`notif_${notifId}`];
  if (!info) return;

  if (info.needsReply && btnIdx === 0) {
    await chrome.storage.local.set({
      pendingReply: {
        tabId: info.tabId, windowId: info.windowId, url: info.url,
        site: info.site || '', siteName: info.siteName || '',
        eventType: info.eventType || '', category: info.category || '',
        messageSnippet: info.messageSnippet || '',
        timestamp: new Date().toISOString(),
      }
    });
    try { await chrome.tabs.create({ url: chrome.runtime.getURL('popup.html') }); } catch {}
    return;
  }
  await jumpToTabFromNotif(notifId);
});

async function jumpToTabFromNotif(notifId) {
  const data = await chrome.storage.local.get([`notif_${notifId}`]);
  const info = data[`notif_${notifId}`];
  if (!info) return;
  await jumpToTabSafe(info.tabId, info.windowId, info.url);
}

async function jumpToTabSafe(tabId, windowId, fallbackUrl) {
  if (Number.isFinite(tabId)) {
    try {
      await chrome.tabs.update(tabId, { active: true });
      if (Number.isFinite(windowId))
        await chrome.windows.update(windowId, { focused: true }).catch(() => {});
      return;
    } catch {}
  }
  if (fallbackUrl && fallbackUrl !== 'terminal://local' && fallbackUrl.startsWith('http')) {
    try {
      let tabs = await chrome.tabs.query({ url: fallbackUrl });
      if (!tabs.length) {
        const base = fallbackUrl.split('?')[0];
        tabs = await chrome.tabs.query({ url: base + '*' });
      }
      if (tabs.length) {
        await chrome.tabs.update(tabs[0].id, { active: true });
        await chrome.windows.update(tabs[0].windowId, { focused: true }).catch(() => {});
        return;
      }
    } catch {}
    chrome.tabs.create({ url: fallbackUrl });
  }
}

// ─── Test ─────────────────────────────────────────────────────────────────────
async function sendTestNotification() {
  await chrome.notifications.create('aw_test', {
    type: 'basic', iconUrl: 'icons/icon128.png',
    title: 'AgentWatch is working!',
    message: "You'll see a notification like this when an AI agent finishes.",
    priority: 1,
  });
  sendToMacApp({ type: 'TEST' });
  playChime(0.7);
}

// ─── Badge ────────────────────────────────────────────────────────────────────
function updateBadge(count) {
  chrome.action.setBadgeText({ text: count > 0 ? String(count) : '' });
  if (count > 0) chrome.action.setBadgeBackgroundColor({ color: '#6366f1' });
}

// ─── Tab Cleanup ──────────────────────────────────────────────────────────────
chrome.tabs.onRemoved.addListener(async (tabId) => {
  const sessions = await getSessions();
  if (sessions[tabId]) {
    delete sessions[tabId];
    await chrome.storage.local.set({ activeSessions: sessions });
    updateBadge(Object.keys(sessions).length);
    if (chrome.alarms) chrome.alarms.clear(`aw_fallback_${tabId}`);
  }
});

chrome.tabs.onUpdated.addListener(async (tabId, changeInfo) => {
  if (changeInfo.url) {
    const sessions = await getSessions();
    if (sessions[tabId]) {
      delete sessions[tabId];
      await chrome.storage.local.set({ activeSessions: sessions });
      updateBadge(Object.keys(sessions).length);
      if (chrome.alarms) chrome.alarms.clear(`aw_fallback_${tabId}`);
    }
  }
});

chrome.runtime.onStartup.addListener(cleanStaleSessions);
chrome.runtime.onInstalled.addListener(cleanStaleSessions);
setInterval(cleanStaleSessions, 5 * 60 * 1000);

async function cleanStaleSessions() {
  const sessions = await getSessions();
  const now = Date.now();
  const cleaned = {};
  for (const [id, s] of Object.entries(sessions)) {
    if (now - s.startTime < STALE_SESSION_MS) cleaned[id] = s;
    else if (chrome.alarms) chrome.alarms.clear(`aw_fallback_${id}`);
  }
  await chrome.storage.local.set({ activeSessions: cleaned });
  updateBadge(Object.keys(cleaned).length);
}

// ─── Helpers ──────────────────────────────────────────────────────────────────
async function getSessions() {
  const { activeSessions = {} } = await chrome.storage.local.get(['activeSessions']);
  return activeSessions;
}
function fmtBytes(n) { return n >= 1000 ? `${(n/1000).toFixed(1)}k chars` : `${n} chars`; }
function fmtDuration(ms) {
  if (!ms) return '';
  const s = Math.round(ms/1000);
  return s < 60 ? `${s}s` : `${Math.floor(s/60)}m ${s%60}s`;
}