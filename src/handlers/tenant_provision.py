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

Provisioning order matters too: the tenant's key material is created BEFORE the
profile row, because the row is what marks the tenant as provisioned. Writing
it first and then failing to create the DEK left the tenant permanently unable
to ingest or diagnose anything, with every later signup short-circuiting on
"already provisioned".

That key material lives in SSM Parameter Store, not Secrets Manager. The
permissions boundary this role must carry grants Secrets Manager read only, so
``CreateSecret`` was denied on every signup -- and because the ``except
ClientError`` below never re-raises, the user was confirmed with no
``custom:tenant_id`` and the console showed them a dead end after a successful
sign-in. See :mod:`common.paramstore`.
"""
import json
import logging
import re
import time

import boto3
from botocore.exceptions import ClientError

from common import config, crypto, paramstore, rbac, tenancy

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_dynamodb = boto3.resource("dynamodb", region_name=config.REGION)
_cognito = boto3.client("cognito-idp", region_name=config.REGION)

ADMIN_GROUP = rbac.TENANT_ADMIN
MEMBER_GROUP = rbac.TENANT_ENGINEER

# Re-exported: fn-pre-signup refuses the addresses this cannot derive a tenant
# from, so both triggers must read one list (see common.tenancy).
PUBLIC_EMAIL_DOMAINS = tenancy.PUBLIC_EMAIL_DOMAINS
tenant_id_for_email = tenancy.tenant_id_for_email


def _provision_keys(tenant_id):
    """Create the tenant's key material, leaving anything already there alone.

    Idempotent per kind: a second signup for the same domain must not replace a
    DEK that already has ciphertext encrypted under it.
    """
    paramstore.create_if_missing(tenant_id, paramstore.DEK, crypto.generate_dek())
    paramstore.create_if_missing(tenant_id, paramstore.INGEST_HMAC, crypto.generate_dek())
    paramstore.create_if_missing(
        tenant_id,
        paramstore.INTEGRATION_CREDS,
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
                "dek_param_name": paramstore.parameter_name(tenant_id, paramstore.DEK),
                "created_at": int(time.time()),
            },
            ConditionExpression="attribute_not_exists(tenant_id)",
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return False


def _record_member(tenant_id, sub, username, email, role):
    """Write the tenant's roster row for this user.

    fn-team serves the roster from these rows rather than from Cognito, because
    ListUsers cannot filter on a custom attribute like custom:tenant_id -- finding
    one tenant's users through Cognito would mean paging the whole pool. Keeping
    them here also means the roster is read through the same session-tagged,
    partition-scoped path as every other table read.

    Best-effort: a user who is already confirmed and in their group is usable
    without this row, so a failure here is logged rather than raised. It costs
    them a line in the team list, not access.
    """
    table = _dynamodb.Table(config.TENANTS_TABLE)
    try:
        table.put_item(
            Item={
                "tenant_id": tenant_id,
                "sk": f"USER#{sub}",
                "sub": sub,
                # What Cognito's admin APIs need; fn-team uses it rather than
                # trusting an identifier from the URL.
                "username": username,
                "email": email,
                "role": role,
                "created_at": int(time.time()),
            }
        )
    except ClientError as exc:
        logger.error("could not record team member row for %s: %s", username, exc)


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
        # Key material first — see the module docstring.
        _provision_keys(tenant_id)
        is_founder = _claim_tenant(tenant_id)

        _cognito.admin_update_user_attributes(
            UserPoolId=user_pool_id,
            Username=username,
            UserAttributes=[{"Name": "custom:tenant_id", "Value": tenant_id}],
        )
        role = ADMIN_GROUP if is_founder else MEMBER_GROUP
        _cognito.admin_add_user_to_group(
            UserPoolId=user_pool_id,
            Username=username,
            GroupName=role,
        )
        # After the group is assigned, so the stored role matches what the token
        # will actually carry.
        _record_member(
            tenant_id, attrs.get("sub") or username, username, attrs.get("email"), role
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
