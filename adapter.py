"""Hermes platform adapter for Meta's WhatsApp Agent Platform.

WhatsApp → Meta ``GET /agent/v1/updates`` (long poll) → Hermes session/agent →
``POST /agent/v1/messages`` → WhatsApp. No webhook, business number or WhatsApp Web
session is involved; this is distinct from the ``whatsapp`` (Baileys) and
``whatsapp_cloud`` (Business Cloud API) transports.

Delivery is at-least-once: a send whose outcome Meta leaves ambiguous (HTTP 500, a
timeout after the request was written) is not retried inline, and Hermes's delivery
ledger re-sends the reply later with a "may be a duplicate" marker.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import math
import os
import re
import threading
import time
import traceback
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from gateway.config import Platform, PlatformConfig
from gateway.platforms._shared import (
    env_is_connected,
    extra_or_secret,
    get_scoped_secret,
    seed_extra_from_env,
    send_error,
)
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from hermes_constants import get_hermes_home

from . import hermes_compat as compat
from . import media, media_inbound, media_outbound
from .client import (
    API_BASE,
    CODE_BAD_FIELD,
    CODE_INVALID_PARAMETER,
    MAX_TEXT_LENGTH,
    AgentPlatformClient,
    AgentPlatformError,
    AuthError,
    MediaGone,
    MediaRejected,
    NotCreatorError,
    NotSent,
    PollConflict,
    RateLimited,
    Retryable,
    Updates,
)
from .formatting import to_whatsapp
from .media_inbound import InboundMediaError, failure_note, fetch_inbound_media, media_hint
from .state import PollState, key_fingerprint, read_creator, state_path

logger = logging.getLogger(__name__)

PLATFORM_NAME = "whatsapp_agent_platform"
LABEL = "WhatsApp Agent Platform"
KEY_ENV = "WHATSAPP_AGENT_PLATFORM_API_KEY"
HOME_ENV = "WHATSAPP_AGENT_PLATFORM_HOME_CHANNEL"
ALLOWED_ENV = "WHATSAPP_AGENT_PLATFORM_ALLOWED_USERS"
ALLOW_ALL_ENV = "WHATSAPP_AGENT_PLATFORM_ALLOW_ALL_USERS"
BASE_URL_ENV = "WHATSAPP_AGENT_PLATFORM_BASE_URL"  # development only: point at a fake API
MEDIA_ENV = "WHATSAPP_AGENT_PLATFORM_MEDIA_ENABLED"  # default on; false/0/no/off = text only
MEDIA_KEY = "media_enabled"  # the same switch in config.yaml (platform ``extra``)
SELF_TARGET = "self"

_USER_ID_RE = re.compile(r"^user:\S+$")
_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

POLL_TIMEOUT = 20  # seconds; Meta allows 0-25
POLL_MIN_INTERVAL = 60.0 / 14  # one poll of headroom under Meta's 15/min
CHUNK_LIMIT = 4000  # split target (UTF-16 units), leaving margin under Meta's 4096
TYPING_REFRESH = 20.0  # Meta's indicator lasts 25 s
REPLY_QUIET = 3.0  # no typing refresh right after a reply (it would re-show the indicator)
INTERIM_SEND_RESERVE = 4  # keep this many /messages slots for the final answer
HANDOFF_ATTEMPTS = 3  # redeliveries of one message to Hermes before skipping it
CLOCK_SKEW = 120  # seconds of tolerance when comparing Meta timestamps with the local clock
CONFLICT_BACKOFF = 60.0  # after a 409, let the other poller finish before polling again
CONFLICT_LIMIT, CONFLICT_WINDOW = 3, 600.0  # this many 409s in the window = a real second poller
SAVE_FAILURE_LIMIT = 5  # consecutive state-save failures before the platform gives up
UNSUPPORTED_NOTICE = "I can only read text messages on this channel for now — please send your request as text."
UNSUPPORTED_TYPE_NOTICE = (
    "I can't read this kind of message on this channel — please send text, a photo, a voice note or a document."
)
DOCUMENTED_TYPES = frozenset({"text", "reaction", *media.MEDIA_KINDS})  # the manual's seven inbound types
MAX_REACTION_CHARS = 32
REACTION_HOOK_TIMEOUT = 5.0  # seconds one reaction hook may hold the poll loop
PREPARED_MEDIA_MAX = 32  # downloaded media kept per wamid, so a Hermes handoff retry doesn't download again
STICKER_NOTE = "[The user sent a sticker]"
MAX_TIMESTAMP = 4_102_444_800  # 2100-01-01; later values aren't Meta's (and overflow datetime on Windows)
_TIMESTAMP_RE = re.compile(r"[0-9]{1,12}")
_WAMID_RE = re.compile(r"[A-Za-z0-9._=+/-]{1,128}")  # "wamid." + Base64, as in the manual's examples
MAX_REMEMBERED_ID = 256  # longer "ids" from a malformed message are not put into the dedup window
_ZWJ = "\u200d"
# Invisible, bidi, control and separator characters: never passed on in a reaction emoji.
_UNSAFE_EMOJI_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
# Subdivision flags (England, Scotland, Wales): a black flag, 2-6 tag letters/digits, then CANCEL TAG.
_FLAG_TAG_SEQUENCE = re.compile("\U0001f3f4[\U000e0030-\U000e0039\U000e0061-\U000e007a]{2,6}\U000e007f")
_DISPLAY_UNSAFE = re.compile(r"[^\w.\- ]")  # Hermes's rule for names shown in prompt notes
_REFUSED_IMAGES = frozenset({"image/heic", "image/heif", "image/avif", "image/tiff"})  # the image cache refuses
_DEFAULT_MIME = {
    "image": "image/jpeg",
    "audio": "audio/ogg",
    "video": "video/mp4",
    "document": media.OCTET_STREAM,
    "sticker": "image/webp",
}


@dataclass
class _InboundMedia:
    """What a media message turned into: the event text and type, and aligned attachment lists."""

    text: str
    message_type: MessageType
    urls: list[str] = field(default_factory=list)
    types: list[str] = field(default_factory=list)
    inlined: list[bool] = field(default_factory=list)


class HandoffError(Exception):
    """Hermes raised while accepting an inbound message (the offset is not advanced)."""


def _api_key() -> str:
    return str(get_scoped_secret(KEY_ENV, "") or "").strip()


def _base_url() -> str:
    """API base URL. The override exists for testing against a local fake API only: it must be
    HTTPS, or plain HTTP on localhost, because the API key is sent with every request."""
    override = str(get_scoped_secret(BASE_URL_ENV, "") or "").strip()
    if not override:
        return API_BASE
    parts = urlsplit(override)
    if parts.scheme == "https" or (parts.scheme == "http" and (parts.hostname or "") in _LOCAL_HOSTS):
        logger.warning("%s: %s is set; sending the API key to %s instead of Meta", LABEL, BASE_URL_ENV, parts.hostname)
        return override
    logger.error("%s: ignoring %s (must be https:// or http://localhost)", LABEL, BASE_URL_ENV)
    return API_BASE


def _csv_env(name: str) -> set[str]:
    raw = str(get_scoped_secret(name, "") or "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def _on_unless_falsy(value: Any) -> bool:
    """A default-on switch: only false/0/no/off (or a YAML ``false``) turns it off."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in _FALSY


def _media_enabled(config: Any) -> bool:
    """``WHATSAPP_AGENT_PLATFORM_MEDIA_ENABLED`` (env wins), else ``media_enabled`` in the platform's config."""
    extra = getattr(config, "extra", None)
    return _on_unless_falsy(extra_or_secret(extra if isinstance(extra, dict) else None, MEDIA_KEY, MEDIA_ENV, True))


def _restrict_permissions(path: str) -> None:
    """Best effort: make a cached inbound file readable by its owner only (POSIX; Windows keeps the profile ACL)."""
    if os.name == "posix":
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)


def _emoji_part(ch: str) -> bool:
    """A character that can sit next to a ZWJ inside an emoji sequence: a pictograph, a skin-tone modifier, a
    variation selector, or a code point newer than this Python's Unicode tables."""
    return ord(ch) > 0x7F and unicodedata.category(ch) in ("So", "Sk", "Mn", "Cn")


def _clean_emoji(value: Any) -> str:
    """A reaction emoji for hooks, at most 32 characters.

    Control (C0/C1), format (zero-width, bidi, BOM, tags), surrogate and line/paragraph-separator characters are
    dropped. Two exceptions keep real emoji intact: a ZWJ *between* two emoji parts (family, profession and flag
    sequences) and a complete subdivision-flag tag sequence.
    """
    if not isinstance(value, str):
        return ""
    value = value[: MAX_REACTION_CHARS * 8]  # bound the work; far more than any real emoji sequence
    out: list[str] = []
    i = 0
    while i < len(value):
        flag = _FLAG_TAG_SEQUENCE.match(value, i)
        if flag is not None:
            out.append(flag.group())
            i = flag.end()
            continue
        ch = value[i]
        if ch == _ZWJ:
            following = value[i + 1] if i + 1 < len(value) else ""
            if out and _emoji_part(out[-1][-1]) and following and _emoji_part(following):
                out.append(ch)
        elif unicodedata.category(ch) not in _UNSAFE_EMOJI_CATEGORIES:
            out.append(ch)
        i += 1
    return "".join(out).strip()[:MAX_REACTION_CHARS].rstrip(_ZWJ).strip()


