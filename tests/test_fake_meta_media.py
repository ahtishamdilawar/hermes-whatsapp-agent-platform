"""The fake's own media behaviour, checked against the live P0 captures, so later phases can trust it."""

from __future__ import annotations

import base64
import hashlib
import io
import re
import time

import httpx
import pytest
import pytest_asyncio
from wap_helpers import (
    API_KEY,
    CREATOR,
    EVIL_HOST,
    IMAGE_MAX_BYTES,
    LOOKASIDE_HOST,
    MEDIA_MAX_BYTES,
    UPLOAD_MIME_RULES,
    error_response,
    fixture_json,
    multipart_fields,
    rate_limited_response,
    reaction_message,
    sample_gif,
    sample_jpeg,
    sample_mp3,
    sample_mp4,
    sample_ogg_opus,
    sample_pdf,
    sample_png,
    sample_webp,
    text_message,
    updates_response,
)
from wap_plugin_under_test import client as c

API = "https://api.whatsapp.com/agent/v1"
AUTH = {"Authorization": f"Bearer {API_KEY}"}
LIVE = fixture_json("live_media_errors.json")


def error_fields(response: httpx.Response) -> dict:
    """The fields the live probe recorded, in the same shape as ``live_media_errors.json``."""
    err = response.json()["error"]
    out = {"http": response.status_code, "code": err["code"], "type": err["type"], "message": err["message"]}
    if "error_data" in err:
        out["error_data"] = err["error_data"]
    return out


async def upload(http, data: bytes, mime: str | None, *, filename="file.bin", part_type=None, product="whatsapp"):
    form = {}
    if product is not None:
        form["messaging_product"] = product
    if mime is not None:
        form["type"] = mime
    files = {"file": (filename, data, part_type)} if data is not None else None
    if files is None:
        return await http.post(f"{API}/media", headers=AUTH, data=form, files={"x": ("x", b"")})
    return await http.post(f"{API}/media", headers=AUTH, data=form, files=files)


async def uploaded_id(http, data: bytes, mime: str, **kw) -> str:
    response = await upload(http, data, mime, **kw)
    assert response.status_code == 200, response.text
    return response.json()["id"]


async def send(http, msg_type: str, obj: dict, **extra) -> httpx.Response:
    body = {"messaging_product": "whatsapp", "to": CREATOR, "type": msg_type, msg_type: obj, **extra}
    return await http.post(f"{API}/messages", headers=AUTH, json=body)


async def download(http, url: str, *, auth: bool = True) -> httpx.Response:
    return await http.get(url, headers=AUTH if auth else None)


@pytest_asyncio.fixture
async def http(meta):
    async with meta.http_client() as client:
        yield client


# ------------------------------------------------------------------ routing and back-compat


@pytest.mark.asyncio
async def test_routes_by_method_and_path_and_keeps_text_behaviour(meta, http):
    wamid = await meta.client().send_text(CREATOR, "hi")
    media_id = await uploaded_id(http, sample_jpeg(), "image/jpeg")
    await http.get(f"{API}/media/{media_id}", headers=AUTH)
    await http.delete(f"{API}/media/{media_id}", headers=AUTH)
    await http.get(f"https://{LOOKASIDE_HOST}/agent/v1/media/{media_id}/content", headers=AUTH)
    assert wamid == "wamid.out1"
    assert [name for name, _ in meta.routed] == ["messages", "media_upload", "media_get", "media_delete", "download"]
    assert len(meta.calls("media_download")) == 1  # alias
    assert meta.bodies("messages")[0]["text"] == {"body": "hi"}
    assert meta.bodies("media_upload") == []  # multipart is not JSON; see meta.uploads


@pytest.mark.asyncio
async def test_queued_responses_win_over_media_validation(meta, http):
    meta.queue("messages", rate_limited_response(7))
    response = await send(http, "image", {"id": "does-not-exist"})
    assert response.status_code == 429 and response.headers["Retry-After"] == "7"
    assert (await send(http, "image", {"id": "does-not-exist"})).json()["error"]["code"] == 131009


