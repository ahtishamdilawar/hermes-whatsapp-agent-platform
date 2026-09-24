"""Protocol tests for client.py against the manual's documented shapes and error codes."""

from __future__ import annotations

import httpx
import pytest
from wap_helpers import API_KEY, CREATOR, FakeMeta, error_response, fixture_json
from wap_plugin_under_test import client as c


@pytest.mark.asyncio
async def test_updates_fixture_parses_and_sends_bearer_and_params():
    meta = FakeMeta()
    meta.queue("updates", httpx.Response(200, json=fixture_json("updates_text.json")))
    api = meta.client()
    page = await api.get_updates(1200, timeout=20)
    request = meta.calls("updates")[0]
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"
    assert dict(request.url.params) == {"timeout": "20", "limit": "50", "offset": "1200"}
    assert page.next_offset == 1287
    assert page.messages[0]["text"]["body"] == "Hello agent"
    assert page.contacts[0]["profile"]["name"] == "Alex"
    assert page.statuses == []


@pytest.mark.asyncio
async def test_empty_poll_204_has_no_offset_and_offset_none_means_head():
    meta = FakeMeta()
    page = await meta.client().get_updates(None, timeout=99, limit=500)
    assert page.next_offset is None and page.messages == []
    assert dict(meta.calls("updates")[0].url.params) == {"timeout": "25", "limit": "100"}


@pytest.mark.asyncio
async def test_send_text_payload_and_reply_context():
    meta = FakeMeta()
    meta.queue("messages", httpx.Response(200, json=fixture_json("send_response.json")))
    wamid = await meta.client().send_text(CREATOR, "hi", reply_to="wamid.in1")
    assert wamid == "wamid.HBg..."
    assert meta.bodies("messages")[0] == {
        "messaging_product": "whatsapp",
        "to": CREATOR,
        "type": "text",
        "text": {"body": "hi"},
        "context": {"message_id": "wamid.in1"},
    }


@pytest.mark.asyncio
async def test_mark_read_with_typing_payload():
    meta = FakeMeta()
    await meta.client().mark_read("wamid.in1", typing=True)
    assert meta.bodies("statuses")[0] == {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": "wamid.in1",
        "typing_indicator": {"type": "text"},
    }


@pytest.mark.parametrize(
    ("endpoint", "response", "expected"),
    [
        ("updates", error_response(401, 190), c.AuthError),
        ("updates", error_response(400, 100), c.AuthError),
        ("updates", error_response(409, 1752041), c.PollConflict),
        ("updates", error_response(429, 130429, {"Retry-After": "7"}), c.RateLimited),
        ("updates", error_response(500, 2), c.Ambiguous),
        ("messages", error_response(403, 131005), c.NotCreatorError),
        ("messages", error_response(400, 131009), c.NotSent),
        ("messages", error_response(503, 131016), c.Retryable),
        ("messages", error_response(500, 2), c.Ambiguous),
        ("messages", httpx.Response(200, json={"unexpected": True}), c.MalformedResponse),
    ],
)
@pytest.mark.asyncio
async def test_error_classification(endpoint, response, expected):
    meta = FakeMeta()
    meta.queue(endpoint, response)
    api = meta.client()
    with pytest.raises(expected) as info:
        if endpoint == "updates":
            await api.get_updates(0)
        else:
            await api.send_text(CREATOR, "x")
    assert type(info.value) is expected
    assert API_KEY not in info.value.describe()
    if expected is c.RateLimited:
        assert info.value.retry_after == 7.0


def test_manual_error_envelope_fields():
    err = c.classify_response(httpx.Response(400, json=fixture_json("error_131009.json")), "send")
    assert isinstance(err, c.NotSent) and not err.retryable
    assert (err.status, err.code, err.fbtrace_id) == (400, 131009, "AW7bqWj4...")


@pytest.mark.parametrize(
    ("exc", "write", "expected"),
    [
        (httpx.ConnectError("boom"), True, c.Retryable),
        (httpx.ReadTimeout("slow"), True, c.Ambiguous),
        (httpx.ReadTimeout("slow"), False, c.Retryable),
        (httpx.RemoteProtocolError("reset"), True, c.Ambiguous),
    ],
)
@pytest.mark.asyncio
async def test_transport_failures_distinguish_not_sent_from_ambiguous(exc, write, expected):
    meta = FakeMeta()
    endpoint = "messages" if write else "updates"
    meta.queue(endpoint, exc)
    api = meta.client()
    with pytest.raises(expected):
        if write:
            await api.send_text(CREATOR, "x")
        else:
            await api.get_updates(0)


@pytest.mark.asyncio
async def test_malformed_updates_body_is_rejected():
    meta = FakeMeta()
    meta.queue("updates", httpx.Response(200, json={"object": "something_else", "entry": [], "next_offset": 1}))
    with pytest.raises(c.MalformedResponse):
        await meta.client().get_updates(0)


def test_rate_window_allows_burst_then_waits_and_penalizes():
    now = [100.0]
    window = c.RateWindow(3, 60.0, clock=lambda: now[0])
    assert [window.try_acquire() for _ in range(4)] == [True, True, True, False]
    assert window.remaining() == 0 and window.delay() == pytest.approx(60.0)
    now[0] += 60.0
    assert window.remaining() == 3
    window.penalize(10.0)
    assert window.remaining() == 0 and window.delay() == pytest.approx(10.0)


def test_client_requires_key():
    with pytest.raises(ValueError):
        c.AgentPlatformClient("")
