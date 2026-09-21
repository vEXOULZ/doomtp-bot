# Roadmap and progress

**As of 2026-09-21.** Every decision in this project carries its own action items, so this page is the
sum of them: what each ADR set out to do, how much of it is done, and what is left. It is written by
hand — when an item closes, tick it in its ADR and update the row here in the same commit.

## Where the plan stands

| ADR | Decision | Items | State |
|-----|----------|-------|-------|
| [0001](adr/0001-chat-transport-eventsub-websocket.md) | EventSub WebSocket in, Helix out | 6/6 | Complete |
| [0002](adr/0002-twitch-library-twitchio.md) | TwitchIO 3.x behind an adapter | 4/4 | Complete |
| [0003](adr/0003-storage-sqlite.md) | SQLite: `bot.db` and `chatlog.db` | 5/5 | Complete |
| [0004](adr/0004-modular-monolith.md) | One async process, one container | 4/4 | Complete |
| [0005](adr/0005-command-pipeline-runtime.md) | Parse → resolve → preflight → execute | 6/6 | Complete |
| [0006](adr/0006-permissions-cooldowns-toggles.md) | Ranked roles, two cooldowns, layered toggles | 4/5 | One deferred to v1.x |
| [0007](adr/0007-channel-access-tiers.md) | Basic, moderator and full channel tiers | 5/5 | Complete |
| [0008](adr/0008-history-backfill-recent-messages.md) | Fill log gaps from recent-messages | 5/6 | One blocked |
| [0009](adr/0009-user-custom-commands-sharing.md) | User-owned commands: link, publish, version | 6/6 | Complete |
| [0010](adr/0010-variables-scopes.md) | Seven namespaces, exact-name write grants | 4/4 | Complete |
| [0011](adr/0011-parser-and-web-editor.md) | One server-side PEG parser, a local highlighter | 6/6 | Complete |
| [0012](adr/0012-derived-commands-and-packs.md) | Derived commands are global publications | 6/6 | Complete |
| [0013](adr/0013-deploy-by-pulling-a-published-image.md) | CI publishes, the server pulls | 3/5 | Two need the server |

**64 of 68 action items are closed.** The four that aren't are below, and none of them is waiting on
code that hasn't been thought through — they are waiting on a person, a server, or a version boundary.

## What is left, and why

### Waiting on the server — ADR-0013 items 4 and 5

The deploy path is built and tested against a local registry standing in for GHCR: a directory holding
only the compose files pulls the image rather than building it, `deploy/update.sh` does nothing when the
tag hasn't moved and restarts cleanly when it has, and the packaged tools run from the image. What has
never run is the real thing:

- **Publish to GHCR.** Needs a git remote and a first push to `main`. Nothing verifies the login, the
  lowercased package path or `packages: write` until then.
- **Raise the guest's shutdown timeouts** past the 45 s stop grace period, and install
  `qemu-guest-agent`. Until that is done, a host reboot can kill the bot mid-flush and the next startup
  records an unclean shutdown — a wider gap in the chat log than the deploy needed to cost.

### Waiting on a reply — ADR-0008 item 4

Backfill reads from `recent-messages.robotty.de`, a service someone else runs and pays for. The bot
already asks each channel before using it (`!backfill`), records what it filled, and keeps its requests
within the documented reach. **Contacting the maintainer about the bot integration and the keep-warm
interval is still open, and should happen before backfill is enabled for a real channel.** This is a
courtesy item, not a technical one — which is exactly the sort that quietly never gets done.

### Waiting on v1.x — ADR-0006 item 5

Cooldowns are checked in preflight, so a cooldown failure stops the whole line. Spec §5.2 wants them
checked at runtime instead, failing the individual invocation with code 128 so `!a || !b` runs `b` when
`a` is on cooldown. It is a behaviour change to the operator semantics and belongs with a version bump,
not in a patch.

## Deferred by design

These are decided, not forgotten. See the [language proposal §6](command-language-proposal.md) for the
full table.

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

The test suite is 544 pytest cases plus 36 vitest ones, with the parser corpus shared between them, the
EventSub adapter pinned to payloads recorded from Twitch's own simulator, and the railroad diagrams
checked against the grammar.

## Next, in the order it makes sense

1. **Give it a remote and push.** That unblocks ADR-0013 item 4 and is the only way to find out whether
   the publish step works.
2. **Stand up the Proxmox guest** and run through the deploy tutorial in the README, closing ADR-0013
   item 5 on the way.
3. **Write to the recent-messages maintainer** (ADR-0008 item 4), then turn backfill on for one channel
   and read what `scripts/coverage.py` says the next morning.
4. **Run the bot in its own channel for a week** before inviting anyone else. Every remaining unknown in
   this project is about what real chat does to it, not about what the code does.
