"""The Bedrock tool-use loop.

This is the one place in the codebase where the model decides what to call, so
the loop itself is what has to be trustworthy rather than any single prompt.
Three properties matter and each has a test below:

* **The loop terminates.** A model that keeps asking for tools is capped, and the
  cap raises rather than returning a half-finished answer as if it were complete.
* **A refused tool is an answer, not a crash.** RBAC denials come back to the
  model as a tool_result so it can tell the user why, which is the difference
  between "only Tenant Admins may read the audit trail" and a 500.
* **Only declared tools run.** `dispatch` is asked for a name the model invented
  and must refuse it, because a tool name is model-controlled input.
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from common import bedrock

TOOLS = [
    {
        "name": "list_incidents",
        "description": "List this tenant's incidents",
        "input_schema": {"type": "object", "properties": {}},
    }
]


def _payload(blocks, stop_reason="end_turn"):
    return {"content": blocks, "stop_reason": stop_reason}


def _resp(payload):
    return {"body": MagicMock(read=lambda: json.dumps(payload).encode("utf-8"))}


def _turns(*payloads):
    """Bedrock returning a different body on each successive invoke_model."""
    return [_resp(p) for p in payloads]


@pytest.fixture
def invoke():
    with patch.object(bedrock._bedrock, "invoke_model") as mock:
        yield mock


def _run(dispatch, **kwargs):
    return bedrock.run_tool_conversation(
        "system",
        [{"role": "user", "content": "what is broken?"}],
        TOOLS,
        dispatch,
        **kwargs,
    )


def test_a_reply_with_no_tool_call_comes_straight_back(invoke):
    invoke.side_effect = _turns(_payload([{"type": "text", "text": "Nothing is broken."}]))
    result = _run(lambda name, args: {})

    assert result.text == "Nothing is broken."
    assert result.tool_calls == []
    assert result.iterations == 1


def test_a_tool_call_is_dispatched_and_its_result_fed_back(invoke):
    invoke.side_effect = _turns(
        _payload(
            [{"type": "tool_use", "id": "tu-1", "name": "list_incidents", "input": {"limit": 5}}],
            stop_reason="tool_use",
        ),
        _payload([{"type": "text", "text": "One incident is open."}]),
    )
    calls = []

    def dispatch(name, args):
        calls.append((name, args))
        return {"alerts": [{"alert_id": "ALT-001"}]}

    result = _run(dispatch)

    assert calls == [("list_incidents", {"limit": 5})]
    assert result.text == "One incident is open."
    assert result.iterations == 2
    assert [c.name for c in result.tool_calls] == ["list_incidents"]
    assert result.tool_calls[0].refused is False

    # The second call must carry the assistant's tool_use turn and our
    # tool_result, or the model answers without ever seeing what it asked for.
    second = json.loads(invoke.call_args_list[1].kwargs["body"])
    assert second["messages"][1]["role"] == "assistant"
    assert second["messages"][2]["content"][0]["type"] == "tool_result"
    assert second["messages"][2]["content"][0]["tool_use_id"] == "tu-1"
    assert "ALT-001" in second["messages"][2]["content"][0]["content"]


def test_the_declared_tools_are_sent_to_the_model(invoke):
    invoke.side_effect = _turns(_payload([{"type": "text", "text": "hi"}]))
    _run(lambda name, args: {})

    body = json.loads(invoke.call_args.kwargs["body"])
    assert body["tools"] == TOOLS
    # The contract in CLAUDE.md: Claude only through the global. inference profile.
    assert invoke.call_args.kwargs["modelId"] == bedrock.config.HAIKU_MODEL_ID
    assert bedrock.config.HAIKU_MODEL_ID.startswith("global.")


def test_a_refused_tool_is_reported_to_the_model_rather_than_raised(invoke):
    """An RBAC denial has to reach the user as prose, not as a 500."""
    invoke.side_effect = _turns(
        _payload(
            [{"type": "tool_use", "id": "tu-1", "name": "search_audit", "input": {}}],
            stop_reason="tool_use",
        ),
        _payload([{"type": "text", "text": "Only Tenant Admins may read the audit trail."}]),
    )

    def dispatch(name, args):
        raise bedrock.ToolRefused("Only Tenant Admins may read the audit trail.")

    result = _run(dispatch)

    assert result.tool_calls[0].refused is True
    second = json.loads(invoke.call_args_list[1].kwargs["body"])
    block = second["messages"][2]["content"][0]
    assert block["is_error"] is True
    assert "Tenant Admins" in block["content"]


def test_a_model_that_never_stops_asking_for_tools_is_capped(invoke):
    """Without a cap a looping model bills a request per turn until Lambda times
    out, and the caller cannot tell that from a slow answer."""
    invoke.side_effect = _turns(
        *[
            _payload(
                [{"type": "tool_use", "id": f"tu-{i}", "name": "list_incidents", "input": {}}],
                stop_reason="tool_use",
            )
            for i in range(6)
        ]
    )

    with pytest.raises(bedrock.ToolLoopExhausted):
        _run(lambda name, args: {"alerts": []}, max_iterations=3)

    assert invoke.call_count == 3


def test_several_tool_calls_in_one_turn_are_all_answered(invoke):
    """The model may batch; one tool_result per tool_use or the next turn is
    rejected by the API for an unanswered tool_use block."""
    invoke.side_effect = _turns(
        _payload(
            [
                {"type": "tool_use", "id": "tu-1", "name": "list_incidents", "input": {}},
                {"type": "tool_use", "id": "tu-2", "name": "list_incidents", "input": {"limit": 1}},
            ],
            stop_reason="tool_use",
        ),
        _payload([{"type": "text", "text": "done"}]),
    )
    result = _run(lambda name, args: {"alerts": []})

    second = json.loads(invoke.call_args_list[1].kwargs["body"])
    ids = [b["tool_use_id"] for b in second["messages"][2]["content"]]
    assert ids == ["tu-1", "tu-2"]
    assert len(result.tool_calls) == 2


def test_text_alongside_a_final_answer_is_concatenated(invoke):
    invoke.side_effect = _turns(
        _payload([{"type": "text", "text": "First. "}, {"type": "text", "text": "Second."}])
    )
    assert _run(lambda name, args: {}).text == "First. Second."


def test_a_tool_result_is_json_so_the_model_sees_structure_not_a_repr(invoke):
    """A Python repr of a DynamoDB item carries Decimal(...) and single quotes,
    which the model then quotes back verbatim into its answer."""
    invoke.side_effect = _turns(
        _payload(
            [{"type": "tool_use", "id": "tu-1", "name": "list_incidents", "input": {}}],
            stop_reason="tool_use",
        ),
        _payload([{"type": "text", "text": "ok"}]),
    )
    _run(lambda name, args: {"alerts": [{"received_at": 1700000000}]})

    body = json.loads(invoke.call_args_list[1].kwargs["body"])
    content = body["messages"][2]["content"][0]["content"]
    assert json.loads(content) == {"alerts": [{"received_at": 1700000000}]}
