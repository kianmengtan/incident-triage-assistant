"""Writing an alert and starting its diagnosis -- shared by both entry points.

Two things create incidents: ``fn-ingest-normalize``, the public webhook
authenticated by an API key plus that tenant's HMAC secret, and
``fn-create-incident``, the authenticated endpoint behind the console's form.
Both have to produce the same Alerts row and the same ``alert.received`` event,
because everything downstream -- the Step Functions pipeline, the correlation
cache's S3 keys, the incident rail -- reads that one shape.

The deduplication here is the subtle part and the reason this is shared rather
than written twice. The conditional put is keyed on ``alert#{alert_id}`` alone,
so a webhook retry a second later collides instead of being diagnosed a second
time. But a row that exists *without* ``dispatched_at`` means an earlier delivery
stored the alert and then failed to publish it, and that retry is the alert's
only remaining chance to be diagnosed -- so it must publish rather than report a
duplicate and drop it silently.
"""
import json
import logging
import re
import time
import uuid

import boto3
from botocore.exceptions import ClientError

from common import config

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_events = boto3.client("events", region_name=config.REGION)

# DynamoDB items cap at 400 KB. Oversized input is refused at the edge with a 413
# rather than letting the put fail as an unexplained 500 further in.
MAX_BODY_BYTES = 128 * 1024
MAX_FIELD_CHARS = 8 * 1024

# alert_id reaches the Alerts sort key (``alert#{alert_id}``) and the correlation
# cache's S3 object keys (``tenant/{tenant}/alert/{alert_id}/logs.json``).
# Unvalidated, a "/" nested the cached evidence under a prefix nothing reads back,
# and a "#" could forge a key in another namespace of a schema built entirely on
# ``prefix#id``.
ALERT_ID_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")

# Accepts what real monitoring tools actually send, rather than forcing callers to
# translate into one vocabulary.
VALID_SEVERITIES = frozenset(
    {
        "sev1", "sev2", "sev3", "sev4",
        "critical", "high", "major", "moderate", "medium", "warning", "low", "minor", "info",
        "p1", "p2", "p3", "p4",
    }
)

#: Outcomes of :func:`store_and_dispatch`. Each caller maps these to its own
#: status code, because the webhook and the console answer differently.
CREATED = "created"
REDISPATCHED = "redispatched"
DUPLICATE = "duplicate"
DISPATCH_FAILED = "dispatch_failed"


def is_valid_severity(severity):
    if severity is None:
        return False
    return str(severity).strip().lower() in VALID_SEVERITIES


def usable_alert_id(alert_id):
    return bool(ALERT_ID_PATTERN.match(str(alert_id or "")))


def normalize(payload, tenant_id):
    """Build the stored alert row.

    ``tenant_id`` is a parameter rather than a field read out of ``payload`` on
    purpose. The webhook derives it from the body only *after* verifying that
    body's HMAC signature; the authenticated endpoint takes it from the
    authorizer context. Making it an argument means a handler cannot silently
    fall back to a client-supplied value, which is how a caller would otherwise
    write an alert into somebody else's tenant.
    """
    return {
        "alert_id": str(payload.get("alert_id") or uuid.uuid4()),
        "tenant_id": tenant_id,
        "source": str(payload.get("source", "unknown"))[:MAX_FIELD_CHARS],
        "severity": str(payload["severity"])[:MAX_FIELD_CHARS],
        "service": str(payload["service"])[:MAX_FIELD_CHARS],
        "description": str(payload["description"])[:MAX_FIELD_CHARS],
        "status": "received",
        "received_at": int(time.time()),
    }


def publish_received(alert):
    """Publish ``alert.received``. False if EventBridge rejected the entry."""
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


def store_and_dispatch(table, tenant_id, alert):
    """Store the alert and start its diagnosis. Returns one of the outcomes above.

    ``table`` is a tenant-scoped DynamoDB Table resource, so the caller decides
    how it was scoped (both callers use ``tenant_scope``, which session-tags an
    assumed role so DynamoDB itself enforces the partition).
    """
    sort_key = f"alert#{alert['alert_id']}"

    try:
        table.put_item(
            Item={"tenant_id": tenant_id, "sk": sort_key, **alert},
            ConditionExpression="attribute_not_exists(sk)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        existing = table.get_item(Key={"tenant_id": tenant_id, "sk": sort_key}).get("Item") or {}
        if existing.get("dispatched_at"):
            return DUPLICATE
        # Stored but never dispatched: this is the alert's last chance.
        if not publish_received(existing or alert):
            return DISPATCH_FAILED
        _mark_dispatched(table, tenant_id, sort_key)
        return REDISPATCHED

    if not publish_received(alert):
        # Stored, but nothing will diagnose it. Saying "accepted" here would
        # leave the alert stuck forever, so the caller has to be told.
        return DISPATCH_FAILED
    _mark_dispatched(table, tenant_id, sort_key)
    return CREATED
