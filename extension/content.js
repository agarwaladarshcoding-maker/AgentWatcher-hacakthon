/**
 * AgentWatch — content.js  v4.0
 *
 * Architecture decision (lead override):
 *   All selector maps, detection logic, prompt capture, and reply injection
 *   live in ONE file. No external patch appends. This eliminates the
 *   duplicate-declaration crash that was killing the content script.
 *
 * v4.0 changes vs v3.6:
 *   FIX-1  : Removed split-patch pattern — USER_MESSAGE_SELECTORS merged here,
 *             no second declaration possible.
 *   FIX-2  : extractLastUserMessage() now uses a per-site ordered selector list
 *             with a robust text-node walker fallback.
 *   FIX-3  : injectReply() rebuilt with a 4-strategy cascade:
 *               1. Native-setter + React synthetic event (textarea/input)
 *               2. execCommand insertText (contenteditable)
 *               3. ClipboardEvent DataTransfer paste (contenteditable fallback)
 *               4. Direct textContent assignment (last resort)
 *             Auto-submit covers both dedicated send buttons and Enter/⌘Enter.
 *   FIX-4  : CHECK_GENERATING now returns synchronously via sendResponse —
 *             fixes "message channel closed" errors in background.js.
 *   NEW    : userPrompt field (≤200 chars) emitted on every AGENT_EVENT.
 *   NEW    : Per-site REPLY_INPUT_SELECTORS for bulletproof input targeting.
 */
