# ADR-0005: Command runtime — AST, all-or-nothing preflight, three-part Result

**Status:** Accepted, revision 2 (implemented; see Action Items) — 2026-09-17
**Date:** 2026-09-16
**Deciders:** Project owner

## Context

Commands compose with pipes and chain operators, including `|`, `&&`, `||`, grouping and variable writes (see [command-language-proposal.md](../command-language-proposal.md)). **The syntax isn't final.** Commands must return:

- an **exit code** (0 = success)
- a **formatted message**, shown only if it's the final result
- **structured data** that later commands can address (`!weather | echo "it's {1.celsius}C"`)

The runtime also has to:

- Enforce toggles, permissions and cooldowns for every command, including the commands inside custom commands.
- Support `!explain` dry runs.
- Accept triggers from sources other than chat: events, timers, listeners and the API.
- Cancel cleanly when moderators act on the triggering message.

## Decision

1. **Pipeline:** Lexer/Parser (versioned, swappable) → **AST** → **Resolver** → **Preflight** → **Executor** → **Outbox**.
2. **AST nodes:** `And`, `Or`, `Pipe`, `Group`, `Store(target, append)`, `Invocation(name, args, span, index)`, `Placeholder(root, path, type, fallback)`. The AST is the stable contract between syntax and runtime. Placeholder roots must come from the [namespace registry](../namespaces.md).
3. **Result:** `Result(code: int, message: str | None, data: JSON)`. Codes follow shell conventions: 0, 1, 2, 3, 124, 125, 126, 127, 128 (cooldown) and 130 (cancelled).
4. **Commands are declared with a full `CommandSpec`:**
   - name, aliases, module
   - summary, description
   - params, declared **positionally as `arg.1`, `arg.2`, `arg.3+`** with name, type (from namespaces.md §2), required, default, min/max/choices and description. The same shape is used for custom commands.
   - input mode, data schema, examples
   - required role, default cooldowns, log level, side effects, required capabilities

   The same spec generates usage text, `!help`, `GET /api/v1/commands` and the public docs page.
5. **Commands return Results and never send chat themselves.** Only the Outbox sends.
6. **Preflight is all-or-nothing:**
   - Every `Invocation` is resolved and checked **before any runs**, including branches that may be skipped and the inner commands of custom commands, expanded up to depth 3.
   - Placeholder references are validated statically. For example, `{3}` can't be referenced before command 3 can have run.
7. **Operator semantics:**
   - `|` **stops** on a non-zero code and returns the failing result.
   - `&&` / `||` follow exit codes. `||` is the failure handler.
   - **There is no `;`.** It's reserved and rejected by the parser.
   - A group's result is the last executed result.
   - `>` / `>>` store data, or the message when there is no data, **only on success**, and pass the result through.
8. **Final output:** only the message of the last executed command is sent, and only if it isn't empty.
9. **Sentinel commands** in the non-toggleable `core` module:
   - `true`: code 0, passes `{_}` through.
   - `false`: code 1.
   - `default <value>`: code 0, value as message and data.
   - `fail [code] <message>`: fails with that code and message.
   - `echo`.

   Idiom: `!cmd || true` makes a command optional.
10. **Argument validation:** declared param types, and inline `{arg.N:type}`, are validated before the command body runs. Failure returns code 2 with usage text generated from the spec.
11. **Cancellation:** the executor checks the ModerationIndex before and between stages. Commands with side effects check it again right before acting. If cancelled, the run gets code 130, variable writes are discarded and queued sends are dropped.
12. **Variable writes are buffered** and committed atomically at the end of the run unless it was cancelled (ADR-0010).
13. **Limits:**
    - 8 invocations
    - 3 s per stage, 6 s in total
    - 4 KB of data per stage
    - 2 chat messages of output
    - nesting depth 3, with cycle detection
    - output never re-parsed as a command
14. **`!explain`** runs the parser, resolver and preflight, then prints the report. `--run` also executes, with writes and sends disabled.
15. **Logging:** each command's `log_level` (`off | errors | output | invocations | all`) can be overridden per channel and controls `command_runs` rows.

## Options Considered

### Option A: AST runtime with a three-part Result (chosen)
**Pros:**
- Syntax can change without touching commands.
- Structured data enables `{1.celsius}`.
- Exit codes make `&&`/`||` natural.
- Atomic preflight.
- `!explain`, triggers and the API reuse everything.

**Cons:** More upfront structure. Every command must declare a spec.

### Option B: String-rewriting dispatcher (text in, text out)
**Pros:** Fast to prototype.
**Cons:** No structured data and no exit codes, so it fails requirement F4. Half-executed chains. Grammar changes touch every command.

### Option C: Embedded scripting language (Lua or a Python sandbox)
**Pros:** Expressive.
**Cons:** Hard to sandbox. Per-call policy checks become opaque. Hostile syntax for chat.

## Trade-off Analysis

Requirement F4 (code, message and data) together with the operator set essentially requires A. Its cost is mostly the spec declarations, and those are needed anyway for the self-documenting help and API requirement (F9). So the upfront structure pays for itself twice.

## Consequences

- **Easier:**
  - New operators and syntax changes.
  - Triggers from any source.
  - Docs generation.
  - Testing, since commands are pure `(ctx, args, stdin) → Result`.
- **Harder:**
  - Every command author must describe params, data and examples.
  - Static placeholder validation needs a data-flow pass over the AST.
- **Revisit:**
  - If conditionals, loops or arithmetic become syntax, add a step budget.
  - The deferred `?` shorthand (language proposal §6).

## Action Items

1. [x] Add `runtime/result.py`, `runtime/spec.py` (`CommandSpec`, `Param`, `Example`) and `lang/ast.py`.
2. [x] Write the parser per the normative grammar (spec Appendix C), with a golden corpus (text → AST) including the chat-collision cases.
3. [x] Build the resolver and preflight with static placeholder checks. *(built-ins only; publication and personal lookups arrive with ADR-0009)*
4. [x] Build the executor: operator semantics, buffered writes, moderation checkpoints, limits and timeouts.
5. [x] Add `!explain` and `POST /api/v1/explain`. *(`--run` evaluates with the write buffer discarded and no cooldown commits; the caller sends nothing)*
6. [x] Generate `!help` and `GET /api/v1/commands` from the specs.
