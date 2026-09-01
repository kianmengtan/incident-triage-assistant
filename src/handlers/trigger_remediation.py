"""fn-trigger-remediation

POST /v1/runbooks/{runbookId}/approve
POST /v1/runbooks/{runbookId}/decline

Only a caller in the TenantAdmin group may approve a runbook, and approving is
the only thing in this system that changes a customer's live infrastructure.
Three properties matter here:

* **Exactly once.** The runbook is claimed with a conditional write that only
  succeeds from ``approval_status = pending``, BEFORE the remediation platform
  is called. Without it a double-clicked button or a client retry executed the
  remediation twice.
* **Recorded even if this function dies.** An ``attempted`` audit record is
  written before the external call and the outcome after, so an execution can
  never happen with no trace of it. Refused attempts are audited too.
* **Approval and execution are different facts.** ``approval_status`` records
  the human decision (pending/approved/declined); ``execution_status`` records
  what the remediation platform did. Collapsing them, as this used to, wrote
  "skipped" into the approval field when a tenant had no platform configured.
"""
import json
import logging
import time

import boto3
from botocore.exceptions import ClientError

from common import audit, config, crypto, http, integrations, rbac, tenant_scope
from common.response import api_response

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_lambda = boto3.client("lambda", region_name=config.REGION)

# Kept as a name for readability; the authority is common.rbac's matrix, which
# every group check in this application resolves through.
APPROVER_GROUP = rbac.TENANT_ADMIN
CAPABILITY = "approve_remediation"

EXECUTION_SUCCEEDED = "succeeded"
EXECUTION_FAILED = "failed"
EXECUTION_SKIPPED = "skipped"


def _authorizer_ctx(event):
    return event.get("requestContext", {}).get("authorizer") or {}


def _call_remediation_platform(creds, runbook):
    endpoint = creds.get("endpoint")
    if not endpoint:
        return {"status": EXECUTION_SKIPPED, "reason": "not_configured"}
    try:
        response = http.request_json(
            endpoint,
            api_key=creds.get("api_key"),
            payload={"runbook_id": runbook["runbook_id"], "s3_key": runbook.get("s3_key")},
            method="POST",
            timeout=15,
        )
    except http.EndpointNotAllowed as exc:
        logger.warning("refusing to call remediation endpoint: %s", exc)
        return {"status": EXECUTION_FAILED, "error": "endpoint not permitted"}
    except Exception as exc:  # noqa: BLE001 - captured as a failed execution result
        logger.warning("remediation platform call failed: %s", type(exc).__name__)
        return {"status": EXECUTION_FAILED, "error": type(exc).__name__}
    return {"status": EXECUTION_SUCCEEDED, "response": response}


def _rca_summary(tenant_id, alert_id):
    """The diagnosis text, for the IMS notification. Best-effort."""
    if not alert_id:
        return None
    try:
        table = tenant_scope.tenant_dynamodb_resource(tenant_id).Table(config.DIAGNOSTICS_TABLE)
        item = table.get_item(Key={"tenant_id": tenant_id, "sk": f"diag#{alert_id}"}).get("Item")
        if not item:
            return None
        return crypto.decrypt_field(tenant_id, item.get("rca_summary"))
    except Exception as exc:  # noqa: BLE001 - the notification is not worth failing over
        logger.warning("could not read RCA for %s: %s", alert_id, type(exc).__name__)
        return None


