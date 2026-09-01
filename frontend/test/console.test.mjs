/* Logic behind the live console's new surfaces: role gating, sign-up input
 * validation, and the leadership overview.
 *
 * These are the parts that decide what a person can see and what the numbers on
 * the dashboard mean, so they are unit-tested away from the DOM. The capability
 * checks here are the UX mirror of common/rbac.py -- authorisation itself is
 * enforced in the handlers, and tests/test_rbac_parity.py fails if the two copies
 * of the matrix drift apart.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  CAPABILITIES,
  can,
  capabilityDenialReason,
  roleName,
  canApprove,
  emailDomain,
  isPublicEmailDomain,
  signupEmailProblem,
  PASSWORD_MIN_LENGTH,
  passwordChecklist,
  passwordIsAcceptable,
  passwordStrength,
  summarise,
  SLA_BUDGET_MS
} from '../lib/triage.mjs';

const ADMIN = 'TenantAdmin';
const ENGINEER = 'TenantEngineer';
const LEAD = 'TenantLeadership';

/* ---------- capabilities ---------- */

test('an admin holds every capability', () => {
  for (const name of Object.keys(CAPABILITIES)) {
    assert.equal(can(ADMIN, name), true, `${name} should be held by an admin`);
  }
});

test('an engineer can operate but not administer or oversee', () => {
  assert.equal(can(ENGINEER, 'create_incident'), true);
  assert.equal(can(ENGINEER, 'view_incidents'), true);
  assert.equal(can(ENGINEER, 'approve_remediation'), false);
  assert.equal(can(ENGINEER, 'view_overview'), false);
  assert.equal(can(ENGINEER, 'view_audit'), false);
  assert.equal(can(ENGINEER, 'manage_roles'), false);
});

test('leadership oversees but does not operate', () => {
  assert.equal(can(LEAD, 'view_overview'), true);
  assert.equal(can(LEAD, 'view_audit'), true);
  assert.equal(can(LEAD, 'view_team'), true);
  assert.equal(can(LEAD, 'create_incident'), false);
  assert.equal(can(LEAD, 'approve_remediation'), false);
  assert.equal(can(LEAD, 'manage_roles'), false);
});

test('a session with no role can do nothing', () => {
  for (const name of Object.keys(CAPABILITIES)) {
    assert.equal(can('', name), false);
    assert.equal(can(undefined, name), false);
  }
});

test('an unknown capability throws rather than quietly denying', () => {
  /* A typo that merely returned false would disable the control for everyone,
   * including admins, and read as a data problem rather than a misspelling. */
  assert.throws(() => can(ADMIN, 'aprove_remediation'), /unknown capability/);
});

test('canApprove agrees with the matrix', () => {
  /* It delegates, so these cannot drift apart. */
  for (const role of [ADMIN, ENGINEER, LEAD, '']) {
    assert.equal(canApprove(role), can(role, 'approve_remediation'));
  }
});

test('a denial names who can, not just that you cannot', () => {
  const reason = capabilityDenialReason(ENGINEER, 'approve_remediation');
  assert.match(reason, /Tenant Admins/);
  assert.match(reason, /approve remediation/);
});

test('a denial for a roleless session says to sign in again', () => {
  assert.match(capabilityDenialReason('', 'view_overview'), /Sign in again/);
});

test('there is no denial reason when the capability is held', () => {
  assert.equal(capabilityDenialReason(ADMIN, 'approve_remediation'), null);
});

test('a denial listing two holders reads as a sentence', () => {
  assert.match(
    capabilityDenialReason(LEAD, 'create_incident'),
    /Only Tenant Admins or Tenant Engineers may raise an incident\./
  );
});

test('roles have short names for the header', () => {
  assert.equal(roleName(ADMIN), 'Admin');
  assert.equal(roleName(LEAD), 'Leadership');
  assert.equal(roleName(''), 'No role');
});

/* ---------- sign-up input ---------- */

test('the email domain is the part after the last at-sign, lowercased', () => {
  assert.equal(emailDomain('Ada@Acme-Retail.COM'), 'acme-retail.com');
  assert.equal(emailDomain('  ops@sub.example.co.uk '), 'sub.example.co.uk');
});

test('a malformed address has no domain', () => {
  for (const bad of ['', null, undefined, 'no-at-sign', '@nodomain.com', 'trailing@']) {
    assert.equal(emailDomain(bad), null, String(bad));
  }
});

test('consumer providers are recognised', () => {
  for (const email of ['a@gmail.com', 'b@GMAIL.com', 'c@outlook.com', 'd@proton.me']) {
    assert.equal(isPublicEmailDomain(email), true, email);
  }
});

