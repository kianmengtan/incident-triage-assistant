import json


def api_response(status_code, body, headers=None):
    resp_headers = {"Content-Type": "application/json"}
    if headers:
        resp_headers.update(headers)
    return {
        "statusCode": status_code,
        "headers": resp_headers,
        "body": json.dumps(body, default=str),
    }
