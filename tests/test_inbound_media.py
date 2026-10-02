"""Inbound media end to end: fake Meta update -> adapter -> Hermes's real cache helpers -> ``handle_message``."""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from gateway.platforms.event import MessageType
from wap_helpers import (
    API_KEY,
    CREATOR,
    EVIL_HOST,
    LOOKASIDE_HOST,
    error_response,
    media_message,
    media_not_found_response,
    rate_limited_response,
    sample_jpeg,
    sample_mp3,
    sample_mp4,
    sample_ogg_opus,
    sample_pdf,
    sample_png,
    sample_webp,
    sha256_b64,
    text_message,
    until,
    updates_response,
)
from wap_plugin_under_test import adapter as mod
from wap_plugin_under_test import media_inbound
from wap_plugin_under_test.state import PollState, state_path

OTHER = "user:777"
HEIC = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 64


def make_adapter(config=None):
    from gateway.config import PlatformConfig

    a = mod.WhatsAppAgentPlatformAdapter(config or PlatformConfig(enabled=True))
    a._poll_min_interval = 0.01
    a._poll_timeout = 0
    a._backoff_base = 0.01
    a._conflict_backoff = 0.01
    a._media_retry_wait = 0  # don't sleep between download attempts
    a.handle_message = AsyncMock()
    a._notify_fatal_error = AsyncMock()
    return a


@pytest_asyncio.fixture
async def connected(meta, api_key):
    a = make_adapter()
    assert await a.connect() is True
    yield a
    await a.disconnect()


def dispatched(a) -> list:
    return [call.args[0] for call in a.handle_message.await_args_list]


def saved_state() -> PollState:
    from hermes_constants import get_hermes_home

    return PollState.load(state_path(get_hermes_home(), API_KEY))


async def deliver(a, meta, *messages, offset: int):
    """Queue one page and wait until the adapter saved its offset (the whole page was handled)."""
    meta.queue("updates", updates_response(*messages, next_offset=offset))
    await until(lambda: saved_state().next_offset == offset, timeout=10)
    return dispatched(a)


def only_event(a):
    events = dispatched(a)
    assert len(events) == 1
    return events[0]


def assert_aligned(event) -> None:
    assert len(event.media_urls) == len(event.media_types) == len(event.media_text_inlined)


def assert_file(path: str, data: bytes, cache_dir: str, hermes_home: Path) -> None:
    p = Path(path)
    assert p.is_file() and p.read_bytes() == data
    assert p.parent == hermes_home / "cache" / cache_dir


def cache_files(hermes_home: Path) -> list[Path]:
    """Every file in Hermes's media cache directories."""
    dirs = [hermes_home / "cache" / name for name in ("images", "audio", "videos", "documents")]
    return [f for d in dirs if d.is_dir() for f in d.iterdir()]


def hermes_routing():
    """Hermes's own per-attachment routing (``gateway/run.py``, ``gateway/run_inbound.py``)."""
    import gateway.run as run
    from gateway.run_inbound import GatewayInboundMixin

    for name in ("_event_media_is_image", "_event_media_is_stt_input"):
        if not hasattr(run, name):
            pytest.skip(f"this Hermes has no gateway.run.{name}")
    for name in ("_classify_inbound_media", "_prepend_inbound_document_notes"):
        if not hasattr(GatewayInboundMixin, name):
            pytest.skip(f"this Hermes has no GatewayInboundMixin.{name}")
    return run, GatewayInboundMixin


def assert_routed_as_document(event) -> None:
    """Hermes gives the file a document note and never sends it to vision."""
    run, mixin = hermes_routing()
    assert not run._event_media_is_image(event, 0)
    image_paths, *_ = mixin._classify_inbound_media(event, False)
    assert image_paths == []
    note = mixin._prepend_inbound_document_notes(event, event.text)
    assert note.startswith("[The user sent a document: ")


def assert_note_only(event, caption: str | None = None) -> None:
    """A failed attachment: dispatched as text with the caption and a bracketed note, no media."""
    assert event.media_urls == [] and event.media_types == [] and event.media_text_inlined == []
    assert event.message_type == MessageType.TEXT
    assert event.text.rstrip().endswith("]") and "[" in event.text
    if caption:
        assert event.text.startswith(caption)


# ---------------------------------------------------------------- every live inbound shape
@pytest.mark.asyncio
async def test_image_with_caption(connected, meta, hermes_home):
    data = sample_jpeg()
    await deliver(connected, meta, meta.inbound_image("wamid.i1", data, caption="what is this?"), offset=2)
    event = only_event(connected)
    assert event.message_type == MessageType.PHOTO and event.text == "what is this?"
    assert event.media_types == ["image/jpeg"] and event.media_text_inlined == [False]
    assert_file(event.media_urls[0], data, "images", hermes_home)
    assert saved_state().seen("wamid.i1")


@pytest.mark.asyncio
async def test_image_without_caption_has_empty_text(connected, meta, hermes_home):
    await deliver(connected, meta, meta.inbound_image("wamid.i1"), offset=2)
    event = only_event(connected)
    assert event.message_type == MessageType.PHOTO and event.text == ""
    assert len(event.media_urls) == 1


