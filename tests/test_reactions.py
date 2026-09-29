"""Inbound reactions: forwarded to Hermes hooks only (no agent turn, no /statuses, no creator pinning)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from wap_helpers import API_KEY, CREATOR, reaction_message, text_message, until, updates_response
from wap_plugin_under_test import adapter as mod
from wap_plugin_under_test.state import PollState, state_path

OTHER = "user:777"
LAUGH = "\U0001f602"
FAMILY = "\U0001f468\u200d\U0001f469\u200d\U0001f467"  # a ZWJ sequence must survive sanitising


def make_adapter():
    from gateway.config import PlatformConfig

    a = mod.WhatsAppAgentPlatformAdapter(PlatformConfig(enabled=True))
    a._poll_min_interval = 0.01
    a._poll_timeout = 0
    a._backoff_base = 0.01
    a.handle_message = AsyncMock()
    a._notify_fatal_error = AsyncMock()
    return a


class Hooks:
    """Records what the adapter hands to the reaction handler and the platform-event handler."""

    def __init__(self, a, *, fail: bool = False) -> None:
        self.reactions: list[dict] = []
        self.events: list[tuple[dict, object]] = []
        self.fail = fail
        a.set_reaction_handler(self._on_reaction)
        a.set_platform_event_handler(self._on_event)

    async def _on_reaction(self, ctx: dict) -> None:
        self.reactions.append(ctx)
        if self.fail:
            raise RuntimeError("hook exploded")

    async def _on_event(self, event: dict, source) -> None:
        self.events.append((event, source))
        if self.fail:
            raise RuntimeError("plugin exploded")


def saved_state() -> PollState:
    from hermes_constants import get_hermes_home

    return PollState.load(state_path(get_hermes_home(), API_KEY))


async def deliver(meta, *messages, offset: int) -> None:
    meta.queue("updates", updates_response(*messages, next_offset=offset))
    await until(lambda: saved_state().next_offset == offset)


async def connect_with_creator(meta, hooks_fail: bool = False):
    """A connected adapter whose creator Meta already confirmed (one text message)."""
    a = make_adapter()
    hooks = Hooks(a, fail=hooks_fail)
    assert await a.connect()
    await deliver(meta, text_message("wamid.t0", "hi"), offset=1)
    assert a._state.creator == CREATOR
    return a, hooks


@pytest.mark.asyncio
async def test_creator_reaction_reaches_both_hooks_without_a_turn_or_status(meta, api_key):
    a, hooks = await connect_with_creator(meta)
    statuses = len(meta.calls("statuses"))
    await deliver(meta, reaction_message("wamid.r1", "wamid.out9", LAUGH), offset=2)
    await a.disconnect()
    assert a.handle_message.await_count == 1  # only the text message
    assert len(meta.calls("statuses")) == statuses  # no read receipt, no typing for a reaction
    assert meta.calls("messages") == []
    [ctx] = hooks.reactions
    assert ctx["event_name"] == "reaction:added" and ctx["reaction"] == LAUGH
    assert ctx["platform"] == "whatsapp_agent_platform" and ctx["user_id"] == CREATOR
    assert ctx["channel_id"] == CREATOR and ctx["message_ts"] == "wamid.out9" and ctx["item_type"] == "message"
    assert set(ctx) >= {"item_user_id", "team_id", "event_ts", "raw_event"}
    assert ctx["raw_event"] == {
        "id": "wamid.r1",
        "type": "reaction",
        "timestamp": ctx["event_ts"],
        "reaction": {"message_id": "wamid.out9", "emoji": LAUGH},
    }
    [(event, source)] = hooks.events
    assert event == {
        "platform": "whatsapp_agent_platform",
        "event_type": "reaction",
        "payload": {
            "emojis": [LAUGH],
            "custom_emoji_ids": [],
            "chat_id": CREATOR,
            "message_id": "wamid.out9",
            "thread_id": None,
        },
    }
    assert source.chat_id == CREATOR and source.user_id == CREATOR and source.chat_type == "dm"
    assert saved_state().seen("wamid.r1")


@pytest.mark.asyncio
async def test_reactions_reach_gateway_hooks_through_the_runner(meta, api_key):
    """The runner's own reaction handler fans the dict out to ``reaction:added``/``reaction:removed`` hooks."""
    import functools
    from types import SimpleNamespace

    from gateway.hooks import HookRegistry
    from gateway.run_adapters import GatewayAdapterLifecycleMixin

    registry = HookRegistry()
    fired: list[tuple[str, dict]] = []
    registry._handlers["reaction:*"] = [lambda event_type, ctx: fired.append((event_type, ctx))]
    runner = SimpleNamespace(hooks=registry)
    a = make_adapter()
    a.set_reaction_handler(functools.partial(GatewayAdapterLifecycleMixin._handle_reaction_event, runner))
    assert await a.connect()
    await deliver(meta, text_message("wamid.t0", "hi"), offset=1)
    added, removed = reaction_message("wamid.r1", "wamid.t0"), reaction_message("wamid.r2", "wamid.t0", "")
    await deliver(meta, added, removed, offset=2)
    await a.disconnect()
    assert [(name, ctx["reaction"]) for name, ctx in fired] == [("reaction:added", LAUGH), ("reaction:removed", "")]


