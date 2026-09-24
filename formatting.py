"""Markdown → WhatsApp text formatting.

Adapted from ``gateway/platforms/whatsapp_common.py`` (``WhatsAppBehaviorMixin.format_message``)
in NousResearch/hermes-agent. Vendored as a plain function because the upstream version is an
instance method on a mixin whose other behaviour (self-chat reply prefix, own access policy)
must not apply here. Changed: italics and ``__bold__`` require word boundaries, so identifiers
such as ``__init__.py`` and expressions such as ``5*2*3`` pass through unchanged.

Portions Copyright (c) 2025 Nous Research, used under the MIT License:

    Permission is hereby granted, free of charge, to any person obtaining a copy of this
    software and associated documentation files (the "Software"), to deal in the Software
    without restriction, including without limitation the rights to use, copy, modify, merge,
    publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons
    to whom the Software is furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in all copies or
    substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
    INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR
    PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE
    FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
    OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
    DEALINGS IN THE SOFTWARE.
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
    result = re.sub(r"(?<![\w*])\*(?!\s|\*)([^*\n]*?\S[^*\n]*?)\*(?![\w*])", r"_\1_", result)
    result = re.sub(r"\*\*(.+?)\*\*", r"*\1*", result)
    result = re.sub(r"(?<![\w.])__(?=\S)(.+?)(?<=\S)__(?=[\s,;:!?)]|$)", r"*\1*", result)
    result = re.sub(r"~~(.+?)~~", r"~\1~", result)
    result = re.sub(r"^#{1,6}\s+(.+)$", _header_to_bold, result, flags=re.MULTILINE)
    result = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", result)
    for tag, saved in (("FENCE", fences), ("CODE", codes)):
        for i, original in enumerate(saved):
            result = result.replace(f"\x00{tag}{i}\x00", original)
    return result
