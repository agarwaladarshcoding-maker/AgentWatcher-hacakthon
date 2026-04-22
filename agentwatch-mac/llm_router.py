"""
AgentWatch macOS — llm_router.py

Thread-safe, non-blocking local LLM classifier.

Design contract (PHASE-3 requirements):
  • Runs via stdlib only (urllib) — no extra PyPI deps required.
  • classify() is an async coroutine and NEVER blocks the asyncio loop:
    the synchronous HTTP call is dispatched to the default thread pool
    through asyncio.to_thread. That leaves NSRunLoop (rumps / AppKit)
    and the asyncio event loop (websockets server) completely free.
  • Any failure (timeout, connection refused, bad JSON, disabled) falls
    back to the heuristic derived from the raw eventType — so the
    notification pipeline is never blocked or delayed beyond `timeout_ms`.

Public API:
    cfg = LLMConfig(enabled=True, endpoint="http://localhost:11434",
                    model="llama3.2:1b", timeout_ms=1500)
    result = await classify(event_dict, cfg)
    # -> LLMResult(category=..., needs_reply=..., reason=..., source=...)
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional
from urllib import request as _urlreq, error as _urlerr


log = logging.getLogger("agentwatch.llm")

VALID_CATEGORIES = {"ACTION_REQUIRED", "INFORMATION", "PENDING", "COMPLETED"}

SYSTEM_PROMPT = (
    "You are an event router for an AI-agent monitor. Given a short snapshot "
    "of what an AI assistant just produced, pick exactly ONE category from:\n"
    "  - ACTION_REQUIRED: the assistant asks a question, needs permission, "
    "is stuck, errored out, or explicitly waits for input.\n"
    "  - INFORMATION: the assistant delivered an informational answer; no user action needed.\n"
    "  - PENDING: the assistant is mid-task or partially done, waiting on external step.\n"
    "  - COMPLETED: the assistant finished a task successfully, no follow-up required.\n"
    "Also decide needsReply (true only if the user realistically needs to type "
    "something now). Return ONLY compact JSON with keys category, needsReply, reason."
)


@dataclass(frozen=True)
class LLMConfig:
    enabled: bool = False
    endpoint: str = "http://localhost:11434"
    model: str = "llama3.2:1b"
    timeout_ms: int = 1500


@dataclass(frozen=True)
class LLMResult:
    category: str
    needs_reply: bool
    reason: str
    source: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── Heuristic fallback ────────────────────────────────────────────────────────
def _heuristic(event_type: str) -> LLMResult:
    et = (event_type or "").upper()
    if et in ("DECISION", "BLOCKED", "PERMISSION"):
        return LLMResult("ACTION_REQUIRED", True,  f"heuristic:{et}", "heuristic")
    if et == "ERROR":
        return LLMResult("ACTION_REQUIRED", True,  "heuristic:ERROR", "heuristic")
    if et == "COMPLETED":
        return LLMResult("COMPLETED",       False, "heuristic:COMPLETED", "heuristic")
    return LLMResult("INFORMATION", False, f"heuristic:{et or 'unknown'}", "heuristic")


# ── Blocking Ollama call (runs inside asyncio.to_thread) ──────────────────────
def _blocking_call_ollama(cfg: LLMConfig, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    url = cfg.endpoint.rstrip("/") + "/api/generate"
    snippet = (event.get("messageText") or event.get("messageSnippet") or "")[:1200]
    user_payload = {
        "siteName":      event.get("siteName", ""),
        "rawEventType":  event.get("eventType", ""),
        "durationMs":    event.get("durationMs", 0),
        "responseLength": event.get("responseLength", 0),
        "messageSnippet": snippet,
    }
    body = {
        "model":  cfg.model,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0, "num_predict": 80},
        "system": SYSTEM_PROMPT,
        "prompt": (
            "Event:\n"
            + json.dumps(user_payload, indent=2)
            + "\n\nRespond with JSON: "
              '{"category":"...","needsReply":true|false,"reason":"..."}'
        ),
    }
    req = _urlreq.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    timeout = max(0.2, cfg.timeout_ms / 1000.0)
    with _urlreq.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _parse_llm_response(wrapper: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(wrapper, dict):
        return None
    text = wrapper.get("response", "")
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None


def _normalize(parsed: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(parsed, dict):
        return None
    cat = str(parsed.get("category", "")).upper().strip()
    if cat not in VALID_CATEGORIES:
        return None
    return {
        "category":   cat,
        "needsReply": bool(parsed.get("needsReply") or cat == "ACTION_REQUIRED"),
        "reason":     str(parsed.get("reason", ""))[:200],
    }


# ── Public async API ──────────────────────────────────────────────────────────
async def classify(event: Dict[str, Any], cfg: LLMConfig) -> LLMResult:
    """Categorize an AI-agent event. Guaranteed never to block longer than
    `cfg.timeout_ms` + a small scheduling margin.
    """
    fallback = _heuristic(event.get("eventType", ""))

    if not cfg.enabled:
        return LLMResult(fallback.category, fallback.needs_reply, fallback.reason, "heuristic-disabled")

    try:
        wrapper = await asyncio.wait_for(
            asyncio.to_thread(_blocking_call_ollama, cfg, event),
            timeout=(cfg.timeout_ms / 1000.0) + 0.3,
        )
    except asyncio.TimeoutError:
        return LLMResult(fallback.category, fallback.needs_reply, fallback.reason, "fallback-timeout")
    except (_urlerr.URLError, ConnectionError, OSError) as exc:
        log.debug("llm_router network error: %r", exc)
        return LLMResult(fallback.category, fallback.needs_reply, fallback.reason, "fallback-network")
    except Exception as exc:  # pragma: no cover — defensive
        log.warning("llm_router unexpected error: %r", exc)
        return LLMResult(fallback.category, fallback.needs_reply, fallback.reason, "fallback-exception")

    parsed = _normalize(_parse_llm_response(wrapper))
    if not parsed:
        return LLMResult(fallback.category, fallback.needs_reply, fallback.reason, "fallback-bad-json")

    return LLMResult(
        category=parsed["category"],
        needs_reply=parsed["needsReply"],
        reason=parsed["reason"],
        source=f"llm:{cfg.model}",
    )


async def ping(cfg: LLMConfig) -> Dict[str, Any]:
    """Non-blocking health-check of the Ollama endpoint."""
    def _do() -> Dict[str, Any]:
        url = cfg.endpoint.rstrip("/") + "/api/version"
        req = _urlreq.Request(url, method="GET")
        with _urlreq.urlopen(req, timeout=max(0.2, cfg.timeout_ms / 1000.0)) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        try:
            return {"ok": True, "version": json.loads(raw).get("version", "unknown")}
        except json.JSONDecodeError:
            return {"ok": True, "version": "unknown"}

    try:
        return await asyncio.wait_for(asyncio.to_thread(_do), timeout=(cfg.timeout_ms / 1000.0) + 0.3)
    except asyncio.TimeoutError:
        return {"ok": False, "error": "timeout"}
    except Exception as exc:  # pragma: no cover
        return {"ok": False, "error": str(exc)}