@pytest.mark.parametrize(
    ("endpoint", "request_fn"),
    [
        ("media_upload", lambda http: upload(http, b"x", "text/plain")),
        ("media_get", lambda http: http.get(f"{API}/media/abc", headers=AUTH)),
        ("media_delete", lambda http: http.delete(f"{API}/media/abc", headers=AUTH)),
        ("download", lambda http: http.get(f"https://{LOOKASIDE_HOST}/agent/v1/media/abc/content", headers=AUTH)),
    ],
)
@pytest.mark.asyncio
async def test_queue_injects_rate_limits_errors_and_exceptions_on_media_endpoints(meta, http, endpoint, request_fn):
    meta.queue(endpoint, rate_limited_response(3), error_response(503, 131016), httpx.ConnectError("boom"))
    first = await request_fn(http)
    assert first.status_code == 429 and first.headers["Retry-After"] == "3"
    assert (await request_fn(http)).status_code == 503
    with pytest.raises(httpx.ConnectError):
        await request_fn(http)


@pytest.mark.asyncio
async def test_client_error_taxonomy_sees_fake_errors_through_real_classifier(meta, http):
    response = await http.get(f"{API}/media/nope", headers=AUTH)
    err = c.classify_response(response, "media_get")
    assert isinstance(err, c.NotSent) and err.code == 100 and err.status == 400


# ------------------------------------------------------------------ upload


@pytest.mark.asyncio
async def test_upload_parses_multipart_and_stores_bytes(meta, http):
    tricky = b"--\r\n\r\n" + bytes(range(256)) * 40 + b"\r\n--boundary\r\n\n\r"
    response = await upload(http, tricky, "application/octet-stream", filename="résumé v2.bin")
    media_id = response.json()["id"]
    assert response.status_code == 200 and re.fullmatch(r"[0-9a-f-]{36}", media_id)
    [record] = meta.uploads
    assert record.messaging_product == "whatsapp" and record.type == "application/octet-stream"
    assert record.filename == "résumé v2.bin" and record.data == tricky and record.media_id == media_id
    assert meta.media[media_id].data == tricky
    fields = multipart_fields(meta.calls("media_upload")[0])
    assert set(fields) == {"messaging_product", "type", "file"} and fields["type"].content_type is None


@pytest.mark.asyncio
async def test_upload_without_type_uses_part_content_type(meta, http):
    media_id = await uploaded_id(http, sample_png(), None, filename="a.png", part_type="image/png")
    assert meta.media[media_id].mime_type == "image/png"
    assert meta.uploads[0].type is None and meta.uploads[0].content_type == "image/png"


@pytest.mark.asyncio
async def test_upload_opus_is_stored_as_ogg_with_codec_parameter(meta, http):
    media_id = await uploaded_id(http, sample_ogg_opus(), "audio/opus")
    assert meta.media[media_id].mime_type == "audio/ogg; codecs=opus"


@pytest.mark.parametrize("mime", sorted(UPLOAD_MIME_RULES))
@pytest.mark.asyncio
async def test_every_allowlisted_mime_uploads(meta, http, mime):
    assert (await upload(http, b"abc", mime)).status_code == 200


@pytest.mark.parametrize(
    ("label", "mime"),
    [
        ("gif_static", "image/gif"),
        ("text_csv", "text/csv"),
        ("text_markdown", "text/markdown"),
        ("application_zip", "application/zip"),
        ("wav", "audio/wav"),
        ("webm", "video/webm"),
    ],
)
@pytest.mark.asyncio
async def test_rejected_mime_matches_live_body(meta, http, label, mime):
    response = await upload(http, b"data", mime)
    assert error_fields(response) == LIVE[f"upload:{label}"]
    assert meta.uploads[0].media_id is None and meta.media == {}


@pytest.mark.asyncio
async def test_image_limit_is_binary_and_matches_live_body(meta, http):
    assert (await upload(http, b"\xff" * IMAGE_MAX_BYTES, "image/jpeg")).status_code == 200
    response = await upload(http, b"\xff" * (IMAGE_MAX_BYTES + 1), "image/jpeg")
    assert error_fields(response) == LIVE["upload:jpeg_5242881B"]


