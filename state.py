"""Durable polling state for one agent API key.

Stored at ``<HERMES_HOME>/platforms/whatsapp_agent_platform/<key-fingerprint>.json``
(mode 0600, atomic replace). The file never contains the key itself.

* ``next_offset``      Meta's opaque cursor, passed back unchanged.
* ``start_timestamp``  first activation; retained backlog older than this is skipped.
* ``recent_message_ids`` window of handled inbound wamids (dedup across restarts).
* ``creator``          the ``user:<id>`` that talks to this agent (trust on first use).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

RECENT_IDS_MAX = 512


def key_fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def state_path(hermes_home: Path, api_key: str) -> Path:
    return hermes_home / "platforms" / "whatsapp_agent_platform" / f"{key_fingerprint(api_key)}.json"


@dataclass
class PollState:
    path: Path
    next_offset: int = 0
    start_timestamp: int = field(default_factory=lambda: int(time.time()))
    recent_message_ids: list[str] = field(default_factory=list)
    creator: str | None = None

    @classmethod
    def load(cls, path: Path) -> PollState:
        """Load state; a corrupt file is quarantined and replaced by a fresh start."""
        if not path.exists():
            return cls(path=path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            offset = data["next_offset"]
            start = data["start_timestamp"]
            if type(offset) is not int or offset < 0 or type(start) is not int or start < 0:
                raise ValueError("offset/start_timestamp must be non-negative integers")
            ids = [v for v in data.get("recent_message_ids", []) if isinstance(v, str)]
            creator = data.get("creator")
            if not isinstance(creator, str) or not creator.startswith("user:"):
                creator = None
            return cls(
                path=path,
                next_offset=offset,
                start_timestamp=start,
                recent_message_ids=ids[-RECENT_IDS_MAX:],
                creator=creator,
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            quarantine = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
            try:
                os.replace(path, quarantine)
            except OSError:
                quarantine = path
            logger.warning(
                "WhatsApp Agent Platform: unreadable state file (%s); moved to %s and starting "
                "fresh (retained backlog before now is skipped)",
                type(exc).__name__,
                quarantine.name,
            )
            return cls(path=path)

    def remember(self, message_id: str) -> None:
        self.recent_message_ids.append(message_id)
        del self.recent_message_ids[:-RECENT_IDS_MAX]

    def seen(self, message_id: str) -> bool:
        return message_id in self.recent_message_ids

    def save(self) -> None:
        """Atomic write: temp file in the same dir, fsync, ``os.replace``."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "next_offset": self.next_offset,
            "start_timestamp": self.start_timestamp,
            "recent_message_ids": self.recent_message_ids[-RECENT_IDS_MAX:],
            "creator": self.creator,
        }
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-", suffix=".tmp")
        tmp_path = Path(tmp)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(tmp_path, 0o600)
            except OSError:
                pass
            os.replace(tmp_path, self.path)
        finally:
            tmp_path.unlink(missing_ok=True)


def read_creator(hermes_home: Path, api_key: str) -> str | None:
    """Creator id recorded by the gateway (used by out-of-process cron sends)."""
    path = state_path(hermes_home, api_key)
    try:
        creator = json.loads(path.read_text(encoding="utf-8")).get("creator")
    except (OSError, ValueError, AttributeError):
        return None
    return creator if isinstance(creator, str) and creator.startswith("user:") else None
