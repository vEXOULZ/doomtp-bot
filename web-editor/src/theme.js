/** Colours for the token classes, taken from the page's own CSS variables so light and dark follow it. */

import { EditorView } from "@codemirror/view";

export const theme = EditorView.theme({
  "&": {
    background: "var(--panel)",
    color: "var(--ink)",
    border: "1px solid var(--line)",
    borderRadius: ".4rem",
    fontSize: ".95rem",
  },
  "&.cm-focused": { outline: "1px solid var(--accent)" },
  ".cm-content": {
    fontFamily: 'ui-monospace, "Cascadia Code", Consolas, monospace',
    padding: ".6rem .2rem",
    caretColor: "var(--ink)",
  },
  ".cm-gutters": { background: "transparent", border: "none", color: "var(--muted)" },
  ".cm-activeLine": { background: "transparent" },
  ".cm-tooltip": {
    background: "var(--panel)",
    border: "1px solid var(--line)",
    color: "var(--ink)",
  },
  ".cm-tooltip-autocomplete ul li[aria-selected]": {
    background: "var(--accent)",
    color: "var(--bg)",
  },
  ".dtb-prefix": { color: "var(--accent)", fontWeight: "600" },
  ".dtb-personal": { color: "var(--accent)" },
  ".dtb-command": { color: "var(--accent)", fontWeight: "600" },
  ".dtb-word": { color: "var(--ink)" },
  ".dtb-string": { color: "var(--ok)" },
  ".dtb-escape": { color: "var(--muted)" },
  ".dtb-operator": { color: "var(--bad)", fontWeight: "600" },
  ".dtb-store-target": { color: "var(--bad)" },
  ".dtb-ph-open, .dtb-ph-close": { color: "var(--muted)" },
  ".dtb-ph-root": { color: "#8ab4f8" },
  ".dtb-ph-path": { color: "#8ab4f8", opacity: ".85" },
  ".dtb-ph-type": { color: "var(--muted)", fontStyle: "italic" },
  ".dtb-ph-fallback": { color: "var(--muted)" },
  ".dtb-raw": { color: "var(--ink)", opacity: ".8" },
  ".dtb-error": { textDecoration: "underline wavy var(--bad)" },
});
