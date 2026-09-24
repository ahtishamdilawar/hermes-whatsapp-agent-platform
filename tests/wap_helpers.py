"""Shared test helpers: load the repo root as a plugin package and fake Meta's API."""

from __future__ import annotations

import collections
import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
PKG = "wap_plugin_under_test"
API_KEY = "WAAVtest-key-0123456789abcdefghijklmnopqrstuvwxyz"
CREATOR = "user:50972923564215"


def load_plugin() -> Any:
    """Import the repo root exactly as Hermes does: a package with relative imports."""
    if PKG in sys.modules:
        return sys.modules[PKG]
    spec = importlib.util.spec_from_file_location(PKG, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[PKG] = module
    spec.loader.exec_module(module)
    return module


def fixture_json(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeMeta:
    """Scriptable stand-in for ``https://api.whatsapp.com/agent/v1``.

    Queue responses per endpoint (``updates``, ``messages``, ``statuses``); each item is an
    ``httpx.Response``, an exception to raise, or a callable(request) -> Response. When a
    queue is empty, ``updates`` returns 204 and the others return a success body.
    """

    def __init__(self) -> None:
        self.queues: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self.requests: list[httpx.Request] = []
        self._wamid = 0

    def calls(self, endpoint: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("/" + endpoint)]

    def bodies(self, endpoint: str) -> list[dict]:
        return [json.loads(r.content) for r in self.calls(endpoint)]

    def queue(self, endpoint: str, *items: Any) -> None:
        self.queues[endpoint].extend(items)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        endpoint = request.url.path.rsplit("/", 1)[-1]
        queue = self.queues[endpoint]
        if queue:
            item = queue.popleft()
            if isinstance(item, Exception):
                raise item
            return item(request) if callable(item) else item
        if endpoint == "updates":
            return httpx.Response(204)
        if endpoint == "messages":
            self._wamid += 1
            return httpx.Response(
                200,
                json={
                    "messaging_product": "whatsapp",
                    "contacts": [{"input": CREATOR, "wa_id": CREATOR}],
                    "messages": [{"id": f"wamid.out{self._wamid}"}],
                },
            )
        return httpx.Response(200, json={"success": True})

    def client(self, api_key: str = API_KEY, *, unthrottled: bool = False, **kwargs: Any):
        from wap_plugin_under_test.client import AgentPlatformClient, RateWindow

        http = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        client = AgentPlatformClient(api_key, http=http, **kwargs)
        if unthrottled:  # adapter tests poll in a tight loop; don't wait on Meta's per-minute budgets
            client.limits = {name: RateWindow(10**6) for name in client.limits}
        return client


def error_response(status: int, code: int, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        headers=headers,
        json={
            "error": {
                "message": f"(#{code}) test",
                "type": "OAuthException",
                "code": code,
                "error_data": {"messaging_product": "whatsapp", "details": "test"},
                "fbtrace_id": "AWtrace",
            }
        },
    )


def updates_response(
    *messages: dict, next_offset: int, contacts: list | None = None, statuses: list | None = None
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "object": "whatsapp_agent_platform",
            "entry": [
                {
                    "id": "123456789",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "contacts": contacts
                                if contacts is not None
                                else [{"wa_id": CREATOR, "profile": {"name": "Alex"}}],
                                "messages": list(messages),
                                "statuses": statuses or [],
                            },
                        }
                    ],
                }
            ],
            "next_offset": next_offset,
        },
    )


def text_message(
    wamid: str, body: str, *, sender: str = CREATOR, timestamp: int | None = None, context: dict | None = None
) -> dict:
    msg = {
        "from": sender,
        "id": wamid,
        "timestamp": str(timestamp or int(time.time()) + 5),
        "type": "text",
        "text": {"body": body},
    }
    if context:
        msg["context"] = context
    return msg


load_plugin()  # test modules import ``wap_plugin_under_test`` at collection time