def _valid_wamid(value: Any) -> bool:
    """A message id shaped like Meta's wamids (ASCII ``wamid.`` + Base64, at most 128 characters)."""
    return isinstance(value, str) and _WAMID_RE.fullmatch(value) is not None


def _rememberable(value: Any) -> bool:
    """An inbound id that may go into the dedup window, even when the rest of its message is unusable."""
    return isinstance(value, str) and 0 < len(value) <= MAX_REMEMBERED_ID


def _timestamp(value: Any) -> int | None:
    """Meta's ``timestamp`` (epoch seconds, sent as a string) as an int; None when missing or out of range."""
    if isinstance(value, str) and _TIMESTAMP_RE.fullmatch(value.strip()):
        seconds = int(value.strip())
    elif isinstance(value, int) and not isinstance(value, bool):
        seconds = value
    elif isinstance(value, float) and math.isfinite(value):
        seconds = int(value)
    else:
        return None
    return seconds if 0 < seconds <= MAX_TIMESTAMP else None


def _text_body(message: dict[str, Any]) -> str | None:
    """The body of a ``text`` message; None when ``text`` isn't an object or its body isn't a string."""
    text = message.get("text")
    body = text.get("body") if isinstance(text, dict) else None
    return body if isinstance(body, str) else None


def _discard(path: str) -> None:
    """Remove a cached file whose message then failed, so nothing unreferenced stays in Hermes's cache."""
    with contextlib.suppress(OSError):
        os.remove(path)


def _where(exc: BaseException) -> str:
    """``file:line`` of the innermost frame, for logs that must not quote the exception's text."""
    frames = traceback.extract_tb(exc.__traceback__)
    return f"{os.path.basename(frames[-1].filename)}:{frames[-1].lineno}" if frames else "?"


def _media_label(kind: str, *, voice: bool = False, filename: str | None = None) -> str:
    """How a failure note names the attachment ("an image", "a document ('report.pdf')")."""
    if kind == "audio":
        return "a voice message" if voice else "an audio file"
    if kind == "document" and filename:
        name = _DISPLAY_UNSAFE.sub("_", media.sanitize_filename(filename, default_stem="document"))
        return f"a document ('{name}')"
    return {"image": "an image", "video": "a video", "sticker": "a sticker"}.get(kind, "a document")


def _document_name(filename: str | None, mime: str, default_stem: str) -> str:
    """The sanitised name for the document cache; the MIME's extension is added only when the name has none."""
    name = media.sanitize_filename(filename, default_stem=default_stem)
    ext = media.ext_for_mime(mime)
    if ext and not os.path.splitext(name)[1]:
        name = media.sanitize_filename(filename, default_stem=default_stem, ext=ext)
    return name


def _single(path: str, mime: str, message_type: MessageType, text: str) -> _InboundMedia:
    _restrict_permissions(path)
    return _InboundMedia(text, message_type, [path], [mime], [False])


def _id_hint(user_id: str) -> str:
    """Log-safe hint for a participant id (the manual says ids must never be shown to users)."""
    return f"…{user_id[-4:]}"


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _hard_split(chunk: str) -> list[str]:
    if _utf16_len(chunk) <= MAX_TEXT_LENGTH:
        return [chunk]
    parts, current, width = [], [], 0
    for char in chunk:
        w = 2 if ord(char) > 0xFFFF else 1
        if width + w > MAX_TEXT_LENGTH:
            parts.append("".join(current))
            current, width = [], 0
        current.append(char)
        width += w
    if current:
        parts.append("".join(current))
    return parts


def _chunks(text: str) -> list[str]:
    """Hermes's fence-aware splitter (counting UTF-16 units, as WhatsApp does for emoji),
    then a hard cap at Meta's 4096."""
    out: list[str] = []
    for chunk in BasePlatformAdapter.truncate_message(text, CHUNK_LIMIT, len_fn=_utf16_len):
        out.extend(_hard_split(chunk))
    return [c for c in out if c.strip()]


