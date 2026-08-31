from unittest.mock import MagicMock, patch

import generate_runbook


def test_s3_put_prefix_and_runbook_defaults_pending():
    event = {
        "tenant_id": "acme",
        "alert_id": "alert-1",
        "alert": {"service": "checkout"},
        "diagnostic": {
            "diagnostic_id": "diag-1",
            "rca_summary": "root cause",
            "remediation_steps": ["restart pod"],
        },
    }

    s3 = MagicMock()
    table = MagicMock()

    with patch.object(generate_runbook.bedrock, "generate_text", return_value="# Runbook"):
        with patch.object(generate_runbook.tenant_scope, "tenant_s3_client", return_value=s3):
            with patch.object(
                generate_runbook.tenant_scope, "tenant_dynamodb_resource"
            ) as mock_resource:
                mock_resource.return_value.Table.return_value = table
                result = generate_runbook.handler(event, None)

    s3.put_object.assert_called_once()
    assert "tenant/acme/runbook/" in s3.put_object.call_args.kwargs["Key"]

    table.put_item.assert_called_once()
    item = table.put_item.call_args.kwargs["Item"]
    assert item["approval_status"] == "pending"
    assert item["runbook_id"] == result["runbook_id"]
