# Placeholder Namespaces — Registry

**Status:** Canonical list (update together with any language or runtime change) · **Date:** 2026-09-28 · **Syntax:** 2.0 (ADR-0018)

Every reference in a `{…}` placeholder starts with one of the roots below. **No other root names are valid.** Adding a root means adding a row here, updating `lang/parser.py` (`BOT_FIELDS`, `REGISTERED_ROOTS`) and `runtime/namespaces.py`, and checking that `GET /api/v1/language` reports it.

General form: `{ expression [ ?? fallback ] }`, where the simplest expression is one reference such as `{channel.deaths}`. The full grammar is in [command-language-spec.md](command-language-spec.md) §2.7–§2.8.

**`.` and `[ ]`.** A `.` walks names the bot defines: a namespace, a variable's name, a `$` field, a result's `code`/`message`/`data`. `[ ]` walks into a *value*: `{channel.stats[kills]}`, `{channel.log[-1]}`, `{channel.quotes[arg.1]}`, `{x["key with spaces"]}`. A bare word in brackets is a literal key; anything else (a number, a string, a reference, an operator) is an expression.

**`$` means the bot's.** Everything the bot supplies starts with `$` (`{$chatter.display}`); no `$` means a variable (`{channel.deaths}`). A new field can never collide with a variable.

---

## 1. Results

| Reference | Meaning | Available in |
|-----------|---------|--------------|
| `{_}` | Result flowing into this command (pipe stdin, or the previous result after `&&`/`\|\|`) | any expression |
| `{_N}` (`{_1}`, `{_2}`, …) | Result of the Nth command in source order (1-based) | any expression; must be able to have run before use (checked at preflight) |

A bare number is a number: `{1}` renders `1`. (For one release a typed line refuses `{1}` alone with the hint `a result is {_1} now`.)

| Form | Value |
|------|-------|
| `{_1}` | data if scalar, else message |
| `{_1.code}` | exit code |
| `{_1.message}` | formatted message |
| `{_1.data}` | full data |
| `{_1[key]}`, `{_1[items][0]}` | inside the data (e.g. `{_1[celsius]}`) |

At most one of `.code`, `.message`, `.data` follows a result; anything else goes in brackets, so a data key named `code` is `{_1[code]}`.

## 2. Arguments

Only inside **custom command bodies**, trigger expressions (where the arguments are the event text) and callbacks. Built-in commands receive typed args directly, but **their documentation uses the same `arg.N` names**.

| Placeholder | Meaning |
|-------------|---------|
| `{arg.1}`, `{arg.2}` … | Nth argument (quotes removed; a quoted string is one argument) |
| `{arg.3+}` | Arguments 3..end, joined with single spaces, quotes removed. This is the "rest of the message" capture for unquoted text. *(Stripping quotes is the current choice; verify it in testing. See language proposal §5.)* |
| `{arg.3+raw}` | Arguments 3..end exactly as typed (original spacing and quotes; a placeholder there counts as the text it expanded to) |
| `{arg.count}` | Number of arguments |
| `{args}` | Same as `{arg.1+}` |
| `{arg.<name>}` | Alias for a positional argument, if the command declares a name for it (e.g. `arg.1` = `sides` → `{arg.sides}`) |

### Types (`:type`)

A type is either **declared once** in the command's parameter definition, or given **inline** as `{arg.1:int}`. It validates the value and converts it. If a declared parameter fails validation, the command returns **code 2**, with a usage message generated from the parameter docs. A failed inline cast counts as missing: the `??` fallback is used, or the invocation fails with 230 (`E_MISSING_VALUE`).

| Type | Accepts | Converted value / paths |
|------|---------|-------------------------|
| `str` | anything (default) | string |
| `int` | `-3`, `42` | integer; params may add `min` and `max` |
| `float` | `3.5`, `-0.2` | float |
| `bool` | `true/false/yes/no/on/off/1/0` | boolean |
| `range` | `1-100` | map: `{arg.1[lo]}`, `{arg.1[hi]}` |
| `duration` | `30s`, `10m`, `1h30m`, or plain seconds | integer seconds |
| `user` | `@name`, `name` | resolved Twitch user, a map: `{arg.1[id]}`, `{arg.1[name]}`, `{arg.1[display]}` |
| `choice(a,b,c)` | one of the listed values | string |
| `url` | http(s) URLs | string (passes a URL safety check) |
| `list` | a placeholder holding a list (passed through), or JSON `[…]` | list: `[i]`, `[-i]`, `:len` |
| `map` | a placeholder holding a map (passed through), or JSON `{…}` | map: `[key]`, `:len`, `:keys`, `:values` |

