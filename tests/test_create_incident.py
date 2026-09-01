"""fn-create-incident -- the authenticated endpoint behind the console's form.

The webhook (fn-ingest-normalize) proves who it is with an API key and a
per-tenant HMAC over the body, and only then trusts the ``tenant_id`` in that
body. This endpoint has no signature: it is a person with a Cognito token, so the
tenant comes from the authorizer context and the body's own ``tenant_id``, if any,
must be ignored. Anything else lets a signed-in user of one tenant write an
incident into another.
"""
import json
from unittest.mock import MagicMock, patch

import pytest

import create_incident
from common import alerts, rbac


def _event(body, tenant_id="acme", group=rbac.TENANT_ENGINEER, principal="user-1"):
    authorizer = {}
    if tenant_id is not None:
        authorizer["tenant_id"] = tenant_id
    if group is not None:
        authorizer["group"] = group
    authorizer["principalId"] = principal
    return {
        "resource": "/v1/alerts",
        "httpMethod": "POST",
        "requestContext": {"authorizer": authorizer},
        "body": body if isinstance(body, str) else json.dumps(body),
    }


def _payload(**overrides):
    base = {"severity": "sev2", "service": "checkout-api", "description": "5xx spike"}
    base.update(overrides)
    return base


@pytest.fixture
def table():
    t = MagicMock()
    t.get_item.return_value = {}
    return t


@pytest.fixture
def harness(table):
    with patch.object(create_incident.tenant_scope, "tenant_dynamodb_resource") as resource, \
         patch.object(alerts, "publish_received", return_value=True) as publish, \
         patch.object(create_incident.audit, "record_audit") as record:
        resource.return_value.Table.return_value = table
        yield {"table": table, "publish": publish, "audit": record, "resource": resource}


# ---------- the tenant boundary ----------

def test_the_tenant_comes_from_the_token_not_the_body(harness):
    """The whole reason this handler exists separately from the webhook."""
    resp = create_incident.handler(
        _event(_payload(tenant_id="victim-co"), tenant_id="acme"), None
    )

    assert resp["statusCode"] == 202
    item = harness["table"].put_item.call_args.kwargs["Item"]
    assert item["tenant_id"] == "acme"
    assert harness["resource"].call_args[0][0] == "acme", "must scope DynamoDB to the token's tenant"


def test_a_token_with_no_tenant_is_refused(harness):
    """An unscoped account -- a public-domain signup -- can create nothing."""
    resp = create_incident.handler(_event(_payload(), tenant_id=None), None)
    assert resp["statusCode"] == 403
    harness["table"].put_item.assert_not_called()


# ---------- RBAC ----------

def test_leadership_cannot_raise_an_incident(harness):
    resp = create_incident.handler(_event(_payload(), group=rbac.TENANT_LEADERSHIP), None)

    assert resp["statusCode"] == 403
    harness["table"].put_item.assert_not_called()
    harness["publish"].assert_not_called()


def test_the_denial_explains_who_can_instead_of_just_refusing(harness):
    resp = create_incident.handler(_event(_payload(), group=rbac.TENANT_LEADERSHIP), None)
    message = json.loads(resp["body"])["message"]
    assert "Tenant Admins" in message and "Tenant Engineers" in message


@pytest.mark.parametrize("group", [rbac.TENANT_ADMIN, rbac.TENANT_ENGINEER])
def test_admins_and_engineers_can_raise_one(harness, group):
    resp = create_incident.handler(_event(_payload(), group=group), None)
    assert resp["statusCode"] == 202


def test_an_unrecognised_group_is_refused(harness):
    resp = create_incident.handler(_event(_payload(), group="Everyone"), None)
    assert resp["statusCode"] == 403


# ---------- validation ----------

def test_missing_fields_are_named(harness):
    resp = create_incident.handler(_event({"severity": "sev1"}), None)
    assert resp["statusCode"] == 400
    message = json.loads(resp["body"])["message"]
    assert "service" in message and "description" in message


