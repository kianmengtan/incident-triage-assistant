"""The SSRF guard. Four handlers call URLs that a tenant put in their own
integration secret, so the destination is attacker-controlled input."""
from unittest.mock import MagicMock, patch

import pytest

from common import http


def test_only_https_is_allowed():
    for url in [
        "http://example.com/logs",
        "file:///proc/self/environ",
        "ftp://example.com/x",
        "gopher://example.com/",
    ]:
        with pytest.raises(http.EndpointNotAllowed, match="scheme"):
            http.request_json(url)


def test_a_url_with_no_host_is_refused():
    with pytest.raises(http.EndpointNotAllowed):
        http.request_json("https:///nohost")


@pytest.mark.parametrize(
    "address,label",
    [
        ("169.254.169.254", "instance metadata"),
        ("127.0.0.1", "loopback"),
        ("10.0.0.5", "private"),
        ("192.168.1.1", "private"),
        ("172.16.0.1", "private"),
        ("0.0.0.0", "unspecified"),
        ("224.0.0.1", "multicast"),
    ],
)
def test_internal_destinations_are_refused(address, label):
    """Resolution is checked, not just the literal, so a hostname that resolves
    to a private address is refused too."""
    with patch.object(http, "_resolved_addresses", return_value={__import__("ipaddress").ip_address(address)}):
        with pytest.raises(http.EndpointNotAllowed, match="non-public"):
            http.request_json("https://logs.tenant.example/query")


def test_a_hostname_resolving_internally_is_refused_even_though_it_looks_public():
    import ipaddress

    with patch.object(
        http, "_resolved_addresses", return_value={ipaddress.ip_address("10.1.2.3")}
    ):
        with pytest.raises(http.EndpointNotAllowed):
            http.request_json("https://totally-legit-logs.example.com/api")


def test_an_unresolvable_host_is_refused_rather_than_attempted():
    import socket

    with patch.object(http.socket, "getaddrinfo", side_effect=socket.gaierror("nope")):
        with pytest.raises(http.EndpointNotAllowed, match="cannot resolve"):
            http.request_json("https://does-not-exist.invalid/x")


def _public_response(body=b'{"entries": []}'):
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda s: resp
    resp.__exit__ = lambda *a: False
    return resp


def _allow_public():
    import ipaddress

    return patch.object(http, "_resolved_addresses", return_value={ipaddress.ip_address("93.184.216.34")})


def test_a_public_https_endpoint_is_called_and_parsed():
    with _allow_public(), patch.object(http._opener, "open", return_value=_public_response()) as open_:
        assert http.request_json("https://logs.example.com/q", api_key="k") == {"entries": []}

    request = open_.call_args.args[0]
    assert request.get_header("Authorization") == "Bearer k"


def test_a_payload_is_posted_as_json():
    import json

    with _allow_public(), patch.object(http._opener, "open", return_value=_public_response(b"{}")) as open_:
        http.request_json("https://ims.example.com/i", payload={"a": 1}, method="POST")

    request = open_.call_args.args[0]
    assert request.method == "POST"
    assert json.loads(request.data) == {"a": 1}
    assert request.get_header("Content-type") == "application/json"


def test_an_oversized_response_is_refused_rather_than_truncated():
    """A hostile endpoint returning gigabytes would otherwise OOM the function."""
    oversized = b"x" * (http.MAX_RESPONSE_BYTES + 1)
    with _allow_public(), patch.object(http._opener, "open", return_value=_public_response(oversized)):
        with pytest.raises(http.EndpointNotAllowed, match="exceeded"):
            http.request_json("https://logs.example.com/q")


def test_an_empty_response_body_is_an_empty_object():
    with _allow_public(), patch.object(http._opener, "open", return_value=_public_response(b"")):
        assert http.request_json("https://logs.example.com/q") == {}


def test_redirects_are_refused_because_the_target_was_never_checked():
    """Following a redirect means fetching a second destination that never went
    through _assert_allowed — the classic way round an allowlist."""
    handler = http._NoRedirects()
    with pytest.raises(http.EndpointNotAllowed, match="redirected"):
        handler.redirect_request(
            MagicMock(), MagicMock(), 302, "Found", {}, "https://169.254.169.254/"
        )


# ---------------------------------------------------------------------------
# The write-time gate: shape only, no DNS.
# ---------------------------------------------------------------------------
def test_the_shape_check_needs_no_dns():
    """Storing an endpoint must not depend on resolution being available, and
    resolution cannot be cached anyway: where a name points is exactly what can
    change between the write and the call."""
    with patch.object(http, "_resolved_addresses") as resolve:
        http.assert_target_shape("https://logs.tenant.example/query")
    resolve.assert_not_called()


@pytest.mark.parametrize(
    "url",
    [
        "http://logs.example.com/q",
        "file:///proc/self/environ",
        "https:///nohost",
    ],
)
def test_the_shape_check_refuses_a_bad_scheme_or_missing_host(url):
    with pytest.raises(http.EndpointNotAllowed):
        http.assert_target_shape(url)


@pytest.mark.parametrize(
    "host",
    [
        "169.254.169.254",
        "127.0.0.1",
        "10.0.0.5",
        "[::1]",
        "[::ffff:169.254.169.254]",
    ],
)
def test_the_shape_check_refuses_a_literal_internal_address(host):
    """A literal address can be judged with no lookup, including the IPv4-mapped
    IPv6 spelling of one."""
    with pytest.raises(http.EndpointNotAllowed, match="non-public"):
        http.assert_target_shape(f"https://{host}/query")


def test_the_shape_check_accepts_a_public_literal_address():
    http.assert_target_shape("https://93.184.216.34/query")


def test_the_outbound_guard_still_resolves():
    """The shape check is the earlier of two gates, never a replacement: a name
    that resolves somewhere internal must still be refused at call time."""
    import ipaddress

    with patch.object(http, "_resolved_addresses", return_value={ipaddress.ip_address("10.1.2.3")}):
        with pytest.raises(http.EndpointNotAllowed, match="resolves to non-public"):
            http.request_json("https://logs.tenant.example/query")
