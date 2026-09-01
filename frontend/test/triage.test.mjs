import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import {
  SLA_BUDGET_MS,
  severityMeta,
  approvalMeta,
  stageTone,
  pipelineProgress,
  pipelineIsComplete,
  pipelineHasFailure,
  formatElapsed,
  slaState,
  canApprove,
  approvalBlockReason,
  isTenantMatch,
  extractIds,
  routeIntent,
  redact,
  redactAll,
  relativeTime,
  escapeHtml,
  richText
} from '../lib/triage.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const frontendDir = join(here, '..');

/* ---------- severity ---------- */

test('severity maps onto the reference badge tones', () => {
  assert.equal(severityMeta('SEV1').tone, 'danger');
  assert.equal(severityMeta('SEV2').tone, 'warning');
  assert.equal(severityMeta('SEV3').tone, 'neutral');
  assert.equal(severityMeta('SEV4').tone, 'neutral');
});

test('severity accepts the aliases external alert systems actually send', () => {
  assert.equal(severityMeta('critical').label, 'SEV1');
  assert.equal(severityMeta('P1').label, 'SEV1');
  assert.equal(severityMeta('  High  ').label, 'SEV2');
  assert.equal(severityMeta('warning').label, 'SEV3');
  assert.equal(severityMeta('minor').label, 'SEV4');
});

test('unknown severity degrades to a labelled neutral rather than throwing', () => {
  for (const input of [undefined, null, '', 'catastrophic', 42]) {
    const meta = severityMeta(input);
    assert.equal(meta.tone, 'neutral');
    assert.equal(meta.label, 'UNKNOWN');
    assert.ok(meta.text.length > 0);
  }
});

test('every severity carries a glyph and a word, never colour alone', () => {
  for (const key of ['SEV1', 'SEV2', 'SEV3', 'SEV4']) {
    const meta = severityMeta(key);
    assert.ok(meta.glyph && meta.glyph.length > 0, `${key} needs a glyph`);
    assert.ok(meta.text && meta.text.length > 0, `${key} needs a word`);
  }
});

/* ---------- approval status ---------- */

test('approval status maps to tones, and execution failure reads as danger', () => {
  assert.equal(approvalMeta('pending').tone, 'warning');
  assert.equal(approvalMeta('approved').tone, 'neutral');
  assert.equal(approvalMeta('failed').tone, 'danger');
  assert.equal(approvalMeta('declined').tone, 'neutral');
  assert.equal(approvalMeta('nonsense').tone, 'neutral');
});

/* The vocabulary the API actually emits. approvalMeta used to carry 'executed',
 * which no handler produces, and had no entry for any of the four execution_status
 * values that fn-trigger-remediation really writes -- so a successfully executed
 * remediation rendered as "Unknown". */
test('every status the backend emits has a label, and none reads as Unknown', () => {
  const approvalStatuses = ['pending', 'approved', 'declined'];
  const executionStatuses = ['not_started', 'in_progress', 'succeeded', 'failed', 'skipped'];
  for (const status of approvalStatuses.concat(executionStatuses)) {
    const meta = approvalMeta(status);
    assert.notEqual(meta.text, 'Unknown', `${status} has no label`);
    assert.ok(meta.glyph && meta.glyph.length > 0, `${status} needs a glyph`);
    assert.ok(meta.tone && meta.tone.length > 0, `${status} needs a tone`);
  }
});

test('a successful execution reads as success and a skipped one as a caveat', () => {
  assert.equal(approvalMeta('succeeded').tone, 'success');
  assert.equal(approvalMeta('skipped').tone, 'warning');
  assert.equal(approvalMeta('in_progress').tone, 'neutral');
});

/* ---------- pipeline ---------- */

test('stage tones cover every stage status', () => {
  assert.equal(stageTone('pending'), 'idle');
  assert.equal(stageTone('running'), 'active');
  assert.equal(stageTone('done'), 'success');
  assert.equal(stageTone('failed'), 'danger');
  assert.equal(stageTone('skipped'), 'warning');
  assert.equal(stageTone('made-up'), 'idle');
});

