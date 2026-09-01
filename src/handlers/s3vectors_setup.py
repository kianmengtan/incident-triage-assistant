"""S3 Vectors bucket/index setup.

S3 Vectors is not a native CloudFormation resource type, and creating the
vector bucket/index can take far longer than CloudFormation's stack-operation
timeout allows. So this Lambda is invoked directly (via S3VectorsAsyncSetupFunction,
triggered by an SNS message published after the stack finishes deploying)
rather than as a CloudFormation custom resource. It still supports the
legacy CFN custom-resource event shape (detected by the presence of
"ResponseURL") for backward compatibility, but that path is no longer wired
up in template.yaml.
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
    if "ResponseURL" in event:
        request_type = event.get("RequestType")
        physical_id = f"{config.VECTOR_BUCKET}/{config.VECTOR_INDEX}"
        try:
            if request_type in ("Create", "Update"):
                _ensure_bucket()
                _ensure_index()
            elif request_type == "Delete":
                _delete()
            cfnresponse.send(event, context, cfnresponse.SUCCESS, {}, physical_id)
        except Exception as exc:  # noqa: BLE001 - must always signal CFN, never hang the stack
            cfnresponse.send(event, context, cfnresponse.FAILED, {"Error": str(exc)}, physical_id)
        return None

    action = event.get("action", "create")
    if action == "delete":
        _delete()
    else:
        _ensure_bucket()
        _ensure_index()
    return {"vectorBucket": config.VECTOR_BUCKET, "vectorIndex": config.VECTOR_INDEX, "action": action}
