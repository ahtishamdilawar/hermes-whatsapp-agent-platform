"""Async client for Meta's WhatsApp Agent Platform API (``/agent/v1``).

Pure transport: no Hermes imports, so the protocol layer can be tested and
reasoned about against the developer manual alone (Version 1, 2026-08-25).

Error classification follows the manual's delivery semantics:

* ``NotSent``   — the request certainly did not take effect (connect failure,
  4xx, 429, 503/131016). Safe to retry when ``retryable``.
* ``Ambiguous`` — it may or may not have taken effect (HTTP 500, connection
  reset or read timeout after the request was written). The client never
  retries these itself; the adapter reports them to Hermes, whose delivery
  ledger re-sends the reply later with a "may be a duplicate" marker.
"""

from __future__ import annotations

import asyncio
import collections
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

API_BASE = "https://api.whatsapp.com/agent/v1"
MESSAGING_PRODUCT = "whatsapp"
MAX_TEXT_LENGTH = 4096
MAX_CAPTION_LENGTH = 1024
MAX_POLL_TIMEOUT = 25
MAX_POLL_LIMIT = 100

# Documented ``error.code`` values.
CODE_INTERNAL = 2
CODE_INVALID_PARAMETER = 100  # also: invalid token (HTTP 400), unknown media, length cap
CODE_AUTH_HEADER = 190
CODE_RATE_LIMITED = 130429
CODE_NOT_CREATOR = 131005
CODE_BAD_FIELD = 131009
CODE_NOT_ACCEPTED = 131016
CODE_MEDIA_REJECTED = 131053
CODE_POLL_REPLACED = 1752041

# Per agent, rolling 60 s, counted per method.
# ``updates`` keeps one poll of headroom under Meta's 15/min: local stamps are taken before
# network latency, and a new adapter starts with an empty window while Meta's does not.
RATE_LIMITS = {"messages": 12, "statuses": 12, "updates": 14}


class AgentPlatformError(Exception):
    """Base error. ``str()`` never contains the API key or a response body."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: int | None = None,
        fbtrace_id: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.fbtrace_id = fbtrace_id
        self.retry_after = retry_after

    def describe(self) -> str:
        """Log-safe summary: HTTP status, Meta error code and trace id only."""
        parts = [str(self)]
        if self.status is not None:
            parts.append(f"http={self.status}")
        if self.code is not None:
            parts.append(f"code={self.code}")
        if self.fbtrace_id:
            parts.append(f"fbtrace_id={self.fbtrace_id}")
        return " ".join(parts)


class NotSent(AgentPlatformError):
    """The request definitely had no effect."""

    retryable = False


class Retryable(NotSent):
    """Not sent, and the manual says to retry after backing off."""

    retryable = True


class AuthError(NotSent):
    """Missing, malformed or invalid API key (401/190 or 400/100 on a token)."""


class NotCreatorError(NotSent):
    """403/131005: the recipient (or message author) is not the agent's creator."""


class RateLimited(Retryable):
    """429/130429 for this method's per-agent budget."""


class PollConflict(NotSent):
    """409/1752041: a newer ``GET /updates`` for this agent replaced this one."""


class Ambiguous(AgentPlatformError):
    """The request may have taken effect (500, reset, read timeout)."""


class MalformedResponse(AgentPlatformError):
    """A 2xx response whose body does not match the documented schema."""


class RateWindow:
    """Rolling-window limiter for one endpoint's per-minute budget.

    Unlike fixed spacing, it lets a short burst through (e.g. a 3-chunk reply)
    and only waits when the window is actually full.
    """

    def __init__(self, limit: int, window: float = 60.0, *, clock=time.monotonic) -> None:
        self.limit = limit
        self.window = window
        self._clock = clock
        self._stamps: collections.deque[float] = collections.deque()
        self._lock = asyncio.Lock()

    def _prune(self, now: float) -> None:
        while self._stamps and now - self._stamps[0] >= self.window:
            self._stamps.popleft()

    def remaining(self) -> int:
        self._prune(self._clock())
        return max(0, self.limit - len(self._stamps))

    def delay(self) -> float:
        """Seconds until a slot frees up (0 when one is free now)."""
        now = self._clock()
        self._prune(now)
        if len(self._stamps) < self.limit:
            return 0.0
        return max(0.0, self.window - (now - self._stamps[0]))

    def try_acquire(self) -> bool:
        """Reserve a slot without waiting."""
        now = self._clock()
        self._prune(now)
        if len(self._stamps) >= self.limit:
            return False
        self._stamps.append(now)
        return True

    async def acquire(self) -> None:
        """Wait for, then reserve, a slot (reserved before any network I/O)."""
        async with self._lock:
            while True:
                wait = self.delay()
                if wait <= 0:
                    self._stamps.append(self._clock())
                    return
                await asyncio.sleep(wait)

    def penalize(self, seconds: float) -> None:
        """After a server 429, treat the window as full for ``seconds`` (at most one window)."""
        now = self._clock()
        fill_at = now - self.window + min(self.window, max(0.0, seconds))
        self._stamps = collections.deque([fill_at] * self.limit)


