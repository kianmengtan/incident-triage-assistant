"""Per-tenant integration credentials.

Nothing in the deployed system could ever populate these. fn-tenant-provision
created the secret with four empty objects and there was no write path anywhere,
so on any fresh deployment log correlation and config correlation both returned
"no platform configured" permanently, the IMS was never notified, and every
approval recorded execution_status "skipped" -- the product's headline feature
was unreachable.
"""
import json
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

import tenant_settings


def _event(resource="/v1/integrations", method="GET", body=None, params=None,
           tenant_id="acme", group="TenantAdmin"):
    return {
        "resource": resource,
        "httpMethod": method,
        "pathParameters": params or {},
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {
            "authorizer": {"tenant_id": tenant_id, "group": group, "principalId": "ada"}
        },
    }


@pytest.fixture
def secrets():
    client = MagicMock()
    client.get_secret_value.return_value = {
        "SecretString": json.dumps(
            {"log_platform": {"endpoint": "https://logs.example.com/q", "api_key": "old"},
             "vcs": {}, "remediation_platform": {}, "ims": {}}
        )
    }
    with patch.object(tenant_settings, "_secrets", client), \
         patch.object(tenant_settings.audit, "record_audit") as record:
        yield {"client": client, "audit": record}


# ---------------------------------------------------------------------------
# Authorisation
# ---------------------------------------------------------------------------
def test_a_missing_tenant_context_is_forbidden(secrets):
    assert tenant_settings.handler(_event(tenant_id=None), None)["statusCode"] == 403


def test_only_an_admin_may_read_the_integrations(secrets):
    resp = tenant_settings.handler(_event(group="TenantEngineer"), None)
    assert resp["statusCode"] == 403


def test_only_an_admin_may_write_an_integration(secrets):
    resp = tenant_settings.handler(
        _event(
            "/v1/integrations/{integration}", "PUT",
            body={"endpoint": "https://logs.example.com/q"},
            params={"integration": "log_platform"},
            group="TenantEngineer",
        ),
        None,
    )
    assert resp["statusCode"] == 403
    secrets["client"].put_secret_value.assert_not_called()


def test_a_refused_write_is_audited(secrets):
    tenant_settings.handler(
        _event(
            "/v1/integrations/{integration}", "PUT",
            body={"endpoint": "https://logs.example.com/q"},
            params={"integration": "log_platform"},
            group="TenantEngineer",
        ),
        None,
    )
    assert secrets["audit"].call_args.kwargs["result"] == "refused_not_admin"


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def test_the_api_key_is_never_returned(secrets):
    """The endpoint is the tenant's own configuration and useful to show. The key
    is a credential, and a read endpoint that echoes it turns any console XSS into
    credential exfiltration."""
    body = json.loads(tenant_settings.handler(_event(), None)["body"])

    log_platform = body["integrations"]["log_platform"]
    assert log_platform["endpoint"] == "https://logs.example.com/q"
    assert "api_key" not in log_platform
    assert log_platform["api_key_set"] is True


def test_an_unconfigured_integration_reports_no_key(secrets):
    body = json.loads(tenant_settings.handler(_event(), None)["body"])
    assert body["integrations"]["vcs"] == {"endpoint": None, "api_key_set": False}


def test_a_tenant_with_no_secret_yet_reads_as_empty(secrets):
    secrets["client"].get_secret_value.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "x"}}, "GetSecretValue"
    )
    body = json.loads(tenant_settings.handler(_event(), None)["body"])
    assert body["integrations"]["log_platform"]["api_key_set"] is False


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
def _put(integration="log_platform", body=None):
    return _event(
        "/v1/integrations/{integration}", "PUT",
        body=body if body is not None else {"endpoint": "https://logs.example.com/q", "api_key": "k"},
        params={"integration": integration},
    )


