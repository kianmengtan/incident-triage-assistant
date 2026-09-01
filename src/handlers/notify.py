"""fn-notify / fn-mark-pipeline-failed

Two Step Functions tasks that both report on the pipeline's outcome.

``handler`` publishes the runbook-ready notification to SNS. A failure there is
logged and reported, not raised: the runbook is already written and approvable at
that point, so failing the execution over an SNS blip would send a complete
diagnosis down the failure path and into the DLQ.

``failure_handler`` marks the alert failed. It exists because nothing used to
write ``progress.FAILED``: the state machine sent the failed state to SQS and
published to the ops topic, but never touched the alert row -- so the read API's
"failed" state was unreachable and a failed diagnosis showed as "diagnosing" for
as long as anyone cared to look.

It has two callers, because there are two ways a diagnosis dies. The state
machine invokes it first on its failure path, with the execution state. And
EventBridge invokes it for an execution that TIMED_OUT or was ABORTED, which no
Catch inside the state machine can ever see -- for those, the alert ids are in
the execution input carried on the event.
"""
import json
import logging

import boto3
from botocore.exceptions import ClientError

from common import config, progress

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_sns = boto3.client("sns", region_name=config.REGION)


def handler(event, context):
    tenant_id = event["tenant_id"]
    progress.mark_stage(tenant_id, event["alert_id"], progress.NOTIFY)
    message = {
        "tenant_id": tenant_id,
        "alert_id": event["alert_id"],
        "runbook_id": event["runbook"]["runbook_id"],
    }
    try:
        _sns.publish(
            TopicArn=config.RUNBOOK_READY_TOPIC_ARN,
            Subject="Runbook ready",
            Message=json.dumps(message),
            MessageAttributes={
                "tenant_id": {"DataType": "String", "StringValue": tenant_id}
            },
        )
    except ClientError as exc:
        logger.error("runbook-ready notification failed for %s: %s", event["alert_id"], exc)
        return {"notified": False, "reason": exc.response["Error"]["Code"]}
    return {"notified": True}


def _failure_subject(event):
    """(tenant_id, alert_id, error) from either caller's event shape."""
    detail = event.get("detail")
    if isinstance(detail, dict) and "status" in detail:
        # EventBridge: a TIMED_OUT or ABORTED execution. The ids are in the
        # execution input, which arrives as a JSON string.
        try:
            started_with = json.loads(detail.get("input") or "{}")
        except (json.JSONDecodeError, TypeError):
            started_with = {}
        if not isinstance(started_with, dict):
            started_with = {}
        return (
            started_with.get("tenant_id"),
            started_with.get("alert_id"),
            f"States.{str(detail['status']).title().replace('_', '')}",
        )

    return event.get("tenant_id"), event.get("alert_id"), (event.get("error") or {}).get("Error")


def failure_handler(event, context):
    """Mark the alert failed, so the console can say so.

    Nothing here may raise. On the state machine's path it runs inside a catch
    handler, so an exception would replace the real error with this one and skip
    the DLQ send that preserves the gathered context for a redrive.
    """
    tenant_id, alert_id, error = _failure_subject(event)

    recorded = True
    try:
        progress.mark_stage(tenant_id, alert_id, progress.FAILED)
    except Exception as exc:  # noqa: BLE001 - mark_stage swallows its own, this is belt and braces
        logger.error("could not mark %s failed: %s", alert_id, type(exc).__name__)
        recorded = False

    logger.error("diagnosis pipeline failed for alert %s: %s", alert_id, error)
    return {"stage": progress.FAILED, "error": error, "recorded": recorded}
