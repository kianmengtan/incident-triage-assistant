"""Per-tenant IAM isolation.

Every data-plane call (DynamoDB, S3) is made using temporary credentials
obtained by assuming a single shared role and tagging the session with the
caller's tenant_id. The assumed role's own policy restricts DynamoDB access
to items whose partition key equals ``${aws:PrincipalTag/tenant_id}`` and S3
access to objects under ``tenant/${aws:PrincipalTag/tenant_id}/*`` — so a
Lambda holding these credentials physically cannot touch another tenant's
rows or objects, regardless of what the application code asks for.
"""
import time

import boto3

from . import config

_cache = {}
_CACHE_TTL_SECONDS = 15 * 60


def _assumed_session(tenant_id):
    cached = _cache.get(tenant_id)
    now = time.time()
    if cached and cached["expires_at"] - now > 60:
        return cached["session"]

    sts = boto3.client("sts", region_name=config.REGION)
    resp = sts.assume_role(
        RoleArn=config.TENANT_SCOPED_ROLE_ARN,
        RoleSessionName=f"tenant-{tenant_id}"[:64],
        Tags=[{"Key": "tenant_id", "Value": tenant_id}],
        TransitiveTagKeys=["tenant_id"],
        DurationSeconds=_CACHE_TTL_SECONDS,
    )
    creds = resp["Credentials"]
    session = boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=config.REGION,
    )
    _cache[tenant_id] = {"session": session, "expires_at": creds["Expiration"].timestamp()}
    return session


def tenant_dynamodb_resource(tenant_id):
    return _assumed_session(tenant_id).resource("dynamodb", region_name=config.REGION)


def tenant_s3_client(tenant_id):
    return _assumed_session(tenant_id).client("s3", region_name=config.REGION)


def tenant_object_key(tenant_id, *parts):
    return "/".join(["tenant", tenant_id, *[str(p) for p in parts]])
