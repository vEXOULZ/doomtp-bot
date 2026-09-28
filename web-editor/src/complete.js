/** Autocomplete, entirely from `/api/v1/language` and `/api/v1/commands` — never a local list. */

import * as api from "./api.js";

const option = (label, type, detail) => ({ label, type, detail });

/**
 * What is being typed at `pos`: a placeholder root, a `$` field, a type, a variable to write, or a
 * command name (at the start of a stage, or after `{!`).
 */
export function where(text, pos) {
  const open = text.lastIndexOf("{", pos - 1);
  const close = text.lastIndexOf("}", pos - 1);
  if (open > close) {
    const inside = text.slice(open + 1, pos);
    if (/^!\S*$/.test(inside)) return { kind: "command", from: open + 2 };
    const field = /\$([a-z]+)\.([a-z_]*)$/.exec(inside);
    if (field) return { kind: "field", root: `$${field[1]}`, from: pos - field[2].length };
    const typed = /:\s*([a-z]*)$/.exec(inside);
    if (typed) return { kind: "type", from: pos - typed[1].length };
    if (/^\s*[^\s.[\]()]*$/.test(inside)) return { kind: "root", from: pos - inside.trimStart().length };
    return { kind: "none" }; // inside a value, or mid-expression: only the server knows
  }
  const before = text.slice(0, pos);
  const word = /(\S*)$/.exec(before)[1];
  const start = pos - word.length;
  const earlier = before.slice(0, start).trimEnd();
  if (/(^|\s)--?>$/.test(earlier)) return { kind: "variable", from: start };
  if (start === 0 || /(\||\|\||&&|\()$/.test(earlier)) return { kind: "command", from: start };
  return { kind: "none" };
}

export function completions(options) {
  return async (context) => {
    const text = context.state.doc.toString();
    const spot = where(text, context.pos);
    if (spot.kind === "none") return null;
    if (spot.from === context.pos && !context.explicit) return null;

    if (spot.kind === "command") {
      const { commands } = await api.commands();
      return {
        from: spot.from,
        options: commands.map((c) => option(c.name, "function", c.summary)),
      };
    }
    const language = await api.language();
    if (spot.kind === "root") {
      const roots = language.roots_by_context[options.context] || language.roots;
      return { from: spot.from, options: roots.map((r) => option(r, "variable")) };
    }
    if (spot.kind === "field") {
      const fields = language.bot_fields[spot.root] || [];
      return { from: spot.from, options: fields.map((f) => option(f, "property", spot.root)) };
    }
    if (spot.kind === "type") {
      return { from: spot.from, options: language.types.map((t) => option(t, "type")) };
    }
    return {
      from: spot.from,
      options: language.variable_namespaces.map((ns) => option(`${ns}.`, "namespace")),
    };
  };
}
