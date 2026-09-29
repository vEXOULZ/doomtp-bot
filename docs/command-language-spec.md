# doomtp-bot Command Language — Specification

**Version:** 2.0.0-draft · **Status:** Draft for review · **Date:** 2026-09-28 (1.2: 2026-09-28, 1.1: 2026-09-22, 1.0: 2026-09-16)
**Syntax version:** 2.0 ([ADR-0018](adr/0018-command-language-v2-expressions-brackets-error-codes.md): expressions, brackets, `$` fields, `->`/`-->`, one code per error)
**Supersedes:** [command-language-proposal.md](command-language-proposal.md) (kept for rationale)
**Normative companions:** [namespaces.md](namespaces.md) (namespaces, types, reserved names) and [variable-access-matrix.md](variable-access-matrix.md) (variable permissions)
**Runtime design:** [ADR-0005](adr/0005-command-pipeline-runtime.md)

---

## 0. Conventions

- The key words **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT** and **MAY** are used as in RFC 2119.
- Grammar is written in ISO-style EBNF. `WS` is defined in §2.2.
- "Implementation" means the doomtp-bot lexer, parser, resolver, preflight and executor.
- Examples use `!` as the channel prefix. The shipped default is `🏜` (§2.1); every channel can change it.
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
4. **Detect the command.** The text is an expression only if it matches `line_start` (§3.1): the channel prefix followed by a name character, optionally preceded by standalone `(` tokens. Otherwise the message is not a command, and it goes to listeners only.

Other Unicode normalization (NFC/NFKC, case folding) MUST NOT be applied to argument text. Command names are case-folded (§2.6).

**The channel prefix** MUST be 1–3 characters, contain no WS, not start with `/` or `.`, and not contain `{`, `}`, `"`, `\`, `|`, `&`, `>`, `(` or `)`. Its **last character MUST NOT be a name character** (`[A-Za-z0-9_@-]`), so the boundary between prefix and name is unambiguous. The default prefix is `🏜` (U+1F3DC).

**The prefix gap.** When the prefix's last character is **not ASCII** — an emoji prefix such as `🏜` — WS between the prefix and the name is allowed and ignored: `🏜 ping` and `🏜ping` are the same command. ASCII prefixes do **not** allow the gap, because `! ping` and `! that was close` are indistinguishable in ordinary chat.

**U+FE0F (the emoji presentation selector)** is ignored when matching the prefix, on either side. A prefix saved as `🏜` matches a message that begins with `🏜\uFE0F`, and the other way round, because chat clients differ in which form they send.

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
| `->` | store |
| `-->` | append |
| `(` | group open |
| `)` | group close |
| `;` | **reserved.** MUST produce parse error `E_RESERVED_OPERATOR`. |

Every other chunk is a **word**. So `a|b`, `a->b`, `>`, `>>`, `>:(`, `;)`, `(lol)`, `&` and `|||` are all ordinary words.

> **Changed in 2.0:** the store operators were `>` and `>>`, which are now plain words. For one release, a typed Line where a `>` or `>>` chunk is followed by a variable (`> channel.x`) fails with `E_UNEXPECTED_OPERATOR` and the hint `> is plain text now: store with ->`. Stored bodies were rewritten when they moved to 2.0 (§11).

### 2.4 Quoted strings

- `"` starts a quoted segment, and the next unescaped `"` ends it.
- Inside a quoted segment, WS and operator characters are literal, while escapes (§2.5) and placeholders (§2.7) are still processed.
- `'` has no special meaning anywhere.
- A quoted segment left unterminated at end of input MUST produce `E_UNTERMINATED_QUOTE`.
- `""` is a valid empty argument.

### 2.5 Escapes

- `\` followed by any character yields that character literally, and removes any special meaning it has (`\"`, `\\`, `\{`, `\}`, `\|`).
- A chunk made only of an escaped operator (`\|`, `\&&`, `\->`, `\(`) is a word.
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
- A name MUST NOT consist only of digits (`E_BAD_NAME`). After the prefix, such a chunk is either chat (`!100`) or the start of a bare expression line (§3.5). Custom commands can't take such a name.
- Inside an expression, a command after an operator MAY carry the channel prefix. If it does, the prefix is removed before name matching.

### 2.7 Placeholders

```ebnf
placeholder  = "{" , [ WSo ] , ( substitution | or_expr , [ WSo , "??" , WSo , fallback ] ) , [ WSo ] , "}" ;
substitution = "!" , invocation , [ WSo ] ;                 (* one command, §7.7 *)
fallback     = { fb_char | escape | placeholder } ;         (* text, may be empty *)
fb_char      = ? any character except "{", "}", "\" ? ;
WSo          = ? optional WS ? ;
```

- An unescaped `{` always starts a placeholder, both inside and outside quotes.
- A placeholder holds an **expression** (§2.8). `{channel.deaths}`, `{$chatter.display}`, `{arg.1:int}` and `{channel.deaths * 2 > 10}` are all placeholders.
- The top-level `??` is special: what follows it is **text**, which MAY contain further placeholders, so `{channel.city ?? somewhere near {$channel.name}}` works. Inside an expression, `??` takes an expression on its right (§2.8).
- `{!cmd args}` is a **command substitution**: one invocation whose value is its data, or its message when the data is null (§7.7). The `!` is fixed, whatever the channel prefix is. A substitution has no `??` of its own; nest it to give one: `{ {!weather x} ?? unknown }`.
- If the placeholder is malformed, or its root isn't registered in namespaces.md, parsing MUST fail with `E_BAD_PLACEHOLDER`. The error message SHOULD suggest `\{`.
- An unescaped `}` outside a placeholder is literal.
- Placeholders MAY nest, in fallbacks and in expressions. Nesting depth MUST NOT exceed `MAX_PLACEHOLDER_NESTING (4)`.
- The fallback text is trimmed of leading and trailing WS.
- **Indexing:** result references (`{_1}`) and argument positions (`{arg.1}`) are **1-based**. List indexes inside values (`{_1[items][0]}`) are **0-based**, and a negative index counts from the end (`[-1]` is the last item).

> **Changed in 2.0.** For one release, a typed Line points the old spellings at the new ones with `E_BAD_PLACEHOLDER`: `{1}` gives the hint `a result is {_1} now`, and `{chatter.display}` gives `the bot's fields start with $: {$chatter.display}`.

### 2.8 Expressions

```ebnf
expression  = or_expr , [ "??" , expression ] ;
or_expr     = and_expr , { "or" , and_expr } ;
and_expr    = not_expr , { "and" , not_expr } ;
not_expr    = "not" , not_expr | comparison ;
comparison  = sum , { compare_op , sum } ;                  (* chained: a < b < c *)
compare_op  = "==" | "!=" | "<=" | ">=" | "<" | ">" | "in" | "not" , "in" ;
sum         = product , { ( "+" | "-" ) , product } ;
product     = unary , { ( "*" | "//" | "/" | "%" ) , unary } ;
unary       = "-" , unary | postfix ;
postfix     = atom , { ":" , accessor | "[" , key , "]" } ;
accessor    = "len" | "keys" | "values" | type ;
key         = ident | expression ;                          (* a bare word is a literal key *)
atom        = number | string | "true" | "false" | "(" , expression , ")" | placeholder
            | bot_field | result_ref | arg_ref | "args" | path_ref | variable ;
bot_field   = "$" , bot_root , "." , ident ;
bot_root    = "chatter" | "channel" | "publisher" | "bot" | "now" ;
result_ref  = "_" , [ digit_nz , { digit } ] , [ "." , ( "code" | "message" | "data" ) ] ;
arg_ref     = "arg" , "." , ( digits , [ "+" | "+raw" ] | ident ) ;
path_ref    = path_root , { "." , ident } ;
path_root   = "event" | "match" | "cooldown" | "denied" | "run" | "cmd" ;
variable    = var_ns , "." , var_name ;                     (* §3.1 *)
number      = digits , [ "." , digits ] , [ ( "e" | "E" ) , [ "+" | "-" ] , digits ] ;
string      = '"' , { ? any character except '"' and "\" ? | "\" , ? any character ? } , '"' ;
type        = type_name | "choice(" , choice_item , { "," , choice_item } , ")" ;
type_name   = "str" | "int" | "float" | "bool" | "range" | "duration" | "user" | "url" | "list" | "map" ;
ident       = ( letter | "_" ) , { letter | digit | "_" } ;
```

