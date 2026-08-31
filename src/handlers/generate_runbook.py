"""fn-generate-runbook

Step Functions task. Renders a standardized runbook document from the
diagnostic result via Bedrock, stores it in the runbooks S3 bucket, and
writes metadata to the Runbooks table with approval_status=pending.
"""
import time
import uuid

from common import bedrock, config, tenant_scope

SYSTEM_PROMPT = (
    "You are an SRE assistant. Render a standardized, human-readable incident "
    "runbook in Markdown given a root-cause analysis and remediation steps. "
    "Include sections: Summary, Root Cause, Remediation Steps (numbered, "
    "each with a script/API reference placeholder where applicable), and "
    "Rollback Plan."
)


def handler(event, context):
    tenant_id = event["tenant_id"]
    alert_id = event["alert_id"]
    diagnostic = event["diagnostic"]

    user_prompt = (
        f"RCA summary: {diagnostic['rca_summary']}\n"
        f"Remediation steps: {diagnostic['remediation_steps']}\n"
        f"Service: {event['alert'].get('service')}\n"
    )
    runbook_markdown = bedrock.generate_text(SYSTEM_PROMPT, user_prompt, max_tokens=3072)

    runbook_id = str(uuid.uuid4())
    generated_at = int(time.time())
    s3_key = tenant_scope.tenant_object_key(tenant_id, "runbook", f"{runbook_id}.md")

    s3 = tenant_scope.tenant_s3_client(tenant_id)
    s3.put_object(
        Bucket=config.RUNBOOKS_BUCKET,
        Key=s3_key,
        Body=runbook_markdown.encode("utf-8"),
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
            "approved_by": None,
            "generated_at": generated_at,
        }
    )

    return {"runbook_id": runbook_id, "s3_key": s3_key, "generated_at": generated_at}
