import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "handlers"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "layer", "python"))

os.environ.setdefault("TENANTS_TABLE", "app-b9dac5ac-bc8fbf47-tenants")
os.environ.setdefault("ALERTS_TABLE", "app-b9dac5ac-bc8fbf47-alerts")
os.environ.setdefault("DIAGNOSTICS_TABLE", "app-b9dac5ac-bc8fbf47-diagnostics")
os.environ.setdefault("RUNBOOKS_TABLE", "app-b9dac5ac-bc8fbf47-runbooks")
os.environ.setdefault("AUDIT_TABLE", "app-b9dac5ac-bc8fbf47-audit-trail")
os.environ.setdefault("CONTEXT_CACHE_BUCKET", "app-b9dac5ac-bc8fbf47-context-cache-123456789012")
os.environ.setdefault("RUNBOOKS_BUCKET", "app-b9dac5ac-bc8fbf47-runbooks-123456789012")
os.environ.setdefault("TENANT_SCOPED_ROLE_ARN", "arn:aws:iam::123456789012:role/app-b9dac5ac-bc8fbf47-role-tenant-scoped")
os.environ.setdefault("USER_POOL_ID", "us-east-1_testpool")
os.environ.setdefault("AUDIT_WRITE_FUNCTION_NAME", "app-b9dac5ac-bc8fbf47-fn-audit-write")
os.environ.setdefault("NOTIFY_IMS_FUNCTION_NAME", "app-b9dac5ac-bc8fbf47-fn-notify-ims")
os.environ.setdefault("S3VECTORS_SETUP_FUNCTION_NAME", "app-b9dac5ac-bc8fbf47-fn-s3vectors-setup")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-1")
