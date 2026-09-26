/**
 * Bundle the editor into the bot's own static directory (ADR-0011).
 *
 * The output is committed, because the Docker image has no Node in it and the bot serves the file as it
 * stands. Run `npm run build` after changing anything here, and commit what it writes.
 *
 * Two files: `editor.js`, the `<dtb-editor>` element for a page to load with a <script>, and `tokens.js`,
 * the lexer on its own as an ES module, so a site on the same origin can colour command text the way the
 * editor does (`import("/static/editor/tokens.js")`, ADR-0016) without shipping a copy that could drift.
 */

import { build } from "esbuild";
import { mkdir } from "node:fs/promises";

const outdir = "../src/doomtp_bot/webui/static/editor";
const banner = "/* doomtp-bot expression editor — built by web-editor/build.mjs, do not edit */";

await mkdir(new URL(`${outdir}/`, import.meta.url), { recursive: true });

const bundles = [
  { entryPoints: ["src/index.js"], outfile: `${outdir}/editor.js`, format: "iife" },
  { entryPoints: ["src/tokens.js"], outfile: `${outdir}/tokens.js`, format: "esm" },
];
for (const options of bundles) {
  const result = await build({
    ...options,
    bundle: true,
    minify: true,
    sourcemap: false,
    target: ["es2020"],
    legalComments: "none",
    metafile: true,
    banner: { js: banner },
  });
  const bytes = Object.values(result.metafile.outputs)[0].bytes;
  console.log(`${options.outfile} — ${(bytes / 1024).toFixed(1)} KiB`);
}
