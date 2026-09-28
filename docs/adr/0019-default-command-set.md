# ADR-0019: The default command set, bot-owned packs and storage limits

**Status:** Accepted — 2026-09-28
**Date:** 2026-09-28
**Deciders:** Project owner

## Context

The built-in commands grew one feature at a time, and nobody had written down which commands should
ship by default or why. On 2026-09-28 every built-in was reviewed, along with what it would take to
write each one in the language instead of in Python. This ADR records the result. The language changes
it depends on are in ADR-0018.

The rule used throughout: a command is **primitive** (Python) only when it needs something the language
can't do, such as a Twitch call, randomness, or engine policy. Everything else is **derived** (ADR-0012)
and published by the bot account.

## Decision

### Sentinels (`core`)

- `true`, `fail`, `echo`, `check`, `ifelse`, `calc` and the operator commands are primitive. `true`
  stays primitive because it passes the previous data through.
- `false` (= `fail`) and `default` (= `echo {args}`) become derived. They live in a **system pack**: it
  is bot-owned, can't be toggled, shadowed or unpublished, and resolves at the sentinel step. A sentinel
  may be derived only if its body uses commands that can never be disabled.

### Channel management (`core_admin`)

These stay as they are, plus:
- `!admin quota` and `!admin valuecap`, each taking `default <size>` or `<channel|publisher|chatter>
  <name> <size|reset>`.
- `!customecho show|set|clear <cmd>` (below).

### Other primitives

- **`help`** appends a link to the channel's command page on the web site.
- **`random`** takes an optional seed of any type: the same seed always gives the same result. It also
  picks one element from a list or map, with data `{key, value}`. An empty collection fails with
  `E_EMPTY`.
- **`shoutout`** is Twitch's native card only, with no chat line. It is silent on success and fails
  with code 1 and Twitch's reason otherwise.
- **Moderation candidates:** `ban`/`unban`/`untimeout`, `delete`, `warn`, `announce`, `chatmode`, `clear`,
  `shield`, `settitle`/`setgame`, `marker` and `raid`. Each needs its Helix endpoint and scope confirmed
  first. `pin`/`unpin` has no known public endpoint.
- **Automation** replaces `trigger` with three commands on the same `triggers` table: `listen` (chat
  regex, named), `event` (Twitch events) and `timer` (interval or quoted cron). `!trigger` stays as a
  deprecated alias for one release.

### Derived commands

- **`ping`** moves out of Python into the `starter` pack.
- **`starter`** keeps `hug`, `lurk`, `roll` and `deaths`. It changes `so` to call `shoutout` and then
  echo a line, and adds the readouts `uptime`, `title`, `game`, `viewers`, `time`, `bot` and
  `nextstream`. `$channel.next_stream` comes from Helix `GET /schedule`, cached like `fetch_live`.
- **Quotes** become a derived pack. `quote` dispatches with `ifelse` to the internal members
  `quote_add`, `quote_del`, `quote_show` and `quote_random`, with the data in `channel.quotes` and
  `channel.quote_next`. `modules/quotes.py`, `quotes.py` and the `quotes` table are removed after the
  data is migrated. `quote find` waits for search over values.
- **Internal pack members** can be called only from bodies in the same pack. They are hidden from
  `help`, and code 127 when typed.
- **`customecho`:** readouts look up `channel.customecho[<cmd>]` first. The `:template` accessor renders
  a stored string at the invoker's rank, one level deep, with no `{!…}` and no nested templates.

### Bot-owned packs install by script

ADR-0012 item 6 stands: an admin installs bot-owned packs by running a script, and startup doesn't
write them. The database stays the only source of truth.

- `scripts/starter_pack.py` installs the `core` system pack as well as `starter`, idempotently. An
  upgrade ships by editing the definitions and running the script again.
- Once `false` and `default` live in `core`, a bot without that pack is broken. So **startup checks
  that `core` is installed and at least the version the code expects**, and refuses to start if it
  isn't. The error names the script to run.
