"""Log and config correlation, now one implementation behind two handlers."""
import json
from unittest.mock import MagicMock, patch

import pytest

import correlate

EVENT = {
    "tenant_id": "acme",
    "alert_id": "alert-1",
    "alert": {"service": "checkout", "severity": "high", "received_at": 1_700_000_000},
}


@pytest.fixture
def s3():
    return MagicMock()


@pytest.fixture
def harness(s3):
    with patch.object(correlate.tenant_scope, "tenant_s3_client", return_value=s3), \
         patch.object(correlate.integrations, "creds", return_value={}) as creds, \
         patch.object(correlate.progress, "mark_stage") as mark_stage:
        yield {"s3": s3, "creds": creds, "mark_stage": mark_stage}


def test_logs_are_cached_under_the_tenants_prefix(harness):
    result = correlate.logs_handler(EVENT, None)

    key = harness["s3"].put_object.call_args.kwargs["Key"]
    assert key == "tenant/acme/alert/alert-1/logs.json"
    assert result["s3_key"] == key
    assert result["entry_count"] == 0


def test_config_changes_are_cached_separately(harness):
    result = correlate.config_handler(EVENT, None)

    assert harness["s3"].put_object.call_args.kwargs["Key"] == "tenant/acme/alert/alert-1/config.json"
    assert result["change_count"] == 0


def test_each_handler_reads_its_own_integration(harness):
    correlate.logs_handler(EVENT, None)
    assert harness["creds"].call_args.args[1] == correlate.integrations.LOG_PLATFORM
    correlate.config_handler(EVENT, None)
    assert harness["creds"].call_args.args[1] == correlate.integrations.VCS


def test_the_query_is_scoped_to_the_alerts_service_and_time_window(harness):
    """The alert argument used to be accepted and then ignored: the endpoint was
    fetched bare, so "logs around the alert" was whatever it returned by
    default."""
    with patch.object(correlate.integrations, "creds", return_value={"endpoint": "https://logs.example.com/q", "api_key": "k"}), \
         patch.object(correlate.http, "request_json", return_value={"entries": [1, 2]}) as request:
        correlate.logs_handler(EVENT, None)

    url = request.call_args.args[0]
    assert "service=checkout" in url
    assert f"since={1_700_000_000 - correlate.LOOKBACK_SECONDS}" in url
    assert f"until={1_700_000_000 + correlate.LOOKAHEAD_SECONDS}" in url


def test_an_endpoint_with_an_existing_query_string_is_appended_to(harness):
    with patch.object(correlate.integrations, "creds", return_value={"endpoint": "https://logs.example.com/q?index=app"}), \
         patch.object(correlate.http, "request_json", return_value={"entries": []}) as request:
        correlate.logs_handler(EVENT, None)
    assert "?index=app&service=checkout" in request.call_args.args[0]


def test_no_configured_platform_yields_an_empty_result_with_a_note(harness):
    result = correlate.logs_handler(EVENT, None)
    cached = json.loads(harness["s3"].put_object.call_args.kwargs["Body"])
    assert result["entry_count"] == 0
    assert cached["source"] == "none"


def test_a_refused_endpoint_degrades_instead_of_failing_the_pipeline(harness):
    """C-02: a missing evidence source is a degraded diagnosis, not a failure."""
    with patch.object(correlate.integrations, "creds", return_value={"endpoint": "https://10.0.0.1/q"}), \
         patch.object(correlate.http, "request_json", side_effect=correlate.http.EndpointNotAllowed("private")):
        result = correlate.logs_handler(EVENT, None)

    assert result["entry_count"] == 0
    assert result["source"] == "error"
    assert result["note"] == "endpoint not permitted"


