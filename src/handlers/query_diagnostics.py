"""fn-query-diagnostics

Serves the read-only admin API routes:
  GET /v1/alerts                      the incident list
  GET /v1/alerts/{alertId}/status     where the pipeline is, for the stage timeline
  GET /v1/diagnostics/{alertId}
  GET /v1/runbooks
  GET /v1/runbooks/{runbookId}
  GET /v1/runbooks/{runbookId}/export
  GET /v1/audit                       the audit trail

Every query is scoped to the tenant_id taken from the authorizer context
(never from client input), so a caller can only ever see their own tenant's
rows — a cross-tenant request simply returns 404, not another tenant's data.
"""
import json
import logging

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError
from cryptography.fernet import InvalidToken

from common import config, crypto, progress, tenant_scope
from common.response import api_response

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# One page of runbooks. The previous version read resp["Items"] once and dropped
# whatever DynamoDB left behind its 1 MB limit, silently, with a 200.
PAGE_SIZE = 50
MAX_PAGES = 20

# Reading the audit trail is a review activity, so leadership gets it as well as
# admins — and it is the only thing the TenantLeadership group currently confers.
AUDIT_READERS = ("TenantAdmin", "TenantLeadership")


def _tenant_id(event):
    return (event.get("requestContext", {}).get("authorizer") or {}).get("tenant_id")


def _decrypt(tenant_id, ciphertext, field, alert_id):
    """Decrypt one field, or return None and log rather than 500.

    A field encrypted under a DEK that has since been replaced cannot be read
    back; that should degrade the response, not break the endpoint.
    """
    if ciphertext is None:
        return None
    try:
        return crypto.decrypt_field(tenant_id, ciphertext)
    except (InvalidToken, ClientError) as exc:
        logger.error(
            "cannot decrypt %s for tenant %s alert %s: %s",
            field,
            tenant_id,
            alert_id,
            type(exc).__name__,
        )
        return None


def _parse_steps(plaintext):
    """Steps are stored as a JSON array of objects.

    Rows written by the earlier newline-joined format are still readable, so an
    existing deployment's diagnostics do not become unreadable on upgrade.
    """
    if not plaintext:
        return []
    try:
        parsed = json.loads(plaintext)
    except json.JSONDecodeError:
        return [
            {"text": line, "priority": "P2", "command": None, "reversible": None}
            for line in plaintext.split("\n")
            if line
        ]
    return parsed if isinstance(parsed, list) else []


def _parse_json_list(plaintext):
    if not plaintext:
        return []
    try:
        parsed = json.loads(plaintext)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _get_diagnostic(tenant_id, alert_id):
    table = tenant_scope.tenant_dynamodb_resource(tenant_id).Table(config.DIAGNOSTICS_TABLE)
    item = table.get_item(Key={"tenant_id": tenant_id, "sk": f"diag#{alert_id}"}).get("Item")
    if not item:
        return None

    item["rca_summary"] = _decrypt(tenant_id, item.get("rca_summary"), "rca_summary", alert_id)
    item["remediation_steps"] = _parse_steps(
        _decrypt(tenant_id, item.get("remediation_steps"), "remediation_steps", alert_id)
    )
    item["rag_context_refs"] = _parse_json_list(
        _decrypt(tenant_id, item.get("rag_context_refs"), "rag_context_refs", alert_id)
    )
    return item


def _and(*conditions):
    """Combine the conditions that are set, or None if none are."""
    present = [c for c in conditions if c is not None]
    if not present:
        return None
    combined = present[0]
    for condition in present[1:]:
        combined = combined & condition
    return combined


def _paginated(table, limit, page_size=None, **kwargs):
    """Collect up to `limit` items, following LastEvaluatedKey.

    `page_size` is how many items DynamoDB may examine per request, which is not
    the same as how many it returns: a FilterExpression is applied after the read,
    so a filtered query needs a page size larger than `limit` or it examines one
    row per round trip.
    """
    items = []
    pages = 0
    kwargs["Limit"] = page_size or limit
    while pages < MAX_PAGES:
        resp = table.query(**kwargs)
        items.extend(resp.get("Items", []))
        pages += 1
        last_key = resp.get("LastEvaluatedKey")
        if not last_key or len(items) >= limit:
            break
        kwargs["ExclusiveStartKey"] = last_key
    return items[:limit]


def _requested_limit(query):
    try:
        return max(1, min(int(query.get("limit", PAGE_SIZE)), PAGE_SIZE))
    except (TypeError, ValueError):
        return PAGE_SIZE


