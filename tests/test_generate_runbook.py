"""Runbook rendering and storage."""
import json
from unittest.mock import MagicMock, patch

import pytest

import generate_runbook
from common import bedrock

EVENT = {
    "tenant_id": "acme",
    "alert_id": "alert-1",
    "alert": {"service": "checkout", "severity": "high"},
    "diagnostic": {
        "diagnostic_id": "diag-abc",
        "rca_summary": "pool exhausted",
        "confidence": "high",
        "remediation_steps": [{"text": "Raise DB_POOL_MAX", "priority": "P1"}],
    },
}


def _model(text="# Runbook\n\n1. Raise the pool", stop_reason="end_turn"):
    body = {"content": [{"text": text}], "stop_reason": stop_reason}
    return {"body": MagicMock(read=lambda: json.dumps(body).encode())}


@pytest.fixture
def harness():
    table, s3 = MagicMock(), MagicMock()
    with patch.object(generate_runbook.tenant_scope, "tenant_dynamodb_resource") as resource, \
         patch.object(generate_runbook.tenant_scope, "tenant_s3_client", return_value=s3), \
         patch.object(generate_runbook.progress, "mark_stage"), \
         patch.object(bedrock._bedrock, "invoke_model", return_value=_model()):
        resource.return_value.Table.return_value = table
        yield {"table": table, "s3": s3}


def test_the_runbook_is_stored_under_the_tenants_prefix_and_marked_pending(harness):
    result = generate_runbook.handler(EVENT, None)

    assert harness["s3"].put_object.call_args.kwargs["Key"].startswith("tenant/acme/runbook/")
    item = harness["table"].put_item.call_args.kwargs["Item"]
    assert item["approval_status"] == "pending"
    assert item["execution_status"] == "not_started"
    assert result["runbook_id"]


def test_the_runbook_id_is_derived_so_a_retry_overwrites_its_own_attempt(harness):
    """A random uuid meant a Step Functions retry after a successful S3 put left
    an orphaned object and a second Runbooks row behind."""
    first = generate_runbook.handler(EVENT, None)["runbook_id"]
    second = generate_runbook.handler(EVENT, None)["runbook_id"]

    assert first == second


def test_a_different_diagnostic_gets_a_different_runbook_id(harness):
    a = generate_runbook.handler(EVENT, None)["runbook_id"]
    other = dict(EVENT, diagnostic=dict(EVENT["diagnostic"], diagnostic_id="diag-xyz"))
    b = generate_runbook.handler(other, None)["runbook_id"]

    assert a != b


def test_the_prompt_carries_the_structured_steps(harness):
    with patch.object(bedrock._bedrock, "invoke_model", return_value=_model()) as invoke:
        generate_runbook.handler(EVENT, None)

    prompt = json.loads(invoke.call_args.kwargs["body"])["messages"][0]["content"]
    assert "Raise DB_POOL_MAX" in prompt
    assert "P1" in prompt


def test_a_truncated_runbook_is_refused_rather_than_stored(harness):
    """A runbook cut off mid-step is worse than none: the missing steps are
    invisible and someone would execute the ones that made it."""
    with patch.object(bedrock._bedrock, "invoke_model", return_value=_model(stop_reason="max_tokens")):
        with pytest.raises(bedrock.TruncatedResponse):
            generate_runbook.handler(EVENT, None)

    harness["s3"].put_object.assert_not_called()
    harness["table"].put_item.assert_not_called()
