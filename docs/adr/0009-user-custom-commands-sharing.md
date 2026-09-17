# ADR-0009: User-owned custom commands — aliases, publishing, linking, live edits

**Status:** Proposed (revision 2)
**Date:** 2026-09-16
**Deciders:** Project owner

## Context

Users build pipelines (see the command language proposal) and save them as commands:

- A creator uses their command under their own alias.
- They can **publish** it to channels where they have permission, so others can use it.
- Other users can **link** a command under their own alias and **republish** it in channels where *they* have permission.
- Commands are **editable**, and **edits propagate instantly** to every link and publication. There's no pinning or approval step.

Chat is untrusted, so sharing must not allow privilege escalation. Because edits are live, anyone who links or publishes must be **told clearly** that the command can change under them.

## Decision

### Model

```sql
custom_commands(
  id TEXT PRIMARY KEY,               -- short stable id, e.g. "cc_7f3k2"
  owner_user_id TEXT NOT NULL,
  name TEXT NOT NULL,                -- owner's canonical name
  summary TEXT, description TEXT,
  params TEXT,                       -- JSON: [{pos:"1", name, type, min, max, default, description}, {pos:"2+", …}]
  examples TEXT,                     -- JSON: [{invocation, output}]
  data_schema TEXT,                  -- optional JSON description of the final data
  current_version INTEGER NOT NULL,
  visibility TEXT DEFAULT 'private', -- private | shareable
  status TEXT DEFAULT 'active',      -- active | deleted | banned
  created_at INTEGER, updated_at INTEGER,
  UNIQUE (owner_user_id, name))

custom_command_versions(             -- history for audit and owner rollback, not pinning
  command_id TEXT, version INTEGER, body TEXT, params TEXT,
  created_at INTEGER, PRIMARY KEY (command_id, version))

custom_command_links(                -- personal aliases (own commands get one implicitly)
  user_id TEXT, alias TEXT, command_id TEXT, created_at INTEGER,
  PRIMARY KEY (user_id, alias))

custom_command_publications(
  channel_id TEXT, name TEXT, command_id TEXT, published_by TEXT,
  status TEXT DEFAULT 'active',      -- active | disabled (by channel mod) | orphaned (owner deleted)
  cooldown_overrides TEXT, required_role TEXT,
  created_at INTEGER, PRIMARY KEY (channel_id, name))
-- write grants: see ADR-0010 publication_write_grants
```

### Rules

| Action | Who | Notes |
|--------|-----|-------|
| Create | A user with rank ≥ the channel's `create_min_role` (default: everyone) where they type it; the web UI later | Quota per user (default 50). Names, bodies and docs pass the badword filter. Parsed and preflighted **against the creator** at save time. |
| Edit | Owner only | New version, **live immediately** in every link and publication. The confirmation says how many links and channels are affected. |
| Revert | Owner only | `!cc revert <name> <version>` creates a new version with the old body. Also live immediately. |
| Delete | Owner (soft delete) | Links and publications **stop working immediately**: code 127 with `command removed by its owner`. Publications show as `orphaned` in the channel's admin list. |
| Link | Any user, for `shareable` commands or commands published in a channel they're in | **The reply includes the live-edit warning.** |
| Publish | A user with rank ≥ the channel's `publish_min_role` (default: moderator), for their own or a linked command | **The reply includes the live-edit warning**, plus a write-grant warning if grants are requested. |
| Disable / unpublish / revoke grants | Channel mods | Audited |
| Ban a command or user from publishing | Bot owner or bot admin | Global |

### Live-edit warnings (exact wording to be refined)

