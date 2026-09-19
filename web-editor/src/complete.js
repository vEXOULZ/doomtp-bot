/** Autocomplete, entirely from `/api/v1/language` and `/api/v1/commands` — never a local list. */

import * as api from "./api.js";

const option = (label, type, detail) => ({ label, type, detail });

/** What is being typed at `pos`: a placeholder root, a type, a variable to write, or a command name. */
export function where(text, pos) {
  const open = text.lastIndexOf("{", pos - 1);
  const close = text.lastIndexOf("}", pos - 1);
  if (open > close) {
    const inside = text.slice(open + 1, pos);
    if (inside.includes(":")) return { kind: "type", from: pos - inside.split(":").pop().length };
    if (inside.includes(".")) return { kind: "none" }; // fields of a value: only the server knows
    return { kind: "root", from: open + 1 };
  }
  const before = text.slice(0, pos);
  const word = /(\S*)$/.exec(before)[1];
  const start = pos - word.length;
  const earlier = before.slice(0, start).trimEnd();
  if (/(^|\s)>>?$/.test(earlier)) return { kind: "variable", from: start };
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
    if (spot.kind === "type") {
      return { from: spot.from, options: language.types.map((t) => option(t, "type")) };
    }
    return {
      from: spot.from,
      options: language.variable_namespaces.map((ns) => option(`${ns}.`, "namespace")),
    };
  };
}
