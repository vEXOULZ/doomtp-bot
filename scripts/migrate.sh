#!/bin/sh
# The migrate step (ADR-0022): everything the bot needs from the database before it starts, run from the
# bot's own image. Compose runs it ahead of the bot on every `up`; both halves are safe to run again.
set -eu

doomtp-bot db upgrade

# Exit 2 means the bot was never signed in, so there is no account to own the packs yet. The bot starts
# anyway and says so; anything else stops the deploy here, with the old bot still running.
status=0
python /app/scripts/starter_pack.py "$@" || status=$?
if [ "$status" -ne 0 ] && [ "$status" -ne 2 ]; then
    exit "$status"
fi
