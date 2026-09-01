"""Runbook-ready notification and IMS push."""
import json
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

import notify
import notify_ims

EVENT = {"tenant_id": "acme", "alert_id": "alert-1", "runbook": {"runbook_id": "rb-1"}}


def test_the_notification_is_published_with_a_tenant_attribute():
    with patch.object(notify.progress, "mark_stage"), patch.object(notify._sns, "publish") as publish:
        assert notify.handler(EVENT, None)["notified"] is True

    kwargs = publish.call_args.kwargs
    assert json.loads(kwargs["Message"])["runbook_id"] == "rb-1"
    assert kwargs["MessageAttributes"]["tenant_id"]["StringValue"] == "acme"


def test_a_publish_failure_does_not_fail_the_pipeline():
    """The runbook is already stored and approvable by this point, so failing the
    execution here would send a complete diagnosis to the DLQ."""
    with patch.object(notify.progress, "mark_stage"), patch.object(
        notify._sns, "publish",
        side_effect=ClientError({"Error": {"Code": "InternalError", "Message": "x"}}, "Publish"),
    ):
        result = notify.handler(EVENT, None)

    assert result["notified"] is False
    assert result["reason"] == "InternalError"


IMS_EVENT = {
    "tenant_id": "acme",
    "alert_id": "alert-1",
    "runbook_id": "rb-1",
    "runbook_link": "tenant/acme/runbook/rb-1.md",
    "rca_summary": "pool exhausted",
}


def test_no_configured_ims_is_skipped():
    with patch.object(notify_ims.integrations, "creds", return_value={}):
        assert notify_ims.handler(IMS_EVENT, None) == {"notified": False, "reason": "not_configured"}


def test_the_findings_are_included_in_the_push():
    with patch.object(notify_ims.integrations, "creds", return_value={"endpoint": "https://ims.example.com/i", "api_key": "k"}), \
         patch.object(notify_ims.http, "request_json", return_value={}) as request:
        assert notify_ims.handler(IMS_EVENT, None)["notified"] is True

    assert request.call_args.kwargs["payload"]["rca_summary"] == "pool exhausted"


def test_a_failure_is_caught_and_never_raised():
    with patch.object(notify_ims.integrations, "creds", return_value={"endpoint": "https://ims.example.com/i"}), \
         patch.object(notify_ims.http, "request_json", side_effect=TimeoutError("slow")):
        result = notify_ims.handler(IMS_EVENT, None)

    assert result == {"notified": False, "reason": "TimeoutError"}


def test_a_refused_endpoint_is_reported_distinctly():
    with patch.object(notify_ims.integrations, "creds", return_value={"endpoint": "https://127.0.0.1/i"}), \
         patch.object(notify_ims.http, "request_json", side_effect=notify_ims.http.EndpointNotAllowed("loopback")):
        assert notify_ims.handler(IMS_EVENT, None)["reason"] == "endpoint_not_permitted"


# ---------------------------------------------------------------------------
# The failure path
# ---------------------------------------------------------------------------
FAILURE_EVENT = {
    "tenant_id": "acme",
    "alert_id": "alert-1",
    "error": {"Error": "InvalidModelJson", "Cause": "no rca_summary"},
}


def test_a_failed_pipeline_is_recorded_on_the_alert():
    """Nothing used to write progress.FAILED, so a failed diagnosis was
    indistinguishable from one still running: the console showed "diagnosing"
    forever and the read API's "failed" state was unreachable."""
    with patch.object(notify.progress, "mark_stage") as mark_stage:
        result = notify.failure_handler(FAILURE_EVENT, None)

    mark_stage.assert_called_once_with("acme", "alert-1", notify.progress.FAILED)
    assert result["stage"] == notify.progress.FAILED
    assert result["error"] == "InvalidModelJson"


def test_recording_the_failure_never_raises_into_the_failure_path():
    """This runs inside the catch handler. Raising here would replace the real
    error with this one and skip the DLQ send that preserves the state."""
    with patch.object(notify.progress, "mark_stage", side_effect=RuntimeError("ddb down")):
        assert notify.failure_handler(FAILURE_EVENT, None)["recorded"] is False


def test_the_failure_handler_tolerates_a_state_with_no_error_key():
    with patch.object(notify.progress, "mark_stage"):
        result = notify.failure_handler({"tenant_id": "acme", "alert_id": "a"}, None)
    assert result["error"] is None


# ---------------------------------------------------------------------------
# A timed-out execution. No Catch inside the state machine can see one, so
# EventBridge delivers it instead -- with the alert ids in the execution input.
# ---------------------------------------------------------------------------
def _timed_out_event(status="TIMED_OUT", payload=None):
    return {
        "source": "aws.states",
        "detail-type": "Step Functions Execution Status Change",
        "detail": {
            "status": status,
            "executionArn": "arn:aws:states:ap-southeast-1:123456789012:execution:sfn:e1",
            "input": json.dumps(
                {"tenant_id": "acme", "alert_id": "alert-1"} if payload is None else payload
            ),
        },
    }


def test_a_timed_out_execution_marks_the_alert_failed():
    """The 300s SLA guard kills the execution without running any Catch, so this
    is the only path that records the outcome of the case NFR-01 cares most
    about."""
    with patch.object(notify.progress, "mark_stage") as mark_stage:
        result = notify.failure_handler(_timed_out_event(), None)

    mark_stage.assert_called_once_with("acme", "alert-1", notify.progress.FAILED)
    assert result["error"] == "States.TimedOut"


def test_an_aborted_execution_is_recorded_too():
    with patch.object(notify.progress, "mark_stage") as mark_stage:
        assert notify.failure_handler(_timed_out_event("ABORTED"), None)["error"] == "States.Aborted"
    assert mark_stage.call_args.args[2] == notify.progress.FAILED


def test_an_execution_input_that_is_not_usable_json_does_not_raise():
    event = _timed_out_event()
    event["detail"]["input"] = "{not json"
    with patch.object(notify.progress, "mark_stage") as mark_stage:
        notify.failure_handler(event, None)
    # No ids to mark, so mark_stage short-circuits rather than the handler failing.
    mark_stage.assert_called_once_with(None, None, notify.progress.FAILED)


def test_the_state_machine_shape_is_still_understood():
    """Both callers reach the same handler, so neither shape may shadow the other."""
    with patch.object(notify.progress, "mark_stage") as mark_stage:
        notify.failure_handler(FAILURE_EVENT, None)
    mark_stage.assert_called_once_with("acme", "alert-1", notify.progress.FAILED)
