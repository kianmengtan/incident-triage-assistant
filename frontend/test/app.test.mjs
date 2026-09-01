/* Structural guarantees for app.html, the page deploy.sh actually publishes.
 *
 * Unlike prototype.html this page is allowed to make network calls -- it is the
 * live console. What it is not allowed to do is drift from the design reference,
 * load anything from a third party, or carry a stale copy of the shared logic.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const frontendDir = join(dirname(fileURLToPath(import.meta.url)), '..');
const repoDir = join(frontendDir, '..');

const app = await readFile(join(frontendDir, 'app.html'), 'utf8');
const reference = await readFile(join(repoDir, 'design', 'reference.html'), 'utf8');

/** Every `--token: value;` declared inside the first :root block. */
function tokens(source) {
  const start = source.indexOf(':root');
  const end = source.indexOf('}', start);
  const body = source.slice(start, end);
  const found = new Map();
  for (const match of body.matchAll(/(--[a-z0-9-]+)\s*:\s*([^;]+);/g)) {
    found.set(match[1], match[2].trim());
  }
  return found;
}

test('the design tokens match design/reference.html exactly', () => {
  /* design/README.md makes reference.html authoritative for colour, type and
   * spacing, and forbids inventing adjacent shades. Comparing values rather than
   * eyeballing them is the only way that stays true after an edit. */
  const ref = tokens(reference);
  const mine = tokens(app);

  assert.ok(ref.size > 20, `expected a full palette in the reference, found ${ref.size}`);

  const drifted = [];
  for (const [name, value] of mine) {
    if (ref.has(name) && ref.get(name) !== value) {
      drifted.push(`${name}: ${value} (reference: ${ref.get(name)})`);
    }
  }
  assert.deepEqual(drifted, [], 'these tokens no longer match the reference');
});

