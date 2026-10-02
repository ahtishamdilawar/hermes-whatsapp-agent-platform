"""Standalone warnings distinguish uncertain message delivery from failed uploads."""

import asyncio
from collections.abc import Callable

import httpx
import pytest
from wap_helpers import API_KEY, CREATOR, error_response, sample_png
from wap_plugin_under_test import adapter as mod
from wap_plugin_under_test.state import PollState, state_path


@pytest.fixture
def attachment(hermes_home, api_key, tmp_path):
    state = PollState.load(state_path(hermes_home, API_KEY))
    state.creator = CREATOR
    state.save()
    path = tmp_path / "chart.png"
    path.write_bytes(sample_png())
    return str(path)


def lost_ack(kind, request):
    if kind == "malformed_200":
        return httpx.Response(200, json={})
    if kind == "read_timeout":
        raise httpx.ReadTimeout("fixture ACK lost", request=request)
    return error_response(500, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("ack", ["malformed_200", "read_timeout", "http_500"])
async def test_accepted_message_without_ack_is_unknown(meta, attachment, ack):
    def accepted_but_ack_lost(request):
        assert meta._messages(request).status_code == 200
        return lost_ack(ack, request)

    meta.queue("messages", meta._messages, accepted_but_ack_lost)
    result = await mod._standalone_send(None, "self", "report", media_files=[attachment])
    assert result["success"] is True
    assert "media_delivered" not in result
    assert len(meta.sent_media) == 1
    assert len(meta.calls("messages")) == 2  # text + attachment; never retry an unknown send
    assert len(meta.calls("media_upload")) == 1
    [warning] = result["warnings"]
    assert warning.startswith("attachment 1 of 1 (chart.png): outcome unknown")
    assert "may have arrived" in warning and "not sent" not in warning
    assert attachment not in warning


@pytest.mark.asyncio
@pytest.mark.parametrize("ack", ["malformed_200", "read_timeout", "http_500"])
@pytest.mark.parametrize("definite_failure", [None, "before", "after"])
async def test_media_only_unknown_summary(meta, attachment, ack, definite_failure):
    def accepted_but_ack_lost(request):
        assert meta._messages(request).status_code == 200
        return lost_ack(ack, request)

    responses: list[httpx.Response | Callable] = [accepted_but_ack_lost]
    if definite_failure == "before":
        responses.insert(0, error_response(400, 131009))
    elif definite_failure == "after":
        responses.append(error_response(400, 131009))
    meta.queue("messages", *responses)
    result = await mod._standalone_send(None, "self", "", media_files=[attachment] * len(responses))
    assert not result.get("success") and "media_delivered" not in result
    assert result["error"].startswith("attachment delivery could not be confirmed: ")
    assert "no attachment could be delivered" not in result["error"]
    assert len(result["warnings"]) == len(responses)
    assert sum("outcome unknown" in warning for warning in result["warnings"]) == 1
    assert len(meta.sent_media) == 1
    assert len(meta.calls("messages")) == len(responses)
    assert len(meta.calls("media_upload")) == len(responses)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["messages", "media_upload"])
async def test_media_only_deadline_summary_distinguishes_message_from_upload(meta, attachment, monkeypatch, stage):
    monkeypatch.setattr(mod, "STANDALONE_MEDIA_DEADLINE", 0.5)

    async def stalled(request):
        await asyncio.sleep(5)
        raise AssertionError("deadline did not cancel the request")

    meta.queue(stage, stalled)
    result = await mod._standalone_send(None, "self", "", media_files=[attachment, attachment])
    assert not result.get("success") and "media_delivered" not in result
    expected = (
        "attachment delivery could not be confirmed" if stage == "messages" else "no attachment could be delivered"
    )
    assert result["error"].startswith(expected + ": ")
    first, second = result["warnings"]
    assert ("outcome unknown" in first) is (stage == "messages")
    assert "not sent" in second and "time limit" in second
    assert len(meta.calls("messages")) == (1 if stage == "messages" else 0)
    assert len(meta.calls("media_upload")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("ack", ["malformed_200", "read_timeout", "http_500"])
async def test_upload_uncertainty_does_not_mean_unknown_message_delivery(meta, attachment, ack):
    def upload_ack_lost(request):
        assert meta._upload(request).status_code == 200
        return lost_ack(ack, request)

    meta.queue("media_upload", upload_ack_lost, upload_ack_lost)
    result = await mod._standalone_send(None, "self", "report", media_files=[attachment])
    assert result["success"] is True and "media_delivered" not in result
    assert not meta.sent_media
    assert len(meta.calls("messages")) == 1  # only the text; uploads are invisible
    [warning] = result["warnings"]
    assert warning.startswith("attachment 1 of 1 (chart.png) not sent:")
    assert "may have arrived" not in warning
    assert attachment not in warning


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code", [(400, 131009), (401, 190), (403, 131005)])
async def test_confirmed_message_failure_remains_not_sent(meta, attachment, status, code):
    meta.queue("messages", meta._messages, error_response(status, code))
    result = await mod._standalone_send(None, "self", "report", media_files=[attachment])
    assert result["success"] is True and "media_delivered" not in result
    assert not meta.sent_media
    assert len(meta.calls("messages")) == 2 and len(meta.calls("media_upload")) == 1
    [warning] = result["warnings"]
    assert warning.startswith("attachment 1 of 1 (chart.png) not sent:")
    assert "outcome unknown" not in warning and "may have arrived" not in warning
    assert attachment not in warning


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage,ack",
    [
        ("media_upload", "malformed_200"),
        ("media_upload", "read_timeout"),
        ("media_upload", "http_500"),
        ("media_upload", "rejected"),
        ("messages", "rejected"),
    ],
)
async def test_media_only_definite_failure_summary(meta, attachment, stage, ack):

    def failed(request):
        return error_response(400, 131009) if ack == "rejected" else lost_ack(ack, request)

    meta.queue(stage, failed, failed)
    result = await mod._standalone_send(None, "self", "", media_files=[attachment])
    assert not result.get("success") and "media_delivered" not in result
    assert result["error"].startswith("no attachment could be delivered: ")
    assert "may have arrived" not in result["error"]
    assert not meta.sent_media
    assert len(meta.calls("messages")) == (1 if stage == "messages" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("ack", ["malformed_200", "read_timeout", "http_500"])
@pytest.mark.parametrize("acknowledged_first", [False, True])
async def test_media_only_acknowledged_delivery_preserves_success(meta, attachment, ack, acknowledged_first):
    def accepted_but_ack_lost(request):
        assert meta._messages(request).status_code == 200
        return lost_ack(ack, request)

    responses = [meta._messages, accepted_but_ack_lost]
    if not acknowledged_first:
        responses.reverse()
    meta.queue("messages", *responses)
    result = await mod._standalone_send(None, "self", "", media_files=[attachment, attachment])
    assert result["success"] is True and result["media_delivered"] is True
    assert "error" not in result
    [warning] = result["warnings"]
    assert "outcome unknown" in warning and "may have arrived" in warning
    assert len(meta.sent_media) == 2
    assert len(meta.calls("messages")) == 2 and len(meta.calls("media_upload")) == 2
