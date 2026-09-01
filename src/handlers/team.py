"""fn-team

``GET /v1/team`` and ``POST /v1/team/{userSub}/role`` on the admin API: who is in
this tenant, and what they are allowed to do.

The roster comes from member rows in the Tenants table
(``sk = USER#{sub}``), written by ``fn-tenant-provision`` at signup, rather than
from Cognito. Two reasons: ``ListUsers`` cannot filter on a custom attribute like
``custom:tenant_id``, so finding one tenant's users through Cognito would mean
paging the entire pool and filtering in memory; and a row in the tenant's own
partition inherits the IAM session-tag scoping that already protects every other
table read, instead of introducing a second, unscoped path to user data.

**The tenant check before any Cognito call is load-bearing.** Cognito's admin APIs
are account-wide -- ``admin_add_user_to_group`` will promote any user in the pool,
in any tenant. What confines this handler is that it first looks the target up in
its *own* tenant's partition and refuses if the row is not there. Take that lookup
out and an admin of one tenant can hand themselves, or revoke, roles in another.
"""
import json
import logging

import boto3
from boto3.dynamodb.conditions import Key

from common import audit, config, rbac, tenant_scope
from common.response import api_response

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_cognito = boto3.client("cognito-idp", region_name=config.REGION)

MEMBER_PREFIX = "USER#"
ACTION = "team.role_change"

LIST_CAPABILITY = "view_team"
CHANGE_CAPABILITY = "manage_roles"

# One page is plenty for a tenant's roster, and bounds the response.
MAX_MEMBERS = 200


def _authorizer_ctx(event):
    return (event.get("requestContext", {}) or {}).get("authorizer") or {}


def _members_table(tenant_id):
    return tenant_scope.tenant_dynamodb_resource(tenant_id).Table(config.TENANTS_TABLE)


def _members(table, tenant_id):
    resp = table.query(
        KeyConditionExpression=Key("tenant_id").eq(tenant_id)
        & Key("sk").begins_with(MEMBER_PREFIX),
        Limit=MAX_MEMBERS,
    )
    return resp.get("Items", []) or []


def _public(member):
    """Only the fields the console needs; sk and tenant_id are storage details."""
    return {
        "sub": member.get("sub"),
        "email": member.get("email"),
        "role": member.get("role"),
        "created_at": member.get("created_at"),
    }


def _current_group(username):
    """The user's role according to Cognito, which is what the token will carry."""
    resp = _cognito.admin_list_groups_for_user(
        UserPoolId=config.USER_POOL_ID, Username=username
    )
    held = [g.get("GroupName") for g in resp.get("Groups", [])]
    for candidate in config.GROUP_PRIORITY:
        if candidate in held:
            return candidate
    return ""


def _list_team(tenant_id, group):
    if not rbac.can(group, LIST_CAPABILITY):
        return api_response(403, {"message": rbac.denial_message(group, LIST_CAPABILITY)})
    table = _members_table(tenant_id)
    return api_response(
        200, {"members": [_public(m) for m in _members(table, tenant_id)]}
    )


def _change_role(event, tenant_id, group, actor):
    if not rbac.can(group, CHANGE_CAPABILITY):
        audit.record_audit(
            tenant_id=tenant_id,
            actor=actor,
            action=ACTION,
            result="refused_not_permitted",
        )
        return api_response(403, {"message": rbac.denial_message(group, CHANGE_CAPABILITY)})

    try:
        payload = json.loads(event.get("body") or "")
    except (json.JSONDecodeError, TypeError):
        return api_response(400, {"message": "body must be a JSON object with a role"})
    if not isinstance(payload, dict):
        return api_response(400, {"message": "body must be a JSON object with a role"})

    new_role = payload.get("role")
    if new_role not in rbac.ROLES:
        return api_response(
            400, {"message": "unknown role", "accepted": list(rbac.ROLES)}
        )

    target_sub = (event.get("pathParameters") or {}).get("userSub")
    if not target_sub:
        return api_response(400, {"message": "no user given"})

    table = _members_table(tenant_id)
    # The tenant boundary. A sub that is not a member row in *this* partition is
    # indistinguishable from one that does not exist, so the response is the same
    # 404 either way and this does not confirm who exists in other tenants.
    target = table.get_item(
        Key={"tenant_id": tenant_id, "sk": f"{MEMBER_PREFIX}{target_sub}"}
    ).get("Item")
    if not target:
        return api_response(404, {"message": "no such member of this tenant"})

    username = target.get("username") or target.get("email")
    current_role = _current_group(username)

    if current_role == new_role:
        # Nothing to do. Removing and re-adding the same group would be a no-op
        # with an audit record implying a change happened.
        return api_response(200, {"member": _public({**target, "role": new_role})})

    if current_role == rbac.TENANT_ADMIN and new_role != rbac.TENANT_ADMIN:
        admins = [
            m
            for m in _members(table, tenant_id)
            if m.get("role") == rbac.TENANT_ADMIN and m.get("sub") != target_sub
        ]
        if not admins:
            # Only the first user of a tenant is ever made an admin
            # automatically, so a tenant that demotes its last one can never get
            # another, and approving remediation becomes permanently impossible.
            return api_response(
                409,
                {
                    "message": (
                        "This is the tenant's only Tenant Admin. Promote someone "
                        "else to admin first, or nobody will be able to approve "
                        "remediation."
                    )
                },
            )

    if current_role:
        _cognito.admin_remove_user_from_group(
            UserPoolId=config.USER_POOL_ID, Username=username, GroupName=current_role
        )
    _cognito.admin_add_user_to_group(
        UserPoolId=config.USER_POOL_ID, Username=username, GroupName=new_role
    )

    # The roster is served from DynamoDB, so a Cognito-only change would keep
    # showing the old role until that user's next sign-in.
    table.update_item(
        Key={"tenant_id": tenant_id, "sk": f"{MEMBER_PREFIX}{target_sub}"},
        UpdateExpression="SET #r = :role",
        ExpressionAttributeNames={"#r": "role"},
        ExpressionAttributeValues={":role": new_role},
    )

    audit.record_audit(
        tenant_id=tenant_id, actor=actor, action=ACTION, result=f"{current_role or 'none'}->{new_role}"
    )
    logger.info(
        "%s changed %s from %s to %s in tenant %s",
        actor, target_sub, current_role or "none", new_role, tenant_id,
    )

    return api_response(
        200,
        {
            "member": _public({**target, "role": new_role}),
            # The affected user keeps their old permissions until their token is
            # reissued, so the UI can say so rather than looking broken.
            "note": "takes effect when that user's session refreshes",
        },
    )


def handler(event, context):
    ctx = _authorizer_ctx(event)
    tenant_id = ctx.get("tenant_id")
    group = ctx.get("group")
    actor = ctx.get("principalId", "unknown")
    resource = event.get("resource", "")

    if not tenant_id:
        return api_response(403, {"message": "forbidden"})

    if resource == "/v1/team":
        return _list_team(tenant_id, group)

    if resource == "/v1/team/{userSub}/role":
        return _change_role(event, tenant_id, group, actor)

    return api_response(404, {"message": "not found"})
