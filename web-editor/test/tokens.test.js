/**
 * Token-class tests (ADR-0011 item 5). The lexer only colours text, so these assert classes and spans —
 * never validity. The shared corpus (`../tests/lang/corpus.yaml`, spec Appendix A) is the drift alarm:
 * anything the server parses happily has to lex cleanly here too.
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { parse as parseYaml } from "yaml";
import { tokenize } from "../src/tokens.js";

const corpus = parseYaml(
  readFileSync(fileURLToPath(new URL("../../tests/lang/corpus.yaml", import.meta.url)), "utf8"),
);

/** "!echo hi" → "command:echo word:hi", so a case reads as what it should look like on screen. */
const classes = (text, options) =>
  tokenize(text, options)
    .map(({ t, s, e }) => `${t}:${text.slice(s, e)}`)
    .join(" ");

describe("the lexer", () => {
  it("colours a command line", () => {
    expect(classes("!random 1-100 | echo a {_1}!", { prefix: "!", context: "line" })).toBe(
      "prefix:! command:random word:1-100 operator:| command:echo word:a " +
        "ph.open:{ ph.root:_1 ph.close:} word:!",
    );
  });

  it("reads an emoji sign, with or without the space it allows", () => {
    for (const text of ["\u{1F3DC}ping", "\u{1F3DC} ping"]) {
      expect(classes(text, { context: "line" })).toBe("prefix:\u{1F3DC} command:ping");
    }
  });

  it("only treats an operator as one when it stands alone", () => {
    expect(classes("echo a|b ;) a->b > (lol)")).toBe(
      "command:echo word:a|b word:;) word:a->b word:> word:(lol)",
    );
    expect(classes("echo a | b")).toBe("command:echo word:a operator:| command:b");
  });

  it("keeps quotes, escapes and the words they are glued to apart", () => {
    expect(classes('echo ab"c d"e')).toBe('command:echo word:ab string:"c d" word:e');
    expect(classes("echo \\| literal")).toBe("command:echo escape:\\| word:literal");
    expect(classes('echo "a \\" b"')).toBe('command:echo string:"a  escape:\\" string: b"');
  });

  it("takes a placeholder apart", () => {
    expect(classes("echo {arg.1:int ?? 20}")).toBe(
      "command:echo ph.open:{ ph.root:arg ph.path:.1 ph.type::int ph.fallback:?? " +
        "ph.fallback:20 ph.close:}",
    );
    expect(classes("echo {$chatter.name}")).toBe(
      "command:echo ph.open:{ ph.root:$chatter ph.path:.name ph.close:}",
    );
  });

  it("colours a placeholder inside a fallback", () => {
    expect(classes("echo {arg.1 ?? {$chatter.display}}")).toBe(
      "command:echo ph.open:{ ph.root:arg ph.path:.1 ph.fallback:?? ph.open:{ " +
        "ph.root:$chatter ph.path:.display ph.close:} ph.close:}",
    );
  });

  it("colours an expression inside a placeholder", () => {
    expect(classes("echo {channel.deaths * 2 > 10}")).toBe(
      "command:echo ph.open:{ ph.root:channel ph.path:.deaths ph.op:* ph.num:2 ph.op:> ph.num:10 ph.close:}",
    );
    expect(classes('echo {(arg.1 ?? "") == "add" and not true}')).toBe(
      "command:echo ph.open:{ ph.op:( ph.root:arg ph.path:.1 ph.op:?? string:\"\" ph.op:) ph.op:== " +
        'string:"add" ph.op:and ph.op:not ph.num:true ph.close:}',
    );
    expect(classes("echo {channel.deaths-1}")).toBe(
      "command:echo ph.open:{ ph.root:channel ph.path:.deaths ph.op:- ph.num:1 ph.close:}",
    );
  });

  it("reads brackets, accessors and result references", () => {
    expect(classes("echo {channel.stats[kills]} {channel.log[-1]} {channel.q[arg.1]:len}")).toBe(
      "command:echo ph.open:{ ph.root:channel ph.path:.stats ph.op:[ ph.path:kills ph.op:] ph.close:} " +
        "ph.open:{ ph.root:channel ph.path:.log ph.op:[ ph.op:- ph.num:1 ph.op:] ph.close:} " +
        "ph.open:{ ph.root:channel ph.path:.q ph.op:[ ph.root:arg ph.path:.1 ph.op:] ph.type::len ph.close:}",
    );
    expect(classes("echo {_1.code} {_[_1]}")).toBe(
      "command:echo ph.open:{ ph.root:_1 ph.path:.code ph.close:} " +
        "ph.open:{ ph.root:_ ph.op:[ ph.root:_1 ph.op:] ph.close:}",
    );
  });

  it("colours a command substitution", () => {
    expect(classes("echo you rolled {!random 1-6}!")).toBe(
      "command:echo word:you word:rolled ph.open:{ ph.op:! command:random word:1-6 ph.close:} word:!",
    );
  });

  it("reads check and calc as one expression", () => {
    expect(classes("check {channel.deaths} > 3 && echo rough")).toBe(
      "command:check ph.open:{ ph.root:channel ph.path:.deaths ph.close:} ph.op:> ph.num:3 " +
        "operator:&& command:echo word:rough",
    );
    expect(classes("@calc 1 + 2")).toBe("personal:@ command:calc word:1 word:+ word:2");
  });

  it("reads a bare expression line, and leaves a lone number alone", () => {
    expect(classes("\u{1F3DC}(2 + 3) * 4", { context: "line" })).toBe(
      "prefix:\u{1F3DC} ph.op:( ph.num:2 ph.op:+ ph.num:3 ph.op:) ph.op:* ph.num:4",
    );
    expect(classes("\u{1F3DC}100", { context: "line" })).toBe("prefix:\u{1F3DC} command:100");
  });

  it("reads ifelse as a command with groups", () => {
    expect(classes("ifelse {x.y > 1} ( echo a ) ( echo b )")).toBe(
      "command:ifelse ph.open:{ ph.root:x ph.path:.y ph.op:> ph.num:1 ph.close:} operator:( " +
        "command:echo word:a operator:) operator:( command:echo word:b operator:)",
    );
  });

  it("names the variable a result is stored in", () => {
    expect(classes("echo hi -> channel.greeting")).toBe(
      "command:echo word:hi operator:-> store.target:channel.greeting",
    );
    expect(classes("echo hi --> chatter.log")).toBe(
      "command:echo word:hi operator:--> store.target:chatter.log",
    );
    expect(classes("echo 3 -> channel.stats[kills]")).toBe(
      "command:echo word:3 operator:-> store.target:channel.stats[kills]",
    );
  });

  it("marks a personal alias", () => {
    expect(classes("@hype arg")).toBe("personal:@ command:hype word:arg");
  });

  it("underlines text that stops making sense, and nothing else", () => {
    expect(classes('echo "unterminated')).toBe('command:echo error:"unterminated');
    expect(classes("echo {arg.1")).toBe("command:echo ph.open:{ ph.root:arg ph.path:.1");
  });
});

