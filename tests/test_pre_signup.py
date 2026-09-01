"""fn-pre-signup -- Cognito PreSignUp trigger.

Does two things, and the second one is a deliberate, documented trade-off.

1. **Refuses a consumer email domain, with a reason.** Tenancy is derived from the
   email domain, so a gmail.com signup cannot belong to an organisation. Before
   this trigger existed such a signup succeeded and produced an account the
   PostConfirmation trigger left unscoped -- so it could sign in, and then every
   single API call it made was denied by the authorizer with no explanation.
   Failing at signup, where the form can show why, is the kinder and clearer
   outcome.

2. **Auto-confirms, skipping the emailed verification code.** This is what makes
   signup a single step. It also means the address is never proven, and since the
   tenant is derived from its domain, someone can sign up as
   ``anyone@bigcorp.com`` and land inside BigCorp's tenant. Accepted knowingly
   for this prototype; removing the trigger restores the code step.
"""
import pytest

import pre_signup


def _event(email="ada@acme-retail.com"):
    return {
        "userPoolId": "ap-southeast-1_testpool",
        "userName": "ada",
        "request": {"userAttributes": {"email": email} if email else {}},
        "response": {},
    }


def test_a_work_address_is_auto_confirmed():
    resp = pre_signup.handler(_event(), None)["response"]
    assert resp["autoConfirmUser"] is True


def test_the_email_is_marked_verified_so_the_account_is_usable():
    """Confirmed but unverified would leave the account in a half state that
    Cognito treats differently on later flows like password reset."""
    resp = pre_signup.handler(_event(), None)["response"]
    assert resp["autoVerifyEmail"] is True


@pytest.mark.parametrize(
    "email",
    ["someone@gmail.com", "x@googlemail.com", "y@outlook.com", "z@yahoo.com", "q@proton.me"],
)
def test_consumer_domains_are_refused_at_signup(email):
    """Rather than creating an account that can sign in and then do nothing."""
    with pytest.raises(Exception) as excinfo:
        pre_signup.handler(_event(email), None)
    assert "work" in str(excinfo.value).lower()


def test_the_refusal_names_the_problem_so_the_form_can_show_it():
    with pytest.raises(Exception) as excinfo:
        pre_signup.handler(_event("someone@gmail.com"), None)
    message = str(excinfo.value)
    # Cognito passes the exception message straight back to the client.
    assert "gmail.com" in message


def test_an_address_with_no_domain_is_refused():
    with pytest.raises(Exception):
        pre_signup.handler(_event("not-an-email"), None)


def test_a_missing_email_is_refused():
    with pytest.raises(Exception):
        pre_signup.handler(_event(None), None)


def test_a_subdomain_address_is_accepted():
    """sub.example.co.uk is a real organisation, not a consumer provider."""
    resp = pre_signup.handler(_event("ops@sub.example.co.uk"), None)["response"]
    assert resp["autoConfirmUser"] is True


def test_it_refuses_the_same_domains_the_tenant_derivation_rejects():
    """One list, two consumers.

    If these ever diverged, a domain this trigger allowed but the derivation
    rejected would produce exactly the unscoped zombie account the trigger exists
    to prevent.
    """
    from common import tenancy

    for domain in tenancy.PUBLIC_EMAIL_DOMAINS:
        assert tenancy.tenant_id_for_email(f"user@{domain}") is None
        with pytest.raises(Exception):
            pre_signup.handler(_event(f"user@{domain}"), None)
