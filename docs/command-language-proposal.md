# Command Language — Syntax Proposal (draft 2)

**Status:** Superseded by **[command-language-spec.md](command-language-spec.md)**. Kept for rationale and the deferred and test items in §6. · **Date:** 2026-09-16

The runtime contract (AST, `Result`, preflight) is in ADR-0005. The list of placeholder namespaces is in [namespaces.md](namespaces.md).

### Decisions so far

| # | Decision |
|---|----------|
| D1 | `{N}` = result of the Nth command. Arguments are `{arg.N}`, `{arg.N+}` and `{arg.N:type}`. Command docs use the `arg.N` names too. |
| D2 | Only the final message is sent. **`;` is dropped** (see §3.3). |
| D3 | A pipe stops on failure. `\|\|` handles failures. Sentinel commands `true`, `false`, `default` and `fail` exist (see §3.4). |
| D4 | Operators count only as standalone, whitespace-separated tokens. Only `"` quotes. `\` escapes. |
| D5 | `>` / `>>` store **only on success**. A fallback is written explicitly: `( !cmd \|\| default 123 ) > channel.x` |
| D6 | **No keyword operator aliases** (`and`, `or`, `then`) |
| D7 | `{arg.N+}` strips quotes and `{arg.N+raw}` keeps the exact text. **This is a test item** (§6). |
| D8 | The `?` shorthand for `\|\| true` is **deferred** (§6) |

---

## 1. Constraints from Twitch chat

| Character or form | Normal chat use | Consequence |
|-------------------|-----------------|-------------|
| `/cmd`, `.cmd` at line start | Twitch client commands | Never a prefix |
| `@name` | Mentions | Literal; the `user` type parses it |
| `'` | Apostrophes | **Not a quote character** |
| `$`, `%` | Prices, percentages | Not used in syntax |
| `;` | Winks `;)` | **Not an operator** (dropped) |
| `>` `<` | `->`, `>:(`, `<3` | `>`/`>>` are operators **only** as standalone tokens |
| `( )` | "(lol)" | Grouping **only** as standalone tokens |
| `{ }` | Almost never used | Placeholders |
| `:word:` | Emoji shortcodes | `:` only has meaning inside `{…}` |
| `\` | Almost never used | Escape |

## 2. Prior art

- **sh/bash:** `|`, `&&`, `||`, `( )`, `>`, exit codes, `true`/`false`.
- **Nushell and PowerShell:** structured data in pipes; "previous result" as `_`.
- **Chatterino:** `{…}` placeholders, `{2+}` rest-of-arguments, dotted paths.
- **Supibot:** `|` pipes work fine in Twitch chat.
- **Rejected:** bash `'` quoting and `$vars`, Mustache `{{ }}`, and `%var%`, because they collide with chat or are tedious to type.

## 3. Syntax

### 3.1 Tokens

1. **Operators must be standalone tokens**, separated by whitespace: `|`, `&&`, `||`, `>`, `>>`, `(`, `)`. `a|b`, `;)`, `->`, `(lol)` stay literal.
2. **`"…"` quotes** group words into one argument. Operators inside quotes are literal, but placeholders are still expanded. `'` is always literal.
3. **`\` escapes** the next character: `\"`, `\{`, `\\`, and `\|` for a literal standalone pipe.
4. **Placeholders** use `{…}` inside or outside quotes. See §3.5.
5. **Only the first command needs the prefix.** Later commands may include it or not (`| echo` = `| !echo`).

### 3.2 Operators (highest precedence first)

| Operator | Semantics |
|----------|-----------|
| `( … )` | Group. Its result is the result of the last command executed inside. |
| `\|` | Pipe. The right side gets `stdin` = the left result. **If the left code ≠ 0, the pipe stops** and the whole pipe returns that failing result. |
| `> var` / `>> var` | Store or append the left result's data (the message if there's no data). The result passes through unchanged. **Only on success.** A failing result isn't stored and the failure continues. To store a fallback, handle the failure first inside a group: `( !cmd \|\| default 123 ) > channel.x`. |
| `&&` | Runs the right side only if the left code = 0 |
| `\|\|` | Runs the right side only if the left code ≠ 0. `{_}` on the right is the failing result (`{_.code}`, `{_.message}`). |

- `&&` and `||` have equal precedence and associate left, as in bash: `a && b || c` = `(a && b) || c`.
- **The message sent to chat is the message of the last executed command**, and only if it isn't empty. Nothing is sent when the final message is empty. Other messages are never sent.

### 3.3 Why `;` was dropped

- Now that only the final message is sent, `a ; b` differs from `a && b` in one way only: `b` also runs when `a` fails. That case is spelled `( a || true ) && b`.
- Dropping `;` removes the operator most likely to collide with chat (`;)`, `;D`).
- It also removes a question nobody can answer intuitively: which message gets sent?
- `;` is **reserved** as a standalone token, and currently rejected with a parse error. It could come back later if a real need appears, for example meaning "send each message" with the chat-message cap.

### 3.4 Sentinel commands

Built-in commands in the `core` module. They can't be disabled, have no cooldowns, and aren't logged.

| Command | Result | Typical use |
|---------|--------|-------------|
| `true` | `code 0`, no message, **data and message pass through from `{_}` if there is one** | Make something optional: `!shoutout {arg.1} \|\| true` |
| `false` | `code 1`, no message | Testing, forcing a branch |
| `default <value…>` | `code 0`, message = data = value | Fallback value: `!weather {arg.1+} \|\| default "weather unavailable"` |
| `fail [code] <message…>` | `code` (default 1), message | Validation in custom commands: `!check {arg.1:int} <= 20 \|\| fail 2 "max 20 dice"` |
| `echo <text…>` | `code 0`, message = data = text | Formatting |

