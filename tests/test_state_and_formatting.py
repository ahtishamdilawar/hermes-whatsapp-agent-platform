from __future__ import annotations

import json

from wap_helpers import API_KEY, CREATOR
from wap_plugin_under_test.formatting import to_whatsapp
from wap_plugin_under_test.state import PollState, read_creator, state_path


def test_state_roundtrip_never_contains_key(hermes_home):
    path = state_path(hermes_home, API_KEY)
    state = PollState.load(path)
    state.next_offset, state.creator = 42, CREATOR
    for i in range(600):
        state.remember(f"wamid.{i}")
    state.save()
    raw = path.read_text(encoding="utf-8")
    assert API_KEY not in raw and API_KEY not in path.name
    again = PollState.load(path)
    assert (again.next_offset, again.creator, len(again.recent_message_ids)) == (42, CREATOR, 512)
    assert again.seen("wamid.599") and not again.seen("wamid.0")
    assert read_creator(hermes_home, API_KEY) == CREATOR


def test_corrupt_state_is_quarantined_and_restarts_fresh(hermes_home):
    path = state_path(hermes_home, API_KEY)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    state = PollState.load(path)
    assert state.next_offset == 0 and state.creator is None
    assert not path.exists()
    assert len(list(path.parent.glob(f"{path.name}.corrupt-*"))) == 1


def test_invalid_offset_is_quarantined(hermes_home):
    path = state_path(hermes_home, API_KEY)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"next_offset": -1, "start_timestamp": 1}), encoding="utf-8")
    assert PollState.load(path).next_offset == 0


def test_whatsapp_formatting():
    md = "# Title\n**bold** and *it* ~~gone~~ [site](https://x.y)\n`**code**`\n```\n**fence**\n```"
    assert to_whatsapp(md) == "*Title*\n*bold* and _it_ ~gone~ site (https://x.y)\n`**code**`\n```\n**fence**\n```"


def test_formatting_strips_invisible_characters():
    assert to_whatsapp("a\u2060b\u00a0c") == "ab c"
