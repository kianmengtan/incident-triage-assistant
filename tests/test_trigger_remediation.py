from unittest.mock import MagicMock, patch

import trigger_remediation


def _event(group, runbook_id="rb-1"):
    return {
        "requestContext": {"authorizer": {"tenant_id": "acme", "group": group, "principalId": "user-1"}},
        "pathParameters": {"runbookId": runbook_id},
    }


def test_non_admin_gets_403_and_never_calls_remediation_platform():
    with patch.object(trigger_remediation, "_call_remediation_platform") as mock_call:
        resp = trigger_remediation.handler(_event("TenantEngineer"), None)

    assert resp["statusCode"] == 403
    mock_call.assert_not_called()


def test_admin_approval_invokes_remediation_and_writes_audit():
    table = MagicMock()
    table.get_item.return_value = {
        "Item": {"runbook_id": "rb-1", "s3_key": "tenant/acme/runbook/rb-1.md", "alert_id": "alert-1"}
    }

    with patch.object(trigger_remediation.tenant_scope, "tenant_dynamodb_resource") as mock_resource:
        mock_resource.return_value.Table.return_value = table
        with patch.object(
            trigger_remediation, "_call_remediation_platform", return_value={"status": "success"}
        ):
            with patch.object(trigger_remediation.audit, "record_audit") as mock_audit:
                with patch.object(trigger_remediation._lambda, "invoke") as mock_invoke:
                    resp = trigger_remediation.handler(_event("TenantAdmin"), None)

    assert resp["statusCode"] == 200
    mock_audit.assert_called_once()
    assert mock_audit.call_args.kwargs["result"] == "success"
    mock_invoke.assert_called_once()