@pytest.mark.asyncio
async def test_reaction_to_an_agent_message_names_the_agent(meta, api_key):
    a, hooks = await connect_with_creator(meta)
    sent = await a.send(CREATOR, "the answer")
    await deliver(meta, reaction_message("wamid.r1", sent.message_id), offset=2)
    await deliver(meta, reaction_message("wamid.r2", "wamid.t0"), offset=3)
    await a.disconnect()
    assert [ctx["item_user_id"] for ctx in hooks.reactions] == ["agent", None]


@pytest.mark.parametrize("emoji", ["", None, "   ", 5])
@pytest.mark.asyncio
async def test_empty_or_missing_emoji_is_a_removal(meta, api_key, emoji):
    a, hooks = await connect_with_creator(meta)
    await deliver(meta, reaction_message("wamid.r1", "wamid.t0", emoji), offset=2)
    await a.disconnect()
    [ctx] = hooks.reactions
    assert ctx["event_name"] == "reaction:removed" and ctx["reaction"] == ""
    assert hooks.events[0][0]["payload"]["emojis"] == []


@pytest.mark.asyncio
async def test_emoji_is_sanitised_and_capped(meta, api_key):
    a, hooks = await connect_with_creator(meta)
    hostile = "\u202e" + FAMILY + "\x00\x07" + "x" * 100
    await deliver(meta, reaction_message("wamid.r1", "wamid.t0", hostile), offset=2)
    await deliver(meta, reaction_message("wamid.r2", "wamid.t0", FAMILY), offset=3)
    await a.disconnect()
    first, second = (ctx["reaction"] for ctx in hooks.reactions)
    assert len(first) == mod.MAX_REACTION_CHARS and first.startswith(FAMILY)
    assert not any(ch in first for ch in "\u202e\x00\x07")
    assert second == FAMILY


@pytest.mark.asyncio
async def test_allowlisted_sender_reaction_is_forwarded(meta, api_key, monkeypatch):
    monkeypatch.setenv("WHATSAPP_AGENT_PLATFORM_ALLOWED_USERS", OTHER)
    a, hooks = await connect_with_creator(meta)
    await deliver(meta, reaction_message("wamid.r1", "wamid.t0", sender=OTHER), offset=2)
    await a.disconnect()
    assert [ctx["user_id"] for ctx in hooks.reactions] == [OTHER]
    assert len(hooks.events) == 1


@pytest.mark.asyncio
async def test_stranger_reaction_is_dropped(meta, api_key):
    a, hooks = await connect_with_creator(meta)
    statuses = len(meta.calls("statuses"))
    await deliver(meta, reaction_message("wamid.r1", "wamid.t0", sender=OTHER), offset=2)
    await deliver(meta, reaction_message("wamid.r2", "wamid.t0"), offset=3)  # positive control
    await a.disconnect()
    assert [ctx["user_id"] for ctx in hooks.reactions] == [CREATOR]
    assert len(hooks.events) == 1
    assert len(meta.calls("statuses")) == statuses
    assert saved_state().seen("wamid.r1")


@pytest.mark.asyncio
async def test_reaction_before_the_creator_is_known_is_dropped_and_never_pins(meta, api_key):
    a = make_adapter()
    hooks = Hooks(a)
    assert await a.connect()
    await deliver(meta, reaction_message("wamid.r1", "wamid.x"), offset=1)
    assert saved_state().creator is None and meta.calls("statuses") == []
    await deliver(meta, text_message("wamid.t0", "hi"), reaction_message("wamid.r2", "wamid.t0"), offset=2)
    await a.disconnect()
    assert [ctx["raw_event"]["id"] for ctx in hooks.reactions] == ["wamid.r2"]  # only after Meta confirmed
    assert saved_state().creator == CREATOR


