# ADR-0010: Variables — explicit namespaces, no hidden sandboxing

**Status:** Accepted, revision 2 (implemented; see Action Items) — 2026-09-17
**Date:** 2026-09-16
**Deciders:** Project owner

## Context

Pipelines need persistent state such as counters, preferences, points and game state. Requested so far:

- `{chatter.x}`, **one space per user across all channels**
- `{channel.x}`
- `{publisher.x}`
- combined **channel + chatter** and **publisher + chatter** spaces

Custom commands are shared between users, and edits propagate instantly (ADR-0009). A command written by someone else must not be able to damage the invoker's data or the channel's data.

Revision 1 used a hidden sandbox that silently redirected `{chatter.x}` inside foreign commands. The combined namespaces make that unnecessary: isolation becomes **explicit in the placeholder name**.

## Decision

### Seven variable namespaces

The canonical list is in [namespaces.md](../namespaces.md). The full access rules per actor, the grant types and admin actions are in [variable-access-matrix.md](../variable-access-matrix.md), which is authoritative and still up for review. The table under "Access rules" below is a summary.

| Namespace | Key | Typical use |
|-----------|-----|-------------|
| `chatter.x` | `user_id` | Personal preferences across channels (`location`, `timezone`, `pronouns`) |
| `channel.x` | `channel_id` | Channel state (`deaths`, `goal`, `last_raid`) |
| `channel.chatter.x` | `channel_id` + `user_id` | Per-channel user state (`points`, `wins`, `streak`) |
| `publisher.x` | `owner_id` | A custom command author's shared state (`total_uses`, a word list) |
| `publisher.chatter.x` | `owner_id` + `user_id` | An author's per-user state (a game's save data for each player) |
| `publisher.channel.x` | `owner_id` + `channel_id` | Per-channel state of an author's game (current round, the secret number) |
| `publisher.channel.chatter.x` | `owner_id` + `channel_id` + `user_id` | A player's state in that channel's game (score, inventory) |

In combined namespaces, `chatter` is always the **invoker**, or the event user for triggers. `channel` is always the channel the run is in.

### Access rules

A command is **own** if the invoker typed it or owns the running custom command. A **built-in** is a code-defined command that declares its writes in its spec. A **foreign** command is a custom command owned by someone else.

| Namespace | Own / built-in: read | Own / built-in: write | Foreign custom command: read | Foreign custom command: write |
|-----------|---------------------|-----------------------|------------------------------|-------------------------------|
| `chatter.x` | ✔ | ✔ | ✔ | ✘ |
| `channel.x` | ✔ | rank ≥ `channel_var_write_role` (default mod), or a built-in declaring it | ✔ | only with a **write grant** |
| `channel.chatter.x` | ✔ | ✔ (own row only) | ✔ | only with a **write grant** |
| `publisher.x` | ✔ (own commands) | ✔ | ✔ (its own owner's space) | ✔ (its own owner's space) |
| `publisher.chatter.x` | ✔ | ✔ | ✔ | ✔ |
| `publisher.channel.x` | ✔ | ✔ | ✔ (current channel) | ✔ (current channel) |
| `publisher.channel.chatter.x` | ✔ | ✔ | ✔ | ✔ |

- **Write grants:**
  - A channel mod attaches a write grant to a **publication**: `!cc publish pts --grant-write channel.chatter.points`.
  - A grant names exact variables and is audited.
  - Because edits propagate instantly, the publish confirmation **warns** that future edits by the owner can use the grant. A mod can revoke it with `!cc grant pts revoke`.
- **Denied writes** fail preflight with code 126. `!explain` names the missing grant.
- **Nothing is silently redirected.** What you write is the key that gets used.
- **All variables are public (for now).** There are no private keys and no read grants. Access control covers writes only. Grants always name exact variables; there are no wildcards.

### Storage

```sql
variables(
  ns TEXT,
  -- ns                          key1        key2        key3
  -- chatter                     user_id     ''          ''
  -- channel                     channel_id  ''          ''
  -- channel.chatter             channel_id  user_id     ''
  -- publisher                   owner_id    ''          ''
  -- publisher.chatter           owner_id    user_id     ''
  -- publisher.channel           owner_id    channel_id  ''
  -- publisher.channel.chatter   owner_id    channel_id  user_id
  key1 TEXT, key2 TEXT DEFAULT '', key3 TEXT DEFAULT '',
  name TEXT, value TEXT,          -- JSON
  updated_at INTEGER, updated_by TEXT, updated_via TEXT,
  PRIMARY KEY (ns, key1, key2, key3, name))
-- leaderboards: top-N over the last key within a fixed prefix
CREATE INDEX ix_vars_board ON variables(ns, key1, key2, name);

publication_write_grants(channel_id TEXT, publication_name TEXT, variable TEXT,
                         granted_by TEXT, granted_at INTEGER,
                         PRIMARY KEY (channel_id, publication_name, variable))
```

- **Names:** `[a-z][a-z0-9_]{0,31}`, excluding reserved names (see namespaces.md §5).
- **Size limits:**
  - At most 2 KB of JSON per value.
  - At most 200 names per `(ns, key1, key2, key3)`.
  - At most 100,000 rows per channel across `channel.chatter` and `publisher.channel.chatter`.
  - Per-owner caps on `publisher.*` rows, so one author's game can't fill the database.
- **Operations:** `get`, `set`, `incr` (atomic), `append` (capped list), `del`, `top` (leaderboard over `channel.chatter`, `publisher.chatter` and `publisher.channel.chatter`). Available through `>` / `>>` and the `!var` commands.
- **Transactions:** writes are buffered per run and committed atomically when the run ends, unless it was cancelled. `incr` is applied at commit as `value + n`.
- **Filtering:** string values pass the badword filter's storage policy.
- **Audit:** writes to `channel.*` and grant changes go to the audit log. Other namespaces follow the command's log level.

## Options Considered

| Option | Verdict |
|--------|---------|
| **Explicit combined namespaces + write grants** (chosen) | Isolation is visible in the syntax. Supports leaderboards and per-author game state. No hidden behavior. |
| Hidden sandbox (revision 1) | Surprising: the same placeholder means different keys depending on who owns the command. Replaced. |
| No isolation | Any shared command could wipe any user's or channel's data. Rejected. |
| A per-command namespace for everything | Can't share state across an author's commands or with built-ins. Rejected. |

## Consequences

- **Easier:**
  - Reasoning about what a command can touch, since it's readable from its body.
  - Per-channel points and leaderboards.
  - Author-scoped games.
- **Harder:**
  - Mods must grant writes for channel-level community commands.
  - Five namespaces to document, handled by the registry.
- **Revisit:**
  - Retention rules for inactive `*.chatter` rows.
  - Private variables and per-link read grants (variable access matrix §7).

## Action Items

1. [x] Add the `variables` and `publication_write_grants` migrations, plus `variables/store.py` with buffered writes, atomic `incr`/`append` and `top`.
2. [x] Build an access matrix implementation with a table-driven test covering every namespace × own/built-in/foreign × grant case.
3. [x] Add `!var get|set|incr|del|list|top` and the `>`/`>>` executor support.
4. [x] Add the publish-time write-grant warning, `!cc grant … revoke`, and `!explain` output for denied writes. *(2026-09-20: publishing a command or a pack parses each body, collects `channel.*` and `channel.chatter.*` write targets, and names the ones without a grant plus the `cc grant` line that allows them; `explain`'s one-line answer says `can't write <variable>` for every denied store, and the structured report has said so per store since item 3.)*
