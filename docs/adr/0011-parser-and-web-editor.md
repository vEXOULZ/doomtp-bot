# ADR-0011: One authoritative parser on the server; the web editor highlights only

**Status:** Accepted (server side implemented) — 2026-09-18
**Date:** 2026-09-16
**Deciders:** Project owner

## Context

The command language ([spec](../command-language-spec.md)) is parsed in the bot (Python). It also needs editing support in the web UI: syntax highlighting, error underlines, autocomplete and `!explain` previews when writing custom command bodies, triggers and callbacks.

Two parsers in two languages (Python and JavaScript) tend to drift apart. The spec also defines exact error codes, columns and hints (Appendix C.8) that users should see identically in chat and in the UI. And the grammar needs runtime parameters that only the server knows: the channel prefix, raw-tail commands and registered roots.

## Decision

1. **The authoritative parser is the Python reference parser only.** It's a hand-written recursive-descent PEG parser in `lang/parser.py` that mirrors spec Appendix C rule-for-rule.
2. **The server exposes parsing through the API:**
   - `POST /api/v1/parse` returns tokens, the AST and errors (below).
   - `POST /api/v1/explain` returns the full explain report (spec §9).
   - `GET /api/v1/language` returns the syntax version, operators, registered roots, context availability (§7.2), types, raw-tail commands, reserved variable names and limits. This drives autocomplete and hover docs.
3. **The browser editor is CodeMirror 6, with its own lexer used only for highlighting.**
   - It gives instant local colours while typing.
   - It is **not** authoritative: it never decides validity.
   - It ignores context-sensitive rules (raw tails, the prefix), and at worst colours a raw tail as ordinary words.
4. **Diagnostics come from the server.**
   - The editor calls `/parse` about 300 ms after typing stops, and requests are cancelled if the text changes again.
   - Server errors become CodeMirror diagnostics, using server spans.
   - Server tokens *replace* the local highlighting once they arrive.
   - `/explain` runs on demand (button or shortcut), or automatically after a successful parse when the text is under 500 characters.
5. **The conformance corpus is shared.**
   - Spec Appendix A is stored as data (`tests/lang/corpus.yaml`).
   - The Python parser tests assert the AST, error code and column.
   - The Lezer highlighter tests (vitest) assert only token *classes* for the lexing cases, so highlighting can't drift badly.
6. **The docs page** renders railroad diagrams at build time from spec Appendix D (`docs/grammar/railroad.ebnf`). A CI check fails if that file and the appendix differ.

## API contract (v1)

```http
POST /api/v1/parse
{
  "text": "!random 1-{arg.1:int ?? 20} | echo {chatter.name} rolled {1}",
  "context": "body",              // line | body | trigger | listener | callback
  "channel": "doomtp",            // optional: supplies prefix, enabled raw-tail commands
  "include": ["tokens", "ast", "errors"]
}
```

```json
{
  "ok": true,
  "syntax_version": "1.0",
  "tokens": [
    {"t": "prefix",       "s": 0,  "e": 1},
    {"t": "command",      "s": 1,  "e": 7},
    {"t": "word",         "s": 8,  "e": 10},
    {"t": "ph.open",      "s": 10, "e": 11},
    {"t": "ph.root",      "s": 11, "e": 14},
    {"t": "ph.path",      "s": 14, "e": 16},
    {"t": "ph.type",      "s": 16, "e": 20},
    {"t": "ph.fallback",  "s": 21, "e": 26},
    {"t": "ph.close",     "s": 26, "e": 27},
    {"t": "operator",     "s": 28, "e": 29}
  ],
  "ast": { "type": "Pipe", "left": { "type": "Invocation", "index": 1, "name": "random", "...": "..." }, "right": { "...": "..." } },
  "errors": [],
  "warnings": [
    {"code": "W_UNKNOWN_COMMAND", "message": "no command named 'rando' is visible in #doomtp", "s": 1, "e": 6}
  ]
}
```

