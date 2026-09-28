#!/bin/sh
# Which branch may open a pull request into which (ADR-0021). CI runs it for every pull request:
#
#     .githooks/check-pr-branches.sh <head> <base>
#
# Work integrates on `dev`, and `main` is what production runs. So:
#   - into main:  only dev (a release), release/* or hotfix/*
#   - into dev:   any Conventional Branch, or main itself (bringing a hotfix back)
#   - elsewhere:  any Conventional Branch (a PR stacked on another one)
set -eu

head=${1:-}
base=${2:-}
if [ -z "$head" ] || [ -z "$base" ]; then
    echo "check-pr-branches: usage: check-pr-branches.sh <head> <base>" >&2
    exit 2
fi

here=$(dirname "$0")

case "$base" in
    main)
        case "$head" in
            dev | release/* | hotfix/*) ;;
            *)
                cat >&2 <<EOF
'$head' can't merge into main.

main is what production runs, and it only takes a release (dev), a release/* branch or a hotfix/*.
Point this pull request at dev instead:

    gh pr edit --base dev
EOF
                exit 1
                ;;
        esac
        ;;
    dev)
        # A hotfix lands on main first; main then merges back into dev so dev never loses it.
        [ "$head" = main ] && exit 0
        ;;
esac

[ "$head" = dev ] && exit 0
exec "$here/check-branch-name.sh" "$head"
