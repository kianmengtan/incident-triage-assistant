"""fn-tenant-provision

Cognito PostConfirmation Lambda trigger. Fires once, the first time any user
of a given tenant_id confirms their signup: creates the Tenant profile row
(if it doesn't already exist) and provisions that tenant's per-tenant
secrets (data-encryption key, ingestion HMAC secret, empty integration
credential placeholders). Later users of the same tenant_id are no-ops.
"""
import json
import time

import boto3
from botocore.exceptions import ClientError

from common import config, crypto

_secrets = boto3.client("secretsmanager", region_name=config.REGION)
_dynamodb = boto3.resource("dynamodb", region_name=config.REGION)


def _create_secret_if_missing(name, value):
    try:
        _secrets.create_secret(Name=name, SecretString=value)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceExistsException":
            raise


def handler(event, context):
    attrs = event["request"]["userAttributes"]
    tenant_id = attrs.get("custom:tenant_id")
    if not tenant_id:
        return event

    table = _dynamodb.Table(config.TENANTS_TABLE)
    dek_secret_name = f"{config.PREFIX}-tenant-{tenant_id}-dek"

    try:
        table.put_item(
            Item={
                "tenant_id": tenant_id,
                "sk": "PROFILE",
                "name": tenant_id,
                "status": "active",
                "dek_secret_arn": dek_secret_name,
                "created_at": int(time.time()),
            },
            ConditionExpression="attribute_not_exists(tenant_id)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return event  # tenant already provisioned

    _create_secret_if_missing(dek_secret_name, crypto.generate_dek())
    _create_secret_if_missing(f"{config.PREFIX}-tenant-{tenant_id}-ingest-hmac", crypto.generate_dek())
    _create_secret_if_missing(
        f"{config.PREFIX}-tenant-{tenant_id}-integration-creds",
        json.dumps({"log_platform": {}, "vcs": {}, "remediation_platform": {}, "ims": {}}),
    )

    return event
