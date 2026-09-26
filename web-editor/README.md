# web-editor

The expression editor for the web UI (ADR-0011): a CodeMirror 6 web component whose own lexer is used
**only for colours**. Validation, diagnostics, autocomplete and explain previews come from the bot:

- `POST /api/v1/parse` → the error a chat user would get, with its offset
- `GET /api/v1/language` → roots per context, types, variable namespaces, limits
- `GET /api/v1/commands` → command names for autocomplete
- `POST /api/v1/explain` → the preflight report (spec §9)

This is the only Node-tooled part of the repository.

```bash
npm install
npm test     # vitest: token classes, including against ../tests/lang/corpus.yaml
npm run build  # esbuild → ../src/doomtp_bot/webui/static/editor/{editor,tokens}.js
```

**The built bundle is committed.** The Docker image has no Node in it and the bot serves the file as it
stands, so `npm run build` is part of changing anything here, and its output goes in the same commit.

## Using it

```html
<dtb-editor context="line" prefix="🏜" channel="doomtp" explain>
  <textarea name="expr" rows="3">🏜ping | echo {1}</textarea>
</dtb-editor>
<script src="/static/editor/editor.js" defer></script>
```

It upgrades the `<textarea>` it wraps and keeps it in sync, so the page still works without JavaScript
and an ordinary form post still carries the same field. `context` is the parse context (`line`, `body`,
`trigger`, `listener`, `callback`), `channel` supplies that channel's prefix and visible commands to the
server, and `explain` adds the report under the editor.

The lexer is also published on its own, as an ES module at `/static/editor/tokens.js`, for a site on the
same origin that wants the editor's colours without the editor (doomtp-web colours every command it shows
this way, ADR-0016):

```js
const { tokenize } = await import("/static/editor/tokens.js");
tokenize("🏜random 1-6 | echo {1}", { context: "line" }); // [{ t: "prefix", s: 0, e: 2 }, …]
```

Its exports (`tokenize`, `allowsGap`, `DEFAULT_PREFIX`, `VARIATION_SELECTOR`) and the token classes are a
contract with that site: add to them, don't rename them.

## Layout

| File | What it is |
|------|------------|
| `src/tokens.js` | the lexer: text → token classes of ADR-0011. Pure, and the only thing worth testing |
| `src/highlight.js` | those tokens as CodeMirror decorations |
| `src/theme.js` | colours, taken from the page's CSS variables so light and dark follow it |
| `src/complete.js` | where the cursor is → which list to ask the server for |
| `src/api.js` | the four endpoints, with the page-lifetime cache for the ones that don't change |
| `src/editor.js` | the `<dtb-editor>` element: the textarea it upgrades, diagnostics, the report |

The lexer deliberately knows nothing context-sensitive — raw tails, which names resolve, which roots
exist in a context. It is allowed to be wrong about all of it; the server is asked, and its answer wins.
