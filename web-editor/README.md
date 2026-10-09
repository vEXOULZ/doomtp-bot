# web-editor

The expression editor for the web site (ADR-0011, ADR-0016): a CodeMirror 6 web component whose own lexer is used
**only for colours**. Validation, diagnostics, autocomplete and explain previews come from the bot:

- `POST /api/v1/parse` → the error a chat user would get, with its offset
- `GET /api/v1/language` → roots per context, types, variable namespaces, limits
- `GET /api/v1/commands` → command names for autocomplete
- `POST /api/v1/explain` → the preflight report (spec §9)

This is the only Node-tooled part of the repository.

```bash
npm install
npm test     # vitest: token classes, including against ../tests/lang/corpus.yaml
npm run build  # esbuild → ../src/doomtp_bot/api/static/editor/{editor,tokens}.js
```

**The built bundle is committed.** The Docker image has no Node in it and the bot serves the file as it
stands, so `npm run build` is part of changing anything here, and its output goes in the same commit.

## Using it

```html
<dtb-editor context="line" prefix="🏜" channel="doomtp" explain>
  <textarea name="expr" rows="3">🏜ping | echo {_1}</textarea>
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
tokenize("🏜random 1-6 | echo {_1}", { context: "line" }); // [{ t: "prefix", s: 0, e: 2 }, …]
```

Its exports (`tokenize`, `allowsGap`, `DEFAULT_PREFIX`, `VARIATION_SELECTOR`) and the token classes are a
contract with that site: add to them, don't rename them.

## Theming

The element has no shadow root, so there are no `::part()`s. A page themes it with these custom properties,
set on `dtb-editor` or any ancestor. They are a contract with doomtp-web: add to them, don't rename them.

| Property | What it colours or sets | Falls back to | Default |
|----------|-------------------------|---------------|---------|
| `--dtb-bg` | the editor's background | `--panel` | `#16181d` |
| `--dtb-ink` | text, the caret, words | `--ink` | `#e6e6e6` |
| `--dtb-muted` | the gutter, placeholder braces, types, the hint | `--muted` | `#8b8f98` |
| `--dtb-line` | every border | `--line` | `#2c3038` |
| `--dtb-accent` | the prefix and commands, the focus ring, the chosen completion, a hovered button | `--accent` | `#e8a33d` |
| `--dtb-accent-ink` | text on the accent | `--bg` | `#0f1115` |
| `--dtb-ok` | strings and numbers | `--ok` | `#7cc47c` |
| `--dtb-bad` | the wavy underline under an error | `--bad` | `#ff6b6b` |
| `--dtb-raised` | the Explain button, the report, tooltips | | `--dtb-bg` |
| `--dtb-soft` | escapes and fallbacks | | `#e8c35a` |
| `--dtb-op` | operators | | `#ff7ab2` |
| `--dtb-var` | variables, read or written | | `#8ab4f8` |
| `--dtb-path` | a variable's path after its root | | `#b7cffb` |
| `--dtb-font` | the editor's and the report's font | | `ui-monospace, …` |
| `--dtb-font-size` | the editor's font size | | `.95rem` |
| `--dtb-small-font-size` | the button's and the report's font size | | `.85rem` |
| `--dtb-radius` | every corner | | `.4rem` |
| `--dtb-gap` | the space above the button and the report | | `.5rem` |
| `--dtb-control-height` | the Explain button's height | | `2rem` |

The "falls back to" column is the variables the bot's own pages used; a page that sets neither gets the
dark default. For anything the properties don't reach, three class names are stable too: `.dtb-editor`
(the box around the editor), `.dtb-explain` (the button) and `.dtb-report`. The element's own styles for
the button and the report weigh nothing (`:where()`), so any rule of the page's wins. CodeMirror's `.cm-*`
classes are not part of the API.

```css
dtb-editor {
  --dtb-bg: var(--vx-surface);
  --dtb-raised: var(--vx-surface-2);
  --dtb-line: var(--vx-line);
  --dtb-font: var(--vx-font-mono);
  --dtb-font-size: 14px;
  --dtb-radius: var(--vx-radius-sm);
  --dtb-control-height: var(--vx-ctl);
}
```

## Layout

| File | What it is |
|------|------------|
| `src/tokens.js` | the lexer: text → token classes of ADR-0011. Pure, and the only thing worth testing |
| `src/highlight.js` | those tokens as CodeMirror decorations |
| `src/theme.js` | colours and the frame, all through the theming properties |
| `src/properties.js` | the theming API: the `--dtb-*` properties, their fallbacks, the button's and report's styles |
| `src/complete.js` | where the cursor is → which list to ask the server for |
| `src/api.js` | the four endpoints, with the page-lifetime cache for the ones that don't change |
| `src/editor.js` | the `<dtb-editor>` element: the textarea it upgrades, diagnostics, the report |

The lexer deliberately knows nothing context-sensitive — raw tails, which names resolve, which roots
exist in a context. It is allowed to be wrong about all of it; the server is asked, and its answer wins.
