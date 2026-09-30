# ADR-0024: Keep every event as Twitch sent it, and backfill older gaps from logs.ivr.fi

**Status:** Accepted — 2026-09-28; amended 2026-09-29 (the migration in §1; §5, three times; §4:
logs.ivr.fi is the only provider, see ADR-0008's amendment)
**Date:** 2026-09-28
**Deciders:** Project owner

## Context

The chat log keeps what `twitch/mapping.py` picks out of each event and throws the rest away. A live
message arrives from EventSub as a JSON object; TwitchIO turns it into Python objects
(`ChatMessage.__init__`) and our mapping copies a dozen of their fields into columns. Anything we did
not think to copy is gone: the chatter's name color, an emote's set and owner, a mention's user id, a
cheermote's tier, and whatever Twitch adds next. Only backfilled messages keep their source, the IRC
line, in `messages.raw` (ADR-0008).

Two things now want more than we keep:

- **The chat log API** (ADR-0025) is a contract with the web site and the VOD archive. A replay that
  wants name colors or animated emotes cannot have them, and a field we never stored cannot be added
  later.
- **Backfill** only reaches as far back as recent-messages does: at most 800 lines, recent ones only
  (ADR-0008). A gap older than that, or longer, stays open. **logs.ivr.fi** keeps whole days of chat
  for many channels, this one included. *(2026-09-29: it has replaced recent-messages, see §4.)*

### Service facts (logs.ivr.fi, checked 2026-09-28)

- It runs [rustlog](https://github.com/boring-nick/rustlog), whose API is described at
  `https://logs.ivr.fi/openapi.json`.
- `GET /channelid/{id}?from=&to=` returns a channel's log between two RFC 3339 times, with `limit`,
  `offset`, and `raw`, `json` or `ndjson` for the format. `/channelid/{id}/{y}/{m}/{d}` returns one day.
- `?json=true` gives each line as `{text, displayName, timestamp, id, tags, username, channel, raw,
  type}`: `raw` is the IRC line, `tags` its tags already split, `type` the IRC command (1 `PRIVMSG`,
  4 `USERNOTICE` on the day checked). So every format carries the same information: the IRC line.
- Message ids are Twitch's own UUIDs, the ones EventSub gives. A message both sources have is one row.
- Users can opt out (`POST /optout`); an opted-out user is simply missing from the log.
- **No terms, rate limits, fair-use rules or contact are published** — not on the site, not in the API
  description, not in rustlog's configuration (which has no request limit). ivr.fi may still limit
  requests in front of rustlog; we cannot see that.
- The day checked (2026-02-21) had no `CLEARCHAT` or `CLEARMSG` lines. Whether ivr.fi logs timeouts and
  deletions for this channel is not yet known.

### What an IRC line has and lacks next to EventSub

A `PRIVMSG` maps onto `channel.chat.message` almost field for field: `id`, `user-id`, nick,
`display-name`, `room-id`, `color`, `badges` + `badge-info`, the `emotes` positions (which rebuild the
fragments), `msg-id` (→ `message_type`), `reply-parent-*` and `reply-thread-parent-*`, `bits`,
`custom-reward-id`, and the shared-chat `source-*` tags. `USERNOTICE` maps onto
`channel.chat.notification` (`msg-id` → `notice_type`, with renames such as `subgift` → `sub_gift`;
`msg-param-*` → its details), and `CLEARMSG`/`CLEARCHAT` onto the delete and clear events.

IRC lacks: an emote's set, owner and animated format; a mention's user id; a cheermote's tier; the
channel's display name; and, for timeouts and bans, **the moderator and the reason**. It has tags
EventSub does not: `first-msg`, `returning-chatter`, AutoMod's `flags`, `client-nonce`, `mod`,
`subscriber`, `turbo`.

## Decision

### 1. Store the source, and only the columns we query

`messages`, `chat_notifications` and `mod_events` each get **`raw jsonb`** and **`raw_format`**:

| `raw_format` | `raw` holds |
|---|---|
| `eventsub` | the notification's `event` object, exactly as Twitch sent it |
| `irc` | `{"line": "<the IRC line>"}`, from recent-messages or ivr.fi |
| `legacy` | an EventSub-shaped object rebuilt once from a row written before this ADR |

The columns that stay are the ones the API filters, sorts, joins or searches on:

- **messages:** `message_id`, `channel_id`, `user_id`, `user_login`, `text` (the search index is built
  from it), `sent_at`, `received_at`, `deleted_at`, `cleared_at`, `mod_event_id`, `is_self`,
  `is_command`, `source`.
- **chat_notifications:** `id`, `channel_id`, `user_id`, `type`, `sent_at`, `source`.
- **mod_events:** `id`, `channel_id`, `type`, `message_id`, `target_user_id`, `moderator_user_id`, `at`,
  `source`.

Everything else — display name, badges, fragments, color, message type, reply, cheer, reward,
shared-chat source, a notification's payload, a timeout's duration and reason — is read from `raw`. A
field that turns out to be worth filtering on becomes a column again, filled from `raw`, with nothing
lost in between.

**Capturing it.** TwitchIO drops the JSON before our code sees the event. It is still whole in
`EventSubWebsocket._process_notification` (`data["payload"]["event"]`). The adapter (ADR-0002) wraps
that one method and hands the JSON to the mapping beside the parsed event. That is the only place the
adapter reaches into TwitchIO's internals, and a test pins it so a TwitchIO upgrade that moves it fails
loudly.

**Size.** An EventSub message event is 1–2 KB of JSON, several times what the columns hold. Postgres
compresses values past about 2 KB and the log is one channel's chat; this is accepted, and `raw` is left
out of every query that does not need it.

**Migration** (ADR-0022): add `raw`/`raw_format`; move the existing IRC lines into `raw` as `irc`;
rebuild the other rows as `legacy`; switch the readers to `raw`, and drop the columns that moved **in
the same release**, making `raw` required.

CONTRIBUTING.md asks for a column to be dropped one release after the code stops using it, because the
old bot keeps writing while the migrate step runs. That rule protects a log someone relies on. On
2026-09-29 the bot is not in real use yet, and the owner has said the logs it holds need not be kept, so
the rule is set aside for this ADR: at worst, the few seconds of chat the old bot sees during the update
are lost. For the same reason the `legacy` rebuild is best effort; a detail it cannot recover is not
worth more work.

### 2. One reader turns every format into the EventSub shape

`chatlog/events.py` turns a row into an EventSub-shaped event: `eventsub` as it is, `irc` converted
field by field as above, `legacy` as stored. IRC-only tags are kept under an `irc` key rather than
dropped. The `/log` entries (ADR-0025) are built from that, so their shape does not change; new fields
such as `color` are added to them, which ADR-0025 allows.

Converting on read, not on write, means a better converter improves every old row, and the IRC line is
never replaced by a guess.

### 3. Fill in what IRC lacks, once, when a line is backfilled

What IRC lacks is looked up when a line is stored and kept in **`enrichment jsonb`**, beside `raw` and
never mixed into it. Each looked-up value records where it came from.

- **Emotes**, by emote id, into a **`chatlog.emotes`** cache (id, set, owner, formats, source, when):
  1. our own log: any EventSub message that used the same emote already carries all of it;
  2. Helix *Get Channel Emotes* for the channel (one call covers all its emotes; the owner is the
     channel) and *Get Global Emotes*;
  3. an emote from another channel we have never seen stays without a set and owner. Whether it is
     animated can still be read from Twitch's emote CDN, which serves an animated image only for an
     animated emote.

  A deleted emote resolves to nothing, and stays that way.
- **Mentions** (`@name` in the text), by login **at the message's time**:
  1. the log around the message — a mentioned chatter usually spoke just before;
  2. `user_names`, which knows who held a login when;
  3. Helix *Get Users*, last. Twitch answers who holds the login **now**, and logins are reused after a
     rename, so its answer is recorded as `twitch`, and a reader can treat it as less certain on an old
     message.
- **Cheermote tiers** are computed from the bits and Twitch's published tier thresholds.
- The **moderator and reason** of a backfilled timeout are not recoverable, and stay empty.

### 4. logs.ivr.fi as the history provider

*(2026-09-29: first written as a second provider, after recent-messages. It is now the only one;
recent-messages is gone, see ADR-0008's amendment.)*

- **`IvrLogsProvider`** implements `HistoryProvider` (ADR-0008), asked by channel id for a range
  (`from_ms` inclusive, `to_ms` exclusive) with `limit` and `offset`. It asks
  `/channelid/{id}?from=&to=&raw=true` and returns the IRC lines, oldest first, which go through the
  existing `parse_line` and `to_events`. `GET /channels` (cached for 6 hours) tells a channel it doesn't
  log (`channel_not_logged`) from a range with no chat, since both answer 404.
- **Self-imposed limits,** since the service publishes none. The owner chose generous ones, with backoff:
  - at most **one request every 10 seconds**, and **200 a day** (UTC);
  - only the gaps a job asks for (§5), never whole days or a whole channel;
  - a `User-Agent` naming the bot and its repository;
  - on 429, 5xx or a network error, wait 30 s, then 2 min, and after three failures in a row ask nothing
    more until the next day;
  - never ask for lines already fetched: `backfill_runs.reached_ms` records the newest line stored for a
    gap a fill was stopped in, and the next fill resumes there.
- **Completeness:** `backfill_runs` records the `provider`. A gap is `complete` once the service's last
  page for it came back short. Its log has its own holes and opt-outs, which we cannot tell from a quiet
  channel, so `complete` means "the service had nothing more". `/log/coverage` reports the provider with
  each backfill run.
- **Consent:** per channel, opt-in (ADR-0008 item 3), and the `!backfill` prompt names the service.
- **Config:** `IVR_LOGS_URL`, default `https://logs.ivr.fi`.

### 5. Backfill runs as queued jobs, which can also be started by hand

Today a backfill runs only at startup, inline, for every open gap. With a rate-limited second provider
a backfill can take minutes or days, so it becomes a **job in a queue**:

- **`chatlog.backfill_jobs`**: the channel, the kind (`gaps` or `range`), the range (`from_ms`, `to_ms`),
  who asked (`startup`, a chatter or an API caller), when, and the state: `queued`, `running`, `done`,
  `failed` or `cancelled`, with what it fetched and stored, whether the range is `complete`, and any error.
- **One job per channel for its gaps.** A job per gap meant a request per gap, and a gap older than the
  service's history was queued at every startup, for good. Instead:
  - a **`gaps`** job looks up the channel's open gaps when it runs, and fills them oldest first, **each
    with its own requests**, from 5 s before it starts to its end, paged by `offset` until a page comes
    back short. Gaps can lie months apart, and the chat between them is in the live log already. Its range
    only says what was open when it was queued. *(2026-09-29: with recent-messages, one request spanned
    all the gaps, paged back in time, and a gap older than that service's day of history was recorded
    `out_of_reach` and no longer counted as open. Those gaps are open again, for ivr.fi to fill.)*
  - a channel has **at most one `gaps` job waiting**: startup, a reconnect and `!backfill gaps` join it
    (its range widens) instead of queueing another;
  - a gap stays open until a run fills it completely. An error stops the job, and the gaps after it are
    recorded with the same error, without being asked for;
  - a **`range`** job is one asked for by hand, and is filled the same way; the same range can be queued
    or running only once.
- **One worker** takes the oldest queued job, fills its range (§4) and records a `backfill_runs` row per
  gap. It wakes when a job is queued. A job still `running` when the bot stops goes back to `queued` at
  startup. When the provider pauses (the day's budget is spent, or three failures in a row), the job goes
  back to `queued` with the `paused` error, and the worker waits until the time the provider gave.
- **Startup** queues the channel's `gaps` job instead of filling gaps inline.
- **By hand**, in the channel, by the broadcaster:
  - `!backfill gaps` queues the channel's `gaps` job, or names the one already waiting;
  - `!backfill <duration>`, e.g. `!backfill 6h`, queues the range from that long ago until now;
  - `!backfill queue` lists the channel's queued and running jobs;
  - `!backfill cancel <id>` cancels a queued job.

  `!backfill`, `!backfill on` and `!backfill off` keep their meaning.
- **API** (`admin` area, like the setting itself): `GET /channels/{login}/backfill` lists the
  channel's jobs; `POST /channels/{login}/backfill` queues `{"from_ms", "to_ms"}` or `{"gaps": true}`;
  `DELETE /channels/{login}/backfill/{job_id}` cancels a queued job. `{"gaps": true}` answers the
  waiting `gaps` job when there is one, and no job when no gap is open. The admin page is a doomtp-web
  change on top of this.
- **Consent** does not change: a channel with backfill off can queue nothing.
- A range asked for by hand can overlap the live log; message ids keep a message from being stored
  twice.

## Options Considered

### Option A: keep the columns, add more of them as they are needed
**Pros:** no JSON in the database; every field typed. **Cons:** each new field is a migration and is
empty for everything logged before it. What we did not think to keep is lost — the problem this ADR
exists to fix.

### Option B: store the source and query columns, convert on read (chosen)
**Pros:** nothing Twitch sends is lost; a field can become a column later and be filled from history;
a better IRC converter improves old rows. **Cons:** a larger table; readers go through one converter;
the adapter reaches into one TwitchIO method.

### Option C: convert IRC to EventSub JSON on write, and store only that
**Pros:** one format in `raw`. **Cons:** the conversion is lossy both ways — IRC-only tags are dropped
and missing fields become guesses stored as if Twitch had sent them. A mistake in the converter is
written into history.

### Option D: backfill only from recent-messages
**Pros:** designed for this. **Cons:** any gap longer or older than recent-messages reaches (about a day,
800 lines) stays open for good. It was what the bot first ran, and why a February range came back empty;
ivr.fi replaced it (ADR-0008's amendment).

## Consequences

- **Easier:** adding a field to `/log` without a migration; name colors and emote details in the replay;
  filling gaps from days ago.
- **Harder:** the adapter depends on one TwitchIO internal; the IRC converter and the enrichment lookups
  are code to keep right, tested against real lines.
- **Risk:** logs.ivr.fi is run by someone else, without published terms. Everything that talks to it is
  one provider behind one setting, and the limits above keep the bot a light user of it.
- **Risk:** Helix calls for emotes and users spend the bot's rate budget; both go through caches and
  happen only on backfill.
- **Revisit:** if ivr.fi does not log timeouts and deletions for this channel, a gap it fills shows
  removed messages as if they were not removed. Check before enabling it.

## Action Items

1. [x] Migration: `raw jsonb`, `raw_format` and `enrichment jsonb` on `messages`, `chat_notifications`
   and `mod_events`; `provider` on `backfill_runs`; the `emotes` cache; move IRC lines and rebuild
   the other rows as `legacy`.
2. [x] Capture the EventSub `event` JSON in the adapter and store it for live messages, notifications
   and moderation events.
3. [x] `chatlog/events.py`: the one reader from `eventsub`, `irc` and `legacy` to the EventSub shape,
   with golden tests from real recent-messages and ivr.fi lines; `/log` built from it. *(2026-09-29:
   `history/irc_convert.py` turns a PRIVMSG into `channel.chat.message`, a USERNOTICE into
   `channel.chat.notification` (the notice's object by EventSub's field names, the `msg-param-*` tags it
   has no field for under `irc` by their IRC names) and CLEARMSG/CLEARCHAT into the moderation events.
   Golden tests use ivr.fi lines. `/log` reads every entry from `raw`, so a message entry now also has
   `color` and `reply_parent_user`, and a chat notice's `detail` uses EventSub's names, e.g. `sub_tier`
   and `is_prime`.)*
4. [x] Enrichment on backfill: the emote cache and lookups, mention resolution by time, cheermote tiers.
   *(2026-09-29: `history/enrich.py`. Emotes: the channel's EventSub rows, then Helix channel and global
   emotes, then the CDN, cached in `emotes`; one the CDN can't answer for is asked again next fill.
   Mentions: the reply's parent, then someone who spoke earlier in the fill, then `user_names` nearest
   the time, then Helix for a valid login. Cheermotes in a message with bits: prefixes and tiers from
   Helix. A failed lookup leaves the value out and never stops the fill. Notice text is not enriched.)*
5. [x] `IvrLogsProvider` and the self-imposed limits. *(2026-09-29: the only provider; recent-messages
   removed. `backfill_runs.reached_ms` resumes a stopped fill, and old `out_of_reach` gaps are open
   again.)*
6. [ ] Check whether ivr.fi logs `CLEARCHAT` and `CLEARMSG` for this channel, and contact its
   maintainers about the integration. **Do this before enabling it for a real channel.** *(2026-09-29:
   checked. It logs `CLEARCHAT`: a day of #forsen had 292, every one a timeout or ban that converts with
   its `ban-duration`. It does not log `CLEARMSG`: none in 100,000 lines of that day, nor in two months
   of #vexoulz. So a backfilled message a moderator deleted is not marked deleted. Contacting the
   maintainers is still open, for the owner.)*
7. [x] The `!backfill` prompt, the admin page and the channels API name ivr.fi and set it per channel.
   *(2026-09-29: the prompt names `IVR_LOGS_URL`; the per-channel setting is `history_backfill`, which
   the admin page and the channels API already set.)*
8. [x] With item 3, drop the columns that moved into `raw` and make `raw` required (not one release
   later: see the migration in §1). *(2026-09-29: chatlog revision 0006. Search reads the display name
   and the bot's badge seed reads its badges from `raw`; downgrade refills the columns from `raw` where
   it is EventSub-shaped.)*
9. [x] The backfill queue (§5): `backfill_jobs`, the worker, startup queueing, the `!backfill`
   subcommands and the API.
