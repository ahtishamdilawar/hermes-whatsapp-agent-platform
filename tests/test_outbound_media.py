"""Outbound media: the adapter's send_* overrides and the shared ``media_outbound.send_media_file`` against FakeMeta.

Notices are checked by what was *sent* (a text message), never by wording: Hermes translates its own on main.
"""

from __future__ import annotations

import ast
import glob
import inspect
import io
import os
import shutil
import tempfile
import time
import wave
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import quote

import pytest
import pytest_asyncio
from wap_helpers import (
    CREATOR,
    MIB,
    bad_field_response,
    caption_too_long_response,
    error_response,
    fixture_json,
    media_type_mismatch_response,
    no_media_found_send_response,
    rate_limited_response,
    sample_gif,
    sample_jpeg,
    sample_mp3,
    sample_mp4,
    sample_ogg_opus,
    sample_pdf,
    sample_png,
    sample_webp,
    unsupported_mime_response,
)
from wap_plugin_under_test import adapter as mod
from wap_plugin_under_test import hermes_compat, media_outbound
from wap_plugin_under_test import media as policy
from wap_plugin_under_test.client import RateWindow

OTHER = "user:777"


def make_adapter():
    from gateway.config import PlatformConfig

    a = mod.WhatsAppAgentPlatformAdapter(PlatformConfig(enabled=True))
    a._poll_min_interval, a._poll_timeout, a._backoff_base = 0.01, 0, 0.01
    a._media_retry_backoff = 0.0
    a.handle_message = AsyncMock()
    a._notify_fatal_error = AsyncMock()
    return a


@pytest_asyncio.fixture
async def ad(meta, api_key):
    a = make_adapter()
    assert await a.connect()
    a._state.creator = CREATOR
    yield a
    await a.disconnect()


def put(tmp_path: Path, name: str, data: bytes) -> str:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


def sample_wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\0\0\0\0" * 800)
    return buf.getvalue()


def message_types(meta) -> list[str]:
    return [body["type"] for body in meta.bodies("messages")]


def assert_path_free(result, tmp_path: Path, *names: str) -> None:
    assert str(tmp_path) not in (result.error or "")
    for name in names:
        assert name not in (result.error or "")


# ------------------------------------------------------------------ contract with Hermes
def test_all_media_methods_are_overridden_with_hermes_parameter_names():
    from gateway.platforms.base import BasePlatformAdapter

    for name in ("send_image_file", "send_video", "send_voice", "send_document", "send_image"):
        ours, base = getattr(mod.WhatsAppAgentPlatformAdapter, name), getattr(BasePlatformAdapter, name)
        assert ours is not base  # the send_message tool detects media support by override only
        mine = list(inspect.signature(ours).parameters.values())
        theirs = list(inspect.signature(base).parameters.values())
        named = [(p.name, p.default) for p in theirs if p.kind is not p.VAR_KEYWORD]
        assert [(p.name, p.default) for p in mine if p.kind is not p.VAR_KEYWORD] == named
        assert mine[-1].kind is inspect.Parameter.VAR_KEYWORD
    assert mod.WhatsAppAgentPlatformAdapter.send_multiple_images is BasePlatformAdapter.send_multiple_images


def test_media_outbound_is_hermes_free():
    tree = ast.parse(Path(media_outbound.__file__).read_text(encoding="utf-8"))
    modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {
        alias.name for n in ast.walk(tree) if isinstance(n, ast.Import) for alias in n.names
    }
    assert not {m for m in modules if m and m.split(".")[0] in ("gateway", "hermes_constants", "agent", "tools")}


# ------------------------------------------------------------------ happy paths
@pytest.mark.asyncio
async def test_send_image_file_uploads_then_sends_with_caption_and_quote(ad, meta, tmp_path):
    data = sample_png()
    result = await ad.send_image_file(CREATOR, put(tmp_path, "chart.png", data), caption="**Q3**", reply_to="wamid.in1")
    assert result.success
    [upload] = meta.uploads
    assert (upload.messaging_product, upload.type, upload.filename, upload.data) == (
        "whatsapp",
        "image/png",
        "chart.png",
        data,
    )
    [sent] = meta.sent_media
    assert (sent.type, sent.media_id, sent.caption, sent.to) == ("image", upload.media_id, "*Q3*", CREATOR)
    assert sent.context == {"message_id": "wamid.in1"} and sent.filename is None
    assert result.message_id == sent.wamid


@pytest.mark.asyncio
async def test_send_document_keeps_file_name_and_caption(ad, meta, tmp_path):
    path = put(tmp_path, "tmpab12.pdf", sample_pdf())
    result = await ad.send_document(chat_id=CREATOR, file_path=path, caption="the report", file_name="Report.pdf")
    assert result.success
    assert meta.uploads[0].type == "application/pdf"
    [sent] = meta.sent_media
    assert (sent.type, sent.filename, sent.caption) == ("document", "Report.pdf", "the report")


@pytest.mark.asyncio
async def test_send_video_mp4(ad, meta, tmp_path):
    result = await ad.send_video(CREATOR, put(tmp_path, "clip.mp4", sample_mp4()), caption="look")
    assert result.success
    assert meta.uploads[0].type == "video/mp4"
    assert (meta.sent_media[0].type, meta.sent_media[0].caption) == ("video", "look")


@pytest.mark.parametrize(
    ("name", "data", "mime"), [("reply.mp3", sample_mp3(), "audio/mpeg"), ("note.ogg", sample_ogg_opus(), "audio/ogg")]
)
@pytest.mark.asyncio
async def test_send_voice_sends_accepted_audio_as_is(ad, meta, tmp_path, name, data, mime):
    result = await ad.send_voice(chat_id=CREATOR, audio_path=put(tmp_path, name, data), is_voice=True)
    assert result.success
    assert (meta.uploads[0].type, meta.uploads[0].data) == (mime, data)
    assert (meta.sent_media[0].type, meta.sent_media[0].caption) == ("audio", None)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
