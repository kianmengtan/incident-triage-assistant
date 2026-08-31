import json
from unittest.mock import patch

import notify_ims


def test_ims_failure_is_caught_and_logged_not_raised():
    event = {"tenant_id": "acme", "alert_id": "alert-1", "runbook_id": "rb-1"}
    creds = {"ims": {"endpoint": "https://ims.example.com/webhook", "api_key": "k"}}

    with patch.object(
        notify_ims._secrets,
        "get_secret_value",
        return_value={"SecretString": json.dumps(creds)},
    ):
        with patch.object(notify_ims.urllib.request, "urlopen", side_effect=Exception("boom")):
            result = notify_ims.handler(event, None)  # must not raise

    assert result["notified"] is False
    assert "boom" in result["reason"]


def test_skips_when_not_configured():
    event = {"tenant_id": "acme"}
    with patch.object(
        notify_ims._secrets,
        "get_secret_value",
        return_value={"SecretString": json.dumps({})},
    ):
        result = notify_ims.handler(event, None)

    assert result == {"notified": False, "reason": "not_configured"}
