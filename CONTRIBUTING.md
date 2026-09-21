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

Once the repository has a remote, turn on branch protection for `main` as well — require a pull request,
and disallow direct pushes. A local hook protects the person who installed it; the setting protects the
branch.

## Before you push

```bash
.venv/Scripts/python -m ruff check src tests scripts && .venv/Scripts/python -m ruff format src tests scripts
```

```bash
.venv/Scripts/python -m mypy && .venv/Scripts/python -m pytest -q
```

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
