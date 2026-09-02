"""fn-chat

POST /v1/chat

The conversational surface over an incident. It answers questions by calling
read-only tools and summarising what they return; it cannot change anything.

Why the tool list is read-only, explicitly
------------------------------------------
``trigger_remediation`` is the only thing in this system that alters a
customer's live infrastructure. Its safety properties -- a conditional write
claiming the runbook from ``approval_status = pending`` before the external
call, an ``attempted`` audit record on both sides of it -- guarantee that an
approval executes *exactly once*. They do not, and cannot, guarantee that it
executes only when a human meant it to: a model-issued approval is an
authorised, audited, exactly-once execution of the wrong thing, and every
control below that point behaves correctly while it happens.

So approval is not a tool the model is given and then told not to use. It is
absent from ``TOOL_SPECS`` (``tests/test_chat.py`` fails if anything mutating
appears there), and an approval phrase is intercepted *before* the model is
invoked and answered deterministically -- the client is told to focus the
approval card, which is exactly what ``routeIntent`` already does in the front
end. ``tests/test_chat_parity.py`` pins the two phrase lists together.

Why it delegates to query_diagnostics
-------------------------------------
Every tool reads through the readers in that module rather than issuing its own
queries. They already take their tenant id as their first argument and go
through ``common.tenant_scope``'s STS-tagged session, so reuse inherits the
tenant isolation instead of reimplementing it. A second copy of a tenant-scoped
read is a second chance to forget the scoping, and here the arguments are chosen
by a model.

What is deliberately NOT exposed
--------------------------------
Correlated raw application logs. ``docs/brd.md`` flags them as possibly carrying
usernames, session ids and request data; feeding them through a model and into a
transcript widens that exposure well beyond the drawer that renders them
redacted today. The tools return curated projections (see ``_project``) rather
than whole DynamoDB items, so an attribute added to a table later does not
silently start flowing into the model's context.
"""
import json
import logging
import re

import query_diagnostics as reads
from common import bedrock, rbac
from common.response import api_response

logger = logging.getLogger()
logger.setLevel(logging.INFO)

#: Long enough for a pasted stack-trace line, short enough that a single request
#: cannot be used to push a large prompt through the model on the tenant's bill.
MAX_MESSAGE_CHARS = 2000

#: Turns of prior conversation the client may replay. The client is untrusted
#: input like any other, so it does not get to grow the prompt without limit.
MAX_HISTORY_TURNS = 10

MAX_HISTORY_CHARS = 1000

#: Tool turns before the loop gives up. Four is enough for "list, then read the
#: one that matters, then answer" with a turn spare.
MAX_TOOL_ITERATIONS = 4

MAX_REPLY_TOKENS = 1024

#: One page of rows for any tool that lists. The model does not get to ask for
#: more: a larger page is a bigger prompt and a slower answer, not a better one.
TOOL_PAGE_SIZE = 20


# ---------------------------------------------------------------------------
# Approval and decline phrasing, intercepted before the model runs
# ---------------------------------------------------------------------------
#: Mirrors the two alternations in ``routeIntent`` in frontend/lib/triage.mjs.
#: Pinned by tests/test_chat_parity.py -- if the front end learns to recognise a
#: new way of saying "approve" and this does not, that phrasing reaches the
#: model here while the browser routes it to the card, and the two surfaces stop
#: agreeing about what is a command.
APPROVAL_PHRASES = (
    "approve",
    "authorise",
    "authorize",
    "execute",
    "run it",
    "go ahead",
    "do it",
    "ship it",
)

DECLINE_PHRASES = ("decline", "reject", "cancel", "abort", "don't", "stop")


def _phrase_pattern(phrases):
    return re.compile(r"\b(?:" + "|".join(re.escape(p) for p in phrases) + r")\b", re.I)


APPROVAL_RE = _phrase_pattern(APPROVAL_PHRASES)
DECLINE_RE = _phrase_pattern(DECLINE_PHRASES)

