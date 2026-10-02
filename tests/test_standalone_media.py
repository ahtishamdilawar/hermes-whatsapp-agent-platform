"""Cron / out-of-process delivery with attachments: ``adapter._standalone_send`` against FakeMeta.

Hermes calls the standalone sender with ``media_files`` as ``(path, is_voice)`` tuples on the last text chunk. The
contract checked here: ``media_delivered`` only when a file arrived, one ``warnings`` entry per file that didn't
(cron turns each into a run error), no host paths in any message, and an error when nothing at all was delivered.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from wap_helpers import API_KEY, CREATOR, sample_jpeg, sample_mp3, sample_pdf, sample_png, sample_webp
from wap_plugin_under_test import adapter as mod
from wap_plugin_under_test.client import RateWindow
from wap_plugin_under_test.state import PollState, state_path

OTHER = "user:777"


@pytest.fixture
def creator(hermes_home, api_key):
    state = PollState.load(state_path(hermes_home, API_KEY))
    state.creator = CREATOR
    state.save()
    return CREATOR


def put(tmp_path, name: str, data: bytes) -> str:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


def types_sent(meta) -> list[str]:
    return [body["type"] for body in meta.bodies("messages")]


def assert_path_free(result: dict, tmp_path) -> None:
    for text in [*result.get("warnings", []), result.get("error") or ""]:
        assert str(tmp_path) not in text and "\\" not in text


@pytest.mark.asyncio
async def test_text_then_two_files_delivered(meta, creator, tmp_path):
    files = [(put(tmp_path, "chart.png", sample_png()), False), (put(tmp_path, "report.pdf", sample_pdf()), False)]
    result = await mod._standalone_send(None, "self", "**daily** report", media_files=files)
    assert result["success"] is True and result["media_delivered"] is True
    assert "warnings" not in result and "error" not in result
    assert types_sent(meta) == ["text", "image", "document"]
    assert all(body["to"] == CREATOR for body in meta.bodies("messages"))
    assert len(meta.calls("media_upload")) == 2
    assert result["message_id"] == meta.sent_media[-1].wamid
    assert meta.sent_media[1].filename == "report.pdf"
    assert all(sent.caption is None for sent in meta.sent_media)  # the text went as its own message


@pytest.mark.asyncio
@pytest.mark.parametrize("thread_id", ["123", "wamid.reply", " "])
@pytest.mark.parametrize("payload", ["text", "media", "both"])
async def test_thread_id_is_rejected_before_any_delivery(meta, creator, tmp_path, thread_id, payload):
    message = "report" if payload != "media" else ""
    files = [(put(tmp_path, "a.png", sample_png()), False)] if payload != "text" else []
    result = await mod._standalone_send(None, "self", message, thread_id=thread_id, media_files=files)
    assert not result.get("success")
    assert "thread_id" in result["error"] and "not supported" in result["error"]
    assert "message_id" not in result and "media_delivered" not in result
    assert meta.calls("messages") == [] and meta.calls("media_upload") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("thread_id", [None, ""])
async def test_absent_thread_id_preserves_text_and_media_delivery(meta, creator, tmp_path, thread_id):
    files = [(put(tmp_path, "a.png", sample_png()), False)]
    result = await mod._standalone_send(None, "self", "report", thread_id=thread_id, media_files=files)
    assert result["success"] is True and result["media_delivered"] is True
    assert types_sent(meta) == ["text", "image"]


@pytest.mark.asyncio
async def test_bare_paths_are_tolerated(meta, creator, tmp_path):
    result = await mod._standalone_send(None, "self", "x", media_files=[put(tmp_path, "a.jpg", sample_jpeg())])
    assert result["media_delivered"] is True and types_sent(meta) == ["text", "image"]


@pytest.mark.asyncio
async def test_is_voice_audio_goes_as_a_plain_audio_attachment(meta, creator, tmp_path):
    result = await mod._standalone_send(None, "self", "x", media_files=[(put(tmp_path, "a.mp3", sample_mp3()), True)])
    assert result["media_delivered"] is True and types_sent(meta) == ["text", "audio"]


@pytest.mark.asyncio
async def test_missing_file_gets_a_warning_and_the_rest_is_delivered(meta, creator, tmp_path):
    missing = str(tmp_path / "secret-dir" / "gone.pdf")
    files = [(missing, False), (put(tmp_path, "b.png", sample_png()), False)]
    result = await mod._standalone_send(None, "self", "report", media_files=files)
    assert result["success"] is True and result["media_delivered"] is True
    assert len(result["warnings"]) == 1
    assert "attachment 1 of 2" in result["warnings"][0] and "gone.pdf" in result["warnings"][0]
    assert "not found" in result["warnings"][0]
    assert_path_free(result, tmp_path)
    assert types_sent(meta) == ["text", "image"]


@pytest.mark.asyncio
async def test_text_delivered_but_every_file_failed(meta, creator, tmp_path):
    result = await mod._standalone_send(None, "self", "report", media_files=[(str(tmp_path / "nope.pdf"), False)])
    # Hermes's convention (cron ``_deliver_standalone``): success for the text, the file in ``warnings``.
    assert result["success"] is True and "media_delivered" not in result
    assert len(result["warnings"]) == 1
    assert types_sent(meta) == ["text"]


@pytest.mark.asyncio
async def test_media_only_job_whose_only_file_fails_is_an_error(meta, creator, tmp_path):
    result = await mod._standalone_send(None, "self", "", media_files=[(str(tmp_path / "nope.pdf"), False)])
    assert not result.get("success") and "error" in result
    assert "media_delivered" not in result
    assert len(result["warnings"]) == 1 and "nope.pdf" in result["warnings"][0]
    assert_path_free(result, tmp_path)
    assert meta.calls("messages") == [] and meta.calls("media_upload") == []


@pytest.mark.asyncio
async def test_media_only_job_delivered(meta, creator, tmp_path):
    result = await mod._standalone_send(None, "self", "", media_files=[(put(tmp_path, "a.png", sample_png()), False)])
    assert result["success"] is True and result["media_delivered"] is True
    assert types_sent(meta) == ["image"]


@pytest.mark.asyncio
async def test_rejected_upload_is_a_warning(meta, creator, tmp_path):
    from wap_helpers import unsupported_mime_response

    meta.queue("media_upload", unsupported_mime_response("image/png"))
    files = [(put(tmp_path, "a.png", sample_png()), False), (put(tmp_path, "b.png", sample_png()), False)]
    result = await mod._standalone_send(None, "self", "report", media_files=files)
    assert result["media_delivered"] is True
    assert len(result["warnings"]) == 1 and "attachment 1 of 2" in result["warnings"][0]
    assert "rejected" in result["warnings"][0]
    assert types_sent(meta) == ["text", "image"]


@pytest.mark.asyncio
async def test_deadline_passed_remaining_files_are_warned(meta, creator, tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "STANDALONE_MEDIA_DEADLINE", 0.3)

    def slow_send(request):
        time.sleep(0.4)  # the first file's message takes the whole deadline
        return meta._messages(request)

    meta.queue("messages", meta._messages, slow_send)  # the text, then the first file
    files = [(put(tmp_path, f"{n}.png", sample_png()), False) for n in "abc"]
    result = await mod._standalone_send(None, "self", "report", media_files=files)
    assert result["success"] is True and result["media_delivered"] is True
    assert [w.split(" not sent")[0] for w in result["warnings"]] == [
        "attachment 2 of 3 (b.png)",
        "attachment 3 of 3 (c.png)",
    ]
    assert all("time limit" in w for w in result["warnings"])
    assert len(meta.calls("media_upload")) == 1 and types_sent(meta) == ["text", "image"]


@pytest.mark.asyncio
async def test_budget_wait_is_bounded_by_the_time_left(meta, creator, tmp_path, monkeypatch):
    real = mod.AgentPlatformClient

    def full_upload_budget(*args, **kwargs):
        client = real(*args, **kwargs)
        client.limits["media_upload"] = window = RateWindow(1, window=600.0)
        window.try_acquire()
        return client

    monkeypatch.setattr(mod, "AgentPlatformClient", full_upload_budget)
    started = time.monotonic()
    result = await mod._standalone_send(None, "self", "x", media_files=[(put(tmp_path, "a.png", sample_png()), False)])
    assert time.monotonic() - started < 5  # no 10-minute wait: the bound is the deadline's time left
    assert result["success"] is True and "media_delivered" not in result
    assert "send limit" in result["warnings"][0]
    assert meta.calls("media_upload") == []


@pytest.mark.asyncio
async def test_non_creator_target_gets_no_upload(meta, creator, tmp_path):
    result = await mod._standalone_send(None, OTHER, "x", media_files=[(put(tmp_path, "a.png", sample_png()), False)])
    assert "creator" in result["warnings"][0] and "media_delivered" not in result
    assert meta.calls("media_upload") == []


@pytest.mark.asyncio
async def test_explicit_target_without_a_confirmed_creator_gets_no_upload(meta, api_key, tmp_path):
    result = await mod._standalone_send(None, CREATOR, "x", media_files=[(put(tmp_path, "a.png", sample_png()), False)])
    assert result["success"] is True and "creator" in result["warnings"][0]
    assert meta.calls("media_upload") == [] and types_sent(meta) == ["text"]


@pytest.mark.asyncio
async def test_denylisted_files_are_never_uploaded(meta, creator, hermes_home, tmp_path):
    state_file = str(state_path(hermes_home, API_KEY))
    files = [(put(tmp_path, ".env", b"KEY=1\n"), False), (state_file, False), (put(tmp_path, "k.pem", b"x"), False)]
    result = await mod._standalone_send(None, "self", "x", media_files=files)
    assert len(result["warnings"]) == 3 and all("never sent" in w for w in result["warnings"])
    assert meta.calls("media_upload") == [] and "media_delivered" not in result
    assert_path_free(result, tmp_path)


@pytest.mark.asyncio
async def test_force_document_sends_the_image_untouched(meta, creator, tmp_path):
    data = sample_webp()
    result = await mod._standalone_send(
        None, "self", "x", media_files=[(put(tmp_path, "a.webp", data), False)], force_document=True
    )
    assert result["media_delivered"] is True
    assert meta.sent_media[0].type == "document" and meta.sent_media[0].media.data == data


@pytest.mark.asyncio
async def test_webp_is_converted_without_force_document(meta, creator, tmp_path):
    result = await mod._standalone_send(
        None, "self", "x", media_files=[(put(tmp_path, "a.webp", sample_webp()), False)]
    )
    assert result["media_delivered"] is True
    assert meta.sent_media[0].type == "image" and meta.sent_media[0].media.mime_type in ("image/png", "image/jpeg")


@pytest.mark.asyncio
@pytest.mark.parametrize("off", ["env", "config"])
async def test_switch_off_keeps_the_text_note(meta, creator, tmp_path, monkeypatch, off):
    pconfig = None
    if off == "env":
        monkeypatch.setenv(mod.MEDIA_ENV, "false")
    else:
        pconfig = SimpleNamespace(extra={mod.MEDIA_KEY: False})
    files = [(put(tmp_path, "a.png", sample_png()), False), (put(tmp_path, "b.pdf", sample_pdf()), False)]
    result = await mod._standalone_send(pconfig, "self", "report", media_files=files)
    assert result["success"] is True and "media_delivered" not in result and "warnings" not in result
    assert types_sent(meta) == ["text"] and meta.calls("media_upload") == []
    assert "2 attachment(s)" in meta.bodies("messages")[0]["text"]["body"]


@pytest.mark.asyncio
async def test_unknown_keyword_arguments_are_accepted(meta, creator, tmp_path):
    result = await mod._standalone_send(None, "self", "x", media_files=[], thread_id=None, caption="new in Hermes")
    assert result["success"] is True and "media_delivered" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("thread_id", [None, "", "123"])
async def test_through_hermes_send_to_platform(meta, creator, tmp_path, monkeypatch, thread_id):
    """The real cron/send_message routing: no "attachments were omitted" warning once media was delivered."""
    send_message_tool = pytest.importorskip("tools.send_message_tool")
    send_to_platform = getattr(send_message_tool, "_send_to_platform", None)
    if send_to_platform is None:
        pytest.skip("this Hermes has no _send_to_platform")
    from gateway.config import Platform
    from gateway.platform_registry import PlatformEntry, platform_registry

    entry = PlatformEntry(
        name=mod.PLATFORM_NAME,
        label=mod.LABEL,
        adapter_factory=lambda cfg: None,
        check_fn=lambda: True,
        standalone_sender_fn=mod._standalone_send,
        max_message_length=mod.MAX_TEXT_LENGTH,
    )
    real_get = platform_registry.get
    monkeypatch.setattr(platform_registry, "get", lambda name: entry if name == mod.PLATFORM_NAME else real_get(name))
    monkeypatch.setattr(send_message_tool, "_live_adapter", lambda platform: (None, None), raising=False)
    pconfig = SimpleNamespace(extra={})
    good = (put(tmp_path, "a.png", sample_png()), False)
    result = await send_to_platform(
        Platform(mod.PLATFORM_NAME), pconfig, "self", "report", thread_id=thread_id, media_files=[good]
    )
    if thread_id:
        assert not result.get("success") and "thread_id" in result["error"]
        assert meta.calls("messages") == [] and meta.calls("media_upload") == []
        return
    assert result["success"] is True and result["media_delivered"] is True and not result.get("warnings")

    missing = (str(tmp_path / "gone.pdf"), False)
    result = await send_to_platform(Platform(mod.PLATFORM_NAME), pconfig, "self", "report", media_files=[missing])
    assert result["success"] is True and any("gone.pdf" in w for w in result["warnings"])


@pytest.mark.asyncio
async def test_media_only_job_whose_upload_fails_is_an_error(meta, creator, tmp_path):
    from wap_helpers import unsupported_mime_response

    meta.queue("media_upload", unsupported_mime_response("image/png"))
    result = await mod._standalone_send(None, "self", "", media_files=[(put(tmp_path, "a.png", sample_png()), False)])
    assert not result.get("success") and result.get("error")
    assert "media_delivered" not in result
    assert len(result["warnings"]) == 1 and "a.png" in result["warnings"][0] and "rejected" in result["warnings"][0]
    assert_path_free(result, tmp_path)
    assert len(meta.calls("media_upload")) == 1 and meta.calls("messages") == []


def slow(meta, handler, seconds: float = 5.0):
    """An async FakeMeta handler that stalls (without blocking the loop), then answers normally."""

    async def stalled(request):
        await asyncio.sleep(seconds)
        return handler(request)

    return stalled


@pytest.mark.asyncio
async def test_deadline_cancels_a_media_message_in_flight_and_reports_outcome_unknown(
    meta, creator, tmp_path, monkeypatch
):
    monkeypatch.setattr(mod, "STANDALONE_MEDIA_DEADLINE", 0.5)
    meta.queue("messages", meta._messages, slow(meta, meta._messages))  # the text, then the first file stalls
    files = [(put(tmp_path, f"{n}.png", sample_png()), False) for n in "ab"]
    started = time.monotonic()
    result = await mod._standalone_send(None, "self", "report", media_files=files)
    assert time.monotonic() - started < 3  # returned at the deadline, not after the stalled send
    assert result["success"] is True and "media_delivered" not in result  # the text arrived
    first, second = result["warnings"]
    assert first.startswith("attachment 1 of 2 (a.png): outcome unknown") and "may have arrived" in first
    assert second.startswith("attachment 2 of 2 (b.png) not sent") and "time limit" in second
    assert len(meta.calls("media_upload")) == 1
    assert_path_free(result, tmp_path)


@pytest.mark.asyncio
async def test_deadline_cancels_an_upload_in_flight_and_reports_the_file_not_sent(meta, creator, tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "STANDALONE_MEDIA_DEADLINE", 0.5)
    meta.queue("media_upload", slow(meta, meta._upload))
    started = time.monotonic()
    result = await mod._standalone_send(None, "self", "", media_files=[(put(tmp_path, "a.pdf", sample_pdf()), False)])
    assert time.monotonic() - started < 3
    assert not result.get("success") and result.get("error") and "media_delivered" not in result
    [warning] = result["warnings"]
    assert warning.startswith("attachment 1 of 1 (a.pdf) not sent") and "time limit" in warning
    assert "outcome unknown" not in warning
    assert meta.calls("messages") == []


@pytest.mark.asyncio
async def test_standalone_logs_never_carry_paths_or_names(meta, creator, tmp_path, caplog):
    import logging

    from wap_helpers import unsupported_mime_response

    caplog.set_level(logging.DEBUG)
    meta.queue("media_upload", unsupported_mime_response("image/png"))
    files = [
        (put(tmp_path / "zzHOSTDIRzz", "zzNAMEzz.png", sample_png()), False),
        (str(tmp_path / "zzHOSTDIRzz" / "zzNAMEzz-gone.pdf"), False),
        (put(tmp_path / "zzHOSTDIRzz", ".env", b"K=1\n"), False),
    ]
    result = await mod._standalone_send(None, "self", "report", media_files=files)
    assert len(result["warnings"]) == 3
    plugin_logs = [r.getMessage() for r in caplog.records if r.name.startswith("wap_plugin_under_test")]
    assert plugin_logs
    for text in plugin_logs:
        for secret in (str(tmp_path), "zzHOSTDIRzz", "zzNAMEzz", *(u.media_id for u in meta.uploads)):
            assert secret not in text, (secret, text)
