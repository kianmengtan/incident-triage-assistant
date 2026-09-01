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


def _invoke_claude(system_prompt, messages, max_tokens):
    body = json.dumps(
        {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": messages,
        }
    )
    resp = _bedrock.invoke_model(
        modelId=config.HAIKU_MODEL_ID,
        body=body,
        contentType="application/json",
        accept="application/json",
    )
    payload = json.loads(resp["body"].read())
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
