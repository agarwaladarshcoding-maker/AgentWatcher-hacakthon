/**
 * AgentWatch - fetch_patcher.js
 * Runs in MAIN world — patches fetch + XHR to detect AI streaming responses.
 * Communicates with content.js (ISOLATED world) via window.postMessage().
 * (CustomEvents do NOT cross the MAIN→ISOLATED world boundary.)
 */
(function () {
  'use strict';

  if (window.__agentWatchPatched) return;
  window.__agentWatchPatched = true;

  // AI API URL patterns that indicate a streaming response is in progress
  const AI_STREAM_PATTERNS = [
    '/backend-api/conversation',    // ChatGPT
    '/api/append_message',          // Claude
    '/api/chat_conversation',       // Claude (alt)
    'streamGenerateContent',        // Gemini
    '/api/ask',                     // Perplexity
    '/api/query',                   // Perplexity (alt)
    '/chat/completions',            // OpenAI API / many sites
    '/v1/messages',                 // Anthropic API
    '/api/generate',                // Various
    '/api/stream',                  // Various
    '/api/chat',                    // Various
    '/completions',                 // OpenAI style
    'chat-messages',                // Cohere
    '/api/converse',                // Character.ai
    '/talk',                        // Pi.ai
    '/inference',                   // Various
    'generate_stream',              // Various
  ];

  // Minimum response size and duration to consider it an AI stream
  const MIN_STREAM_BYTES = 50;
  const MIN_STREAM_MS = 800;

  function isAIStreamURL(url) {
    if (typeof url !== 'string') return false;
    return AI_STREAM_PATTERNS.some(p => url.includes(p));
  }

  function dispatch(type, detail) {
    window.postMessage({ __aw: true, type, ...(detail || {}) }, '*');
  }

  // ─── Patch window.fetch ────────────────────────────────────────────────────
  const _origFetch = window.fetch;
  window.fetch = async function (...args) {
    const reqUrl = typeof args[0] === 'string' ? args[0] : args[0]?.url || '';
    
    // Suppress known/noisy CSP violations from Gemini's ad tracking
    // Gemini's own CSP blocks its own ad trackers, filling the console with errors
    if (reqUrl.includes('googleadservices.com/pagead')) {
      return new Response(null, { status: 200, statusText: 'OK (Blocked by AgentWatch)' });
    }

    const isAI = isAIStreamURL(reqUrl);

    const startTime = Date.now();
    let response;
    try {
      response = await _origFetch.apply(this, args);
    } catch (err) {
      if (isAI) dispatch('aw_fetch_error', {});
      throw err;
    }

    if (!isAI) return response;

    const contentType = response.headers.get('content-type') || '';
    const isStream = contentType.includes('event-stream') ||
      contentType.includes('stream') ||
      contentType.includes('octet-stream');

    // Fire "start" event
    dispatch('aw_fetch_start', { url: reqUrl });

    // Clone response so original body is untouched
    const clone = response.clone();
    const reader = clone.body?.getReader();

    if (reader) {
      let totalBytes = 0;
      const pump = async () => {
        try {
          while (true) {
            const { done, value } = await reader.read();
            if (done) {
              const elapsed = Date.now() - startTime;
              if (totalBytes >= MIN_STREAM_BYTES && elapsed >= MIN_STREAM_MS) {
                dispatch('aw_fetch_done', { bytes: totalBytes, ms: elapsed, url: reqUrl });
              }
              break;
            }
            totalBytes += value?.length || 0;
          }
        } catch {
          const elapsed = Date.now() - startTime;
          if (totalBytes >= MIN_STREAM_BYTES && elapsed >= MIN_STREAM_MS) {
            dispatch('aw_fetch_done', { bytes: totalBytes, ms: elapsed, url: reqUrl, error: true });
          }
        }
      };
      pump(); // async — doesn't block original response
    } else {
      // Fallback: fire done after a tick if no body reader
      setTimeout(() => dispatch('aw_fetch_done', { bytes: 0, ms: Date.now() - startTime, url: reqUrl }), 100);
    }

    return response;
  };

  // ─── Patch XMLHttpRequest (fallback for older-style integrations) ──────────
  const _origOpen = XMLHttpRequest.prototype.open;
  const _origSend = XMLHttpRequest.prototype.send;

  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    this._aw_url = typeof url === 'string' ? url : '';
    this._aw_is_ai = isAIStreamURL(this._aw_url);
    this._aw_start = Date.now();
    return _origOpen.apply(this, [method, url, ...rest]);
  };

  XMLHttpRequest.prototype.send = function (...args) {
    if (this._aw_is_ai) {
      this.addEventListener('loadstart', () => dispatch('aw_fetch_start', { url: this._aw_url }));
      this.addEventListener('load', () => {
        const elapsed = Date.now() - this._aw_start;
        const bytes = this.responseText?.length || 0;
        if (bytes >= MIN_STREAM_BYTES && elapsed >= MIN_STREAM_MS) {
          dispatch('aw_fetch_done', { bytes, ms: elapsed, url: this._aw_url });
        }
      });
      this.addEventListener('error', () => dispatch('aw_fetch_error', { url: this._aw_url }));
    }
    return _origSend.apply(this, args);
  };

  // ─── Patch WebSocket (for sites like Perplexity, Character.ai) ────────────
  const _OrigWS = window.WebSocket;
  window.WebSocket = function (url, protocols) {
    const ws = protocols ? new _OrigWS(url, protocols) : new _OrigWS(url);

    if (typeof url === 'string' && isAIStreamURL(url)) {
      const startTime = Date.now();
      let msgCount = 0;
      let isOpen = false;

      ws.addEventListener('open', () => {
        isOpen = true;
        dispatch('aw_fetch_start', { url, ws: true });
      });

      ws.addEventListener('message', () => {
        msgCount++;
      });

      ws.addEventListener('close', () => {
        if (isOpen && msgCount > 2) {
          const elapsed = Date.now() - startTime;
          if (elapsed >= MIN_STREAM_MS) {
            dispatch('aw_fetch_done', { bytes: msgCount * 50, ms: elapsed, url, ws: true });
          }
        }
      });
    }

    return ws;
  };
  window.WebSocket.prototype = _OrigWS.prototype;
  window.WebSocket.CONNECTING = _OrigWS.CONNECTING;
  window.WebSocket.OPEN = _OrigWS.OPEN;
  window.WebSocket.CLOSING = _OrigWS.CLOSING;
  window.WebSocket.CLOSED = _OrigWS.CLOSED;

  // ─── SPA navigation tracking ─────────────────────────────────────────────
  // Patch history.pushState / replaceState so content.js (ISOLATED world)
  // is informed whenever a SPA (Gemini, ChatGPT, Claude, etc.) switches
  // context without a full page reload.
  const _pushState    = history.pushState;
  const _replaceState = history.replaceState;

  function emitUrlChange(source) {
    window.postMessage({
      __aw: true,
      type: 'url_changed',
      url: window.location.href,
      pathname: window.location.pathname,
      hash: window.location.hash,
      source,
      t: Date.now(),
    }, '*');
  }

  history.pushState = function (...args) {
    const r = _pushState.apply(this, args);
    // Defer by a microtask so the URL has actually changed before we read it
    Promise.resolve().then(() => emitUrlChange('pushState'));
    return r;
  };

  history.replaceState = function (...args) {
    const r = _replaceState.apply(this, args);
    Promise.resolve().then(() => emitUrlChange('replaceState'));
    return r;
  };

  window.addEventListener('popstate',   () => emitUrlChange('popstate'));
  window.addEventListener('hashchange', () => emitUrlChange('hashchange'));

  // Svelte router (HuggingFace Chat) — custom event, must relay via postMessage
  window.addEventListener('svelte:navigate', () => emitUrlChange('svelte:navigate'));

})();
