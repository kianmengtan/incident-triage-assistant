"""Per-tenant third-party integration credentials.

One secret per tenant (``{PREFIX}-tenant-{tenant_id}-integration-creds``) holds
one entry per integration. Four handlers used to carry their own identical copy
of this lookup; they all call :func:`creds` now.
"""
import json
import logging

import boto3
from botocore.exceptions import ClientError

from . import config

logger = logging.getLogger(__name__)

_secrets = boto3.client("secretsmanager", region_name=config.REGION)

LOG_PLATFORM = "log_platform"
VCS = "vcs"
REMEDIATION_PLATFORM = "remediation_platform"
IMS = "ims"


def _secret_name(tenant_id):
    return f"{config.PREFIX}-tenant-{tenant_id}-integration-creds"


def creds(tenant_id, integration):
    """Return one integration's credentials, or {} if unset or unreadable."""
    try:
        secret = _secrets.get_secret_value(SecretId=_secret_name(tenant_id))
    except ClientError as exc:
        logger.warning(
            "cannot read integration creds for tenant %s: %s",
            tenant_id,
            exc.response["Error"]["Code"],
        )
        return {}
    try:
        return json.loads(secret["SecretString"]).get(integration, {}) or {}
    except (json.JSONDecodeError, AttributeError):
        logger.warning("integration creds for tenant %s are not valid JSON", tenant_id)
        return {}
