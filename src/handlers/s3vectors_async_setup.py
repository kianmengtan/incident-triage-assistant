"""fn-s3vectors-async-setup

Triggered by the S3VectorsSetupTopic SNS topic, which deploy.sh publishes to
once the CloudFormation stack has finished creating. Invokes
S3VectorsSetupFunction synchronously so the vector bucket/index creation runs
after the stack is live, instead of blocking CloudFormation itself.
Any failure is logged to CloudWatch and re-raised so the invocation shows up
as failed in the function's own metrics/logs.
"""
import json
import logging

import boto3

from common import config

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_lambda = boto3.client("lambda", region_name=config.REGION)


def _run_setup(action):
    response = _lambda.invoke(
        FunctionName=config.S3VECTORS_SETUP_FUNCTION_NAME,
        InvocationType="RequestResponse",
        Payload=json.dumps({"action": action}).encode("utf-8"),
    )
    payload = json.loads(response["Payload"].read() or b"{}")
    if response.get("FunctionError"):
        raise RuntimeError(f"S3 Vectors setup failed: {payload}")
    return payload


def handler(event, context):
    results = []
    for record in event.get("Records", []):
        message = record.get("Sns", {}).get("Message", "{}")
        try:
            body = json.loads(message)
        except ValueError:
            body = {}
        action = body.get("action", "create")
        logger.info("Starting S3 Vectors %s via %s", action, config.S3VECTORS_SETUP_FUNCTION_NAME)
        result = _run_setup(action)
        logger.info("S3 Vectors %s complete: %s", action, result)
        results.append(result)
    return {"results": results}
