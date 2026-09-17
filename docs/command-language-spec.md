# doomtp-bot Command Language — Specification

**Version:** 1.0.0-draft · **Status:** Draft for review · **Date:** 2026-09-16
**Supersedes:** [command-language-proposal.md](command-language-proposal.md) (kept for rationale)
**Normative companions:** [namespaces.md](namespaces.md) (namespaces, types, reserved names) and [variable-access-matrix.md](variable-access-matrix.md) (variable permissions)
**Runtime design:** [ADR-0005](adr/0005-command-pipeline-runtime.md)

---

## 0. Conventions

- The key words **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT** and **MAY** are used as in RFC 2119.
- Grammar is written in ISO-style EBNF. `WS` is defined in §2.2.
- "Implementation" means the doomtp-bot lexer, parser, resolver, preflight and executor.
- Examples use `!` as the channel prefix.
- Configurable limits are written as `LIMIT_NAME (default)`. Channels MAY lower limits but MUST NOT raise them above the global configuration.

## 1. Scope and contexts

An **expression** is text that the implementation parses and evaluates to a single **Result** (§6.1). Expressions occur in these **contexts**:

| Context | Source | Prefix on first command | Available namespaces (§7.2) |
|---------|--------|-------------------------|-----------------------------|
| **Line** | A chat message | required | results, variables (no `publisher.*`), context |
| **Body** | A custom command's stored body | optional | + `arg`, `args`, `publisher.*`, `cmd` |
| **Trigger** | Redemption, event or timer expression | optional | + `event` (no `publisher.*`) |
| **Listener** | A regex listener expression | optional | + `match` (no `publisher.*`) |
| **Callback** | `on_cooldown` / `on_denied` expression | optional | + `cooldown` or `denied` |
| **Explain** | The raw tail of `!explain` / `POST /api/v1/explain` | as in the context being explained | as in the context being explained |

---

## 2. Input processing and lexical structure

### 2.1 Pre-processing (Line context)

Before lexing a chat message, the implementation MUST apply these steps in order:

