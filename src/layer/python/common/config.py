import os

PREFIX = "app-b9dac5ac-bc8fbf47"
REGION = os.environ.get("AWS_REGION", "ap-southeast-1")

TENANTS_TABLE = os.environ.get("TENANTS_TABLE", f"{PREFIX}-tenants")
ALERTS_TABLE = os.environ.get("ALERTS_TABLE", f"{PREFIX}-alerts")
DIAGNOSTICS_TABLE = os.environ.get("DIAGNOSTICS_TABLE", f"{PREFIX}-diagnostics")
RUNBOOKS_TABLE = os.environ.get("RUNBOOKS_TABLE", f"{PREFIX}-runbooks")
AUDIT_TABLE = os.environ.get("AUDIT_TABLE", f"{PREFIX}-audit-trail")

CONTEXT_CACHE_BUCKET = os.environ.get("CONTEXT_CACHE_BUCKET", "")
RUNBOOKS_BUCKET = os.environ.get("RUNBOOKS_BUCKET", "")

VECTOR_BUCKET = os.environ.get("VECTOR_BUCKET", f"{PREFIX}-vectors")
VECTOR_INDEX = os.environ.get("VECTOR_INDEX", "incidents")

RUNBOOK_READY_TOPIC_ARN = os.environ.get("RUNBOOK_READY_TOPIC_ARN", "")
OPS_ALARMS_TOPIC_ARN = os.environ.get("OPS_ALARMS_TOPIC_ARN", "")

TENANT_SCOPED_ROLE_ARN = os.environ.get("TENANT_SCOPED_ROLE_ARN", "")

USER_POOL_ID = os.environ.get("USER_POOL_ID", "")

EMBED_MODEL_ID = "cohere.embed-multilingual-v3"
HAIKU_MODEL_ID = "global.anthropic.claude-haiku-4-5-20251001-v1:0"

MAX_EMBED_INPUT_CHARS = 2048

AUDIT_WRITE_FUNCTION_NAME = os.environ.get("AUDIT_WRITE_FUNCTION_NAME", "")
NOTIFY_IMS_FUNCTION_NAME = os.environ.get("NOTIFY_IMS_FUNCTION_NAME", "")
