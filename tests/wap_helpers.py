"""Shared test helpers: load the repo root as a plugin package and fake Meta's API.

``FakeMeta`` reproduces the live ``/agent/v1`` behaviour recorded by the P0 probes (2026-09-29/30, see
``notes/media-research/10-live-probe-results.md`` in the workspace): the error codes, messages and
``error_data.details`` strings below are copied from those captures unless a comment says otherwise.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import email.parser
import email.policy
import email.utils
import functools
import hashlib
import importlib.util
import io
import json
import re
import sys
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
PKG = "wap_plugin_under_test"
API_KEY = "WAAVtest-key-0123456789abcdefghijklmnopqrstuvwxyz"
CREATOR = "user:50972923564215"

LOOKASIDE_HOST = "lookaside.fbsbx.com"
EVIL_HOST = "evil.example"

# Upload limits are binary (P0: 5,242,880 B accepted, 5,242,881 B rejected). The sticker limit could not be
# probed (the padded WebP was invalid), so the plan's conservative 500,000 B is used.
MIB = 1024 * 1024
IMAGE_MAX_BYTES = 5 * MIB
STICKER_MAX_BYTES = 500_000
MEDIA_MAX_BYTES = 16 * MIB
MAX_CAPTION_UTF16 = 1024

MEDIA_MESSAGE_TYPES = ("image", "audio", "video", "document", "sticker")
CAPTION_TYPES = ("image", "video", "document")

_OFFICE = {
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-powerpoint": "ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
}


@dataclass(frozen=True)
class MimeRule:
    limit: int
    message_types: frozenset[str]  # message types this MIME may be sent as, besides "document"
    ext: str


# Upload allowlist: the manual's table plus application/octet-stream; confirmed live for jpeg, png, webp,
# text/plain, octet-stream, pdf, audio/ogg, audio/mpeg, audio/mp4 and video/mp4. Everything else is 131053.
UPLOAD_MIME_RULES: dict[str, MimeRule] = {
    "image/jpeg": MimeRule(IMAGE_MAX_BYTES, frozenset({"image"}), "jpg"),
    "image/png": MimeRule(IMAGE_MAX_BYTES, frozenset({"image"}), "png"),
    "image/webp": MimeRule(STICKER_MAX_BYTES, frozenset({"sticker"}), "webp"),
    "video/mp4": MimeRule(MEDIA_MAX_BYTES, frozenset({"video"}), "mp4"),
    "video/3gpp": MimeRule(MEDIA_MAX_BYTES, frozenset({"video"}), "3gp"),
    "audio/aac": MimeRule(MEDIA_MAX_BYTES, frozenset({"audio"}), "aac"),
    "audio/mp4": MimeRule(MEDIA_MAX_BYTES, frozenset({"audio"}), "m4a"),
    "audio/mpeg": MimeRule(MEDIA_MAX_BYTES, frozenset({"audio"}), "mp3"),
    "audio/amr": MimeRule(MEDIA_MAX_BYTES, frozenset({"audio"}), "amr"),
    "audio/ogg": MimeRule(MEDIA_MAX_BYTES, frozenset({"audio"}), "ogg"),
    "audio/opus": MimeRule(MEDIA_MAX_BYTES, frozenset({"audio"}), "ogg"),
    "application/pdf": MimeRule(MEDIA_MAX_BYTES, frozenset(), "pdf"),
    "text/plain": MimeRule(MEDIA_MAX_BYTES, frozenset(), "txt"),
    "application/octet-stream": MimeRule(MEDIA_MAX_BYTES, frozenset(), "bin"),
    **{mime: MimeRule(MEDIA_MAX_BYTES, frozenset(), ext) for mime, ext in _OFFICE.items()},
}

# Inbound-only MIME types the fake may store (users can send them; uploads of them are rejected).
_INBOUND_EXT = {"video/quicktime": "mov", "image/gif": "gif"}


def load_plugin() -> Any:
    """Import the repo root exactly as Hermes does: a package with relative imports."""
    if PKG in sys.modules:
        return sys.modules[PKG]
    spec = importlib.util.spec_from_file_location(PKG, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[PKG] = module
    spec.loader.exec_module(module)
    return module


def fixture_json(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


async def until(predicate, timeout: float = 3.0) -> None:
    """Wait for a positive condition (never "sleep, then assert nothing happened")."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def base_mime(mime: str | None) -> str:
    """``"audio/ogg; codecs=opus"`` -> ``"audio/ogg"`` (lower-cased, parameters dropped)."""
    return (mime or "").split(";", 1)[0].strip().lower()


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")


# --------------------------------------------------------------------------- error bodies


