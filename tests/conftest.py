"""Fixtures: isolate Hermes state per test, register the platform id, fake Meta's API."""

from __future__ import annotations

import os

import pytest
from wap_helpers import API_KEY, FakeMeta, load_plugin


@pytest.fixture(scope="session", autouse=True)
def _platform_registered():
    """``Platform("whatsapp_agent_platform")`` only resolves once the name is registered."""
    from gateway.platform_registry import PlatformEntry, platform_registry

    platform_registry.register(
        PlatformEntry(
            name="whatsapp_agent_platform",
            label="WhatsApp Agent Platform",
            adapter_factory=lambda cfg: None,
            check_fn=lambda: True,
        )
    )
    yield


@pytest.fixture(autouse=True)
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("HERMES_DISABLE_LAZY_INSTALLS", "1")
    for key in [k for k in os.environ if k.startswith(("WHATSAPP_AGENT_PLATFORM_", "GATEWAY_ALLOW"))]:
        monkeypatch.delenv(key)
    import agent.secret_scope as secret_scope

    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", False, raising=False)
    monkeypatch.setattr(secret_scope, "_AUTO_PINNED_HOME", None, raising=False)
    load_plugin().adapter.WhatsAppAgentPlatformAdapter._active_keys.clear()
    return home


@pytest.fixture
def plugin():
    return load_plugin()


@pytest.fixture
def api_key(monkeypatch):
    monkeypatch.setenv("WHATSAPP_AGENT_PLATFORM_API_KEY", API_KEY)
    return API_KEY


@pytest.fixture
def meta(plugin, monkeypatch) -> FakeMeta:
    fake = FakeMeta()

    def make_client(key, base_url=None, **kwargs):
        # Tolerates constructor kwargs added later (timeouts, allowlists, ...): they reach the real client.
        if base_url is not None:
            kwargs["base_url"] = base_url
        kwargs.pop("http", None)
        return fake.client(key, unthrottled=True, **kwargs)

    monkeypatch.setattr(plugin.adapter, "AgentPlatformClient", make_client)
    return fake
