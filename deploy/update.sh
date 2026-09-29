#!/usr/bin/env bash
# Pull the published image and, if it moved, restart the bot and check what the log covers (ADR-0013).
#
# Run it from the directory holding compose.yaml, .env and secrets/ — the systemd unit beside this file
# does that. Doing nothing is the common case: the tag usually hasn't moved.
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")/.."
# COMPOSE_FILE names the compose files, from the environment or .env, so a server's own override (a
# port, a network) stays in place; docker compose reads it itself. Without it, the two every server uses.
COMPOSE_FILE=${COMPOSE_FILE:-$(sed -n 's/^COMPOSE_FILE=//p' .env | tail -1)}
export COMPOSE_FILE=${COMPOSE_FILE:-compose.yaml:compose.prod.yaml}
compose=(docker compose)

# Read the one value we need rather than sourcing .env: it is a compose env file, not a shell script.
BOT_IMAGE=${BOT_IMAGE:-$(sed -n 's/^BOT_IMAGE=//p' .env | tail -1)}
: "${BOT_IMAGE:?set BOT_IMAGE in .env}"

digest() { docker image inspect --format '{{.Id}}' "$BOT_IMAGE" 2>/dev/null || true; }

before=$(digest)
"${compose[@]}" pull --quiet doomtp-bot
after=$(digest)
if [ "$before" = "$after" ]; then
    echo "up to date: $BOT_IMAGE ${after:0:19}"
    exit 0
fi

echo "updating: ${before:0:19} -> ${after:0:19}"
# The migrate step, from the new image: the schema upgrade and the `core` pack the new bot needs (ADR-0022).
# `up` below runs it too, but running it here first stops a failed one with the old bot still running and
# a clear message. It is safe to run twice.
if ! "${compose[@]}" run --rm migrate; then
    echo "migrate step failed: not updating. The old bot is still running." >&2
    exit 1
fi
# Only the bot. Postgres is named as a dependency so it gets started if it is down, but an unchanged,
# healthy one is left exactly as it is: its image never moves, and restarting it would drop the bot's
# connections to no purpose (ADR-0014).
# Compose stops the old container with SIGTERM and waits out stop_grace_period, which is what lets the
# bot close its log sessions instead of leaving a gap that looks like a crash (ADR-0008).
"${compose[@]}" up -d doomtp-bot
docker image prune --force >/dev/null  # dangling images: after an update, the one it replaced

# Give the bot a minute to connect and queue its backfill jobs (ADR-0024 §5), then ask what they covered,
# waiting up to five minutes for jobs still in the queue. That stays well inside the unit's TimeoutStartSec.
sleep 60
"${compose[@]}" --profile tools run --rm coverage --wait 300
