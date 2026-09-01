import json
from unittest.mock import patch

import s3vectors_async_setup


def _sns_event(message):
    return {"Records": [{"Sns": {"Message": json.dumps(message)}}]}


def _lambda_payload(body):
    class _Payload:
        def read(self):
            return json.dumps(body).encode("utf-8")

    return _Payload()


def test_invokes_setup_function_on_sns_message():
    with patch.object(s3vectors_async_setup._lambda, "invoke") as mock_invoke:
        mock_invoke.return_value = {"Payload": _lambda_payload({"action": "create"})}
        result = s3vectors_async_setup.handler(_sns_event({"action": "create"}), None)

    mock_invoke.assert_called_once()
    assert mock_invoke.call_args.kwargs["FunctionName"] == s3vectors_async_setup.config.S3VECTORS_SETUP_FUNCTION_NAME
    assert mock_invoke.call_args.kwargs["InvocationType"] == "RequestResponse"
    assert result["results"] == [{"action": "create"}]


def test_raises_when_setup_function_errors():
    with patch.object(s3vectors_async_setup._lambda, "invoke") as mock_invoke:
        mock_invoke.return_value = {
            "Payload": _lambda_payload({"errorMessage": "boom"}),
            "FunctionError": "Unhandled",
        }
        try:
            s3vectors_async_setup.handler(_sns_event({"action": "create"}), None)
        except RuntimeError as exc:
            assert "boom" in str(exc)
        else:
            raise AssertionError("expected RuntimeError")


def test_defaults_to_create_when_message_missing_action():
    with patch.object(s3vectors_async_setup._lambda, "invoke") as mock_invoke:
        mock_invoke.return_value = {"Payload": _lambda_payload({"action": "create"})}
        s3vectors_async_setup.handler(_sns_event({}), None)

    sent_payload = json.loads(mock_invoke.call_args.kwargs["Payload"])
    assert sent_payload == {"action": "create"}
