"""The one-off repair for users confirmed while provisioning was broken.

Cognito runs PostConfirmation once per user, so fixing the trigger does nothing
for the accounts it already failed on. These cover the selection logic (whose
account is actually broken) and the event shape, because the script drives the
real trigger rather than reimplementing it.
"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import backfill_tenants as backfill  # noqa: E402
import tenant_provision  # noqa: E402


def _user(username="ada", email="ada@acme-retail.com", status="CONFIRMED",
          tenant=None, enabled=True, sub="sub-1"):
    attrs = [{"Name": "email", "Value": email}, {"Name": "sub", "Value": sub}]
    if tenant:
        attrs.append({"Name": "custom:tenant_id", "Value": tenant})
    return {"Username": username, "UserStatus": status, "Enabled": enabled, "Attributes": attrs}


def _cognito(users, after_attrs=None):
    client = MagicMock()
    client.list_users.return_value = {"Users": users}
    client.admin_get_user.return_value = {
        "UserAttributes": after_attrs
        if after_attrs is not None
        else [{"Name": "custom:tenant_id", "Value": "acme-retail-com"}]
    }
    return client


# ---------------------------------------------------------------- selection

def test_a_confirmed_user_with_no_tenant_needs_repair():
    assert backfill.needs_backfill(_user()) is True


def test_a_user_who_already_has_a_tenant_is_left_alone():
    assert backfill.needs_backfill(_user(tenant="acme-retail-com")) is False


@pytest.mark.parametrize("status", ["UNCONFIRMED", "RESET_REQUIRED", "FORCE_CHANGE_PASSWORD"])
def test_an_unconfirmed_user_is_not_repaired(status):
    """PostConfirmation has not run for them yet, and the fixed trigger will."""
    assert backfill.needs_backfill(_user(status=status)) is False


def test_a_disabled_user_is_not_repaired():
    assert backfill.needs_backfill(_user(enabled=False)) is False


def test_attributes_reads_both_shapes_the_cognito_apis_return():
    """list_users says Attributes, admin_get_user says UserAttributes."""
    assert backfill.attributes({"Attributes": [{"Name": "email", "Value": "a@b.co"}]})["email"] == "a@b.co"
    assert backfill.attributes({"UserAttributes": [{"Name": "email", "Value": "a@b.co"}]})["email"] == "a@b.co"
    assert backfill.attributes({}) == {}


# ---------------------------------------------------------------- pagination

def test_every_page_of_users_is_read():
    client = MagicMock()
    client.list_users.side_effect = [
        {"Users": [_user("a")], "PaginationToken": "t1"},
        {"Users": [_user("b")]},
    ]
    assert [u["Username"] for u in backfill.list_all_users(client, "pool")] == ["a", "b"]


# ---------------------------------------------------------------- dry run

def test_a_dry_run_provisions_nothing():
    provision = MagicMock()
    repaired, _, failed = backfill.backfill(
        _cognito([_user()]), "pool", provision, apply=False, out=lambda *a: None
    )
    provision.assert_not_called()
    assert repaired == ["ada"] and failed == []


# ---------------------------------------------------------------- applying

def test_applying_provisions_the_user_and_confirms_it_took():
    provision = MagicMock()
    repaired, _, failed = backfill.backfill(
        _cognito([_user()]), "pool", provision, apply=True, out=lambda *a: None
    )
    assert provision.call_count == 1
    assert repaired == ["ada"] and failed == []


def test_a_silent_provisioning_failure_is_reported_not_counted_as_repaired():
    """The trigger swallows ClientError by design, so the script has to verify by
    re-reading the user rather than trusting that the call did not raise."""
    provision = MagicMock()
    repaired, _, failed = backfill.backfill(
        _cognito([_user()], after_attrs=[]), "pool", provision, apply=True, out=lambda *a: None
    )
    assert repaired == [] and failed == ["ada"]


def test_an_address_with_no_derivable_tenant_is_flagged_and_never_provisioned():
    provision = MagicMock()
    repaired, _, failed = backfill.backfill(
        _cognito([_user(email="someone@gmail.com")]), "pool", provision,
        apply=True, out=lambda *a: None,
    )
    provision.assert_not_called()
    assert failed == ["ada"] and repaired == []


def test_an_already_scoped_user_is_skipped_without_provisioning():
    provision = MagicMock()
    repaired, skipped, failed = backfill.backfill(
        _cognito([_user(tenant="acme-retail-com")]), "pool", provision,
        apply=True, out=lambda *a: None,
    )
    provision.assert_not_called()
    assert skipped == ["ada"] and repaired == [] and failed == []


# ---------------------------------------------------------------- event shape

def test_the_synthesised_event_drives_the_real_trigger():
    """The point of the script: this event must be what the trigger reads. If the
    handler's expectations move, this fails instead of the backfill silently
    provisioning nobody."""
    event = backfill.post_confirmation_event(_user(), "pool-1")
    table = MagicMock()
    with patch.object(tenant_provision, "_dynamodb") as dynamodb, \
         patch.object(tenant_provision.paramstore, "create_if_missing"), \
         patch.object(tenant_provision, "_cognito") as cognito:
        dynamodb.Table.return_value = table
        tenant_provision.handler(event, None)

    cognito.admin_update_user_attributes.assert_called_once()
    kwargs = cognito.admin_update_user_attributes.call_args.kwargs
    assert kwargs["UserPoolId"] == "pool-1"
    assert kwargs["Username"] == "ada"
    assert kwargs["UserAttributes"] == [
        {"Name": "custom:tenant_id", "Value": "acme-retail-com"}
    ]
    cognito.admin_add_user_to_group.assert_called_once()
