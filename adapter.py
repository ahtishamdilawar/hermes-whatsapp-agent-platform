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
import re
import threading
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from gateway.config import Platform, PlatformConfig
from gateway.platforms._shared import env_is_connected, get_scoped_secret, seed_extra_from_env, send_error
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from hermes_constants import get_hermes_home

from .client import (
    API_BASE,
    CODE_BAD_FIELD,
    CODE_INVALID_PARAMETER,
    MAX_TEXT_LENGTH,
    AgentPlatformClient,
    AgentPlatformError,
    AuthError,
    NotCreatorError,
    NotSent,
    PollConflict,
    RateLimited,
    Retryable,
    Updates,
)
from .formatting import to_whatsapp
from .state import PollState, key_fingerprint, read_creator, state_path

logger = logging.getLogger(__name__)

PLATFORM_NAME = "whatsapp_agent_platform"
LABEL = "WhatsApp Agent Platform"
KEY_ENV = "WHATSAPP_AGENT_PLATFORM_API_KEY"
HOME_ENV = "WHATSAPP_AGENT_PLATFORM_HOME_CHANNEL"
ALLOWED_ENV = "WHATSAPP_AGENT_PLATFORM_ALLOWED_USERS"
ALLOW_ALL_ENV = "WHATSAPP_AGENT_PLATFORM_ALLOW_ALL_USERS"
BASE_URL_ENV = "WHATSAPP_AGENT_PLATFORM_BASE_URL"  # development only: point at a fake API
SELF_TARGET = "self"

_USER_ID_RE = re.compile(r"^user:\S+$")
_TRUTHY = {"1", "true", "yes", "on"}
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
            if await self._handle_inbound(message):
                handed += 1
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
        if not isinstance(wamid, str) or not isinstance(sender, str) or not _USER_ID_RE.match(sender):
            logger.warning("%s: malformed inbound message skipped", LABEL)
            return False
        if state.seen(wamid):
            return False
        try:
            sent_at = int(message.get("timestamp"))
        except (TypeError, ValueError):
            sent_at = int(time.time())
        if sent_at < state.start_timestamp - CLOCK_SKEW:
            return False  # retained backlog from before first activation
        if mtype == "reaction":
            logger.debug("%s: reaction received (not forwarded)", LABEL)
            state.remember(wamid)
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

        text = (message.get("text") or {}).get("body") if mtype == "text" else None
        if not isinstance(text, str) or not text.strip():
            logger.info("%s: unsupported inbound type %r; notified sender", LABEL, mtype)
            state.remember(wamid)
            self._spawn(self._send_notice(sender, UNSUPPORTED_NOTICE))
            return False

        event = self._build_event(message, sender, wamid, text, sent_at)
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
        state.remember(wamid)
        return True

    def _build_event(self, message: dict[str, Any], sender: str, wamid: str, text: str, sent_at: int) -> MessageEvent:
        name = self._names.get(sender)
        context = message.get("context") if isinstance(message.get("context"), dict) else {}
        quoted = context.get("id") if isinstance(context.get("id"), str) else None
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
            message_type=MessageType.TEXT,
            source=source,
            user_id=sender,
            user_name=name,
            message_id=wamid,
            timestamp=datetime.fromtimestamp(sent_at, tz=UTC),
            raw_message=message,
            reply_to_message_id=quoted,
            reply_to_text=reply_text,
            reply_to_is_own_message=quoted_own,
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
        client = self._client
        if client is None:
            return SendResult(success=False, error="send_path_degraded", raw_response={"final": True})
        ids = list(delivered)
        async with self._send_lock:  # the manual forbids concurrent sends to one recipient
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
    return seed_extra_from_env((), home_env=HOME_ENV, home_default=SELF_TARGET)


def _parse_target(ref: str) -> tuple[str, str | None] | None:
    ref = (ref or "").strip()
    if ref == SELF_TARGET or _USER_ID_RE.match(ref):
        return ref, None
    return None


async def _standalone_send(
    pconfig: Any,
    chat_id: str,
    message: str,
    *,
    thread_id: str | None = None,
    media_files: list[str] | None = None,
    force_document: bool = False,
) -> dict[str, Any]:
    """Out-of-process delivery (cron without a running gateway). The agent may message
    its creator first, so no inbound message is needed — only the creator's id, which the
    gateway records once Meta confirms it (or pass an explicit ``user:<id>``)."""
    key = _api_key()
    if not key:
        return send_error(f"{KEY_ENV} is not set")
    target = (chat_id or "").strip()
    if target in (SELF_TARGET, ""):
        to = read_creator(get_hermes_home(), key)
    elif _USER_ID_RE.match(target):
        to = target
    else:
        return send_error(f"invalid target {target!r}: use 'self' or user:<id>")
    if not to:
        return send_error(f"recipient unknown: send the agent one WhatsApp message first, or set {HOME_ENV}=user:<id>")
    text = to_whatsapp(message or "")
    if media_files:
        text += f"\n\n[{len(media_files)} attachment(s) generated; not sent from a scheduled job]"
    client = AgentPlatformClient(key, base_url=_base_url())
    sent: list[str] = []
    try:
        for chunk in _chunks(text):
            sent.append(await client.send_text(to, chunk))
        return {"success": True, "message_id": sent[-1] if sent else None}
    except AgentPlatformError as exc:
        partial = f" after {len(sent)} of the message's parts were delivered" if sent else ""
        return send_error(f"{exc.describe()}{partial}")
    finally:
        await client.aclose()


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
PLATFORM_HINT = (
    "You are chatting via Meta's WhatsApp Agent Platform with the person who created this "
    "agent. Standard markdown auto-converts to WhatsApp syntax (bold, italic, strike, monospace) "
    "— write markdown freely, bullets included. No tables — use bullets or labeled lines. Sent "
    "messages cannot be edited or deleted, and replies over 4000 characters are split into "
    "several messages (max 12 per minute), so keep answers concise. Only text can be sent here."
)


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
        platform_hint=PLATFORM_HINT,
    )
