"""The alert-writing path is shared by the webhook and the authenticated form.

`fn-ingest-normalize` (API key + per-tenant HMAC, for PagerDuty and friends) and
`fn-create-incident` (Cognito, for a person filling in a form) must produce the
same row and the same ``alert.received`` event, or an incident raised by hand
would behave differently from one raised by a webhook -- diagnosed twice,
diagnosed never, or stored in a shape the pipeline cannot read.

The subtle part being shared is the deduplication: the conditional put is keyed
on ``alert#{alert_id}`` alone, and a row that exists *without* ``dispatched_at``
means a previous delivery stored the alert but failed to publish it, so the retry
has to publish rather than report a duplicate and drop it.
"""
from unittest.mock import patch

import pytest

from common import alerts


class _Table:
    """Minimal DynamoDB Table double recording puts and updates."""

    def __init__(self, existing=None):
        self.existing = existing
        self.put_items = []
        self.updates = []

    def put_item(self, **kwargs):
        from botocore.exceptions import ClientError

        if self.existing is not None:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem"
            )
        self.put_items.append(kwargs["Item"])

    def get_item(self, **kwargs):
        return {"Item": self.existing} if self.existing else {}

    def update_item(self, **kwargs):
        self.updates.append(kwargs)


def test_tenant_id_is_a_parameter_not_a_body_field():
    """The authenticated path must not be able to take tenant from the client.

    `normalize` takes tenant_id positionally, so a handler physically cannot
    forget and fall back to the request body -- which is how a caller would
    otherwise write an alert into another tenant.
    """
    alert = alerts.normalize({"tenant_id": "attacker-co", "severity": "sev1",
                              "service": "checkout", "description": "boom"}, "victim-co")
    assert alert["tenant_id"] == "victim-co"


def test_normalize_fills_defaults_and_stamps_received_at():
    alert = alerts.normalize({"severity": "SEV2", "service": "auth", "description": "d"}, "acme")
    assert alert["status"] == "received"
    assert alert["source"] == "unknown"
    assert isinstance(alert["received_at"], int)
    assert alert["alert_id"]


def test_normalize_generates_an_alert_id_when_none_is_given():
    a = alerts.normalize({"severity": "sev1", "service": "s", "description": "d"}, "acme")
    b = alerts.normalize({"severity": "sev1", "service": "s", "description": "d"}, "acme")
    assert a["alert_id"] != b["alert_id"]


def test_normalize_honours_a_supplied_alert_id():
    alert = alerts.normalize(
        {"alert_id": "ALT-1", "severity": "sev1", "service": "s", "description": "d"}, "acme"
    )
    assert alert["alert_id"] == "ALT-1"


def test_long_fields_are_truncated_rather_than_rejected():
    """DynamoDB items cap at 400 KB; a long description must not fail the put."""
    alert = alerts.normalize(
        {"severity": "sev1", "service": "s", "description": "x" * 99999}, "acme"
    )
    assert len(alert["description"]) == alerts.MAX_FIELD_CHARS


@pytest.mark.parametrize("severity", ["sev1", "SEV1", " sev1 ", "critical", "p1", "low"])
def test_recognised_severities(severity):
    assert alerts.is_valid_severity(severity)


@pytest.mark.parametrize("severity", ["", None, "sev5", "urgent", "catastrophic", "9"])
def test_unrecognised_severities(severity):
    assert not alerts.is_valid_severity(severity)


@pytest.mark.parametrize("alert_id", ["A", "ALT-1041", "a.b_c:d-1", "x" * 128])
def test_usable_alert_ids(alert_id):
    assert alerts.usable_alert_id(alert_id)


@pytest.mark.parametrize(
    "alert_id",
    [
        "",
        "-leading-hyphen",
        "has/slash",  # would nest the S3 evidence key under an unread prefix
        "has#hash",  # could forge a key in another namespace of prefix#id
        "has space",
        "x" * 129,
    ],
)
def test_unusable_alert_ids(alert_id):
    assert not alerts.usable_alert_id(alert_id)


def test_store_and_dispatch_writes_the_row_and_publishes():
    table = _Table()
    alert = alerts.normalize({"severity": "sev1", "service": "s", "description": "d"}, "acme")
    with patch.object(alerts, "publish_received", return_value=True) as pub:
        outcome = alerts.store_and_dispatch(table, "acme", alert)
    assert outcome == alerts.CREATED
    assert table.put_items[0]["sk"] == f"alert#{alert['alert_id']}"
    assert table.put_items[0]["tenant_id"] == "acme"
    pub.assert_called_once()
    assert table.updates, "dispatched_at must be stamped once publishing succeeded"


def test_a_stored_but_undispatched_duplicate_is_published_not_dropped():
    """The first delivery stored the row then failed to publish.

    Reporting 'duplicate' here would leave the alert stored and never diagnosed.
    """
    alert = alerts.normalize(
        {"alert_id": "ALT-9", "severity": "sev1", "service": "s", "description": "d"}, "acme"
    )
    table = _Table(existing={"tenant_id": "acme", "sk": "alert#ALT-9", **alert})
    with patch.object(alerts, "publish_received", return_value=True) as pub:
        outcome = alerts.store_and_dispatch(table, "acme", alert)
    assert outcome == alerts.REDISPATCHED
    pub.assert_called_once()


def test_an_already_dispatched_duplicate_is_reported_as_such():
    alert = alerts.normalize(
        {"alert_id": "ALT-9", "severity": "sev1", "service": "s", "description": "d"}, "acme"
    )
    table = _Table(existing={"sk": "alert#ALT-9", "dispatched_at": 123, **alert})
    with patch.object(alerts, "publish_received", return_value=True) as pub:
        outcome = alerts.store_and_dispatch(table, "acme", alert)
    assert outcome == alerts.DUPLICATE
    pub.assert_not_called()


def test_a_failed_publish_is_reported_so_the_caller_is_not_told_it_worked():
    """Stored but unpublished means nothing will ever diagnose it."""
    table = _Table()
    alert = alerts.normalize({"severity": "sev1", "service": "s", "description": "d"}, "acme")
    with patch.object(alerts, "publish_received", return_value=False):
        outcome = alerts.store_and_dispatch(table, "acme", alert)
    assert outcome == alerts.DISPATCH_FAILED
    assert not table.updates, "dispatched_at must not be stamped when publishing failed"


def test_publish_received_reports_false_when_eventbridge_rejects_the_entry():
    alert = alerts.normalize({"severity": "sev1", "service": "s", "description": "d"}, "acme")
    with patch.object(alerts, "_events") as events:
        events.put_events.return_value = {
            "FailedEntryCount": 1,
            "Entries": [{"ErrorCode": "InternalException"}],
        }
        assert alerts.publish_received(alert) is False


def test_publish_received_sends_the_detail_the_pipeline_reads():
    import json

    alert = alerts.normalize({"severity": "sev1", "service": "s", "description": "d"}, "acme")
    with patch.object(alerts, "_events") as events:
        events.put_events.return_value = {"FailedEntryCount": 0, "Entries": [{}]}
        assert alerts.publish_received(alert) is True
    entry = events.put_events.call_args.kwargs["Entries"][0]
    assert entry["DetailType"] == "alert.received"
    detail = json.loads(entry["Detail"])
    assert detail["tenant_id"] == "acme"
    assert detail["alert_id"] == alert["alert_id"]
    assert detail["alert"]["service"] == "s"
