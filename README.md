# doomtp-bot

A self-hosted, multi-channel Twitch chat bot with a composable command language
(`!random 1-100 | echo you rolled {_1}`), user-published custom commands, a complete chat log, and a JSON API
that its web site, [doomtp-web](https://github.com/vEXOULZ/doomtp-web), is built on.

**Status:** feature-complete for v1 and not yet run in anger. The command language and its runtime,
permissions, cooldowns and toggles, variables, the chat log with gap backfill, custom commands with
versions and packs, triggers, the badword filter and the JSON API are all built and tested;
so is the deploy path. What is left is running it against real chat — see
[docs/roadmap.md](docs/roadmap.md).

## Connecting to Twitch

1. Create an application at https://dev.twitch.tv/console/apps.
   - **OAuth Redirect URLs:** `http://localhost:8080/auth/callback`. It must match `PUBLIC_BASE_URL` + `/auth/callback`.
     To let moderators sign in to the web admin with Twitch (ADR-0017), add a second one,
     `PUBLIC_BASE_URL` + `/auth/admin/callback` — for example `https://bot.example.com/auth/admin/callback`.
   - **Category:** Chat Bot. **Client type:** Confidential.
2. Put the Client ID in `.env` as `TWITCH_CLIENT_ID`, and the client secret in `secrets/twitch_client_secret`. For local development you can use `TWITCH_CLIENT_SECRET` instead.
3. Start the bot, then open `http://localhost:8080/auth/login` in a browser on the same machine. Sign in as the **bot account**, not your personal account.
4. The bot joins its own channel. A streamer adds it to their channel by typing `!join` in the bot's chat. A bot owner can add any channel with `!join <channel>` there. Set owners with `BOT_OWNER_IDS`.

**Keep development and the server apart.** Give each its own Twitch bot account and its own Twitch
application, which means its own `TWITCH_CLIENT_ID`, client secret, `TWITCH_BOT_ID` and database. Then
nothing you try in development reaches the real bot's channels or data. The bot token lives in the
database, not in `.env`. If `TWITCH_BOT_ID` changes while an older token is still stored, the bot refuses
to start the Twitch side and `/readyz` names the account it found. Sign in again at `/auth/login`, or
start from an empty database (`docker compose down -v` throws the dev volume away).

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

Requires Python 3.12+.

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

The tests run against a real Postgres rather than a stand-in
([ADR-0014](docs/adr/0014-storage-postgres-one-database-two-schemas.md)). The compose file carries a
throwaway one that keeps its data in a tmpfs:

```bash
docker compose --profile test up -d postgres-test
```

It listens on `127.0.0.1:55432`; set `TEST_DATABASE_URL` to use a different server. Each run creates a
database of its own and drops it afterwards, and each test rolls back, so nothing piles up.

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

To run the bot itself outside Docker, point it at a database — the default URL names the compose
service, which only resolves inside the compose network. The test container above will do. Create a
database on it once:

```bash
docker compose exec postgres-test createdb -U postgres doomtp_dev
```

and put its URL in `.env` (the password is in the URL here because this database is a throwaway; on a
server it comes from a secret file instead):

```
DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/doomtp_dev
```

The bot creates its own `bot` and `chatlog` schemas and migrates them on startup.

Then open http://127.0.0.1:8080/readyz. Configuration comes from environment variables or `.env`.
See [.env.example](.env.example).

## Running with Docker

```bash
cp .env.example .env
```

```bash
mkdir -p secrets data && printf '%s' 'your-client-secret' > secrets/twitch_client_secret
```

The database needs a password of its own. Pick one and write it to `secrets/postgres_password` — it is
read by both Postgres and the bot, so it only has to be typed once:

```bash
printf '%s' 'a long random password' > secrets/postgres_password
```

```bash
docker compose up -d --build
```

That starts Postgres and the bot. The bot waits for the database's healthcheck, then applies its
migrations at startup — on a fresh volume that is where the `bot` and `chatlog` schemas come from.

The API listens on `127.0.0.1:8080` only. To browse the chat log, run the optional read-only tool:

```bash
docker compose --profile tools up -d pgweb
```

It appears on `127.0.0.1:8001`. Open the `chatlog` schema for messages, `bot` for configuration.

The image installs the exact dependency set from `uv.lock`, so rebuilding an old commit gives the same
versions. After changing a dependency in `pyproject.toml`, refresh the lock (CI fails if it is stale):

```bash
uv lock
```

## Starter commands

The bot ships commands written in its own language rather than Python: the sentinels `false` and
`default` in the `core` system pack, and `hug`, `lurk`, `roll`, `so`, `deaths` and `weather`, published
globally as the `starter` pack. `weather` reads wttr.in, so it works once the bot account is a bot admin
and an admin has run `!admin http allow wttr.in`. They are not installed automatically; the database stays the only source of truth for
what the bot offers. **The bot refuses to start until `core` is installed at the version it expects.** The
`migrate` step installs it: compose runs that before the bot on every `up`, with the schema upgrade
(ADR-0022). On a brand-new database the bot starts anyway and warns, because the script installs under the
bot's account: sign the bot in at `/auth/login`, then run the step again and restart:

```bash
docker compose run --rm migrate
docker compose restart doomtp-bot
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

Once the bot is back, it queues a backfill job for every open gap. Give the jobs a moment, then check what
they covered:

```bash
docker compose --profile tools run --rm coverage --wait 300
```

It prints how each channel's last session ended and, for channels with backfill on, every gap in the last
week with whether it was filled. A gap a job is still to fill shows as `OPEN — queued as backfill job #N`
(or `running`), and `--wait` checks again every few seconds until no gap is waiting or the time is up.
`!backfill queue` in chat and `GET /api/v1/channels/{login}/backfill` show the same queue. Exit code 1
means a gap is still open — the usual causes are the
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
| Disk | 16 GB on local storage | The chat log grows slowly; Postgres wants a real disk, never NFS or CIFS |
| CPU | 2 cores | The bot is idle most of the time |
| Memory | 2048 MB, ballooning off | The bot is capped at 256 MB and Postgres at 512 MB; the rest is the OS and page cache |
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
lowercase. Use the **server's** Twitch app and bot account here, not the ones you develop with. Leave
`PUBLIC_BASE_URL=http://localhost:8080` for now: step 7 signs the bot in through a tunnel, and step 8
changes it once the web site is published.

Set `ADMIN_PASSWORD` in `.env` too. People sign in to the web admin with Twitch; the password is the
way in that still works when Twitch is down or the sign-in is misconfigured. It only works from the local
network (`ADMIN_PASSWORD_NETWORKS`: this host and the private ranges by default), so the public site
doesn't offer it. It is an admin login, so make it long. `.env` holds it in the clear, so keep the file to yourself (`grep ADMIN_PASSWORD .env` shows it):

```bash
printf 'ADMIN_PASSWORD=%s\n' "$(openssl rand -base64 24)" >> .env && chmod 600 .env
```

Then the two secrets, which only ever live in files:

```bash
printf '%s' 'the-secret-from-the-twitch-console' > secrets/twitch_client_secret && chmod 600 secrets/twitch_client_secret
```

```bash
printf '%s' 'a long random password' > secrets/postgres_password && chmod 600 secrets/postgres_password
```

The second one is the database's password, and you are choosing it right now — it is read by Postgres
when it initialises its volume and by the bot when it connects, so it is never typed anywhere else.
Change it later and the volume keeps the old one: `POSTGRES_PASSWORD_FILE` is only consulted on first
init, so a change means `ALTER ROLE doomtp PASSWORD …` inside the running database as well.

### 6. Start it

```bash
docker compose -f compose.yaml -f compose.prod.yaml up -d
```

It pulls the image, starts Postgres, waits for it to report healthy, then creates both schemas and
runs the migrations. Give it a few seconds, then:

```bash
curl -s localhost:8080/readyz
```

Expect `"status":"degraded"` with `twitch: bot not authorized` — the database is up and Twitch is
waiting for step 7. `docker compose logs -f doomtp-bot` shows what it is doing.

### 7. Authorize the bot account

The bot listens on `127.0.0.1` on the guest and the redirect URL registered with Twitch is
`http://localhost:8080/auth/callback`. An SSH tunnel satisfies both at once. Register that URL on the
server's Twitch app for this step; step 8 adds the public ones. **From your own machine:**

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

### 8. Publish the web site

The pages are [doomtp-web](https://github.com/vEXOULZ/doomtp-web), a separate site built to static
files; its CI publishes each build to that repository's `deploy` branch. The bot serves no pages. A
reverse proxy puts the two on **one hostname with HTTPS**, so the browser needs no CORS and the session
stays a plain cookie (ADR-0016):

| Paths | Go to |
|-------|-------|
| `/api/*`, `/auth/*`, `/static/*`, `/healthz`, `/readyz`, exactly `/docs` and exactly `/openapi.json` | the bot, `127.0.0.1:8080` |
| everything else | the site's files, with `index.html` for any path that isn't a file |

`/metrics` stays off the public side: scrape it on the LAN. Run the proxy, or the tunnel agent if you
publish through one, on the guest itself. The bot's port is published on `127.0.0.1` only, on purpose.
Which proxy, and where, is yours to pick. Keep those details in your private notes rather than this
repository.

Then point the bot at the public address. In `.env`:

```
PUBLIC_BASE_URL=https://bot.example.com
PUBLIC_WEB_UI=true
WEB_SITE_URL=https://bot.example.com
WEB_FORWARDED_ALLOW_IPS=172.18.0.1
```

`WEB_SITE_URL` is where the site's pages open. `!help` ends with a link to the channel's page there
(`/channels/<login>`). It is the same address as `PUBLIC_BASE_URL` when one proxy serves both.

`WEB_FORWARDED_ALLOW_IPS` is the address the bot sees the proxy connect from. The failed-login limit
trusts `X-Forwarded-For` from that address only. A proxy on the guest reaching `127.0.0.1:8080` arrives
through Docker's port mapping, so the bot sees the compose network's gateway, not `127.0.0.1`:

```bash
docker network inspect doomtp-bot_default -f '{{(index .IPAM.Config 0).Gateway}}'
```

On the **server's** Twitch app, add both redirect URLs for the new address:
`https://bot.example.com/auth/callback` (the bot account and broadcasters connecting) and
`https://bot.example.com/auth/admin/callback` (signing in to the web admin). Then restart:

```bash
docker compose -f compose.yaml -f compose.prod.yaml up -d
```

The bot token you stored in step 7 stays valid; nothing needs signing in again. Open
`https://bot.example.com/admin/login` and sign in with Twitch as a bot owner, or with the password.

### 9. Turn on unattended updates

```bash
sudo cp deploy/doomtp-bot-update.* /etc/systemd/system/ && sudo systemctl enable --now doomtp-bot-update.timer
```

Nightly it pulls, does nothing if the tag hasn't moved, and otherwise restarts through compose — which
waits out the grace period from step 2 — then runs the coverage check, which waits up to five minutes for
queued backfill jobs, and takes its exit code. So a failed unit means an unfilled gap in the chat log, not
a failed deploy.

```bash
systemctl list-timers doomtp-bot-update
```

```bash
sudo systemctl start doomtp-bot-update && journalctl -u doomtp-bot-update -n 20
```

That second command deploys now instead of waiting for tonight.

### 10. Back it up

The `bot` schema holds the OAuth refresh tokens, every channel's configuration and every custom command.
A Proxmox backup of a running VM snapshots a disk, which is not the same as a consistent dump of a
database that was mid-write — so run the job that is, from the guest's own crontab (`crontab -e`):

```bash
15 4 * * * cd /srv/doomtp-bot && docker compose -f compose.yaml -f compose.prod.yaml --profile tools run --rm backup >> data/backups/cron.log 2>&1
```

Keep a copy off the guest. The snapshots sit on the same disk as the originals, so they survive mistakes,
not drive failures.

### 11. Day to day

| | |
|---|---|
| Is it healthy? | `curl -s localhost:8080/readyz` |
| How often does it happen? | `curl -s localhost:8080/metrics` — counters in Prometheus text (ADR-0015); point a scraper on the LAN at it |
| What is it doing? | `docker compose -f compose.yaml -f compose.prod.yaml logs -f doomtp-bot` |
| Did the log lose anything? | `docker compose -f compose.yaml -f compose.prod.yaml --profile tools run --rm coverage --wait 300` |
| Deploy now | `sudo systemctl start doomtp-bot-update` |
| Upgrade the schema and install `core` and the starter commands | `docker compose -f compose.yaml -f compose.prod.yaml run --rm migrate` |
| Where does the schema stand? | `docker compose -f compose.yaml -f compose.prod.yaml run --rm --no-deps --entrypoint doomtp-bot migrate db current` |

To **roll back**, run `deploy/rollback.sh ghcr.io/<owner>/doomtp-bot:vX.Y.Z` (or any `:<sha>` tag). It takes
a backup, downgrades the schema to what that image expects using the image running now, points `BOT_IMAGE`
in `.env` at it and restarts the bot on it (ADR-0022). The update unit then stays on that tag until you
change `BOT_IMAGE` back. A downgrade that drops a column drops its data, so the backup it took is the way
back to that data. Releases are cut from `dev` into `main` (CONTRIBUTING.md, ADR-0021).

An image from before ADR-0022 has no migrate step. After rolling back to one, start it with `up -d --no-deps
doomtp-bot`, not a bare `up -d`, which would run the step and fail.

Both scripts use `compose.yaml` and `compose.prod.yaml`. A server with an override file of its own lists
all of them in `COMPOSE_FILE` in `.env` (`COMPOSE_FILE=compose.yaml:compose.prod.yaml:compose.local.yaml`),
which the scripts and a bare `docker compose` both read, so a rollback keeps the override.

### If something is wrong

| Symptom | Cause |
|---------|-------|
| `up -d` tries to build | `BOT_IMAGE` is unset, or `compose.prod.yaml` was left off the command |
| The bot logs `bot.schema_mismatch` and says to run `db upgrade` | It was started without the migrate step: `docker compose ... run --rm migrate`, then start it |
| The bot logs `bot.schema_mismatch` and says a newer build wrote the schema | An older image was started on a newer schema: use `deploy/rollback.sh`, which downgrades first |
| `migrate` fails and the bot isn't replaced | Working as intended: the old bot keeps running. `docker compose ... logs migrate` says why |
| `denied` or `manifest unknown` on pull | The package path is wrong or private — GHCR paths are lowercase, and a private package needs `docker login ghcr.io` |
| `/readyz` says the stored bot token is for another account | `TWITCH_BOT_ID` changed, or a token from before it was set is stored. Open `/auth/login` and sign in as the bot account |
| `/readyz` says `bot not authorized` after signing in | `PUBLIC_BASE_URL` no longer matches the redirect URL registered with Twitch |
| Twitch says the redirect URI doesn't match | `PUBLIC_BASE_URL` + `/auth/callback` (or `/auth/admin/callback`) isn't registered on the Twitch app the bot's `TWITCH_CLIENT_ID` names |
| The password form is missing, or the login says "only works from the local network" | You are outside `ADMIN_PASSWORD_NETWORKS`, or the proxy isn't in `WEB_FORWARDED_ALLOW_IPS`, so the bot can't tell where you are |
| Nobody can log in with the password: "too many failed logins" | `WEB_FORWARDED_ALLOW_IPS` doesn't name the proxy, so every visitor shares its address and its limit (step 8) |
| The site loads, but `/admin` shows nothing and the API calls fail | The proxy sends the bot's paths to the site's files. Check the table in step 8 |
| The browser can't reach `/auth/login` before step 8 | The tunnel dropped. The bot listens on `127.0.0.1` on the guest by design |
| `chatlog.unclean_shutdown_detected` at startup | Something killed the bot instead of stopping it — revisit step 2 |
| The update unit is failed but the bot is fine | That is the coverage check reporting a gap. `journalctl -u doomtp-bot-update` says which channel |

## Web site

The pages, public and admin, are [doomtp-web](https://github.com/vEXOULZ/doomtp-web): a separate Vue site
over this bot's JSON API, served from the same hostname (ADR-0016; step 8 above). The bot serves the API,
`/auth/*`, `/static/*` (the expression editor and the railroad diagrams the site loads) and Swagger at
`/docs`, and no pages of its own. Anything the site needs that the API doesn't return is added to the API
here.

To work on the site without Twitch, run `scripts/dev_api.py` here. It serves the same API with made-up
data, and doomtp-web's `npm run dev` forwards to it. Its README has the details.

**Admin sign-in.** Bot owners and admins sign in with Twitch, and so do broadcasters and moderators, who
get only their own channels (ADR-0017). With `SIGNIN_PROVIDER=vexoulz` that sign-in goes through
vexoulz-auth, shared with the other vexoulz sites, instead of straight to Twitch (ADR-0023); the routes and
sessions are the same. `ADMIN_PASSWORD` in `.env` is the way in that doesn't need Twitch.
Without it the password login is off. It is taken only from `ADMIN_PASSWORD_NETWORKS` (this host and the
private ranges by default; `*` for anywhere), so from outside the site offers Twitch alone. A request that
came through a proxy the bot doesn't trust is never local, since its address is the proxy's. Failed logins are limited per client address. Behind a proxy, set
`WEB_FORWARDED_ALLOW_IPS` to the proxy's address, or the limit counts every visitor as the proxy.

## Backups

The `bot` schema holds the OAuth refresh tokens and every channel's configuration; the `chatlog` schema
holds the message history. One script dumps both, using `pg_dump`, which takes its snapshot inside a
single transaction and is therefore safe to run while the bot is writing:

```bash
docker compose --profile tools run --rm backup
```

Each run writes `<schema>-<timestamp>.dump` into `data/backups/` and keeps the newest 7 of each
(`--keep`). The two schemas are dumped separately on purpose: the state you cannot lose and the log that
grows without bound do not have to share a retention policy. For a nightly copy, add it to the host's
crontab (`crontab -e`):

```bash
15 4 * * * cd /srv/doomtp-bot && docker compose --profile tools run --rm backup >> data/backups/cron.log 2>&1
```

To restore, stop the bot, drop the schema and let `pg_restore` put it back:

```bash
docker compose stop doomtp-bot
```

```bash
docker compose exec -T postgres psql -U doomtp -d doomtp -c 'DROP SCHEMA bot CASCADE'
```

```bash
docker compose exec -T postgres pg_restore -U doomtp -d doomtp < data/backups/bot-20260922T041500Z.dump
```

Into a new, empty database (a rebuilt server), create the one extension first. The `chatlog` dump uses
`unaccent`, which lives in `public` and so isn't in either archive:

```bash
docker compose exec -T postgres psql -U doomtp -d doomtp -c 'CREATE EXTENSION IF NOT EXISTS unaccent WITH SCHEMA public'
```

The archives are in `custom` format, so `pg_restore --list` shows what is in one and `--table=` pulls a
single table out without touching the rest. Keep a copy off the host: the backups sit on the same disk as
the originals, so they survive mistakes, not drive failures.

## Project layout

```
src/doomtp_bot/
  api/          FastAPI app: health, OAuth, the language API, /api/v1, admin sessions, and the
                static files the web site loads (editor bundle, railroad diagrams)
  core/         dispatch, channels, outbox, health, capabilities, stream status
  lang/         AST, parse errors, PEG recursive-descent parser
  runtime/      Result, command specs, resolver, preflight, executor, explain
  storage/      Postgres connections + migrations for the bot and chatlog schemas
  twitch/ history/ chatlog/ moderation/ policy/ customcmds/
  variables/ triggers/ filters/ audit/ modules/
tests/          pytest; tests/lang/corpus.yaml is the shared parser conformance corpus
                and tests/fixtures/eventsub/ holds EventSub payloads recorded from Twitch
web-editor/     the CodeMirror expression editor — npm, and the only Node in the repo
scripts/        the one-shot tools: backup, coverage, starter pack, fixture recording
deploy/         what a server needs: the update script and its systemd timer
docs/           architecture, spec, ADRs, grammar
```
