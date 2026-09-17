# doomtp-bot

A self-hosted, multi-channel Twitch chat bot with a composable command language
(`!random 1-100 | echo you rolled {1}`), user-published custom commands, a complete chat log, and a REST API
with a web UI.

**Status:** early scaffold. The app starts, migrates its databases, and serves `/healthz` and `/readyz`.
The command language parser is implemented (spec Appendix C). The runtime and the Twitch connection are next.

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
