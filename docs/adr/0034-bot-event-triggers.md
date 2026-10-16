# ADR-0034: Triggers on the bot's own events

**Status:** Accepted, 2026-10-15
**Date:** 2026-10-15
**Deciders:** Project owner

## Context

Triggers react to what happens in a channel: chat lines, raids, subs, the stream going live (architecture
§7). Nothing reacts to what happens to the bot itself. A channel can't say "back online" after a restart,
announce a new version, greet chat when the bot joins, or thank the broadcaster for modding it. The owner
asked for a trigger for "bot comes online again" and other internal bot events (2026-10-15), and decided
that an update fires both `bot_updated` and `bot_online`, in that order.

## Options

### A. A single `bot_event` type with a `kind` match

**Pros:** one type. **Cons:** every consumer has to read the match to know what it reacts to, and the web's
event picker would need a second level.

### B. One trigger type per event

**Pros:** listed, picked and documented like `stream_online`. **Cons:** five more names.

## Decision

**B.** Five types, none of which has a chatter (they are in `NO_CHATTER`):

| Type | Fires | `event.*` |
| --- | --- | --- |
| `bot_online` | the bot is in chat: at start, or after Twitch dropped it and it came back | `reason` (`start`, `update`, `reconnect`), `cause` (on `reconnect`: what the watchdog found, or the client's error), `down_s`, `version` |
| `bot_updated` | at start, when the version differs from the last run's; before that start's `bot_online` | `version`, `previous` |
| `bot_joined` | the bot joined this channel (`!join`, or added on the web) | — |
| `bot_modded` / `bot_unmodded` | the capability probe saw the bot gain or lose moderator here | — |

- The last run comes from `bot_runs` in the chat log: one row per process, with its version, release and
  commit, kept alive every minute (`alive_at`) and closed on a clean stop. `down_s` at start is the time
  since the last run's `alive_at`, so it is accurate to the minute after a crash too.
- `bot_online` has a 15-minute floor on its channel cooldown (`MIN_CHANNEL_COOLDOWN_S`), whatever the
  trigger sets, so a connection that flaps doesn't post a line every time it comes back.
- Modding is seen by the probe, which runs at join, hourly and when Twitch refuses a call, so
  `bot_modded` can trail the change by up to an hour.
- There is no `bot_offline`: a bot that is going away can't be relied on to say so.
- One channel's failing trigger doesn't stop the rest from firing.

## Consequences

- Chat (`!event add bot_online …`) and the web's trigger list offer the same five types.
- A reconnect is the Twitch client started again inside the running process (the chat watchdog, or EventSub giving up), so uptime does not reset and `bot_online` can fire without the bot ever having left chat; `cause` says which. The 15-minute floor is what keeps that quiet.
- `bot_runs` also records which builds ran when, which the stats page can use later.
