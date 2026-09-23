# Contributing

## Set up the hooks once per clone

```bash
git config core.hooksPath .githooks
```

Git does not carry hooks in a clone, so this is the one step nothing can do for you. Without it the
branch rules below are only enforced in CI, which is a slower way to hear about a typo.

## Branches

**`main` is merge-only.** The `pre-commit` hook refuses a commit made on `main`, `master` or `develop`.
Work on a branch and merge it:

```bash
git switch -c feature/what-you-are-doing
```

Branch names follow [Conventional Branch](https://conventional-branch.github.io/): `<type>/<description>`,
where the description is lowercase letters, digits and single hyphens.

| Type | For |
|------|-----|
| `feature/` | a new capability — `feature/publish-packs-globally` |
| `bugfix/` | a fix — `bugfix/issue-42-cooldown-off-by-one` |
| `hotfix/` | a fix that can't wait for the usual path — `hotfix/token-refresh-loop` |
| `release/` | preparing a version — `release/1-2-0` |
| `chore/` | dependencies, tooling, docs, anything with no behaviour change — `chore/bump-twitchio` |

A ticket number is just another word in the description. `.githooks/check-branch-name.sh <name>` says
whether a name passes, and CI runs the same script against the branch a pull request comes from.

Concluding a merge that hit conflicts is a commit on `main`, and the hook lets that one through — the
rule is about where work starts, not where it lands. `git commit --no-verify` skips the hook entirely.
It exists for the day you need it, not for the day you are in a hurry.

GitHub enforces the same rule on the server, because a local hook protects only the person who
installed it. `main` on [github.com/vEXOULZ/doomtp-bot](https://github.com/vEXOULZ/doomtp-bot) accepts
changes only through a pull request, and a pull request merges only when all five CI jobs are green on a
branch that is up to date with `main`:

| Check | What it guards |
|-------|----------------|
| `branch-name` | the Conventional Branch rule above, the same script the hook runs |
| `python (3.11)`, `python (3.12)` | lint, types, the suite against Postgres 17, grammar and railroad checks |
| `web-editor` | vitest, and that the committed editor bundle matches its source |
| `docker` | the image builds and the server compose files parse together |

No approvals are required — on a one-person project GitHub would not let you approve your own pull
request, and requiring one would only mean switching the rule off to merge. Review conversations must be
resolved. The rule applies to administrators too, so there is no quiet way around it; changing that is a
visible settings change, which is the point. Force-pushes and deleting `main` are refused.

The day-to-day flow is therefore: branch, push the branch, open a pull request, merge when it is green.

```bash
gh pr create --fill
```

```bash
gh pr merge --merge --delete-branch
```

Merge commits, not squashes: the history reads as branches landing, which is how it was written.

## Before you push

The suite runs against a real Postgres rather than a stand-in (ADR-0014), so start one first. It keeps
its data in a tmpfs and is meant to be thrown away:

```bash
docker compose --profile test up -d postgres-test
```

Point `TEST_DATABASE_URL` elsewhere if you would rather use your own server. Each run creates a database
of its own and drops it at the end, and each test rolls back, so nothing accumulates.

```bash
.venv/Scripts/python -m ruff check src tests scripts && .venv/Scripts/python -m ruff format src tests scripts
```

```bash
.venv/Scripts/python -m mypy && .venv/Scripts/python -m pytest -q
```

Two backup tests need a `pg_dump` at least as new as the server — it refuses to dump a newer one —
and skip without it, naming the package to install. A dev box need not have it. CI must, so there
the same condition fails the run instead: a skipped backup test in CI would mean nothing is testing
backups while the run still goes green.

CI runs those plus the web editor's tests, the committed-bundle check, the grammar and railroad diagram
checks, and a Docker build. Nothing merges that CI hasn't agreed with.

## Commits

Write the subject as what the commit does, in the imperative and under about 60 characters — the log
reads as a list of changes, not a list of areas touched. The body says what was wrong before and why
this is the fix, wrapping at 100 columns. Close an ADR action item by name (`Closes ADR-0009 item 2`) so
the decision record and the code agree about what is done.

## Decisions

Anything that will be hard to reverse, or that someone will later ask "why is it like this?" about, goes
in an ADR under [docs/adr/](docs/adr/) before the code does — context, the options weighed, the decision,
the consequences, and the action items it creates. [docs/roadmap.md](docs/roadmap.md) tracks those items.
