"""fn-trigger-remediation

POST /v1/runbooks/{runbookId}/approve

Enforces that only a caller in the TenantAdmin group may approve and
execute a runbook's remediation via the tenant's Remediation Execution
Platform. Every outcome — success or failure — is written to the
AuditTrail, and the tenant's Incident Management System is notified
asynchronously afterward.
"""
import json
import logging

import boto3
from botocore.exceptions import ClientError

from common import audit, config, tenant_scope
from common.response import api_response

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_secrets = boto3.client("secretsmanager", region_name=config.REGION)
_lambda = boto3.client("lambda", region_name=config.REGION)


def _authorizer_ctx(event):
    return event.get("requestContext", {}).get("authorizer") or {}


def _remediation_creds(tenant_id):
    try:
        secret = _secrets.get_secret_value(
            SecretId=f"{config.PREFIX}-tenant-{tenant_id}-integration-creds"
        )
        return json.loads(secret["SecretString"]).get("remediation_platform", {})
    except ClientError:
        return {}


def _call_remediation_platform(creds, runbook):
    import urllib.request

    endpoint = creds.get("endpoint")
    if not endpoint:
        return {"status": "skipped", "reason": "not_configured"}
    body = json.dumps({"runbook_id": runbook["runbook_id"], "s3_key": runbook["s3_key"]}).encode(
        "utf-8"
    )
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Authorization": f"Bearer {creds.get('api_key', '')}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            return {"status": "success", "response": resp.read().decode("utf-8")}
    except Exception as exc:  # noqa: BLE001 - captured as a failed execution result
        return {"status": "failed", "error": str(exc)}


def handler(event, context):
    ctx = _authorizer_ctx(event)
    tenant_id = ctx.get("tenant_id")
    group = ctx.get("group")
    actor = event.get("requestContext", {}).get("authorizer", {}).get("principalId", "unknown")
    runbook_id = (event.get("pathParameters") or {}).get("runbookId")

    if not tenant_id:
        return api_response(403, {"message": "forbidden"})

    if group != "TenantAdmin":
        return api_response(403, {"message": "only TenantAdmin may approve remediation"})

    table = tenant_scope.tenant_dynamodb_resource(tenant_id).Table(config.RUNBOOKS_TABLE)
    resp = table.get_item(Key={"tenant_id": tenant_id, "sk": f"runbook#{runbook_id}"})
    runbook = resp.get("Item")
    if not runbook:
        return api_response(404, {"message": "not found"})

    creds = _remediation_creds(tenant_id)
    result = _call_remediation_platform(creds, runbook)

    table.update_item(
        Key={"tenant_id": tenant_id, "sk": f"runbook#{runbook_id}"},
        UpdateExpression="SET approval_status = :s, approved_by = :a",
        ExpressionAttributeValues={":s": result["status"], ":a": actor},
    )

    audit.record_audit(
        tenant_id=tenant_id,
        actor=actor,
        action="remediation.approve",
        result=result["status"],
        alert_id=runbook.get("alert_id"),
        runbook_id=runbook_id,
    )

    _lambda.invoke(
        FunctionName=config.NOTIFY_IMS_FUNCTION_NAME,
        InvocationType="Event",
        Payload=json.dumps(
            {
                "tenant_id": tenant_id,
                "alert_id": runbook.get("alert_id"),
                "runbook_id": runbook_id,
                "runbook_link": runbook.get("s3_key"),
            }
        ).encode("utf-8"),
    )

    return api_response(200, {"runbook_id": runbook_id, "execution_result": result})
