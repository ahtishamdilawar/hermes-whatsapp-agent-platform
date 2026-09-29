"""Media policy for the WhatsApp Agent Platform: what may be sent, how, and how inbound media is read.

Pure policy with no Hermes imports and no network I/O. Pillow (a Hermes core dependency, not ours) is imported
lazily inside the conversion helpers, so this module imports cleanly without it; when it is missing or fails,
the file goes out as a document instead.

The MIME tables are our own and keyed by lower-cased extension. ``mimetypes`` is never used: on Windows it reads
the registry, so the same file would get different types on different machines. Meta's limits are binary
(5,242,880 B accepted and 5,242,881 B rejected for an image, observed live). Outbound decisions follow the P0
live probes: WebP only as a sticker, GIF rejected at upload, text-like files as ``text/plain`` (the phone shows
``name.ext.txt``), and any other binary as ``application/octet-stream`` (the phone keeps the name).
"""

from __future__ import annotations

import base64
import binascii
import codecs
import io
import math
import os
import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass

# --------------------------------------------------------------------------- limits and tables

MIB = 1024 * 1024

SEND_LIMITS: dict[str, int] = {
    "image": 5 * MIB,
    "video": 16 * MIB,
    "audio": 16 * MIB,
    "document": 16 * MIB,
    "sticker": 500_000,  # the manual says 500 KB; the probe was inconclusive, so the smaller reading
}
MAX_UPLOAD_BYTES = max(SEND_LIMITS.values())
MAX_CAPTION_UTF16 = 1024
INLINE_TEXT_MAX_BYTES = 100 * 1024
INBOUND_HARD_CAP = 64 * MIB
MAX_FILENAME_CHARS = 120
MAX_FILENAME_BYTES = 200  # UTF-8; keeps Hermes's ``doc_<hex>_`` prefix plus the name under ext4's 255 bytes
MAX_EXT_CHARS = 10
MAX_IMAGE_PIXELS = 25_000_000  # manual: "at or below 25 megapixels"
_MAX_DECODE_PIXELS = 100_000_000  # refuse to decode anything bigger (memory), fall back to a document

OCTET_STREAM = "application/octet-stream"
TEXT_PLAIN = "text/plain"

MEDIA_KINDS = ("image", "audio", "video", "document", "sticker")
REQUESTABLE = ("image", "video", "audio", "voice", "document", "sticker")

_OFFICE = {
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}

# Extension -> MIME. The first extension listed for a MIME is its canonical one (``ext_for_mime``).
MIME_BY_EXT: dict[str, str] = {
    # images
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".jpe": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".ico": "image/x-icon",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".avif": "image/avif",
    ".svg": "image/svg+xml",
    # video
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".3gp": "video/3gpp",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".avi": "video/x-msvideo",
    ".ogv": "video/ogg",
    ".mpg": "video/mpeg",
    ".mpeg": "video/mpeg",
    ".wmv": "video/x-ms-wmv",
    ".flv": "video/x-flv",
    # audio
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".amr": "audio/amr",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/opus",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".m2a": "audio/mp2",
    ".aif": "audio/aiff",
    ".aiff": "audio/aiff",
    ".wma": "audio/x-ms-wma",
    ".weba": "audio/webm",
    ".mka": "audio/x-matroska",
    # documents Meta lists
    ".pdf": "application/pdf",
    ".txt": TEXT_PLAIN,
    **_OFFICE,
    # everything else we know (none of these are accepted by the upload as such)
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".html": "text/html",
    ".htm": "text/html",
    ".json": "application/json",
    ".xml": "application/xml",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".rtf": "application/rtf",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".odp": "application/vnd.oasis.opendocument.presentation",
    ".epub": "application/epub+zip",
    ".key": "application/vnd.apple.keynote",
    ".kmz": "application/vnd.google-earth.kmz",
    ".apk": "application/vnd.android.package-archive",
    ".jar": "application/java-archive",
    ".zip": "application/zip",
    ".gz": "application/gzip",
    ".tgz": "application/gzip",
    ".tar": "application/x-tar",
    ".bz2": "application/x-bzip2",
    ".xz": "application/x-xz",
    ".7z": "application/x-7z-compressed",
    ".rar": "application/vnd.rar",
}

_EXT_BY_MIME: dict[str, str] = {}
for _ext, _mime in MIME_BY_EXT.items():
    _EXT_BY_MIME.setdefault(_mime, _ext)
_EXT_BY_MIME["audio/opus"] = ".ogg"  # stored by Meta as audio/ogg; codecs=opus
_EXT_BY_MIME.update({"image/x-ms-bmp": ".bmp", "audio/x-wav": ".wav", "audio/wave": ".wav", "image/jpg": ".jpg"})
del _ext, _mime

