/* Inlines frontend/lib/triage.mjs into the marked block of every page that uses
 * it.
 *
 * Both pages have to stay single self-contained files -- prototype.html because
 * it opens from disk with no server and no build, app.html because deploy.sh
 * uploads exactly one HTML object -- while the logic inside them stays
 * unit-testable. So the module is the source of truth and this copies it in.
 * `node --test frontend/test/*.test.mjs` fails if any copy drifts.
 *
 * Usage: node frontend/sync-lib.mjs [--check]
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const START = '/* --- triage:lib start --- */';
const END = '/* --- triage:lib end --- */';
const here = dirname(fileURLToPath(import.meta.url));
const libPath = join(here, 'lib', 'triage.mjs');
const TARGETS = ['prototype.html', 'app.html'];

function block(source, label) {
  const from = source.indexOf(START);
  const to = source.indexOf(END);
  if (from === -1 || to === -1) throw new Error(`${label} is missing the triage:lib markers`);
  return { from, to: to + END.length, text: source.slice(from, to + END.length) };
}

const lib = block(readFileSync(libPath, 'utf8'), 'lib/triage.mjs');
const checking = process.argv.includes('--check');

let drifted = 0;
let written = 0;

for (const name of TARGETS) {
  const path = join(here, name);
  const html = readFileSync(path, 'utf8');
  const target = block(html, name);

  if (target.text.trim() === lib.text.trim()) {
    console.log(`${name} is already in sync with lib/triage.mjs`);
    continue;
  }

  if (checking) {
    console.error(`${name} is OUT OF SYNC with lib/triage.mjs — run: node frontend/sync-lib.mjs`);
    drifted += 1;
    continue;
  }

  writeFileSync(path, html.slice(0, target.from) + lib.text + html.slice(target.to));
  console.log(`synced ${lib.text.split('\n').length} lines of lib into ${name}`);
  written += 1;
}

if (drifted) process.exit(1);
if (!checking && !written) process.exit(0);
