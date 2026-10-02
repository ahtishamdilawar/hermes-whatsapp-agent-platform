"""Creator-only display metadata; no live API or installed state is used."""

import json

import pytest
from gateway.config import PlatformConfig
from wap_helpers import CREATOR, text_message, updates_response
from wap_plugin_under_test.adapter import WhatsAppAgentPlatformAdapter
from wap_plugin_under_test.client import parse_updates
from wap_plugin_under_test.state import PollState


def test_creator_name_roundtrip_and_legacy(tmp_path):
    path = tmp_path / "state.json"
    state = PollState(path, next_offset=42, creator=CREATOR)
    state.creator_name = "Alex"
    state.creator_name_id = CREATOR
    state.save()
    loaded = PollState.load(path)
    assert getattr(loaded, "creator_name", None) == "Alex"
    assert loaded.creator_name_id == CREATOR
    data = json.loads(path.read_text())
    del data["creator_name"]
    del data["creator_name_id"]
    path.write_text(json.dumps(data))
    legacy = PollState.load(path)
    assert legacy.creator_name is None
    assert legacy.creator_name_id is None
    assert legacy.creator == CREATOR and legacy.next_offset == 42


@pytest.mark.asyncio
async def test_confirmed_creator_name_survives_restart_with_omitted_profile(tmp_path, meta):
    events = []

    async def capture(event):
        events.append(event)

    path = tmp_path / "state.json"
    for index, contacts in enumerate(([{"wa_id": CREATOR, "profile": {"name": "Alex"}}], []), 1):
        adapter = WhatsAppAgentPlatformAdapter(PlatformConfig())
        adapter._state = PollState.load(path)
        adapter._client = meta.client(unthrottled=True)
        adapter.handle_message = capture
        try:
            await adapter._process(
                parse_updates(
                    updates_response(
                        text_message(f"wamid.{index}", "hello"),
                        next_offset=index,
                        contacts=contacts,
                    )
                )
            )
            assert events[-1].user_name == "Alex"
            assert events[-1].source.user_name == "Alex"
            assert events[-1].source.chat_name == "Alex"
            assert (await adapter.get_chat_info(CREATOR))["name"] == "Alex"
            assert PollState.load(path).creator_name == "Alex"
        finally:
            await adapter.disconnect()


@pytest.mark.asyncio
async def test_creator_change_drops_previous_name(tmp_path, meta):
    adapter = WhatsAppAgentPlatformAdapter(PlatformConfig())
    adapter._state = PollState(tmp_path / "state.json", creator=CREATOR, creator_name="Alex", creator_name_id=CREATOR)
    adapter._client = meta.client(unthrottled=True)
    events = []

    async def capture(event):
        events.append(event)

    adapter.handle_message = capture
    try:
        await adapter._process(
            parse_updates(
                updates_response(
                    text_message("wamid.changed", "hello", sender="user:new"),
                    next_offset=1,
                    contacts=[],
                )
            )
        )
        assert events[-1].user_name is None
        saved = PollState.load(adapter._state.path)
        assert saved.creator == "user:new"
        assert saved.creator_name is None and saved.creator_name_id is None
        assert "Alex" not in adapter._state.path.read_text()
    finally:
        await adapter.disconnect()


@pytest.mark.parametrize(
    "name,owner",
    [
        ("Alex", "user:other"),
        ("Alex", None),
        (123, CREATOR),
        ({"name": "Alex"}, CREATOR),
        ("x" * 257, CREATOR),
        ("bad\nname", CREATOR),
        ("bad\u202ename", CREATOR),
        ("", CREATOR),
    ],
)
def test_invalid_optional_name_does_not_reset_poll_state(tmp_path, name, owner):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "next_offset": 42,
                "start_timestamp": 1,
                "creator": CREATOR,
                "creator_name": name,
                "creator_name_id": owner,
            }
        )
    )
    state = PollState.load(path)
    assert state.creator_name is None and state.creator_name_id is None
    assert state.next_offset == 42 and state.creator == CREATOR
    assert not list(tmp_path.glob("*.corrupt-*"))


