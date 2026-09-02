"""The SSM-backed store that replaced Secrets Manager for per-tenant material.

The bug these cover: the deploy boundary allows no secretsmanager:CreateSecret,
so provisioning a tenant's DEK failed on every signup and left the user without
custom:tenant_id. See common/paramstore.py's module docstring.
"""
import json
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError

from common import config, paramstore


def _already_exists():
    return paramstore._ssm.exceptions.ParameterAlreadyExists(
        {"Error": {"Code": "ParameterAlreadyExists", "Message": "exists"}},
        "PutParameter",
    )


def _not_found():
    return paramstore._ssm.exceptions.ParameterNotFound(
        {"Error": {"Code": "ParameterNotFound", "Message": "absent"}},
        "GetParameter",
    )


# ---------- naming ----------

@pytest.mark.parametrize("kind", paramstore.KINDS)
def test_every_kind_lands_under_this_projects_reserved_path(kind):
    """The boundary allows parameter/app-* and the contract reserves
    /app-b9dac5ac-bc8fbf47/. A name outside it is denied at runtime."""
    name = paramstore.parameter_name("acme", kind)
    assert name == f"/{config.PREFIX}/tenant/acme/{kind}"
    assert name.startswith("/app-")


def test_each_kind_gets_a_distinct_parameter():
    names = {paramstore.parameter_name("acme", k) for k in paramstore.KINDS}
    assert len(names) == len(paramstore.KINDS)


def test_two_tenants_never_share_a_parameter():
    assert paramstore.parameter_name("acme", paramstore.DEK) != paramstore.parameter_name(
        "globex", paramstore.DEK
    )


@pytest.mark.parametrize(
    "tenant_id",
    ["../other", "acme/dek", "", "ACME", "-acme", "a" * 121, None, 7, "acme.com"],
)
def test_a_tenant_id_that_could_escape_its_subtree_is_refused(tenant_id):
    """tenant_id becomes a path segment, so this is the last place that can
    refuse one that addresses another tenant's material."""
    with pytest.raises(ValueError):
        paramstore.parameter_name(tenant_id, paramstore.DEK)


def test_an_unknown_kind_is_refused():
    with pytest.raises(ValueError):
        paramstore.parameter_name("acme", "root-password")


# ---------- read ----------

def test_read_returns_the_stored_value():
    with patch.object(
        paramstore._ssm, "get_parameter", return_value={"Parameter": {"Value": "v"}}
    ) as mock:
        assert paramstore.read("acme", paramstore.DEK) == "v"
    mock.assert_called_once_with(Name=f"/{config.PREFIX}/tenant/acme/dek")


def test_a_missing_parameter_raises_a_clienterror_callers_already_catch():
    """ingest_normalize and integrations catch ClientError to make an unknown
    tenant indistinguishable from an unreadable one. ParameterNotFound has to
    keep satisfying that except clause."""
    with patch.object(paramstore._ssm, "get_parameter", side_effect=_not_found()):
        with pytest.raises(ClientError):
            paramstore.read("acme", paramstore.DEK)


# ---------- write ----------

def test_write_replaces_an_existing_value():
    with patch.object(paramstore._ssm, "put_parameter") as mock:
        paramstore.write("acme", paramstore.INTEGRATION_CREDS, json.dumps({"vcs": {}}))
    kwargs = mock.call_args.kwargs
    assert kwargs["Name"] == f"/{config.PREFIX}/tenant/acme/integration-creds"
    assert kwargs["Overwrite"] is True
    assert kwargs["Type"] == "String"


def test_write_never_asks_for_securestring():
    """The boundary allows no kms:*, so a SecureString write is denied."""
    with patch.object(paramstore._ssm, "put_parameter") as mock:
        paramstore.write("acme", paramstore.DEK, "k")
    assert mock.call_args.kwargs["Type"] == "String"
    assert "KeyId" not in mock.call_args.kwargs


# ---------- create_if_missing ----------

def test_create_if_missing_creates_and_reports_it_did():
    with patch.object(paramstore._ssm, "put_parameter") as mock:
        assert paramstore.create_if_missing("acme", paramstore.DEK, "k") is True
    assert mock.call_args.kwargs["Overwrite"] is False


def test_create_if_missing_keeps_an_existing_dek():
    """Overwriting a DEK that already has ciphertext under it would strand every
    field already encrypted for the tenant, so a second signup must not."""
    with patch.object(paramstore._ssm, "put_parameter", side_effect=_already_exists()):
        assert paramstore.create_if_missing("acme", paramstore.DEK, "new") is False


def test_create_if_missing_does_not_swallow_a_denial():
    """An AccessDenied here is the failure that caused this rewrite; it must
    surface rather than look like 'already provisioned'."""
    denied = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "PutParameter"
    )
    with patch.object(paramstore._ssm, "put_parameter", side_effect=denied):
        with pytest.raises(ClientError):
            paramstore.create_if_missing("acme", paramstore.DEK, "k")