class WhatsAppAgentPlatformAdapter(BasePlatformAdapter):
    """Long-poll adapter for one WhatsApp agent (one API key)."""

    MAX_MESSAGE_LENGTH = MAX_TEXT_LENGTH
    # No edit endpoint: Hermes then skips token streaming and tool-progress bubbles.
    SUPPORTS_MESSAGE_EDITING = False
    splits_long_messages = True

    # The machine-wide scoped lock is re-acquirable by the same PID, so also guard
    # against two adapters for one key inside a multiplexed gateway process.
    _guard = threading.Lock()
    _active_keys: set[str] = set()

    def __init__(self, config: PlatformConfig) -> None:
        super().__init__(config, Platform(PLATFORM_NAME))
        self._client: AgentPlatformClient | None = None
        self._state: PollState | None = None
        self._fingerprint: str | None = None
        self._poll_task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self._background: set[asyncio.Task] = set()
        self._latest_inbound: collections.OrderedDict[str, str] = collections.OrderedDict()
        self._status_at: collections.OrderedDict[str, float] = collections.OrderedDict()
        self._reply_sent_at: dict[str, float] = {}
        self._names: dict[str, str] = {}
        self._handoff_failures: dict[str, int] = {}
        self._non_creators: set[str] = set()  # senders Meta confirmed are not the creator
        self._conflicts: collections.deque[float] = collections.deque()
        self._save_failures = 0
        self._last_poll_at = 0.0
        self._poll_timeout = POLL_TIMEOUT
        self._poll_min_interval = POLL_MIN_INTERVAL
        self._backoff_base = 2.0
        self._conflict_backoff = CONFLICT_BACKOFF
        self._media_enabled = _media_enabled(config)
        self._media_timeout: float | None = None  # None = media_inbound.FETCH_DEADLINE; tests shrink it
        self._media_retry_wait: float | None = None  # None = media_inbound.RETRY_WAIT_CAP; tests set 0
        self._hook_timeout = REACTION_HOOK_TIMEOUT
        self._prepared_media: collections.OrderedDict[str, _InboundMedia] = collections.OrderedDict()

    @property
    def name(self) -> str:
        return LABEL

    @property
    def authorization_is_upstream(self) -> bool:
        """The adapter authorizes every sender itself before dispatch: the agent's creator is
        confirmed with Meta (``POST /statuses`` succeeds only for the creator's messages and
        returns 403/131005 otherwise), and anyone else needs
        ``WHATSAPP_AGENT_PLATFORM_ALLOWED_USERS`` or ``..._ALLOW_ALL_USERS``."""
        return True

    def _typing_enabled(self) -> bool:
        return bool(getattr(self.config, "typing_indicator", True))

    # ------------------------------------------------------------------ lifecycle
    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if self._poll_task is not None and not self._poll_task.done():
            return True
        key = _api_key()
        if not key:
            self._set_fatal_error("config_missing", f"{KEY_ENV} is not set", retryable=False)
            return False
        fingerprint = key_fingerprint(key)
        with self._guard:
            if fingerprint in self._active_keys:
                self._set_fatal_error(
                    f"{PLATFORM_NAME}_lock",
                    "This agent API key is already polling in this gateway process",
                    retryable=True,
                )
                return False
            self._active_keys.add(fingerprint)
        self._fingerprint = fingerprint
        if not self._acquire_platform_lock(PLATFORM_NAME, key, "WhatsApp Agent Platform API key"):
            self._release_guard()
            return False

        try:
            self._state = PollState.load(state_path(get_hermes_home(), key))
        except OSError as exc:
            await self._fail_connect("state_unreadable", f"cannot read polling state ({type(exc).__name__})", True)
            return False
        self._client = AgentPlatformClient(key, base_url=_base_url())
        logger.info("%s: connecting (profile state %s)", LABEL, self._state.path.name)
        try:
            if not self._state.path.exists():
                self._state.save()
            # Auth probe: an immediate poll that is NOT dispatched. Polling does not
            # consume updates, so the loop re-reads anything this returns.
            await self._client.get_updates(self._state.next_offset, timeout=0, limit=1)
        except AuthError as exc:
            await self._fail_connect("invalid_auth", f"API key rejected by Meta ({exc.describe()})", False)
            return False
        except OSError as exc:
            await self._fail_connect("state_unwritable", f"cannot write polling state ({type(exc).__name__})", True)
            return False
        except AgentPlatformError as exc:
            await self._fail_connect("api_unavailable", exc.describe(), True)
            return False

        self._mark_connected()
        self._poll_task = asyncio.create_task(self._poll_loop(), name=f"{PLATFORM_NAME}-poll")
        logger.info("%s: authenticated; long polling started", LABEL)
        return True

    async def _fail_connect(self, code: str, message: str, retryable: bool) -> None:
        logger.error("%s: connect failed: %s", LABEL, message)
        self._set_fatal_error(code, f"{LABEL}: {message}", retryable=retryable)
        await self._close_resources()

    async def disconnect(self) -> None:
        self._mark_disconnected()
        task, self._poll_task = self._poll_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._close_resources()

    async def _close_resources(self) -> None:
        for task in list(self._background):
            task.cancel()
        self._background.clear()
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._release_platform_lock()
        self._release_guard()

    def _release_guard(self) -> None:
        if self._fingerprint is not None:
            with self._guard:
                self._active_keys.discard(self._fingerprint)
            self._fingerprint = None

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # ---------------------------------------------------------------- inbound
    async def _poll_loop(self) -> None:
        failures = 0
        while self._running:
            wait = self._poll_min_interval - (time.monotonic() - self._last_poll_at)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_poll_at = time.monotonic()
            try:
                assert self._client is not None and self._state is not None
                updates = await self._client.get_updates(self._state.next_offset, timeout=self._poll_timeout)
                await self._process(updates)
                failures = 0
            except asyncio.CancelledError:
                raise
            except AuthError as exc:
                await self._stop_polling("invalid_auth", f"API key rejected by Meta ({exc.describe()})")
                return
            except PollConflict as exc:
                if self._record_conflict():
                    await self._stop_polling(
                        "poll_conflict",
                        "another client keeps polling this agent's API key (HTTP 409 "
                        f"{CONFLICT_LIMIT} times in {CONFLICT_WINDOW / 60:.0f} min); only one poller per key "
                        "is allowed — stop the other client, then restart the gateway",
                    )
                    return
                logger.warning(
                    "%s: another client polled this agent's API key (%s); resuming in %.0fs",
                    LABEL,
                    exc.describe(),
                    self._conflict_backoff,
                )
                await asyncio.sleep(self._conflict_backoff)
            except OSError as exc:
                self._save_failures += 1
                if self._save_failures >= SAVE_FAILURE_LIMIT:
                    await self._stop_polling(
                        "state_unwritable",
                        f"cannot persist polling offset ({type(exc).__name__}, {self._save_failures} attempts)",
                        retryable=True,
                    )
                    return
                failures = await self._backoff(exc, failures)
            except Exception as exc:  # Retryable, Ambiguous, Malformed, other 4xx, handoff failures
                failures = await self._backoff(exc, failures)

    def _record_conflict(self) -> bool:
        """Track 409s; True when they are frequent enough to mean a real second poller."""
        now = time.monotonic()
        self._conflicts.append(now)
        while self._conflicts and now - self._conflicts[0] > CONFLICT_WINDOW:
            self._conflicts.popleft()
        return len(self._conflicts) >= CONFLICT_LIMIT

    async def _backoff(self, exc: Exception, failures: int) -> int:
        """Sleep before the next poll; the offset is unchanged, so nothing is lost."""
        failures += 1
        delay = min(60.0, self._backoff_base * 2 ** min(failures - 1, 5))
        if isinstance(exc, RateLimited) and exc.retry_after is not None:
            delay = max(delay, min(60.0, exc.retry_after))
            if self._client is not None and exc.status == 429:
                self._client.limits["updates"].penalize(delay)
        if isinstance(exc, AgentPlatformError):
            logger.warning("%s: poll failed (%s); retrying in %.0fs", LABEL, exc.describe(), delay)
        elif isinstance(exc, (HandoffError, OSError)):
            logger.warning("%s: %s; retrying in %.0fs", LABEL, type(exc).__name__, delay)
        else:
            logger.warning("%s: poll failed; retrying in %.0fs", LABEL, delay, exc_info=True)
        await asyncio.sleep(delay)
        return failures

    async def _stop_polling(self, code: str, message: str, *, retryable: bool = False) -> None:
        logger.error("%s: polling stopped: %s", LABEL, message)
        self._set_fatal_error(code, f"{LABEL}: {message}", retryable=retryable)
        await self._notify_fatal_error()

    async def _process(self, updates: Updates) -> None:
        state = self._state
        assert state is not None
        if updates.next_offset is None:
            return  # 204: nothing new; re-poll with the same offset.
        for contact in updates.contacts:
            profile = contact.get("profile")
            wa_id = contact.get("wa_id")
            if isinstance(wa_id, str) and isinstance(profile, dict) and isinstance(profile.get("name"), str):
                self._names[wa_id] = profile["name"]
        handed = 0
        for message in updates.messages:
            try:
                if await self._handle_inbound(message):
                    handed += 1
            except (HandoffError, AgentPlatformError, OSError):
                raise  # Hermes busy, Meta can't confirm a sender yet, state unwritable: re-poll the page
            except Exception as exc:  # a message we can't parse must not block the page forever
                self._skip_unhandled(message, exc)
        if updates.next_offset < state.next_offset:
            logger.warning("%s: next_offset went backwards; using Meta's value unchanged", LABEL)
        state.next_offset = updates.next_offset
        state.save()
        self._save_failures = 0
        if handed:
            logger.info("%s: handed %d message(s) to Hermes", LABEL, handed)

    async def _authorize(self, sender: str, wamid: str) -> bool:
        """True when ``sender`` may talk to Hermes. Raises ``AgentPlatformError`` when Meta
        cannot answer right now; the message is then re-checked on the next poll."""
        state = self._state
        client = self._client
        assert state is not None and client is not None
        if sender == state.creator:
            return True
        if sender not in self._non_creators:
            # Meta accepts a read receipt only for the creator's own messages (403/131005
            # otherwise), which makes it an authoritative creator check. The receipt (with the
            # typing indicator when enabled) is wanted for the creator anyway.
            try:
                await client.mark_read(wamid, typing=self._typing_enabled())
            except NotCreatorError:
                self._non_creators.add(sender)
            except NotSent as exc:
                if isinstance(exc, Retryable):
                    raise  # transient: re-check on the next poll
                # A permanent rejection is not a confirmation: treat the sender as unverified.
                logger.warning("%s: could not confirm a sender with Meta (%s)", LABEL, exc.describe())
            else:
                self._note_status(wamid)
                previous, state.creator = state.creator, sender
                if previous is None:
                    logger.info("%s: agent creator confirmed by Meta", LABEL)
                else:
                    logger.warning("%s: the creator's WhatsApp id changed (confirmed by Meta)", LABEL)
                return True
        if str(get_scoped_secret(ALLOW_ALL_ENV, "") or "").strip().lower() in _TRUTHY:
            return True
        if sender in _csv_env(ALLOWED_ENV):
            return True
        logger.warning(
            "%s: dropped a message from a sender who is not the agent's creator (id %s); add their "
            "user id to %s to allow them",
            LABEL,
            _id_hint(sender),
            ALLOWED_ENV,
        )
        return False

    async def _handle_inbound(self, message: dict[str, Any]) -> bool:
        """Dispatch one inbound message; True when handed to Hermes."""
        state = self._state
        assert state is not None
        wamid, sender, mtype = message.get("id"), message.get("from"), message.get("type")
        if not isinstance(wamid, str) or not wamid or not isinstance(sender, str) or not _USER_ID_RE.match(sender):
            return self._skip_malformed(wamid, "id or sender")
        if state.seen(wamid):
            return False
        if not isinstance(mtype, str) or (mtype == "text" and _text_body(message) is None):
            return self._skip_malformed(wamid, "type or text")
        sent_at = _timestamp(message.get("timestamp"))
        if sent_at is None:
            # Meta always sends one; if it ever doesn't (or sends garbage), the message itself is still good.
            logger.warning("%s: inbound message without a usable timestamp; using the arrival time", LABEL)
            sent_at = int(time.time())
        if sent_at < state.start_timestamp - CLOCK_SKEW:
            return False  # retained backlog from before first activation
        if mtype == "reaction":
            # Before _authorize on purpose: a reaction never calls /statuses (no read receipt, no typing) and
            # never pins the creator. It reaches hooks only, never the agent.
            state.remember(wamid)
            await self._forward_reaction(message, sender, wamid, sent_at)
            return False
        if not await self._authorize(sender, wamid):
            state.remember(wamid)
            return False

        self._latest_inbound[sender] = wamid
        self._latest_inbound.move_to_end(sender)
        while len(self._latest_inbound) > 256:
            self._latest_inbound.popitem(last=False)
        if wamid not in self._status_at:
            self._note_status(wamid)
            self._spawn(self._post_status(wamid, self._typing_enabled()))

        inbound: _InboundMedia | None = None
        if mtype in media.MEDIA_KINDS and self._media_enabled:
            # Only after a positive _authorize, and inline: the offset is saved once the page is handed off.
            inbound = await self._receive_media(message, sender, wamid)
            text = inbound.text
        else:
            text = _text_body(message) if mtype == "text" else None
            if not isinstance(text, str) or not text.strip():
                self._reject_unsupported(sender, wamid, mtype)
                return False

        quoted_media = await self._quoted_media(sender, message) if self._media_enabled else []
        event = self._build_event(message, sender, wamid, text, sent_at, inbound, quoted_media)
        try:
            await self.handle_message(event)
        except Exception as exc:
            attempts = self._handoff_failures.get(wamid, 0) + 1
            self._handoff_failures[wamid] = attempts
            if attempts < HANDOFF_ATTEMPTS:
                logger.error(
                    "%s: Hermes rejected an inbound message (attempt %d/%d)",
                    LABEL,
                    attempts,
                    HANDOFF_ATTEMPTS,
                    exc_info=True,
                )
                raise HandoffError(type(exc).__name__) from exc
            logger.error("%s: skipping a message Hermes rejected %d times", LABEL, attempts, exc_info=True)
        self._handoff_failures.pop(wamid, None)
        self._prepared_media.pop(wamid, None)
        state.remember(wamid)
        if inbound is not None:
            # Persist the dedup window now (the offset is unchanged): a restart later in this page must not
            # replay a message whose media was already downloaded and handed off.
            state.save()
        return True

    def _reject_unsupported(self, sender: str, wamid: str, mtype: Any) -> None:
        """A type we can't hand to Hermes: remember it and tell the sender, unless warnings are suppressed."""
        assert self._state is not None
        self._state.remember(wamid)
        notice = UNSUPPORTED_TYPE_NOTICE if self._media_enabled else UNSUPPORTED_NOTICE
        try:
            visible = self.warning_text(notice)  # honours display.suppress_warning_notifications
        except Exception:
            visible = notice
        shown = mtype if mtype in DOCUMENTED_TYPES else "other"
        if not visible:
            logger.info("%s: unsupported inbound type %r; notice suppressed", LABEL, shown)
            return
        logger.info("%s: unsupported inbound type %r; notified sender", LABEL, shown)
        self._spawn(self._send_notice(sender, visible))

    def _skip_malformed(self, wamid: Any, what: str) -> bool:
        """A message whose envelope Meta shouldn't send: skip it (no read receipt, no notice, no content logged)
        and remember its id when it has a usable one, so a re-poll doesn't look at it again."""
        logger.warning("%s: malformed inbound message skipped (%s)", LABEL, what)
        if _rememberable(wamid) and self._state is not None:
            self._state.remember(wamid)
        return False

    def _skip_unhandled(self, message: dict[str, Any], exc: Exception) -> None:
        """Last resort for a message that raised while being handled: skip it and keep polling (the offset
        advances with the rest of the page). Only the exception type and location are logged, never its text."""
        logger.error(
            "%s: skipped an inbound message that could not be handled (%s at %s)",
            LABEL,
            type(exc).__name__,
            _where(exc),
        )
        wamid = message.get("id") if isinstance(message, dict) else None
        if _rememberable(wamid) and self._state is not None:
            self._state.remember(wamid)

    # ------------------------------------------------------------ inbound media
    async def _receive_media(self, message: dict[str, Any], sender: str, wamid: str) -> _InboundMedia:
        """Download and cache one media message's attachment. Never raises (except on cancellation): any
        failure becomes a bracketed note for the agent, and the message is still dispatched with its caption."""
        prepared = self._prepared_media.get(wamid)
        if prepared is not None:  # a Hermes handoff retry: don't download again
            return prepared
        kind = str(message.get("type"))
        try:
            item = media.parse_inbound(message)
        except Exception:  # ValueError for a malformed media object; anything else is just as unusable
            item = None
        if item is None:
            logger.warning("%s: inbound %s with a malformed media object; told the agent", LABEL, kind)
            result = _InboundMedia(failure_note(media_inbound.FAILED, _media_label(kind)), MessageType.TEXT)
            return self._keep_prepared(wamid, result)
        caption = item.caption or ""
        label = _media_label(item.kind, voice=item.voice, filename=item.filename)
        reason, size, limit = None, None, None
        try:
            cap = media.inbound_cap(item.kind, compat.inbound_media_max_bytes())
            data = await fetch_inbound_media(self._client, item, max_bytes=cap, **self._fetch_options())
            result = await self._cache_media(item, data, caption)
        except InboundMediaError as exc:
            reason, size, limit = exc.reason, exc.size, exc.limit
        except ValueError:  # the image cache refused the bytes (not an image it can use)
            reason = media_inbound.UNSUPPORTED
        except Exception as exc:  # OSError from a cache write, anything unexpected: never reaches the poll loop
            logger.warning("%s: caching inbound %s failed (%s)", LABEL, item.kind, type(exc).__name__)
            reason = media_inbound.FAILED
        if reason is not None:
            hint = media_hint(item.media_id)
            logger.warning("%s: inbound %s %s not received (%s); told the agent", LABEL, item.kind, hint, reason)
            note = failure_note(reason, label, size=size, limit=limit)
            result = _InboundMedia(f"{caption}\n\n{note}" if caption else note, MessageType.TEXT)
        else:
            await compat.record_media(sender, wamid, list(zip(result.urls, result.types, strict=True)))
            logger.info("%s: received %s %s", LABEL, item.kind, media_hint(item.media_id))
        return self._keep_prepared(wamid, result)

    def _fetch_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {}
        if self._media_timeout is not None:
            options["timeout"] = self._media_timeout
        if self._media_retry_wait is not None:
            options["retry_wait_cap"] = self._media_retry_wait
        return options

    def _keep_prepared(self, wamid: str, result: _InboundMedia) -> _InboundMedia:
        self._prepared_media[wamid] = result
        self._prepared_media.move_to_end(wamid)
        while len(self._prepared_media) > PREPARED_MEDIA_MAX:
            self._prepared_media.popitem(last=False)
        return result

    async def _cache_media(self, item: media.InboundMedia, data: bytes, caption: str) -> _InboundMedia:
        """Cache the bytes with Hermes's kind-specific helper and describe them for the event.

        Raises ``ValueError`` when the image cache refuses an image or sticker it can't fall back from, and
        ``OSError`` when a cache write fails (the caller turns both into notes).
        """
        kind = item.kind
        sniffed = media.sniff_mime(data[:4096])
        mime = item.mime or sniffed or _DEFAULT_MIME[kind]
        if kind == "audio":
            path = await compat.cache_audio_from_bytes_async(data, media.ext_for_mime(mime) or ".ogg")
            voice = MessageType.VOICE if item.voice else MessageType.AUDIO  # only voice notes are transcribed
            return _single(path, mime, voice, caption)
        if kind == "video":
            path = await compat.cache_video_from_bytes_async(data, media.ext_for_mime(mime) or ".mp4")
            return _single(path, mime, MessageType.VIDEO, caption)
        if kind == "sticker":
            path = await compat.cache_image_from_bytes_async(data, ".webp")
            return _single(path, "image/webp", MessageType.PHOTO, caption or STICKER_NOTE)
        refused_image = False
        if kind == "image" or mime.startswith("image/"):
            try:
                path = await compat.cache_image_from_bytes_async(data, media.ext_for_mime(mime) or ".jpg")
            except ValueError:
                if kind == "image":  # only formats the image cache refuses by design go on as a document
                    refused = mime if mime in _REFUSED_IMAGES else sniffed
                    if refused not in _REFUSED_IMAGES:
                        raise
                    mime = refused
                refused_image = True
            else:
                return _single(path, mime, MessageType.PHOTO, caption)  # a photo sent as a document, too
        name = _document_name(item.filename, mime, "image" if kind == "image" else "document")
        path = await compat.cache_document_from_bytes_async(data, name)
        try:
            _restrict_permissions(path)
            inline = media.should_inline_text(name, mime, data)
        except BaseException:
            _discard(path)
            raise
        text = caption
        # The MIME Hermes routes by (gateway/run.py): per attachment, it wins over the message type.
        if inline is not None:
            header = f"[Content of {_DISPLAY_UNSAFE.sub('_', name)}]:\n{inline}"
            text = f"{header}\n\n{caption}" if caption else header
            routed = mime if mime.startswith("text/") else "text/plain"  # else: a "binary, extract it" note
        elif refused_image:
            routed = media.OCTET_STREAM  # an image/* MIME would send it to vision, which can't read it
        else:
            routed = mime
        return _InboundMedia(text, MessageType.DOCUMENT, [path], [routed], [inline is not None])

    async def _quoted_media(self, sender: str, message: dict[str, Any]) -> list[tuple[str, str]]:
        """Attachments recorded for the quoted message (ours or the user's), so a reply re-attaches them."""
        context = message.get("context")
        quoted = context.get("id") if isinstance(context, dict) else None
        if not isinstance(quoted, str) or not quoted:
            return []
        return await compat.lookup_media(sender, quoted)

    # --------------------------------------------------------------- reactions
    def _reaction_sender_allowed(self, sender: str) -> bool:
        """The creator Meta already confirmed, or an allowed sender. Never probes Meta, never pins the creator."""
        state = self._state
        if state is not None and state.creator is not None and sender == state.creator:
            return True
        if str(get_scoped_secret(ALLOW_ALL_ENV, "") or "").strip().lower() in _TRUTHY:
            return True
        return sender in _csv_env(ALLOWED_ENV)

    async def _forward_reaction(self, message: dict[str, Any], sender: str, wamid: str, sent_at: int) -> None:
        """Emit a reaction to Hermes's hooks: ``reaction:added``/``reaction:removed`` (via the reaction handler)
        and the ``gateway_platform_event`` plugin hook. No agent turn. Never raises into the poll loop."""
        try:
            reaction = message.get("reaction")
            target = reaction.get("message_id") if isinstance(reaction, dict) else None
            if not _valid_wamid(target):
                logger.debug("%s: malformed reaction skipped", LABEL)
                return
            if not self._reaction_sender_allowed(sender):
                logger.debug("%s: reaction from a sender who is not allowed (id %s) dropped", LABEL, _id_hint(sender))
                return
            # Meta delivers additions only (live); an empty or missing emoji is treated as a removal.
            emoji = _clean_emoji(reaction.get("emoji"))
            action = "added" if emoji else "removed"
            raw_event = {
                "id": wamid,
                "type": "reaction",
                "timestamp": str(sent_at),
                "reaction": {"message_id": target, "emoji": emoji},
            }
            handler = getattr(self, "_reaction_handler", None)
            if handler is not None:
                own = await self._is_own_message(sender, target)
                context = {
                    "platform": PLATFORM_NAME,
                    "event_name": f"reaction:{action}",
                    "reaction": emoji,
                    "user_id": sender,
                    "item_user_id": "agent" if own else None,
                    "item_type": "message",
                    "channel_id": sender,
                    "message_ts": target,
                    "team_id": None,
                    "event_ts": str(sent_at),
                    "raw_event": raw_event,
                }
                await self._run_hook("reaction hook", lambda: handler(context))
            event_handler = getattr(self, "_platform_event_handler", None)
            if event_handler is not None:
                name = self._names.get(sender)
                source = self.build_source(
                    chat_id=sender,
                    chat_name=name or "WhatsApp",
                    chat_type="dm",
                    user_id=sender,
                    user_name=name,
                    message_id=wamid,
                )
                event = {
                    "platform": PLATFORM_NAME,
                    "event_type": "reaction",
                    "payload": {
                        "emojis": [emoji] if emoji else [],
                        "custom_emoji_ids": [],
                        "chat_id": sender,
                        "message_id": target,
                        "thread_id": None,
                    },
                }
                await self._run_hook("gateway_platform_event dispatch", lambda: event_handler(event, source))
            logger.debug("%s: reaction %s forwarded to hooks", LABEL, action)
        except Exception:
            logger.debug("%s: reaction handling failed", LABEL, exc_info=True)

    async def _run_hook(self, what: str, call) -> None:
        """Await one hook, bounded by ``_hook_timeout``: hooks run inside the poll loop, so a slow one would delay
        every later message. A hook that blocks the event loop synchronously can't be bounded this way."""
        try:
            await asyncio.wait_for(call(), self._hook_timeout)
        except TimeoutError:
            logger.warning("%s: %s took longer than %gs; abandoned", LABEL, what, self._hook_timeout)
        except Exception:
            logger.debug("%s: %s failed", LABEL, what, exc_info=True)

    async def _is_own_message(self, chat_id: str, message_id: str) -> bool:
        """True when ``message_id`` is a message this adapter sent: a text recorded in ``rich_sent_store``, or an
        attachment recorded there that the user didn't send (inbound media is recorded too, for quotes, but the
        user's own message ids are in the dedup window)."""
        with contextlib.suppress(Exception):
            from gateway import rich_sent_store

            if await asyncio.to_thread(rich_sent_store.lookup, chat_id, message_id):
                return True
        if self._state is not None and self._state.seen(message_id):
            return False
        return bool(await compat.lookup_media(chat_id, message_id))

    def _build_event(
        self,
        message: dict[str, Any],
        sender: str,
        wamid: str,
        text: str,
        sent_at: int,
        inbound: _InboundMedia | None = None,
        quoted_media: list[tuple[str, str]] | None = None,
    ) -> MessageEvent:
        name = self._names.get(sender)
        context = message.get("context") if isinstance(message.get("context"), dict) else {}
        quoted = context.get("id") if isinstance(context.get("id"), str) else None
        # The three media lists stay aligned: one type and one inlined flag per path, quoted media included.
        urls = list(inbound.urls) if inbound else []
        types = list(inbound.types) if inbound else []
        inlined = list(inbound.inlined) if inbound else []
        for path, mime in quoted_media or ():
            if path and path not in urls:
                urls.append(path)
                types.append(media.base_mime(mime) or "")
                inlined.append(False)
        quoted_own = bool(quoted and str(context.get("from", "")).startswith("agent:"))
        reply_text = None
        if quoted_own:
            with contextlib.suppress(Exception):
                from gateway import rich_sent_store

                reply_text = rich_sent_store.lookup(sender, quoted)
        source = self.build_source(
            chat_id=sender,
            chat_name=name or "WhatsApp",
            chat_type="dm",
            user_id=sender,
            user_name=name,
            message_id=wamid,
        )
        return MessageEvent(
            text=text,
            message_type=inbound.message_type if inbound else MessageType.TEXT,
            source=source,
            user_id=sender,
            user_name=name,
            message_id=wamid,
            timestamp=datetime.fromtimestamp(sent_at, tz=UTC),
            raw_message=message,
            reply_to_message_id=quoted,
            reply_to_text=reply_text,
            reply_to_is_own_message=quoted_own,
            media_urls=urls,
            media_types=types,
            media_text_inlined=inlined,
        )

    # --------------------------------------------------------------- outbound
    def _resolve_recipient(self, chat_id: str) -> str | None:
        if chat_id in (SELF_TARGET, "") and self._state is not None:
            return self._state.creator
        return chat_id if _USER_ID_RE.match(chat_id or "") else None

    async def send(self, chat_id: str, content: str, reply_to: str | None = None, metadata: Any = None) -> SendResult:
        if not content or not content.strip():
            return SendResult(success=True)
        client = self._client
        if client is None or client.closed:
            # Hermes's delivery ledger redelivers this once the platform reconnects.
            return SendResult(success=False, error="send_path_degraded", raw_response={"final": True})
        to = self._resolve_recipient(chat_id)
        if to is None:
            return SendResult(
                success=False,
                error="recipient unknown: the agent's creator has not been confirmed yet (message the agent once)",
                error_kind="unknown",
                raw_response={"final": True},
            )
        if isinstance(metadata, dict) and metadata.get("_interim_send"):
            if client.limits["messages"].remaining() <= INTERIM_SEND_RESERVE:
                logger.debug("%s: interim message dropped to save send budget", LABEL)
                return SendResult(success=True)
        return await self._send_chunks(to, _chunks(to_whatsapp(content)), reply_to=reply_to)

    async def _send_chunks(
        self, to: str, chunks: list[str], *, reply_to: str | None, delivered: tuple[str, ...] = ()
    ) -> SendResult:
        if self._client is None:
            return SendResult(success=False, error="send_path_degraded", raw_response={"final": True})
        async with self._send_lock:  # the manual forbids concurrent sends to one recipient
            return await self._send_chunks_locked(to, chunks, reply_to=reply_to, delivered=delivered)

    async def _send_chunks_locked(
        self, to: str, chunks: list[str], *, reply_to: str | None, delivered: tuple[str, ...] = ()
    ) -> SendResult:
        """``_send_chunks`` for a caller already holding ``_send_lock`` (a media caption sent as text)."""
        client = self._client
        if client is None:
            return SendResult(success=False, error="send_path_degraded", raw_response={"final": True})
        ids = list(delivered)
        for index, chunk in enumerate(chunks):
            quote = reply_to if index == 0 and not delivered else None
            try:
                wamid = await client.send_text(to, chunk, reply_to=quote)
            except AgentPlatformError as exc:
                if not (quote and self._is_stale_quote(exc)):
                    return self._send_failure(exc, to, ids, chunks[index:])
                # The quoted message is gone: nothing was sent, so send without the quote.
                try:
                    wamid = await client.send_text(to, chunk)
                except AgentPlatformError as retry_exc:
                    return self._send_failure(retry_exc, to, ids, chunks[index:])
            ids.append(wamid)
            self._reply_sent_at[to] = time.monotonic()
            with contextlib.suppress(Exception):
                from gateway import rich_sent_store

                rich_sent_store.record(to, wamid, chunk)
        logger.info("%s: response sent (%d message(s))", LABEL, len(ids) - len(delivered))
        return SendResult(success=True, message_id=ids[-1] if ids else None, continuation_message_ids=tuple(ids[:-1]))

    @staticmethod
    def _is_stale_quote(exc: AgentPlatformError) -> bool:
        """Text only. The media path never uses this: there code 100/131009 mean a bad caption, media type or
        size (``MediaRejected``), so it has its own narrow check (``media_outbound.is_stale_media_quote``)."""
        return type(exc) is NotSent and exc.status == 400 and exc.code in (CODE_INVALID_PARAMETER, CODE_BAD_FIELD)

    @staticmethod
    def _send_failure(exc: AgentPlatformError, to: str, delivered: list[str], remaining: list[str]) -> SendResult:
        """Map a failed chunk to a ``SendResult`` Hermes's retry loop and delivery ledger
        classify correctly (``flood_control:<s>`` for rate limits, "forbidden" for a
        non-creator recipient, final for anything a retry cannot fix)."""
        raw: dict[str, Any] = {
            "to": to,
            "delivered": tuple(delivered),
            "remaining": remaining,
            "resumable": isinstance(exc, Retryable),
        }
        if delivered:
            raw["partial_overflow"] = True
        detail = exc.describe()
        logger.warning("%s: send failed: %s", LABEL, detail)
        if isinstance(exc, RateLimited):
            wait = 10.0 if exc.retry_after is None else exc.retry_after
            return SendResult(
                success=False,
                error=f"flood_control:{wait:g}",
                raw_response=raw,
                retry_after=wait,
                error_kind="rate_limited",
            )
        if isinstance(exc, Retryable):
            return SendResult(
                success=False,
                error=f"network error: {detail}",
                raw_response=raw,
                retryable=True,
                retry_after=exc.retry_after,
                error_kind="transient",
            )
        raw["final"] = True
        if isinstance(exc, (MediaRejected, MediaGone)):
            # Meta refused the file or its fields (or its id expired twice): resending it cannot help.
            return SendResult(
                success=False,
                error=f"media rejected: {detail}",
                raw_response=raw,
                retryable=False,
                error_kind="unknown",
            )
        if isinstance(exc, NotCreatorError):
            return SendResult(success=False, error=f"forbidden: {detail}", raw_response=raw, error_kind="forbidden")
        if isinstance(exc, AuthError):
            return SendResult(success=False, error=f"api key rejected: {detail}", raw_response=raw)
        if isinstance(exc, NotSent):
            return SendResult(success=False, error=f"rejected: {detail}", raw_response=raw, error_kind="unknown")
        # Ambiguous or a malformed 2xx: maybe delivered. Not retried inline; the delivery
        # ledger re-sends the reply later, marked as a possible duplicate.
        return SendResult(success=False, error=f"outcome unknown: {detail}", raw_response=raw, error_kind="unknown")

    def _send_retry_is_final(self, result: SendResult) -> bool:
        raw = result.raw_response
        return isinstance(raw, dict) and bool(raw.get("final"))

    async def _resume_partial_send(
        self, chat_id: str, result: SendResult, *, reply_to: str | None, metadata: Any
    ) -> SendResult | None:
        raw = result.raw_response
        if not (isinstance(raw, dict) and raw.get("resumable") and raw.get("remaining")):
            return None
        return await self._send_chunks(
            raw["to"], list(raw["remaining"]), reply_to=None, delivered=tuple(raw.get("delivered", ()))
        )

    async def _send_plain_fallback(
        self, chat_id: str, content: str, *, reply_to: str | None, metadata: Any
    ) -> SendResult:
        # Plain WhatsApp text has no markup that could fail; re-sending would only duplicate.
        return SendResult(success=False, error="plain-text fallback not applicable", error_kind="unknown")

    async def _send_notice(self, to: str, text: str) -> None:
        client = self._client
        if client is None or client.limits["messages"].remaining() <= INTERIM_SEND_RESERVE:
            return
        with contextlib.suppress(AgentPlatformError):
            async with self._send_lock:
                await client.send_text(to, text)
                self._reply_sent_at[to] = time.monotonic()

    # ---------------------------------------------------------- outbound media
    # Parameter names, order and defaults are Hermes's own (cron and Kanban call with keywords only, the
    # send_message tool positionally, the reply path adds ``is_voice=``). ``send_multiple_images`` stays the
    # base loop over ``send_image_file``, which keeps its "at least one image delivered" result.
    _media_wait_bound = media_outbound.WAIT_BOUND
    _media_pacing_after = media_outbound.PACING_NOTICE_AFTER
    _media_retry_backoff = media_outbound.RETRY_BACKOFF
    _media_pacing_interval = 60.0  # at most one pacing notice per recipient per window
    _media_url_timeout = 60.0  # seconds for Hermes's download of an image URL (send_image), then the link

    def _media_on(self) -> bool:
        return bool(getattr(self, "_media_enabled", True))

    @staticmethod
    def _media_off_result() -> SendResult:
        """Media switched off: a final failure, never a notice reported as success. Hermes's reply path then
        adds its own "couldn't deliver" notice for non-image files, and the send_message tool and cron's live
        lane report the file as not sent (as v0.1.x did, where these methods were not overridden)."""
        logger.info("%s: attachment not sent: media is turned off", LABEL)
        return SendResult(
            success=False, error=MEDIA_OFF_ERROR, retryable=False, error_kind="unknown", raw_response={"final": True}
        )

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> SendResult:
        if not self._media_on():
            return self._media_off_result()
        return (await self._send_media(chat_id, image_path, "image", caption, reply_to, metadata, kwargs))[0]

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> SendResult:
        if not self._media_on():
            return self._media_off_result()
        return (await self._send_media(chat_id, video_path, "video", caption, reply_to, metadata, kwargs))[0]

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> SendResult:
        """Audio always arrives as a plain attachment on this platform (no voice bubble), so ``is_voice`` is
        ignored; a caption goes first as text, because audio takes none."""
        if not self._media_on():
            return self._media_off_result()
        return (await self._send_media(chat_id, audio_path, "voice", caption, reply_to, metadata, kwargs))[0]

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: str | None = None,
        file_name: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> SendResult:
        """The bytes go unmodified as a document (Hermes routes ``[[as_document]]`` files here)."""
        if not self._media_on():
            return self._media_off_result()
        kwargs["file_name"] = file_name
        return (await self._send_media(chat_id, file_path, "document", caption, reply_to, metadata, kwargs))[0]

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> SendResult:
        """Fetch the URL with Hermes's SSRF-guarded image cache (bounded in time and size), then send it like a
        local image. The link goes as text (the base behaviour) only when the image certainly did not arrive:
        the fetch failed, the file was refused before its upload, or Meta rejected it. After a send that may
        have delivered it (outcome unknown), a refused recipient (forbidden), a send-limit wait (flood_control)
        or a failed caption text, that result is returned as is: a link could duplicate the image, or would be
        refused the same way. A caption that already went as its own text is not repeated with the link. The
        plugin itself never requests the URL. With media off the link goes as text, as in v0.1.x."""
        if not self._media_on():
            return await super().send_image(chat_id, image_url, caption=caption, reply_to=reply_to, metadata=metadata)
        refused = self._media_precheck(chat_id)
        if refused is not None:
            return refused
        path = await self._fetch_image_url(image_url)
        if path is None:
            return await super().send_image(chat_id, image_url, caption=caption, reply_to=reply_to, metadata=metadata)
        result, outcome = await self._send_media(chat_id, path, "image", caption, reply_to, metadata, {})
        if result.success or not self._media_certainly_unsent(outcome):
            return result
        logger.info("%s: image from a URL not sent; sending its link instead", LABEL)
        if outcome is not None and outcome.caption_sent:  # the caption (with the quote) is already in the chat
            return await super().send_image(chat_id, image_url, caption=None, reply_to=None, metadata=metadata)
        return await super().send_image(chat_id, image_url, caption=caption, reply_to=reply_to, metadata=metadata)

    async def _fetch_image_url(self, url: str) -> str | None:
        """Hermes's SSRF-guarded download into its image cache, bounded to ``_media_url_timeout`` seconds and
        ``URL_IMAGE_MAX_BYTES``. None when it failed; the caller then sends the link."""
        try:
            path = await asyncio.wait_for(compat.cache_image_from_url(url), self._media_url_timeout)
        except Exception as exc:  # SSRF refusal, HTTP error, time limit, not an image, over Hermes's own cap
            logger.info("%s: image URL not fetched (%s); sending it as a link", LABEL, type(exc).__name__)
            return None
        try:
            size = os.path.getsize(path)
        except (OSError, TypeError, ValueError) as exc:
            logger.info("%s: fetched image unreadable (%s); sending it as a link", LABEL, type(exc).__name__)
            return None
        if size > URL_IMAGE_MAX_BYTES:
            with contextlib.suppress(OSError):
                os.remove(path)
            logger.info("%s: image URL too large (%d bytes); sending it as a link", LABEL, size)
            return None
        return path

    def _media_precheck(self, chat_id: str) -> SendResult | None:
        """Before any fetch or upload: a live client and a recipient Meta will deliver to (only the creator)."""
        client = self._client
        if client is None or client.closed:
            return SendResult(success=False, error="send_path_degraded", raw_response={"final": True})
        to = self._resolve_recipient(chat_id)
        creator = self._state.creator if self._state is not None else None
        if to is None or creator is None:
            return SendResult(
                success=False,
                error="recipient unknown: the agent's creator has not been confirmed yet (message the agent once)",
                error_kind="unknown",
                raw_response={"final": True},
            )
        if to != creator or to in self._non_creators:
            logger.warning("%s: media not sent to %s: not the agent's creator", LABEL, _id_hint(to))
            return SendResult(
                success=False,
                error="forbidden: media can only be sent to the agent's creator",
                error_kind="forbidden",
                raw_response={"final": True},
            )
        return None

    @staticmethod
    def _media_certainly_unsent(outcome: media_outbound.MediaOutcome | None) -> bool:
        """The image certainly did not arrive and a link cannot duplicate it: refused before the upload
        (unreadable, too big, not preparable) or refused by Meta (``MediaRejected``, or ``MediaGone`` after the
        one re-upload). Never after an outcome unknown, a forbidden recipient, a send-limit wait or a caption
        text that failed."""
        if outcome is None or outcome.text_failure is not None or outcome.wait is not None:
            return False
        if outcome.refusal is not None:
            return True
        return isinstance(outcome.error, (MediaRejected, MediaGone))

    def _media_deny_dirs(self) -> list[str]:
        """The plugin's own state directory is never uploaded (on top of ``media.is_denied_path``'s names)."""
        if self._state is not None:
            return [str(self._state.path.parent)]
        return [str(get_hermes_home() / "platforms" / PLATFORM_NAME)]

    async def _send_media(
        self,
        chat_id: str,
        path: str,
        requested: str,
        caption: str | None,
        reply_to: str | None,
        metadata: Any,
        kwargs: dict[str, Any],
    ) -> tuple[SendResult, media_outbound.MediaOutcome | None]:
        refused = self._media_precheck(chat_id)
        if refused is not None:
            return refused, None
        client = self._client
        to = self._resolve_recipient(chat_id)
        assert client is not None and to is not None

        async def caption_text(text: str, quote: str | None) -> None:
            # Runs while media_outbound holds _send_lock: the normal text path, minus the lock.
            result = await self._send_chunks_locked(to, _chunks(to_whatsapp(text)), reply_to=quote)
            if not result.success:
                raise media_outbound.TextNotSent(result)

        async def pacing(wait: float) -> None:
            await self._media_pacing_notice(chat_id, to, wait, metadata)

        pending: collections.Counter[str] = self.__dict__.setdefault("_media_pending", collections.Counter())
        pending[to] += 1  # files in progress for this recipient: the pacing notice says "more files" only if > 1
        try:
            outcome = await media_outbound.send_media_file(
                client,
                to,
                path,
                requested=requested,
                caption=caption,
                reply_to=reply_to,
                file_name=kwargs.get("file_name"),
                force_document=requested == "document" or bool(kwargs.get("force_document")),
                transcoder=compat.transcode_to_ogg_opus,
                deny_dirs=self._media_deny_dirs(),
                lock=self._send_lock,
                send_text=caption_text,
                notify_pacing=pacing,
                note_filter=self._media_note,
                wait_bound=self._media_wait_bound,
                pacing_after=self._media_pacing_after,
                retry_backoff=self._media_retry_backoff,
            )
        finally:
            pending[to] -= 1
            if pending[to] <= 0:
                del pending[to]
        return await self._media_result(outcome, to), outcome

    async def _media_result(self, outcome: media_outbound.MediaOutcome, to: str) -> SendResult:
        if outcome.success:
            self._reply_sent_at[to] = time.monotonic()
            # So a quote of this attachment re-attaches it (lookup drops files that no longer exist).
            await compat.record_media(to, outcome.message_id, [(outcome.path, outcome.mime)])
            return SendResult(success=True, message_id=outcome.message_id)
        if outcome.text_failure is not None:
            if isinstance(outcome.text_failure, SendResult):
                return outcome.text_failure
            return SendResult(success=False, error="caption not sent", error_kind="unknown")
        if outcome.refusal is not None:
            # Base sends its own "couldn't deliver" notice for non-image files; none is added here.
            return SendResult(
                success=False,
                error=f"media not sent: {outcome.refusal}",
                retryable=False,
                error_kind="unknown",
                raw_response={"final": True},
            )
        if outcome.wait is not None:
            wait = float(max(1, round(outcome.wait)))
            logger.warning("%s: media not sent: send budget exhausted for %.0fs", LABEL, wait)
            return SendResult(
                success=False,
                error=f"flood_control:{wait:g}",
                retry_after=wait,
                error_kind="rate_limited",
                raw_response={"final": True},
            )
        assert outcome.error is not None
        return self._send_failure(outcome.error, to, [], [])

    def _media_note(self, note: str) -> str | None:
        """The "sent as a file" caption note, hidden when the operator suppresses warning notices."""
        warning_text = getattr(self, "warning_text", None)
        if warning_text is None:
            return note
        try:
            return warning_text(note, "")
        except Exception:
            return note

    async def _media_pacing_notice(self, chat_id: str, to: str, wait: float, metadata: Any) -> None:
        """One "pausing for the rate limit" notice (Signal's pattern), only when it can go out now: when the
        messages budget itself is what we wait for, a notice would only arrive with the file."""
        emit = getattr(self, "emit_warning", None)
        client = self._client
        if emit is None or client is None or client.limits["messages"].remaining() <= 0:
            return
        sent_at: dict[str, float] = self.__dict__.setdefault("_media_pacing_at", {})
        now = time.monotonic()
        if now - sent_at.get(to, float("-inf")) < self._media_pacing_interval:
            return
        sent_at[to] = now
        seconds = max(1, round(wait))
        if self.__dict__.get("_media_pending", {}).get(to, 0) > 1:
            text = f"(More files coming: pausing ~{seconds}s for WhatsApp's send limit.)"
        else:
            text = f"(A file is on its way: pausing ~{seconds}s for WhatsApp's send limit.)"
        await emit(chat_id, text, metadata=metadata)

    # ----------------------------------------------------------- read / typing
    def _note_status(self, wamid: str) -> None:
        self._status_at[wamid] = time.monotonic()
        self._status_at.move_to_end(wamid)
        while len(self._status_at) > 256:
            self._status_at.popitem(last=False)

    async def send_typing(self, chat_id: str, metadata: Any = None) -> None:
        """Refresh the typing indicator while Hermes works.

        The read receipt (plus the first indicator) is sent when the message arrives. Hermes
        calls this every ~2 s and cancels each call after 1.5 s, so the POST runs as a
        background task and is repeated only every 20 s (Meta's indicator lasts 25 s and
        ``/statuses`` allows 12/min).
        """
        if not self._typing_enabled() or self._client is None:
            return
        to = self._resolve_recipient(chat_id)
        wamid = self._latest_inbound.get(to) if to else None
        if not wamid:
            return
        now = time.monotonic()
        last = self._status_at.get(wamid)
        if last is not None and now - last < TYPING_REFRESH:
            return
        if now - self._reply_sent_at.get(to, 0.0) < REPLY_QUIET:
            return
        self._note_status(wamid)
        self._spawn(self._post_status(wamid, True))

    async def _post_status(self, wamid: str, typing: bool) -> None:
        client = self._client
        if client is None:
            return
        try:
            await client.mark_read(wamid, typing=typing)
        except AgentPlatformError as exc:
            logger.debug("%s: read/typing status not accepted (%s)", LABEL, exc.describe())

    async def stop_typing(self, chat_id: str) -> None:
        """Meta clears the indicator when the reply arrives (or after 25 s)."""

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": self._names.get(chat_id) or "WhatsApp", "type": "dm", "chat_id": chat_id}


