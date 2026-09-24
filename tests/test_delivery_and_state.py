"""Review fixes: how failures reach Hermes's retry loop and delivery ledger, state durability, guards."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from wap_helpers import API_KEY, CREATOR, error_response, text_message, updates_response
from wap_plugin_under_test import adapter as mod
from wap_plugin_under_test import state as state_mod
from wap_plugin_under_test.client import Ambiguous, NotCreatorError, RateLimited, Retryable
from wap_plugin_under_test.state import PollState, state_path


def make_adapter():
    from gateway.config import PlatformConfig

    a = mod.WhatsAppAgentPlatformAdapter(PlatformConfig(enabled=True))
    a._poll_min_interval, a._poll_timeout, a._backoff_base = 0.01, 0, 0.01
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
    assert await a.connect()
    yield a
    await a.disconnect()


# ------------------------------------------------ what Hermes's delivery ledger sees
def test_rate_limit_is_reported_as_flood_control_with_metas_wait():
    from gateway.delivery_ledger import flood_wait_seconds, is_flood_error

    result = mod.WhatsAppAgentPlatformAdapter._send_failure(
        RateLimited("send: rate limited", retry_after=7), CREATOR, [], ["x"]
    )
    assert is_flood_error(result.error) and flood_wait_seconds(result.error) == 7
    assert result.retry_after == 7 and result.error_kind == "rate_limited"


def test_not_creator_is_a_dead_target_and_never_retried():
    from gateway.dead_targets import classify_dead_error

    result = mod.WhatsAppAgentPlatformAdapter._send_failure(
        NotCreatorError("send: forbidden, not the agent's creator", status=403, code=131005), CREATOR, [], ["x"]
    )
    assert classify_dead_error(result.error) == "forbidden" and result.raw_response["final"]


def test_ambiguous_failure_is_final_inline_but_left_to_the_ledger():
    from gateway.dead_targets import classify_dead_error
    from gateway.delivery_ledger import is_flood_error

    result = mod.WhatsAppAgentPlatformAdapter._send_failure(
        Ambiguous("send: server error, outcome unknown", status=500), CREATOR, ["wamid.1"], ["y"]
    )
    assert result.raw_response["final"] and result.raw_response["partial_overflow"]
    # Not dead and not a flood: the ledger's timed redelivery (with its duplicate marker) applies.
    assert classify_dead_error(result.error) is None and not is_flood_error(result.error)


def test_transient_failure_is_retryable_and_resumable():
    result = mod.WhatsAppAgentPlatformAdapter._send_failure(
        Retryable("send: not accepted, retry later", status=503), CREATOR, [], ["x"]
    )
    assert result.retryable and result.raw_response["resumable"] and not result.raw_response.get("final")


@pytest.mark.asyncio
async def test_send_while_disconnected_is_left_to_the_reconnect_sweep(meta, api_key):
    from gateway.delivery_ledger import is_reconnect_only

    a = make_adapter()  # never connected
    result = await a._send_with_retry(CREATOR, "hello")
    assert not result.success and is_reconnect_only(result.error)
    assert meta.calls("messages") == []


# ----------------------------------------------------------------- outbound details
@pytest.mark.asyncio
async def test_stale_quote_is_resent_without_context(connected, meta):
    meta.queue("messages", error_response(400, 131009))
    result = await connected.send(CREATOR, "answer", reply_to="wamid.gone")
    bodies = meta.bodies("messages")
    assert result.success and len(bodies) == 2
    assert bodies[0]["context"] == {"message_id": "wamid.gone"} and "context" not in bodies[1]


def test_chunks_count_emoji_as_two_utf16_units():
    chunks = mod._chunks("😀" * 3000)  # 6000 UTF-16 units
    assert len(chunks) >= 2
    assert all(mod._utf16_len(c) <= 4096 for c in chunks)
    assert "".join(c.split(" (")[0] for c in chunks).count("😀") == 3000


def test_base_url_override_must_be_https_or_localhost(monkeypatch):
    monkeypatch.setenv("WHATSAPP_AGENT_PLATFORM_BASE_URL", "http://example.com/agent/v1")
    assert mod._base_url() == mod.API_BASE
    monkeypatch.setenv("WHATSAPP_AGENT_PLATFORM_BASE_URL", "http://127.0.0.1:8099/agent/v1")
    assert mod._base_url() == "http://127.0.0.1:8099/agent/v1"
    monkeypatch.setenv("WHATSAPP_AGENT_PLATFORM_BASE_URL", "https://proxy.internal/agent/v1")
    assert mod._base_url() == "https://proxy.internal/agent/v1"


@pytest.mark.asyncio
async def test_standalone_send_rejects_unknown_target_forms(meta, api_key):
    result = await mod._standalone_send(None, "+15551234567", "report")
    assert "error" in result and meta.calls("messages") == []


# --------------------------------------------------------------- state durability
def test_permission_error_is_retried(monkeypatch):
    monkeypatch.setattr(state_mod.time, "sleep", lambda s: None)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError("sharing violation")
        return "ok"

    assert state_mod._retry_on_permission_error(flaky) == "ok" and len(calls) == 3


def test_unreadable_state_is_not_treated_as_corruption(hermes_home, monkeypatch):
    path = state_path(hermes_home, API_KEY)
    state = PollState.load(path)
    state.creator = CREATOR
    state.save()
    monkeypatch.setattr(state_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(type(path), "read_text", lambda self, **kw: (_ for _ in ()).throw(PermissionError("locked")))
    with pytest.raises(PermissionError):
        PollState.load(path)
    monkeypatch.undo()
    assert path.exists() and PollState.load(path).creator == CREATOR  # nothing quarantined


@pytest.mark.asyncio
async def test_transient_save_failures_back_off_then_recover(meta, api_key, monkeypatch):
    a = make_adapter()
    assert await a.connect()
    real_save = PollState.save
    failures = {"left": 2}

    def flaky_save(self):
        if failures["left"]:
            failures["left"] -= 1
            raise PermissionError("locked")
        real_save(self)

    monkeypatch.setattr(PollState, "save", flaky_save)
    msg = text_message("wamid.in1", "hi")
    meta.queue("updates", *[updates_response(msg, next_offset=2) for _ in range(3)])
    await until(lambda: failures["left"] == 0 and a._save_failures == 0 and a._state.next_offset == 2)
    await a.disconnect()
    assert not a.has_fatal_error and a.handle_message.await_count == 1


@pytest.mark.asyncio
async def test_persistent_save_failure_is_fatal_but_retryable(meta, api_key, monkeypatch):
    a = make_adapter()
    assert await a.connect()
    monkeypatch.setattr(PollState, "save", lambda self: (_ for _ in ()).throw(PermissionError("locked")))
    meta.queue("updates", *[updates_response(next_offset=2) for _ in range(mod.SAVE_FAILURE_LIMIT)])
    await until(lambda: a.has_fatal_error)
    assert a.fatal_error_code == "state_unwritable" and a.fatal_error_retryable
    await a.disconnect()


@pytest.mark.asyncio
async def test_statuses_only_page_advances_the_offset(connected, meta, hermes_home):
    status = {"id": "wamid.out1", "status": "read", "recipient_id": CREATOR, "timestamp": str(int(time.time()))}
    meta.queue("updates", updates_response(next_offset=9, statuses=[status]))
    await until(lambda: PollState.load(state_path(hermes_home, API_KEY)).next_offset == 9)
    assert connected.handle_message.await_count == 0


@pytest.mark.asyncio
async def test_closed_client_is_reported_as_degraded_not_a_crash(connected, meta):
    await connected._client._http.aclose()
    with pytest.raises(Retryable):  # a call already past the closed check maps to not-sent
        await connected._client.send_text(CREATOR, "hello")
    result = await connected.send(CREATOR, "hello")
    assert not result.success and result.error == "send_path_degraded"


@pytest.mark.asyncio
async def test_poll_uses_the_documented_body_for_rate_limit_waits(meta, api_key):
    a = make_adapter()
    assert await a.connect()
    meta.queue("updates", error_response(429, 130429, {"Retry-After": "0"}), httpx.Response(204))
    await until(lambda: len(meta.calls("updates")) >= 3)
    assert not a.has_fatal_error
    await a.disconnect()
