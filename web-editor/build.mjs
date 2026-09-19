/**
 * Bundle the editor into the bot's own static directory (ADR-0011).
 *
 * The output is committed, because the Docker image has no Node in it and the bot serves the file as it
 * stands. Run `npm run build` after changing anything here, and commit what it writes.
 */

import { build } from "esbuild";
import { mkdir } from "node:fs/promises";

const outfile = "../src/doomtp_bot/webui/static/editor/editor.js";

await mkdir(new URL("../src/doomtp_bot/webui/static/editor/", import.meta.url), { recursive: true });
const result = await build({
  entryPoints: ["src/index.js"],
  outfile,
  bundle: true,
  minify: true,
  sourcemap: false,
  format: "iife",
  target: ["es2020"],
  legalComments: "none",
  metafile: true,
  banner: { js: "/* doomtp-bot expression editor — built by web-editor/build.mjs, do not edit */" },
});
const bytes = Object.values(result.metafile.outputs)[0].bytes;
console.log(`${outfile} — ${(bytes / 1024).toFixed(0)} KiB`);
