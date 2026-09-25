# Hermes WhatsApp Agent Platform

Talk to your [Hermes Agent](https://github.com/NousResearch/hermes-agent) from a **WhatsApp Agent** chat, using
Meta's official [WhatsApp Agent Platform](https://www.whatsapp.com/developer/WhatsApp-Agent-Platform-Developer-Manual.pdf)
API (`https://api.whatsapp.com/agent/v1`).

> Community plugin — not affiliated with Meta, WhatsApp or Nous Research.

```
WhatsApp ──► Meta Agent Platform ──GET /updates (long poll)──► Hermes gateway ──► your Hermes agent
         ◄──                     ◄──POST /messages─────────────               ◄── (tools, memory, skills)
```

## How it differs from the other WhatsApp options in Hermes

| | This plugin (`whatsapp_agent_platform`) | `whatsapp` (Baileys) | `whatsapp_cloud` (Business Cloud API) |
|---|---|---|---|
| What it is | Meta's API for personal AI agents | Unofficial WhatsApp Web bridge | WhatsApp Business API |
| Needs | An agent created in WhatsApp + its API key | Linking your phone as a WhatsApp Web session | Meta business app, business number, public webhook |
| Inbound | Outbound long polling — no public URL | Local bridge | Webhook |
| Who can chat | The agent's creator (current beta) | Anyone who messages the linked number | Customers of the business number |
| Proactive / cron messages | Yes, to the creator | Yes | Only within the 24 h window or templates |

## Requirements

- **Hermes Agent ≥ 0.21.4** (GitHub release `v2026.9.21` or newer). The PyPI `hermes-agent` package (0.19.0) is
  **not supported**.
- A WhatsApp account with the Agents feature (Meta's beta is rolling out gradually).

## Install

```bash
hermes plugins install ahtishamdilawar/hermes-whatsapp-agent-platform --enable
```

Then get your key: in WhatsApp go to **Settings → Agents → Create an agent**, open the agent's chat →
**Chat info → API key**. Store it with `hermes gateway setup` (choose *WhatsApp Agent Platform*) or add it to
`~/.hermes/.env`:

```bash
WHATSAPP_AGENT_PLATFORM_API_KEY=your-key
```

Restart the gateway (`hermes gateway restart`) and send your agent a message. `hermes gateway status` should
list `whatsapp_agent_platform` as connected, and the log shows `WhatsApp Agent Platform: authenticated; long
polling started`.

### Recommended display settings

The platform cannot edit messages and allows 12 sends per minute, so status chatter should be off. Add to
`~/.hermes/config.yaml`:

```yaml
display:
  platforms:
    whatsapp_agent_platform:
      tool_progress: "off"
      interim_assistant_messages: false
      long_running_notifications: false
      busy_ack_detail: false
```

(Tool-progress bubbles and token streaming are already skipped automatically because the platform has no edit
endpoint; the plugin also drops interim messages when the send budget runs low.)

## Configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `WHATSAPP_AGENT_PLATFORM_API_KEY` | yes | — | Agent API key (Chat info → API key). |
| `WHATSAPP_AGENT_PLATFORM_HOME_CHANNEL` | no | `self` | Where cron / `send_message` deliver. `self` = the agent's creator, or `user:<id>`. |
| `WHATSAPP_AGENT_PLATFORM_ALLOWED_USERS` | no | — | Extra `user:<id>` senders to accept, comma-separated, in addition to the creator. |
| `WHATSAPP_AGENT_PLATFORM_ALLOW_ALL_USERS` | no | `false` | Accept any sender Meta delivers (not recommended). |

Disable the platform without removing the key: `gateway.platforms.whatsapp_agent_platform.enabled: false`.
Turn off the typing indicator (read receipts are still sent):
`gateway.platforms.whatsapp_agent_platform.typing_indicator: false`.

### Who can talk to your Hermes

Only the agent's **creator** by default. The plugin confirms the creator with Meta before handing any message to
Hermes (Meta accepts a read receipt only for the creator's own messages), so a message from anyone else is
dropped and logged. If the creator's WhatsApp id changes (for example after a phone-number change), Meta's
confirmation re-pins the new id automatically.

Other senders can be admitted with `WHATSAPP_AGENT_PLATFORM_ALLOWED_USERS`, but note that Meta currently lets an
agent send only to its creator, so replies to anyone else are refused by Meta. The creator's `user:<id>` is
stored as `creator` in the state file (see *Security and privacy*).

## Features

| Feature | Status |
|---|---|
| Text messages in and out, WhatsApp formatting, long replies split in order (≤ 4000 characters each) | ✅ |
| Quoted replies (both directions) | ✅ |
| Read receipt on arrival + typing indicator while Hermes works | ✅ |
| Typed slash commands (`/new`, `/help`, …) | ✅ |
| Cron jobs / proactive messages to the creator (`deliver: whatsapp_agent_platform`) | ✅ |
| Images, documents, voice notes | Planned — the plugin currently replies asking for text |
| Message edit/delete, sending reactions, buttons, lists, groups, command menus | ❌ Not offered by Meta's API |

## Limits and delivery guarantees

- Meta allows **12 messages, 12 status updates and 15 polls per minute** per agent. The plugin paces itself to
  stay inside these budgets (it polls at most 14 times a minute).
- **One poller per API key.** When another client starts polling the same key, Meta answers the plugin's
  running poll with HTTP 409. The plugin pauses for a minute and resumes; if it keeps happening (3 times in
  10 minutes) it stops with a clear `poll_conflict` error. Use a separate agent for development — Meta's
  [terms](https://www.whatsapp.com/legal/third-party-agents-terms) allow up to five per account.
- **Delivery is at-least-once.** Sends Meta refused (rate limit, "not accepted") are retried after backing off;
  a split reply resumes with only the parts not yet sent. A send whose outcome is unknown (HTTP 500 or a timeout
  after the request went out) is not retried immediately; Hermes's delivery ledger re-sends the reply later,
  marked "may be a duplicate". With `gateway.delivery_ledger: false` such a reply is not re-sent.
- On first start, messages Meta retained from before activation are skipped; after that, messages that arrive
  while the gateway is down are processed when it comes back.
- Regenerating the API key (reinstalling WhatsApp does this) starts fresh state for the new key: the creator is
  confirmed again and older retained messages are skipped.

## Security and privacy

- Network traffic goes only to `api.whatsapp.com`. No telemetry, no self-updating code.
  (`WHATSAPP_AGENT_PLATFORM_BASE_URL` exists only for testing against a local fake API; it accepts HTTPS or
  `http://localhost` and logs a warning when set.)
- The API key stays in your Hermes `.env`; it is never logged or written to plugin state.
- State is kept in `$HERMES_HOME/platforms/whatsapp_agent_platform/<key-fingerprint>.json`: the poll cursor,
  first-activation time, recently handled message ids and the creator's `user:<id>`.
- Per WhatsApp's [Third-Party Agents Terms](https://www.whatsapp.com/legal/third-party-agents-terms), agent chats are **not end-to-end encrypted**: messages
  pass through Meta to your Hermes instance.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `invalid_auth` | The key is wrong or was regenerated (reinstalling WhatsApp regenerates it). Copy it again. |
| `poll_conflict` (HTTP 409) | Another process keeps polling the same key (for example a second gateway or a test script). Stop it, then restart the gateway. |
| "dropped a message from a sender who is not the agent's creator" | Meta says that sender is not this agent's creator. Add their `user:<id>` to `WHATSAPP_AGENT_PLATFORM_ALLOWED_USERS` only if you really want them to reach Hermes. |
| `whatsapp_agent_platform_lock` | Another gateway on this machine already uses this key. |
| Cron says the recipient is unknown | Send the agent one message first so the plugin can confirm the creator, or set `WHATSAPP_AGENT_PLATFORM_HOME_CHANNEL=user:<id>`. |
| Plugin not listed | `hermes plugins enable whatsapp-agent-platform`, and check `hermes --version` ≥ 0.21.4. |

## Development

```bash
git clone https://github.com/NousResearch/hermes-agent
git clone https://github.com/ahtishamdilawar/hermes-whatsapp-agent-platform
python -m venv .venv && . .venv/bin/activate      # Python 3.14 (3.11–3.13 for Hermes <= 0.21.4)
pip install -e ./hermes-agent "pytest==9.1.1" "pytest-asyncio==1.3.0"
cd hermes-whatsapp-agent-platform
pytest                                             # protocol, adapter and real-loader tests (fake Meta API)
hermes plugins validate . && hermes plugins compat . && hermes plugins doctor . --ci
```

`client.py` is the pure `/agent/v1` client (no Hermes imports), `adapter.py` the Hermes glue, `state.py` the
cursor store and `formatting.py` the markdown → WhatsApp converter. Test fixtures in `tests/fixtures/` are copied
from Meta's developer manual.

## License

MIT — see [LICENSE](LICENSE). `formatting.py` is adapted from Hermes Agent (MIT, © Nous Research); its header
carries the original notice.