@pytest.mark.asyncio
async def test_sticker_limit_and_generic_16_mib_limit(meta, http):
    assert (await upload(http, b"R" * 500_000, "image/webp")).status_code == 200
    assert error_fields(await upload(http, b"R" * 500_001, "image/webp")) == LIVE["upload:webp_500001B"]
    assert (await upload(http, b"\0" * MEDIA_MAX_BYTES, "application/octet-stream")).status_code == 200
    over = await upload(http, b"\0" * (MEDIA_MAX_BYTES + 1), "application/octet-stream")
    assert over.json()["error"]["code"] == 131053
    assert over.json()["error"]["error_data"]["details"] == (
        f"Media is {MEDIA_MAX_BYTES + 1} bytes, over the {MEDIA_MAX_BYTES} byte limit for application/octet-stream"
    )


@pytest.mark.asyncio
async def test_upload_missing_fields(meta, http):
    no_product = await upload(http, b"abc", "text/plain", product=None)
    no_file = await upload(http, None, "text/plain")
    no_type = await upload(http, b"abc", None, part_type="")
    assert (no_product.status_code, no_product.json()["error"]["code"]) == (400, 131009)
    assert (no_file.status_code, no_file.json()["error"]["code"]) == (400, 131009)
    assert (no_type.status_code, no_type.json()["error"]["code"]) == (400, 131053)


# ------------------------------------------------------------------ GET / DELETE / download


@pytest.mark.asyncio
async def test_get_returns_live_shape_and_download_serves_matching_bytes(meta, http):
    data = sample_jpeg()
    media_id = await uploaded_id(http, data, "image/jpeg")
    meta_get = await http.get(f"{API}/media/{media_id}", headers=AUTH)
    body = meta_get.json()
    assert sorted(body) == ["file_size", "id", "messaging_product", "mime_type", "sha256", "url"]
    assert (body["id"], body["mime_type"], body["file_size"]) == (media_id, "image/jpeg", len(data))
    assert body["sha256"] == hashlib.sha256(data).hexdigest()
    url = httpx.URL(body["url"])
    assert url.scheme == "https" and url.host == LOOKASIDE_HOST and url.port is None
    assert url.path.startswith("/agent") and sorted(url.params) == ["ext", "hash"] and url.params["ext"] == "jpg"
    got = await download(http, body["url"])
    assert got.status_code == 200 and got.content == data
    assert got.headers["content-type"] == "image/jpeg" and got.headers["content-length"] == str(len(data))
    assert "content-encoding" not in got.headers


@pytest.mark.asyncio
async def test_text_plain_downloads_with_charset(meta, http):
    media_id = await uploaded_id(http, b"# notes\n", "text/plain")
    got = await download(http, meta.media_url(media_id))
    assert got.headers["content-type"] == "text/plain;charset=utf-8"


@pytest.mark.asyncio
async def test_download_without_bearer_is_401_190_and_is_recorded(meta, http):
    media_id = await uploaded_id(http, sample_mp3(), "audio/mpeg")
    response = await download(http, meta.media_url(media_id), auth=False)
    assert error_fields(response) == LIVE["download_no_auth:mp3"]
    assert meta.requests_to(LOOKASIDE_HOST)[-1].authorized is False


@pytest.mark.asyncio
async def test_delete_then_everything_reports_gone_like_live(meta, http):
    media_id = await uploaded_id(http, sample_pdf(), "application/pdf")
    url = meta.media_url(media_id)
    first = await http.delete(f"{API}/media/{media_id}", headers=AUTH)
    assert first.status_code == 200 and first.json() == fixture_json("media_delete_response.json")
    assert error_fields(await http.get(f"{API}/media/{media_id}", headers=AUTH)) == LIVE["get_after_delete"]
    assert error_fields(await download(http, url)) == LIVE["download_after_delete"]
    assert error_fields(await http.delete(f"{API}/media/{media_id}", headers=AUTH)) == LIVE["delete_2"]
    sent = await send(http, "document", {"id": media_id, "filename": "a.pdf"})
    assert error_fields(sent)["error_data"]["details"] == f"No media found for id {media_id}"


