"""Ownership is released after polling stops, even when HTTP close fails."""

import asyncio

import httpx
import pytest
from gateway.config import PlatformConfig
from wap_helpers import load_plugin


class CloseTransport(httpx.AsyncBaseTransport):
    def __init__(self, fault):
        self.fault = fault
        self.closing = asyncio.Event()
        self.release = asyncio.Event()
        self.error = OSError("simulated transport close failure")

    async def handle_async_request(self, request):
        return httpx.Response(204)

    async def aclose(self):
        self.closing.set()
        if self.fault == "error":
            raise self.error
        if self.fault == "cancel":
            await self.release.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["none", "error", "cancel"])
async def test_disconnect_releases_ownership_after_client_close(monkeypatch, api_key, fault):
    mod = load_plugin().adapter
    real_http_client = httpx.AsyncClient
    transport = CloseTransport(fault)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_http_client(transport=transport, **kwargs))
    first = mod.WhatsAppAgentPlatformAdapter(PlatformConfig(enabled=True))
    replacement = mod.WhatsAppAgentPlatformAdapter(PlatformConfig(enabled=True))
    cleanup = None
    try:
        assert await first.connect()
        fingerprint = first._fingerprint
        poll = first._poll_task
        if fault == "cancel":
            cleanup = asyncio.create_task(first.disconnect())
            await asyncio.wait_for(transport.closing.wait(), 3)
            assert poll.done()
            cleanup.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cleanup
        elif fault == "error":
            with pytest.raises(OSError) as caught:
                await first.disconnect()
            assert caught.value is transport.error
        else:
            await first.disconnect()
        assert poll.done() and first._poll_task is None and not first._running
        assert await replacement.connect(), "stopped adapter must not block its replacement"
        assert first._client is None
        assert first._fingerprint is None
        assert first._platform_lock_identity is None
        # Repeated cleanup of the old adapter must not release the new owner's guard.
        await first.disconnect()
        assert fingerprint in replacement._active_keys
        assert replacement._poll_task is not None and not replacement._poll_task.done()
    finally:
        transport.fault = "none"
        transport.release.set()
        if cleanup is not None and not cleanup.done():
            await cleanup
        await first.disconnect()
        await replacement.disconnect()


@pytest.mark.asyncio
async def test_disconnect_keeps_ownership_until_active_poll_stops(monkeypatch, api_key):
    mod = load_plugin().adapter
    real_http_client = httpx.AsyncClient
    transport = CloseTransport("none")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_http_client(transport=transport, **kwargs))
    first = mod.WhatsAppAgentPlatformAdapter(PlatformConfig(enabled=True))
    replacement = mod.WhatsAppAgentPlatformAdapter(PlatformConfig(enabled=True))
    started, stopping, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def slow_poll():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopping.set()
            await finish.wait()

    monkeypatch.setattr(first, "_poll_loop", slow_poll)
    cleanup = None
    try:
        assert await first.connect()
        poll = first._poll_task
        fingerprint = first._fingerprint
        await asyncio.wait_for(started.wait(), 3)
        cleanup = asyncio.create_task(first.disconnect())
        await asyncio.wait_for(stopping.wait(), 3)
        assert not poll.done() and not cleanup.done()
        assert not transport.closing.is_set()
        assert fingerprint in first._active_keys
        assert first._platform_lock_identity == api_key
        assert not await replacement.connect()
        assert replacement.fatal_error_code == "whatsapp_agent_platform_lock"
        finish.set()
        await asyncio.wait_for(cleanup, 3)
        assert poll.done()
        assert await replacement.connect()
    finally:
        finish.set()
        if cleanup is not None:
            await cleanup
        await first.disconnect()
        await replacement.disconnect()
