import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

import ingest_normalize

TENANT_ID = "acme"
SECRET = "shh-its-a-secret"


def _sign(body):
    return hmac.new(SECRET.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()


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


def _event(payload=None, body=None, **extra):
    body = body if body is not None else json.dumps(payload)
    event = {"body": body, "headers": {"X-Signature": _sign(body)}}
    event.update(extra)
    return event


def _conditional_failure():
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "dup"}}, "PutItem"
    )


@pytest.fixture
def table():
    t = MagicMock()
    t.get_item.return_value = {}
    return t


@pytest.fixture
def harness(table):
    """Patches the three boundaries and hands back the mocks."""
    with patch.object(
        ingest_normalize._secrets, "get_secret_value", return_value={"SecretString": SECRET}
    ), patch.object(ingest_normalize.tenant_scope, "tenant_dynamodb_resource") as resource, patch.object(
        ingest_normalize._events, "put_events", return_value={"FailedEntryCount": 0}
    ) as put_events:
        resource.return_value.Table.return_value = table
        yield {"table": table, "put_events": put_events}


def test_valid_alert_is_persisted_and_published(harness):
    resp = ingest_normalize.handler(_event(_payload()), None)

    assert resp["statusCode"] == 202
    assert json.loads(resp["body"])["status"] == "received"
    harness["put_events"].assert_called_once()
    detail = json.loads(harness["put_events"].call_args.kwargs["Entries"][0]["Detail"])
    assert detail["tenant_id"] == TENANT_ID
    assert detail["alert_id"] == "alert-1"


def test_the_dedup_key_is_the_alert_id_alone(harness):
    """The property the old test asserted by name but never actually checked.

    It injected a ConditionalCheckFailedException itself, so it only proved the
    handler translates that exception into a 202. The real key included
    int(time.time()), so a webhook retry a second later wrote a second row and
    the alert was diagnosed twice.
    """
    ingest_normalize.handler(_event(_payload()), None)
    item = harness["table"].put_item.call_args.kwargs["Item"]

    assert item["sk"] == "alert#alert-1"
    assert str(item["received_at"]) not in item["sk"]
    assert harness["table"].put_item.call_args.kwargs["ConditionExpression"] == (
        "attribute_not_exists(sk)"
    )


def test_two_deliveries_a_second_apart_produce_the_same_key(harness):
    with patch("ingest_normalize.time.time", side_effect=[1000, 1000, 1001, 1001]):
        ingest_normalize.handler(_event(_payload()), None)
        first = harness["table"].put_item.call_args.kwargs["Item"]["sk"]
        harness["table"].put_item.reset_mock()
        ingest_normalize.handler(_event(_payload()), None)
        second = harness["table"].put_item.call_args.kwargs["Item"]["sk"]

    assert first == second, "a retry must collide with the original, not write a new row"


def test_a_dispatched_duplicate_is_reported_and_not_republished(harness):
    harness["table"].put_item.side_effect = _conditional_failure()
    harness["table"].get_item.return_value = {"Item": {"dispatched_at": 1234}}

    resp = ingest_normalize.handler(_event(_payload()), None)

    assert json.loads(resp["body"])["status"] == "duplicate"
    harness["put_events"].assert_not_called()


def test_a_duplicate_that_was_never_dispatched_is_published_on_retry(harness):
    """The alert's last chance not to be silently dropped.

    If the first delivery stored the row but failed to publish, treating the
    retry as a plain duplicate would leave the alert stored and never diagnosed.
    """
    harness["table"].put_item.side_effect = _conditional_failure()
    harness["table"].get_item.return_value = {
        "Item": {
            "tenant_id": TENANT_ID,
            "alert_id": "alert-1",
            "received_at": 1000,
            "sk": "alert#alert-1",
        }
    }

    resp = ingest_normalize.handler(_event(_payload()), None)

    assert json.loads(resp["body"])["status"] == "received"
    harness["put_events"].assert_called_once()
    harness["table"].update_item.assert_called_once()


