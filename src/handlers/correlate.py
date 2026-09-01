"""fn-correlate-logs / fn-correlate-config

One Step Functions task implementation behind two handlers. Both pull external
context for an alert from a tenant-configured endpoint and cache the result
under that tenant's S3 prefix; they used to be two near-identical 66-line files
differing only in which credential key they read and what they named the cached
object.

Both query the endpoint with the alert's service and time window, which the
previous version accepted as a parameter and then ignored — it fetched the
endpoint bare, so "logs around the alert" was whatever the endpoint returned by
default.
"""
import json
import logging
import time
import urllib.parse

from common import config, http, integrations, progress, tenant_scope

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# How far either side of the alert to ask about. Requirement 2 wants the
# changes and log entries immediately preceding an alert, not a whole day of
# them.
LOOKBACK_SECONDS = 30 * 60
LOOKAHEAD_SECONDS = 5 * 60


def _window(alert):
    received_at = int(alert.get("received_at") or time.time())
    return received_at - LOOKBACK_SECONDS, received_at + LOOKAHEAD_SECONDS


def _query(endpoint_creds, alert, empty_key):
    """Call the tenant's platform, degrading to an empty result with a note.

    Anything the endpoint does wrong — unreachable, slow, not JSON, refused as
    a destination — becomes a note on an empty result rather than an exception,
    because a missing evidence source must not fail the whole diagnosis (C-02).
    """
    endpoint = endpoint_creds.get("endpoint")
    if not endpoint:
        return {empty_key: [], "source": "none", "note": "no platform configured"}

    since, until = _window(alert)
    query = {
        "service": alert.get("service"),
        "since": since,
        "until": until,
        "severity": alert.get("severity"),
    }
    # urlencode, not f-string concatenation. The service name comes from the
    # inbound alert, so a space produced a malformed URL and an "&" or "=" in it
    # injected extra parameters into the tenant's own platform request.
    encoded = urllib.parse.urlencode(
        {k: v for k, v in query.items() if v is not None}, quote_via=urllib.parse.quote_plus
    )
    separator = "&" if urllib.parse.urlsplit(endpoint).query else "?"
    url = f"{endpoint}{separator}{encoded}"

    try:
        payload = http.request_json(url, api_key=endpoint_creds.get("api_key"))
    except http.EndpointNotAllowed as exc:
        logger.warning("refusing to call configured endpoint: %s", exc)
        return {empty_key: [], "source": "error", "note": "endpoint not permitted"}
    except Exception as exc:  # noqa: BLE001 - external dependency, degrade gracefully
        logger.warning("endpoint query failed: %s", type(exc).__name__)
        # Deliberately not str(exc): the message can echo the URL, and the URL
        # can carry the tenant's credentials into the S3 cache and from there
        # into the model prompt.
        return {empty_key: [], "source": "error", "note": f"query failed ({type(exc).__name__})"}

    if not isinstance(payload, dict):
        return {empty_key: [], "source": "error", "note": "endpoint returned unexpected shape"}
    payload.setdefault(empty_key, [])
    payload.setdefault("source", "platform")
    return payload


def _correlate(event, integration, empty_key, cache_filename, count_key, stage):
    started = time.monotonic()
    tenant_id = event["tenant_id"]
    alert = event["alert"]
    progress.mark_stage(tenant_id, event["alert_id"], stage)

    result = _query(integrations.creds(tenant_id, integration), alert, empty_key)

    s3 = tenant_scope.tenant_s3_client(tenant_id)
    key = tenant_scope.tenant_object_key(tenant_id, "alert", event["alert_id"], cache_filename)
    s3.put_object(
        Bucket=config.CONTEXT_CACHE_BUCKET,
        Key=key,
        Body=json.dumps(result).encode("utf-8"),
        ContentType="application/json",
    )

    elapsed = time.monotonic() - started
    if elapsed > config.CORRELATION_SLA_SECONDS:
        # NFR-05. Logged as well as returned, because the return value only
        # reaches the execution history while the log line reaches CloudWatch.
        logger.warning(
            "%s correlation took %.1fs, past the %ss target",
            integration,
            elapsed,
            config.CORRELATION_SLA_SECONDS,
        )

    return {
        "s3_key": key,
        count_key: len(result.get(empty_key) or []),
        "source": result.get("source"),
        "note": result.get("note"),
        "elapsed_seconds": round(elapsed, 3),
        "sla_flagged": elapsed > config.CORRELATION_SLA_SECONDS,
    }


def logs_handler(event, context):
    return _correlate(
        event,
        integration=integrations.LOG_PLATFORM,
        empty_key="entries",
        cache_filename="logs.json",
        count_key="entry_count",
        stage=progress.CORRELATE_LOGS,
    )


def config_handler(event, context):
    return _correlate(
        event,
        integration=integrations.VCS,
        empty_key="changes",
        cache_filename="config.json",
        count_key="change_count",
        stage=progress.CORRELATE_CONFIG,
    )
