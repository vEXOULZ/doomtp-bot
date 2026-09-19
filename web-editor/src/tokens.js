/**
 * Lexer for the command language — highlighting only (ADR-0011).
 *
 * The server's parser in `lang/parser.py` is the only authority on what is valid; this one just colours
 * text while you type, and is allowed to be wrong about anything context-sensitive (raw tails, which
 * roots exist, whether a name resolves). It never reports validity: the only `error` tokens it produces
 * are for text that stops making sense lexically, like an unterminated quote.
 *
 * Token classes are the ones in ADR-0011, shared with the server's own token stream.
 */

export const VARIATION_SELECTOR = "️";
export const DEFAULT_PREFIX = "\u{1F3DC}";
const OPERATORS = ["||", "|", "&&", ">>", ">", "(", ")", ";"];
const STORES = new Set([">", ">>"]);
const OPENS_COMMAND = new Set(["|", "||", "&&", "("]);
const MAX_NESTING = 4;

const isSpace = (ch) => /\s/.test(ch);
const isNameChar = (ch) => /[A-Za-z0-9_-]/.test(ch);
const isRootChar = (ch) => /[A-Za-z0-9_]/.test(ch);
const isPathChar = (ch) => /[A-Za-z0-9_+-]/.test(ch);

/** Emoji signs may be followed by a space (`🏜 ping`); `!` may not, or "! that was close" is a command. */
export function allowsGap(prefix) {
  const stripped = prefix.replace(new RegExp(`${VARIATION_SELECTOR}+$`), "");
  const last = [...stripped].pop();
  return Boolean(last) && last.codePointAt(0) > 127;
}

/**
 * @param {string} text
 * @param {{prefix?: string, context?: string}} options
 * @returns {{t: string, s: number, e: number}[]} tokens in source order; whitespace is not a token.
 */
export function tokenize(text, options = {}) {
  const prefix = options.prefix || DEFAULT_PREFIX;
  const context = options.context || "body";
  const out = [];
  let i = 0;
  let command = true; // the next word names a command
  let store = false; // the next word is a variable being written to

  const push = (t, s, e) => {
    if (e > s) out.push({ t, s, e });
  };

  if (context === "line" && text.startsWith(prefix)) {
    i = prefix.length;
    while (text[i] === VARIATION_SELECTOR) i += 1; // "🏜️" is the same sign as "🏜"
    push("prefix", 0, i);
    if (allowsGap(prefix)) while (i < text.length && isSpace(text[i])) i += 1;
  }

  while (i < text.length) {
    if (isSpace(text[i])) {
      i += 1;
      continue;
    }
    const operator = operatorAt(text, i);
    if (operator) {
      push("operator", i, i + operator.length);
      i += operator.length;
      command = OPENS_COMMAND.has(operator);
      store = STORES.has(operator);
      continue;
    }
    if (command) {
      const named = commandAt(text, i, push);
      command = false;
      if (named > i) {
        i = named;
        continue;
      }
    }
    i = argument(text, i, push, store);
    store = false;
  }
  return out;
}

/** An operator only counts when it stands alone between spaces, so `(lol)` and `->` stay words. */
function operatorAt(text, i) {
  for (const op of OPERATORS) {
    if (!text.startsWith(op, i)) continue;
    const after = i + op.length;
    if (after >= text.length || isSpace(text[after])) return op;
  }
  return null;
}

/** `@alias` or `name` in command position. Returns where the name ended, or `i` if there isn't one. */
function commandAt(text, i, push) {
  let j = i;
  if (text[j] === "@") j += 1;
  let end = j;
  while (end < text.length && isNameChar(text[end])) end += 1;
  if (end === j) return i;
  if (j > i) push("personal", i, j);
  push("command", j, end);
  return end;
}

/** One whitespace-delimited argument: quotes, escapes and placeholders, glued together. */
function argument(text, i, push, store) {
  const start = i;
  const parts = [];
  while (i < text.length && !isSpace(text[i])) {
    const ch = text[i];
    if (ch === "\\" && i + 1 < text.length) {
      parts.push(["escape", i, i + 2]);
      i += 2;
    } else if (ch === '"') {
      i = quoted(text, i, (t, s, e) => parts.push([t, s, e]));
    } else if (ch === "{") {
      i = placeholder(text, i, (t, s, e) => parts.push([t, s, e]), 1);
    } else {
      const from = i;
      while (i < text.length && !isSpace(text[i]) && !'"{\\'.includes(text[i])) i += 1;
      parts.push(["word", from, i]);
    }
  }
  // A store target is one word — `> channel.count` — but it is still written with the same pieces.
  if (store && parts.every(([t]) => t === "word")) push("store.target", start, i);
  else for (const [t, s, e] of parts) push(t, s, e);
  return i;
}

function quoted(text, i, push) {
  const open = i;
  let run = i; // the quotes themselves are part of the string
  i += 1;
  while (i < text.length) {
    if (text[i] === "\\" && i + 1 < text.length) {
      push("string", run, i);
      push("escape", i, i + 2);
      i += 2;
      run = i;
    } else if (text[i] === '"') {
      push("string", run, i + 1);
      return i + 1;
    } else {
      i += 1;
    }
  }
  push("error", open, text.length); // unterminated: the rest of the line is the quote
  return text.length;
}

/** `{root.path:type ?? fallback}`, where the fallback may hold placeholders of its own. */
function placeholder(text, i, push, depth) {
  push("ph.open", i, i + 1);
  i += 1;
  let end = i;
  while (end < text.length && isRootChar(text[end])) end += 1;
  push("ph.root", i, end);
  i = end;
  while (text[i] === ".") {
    let seg = i + 1;
    while (seg < text.length && isPathChar(text[seg])) seg += 1;
    push("ph.path", i, seg);
    i = seg;
  }
  if (text[i] === ":") {
    let seg = i + 1;
    while (seg < text.length && /[A-Za-z0-9_]/.test(text[seg])) seg += 1;
    if (text[seg] === "(") {
      // choice(rock,paper,scissors) — everything up to its own closing paren is the type
      while (seg < text.length && text[seg] !== ")" && text[seg] !== "}") seg += 1;
      if (text[seg] === ")") seg += 1;
    }
    push("ph.type", i, seg);
    i = seg;
  }
  while (i < text.length && isSpace(text[i])) i += 1;
  if (text.startsWith("??", i)) {
    push("ph.fallback", i, i + 2);
    i += 2;
    while (i < text.length && isSpace(text[i])) i += 1;
    let run = i;
    while (i < text.length && text[i] !== "}") {
      if (text[i] === "{" && depth < MAX_NESTING) {
        push("ph.fallback", run, i);
        i = placeholder(text, i, push, depth + 1);
        run = i;
      } else if (text[i] === "\\" && i + 1 < text.length) {
        i += 2;
      } else {
        i += 1;
      }
    }
    push("ph.fallback", run, i);
  }
  if (text[i] === "}") {
    push("ph.close", i, i + 1);
    return i + 1;
  }
  push("error", i, text.length); // unterminated: whatever is left belongs to the placeholder
  return text.length;
}