RUNBOOK_ID_RE = re.compile(r"\bRB-[A-Za-z0-9_-]+\b", re.I)
ALERT_ID_RE = re.compile(r"\bALT-[A-Za-z0-9_-]+\b", re.I)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
#: name -> {description, input_schema, capability}. Every entry is a read.
#: ``capability`` is resolved through common.rbac, the same matrix the REST
#: handlers and the front end use, so chat cannot become a way around a role.
TOOL_SPECS = {
    "list_incidents": {
        "description": (
            "List this tenant's incidents, newest first. Use this to answer "
            "'what is broken right now' or to find an alert id."
        ),
        "capability": "view_incidents",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": f"How many to return, 1-{TOOL_PAGE_SIZE}.",
                }
            },
        },
    },
    "get_incident_status": {
        "description": (
            "Where one incident is in the diagnosis pipeline, including whether a "
            "runbook is ready and its approval state."
        ),
        "capability": "view_incidents",
        "input_schema": {
            "type": "object",
            "properties": {"alert_id": {"type": "string"}},
            "required": ["alert_id"],
        },
    },
    "get_diagnosis": {
        "description": (
            "The root-cause analysis and proposed remediation steps for one "
            "incident. Use this to answer 'why did this happen'."
        ),
        "capability": "view_diagnosis",
        "input_schema": {
            "type": "object",
            "properties": {"alert_id": {"type": "string"}},
            "required": ["alert_id"],
        },
    },
    "list_runbooks": {
        "description": "List this tenant's runbooks, optionally filtered by status.",
        "capability": "view_runbooks",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "e.g. 'ready'"},
                "limit": {"type": "integer"},
            },
        },
    },
    "get_runbook": {
        "description": (
            "One runbook's metadata and approval state. The remediation steps "
            "themselves come from get_diagnosis."
        ),
        "capability": "view_runbooks",
        "input_schema": {
            "type": "object",
            "properties": {"runbook_id": {"type": "string"}},
            "required": ["runbook_id"],
        },
    },
    "search_audit": {
        "description": (
            "The audit trail: who did what, and when. Optionally filtered to one "
            "incident or runbook."
        ),
        "capability": "view_audit",
        "input_schema": {
            "type": "object",
            "properties": {
                "alert_id": {"type": "string"},
                "runbook_id": {"type": "string"},
                "limit": {"type": "integer"},
            },
        },
    },
}

#: Field projections. An explicit allowlist rather than the whole item, so a new
#: attribute on a table does not start reaching the model on its own.
_ALERT_FIELDS = (
    "alert_id",
    "service",
    "severity",
    "description",
    "received_at",
    "pipeline_stage",
    "source",
)
_DIAGNOSTIC_FIELDS = (
    "alert_id",
    "rca_summary",
    "confidence",
    "remediation_steps",
    "created_at",
)
_RUNBOOK_FIELDS = (
    "runbook_id",
    "alert_id",
    "title",
    "status",
    "approval_status",
    "execution_status",
    "created_at",
    "approved_by",
    "declined_by",
)
_AUDIT_FIELDS = (
    "action",
    "actor",
    "outcome",
    "alert_id",
    "runbook_id",
    "recorded_at",
    "detail",
)


def _project(item, fields):
    if not item:
        return {}
    return {name: item[name] for name in fields if name in item}


def _limit(arguments):
    try:
        return max(1, min(int(arguments.get("limit", TOOL_PAGE_SIZE)), TOOL_PAGE_SIZE))
    except (TypeError, ValueError):
        return TOOL_PAGE_SIZE


def _required(arguments, name):
    """A required argument, or a refusal the model can act on.

    The model picks these, so a missing one is ordinary input handling rather
    than a bug: refusing tells it what to send next, where a KeyError would 500
    the whole request.
    """
    value = arguments.get(name)
    if not value or not isinstance(value, str):
        raise bedrock.ToolRefused(f"{name} is required and must be a string")
    return value