@pytest.mark.asyncio
async def test_unknown_and_malformed_ids(meta, http):
    for media_id in ("0000000000000000", "not..valid", "%2e%2e"):
        response = await http.get(f"{API}/media/{media_id}", headers=AUTH)
        assert error_fields(response) == LIVE["get_malformed_id"]
    assert (await download(http, f"https://{LOOKASIDE_HOST}/agent/v1/media/nope/content")).status_code == 404


# ------------------------------------------------------------------ download faults


@pytest.fixture
def stored(meta):
    return meta.add_media(bytes(range(256)) * 1000, "application/pdf", filename="a.pdf")  # 256,000 B


@pytest.mark.asyncio
async def test_fault_redirect_points_off_host_and_is_not_followed_by_default(meta, http, stored):
    meta.fault_download(stored.id, "redirect")
    response = await download(http, meta.media_url(stored.id))
    assert response.status_code == 302 and httpx.URL(response.headers["location"]).host == EVIL_HOST
    assert meta.hosts() == {LOOKASIDE_HOST}


@pytest.mark.asyncio
async def test_fault_other_host_serves_bytes_and_records_key_leaks(meta, http, stored):
    meta.fault_download(stored.id, "other_host")
    url = (await http.get(f"{API}/media/{stored.id}", headers=AUTH)).json()["url"]
    assert httpx.URL(url).host == EVIL_HOST
    assert meta.authorized_hosts() == {"api.whatsapp.com"}
    leaked = await download(http, url)  # a broken client would do this
    assert leaked.status_code == 200 and leaked.content == stored.data
    assert EVIL_HOST in meta.authorized_hosts()
    meta.fault_download(stored.id, "other_host", url="http://lookaside.fbsbx.com:8443/agent/v1/media/{id}/content")
    url = (await http.get(f"{API}/media/{stored.id}", headers=AUTH)).json()["url"]
    assert url == f"http://lookaside.fbsbx.com:8443/agent/v1/media/{stored.id}/content"


@pytest.mark.asyncio
async def test_fault_oversize_body_exceeds_file_size(meta, http, stored):
    meta.fault_download(stored.id, "oversize", extra_bytes=10)
    response = await download(http, meta.media_url(stored.id))
    assert len(response.content) == stored.file_size + 10 == int(response.headers["content-length"])


@pytest.mark.asyncio
async def test_fault_no_length_streams_without_content_length(meta, http, stored):
    meta.fault_download(stored.id, "no_length", extra_bytes=5)
    async with http.stream("GET", meta.media_url(stored.id), headers=AUTH) as response:
        assert "content-length" not in response.headers
        chunks = [chunk async for chunk in response.aiter_raw()]
    assert len(chunks) > 1 and len(b"".join(chunks)) == stored.file_size + 5


@pytest.mark.asyncio
async def test_fault_wrong_length_header_disagrees_with_body(meta, http, stored):
    meta.fault_download(stored.id, "wrong_length")
    response = await download(http, meta.media_url(stored.id))
    assert int(response.headers["content-length"]) == stored.file_size // 2 and response.content == stored.data
    meta.fault_download(stored.id, "wrong_length", declared_length=stored.file_size + 100)
    response = await download(http, meta.media_url(stored.id))
    assert int(response.headers["content-length"]) == stored.file_size + 100 and len(response.content) < 256_100


@pytest.mark.asyncio
async def test_fault_bad_bytes_keeps_length_but_breaks_the_hash(meta, http, stored):
    meta.fault_download(stored.id, "bad_bytes")
    response = await download(http, meta.media_url(stored.id))
    assert len(response.content) == stored.file_size
    assert hashlib.sha256(response.content).hexdigest() != stored.sha256_hex


@pytest.mark.asyncio
async def test_fault_timeouts(meta, http, stored):
    meta.fault_download(stored.id, "timeout")
    with pytest.raises(httpx.ReadTimeout):
        await download(http, meta.media_url(stored.id))
    meta.fault_download(stored.id, "timeout_mid_stream")
    received = []
    with pytest.raises(httpx.ReadTimeout):
        async with http.stream("GET", meta.media_url(stored.id), headers=AUTH) as response:
            async for chunk in response.aiter_raw():
                received.append(chunk)
    assert received and sum(map(len, received)) < stored.file_size


