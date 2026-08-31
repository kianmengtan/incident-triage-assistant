"""fn-correlate-config

Step Functions task. Queries the tenant's Git/VCS/deployment API (using
tenant-scoped credentials) for recent changes preceding the alert, and
caches the diff under the tenant's S3 prefix.
"""
import json
import time
import urllib.request

import boto3
from botocore.exceptions import ClientError

from common import config, tenant_scope

_secrets = boto3.client("secretsmanager", region_name=config.REGION)


def _vcs_creds(tenant_id):
    try:
        secret = _secrets.get_secret_value(
            SecretId=f"{config.PREFIX}-tenant-{tenant_id}-integration-creds"
        )
        return json.loads(secret["SecretString"]).get("vcs", {})
    except ClientError:
        return {}


def _query_vcs(creds, alert):
    endpoint = creds.get("endpoint")
    if not endpoint:
        return {"changes": [], "source": "none", "note": "no VCS platform configured"}
    request = urllib.request.Request(
        endpoint,
        headers={"Authorization": f"Bearer {creds.get('api_key', '')}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as exc:  # noqa: BLE001 - external dependency, degrade gracefully
        return {"changes": [], "source": "error", "note": str(exc)}


def handler(event, context):
    start = time.time()
    tenant_id = event["tenant_id"]
    alert = event["alert"]

    creds = _vcs_creds(tenant_id)
    changes = _query_vcs(creds, alert)

    s3 = tenant_scope.tenant_s3_client(tenant_id)
    key = tenant_scope.tenant_object_key(tenant_id, "alert", event["alert_id"], "config.json")
    s3.put_object(
        Bucket=config.CONTEXT_CACHE_BUCKET,
        Key=key,
        Body=json.dumps(changes).encode("utf-8"),
        ContentType="application/json",
    )

    elapsed = time.time() - start
    return {
        "s3_key": key,
        "change_count": len(changes.get("changes", [])),
        "sla_flagged": elapsed > 60,
    }
