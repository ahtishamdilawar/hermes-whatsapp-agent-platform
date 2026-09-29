"""client.py media transport: upload, metadata, safe download, delete and media sends (plan §8).

Error shapes come from the live P0 captures reproduced by ``FakeMeta``. Security properties (S-01, S-03, S-08,
S-10, R-06 in ``notes/media-research/05``) are asserted on what actually reached each host.
"""

from __future__ import annotations

import dataclasses

import httpx
import pytest
from wap_helpers import (
    API_KEY,
    CREATOR,
    EVIL_HOST,
    LOOKASIDE_HOST,
    bad_field_response,
    download_unauthorized_response,
    error_response,
    meta_error,
    multipart_fields,
    rate_limited_response,
    sample_jpeg,
    sample_mp3,
    sample_mp4,
    sample_pdf,
    sample_webp,
    sha256_b64,
    sha256_hex,
    upload_missing_field_response,
)
from wap_plugin_under_test import client as c

API_HOST = "api.whatsapp.com"
SENTINEL = "zzSENTINELzz"
CHUNK = 64 * 1024
MEDIA_WINDOWS = ("media_upload", "media_get", "media_delete")


def remaining(api) -> dict[str, int]:
    return {name: window.remaining() for name, window in api.limits.items()}


def lookaside_info(media_id: str = "abc", *, file_size=None, sha256=None, url=None) -> c.MediaInfo:
    url = f"https://{LOOKASIDE_HOST}/agent/v1/media/{media_id}/content?ext=bin&hash=h" if url is None else url
    return c.MediaInfo(id=media_id, url=url, mime_type="application/pdf", sha256=sha256, file_size=file_size)


class Pulled:
    """Counts the bytes a streamed fake body actually handed to the client."""

    def __init__(self) -> None:
        self.bytes = 0


def streamed(total: int, pulled: Pulled, *, headers: dict | None = None) -> httpx.Response:
    async def body():
        sent = 0
        while sent < total:
            n = min(CHUNK, total - sent)
            sent += n
            pulled.bytes += n
            yield b"\0" * n

    return httpx.Response(200, headers=headers or {}, content=body())


def assert_clean(exc: BaseException, *secrets: str) -> None:
    """No secret in the message, repr, args or ``describe()``, and no chained httpx error carrying a URL."""
    texts = [str(exc), repr(exc), *map(str, exc.args)]
    if isinstance(exc, c.AgentPlatformError):
        texts.append(exc.describe())
    for secret in (API_KEY, *secrets):
        for text in texts:
            assert secret not in text, text
    assert exc.__cause__ is None


# ------------------------------------------------------------------ contract basics


def test_rate_limits_and_error_hierarchy():
    assert {name: c.RATE_LIMITS[name] for name in MEDIA_WINDOWS} == dict.fromkeys(MEDIA_WINDOWS, 12)
    assert "download" not in c.RATE_LIMITS and "media_download" not in c.RATE_LIMITS
    for cls in (c.MediaRejected, c.MediaGone, c.MediaDownloadError):
        assert issubclass(cls, c.NotSent) and not cls.retryable
    for cls in (c.UntrustedMediaURL, c.MediaTooLarge, c.MediaIntegrityError, c.MediaRedirected):
        assert issubclass(cls, c.MediaDownloadError)
    assert not issubclass(c.MediaDownloadError, c.AuthError) and c.DEFAULT_MEDIA_HOSTS == (LOOKASIDE_HOST,)
    assert c.MEDIA_KINDS == ("image", "video", "audio", "document", "sticker")


def test_media_info_is_frozen_and_repr_hides_id_and_url():
    info = lookaside_info(SENTINEL, url=f"https://{LOOKASIDE_HOST}/{SENTINEL}")
    assert SENTINEL not in repr(info)
    with pytest.raises(dataclasses.FrozenInstanceError):
        info.url = "https://evil.example/"  # type: ignore[misc]


def test_client_limits_include_media_windows(meta):
    api = meta.client()
    assert all(api.limits[name].limit == 12 for name in MEDIA_WINDOWS)
    assert api.media_hosts == frozenset({LOOKASIDE_HOST})


# ------------------------------------------------------------------ upload


@pytest.mark.asyncio
async def test_upload_sends_multipart_fields_and_returns_id(meta):
    api = meta.client()
    data = sample_jpeg()
    media_id = await api.upload_media(data, mime="image/jpeg", filename="photo.jpg")
    [record] = meta.uploads
    assert record.media_id == media_id and meta.media[media_id].data == data
    assert (record.messaging_product, record.type) == ("whatsapp", "image/jpeg")
    assert (record.filename, record.content_type, record.data) == ("photo.jpg", "image/jpeg", data)
    request = meta.calls("media_upload")[0]
    assert request.url == "https://api.whatsapp.com/agent/v1/media" and request.method == "POST"
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"
    assert set(multipart_fields(request)) == {"messaging_product", "type", "file"}
    assert remaining(api)["media_upload"] == 11 and remaining(api)["messages"] == 12


