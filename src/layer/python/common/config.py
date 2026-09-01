import os

# Set from the stack's NamePrefix parameter. The fallback must track that
# parameter's default: it is what the Secrets Manager names below are built from,
# and the IAM policies that authorise them are written as ${NamePrefix}-tenant-*,
# so a stale value here fails at runtime rather than at deploy time.
PREFIX = os.environ.get("NAME_PREFIX", "app-b9dac5ac-bc8fbf47-v2")
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
USER_POOL_CLIENT_ID = os.environ.get("USER_POOL_CLIENT_ID", "")

# Cognito ID tokens carry the caller's groups, so the authorizer resolves roles
# from the verified token instead of an API call per request.
GROUP_PRIORITY = ("TenantAdmin", "TenantEngineer", "TenantLeadership")

EMBED_MODEL_ID = "cohere.embed-multilingual-v3"
HAIKU_MODEL_ID = "global.anthropic.claude-haiku-4-5-20251001-v1:0"

MAX_EMBED_INPUT_CHARS = 2048

# NFR-01 gives the whole pipeline five minutes; NFR-05 gives correlation 60s.
RUNBOOK_SLA_SECONDS = 300
CORRELATION_SLA_SECONDS = 60

# Presigned runbook downloads. The signing session is sized from this, so the
# URL cannot outlive the credentials behind it.
RUNBOOK_URL_TTL_SECONDS = 900

AUDIT_WRITE_FUNCTION_NAME = os.environ.get("AUDIT_WRITE_FUNCTION_NAME", "")
NOTIFY_IMS_FUNCTION_NAME = os.environ.get("NOTIFY_IMS_FUNCTION_NAME", "")
S3VECTORS_SETUP_FUNCTION_NAME = os.environ.get("S3VECTORS_SETUP_FUNCTION_NAME", "")
