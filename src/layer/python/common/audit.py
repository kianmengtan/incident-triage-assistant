import json

import boto3

from . import config

_lambda = boto3.client("lambda", region_name=config.REGION)


def record_audit(tenant_id, actor, action, result, alert_id=None, runbook_id=None):
    payload = {
        "tenant_id": tenant_id,
        "actor": actor,
        "action": action,
        "result": result,
        "alert_id": alert_id,
        "runbook_id": runbook_id,
    }
    _lambda.invoke(
        FunctionName=config.AUDIT_WRITE_FUNCTION_NAME,
        InvocationType="Event",
        Payload=json.dumps(payload).encode("utf-8"),
    )
