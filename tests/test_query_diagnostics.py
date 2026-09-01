"""The read API. Every query is scoped to the authorizer's tenant_id."""
import json
from unittest.mock import MagicMock, patch

import pytest
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError
from cryptography.fernet import InvalidToken

import query_diagnostics
from common import config

STEPS = [{"text": "Raise DB_POOL_MAX", "priority": "P1", "command": "kubectl ...", "reversible": True}]


def _event(resource, params=None, query=None, tenant_id="acme"):
    return {
        "resource": resource,
        "pathParameters": params or {},
        "queryStringParameters": query,
        "requestContext": {"authorizer": {"tenant_id": tenant_id} if tenant_id else {}},
    }


@pytest.fixture
def table():
    t = MagicMock()
    t.query.return_value = {"Items": []}
    return t


@pytest.fixture
def harness(table):
    s3 = MagicMock()
    s3.generate_presigned_url.return_value = "https://example.com/signed"
    with patch.object(query_diagnostics.tenant_scope, "tenant_dynamodb_resource") as resource, \
         patch.object(query_diagnostics.tenant_scope, "tenant_s3_client", return_value=s3), \
         patch.object(query_diagnostics.tenant_scope, "tenant_signing_s3_client", return_value=s3), \
         patch.object(query_diagnostics.crypto, "decrypt_field", side_effect=lambda t, v: v):
        resource.return_value.Table.return_value = table
        yield {"table": table, "s3": s3}


def test_a_missing_tenant_context_is_forbidden(harness):
    assert query_diagnostics.handler(_event("/v1/runbooks", tenant_id=None), None)["statusCode"] == 403


def test_the_diagnostic_query_uses_the_authorizer_tenant_never_client_input(harness):
    harness["table"].get_item.return_value = {
        "Item": {"tenant_id": "acme", "rca_summary": "cause", "remediation_steps": json.dumps(STEPS)}
    }
    query_diagnostics.handler(_event("/v1/diagnostics/{alertId}", {"alertId": "alert-1"}), None)

    assert harness["table"].get_item.call_args.kwargs["Key"] == {
        "tenant_id": "acme", "sk": "diag#alert-1"
    }


def test_structured_steps_are_returned_as_objects(harness):
    harness["table"].get_item.return_value = {
        "Item": {"rca_summary": "cause", "remediation_steps": json.dumps(STEPS)}
    }
    resp = query_diagnostics.handler(_event("/v1/diagnostics/{alertId}", {"alertId": "a"}), None)

    assert json.loads(resp["body"])["remediation_steps"][0]["priority"] == "P1"


def test_rows_written_in_the_old_newline_format_are_still_readable(harness):
    """So an existing deployment's diagnostics do not become unreadable."""
    harness["table"].get_item.return_value = {
        "Item": {"rca_summary": "cause", "remediation_steps": "step one\nstep two"}
    }
    resp = query_diagnostics.handler(_event("/v1/diagnostics/{alertId}", {"alertId": "a"}), None)

    steps = json.loads(resp["body"])["remediation_steps"]
    assert [s["text"] for s in steps] == ["step one", "step two"]


def test_an_undecryptable_field_degrades_instead_of_500ing(harness):
    harness["table"].get_item.return_value = {"Item": {"rca_summary": "gibberish"}}
    with patch.object(query_diagnostics.crypto, "decrypt_field", side_effect=InvalidToken()):
        resp = query_diagnostics.handler(_event("/v1/diagnostics/{alertId}", {"alertId": "a"}), None)

    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["rca_summary"] is None


def test_a_missing_diagnostic_is_404(harness):
    harness["table"].get_item.return_value = {}
    assert query_diagnostics.handler(_event("/v1/diagnostics/{alertId}", {"alertId": "a"}), None)["statusCode"] == 404


def test_the_runbook_list_follows_pagination(harness):
    """One query returned at most 1 MB and the rest was dropped with a 200."""
    harness["table"].query.side_effect = [
        {"Items": [{"runbook_id": str(i)} for i in range(30)], "LastEvaluatedKey": {"sk": "x"}},
        {"Items": [{"runbook_id": str(i)} for i in range(30, 45)]},
    ]
    resp = query_diagnostics.handler(_event("/v1/runbooks"), None)

    assert harness["table"].query.call_count == 2
    assert len(json.loads(resp["body"])["runbooks"]) == 45


def test_pagination_stops_at_the_page_size(harness):
    harness["table"].query.return_value = {
        "Items": [{"runbook_id": str(i)} for i in range(60)], "LastEvaluatedKey": {"sk": "x"}
    }
    resp = query_diagnostics.handler(_event("/v1/runbooks"), None)
    assert len(json.loads(resp["body"])["runbooks"]) == query_diagnostics.PAGE_SIZE


