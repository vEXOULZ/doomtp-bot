# doomtp-bot

A self-hosted, multi-channel Twitch chat bot with a composable command language
(`!random 1-100 | echo you rolled {1}`), user-published custom commands, a complete chat log, and a REST API
with a web UI.

**Status:** feature-complete for v1 and not yet run in anger. The command language and its runtime,
permissions, cooldowns and toggles, variables, the chat log with gap backfill, custom commands with
versions and packs, triggers, the badword filter, the REST API and the web UI are all built and tested;
so is the deploy path. What is left is running it against real chat — see
[docs/roadmap.md](docs/roadmap.md).

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
| [docs/roadmap.md](docs/roadmap.md) | What is done, what is left, and why |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Branch rules, hooks, what CI checks |

## Local development

Requires Python 3.11+.

```bash
python -m venv .venv
```

```bash
.venv/Scripts/python -m pip install -e ".[dev]"
```

```bash
git config core.hooksPath .githooks
```

That last one is per clone and git can't do it for you. It stops commits landing on `main` and checks
branch names — see [CONTRIBUTING.md](CONTRIBUTING.md).

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

## Deploying to a server, step by step

This is the whole path from an empty Proxmox host to a bot that updates itself. It assumes a Proxmox
host and a Twitch application you have already created (see *Connecting to Twitch* above); everything
else is below. A server runs the image CI published rather than building one, and pulls it itself, so
nothing from outside ever connects to the homelab
([ADR-0013](docs/adr/0013-deploy-by-pulling-a-published-image.md)).

### 1. Create the guest

In the Proxmox web UI, **Create VM** — not a container. Docker inside an unprivileged LXC needs nesting,
keyctl and a cooperative overlayfs, and you would spend the evening on that instead of the bot.

| Setting | Value | Why |
|---------|-------|-----|
| OS | Debian 13 netinst ISO | Anything with a current Docker package does |
| System | Machine `q35`, BIOS `OVMF (UEFI)`, **QEMU Agent ticked** | The agent is what lets Proxmox shut it down gracefully |
| Disk | 16 GB on local storage | The chat log grows slowly; SQLite wants a real disk, never NFS or CIFS |
| CPU | 2 cores | The bot is idle most of the time |
| Memory | 2048 MB, ballooning off | The container is capped at 256 MB; the rest is the OS and page cache |
| Network | bridged, DHCP or a static lease | Outbound only. No port forward, ever |

Install Debian with an SSH server and no desktop. Then, in the guest:

```bash
sudo apt update && sudo apt install -y qemu-guest-agent && sudo systemctl enable --now qemu-guest-agent
```

### 2. Let the bot finish when the host stops it

The bot closes its chat-log sessions on `SIGTERM` and compose gives it 45 s. Both layers above that
default to 10, so a reboot would kill it mid-flush and leave a gap in the log that the deploy never
needed to cost ([ADR-0008](docs/adr/0008-history-backfill-recent-messages.md)).

```bash
sudo mkdir -p /etc/systemd/system.conf.d && printf '[Manager]
DefaultTimeoutStopSec=90s
' | sudo tee /etc/systemd/system.conf.d/timeout.conf
```

Then on the **Proxmox host**, give the guest the same room:

```bash
qm set <vmid> --startup 'order=1,up=30,down=120'
```

### 3. Install Docker

From Docker's own repository — Debian's `docker.io` package lags, and compose v2 matters here.

```bash
curl -fsSL https://get.docker.com | sudo sh && sudo usermod -aG docker "$USER"
```

Log out and back in, then check both halves are there:

```bash
docker compose version && docker run --rm hello-world
```

### 4. Put the files on the guest

```bash
sudo mkdir -p /srv/doomtp-bot && sudo chown "$USER" /srv/doomtp-bot && git clone <repo-url> /srv/doomtp-bot
```

Everything from here happens in `/srv/doomtp-bot`.

### 5. Configure it

```bash
cd /srv/doomtp-bot && cp .env.example .env && mkdir -p secrets data
```

Edit `.env`: `TWITCH_CLIENT_ID` and `TWITCH_BOT_ID` from the Twitch console, `BOT_OWNER_IDS` with your
own Twitch user ID, and `BOT_IMAGE=ghcr.io/<owner>/doomtp-bot:main` — the package CI publishes, all
lowercase. Leave `PUBLIC_BASE_URL=http://localhost:8080` alone; step 7 explains why.

