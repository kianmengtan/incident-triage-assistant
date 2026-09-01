"""fn-tenant-provision

Cognito PostConfirmation trigger. Runs once per confirmed signup and is the
only place a user's tenant is decided.

Two things here are load-bearing for multi-tenancy:

1. **tenant_id is derived here, never accepted from the client.** It used to be
   a writable ``custom:tenant_id`` attribute the user supplied at signup, which
   with self-signup enabled meant anyone could type an existing tenant's id and
   read that tenant's diagnostics. It is now derived from the verified email
   domain and written back by this function; the app client cannot write it.
2. **The first user of a tenant becomes its TenantAdmin.** Nothing else grants
   group membership, so without this no one could ever approve remediation
   (Requirement 7) — the approval path was unreachable end to end.

Provisioning order matters too: the tenant's secrets are created BEFORE the
profile row, because the row is what marks the tenant as provisioned. Writing
it first and then failing to create the DEK left the tenant permanently unable
to ingest or diagnose anything, with every later signup short-circuiting on
"already provisioned".
"""
import json
import logging
import re
import time

import boto3
from botocore.exceptions import ClientError

from common import config, crypto

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_secrets = boto3.client("secretsmanager", region_name=config.REGION)
_dynamodb = boto3.resource("dynamodb", region_name=config.REGION)
_cognito = boto3.client("cognito-idp", region_name=config.REGION)

ADMIN_GROUP = "TenantAdmin"
MEMBER_GROUP = "TenantEngineer"

# A shared consumer-mail domain is not an organisation: deriving a tenant from
# it would put every gmail.com signup in one tenant, sharing each other's
# incidents. Those signups get no tenant, and the authorizer denies them.
PUBLIC_EMAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "yahoo.com",
        "icloud.com",
        "me.com",
        "aol.com",
        "proton.me",
        "protonmail.com",
        "gmx.com",
        "mail.com",
        "yandex.com",
        "zoho.com",
        "qq.com",
    }
)


def tenant_id_for_email(email):
    """Derive a stable tenant id from an email address, or None.

    The domain is slugified so the result is safe as a DynamoDB partition key,
    a Secrets Manager name component and an IAM session tag value.
    """
    if not email or "@" not in email:
        return None
    local, _, domain = email.rpartition("@")
    # Both halves must be present: an address like "@example.com" is not one
    # this function should derive a tenant from.
    if not local.strip():
        return None
    domain = domain.strip().lower()
    if not domain or domain in PUBLIC_EMAIL_DOMAINS:
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", domain).strip("-")
    return slug or None


def _create_secret_if_missing(name, value):
    try:
        _secrets.create_secret(Name=name, SecretString=value)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceExistsException":
            raise


def _provision_secrets(tenant_id):
    _create_secret_if_missing(
        f"{config.PREFIX}-tenant-{tenant_id}-dek", crypto.generate_dek()
    )
    _create_secret_if_missing(
        f"{config.PREFIX}-tenant-{tenant_id}-ingest-hmac", crypto.generate_dek()
    )
    _create_secret_if_missing(
        f"{config.PREFIX}-tenant-{tenant_id}-integration-creds",
        json.dumps({"log_platform": {}, "vcs": {}, "remediation_platform": {}, "ims": {}}),
    )


def _claim_tenant(tenant_id):
    """Write the tenant profile row. True if this call created the tenant."""
    table = _dynamodb.Table(config.TENANTS_TABLE)
    try:
        table.put_item(
            Item={
                "tenant_id": tenant_id,
                "sk": "PROFILE",
                "name": tenant_id,
                "status": "active",
                "dek_secret_arn": f"{config.PREFIX}-tenant-{tenant_id}-dek",
                "created_at": int(time.time()),
            },
            ConditionExpression="attribute_not_exists(tenant_id)",
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return False


def handler(event, context):
    user_pool_id = event["userPoolId"]
    username = event["userName"]
    attrs = event["request"]["userAttributes"]

    tenant_id = tenant_id_for_email(attrs.get("email"))
    if not tenant_id:
        # Fail closed: no tenant attribute is written, so the authorizer denies
        # every request this user makes rather than admitting them to a
        # guessed-at tenant.
        logger.warning(
            "no tenant could be derived for %s; user is confirmed but unscoped", username
        )
        return event

    try:
        # Secrets first — see the module docstring.
        _provision_secrets(tenant_id)
        is_founder = _claim_tenant(tenant_id)

        _cognito.admin_update_user_attributes(
            UserPoolId=user_pool_id,
            Username=username,
            UserAttributes=[{"Name": "custom:tenant_id", "Value": tenant_id}],
        )
        _cognito.admin_add_user_to_group(
            UserPoolId=user_pool_id,
            Username=username,
            GroupName=ADMIN_GROUP if is_founder else MEMBER_GROUP,
        )
        logger.info(
            "provisioned %s into tenant %s as %s",
            username,
            tenant_id,
            ADMIN_GROUP if is_founder else MEMBER_GROUP,
        )
    except ClientError as exc:
        # Never re-raise: the user is already confirmed by the time this trigger
        # runs, so raising just returns an error to a client that cannot retry
        # confirmation. An unscoped user is denied by the authorizer, which is
        # the safe outcome, and the next signup for this domain retries cleanly
        # because the profile row is only written after the secrets exist.
        logger.exception("provisioning failed for %s in tenant %s: %s", username, tenant_id, exc)

    return event
