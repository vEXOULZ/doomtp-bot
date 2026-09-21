#!/usr/bin/env bash
# Pull the published image and, if it moved, restart the bot and check what the log covers (ADR-0013).
#
# Run it from the directory holding compose.yaml, .env and secrets/ — the systemd unit beside this file
# does that. Doing nothing is the common case: the tag usually hasn't moved.
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")/.."
compose=(docker compose -f compose.yaml -f compose.prod.yaml)

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
# Compose stops the old container with SIGTERM and waits out stop_grace_period, which is what lets the
# bot close its log sessions instead of leaving a gap that looks like a crash (ADR-0008).
"${compose[@]}" up -d
docker image prune --force >/dev/null  # dangling images: after an update, the one it replaced

# Give backfill its pass before asking what it covered.
sleep 60
"${compose[@]}" --profile tools run --rm coverage