test('a work domain is not a consumer provider', () => {
  assert.equal(isPublicEmailDomain('ada@acme-retail.com'), false);
});

test('a consumer address is refused with the domain named', () => {
  /* Tenancy comes from the domain, so a gmail signup cannot belong to an
   * organisation. Saying which domain was rejected is what makes it fixable. */
  const problem = signupEmailProblem('someone@gmail.com');
  assert.match(problem, /gmail\.com/);
  assert.match(problem, /work email/);
});

test('a work address has no problem', () => {
  assert.equal(signupEmailProblem('ada@acme-retail.com'), null);
});

test('an address with no dot in the domain is refused', () => {
  assert.match(signupEmailProblem('root@localhost'), /valid work email/);
});

/* ---------- password rules ---------- */

test('the checklist mirrors Cognito defaults and nothing more', () => {
  /* CLAUDE.md requires leaving the pool policy at its defaults, so demanding
   * more here would reject passwords the pool itself would accept. */
  assert.deepEqual(
    passwordChecklist('').map((r) => r.id),
    ['length', 'lower', 'upper', 'digit', 'symbol']
  );
});

test('a compliant password satisfies every rule', () => {
  assert.equal(passwordIsAcceptable('Tr1age!pass'), true);
  assert.equal(passwordStrength('Tr1age!pass'), 5);
});

test('each missing class is reported individually', () => {
  const met = (pw, id) => passwordChecklist(pw).find((r) => r.id === id).met;
  assert.equal(met('short1!A', 'length'), true);
  assert.equal(met('Sh1!', 'length'), false);
  assert.equal(met('ALLUPPER1!', 'lower'), false);
  assert.equal(met('alllower1!', 'upper'), false);
  assert.equal(met('NoDigits!!', 'digit'), false);
  assert.equal(met('NoSymbol123', 'symbol'), false);
});

test(`${PASSWORD_MIN_LENGTH} characters is the minimum, not ${PASSWORD_MIN_LENGTH - 1}`, () => {
  assert.equal(passwordChecklist('Aa1!' + 'x'.repeat(4)).find((r) => r.id === 'length').met, true);
  assert.equal(passwordChecklist('Aa1!' + 'x'.repeat(3)).find((r) => r.id === 'length').met, false);
});

test('an empty password is not acceptable and scores zero', () => {
  assert.equal(passwordIsAcceptable(''), false);
  assert.equal(passwordStrength(''), 0);
});

/* ---------- the overview ---------- */

const NOW = 1_700_000_000_000;
const secs = (ms) => Math.floor(ms / 1000);

function incident(overrides = {}) {
  return {
    alert_id: 'ALT-1',
    severity: 'sev1',
    service: 'checkout',
    source: 'pagerduty',
    received_at: secs(NOW - 60_000),
    ...overrides
  };
}

function runbook(overrides = {}) {
  return {
    runbook_id: 'RB-1',
    alert_id: 'ALT-1',
    approval_status: 'pending',
    execution_status: 'not_started',
    generated_at: secs(NOW - 30_000),
    ...overrides
  };
}

test('an empty tenant summarises to zeroes rather than throwing', () => {
  const s = summarise([], [], NOW);
  assert.equal(s.total, 0);
  assert.equal(s.diagnosed, 0);
  assert.equal(s.medianRunbookMs, null);
});

test('severity aliases are bucketed onto the four levels', () => {
  /* The API accepts critical/p1/sev1 and friends, so the dashboard has to fold
   * them together or the same severity appears as several categories. */
  const s = summarise(
    [
      incident({ alert_id: 'a', severity: 'critical' }),
      incident({ alert_id: 'b', severity: 'p1' }),
      incident({ alert_id: 'c', severity: 'SEV1' }),
      incident({ alert_id: 'd', severity: 'warning' }),
      incident({ alert_id: 'e', severity: 'nonsense' })
    ],
    [],
    NOW
  );
  assert.equal(s.bySeverity.SEV1, 3);
  assert.equal(s.bySeverity.SEV3, 1);
  assert.equal(s.bySeverity.UNKNOWN, 1);
});

test('an incident with a runbook counts as diagnosed', () => {
  const s = summarise([incident()], [runbook()], NOW);
  assert.equal(s.diagnosed, 1);
  assert.equal(s.awaitingRunbook, 0);
});

test('an incident with no runbook is awaiting one', () => {
  const s = summarise([incident()], [], NOW);
  assert.equal(s.diagnosed, 0);
  assert.equal(s.awaitingRunbook, 1);
});