@pytest.mark.asyncio
async def test_send_voice_wav_is_transcoded_and_the_temp_file_removed(ad, meta, tmp_path):
    pattern = os.path.join(tempfile.gettempdir(), "voice_transcode_*")
    before = set(glob.glob(pattern))
    result = await ad.send_voice(CREATOR, put(tmp_path, "tts.wav", sample_wav()))
    assert result.success
    assert meta.uploads[0].type == "audio/ogg" and meta.uploads[0].data.startswith(b"OggS")
    assert meta.sent_media[0].type == "audio"
    assert set(glob.glob(pattern)) <= before


@pytest.mark.asyncio
async def test_send_voice_wav_without_ffmpeg_goes_as_a_document(ad, meta, tmp_path, monkeypatch):
    monkeypatch.setattr(hermes_compat, "transcode_to_ogg_opus", lambda path, **kw: None)
    data = sample_wav()
    result = await ad.send_voice(CREATOR, put(tmp_path, "tts.wav", data))
    assert result.success
    assert (meta.uploads[0].type, meta.uploads[0].data) == ("application/octet-stream", data)
    [sent] = meta.sent_media
    assert sent.type == "document" and sent.filename == "tts.wav" and "sent as a file" in sent.caption


@pytest.mark.asyncio
async def test_transcoder_output_is_deleted_after_the_upload(ad, meta, tmp_path, monkeypatch):
    produced = []

    def fake_transcode(path, **kw):
        out = tmp_path / "out" / "converted.ogg"
        out.parent.mkdir(exist_ok=True)
        out.write_bytes(sample_ogg_opus())
        produced.append(out)
        return str(out)

    monkeypatch.setattr(hermes_compat, "transcode_to_ogg_opus", fake_transcode)
    result = await ad.send_voice(CREATOR, put(tmp_path, "a.flac", b"fLaC" + b"\0" * 64))
    assert result.success and meta.uploads[0].data == sample_ogg_opus()
    assert produced and not produced[0].exists()


@pytest.mark.asyncio
async def test_webp_image_is_converted_to_png(ad, meta, tmp_path):
    result = await ad.send_image_file(CREATOR, put(tmp_path, "gen.webp", sample_webp()))
    assert result.success
    upload = meta.uploads[0]
    assert (upload.type, upload.filename) == ("image/png", "gen.png") and upload.data.startswith(b"\x89PNG")
    assert meta.sent_media[0].type == "image"


@pytest.mark.asyncio
async def test_animated_gif_goes_as_an_untouched_document_with_a_note(ad, meta, tmp_path):
    data = sample_gif(animated=True)
    result = await ad.send_image_file(CREATOR, put(tmp_path, "dance.gif", data), caption="fun")
    assert result.success
    assert (meta.uploads[0].type, meta.uploads[0].data) == ("application/octet-stream", data)
    [sent] = meta.sent_media
    assert sent.type == "document" and sent.filename == "dance.gif"
    assert sent.caption.startswith("fun\n") and policy.downgrade_note(policy.REASON_ANIMATED) in sent.caption


@pytest.mark.asyncio
async def test_downgrade_note_is_hidden_when_warnings_are_suppressed(ad, meta, tmp_path, monkeypatch):
    monkeypatch.setattr(ad, "warning_notifications_enabled", lambda *a, **k: False)
    result = await ad.send_image_file(CREATOR, put(tmp_path, "dance.gif", sample_gif(animated=True)))
    assert result.success and meta.sent_media[0].caption is None


@pytest.mark.asyncio
async def test_mov_video_goes_as_a_document(ad, meta, tmp_path):
    data = b"\0\0\0\x14ftypqt  " + b"\0" * 64
    result = await ad.send_video(CREATOR, put(tmp_path, "IMG_1.MOV", data))
    assert result.success
    assert (meta.uploads[0].type, meta.sent_media[0].type, meta.sent_media[0].filename) == (
        "application/octet-stream",
        "document",
        "IMG_1.MOV",
    )


@pytest.mark.asyncio
async def test_markdown_goes_as_a_text_plain_document(ad, meta, tmp_path):
    result = await ad.send_document(CREATOR, put(tmp_path, "notes.md", b"# Title\n\nbody\n"))
    assert result.success
    assert meta.uploads[0].type == "text/plain"
    assert (meta.sent_media[0].type, meta.sent_media[0].filename) == ("document", "notes.md")


@pytest.mark.asyncio
async def test_forced_document_image_is_sent_untouched(ad, meta, tmp_path):
    png, webp = sample_png(mode="P"), sample_webp()
    assert (await ad.send_document(CREATOR, put(tmp_path, "scan.png", png))).success
    assert (await ad.send_image_file(CREATOR, put(tmp_path, "gen.webp", webp), force_document=True)).success
    assert [(u.type, u.data) for u in meta.uploads] == [("image/png", png), ("application/octet-stream", webp)]
    assert [s.type for s in meta.sent_media] == ["document", "document"]
    assert [s.filename for s in meta.sent_media] == ["scan.png", "gen.webp"]


# ------------------------------------------------------------------ refusals before any upload
@pytest.mark.asyncio
async def test_oversize_file_fails_final_without_upload(ad, meta, tmp_path):
    path = tmp_path / "huge.zip"
    with open(path, "wb") as fh:
        fh.truncate(16 * MIB + 1)
    result = await ad.send_document(CREATOR, str(path))
    assert not result.success and result.error.startswith("media not sent: file too large")
    assert result.retryable is False and result.raw_response["final"]
    assert meta.calls("media_upload") == [] and meta.calls("messages") == []
    assert_path_free(result, tmp_path, "huge.zip")


