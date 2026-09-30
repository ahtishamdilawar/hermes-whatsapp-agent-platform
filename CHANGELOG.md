# Changelog

All notable changes to this plugin are documented here. Versions follow [SemVer](https://semver.org/);
`plugin.yaml` `version` always equals the release tag.

## [Unreleased]

## [0.2.0] - 2026-09-30

### Added
- Receive media: photos, voice notes, audio files, videos, documents and stickers are downloaded from Meta's media
  host (`lookaside.fbsbx.com`, the only host the key is sent to besides the API; no redirects, a byte cap of
  64 MiB or Hermes's `gateway.max_inbound_media_bytes` if lower, and a sha256 check) into Hermes's media cache and
  handed to the agent with their caption. Voice notes are transcribed by Hermes speech-to-text; audio files are
  not. Small UTF-8 text documents (`.txt`, `.json`, `.py`, …) are included in the message as text, before the
  caption; a photo sent as a document is treated as a photo, a sticker arrives as an image, and an image Hermes
  can't use as one (HEIC, AVIF, TIFF) arrives as a document. A photo's caption is the message text, so `/new` and
  other commands work as captions. A file that is too large, expired or fails to download reaches the agent as a
  short note instead, and polling carries on.
- Quoting an earlier photo or file (yours or the agent's) attaches it again.
- Send media: `send_image_file`, `send_video`, `send_voice`, `send_document` and `send_image`. Formats Meta refuses
  are converted (WebP/GIF/BMP/TIFF/AVIF → PNG/JPEG, HEIC/HEIF → JPEG when `pillow_heif` is available, oversize
  photos re-encoded, WAV/FLAC → Ogg/Opus with ffmpeg) or sent as files with a note in the caption. Images over
  25 megapixels are never converted, so a tiny "decompression bomb" can't exhaust memory: they go as files (a large
  JPEG is first scaled down while decoding). `[[as_document]]` sends an image's bytes untouched. Captions over
  1024 characters go first as text. Uploads wait for Meta's budgets (up to 60 s, with one pacing notice that says
  "more files" only when several are waiting), 429/503 are retried with the same upload, and a send whose outcome
  is unknown is never retried.
- Image URLs in replies are fetched by Hermes's SSRF-guarded image cache (60 s limit; an image over 16 MiB is
  dropped) and sent as photos. The link goes as text only when the image certainly didn't arrive; never after an
  unknown outcome, a send-limit wait or a refused recipient, and without repeating a caption already sent.
- Cron / `send_message` attachments without a running gateway: the standalone sender delivers them after the
  text within 45 s, which also bounds an upload or send already running, reports each file it could not send in
  `warnings` (a file cut off mid-send as "outcome unknown"), and sets `media_delivered` only when a file arrived.
- Reactions from the creator or an allowed sender are forwarded to Hermes hooks (`reaction:added`) and the
  `gateway_platform_event` plugin hook, each bounded to 5 s; they are not answered. Invisible and control
  characters are stripped from the emoji. Meta delivers new reactions only.
- `WHATSAPP_AGENT_PLATFORM_MEDIA_ENABLED` (default `true`; `false` = text only, like 0.1), also settable as
  `gateway.platforms.whatsapp_agent_platform.media_enabled`. With media off, the agent's file sends fail (Hermes
  shows "Couldn't deliver" for non-image files) and image links are sent as text.
- The platform hint tells the model how to send files (`MEDIA:/absolute/path`), the types and limits, and what it
  can receive. With `WHATSAPP_AGENT_PLATFORM_MEDIA_ENABLED=false` it switches to a text-only hint; for media off
  set only in `config.yaml`, use `platform_hints.whatsapp_agent_platform.replace` (example in the README).

### Changed
- Media messages are handled instead of being answered with the "I can only read text messages" notice (which
  remains when media is turned off). Message types Meta hasn't documented get a shorter notice, and both notices
  now honour `display.platforms.whatsapp_agent_platform.suppress_warning_notifications`.
- HTTP 409 on sends and read receipts is no longer treated as a poll conflict; only 409 on `GET /updates` is.
- Cron messages with attachments no longer carry the "attachment(s) generated; not sent from a scheduled job"
  note (it is kept, reworded, when media is off).

### Fixed
- A malformed message from Meta (a text that isn't an object, a body that isn't a string, and the like) is skipped
  and logged without its content, instead of making polling retry the same page until the platform stopped. A
  missing or unusable timestamp falls back to the arrival time.

### Security
- New egress: Meta's media host `lookaside.fbsbx.com` for downloads. The API key and message content go only there
  and to `api.whatsapp.com`; other hosts, plain HTTP and redirects are refused.
- Image URLs in the agent's replies are fetched by Hermes's SSRF-guarded downloader, without the key, from the
  host in the URL, which sees the server's IP address.
- Files the agent sends are uploaded to Meta (kept up to 30 days), and only to the confirmed creator; the check
  runs before any upload or URL fetch. Received files stay in Hermes's media cache under `HERMES_HOME` (deleted by
  Hermes after about 24 h while the gateway runs; owner-only on POSIX, best effort).
- A narrow, name-based never-upload list (`.env`, `.env.*`, `*.pem`, SSH private keys, `.git-credentials`,
  `.netrc`, `auth.json`, the plugin's state directory, also through Windows `\\?\` paths) on top of Hermes's
  delivery policy. Hardlinks and renamed copies are not caught; `gateway.strict` is recommended for agents that
  read untrusted content, and narrows the risk without removing it when the agent has shell or file tools.

## [0.1.1] - 2026-09-25

### Fixed
- `requires-python` no longer excludes Python 3.14. Hermes `main` runs only on 3.14 and refuses (or, on update,
  disables) a plugin whose `requires-python` does not include the interpreter it runs on.

## [0.1.0] - 2026-09-24

### Added
- Platform adapter `whatsapp_agent_platform` for Meta's WhatsApp Agent Platform (`/agent/v1`, manual v1).
- Long-poll inbound (`GET /updates`) with a persisted cursor, restart-safe dedup, backlog skip on first
  activation, and one poller per API key (process guard + machine-wide lock; clear 409 handling).
- Text replies with WhatsApp formatting, fence-aware 4096-character splitting and quoted replies.
- At-least-once delivery that fits Hermes's delivery ledger: 429 (reported as `flood_control:<s>`) and
  503/131016 are retried after backing off; a partly delivered split resumes with only the remainder; an
  ambiguous send (HTTP 500, timeout) is not retried inline and is re-sent later by the ledger, marked as a
  possible duplicate; a recipient that is not the creator is reported as forbidden and never retried.
- Read receipt on arrival (also with `typing_indicator: false`) and a typing indicator refreshed every 20 s,
  within Meta's 12/minute `/statuses` budget.
- Budget guard that drops interim/progress messages before they can crowd out the final answer.
- Cron / `send_message` delivery to the agent's creator (`self` target, standalone sender).
- `hermes gateway setup` flow; creator confirmed with Meta before any message reaches Hermes (re-confirmed
  when the creator's id changes); additive sender allowlist.
- Resilience: a single HTTP 409 pauses polling instead of stopping it; Windows file-sharing violations on the
  state file are retried; unknown 4xx responses back off instead of stopping; stale quotes are re-sent without
  the quote; replies are split by UTF-16 length with margin under Meta's 4096 limit.