def meta_error(
    status: int,
    code: int,
    message: str,
    details: str | None = None,
    *,
    headers: dict | None = None,
    fbtrace_id: str = "AWtrace",
) -> httpx.Response:
    """A Meta error envelope. ``details=None`` omits ``error_data`` (as live 401/190 and caption errors do)."""
    error: dict[str, Any] = {"message": message, "type": "OAuthException", "code": code}
    if details is not None:
        error["error_data"] = {"messaging_product": "whatsapp", "details": details}
    error["fbtrace_id"] = fbtrace_id
    return httpx.Response(status, headers=headers, json={"error": error})


def error_response(status: int, code: int, headers: dict | None = None) -> httpx.Response:
    """Generic error for injection via ``queue()``; use the specific builders below for live shapes."""
    return meta_error(status, code, f"(#{code}) test", "test", headers=headers)


def rate_limited_response(retry_after: float | None = None) -> httpx.Response:
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
    return meta_error(429, 130429, "(#130429) Rate limit hit", "Rate limit hit", headers=headers)


def media_not_found_response(status: int = 400) -> httpx.Response:
    """Live: GET/DELETE of an unknown, deleted or malformed id (400), and a download of a deleted one (404)."""
    return meta_error(status, 100, "(#100) No media found for the provided id", "No media found for this id.")


def unsupported_mime_response(mime: str) -> httpx.Response:
    """Live: upload with a MIME type outside the allowlist."""
    return meta_error(
        400,
        131053,
        "(#131053) The declared media type is not one WhatsApp can render",
        f"{mime} is not a supported media type",
    )


def media_too_large_response(size: int, limit: int, mime: str) -> httpx.Response:
    """Live: upload over the per-MIME byte limit."""
    return meta_error(
        400,
        131053,
        "(#131053) Media exceeds the upload limit for its declared media type",
        f"Media is {size} bytes, over the {limit} byte limit for {mime}",
    )


def upload_not_accepted_response() -> httpx.Response:
    """Live: the answer to a 500,001-byte WebP upload."""
    return meta_error(400, 131053, "(#131053) Media upload failed", "The uploaded media was not accepted")


def no_declared_type_response() -> httpx.Response:
    """Manual: no ``type`` field and no part Content-Type -> 400/131053. Wording not observed live."""
    return meta_error(
        400,
        131053,
        "(#131053) The declared media type is not one WhatsApp can render",
        "No media type was declared",
    )


def upload_missing_field_response() -> httpx.Response:
    """Manual: 131009 when ``messaging_product`` or ``file`` is missing. Details wording from the manual."""
    return meta_error(
        400,
        131009,
        "(#131009) Missing or malformed required fields",
        "Invalid upload: messaging_product or file is missing.",
    )


def bad_field_response(details: str) -> httpx.Response:
    return meta_error(400, 131009, "(#131009) Missing or malformed required fields", details)


def no_media_found_send_response(media_id: str) -> httpx.Response:
    """Live: ``POST /messages`` naming an unknown or deleted media id."""
    return bad_field_response(f"No media found for id {media_id}")


def media_type_mismatch_response() -> httpx.Response:
    """Live: e.g. a WebP id sent as ``image``, or an MP3 id sent as ``image``."""
    return bad_field_response("Media of this content type cannot be sent as the requested message type")


def caption_too_long_response() -> httpx.Response:
    """Live: 1025-unit caption -> 400, code 100, message is the field name, no error_data."""
    return meta_error(400, 100, "caption")


def download_unauthorized_response() -> httpx.Response:
    """Live: a lookaside download without the bearer token -> 401/190 (118-byte JSON body)."""
    return meta_error(401, 190, "Authentication Error", fbtrace_id="AbCdEfGhIjKlMnOpQrStUvW")


# --------------------------------------------------------------------------- multipart


@dataclass
class FormPart:
    name: str
    filename: str | None
    content_type: str | None  # the part's own Content-Type header, None when absent
    data: bytes


def multipart_parts(request: httpx.Request) -> list[FormPart]:
    """Parse a ``multipart/form-data`` body with the stdlib ``email`` parser."""
    ctype = request.headers.get("content-type", "")
    if not ctype.lower().startswith("multipart/form-data"):
        raise ValueError(f"not multipart/form-data: {ctype!r}")
    message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        b"Content-Type: " + ctype.encode("latin-1") + b"\r\n\r\n" + request.content
    )
    parts = []
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        filename = part.get_param("filename", header="content-disposition")
        parts.append(
            FormPart(
                name=_header_text(name) or "",
                filename=_header_text(filename),
                content_type=part.get("Content-Type"),
                data=part.get_payload(decode=True) or b"",
            )
        )
    return parts