- **Token classes:** `prefix`, `personal`, `command`, `word`, `string`, `escape`, `operator`, `store.target`, `ph.open`, `ph.close`, `ph.root`, `ph.path`, `ph.type`, `ph.fallback`, `raw`, `error`. The Lezer grammar uses the same class names.
- **Errors:** `{code, message, hint, s, e}`, where `s`/`e` are 0-based character offsets for the editor. Chat messages show the 1-based column from the spec.
- **Warnings** come from a light resolution pass (unknown names, context-unavailable roots) when `channel` is given. They're advisory. Full preflight is the job of `/explain`.
- **Auth and limits:**
  - `/parse` is available to anyone who can reach the UI.
  - It's rate-limited to 10 requests/s per session.
  - It needs no channel permission, because it reveals no private data: names visible in a channel are already public through `/api/v1/channels/{login}/commands`.
  - `/explain` with `as_user` requires admin rights.

## Options Considered

| Option | Verdict |
|--------|---------|
| **Server-authoritative parser + highlight-only Lezer grammar** (chosen) | One source of truth. Error codes and hints match the spec exactly. Grammar parameters are always correct. Cost: diagnostics need a round-trip, which is negligible on the LAN. |
| tree-sitter grammar shared by Python and WASM | Offline parsing in the browser and great error recovery. But the spec's error codes would have to be recovered from generic `ERROR` nodes, and the raw-tail and prefix parameters need an external scanner in C. More build tooling (C and WASM). |
| Two PEG implementations (TatSu + Peggy) | Two grammar dialects in sync by hand, with duplicated error handling. Most drift-prone. Rejected. |
| Python parser compiled to WASM (Pyodide) | Heavy download (MBs) for an editor feature. Rejected. |

## Consequences

- **Easier:**
  - Spec changes happen in one parser.
  - The UI shows exactly what chat users will see.
  - Autocomplete is driven by `/language`, so it follows the registry automatically.
- **Harder:**
  - The editor needs the API for validation. Offline editing only gets highlighting.
  - The Lezer grammar needs occasional updates when lexing rules change. Corpus token tests catch drift.
- **Revisit:** if the UI becomes public on the internet with heavy traffic, or needs offline validation, reconsider tree-sitter.

## Action Items

1. [x] Build `lang/parser.py` as a PEG recursive-descent parser mirroring spec Appendix C, with labeled-failure throws and the parameter hooks. *Built 2026-09-18.*
2. [x] Move Appendix A to `tests/lang/corpus.yaml` and add pytest runners for AST, error code and column. *Built 2026-09-18: `tests/lang/test_corpus.py`; the editor's own tests read the same file.*
3. [x] Add `/api/v1/parse`, `/api/v1/explain` and `/api/v1/language`. *Built 2026-09-18 in `api/routes/language.py`. **Deviation:** `/parse` returns the AST, the invocation spans and the error, not a token stream — the browser lexes for colours (item 4), so a second token stream over the wire would be one more thing to keep in step for no visible gain.*
4. [x] Web: a CodeMirror 6 editor component, a highlight lexer, a debounced diagnostics linter and autocomplete from `/language`. *Built 2026-09-19: `web-editor/` bundles `<dtb-editor>` to `webui/static/editor/editor.js` (committed, since the image has no Node), and the language page carries it as a playground. **Deviation:** the highlighter is a hand-written lexer feeding CodeMirror decorations, not a Lezer grammar. Lezer builds a syntax tree, and nothing here needs one — the server owns every structural question — so a generated grammar would have added a build step and a second description of the syntax for colours alone. The token classes, the corpus tests and the 'never decides validity' rule are unchanged.*
5. [x] Web tests: token classes against the corpus lexing cases. *Built 2026-09-19: `web-editor/test/tokens.test.js` asserts classes and spans, and every corpus case with an expected AST has to lex cleanly — nothing the server accepts may be underlined here.*
6. [x] Docs build: railroad diagrams from `docs/grammar/railroad.ebnf`, plus the CI diff check against spec Appendix D. *Built 2026-09-20: `scripts/render_railroad.py` draws one SVG per rule into the package's static files, committed like the editor bundle — `--check` fails in CI when they are stale, beside `check_railroad.py`, which is what keeps the file and the appendix equal. The language page shows the pictures with the text underneath, in a `<details>`.*
