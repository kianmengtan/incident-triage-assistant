"""Pipeline progress, recorded on the alert row.

Advisory by design: the console needs to know which stage is running, but a
failure to record that must never fail the stage it is reporting on. Everything
here is therefore swallowed -- which is also why a missing IAM grant on fn-notify
went unnoticed for the life of the deployment.
"""
from unittest.mock import MagicMock, patch

import pytest

from common import config, progress


@pytest.fixture
def table():
    t = MagicMock()
    t.get_item.return_value = {}
    return t


@pytest.fixture
def harness(table):
    with patch.object(progress.tenant_scope, "tenant_dynamodb_resource") as resource:
        resource.return_value.Table.return_value = table
        yield table


def test_the_stage_is_written_to_the_alerts_row(harness):
    progress.mark_stage("acme", "alert-1", progress.RCA)

    kwargs = harness.update_item.call_args.kwargs
    assert kwargs["Key"] == {"tenant_id": "acme", "sk": "alert#alert-1"}
    assert kwargs["ExpressionAttributeValues"][":stage"] == progress.RCA


def test_nothing_is_written_without_a_tenant_or_alert(harness):
    progress.mark_stage("", "alert-1", progress.RCA)
    progress.mark_stage("acme", None, progress.RCA)
    harness.update_item.assert_not_called()


def test_a_failure_to_record_never_raises(harness):
    """A stage that did its work must not be failed by its own progress report."""
    harness.update_item.side_effect = RuntimeError("AccessDenied")
    progress.mark_stage("acme", "alert-1", progress.RCA)  # must not raise


def test_the_stage_history_is_a_set_not_an_append_log(harness):
    """stages_seen used to list_append unconditionally, so every Step Functions
    retry of a stage added another copy and the timeline drew duplicates on a row
    that grew without bound."""
    harness.get_item.return_value = {
        "Item": {"stages_seen": [progress.CORRELATE_LOGS, progress.CORRELATE_CONFIG]}
    }
    progress.mark_stage("acme", "alert-1", progress.CORRELATE_LOGS)

    written = harness.update_item.call_args.kwargs["ExpressionAttributeValues"][":seen"]
    assert written.count(progress.CORRELATE_LOGS) == 1
    assert set(written) == {progress.CORRELATE_LOGS, progress.CORRELATE_CONFIG}


def test_the_history_is_kept_in_pipeline_order(harness):
    """The UI draws them in order, so the row should not depend on arrival order
    of retries to look right."""
    harness.get_item.return_value = {"Item": {"stages_seen": [progress.RCA, progress.CORRELATE_LOGS]}}
    progress.mark_stage("acme", "alert-1", progress.RAG)

    written = harness.update_item.call_args.kwargs["ExpressionAttributeValues"][":seen"]
    assert written == [progress.CORRELATE_LOGS, progress.RAG, progress.RCA]


def test_a_new_alert_starts_a_history(harness):
    progress.mark_stage("acme", "alert-1", progress.CORRELATE_LOGS)
    written = harness.update_item.call_args.kwargs["ExpressionAttributeValues"][":seen"]
    assert written == [progress.CORRELATE_LOGS]


def test_the_failed_marker_is_not_a_pipeline_stage(harness):
    """It is a terminal marker the read API branches on, so it must not appear in
    the ordered stage list the timeline draws."""
    assert progress.FAILED not in progress.STAGE_ORDER


def test_failure_is_recorded_even_though_it_is_not_in_the_stage_order(harness):
    progress.mark_stage("acme", "alert-1", progress.FAILED)
    kwargs = harness.update_item.call_args.kwargs
    assert kwargs["ExpressionAttributeValues"][":stage"] == progress.FAILED
