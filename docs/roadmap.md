# Roadmap and progress

**As of 2026-09-22** (third revision of the day: storage moved to Postgres, the repository got a remote, and cooldowns moved to runtime). Every decision in this project carries its own action items, so this page is the
sum of them: what each ADR set out to do, how much of it is done, and what is left. It is written by
hand — when an item closes, tick it in its ADR and update the row here in the same commit.

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

**75 of 79 action items are closed.** The four that aren't are below, and none of them is waiting on
code — they are waiting on a person or a server.

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

### Waiting on a reply — ADR-0008 item 4

Backfill reads from `recent-messages.robotty.de`, a service someone else runs and pays for. The bot
already asks each channel before using it (`!backfill`), records what it filled, and keeps its requests
within the documented reach. **Contacting the maintainer about the bot integration and the keep-warm
interval is still open, and should happen before backfill is enabled for a real channel.** This is a
courtesy item, not a technical one — which is exactly the sort that quietly never gets done.

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

Everything in [architecture.md §12](architecture.md#12-package-layout) marked ✔, which is now the whole
of it: the command language and its runtime, permissions, cooldowns and toggles, variables with grants,
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
3. **Run the bot in its own channel for a week** before inviting anyone else. Every remaining unknown in
   this project is about what real chat does to it, not about what the code does.
