# ADR-0032: Triggers on your own packs

**Status:** Accepted, 2026-10-15
**Date:** 2026-10-15
**Deciders:** Project owner
**Amends:** ADR-0029

## Context

ADR-0029 gave packs their own triggers: one row, fired in every channel where the pack is published and its
module is on. Only the pack script writes them, though, so only the bot's own packs (`pyramid`) can react on
their own. A user who builds a pack that should answer a phrase, greet raiders or post on a clock still has
to ask a moderator in every channel to add a trigger there by hand.

The owner asked for pack owners to manage their packs' triggers, in chat and on the site (2026-10-15). The
question this ADR has to answer is how far a trigger written by one user may reach in other people's
channels.

## Options

### A. Keep pack triggers script-only

**Pros:** nothing a user writes runs on its own anywhere else. **Cons:** a user's pack is never complete:
every channel that publishes it needs a moderator to add the triggers, and they outlive
`!module disable`.

### B. Let a pack's owner write its triggers, as a channel's moderator writes a channel's

**Pros:** the same grammar and rules. **Cons:** a channel trigger runs at its creator's rank and may call
anything published there. A pack's owner has no rank in most of the channels the pack reaches, and what a
command name means differs from one channel to the next.

### C. Let the owner write them, under rules for something that runs in other people's channels

**Pros:** the pack is complete on its own, and a channel that publishes it knows what it brings. **Cons:**
pack triggers are less capable than a channel's own.

## Decision

**C.** A pack's owner adds, replaces, toggles and removes its triggers. Script-installed triggers in the bot's
packs stay script-owned: the bot's account owns those packs, and system packs stay closed. A pack trigger:

- **runs at everyone's rank**, whoever wrote it, so it can't do moderator things in someone else's channel;
- **calls only its pack's public commands and what runs everywhere** (built-ins and system pack members).
  Other custom commands, the pack's internal members and personal links (`@name`) are refused when it is
  written, because what they mean differs by channel or chatter. Internal members stay reachable through a
  public member that calls them;
- **fires on a clock at most every 15 minutes**: a timer's interval is 15m or more, and a cron's closest two
  firings in a day are 15 minutes apart or more;
- **is one of at most 10** in its pack;
- takes the cooldowns of ADR-0031, and keeps them (and its on/off state) when it is replaced;
- keeps everything else of ADR-0029: it applies where the pack is published and its module is on, a channel
  turns it off with `!module disable <pack>`, and events that need a capability stay quiet where the channel
  hasn't granted it.

Chat:

```
cc pack trigger list <pack>
cc pack trigger add <pack> <key> listen </regex/|"regex"> <expression>
cc pack trigger add <pack> <key> event <type> <expression>
cc pack trigger add <pack> <key> timer <every> [jitter=<d>] [only_live] [min_lines=<n>] <expression>
cc pack trigger add <pack> <key> cron "<m h dom mon dow>" <expression>
cc pack trigger rm|on|off <pack> <key>
cc pack trigger cooldown <pack> <key> [<channel> [<user>]]
```

`add` on a key that exists replaces it. The raw tail starts after the trigger's kind, so a regex keeps its
backslashes: built-in commands' raw-tail subcommands can now be several words (`pack trigger add`). The
cooldowns are set apart from `add`, as `!listen cooldown` sets a channel's (ADR-0031).

API: `GET /me/packs/{name}/triggers`, `PUT` (add or replace), `PATCH` (`enabled` and the cooldowns) and
`DELETE /me/packs/{name}/triggers/{key}`. `GET /me/packs` lists each pack's triggers. Changes are audited
as `trigger.pack_set`, `trigger.pack_enable|disable`, `trigger.pack_edit` and `trigger.pack_remove`.

Publishing a pack says what triggers it brings: the chat reply names them and how to turn them off, and the
publish response lists their keys for the site to show.

## Consequences

- **Easier:** a pack is complete when its owner says so; a channel gets its behaviour by publishing it and
  loses all of it with `!module disable`.
- **Harder:** a pack's owner can make it speak in every channel that has it. The rank, the call rule, the
  clock limit and the cap bound what that can be, and the publish reply says it will happen. A channel that
  doesn't want it turns the module off.
- Pack triggers are keyed by pack id, not name: two owners can each have a pack of the same name.
- **doomtp-web:** a Triggers section on each pack in My packs, and pack triggers on a channel's Triggers
  tab saying how to turn them off.

## Action Items

1. [x] `TriggerService`: pack triggers by pack id; `set_pack_trigger`, `set_pack_trigger_enabled`,
   `set_pack_trigger_cooldowns` and `remove_pack_trigger`, with the rank, call, clock and cap rules.
2. [x] Chat: `cc pack trigger …`, multi-word raw-tail subcommands, triggers in `cc pack info` and in the
   `cc publish pack` reply.
3. [x] API: `/me/packs/{name}/triggers…`, triggers in `/me/packs` and in the pack publish response.
4. [x] Documentation: architecture §7, ADR-0029 and ADR-0031 amended, the roadmap.
5. [ ] doomtp-web: the Triggers section in My packs, and the pack note on a channel's Triggers tab.
