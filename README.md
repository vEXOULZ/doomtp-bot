# doomtp-bot

A self-hosted, multi-channel Twitch chat bot with a composable command language
(`!random 1-100 | echo you rolled {1}`), user-published custom commands, a complete chat log, and a REST API
with a web UI.

**Status:** working core. Implemented so far:
- Parser and runtime for the command language
- Permissions, cooldowns and toggles
- Variables
- The chat log
- The Twitch connection (EventSub chat events, Helix sending, OAuth)

Custom commands, triggers, filters, history backfill and the web UI are next.

## Connecting to Twitch

1. Create an application at https://dev.twitch.tv/console/apps.
   - **OAuth Redirect URL:** `http://localhost:8080/auth/callback`. It must match `PUBLIC_BASE_URL` + `/auth/callback`.
   - **Category:** Chat Bot. **Client type:** Confidential.
2. Put the Client ID in `.env` as `TWITCH_CLIENT_ID`, and the client secret in `secrets/twitch_client_secret`. For local development you can use `TWITCH_CLIENT_SECRET` instead.
3. Start the bot, then open `http://localhost:8080/auth/login` in a browser on the same machine. Sign in as the **bot account**, not your personal account.
4. The bot joins its own channel. A streamer adds it to their channel by typing `!join` in the bot's chat. A bot owner can add any channel with `!join <channel>` there. Set owners with `BOT_OWNER_IDS`.

## Docs

| | |
|---|---|
| [docs/architecture.md](docs/architecture.md) | System overview, requirements, data model |
| [docs/command-language-spec.md](docs/command-language-spec.md) | Command language spec (incl. PEG grammar) |
| [docs/namespaces.md](docs/namespaces.md) | Placeholder namespaces, argument types |
| [docs/variable-access-matrix.md](docs/variable-access-matrix.md) | Variable read/write rules and grants |
| [docs/adr/](docs/adr/) | Architecture decision records |

## Local development

Requires Python 3.11+.

```bash
python -m venv .venv
```

```bash
.venv/Scripts/python -m pip install -e ".[dev]"
```

(on Linux/macOS use `.venv/bin/python`)

```bash
.venv/Scripts/python -m pytest
```

Two of the test files talk to Twitch's own event simulator: one speaks the EventSub handshake for real
(welcome, session id, reconnect), the other checks the recorded payloads in `tests/fixtures/eventsub/`
still match what Twitch sends. They skip themselves unless the [Twitch CLI][twitch-cli] is installed —
set `TWITCH_CLI` to its path if it isn't on `PATH`, and re-record the fixtures with:

```bash
.venv/Scripts/python scripts/record_eventsub.py --start-server
```

[twitch-cli]: https://dev.twitch.tv/docs/cli/

```bash
.venv/Scripts/python -m doomtp_bot
```

Then open http://127.0.0.1:8080/readyz. Configuration comes from environment variables or `.env`.
See [.env.example](.env.example).

## Running with Docker

```bash
cp .env.example .env
```

```bash
mkdir -p secrets data && printf '%s' 'your-client-secret' > secrets/twitch_client_secret
```

```bash
docker compose up -d --build
```

The API listens on `127.0.0.1:8080` only. To browse the chat log, run the optional Datasette tool:

```bash
docker compose --profile tools up -d datasette
```

The image installs the exact dependency set from `uv.lock`, so rebuilding an old commit gives the same
versions. After changing a dependency in `pyproject.toml`, refresh the lock (CI fails if it is stale):

```bash
uv lock
```

## Starter commands

The bot ships a small set of commands written in its own language rather than Python — `hug`, `lurk`,
`roll`, `so` and `deaths` — published globally as the `starter` pack. They are not installed
automatically; the database stays the only source of truth for what the bot offers:

```bash
docker compose --profile tools run --rm starter-pack
```

It creates them under the bot's own account (`--dry-run` says what it would change first). Re-run it
after an upgrade to pick up fixes: it edits only what changed and leaves anything else alone. A channel
turns the set off with `!module disable starter` or one command with `!cmd disable hug`, and `!cc info
hug` shows the body of any of them. `!deaths` writes a channel variable, so each channel allows it once
with `!cc grant deaths channel.deaths`.

## Deploying an update

The chat log records when the bot was listening, and fills what it missed from the recent-messages
service when it comes back (ADR-0008). That only works if the old process is allowed to finish: stop it
with a signal, never with a kill.

```bash
docker compose up -d --build
```

Compose sends `SIGTERM` and waits out `stop_grace_period` (45s), which is the bot's cue to close its log
sessions and flush the writer queue — the last line it logs is `bot.stop`. A process that dies without it
leaves its sessions open; the next startup closes them at the last message it stored and says
`chatlog.unclean_shutdown_detected`, so the gap is honest either way, but it is wider than it had to be.

Once the bot is back, give backfill its pass and check what it covered:

```bash
docker compose --profile tools run --rm coverage
```

It prints how each channel's last session ended and, for channels with backfill on, every gap in the last
week with whether it was filled. Exit code 1 means a gap is still open — the usual causes are the
recent-messages service being down or the outage being longer than its 800-message reach, and both are
worth seeing in the log before you assume the history is complete.

## Web UI

With the bot running, <http://127.0.0.1:8080/> documents every feature, the command reference is
generated from the bot's own specs, and `/docs/language` is the language reference.

The admin pages at `/admin` show health, channels, modules, triggers, filters, published commands and the
audit trail, and can toggle each of those. They need a password:

```bash
mkdir -p secrets && printf '%s' 'a long random password' > secrets/admin_password
```

Then set `ADMIN_PASSWORD_FILE=./secrets/admin_password` in `.env`. Without it `/admin` returns 404 rather
than being open. The bot binds to `127.0.0.1` by default; keep it on the LAN.

## Backups

`bot.db` holds the OAuth refresh tokens and every channel's configuration; `chatlog.db` holds the message
history. Both are backed up by one script, which uses SQLite's online backup API and is safe to run while
the bot is writing:

```bash
docker compose --profile tools run --rm backup
```

Each run writes a gzipped snapshot to `data/backups/` and keeps the newest 7 per database (`--keep`).
For a nightly copy, add it to the host's crontab (`crontab -e`):

```bash
15 4 * * * cd /srv/doomtp-bot && docker compose --profile tools run --rm backup >> data/backups/cron.log 2>&1
```

Restore by stopping the bot, then gunzipping the snapshot over the database file. Keep a copy off the host:
the backups sit on the same disk as the originals, so they survive mistakes, not drive failures.

## Project layout

```
src/doomtp_bot/
  api/          FastAPI app: health, OAuth, the language API and /api/v1
  core/         dispatch, channels, outbox, health, capabilities, stream status
  lang/         AST, parse errors, PEG recursive-descent parser
  runtime/      Result, command specs, resolver, preflight, executor, explain
  storage/      SQLite connections + migrations for bot.db and chatlog.db
  webui/        server-rendered pages, the admin UI and the static files they serve
  twitch/ history/ chatlog/ moderation/ policy/ customcmds/
  variables/ triggers/ filters/ audit/ modules/
tests/          pytest; tests/lang/corpus.yaml is the shared parser conformance corpus
                and tests/fixtures/eventsub/ holds EventSub payloads recorded from Twitch
web-editor/     the CodeMirror expression editor — npm, and the only Node in the repo
docs/           architecture, spec, ADRs, grammar
```