def _make_dispatch(tenant_id, group):
    """The tool executor for one request, closed over the *verified* identity.

    tenant_id comes from the authorizer context and is captured here, so no tool
    argument can influence which tenant is read -- a tenant_id in the model's
    tool input is simply never looked at.
    """

    def dispatch(name, arguments):
        spec = TOOL_SPECS.get(name)
        if not spec:
            # A tool name is model-controlled input; an unrecognised one is
            # refused rather than resolved against anything.
            raise bedrock.ToolRefused(f"{name} is not a tool this assistant has")
        if not rbac.can(group, spec["capability"]):
            raise bedrock.ToolRefused(rbac.denial_message(group, spec["capability"]))

        arguments = arguments if isinstance(arguments, dict) else {}

        if name == "list_incidents":
            alerts = reads._list_alerts(tenant_id, _limit(arguments))
            return {"alerts": [_project(a, _ALERT_FIELDS) for a in alerts]}

        if name == "get_incident_status":
            status = reads._alert_status(tenant_id, _required(arguments, "alert_id"))
            if not status:
                return {"found": False}
            return dict(status, found=True)

        if name == "get_diagnosis":
            diagnostic = reads._get_diagnostic(tenant_id, _required(arguments, "alert_id"))
            if not diagnostic:
                return {"found": False}
            return dict(_project(diagnostic, _DIAGNOSTIC_FIELDS), found=True)

        if name == "list_runbooks":
            status = arguments.get("status")
            runbooks = reads._list_runbooks(
                tenant_id, status if isinstance(status, str) else None, _limit(arguments)
            )
            return {"runbooks": [_project(r, _RUNBOOK_FIELDS) for r in runbooks]}

        if name == "get_runbook":
            runbook = reads._get_runbook(tenant_id, _required(arguments, "runbook_id"))
            if not runbook:
                return {"found": False}
            return dict(_project(runbook, _RUNBOOK_FIELDS), found=True)

        if name == "search_audit":
            entries = reads._list_audit(
                tenant_id,
                _limit(arguments),
                alert_id=arguments.get("alert_id"),
                runbook_id=arguments.get("runbook_id"),
            )
            return {"entries": [_project(e, _AUDIT_FIELDS) for e in entries]}

        # Unreachable: every name in TOOL_SPECS is handled above, and anything
        # else was refused at the top. Kept so a tool added to the spec without a
        # branch fails loudly here instead of returning None.
        raise bedrock.ToolRefused(f"{name} is declared but not implemented")

    return dispatch


def _tools_for(group):
    """Only the tools this caller may actually use.

    Offering one that will certainly be refused spends a turn on a denial the
    model could have avoided, and invites it to explain a restriction rather than
    answer the question.
    """
    return [
        {
            "name": name,
            "description": spec["description"],
            "input_schema": spec["input_schema"],
        }
        for name, spec in TOOL_SPECS.items()
        if rbac.can(group, spec["capability"])
    ]


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------
def _system_prompt(tenant_id, group):
    return (
        "You are the incident triage assistant for a site reliability team. "
        f"You are answering for the organisation '{tenant_id}', and the person "
        f"asking holds the role {group}.\n\n"
        "Answer from the tools. Call a tool rather than guessing, and if the "
        "tools do not have the answer, say so plainly.\n\n"
        "You cannot approve, decline or execute a remediation, and you cannot "
        "change anything at all -- every tool you have is a read. If someone "
        "asks you to approve or run a runbook, tell them that a Tenant Admin has "
        "to do it from the approval card on the incident, and summarise what the "
        "runbook would do so they can decide.\n\n"
        "Be brief. An on-call engineer is reading this mid-incident: lead with "
        "the answer, name the incident and runbook ids you used, and keep it to "
        "a few sentences unless asked for detail."
    )


