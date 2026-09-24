"""Load the repo through Hermes's real plugin loader, as ``hermes plugins install`` lays it out."""

from __future__ import annotations

import shutil

from wap_helpers import ROOT


def test_real_loader_registers_the_platform(hermes_home):
    target = hermes_home / "plugins" / "whatsapp-agent-platform"
    shutil.copytree(
        ROOT,
        target,
        ignore=shutil.ignore_patterns(".git", ".github", "tests", "__pycache__", ".venv", "*.pyc"),
    )
    (hermes_home / "config.yaml").write_text("plugins:\n  enabled:\n    - whatsapp-agent-platform\n", encoding="utf-8")

    import hermes_cli.plugins as plugins
    from gateway.platform_registry import platform_registry

    plugins._reset_plugin_managers_for_tests()
    try:
        plugins.discover_plugins(force=True)
        entry = platform_registry.get("whatsapp_agent_platform")
        assert entry is not None and entry.source == "plugin"
        assert entry.plugin_name == "whatsapp-agent-platform"
        assert entry.cron_deliver_env_var == "WHATSAPP_AGENT_PLATFORM_HOME_CHANNEL"
        assert callable(entry.standalone_sender_fn)
    finally:
        plugins._reset_plugin_managers_for_tests()
