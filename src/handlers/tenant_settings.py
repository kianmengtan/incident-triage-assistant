"""fn-tenant-settings

GET    /v1/integrations
PUT    /v1/integrations/{integration}
DELETE /v1/integrations/{integration}

The write path for a tenant's third-party integration credentials, which had none.
fn-tenant-provision creates the secret with four empty objects and nothing in the
deployed system could ever fill them in, so on a fresh deployment log correlation
and config correlation both returned "no platform configured" for good, the IMS
was never notified, and every approval recorded execution_status "skipped". The
integrations are what the diagnosis pipeline draws its evidence from and what
executes a runbook, so without this the product's headline feature is unreachable.

Two decisions worth stating:

* **The endpoint is validated here, against the same guard the outbound calls
  use.** Rejecting an SSRF target at write time tells the tenant immediately,
  rather than letting it surface later as a degraded diagnosis with a note nobody
  can act on. The outbound guard still runs on every call -- this is the earlier
  of two gates, not a replacement for it.
* **The API key is write-only.** A read returns the endpoint, which is the
  tenant's own configuration and useful to display, and a boolean for whether a
  key is set. Echoing the key back would turn any console XSS into credential
  exfiltration.
"""
import json
import logging

import boto3
from botocore.exceptions import ClientError

from common import audit, config, http, integrations, rbac
from common.response import api_response

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_secrets = boto3.client("secretsmanager", region_name=config.REGION)

# Only a TenantAdmin may change where this system sends a tenant's credentials or
# which endpoint it will execute a runbook against.
# Kept as a name for readability; common.rbac's matrix is the authority.
ADMIN_GROUP = rbac.TENANT_ADMIN
CAPABILITY = "manage_integrations"

KNOWN_INTEGRATIONS = (
    integrations.LOG_PLATFORM,
    integrations.VCS,
    integrations.REMEDIATION_PLATFORM,
    integrations.IMS,
)

MAX_ENDPOINT_CHARS = 2048
MAX_API_KEY_CHARS = 4096


def _authorizer_ctx(event):
    return event.get("requestContext", {}).get("authorizer") or {}


def _secret_name(tenant_id):
    return f"{config.PREFIX}-tenant-{tenant_id}-integration-creds"


def _read(tenant_id):
    """The tenant's whole integration document, or empty defaults."""
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
        stored = json.loads(secret["SecretString"])
    except (json.JSONDecodeError, TypeError):
        logger.warning("integration creds for tenant %s are not valid JSON", tenant_id)
        return {}
    return stored if isinstance(stored, dict) else {}


def _redacted(document):
    """What a caller may see: the endpoint, and whether a key is set."""
    out = {}
    for name in KNOWN_INTEGRATIONS:
        entry = document.get(name) or {}
        out[name] = {
            "endpoint": entry.get("endpoint") or None,
            "api_key_set": bool(entry.get("api_key")),
        }
    return out


def _endpoint_problem(endpoint):
    """Why this endpoint is unacceptable, or None if it is fine."""
    if not endpoint or not isinstance(endpoint, str):
        return "endpoint is required"
    if len(endpoint) > MAX_ENDPOINT_CHARS:
        return f"endpoint must be at most {MAX_ENDPOINT_CHARS} characters"
    try:
        # Shape only, deliberately: no DNS. A configuration write must not fail
        # because resolution was briefly unavailable, and where a hostname points
        # is re-checked on every outbound call anyway -- which is the gate that
        # actually has to hold, since a name can be repointed after this write.
        http.assert_target_shape(endpoint)
    except http.EndpointNotAllowed as exc:
        return f"endpoint is not acceptable: {exc}"
    return None


def _write(tenant_id, document):
    _secrets.put_secret_value(
        SecretId=_secret_name(tenant_id), SecretString=json.dumps(document)
    )


def _put(tenant_id, integration, body):
    endpoint = body.get("endpoint")
    problem = _endpoint_problem(endpoint)
    if problem:
        return api_response(400, {"message": problem})

    api_key = body.get("api_key")
    if api_key is not None and (not isinstance(api_key, str) or len(api_key) > MAX_API_KEY_CHARS):
        return api_response(400, {"message": "api_key must be a string"})

    document = _read(tenant_id)
    entry = {"endpoint": endpoint}
    if api_key:
        entry["api_key"] = api_key
    elif (document.get(integration) or {}).get("api_key"):
        # An update that omits api_key keeps the stored one, so changing an
        # endpoint does not silently drop the credential behind it.
        entry["api_key"] = document[integration]["api_key"]
    document[integration] = entry
    _write(tenant_id, document)
    return api_response(200, {"integration": integration, "endpoint": endpoint})


def _delete(tenant_id, integration):
    document = _read(tenant_id)
    document[integration] = {}
    _write(tenant_id, document)
    return api_response(200, {"integration": integration, "endpoint": None})


def handler(event, context):
    ctx = _authorizer_ctx(event)
    tenant_id = ctx.get("tenant_id")
    group = ctx.get("group")
    actor = ctx.get("principalId", "unknown")
    resource = event.get("resource", "")
    method = (event.get("httpMethod") or "").upper()
    integration = (event.get("pathParameters") or {}).get("integration")

    if not tenant_id:
        return api_response(403, {"message": "forbidden"})

    if resource not in ("/v1/integrations", "/v1/integrations/{integration}"):
        return api_response(404, {"message": "not found"})

    if not rbac.can(group, CAPABILITY):
        # Audited on the write paths: which endpoint this system will hand a
        # tenant's credentials to, and which one it will execute a runbook
        # against, is exactly the kind of change an audit trail exists to show.
        if method in ("PUT", "DELETE"):
            audit.record_audit(
                tenant_id=tenant_id,
                actor=actor,
                action="integration.update",
                result="refused_not_admin",
            )
        return api_response(403, {"message": rbac.denial_message(group, CAPABILITY)})

    if resource == "/v1/integrations":
        return api_response(200, {"integrations": _redacted(_read(tenant_id))})

    if integration not in KNOWN_INTEGRATIONS:
        return api_response(
            400, {"message": f"unknown integration; expected one of {', '.join(KNOWN_INTEGRATIONS)}"}
        )

    if method == "DELETE":
        response = _delete(tenant_id, integration)
    else:
        try:
            body = json.loads(event.get("body") or "{}")
        except json.JSONDecodeError:
            return api_response(400, {"message": "invalid JSON body"})
        if not isinstance(body, dict):
            return api_response(400, {"message": "body must be a JSON object"})
        response = _put(tenant_id, integration, body)

    if response["statusCode"] == 200:
        # The integration name, never the credential: the audit trail is read back
        # by leadership as well as admins.
        audit.record_audit(
            tenant_id=tenant_id,
            actor=actor,
            action="integration.update",
            result=integration,
        )
    return response
