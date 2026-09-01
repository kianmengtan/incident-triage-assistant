/* Inlines frontend/lib/triage.mjs into the marked block in prototype.html.
 *
 * The prototype has to stay a single self-contained file that opens from disk
 * with no server and no build, but the logic inside it still has to be
 * unit-testable. So the module is the source of truth and this copies it in.
 * `node --test frontend/test/*.test.mjs` fails if the two ever drift.
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
const htmlPath = join(here, 'prototype.html');

function block(source, label) {
  const from = source.indexOf(START);
  const to = source.indexOf(END);
  if (from === -1 || to === -1) throw new Error(`${label} is missing the triage:lib markers`);
  return { from, to: to + END.length, text: source.slice(from, to + END.length) };
}

const lib = block(readFileSync(libPath, 'utf8'), 'lib/triage.mjs');
const html = readFileSync(htmlPath, 'utf8');
const target = block(html, 'prototype.html');

if (target.text.trim() === lib.text.trim()) {
  console.log('prototype.html is already in sync with lib/triage.mjs');
  process.exit(0);
}
if (process.argv.includes('--check')) {
  console.error('prototype.html is OUT OF SYNC with lib/triage.mjs — run: node frontend/sync-lib.mjs');
  process.exit(1);
}

writeFileSync(htmlPath, html.slice(0, target.from) + lib.text + html.slice(target.to));
console.log(`synced ${lib.text.split('\n').length} lines of lib into prototype.html`);
