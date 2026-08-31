from unittest.mock import patch

import notify
from common import config


def test_publishes_to_runbook_ready_topic():
    event = {"tenant_id": "acme", "alert_id": "alert-1", "runbook": {"runbook_id": "rb-1"}}

    with patch.object(notify._sns, "publish") as mock_publish:
        notify.handler(event, None)

    mock_publish.assert_called_once()
    assert mock_publish.call_args.kwargs["TopicArn"] == config.RUNBOOK_READY_TOPIC_ARN
