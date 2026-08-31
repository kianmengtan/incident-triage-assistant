import json

import boto3

from . import config

_bedrock = boto3.client("bedrock-runtime", region_name=config.REGION)


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


def generate_text(system_prompt, user_prompt, max_tokens=2048):
    body = json.dumps(
        {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }
    )
    resp = _bedrock.invoke_model(
        modelId=config.HAIKU_MODEL_ID,
        body=body,
        contentType="application/json",
        accept="application/json",
    )
    payload = json.loads(resp["body"].read())
    return "".join(block.get("text", "") for block in payload.get("content", []))
