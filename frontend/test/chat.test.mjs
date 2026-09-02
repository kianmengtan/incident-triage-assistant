/* The Ask surface in app.html.
 *
 * These are static assertions on the source, like the rest of the front-end
 * suite: the page has no build step and no module resolution, so a structural
 * mistake here shows up as a blank screen in a browser rather than as a failure
 * anywhere else.
 *
 * The bulk of the file is about one property. Typing an approval must never
 * approve anything. The console enforces that before any request is made, and
 * these tests are what stop a later edit from quietly routing the text to the
 * API instead.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const frontendDir = join(dirname(fileURLToPath(import.meta.url)), '..');
const app = await readFile(join(frontendDir, 'app.html'), 'utf8');

/** The body of a named function declaration in the page. */
function fn(name) {
  const start = app.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `${name} is not defined in app.html`);
  const end = app.indexOf('\n}', start);
  assert.notEqual(end, -1, `${name} has no closing brace`);
  return app.slice(start, end + 2);
}

test('the Ask view is registered in the nav and dispatched by show()', () => {
  assert.match(app, /\{ id: 'ask',\s+label: 'Ask',\s+capability: 'view_incidents' \}/);
  assert.match(app, /ask: renderAsk/);
});

test('Ask is the landing view', () => {
  /* design/frontend-design.md argues for a chat-primary console. If this ever
   * needs to change it should change deliberately, not by drift. */
  assert.match(app, /var current = 'ask';/);
});