- **Spaces** between the tokens of an expression are optional. The words `and`, `or`, `not`, `in`, `true` and `false` end at the next character that can't be part of an `ident`.
- **Dots and brackets.** `.` walks only names the bot defines: a namespace, a variable's name, a `$` field, a result's `code`/`message`/`data`, an argument, and the fields of `event`, `match`, `cooldown`, `denied`, `run` and `cmd`. `[ ]` walks into **values**, so a map key never collides with a name the bot defines. `{channel.stats.kills}` is `E_BAD_PLACEHOLDER` with the hint `read inside a value with [ ]: {channel.stats[kills]}`.
- **Bracket keys.** A bare word followed by `]` is a literal key: `[kills]`. Anything else is an expression: `["best run"]`, `[0]`, `[-1]`, `[arg.1]`, `[$now.date]`, `[_1]`, `[arg.1 + 1]`. `_` and `_N` are always result references, never literal keys.
- **`$` fields** take exactly one field: `{$chatter.display}`, `{$now.date}`. The fields are listed in namespaces.md. `{$chatter}` or `{$chatter.a.b}` is `E_BAD_PLACEHOLDER`.
- **Results.** `_` is the previous result and `_N` the result of invocation N (§6.4). A result has only `.code`, `.message` and `.data`: `{_1.celsius}` is `E_BAD_PLACEHOLDER`, and the data is read with brackets, `{_1[celsius]}`.
- **Arguments.** `{arg.2+}` is arguments 2 onward only where the `+` is followed by `}`, `:`, `]`, WS or the end. Otherwise `+` is addition: `{arg.1+1}` is `arg.1 + 1`. An argument is read into with brackets: `{arg.1[name]}`.
- **Strings** are literal. `"…"` inside an expression holds no placeholders, and `\` makes the next character literal.
- **Numbers.** An integer has at most `MAX_INT_DIGITS (18)` digits, and a float literal must be finite. Otherwise `E_EXPR_SYNTAX`.
- **Operators we don't have.** `**`, `>>`, `<<`, `&&`, `||`, `|`, `&`, `^`, `~`, `!` and `=` inside an expression, and an unknown `:name`, give `E_UNKNOWN_OP`. Any other stray character after a complete expression gives `E_EXPR_SYNTAX`.
- **Depth.** Parentheses, brackets, unary operators and placeholders together MUST NOT nest deeper than `MAX_EXPR_DEPTH (32)`, or parsing fails with `E_EXPR_TOO_DEEP`.
- **Store operators end an expression.** A `-` that starts a `->` or `-->` token is not subtraction, so `check {x} -> channel.y` stores the check's result.

**Precedence**, from tightest to loosest (Python's order):

| # | Operators | Associativity |
|---|-----------|---------------|
| 1 | literals, references, `( … )`, placeholders, `{! … }` | — |
| 2 | `[ … ]`, `:len` `:keys` `:values` `:int` … | left |
| 3 | unary `-` | right |
| 4 | `*` `/` `//` `%` | left |
| 5 | `+` `-` | left |
| 6 | `==` `!=` `<` `<=` `>` `>=` `in` `not in` | chained |
| 7 | `not` | right |
| 8 | `and` | left |
| 9 | `or` | left |
| 10 | `??` | right |

`1 < x < 3` means `1 < x and x < 3`, with `x` evaluated once. Evaluation is described in §7.8.

---

## 3. Syntax

### 3.1 Grammar

```ebnf
line        = line_start_check , expr , EOF ;          (* Line context *)
body        = expr , EOF ;                             (* all other contexts *)

expr        = logical ;
logical     = pipeline , { WS , ( "&&" | "||" ) , WS , pipeline } ;
pipeline    = stage , { WS , "|" , WS , stage } ;
stage       = primary , [ WS , ( "->" | "-->" ) , WS , varref ] ;
primary     = group | ifelse | invocation ;
group       = "(" , WS , expr , WS , ")" ;
ifelse      = [ prefix , [ prefix_gap ] ] , "ifelse" , WS , word , WS , group , [ WS , group ] ;   (* §8.2 *)
invocation  = [ prefix , [ prefix_gap ] ] , name , ( WS , expression     (* check, calc: §8.1 *)
                                                   | { WS , word } , [ raw_tail ] ) ;
word        = chunk ;                                  (* §2.3, not an operator token *)
raw_tail    = WS , ? remainder of input, see §3.3 ? ;
varref      = var_ns , "." , var_name , { "[" , key , "]" } ;   (* key: §2.8 *)
var_ns      = "chatter" | "channel" | "channel.chatter"
            | "publisher" | "publisher.chatter" | "publisher.channel" | "publisher.channel.chatter" ;
var_name    = "a".."z" , { "a".."z" | "0".."9" | "_" } ;  (* max 32; not reserved, namespaces.md §5 *)

line_start_check = { "(" , WS } , prefix , [ prefix_gap ] , name_char_1      (* lookahead only *)
                 | prefix , [ prefix_gap ] , expression_line ;             (* §3.5 *)
prefix_gap      = WS ;                          (* non-ASCII prefixes only, §2.1 *)
```

- The `WS` around operator tokens is implied by the chunking rule (§2.3). The grammar writes it out for clarity.
- A `varref` MUST be literal. Placeholders in it MUST produce `E_DYNAMIC_VARREF`. Its bracket keys are expressions (§2.8), evaluated when the store runs: `-> channel.quotes[_1]`.
- `check` and `calc` (not `@check`) take the rest of their stage as **one expression**, written without braces. It ends at the end of input, at an operator token, or at the `}` that closes a `{!…}`: `!check {channel.deaths ?? 0} > 10 && echo rough day`.

### 3.2 Precedence and associativity

From tightest binding to loosest:

| Level | Construct | Associativity |
|-------|-----------|---------------|
| 1 | `( … )` group | — |
| 2 | `->` / `-->` store, applied to one primary | — (at most one store per stage) |
| 3 | `\|` pipe | left |
| 4 | `&&`, `\|\|` | left, equal precedence |

Examples:
- `a && b | c -> x || d` parses as `((a && (b | (c -> x))) || d)`. The store applies only to `c`.
- To store a pipe's result, group it: `( b | c ) -> x`.
- Operators inside an expression (§2.8) are a separate level: they live inside one argument, or inside the expression of `check`/`calc`, and never cross an operator token.

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

Example: `!cc add roll !random 1-{arg.1:int ?? 20} | echo {$chatter.name} rolled {_1}` stores the body exactly as typed.

### 3.4 Parse errors

Parse errors produce a Result with **the error's own code** (200–219, §6.2) and message `parse error: <E_CODE> at <column>: <hint>`.

- The message is **only sent** if the first invocation of the line resolves to a command the invoker is permitted to run (§5, §6.6).
- Otherwise the line is silently ignored. This keeps typos in normal chat, like `!` followed by text, from producing bot noise.

| Error | Condition |
|------|-----------|
| `E_UNTERMINATED_QUOTE` | `"` not closed |
| `E_BAD_PLACEHOLDER` | malformed `{…}`, unknown root, bad type, nesting too deep, `.` into a value, a v1 spelling in a typed line (§2.7) |
| `E_RESERVED_OPERATOR` | `;` used as an operator |
| `E_UNEXPECTED_OPERATOR` | operator where a command is expected, e.g. `\| echo`, `a && && b` |
| `E_MISSING_OPERAND` | expression ends with an operator (`!a \|`, `!a ->`, `(` at end of input) |
| `E_UNBALANCED_GROUP` | `(` without `)`, or the reverse |
| `E_BAD_NAME` | a command position holds something that isn't a valid name (e.g. `!echo"x"`, where a name must be followed by whitespace or end of input, or a name made only of digits) |
| `E_DYNAMIC_NAME` / `E_DYNAMIC_VARREF` | placeholder in a name or store target |
| `E_BAD_VARREF` | store target isn't a valid `varref` |
| `E_RAW_TAIL_POSITION` | raw-tail command combined with other invocations |
| `E_TOO_LONG` | input over `MAX_EXPR_CHARS` (2000 for bodies; chat lines are capped at 500 by Twitch) |
| `E_EXPR_SYNTAX` | a malformed expression: an operator with no operand, an unclosed `(` or `[`, a number too long, `ifelse` without its condition or `( … )` |
| `E_UNKNOWN_OP` | an operator or accessor the language doesn't have (`**`, `=`, `:upper`, …, §2.8) |
| `E_EXPR_TOO_DEEP` | an expression nested deeper than `MAX_EXPR_DEPTH` (§2.8) |
| `E_INTERNAL` | the parser failed without a named error. This is a bug (Appendix C.8). |

`( )` produces `E_UNEXPECTED_OPERATOR`, because the `)` sits where a command is expected.

**Appendix C is the normative parsing definition.** The grammar in §3.1 is explanatory.

### 3.5 Bare expression lines

In the Line context, a message is a **bare expression line** when:

1. the first character after the prefix (and the prefix gap) is a digit, `{`, `(` or `-`,
2. everything after the prefix parses as one expression (§2.8), and
3. that expression contains an operator: unary `-`, arithmetic, a comparison, `not`, `and`, `or` or `??`.

The line then runs as `calc <expression>` (§8.1): `!1 + 3` says `4`, `!(2 + 3) * 4` says `20`, and `!{channel.deaths} * 2` says twice the counter. `calc` is an ordinary command here, so its permission, cooldown and toggle apply.

A line that fails any of the three conditions is parsed as usual. `!100` alone is chat, not a command (a name MUST NOT be only digits, §2.6), and `!8ball` is still the command `8ball`.

---

## 4. Abstract syntax tree

The parser MUST produce this AST. It is the stable contract with the runtime, and later syntax versions MUST map onto it or extend it.