@pytest.mark.asyncio
async def test_voice_note_is_voice_with_params_stripped(connected, meta, hermes_home):
    data = sample_ogg_opus()
    await deliver(connected, meta, meta.inbound_voice("wamid.v1", data), offset=2)
    event = only_event(connected)
    assert event.message_type == MessageType.VOICE
    assert event.media_types == ["audio/ogg"]
    assert event.media_urls[0].endswith(".ogg")
    assert_file(event.media_urls[0], data, "audio", hermes_home)


@pytest.mark.asyncio
async def test_audio_file_is_audio_not_voice(connected, meta, hermes_home):
    data = sample_mp3()
    await deliver(connected, meta, meta.inbound_audio("wamid.a1", data), offset=2)
    event = only_event(connected)
    assert event.message_type == MessageType.AUDIO and event.media_types == ["audio/mpeg"]
    assert event.media_urls[0].endswith(".mp3")
    assert_file(event.media_urls[0], data, "audio", hermes_home)


@pytest.mark.asyncio
async def test_video(connected, meta, hermes_home):
    data = sample_mp4()
    await deliver(connected, meta, meta.inbound_video("wamid.m1", data, caption="clip"), offset=2)
    event = only_event(connected)
    assert event.message_type == MessageType.VIDEO and event.text == "clip"
    assert event.media_types == ["video/mp4"]
    assert_file(event.media_urls[0], data, "videos", hermes_home)


@pytest.mark.asyncio
async def test_pdf_document(connected, meta, hermes_home):
    data = sample_pdf()
    await deliver(connected, meta, meta.inbound_document("wamid.d1", data, caption="summarise"), offset=2)
    event = only_event(connected)
    assert event.message_type == MessageType.DOCUMENT and event.text == "summarise"
    assert event.media_types == ["application/pdf"] and event.media_text_inlined == [False]
    assert_file(event.media_urls[0], data, "documents", hermes_home)
    assert Path(event.media_urls[0]).name.endswith("_report.pdf")


@pytest.mark.asyncio
async def test_photo_sent_as_document_becomes_photo(connected, meta, hermes_home):
    data = sample_png()
    await deliver(connected, meta, meta.inbound_photo_document("wamid.d1", data), offset=2)
    event = only_event(connected)
    assert event.message_type == MessageType.PHOTO and event.media_types == ["image/png"]
    assert_file(event.media_urls[0], data, "images", hermes_home)


@pytest.mark.asyncio
async def test_sticker_is_a_photo_with_a_note(connected, meta, hermes_home):
    data = sample_webp()
    await deliver(connected, meta, meta.inbound_sticker("wamid.s1", data), offset=2)
    event = only_event(connected)
    assert event.message_type == MessageType.PHOTO and event.media_types == ["image/webp"]
    assert event.text == mod.STICKER_NOTE
    assert event.media_urls[0].endswith(".webp")
    assert_file(event.media_urls[0], data, "images", hermes_home)


@pytest.mark.asyncio
async def test_heic_document_refused_by_the_image_cache_stays_a_document(connected, meta, hermes_home):
    msg = meta.inbound_document("wamid.h1", HEIC, mime_type="image/heic", filename="IMG_1.HEIC")
    await deliver(connected, meta, msg, offset=2)
    event = only_event(connected)
    # An image/* MIME would make Hermes treat it as an image (vision, no document note); vision can't read it.
    assert event.message_type == MessageType.DOCUMENT and event.media_types == ["application/octet-stream"]
    assert_file(event.media_urls[0], HEIC, "documents", hermes_home)
    assert Path(event.media_urls[0]).name.endswith("_IMG_1.HEIC")
    assert_routed_as_document(event)


@pytest.mark.asyncio
async def test_heic_image_message_falls_back_to_a_document(connected, meta, hermes_home):
    await deliver(connected, meta, meta.inbound_image("wamid.h1", HEIC, mime_type="image/heic"), offset=2)
    event = only_event(connected)
    assert event.message_type == MessageType.DOCUMENT and event.media_types == ["application/octet-stream"]
    assert Path(event.media_urls[0]).name.endswith("_image.heic")  # the name keeps the real type
    assert_routed_as_document(event)


@pytest.mark.asyncio
async def test_corrupt_image_sent_as_a_document_is_routed_as_a_document(connected, meta, hermes_home):
    html = b"<html><body>not a png</body></html>"
    msg = meta.inbound_document("wamid.d1", html, mime_type="image/png", filename="chart.png", caption="see")
    await deliver(connected, meta, msg, offset=2)
    event = only_event(connected)
    assert event.message_type == MessageType.DOCUMENT and event.media_types == ["application/octet-stream"]
    assert event.text == "see"
    assert_file(event.media_urls[0], html, "documents", hermes_home)
    assert_routed_as_document(event)


@pytest.mark.asyncio
async def test_image_that_is_not_an_image_gets_a_note(connected, meta, hermes_home):
    html = b"<html><body>error</body></html>"
    msg = meta.inbound_image("wamid.i1", html, caption="look")
    await deliver(connected, meta, msg, text_message("wamid.t1", "after"), offset=2)
    first, second = dispatched(connected)
    assert_note_only(first, "look")
    assert second.text == "after"
    assert not (hermes_home / "cache" / "images").exists() or not any((hermes_home / "cache" / "images").iterdir())


