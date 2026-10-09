/**
 * Colours for the token classes, and the editor's frame. Everything a page may change goes through the
 * `--dtb-*` custom properties of `properties.js`, so light and dark, or a site's own look, follow the page.
 */

import { EditorView } from "@codemirror/view";
import { v } from "./properties.js";

export const theme = EditorView.theme({
  "&": {
    background: v("--dtb-bg"),
    color: v("--dtb-ink"),
    border: `1px solid ${v("--dtb-line")}`,
    borderRadius: v("--dtb-radius"),
    fontSize: v("--dtb-font-size"),
  },
  "&.cm-focused": { outline: `1px solid ${v("--dtb-accent")}` },
  ".cm-scroller": { fontFamily: v("--dtb-font") },
  ".cm-content": {
    fontFamily: v("--dtb-font"),
    padding: ".6rem .2rem",
    caretColor: v("--dtb-ink"),
  },
  ".cm-gutters": { background: "transparent", border: "none", color: v("--dtb-muted") },
  ".cm-activeLine": { background: "transparent" },
  ".cm-placeholder": { color: v("--dtb-muted") },
  ".cm-tooltip": {
    background: v("--dtb-raised"),
    border: `1px solid ${v("--dtb-line")}`,
    borderRadius: v("--dtb-radius"),
    color: v("--dtb-ink"),
  },
  ".cm-tooltip-autocomplete ul li[aria-selected]": {
    background: v("--dtb-accent"),
    color: v("--dtb-accent-ink"),
  },
  // Commands gold, strings green, operators pink, variables blue whether read or written (a written one is
  // italic), fallbacks and escapes yellow. Inside an expression, operators are pink and numbers green like
  // strings.
  ".dtb-prefix": { color: v("--dtb-accent"), fontWeight: "600" },
  ".dtb-personal": { color: v("--dtb-accent") },
  ".dtb-command": { color: v("--dtb-accent"), fontWeight: "600" },
  ".dtb-word": { color: v("--dtb-ink") },
  ".dtb-string": { color: v("--dtb-ok") },
  ".dtb-escape": { color: v("--dtb-soft") },
  ".dtb-operator": { color: v("--dtb-op"), fontWeight: "600" },
  ".dtb-store-target": { color: v("--dtb-var"), fontStyle: "italic" },
  ".dtb-ph-open, .dtb-ph-close": { color: v("--dtb-muted") },
  ".dtb-ph-root": { color: v("--dtb-var") },
  ".dtb-ph-path": { color: v("--dtb-path") },
  ".dtb-ph-type": { color: v("--dtb-muted"), fontStyle: "italic" },
  ".dtb-ph-fallback": { color: v("--dtb-soft") },
  ".dtb-ph-op": { color: v("--dtb-op") },
  ".dtb-ph-num": { color: v("--dtb-ok") },
  ".dtb-raw": { color: v("--dtb-ink"), opacity: ".8" },
  ".dtb-error": { textDecoration: `underline wavy ${v("--dtb-bad")}` },
});
