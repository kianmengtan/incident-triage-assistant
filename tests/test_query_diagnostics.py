from unittest.mock import MagicMock, patch

import query_diagnostics


def _event(tenant_id, resource, path_params=None):
    return {
        "requestContext": {"authorizer": {"tenant_id": tenant_id}},
        "resource": resource,
        "pathParameters": path_params or {},
        "queryStringParameters": {},
    }


def test_diagnostics_query_is_scoped_by_authorizer_tenant_id():
    table = MagicMock()
    table.get_item.return_value = {
        "Item": {
            "tenant_id": "acme",
            "sk": "diag#alert-1",
            "rca_summary": "cipher-rca",
            "remediation_steps": "cipher-steps",
        }
    }

    with patch.object(query_diagnostics.tenant_scope, "tenant_dynamodb_resource") as mock_resource:
        mock_resource.return_value.Table.return_value = table
        with patch.object(query_diagnostics.crypto, "decrypt_field", side_effect=lambda t, v: v):
            resp = query_diagnostics.handler(
                _event("acme", "/v1/diagnostics/{alertId}", {"alertId": "alert-1"}), None
            )

    assert resp["statusCode"] == 200
    table.get_item.assert_called_once_with(Key={"tenant_id": "acme", "sk": "diag#alert-1"})
    mock_resource.assert_called_once_with("acme")


def test_missing_tenant_context_is_forbidden():
    resp = query_diagnostics.handler(
        {"requestContext": {}, "resource": "/v1/runbooks", "pathParameters": {}, "queryStringParameters": {}},
        None,
    )
    assert resp["statusCode"] == 403