test('a skipped stage counts as resolved, because C-02 makes VCS data optional', () => {
  const stages = [
    { status: 'done' },
    { status: 'skipped' },
    { status: 'done' },
    { status: 'done' }
  ];
  assert.equal(pipelineProgress(stages), 1);
  assert.equal(pipelineIsComplete(stages), true);
  assert.equal(pipelineHasFailure(stages), false);
});

test('a running pipeline reports partial progress and is not complete', () => {
  const stages = [{ status: 'done' }, { status: 'running' }, { status: 'pending' }, { status: 'pending' }];
  assert.equal(pipelineProgress(stages), 0.25);
  assert.equal(pipelineIsComplete(stages), false);
});

test('a failed stage is detected and still counts toward resolution', () => {
  const stages = [{ status: 'done' }, { status: 'failed' }];
  assert.equal(pipelineHasFailure(stages), true);
  assert.equal(pipelineIsComplete(stages), true);
});

test('pipelineProgress tolerates junk input', () => {
  assert.equal(pipelineProgress([]), 0);
  assert.equal(pipelineProgress(null), 0);
  assert.equal(pipelineProgress(undefined), 0);
  assert.equal(pipelineProgress([null, undefined]), 0);
});

/* ---------- elapsed clock ---------- */

test('elapsed time formats as mm:ss with a padded seconds field', () => {
  assert.equal(formatElapsed(0), '0:00');
  assert.equal(formatElapsed(9000), '0:09');
  assert.equal(formatElapsed(134000), '2:14');
  assert.equal(formatElapsed(300000), '5:00');
});

test('elapsed time grows an hours field rather than showing 61:00', () => {
  assert.equal(formatElapsed(3600000), '1:00:00');
  assert.equal(formatElapsed(3725000), '1:02:05');
});

test('clock skew and junk clamp to 0:00 instead of rendering negatives', () => {
  assert.equal(formatElapsed(-5000), '0:00');
  assert.equal(formatElapsed(NaN), '0:00');
  assert.equal(formatElapsed(Infinity), '0:00');
  assert.equal(formatElapsed(undefined), '0:00');
});

/* ---------- SLA budget (NFR-01) ---------- */

test('SLA tone is calm early, warns in the final minute, and breaches past five', () => {
  assert.equal(slaState(30000).tone, 'ok');
  assert.equal(slaState(239000).tone, 'ok');
  assert.equal(slaState(240000).tone, 'warning', 'the 4:00 mark starts warning');
  assert.equal(slaState(299000).tone, 'warning');
  assert.equal(slaState(300000).tone, 'warning', 'exactly on budget is not yet a breach');
  assert.equal(slaState(300001).tone, 'danger');
});

test('SLA breach flag flips only after the budget is exceeded', () => {
  assert.equal(slaState(300000).breached, false);
  assert.equal(slaState(300001).breached, true);
});

test('SLA percentage is clamped to 0-100 so the bar never overflows its track', () => {
  assert.equal(slaState(0).pct, 0);
  assert.equal(slaState(150000).pct, 50);
  assert.equal(slaState(SLA_BUDGET_MS).pct, 100);
  assert.equal(slaState(99 * 60 * 1000).pct, 100);
  assert.equal(slaState(-1).pct, 0);
});

test('remaining time counts down and floors at zero', () => {
  assert.equal(slaState(60000).remainingMs, 240000);
  assert.equal(slaState(600000).remainingMs, 0);
});

test('a custom budget warns at 80 percent, which covers the 60s correlation target', () => {
  const sixty = 60000;
  assert.equal(slaState(47000, sixty).tone, 'ok');
  assert.equal(slaState(48000, sixty).tone, 'warning');
  assert.equal(slaState(61000, sixty).tone, 'danger');
});

