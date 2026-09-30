# ADR-0008: Chat log gap backfill via logs.ivr.fi

**Status:** Accepted (implemented; see Action Items) — 2026-09-18; amended 2026-09-29 (logs.ivr.fi replaces recent-messages)
**Date:** 2026-09-16
**Deciders:** Project owner

## Amendment, 2026-09-29

The provider asked for was **logs.ivr.fi**, which keeps months of chat. The first implementation used
**recent-messages** (robotty) instead, which keeps about a day and at most 800 lines per request. A range
from February came back empty in production, because recent-messages no longer had it. logs.ivr.fi is now
the **only** history provider. recent-messages, its keep-warm requests and its `HISTORY_PROVIDER_URL`
setting are gone. The sections below describe the decision as it now stands. What changed is summarised
under "Superseded" at the end. Rows recent-messages filled keep `source='recent-messages'`.

## Context

Twitch has no chat history API. Whenever the bot is offline, whether from a crash, an update or an EventSub reconnect, messages are lost, and the requirement is to log *every* message. A community service, **logs.ivr.fi** (run by ivr.fi on [rustlog](https://github.com/boring-nick/rustlog)), logs the chat of many channels, this one included, and serves it back by channel and time range.

### Service facts (from https://logs.ivr.fi/openapi.json and requests, checked 2026-09-29)

- `GET https://logs.ivr.fi/channelid/{id}?from=<RFC 3339>&to=<RFC 3339>&raw=true&limit=N&offset=M` returns the channel's lines in that range as **raw IRC lines**, **oldest first**.
  - `from` is inclusive and `to` exclusive. Millisecond precision is accepted.
  - `limit` and `offset` page through a range; a page shorter than `limit` is the last.
  - `json=true` and `ndjson=true` give the same lines as objects; the IRC line is in each.
- **404 `Not found`** means nothing in the range, an `offset` past its end, or a channel it doesn't log. So `GET /channels` (`{"channels": [{"name", "userID"}]}`) is checked first, to tell a channel it doesn't log from a quiet one.
- Lines carry `tmi-sent-ts` and Twitch's own message `id` (the UUID EventSub gives), but no receive time.
- Users can opt out (`POST /optout`); an opted-out user is missing from the log.
- **No terms, rate limits, fair-use rules or contact are published.** Responses carry `Cache-Control: public, max-age=36000`.
- **Tag order isn't stable**, and the trailing `:` may be missing, so the bot needs an RFC 2812-compliant parser.

## Decision

- Add a pluggable **`HistoryProvider`** interface. The implementation is **`IvrLogsProvider`**. It asks by channel id for a time range, `from_ms` inclusive and `to_ms` exclusive, with `limit` and `offset`.
- **Gap detection:**
  - `log_sessions` records the live coverage for each channel.
  - A gap is `(last message received or session end) → (new session start)`.
  - Gaps are checked at startup and after each EventSub reconnect longer than 5 s. They are filled by queued jobs (ADR-0024 §5).
- **Fetch:** each gap on its own, from 5 s before it starts to its end (`to = gap_to + 1`, as `to` is exclusive), oldest gap first, paged by `offset` until a page comes back short. Gaps can lie months apart, and the chat between them is already in the live log, so one request spanning them would fetch it for nothing.
- **Self-imposed limits,** since the service publishes none. The owner chose generous ones, with backoff:
  - at most **one request every 10 seconds**, and **200 a day** (UTC), counted across all channels;
  - a `User-Agent` naming the bot and its repository;
  - on 429, 5xx or a network error, wait (30 s, then 2 min) and try again; after **three failures in a row**, ask nothing more until the next day;
  - once the day's budget is spent, or after those failures, the provider answers `paused` with the time it will ask again. The job goes back to the queue and the worker waits until then;
  - never ask for lines already fetched: a gap a pause cut short records the newest line it stored (`backfill_runs.reached_ms`), and the next fill resumes there.
- **Parse and map:**
  - `PRIVMSG`: a `messages` row. `id` becomes `message_id` (the same UUID EventSub uses, so it dedupes on insert), `user-id`, `tmi-sent-ts` becomes `sent_at` and `received_at`, `badges`. `source='ivr-logs'` and the raw line is stored. The columns are filled in EventSub's terms (`history/irc_convert.py`): fragments from the `emotes` tag, `msg-id` as `message_type`, badge months from `badge-info`, the reply parent's user id, the reward id and the shared chat source channel, and the `/me` wrapper dropped from the text.
  - `CLEARMSG` becomes a delete `mod_event`. Its target is the author's user id, looked up from the message (IRC names only the login). `CLEARCHAT` with a target becomes a `user_clear` (with `ban-duration`), and without a target a `chat_clear`.
  - `USERNOTICE` becomes a `chat_notifications` row, with `msg-id` as the EventSub notice type and the payload in the live shape (`system_message`, `text`, `chatter`, and the `msg-param-*` tags in snake case as `detail`). An anonymous gifter names no one.
- **Completeness:** a gap is `complete` once its last page came back short. A failure, a pause or a channel the service doesn't log (`channel_not_logged`) leaves it incomplete, with the error, and it stays open for the next job. The service's own holes and opt-outs can't be told from a quiet chat, so `complete` means "the service had nothing more", not "nothing was missed".
- **Never act on history.** Backfilled events go to the log only, never to commands, listeners, triggers or variables.
- **Consent:** `channels.history_backfill` is **opt-in** at onboarding. The prompt names the service and links it.
- **Config:** `IVR_LOGS_URL` (default `https://logs.ivr.fi`) lets another rustlog or justlog deployment stand in.

## Options Considered

### Option A: logs.ivr.fi (chosen)
| Dimension | Assessment |
|-----------|------------|
| Complexity | Low-medium (IRC parsing, mapping, paging, own limits) |
| Cost | Free, a third-party dependency |
| Coverage | Months back, whole ranges, for channels it logs |

**Pros:** Nothing more to host. Independent of the homelab, so it covers even a full homelab outage. Reaches far enough back to fill any gap the bot has had.
**Cons:** Third-party availability, with no published terms. Lower fidelity than EventSub (no fragments, `message_type` inferred). Only for channels it logs.

### Option B: recent-messages (robotty)
**Pros:** Designed for this; answers `channel_not_joined` rather than a bare 404.
**Cons:** About a day of history and 800 lines per request, and only for channels it had already joined, hence the keep-warm requests. It was the first implementation, and it could not fill a gap from months before (see the amendment).

### Option C: Self-hosted rustlog or recent-messages2
**Pros:** Your own data and limits. No third-party dependency.
**Cons:** It **shares the homelab's failure domain**, so it doesn't help with power or network outages. It must not be restarted together with the bot.

### Option D: A second "witness" instance of the bot's logger (hot standby)
**Pros:** Full fidelity (EventSub).
**Cons:** Two processes must coordinate for deduping and ownership of the single writer. More complexity than the gap problem is worth right now.

### Option E: Accept gaps
**Cons:** Fails requirement F2.

## Trade-off Analysis

A covers the common cases (updates, crashes, short outages) and the long ones, cheaply, and even covers homelab-wide outages. C is the same code path behind a URL switch (`IVR_LOGS_URL`), so it's available if you want independence. D is the only full-fidelity option, but it's disproportionate for now. Recording completeness keeps the log honest.

## Consequences

- **Easier:** zero-downtime-ish updates as far as the log is concerned; filling gaps from months ago.
- **Harder:**
  - Maintaining the IRC parser.
  - Mixed-fidelity rows, so queries may need to check `source`.
  - Consent UX at onboarding.
- **Risk:** the service could change, limit the bot or disappear. Behind the provider interface, only this adapter would need to change.
- **Risk:** the daily budget and its pause live in memory, so a restart starts a new count.

## Action Items

1. [x] Add an RFC 2812 IRC parser with golden tests (reordered tags, no trailing `:`).
2. [x] Build the provider, the gap detector and `backfill_runs`. *(2026-09-29: `IvrLogsProvider`, with the limits above; recent-messages and its keep-warm scheduler removed.)*
3. [x] The per-channel toggle (`channels.history_backfill`) is respected and partial gaps are recorded with their reason. *Finished 2026-09-20: `!join` now answers with what the log does and points at `!backfill`, which is the consent prompt. It names the service (`IVR_LOGS_URL`, whichever deployment this bot points at) and says backfilled messages never run commands or triggers, before anything is sent there. Only the broadcaster can turn it on or off. The admin channel page and the channels API both show and set it.*
4. [ ] Tell ivr.fi about the bot integration, if a contact turns up. None is published.
5. [x] Add a deploy runbook step: record the session end before stopping the bot, and verify backfill afterwards. *(2026-09-20: the README's "Deploying an update" section. The session end is recorded by the shutdown path itself, so the runbook's job is to let it finish (compose now waits 45s for SIGTERM instead of 10) and to check afterwards with `scripts/coverage.py`, a one-shot `docker compose --profile tools run --rm coverage` that lists the gaps no complete backfill run covers and exits 1 if any are still open.)*
6. [x] Re-check gaps after an EventSub reconnect, not only at startup. *Done 2026-09-20: a client that stops is started again (ADR-0001 item 4) through the same path startup uses, which queues the channel's gaps job, so whatever was missed while the bot was deaf is filled when it comes back. A reconnect TwitchIO handles inside one running client is invisible to us and needs no gap check, because the session never ended.*

## Superseded (2026-09-29)

What the first version decided, and what replaced it:

- `RecentMessagesProvider` against `recent-messages.robotty.de/api/v2` → `IvrLogsProvider` against `logs.ivr.fi`.
- `?after=<gap_from − 5 s>&limit=800`, later one request spanning all gaps and paging back in time → one request per gap, paged forward by `offset`.
- Completeness from the oldest `rm-received-ts`, and `out_of_reach` for a gap older than the service's history → complete when the last page comes back short. Runs recorded `out_of_reach` no longer settle a gap, so those gaps are open again for ivr.fi.
- Keep-warm (`limit=1` every 30 minutes) → none; ivr.fi logs channels without being asked.
- `HISTORY_PROVIDER_URL` → `IVR_LOGS_URL`.