@pytest.mark.asyncio
async def test_upload_filename_cannot_inject_multipart_headers(meta):
    api = meta.client()
    await api.upload_media(b"%PDF-1.4", mime="application/pdf", filename='a"\r\nX-Evil: 1\r\n\r\nb.pdf')
    fields = multipart_fields(meta.calls("media_upload")[0])
    assert set(fields) == {"messaging_product", "type", "file"}
    assert "\r" not in fields["file"].filename and "\n" not in fields["file"].filename
    assert fields["file"].data == b"%PDF-1.4"


@pytest.mark.parametrize("mime", ["", "jpeg", "image/jpeg\r\nX-Evil: 1", 'image/"jpeg"', None])
@pytest.mark.asyncio
async def test_upload_rejects_bad_mime_or_filename_without_a_request(meta, mime):
    api = meta.client()
    with pytest.raises(ValueError):
        await api.upload_media(b"x", mime=mime, filename="a.bin")
    with pytest.raises(ValueError):
        await api.upload_media(b"x", mime="text/plain", filename="")
    assert meta.requests == [] and remaining(api)["media_upload"] == 12


@pytest.mark.asyncio
async def test_upload_accepts_mime_parameters(meta):
    media_id = await meta.client().upload_media(b"OggS", mime="audio/ogg; codecs=opus", filename="v.ogg")
    assert meta.uploads[0].type == "audio/ogg; codecs=opus" and media_id in meta.media


@pytest.mark.parametrize(
    ("mime", "size", "code", "details"),
    [
        ("image/gif", 6, 131053, "image/gif is not a supported media type"),
        ("image/jpeg", 5 * 1024 * 1024 + 1, 131053, "over the 5242880 byte limit"),
    ],
)
@pytest.mark.asyncio
async def test_upload_meta_refusal_is_media_rejected(meta, mime, size, code, details):
    with pytest.raises(c.MediaRejected) as info:
        await meta.client().upload_media(b"\xff" * size, mime=mime, filename="f")
    assert info.value.code == code and details in info.value.details and not info.value.retryable


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        (upload_missing_field_response(), c.MediaRejected),
        (error_response(413, 0), c.MediaRejected),
        (error_response(503, 131016), c.Retryable),
        (error_response(500, 2), c.Ambiguous),
        (error_response(401, 190), c.AuthError),
        (error_response(409, 1752041), c.NotSent),
        (error_response(400, 100), c.NotSent),
        (httpx.ConnectError("boom"), c.Retryable),
        (httpx.ReadTimeout("slow"), c.Ambiguous),
        (httpx.WriteTimeout("slow"), c.Ambiguous),
        (httpx.RemoteProtocolError("reset"), c.Ambiguous),
        (httpx.Response(200, json={"nope": 1}), c.MalformedResponse),
    ],
)
@pytest.mark.asyncio
async def test_upload_error_taxonomy(meta, item, expected):
    meta.queue("media_upload", item)
    with pytest.raises(expected) as info:
        await meta.client().upload_media(b"abc", mime="text/plain", filename="a.txt")
    assert type(info.value) is expected


@pytest.mark.asyncio
async def test_upload_429_penalizes_only_the_upload_window(meta):
    meta.queue("media_upload", rate_limited_response(7))
    api = meta.client()
    with pytest.raises(c.RateLimited) as info:
        await api.upload_media(b"abc", mime="text/plain", filename="a.txt")
    assert info.value.retry_after == 7.0
    assert api.limits["media_upload"].delay() == pytest.approx(7.0, abs=0.5)
    assert {k: v for k, v in remaining(api).items() if k != "media_upload"} == {
        "messages": 12,
        "statuses": 12,
        "updates": 14,
        "media_get": 12,
        "media_delete": 12,
    }


# ------------------------------------------------------------------ GET / DELETE


@pytest.mark.asyncio
async def test_get_media_returns_info(meta):
    stored = meta.add_media(sample_pdf(), "application/pdf")
    api = meta.client()
    info = await api.get_media(stored.id)
    assert info == c.MediaInfo(
        id=stored.id,
        url=meta.media_url(stored.id),
        mime_type="application/pdf",
        sha256=stored.sha256_hex,
        file_size=stored.file_size,
    )
    request = meta.calls("media_get")[0]
    assert request.url.path == f"/agent/v1/media/{stored.id}" and request.method == "GET"
    assert remaining(api)["media_get"] == 11