@dataclass
class Updates:
    """One ``GET /updates`` page. ``next_offset`` is None for an empty (204) poll."""

    next_offset: int | None
    messages: list[dict[str, Any]] = field(default_factory=list)
    statuses: list[dict[str, Any]] = field(default_factory=list)
    contacts: list[dict[str, Any]] = field(default_factory=list)


def _error_fields(response: httpx.Response) -> tuple[int | None, str | None]:
    try:
        err = response.json().get("error") or {}
    except (ValueError, AttributeError, TypeError):
        return None, None
    if not isinstance(err, dict):
        return None, None
    code = err.get("code")
    trace = err.get("fbtrace_id")
    return (code if isinstance(code, int) else None), (trace if isinstance(trace, str) else None)


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def classify_response(response: httpx.Response, action: str) -> AgentPlatformError:
    """Map a non-2xx response to the manual's error semantics."""
    status = response.status_code
    code, trace = _error_fields(response)
    kw = {"status": status, "code": code, "fbtrace_id": trace}
    if status == 401 or code == CODE_AUTH_HEADER:
        return AuthError(f"{action}: missing or malformed Authorization header", **kw)
    if status == 400 and code == CODE_INVALID_PARAMETER and action == "updates":
        # On /updates the only parameters are integers; a code-100 400 is the token.
        return AuthError(f"{action}: API key rejected", **kw)
    if status == 403 or code == CODE_NOT_CREATOR:
        return NotCreatorError(f"{action}: forbidden, not the agent's creator", **kw)
    if status == 409 or code == CODE_POLL_REPLACED:
        return PollConflict(f"{action}: a newer poll for this agent replaced this one", **kw)
    if status == 429 or code == CODE_RATE_LIMITED:
        return RateLimited(f"{action}: rate limited", retry_after=_retry_after(response), **kw)
    if status == 503:
        # 503/131016 on /messages and any 503 on /statuses: not accepted, resend.
        return Retryable(f"{action}: not accepted, retry later", retry_after=_retry_after(response), **kw)
    if status >= 500:
        return Ambiguous(f"{action}: server error, outcome unknown", **kw)
    if status == 400 and code == CODE_INVALID_PARAMETER:
        return NotSent(f"{action}: invalid parameter or API key", **kw)
    return NotSent(f"{action}: rejected", **kw)


