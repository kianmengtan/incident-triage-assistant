"""fn-rag-context

Step Functions task. Embeds the correlated alert/log/config context with
cohere.embed-multilingual-v3 and queries the tenant's namespace in the
shared S3 Vectors index for similar past incidents.
"""
import json

import boto3

from common import bedrock, config, tenant_scope

_s3vectors = boto3.client("s3vectors", region_name=config.REGION)


def _build_context_text(event):
    alert = event["alert"]
    parts = [
        f"service={alert.get('service')}",
        f"severity={alert.get('severity')}",
        f"description={alert.get('description')}",
    ]
    logs = event.get("logs_context", {})
    if logs.get("entry_count"):
        parts.append(f"log_entries={logs['entry_count']}")
    changes = event.get("config_context", {})
    if changes.get("change_count"):
        parts.append(f"config_changes={changes['change_count']}")
    return " | ".join(parts)[: config.MAX_EMBED_INPUT_CHARS]


def handler(event, context):
    tenant_id = event["tenant_id"]
    context_text = _build_context_text(event)

    embeddings = bedrock.embed_texts([context_text], input_type="search_query")
    query_vector = embeddings[0]

    namespace_filter = {"tenant_id": tenant_id}
    try:
        matches = _s3vectors.query_vectors(
            vectorBucketName=config.VECTOR_BUCKET,
            indexName=config.VECTOR_INDEX,
            queryVector={"float32": query_vector},
            topK=5,
            filter=namespace_filter,
            returnMetadata=True,
        ).get("vectors", [])
    except Exception:  # noqa: BLE001 - index may be empty on first-ever incident
        matches = []

    similar_incidents = [
        {
            "key": m.get("key"),
            "score": m.get("distance"),
            "metadata": m.get("metadata", {}),
        }
        for m in matches
    ]

    # Store this incident's own embedding for future RAG lookups.
    try:
        _s3vectors.put_vectors(
            vectorBucketName=config.VECTOR_BUCKET,
            indexName=config.VECTOR_INDEX,
            vectors=[
                {
                    "key": f"{tenant_id}#{event['alert_id']}",
                    "data": {"float32": query_vector},
                    "metadata": {
                        "tenant_id": tenant_id,
                        "alert_id": event["alert_id"],
                        "summary": context_text,
                    },
                }
            ],
        )
    except Exception:  # noqa: BLE001 - RAG write is best-effort, never blocks the pipeline
        pass

    return {"context_text": context_text, "similar_incidents": similar_incidents}
