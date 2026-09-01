# Triage — the incident console

Two pages live here, and they are for different things.

| File | What it is |
|---|---|
| **`app.html`** | The live application. `../deploy.sh` publishes it as `index.html`, so it is what `app_url` serves. Real Cognito sign-up and sign-in, real incidents, role-based access control, and the leadership overview. Needs `config.json` beside it, which the deploy writes. |
| **`prototype.html`** | The offline design reference for the chat-first surface. One self-contained file that opens from disk with no server and no network calls, running on fixture data. Not deployed. |

`lib/triage.mjs` is the source of truth for the logic both share — severity
mapping, SLA arithmetic, the capability matrix, the overview aggregation. It is
inlined into both pages by `node sync-lib.mjs`; `npm run check-sync` fails if a
copy has drifted, and `../deploy.sh` runs that check before deploying.

Together they are the UI half of **FR-10** ("allow administrators to view
diagnostic analysis, recommendations and generated runbooks via UI or API"), which
the specification otherwise covers only as an API.

## The live app

Served from CloudFront at `app_url`. Sign up with a work email — the first person
from an email domain becomes that organisation's admin, everyone after joins as an
engineer, and an admin promotes people to leadership from the Team screen. What
each role can do is documented in the root `../README.md`.

Locally it needs a `config.json` (API URL and user pool ids) next to it, so the
deployed URL is the practical way to see it.

## The design reference

```
open frontend/prototype.html
```

That is the whole instruction. It is one self-contained file — no server, no `npm
install`, no build step, no network calls, no AWS account. It runs entirely on mock
data. Add `?state=empty` to see it with no incidents.

## What to look at

Pick **checkout-api (SEV1)** in the left rail. It is mid-diagnosis, so you get the
part that matters most: a five-minute pipeline rendered as a live stage timeline
with an elapsed clock and a budget bar, replayed at 15× so the whole arc takes
about twenty seconds. Then the diagnosis, the runbook, and the approval gate arrive
in turn.

The other four incidents are already finished and load instantly:

| Incident | Shows |
|---|---|
| **auth-api** SEV2 | A runbook awaiting approval, with an irreversible action in its blast radius |
| **cdn-edge** SEV3 | A degraded diagnosis — VCS correlation was skipped, and the UI says so |
| **payments-api** SEV2 | An already-executed remediation with its audit receipt |
| **search-api** SEV1 | A pipeline that failed and went to the DLQ, so no runbook exists |

Things worth trying in the composer: `why did this happen?`, `what changed before
this?`, `show me the runbook`, `how long until it is done?`, `who approved it?`, and
`approve it` — the last one deliberately refuses to act and only moves you to the
approval card.

Use the **demo role** switcher in the top bar to see the approval gate from a
`TenantEngineer` account: the button is disabled with an explanation rather than
letting you click into a 403.

## Edge cases by URL

Every state has a direct link, so no one has to wait for a scenario to happen:

| URL | State |
|---|---|
| `?state=vcs-degraded` | Config/VCS correlation skipped after a 401 (C-02) |
| `?state=sla-breach` | Pipeline runs past the 5-minute budget; alarm raised (NFR-01) |
| `?state=403` | Approval refused server-side for a non-admin (Requirement 7.2) |
| `?state=ims-failed` | Remediation succeeded, Incident Management System notify failed (Requirement 8.2) |
| `?state=error` | The diagnostics API is unreachable |
| `?state=empty` | No open incidents |
| `?speed=60` | Slow the pipeline replay down (default 15×; higher is faster) |

## Keyboard

`/` focus composer · `⌘K` filter incidents · `J`/`K` move between incidents ·
`Enter` send · `Shift`+`Enter` newline · `Esc` close the detail panel.

## Layout

Files in this folder:

```
prototype.html      the application — self-contained, open it directly
lib/triage.mjs      the pure logic, and the source of truth for it
sync-lib.mjs        inlines lib/triage.mjs into prototype.html
test/               node --test, zero dependencies
```

`prototype.html` has to stay a single file you can open from disk, but the logic
inside it still has to be testable. So `lib/triage.mjs` owns the pure functions —
severity and status mapping, the SLA budget classification, role-based approval
gating, intent routing, log redaction, escaping — and `sync-lib.mjs` copies them
into the marked block in the HTML. A test fails if the two ever drift.

## Tests

```
cd frontend && npm test          # node --test test/*.test.mjs
npm run sync                     # inline lib/triage.mjs into prototype.html
npm run check-sync               # fail if the two have drifted
```

58 tests, no dependencies, Node 22+. `package.json` carries no dependencies —
only scripts — because `prototype.html` has to stay a single file that opens from
disk. They cover the rules that carry real weight:

- Only the exact string `TenantAdmin` passes the approval gate — no lookalike does.
- Every approval phrasing (`approve it`, `run it`, `ship it`, `go ahead`, …) routes
  to `focus_approval` and can never produce an executing intent.
- The SLA tone escalates at 4:00 and only breaches *past* 5:00, and the bar's
  percentage is clamped so it cannot overflow its track.
- A skipped stage counts as resolved, because C-02 makes VCS data genuinely optional.
- Emails, AWS keys, bearer tokens and session ids are redacted out of echoed log
  text, while IPs keep their first two octets because operators need the subnet.
- Model-authored prose (the root cause, the reasoning, each step's text) goes
  through `richText`, which escapes everything and then re-permits only
  `<strong>`, `<em>`, `<code>`, `<b>`, `<i>` and `<br>` with no attributes — so
  the design's inline emphasis survives and `<code onclick=…>` does not.
- Identifiers, service names and model versions go through `escapeHtml`. A static
  test scans `prototype.html` for any of those fields being concatenated into
  HTML unfiltered, so the next person to add a card cannot reintroduce it.
- Deferred work is generation-guarded: switching incidents mid-stream cannot land
  the previous incident's cards in the new transcript.
- The transcript is not itself an `aria-live` region, the rail makes no invalid
  listbox claims, and the page carries no inline event handlers.
- The copy of the logic inlined in `prototype.html` matches `lib/triage.mjs`, and
  the file contains no imports, external scripts or network calls.

## Layout note

`package.json` exists for the scripts only. Adding a dependency to it would break
the property the whole file is built around, so don't.

## What this is and is not

There is no backend behind it. Every incident, diagnosis and runbook on the page
is fixture data, and the top bar says so.

`./deploy.sh` now publishes this file as the deployed console — it is what
`app_url` in `outputs.json` points at — because the alternative was `app_url`
pointing at an API root that answers `403 Missing Authentication Token` to a
browser. Deploying it does not wire it to live data: the page still reads no
config and makes no requests, which is what keeps the self-contained tests
meaningful. `deploy.sh` writes the deployed API and Cognito identifiers to
`config.json` beside it, ready for that next step.

`../design/frontend-design.md` covers what wiring it up requires.