test('the composer posts to the chat route and nothing else', () => {
  const submit = fn('askSubmit');
  assert.match(submit, /api\('\/v1\/chat', \{ method: 'POST'/);

  /* The routes that change state must not be reachable from this function. */
  for (const route of ['/approve', '/decline', '/v1/alerts\'', '/v1/team']) {
    assert.ok(!submit.includes(route), `askSubmit must not call ${route}`);
  }
});

test('an approval phrase is answered locally, before any request', () => {
  /* Layer 1 of three. The whole point is that the branch returns without
   * reaching the network at all. */
  const submit = fn('askSubmit');
  const guard = submit.indexOf("routeIntent(question)");
  const post = submit.indexOf("api('/v1/chat'");
  assert.notEqual(guard, -1, 'askSubmit must consult routeIntent');
  assert.ok(guard < post, 'the intent guard must run before the request is made');

  const branch = submit.slice(guard, post);
  assert.match(branch, /focus_approval/);
  assert.match(branch, /openApproval\(subject\)/);
  assert.match(branch, /return;/, 'the approval branch must return, not fall through to the API');
});

test('a role that cannot approve is told why instead of being sent to the card', () => {
  const submit = fn('askSubmit');
  assert.match(submit, /approvalBlockReason\(session\.role\)/);
  /* Refused, and rendered as a refusal rather than as an answer. */
  assert.match(submit, /askSay\('assistant', blocked, \[\], true\)/);
});

test('nothing in the Ask surface issues a write of any kind', () => {
  /* `decide` is the only function allowed to approve or decline, and it is
   * reached from the incident card's own button after an explicit
   * acknowledgement of the blast radius.
   *
   * The assertion is about requests, not vocabulary: openApproval legitimately
   * mentions the string 'approve' because it scrolls to the element with that
   * id. What it must never do is send anything. */
  const WRITE_METHODS = /method:\s*'(POST|PUT|DELETE|PATCH)'/;
  const APPROVAL_ROUTE = /\/(approve|decline)\b/;

  for (const name of ['openApproval', 'renderAsk', 'askSay', 'askTurnHtml', 'askProse']) {
    const body = fn(name);
    assert.doesNotMatch(body, WRITE_METHODS, `${name} must not issue a write`);
    assert.doesNotMatch(body, APPROVAL_ROUTE, `${name} must not name the approval routes`);
  }

  /* askSubmit is the one exception: it POSTs, but only ever to /v1/chat. */
  const submit = fn('askSubmit');
  assert.doesNotMatch(submit, APPROVAL_ROUTE, 'askSubmit must not name the approval routes');
  const posts = [...submit.matchAll(/api\('([^']+)'/g)].map((m) => m[1]);
  assert.deepEqual(posts, ['/v1/chat'], 'askSubmit may only call the chat route');
});

test('openApproval only reads, and resolves a runbook id through the read route', () => {
  const body = fn('openApproval');
  assert.match(body, /api\('\/v1\/runbooks\/' \+ encodeURIComponent\(subject\)\)/);
  assert.ok(!body.includes('method:'), 'openApproval must not issue a write of any kind');
});

test('model prose is filtered before it reaches the DOM', () => {
  /* The reply is model-authored text arriving over the network. richText escapes
   * first and then re-permits only a small inline allowlist. */
  const prose = fn('askProse');
  assert.match(prose, /richText\(para\)/);
  assert.ok(!/\+\s*para\s*\+/.test(prose), 'raw paragraph text must not be concatenated in');
});

test('the question the user typed is escaped, not richText-ed', () => {
  /* Their own input needs no inline emphasis, so it gets the stricter treatment. */
  assert.match(fn('askTurnHtml'), /escapeHtml\(entry\.text\)/);
});

test('tool names from the response are escaped', () => {
  /* They come back over the network, so they are untrusted like any other field. */
  assert.match(fn('askTurnHtml'), /escapeHtml\(name\)/);
});

test('a new turn is appended, and the transcript is not its own live region', () => {
  /* #transcript sits inside #viewHost, which is already aria-live="polite".
   * Repainting the whole transcript there re-announces every earlier turn. */
  const markup = app.match(/<div class="transcript" id="transcript"[^>]*>/);
  assert.ok(markup, 'the transcript container is missing');
  assert.doesNotMatch(markup[0], /aria-live/);
  assert.match(app, /id="askStatus" aria-live="polite"/);

  const say = fn('askSay');
  assert.match(say, /host\.appendChild\(node\)/);
  assert.ok(!say.includes('askLog.map'), 'askSay must append one turn, not rebuild the transcript');
});

test('the reply, not the chrome, is what gets announced', () => {
  assert.match(fn('askSay'), /\$\('askStatus'\)[\s\S]*status\.textContent = text/);
});

test('the composer is disabled while a request is in flight', () => {
  /* Otherwise Enter twice sends two questions and the second reply overwrites
   * the first turn's ordering. */
  const busy = fn('askBusyState');
  assert.match(busy, /button\.disabled = on/);
  assert.match(busy, /box\.disabled = on/);
  assert.match(fn('askSubmit'), /if \(!question \|\| askBusy\) return;/);
});

test('the history sent to the model is capped by the server, and replayed as prose', () => {
  /* The client sends what it has; fn-chat caps it at MAX_HISTORY_TURNS and drops
   * any role that is not user or assistant. */
  const submit = fn('askSubmit');
  assert.match(submit, /history: askTurns\.slice\(\)/);
  assert.match(submit, /askTurns\.push\(\{ role: 'user', content: question \}\)/);
  assert.match(submit, /askTurns\.push\(\{ role: 'assistant', content: reply \}\)/);
});

test('the empty state and the hint both say the assistant cannot act', () => {
  /* The constraint is a feature and should be visible, so nobody waits for an
   * approval that was never going to happen. */
  const render = fn('renderAsk');
  assert.match(render, /cannot approve, decline or run a runbook/);
});

test('a failed request becomes a refusal bubble rather than a silent no-op', () => {
  const submit = fn('askSubmit');
  assert.match(submit, /\.catch\(function \(err\)/);
  assert.match(submit, /askBusyState\(false\)/);
});

test('the view is gated on a capability that exists', async () => {
  const { CAPABILITIES } = await import('../lib/triage.mjs');
  assert.ok(CAPABILITIES.view_incidents, 'view_incidents must exist in the matrix');
});

test('no inline event handler was introduced by the Ask view', () => {
  /* An inline handler is the one thing that breaks under a script-src CSP. */
  const render = fn('renderAsk');
  assert.doesNotMatch(render, /\son[a-z]+=/, 'wire listeners with addEventListener');
  assert.match(render, /addEventListener\('submit'/);
});
