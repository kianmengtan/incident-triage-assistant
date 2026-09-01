"""fn-notify-ims

Invoked after remediation approval/execution. Pushes the diagnostic findings and
the runbook link to the tenant's configured Incident Management System, if any.
Failures are logged and swallowed — this must never block or fail the
remediation flow (Requirement 8.2).
"""
import logging

from common import http, integrations

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event, context):
    tenant_id = event["tenant_id"]
    creds = integrations.creds(tenant_id, integrations.IMS)
    endpoint = creds.get("endpoint")

    if not endpoint:
        logger.info("no IMS configured for tenant %s, skipping", tenant_id)
        return {"notified": False, "reason": "not_configured"}

    payload = {
        "alert_id": event.get("alert_id"),
        "runbook_id": event.get("runbook_id"),
        "runbook_link": event.get("runbook_link"),
        "rca_summary": event.get("rca_summary"),
    }

    try:
        http.request_json(endpoint, api_key=creds.get("api_key"), payload=payload, method="POST")
    except http.EndpointNotAllowed as exc:
        logger.warning("refusing to call IMS endpoint for %s: %s", tenant_id, exc)
        return {"notified": False, "reason": "endpoint_not_permitted"}
    except Exception as exc:  # noqa: BLE001 - must never raise to the caller
        logger.warning("IMS notification failed for tenant %s: %s", tenant_id, type(exc).__name__)
        return {"notified": False, "reason": type(exc).__name__}
    return {"notified": True}