@pytest.mark.asyncio
async def test_empty_missing_and_directory_paths_are_refused(ad, meta, tmp_path):
    empty = put(tmp_path, "empty.pdf", b"")
    results = [
        await ad.send_document(CREATOR, empty),
        await ad.send_image_file(CREATOR, str(tmp_path / "gone.png")),
        await ad.send_document(CREATOR, str(tmp_path)),
    ]
    assert [r.success for r in results] == [False, False, False]
    assert [r.error for r in results] == [
        "media not sent: file is empty",
        f"media not sent: {media_outbound.REFUSED_MISSING}",
        f"media not sent: {media_outbound.REFUSED_NOT_FILE}",
    ]
    for r in results:
        assert_path_free(r, tmp_path, "empty.pdf", "gone.png")
    assert meta.calls("media_upload") == [] and meta.calls("messages") == []


@pytest.mark.parametrize("name", [".env", ".env.production", "id_rsa", "server.pem", ".git-credentials", ".netrc"])
@pytest.mark.asyncio
async def test_secret_looking_files_are_never_uploaded(ad, meta, tmp_path, name):
    result = await ad.send_document(CREATOR, put(tmp_path / "project", name, b"SECRET=1\n"))
    assert not result.success and result.error == f"media not sent: {media_outbound.REFUSED_DENIED}"
    assert "forbidden" not in result.error  # must not mark the chat as a dead target
    assert meta.calls("media_upload") == []
    assert_path_free(result, tmp_path, name)


@pytest.mark.asyncio
async def test_plugin_state_dir_is_never_uploaded(ad, meta):
    assert ad._state.path.exists()
    result = await ad.send_document(CREATOR, str(ad._state.path))
    other = ad._state.path.parent / "notes.txt"
    other.write_text("x", encoding="utf-8")
    result2 = await ad.send_document(CREATOR, str(other))
    assert not result.success and not result2.success
    assert meta.calls("media_upload") == []


@pytest.mark.parametrize("name", ["id_photo.jpg", "keynote.key", "id_rsa.pub"])
@pytest.mark.asyncio
async def test_lookalike_names_are_not_refused(ad, meta, tmp_path, name):
    data = sample_jpeg() if name.endswith(".jpg") else b"PK\x03\x04" + b"\0" * 64
    result = await ad.send_document(CREATOR, put(tmp_path, name, data))
    assert result.success and len(meta.uploads) == 1


@pytest.mark.asyncio
async def test_non_creator_recipient_gets_forbidden_without_upload(ad, meta, tmp_path):
    path = put(tmp_path, "a.pdf", sample_pdf())
    result = await ad.send_document(OTHER, path)
    assert not result.success and result.error.startswith("forbidden") and result.error_kind == "forbidden"
    ad._state.creator = None
    unknown = await ad.send_document("self", path)
    assert not unknown.success and unknown.error.startswith("recipient unknown")
    assert meta.calls("media_upload") == [] and meta.calls("messages") == []


@pytest.mark.asyncio
async def test_disconnected_adapter_reports_a_degraded_send_path(ad, meta, tmp_path):
    await ad._client._http.aclose()  # the fake's transport is not owned by the client
    result = await ad.send_image_file(CREATOR, put(tmp_path, "a.png", sample_png()))
    assert not result.success and result.error == "send_path_degraded"


# ------------------------------------------------------------------ captions
@pytest.mark.asyncio
async def test_caption_of_1024_units_rides_on_the_file(ad, meta, tmp_path):
    caption = "\U0001f600" * 512  # 1024 UTF-16 units
    result = await ad.send_image_file(CREATOR, put(tmp_path, "a.png", sample_png()), caption=caption)
    assert result.success and message_types(meta) == ["image"] and meta.sent_media[0].caption == caption


@pytest.mark.asyncio
async def test_caption_over_1024_units_goes_first_as_text_then_the_file(ad, meta, tmp_path):
    caption = "\U0001f600" * 512 + "x"  # 1025 units
    result = await ad.send_image_file(CREATOR, put(tmp_path, "a.png", sample_png()), caption=caption, reply_to="w.1")
    assert result.success
    bodies = meta.bodies("messages")
    assert [b["type"] for b in bodies] == ["text", "image"]
    assert bodies[0]["text"]["body"] == caption and bodies[0]["context"] == {"message_id": "w.1"}
    assert meta.sent_media[0].caption is None and meta.sent_media[0].context is None
    assert result.message_id == meta.sent_media[0].wamid


@pytest.mark.asyncio
async def test_audio_caption_goes_first_as_text(ad, meta, tmp_path):
    result = await ad.send_voice(CREATOR, put(tmp_path, "a.mp3", sample_mp3()), caption="**listen**")
    assert result.success
    bodies = meta.bodies("messages")
    assert [b["type"] for b in bodies] == ["text", "audio"] and bodies[0]["text"]["body"] == "*listen*"
    assert "caption" not in bodies[1]["audio"]


@pytest.mark.asyncio
async def test_failed_caption_text_stops_before_the_file_is_sent(ad, meta, tmp_path):
    meta.queue("messages", error_response(403, 131005))
    result = await ad.send_voice(CREATOR, put(tmp_path, "a.mp3", sample_mp3()), caption="hi")
    assert not result.success and result.error_kind == "forbidden"
    assert meta.sent_media == []


# ------------------------------------------------------------------ retries and error mapping
@pytest.mark.asyncio
async def test_upload_429_waits_and_retries(ad, meta, tmp_path):
    meta.queue("media_upload", rate_limited_response(0.05))
    result = await ad.send_image_file(CREATOR, put(tmp_path, "a.png", sample_png()))
    assert result.success and len(meta.calls("media_upload")) == 2 and len(meta.sent_media) == 1


@pytest.mark.asyncio
async def test_upload_500_is_retried_once(ad, meta, tmp_path):
    meta.queue("media_upload", error_response(500, 2))
    result = await ad.send_image_file(CREATOR, put(tmp_path, "a.png", sample_png()))
    assert result.success and len(meta.calls("media_upload")) == 2