`[ ]` goes **into a value** and `:` **converts or measures** it. This keeps `{arg.1[name]}` (a key of a user) distinct from `{arg.1:int}` (type validation). The accessors `:len`, `:keys` and `:values` work on any list or map.

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
- Read inside a stored value with brackets: `{channel.stats[kills]}`. Writes take the same path: `!var set channel.stats[kills] 3`, `-> channel.stats[kills]`, `--> channel.log` (append).
- The full read/write rules per actor, plus grants, are in **[variable-access-matrix.md](variable-access-matrix.md)**. That table is authoritative. The Read and Write columns above are a summary.

## 4. Context (read-only)

| Root | Fields | Available in |
|------|--------|--------------|
| `{$chatter.*}` | `id`, `name` (login), `display`, `rank`, `roles`, `is_sub`, `is_vip`, `is_mod` | anywhere |
| `{$channel.*}` | `id`, `name`, `display`, `prefix`, `live`, `title`, `game`, `viewers`, `uptime` (seconds; `:human` says it for people), `next_stream` (`[title]`, `[category]`, `[start]`, `[in]` seconds from now; missing when nothing is scheduled) | anywhere |
| `{$publisher.*}` | `id`, `name`, `display` (owner of the custom command) | custom commands |
| `{$bot.*}` | `name`, `id`, `version` | anywhere |
| `{$now.*}` | `iso`, `unix`, `date`, `time`, `weekday` (channel timezone) | anywhere |
| `{cmd.*}` | `name`, `alias`, `id`, `version`, `owner` | custom commands |
| `{event.*}` | trigger payload: `type`, `user.*`, `input`, `reward.*`, `viewers`, `bits`, `months`, `tier`, `message`; for `pyramid`, `pyramid_id`, `phase`, `direction`, `token`, `width`, `peak`, `breaker.*`, `by_bot`, `self_broken` (ADR-0028) | custom commands and triggers |
| `{match.*}` | `0` (whole match), `1..N`, named groups | listeners only |
| `{cooldown.*}` | `command`, `tier`, `tier_remaining`, `user_remaining` | `on_cooldown` callbacks only |
| `{denied.*}` | `command`, `required_role`, `rank` | `on_denied` callbacks only |
| `{run.*}` | `id`, `trigger` (`chat`, `timer`, `redemption`…) | anywhere |

A `$` root takes exactly one field. `cmd`, `event`, `match`, `cooldown`, `denied` and `run` are structures the bot defines, so their dots go as deep as the structure does.

## 5. Reserved names

- **Roots:** `_`, `_N`, `arg`, `args`, `$chatter`, `$channel`, `$publisher`, `$bot`, `$now`, `cmd`, `event`, `match`, `cooldown`, `denied`, `run`, and the variable namespaces of §3. The expression keywords `and`, `or`, `not`, `in`, `true` and `false` are never a reference.
- **Variable names can't be** (a closed list, `runtime/namespaces.py`):
  - `data`, `code`, `message`, `public`, `root`, in every namespace;
  - `chatter` under `channel.`, `publisher.` and `publisher.channel.`, and `channel` under `publisher.`, because those spell a longer namespace.

  Since the bot's fields moved behind `$`, names like `channel.title` are ordinary variables, separate from `$channel.title`.
- **Variable name format:** `[a-z][a-z0-9_]{0,31}`.

## 6. Fallbacks

`{x ?? default}`: if `x` is missing, empty or fails type validation, the default is used instead. The default can be a literal or another placeholder: `{chatter.location ?? {channel.location ?? Lisbon}}`. In an expression, `??` is an operator too: `{(arg.1 ?? "") == "add"}`.
