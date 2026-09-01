"""Audit recording.

Writes go through a separate function invoked asynchronously so that recording
an action never adds latency to it. The invocation is fire-and-forget by design,
but the audit-write function has an on-failure destination (an SQS DLQ) so a
record that cannot be persisted is recoverable rather than lost — which matters
for a trail the product describes as append-only and retained six months.
"""
import json
import logging

import boto3
from botocore.exceptions import ClientError

from . import config

logger = logging.getLogger(__name__)

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
    try:
        _lambda.invoke(
            FunctionName=config.AUDIT_WRITE_FUNCTION_NAME,
            InvocationType="Event",
            Payload=json.dumps(payload).encode("utf-8"),
        )
    except ClientError as exc:
        # Log the whole record: if the audit path is broken, the log line is the
        # only remaining evidence that the action happened.
        logger.error("could not enqueue audit record %s: %s", payload, exc)