@pytest.mark.asyncio
async def test_upload_rejection_is_final(ad, meta, tmp_path):
    meta.queue("media_upload", unsupported_mime_response("image/png"))
    result = await ad.send_image_file(CREATOR, put(tmp_path, "a.png", sample_png()))
    assert not result.success and result.error.startswith("media rejected") and result.retryable is False
    assert len(meta.calls("media_upload")) == 1 and meta.calls("messages") == []


@pytest.mark.asyncio
async def test_message_429_resends_the_same_media_id(ad, meta, tmp_path):
    meta.queue("messages", rate_limited_response(0))
    result = await ad.send_image_file(CREATOR, put(tmp_path, "a.png", sample_png()))
    assert result.success and len(meta.calls("media_upload")) == 1
    bodies = meta.bodies("messages")
    assert len(bodies) == 2 and bodies[0]["image"]["id"] == bodies[1]["image"]["id"] == meta.uploads[0].media_id


@pytest.mark.asyncio
async def test_message_503_resends_the_same_media_id(ad, meta, tmp_path):
    meta.queue("messages", error_response(503, 131016))
    result = await ad.send_document(CREATOR, put(tmp_path, "a.pdf", sample_pdf()))
    assert result.success and len(meta.calls("media_upload")) == 1 and len(meta.calls("messages")) == 2


@pytest.mark.asyncio
async def test_media_gone_on_send_reuploads_exactly_once(ad, meta, tmp_path):
    meta.queue("messages", no_media_found_send_response("expired-id"))
    result = await ad.send_image_file(CREATOR, put(tmp_path, "a.png", sample_png()))
    assert result.success and len(meta.uploads) == 2
    assert meta.sent_media[0].media_id == meta.uploads[1].media_id


@pytest.mark.asyncio
async def test_media_gone_twice_is_final(ad, meta, tmp_path):
    meta.queue("messages", no_media_found_send_response("a"), no_media_found_send_response("b"))
    result = await ad.send_image_file(CREATOR, put(tmp_path, "a.png", sample_png()))
    assert not result.success and result.error.startswith("media rejected") and result.retryable is False
    assert len(meta.uploads) == 2 and len(meta.calls("messages")) == 2


@pytest.mark.asyncio
async def test_media_rejected_is_final_without_retry(ad, meta, tmp_path):
    meta.queue("messages", media_type_mismatch_response())
    result = await ad.send_image_file(CREATOR, put(tmp_path, "a.png", sample_png()))
    assert not result.success and result.retryable is False and result.raw_response["final"]
    assert len(meta.calls("messages")) == 1 and len(meta.uploads) == 1


@pytest.mark.asyncio
async def test_ambiguous_media_message_is_never_retried(ad, meta, tmp_path):
    meta.queue("messages", error_response(500, 2))
    result = await ad.send_image_file(CREATOR, put(tmp_path, "a.png", sample_png()))
    assert not result.success and result.error.startswith("outcome unknown")
    assert len(meta.calls("messages")) == 1


@pytest.mark.asyncio
async def test_code_100_caption_error_with_a_quote_is_not_resent_without_it(ad, meta, tmp_path):
    meta.queue("messages", caption_too_long_response())
    result = await ad.send_image_file(CREATOR, put(tmp_path, "a.png", sample_png()), caption="c", reply_to="w.1")
    assert not result.success and result.error.startswith("media rejected")
    assert len(meta.calls("messages")) == 1


@pytest.mark.asyncio
async def test_rejected_quote_is_resent_without_it_with_the_same_media_id(ad, meta, tmp_path):
    meta.queue("messages", bad_field_response("context.message_id is not a wamid from this conversation"))
    result = await ad.send_image_file(CREATOR, put(tmp_path, "a.png", sample_png()), reply_to="wamid.old")
    assert result.success
    first, second = meta.bodies("messages")
    assert first["context"] == {"message_id": "wamid.old"} and "context" not in second
    assert first["image"]["id"] == second["image"]["id"] and len(meta.uploads) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["send:stale_context_media_made_up_wamid", "send:stale_context_media_garbage_id"])
async def test_live_stale_quote_errors_resend_without_the_quote(ad, meta, tmp_path, key):
    # Shapes recorded live on 2026-09-30 (P6): a media send quoting a wamid Meta doesn't know.
    live = fixture_json("live_media_errors.json")[key]
    meta.queue("messages", bad_field_response(live["error_data"]["details"]))
    result = await ad.send_image_file(CREATOR, put(tmp_path, "a.png", sample_png()), reply_to="wamid.old")
    assert result.success
    first, second = meta.bodies("messages")
    assert first["context"] == {"message_id": "wamid.old"} and "context" not in second
    assert first["image"]["id"] == second["image"]["id"] and len(meta.uploads) == 1


@pytest.mark.asyncio
async def test_upload_runs_outside_the_send_lock_and_the_message_inside(ad, meta, tmp_path):
    seen = {}

    def upload(request):
        seen["upload"] = ad._send_lock.locked()
        return meta._upload(request)

    def message(request):
        seen["message"] = ad._send_lock.locked()
        return meta._messages(request)

    meta.queue("media_upload", upload)
    meta.queue("messages", message)
    assert (await ad.send_image_file(CREATOR, put(tmp_path, "a.png", sample_png()))).success
    assert seen == {"upload": False, "message": True}


# ------------------------------------------------------------------ budgets
@pytest.mark.asyncio
async def test_exhausted_budget_sends_one_pacing_notice_then_the_files(ad, meta, tmp_path):
    ad._media_pacing_after, ad._media_wait_bound = 0.05, 5.0
    ad._client.limits["media_upload"] = window = RateWindow(1, window=0.2)
    window.try_acquire()
    paths = [put(tmp_path, f"{i}.png", sample_png()) for i in range(3)]
    results = [await ad.send_image_file(CREATOR, p) for p in paths]
    assert all(r.success for r in results)
    assert message_types(meta) == ["text", "image", "image", "image"]  # one notice, sent before the files