@pytest.mark.asyncio
async def test_fault_slow_delays_each_chunk(meta, http, stored):
    meta.fault_download(stored.id, "slow", delay=0.01)
    started = time.monotonic()
    response = await download(http, meta.media_url(stored.id))
    assert response.content == stored.data and time.monotonic() - started >= 0.03  # 4 chunks of 64 KiB


@pytest.mark.asyncio
async def test_fault_status_and_not_found(meta, http, stored):
    meta.fault_download(stored.id, "status", status=503)
    assert (await download(http, meta.media_url(stored.id))).status_code == 503
    meta.fault_download(stored.id, "status", status=429, retry_after=4)
    limited = await download(http, meta.media_url(stored.id))
    assert limited.status_code == 429 and limited.headers["Retry-After"] == "4"
    meta.fault_download(stored.id, "not_found")
    assert error_fields(await download(http, meta.media_url(stored.id))) == LIVE["download_after_delete"]
    with pytest.raises(ValueError):
        meta.fault_download(stored.id, "nonsense")


@pytest.mark.asyncio
async def test_base_url_override_host_serves_media_like_meta(meta):
    client = meta.client(base_url="https://proxy.internal/agent/v1")
    assert "proxy.internal" in meta.trusted_download_hosts
    await client.aclose()


# ------------------------------------------------------------------ /messages with media


@pytest.mark.asyncio
async def test_media_send_is_recorded(meta, http):
    media_id = await uploaded_id(http, sample_jpeg(), "image/jpeg")
    response = await send(http, "image", {"id": media_id, "caption": "hi"}, context={"message_id": "wamid.in1"})
    assert response.status_code == 200
    [sent] = meta.sent_media
    assert (sent.wamid, sent.type, sent.media_id, sent.caption) == (
        response.json()["messages"][0]["id"],
        "image",
        media_id,
        "hi",
    )
    assert sent.context == {"message_id": "wamid.in1"} and sent.media.data == sample_jpeg()


@pytest.mark.parametrize(
    ("label", "mime", "data", "msg_type"),
    [
        ("webp_as_image", "image/webp", sample_webp(), "image"),
        ("audio_as_image_mismatch", "audio/mpeg", sample_mp3(), "image"),
    ],
)
@pytest.mark.asyncio
async def test_type_mismatch_matches_live(meta, http, label, mime, data, msg_type):
    media_id = await uploaded_id(http, data, mime)
    assert error_fields(await send(http, msg_type, {"id": media_id})) == LIVE[f"send:{label}"]
    assert meta.sent_media == []


@pytest.mark.parametrize(
    ("mime", "data", "msg_type"),
    [
        ("image/webp", sample_webp(), "sticker"),
        ("image/jpeg", sample_jpeg(), "document"),
        ("text/plain", b"notes", "document"),
        ("application/octet-stream", b"PK\x03\x04", "document"),
        ("audio/ogg", sample_ogg_opus(), "audio"),
        ("audio/mpeg", sample_mp3(), "audio"),
        ("video/mp4", sample_mp4(), "video"),
        ("image/png", sample_png(mode="P"), "image"),
    ],
)
@pytest.mark.asyncio
async def test_compatible_types_send(meta, http, mime, data, msg_type):
    media_id = await uploaded_id(http, data, mime)
    assert (await send(http, msg_type, {"id": media_id})).status_code == 200


@pytest.mark.asyncio
async def test_unknown_media_id_on_send_matches_live(meta, http):
    assert error_fields(await send(http, "image", {"id": "0000000000000000"})) == LIVE["send:bogus_media_id"]


@pytest.mark.asyncio
async def test_caption_limit_counts_utf16_units_like_live(meta, http):
    media_id = await uploaded_id(http, sample_jpeg(), "image/jpeg")
    assert (
        error_fields(await send(http, "image", {"id": media_id, "caption": "x" * 1025}))
        == LIVE["send:image_caption_1025"]
    )
    assert (await send(http, "image", {"id": media_id, "caption": "\U0001f600" * 512})).status_code == 200
    too_long = await send(http, "image", {"id": media_id, "caption": "\U0001f600" * 512 + "x"})
    assert too_long.status_code == 400 and too_long.json()["error"]["message"] == "caption"


