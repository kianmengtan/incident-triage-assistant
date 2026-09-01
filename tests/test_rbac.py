"""The capability matrix is the one place a role's permissions are defined.

Three handlers each carried their own group check -- ``APPROVER_GROUP`` in
trigger_remediation, ``AUDIT_READERS`` in query_diagnostics, ``ADMIN_GROUP`` in
tenant_settings. Three copies of the same idea drift, and the drift is invisible:
a role silently gains or loses a permission in one handler while the UI, which
disables controls from its own copy of the rules, keeps saying otherwise.

These tests pin the matrix itself, then pin that each handler still enforces the
same answer through it.
"""
import pytest

from common import rbac


def test_every_capability_lists_at_least_one_role():
    """A capability nobody holds is dead code that reads as a working feature."""
    empty = [name for name, roles in rbac.CAPABILITIES.items() if not roles]
    assert not empty, f"these capabilities are granted to nobody: {empty}"


def test_every_capability_only_names_known_roles():
    """A typo'd role name grants nothing, and `can` would just return False."""
    for name, roles in rbac.CAPABILITIES.items():
        unknown = [r for r in roles if r not in rbac.ROLES]
        assert not unknown, f"{name} names roles that do not exist: {unknown}"


def test_admin_holds_every_capability():
    """TenantAdmin is the tenant's owner; a capability it lacks needs a reason."""
    missing = [name for name in rbac.CAPABILITIES if not rbac.can(rbac.TENANT_ADMIN, name)]
    assert not missing, f"TenantAdmin unexpectedly cannot: {missing}"


@pytest.mark.parametrize(
    "role,capability,allowed",
    [
        # Preserves what the handlers enforced before the matrix existed.
        (rbac.TENANT_ADMIN, "approve_remediation", True),
        (rbac.TENANT_ENGINEER, "approve_remediation", False),
        (rbac.TENANT_LEADERSHIP, "approve_remediation", False),
        (rbac.TENANT_ADMIN, "view_audit", True),
        (rbac.TENANT_LEADERSHIP, "view_audit", True),
        (rbac.TENANT_ENGINEER, "view_audit", False),
        (rbac.TENANT_ADMIN, "manage_integrations", True),
        (rbac.TENANT_ENGINEER, "manage_integrations", False),
        (rbac.TENANT_LEADERSHIP, "manage_integrations", False),
        # New in this change.
        (rbac.TENANT_ADMIN, "create_incident", True),
        (rbac.TENANT_ENGINEER, "create_incident", True),
        (rbac.TENANT_LEADERSHIP, "create_incident", False),
        (rbac.TENANT_LEADERSHIP, "view_overview", True),
        (rbac.TENANT_ENGINEER, "view_overview", False),
        (rbac.TENANT_ADMIN, "manage_roles", True),
        (rbac.TENANT_LEADERSHIP, "manage_roles", False),
        # Reading incidents is what every member of a tenant is for.
        (rbac.TENANT_ENGINEER, "view_incidents", True),
        (rbac.TENANT_LEADERSHIP, "view_incidents", True),
    ],
)
def test_matrix(role, capability, allowed):
    assert rbac.can(role, capability) is allowed


def test_an_unrecognised_role_can_do_nothing():
    """A token with no group, or a group we retired, must fail closed."""
    for capability in rbac.CAPABILITIES:
        assert rbac.can("", capability) is False
        assert rbac.can(None, capability) is False
        assert rbac.can("Administrators", capability) is False


def test_an_unknown_capability_raises_rather_than_denying():
    """A typo must not read as 'denied'.

    `can(group, "aprove_remediation")` returning False would disable the button
    for everyone including admins, and look like a permissions bug in the data
    rather than a misspelling in the code.
    """
    with pytest.raises(rbac.UnknownCapability):
        rbac.can(rbac.TENANT_ADMIN, "aprove_remediation")


def test_denial_message_names_who_can_instead_of_just_refusing():
    """Design section 5: a denied user should see why, not a bare 403.

    The message names the role in the form a person reads ("Tenant Admins"),
    not the raw Cognito group name, since it is rendered straight into the UI.
    """
    message = rbac.denial_message(rbac.TENANT_ENGINEER, "approve_remediation")
    assert "Tenant Admins" in message
    assert "approve" in message.lower()


def test_denial_message_for_a_roleless_session_says_to_sign_in_again():
    message = rbac.denial_message("", "view_audit")
    assert "sign in" in message.lower()


def test_roles_match_the_authorizer_group_priority():
    """`highest_priority_group` resolves a token's group from config.GROUP_PRIORITY.

    A role in the matrix that the authorizer can never emit is unreachable; a
    group the authorizer emits that the matrix does not know is denied
    everything. Either way the two lists have to hold the same names.
    """
    from common import config

    assert set(rbac.ROLES) == set(config.GROUP_PRIORITY)
