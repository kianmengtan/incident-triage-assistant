from unittest.mock import patch

import s3vectors_setup


def test_direct_invocation_creates_bucket_and_index():
    with patch.object(s3vectors_setup, "_ensure_bucket") as mock_bucket:
        with patch.object(s3vectors_setup, "_ensure_index") as mock_index:
            result = s3vectors_setup.handler({"action": "create"}, None)

    mock_bucket.assert_called_once()
    mock_index.assert_called_once()
    assert result["action"] == "create"


def test_direct_invocation_defaults_to_create():
    with patch.object(s3vectors_setup, "_ensure_bucket") as mock_bucket:
        with patch.object(s3vectors_setup, "_ensure_index") as mock_index:
            s3vectors_setup.handler({}, None)

    mock_bucket.assert_called_once()
    mock_index.assert_called_once()


def test_direct_invocation_delete():
    with patch.object(s3vectors_setup, "_delete") as mock_delete:
        result = s3vectors_setup.handler({"action": "delete"}, None)

    mock_delete.assert_called_once()
    assert result["action"] == "delete"


def test_cfn_custom_resource_event_still_uses_cfnresponse():
    event = {"RequestType": "Create", "ResponseURL": "https://example.com/cfn"}

    with patch.object(s3vectors_setup, "_ensure_bucket") as mock_bucket:
        with patch.object(s3vectors_setup, "_ensure_index") as mock_index:
            with patch.object(s3vectors_setup.cfnresponse, "send") as mock_send:
                result = s3vectors_setup.handler(event, None)

    mock_bucket.assert_called_once()
    mock_index.assert_called_once()
    mock_send.assert_called_once()
    assert mock_send.call_args[0][2] == s3vectors_setup.cfnresponse.SUCCESS
    assert result is None