# ---------------------------------------------------------------- registration
def _has_key(config: Any = None) -> bool:
    return bool(_api_key())


def _env_enablement() -> dict[str, Any] | None:
    if not _api_key():
        return None
    return seed_extra_from_env(((MEDIA_ENV, MEDIA_KEY, _on_unless_falsy),), home_env=HOME_ENV, home_default=SELF_TARGET)


def _parse_target(ref: str) -> tuple[str, str | None] | None:
    ref = (ref or "").strip()
    if ref == SELF_TARGET or _USER_ID_RE.match(ref):
        return ref, None
    return None


STANDALONE_MEDIA_DEADLINE = 45.0  # seconds for text + attachments; Hermes cancels the whole send at 60 s
URL_IMAGE_MAX_BYTES = media.MAX_UPLOAD_BYTES  # a larger downloaded image is dropped and the link sent instead
MEDIA_OFF_ERROR = "media disabled: media is turned off for WhatsApp Agent Platform"


def _standalone_media_paths(media_files: Any) -> list[str]:
    """Hermes passes ``(path, is_voice)`` tuples; bare paths are tolerated. ``is_voice`` changes nothing here:
    audio always arrives as a plain attachment on this platform."""
    paths: list[str] = []
    for item in media_files or ():
        path = item[0] if isinstance(item, (list, tuple)) and item else item
        try:
            paths.append(os.fspath(path))
        except TypeError:
            paths.append("")  # reported as "file not found" by send_media_file
    return paths