@pytest.mark.asyncio
async def test_document_without_extension_gets_the_mime_extension(connected, meta):
    await deliver(connected, meta, meta.inbound_document("wamid.d1", filename=None), offset=2)
    event = only_event(connected)
    assert Path(event.media_urls[0]).name.endswith("_document.pdf")


@pytest.mark.asyncio
async def test_hostile_filename_stays_inside_the_document_cache(connected, meta, hermes_home):
    msg = meta.inbound_document("wamid.d1", b"%PDF-1.4 x", filename="..\\../CON.pdf:evil\u202e")
    await deliver(connected, meta, msg, offset=2)
    path = Path(only_event(connected).media_urls[0])
    assert path.parent == hermes_home / "cache" / "documents"
    assert ":" not in path.name and "\u202e" not in path.name and ".." not in path.name


# ---------------------------------------------------------------- text inlining
@pytest.mark.asyncio
async def test_small_text_document_is_inlined(connected, meta):
    data = "\ufeffline one\nline two\n".encode()
    msg = meta.inbound_document("wamid.d1", data, mime_type="text/plain", filename="notes.txt", caption="read it")
    await deliver(connected, meta, msg, offset=2)
    event = only_event(connected)
    assert event.message_type == MessageType.DOCUMENT and event.media_text_inlined == [True]
    assert event.text.startswith("[Content of notes.txt]:\nline one\nline two")
    assert event.text.endswith("\n\nread it")
    assert event.media_types == ["text/plain"]


@pytest.mark.parametrize(
    ("data", "mime", "filename"),
    [
        (b"x" * (100 * 1024 + 1), "text/plain", "big.txt"),  # over 100 KiB
        (b"\xff\xfe\x00bad utf-8", "text/plain", "bad.txt"),  # not strict UTF-8
        (b"plain words", "application/pdf", "report.pdf"),  # neither a text extension nor text/*
        (b"a,b\n1,2\n", "application/octet-stream", "data.bin"),
    ],
    ids=["over-100-KiB", "not-utf-8", "pdf-mime", "binary-extension"],
)
@pytest.mark.asyncio
async def test_documents_that_are_not_inlined(connected, meta, data, mime, filename):
    await deliver(connected, meta, meta.inbound_document("wamid.d1", data, mime_type=mime, filename=filename), offset=2)
    event = only_event(connected)
    assert event.media_text_inlined == [False] and event.text == ""
    assert Path(event.media_urls[0]).read_bytes() == data


@pytest.mark.parametrize(
    ("filename", "mime", "data"),
    [
        ("data.json", "application/json", b'{"a": 1}'),
        ("feed.xml", "application/xml", b"<a>1</a>"),
        ("tool.py", "application/octet-stream", b"print('hi')\n"),
        ("notes.csv", "text/csv", b"a,b\n1,2\n"),  # already text/*: kept
    ],
)
@pytest.mark.asyncio
async def test_inlined_text_files_are_routed_as_text(connected, meta, filename, mime, data):
    await deliver(connected, meta, meta.inbound_document("wamid.d1", data, mime_type=mime, filename=filename), offset=2)
    event = only_event(connected)
    assert event.media_text_inlined == [True]
    assert event.media_types == [mime if mime.startswith("text/") else "text/plain"]
    _, mixin = hermes_routing()
    prompt = mixin._prepend_inbound_document_notes(event, event.text)
    assert "Its content has been included below" in prompt
    assert "binary format" not in prompt
    assert prompt.endswith(f"[Content of {filename}]:\n{data.decode()}")


@pytest.mark.asyncio
async def test_markdown_by_extension_is_inlined(connected, meta):
    msg = meta.inbound_document("wamid.d1", b"# Title\n", mime_type="application/octet-stream", filename="a.md")
    await deliver(connected, meta, msg, offset=2)
    event = only_event(connected)
    assert event.media_text_inlined == [True] and event.text == "[Content of a.md]:\n# Title\n"


# ---------------------------------------------------------------- authorization and dedup
@pytest.mark.asyncio
async def test_unauthorised_sender_never_triggers_a_download(meta, api_key, hermes_home):
    a = make_adapter()
    assert await a.connect()
    msg = meta.inbound_image("wamid.x1", sender=OTHER, caption="hi")
    meta.not_creator_wamids.add("wamid.x1")
    await deliver(a, meta, msg, offset=2)
    await a.disconnect()
    assert a.handle_message.await_count == 0
    assert meta.calls("media_get") == [] and meta.calls("download") == []
    assert [b["message_id"] for b in meta.bodies("statuses")] == ["wamid.x1"]  # only the creator probe
    assert saved_state().seen("wamid.x1")
    assert not (hermes_home / "cache" / "images").exists() or not any((hermes_home / "cache" / "images").iterdir())


