"""fn-rag-context

Step Functions task. Embeds the correlated alert/log/config context with
cohere.embed-multilingual-v3 and queries the tenant's slice of the shared
S3 Vectors index for similar past incidents.

Two things this used to get wrong. It swallowed every exception from both the
query and the write, so an IAM or filter problem looked exactly like "no similar
incidents yet" — RAG could be dead for the life of the deployment with nothing
in any log to say so. And it trusted the metadata filter as the only thing
keeping tenants apart; matches are now re-checked in code, because a filter that
is accepted but not applied would quietly feed one tenant's incidents into
another tenant's prompt.
"""
import logging

import boto3
import botocore.exceptions
from botocore.exceptions import ClientError

from common import bedrock, config, progress

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Built on first use, not at import. Constructing an s3vectors client at module
# scope means a botocore without that service model takes the whole function down
# before any handler code runs — which in a custom resource stalls a deploy with
# no callback, and here would fail every diagnosis instead of degrading RAG.
_s3vectors = None


def _client():
    global _s3vectors
    if _s3vectors is None:
        _s3vectors = boto3.client("s3vectors", region_name=config.REGION)
    return _s3vectors

TOP_K = 5
# Cap on how much log/config text is folded into the embedded summary. The
# previous version embedded only counts ("log_entries=42"), so retrieval was
# matching on service and severity alone and the R in RAG did nothing.
MAX_EXCERPT_CHARS = 400


def _excerpt(values, field):
    out = []
    for value in values or []:
        if isinstance(value, dict):
            text = value.get(field) or value.get("message") or value.get("text")
        else:
            text = value
        if text:
            out.append(str(text))
        if sum(len(t) for t in out) > MAX_EXCERPT_CHARS:
            break
    return " ".join(out)[:MAX_EXCERPT_CHARS]


def _build_context_text(event):
    alert = event["alert"]
    logs = event.get("logs_context") or {}
    changes = event.get("config_context") or {}
    parts = [
        f"service={alert.get('service')}",
        f"severity={alert.get('severity')}",
        f"description={alert.get('description')}",
    ]
    if logs.get("entry_count"):
        parts.append(f"log_entries={logs['entry_count']}")
    if logs.get("sample"):
        parts.append(f"log_excerpt={_excerpt(logs.get('sample'), 'message')}")
    if changes.get("change_count"):
        parts.append(f"config_changes={changes['change_count']}")
    if changes.get("sample"):
        parts.append(f"change_excerpt={_excerpt(changes.get('sample'), 'message')}")
    return " | ".join(parts)[: config.MAX_EMBED_INPUT_CHARS]


def _query_similar(tenant_id, query_vector):
    try:
        response = _client().query_vectors(
            vectorBucketName=config.VECTOR_BUCKET,
            indexName=config.VECTOR_INDEX,
            queryVector={"float32": query_vector},
            topK=TOP_K,
            filter={"tenant_id": tenant_id},
            returnMetadata=True,
        )
    except botocore.exceptions.UnknownServiceError:
        logger.error(
            "botocore in this deployment has no s3vectors service model; "
            "RAG is disabled until the layer's boto3 is updated"
        )
        return []
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code in ("NotFoundException", "ResourceNotFoundException"):
            # Genuinely nothing indexed yet: the first incident for a tenant.
            logger.info("vector index not populated yet, continuing without RAG")
            return []
        # AccessDenied, ValidationException and friends are configuration bugs.
        # They must be visible, not indistinguishable from an empty index.
        logger.error("vector query failed (%s): %s", code, exc)
        return []

    kept = []
    for match in response.get("vectors", []):
        metadata = match.get("metadata") or {}
        if metadata.get("tenant_id") != tenant_id:
            # Defence in depth behind the server-side filter, mirroring the
            # tenant check the UI does on everything it renders.
            logger.error(
                "dropping vector match for tenant %s from a query scoped to %s",
                metadata.get("tenant_id"),
                tenant_id,
            )
            continue
        kept.append(
            {
                "key": match.get("key"),
                # Cosine DISTANCE, so smaller is closer. Named for what it is:
                # calling it "score" invited the UI to render it as similarity.
                "distance": match.get("distance"),
                "metadata": metadata,
            }
        )
    return kept


def _store_own_vector(tenant_id, alert_id, query_vector, context_text):
    try:
        _client().put_vectors(
            vectorBucketName=config.VECTOR_BUCKET,
            indexName=config.VECTOR_INDEX,
            vectors=[
                {
                    "key": f"{tenant_id}#{alert_id}",
                    "data": {"float32": query_vector},
                    "metadata": {
                        "tenant_id": tenant_id,
                        "alert_id": alert_id,
                        "summary": context_text,
                    },
                }
            ],
        )
    except botocore.exceptions.UnknownServiceError:
        logger.error("cannot store vector: no s3vectors service model available")
    except ClientError as exc:
        # Best-effort: this incident not being retrievable later must not fail
        # the diagnosis. But it is logged as an error, because a permanent
        # failure here means RAG never accumulates anything at all.
        logger.error(
            "could not store vector for %s (%s): %s",
            alert_id,
            exc.response["Error"]["Code"],
            exc,
        )


def handler(event, context):
    tenant_id = event["tenant_id"]
    progress.mark_stage(tenant_id, event["alert_id"], progress.RAG)
    context_text = _build_context_text(event)

    query_vector = bedrock.embed_texts([context_text], input_type="search_query")[0]

    similar = _query_similar(tenant_id, query_vector)
    _store_own_vector(tenant_id, event["alert_id"], query_vector, context_text)

    return {"context_text": context_text, "similar_incidents": similar}
