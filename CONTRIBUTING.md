# Contributing

<!-- conventions:begin: synced from vEXOULZ/conventions; edit it there, then run `conventions sync` -->
## Set up the hooks once per clone

```bash
git config core.hooksPath .conventions/githooks
```

Git does not carry hooks in a clone, so this is the one step nothing can do for you (`conventions sync`
does it as a side effect). Without it, the branch rules below are only enforced in CI, which is a slower
way to hear about a typo. A repo's own extra checks live in `.githooks/local/pre-commit`, which the
shared hook runs after its own.

## Branches

Branch names follow [Conventional Branch](https://conventional-branch.github.io/): `<type>/<description>`,
where the description is lowercase letters, digits and single hyphens.

| Type | For |
|------|-----|
| `feature/` | a new capability, e.g. `feature/part-mapping` |
| `bugfix/` | a fix, e.g. `bugfix/issue-42-restricted-seek` |
| `hotfix/` | a fix that can't wait for the usual path, e.g. `hotfix/broken-deploy` |
| `release/` | preparing a version, e.g. `release/1-2-0` |
| `chore/` | dependencies, tooling, docs, anything with no behaviour change, e.g. `chore/bump-vite` |

A ticket number is just another word in the description.
`.conventions/githooks/check-branch-name.sh <name>` says whether a name passes, and CI runs the same
script (the `conventions / branch-name` check) against the branch a pull request comes from.

### Release flow: dev → main

Work integrates on `dev`, and `main` is what production runs. The `pre-commit` hook refuses a commit
made on `main`, `master`, `dev` or `develop`.

- Feature and bugfix branches start from `dev`, and their pull requests target `dev`.
- A release is a pull request into `main`, and it has to raise the version (`conventions / version`).
  Start one from the **Actions → release → Run workflow** button: it works out the next version from
  the Conventional Commits since the last tag (`feat` is a minor, `!` or `BREAKING CHANGE` a major,
  anything else a patch; choose a bump to override), writes it on a `release/X-Y-Z` branch and opens
  "Release vX.Y.Z" into `main`.
- Merging the release pull request tags the merge commit `vX.Y.Z`, publishes the GitHub release, and
  opens the pull request that brings `main` back into `dev`. Merge that one too.
- A `hotfix/*` branch starts from `main`, raises the version (`conventions version set X.Y.Z`) and
  merges into `main`; it is released the same way, and `main` then merges back into `dev` so `dev`
  never loses it.
- The `conventions / branch-name` check refuses any other pull request into `main`.

```bash
git switch dev && git pull && git switch -c feature/what-you-are-doing
```

Concluding a merge that hit conflicts is a commit on the branch merged into, and the hook lets that one
through: the rule is about where work starts, not where it lands. `git commit --no-verify` skips the
hook entirely. It exists for the day you need it, not for the day you are in a hurry.

## Checks

Every pull request runs:

- **`conventions / branch-name`:** the branch name, and in a `flow = "dev"` repo whether it may merge
  into its base.
- **`conventions / check`:** the synced files match the version pinned in `.conventions.toml`, and the
  repo follows the conventions for its profile. A public repo is also checked for private
  infrastructure (addresses, server paths).
- **`conventions / version`:** every file that carries the version (`pyproject.toml`, `uv.lock`,
  `__version__`, `package.json`, `package-lock.json`) says the same. In a `flow = "dev"` repo a pull
  request into `main` must also raise it, to a version with no tag yet.
- **`ci / …`:** the repo's lint, tests and build, from the reusable workflows in
  [vEXOULZ/conventions](https://github.com/vEXOULZ/conventions).

They are required checks on `main`. The repo's settings, protection included, are set by
`conventions repo-settings --apply`.

## Shared conventions

This section, `.conventions/`, `.gitattributes` and the other synced files come from
[vEXOULZ/conventions](https://github.com/vEXOULZ/conventions), at the version pinned in
`.conventions.toml`. Don't edit them here: change them there, release, and bump the pin (Renovate opens
that pull request). After a bump, rewrite the copies and commit them:

```bash
uvx --from "git+https://github.com/vEXOULZ/conventions@$(sed -n 's/^version *= *"\(.*\)"/\1/p' .conventions.toml)" conventions sync
```
<!-- conventions:end -->

## This repo

The release flow above is ADR-0021. Besides the shared checks, every pull request runs **`web-editor`**:
vitest, and that the committed editor bundle matches its source. It is a required check too
(`extra_checks` in `.conventions.toml`).

No approvals are required: on a one-person project GitHub would not let you approve your own pull
request. Review conversations must be resolved, and the rules apply to administrators too. `dev` is the
default branch, so `gh pr create --fill` targets it already. Merge with a merge commit:

```bash
gh pr merge --merge --delete-branch
```

## Releases

Merging into `dev` deploys nothing. Production changes only when a release reaches `main`, and
`.github/workflows/release.yml` does the steps (it needs the repository secret `RELEASE_TOKEN`):

1. **Actions → release → Run workflow** (or `gh workflow run release.yml -f bump=auto`). It works out
   the next version from the commits since the last tag, writes it into `pyproject.toml`, `uv.lock` and
   `src/doomtp_bot/__init__.py` on a `release/x-y-z` branch, and opens "Release vX.Y.Z" into `main`.
   Set `bump` to `patch`, `minor` or `major` to override it.
2. Merge that pull request with a merge commit. The `conventions / version` check has already made
   sure the version went up.
3. The `finish` job tags the merge commit `vX.Y.Z` and publishes the GitHub release. CI builds the
   tag again and publishes `:vX.Y.Z` beside `:main`; check that the tag's run passed, because that
   image is what a rollback needs.
4. `finish` also opens "Bring release vX.Y.Z back into dev". Merge it straight away: the release's
   merge commit exists only on `main`, and the next release pull request shows as out of date without
   it. If `dev` moves first, that pull request can't be updated, because its head is the protected
   `main`. Merge `main` into a branch cut from `dev` instead:

   ```bash
   git switch -c chore/back-merge-main origin/dev && git merge --no-ff origin/main
   ```

To roll back, run `scripts/rollback.sh` on the server with the previous `:vX.Y.Z`, which downgrades the schema
and pins `BOT_IMAGE` to it (README).

**A hotfix** branches from `main` (`hotfix/…`), raises the version itself and merges into `main`:

```bash
uvx --from "git+https://github.com/vEXOULZ/conventions@$(sed -n 's/^version *= *"\(.*\)"/\1/p' .conventions.toml)" conventions version set X.Y.Z
```

Its merge is released like any other: `finish` tags it and opens the pull request that brings `main`
back into `dev`, so `dev` never loses the fix.

## Before you push

The suite runs against a real Postgres rather than a stand-in (ADR-0014), so start one first. It keeps
its data in a tmpfs and is meant to be thrown away:

```bash
docker compose --profile test up -d postgres-test
```

Point `TEST_DATABASE_URL` elsewhere if you would rather use your own server. Each run creates a database
of its own and drops it at the end, and each test rolls back, so nothing accumulates.

```bash
uv run ruff check && uv run ruff format --check && uv run mypy
```

```bash
uv run pytest -q
```

Some tests need an external tool and skip without it, saying what to install. Two backup tests need a
`pg_dump` at least as new as the server — it refuses to dump a newer one — and the EventSub handshake
tests need the [Twitch CLI](https://dev.twitch.tv/docs/cli/). A dev box need not have either. CI must,
so it runs pytest with `--require-tools`, which turns those skips into failures: a skipped test in CI
would mean nothing is testing backups or the handshake while the run still goes green. Pass the flag
yourself to check that a machine has everything.

CI runs those plus the web editor's tests, the committed-bundle check, the grammar and railroad diagram
checks (`uv run python scripts/check_railroad.py`, `uv run python scripts/render_railroad.py --check`),
and a Docker build. Nothing merges that CI hasn't agreed with.

## Schema changes

A schema change is an Alembic revision in `src/doomtp_bot/storage/migrations/<bot|chatlog>/versions/`
(ADR-0022). Copy the newest one there. The id is the next four-digit number, and `down_revision` is the
one before it. Write the SQL in `upgrade()` with `op.execute`, and write a `downgrade()` that undoes it
exactly: `tests/test_schema.py` runs every revision down and back up, and compares the schema at each
step. Keep changes additive where you can, because the old bot is still running while the migrate step
runs. Drop a column one release after the code stops using it.

## Commits

Write the subject as what the commit does, in the imperative and under about 60 characters — the log
reads as a list of changes, not a list of areas touched. The body says what was wrong before and why
this is the fix, wrapping at 100 columns. Close an ADR action item by name (`Closes ADR-0009 item 2`) so
the decision record and the code agree about what is done.

## Decisions

Anything that will be hard to reverse, or that someone will later ask "why is it like this?" about, goes
in an ADR under [docs/adr/](docs/adr/) before the code does — context, the options weighed, the decision,
the consequences, and the action items it creates. [docs/roadmap.md](docs/roadmap.md) tracks those items.
