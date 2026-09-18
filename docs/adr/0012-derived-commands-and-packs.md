# ADR-0012: Derived commands and packs — custom commands as built-ins, grouped

**Status:** Accepted
**Date:** 2026-09-18
**Deciders:** Project owner

## Context

Two needs came out of using custom commands (ADR-0009):

1. **Commands the bot should offer everywhere**, written in the command language rather than Python. A
   built-in like `!ping` is *primitive*: Python code in `modules/`. A command like `!hug` is **derived**:
   an expression over primitives. Writing derived commands in the language means they can be fixed live,
   read by anyone, and versioned like any other custom command — but publishing them channel by channel
   doesn't scale.
2. **Commands that belong together.** A blackjack game is `hit`, `stand`, `deal` and `score`. A channel
   wants all of them or none, and publishing four commands by hand invites half-installed games.

## Decision

### Derived commands are global publications

A **derived command** is an ordinary custom command published to the **global scope** (`channel_id = '*'`,
the same sentinel policy rows already use). Nothing new is stored: versions, audit, `!cc info`, write
grants and live edits work exactly as they do per channel.

- Resolution in a channel becomes: **built-in → channel publication → channel pack → global publication →
  global pack → the invoker's personal alias** (spec §5.1).
- Publishing globally requires **bot owner or bot admin** (`!cc publish <name> global`). It is audited.
- Primitives still win. A derived command can never shadow a Python built-in, so `!ping` is always `!ping`.
- A channel can disable a derived command like any other: `!cmd disable <name>` or `!module disable <pack>`.

### Packs group commands

A **pack** is a named set of a user's commands: `!cc pack create blackjack`, `!cc pack add blackjack hit
stand`. Publishing the pack publishes **every member at once**, and unpublishing removes them together.

- A channel stores **one row per published pack**, not one per member. New members therefore appear
  immediately, which is the same live-edit rule as ADR-0009: what a channel accepted is *the pack*, and the
  owner can change what is in it. The publish reply says so.
- The pack name becomes the **module name** of its commands, so `!module disable blackjack` turns the whole
  game off in a channel, and `!cmd disable hit` turns off one command. Pack names may not collide with a
  built-in module name.
- Publishing a pack fails, and changes nothing, if any member's name is already published in that channel by
  a different command. The reply lists the conflicts.
- A command may belong to several packs. Its policy key stays its command id, so cooldowns and permissions
  follow the command, not the pack it arrived through.

## Options Considered

| Option | Verdict |
|--------|---------|
| **Global publications + packs** (chosen) | Reuses everything custom commands already have. Derived commands are editable live by admins and visible to users like any other command. |
| Derived commands shipped in repo files, loaded at boot | Reviewable in git and identical everywhere, but a fix needs a deploy, and none of the versioning, audit or `!cc info` machinery applies. Worth revisiting if a curated default set grows. |
| Files seed the database at boot | Two sources of truth and a merge rule; rejected for now. |
| Reuse the existing `module` string instead of packs | No owner, no atomic publish, no membership: just a shared label. Rejected. |
| Group by name prefix (`blackjack-hit`) | Zero schema, but fragile and it constrains command names. Rejected. |
| Pin pack membership at publish time | Safer for channels, but it contradicts the live-edit rule and adds a pinned-membership concept. Rejected. |

## Consequences

- **Easier:** shipping a command set as one unit; fixing a derived command everywhere in one edit; reading
  what the bot offers, because derived commands are just commands.
- **Harder:**
  - A malicious or careless global edit reaches every channel at once. The limits are the same as ADR-0009:
    the body runs as the invoker, variable writes need grants, and any channel can disable it. Only bot
    owners and admins can publish globally.
  - Two scopes to look at when a name misbehaves. `!explain` and `!cc info` must say which scope won.
  - A pack whose member is deleted by its owner silently shrinks. The pack listing shows it.
- **Revisit:** if derived commands become a curated library, consider shipping a starter set as files that
  seed global publications on first boot.

## Action Items

1. [x] Migration: `custom_command_packs`, `custom_command_pack_members`, `custom_command_pack_publications`.
2. [x] Resolution through channel publication → channel pack → global publication → global pack.
3. [x] `!cc pack create|add|rm|list|info`, `!cc publish pack <name> [global]`, `!cc unpublish pack <name>`.
4. [x] Pack name becomes the module name, so existing `!module` toggles apply.
5. [x] `!explain` and `/api/v1/commands` report which scope a name resolved through. *(the report carries source, owner and version per invocation)*
6. [ ] A starter set of derived commands, once the badword filter and `!cc param` docs settle.
