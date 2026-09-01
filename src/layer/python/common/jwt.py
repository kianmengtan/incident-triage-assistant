"""Verification of Cognito ID tokens.

The authorizer used to call ``cognito-idp:GetUser`` with the bearer token and
treat a successful response as proof of identity. GetUser validates a token
against whichever pool issued it, so that accepted a token minted by any other
Cognito user pool and then trusted ITS ``custom:tenant_id`` — an authentication
bypass, and the cheapest possible route into another tenant's data.

So verify the token properly instead: RS256 signature against this pool's
published JWKS, plus issuer, token_use, audience and expiry.

The ID token is the one we verify, because ``custom:tenant_id`` is an identity
claim and Cognito puts custom attributes in the ID token only — an access token
carries scopes and groups but no custom attributes, so resolving a tenant from
one would mean a ``GetUser`` call per request. Binding on ``aud`` is what makes
this safe: the token must have been minted by this pool FOR this app client.
Both ``custom:tenant_id`` and ``cognito:groups`` then come out of the verified
token, so the authorizer makes no Cognito API calls at all.

RS256 verification is done with ``cryptography`` (already a layer dependency)
rather than adding a JWT library for one algorithm.
"""
import base64
import json
import logging
import time
import urllib.request

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from . import config

logger = logging.getLogger(__name__)

_JWKS_TTL_SECONDS = 60 * 60
_jwks_cache = {"fetched_at": 0.0, "keys": {}}

# Cognito issues RS256 only; naming the accepted algorithm explicitly stops an
# "alg": "none" or HMAC-with-the-public-key token from being considered.
ALLOWED_ALG = "RS256"
# Tolerance for clock skew between Cognito and this Lambda, in seconds.
LEEWAY_SECONDS = 60


class InvalidToken(ValueError):
    """The token is missing, malformed, unverifiable or not for this app."""


def issuer():
    return f"https://cognito-idp.{config.REGION}.amazonaws.com/{config.USER_POOL_ID}"


def _b64url_decode(segment):
    padding_needed = -len(segment) % 4
    try:
        return base64.urlsafe_b64decode(segment + "=" * padding_needed)
    except (ValueError, TypeError) as exc:
        raise InvalidToken(f"malformed base64url segment: {exc}") from exc


def _fetch_jwks():
    url = f"{issuer()}/.well-known/jwks.json"
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.loads(resp.read())


def _public_keys(force_refresh=False):
    now = time.time()
    fresh = now - _jwks_cache["fetched_at"] < _JWKS_TTL_SECONDS
    if _jwks_cache["keys"] and fresh and not force_refresh:
        return _jwks_cache["keys"]

    keys = {}
    for jwk in _fetch_jwks().get("keys", []):
        if jwk.get("kty") != "RSA":
            continue
        numbers = rsa.RSAPublicNumbers(
            e=int.from_bytes(_b64url_decode(jwk["e"]), "big"),
            n=int.from_bytes(_b64url_decode(jwk["n"]), "big"),
        )
        keys[jwk["kid"]] = numbers.public_key()
    _jwks_cache.update({"fetched_at": now, "keys": keys})
    return keys


def _verified_claims(token):
    parts = token.split(".")
    if len(parts) != 3:
        raise InvalidToken("token is not a three-part JWS")
    header_b64, payload_b64, signature_b64 = parts

    header = json.loads(_b64url_decode(header_b64))
    if header.get("alg") != ALLOWED_ALG:
        raise InvalidToken(f"unexpected alg {header.get('alg')!r}")
    kid = header.get("kid")
    if not kid:
        raise InvalidToken("token header has no kid")

    fetched_at_before = _jwks_cache["fetched_at"]
    keys = _public_keys()
    if kid not in keys and _jwks_cache["fetched_at"] == fetched_at_before:
        # A rotated signing key is the one legitimate reason for an unknown kid,
        # so refresh once — but only if the keys we just used came from the
        # cache. Refreshing after a fetch that only just happened would mean two
        # JWKS requests for every unknown kid on a cold Lambda.
        keys = _public_keys(force_refresh=True)
    key = keys.get(kid)
    if key is None:
        raise InvalidToken(f"no public key for kid {kid!r}")

    try:
        key.verify(
            _b64url_decode(signature_b64),
            f"{header_b64}.{payload_b64}".encode("ascii"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except InvalidSignature as exc:
        raise InvalidToken("signature does not verify") from exc

    return json.loads(_b64url_decode(payload_b64))


def verify_id_token(token):
    """Return the claims of a valid ID token for this pool and app client.

    Raises InvalidToken for anything else, including a token that verifies
    against a different pool or was issued to a different client.
    """
    if not token:
        raise InvalidToken("no token presented")

    claims = _verified_claims(token)

    if claims.get("iss") != issuer():
        raise InvalidToken(f"issuer {claims.get('iss')!r} is not this user pool")
    if claims.get("token_use") != "id":
        raise InvalidToken(f"token_use {claims.get('token_use')!r} is not 'id'")
    if not config.USER_POOL_CLIENT_ID:
        # Without a client id there is nothing to bind the audience to, and an
        # unbound token is exactly the hole this module exists to close.
        raise InvalidToken("USER_POOL_CLIENT_ID is not configured")
    if claims.get("aud") != config.USER_POOL_CLIENT_ID:
        raise InvalidToken("token was issued to a different app client")

    expires_at = claims.get("exp")
    if not isinstance(expires_at, (int, float)):
        raise InvalidToken("token has no usable exp claim")
    if time.time() > expires_at + LEEWAY_SECONDS:
        raise InvalidToken("token has expired")

    return claims


def highest_priority_group(claims):
    """The caller's most privileged group, or '' if they hold none."""
    held = set(claims.get("cognito:groups") or [])
    for candidate in config.GROUP_PRIORITY:
        if candidate in held:
            return candidate
    return ""
