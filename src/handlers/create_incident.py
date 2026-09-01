"""fn-create-incident

``POST /v1/alerts`` on the admin API: a signed-in person raising an incident from
the console's form, rather than a monitoring tool posting to the public webhook.

The webhook (``fn-ingest-normalize``) authenticates with an API key plus an HMAC
over the request body, and only once that signature verifies does it trust the
``tenant_id`` inside the body. This endpoint has no such signature -- it has a
Cognito ID token -- so the tenant comes from the authorizer context and a
``tenant_id`` in the body is ignored entirely. Reading it from the body here would
let any signed-in user write an incident into another tenant, which is the same
mistake ``fn-tenant-provision`` avoids by deriving the tenant at signup instead of
accepting one.

Everything after validation is shared with the webhook via ``common.alerts``, so a
hand-raised incident is stored in the same shape and starts the same diagnosis
pipeline as one that arrived from PagerDuty.
"""
import json
import logging

from common import alerts, audit, config, rbac, tenant_scope
from common.response import api_response

logger = logging.getLogger()
logger.setLevel(logging.INFO)

CAPABILITY = "create_incident"

# tenant_id is deliberately absent: it comes from the verified token.
REQUIRED_FIELDS = ("severity", "service", "description")

ACTION = "incident.create"


def _authorizer_ctx(event):
    return (event.get("requestContext", {}) or {}).get("authorizer") or {}


def handler(event, context):
    ctx = _authorizer_ctx(event)
    tenant_id = ctx.get("tenant_id")
    group = ctx.get("group")
    actor = ctx.get("principalId", "unknown")

    if not tenant_id:
        # An account the PostConfirmation trigger could not scope to a tenant --
        # a public-domain signup. It can read nothing and create nothing.
        return api_response(403, {"message": "forbidden"})

    if not rbac.can(group, CAPABILITY):
        # Audited: an attempt to raise an incident that was refused is a fact
        # about who tried to do what, which is what the trail is for.
        audit.record_audit(
            tenant_id=tenant_id,
            actor=actor,
            action=ACTION,
            result="refused_not_permitted",
        )
        return api_response(403, {"message": rbac.denial_message(group, CAPABILITY)})

    raw_body = event.get("body") or "{}"
    if len(raw_body.encode("utf-8")) > alerts.MAX_BODY_BYTES:
        return api_response(413, {"message": "incident payload too large"})

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
        return api_response(
            400,
            {
                "message": "unrecognised severity",
                "accepted": sorted(alerts.VALID_SEVERITIES),
            },
        )

    if payload.get("alert_id") and not alerts.usable_alert_id(payload["alert_id"]):
        return api_response(
            400,
            {
                "message": (
                    "alert_id must be 1-128 characters of letters, digits, dot, "
                    "underscore, colon or hyphen"
                )
            },
        )

    # Distinguishes a hand-raised incident from a webhook one, which the overview
    # needs in order to be honest about where its numbers come from.
    payload.setdefault("source", "console")

    alert = alerts.normalize(payload, tenant_id)
    table = tenant_scope.tenant_dynamodb_resource(tenant_id).Table(config.ALERTS_TABLE)
    outcome = alerts.store_and_dispatch(table, tenant_id, alert)

    if outcome == alerts.DISPATCH_FAILED:
        # Stored, but nothing will diagnose it. Answering 202 would tell the
        # person their incident is being worked on when it is inert.
        audit.record_audit(
            tenant_id=tenant_id,
            actor=actor,
            action=ACTION,
            result="dispatch_failed",
            alert_id=alert["alert_id"],
        )
        return api_response(
            502, {"alert_id": alert["alert_id"], "message": "could not start diagnosis"}
        )

    if outcome == alerts.DUPLICATE:
        # A webhook retry is routine and answers 202. A person double-submitting a
        # form is a different situation: telling them "received" would leave them
        # believing they had raised a second, separate incident.
        return api_response(
            409,
            {
                "alert_id": alert["alert_id"],
                "status": "duplicate",
                "message": "an incident with this id already exists and is being diagnosed",
            },
        )

    audit.record_audit(
        tenant_id=tenant_id,
        actor=actor,
        action=ACTION,
        result="created",
        alert_id=alert["alert_id"],
    )
    logger.info("incident %s raised in tenant %s by %s", alert["alert_id"], tenant_id, actor)

    return api_response(
        202,
        {
            "alert_id": alert["alert_id"],
            "status": "received",
            "severity": alert["severity"],
            "service": alert["service"],
            "received_at": alert["received_at"],
        },
    )