@pytest.mark.asyncio
async def test_get_media_tolerates_missing_optional_fields_and_rejects_missing_url(meta):
    meta.queue("media_get", httpx.Response(200, json={"url": "https://x/y", "file_size": True, "sha256": 5}))
    meta.queue("media_get", httpx.Response(200, json={"id": "abc"}), httpx.Response(200, content=b"<html>"))
    api = meta.client()
    info = await api.get_media("abc")
    assert (info.mime_type, info.sha256, info.file_size) == (None, None, None)
    for _ in range(2):
        with pytest.raises(c.MalformedResponse):
            await api.get_media("abc")


@pytest.mark.asyncio
async def test_get_and_delete_of_unknown_or_deleted_media_are_media_gone(meta):
    stored = meta.add_media(sample_pdf(), "application/pdf")
    api = meta.client()
    await api.delete_media(stored.id)
    assert meta.calls("media_delete")[0].url.path == f"/agent/v1/media/{stored.id}"
    for call in (api.get_media, api.delete_media):
        with pytest.raises(c.MediaGone) as info:
            await call(stored.id)
        assert (info.value.status, info.value.code) == (400, 100)
    meta.queue("media_get", meta_error(404, 0, "gone"))
    with pytest.raises(c.MediaGone):
        await api.get_media("abc")
    assert remaining(api)["media_delete"] == 10 and remaining(api)["media_get"] == 10


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        (rate_limited_response(4), c.RateLimited),
        (error_response(503, 131016), c.Retryable),
        (error_response(401, 190), c.AuthError),
        (error_response(409, 1752041), c.NotSent),
        (httpx.ConnectError("boom"), c.Retryable),
        (httpx.ReadTimeout("slow"), c.Retryable),  # a read: safe to retry
    ],
)
@pytest.mark.asyncio
async def test_get_media_transport_taxonomy(meta, item, expected):
    meta.queue("media_get", item)
    api = meta.client()
    with pytest.raises(expected) as info:
        await api.get_media("abc")
    assert type(info.value) is expected
    if expected is c.RateLimited:
        assert api.limits["media_get"].remaining() == 0 and api.limits["media_upload"].remaining() == 12


@pytest.mark.asyncio
async def test_delete_media_timeout_is_ambiguous_and_429_penalizes_delete_window(meta):
    meta.queue("media_delete", httpx.ReadTimeout("slow"), rate_limited_response(3), httpx.Response(200, json={}))
    api = meta.client()
    with pytest.raises(c.Ambiguous):
        await api.delete_media("abc")
    with pytest.raises(c.RateLimited):
        await api.delete_media("abc")
    assert api.limits["media_delete"].remaining() == 0 and api.limits["media_get"].remaining() == 12
    api.limits["media_delete"] = c.RateWindow(12)
    with pytest.raises(c.MalformedResponse):
        await api.delete_media("abc")


BAD_IDS = ["../x", "..", ".", "a/b", "x?y", "a#b", "%2F", "%2e%2e", "", "a" * 129, "ab c", "abc\n", "é", None, 5]


@pytest.mark.parametrize("media_id", BAD_IDS)
@pytest.mark.asyncio
async def test_invalid_media_ids_never_reach_a_url(meta, media_id):
    api = meta.client()
    for call in (api.get_media, api.delete_media):
        with pytest.raises(ValueError) as info:
            await call(media_id)
        if isinstance(media_id, str) and media_id:
            assert media_id not in str(info.value)
    assert meta.requests == [] and remaining(api)["media_get"] == remaining(api)["media_delete"] == 12


@pytest.mark.asyncio
async def test_longest_valid_media_id_is_accepted(meta):
    media_id = "a" + "b-._" * 31 + "cde"
    assert len(media_id) == 128
    with pytest.raises(c.MediaGone):
        await meta.client().get_media(media_id)
    assert meta.calls("media_get")[0].url.path == f"/agent/v1/media/{media_id}"


# ------------------------------------------------------------------ media sends


SEND_CASES = [
    ("image", "image/jpeg", sample_jpeg(), {"caption": "look"}),
    ("video", "video/mp4", sample_mp4(), {"caption": "clip"}),
    ("audio", "audio/mpeg", sample_mp3(), {}),
    ("document", "application/pdf", sample_pdf(), {"caption": "report", "filename": "report.pdf"}),
    ("sticker", "image/webp", sample_webp(), {}),
]


@pytest.mark.parametrize(("kind", "mime", "data", "fields"), SEND_CASES)
@pytest.mark.asyncio
async def test_send_media_every_kind(meta, kind, mime, data, fields):
    api = meta.client()
    media_id = await api.upload_media(data, mime=mime, filename=f"f.{kind}")
    wamid = await api.send_media(CREATOR, kind, media_id, reply_to="wamid.in1", **fields)
    [sent] = meta.sent_media
    assert sent.wamid == wamid and sent.type == kind and sent.media.data == data
    assert sent.body == {
        "messaging_product": "whatsapp",
        "to": CREATOR,
        "type": kind,
        kind: {"id": media_id, **fields},
        "context": {"message_id": "wamid.in1"},
    }
    assert remaining(api)["messages"] == 11