Then the client secret, which only ever lives in a file:

```bash
printf '%s' 'the-secret-from-the-twitch-console' > secrets/twitch_client_secret && chmod 600 secrets/twitch_client_secret
```

### 6. Start it

```bash
docker compose -f compose.yaml -f compose.prod.yaml up -d
```

It pulls the image, creates both databases and runs the migrations. Give it a few seconds, then:

```bash
curl -s localhost:8080/readyz
```

Expect `"status":"degraded"` with `twitch: bot not authorized` — the databases are up and Twitch is
waiting for step 7. `docker compose logs -f doomtp-bot` shows what it is doing.

### 7. Authorize the bot account

The web UI is bound to `127.0.0.1` on the guest and the redirect URL registered with Twitch is
`http://localhost:8080/auth/callback`. An SSH tunnel satisfies both at once, so nothing in `.env` or the
Twitch console has to change. **From your own machine:**

```bash
ssh -L 8080:127.0.0.1:8080 you@bot-guest
```

Leave that open, and in your own browser go to <http://localhost:8080/auth/login>. Sign in as the **bot
account**, not your personal one — a token belonging to anyone else is rejected at the callback. Then
check again:

```bash
curl -s localhost:8080/readyz
```

`"status":"ok"`. The bot is now in its own chat. Type `!join` there from your channel's account to add
it, or `!join <channel>` as a bot owner.

### 8. Turn on unattended updates

```bash
sudo cp deploy/doomtp-bot-update.* /etc/systemd/system/ && sudo systemctl enable --now doomtp-bot-update.timer
```

Nightly it pulls, does nothing if the tag hasn't moved, and otherwise restarts through compose — which
waits out the grace period from step 2 — then runs the coverage check and takes its exit code. So a
failed unit means an unfilled gap in the chat log, not a failed deploy.

```bash
systemctl list-timers doomtp-bot-update
```

```bash
sudo systemctl start doomtp-bot-update && journalctl -u doomtp-bot-update -n 20
```

That second command deploys now instead of waiting for tonight.

### 9. Back it up

`bot.db` holds the OAuth refresh tokens, every channel's configuration and every custom command. A
Proxmox backup of a running VM snapshots a disk, which is not the same as a consistent SQLite file — so
run the job that is, from the guest's own crontab (`crontab -e`):

```bash
15 4 * * * cd /srv/doomtp-bot && docker compose -f compose.yaml -f compose.prod.yaml --profile tools run --rm backup >> data/backups/cron.log 2>&1
```

Keep a copy off the guest. The snapshots sit on the same disk as the originals, so they survive mistakes,
not drive failures.

### 10. Day to day

| | |
|---|---|
| Is it healthy? | `curl -s localhost:8080/readyz` |
| What is it doing? | `docker compose -f compose.yaml -f compose.prod.yaml logs -f doomtp-bot` |
| Did the log lose anything? | `docker compose -f compose.yaml -f compose.prod.yaml --profile tools run --rm coverage` |
| Deploy now | `sudo systemctl start doomtp-bot-update` |
| Install the starter commands | `docker compose -f compose.yaml -f compose.prod.yaml --profile tools run --rm starter-pack` |

To **roll back**, point `BOT_IMAGE` at a `:<sha>` tag and run the update unit again. Mind that migrations
run at startup and only go forward: roll back within a schema, or restore a backup taken before the
deploy.

### If something is wrong

| Symptom | Cause |
|---------|-------|
| `up -d` tries to build | `BOT_IMAGE` is unset, or `compose.prod.yaml` was left off the command |
| `denied` or `manifest unknown` on pull | The package path is wrong or private — GHCR paths are lowercase, and a private package needs `docker login ghcr.io` |
| `/readyz` says `bot not authorized` after signing in | The token belongs to another account, or `PUBLIC_BASE_URL` no longer matches the redirect URL registered with Twitch |
| The browser can't reach the login page | The tunnel dropped. The bot listens on `127.0.0.1` on the guest by design |
| `chatlog.unclean_shutdown_detected` at startup | Something killed the bot instead of stopping it — revisit step 2 |
| The update unit is failed but the bot is fine | That is the coverage check reporting a gap. `journalctl -u doomtp-bot-update` says which channel |

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
scripts/        the one-shot tools: backup, coverage, starter pack, fixture recording
deploy/         what a server needs: the update script and its systemd timer
docs/           architecture, spec, ADRs, grammar
```
