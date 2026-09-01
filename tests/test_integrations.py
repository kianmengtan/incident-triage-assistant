"""The shared per-tenant integration credential lookup, which replaced four
identical copies of itself across four handlers."""
import json
from unittest.mock import patch

from botocore.exceptions import ClientError

from common import integrations

SECRET = json.dumps(
    {
        "log_platform": {"endpoint": "https://logs.example.com", "api_key": "k1"},
        "vcs": {"endpoint": "https://git.example.com"},
        "remediation_platform": {},
        "ims": {},
    }
)


def _secret(value=SECRET):
    return patch.object(integrations._secrets, "get_secret_value", return_value={"SecretString": value})


def test_each_integration_is_read_from_the_one_tenant_secret():
    with _secret() as mock:
        assert integrations.creds("acme", integrations.LOG_PLATFORM)["api_key"] == "k1"
        assert integrations.creds("acme", integrations.VCS)["endpoint"] == "https://git.example.com"

    assert mock.call_args.kwargs["SecretId"] == "app-b9dac5ac-bc8fbf47-v2-tenant-acme-integration-creds"


def test_an_unconfigured_integration_is_an_empty_dict():
    with _secret():
        assert integrations.creds("acme", integrations.IMS) == {}
        assert integrations.creds("acme", "nonexistent") == {}


def test_a_missing_secret_degrades_to_empty_rather_than_raising():
    with patch.object(
        integrations._secrets, "get_secret_value",
        side_effect=ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "x"}}, "GetSecretValue"),
    ):
        assert integrations.creds("acme", integrations.LOG_PLATFORM) == {}


def test_a_corrupt_secret_degrades_to_empty():
    with _secret("not json at all"):
        assert integrations.creds("acme", integrations.LOG_PLATFORM) == {}


def test_a_null_integration_value_is_normalised_to_a_dict():
    with _secret(json.dumps({"ims": None})):
        assert integrations.creds("acme", integrations.IMS) == {}
