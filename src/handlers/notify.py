"""fn-notify

Step Functions task. Publishes a runbook-ready notification to SNS once the
runbook has been generated and stored.
"""
import json

import boto3

from common import config

_sns = boto3.client("sns", region_name=config.REGION)


def handler(event, context):
    message = {
        "tenant_id": event["tenant_id"],
        "alert_id": event["alert_id"],
        "runbook_id": event["runbook"]["runbook_id"],
    }
    _sns.publish(
        TopicArn=config.RUNBOOK_READY_TOPIC_ARN,
        Subject="Runbook ready",
        Message=json.dumps(message),
        MessageAttributes={
            "tenant_id": {"DataType": "String", "StringValue": event["tenant_id"]}
        },
    )
    return {"notified": True}
