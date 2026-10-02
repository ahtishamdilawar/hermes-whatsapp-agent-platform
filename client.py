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

Media follows the same split, with the mappings recorded live by the P0 probes
(``MediaRejected``, ``MediaGone`` and the ``MediaDownloadError`` family). No
exception message ever contains a media URL, media id, filename or caption.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import collections
import hashlib
import hmac
import re
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
# Media downloads have no budget: live, 12 downloads in a minute did not count against any (P0, V-02).
RATE_LIMITS = {
    "messages": 12,
    "statuses": 12,
    "updates": 14,
    "media_upload": 12,
    "media_get": 12,
    "media_delete": 12,
}

MEDIA_KINDS = ("image", "video", "audio", "document", "sticker")
# The only host ``GET /media`` urls were seen on (P0, V-01). A non-default ``base_url`` adds its own host.
DEFAULT_MEDIA_HOSTS = ("lookaside.fbsbx.com",)
DOWNLOAD_TIMEOUT = httpx.Timeout(45.0, connect=10.0)
# Stricter than ``[A-Za-z0-9._-]{1,128}``: a leading alphanumeric rules out "." and "..", which httpx would
# normalise out of the path (``/media/..`` becomes ``/agent/v1``). Meta's ids are 36-character UUIDs.
_MEDIA_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
# ``type/subtype`` with optional ``; key=value`` parameters; no CR/LF or quotes can reach a multipart header.
_MIME_RE = re.compile(r"[\w!#$&^.+-]+/[\w!#$&^.+-]+(?:\s*;\s*[\w!#$&^.+-]+=[\w!#$&^.+-]+)*", re.ASCII)
_MAX_MEDIA_URL = 4096
_ERROR_BODY_CAP = 16 * 1024


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


class MediaRejected(NotSent):
    """Meta refused the media or its fields: 131053 on upload, 131009 other than "No media found",
    or code 100 on a media send (e.g. a caption over 1024 UTF-16 units). Never retry.

    ``details`` is Meta's ``error_data.details`` and ``error_message`` Meta's ``error.message`` (either may be
    None; a live code-100 error names the bad field only in ``message``, e.g. ``"caption"``). Both can quote a
    media id or MIME type, so neither is part of ``str()``/``repr()``/``describe()``; don't log them raw.
    """

    def __init__(
        self, message: str, *, details: str | None = None, error_message: str | None = None, **kw: Any
    ) -> None:
        super().__init__(message, **kw)
        self.details = details
        self.error_message = error_message


class MediaGone(NotSent):
    """The media id is unknown, expired or deleted (GET/DELETE code 100, download 404, send 131009 "No media
    found"). An outbound send can re-upload once."""


class MediaDownloadError(NotSent):
    """A media download failed for a reason that retrying the same url will not fix."""


class UntrustedMediaURL(MediaDownloadError):
    """The url is not https on port 443 to an allowlisted host without userinfo. No request was made."""


class MediaTooLarge(MediaDownloadError):
    """The declared or streamed size is over the caller's cap."""


class MediaIntegrityError(MediaDownloadError):
    """The body does not match the expected sha256 or length."""


class MediaRedirected(MediaDownloadError):
    """The media host answered with a redirect; it is never followed."""


@dataclass(frozen=True)
class MediaInfo:
    """``GET /media/<id>``. ``id`` and ``url`` stay out of ``repr()`` so the object is safe to log."""

    id: str = field(repr=False)
    url: str = field(repr=False)
    mime_type: str | None
    sha256: str | None
    file_size: int | None


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
        """After a server 429, treat the window as full for all of ``seconds``."""
        now = self._clock()
        # Future stamps keep the window full until even a multi-window penalty expires.
        fill_at = now - self.window + max(0.0, seconds)
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
    if action == "updates" and (status == 409 or code == CODE_POLL_REPLACED):
        # Only a poll can be replaced by a newer poll; a 409 anywhere else is a plain rejection.
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


def _error_details(response: httpx.Response) -> str | None:
    try:
        details = response.json()["error"]["error_data"]["details"]
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    return details if isinstance(details, str) else None


def _error_message(response: httpx.Response) -> str | None:
    """Meta's ``error.message``, capped. Kept only on ``MediaRejected`` for classification; never logged."""
    try:
        message = response.json()["error"]["message"]
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    return message[:1024] if isinstance(message, str) else None


