"""fn-team -- listing teammates and changing their roles.

Two guards here are the whole reason this handler is not three lines.

**The target must be in the caller's own tenant.** Cognito's admin APIs are
account-wide: ``admin_add_user_to_group`` will happily promote any user in the
pool. The only thing stopping an admin of one tenant from promoting -- or
demoting -- a user in another is that this handler looks the target up in its own
tenant's partition first and refuses if it is not there.

**The last admin cannot be demoted.** Group membership is granted by the
PostConfirmation trigger, which only makes the *first* user of a tenant an admin.
A tenant that demotes its only admin therefore has no way to ever get one back,
and approving remediation -- the one thing only an admin may do -- becomes
permanently unreachable for that tenant.

Cognito ListUsers cannot filter on a custom attribute, so the roster comes from
member rows in the Tenants table instead. Those rows live in the tenant's own
partition, so the IAM session tagging that scopes every other table read applies
to them too.
"""
import json
from unittest.mock import MagicMock, patch

import pytest

import team
from common import rbac


def _event(
    resource="/v1/team",
    method="GET",
    tenant_id="acme",
    group=rbac.TENANT_ADMIN,
    path_params=None,
    body=None,
    principal="admin-1",
):
    authorizer = {"principalId": principal}
    if tenant_id is not None:
        authorizer["tenant_id"] = tenant_id
    if group is not None:
        authorizer["group"] = group
    return {
        "resource": resource,
        "httpMethod": method,
        "requestContext": {"authorizer": authorizer},
        "pathParameters": path_params or {},
        "body": json.dumps(body) if body is not None else None,
    }


def _member(sub, email, role, username=None):
    return {
        "tenant_id": "acme",
        "sk": f"USER#{sub}",
        "sub": sub,
        "username": username or email,
        "email": email,
        "role": role,
        "created_at": 1000,
    }


ADMIN = _member("s-admin", "ada@acme.com", rbac.TENANT_ADMIN)
ENGINEER = _member("s-eng", "bob@acme.com", rbac.TENANT_ENGINEER)
LEAD = _member("s-lead", "cleo@acme.com", rbac.TENANT_LEADERSHIP)


@pytest.fixture
def table():
    t = MagicMock()
    t.query.return_value = {"Items": [ADMIN, ENGINEER]}
    t.get_item.return_value = {"Item": ENGINEER}
    return t


@pytest.fixture
def harness(table):
    with patch.object(team.tenant_scope, "tenant_dynamodb_resource") as resource, \
         patch.object(team, "_cognito") as cognito, \
         patch.object(team.audit, "record_audit") as record:
        resource.return_value.Table.return_value = table
        cognito.admin_list_groups_for_user.return_value = {
            "Groups": [{"GroupName": rbac.TENANT_ENGINEER}]
        }
        yield {"table": table, "cognito": cognito, "audit": record, "resource": resource}


# ---------- listing ----------

def test_the_roster_comes_from_the_tenants_own_partition(harness):
    resp = team.handler(_event(), None)

    assert resp["statusCode"] == 200
    assert harness["resource"].call_args[0][0] == "acme"
    members = json.loads(resp["body"])["members"]
    assert {m["email"] for m in members} == {"ada@acme.com", "bob@acme.com"}


def test_the_roster_does_not_leak_internal_keys(harness):
    """sk and tenant_id are storage details, not part of the API."""
    member = json.loads(team.handler(_event(), None)["body"])["members"][0]
    assert "sk" not in member and "tenant_id" not in member


def test_leadership_may_read_the_roster(harness):
    assert team.handler(_event(group=rbac.TENANT_LEADERSHIP), None)["statusCode"] == 200


def test_an_engineer_may_not_read_the_roster(harness):
    resp = team.handler(_event(group=rbac.TENANT_ENGINEER), None)
    assert resp["statusCode"] == 403


def test_a_token_with_no_tenant_reads_nothing(harness):
    assert team.handler(_event(tenant_id=None), None)["statusCode"] == 403


# ---------- role changes ----------

def _promote(role="TenantLeadership", sub="s-eng", **kw):
    return _event(
        resource="/v1/team/{userSub}/role",
        method="POST",
        path_params={"userSub": sub},
        body={"role": role},
        **kw,
    )


def test_an_admin_can_promote_a_teammate(harness):
    resp = team.handler(_promote(), None)

    assert resp["statusCode"] == 200
    harness["cognito"].admin_add_user_to_group.assert_called_once()
    kwargs = harness["cognito"].admin_add_user_to_group.call_args.kwargs
    assert kwargs["GroupName"] == rbac.TENANT_LEADERSHIP
    assert kwargs["Username"] == "bob@acme.com", "must use the stored username, not the URL id"