@pytest.mark.asyncio
async def test_budget_wait_over_the_bound_is_flood_control_without_upload(ad, meta, tmp_path):
    from gateway.delivery_ledger import flood_wait_seconds, is_flood_error

    ad._media_wait_bound = 1.0
    ad._client.limits["messages"] = window = RateWindow(1, window=60)
    window.try_acquire()
    result = await ad.send_image_file(CREATOR, put(tmp_path, "a.png", sample_png()))
    assert not result.success and is_flood_error(result.error) and flood_wait_seconds(result.error) >= 55
    assert result.error_kind == "rate_limited"
    assert meta.calls("media_upload") == [] and meta.calls("messages") == []


# ------------------------------------------------------------------ URL images, batches, switch, quotes
@pytest.mark.asyncio
async def test_send_image_url_is_fetched_by_hermes_then_sent_as_an_image(ad, meta, tmp_path, monkeypatch):
    cached = put(tmp_path, "cache/img_abc.jpg", sample_png())  # Hermes names it .jpg whatever the bytes are
    fetch = AsyncMock(return_value=cached)
    monkeypatch.setattr(hermes_compat, "cache_image_from_url", fetch)
    result = await ad.send_image(CREATOR, "https://example.com/a.png", caption="alt")
    assert result.success
    fetch.assert_awaited_once_with("https://example.com/a.png")
    assert meta.uploads[0].type == "image/png" and meta.sent_media[0].caption == "alt"


@pytest.mark.asyncio
async def test_send_image_url_falls_back_to_the_link(ad, meta, monkeypatch):
    monkeypatch.setattr(hermes_compat, "cache_image_from_url", AsyncMock(side_effect=ValueError("unsafe url")))
    result = await ad.send_image(CREATOR, "http://169.254.169.254/latest.png")
    assert result.success and meta.uploads == []
    assert [b["text"]["body"] for b in meta.bodies("messages")] == ["http://169.254.169.254/latest.png"]


@pytest.mark.asyncio
async def test_send_image_url_is_not_fetched_for_a_non_creator(ad, meta, monkeypatch):
    fetch = AsyncMock()
    monkeypatch.setattr(hermes_compat, "cache_image_from_url", fetch)
    result = await ad.send_image(OTHER, "https://example.com/a.png")
    assert not result.success and result.error.startswith("forbidden")
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_base_send_multiple_images_sends_each_through_send_image_file(ad, meta, tmp_path):
    paths = [put(tmp_path, f"img {i}.png", sample_png()) for i in range(3)]
    result = await ad.send_multiple_images(CREATOR, [(f"file://{quote(p)}", "") for p in paths])
    assert result.success
    assert [s.type for s in meta.sent_media] == ["image", "image", "image"] and len(meta.uploads) == 3


@pytest.mark.asyncio
async def test_switch_off_file_sends_fail_final_and_url_images_go_as_links(ad, meta, tmp_path):
    ad._media_enabled = False
    png, pdf = put(tmp_path, "a.png", sample_png()), put(tmp_path, "a.pdf", sample_pdf())
    results = [
        await ad.send_image_file(CREATOR, png, caption="c"),
        await ad.send_video(CREATOR, put(tmp_path, "a.mp4", sample_mp4())),
        await ad.send_voice(CREATOR, put(tmp_path, "a.mp3", sample_mp3()), is_voice=True),
        await ad.send_document(CREATOR, pdf, caption="c", file_name="r.pdf"),
    ]
    for result in results:  # never a notice reported as success (the send_message tool trusted that)
        assert not result.success and result.error == mod.MEDIA_OFF_ERROR
        assert result.retryable is False and result.raw_response["final"]
        assert "forbidden" not in result.error and "flood" not in result.error
    assert meta.calls("messages") == [] and meta.calls("media_upload") == []
    link = await ad.send_image(CREATOR, "https://example.com/a.png", caption="alt")
    assert link.success and meta.uploads == []  # the link as text is real content, as in v0.1.x
    assert [b["text"]["body"] for b in meta.bodies("messages")] == ["alt\nhttps://example.com/a.png"]


@pytest.mark.asyncio
async def test_switch_off_hermes_callers_report_the_failure(ad, meta, tmp_path):
    """The send_message tool reports the file as not sent; the reply path records failures and posts its own
    "couldn't deliver" notice for the non-image file (one text message, no upload)."""
    send_message_tool = pytest.importorskip("tools.send_message_tool")
    from gateway.config import Platform
    from gateway.platforms.event import MessageEvent
    from gateway.session import SessionSource

    ad._media_enabled = False
    pdf, png = put(tmp_path, "report.pdf", sample_pdf()), put(tmp_path, "chart.png", sample_png())
    tool = await send_message_tool._send_live_adapter_media(ad, CREATOR, "here", [(pdf, False)])
    assert "error" in tool and not tool.get("success") and "media_delivered" not in tool
    assert meta.calls("messages") == []

    event = MessageEvent(text="", source=SessionSource(platform=Platform(mod.PLATFORM_NAME), chat_id=CREATOR))
    recorded = []
    await ad._deliver_media_attachments(
        event,
        [(png, False), (pdf, False)],
        [],
        force_document_attachments=False,
        human_delay=0.0,
        metadata={},
        record_delivery=recorded.append,
    )
    assert len(recorded) == 2 and not any(r.success for r in recorded)
    assert message_types(meta) == ["text"] and meta.uploads == []  # Hermes's notice for the PDF only


@pytest.mark.asyncio
async def test_sent_media_is_recorded_for_quotes(ad, meta, tmp_path):
    from gateway import rich_sent_store

    path = put(tmp_path, "a.webp", sample_webp())
    result = await ad.send_image_file(CREATOR, path)
    assert result.success
    assert rich_sent_store.lookup_media(CREATOR, result.message_id) == [(os.path.realpath(path), "image/webp")]


