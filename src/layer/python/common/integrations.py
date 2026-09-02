"""Per-tenant third-party integration credentials.

One SSM parameter per tenant (``/{PREFIX}/tenant/{tenant_id}/integration-creds``,
see :mod:`common.paramstore`) holds one entry per integration. Four handlers used
to carry their own identical copy of this lookup; they all call :func:`creds` now.
"""
import json
import logging

from botocore.exceptions import ClientError

from . import paramstore

logger = logging.getLogger(__name__)

LOG_PLATFORM = "log_platform"
VCS = "vcs"
REMEDIATION_PLATFORM = "remediation_platform"
IMS = "ims"


def creds(tenant_id, integration):
    """Return one integration's credentials, or {} if unset or unreadable."""
    try:
        document = paramstore.read(tenant_id, paramstore.INTEGRATION_CREDS)
    except ClientError as exc:
        logger.warning(
            "cannot read integration creds for tenant %s: %s",
            tenant_id,
            exc.response["Error"]["Code"],
        )
        return {}
    try:
        return json.loads(document).get(integration, {}) or {}
    except (json.JSONDecodeError, AttributeError):
        logger.warning("integration creds for tenant %s are not valid JSON", tenant_id)
        return {}