@pytest.mark.asyncio
async def test_misplaced_fields_missing_ids_and_reactions_are_rejected(meta, http):
    audio_id = await uploaded_id(http, sample_mp3(), "audio/mpeg")
    image_id = await uploaded_id(http, sample_jpeg(), "image/jpeg")
    for response in (
        await send(http, "audio", {"id": audio_id, "caption": "no"}),
        await send(http, "image", {"id": image_id, "filename": "a.jpg"}),
        await send(http, "image", {"caption": "no id"}),
        await http.post(f"{API}/messages", headers=AUTH, json={"to": CREATOR, "type": "image"}),
        await send(http, "reaction", {"message_id": "wamid.x", "emoji": "\U0001f44d"}),
    ):
        assert (response.status_code, response.json()["error"]["code"]) == (400, 131009)
    assert meta.sent_media == []


# ------------------------------------------------------------------ inbound builders


def _b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode()


@pytest.mark.asyncio
async def test_inbound_builders_match_live_wire_shapes(meta, http):
    cases = {
        "photo": (meta.inbound_image("wamid.1"), "image", ["id", "mime_type", "sha256"], "image/jpeg"),
        "photo_caption": (
            meta.inbound_image("wamid.2", caption="hi"),
            "image",
            ["caption", "id", "mime_type", "sha256"],
            "image/jpeg",
        ),
        "voice": (
            meta.inbound_voice("wamid.3"),
            "audio",
            ["id", "mime_type", "sha256", "voice"],
            "audio/ogg; codecs=opus",
        ),
        "audio": (meta.inbound_audio("wamid.4"), "audio", ["id", "mime_type", "sha256"], "audio/mpeg"),
        "pdf": (
            meta.inbound_document("wamid.5"),
            "document",
            ["filename", "id", "mime_type", "sha256"],
            "application/pdf",
        ),
        "photo_doc": (
            meta.inbound_photo_document("wamid.6"),
            "document",
            ["filename", "id", "mime_type", "sha256"],
            "image/png",
        ),
        "sticker": (
            meta.inbound_sticker("wamid.7"),
            "sticker",
            ["animated", "id", "mime_type", "sha256"],
            "image/webp",
        ),
        "video": (
            meta.inbound_video("wamid.8", caption="v"),
            "video",
            ["caption", "id", "mime_type", "sha256"],
            "video/mp4",
        ),
    }
    for name, (msg, kind, obj_keys, mime) in cases.items():
        assert sorted(msg) == sorted(["from", "id", "timestamp", "type", kind]), name
        assert msg["type"] == kind and msg["from"] == CREATOR and msg["timestamp"].isdigit()
        obj = msg[kind]
        assert sorted(obj) == obj_keys and obj["mime_type"] == mime, name
        assert len(obj["sha256"]) == 44
        meta_get = (await http.get(f"{API}/media/{obj['id']}", headers=AUTH)).json()
        body = (await download(http, meta_get["url"])).content
        assert obj["sha256"] == _b64(body) and meta_get["sha256"] == hashlib.sha256(body).hexdigest(), name
        assert meta_get["file_size"] == len(body) and meta_get["mime_type"] == mime
    assert cases["voice"][0]["audio"]["voice"] is True
    assert cases["sticker"][0]["sticker"]["animated"] is False
    assert cases["photo_doc"][0]["document"]["filename"].endswith(".png")


@pytest.mark.asyncio
async def test_inbound_oversize_document_reports_and_streams_full_size(meta, http):
    msg = meta.inbound_oversize_document("wamid.big", file_size=3 * 1024 * 1024 + 7)
    doc = msg["document"]
    assert doc["mime_type"] == "video/quicktime" and doc["filename"] == "IMG_0001.MOV"
    info = (await http.get(f"{API}/media/{doc['id']}", headers=AUTH)).json()
    assert info["file_size"] == 3 * 1024 * 1024 + 7
    body = (await download(http, info["url"])).content
    assert len(body) == info["file_size"] and doc["sha256"] == _b64(body)
    default = meta.inbound_oversize_document("wamid.big2")
    assert meta.media[default["document"]["id"]].file_size == 23_096_834