def _claim_for_approval(table, tenant_id, runbook_id, actor, now):
    """Move the runbook from pending to approved, once.

    Returns False if someone (or a retry) got there first, which is what makes
    duplicate execution impossible rather than merely unlikely.
    """
    try:
        table.update_item(
            Key={"tenant_id": tenant_id, "sk": f"runbook#{runbook_id}"},
            UpdateExpression=(
                "SET approval_status = :approved, approved_by = :actor, "
                "approved_at = :now, execution_status = :in_progress"
            ),
            ConditionExpression="approval_status = :pending",
            ExpressionAttributeValues={
                ":approved": "approved",
                ":pending": "pending",
                ":in_progress": "in_progress",
                ":actor": actor,
                ":now": now,
            },
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def _decline(table, tenant_id, runbook_id, actor, now):
    try:
        table.update_item(
            Key={"tenant_id": tenant_id, "sk": f"runbook#{runbook_id}"},
            UpdateExpression=(
                "SET approval_status = :declined, declined_by = :actor, declined_at = :now"
            ),
            ConditionExpression="approval_status = :pending",
            ExpressionAttributeValues={
                ":declined": "declined",
                ":pending": "pending",
                ":actor": actor,
                ":now": now,
            },
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def _notify_ims(tenant_id, runbook, runbook_id):
    _lambda.invoke(
        FunctionName=config.NOTIFY_IMS_FUNCTION_NAME,
        InvocationType="Event",
        Payload=json.dumps(
            {
                "tenant_id": tenant_id,
                "alert_id": runbook.get("alert_id"),
                "runbook_id": runbook_id,
                "runbook_link": runbook.get("s3_key"),
                # Was never sent, so every IMS notification carried a null
                # summary despite Requirement 8 asking for the findings.
                "rca_summary": _rca_summary(tenant_id, runbook.get("alert_id")),
            },
            default=str,
        ).encode("utf-8"),
    )


def handler(event, context):
    ctx = _authorizer_ctx(event)
    tenant_id = ctx.get("tenant_id")
    group = ctx.get("group")
    actor = ctx.get("principalId", "unknown")
    runbook_id = (event.get("pathParameters") or {}).get("runbookId")
    declining = event.get("resource", "").endswith("/decline")
    action = "remediation.decline" if declining else "remediation.approve"

    if not tenant_id:
        return api_response(403, {"message": "forbidden"})

    if not rbac.can(group, CAPABILITY):
        # Audited: a refused approval attempt on live infrastructure is exactly
        # the kind of thing an audit trail exists to show.
        audit.record_audit(
            tenant_id=tenant_id,
            actor=actor,
            action=action,
            result="refused_not_admin",
            runbook_id=runbook_id,
        )
        return api_response(403, {"message": rbac.denial_message(group, CAPABILITY)})

    table = tenant_scope.tenant_dynamodb_resource(tenant_id).Table(config.RUNBOOKS_TABLE)
    runbook = table.get_item(
        Key={"tenant_id": tenant_id, "sk": f"runbook#{runbook_id}"}
    ).get("Item")
    if not runbook:
        return api_response(404, {"message": "not found"})

    now = int(time.time())

    if declining:
        if not _decline(table, tenant_id, runbook_id, actor, now):
            return api_response(
                409,
                {
                    "message": "runbook is no longer pending",
                    "approval_status": runbook.get("approval_status"),
                },
            )
        audit.record_audit(
            tenant_id=tenant_id,
            actor=actor,
            action=action,
            result="declined",
            alert_id=runbook.get("alert_id"),
            runbook_id=runbook_id,
        )
        return api_response(200, {"runbook_id": runbook_id, "approval_status": "declined"})

    if not _claim_for_approval(table, tenant_id, runbook_id, actor, now):
        return api_response(
            409,
            {
                "message": "runbook is not awaiting approval",
                "approval_status": runbook.get("approval_status"),
            },
        )

    # Written before the call, so an execution is never invisible even if this
    # function is killed mid-flight.
    audit.record_audit(
        tenant_id=tenant_id,
        actor=actor,
        action=action,
        result="attempted",
        alert_id=runbook.get("alert_id"),
        runbook_id=runbook_id,
    )

    result = _call_remediation_platform(
        integrations.creds(tenant_id, integrations.REMEDIATION_PLATFORM), runbook
    )

    table.update_item(
        Key={"tenant_id": tenant_id, "sk": f"runbook#{runbook_id}"},
        UpdateExpression="SET execution_status = :s, executed_at = :now",
        ExpressionAttributeValues={":s": result["status"], ":now": now},
    )

    audit.record_audit(
        tenant_id=tenant_id,
        actor=actor,
        action=action,
        result=result["status"],
        alert_id=runbook.get("alert_id"),
        runbook_id=runbook_id,
    )

    _notify_ims(tenant_id, runbook, runbook_id)

    return api_response(
        200,
        {
            "runbook_id": runbook_id,
            "approval_status": "approved",
            "execution_status": result["status"],
        },
    )
