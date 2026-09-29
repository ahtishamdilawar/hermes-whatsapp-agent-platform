"""Send one local file to WhatsApp through ``/agent/v1``: checks, conversion, upload, message, retries.

Hermes-free, so the gateway adapter and the out-of-process (cron) sender share it. The caller resolves and
authorises the recipient first (Meta delivers only to the agent's creator, so an upload for anyone else would
only copy the file to Meta for nothing), then calls ``send_media_file``.

Order for one file:

1. The narrow never-upload list (``media.is_denied_path``) before anything is read; then the path must be an
   existing regular file.
2. ``plan_outbound`` + ``prepare_outbound`` on a worker thread (Pillow, ffmpeg and file reads block).
3. Wait for both the ``media_upload`` and ``messages`` budgets, bounded (``wait_bound`` seconds in total). A
   wait of ``pacing_after`` seconds or more first calls ``notify_pacing`` once. Hermes never retries media,
   so failing fast would lose the file.
4. Upload outside ``lock`` (a 16 MiB upload must not block text replies); retried once on ``Retryable``,
   ``Ambiguous`` or a malformed answer (a stray upload is invisible and expires after 30 days).
5. Inside ``lock``: an overflowing caption as text first (``send_text``), then the media message. ``Retryable``
   (429/503/connect) is retried with the same media id; ``MediaGone`` re-uploads once; ``MediaRejected`` is
   final; ``Ambiguous`` is never retried (the file may have arrived). A quote is dropped and the message
   resent only when Meta's details name the quote (``context``/``message_id``): a code-100 media error is a
   bad field such as the caption, never a stale quote.

The upload always finishes before this returns: Hermes deletes TTS and ``/save`` files right after the call.
Nothing here logs or returns a path, filename, caption or media id.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import stat
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from . import media as policy
from .client import (
    MAX_TEXT_LENGTH,
    AgentPlatformError,
    Ambiguous,
    MalformedResponse,
    MediaGone,
    MediaRejected,
    RateLimited,
    Retryable,
)
from .formatting import to_whatsapp

logger = logging.getLogger(__name__)

WAIT_BOUND = 60.0  # seconds of budget waiting per file before giving up with flood_control
PACING_NOTICE_AFTER = 10.0  # a budget wait this long is worth telling the user about
SEND_ATTEMPTS = 3  # media message attempts on Retryable (429/503/connect), same media id
RETRY_BACKOFF = 1.0  # seconds, times the attempt number, for a Retryable without Retry-After
CAPTION_KINDS = ("image", "video", "document")  # audio and stickers take no caption

REFUSED_DENIED = "this file is never sent (it looks like a secret or the plugin's own state)"
REFUSED_MISSING = "file not found"
REFUSED_NOT_FILE = "not a regular file"
REFUSED_UNREADABLE = "file could not be read"
REFUSED_INVALID = "file could not be prepared for WhatsApp"


class TextNotSent(Exception):
    """Raised by a ``send_text`` callback when the caption text did not go out. ``payload`` (e.g. the text
    path's own result) is handed back to the caller in ``MediaOutcome.text_failure``."""

    def __init__(self, payload: Any = None) -> None:
        super().__init__("caption text not sent")
        self.payload = payload


@dataclass
class MediaOutcome:
    """What happened to one file. Exactly one of ``message_id``, ``refusal``, ``wait``, ``error`` or
    ``text_failure`` is set."""

    message_id: str | None = None  # the media message's wamid
    kind: str | None = None  # message type sent: image | video | audio | document | sticker
    mime: str | None = None  # MIME type of the file at ``path`` (what a quote re-attaches)
    path: str | None = None  # resolved local path that was read
    refusal: str | None = None  # refused locally before any upload (path-free text); final
    wait: float | None = None  # seconds of budget still needed when ``wait_bound`` ran out
    error: AgentPlatformError | None = None  # the API error that ended the attempt
    text_failure: Any = None  # ``TextNotSent.payload`` when the caption text failed
    uploads: int = 0

    @property
    def success(self) -> bool:
        return self.message_id is not None


class _Refused(Exception):
    pass


class _OverBudget(Exception):
    def __init__(self, need: float) -> None:
        super().__init__("budget wait over the bound")
        self.need = need


class _Budget:
    """Bounded waiting on the client's per-endpoint ``RateWindow``s (the total wait per file is capped)."""

    def __init__(self, client: Any, bound: float) -> None:
        self.limits = client.limits
        self.bound = max(0.0, float(bound))
        self.waited = 0.0

    def need(self, *names: str) -> float:
        return max(self.limits[name].delay() for name in names)

    async def wait_for(self, *names: str) -> None:
        """Sleep until every named window has a free slot; ``_OverBudget`` when the bound would be exceeded."""
        while True:
            need = self.need(*names)
            if need <= 0:
                return
            if self.waited + need > self.bound:
                raise _OverBudget(need)
            await asyncio.sleep(need)
            self.waited += need

    async def sleep(self, seconds: float) -> bool:
        seconds = max(0.0, seconds)
        if self.waited + seconds > self.bound:
            return False
        await asyncio.sleep(seconds)
        self.waited += seconds
        return True


def is_stale_media_quote(exc: AgentPlatformError) -> bool:
    """A media send refused only because of its quote. Meta's documented error for a bad
    ``context.message_id`` has no code of its own, and code 100/131009 also mean a bad caption, media type or
    size on a media send, so only Meta's details naming the quote count."""
    if not isinstance(exc, MediaRejected) or exc.status != 400:
        return False
    details = (exc.details or "").lower()
    return "context" in details or "message_id" in details


def _load(
    path: Any,
    *,
    requested: str,
    force_document: bool,
    file_name: str | None,
    transcoder: Callable[[str], str | None] | None,
    deny_dirs: Iterable[str],
) -> tuple[policy.PreparedMedia, policy.OutboundPlan, str]:
    """Blocking part (worker thread): checks, plan and prepare. ``_Refused`` carries a path-free reason."""
    try:
        raw = os.fspath(path)
    except TypeError:
        raise _Refused(REFUSED_MISSING) from None
    if not isinstance(raw, str) or not raw:
        raise _Refused(REFUSED_MISSING)
    if policy.is_denied_path(raw, extra_dirs=deny_dirs):
        raise _Refused(REFUSED_DENIED)
    try:
        resolved = os.path.realpath(raw)  # read the file that was checked, not a symlink swapped in later
        info = os.stat(resolved)
    except FileNotFoundError:
        raise _Refused(REFUSED_MISSING) from None
    except (OSError, ValueError):
        raise _Refused(REFUSED_UNREADABLE) from None
    if not stat.S_ISREG(info.st_mode):
        raise _Refused(REFUSED_NOT_FILE)
    name = file_name if isinstance(file_name, str) and file_name.strip() else os.path.basename(raw)
    try:
        plan = policy.plan_outbound(
            resolved, requested=requested, force_document=force_document, file_name=name, size=info.st_size
        )
        prepared = policy.prepare_outbound(resolved, plan, transcoder=transcoder)
    except policy.MediaPolicyError as exc:  # MediaTooBig / EmptyMedia: messages carry no path
        raise _Refused(str(exc)) from None
    except OSError:
        raise _Refused(REFUSED_UNREADABLE) from None
    return prepared, plan, resolved


def _split_utf16(text: str, limit: int = MAX_TEXT_LENGTH) -> list[str]:
    parts, current, width = [], [], 0
    for char in text:
        w = 2 if ord(char) > 0xFFFF else 1
        if width + w > limit:
            parts.append("".join(current))
            current, width = [], 0
        current.append(char)
        width += w
    if current:
        parts.append("".join(current))
    return [p for p in parts if p.strip()]


async def send_media_file(
    client: Any,
    to: str,
    path: Any,
    *,
    requested: str,
    caption: str | None = None,
    reply_to: str | None = None,
    file_name: str | None = None,
    force_document: bool = False,
    transcoder: Callable[[str], str | None] | None = None,
    deny_dirs: Iterable[str] = (),
    lock: Any = None,
    send_text: Callable[[str, str | None], Awaitable[Any]] | None = None,
    notify_pacing: Callable[[float], Awaitable[Any]] | None = None,
    note_filter: Callable[[str], str | None] | None = None,
    wait_bound: float = WAIT_BOUND,
    pacing_after: float = PACING_NOTICE_AFTER,
    retry_backoff: float = RETRY_BACKOFF,
) -> MediaOutcome:
    """Send the file at ``path`` to ``to`` (an already authorised ``user:<id>``).

    ``requested`` is ``image``/``video``/``audio``/``voice``/``document``; ``document`` or ``force_document``
    sends the bytes unmodified as a document. ``caption`` is raw markdown: it is converted with
    ``to_whatsapp`` and measured in UTF-16 units. If it doesn't fit Meta's 1024 (or the type takes no caption)
    the whole caption goes first through ``send_text(raw_caption, reply_to)``, called while ``lock`` is held,
    which must convert and split it and raise ``TextNotSent`` (or an ``AgentPlatformError``) on failure;
    without one it is sent with ``client.send_text``. ``note_filter`` may hide the "sent as a file" note
    (return ``None``/``""``). ``lock`` is an async context manager serialising sends to the recipient.
    """
    lock = lock if lock is not None else contextlib.nullcontext()
    try:
        prepared, plan, resolved = await asyncio.to_thread(
            _load,
            path,
            requested=requested,
            force_document=force_document,
            file_name=file_name,
            transcoder=transcoder,
            deny_dirs=tuple(deny_dirs),
        )
    except _Refused as exc:
        logger.warning("media not sent: %s", exc)
        return MediaOutcome(refusal=str(exc))
    outcome = MediaOutcome(kind=prepared.kind, mime=plan.source_mime or prepared.mime, path=resolved)

    # Caption: the whole caption rides on the file only if it fits; never split between bubble and text.
    raw_caption = caption.strip() if isinstance(caption, str) else ""
    note = prepared.note
    if note and note_filter is not None:
        note = note_filter(note) or None
    text_first: str | None = None
    media_caption: str | None = None
    if prepared.kind not in CAPTION_KINDS:
        text_first = raw_caption or None
    else:
        converted = to_whatsapp(raw_caption).strip() if raw_caption else ""
        combined = "\n".join(part for part in (converted, note) if part)
        if combined and policy.caption_fits(combined):
            media_caption = combined
        elif combined:
            text_first = raw_caption or None
            media_caption = note if note and policy.caption_fits(note) else None
    quote = None if text_first else reply_to
    filename = prepared.filename if prepared.kind == "document" else None

    budget = _Budget(client, wait_bound)
    try:
        need = budget.need("media_upload", "messages")
        if need > budget.bound:
            raise _OverBudget(need)
        if need >= pacing_after and notify_pacing is not None:
            try:
                await notify_pacing(need)
            except Exception as exc:  # a notice must never cost the file
                logger.debug("pacing notice failed (%s)", type(exc).__name__)
        await budget.wait_for("media_upload", "messages")

        media_id = await _upload(client, prepared, budget, retry_backoff, outcome)
        reuploaded = False
        attempts = 0
        while True:
            gone = False
            async with lock:
                if text_first:
                    await budget.wait_for("messages")
                    try:
                        await _send_caption_text(client, to, text_first, reply_to, send_text)
                    except TextNotSent as exc:
                        outcome.text_failure = exc.payload if exc.payload is not None else exc
                        return outcome
                    text_first = None
                while True:
                    await budget.wait_for("messages")
                    try:
                        outcome.message_id = await client.send_media(
                            to, prepared.kind, media_id, caption=media_caption, filename=filename, reply_to=quote
                        )
                        break
                    except MediaGone as exc:
                        if reuploaded:
                            outcome.error = exc
                            return outcome
                        gone = True
                        break
                    except MediaRejected as exc:
                        if quote and is_stale_media_quote(exc):
                            quote = None  # nothing was sent; the same media id without the quote
                            continue
                        outcome.error = exc
                        return outcome
                    except Retryable as exc:
                        attempts += 1
                        if attempts >= SEND_ATTEMPTS:
                            outcome.error = exc
                            return outcome
                        if not isinstance(exc, RateLimited):  # a 429 already marked the window full
                            delay = exc.retry_after if exc.retry_after is not None else retry_backoff * attempts
                            if not await budget.sleep(delay):
                                outcome.error = exc
                                return outcome
                    except AgentPlatformError as exc:  # Ambiguous, malformed 2xx, not creator, auth, ...
                        outcome.error = exc
                        return outcome
            if not gone:
                break
            reuploaded = True  # the id expired between upload and send: upload the same bytes once more
            media_id = await _upload(client, prepared, budget, retry_backoff, outcome)
    except _OverBudget as exc:
        outcome.wait = exc.need
        return outcome
    except AgentPlatformError as exc:  # upload or caption text failed for good
        outcome.error = exc
        return outcome
    except ValueError:  # an argument the client refused before any request
        outcome.refusal = REFUSED_INVALID
        return outcome
    logger.info(
        "media sent: %s (%s, %d bytes, %d upload(s))", prepared.kind, prepared.mime, len(prepared.data), outcome.uploads
    )
    return outcome


async def _upload(
    client: Any, prepared: policy.PreparedMedia, budget: _Budget, retry_backoff: float, outcome: MediaOutcome
) -> str:
    """``POST /media``, retried once when it may simply not have happened (or happened unseen)."""
    for attempt in (1, 2):
        try:
            outcome.uploads += 1
            return await client.upload_media(prepared.data, mime=prepared.mime, filename=prepared.filename)
        except (Retryable, Ambiguous, MalformedResponse) as exc:
            if attempt == 2:
                raise
            if isinstance(exc, RateLimited):
                await budget.wait_for("media_upload")
            else:
                delay = exc.retry_after if getattr(exc, "retry_after", None) is not None else retry_backoff
                if not await budget.sleep(delay):
                    raise
    raise AssertionError("unreachable")  # pragma: no cover


async def _send_caption_text(
    client: Any, to: str, text: str, reply_to: str | None, send_text: Callable[[str, str | None], Awaitable[Any]] | None
) -> None:
    if send_text is not None:
        await send_text(text, reply_to)
        return
    for index, chunk in enumerate(_split_utf16(to_whatsapp(text))):
        await client.send_text(to, chunk, reply_to=reply_to if index == 0 else None)