def test_the_failure_note_never_echoes_the_url_or_its_credentials(harness):
    """str(exc) from urllib includes the URL, which can carry an api key in its
    query string — and the note is cached to S3 and fed into the model prompt."""
    secret_url = "https://logs.example.com/q?api_key=SUPERSECRETVALUE"
    with patch.object(correlate.integrations, "creds", return_value={"endpoint": secret_url}), \
         patch.object(correlate.http, "request_json", side_effect=RuntimeError(f"failed fetching {secret_url}")):
        result = correlate.logs_handler(EVENT, None)

    cached = harness["s3"].put_object.call_args.kwargs["Body"].decode("utf-8")
    assert "SUPERSECRETVALUE" not in result["note"]
    assert "SUPERSECRETVALUE" not in cached
    assert result["note"] == "query failed (RuntimeError)"


def test_an_unexpected_response_shape_is_handled(harness):
    with patch.object(correlate.integrations, "creds", return_value={"endpoint": "https://logs.example.com/q"}), \
         patch.object(correlate.http, "request_json", return_value=["not", "a", "dict"]):
        result = correlate.logs_handler(EVENT, None)
    assert result["source"] == "error"


def test_the_sla_flag_reflects_real_elapsed_time(harness):
    """It compared a wall-clock delta against 60s while the HTTP timeout was 10s,
    so it could never be true and NFR-05 had no signal at all."""
    with patch.object(correlate.time, "monotonic", side_effect=[100.0, 100.0 + 61]):
        result = correlate.logs_handler(EVENT, None)
    assert result["sla_flagged"] is True
    assert result["elapsed_seconds"] == 61.0


def test_a_fast_correlation_is_not_flagged(harness):
    with patch.object(correlate.time, "monotonic", side_effect=[100.0, 100.5]):
        result = correlate.logs_handler(EVENT, None)
    assert result["sla_flagged"] is False


def test_an_alert_without_a_timestamp_still_produces_a_window(harness):
    event = {"tenant_id": "acme", "alert_id": "a", "alert": {"service": "checkout"}}
    with patch.object(correlate.integrations, "creds", return_value={"endpoint": "https://logs.example.com/q"}), \
         patch.object(correlate.http, "request_json", return_value={"entries": []}) as request:
        correlate.logs_handler(event, None)
    assert "since=" in request.call_args.args[0]


def test_query_parameters_are_percent_encoded():
    """The query string used to be built by f-string concatenation, so a service
    name with a space produced a malformed URL and one containing & or = injected
    extra parameters into the tenant's own log-platform request."""
    event = dict(EVENT, alert={"service": "checkout api", "severity": "sev1", "received_at": 1_700_000_000})
    with patch.object(correlate.tenant_scope, "tenant_s3_client", return_value=MagicMock()), \
         patch.object(correlate.progress, "mark_stage"), \
         patch.object(correlate.integrations, "creds", return_value={"endpoint": "https://logs.example.com/q"}), \
         patch.object(correlate.http, "request_json", return_value={"entries": []}) as request:
        correlate.logs_handler(event, None)

    url = request.call_args.args[0]
    assert " " not in url
    assert "service=checkout+api" in url or "service=checkout%20api" in url


def test_an_injected_service_name_cannot_add_query_parameters():
    event = dict(
        EVENT,
        alert={"service": "x&admin=true", "severity": "sev1", "received_at": 1_700_000_000},
    )
    with patch.object(correlate.tenant_scope, "tenant_s3_client", return_value=MagicMock()), \
         patch.object(correlate.progress, "mark_stage"), \
         patch.object(correlate.integrations, "creds", return_value={"endpoint": "https://logs.example.com/q"}), \
         patch.object(correlate.http, "request_json", return_value={"entries": []}) as request:
        correlate.logs_handler(event, None)

    url = request.call_args.args[0]
    assert "admin=true" not in url
    assert "%26admin%3Dtrue" in url


def test_an_endpoint_that_already_has_a_query_string_keeps_it():
    with patch.object(correlate.tenant_scope, "tenant_s3_client", return_value=MagicMock()), \
         patch.object(correlate.progress, "mark_stage"), \
         patch.object(correlate.integrations, "creds", return_value={"endpoint": "https://logs.example.com/q?team=sre"}), \
         patch.object(correlate.http, "request_json", return_value={"entries": []}) as request:
        correlate.logs_handler(EVENT, None)

    url = request.call_args.args[0]
    assert "team=sre" in url and "&service=checkout" in url
