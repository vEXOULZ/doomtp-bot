# ADR-0018: Command language v2 — `$` fields, brackets, expressions and numbered errors

**Status:** Accepted — 2026-09-28
**Date:** 2026-09-28
**Deciders:** Project owner

## Context

The default command set was reviewed as a whole for the first time (ADR-0019). Several of the commands it
wants cannot be written in the language as it stands (spec version 1.1):

- **No arithmetic or comparison.** Branching needs a command that fails on purpose, and `quote 3` vs
  `quote add …` cannot be told apart without a primitive.
- **Dots walk into values.** `{channel.stats.kills}` reads a map key, so a key named `code` or `data`
  collides with result fields. A key with a space or an emoji can't be read at all.
- **Bot fields and variables share one name space.** `channel.title` can't be a variable because the bot
  might supply a `title` field, so `runtime/namespaces.py` keeps a growing reserved list.
- **`{1}` means "the first result"**, so the number 1 can't be written inside a placeholder.
- **`>` and `>>` store**, which clashes with `>` as a comparison.
- **Every language failure is code 2**, so a script can't tell a missing value from a full list.

These changes affect each other, and each one changes how stored bodies parse. They ship as **one syntax
version bump** with one rewrite of stored bodies, not as a series of breaking changes.

## Decision

### Names and values

| Thing | Version 1.1 | Version 2 |
|---|---|---|
| Bot-supplied field | `{chatter.display}` | `{$chatter.display}` |
| Variable | `{channel.deaths}` | unchanged |
| Inside a value | `{channel.stats.kills}`, `{channel.log.0}` | `{channel.stats[kills]}`, `{channel.log[-1]}`, `{x["best run"]}`, `{channel.quotes[arg.1]}` |
| Result data | `{1}`, `{1.name}`, `{_}` | `{_1}`, `{_1[name]}`, `{_}`; `{1}` is the number 1 |
| Store / append | `> ns.x` / `>> ns.x` | `-> ns.x` / `--> ns.x` |
| Cron timer | `timer cron 0 18 * * fri => expr` | `timer cron "0 18 * * fri" expr` |

- **`.` walks names the bot defines** (namespaces, a variable's name, bot fields, and the result fields
  `code`, `message` and `data`). **`[ ]` walks into values.** No map key can collide with a bot name.
- **Brackets:** a bare word is a literal key, a quoted string is any key, and an integer is an index.
  Negative indexes count from the end, down to `-len`. Anything with a namespace root, a result ref or
  an operator is an expression.
- **`$` marks everything the bot supplies**, so new fields never collide with a variable. What stays
  reserved is a short closed list: the structural segments (`chatter`, `channel` where they nest) and the
  result fields `data code message public root`. `@` was rejected as the sigil because it already means
  a personal alias and is Twitch's mention sign.
- **Types `list` and `map`:** a single placeholder whose value is a collection passes through unchanged.
  Otherwise JSON text is parsed. Accessors are `:len`, `:keys` and `:values`.
- **Path writes:** `!var set/incr/del ns.name[path]` and `-> ns.name[path]`. A missing map on the way is
  created. `!var pop ns.name [index]` is for lists only, uses Python indexes, and defaults to `-1`.

### Expressions

- **Inline:** `{channel.deaths * 2}`, `{arg.1 == "add"}`. Our own parser, not `eval`, with limits on
  length, depth and operation count.
- **Operators are commands:** `a + b` evaluates as `add a b`. The operator commands are `add sub mul div
  idiv mod neg eq ne lt le gt ge in not and or`, and they can be called directly.
- **Precedence**, highest first, following Python: literals, refs and brackets; accessors and casts;
  unary `-`; `* / // %`; `+ -`; comparisons and `in`, chained as in Python (`1 < x < 3`); `not`; `and`;
  `or`; `??`.
- **Comparisons** are case-sensitive. They compare numerically when both sides parse as numbers, and as
  text otherwise.
- **Truthiness** follows Python: `false`, `0`, `0.0`, `""`, `[]` and `{}` are false. **Missing is not
  false:** it fails with `E_MISSING`, unless `??` gives a fallback.
- **`{!cmd args}`** runs one invocation and gives its data, or its message when the data is null. The
  `!` is fixed whatever the channel prefix is. Permissions, cooldowns and budgets apply as for a stage,
  with a depth limit of 3.
- **`check expr`** succeeds when the value is truthy and fails with 1 when it is falsy. **`ifelse cond
  ( then ) [ ( else ) ]`** is a special form: both branches are parsed and checked up front, but only
  the chosen one runs. A missing condition fails with `E_MISSING` and runs neither branch.
