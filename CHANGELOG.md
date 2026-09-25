# Changelog

All notable changes to this plugin are documented here. Versions follow [SemVer](https://semver.org/);
`plugin.yaml` `version` always equals the release tag.

## [Unreleased]

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
