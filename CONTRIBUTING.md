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
- A release is a pull request from `dev` (or a `release/*` branch) into `main`, then a `vX.Y.Z` tag on
  `main`.
- A `hotfix/*` branch starts from `main` and merges into `main`; `main` then merges back into `dev` so
  `dev` never loses it.
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

Merging into `dev` deploys nothing. Production changes only when a release reaches `main`:

1. Bump the version on `dev` through a `release/x-y-z` pull request: `version` in `pyproject.toml` and
   `__version__` in `src/doomtp_bot/__init__.py`.
2. Open the release pull request from `dev` into `main` and merge it with a merge commit.

   ```bash
   gh pr create --base main --head dev --title "Release vX.Y.Z"
   ```

3. Tag it once the release pull request has merged, never before. CI builds the tag again, checks
   that it matches the package version and publishes `:vX.Y.Z` beside `:main`. Check that the tag's
   run passed: a tag on a `main` without the version bump publishes nothing, and then there's nothing
   to roll back to (v0.2.0's tag did exactly that).

   ```bash
   gh release create vX.Y.Z --target main --generate-notes
   ```

4. Merge `main` back into `dev`. The release's merge commit exists only on `main`, and `main` accepts
   a pull request only from a branch that is up to date with it, so without this the next release
   pull request shows as out of date.

   ```bash
   gh pr create --base dev --head main --title "Bring release vX.Y.Z back into dev"
   ```

To roll back, set `BOT_IMAGE` on the server to the previous `:vX.Y.Z` and run the update unit (README).

**A hotfix** branches from `main` (`hotfix/…`), merges into `main`, and then `main` merges back into
`dev` in a pull request of its own, so `dev` never loses the fix:

```bash
gh pr create --base dev --head main --title "Bring hotfix back into dev"
```

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