def _list_alerts(tenant_id, limit):
    """Newest first, via the received-at index.

    The base table's sort key is alert#{alert_id} so it deduplicates correctly
    but orders by id; chronological order comes from the GSI.
    """
    table = tenant_scope.tenant_dynamodb_resource(tenant_id).Table(config.ALERTS_TABLE)
    return _paginated(
        table,
        limit,
        IndexName="received-at-index",
        KeyConditionExpression=Key("tenant_id").eq(tenant_id),
        ScanIndexForward=False,
    )


def _alert_status(tenant_id, alert_id):
    """Where this alert is in the pipeline.

    Stage marks are advisory (see common.progress); the authoritative facts are
    whether the diagnostic and runbook rows exist, so those are checked too and
    win when they disagree.
    """
    resource = tenant_scope.tenant_dynamodb_resource(tenant_id)
    alert = resource.Table(config.ALERTS_TABLE).get_item(
        Key={"tenant_id": tenant_id, "sk": f"alert#{alert_id}"}
    ).get("Item")
    if not alert:
        return None

    diagnostic = resource.Table(config.DIAGNOSTICS_TABLE).get_item(
        Key={"tenant_id": tenant_id, "sk": f"diag#{alert_id}"}
    ).get("Item")

    # Filtered in DynamoDB and paginated. A bare Query stops at the 1 MB page
    # limit, so for a tenant with many ready runbooks this alert's own could sit
    # past it -- and the state then regressed to "diagnosed" with a null
    # runbook_id even though the runbook was ready and awaiting approval.
    runbooks = _paginated(
        resource.Table(config.RUNBOOKS_TABLE),
        1,
        page_size=PAGE_SIZE,
        IndexName="status-index",
        KeyConditionExpression=Key("tenant_id").eq(tenant_id) & Key("status").eq("ready"),
        FilterExpression=Attr("alert_id").eq(alert_id),
    )
    runbook = runbooks[0] if runbooks else None

    if alert.get("pipeline_stage") == progress.FAILED:
        state = "failed"
    elif runbook:
        state = "runbook_ready"
    elif diagnostic:
        state = "diagnosed"
    elif alert.get("dispatched_at"):
        state = "diagnosing"
    else:
        state = "received"

    return {
        "alert_id": alert_id,
        "state": state,
        "stage": alert.get("pipeline_stage"),
        "stages_seen": alert.get("stages_seen", []),
        "stage_order": list(progress.STAGE_ORDER),
        "received_at": alert.get("received_at"),
        "dispatched_at": alert.get("dispatched_at"),
        "elapsed_seconds": (
            int(alert.get("pipeline_stage_at", 0)) - int(alert.get("received_at", 0))
            if alert.get("pipeline_stage_at") else None
        ),
        "runbook_id": (runbook or {}).get("runbook_id"),
        "approval_status": (runbook or {}).get("approval_status"),
        "execution_status": (runbook or {}).get("execution_status"),
        "confidence": (diagnostic or {}).get("confidence"),
        "sla_budget_seconds": config.RUNBOOK_SLA_SECONDS,
    }


def _list_audit(tenant_id, limit, alert_id=None, runbook_id=None):
    """Newest first. The sort key is audit#{unix_seconds}#{uuid}, so it sorts
    chronologically without an index.

    The alert/runbook filter goes to DynamoDB rather than being applied to the
    result. Filtering afterwards meant ?alert_id=X returned nothing whenever X was
    not among the newest `limit` records tenant-wide -- which is every incident
    except the most recent one.
    """
    table = tenant_scope.tenant_dynamodb_resource(tenant_id).Table(config.AUDIT_TABLE)
    kwargs = {
        "KeyConditionExpression": Key("tenant_id").eq(tenant_id) & Key("sk").begins_with("audit#"),
        "ScanIndexForward": False,
    }
    condition = _and(
        Attr("alert_id").eq(alert_id) if alert_id else None,
        Attr("runbook_id").eq(runbook_id) if runbook_id else None,
    )
    if condition is not None:
        kwargs["FilterExpression"] = condition
    # A FilterExpression is applied after the read, so a page can come back short
    # of `limit` with more matches behind it; _paginated already follows
    # LastEvaluatedKey until it has enough or runs out of pages.
    return _paginated(table, limit, **kwargs)


