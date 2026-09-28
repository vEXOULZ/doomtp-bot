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
const OPERATORS = ["||", "|", "&&", "-->", "->", "(", ")", ";"];
const STORES = new Set(["->", "-->"]);
const OPENS_COMMAND = new Set(["|", "||", "&&", "("]);
// Operators inside an expression (spec §2.8), longest first. `and or not in` are words, handled apart.
const EXPR_OPERATORS = ["//", "==", "!=", "<=", ">=", "??", "+", "-", "*", "/", "%", "<", ">"];
const EXPR_WORDS = new Set(["and", "or", "not", "in"]);
const EXPR_LITERALS = new Set(["true", "false"]);
// `check` and `calc` take one expression instead of words (spec §3.1).
const EXPR_COMMANDS = new Set(["check", "calc"]);

const isSpace = (ch) => /\s/.test(ch);
const isNameChar = (ch) => /[A-Za-z0-9_-]/.test(ch);
const isRootChar = (ch) => /[A-Za-z0-9_]/.test(ch);
const PATH_SEGMENT = /^\.([0-9]+(\+raw\b|\+(?=[\s}:\])]|$))?|[A-Za-z0-9_]+)/;

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
    // `🏜1 + 3` is a bare expression line (spec §3.5): all of it one expression, with an operator in it.
    if (/[0-9({-]/.test(text[i] || "")) {
      const tokens = [];
      const lexed = expression(text, i, (t, s, e) => e > s && tokens.push({ t, s, e }), 1, () => false);
      if (lexed.operator && text.slice(lexed.i).trim() === "") return out.concat(tokens);
    }
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
        const name = text.slice(i, named);
        i = named;
        if (EXPR_COMMANDS.has(name.toLowerCase())) {
          i = expression(text, i, push, 1, atStageEnd).i;
        }
        continue;
      }
    }
    i = argument(text, i, push, store);
    store = false;
  }
  return out;
}

/** An operator only counts when it stands alone between spaces, so `(lol)` and `a->b` stay words. */
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

/** An expression outside a placeholder ends where the stage does: a standalone `|`, `&&`, `->`, `)`… */
function atStageEnd(text, i, depth) {
  return depth === 0 && operatorAt(text, i) !== null;
}

/**
 * One whitespace-delimited argument: quotes, escapes and placeholders, glued together. Inside `{!…}`
 * (`inSubst`) a `}` ends it too.
 */
function argument(text, i, push, store, inSubst = false) {
  const start = i;
  const parts = [];
  const ends = (k) => isSpace(text[k]) || (inSubst && text[k] === "}");
  while (i < text.length && !ends(i)) {
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
      while (i < text.length && !ends(i) && !'"{\\'.includes(text[i])) i += 1;
      parts.push(["word", from, i]);
    }
  }
  // A store target is one word — `-> channel.stats[kills]` — but it is still written with the same pieces.
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

/**
 * `{expression ?? fallback}` or `{!command args}`. The fallback is text, and may hold placeholders of its
 * own; `??` inside parentheses is an operator instead.
 */
function placeholder(text, i, push, depth) {
  push("ph.open", i, i + 1);
  i += 1;
  if (text[i] === "!") {
    push("ph.op", i, i + 1);
    i = commandAt(text, i + 1, push);
    while (i < text.length && text[i] !== "}") {
      if (isSpace(text[i])) i += 1;
      else i = argument(text, i, push, false, true);
    }
  } else {
    const atEnd = (s, k, open) => open === 0 && (s[k] === "}" || s.startsWith("??", k));
    i = expression(text, i, push, depth, atEnd).i;
    if (text.startsWith("??", i)) {
      push("ph.fallback", i, i + 2);
      i += 2;
      while (i < text.length && isSpace(text[i])) i += 1;
      let run = i;
      while (i < text.length && text[i] !== "}") {
        if (text[i] === "{") {
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
  }
  if (text[i] === "}") {
    push("ph.close", i, i + 1);
    return i + 1;
  }
  push("error", i, text.length); // unterminated: whatever is left belongs to the placeholder
  return text.length;
}

/**
 * Operands, operators, `( )`, `[ ]`, `:accessors`, strings and nested placeholders, until `atEnd` says
 * stop. Returns where it stopped, and whether it saw an operator (which is what makes `🏜1 + 3` a
 * calculation and `🏜100` not).
 */
function expression(text, i, push, depth, atEnd) {
  let open = 0;
  let operator = false;
  while (i < text.length) {
    const ch = text[i];
    if (isSpace(ch)) {
      i += 1;
      continue;
    }
    if (atEnd(text, i, open)) break;
    if (ch === "{") {
      i = placeholder(text, i, push, depth + 1);
    } else if (ch === '"') {
      i = quoted(text, i, push);
    } else if (ch === "(" || ch === "[") {
      push("ph.op", i, i + 1);
      open += 1;
      i += 1;
      if (ch === "[") i = bareKey(text, i, push);
    } else if (ch === ")" || ch === "]") {
      if (open === 0) break;
      push("ph.op", i, i + 1);
      open -= 1;
      i += 1;
    } else if (ch === ":") {
      let end = i + 1;
      while (end < text.length && isSpace(text[end])) end += 1;
      while (end < text.length && isRootChar(text[end])) end += 1;
      if (text[end] === "(") {
        // choice(rock,paper,scissors): everything up to its own closing paren is the type
        while (end < text.length && text[end] !== ")" && text[end] !== "}") end += 1;
        if (text[end] === ")") end += 1;
      }
      push("ph.type", i, end);
      i = end;
    } else if (/[0-9]/.test(ch)) {
      const number = /^[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?/.exec(text.slice(i))[0];
      push("ph.num", i, i + number.length);
      i += number.length;
    } else if (ch === "$" || isRootChar(ch)) {
      let end = i + 1;
      while (end < text.length && isRootChar(text[end])) end += 1;
      const word = text.slice(i, end);
      if (EXPR_WORDS.has(word)) {
        push("ph.op", i, end);
        operator = true;
        i = end;
        continue;
      }
      push(EXPR_LITERALS.has(word) ? "ph.num" : "ph.root", i, end);
      i = end;
      // `.name`, `.1`, `.3+`, `.3+raw`; a `+` or `-` followed by an operand is an operator instead
      let seg;
      while ((seg = PATH_SEGMENT.exec(text.slice(i)))) {
        push("ph.path", i, i + seg[0].length);
        i += seg[0].length;
      }
    } else {
      const op = EXPR_OPERATORS.find((candidate) => text.startsWith(candidate, i));
      push(op ? "ph.op" : "error", i, i + (op ? op.length : 1));
      operator = operator || Boolean(op);
      i += op ? op.length : 1;
    }
  }
  return { i, operator };
}

/** `[kills]`: a bare word right before `]` is a literal key, so it reads like a path, not a reference. */
function bareKey(text, i, push) {
  let start = i;
  while (start < text.length && isSpace(text[start])) start += 1;
  let end = start;
  while (end < text.length && isRootChar(text[end])) end += 1;
  let after = end;
  while (after < text.length && isSpace(text[after])) after += 1;
  const word = text.slice(start, end);
  if (end > start && text[after] === "]" && !/^[0-9]/.test(word) && !/^_[0-9]*$/.test(word)) {
    push("ph.path", start, end);
    return end;
  }
  return i;
}
