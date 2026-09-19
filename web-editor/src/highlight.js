/** The lexer from `tokens.js`, dressed as a CodeMirror decoration layer (ADR-0011). */

import { Decoration, ViewPlugin } from "@codemirror/view";
import { RangeSetBuilder } from "@codemirror/state";
import { tokenize } from "./tokens.js";

// One class per token class of ADR-0011. The stylesheet in `theme.js` colours them.
const MARKS = new Map(
  [
    "prefix",
    "personal",
    "command",
    "word",
    "string",
    "escape",
    "operator",
    "store.target",
    "ph.open",
    "ph.close",
    "ph.root",
    "ph.path",
    "ph.type",
    "ph.fallback",
    "raw",
    "error",
  ].map((name) => [name, Decoration.mark({ class: `dtb-${name.replace(".", "-")}` })]),
);

function decorations(view, options) {
  const builder = new RangeSetBuilder();
  const text = view.state.doc.toString();
  for (const { t, s, e } of tokenize(text, options)) {
    const mark = MARKS.get(t);
    if (mark) builder.add(s, e, mark);
  }
  return builder.finish();
}

/** `options` is read on every change, so a channel's prefix can arrive after the editor is built. */
export function highlighting(options) {
  return ViewPlugin.fromClass(
    class {
      constructor(view) {
        this.decorations = decorations(view, options);
      }

      update(update) {
        if (update.docChanged || update.viewportChanged) {
          this.decorations = decorations(update.view, options);
        }
      }
    },
    { decorations: (plugin) => plugin.decorations },
  );
}