# ------------------------------------------------------------------ shared coroutine without the adapter
@pytest.mark.asyncio
async def test_send_media_file_works_standalone(meta, tmp_path):
    client = meta.client(unthrottled=True)
    try:
        outcome = await media_outbound.send_media_file(
            client, CREATOR, put(tmp_path, "a.mp3", sample_mp3()), requested="audio", caption="words"
        )
    finally:
        await client.aclose()
    assert outcome.success and outcome.kind == "audio" and outcome.uploads == 1
    assert [b["type"] for b in meta.bodies("messages")] == ["text", "audio"]


@pytest.mark.asyncio
async def test_pacing_notice_is_skipped_when_the_messages_budget_is_the_wait(ad, meta, tmp_path):
    ad._media_pacing_after, ad._media_wait_bound = 0.05, 5.0
    ad._client.limits["messages"] = window = RateWindow(1, window=0.2)
    window.try_acquire()
    started = time.monotonic()
    result = await ad.send_image_file(CREATOR, put(tmp_path, "a.png", sample_png()))
    assert result.success and message_types(meta) == ["image"]
    assert time.monotonic() - started >= 0.1


# ------------------------------------------------------------------ Meta refuses the recipient after the upload
@pytest.mark.asyncio
async def test_media_message_403_after_upload_is_forbidden_final_and_not_retried(ad, meta, tmp_path):
    meta.queue("messages", error_response(403, 131005))
    result = await ad.send_image_file(CREATOR, put(tmp_path, "a.png", sample_png()), caption="c")
    assert not result.success and result.error.startswith("forbidden") and result.error_kind == "forbidden"
    assert result.raw_response["final"]
    assert len(meta.calls("media_upload")) == 1 and len(meta.calls("messages")) == 1


@pytest.mark.asyncio
async def test_known_non_creator_is_refused_in_the_media_precheck(ad, meta, tmp_path, monkeypatch):
    fetch = AsyncMock()
    monkeypatch.setattr(hermes_compat, "cache_image_from_url", fetch)
    ad._non_creators.add(CREATOR)  # Meta answered 403 for this id before, even though it is the pinned creator
    results = [
        await ad.send_document(CREATOR, put(tmp_path, "a.pdf", sample_pdf())),
        await ad.send_image(CREATOR, "https://example.com/a.png"),
    ]
    assert all(not r.success and r.error.startswith("forbidden") for r in results)
    fetch.assert_not_awaited()
    assert meta.calls("media_upload") == [] and meta.calls("messages") == []


# ------------------------------------------------------------------ send_image(url): when the link may be sent
URL = "https://example.com/pics/a.png"


def fetched(monkeypatch, path: str) -> AsyncMock:
    fetch = AsyncMock(return_value=path)
    monkeypatch.setattr(hermes_compat, "cache_image_from_url", fetch)
    return fetch


def link_messages(meta) -> list[dict]:
    return [b for b in meta.bodies("messages") if b["type"] == "text" and URL in b["text"]["body"]]


def full_messages_window(ad) -> None:
    ad._media_wait_bound = 1.0
    ad._client.limits["messages"] = window = RateWindow(1, window=60)
    window.try_acquire()


@pytest.mark.parametrize(
    ("case", "expect_error"),
    [
        ("forbidden", "forbidden"),  # a 403 after the upload: the link would be refused the same way
        ("ambiguous", "outcome unknown"),  # the image may have arrived: a link could duplicate it
        ("budget", "flood_control:"),  # Hermes retries a flood-controlled send; a link now would duplicate
        ("caption_text", "outcome unknown"),  # the caption text itself failed: nothing more is sent
    ],
)
@pytest.mark.asyncio
async def test_send_image_url_never_falls_back_when_the_image_may_arrive_or_would_be_refused(
    ad, meta, tmp_path, monkeypatch, case, expect_error
):
    fetched(monkeypatch, put(tmp_path, "cache/img_1.jpg", sample_png()))
    caption = "c"
    if case == "forbidden":
        meta.queue("messages", error_response(403, 131005))
    elif case == "ambiguous":
        meta.queue("messages", error_response(500, 2))
    elif case == "budget":
        full_messages_window(ad)
    else:
        caption = "x" * 1100  # over 1024: goes first as its own text, which then fails
        meta.queue("messages", error_response(500, 2))
    result = await ad.send_image(CREATOR, URL, caption=caption)
    assert not result.success and result.error.startswith(expect_error)
    assert link_messages(meta) == []
    if case == "caption_text":
        assert meta.sent_media == [] and message_types(meta) == ["text"]
    elif case == "budget":
        assert meta.calls("messages") == [] and meta.calls("media_upload") == []
    else:
        assert message_types(meta) == ["image"]  # exactly one media POST, no retry


@pytest.mark.parametrize("stage", ["upload", "message", "media_gone"])
@pytest.mark.asyncio
async def test_send_image_url_falls_back_to_the_link_when_meta_rejects_the_image(
    ad, meta, tmp_path, monkeypatch, stage
):
    fetched(monkeypatch, put(tmp_path, "cache/img_1.jpg", sample_png()))
    if stage == "upload":
        meta.queue("media_upload", unsupported_mime_response("image/png"))
    elif stage == "message":
        meta.queue("messages", media_type_mismatch_response())
    else:
        meta.queue("messages", no_media_found_send_response("a"), no_media_found_send_response("b"))
    result = await ad.send_image(CREATOR, URL, caption="alt", reply_to="wamid.in1")
    assert result.success
    [link] = link_messages(meta)
    assert link["text"]["body"] == f"alt\n{URL}" and link["context"] == {"message_id": "wamid.in1"}
    assert meta.sent_media == []