```python
Node = And | Or | Pipe | Group | Store | IfElse | Invocation

And(left: Node, right: Node)
Or(left: Node, right: Node)
Pipe(left: Node, right: Node)
Group(inner: Node)
Store(inner: Node, target: VarRef, append: bool)
IfElse(cond: Arg, then: Group, else_: Group | None)
Invocation(index: int,               # 1-based, pre-order source order within the scope (§6.4);
                                     # -1, -2, … for a {!…} substitution
           name: str, personal: bool,
           args: list[Arg], raw_tail: str | None,
           span: tuple[int, int],
           expr: Expr | None)        # check and calc: their expression (§3.1), and no args
Arg = list[Text | Placeholder]       # concatenated parts of one chunk
Text(value: str)
Placeholder(expr: Expr, fallback: list[Text | Placeholder] | None, span: tuple[int, int])

Expr = Lit | Ref | VarRef | Index | Access | Unary | Binary | Compare | Subst | Placeholder
Lit(value: int | float | str | bool)
Ref(root: str, path: list[str])      # $chatter.name, _, _2.code, arg.1, args, event.user.name
VarRef(namespace: str, name: str, path: list[Expr])   # channel.stats[kills]; path only in store targets
Index(target: Expr, key: Expr)       # x[key]
Access(target: Expr, name: str, choices: list[str])   # :len :keys :values, or a type such as :int
Unary(op: "-" | "not", operand: Expr)
Binary(op: "+" | "-" | "*" | "/" | "//" | "%" | "and" | "or" | "??", left: Expr, right: Expr)
Compare(first: Expr, rest: list[tuple[op, Expr]])     # a < b <= c
Subst(inv: Invocation)               # {!cmd args}
```

A variable read inside an expression is a `VarRef` with an empty path, followed by `Index` nodes: `{channel.stats[kills]}` is `Index(VarRef(channel, stats), Lit("kills"))`. A unary `-` applied to a number literal is folded into the literal.

Each AST MUST record `syntax_version = "2.0"`. Custom command versions (ADR-0009), triggers and callbacks MUST store the syntax version they were parsed with (§11).

---

## 5. Static semantics (resolution and preflight)

After parsing, and **before any invocation executes**, the implementation MUST run the steps below over the whole AST. That includes branches that might not run, and custom command bodies expanded recursively up to `MAX_CC_DEPTH (3)`.

### 5.1 Name resolution

For each `Invocation` in channel C for invoker U:

1. If `personal` is set (`@name`): U's personal alias `name`, otherwise unresolved.
2. A public member of a **system pack** named `name` (§8). System packs resolve in every channel without
   being published, and can't be disabled or shadowed.
3. A built-in command or built-in alias named `name` that is enabled in C.
4. Inside the body of a command that arrived through a pack P: an **internal** member of P named `name`.
5. A publication named `name` in C with status `active`.
6. U's personal alias `name`.
7. Otherwise unresolved. Result code **127**.

An internal pack member is reachable only through step 4, so typing its name, or calling it from a body
outside its pack, is unresolved (127), and `help` doesn't list it. A channel publication of the same name
doesn't shadow it inside the pack's own bodies.

Custom command names MUST NOT equal a sentinel name (§8) or a `core_admin` command name.

### 5.2 Preflight checks

For each resolved invocation, in this order:

| # | Check | Failure code |
|---|-------|--------------|
| 1 | Toggles and channel capabilities (ADR-0006, ADR-0007) | 127 (a disabled command behaves as unknown) |
| 2 | Permission: invoker rank vs. required role or allowed roles | 126 |
| 3 | Input mode: a command with `input=NONE` must not be the right operand of `\|` | 221 (`E_INPUT_NOT_ACCEPTED`) |
| 4 | Placeholder validity for this context (§7.2), and `_N` with 1 ≤ N < this invocation's index | 222 (`E_BAD_REFERENCE`) |
| 5 | Store targets: namespace valid for the context, and write permitted per variable-access-matrix.md | the variable error (§6.2) for an invalid target, 126 for denied |
| 6 | Custom command expansion: depth ≤ `MAX_CC_DEPTH`, no cycles | 224 (`E_CC_DEPTH`) / 223 (`E_CC_CYCLE`) |
| 7 | Total invocations after expansion ≤ `MAX_INVOCATIONS (16)`; sentinels count, a custom command's body counts at every call, and an `ifelse` counts as its larger branch | 220 (`E_TOO_MANY`) |

If any check fails, **no invocation executes.** The expression's result is the first failure in source order. Output rules for these codes are in §6.6.

**Error identifiers.** The `E_…` names above are carried in the Result's `data.error`, with the human-readable text in `message` — for example `Result(222, "{_2} refers to a command that runs later", {"error": "E_BAD_REFERENCE", "reference": "{_2}"})`. Parse errors (§3.4) also put their code in `data.error` alongside `data.column`. This keeps chat replies readable while `!explain`, `/parse` and the editor get stable identifiers.

**Cooldowns are not a preflight check.** They are checked when evaluation reaches an invocation (§6.3), and an invocation on cooldown **fails with code 128** like any other failure. So `||` can route around it — `!a || !b` runs `b` when `a` is on cooldown — and `&&` stops at it. An invocation that is never reached is never held to its cooldown and never starts one. If the final Result is 128, output stays silent and the `on_cooldown` callback runs (§6.6).

The cooldown is claimed immediately before the invocation executes, so a command repeated in one line meets the cooldown its own first run started: in `!dice && !dice`, the second `dice` fails with 128 unless its cooldown is zero.

> **Changed in 1.1:** in 1.0, cooldowns were check 3 of this table. Every invocation was checked up front, including branches that never ran, and one command on cooldown stopped the whole line.

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
- The serialized size of `data` MUST NOT exceed `MAX_DATA_BYTES (4096)`. Larger data yields code 231 (`E_DATA_TOO_LARGE`).
- On a runtime or preflight failure with a named error, `data` is a map carrying that name: `{"error": "E_…", …}` (§5.2).
- `message` MUST NOT exceed `MAX_MESSAGE_CHARS (2000)` and is truncated with `…`.

### 6.2 Exit codes

| Code | Name | Produced by |
|------|------|-------------|
| 0 | OK | success |
| 1 | FAIL | generic command failure |
| 2 | USAGE | a command's own bad arguments or usage (§5.3) |
| 3 | NOT_FOUND | lookup found nothing |
| 124 | TIMEOUT | stage or expression timeout |
| 125 | UPSTREAM_LIMITED | external API rate limit |
| 126 | DENIED | permission or write denied |
| 127 | UNKNOWN | unresolved or disabled command |
| 128 | COOLDOWN | cooldown active |
| 130 | CANCELLED | moderation cancellation (§6.7) |
| 200–1023 | `E_…` | a named runtime error, each with its own code (below) |

Codes run from 0 to 1023. Commands MUST use 1–99 for their own failures (4 included). Codes 100–1023 are reserved for the runtime, and a command that *returns* one is corrected to code 1. A built-in MAY *raise* a runtime failure that carries a reserved code when the runtime owns that meaning: `!var` raises 126 when a write is denied, so the denial behaves like any other (silent, with the `on_denied` callback), and raises the storage errors below for a bad write. A custom command's body that ends in a named error passes that code to its caller unchanged.

**Named errors (ADR-0018).** Every `E_…` identifier has its own code, carried alongside its name in `data.error`, so a script can tell one error from another by its code instead of parsing the message. The names and numbers live in `ErrorCode` (`runtime/result.py`) and are published by `GET /api/v1/language` as `exit_codes`. These codes carry no special behaviour: they are not silent and fire no callback.

| Block | Errors |
|---|---|
| 200–219 parse (§3.4) | 200 `E_UNTERMINATED_QUOTE`, 201 `E_BAD_PLACEHOLDER`, 202 `E_RESERVED_OPERATOR`, 203 `E_UNEXPECTED_OPERATOR`, 204 `E_MISSING_OPERAND`, 205 `E_UNBALANCED_GROUP`, 206 `E_BAD_NAME`, 207 `E_DYNAMIC_NAME`, 208 `E_DYNAMIC_VARREF`, 209 `E_BAD_VARREF`, 210 `E_RAW_TAIL_POSITION`, 211 `E_TOO_LONG`, 212 `E_EXPR_SYNTAX`, 213 `E_UNKNOWN_OP`, 214 `E_EXPR_TOO_DEEP` |
| 220–229 preflight (§5.2) | 220 `E_TOO_MANY`, 221 `E_INPUT_NOT_ACCEPTED`, 222 `E_BAD_REFERENCE`, 223 `E_CC_CYCLE`, 224 `E_CC_DEPTH` |
| 230–249 evaluation (§7.3, §7.8) | 230 `E_MISSING_VALUE`, 231 `E_DATA_TOO_LARGE`, 232 `E_TYPE`, 233 `E_DIV_ZERO`, 234 `E_OVERFLOW`, 235 `E_EXPR_BUDGET`, 236 `E_SUBST_DEPTH` |
| 250–269 values | 250 `E_INDEX`, 251 `E_KEY`, 252 `E_NOT_A_LIST`, 253 `E_NOT_A_MAP`, 254 `E_EMPTY`, 255 `E_NOT_A_NUMBER` |
| 299 | `E_INTERNAL`: the parser failed without a named error, which is a bug |
| 300–399 storage (§6.5) | 300 `E_LIST_FULL`, 301 `E_QUOTA`, 302 `E_VALUE_TOO_BIG`, 303 `E_BAD_NAMESPACE`, 304 `E_BAD_VAR_NAME`, 305 `E_TOO_MANY_NAMES` |
| 400–499 http (ADR-0020) | 400 `E_HTTP_NOT_ALLOWED`, 401 `E_HTTP_ADDRESS`, 402 `E_HTTP_TIMEOUT`, 403 `E_HTTP_TOO_BIG`, 404 `E_HTTP_STATUS`, 405 `E_HTTP_NOT_JSON`, 406 `E_HTTP_PATH`, 407 `E_HTTP_UNREACHABLE` |