def test_a_status_filter_uses_the_index(harness):
    query_diagnostics.handler(_event("/v1/runbooks", query={"status": "ready"}), None)
    assert harness["table"].query.call_args.kwargs["IndexName"] == "status-index"


def test_the_presigned_url_is_signed_by_a_session_that_outlives_it(harness):
    harness["table"].get_item.return_value = {"Item": {"s3_key": "tenant/acme/runbook/rb-1.md"}}
    with patch.object(
        query_diagnostics.tenant_scope, "tenant_signing_s3_client", return_value=harness["s3"]
    ) as signing:
        resp = query_diagnostics.handler(_event("/v1/runbooks/{runbookId}", {"runbookId": "rb-1"}), None)

    assert signing.call_args.args[1] == config.RUNBOOK_URL_TTL_SECONDS
    body = json.loads(resp["body"])
    assert body["download_url_expires_in"] == config.RUNBOOK_URL_TTL_SECONDS


def test_a_runbook_row_without_an_s3_key_does_not_crash(harness):
    harness["table"].get_item.return_value = {"Item": {"runbook_id": "rb-1"}}
    resp = query_diagnostics.handler(_event("/v1/runbooks/{runbookId}", {"runbookId": "rb-1"}), None)
    assert resp["statusCode"] == 200
    assert "download_url" not in json.loads(resp["body"])


def test_export_returns_the_runbook_itself_not_only_its_metadata(harness):
    """It used to return the DynamoDB row, so the export contained none of the
    runbook the caller asked to export."""
    harness["table"].get_item.side_effect = [
        {"Item": {"s3_key": "tenant/acme/runbook/rb-1.md", "alert_id": "alert-1"}},
        {"Item": {"rca_summary": "cause", "remediation_steps": json.dumps(STEPS)}},
    ]
    harness["s3"].get_object.return_value = {"Body": MagicMock(read=lambda: b"# Runbook\n1. do it")}

    resp = query_diagnostics.handler(
        _event("/v1/runbooks/{runbookId}/export", {"runbookId": "rb-1"}), None
    )

    body = json.loads(resp["body"])
    assert body["markdown"] == "# Runbook\n1. do it"
    assert body["remediation_steps"][0]["priority"] == "P1"
    assert body["rca_summary"] == "cause"


def test_export_survives_an_unreadable_s3_object(harness):
    harness["table"].get_item.side_effect = [
        {"Item": {"s3_key": "tenant/acme/runbook/rb-1.md", "alert_id": "a"}},
        {},
    ]
    harness["s3"].get_object.side_effect = ClientError(
        {"Error": {"Code": "NoSuchKey", "Message": "x"}}, "GetObject"
    )
    resp = query_diagnostics.handler(
        _event("/v1/runbooks/{runbookId}/export", {"runbookId": "rb-1"}), None
    )
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["markdown"] is None


def test_an_unknown_route_is_404(harness):
    assert query_diagnostics.handler(_event("/v1/nope"), None)["statusCode"] == 404


# ---------------------------------------------------------------------------
# Routes that were implemented but unreachable until the template wired them.
# ---------------------------------------------------------------------------
def test_the_incident_list_is_ordered_by_the_received_at_index(harness):
    """Newest first. The base sort key is alert#{alert_id}, which deduplicates
    correctly but orders lexicographically by id, so chronological order can only
    come from the GSI."""
    harness["table"].query.return_value = {"Items": [{"alert_id": "alert-1"}]}
    resp = query_diagnostics.handler(_event("/v1/alerts"), None)

    kwargs = harness["table"].query.call_args.kwargs
    assert kwargs["IndexName"] == "received-at-index"
    assert kwargs["ScanIndexForward"] is False
    assert json.loads(resp["body"])["alerts"] == [{"alert_id": "alert-1"}]


def test_audit_access_is_refused_to_an_engineer(harness):
    event = _event("/v1/audit")
    event["requestContext"]["authorizer"]["group"] = "TenantEngineer"
    assert query_diagnostics.handler(event, None)["statusCode"] == 403


def test_audit_access_is_allowed_to_leadership(harness):
    harness["table"].query.return_value = {"Items": [{"entry_id": "e1"}]}
    event = _event("/v1/audit")
    event["requestContext"]["authorizer"]["group"] = "TenantLeadership"
    resp = query_diagnostics.handler(event, None)

    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["entries"] == [{"entry_id": "e1"}]