@pytest.mark.asyncio
async def test_send_media_omits_empty_optional_fields(meta):
    stored = meta.add_media(sample_jpeg(), "image/jpeg")
    await meta.client().send_media(CREATOR, "image", stored.id, caption="", filename=None, reply_to=None)
    assert meta.sent_media[0].body == {
        "messaging_product": "whatsapp",
        "to": CREATOR,
        "type": "image",
        "image": {"id": stored.id},
    }


@pytest.mark.parametrize(("kind", "media_id"), [("reaction", "abc"), ("text", "abc"), ("image", ""), ("image", None)])
@pytest.mark.asyncio
async def test_send_media_rejects_bad_kind_or_id_without_a_request(meta, kind, media_id):
    with pytest.raises(ValueError):
        await meta.client().send_media(CREATOR, kind, media_id)
    assert meta.requests == []


@pytest.mark.asyncio
async def test_send_media_live_error_mapping(meta):
    api = meta.client()
    webp = meta.add_media(sample_webp(), "image/webp")
    jpeg = meta.add_media(sample_jpeg(), "image/jpeg")
    mp3 = meta.add_media(sample_mp3(), "audio/mpeg")

    with pytest.raises(c.MediaGone) as gone:  # live: 400/131009 "No media found for id …"
        await api.send_media(CREATOR, "image", "00000000-0000-0000-0000-000000000000")
    assert (gone.value.status, gone.value.code) == (400, 131009)

    with pytest.raises(c.MediaRejected) as mismatch:  # live: WebP as image
        await api.send_media(CREATOR, "image", webp.id)
    assert mismatch.value.code == 131009 and "cannot be sent" in mismatch.value.details

    with pytest.raises(c.MediaRejected) as caption:  # live: 400/100 "caption", no error_data
        await api.send_media(CREATOR, "image", jpeg.id, caption="x" * 1025)
    assert (caption.value.code, caption.value.details) == (100, None)

    with pytest.raises(c.MediaRejected):  # captions are not validated here: Meta refuses one on audio
        await api.send_media(CREATOR, "audio", mp3.id, caption="no")
    assert meta.sent_media == []


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        (meta_error(400, 131053, "(#131053) Media upload failed", "The uploaded media was not accepted"), None),
        (bad_field_response("  no media found for id x"), c.MediaGone),
        (error_response(403, 131005), c.NotCreatorError),
        (error_response(503, 131016), c.Retryable),
        (error_response(500, 2), c.Ambiguous),
        (error_response(409, 1752041), c.NotSent),
        (httpx.ConnectError("boom"), c.Retryable),
        (httpx.ReadTimeout("slow"), c.Ambiguous),
        (httpx.RemoteProtocolError("reset"), c.Ambiguous),
        (httpx.Response(200, json={"messages": []}), c.MalformedResponse),
    ],
)
@pytest.mark.asyncio
async def test_send_media_taxonomy(meta, item, expected):
    meta.queue("messages", item)
    with pytest.raises(expected or c.MediaRejected) as info:
        await meta.client().send_media(CREATOR, "document", "abc", filename="a.pdf")
    assert type(info.value) is (expected or c.MediaRejected)


@pytest.mark.asyncio
async def test_send_media_429_penalizes_the_messages_window(meta):
    meta.queue("messages", rate_limited_response(9))
    api = meta.client()
    with pytest.raises(c.RateLimited):
        await api.send_media(CREATOR, "image", "abc")
    assert api.limits["messages"].delay() == pytest.approx(9.0, abs=0.5)
    assert api.limits["media_upload"].remaining() == 12


@pytest.mark.asyncio
async def test_text_sends_keep_their_plain_taxonomy(meta):
    meta.queue("messages", bad_field_response("No media found for id x"), meta_error(400, 100, "caption"))
    api = meta.client()
    for _ in range(2):
        with pytest.raises(c.NotSent) as info:
            await api.send_text(CREATOR, "hi")
        assert type(info.value) is c.NotSent


# ------------------------------------------------------------------ 409 scoping


@pytest.mark.asyncio
async def test_409_is_poll_conflict_only_on_updates(meta):
    api = meta.client()
    meta.queue("updates", error_response(409, 1752041))
    with pytest.raises(c.PollConflict):
        await api.get_updates(0)
    meta.queue("messages", error_response(409, 1752041), error_response(400, 1752041))
    meta.queue("statuses", error_response(409, 1752041))
    for call in (lambda: api.send_text(CREATOR, "x"), lambda: api.send_text(CREATOR, "y")):
        with pytest.raises(c.NotSent) as info:
            await call()
        assert type(info.value) is c.NotSent
    with pytest.raises(c.NotSent) as info:
        await api.mark_read("wamid.in1")
    assert type(info.value) is c.NotSent


