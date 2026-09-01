"""Custom::S3VectorsSetup

CloudFormation custom-resource Lambda. S3 Vectors has no native CloudFormation
resource type, so the vector bucket and index are created here idempotently via
the s3vectors API.

Everything below is shaped by one rule: **this function must always answer
CloudFormation.** A custom resource that raises before it can respond does not
fail the stack quickly — CloudFormation simply waits for a reply that never
comes, for an hour by default, and reports a timeout that names no cause.

That is what used to happen. The boto3 s3vectors client was constructed at
module scope, so on any environment whose botocore lacked the s3vectors service
model it raised UnknownServiceError at import: the handler never ran, no
try/except could catch it, and the deploy stalled. So:

* module scope does nothing that can fail — standard library and config only;
* the client is built inside the handler, where a failure is catchable;
* the handler catches BaseException and reports FAILED with the reason;
* a Delete always reports SUCCESS, because a failed Delete response wedges the
  stack in DELETE_FAILED, and destroy.sh checks for and removes a surviving
  vector bucket anyway.
"""
import logging

import cfnresponse

from common import config

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DIMENSION = 1024  # cohere.embed-multilingual-v3 output dimension
DISTANCE_METRIC = "cosine"

# Error codes meaning "it isn't there", which is the normal case on a first
# deploy and not a failure.
_NOT_FOUND = ("NotFoundException", "ResourceNotFoundException")


def _client():
    """Build the s3vectors client here, not at import.

    boto3 is imported inside the function too: if the layer resolved a botocore
    without the s3vectors service model, both the import and the client
    construction have to fail somewhere the handler can catch them.
    """
    import boto3

    return boto3.client("s3vectors", region_name=config.REGION)


def _not_found(exc):
    return exc.response["Error"]["Code"] in _NOT_FOUND


def _ensure_bucket(s3vectors):
    from botocore.exceptions import ClientError

    try:
        s3vectors.get_vector_bucket(vectorBucketName=config.VECTOR_BUCKET)
        logger.info("vector bucket %s already exists", config.VECTOR_BUCKET)
    except ClientError as exc:
        if not _not_found(exc):
            raise
        s3vectors.create_vector_bucket(vectorBucketName=config.VECTOR_BUCKET)
        logger.info("created vector bucket %s", config.VECTOR_BUCKET)


def _ensure_index(s3vectors):
    from botocore.exceptions import ClientError

    try:
        s3vectors.get_index(
            vectorBucketName=config.VECTOR_BUCKET, indexName=config.VECTOR_INDEX
        )
        logger.info("vector index %s already exists", config.VECTOR_INDEX)
    except ClientError as exc:
        if not _not_found(exc):
            raise
        s3vectors.create_index(
            vectorBucketName=config.VECTOR_BUCKET,
            indexName=config.VECTOR_INDEX,
            dimension=DIMENSION,
            distanceMetric=DISTANCE_METRIC,
            dataType="float32",
        )
        logger.info(
            "created vector index %s (dim=%s, metric=%s)",
            config.VECTOR_INDEX,
            DIMENSION,
            DISTANCE_METRIC,
        )


def _delete(s3vectors):
    """Tear the index and bucket down, loudly enough to notice a leftover."""
    from botocore.exceptions import ClientError

    try:
        s3vectors.delete_index(
            vectorBucketName=config.VECTOR_BUCKET, indexName=config.VECTOR_INDEX
        )
    except ClientError as exc:
        logger.warning("could not delete vector index: %s", exc)
    try:
        s3vectors.delete_vector_bucket(vectorBucketName=config.VECTOR_BUCKET)
    except ClientError as exc:
        logger.error(
            "vector bucket %s was not deleted and will be left behind: %s",
            config.VECTOR_BUCKET,
            exc,
        )


def handler(event, context):
    request_type = (event or {}).get("RequestType")
    # Stable across Create and Update, so an Update does not make CloudFormation
    # issue a Delete for a replaced physical id.
    physical_id = f"{config.VECTOR_BUCKET}/{config.VECTOR_INDEX}"

    try:
        s3vectors = _client()
        if request_type in ("Create", "Update"):
            _ensure_bucket(s3vectors)
            _ensure_index(s3vectors)
        elif request_type == "Delete":
            _delete(s3vectors)
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {}, physical_id)
    except BaseException as exc:  # noqa: BLE001 - must always signal CloudFormation
        logger.exception("%s failed", request_type)
        if request_type == "Delete":
            # Reporting FAILED on a Delete leaves the stack in DELETE_FAILED and
            # needs manual intervention. destroy.sh verifies the vector bucket is
            # gone and removes it directly, so SUCCESS here is both safe and the
            # only outcome that lets a teardown finish.
            cfnresponse.send(
                event, context, cfnresponse.SUCCESS, {}, physical_id,
                reason=f"delete reported success despite {type(exc).__name__}; see logs",
            )
        else:
            cfnresponse.send(
                event, context, cfnresponse.FAILED, {}, physical_id,
                reason=f"{type(exc).__name__}: {exc}"[:1000],
            )