Each block leaves gaps for related errors, and the rest of 100–1023 is free for new blocks. A script branches on the code of the previous result: `!var pop channel.queue || ifelse {_.code == 254} ( echo queue is empty ) ( echo {_.message} )`.

### 6.3 Evaluation

`eval(node, prev) → Result`, where `prev` is the Result visible as `_` (§6.4).

| Node | Evaluation |
|------|------------|
| `Invocation` | 1. Check moderation (§6.7). 2. If the command is on cooldown, fail with 128 (§5.2) before expanding anything. 3. Expand the arguments (§7.3) with `_` = prev. 4. Validate the parameters (§5.3). 5. Claim this invocation's cooldowns: check them again and commit them in one step, failing with 128 if they are no longer clear. 6. Execute with `stdin` (the pipe input, or null) under `STAGE_TIMEOUT (3 s)`. 7. Record the Result under its index. |
| `Pipe(L, R)` | `r = eval(L, prev)`. If `r.code ≠ 0`, return `r`. Otherwise return `eval(R, r)` with **stdin = r**. |
| `And(L, R)` | `r = eval(L, prev)`. If `r.code ≠ 0`, return `r`. Otherwise return `eval(R, r)` with stdin = null. |
| `Or(L, R)` | `r = eval(L, prev)`. If `r.code = 0`, return `r`. Otherwise return `eval(R, r)` with stdin = null. |
| `Group(E)` | `eval(E, prev)`: the result of the last invocation executed inside. |
| `Store(E, target, append)` | `r = eval(E, prev)`. If `r.code = 0`, evaluate the target's bracket keys with `_` = r and buffer the write (§6.5). Return `r` unchanged, or the write's error. |
| `IfElse(C, T, E)` | Expand the condition `C` like an argument with `_` = prev. If it is missing, or its expansion fails, return that failure and run neither branch. If it holds (§7.8), return `eval(T, prev)`; otherwise return `eval(E, prev)`, or `Result(0, null, null)` when there is no `E`. The chosen branch receives this node's stdin. |

- **Pipe vs. and:** both stop on failure and both expose the left result as `_`. Only a pipe *also* delivers it as `stdin` to the command. Commands that accept input act on `stdin` without placeholders (e.g. `!upper`).
- **Pipe stdin into a group:** `L | ( A && B )` delivers stdin to the **first invocation evaluated** inside the group, which is `A`.
- The whole expression is bounded by `EXPR_TIMEOUT (6 s)`. When it expires, the running invocation is cancelled and the expression returns code 124.
- **`ifelse`** is checked up front like any other node: both branches go through preflight (§5) and appear in `!explain`, and the larger one counts toward `MAX_INVOCATIONS`, but only the chosen branch runs. The invocations of the other branch never execute, so their `_N` are missing.
- **A branch's permissions wait for the branch.** Inside an `ifelse` branch, a command the invoker may not run (126, or 127 for a missing capability, §5.2 check 2) doesn't stop the line up front. It returns that refusal when, and only if, its branch is chosen, so a condition can guard it: `ifelse {$chatter.is_mod} ( shoutout {arg.1} || true )`. Unknown names, bad references and every other check still fail the whole line before anything runs.

### 6.4 Scopes, `_` and `_N`

- **A scope** is one expression: a line, or one expansion of a custom command body. Every expansion has its own scope.
- **Indexes:** invocations are numbered 1..n in pre-order source order within their scope, whether or not they execute. A custom command invocation is one index in the outer scope. Its body's invocations belong to the inner scope.
- **`_N`:** the Result of invocation N in the current scope. If N didn't execute (it was on a skipped branch), the reference is **missing** (§7.5). `{!…}` substitutions (§7.7) are not numbered in the scope.
- **`_`:** the `prev` argument of the current evaluation:
  - For the first evaluated invocation of a Line, Trigger, Listener or Callback scope, `_` is missing.
  - For the first evaluated invocation of a Body scope, `_` is the custom command's stdin if it has one, otherwise missing.
  - After an operator, `_` is the left operand's Result (per §6.3).