def test_classify_response_scopes_409_to_updates():
    response = error_response(409, 1752041)
    assert type(c.classify_response(response, "updates")) is c.PollConflict
    for action in ("send", "statuses", "media_upload", "media_get", "media_delete"):
        assert type(c.classify_response(response, action)) is c.NotSent
        assert not isinstance(c.classify_media_response(response, action), c.PollConflict)


# ------------------------------------------------------------------ download: happy paths


@pytest.mark.asyncio
async def test_download_streams_verified_bytes_with_identity_encoding_and_no_budget(meta):
    stored = meta.add_media(bytes(range(256)) * 1000, "application/pdf")
    api = meta.client()
    info = await api.get_media(stored.id)
    before = remaining(api)
    assert await api.download_media(info, max_bytes=stored.file_size) == stored.data  # exactly at the cap
    assert await api.download_media(info, max_bytes=10**7, verify_sha256=stored.sha256_b64) == stored.data
    assert await api.download_media(info, max_bytes=10**7, verify_sha256=stored.sha256_hex.upper()) == stored.data
    assert remaining(api) == before  # downloads have no budget (P0)
    request = meta.calls("download")[0]
    assert request.url == info.url and request.headers["Accept-Encoding"] == "identity"
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"
    assert meta.authorized_hosts() == {API_HOST, LOOKASIDE_HOST}


@pytest.mark.asyncio
async def test_download_inbound_base64_digest_without_metadata_digest(meta):
    msg = meta.inbound_image("wamid.1")
    api = meta.client()
    info = dataclasses.replace(await api.get_media(msg["image"]["id"]), sha256=None)
    assert await api.download_media(info, max_bytes=10**6, verify_sha256=msg["image"]["sha256"]) == sample_jpeg()
    urlsafe = msg["image"]["sha256"].replace("+", "-").replace("/", "_").rstrip("=")
    assert await api.download_media(info, max_bytes=10**6, verify_sha256=urlsafe) == sample_jpeg()


@pytest.mark.asyncio
async def test_download_without_any_digest_or_size_still_works(meta):
    stored = meta.add_media(b"hello", "text/plain")
    info = c.MediaInfo(id=stored.id, url=meta.media_url(stored.id), mime_type=None, sha256=None, file_size=None)
    assert await meta.client().download_media(info, max_bytes=5) == b"hello"


@pytest.mark.asyncio
async def test_base_url_override_host_is_allowlisted_but_nothing_wider(meta):
    api = meta.client(base_url="https://proxy.internal/agent/v1")
    assert api.media_hosts == frozenset({LOOKASIDE_HOST, "proxy.internal"})
    meta.download_host = "proxy.internal"
    stored = meta.add_media(b"via proxy", "text/plain")
    info = await api.get_media(stored.id)
    assert httpx.URL(info.url).host == "proxy.internal"
    assert await api.download_media(info, max_bytes=100) == b"via proxy"
    for url in ("https://sub.proxy.internal/x", "https://internal/x", f"https://{API_HOST}/x"):
        with pytest.raises(c.UntrustedMediaURL):
            await api.download_media(lookaside_info(url=url), max_bytes=100)
    default = meta.client(base_url=c.API_BASE + "/")
    assert default.media_hosts == frozenset({LOOKASIDE_HOST})  # api.whatsapp.com is not a media host


# ------------------------------------------------------------------ download: the key never leaves the allowlist


UNTRUSTED_URLS = [
    f"https://{EVIL_HOST}/agent/v1/media/{SENTINEL}/content",
    f"http://{LOOKASIDE_HOST}/agent/v1/media/{SENTINEL}/content",
    f"https://{LOOKASIDE_HOST}.evil.com/agent/v1/media/{SENTINEL}/content",
    f"https://x{LOOKASIDE_HOST}/{SENTINEL}",
    f"https://fbsbx.com/{SENTINEL}",
    f"https://user:pw@{LOOKASIDE_HOST}/{SENTINEL}",
    f"https://{SENTINEL}@{LOOKASIDE_HOST}/x",
    f"https://{LOOKASIDE_HOST}:8443/{SENTINEL}",
    f"https://{LOOKASIDE_HOST}:80/{SENTINEL}",
    f"https://{LOOKASIDE_HOST.upper()}./{SENTINEL}",
    f"https://{LOOKASIDE_HOST}\\@{EVIL_HOST}/{SENTINEL}",
    f"https://{LOOKASIDE_HOST}/{SENTINEL} x",
    f"https://lookaside.fbsbx.cöm/{SENTINEL}",
    f"ftp://{LOOKASIDE_HOST}/{SENTINEL}",
    f"//{LOOKASIDE_HOST}/{SENTINEL}",
    f"/{SENTINEL}",
    f"https://{LOOKASIDE_HOST}/{SENTINEL}" + "a" * 5000,
    "",
    "https://169.254.169.254/latest/meta-data/",
]