@pytest.mark.asyncio
async def test_backlog_media_is_never_downloaded(connected, meta):
    old = meta.inbound_image("wamid.old", timestamp=int(time.time()) - 3600)
    await deliver(connected, meta, old, text_message("wamid.t1", "new"), offset=3)
    assert [e.text for e in dispatched(connected)] == ["new"]
    assert meta.calls("media_get") == [] and meta.calls("download") == []


@pytest.mark.asyncio
async def test_seen_media_is_never_downloaded_again(connected, meta):
    msg = meta.inbound_image("wamid.i1")
    await deliver(connected, meta, msg, offset=2)
    await deliver(connected, meta, msg, text_message("wamid.t1", "next"), offset=3)
    assert [e.message_id for e in dispatched(connected)] == ["wamid.i1", "wamid.t1"]
    assert len(meta.calls("media_get")) == 1 and len(meta.calls("download")) == 1


# ---------------------------------------------------------------- failures never reach the poll loop
DOWNLOAD_FAULTS = [
    ("not_found", {}),
    ("redirect", {}),
    ("bad_bytes", {}),
    ("other_host", {}),
    ("wrong_length", {}),
    ("oversize", {}),
    ("no_length", {"extra_bytes": 0}),  # no Content-Length is fine: delivered (checked below)
    ("status", {"status": 500}),
    ("status", {"status": 401}),
    ("status", {"status": 403}),
    ("status", {"status": 429, "retry_after": 0}),
    ("timeout", {}),
    ("timeout_mid_stream", {}),
]


@pytest.mark.parametrize(("mode", "options"), DOWNLOAD_FAULTS)
@pytest.mark.asyncio
async def test_download_faults_dispatch_a_note_and_polling_continues(connected, meta, mode, options):
    msg = meta.inbound_image("wamid.i1", caption="cap")
    meta.fault_download(msg["image"]["id"], mode, **options)
    await deliver(connected, meta, msg, text_message("wamid.t1", "after"), offset=2)
    await deliver(connected, meta, text_message("wamid.t2", "still polling"), offset=3)
    first, *rest = dispatched(connected)
    assert [e.text for e in rest] == ["after", "still polling"]
    assert not connected.has_fatal_error
    assert EVIL_HOST not in connected._client.media_hosts
    assert all(entry.host in ("api.whatsapp.com", LOOKASIDE_HOST) for entry in meta.host_log if entry.authorized)
    if mode == "no_length":
        assert first.message_type == MessageType.PHOTO and len(first.media_urls) == 1
    else:
        assert_note_only(first, "cap")
    assert len(meta.calls("download")) <= 2  # at most two attempts


@pytest.mark.parametrize("metadata_digest", [True, False], ids=["matching-metadata", "no-metadata-digest"])
@pytest.mark.asyncio
async def test_update_digest_mismatch_never_reaches_the_media_cache(connected, meta, hermes_home, metadata_digest):
    """Valid media bytes must also match the digest in the inbound update, not just GET /media."""
    msg = meta.inbound_image("wamid.i1", caption="look", sha256=sha256_b64(b"different original attachment"))
    media_id = msg["image"]["id"]
    if not metadata_digest:
        meta.queue(
            "media_get",
            httpx.Response(
                200,
                json={
                    "id": media_id,
                    "url": meta.media_url(media_id),
                    "mime_type": "image/jpeg",
                    "file_size": len(sample_jpeg()),
                },
            ),
        )

    await deliver(connected, meta, msg, text_message("wamid.t1", "after"), offset=2)
    failed, following = dispatched(connected)
    assert_note_only(failed, "look")
    assert media_inbound.FAILED in failed.text
    assert cache_files(hermes_home) == []
    assert following.text == "after"
    assert saved_state().seen("wamid.i1")
    assert not connected.has_fatal_error
    assert len(meta.calls("media_get")) == 1


@pytest.mark.asyncio
async def test_slow_download_hits_the_overall_deadline(connected, meta):
    connected._media_timeout = 0.3
    msg = meta.inbound_image("wamid.i1", sample_jpeg() + b"\0" * 400_000)  # several 64 KiB chunks
    meta.fault_download(msg["image"]["id"], "slow", delay=0.2)
    started = time.monotonic()
    await deliver(connected, meta, msg, offset=2)
    assert time.monotonic() - started < 5
    assert_note_only(only_event(connected))


@pytest.mark.parametrize(
    "responses",
    [
        [error_response(409, 1752041)],
        [error_response(401, 190)],
        [error_response(403, 131005)],
        [media_not_found_response(400)],
        [error_response(503, 2), error_response(503, 2)],
        [httpx.ConnectError("down"), httpx.ConnectError("down")],
        [error_response(500, 2), error_response(500, 2)],
    ],
)
@pytest.mark.asyncio
async def test_media_endpoint_errors_are_isolated(connected, meta, responses):
    meta.queue("media_get", *responses)
    msg = meta.inbound_voice("wamid.v1")
    await deliver(connected, meta, msg, offset=2)
    await deliver(connected, meta, text_message("wamid.t1", "next"), offset=3)
    first, second = dispatched(connected)
    assert_note_only(first)
    assert second.text == "next"
    assert not connected.has_fatal_error
    connected._notify_fatal_error.assert_not_awaited()
    assert len(meta.calls("media_get")) <= 2


