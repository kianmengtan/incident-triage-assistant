"""fn-generate-runbook

Step Functions task. Renders a standardized runbook document from the
diagnostic result via Bedrock, stores it in the runbooks S3 bucket, and writes
metadata to the Runbooks table with approval_status=pending.

The runbook id is derived from the diagnostic rather than random, so a Step
Functions retry of this task overwrites its own previous attempt instead of
leaving an orphaned S3 object and a second Runbooks row behind.

One deliberate asymmetry: the RCA text is encrypted with the tenant's DEK in the
Diagnostics table, and the runbook document -- which restates it -- is written
here under bucket default encryption (SSE-S3) only. That is a trade-off, not an
oversight. A runbook is 1-3 MB and is delivered to the browser as a presigned
GET, and an object encrypted with an application-layer key cannot be, so
encrypting it would mean routing every download through the API and giving up the
presigned URL. The isolation boundary for these objects is therefore the
tenant-prefixed IAM condition on TenantScopedRole (tenant/${tenant_id}/*) plus a
bucket with public access fully blocked and no CloudFront origin in front of it.
"""
import json
import logging
import time
import uuid

from common import bedrock, config, progress, tenant_scope

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SYSTEM_PROMPT = (
    "You are an SRE assistant. Render a standardized, human-readable incident "
    "runbook in Markdown given a root-cause analysis and remediation steps. "
    "Include sections: Summary, Root Cause, Remediation Steps (numbered, each "
    "with its priority and its script/API reference where one is given), and "
    "Rollback Plan. Do not invent steps that are not in the input."
)

# Namespace for deriving a stable runbook id from a diagnostic id.
_RUNBOOK_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


def _runbook_id(diagnostic_id):
    return str(uuid.uuid5(_RUNBOOK_NAMESPACE, str(diagnostic_id)))


def handler(event, context):
    tenant_id = event["tenant_id"]
    alert_id = event["alert_id"]
    diagnostic = event["diagnostic"]
    progress.mark_stage(tenant_id, alert_id, progress.RUNBOOK)

    user_prompt = json.dumps(
        {
            "service": event["alert"].get("service"),
            "severity": event["alert"].get("severity"),
            "rca_summary": diagnostic["rca_summary"],
            "confidence": diagnostic.get("confidence"),
            "remediation_steps": diagnostic["remediation_steps"],
        },
        default=str,
        indent=2,
    )
    response = bedrock.generate_text(SYSTEM_PROMPT, user_prompt, max_tokens=3072)
    if response.stop_reason == "max_tokens":
        # A runbook cut off mid-step is worse than no runbook: the missing steps
        # are invisible, and someone would execute the ones that made it.
        raise bedrock.TruncatedResponse(
            f"runbook for {alert_id} was truncated at max_tokens; refusing to store a partial runbook"
        )

    runbook_id = _runbook_id(diagnostic["diagnostic_id"])
    generated_at = int(time.time())
    s3_key = tenant_scope.tenant_object_key(tenant_id, "runbook", f"{runbook_id}.md")

    s3 = tenant_scope.tenant_s3_client(tenant_id)
    s3.put_object(
        Bucket=config.RUNBOOKS_BUCKET,
        Key=s3_key,
        Body=response.text.encode("utf-8"),
        ContentType="text/markdown",
    )

    table = tenant_scope.tenant_dynamodb_resource(tenant_id).Table(config.RUNBOOKS_TABLE)
    table.put_item(
        Item={
            "tenant_id": tenant_id,
            "sk": f"runbook#{runbook_id}",
            "runbook_id": runbook_id,
            "alert_id": alert_id,
            "diagnostic_id": diagnostic["diagnostic_id"],
            "s3_key": s3_key,
            "status": "ready",
            "approval_status": "pending",
            "execution_status": "not_started",
            "approved_by": None,
            "generated_at": generated_at,
        }
    )

    return {"runbook_id": runbook_id, "s3_key": s3_key, "generated_at": generated_at}