# Meta's upload allowlist (manual, confirmed live for the common ones). Everything else is 131053.
ACCEPTED_UPLOAD_MIMES: frozenset[str] = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/webp",
        "video/mp4",
        "video/3gpp",
        "audio/aac",
        "audio/mp4",
        "audio/mpeg",
        "audio/amr",
        "audio/ogg",
        "audio/opus",
        "application/pdf",
        TEXT_PLAIN,
        *_OFFICE.values(),
        OCTET_STREAM,
    }
)

# MIME types sent as a document with their own type. JPEG/PNG as a document was checked on the phone; audio,
# video and WebP ids as a document were not, so those go as octet-stream (bytes and name are unchanged anyway).
DOCUMENT_MIMES: frozenset[str] = frozenset(
    {"application/pdf", TEXT_PLAIN, *_OFFICE.values(), "image/jpeg", "image/png"}
)

_IMAGE_AS_IS = frozenset({"image/jpeg", "image/png"})
_VIDEO_AS_IS = frozenset({"video/mp4", "video/3gpp"})
_AUDIO_AS_IS = frozenset({"audio/mpeg", "audio/mp4", "audio/aac", "audio/amr", "audio/ogg", "audio/opus"})
_LOSSLESS_IMAGES = frozenset({"image/gif", "image/webp", "image/bmp", "image/x-ms-bmp", "image/tiff", "image/x-icon"})
_PHOTO_IMAGES = frozenset({"image/heic", "image/heif", "image/avif"})
_PNG_SOURCE_MAX = 2 * MIB  # a lossless source above this would likely make a PNG over the image limit

# Mirrors Hermes's private ``gateway.platforms.base._TEXT_INJECT_EXTENSIONS`` (not imported: it is private).
TEXT_EXTS: frozenset[str] = frozenset(
    {
        ".txt", ".md", ".markdown", ".csv", ".tsv", ".log", ".json", ".jsonl", ".ndjson", ".xml",
        ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env", ".properties", ".html", ".htm",
        ".css", ".scss", ".sass", ".less", ".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
        ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".c", ".h", ".cpp", ".cc", ".hpp", ".cs",
        ".java", ".kt", ".go", ".rs", ".rb", ".php", ".pl", ".lua", ".r", ".jl", ".swift", ".m",
        ".scala", ".clj", ".ex", ".exs", ".erl", ".sql", ".graphql", ".proto", ".tf", ".hcl",
        ".dockerfile", ".makefile", ".cmake", ".gradle", ".rst", ".tex", ".srt", ".vtt", ".diff",
        ".patch",
    }
)  # fmt: skip

# Short, neutral reasons for the "sent as a file" caption note.
REASON_UNSUPPORTED = "format not supported by WhatsApp"
REASON_ANIMATED = "animated images are not supported by WhatsApp"
REASON_TOO_LARGE = "too large for a WhatsApp image"


def downgrade_note(reason: str) -> str:
    return f"(sent as a file: {reason})"


class MediaPolicyError(ValueError):
    """A file the policy refuses before any upload. The message never contains a path or a filename."""


class MediaTooBig(MediaPolicyError):
    def __init__(self, size: int, limit: int, kind: str = "document") -> None:
        self.size, self.limit, self.kind = size, limit, kind
        super().__init__(f"file too large for WhatsApp ({_mib(size)} MiB, the {kind} limit is {_mib(limit)} MiB)")


class EmptyMedia(MediaPolicyError):
    def __init__(self) -> None:
        super().__init__("file is empty")


def _mib(n: int) -> str:
    return f"{n / MIB:.1f}".removesuffix(".0")


def upload_limit(mime: str | None) -> int:
    """Meta's upload limit for a declared MIME type (the upload knows only the MIME, not the message type)."""
    mime = base_mime(mime)
    if mime in _IMAGE_AS_IS:
        return SEND_LIMITS["image"]
    if mime == "image/webp":
        return SEND_LIMITS["sticker"]
    return MAX_UPLOAD_BYTES


def ext_for_mime(mime: str | None) -> str | None:
    """Canonical extension (with the dot) for a MIME type; None when unknown or octet-stream."""
    return _EXT_BY_MIME.get(base_mime(mime) or "")


# --------------------------------------------------------------------------- small helpers


def utf16_len(s: str) -> int:
    """Length in UTF-16 code units (how Meta counts; a lone surrogate counts as one)."""
    return len(s.encode("utf-16-le", "surrogatepass")) // 2


def caption_fits(s: str) -> bool:
    return utf16_len(s) <= MAX_CAPTION_UTF16


def base_mime(mime: str | None) -> str | None:
    """``"Audio/OGG; codecs=opus"`` -> ``"audio/ogg"``; None for None or blank."""
    if not isinstance(mime, str):
        return None
    out = mime.split(";", 1)[0].strip().lower()
    return out or None


_MEDIA_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")  # same rule as client.py