def test_an_audit_filter_is_applied_by_dynamodb_not_after_the_page_limit(harness):
    """Filtering in Python after truncating to `limit` meant ?alert_id=X returned
    nothing whenever X was not among the newest 50 records tenant-wide -- which is
    every incident but the latest one."""
    harness["table"].query.return_value = {"Items": [{"entry_id": "e1", "alert_id": "alert-9"}]}
    event = _event("/v1/audit", query={"alert_id": "alert-9"})
    event["requestContext"]["authorizer"]["group"] = "TenantAdmin"
    query_diagnostics.handler(event, None)

    kwargs = harness["table"].query.call_args.kwargs
    assert kwargs["FilterExpression"] == Attr("alert_id").eq("alert-9")


def test_an_unfiltered_audit_query_sends_no_filter_expression(harness):
    event = _event("/v1/audit")
    event["requestContext"]["authorizer"]["group"] = "TenantAdmin"
    query_diagnostics.handler(event, None)
    assert "FilterExpression" not in harness["table"].query.call_args.kwargs


# ---------------------------------------------------------------------------
# Pipeline status
# ---------------------------------------------------------------------------
def _status_tables(alert, diagnostic=None, runbook_pages=None):
    """Alerts, Diagnostics and Runbooks as three distinct table mocks."""
    alerts, diagnostics, runbooks = MagicMock(), MagicMock(), MagicMock()
    alerts.get_item.return_value = {"Item": alert} if alert else {}
    diagnostics.get_item.return_value = {"Item": diagnostic} if diagnostic else {}
    runbooks.query.side_effect = runbook_pages or [{"Items": []}]
    by_name = {
        config.ALERTS_TABLE: alerts,
        config.DIAGNOSTICS_TABLE: diagnostics,
        config.RUNBOOKS_TABLE: runbooks,
    }
    return by_name, runbooks


def _status(by_name, alert_id="alert-1"):
    with patch.object(query_diagnostics.tenant_scope, "tenant_dynamodb_resource") as resource:
        resource.return_value.Table.side_effect = lambda name: by_name[name]
        resp = query_diagnostics.handler(
            _event("/v1/alerts/{alertId}/status", {"alertId": alert_id}), None
        )
    return resp


def test_a_missing_alert_status_is_404():
    by_name, _ = _status_tables(None)
    assert _status(by_name)["statusCode"] == 404


def test_a_dispatched_alert_with_no_diagnostic_yet_is_diagnosing():
    by_name, _ = _status_tables({"received_at": 100, "dispatched_at": 101})
    body = json.loads(_status(by_name)["body"])
    assert body["state"] == "diagnosing"
    assert body["stage_order"] == list(query_diagnostics.progress.STAGE_ORDER)
    assert body["sla_budget_seconds"] == config.RUNBOOK_SLA_SECONDS


def test_a_failed_pipeline_reports_failed():
    """Nothing used to write progress.FAILED, so this state was unreachable and a
    failed diagnosis showed as "diagnosing" forever."""
    by_name, _ = _status_tables(
        {"received_at": 100, "dispatched_at": 101, "pipeline_stage": query_diagnostics.progress.FAILED}
    )
    assert json.loads(_status(by_name)["body"])["state"] == "failed"


def test_the_runbook_lookup_follows_pagination():
    """A bare Query stops at DynamoDB's 1 MB page. The alert's runbook could sit
    past it, and the state then regressed to "diagnosed" with a null runbook_id
    even though the runbook was ready.

    A FilterExpression is applied after the read, so an early page legitimately
    comes back empty with more behind it -- which is exactly the case a single
    unpaginated Query gets wrong.
    """
    by_name, runbooks = _status_tables(
        {"received_at": 100, "dispatched_at": 101},
        diagnostic={"confidence": "high"},
        runbook_pages=[
            {"Items": [], "LastEvaluatedKey": {"sk": "s"}},
            {"Items": [{"alert_id": "alert-1", "runbook_id": "rb-1", "approval_status": "pending"}]},
        ],
    )
    body = json.loads(_status(by_name)["body"])

    assert runbooks.query.call_count == 2
    assert body["state"] == "runbook_ready"
    assert body["runbook_id"] == "rb-1"
    assert body["approval_status"] == "pending"


def test_the_runbook_lookup_filters_on_the_alert_in_dynamodb():
    by_name, runbooks = _status_tables({"received_at": 100, "dispatched_at": 101})
    _status(by_name)
    kwargs = runbooks.query.call_args.kwargs
    assert kwargs["IndexName"] == "status-index"
    assert kwargs["FilterExpression"] == Attr("alert_id").eq("alert-1")
    # Not 1: a filter is applied after the read, so a page size of 1 would examine
    # one runbook per round trip and give up after MAX_PAGES of them.
    assert kwargs["Limit"] == query_diagnostics.PAGE_SIZE