test('the SLA is measured from alert arrival to runbook delivery', () => {
  /* NFR-01: a runbook within five minutes. */
  const met = summarise(
    [incident({ received_at: secs(NOW - 120_000) })],
    [runbook({ generated_at: secs(NOW - 30_000) })],
    NOW
  );
  assert.equal(met.slaMet, 1);
  assert.equal(met.slaBreached, 0);

  const missed = summarise(
    [incident({ received_at: secs(NOW - 600_000) })],
    [runbook({ generated_at: secs(NOW - 30_000) })],
    NOW
  );
  assert.equal(missed.slaMet, 0);
  assert.equal(missed.slaBreached, 1);
});

test('a pipeline that never produced a runbook still counts as breached', () => {
  /* Counting only incidents that have a runbook would hide exactly the failure
   * worth seeing: one where the pipeline silently stopped. */
  const s = summarise([incident({ received_at: secs(NOW - SLA_BUDGET_MS - 60_000) })], [], NOW);
  assert.equal(s.slaBreached, 1);
  assert.equal(s.slaPending, 0);
});

test('a young incident with no runbook yet is pending, not breached', () => {
  const s = summarise([incident({ received_at: secs(NOW - 30_000) })], [], NOW);
  assert.equal(s.slaPending, 1);
  assert.equal(s.slaBreached, 0);
});

test('approval and execution states are counted', () => {
  const s = summarise(
    [
      incident({ alert_id: 'a' }),
      incident({ alert_id: 'b' }),
      incident({ alert_id: 'c' }),
      incident({ alert_id: 'd' })
    ],
    [
      runbook({ alert_id: 'a', approval_status: 'pending' }),
      runbook({ alert_id: 'b', approval_status: 'approved', execution_status: 'succeeded' }),
      runbook({ alert_id: 'c', approval_status: 'declined' }),
      runbook({ alert_id: 'd', approval_status: 'approved', execution_status: 'failed' })
    ],
    NOW
  );
  assert.equal(s.awaitingApproval, 1);
  assert.equal(s.approved, 2);
  assert.equal(s.declined, 1);
  assert.equal(s.executed, 1);
  assert.equal(s.executionFailed, 1);
});

test('hand-raised incidents are counted separately from webhook ones', () => {
  const s = summarise(
    [incident({ alert_id: 'a', source: 'console' }), incident({ alert_id: 'b', source: 'pagerduty' })],
    [],
    NOW
  );
  assert.equal(s.raisedByHand, 1);
});

test('the median runbook time ignores incidents that have none', () => {
  const s = summarise(
    [
      incident({ alert_id: 'a', received_at: secs(NOW - 100_000) }),
      incident({ alert_id: 'b', received_at: secs(NOW - 100_000) }),
      incident({ alert_id: 'c' })
    ],
    [
      runbook({ alert_id: 'a', generated_at: secs(NOW - 90_000) }), // 10s
      runbook({ alert_id: 'b', generated_at: secs(NOW - 70_000) }) //  30s
    ],
    NOW
  );
  assert.equal(s.medianRunbookMs, 20_000);
});

test('the oldest undiagnosed incident is reported', () => {
  const s = summarise(
    [
      incident({ alert_id: 'a', received_at: secs(NOW - 60_000) }),
      incident({ alert_id: 'b', received_at: secs(NOW - 600_000) })
    ],
    [],
    NOW
  );
  assert.equal(s.oldestOpenMs, 600_000);
});

test('a diagnosed incident does not count towards the oldest open age', () => {
  const s = summarise(
    [incident({ alert_id: 'a', received_at: secs(NOW - 600_000) })],
    [runbook({ alert_id: 'a' })],
    NOW
  );
  assert.equal(s.oldestOpenMs, 0);
});

test('only the last 24 hours count towards recent volume', () => {
  const s = summarise(
    [
      incident({ alert_id: 'a', received_at: secs(NOW - 60_000) }),
      incident({ alert_id: 'b', received_at: secs(NOW - 40 * 60 * 60 * 1000) })
    ],
    [],
    NOW
  );
  assert.equal(s.last24h, 1);
  assert.equal(s.total, 2);
});

test('a runbook for an alert that is not in the list is ignored', () => {
  /* The two endpoints paginate independently, so the sets need not line up. */
  const s = summarise([incident({ alert_id: 'a' })], [runbook({ alert_id: 'zzz' })], NOW);
  assert.equal(s.diagnosed, 0);
});

test('malformed input does not throw', () => {
  assert.equal(summarise(null, null, NOW).total, 0);
  assert.equal(summarise([{}], [{}], NOW).total, 1);
});