def valid_media_id(s: object) -> bool:
    """Safe to put in a URL path: ``[A-Za-z0-9][A-Za-z0-9._-]{0,127}``.

    The first character must be alphanumeric: httpx normalises ``.``/``..`` path segments, so an id starting with a
    dot could point an authorised request at another endpoint.
    """
    return isinstance(s, str) and _MEDIA_ID_RE.fullmatch(s) is not None


_HEX64_RE = re.compile(r"[0-9a-fA-F]{64}")
_B64_32_RE = re.compile(r"[A-Za-z0-9+/_-]{43}=?")


def sha256_bytes(s: object) -> bytes | None:
    """A SHA-256 digest given as hex (``GET /media``) or Base64 (updates; also URL-safe or unpadded) -> 32 bytes."""
    if not isinstance(s, str):
        return None
    t = s.strip()
    if _HEX64_RE.fullmatch(t):
        return bytes.fromhex(t)
    if _B64_32_RE.fullmatch(t):
        t = t.rstrip("=").replace("-", "+").replace("_", "/") + "="
        try:
            raw = base64.b64decode(t, validate=True)
        except (binascii.Error, ValueError):
            return None
        return raw if len(raw) == 32 else None
    return None


# --------------------------------------------------------------------------- filenames

_UNSAFE_CHARS = re.compile(r"[^\w .\-()\[\]+,@=#]")  # also covers <>:"/\|?* and every other punctuation
_SPACES = re.compile(r" {2,}")
_EXT_RE = re.compile(rf"[A-Za-z0-9]{{1,{MAX_EXT_CHARS}}}")
_DROP_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})  # controls, bidi/format, surrogates, ...
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def _clean_component(name: str) -> str:
    text = unicodedata.normalize("NFKC", name)
    text = "".join(ch for ch in text if unicodedata.category(ch) not in _DROP_CATEGORIES)
    parts = [p for p in re.split(r"[/\\]", text) if p.strip(" .")]
    text = parts[-1] if parts else ""
    text = _SPACES.sub(" ", _UNSAFE_CHARS.sub("_", text))
    return text.lstrip(" .-").rstrip(" .")


def _split_ext(name: str) -> tuple[str, str]:
    stem, dot, ext = name.rpartition(".")
    if dot and stem and _EXT_RE.fullmatch(ext):
        return stem.rstrip(" ."), ext
    return name, ""


def sanitize_filename(name: str | None, *, default_stem: str, ext: str | None = None) -> str:
    """A safe, deterministic display/upload filename.

    Drops directories, control/format (incl. bidi override) characters; replaces ``<>:"/\\|?*`` and other
    punctuation with ``_``; strips leading dots/dashes/spaces and trailing dots/spaces; prefixes Windows reserved
    names (``CON``, ``COM1.txt``...) with ``_``; caps the name at 120 characters (and 200 UTF-8 bytes), keeping the
    extension. ``ext`` forces the extension: a recognised media/document extension is replaced (``a.webp`` ->
    ``a.png``), anything else is kept in the stem (``notes.v2`` -> ``notes.v2.pdf``).
    """
    stem, cur = _split_ext(_clean_component(name if isinstance(name, str) else ""))
    if ext is not None:
        want = ext.lstrip(".")
        if _EXT_RE.fullmatch(want):
            if cur.lower() == want.lower():
                pass
            elif cur and f".{cur.lower()}" in MIME_BY_EXT:
                cur = want
            else:
                stem, cur = (f"{stem}.{cur}" if cur else stem), want
    if not stem:
        stem = _clean_component(default_stem) or "file"
    if stem.split(".", 1)[0].rstrip(" ").upper() in _WINDOWS_RESERVED:
        stem = "_" + stem
    suffix = f".{cur}" if cur else ""
    stem = stem[: MAX_FILENAME_CHARS - len(suffix)]
    while stem and len((stem + suffix).encode("utf-8")) > MAX_FILENAME_BYTES:
        stem = stem[:-1]
    return (stem.rstrip(" .") or "file") + suffix


def _ext_of(name: str | None) -> str:
    if not name:
        return ""
    base = re.split(r"[/\\]", name)[-1]
    _, dot, ext = base.rpartition(".")
    return f".{ext.lower()}" if dot and ext else ""


# --------------------------------------------------------------------------- sniffing

