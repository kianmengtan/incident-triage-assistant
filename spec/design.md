# Design: Automated Technical Outage Diagnosis and Remediation

## Overview
Alerts land on a public webhook (API Gateway + HMAC), are normalized by Lambda, and fan out through EventBridge/SQS into a Step Functions pipeline that pulls logs and config/VCS changes via tenant-scoped credentials, embeds context for RAG lookup against S3 Vectors, and calls Bedrock (claude-haiku-4-5) to produce RCA, remediation steps, and a runbook stored in S3/DynamoDB. Tenant users authenticate via Cognito and consume diagnostics/runbooks through a Cognito-authorized admin API; remediation execution against the outbound Remediation Execution Platform is gated behind an explicit Tenant-Admin-only approval action, with every executed action written to an AuditTrail table. Optional outbound notification to a customer's Incident Management System fires alongside approval/execution, using the same per-tenant Secrets Manager credential pattern as inbound integrations.

## Data Model
| Table / Store | Key Schema | Key Attributes | Notes |
|----------------|------------|-----------------|-------|
| Tenants | PK `tenant_id` / SK `PROFILE` | `name`, `status`, `dek_secret_arn`, `created_at` | One profile item per tenant |
| Alerts | PK `tenant_id` / SK `alert#{received_at}#{alert_id}` | `alert_id`, `source`, `severity`, `service`, `status`, `sfn_execution_arn` | Status: received/processing/complete/failed |
| Diagnostics | PK `tenant_id` / SK `diag#{alert_id}` | `alert_id`, `rca_summary`, `remediation_steps`, `rag_context_refs`, `model_version`, `generated_at` | 1:1 with Alerts |
| Runbooks | PK `tenant_id` / SK `runbook#{runbook_id}` | `alert_id`, `diagnostic_id`, `s3_key`, `status`, `approval_status`, `approved_by`, `generated_at` | GSI `status-index` (PK `tenant_id`, SK `status`) for admin listing |
| AuditTrail | PK `tenant_id` / SK `audit#{timestamp}#{actor}` | `action`, `actor`, `runbook_id`, `alert_id`, `result` | Append-only; 6-month TTL attribute |

## API / Interface Contracts
| Endpoint or Interface | Method | Request | Response | Auth |
|------------------------|--------|---------|----------|------|
| `/v1/alerts` (ingestion API) | POST | Alert payload (id, severity, service, timestamp, description) | 202 `{alert_id, status}` | API key + HMAC signature |
| `/v1/diagnostics/{alertId}` (admin API) | GET | Path param `alertId` | RCA + remediation JSON | Cognito JWT (any tenant role) |
| `/v1/runbooks` (admin API) | GET | Query `status` (optional) | List of runbook summaries | Cognito JWT (any tenant role) |
| `/v1/runbooks/{runbookId}` (admin API) | GET | Path param `runbookId` | Runbook detail + presigned S3 URL | Cognito JWT (any tenant role) |
| `/v1/runbooks/{runbookId}/approve` (admin API) | POST | `{approved: true}` | 200 execution status / 403 if not admin | Cognito JWT, group=`TenantAdmin` |
| `/v1/runbooks/{runbookId}/export` (admin API) | GET | Path param `runbookId` | Runbook JSON w/ script/API references | Cognito JWT (any tenant role) |
| Remediation Execution Platform (outbound) | POST | Normalized runbook action payload | Execution status/result | Per-tenant credentials (Secrets Manager) |
| Incident Management System (outbound) | POST | Diagnostic findings + runbook link | Ack | Per-tenant credentials (Secrets Manager) |

## Sequence Detail
1. External alert source posts to the ingestion webhook.
2. API Gateway invokes the Ingestion Normalizer Lambda synchronously.
3. Lambda writes the Alerts row and returns 202 immediately (meets NFR-02 <1min).
4. Lambda fires `alert.received` to EventBridge (async, fire-and-forget).
5. EventBridge rule starts a Step Functions execution scoped to tenant_id/alert_id.
6. Step Functions invokes log/config correlation Lambdas in parallel.
7. Correlation output is embedded and matched against S3 Vectors for similar past incidents.
8. Step Functions invokes Bedrock (claude-haiku) with alert + correlated context + RAG results to produce RCA/remediation, stored in Diagnostics table.
9. Step Functions invokes the Runbook Generator, which stores the document in S3 and metadata in Runbooks table (must complete within 5-min SLA, NFR-01).
10. Runbook Generator fires notification (async) to the Notification Dispatcher.
11. Notification Dispatcher publishes to SNS, which delivers to tenant admins (and optionally the Incident Management System).

