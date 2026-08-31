from unittest.mock import MagicMock, patch

import rag_context
from common import config


def test_embedding_uses_approved_model_and_caps_input_length():
    event = {
        "tenant_id": "acme",
        "alert_id": "alert-1",
        "alert": {"service": "checkout", "severity": "high", "description": "x" * 5000},
    }

    captured = {}

    def fake_invoke_model(**kwargs):
        import json

        captured["modelId"] = kwargs["modelId"]
        captured["body"] = json.loads(kwargs["body"])
        return {
            "body": MagicMock(
                read=lambda: json.dumps({"embeddings": [[0.1, 0.2, 0.3]]}).encode("utf-8")
            )
        }

    with patch.object(rag_context.bedrock._bedrock, "invoke_model", side_effect=fake_invoke_model):
        with patch.object(rag_context._s3vectors, "query_vectors", side_effect=Exception("empty index")):
            with patch.object(rag_context._s3vectors, "put_vectors"):
                result = rag_context.handler(event, None)

    assert captured["modelId"] == config.EMBED_MODEL_ID == "cohere.embed-multilingual-v3"
    assert all(len(t) <= config.MAX_EMBED_INPUT_CHARS for t in captured["body"]["texts"])
    assert result["similar_incidents"] == []