def test_an_integration_is_written_into_the_tenants_own_secret(secrets):
    resp = tenant_settings.handler(_put(), None)

    assert resp["statusCode"] == 200
    kwargs = secrets["client"].put_secret_value.call_args.kwargs
    assert kwargs["SecretId"] == "app-b9dac5ac-bc8fbf47-v2-tenant-acme-integration-creds"
    assert json.loads(kwargs["SecretString"])["log_platform"] == {
        "endpoint": "https://logs.example.com/q", "api_key": "k"
    }


def test_the_other_integrations_are_left_untouched(secrets):
    tenant_settings.handler(_put("vcs", {"endpoint": "https://vcs.example.com/c"}), None)

    written = json.loads(secrets["client"].put_secret_value.call_args.kwargs["SecretString"])
    assert written["log_platform"]["api_key"] == "old"
    assert written["vcs"]["endpoint"] == "https://vcs.example.com/c"


def test_an_unknown_integration_is_rejected(secrets):
    resp = tenant_settings.handler(_put("not_a_platform"), None)
    assert resp["statusCode"] == 400
    secrets["client"].put_secret_value.assert_not_called()


def test_a_literal_internal_address_is_refused_at_write_time(secrets):
    """Told immediately, rather than surfacing later as a degraded diagnosis with
    a note nobody can act on."""
    resp = tenant_settings.handler(
        _put(body={"endpoint": "https://169.254.169.254/latest/meta-data"}), None
    )

    assert resp["statusCode"] == 400
    assert "endpoint" in json.loads(resp["body"])["message"]
    secrets["client"].put_secret_value.assert_not_called()


def test_a_non_https_endpoint_is_rejected(secrets):
    assert tenant_settings.handler(_put(body={"endpoint": "http://logs.example.com/q"}), None)["statusCode"] == 400


def test_a_hostname_is_stored_without_resolving_it(secrets):
    """The write path must not depend on DNS: an endpoint that fails to resolve at
    this moment is not the same thing as one this system refuses to call, and where
    a name points is re-checked on every outbound call regardless."""
    with patch.object(tenant_settings.http, "_resolved_addresses") as resolve:
        resp = tenant_settings.handler(
            _put(body={"endpoint": "https://logs.does-not-resolve.invalid/q"}), None
        )

    assert resp["statusCode"] == 200
    resolve.assert_not_called()


def test_an_endpoint_is_required(secrets):
    assert tenant_settings.handler(_put(body={"api_key": "k"}), None)["statusCode"] == 400


def test_an_invalid_json_body_is_rejected(secrets):
    event = _put()
    event["body"] = "{not json"
    assert tenant_settings.handler(event, None)["statusCode"] == 400


def test_clearing_an_integration_removes_it(secrets):
    resp = tenant_settings.handler(
        _event("/v1/integrations/{integration}", "DELETE", params={"integration": "log_platform"}),
        None,
    )

    assert resp["statusCode"] == 200
    written = json.loads(secrets["client"].put_secret_value.call_args.kwargs["SecretString"])
    assert written["log_platform"] == {}


def test_a_successful_write_is_audited(secrets):
    tenant_settings.handler(_put(), None)

    kwargs = secrets["audit"].call_args.kwargs
    assert kwargs["action"] == "integration.update"
    assert kwargs["result"] == "log_platform"
    assert kwargs["actor"] == "ada"


def test_the_api_key_is_never_written_to_the_audit_trail(secrets):
    """The trail records that an integration changed and who changed it, never the
    credential itself: the audit table is read back by a wider group than the one
    allowed to set it."""
    tenant_settings.handler(
        _put(body={"endpoint": "https://logs.example.com/q", "api_key": "s3cret"}), None
    )

    recorded = json.dumps(secrets["audit"].call_args.kwargs)
    assert "s3cret" not in recorded
    assert "api_key" not in recorded


def test_an_unknown_route_is_404(secrets):
    assert tenant_settings.handler(_event("/v1/nope"), None)["statusCode"] == 404
