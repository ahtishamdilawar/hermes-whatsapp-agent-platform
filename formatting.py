"""Markdown → WhatsApp text formatting.

Adapted from ``gateway/platforms/whatsapp_common.py`` (``WhatsAppBehaviorMixin.format_message``)
in NousResearch/hermes-agent, Copyright (c) 2025 Nous Research, MIT License.
Vendored as a plain function because the upstream version is an instance method on a
mixin whose other behaviour (self-chat reply prefix, own access policy) must not apply here.
"""

from __future__ import annotations

import re

_INVISIBLE_RE = re.compile("[\u200b\u2060\u2063\ufeff]")
_ODD_SPACE_RE = re.compile("[\u00a0\u1680\u180e\u2000-\u200a\u202f\u205f\u3000]")


def _stash(pattern: str, text: str, tag: str) -> tuple[str, list[str]]:
    """Replace each ``pattern`` match with a ``\\x00<tag><n>\\x00`` placeholder."""
    saved: list[str] = []

    def keep(m: re.Match) -> str:
        saved.append(m.group(0))
        return f"\x00{tag}{len(saved) - 1}\x00"

    return re.sub(pattern, keep, text), saved


def _header_to_bold(m: re.Match) -> str:
    inner = m.group(1).strip()
    while len(inner) > 1 and inner.startswith("*") and inner.endswith("*"):
        inner = inner[1:-1].strip()
    return f"*{inner}*"


def sanitize(text: str) -> str:
    """Drop zero-width format characters and normalise odd unicode spaces."""
    return _ODD_SPACE_RE.sub(" ", _INVISIBLE_RE.sub("", text))


def to_whatsapp(text: str) -> str:
    """``**bold**``→``*bold*``, ``*it*``→``_it_``, ``~~s~~``→``~s~``, ``# H``→``*H*``,
    ``[t](u)``→``t (u)``; fenced and inline code are left untouched."""
    if not text:
        return text
    result, fences = _stash(r"```[\s\S]*?```", sanitize(text), "FENCE")
    result, codes = _stash(r"`[^`\n]+`", result, "CODE")
    result = re.sub(r"(?<!\*)\*(?!\s|\*)([^*\n]*?\S[^*\n]*?)\*(?!\*)", r"_\1_", result)
    result = re.sub(r"\*\*(.+?)\*\*", r"*\1*", result)
    result = re.sub(r"__(.+?)__", r"*\1*", result)
    result = re.sub(r"~~(.+?)~~", r"~\1~", result)
    result = re.sub(r"^#{1,6}\s+(.+)$", _header_to_bold, result, flags=re.MULTILINE)
    result = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", result)
    for tag, saved in (("FENCE", fences), ("CODE", codes)):
        for i, original in enumerate(saved):
            result = result.replace(f"\x00{tag}{i}\x00", original)
    return result