def multipart_fields(request: httpx.Request) -> dict[str, FormPart]:
    """``{field name: FormPart}`` (the last part wins for repeated names)."""
    return {part.name: part for part in multipart_parts(request)}


def _header_text(value: Any) -> str | None:
    """Undo the parser's surrogateescape of raw UTF-8 header bytes (httpx sends filenames as raw UTF-8)."""
    if value is None:
        return None
    if isinstance(value, tuple):  # RFC 2231 form: (charset, language, value)
        value = email.utils.collapse_rfc2231_value(value)
    text = str(value)
    try:
        return text.encode("utf-8", "surrogateescape").decode("utf-8")
    except UnicodeDecodeError:
        return text


# --------------------------------------------------------------------------- media store and records


_CHUNK = 64 * 1024


@dataclass
class StoredMedia:
    """A media object held by the fake, uploaded or "received from a user"."""

    id: str
    data: bytes
    mime_type: str  # as GET /media reports it; may carry parameters
    filename: str | None = None
    virtual_size: int | None = None  # serve ``data`` padded with zeros up to this size (huge inbound files)

    @property
    def file_size(self) -> int:
        return self.virtual_size if self.virtual_size is not None else len(self.data)

    def chunks(self, chunk_size: int = _CHUNK) -> Iterator[bytes]:
        for i in range(0, len(self.data), chunk_size):
            yield self.data[i : i + chunk_size]
        remaining = self.file_size - len(self.data)
        zeros = b"\0" * chunk_size
        while remaining > 0:
            n = min(chunk_size, remaining)
            yield zeros[:n]
            remaining -= n

    def body(self) -> bytes:
        return b"".join(self.chunks())

    @functools.cached_property
    def _digest(self) -> bytes:
        h = hashlib.sha256()
        for chunk in self.chunks(MIB):
            h.update(chunk)
        return h.digest()

    @property
    def sha256_hex(self) -> str:
        return self._digest.hex()

    @property
    def sha256_b64(self) -> str:
        return base64.b64encode(self._digest).decode("ascii")

    @property
    def ext(self) -> str:
        mime = base_mime(self.mime_type)
        rule = UPLOAD_MIME_RULES.get(mime)
        return rule.ext if rule else _INBOUND_EXT.get(mime, "bin")


@dataclass
class Upload:
    """One ``POST /media`` as the fake parsed it. ``media_id`` is None when it was rejected."""

    messaging_product: str | None
    type: str | None  # the ``type`` form field (a MIME type)
    filename: str | None
    content_type: str | None  # the file part's own Content-Type
    data: bytes | None
    media_id: str | None
    status: int


@dataclass
class SentMedia:
    """One accepted ``POST /messages`` with a media payload."""

    wamid: str
    to: str | None
    type: str
    media_id: str
    caption: str | None
    filename: str | None
    context: dict | None
    body: dict
    media: StoredMedia


@dataclass
class HostRequest:
    """Every request the fake saw, by host, and whether it carried an Authorization header."""

    host: str
    method: str
    path: str
    endpoint: str
    authorized: bool


@dataclass
class DownloadFault:
    """How the fake misbehaves for one media id. See ``FakeMeta.fault_download``."""

    mode: str
    status: int = 500
    location: str | None = None  # redirect target; default on EVIL_HOST
    url: str | None = None  # other_host: the url GET /media returns ({id} is filled in)
    extra_bytes: int = 0  # bytes served beyond file_size
    declared_length: int | None = None  # wrong_length: the Content-Length header value
    delay: float = 0.05  # slow: seconds before each chunk
    retry_after: float | None = None


DOWNLOAD_FAULT_MODES = frozenset(
    {
        "redirect",  # 302 with Location on another host
        "oversize",  # body larger than file_size (Content-Length matches the body)
        "no_length",  # chunked body without Content-Length
        "wrong_length",  # Content-Length disagrees with the body
        "bad_bytes",  # same length, one byte flipped: sha256 mismatch
        "timeout",  # httpx.ReadTimeout before any response
        "timeout_mid_stream",  # headers and a first chunk, then httpx.ReadTimeout
        "slow",  # sleeps ``delay`` before every chunk
        "status",  # an error status (default 500/2); 429 carries Retry-After when set
        "not_found",  # 404/100 as for deleted media
        "other_host",  # GET /media returns a url on another host (EVIL_HOST by default)
    }
)

_DOWNLOAD_RE = re.compile(r"/media/([^/]+)/content$")
_MEDIA_ID_RE = re.compile(r"/media/([^/]+)$")
_ALIASES = {"media_download": "download"}