@pytest.mark.parametrize("url", UNTRUSTED_URLS)
@pytest.mark.asyncio
async def test_untrusted_urls_get_no_request_at_all(meta, url):
    api = meta.client()
    with pytest.raises(c.UntrustedMediaURL) as info:
        await api.download_media(lookaside_info(url=url), max_bytes=10**6)
    assert meta.requests == []
    assert_clean(info.value, SENTINEL, url or SENTINEL)


@pytest.mark.asyncio
async def test_uppercase_allowlisted_host_is_the_same_host(meta):
    stored = meta.add_media(b"abc", "text/plain")
    url = meta.media_url(stored.id).replace(LOOKASIDE_HOST, LOOKASIDE_HOST.upper()).replace("https", "HTTPS", 1)
    url = url.replace(f"{LOOKASIDE_HOST.upper()}/", f"{LOOKASIDE_HOST.upper()}:443/")
    assert await meta.client().download_media(lookaside_info(url=url), max_bytes=10) == b"abc"
    assert meta.hosts() == {LOOKASIDE_HOST}


@pytest.mark.asyncio
async def test_metadata_pointing_off_host_never_gets_the_key(meta):
    stored = meta.add_media(sample_pdf(), "application/pdf")
    meta.fault_download(stored.id, "other_host")
    api = meta.client()
    info = await api.get_media(stored.id)
    with pytest.raises(c.UntrustedMediaURL):
        await api.download_media(info, max_bytes=10**6)
    assert meta.requests_to(EVIL_HOST) == [] and meta.authorized_hosts() == {API_HOST}


@pytest.mark.parametrize("location", [f"https://{EVIL_HOST}/x", f"https://{LOOKASIDE_HOST}/agent/v1/media/y/content"])
@pytest.mark.asyncio
async def test_redirects_are_refused_not_followed(meta, location):
    stored = meta.add_media(sample_pdf(), "application/pdf")
    meta.fault_download(stored.id, "redirect", location=location)
    api = meta.client()
    with pytest.raises(c.MediaRedirected) as info:
        await api.download_media(await api.get_media(stored.id), max_bytes=10**6)
    assert info.value.status == 302
    assert meta.requests_to(EVIL_HOST) == [] and len(meta.calls("download")) == 1
    assert meta.authorized_hosts() == {API_HOST, LOOKASIDE_HOST}
    assert_clean(info.value, location, stored.id)


# ------------------------------------------------------------------ download: size caps


@pytest.mark.asyncio
async def test_declared_file_size_over_cap_makes_no_request(meta):
    with pytest.raises(c.MediaTooLarge):
        await meta.client().download_media(lookaside_info(file_size=101), max_bytes=100)
    assert meta.requests == []


@pytest.mark.asyncio
async def test_content_length_over_cap_aborts_before_reading_the_body(meta):
    pulled = Pulled()
    meta.queue("download", streamed(10 * CHUNK, pulled, headers={"Content-Length": str(10 * CHUNK)}))
    with pytest.raises(c.MediaTooLarge):
        await meta.client().download_media(lookaside_info(), max_bytes=CHUNK)
    assert pulled.bytes == 0


@pytest.mark.asyncio
async def test_cap_holds_without_content_length(meta):
    pulled = Pulled()
    meta.queue("download", streamed(50 * 1024 * 1024, pulled))
    with pytest.raises(c.MediaTooLarge):
        await meta.client().download_media(lookaside_info(), max_bytes=100_000)
    assert pulled.bytes <= 100_000 + CHUNK  # memory bounded by the cap plus one chunk


@pytest.mark.asyncio
async def test_cap_holds_when_content_length_lies_low(meta):
    pulled = Pulled()
    meta.queue("download", streamed(50 * 1024 * 1024, pulled, headers={"Content-Length": "1000"}))
    with pytest.raises(c.MediaDownloadError) as info:
        await meta.client().download_media(lookaside_info(), max_bytes=100_000)
    assert isinstance(info.value, c.MediaIntegrityError | c.MediaTooLarge)
    assert pulled.bytes <= CHUNK


@pytest.mark.asyncio
async def test_large_body_under_a_lying_small_file_size_is_cut_at_the_expected_length(meta):
    stored = meta.inbound_oversize_document("wamid.big", file_size=3 * 1024 * 1024)["document"]
    meta.fault_download(stored["id"], "no_length")
    api = meta.client()
    info = dataclasses.replace(await api.get_media(stored["id"]), file_size=1024)
    with pytest.raises(c.MediaIntegrityError):
        await api.download_media(info, max_bytes=10 * 1024 * 1024)


