"""fn-ingest-normalize

Validates the inbound alert's HMAC signature (using that tenant's own
ingestion secret), normalizes the payload, writes the Alerts row with a
conditional put that genuinely deduplicates on alert_id, and publishes
``alert.received`` to EventBridge so the diagnosis pipeline can start
asynchronously.
"""
import base64
import hashlib
import hmac
import json
import logging

import boto3
from botocore.exceptions import ClientError

from common import alerts, config, paramstore, tenant_scope
from common.response import api_response

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_events = boto3.client("events", region_name=config.REGION)

REQUIRED_FIELDS = ("tenant_id", "severity", "service", "description")


def _raw_body(event):
    """The exact bytes the signature was computed over.

    API Gateway base64-encodes the body for content types it treats as binary,
    so signing has to happen against the decoded text or every such delivery
    fails signature validation.
    """
    body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        try:
            return base64.b64decode(body).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return ""
    return body


def _valid_signature(tenant_id, raw_body, signature):
    if not signature:
        return False
    try:
        shared_secret = paramstore.read(tenant_id, paramstore.INGEST_HMAC)
    except ClientError:
        # Also the path for an unknown tenant_id: the response is the same 401
        # either way, so the endpoint does not confirm which tenants exist.
        return False
    expected = hmac.new(
        shared_secret.encode("utf-8"), raw_body.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def handler(event, context):
    raw_body = _raw_body(event)
    if len(raw_body.encode("utf-8")) > alerts.MAX_BODY_BYTES:
        return api_response(413, {"message": "alert payload too large"})

    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    signature = headers.get("x-signature", "")

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return api_response(400, {"message": "invalid JSON body"})
    if not isinstance(payload, dict):
        return api_response(400, {"message": "body must be a JSON object"})

    missing = [f for f in REQUIRED_FIELDS if not payload.get(f)]
    if missing:
        return api_response(400, {"message": f"missing fields: {', '.join(missing)}"})
    if not alerts.is_valid_severity(payload["severity"]):
        return api_response(400, {"message": "unrecognised severity"})
    if "alert_id" in payload and payload["alert_id"] is not None:
        if not alerts.usable_alert_id(payload["alert_id"]):
            return api_response(
                400,
                {
                    "message": (
                        "alert_id must be 1-128 characters of letters, digits, "
                        "dot, underscore, colon or hyphen"
                    )
                },
            )

    tenant_id = str(payload["tenant_id"])

    if not _valid_signature(tenant_id, raw_body, signature):
        return api_response(401, {"message": "invalid signature"})

    alert = alerts.normalize(payload, tenant_id)
    table = tenant_scope.tenant_dynamodb_resource(tenant_id).Table(config.ALERTS_TABLE)
    outcome = alerts.store_and_dispatch(table, tenant_id, alert)

    if outcome == alerts.DISPATCH_FAILED:
        # Stored, but nothing will diagnose it. A 202 here would tell the caller
        # it was accepted and leave it stuck forever.
        return api_response(
            502, {"alert_id": alert["alert_id"], "message": "could not start diagnosis"}
        )
    if outcome == alerts.DUPLICATE:
        return api_response(202, {"alert_id": alert["alert_id"], "status": "duplicate"})
    return api_response(202, {"alert_id": alert["alert_id"], "status": "received"})
