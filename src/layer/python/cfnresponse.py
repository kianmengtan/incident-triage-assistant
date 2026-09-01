"""Signalling CloudFormation from a custom-resource Lambda.

Uses nothing but the standard library, deliberately. This module is the only way
a custom resource can tell CloudFormation it finished, so anything it imports
becomes a way for the response to never be sent — and a custom resource that
sends no response does not fail fast, it stalls the deploy until CloudFormation
gives up (an hour, by default).

It used to import urllib3, which is not declared in the layer's requirements and
was present only as a transitive dependency of boto3.
"""
import json
import urllib.request

SUCCESS = "SUCCESS"
FAILED = "FAILED"


def send(event, context, response_status, response_data=None, physical_resource_id=None, reason=None):
    """PUT the outcome to the pre-signed URL CloudFormation supplied.

    Never raises: a failure to report is logged, because there is nothing useful
    left to do with an exception here and raising would mask the original error.
    """
    response_url = (event or {}).get("ResponseURL")
    if not response_url:
        print("cfnresponse: event carries no ResponseURL; nothing to signal")
        return

    log_stream = getattr(context, "log_stream_name", "unknown")
    body = json.dumps(
        {
            "Status": response_status,
            "Reason": reason or f"See CloudWatch Log Stream: {log_stream}",
            "PhysicalResourceId": physical_resource_id or log_stream,
            "StackId": event.get("StackId"),
            "RequestId": event.get("RequestId"),
            "LogicalResourceId": event.get("LogicalResourceId"),
            "NoEcho": False,
            "Data": response_data or {},
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        response_url,
        data=body,
        method="PUT",
        # An empty content-type is what the pre-signed URL expects; letting a
        # client library pick its own default is a known way to get a 403 here.
        headers={"content-type": "", "content-length": str(len(body))},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            print(f"cfnresponse: sent {response_status}, status {resp.status}")
    except Exception as exc:  # noqa: BLE001 - best-effort signal, nothing else to do
        print(f"cfnresponse.send failed: {type(exc).__name__}: {exc}")