@pytest.mark.parametrize(
    "transient", [rate_limited_response(0), error_response(503, 2), httpx.ConnectError("blip"), error_response(500, 2)]
)
@pytest.mark.asyncio
async def test_one_transient_media_get_error_is_retried(connected, meta, transient):
    meta.queue("media_get", transient)
    data = sample_jpeg()
    await deliver(connected, meta, meta.inbound_image("wamid.i1", data), offset=2)
    event = only_event(connected)
    assert event.message_type == MessageType.PHOTO and Path(event.media_urls[0]).read_bytes() == data
    assert len(meta.calls("media_get")) == 2


@pytest.mark.asyncio
async def test_os_error_from_the_cache_is_isolated(connected, meta, hermes_home):
    (hermes_home / "cache").mkdir(exist_ok=True)
    (hermes_home / "cache" / "images").write_bytes(b"not a directory")  # the real helper's mkdir fails
    await deliver(connected, meta, meta.inbound_image("wamid.i1", caption="c"), text_message("wamid.t1", "t"), offset=2)
    await deliver(connected, meta, text_message("wamid.t2", "t2"), offset=3)
    first, *rest = dispatched(connected)
    assert_note_only(first, "c")
    assert [e.text for e in rest] == ["t", "t2"]
    assert not connected.has_fatal_error and connected._save_failures == 0


@pytest.mark.asyncio
async def test_oversize_by_declared_size_is_not_downloaded(meta, api_key, hermes_home):
    (hermes_home / "config.yaml").write_text("gateway:\n  max_inbound_media_bytes: 1048576\n", encoding="utf-8")
    a = make_adapter()
    assert await a.connect()
    msg = meta.inbound_oversize_document("wamid.big", caption="my video")
    await deliver(a, meta, msg, offset=2)
    await a.disconnect()
    event = only_event(a)
    assert_note_only(event, "my video")
    assert "22 MB" in event.text and "1 MB" in event.text  # the size is in the note
    assert len(meta.calls("media_get")) == 1 and meta.calls("download") == []


@pytest.mark.asyncio
async def test_malformed_media_object_gets_a_note_without_any_request(connected, meta):
    msg = media_message("wamid.i1", "image", {"id": "../updates", "mime_type": "image/jpeg"})
    await deliver(connected, meta, msg, offset=2)
    assert_note_only(only_event(connected))
    assert meta.calls("media_get") == []


# ---------------------------------------------------------------- state, handoff, quotes
@pytest.mark.asyncio
async def test_dedup_is_saved_after_each_media_message(connected, meta):
    calls = []

    async def handle(event):
        calls.append(event.message_id)
        if event.message_id == "wamid.t1":
            raise RuntimeError("hermes busy")

    connected._backoff_base = 5.0  # keep the page from being retried during the check
    connected.handle_message = AsyncMock(side_effect=handle)
    page = updates_response(meta.inbound_image("wamid.i1"), text_message("wamid.t1", "x"), next_offset=2)
    meta.queue("updates", page)
    await until(lambda: calls == ["wamid.i1", "wamid.t1"])
    await until(lambda: saved_state().seen("wamid.i1"))
    state = saved_state()
    assert state.next_offset == 0 and not state.seen("wamid.t1")


@pytest.mark.asyncio
async def test_handoff_retry_does_not_download_again(connected, meta):
    connected.handle_message = AsyncMock(side_effect=[RuntimeError("busy"), None])
    msg = meta.inbound_image("wamid.i1")
    meta.queue("updates", updates_response(msg, next_offset=2))
    await deliver(connected, meta, msg, offset=2)
    assert connected.handle_message.await_count == 2
    first, second = dispatched(connected)
    assert first.media_urls == second.media_urls
    assert len(meta.calls("media_get")) == 1 and len(meta.calls("download")) == 1


@pytest.mark.asyncio
async def test_quoted_media_is_reattached_and_lists_stay_aligned(connected, meta):
    await deliver(connected, meta, meta.inbound_image("wamid.i1"), offset=2)
    photo = only_event(connected)
    quote = {"from": CREATOR, "id": "wamid.i1"}
    await deliver(connected, meta, text_message("wamid.t1", "what about this?", context=quote), offset=3)
    reply = dispatched(connected)[1]
    assert reply.message_type == MessageType.TEXT
    assert reply.media_urls == photo.media_urls and reply.media_types == ["image/jpeg"]
    assert reply.media_text_inlined == [False]
    doc = meta.inbound_document("wamid.d1", b"hello", mime_type="text/plain", filename="n.txt", context=quote)
    await deliver(connected, meta, doc, offset=4)
    both = dispatched(connected)[2]
    assert_aligned(both)
    assert both.media_types == ["text/plain", "image/jpeg"] and both.media_text_inlined == [True, False]
    assert both.media_urls[1] == photo.media_urls[0]


@pytest.mark.asyncio
async def test_works_without_rich_sent_store_async_functions(connected, meta, monkeypatch):
    from gateway import rich_sent_store

    for name in ("record_async", "record_media_async", "lookup_async", "lookup_media_async"):
        monkeypatch.delattr(rich_sent_store, name, raising=False)
    await deliver(connected, meta, meta.inbound_image("wamid.i1"), offset=2)
    quote = {"from": CREATOR, "id": "wamid.i1"}
    await deliver(connected, meta, text_message("wamid.t1", "again", context=quote), offset=3)
    photo, reply = dispatched(connected)
    assert reply.media_urls == photo.media_urls