def classify_media_response(response: httpx.Response, action: str) -> AgentPlatformError:
    """``classify_response`` plus the media mappings recorded live (P0 probes).

    Auth, not-creator, rate-limit and 5xx errors keep their meaning; only plain rejections are refined.
    ``action`` is ``send`` (a media message), ``media_upload``, ``media_get`` or ``media_delete``.
    """
    err = classify_response(response, action)
    if type(err) is not NotSent:
        return err
    status, code = err.status, err.code
    kw = {"status": status, "code": code, "fbtrace_id": err.fbtrace_id}
    details = _error_details(response)
    if action == "send":
        if code == CODE_BAD_FIELD and (details or "").strip().lower().startswith("no media found"):
            return MediaGone(f"{action}: media not found or expired", **kw)
        if code in (CODE_INVALID_PARAMETER, CODE_BAD_FIELD, CODE_MEDIA_REJECTED):
            # Live: 100 names a bad media field (e.g. the caption). The caller treats it as a stale quote only
            # when details or message name the quote (media_outbound.is_stale_media_quote).
            return MediaRejected(
                f"{action}: media or its fields rejected", details=details, error_message=_error_message(response), **kw
            )
    elif action == "media_upload":
        if code in (CODE_BAD_FIELD, CODE_MEDIA_REJECTED) or status == 413:
            return MediaRejected(
                f"{action}: media rejected", details=details, error_message=_error_message(response), **kw
            )
    elif action in ("media_get", "media_delete"):
        if code == CODE_INVALID_PARAMETER or status in (404, 410):
            return MediaGone(f"{action}: media not found or expired", **kw)
    return err


def check_media_id(media_id: Any, action: str) -> str:
    """Validate a media id before it goes into a URL path (``ValueError`` otherwise, with no id in the text)."""
    if not isinstance(media_id, str) or not _MEDIA_ID_RE.fullmatch(media_id):
        raise ValueError(f"{action}: invalid media id")
    return media_id


def parse_sha256(value: str) -> bytes:
    """Hex (``GET /media``) or Base64 (inbound updates) sha256 -> the 32-byte digest; ``ValueError`` otherwise."""
    text = value.strip() if isinstance(value, str) else ""
    if len(text) == 64:
        try:
            return bytes.fromhex(text)
        except ValueError:
            pass
    standard = text.replace("-", "+").replace("_", "/")  # also accept the URL-safe alphabet
    try:
        digest = base64.b64decode(standard + "=" * (-len(standard) % 4), validate=True)
    except (binascii.Error, ValueError):
        digest = b""
    if len(digest) != 32:
        raise ValueError("sha256 is neither hex nor Base64 of 32 bytes")
    return digest


