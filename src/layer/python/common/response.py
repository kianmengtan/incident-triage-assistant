import json

# The console is served from CloudFront and this API answers on a different
# origin, so every response needs CORS headers of its own.
#
# `AdminApi` in template.yaml sets SAM's `Cors` property, but that only builds
# the OPTIONS preflight method. Under Lambda proxy integration API Gateway
# returns exactly what the function returns, so without the header here the
# preflight succeeds and the browser then refuses to let the page read the real
# response. The resulting error names CORS, which sends you to the API Gateway
# configuration -- where everything is correct.
#
# `*` is deliberate: the CloudFront domain is generated at deploy time and is not
# known when this layer is built, and the API is authorised by a bearer token
# rather than by cookies, so no credentialed cross-origin request is being
# permitted here.
_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    # The response depends on the request's origin, so a shared cache in front of
    # the API must not serve one origin's response to another.
    "Vary": "Origin",
}


def api_response(status_code, body, headers=None):
    resp_headers = {"Content-Type": "application/json"}
    resp_headers.update(_CORS_HEADERS)
    if headers:
        # Caller-supplied headers win, so a handler can still override the origin
        # deliberately rather than being silently overruled here.
        resp_headers.update(headers)
    return {
        "statusCode": status_code,
        "headers": resp_headers,
        "body": json.dumps(body, default=str),
    }