def test_tenant_id_is_not_a_required_field(harness):
    """It comes from the token, so requiring it in the body would be nonsense."""
    resp = create_incident.handler(_event(_payload()), None)
    assert resp["statusCode"] == 202


def test_an_unrecognised_severity_is_refused(harness):
    resp = create_incident.handler(_event(_payload(severity="catastrophic")), None)
    assert resp["statusCode"] == 400
    assert "severity" in json.loads(resp["body"])["message"]
    harness["table"].put_item.assert_not_called()


@pytest.mark.parametrize("severity", ["sev1", "SEV4", "critical", "p3", "low"])
def test_the_severities_the_webhook_accepts_are_accepted_here_too(harness, severity):
    """One vocabulary across both entry points, or the same incident validates
    differently depending on how it was raised."""
    assert create_incident.handler(_event(_payload(severity=severity)), None)["statusCode"] == 202


def test_a_malformed_alert_id_is_refused(harness):
    resp = create_incident.handler(_event(_payload(alert_id="has/slash")), None)
    assert resp["statusCode"] == 400
    harness["table"].put_item.assert_not_called()


def test_invalid_json_is_refused(harness):
    resp = create_incident.handler(_event("{not json"), None)
    assert resp["statusCode"] == 400


def test_a_non_object_body_is_refused(harness):
    resp = create_incident.handler(_event("[1,2,3]"), None)
    assert resp["statusCode"] == 400


def test_an_oversized_body_is_refused_before_dynamodb(harness):
    resp = create_incident.handler(
        _event(_payload(description="x" * (alerts.MAX_BODY_BYTES + 10))), None
    )
    assert resp["statusCode"] == 413
    harness["table"].put_item.assert_not_called()


# ---------- behaviour ----------

def test_the_source_records_that_a_person_raised_it(harness):
    """Leadership needs to tell a hand-raised incident from a webhook one."""
    create_incident.handler(_event(_payload()), None)
    assert harness["table"].put_item.call_args.kwargs["Item"]["source"] == "console"


def test_an_explicit_source_is_still_honoured(harness):
    create_incident.handler(_event(_payload(source="grafana")), None)
    assert harness["table"].put_item.call_args.kwargs["Item"]["source"] == "grafana"


def test_creating_an_incident_starts_the_diagnosis_pipeline(harness):
    resp = create_incident.handler(_event(_payload()), None)
    assert resp["statusCode"] == 202
    harness["publish"].assert_called_once()


def test_the_new_alert_id_is_returned_so_the_ui_can_open_it(harness):
    resp = create_incident.handler(_event(_payload()), None)
    assert json.loads(resp["body"])["alert_id"]


def test_a_failed_publish_is_not_reported_as_success(harness):
    """Stored but undispatched means nothing will diagnose it."""
    harness["publish"].return_value = False
    resp = create_incident.handler(_event(_payload()), None)
    assert resp["statusCode"] == 502


def test_resubmitting_the_same_alert_id_is_a_conflict_not_a_silent_success(harness):
    """A webhook retry is normal and answers 202; a person double-submitting a
    form should be told the incident already exists rather than believing they
    raised a second one."""
    from botocore.exceptions import ClientError

    harness["table"].put_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem"
    )
    harness["table"].get_item.return_value = {"Item": {"dispatched_at": 1234}}

    resp = create_incident.handler(_event(_payload(alert_id="ALT-7")), None)
    assert resp["statusCode"] == 409


def test_creation_is_audited(harness):
    create_incident.handler(_event(_payload()), None)
    kwargs = harness["audit"].call_args.kwargs
    assert kwargs["tenant_id"] == "acme"
    assert kwargs["action"] == "incident.create"
    assert kwargs["actor"] == "user-1"


def test_a_refused_creation_is_audited_too(harness):
    """A blocked attempt to raise an incident is worth a record."""
    create_incident.handler(_event(_payload(), group=rbac.TENANT_LEADERSHIP), None)
    assert harness["audit"].call_args.kwargs["result"] == "refused_not_permitted"