class FakeMeta:
    """Scriptable stand-in for ``https://api.whatsapp.com/agent/v1`` and the ``lookaside.fbsbx.com`` media host.

    Requests are routed to endpoint names: ``updates``, ``messages``, ``statuses``, ``media_upload``
    (``POST /media``), ``media_get`` (``GET /media/<id>``), ``media_delete`` (``DELETE /media/<id>``) and
    ``download`` (``GET …/media/<id>/content`` on any host; alias ``media_download``).

    Queue responses per endpoint with ``queue(name, ...)``; each item is an ``httpx.Response``, an exception to
    raise, or a callable(request) -> Response. Queued items always win over the built-in behaviour. When a queue is
    empty, ``updates`` returns 204 and the others behave like the live API (see ``notes/media-research/10``).
    """

    def __init__(self) -> None:
        self.queues: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self.requests: list[httpx.Request] = []
        self.routed: list[tuple[str, httpx.Request]] = []
        self.host_log: list[HostRequest] = []
        self._wamid = 0
        # Like Meta: POST /statuses answers 403/131005 for a message not sent by the creator.
        self.not_creator_wamids: set[str] = set()
        # Media
        self.media: dict[str, StoredMedia] = {}
        self.deleted_media: set[str] = set()
        self.uploads: list[Upload] = []
        self.sent_media: list[SentMedia] = []
        self.download_faults: dict[str, DownloadFault] = {}
        self.download_host = LOOKASIDE_HOST  # host of the urls GET /media returns
        self.trusted_download_hosts: set[str] = {LOOKASIDE_HOST}  # hosts that behave like Meta's media host
        # Not documented either way; the fake rejects them so a client can't rely on them being ignored.
        self.reject_misplaced_media_fields = True

    # ------------------------------------------------------------------ inspection
    def calls(self, endpoint: str) -> list[httpx.Request]:
        endpoint = _ALIASES.get(endpoint, endpoint)
        return [request for name, request in self.routed if name == endpoint]

    def bodies(self, endpoint: str) -> list[dict]:
        """JSON bodies of an endpoint's requests (multipart uploads: see ``uploads`` / ``multipart_fields``)."""
        return [
            json.loads(r.content)
            for r in self.calls(endpoint)
            if r.content and "json" in r.headers.get("content-type", "")
        ]

    def hosts(self) -> set[str]:
        return {entry.host for entry in self.host_log}

    def authorized_hosts(self) -> set[str]:
        """Hosts that received an Authorization header: assert the key never left the allowlist."""
        return {entry.host for entry in self.host_log if entry.authorized}

    def requests_to(self, host: str) -> list[HostRequest]:
        return [entry for entry in self.host_log if entry.host == host]

    def queue(self, endpoint: str, *items: Any) -> None:
        self.queues[_ALIASES.get(endpoint, endpoint)].extend(items)

    # ------------------------------------------------------------------ media setup
    def add_media(
        self,
        data: bytes,
        mime_type: str,
        *,
        media_id: str | None = None,
        filename: str | None = None,
        virtual_size: int | None = None,
    ) -> StoredMedia:
        """Store bytes as Meta would hold them (an inbound attachment, or a pre-existing upload)."""
        media = StoredMedia(
            id=media_id or str(uuid.uuid4()),
            data=data,
            mime_type=mime_type,
            filename=filename,
            virtual_size=virtual_size,
        )
        self.media[media.id] = media
        self.deleted_media.discard(media.id)
        return media

    def expire_media(self, media_id: str) -> None:
        """Make an id behave as deleted/expired everywhere (GET 400/100, download 404/100, send 131009)."""
        self.media.pop(media_id, None)
        self.deleted_media.add(media_id)

    def fault_download(self, media_id: str, mode: str, **options: Any) -> DownloadFault:
        """Make the media host misbehave for ``media_id``. Modes: see ``DOWNLOAD_FAULT_MODES``.

        Options (``DownloadFault`` fields): ``status``, ``location``, ``url``, ``extra_bytes``, ``declared_length``,
        ``delay``, ``retry_after``. ``oversize`` defaults ``extra_bytes`` to 1024.
        """
        if mode not in DOWNLOAD_FAULT_MODES:
            raise ValueError(f"unknown download fault {mode!r}")
        if mode == "oversize":
            options.setdefault("extra_bytes", 1024)
        fault = DownloadFault(mode=mode, **options)
        self.download_faults[media_id] = fault
        return fault

    def media_url(self, media_id: str) -> str:
        """The url GET /media returns, shaped like the live one (``/agent…?ext=…&hash=…``)."""
        fault = self.download_faults.get(media_id)
        if fault is not None and fault.mode == "other_host":
            template = fault.url or f"https://{EVIL_HOST}/agent/v1/media/{{id}}/content?ext=bin&hash=x"
            return template.replace("{id}", quote(media_id, safe=""))
        media = self.media.get(media_id)
        ext = media.ext if media else "bin"
        token = base64.urlsafe_b64encode(hashlib.sha256(media_id.encode()).digest()[:26]).decode().rstrip("=")
        return f"https://{self.download_host}/agent/v1/media/{quote(media_id, safe='')}/content?ext={ext}&hash={token}"

    # ------------------------------------------------------------------ inbound builders
    def inbound_media(
        self,
        kind: str,
        wamid: str,
        data: bytes,
        mime_type: str,
        *,
        caption: str | None = None,
        filename: str | None = None,
        voice: bool = False,
        animated: bool | None = None,
        sha256: str | None | bool = True,
        media_id: str | None = None,
        virtual_size: int | None = None,
        sender: str = CREATOR,
        timestamp: int | None = None,
        context: dict | None = None,
    ) -> dict:
        """Store ``data`` and build an inbound ``messages[]`` item in the live wire shape.

        ``sha256=True`` puts the Base64 digest (as live), ``None``/``False`` omits the key, a string is used as is.
        ``caption``/``filename`` keys are present only when given; ``voice`` only when True (as live).
        """
        media = self.add_media(data, mime_type, media_id=media_id, filename=filename, virtual_size=virtual_size)
        obj: dict[str, Any] = {"id": media.id, "mime_type": mime_type}
        if sha256 is True:
            obj["sha256"] = media.sha256_b64
        elif isinstance(sha256, str):
            obj["sha256"] = sha256
        if caption is not None:
            obj["caption"] = caption
        if filename is not None:
            obj["filename"] = filename
        if voice:
            obj["voice"] = True
        if animated is not None:
            obj["animated"] = animated
        return media_message(wamid, kind, obj, sender=sender, timestamp=timestamp, context=context)

    def inbound_image(self, wamid: str, data: bytes | None = None, *, mime_type: str = "image/jpeg", **kw: Any) -> dict:
        return self.inbound_media("image", wamid, sample_jpeg() if data is None else data, mime_type, **kw)

    def inbound_voice(self, wamid: str, data: bytes | None = None, **kw: Any) -> dict:
        """Live voice note: ``audio/ogg; codecs=opus`` with ``voice: true``."""
        kw.setdefault("voice", True)
        data = sample_ogg_opus() if data is None else data
        return self.inbound_media("audio", wamid, data, kw.pop("mime_type", "audio/ogg; codecs=opus"), **kw)

    def inbound_audio(self, wamid: str, data: bytes | None = None, *, mime_type: str = "audio/mpeg", **kw) -> dict:
        """Live audio file: no ``voice`` key at all."""
        return self.inbound_media("audio", wamid, sample_mp3() if data is None else data, mime_type, **kw)

    def inbound_video(self, wamid: str, data: bytes | None = None, *, mime_type: str = "video/mp4", **kw) -> dict:
        return self.inbound_media("video", wamid, sample_mp4() if data is None else data, mime_type, **kw)

    def inbound_document(
        self,
        wamid: str,
        data: bytes | None = None,
        *,
        mime_type: str = "application/pdf",
        filename: str | None = "report.pdf",
        **kw: Any,
    ) -> dict:
        data = sample_pdf() if data is None else data
        return self.inbound_media("document", wamid, data, mime_type, filename=filename, **kw)

    def inbound_photo_document(self, wamid: str, data: bytes | None = None, **kw: Any) -> dict:
        """Live "photo sent as a document": ``document`` with ``image/png`` and a ``.png`` filename."""
        kw.setdefault("filename", "IMG_20260930_101010.png")
        return self.inbound_document(wamid, sample_png() if data is None else data, mime_type="image/png", **kw)

    def inbound_sticker(self, wamid: str, data: bytes | None = None, **kw: Any) -> dict:
        """Live sticker: ``image/webp`` with ``animated: false``."""
        kw.setdefault("animated", False)
        return self.inbound_media("sticker", wamid, sample_webp() if data is None else data, "image/webp", **kw)

    def inbound_oversize_document(
        self,
        wamid: str,
        *,
        file_size: int = 23_096_834,
        mime_type: str = "video/quicktime",
        filename: str = "IMG_0001.MOV",
        **kw: Any,
    ) -> dict:
        """Live 23 MB ``.MOV`` sent as a document: GET reports the full size, the download streams all of it."""
        return self.inbound_media(
            "document", wamid, b"\0\0\0\x14ftypqt  ", mime_type, filename=filename, virtual_size=file_size, **kw
        )

    # ------------------------------------------------------------------ transport
    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        endpoint, media_id = _route(request)
        self.routed.append((endpoint, request))
        self.host_log.append(
            HostRequest(
                host=request.url.host,
                method=request.method,
                path=request.url.path,
                endpoint=endpoint,
                authorized="authorization" in request.headers,
            )
        )
        queue = self.queues[endpoint]
        if queue:
            item = queue.popleft()
            if isinstance(item, Exception):
                raise item
            return item(request) if callable(item) else item
        if endpoint == "updates":
            return httpx.Response(204)
        if endpoint == "statuses" and json.loads(request.content).get("message_id") in self.not_creator_wamids:
            return error_response(403, 131005)
        if endpoint == "messages":
            return self._messages(request)
        if endpoint == "media_upload":
            return self._upload(request)
        if endpoint == "media_get":
            return self._media_get(media_id)
        if endpoint == "media_delete":
            return self._media_delete(media_id)
        if endpoint == "download":
            return self._download(request, media_id)
        return httpx.Response(200, json={"success": True})

    def http_client(self, **kwargs: Any) -> httpx.AsyncClient:
        """A raw ``httpx.AsyncClient`` wired to this fake (every host, including the media host)."""
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler), **kwargs)

    def client(self, api_key: str = API_KEY, *, unthrottled: bool = False, **kwargs: Any):
        from wap_plugin_under_test.client import AgentPlatformClient, RateWindow

        base_url = kwargs.get("base_url")
        if base_url:  # a base-URL override host serves media like Meta's (the plan allowlists it)
            host = httpx.URL(base_url).host
            if host:
                self.trusted_download_hosts.add(host)
        client = AgentPlatformClient(api_key, http=self.http_client(), **kwargs)
        if unthrottled:  # adapter tests poll in a tight loop; don't wait on Meta's per-minute budgets
            client.limits = {name: RateWindow(10**6) for name in client.limits}
        return client

    # ------------------------------------------------------------------ default endpoint behaviour
    def _ok_send(self) -> tuple[str, httpx.Response]:
        self._wamid += 1
        wamid = f"wamid.out{self._wamid}"
        return wamid, httpx.Response(
            200,
            json={
                "messaging_product": "whatsapp",
                "contacts": [{"input": CREATOR, "wa_id": CREATOR}],
                "messages": [{"id": wamid}],
            },
        )

    def _messages(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        msg_type = body.get("type") if isinstance(body, dict) else None
        if msg_type == "reaction":  # manual: reactions are receive-only
            return bad_field_response("Reactions cannot be sent")
        if msg_type not in MEDIA_MESSAGE_TYPES:
            return self._ok_send()[1]
        obj = body.get(msg_type)
        if not isinstance(obj, dict):
            return bad_field_response(f"{msg_type} is required when type is {msg_type}")
        media_id = obj.get("id")
        if not isinstance(media_id, str) or not media_id:
            return bad_field_response(f"{msg_type}.id is required")
        caption = obj.get("caption")
        if caption is not None:
            if self.reject_misplaced_media_fields and msg_type not in CAPTION_TYPES:
                return bad_field_response(f"caption is not available on {msg_type}")
            if not isinstance(caption, str) or utf16_len(caption) > MAX_CAPTION_UTF16:
                return caption_too_long_response()
        filename = obj.get("filename")
        if filename is not None and self.reject_misplaced_media_fields and msg_type != "document":
            return bad_field_response(f"filename is not available on {msg_type}")
        media = self.media.get(media_id)
        if media is None:
            return no_media_found_send_response(media_id)
        rule = UPLOAD_MIME_RULES.get(base_mime(media.mime_type))
        allowed = msg_type == "document" or (rule is not None and msg_type in rule.message_types)
        if not allowed:
            return media_type_mismatch_response()
        wamid, response = self._ok_send()
        self.sent_media.append(
            SentMedia(
                wamid=wamid,
                to=body.get("to"),
                type=msg_type,
                media_id=media_id,
                caption=caption,
                filename=filename,
                context=body.get("context"),
                body=body,
                media=media,
            )
        )
        return response

    def _upload(self, request: httpx.Request) -> httpx.Response:
        try:
            fields = multipart_fields(request)
        except ValueError:
            fields = {}
        product = fields.get("messaging_product")
        declared = fields.get("type")
        file_part = fields.get("file")
        record = Upload(
            messaging_product=product.data.decode("utf-8", "replace") if product else None,
            type=declared.data.decode("utf-8", "replace") if declared else None,
            filename=file_part.filename if file_part else None,
            content_type=file_part.content_type if file_part else None,
            data=file_part.data if file_part else None,
            media_id=None,
            status=400,
        )
        self.uploads.append(record)
        if record.messaging_product is None or file_part is None:
            return upload_missing_field_response()
        mime = (record.type or record.content_type or "").strip()
        if not mime:
            return no_declared_type_response()
        rule = UPLOAD_MIME_RULES.get(base_mime(mime))
        if rule is None:
            return unsupported_mime_response(mime)
        size = len(file_part.data)
        if size > rule.limit:
            if base_mime(mime) == "image/webp":
                return upload_not_accepted_response()
            return media_too_large_response(size, rule.limit, mime)
        stored_mime = "audio/ogg; codecs=opus" if base_mime(mime) == "audio/opus" else mime
        media = self.add_media(file_part.data, stored_mime, filename=file_part.filename)
        record.media_id, record.status = media.id, 200
        return httpx.Response(200, json={"id": media.id})

    def _media_get(self, media_id: str | None) -> httpx.Response:
        media = self.media.get(media_id or "")
        if media is None:
            return media_not_found_response(400)
        return httpx.Response(
            200,
            json={
                "url": self.media_url(media.id),
                "mime_type": media.mime_type,
                "sha256": media.sha256_hex,
                "file_size": media.file_size,
                "id": media.id,
                "messaging_product": "whatsapp",
            },
        )

    def _media_delete(self, media_id: str | None) -> httpx.Response:
        if not media_id or media_id not in self.media:
            return media_not_found_response(400)
        self.expire_media(media_id)
        return httpx.Response(200, json={"success": True})

    def _download(self, request: httpx.Request, media_id: str | None) -> httpx.Response:
        trusted = request.url.host in self.trusted_download_hosts
        fault = self.download_faults.get(media_id or "")
        mode = fault.mode if fault else None
        if trusted and not request.headers.get("authorization", "").startswith("Bearer "):
            return download_unauthorized_response()
        if mode == "timeout":
            raise httpx.ReadTimeout("fake read timeout", request=request)
        if trusted and mode == "redirect":
            location = fault.location or f"https://{EVIL_HOST}/agent/v1/media/{quote(media_id or '', safe='')}/content"
            return httpx.Response(302, headers={"Location": location})
        if trusted and mode == "status":
            headers = {"Retry-After": str(fault.retry_after)} if fault.retry_after is not None else None
            code = 130429 if fault.status == 429 else 2
            return meta_error(fault.status, code, f"(#{code}) fake media host error", headers=headers)
        media = self.media.get(media_id or "")
        if media is None or (trusted and mode == "not_found"):
            return media_not_found_response(404)
        body_size = media.file_size + (fault.extra_bytes if fault else 0)
        length: int | None = body_size
        if mode == "no_length":
            length = None
        elif mode == "wrong_length":
            length = fault.declared_length if fault.declared_length is not None else body_size // 2
        headers = {"Content-Type": _download_content_type(media.mime_type)}
        if length is not None:
            headers["Content-Length"] = str(length)
        return httpx.Response(200, headers=headers, content=self._stream(request, media, fault, body_size))

    async def _stream(
        self, request: httpx.Request, media: StoredMedia, fault: DownloadFault | None, body_size: int
    ) -> AsyncIterator[bytes]:
        mode = fault.mode if fault else None
        first = True
        for chunk in _padded_chunks(media, body_size):
            if mode == "bad_bytes" and first and chunk:
                chunk = bytes([chunk[0] ^ 0xFF]) + chunk[1:]
            if mode == "slow":
                await asyncio.sleep(fault.delay)
            yield chunk
            if mode == "timeout_mid_stream" and first:
                raise httpx.ReadTimeout("fake read timeout mid-stream", request=request)
            first = False


def _padded_chunks(media: StoredMedia, total: int) -> Iterator[bytes]:
    remaining = total
    for chunk in media.chunks():
        if remaining <= 0:
            return
        chunk = chunk[:remaining]
        remaining -= len(chunk)
        yield chunk
    zeros = b"\0" * _CHUNK
    while remaining > 0:
        n = min(_CHUNK, remaining)
        remaining -= n
        yield zeros[:n]


def _download_content_type(mime: str) -> str:
    # Live: a text/plain document downloads as ``text/plain;charset=utf-8``; others as stored.
    return "text/plain;charset=utf-8" if base_mime(mime) == "text/plain" else mime


def _route(request: httpx.Request) -> tuple[str, str | None]:
    path = request.url.path
    method = request.method.upper()
    match = _DOWNLOAD_RE.search(path)
    if match and method == "GET":
        return "download", match.group(1)
    if path.endswith("/media") and method == "POST":
        return "media_upload", None
    match = _MEDIA_ID_RE.search(path)
    if match and method == "GET":
        return "media_get", match.group(1)
    if match and method == "DELETE":
        return "media_delete", match.group(1)
    return path.rsplit("/", 1)[-1], None


# --------------------------------------------------------------------------- update builders


def updates_response(
    *messages: dict, next_offset: int, contacts: list | None = None, statuses: list | None = None
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "object": "whatsapp_agent_platform",
            "entry": [
                {
                    "id": "123456789",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "contacts": contacts
                                if contacts is not None
                                else [{"wa_id": CREATOR, "profile": {"name": "Alex"}}],
                                "messages": list(messages),
                                "statuses": statuses or [],
                            },
                        }
                    ],
                }
            ],
            "next_offset": next_offset,
        },
    )