def _standalone_kind(path: str) -> str:
    """The ``send_*`` Hermes's live cron lane would pick, by extension (the media policy then routes by content)."""
    family = (media.MIME_BY_EXT.get(os.path.splitext(path)[1].lower()) or "").split("/", 1)[0]
    return {"image": "image", "video": "video", "audio": "voice"}.get(family, "document")


def _attachment_label(index: int, total: int, path: str) -> str:
    """Names an attachment in a warning by position and file name, never by its host path."""
    name = _DISPLAY_UNSAFE.sub("_", os.path.basename(path.replace("\\", "/")))[:80]
    return f"attachment {index + 1} of {total}" + (f" ({name})" if name else "")


def _standalone_failure(outcome: media_outbound.MediaOutcome) -> str:
    """A path-free reason for a file ``send_media_file`` did not deliver."""
    if outcome.refusal is not None:
        return outcome.refusal
    if outcome.wait is not None:
        return f"WhatsApp's send limit is reached (about {max(1, round(outcome.wait))}s to wait)"
    if outcome.error is not None:
        if isinstance(outcome.error, (MediaRejected, MediaGone)):
            return f"media rejected by Meta ({outcome.error.describe()})"
        return outcome.error.describe()
    return "caption not sent"


async def _standalone_send(
    pconfig: Any,
    chat_id: str,
    message: str,
    *,
    thread_id: str | None = None,
    media_files: list[tuple[str, bool]] | list[str] | None = None,
    force_document: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Out-of-process delivery (cron without a running gateway). The agent may message
    its creator first, so no inbound message is needed — only the creator's id, which the
    gateway records once Meta confirms it (or pass an explicit ``user:<id>``).

    The text goes first, then each attachment (``(path, is_voice)`` tuples, or bare paths) through the same
    ``media_outbound.send_media_file`` the gateway uses, with a fresh client and no caption. Attachments go only
    to the creator Meta confirmed. ``STANDALONE_MEDIA_DEADLINE`` bounds every file, including an upload or send
    already running (Hermes cancels the whole call at ``cron.standalone_send_timeout_seconds``, 60 s by
    default). Every file not delivered gets a ``warnings`` entry (cron reports those as run errors);
    ``media_delivered`` is set only when at least one file arrived. When nothing at all was delivered the result
    is an error. Non-empty ``thread_id`` values are rejected: this platform has no thread routing."""
    if thread_id:
        return send_error("thread_id is not supported by WhatsApp Agent Platform; omit it to send to the chat")
    started = time.monotonic()
    key = _api_key()
    if not key:
        return send_error(f"{KEY_ENV} is not set")
    home = get_hermes_home()
    target = (chat_id or "").strip()
    if target in (SELF_TARGET, ""):
        to = read_creator(home, key)
    elif _USER_ID_RE.match(target):
        to = target
    else:
        return send_error(f"invalid target {target!r}: use 'self' or user:<id>")
    if not to:
        return send_error(f"recipient unknown: send the agent one WhatsApp message first, or set {HOME_ENV}=user:<id>")
    paths = _standalone_media_paths(media_files)
    media_on = _media_enabled(pconfig)
    text = to_whatsapp(message or "")
    if paths and not media_on:
        text += f"\n\n[{len(paths)} attachment(s) not sent: media is turned off for WhatsApp Agent Platform]"
    client = AgentPlatformClient(key, base_url=_base_url())
    sent: list[str] = []
    try:
        try:
            for chunk in _chunks(text):
                sent.append(await client.send_text(to, chunk))
        except AgentPlatformError as exc:
            partial = f" after {len(sent)} of the message's parts were delivered" if sent else ""
            return send_error(f"{exc.describe()}{partial}")
        if not paths or not media_on:
            return {"success": True, "message_id": sent[-1] if sent else None}
        warnings, delivered = await _standalone_media(client, key, home, to, paths, force_document, started)
    finally:
        await client.aclose()
    if not sent and not delivered:
        return {**send_error("no attachment could be delivered: " + "; ".join(warnings)), "warnings": warnings}
    result: dict[str, Any] = {"success": True, "message_id": (delivered or sent)[-1]}
    if delivered:
        result["media_delivered"] = True
    if warnings:
        result["warnings"] = warnings
    return result


async def _standalone_media(
    client: AgentPlatformClient,
    key: str,
    home: Any,
    to: str,
    paths: list[str],
    force_document: bool,
    started: float,
) -> tuple[list[str], list[str]]:
    """Send each file; returns (warnings, delivered wamids)."""
    warnings: list[str] = []
    delivered: list[str] = []
    creator = read_creator(home, key)
    deny_dirs = [str(state_path(home, key).parent)]
    deadline = started + STANDALONE_MEDIA_DEADLINE
    time_limit = f"the {STANDALONE_MEDIA_DEADLINE:g}s time limit for this send"
    for index, path in enumerate(paths):
        label = _attachment_label(index, len(paths), path)
        if creator is None or to != creator:
            # The same rule as the gateway, checked before any upload: Meta delivers only to the creator.
            warnings.append(f"{label} not sent: media can only be sent to the agent's confirmed creator")
            continue
        left = deadline - time.monotonic()
        if left <= 0:
            warnings.append(f"{label} not sent: {time_limit} ran out")
            continue
        progress = media_outbound.MediaOutcome()
        try:
            # The deadline also bounds an upload or message already running, so the result (and its warnings)
            # is returned before Hermes's own timer cancels the whole send and discards it.
            outcome = await asyncio.wait_for(
                media_outbound.send_media_file(
                    client,
                    to,
                    path,
                    requested=_standalone_kind(path),
                    force_document=bool(force_document),
                    transcoder=compat.transcode_to_ogg_opus,
                    deny_dirs=deny_dirs,
                    lock=None,
                    wait_bound=left,
                    outcome=progress,
                ),
                timeout=left,
            )
        except TimeoutError:
            logger.warning("%s: scheduled attachment stopped at the time limit (%s)", LABEL, progress.stage)
            if progress.stage == media_outbound.STAGE_MESSAGE:
                # Cancelled while the media message was in flight: Meta may have delivered it.
                warnings.append(
                    f"{label}: outcome unknown ({time_limit} ran out while it was being sent; it may have arrived)"
                )
            else:  # still preparing or uploading: no message went out (an upload alone is never shown)
                warnings.append(f"{label} not sent: {time_limit} ran out")
            continue
        except Exception as exc:  # never lose the text's success over one file
            logger.warning("%s: scheduled attachment failed (%s)", LABEL, type(exc).__name__)
            warnings.append(f"{label} not sent: {type(exc).__name__}")
            continue
        if outcome.success:
            delivered.append(outcome.message_id)
        else:
            warnings.append(f"{label} not sent: {_standalone_failure(outcome)}")
    if delivered:
        logger.info("%s: scheduled send delivered %d of %d attachment(s)", LABEL, len(delivered), len(paths))
    return warnings, delivered


def _interactive_setup() -> None:
    """``hermes gateway setup`` flow: store the API key in the profile's ``.env``."""
    from hermes_cli.cli_output import print_header, print_info, prompt
    from hermes_cli.config import get_env_value, save_env_value
    from hermes_cli.setup_platforms import declines_reconfigure

    print_header(LABEL)
    if declines_reconfigure(LABEL, f"Reconfigure {LABEL}?", KEY_ENV):
        return
    print_info("In WhatsApp: Settings → Agents → create an agent, then open its chat → Chat info → API key.")
    suffix = " [keep current]" if get_env_value(KEY_ENV) else ""
    value = prompt(f"Agent API key{suffix}", password=True)
    if value:
        save_env_value(KEY_ENV, value.strip())
    print_info(
        "Done. Recommended in config.yaml (the platform cannot edit messages and allows "
        "12 sends/min): display.platforms.whatsapp_agent_platform: {tool_progress: 'off', "
        "interim_assistant_messages: false, long_running_notifications: false, "
        "busy_ack_detail: false}"
    )


# Same contract as Hermes's built-in WhatsApp hint: the model writes standard markdown and
# formatting.to_whatsapp converts it. (Asking for WhatsApp syntax directly would turn *bold*
# into italics when converted.)
HINT_BASE = (
    "You are chatting via Meta's WhatsApp Agent Platform with the person who created this "
    "agent. Standard markdown auto-converts to WhatsApp syntax (bold, italic, strike, monospace) "
    "— write markdown freely, bullets included. No tables — use bullets or labeled lines. Sent "
    "messages cannot be edited or deleted, and replies over 4000 characters are split into "
    "several messages (max 12 per minute), so keep answers concise."
)
HINT_MEDIA = (
    "You can send files natively: write MEDIA:/absolute/path/to/file in your response, on its own line. "
    "Images go as photos (JPEG/PNG; most other formats are converted, animated images go as files), MP4 videos "
    "play inline, audio arrives as a normal audio file (no voice-note bubble), documents up to 16 MB, other "
    "formats arrive as files. Captions max 1024 characters. Add [[as_document]] to send an image untouched as "
    "a file. Prefer PDF over Markdown for documents, and zip many files into one instead of sending lots. Image "
    "URLs via ![alt](url) are downloaded and sent as photos when possible. You can receive photos, voice notes "
    "(transcribed), videos and documents; audio files are not transcribed."
)
HINT_TEXT_ONLY = "This channel is text only: you cannot send or receive files, images or voice notes here."
PLATFORM_HINT = f"{HINT_BASE} {HINT_MEDIA}"  # media on (the default)
TEXT_ONLY_PLATFORM_HINT = f"{HINT_BASE} {HINT_TEXT_ONLY}"  # WHATSAPP_AGENT_PLATFORM_MEDIA_ENABLED=false


def _platform_hint() -> str:
    """The hint for the media switch as the environment sets it when the plugin registers (Hermes loads the
    profile's ``.env`` before plugins), parsed like the adapter's switch. Hermes keeps one hint per process, so
    ``media_enabled: false`` set only in ``config.yaml``, or multiplexed profiles with different switches, still
    get the full hint; ``platform_hints.whatsapp_agent_platform.replace`` covers those."""
    try:
        return PLATFORM_HINT if _media_enabled(None) else TEXT_ONLY_PLATFORM_HINT
    except Exception:  # an unreadable secret scope must not break registration
        return PLATFORM_HINT


def register(ctx: Any) -> None:
    ctx.register_platform(
        name=PLATFORM_NAME,
        label=LABEL,
        adapter_factory=WhatsAppAgentPlatformAdapter,
        check_fn=lambda: True,  # only needs httpx, a core Hermes dependency
        validate_config=_has_key,
        # .env-backed and profile-aware, so `hermes gateway setup` / `hermes status` see a key
        # stored in .env even when it is not exported into the process environment.
        is_connected=env_is_connected(KEY_ENV),
        required_env=[KEY_ENV],
        install_hint=f"Set {KEY_ENV} (WhatsApp → agent chat → Chat info → API key)",
        setup_fn=_interactive_setup,
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var=HOME_ENV,
        parse_target_ref_fn=_parse_target,
        standalone_sender_fn=_standalone_send,
        max_message_length=MAX_TEXT_LENGTH,
        pii_safe=True,
        emoji="💬",
        platform_hint=_platform_hint(),
    )
