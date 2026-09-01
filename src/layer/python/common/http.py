"""Outbound HTTP to tenant-configured endpoints.

Every endpoint this application calls out to (log platform, VCS, remediation
platform, IMS) is a URL a tenant put in their own integration-credentials
secret. That makes each one an SSRF sink: a tenant who sets their endpoint to
``file:///proc/self/environ`` or to the instance metadata service can have this
Lambda fetch it and — in the correlate handlers — cache the result where they
can read it back through the diagnostics API.

So requests go through :func:`request_json`, which enforces https, resolves the
host and refuses anything that lands on a private, loopback or link-local
address, declines redirects, and caps how much of the response it will read.

The checks are split in two, because they have different callers.
:func:`assert_target_shape` judges what can be judged from the URL alone -- the
scheme, and a literal IP address -- and needs no network. :func:`_assert_allowed`
adds the resolution check and runs on every outbound call. Storing an endpoint
uses the first: a configuration write should not fail because DNS was briefly
unavailable, and resolution cannot be cached anyway, since where a name points is
exactly what can change between the write and the call.
"""
import ipaddress
import json
import socket
import urllib.parse
import urllib.request

ALLOWED_SCHEMES = ("https",)
MAX_RESPONSE_BYTES = 1 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 10


class EndpointNotAllowed(ValueError):
    """The configured endpoint is not a destination we are willing to call."""


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect is a second destination that never passed _assert_allowed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise EndpointNotAllowed(f"endpoint redirected to {newurl}")


_opener = urllib.request.build_opener(_NoRedirects)


def _resolved_addresses(host):
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise EndpointNotAllowed(f"cannot resolve {host}: {exc}") from exc
    return {ipaddress.ip_address(info[4][0]) for info in infos}


def _is_internal(address):
    """Anything not routable on the public internet.

    ip_address normalises IPv4-mapped IPv6 forms, so ::ffff:169.254.169.254 is
    recognised as link-local here rather than slipping through as a v6 address.
    """
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def assert_target_shape(url):
    """The checks that need no DNS, for validating an endpoint before storing it.

    Deliberately does NOT resolve: a stored endpoint must not be rejected because
    resolution happened to fail, and a name that resolves publicly today can point
    somewhere internal tomorrow. Resolution is therefore checked on every call, in
    _assert_allowed, not once at write time.
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise EndpointNotAllowed(f"scheme {parsed.scheme!r} is not allowed; use https")
    if not parsed.hostname:
        raise EndpointNotAllowed("endpoint has no host")
    try:
        address = ipaddress.ip_address(parsed.hostname.strip("[]"))
    except ValueError:
        # A name. Only the resolution check can say where it points.
        return
    if _is_internal(address):
        raise EndpointNotAllowed(f"endpoint is a non-public address {address}")


def _assert_allowed(url):
    assert_target_shape(url)
    for address in _resolved_addresses(urllib.parse.urlsplit(url).hostname):
        if _is_internal(address):
            raise EndpointNotAllowed(
                f"endpoint resolves to non-public address {address}"
            )


def request_json(url, api_key=None, payload=None, method=None, timeout=DEFAULT_TIMEOUT_SECONDS):
    """Call ``url`` and parse the response as JSON.

    Raises EndpointNotAllowed for a destination we refuse to contact, and lets
    urllib/JSON errors propagate so callers can decide how to degrade.
    """
    _assert_allowed(url)

    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    with _opener.open(request, timeout=timeout) as resp:
        # read one byte past the cap so an oversized body is an error, not a
        # silent truncation into the JSON parser.
        raw = resp.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise EndpointNotAllowed(
            f"response exceeded {MAX_RESPONSE_BYTES} bytes"
        )
    if not raw:
        return {}
    return json.loads(raw)
