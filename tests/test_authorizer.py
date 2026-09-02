"""Token verification. The previous implementation called cognito-idp:GetUser
and trusted any token GetUser accepted — which is any token from any Cognito
user pool, along with that pool's custom:tenant_id."""
import base64
import json
import time
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

import authorizer
from common import config, jwt

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KID = "test-kid"


def _b64(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _jwks(key=_KEY, kid=KID):
    numbers = key.public_key().public_numbers()
    to_bytes = lambda n: n.to_bytes((n.bit_length() + 7) // 8, "big")
    return {
        "keys": [
            {
                "kty": "RSA",
                "kid": kid,
                "n": _b64(to_bytes(numbers.n)),
                "e": _b64(to_bytes(numbers.e)),
            }
        ]
    }


def _token(claims=None, key=_KEY, kid=KID, alg="RS256"):
    header = {"alg": alg, "kid": kid}
    payload = {
        "iss": jwt.issuer(),
        "aud": config.USER_POOL_CLIENT_ID,
        "token_use": "id",
        "exp": int(time.time()) + 3600,
        "cognito:username": "ada",
        "custom:tenant_id": "acme",
        "cognito:groups": ["TenantAdmin"],
    }
    if claims:
        payload.update(claims)
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(payload).encode())}"
    signature = key.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_b64(signature)}"


@pytest.fixture(autouse=True)
def jwks():
    jwt._jwks_cache.update({"fetched_at": 0.0, "keys": {}})
    with patch.object(jwt, "_fetch_jwks", return_value=_jwks()) as m:
        yield m
    jwt._jwks_cache.update({"fetched_at": 0.0, "keys": {}})


def _event(token):
    return {
        "methodArn": "arn:aws:execute-api:ap-southeast-1:1:api/prod/GET/v1/runbooks",
        "headers": {"Authorization": f"Bearer {token}"},
    }


def _effect(resp):
    return resp["policyDocument"]["Statement"][0]["Effect"]


def _resource(resp):
    return resp["policyDocument"]["Statement"][0]["Resource"]


# --------------------------------------------------------------------- allowed


def test_a_valid_token_is_allowed_with_tenant_and_group_in_context():
    resp = authorizer.handler(_event(_token()), None)

    assert _effect(resp) == "Allow"
    assert resp["context"] == {"tenant_id": "acme", "group": "TenantAdmin"}
    assert resp["principalId"] == "ada"


def test_a_cached_allow_policy_covers_every_route_in_the_api_stage():
    """The token is the authorizer cache key, so an exact-method policy would
    make whichever route was requested first the only usable route for five
    minutes."""
    resp = authorizer.handler(_event(_token()), None)
    assert _resource(resp) == "arn:aws:execute-api:ap-southeast-1:1:api/prod/*/*"


def test_the_highest_priority_group_wins():
    token = _token({"cognito:groups": ["TenantLeadership", "TenantEngineer", "TenantAdmin"]})
    assert authorizer.handler(_event(token), None)["context"]["group"] == "TenantAdmin"


def test_a_user_in_no_group_is_allowed_but_carries_no_group():
    token = _token({"cognito:groups": []})
    resp = authorizer.handler(_event(token), None)
    assert _effect(resp) == "Allow"
    assert resp["context"]["group"] == ""


def test_a_bare_token_without_the_bearer_prefix_is_accepted():
    event = _event(_token())
    event["headers"]["Authorization"] = event["headers"]["Authorization"][7:]
    assert _effect(authorizer.handler(event, None)) == "Allow"


# ---------------------------------------------------------------------- denied


def test_a_token_from_another_user_pool_is_denied():
    """The bypass the old GetUser check allowed: a valid token, from a pool we
    have nothing to do with, carrying its own custom:tenant_id."""
    token = _token({"iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_someoneelse"})
    assert _effect(authorizer.handler(_event(token), None)) == "Deny"


def test_a_token_for_another_app_client_is_denied():
    assert _effect(authorizer.handler(_event(_token({"aud": "someone-elses-client"})), None)) == "Deny"


def test_a_token_signed_by_the_wrong_key_is_denied():
    assert _effect(authorizer.handler(_event(_token(key=_OTHER_KEY)), None)) == "Deny"


def test_an_access_token_is_denied_because_it_carries_no_tenant():
    assert _effect(authorizer.handler(_event(_token({"token_use": "access"})), None)) == "Deny"


def test_an_expired_token_is_denied():
    assert _effect(authorizer.handler(_event(_token({"exp": int(time.time()) - 3600})), None)) == "Deny"


def test_a_token_with_no_tenant_id_is_denied():
    """A signup whose tenant could not be derived lands here, and must not be
    admitted to anything."""
    assert _effect(authorizer.handler(_event(_token({"custom:tenant_id": ""})), None)) == "Deny"


def test_an_unsigned_alg_none_token_is_denied():
    header = _b64(json.dumps({"alg": "none", "kid": KID}).encode())
    payload = _b64(json.dumps({"iss": jwt.issuer(), "custom:tenant_id": "acme"}).encode())
    assert _effect(authorizer.handler(_event(f"{header}.{payload}."), None)) == "Deny"


def test_a_missing_token_is_denied():
    assert _effect(authorizer.handler({"methodArn": "arn:x", "headers": {}}, None)) == "Deny"


def test_a_malformed_token_is_denied():
    for bad in ["not-a-jwt", "a.b", "a.b.c.d", "...", "Bearer"]:
        assert _effect(authorizer.handler(_event(bad), None)) == "Deny"


def test_a_tampered_payload_is_denied():
    """Escalating your own group by editing the claims must fail the signature."""
    token = _token()
    header, payload, signature = token.split(".")
    forged = json.loads(base64.urlsafe_b64decode(payload + "=="))
    forged["cognito:groups"] = ["TenantAdmin"]
    forged["custom:tenant_id"] = "globex"
    tampered = f"{header}.{_b64(json.dumps(forged).encode())}.{signature}"
    assert _effect(authorizer.handler(_event(tampered), None)) == "Deny"


# ------------------------------------------------------------------- behaviour


def test_no_aws_api_calls_are_made_on_the_request_path():
    """Two Cognito calls per request, with authorizer caching disabled, was a
    throttling limit on the whole admin API."""
    with patch.object(jwt, "_fetch_jwks", return_value=_jwks()) as fetch:
        jwt._jwks_cache.update({"fetched_at": 0.0, "keys": {}})
        for _ in range(5):
            authorizer.handler(_event(_token()), None)
    assert fetch.call_count == 1, "the JWKS should be fetched once and cached"


def test_an_unknown_kid_triggers_exactly_one_refresh():
    """A rotated signing key is the legitimate case; it must not refetch forever."""
    jwt._jwks_cache.update({"fetched_at": time.time(), "keys": {}})
    with patch.object(jwt, "_fetch_jwks", return_value=_jwks(kid="rotated")) as fetch:
        assert _effect(authorizer.handler(_event(_token(kid="unknown")), None)) == "Deny"
    assert fetch.call_count == 1
