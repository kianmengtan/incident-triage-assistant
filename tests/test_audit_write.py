from unittest.mock import MagicMock, patch

import audit_write


def test_audit_record_includes_required_fields():
    table = MagicMock()
    event = {
        "tenant_id": "acme",
        "actor": "user-1",
        "action": "remediation.approve",
        "result": "success",
        "alert_id": "alert-1",
        "runbook_id": "rb-1",
    }

    with patch.object(audit_write.tenant_scope, "tenant_dynamodb_resource") as mock_resource:
        mock_resource.return_value.Table.return_value = table
        audit_write.handler(event, None)

    table.put_item.assert_called_once()
    item = table.put_item.call_args.kwargs["Item"]
    for key in ("tenant_id", "actor", "action", "result"):
        assert key in item
