"""Pipeline progress, recorded on the alert row.

The console renders the five-minute diagnosis as a stage timeline, which needs to
know which stage is running. Nothing recorded that: the Step Functions execution
ARN is never stored against an alert (EventBridge starts the execution and names
it itself), so there was no way to ask.

Each pipeline task marks its own stage here as its first action. Advisory by
design -- a failure to record progress must never fail the stage it is reporting
on, so everything is swallowed and logged. The authoritative outcome is still the
Diagnostics and Runbooks rows.

Because this is advisory and silent, a missing IAM grant here is invisible: a
function whose role cannot assume the tenant-scoped role simply never reports a
stage. tests/test_template_contract.py asserts every function that can reach this
module has that grant, which is how fn-notify's missing one was found.
"""
import logging
import time

from . import config, tenant_scope

logger = logging.getLogger(__name__)

# In pipeline order, which is also the order the UI draws them.
CORRELATE_LOGS = "correlate_logs"
CORRELATE_CONFIG = "correlate_config"
RAG = "rag_context"
RCA = "generate_rca"
RUNBOOK = "generate_runbook"
NOTIFY = "notify"

STAGE_ORDER = (
    CORRELATE_LOGS,
    CORRELATE_CONFIG,
    RAG,
    RCA,
    RUNBOOK,
    NOTIFY,
)

# Not a stage: a terminal marker the read API branches on to report a failed
# diagnosis. Deliberately outside STAGE_ORDER so the timeline never draws it as a
# step of its own.
FAILED = "failed"


def _merged_history(table, tenant_id, alert_id, stage):
    """The stages seen so far, plus this one, deduplicated and in pipeline order.

    This used to be a DynamoDB list_append, which appended unconditionally: every
    Step Functions retry of a stage added another copy, so the timeline drew
    duplicates and the attribute grew without bound on a retried alert.
    """
    seen = {stage}
    try:
        item = table.get_item(
            Key={"tenant_id": tenant_id, "sk": f"alert#{alert_id}"},
            ProjectionExpression="stages_seen",
        ).get("Item") or {}
        seen.update(item.get("stages_seen") or [])
    except Exception as exc:  # noqa: BLE001 - fall back to just this stage
        logger.warning("could not read stage history for %s: %s", alert_id, type(exc).__name__)

    ordered = [s for s in STAGE_ORDER if s in seen]
    # Anything not in STAGE_ORDER (today only FAILED) sorts last, so the terminal
    # marker never displaces a real stage.
    return ordered + sorted(seen - set(STAGE_ORDER))


def mark_stage(tenant_id, alert_id, stage):
    """Record that `stage` has begun for this alert. Never raises."""
    if not tenant_id or not alert_id:
        return
    try:
        table = tenant_scope.tenant_dynamodb_resource(tenant_id).Table(config.ALERTS_TABLE)
        table.update_item(
            Key={"tenant_id": tenant_id, "sk": f"alert#{alert_id}"},
            UpdateExpression=(
                "SET pipeline_stage = :stage, pipeline_stage_at = :now, stages_seen = :seen"
            ),
            ExpressionAttributeValues={
                ":stage": stage,
                ":now": int(time.time()),
                ":seen": _merged_history(table, tenant_id, alert_id, stage),
            },
        )
    except Exception as exc:  # noqa: BLE001 - progress reporting must not fail the stage
        logger.warning(
            "could not record stage %s for %s: %s", stage, alert_id, type(exc).__name__
        )