class AgentPlatformClient:
    """Thin async wrapper around the documented endpoints, plus the media host download."""

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
        hosts = set(DEFAULT_MEDIA_HOSTS)
        if self._base != API_BASE:
            # The override is already trusted with the key; it adds exactly its own host, nothing wider.
            override = httpx.URL(self._base).host
            if override:
                hosts.add(override.lower())
        self.media_hosts = frozenset(hosts)

    @property
    def closed(self) -> bool:
        return self._http.is_closed

    async def aclose(self) -> None:
        if self._owns_http and not self._http.is_closed:
            await self._http.aclose()

    async def _request(
        self, method: str, path: str, action: str, *, write: bool, classify=classify_response, **kwargs: Any
    ) -> httpx.Response:
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
            raise classify(response, action)
        return response

    async def _budgeted(self, window: str, method: str, path: str, action: str, **kwargs: Any) -> httpx.Response:
        """``_request`` under one ``RateWindow``; a server 429 marks that window full."""
        await self.limits[window].acquire()
        try:
            return await self._request(method, path, action, **kwargs)
        except RateLimited as exc:
            self.limits[window].penalize(10.0 if exc.retry_after is None else exc.retry_after)
            raise

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
        return await self._post_message(payload, classify_response)

    async def _post_message(self, payload: dict[str, Any], classify) -> str:
        body = {"messaging_product": MESSAGING_PRODUCT, **payload}
        response = await self._budgeted(
            "messages",
            "POST",
            "/messages",
            "send",
            write=True,
            classify=classify,
            json=body,
            timeout=httpx.Timeout(self._send_timeout, connect=10.0),
        )
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

    # -------------------------------------------------------------------- media
    async def upload_media(self, data: bytes, *, mime: str, filename: str) -> str:
        """POST /media (multipart ``messaging_product``, ``type``, ``file``); returns the media id.

        A timeout or reset after the body was written raises ``Ambiguous`` (a stray upload only costs storage).
        """
        action = "media_upload"
        mime = mime.strip() if isinstance(mime, str) else ""
        if not _MIME_RE.fullmatch(mime):
            raise ValueError(f"{action}: invalid MIME type")
        if not isinstance(filename, str) or not filename:
            raise ValueError(f"{action}: a filename is required")
        response = await self._budgeted(
            "media_upload",
            "POST",
            "/media",
            action,
            write=True,
            classify=classify_media_response,
            data={"messaging_product": MESSAGING_PRODUCT, "type": mime},
            files={"file": (filename, bytes(data), mime)},
            timeout=httpx.Timeout(self._send_timeout, connect=10.0),
        )
        try:
            media_id = response.json()["id"]
        except (ValueError, KeyError, TypeError):
            raise MalformedResponse(f"{action}: 2xx without id", status=response.status_code) from None
        if not isinstance(media_id, str) or not media_id:
            raise MalformedResponse(f"{action}: 2xx without id", status=response.status_code)
        return media_id

    async def get_media(self, media_id: str) -> MediaInfo:
        """GET /media/<id>: the download url, MIME type, hex sha256 and size. The id is validated first."""
        action = "media_get"
        check_media_id(media_id, action)
        response = await self._budgeted(
            "media_get", "GET", f"/media/{media_id}", action, write=False, classify=classify_media_response
        )
        try:
            payload = response.json()
        except ValueError:
            raise MalformedResponse(f"{action}: body is not JSON", status=response.status_code) from None
        url = payload.get("url") if isinstance(payload, dict) else None
        if not isinstance(url, str) or not url:
            raise MalformedResponse(f"{action}: 2xx without url", status=response.status_code)
        mime, sha, size = payload.get("mime_type"), payload.get("sha256"), payload.get("file_size")
        return MediaInfo(
            id=media_id,
            url=url,
            mime_type=mime if isinstance(mime, str) and mime else None,
            sha256=sha if isinstance(sha, str) and sha else None,
            file_size=size if type(size) is int and size >= 0 else None,
        )

    async def delete_media(self, media_id: str) -> None:
        """DELETE /media/<id> (tests and operations; the adapter never deletes). The id is validated first."""
        action = "media_delete"
        check_media_id(media_id, action)
        response = await self._budgeted(
            "media_delete", "DELETE", f"/media/{media_id}", action, write=True, classify=classify_media_response
        )
        try:
            ok = response.json().get("success") is True
        except (ValueError, AttributeError):
            ok = False
        if not ok:
            raise MalformedResponse(f"{action}: 2xx without success=true", status=response.status_code)

    async def send_media(
        self,
        to: str,
        kind: str,
        media_id: str,
        *,
        caption: str | None = None,
        filename: str | None = None,
        reply_to: str | None = None,
    ) -> str:
        """POST /messages with an uploaded media id; returns the ``wamid``.

        Same budget and taxonomy as ``send_message``, except that 100/131009/131053 become ``MediaRejected``, and
        131009 "No media found" becomes ``MediaGone``. Captions are not validated here (the caller's job).
        """
        if kind not in MEDIA_KINDS:
            raise ValueError("send: unknown media kind")
        if not isinstance(media_id, str) or not media_id:
            raise ValueError("send: a media id is required")
        obj: dict[str, Any] = {"id": media_id}
        if caption:
            obj["caption"] = caption
        if filename:
            obj["filename"] = filename
        payload: dict[str, Any] = {"to": to, "type": kind, kind: obj}
        if reply_to:
            payload["context"] = {"message_id": reply_to}
        return await self._post_message(payload, classify_media_response)

    def trusted_media_url(self, url: Any) -> httpx.URL:
        """Parse a media url and require https, port 443, no userinfo and an allowlisted host.

        Parsing uses httpx's own parser, the one the request is made with, so there is no parser differential.
        Raises ``UntrustedMediaURL`` (without the url or host in its text) otherwise.
        """
        action = "download"
        if (
            not isinstance(url, str)
            or not url
            or len(url) > _MAX_MEDIA_URL
            or not url.isascii()
            or any(ch <= " " or ch in "\\\x7f" for ch in url)
        ):
            raise UntrustedMediaURL(f"{action}: media url is malformed")
        try:
            parsed = httpx.URL(url)
        except (httpx.InvalidURL, ValueError, TypeError):
            raise UntrustedMediaURL(f"{action}: media url is malformed") from None
        if parsed.scheme != "https":
            raise UntrustedMediaURL(f"{action}: media url is not https")
        if parsed.userinfo:
            raise UntrustedMediaURL(f"{action}: media url carries credentials")
        if parsed.port not in (None, 443):
            raise UntrustedMediaURL(f"{action}: media url uses a non-standard port")
        if parsed.host not in self.media_hosts:
            raise UntrustedMediaURL(f"{action}: media host is not allowlisted")
        return parsed

    async def download_media(self, info: MediaInfo, *, max_bytes: int, verify_sha256: str | None = None) -> bytes:
        """Stream ``info.url`` into memory, bounded by ``max_bytes``, and verify it.

        The bearer is sent only after every check on the url, the cap and the expected digest passes; redirects
        are refused, not followed. Verifies the length against Content-Length and ``info.file_size`` and the
        sha256 against ``verify_sha256`` and ``info.sha256`` (hex or Base64), whichever are given. Reads are safe
        to repeat, so timeouts, resets and 5xx are ``Retryable``; a 401/403 is a ``MediaDownloadError``, never
        ``AuthError``.
        """
        action = "download"
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError(f"{action}: max_bytes must be a positive integer")
        url = self.trusted_media_url(info.url)
        digests = []
        for value in (verify_sha256, info.sha256):
            if value:
                try:
                    digests.append(parse_sha256(value))
                except ValueError:
                    raise MediaIntegrityError(f"{action}: expected sha256 is not hex or Base64") from None
        size = info.file_size
        expected = size if type(size) is int and size >= 0 else None
        if expected is not None and expected > max_bytes:
            raise MediaTooLarge(f"{action}: declared size is over the cap")
        headers = {**self._headers, "Accept-Encoding": "identity"}
        try:
            async with self._http.stream(
                "GET", url, headers=headers, timeout=DOWNLOAD_TIMEOUT, follow_redirects=False
            ) as response:
                if response.status_code != 200:
                    raise await _download_error(response)
                return await _read_media(response, max_bytes=max_bytes, expected=expected, digests=digests)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise Retryable(f"{action}: could not connect ({type(exc).__name__})") from None
        except httpx.TimeoutException as exc:
            raise Retryable(f"{action}: timed out ({type(exc).__name__})") from None
        except httpx.HTTPError as exc:  # reset or protocol error mid-body: a read, so safe to retry
            raise Retryable(f"{action}: connection lost ({type(exc).__name__})") from None
        except RuntimeError:
            if self._http.is_closed:
                raise Retryable(f"{action}: client closed") from None
            raise


