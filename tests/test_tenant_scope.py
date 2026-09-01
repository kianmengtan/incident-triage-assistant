"""Tenant-scoped credential vending. Previously untested, despite being the
mechanism the README describes as making cross-tenant leakage impossible."""
import datetime
from unittest.mock import MagicMock, patch

import pytest

from common import tenant_scope


@pytest.fixture(autouse=True)
def clear_cache():
    tenant_scope._cache.clear()
    yield
    tenant_scope._cache.clear()


def _credentials(lifetime_seconds):
    expiry = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        seconds=lifetime_seconds
    )
    return {
        "Credentials": {
            "AccessKeyId": "AKIAtest",
            "SecretAccessKey": "secret",
            "SessionToken": "token",
            "Expiration": expiry,
        }
    }


def _sts(lifetime_seconds=900):
    client = MagicMock()
    client.assume_role.return_value = _credentials(lifetime_seconds)
    return client


def test_the_session_is_tagged_with_the_tenant_id():
    """The tag is what the role's LeadingKeys condition matches on. Passing it
    also requires sts:TagSession in both policies, which the template grants."""
    sts = _sts()
    with patch.object(tenant_scope.boto3, "client", return_value=sts):
        tenant_scope.tenant_dynamodb_resource("acme")

    kwargs = sts.assume_role.call_args.kwargs
    assert kwargs["Tags"] == [{"Key": "tenant_id", "Value": "acme"}]
    assert kwargs["TransitiveTagKeys"] == ["tenant_id"]
    assert kwargs["RoleArn"] == tenant_scope.config.TENANT_SCOPED_ROLE_ARN


def test_each_tenant_gets_its_own_session():
    sts = _sts()
    with patch.object(tenant_scope.boto3, "client", return_value=sts):
        tenant_scope.tenant_dynamodb_resource("acme")
        tenant_scope.tenant_dynamodb_resource("globex")

    tags = [c.kwargs["Tags"][0]["Value"] for c in sts.assume_role.call_args_list]
    assert tags == ["acme", "globex"]


def test_a_session_is_reused_while_it_has_life_left():
    sts = _sts(lifetime_seconds=900)
    with patch.object(tenant_scope.boto3, "client", return_value=sts):
        tenant_scope.tenant_dynamodb_resource("acme")
        tenant_scope.tenant_dynamodb_resource("acme")
    assert sts.assume_role.call_count == 1


def test_a_nearly_expired_session_is_replaced():
    sts = _sts(lifetime_seconds=30)
    with patch.object(tenant_scope.boto3, "client", return_value=sts):
        tenant_scope.tenant_dynamodb_resource("acme")
        tenant_scope.tenant_dynamodb_resource("acme")
    assert sts.assume_role.call_count == 2


def test_a_signing_client_refuses_a_session_shorter_than_the_url_it_will_sign():
    """A presigned URL dies with the credentials that signed it. Signing a
    15-minute URL from a session with 40 seconds left produced a URL that looked
    valid for 15 minutes and stopped working after 40 seconds."""
    sts = _sts(lifetime_seconds=900)
    with patch.object(tenant_scope.boto3, "client", return_value=sts):
        tenant_scope.tenant_dynamodb_resource("acme")   # caches a 900s session
        assert sts.assume_role.call_count == 1
        tenant_scope.tenant_signing_s3_client("acme", 900)

    assert sts.assume_role.call_count == 2, "should not reuse a session it will outlive"
    assert sts.assume_role.call_args.kwargs["DurationSeconds"] >= 900


def test_a_requested_duration_never_exceeds_the_roles_max_session_duration():
    sts = _sts(lifetime_seconds=3600)
    with patch.object(tenant_scope.boto3, "client", return_value=sts):
        tenant_scope.tenant_signing_s3_client("acme", 3600)
    assert sts.assume_role.call_args.kwargs["DurationSeconds"] <= tenant_scope.MAX_SESSION_SECONDS


def test_object_keys_are_confined_to_the_tenants_prefix():
    """The S3 half of the role policy grants tenant/<tag>/* and nothing else."""
    assert tenant_scope.tenant_object_key("acme", "runbook", "rb-1.md") == (
        "tenant/acme/runbook/rb-1.md"
    )
    assert tenant_scope.tenant_object_key("acme", "alert", 42, "logs.json").startswith("tenant/acme/")
