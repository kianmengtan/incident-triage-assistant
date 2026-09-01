import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "handlers"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "layer", "python"))

# What the stack passes from its NamePrefix parameter. Set here so the handlers
# build secret names through the same env var they use in production, rather
# than through config.py's compiled-in fallback.
os.environ.setdefault("NAME_PREFIX", "app-b9dac5ac-bc8fbf47")
os.environ.setdefault("TENANTS_TABLE", "app-b9dac5ac-bc8fbf47-tenants")
os.environ.setdefault("ALERTS_TABLE", "app-b9dac5ac-bc8fbf47-alerts")
os.environ.setdefault("DIAGNOSTICS_TABLE", "app-b9dac5ac-bc8fbf47-diagnostics")
os.environ.setdefault("RUNBOOKS_TABLE", "app-b9dac5ac-bc8fbf47-runbooks")
os.environ.setdefault("AUDIT_TABLE", "app-b9dac5ac-bc8fbf47-audit-trail")
os.environ.setdefault("CONTEXT_CACHE_BUCKET", "app-b9dac5ac-bc8fbf47-context-cache-123456789012")
os.environ.setdefault("RUNBOOKS_BUCKET", "app-b9dac5ac-bc8fbf47-runbooks-123456789012")
os.environ.setdefault("TENANT_SCOPED_ROLE_ARN", "arn:aws:iam::123456789012:role/app-b9dac5ac-bc8fbf47-role-tenant-scoped")
os.environ.setdefault("USER_POOL_ID", "ap-southeast-1_testpool")
os.environ.setdefault("USER_POOL_CLIENT_ID", "test-client-id")
os.environ.setdefault("AUDIT_WRITE_FUNCTION_NAME", "app-b9dac5ac-bc8fbf47-fn-audit-write")
os.environ.setdefault("NOTIFY_IMS_FUNCTION_NAME", "app-b9dac5ac-bc8fbf47-fn-notify-ims")
os.environ.setdefault(
    "S3VECTORS_SETUP_FUNCTION_NAME", "app-b9dac5ac-bc8fbf47-fn-s3vectors-setup"
)

# config reads AWS_REGION (what Lambda sets), not AWS_DEFAULT_REGION, so setting
# only the latter meant the tests passed via the hardcoded fallback instead of
# the code path that runs in production.
os.environ.setdefault("AWS_REGION", "ap-southeast-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-1")

# Dummy credentials, so a test with an incomplete patch fails with a signing
# error against a fake key instead of making a real API call with whatever
# credentials the developer happens to have in their environment.
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