async def _download_error(response: httpx.Response) -> AgentPlatformError:
    """Map a non-200 media host answer. The body is read only for errors, and at most 16 KiB of it."""
    action = "download"
    status = response.status_code
    if 300 <= status < 400:
        return MediaRedirected(f"{action}: redirect refused", status=status)
    body = bytearray()
    try:
        body += response.content[:_ERROR_BODY_CAP]  # in-memory transports hand over a read body
    except httpx.ResponseNotRead:
        try:
            async for chunk in response.aiter_raw():
                body += chunk
                if len(body) >= _ERROR_BODY_CAP:
                    break
        except httpx.HTTPError:
            pass
    code, trace = _error_fields(httpx.Response(status, content=bytes(body[:_ERROR_BODY_CAP])))
    kw = {"status": status, "code": code, "fbtrace_id": trace}
    if status in (404, 410):
        return MediaGone(f"{action}: media not found or expired", **kw)
    if status == 429:
        return RateLimited(f"{action}: rate limited", retry_after=_retry_after(response), **kw)
    if status in (401, 403):
        # The platform's key works for the API; a media host refusal must not stop the platform.
        return MediaDownloadError(f"{action}: media host refused the request", **kw)
    if status >= 500:
        return Retryable(f"{action}: media host error, retry later", retry_after=_retry_after(response), **kw)
    return MediaDownloadError(f"{action}: unexpected HTTP status", **kw)


async def _read_media(response: httpx.Response, *, max_bytes: int, expected: int | None, digests: list[bytes]) -> bytes:
    """Read a 200 body within ``max_bytes`` (with or without a truthful Content-Length) and verify it."""
    action = "download"
    status = response.status_code
    encoding = response.headers.get("Content-Encoding", "").strip().lower()
    if encoding not in ("", "identity"):
        # We asked for identity; a compressed body could expand far past the cap before we count it.
        raise MediaDownloadError(f"{action}: unexpected Content-Encoding", status=status)
    declared = None
    raw_length = response.headers.get("Content-Length")
    if raw_length is not None:
        raw_length = raw_length.strip()
        if not (raw_length.isascii() and raw_length.isdigit()):
            raise MediaIntegrityError(f"{action}: invalid Content-Length", status=status)
        declared = int(raw_length)
        if declared > max_bytes:
            raise MediaTooLarge(f"{action}: Content-Length is over the cap", status=status)
        if expected is not None and declared != expected:
            raise MediaIntegrityError(f"{action}: Content-Length does not match the media size", status=status)
    limit = declared if declared is not None else expected
    hasher = hashlib.sha256()
    body = bytearray()
    async for chunk in response.aiter_bytes():  # identity only (checked above), so these are the raw bytes
        total = len(body) + len(chunk)
        if total > max_bytes:
            raise MediaTooLarge(f"{action}: body is over the cap", status=status)
        if limit is not None and total > limit:
            raise MediaIntegrityError(f"{action}: body is longer than expected", status=status)
        body += chunk
        hasher.update(chunk)
    if limit is not None and len(body) != limit:
        raise MediaIntegrityError(f"{action}: body is shorter than expected", status=status)
    digest = hasher.digest()
    if any(not hmac.compare_digest(digest, want) for want in digests):
        raise MediaIntegrityError(f"{action}: sha256 mismatch", status=status)
    return bytes(body)


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
