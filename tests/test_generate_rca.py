import json
from unittest.mock import MagicMock, patch

import generate_rca
from common import config


def test_bedrock_model_id_and_diagnostics_put_keys():
    event = {
        "tenant_id": "acme",
        "alert_id": "alert-1",
        "alert": {"service": "checkout", "severity": "high", "description": "5xx spike"},
        "logs_context": {},
        "config_context": {},
        "rag_context": {"context_text": "", "similar_incidents": []},
    }

    captured = {}

    def fake_invoke_model(**kwargs):
        captured["modelId"] = kwargs["modelId"]
        payload = {
            "content": [
                {
                    "text": json.dumps(
                        {"rca_summary": "root cause", "remediation_steps": ["restart pod"]}
                    )
                }
            ]
        }
        return {"body": MagicMock(read=lambda: json.dumps(payload).encode("utf-8"))}

    table = MagicMock()

    with patch.object(generate_rca.bedrock._bedrock, "invoke_model", side_effect=fake_invoke_model):
        with patch.object(generate_rca.crypto, "encrypt_field", side_effect=lambda t, v: v):
            with patch.object(
                generate_rca.tenant_scope, "tenant_dynamodb_resource"
            ) as mock_resource:
                mock_resource.return_value.Table.return_value = table
                result = generate_rca.handler(event, None)

    assert captured["modelId"] == config.HAIKU_MODEL_ID == "global.anthropic.claude-haiku-4-5-20251001-v1:0"
    table.put_item.assert_called_once()
    item = table.put_item.call_args.kwargs["Item"]
    assert item["tenant_id"] == "acme"
    assert item["sk"] == "diag#alert-1"
    assert result["rca_summary"] == "root cause"