@pytest.mark.parametrize(
    ("mode", "options"),
    [
        ("oversize", {}),  # Content-Length matches the body but not file_size: caught before the body
        ("no_length", {"extra_bytes": 5}),  # longer than file_size while streaming
        ("wrong_length", {}),  # Content-Length half the body
        ("wrong_length", {"declared_length": 256_000 + 100}),  # Content-Length longer than the body
    ],
)
@pytest.mark.asyncio
async def test_length_faults_are_integrity_errors(meta, mode, options):
    stored = meta.add_media(bytes(range(256)) * 1000, "application/pdf")
    meta.fault_download(stored.id, mode, **options)
    api = meta.client()
    with pytest.raises(c.MediaIntegrityError):
        await api.download_media(await api.get_media(stored.id), max_bytes=10**6)


@pytest.mark.asyncio
async def test_body_shorter_than_content_length_without_metadata_size(meta):
    pulled = Pulled()
    meta.queue("download", streamed(50, pulled, headers={"Content-Length": "100"}))
    with pytest.raises(c.MediaIntegrityError):
        await meta.client().download_media(lookaside_info(), max_bytes=1000)


@pytest.mark.parametrize("value", ["abc", "-5", "1e3", "10, 10"])
@pytest.mark.asyncio
async def test_invalid_content_length_is_rejected_before_the_body(meta, value):
    pulled = Pulled()
    meta.queue("download", streamed(10, pulled, headers={"Content-Length": value}))
    with pytest.raises(c.MediaIntegrityError):
        await meta.client().download_media(lookaside_info(), max_bytes=1000)
    assert pulled.bytes == 0


@pytest.mark.asyncio
async def test_compressed_body_is_refused_before_decoding(meta):
    pulled = Pulled()
    meta.queue("download", streamed(10 * CHUNK, pulled, headers={"Content-Encoding": "gzip"}))
    with pytest.raises(c.MediaDownloadError) as info:
        await meta.client().download_media(lookaside_info(), max_bytes=10**6)
    assert type(info.value) is c.MediaDownloadError and pulled.bytes == 0


@pytest.mark.parametrize("max_bytes", [0, -1, 1.5, True, None])
@pytest.mark.asyncio
async def test_max_bytes_must_be_a_positive_int(meta, max_bytes):
    with pytest.raises(ValueError):
        await meta.client().download_media(lookaside_info(), max_bytes=max_bytes)
    assert meta.requests == []


# ------------------------------------------------------------------ download: integrity


@pytest.mark.asyncio
async def test_flipped_byte_fails_the_hex_digest(meta):
    stored = meta.add_media(bytes(range(256)) * 1000, "application/pdf")
    meta.fault_download(stored.id, "bad_bytes")
    api = meta.client()
    with pytest.raises(c.MediaIntegrityError):
        await api.download_media(await api.get_media(stored.id), max_bytes=10**6)


@pytest.mark.asyncio
async def test_base64_digest_mismatch_and_disagreeing_digests(meta):
    stored = meta.add_media(b"real bytes", "text/plain")
    api = meta.client()
    info = await api.get_media(stored.id)
    with pytest.raises(c.MediaIntegrityError):
        await api.download_media(info, max_bytes=100, verify_sha256=sha256_b64(b"other bytes"))
    bare = dataclasses.replace(info, sha256=None)
    with pytest.raises(c.MediaIntegrityError):
        await api.download_media(bare, max_bytes=100, verify_sha256=sha256_hex(b"other bytes"))


@pytest.mark.parametrize("digest", ["nothex", "z" * 64, "YWJj", sha256_hex(b"x")[:-2], "===="])
@pytest.mark.asyncio
async def test_unparseable_expected_digest_makes_no_request(meta, digest):
    with pytest.raises(c.MediaIntegrityError):
        await meta.client().download_media(lookaside_info(), max_bytes=100, verify_sha256=digest)
    assert meta.requests == []


def test_parse_sha256_accepts_hex_and_base64():
    digest = bytes(range(32))
    assert c.parse_sha256(digest.hex()) == c.parse_sha256(digest.hex().upper()) == digest
    assert c.parse_sha256(sha256_b64(b"x")) == c.parse_sha256(sha256_hex(b"x")) == bytes.fromhex(sha256_hex(b"x"))


# ------------------------------------------------------------------ download: status and transport taxonomy


