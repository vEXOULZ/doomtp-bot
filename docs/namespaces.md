# Placeholder Namespaces — Registry

**Status:** Canonical list (update together with any language or runtime change) · **Date:** 2026-09-16

Every `{…}` placeholder starts with one of the namespaces below. **No other root names are valid.** Adding a namespace means adding a row here, updating the parser's reserved list, and documenting it in `!help placeholders` and `GET /api/v1/namespaces`.

General form: `{ namespace . path [ :type ] [ ?? fallback ] }`. See [command-language-proposal.md](command-language-proposal.md).

---

## 1. Results

| Namespace | Meaning | Available in | Access |
|-----------|---------|--------------|--------|
| `{_}` | Result flowing into this command (pipe stdin, or previous result after `&&`/`\|\|`) | any expression | read |
| `{N}` (`{1}`, `{2}`, …) | Result of the Nth command in source order (1-based) | any expression; must be able to have run before use (checked at preflight) | read |

Paths on results:

| Path | Value |
|------|-------|
| `{1}` | data if scalar, else message |
| `{1.code}` | exit code |
| `{1.message}` | formatted message |
| `{1.data}` | full data |
| `{1.<key>}` / `{1.<key>.<n>}` | shortcut into data (e.g. `{1.celsius}`, `{1.items.0}`) |

`code`, `message` and `data` are reserved keys at the top level of a result. A data key with one of those names must be accessed as `{1.data.code}`.

## 2. Arguments

Only inside **custom command bodies**, trigger expressions (where the arguments are the event text) and callbacks. Built-in commands receive typed args directly, but **their documentation uses the same `arg.N` names**.

| Placeholder | Meaning |
|-------------|---------|
| `{arg.1}`, `{arg.2}` … | Nth argument (quotes removed; a quoted string is one argument) |
| `{arg.3+}` | Arguments 3..end, joined with single spaces, quotes removed. This is the "rest of the message" capture for unquoted text. *(Stripping quotes is the current choice; verify it in testing. See language proposal §5.)* |
| `{arg.3+raw}` | Arguments 3..end exactly as typed (original spacing and quotes) |
| `{arg.count}` | Number of arguments |
| `{args}` | Same as `{arg.1+}` |
| `{arg.<name>}` | Alias for a positional argument, if the command declares a name for it (e.g. `arg.1` = `sides` → `{arg.sides}`) |

### Types (`:type`)

A type is either **declared once** in the command's parameter definition, or given **inline** as `{arg.1:int}`. It validates the value and converts it. If validation fails, the command returns **code 2**, with a usage message generated from the parameter docs.

| Type | Accepts | Converted value / paths |
|------|---------|-------------------------|
| `str` | anything (default) | string |
| `int` | `-3`, `42` | integer; params may add `min` and `max` |
| `float` | `3.5`, `-0.2` | float |
| `bool` | `true/false/yes/no/on/off/1/0` | boolean |
| `range` | `1-100` | `{arg.1.lo}`, `{arg.1.hi}` |
| `duration` | `30s`, `10m`, `1h30m` | seconds; `{arg.1.seconds}` |
| `user` | `@name`, `name` | resolved Twitch user: `{arg.1.id}`, `{arg.1.name}`, `{arg.1.display}` |
| `choice(a,b,c)` | one of the listed values | string |
| `url` | http(s) URLs | string (passes a URL safety check) |

`.` is for **paths** and `:` is for **types**. This keeps `{arg.1.name}` (a path into a user) distinct from `{arg.1:int}` (type validation).

## 3. Variables (persistent)

| Namespace | Keyed by | Read | Write |
|-----------|----------|------|-------|
| `{chatter.x}` | user (global, all channels) | any pipeline (invoker's row) | invoker's own pipelines (typed by them, or their own custom commands) and built-ins declaring the write |
| `{channel.x}` | channel | anyone in the channel | invoker rank ≥ `channel_var_write_role` (default: moderator), built-ins declaring the write, or a publication with a **write grant** (ADR-0010) |
| `{channel.chatter.x}` | channel + user | anyone in the channel (e.g. leaderboards) | invoker's own pipelines, built-ins declaring the write, publications with a write grant |
| `{publisher.x}` | owner of the running custom command | only inside that owner's custom commands | same as read |
| `{publisher.chatter.x}` | owner of the running custom command + invoker | only inside that owner's custom commands | same as read |
| `{publisher.channel.x}` | owner + current channel | only inside that owner's custom commands (channel games, per-channel state) | same as read |
| `{publisher.channel.chatter.x}` | owner + current channel + invoker | only inside that owner's custom commands (per-player state in a channel game) | same as read |

- **`chatter` in these combined namespaces always means the invoker.** For triggers, it is the event user.
- **`channel` always means the channel the run is in.**
- **All variables are public for now.** Access control only restricts writes. Private variables are a future consideration.
- Other users' or channels' rows can't be addressed directly.
- The full read/write rules per actor, plus grants, are in **[variable-access-matrix.md](variable-access-matrix.md)**. That table is authoritative. The Read and Write columns above are a summary.

## 4. Context (read-only)

| Namespace | Fields | Available in |
|-----------|--------|--------------|
| `{chatter.*}` reserved fields | `id`, `name` (login), `display`, `rank`, `roles`, `is_sub`, `is_vip`, `is_mod` | anywhere a chatter exists |
| `{channel.*}` reserved fields | `id`, `name`, `display`, `prefix`, `live`, `title`, `game`, `viewers`, `uptime` | anywhere |
| `{publisher.*}` reserved fields | `id`, `name`, `display` (owner of the custom command) | custom commands |
| `{cmd.*}` | `name`, `alias`, `id`, `version`, `owner` | custom commands |
| `{bot.*}` | `name`, `id`, `version` | anywhere |
| `{now.*}` | `iso`, `unix`, `date`, `time`, `weekday` (channel timezone) | anywhere |
| `{event.*}` | trigger payload: `type`, `user.*`, `input`, `reward.*`, `viewers`, `bits`, `months`, `tier`, `message` | triggers only |
| `{match.*}` | `0` (whole match), `1..N`, named groups | listeners only |
| `{cooldown.*}` | `command`, `tier`, `tier_remaining`, `user_remaining` | `on_cooldown` callbacks only |
| `{denied.*}` | `command`, `required_role`, `rank` | `on_denied` callbacks only |
| `{run.*}` | `id`, `trigger` (`chat`, `timer`, `redemption`…) | anywhere |

## 5. Reserved names

- **Namespace roots:** `_`, digits, `arg`, `args`, `chatter`, `channel`, `publisher`, `cmd`, `bot`, `now`, `event`, `match`, `cooldown`, `denied`, `run`.
- **Variable names can't be:** any reserved field listed in §4 for that namespace, `chatter` (under `channel.`, `publisher.` and `publisher.channel.`), `channel` (under `publisher.`), `data`, `code`, `message`, `public`, `root`.
- **Variable name format:** `[a-z][a-z0-9_]{0,31}`.

## 6. Fallbacks

`{x ?? default}`: if `x` is missing, empty or fails type validation, the default is used instead. The default can be a literal or another placeholder: `{chatter.location ?? {channel.location ?? Lisbon}}`.
