"""Vendored copy of AWS's cfnresponse helper (not published to PyPI)."""
import json

import urllib3

SUCCESS = "SUCCESS"
FAILED = "FAILED"

_http = urllib3.PoolManager()


def send(event, context, response_status, response_data=None, physical_resource_id=None, reason=None):
    response_body = {
        "Status": response_status,
        "Reason": reason or f"See CloudWatch Log Stream: {context.log_stream_name}",
        "PhysicalResourceId": physical_resource_id or context.log_stream_name,
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "NoEcho": False,
        "Data": response_data or {},
    }
    encoded = json.dumps(response_body).encode("utf-8")
    try:
        _http.request(
            "PUT",
            event["ResponseURL"],
            body=encoded,
            headers={"content-type": "", "content-length": str(len(encoded))},
        )
    except Exception as exc:  # noqa: BLE001 - best-effort signal, nothing else to do
        print(f"cfnresponse.send failed: {exc}")
