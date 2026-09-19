# doomtp-bot — Architecture

**Status:** Proposed · **Date:** 2026-09-16 · **Revision:** 3

A multi-channel Twitch chat bot written in Python, self-hosted on a homelab in a container. The main features are a composable command language, user-published custom commands, a complete chat log, and a REST API with a web UI.

| Doc | Topic |
|-----|-------|
| [command-language-spec.md](command-language-spec.md) | **Formal command language spec v1.0** (lexing, grammar, AST, preflight, evaluation, placeholders, sentinels, conformance tests) |
| [command-language-proposal.md](command-language-proposal.md) | Superseded syntax proposal. Kept for rationale and the deferred and test items. |
| [namespaces.md](namespaces.md) | **Canonical registry** of placeholder namespaces, argument types and reserved names |
| [variable-access-matrix.md](variable-access-matrix.md) | Read/write access per namespace and actor, grant types, admin actions (reviewed) |
| [ADR-0001](adr/0001-chat-transport-eventsub-websocket.md) | Read chat through EventSub over WebSocket and send through the Helix API |
| [ADR-0002](adr/0002-twitch-library-twitchio.md) | Use TwitchIO 3.x behind an adapter |
| [ADR-0003](adr/0003-storage-sqlite.md) | SQLite with two files: `bot.db` (state) and `chatlog.db` (logs) |
| [ADR-0004](adr/0004-modular-monolith.md) | One async process, one container |
| [ADR-0005](adr/0005-command-pipeline-runtime.md) | Command runtime: an AST, preflight checks, and a three-part `Result` |
| [ADR-0006](adr/0006-permissions-cooldowns-toggles.md) | Ranked roles, per-tier and per-user cooldowns, layered toggles |
| [ADR-0007](adr/0007-channel-access-tiers.md) | Join channels in basic, moderator and full tiers, with capability detection |
| [ADR-0008](adr/0008-history-backfill-recent-messages.md) | Fill chat log gaps from recent-messages.robotty.de |
| [ADR-0009](adr/0009-user-custom-commands-sharing.md) | User-owned custom commands: link, publish, edit, versions |
| [ADR-0010](adr/0010-variables-scopes.md) | Variables: seven namespaces, exact-name write grants, all public for now |
| [ADR-0011](adr/0011-parser-and-web-editor.md) | One authoritative server-side PEG parser. The web editor highlights locally and gets diagnostics from the API. |

---

## 1. Requirements

### Functional