def text_message(
    wamid: str, body: str, *, sender: str = CREATOR, timestamp: int | None = None, context: dict | None = None
) -> dict:
    msg = {
        "from": sender,
        "id": wamid,
        "timestamp": str(timestamp or int(time.time()) + 5),
        "type": "text",
        "text": {"body": body},
    }
    if context:
        msg["context"] = context
    return msg


def media_message(
    wamid: str,
    kind: str,
    media_obj: dict,
    *,
    sender: str = CREATOR,
    timestamp: int | None = None,
    context: dict | None = None,
) -> dict:
    """An inbound message of ``type=kind`` carrying ``media_obj`` verbatim (no store; see ``FakeMeta.inbound_*``)."""
    msg = {
        "from": sender,
        "id": wamid,
        "timestamp": str(timestamp or int(time.time()) + 5),
        "type": kind,
        kind: media_obj,
    }
    if context:
        msg["context"] = context
    return msg


def reaction_message(
    wamid: str,
    target_wamid: str,
    emoji: Any = "\U0001f602",
    *,
    sender: str = CREATOR,
    timestamp: int | None = None,
) -> dict:
    """Live reaction shape ``{emoji, message_id}``. ``emoji=""`` means removed; ``emoji=None`` omits the key."""
    reaction: dict[str, Any] = {"message_id": target_wamid}
    if emoji is not None:
        reaction["emoji"] = emoji
    return media_message(wamid, "reaction", reaction, sender=sender, timestamp=timestamp)


