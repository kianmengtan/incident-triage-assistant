"""RAG retrieval. The failure mode this guards against is silence: swallowing
every exception made a broken index indistinguishable from an empty one."""
import json
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

import rag_context
from common import bedrock, config

EVENT = {
    "tenant_id": "acme",
    "alert_id": "alert-1",
    "alert": {"service": "checkout", "severity": "high", "description": "5xx spike"},
    "logs_context": {"entry_count": 3, "sample": [{"message": "TimeoutError: pool exhausted"}]},
    "config_context": {"change_count": 1, "sample": [{"message": "lower DB_POOL_MAX to 8"}]},
}


def _embedding(dim=1024):
    return {"body": MagicMock(read=lambda: json.dumps({"embeddings": [[0.1] * dim]}).encode())}


def _client_error(code):
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "QueryVectors")


@pytest.fixture
def harness():
    with patch.object(bedrock._bedrock, "invoke_model", return_value=_embedding()), \
         patch.object(rag_context.progress, "mark_stage"), \
         patch.object(rag_context, "_s3vectors") as vectors:
        vectors.query_vectors.return_value = {"vectors": []}
        yield {"vectors": vectors}


def test_the_approved_embedding_model_is_used_with_its_bare_id(harness):
    with patch.object(bedrock._bedrock, "invoke_model", return_value=_embedding()) as invoke:
        rag_context.handler(EVENT, None)

    assert invoke.call_args.kwargs["modelId"] == config.EMBED_MODEL_ID == "cohere.embed-multilingual-v3"
    assert not config.EMBED_MODEL_ID.startswith("global."), "Cohere Embed is the local model"


def test_the_embedded_summary_includes_log_and_config_text(harness):
    """It used to embed only counts ("log_entries=3"), so retrieval matched on
    service and severity alone and the retrieval step contributed nothing."""
    result = rag_context.handler(EVENT, None)

    assert "TimeoutError: pool exhausted" in result["context_text"]
    assert "lower DB_POOL_MAX to 8" in result["context_text"]


def test_the_embedded_text_is_capped(harness):
    event = dict(EVENT, alert=dict(EVENT["alert"], description="x" * 5000))
    result = rag_context.handler(event, None)
    assert len(result["context_text"]) <= config.MAX_EMBED_INPUT_CHARS


def test_the_query_is_filtered_to_the_calling_tenant(harness):
    rag_context.handler(EVENT, None)
    assert harness["vectors"].query_vectors.call_args.kwargs["filter"] == {"tenant_id": "acme"}


def test_a_match_from_another_tenant_is_dropped_even_if_the_filter_let_it_through(harness):
    """Defence in depth: the metadata filter was the only thing separating
    tenants, so a filter accepted but not applied would feed one tenant's
    incidents into another tenant's prompt."""
    harness["vectors"].query_vectors.return_value = {
        "vectors": [
            {"key": "acme#a", "distance": 0.1, "metadata": {"tenant_id": "acme"}},
            {"key": "globex#b", "distance": 0.05, "metadata": {"tenant_id": "globex"}},
            {"key": "orphan", "distance": 0.2, "metadata": {}},
        ]
    }
    result = rag_context.handler(EVENT, None)

    assert [m["key"] for m in result["similar_incidents"]] == ["acme#a"]


def test_matches_report_distance_not_score(harness):
    """Cosine distance: smaller is closer. Calling it "score" invited the UI to
    render it as a similarity percentage, which inverts the meaning."""
    harness["vectors"].query_vectors.return_value = {
        "vectors": [{"key": "acme#a", "distance": 0.12, "metadata": {"tenant_id": "acme"}}]
    }
    match = rag_context.handler(EVENT, None)["similar_incidents"][0]
    assert match["distance"] == 0.12
    assert "score" not in match


def test_an_empty_index_is_not_an_error(harness):
    harness["vectors"].query_vectors.side_effect = _client_error("NotFoundException")
    assert rag_context.handler(EVENT, None)["similar_incidents"] == []


def test_a_permissions_failure_is_logged_as_an_error(harness, caplog):
    """The regression that mattered: AccessDenied looked exactly like "no similar
    incidents yet", so RAG could be dead for the life of a deployment with
    nothing in any log."""
    harness["vectors"].query_vectors.side_effect = _client_error("AccessDeniedException")

    with caplog.at_level("ERROR"):
        result = rag_context.handler(EVENT, None)

    assert result["similar_incidents"] == []
    assert any("vector query failed" in r.message for r in caplog.records)
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_a_validation_failure_is_also_surfaced(harness, caplog):
    harness["vectors"].query_vectors.side_effect = _client_error("ValidationException")
    with caplog.at_level("ERROR"):
        rag_context.handler(EVENT, None)
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_this_incidents_vector_is_stored_for_future_retrieval(harness):
    rag_context.handler(EVENT, None)

    kwargs = harness["vectors"].put_vectors.call_args.kwargs
    vector = kwargs["vectors"][0]
    assert vector["key"] == "acme#alert-1"
    assert vector["metadata"]["tenant_id"] == "acme"


def test_a_failed_vector_write_does_not_fail_the_diagnosis_but_is_logged(harness, caplog):
    harness["vectors"].put_vectors.side_effect = _client_error("AccessDeniedException")

    with caplog.at_level("ERROR"):
        result = rag_context.handler(EVENT, None)

    assert "context_text" in result
    assert any("could not store vector" in r.message for r in caplog.records)
