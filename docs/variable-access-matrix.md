# Variable Access Matrix — for review

**Status:** Reviewed (round 1 complete) · **Date:** 2026-09-16
Related: [namespaces.md](namespaces.md) · [ADR-0010](adr/0010-variables-scopes.md) · [ADR-0009](adr/0009-user-custom-commands-sharing.md)

Legend:
- **R** = read
- **W** = write (set, incr, append, del)
- **—** = no access
- **G** = only with a matching **grant**
- **(own)** = only the row for the invoker or event user
- **(here)** = only the row for the current channel
- **⚑** = design choice to confirm in review

---

## 1. Namespaces

| Namespace | Key | Example |
|-----------|-----|---------|
| `chatter.x` | user | `{chatter.location}` |
| `channel.x` | channel | `{channel.deaths}` |
| `channel.chatter.x` | channel + user | `{channel.chatter.points}` |
| `publisher.x` | owner | `{publisher.total_uses}` |
| `publisher.chatter.x` | owner + user | `{publisher.chatter.save}` |
| `publisher.channel.x` | owner + channel | `{publisher.channel.round}` (a channel game's state) |
| `publisher.channel.chatter.x` | owner + channel + user | `{publisher.channel.chatter.score}` (a player's score in that channel's game) |

In every combined namespace, `chatter` = the invoker (or the event user for triggers) and `channel` = the channel the run is in. **No pipeline can address another user's or another channel's row directly.** Cross-row access exists only through built-in aggregate commands such as `!var top`.

## 2. Who is running the code

| Code | Description |
|------|-------------|
| **Typed** | An expression the invoker typed directly in chat |
| **Own CC** | A custom command whose owner is the invoker |
| **Built-in** | A code-defined command. It can only touch the variables its spec declares. |
| **Foreign CC (link)** | Someone else's custom command, run through the invoker's personal link |
| **Foreign CC (pub)** | Someone else's custom command, run through a channel publication |
| **Trigger** | A redemption, event, timer or listener expression configured by a channel mod. It runs at `run_as_rank`, capped at its creator's rank. |
| **Callback** | An `on_cooldown` or `on_denied` expression |

Access for a foreign custom command applies to the command owner's own `publisher.*` spaces only. A command can never reach another owner's `publisher.*`.

## 3. Pipeline access matrix

| Namespace | Typed | Own CC | Built-in | Foreign CC (link) | Foreign CC (pub) | Trigger | Callback |
|-----------|-------|--------|----------|-------------------|------------------|---------|----------|
| `chatter.x` | R W (own) | R W (own) | R W (own, declared) | R (own) · W: — | R (own) · W: — | R (own) · W: — | R (own) · W: — |
| `channel.x` | R · W if rank ≥ `channel_var_write_role` | R · W if rank ≥ `channel_var_write_role` | R · W (declared) | R · W: — | R · W: G | R · W if `run_as_rank` ≥ `channel_var_write_role` | R · W: — |
| `channel.chatter.x` | R (here) · W (own) | R (here) · W (own) | R (here) · W (own, declared) | R (here) · W: — | R (here) · W (own): G | R (here) · W (own) | R (here) · W: — |
| `publisher.x` | — | R W | — | R W | R W | — | — |
| `publisher.chatter.x` | — | R W (own) | — | R W (own) | R W (own) | — | — |
| `publisher.channel.x` | — | R W (here) | — | R W (here) | R W (here) | — | — |
| `publisher.channel.chatter.x` | — | R W (here, own) | — | R W (here, own) | R W (here, own) | — | — |

Notes:
- **`publisher.*` in Typed code:** there is no publisher when the invoker types an expression directly, so these namespaces are invalid there (preflight code 2).
- **Foreign CC (link) and `publisher.channel.*`** *(decided)*: a personally linked command can use channel-game state **in any channel**, even where it isn't published. The data stays inside the owner's space for that channel.
- **Trigger and `publisher.*`** *(decided)*: denied in the trigger's own expression. A trigger that calls a published custom command runs *that command* as a Foreign CC (pub), with that command's access.
- **Trigger and `chatter.x` writes** *(decided)*: denied. A redemption can't change a viewer's global preferences. `channel.chatter.x` is the place for per-viewer state from events.
- **Built-ins** only get what their spec lists in `reads` and `writes`, as `namespace.name`; anything else fails the command with code 126 (architecture §4.2). `!var` is the exception, because it's a built-in acting *as the typed expression* and follows the Typed column: its spec declares `*`, and a test keeps it the only one that does.
- A denied `!var` write or delete fails with **code 126**, so it is silent and runs the `on_denied` callback, like every other denial (spec §6.6).

## 4. Grants

### 4.1 Grant types

| Grant | Attached to | Allows | Scope | Issued by | Revoked by |
|-------|-------------|--------|-------|-----------|------------|
| `write channel.<name>` | Publication | A Foreign CC (pub) to write `channel.<name>` | That channel | rank ≥ `grant_min_role` (default: moderator) **and** rank ≥ `channel_var_write_role` | Any rank ≥ `grant_min_role`, bot admin |
| `write channel.chatter.<name>` | Publication | A Foreign CC (pub) to write the invoker's own `channel.chatter.<name>` row | That channel | same as above | same as above |

- **Every grant names exact variables.** There are no wildcard grants.
- **There are no read grants.** Every variable is readable (§4.2).
- **Grants follow instant edits (ADR-0009).** When a grant is issued, the bot replies with the warning `⚠ @owner's future edits can use this grant`.
- **Missing grants are announced, not discovered.** Publishing a command or a pack parses each body and names the `channel.*` and `channel.chatter.*` writes that have no grant yet, with the `cc grant` line that allows them; `explain` says `can't write <variable>` for any store the caller couldn't make.
- **Grants are audited:** issue, revoke, and automatic revoke when the publication is removed.
- **Grants are never transferable.** Republishing the same command in another channel starts with no grants.

### 4.2 Visibility: all variables are public (for now)

- **Every variable in every namespace is readable.** There are no private variables, no `public` flag and no read grants.
- Access control covers **writes only.**
- **Reads by pipelines** are still limited to what the pipeline can *address*: the invoker's own row, the current channel, and the running command's owner. That comes from the namespace keys, not from privacy rules.
- **Reads through `!var`, the API and the web UI** can target any user's or channel's rows. For example `!var get @alice chatter.location` or `GET /api/v1/users/{id}/variables`.
- **Consequence for users:** don't store anything private in variables. `!var set` and the web UI should say so.
- **Future consideration:** private variables, a per-key `private` flag, and per-link read grants. Tracked in §7.

### 4.3 Channel settings that control access

| Setting | Default | Controls |
|---------|---------|----------|
| `channel_var_write_role` | moderator | Minimum rank for typed or own code, and triggers, to write `channel.*` |
| `grant_min_role` | moderator | Minimum rank to issue or revoke publication grants |
| `publish_min_role` | moderator | Minimum rank to publish custom commands (ADR-0009) |
| `create_min_role` | everyone | Minimum rank to create custom commands in this channel |
| `var_admin_role` | moderator | Minimum rank to reset variables in this channel (§5). Reading needs no rank, because everything is public. |

## 5. Administrative access (chat `!var`, API, web UI)

This covers inspecting and managing stored values outside of pipelines.

| Action | Self | Command owner | Channel mod (≥ `var_admin_role`) | Broadcaster | Bot admin / owner |
|--------|------|---------------|----------------------------------|-------------|-------------------|
| List/read any user's `chatter.*` (all public) | ✔ | ✔ | ✔ | ✔ | ✔ |
| Set/delete own `chatter.*` | ✔ | — | — | — | ✔ |
| Delete another user's `chatter.*` | — | — | — | — | ✔ (audited) |
| List/read `channel.*` (all public) | ✔ | ✔ | ✔ | ✔ | ✔ |
| Set/delete `channel.*` | if rank ≥ `channel_var_write_role` | — | ✔ | ✔ | ✔ |
| Read any `channel.chatter.*` (all public) | ✔ | ✔ | ✔ | ✔ | ✔ |
| Reset a user's `channel.chatter.*` here | — | — | ✔ | ✔ | ✔ |
| Read `publisher.*` / `publisher.chatter.*` (all public) | ✔ | ✔ | ✔ | ✔ | ✔ |
| Reset `publisher.*` / `publisher.chatter.*` | — | ✔ | — | — | ✔ |
| Read `publisher.channel.*` (+`.chatter`) (all public) | ✔ | ✔ | ✔ | ✔ | ✔ |
| Reset `publisher.channel.*` (+`.chatter`) for this channel | — | ✔ | ✔ (moderation) | ✔ | ✔ |
| Issue/revoke publication grants | — | — | ✔ (≥ `grant_min_role`) | ✔ | ✔ |
| Leaderboards (`!var top`) over `channel.chatter.*` / `publisher.channel.chatter.*` | ✔ | ✔ | ✔ | ✔ | ✔ |

**Audit:**
- Always audited: every administrative write or reset, and every grant change.
- Pipeline writes to `channel.*` are audited too.
- Other pipeline writes follow the command's log level.

## 6. Review checklist

- [x] §3 Foreign CC (link) may use `publisher.channel.*` → **any channel**
- [x] §3 Triggers are denied writes to global `chatter.*` → **yes**
- [x] §3 Triggers are denied `publisher.*` in their own expression → **yes**
- [x] §4.1 Wildcard `channel.*` grants → **no**
- [x] §4.1 Read grants on links → **no, for now. All variables are public** (§4.2). Private variables are a future consideration.
- [x] §5 Users can read their own rows in `publisher.*` spaces → **yes** (and now everyone can, since all variables are public)
- [x] §5 Channel mods can reset `publisher.channel.*` for their channel → **yes**
- [x] §4.3 Defaults → **yes, except `publish_min_role` = moderator** (was vip)

**Review complete.** There are no open ⚑ items.

## 7. Future considerations

| Item | Notes |
|------|-------|
| Private variables | A per-key `private` flag. Private keys readable only by own and built-in code, and by bot admins (audited). |
| Read grants on links | Let a linker allow one foreign command to read one private key. Only meaningful once private variables exist. |
| Wildcard write grants | Rejected for now. Revisit only if exact-name grants turn out to be painful. |