describe("the shared corpus", () => {
  const cases = corpus.cases.filter((c) => c.ast && !c.raw_tail);

  it("has cases to check", () => {
    expect(cases.length).toBeGreaterThan(20);
  });

  it.each(cases.map((c) => [c.id, c]))("lexes %s cleanly", (_id, testCase) => {
    const options = { prefix: testCase.prefix || "!", context: testCase.context || "line" };
    const tokens = tokenize(testCase.input, options);
    // Nothing the server accepts may be underlined as nonsense, and spans stay ordered and inside.
    expect(tokens.filter((t) => t.t === "error")).toEqual([]);
    let at = 0;
    for (const { s, e } of tokens) {
      expect(s).toBeGreaterThanOrEqual(at);
      expect(e).toBeGreaterThan(s);
      expect(e).toBeLessThanOrEqual(testCase.input.length);
      at = e;
    }
    // Everything that isn't whitespace belongs to some token.
    const covered = new Set();
    for (const { s, e } of tokens) for (let k = s; k < e; k += 1) covered.add(k);
    const missed = [];
    for (let k = 0; k < testCase.input.length; k += 1) {
      if (!/\s/.test(testCase.input[k]) && !covered.has(k)) missed.push(k);
    }
    expect(missed).toEqual([]);
  });
});