test('an invalid budget falls back to the five-minute default', () => {
  assert.equal(slaState(1000, 0).budgetMs, SLA_BUDGET_MS);
  assert.equal(slaState(1000, -5).budgetMs, SLA_BUDGET_MS);
  assert.equal(slaState(1000, NaN).budgetMs, SLA_BUDGET_MS);
});

/* ---------- authorisation (Requirement 7) ---------- */

test('only TenantAdmin may approve remediation', () => {
  assert.equal(canApprove('TenantAdmin'), true);
  assert.equal(canApprove('TenantEngineer'), false);
  assert.equal(canApprove('TenantLeadership'), false);
});

test('role checking is exact, so no lookalike string slips the gate', () => {
  for (const role of ['tenantadmin', 'TENANTADMIN', 'TenantAdmin ', 'Admin', 'TenantAdminX', '', null, undefined]) {
    assert.equal(canApprove(role), false, `${JSON.stringify(role)} must not pass the gate`);
  }
});

test('a known non-admin gets an actionable reason, not a bare refusal', () => {
  const reason = approvalBlockReason('TenantEngineer');
  assert.ok(reason);
  assert.match(reason, /Tenant Admin/);
  assert.match(reason, /ask an admin/i);
});

test('an unrecognised role is told to sign in again', () => {
  assert.match(approvalBlockReason('whoever'), /sign in again/i);
});

test('an admin has no block reason at all', () => {
  assert.equal(approvalBlockReason('TenantAdmin'), null);
});

/* ---------- tenant isolation (Requirement 6.3 / C-01) ---------- */

test('tenant match requires two present, identical ids', () => {
  assert.equal(isTenantMatch('acme-retail', 'acme-retail'), true);
  assert.equal(isTenantMatch('acme-retail', 'globex'), false);
});

test('a missing tenant id on either side is a mismatch, never a pass', () => {
  for (const [a, b] of [['acme', ''], ['', 'acme'], [null, null], ['acme', undefined], [undefined, 'acme']]) {
    assert.equal(isTenantMatch(a, b), false);
  }
});

/* ---------- id extraction ---------- */

test('alert and runbook ids are lifted out of free text and normalised', () => {
  const ids = extractIds('why did alt-1041 fire, and where is rb-2291?');
  assert.deepEqual(ids.alertIds, ['ALT-1041']);
  assert.deepEqual(ids.runbookIds, ['RB-2291']);
});

test('text with no ids yields empty lists', () => {
  const ids = extractIds('what is going on with checkout');
  assert.deepEqual(ids.alertIds, []);
  assert.deepEqual(ids.runbookIds, []);
});

/* ---------- intent routing ---------- */

test('every approval phrasing routes to the card, never to an execution', () => {
  const phrasings = [
    'approve it',
    'approve RB-2291',
    'authorise the remediation',
    'authorize it please',
    'execute the runbook',
    'run it',
    'go ahead',
    'just do it',
    'ship it'
  ];
  for (const phrase of phrasings) {
    const { intent } = routeIntent(phrase);
    assert.equal(intent, 'focus_approval', `"${phrase}" must only focus the approval card`);
  }
});

test('declining also routes to the card, so the decision stays a deliberate click', () => {
  for (const phrase of ['decline', 'reject it', 'cancel that', 'stop']) {
    assert.equal(routeIntent(phrase).intent, 'focus_approval');
  }
});

test('no intent other than focus_approval can ever be produced for approval language', () => {
  /* Guards against a future keyword being added above the safety check. */
  const { intent } = routeIntent('approve the runbook and show me the logs');
  assert.equal(intent, 'focus_approval');
});

