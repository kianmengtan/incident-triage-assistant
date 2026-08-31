"""fn-authorizer

API Gateway REQUEST authorizer for the admin API. Validates the bearer
access token against Cognito (cognito-idp:GetUser), and resolves the
caller's tenant_id and highest-priority group, surfaced to downstream
Lambdas via the authorizer context.
"""
import boto3
from botocore.exceptions import ClientError

from common import config

_cognito = boto3.client("cognito-idp", region_name=config.REGION)

GROUP_PRIORITY = ["TenantAdmin", "TenantEngineer", "TenantLeadership"]


def _extract_token(event):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    auth_header = headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:]
    return auth_header


def _resolve_group(username):
    try:
        resp = _cognito.admin_list_groups_for_user(
            UserPoolId=config.USER_POOL_ID, Username=username
        )
    except ClientError:
        return None
    names = {g["GroupName"] for g in resp.get("Groups", [])}
    for candidate in GROUP_PRIORITY:
        if candidate in names:
            return candidate
    return None


def _policy(principal_id, effect, resource, context):
    return {
        "principalId": principal_id,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {"Action": "execute-api:Invoke", "Effect": effect, "Resource": resource}
            ],
        },
        "context": context,
    }


def handler(event, context):
    token = _extract_token(event)
    method_arn = event["methodArn"]

    if not token:
        return _policy("anonymous", "Deny", method_arn, {})

    try:
        user = _cognito.get_user(AccessToken=token)
    except ClientError:
        return _policy("anonymous", "Deny", method_arn, {})

    username = user["Username"]
    attrs = {a["Name"]: a["Value"] for a in user.get("UserAttributes", [])}
    tenant_id = attrs.get("custom:tenant_id", "")
    group = _resolve_group(username) or ""

    if not tenant_id:
        return _policy(username, "Deny", method_arn, {})

    return _policy(username, "Allow", method_arn, {"tenant_id": tenant_id, "group": group})
