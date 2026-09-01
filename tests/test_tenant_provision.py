"""Where a user's tenant is decided. This is the multi-tenancy boundary: it used
to be a client-writable signup attribute, so with self-signup enabled anyone
could claim an existing tenant's id and read its incidents."""
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

import tenant_provision


def _event(email="ada@acme-retail.com", username="ada"):
    return {
        "userPoolId": "ap-southeast-1_testpool",
        "userName": username,
        "request": {"userAttributes": {"email": email}},
    }


def _exists_error(code="ResourceExistsException"):
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "CreateSecret")


def _conditional_failure():
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}}, "PutItem"
    )


@pytest.fixture
def harness():
    table = MagicMock()
    with patch.object(tenant_provision, "_dynamodb") as dynamodb, \
         patch.object(tenant_provision, "_secrets") as secrets, \
         patch.object(tenant_provision, "_cognito") as cognito:
        dynamodb.Table.return_value = table
        yield {"table": table, "secrets": secrets, "cognito": cognito}


# ------------------------------------------------------------------- derivation


def test_the_tenant_is_derived_from_the_email_domain():
    assert tenant_provision.tenant_id_for_email("ada@acme-retail.com") == "acme-retail-com"
    assert tenant_provision.tenant_id_for_email("ADA@Acme-Retail.COM") == "acme-retail-com"
    assert tenant_provision.tenant_id_for_email("x@sub.example.co.uk") == "sub-example-co-uk"


def test_the_derived_id_is_safe_as_a_key_a_secret_name_and_a_tag_value():
    derived = tenant_provision.tenant_id_for_email("x@weird_domain!!.example.com")
    assert derived is not None
    assert all(c.isalnum() or c == "-" for c in derived), derived
    assert not derived.startswith("-") and not derived.endswith("-")


def test_consumer_mail_domains_get_no_tenant():
    """Otherwise every gmail.com signup shares one tenant, and each other's
    incidents."""
    for email in ["a@gmail.com", "b@outlook.com", "c@yahoo.com", "d@proton.me"]:
        assert tenant_provision.tenant_id_for_email(email) is None


def test_a_missing_or_malformed_email_gets_no_tenant():
    for email in [None, "", "not-an-email", "@nodomain.com", "trailing@"]:
        assert tenant_provision.tenant_id_for_email(email) is None


def test_a_user_with_no_derivable_tenant_is_left_unscoped(harness):
    """Fail closed: no attribute is written, so the authorizer denies them."""
    event = tenant_provision.handler(_event(email="someone@gmail.com"), None)

    assert event is not None
    harness["cognito"].admin_update_user_attributes.assert_not_called()
    harness["cognito"].admin_add_user_to_group.assert_not_called()
    harness["table"].put_item.assert_not_called()


# ------------------------------------------------------------------ first user


def test_the_first_user_of_a_tenant_becomes_its_admin(harness):
    """Nothing else grants group membership, so without this the approval path
    is unreachable: every approval would 403 forever."""
    tenant_provision.handler(_event(), None)

    harness["cognito"].admin_add_user_to_group.assert_called_once()
    assert harness["cognito"].admin_add_user_to_group.call_args.kwargs["GroupName"] == "TenantAdmin"


def test_a_later_user_of_the_same_tenant_is_an_engineer(harness):
    harness["table"].put_item.side_effect = _conditional_failure()

    tenant_provision.handler(_event(email="bob@acme-retail.com", username="bob"), None)

    assert harness["cognito"].admin_add_user_to_group.call_args.kwargs["GroupName"] == "TenantEngineer"


def test_the_tenant_id_is_written_back_onto_the_user(harness):
    tenant_provision.handler(_event(), None)

    kwargs = harness["cognito"].admin_update_user_attributes.call_args.kwargs
    assert kwargs["UserAttributes"] == [{"Name": "custom:tenant_id", "Value": "acme-retail-com"}]


# ----------------------------------------------------------- ordering and wedge


def test_secrets_are_created_before_the_profile_row(harness):
    """Order is the fix for a permanent wedge: the row marks the tenant as
    provisioned, so writing it first and then failing to create the DEK left the
    tenant unable to ingest or diagnose anything, with every later signup
    short-circuiting on "already provisioned"."""
    order = []
    harness["secrets"].create_secret.side_effect = lambda **kw: order.append("secret")
    harness["table"].put_item.side_effect = lambda **kw: order.append("row")

    tenant_provision.handler(_event(), None)

    assert order.index("row") > order.index("secret"), order
    assert order.count("secret") == 3


