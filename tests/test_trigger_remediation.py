"""The safety-critical path: approving remediation runs real infrastructure
changes, so these tests are about exactly-once, auditing, and refusals."""
import json
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

import trigger_remediation

RUNBOOK = {
    "runbook_id": "rb-1",
    "s3_key": "tenant/acme/runbook/rb-1.md",
    "alert_id": "alert-1",
    "approval_status": "pending",
}


def _event(group, resource="/v1/runbooks/{runbookId}/approve", runbook_id="rb-1"):
    return {
        "resource": resource,
        "requestContext": {
            "authorizer": {"tenant_id": "acme", "group": group, "principalId": "user-1"}
        },
        "pathParameters": {"runbookId": runbook_id},
    }


def _conditional_failure():
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "no"}}, "UpdateItem"
    )


@pytest.fixture
def table():
    t = MagicMock()
    t.get_item.return_value = {"Item": dict(RUNBOOK)}
    return t


@pytest.fixture
def harness(table):
    with patch.object(trigger_remediation.tenant_scope, "tenant_dynamodb_resource") as resource, \
         patch.object(trigger_remediation.audit, "record_audit") as record_audit, \
         patch.object(trigger_remediation._lambda, "invoke") as invoke, \
         patch.object(trigger_remediation.integrations, "creds", return_value={}), \
         patch.object(trigger_remediation, "_rca_summary", return_value="because the pool ran out"):
        resource.return_value.Table.return_value = table
        yield {"table": table, "audit": record_audit, "invoke": invoke}


# ---------------------------------------------------------------- authorisation


def test_non_admin_gets_403_and_never_calls_remediation_platform(harness):
    with patch.object(trigger_remediation, "_call_remediation_platform") as call:
        resp = trigger_remediation.handler(_event("TenantEngineer"), None)

    assert resp["statusCode"] == 403
    call.assert_not_called()


def test_a_refused_attempt_is_audited(harness):
    """The UI tells the user a refused attempt is recorded. It has to be."""
    trigger_remediation.handler(_event("TenantEngineer"), None)

    harness["audit"].assert_called_once()
    assert harness["audit"].call_args.kwargs["result"] == "refused_not_admin"


def test_a_missing_tenant_context_is_forbidden(harness):
    event = _event("TenantAdmin")
    event["requestContext"]["authorizer"].pop("tenant_id")
    assert trigger_remediation.handler(event, None)["statusCode"] == 403


# ------------------------------------------------------------------ exactly once


def test_the_runbook_is_claimed_before_the_platform_is_called(harness):
    """Ordering is the whole guarantee: claim first, then execute.

    Calling the platform before the conditional write means two concurrent
    approvals both call it, and the infrastructure change happens twice.
    """
    order = []
    harness["table"].update_item.side_effect = lambda **kw: order.append("claim")

    with patch.object(
        trigger_remediation, "_call_remediation_platform",
        side_effect=lambda *a: order.append("execute") or {"status": "succeeded"},
    ):
        trigger_remediation.handler(_event("TenantAdmin"), None)

    assert order[0] == "claim", f"expected the claim first, got {order}"
    assert "execute" in order


def test_the_claim_is_conditional_on_the_runbook_still_being_pending(harness):
    with patch.object(
        trigger_remediation, "_call_remediation_platform", return_value={"status": "succeeded"}
    ):
        trigger_remediation.handler(_event("TenantAdmin"), None)

    claim = harness["table"].update_item.call_args_list[0].kwargs
    assert claim["ConditionExpression"] == "approval_status = :pending"
    assert claim["ExpressionAttributeValues"][":approved"] == "approved"


def test_a_second_approval_is_a_409_and_does_not_execute_again(harness):
    harness["table"].update_item.side_effect = _conditional_failure()

    with patch.object(trigger_remediation, "_call_remediation_platform") as call:
        resp = trigger_remediation.handler(_event("TenantAdmin"), None)

    assert resp["statusCode"] == 409
    call.assert_not_called()


# --------------------------------------------------------------------- auditing


