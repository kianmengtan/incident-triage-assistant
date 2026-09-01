"""fn-generate-rca

Step Functions task. Invokes Bedrock claude-haiku-4-5 with the alert,
correlated context, and RAG results to produce root cause analysis and
prioritized remediation steps, then persists the result to the Diagnostics
table.

The model is asked for structured JSON and held to it (see
common.bedrock.generate_json). The previous version caught every exception
around parsing and fell back to putting the raw model output in the RCA
summary, which meant a truncated or chatty response surfaced to an on-call
engineer as a wall of half-finished JSON. It also lost the per-step priority
the specification and the UI both rely on, by joining steps into one string.
"""
import json
import logging
import time
import uuid

from common import bedrock, config, crypto, progress, tenant_scope

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SYSTEM_PROMPT = (
    "You are an SRE incident-triage assistant. Given an alert, correlated log/"
    "config context, and similar past incidents, produce a concise root cause "
    "analysis and a prioritized, actionable list of remediation steps.\n\n"
    "Respond with a single JSON object and nothing else, with keys:\n"
    '  "rca_summary": string — one or two sentences naming the most probable cause\n'
    '  "confidence": one of "high", "probable", "low"\n'
    '  "remediation_steps": array of objects, each with:\n'
    '      "text": string — the action, imperative mood\n'
    '      "priority": one of "P1", "P2", "P3"\n'
    '      "command": string or null — a shell command or API call, if one applies\n'
    '      "reversible": boolean — whether the step can be undone\n'
    "Order remediation_steps most urgent first. If the evidence does not support "
    'a confident cause, say so in rca_summary and set confidence to "low".'
)

VALID_PRIORITIES = ("P1", "P2", "P3")
VALID_CONFIDENCE = ("high", "probable", "low")


def _build_user_prompt(event):
    """Serialize the context as JSON rather than Python reprs.

    f-string interpolation of dicts produced single-quoted Python literals with
    None and True in them, which is not a format the model has seen much of.
    """
    return json.dumps(
        {
            "alert": event["alert"],
            "log_context": event.get("logs_context"),
            "config_context": event.get("config_context"),
            "retrieved_similar_incidents": event.get("rag_context", {}).get(
                "similar_incidents"
            ),
        },
        default=str,
        indent=2,
    )


def _normalize_steps(raw_steps):
    """Coerce the model's steps into the shape the rest of the system expects.

    Accepts a list of objects or a list of bare strings; anything else yields no
    steps rather than an exception. This used to be ``"\\n".join(steps)``, which
    raised TypeError the moment the model returned objects — outside the try, so
    it failed the task and burned three Step Functions retries.
    """
    if not isinstance(raw_steps, list):
        return []
    steps = []
    for raw in raw_steps:
        if isinstance(raw, str):
            steps.append({"text": raw, "priority": "P2", "command": None, "reversible": None})
            continue
        if not isinstance(raw, dict):
            continue
        text = raw.get("text") or raw.get("step") or raw.get("action")
        if not text:
            continue
        priority = str(raw.get("priority", "")).upper()
        steps.append(
            {
                "text": str(text),
                "priority": priority if priority in VALID_PRIORITIES else "P2",
                "command": raw.get("command") or raw.get("cmd") or None,
                "reversible": raw.get("reversible"),
            }
        )
    return steps


def handler(event, context):
    tenant_id = event["tenant_id"]
    alert_id = event["alert_id"]
    progress.mark_stage(tenant_id, alert_id, progress.RCA)

    parsed = bedrock.generate_json(SYSTEM_PROMPT, _build_user_prompt(event))

    rca_summary = str(parsed.get("rca_summary") or "").strip()
    if not rca_summary:
        raise bedrock.InvalidModelJson("model returned no rca_summary")
    confidence = str(parsed.get("confidence", "")).lower()
    if confidence not in VALID_CONFIDENCE:
        confidence = "probable"
    remediation_steps = _normalize_steps(parsed.get("remediation_steps"))
    if not remediation_steps:
        logger.warning("model returned no usable remediation steps for %s", alert_id)

    diagnostic_id = str(uuid.uuid4())
    generated_at = int(time.time())

    # Summary, steps AND retrieved-incident metadata are all encrypted with the
    # tenant's DEK. The refs used to be stored in plaintext beside an encrypted
    # summary, and they carry other incidents' service and description text — so
    # encrypting only the summary protected very little.
    table = tenant_scope.tenant_dynamodb_resource(tenant_id).Table(config.DIAGNOSTICS_TABLE)
    table.put_item(
        Item={
            "tenant_id": tenant_id,
            "sk": f"diag#{alert_id}",
            "diagnostic_id": diagnostic_id,
            "alert_id": alert_id,
            "rca_summary": crypto.encrypt_field(tenant_id, rca_summary),
            "remediation_steps": crypto.encrypt_field(
                tenant_id, json.dumps(remediation_steps)
            ),
            "rag_context_refs": crypto.encrypt_field(
                tenant_id,
                json.dumps(
                    event.get("rag_context", {}).get("similar_incidents", []), default=str
                ),
            ),
            "confidence": confidence,
            "model_version": config.HAIKU_MODEL_ID,
            "generated_at": generated_at,
        }
    )

    return {
        "diagnostic_id": diagnostic_id,
        "rca_summary": rca_summary,
        "remediation_steps": remediation_steps,
        "confidence": confidence,
        "generated_at": generated_at,
    }
