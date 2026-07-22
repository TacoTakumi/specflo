/**
 * Suite entry point for `node --test src/specflo/extension/test/`.
 *
 * Node 24 treats a positional argument to `--test` as a glob pattern, not as a
 * directory to search: a bare directory path resolves through directory-index
 * lookup instead of being expanded. That lookup knows `index.js` and not
 * `index.ts`, so this barrel is plain JavaScript (ESM, per the package's
 * `"type": "module"`) and the `.ts` suites are pulled in from here.
 *
 * The imports are discovered, never listed: every `*.test.ts` beside this file
 * is loaded, so adding a suite needs no edit here and no suite can be silently
 * left out of the run.
 *
 * pi never loads this file. Discovery resolves the installed package through
 * `package.json`'s `pi.extensions`, which points into `src/` only.
 */

import { readdirSync } from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));

for (const name of readdirSync(here)
  .filter((entry) => entry.endsWith(".test.ts"))
  .sort()) {
  await import(path.join(here, name));
}