class AgentPlatformClient:
    """Thin async wrapper around the six documented endpoints."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = API_BASE,
        http: httpx.AsyncClient | None = None,
        send_timeout: float = 60.0,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._send_timeout = send_timeout
        self._owns_http = http is None
        # Read timeout is set per request; long polls need poll timeout + margin.
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
        self.limits = {name: RateWindow(limit) for name, limit in RATE_LIMITS.items()}

    @property
    def closed(self) -> bool:
        return self._http.is_closed

    async def aclose(self) -> None:
        if self._owns_http and not self._http.is_closed:
            await self._http.aclose()

    async def _request(self, method: str, path: str, action: str, *, write: bool, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._http.request(method, f"{self._base}{path}", headers=self._headers, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise Retryable(f"{action}: could not connect ({type(exc).__name__})") from None
        except httpx.TimeoutException as exc:
            if write:
                raise Ambiguous(f"{action}: timed out, outcome unknown ({type(exc).__name__})") from None
            raise Retryable(f"{action}: timed out ({type(exc).__name__})") from None
        except httpx.HTTPError as exc:  # transport resets, decoding errors after the request was sent
            if write:
                raise Ambiguous(f"{action}: connection lost, outcome unknown ({type(exc).__name__})") from None
            raise Retryable(f"{action}: connection lost ({type(exc).__name__})") from None
        except RuntimeError:
            if self._http.is_closed:  # disconnect() closed the client while this call waited
                raise Retryable(f"{action}: client closed") from None
            raise
        if response.status_code >= 400:
            raise classify_response(response, action)
        return response

    # ------------------------------------------------------------------ updates
    async def get_updates(self, offset: int | None, *, timeout: int = 20, limit: int = 50) -> Updates:
        """Long-poll for updates. ``offset=None`` starts at the head (skips backlog)."""
        timeout = max(0, min(MAX_POLL_TIMEOUT, int(timeout)))
        params: dict[str, int] = {"timeout": timeout, "limit": max(1, min(MAX_POLL_LIMIT, int(limit)))}
        if offset is not None:
            params["offset"] = offset
        await self.limits["updates"].acquire()
        response = await self._request(
            "GET",
            "/updates",
            "updates",
            write=False,
            params=params,
            timeout=httpx.Timeout(timeout + 15.0, connect=10.0),
        )
        if response.status_code == 204:
            return Updates(next_offset=None)
        return parse_updates(response)

    # ----------------------------------------------------------------- messages
    async def send_message(self, payload: dict[str, Any]) -> str:
        """POST /messages; returns the outbound ``wamid``."""
        body = {"messaging_product": MESSAGING_PRODUCT, **payload}
        await self.limits["messages"].acquire()
        try:
            response = await self._request(
                "POST",
                "/messages",
                "send",
                write=True,
                json=body,
                timeout=httpx.Timeout(self._send_timeout, connect=10.0),
            )
        except RateLimited as exc:
            self.limits["messages"].penalize(10.0 if exc.retry_after is None else exc.retry_after)
            raise
        try:
            message_id = response.json()["messages"][0]["id"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise MalformedResponse("send: 2xx without messages[0].id", status=response.status_code) from None
        if not isinstance(message_id, str) or not message_id:
            raise MalformedResponse("send: 2xx without messages[0].id", status=response.status_code)
        return message_id

    async def send_text(self, to: str, body: str, *, reply_to: str | None = None, preview_url: bool = False) -> str:
        payload: dict[str, Any] = {"to": to, "type": "text", "text": {"body": body}}
        if preview_url:
            payload["text"]["preview_url"] = True
        if reply_to:
            payload["context"] = {"message_id": reply_to}
        return await self.send_message(payload)

    # ----------------------------------------------------------------- statuses
    async def mark_read(self, message_id: str, *, typing: bool = False) -> None:
        """POST /statuses: read receipt, optionally with the 25 s typing indicator."""
        body: dict[str, Any] = {
            "messaging_product": MESSAGING_PRODUCT,
            "status": "read",
            "message_id": message_id,
        }
        if typing:
            body["typing_indicator"] = {"type": "text"}
        if not self.limits["statuses"].try_acquire():
            raise RateLimited("statuses: local budget exhausted")
        response = await self._request("POST", "/statuses", "statuses", write=True, json=body)
        try:
            ok = response.json().get("success") is True
        except (ValueError, AttributeError):
            ok = False
        if not ok:
            raise MalformedResponse("statuses: 2xx without success=true", status=response.status_code)


def parse_updates(response: httpx.Response) -> Updates:
    """Validate a 200 ``GET /updates`` body against the documented envelope."""
    try:
        payload = response.json()
    except ValueError:
        raise MalformedResponse("updates: body is not JSON", status=response.status_code) from None
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_agent_platform":
        raise MalformedResponse("updates: unexpected object type", status=response.status_code)
    next_offset = payload.get("next_offset")
    if type(next_offset) is not int:
        raise MalformedResponse("updates: missing integer next_offset", status=response.status_code)
    result = Updates(next_offset=next_offset)
    entries = payload.get("entry")
    if not isinstance(entries, list):
        raise MalformedResponse("updates: entry is not a list", status=response.status_code)
    for entry in entries:
        changes = entry.get("changes") if isinstance(entry, dict) else None
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict) or change.get("field") != "messages":
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            for key, bucket in (
                ("messages", result.messages),
                ("statuses", result.statuses),
                ("contacts", result.contacts),
            ):
                items = value.get(key)
                if isinstance(items, list):
                    bucket.extend(item for item in items if isinstance(item, dict))
    return result