@pytest.mark.asyncio
async def test_every_event_has_aligned_lists(connected, meta):
    messages = [
        meta.inbound_image("wamid.1"),
        meta.inbound_voice("wamid.2"),
        meta.inbound_audio("wamid.3"),
        meta.inbound_video("wamid.4"),
        meta.inbound_document("wamid.5"),
        meta.inbound_photo_document("wamid.6"),
        meta.inbound_sticker("wamid.7"),
        text_message("wamid.8", "text"),
    ]
    await deliver(connected, meta, *messages, offset=2)
    events = dispatched(connected)
    assert len(events) == 8
    for event in events:
        assert_aligned(event)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
@pytest.mark.asyncio
async def test_cached_files_are_owner_only(connected, meta):
    await deliver(connected, meta, meta.inbound_image("wamid.i1"), meta.inbound_document("wamid.d1"), offset=2)
    for event in dispatched(connected):
        assert os.stat(event.media_urls[0]).st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_logs_never_carry_captions_filenames_ids_or_urls(connected, meta, caplog):
    caplog.set_level(logging.DEBUG)
    image = meta.inbound_image("wamid.i1", caption="SECRET-CAPTION")
    doc = meta.inbound_document("wamid.d1", filename="SECRET-NAME.pdf", caption="OTHER-CAPTION")
    broken = meta.inbound_video("wamid.v1", caption="THIRD-CAPTION")
    meta.fault_download(broken["video"]["id"], "bad_bytes")
    await deliver(connected, meta, image, doc, broken, offset=2)
    assert len(dispatched(connected)) == 3
    ours = [r for r in caplog.records if r.name.startswith("wap_plugin_under_test")]
    assert ours  # the flow does log (received / not received)
    text = "\n".join(f"{r.getMessage()} {r.exc_text or ''}" for r in ours)
    for secret in ("SECRET-CAPTION", "OTHER-CAPTION", "THIRD-CAPTION", "SECRET-NAME", API_KEY, LOOKASIDE_HOST):
        assert secret not in text
    for m in (image["image"], doc["document"], broken["video"]):
        assert m["id"] not in text


# ---------------------------------------------------------------- the switch and notices
@pytest.mark.parametrize("value", ["false", "0", "no", "OFF"])
@pytest.mark.asyncio
async def test_switch_off_restores_the_text_only_notice(meta, api_key, monkeypatch, value):
    monkeypatch.setenv(mod.MEDIA_ENV, value)
    a = make_adapter()
    assert await a.connect()
    await deliver(a, meta, meta.inbound_image("wamid.i1"), offset=2)
    await until(lambda: len(meta.calls("messages")) == 1)
    await a.disconnect()
    assert a.handle_message.await_count == 0
    assert meta.bodies("messages")[0]["text"]["body"] == mod.UNSUPPORTED_NOTICE
    assert meta.calls("media_get") == [] and meta.calls("download") == []


@pytest.mark.asyncio
async def test_switch_off_from_config_extra_and_env_wins(meta, api_key, monkeypatch):
    from gateway.config import PlatformConfig

    assert make_adapter(PlatformConfig(enabled=True, extra={"media_enabled": False}))._media_enabled is False
    assert make_adapter(PlatformConfig(enabled=True, extra={"media_enabled": "true"}))._media_enabled is True
    assert make_adapter()._media_enabled is True
    monkeypatch.setenv(mod.MEDIA_ENV, "true")
    assert make_adapter(PlatformConfig(enabled=True, extra={"media_enabled": False}))._media_enabled is True


def test_env_enablement_seeds_the_switch(api_key, monkeypatch):
    assert "media_enabled" not in mod._env_enablement()
    monkeypatch.setenv(mod.MEDIA_ENV, "off")
    assert mod._env_enablement()["media_enabled"] is False
    monkeypatch.setenv(mod.MEDIA_ENV, "yes")
    assert mod._env_enablement()["media_enabled"] is True


@pytest.mark.asyncio
async def test_suppressed_warnings_send_no_notice(connected, meta):
    connected.warning_text = lambda *args, **kwargs: None
    # The notice would be scheduled synchronously while the page is handled, so once the offset is
    # saved a spy on _send_notice decides deterministically.
    connected._send_notice = AsyncMock()
    location = media_message("wamid.l1", "location", {"latitude": 1, "longitude": 2})
    await deliver(connected, meta, location, text_message("wamid.t1", "after"), offset=2)
    assert [e.text for e in dispatched(connected)] == ["after"]
    connected._send_notice.assert_not_called()
    assert saved_state().seen("wamid.l1")