@pytest.mark.asyncio
async def test_send_image_url_fallback_after_the_caption_went_as_text_does_not_repeat_it(
    ad, meta, tmp_path, monkeypatch
):
    fetched(monkeypatch, put(tmp_path, "cache/img_1.jpg", sample_png()))
    meta.queue("messages", meta._messages, media_type_mismatch_response())  # caption text OK, image rejected
    caption = "y" * 1100
    result = await ad.send_image(CREATOR, URL, caption=caption, reply_to="wamid.in1")
    assert result.success
    bodies = meta.bodies("messages")
    assert [b["type"] for b in bodies] == ["text", "image", "text"]
    assert bodies[0]["text"]["body"] == caption and bodies[0]["context"] == {"message_id": "wamid.in1"}
    assert bodies[2]["text"]["body"] == URL and "context" not in bodies[2]  # the link alone, once


@pytest.mark.parametrize("kind", ["empty", "missing"])
@pytest.mark.asyncio
async def test_send_image_url_falls_back_when_the_fetched_file_cannot_be_prepared(
    ad, meta, tmp_path, monkeypatch, kind
):
    path = put(tmp_path, "cache/img_1.jpg", b"") if kind == "empty" else str(tmp_path / "cache" / "gone.jpg")
    fetched(monkeypatch, path)
    result = await ad.send_image(CREATOR, URL, caption="alt")
    assert result.success and meta.uploads == []
    assert [b["text"]["body"] for b in meta.bodies("messages")] == [f"alt\n{URL}"]


@pytest.mark.asyncio
async def test_send_image_url_fetch_is_bounded_in_time(ad, meta, monkeypatch):
    import asyncio

    async def hang(url):
        await asyncio.sleep(30)

    monkeypatch.setattr(hermes_compat, "cache_image_from_url", hang)
    ad._media_url_timeout = 0.05
    started = time.monotonic()
    result = await ad.send_image(CREATOR, URL)
    assert time.monotonic() - started < 5
    assert result.success and meta.uploads == [] and [b["text"]["body"] for b in meta.bodies("messages")] == [URL]


@pytest.mark.asyncio
async def test_send_image_url_over_16_mib_is_dropped_and_sent_as_a_link(ad, meta, tmp_path, monkeypatch):
    path = tmp_path / "cache" / "img_big.jpg"
    path.parent.mkdir()
    with open(path, "wb") as fh:
        fh.write(sample_jpeg())
        fh.truncate(mod.URL_IMAGE_MAX_BYTES + 1)
    fetched(monkeypatch, str(path))
    result = await ad.send_image(CREATOR, URL)
    assert result.success and meta.uploads == [] and not path.exists()
    assert [b["text"]["body"] for b in meta.bodies("messages")] == [URL]


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:1/x.png", "http://10.0.0.1/x.png", "http://169.254.169.254/latest/meta.png"]
)
@pytest.mark.asyncio
async def test_send_image_url_real_ssrf_guard_refuses_internal_hosts(ad, meta, monkeypatch, url):
    """Hermes's own ``cache_image_from_url`` (not patched) refuses the URL; the link goes as text instead."""
    import httpx

    real_send = httpx.AsyncClient.send
    reached = []

    async def guarded_send(self, request, *args, **kwargs):
        if request.url.host in ("127.0.0.1", "10.0.0.1", "169.254.169.254"):
            reached.append(str(request.url))
            raise AssertionError("an internal URL was requested")
        return await real_send(self, request, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "send", guarded_send)
    assert hermes_compat.cache_image_from_url.__module__ == "gateway.platforms.base"
    result = await ad.send_image(CREATOR, url)
    assert result.success and reached == [] and meta.uploads == []
    assert [b["text"]["body"] for b in meta.bodies("messages")] == [url]
    assert {h.host for h in meta.host_log} == {"api.whatsapp.com"}


# ------------------------------------------------------------------ batches
@pytest.mark.asyncio
async def test_send_multiple_images_with_one_failure_is_still_a_success(ad, meta, tmp_path):
    paths = [put(tmp_path, "a.png", sample_png()), str(tmp_path / "gone.png"), put(tmp_path, "c.png", sample_png())]
    result = await ad.send_multiple_images(CREATOR, [(f"file://{quote(p)}", "") for p in paths])
    assert result.success  # Hermes's rule: at least one image delivered
    assert [s.type for s in meta.sent_media] == ["image", "image"] and len(meta.uploads) == 2
    assert message_types(meta) == ["image", "image"]  # Hermes's image batch posts no failure notice


# ------------------------------------------------------------------ pacing notice wording
@pytest.mark.asyncio
async def test_pacing_notice_says_more_files_only_when_more_are_waiting(ad, meta, tmp_path):
    import asyncio

    ad._media_pacing_after, ad._media_wait_bound, ad._media_pacing_interval = 0.05, 5.0, 0.0
    ad._client.limits["media_upload"] = window = RateWindow(1, window=0.2)
    window.try_acquire()
    assert (await ad.send_image_file(CREATOR, put(tmp_path, "one.png", sample_png()))).success
    notices = [b["text"]["body"] for b in meta.bodies("messages") if b["type"] == "text"]
    assert len(notices) == 1 and "More files" not in notices[0] and "A file is on its way" in notices[0]

    await asyncio.sleep(0.25)
    window.try_acquire()
    paths = [put(tmp_path, f"{i}.png", sample_png()) for i in range(2)]
    results = await asyncio.gather(*(ad.send_image_file(CREATOR, p) for p in paths))
    assert all(r.success for r in results)
    notices = [b["text"]["body"] for b in meta.bodies("messages") if b["type"] == "text"]
    assert any("More files coming" in n for n in notices[1:])


# ------------------------------------------------------------------ stale quote named only in Meta's message
def stale_quote_message_only_response(message: str = "context.message_id"):
    """Hypothetical shape (not yet seen live): code 100 naming the field in ``message`` with no ``error_data``,
    the way Meta answered the live 1025-unit caption (``message: "caption"``). P6 records the real one."""
    from wap_helpers import meta_error

    return meta_error(400, 100, message)