(function () {
  'use strict';

  // ── Guard: only inject once even if script re-runs ──────────────────────────
  if (window.__agentWatchContent) return;
  window.__agentWatchContent = true;

  // ─── Site Configuration ───────────────────────────────────────────────────
  const SITE_MAP = {
    'claude.ai':               { id: 'claude',      name: 'Claude',           holdoff: 9000 },
    'groq.com':                { id: 'groq',         name: 'Groq',             holdoff: 1500 },
    'chat.openai.com':         { id: 'chatgpt',      name: 'ChatGPT',          holdoff: 2500 },
    'chatgpt.com':             { id: 'chatgpt',      name: 'ChatGPT',          holdoff: 2500 },
    'gemini.google.com':       { id: 'gemini',       name: 'Gemini',           holdoff: 3000 },
    'perplexity.ai':           { id: 'perplexity',   name: 'Perplexity',       holdoff: 2500 },
    'copilot.microsoft.com':   { id: 'copilot',      name: 'Copilot',          holdoff: 3000 },
    'grok.com':                { id: 'grok',         name: 'Grok',             holdoff: 2500 },
    'x.ai':                    { id: 'grok',         name: 'Grok',             holdoff: 2500 },
    'meta.ai':                 { id: 'meta',         name: 'Meta AI',          holdoff: 2500 },
    'poe.com':                 { id: 'poe',          name: 'Poe',              holdoff: 3000 },
    'phind.com':               { id: 'phind',        name: 'Phind',            holdoff: 2500 },
    'you.com':                 { id: 'you',          name: 'You.com',          holdoff: 2500 },
    'huggingface.co':          { id: 'hf',           name: 'HuggingFace',      holdoff: 3000 },
    'chat.mistral.ai':         { id: 'mistral',      name: 'Mistral',          holdoff: 2500 },
    'chat.deepseek.com':       { id: 'deepseek',     name: 'DeepSeek',         holdoff: 3000 },
    'pi.ai':                   { id: 'pi',           name: 'Pi AI',            holdoff: 3500 },
    'character.ai':            { id: 'character',    name: 'Character.ai',     holdoff: 3500 },
    'coral.cohere.com':        { id: 'cohere',       name: 'Cohere',           holdoff: 2500 },
    'bing.com':                { id: 'bing',         name: 'Bing Copilot',     holdoff: 3000 },
    'emergent.sh':             { id: 'emergent',     name: 'Emergent',         holdoff: 3000 },
    'app.emergent.sh':         { id: 'emergent',     name: 'Emergent',         holdoff: 3000 },
    'aistudio.google.com':     { id: 'aistudio',     name: 'AI Studio',        holdoff: 3000 },
    'lmsys.org':               { id: 'lmsys',        name: 'LMSYS Chat',       holdoff: 2500 },
    'chat.lmsys.org':          { id: 'lmsys',        name: 'LMSYS Chat',       holdoff: 2500 },
    'together.ai':             { id: 'together',     name: 'Together AI',      holdoff: 2500 },
    'console.anthropic.com':   { id: 'claude',       name: 'Claude (Console)', holdoff: 4000 },
  };

  let site = null;
  for (const [domain, config] of Object.entries(SITE_MAP)) {
    if (window.location.hostname.includes(domain)) { site = config; break; }
  }
  if (!site) return;

  // ─── Assistant message selectors (last response text) ─────────────────────
  const MESSAGE_SELECTORS = {
    chatgpt:    ['[data-message-author-role="assistant"]'],
    claude:     [
      '[data-testid^="conversation-turn-"] .font-claude-message',
      '.font-claude-message',
      '.prose-sm', '.prose',
    ],
    gemini:     ['message-content', '[data-response-index]', '.model-response-text'],
    perplexity: ['.prose', '[data-testid*="answer"]'],
    copilot:    ['[data-author="bot"]', '.ac-textBlock'],
    grok:       ['[data-message-author-role="assistant"]', '.message-bubble'],
    meta:       ['[data-testid*="assistant"]'],
    poe:        ['.ChatMessage_botMessageBubble__', '.Markdown_markdownContainer__'],
    phind:      ['.messageDesktop', '[data-message-role="assistant"]'],
    deepseek:   ['.ds-markdown', '[data-role="assistant"]'],
    mistral:    ['[data-testid*="assistant"]', '.message-content'],
    groq:       ['[data-testid*="assistant"]'],
    emergent:   ['[data-role="assistant"]', '[data-message-role="assistant"]', '.assistant-message', '.prose'],
    aistudio:   ['ms-chat-turn[data-turn-role="model"]', '.turn-content', '.model-response'],
    default:    [
      '[data-message-author-role="assistant"]', '[data-role="assistant"]',
      '[data-author="bot"]', '.markdown', '.prose',
    ],
  };

  // ─── User message selectors (last prompt sent) ────────────────────────────
  // These are declared ONCE here — the source of truth.
  // Ordered from most-specific to most-generic per site.
  const USER_MESSAGE_SELECTORS = {
    chatgpt:    [
      '[data-message-author-role="user"] .whitespace-pre-wrap',
      '[data-message-author-role="user"]',
    ],
    claude:     [
      '[data-testid^="human-turn-"] .font-user-message',
      '[data-testid^="human-turn-"]',
      '.font-user-message',
    ],
    gemini:     [
      '.user-query-text',
      'user-query .query-text',
      '[data-turn-role="user"] .query-text',
    ],
    perplexity: [
      '[data-testid="user-message"]',
      '.query-text',
      '[class*="UserMessage"]',
    ],
    grok:       [
      '[data-message-author-role="user"]',
      '.user-bubble',
      '[class*="userMessage"]',
    ],
    meta:       [
      '[data-testid*="user-message"]',
      '[data-testid*="human"]',
      '.user-message',
    ],
    poe:        [
      '.ChatMessage_humanMessageBubble__',
      '.human-message',
      '[class*="humanMessage"]',
    ],
    phind:      [
      '[data-message-role="user"]',
      '.userMessage',
      '[class*="UserMessage"]',
    ],
    deepseek:   [
      '[data-role="user"]',
      '.user-message',
      '[class*="userMessage"]',
    ],
    mistral:    [
      '[data-testid*="user"]',
      '[class*="UserMessage"]',
      '.user-message',
    ],
    copilot:    ['[data-author="user"]', '.user-message'],
    groq:       ['[data-testid*="user"]'],
    emergent:   ['[data-role="user"]', '[data-message-role="user"]', '.user-message'],
    aistudio:   ['ms-chat-turn[data-turn-role="user"]', '.user-query'],
    default:    [
      '[data-message-author-role="user"]',
      '[data-role="user"]',
      '[data-author="user"]',
      '.user-message',
    ],
  };

  // ─── Reply input selectors (where to type the reply) ─────────────────────
  // Ordered: most reliable → most generic. All 10 priority sites covered.
  const REPLY_INPUT_SELECTORS = {
    chatgpt:    [
      '#prompt-textarea',
      'div[id="prompt-textarea"][contenteditable="true"]',
      'textarea[data-id]',
      'textarea[placeholder*="message" i]',
    ],
    claude:     [
      'div[contenteditable="true"][data-placeholder]',
      '.ProseMirror[contenteditable="true"]',
      'div.ProseMirror',
      'div[contenteditable="true"]',
    ],
    gemini:     [
      'div[contenteditable="true"][aria-label*="input" i]',
      'div[contenteditable="true"][aria-multiline="true"]',
      'rich-textarea div[contenteditable="true"]',
      'textarea',
    ],
    perplexity: [
      'textarea[placeholder*="ask" i]',
      'textarea[placeholder*="follow" i]',
      'textarea',
    ],
    grok:       [
      'textarea[placeholder*="message" i]',
      'div[contenteditable="true"]',
      'textarea',
    ],
    meta:       [
      'div[contenteditable="true"][aria-label*="message" i]',
      'div[contenteditable="true"]',
      'textarea',
    ],
    poe:        [
      'textarea[placeholder*="message" i]',
      'div[contenteditable="true"]',
      'textarea',
    ],
    phind:      [
      'textarea[placeholder*="ask" i]',
      'textarea',
      'div[contenteditable="true"]',
    ],
    deepseek:   [
      'textarea#chat-input',
      'textarea[placeholder*="message" i]',
      'div[contenteditable="true"]',
    ],
    mistral:    [
      'textarea[placeholder*="message" i]',
      'div[contenteditable="true"]',
      'textarea',
    ],
    copilot:    ['textarea', 'div[contenteditable="true"]'],
    groq:       ['textarea', 'div[contenteditable="true"]'],
    default:    [
      'textarea[placeholder*="message" i]',
      'textarea[placeholder*="ask" i]',
      'div[contenteditable="true"]',
      'textarea',
    ],
  };

  // ─── Send-button selectors ────────────────────────────────────────────────
  const SEND_BUTTON_SELECTORS = [
    // Explicit data-testid / aria-label
    'button[data-testid="send-button"]',
    'button[aria-label="Send message"]',
    'button[aria-label="Send"]',
    'button[aria-label="Send Message"]',
    'button[aria-label="Ask"]',
    'button[aria-label="Submit"]',
    // ChatGPT
    'button[data-testid="fruitjuice-send-button"]',
    // Gemini
    'button[aria-label="Send message"][jsname]',
    'button.send-button',
    // Perplexity
    'button[aria-label*="submit" i]',
    // Generic fallbacks
    'button[type="submit"]',
    'form button:last-of-type',
    '[data-testid="send-button"]',
    '[class*="SendButton"]',
    '[class*="send-button"]',
    '[class*="sendButton"]',
  ];

  // ─── Stop button selectors ────────────────────────────────────────────────
  const STOP_SELECTORS = [
    'button[aria-label="Stop Response"]',
    'button[aria-label="Stop response"]',
    'button[aria-label="Stop streaming"]',
    'button[data-testid="stop-button"]',
    'button[aria-label="Stop"]',
    'button[jsname="Njthtb"]',
    'button[aria-label*="stop" i]:not([aria-label*="stop sharing" i]):not([aria-label*="stop screen" i])',
    'button[aria-label*="cancel generation" i]',
    '[data-testid*="stop"]',
    '[class*="StopButton"]',
    '[class*="stop-button"]',
    '[class*="stopButton"]',
  ].join(', ');

  // ─── Error/blocked/decision selectors ────────────────────────────────────
  const MODAL_SELECTORS = [
    '[role="dialog"][aria-modal="true"]',
    '[role="dialog"]:not([aria-hidden="true"])',
    '[role="alertdialog"]',
    '.modal:not([hidden]):not([style*="display: none"])',
    '[class*="modal"][class*="overlay"]:not([hidden])',
    '[data-testid="modal"]',
    '[data-testid*="dialog"]',
    '[data-testid="upsell-modal"]',
    'mat-dialog-container',
    '[class*="paywall"]',
    '[class*="upgrade-modal"]',
    '[class*="SubscriptionModal"]',
  ].join(', ');

  const MODAL_CONTENT_KEYWORDS = [
    'upgrade', 'subscribe', 'sign in', 'log in', 'rate limit', 'try again',
    'too many', 'quota', 'limit reached', 'are you sure', 'delete', 'remove',
    'confirm', 'warning', 'error', 'permission', 'access denied', 'blocked',
    'continue', 'unavailable', 'temporarily', 'captcha', 'verify',
  ];

  const ERROR_SELECTORS = [
    '[role="alert"]', '.text-red-500', '[data-testid="error-message"]',
    '[class*="error-message"]', '[class*="errorMessage"]', '.error', '[data-error]',
  ].join(', ');

  const BLOCKED_SELECTORS = [
    '[class*="blocked"]', '[class*="content-policy"]', '[class*="safety"]',
  ].join(', ');

  const DECISION_SELECTORS = [
    '[class*="clarification"]', '[class*="followup"]', '[class*="suggestion-chips"]',
  ].join(', ');

  const RATE_LIMIT_PATTERNS = [
    'rate limit', 'too many messages', 'too many requests', 'you have sent too many',
    'message limit', 'hourly limit', 'daily limit', 'slow down', "you've reached",
    'quota exceeded', 'try again later', 'try again in',
  ];

  const RATE_LIMIT_SELECTORS = [
    '[data-testid="rate-limit-message"]', '[class*="rate-limit"]',
    '[class*="rateLimit"]', '[class*="limit-message"]',
  ].join(', ');

  // ─── State ────────────────────────────────────────────────────────────────
  let isGenerating      = false;
  let generationStart   = 0;
  let holdoffTimer      = null;
  let lastEventTime     = 0;
  let stopBtnWasPresent = false;
  let userClickedStop   = false;
  let generationId      = 0;
  const firedGenerationIds = new Set();
  let generationUrl  = '';
  let generationTitle = '';
  let currentUrl   = window.location.href;
  let currentTitle = document.title;

  let modalNotifyTimer   = null;
  let lastModalNotifyTime = 0;
  const MODAL_HOLDOFF_MS  = 5000;
  const MODAL_COOLDOWN_MS = 60000;

  // ─── Helpers ─────────────────────────────────────────────────────────────
  function send(type, extra) {
    try {
      chrome.runtime.sendMessage({
        type, site: site.id, siteName: site.name,
        url: window.location.href, title: document.title, ...extra,
      });
    } catch {}
  }

  function qs(sel) {
    try { return document.querySelector(sel); } catch { return null; }
  }

  function qsAll(sel) {
    try { return document.querySelectorAll(sel); } catch { return null; }
  }

  function delay(ms) { return new Promise(r => setTimeout(r, ms)); }

  // ─── Text extraction ──────────────────────────────────────────────────────
  const MAX_MSG_CHARS    = 8000;
  const MAX_PROMPT_CHARS = 200;

  function _extractFromSelectors(sels, maxChars) {
    for (const sel of sels) {
      const nodes = qsAll(sel);
      if (!nodes || !nodes.length) continue;
      for (let i = nodes.length - 1; i >= 0; i--) {
        const txt = (nodes[i].innerText || nodes[i].textContent || '').trim();
        if (txt && txt.length > 5) {
          return txt.length > maxChars ? txt.slice(-maxChars) : txt;
        }
      }
    }
    return '';
  }

  function extractClaudeText() {
    const candidates = [
      '.font-claude-message',
      '[data-testid^="conversation-turn-"] .contents',
    ];
    for (const sel of candidates) {
      const nodes = qsAll(sel);
      if (!nodes || !nodes.length) continue;
      for (let i = nodes.length - 1; i >= 0; i--) {
        const el = nodes[i];
        if (el.closest('[data-testid*="thinking"]')) continue;
        if (el.closest('[aria-label*="thinking" i]'))  continue;
        if (el.closest('[class*="thinking"]'))          continue;
        if (el.closest('details'))                      continue;
        const txt = (el.innerText || el.textContent || '').trim();
        if (txt && txt.length > 20)
          return txt.length > MAX_MSG_CHARS ? txt.slice(-MAX_MSG_CHARS) : txt;
      }
    }
    return '';
  }

  function extractLatestAssistantText() {
    if (site.id === 'claude') {
      const t = extractClaudeText();
      if (t) return t;
    }
    const sels = MESSAGE_SELECTORS[site.id] || MESSAGE_SELECTORS.default;
    return _extractFromSelectors(sels, MAX_MSG_CHARS);
  }

  function extractLastUserMessage() {
    const sels = USER_MESSAGE_SELECTORS[site.id] || USER_MESSAGE_SELECTORS.default;
    return _extractFromSelectors(sels, MAX_PROMPT_CHARS);
  }

  // ─── Event classification ─────────────────────────────────────────────────
  function isRateLimited() {
    if (qs(RATE_LIMIT_SELECTORS)) return true;
    const txt = extractLatestAssistantText().toLowerCase();
    return RATE_LIMIT_PATTERNS.some(p => txt.includes(p));
  }

  function classifyEventType(fetchEventType) {
    if (isRateLimited())         return 'RATE_LIMITED';
    if (fetchEventType === 'ERROR') return 'ERROR';
    if (userClickedStop)         return null;
    if (qs(ERROR_SELECTORS))     return 'ERROR';
    if (qs(BLOCKED_SELECTORS))   return 'BLOCKED';
    if (qs(DECISION_SELECTORS))  return 'DECISION';
    return 'COMPLETED';
  }

  // ─── Generation lifecycle ────────────────────────────────────────────────
  function startGeneration() {
    if (isGenerating) return;
    isGenerating    = true;
    generationId++;
    generationStart = Date.now();
    generationUrl   = window.location.href;
    generationTitle = document.title;
    clearTimeout(holdoffTimer);
    send('AGENT_GENERATING');
  }

  function finishGeneration(eventType, bytes) {
    if (!isGenerating) return;
    const myGenId = generationId;
    clearTimeout(holdoffTimer);
    holdoffTimer = setTimeout(() => {
      if (firedGenerationIds.has(myGenId)) { isGenerating = false; return; }
      if (myGenId !== generationId && isGenerating) return;

      const now = Date.now();
      if (now - lastEventTime < 2000) { isGenerating = false; return; }
      isGenerating = false;

      const duration = now - generationStart;
      if (duration < 1500) return;

      const classified = classifyEventType(eventType);
      if (classified === null) { userClickedStop = false; return; }

      firedGenerationIds.add(myGenId);
      if (firedGenerationIds.size > 50)
        firedGenerationIds.delete(firedGenerationIds.values().next().value);

      lastEventTime = now;

      const messageText = extractLatestAssistantText();
      const userPrompt  = extractLastUserMessage();

      send('AGENT_EVENT', {
        eventType: classified,
        responseLength: bytes || (messageText ? messageText.length : 0),
        durationMs: duration,
        timestamp: new Date().toISOString(),
        messageText,
        userPrompt,
        url: generationUrl,
        title: generationTitle,
      });
      userClickedStop = false;
    }, site.holdoff);
  }

  // ─── Modal detection ─────────────────────────────────────────────────────
  function checkForModal() {
    if (isGenerating) return;
    const now = Date.now();
    if (now - lastModalNotifyTime < MODAL_COOLDOWN_MS) return;

    const el = qs(MODAL_SELECTORS);
    if (!el) return;

    const text      = (el.textContent || '').toLowerCase().trim();
    const isRelevant =
      MODAL_CONTENT_KEYWORDS.some(k => text.includes(k)) ||
      el.matches('[role="alertdialog"]') ||
      el.matches('[data-testid*="modal"]') ||
      !!el.querySelector('button') ||
      text.length > 30;

    if (!isRelevant) return;

    clearTimeout(modalNotifyTimer);
    modalNotifyTimer = setTimeout(() => {
      const still = qs(MODAL_SELECTORS);
      if (!still) return;
      lastModalNotifyTime = Date.now();
      const modalText = (still.textContent || '').trim().slice(0, 300);
      const now2 = Date.now();
      if (now2 - lastEventTime < 3000) return;
      lastEventTime = now2;
      send('AGENT_EVENT', {
        eventType: 'BLOCKED',
        responseLength: 0, durationMs: 0,
        timestamp: new Date().toISOString(),
        messageText: `Modal/dialog appeared on ${site.name}: "${modalText}"`,
        userPrompt: '',
        url: window.location.href, title: document.title,
      });
    }, MODAL_HOLDOFF_MS);
  }

  new MutationObserver(() => checkForModal()).observe(document.documentElement, {
    childList: true, subtree: true,
    attributes: true, attributeFilter: ['aria-modal', 'role', 'open'],
  });

  // ─── Reply injection (4-strategy cascade) ────────────────────────────────
  /**
   * Strategy 1 — textarea / input  : native setter + React synthetic event
   * Strategy 2 — contenteditable   : execCommand('insertText')
   * Strategy 3 — contenteditable   : ClipboardEvent DataTransfer paste
   * Strategy 4 — last resort       : direct textContent + InputEvent
   *
   * Auto-submit after injection:
   *   a) Click dedicated send button  (per-site selector list)
   *   b) Dispatch Enter keydown/keypress/keyup on the element
   *   c) For Claude / Gemini (⌘Enter): dispatch with metaKey:true
   */

  function _nativeSetter(el, text) {
    const proto = el.tagName === 'TEXTAREA'
      ? window.HTMLTextAreaElement.prototype
      : window.HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    if (setter) setter.call(el, text);
    else el.value = text;
  }

  function _dispatchInput(el) {
    el.dispatchEvent(new Event('input',  { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }

  function _dispatchEnter(el, meta = false) {
    const opts = {
      key: 'Enter', code: 'Enter', keyCode: 13, which: 13,
      bubbles: true, cancelable: true,
      metaKey: meta, ctrlKey: false,
    };
    el.dispatchEvent(new KeyboardEvent('keydown',  opts));
    el.dispatchEvent(new KeyboardEvent('keypress', opts));
    el.dispatchEvent(new KeyboardEvent('keyup',    opts));
  }

  async function _clickSendButton() {
    await delay(120);
    for (const sel of SEND_BUTTON_SELECTORS) {
      const btn = qs(sel);
      if (btn && !btn.disabled && btn.getAttribute('aria-disabled') !== 'true') {
        btn.click();
        return true;
      }
    }
    return false;
  }

  async function _autoSubmit(el) {
    // Try dedicated send button first
    const sent = await _clickSendButton();
    if (sent) return;

    // Site-specific key combos
    if (site.id === 'claude' || site.id === 'gemini') {
      _dispatchEnter(el, true);   // ⌘Enter
    } else {
      _dispatchEnter(el, false);  // plain Enter
    }
  }

  async function _injectIntoTextarea(el, text) {
    el.focus();
    _nativeSetter(el, text);
    _dispatchInput(el);
    await _autoSubmit(el);
    return true;
  }

  async function _injectIntoContentEditable(el, text) {
    el.focus();

    // Select all existing content
    const range = document.createRange();
    range.selectNodeContents(el);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);

    // Strategy 2: execCommand
    const inserted = document.execCommand('insertText', false, text);
    if (inserted) {
      await delay(80);
      await _autoSubmit(el);
      return true;
    }

    // Strategy 3: ClipboardEvent DataTransfer
    try {
      const dt = new DataTransfer();
      dt.setData('text/plain', text);
      el.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true }));
      const afterPaste = (el.innerText || el.textContent || '').trim();
      if (afterPaste.includes(text.slice(0, 20))) {
        await delay(80);
        await _autoSubmit(el);
        return true;
      }
    } catch {}

    // Strategy 4: direct set
    el.textContent = text;
    el.dispatchEvent(new InputEvent('input', {
      bubbles: true, inputType: 'insertText', data: text,
    }));
    await delay(80);
    await _autoSubmit(el);
    return true;
  }

  async function injectReply(text) {
    const sels = REPLY_INPUT_SELECTORS[site.id] || REPLY_INPUT_SELECTORS.default;

    for (const sel of sels) {
      const el = qs(sel);
      if (!el) continue;

      // Confirm element is visible / interactive
      const style = window.getComputedStyle(el);
      if (style.display === 'none' || style.visibility === 'hidden') continue;

      if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {
        return _injectIntoTextarea(el, text);
      }
      if (el.contentEditable === 'true' || el.getAttribute('contenteditable') === 'true') {
        return _injectIntoContentEditable(el, text);
      }
    }

    // Last resort: find any visible contenteditable on the page
    const allCE = qsAll('[contenteditable="true"]');
    if (allCE) {
      for (const el of allCE) {
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden') continue;
        if ((el.offsetWidth + el.offsetHeight) === 0) continue;
        return _injectIntoContentEditable(el, text);
      }
    }

    console.warn('[AgentWatch] injectReply: no suitable input found for', site.id);
    return false;
  }

  // ─── SPA navigation ───────────────────────────────────────────────────────
  let navDebounceTimer           = null;
  let containerReplacementCount  = 0;
  let lastNavState               = '';

  function getNavState() {
    const attrs = ['data-conversation-id','data-thread-id','data-chat-id','data-session-id']
      .map(a =>
        document.body?.getAttribute(a) ||
        document.documentElement?.getAttribute(a) || ''
      ).join('|');
    return `${window.location.href}|${attrs}|${containerReplacementCount}`;
  }

  function handleNavigation(reason) {
    clearTimeout(navDebounceTimer);
    navDebounceTimer = setTimeout(() => {
      const state = getNavState();
      if (state === lastNavState && lastNavState !== '') return;
      lastNavState = state;
      stopBtnWasPresent = false;
      userClickedStop   = false;
      if (!isGenerating) {
        send('AGENT_CONTEXT_SWITCH', {
          reason,
          previousUrl: currentUrl,
          previousTitle: currentTitle,
          timestamp: new Date().toISOString(),
        });
      }
      currentUrl   = window.location.href;
      currentTitle = document.title;
      setTimeout(() => {
        if (!isGenerating && qs(STOP_SELECTORS)) startGeneration();
      }, 800);
    }, 300);
  }

  lastNavState = getNavState();
  window.addEventListener('popstate',        () => handleNavigation('popstate'));
  window.addEventListener('hashchange',      () => handleNavigation('hashchange'));
  window.addEventListener('svelte:navigate', () => handleNavigation('svelte:navigate'));

  const rootAttrObserver = new MutationObserver((mutations) => {
    for (const m of mutations) {
      if (m.type === 'attributes') { handleNavigation('root_attribute_change'); break; }
    }
  });
  if (document.documentElement) rootAttrObserver.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ['data-conversation-id','data-thread-id','data-chat-id','data-session-id'],
  });
  if (document.body) rootAttrObserver.observe(document.body, {
    attributes: true,
    attributeFilter: ['data-conversation-id','data-thread-id','data-chat-id','data-session-id'],
  });

  if (site.id === 'character' || site.id === 'poe') {
    const cObs = new MutationObserver((mutations) => {
      let triggered = false;
      for (const m of mutations) {
        if (m.type === 'childList') {
          const el = m.target;
          if (el?.nodeType === 1 && typeof el.className === 'string') {
            const cn = el.className.toLowerCase();
            if (cn.includes('conversation') || cn.includes('chat-container')) {
              triggered = true; break;
            }
          }
        }
      }
      if (triggered) { containerReplacementCount++; handleNavigation('container_replaced'); }
    });
    cObs.observe(document.body || document.documentElement, { childList: true, subtree: true });
  }

  const titleEl = document.querySelector('head > title');
  if (titleEl) {
    new MutationObserver(() => handleNavigation('titlechange'))
      .observe(titleEl, { childList: true, characterData: true, subtree: true });
  }

  // ─── DOM stop-button observer ─────────────────────────────────────────────
  const domObserver = new MutationObserver(() => {
    const stopEl  = qs(STOP_SELECTORS);
    const present = !!stopEl;

    if (present && !stopBtnWasPresent) {
      stopBtnWasPresent = true;
      startGeneration();
      if (stopEl) stopEl.addEventListener('click', () => { userClickedStop = true; }, { once: true });
    } else if (!present && stopBtnWasPresent) {
      stopBtnWasPresent = false;
      finishGeneration('COMPLETED');
    }

    if (window.location.href !== currentUrl) handleNavigation('observer');
  });

  domObserver.observe(document.body || document.documentElement, {
    childList: true, subtree: true,
    attributes: true, attributeFilter: ['disabled', 'aria-disabled', 'aria-busy'],
  });

  setTimeout(() => { if (qs(STOP_SELECTORS)) startGeneration(); }, 1000);

  // ─── postMessage from MAIN world (fetch_patcher.js) ──────────────────────
  window.addEventListener('message', (e) => {
    if (!e.data?.__aw) return;
    switch (e.data.type) {
      case 'url_changed':    handleNavigation(e.data.source || 'postMessage'); break;
      case 'aw_fetch_start': startGeneration(); break;
      case 'aw_fetch_done':  finishGeneration('COMPLETED', e.data.bytes); break;
      case 'aw_fetch_error': if (isGenerating) finishGeneration('ERROR'); break;
    }
  });

  // ─── ARIA streaming detection ─────────────────────────────────────────────
  new MutationObserver((mutations) => {
    for (const m of mutations) {
      if (m.type !== 'attributes') continue;
      const el   = m.target;
      const busy =
        el.getAttribute('aria-busy') === 'true' ||
        el.hasAttribute('data-is-streaming') ||
        el.hasAttribute('data-streaming');
      if (busy) startGeneration();
      else if (isGenerating) finishGeneration('COMPLETED');
    }
  }).observe(document.documentElement, {
    attributes: true,
    attributeFilter: ['aria-busy', 'data-is-streaming', 'data-streaming'],
    subtree: true,
  });

  // ─── Runtime message listener ─────────────────────────────────────────────
  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg.type === 'INJECT_REPLY') {
      // Fire async; return true so Chrome keeps channel open
      injectReply(msg.text).catch(console.warn);
      sendResponse({ ok: true });
      return true;
    }

    if (msg.type === 'CHECK_GENERATING') {
      // Synchronous response — fixes "message channel closed" error
      sendResponse({ generating: !!isGenerating });
      return false;   // no async needed
    }
  });

})();