# ADR-0033: Ranks on pack members

**Status:** Accepted, 2026-10-15
**Date:** 2026-10-15
**Deciders:** Project owner

## Context

A pack's members run at role `everyone` wherever the pack is published (`spec_for`), and a channel can
change that with `!perm`. A command meant for moderators has to check for itself: the quotes pack's
`quote_add` and `quote_del`, and the pyramid pack's settings commands, all begin with
`ifelse {$chatter.is_mod}`. The command list says `everyone` for each, which is wrong, and a pack's owner
has no way to say who a command is for. A channel that lowers a moderator command with `!perm` would let
anyone change the channel's quotes if the body didn't check.

The owner decided (2026-10-15) that a pack sets a rank on each member as a floor: a channel can raise it,
never lower it. Chat and the web must work the same way.

## Options

### A. Keep the checks in the bodies

**Pros:** no change. **Cons:** lists show the wrong rank, and every pack author has to remember the check.

### B. A default rank a channel can change either way

**Pros:** the same as a built-in's `required_role`. **Cons:** a channel's `!perm` could open a command its
author meant for moderators, and the body would still need its check.

### C. A rank on the pack membership, as a floor

**Pros:** the rank is declared once, shows in every list, and the policy enforces it. **Cons:** a column
and a new chat and API control.

## Decision

**C.**

- `custom_command_pack_members` gains `role`, one of the built-in roles from `subscriber` to
  `broadcaster`. NULL is everyone, which every existing member keeps. A channel's own roles can't be
  used: a pack is published to channels that don't have them.
- `spec_for` gives a member reached through its pack `required_role` and `min_role` both set to that rank.
  `PolicyService.required_role` never returns a role under `min_role`, and drops `allowed_roles` under it,
  so a rule a channel wrote before the pack set its rank can't lower it either. `!perm set|allow` and
  `PATCH /channels/{login}/commands/{name}` refuse a role under it and say why.
- The rank applies wherever the member is reached through the pack, internal members included, so a
  public command calling an internal one for moderators is refused at that call. Inside a body, a refused
  call fails with "that takes <role> rank", which is what chat sees.
- System packs ignore it: their members are sentinels (ADR-0019).
- Chat: `cc pack role <pack> <command…> <role>`, with `everyone` to clear it. `cc pack info` shows each
  member's rank.
- API: `role` on `PUT /me/packs/{name}/commands/{command}`. Each member's `role` in `/me/packs`, and in
  `/packs` and `/channels/{login}/packs`. `min_role` on every command in `/channels/{login}/commands`.
- Audited as `pack.role`.
- The bot's packs declare it in `scripts/starter_pack.py` (`Derived.role`): `quote_add`, `quote_del`,
  `pyramid_set`, `pyramid_exempt`, `pyramid_addfact` and `pyramid_delfact` need moderator and drop their
  own checks.

## Consequences

- **Easier:** a pack says who each command is for, lists say so too, and a channel can't open a
  moderator command by accident.
- **Harder:** a channel that wants a moderator pack command open to everyone has to ask the pack's
  owner, or copy the command.
- **doomtp-web:** the rank on the pack editor and in the command lists, and the floor in the rule editor.

## Action Items

1. [x] Migration 0016: `custom_command_pack_members.role`.
2. [x] `min_role` on `CommandSpec`, the floor in `PolicyService`, and the refusals in `!perm` and the API.
3. [x] Chat (`cc pack role`), the API field and the listings.
4. [x] The bot's packs set their ranks.
5. [ ] doomtp-web: the pack editor and the lists.
