#!/bin/sh
# Run graphify against the one knowledge graph this clone keeps, in the main checkout's graphify-out/.
#
#     scripts/graphify.sh query "how does a command reach the outbox"
#     scripts/graphify.sh explain "PolicyService"
#     scripts/graphify.sh refresh          # rebuild the code part of the graph (AST only, no LLM)
#     scripts/graphify.sh hook-guard search # the Claude Code PreToolUse hook (.claude/settings.json)
#
# Claude Code sessions run in linked worktrees under .claude/worktrees/, which have no graph of their
# own; the main checkout sits next to the git common dir, so every worktree finds the same one.
# A missing `graphify` is not an error: the script says so and exits 0, so hooks never break a session.
set -eu

command -v graphify >/dev/null 2>&1 || { echo "graphify is not installed (pip install graphifyy)" >&2; exit 0; }

main=$(cd "$(git rev-parse --path-format=absolute --git-common-dir)/.." && pwd)
export GRAPHIFY_OUT="$main/graphify-out"

case "${1:-}" in
refresh)
    # Without the ignore file the minified editor bundle floods the graph with ~800 meaningless nodes.
    [ -f "$main/.graphifyignore" ] || { echo "no .graphifyignore in $main: pull main there first" >&2; exit 1; }
    cd "$main"
    exec graphify update .
    ;;
hook-guard)
    # The guard's nudge names bare `graphify`, which finds no graph from a worktree; name this script.
    graphify "$@" | sed 's/`graphify /`scripts\/graphify.sh /g'
    ;;
*)
    exec graphify "$@"
    ;;
esac
