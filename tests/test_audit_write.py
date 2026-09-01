"""The audit trail. "Append-only" has to be enforced, not just documented."""
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

import audit_write

EVENT = {
    "tenant_id": "acme",
    "actor": "user-1",
    "action": "remediation.approve",
    "result": "succeeded",
    "alert_id": "alert-1",
    "runbook_id": "rb-1",
}


@pytest.fixture
def table():
    return MagicMock()


@pytest.fixture
def harness(table):
    with patch.object(audit_write.tenant_scope, "tenant_dynamodb_resource") as resource:
        resource.return_value.Table.return_value = table
        yield {"table": table}


def test_the_record_carries_every_required_field(harness):
    audit_write.handler(EVENT, None)

    item = harness["table"].put_item.call_args.kwargs["Item"]
    for field in ("tenant_id", "action", "actor", "result", "alert_id", "runbook_id", "timestamp", "expires_at"):
        assert field in item, field
    assert item["expires_at"] > item["timestamp"]


def test_two_records_in_the_same_second_do_not_collide(harness):
    """The key was audit#{unix_second}#{actor}, so the "attempted" record and its
    outcome — written by one approval, same actor, same second — silently
    overwrote each other."""
    with patch.object(audit_write.time, "time", return_value=1_700_000_000):
        audit_write.handler(EVENT, None)
        first = harness["table"].put_item.call_args.kwargs["Item"]["sk"]
        audit_write.handler(dict(EVENT, result="attempted"), None)
        second = harness["table"].put_item.call_args.kwargs["Item"]["sk"]

    assert first != second


def test_the_put_is_conditional_so_nothing_can_be_overwritten(harness):
    audit_write.handler(EVENT, None)
    assert harness["table"].put_item.call_args.kwargs["ConditionExpression"] == (
        "attribute_not_exists(sk)"
    )


def test_the_key_stays_time_ordered_for_chronological_queries(harness):
    with patch.object(audit_write.time, "time", return_value=1_700_000_000):
        audit_write.handler(EVENT, None)
    assert harness["table"].put_item.call_args.kwargs["Item"]["sk"].startswith("audit#1700000000#")


def test_a_write_failure_is_raised_so_it_reaches_the_dlq(harness):
    """Swallowing this would lose the record silently; raising sends the async
    invocation to its on-failure destination."""
    harness["table"].put_item.side_effect = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "x"}}, "PutItem"
    )
    with pytest.raises(ClientError):
        audit_write.handler(EVENT, None)