@pytest.mark.asyncio
async def test_deleted_media_download_is_media_gone(meta):
    stored = meta.add_media(sample_pdf(), "application/pdf")
    api = meta.client()
    info = await api.get_media(stored.id)
    meta.expire_media(stored.id)
    with pytest.raises(c.MediaGone) as info_err:
        await api.download_media(info, max_bytes=10**6)
    assert (info_err.value.status, info_err.value.code) == (404, 100)


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        (download_unauthorized_response(), c.MediaDownloadError),  # live 401/190: never AuthError
        (error_response(403, 131005), c.MediaDownloadError),  # never NotCreatorError
        (meta_error(410, 100, "gone"), c.MediaGone),
        (error_response(503, 131016), c.Retryable),
        (error_response(500, 2), c.Retryable),  # a read: safe to retry, unlike a send
        (httpx.Response(206, content=b"x"), c.MediaDownloadError),
        (httpx.Response(204), c.MediaDownloadError),
        (error_response(409, 1752041), c.MediaDownloadError),
        (httpx.ConnectError("boom"), c.Retryable),
        (httpx.ReadTimeout("slow"), c.Retryable),
        (httpx.RemoteProtocolError("reset"), c.Retryable),
    ],
)
@pytest.mark.asyncio
async def test_download_status_and_transport_taxonomy(meta, item, expected):
    meta.queue("download", item)
    with pytest.raises(expected) as info:
        await meta.client().download_media(lookaside_info(), max_bytes=10**6)
    assert type(info.value) is expected
    assert not isinstance(info.value, c.AuthError | c.NotCreatorError | c.PollConflict | c.Ambiguous)


@pytest.mark.asyncio
async def test_download_429_is_rate_limited_without_touching_any_window(meta):
    stored = meta.add_media(sample_pdf(), "application/pdf")
    meta.fault_download(stored.id, "status", status=429, retry_after=4)
    api = meta.client()
    info = await api.get_media(stored.id)
    before = remaining(api)
    with pytest.raises(c.RateLimited) as err:
        await api.download_media(info, max_bytes=10**6)
    assert err.value.retry_after == 4.0 and remaining(api) == before


@pytest.mark.parametrize("mode", ["timeout", "timeout_mid_stream"])
@pytest.mark.asyncio
async def test_download_timeouts_are_retryable(meta, mode):
    stored = meta.add_media(bytes(range(256)) * 1000, "application/pdf")
    meta.fault_download(stored.id, mode)
    api = meta.client()
    with pytest.raises(c.Retryable) as info:
        await api.download_media(await api.get_media(stored.id), max_bytes=10**6)
    assert type(info.value) is c.Retryable


@pytest.mark.asyncio
async def test_closed_client_download_is_retryable(meta):
    api = meta.client()
    await api._http.aclose()
    with pytest.raises(c.Retryable):
        await api.download_media(lookaside_info(), max_bytes=10)


# ------------------------------------------------------------------ nothing sensitive in exception text


@pytest.mark.asyncio
async def test_no_url_id_filename_or_caption_in_any_exception(meta):
    api = meta.client()
    media_id = f"{SENTINEL}-0000"
    caption = f"{SENTINEL} caption " * 100
    filename = f"{SENTINEL}.gif"
    errors: list[BaseException] = []

    async def collect(coro):
        try:
            await coro
        except (c.AgentPlatformError, ValueError) as exc:
            errors.append(exc)
        else:
            raise AssertionError("expected an error")

    await collect(api.upload_media(b"GIF89a", mime="image/gif", filename=filename))  # 131053
    meta.queue("media_upload", httpx.ReadTimeout(f"timed out {SENTINEL}"))
    await collect(api.upload_media(b"x", mime="text/plain", filename=filename))
    await collect(api.get_media(media_id))  # 400/100
    await collect(api.get_media(f"../{SENTINEL}"))  # ValueError
    await collect(api.delete_media(media_id))
    await collect(api.send_media(CREATOR, "image", media_id, caption=caption))  # 131009 quoting the id
    jpeg = meta.add_media(sample_jpeg(), "image/jpeg")
    await collect(api.send_media(CREATOR, "image", jpeg.id, caption=caption))  # 400/100 caption
    doc = meta.add_media(b"%PDF", "application/pdf", media_id=media_id)
    lookaside = f"https://{LOOKASIDE_HOST}/agent/v1/media/{media_id}/content?hash={SENTINEL}"
    for mode in ("redirect", "not_found", "bad_bytes", "timeout", "timeout_mid_stream", "wrong_length"):
        meta.fault_download(doc.id, mode)
        info = c.MediaInfo(id=media_id, url=lookaside, mime_type=None, sha256=sha256_hex(b"%PDF"), file_size=4)
        await collect(api.download_media(info, max_bytes=10**6))
    meta.queue("download", download_unauthorized_response(), httpx.ConnectError(f"no route {SENTINEL}"))
    await collect(api.download_media(lookaside_info(url=lookaside), max_bytes=10**6))
    await collect(api.download_media(lookaside_info(url=lookaside), max_bytes=10**6))
    await collect(api.download_media(lookaside_info(url=f"https://{EVIL_HOST}/{SENTINEL}"), max_bytes=10))

    assert len(errors) == 16
    for exc in errors:
        assert_clean(exc, SENTINEL)