- **Reading a result.** `{_N}` alone renders the data if it is a scalar, otherwise the message. `.code`, `.message` and `.data` select one part. In an expression, `[ ]` and `:` work on the data, whatever it is: `{_1[celsius]}`, `{_1:len}`, `{_1[tags][-1]}`, and `_1 + 1` adds to the data.
- A bare number is always a number: `{1}` renders `1`, and `{1 + 1}` renders `2`. For one release a typed line refuses `{1}` and `{1.x}` alone with the hint `a result is {_1} now`, so a 1.0 habit fails loudly instead of printing a number (§6.4, A.2 #25).
- **A custom command invocation's Result** is the Result of its body expression.

### 6.5 Variable writes

- **Buffering:** store operators and variable-writing commands add entries to a per-expression **write buffer**. Reads within the same expression MUST see buffered values (read-your-writes).
- **Committing:** the buffer is committed atomically after evaluation finishes, **regardless of the final code**, unless the expression was cancelled (§6.7) or timed out. In those cases the buffer is discarded.
- **Stored value for `->`:** `data` if it is not null, otherwise `message`. If both are null, the store is skipped and the result passes through; `!explain` and `command_runs` note the skip.
- **Paths.** A target may name a place inside the variable: `-> channel.stats[kills]`, `--> channel.log[today]`, `-> channel.quotes[_1]`. Keys are evaluated when the store runs, with `_` = the stored result. A missing map on the way is created. Going inside a value that is neither a map nor a list, or giving a list a key that isn't a whole number, fails the store with 253 (`E_NOT_A_MAP`). An index outside `-len … len-1` fails with 250 (`E_INDEX`); a write never extends a list. `!var set|incr|del ns.name[path]` take the same paths; `del` fails with 251 (`E_KEY`) on a key that isn't there. `!var pop ns.name [index]` removes and returns one list item (default the last): 252 (`E_NOT_A_LIST`) on something else, 254 (`E_EMPTY`) on an empty list.
- **`-->` (append):**

| Existing value | Effect |
|----------------|--------|
| missing | the variable becomes `[v]` |
| list | `v` is appended. A list already at its owner's list limit (default 100 items, see quotas below) fails the store with 300 (`E_LIST_FULL`), and nothing is dropped |
| anything else | the store fails: the Store node returns code 252 (`E_NOT_A_LIST`) instead of `r`, and nothing is buffered |

- **Limits:** a value over its owner's value cap or a space over its owner's name limit (default 200 names, see quotas below) fails when the write is buffered, with 302 (`E_VALUE_TOO_BIG`) or 305 (`E_TOO_MANY_NAMES`).
- **Quotas (ADR-0019):** every variable counts against one **owner**, named by the first segment of its namespace: `chatter.*` against the chatter, `channel.*` and `channel.chatter.*` against the channel, and `publisher.*` against the command's publisher. Each owner has a quota for all its values together (default 1 MB) and a cap on any one value (default 256 KB, at most 1 MB). Each owner also has a list limit (default 100 items per list, at most 10,000) and a name limit (default 200 variables per space, at most 10,000). Admins change any of the four, as a default or for one owner, with `!admin quota|valuecap|listitems|names` or the API; an owner without an override of its own follows the default. Both are checked at commit, in the commit's transaction: a value over its owner's cap gives 302 (`E_VALUE_TOO_BIG`), and a commit that leaves an owner over its quota gives 301 (`E_QUOTA`). Either way nothing in the run is stored. A commit that only shrinks what an owner stores always succeeds, so lowering a quota never blocks a clean-up. Sizes are the stored JSON's UTF-8 bytes. `!var usage [ns]` shows an owner's use.

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
- **Sending path:** the message goes through the Outbox: the moderation recheck, then the badword filter, then the link rule (outside channels where the bot is a moderator or VIP, or its own channel, every `.` in a link's host becomes ` dot `; ADR-0019), then chunking into at most `MAX_CHAT_MESSAGES (2)` messages of 500 characters each, then rate limiting.
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

The authoritative list is [namespaces.md](namespaces.md) §1–§5. Roots in 2.0:

- results: `_`, `_N`
- arguments: `arg`, `args`
- the bot's fields, always with `$`: `$chatter`, `$channel`, `$publisher`, `$bot`, `$now`
- variables, never with `$`: `chatter`, `channel`, `publisher` (and their nested spaces)
- context paths: `cmd`, `event`, `match`, `cooldown`, `denied`, `run`

`$chatter.title` and `chatter.title` are different things: the first is a field the bot supplies, the second a variable. The only reserved variable names are the structural segments (`chatter` under `channel.`, `publisher.` and `publisher.channel.`, and `channel` under `publisher.`) and the result fields `data`, `code`, `message`, `public` and `root`.

### 7.2 Availability by context

| Root | Line | Body | Trigger | Listener | Callback |
|------|------|------|---------|----------|----------|
| `_`, `_N` | ✔ | ✔ | ✔ | ✔ | ✔ |
| `arg`, `args` | — | ✔ | ✔ (event input text as arguments) | — | — |
| `$chatter.*`, `chatter.*` | ✔ | ✔ | ✔ (event user, if any) | ✔ | ✔ |
| `$channel.*`, `channel.*`, `channel.chatter.*` | ✔ | ✔ | ✔ | ✔ | ✔ |
| `$publisher.*`, `publisher.*` (all four) and `cmd` | — | ✔ | — | — | — |
| `$bot`, `$now`, `run` | ✔ | ✔ | ✔ | ✔ | ✔ |
| `event` | — | inherited, if the body runs from a trigger | ✔ | — | — |
| `match` | — | — | — | ✔ | — |
| `cooldown` / `denied` | — | — | — | — | ✔ (matching callback type) |

A root that isn't available in the context fails preflight with code 222 (`E_BAD_REFERENCE`). Timers have no chatter, so `$chatter.*` and `chatter.*` there are missing.

### 7.3 Expansion

Placeholders are expanded **immediately before their invocation executes** (§6.3 step 1), and never earlier.

1. **Evaluate** the placeholder's expression (§7.8) to a Value, or to **missing**:
   - A missing key or an out-of-range index gives missing. `[ ]` on something that is neither a map nor a list (nor missing) fails with 232 (`E_TYPE`).
   - A bare `{_N}` / `{_}` gives `data` if it is a scalar (not null, list or map), otherwise `message` (§6.4).
2. **Type:** a `:type` accessor converts per §7.4. A conversion failure is treated as missing.
3. **Fallback:** if the value is missing, null or the empty string, and `??` is present, the fallback is expanded (recursively) and used, and no type is applied to it. Without `??`, the invocation fails with code 230 (`E_MISSING_VALUE: {ref}`) and does not execute. Any other failure while evaluating (232–236) fails the invocation with that code; `??` does not catch it.
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

| Type | Accepts (after trimming) | Converted value | Read with |
|------|--------------------------|-----------------|-------|
| `str` | anything | str | — |
| `int` | `^[+-]?\d{1,18}$` | int | — |
| `float` | decimal or exponent notation; not NaN or Inf | float | — |
| `bool` | `true false yes no on off 1 0` (case-insensitive) | bool | — |
| `range` | `^(-?\d+)-(-?\d+)$` with lo ≤ hi | map | `[lo]`, `[hi]` |
| `duration` | `^(\d+h)?(\d+m)?(\d+s)?$`, non-empty, or a plain integer (seconds) | int (seconds) | — |
| `user` | `@?[A-Za-z0-9_]{1,25}`, resolved through Helix (cached; counts toward the stage timeout) | map | `[id]`, `[name]`, `[display]` |
| `choice(a,…)` | one of the listed items (case-insensitive) | str (canonical item) | — |
| `url` | absolute `http`/`https` URL passing the URL safety policy | str | — |
| `list` | a single placeholder whose value is a list passes through; otherwise JSON text `[…]` | list | `[i]`, `[-i]`, `:len` |
| `map` | a single placeholder whose value is a map passes through; otherwise JSON text `{…}` | map | `[key]`, `:len`, `:keys`, `:values` |
| `any` | anything: a single placeholder's value passes through with its type, anything else is text | any | whatever the value has |

`int`, `float` and `duration` params MAY declare `min`/`max`. `str` MAY declare `max_len`.

### 7.5 Missing values

A reference is **missing** when:
- its lookup fails (§7.3.1)
- it refers to `_N` that didn't execute
- `_` has no previous result
- a `{!…}` substitution has neither data nor a message
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

### 7.7 Command substitution

`{!cmd args}` runs one invocation while its enclosing invocation expands its arguments (§6.3 step 3), and its value is the command's `data` if it is not null, otherwise its `message`, otherwise missing.

- It is an ordinary invocation: name resolution, preflight (permission, toggles, input mode), cooldowns and `MAX_INVOCATIONS` apply to it as to a pipeline stage. It has no stdin. Its `_` is the enclosing invocation's `_`.
- Substitutions are numbered −1, −2, … and don't take a `_N` index.
- If it fails, the enclosing invocation fails with the same Result, so `{!fail 4 bad}` fails with code 4 and `{!nosuch}` with 127.
- Substitutions nest (`{!echo {!random 1-6}}`) at most `MAX_SUBST_DEPTH (3)` deep. Deeper nesting fails with 236 (`E_SUBST_DEPTH`) when it runs.
- A `??` applies to a missing value, never to a failure: `{ {!weather x} ?? unknown }` covers weather succeeding with no data, not weather failing.

### 7.8 Expression evaluation

- **Values.** Literals are what they say. A reference reads as in §7.3. A variable that doesn't exist is missing.
- **Missing propagates.** An operator or accessor applied to a missing operand gives missing, and `??` replaces it. `a ?? b` evaluates `b` only when `a` is missing, null or the empty string.
- **`and`, `or`** short-circuit and return one of their operands, as in Python: `{0 or 5}` is `5`, `{3 and 4}` is `4`. `not` gives `true` or `false`.
- **Truthiness** (Python's rules): `false`, `0`, `0.0`, `""`, `[]` and `{}` are false; everything else is true. Missing is neither: `check` and `ifelse` fail on it with 230.
- **Conditions read text like an argument.** In `check` and `ifelse`, a text value is first read as it would be as an argument, so a stored `"false"` or `"0"` is false, the same as typing `!check false`.
- **Arithmetic** needs numbers. Numeric text counts as a number (`"10"` is 10), `true` and `false` don't. `+` also joins two lists. Anything else fails with 232 (`E_TYPE`). `/` always gives a float, `//` and `%` round toward minus infinity as in Python, and all three fail with 233 (`E_DIV_ZERO`) on zero. An int past 18 digits, or a float past the finite range, fails with 234 (`E_OVERFLOW`).
- **Comparisons** are case-sensitive. `==`, `!=`, `<`, `<=`, `>`, `>=` compare as numbers when both sides are numbers, otherwise as rendered text (§7.6). Lists and maps are equal only to an equal list or map, and can't be ordered (232). `a in b` looks for an item of a list, a key of a map, or a piece of text; anything else is 232. Comparisons chain: `a < b < c` is `a < b and b < c` with `b` evaluated once.
- **Accessors.** `:len` counts a list, a map or text. `:keys` and `:values` need a map (253 `E_NOT_A_MAP`). A type (`:int`, `:user`, …) converts per §7.4.
- **Budget.** One run evaluates at most `MAX_EXPR_OPS (1000)` operators, accessors and index steps across all its expressions. The one that passes the limit fails with 235 (`E_EXPR_BUDGET`).
- **No `eval`.** Expressions are evaluated by the language's own evaluator over the AST; nothing is handed to a host language.

---

## 8. Sentinel commands

They can't be disabled or shadowed, have no cooldowns, require role `everyone`, are logged at level `off`, and **count toward `MAX_INVOCATIONS`**. `true`, `fail` and `echo` are primitives in the `core` module. `false` and `default` are **derived**: custom commands in the bot-owned `core` system pack (§5.1 step 2), whose bodies may call only other sentinels. A call to a derived sentinel doesn't count toward `MAX_CC_DEPTH`.

| Derived sentinel | Body |
|------------------|------|
| `false` | `fail` |
| `default` | `echo {args}` |

| Command | Arguments | Result |
|---------|-----------|--------|
| `true` | none (extra arguments → code 2) | `code 0`, `message = null`, `data` copied from `{_}` if present, otherwise null. It never repeats a failure message. *(Appendix B #1)* |
| `false` | none | `code 1`, null message and data |
| `default` | `arg.1+ : str` (required) | `code 0`, `message = data = arg.1+` |
| `fail` | `arg.1 : int` (optional, 1–99, default 1), `arg.2+ : str` (optional) | `code = arg.1`, `message = arg.2+` or null, `data = null`. If the first argument isn't an int, all arguments form the message and the code is 1. |
| `echo` | `arg.1+ : str` (optional) | `code 0`, `message = data = arg.1+` (the empty string if no arguments) |

Idioms:
- **Optional command:** `!shoutout {arg.1} || true`
- **Fallback value:** `( !weather {arg.1+} || default unknown ) -> chatter.last_weather`
- **Validation:** `check {arg.1:int} <= 20 || fail 2 max 20 dice`

### 8.1 Expression commands

These are also in `core`, with the same rules as the sentinels.

| Command | Arguments | Result |
|---------|-----------|--------|
| `check` | one expression (§3.1), written without braces | `code 0` if it holds (§7.8), `1` if it doesn't, 230 (`E_MISSING_VALUE`) if it is missing; `message = null`, `data` = the value |
| `calc` | one expression | `code 0`, `message` = the value rendered (§7.6), `data` = the value. The target of a bare expression line (§3.5) |
| `add` `sub` `mul` `div` `idiv` `mod` | `left right` | `left + - * / // % right`, said |
| `eq` `ne` `lt` `le` `gt` `ge` `in` | `left right` | `left == != < <= > >= in right`, said as `true`/`false` |
| `neg`, `not` | `value` | `-value`, `not value` |
| `and`, `or` | `left right` | as the operators (§7.8) |

The operator commands read each argument like a literal: a number, `true`/`false`, or else the text itself. So `!lt 2 10` is `true`, where comparing the text would say `false`. They fail with the same errors as the operators.

### 8.2 `ifelse`

`ifelse condition ( then … ) [ ( else … ) ]` is a special form in the grammar (§3.1), not a command: it has no permission, cooldown or index of its own. The condition is one argument, usually a placeholder: `!ifelse {channel.deaths > 10} ( echo rough day ) ( echo going well )`. Evaluation is in §6.3.

- A missing condition fails with 230 and runs neither branch, like `check`. Choose a default with `??`: `{channel.deaths ?? 0}`.
- The branches are groups and hold whole expressions, including stores and nested `ifelse`.
- Without an else branch, a false condition gives success with no output.
- A command in a branch is refused only if its branch is chosen (§6.3), which is what lets the condition guard a command not everyone may run.

---

## 9. `!explain`

`explain` is a raw-tail command (§3.3). It parses its raw argument as an expression in the context being explained: the Line context by default, or Body with `--as-body`. In the Line context the chat command adds the channel's sign when the argument doesn't start with one, so `!explain echo hi` and `!explain !echo hi` explain the same line. `POST /api/v1/explain` takes its text exactly as given.

It MUST report:
- the AST, with operator precedence made visible and invocation indexes
- name resolution for each invocation: source, owner, version and publication grants
- the preflight result for each invocation: toggle, rank vs. required role and input mode, plus both cooldowns with remaining time. A cooldown is reported, not failed: whether it blocks depends on whether evaluation reaches that invocation (§5.2)
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
| `MAX_EXPR_DEPTH` | 32 (parentheses, brackets, unary operators and placeholders together) | §2.8 |
| `MAX_INT_DIGITS` | 18 | §2.8 |
| `MAX_EXPR_OPS` | 1000 per run | §7.8 |
| `MAX_SUBST_DEPTH` | 3 | §7.7 |
| `MAX_INVOCATIONS` | 16 (after expansion) | §5.2 |
| `MAX_CC_DEPTH` | 3 | §5 |
| `STAGE_TIMEOUT` | 3 s | §6.3 |
| `EXPR_TIMEOUT` | 6 s | §6.3 |
| `MAX_DATA_BYTES` | 4096 | §6.1 |
| `MAX_MESSAGE_CHARS` | 2000 | §6.1 |
| `MAX_CHAT_MESSAGES` | 2 (500 chars each) | §6.6 |
| Items per list | 100 default per owner, 10,000 at most | ADR-0019 |
| Variables per space | 200 default per owner, 10,000 at most | ADR-0019 |
| Variable value size | 256 KB default per owner, 1 MB at most | ADR-0019 |
| Variable quota | 1 MB default per owner | ADR-0019 |

---

## 11. Versioning

- This document is **syntax version 2.0**. Changes that alter how existing valid input parses or evaluates require a **major** version change. Additions that only make previously invalid input valid (e.g. un-reserving `;`) are **minor** version changes.
- The syntax version covers the grammar and how a body parses. Document 1.1 moved cooldowns from preflight to runtime (§5.2) — a change in *policy outcome*, which 1.0 declared in advance as a minor change (Appendix B item 4). The grammar and every parse are unchanged, so `syntax_version` stays `"1.0"` and no stored body needs migrating.
- Document 1.2 gave every named runtime error its own code (§6.2, ADR-0018) where they were all code 2, and made `>>` fail on a full list instead of dropping the oldest item. Again the grammar and every parse are unchanged, so `syntax_version` stays `"1.0"`. A stored body that tests for code 2 after one of these errors now sees the new code.
- Stored bodies keep the syntax version they were parsed with. When the major version changes, the implementation MUST either keep a parser for the old version or migrate bodies automatically.
- **2.0** (ADR-0018) changes how existing input parses: `>`/`>>` became `->`/`-->`, `{N}` became `{_N}`, the bot's fields took a `$`, reads into values use `[ ]`, and placeholders hold expressions. The implementation keeps only the 2.0 parser. `scripts/migrate_v2.py` rewrites every stored 1.0 body (custom command versions, triggers, callbacks) in one transaction, checks each rewrite parses, and marks it `"2.0"`; `--dry-run` shows every rewrite first. It also lists what no rewrite can decide: bodies that read a result's `.code` (a missing placeholder is now 230, not 2), and custom commands whose names 2.0 took (`check`, `calc`, `ifelse`, the operator commands, and names made only of digits). The rewrite changes the stored version in place rather than adding a custom command version, because it doesn't change what the command does.
- Deferred features are tracked in language proposal §6: the `?` suffix, and `;` behavior.

---

## Appendix A. Conformance examples

The parser golden-test corpus MUST include at least the following cases. `⟨…⟩` shows argument boundaries.

### A.1 Lexing and parsing

| # | Input (Line context) | Expected |
|---|----------------------|----------|
| 1 | `!random 1-100 \| echo dice rolled a {_1}!` | `Pipe(random⟨1-100⟩, echo⟨dice⟩⟨rolled⟩⟨a⟩⟨{_1}!⟩)` |
| 2 | `!echo a\|b ;) a->b >:( (lol) >` | `echo⟨a\|b⟩⟨;)⟩⟨a->b⟩⟨>:(⟩⟨(lol)⟩⟨>⟩` |
| 3 | `!echo it's "a \| b" fine` | `echo⟨it's⟩⟨a \| b⟩⟨fine⟩` |
| 4 | `!echo ab"c d"e` | `echo⟨abc de⟩` |
| 5 | `!echo \| literal` | `Pipe(echo, literal)`: the second chunk is an operator |
| 6 | `!echo \\\| literal` | `echo⟨\|⟩⟨literal⟩` (escaped pipe) |
| 7 | `!echo a ; b` | `E_RESERVED_OPERATOR` |
| 8 | `!echo "unterminated` | `E_UNTERMINATED_QUOTE` |
| 9 | `!echo {friend}` | `E_BAD_PLACEHOLDER` (unknown root) |
| 10 | `!echo \{friend}` | `echo⟨{friend}⟩` |
| 11 | `( !a \|\| true ) && !b` | `And(Group(Or(a, true)), b)` |
| 12 | `!a && !b \| !c -> channel.x \|\| !d` | `Or(And(a, Pipe(b, Store(c, channel.x))), d)` |
| 13 | `!a \| && !b` | `E_UNEXPECTED_OPERATOR` |
| 14 | `!a -> {$chatter.name}` | `E_DYNAMIC_VARREF` |
| 15 | `!explain !random 1-6 \| echo {_1}` | `explain` with raw tail `!random 1-6 \| echo {_1}` |
| 16 | `!cc add roll "!random 1-6 \| echo {_1}"` | raw tail `!random 1-6 \| echo {_1}` (outer quotes removed) |
| 17 | `! random` | not a command with an ASCII prefix (no name character right after it); with `🏜` as the prefix, `🏜 random` **is** a command (§2.1 prefix gap) |
| 18 | `@alice !random 1-6` sent as a reply to alice | reply mention stripped → `random⟨1-6⟩` |
| 19 | `!echo {chatter.location ?? {channel.location ?? Lisbon}}` | nested fallback placeholder |
| 20 | `!echo {arg.1:choice(a,b) ?? a}` | typed placeholder with a choice type (valid in the Body context only) |
| 21 | `!echo {1 + 2 * 3 > 6 and not false}` | one placeholder: `and(>(+(1, *(2, 3)), 6), not(false))` |
| 22 | `!echo {1 < x < 3}` | `Compare(1, [(<, x), (<, 3)])`, chained |
| 23 | `!echo {channel.stats[kills]} {channel.log[-1]} {x["best run"]}` | `Index(channel.stats, "kills")`, `Index(channel.log, -1)`, `Index(x, "best run")` |
| 24 | `!echo {channel.stats.kills}` | `E_BAD_PLACEHOLDER`, hint `read inside a value with [ ]` |
| 25 | `!echo {1}` | `E_BAD_PLACEHOLDER`, hint `a result is {_1} now` (typed lines only) |
| 26 | `!echo a > channel.x` | `E_UNEXPECTED_OPERATOR`, hint `> is plain text now: store with ->` (typed lines only) |
| 27 | `!echo a -> channel.m[x][arg.1]` | `Store(echo⟨a⟩, channel.m[x][arg.1])` |
| 28 | `!echo {!random 1-6}` | `echo⟨Subst(random⟨1-6⟩)⟩` |
| 29 | `!check {arg.1:int ?? 0} > 10 && echo big` | `And(check(>(arg.1:int ?? 0, 10)), echo⟨big⟩)` |
| 30 | `!ifelse {x} ( echo a ) ( echo b )` | `IfElse({x}, Group(echo⟨a⟩), Group(echo⟨b⟩))` |
| 31 | `!ifelse {x} echo a` | `E_EXPR_SYNTAX`, hint `ifelse needs ( a command ) after its condition` |
| 32 | `!1 + 3` | `calc(+(1, 3))`, a bare expression line (§3.5) |
| 33 | `!100` | not a command |
| 34 | `!echo {1 ** 2}` | `E_UNKNOWN_OP` |
| 35 | `!echo {1 +}` | `E_EXPR_SYNTAX` |

### A.2 Evaluation

| # | Expression | Scenario | Final Result / sent |
|---|------------|----------|---------------------|
| 1 | `!weather Lisbon \| echo "it's {_1[celsius]}C"` | weather OK, `data.celsius = 21.5` | `0` / `it's 21.5C` |
| 2 | `!weather Nowhere \| echo "{_1[celsius]}"` | weather returns code 3 with "location not found" | `3` / `location not found` (the pipe stopped) |
| 3 | `!weather Nowhere \|\| echo "failed: {_.message}"` | as above | `0` / `failed: location not found` |
| 4 | `( !weather x \|\| default ? ) -> chatter.w` | weather fails | `0` / `?`; `chatter.w = "?"` |
| 5 | `!weather x -> chatter.w` | weather fails | `3` / weather's message; **nothing stored** |
| 6 | `!a \|\| !b && echo {_2}` | `a` succeeds | `_2` is missing → code 230 `E_MISSING_VALUE` unless `??` is used |
| 7 | `!shoutout @x \|\| true` | shoutout fails | `0` / nothing sent (`true` has no message) |
| 8 | `!foo` (unknown) | — | `127` / nothing sent |
| 9 | `!random 1-6 \| !foo` (unknown second) | — | `127` / `unknown command: foo` |
| 10 | `!mod-only-cmd` by a viewer | — | `126` / nothing, and `on_denied` runs if set |
| 11 | `!random 1-6 -> channel.x` by a viewer (channel write role = mod) | — | preflight `126` / nothing; `random` never runs |
| 12 | `!echo {_1}` | — | preflight `E_BAD_REFERENCE` (N must be < own index) |
| 13 | `!echo {7 / 2} {7 // 2} {-7 // 2}` | — | `0` / `3.5 3 -4` |
| 14 | `!echo {1 / 0} \|\| echo {_.code}` | — | `0` / `233` |
| 15 | `!echo {"a" + 1}` | — | `232` `E_TYPE` |
| 16 | `!echo {channel.missing ?? none} {0 or 5}` | `channel.missing` unset | `0` / `none 5` |
| 17 | `!ifelse {channel.deaths > 10} ( echo rough ) ( echo fine )` | `channel.deaths = 12` | `0` / `rough`; `fine` never runs |
| 18 | `!ifelse {channel.nope} ( echo a ) ( echo b )` | unset | `230` / neither branch runs |
| 19 | `!echo a -> channel.m[x][y] && echo {channel.m}` | — | `0` / `{"x":{"y":"a"}}` |
| 20 | `!echo a -> channel.s && echo b -> channel.s[k]` | — | `253` `E_NOT_A_MAP` |
| 21 | `!echo {!fail 4 bad}` | — | `4` / `bad` |
| 22 | `!echo {!echo {!echo {!echo deep}}}` | — | `236` `E_SUBST_DEPTH` |

---

## Appendix B. Open items found while writing the spec

All items were resolved in review (2026-09-16):

1. **`true` pass-through:** copies `data` only, never the message. ✔
2. **Parse-error visibility:** parse errors are sent only when the first command is runnable by the invoker. ✔
3. **Unknown later commands:** reply `unknown command: x`, unless `quiet_errors` is set. ✔
4. **Cooldowns in preflight:** kept for 1.0, so every invocation was checked, including skipped branches. **Done in 1.1 (2026-09-22):** cooldowns are a *runtime* failure (code 128) of the individual invocation, so `||` can handle them, and `!a || !b` with `a` on cooldown runs `b` (§5.2). Permissions and toggles stay in preflight. ✔
5. **Reply-mention stripping:** keep the step. Verify the EventSub text format during implementation (action item).
6. **Commit on failure:** writes commit even when the final code ≠ 0. There is **no rollback** based on the end result. Only moderation cancellation and timeouts discard the buffer. ✔

---

## Appendix C. PEG grammar (normative for parsing)

> **Errata applied while implementing the reference parser (2026-09-16):**
> 1. The right side of `&&`/`||` is a `Pipeline` (`LogicOperand`), not a single `Stage`. Otherwise `a && b | c` failed to parse.
> 2. `RawTailInvocation` needs a `&NameStart` lookahead before `Name`, so trying the raw-tail alternative on input like `( !a …` backtracks instead of throwing `E_BAD_NAME`.
>
> **2.0 (ADR-0018):** stores are `->`/`-->`; `IfElse` and bare expression lines are new; `check` and `calc` take one `Expression`; placeholders hold an `Expression`, or a `Subst`; C.7 is new and the old C.7/C.8 are now C.8/C.9.

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

LineBody        <- &{ expr_line } ExprLine                 # action: Invocation('calc', expr = e)
                 / RawTailInvocation _ EOF
                 / Expr End

LineStart       <- &( PREFIX PrefixGap? ExprLine )           # only at position 0 (§3.5)
                 / &( (Open WS)* PREFIX PrefixGap? '@'? NameStart !(Digit+ (WSChar / EOF)) )
ExprLine        <- &(Digit / '{' / '(' / '-') e:Expression _ EOF
                   &{ has_operator(e) }                   # Unary, Binary or Compare at the top

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
                 / IfElse
                 / Invocation

IfElse          <- CmdPrefix? 'ifelse' Boundary IfCond IfThen (WS Group)?
IfCond          <- WS !OperatorToken Word
                 / _ EOF %E_MISSING_OPERAND
                 / %E_EXPR_SYNTAX                       # hint: ifelse needs a condition, then ( a command )
IfThen          <- WS Group
                 / _ EOF %E_MISSING_OPERAND
                 / %E_EXPR_SYNTAX                       # hint: ifelse needs ( a command ) after its condition

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
Invocation      <- CmdPrefix? p:'@'? n:Name
                   ( &{ p is None and n in ('check', 'calc') } ExprArgs
                   / a:Arg* RawCheck )
ExprArgs        <- WS !OperatorToken !SubstEnd e:Expression &(_ (EOF / OperatorToken / SubstEnd))
                 / WS !OperatorToken !SubstEnd Expression _ Stray
                 / ''                                   # no expression: check/calc fail with code 2
Arg             <- WS !OperatorToken !SubstEnd w:Word OldStore
OldStore        <- &{ line and w in ('>', '>>') } &(WS VarNs '.') %E_UNEXPECTED_OPERATOR
                                                       # hint: > is plain text now: store with ->
                 / ''
SubstEnd        <- &{ in_subst } '}'
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

CmdPrefix       <- PREFIX PrefixGap? &('@'? NameStart)
# PREFIX matching ignores U+FE0F on either side (§2.1). The gap exists only for non-ASCII prefixes.
PrefixGap       <- &{ prefix_allows_gap() } WS
Name            <- n:(NameStart NameChar*) &(WSChar / EOF / SubstEnd)
                   &{ len(n) <= 32 and not n.isdigit() }                 # action: lowercase
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
                   ( Subst
                   / OldResultRef
                   / PhInner '}'
                   / _ EOF %E_BAD_PLACEHOLDER
                   / PhInner Stray
                   / %E_BAD_PLACEHOLDER )
Subst           <- '!' Invocation _ '}'                 # in_subst while parsing the Invocation
                 / '!' %E_BAD_PLACEHOLDER               # hint: {!…} runs one command and ends with }
OldResultRef    <- &{ line } &(_ [1-9] Digit* ('.' IdentChar+)* _ ('??' / '}')) %E_BAD_PLACEHOLDER
                                                       # hint: a result is {_N} now
PhInner         <- _ OrExpr Fallback? _
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
StoreOp         <- '-->' Boundary / '->' Boundary
Open            <- '('  Boundary
Close           <- ')'  Boundary
Reserved        <- ';'  Boundary
OperatorToken   <- ('||' / '|' / '&&' / '-->' / '->' / '(' / ')' / ';') Boundary

VarRef          <- VarNs '.' v:VarName &{ len(v) <= 32 and not is_reserved_var(ns, v) }
                   ('[' Key ']')* Boundary
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
| `prefix_allows_gap()` | true when the prefix's last character (ignoring U+FE0F) is not ASCII (§2.1) |
| `raw_tail_from(name, lead_args)` | the command registry: built-in specs with `raw_tail_from` |
| `is_registered_root(ident)` | namespaces.md §1–§5, with `$` for the bot's fields |
| `bot_fields(root)` | namespaces.md: the fields of `$chatter`, `$channel`, `$publisher`, `$bot`, `$now` |
| `is_reserved_var(ns, name)` | namespaces.md §5 |
| `MAX_PLACEHOLDER_NESTING`, `MAX_EXPR_DEPTH`, `MAX_INT_DIGITS` | §10 |
| `line` | true in the Line context: enables the hints for 1.0 spellings (§2.3, §2.7) |

Parameters may depend on the channel, for example which raw-tail commands are enabled. Name *resolution* never happens during parsing (§5.1). The parser only asks whether a name is a raw-tail command.

### C.7 Expressions inside placeholders

Spaces between tokens are optional (`_`). Every `(`, `[`, unary operator and placeholder adds one level of depth; past `MAX_EXPR_DEPTH` the parser throws `E_EXPR_TOO_DEEP`. An operator with no operand after it throws `E_EXPR_SYNTAX`.

```peg
Expression      <- OrExpr (_ '??' _ Expression)?
OrExpr          <- AndExpr (_ 'or' !IdentChar _ AndExpr)*
AndExpr         <- NotExpr (_ 'and' !IdentChar _ NotExpr)*
NotExpr         <- 'not' !IdentChar _ NotExpr
                 / Comparison
Comparison      <- Sum (_ CompareOp _ Sum)*
CompareOp       <- '==' / '!=' / '<=' / '>=' / '<' !'<' / '>' !'>'
                 / 'in' !IdentChar / 'not' !IdentChar _ 'in' !IdentChar
Sum             <- Product (_ !StoreOp ('+' / '-') _ Product)*
Product         <- Unary (_ ('//' / '/' / '%' / '*' !'*') _ Unary)*
Unary           <- !StoreOp '-' _ Unary                 # action: fold into a number literal
                 / Postfix
Postfix         <- Atom (Accessor / '[' _ Key _ ']')*
Key             <- !('_' Digit* _ ']') Ident &(_ ']')   # a bare word is a literal key
                 / Expression
Accessor        <- ':' _ ( 'choice(' _ ChoiceItem (_ ',' _ ChoiceItem)* _ ')'
                         / ('len' / 'keys' / 'values' / TypeName) !IdentChar
                         / %E_UNKNOWN_OP )
ChoiceItem      <- (!(',' / ')' / '}' / WSChar) .)+
TypeName        <- 'str' / 'int' / 'float' / 'bool' / 'range' / 'duration' / 'user' / 'url'
                 / 'list' / 'map'

Atom            <- Number / String
                 / 'true' !IdentChar / 'false' !IdentChar
                 / '(' _ Expression _ ')'
                 / Placeholder
                 / BotField / ResultRef / ArgRef / 'args' !IdentChar / PathRef / VarRead
Number          <- Digit+ ('.' Digit+)? ([eE] [+-]? Digit+)?
                   # action: an int has at most MAX_INT_DIGITS digits, a float is finite, else %E_EXPR_SYNTAX
String          <- '"' ('\\' . / !'"' .)* ('"' / %E_UNTERMINATED_QUOTE)
BotField        <- '$' r:Ident &{ r in BOT_ROOTS } '.' f:Ident !('.' IdentChar)
                 / '$' %E_BAD_PLACEHOLDER               # hint: $root takes one field
ResultRef       <- '_' ([1-9] Digit*)? !IdentChar ('.' ('code' / 'message' / 'data') !IdentChar)?
                   !('.' IdentChar) / &('_' Digit* '.') %E_BAD_PLACEHOLDER
ArgRef          <- 'arg.' ( Digit+ ('+raw' !IdentChar / '+' &(WSChar / '}' / ':' / ']' / EOF))?
                          / Ident ) !('.' IdentChar)
                 / 'arg' %E_BAD_PLACEHOLDER
PathRef         <- r:Ident &{ r in PATH_ROOTS } ('.' Ident)*
VarRead         <- VarNs '.' VarName !('.' IdentChar)   # hints: $ for the bot's fields, [ ] into values
                 / &(('chatter' / 'channel' / 'publisher') !IdentChar) %E_BAD_PLACEHOLDER
                 / &(('bot' / 'now') !IdentChar) %E_BAD_PLACEHOLDER
Stray           <- &UnknownOp %E_UNKNOWN_OP
                 / %E_EXPR_SYNTAX
UnknownOp       <- '**' / '>>' / '<<' / '&&' / '||' / '|' / '&' / '^' / '~' / '!' !'=' / '='
Digit           <- [0-9]
```

`BOT_ROOTS` are `chatter channel publisher bot now`, and `PATH_ROOTS` are `event match cooldown denied run cmd`. The keywords `and`, `or`, `not`, `in`, `true` and `false` are never a reference.

### C.8 Semantic actions (AST construction)

| Rule | Produces |
|------|----------|
| `Logical`, `Pipeline` | left-folded `And`/`Or` and `Pipe` nodes |
| `Stage` with `StoreSuffix` | `Store(inner, VarRef, append = (op == '-->'))` |
| `Group` | `Group(inner)` |
| `IfElse` | `IfElse(cond, then, else_)` |
| `Invocation`, `RawTailInvocation` | `Invocation(index=0, name=lower(n), personal=p is not None, args, raw_tail, span, expr)`. Indexes are assigned in a pre-order pass after parsing (§6.4). |
| `ExprLine` | `Invocation(name='calc', expr=e)` |
| `Subst` | `Subst(Invocation)` with index −1, −2, … in source order |
| `Word` | `Arg`: adjacent `Text` parts merged, and `Placeholder` parts kept in order |
| `Quoted` | its parts, without the quote characters |
| `Escape` | `Text(char)` |
| `Placeholder` | `Placeholder(expr, fallback, span)`; a `Subst` is `Placeholder(Subst, None, span)` |
| `C.7` rules | the `Expr` nodes of §4: `Lit`, `Ref`, `VarRef`, `Index`, `Access`, `Unary`, `Binary`, `Compare` |

Checks outside the grammar, run before parsing:
- `E_TOO_LONG`, on the whole input
- pre-processing (§2.1)

### C.9 Error reporting

- The parser MUST report the **first** thrown error, with its code, the 1-based column and a hint.
- Errors not thrown by name (plain PEG failure at the top level) MUST NOT happen in a conforming implementation. `End` and `Primary` cover every leftover case. If one does happen, it is reported as `E_INTERNAL`, logged, and treated as a parse error (code 299).
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
| `E_TOO_LONG` | `expression too long` |
| `E_EXPR_SYNTAX` | `invalid expression` |
| `E_UNKNOWN_OP` | `unknown operator <op>` |
| `E_EXPR_TOO_DEEP` | `expression nested too deeply` |

Some throws carry a more specific hint instead, named in the grammar above: `ifelse needs ( a command ) after its condition`, `read inside a value with [ ]: {channel.stats[kills]}`, `a result has .code, .message and .data; use _1[key]`, and, in the Line context only, the pointers from 1.0 spellings: `a result is {_1} now`, `the bot's fields start with $: {$chatter.display}`, `> is plain text now: store with ->`.

---

## Appendix D. EBNF for documentation (non-normative)

This is a simplified W3C-style EBNF (XML spec notation) for **railroad diagrams** on the public docs page. It leaves out error productions, predicates and raw tails. Appendix C is authoritative.

```ebnf
Line        ::= Prefix Gap? ( Expr | Expression )
Expr        ::= Pipeline ( ( '&&' | '||' ) Pipeline )*
Pipeline    ::= Stage ( '|' Stage )*
Stage       ::= ( Group | IfElse | Invocation ) ( ( '->' | '-->' ) VarRef )?
Group       ::= '(' Expr ')'
IfElse      ::= Prefix? 'ifelse' Argument Group Group?
Invocation  ::= Prefix? Gap? '@'? Name Argument*
              | Prefix? Gap? ( 'check' | 'calc' ) Expression
Argument    ::= ( Text | Quoted | Escape | Placeholder )+
Quoted      ::= '"' ( [^"\{] | Escape | Placeholder )* '"'
Escape      ::= '\' Char
Placeholder ::= '{' ( '!' Invocation | Or ( '??' Fallback )? ) '}'
Expression  ::= Or ( '??' Expression )?
Or          ::= And ( 'or' And )*
And         ::= Not ( 'and' Not )*
Not         ::= 'not' Not | Comparison
Comparison  ::= Sum ( ( '==' | '!=' | '<' | '<=' | '>' | '>=' | 'in' | 'not in' ) Sum )*
Sum         ::= Product ( ( '+' | '-' ) Product )*
Product     ::= Unary ( ( '*' | '/' | '//' | '%' ) Unary )*
Unary       ::= '-' Unary | Postfix
Postfix     ::= Atom ( ':' Accessor | '[' Key ']' )*
Accessor    ::= 'len' | 'keys' | 'values' | Type
Key         ::= Identifier | Expression
Atom        ::= Number | String | 'true' | 'false' | '(' Expression ')' | Placeholder | Reference
Reference   ::= '$' BotRoot '.' Identifier
              | '_' Digits? ( '.' ( 'code' | 'message' | 'data' ) )?
              | 'arg' '.' ( Digits ( '+' | '+raw' )? | Identifier )
              | 'args'
              | Root ( '.' Identifier )*
              | Variable
BotRoot     ::= 'chatter' | 'channel' | 'publisher' | 'bot' | 'now'
Root        ::= 'event' | 'match' | 'cooldown' | 'denied' | 'run' | 'cmd'
Type        ::= 'str' | 'int' | 'float' | 'bool' | 'range' | 'duration' | 'user' | 'url'
              | 'list' | 'map' | 'choice(' Item ( ',' Item )* ')'
Variable    ::= ( 'chatter' | 'channel' | 'channel.chatter' | 'publisher' | 'publisher.chatter'
              | 'publisher.channel' | 'publisher.channel.chatter' ) '.' VarName
VarRef      ::= Variable ( '[' Key ']' )*
Gap         ::= WS            /* only after a non-ASCII (emoji) prefix */
Name        ::= [a-z0-9] [a-z0-9_-]*
```

Diagrams are generated when the web UI is built, from a copy of this block extracted to `docs/grammar/railroad.ebnf`. A CI check fails if that copy differs from this appendix.