| # | Requirement |
|---|-------------|
| F1 | **Log every chat message** into a queryable database. **Never delete log rows.** Deletions, timeouts, bans and clears are recorded as events and flagged on the affected messages. |
| F2 | **Fill log gaps** caused by crashes, updates or disconnects from a third-party history service (recent-messages) |
| F3 | **Command language** with pipes and chain operators (`\|`, `&&`, `\|\|`, grouping, `>`/`>>` variable writes) and sentinel commands (`true`, `false`, `default`, `fail`). A pipe stops on failure, and `\|\|` handles failures. Details are in the language proposal. |
| F4 | **Commands return three things:** an exit code (0 = success), a formatted message (shown when it's the final result) and structured data (usable by later commands, e.g. `{1.celsius}`) |
| F5 | **Permission tiers:** Twitch built-ins (broadcaster, lead mod, mod, VIP, sub), custom roles (e.g. ambassador) at any rank including above moderator, and **global bot owners and bot admins** above everything |
| F6 | **Cooldowns:** a global cooldown per tier **and** a per-user cooldown. **Both must have expired** for the command to run. Rejections are silent, with an optional **callback** that can customize the response. |
| F7 | **Toggles** for modules and individual commands, **globally and per channel** |
| F8 | **Per-channel command prefix**, defaulting to `🏜`. An emoji prefix may be followed by a space (`🏜 ping`); an ASCII one may not (spec §2.1). |
| F9 | **Command self-documentation:** every command defines a description, parameters, data outputs and usage examples. Chat `!help` lists only the commands *that user* can run. The REST API lists **all** commands for a docs web page. |
| F10 | **`!explain <expr>`:** dry run that shows the parse tree, name resolution, and the result of every policy check |
| F11 | **Custom commands:** users build pipelines, save them under their own alias, **publish** them to channels where they have permission, **link** other users' commands under their own alias, and **republish** them. **Edits propagate instantly.** Linking and publishing replies warn about this. Parameters are documented as `arg.N`. |
| F12 | **Variables:** `{chatter.x}` (global per user), `{channel.x}`, `{channel.chatter.x}`, `{publisher.x}`, `{publisher.chatter.x}`, `{publisher.channel.x}`, `{publisher.channel.chatter.x}`. Namespaces are kept in a canonical registry, and access rules and grants in a review matrix. |
| F13 | **Triggers:** channel point redemptions, raids, subs, other events, **timers**, and regex **listeners** can each run a pipeline |
| F14 | **Moderation-aware replies:** the bot doesn't reply if the triggering message was deleted, or its author was timed out or banned, while the command was processing |
| F15 | **Badword filter:** global and per-channel lists. A matched word can be censored (`****`), replaced (`flowers`), tagged (`[slur]`) or cause the message to be blocked. |
| F16 | **Ignore list** for other bots and specific users, global and per channel |
| F17 | **Logs:** an audit log of every configuration change, and a command usage log with a **log level set per command** |
| F18 | **Channel onboarding:** a full tier where the broadcaster authorizes the bot, and a **basic tier that needs no broadcaster action** |
| F19 | **REST API** (health checks now, `/api/v1` later) plus a **web admin UI** and a **public web UI** |

### Out of scope (for now)

- A personal Twitch dashboard for each user with Twitch login on the website. It's a future feature: the API and auth layers shouldn't block it, and it isn't designed here.

### Non-functional

| Concern | Target |
|---------|--------|
| Scale | 1–20 channels. About 50 messages/s peak, with a much lower average. |
| Latency | Replies go out within 1 s, not counting rate-limit waits or the optional moderation hold. Logging never blocks commands. |
| Log durability | Every message received is stored, except messages still in the batch window (≤1 s) at the moment of a crash. Gaps are recorded explicitly and filled from recent-messages where possible. |
| Safety | All chat input is untrusted. Pipelines have bounded time, size and depth. Published commands run with the **invoker's** permissions. |
| Footprint | Under 250 MB RAM |
| Network | Outbound-only connections to Twitch. The API and web UI are LAN-only until a reverse proxy and authentication are added. |

---

## 2. High-level design

```
      Twitch EventSub WS            Twitch Helix               recent-messages.robotty.de
             │ events                  ▲ send/mod/lookup             ▲ backfill (HTTP)
─────────────┼─────────────────────────┼─────────────────────────────┼──────── container
             ▼                         │                             │
   ┌──────────────────┐        ┌───────┴────────┐          ┌─────────┴────────┐
   │ twitch/ adapter  │        │ twitch/ helix  │          │ history/ provider│
   │ dedupe, map      │        └───────▲────────┘          └─────────┬────────┘
   └────────┬─────────┘                │                             │ historical events
            ▼                          │                             ▼
   ┌────────────────────────── Dispatcher (core/dispatch.py) ─────────────────────┐
   │  calls each step in order for every event; one failing step is logged, not fatal │
   └──┬───────────────┬────────────────────┬───────────────────┬───────────────┬──┘
      ▼               ▼                    ▼                   ▼               ▼
┌───────────┐ ┌───────────────┐ ┌───────────────────┐ ┌──────────────┐ ┌─────────────┐
│ ChatLogger│ │ Moderation    │ │ Dispatch          │ │ Triggers     │ │ Ignore list │
│ (batch)   │ │ Index         │ │ prefix / listener │ │ events,timers│ │ (gate)      │
└─────┬─────┘ │ deleted msgs, │ └─────────┬─────────┘ └──────┬───────┘ └─────────────┘
      │       │ user clears,  │           │ text              │ pipeline
      │       │ chat clears   │           ▼                   ▼
      │       └──────┬────────┘ ┌──────────────────────────────────────────────┐
      │              │ checks   │ Command Runtime                              │
      │              ├─────────►│ Parser → Resolver → Preflight → Executor     │
      │              │          │   AST    builtins/   toggles     stages,     │
      │              │          │          published/  roles       operators,  │
      │              │          │          personal    cooldowns   vars (buffered)
      │              │          └───────────────┬──────────────────────────────┘
      │              │                          │ Result(code, message, data)
      │              │                          ▼
      │              │          ┌──────────────────────────────┐
      │              └─────────►│ Outbox: moderation recheck → │──► Helix send
      │                         │ badword filter → chunk →     │
      │                         │ per-channel token bucket     │
      │                         └──────────────────────────────┘
      ▼
┌──────────────────────────────┐   ┌──────────────────────────────────────────────┐
│ chatlog.db                   │   │ bot.db                                       │
│ messages(+FTS) mod_events    │   │ channels roles toggles cooldowns commands    │
│ log_sessions backfill_runs   │   │ custom_commands(+versions,links,publications)│
│ command_runs outbound_msgs   │   │ variables triggers filters ignore audit_log  │
└──────────────────────────────┘   │ oauth_tokens api_keys                        │
                                   └──────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────────────────────────┐
│ api/ FastAPI (same event loop): /healthz /readyz /auth/*  /api/v1/*  /admin  /   │
└──────────────────────────────────────────────────────────────────────────────────┘
```

### Main flow: a chat command

1. The adapter receives `channel.chat.message`, maps it to a `ChatMessage` and dedupes it.
2. The Dispatcher handles it in order:
   - **ChatLogger** queues it for storage. This always happens, including for ignored users.
   - The **Ignore gate** stops processing here if the sender is ignored, is the bot itself, or carries the Twitch chat-bot badge (configurable).
   - **Dispatch** checks whether the message starts with the channel prefix, which makes it a command expression, and runs any regex **listeners** enabled for the channel.
3. The runtime handles the expression (ADR-0005):
   1. **Parse** the text into an AST.
   2. **Resolve** each command name. The order is built-in, then published in the channel, then the user's personal commands.
   3. **Preflight** every command against toggles, roles and both cooldowns. On a denial, the runtime runs the optional callback and otherwise stays silent.
   4. **Execute** the commands, applying operator semantics. Variable writes are buffered.
4. **Moderation checkpoints:**
   - The runtime checks the Moderation Index between stages and cancels if the trigger was invalidated (exit code 130).
   - The **Outbox rechecks immediately before calling Helix**.
   - Buffered variable writes are committed only if the run wasn't cancelled.
5. The final result goes through the Outbox: badword filter, then chunking, then the rate limit, then send. What was actually sent is written to `outbound_msgs`.
6. The run is recorded in `command_runs` according to the command's log level.

---

## 3. Chat log, moderation events, gaps

### 3.1 Log everything, delete nothing

- Messages are **never** deleted or altered in response to moderation. Moderation *adds* rows to `mod_events` and sets flags on the affected messages.
- **Retention defaults to forever.** An admin-only, audited purge tool can be added later if a legal or privacy need comes up. It is not automatic.

| EventSub event (basic tier) | Stored as |
|-----------------------------|-----------|
| `channel.chat.message` | `messages` row |
| `channel.chat.notification` (subs, resubs, gifts, raids, announcements…) | `chat_notifications` row, and also published to Triggers |
| `channel.chat.message_delete` | `mod_events(type='delete', message_id)`; sets `messages.deleted_at` |
| `channel.chat.clear_user_messages` (a timeout or ban happened) | `mod_events(type='user_clear', target_user_id)`; sets `messages.cleared_at` on that user's recent messages |
| `channel.chat.clear` | `mod_events(type='chat_clear')`; sets `messages.cleared_at` for the channel |
| `channel.moderate` / `channel.ban` (mod or full tier only) | Enriches `mod_events` with the moderator, reason, duration and action type |

Deletions are flags, never row removals. Queries and the web UI choose whether to hide flagged messages. The raw text is kept.

### 3.2 Schema (`chatlog.db`)

```sql
messages(
  message_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL,
  user_id TEXT NOT NULL, user_login TEXT NOT NULL, display_name TEXT,
  text TEXT NOT NULL, message_type TEXT, badges TEXT, fragments TEXT,
  bits INTEGER DEFAULT 0, reply_parent_id TEXT, reward_id TEXT, source_channel_id TEXT,
  is_self INTEGER DEFAULT 0, is_command INTEGER DEFAULT 0,
  source TEXT NOT NULL DEFAULT 'eventsub',     -- eventsub | recent-messages
  raw TEXT,                                    -- original IRC line when backfilled
  sent_at INTEGER NOT NULL, received_at INTEGER NOT NULL,
  deleted_at INTEGER, cleared_at INTEGER, mod_event_id INTEGER)
-- indexes: (channel_id, sent_at), (user_id, sent_at); FTS5 external-content table on text

chat_notifications(id TEXT PRIMARY KEY, channel_id TEXT, user_id TEXT, type TEXT,
                   payload TEXT, source TEXT, sent_at INTEGER)
mod_events(id INTEGER PRIMARY KEY, channel_id TEXT, type TEXT, message_id TEXT,
           target_user_id TEXT, moderator_user_id TEXT, duration_s INTEGER, reason TEXT,
           source TEXT, at INTEGER)
users(user_id TEXT PRIMARY KEY, login TEXT, display_name TEXT, first_seen INTEGER, last_seen INTEGER)
user_names(user_id TEXT, login TEXT, display_name TEXT, seen_from INTEGER, PRIMARY KEY (user_id, login))

log_sessions(id INTEGER PRIMARY KEY, channel_id TEXT, started_at INTEGER, ended_at INTEGER,
             end_reason TEXT)                          -- live coverage intervals
backfill_runs(id INTEGER PRIMARY KEY, channel_id TEXT, gap_from INTEGER, gap_to INTEGER,
              fetched INTEGER, inserted INTEGER, complete INTEGER, error TEXT, at INTEGER)

command_runs(id INTEGER PRIMARY KEY, channel_id TEXT, user_id TEXT, trigger_type TEXT,
             trigger_id TEXT, expr TEXT, resolved TEXT, code INTEGER, message TEXT,
             duration_ms INTEGER, cancelled_reason TEXT, run_ref TEXT, at INTEGER)
outbound_msgs(id INTEGER PRIMARY KEY, channel_id TEXT, run_ref TEXT, text_sent TEXT,
              text_prefilter TEXT, filter_hits TEXT, twitch_message_id TEXT,
              dropped_reason TEXT, at INTEGER)
-- run_ref is the runtime's run id: it links every sent or dropped message to the run that produced it
```

All users are keyed by **`user_id`**. Logins are snapshots plus rename history.

### 3.3 Gaps and backfill (ADR-0008)

- `log_sessions` records exactly when the bot was listening to each channel.
- On startup, and after any EventSub reconnect gap longer than 5 s, the **HistoryProvider** fetches `recent-messages/:channel?after=<gap_from - 5s>`.
- It parses the raw IRC lines and inserts them with `source='recent-messages'`. Inserts are idempotent on the message ID.
- It records a `backfill_runs` row. The row is marked `complete=0` if the service hit its 800-message cap or reported `channel_not_joined`.
- **Backfilled events never trigger commands, listeners or triggers.**
- The service only starts collecting a channel after the first request for it, so the bot **keeps each channel warm** with periodic `limit=1` requests.
- Per the service's guidelines, backfill is **opt-in per channel**, chosen at onboarding.

---

## 4. Command runtime (ADR-0005; syntax in the language proposal)

### 4.1 Result model

```python
@dataclass(frozen=True)
class Result:
    code: int = 0                 # 0 ok; non-zero = error (see table)
    message: str | None = None    # human text; sent to chat only if this is the final result
    data: JsonValue = None        # structured; addressable as {N.path} / {_.path}
```

| Code | Meaning (shell-inspired) |
|------|--------------------------|
| 0 | Success |
| 1 | Generic failure. The command decides the message. |
| 2 | Usage error (bad parameters) |
| 3 | Not found or empty result |
| 124 | Timeout |
| 125 | Rate-limited by an upstream API |
| 126 | Permission denied (surfaced in `!explain` and callbacks, never in chat by default) |
| 127 | Unknown or disabled command (silent) |
| 128 | Cooldown (silent, unless a callback is set) |
| 130 | Cancelled by moderation |

### 4.2 Command specification (self-documenting)

```python
@command(
    name="weather", module="weather", aliases=["w"],
    summary="Current weather for a location",
    description="Looks up current conditions. Defaults to your saved {chatter.location}.",
    params=[                                   # positional, documented as arg.N
        Param("1+", name="location", type="str", required=False,
              description="City name; falls back to {chatter.location}"),
    ],
    input=InputMode.OPTIONAL,                # can receive piped data
    data_schema={"celsius": float, "fahrenheit": float, "condition": str, "location": str},
    examples=[                                 # {sign} = the reader's own command sign
        Example("{sign}weather Lisbon", "Lisbon: 21°C, clear"),
        Example('{sign}weather Lisbon | echo "it\'s {1.celsius}C now!"', "it's 21C now!"),
    ],
    required_role="everyone",
    default_cooldowns={"everyone": Cooldown(tier_s=10, user_s=30), "moderator": Cooldown(0, 0)},
    log_level=LogLevel.INVOCATIONS,
    side_effects=False,                       # True → may only commit if run not cancelled
)
async def weather(ctx: Ctx, args: Args, stdin: Result | None) -> Result: ...
```

- **Usage strings, `!help`, the `/api/v1/commands` JSON and the public docs page are all generated** from these specs.
- **No spec hard-codes a command sign.** Every channel picks its own, so summary, description, param and example text writes `{sign}` (`runtime.spec.SIGN`) and whoever shows it substitutes the sign that reader types — the channel's in chat, the default on the docs page. Chat messages built by a handler use `ctx.channel.prefix`, or `sign_of(ctx, channel_id)` when they name another channel. A test walks the registry and fails on a literal `!command` in spec text.
- `reads`, `writes` and `side_effects` are **declarations used for documentation and `!explain`**. They are not enforced yet: today only `!var` writes variables, and it is the documented exception (variable-access-matrix.md §2). Enforcement arrives with the first other built-in that writes.
- Custom commands carry the same metadata (summary, params, examples), written by their owner.
- `!help` filters by the **effective policy** for the caller in that channel. `GET /api/v1/commands` lists everything, including role, cooldown and toggle defaults.

### 4.3 Execution rules

| Rule | Default |
|------|---------|
| Preflight | Every command in the expression is resolved and policy-checked **before anything runs**. Commands on an operator branch that may never run are still checked, which keeps `!explain` predictable. |
| Limits | 8 commands per expression, 3 s per stage, 6 s in total, 4 KB of data per stage, final message up to 2 chat messages, custom command nesting depth 3, cycle detection |
| Operators | `\|` stops on failure. `&&`/`\|\|` branch on the exit code. There is no `;` (reserved). `>`/`>>` store only on success. |
| Output shown | Only the **message of the last executed command**, and only if it isn't empty |
| Arguments | Declared param types and inline `{arg.N:type}` are validated before the body runs. Failure returns code 2 with generated usage text. |
| Re-entrancy | Output is never parsed as a command. The bot ignores its own messages. |
| Variable writes | Buffered per run. They commit atomically at the end if the run was not cancelled, **including when the final code is non-zero** (e.g. `!counter +1 && !fail`). |
| Side-effect commands | Commands such as `!timeout`, `!shoutout` and Helix writes run at the time of their stage. Right before running, they check the moderation index. |

### 4.4 `!explain <expr>`

`!explain` returns a compact summary in chat, plus a link to a full report in the web UI when the UI is enabled. The report shows:

- the AST with operator precedence
- how each name resolved (built-in, published in the channel with its version, or personal link)
- toggles, the required role against the caller's rank, and cooldown status with remaining times
- which branches would run
- the placeholders each command references, and whether they can be satisfied
- the executed result, only if `!explain --run` is used, and even then without committing variable writes or sending anything

### 4.5 Command usage logging (per-command log level)

| Level | What gets written to `command_runs` |
|-------|-------------------------------------|
| `off` | Nothing |
| `errors` | Runs with code ≠ 0, excluding 127 and 128 |
| `output` | Runs that produced a message, had side effects, or errored. **The default for listeners**, e.g. a regex check on every message that rarely answers. |
| `invocations` | Every explicit invocation. **The default for prefixed commands.** |
| `all` | Everything, including cooldown and permission rejections and listener non-matches. For debugging only. |

- The level is set in the spec and overridden per channel with `!cmd log <command> <level>`.
- Audit logging (§5.5) is **always on** and is not affected by these levels.

---

## 5. Policy: roles, cooldowns, toggles, ignore list, audit (ADR-0006)

### 5.1 Roles

| Role | Rank | Source |
|------|------|--------|
| everyone | 0 | implicit |
| subscriber | 20 | badge |
| vip | 60 | badge |
| moderator | 80 | badge |
| lead_moderator | 90 | Badge when shown. Twitch lets lead mods display the plain mod badge, so channels can also assign this role manually. |
| broadcaster | 100 | badge |
| **custom roles** (e.g. ambassador 50, trusted 85, co-streamer 95) | 1–99 | `role_members`, per channel or global |
| **bot_admin** | 1000 | global. Granted by bot owners. |
| **bot_owner** | 10000 | global, from config (user IDs) |

- **Grant rule:** you can create, edit, grant or revoke only roles ranked strictly below your own. The broadcaster manages all custom roles in their channel. Only bot owners manage bot admins.
- Each command passes if `effective_rank ≥ required_role.rank` or if the user holds one of the command's exact `allowed_roles`.

### 5.2 Cooldowns: both must be clear

- **Tier bucket** `(channel, command, tier)`: the tier is the user's highest-ranked role that has a rule for this command. Each tier has its own shared timer, so mods don't block viewers and viewers don't block mods.
- **User bucket** `(channel, command, user_id)`, with its duration taken from the same rule.
- A command runs only if **both** have expired. When it runs, both start.
- **Rejection is silent.** Optional callbacks can respond:
  - `on_cooldown` and `on_denied` can be set per command, per module or per channel.
  - A callback is either a Python hook or a **pipeline** with access to `{cooldown.tier_remaining}`, `{cooldown.user_remaining}`, `{cooldown.tier}` and `{cooldown.command}`.
  - Callbacks are rate-limited to one notice per user per command per 30 s, so a callback can't become a spam vector.

### 5.3 Toggles

Resolution runs in this order, and the first rule that matches decides:

1. Global module kill-switch
2. Global command kill-switch
3. Channel command override
4. Channel module override
5. Global module default
6. The module's code default

- `core_admin` can't be disabled.
- A module also stays unavailable if its **required capabilities** (ADR-0007) are missing in the channel. The reason is shown in `!explain`.

### 5.4 Ignore list

- Entries are `ignore(scope '*' | channel_id, user_id, reason, added_by)`.
- The Twitch chat-bot badge is auto-ignored, and the bot always ignores itself.
- Ignored users' messages are **logged** but never reach commands, listeners, triggers, variable writes or stats counters (configurable).

### 5.5 Audit log (always on, `bot.db`)

- Every configuration change is recorded, whether it came from chat, the API or the web UI. That covers:
  - roles and role memberships
  - toggles, permission rules, cooldown rules and log levels
  - prefix, filters, ignore list, triggers and timers
  - channel variables
  - custom command publish, unpublish, pin, edit and delete
  - channel join and leave
- Each entry records `actor_user_id`, `via`, `action`, `target`, `before` and `after`.

---

## 6. Custom commands and variables (ADR-0009, ADR-0010)

### Custom commands, briefly

- A **command** belongs to its creator. It has a stable ID, a name, metadata and **versions**. Every edit creates a new version.
- The **owner** can use it anywhere through a *personal alias*, as long as the channel has `custom_cmds` enabled.
- A **publication** makes a command available to everyone in a channel. Publishing requires the channel's `publish_min_role` (default: moderator). Channel mods can disable or unpublish it.
- A **link** lets another user add someone's published command to their own personal aliases. They can then **republish** it in channels where they have permission. The original owner stays the owner.
- **Edits and deletes take effect instantly** everywhere. There's no pinning. The link and publish replies carry a **warning** that the owner can change or remove the command at any time. Mods see a change notice after edits.
- **Commands always run with the *invoker's* permissions and cooldowns**, for the custom command itself and for every command inside it. Publishing can't be used to escalate privileges.
- **Name resolution** in a channel: built-in commands, then channel publications, then the caller's personal aliases. `@name` addresses a personal alias directly. `!explain` shows which one won.
- **Limits:** a body may nest custom commands `MAX_CC_DEPTH (3)` deep, cycles are rejected, and the 8-invocation limit counts every command after expansion (spec §5.2).
- **Packs** group a user's commands so they publish and unpublish as one unit, and the pack's name is the module name for `!module` toggles (ADR-0012). A command added to a published pack appears immediately.
- **Derived commands** are custom commands published to the global scope by a bot owner or admin: available in every channel, still overridable by a channel publication, and never able to shadow a Python built-in (a *primitive*).

### Variables, briefly

| Placeholder | Keyed by | Written by own/built-in commands | Written by someone else's custom command |
|-------------|----------|----------------------------------|------------------------------------------|
| `{chatter.x}` | user (global) | ✔ | ✘ (read only) |
| `{channel.x}` | channel | rank ≥ `channel_var_write_role` (default mod), or a built-in declaring it | only with a mod-issued **write grant** on the publication |
| `{channel.chatter.x}` | channel + user | ✔ | only with a write grant |
| `{publisher.x}` | command owner | ✔ | ✔ (its owner's space) |
| `{publisher.chatter.x}` | command owner + user | ✔ | ✔ |
| `{publisher.channel.x}` | command owner + channel | ✔ | ✔ (its owner's space, current channel) |
| `{publisher.channel.chatter.x}` | command owner + channel + user | ✔ | ✔ |

- Nothing is silently redirected: the placeholder name is the key.
- **All variables are public for now.** Access control only restricts writes, and write grants name exact variables (no wildcards). Private variables are a future consideration.
- The full per-actor rules (typed, own, built-in, foreign via link or publication, trigger, callback), grant types and admin actions are in **[variable-access-matrix.md](variable-access-matrix.md)** (reviewed).
- Values are JSON, capped at 2 KB each. Operations are atomic (`set`, `incr`, `append`, `del`, `top`). Writes are buffered per run (§4.3).
- The full list of namespaces, context fields, argument types and reserved names is in **[namespaces.md](namespaces.md)**.

---

## 7. Triggers, timers and listeners

```sql
triggers(id INTEGER PRIMARY KEY, channel_id TEXT, type TEXT,
         -- redemption | raid | sub | resub | gift_sub | cheer | follow | stream_online |
         -- stream_offline | timer | listener
         match TEXT,        -- JSON: {reward_id}, {min_viewers}, {regex}, {min_bits}, …
         schedule TEXT,     -- timers: {"every": "15m", "jitter": "2m", "only_live": true,
                            --          "min_chat_lines": 5}
         expr TEXT,         -- pipeline expression
         run_as_rank INTEGER,   -- capped at the rank of the mod who created it
         enabled INTEGER, log_level TEXT, created_by TEXT)
```

- Inside the pipeline, `{event.*}` exposes the payload: `{event.user.name}`, `{event.viewers}`, `{event.input}` (redemption text), `{event.reward.title}`, `{event.bits}`, `{event.months}` and so on.
- **`chatter` for a trigger is the event's user:** the redeemer, the raider or the subscriber. Timers have no chatter.
- **Listeners** run on every non-ignored, non-command message that matches the regex:
  - They use Python `re` with a timeout guard and patterns limited in length. Catastrophic backtracking is prevented with an RE2-compatible check through the `google-re2` package when it is available.
  - Listener captures are available as `{match.1}` and `{match.name}`.
- Every trigger passes through the same runtime, including preflight, cooldowns (keyed by trigger), the moderation index (for listeners and redemptions) and the Outbox.
- Some trigger types need capabilities. Redemptions need the full tier, and follows need moderator status (ADR-0007).

**Built so far:** `!trigger listen <regex> => <expression>`, `!trigger add <event> <expression>`, `!timer add <every> [jitter=] [only_live] [min_lines=] <expression>`, each with `list`, `rm` and `on`/`off`. Listeners and the notification events the basic tier receives (raid, sub, resub, gift sub) run end to end; the other event types are stored with a warning that the bot can't receive them yet. Timers tick every 5s against a per-channel line counter; `only_live` waits on the stream poller (ADR-0007). Expressions are parsed and filtered before they are stored, and run at the rank of the moderator who created them — never above it.

---

## 8. Moderation-aware replies

The **ModerationIndex** is in memory and fed by moderation events:

- deleted message IDs, kept 10 minutes
- per-user clear timestamps, per channel
- per-channel clear timestamps

**Checkpoints** for a run triggered by message M from user U at time T:

1. Before execution.
2. Between stages.
3. **In the Outbox, immediately before the Helix send call.**

At each checkpoint, the run is invalidated if any of these is true:

- M was deleted.
- U has a clear at a time ≥ T (a timeout or ban).
- The channel was cleared at a time ≥ T.

When a run is invalidated:

- The runtime task is cancelled and gets code 130.
- Buffered variable writes are discarded.
- Queued outbox messages for the run are dropped with `dropped_reason='moderated'`.
- `command_runs` records `cancelled_reason`.

A **race window** remains: a mod can act after the message has already been sent. Per channel you can set **`reply_hold_ms`** (default 0, suggested 300–800 for strict channels). It delays sending by that amount so late moderation events can still cancel the reply.

---

## 9. Badword filter

- **Lists:** `filters(scope, id, pattern, kind: word|wildcard|regex, category, action, replacement, enabled)`.
  - `action` is one of `mask` (`****`), `replace` (`flowers`), `tag` (`[slur]`) or `block` (don't send the message).
  - Allow-list entries prevent false positives, the "Scunthorpe problem" where a banned string appears inside an innocent word.
- **Normalization before matching:** NFKC, casefold, removal of zero-width and diacritic characters, a map of confusable and leetspeak characters, and collapsing of repeated letters. Matching uses word boundaries. Each channel's list compiles into one combined regex, or Aho-Corasick if lists grow large.
- **Where the filter applies:**
  1. **All bot output**, in the Outbox, after placeholder substitution. This is mandatory.
  2. **Content users store:** custom command names, bodies and metadata, variable values and trigger text. It rejects or censors the content at save time, per channel policy.
  3. Optionally, **incoming chat** through an `automod` module (delete or timeout). This requires the moderator tier and is off by default.
- **Logs keep original incoming text.** `outbound_msgs` stores both the pre-filter and the sent text, plus which filter entries matched.

---

## 10. Channels and onboarding (ADR-0007)

| Tier | How the channel gets it | What works |
|------|-------------------------|------------|
| **basic** | A bot owner or admin runs `!join <channel>`, or the broadcaster types `!join` in the bot's own channel. **No broadcaster OAuth.** | Chat, deletes, clears and chat notifications (subs, resubs, gifts, raids, announcements), reading and sending chat, commands, the log, variables and custom commands. Stream online/offline comes from Helix polling (see ADR-0007). |
| **moderator** | The broadcaster mods the bot | Everything in basic, plus timeouts, bans and deletes by the bot, higher send limits, follows, `channel.moderate` details (who, why) and the `automod` module |
| **full** | The broadcaster completes OAuth at `/auth/connect` | Everything in moderator, plus channel point redemptions, subscription and cheer event details, the chat bot badge (`channel:bot`) and other broadcaster-scoped features |

- The **CapabilityProbe** runs at join, hourly, and whenever a 401 or 403 comes back. It updates `channels.capabilities`.
- Modules and triggers declare what they `require`. Unmet requirements disable a feature with a visible reason instead of an error.
- **Etiquette:**
  - Join only when a broadcaster, mod or bot owner asks.
  - Leave with `!part`.
  - Auto-leave and flag the channel if the bot gets a 403 (banned).
  - Never send unsolicited messages in basic-tier channels. Timers and alerts there require an explicit opt-in by a mod.
- **Per-channel settings:** `prefix`, `reply_hold_ms`, `publish_min_role`, `channel_var_write_role`, `history_backfill` (opt-in), `log_enabled`, `quiet_errors` and the callback defaults.
- **Prefix validation:** a prefix can't start with `/` or `.`, because Twitch clients treat those as chat commands. Its length is 1–3 characters and it can't contain whitespace.

---

## 11. REST API and web UI

- The same **FastAPI** app runs in the bot's event loop and is LAN-only by default.

| Path | When | Notes |
|------|------|-------|
| `GET /healthz`, `GET /readyz` | Now | Liveness, and readiness with component detail: EventSub, tokens, both databases, log queue depth, backfill status, channels |
| `/auth/*` | Now | Bot OAuth setup and broadcaster full-tier connect |
| `GET /api/v1/commands` | Early | **All** commands with their full specs. Feeds the public docs page. |
| `GET /api/v1/channels/{login}/commands` | Early | The effective command list for a channel, including enabled state, roles, cooldowns and publications |
| `POST /api/v1/parse` | Early | Tokens, AST, errors and warnings from the authoritative parser (ADR-0011). Powers editor diagnostics. |
| `POST /api/v1/explain` | Early | Same output as `!explain`, with an optional `as_user` for admins |
| `GET /api/v1/language` | Early | Syntax version, operators, namespace roots per context, types, raw-tail commands, limits. Powers autocomplete and hover docs. |
| `/api/v1/...` channels, roles, toggles, cooldowns, filters, triggers, variables, custom commands, messages search, audit log, command runs | Later | API key or session auth. All writes go through the same services and the audit log. |
| `/admin/*` | **Now** | **Admin UI.** Local admin password (scrypt from the standard library, not argon2 — one less native dependency), sessions in memory, CSRF token per form. Disabled entirely when no password is set. |
| `/` | **Now** | **Public UI.** Feature documentation, the generated command reference, the language reference and per-channel pages. The command reference and the channel pages share one compact table: a line per command, a `<details>` pane for arguments, cooldowns and examples, and a search box that filters client-side over a precomputed `data-search` string (so it needs no request per keystroke, and the page still lists everything without JavaScript). |

**UI technology:**
- **Pages** are server-rendered **Jinja2** inside the same FastAPI app. That's the smallest option for a single Python maintainer: no second container, and no build for pages. *(Built with plain forms so far: HTMX would be a CDN dependency or a vendored file, and nothing yet needs partial updates. Add it when a page does.)*
- **The expression editor** is the one exception (ADR-0011). It's a **CodeMirror 6** component with a small **Lezer** grammar for local highlighting. It gets diagnostics from `/api/v1/parse`, autocomplete from `/api/v1/language` and previews from `/api/v1/explain`.
  - It ships as a single static JS bundle, built in CI (esbuild) and served from `/static`.
  - It's embedded in the HTMX pages as a web component, so only this component needs Node tooling.
- **Public docs pages** include railroad diagrams generated at build time from `docs/grammar/railroad.ebnf` (spec Appendix D).
- If the UI ever needs rich client-side state beyond this, a SPA generated from the OpenAPI schema can replace the pages without API changes.

*Future (not designed): Twitch OAuth login for a per-user dashboard. The auth layer is written as a pluggable `Authenticator` so this can be added later.*

---

## 12. Package layout

Built (✔) and planned (·):

```
src/doomtp_bot/
├─ __main__.py  config.py  clock.py                                        ✔ wiring, settings, now_ms
├─ core/        events.py dispatch.py channels.py outbox.py health.py      ✔ dispatch calls each step directly
│               instance_lock.py                                           ·  capabilities.py (ADR-0007 probe)
├─ twitch/      client.py mapping.py auth.py tokens.py    # only place importing twitchio  ✔
│                                                                          ·  probe.py, streams poller
├─ history/     provider.py recent_messages.py irc_parse.py                ·  ADR-0008
├─ chatlog/     writer.py                                                  ✔  ·  queries.py
├─ moderation/  index.py                                                   ✔
├─ lang/        parser.py (PEG, spec App. C) ast.py errors.py              ✔ syntax (versioned)
├─ runtime/     engine.py resolver.py preflight.py executor.py result.py   ✔
│               context.py values.py variables.py namespaces.py output.py policy.py spec.py registry.py
│                                                                          ·  explain.py
├─ policy/      service.py repository.py snapshot.py roles.py cooldowns.py ✔ one service, not a file per concern
├─ customcmds/  service.py resolution.py versions.py                       ·  ADR-0009
├─ variables/   store.py access.py                                         ✔
├─ triggers/    service.py timers.py listeners.py                          ·  architecture §7
├─ filters/     normalize.py matcher.py service.py                         ·  architecture §9
├─ audit/       log.py                                                     ✔
├─ storage/     db.py migrations/bot/ migrations/chatlog/                  ✔  ·  repos/
├─ modules/     core.py core_admin.py channels.py help.py basic.py         ✔ built-in command groups
│               variables.py _common.py                                    ·  weather, quotes, logsearch, automod…
└─ api/         app.py routes/ (health auth)                               ✔  ·  commands parse explain language v1 web

web-editor/                  # the only Node-tooled part: CodeMirror 6 + Lezer highlight grammar → static bundle
tests/lang/corpus.yaml       # spec Appendix A, shared by pytest (parser) and vitest (highlighter)
docs/grammar/railroad.ebnf   # spec Appendix D, CI-checked copy for railroad diagrams
```

---

## 13. Deployment and operations

The deployment setup is unchanged from revision 2, apart from the notes below.

- **Docker Compose:**
  - `doomtp-bot`: non-root, read-only root filesystem, `/data` volume, LAN-bound port.
  - `datasette`: optional, read-only on `chatlog.db`.
- **Self-hosted history, optional:** for independence from the public recent-messages service, run a `recent-messages2` container on a separate compose stack. It needs TimescaleDB. Don't restart it together with the bot during updates. Point `HISTORY_PROVIDER_URL` at it.
- **Updates:** before stopping, the bot writes `log_sessions.end_reason='update'`. On start, it backfills the gap.
- **Backups:** run nightly `sqlite3 .backup` for both database files. `bot.db` is critical because it holds custom commands, variables and roles.
- **Metrics:**
  - `messages_logged_total{source}`
  - `backfill_inserted_total`
  - `backfill_incomplete_total`
  - `runs_total{code}`
  - `runs_cancelled_total{reason}`
  - `cooldown_rejections_total{tier}`
  - `filter_hits_total{action}`
  - `outbox_dropped_total{reason}`
  - `eventsub_reconnects_total`

---

## 14. Open questions

Resolved in review round 1:
- `{N}` refers to results and `{arg.N}` to arguments.
- `;` is dropped, and only the final message is sent.
- A pipe stops on failure, with sentinel commands for handling it.
- `chatter` is global, with added `channel.chatter` and `publisher.chatter` namespaces.
- Edits propagate instantly, with warnings.
- Admin means global bot owners and bot admins.

Resolved in review round 2:
- `>` stores only on success. Fallbacks are written `( cmd || default 123 ) > x`.
- No keyword aliases.
- `{arg.N+}` strips quotes (a test item).
- Write grants stay.
- `publisher.channel` and `publisher.channel.chatter` were added.

Deferred or to test: see [language proposal §6](command-language-proposal.md).

Resolved in review round 3 (variable access matrix):
- Linked commands can use `publisher.channel.*` in any channel.
- Triggers can't write `chatter.*` or use `publisher.*`.
- No wildcard grants and no read grants. All variables are public.
- Mods can reset `publisher.channel.*`.
- Publishing requires moderator.

Future consideration: private variables and read grants ([matrix §7](variable-access-matrix.md#7-future-considerations)).

Command language spec Appendix B: all six items resolved. Runtime cooldown failures are planned for v1.x.
