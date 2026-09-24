"""Hermes platform adapter for Meta's WhatsApp Agent Platform.

WhatsApp → Meta ``GET /agent/v1/updates`` (long poll) → Hermes session/agent →
``POST /agent/v1/messages`` → WhatsApp. No webhook, business number or WhatsApp Web
session is involved; this is distinct from the ``whatsapp`` (Baileys) and
``whatsapp_cloud`` (Business Cloud API) transports.
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

from gateway.config import Platform, PlatformConfig
from gateway.platforms._shared import get_scoped_secret, seed_extra_from_env, send_error
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from hermes_constants import get_hermes_home

from .client import (
    API_BASE,
    MAX_TEXT_LENGTH,
    AgentPlatformClient,
    AgentPlatformError,
    Ambiguous,
    AuthError,
    MalformedResponse,
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
BASE_URL_ENV = "WHATSAPP_AGENT_PLATFORM_BASE_URL"  # testing against a fake server only
SELF_TARGET = "self"

_USER_ID_RE = re.compile(r"^user:\S+$")
_TRUTHY = {"1", "true", "yes", "on"}

POLL_TIMEOUT = 20  # seconds; Meta allows 0–25
POLL_MIN_INTERVAL = 60.0 / 15  # 15 polls/min per agent
TYPING_REFRESH = 20.0  # Meta's indicator lasts 25 s
INTERIM_SEND_RESERVE = 4  # keep this many /messages slots for the final answer
HANDOFF_ATTEMPTS = 3  # redeliveries of one message to Hermes before skipping it
UNSUPPORTED_NOTICE = "I can only read text messages on this channel for now — please send your request as text."


def _api_key() -> str:
    return str(get_scoped_secret(KEY_ENV, "") or "").strip()


def _base_url() -> str:
    return str(get_scoped_secret(BASE_URL_ENV, "") or "").strip() or API_BASE


def _csv_env(name: str) -> set[str]:
    raw = str(get_scoped_secret(name, "") or "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def _chunks(text: str) -> list[str]:
    """Split with Hermes's fence-aware splitter, then hard-cap at Meta's 4096 chars."""
    out: list[str] = []
    for chunk in BasePlatformAdapter.truncate_message(text, MAX_TEXT_LENGTH):
        out.extend(chunk[i : i + MAX_TEXT_LENGTH] for i in range(0, len(chunk), MAX_TEXT_LENGTH))
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
        self._names: dict[str, str] = {}
        self._handoff_failures: dict[str, int] = {}
        self._last_poll_at = 0.0
        self._poll_timeout = POLL_TIMEOUT
        self._poll_min_interval = POLL_MIN_INTERVAL
        self._backoff_base = 2.0

    @property
    def name(self) -> str:
        return LABEL

    @property
    def authorization_is_upstream(self) -> bool:
        """Meta delivers only the agent creator's messages and rejects sends to anyone
        else (403/131005). The adapter additionally pins the creator on first contact and
        drops other senders unless ``WHATSAPP_AGENT_PLATFORM_ALLOWED_USERS`` /
        ``..._ALLOW_ALL_USERS`` admit them, so this stays safe if Meta widens access."""
        return True

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
                    retryable=False,
                )
                return False
            self._active_keys.add(fingerprint)
        self._fingerprint = fingerprint
        if not self._acquire_platform_lock(PLATFORM_NAME, key, "WhatsApp Agent Platform API key"):
            self._release_guard()
            return False

        self._state = PollState.load(state_path(get_hermes_home(), key))
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
            await self._fail_connect("state_unwritable", f"cannot write polling state ({exc})", False)
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
            except PollConflict:
                await self._stop_polling(
                    "poll_conflict",
                    "another client started polling this agent's API key (HTTP 409); only one "
                    "poller per key is allowed — stop the other client, then restart the gateway",
                )
                return
            except OSError as exc:
                await self._stop_polling("state_unwritable", f"cannot persist polling offset ({exc})")
                return
            except NotSent as exc:
                if not isinstance(exc, Retryable):
                    await self._stop_polling("api_error", exc.describe())
                    return
                failures = await self._backoff(exc, failures)
            except Exception as exc:  # Ambiguous, Malformed, handoff retry, unexpected bugs
                failures = await self._backoff(exc, failures)

    async def _backoff(self, exc: Exception, failures: int) -> int:
        """Sleep before the next poll; the offset is unchanged, so nothing is lost."""
        failures += 1
        delay = min(60.0, self._backoff_base * 2 ** min(failures - 1, 5))
        if isinstance(exc, RateLimited):
            delay = max(delay, exc.retry_after or 0.0)
            if self._client is not None:
                self._client.limits["updates"].penalize(delay)
        if isinstance(exc, AgentPlatformError):
            logger.warning("%s: poll failed (%s); retrying in %.0fs", LABEL, exc.describe(), delay)
        else:
            logger.warning("%s: poll failed; retrying in %.0fs", LABEL, delay, exc_info=True)
        await asyncio.sleep(delay)
        return failures

    async def _stop_polling(self, code: str, message: str) -> None:
        logger.error("%s: polling stopped: %s", LABEL, message)
        self._set_fatal_error(code, f"{LABEL}: {message}", retryable=False)
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
        if handed:
            logger.info("%s: handed %d message(s) to Hermes", LABEL, handed)

    def _sender_allowed(self, sender: str) -> bool:
        state = self._state
        assert state is not None
        if state.creator is None:
            # Trust on first use: Meta only delivers the creator's messages. Also the
            # recipient for the ``self`` target (cron / send_message).
            state.creator = sender
            logger.info("%s: recorded the agent creator", LABEL)
        if str(get_scoped_secret(ALLOW_ALL_ENV, "") or "").strip().lower() in _TRUTHY:
            return True
        allowed = _csv_env(ALLOWED_ENV)
        if allowed:
            return sender in allowed
        return sender == state.creator

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
        if sent_at < state.start_timestamp:
            return False  # retained backlog from before first activation
        if not self._sender_allowed(sender):
            logger.warning("%s: message from a non-allowed sender dropped", LABEL)
            state.remember(wamid)
            return False

        self._latest_inbound[sender] = wamid
        self._latest_inbound.move_to_end(sender)
        while len(self._latest_inbound) > 256:
            self._latest_inbound.popitem(last=False)

        if mtype == "reaction":
            logger.debug("%s: reaction received (not forwarded)", LABEL)
            state.remember(wamid)
            return False
        text = (message.get("text") or {}).get("body") if mtype == "text" else None
        if not isinstance(text, str) or not text.strip():
            logger.info("%s: unsupported inbound type %r; notified sender", LABEL, mtype)
            state.remember(wamid)
            self._spawn(self._send_notice(sender, UNSUPPORTED_NOTICE))
            return False

        event = self._build_event(message, sender, wamid, text, sent_at)
        try:
            await self.handle_message(event)
        except Exception:
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
                raise
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
        to = self._resolve_recipient(chat_id)
        if to is None:
            return SendResult(
                success=False,
                error="no WhatsApp recipient known yet (message the agent once first)",
                error_kind="not_found",
                raw_response={"final": True},
            )
        client = self._client
        if client is None or client.closed:
            return SendResult(success=False, error="not connected", retryable=True, error_kind="transient")
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
            return SendResult(success=False, error="not connected", retryable=True, error_kind="transient")
        ids = list(delivered)
        async with self._send_lock:  # the manual forbids concurrent sends to one recipient
            for index, chunk in enumerate(chunks):
                try:
                    wamid = await client.send_text(
                        to, chunk, reply_to=reply_to if index == 0 and not delivered else None
                    )
                except AgentPlatformError as exc:
                    return self._send_failure(exc, to, ids, chunks[index:])
                ids.append(wamid)
                with contextlib.suppress(Exception):
                    from gateway import rich_sent_store

                    rich_sent_store.record(to, wamid, chunk)
        logger.info("%s: response sent (%d message(s))", LABEL, len(ids) - len(delivered))
        return SendResult(success=True, message_id=ids[-1] if ids else None, continuation_message_ids=tuple(ids[:-1]))

    @staticmethod
    def _send_failure(exc: AgentPlatformError, to: str, delivered: list[str], remaining: list[str]) -> SendResult:
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
            return SendResult(
                success=False,
                error=f"rate limited: {detail}",
                raw_response=raw,
                retry_after=exc.retry_after or 10.0,
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
        if isinstance(exc, (Ambiguous, MalformedResponse)):
            # Possibly delivered: never retried, never re-sent as a plain-text fallback.
            return SendResult(
                success=False,
                error=f"send timed out, outcome unknown: {detail}",
                raw_response=raw,
                error_kind="unknown",
            )
        kind = "forbidden" if isinstance(exc, (AuthError, NotCreatorError)) else "unknown"
        return SendResult(success=False, error=detail, raw_response=raw, error_kind=kind)

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

    async def send_typing(self, chat_id: str, metadata: Any = None) -> None:
        """Read receipt + typing indicator for the latest inbound message.

        Called every ~2 s by the gateway and cancelled after 1.5 s, so the POST runs as a
        background task and is refreshed only every 20 s (Meta's indicator lasts 25 s and
        /statuses is limited to 12/min). With ``typing_indicator: false`` only a single
        read receipt is sent.
        """
        to = self._resolve_recipient(chat_id)
        wamid = self._latest_inbound.get(to) if to else None
        if not wamid or self._client is None:
            return
        typing = bool(self.config.typing_indicator)
        now = time.monotonic()
        last = self._status_at.get(wamid)
        if last is not None and (not typing or now - last < TYPING_REFRESH):
            return
        self._status_at[wamid] = now
        while len(self._status_at) > 256:
            self._status_at.popitem(last=False)
        self._spawn(self._post_status(wamid, typing))

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
    gateway records on first contact (or pass an explicit ``user:<id>``)."""
    key = _api_key()
    if not key:
        return send_error(f"{KEY_ENV} is not set")
    to = chat_id if _USER_ID_RE.match(chat_id or "") else read_creator(get_hermes_home(), key)
    if not to:
        return send_error(f"recipient unknown: send the agent one WhatsApp message first, or set {HOME_ENV}=user:<id>")
    text = to_whatsapp(message or "")
    if media_files:
        text += f"\n\n[{len(media_files)} attachment(s) generated; not sent from a scheduled job]"
    client = AgentPlatformClient(key, base_url=_base_url())
    try:
        last = None
        for chunk in _chunks(text):
            last = await client.send_text(to, chunk)
        return {"success": True, "message_id": last}
    except AgentPlatformError as exc:
        return send_error(exc.describe())
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
    "messages cannot be edited or deleted, and replies over 4096 characters are split into "
    "several messages (max 12 per minute), so keep answers concise. Only text can be sent here."
)


def register(ctx: Any) -> None:
    ctx.register_platform(
        name=PLATFORM_NAME,
        label=LABEL,
        adapter_factory=WhatsAppAgentPlatformAdapter,
        check_fn=lambda: True,  # only needs httpx, a core Hermes dependency
        validate_config=_has_key,
        is_connected=_has_key,
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
