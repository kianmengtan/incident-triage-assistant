"""fn-pre-signup

Cognito PreSignUp trigger. Runs before an account is created and does two things.

**Refuses an address no tenant can be derived from.** Tenancy comes from the email
domain (see ``common.tenancy``), so a consumer address cannot belong to an
organisation. Without this check such a signup succeeded and produced an account
that ``fn-tenant-provision`` left unscoped: it could sign in, and then every API
call it made was denied by the authorizer with nothing on screen explaining why.
Raising here instead surfaces the reason in the signup form, because Cognito
returns this exception's message to the client.

**Auto-confirms the account**, which is what makes signup a single step with no
emailed code. This is a deliberate trade-off and it weakens one thing: the address
is never proven, and because the tenant is derived from its domain, anyone can
sign up as ``someone@bigcorp.com`` and land inside BigCorp's tenant with access to
its incidents. That was accepted knowingly for this prototype. Deleting this
trigger from the template restores the verification step, and nothing else has to
change -- ``fn-tenant-provision`` runs on confirmation either way.
"""
import logging

from common import tenancy

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class SignUpRefused(Exception):
    """Message reaches the browser: Cognito returns it to the caller verbatim."""


def handler(event, context):
    attrs = (event.get("request") or {}).get("userAttributes") or {}
    email = attrs.get("email")

    domain = tenancy.email_domain(email)
    if domain is None:
        raise SignUpRefused("Enter a valid work email address to create an account.")

    if tenancy.is_public_domain(email):
        # Named explicitly so the form can say what was wrong rather than
        # offering a generic failure.
        raise SignUpRefused(
            f"{domain} is a personal email provider. Sign up with your work email "
            "address so your account joins your organisation."
        )

    event.setdefault("response", {})
    event["response"]["autoConfirmUser"] = True
    # Confirmed but unverified leaves the account in a half state that Cognito
    # treats differently on later flows such as password reset.
    event["response"]["autoVerifyEmail"] = True

    logger.info("auto-confirming signup for domain %s", domain)
    return event