12. Admin retrieves runbook detail via the admin API; the authorizer Lambda validates the Cognito JWT and attaches the tenant_id/role claims.
13. Query Lambda returns runbook detail (status, S3-backed content link) within NFR-06's 2-second target.
14. Admin submits an approval request on the same runbook.
15. Approve Lambda checks the authorizer's role claim: only `TenantAdmin` may proceed (per approved design decision); any other role receives 403.
16. On approval, Approve Lambda calls the tenant's Remediation Execution Platform using credentials from Secrets Manager.
17. Execution result is written asynchronously to the AuditTrail table (actor, timestamp, tenant_id, result).
18. Approve Lambda asynchronously notifies the tenant's Incident Management System with the diagnostic findings/runbook link, if configured.

## IAM & Access Design
| Principal | Resource | Actions | Justification |
|-----------|----------|---------|----------------|
| `fn-ingest-normalize` role | Alerts table, EventBridge bus, SQS queue | `dynamodb:PutItem` (tenant_id condition), `events:PutEvents`, `sqs:SendMessage` | Normalize and enqueue inbound alerts |
| `fn-correlate-logs` / `fn-correlate-config` roles | Secrets Manager (tenant creds), S3 context-cache bucket | `secretsmanager:GetSecretValue` (tenant-scoped ARN condition), `s3:PutObject` (tenant prefix) | Pull external logs/config using tenant credentials |
| `fn-rag-context` role | S3 Vectors index, Bedrock embed model | `s3vectors:QueryVectors`/`PutVectors` (tenant namespace), `bedrock:InvokeModel` (cohere.embed-multilingual-v3) | RAG retrieval grounding |
| `fn-generate-rca` role | Bedrock generation model, Diagnostics table, S3 context-cache | `bedrock:InvokeModel` (claude-haiku-4-5), `dynamodb:PutItem` (tenant_id condition), `s3:GetObject` (tenant prefix) | Produce RCA/remediation |
| `fn-generate-runbook` role | Bedrock generation model, S3 runbooks bucket, Runbooks table | `bedrock:InvokeModel`, `s3:PutObject` (tenant prefix), `dynamodb:PutItem` (tenant_id condition) | Render and store runbook |
| `fn-notify` / `fn-notify-ims` roles | SNS topics, Secrets Manager (IMS creds) | `sns:Publish`, `secretsmanager:GetSecretValue` | Notify admins / outbound IMS update |
| `fn-authorizer` role | Cognito user pool | `cognito-idp:GetUser` | Validate JWT, extract tenant_id + group claim |
| `fn-query-diagnostics` role | Diagnostics, Runbooks tables | `dynamodb:GetItem`/`Query` (tenant_id condition) | Serve read endpoints |
| `fn-trigger-remediation` role | Runbooks table, Secrets Manager (remediation creds), AuditTrail table | `dynamodb:UpdateItem`, `secretsmanager:GetSecretValue`, `dynamodb:PutItem`; invocation gated on authorizer context `group == TenantAdmin` | Enforces C-03 human-approval, Admin-only per user decision |
| `fn-audit-write` role | AuditTrail table | `dynamodb:PutItem` (tenant_id condition) | Immutable action log |
| Step Functions execution role | All pipeline stage Lambdas | `lambda:InvokeFunction` (scoped to named function ARNs) | Orchestration |
| Cognito group `TenantAdmin` | Admin API `/runbooks/{id}/approve` | Route-level authorization | Only admins may authorize remediation execution |
| Cognito group `TenantEngineer`/`TenantLeadership` | Admin API read routes | Route-level authorization | View-only access to diagnostics/runbooks |

## Error Handling & Observability
| Concern | Approach |
|---------|----------|
| Retries/idempotency | Alerts table conditional write on `alert_id` dedups re-delivered webhooks; Step Functions retry policy (exponential backoff, max 3 attempts) on transient Lambda/Bedrock errors |
| Failure alerting | CloudWatch Alarms on Step Functions execution failures and SQS DLQ depth publish to `outagediag-{env}-topic-ops-alarms` |
| SLA breach detection | CloudWatch Alarms on runbook-delivery latency (>5min) and ingestion latency (>1min) per BRD NFR-01/NFR-02 |
| Logging | Structured JSON logs per Lambda to CloudWatch Logs, correlated by `tenant_id` + `alert_id` |
| Tracing | AWS X-Ray enabled on all Lambdas and the Step Functions state machine; trace ID propagated through Bedrock invocation spans |
| Bedrock throttling | Lambda-side retry with jittered backoff; persistent failure routes execution to SQS DLQ for manual redrive |
| Remediation execution failures | Captured as `result=failed` in AuditTrail; admin notified via SNS ops topic for manual follow-up |
