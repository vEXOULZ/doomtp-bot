/** The theming API is a contract with doomtp-web: every `--dtb-*` property used is listed and documented. */

import { readFileSync, readdirSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { CHROME_CSS, PROPERTIES, v } from "../src/properties.js";

const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const used = (text) => new Set(text.match(/--dtb-[a-z0-9-]+/g) ?? []);

describe("the custom properties", () => {
  it("fall back to the page's older variable, then to a default", () => {
    expect(v("--dtb-bg")).toBe("var(--dtb-bg, var(--panel, #16181d))");
    expect(v("--dtb-radius")).toBe("var(--dtb-radius, .4rem)");
    expect(() => v("--dtb-nope")).toThrow();
  });

  it("are the only ones the sources use", () => {
    const sources = readdirSync(new URL("../src/", import.meta.url)).map((name) => read(`../src/${name}`));
    const all = new Set(sources.flatMap((text) => [...used(text)]));
    expect([...all].sort()).toEqual(Object.keys(PROPERTIES).sort());
  });

  it("are each documented in the README", () => {
    const documented = used(read("../README.md"));
    for (const name of Object.keys(PROPERTIES)) expect(documented, name).toContain(name);
    for (const name of documented) expect(PROPERTIES, name).toHaveProperty([name]);
  });

  it("style the button and the report without outweighing the page", () => {
    for (const rule of CHROME_CSS.match(/^[^\s{}][^{]*\{/gm)) expect(rule).toMatch(/^:where\(/);
  });
});
