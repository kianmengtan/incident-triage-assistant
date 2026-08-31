import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

import ingest_normalize

TENANT_ID = "acme"
SECRET = "shh-its-a-secret"


def _sign(body):
    return hmac.new(SECRET.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()


def _event(payload):
    body = json.dumps(payload)
    return {
        "body": body,
        "headers": {"X-Signature": _sign(body)},
    }


def _payload(**overrides):
    base = {
        "tenant_id": TENANT_ID,
        "severity": "high",
        "service": "checkout",
        "description": "5xx spike",
        "alert_id": "alert-1",
    }
    base.update(overrides)
    return base


def _patch_secret():
    return patch.object(
        ingest_normalize._secrets,
        "get_secret_value",
        return_value={"SecretString": SECRET},
    )


def test_duplicate_alert_id_is_deduped_without_second_write():
    table = MagicMock()
    table.put_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "dup"}}, "PutItem"
    )

    with _patch_secret(), patch.object(
        ingest_normalize.tenant_scope, "tenant_dynamodb_resource"
    ) as mock_resource, patch.object(ingest_normalize._events, "put_events") as mock_put_events:
        mock_resource.return_value.Table.return_value = table
        resp = ingest_normalize.handler(_event(_payload()), None)

    assert resp["statusCode"] == 202
    assert json.loads(resp["body"])["status"] == "duplicate"
    assert table.put_item.call_count == 1
    mock_put_events.assert_not_called()


def test_valid_alert_is_persisted_and_published():
    table = MagicMock()

    with _patch_secret(), patch.object(
        ingest_normalize.tenant_scope, "tenant_dynamodb_resource"
    ) as mock_resource, patch.object(ingest_normalize._events, "put_events") as mock_put_events:
        mock_resource.return_value.Table.return_value = table
        resp = ingest_normalize.handler(_event(_payload()), None)

    assert resp["statusCode"] == 202
    body = json.loads(resp["body"])
    assert body["status"] == "received"
    table.put_item.assert_called_once()
    mock_put_events.assert_called_once()
    detail = json.loads(mock_put_events.call_args.kwargs["Entries"][0]["Detail"])
    assert detail["tenant_id"] == TENANT_ID


def test_invalid_signature_is_rejected():
    table = MagicMock()
    bad_event = _event(_payload())
    bad_event["headers"]["X-Signature"] = "not-the-right-signature"

    with _patch_secret(), patch.object(
        ingest_normalize.tenant_scope, "tenant_dynamodb_resource"
    ) as mock_resource:
        mock_resource.return_value.Table.return_value = table
        resp = ingest_normalize.handler(bad_event, None)

    assert resp["statusCode"] == 401
    table.put_item.assert_not_called()
