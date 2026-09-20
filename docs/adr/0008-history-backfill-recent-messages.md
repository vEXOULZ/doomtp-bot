# ADR-0008: Chat log gap backfill via recent-messages

**Status:** Accepted (implemented; see Action Items) — 2026-09-18
**Date:** 2026-09-16
**Deciders:** Project owner

## Context

Twitch has no chat history API. Whenever the bot is offline, whether from a crash, an update or an EventSub reconnect, messages are lost, and the requirement is to log *every* message. A community service, **recent-messages** (robotty, used by Chatterino), keeps recent chat for channels it has joined.

### Service facts (from https://recent-messages.robotty.de/api, checked 2026-09-16)

- `GET https://recent-messages.robotty.de/api/v2/recent-messages/:channel_login`
- Query parameters:
  - `limit`
  - `before` and `after`, as ms timestamps compared against `rm-received-ts`
  - `hide_moderation_messages` and `hide_moderated_messages`
- It returns **up to 800 messages**, oldest first, as **raw IRC lines**: `PRIVMSG`, `CLEARCHAT`, `CLEARMSG`, `USERNOTICE`, `NOTICE` and `ROOMSTATE`.
  - Every line carries `historical=1` and `rm-received-ts`.
  - Deleted messages carry `rm-deleted=1`.
- **Tag order isn't stable**, and the trailing `:` may be missing, so the bot needs an RFC 2812-compliant parser.
- `error_code: channel_not_joined` means the service isn't currently listening, which includes **the first request for a channel**. Its presence is informational, and messages may still be returned.
- A channel that opted out returns 403 `channel_ignored`.
- **Usage guidelines:** user-facing integrations should be **opt-in** (or at least opt-out), with an explanation and a link to the site. For bigger integrations, contact the maintainer first.

## Decision

- Add a pluggable **`HistoryProvider`** interface. The first implementation is **`RecentMessagesProvider`**.
- **Gap detection:**
  - `log_sessions` records the live coverage for each channel.
  - A gap is `(last message received or session end) → (new session start)`.
  - Gaps are checked at startup and after each EventSub reconnect longer than 5 s.
- **Fetch:** `?after=<gap_from − 5 s>&limit=800`, requested per channel with backoff on errors.
- **Parse and map:**
  - `PRIVMSG`: a `messages` row. `id` becomes `message_id` (the same UUID EventSub uses, so it dedupes via `INSERT OR IGNORE`), `user-id`, `tmi-sent-ts` becomes `sent_at`, `badges`. `source='recent-messages'` and the raw line is stored.
  - `rm-deleted=1` sets `deleted_at`, using the `CLEARMSG`/`CLEARCHAT` time when present.
  - `CLEARMSG` becomes a delete `mod_event`. `CLEARCHAT` with a target becomes a `user_clear` (with `ban-duration`), and without a target a `chat_clear`.
  - `USERNOTICE` becomes a `chat_notifications` row.
- **Completeness:** if the oldest returned `rm-received-ts` is after `gap_from`, or the response hit 800, or it returned `channel_not_joined`, then `backfill_runs.complete=0`. That gap is still visible as partial.
- **Never act on history.** Backfilled events go to the log only, never to commands, listeners, triggers or variables.
- **Keep warm:** for channels with backfill enabled, send `limit=1` every 30 minutes so the service stays joined. The interval should be tuned after contacting the maintainer.
- **Consent:** `channels.history_backfill` is **opt-in** at onboarding. The prompt names the service and links it, per its guidelines.
- **Config:** `HISTORY_PROVIDER_URL` lets a **self-hosted `recent-messages2`** replace the public service.

## Options Considered

### Option A: Public recent-messages service (chosen default)
| Dimension | Assessment |
|-----------|------------|
| Complexity | Low-medium (IRC parsing, mapping) |
| Cost | Free, a third-party dependency |
| Coverage | ≤800 messages per gap. Only works if the service was already joined. |

**Pros:** Nothing more to host. Independent of the homelab, so it covers even a full homelab outage.
**Cons:** Third-party availability. Capped history. The first-request cold start. Lower fidelity than EventSub (no fragments, `message_type` inferred).

### Option B: Self-hosted recent-messages2
**Pros:** Your own data and limits. No third-party dependency.
**Cons:** Needs Postgres and TimescaleDB. It **shares the homelab's failure domain**, so it doesn't help with power or network outages. It must not be restarted together with the bot.

### Option C: A second "witness" instance of the bot's logger (hot standby)
**Pros:** Full fidelity (EventSub).
**Cons:** Two processes must coordinate for deduping and ownership of the single writer. More complexity than the gap problem is worth right now.

### Option D: Accept gaps
**Cons:** Fails requirement F2.

## Trade-off Analysis

A covers the common cases (updates, crashes, short outages) cheaply and even covers homelab-wide outages. B is the same code path behind a URL switch, so it's available if you want independence. C is the only full-fidelity option, but it's disproportionate for now. Recording completeness keeps the log honest whichever provider fills the gap.

## Consequences

- **Easier:** zero-downtime-ish updates as far as the log is concerned.
- **Harder:**
  - Maintaining the IRC parser.
  - Mixed-fidelity rows, so queries may need to check `source`.
  - Consent UX at onboarding.
- **Risk:** the service could change or disappear. Behind the provider interface, only this adapter would need to change.
- **Revisit:** long gaps (over 800 messages) in busy channels are a reason to consider Option C.

## Action Items

1. [x] Add an RFC 2812 IRC parser with golden tests using the lines from the API docs (reordered tags, no trailing `:`).
2. [x] Build `RecentMessagesProvider`, the gap detector, `backfill_runs`, and the keep-warm scheduler. *(gaps are filled once at startup, after the bot connects)*
3. [x] The per-channel toggle (`channels.history_backfill`) is respected and partial gaps are recorded with their reason. *Finished 2026-09-20: `!join` now answers with what the log does and points at `!backfill`, which is the consent prompt — it names the service (`HISTORY_PROVIDER_URL`, whichever deployment this bot points at) and says backfilled messages never run commands or triggers, before anything is sent there. Only the broadcaster can turn it on or off. The admin channel page and the channels API both show and set it.*
4. [ ] Contact the service maintainer about the bot integration and the keep-warm interval. **Do this before enabling backfill for real channels.**
5. [x] Add a deploy runbook step: record the session end before stopping the bot, and verify backfill afterwards. *(2026-09-20: the README's "Deploying an update" section. The session end is recorded by the shutdown path itself, so the runbook's job is to let it finish — compose now waits 45s for SIGTERM instead of 10 — and to check afterwards with `scripts/coverage.py`, a one-shot `docker compose --profile tools run --rm coverage` that lists the gaps no complete backfill run covers and exits 1 if any are still open.)*
6. [x] Re-check gaps after an EventSub reconnect, not only at startup. *Done 2026-09-20: a client that stops is started again (ADR-0001 item 4) through the same path startup uses, which ends in `backfill.run_all()` — so whatever was missed while the bot was deaf is filled when it comes back. A reconnect TwitchIO handles inside one running client is invisible to us and needs no gap check, because the session never ended.*
