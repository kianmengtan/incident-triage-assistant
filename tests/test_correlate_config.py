from unittest.mock import MagicMock, patch

import correlate_config


def test_writes_to_tenant_scoped_s3_key():
    event = {
        "tenant_id": "acme",
        "alert_id": "alert-1",
        "alert": {"service": "checkout", "severity": "high", "description": "5xx spike"},
    }
    s3 = MagicMock()

    with patch.object(correlate_config, "_vcs_creds", return_value={}), patch.object(
        correlate_config.tenant_scope, "tenant_s3_client", return_value=s3
    ):
        result = correlate_config.handler(event, None)

    s3.put_object.assert_called_once()
    key = s3.put_object.call_args.kwargs["Key"]
    assert "tenant/acme/alert/" in key
    assert result["s3_key"] == key
