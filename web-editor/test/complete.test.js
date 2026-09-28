/** Where the cursor is decides which list `/api/v1/language` is asked for (ADR-0011). */

import { describe, expect, it } from "vitest";
import { where } from "../src/complete.js";

// "‸" marks the cursor; a plain "|" would collide with the pipe operator.
const at = (text) => where(text.replace("‸", ""), text.indexOf("‸"));

describe("the cursor", () => {
  it("names a command at the start of a line and after an operator", () => {
    expect(at("ec‸").kind).toBe("command");
    expect(at("echo hi | ec‸").kind).toBe("command");
    expect(at("echo a && ec‸").kind).toBe("command");
  });

  it("names an argument nothing in particular", () => {
    expect(at("echo hi th‸").kind).toBe("none");
  });

  it("is inside a placeholder root until the first dot", () => {
    const root = at("echo {chat‸");
    expect(root.kind).toBe("root");
    expect(root.from).toBe(6);
    expect(at("echo {channel.stats[ki‸").kind).toBe("none"); // keys belong to the value, not to a list
    expect(at("echo {x + y‸").kind).toBe("none");
    expect(at("echo {arg.1} th‸").kind).toBe("none"); // the placeholder is closed again
  });

  it("offers the bot's fields after a $ root", () => {
    const field = at("echo {$chatter.di‸");
    expect(field).toEqual({ kind: "field", root: "$chatter", from: 15 });
    expect(at("echo {1 + $now.‸").kind).toBe("field");
  });

  it("offers a command inside {!…}", () => {
    expect(at("echo {!ran‸")).toEqual({ kind: "command", from: 7 });
  });

  it("offers types after a colon", () => {
    expect(at("echo {arg.1:i‸").kind).toBe("type");
    expect(at("echo {channel.log:l‸").kind).toBe("type");
  });

  it("offers variables to store into", () => {
    expect(at("echo hi -> cha‸").kind).toBe("variable");
    expect(at("echo hi --> cha‸").kind).toBe("variable");
    expect(at("echo hi > cha‸").kind).toBe("none"); // `>` is a plain word since 2.0
  });
});
