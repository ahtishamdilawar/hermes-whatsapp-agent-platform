"""Hermes helpers the media code relies on, kept in one place so version drift is handled once.

Supported range: Hermes v2026.9.21 (0.21.4) through current ``main``.

* Only the kind-specific cache helpers are used. ``cache_media_bytes[_async]`` is avoided on purpose:
  ``CachedMedia.path`` changed meaning on main (agent-visible path before, host path after).
* ``rich_sent_store`` is used through its sync functions on a worker thread. The ``*_async`` variants and the
  store's own write lock don't exist in v2026.9.21, so writes are serialised here.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Iterable

from gateway.platforms.base import (
    cache_audio_from_bytes_async,
    cache_document_from_bytes_async,
    cache_image_from_bytes_async,
    cache_image_from_url,
    cache_video_from_bytes_async,
    get_inbound_media_max_bytes,
    transcode_to_ogg_opus,
)

try:
    from gateway import rich_sent_store as _rss
except ImportError:  # pragma: no cover - present in every supported Hermes
    _rss = None

logger = logging.getLogger(__name__)

__all__ = [
    "cache_audio_from_bytes_async",
    "cache_document_from_bytes_async",
    "cache_image_from_bytes_async",
    "cache_image_from_url",
    "cache_video_from_bytes_async",
    "inbound_media_max_bytes",
    "lookup_media",
    "record_media",
    "transcode_to_ogg_opus",
]

_RSS_LOCK = threading.Lock()  # a thread lock: the store is written from worker threads


def inbound_media_max_bytes() -> int:
    """Hermes's ``gateway.max_inbound_media_bytes``; ``0`` means Hermes applies no cap."""
    try:
        return max(0, int(get_inbound_media_max_bytes()))
    except Exception:  # unreadable config must not break inbound handling
        return 0


def _record_media_sync(chat_id: str, message_id: str, media: list[tuple[str, str]]) -> None:
    with _RSS_LOCK:
        _rss.record_media(chat_id, message_id, media)


def _lookup_media_sync(chat_id: str, message_id: str) -> list[tuple[str, str]]:
    with _RSS_LOCK:
        return list(_rss.lookup_media(chat_id, message_id))


async def record_media(chat_id: str, message_id: str, media: Iterable[tuple[str, str]]) -> None:
    """Remember ``(path, mime)`` attachments of a message so a later quote can re-attach them. Never raises."""
    pairs = [(str(path), str(mime or "")) for path, mime in media if path]
    if _rss is None or not pairs or not chat_id or not message_id:
        return
    try:
        await asyncio.to_thread(_record_media_sync, chat_id, message_id, pairs)
    except Exception as exc:
        logger.debug("rich_sent_store.record_media failed (%s)", type(exc).__name__)


async def lookup_media(chat_id: str, message_id: str) -> list[tuple[str, str]]:
    """Attachments recorded for a quoted message whose files still exist; ``[]`` on any failure."""
    if _rss is None or not chat_id or not message_id:
        return []
    try:
        return await asyncio.to_thread(_lookup_media_sync, chat_id, message_id)
    except Exception as exc:
        logger.debug("rich_sent_store.lookup_media failed (%s)", type(exc).__name__)
        return []