@pytest.mark.parametrize("message", ["context.message_id", "context", "(#100) Param context[message_id] is invalid"])
@pytest.mark.asyncio
async def test_stale_quote_named_only_in_the_message_is_resent_without_it(ad, meta, tmp_path, message):
    meta.queue("messages", stale_quote_message_only_response(message))
    result = await ad.send_image_file(CREATOR, put(tmp_path, "a.png", sample_png()), reply_to="wamid.old")
    assert result.success
    first, second = meta.bodies("messages")
    assert first["context"] == {"message_id": "wamid.old"} and "context" not in second
    assert first["image"]["id"] == second["image"]["id"] and len(meta.uploads) == 1


def test_is_stale_media_quote_is_conservative():
    from wap_plugin_under_test.client import MediaRejected, NotSent

    def rejected(message=None, details=None, status=400):
        return MediaRejected("send: rejected", status=status, code=100, details=details, error_message=message)

    assert media_outbound.is_stale_media_quote(rejected("context.message_id"))
    assert media_outbound.is_stale_media_quote(rejected(details="context.message_id is not a wamid from this chat"))
    assert media_outbound.is_stale_media_quote(rejected("message_id"))
    for message, details in [
        ("caption", None),  # live shape for a long caption
        ("(#100) Invalid parameter", None),
        ("(#131009) Missing or malformed required fields", "No media found for id wamid-like-id"),
        ("(#100) No media found for the provided id", "No media found for this id."),
        ("Invalid value in this context, try again", None),
        (None, None),
    ]:
        assert not media_outbound.is_stale_media_quote(rejected(message, details)), (message, details)
    assert not media_outbound.is_stale_media_quote(rejected("context", status=500))
    assert not media_outbound.is_stale_media_quote(NotSent("context.message_id", status=400, code=100))


# ------------------------------------------------------------------ outbound log hygiene
@pytest.mark.asyncio
async def test_outbound_logs_and_errors_never_carry_paths_names_captions_ids_or_urls(
    ad, meta, tmp_path, monkeypatch, caplog
):
    import logging

    secret_dir = tmp_path / "zzHOSTDIRzz"
    name = "zzFILENAMEzz"
    caption = "zzCAPTIONzz " * 3
    url = "https://example.com/zzURLzz.png"
    caplog.set_level(logging.DEBUG)
    results = []

    async def run(coro):
        results.append(await coro)

    png = put(secret_dir, f"{name}.png", sample_png())
    await run(ad.send_image_file(CREATOR, png, caption=caption))  # success
    await run(ad.send_document(CREATOR, str(secret_dir / f"{name}-missing.pdf"), caption=caption))
    await run(ad.send_document(CREATOR, put(secret_dir, ".env", b"K=1\n")))
    await run(ad.send_document(CREATOR, put(secret_dir, f"{name}-empty.pdf", b"")))
    big = secret_dir / f"{name}-big.zip"
    with open(big, "wb") as fh:
        fh.truncate(16 * MIB + 1)
    await run(ad.send_document(CREATOR, str(big)))
    meta.queue("media_upload", unsupported_mime_response("image/png"))
    await run(ad.send_image_file(CREATOR, png, caption=caption))
    meta.queue("messages", no_media_found_send_response("zzMEDIAIDzz"), no_media_found_send_response("zzMEDIAIDzz"))
    await run(ad.send_image_file(CREATOR, png, caption=caption))
    await run(ad.send_image_file(CREATOR, png, caption=caption + "x" * 1100))  # caption overflow, then the file
    meta.queue("messages", error_response(403, 131005))
    await run(ad.send_document(CREATOR, put(secret_dir, f"{name}.pdf", sample_pdf()), caption=caption))
    meta.queue("messages", error_response(500, 2))
    await run(ad.send_image_file(CREATOR, png, caption=caption))
    monkeypatch.setattr(hermes_compat, "cache_image_from_url", AsyncMock(side_effect=ValueError(f"unsafe {url}")))
    await run(ad.send_image(CREATOR, url, caption=caption))
    monkeypatch.setattr(hermes_compat, "cache_image_from_url", AsyncMock(return_value=png))
    meta.queue("messages", media_type_mismatch_response())
    await run(ad.send_image(CREATOR, url, caption=caption))
    ad._non_creators.add(CREATOR)
    await run(ad.send_document(CREATOR, png, caption=caption))

    assert any(not r.success for r in results) and any(r.success for r in results)
    secrets = [str(tmp_path), "zzHOSTDIRzz", name, "zzCAPTIONzz", "zzURLzz", "zzMEDIAIDzz", "example.com"]
    secrets += [u.media_id for u in meta.uploads]
    plugin_logs = [r.getMessage() for r in caplog.records if r.name.startswith("wap_plugin_under_test")]
    assert plugin_logs  # the paths above do log
    for text in plugin_logs + [r.error or "" for r in results]:
        for secret in secrets:
            assert secret not in text, (secret, text)


# ------------------------------------------------------------------ plugin-wide rule (09 M1)
def test_no_plugin_module_uses_cache_media_bytes():
    """``CachedMedia.path`` changed meaning on Hermes main, so no plugin module may reference the helper
    (docstrings may mention it)."""
    root = Path(mod.__file__).parent
    banned = {"cache_media_bytes", "cache_media_bytes_async"}
    modules = sorted(root.glob("*.py"))
    assert {"adapter.py", "hermes_compat.py", "media_outbound.py"} <= {m.name for m in modules}
    for module in modules:
        tree = ast.parse(module.read_text(encoding="utf-8"))
        used = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                used.add(node.id)
            elif isinstance(node, ast.Attribute):
                used.add(node.attr)
            elif isinstance(node, ast.alias):
                used.update({node.name.rsplit(".", 1)[-1], node.asname or ""})
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.strip() in banned:
                used.add(node.value.strip())  # getattr(base, "cache_media_bytes")
        assert not used & banned, module.name