def _history(raw):
    """The client's replayed turns, capped and filtered.

    Only user and assistant roles survive: a 'system' entry in the history is
    how a client would try to append to the instructions above.
    """
    if not isinstance(raw, list):
        return []
    turns = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        role = entry.get("role")
        if role not in ("user", "assistant"):
            continue
        content = entry.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        turns.append({"role": role, "content": content[:MAX_HISTORY_CHARS]})
    return turns[-MAX_HISTORY_TURNS:]


# ---------------------------------------------------------------------------
# The approval interception
# ---------------------------------------------------------------------------
def _approval_reply(message, group):
    """The answer to "approve this", or None if that is not what was asked.

    Deliberately deterministic and ahead of the model: the safety property is
    that typed text never becomes an action, and a property enforced by a prompt
    is a property that holds most of the time.
    """
    if not (APPROVAL_RE.search(message) or DECLINE_RE.search(message)):
        return None

    if not rbac.can(group, "approve_remediation"):
        # Pointing this person at the card would send them to a control that is
        # disabled for their role, with no explanation of why.
        return {
            "reply": rbac.denial_message(group, "approve_remediation"),
            "focus": None,
            "acted": False,
        }

    runbook = RUNBOOK_ID_RE.search(message)
    alert = ALERT_ID_RE.search(message)
    subject = runbook.group(0).upper() if runbook else (alert.group(0).upper() if alert else None)

    reply = (
        "Deciding on a remediation is a human action, so I will not do it from "
        "chat. "
    )
    if subject:
        reply += f"I have opened the approval card for {subject} — "
    else:
        reply += "Open the incident and use its approval card — "
    reply += (
        "the decision is recorded against your name either way, and the runbook "
        "runs on your live infrastructure. Ask me what the steps are first if you "
        "want them summarised."
    )

    return {"reply": reply, "focus": "approval", "acted": False, "subject": subject}


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------
def _authorizer_ctx(event):
    return event.get("requestContext", {}).get("authorizer") or {}


def handler(event, context):
    resource = event.get("resource", "")
    if resource != "/v1/chat":
        return api_response(404, {"message": "not found"})

    ctx = _authorizer_ctx(event)
    tenant_id = ctx.get("tenant_id")
    group = ctx.get("group")
    if not tenant_id:
        return api_response(403, {"message": "forbidden"})

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return api_response(400, {"message": "body must be JSON"})
    if not isinstance(body, dict):
        return api_response(400, {"message": "body must be a JSON object"})

    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        return api_response(400, {"message": "message is required"})
    if len(message) > MAX_MESSAGE_CHARS:
        return api_response(
            400, {"message": f"message must be {MAX_MESSAGE_CHARS} characters or fewer"}
        )
    message = message.strip()

    # Before the model, not after it.
    intercepted = _approval_reply(message, group)
    if intercepted:
        return api_response(200, dict(intercepted, tools_used=[]))

    if not rbac.can(group, "view_incidents"):
        return api_response(403, {"message": rbac.denial_message(group, "view_incidents")})

    messages = _history(body.get("history")) + [{"role": "user", "content": message}]

    try:
        result = bedrock.run_tool_conversation(
            _system_prompt(tenant_id, group),
            messages,
            _tools_for(group),
            _make_dispatch(tenant_id, group),
            max_tokens=MAX_REPLY_TOKENS,
            max_iterations=MAX_TOOL_ITERATIONS,
        )
    except bedrock.ToolLoopExhausted:
        logger.warning("tool loop exhausted for tenant %s", tenant_id)
        return api_response(
            504,
            {
                "message": (
                    "I could not get to an answer in time. Try asking about one "
                    "incident by id."
                )
            },
        )

    return api_response(
        200,
        {
            "reply": result.text,
            "focus": None,
            "acted": False,
            "tools_used": [call.name for call in result.tool_calls],
            "refused_tools": [call.name for call in result.tool_calls if call.refused],
        },
    )
