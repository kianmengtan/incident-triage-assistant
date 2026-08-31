# Architecture: Automated Technical Outage Diagnosis and Remediation
Data Sensitivity: Internal | Pattern: Event-driven serverless (multi-tenant, AI-augmented diagnostics)

## Solution Summary
The platform ingests alerts from external alert-management systems, correlates them asynchronously with logs and configuration/VCS changes, and uses Amazon Bedrock to generate root cause analysis, remediation recommendations, and executable runbooks — all within the 5-minute SLA. All tenant data (alerts, diagnostics, runbooks, audit trail) lives in pooled DynamoDB tables and S3 buckets, logically isolated by tenant_id partitioning, row-level access checks, and per-tenant data-encryption keys. Tenant administrators/engineers authenticate via Cognito and consume diagnostics/runbooks through a REST API; remediation execution remains human-approved and fully audited. The solution is entirely serverless (no VPC-bound compute), so no VPC/subnet dependency exists.

## AWS Services
| Service | Purpose | Config Notes |
|---------|---------|---------------|
| Amazon API Gateway | Public alert-ingestion webhook endpoint + tenant-facing admin/API endpoint | Two REST APIs: ingestion (API-key/HMAC), admin (Cognito authorizer) |
| AWS Lambda | Ingestion normalization, log/config retrieval, RAG context build, Bedrock invocation, notification, remediation trigger | One function per pipeline stage, tenant-scoped IAM |
| AWS Step Functions | Orchestrates alert-to-runbook diagnosis workflow | Standard workflow, one execution per alert |
| Amazon EventBridge | Decouples ingestion from diagnosis; triggers Step Functions | Rule per event type (alert.received) |
| Amazon SQS | Buffers alert bursts; DLQ for failed diagnostic executions | Standard queue + DLQ, visibility timeout tuned to pipeline |
| Amazon SNS | Notifies admins of runbook readiness / execution status; outbound updates to Incident Management System | Topic w/ tenant filter policy |
| Amazon Bedrock | RCA, remediation recommendation, runbook generation; embeddings for RAG | claude-haiku-4-5 for generation, cohere.embed-multilingual-v3 for embeddings |
| Amazon S3 Vectors | Similar-incident/runbook retrieval (RAG) to ground RCA generation | Tenant-partitioned vector namespaces |
| Amazon S3 | Cache of retrieved logs/config diffs, generated runbooks, audit exports | Tenant-prefixed keys, SSE-S3/app-layer encryption |
| Amazon DynamoDB | Tenants, Alerts, Diagnostics, Runbooks, AuditTrail tables | tenant_id partition key, on-demand capacity |
| Amazon Cognito | Tenant admin/engineer/leadership authentication | User pool, tenant_id custom attribute, role groups |
| AWS Secrets Manager | Per-tenant external-integration credentials; per-tenant data encryption keys (DEK) | Rotation enabled, scoped IAM per secret |
| AWS Systems Manager Parameter Store | Tenant integration endpoint configs, feature flags | Standard string params |
| Amazon CloudWatch | Logs, metrics, SLA alarms (runbook delivery >5min, ingestion >1min) | Alarms wired to SNS ops topic |
| AWS X-Ray | End-to-end trace of diagnosis pipeline for latency troubleshooting | Enabled on Lambda + Step Functions |
| AWS IAM | Least-privilege roles; tenant-scoped access conditions | Condition keys on tenant_id claim |

## Data Flow
1. **Alert ingress (FR-01/NFR-02):** External alert system posts webhook to API Gateway → Lambda validates/normalizes payload, writes to Alerts table (DynamoDB, tenant_id key), publishes `alert.received` to EventBridge, and enqueues to SQS as processing buffer.
2. **Diagnosis orchestration:** EventBridge rule triggers Step Functions execution scoped to tenant_id and alert_id.
3. **Correlation (FR-02/FR-03/C-04):** Step Functions invokes Lambda functions in parallel to query log aggregation platform and Git/VCS/deployment APIs using tenant-specific credentials from Secrets Manager; results cached in S3 (tenant prefix) with timestamp/service alignment for correlation.
4. **RAG context (FR-04):** Lambda embeds alert+log+config context via Bedrock (cohere embeddings), queries S3 Vectors for similar past incidents/runbooks within the tenant namespace.
5. **RCA & remediation (FR-04/FR-05):** Lambda invokes Bedrock (claude-haiku) with alert, correlated logs/configs, and RAG context to produce root cause analysis and prioritized remediation steps; result stored in Diagnostics table.
6. **Runbook generation (FR-06/NFR-01):** Lambda invokes Bedrock to render a standardized human-readable runbook (with script/API references), stores document in S3 (runbooks bucket) and metadata in Runbooks table; Step Functions completes within 5-minute budget.
7. **Notification:** SNS notifies tenant admins (and optionally outbound Incident Management System) that runbook is ready; failures route to DLQ/CloudWatch alarm.
8. **UI/API consumption (FR-10/NFR-06):** Tenant users authenticate via Cognito; admin API Gateway (Lambda authorizer enforcing tenant_id claim) serves diagnostics/runbook status/retrieval queries against DynamoDB/S3.
9. **Runbook export/execution (FR-11/C-03):** Authenticated API call triggers Lambda that requires an explicit human-approval flag before calling outbound Remediation Execution Platform; execution status recorded.
10. **Audit trail (FR-09):** All executed remediation actions (manual or automated) written to AuditTrail table with actor, timestamp, tenant_id; retained per compliance window.

