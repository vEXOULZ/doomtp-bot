# ADR-0021: Integrate on dev, release to main

**Status:** Accepted — 2026-09-28
**Date:** 2026-09-28
**Deciders:** Project owner

## Context

Every pull request merged into `main` publishes `:main`, and the server's update timer deploys whatever
`:main` points at (ADR-0013). So each merge is a production deploy. Features that belong together reach
production one at a time, a half-finished series is live between its merges, and there is no name for
"what was running last week" other than a commit sha.

## Decision

- **`dev` is the integration branch.** Feature, bugfix and chore branches open their pull requests
  against `dev`, which is the repository's default branch. `dev` has the same protection as `main`: pull
  requests only, the four CI checks green, up to date with its base.
- **`main` is production.** It takes pull requests only from `dev` (a release), `release/*` or `hotfix/*`.
  CI's `branch-name` job enforces this with `check-pr-branches.sh` (now `.conventions/githooks/`, from vEXOULZ/conventions).
- **A release** is one pull request from `dev` into `main`, merged with a merge commit. The version is
  bumped on `dev` first, in a `release/x-y-z` pull request that changes `pyproject.toml` and
  `__version__`. After the merge, a `vX.Y.Z` tag on `main` names it:
  `gh release create vX.Y.Z --target main --generate-notes`. CI refuses a tag that disagrees with the
  package version, so `/healthz` and `$bot.version` always match the tag.
- **Images:** a push to `main` publishes `:main`, a push to `dev` publishes `:dev`, and a tag publishes
  `:vX.Y.Z`. Each also gets its `:<sha>`. Production follows `:main` as before. A test box can follow
  `:dev`.
- **Rolling back** is pinning `BOT_IMAGE` to the previous `:vX.Y.Z`. The migration caveat of ADR-0013
  still applies.
- **A hotfix** branches from `main`, merges into `main`, and then `main` merges back into `dev` in a pull
  request, so `dev` never loses it.

## Amendment (2026-10-05): releases run from release.yml

The release steps above are now a workflow, from vEXOULZ/conventions v2.0.0. **Actions → release → Run workflow** works out the next version from the Conventional Commits since the last tag, writes it into `pyproject.toml`, `uv.lock` and `__version__` on a `release/x-y-z` branch and opens "Release vX.Y.Z" from it into `main`. That replaces the separate version pull request into `dev` and the release pull request from `dev`. When it merges, the workflow tags the merge commit, publishes the GitHub release and opens the pull request that brings `main` back into `dev`. A required check, `conventions / version`, fails any pull request into `main` that doesn't raise the version to one with no tag yet, which closes the gap that left v0.2.0's tag without an image in doomtp-bot. CONTRIBUTING.md has the steps.

## Options Considered

### Option A: keep merging features into main
**Pros:** nothing to set up, one pull request per change. **Cons:** every merge is a deploy, and there are
no release names to roll back to.

### Option B: dev plus main, releases tagged (chosen)
**Pros:** production changes only when a release is merged, related features ship together, and every
release has a tag and an image. **Cons:** a second pull request per release, and hotfixes need a merge
back into `dev`.

### Option C: keep main as the integration branch and deploy tags only
The server would follow `:vX.Y.Z` rather than `:main`. **Pros:** one long-lived branch. **Cons:** the
server's `BOT_IMAGE` would have to change for every release, since there is no moving tag to follow, and
`main` would no longer say what production runs.

## Consequences

- **Easier:** batching features into one deploy, naming releases, rolling back to a release.
- **Harder:** a release needs a version bump and a second pull request. Open pull requests have to be
  pointed at `dev`.
- A pull request stacked on another one still targets its base branch, and the rule in CONTRIBUTING.md
  about stacked pull requests is unchanged.

## Action Items

1. [x] `check-pr-branches.sh` in CI, `dev` refused by the pre-commit hook, and CI publishing `:dev` and
   `:vX.Y.Z`. *(2026-09-28)*
2. [x] Create `dev` on GitHub from `main`, protect it like `main`, and make it the default branch.
   *(2026-09-28. Both branches require the four CI jobs and an up-to-date branch.)*
3. [x] Point the open pull requests at `dev`. *(2026-09-28)*
4. [x] The first release, `v0.2.0`, once this change reaches `main`. *(2026-09-28: vEXOULZ/doomtp-bot#54,
   tagged `v0.2.0`.)*
