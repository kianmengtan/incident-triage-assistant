"""Role capabilities -- the single source of truth for who may do what.

Every group check in this application resolves through the matrix below. Three
handlers previously each carried their own: ``APPROVER_GROUP`` in
trigger_remediation, ``AUDIT_READERS`` in query_diagnostics and ``ADMIN_GROUP``
in tenant_settings. Three copies of one idea drift apart quietly -- a role gains
a permission in one handler and not another, and the front end, which disables
controls from its own copy of the rules, then disagrees with the server about
what is allowed. The user discovers the disagreement as a 403 on a button that
looked enabled.

The front end mirrors this matrix in ``frontend/lib/triage.mjs``, and
``tests/test_rbac_parity.py`` fails if the two ever disagree. That mirror is for
UX only: it decides which controls render disabled and with what explanation.
Authorisation itself is always this module, called inside the handler, because
anything the browser enforces the browser can also skip.

The group names come from the Cognito groups in ``template.yaml`` and reach a
handler through the authorizer's request context, having been read out of a
cryptographically verified ID token (see ``common.jwt``).
"""

TENANT_ADMIN = "TenantAdmin"
TENANT_ENGINEER = "TenantEngineer"
TENANT_LEADERSHIP = "TenantLeadership"

#: Must hold the same names as ``config.GROUP_PRIORITY`` -- a role the authorizer
#: cannot emit is unreachable, and a group it emits that is missing here is
#: denied everything. Pinned by a test.
ROLES = (TENANT_ADMIN, TENANT_ENGINEER, TENANT_LEADERSHIP)

#: capability -> the roles that hold it.
CAPABILITIES = {
    # Reading the tenant's incidents is what membership of a tenant means.
    "view_incidents": (TENANT_ADMIN, TENANT_ENGINEER, TENANT_LEADERSHIP),
    "view_diagnosis": (TENANT_ADMIN, TENANT_ENGINEER, TENANT_LEADERSHIP),
    "view_runbooks": (TENANT_ADMIN, TENANT_ENGINEER, TENANT_LEADERSHIP),
    # Raising an incident is an operational act; leadership observes rather than
    # operates, so it is deliberately absent here.
    "create_incident": (TENANT_ADMIN, TENANT_ENGINEER),
    # C-03 and Requirement 7: remediation runs real changes on live
    # infrastructure, so exactly one role may authorise it.
    "approve_remediation": (TENANT_ADMIN,),
    # Reading the audit trail is a review activity, so leadership gets it too.
    "view_audit": (TENANT_ADMIN, TENANT_LEADERSHIP),
    # The cross-incident overview exists for the people accountable for the
    # tenant, not for the engineer working a single incident.
    "view_overview": (TENANT_ADMIN, TENANT_LEADERSHIP),
    "view_team": (TENANT_ADMIN, TENANT_LEADERSHIP),
    # Changing integration credentials or someone's role is tenant
    # administration.
    "manage_integrations": (TENANT_ADMIN,),
    "manage_roles": (TENANT_ADMIN,),
}

#: Reads naturally in a denial message ("only Tenant Admins may ...").
_HUMAN = {
    TENANT_ADMIN: "Tenant Admins",
    TENANT_ENGINEER: "Tenant Engineers",
    TENANT_LEADERSHIP: "Tenant Leadership",
}

_VERB = {
    "view_incidents": "view incidents",
    "view_diagnosis": "view a diagnosis",
    "view_runbooks": "view runbooks",
    "create_incident": "raise an incident",
    "approve_remediation": "approve remediation",
    "view_audit": "read the audit trail",
    "view_overview": "see the tenant overview",
    "view_team": "see the team",
    "manage_integrations": "view or change integrations",
    "manage_roles": "change a teammate's role",
}


class UnknownCapability(KeyError):
    """Raised for a capability name the matrix does not define.

    Deliberately an exception rather than a False return. A misspelled
    capability that merely denied would disable the control for everybody,
    including admins, and present as a permissions bug in the data rather than a
    typo in the code -- so it would be looked for in the wrong place.
    """


def _roles_for(capability):
    try:
        return CAPABILITIES[capability]
    except KeyError:
        raise UnknownCapability(capability) from None


def can(group, capability):
    """True if ``group`` holds ``capability``.

    Fails closed for an empty, absent or unrecognised group: a token carrying no
    group, or one from a role that has since been retired, gets nothing.
    """
    return group in _roles_for(capability)


def denial_message(group, capability):
    """Why this caller cannot do this, phrased for a person to read.

    Names the roles that *can*, so the reader knows who to ask rather than only
    that they were refused (design/frontend-design.md section 5).
    """
    allowed = _roles_for(capability)
    verb = _VERB.get(capability, capability.replace("_", " "))

    if group not in ROLES:
        return (
            f"Your session does not carry a recognised role, so you cannot {verb}. "
            "Sign in again."
        )

    holders = " or ".join(_HUMAN.get(role, role) for role in allowed)
    return f"Only {holders} may {verb}."


def capabilities_for(group):
    """Every capability ``group`` holds, sorted. Handy for a whoami response."""
    return tuple(sorted(name for name in CAPABILITIES if can(group, name)))