def test_inbound_options_context_sha_and_explicit_ids(meta):
    msg = meta.inbound_image(
        "wamid.q",
        sha256=None,
        media_id="11111111-2222-3333-4444-555555555555",
        context={"id": "wamid.a", "from": CREATOR},
    )
    assert "sha256" not in msg["image"] and msg["image"]["id"] == "11111111-2222-3333-4444-555555555555"
    assert msg["context"] == {"id": "wamid.a", "from": CREATOR}
    assert meta.inbound_image("wamid.r", sha256="bogus")["image"]["sha256"] == "bogus"


def test_reaction_builder_live_shape():
    msg = reaction_message("wamid.r1", "wamid.out1")
    assert sorted(msg) == ["from", "id", "reaction", "timestamp", "type"] and msg["type"] == "reaction"
    assert msg["reaction"] == {"message_id": "wamid.out1", "emoji": "\U0001f602"}
    assert reaction_message("wamid.r2", "wamid.out1", "")["reaction"]["emoji"] == ""
    assert "emoji" not in reaction_message("wamid.r3", "wamid.out1", None)["reaction"]


def test_builders_parse_through_the_real_updates_parser(meta):
    page = c.parse_updates(
        updates_response(
            meta.inbound_image("wamid.1", caption="x"),
            reaction_message("wamid.2", "wamid.1"),
            text_message("wamid.3", "hi"),
            next_offset=5,
        )
    )
    assert [m["type"] for m in page.messages] == ["image", "reaction", "text"] and page.next_offset == 5


# ------------------------------------------------------------------ manual fixtures and samples


def test_manual_media_fixtures():
    image = c.parse_updates(httpx.Response(200, json=fixture_json("updates_image.json"))).messages[0]
    voice = c.parse_updates(httpx.Response(200, json=fixture_json("updates_voice.json"))).messages[0]
    assert image["image"]["caption"] == "look at this" and len(image["image"]["sha256"]) == 44
    assert voice["audio"]["voice"] is True and voice["audio"]["mime_type"] == "audio/ogg"
    get = fixture_json("media_get_response.json")
    assert httpx.URL(get["url"]).host == LOOKASIDE_HOST and len(get["sha256"]) == 64
    assert fixture_json("media_upload_response.json") == {"id": "<MEDIA_ID>"}
    assert fixture_json("send_image_request.json")["image"] == {"id": "<MEDIA_ID>", "caption": "Check this out!"}


def test_sample_media_bytes():
    from PIL import Image

    for data, fmt in ((sample_jpeg(), "JPEG"), (sample_png(), "PNG"), (sample_webp(), "WEBP"), (sample_gif(), "GIF")):
        assert Image.open(io.BytesIO(data)).format == fmt
    assert getattr(Image.open(io.BytesIO(sample_gif(animated=True))), "n_frames", 1) == 2
    assert Image.open(io.BytesIO(sample_png(mode="P"))).mode == "P"
    assert Image.open(io.BytesIO(sample_png(mode="I;16"))).mode.startswith("I")
    assert sample_ogg_opus().startswith(b"OggS") and b"OpusHead" in sample_ogg_opus()
    assert sample_mp3().startswith(b"ID3") and sample_mp4()[4:8] == b"ftyp" and sample_pdf().startswith(b"%PDF")


# ------------------------------------------------------------------ the conftest ``meta`` fixture


@pytest.mark.asyncio
async def test_meta_fixture_passes_client_kwargs_through(meta, plugin):
    client = plugin.adapter.AgentPlatformClient(
        API_KEY, base_url="https://proxy.internal/agent/v1", send_timeout=5.0, http=object()
    )
    assert isinstance(client, c.AgentPlatformClient) and client._send_timeout == 5.0
    assert "proxy.internal" in meta.trusted_download_hosts
    assert all(window.limit == 10**6 for window in client.limits.values())  # unthrottled
    await client.send_text(CREATOR, "via proxy")
    assert meta.calls("messages")[0].url.host == "proxy.internal"
    await client.aclose()
