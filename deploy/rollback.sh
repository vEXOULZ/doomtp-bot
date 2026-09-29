#!/usr/bin/env bash
# Roll the bot back to an older image, schema included (ADR-0022):
#
#     deploy/rollback.sh ghcr.io/<owner>/doomtp-bot:0.1.0
#
# Run it from the server, like update.sh. Only the image running now has the code to undo its own
# migrations, so it does the downgrade, after a backup; then BOT_IMAGE in .env points at the target and the
# bot restarts on it without the migrate step. A downgrade that drops a column drops its data: the backup
# is the way back to it.
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")/.."
# COMPOSE_FILE names the compose files, from the environment or .env, so a server's own override (a
# port, a network) stays in place; docker compose reads it itself. Without it, the two every server uses.
COMPOSE_FILE=${COMPOSE_FILE:-$(sed -n 's/^COMPOSE_FILE=//p' .env | tail -1)}
export COMPOSE_FILE=${COMPOSE_FILE:-compose.yaml:compose.prod.yaml}
compose=(docker compose)

target=${1:?usage: deploy/rollback.sh <image>}
current=${BOT_IMAGE:-$(sed -n 's/^BOT_IMAGE=//p' .env | tail -1)}
: "${current:?set BOT_IMAGE in .env}"
if [ "$target" = "$current" ]; then
    echo "already on $target" >&2
    exit 1
fi

docker pull --quiet "$target" >/dev/null
# The schema the target was built for. An image from before ADR-0022 has no `db` command (it would start the
# bot instead), so look for the module first. Its numbered SQL files are the same revisions, so their count
# is its head.
if docker run --rm --entrypoint python "$target" -c 'import doomtp_bot.storage.schema' 2>/dev/null; then
    heads=$(docker run --rm --entrypoint doomtp-bot "$target" db heads)
else
    heads=$(docker run --rm --entrypoint python "$target" -c \
        'from doomtp_bot.storage.db import load_migrations as m; print("bot=%04d chatlog=%04d" % (len(m("bot")), len(m("chatlog"))))')
fi
bot=$(sed -n 's/.*\bbot=\([0-9a-z]*\).*/\1/p' <<<"$heads")
chatlog=$(sed -n 's/.*chatlog=\([0-9a-z]*\).*/\1/p' <<<"$heads")
: "${bot:?cannot tell the target's bot schema from: $heads}" "${chatlog:?cannot tell the target's chatlog schema from: $heads}"
echo "rolling back: $current -> $target (bot=$bot chatlog=$chatlog)"

"${compose[@]}" --profile tools run --rm backup
# The current image, while the current bot keeps running: it would refuse the older schema only on its
# next start, and the target replaces it straight after.
"${compose[@]}" run --rm --no-deps --entrypoint doomtp-bot migrate db downgrade --bot "$bot" --chatlog "$chatlog"

if grep -q '^BOT_IMAGE=' .env; then
    sed -i "s|^BOT_IMAGE=.*|BOT_IMAGE=$target|" .env
else
    echo "BOT_IMAGE=$target" >>.env
fi
# --no-deps: no migrate step, which would only upgrade again (and an image from before ADR-0022 has none).
BOT_IMAGE=$target "${compose[@]}" up -d --no-deps doomtp-bot
echo "rolled back to $target. BOT_IMAGE in .env now names it, so update.sh stays there until you change it."
