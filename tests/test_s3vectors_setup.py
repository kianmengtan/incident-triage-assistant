"""The custom resource that stalled a deploy.

It timed out with no callback: the s3vectors boto3 client was built at module
scope, so on a botocore without that service model the import raised, the handler
never ran, and CloudFormation waited for a reply that could not come. These tests
pin the properties that make that impossible rather than unlikely.
"""
import ast
import pathlib
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError, UnknownServiceError

import s3vectors_setup

HANDLERS_DIR = pathlib.Path(__file__).resolve().parent.parent / "src" / "handlers"


def _event(request_type="Create"):
    return {
        "RequestType": request_type,
        "ResponseURL": "https://cloudformation-custom-resource-response.example/abc",
        "StackId": "arn:aws:cloudformation:ap-southeast-1:1:stack/app/1",
        "RequestId": "req-1",
        "LogicalResourceId": "S3VectorsSetup",
    }


class _Context:
    log_stream_name = "2026/08/31/[$LATEST]abc"


def _client_error(code):
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "GetVectorBucket")


# ------------------------------------------------- nothing risky at import time


def test_no_aws_client_is_constructed_at_import():
    """The regression itself. A module-scope client cannot be caught by any
    try/except in the handler, so its failure is unreportable."""
    tree = ast.parse((HANDLERS_DIR / "s3vectors_setup.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for call in ast.walk(node):
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute):
                    assert call.func.attr != "client", (
                        "s3vectors_setup must not construct a client at module scope"
                    )


def test_no_handler_builds_an_s3vectors_client_at_module_scope():
    """s3vectors is the newest service this app uses and the likeliest to be
    missing from a given botocore, so it is the one that must always be lazy.
    Long-established services (secretsmanager, sns, events) are safe eagerly."""
    for path in HANDLERS_DIR.glob("*.py"):
        for node in ast.parse(path.read_text()).body:
            if not isinstance(node, ast.Assign):
                continue
            for call in ast.walk(node):
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "client"
                    and call.args
                    and isinstance(call.args[0], ast.Constant)
                ):
                    assert call.args[0].value != "s3vectors", (
                        f"{path.name} builds an s3vectors client at import"
                    )


def test_the_response_module_depends_only_on_the_standard_library():
    """Anything cfnresponse imports is a way for the callback never to be sent.
    It used to import urllib3, which the layer never declared."""
    tree = ast.parse((pathlib.Path(s3vectors_setup.cfnresponse.__file__)).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= set(sys.stdlib_module_names), f"non-stdlib imports: {imported}"


# --------------------------------------------------- always answers, never hangs


def test_a_missing_service_model_reports_failed_instead_of_dying_at_import():
    """The exact failure, now reportable: CloudFormation gets FAILED with a
    reason in seconds instead of waiting an hour for nothing."""
    with patch.object(
        s3vectors_setup, "_client",
        side_effect=UnknownServiceError(service_name="s3vectors", known_service_names=[]),
    ), patch.object(s3vectors_setup.cfnresponse, "send") as send:
        s3vectors_setup.handler(_event(), _Context())

    send.assert_called_once()
    assert send.call_args.args[2] == s3vectors_setup.cfnresponse.FAILED
    assert "UnknownServiceError" in send.call_args.kwargs["reason"]


def test_any_unexpected_error_still_produces_a_response():
    for boom in [RuntimeError("nope"), KeyError("k"), MemoryError()]:
        with patch.object(s3vectors_setup, "_client", side_effect=boom), \
             patch.object(s3vectors_setup.cfnresponse, "send") as send:
            s3vectors_setup.handler(_event(), _Context())
        assert send.call_count == 1, f"{type(boom).__name__} produced no response"
        assert send.call_args.args[2] == s3vectors_setup.cfnresponse.FAILED


def test_the_handler_never_raises_out_to_the_runtime():
    """A raise means the runtime reports an error and CloudFormation hears
    nothing at all."""
    with patch.object(s3vectors_setup, "_client", side_effect=RuntimeError("boom")), \
         patch.object(s3vectors_setup.cfnresponse, "send"):
        s3vectors_setup.handler(_event(), _Context())  # must not raise


def test_a_delete_reports_success_even_when_it_fails():
    """FAILED on a Delete leaves the stack in DELETE_FAILED needing manual
    intervention; destroy.sh removes a surviving vector bucket directly."""
    with patch.object(s3vectors_setup, "_client", side_effect=RuntimeError("boom")), \
         patch.object(s3vectors_setup.cfnresponse, "send") as send:
        s3vectors_setup.handler(_event("Delete"), _Context())

    assert send.call_args.args[2] == s3vectors_setup.cfnresponse.SUCCESS


# ------------------------------------------------------------- the work itself


@pytest.fixture
def vectors():
    client = MagicMock()
    with patch.object(s3vectors_setup, "_client", return_value=client), \
         patch.object(s3vectors_setup.cfnresponse, "send") as send:
        yield {"client": client, "send": send}


def test_a_first_deploy_creates_the_bucket_and_index(vectors):
    vectors["client"].get_vector_bucket.side_effect = _client_error("NotFoundException")
    vectors["client"].get_index.side_effect = _client_error("NotFoundException")

    s3vectors_setup.handler(_event(), _Context())

    vectors["client"].create_vector_bucket.assert_called_once()
    index = vectors["client"].create_index.call_args.kwargs
    assert index["dimension"] == 1024, "cohere.embed-multilingual-v3 output width"
    assert index["distanceMetric"] == "cosine"
    assert vectors["send"].call_args.args[2] == s3vectors_setup.cfnresponse.SUCCESS


def test_a_redeploy_creates_nothing(vectors):
    s3vectors_setup.handler(_event("Update"), _Context())

    vectors["client"].create_vector_bucket.assert_not_called()
    vectors["client"].create_index.assert_not_called()
    assert vectors["send"].call_args.args[2] == s3vectors_setup.cfnresponse.SUCCESS


def test_an_unexpected_client_error_is_reported_not_swallowed(vectors):
    vectors["client"].get_vector_bucket.side_effect = _client_error("AccessDeniedException")

    s3vectors_setup.handler(_event(), _Context())

    assert vectors["send"].call_args.args[2] == s3vectors_setup.cfnresponse.FAILED
    vectors["client"].create_vector_bucket.assert_not_called()


def test_the_physical_id_is_stable_across_create_and_update(vectors):
    s3vectors_setup.handler(_event("Create"), _Context())
    created = vectors["send"].call_args.args[4]
    s3vectors_setup.handler(_event("Update"), _Context())
    updated = vectors["send"].call_args.args[4]

    assert created == updated, "a changed physical id makes CloudFormation Delete the old one"


def test_a_delete_removes_the_index_then_the_bucket(vectors):
    s3vectors_setup.handler(_event("Delete"), _Context())

    order = [c[0] for c in vectors["client"].method_calls]
    assert order.index("delete_index") < order.index("delete_vector_bucket")
