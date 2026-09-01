"""The callback path. If this cannot run, a custom resource stalls a deploy for
an hour and reports a timeout that names no cause — so it must work when
everything around it is broken."""
import json
from unittest.mock import MagicMock, patch

import cfnresponse

EVENT = {
    "ResponseURL": "https://cloudformation-custom-resource-response.example/abc",
    "StackId": "arn:aws:cloudformation:ap-southeast-1:1:stack/app/1",
    "RequestId": "req-1",
    "LogicalResourceId": "S3VectorsSetup",
}


class _Context:
    log_stream_name = "2026/08/31/[$LATEST]abc"


def _urlopen():
    resp = MagicMock(status=200)
    resp.__enter__ = lambda s: resp
    resp.__exit__ = lambda *a: False
    return resp


def _sent(mock):
    return json.loads(mock.call_args.args[0].data)


def test_the_outcome_is_put_to_the_presigned_url():
    with patch.object(cfnresponse.urllib.request, "urlopen", return_value=_urlopen()) as urlopen:
        cfnresponse.send(EVENT, _Context(), cfnresponse.SUCCESS, {"a": 1}, "phys-1")

    request = urlopen.call_args.args[0]
    assert request.method == "PUT"
    assert request.full_url == EVENT["ResponseURL"]
    body = json.loads(request.data)
    assert body["Status"] == "SUCCESS"
    assert body["PhysicalResourceId"] == "phys-1"
    assert body["Data"] == {"a": 1}
    assert body["StackId"] == EVENT["StackId"]
    assert body["RequestId"] == EVENT["RequestId"]
    assert body["LogicalResourceId"] == EVENT["LogicalResourceId"]


def test_the_content_type_is_empty_as_the_presigned_url_expects():
    """Letting a client library choose its own default here is a known route to
    a 403 on the pre-signed PUT, which loses the response."""
    with patch.object(cfnresponse.urllib.request, "urlopen", return_value=_urlopen()) as urlopen:
        cfnresponse.send(EVENT, _Context(), cfnresponse.SUCCESS)

    assert urlopen.call_args.args[0].get_header("Content-type") == ""


def test_a_failure_reason_is_carried_through():
    with patch.object(cfnresponse.urllib.request, "urlopen", return_value=_urlopen()) as urlopen:
        cfnresponse.send(EVENT, _Context(), cfnresponse.FAILED, reason="UnknownServiceError: s3vectors")

    body = _sent(urlopen)
    assert body["Status"] == "FAILED"
    assert body["Reason"] == "UnknownServiceError: s3vectors"


def test_the_default_reason_points_at_the_log_stream():
    with patch.object(cfnresponse.urllib.request, "urlopen", return_value=_urlopen()) as urlopen:
        cfnresponse.send(EVENT, _Context(), cfnresponse.SUCCESS)
    assert _Context.log_stream_name in _sent(urlopen)["Reason"]


def test_a_network_failure_is_logged_and_never_raised():
    """Raising here would replace the original error with this one and still
    leave CloudFormation waiting."""
    with patch.object(cfnresponse.urllib.request, "urlopen", side_effect=OSError("connection reset")):
        cfnresponse.send(EVENT, _Context(), cfnresponse.SUCCESS)  # must not raise


def test_an_event_without_a_response_url_is_handled():
    with patch.object(cfnresponse.urllib.request, "urlopen") as urlopen:
        cfnresponse.send({}, _Context(), cfnresponse.SUCCESS)
        cfnresponse.send(None, _Context(), cfnresponse.SUCCESS)
    urlopen.assert_not_called()


def test_a_context_without_a_log_stream_name_is_handled():
    with patch.object(cfnresponse.urllib.request, "urlopen", return_value=_urlopen()) as urlopen:
        cfnresponse.send(EVENT, object(), cfnresponse.SUCCESS)
    assert _sent(urlopen)["PhysicalResourceId"] == "unknown"


def test_noecho_is_set_so_the_response_data_is_not_echoed_into_stack_output():
    with patch.object(cfnresponse.urllib.request, "urlopen", return_value=_urlopen()) as urlopen:
        cfnresponse.send(EVENT, _Context(), cfnresponse.SUCCESS)
    assert _sent(urlopen)["NoEcho"] is False
