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
import re
import time
import uuid

import boto3
from botocore.exceptions import ClientError

from common import config, tenant_scope
from common.response import api_response

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_events = boto3.client("events", region_name=config.REGION)
_secrets = boto3.client("secretsmanager", region_name=config.REGION)

REQUIRED_FIELDS = ("tenant_id", "severity", "service", "description")

# DynamoDB items cap at 400 KB; refuse oversized alerts at the edge with a 413
# rather than letting the put fail as an unexplained 500 further in.
MAX_BODY_BYTES = 128 * 1024
MAX_FIELD_CHARS = 8 * 1024

# alert_id is client-supplied and is interpolated into the Alerts sort key
# (alert#{alert_id}) and into the correlation cache's S3 object keys
# (tenant/{tenant}/alert/{alert_id}/logs.json). Unvalidated, a "/" nested the
# cached evidence under a prefix nothing reads back, and a "#" could forge a key
# in another namespace of a schema built entirely on prefix#id.
ALERT_ID_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")

VALID_SEVERITIES = frozenset(
    {
        "sev1", "sev2", "sev3", "sev4",
        "critical", "high", "major", "moderate", "medium", "warning", "low", "minor", "info",
        "p1", "p2", "p3", "p4",
    }
)


def _usable_alert_id(alert_id):
    return bool(ALERT_ID_PATTERN.match(alert_id))


def _ingest_secret_name(tenant_id):
    return f"{config.PREFIX}-tenant-{tenant_id}-ingest-hmac"


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
        secret = _secrets.get_secret_value(SecretId=_ingest_secret_name(tenant_id))
    except ClientError:
        # Also the path for an unknown tenant_id: the response is the same 401
        # either way, so the endpoint does not confirm which tenants exist.
        return False
    expected = hmac.new(
        secret["SecretString"].encode("utf-8"), raw_body.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _normalize(payload):
    alert_id = str(payload.get("alert_id") or uuid.uuid4())
    return {
        "alert_id": alert_id,
        "tenant_id": payload["tenant_id"],
        "source": str(payload.get("source", "unknown"))[:MAX_FIELD_CHARS],
        "severity": str(payload["severity"])[:MAX_FIELD_CHARS],
        "service": str(payload["service"])[:MAX_FIELD_CHARS],
        "description": str(payload["description"])[:MAX_FIELD_CHARS],
        "status": "received",
        "received_at": int(time.time()),
    }


def _publish(alert):
    """Publish alert.received. False if EventBridge rejected the entry."""
    resp = _events.put_events(
        Entries=[
            {
                "Source": "outagediag.ingestion",
                "DetailType": "alert.received",
                "Detail": json.dumps(
                    {
                        "tenant_id": alert["tenant_id"],
                        "alert_id": alert["alert_id"],
                        "received_at": alert["received_at"],
                        "alert": alert,
                    },
                    default=str,
                ),
            }
        ]
    )
    if resp.get("FailedEntryCount"):
        failures = [e for e in resp.get("Entries", []) if e.get("ErrorCode")]
        logger.error("failed to publish alert.received for %s: %s", alert["alert_id"], failures)
        return False
    return True


def _mark_dispatched(table, tenant_id, sort_key):
    table.update_item(
        Key={"tenant_id": tenant_id, "sk": sort_key},
        UpdateExpression="SET dispatched_at = :now",
        ExpressionAttributeValues={":now": int(time.time())},
    )


def handler(event, context):
    raw_body = _raw_body(event)
    if len(raw_body.encode("utf-8")) > MAX_BODY_BYTES:
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
    if str(payload["severity"]).strip().lower() not in VALID_SEVERITIES:
        return api_response(400, {"message": "unrecognised severity"})
    if "alert_id" in payload and payload["alert_id"] is not None:
        if not _usable_alert_id(str(payload["alert_id"])):
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

    alert = _normalize(payload)
    # Keyed on alert_id alone. Including received_at here meant a webhook retry
    # a second later produced a different sort key, the conditional put
    # succeeded, and the same alert was diagnosed twice.
    sort_key = f"alert#{alert['alert_id']}"

    table = tenant_scope.tenant_dynamodb_resource(tenant_id).Table(config.ALERTS_TABLE)
    try:
        table.put_item(
            Item={"tenant_id": tenant_id, "sk": sort_key, **alert},
            ConditionExpression="attribute_not_exists(sk)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        # Already seen. If the first delivery stored the row but failed to
        # publish, dispatched_at is absent and this retry is the alert's only
        # remaining chance to be diagnosed — so publish rather than reporting a
        # duplicate and dropping it silently.
        existing = table.get_item(Key={"tenant_id": tenant_id, "sk": sort_key}).get("Item") or {}
        if existing.get("dispatched_at"):
            return api_response(202, {"alert_id": alert["alert_id"], "status": "duplicate"})
        if not _publish(existing or alert):
            return api_response(
                502, {"alert_id": alert["alert_id"], "message": "could not start diagnosis"}
            )
        _mark_dispatched(table, tenant_id, sort_key)
        return api_response(202, {"alert_id": alert["alert_id"], "status": "received"})

    if not _publish(alert):
        # The alert is stored but nothing will diagnose it. Reporting 202 would
        # tell the caller it was accepted and leave it stuck forever, so fail
        # loudly; the retry path above picks it up from the stored row.
        return api_response(
            502, {"alert_id": alert["alert_id"], "message": "could not start diagnosis"}
        )
    _mark_dispatched(table, tenant_id, sort_key)

    return api_response(202, {"alert_id": alert["alert_id"], "status": "received"})
