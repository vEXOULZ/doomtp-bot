# ADR-0029: Pack triggers

**Status:** Accepted, 2026-10-04
**Date:** 2026-10-04
**Deciders:** Project owner

## Context

A pack is a module: published globally, it is on in every channel until `!module disable <pack>`
(ADR-0012, ADR-0019). Its commands only run when something calls them, though. A pack that reacts on its
own needs a trigger, and triggers belong to one channel each (architecture §7). So the `pyramid` pack
(ADR-0028) is "on" everywhere, but does nothing until a moderator in each channel types
`!event add pyramid pyramid_on_event`.

The same goes for any other kind of trigger. A pack with an hourly reminder needs a `cron` or `timer`
trigger in every channel, and a pack that answers a phrase needs a `listener` in every channel. Each one
added by hand, per channel, defeats the point of publishing the pack once.

## Options

### A. Keep per-channel triggers, and document the setup line

**Pros:** no change. **Cons:** every channel needs a moderator to type a line before the pack works;
turning the module off leaves its triggers firing into commands that are off.

### B. The install script adds a trigger in every joined channel

**Pros:** no schema change. **Cons:** channels joined later miss it; a fix to the trigger means
rewriting a row per channel; the triggers outlive `!module disable`.

### C. Triggers owned by a pack, fired wherever the pack is on

**Pros:** one row per pack trigger, of any type; it follows the pack's publication and its module toggle,
including channels joined later. **Cons:** a schema change, and the trigger service has to work out
where each one applies.

## Decision

**C.** A trigger can belong to a pack instead of a channel:

- `triggers` gains `pack_id` and `pack_key`. A pack trigger has `channel_id = '*'`, its `pack_id`, and a
  `pack_key` unique within the pack, which the install script matches on.
- Any trigger type works: events (including watcher types such as `pyramid`), listeners, timers and
  crons, with the same `match`, `schedule` and checks as a channel's own trigger.
- A pack trigger applies in a channel when the pack is published there or globally, and the pack's
  module is on there. `TriggerService` hands it out as a per-channel copy wherever it applies, so the
  dispatcher, the watcher gate (ADR-0028) and the timer scheduler need no special case. Timer and cron
  state is kept per trigger and channel.
- Pack triggers are written only by the script that owns the pack (`scripts/starter_pack.py`). Chat and
  the API list them in each channel where they apply, marked with their pack, and refuse to edit, toggle
  or delete them. A channel turns one off with `!module disable <pack>`.
- The pack's module toggle and publications are read from the policy snapshot and the trigger cache, which
  reloads after a pack is published or unpublished.

The `pyramid` pack gets a `pyramid` trigger running `pyramid_on_event`, so pyramids are watched in every
channel by default, at the pack's default chances.

## Consequences

- **Easier:** a pack is complete on install. A channel gets its behaviour by having the module on, and
  loses all of it with `!module disable`.
- **Harder:** trigger ids are no longer unique per channel row: a pack trigger's id is shared by its
  copies. Anything keyed on a trigger id alone, such as timer state, keys on the id and the channel.
- **doomtp-web:** the trigger list carries a `pack` field, and pack triggers can't be edited there.

## Action Items

1. [x] Migration: `triggers.pack_id`, `triggers.pack_key`.
2. [x] `TriggerService`: pack triggers, applied per channel by publication and module toggle; refused by
   edit, toggle and delete.
3. [x] Timer and cron state per trigger and channel.
4. [x] `scripts/starter_pack.py` installs pack triggers, and the `pyramid` pack's event trigger.
5. [x] Documentation: architecture §7, the README, ADR-0028's setup line.