# --------------------------------------------------------------------------- sample media bytes


@functools.cache
def _pillow_image(fmt: str, mode: str, size: tuple[int, int], **save: Any) -> bytes:
    from PIL import Image

    color: Any = {"RGB": (200, 30, 30), "RGBA": (30, 200, 30, 128), "L": 128, "P": 3, "I;16": 40000}[mode]
    image = Image.new(mode, size, color)
    out = io.BytesIO()
    image.save(out, format=fmt, **save)
    return out.getvalue()


def sample_jpeg(size: tuple[int, int] = (8, 8)) -> bytes:
    """A real, decodable baseline JPEG."""
    return _pillow_image("JPEG", "RGB", size, quality=80)


def sample_png(size: tuple[int, int] = (8, 8), mode: str = "RGBA") -> bytes:
    """A real PNG (``mode`` "RGBA", "RGB", "P" palette, "L", or "I;16" 16-bit grey)."""
    return _pillow_image("PNG", mode, size)


def sample_webp(size: tuple[int, int] = (16, 16)) -> bytes:
    """A real lossless WebP (sticker or "image" that must be converted)."""
    return _pillow_image("WEBP", "RGBA", size, lossless=True)


@functools.cache
def sample_gif(animated: bool = False) -> bytes:
    """A real GIF; ``animated=True`` has two frames."""
    from PIL import Image

    colors = [(255, 0, 0), (0, 0, 255)][: 2 if animated else 1]
    frames = [Image.new("RGB", (8, 8), color) for color in colors]
    out = io.BytesIO()
    frames[0].save(out, format="GIF", save_all=animated, append_images=frames[1:], duration=100, loop=0)
    return out.getvalue()


def sample_ogg_opus() -> bytes:
    """Ogg/Opus magic bytes (``OggS`` page with an ``OpusHead``); not decodable audio."""
    head = b"OpusHead\x01\x01\x38\x01\x80\xbb\x00\x00\x00\x00\x00"
    return b"OggS\x00\x02" + b"\x00" * 20 + b"\x01\x13" + head + b"OggS\x00\x00" + b"\x00" * 64


def sample_mp3() -> bytes:
    """An ID3 header plus one MPEG-1 Layer III frame header; not decodable audio."""
    return b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x64" + b"\x00" * 413


def sample_mp4() -> bytes:
    """An ``ftyp`` box plus an empty ``mdat``; not playable video."""
    return b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00\x00\x00\x08mdat"


def sample_pdf() -> bytes:
    """A minimal one-page PDF."""
    return (
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 72 72]>>endobj\n"
        b"trailer<</Root 1 0 R>>\n%%EOF\n"
    )


load_plugin()  # test modules import ``wap_plugin_under_test`` at collection time
