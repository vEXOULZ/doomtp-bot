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
  api/          FastAPI app (health now; parse/explain/language/v1 next)
  core/         events, event bus, health registry, single-instance lock
  lang/         AST, parse errors, PEG recursive-descent parser
  runtime/      Result, command specs (resolver/preflight/executor next)
  storage/      SQLite connections + migrations for bot.db and chatlog.db
  twitch/ history/ chatlog/ moderation/ policy/ customcmds/
  variables/ triggers/ filters/ audit/ modules/   (packages reserved per architecture)
tests/          pytest; tests/lang/corpus.yaml is the shared parser conformance corpus
web-editor/     CodeMirror/Lezer expression editor (planned)
docs/           architecture, spec, ADRs, grammar
```