_FTYP_BRANDS = {
    b"qt  ": "video/quicktime",
    b"M4A ": "audio/mp4",
    b"M4B ": "audio/mp4",
    b"M4P ": "audio/mp4",
    b"avif": "image/avif",
    b"avis": "image/avif",
    **dict.fromkeys((b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"), "image/heic"),
}


def sniff_mime(head: bytes) -> str | None:
    """MIME type from magic bytes (pass at least the first 64 bytes), or None when nothing matched."""
    h = bytes(head[:4096])
    if h.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if h.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if h[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if h[:4] == b"RIFF" and len(h) >= 12:
        return {b"WEBP": "image/webp", b"WAVE": "audio/wav", b"AVI ": "video/x-msvideo"}.get(h[8:12])
    if h.startswith(b"%PDF-"):
        return "application/pdf"
    if h.startswith(b"OggS"):
        return "video/ogg" if b"theora" in h[:128] else "audio/ogg"
    if h[4:8] == b"ftyp" and len(h) >= 12:
        brand = h[8:12]
        if brand[:3] in (b"3gp", b"3g2"):
            return "video/3gpp"
        return _FTYP_BRANDS.get(brand, "video/mp4")
    if h[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return "application/zip"
    if h.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "application/x-ole-storage"
    if h.startswith(b"fLaC"):
        return "audio/flac"
    if h.startswith(b"ID3"):
        return "audio/mpeg"
    if h.startswith(b"#!AMR"):
        return "audio/amr"
    if h.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm" if b"webm" in h[:64] else "video/x-matroska"
    if h[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    if h.startswith(b"BM") and len(h) >= 18 and int.from_bytes(h[14:18], "little") in (12, 40, 52, 56, 108, 124):
        return "image/bmp"
    if len(h) >= 3 and h[0] == 0xFF and (h[1] & 0xE0) == 0xE0 and h[1] not in (0xFE, 0xFF):  # not a UTF-16 BOM
        if (h[1] & 0xF6) == 0xF0:
            return "audio/aac"  # ADTS: 12-bit sync, layer 00
        if (h[1] >> 3) & 3 != 1 and (h[1] >> 1) & 3 != 0 and (h[2] >> 4) not in (0, 15):
            return "audio/mpeg"  # MPEG audio frame without an ID3 tag
    return None


def webp_is_animated(head: bytes) -> bool:
    """True for an extended-format WebP with the animation flag set (needs the first 21 bytes)."""
    return len(head) > 20 and head[:4] == b"RIFF" and head[8:16] == b"WEBPVP8X" and bool(head[20] & 0x02)


_FAMILY = {
    **dict.fromkeys(("video/mp4", "audio/mp4", "video/3gpp", "video/quicktime"), "ftyp-av"),
    **dict.fromkeys(("image/heic", "image/heif", "image/avif"), "ftyp-image"),
    **dict.fromkeys(("audio/ogg", "audio/opus"), "ogg-audio"),
    **dict.fromkeys(("audio/mpeg", "audio/aac", "audio/mp2"), "mpeg-audio"),
    **dict.fromkeys(("video/webm", "video/x-matroska", "audio/webm", "audio/x-matroska"), "ebml"),
    **dict.fromkeys(("application/msword", "application/vnd.ms-excel", "application/vnd.ms-powerpoint"), "ole"),
    "application/x-ole-storage": "ole",
    **dict.fromkeys(
        (
            "application/zip",
            *(m for e, m in _OFFICE.items() if e.endswith("x")),
            "application/vnd.oasis.opendocument.text",
            "application/vnd.oasis.opendocument.spreadsheet",
            "application/vnd.oasis.opendocument.presentation",
            "application/epub+zip",
            "application/vnd.apple.keynote",
            "application/vnd.google-earth.kmz",
            "application/vnd.android.package-archive",
            "application/java-archive",
        ),
        "zip",
    ),
}


def _family(mime: str | None) -> str | None:
    return _FAMILY.get(mime or "", mime)


def gif_is_animated(data: bytes) -> bool:
    """True when a GIF has more than one image (walks the block structure; stdlib only)."""
    if data[:6] not in (b"GIF87a", b"GIF89a") or len(data) < 13:
        return False
    n, pos, frames = len(data), 13, 0
    if data[10] & 0x80:
        pos += 3 << ((data[10] & 7) + 1)
    while pos < n:
        block = data[pos]
        if block == 0x21:  # extension: label, then sub-blocks
            pos = _skip_sub_blocks(data, pos + 2)
        elif block == 0x2C:  # image descriptor
            frames += 1
            if frames > 1:
                return True
            if pos + 10 > n:
                return False
            flags = data[pos + 9]
            pos += 10
            if flags & 0x80:
                pos += 3 << ((flags & 7) + 1)
            pos = _skip_sub_blocks(data, pos + 1)  # + LZW minimum code size
        else:  # 0x3B trailer, or garbage
            return False
    return False


def _skip_sub_blocks(data: bytes, pos: int) -> int:
    n = len(data)
    while pos < n:
        size = data[pos]
        pos += 1
        if size == 0:
            return pos
        pos += size
    return pos


def _looks_like_text(head: bytes, complete: bool) -> bool:
    """UTF-8 (BOM allowed) with no NUL; a multi-byte character cut at the end of ``head`` is tolerated."""
    if not head or b"\x00" in head:
        return False
    try:
        codecs.getincrementaldecoder("utf-8-sig")().decode(head, final=complete)
    except UnicodeDecodeError:
        return False
    return True


# --------------------------------------------------------------------------- inbound


@dataclass(frozen=True)
class InboundMedia:
    kind: str  # image | audio | video | document | sticker
    media_id: str
    mime: str | None  # parameters stripped, lower-cased; None when absent or unparseable
    sha256: str | None  # as received (Base64 in updates); validated to decode to 32 bytes
    caption: str | None
    filename: str | None  # raw, UNSANITISED; run it through sanitize_filename before any use
    voice: bool  # the ``voice`` key is present only on voice notes
    animated: bool


_MIME_RE = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,127}")


def parse_inbound(message: dict) -> InboundMedia | None:
    """The media part of an inbound ``messages[]`` item.

    None for text, reactions and unknown types. ``ValueError`` for a malformed media object (not a dict, a
    missing or unsafe id, wrongly typed fields, an undecodable sha256). The error text never contains the id.
    """
    if not isinstance(message, dict):
        raise ValueError("message is not an object")
    kind = message.get("type")
    if kind not in MEDIA_KINDS:
        return None
    obj = message.get(kind)
    if not isinstance(obj, dict):
        raise ValueError(f"{kind} payload is missing or not an object")
    media_id = obj.get("id")
    if not valid_media_id(media_id):
        raise ValueError(f"{kind} media id is missing or malformed")
    raw_mime = obj.get("mime_type")
    if raw_mime is not None and not isinstance(raw_mime, str):
        raise ValueError(f"{kind} mime_type is not a string")
    mime = base_mime(raw_mime)
    if mime is not None and not _MIME_RE.fullmatch(mime):
        mime = None
    sha = obj.get("sha256")
    if sha is not None and sha256_bytes(sha) is None:
        raise ValueError(f"{kind} sha256 is not a hex or Base64 SHA-256 digest")
    caption = _optional_str(obj, "caption", kind)
    filename = _optional_str(obj, "filename", kind)
    return InboundMedia(
        kind=kind,
        media_id=media_id,
        mime=mime,
        sha256=sha.strip() if isinstance(sha, str) else None,
        caption=caption or None,
        filename=filename or None,
        voice=kind == "audio" and obj.get("voice") is True,
        animated=kind == "sticker" and obj.get("animated") is True,
    )


def _optional_str(obj: dict, key: str, kind: str) -> str | None:
    value = obj.get(key)
    if value is None or isinstance(value, str):
        return value
    raise ValueError(f"{kind} {key} is not a string")


def inbound_cap(kind: str, hermes_max: int | None) -> int:
    """Download cap: Hermes's ``max_inbound_media_bytes`` when > 0, never above the plugin's 64 MiB hard cap.

    Inbound is not limited by Meta's send table: users can send larger files (23 MB observed live).
    """
    if kind not in MEDIA_KINDS:
        raise ValueError("unknown media kind")
    if isinstance(hermes_max, int) and not isinstance(hermes_max, bool) and hermes_max > 0:
        return min(hermes_max, INBOUND_HARD_CAP)
    return INBOUND_HARD_CAP


def should_inline_text(filename: str | None, mime: str | None, data: bytes) -> str | None:
    """The document's text to inline into the prompt, or None.

    Only when the extension is in ``TEXT_EXTS`` or the MIME is ``text/*`` (an extension gate, not a blind decode:
    PDF/ZIP/DOCX headers are valid ASCII), the data is at most 100 KiB, and it decodes as strict ``utf-8-sig``
    (the BOM is dropped). Text containing NUL is refused.
    """
    ext = _ext_of(sanitize_filename(filename, default_stem="document")) if filename else ""
    if ext not in TEXT_EXTS and not (base_mime(mime) or "").startswith("text/"):
        return None
    if len(data) > INLINE_TEXT_MAX_BYTES:
        return None
    try:
        text = bytes(data).decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    return None if "\x00" in text else text


# --------------------------------------------------------------------------- outbound: plan


@dataclass(frozen=True)
class OutboundPlan:
    kind: str  # image | video | audio | document | sticker (the message type)
    mime: str  # the upload's declared MIME type
    filename: str  # sanitised
    transform: str | None  # None | "to_png" | "to_jpeg" | "transcode_opus"
    downgraded: bool  # a photo/video/audio going out as a document
    reason: str | None  # why it was downgraded (for the caption note)
    source_mime: str | None = None  # what the file was detected as
    source_filename: str | None = None  # sanitised original name, used by a document fallback


def plan_outbound(
    path: str,
    *,
    requested: str,
    force_document: bool = False,
    file_name: str | None = None,
    size: int,
) -> OutboundPlan:
    """Decide how a local file goes out. Reads at most the file's head (the whole file only for a GIF).

    Routes by what the file *is* (extension, corrected by magic bytes), not by which ``send_*`` was called,
    except that ``document`` / ``force_document`` always send the bytes unmodified as a document.
    Raises ``MediaTooBig`` over 16 MiB, ``EmptyMedia`` for 0 bytes, ``ValueError`` for an unknown ``requested``,
    and ``OSError`` when the file cannot be read.
    """
    if requested not in REQUESTABLE:
        raise ValueError(f"unknown requested kind {requested!r}")
    if size <= 0:
        raise EmptyMedia()
    if size > MAX_UPLOAD_BYTES:
        raise MediaTooBig(size, MAX_UPLOAD_BYTES)
    name = file_name or os.path.basename(path)
    if not _ext_of(name) and _ext_of(path):
        name += _ext_of(path)  # a display name without extension takes the file's
    with open(path, "rb") as fh:
        head = fh.read(4096)
    ext_mime = MIME_BY_EXT.get(_ext_of(name))
    sniffed = sniff_mime(head)
    mime = sniffed if sniffed and _family(sniffed) != _family(ext_mime) else ext_mime

    if force_document or requested == "document":
        return _as_document(name, mime, head, size)

    if requested == "sticker" and mime == "image/webp" and size <= SEND_LIMITS["sticker"]:
        return OutboundPlan("sticker", "image/webp", _named(name, "image/webp", "sticker"), None, False, None, mime)

    family = (mime or "").split("/", 1)[0]
    if family == "audio" or (requested in ("audio", "voice") and mime in ("video/mp4", "audio/mp4")):
        return _plan_audio(name, mime)
    if family == "image":
        if sniffed is None and mime != "image/svg+xml":  # named like an image, but the bytes are not one
            return _downgrade(name, mime, REASON_UNSUPPORTED)
        return _plan_image(path, name, mime, head, size)
    if family == "video":
        return _plan_video(name, mime)
    return _as_document(name, mime, head, size)


def _named(name: str, mime: str | None, default_stem: str) -> str:
    """Sanitised name whose extension agrees with ``mime`` (added when missing or of another family)."""
    want = ext_for_mime(mime) if mime not in (None, OCTET_STREAM, TEXT_PLAIN) else None
    ext = _ext_of(name)
    if want and (not ext or _family(MIME_BY_EXT.get(ext)) != _family(mime)):
        return sanitize_filename(name, default_stem=default_stem, ext=want)
    return sanitize_filename(name, default_stem=default_stem)


def _as_document(name: str, mime: str | None, head: bytes, size: int) -> OutboundPlan:
    """The bytes as they are: the file's own MIME if Meta lists it for documents, else text/plain or octet-stream."""
    if mime in DOCUMENT_MIMES and size <= upload_limit(mime):
        doc_mime = mime
    elif _is_text_like(name, mime, head, size):
        doc_mime = TEXT_PLAIN
    else:
        doc_mime = OCTET_STREAM
    if doc_mime in (TEXT_PLAIN, OCTET_STREAM):
        filename = _keep_name(name, mime, "document")
    else:
        filename = _named(name, doc_mime, "document")
    return OutboundPlan("document", doc_mime, filename, None, False, None, mime)


def _keep_name(name: str, mime: str | None, default_stem: str) -> str:
    """Sanitised name as given; only a name without any extension gets the detected type's one."""
    return sanitize_filename(name, default_stem=default_stem, ext=None if _ext_of(name) else ext_for_mime(mime))


def _is_text_like(name: str, mime: str | None, head: bytes, size: int) -> bool:
    """In ``TEXT_EXTS`` (or of no known type) and the head decodes as UTF-8 without NUL."""
    ext = _ext_of(name)
    if ext not in TEXT_EXTS and (mime is not None and not mime.startswith("text/")):
        return False
    return _looks_like_text(head, complete=size <= len(head))


def _downgrade(name: str, mime: str | None, reason: str) -> OutboundPlan:
    """A photo/video/audio going out as an unmodified ``application/octet-stream`` document (name kept)."""
    return OutboundPlan("document", OCTET_STREAM, _keep_name(name, mime, "file"), None, True, reason, mime)


def _plan_image(path: str, name: str, mime: str | None, head: bytes, size: int) -> OutboundPlan:
    original = _named(name, mime, "image")
    if mime in _IMAGE_AS_IS:
        if size <= SEND_LIMITS["image"]:
            return OutboundPlan("image", mime, original, None, False, None, mime)
        return _convert_plan("to_jpeg", name, mime, original)
    if mime == "image/gif":
        with open(path, "rb") as fh:
            data = fh.read(MAX_UPLOAD_BYTES + 1)
        if gif_is_animated(data):
            return _downgrade(name, mime, REASON_ANIMATED)
    if mime == "image/webp" and webp_is_animated(head):
        return _downgrade(name, mime, REASON_ANIMATED)
    if mime in _LOSSLESS_IMAGES:
        return _convert_plan("to_png" if size <= _PNG_SOURCE_MAX else "to_jpeg", name, mime, original)
    if mime in _PHOTO_IMAGES:
        return _convert_plan("to_jpeg", name, mime, original)
    return _downgrade(name, mime, REASON_UNSUPPORTED)  # SVG (never rasterised) and unknown image/*


def _convert_plan(transform: str, name: str, mime: str | None, original: str) -> OutboundPlan:
    out_mime = "image/png" if transform == "to_png" else "image/jpeg"
    filename = sanitize_filename(name, default_stem="image", ext=ext_for_mime(out_mime))
    return OutboundPlan("image", out_mime, filename, transform, False, None, mime, original)


def _plan_video(name: str, mime: str | None) -> OutboundPlan:
    if mime in _VIDEO_AS_IS:
        return OutboundPlan("video", mime, _named(name, mime, "video"), None, False, None, mime)
    return _downgrade(name, mime, REASON_UNSUPPORTED)  # MOV/MKV/WebM/AVI...: rejected or unplayable


def _plan_audio(name: str, mime: str | None) -> OutboundPlan:
    if mime in ("video/mp4", "audio/mp4"):
        return OutboundPlan("audio", "audio/mp4", _named(name, "audio/mp4", "audio"), None, False, None, mime)
    if mime in _AUDIO_AS_IS:
        # Stereo Opus and Vorbis were accepted live; .opus is uploaded as the container Meta stores it in.
        upload = "audio/ogg" if mime == "audio/opus" else mime
        return OutboundPlan("audio", upload, _named(name, mime, "audio"), None, False, None, mime)
    filename = sanitize_filename(name, default_stem="audio", ext=".ogg")
    original = sanitize_filename(name, default_stem="audio")
    return OutboundPlan("audio", "audio/ogg", filename, "transcode_opus", False, None, mime, original)


# --------------------------------------------------------------------------- outbound: prepare


@dataclass(frozen=True)
class PreparedMedia:
    data: bytes
    mime: str
    filename: str
    kind: str
    note: str | None  # "(sent as a file: ...)" when a photo/video/audio went out as a document

    @property
    def downgraded(self) -> bool:
        return self.note is not None


class _ConversionFailed(Exception):
    pass


def prepare_outbound(
    path: str, plan: OutboundPlan, *, transcoder: Callable[[str], str | None] | None = None
) -> PreparedMedia:
    """Read (and if planned, convert) the file. Sync and blocking: run it in ``asyncio.to_thread``.

    Pillow conversions run in memory. The transcoder's output file is always deleted (also on errors), and
    nothing is ever written next to the source. A failed conversion falls back to an unmodified octet-stream
    document with a note. Raises ``MediaTooBig`` if the bytes to send are over their limit, ``OSError`` on read.
    """
    if plan.transform is None:
        data = _read_limited(path, plan.kind, plan.mime)
        note = downgrade_note(plan.reason or REASON_UNSUPPORTED) if plan.downgraded else None
        return PreparedMedia(data, plan.mime, plan.filename, plan.kind, note)

    if plan.transform in ("to_png", "to_jpeg"):
        try:
            data, mime = _convert_image(path, plan.transform)
        except Exception:  # ImportError (no Pillow), decode errors, bombs, oversize after the ladder
            reason = REASON_TOO_LARGE if plan.source_mime in _IMAGE_AS_IS else REASON_UNSUPPORTED
            return _fallback_document(path, plan, reason)
        filename = plan.filename
        if mime != plan.mime:
            filename = sanitize_filename(filename, default_stem="image", ext=ext_for_mime(mime))
        return PreparedMedia(data, mime, filename, "image", None)

    if plan.transform == "transcode_opus":
        data = _transcode(path, transcoder)
        if data is None:
            return _fallback_document(path, plan, REASON_UNSUPPORTED)
        return PreparedMedia(data, "audio/ogg", plan.filename, "audio", None)

    raise ValueError(f"unknown transform {plan.transform!r}")


def _read_limited(path: str, kind: str, mime: str) -> bytes:
    limit = min(SEND_LIMITS.get(kind, MAX_UPLOAD_BYTES), upload_limit(mime))
    with open(path, "rb") as fh:
        data = fh.read(limit + 1)
        if len(data) > limit:
            fh.seek(0, os.SEEK_END)
            raise MediaTooBig(fh.tell(), limit, kind)
    if not data:
        raise EmptyMedia()
    return data


def _fallback_document(path: str, plan: OutboundPlan, reason: str) -> PreparedMedia:
    filename = plan.source_filename or sanitize_filename(plan.filename, default_stem="file")
    data = _read_limited(path, "document", OCTET_STREAM)
    return PreparedMedia(data, OCTET_STREAM, filename, "document", downgrade_note(reason))


def _transcode(path: str, transcoder: Callable[[str], str | None] | None) -> bytes | None:
    if transcoder is None:
        return None
    out: str | None = None
    try:
        try:
            out = transcoder(path)
        except Exception:
            return None
        if not out or _same_file(out, path):
            out = None  # never delete (or trust) the source itself
            return None
        with open(out, "rb") as fh:
            data = fh.read(SEND_LIMITS["audio"] + 1)
        if not data or len(data) > SEND_LIMITS["audio"]:
            return None
        return data
    except OSError:
        return None
    finally:
        if out:
            try:
                os.unlink(out)
            except OSError:
                pass


def _same_file(a: str, b: str) -> bool:
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except (OSError, ValueError):
        return True


def _convert_image(path: str, transform: str) -> tuple[bytes, str]:
    """PNG or JPEG bytes of the image, at most 25 MP and within the image limit. Raises on any failure."""
    from PIL import Image, ImageOps  # lazy: Hermes core dependency, not ours

    limit = SEND_LIMITS["image"]
    with Image.open(path) as src:
        width, height = src.size
        if width <= 0 or height <= 0 or width * height > _MAX_DECODE_PIXELS:
            raise _ConversionFailed("image dimensions out of range")
        if getattr(src, "n_frames", 1) > 1:
            src.seek(0)
        if width * height > MAX_IMAGE_PIXELS:
            scale = math.sqrt(MAX_IMAGE_PIXELS / (width * height))
            src.thumbnail((max(1, int(width * scale)), max(1, int(height * scale))), Image.Resampling.LANCZOS)
        src.load()
        image = ImageOps.exif_transpose(src) or src
        image = _normalise_mode(image)
        if transform == "to_png":
            out = _save(image, "PNG")
            if len(out) <= limit:
                return out, "image/png"
        return _jpeg_ladder(image, limit), "image/jpeg"


def _normalise_mode(image):  # -> PIL.Image.Image, 8-bit RGB or RGBA
    mode = image.mode
    if mode in ("RGB", "RGBA"):
        return image
    if mode in ("I", "I;16", "I;16B", "I;16L", "I;16N", "F"):
        image = image.convert("I").point(lambda v: v * (1 / 256)).convert("L")
        return image.convert("RGB")
    has_alpha = mode in ("LA", "PA", "La", "RGBa") or "transparency" in image.info
    return image.convert("RGBA" if has_alpha else "RGB")


def _save(image, fmt: str, **params) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format=fmt, **params)
    return buf.getvalue()


def _jpeg_ladder(image, limit: int) -> bytes:
    from PIL import Image

    if image.mode != "RGB":
        background = Image.new("RGB", image.size, (255, 255, 255))
        rgba = image.convert("RGBA")
        background.paste(rgba, mask=rgba.getchannel("A"))
        image = background
    for _ in range(8):
        for quality in (85, 72, 60):
            out = _save(image, "JPEG", quality=quality, optimize=True)
            if len(out) <= limit:
                return out
        width, height = image.size
        if width < 64 or height < 64:
            break
        image = image.resize((max(1, int(width * 0.75)), max(1, int(height * 0.75))), Image.Resampling.LANCZOS)
    raise _ConversionFailed("could not get the image under the size limit")


# --------------------------------------------------------------------------- outbound: denylist

_DENIED_NAMES = frozenset({"id_rsa", "id_ed25519", "id_ecdsa", "id_dsa", ".git-credentials", ".netrc"})


def _denied_basename(name: str) -> bool:
    # Windows opens "id_rsa." / "id_rsa::$DATA" / ".ENV" as the same file, so compare the normalised form.
    base = name.split(":", 1)[0].rstrip(" .").casefold()
    return base in _DENIED_NAMES or base == ".env" or base.startswith(".env.") or base.endswith(".pem")


def is_denied_path(path: str, *, extra_dirs: Iterable[str] = ()) -> bool:
    """The plugin's narrow never-upload list (D6), on top of Hermes's own delivery policy.

    Denies ``.env`` / ``.env.*``, ``*.pem``, ``id_rsa`` / ``id_ed25519`` / ``id_ecdsa`` / ``id_dsa`` (not their
    ``.pub``), ``.git-credentials``, ``.netrc``, and anything under ``extra_dirs``. Both the given name and the
    symlink-resolved target are checked. Fails closed (True) when the path cannot be resolved.
    """
    try:
        raw = os.fspath(path)
        if not isinstance(raw, str) or "\x00" in raw:
            return True
        resolved = os.path.realpath(raw)
        if _denied_basename(os.path.basename(raw)) or _denied_basename(os.path.basename(resolved)):
            return True
        target = os.path.normcase(resolved)
        roots = [os.path.normcase(os.path.realpath(os.fspath(d))) for d in extra_dirs]
    except (OSError, ValueError, TypeError):
        return True
    for root in roots:
        try:
            if os.path.commonpath([target, root]) == root:
                return True
        except ValueError:  # different drives (Windows), or mixed absolute/relative: not under it
            continue
    return False