@pytest.mark.asyncio
async def test_suppress_warning_notifications_config_is_honoured(meta, api_key, hermes_home):
    (hermes_home / "config.yaml").write_text(
        "display:\n  platforms:\n    whatsapp_agent_platform:\n      suppress_warning_notifications: true\n",
        encoding="utf-8",
    )
    a = make_adapter()
    a._send_notice = AsyncMock()
    assert await a.connect()
    location = media_message("wamid.l1", "location", {"latitude": 1, "longitude": 2})
    await deliver(a, meta, location, text_message("wamid.t1", "after"), offset=2)
    await a.disconnect()
    assert [e.text for e in dispatched(a)] == ["after"]
    a._send_notice.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_type_with_media_on_gets_a_notice(connected, meta):
    location = media_message("wamid.l1", "location", {"latitude": 1, "longitude": 2})
    spy = AsyncMock(wraps=connected._send_notice)
    connected._send_notice = spy
    await deliver(connected, meta, location, offset=2)
    spy.assert_called_once()  # the same spy stays silent in the two suppression tests above
    await until(lambda: len(meta.calls("messages")) == 1)
    assert connected.handle_message.await_count == 0
    assert meta.bodies("messages")[0]["to"] == CREATOR


# ---------------------------------------------------------------- commands in captions
@pytest.mark.parametrize(("caption", "command", "args"), [("/new", "new", ""), ("/model gpt-5", "model", "gpt-5")])
@pytest.mark.asyncio
async def test_command_caption_is_a_command_like_a_text_body(connected, meta, caption, command, args):
    """The caption is the event text, unchanged, exactly as a text body is: Hermes's own command detection decides,
    and it sees the same thing it would see for a text message."""
    image = meta.inbound_image("wamid.i1", caption=caption)
    await deliver(connected, meta, image, text_message("wamid.t1", caption), offset=2)
    photo, text = dispatched(connected)
    assert photo.message_type == MessageType.PHOTO and len(photo.media_urls) == 1
    assert photo.text == text.text == caption
    assert photo.get_command() == text.get_command() == command
    assert photo.get_command_args() == text.get_command_args() == args


@pytest.mark.asyncio
async def test_command_caption_on_an_inlined_text_file_is_not_a_command(connected, meta):
    """Hermes puts inlined file content before the caption (its WhatsApp Cloud adapter does the same), so the
    message starts with the content, not the command."""
    doc = meta.inbound_document("wamid.d1", b"hello", mime_type="text/plain", filename="n.txt", caption="/new")
    await deliver(connected, meta, doc, offset=2)
    event = only_event(connected)
    assert event.text.startswith("[Content of n.txt]:") and event.text.endswith("/new")
    assert event.get_command() is None


# ---------------------------------------------------------------- restart, metadata, expiry
@pytest.mark.asyncio
async def test_restart_mid_page_does_not_redispatch_media(meta, api_key):
    """Hermes takes the image, then rejects the text; the process restarts before the page completes. The new
    adapter re-polls the same page from the saved offset: the image is neither downloaded nor dispatched again."""
    first = make_adapter()
    first._backoff_base = 5.0  # keep the page from being retried before the "restart"
    calls = []

    async def handle(event):
        calls.append(event.message_id)
        if event.message_id == "wamid.t1":
            raise RuntimeError("hermes busy")

    first.handle_message = AsyncMock(side_effect=handle)
    assert await first.connect()
    image = meta.inbound_image("wamid.i1", caption="look")
    page = [image, text_message("wamid.t1", "and this")]
    meta.queue("updates", updates_response(*page, next_offset=2))
    await until(lambda: calls == ["wamid.i1", "wamid.t1"])
    await until(lambda: saved_state().seen("wamid.i1"))
    await first.disconnect()
    assert saved_state().next_offset == 0

    second = make_adapter()
    assert await second.connect()
    await deliver(second, meta, *page, offset=2)
    await second.disconnect()
    assert [e.message_id for e in dispatched(second)] == ["wamid.t1"]
    assert len(meta.calls("media_get")) == 1 and len(meta.calls("download")) == 1


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"id": "x", "mime_type": "image/jpeg"}),  # no url
        httpx.Response(200, content=b"<html>not json</html>", headers={"content-type": "text/html"}),
        httpx.Response(200, json=["not", "an", "object"]),
    ],
    ids=["no-url", "not-json", "not-an-object"],
)
@pytest.mark.asyncio
async def test_malformed_media_metadata_is_a_note(connected, meta, hermes_home, response):
    meta.queue("media_get", response)
    await deliver(connected, meta, meta.inbound_image("wamid.i1", caption="c"), text_message("wamid.t1", "t"), offset=2)
    await deliver(connected, meta, text_message("wamid.t2", "t2"), offset=3)
    first, *rest = dispatched(connected)
    assert_note_only(first, "c")
    assert media_inbound.FAILED in first.text
    assert [e.text for e in rest] == ["t", "t2"]
    assert not connected.has_fatal_error
    assert meta.calls("download") == [] and cache_files(hermes_home) == []