1. **Strip invisible padding.** Remove leading and trailing characters in: U+E0000 (the tag character some chat clients append to bypass duplicate-message filters), U+200B–U+200D, U+2060 and U+FEFF. Characters *inside* the text MUST NOT be changed.
2. **Strip the reply mention.** If the message is a reply (it has reply metadata) and the text begins with `@<parent display name>` or `@<parent login>` (case-insensitive) followed by WS, remove that mention and the whitespace. *(Verified live 2026-09-17: Twitch prefixes replies with the parent's **display name**, e.g. `@vexouLz pong`. Display names can differ from logins beyond case, so both are accepted.)*
3. **Trim** leading and trailing WS.
4. **Detect the command.** The text is an expression only if it matches `line_start` (§3.1): the channel prefix immediately followed by a name character, optionally preceded by standalone `(` tokens. Otherwise the message is not a command, and it goes to listeners only.

Other Unicode normalization (NFC/NFKC, case folding) MUST NOT be applied to argument text. Command names are case-folded (§2.6).

**The channel prefix** MUST be 1–3 characters, contain no WS, not start with `/` or `.`, and not contain `{`, `}`, `"`, `\`, `|`, `&`, `>`, `(` or `)`. Its **last character MUST NOT be a name character** (`[A-Za-z0-9_@-]`), so the boundary between prefix and name is unambiguous.

### 2.2 Whitespace

`WS` is one or more characters with the Unicode `White_Space` property. Consecutive WS is equivalent to a single space, except inside quoted strings (§2.4) and in `+raw` captures (§7.3).

### 2.3 Chunks and operator tokens

The input is split into **chunks** at unquoted, unescaped WS. A chunk MAY contain several quoted segments, unquoted segments, escapes and placeholders, and these are concatenated (`ab"c d"e` is the single argument `abc de`).

A chunk is an **operator token** only if it consists solely of one of the following unquoted, unescaped ASCII sequences:

| Token | Meaning |
|-------|---------|
| `\|` | pipe |
| `&&` | and |
| `\|\|` | or |
| `>` | store |
| `>>` | append |
| `(` | group open |
| `)` | group close |
| `;` | **reserved.** MUST produce parse error `E_RESERVED_OPERATOR`. |

Every other chunk is a **word**. So `a|b`, `->`, `>:(`, `;)`, `(lol)`, `&` and `|||` are all ordinary words.

### 2.4 Quoted strings

- `"` starts a quoted segment, and the next unescaped `"` ends it.
- Inside a quoted segment, WS and operator characters are literal, while escapes (§2.5) and placeholders (§2.7) are still processed.
- `'` has no special meaning anywhere.
- A quoted segment left unterminated at end of input MUST produce `E_UNTERMINATED_QUOTE`.
- `""` is a valid empty argument.

### 2.5 Escapes

- `\` followed by any character yields that character literally, and removes any special meaning it has (`\"`, `\\`, `\{`, `\}`, `\|`).
- A chunk made only of an escaped operator (`\|`, `\&&`, `\>`, `\(`) is a word.
- `\` at end of input yields a literal `\`.
- There are no other escape sequences: `\n` is the literal `n`.

### 2.6 Command names

```ebnf
name        = [ "@" ] , name_char_1 , { name_char } ;       (* max 32 chars excluding "@" *)
name_char_1 = "a".."z" | "0".."9" ;
name_char   = name_char_1 | "_" | "-" ;
```

- Names MUST be ASCII-case-folded to lowercase before matching.
- `@` forces resolution to the invoker's **personal alias** (§5.1).
- A name MUST be literal: a placeholder in the name position MUST produce `E_DYNAMIC_NAME`.
- Inside an expression, a command after an operator MAY carry the channel prefix. If it does, the prefix is removed before name matching.

### 2.7 Placeholders

```ebnf
placeholder = "{" , [ WSo ] , ref , [ ":" , type ] , [ WSo , "??" , WSo , fallback ] , [ WSo ] , "}" ;
ref         = root , { "." , segment } ;
root        = "_" | index | ident ;                        (* must be a registered root, §7.1 *)
index       = digit_nz , { digit } ;                        (* 1-based result index *)
segment     = ident | digits | digits , "+" | digits , "+raw" ;
ident       = ( letter | "_" ) , { letter | digit | "_" } ;  (* data keys are case-sensitive *)
type        = type_name | "choice(" , choice_item , { "," , choice_item } , ")" ;
type_name   = "str" | "int" | "float" | "bool" | "range" | "duration" | "user" | "url" ;
fallback    = { fb_char | escape | placeholder } ;         (* may be empty *)
fb_char     = ? any character except "{", "}", "\" ? ;
WSo         = ? optional WS ? ;
```

- An unescaped `{` always starts a placeholder, both inside and outside quotes.
- If the placeholder is malformed, or its root isn't registered in namespaces.md, parsing MUST fail with `E_BAD_PLACEHOLDER`. The error message SHOULD suggest `\{`.
- An unescaped `}` outside a placeholder is literal.
- Fallbacks MAY nest placeholders. Nesting depth MUST NOT exceed `MAX_PLACEHOLDER_NESTING (4)`.
- The fallback text is trimmed of leading and trailing WS.
- **Indexing:** result indexes (`{1}`) and argument positions (`{arg.1}`) are **1-based**. List indexes inside data (`{1.items.0}`) are **0-based**.

---

## 3. Syntax

### 3.1 Grammar

```ebnf
line        = line_start_check , expr , EOF ;          (* Line context *)
body        = expr , EOF ;                             (* all other contexts *)

expr        = logical ;
logical     = pipeline , { WS , ( "&&" | "||" ) , WS , pipeline } ;
pipeline    = stage , { WS , "|" , WS , stage } ;
stage       = primary , [ WS , ( ">" | ">>" ) , WS , varref ] ;
primary     = group | invocation ;
group       = "(" , WS , expr , WS , ")" ;
invocation  = [ prefix ] , name , { WS , word } , [ raw_tail ] ;
word        = chunk ;                                  (* §2.3, not an operator token *)
raw_tail    = WS , ? remainder of input, see §3.3 ? ;
varref      = var_ns , "." , var_name ;
var_ns      = "chatter" | "channel" | "channel.chatter"
            | "publisher" | "publisher.chatter" | "publisher.channel" | "publisher.channel.chatter" ;
var_name    = "a".."z" , { "a".."z" | "0".."9" | "_" } ;  (* max 32; not reserved, namespaces.md §5 *)

line_start_check = { "(" , WS } , prefix , name_char_1 ;   (* lookahead only *)
```

- The `WS` around operator tokens is implied by the chunking rule (§2.3). The grammar writes it out for clarity.
- A `varref` MUST be literal. Placeholders in it MUST produce `E_DYNAMIC_VARREF`.

### 3.2 Precedence and associativity

From tightest binding to loosest:

| Level | Construct | Associativity |
|-------|-----------|---------------|
| 1 | `( … )` group | — |
| 2 | `>` / `>>` store, applied to one primary | — (at most one store per stage) |
| 3 | `\|` pipe | left |
| 4 | `&&`, `\|\|` | left, equal precedence |

Examples:
- `a && b | c > x || d` parses as `((a && (b | (c > x))) || d)`. The store applies only to `c`.
- To store a pipe's result, group it: `( b | c ) > x`.

### 3.3 Raw-tail commands

A command's spec MAY declare `raw_tail_from = N`. For such a command:

1. Lexing proceeds normally for arguments `1..N-1`.
2. Everything after the WS that follows argument `N-1` becomes **one raw argument**, up to end of input. It is taken verbatim: operators, escapes, quotes and placeholders are not processed.
3. If the raw text begins and ends with `"` and contains no other unescaped `"`, those two quotes are removed.
4. A raw-tail command MUST be the only invocation in its expression. Otherwise parsing fails with `E_RAW_TAIL_POSITION`.

Built-in raw-tail commands in v1:

| Command | Raw tail from |
|---------|---------------|
| `explain` | 1 |
| `cc add`, `cc edit` | 3 (after the subcommand and name) |
| `callback set` | 4 (after the subcommand, kind and scope) |
| `trigger set` | 3 |

Example: `!cc add roll !random 1-{arg.1:int ?? 20} | echo {chatter.name} rolled {1}` stores the body exactly as typed.

### 3.4 Parse errors

Parse errors produce a Result with **code 2** and message `parse error: <E_CODE> at <column>: <hint>`.

- The message is **only sent** if the first invocation of the line resolves to a command the invoker is permitted to run (§5, §6.6).
- Otherwise the line is silently ignored. This keeps typos in normal chat, like `!` followed by text, from producing bot noise.

| Code | Condition |
|------|-----------|
| `E_UNTERMINATED_QUOTE` | `"` not closed |
| `E_BAD_PLACEHOLDER` | malformed `{…}`, unknown root, bad type, nesting too deep |
| `E_RESERVED_OPERATOR` | `;` used as an operator |
| `E_UNEXPECTED_OPERATOR` | operator where a command is expected, e.g. `\| echo`, `a && && b` |
| `E_MISSING_OPERAND` | expression ends with an operator (`!a \|`, `!a >`, `(` at end of input) |
| `E_UNBALANCED_GROUP` | `(` without `)`, or the reverse |
| `E_BAD_NAME` | a command position holds something that isn't a valid name (e.g. `!echo"x"`, where a name must be followed by whitespace or end of input) |
| `E_DYNAMIC_NAME` / `E_DYNAMIC_VARREF` | placeholder in a name or store target |
| `E_BAD_VARREF` | store target isn't a valid `varref` |
| `E_RAW_TAIL_POSITION` | raw-tail command combined with other invocations |
| `E_TOO_LONG` | input over `MAX_EXPR_CHARS` (2000 for bodies; chat lines are capped at 500 by Twitch) |
| `E_INTERNAL` | the parser failed without a named error. This is a bug (Appendix C.8). |

`( )` produces `E_UNEXPECTED_OPERATOR`, because the `)` sits where a command is expected.

**Appendix C is the normative parsing definition.** The grammar in §3.1 is explanatory.

---

## 4. Abstract syntax tree

The parser MUST produce this AST. It is the stable contract with the runtime, and later syntax versions MUST map onto it or extend it.

```python
Node = And | Or | Pipe | Group | Store | Invocation

And(left: Node, right: Node)
Or(left: Node, right: Node)
Pipe(left: Node, right: Node)
Group(inner: Node)
Store(inner: Node, target: VarRef, append: bool)
Invocation(index: int,               # 1-based, pre-order source order within the scope (§6.4)
           name: str, personal: bool,
           args: list[Arg], raw_tail: str | None,
           span: tuple[int, int])
Arg = list[Text | Placeholder]       # concatenated parts of one chunk
Text(value: str)
Placeholder(root: str, path: list[str], type: TypeSpec | None,
            fallback: list[Text | Placeholder] | None, span: tuple[int, int])
VarRef(namespace: str, name: str)
```

Each AST MUST record `syntax_version = "1.0"`. Custom command versions (ADR-0009) MUST store the syntax version they were parsed with.

---

## 5. Static semantics (resolution and preflight)

After parsing, and **before any invocation executes**, the implementation MUST run the steps below over the whole AST. That includes branches that might not run, and custom command bodies expanded recursively up to `MAX_CC_DEPTH (3)`.

### 5.1 Name resolution

For each `Invocation` in channel C for invoker U:

1. If `personal` is set (`@name`): U's personal alias `name`, otherwise unresolved.
2. A built-in command or built-in alias named `name` that is enabled in C.
3. A publication named `name` in C with status `active`.
4. U's personal alias `name`.
5. Otherwise unresolved. Result code **127**.

Custom command names MUST NOT equal a sentinel name (§8) or a `core_admin` command name.

### 5.2 Preflight checks

For each resolved invocation, in this order:

| # | Check | Failure code |
|---|-------|--------------|
| 1 | Toggles and channel capabilities (ADR-0006, ADR-0007) | 127 (a disabled command behaves as unknown) |
| 2 | Permission: invoker rank vs. required role or allowed roles | 126 |
| 3 | Cooldowns: tier bucket **and** user bucket both expired | 128 |
| 4 | Input mode: a command with `input=NONE` must not be the right operand of `\|` | 2 (`E_INPUT_NOT_ACCEPTED`) |
| 5 | Placeholder validity for this context (§7.2), and `{N}` with 1 ≤ N < this invocation's index | 2 (`E_BAD_REFERENCE`) |
| 6 | Store targets: namespace valid for the context, and write permitted per variable-access-matrix.md | 2 for an invalid context, 126 for denied |
| 7 | Custom command expansion: depth ≤ `MAX_CC_DEPTH`, no cycles | 2 (`E_CC_DEPTH` / `E_CC_CYCLE`) |
| 8 | Total invocations after expansion ≤ `MAX_INVOCATIONS (8)`; sentinels count | 2 (`E_TOO_MANY`) |

If any check fails, **no invocation executes.** The expression's result is the first failure in source order. Output rules for these codes are in §6.6.

**Error identifiers.** The `E_…` names above are carried in the Result's `data.error`, with the human-readable text in `message` — for example `Result(2, "{2} refers to a command that runs later", {"error": "E_BAD_REFERENCE", "reference": "{2}"})`. Parse errors (§3.4) also put their code in `data.error` alongside `data.column`. This keeps chat replies readable while `!explain`, `/parse` and the editor get stable identifiers.

**Cooldowns** are *checked* for every invocation during preflight, but *committed* only for invocations that actually execute (§6.3).

> **Planned (v1.x, a minor version):** cooldown check 3 moves out of preflight. An invocation on cooldown then fails at runtime with code 128, and `||` can handle that like any other failure: `!a || !b` runs `b` when `a` is on cooldown. If the final Result is 128, output stays silent and the callback still runs (§6.6).

### 5.3 Argument declarations

Built-in specs and custom command metadata declare parameters in the same shape:

```
param := position, name, type, required, default, constraints, description
position := "N" | "N+"          (N ≥ 1)
constraints := min, max (int/float/duration), max_len (str), choices (choice)
```

- Positions MUST be contiguous from 1. At most one `N+` position may exist, and it MUST come last.
- A required parameter MUST NOT follow an optional one.
- `name` MUST be a valid `ident` and unique. It creates the alias `{arg.<name>}`.
- Validation (§7.4) happens when the invocation executes, after placeholder expansion. Failure gives code 2 with usage text generated from the declarations.

---

## 6. Dynamic semantics

### 6.1 Result

```
Result := (code: int, message: str | null, data: Value)
Value  := null | bool | int (64-bit) | float (IEEE-754 double) | str | list[Value] | map[str, Value]
```

- `code = 0` means success. Any other value means failure.
- The serialized size of `data` MUST NOT exceed `MAX_DATA_BYTES (4096)`. Larger data yields code 2 (`E_DATA_TOO_LARGE`).
- On a runtime or preflight failure with a named error, `data` is a map carrying that name: `{"error": "E_…", …}` (§5.2).
- `message` MUST NOT exceed `MAX_MESSAGE_CHARS (2000)` and is truncated with `…`.

### 6.2 Exit codes

| Code | Name | Produced by |
|------|------|-------------|
| 0 | OK | success |
| 1 | FAIL | generic command failure |
| 2 | USAGE | parse errors, bad arguments, invalid references, limits |
| 3 | NOT_FOUND | lookup found nothing |
| 124 | TIMEOUT | stage or expression timeout |
| 125 | UPSTREAM_LIMITED | external API rate limit |
| 126 | DENIED | permission or write denied |
| 127 | UNKNOWN | unresolved or disabled command |
| 128 | COOLDOWN | cooldown active |
| 130 | CANCELLED | moderation cancellation (§6.7) |

Commands MUST use 1–3 or 5–99 for their own failures. Codes 100–255 are reserved for the runtime, and a command that *returns* one is corrected to code 1. A built-in MAY *raise* a runtime failure that carries a reserved code when the runtime owns that meaning: `!var` raises 126 when a write is denied, so the denial behaves like any other (silent, with the `on_denied` callback).

### 6.3 Evaluation

`eval(node, prev) → Result`, where `prev` is the Result visible as `{_}` (§6.4).

| Node | Evaluation |
|------|------------|
| `Invocation` | 1. Check moderation (§6.7). 2. Expand the arguments (§7.3) with `{_}` = prev. 3. Validate the parameters (§5.3). 4. Commit this invocation's cooldowns. 5. Execute with `stdin` (the pipe input, or null) under `STAGE_TIMEOUT (3 s)`. 6. Record the Result under its index. |
| `Pipe(L, R)` | `r = eval(L, prev)`. If `r.code ≠ 0`, return `r`. Otherwise return `eval(R, r)` with **stdin = r**. |
| `And(L, R)` | `r = eval(L, prev)`. If `r.code ≠ 0`, return `r`. Otherwise return `eval(R, r)` with stdin = null. |
| `Or(L, R)` | `r = eval(L, prev)`. If `r.code = 0`, return `r`. Otherwise return `eval(R, r)` with stdin = null. |
| `Group(E)` | `eval(E, prev)`: the result of the last invocation executed inside. |
| `Store(E, target, append)` | `r = eval(E, prev)`. If `r.code = 0`, buffer the write (§6.5). Return `r` unchanged. |

- **Pipe vs. and:** both stop on failure and both expose the left result as `{_}`. Only a pipe *also* delivers it as `stdin` to the command. Commands that accept input act on `stdin` without placeholders (e.g. `!upper`).
- **Pipe stdin into a group:** `L | ( A && B )` delivers stdin to the **first invocation evaluated** inside the group, which is `A`.
- The whole expression is bounded by `EXPR_TIMEOUT (6 s)`. When it expires, the running invocation is cancelled and the expression returns code 124.

### 6.4 Scopes, `{_}` and `{N}`

- **A scope** is one expression: a line, or one expansion of a custom command body. Every expansion has its own scope.
- **Indexes:** invocations are numbered 1..n in pre-order source order within their scope, whether or not they execute. A custom command invocation is one index in the outer scope. Its body's invocations belong to the inner scope.
- **`{N}`:** the Result of invocation N in the current scope. If N didn't execute (it was on a skipped branch), the reference is **missing** (§7.5).
- **`{_}`:** the `prev` argument of the current evaluation:
  - For the first evaluated invocation of a Line, Trigger, Listener or Callback scope, `{_}` is missing.
  - For the first evaluated invocation of a Body scope, `{_}` is the custom command's stdin if it has one, otherwise missing.
  - After an operator, `{_}` is the left operand's Result (per §6.3).
- **A custom command invocation's Result** is the Result of its body expression.

### 6.5 Variable writes

- **Buffering:** store operators and variable-writing commands add entries to a per-expression **write buffer**. Reads within the same expression MUST see buffered values (read-your-writes).
- **Committing:** the buffer is committed atomically after evaluation finishes, **regardless of the final code**, unless the expression was cancelled (§6.7) or timed out. In those cases the buffer is discarded.
- **Stored value for `>`:** `data` if it is not null, otherwise `message`. If both are null, the store is skipped and the result passes through; `!explain` and `command_runs` note the skip.
- **`>>` (append):**

| Existing value | Effect |
|----------------|--------|
| missing | the variable becomes `[v]` |
| list | `v` is appended, and the oldest items are dropped beyond `MAX_LIST_ITEMS (100)` |
| anything else | the store fails: the Store node returns code 2 (`E_NOT_A_LIST`) instead of `r`, and nothing is buffered |

- **Limits:** the per-value size limit (2 KB) and per-space name limits (ADR-0010) are checked when a write is buffered. A violation gives code 2.

### 6.6 Output

The expression's final Result `F` determines what the bot sends:

| `F.code` | Sent to chat |
|----------|--------------|
| 0 | `F.message`, if it is non-null and non-empty |
| 1–125, except the cases below | `F.message`, if it is non-null and non-empty, unless the channel sets `quiet_errors` |
| 2 from a parse error | only under the condition in §3.4 |
| 127 from invocation index 1 of a Line | nothing (silent) |
| 127 from a later invocation | `unknown command: <name>`, unless `quiet_errors` |
| 126, 128 | nothing. The `on_denied` / `on_cooldown` callback runs if configured (ADR-0006). |
| 130 | nothing |

- **Only `F` is ever sent.** Intermediate messages are never sent.
- **Sending path:** the message goes through the Outbox: the moderation recheck, then the badword filter, then chunking into at most `MAX_CHAT_MESSAGES (2)` messages of 500 characters each, then rate limiting.
- **Contexts:** Trigger, Listener and Callback contexts follow the same table. Explain never sends `F`; it sends its report instead (§9).

### 6.7 Moderation cancellation

For Line and Listener contexts, the implementation MUST check the ModerationIndex (architecture §8) at these **checkpoints**:

- before each invocation
- inside a command, before each side-effecting action: handlers call `ctx.ensure_not_cancelled()` before acting
- in the Outbox, immediately before sending

Cancellation is **cooperative**: a handler that is already running is not interrupted between checkpoints, so a slow call (an external API) may still complete. Nothing it produced is sent, because the Outbox rechecks.

If the triggering message was deleted, or its author was cleared (timed out or banned), or the channel was cleared at or after the message time:

- The run stops at the next checkpoint.
- The expression result becomes code 130.
- The write buffer is discarded, and nothing is sent.

Side effects that already happened, like an external API call or a Twitch moderation action, are not rolled back.

---

## 7. Placeholders

### 7.1 Registered roots

The authoritative list is [namespaces.md](namespaces.md) §1–§5. v1 roots: `_`, `N`, `arg`, `args`, `chatter`, `channel`, `publisher`, `cmd`, `bot`, `now`, `event`, `match`, `cooldown`, `denied`, `run`.

### 7.2 Availability by context

| Root | Line | Body | Trigger | Listener | Callback |
|------|------|------|---------|----------|----------|
| `_`, `N` | ✔ | ✔ | ✔ | ✔ | ✔ |
| `arg`, `args` | — | ✔ | ✔ (event input text as arguments) | — | — |
| `chatter.*` (fields and variables) | ✔ | ✔ | ✔ (event user, if any) | ✔ | ✔ |
| `channel.*`, `channel.chatter.*` | ✔ | ✔ | ✔ | ✔ | ✔ |
| `publisher.*` (all four) and `cmd` | — | ✔ | — | — | — |
| `bot`, `now`, `run` | ✔ | ✔ | ✔ | ✔ | ✔ |
| `event` | — | inherited, if the body runs from a trigger | ✔ | — | — |
| `match` | — | — | — | ✔ | — |
| `cooldown` / `denied` | — | — | — | — | ✔ (matching callback type) |

A root that isn't available in the context fails preflight with code 2 (`E_BAD_REFERENCE`). Timers have no chatter, so `chatter.*` there is missing.

### 7.3 Expansion

Placeholders are expanded **immediately before their invocation executes** (§6.3 step 1), and never earlier.

1. **Lookup:** resolve the root and path to a Value, or to **missing**:
   - A path into a non-map or non-list, a missing key, or an out-of-range index gives missing.
   - For result roots, a bare `{N}` / `{_}` gives `data` if it is a scalar (not null, list or map), otherwise `message`. `.code`, `.message` and `.data` select those parts. Any other first segment is looked up inside `data`.
2. **Type:** if `:type` is present, convert per §7.4. A conversion failure is treated as missing.
3. **Fallback:** if the value is missing, null or the empty string, and `??` is present, the fallback is expanded (recursively) and used, and no type is applied to it. Without `??`, the invocation fails with code 2 (`E_MISSING_VALUE: {ref}`) and does not execute.
4. **Render** the value to text (§7.6) and substitute it into the argument.
5. **No re-lexing.** Expanded text is never split into multiple arguments, never interpreted as operators, placeholders or escapes, and never parsed as a command. **One chunk stays one argument.**

**Argument captures** (Body and Trigger contexts):

| Capture | Value |
|---------|-------|
| `{arg.N}` | the Nth argument after the invoking command's own lexing (quotes removed, escapes applied) |
| `{arg.N+}` | arguments N..end joined with a single U+0020. *Test item: language proposal §6.* |
| `{arg.N+raw}` | the invocation's original source text from the start of argument N to the end of its arguments, byte-for-byte |
| `{arg.count}` | the number of arguments, as an int |
| `{args}` | same as `{arg.1+}` |
| `{arg.<name>}` | the declared parameter's **validated, converted** value (§5.3) |

### 7.4 Types

| Type | Accepts (after trimming) | Converted value | Paths |
|------|--------------------------|-----------------|-------|
| `str` | anything | str | — |
| `int` | `^[+-]?\d{1,18}$` | int | — |
| `float` | decimal or exponent notation; not NaN or Inf | float | — |
| `bool` | `true false yes no on off 1 0` (case-insensitive) | bool | — |
| `range` | `^(-?\d+)-(-?\d+)$` with lo ≤ hi | map | `lo`, `hi` |
| `duration` | `^(\d+h)?(\d+m)?(\d+s)?$`, non-empty, or a plain integer (seconds) | int (seconds) | `seconds` |
| `user` | `@?[A-Za-z0-9_]{1,25}`, resolved through Helix (cached; counts toward the stage timeout) | map | `id`, `name`, `display` |
| `choice(a,…)` | one of the listed items (case-insensitive) | str (canonical item) | — |
| `url` | absolute `http`/`https` URL passing the URL safety policy | str | — |

`int`, `float` and `duration` params MAY declare `min`/`max`. `str` MAY declare `max_len`.

### 7.5 Missing values

A reference is **missing** when:
- its lookup fails (§7.3.1)
- it refers to `{N}` that didn't execute
- `{_}` has no previous result
- a variable doesn't exist
- a context field is unavailable at runtime (e.g. `chatter` in a timer)
- a type conversion fails

### 7.6 Rendering values to text

| Value | Text |
|-------|------|
| str | as is |
| int | decimal, with no separators |
| float | shortest round-trip representation, max 6 decimal places, no exponent for 1e-6 ≤ \|x\| < 1e15, trailing zeros removed (`21.5`, `3`, `0.333333`) |
| bool | `true` / `false` |
| null | *(treated as missing before rendering; see §7.3.3)* |
| list | items rendered and joined with `, ` |
| map | compact JSON with keys in insertion order |

---

## 8. Sentinel commands

These are built into the `core` module. They can't be disabled or shadowed, have no cooldowns, require role `everyone`, are logged at level `off`, and **count toward `MAX_INVOCATIONS`**.

| Command | Arguments | Result |
|---------|-----------|--------|
| `true` | none (extra arguments → code 2) | `code 0`, `message = null`, `data` copied from `{_}` if present, otherwise null. It never repeats a failure message. *(Appendix B #1)* |
| `false` | none | `code 1`, null message and data |
| `default` | `arg.1+ : str` (required) | `code 0`, `message = data = arg.1+` |
| `fail` | `arg.1 : int` (optional, 1–99, default 1), `arg.2+ : str` (optional) | `code = arg.1`, `message = arg.2+` or null, `data = null`. If the first argument isn't an int, all arguments form the message and the code is 1. |
| `echo` | `arg.1+ : str` (optional) | `code 0`, `message = data = arg.1+` (the empty string if no arguments) |

Idioms:
- **Optional command:** `!shoutout {arg.1} || true`
- **Fallback value:** `( !weather {arg.1+} || default unknown ) > chatter.last_weather`
- **Validation:** `!check {arg.1:int} <= 20 || fail 2 max 20 dice`

---

## 9. `!explain`

`explain` is a raw-tail command (§3.3). It parses its raw argument as an expression in the context being explained: the Line context by default, or Body with `--as-body`.

It MUST report:
- the AST, with operator precedence made visible and invocation indexes
- name resolution for each invocation: source, owner, version and publication grants
- the preflight result for each invocation: toggle, rank vs. required role, both cooldowns with remaining time, and input mode
- placeholder references, whether each is available in the context, and store targets with their write permission
- the first failing check and the resulting code, if any

With `--run`, it also evaluates the expression with a **discarded** write buffer, **no Outbox send**, no cooldown commits and no side effects (commands with side effects return code 0 with `data = {"dry_run": true}`), and reports each executed Result.

- **Chat output:** a compact one-line summary, plus a link to the full report when the web UI is enabled.
- **API output:** the full structured report.

---

## 10. Limits (summary)

| Name | Default | Section |
|------|---------|---------|
| `MAX_EXPR_CHARS` | 2000 (bodies) | §3.4 |
| `MAX_PLACEHOLDER_NESTING` | 4 | §2.7 |
| `MAX_INVOCATIONS` | 8 (after expansion) | §5.2 |
| `MAX_CC_DEPTH` | 3 | §5 |
| `STAGE_TIMEOUT` | 3 s | §6.3 |
| `EXPR_TIMEOUT` | 6 s | §6.3 |
| `MAX_DATA_BYTES` | 4096 | §6.1 |
| `MAX_MESSAGE_CHARS` | 2000 | §6.1 |
| `MAX_CHAT_MESSAGES` | 2 (500 chars each) | §6.6 |
| `MAX_LIST_ITEMS` | 100 | §6.5 |
| Variable value size | 2 KB | ADR-0010 |

---

## 11. Versioning

- This document is **syntax version 1.0**. Changes that alter how existing valid input parses or evaluates require a **major** version change. Additions that only make previously invalid input valid (e.g. un-reserving `;`) are **minor** version changes.
- Stored bodies keep the syntax version they were parsed with. When the major version changes, the implementation MUST either keep a parser for the old version or migrate bodies automatically, and record the migration as a new custom command version.
- Deferred features are tracked in language proposal §6: the `?` suffix, and `;` behavior.

---

## Appendix A. Conformance examples

The parser golden-test corpus MUST include at least the following cases. `⟨…⟩` shows argument boundaries.

### A.1 Lexing and parsing

| # | Input (Line context) | Expected |
|---|----------------------|----------|
| 1 | `!random 1-100 \| echo dice rolled a {1}!` | `Pipe(random⟨1-100⟩, echo⟨dice⟩⟨rolled⟩⟨a⟩⟨{1}!⟩)` |
| 2 | `!echo a\|b ;) -> >:( (lol)` | `echo⟨a\|b⟩⟨;)⟩⟨->⟩⟨>:(⟩⟨(lol)⟩` |
| 3 | `!echo it's "a \| b" fine` | `echo⟨it's⟩⟨a \| b⟩⟨fine⟩` |
| 4 | `!echo ab"c d"e` | `echo⟨abc de⟩` |
| 5 | `!echo \| literal` | `Pipe(echo, literal)`: the second chunk is an operator |
| 6 | `!echo \\\| literal` | `echo⟨\|⟩⟨literal⟩` (escaped pipe) |
| 7 | `!echo a ; b` | `E_RESERVED_OPERATOR` |
| 8 | `!echo "unterminated` | `E_UNTERMINATED_QUOTE` |
| 9 | `!echo {friend}` | `E_BAD_PLACEHOLDER` (unknown root) |
| 10 | `!echo \{friend}` | `echo⟨{friend}⟩` |
| 11 | `( !a \|\| true ) && !b` | `And(Group(Or(a, true)), b)` |
| 12 | `!a && !b \| !c > channel.x \|\| !d` | `Or(And(a, Pipe(b, Store(c, channel.x))), d)` |
| 13 | `!a \| && !b` | `E_UNEXPECTED_OPERATOR` |
| 14 | `!a > {chatter.name}` | `E_DYNAMIC_VARREF` |
| 15 | `!explain !random 1-6 \| echo {1}` | `explain` with raw tail `!random 1-6 \| echo {1}` |
| 16 | `!cc add roll "!random 1-6 \| echo {1}"` | raw tail `!random 1-6 \| echo {1}` (outer quotes removed) |
| 17 | `! random` | not a command (no name character right after the prefix) |
| 18 | `@alice !random 1-6` sent as a reply to alice | reply mention stripped → `random⟨1-6⟩` |
| 19 | `!echo {chatter.location ?? {channel.location ?? Lisbon}}` | nested fallback placeholder |
| 20 | `!echo {arg.1:choice(a,b) ?? a}` | typed placeholder with a choice type (valid in the Body context only) |

### A.2 Evaluation

| # | Expression | Scenario | Final Result / sent |
|---|------------|----------|---------------------|
| 1 | `!weather Lisbon \| echo "it's {1.celsius}C"` | weather OK, `data.celsius = 21.5` | `0` / `it's 21.5C` |
| 2 | `!weather Nowhere \| echo "{1.celsius}"` | weather returns code 3 with "location not found" | `3` / `location not found` (the pipe stopped) |
| 3 | `!weather Nowhere \|\| echo "failed: {_.message}"` | as above | `0` / `failed: location not found` |
| 4 | `( !weather x \|\| default ? ) > chatter.w` | weather fails | `0` / `?`; `chatter.w = "?"` |
| 5 | `!weather x > chatter.w` | weather fails | `3` / weather's message; **nothing stored** |
| 6 | `!a \|\| !b && echo {2}` | `a` succeeds | `{2}` is missing → code 2 `E_MISSING_VALUE` unless `??` is used |
| 7 | `!shoutout @x \|\| true` | shoutout fails | `0` / nothing sent (`true` has no message) |
| 8 | `!foo` (unknown) | — | `127` / nothing sent |
| 9 | `!random 1-6 \| !foo` (unknown second) | — | `127` / `unknown command: foo` |
| 10 | `!mod-only-cmd` by a viewer | — | `126` / nothing, and `on_denied` runs if set |
| 11 | `!random 1-6 > channel.x` by a viewer (channel write role = mod) | — | preflight `126` / nothing; `random` never runs |
| 12 | `!echo {1}` | — | preflight `E_BAD_REFERENCE` (N must be < own index) |

---

## Appendix B. Open items found while writing the spec

All items were resolved in review (2026-09-16):

1. **`true` pass-through:** copies `data` only, never the message. ✔
2. **Parse-error visibility:** parse errors are sent only when the first command is runnable by the invoker. ✔
3. **Unknown later commands:** reply `unknown command: x`, unless `quiet_errors` is set. ✔
4. **Cooldowns in preflight:** kept for v1, so every invocation is checked, including skipped branches. **Planned refinement (v1.x):** cooldowns become a *runtime* failure (code 128) of the individual invocation, so `||` can handle them. `!a || !b` with `a` on cooldown would then run `b`. Permissions and toggles stay in preflight. Tracked in language proposal §6.
5. **Reply-mention stripping:** keep the step. Verify the EventSub text format during implementation (action item).
6. **Commit on failure:** writes commit even when the final code ≠ 0. There is **no rollback** based on the end result. Only moderation cancellation and timeouts discard the buffer. ✔

---

## Appendix C. PEG grammar (normative for parsing)

> **Errata applied while implementing the reference parser (2026-09-16):**
> 1. The right side of `&&`/`||` is a `Pipeline` (`LogicOperand`), not a single `Stage`. Otherwise `a && b | c` failed to parse.
> 2. `RawTailInvocation` needs a `&NameStart` lookahead before `Name`, so trying the raw-tail alternative on input like `( !a …` backtracks instead of throwing `E_BAD_NAME`.

This grammar is the **authoritative parsing definition**. §2–§3 describe the same language in prose and EBNF. Where they disagree, this appendix wins, and the prose MUST be corrected. The reference parser (`lang/parser.py`) MUST mirror these rules one-to-one, as a hand-written recursive-descent PEG parser with memoization where needed.

### C.1 Notation

| Notation | Meaning |
|----------|---------|
| `A <- e` | rule definition |
| `'x'` | literal, case-sensitive |
| `[a-z]` | character class |
| `.` | any single character |
| `e1 / e2` | **ordered** choice: `e2` is tried only if `e1` fails |
| `e*`, `e+`, `e?` | zero or more, one or more, optional (greedy, no backtracking into repetitions) |
| `&e`, `!e` | positive and negative lookahead (consume nothing) |
| `n:e` | label: bind the match of `e` to `n` for semantic actions and predicates |
| `&{ p }` | semantic predicate: succeeds if `p` is true (consumes nothing) |
| `%E_CODE` | **throw**: parsing stops immediately with error `E_CODE` at the current position. Throws are *not* caught by ordered choice or repetition. |
| `PREFIX`, `raw_tail_from(…)`, `is_registered_root(…)` | **parser parameters** supplied by the runtime (§C.6) |

Parsing works directly on characters, with no separate lexer. Positions are character offsets in the pre-processed input (§2.1), and error columns are 1-based.

### C.2 Entry points

```peg
# Line context. If LineStart fails, the result is NOT_A_COMMAND (no error, no reply).
Line            <- LineStart LineBody
# Body, Trigger, Listener, Callback and Explain contexts.
Body            <- _ LineBody

LineBody        <- RawTailInvocation _ EOF
                 / Expr End

LineStart       <- &( (Open WS)* PREFIX '@'? NameStart )

End             <- _ EOF
                 / WS Reserved %E_RESERVED_OPERATOR
                 / WS Close    %E_UNBALANCED_GROUP
                 / WS OperatorToken %E_UNEXPECTED_OPERATOR
```

### C.3 Expressions

```peg
Expr            <- Logical

Logical         <- Pipeline (WS LogicOp LogicOperand)*
LogicOperand    <- WS Pipeline                         # `a && b | c` = a && (b | c)
                 / _ EOF %E_MISSING_OPERAND
Pipeline        <- Stage (WS PipeOp Operand)*
Operand         <- WS Stage
                 / _ EOF %E_MISSING_OPERAND

Stage           <- Primary StoreSuffix?
StoreSuffix     <- WS StoreOp StoreTarget
StoreTarget     <- WS VarRef
                 / _ EOF %E_MISSING_OPERAND
                 / WS OperatorToken %E_UNEXPECTED_OPERATOR

Primary         <- Group
                 / &Reserved %E_RESERVED_OPERATOR
                 / &OperatorToken %E_UNEXPECTED_OPERATOR
                 / Invocation

Group           <- Open GroupInner GroupEnd
GroupInner      <- WS Expr
                 / _ EOF %E_MISSING_OPERAND
GroupEnd        <- WS Close
                 / _ EOF %E_UNBALANCED_GROUP
                 / WS Reserved %E_RESERVED_OPERATOR
                 / WS OperatorToken %E_UNEXPECTED_OPERATOR
```

### C.4 Invocations

```peg
Invocation      <- CmdPrefix? p:'@'? n:Name a:Arg* RawCheck
Arg             <- WS !OperatorToken Word
RawCheck        <- &{ is_int(raw_tail_from(n, a)) } %E_RAW_TAIL_POSITION   # raw-tail cmd inside an expression
                 / ''

# Raw-tail commands (§3.3): the only invocation in the input. Tried before Expr.
RawTailInvocation
                <- CmdPrefix? p:'@'? &NameStart n:Name    # &NameStart: don't throw E_BAD_NAME on e.g. "( !a"
                   &{ raw_tail_from(n, []) != NONE }
                   a:RawLeadArg*                      # arguments before the raw tail
                   &{ is_int(raw_tail_from(n, a)) }   # fails for e.g. "cc info" → backtrack to Expr
                   RawTail?
RawLeadArg      <- &{ needs_more_lead(n, a) } WS !OperatorToken Word
RawTail         <- WS r:RawText                       # action: strip one pair of outer quotes (§3.3 rule 3)
RawText         <- (!EOF .)+

CmdPrefix       <- PREFIX &('@'? NameStart)
Name            <- NameStart NameChar* &(WSChar / EOF)          # action: lowercase; check length ≤ 32
                 / &'{' %E_DYNAMIC_NAME
                 / %E_BAD_NAME
NameStart       <- [a-zA-Z0-9]
NameChar        <- [a-zA-Z0-9_-]
```

**Raw-tail parameter functions.** `raw_tail_from(name, args)` inspects only as many leading arguments as it needs, and returns:

| Return | Meaning | Examples |
|--------|---------|----------|
| an int `N` | the raw tail starts at argument `N` (1-based) | `("explain", []) → 1`, `("cc", ["add"]) → 3`, `("cc", ["add", "x", "y"]) → 3` |
| `MORE` | the decision depends on the next argument (a subcommand) | `("cc", []) → MORE` |
| `NONE` | not a raw-tail command | `("random", []) → NONE`, `("cc", ["info"]) → NONE` |

- `is_int(r)` is true only for an int.
- `needs_more_lead(n, a)` is `r == MORE or (is_int(r) and len(a) + 1 < r)`, where `r = raw_tail_from(n, a)`.

**This is the one context-sensitive rule.** `RawTailInvocation` is tried first.
- For `cc info …`, the final predicate fails, so the parser backtracks and `Expr` parses `cc` as a normal invocation.
- For a raw-tail command used inside a larger expression (`!a && !cc add x …`), `RawCheck` throws `E_RAW_TAIL_POSITION`.

### C.5 Words, quotes, escapes, placeholders

```peg
Word            <- Segment+
Segment         <- Quoted / Escape / Placeholder / Bare
Bare            <- (!(WSChar / '"' / '\\' / '{') .)+

Quoted          <- '"' QChar* ( '"' / %E_UNTERMINATED_QUOTE )
QChar           <- Escape / Placeholder / (!('"' / '\\' / '{') .)

Escape          <- '\\' .                              # action: the literal next character
                 / '\\' EOF                            # action: a literal backslash

Placeholder     <- '{' &{ depth < MAX_PLACEHOLDER_NESTING }
                   ( PhInner '}' / %E_BAD_PLACEHOLDER )
PhInner         <- _ Ref TypeSpec? Fallback? _
Ref             <- Root ('.' Segment_)*
Root            <- '_' !IdentChar
                 / Index
                 / i:Ident &{ is_registered_root(i) }
Index           <- [1-9] [0-9]*
Segment_        <- Digits '+raw' / Digits '+' / Digits / Ident
TypeSpec        <- ':' _ ( ChoiceType / TypeName )
ChoiceType      <- 'choice(' _ ChoiceItem (_ ',' _ ChoiceItem)* _ ')'
ChoiceItem      <- (!(',' / ')' / '}' / WSChar) .)+
TypeName        <- ('str' / 'int' / 'float' / 'bool' / 'range' / 'duration' / 'user' / 'url') !IdentChar
Fallback        <- _ '??' _ FbPart*                    # action: trim trailing WS
FbPart          <- Escape / Placeholder / (!('{' / '}' / '\\') .)

Ident           <- [A-Za-z_] IdentChar*
IdentChar       <- [A-Za-z0-9_]
Digits          <- [0-9]+
```

### C.6 Operators, variable references, whitespace

```peg
Boundary        <- &(WSChar / EOF)
PipeOp          <- '|'  Boundary
OrOp            <- '||' Boundary
AndOp           <- '&&' Boundary
LogicOp         <- AndOp / OrOp
StoreOp         <- '>>' Boundary / '>' Boundary
Open            <- '('  Boundary
Close           <- ')'  Boundary
Reserved        <- ';'  Boundary
OperatorToken   <- ('||' / '|' / '&&' / '>>' / '>' / '(' / ')' / ';') Boundary

VarRef          <- VarNs '.' v:VarName Boundary
                   &{ len(v) <= 32 and not is_reserved_var(ns, v) }
                 / &'{' %E_DYNAMIC_VARREF
                 / %E_BAD_VARREF
VarNs           <- 'publisher.channel.chatter' &'.'
                 / 'publisher.channel' &'.'
                 / 'publisher.chatter' &'.'
                 / 'publisher' &'.'
                 / 'channel.chatter' &'.'
                 / 'channel' &'.'
                 / 'chatter' &'.'
VarName         <- [a-z] [a-z0-9_]*

WSChar          <- [\p{White_Space}]
WS              <- WSChar+
_               <- WSChar*
EOF             <- !.
```

If the `&{…}` check in the first `VarRef` alternative fails (a reserved or over-long name), the parser MUST throw `E_BAD_VARREF` rather than fall through. Implementations do this by evaluating the predicate as a throw.

**Parser parameters**, supplied per parse call:

| Parameter | Source |
|-----------|--------|
| `PREFIX` | the channel's prefix (§2.1). Ignored for non-Line contexts except inside `CmdPrefix`. |
| `raw_tail_from(name, lead_args)` | the command registry: built-in specs with `raw_tail_from` |
| `is_registered_root(ident)` | namespaces.md §1–§5 |
| `is_reserved_var(ns, name)` | namespaces.md §5 |
| `MAX_PLACEHOLDER_NESTING` | §10 |

Parameters may depend on the channel, for example which raw-tail commands are enabled. Name *resolution* never happens during parsing (§5.1). The parser only asks whether a name is a raw-tail command.

### C.7 Semantic actions (AST construction)

| Rule | Produces |
|------|----------|
| `Logical`, `Pipeline` | left-folded `And`/`Or` and `Pipe` nodes |
| `Stage` with `StoreSuffix` | `Store(inner, VarRef, append = (op == '>>'))` |
| `Group` | `Group(inner)` |
| `Invocation`, `RawTailInvocation` | `Invocation(index=0, name=lower(n), personal=p is not None, args, raw_tail, span)`. Indexes are assigned in a pre-order pass after parsing (§6.4). |
| `Word` | `Arg`: adjacent `Text` parts merged, and `Placeholder` parts kept in order |
| `Quoted` | its parts, without the quote characters |
| `Escape` | `Text(char)` |
| `Placeholder` | `Placeholder(root, path, type, fallback, span)` |

Checks outside the grammar, run before parsing:
- `E_TOO_LONG`, on the whole input
- pre-processing (§2.1)

### C.8 Error reporting

- The parser MUST report the **first** thrown error, with its code, the 1-based column and a hint.
- Errors not thrown by name (plain PEG failure at the top level) MUST NOT happen in a conforming implementation. `End` and `Primary` cover every leftover case. If one does happen, it is reported as `E_INTERNAL`, logged, and treated as a parse error (code 2).
- **Hints per code:**

| Code | Hint text (English, localizable) |
|------|----------------------------------|
| `E_UNTERMINATED_QUOTE` | `missing closing " (use \" for a literal quote)` |
| `E_BAD_PLACEHOLDER` | `invalid placeholder (use \{ for a literal brace)` |
| `E_RESERVED_OPERATOR` | `; is reserved (use && or \|\|)` |
| `E_UNEXPECTED_OPERATOR` | `unexpected <op> (quote it for literal text)` |
| `E_MISSING_OPERAND` | `expected a command after <op>` |
| `E_UNBALANCED_GROUP` | `unbalanced parentheses` |
| `E_BAD_NAME` | `invalid command name` |
| `E_DYNAMIC_NAME` | `command names can't be placeholders` |
| `E_DYNAMIC_VARREF` | `store targets can't be placeholders` |
| `E_BAD_VARREF` | `invalid variable (e.g. channel.deaths)` |
| `E_RAW_TAIL_POSITION` | `<name> must be used alone` |

---

## Appendix D. EBNF for documentation (non-normative)

This is a simplified W3C-style EBNF (XML spec notation) for **railroad diagrams** on the public docs page. It leaves out error productions, predicates and raw tails. Appendix C is authoritative.

```ebnf
Line        ::= Prefix Expr
Expr        ::= Pipeline ( ( '&&' | '||' ) Pipeline )*
Pipeline    ::= Stage ( '|' Stage )*
Stage       ::= ( Group | Invocation ) ( ( '>' | '>>' ) VarRef )?
Group       ::= '(' Expr ')'
Invocation  ::= Prefix? '@'? Name Argument*
Argument    ::= ( Text | Quoted | Escape | Placeholder )+
Quoted      ::= '"' ( [^"\{] | Escape | Placeholder )* '"'
Escape      ::= '\' Char
Placeholder ::= '{' Reference ( ':' Type )? ( '??' Fallback )? '}'
Reference   ::= ( '_' | Index | Root ) ( '.' Segment )*
Root        ::= 'arg' | 'args' | 'chatter' | 'channel' | 'publisher' | 'cmd'
              | 'bot' | 'now' | 'event' | 'match' | 'cooldown' | 'denied' | 'run'
Segment     ::= Identifier | Digits ( '+' | '+raw' )?
Type        ::= 'str' | 'int' | 'float' | 'bool' | 'range' | 'duration' | 'user' | 'url'
              | 'choice(' Item ( ',' Item )* ')'
VarRef      ::= ( 'chatter' | 'channel' | 'channel.chatter' | 'publisher' | 'publisher.chatter'
              | 'publisher.channel' | 'publisher.channel.chatter' ) '.' VarName
Name        ::= [a-z0-9] [a-z0-9_-]*
```

Diagrams are generated when the web UI is built, from a copy of this block extracted to `docs/grammar/railroad.ebnf`. A CI check fails if that copy differs from this appendix.
