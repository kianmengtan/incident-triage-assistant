# Front-end design: chat-first incident console

Design specification for the UI half of **FR-10**. The prototype that implements it
is `../frontend/prototype.html`; this document is what a production build should be
written against.

`reference.html` in this folder is authoritative for look and feel. Every token,
type pairing and component shape below is lifted from it.

---

## 1. Why a chatbot suits this product

The platform's output is already narrative — a root cause explanation, prioritised
remediation steps, a runbook — and its five endpoints map cleanly onto things a
person would ask out loud ("why did this happen", "show me the runbook", "who
approved it"). A conversation is a good fit.

But a chatbot alone would be a bad incident console, for three reasons that shape
everything below:

1. **A blank text box is useless to someone who does not know the alert id.** Hence
   a persistent incident rail.
2. **A 1–3 MB runbook cannot live in a transcript.** Hence a detail drawer.
3. **A five-minute wait cannot be a spinner.** Hence the pipeline card, which is the
   most important component in the design.

So: chat-primary, with an incident rail and an on-demand drawer.

## 2. The seven decisions that make it usable

| # | Decision | Why |
|---|---|---|
| 1 | The pipeline renders as a live stage timeline with an elapsed clock and an SLA budget bar | NFR-01 allows five minutes. Visible progress against a known budget turns dead waiting into information: which stage, how long, how much budget is left |
| 2 | An incident rail plus contextual suggested prompts | Nobody has to know an `alert_id`, and nobody faces an empty box |
| 3 | Every AI claim carries an evidence chip that expands | The RCA is model-generated. Operators will not act on it — correctly — unless they can see the log line, the commit diff and the similar past incidents behind it |
| 4 | One-sentence root cause first, reasoning below, detail on demand | The first thing a responder needs at 3am is the answer, not the analysis |
| 5 | Approval is a card with a review checkbox, pre-emptively disabled for non-admins | C-03 and Requirement 7. A non-admin should see *why* they cannot approve, not discover it through a 403 |
| 6 | Degraded evidence is stated, never silently omitted | C-02 makes VCS data optional. A diagnosis missing change history must say so, or it implies confidence it has not earned |
| 7 | Keyboard-first | The people using this are fast, tired, and already in a terminal |

## 3. Layout

```
┌────────────────────────────────────────────────────────────────────────┐
│ Triage  ·  tenant acme-retail          SLA ● healthy   [role ▾]        │
├──────────────┬─────────────────────────────────────────┬───────────────┤
│ INCIDENTS ⌘K │            conversation                 │  drawer 420px │
│              │                                         │  (on demand)  │
│ NEEDS ATTN   │  ┌─ pipeline ─────────────────────┐     │  Runbook      │
│ ◆ SEV1  2m   │  │ ✓ ingest   ✓ logs   ⟳ config   │     │  Evidence     │
│   checkout   │  │ ▓▓▓▓▓▓▓░░░░░░  2:14 / 5:00     │     │  Audit        │
│ ▲ SEV2  14m  │  └────────────────────────────────┘     │               │
│   auth-api   │  ┌─ diagnosis ────────────────────┐     │               │
│              │  │ cause · evidence · steps       │     │               │
│ RECENT       │  └────────────────────────────────┘     │               │
│ ● SEV3  1h   │  [ why? ] [ runbook ] [ what changed? ] │               │
│   cdn-edge   │  ┌───────────────────────────────────┐  │               │
│              │  │ Ask about this incident…      ↵   │  │               │
└──────────────┴──┴───────────────────────────────────┴──┴───────────────┘
```

- Shell is `grid-template-columns: 300px minmax(0, 1fr)`, with the drawer as a third
  420px column at ≥1440px and a fixed overlay below that.
- Prose keeps the reference's 65–75ch measure. Cards run wider (max 860px) because
  they are scanned, not read.
- Below 900px the rail becomes a sheet behind an "Incidents" button and the drawer
  goes full width. Verified free of horizontal page scroll at 320, 768 and 1440px.

## 4. Component inventory

| Component | Content | States |
|---|---|---|
| `msg-user` | What the operator typed | — |
| `msg-assistant` | Prose on the page background, no bubble — it should read like paper, per the reference's intent | streaming, instant (reduced motion) |
| `card-pipeline` | Ingest → (logs ∥ config) → RAG → RCA → Runbook. Per-stage glyph, label, detail, duration. Elapsed clock. SLA budget bar | stage: pending / running / done / skipped / failed · card: running / complete / complete-breached / failed · retry badge on Step Functions backoff (Req 2.3) |
| `card-diagnosis` | Severity badge, service, alert id, one-sentence cause in serif, reasoning, evidence chips, prioritised P1/P2/P3 steps with copyable commands, model + confidence + timestamp footer | full confidence · degraded (partial-evidence banner, confidence downgraded) |
| `card-runbook` | Title, runbook id, approval badge, first three steps then "show all N", open in drawer, export JSON, download | pending / approved / executed / failed |
| `card-approval` | Warning, "what will run" table (action, target, blast radius), review checkbox, Approve / Decline | admin · non-admin (disabled + reason) · post-decline · 403 fallback |
| `card-audit-receipt` | Actor, time, action, target, result badge, IMS outcome | success · failed · IMS-notify failed (Req 8.2) |
| `tool-chip` | "Retrieving runbook RB-2291…" | pending / resolved |
| Drawer `Runbook` | Full runbook: summary, all steps with commands, rollback note | populated · none yet |
| Drawer `Evidence` | Redacted log excerpt with matched lines highlighted, commit diff, similar incidents with similarity scores | populated · VCS unavailable · no similar incidents |
| Drawer `Audit` | Append-only records with actor, action, target, result | populated · empty |
| Empty / error | Empty rail, empty conversation, API unreachable, tenant-mismatch denial (Req 6.3) | — |

### Status colour mapping

No colour is invented; every state reuses a badge tone from `reference.html`, and
each pairs a **glyph and a word** with the colour so severity is never colour-only.

| State | Tone | Glyph |
|---|---|---|
| SEV1 critical | `badge-danger` | ◆ |
| SEV2 high | `badge-warning` | ▲ |
| SEV3 moderate | `badge-neutral` | ● |
| SEV4 low | `badge-neutral` | ○ |
| Awaiting approval | `badge-warning` | ○ |
| Executed | `badge-success` | ✓ |
| Execution failed | `badge-danger` | ✕ |
| Declined | `badge-neutral` | — |

## 5. The approval gate

The one surface where a UI mistake causes real damage. Requirements 7.1–7.3, C-03.

- States in an `alert-warning` that this executes real changes on live infrastructure.
- Lists the exact normalised actions, their target system and their blast radius —
  including when an action is irreversible — **before** any button.
- Requires "I have reviewed all N actions above and accept the blast radius" before
  the primary button enables.
- For `TenantEngineer` / `TenantLeadership`, Approve is disabled with an explanation
  naming who *can* approve. The 403 response stays implemented as a fallback for the
  case where a token's group changes between page load and click.
- **Typed text never approves anything.** `approve it`, `run it`, `ship it`, `go
  ahead`, `execute the runbook` and their variants all resolve to `focus_approval`,
  which scrolls to the card and focuses the checkbox. The assistant says plainly that
  it will not approve on the user's behalf.

## 6. Mapping to the API

| User intent | Endpoint |
|---|---|
| Rail listing, "what is broken right now" | Alerts query (via chat tool) |
| "why did this happen", diagnosis card | `GET /v1/diagnostics/{alertId}` |
| "show me the runbook" | `GET /v1/runbooks/{runbookId}`, `GET /v1/runbooks` |
| Approve button (never typed text) | `POST /v1/runbooks/{runbookId}/approve` |
| "export it as JSON" | `GET /v1/runbooks/{runbookId}/export` |
| Pipeline card | Step Functions execution state |
| Drawer Audit tab | AuditTrail query |

### The endpoint the specification is missing

Natural language needs somewhere to go, and none of the five endpoints accepts a
sentence. This design assumes one addition:

```
POST /v1/chat        Cognito JWT, tenant-scoped
  → fn-chat, Bedrock global.anthropic.claude-haiku-4-5-20251001-v1:0
      tools:
        list_incidents({ status?, severity?, since? })
        get_diagnostics({ alert_id })
        list_runbooks({ status? })
        get_runbook({ runbook_id })
        get_pipeline_status({ alert_id })
        get_audit_trail({ runbook_id? })
  → { text, cards[] }
```

Each tool is a thin wrapper over a Lambda that already exists in
`../spec/tasks.md`, so the tenant scoping and IAM conditions are inherited rather
than re-implemented. The model id and the mandatory `global.` inference-profile
prefix come from `../CLAUDE.md`.

**`approve_runbook` is deliberately absent from the tool list.** The model can only
render an approval card; the human's click calls the approve endpoint directly. This
matters more than it first appears: the RCA prompt is fed untrusted text from
customer log lines and commit messages, so a tool that could approve remediation
would be reachable by prompt injection in a log line. Leaving it out makes C-03's
human-approval requirement structural rather than merely a policy the model is asked
to follow.

Adding this needs a task in `../spec/tasks.md` plus an IAM role scoped to
`bedrock:InvokeModel` on the approved model and `lambda:InvokeFunction` on the named
query functions. This document does not edit the generated spec.

## 7. Accessibility

- Transcript is `role="log" aria-live="polite" aria-relevant="additions"`. Pipeline
  stage changes go to a separate `role="status"` region and only **completions** are
  announced, so a five-minute pipeline does not spam a screen reader.
- The SLA bar is a real `role="progressbar"` with `aria-valuenow` and a label naming
  what is being measured.
- Every stage carries an `sr-only` status word, so "skipped" and "failed" do not
  depend on the glyph or its colour.
- Focus rings are `2.5px solid var(--color-secondary)` at `2px` offset — the green,
  never the terracotta accent, exactly as the reference requires, so a focus ring
  never reads as an error.
- The drawer moves focus to its close button on open and returns it to the element
  that opened it on close. `Esc` closes it.
- `prefers-reduced-motion: reduce` drops streaming, spinners and slide transitions;
  all content still arrives, instantly.
- Contrast: every pair in use clears WCAG AA (4.5:1) — see the note below.

### One deviation from the reference, and why

`--color-ink-faint` (`#877868`) is designated in `reference.html` for "placeholders,
meta". Against the page background it measures **4.00:1**, which is below AA for
text at 14px. The palette is kept exactly as given — no shade is invented — but
`ink-faint` is used only for genuinely supplementary text (the input placeholder,
the idle stage glyph). Anything carrying information — metadata lines, timestamps,
durations, section labels, similarity scores — uses `--color-ink-muted` (7.42:1).

Worth raising with the design owner: if `ink-faint` is meant for real metadata, it
needs to be about 15% darker to clear AA at 14px.

### Density

The reference is tuned for "warm, roomy, considered"; an incident console needs more
per screen. Every token and the serif/sans pairing are kept as given, and the same
spacing scale is simply used at its lower end (`--space-3`/`--space-4`) inside the
rail and cards, while `--space-5`+ carries the rhythm between conversation turns.

## 8. Open questions for the owner

1. **Dark mode.** Ops teams work at 3am and will ask for it. The reference supplies
   no dark palette and forbids inventing shades, so the prototype is light only. A
   dark palette from the design owner would unblock it.
2. **Streaming transport.** API Gateway REST cannot stream a response. Either a
   Lambda Function URL with response streaming, or short-polling, or accept a
   non-streamed reply. The prototype streams locally to show the intent.
3. **Runbook size.** The BRD budgets 1–3 MB per runbook. The drawer renders the full
   document; above a few hundred KB it should paginate or lean on the presigned S3
   URL instead.
4. **Multi-incident view.** Leadership users (`TenantLeadership`) are a listed user
   type but the BRD puts trend analytics out of scope. Today they get the same
   read-only single-incident view as engineers, which may be the wrong shape for
   them.
5. **Rail freshness.** The prototype's rail is static. In production it needs either
   polling or a push channel to surface a new SEV1 that arrives while someone is
   reading a different incident.