def test_the_old_group_is_removed_so_a_user_holds_exactly_one_role(harness):
    """highest_priority_group picks the most privileged of several, so leaving the
    old membership in place would silently keep the old permissions."""
    team.handler(_promote(), None)
    kwargs = harness["cognito"].admin_remove_user_from_group.call_args.kwargs
    assert kwargs["GroupName"] == rbac.TENANT_ENGINEER


def test_the_stored_role_is_updated_too(harness):
    """The roster is read from DynamoDB, so a Cognito-only change would show the
    old role on screen until the user signed in again."""
    team.handler(_promote(), None)
    harness["table"].update_item.assert_called_once()


def test_an_engineer_cannot_change_roles(harness):
    resp = team.handler(_promote(group=rbac.TENANT_ENGINEER), None)
    assert resp["statusCode"] == 403
    harness["cognito"].admin_add_user_to_group.assert_not_called()


def test_leadership_cannot_change_roles(harness):
    """Leadership observes; it does not administer."""
    resp = team.handler(_promote(group=rbac.TENANT_LEADERSHIP), None)
    assert resp["statusCode"] == 403
    harness["cognito"].admin_add_user_to_group.assert_not_called()


def test_an_unknown_role_is_refused(harness):
    resp = team.handler(_promote(role="Superuser"), None)
    assert resp["statusCode"] == 400
    harness["cognito"].admin_add_user_to_group.assert_not_called()


def test_a_user_from_another_tenant_cannot_be_touched(harness):
    """The guard that stops Cognito's account-wide admin APIs crossing tenants."""
    harness["table"].get_item.return_value = {}

    resp = team.handler(_promote(sub="s-someone-elses"), None)

    assert resp["statusCode"] == 404
    harness["cognito"].admin_add_user_to_group.assert_not_called()
    harness["cognito"].admin_remove_user_from_group.assert_not_called()


def test_the_last_admin_cannot_be_demoted(harness):
    """Otherwise the tenant can never approve remediation again."""
    harness["table"].get_item.return_value = {"Item": ADMIN}
    harness["table"].query.return_value = {"Items": [ADMIN, ENGINEER]}
    harness["cognito"].admin_list_groups_for_user.return_value = {
        "Groups": [{"GroupName": rbac.TENANT_ADMIN}]
    }

    resp = team.handler(
        _promote(role=rbac.TENANT_ENGINEER, sub="s-admin"), None
    )

    assert resp["statusCode"] == 409
    assert "admin" in json.loads(resp["body"])["message"].lower()
    harness["cognito"].admin_remove_user_from_group.assert_not_called()


def test_an_admin_can_be_demoted_when_another_admin_remains(harness):
    second_admin = _member("s-admin2", "dan@acme.com", rbac.TENANT_ADMIN)
    harness["table"].get_item.return_value = {"Item": ADMIN}
    harness["table"].query.return_value = {"Items": [ADMIN, second_admin, ENGINEER]}
    harness["cognito"].admin_list_groups_for_user.return_value = {
        "Groups": [{"GroupName": rbac.TENANT_ADMIN}]
    }

    resp = team.handler(_promote(role=rbac.TENANT_ENGINEER, sub="s-admin"), None)

    assert resp["statusCode"] == 200


def test_promoting_to_the_role_a_user_already_holds_is_a_no_op_success(harness):
    harness["table"].get_item.return_value = {"Item": ENGINEER}
    harness["cognito"].admin_list_groups_for_user.return_value = {
        "Groups": [{"GroupName": rbac.TENANT_ENGINEER}]
    }

    resp = team.handler(_promote(role=rbac.TENANT_ENGINEER), None)

    assert resp["statusCode"] == 200
    harness["cognito"].admin_remove_user_from_group.assert_not_called()


def test_a_role_change_is_audited(harness):
    team.handler(_promote(), None)
    kwargs = harness["audit"].call_args.kwargs
    assert kwargs["action"] == "team.role_change"
    assert kwargs["actor"] == "admin-1"
    assert kwargs["tenant_id"] == "acme"


def test_a_refused_role_change_is_audited(harness):
    team.handler(_promote(group=rbac.TENANT_ENGINEER), None)
    assert harness["audit"].call_args.kwargs["result"] == "refused_not_permitted"


def test_a_missing_body_is_refused(harness):
    resp = team.handler(
        _event(
            resource="/v1/team/{userSub}/role",
            method="POST",
            path_params={"userSub": "s-eng"},
        ),
        None,
    )
    assert resp["statusCode"] == 400


def test_an_unknown_route_is_a_404(harness):
    assert team.handler(_event(resource="/v1/nope"), None)["statusCode"] == 404
