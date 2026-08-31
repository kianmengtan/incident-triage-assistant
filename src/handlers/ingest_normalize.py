"""fn-ingest-normalize

Validates the inbound alert's HMAC signature (using that tenant's own
ingestion secret), normalizes the payload, writes the Alerts row with a
conditional put to dedup on alert_id, and publishes ``alert.received`` to
EventBridge so the diagnosis pipeline can start asynchronously.
"""
import hashlib
import hmac
import json
import time
import uuid

import boto3
from botocore.exceptions import ClientError

from common import config, tenant_scope
from common.response import api_response

_events = boto3.client("events", region_name=config.REGION)
_secrets = boto3.client("secretsmanager", region_name=config.REGION)

REQUIRED_FIELDS = ("tenant_id", "severity", "service", "description")


def _ingest_secret_name(tenant_id):
    return f"{config.PREFIX}-tenant-{tenant_id}-ingest-hmac"


def _valid_signature(tenant_id, raw_body, signature):
    if not signature:
        return False
    try:
        secret = _secrets.get_secret_value(SecretId=_ingest_secret_name(tenant_id))
    except ClientError:
        return False
    expected = hmac.new(
        secret["SecretString"].encode("utf-8"), raw_body.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _normalize(payload):
    alert_id = payload.get("alert_id") or str(uuid.uuid4())
    received_at = int(time.time())
    return {
        "alert_id": alert_id,
        "tenant_id": payload["tenant_id"],
        "source": payload.get("source", "unknown"),
        "severity": payload["severity"],
        "service": payload["service"],
        "description": payload["description"],
        "status": "received",
        "received_at": received_at,
    }


def handler(event, context):
    raw_body = event.get("body") or "{}"
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    signature = headers.get("x-signature", "")

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return api_response(400, {"message": "invalid JSON body"})

    missing = [f for f in REQUIRED_FIELDS if not payload.get(f)]
    if missing:
        return api_response(400, {"message": f"missing fields: {missing}"})

    tenant_id = payload["tenant_id"]

    if not _valid_signature(tenant_id, raw_body, signature):
        return api_response(401, {"message": "invalid signature"})

    alert = _normalize(payload)
    sort_key = f"alert#{alert['received_at']}#{alert['alert_id']}"

    table = tenant_scope.tenant_dynamodb_resource(tenant_id).Table(config.ALERTS_TABLE)
    try:
        table.put_item(
            Item={"tenant_id": tenant_id, "sk": sort_key, **alert},
            ConditionExpression="attribute_not_exists(sk)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return api_response(202, {"alert_id": alert["alert_id"], "status": "duplicate"})
        raise

    _events.put_events(
        Entries=[
            {
                "Source": "outagediag.ingestion",
                "DetailType": "alert.received",
                "Detail": json.dumps(
                    {
                        "tenant_id": tenant_id,
                        "alert_id": alert["alert_id"],
                        "received_at": alert["received_at"],
                        "alert": alert,
                    }
                ),
            }
        ]
    )

    return api_response(202, {"alert_id": alert["alert_id"], "status": "received"})