## Design Decisions
| Decision | Choice | Rationale |
|----------|--------|-----------|
| AI model for RCA/remediation/runbooks | Amazon Bedrock (claude-haiku-4-5) | Explicit requirement; low-cost approved model satisfies FR-04/05/06 |
| Embeddings for similar-incident retrieval | Bedrock cohere.embed-multilingual-v3 + Amazon S3 Vectors | RAG grounding improves RCA accuracy; OpenSearch not permitted |
| Multi-tenant isolation model | Pooled DynamoDB/S3 with tenant_id partitioning + row-level checks + per-tenant DEKs | Explicit user requirement over siloed infra; satisfies C-01/NFR-07 at lower cost |
| Per-tenant encryption key substitution | AWS Secrets Manager stores per-tenant symmetric DEK used by Lambda for app-layer encrypt/decrypt | AWS KMS not in allowed services list; Secrets Manager is closest approved substitute |
| Async decoupled ingestion | SQS + EventBridge in front of Step Functions | Meets NFR-02 (<1min ingest) and NFR-04 (20+ concurrent sessions) without blocking API Gateway |
| Orchestration engine | AWS Step Functions | Coordinates multi-step correlation/RCA/runbook pipeline reliably within NFR-01 5-min SLA |
| Runbook/diagnostic storage split | S3 for large artifacts, DynamoDB for metadata/status | Matches data volume characteristics (1–3MB runbooks) and NFR-06 fast status queries |
| No VPC/networking components | Fully serverless (no RDS/EC2/Fargate) | No VPC-bound resources required; avoids need for pre-provisioned VPC/subnet inputs |
| Remediation execution gating | Explicit approval flag required before outbound execution call | Enforces C-03 (human approval mandatory) |
| Audit scope | Partial audit (executed remediation actions only) in AuditTrail table | Matches BRD compliance requirement (diagnostic sessions excluded) |

## Security Design
| Concern | Approach |
|---------|----------|
| Authentication | Amazon Cognito user pools (tenant admins/engineers/leadership); API-key/HMAC signature validation for inbound alert webhooks |
| Authorisation | Lambda authorizer validates Cognito tenant_id claim against requested resource; IAM condition keys scope Lambda/DynamoDB/S3 access per tenant |
| Data at rest | DynamoDB (SSE) + S3 (SSE) with additional app-layer encryption using per-tenant DEKs from Secrets Manager |
| Data in transit | TLS 1.2+ enforced on all API Gateway, Lambda-to-external-API, and Bedrock calls |
| Network boundary | No VPC-bound compute; all services are AWS-managed endpoints reached over TLS; API Gateway is sole public ingress |
| Secrets | AWS Secrets Manager holds tenant integration credentials and per-tenant DEKs with rotation enabled; no plaintext in code/config |
| Audit trail | DynamoDB AuditTrail table (remediation actions) + CloudWatch Logs (system-level) retained per 6-month policy |

## Integration Confirmation
| System | Direction | Endpoint Type | Auth | Notes |
|--------|-----------|---------------|------|-------|
| Alert Management (PagerDuty/Opsgenie/AlertManager) | Inbound | REST API / Webhook via API Gateway | API key / HMAC signature | Matches BRD; ingestion Lambda normalizes payload |
| Log Aggregation (ELK/Splunk/Datadog/CloudWatch) | Inbound (pull) | REST API / Query Language via Lambda | Per-tenant credentials (Secrets Manager) | Matches BRD; query results cached in S3 |
| Configuration Mgmt / VCS (Git/Ansible/Terraform) | Inbound (pull) | REST API / Git API via Lambda | Per-tenant tokens (Secrets Manager) | Matches BRD; dependent on C-02 availability |
| Remediation Execution Platform (optional) | Outbound | REST API / Script via Lambda | Per-tenant credentials (Secrets Manager) | Matches BRD; gated by human-approval flag (C-03) |
| Incident Management System (optional) | Outbound | REST API via SNS/Lambda | Per-tenant credentials (Secrets Manager) | Matches BRD; sends diagnostic findings/runbook links |