def _list_runbooks(tenant_id, status_filter, limit=PAGE_SIZE):
    table = tenant_scope.tenant_dynamodb_resource(tenant_id).Table(config.RUNBOOKS_TABLE)
    kwargs = {}
    if status_filter:
        kwargs["IndexName"] = "status-index"
        kwargs["KeyConditionExpression"] = Key("tenant_id").eq(tenant_id) & Key("status").eq(
            status_filter
        )
    else:
        kwargs["KeyConditionExpression"] = Key("tenant_id").eq(tenant_id)
    return _paginated(table, limit, **kwargs)


def _get_runbook(tenant_id, runbook_id):
    table = tenant_scope.tenant_dynamodb_resource(tenant_id).Table(config.RUNBOOKS_TABLE)
    return table.get_item(Key={"tenant_id": tenant_id, "sk": f"runbook#{runbook_id}"}).get("Item")


def _runbook_markdown(tenant_id, s3_key):
    """The runbook document itself, read out of S3."""
    s3 = tenant_scope.tenant_s3_client(tenant_id)
    obj = s3.get_object(Bucket=config.RUNBOOKS_BUCKET, Key=s3_key)
    return obj["Body"].read().decode("utf-8")


def handler(event, context):
    tenant_id = _tenant_id(event)
    if not tenant_id:
        return api_response(403, {"message": "forbidden"})

    resource = event.get("resource", "")
    params = event.get("pathParameters") or {}
    query = event.get("queryStringParameters") or {}

    if resource == "/v1/alerts":
        return api_response(200, {"alerts": _list_alerts(tenant_id, _requested_limit(query))})

    if resource == "/v1/alerts/{alertId}/status":
        status = _alert_status(tenant_id, params["alertId"])
        if not status:
            return api_response(404, {"message": "not found"})
        return api_response(200, status)

    if resource == "/v1/audit":
        group = (event.get("requestContext", {}).get("authorizer") or {}).get("group")
        if group not in AUDIT_READERS:
            return api_response(
                403, {"message": f"audit access is restricted to {' and '.join(AUDIT_READERS)}"}
            )
        return api_response(
            200,
            {
                "entries": _list_audit(
                    tenant_id,
                    _requested_limit(query),
                    alert_id=query.get("alert_id"),
                    runbook_id=query.get("runbook_id"),
                )
            },
        )

    if resource == "/v1/diagnostics/{alertId}":
        diagnostic = _get_diagnostic(tenant_id, params["alertId"])
        if not diagnostic:
            return api_response(404, {"message": "not found"})
        return api_response(200, diagnostic)

    if resource == "/v1/runbooks":
        return api_response(
            200,
            {"runbooks": _list_runbooks(tenant_id, query.get("status"), _requested_limit(query))},
        )

    if resource == "/v1/runbooks/{runbookId}":
        runbook = _get_runbook(tenant_id, params["runbookId"])
        if not runbook:
            return api_response(404, {"message": "not found"})
        s3_key = runbook.get("s3_key")
        if s3_key:
            # Signed with a session sized to outlive the URL: a presigned URL
            # expires with the credentials behind it, so signing from the
            # 15-minute cached session could yield a URL good for seconds.
            s3 = tenant_scope.tenant_signing_s3_client(
                tenant_id, config.RUNBOOK_URL_TTL_SECONDS
            )
            runbook["download_url"] = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": config.RUNBOOKS_BUCKET, "Key": s3_key},
                ExpiresIn=config.RUNBOOK_URL_TTL_SECONDS,
            )
            runbook["download_url_expires_in"] = config.RUNBOOK_URL_TTL_SECONDS
        return api_response(200, runbook)

    if resource == "/v1/runbooks/{runbookId}/export":
        runbook = _get_runbook(tenant_id, params["runbookId"])
        if not runbook:
            return api_response(404, {"message": "not found"})
        # The point of export is that an automation platform gets the runbook
        # itself plus its structured steps. Returning only the DynamoDB metadata
        # row, as this used to, exported none of the runbook.
        diagnostic = _get_diagnostic(tenant_id, runbook.get("alert_id")) or {}
        body = {
            "runbook": runbook,
            "remediation_steps": diagnostic.get("remediation_steps", []),
            "rca_summary": diagnostic.get("rca_summary"),
        }
        s3_key = runbook.get("s3_key")
        if s3_key:
            try:
                body["markdown"] = _runbook_markdown(tenant_id, s3_key)
            except ClientError as exc:
                logger.error("cannot read runbook %s: %s", s3_key, exc)
                body["markdown"] = None
        return api_response(200, body)

    return api_response(404, {"message": "not found"})
