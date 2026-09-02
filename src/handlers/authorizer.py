"""fn-authorizer

API Gateway REQUEST authorizer for the admin API. Cryptographically verifies
the bearer ID token against this user pool's JWKS (see common.jwt for why that
replaced the previous cognito-idp:GetUser check), and surfaces the caller's
tenant_id and highest-priority group to downstream Lambdas via the authorizer
context.

Makes no AWS API calls on the request path: everything it needs is in the
verified token, so the result is cacheable and cannot be throttled by Cognito.
"""
import logging

from common import jwt
from common.jwt import InvalidToken

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _extract_token(event):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    auth_header = headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    return auth_header.strip()


def _policy(principal_id, effect, resource, context=None):
    return {
        "principalId": principal_id,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {"Action": "execute-api:Invoke", "Effect": effect, "Resource": resource}
            ],
        },
        "context": context or {},
    }


def _api_stage_arn(method_arn):
    """The whole API stage covered by a cached successful authorization.

    API Gateway caches this policy by bearer token. Returning only ``methodArn``
    would allow the first route requested and make every different route fail
    until the five-minute authorizer cache expired.
    """
    api_arn, stage, *_ = method_arn.split("/")
    return f"{api_arn}/{stage}/*/*"


def handler(event, context):
    method_arn = event["methodArn"]

    try:
        claims = jwt.verify_id_token(_extract_token(event))
    except InvalidToken as exc:
        # Log the reason (never the token) so a misconfigured client is
        # diagnosable without the 401 itself telling a caller what to fix.
        logger.info("denying request: %s", exc)
        return _policy("anonymous", "Deny", method_arn)

    principal = claims.get("cognito:username") or claims.get("sub") or "unknown"
    tenant_id = claims.get("custom:tenant_id") or ""
    if not tenant_id:
        logger.info("denying %s: token carries no tenant_id", principal)
        return _policy(principal, "Deny", method_arn)

    return _policy(
        principal,
        "Allow",
        _api_stage_arn(method_arn),
        {"tenant_id": tenant_id, "group": jwt.highest_priority_group(claims)},
    )
