# Resource Inventory
| Resource | AWS Service | Naming Pattern | Purpose |
|----------|-------------|-----------------|---------|
| Ingestion REST API | API Gateway | `outagediag-{env}-ingestion-api` | Public webhook for inbound alerts (API key/HMAC) |
| Admin REST API | API Gateway | `outagediag-{env}-admin-api` | Tenant-facing diagnostics/runbook/approval API (Cognito authorizer) |
| Lambda: Ingestion Normalizer | AWS Lambda | `outagediag-{env}-fn-ingest-normalize` | Validate/normalize alert payload, write Alerts row, emit event |
| Lambda: Log Correlation | AWS Lambda | `outagediag-{env}-fn-correlate-logs` | Query tenant log platform, cache results in S3 |
| Lambda: Config/VCS Correlation | AWS Lambda | `outagediag-{env}-fn-correlate-config` | Query tenant Git/deployment APIs, cache diffs in S3 |
| Lambda: RAG Context Builder | AWS Lambda | `outagediag-{env}-fn-rag-context` | Embed alert+log+config context (cohere), query S3 Vectors |
| Lambda: RCA & Remediation Generator | AWS Lambda | `outagediag-{env}-fn-generate-rca` | Invoke Bedrock claude-haiku for RCA + remediation steps |
| Lambda: Runbook Generator | AWS Lambda | `outagediag-{env}-fn-generate-runbook` | Render runbook doc, store in S3 + Runbooks table |
| Lambda: Notification Dispatcher | AWS Lambda | `outagediag-{env}-fn-notify` | Publish runbook-ready notifications to SNS |
| Lambda: Admin API Authorizer | AWS Lambda | `outagediag-{env}-fn-authorizer` | Validate Cognito JWT, inject tenant_id/role into context |
| Lambda: Diagnostics/Runbook Query | AWS Lambda | `outagediag-{env}-fn-query-diagnostics` | Backend for GET diagnostics/runbooks endpoints |
| Lambda: Remediation Execution Trigger | AWS Lambda | `outagediag-{env}-fn-trigger-remediation` | Enforce Tenant-Admin approval, call Remediation Execution Platform |
| Lambda: Incident Mgmt Notifier | AWS Lambda | `outagediag-{env}-fn-notify-ims` | Push diagnostic findings/runbook link to Incident Mgmt System |
| Lambda: Audit Writer | AWS Lambda | `outagediag-{env}-fn-audit-write` | Persist executed-action records to AuditTrail |
| Step Functions State Machine | AWS Step Functions | `outagediag-{env}-sfn-diagnosis-pipeline` | Orchestrates correlation → RAG → RCA → runbook stages |
| EventBridge Rule | Amazon EventBridge | `outagediag-{env}-rule-alert-received` | Triggers Step Functions on `alert.received` |
| SQS Queue + DLQ | Amazon SQS | `outagediag-{env}-queue-alert-buffer` / `-dlq` | Buffers alert bursts; DLQ for failed executions |
| SNS Topic: Runbook Ready | Amazon SNS | `outagediag-{env}-topic-runbook-ready` | Notifies tenant admins runbook is available |
| SNS Topic: Ops Alarms | Amazon SNS | `outagediag-{env}-topic-ops-alarms` | Routes CloudWatch SLA alarms to ops |
| S3 Vectors Index | Amazon S3 Vectors | `outagediag-{env}-vectors-incidents` (namespace `tenant/{tenant_id}`) | Similar-incident/runbook retrieval for RAG |
| S3 Bucket: Context Cache | Amazon S3 | `outagediag-{env}-s3-context-cache` (prefix `tenant/{tenant_id}/alert/{alert_id}/`) | Cached logs/config diffs pending correlation |
| S3 Bucket: Runbooks | Amazon S3 | `outagediag-{env}-s3-runbooks` (prefix `tenant/{tenant_id}/runbook/{runbook_id}/`) | Generated runbook documents |
| S3 Bucket: Audit Exports | Amazon S3 | `outagediag-{env}-s3-audit-exports` (prefix `tenant/{tenant_id}/`) | Periodic audit trail exports |
| DynamoDB: Tenants | Amazon DynamoDB | `outagediag-{env}-ddb-tenants` | Tenant registry/config metadata |
| DynamoDB: Alerts | Amazon DynamoDB | `outagediag-{env}-ddb-alerts` | Ingested alert records |
| DynamoDB: Diagnostics | Amazon DynamoDB | `outagediag-{env}-ddb-diagnostics` | RCA/remediation output per alert |
| DynamoDB: Runbooks | Amazon DynamoDB | `outagediag-{env}-ddb-runbooks` | Runbook metadata/status/approval state |
| DynamoDB: AuditTrail | Amazon DynamoDB | `outagediag-{env}-ddb-audit-trail` | Executed remediation action log |
| Cognito User Pool | Amazon Cognito | `outagediag-{env}-cognito-userpool` | Tenant admin/engineer/leadership auth; groups TenantAdmin/TenantEngineer/TenantLeadership |
| Secrets Manager: Integration Creds | AWS Secrets Manager | `outagediag/{env}/tenant/{tenant_id}/integration-creds` | Per-tenant log/VCS/remediation/IMS credentials |
| Secrets Manager: Tenant DEK | AWS Secrets Manager | `outagediag/{env}/tenant/{tenant_id}/dek` | Per-tenant app-layer data encryption key |
| SSM Parameter Store | AWS Systems Manager Parameter Store | `/outagediag/{env}/tenant/{tenant_id}/endpoints`, `/outagediag/{env}/feature-flags/{flag}` | Tenant endpoint configs, feature flags |
| CloudWatch Alarms | Amazon CloudWatch | `outagediag-{env}-alarm-runbook-sla`, `outagediag-{env}-alarm-ingestion-sla` | SLA breach detection (>5min runbook, >1min ingest) |
