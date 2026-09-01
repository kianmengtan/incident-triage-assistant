"""RCA generation. The riskiest branch here is what happens when the model does
not return the JSON it was asked for, which the old implementation handled by
putting the raw output in the summary — and which no test covered."""
import json
from unittest.mock import MagicMock, patch

import pytest

import generate_rca
from common import bedrock, config

EVENT = {
    "tenant_id": "acme",
    "alert_id": "alert-1",
    "alert": {"service": "checkout", "severity": "high", "description": "5xx spike"},
    "logs_context": {"entry_count": 12},
    "config_context": {"change_count": 1},
    "rag_context": {"context_text": "", "similar_incidents": [{"key": "acme#alert-0"}]},
}

GOOD = {
    "rca_summary": "The connection pool ceiling is exhausting under normal load.",
    "confidence": "high",
    "remediation_steps": [
        {"text": "Raise DB_POOL_MAX to 40", "priority": "P1", "command": "kubectl set env ...", "reversible": True},
        {"text": "Redeploy checkout", "priority": "P2", "command": None, "reversible": True},
    ],
}


def _model(payload_text, stop_reason="end_turn"):
    """Bedrock returns the assistant turn; generate_json prefills it with '{'."""
    body = {"content": [{"text": payload_text}], "stop_reason": stop_reason}
    return {"body": MagicMock(read=lambda: json.dumps(body).encode("utf-8"))}


def _as_prefilled(obj):
    """Strip the leading '{' the way a prefilled assistant turn would."""
    return json.dumps(obj)[1:]


@pytest.fixture
def table():
    return MagicMock()


@pytest.fixture
def harness(table):
    with patch.object(generate_rca.crypto, "encrypt_field", side_effect=lambda t, v: v), \
         patch.object(generate_rca.progress, "mark_stage"), \
         patch.object(generate_rca.tenant_scope, "tenant_dynamodb_resource") as resource:
        resource.return_value.Table.return_value = table
        yield {"table": table}


def _run(payload_text, stop_reason="end_turn"):
    with patch.object(bedrock._bedrock, "invoke_model", return_value=_model(payload_text, stop_reason)):
        return generate_rca.handler(EVENT, None)


# ------------------------------------------------------------------- happy path


def test_the_approved_model_id_and_prefill_are_used(harness):
    with patch.object(
        bedrock._bedrock, "invoke_model", return_value=_model(_as_prefilled(GOOD))
    ) as invoke:
        generate_rca.handler(EVENT, None)

    body = json.loads(invoke.call_args.kwargs["body"])
    assert invoke.call_args.kwargs["modelId"] == config.HAIKU_MODEL_ID
    assert config.HAIKU_MODEL_ID == "global.anthropic.claude-haiku-4-5-20251001-v1:0"
    assert body["messages"][-1] == {"role": "assistant", "content": "{"}, "JSON must be forced"


def test_the_diagnostic_is_written_under_the_alerts_key(harness):
    result = _run(_as_prefilled(GOOD))

    item = harness["table"].put_item.call_args.kwargs["Item"]
    assert item["tenant_id"] == "acme"
    assert item["sk"] == "diag#alert-1"
    assert result["rca_summary"] == GOOD["rca_summary"]
    assert result["confidence"] == "high"


def test_step_structure_survives_instead_of_being_flattened_to_a_string(harness):
    """The old code stored "\\n".join(steps), which discarded the priority and
    command the specification and the UI both rely on."""
    result = _run(_as_prefilled(GOOD))

    assert result["remediation_steps"][0]["priority"] == "P1"
    assert result["remediation_steps"][0]["command"] == "kubectl set env ..."
    stored = json.loads(harness["table"].put_item.call_args.kwargs["Item"]["remediation_steps"])
    assert stored[0]["text"] == "Raise DB_POOL_MAX to 40"


def test_every_sensitive_field_is_encrypted(harness):
    """The retrieved-incident refs carry other incidents' descriptions, so
    encrypting only the summary and leaving them in plaintext beside it in the
    same item protected very little."""
    with patch.object(generate_rca.crypto, "encrypt_field", side_effect=lambda t, v: f"enc({v})") as enc, \
         patch.object(generate_rca.tenant_scope, "tenant_dynamodb_resource") as resource, \
         patch.object(bedrock._bedrock, "invoke_model", return_value=_model(_as_prefilled(GOOD))):
        resource.return_value.Table.return_value = harness["table"]
        generate_rca.handler(EVENT, None)

    item = harness["table"].put_item.call_args.kwargs["Item"]
    for field in ("rca_summary", "remediation_steps", "rag_context_refs"):
        assert item[field].startswith("enc("), f"{field} must be encrypted"
    assert enc.call_count == 3


# ------------------------------------------------ model output that misbehaves


def test_steps_returned_as_bare_strings_are_accepted(harness):
    payload = dict(GOOD, remediation_steps=["Restart the pod", "Raise the pool ceiling"])
    result = _run(_as_prefilled(payload))

    assert len(result["remediation_steps"]) == 2
    assert result["remediation_steps"][0] == {
        "text": "Restart the pod", "priority": "P2", "command": None, "reversible": None
    }


def test_steps_returned_as_objects_do_not_crash_the_task(harness):
    """The exact regression: "\\n".join() on a list of dicts raised TypeError
    outside the try, failing the task and burning three retries."""
    payload = dict(GOOD, remediation_steps=[{"step": "Do the thing", "priority": "p1"}])
    result = _run(_as_prefilled(payload))

    assert result["remediation_steps"] == [
        {"text": "Do the thing", "priority": "P1", "command": None, "reversible": None}
    ]


def test_an_unknown_priority_falls_back_rather_than_propagating(harness):
    payload = dict(GOOD, remediation_steps=[{"text": "x", "priority": "URGENT!!"}])
    assert _run(_as_prefilled(payload))["remediation_steps"][0]["priority"] == "P2"


def test_junk_in_the_steps_array_is_dropped(harness):
    payload = dict(GOOD, remediation_steps=[None, 42, {}, {"text": "keep me"}])
    assert [s["text"] for s in _run(_as_prefilled(payload))["remediation_steps"]] == ["keep me"]


def test_a_non_list_steps_value_yields_no_steps(harness):
    payload = dict(GOOD, remediation_steps="just do it")
    assert _run(_as_prefilled(payload))["remediation_steps"] == []


def test_an_unknown_confidence_falls_back_to_probable(harness):
    assert _run(_as_prefilled(dict(GOOD, confidence="very sure")))["confidence"] == "probable"


def test_unparseable_model_output_raises_instead_of_becoming_the_summary(harness):
    """The old fallback put a wall of half-finished JSON in front of an on-call
    engineer as though it were the root cause."""
    with pytest.raises(bedrock.InvalidModelJson):
        _run('this is prose, not JSON')


def test_a_truncated_response_raises_rather_than_being_stored(harness):
    with pytest.raises(bedrock.TruncatedResponse):
        _run(_as_prefilled(GOOD), stop_reason="max_tokens")


def test_a_missing_summary_raises(harness):
    with pytest.raises(bedrock.InvalidModelJson):
        _run(_as_prefilled({"remediation_steps": [], "confidence": "high"}))


def test_nothing_is_written_when_the_model_output_is_rejected(harness):
    with pytest.raises(bedrock.InvalidModelJson):
        _run("not json at all")
    harness["table"].put_item.assert_not_called()


def test_no_steps_is_tolerated_but_logged(harness):
    result = _run(_as_prefilled(dict(GOOD, remediation_steps=[])))
    assert result["remediation_steps"] == []
    assert result["rca_summary"]