def test_an_attempt_is_audited_before_the_call_and_the_outcome_after(harness):
    """So an execution can never happen with no trace, even if this dies."""
    with patch.object(
        trigger_remediation, "_call_remediation_platform", return_value={"status": "succeeded"}
    ):
        trigger_remediation.handler(_event("TenantAdmin"), None)

    results = [c.kwargs["result"] for c in harness["audit"].call_args_list]
    assert results == ["attempted", "succeeded"]


def test_a_failed_execution_is_audited_as_failed(harness):
    with patch.object(
        trigger_remediation,
        "_call_remediation_platform",
        return_value={"status": "failed", "error": "TimeoutError"},
    ):
        resp = trigger_remediation.handler(_event("TenantAdmin"), None)

    assert json.loads(resp["body"])["execution_status"] == "failed"
    assert [c.kwargs["result"] for c in harness["audit"].call_args_list][-1] == "failed"


# ------------------------------------------------- approval vs execution status


def test_approval_status_and_execution_status_are_recorded_separately(harness):
    """approval_status used to be set to the execution result, so a tenant with
    no remediation platform configured had "skipped" written into the field that
    records the human decision."""
    with patch.object(
        trigger_remediation,
        "_call_remediation_platform",
        return_value={"status": "skipped", "reason": "not_configured"},
    ):
        resp = trigger_remediation.handler(_event("TenantAdmin"), None)

    body = json.loads(resp["body"])
    assert body["approval_status"] == "approved"
    assert body["execution_status"] == "skipped"

    claim, outcome = harness["table"].update_item.call_args_list
    assert claim.kwargs["ExpressionAttributeValues"][":approved"] == "approved"
    assert outcome.kwargs["ExpressionAttributeValues"][":s"] == "skipped"
    assert "approval_status" not in outcome.kwargs["UpdateExpression"]


# ------------------------------------------------------------------------ decline


def test_declining_records_the_decision_without_executing(harness):
    with patch.object(trigger_remediation, "_call_remediation_platform") as call:
        resp = trigger_remediation.handler(
            _event("TenantAdmin", resource="/v1/runbooks/{runbookId}/decline"), None
        )

    assert json.loads(resp["body"])["approval_status"] == "declined"
    call.assert_not_called()
    assert harness["audit"].call_args.kwargs["result"] == "declined"


def test_declining_an_already_decided_runbook_is_a_409(harness):
    harness["table"].update_item.side_effect = _conditional_failure()
    resp = trigger_remediation.handler(
        _event("TenantAdmin", resource="/v1/runbooks/{runbookId}/decline"), None
    )
    assert resp["statusCode"] == 409


# --------------------------------------------------------------------------- IMS


def test_the_ims_notification_carries_the_rca_summary(harness):
    """It read event["rca_summary"], which this function never sent, so every
    notification went out with a null summary."""
    with patch.object(
        trigger_remediation, "_call_remediation_platform", return_value={"status": "succeeded"}
    ):
        trigger_remediation.handler(_event("TenantAdmin"), None)

    payload = json.loads(harness["invoke"].call_args.kwargs["Payload"])
    assert payload["rca_summary"] == "because the pool ran out"
    assert payload["runbook_id"] == "rb-1"


def test_a_missing_runbook_is_a_404(harness):
    harness["table"].get_item.return_value = {}
    assert trigger_remediation.handler(_event("TenantAdmin"), None)["statusCode"] == 404


# ---------------------------------------------------------------------- SSRF

def test_the_remediation_endpoint_goes_through_the_ssrf_guard():
    """A tenant configures this URL themselves, so it cannot be fetched raw."""
    with patch.object(
        trigger_remediation.http,
        "request_json",
        side_effect=trigger_remediation.http.EndpointNotAllowed("private address"),
    ):
        result = trigger_remediation._call_remediation_platform(
            {"endpoint": "https://169.254.169.254/latest/meta-data/"}, RUNBOOK
        )

    assert result["status"] == "failed"
    assert result["error"] == "endpoint not permitted"


def test_no_configured_platform_is_skipped_not_failed():
    assert trigger_remediation._call_remediation_platform({}, RUNBOOK)["status"] == "skipped"