@pytest.mark.parametrize(
    "media_obj",
    [
        {"id": "12345", "mime_type": "image/jpeg", "sha256": "not-a-digest"},
        {"id": 12345, "mime_type": "image/jpeg"},
        {"id": "12345", "mime_type": ["image/jpeg"]},
        {"id": "12345", "caption": {"text": "x"}},
        ["not", "an", "object"],
        "not an object",
        None,
    ],
    ids=["bad-sha", "int-id", "list-mime", "dict-caption", "list", "string", "null"],
)
@pytest.mark.asyncio
async def test_malformed_media_object_in_the_update_is_a_note(connected, meta, media_obj):
    msg = media_message("wamid.i1", "image", media_obj)
    await deliver(connected, meta, msg, text_message("wamid.t1", "next"), offset=2)
    first, second = dispatched(connected)
    assert_note_only(first)
    assert second.text == "next"
    assert meta.calls("media_get") == [] and meta.calls("download") == []
    assert not connected.has_fatal_error


@pytest.mark.parametrize("how", ["deleted", "download-404"])
@pytest.mark.asyncio
async def test_expired_media_note_says_no_longer_available(connected, meta, how):
    msg = meta.inbound_image("wamid.i1", caption="old photo")
    if how == "deleted":
        meta.expire_media(msg["image"]["id"])  # GET /media answers 400/100
    else:
        meta.fault_download(msg["image"]["id"], "not_found")
    await deliver(connected, meta, msg, offset=2)
    event = only_event(connected)
    assert_note_only(event, "old photo")
    assert media_inbound.UNAVAILABLE in event.text and media_inbound.FAILED not in event.text


@pytest.mark.asyncio
async def test_repeated_server_errors_note_says_download_failed(connected, meta):
    meta.queue("media_get", error_response(503, 2), error_response(503, 2))
    await deliver(connected, meta, meta.inbound_image("wamid.i1"), offset=2)
    event = only_event(connected)
    assert media_inbound.FAILED in event.text and media_inbound.UNAVAILABLE not in event.text


# ---------------------------------------------------------------- Hermes's routing of voice and audio
@pytest.mark.asyncio
async def test_voice_note_goes_to_stt_and_an_audio_file_does_not(connected, meta):
    run, mixin = hermes_routing()
    await deliver(connected, meta, meta.inbound_voice("wamid.v1"), meta.inbound_audio("wamid.a1"), offset=2)
    voice, audio = dispatched(connected)
    assert run._event_media_is_stt_input(voice, 0) is True
    assert run._event_media_is_stt_input(audio, 0) is False
    images, stt, audio_files, videos = mixin._classify_inbound_media(voice, False)
    assert stt == voice.media_urls and images == audio_files == videos == []
    images, stt, audio_files, videos = mixin._classify_inbound_media(audio, False)
    assert audio_files == audio.media_urls and images == stt == videos == []


# ---------------------------------------------------------------- nothing left behind
@pytest.mark.parametrize(("mode", "options"), [f for f in DOWNLOAD_FAULTS if f[0] != "no_length"])
@pytest.mark.asyncio
async def test_failed_downloads_leave_nothing_in_the_cache(connected, meta, hermes_home, mode, options):
    kept = meta.inbound_document("wamid.d0")  # one good file first, so the cache directory exists
    msg = meta.inbound_image("wamid.i1")
    meta.fault_download(msg["image"]["id"], mode, **options)
    await deliver(connected, meta, kept, msg, offset=2)
    good, failed = dispatched(connected)
    assert_note_only(failed)
    assert cache_files(hermes_home) == [Path(good.media_urls[0])]


@pytest.mark.asyncio
async def test_failed_cache_step_leaves_nothing_in_the_cache(connected, meta, hermes_home, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("inline step failed")

    monkeypatch.setattr(mod.media, "should_inline_text", broken)
    doc = meta.inbound_document("wamid.d1", b"hello", mime_type="text/plain", filename="n.txt", caption="c")
    await deliver(connected, meta, doc, text_message("wamid.t1", "next"), offset=2)
    first, second = dispatched(connected)
    assert_note_only(first, "c")
    assert second.text == "next"
    assert cache_files(hermes_home) == []


# ---------------------------------------------------------------- a burst over the media_get budget
@pytest.mark.asyncio
async def test_photo_burst_over_the_media_get_budget_is_bounded(connected, meta):
    """13 photos in one page against the real 12-per-window media_get budget (a short window here): every photo is
    dispatched in order, the 13th waits for a free slot, and polling continues afterwards."""
    from wap_plugin_under_test.client import RATE_LIMITS, RateWindow

    stamps: list[float] = []

    class Recording(RateWindow):
        async def acquire(self) -> None:
            await super().acquire()
            stamps.append(time.monotonic())

    window = 0.3
    connected._client.limits["media_get"] = Recording(RATE_LIMITS["media_get"], window)
    photos = [meta.inbound_image(f"wamid.p{i:02d}") for i in range(13)]
    await deliver(connected, meta, *photos, offset=2)
    await deliver(connected, meta, text_message("wamid.t1", "after the burst"), offset=3)
    events = dispatched(connected)
    assert [e.message_id for e in events] == [f"wamid.p{i:02d}" for i in range(13)] + ["wamid.t1"]
    assert all(e.message_type == MessageType.PHOTO and len(e.media_urls) == 1 for e in events[:13])
    assert len(stamps) == 13
    assert stamps[12] - stamps[0] >= window * 0.9  # never 13 GETs inside one window
    assert not connected.has_fatal_error