def test_a_failed_publish_is_reported_as_an_error_not_a_202(harness):
    """An alert nothing will diagnose must not be answered with "received"."""
    harness["put_events"].return_value = {
        "FailedEntryCount": 1,
        "Entries": [{"ErrorCode": "InternalException", "ErrorMessage": "nope"}],
    }

    resp = ingest_normalize.handler(_event(_payload()), None)

    assert resp["statusCode"] == 502
    harness["table"].update_item.assert_not_called()


def test_a_successful_publish_marks_the_row_dispatched(harness):
    ingest_normalize.handler(_event(_payload()), None)
    update = harness["table"].update_item.call_args.kwargs
    assert "dispatched_at" in update["UpdateExpression"]


def test_invalid_signature_is_rejected(harness):
    bad = _event(_payload())
    bad["headers"]["X-Signature"] = "not-the-right-signature"

    resp = ingest_normalize.handler(bad, None)

    assert resp["statusCode"] == 401
    harness["table"].put_item.assert_not_called()


def test_a_base64_encoded_body_still_verifies(harness):
    """API Gateway base64-encodes bodies it treats as binary."""
    import base64

    body = json.dumps(_payload())
    event = {
        "body": base64.b64encode(body.encode("utf-8")).decode("ascii"),
        "isBase64Encoded": True,
        "headers": {"X-Signature": _sign(body)},
    }

    resp = ingest_normalize.handler(event, None)

    assert resp["statusCode"] == 202


def test_missing_fields_are_listed_readably(harness):
    resp = ingest_normalize.handler(_event(_payload(service="")), None)
    assert resp["statusCode"] == 400
    assert "service" in json.loads(resp["body"])["message"]
    assert "[" not in json.loads(resp["body"])["message"]


def test_an_unrecognised_severity_is_refused(harness):
    resp = ingest_normalize.handler(_event(_payload(severity="catastrophic")), None)
    assert resp["statusCode"] == 400


def test_an_oversized_body_is_refused_before_dynamodb(harness):
    huge = json.dumps(_payload(description="x" * (ingest_normalize.MAX_BODY_BYTES + 10)))
    resp = ingest_normalize.handler(_event(body=huge), None)

    assert resp["statusCode"] == 413
    harness["table"].put_item.assert_not_called()


def test_a_non_object_body_is_refused(harness):
    resp = ingest_normalize.handler(_event(body="[1,2,3]"), None)
    assert resp["statusCode"] == 400


def test_invalid_json_is_refused(harness):
    resp = ingest_normalize.handler(_event(body="{not json"), None)
    assert resp["statusCode"] == 400


# ---------------------------------------------------------------------------
# alert_id is client-supplied and becomes a DynamoDB sort key and an S3 key.
# ---------------------------------------------------------------------------
def test_a_client_supplied_alert_id_is_kept_when_it_is_sane(harness):
    resp = ingest_normalize.handler(_event(_payload(alert_id="ALT-1041")), None)
    assert json.loads(resp["body"])["alert_id"] == "ALT-1041"


@pytest.mark.parametrize(
    "alert_id",
    [
        "with/slash",
        "with space",
        "with,comma",
        "..",
        "x" * 300,
    ],
)
def test_an_unusable_alert_id_is_rejected(harness, alert_id):
    """It is uncapped and unvalidated on the way in, then interpolated into the
    Alerts sort key and into the correlation cache's S3 object key. A slash nests
    the cached evidence under a prefix nothing reads back."""
    resp = ingest_normalize.handler(_event(_payload(alert_id=alert_id)), None)

    assert resp["statusCode"] == 400
    assert "alert_id" in json.loads(resp["body"])["message"]
    harness["table"].put_item.assert_not_called()


def test_an_alert_id_carrying_the_key_separator_is_rejected(harness):
    """The whole schema is keyed on prefix#id, so an id containing the separator
    can forge a key in another namespace."""
    resp = ingest_normalize.handler(_event(_payload(alert_id="a#alert")), None)
    assert resp["statusCode"] == 400


def test_a_generated_alert_id_is_always_acceptable(harness):
    resp = ingest_normalize.handler(_event(_payload(alert_id=None)), None)
    generated = json.loads(resp["body"])["alert_id"]
    assert ingest_normalize._usable_alert_id(generated)
