from unittest.mock import patch

import authorizer


def _event(token="good-token"):
    return {
        "headers": {"Authorization": f"Bearer {token}"},
        "methodArn": "arn:aws:execute-api:ap-southeast-1:123456789012:abc123/prod/GET/v1/runbooks",
    }


def test_context_includes_tenant_id_and_group():
    with patch.object(
        authorizer._cognito,
        "get_user",
        return_value={
            "Username": "user-1",
            "UserAttributes": [{"Name": "custom:tenant_id", "Value": "acme"}],
        },
    ):
        with patch.object(
            authorizer._cognito,
            "admin_list_groups_for_user",
            return_value={"Groups": [{"GroupName": "TenantEngineer"}]},
        ):
            result = authorizer.handler(_event(), None)

    assert result["policyDocument"]["Statement"][0]["Effect"] == "Allow"
    assert result["context"]["tenant_id"] == "acme"
    assert result["context"]["group"] == "TenantEngineer"


def test_missing_token_denied():
    event = _event()
    event["headers"] = {}
    result = authorizer.handler(event, None)
    assert result["policyDocument"]["Statement"][0]["Effect"] == "Deny"
