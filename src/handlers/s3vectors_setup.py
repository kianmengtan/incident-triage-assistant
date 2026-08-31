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
import boto3
from botocore.exceptions import ClientError
import cfnresponse  # provided by the Lambda custom-resource runtime helper layer

from common import config

_s3vectors = boto3.client("s3vectors", region_name=config.REGION)

DIMENSION = 1024  # cohere.embed-multilingual-v3 output dimension
DISTANCE_METRIC = "cosine"


def _ensure_bucket():
    try:
        _s3vectors.get_vector_bucket(vectorBucketName=config.VECTOR_BUCKET)
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in ("NotFoundException", "ResourceNotFoundException"):
            raise
        _s3vectors.create_vector_bucket(vectorBucketName=config.VECTOR_BUCKET)


def _ensure_index():
    try:
        _s3vectors.get_index(
            vectorBucketName=config.VECTOR_BUCKET, indexName=config.VECTOR_INDEX
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in ("NotFoundException", "ResourceNotFoundException"):
            raise
        _s3vectors.create_index(
            vectorBucketName=config.VECTOR_BUCKET,
            indexName=config.VECTOR_INDEX,
            dimension=DIMENSION,
            distanceMetric=DISTANCE_METRIC,
            dataType="float32",
        )


def _delete():
    try:
        _s3vectors.delete_index(vectorBucketName=config.VECTOR_BUCKET, indexName=config.VECTOR_INDEX)
    except ClientError:
        pass
    try:
        _s3vectors.delete_vector_bucket(vectorBucketName=config.VECTOR_BUCKET)
    except ClientError:
        pass


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
