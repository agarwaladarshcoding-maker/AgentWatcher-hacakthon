/**
 * AgentWatch - llm_router.js
 *
 * Lightweight local-LLM classifier for AI-agent events.
 * Talks to an Ollama-compatible endpoint (default http://localhost:11434).
 *
 * Exposes (via globalThis because MV3 service workers import scripts, not modules):
 *   AgentWatchLLM.classify({ siteName, eventType, messageText, durationMs, responseLength })
 *     -> { category, needsReply, reason, source }
 *   AgentWatchLLM.ping(endpoint) -> { ok, version?, error? }
 *   AgentWatchLLM.getDefaults()
 *
 * Categories: ACTION_REQUIRED | INFORMATION | PENDING | COMPLETED
 *
 * Design notes:
 *   - Any failure (network, timeout, bad JSON) MUST fall back to the heuristic
 *     derived from the raw eventType. We never block the notification pipeline.
 *   - Request timeout is 1500 ms by default. Local small models respond well
 *     within that window; anything slower would hurt UX anyway.
 */
(function () {
  'use strict';

  const DEFAULTS = Object.freeze({
    enabled:  false,
    endpoint: 'http://localhost:11434',
    model:    'llama3.2:1b',
    timeoutMs: 1500,
  });

  const VALID_CATEGORIES = new Set(['ACTION_REQUIRED', 'INFORMATION', 'PENDING', 'COMPLETED']);

  // Heuristic fallback — used when LLM is disabled, unreachable, or malformed.
  function heuristicFromEventType(eventType) {
    switch (eventType) {
      case 'DECISION':
      case 'BLOCKED':
      case 'PERMISSION':
        return { category: 'ACTION_REQUIRED', needsReply: true,  reason: `heuristic:${eventType}` };
      case 'ERROR':
        return { category: 'ACTION_REQUIRED', needsReply: true,  reason: 'heuristic:ERROR' };
      case 'COMPLETED':
        return { category: 'COMPLETED',       needsReply: false, reason: 'heuristic:COMPLETED' };
      default:
        return { category: 'INFORMATION',     needsReply: false, reason: `heuristic:${eventType || 'unknown'}` };
    }
  }

  const SYSTEM_PROMPT =
    'You are an event router for an AI-agent monitor. Given a short snapshot of ' +
    'what an AI assistant just produced, pick exactly ONE category from this set:\n' +
    '  - ACTION_REQUIRED: the assistant asks the user a question, needs permission, ' +
    'is stuck, errored out, or explicitly waits for input.\n' +
    '  - INFORMATION: the assistant delivered an informational answer; no user action needed.\n' +
    '  - PENDING: the assistant is mid-task, waiting on an external step, or partially done.\n' +
    '  - COMPLETED: the assistant finished a task successfully and no follow-up is required.\n' +
    'Also decide needsReply (true only if the user realistically needs to type something now). ' +
    'Return ONLY a compact JSON object with keys category, needsReply, reason — no prose.';

  function buildUserPrompt(ev) {
    const snippet = (ev.messageText || '').slice(0, 1200);
    const safe = {
      siteName: ev.siteName || '',
      rawEventType: ev.eventType || '',
      durationMs: ev.durationMs || 0,
      responseLength: ev.responseLength || 0,
      messageSnippet: snippet,
    };
    return (
      'Event:\n' + JSON.stringify(safe, null, 2) +
      '\n\nRespond with JSON: {"category":"...","needsReply":true|false,"reason":"..."}'
    );
  }

  async function fetchWithTimeout(url, options, ms) {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), ms);
    try {
      return await fetch(url, { ...options, signal: ctrl.signal });
    } finally {
      clearTimeout(t);
    }
  }

  function parseLLMJson(raw) {
    if (!raw || typeof raw !== 'string') return null;
    // Try direct JSON first
    try { return JSON.parse(raw); } catch { /* continue */ }
    // Fallback: first {...} block
    const m = raw.match(/\{[\s\S]*\}/);
    if (!m) return null;
    try { return JSON.parse(m[0]); } catch { return null; }
  }

  function normalizeResult(parsed) {
    if (!parsed || typeof parsed !== 'object') return null;
    const cat = String(parsed.category || '').toUpperCase().trim();
    if (!VALID_CATEGORIES.has(cat)) return null;
    return {
      category:   cat,
      needsReply: !!parsed.needsReply || cat === 'ACTION_REQUIRED',
      reason:     String(parsed.reason || '').slice(0, 200),
    };
  }

  async function classify(event, settings) {
    const cfg = { ...DEFAULTS, ...(settings || {}) };
    const fallback = heuristicFromEventType(event?.eventType);

    if (!cfg.enabled) return { ...fallback, source: 'heuristic-disabled' };

    const url = cfg.endpoint.replace(/\/+$/, '') + '/api/generate';
    const body = {
      model:   cfg.model,
      stream:  false,
      format:  'json',
      options: { temperature: 0, num_predict: 80 },
      system:  SYSTEM_PROMPT,
      prompt:  buildUserPrompt(event),
    };

    try {
      const res = await fetchWithTimeout(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      }, cfg.timeoutMs);

      if (!res.ok) return { ...fallback, source: `fallback-http-${res.status}` };

      const data = await res.json();
      // Ollama returns { response: "<text>" }
      const parsed = parseLLMJson(data?.response);
      const normalized = normalizeResult(parsed);
      if (!normalized) return { ...fallback, source: 'fallback-bad-json' };
      return { ...normalized, source: 'llm:' + cfg.model };
    } catch (err) {
      const reason = err?.name === 'AbortError' ? 'timeout' : (err?.message || 'network');
      return { ...fallback, source: 'fallback-' + reason };
    }
  }

  async function ping(endpoint) {
    const url = (endpoint || DEFAULTS.endpoint).replace(/\/+$/, '') + '/api/version';
    try {
      const res = await fetchWithTimeout(url, { method: 'GET' }, 1500);
      if (!res.ok) return { ok: false, error: `HTTP ${res.status}` };
      const data = await res.json().catch(() => ({}));
      return { ok: true, version: data?.version || 'unknown' };
    } catch (err) {
      return { ok: false, error: err?.name === 'AbortError' ? 'timeout' : (err?.message || 'network') };
    }
  }

  globalThis.AgentWatchLLM = {
    classify,
    ping,
    getDefaults: () => ({ ...DEFAULTS }),
    VALID_CATEGORIES: Array.from(VALID_CATEGORIES),
  };
})();
