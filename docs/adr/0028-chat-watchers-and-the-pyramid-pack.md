# ADR-0028: Chat watchers, and emote pyramids as a pack

**Status:** Accepted, 2026-10-04
**Date:** 2026-10-04
**Deciders:** Project owner

## Context

Chatters build emote pyramids: one chatter posts `LUL`, then `LUL LUL`, `LUL LUL LUL`, and back down to
`LUL`. Channels want the bot to break some of them with a pyramid fact, at a chance each channel sets,
to congratulate the ones that finish, and to keep stats on who builds and who breaks.

Most of that is policy the command language already handles: `random` rolls the chance and picks a
fact, channel variables hold the settings, and `!var incr` and `!var top` keep stats. Detection is the
part it can't do well:

- **Order.** A pyramid is a sequence of lines. Listeners (architecture §7) run as one task per line,
  concurrently, so two rows that arrive close together both read the old state and one update is lost.
  Pyramids arrive in exactly those bursts.
- **Cost.** Every line that isn't a row has to reset the state, so the listener would match every chat
  line in the channel and run the whole runtime, with a variable commit and a `command_runs` row each
  time.
- **The bot's own lines.** Whether the bot's break landed depends on whether its line arrived before the
  builder's last row. Listeners never see the bot's lines (`Dispatcher._message` returns early on
  `is_self`), and an ignored user's line also breaks a pyramid in chat while never reaching a listener.
- **Matching.** Chat clients add invisible characters (U+E0000, U+034F and others) to get past Twitch's
  duplicate-message check. A regex written by a moderator gets this wrong easily.

Other features have the same shape: emote combos across chatters, copypasta waves, a counting game, the
first chatter of a stream. Each needs every line, in order, with a little state per channel.

## Decision

### Chat watchers are a third source of trigger events

A **chat watcher** is Python code that sees every chat line in a channel, in the order EventSub delivers
them, and emits **trigger events**. Channels react to those events with ordinary `!event` triggers, the
same way they react to raids and subs.

- A watcher is synchronous and in memory: `observe(message) -> events`. No I/O, no database, nothing
  slow, because it runs on every line in the event loop.
- The dispatcher calls the watchers right after it writes the line to the chat log, **before** it drops
  the bot's own lines, other bots' lines, ignored users and automod hits. Every line visible in chat
  counts.
- Only live lines are observed. Backfilled history never fires a trigger.
- A watcher only keeps state for a channel that has an enabled trigger of one of its types, so a channel
  that never asked for pyramids costs nothing.
- Events run through `TriggerService.event_triggers` and `TriggerRunner.run` like chat notifications do:
  preflight, cooldowns, the creator's rank, the Outbox.
- Each watcher adds its event types to `TRIGGER_TYPES` in code. Types are not registered at run time,
  so validation, the API and the docs stay static.

Triggers now receive the chatter's badges, so `$chatter.rank` in a trigger reflects moderator and VIP
badges. Until now the trigger runner built the chatter without them. Listeners get the same fix.

### The pyramid watcher reports, the pack decides

`PyramidWatcher` emits the event type `pyramid`. It holds no policy: no chance, no minimum size, no
exemptions. It reports every pyramid from width 2 up.

- A pyramid has **one builder**. Each row is one token repeated, one wider than the last while rising,
  one narrower while falling.
- Events, in `{event.*}`:

| Field | Meaning |
|-------|---------|
| `pyramid_id` | the message id of its first row, so a pack can match a break attempt to its outcome |
| `phase` | `step` (a new row), `complete` (the builder reached width 1 again) or `broken` |
| `direction` | `up` or `down`, on `step` |
| `token`, `width`, `peak` | the emote, the current row's width, the widest row so far |
| `user` | the builder: `id`, `name`, `display` |
| `breaker` | on `broken`: who broke it, `id`, `name`, `display` |
| `by_bot` | on `broken`: the bot's own line broke it |
| `self_broken` | on `broken`: the builder broke it with a wrong row |

- The chatter of the trigger run is the builder, with badges, except on `broken`, where it is the
  breaker. So per-chatter stats land on the right person, and `!var top` can rank breakers.
- **A break counts when the bot's line arrives before the builder's last row.** If the last row arrives
  first, the pyramid is `complete`, and the bot's line after it is only a line. Both cases follow from
  the order the watcher sees.

### Everything else is the `pyramid` pack

The rest is derived commands in a bot-owned `pyramid` pack, installed by `scripts/starter_pack.py`
(ADR-0019) and published globally. Its trigger only calls `pyramid_on_event`, so a fix to the pack reaches
every channel without touching the trigger. *Amended by ADR-0029:* the pack brings that trigger itself, so
it runs wherever the pack's module is on; channels no longer add `!event add pyramid pyramid_on_event`.

Per-channel settings, in channel variables:

| Setting | Default | Meaning |
|---------|---------|---------|
| chance up, chance down | 25, 75 | percent chance, rolled on **every row**, that the bot posts a fact to break it; one for rows going up, one for rows coming down |
| min peak | 3 | the smallest peak that counts as a pyramid in stats and congratulations |
| exempt rank | off | builders at or above this rank are never broken. Off means above `bot_owner`, so nobody |
| congratulations | a short line | posted when a pyramid completes; empty turns it off |
| shared facts | on | off uses only the channel's own facts. With both lists empty the bot never tries |

Stats count pyramids completed, broken by the bot, broken by other chatters, dodged (the bot tried and
lost the race) and fumbled, plus the biggest pyramid and per-chatter counts for `!pyramid top`.

## Options Considered

### Option A: listeners and variables only
**Pros:** nothing new in Python. **Cons:** races between rows, a full run on every chat line, and no
view of the bot's own lines, so it can't tell whether a break landed.

### Option B: a watcher primitive plus a pack (chosen)
**Pros:** detection is ordered, cheap and tested in one place; chance, facts, messages and stats stay in
the language where channels and the pack owner can change them live. The watcher shape fits combos,
copypastas and counting games later. **Cons:** a new trigger source in the dispatcher, and per-channel
state in memory that a restart drops (a pyramid in progress during a restart is simply not seen).

### Option C: a Python `pyramid` module
**Pros:** one place, no pack. **Cons:** every message, chance or stat change is a release, which is what
ADR-0019 keeps out of Python.

## Consequences

- **Easier:** new chat games that need every line in order are a watcher and a trigger type. The
  dispatcher wiring stays the same.
- **Harder:** watchers run inside the event loop on every line, so each one must stay O(line length)
  with no I/O. Their tests need to cover interleaved channels and the bot's own lines.
- **Changed:** `$chatter.rank` in triggers and listeners now includes badge roles. A trigger that relied
  on it being lower behaves differently.
- **doomtp-web:** if the site lists trigger types or event fields, it needs the new `pyramid` type.

## Action Items

1. [x] Chat watchers: the `watchers` package, the dispatcher hook before the `is_self` return, live
   lines only, and the per-channel gate.
2. [x] Badges for trigger and listener runs, so `$chatter.rank` includes badge roles.
3. [x] `PyramidWatcher` and the `pyramid` trigger type, with tests for complete, broken by the bot before
   and after the last row, broken by another chatter, fumbled, invisible characters and interleaved
   channels.
4. [x] The `pyramid` pack: `pyramid_on_event`, `pyramid`, `pyramid_fact`, the shared facts, the
   settings above and the stats, installed by `scripts/starter_pack.py`.
5. [x] Documentation: architecture §7, `{event.*}` for `pyramid` in namespaces.md, and the README's
   command list.
