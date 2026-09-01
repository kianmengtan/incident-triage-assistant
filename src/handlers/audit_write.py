"""fn-audit-write

Persists an append-only AuditTrail record. Invoked asynchronously by other
functions via common.audit.record_audit rather than called directly by end
users.

Append-only is enforced, not just described: the sort key carries a unique
suffix and the put is conditional on that key being absent. The previous key was
``audit#{unix_second}#{actor}``, so two actions by the same actor in the same
second silently overwrote each other — including the "attempted" and outcome
pair that trigger_remediation writes around a single approval.
"""
import logging
import time
import uuid

from botocore.exceptions import ClientError

from common import config, tenant_scope

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_SIX_MONTHS_SECONDS = 6 * 30 * 24 * 60 * 60


def handler(event, context):
    tenant_id = event["tenant_id"]
    now = int(time.time())
    entry_id = str(uuid.uuid4())

    table = tenant_scope.tenant_dynamodb_resource(tenant_id).Table(config.AUDIT_TABLE)
    try:
        table.put_item(
            Item={
                "tenant_id": tenant_id,
                # Time-ordered prefix so a Query returns chronologically, with a
                # unique suffix so nothing can collide.
                "sk": f"audit#{now}#{entry_id}",
                "entry_id": entry_id,
                "action": event["action"],
                "actor": event["actor"],
                "result": event["result"],
                "alert_id": event.get("alert_id"),
                "runbook_id": event.get("runbook_id"),
                "timestamp": now,
                "expires_at": now + _SIX_MONTHS_SECONDS,
            },
            ConditionExpression="attribute_not_exists(sk)",
        )
    except ClientError as exc:
        logger.error(
            "failed to write audit record for tenant %s action %s: %s",
            tenant_id,
            event.get("action"),
            exc,
        )
        # Re-raised so the async invocation retries and, if it keeps failing,
        # lands in the audit DLQ instead of vanishing.
        raise

    return {"recorded": True, "entry_id": entry_id}
