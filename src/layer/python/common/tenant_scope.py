"""Per-tenant IAM isolation.

Every data-plane call (DynamoDB, S3) is made using temporary credentials
obtained by assuming a single shared role and tagging the session with the
caller's tenant_id. The assumed role's own policy restricts DynamoDB access
to items whose partition key equals ``${aws:PrincipalTag/tenant_id}`` and S3
access to objects under ``tenant/${aws:PrincipalTag/tenant_id}/*`` — so a
Lambda holding these credentials physically cannot touch another tenant's
rows or objects, regardless of what the application code asks for.

Note that passing session tags needs ``sts:TagSession`` as well as
``sts:AssumeRole``, in the caller's own policy AND in the role's trust policy.
Granting only AssumeRole makes every call here fail with AccessDenied, which
looks like a broken application rather than a missing permission.
"""
import time

import boto3

from . import config

_cache = {}

DEFAULT_SESSION_SECONDS = 15 * 60
# The role's MaxSessionDuration; nothing may request more than this.
MAX_SESSION_SECONDS = 60 * 60


def _assumed_session(tenant_id, duration_seconds=DEFAULT_SESSION_SECONDS, min_remaining_seconds=60):
    """Credentials for tenant_id, valid for at least min_remaining_seconds.

    Callers that hand out something outliving the call itself — a presigned URL,
    say — must pass the lifetime they need as min_remaining_seconds, or they can
    be given a cached session that expires first.
    """
    duration_seconds = min(duration_seconds, MAX_SESSION_SECONDS)
    cached = _cache.get(tenant_id)
    now = time.time()
    if cached and cached["expires_at"] - now > min_remaining_seconds:
        return cached["session"]

    sts = boto3.client("sts", region_name=config.REGION)
    resp = sts.assume_role(
        RoleArn=config.TENANT_SCOPED_ROLE_ARN,
        RoleSessionName=f"tenant-{tenant_id}"[:64],
        Tags=[{"Key": "tenant_id", "Value": tenant_id}],
        TransitiveTagKeys=["tenant_id"],
        DurationSeconds=duration_seconds,
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


def tenant_s3_client(tenant_id, min_remaining_seconds=60):
    return _assumed_session(
        tenant_id, min_remaining_seconds=min_remaining_seconds
    ).client("s3", region_name=config.REGION)


def tenant_signing_s3_client(tenant_id, url_lifetime_seconds):
    """An S3 client whose credentials outlive the presigned URLs it produces.

    A presigned URL dies with the credentials that signed it, so signing with a
    cached session that has 40 seconds left yields a URL good for 40 seconds
    whatever ExpiresIn says.
    """
    return _assumed_session(
        tenant_id,
        duration_seconds=max(DEFAULT_SESSION_SECONDS, url_lifetime_seconds * 2),
        min_remaining_seconds=url_lifetime_seconds,
    ).client("s3", region_name=config.REGION)


def tenant_object_key(tenant_id, *parts):
    return "/".join(["tenant", tenant_id, *[str(p) for p in parts]])