test('domain questions route to the right intent', () => {
  assert.equal(routeIntent('show me the runbook').intent, 'show_runbook');
  assert.equal(routeIntent('what changed before this?').intent, 'show_changes');
  assert.equal(routeIntent('any errors in the logs').intent, 'show_logs');
  assert.equal(routeIntent('who approved the last one').intent, 'show_audit');
  assert.equal(routeIntent('how long until it is done').intent, 'pipeline_status');
  assert.equal(routeIntent('what is broken right now').intent, 'list_incidents');
  assert.equal(routeIntent('why did this happen').intent, 'show_diagnosis');
  assert.equal(routeIntent('export it as json').intent, 'export_runbook');
  assert.equal(routeIntent('help').intent, 'help');
});

test('intent routing carries extracted ids through as params', () => {
  const routed = routeIntent('show me the runbook for ALT-1041');
  assert.equal(routed.intent, 'show_runbook');
  assert.equal(routed.params.alertId, 'ALT-1041');
});

test('a bare id is treated as asking about that incident', () => {
  assert.equal(routeIntent('ALT-1041').intent, 'show_diagnosis');
});

test('empty input is a no-op and unparseable input is explicitly unknown', () => {
  assert.equal(routeIntent('').intent, 'noop');
  assert.equal(routeIntent('   ').intent, 'noop');
  assert.equal(routeIntent(null).intent, 'noop');
  assert.equal(routeIntent('asdfghjkl').intent, 'unknown');
});

/* ---------- redaction ---------- */

test('emails, keys and credentials are redacted out of echoed log text', () => {
  assert.match(redact('user ada@example.com failed'), /‹redacted:email›/);
  assert.match(redact('AKIAIOSFODNN7EXAMPLE'), /‹redacted:aws-key›/);
  assert.match(redact('Authorization: Bearer abcdef1234567890xyz'), /‹redacted:credential›/);
  assert.match(redact('sid 9f8e7d6c5b4a39281706f5e4d3c2b1a0'), /‹redacted:token›/);
});

test('IP addresses keep their subnet, because operators need to know where', () => {
  assert.equal(redact('from 10.4.19.220'), 'from 10.4.x.x');
});

test('redaction leaves ordinary log text intact', () => {
  const line = 'ERROR checkout-api connection pool exhausted after 30s';
  assert.equal(redact(line), line);
});

test('redaction tolerates empty and nullish input', () => {
  assert.equal(redact(''), '');
  assert.equal(redact(null), '');
  assert.equal(redact(undefined), '');
});

/* ---------- relative time ---------- */

test('relative time reads short, as the narrow rail needs', () => {
  const now = 1_700_000_000_000;
  assert.equal(relativeTime(now - 30_000, now), 'just now');
  assert.equal(relativeTime(now - 120_000, now), '2m ago');
  assert.equal(relativeTime(now - 3 * 3_600_000, now), '3h ago');
  assert.equal(relativeTime(now - 2 * 86_400_000, now), '2d ago');
});

test('a future timestamp reads as just now rather than a negative age', () => {
  const now = 1_700_000_000_000;
  assert.equal(relativeTime(now + 60_000, now), 'just now');
});

/* ---------- escaping ---------- */

test('customer-controlled strings are escaped before reaching the DOM', () => {
  assert.equal(
    escapeHtml('<img src=x onerror="alert(1)">'),
    '&lt;img src=x onerror=&quot;alert(1)&quot;&gt;'
  );
  assert.equal(escapeHtml("O'Brien & co"), 'O&#39;Brien &amp; co');
  assert.equal(escapeHtml(null), '');
});

test('escaping runs before redaction placeholders, so markers survive intact', () => {
  const out = redact(escapeHtml('contact <b>ada@example.com</b>'));
  assert.match(out, /‹redacted:email›/);
  assert.match(out, /&lt;b&gt;/);
});

/* ---------- drift check ---------- */

