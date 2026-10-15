# ADR-0031: Trigger cooldowns

**Status:** Accepted, 2026-10-15
**Date:** 2026-10-15
**Deciders:** Project owner

## Context

A listener fires on every chat line its regex matches, an event on every raid or sub, and a timer on its
clock (architecture §7). Only the commands a trigger calls are held to their cooldowns. A trigger with an
inline expression (`echo hi {$chatter.display}`) has nothing to hold it back, so a listener on a common
word answers every line that has it. The workaround is to wrap the expression in a custom command and give
that a cooldown, which works since custom commands got rules of their own, but it is two things to set up
for one behaviour.

The owner asked for trigger cooldowns that follow the command rules: a separate whole-channel and
per-user cooldown (decided 2026-10-15). Chat and the web must work the same way.

## Options

### A. Keep the workaround

**Pros:** no change. **Cons:** a cooldown needs a custom command and a publication. The web can't show
it on the trigger.

### B. Put the cooldowns in the trigger's `schedule` or `match` JSON

**Pros:** no migration. **Cons:** those fields mean something else per type (a timer's interval, an
event's conditions), and every reader would have to know which keys aren't about the schedule.

### C. Two columns on `triggers`, checked by the runner

**Pros:** one meaning for every type. Listing, auditing and the API read them like any other field.
**Cons:** a migration.

## Decision

**C.**

- `triggers` gains `cooldown_s` and `user_cooldown_s`, integer seconds from 0 to 24h, both 0 by default.
  0 means no cooldown, which is what every existing trigger keeps.
- `cooldown_s` is the whole-channel window: once the trigger fires, it doesn't fire again in that channel
  until the window ends. `user_cooldown_s` is per chatter: the same chatter doesn't set it off again until
  their window ends. Both must be clear, as with a command's shared and personal buckets (ADR-0006 §2).
- Triggers with nobody behind them (`timer`, `cron`, `stream_online`, `stream_offline`) take only the
  channel-wide one. Chat and the API refuse a per-chatter cooldown on them.
- `TriggerRunner` checks the windows before it runs a trigger and takes them at once, with no await in
  between, so two lines arriving together can't both get through. A firing inside a window is skipped and
  logged (`trigger.cooldown`). Trigger runs aren't written to `command_runs` today, so a skipped one isn't
  either. A run that starts and then fails, or is moderated away, has still used its turn.
- The windows live in memory, in a `CooldownTracker` like the commands' own, and reset on restart. A pack
  trigger's copies share an id, so the windows are keyed by channel and trigger.
- Chat: `listen|event|timer cooldown <id|name> [<channel> [<user>]]`. With no durations it shows what's
  set. With them it sets the channel-wide window and, if given, the per-chatter one (`30s`, `2m`, `1h`,
  `0` for none). A per-chatter cooldown that isn't given stays as it was. Listings show the cooldowns.
- API: `cooldown_s` and `user_cooldown_s` on `POST` and `PATCH /channels/{login}/triggers…`, and in
  every trigger's JSON. Changes are audited as `trigger.add` and `trigger.edit`, like the other fields.
- Pack triggers (ADR-0029) keep 0 until their owners can edit them. *Amended by ADR-0032:* they can now,
  with `cc pack trigger cooldown`.

## Consequences

- **Easier:** a listener on a common word can answer once a minute, or once per chatter an hour, without
  a custom command in between.
- **Harder:** a skipped firing leaves no row a moderator can see, only a log line. If that turns out to
  matter, trigger runs as a whole would need to be logged first.
- **doomtp-web:** two fields on the trigger form and the cooldowns in the trigger list.

## Action Items

1. [x] Migration 0015: `triggers.cooldown_s`, `triggers.user_cooldown_s`.
2. [x] `TriggerRunner` checks and takes the windows; `TriggerService` stores and checks them.
3. [x] Chat (`listen|event|timer cooldown`) and the API fields.
4. [ ] doomtp-web: the trigger form and list.
