"""fn-query-diagnostics

Serves the read-only admin API routes:
  GET /v1/diagnostics/{alertId}
  GET /v1/runbooks
  GET /v1/runbooks/{runbookId}
  GET /v1/runbooks/{runbookId}/export

Every query is scoped to the tenant_id taken from the authorizer context
(never from client input), so a caller can only ever see their own tenant's
rows — a cross-tenant request simply returns 404, not another tenant's data.
"""
from boto3.dynamodb.conditions import Key

from common import config, crypto, tenant_scope
from common.response import api_response


def _tenant_id(event):
    return (event.get("requestContext", {}).get("authorizer") or {}).get("tenant_id")


def _get_diagnostic(tenant_id, alert_id):
    table = tenant_scope.tenant_dynamodb_resource(tenant_id).Table(config.DIAGNOSTICS_TABLE)
    resp = table.get_item(Key={"tenant_id": tenant_id, "sk": f"diag#{alert_id}"})
    item = resp.get("Item")
    if not item:
        return None
    item["rca_summary"] = crypto.decrypt_field(tenant_id, item.get("rca_summary"))
    remediation = crypto.decrypt_field(tenant_id, item.get("remediation_steps")) or ""
    item["remediation_steps"] = [s for s in remediation.split("\n") if s]
    return item


def _list_runbooks(tenant_id, status_filter):
    table = tenant_scope.tenant_dynamodb_resource(tenant_id).Table(config.RUNBOOKS_TABLE)
    if status_filter:
        resp = table.query(
            IndexName="status-index",
            KeyConditionExpression=Key("tenant_id").eq(tenant_id)
            & Key("status").eq(status_filter),
        )
    else:
        resp = table.query(KeyConditionExpression=Key("tenant_id").eq(tenant_id))
    return resp.get("Items", [])


def _get_runbook(tenant_id, runbook_id):
    table = tenant_scope.tenant_dynamodb_resource(tenant_id).Table(config.RUNBOOKS_TABLE)
    resp = table.get_item(Key={"tenant_id": tenant_id, "sk": f"runbook#{runbook_id}"})
    return resp.get("Item")


def handler(event, context):
    tenant_id = _tenant_id(event)
    if not tenant_id:
        return api_response(403, {"message": "forbidden"})

    resource = event.get("resource", "")
    params = event.get("pathParameters") or {}
    query = event.get("queryStringParameters") or {}

    if resource == "/v1/diagnostics/{alertId}":
        diagnostic = _get_diagnostic(tenant_id, params["alertId"])
        if not diagnostic:
            return api_response(404, {"message": "not found"})
        return api_response(200, diagnostic)

    if resource == "/v1/runbooks":
        runbooks = _list_runbooks(tenant_id, query.get("status"))
        return api_response(200, {"runbooks": runbooks})

    if resource == "/v1/runbooks/{runbookId}":
        runbook = _get_runbook(tenant_id, params["runbookId"])
        if not runbook:
            return api_response(404, {"message": "not found"})
        s3 = tenant_scope.tenant_s3_client(tenant_id)
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": config.RUNBOOKS_BUCKET, "Key": runbook["s3_key"]},
            ExpiresIn=300,
        )
        runbook["download_url"] = url
        return api_response(200, runbook)

    if resource == "/v1/runbooks/{runbookId}/export":
        runbook = _get_runbook(tenant_id, params["runbookId"])
        if not runbook:
            return api_response(404, {"message": "not found"})
        return api_response(200, {"runbook": runbook})

    return api_response(404, {"message": "not found"})