- `starter` is optional, and startup doesn't check it.

### Links in bot output

All bot output passes through one rule. A link is clickable when **the bot itself** is a moderator or
VIP in the channel, or the channel is the bot's own. Otherwise every `.` in the link's host becomes
` dot `. The rule applies whoever asked, so chatters can post links through `echo` when the bot is a
moderator.

- **Moderator status** comes from the existing `MODERATE` capability.
- **VIP status** comes from the `badges` on the bot's own messages in the chat log (`is_self`). These
  are cached per channel and refreshed from each message the bot sends.
- **Where:** it is applied in `Outbox.send`, after the content filter and before chunking.

### Storage limits

- **Quota per namespace owner:** each channel, publisher and chatter gets 1 MB by default. Every
  namespace counts against one of those three owners (spec, `docs/namespaces.md`).
- **Per-value cap:** 256 KB by default, replacing today's 2 KB.
- **Changing them:** admins set both the same way, as a global default or an override for one owner,
  through `!admin` or the admin UI (a JSON endpoint here, plus a doomtp-web PR).
- **Storage:** a `size_bytes` column on `variables` and a `variable_limits` table. The limits are
  checked in the write buffer's commit.
- **Errors:** a write over the quota fails with `E_QUOTA`, and a value over the cap with
  `E_VALUE_TOO_BIG`.
- **Other limits:** `MAX_LIST_ITEMS` and `MAX_NAMES_PER_SPACE` become admin settings too. A full list
  fails with `E_LIST_FULL` instead of silently dropping its oldest item.
- **Usage:** `!var usage [ns]` shows how much of the quota is used.

### An HTTP primitive, later

`weather` and similar commands wait for a gated HTTP query primitive. It would be off by default, with
per-channel domain allow-lists, GET only, timeouts, size caps and SSRF protection. It gets its own ADR
before any code.

## Options Considered

### Option A: keep quotes and readouts as primitives
**Pros:** nothing to migrate. **Cons:** every new readout is Python. A channel can't reword or copy a
command it doesn't own the code of.

### Option B: derive everything the language can express (chosen)
**Pros:** the Python surface shrinks to what needs Python. Packs and toggles apply uniformly.
**Cons:** depends on ADR-0018 landing first, and a deploy needs the pack script run before the bot
starts.

### Option C: install bot-owned packs at startup
**Pros:** nothing to remember. **Cons:** two sources of truth at boot, which ADR-0012 rejected.
Rejected again on 2026-09-28 in favour of the startup check.

## Consequences

- **Easier:** new default commands are definitions, not code. Channels can reword readouts.
- **Harder:** upgrading the bot can mean running the pack script first. The startup check makes a
  forgotten run a clear error at boot, not broken sentinels in chat.
- **Order:** quotes can move only after ADR-0018 items 3 and 5 and the internal-member support here.

## Action Items

1. [ ] The link rule in `Outbox.send`, the `bot_badges` cache, and the web site link in `help`.
2. [ ] `random` with a seed, and picking from a list or map.
3. [ ] `shoutout` without a chat line, and the moderation primitives whose endpoints check out.
4. [ ] Quotas and per-value caps: `variable_limits`, `size_bytes`, `!admin quota|valuecap`, the JSON
   endpoint, and `!var usage`.
5. [ ] Internal pack members and system packs, with `false` and `default` moved into `core`
   (amends ADR-0012).
6. [ ] The pack script installs `core`, and startup refuses to run without it. Also `ping`, the new
   starter readouts, and `$channel.next_stream`.
7. [ ] `listen`, `event` and `timer` in an `automation` module, with `!trigger` as an alias.
8. [ ] `customecho` and the `:template` accessor.
9. [ ] Quotes as a derived pack, the data migration, and the removal of the table.
10. [ ] An ADR for the HTTP query primitive. Drafted as ADR-0020 (proposed); ticked when it is accepted.
11. [ ] The reserved-name list in ADR-0010 and the access matrix, with a test against
    `runtime/namespaces.py`.
