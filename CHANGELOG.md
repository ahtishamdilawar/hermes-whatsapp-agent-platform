# Changelog

All notable changes to this plugin are documented here. Versions follow [SemVer](https://semver.org/);
`plugin.yaml` `version` always equals the release tag.

## [Unreleased]

### Added
- Platform adapter `whatsapp_agent_platform` for Meta's WhatsApp Agent Platform (`/agent/v1`, manual v1).
- Long-poll inbound (`GET /updates`) with a persisted cursor, restart-safe dedup, backlog skip on first
  activation, and one poller per API key (process guard + machine-wide lock; clear 409 handling).
- Text replies with WhatsApp formatting, fence-aware 4096-character splitting and quoted replies.
- Delivery semantics from the manual: ambiguous failures (HTTP 500, timeouts) are never re-sent; 429 and
  503/131016 are retried after backing off; a partly delivered split resumes with only the remainder.
- Read receipts and typing indicator (`POST /statuses`) within Meta's 12/minute budget.
- Budget guard that drops interim/progress messages before they can crowd out the final answer.
- Cron / `send_message` delivery to the agent's creator (`self` target, standalone sender).
- `hermes gateway setup` flow, creator pinning, optional sender allowlist.
