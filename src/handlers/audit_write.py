"""fn-audit-write

Persists an append-only AuditTrail record. Invoked asynchronously by other
functions via common.audit.record_audit rather than called directly by
end users.
"""
import time

import boto3

from common import config, tenant_scope

_SIX_MONTHS_SECONDS = 6 * 30 * 24 * 60 * 60


def handler(event, context):
    tenant_id = event["tenant_id"]
    now = int(time.time())

    table = tenant_scope.tenant_dynamodb_resource(tenant_id).Table(config.AUDIT_TABLE)
    table.put_item(
        Item={
            "tenant_id": tenant_id,
            "sk": f"audit#{now}#{event['actor']}",
            "action": event["action"],
            "actor": event["actor"],
            "result": event["result"],
            "alert_id": event.get("alert_id"),
            "runbook_id": event.get("runbook_id"),
            "timestamp": now,
            "expires_at": now + _SIX_MONTHS_SECONDS,
        }
    )
    return {"recorded": True}
