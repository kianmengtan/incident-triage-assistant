#!/usr/bin/env python3
"""Repair users who were confirmed while tenant provisioning was broken.

Background. ``fn-tenant-provision`` is a Cognito PostConfirmation trigger, and
Cognito runs it exactly once per user. While it was asking for
``secretsmanager:CreateSecret`` -- an action the deploy permissions boundary does
not grant -- it threw on every signup, and because it deliberately never
re-raises, each user ended up CONFIRMED with no ``custom:tenant_id``. Those users
can sign in and then go nowhere: the console shows them the "no tenant" screen,
and the API authorizer denies every request a token with no tenant makes.

Fixing the trigger does not help them, because it will never run for them again.
This script does what their PostConfirmation run should have done.

It deliberately calls ``tenant_provision.handler`` with a synthesised
PostConfirmation event rather than reimplementing provisioning: a second copy of
"derive the tenant, create the key material, claim the tenant, set the attribute,
assign the group, write the roster row" would drift from the trigger, and the two
disagreeing about who is a TenantAdmin is exactly the kind of bug this codebase
already paid for once.

Dry run by default. Nothing is written unless you pass ``--apply``.

    # see what would change
    python3 scripts/backfill_tenants.py --user-pool-id ap-southeast-1_XXXX

    # do it
    python3 scripts/backfill_tenants.py --user-pool-id ap-southeast-1_XXXX --apply

Needs credentials that may call cognito-idp Admin*, ssm:PutParameter under
/<prefix>/tenant/*, and dynamodb:PutItem on the tenants table. Exits non-zero if
any user it tried to repair is still unscoped afterwards.
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src", "handlers"))
sys.path.insert(0, os.path.join(HERE, "..", "src", "layer", "python"))

TENANT_ATTRIBUTE = "custom:tenant_id"


def attributes(user):
    """A user's attributes as a dict, from either list_users or admin_get_user."""
    pairs = user.get("Attributes") or user.get("UserAttributes") or []
    return {a["Name"]: a.get("Value", "") for a in pairs}


def needs_backfill(user):
    """True if this user is confirmed, enabled and has no tenant."""
    if user.get("UserStatus") != "CONFIRMED" or not user.get("Enabled", True):
        return False
    return not attributes(user).get(TENANT_ATTRIBUTE)


def post_confirmation_event(user, user_pool_id):
    """The event Cognito would have handed the trigger for this user."""
    attrs = attributes(user)
    return {
        "userPoolId": user_pool_id,
        "userName": user["Username"],
        "request": {
            "userAttributes": {
                "email": attrs.get("email", ""),
                "sub": attrs.get("sub", ""),
            }
        },
    }


def list_all_users(cognito, user_pool_id):
    """Every user in the pool, following pagination."""
    users = []
    token = None
    while True:
        kwargs = {"UserPoolId": user_pool_id, "Limit": 60}
        if token:
            kwargs["PaginationToken"] = token
        page = cognito.list_users(**kwargs)
        users.extend(page.get("Users", []))
        token = page.get("PaginationToken")
        if not token:
            break
    return users


def backfill(cognito, user_pool_id, provision, apply=False, out=print):
    """Repair every unscoped user. Returns (repaired, skipped, failed) usernames.

    ``provision`` is called with the synthesised event; it is
    ``tenant_provision.handler`` in production and a stub in the tests.
    """
    from common import tenancy

    repaired, skipped, failed = [], [], []
    for user in list_all_users(cognito, user_pool_id):
        username = user["Username"]
        attrs = attributes(user)
        if not needs_backfill(user):
            if attrs.get(TENANT_ATTRIBUTE):
                out(f"  ok       {username} -> {attrs[TENANT_ATTRIBUTE]}")
            else:
                out(f"  skip     {username} (status {user.get('UserStatus')})")
            skipped.append(username)
            continue

        email = attrs.get("email", "")
        tenant_id = tenancy.tenant_id_for_email(email)
        if not tenant_id:
            # A consumer address or a malformed one. The trigger refuses to guess
            # a tenant for these and so does this; nothing can be done without
            # deciding which organisation they belong to.
            out(f"  UNSCOPED {username} (no tenant derivable from its address)")
            failed.append(username)
            continue

        if not apply:
            out(f"  would    {username} -> {tenant_id}")
            repaired.append(username)
            continue

        provision(post_confirmation_event(user, user_pool_id), None)

        # The trigger swallows ClientError by design, so success is confirmed by
        # re-reading the user rather than by the call not raising.
        after = cognito.admin_get_user(UserPoolId=user_pool_id, Username=username)
        if attributes(after).get(TENANT_ATTRIBUTE) == tenant_id:
            out(f"  fixed    {username} -> {tenant_id}")
            repaired.append(username)
        else:
            out(f"  FAILED   {username} (still unscoped; check the function log)")
            failed.append(username)

    return repaired, skipped, failed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--user-pool-id",
        default=os.environ.get("USER_POOL_ID", ""),
        help="defaults to $USER_POOL_ID",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the changes; without it nothing is modified",
    )
    args = parser.parse_args(argv)

    if not args.user_pool_id:
        parser.error("--user-pool-id is required (or set USER_POOL_ID)")

    # Imported here so --help works without credentials or the layer's deps.
    import boto3

    import tenant_provision
    from common import config

    os.environ.setdefault("USER_POOL_ID", args.user_pool_id)
    cognito = boto3.client("cognito-idp", region_name=config.REGION)

    print(f"pool {args.user_pool_id} in {config.REGION}")
    print("DRY RUN — pass --apply to write" if not args.apply else "APPLYING")
    repaired, skipped, failed = backfill(
        cognito, args.user_pool_id, tenant_provision.handler, apply=args.apply
    )
    verb = "repaired" if args.apply else "would repair"
    print(f"\n{verb} {len(repaired)}, already fine {len(skipped)}, needs attention {len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