- **`true` passes data through.** In `( !weather x || true ) | echo "{_.celsius ?? ?}"`, a failed weather lookup doesn't break the pipe.
- A **`?` suffix** as shorthand for `|| true` (e.g. `!shoutout? {arg.1}`) is **deferred**, not rejected. It's tracked in §6.
- **Value sentinels inside placeholders** are handled by `??` (`{x ?? default}`), not by new keywords.

### 3.5 Placeholders

Full registry: [namespaces.md](namespaces.md).

```
{ root . path  [ :type ]  [ ?? fallback ] }

{1}  {1.celsius}  {_.code}           results
{arg.1}  {arg.3+}  {arg.1:int}       arguments (typed)
{chatter.x}  {channel.chatter.x}     variables
{publisher.chatter.x}
{chatter.name}  {event.input}        context
{chatter.location ?? Lisbon}         fallback
```

- `.` means path access and `:` means type validation.
- If a placeholder is missing and has no `??`, the command returns code 2 before running.

### 3.6 Examples

```text
!random 1-100 | echo dice rolled a {1}!
!weather Lisbon | echo "it's {1.celsius}C now!" || echo "no weather for Lisbon"
!weather {chatter.location ?? Lisbon} | echo "{_.condition}, {_.celsius}C"
!random 1-20 > chatter.lastroll && echo "{chatter.name} rolled {1}"
( !random 1-6 | !check >= 5 ) && echo "win!" || echo "lose"
!shoutout {arg.1:user} || true
echo "arrow -> stays literal ;) and so does (this)"

!cc add roll "!random 1-{arg.1:int ?? 20} | echo {chatter.name} rolled {1} on a d{arg.1 ?? 20}"
!cc add say "echo {chatter.display} says: {arg.1+}"
!cc add pts "!var incr channel.chatter.points {arg.1:int ?? 1} | echo {chatter.name} now has {_} points"
( !weather {arg.1+} || default "unknown" ) > chatter.last_weather
!cc add guess "!check {arg.1:int} == {publisher.channel.secret} && ( !var incr publisher.channel.chatter.wins | echo {chatter.name} got it! ) || echo nope"
```

### 3.7 Draft grammar (EBNF)

```ebnf
line        = prefix , logical ;
logical     = pipeline , { WS , ( "&&" | "||" ) , WS , pipeline } ;
pipeline    = stage , { WS , "|" , WS , stage } ;
stage       = ( group | command ) , [ WS , ( ">" | ">>" ) , WS , varref ] ;
group       = "(" , WS , logical , WS , ")" ;
command     = [ prefix ] , name , { WS , arg } ;
arg         = word | quoted ;
word        = { char - WS | escape | placeholder }- ;     (* never exactly an operator token *)
quoted      = '"' , { char - '"' | escape | placeholder } , '"' ;
placeholder = "{" , ref , [ ":" , type ] , [ WS? , "??" , WS? , fallback ] , "}" ;
ref         = root , { "." , segment } ;
root        = "_" | digits | "arg" | "args" | "chatter" | "channel" | "publisher"
            | "cmd" | "bot" | "now" | "event" | "match" | "cooldown" | "denied" | "run" ;
segment     = ident | digits | digits , "+" | digits , "+raw" ;
varref      = ( "chatter" | "channel" | "channel.chatter"
              | "publisher" | "publisher.chatter" | "publisher.channel" | "publisher.channel.chatter" ) , "." , ident ;
fallback    = { char - "}" | escape | placeholder } ;
escape      = "\" , any ;
```

## 4. How commands document their parameters

Built-in and custom commands both list parameters positionally, using the `arg.N` names:

```
!roll — Roll a die
  arg.1   sides   int (2–1000)   optional, default 20   Number of sides
  arg.2+  label   str            optional               Text shown with the roll
  data:   { value: int, sides: int }
  usage:  !roll [sides] [label…]
  e.g.:   !roll 6 for initiative   →  vex rolled 4 (d6) for initiative
```

```
!cc param roll 1 name=sides type=int min=2 max=1000 default=20 "Number of sides"
!cc param roll 2+ name=label type=str "Text shown with the roll"
!cc example roll "!roll 6 for initiative" "vex rolled 4 (d6) for initiative"
```

Declared types are validated on invocation. Inside the body, `{arg.1}` is then already an int, and `{arg.sides}` is an alias for it.

## 5. Open questions

None at the moment. Round 2 resolved everything (D5–D8).

## 6. Deferred ideas and test items

| Item | Status | Notes |
|------|--------|-------|
| `?` suffix = `\|\| true` (`!shoutout? @x`) | **Deferred** | Revisit after real usage shows how often `\|\| true` gets typed. Check for collisions with commands or arguments ending in `?` (e.g. `!8ball will it work?`). The `?` would have to attach to the *command name* only. |
| `{arg.N+}` strips quotes | **Test** | Parser golden tests plus a live chat trial: `!say "hello" world`, `!say he said "hi"`, `!say it's "fine"`, nested escaped quotes. Confirm people rarely need `+raw`. |
| `;` sequence operator | **Reserved** | Only if a "send every message" mode is ever wanted |
| Runtime cooldown failures | **Planned (v1.x)** | Cooldowns fail the individual invocation with code 128 at runtime instead of blocking the whole line in preflight, so `!a \|\| !b` runs `b` when `a` is on cooldown. See spec §5.2. |
| Keyword operator aliases | **Rejected** | — |
