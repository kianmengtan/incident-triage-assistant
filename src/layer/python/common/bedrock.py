"""Bedrock calls, restricted to the platform's approved model ids.

Claude must be invoked through the ``global.`` cross-region inference profile;
Cohere Embed is the one local model and takes its bare id. Both ids live in
config so there is a single place to check them against the contract.
"""
import json
import logging
from collections import namedtuple

import boto3

from . import config

logger = logging.getLogger(__name__)

_bedrock = boto3.client("bedrock-runtime", region_name=config.REGION)

ModelResponse = namedtuple("ModelResponse", ["text", "stop_reason"])


class TruncatedResponse(RuntimeError):
    """The model hit max_tokens, so its output is incomplete."""


class InvalidModelJson(ValueError):
    """The model was asked for JSON and did not produce parseable JSON."""


def embed_texts(texts, input_type="search_document"):
    """Embed up to 96 texts with cohere.embed-multilingual-v3.

    Each text is truncated to MAX_EMBED_INPUT_CHARS before being sent, per
    the platform's approved-model contract.
    """
    truncated = [t[: config.MAX_EMBED_INPUT_CHARS] for t in texts]
    body = json.dumps({"texts": truncated, "input_type": input_type})
    resp = _bedrock.invoke_model(
        modelId=config.EMBED_MODEL_ID,
        body=body,
        contentType="application/json",
        accept="application/json",
    )
    payload = json.loads(resp["body"].read())
    return payload["embeddings"]


def _invoke_raw(system_prompt, messages, max_tokens, tools=None):
    """One Bedrock turn, returned as the raw payload.

    Kept separate from _invoke_claude because a tool-use caller needs the
    content blocks themselves -- a tool_use block carries an id and an input
    object, and flattening the turn to text throws both away.
    """
    request = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": messages,
    }
    if tools:
        request["tools"] = tools
    resp = _bedrock.invoke_model(
        modelId=config.HAIKU_MODEL_ID,
        body=json.dumps(request),
        contentType="application/json",
        accept="application/json",
    )
    return json.loads(resp["body"].read())


def _invoke_claude(system_prompt, messages, max_tokens):
    payload = _invoke_raw(system_prompt, messages, max_tokens)
    text = "".join(block.get("text", "") for block in payload.get("content", []))
    return ModelResponse(text=text, stop_reason=payload.get("stop_reason"))


def generate_text(system_prompt, user_prompt, max_tokens=2048):
    """Free-form generation. Returns a ModelResponse, stop_reason included.

    Callers must look at stop_reason: a max_tokens stop means the text is cut
    off mid-sentence, which for a runbook means missing remediation steps.
    """
    return _invoke_claude(
        system_prompt, [{"role": "user", "content": user_prompt}], max_tokens
    )


def generate_json(system_prompt, user_prompt, max_tokens=2048):
    """Generation constrained to a JSON object.

    The assistant turn is prefilled with ``{`` so the model cannot open with
    prose ("Here is the analysis: ..."), which is the usual reason a
    JSON-shaped prompt comes back unparseable. Truncation and malformed output
    are raised rather than returned, because the fallback of treating a partial
    JSON blob as prose puts raw JSON in front of an on-call engineer.
    """
    response = _invoke_claude(
        system_prompt,
        [
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": "{"},
        ],
        max_tokens,
    )
    if response.stop_reason == "max_tokens":
        raise TruncatedResponse(
            f"model hit max_tokens={max_tokens} before closing its JSON object"
        )
    try:
        return json.loads("{" + response.text)
    except json.JSONDecodeError as exc:
        raise InvalidModelJson(f"model did not return valid JSON: {exc}") from exc


# ---------------------------------------------------------------------------
# Tool use
# ---------------------------------------------------------------------------
#: One tool the model asked for. ``refused`` records that policy -- not a bug --
#: stopped it, which is what a caller reports back to the user.
ToolCall = namedtuple("ToolCall", ["name", "arguments", "refused"])

ToolConversation = namedtuple(
    "ToolConversation", ["text", "tool_calls", "stop_reason", "iterations"]
)


class ToolRefused(Exception):
    """A tool call that policy declines: unknown name, missing argument, or a
    capability the caller does not hold.

    Raised by a dispatch function and handed back to the model as a tool_result
    rather than propagating. A refusal is information the model should relay
    ("only Tenant Admins may read the audit trail"), so turning it into a 500
    would replace a usable answer with an outage.
    """


class ToolLoopExhausted(RuntimeError):
    """The model kept asking for tools past max_iterations.

    Raised rather than returning whatever text had accumulated: a partial answer
    that reads as complete is worse than a visible failure, and each extra turn
    is another billed request against a Lambda timeout.
    """


def run_tool_conversation(
    system_prompt, messages, tools, dispatch, max_tokens=1024, max_iterations=4
):
    """Run Claude with tools until it answers in prose.

    ``dispatch(name, arguments)`` performs one tool call and returns anything
    JSON-serialisable; it raises ToolRefused for a call policy declines. The
    loop owns three things the callers should not each reinvent: pairing every
    tool_use block with exactly one tool_result (the API rejects the next turn
    otherwise), serialising results as JSON rather than as Python reprs, and
    terminating.
    """
    conversation = [dict(message) for message in messages]
    calls = []

    for iteration in range(1, max_iterations + 1):
        payload = _invoke_raw(system_prompt, conversation, max_tokens, tools)
        blocks = payload.get("content", []) or []
        stop_reason = payload.get("stop_reason")

        if stop_reason != "tool_use":
            text = "".join(
                block.get("text", "")
                for block in blocks
                if block.get("type", "text") == "text"
            )
            return ToolConversation(
                text=text,
                tool_calls=calls,
                stop_reason=stop_reason,
                iterations=iteration,
            )

        # The assistant's turn goes back verbatim: the tool_use ids in it are
        # what the tool_result blocks below refer to.
        conversation.append({"role": "assistant", "content": blocks})

        results = []
        for block in blocks:
            if block.get("type") != "tool_use":
                continue
            name = block.get("name")
            arguments = block.get("input") or {}
            try:
                output = dispatch(name, arguments)
                refused = False
            except ToolRefused as exc:
                output = {"error": str(exc)}
                refused = True
            calls.append(ToolCall(name=name, arguments=arguments, refused=refused))
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.get("id"),
                    "content": json.dumps(output, default=str),
                    "is_error": refused,
                }
            )

        conversation.append({"role": "user", "content": results})

    raise ToolLoopExhausted(
        f"model still requesting tools after {max_iterations} turns"
    )
