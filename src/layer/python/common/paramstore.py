"""Per-tenant key material, held in SSM Parameter Store.

Why not Secrets Manager, which the rest of this design originally used: the
permissions boundary every role in this stack must carry
(``brd-architect-deploy-boundary``) grants Secrets Manager **read only** --
``GetSecretValue``, ``DescribeSecret``, ``GetRandomPassword`` on
``secret:app-*``. It grants no ``CreateSecret`` at all, and a role policy cannot
widen a boundary. So ``fn-tenant-provision`` could never create a tenant's DEK:
every signup failed with ``AccessDeniedException``, the trigger's ``except
ClientError`` swallowed it by design, and the user was left confirmed but
without ``custom:tenant_id`` -- which the console renders as a dead end
immediately after a successful sign-in.

The same boundary allows full parameter CRUD on ``parameter/app-*``, so the key
material lives at::

    /{PREFIX}/tenant/{tenant_id}/{kind}

which is inside the ``/app-b9dac5ac-bc8fbf47/`` path the platform contract
reserves for this project.

The parameters are ``String``, not ``SecureString``: the boundary allows no
``kms:*`` whatsoever, and SecureString needs ``kms:Encrypt`` to write and
``kms:Decrypt`` to read. Parameter Store still encrypts at rest under an
AWS-owned key, but anyone holding ``ssm:GetParameter`` on this path reads the
plaintext, where a SecureString would additionally require the key grant. That
is a real reduction in defence depth, accepted here because the alternative is
an application that cannot provision a tenant at all. It is called out in
README.md rather than left for a reader to discover.

Callers keep catching ``botocore.exceptions.ClientError``: a missing parameter
raises ``ParameterNotFound``, which is a ``ClientError``, so the "unknown
tenant looks exactly like an unreadable one" behaviour the ingest path relies
on is unchanged.
"""
import re

import boto3

from . import config

_ssm = boto3.client("ssm", region_name=config.REGION)

# The three kinds of per-tenant material. Values are the last path segment.
DEK = "dek"
INGEST_HMAC = "ingest-hmac"
INTEGRATION_CREDS = "integration-creds"

KINDS = (DEK, INGEST_HMAC, INTEGRATION_CREDS)

# tenancy.tenant_id_for_email slugifies a domain to [a-z0-9-]. Re-checked here
# because this value becomes a path: a tenant id containing "/" or ".." would
# address a parameter outside the tenant's own subtree, and this module is the
# last point that can tell.
_TENANT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,119}$")


def parameter_name(tenant_id, kind):
    """The full parameter name for one tenant's ``kind``.

    Raises ValueError rather than building a name that escapes the tenant's
    subtree or names something other than the three known kinds.
    """
    if not isinstance(tenant_id, str) or not _TENANT_ID.match(tenant_id):
        raise ValueError(f"unusable tenant id for a parameter path: {tenant_id!r}")
    if kind not in KINDS:
        raise ValueError(f"unknown kind of tenant material: {kind!r}")
    return f"/{config.PREFIX}/tenant/{tenant_id}/{kind}"


def read(tenant_id, kind):
    """The stored value. Raises ClientError (ParameterNotFound) if unset."""
    response = _ssm.get_parameter(Name=parameter_name(tenant_id, kind))
    return response["Parameter"]["Value"]


def write(tenant_id, kind, value):
    """Create or replace the value."""
    _ssm.put_parameter(
        Name=parameter_name(tenant_id, kind),
        Value=value,
        Type="String",
        Overwrite=True,
    )


def create_if_missing(tenant_id, kind, value):
    """Create the value only if absent. True if this call created it.

    Overwrite=False so a concurrent second signup for the same tenant cannot
    replace a DEK that already has ciphertext encrypted under it -- that would
    silently strand every field already written for the tenant.
    """
    try:
        _ssm.put_parameter(
            Name=parameter_name(tenant_id, kind),
            Value=value,
            Type="String",
            Overwrite=False,
        )
        return True
    except _ssm.exceptions.ParameterAlreadyExists:
        return False
