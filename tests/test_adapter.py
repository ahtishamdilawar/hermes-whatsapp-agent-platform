"""Adapter behaviour against a fake Meta API: lifecycle, polling, sending, typing, registration."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from wap_helpers import API_KEY, CREATOR, error_response, text_message, updates_response
from wap_plugin_under_test import adapter as mod
from wap_plugin_under_test.client import RateWindow
from wap_plugin_under_test.state import PollState, state_path

OTHER = "user:777"


def make_adapter():
    from gateway.config import PlatformConfig

    a = mod.WhatsAppAgentPlatformAdapter(PlatformConfig(enabled=True))
    a._poll_min_interval = 0.01
    a._poll_timeout = 0
    a._backoff_base = 0.01
    a.handle_message = AsyncMock()
    a._notify_fatal_error = AsyncMock()
    return a


async def until(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


@pytest_asyncio.fixture
async def connected(meta, api_key):
    a = make_adapter()
    assert await a.connect() is True
    yield a
    await a.disconnect()


def dispatched(a) -> list:
    return [call.args[0] for call in a.handle_message.await_args_list]


def saved_offset() -> int:
    from hermes_constants import get_hermes_home

    return PollState.load(state_path(get_hermes_home(), API_KEY)).next_offset


# ---------------------------------------------------------------- lifecycle
@pytest.mark.asyncio
async def test_connect_probe_is_not_dispatched_then_loop_dispatches_once(meta, api_key, hermes_home):
    msg = text_message("wamid.in1", "hello")
    meta.queue("updates", updates_response(msg, next_offset=5), updates_response(msg, next_offset=5))
    a = make_adapter()
    assert await a.connect() is True
    probe = meta.calls("updates")[0]
    assert probe.url.params["timeout"] == "0" and probe.url.params["offset"] == "0"
    await until(lambda: a.handle_message.await_count == 1)
    await until(lambda: len(meta.calls("updates")) >= 4)
    await a.disconnect()
    assert a.handle_message.await_count == 1
    event = dispatched(a)[0]
    assert (event.text, event.source.chat_id, event.source.chat_type, event.user_name) == (
        "hello",
        CREATOR,
        "dm",
        "Alex",
    )
    saved = PollState.load(state_path(hermes_home, API_KEY))
    assert saved.next_offset == 5 and saved.seen("wamid.in1") and saved.creator == CREATOR


@pytest.mark.asyncio
async def test_missing_key_is_fatal_config_error(meta):
    a = make_adapter()
    assert await a.connect() is False
    assert a.fatal_error_code == "config_missing" and not a.fatal_error_retryable


@pytest.mark.parametrize("response", [error_response(401, 190), error_response(400, 100)])
@pytest.mark.asyncio
async def test_invalid_key_is_fatal_and_releases_the_key(meta, api_key, response):
    meta.queue("updates", response)
    a = make_adapter()
    assert await a.connect() is False
    assert a.fatal_error_code == "invalid_auth" and not a.fatal_error_retryable
    assert API_KEY not in (a.fatal_error_message or "")
    assert mod.WhatsAppAgentPlatformAdapter._active_keys == set()


@pytest.mark.asyncio
async def test_transient_connect_failure_is_retryable(meta, api_key):
    meta.queue("updates", httpx.ConnectError("dns"))
    a = make_adapter()
    assert await a.connect() is False
    assert a.fatal_error_code == "api_unavailable" and a.fatal_error_retryable


@pytest.mark.asyncio
async def test_second_adapter_for_same_key_in_process_is_refused(connected):
    other = make_adapter()
    assert await other.connect() is False
    assert other.fatal_error_code == "whatsapp_agent_platform_lock"


@pytest.mark.asyncio
async def test_single_poll_conflict_backs_off_and_resumes(meta, api_key):
    a = make_adapter()
    a._conflict_backoff = 0.01
    assert await a.connect()
    meta.queue(
        "updates", error_response(409, 1752041), updates_response(text_message("wamid.in1", "hi"), next_offset=2)
    )
    await until(lambda: a.handle_message.await_count == 1)
    assert not a.has_fatal_error
    await a.disconnect()


@pytest.mark.asyncio
async def test_repeated_poll_conflicts_stop_polling(meta, api_key):
    a = make_adapter()
    a._conflict_backoff = 0.01
    assert await a.connect()
    meta.queue("updates", *[error_response(409, 1752041) for _ in range(3)])
    await until(lambda: a.has_fatal_error)
    assert a.fatal_error_code == "poll_conflict" and not a.fatal_error_retryable
    a._notify_fatal_error.assert_awaited()
    await a.disconnect()


@pytest.mark.asyncio
async def test_unknown_4xx_on_updates_backs_off_instead_of_stopping(meta, api_key):
    a = make_adapter()
    assert await a.connect()
    meta.queue("updates", httpx.Response(407), updates_response(text_message("wamid.in1", "hi"), next_offset=2))
    await until(lambda: a.handle_message.await_count == 1)
    assert not a.has_fatal_error
    await a.disconnect()


@pytest.mark.asyncio
async def test_transient_poll_errors_back_off_and_recover(meta, api_key):
    a = make_adapter()
    assert await a.connect()
    meta.queue(
        "updates",
        error_response(500, 2),
        error_response(429, 130429, {"Retry-After": "0"}),
        httpx.ReadTimeout("slow"),
        updates_response(text_message("wamid.in1", "hi"), next_offset=2),
    )
    await until(lambda: a.handle_message.await_count == 1)
    assert not a.has_fatal_error
    await a.disconnect()


# ---------------------------------------------------------------- inbound rules
@pytest.mark.asyncio
async def test_backlog_before_first_activation_is_skipped(meta, api_key):
    old = text_message("wamid.old", "old", timestamp=int(time.time()) - 3600)
    new = text_message("wamid.new", "new")
    a = make_adapter()
    assert await a.connect()
    meta.queue("updates", updates_response(old, new, next_offset=9))
    await until(lambda: a.handle_message.await_count == 1)
    await a.disconnect()
    assert [e.text for e in dispatched(a)] == ["new"]


@pytest.mark.asyncio
async def test_restart_does_not_redeliver_handled_messages(meta, api_key):
    msg = text_message("wamid.in1", "once")
    first = make_adapter()
    assert await first.connect()
    meta.queue("updates", updates_response(msg, next_offset=3))
    await until(lambda: first.handle_message.await_count == 1)
    await first.disconnect()
    # Meta retains updates: an older cursor could return the same message again.
    path = state_path(__import__("hermes_constants").get_hermes_home(), API_KEY)
    state = PollState.load(path)
    state.next_offset = 0
    state.save()
    second = make_adapter()
    assert await second.connect()
    meta.queue("updates", updates_response(msg, next_offset=3))
    await until(lambda: saved_offset() == 3)  # the page was fully processed
    await second.disconnect()
    assert second.handle_message.await_count == 0


@pytest.mark.asyncio
async def test_creator_is_confirmed_by_meta_and_others_are_dropped(meta, api_key, hermes_home):
    meta.not_creator_wamids.add("wamid.b")
    a = make_adapter()
    assert await a.connect()
    # The stranger's message comes first: it must not be pinned as the creator.
    meta.queue(
        "updates",
        updates_response(
            text_message("wamid.b", "stranger", sender=OTHER), text_message("wamid.a", "me"), next_offset=4
        ),
    )
    await until(lambda: saved_offset() == 4)
    await a.disconnect()
    assert [e.text for e in dispatched(a)] == ["me"]
    assert PollState.load(state_path(hermes_home, API_KEY)).creator == CREATOR
    checked = [b["message_id"] for b in meta.bodies("statuses")]
    assert checked[:2] == ["wamid.b", "wamid.a"]  # both senders were checked with Meta


@pytest.mark.asyncio
async def test_creator_id_change_is_reconfirmed(meta, api_key, hermes_home):
    new_id = "user:50972923569999"
    a = make_adapter()
    assert await a.connect()
    meta.queue("updates", updates_response(text_message("wamid.a", "old id"), next_offset=2))
    await until(lambda: a.handle_message.await_count == 1)
    meta.queue("updates", updates_response(text_message("wamid.c", "new id", sender=new_id), next_offset=3))
    await until(lambda: a.handle_message.await_count == 2)
    await a.disconnect()
    assert PollState.load(state_path(hermes_home, API_KEY)).creator == new_id


@pytest.mark.asyncio
async def test_allowlist_adds_senders_without_replacing_the_creator(meta, api_key, monkeypatch, hermes_home):
    monkeypatch.setenv("WHATSAPP_AGENT_PLATFORM_ALLOWED_USERS", OTHER)
    meta.not_creator_wamids.add("wamid.b")
    a = make_adapter()
    assert await a.connect()
    meta.queue(
        "updates",
        updates_response(text_message("wamid.a", "me"), text_message("wamid.b", "friend", sender=OTHER), next_offset=4),
    )
    await until(lambda: a.handle_message.await_count == 2)
    await a.disconnect()
    assert PollState.load(state_path(hermes_home, API_KEY)).creator == CREATOR


@pytest.mark.asyncio
async def test_unconfirmable_sender_is_rechecked_without_advancing(meta, api_key):
    a = make_adapter()
    assert await a.connect()
    meta.queue("statuses", error_response(503, 131016))  # Meta cannot answer the creator check yet
    msg = text_message("wamid.a", "me")
    meta.queue("updates", updates_response(msg, next_offset=2), updates_response(msg, next_offset=2))
    await until(lambda: a.handle_message.await_count == 1)
    await a.disconnect()
    assert len(meta.calls("statuses")) == 2 and saved_offset() == 2


@pytest.mark.asyncio
async def test_unsupported_type_gets_a_text_notice(meta, api_key):
    image = {
        "from": CREATOR,
        "id": "wamid.img",
        "timestamp": str(int(time.time()) + 5),
        "type": "image",
        "image": {"id": "123", "mime_type": "image/jpeg", "sha256": "abc="},
    }
    a = make_adapter()
    assert await a.connect()
    meta.queue("updates", updates_response(image, next_offset=2))
    await until(lambda: len(meta.calls("messages")) == 1)
    await a.disconnect()
    assert a.handle_message.await_count == 0
    body = meta.bodies("messages")[0]
    assert body["to"] == CREATOR and "text" in body["text"]["body"]


@pytest.mark.asyncio
async def test_handoff_failure_retries_without_advancing_then_skips(meta, api_key, hermes_home):
    a = make_adapter()
    a._backoff_base = 0.3
    a.handle_message = AsyncMock(side_effect=RuntimeError("hermes busy"))
    assert await a.connect()
    msg = text_message("wamid.poison", "x")
    meta.queue("updates", *[updates_response(msg, next_offset=6) for _ in range(3)])
    await until(lambda: a.handle_message.await_count == 1)
    assert saved_offset() == 0  # not advanced past a message Hermes did not accept
    await until(lambda: a.handle_message.await_count == 3)
    await until(lambda: PollState.load(state_path(hermes_home, API_KEY)).next_offset == 6)
    await a.disconnect()
    assert not a.has_fatal_error


@pytest.mark.asyncio
async def test_real_base_handler_receives_event(meta, api_key):
    from gateway.config import PlatformConfig

    a = mod.WhatsAppAgentPlatformAdapter(PlatformConfig(enabled=True))
    a._poll_min_interval, a._poll_timeout = 0.01, 0
    received = []

    async def handler(event):
        received.append(event)
        return None

    a.set_message_handler(handler)
    assert await a.connect()
    meta.queue("updates", updates_response(text_message("wamid.in1", "via base"), next_offset=2))
    await until(lambda: received)
    await a.disconnect()
    assert received[0].text == "via base" and received[0].source.platform.value == "whatsapp_agent_platform"


# ---------------------------------------------------------------- outbound
@pytest.mark.asyncio
async def test_send_formats_and_splits_in_order_with_reply_on_first(connected, meta):
    text = "**bold** " + "word " * 1800
    result = await connected.send(CREATOR, text, reply_to="wamid.in1")
    bodies = meta.bodies("messages")
    assert result.success and len(bodies) == 3
    assert bodies[0]["text"]["body"].startswith("*bold* ")
    assert bodies[0]["context"] == {"message_id": "wamid.in1"}
    assert all("context" not in b for b in bodies[1:])
    assert all(len(b["text"]["body"]) <= 4096 for b in bodies)
    assert result.message_id == "wamid.out3" and result.continuation_message_ids == ("wamid.out1", "wamid.out2")


@pytest.mark.asyncio
async def test_ambiguous_500_is_sent_exactly_once_without_fallback(connected, meta):
    meta.queue("messages", error_response(500, 2))
    result = await connected._send_with_retry(CREATOR, "hello")
    assert not result.success and len(meta.calls("messages")) == 1


@pytest.mark.asyncio
async def test_read_timeout_is_sent_exactly_once(connected, meta):
    meta.queue("messages", httpx.ReadTimeout("slow"))
    result = await connected._send_with_retry(CREATOR, "hello")
    assert not result.success and len(meta.calls("messages")) == 1


@pytest.mark.asyncio
async def test_not_creator_403_is_final(connected, meta):
    meta.queue("messages", error_response(403, 131005))
    result = await connected._send_with_retry(CREATOR, "hello")
    assert not result.success and result.error_kind == "forbidden" and len(meta.calls("messages")) == 1


@pytest.mark.asyncio
async def test_rate_limited_send_is_retried_after_retry_after(connected, meta):
    meta.queue("messages", error_response(429, 130429, {"Retry-After": "0"}))
    result = await connected._send_with_retry(CREATOR, "hello")
    assert result.success and len(meta.calls("messages")) == 2


@pytest.mark.asyncio
async def test_partial_split_resumes_only_the_remainder(connected, meta):
    ok = httpx.Response(200, json={"messages": [{"id": "wamid.c1"}]})
    meta.queue("messages", ok, error_response(503, 131016))
    text = "\n".join(["x" * 3000, "y" * 3000, "z" * 100])
    result = await connected._send_with_retry(CREATOR, text)
    bodies = [b["text"]["body"] for b in meta.bodies("messages")]
    assert result.success
    # x delivered, the y+z chunk refused (503/131016), then only that chunk re-sent.
    assert [b[:1] for b in bodies] == ["x", "y", "y"] and "z" * 100 in bodies[-1]


@pytest.mark.asyncio
async def test_partial_split_with_ambiguous_failure_does_not_resend_head(connected, meta):
    ok = httpx.Response(200, json={"messages": [{"id": "wamid.c1"}]})
    meta.queue("messages", ok, error_response(500, 2))
    text = "\n".join(["x" * 3000, "y" * 3000])
    result = await connected._send_with_retry(CREATOR, text)
    assert not result.success and len(meta.calls("messages")) == 2


@pytest.mark.asyncio
async def test_interim_messages_are_dropped_when_send_budget_is_low(connected, meta):
    connected._client.limits["messages"] = RateWindow(12)
    for _ in range(8):
        connected._client.limits["messages"].try_acquire()
    result = await connected.send(CREATOR, "thinking…", metadata={"_interim_send": True})
    assert result.success and meta.calls("messages") == []
    assert (await connected.send(CREATOR, "final answer")).success
    assert len(meta.calls("messages")) == 1


@pytest.mark.asyncio
async def test_self_target_resolves_to_creator_and_empty_is_noop(meta, api_key):
    a = make_adapter()
    assert await a.connect()
    missing = await a.send("self", "hi")
    assert not missing.success and "recipient unknown" in missing.error
    a._state.creator = CREATOR
    assert (await a.send("self", "hi")).success
    assert (await a.send(CREATOR, "   ")).success
    await a.disconnect()
    assert [b["to"] for b in meta.bodies("messages")] == [CREATOR]


@pytest.mark.asyncio
async def test_quoted_reply_to_own_message_carries_its_text(meta, api_key):
    a = make_adapter()
    assert await a.connect()
    sent = await a.send(CREATOR, "the answer is 42")
    quote = text_message("wamid.q", "why?", context={"from": "agent:1", "id": sent.message_id})
    meta.queue("updates", updates_response(quote, next_offset=2))
    await until(lambda: a.handle_message.await_count == 1)
    await a.disconnect()
    event = dispatched(a)[0]
    assert event.reply_to_is_own_message and event.reply_to_text == "the answer is 42"


# ---------------------------------------------------------------- typing / read
@pytest.mark.asyncio
async def test_read_and_typing_on_arrival_then_refresh_is_throttled(meta, api_key):
    a = make_adapter()
    assert await a.connect()
    meta.queue("updates", updates_response(text_message("wamid.in1", "hi"), next_offset=2))
    await until(lambda: a.handle_message.await_count == 1)
    await until(lambda: len(meta.calls("statuses")) == 1)  # sent on arrival, no send_typing needed
    await a.send_typing(CREATOR)  # within 20 s of the last status: no new request
    await a.send_typing(CREATOR)
    await asyncio.sleep(0.05)
    await a.disconnect()
    assert meta.bodies("statuses") == [
        {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": "wamid.in1",
            "typing_indicator": {"type": "text"},
        }
    ]


@pytest.mark.asyncio
async def test_typing_disabled_still_sends_read_receipts(meta, api_key):
    # Hermes never calls send_typing when typing_indicator is off: receipts must not depend on it.
    a = make_adapter()
    a.config.typing_indicator = False
    assert await a.connect()
    meta.queue("updates", updates_response(text_message("wamid.in1", "hi"), next_offset=2))
    await until(lambda: a.handle_message.await_count == 1)
    meta.queue("updates", updates_response(text_message("wamid.in2", "again"), next_offset=3))
    await until(lambda: len(meta.calls("statuses")) == 2)
    await a.disconnect()
    bodies = meta.bodies("statuses")
    assert [b["message_id"] for b in bodies] == ["wamid.in1", "wamid.in2"]
    assert all("typing_indicator" not in b for b in bodies)


@pytest.mark.asyncio
async def test_known_creator_gets_read_receipt_with_typing(meta, api_key):
    a = make_adapter()
    assert await a.connect()
    meta.queue("updates", updates_response(text_message("wamid.in1", "hi"), next_offset=2))
    await until(lambda: a.handle_message.await_count == 1)
    meta.queue("updates", updates_response(text_message("wamid.in2", "again"), next_offset=3))
    await until(lambda: len(meta.calls("statuses")) == 2)
    await a.disconnect()
    assert meta.bodies("statuses")[1]["typing_indicator"] == {"type": "text"}


# ---------------------------------------------------------------- registration / cron
def test_register_declares_platform_capabilities():
    captured = {}

    class Ctx:
        def register_platform(self, **kwargs):
            captured.update(kwargs)

    mod.register(Ctx())
    assert captured["name"] == "whatsapp_agent_platform"
    assert captured["required_env"] == ["WHATSAPP_AGENT_PLATFORM_API_KEY"]
    assert captured["cron_deliver_env_var"] == "WHATSAPP_AGENT_PLATFORM_HOME_CHANNEL"
    assert captured["max_message_length"] == 4096 and captured["pii_safe"] is True
    import hermes_cli.gateway as gateway_mod
    from gateway.config import PlatformConfig

    real_get = gateway_mod.get_env_value
    try:  # status reads through the .env-backed helper, not only os.environ
        gateway_mod.get_env_value = lambda name: "k" if name == "WHATSAPP_AGENT_PLATFORM_API_KEY" else ""
        assert captured["is_connected"](PlatformConfig(enabled=True)) is True
        gateway_mod.get_env_value = lambda name: ""
        assert captured["is_connected"](PlatformConfig(enabled=True)) is False
    finally:
        gateway_mod.get_env_value = real_get
    for hook in ("standalone_sender_fn", "parse_target_ref_fn", "env_enablement_fn", "setup_fn"):
        assert callable(captured[hook])
    # The hint must ask for markdown: WhatsApp-style *bold* from the model would be converted to italics.
    assert "markdown" in captured["platform_hint"] and "*bold*" not in captured["platform_hint"]


def test_register_kwargs_are_valid_platform_entry_fields():
    from gateway.platform_registry import PlatformEntry

    captured = {}

    class Ctx:
        def register_platform(
            self,
            name,
            label,
            adapter_factory,
            check_fn,
            validate_config=None,
            required_env=None,
            install_hint="",
            **entry_kwargs,
        ):
            PlatformEntry(
                name=name,
                label=label,
                adapter_factory=adapter_factory,
                check_fn=check_fn,
                validate_config=validate_config,
                required_env=required_env or [],
                install_hint=install_hint,
                source="plugin",
                **entry_kwargs,
            )
            captured["ok"] = True

    mod.register(Ctx())
    assert captured["ok"]


def test_target_parsing_and_env_enablement(api_key, monkeypatch):
    assert mod._parse_target("self") == ("self", None)
    assert mod._parse_target(CREATOR) == (CREATOR, None)
    assert mod._parse_target("+15551234567") is None
    assert mod._env_enablement()["home_channel"]["chat_id"] == "self"
    monkeypatch.setenv("WHATSAPP_AGENT_PLATFORM_HOME_CHANNEL", CREATOR)
    assert mod._env_enablement()["home_channel"]["chat_id"] == CREATOR
    monkeypatch.delenv("WHATSAPP_AGENT_PLATFORM_API_KEY")
    assert mod._env_enablement() is None and not mod._has_key()


@pytest.mark.asyncio
async def test_standalone_send_targets_recorded_creator(meta, api_key, hermes_home):
    no_creator = await mod._standalone_send(None, "self", "report")
    assert "error" in no_creator
    state = PollState.load(state_path(hermes_home, API_KEY))
    state.creator = CREATOR
    state.save()
    result = await mod._standalone_send(None, "self", "**daily** report")
    assert result["success"] is True
    assert meta.bodies("messages")[0]["to"] == CREATOR
    assert meta.bodies("messages")[0]["text"]["body"] == "*daily* report"
