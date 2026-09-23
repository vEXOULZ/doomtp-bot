# doomtp-bot

Read [CONTRIBUTING.md](CONTRIBUTING.md) before changing anything. It covers branch names, the checks CI runs and the ADR process. Design decisions live in [docs/adr/](docs/adr/) and the overall design in [docs/architecture.md](docs/architecture.md).

## graphify

This repo has a [graphify](https://github.com/safishamsi/graphify) knowledge graph of the code, docs, ADRs and web UI templates. It shows the main hubs, the clusters of related code, and links from docs to code. There is only one graph, in the **main checkout's** `graphify-out/`, and it is not committed. Claude Code sessions run in worktrees under `.claude/worktrees/`, which have no graph of their own. Always go through `scripts/graphify.sh`: it finds the main checkout's graph from any worktree.

Rules:
- **Before exploring code** to answer a question or to plan a change, ask the graph first:
  - `scripts/graphify.sh query "<question>"` for broad context (add `--dfs` to follow one chain, `--budget N` for more output)
  - `scripts/graphify.sh explain "<Symbol>"` for one node and its neighbours
  - `scripts/graphify.sh path "<A>" "<B>"` for how two things connect
  - `scripts/graphify.sh affected "<Symbol>"` for what a change would touch
  - `scripts/graphify.sh god-nodes` for the most connected hubs

  Then read the files the graph points at (it gives `src=` and `loc=` for each node). The graph is a map, not the source of truth. Check what it says against the code before relying on it, especially edges marked INFERRED.
- Read `GRAPH_REPORT.md` in the main checkout's `graphify-out/` only for a broad architecture review. It lists the communities, hub nodes and surprising connections.
- **Do not run `graphify update .` or `/graphify` inside a worktree.** That would create a second, partial graph there.
- The graph shows `main` as it stands in the main checkout. After changes are merged and the main checkout is pulled, refresh the code part with `scripts/graphify.sh refresh`. That runs AST extraction only, with no LLM and no cost. A refresh renames the communities after their hub node, replacing the hand-written names. Changes to docs, ADRs or templates need a full `/graphify --update`, run from the main checkout.
- If `graph.json` is missing, say so and continue without the graph. Do not build one unless asked.
- `.graphifyignore` keeps the minified editor bundle and the worktrees out of the graph.