@pytest.mark.asyncio
async def test_failing_hooks_never_break_polling(meta, api_key):
    a, hooks = await connect_with_creator(meta, hooks_fail=True)
    await deliver(meta, reaction_message("wamid.r1", "wamid.t0"), text_message("wamid.t1", "next"), offset=2)
    await deliver(meta, text_message("wamid.t2", "still polling"), offset=3)
    await a.disconnect()
    assert len(hooks.reactions) == 1 and len(hooks.events) == 1  # the platform event still went out
    assert [call.args[0].text for call in a.handle_message.await_args_list] == ["hi", "next", "still polling"]
    assert not a.has_fatal_error


@pytest.mark.asyncio
async def test_redelivered_or_backlog_reaction_is_forwarded_once(meta, api_key):
    import time

    a, hooks = await connect_with_creator(meta)
    reaction = reaction_message("wamid.r1", "wamid.t0")
    old = reaction_message("wamid.r0", "wamid.t0", timestamp=int(time.time()) - 3600)
    await deliver(meta, reaction, old, offset=2)
    await deliver(meta, reaction, reaction_message("wamid.r2", "wamid.t0"), offset=3)
    await a.disconnect()
    assert [ctx["raw_event"]["id"] for ctx in hooks.reactions] == ["wamid.r1", "wamid.r2"]


@pytest.mark.asyncio
async def test_malformed_reaction_is_skipped(meta, api_key):
    a, hooks = await connect_with_creator(meta)
    broken = reaction_message("wamid.r1", "wamid.t0")
    broken["reaction"] = "not an object"
    await deliver(meta, broken, reaction_message("wamid.r2", "wamid.t0"), offset=2)
    await a.disconnect()
    assert [ctx["raw_event"]["id"] for ctx in hooks.reactions] == ["wamid.r2"]


@pytest.mark.parametrize(
    "target",
    [
        "x" * 129,
        "wamid.<script>",
        "wamid.a b",
        "wamid.‮evil",
        "wamid.١٢",  # non-ASCII digits
        "wamid.x\n",
        "",
        5,
        None,
        ["wamid.t0"],
    ],
    ids=["too-long", "markup", "space", "bidi", "arabic-digits", "newline", "empty", "int", "null", "list"],
)
@pytest.mark.asyncio
async def test_reaction_to_an_invalid_message_id_is_dropped(meta, api_key, target):
    a, hooks = await connect_with_creator(meta)
    bad = reaction_message("wamid.r1", "wamid.t0")
    bad["reaction"]["message_id"] = target
    await deliver(meta, bad, reaction_message("wamid.r2", "wamid.t0"), offset=2)
    await a.disconnect()
    assert [ctx["raw_event"]["id"] for ctx in hooks.reactions] == ["wamid.r2"]
    assert len(hooks.events) == 1 and saved_state().seen("wamid.r1")


@pytest.mark.parametrize(
    "target",
    ["wamid.HBgONTA5NzI5MjM1NjQyMTUVEgARGBI5QTJDNEU2RkQ3OEY5MEExQjJDMwA=", "a.b_c-d+e/f=", "w" * 128],
)
@pytest.mark.asyncio
async def test_wamid_shaped_targets_are_accepted(meta, api_key, target):
    a, hooks = await connect_with_creator(meta)
    await deliver(meta, reaction_message("wamid.r1", target), offset=2)
    await a.disconnect()
    assert [ctx["message_ts"] for ctx in hooks.reactions] == [target]


ENGLAND = "\U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f"
CODER = "\U0001f469\U0001f3fd‍\U0001f4bb"  # skin tone, then ZWJ
RAINBOW_FLAG = "\U0001f3f3️‍\U0001f308"  # VS16, then ZWJ


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        (LAUGH, LAUGH),
        (FAMILY, FAMILY),
        (CODER, CODER),
        (RAINBOW_FLAG, RAINBOW_FLAG),
        (ENGLAND, ENGLAND),
        ("❤️", "❤️"),
        ("​" + LAUGH + "‌", LAUGH),  # zero-width space / non-joiner
        ("⁠" + LAUGH + "﻿", LAUGH),  # word joiner, BOM
        ("‮" + LAUGH + "⁧‎", LAUGH),  # bidi controls
        (LAUGH + "  ", LAUGH),  # line / paragraph separators
        ("\x85" + LAUGH + "\x9b\x00\x1b", LAUGH),  # C1 and C0 controls
        (LAUGH + "\U000e0069\U000e0067\U000e006e", LAUGH),  # tag characters outside a flag ("ASCII smuggling")
        (LAUGH + "\ud800", LAUGH),  # a lone surrogate
        ("‍" + LAUGH, LAUGH),  # a ZWJ that joins nothing
        (LAUGH + "‍", LAUGH),
        ("a‍b", "ab"),
        ("\U0001f468‍‍\U0001f469", "\U0001f468‍\U0001f469"),
        ("   ", ""),
        (5, ""),
    ],
)
def test_clean_emoji(raw, clean):
    assert mod._clean_emoji(raw) == clean