test('the copy of the lib inlined in prototype.html has not drifted', async () => {
  const START = '/* --- triage:lib start --- */';
  const END = '/* --- triage:lib end --- */';

  const extract = (source, label) => {
    const from = source.indexOf(START);
    const to = source.indexOf(END);
    assert.ok(from !== -1, `${label} is missing the ${START} marker`);
    assert.ok(to !== -1, `${label} is missing the ${END} marker`);
    return source.slice(from, to + END.length).trim();
  };

  const lib = await readFile(join(frontendDir, 'lib', 'triage.mjs'), 'utf8');
  const html = await readFile(join(frontendDir, 'prototype.html'), 'utf8');

  assert.equal(
    extract(html, 'prototype.html'),
    extract(lib, 'lib/triage.mjs'),
    'prototype.html is out of date with lib/triage.mjs — run `node frontend/sync-lib.mjs`'
  );
});

test('prototype.html is genuinely self-contained: no imports, no network calls', async () => {
  const html = await readFile(join(frontendDir, 'prototype.html'), 'utf8');
  assert.doesNotMatch(html, /\bfrom\s+['"]\.\.?\//, 'must not import from disk; it is opened via file://');
  assert.doesNotMatch(html, /<script[^>]+\bsrc=/i, 'must not load external scripts');
  assert.doesNotMatch(html, /<link[^>]+stylesheet/i, 'must not load external stylesheets');
  assert.doesNotMatch(html, /\b(fetch|XMLHttpRequest|WebSocket)\s*\(/, 'prototype runs on mock data only');
  assert.doesNotMatch(html, /https?:\/\/(?!www\.w3\.org)/, 'no remote origins');
});

/* ---------- rich text (allowlisted inline markup) ---------- */

test('rich text keeps the inline emphasis the design uses', () => {
  assert.equal(
    richText('pool <strong>d-8841</strong> set <code>DB_POOL_MAX</code> to 8'),
    'pool <strong>d-8841</strong> set <code>DB_POOL_MAX</code> to 8'
  );
  assert.equal(richText('a <em>b</em> c'), 'a <em>b</em> c');
  assert.equal(richText('one <br/> two'), 'one <br> two');
});

test('rich text refuses every tag outside the allowlist', () => {
  for (const payload of [
    '<img src=x onerror="alert(1)">',
    '<script>alert(1)</script>',
    '<iframe src="evil"></iframe>',
    '<svg onload=alert(1)>',
    '<a href="javascript:alert(1)">x</a>',
    '<div>x</div>'
  ]) {
    const out = richText(payload);
    assert.doesNotMatch(out, /<(img|script|iframe|svg|a|div)\b/i, `${payload} must not survive`);
  }
});

test('rich text strips attributes even from allowlisted tags', () => {
  /* This is the property that makes the allowlist safe: a tag is only re-opened
     when nothing but whitespace or a slash sits between its name and the '>'. */
  const out = richText('<code onclick="steal()">x</code>');
  assert.doesNotMatch(out, /<code[^>]*onclick/i);
  assert.match(out, /&lt;code onclick/);
});

test('rich text tolerates nullish input', () => {
  assert.equal(richText(null), '');
  assert.equal(richText(undefined), '');
  assert.equal(richText(''), '');
});

/* ---------- redaction across a whole record ---------- */

test('redactAll redacts every value it is given', () => {
  const out = redactAll([
    'contact ada@example.com',
    'sid 9f8e7d6c5b4a39281706f5e4d3c2b1a0',
    'Authorization: Bearer abcdef1234567890xyz',
    'ERROR pool exhausted'
  ]);
  assert.match(out[0], /‹redacted:email›/);
  assert.match(out[1], /‹redacted:token›/);
  assert.match(out[2], /‹redacted:credential›/);
  assert.equal(out[3], 'ERROR pool exhausted');
});

test('redactAll tolerates a missing list', () => {
  assert.deepEqual(redactAll(null), []);
  assert.deepEqual(redactAll(undefined), []);
  assert.deepEqual(redactAll([]), []);
});

/* ---------- SLA warning threshold is one rule, not a special case ---------- */

test('the warning threshold is the same fraction of every budget', () => {
  /* 4:00 of a 5:00 budget is 0.8, which is what the general rule computes — the
     removed special case produced an identical number by a second route. */
  assert.equal(slaState(4 * 60 * 1000 - 1).tone, 'ok');
  assert.equal(slaState(4 * 60 * 1000).tone, 'warning');
  assert.equal(slaState(0.8 * 60000, 60000).tone, 'warning');
  assert.equal(slaState(0.8 * 60000 - 1, 60000).tone, 'ok');
});

/* ---------- static guards on the prototype's render paths ---------- */

test('model-authored prose is never interpolated into HTML unfiltered', async () => {
  /* The render functions build HTML by concatenation, so a raw `+ dx.cause +`
     puts Bedrock output straight into innerHTML. Every one of these fields must
     go through richText() or escapeHtml() at every use. */
  const html = await readFile(join(frontendDir, 'prototype.html'), 'utf8');
  const proseFields = [
    'dx.cause',
    'dx.model',
    'stage.detail',
    's.text',
    'incident.diagnosis.cause'
  ];
  for (const field of proseFields) {
    const raw = new RegExp(`\\+\\s*${field.replace(/\./g, '\\.')}\\s*\\+`, 'g');
    const hits = html.match(raw) || [];
    assert.equal(
      hits.length,
      0,
      `${field} is concatenated into HTML unfiltered ${hits.length} time(s); wrap it in richText() or escapeHtml()`
    );
  }
});

test('identifiers are escaped wherever they reach the DOM', async () => {
  const html = await readFile(join(frontendDir, 'prototype.html'), 'utf8');
  for (const field of ['incident.id', 'rb.id', 'state.tenantId']) {
    const raw = new RegExp(`\\+\\s*${field.replace(/\./g, '\\.')}\\s*\\+`, 'g');
    const hits = html.match(raw) || [];
    assert.equal(hits.length, 0, `${field} reaches the DOM unescaped ${hits.length} time(s)`);
  }
});

test('the transcript is not itself an aria-live region', async () => {
  /* The typewriter mutates text inside it every 16ms; a live region there makes
     a screen reader read partial words over and over. Announcements go to
     #srStatus once per completed turn instead. */
  const html = await readFile(join(frontendDir, 'prototype.html'), 'utf8');
  const transcript = html.match(/<div class="transcript-inner"[^>]*>/)[0];
  assert.doesNotMatch(transcript, /aria-live/);
  assert.match(html, /id="srStatus"[^>]*aria-live="polite"/);
});

test('the incident rail does not claim invalid listbox semantics', async () => {
  /* As a listbox it had section-header children and interactive buttons as its
     options, neither of which ARIA allows, and no roving tabindex to make the
     pattern usable. */
  const html = await readFile(join(frontendDir, 'prototype.html'), 'utf8');
  assert.doesNotMatch(html, /id="railList"[^>]*role="listbox"/);
  assert.doesNotMatch(html, /role="option"/);
});

test('the prototype has no inline event handlers', async () => {
  /* Inline handlers are also the one thing that would break when the page is
     served from CloudFront under a script-src CSP. */
  const html = await readFile(join(frontendDir, 'prototype.html'), 'utf8');
  assert.doesNotMatch(html, /\son[a-z]+\s*=\s*["']/i);
});

test('deferred work is generation-guarded so it cannot cross incidents', async () => {
  const html = await readFile(join(frontendDir, 'prototype.html'), 'utf8');
  assert.match(html, /function later\(/, 'a generation-aware setTimeout wrapper must exist');
  assert.match(html, /function cancelPendingWork\(/);
  assert.match(html, /cancelPendingWork\(\);/);
  /* No bare setTimeout should remain in the view-mutating paths. The only
     allowed ones are inside copyCommand's label reset, which is element-local. */
  const bare = (html.match(/\bsetTimeout\(/g) || []).length;
  assert.ok(bare <= 2, `expected at most 2 bare setTimeout calls, found ${bare}`);
});