def test_a_secret_failure_leaves_the_tenant_unclaimed_so_the_next_signup_retries(harness):
    harness["secrets"].create_secret.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "CreateSecret"
    )

    tenant_provision.handler(_event(), None)

    harness["table"].put_item.assert_not_called()


def test_existing_secrets_are_tolerated(harness):
    """A re-run over already-provisioned secrets still claims the tenant.

    Asserts the PROFILE row specifically rather than a total put count: the
    handler also writes a USER# roster row for fn-team, so counting calls would
    break on an unrelated change.
    """
    harness["secrets"].create_secret.side_effect = _exists_error()

    tenant_provision.handler(_event(), None)

    written = [c.kwargs["Item"]["sk"] for c in harness["table"].put_item.call_args_list]
    assert "PROFILE" in written


def test_all_three_tenant_secrets_are_provisioned(harness):
    tenant_provision.handler(_event(), None)

    names = [c.kwargs["Name"] for c in harness["secrets"].create_secret.call_args_list]
    assert any(n.endswith("-dek") for n in names)
    assert any(n.endswith("-ingest-hmac") for n in names)
    assert any(n.endswith("-integration-creds") for n in names)
    assert all(n.startswith("app-b9dac5ac-bc8fbf47-tenant-acme-retail-com") for n in names)


# ------------------------------------------------------------------- never raise


def test_a_cognito_failure_does_not_raise_into_the_signup_flow(harness):
    """This trigger runs AFTER confirmation, so raising returns an error to a
    client that cannot retry confirmation and fixes nothing."""
    harness["cognito"].admin_add_user_to_group.side_effect = ClientError(
        {"Error": {"Code": "TooManyRequestsException", "Message": "x"}}, "AdminAddUserToGroup"
    )

    result = tenant_provision.handler(_event(), None)

    assert result is not None


# ------------------------------------------------------------- roster for fn-team


def _member_rows(harness):
    return [
        c.kwargs["Item"]
        for c in harness["table"].put_item.call_args_list
        if str(c.kwargs["Item"].get("sk", "")).startswith("USER#")
    ]


def test_a_roster_row_is_written_so_the_team_screen_has_something_to_list(harness):
    """fn-team cannot use Cognito ListUsers: it cannot filter on a custom
    attribute, so finding one tenant's users would mean paging the whole pool."""
    event = _event()
    event["request"]["userAttributes"]["sub"] = "s-ada"

    tenant_provision.handler(event, None)

    rows = _member_rows(harness)
    assert len(rows) == 1
    row = rows[0]
    assert row["sk"] == "USER#s-ada"
    assert row["tenant_id"] == "acme-retail-com"
    assert row["email"] == "ada@acme-retail.com"
    assert row["role"] == "TenantAdmin"


def test_the_roster_row_stores_the_username_cognito_admin_calls_need(harness):
    """fn-team promotes a user by the stored username rather than by an
    identifier taken from the request URL."""
    event = _event()
    event["request"]["userAttributes"]["sub"] = "s-ada"

    tenant_provision.handler(event, None)

    assert _member_rows(harness)[0]["username"] == "ada"


def test_the_roster_row_records_engineer_for_a_later_signup(harness):
    """The stored role has to match the group actually assigned, or the team
    screen shows one thing and the token carries another."""
    harness["table"].put_item.side_effect = [_conditional_failure(), None]
    event = _event(email="bob@acme-retail.com", username="bob")
    event["request"]["userAttributes"]["sub"] = "s-bob"

    tenant_provision.handler(event, None)

    assert _member_rows(harness)[0]["role"] == "TenantEngineer"


def test_a_user_with_no_derivable_tenant_gets_no_roster_row(harness):
    tenant_provision.handler(_event(email="someone@gmail.com"), None)
    assert _member_rows(harness) == []


def test_a_failed_roster_write_does_not_break_provisioning(harness):
    """The account is already confirmed and in its group by this point; losing
    the row costs a line in the team list, not access."""
    harness["table"].put_item.side_effect = [None, _conditional_failure()]

    event = tenant_provision.handler(_event(), None)

    assert event is not None
    harness["cognito"].admin_add_user_to_group.assert_called_once()