- **Bare expression lines:** `🏜1 + 3` runs the implicit command `calc`. This applies when the first
  chunk is a number, `{`, `(` or `-` and the line has an operator. Purely numeric command names become
  illegal.

### Error codes

The code range widens from 0–255 to **0–1023**. Commands keep **1–99**, and **4 is free** (the spec's
"1–3 or 5–99" had no recorded reason). Everything from 100 up belongs to the runtime.

| Range | Contents |
|---|---|
| 1–99 | commands and `fail`; 2 stays "bad arguments" for a command's own parameters |
| 124–130 | unchanged: timeout, upstream limited, denied, unknown, cooldown, cancelled |
| 200–299 | language errors |
| 300–399 | storage errors |
| the rest of 100–1023 | free, for example 400–499 for an HTTP primitive (ADR-0019) |

| Error | Code | Error | Code |
|---|---|---|---|
| `E_EXPR_SYNTAX` | 200 | `E_INDEX` | 220 |
| `E_UNKNOWN_OP` | 201 | `E_KEY` | 221 |
| `E_EXPR_TOO_LONG` | 202 | `E_NOT_A_LIST` | 222 |
| `E_EXPR_TOO_DEEP` | 203 | `E_NOT_A_MAP` | 223 |
| `E_EXPR_BUDGET` | 204 | `E_EMPTY` | 224 |
| `E_SUBST_DEPTH` | 205 | `E_LIST_FULL` | 300 |
| `E_MISSING` | 210 | `E_QUOTA` | 301 |
| `E_TYPE` | 211 | `E_VALUE_TOO_BIG` | 302 |
| `E_DIV_ZERO` | 212 | | |
| `E_OVERFLOW` | 213 | | |

The name appears in the message and in `explain`, so a script can branch on the number:
`var pop channel.queue || ifelse {_.code == 224} ( echo queue is empty ) ( echo {_.message} )`. The new
codes have no special behaviour: they are not silent and fire no callback. A missing placeholder becomes
210 instead of 2, which `||` does not notice.

### Migration

The syntax version rises to 2. One AST rewrite converts every stored body (custom commands, triggers,
callbacks and templates): dots past a variable or field become brackets, `{1}` becomes `{_1}`, reserved
fields gain `$`, `>`/`>>` become `->`/`-->`, and cron `=>` becomes a quoted spec. It also lists every
body that compares a code with 2. For one release, a typed line in the old form fails with the new
spelling in the message.

## Options Considered

### Option A: extend version 1.1 feature by feature
**Pros:** each step is small. **Cons:** five breaking changes to stored bodies instead of one, and the
dot/value collision stays.

### Option B: one version bump with a rewrite (chosen)
**Pros:** one migration, and every rule above holds from the same day. **Cons:** a large change to the
parser, and doomtp-web's highlighter has to follow in the same window.

### Option C: keep all language errors at code 2 with names only
**Cons:** a script can't react to one error without parsing message text. Rejected on 2026-09-28.

## Consequences

- **Easier:** branching, arithmetic and collections without new primitives. Quotes can be rebuilt as a
  derived pack (ADR-0019).
- **Harder:** the grammar, railroad diagrams, corpus, highlighter and spec all change together.
- **Sharp edge:** `executor.py` still turns a *returned* code ≥ 100 into 1. Language errors must be
  *raised* as `CommandError`, as `!var` already does for 126.

## Action Items

1. [ ] Codes: widen `Result` to 0–1023, add the `E_*` enum beside `Code` in `runtime/result.py`, free
   code 4 in `fail`, and rewrite spec §6.2. Raise `E_LIST_FULL` where `-->` silently drops items today.
2. [ ] `$` fields and the closed reserved list (`runtime/namespaces.py`, `docs/namespaces.md`,
   `GET /api/v1/namespaces`).
3. [ ] Brackets, `list`/`map` types, accessors, path writes and `!var pop`.
4. [ ] Result refs `{_N}`, `->`/`-->`, and the quoted cron spec.
5. [ ] The expression evaluator, the operator commands, `check`, `ifelse` and `calc`.
6. [ ] `{!cmd}` substitution and bare expression lines.
7. [ ] Syntax version 2, and the rewrite of stored bodies with a golden test over a dev dump.
8. [ ] Spec, grammar, railroad diagrams and corpus, plus a doomtp-web PR for the highlighter.
