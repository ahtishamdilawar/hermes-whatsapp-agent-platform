"""hermes_compat.py: one place for Hermes helpers that drift between v2026.9.21 and main."""

from __future__ import annotations

import asyncio
import inspect

import pytest
from wap_plugin_under_test import hermes_compat as compat


def test_never_uses_cache_media_bytes():
    source = inspect.getsource(compat)
    assert "cache_media_bytes(" not in source
    assert "cache_media_bytes_async" not in source.replace("``cache_media_bytes[_async]``", "")


@pytest.mark.asyncio
async def test_record_and_lookup_round_trip(tmp_path):
    media = tmp_path / "photo.jpg"
    media.write_bytes(b"\xff\xd8\xff")
    await compat.record_media("user:1", "wamid.A", [(str(media), "image/jpeg")])
    assert await compat.lookup_media("user:1", "wamid.A") == [(str(media), "image/jpeg")]
    assert await compat.lookup_media("user:1", "wamid.other") == []


@pytest.mark.asyncio
async def test_works_without_async_store_functions(monkeypatch, tmp_path):
    # Hermes v2026.9.21 has no record_async / record_media_async.
    for name in ("record_async", "record_media_async"):
        monkeypatch.delattr(compat._rss, name, raising=False)
    media = tmp_path / "a.ogg"
    media.write_bytes(b"OggS")
    await compat.record_media("user:1", "wamid.B", [(str(media), "audio/ogg")])
    assert await compat.lookup_media("user:1", "wamid.B") == [(str(media), "audio/ogg")]


@pytest.mark.asyncio
async def test_concurrent_records_are_not_lost(tmp_path):
    files = []
    for i in range(20):
        path = tmp_path / f"{i}.jpg"
        path.write_bytes(b"x")
        files.append(path)
    await asyncio.gather(
        *(compat.record_media("user:1", f"wamid.{i}", [(str(p), "image/jpeg")]) for i, p in enumerate(files))
    )
    for i, path in enumerate(files):
        assert await compat.lookup_media("user:1", f"wamid.{i}") == [(str(path), "image/jpeg")]


@pytest.mark.asyncio
async def test_store_failures_are_swallowed(monkeypatch):
    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(compat._rss, "record_media", boom)
    monkeypatch.setattr(compat._rss, "lookup_media", boom)
    await compat.record_media("user:1", "wamid.C", [("/x", "image/jpeg")])
    assert await compat.lookup_media("user:1", "wamid.C") == []


@pytest.mark.asyncio
async def test_empty_inputs_are_no_ops():
    await compat.record_media("user:1", "wamid.D", [])
    await compat.record_media("", "wamid.D", [("/x", "image/jpeg")])
    assert await compat.lookup_media("", "wamid.D") == []


@pytest.mark.parametrize("value, expected", [(10_000, 10_000), (0, 0), (-5, 0)])
def test_inbound_media_max_bytes(monkeypatch, value, expected):
    monkeypatch.setattr(compat, "get_inbound_media_max_bytes", lambda: value)
    assert compat.inbound_media_max_bytes() == expected


def test_inbound_media_max_bytes_survives_config_errors(monkeypatch):
    def boom():
        raise RuntimeError("config")

    monkeypatch.setattr(compat, "get_inbound_media_max_bytes", boom)
    assert compat.inbound_media_max_bytes() == 0
