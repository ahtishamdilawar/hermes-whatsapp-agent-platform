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
| Media | Photos, voice notes, audio, video, documents and stickers in; files out (no voice-note bubbles) | Yes | Yes |
| Proactive / cron messages | Yes, to the creator | Yes | Only within the 24 h window or templates |

## Requirements

- **Hermes Agent ≥ 0.21.4** (GitHub release `v2026.9.21` or newer). The PyPI `hermes-agent` package (0.19.0) is
  **not supported**.
- A WhatsApp account with the Agents feature (Meta's beta is rolling out gradually).

## Install

The plugin is in the [official Hermes plugin catalog](https://github.com/NousResearch/hermes-agent/tree/main/plugin-catalog),
so install it by name:

```bash
hermes plugins install whatsapp-agent-platform
```

This installs the reviewed release pinned in the catalog, then asks whether to enable it (add `--enable` to skip
the prompt). To move to a newer release once the catalog pins it, run
`hermes plugins update whatsapp-agent-platform` and restart the gateway.

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

If you send voice notes, also consider `stt.echo_transcripts: false`: each echoed transcript costs one of the 12
sends a minute (see [Related Hermes settings](#related-hermes-settings)).

## Configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `WHATSAPP_AGENT_PLATFORM_API_KEY` | yes | — | Agent API key (Chat info → API key). |
| `WHATSAPP_AGENT_PLATFORM_HOME_CHANNEL` | no | `self` | Where cron / `send_message` deliver. `self` = the agent's creator, or `user:<id>`. |
| `WHATSAPP_AGENT_PLATFORM_ALLOWED_USERS` | no | — | Extra `user:<id>` senders to accept, comma-separated, in addition to the creator. |
| `WHATSAPP_AGENT_PLATFORM_ALLOW_ALL_USERS` | no | `false` | Accept any sender Meta delivers (not recommended). |
| `WHATSAPP_AGENT_PLATFORM_MEDIA_ENABLED` | no | `true` | `false` = text only: no files are downloaded or sent (see [Media](#media)). |

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
| Quoted replies (both directions), including re-attaching a quoted photo or file | ✅ |
| Read receipt on arrival + typing indicator while Hermes works | ✅ |
| Typed slash commands (`/new`, `/help`, …) | ✅ |
| Photos, voice notes (transcribed by Hermes), audio, video, documents and stickers from you | ✅ |
| Photos, video, audio and documents from your agent (as attachments) | ✅ |
| Cron jobs / proactive messages to the creator (`deliver: whatsapp_agent_platform`), with attachments | ✅ |
| Reactions you add: forwarded to Hermes hooks, not answered | ✅ |
| Message edit/delete, sending reactions, voice-note bubbles, buttons, lists, groups, command menus | ❌ Not offered by Meta's API |

## Media

Media is on by default. Set `WHATSAPP_AGENT_PLATFORM_MEDIA_ENABLED=false` (or
`gateway.platforms.whatsapp_agent_platform.media_enabled: false` in `config.yaml`; the variable wins) to go back to
text only:
- media messages get a short "please send text" reply;
- the agent's files are not sent: each send fails, and the chat shows Hermes's "Couldn't deliver" notice for files
  other than images (images fail silently, which is Hermes's behaviour);
- image links the agent writes (`![alt](https://…)`) are sent as text, not downloaded;
- cron reports the attachments as not sent.

With the environment variable, the plugin's prompt hint also switches to a text-only version. Set only in
`config.yaml`, or with several profiles in one gateway that use different settings, the model keeps the full hint:
replace it with `platform_hints` (see [Related Hermes settings](#related-hermes-settings)).

### What your agent receives

| You send | Your agent gets |
|---|---|
| Photo | The image, for your model's vision (see `agent.image_input_mode`). The caption is the message text, so a caption such as `/new` works as a command, as in a text message. |
| Photo sent as a document | The same as a photo. |
| Voice note | Its transcript, when Hermes speech-to-text is enabled (`stt.enabled`); otherwise a note with the file's path. |
| Audio file (MP3, …) | A note with the file's path. Audio files are **not** transcribed, only voice notes. |
| Video | A note with the file's path. |
| Document | The file's path. Small text files (up to 100 KB, UTF-8, types such as `.txt`, `.md`, `.csv`, `.json`, `.py`) are also included in the message as text, before the caption (so a caption on such a file is not read as a command). |
| Sticker | An image (with the note `[The user sent a sticker]` when there is no caption). |
| A reply quoting an earlier photo or file | That file again, while the file still exists (for files you sent: while they are in Hermes's cache). |

Images Hermes can't use as images (HEIC, AVIF, TIFF) arrive as documents, with Hermes's document note rather
than vision. Files are downloaded up to 64 MiB, or up to `gateway.max_inbound_media_bytes` if that is lower
(Hermes's default is 128 MiB); users can send files larger than Meta's send limits. If a file is too large, has
expired or can't be downloaded (90 s limit), the message still reaches your agent, with its caption and a short
note saying what happened, and polling carries on. Message types Meta hasn't documented get a short "can't read
this kind of message" reply. A malformed message from Meta is skipped (and logged without its content) instead of
blocking polling; a missing or invalid timestamp is replaced by the time the message arrived.

### What your agent can send

The agent sends a file by writing `MEDIA:/absolute/path` in its reply (the plugin's platform hint tells it how);
files from image generation, text-to-speech, `send_message` and `/save` arrive the same way.

| Type | Meta's limit | What the plugin does |
|---|---|---|
| Image | 5 MiB, JPEG or PNG | JPEG and PNG up to 5 MiB go as they are. WebP, static GIF, BMP, TIFF and AVIF are converted to PNG or JPEG; HEIC/HEIF is converted to JPEG when Hermes's HEIF support (`pillow_heif`) is installed, and sent as a file otherwise. An image over 5 MiB is re-encoded as JPEG. Images over 25 megapixels are never converted (this bounds memory): other formats go as files, and a large JPEG is scaled down while decoding and goes as a file only if it is still too large. Animated GIF/WebP and SVG are sent as files. |
| Video | 16 MiB, MP4 or 3GP | MP4 and 3GP play inline. MOV, MKV, WebM, AVI and the like are sent as files. |
| Audio | 16 MiB | MP3, M4A, AAC, AMR and Ogg/Opus go as they are; WAV, FLAC and the like are converted to Ogg/Opus when ffmpeg is installed, and sent as files otherwise. Audio always arrives as a normal audio file: Meta has no voice-note bubble for agents. |
| Document | 16 MiB | PDF, Word, Excel, PowerPoint and `.txt` keep their type. Other text files (`.md`, `.csv`, `.json`, …) go as plain text, and WhatsApp shows them as `name.md.txt`. Other files (`.zip`, …) go as generic files and keep their name. |

- **Captions** can be up to 1024 characters. A longer caption goes first as a text message, then the file without
  one. Audio takes no caption, so its caption goes as text.
- When a photo, video or audio file goes as a file instead, its caption says why ("sent as a file: …").
- `[[as_document]]` in a reply sends images untouched, as files.
- Image URLs in a reply (`![alt](https://…)`) are downloaded by Hermes's SSRF-guarded image fetcher (within 60 s;
  an image over 16 MiB is dropped) and sent as photos. The link is sent as text instead only when the image
  certainly didn't arrive: the download failed, or the plugin or Meta refused the file. After an unknown outcome,
  a send-limit wait or a refused recipient no link follows, so the image is never duplicated, and a caption that
  already went as text is not repeated with the link.
- Files over 16 MiB, empty files and files on the never-upload list (see *Security and privacy*) are not sent.
- Files go only to the agent's creator, the only recipient Meta allows.

**When a file can't be sent:**
- The plugin waits up to 60 s for Meta's send budget. When the wait is 10 s or more it first sends one short
  notice: "A file is on its way: pausing ~Ns for WhatsApp's send limit", or "More files coming: …" when several
  files are waiting.
- A file Meta rejects is not retried. Neither is one whose outcome is unknown (HTTP 500 or a timeout after the
  file's message went out), because it may have arrived.
- Hermes then shows its "Couldn't deliver the attachment" notice (for files other than images).
- Hermes never re-sends attachments later: its delivery ledger covers the reply text only.
- `display.platforms.whatsapp_agent_platform.suppress_warning_notifications: true` hides these notices and the
  "sent as a file" caption note.

### Reactions

A reaction from the creator (or an allowed sender) is forwarded to Hermes's hooks — `reaction:added` and the
`gateway_platform_event` plugin hook with `event_type: "reaction"` — and is not answered by the agent. Meta
delivers only new reactions: changing or removing a reaction produces no event.
- Each hook gets 5 s, because hooks run inside the poll loop; a slower one is abandoned and logged.
- Invisible, control and bidi characters are removed from the emoji, and a reaction to a malformed message id is
  dropped.
- A reaction to one of the agent's messages or attachments is marked `item_user_id: "agent"`. A reaction to one of
  your own older attachments (beyond the last 512 handled messages) may be marked the same way.

### Attachments from cron jobs

While the gateway runs, cron attachments are sent like any reply. Without a running gateway the plugin's
standalone sender sends the text, then each attachment, within 45 s: it starts no new file after that, and a
file still uploading or sending at 45 s is stopped, so the plugin reports back before Hermes's
`cron.standalone_send_timeout_seconds` (default 60 s) cancels the whole delivery. Each file that isn't sent is
reported as a warning on the cron run; a file stopped while its message was going out is reported as "outcome
unknown … it may have arrived". Files go only to a creator the gateway has already confirmed.

Hermes itself refuses `send_message` calls, and out-of-process cron deliveries, that carry attachments but no
text for plugin platforms, so give such messages at least a line of text.

### Related Hermes settings

| Setting (`config.yaml`) | Effect on this platform |
|---|---|
| `gateway.max_inbound_media_bytes` | Largest file downloaded, if lower than the plugin's 64 MiB (Hermes's default is 128 MiB, so 64 MiB applies). |
| `agent.image_input_mode` | How photos reach the model: `native` sends the image to the model itself, `text` sends a description made with `vision_analyze`, and `auto` (the default) picks `native` for models with vision and `text` otherwise. |
| `stt.enabled` | Voice notes are transcribed only when this is on (default on). |
| `stt.echo_transcripts` | Echoes each transcript to the chat; every echo costs one of the 12 sends a minute. `false` is recommended (it applies to every platform). |
| `voice.auto_tts`, `/voice tts` (every reply) or `/voice on` (replies to voice notes) | Each spoken reply also costs an upload and a message (an MP3 file, not a voice bubble). |
| `display.platforms.whatsapp_agent_platform.suppress_warning_notifications` | Hides failure and pacing notices and the "sent as a file" caption note. |
| `cron.standalone_send_timeout_seconds` | Hermes's limit for one out-of-process cron delivery (default 60 s); the plugin finishes or stops its files within 45 s. |
| `cron.media_send_timeout_seconds` | Per-attachment limit for cron while the gateway runs (default 300 s). |
| `gateway.strict` | Recommended: Hermes then sends only files from its cache, `gateway.media_delivery_allow_dirs` or recently created files. |
| `platform_hints.whatsapp_agent_platform.replace` | Replaces the plugin's prompt hint; `.append` (or a plain string) adds to it instead. |

The plugin picks its prompt hint once, from `WHATSAPP_AGENT_PLATFORM_MEDIA_ENABLED`, when Hermes loads it. If media
is off only through `config.yaml`, or several profiles in one gateway use different settings, give the text-only
profiles the text-only hint yourself:

```yaml
platform_hints:
  whatsapp_agent_platform:
    replace: >-
      You are chatting via Meta's WhatsApp Agent Platform with the person who created this agent. Standard
      markdown auto-converts to WhatsApp syntax (bold, italic, strike, monospace) — write markdown freely, bullets
      included. No tables — use bullets or labeled lines. Sent messages cannot be edited or deleted, and replies
      over 4000 characters are split into several messages (max 12 per minute), so keep answers concise. This
      channel is text only: you cannot send or receive files, images or voice notes here.
```

## Limits and delivery guarantees

- Meta allows **12 messages, 12 status updates and 15 polls per minute** per agent. The plugin paces itself to
  stay inside these budgets (it polls at most 14 times a minute).
- **Media has its own budgets.** Each file sent costs one upload (12 a minute) plus one message from the same 12 a
  minute as text; each file received costs one media lookup (12 a minute). Downloads themselves are not limited.
- **One poller per API key.** When another client starts polling the same key, Meta answers the plugin's
  running poll with HTTP 409. The plugin pauses for a minute and resumes; if it keeps happening (3 times in
  10 minutes) it stops with a clear `poll_conflict` error. Use a separate agent for development — Meta's
  [terms](https://www.whatsapp.com/legal/third-party-agents-terms) allow up to five per account.
- **Delivery is at-least-once.** Sends Meta refused (rate limit, "not accepted") are retried after backing off;
  a split reply resumes with only the parts not yet sent. A send whose outcome is unknown (HTTP 500 or a timeout
  after the request went out) is not retried immediately; Hermes's delivery ledger re-sends the reply later,
  marked "may be a duplicate". With `gateway.delivery_ledger: false` such a reply is not re-sent. Attachments
  are sent at most once (see [Media](#media)).
- On first start, messages Meta retained from before activation are skipped; after that, messages that arrive
  while the gateway is down are processed when it comes back.
- Regenerating the API key (reinstalling WhatsApp does this) starts fresh state for the new key: the creator is
  confirmed again and older retained messages are skipped.

## Security and privacy

- The API key and message content go only to Meta: the API at `api.whatsapp.com`, and Meta's media host
  `lookaside.fbsbx.com` to download files users send. A download link to any other host or over plain HTTP is
  refused before any request, and redirects are never followed. No telemetry, no self-updating code.
  (`WHATSAPP_AGENT_PLATFORM_BASE_URL` exists only for testing against a local fake API; it accepts HTTPS or
  `http://localhost` and logs a warning when set.)
- The one other request: image URLs the agent writes in a reply (`![alt](https://…)`) are fetched by Hermes's
  SSRF-guarded image downloader, from the host in the URL, without the API key. The plugin gives it 60 s and
  sends nothing over 16 MiB. The fetch reveals your server's IP address to that host, and such a URL may come
  from content the agent read.
- Files the agent sends are uploaded to Meta, which keeps uploads for up to 30 days. They go only to the agent's
  creator.
- Files users send are cached under `$HERMES_HOME/cache/` (images, audio, videos, documents; an older Hermes
  install may still use its legacy `image_cache/`, `document_cache/`, … folders). Hermes's housekeeping deletes
  them after about 24 hours while the gateway runs; they stay while it is stopped. On Linux and macOS the plugin
  also makes them readable by their owner only (best effort). What reached the agent from them (transcripts, text,
  image descriptions) stays in Hermes's session history like any message.
- What happens to received files next follows your Hermes configuration: photos go to your main model's provider
  (native images) or your vision provider, voice notes to your speech-to-text provider, and small text documents
  are included in the message to your main model.
- The plugin never uploads `.env` / `.env.*`, `*.pem`, SSH private keys (`id_rsa`, `id_ed25519`, `id_ecdsa`,
  `id_dsa`), `.git-credentials`, `.netrc`, `auth.json` or its own state directory, on top of Hermes's own delivery
  policy. That list is deliberately narrow and goes by name: hardlinks, renamed copies, and another profile's
  files other than these names are not caught. For an agent that reads web pages or other untrusted content, set
  `gateway.strict: true`: Hermes then sends only files from its cache, `gateway.media_delivery_allow_dirs` or files
  created moments earlier. That makes it much harder for prompt injection to send arbitrary local files, but it is
  no guarantee when the agent also has shell or file-writing tools, which can copy a file first. Files still go
  only to you, but they are uploaded to Meta.
- The API key stays in your Hermes `.env`; it is never logged or written to plugin state. The plugin's log lines
  don't contain file names, captions, media URLs or media ids.
- State is kept in `$HERMES_HOME/platforms/whatsapp_agent_platform/<key-fingerprint>.json`: the poll cursor,
  first-activation time, recently handled message ids and the creator's `user:<id>`.
- Per WhatsApp's [Third-Party Agents Terms](https://www.whatsapp.com/legal/third-party-agents-terms), agent chats are **not end-to-end encrypted**: messages
  and files pass through Meta to your Hermes instance.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `invalid_auth` | The key is wrong or was regenerated (reinstalling WhatsApp regenerates it). Copy it again. |
| `poll_conflict` (HTTP 409) | Another process keeps polling the same key (for example a second gateway or a test script). Stop it, then restart the gateway. |
| "dropped a message from a sender who is not the agent's creator" | Meta says that sender is not this agent's creator. Add their `user:<id>` to `WHATSAPP_AGENT_PLATFORM_ALLOWED_USERS` only if you really want them to reach Hermes. |
| `whatsapp_agent_platform_lock` | Another gateway on this machine already uses this key. |
| Cron says the recipient is unknown | Send the agent one message first so the plugin can confirm the creator, or set `WHATSAPP_AGENT_PLATFORM_HOME_CHANNEL=user:<id>`. |
| A file didn't arrive; the log says `media not sent` or `media rejected` | Meta refused the file's type or size, or the file is on the never-upload list, missing or empty. Rejected files are not retried. Ask the agent for a PDF, or to zip the files. |
| "file too large for WhatsApp" | Meta's limits are 5 MiB for photos (larger ones are re-encoded) and 16 MiB for everything else. Split or compress the file, or share a link instead. |
| A `.md` or `.csv` arrives as `name.md.txt` | WhatsApp shows text files it doesn't list that way. Ask for a PDF. |
| The agent doesn't understand a voice note | Hermes speech-to-text is off or not configured (`stt.enabled`), or it was an audio file rather than a voice note (audio files are not transcribed). |
| The agent can't see photos | With a model without vision, Hermes describes photos with `vision_analyze`: check that it works (`auxiliary.vision`). Set `agent.image_input_mode: text` if Hermes wrongly treats the model as vision-capable. |
| An image arrives as a file, "(sent as a file: …)" | The format isn't one WhatsApp shows as a photo (animated, SVG, HEIC without `pillow_heif`), or it is over 25 megapixels. |
| The agent answers every photo with "I can only read text messages" | `WHATSAPP_AGENT_PLATFORM_MEDIA_ENABLED` is `false`. |
| Cron warnings like `attachment 2 of 3 (…) not sent` | The reason follows in the warning. For "time limit", send fewer or smaller files, or run cron with the gateway running. |
| Plugin not listed | `hermes plugins enable whatsapp-agent-platform`, and check `hermes --version` ≥ 0.21.4. |

## Development

```bash
git clone https://github.com/NousResearch/hermes-agent
git clone https://github.com/ahtishamdilawar/hermes-whatsapp-agent-platform
python -m venv .venv && . .venv/bin/activate      # Python 3.14 (3.11–3.13 for Hermes <= 0.21.4)
pip install -e ./hermes-agent "pytest==9.1.1" "pytest-asyncio==1.3.0"
cd hermes-whatsapp-agent-platform
pytest                                             # protocol, adapter and real-loader tests (fake Meta API)
hermes plugins validate . && hermes plugins doctor . --ci
```

`client.py` is the pure `/agent/v1` client (no Hermes imports), `adapter.py` the Hermes glue, `state.py` the
cursor store and `formatting.py` the markdown → WhatsApp converter. Media lives in `media.py` (types, limits,
conversions, the never-upload list), `media_inbound.py` (downloads), `media_outbound.py` (uploads and sends,
shared with the cron sender) and `hermes_compat.py` (the Hermes helpers it uses). Test fixtures in
`tests/fixtures/` are copied from Meta's developer manual.

## License

MIT — see [LICENSE](LICENSE). `formatting.py` is adapted from Hermes Agent (MIT, © Nous Research); its header
carries the original notice.
