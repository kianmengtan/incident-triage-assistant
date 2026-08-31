"""fn-generate-rca

Step Functions task. Invokes Bedrock claude-haiku-4-5 with the alert,
correlated context, and RAG results to produce root cause analysis and
prioritized remediation steps, then persists the result to the
Diagnostics table.
"""
import time
import uuid

from common import bedrock, config, crypto, tenant_scope

SYSTEM_PROMPT = (
    "You are an SRE incident-triage assistant. Given an alert, correlated log/"
    "config context, and similar past incidents, produce a concise root cause "
    "analysis and a prioritized, actionable list of remediation steps. Respond "
    "as JSON with keys 'rca_summary' (string) and 'remediation_steps' "
    "(array of strings)."
)


def _build_user_prompt(event):
    alert = event["alert"]
    return (
        f"Alert: {alert}\n"
        f"Log context: {event.get('logs_context')}\n"
        f"Config context: {event.get('config_context')}\n"
        f"RAG context text: {event.get('rag_context', {}).get('context_text')}\n"
        f"Similar past incidents: {event.get('rag_context', {}).get('similar_incidents')}\n"
    )


def handler(event, context):
    tenant_id = event["tenant_id"]
    alert_id = event["alert_id"]

    raw = bedrock.generate_text(SYSTEM_PROMPT, _build_user_prompt(event))
    try:
        import json

        parsed = json.loads(raw)
        rca_summary = parsed["rca_summary"]
        remediation_steps = parsed["remediation_steps"]
    except Exception:  # noqa: BLE001 - fall back to raw text if model didn't return JSON
        rca_summary = raw
        remediation_steps = []

    diagnostic_id = str(uuid.uuid4())
    generated_at = int(time.time())

    table = tenant_scope.tenant_dynamodb_resource(tenant_id).Table(config.DIAGNOSTICS_TABLE)
    table.put_item(
        Item={
            "tenant_id": tenant_id,
            "sk": f"diag#{alert_id}",
            "diagnostic_id": diagnostic_id,
            "alert_id": alert_id,
            "rca_summary": crypto.encrypt_field(tenant_id, rca_summary),
            "remediation_steps": crypto.encrypt_field(
                tenant_id, "\n".join(remediation_steps)
            ),
            "rag_context_refs": event.get("rag_context", {}).get("similar_incidents", []),
            "model_version": config.HAIKU_MODEL_ID,
            "generated_at": generated_at,
        }
    )

    return {
        "diagnostic_id": diagnostic_id,
        "rca_summary": rca_summary,
        "remediation_steps": remediation_steps,
        "generated_at": generated_at,
    }
