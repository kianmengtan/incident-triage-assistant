"""Deriving a tenant from an email address.

Shared by the two Cognito triggers, which have to agree exactly.
``fn-pre-signup`` refuses an address this module cannot derive a tenant from, and
``fn-tenant-provision`` derives the tenant for the addresses that got through. If
the two used separate copies of the domain list, a domain one allowed and the
other rejected would produce a confirmed account with no tenant -- able to sign
in, and then denied by the authorizer on every request with no explanation.

``frontend/lib/triage.mjs`` mirrors this list so the signup form can refuse a
consumer address before submitting it, and ``tests/test_rbac_parity.py`` fails if
the two drift.
"""
import re

# A shared consumer-mail domain is not an organisation: deriving a tenant from it
# would put every gmail.com signup into one tenant, sharing each other's
# incidents.
PUBLIC_EMAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "yahoo.com",
        "icloud.com",
        "me.com",
        "aol.com",
        "proton.me",
        "protonmail.com",
        "gmx.com",
        "mail.com",
        "yandex.com",
        "zoho.com",
        "qq.com",
    }
)


def email_domain(email):
    """The lowercased domain of ``email``, or None if it has no usable one."""
    if not email or "@" not in email:
        return None
    local, _, domain = email.rpartition("@")
    # Both halves must be present: "@example.com" is not an address to derive a
    # tenant from.
    if not local.strip():
        return None
    domain = domain.strip().lower()
    return domain or None


def is_public_domain(email):
    domain = email_domain(email)
    return domain is not None and domain in PUBLIC_EMAIL_DOMAINS


def tenant_id_for_email(email):
    """Derive a stable tenant id from an email address, or None.

    The domain is slugified so the result is safe as a DynamoDB partition key, a
    Secrets Manager name component and an IAM session tag value.
    """
    domain = email_domain(email)
    if domain is None or domain in PUBLIC_EMAIL_DOMAINS:
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", domain).strip("-")
    return slug or None