@pytest.mark.asyncio
async def test_failed_name_save_keeps_atomic_old_state_and_retries(tmp_path, meta, monkeypatch):
    from wap_plugin_under_test import state as state_module

    path = tmp_path / "state.json"
    state = PollState(path, creator=CREATOR, creator_name="Alex", creator_name_id=CREATOR)
    state.save()
    original = path.read_bytes()
    adapter = WhatsAppAgentPlatformAdapter(PlatformConfig())
    adapter._state = state
    adapter._client = meta.client(unthrottled=True)

    async def capture(event):
        pass

    def fail_replace(*args):
        raise OSError("test disk failure")

    adapter.handle_message = capture
    page = parse_updates(
        updates_response(
            text_message("wamid.save", "hi"),
            next_offset=1,
            contacts=[{"wa_id": CREATOR, "profile": {"name": "Renée"}}],
        )
    )
    try:
        with monkeypatch.context() as patcher:
            patcher.setattr(state_module.os, "replace", fail_replace)
            with pytest.raises(OSError):
                await adapter._process(page)
        assert path.read_bytes() == original
        assert not list(tmp_path.glob(".state-*.tmp"))
        assert adapter._state.creator_name == "Renée"
        await adapter._process(page)
        assert PollState.load(path).creator_name == "Renée"
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_restored_name_only_matches_creator_before_any_message(tmp_path):
    adapter = WhatsAppAgentPlatformAdapter(PlatformConfig())
    adapter._state = PollState(tmp_path / "state.json", creator=CREATOR, creator_name="Alex", creator_name_id=CREATOR)
    assert (await adapter.get_chat_info(CREATOR))["name"] == "Alex"
    assert (await adapter.get_chat_info("user:other"))["name"] == "WhatsApp"
    adapter._state.creator = "user:new"
    assert (await adapter.get_chat_info("user:new"))["name"] == "WhatsApp"
    assert (await adapter.get_chat_info(CREATOR))["name"] == "WhatsApp"


@pytest.mark.parametrize("name,owner", [("Alex", "user:other"), ("bad\nname", CREATOR), ("x" * 257, CREATOR)])
def test_save_never_serializes_invalid_or_mismatched_name(tmp_path, name, owner):
    state = PollState(tmp_path / "state.json", creator=CREATOR, creator_name=name, creator_name_id=owner)
    state.save()
    raw = json.loads(state.path.read_text())
    assert raw.get("creator_name") is None and raw.get("creator_name_id") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("allow", [None, "list", "all"])
async def test_other_contacts_and_noncreators_are_never_persisted(tmp_path, meta, monkeypatch, allow):
    other = "user:other"
    if allow == "list":
        monkeypatch.setenv("WHATSAPP_AGENT_PLATFORM_ALLOWED_USERS", other)
    elif allow == "all":
        monkeypatch.setenv("WHATSAPP_AGENT_PLATFORM_ALLOW_ALL_USERS", "true")
    adapter = WhatsAppAgentPlatformAdapter(PlatformConfig())
    adapter._state = PollState(tmp_path / "state.json")
    adapter._client = meta.client(unthrottled=True)
    events = []

    async def capture(event):
        events.append(event)

    adapter.handle_message = capture
    meta.not_creator_wamids.add("wamid.other")
    contacts = [
        {"wa_id": other, "profile": {"name": "Private Other"}},
        {"wa_id": CREATOR, "profile": {"name": "Contact Only"}},
    ]
    try:
        await adapter._process(
            parse_updates(
                updates_response(
                    text_message("wamid.other", "hi", sender=other),
                    next_offset=1,
                    contacts=contacts,
                )
            )
        )
        assert bool(events) == (allow is not None)
        raw = adapter._state.path.read_text()
        assert "Private Other" not in raw and "Contact Only" not in raw
        assert PollState.load(adapter._state.path).creator_name is None
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_name_updates_and_invalid_names_do_not_replace_saved_name(tmp_path, meta):
    adapter = WhatsAppAgentPlatformAdapter(PlatformConfig())
    adapter._state = PollState(tmp_path / "state.json")
    adapter._client = meta.client(unthrottled=True)

    async def capture(event):
        pass

    adapter.handle_message = capture
    try:
        for index, name in enumerate(["Alex", "Renée", "", "bad\nname", "x" * 257], 1):
            await adapter._process(
                parse_updates(
                    updates_response(
                        text_message(f"wamid.update{index}", "hi"),
                        next_offset=index,
                        contacts=[{"wa_id": CREATOR, "profile": {"name": name}}],
                    )
                )
            )
            expected = "Alex" if index == 1 else "Renée"
            assert PollState.load(adapter._state.path).creator_name == expected
            assert json.loads(adapter._state.path.read_text())["creator_name"] == expected
    finally:
        await adapter.disconnect()
