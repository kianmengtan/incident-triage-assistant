"""fn-notify-ims

Invoked after remediation approval/execution. Pushes diagnostic findings and
the runbook link to the tenant's configured Incident Management System, if
any. Failures are logged and swallowed — this must never block the
remediation flow (Requirement 8.2).
"""
import json
import logging
import urllib.request

import boto3
from botocore.exceptions import ClientError

from common import config

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_secrets = boto3.client("secretsmanager", region_name=config.REGION)


def _ims_creds(tenant_id):
    try:
        secret = _secrets.get_secret_value(
            SecretId=f"{config.PREFIX}-tenant-{tenant_id}-integration-creds"
        )
        return json.loads(secret["SecretString"]).get("ims", {})
    except ClientError:
        return {}


def handler(event, context):
    tenant_id = event["tenant_id"]
    creds = _ims_creds(tenant_id)
    endpoint = creds.get("endpoint")

    if not endpoint:
        logger.info("no IMS configured for tenant %s, skipping", tenant_id)
        return {"notified": False, "reason": "not_configured"}

    body = json.dumps(
        {
            "alert_id": event.get("alert_id"),
            "runbook_id": event.get("runbook_id"),
            "runbook_link": event.get("runbook_link"),
            "rca_summary": event.get("rca_summary"),
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Authorization": f"Bearer {creds.get('api_key', '')}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10):
            return {"notified": True}
    except Exception as exc:  # noqa: BLE001 - must never raise to the caller
        logger.warning("IMS notification failed for tenant %s: %s", tenant_id, exc)
        return {"notified": False, "reason": str(exc)}