def test_clean_emoji_is_capped_without_a_dangling_joiner():
    assert len(mod._clean_emoji(LAUGH * 100)) == mod.MAX_REACTION_CHARS
    # 31 characters, then a ZWJ at the cut: the joiner is dropped rather than left dangling.
    capped = mod._clean_emoji("x" * 30 + FAMILY)
    assert capped == "x" * 30 + "\U0001f468"


@pytest.mark.asyncio
async def test_hostile_emoji_reaches_hooks_clean(meta, api_key):
    a, hooks = await connect_with_creator(meta)
    await deliver(meta, reaction_message("wamid.r1", "wamid.t0", "​‮" + LAUGH + " ﻿"), offset=2)
    await a.disconnect()
    assert hooks.reactions[0]["reaction"] == LAUGH
    assert hooks.reactions[0]["raw_event"]["reaction"]["emoji"] == LAUGH
    assert hooks.events[0][0]["payload"]["emojis"] == [LAUGH]


@pytest.mark.asyncio
async def test_slow_hooks_are_abandoned_and_polling_continues(meta, api_key, caplog):
    import asyncio
    import logging

    a = make_adapter()
    a._hook_timeout = 0.05
    started: list[str] = []

    async def stuck_reaction(ctx):
        started.append("reaction")
        await asyncio.sleep(30)

    async def stuck_event(event, source):
        started.append("event")
        await asyncio.sleep(30)

    a.set_reaction_handler(stuck_reaction)
    a.set_platform_event_handler(stuck_event)
    assert await a.connect()
    await deliver(meta, text_message("wamid.t0", "hi"), offset=1)
    caplog.set_level(logging.WARNING)
    page = [reaction_message("wamid.r1", "wamid.t0"), text_message("wamid.t1", "next")]
    await deliver(meta, *page, offset=2)  # well inside until()'s 3 s, although each hook would take 30 s
    await a.disconnect()
    assert started == ["reaction", "event"]
    assert [call.args[0].text for call in a.handle_message.await_args_list] == ["hi", "next"]
    abandoned = [r.getMessage() for r in caplog.records if "abandoned" in r.getMessage()]
    assert len(abandoned) == 2 and not any(LAUGH in m or "wamid" in m for m in abandoned)


@pytest.mark.asyncio
async def test_reaction_to_an_agent_attachment_names_the_agent(meta, api_key, tmp_path):
    a, hooks = await connect_with_creator(meta)
    path = tmp_path / "report.pdf"
    path.write_bytes(b"%PDF-1.4 report")
    sent = await a.send_document(CREATOR, str(path))
    assert sent.success and sent.message_id
    photo = meta.inbound_image("wamid.i1")  # the user's own attachment is recorded too (for quotes)
    await deliver(meta, photo, offset=2)
    await deliver(
        meta, reaction_message("wamid.r1", sent.message_id), reaction_message("wamid.r2", "wamid.i1"), offset=3
    )
    await a.disconnect()
    assert [ctx["item_user_id"] for ctx in hooks.reactions] == ["agent", None]


@pytest.mark.asyncio
async def test_reactions_are_forwarded_with_media_switched_off(meta, api_key, monkeypatch):
    monkeypatch.setenv(mod.MEDIA_ENV, "false")
    a, hooks = await connect_with_creator(meta)
    await deliver(meta, reaction_message("wamid.r1", "wamid.t0"), offset=2)
    await a.disconnect()
    assert len(hooks.reactions) == 1 and len(hooks.events) == 1


@pytest.mark.asyncio
async def test_reactions_work_without_handlers_or_rich_sent_store_async(meta, api_key, monkeypatch):
    from gateway import rich_sent_store

    for name in ("record_async", "record_media_async"):
        monkeypatch.delattr(rich_sent_store, name, raising=False)
    a = make_adapter()  # no handlers installed (e.g. a gateway without hooks)
    assert await a.connect()
    await deliver(meta, text_message("wamid.t0", "hi"), reaction_message("wamid.r1", "wamid.t0"), offset=1)
    await deliver(meta, text_message("wamid.t1", "next"), offset=2)
    await a.disconnect()
    assert a.handle_message.await_count == 2 and saved_state().seen("wamid.r1")