test('no colour is invented outside the token set', () => {
  /* A raw hex anywhere but the :root block means a shade that the reference did
   * not supply, which is exactly what design/README.md rules out. */
  const rootEnd = app.indexOf('}', app.indexOf(':root'));
  const afterTokens = app.slice(rootEnd);
  const styleEnd = afterTokens.indexOf('</style>');
  const rules = afterTokens.slice(0, styleEnd);

  const hexes = [...rules.matchAll(/#[0-9a-fA-F]{3,8}\b/g)].map((m) => m[0]);
  assert.deepEqual(hexes, [], 'use a var(--token) instead of a literal colour');
});

test('the page loads nothing from a third party', () => {
  /* deploy.sh uploads exactly one HTML object, so anything external would either
   * 404 or hand a third party control of the console. */
  assert.doesNotMatch(app, /<script[^>]+\bsrc=/i, 'must not load external scripts');
  assert.doesNotMatch(app, /<link[^>]+stylesheet/i, 'must not load external stylesheets');
});

test('the only remote origin is the Cognito endpoint it authenticates against', () => {
  const origins = new Set(
    [...app.matchAll(/https?:\/\/[a-z0-9.\-]+/gi)].map((m) => m[0].toLowerCase())
  );
  origins.delete('http://www.w3.org');
  /* Built by interpolating the region, so the literal in the source is a prefix. */
  const allowed = [...origins].filter((o) => o.startsWith('https://cognito-idp.'));
  assert.deepEqual(
    [...origins].filter((o) => !allowed.includes(o)),
    [],
    'an unexpected remote origin appeared in app.html'
  );
});

test('the API base URL comes from config.json rather than being baked in', () => {
  /* The API id is generated at deploy time, so a hardcoded one would be wrong on
   * every redeploy that replaces the stack. */
  assert.match(app, /fetch\('config\.json'/);
  assert.match(app, /CONFIG\.adminApiUrl/);
  assert.doesNotMatch(app, /execute-api/, 'no hardcoded API Gateway URL');
});

test('the shared logic is inlined and in sync with lib/triage.mjs', async () => {
  const START = '/* --- triage:lib start --- */';
  const END = '/* --- triage:lib end --- */';
  const slice = (source) => {
    const from = source.indexOf(START);
    const to = source.indexOf(END);
    assert.notEqual(from, -1, 'missing lib start marker');
    assert.notEqual(to, -1, 'missing lib end marker');
    return source.slice(from, to + END.length).trim();
  };
  const lib = await readFile(join(frontendDir, 'lib', 'triage.mjs'), 'utf8');
  assert.equal(
    slice(app),
    slice(lib),
    'app.html is out of sync with lib/triage.mjs — run: node frontend/sync-lib.mjs'
  );
});

test('every capability the shell gates on exists in the matrix', async () => {
  /* `can` throws on an unknown capability, so a typo in a nav entry would take
   * the whole shell down at render rather than merely hiding one tab. */
  const { CAPABILITIES } = await import('../lib/triage.mjs');
  const gated = [...app.matchAll(/capability:\s*'([a-z_]+)'/g)].map((m) => m[1]);
  assert.ok(gated.length >= 5, `expected the nav to gate several views, found ${gated.length}`);
  for (const name of gated) {
    assert.ok(CAPABILITIES[name], `nav gates on unknown capability: ${name}`);
  }
});

test('the approval control is gated on the capability, not on a role string', () => {
  /* A literal role comparison here is how the UI and the server drift apart. */
  assert.match(app, /can\(session\.role, 'approve_remediation'\)/);
});

test('tokens are used for the focus ring, and it is the green not the accent', () => {
  /* The reference is explicit: a focus ring must never read as an error. */
  const ring = app.match(/:focus-visible\s*\{[^}]*\}/);
  assert.ok(ring, 'no :focus-visible rule found');
  assert.match(ring[0], /var\(--color-secondary\)/);
  assert.doesNotMatch(ring[0], /--color-accent/);
});

test('severity is never communicated by colour alone', () => {
  /* Design section 4 and the accessibility notes: the badge carries a glyph and,
   * for a screen reader, the severity in words -- not just a tone class. */
  const badge = app.match(/function severityBadge\(severity\)\s*\{[\s\S]*?\n\}/);
  assert.ok(badge, 'severityBadge not found');
  assert.match(badge[0], /meta\.glyph/, 'the badge needs a glyph');
  assert.match(badge[0], /meta\.label/, 'the badge needs the level in text');
  assert.match(badge[0], /sr-only[\s\S]*meta\.text/, 'the badge needs the severity in words for a screen reader');
});

test('tenant id is never sent to the API by the browser', () => {
  /* It comes from the verified token via the authorizer context. A tenant_id in
   * a request body is the multi-tenancy hole this codebase is careful about. */
  assert.doesNotMatch(app, /tenant_id\s*:/, 'do not put tenant_id in a request body');
});

test('the page declares a language and a viewport', () => {
  assert.match(app, /<html lang="en">/);
  assert.match(app, /name="viewport"/);
});

test('every function the app calls actually exists', async () => {
  /* The console is one file with an inlined library, so a call to a function that
   * was renamed or never exported is a ReferenceError at render time -- which
   * shows up as a blank screen, not as a broken button. Nothing else catches it:
   * the page has no build step and no module resolution.
   */
  const raw = app.slice(app.lastIndexOf('<script>'), app.lastIndexOf('</script>'));
  const libModule = await import('../lib/triage.mjs');

  /* Strip comments and string literals first. Without this the scan trips over
   * prose ("server-side (...)") and over CSS var(--token) inside HTML template
   * strings, neither of which is a call. */
  const appScript = raw
    .replace(/\/\*[\s\S]*?\*\//g, ' ')
    .replace(/\/\/[^\n]*/g, ' ')
    .replace(/'(?:[^'\\\n]|\\.)*'/g, "''")
    .replace(/"(?:[^"\\\n]|\\.)*"/g, '""');

  /* Declared inside the app script itself. */
  const local = new Set([
    ...[...appScript.matchAll(/function\s+([A-Za-z_$][\w$]*)\s*\(/g)].map((m) => m[1]),
    ...[...appScript.matchAll(/var\s+([A-Za-z_$][\w$]*)\s*=/g)].map((m) => m[1])
  ]);

  const platform = new Set([
    'fetch', 'setTimeout', 'clearTimeout', 'atob', 'btoa', 'escape', 'unescape',
    'encodeURIComponent', 'decodeURIComponent', 'isFinite', 'isNaN', 'parseInt',
    'parseFloat', 'String', 'Number', 'Boolean', 'Object', 'Array', 'JSON', 'Math',
    'Date', 'Promise', 'Error', 'RegExp', 'Set', 'Map',
    'if', 'for', 'while', 'switch', 'catch', 'return', 'typeof', 'function', 'new'
  ]);

  /* Bare `name(` calls: not preceded by a dot, so not a method. */
  const called = new Set(
    [...appScript.matchAll(/(?<![.\w$])([a-z_$][\w$]*)\s*\(/g)].map((m) => m[1])
  );

  const missing = [...called].filter(
    (name) => !local.has(name) && !platform.has(name) && !(name in libModule)
  );

  assert.deepEqual(missing, [], 'these are called but defined nowhere reachable');
});

test('the favicon is inlined, not a separate file request', () => {
  /* deploy.sh uploads exactly one HTML object, so a /favicon.ico reference would
   * 404 on the deployed console and the tab would fall back to a blank page icon. */
  const icon = app.match(/<link rel="icon"[^>]*>/);
  assert.ok(icon, 'no favicon declared');
  assert.match(icon[0], /^<link rel="icon" href="data:image\/svg\+xml,/);
  /* Reuses the palette rather than introducing a colour. */
  assert.match(icon[0], /%23A6482B/, 'the mark should use the accent token value');
});
