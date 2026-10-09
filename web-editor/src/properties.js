/**
 * The editor's theming API: the `--dtb-*` custom properties a host page sets on `<dtb-editor>` or any
 * ancestor, documented in the README. They are a contract with doomtp-web: add to them, don't rename them.
 *
 * Each colour falls back to the page variable the bot's own pages used (`--panel`, `--ink`, …), then to a
 * dark default, so a page that sets nothing still gets a readable editor. Pure, so the tests can read it.
 */

/** name → [the older page variable it falls back to, or null; the default]. */
export const PROPERTIES = {
  // Colours
  "--dtb-bg": ["--panel", "#16181d"],
  "--dtb-ink": ["--ink", "#e6e6e6"],
  "--dtb-muted": ["--muted", "#8b8f98"],
  "--dtb-line": ["--line", "#2c3038"],
  "--dtb-accent": ["--accent", "#e8a33d"],
  "--dtb-accent-ink": ["--bg", "#0f1115"],
  "--dtb-ok": ["--ok", "#7cc47c"],
  "--dtb-bad": ["--bad", "#ff6b6b"],
  "--dtb-raised": [null, "var(--dtb-bg, var(--panel, #16181d))"],
  "--dtb-soft": [null, "#e8c35a"],
  "--dtb-op": [null, "#ff7ab2"],
  "--dtb-var": [null, "#8ab4f8"],
  "--dtb-path": [null, "#b7cffb"],
  // Type and shape
  "--dtb-font": [null, 'ui-monospace, "Cascadia Code", Consolas, monospace'],
  "--dtb-font-size": [null, ".95rem"],
  "--dtb-small-font-size": [null, ".85rem"],
  "--dtb-radius": [null, ".4rem"],
  "--dtb-gap": [null, ".5rem"],
  "--dtb-control-height": [null, "2rem"],
};

/** `var(<name>, var(<older>, <default>))`, the value to use wherever the property applies. */
export function v(name) {
  const entry = PROPERTIES[name];
  if (!entry) throw new Error(`${name} is not one of the editor's custom properties`);
  const [legacy, fallback] = entry;
  return legacy ? `var(${name}, var(${legacy}, ${fallback}))` : `var(${name}, ${fallback})`;
}

/**
 * The parts of the element outside CodeMirror: the Explain button and the report. Wrapped in `:where()` so
 * they weigh nothing: any rule of the page's own on `.dtb-explain` or `.dtb-report` wins.
 */
export const CHROME_CSS = `
:where(dtb-editor) { display: block; }
:where(dtb-editor .dtb-explain) {
  margin-top: ${v("--dtb-gap")};
  height: ${v("--dtb-control-height")};
  padding: 0 .75rem;
  font: inherit;
  font-size: ${v("--dtb-small-font-size")};
  color: ${v("--dtb-ink")};
  background: ${v("--dtb-raised")};
  border: 1px solid ${v("--dtb-line")};
  border-radius: ${v("--dtb-radius")};
  cursor: pointer;
}
:where(dtb-editor .dtb-explain:hover, dtb-editor .dtb-explain:focus-visible) { border-color: ${v("--dtb-accent")}; }
:where(dtb-editor .dtb-report) {
  margin-top: ${v("--dtb-gap")};
  padding: .6rem .75rem;
  white-space: pre-wrap;
  font-family: ${v("--dtb-font")};
  font-size: ${v("--dtb-small-font-size")};
  color: ${v("--dtb-ink")};
  background: ${v("--dtb-raised")};
  border: 1px solid ${v("--dtb-line")};
  border-radius: ${v("--dtb-radius")};
}
:where(dtb-editor .dtb-report[hidden]) { display: none; }
`;
