"""Every API response has to carry CORS headers of its own.

`AdminApi` sets SAM's `Cors` property, which builds the OPTIONS preflight method
and nothing else. Under Lambda proxy integration API Gateway returns whatever the
function returns, verbatim -- so a preflight can succeed while the real response
is still blocked by the browser for lacking `Access-Control-Allow-Origin`.

This was latent for the whole life of the project because the console ran on a
hardcoded mock array and never called the API. The failure it produces is
particularly misleading: the browser reports a CORS error, so the obvious place
to look is API Gateway's CORS configuration, which is correct.
"""
import json

from common.response import api_response


def test_every_response_allows_the_browser_to_read_it():
    resp = api_response(200, {"ok": True})
    assert resp["headers"]["Access-Control-Allow-Origin"] == "*"


def test_error_responses_carry_it_too():
    """A 403 the browser cannot read surfaces as a network error, not a denial.

    The UI needs to tell a non-admin *why* an action failed, which means reading
    the body of the 403 -- impossible without the header on the error response.
    """
    for status in (400, 401, 403, 404, 409, 500):
        resp = api_response(status, {"message": "nope"})
        assert resp["headers"]["Access-Control-Allow-Origin"] == "*", status


def test_it_varies_on_origin():
    """Signals that the response is origin-dependent, so a shared cache in front
    of the API cannot serve one origin's response to another."""
    assert api_response(200, {})["headers"]["Vary"] == "Origin"


def test_content_type_is_still_json():
    assert api_response(200, {})["headers"]["Content-Type"] == "application/json"


def test_caller_supplied_headers_still_win():
    """Handlers pass their own headers (e.g. Location); that must keep working."""
    resp = api_response(200, {}, {"X-Custom": "1"})
    assert resp["headers"]["X-Custom"] == "1"
    assert resp["headers"]["Access-Control-Allow-Origin"] == "*"


def test_a_handler_may_override_the_origin_deliberately():
    """Nothing does today, but an explicit override must not be silently ignored."""
    resp = api_response(200, {}, {"Access-Control-Allow-Origin": "https://example.test"})
    assert resp["headers"]["Access-Control-Allow-Origin"] == "https://example.test"


def test_body_is_json_encoded():
    assert json.loads(api_response(200, {"a": 1})["body"]) == {"a": 1}
