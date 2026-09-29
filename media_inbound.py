"""Fetching inbound media for the adapter: ``GET /media/<id>``, then the bounded download, with a small retry.

No Hermes imports: the adapter caches the bytes with Hermes's helpers and builds the event. Every failure leaves
this module as ``InboundMediaError`` with a reason from a fixed set, so the caller can tell the agent what happened
without ever quoting an exception text, a URL or a media id.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging

from .client import (
    Ambiguous,
    MediaDownloadError,
    MediaGone,
    MediaInfo,
    MediaTooLarge,
    Retryable,
)
from .media import MIB, InboundMedia

logger = logging.getLogger(__name__)

FETCH_DEADLINE = 90.0  # seconds for GET /media plus the download, retries and budget waits included
FETCH_ATTEMPTS = 2  # attempts on a transient error (429, 503, connect errors, timeouts, resets)
RETRY_WAIT_CAP = 5.0  # longest Retry-After honoured inline; the poll loop waits while we sleep

# Reasons, a fixed vocabulary (they reach the agent's prompt).
TOO_LARGE = "too large"
UNAVAILABLE = "no longer available"
FAILED = "download failed"
UNSUPPORTED = "unsupported format"
REASONS = (TOO_LARGE, UNAVAILABLE, FAILED, UNSUPPORTED)


class InboundMediaError(Exception):
    """An inbound attachment could not be fetched. ``str()`` is the reason only (no id, url or filename)."""

    def __init__(self, reason: str, *, size: int | None = None, limit: int | None = None) -> None:
        if reason not in REASONS:
            raise ValueError("unknown inbound media failure reason")
        super().__init__(reason)
        self.reason = reason
        self.size = size
        self.limit = limit


def media_hint(media_id: str | None) -> str:
    """Log-safe stand-in for a media id: a short hash."""
    return "#" + hashlib.sha256(str(media_id or "").encode("utf-8")).hexdigest()[:8]


async def fetch_inbound_media(
    client,
    item: InboundMedia,
    *,
    max_bytes: int,
    timeout: float = FETCH_DEADLINE,
    attempts: int = FETCH_ATTEMPTS,
    retry_wait_cap: float = RETRY_WAIT_CAP,
) -> bytes:
    """Download ``item``'s bytes, verified against the update's sha256 and bounded by ``max_bytes``.

    ``timeout`` bounds everything (the ``media_get`` budget wait, both requests and the retries), so a trickling
    media host can't stall the poll loop. Transient errors (``Retryable``, and ``Ambiguous`` from the read-only
    ``GET``) are retried up to ``attempts`` times in total, waiting ``Retry-After`` capped at ``retry_wait_cap``.
    Raises ``InboundMediaError`` for every failure; ``asyncio.CancelledError`` propagates.
    """
    try:
        return await asyncio.wait_for(
            _fetch(client, item, max_bytes=max_bytes, attempts=attempts, retry_wait_cap=retry_wait_cap),
            timeout,
        )
    except InboundMediaError:
        raise
    except TimeoutError:  # asyncio.TimeoutError is TimeoutError from Python 3.11
        logger.debug("inbound media %s: deadline of %.0fs passed", media_hint(item.media_id), timeout)
        raise InboundMediaError(FAILED) from None


async def _fetch(client, item: InboundMedia, *, max_bytes: int, attempts: int, retry_wait_cap: float) -> bytes:
    info: MediaInfo | None = None
    attempt = 0
    while True:
        attempt += 1
        try:
            if info is None:
                info = await client.get_media(item.media_id)
                if info.file_size is not None and info.file_size > max_bytes:
                    raise InboundMediaError(TOO_LARGE, size=info.file_size, limit=max_bytes)
            return await client.download_media(info, max_bytes=max_bytes, verify_sha256=item.sha256)
        except InboundMediaError:
            raise
        except MediaTooLarge:
            raise InboundMediaError(TOO_LARGE, size=info.file_size if info else None, limit=max_bytes) from None
        except MediaGone:
            raise InboundMediaError(UNAVAILABLE) from None
        except MediaDownloadError as exc:  # untrusted url, redirect, integrity, refused by the media host
            logger.debug("inbound media %s: %s", media_hint(item.media_id), exc.describe())
            raise InboundMediaError(FAILED) from None
        except (Retryable, Ambiguous) as exc:
            transient = isinstance(exc, Retryable) or info is None  # Ambiguous only from the read-only GET
            if not transient or attempt >= attempts:
                logger.debug("inbound media %s: giving up (%s)", media_hint(item.media_id), exc.describe())
                raise InboundMediaError(FAILED) from None
            wait = min(retry_wait_cap, max(0.0, exc.retry_after if exc.retry_after is not None else 1.0))
            await asyncio.sleep(wait)
        except ValueError:  # a malformed media id (checked before any request)
            raise InboundMediaError(FAILED) from None
        except Exception as exc:  # NotSent (401, 403, 409, ...), MalformedResponse, anything unexpected
            describe = getattr(exc, "describe", None)
            detail = describe() if callable(describe) else type(exc).__name__
            logger.debug("inbound media %s: %s", media_hint(item.media_id), detail)
            raise InboundMediaError(FAILED) from None


def _mb(n: int) -> str:
    return f"{n / MIB:.1f}".removesuffix(".0")


def failure_note(reason: str, label: str, *, size: int | None = None, limit: int | None = None) -> str:
    """The bracketed note the agent gets instead of the file (Telegram's wording, a fixed reason set).

    ``label`` is a short description such as ``"an image"`` or ``"a document ('report.pdf')"``.
    """
    if reason == TOO_LARGE:
        what = label.split(" ", 1)[1] if label.startswith(("a ", "an ")) else label
        if size is not None and limit is not None:
            detail = f"{_mb(size)} MB exceeds the {_mb(limit)} MB limit"
        elif limit is not None:
            detail = f"it exceeds the {_mb(limit)} MB limit"
        else:
            detail = "it is too large"
        return f"[The user's {what} was not downloaded: {detail}. Ask them to send a smaller file.]"
    if reason == UNSUPPORTED:
        return f"[The user sent {label} but it could not be read ({UNSUPPORTED}).]"
    why = f"it is {UNAVAILABLE}" if reason == UNAVAILABLE else FAILED
    return f"[The user sent {label} but it could not be downloaded ({why}). Ask them to send it again if you need it.]"
