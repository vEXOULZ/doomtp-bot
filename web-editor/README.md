# web-editor

The expression editor for the web UI (ADR-0011): a CodeMirror 6 web component with a Lezer grammar used
**only for highlighting**. Validation, diagnostics, autocomplete and explain previews come from the API:

- `POST /api/v1/parse` → tokens, AST, errors, warnings
- `GET /api/v1/language` → roots, types, raw-tail commands, limits
- `POST /api/v1/explain` → preflight report

Build output is a single static bundle (esbuild) served by the bot from `/static`. This is the only
Node-tooled part of the repository.

Not scaffolded yet. It comes after the parser and the `/parse` endpoint exist. Token-class tests will run against
`../tests/lang/corpus.yaml`.
