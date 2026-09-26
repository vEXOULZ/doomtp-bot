# Roadmap and progress

**As of 2026-09-25** (the architecture's own promises are tracked beside the ADRs', and all closed). Every decision in this project carries its own action items, so this page is the
sum of them: what each ADR set out to do, how much of it is done, and what is left. It is written by
hand — when an item closes, tick it in its ADR and update the row here in the same commit.

[architecture.md](architecture.md) makes promises of its own that no ADR carries as an action item.
Until 2026-09-23 nothing counted them, so the score below could reach 79 of 79 with parts of the design
unbuilt. They are listed under [Promised in the architecture](#promised-in-the-architecture-not-yet-built)
as `ARCH-N`, and close the same way: build it, or change the architecture so it no longer promises it.

## Where the plan stands

| ADR | Decision | Items | State |
|-----|----------|-------|-------|
| [0001](adr/0001-chat-transport-eventsub-websocket.md) | EventSub WebSocket in, Helix out | 6/6 | Complete |
| [0002](adr/0002-twitch-library-twitchio.md) | TwitchIO 3.x behind an adapter | 4/4 | Complete |
| [0003](adr/0003-storage-sqlite.md) | SQLite: `bot.db` and `chatlog.db` | 5/5 | Superseded by 0014 |
| [0004](adr/0004-modular-monolith.md) | One async process, one container | 4/4 | Complete |
| [0005](adr/0005-command-pipeline-runtime.md) | Parse → resolve → preflight → execute | 6/6 | Complete |
| [0006](adr/0006-permissions-cooldowns-toggles.md) | Ranked roles, two cooldowns, layered toggles | 5/5 | Complete |
| [0007](adr/0007-channel-access-tiers.md) | Basic, moderator and full channel tiers | 5/5 | Complete |
| [0008](adr/0008-history-backfill-recent-messages.md) | Fill log gaps from recent-messages | 5/6 | One blocked |
| [0009](adr/0009-user-custom-commands-sharing.md) | User-owned commands: link, publish, version | 6/6 | Complete |
| [0010](adr/0010-variables-scopes.md) | Seven namespaces, exact-name write grants | 4/4 | Complete |
| [0011](adr/0011-parser-and-web-editor.md) | One server-side PEG parser, a local highlighter | 6/6 | Complete |
| [0012](adr/0012-derived-commands-and-packs.md) | Derived commands are global publications | 6/6 | Complete |
| [0013](adr/0013-deploy-by-pulling-a-published-image.md) | CI publishes, the server pulls | 4/6 | Two need the server |
| [0014](adr/0014-storage-postgres-one-database-two-schemas.md) | Postgres: one database, two schemas | 9/10 | One needs the server |
| [0015](adr/0015-metrics-prometheus-text-on-the-api.md) | Counters in Prometheus text on `/metrics` | 4/4 | Complete |
| [0016](adr/0016-web-ui-as-a-separate-site-over-the-json-api.md) | The web UI moves to a separate site over the JSON API | 2/5 | In progress |
| — | [Architecture promises](#promised-in-the-architecture-not-yet-built) (`ARCH-1`…`ARCH-9`) | 9/9 | Complete: six built, three taken out |

**81 of 88 ADR action items are closed.** Four of the seven open ones are waiting on a person or a server,
not on code; the other three are ADR-0016's, and they are the new web site's work. **All 9 architecture promises are closed**: six built,
and three (`storage/repos/`, the `weather` module, a pluggable `Authenticator`) taken out of the
architecture with the reason written where the promise was.

## What is left, and why

### Waiting on the server — ADR-0013 items 5 and 6, ADR-0014 item 10

The publish half is now real. The repository is at
[github.com/vEXOULZ/doomtp-bot](https://github.com/vEXOULZ/doomtp-bot), and a green run pushes `:main`
and `:<sha>` to GHCR; both tags resolve to one digest, and the published image pulls, migrates and serves
against Postgres 17. It took five runs to get there, because CI had never executed on this project at all
— the first four found a `pg_dump` too old for the server, then a missing apt repo, then a PATH that
preferred the old client anyway, then a compose check reading a gitignored `.env`. Worth recording: none
of those were in the application, and all four were invisible until something other than a dev box ran
the suite.

The pull half is still tested only against a local registry standing in for GHCR: a directory holding
only the compose files pulls the image rather than building it, `deploy/update.sh` does nothing when the
tag hasn't moved and restarts cleanly when it has, and the packaged tools run from the image. What has
never run is the real thing:
- **Raise the guest's shutdown timeouts** past the 45 s stop grace period, and install
  `qemu-guest-agent`. Until that is done, a host reboot can kill the bot mid-flush and the next startup
  records an unclean shutdown — a wider gap in the chat log than the deploy needed to cost.
- **Prove a restore on the guest.** The round-trip works locally: the backup service dumps both schemas
  from a password-protected server, and dropping the `bot` schema and running `pg_restore` brings it
  back whole. What has not happened is the same thing on the guest's own volume, on its own cron, with a
  copy then leaving the machine — and a backup nobody has carried off the box is half a backup.

### The new web site — ADR-0016 items 3 to 5

The JSON the pages need is in the API: session login, API keys, and the reads the Jinja pages used to
take straight from the app. The pages themselves are being rebuilt in `dtp-web`, a separate repository
on the design the other vexoulz sites share. When it covers every page, the bot stops serving its own
(item 4) and §11 is rewritten to match (item 5). Until then both work, with one login.

### Waiting on a reply — ADR-0008 item 4

Backfill reads from `recent-messages.robotty.de`, a service someone else runs and pays for. The bot
already asks each channel before using it (`!backfill`), records what it filled, and keeps its requests
within the documented reach. **Contacting the maintainer about the bot integration and the keep-warm
interval is still open, and should happen before backfill is enabled for a real channel.** This is a
courtesy item, not a technical one — which is exactly the sort that quietly never gets done.

### Promised in the architecture, not yet built

*All nine closed on 2026-09-23. The table stays as the record of what was promised and what became of it.*

Found on 2026-09-23 by reading [architecture.md](architecture.md) against `src/`. Each one is written
there as part of the design, not as "later", and none is an ADR action item. Closing one means building
it or rewriting the architecture so it stops promising it — either is fine, silence is not. Items that
choose a dependency or a new outside service (`ARCH-1`, and the weather source in `ARCH-6`) need an ADR
first.

| Item | Promise | Where it stood | State |
|------|---------|----------------|-------|
| `ARCH-1` | **Metrics** (§13): nine named counters — `messages_logged_total{source}`, `runs_total{code}`, `outbox_dropped_total{reason}`, `eventsub_reconnects_total` and the rest | None is exported. The outbox keeps a `dropped` count in memory and `/readyz` reports queue depth, but nothing can be scraped or graphed. Needs an ADR for the format and where it is served (the API is LAN-only). | **Closed** 2026-09-23 — [ADR-0015](adr/0015-metrics-prometheus-text-on-the-api.md): hand-written Prometheus text on `/metrics`, each counter incremented at its source; EventSub is counted as welcomes plus client restarts, since TwitchIO doesn't report reconnects. |
| `ARCH-2` | **Leave when banned** (§10 etiquette): auto-leave and flag a channel when Twitch answers a send with 403 | Not built. `core/outbox.py` logs `outbox.send_failed` and drops the message; the bot stays joined and keeps trying. The only 401/403 handling is for a revoked broadcaster token (`twitch/client.py`). | **Closed** 2026-09-23 — a 403 on a send parts the channel as `system` with `status='banned'`; admin page and API show it, and only an explicit rejoin brings the bot back. |
| `ARCH-3` | **`!explain` report page** (§4.4): the chat summary links to a full report in the web UI | Chat and `POST /api/v1/explain` exist; there is no page, and no link. | **Closed** 2026-09-23 — public `/explain/<token>` (in memory, one hour) linked from chat when `PUBLIC_WEB_UI` is on; checking as another user (`as_user`) is admin-only, on `/admin/explain` and the API. |
| `ARCH-4` | **Side-effect built-ins** (§4.3): `!timeout`, `!shoutout` and Helix writes run at their stage and check the moderation index right before acting | None exists, so the checkpoint has nothing to guard. `side_effects=True` is declared in `runtime/spec.py` and read by nobody. The starter pack's `so` is a custom command that only talks. | **Closed** 2026-09-23 — `!timeout` and `!shoutout` in a new `moderation` module, moderator role and `moderate` capability; the runtime checks the moderation index right before a `side_effects` command and `!explain --run` never runs one; each handler checks again before its Helix call. |
| `ARCH-5` | **Enforced `reads`/`writes`** (§4.2) | Declarations only, as the architecture says — until the first built-in other than `!var` writes. Closes with `ARCH-4` or `ARCH-6`, whichever writes first. | **Closed** 2026-09-23 — `ctx.variables` lets a built-in touch only the keys its spec declares (126 otherwise); `!var` declares `*`, and tests keep it the only one and keep handlers off `ctx.exec.variables`. |
| `ARCH-6` | **Modules `weather`, `quotes`, `logsearch`** (§12, marked `·`) | Not built. `weather` is the spec's running example (§4.2) and needs an outside API, so an ADR; `quotes` and `logsearch` need only the database. | **Closed** 2026-09-23 — `quotes` (numbered per channel, never renumbered, moderators add and delete, audited, filtered) and `logsearch` (moderator; the chat-safe search in `chatlog/queries.py`) built. `weather` dropped from §12: no outside service without an ADR, and nobody asked; it stays the §4.2 spec example. |
| `ARCH-7` | **`chatlog/queries.py` and `storage/repos/`** (§12, marked `·`) | Not on disk. Message search is written inside `api/routes/data.py`. Build them when `logsearch` needs the same query, or take them off the layout. | **Closed** 2026-09-23 — `chatlog/queries.py` built: the API's search moved there, with a chat-safe mode that leaves out deleted, cleared, bot and command messages. `storage/repos/` taken off the layout: SQL stays with the service that owns each table. |
| `ARCH-8` | **A pluggable `Authenticator`** (§11), so Twitch login for a per-user dashboard can come later without rework | Not there: `webui/auth.py` is the admin password and `api/keys.py` the keys, each checked where it is used. | **Closed** 2026-09-23 — taken out of §11: a per-user Twitch login is a new kind of caller, not a third way to be the admin, so an interface written before it is designed would guess. Key and session already meet in one `_authenticate`; §11 says where a login would go. |
| `ARCH-9` | **Docs that match the code** | §7 says listeners use `re` with an RE2 check through `google-re2`; the code uses the `regex` module with a match timeout (`patterns.py`) and no RE2. The architecture's header still reads *Status: Proposed*. Both are edits to the doc, not the code. | **Closed** 2026-09-23 — §7 now describes `regex` with a 50 ms match timeout, and why not RE2; header reads *Accepted, built*, revision 5. |

## Deferred by design

These are decided, not forgotten. See the [language proposal §6](command-language-proposal.md) for the
full table.

Runtime cooldown failures were on this list until 2026-09-22. They are now the language spec's version
1.1: a command on cooldown fails with code 128 when evaluation reaches it, so `||` routes around it and a
branch that never runs is never held to a cooldown (ADR-0006 item 5).

| Item | Status |
|------|--------|
| `?` suffix as shorthand for `\|\| true` | Deferred until real usage shows how often it is typed |
| `;` sequence operator | Reserved, only if a "send every message" mode is ever wanted |
| Keyword operator aliases (`and`, `or`) | Rejected |
| Private variables and read grants | Future consideration ([matrix §7](variable-access-matrix.md#7-future-considerations)) |

## What is built

Everything in [architecture.md §12](architecture.md#12-package-layout) marked ✔ — which is the whole of
the layout except the `·` entries, whose gaps are `ARCH-6` and `ARCH-7` above: the command language and its runtime, permissions, cooldowns and toggles, variables with grants,
the chat log with gap detection and backfill, custom commands with versions, links, publications and
packs, derived commands, triggers and timers, the badword filter, moderation-aware replies, the REST API
and web UI with the CodeMirror editor, OAuth for the bot and for broadcasters, capability tiers, backups,
and the deploy path.

The test suite is 563 pytest cases plus 36 vitest ones, with the parser corpus shared between them, the
EventSub adapter pinned to payloads recorded from Twitch's own simulator, and the railroad diagrams
checked against the grammar. The pytest half runs against a real Postgres rather than a stand-in — start
one with `docker compose --profile test up -d postgres-test`, which is what CI does too.

Storage moved from SQLite to Postgres on 2026-09-22 ([ADR-0014](adr/0014-storage-postgres-one-database-two-schemas.md)),
while there was still no production data to migrate. One database, a `bot` schema and a `chatlog` schema,
psycopg in place of aiosqlite, and full-text search on a `tsvector` column instead of FTS5.

## Next, in the order it makes sense

1. **Stand up the Proxmox guest** and run through the deploy tutorial in the README, closing ADR-0013
   items 5 and 6 and ADR-0014 item 10 on the way. Everything upstream of the guest is now proven.
2. **Write to the recent-messages maintainer** (ADR-0008 item 4), then turn backfill on for one channel
   and read what `scripts/coverage.py` says the next morning.
3. **Sign the bot in again** once it runs there: `!shoutout` needs `moderator:manage:shoutouts`, which a
   token from before 2026-09-23 doesn't carry (the chat line works without it; the card doesn't).
4. **Run the bot in its own channel for a week** before inviting anyone else, and read `/metrics`
   afterwards. Every remaining unknown in this project is about what real chat does to it, not about
   what the code does.