- **Link:** `Linked "roll" (by @alice) as !roll. ⚠ @alice can edit or delete it at any time and changes apply to you immediately.`
- **Publish (someone else's command):** `Published "roll" (by @alice) in #channel. ⚠ @alice can edit or delete it at any time and changes apply here immediately. Mods can !cc disable roll.`
- **Publish with a grant:** the publish warning above, followed by `⚠ This command can write channel.chatter.points, including after future edits by @alice.`

The API and web UI show the same warnings, as a confirmation step.

### Change notices

- Each publication tracks the version it was last run with.
- After an edit, the first `!cc info` or admin UI view in that channel shows `changed since v3 by @alice`. That gives mods visibility without a blocking approval step.
- Optional per channel: `cc_edit_notice=on` makes the bot post a one-line notice the next time an edited command runs in that channel.

### Execution identity (the key safety rule)

- A custom command **always runs as the invoker.** Every inner command is checked for toggles, the invoker's rank and the invoker's cooldowns.
- The custom command itself has its own cooldown **in addition to** its inner commands' cooldowns.
- Variable access follows ADR-0010:
  - A foreign command can write only `publisher.*` and `publisher.chatter.*`.
  - It can write `channel.*` and `channel.chatter.*` only through explicit publication write grants.
- `{arg.*}` is validated against the declared params before the body runs. A failure returns code 2 with generated usage text.

### Name resolution in a channel

1. Built-in command or its alias (if enabled)
2. Channel publication
3. The invoker's personal alias

`!explain` shows the winner, the owner, the version and any grants. `!cc run cc_7f3k2 …` and `!@alias …` bypass resolution explicitly.

### Chat commands (sketch)

```
!cc add <name> "<expr>"          !cc edit <name> "<expr>"       !cc rm <name>
!cc revert <name> <version>      !cc versions <name>            !cc info <name|id>
!cc share <name> on|off
!cc link <id|channel:name> [alias]                              !cc unlink <alias>
!cc publish <alias> [as <name>] [--grant-write <var>…]          !cc unpublish <name>
!cc disable|enable <name>        !cc grant <name> revoke [var]  (channel mod)
!cc describe <name> "<summary>"
!cc param <name> <pos> name=<n> type=<t> [min=] [max=] [default=] "<description>"
!cc example <name> "<invocation>" "<expected output>"
```

## Options Considered

| Option | Verdict |
|--------|---------|
| **Live references, run as invoker, warnings, channel kill switches** (chosen) | Meets the requirement that edits propagate instantly. The risk is limited by invoker identity, explicit grants, audit, and mod disable/unpublish. |
| Live references with version pinning or approval | Safer for channels, but contradicts the requirement that edits propagate instantly. Removed. |
| Copy-on-share | Edits never propagate. Rejected. |
| Run as publisher (setuid) | Privilege escalation by design. Rejected. |

## Trade-off Analysis

Instant propagation makes the owner's trustworthiness matter to everyone who uses the command. Four things limit the damage a malicious or careless edit can do:

1. **Invoker identity:** no new permissions.
2. **Variable access rules:** no writes outside the author's own spaces, unless a mod granted them.
3. **Save-time preflight and filters.**
4. **Fast mod response:** `!cc disable`, with a change notice showing *what* changed.

The warnings make the trade-off explicit to users at the moment they accept it.

## Consequences

- **Easier:** fixes and improvements reach everyone at once, and a community command library is simple to run.
- **Harder:**
  - Mods must monitor the community commands they publish.
  - Deletions break channels immediately, visibly and deliberately.
- **Revisit:** if abuse happens, add per-channel "trusted publishers only", or re-introduce optional pinning.

## Action Items

1. [ ] Add the migrations and `customcmds/service.py`: create, edit, revert, delete, link, publish, disable, grants, resolve. All audited.
2. [ ] Add the warning strings (chat, API, UI) and the change-notice tracking.
3. [ ] Build parameter declaration, validation and generated usage text, shared with built-in specs.
4. [ ] Preflight and filter at save time. Add a cycle check across custom commands.
5. [ ] Add API endpoints `/api/v1/custom-commands` and `/api/v1/channels/{login}/publications` for the public page.
