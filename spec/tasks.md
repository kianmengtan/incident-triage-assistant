# Implementation Tasks: Automated Technical Outage Diagnosis and Remediation

- [ ] 1. Create ingestion API Gateway REST API
  - Public webhook `outagediag-{env}-ingestion-api` with API key/HMAC validation for `/v1/alerts`
  - _Requirements: 1_
  - _Verify: `sam validate` succeeds against the template defining this API resource_

- [ ] 2. Create admin API Gateway REST API
  - Cognito-authorized `outagediag-{env}-admin-api` exposing `/v1/diagnostics/{alertId}`, `/v1/runbooks`, `/v1/runbooks/{runbookId}`, `/v1/runbooks/{runbookId}/approve`, `/v1/runbooks/{runbookId}/export`
  - _Requirements: 6, 7_
  - _Verify: `sam validate` succeeds against the template defining this API resource_

- [ ] 3. Implement Ingestion Normalizer Lambda
  - `outagediag-{env}-fn-ingest-normalize` validates HMAC, writes Alerts row conditionally, publishes `alert.received`
  - _Requirements: 1, 2_
  - _Verify: unit test asserts a duplicate `alert_id` invocation raises a conditional-check-failed path without a second write_

- [ ] 4. Implement Log Correlation Lambda
  - `outagediag-{env}-fn-correlate-logs` queries tenant log platform using Secrets Manager credentials, caches to S3 context-cache prefix
  - _Requirements: 3_
  - _Verify: unit test asserts function writes to S3 key path containing `tenant/{tenant_id}/alert/`_

- [ ] 5. Implement Config/VCS Correlation Lambda
  - `outagediag-{env}-fn-correlate-config` queries tenant Git/deployment APIs, caches diffs to S3 context-cache prefix
  - _Requirements: 3_
  - _Verify: unit test asserts function writes to S3 key path containing `tenant/{tenant_id}/alert/`_

- [ ] 6. Implement RAG Context Builder Lambda
  - `outagediag-{env}-fn-rag-context` embeds context via cohere.embed-multilingual-v3, queries S3 Vectors tenant namespace
  - _Requirements: 4_
  - _Verify: unit test asserts embedding call uses model id `cohere.embed-multilingual-v3` and input text length is capped at 2048 characters_

- [ ] 7. Implement RCA & Remediation Generator Lambda
  - `outagediag-{env}-fn-generate-rca` invokes Bedrock claude-haiku-4-5 with alert+context+RAG results, writes Diagnostics table row
  - _Requirements: 4_
  - _Verify: unit test asserts Bedrock invocation uses model id `global.anthropic.claude-haiku-4-5-20251001-v1:0` and asserts a Diagnostics item is put with keys `tenant_id`/`diag#{alert_id}`_

- [ ] 8. Implement Runbook Generator Lambda
  - `outagediag-{env}-fn-generate-runbook` renders runbook via Bedrock, stores document in S3 runbooks bucket and metadata in Runbooks table
  - _Requirements: 5_
  - _Verify: unit test asserts an S3 put under `tenant/{tenant_id}/runbook/` and a Runbooks table put with `approval_status` default `pending`_

- [ ] 9. Implement Notification Dispatcher Lambda
  - `outagediag-{env}-fn-notify` publishes to SNS `outagediag-{env}-topic-runbook-ready` on runbook completion
  - _Requirements: 5_
  - _Verify: unit test asserts `sns:Publish` call targets the runbook-ready topic ARN pattern_

- [ ] 10. Implement Step Functions diagnosis pipeline state machine
  - `outagediag-{env}-sfn-diagnosis-pipeline` orchestrates correlation (parallel) → RAG → RCA → runbook stages with retry policy (exponential backoff, max 3)
  - _Requirements: 2_
  - _Verify: `sam validate` succeeds and the state machine definition JSON contains a `Retry` block with `MaxAttempts: 3` on each stage_

- [ ] 11. Create EventBridge rule for alert.received
  - `outagediag-{env}-rule-alert-received` triggers the Step Functions execution
  - _Requirements: 2_
  - _Verify: `sam validate` succeeds against the template defining this EventBridge rule and its target_

- [ ] 12. Create SQS buffer queue and DLQ
  - `outagediag-{env}-queue-alert-buffer` and `-dlq` with visibility timeout tuned to pipeline duration
  - _Requirements: 2_
  - _Verify: `sam validate` succeeds and the DLQ is referenced as `RedrivePolicy` target on the primary queue_

- [ ] 13. Create SNS topics
  - `outagediag-{env}-topic-runbook-ready` and `outagediag-{env}-topic-ops-alarms`
  - _Requirements: 5_
  - _Verify: `sam validate` succeeds against the template defining both SNS topic resources_

- [ ] 14. Provision S3 Vectors index
  - `outagediag-{env}-vectors-incidents` with tenant-partitioned namespaces
  - _Requirements: 4_
  - _Verify: `sam validate` succeeds against the template defining this resource_

- [ ] 15. Provision S3 buckets
  - `outagediag-{env}-s3-context-cache`, `outagediag-{env}-s3-runbooks`, `outagediag-{env}-s3-audit-exports` with tenant-prefix conventions and SSE enabled
  - _Requirements: 3, 5, 10_
  - _Verify: `sam validate` succeeds and each bucket resource declares `BucketEncryption`_

- [ ] 16. Provision DynamoDB tables
  - Tenants, Alerts, Diagnostics, Runbooks (with `status-index` GSI), AuditTrail per Data Model schema
  - _Requirements: 1, 2, 4, 5, 7, 10_
  - _Verify: `sam validate` succeeds and the Runbooks table resource declares a GSI named `status-index`_

- [ ] 17. Provision Cognito User Pool
  - `outagediag-{env}-cognito-userpool` with groups TenantAdmin/TenantEngineer/TenantLeadership and `tenant_id` custom attribute
  - _Requirements: 6, 7_
  - _Verify: `sam validate` succeeds and the user pool resource defines a custom attribute named `tenant_id`_

- [ ] 18. Implement Admin API Authorizer Lambda
  - `outagediag-{env}-fn-authorizer` validates Cognito JWT, extracts tenant_id/group claims into request context
  - _Requirements: 6, 7_
  - _Verify: unit test asserts the authorizer output context includes `tenant_id` and `group` keys_

- [ ] 19. Implement Diagnostics/Runbook Query Lambda
  - `outagediag-{env}-fn-query-diagnostics` serves GET endpoints scoped by tenant_id
  - _Requirements: 6_
  - _Verify: unit test asserts query is scoped with a `tenant_id` key condition and rejects mismatched tenant_id in path/context_

- [ ] 20. Implement Remediation Execution Trigger Lambda
  - `outagediag-{env}-fn-trigger-remediation` enforces TenantAdmin-only approval gate before calling Remediation Execution Platform; updates Runbooks approval_status
  - _Requirements: 7_
  - _Verify: unit test asserts a request with `group != TenantAdmin` returns 403 and does not invoke the outbound HTTP client_

- [ ] 21. Implement Incident Management Notifier Lambda
  - `outagediag-{env}-fn-notify-ims` sends diagnostic findings/runbook link to tenant's configured IMS endpoint, non-blocking on failure
  - _Requirements: 8_
  - _Verify: unit test asserts a simulated IMS call failure is caught and logged without raising an exception to the caller_

- [ ] 22. Implement Audit Writer Lambda
  - `outagediag-{env}-fn-audit-write` persists append-only AuditTrail records with actor/timestamp/tenant_id/result
  - _Requirements: 10_
  - _Verify: unit test asserts the AuditTrail put includes `tenant_id`, `actor`, `action`, and `result` attributes_

- [ ] 23. Configure per-tenant Secrets Manager entries
  - `outagediag/{env}/tenant/{tenant_id}/integration-creds` and `/dek` with rotation enabled
  - _Requirements: 3, 8, 9_
  - _Verify: `sam validate` succeeds against the template defining the Secrets Manager resources and rotation configuration_

- [ ] 24. Configure SSM Parameter Store entries
  - `/outagediag/{env}/tenant/{tenant_id}/endpoints` and `/outagediag/{env}/feature-flags/{flag}`
  - _Requirements: 3_
  - _Verify: `sam validate` succeeds against the template defining the SSM parameter resources_

- [ ] 25. Configure CloudWatch alarms and X-Ray tracing
  - `outagediag-{env}-alarm-runbook-sla`, `outagediag-{env}-alarm-ingestion-sla`; enable X-Ray on all Lambdas and the state machine
  - _Requirements: 5_
  - _Verify: `sam validate` succeeds and each Lambda/state machine resource declares `Tracing: Active` / `TracingConfiguration`_

- [ ] 26. Wire IAM role for `fn-ingest-normalize`
  - Scope `dynamodb:PutItem`, `events:PutEvents`, `sqs:SendMessage` with tenant_id condition
  - _Requirements: 1, 9_
  - _Verify: `sam validate` succeeds and the IAM policy JSON includes a `Condition` block referencing `tenant_id`_

- [ ] 27. Wire IAM roles for `fn-correlate-logs` and `fn-correlate-config`
  - Scope `secretsmanager:GetSecretValue` (tenant-scoped ARN condition) and `s3:PutObject` (tenant prefix)
  - _Requirements: 3, 9_
  - _Verify: `sam validate` succeeds and the IAM policy JSON restricts the S3 action to a tenant-prefixed resource pattern_

- [ ] 28. Wire IAM role for `fn-rag-context`
  - Scope `s3vectors:QueryVectors`/`PutVectors` to tenant namespace and `bedrock:InvokeModel` to cohere.embed-multilingual-v3
  - _Requirements: 4, 9_
  - _Verify: `sam validate` succeeds and the IAM policy JSON's `Resource` for the Bedrock action references only the approved embedding model id_

- [ ] 29. Wire IAM role for `fn-generate-rca`
  - Scope `bedrock:InvokeModel` to claude-haiku-4-5, `dynamodb:PutItem` (tenant_id condition), `s3:GetObject` (tenant prefix)
  - _Requirements: 4, 9_
  - _Verify: `sam validate` succeeds and the IAM policy JSON's `Resource` for the Bedrock action references only the approved generation model id_

- [ ] 30. Wire IAM role for `fn-generate-runbook`
  - Scope `bedrock:InvokeModel`, `s3:PutObject` (tenant prefix), `dynamodb:PutItem` (tenant_id condition)
  - _Requirements: 5, 9_
  - _Verify: `sam validate` succeeds against the template defining this IAM role_

- [ ] 31. Wire IAM roles for `fn-notify` and `fn-notify-ims`
  - Scope `sns:Publish` and `secretsmanager:GetSecretValue`
  - _Requirements: 5, 8_
  - _Verify: `sam validate` succeeds against the template defining these IAM roles_

- [ ] 32. Wire IAM role for `fn-authorizer`
  - Scope `cognito-idp:GetUser`
  - _Requirements: 6_
  - _Verify: `sam validate` succeeds against the template defining this IAM role_

- [ ] 33. Wire IAM role for `fn-query-diagnostics`
  - Scope `dynamodb:GetItem`/`Query` with tenant_id condition on Diagnostics and Runbooks tables
  - _Requirements: 6, 9_
  - _Verify: `sam validate` succeeds and the IAM policy JSON includes a `Condition` block referencing `tenant_id`_

- [ ] 34. Wire IAM role for `fn-trigger-remediation`
  - Scope `dynamodb:UpdateItem`, `secretsmanager:GetSecretValue`, `dynamodb:PutItem`; enforce TenantAdmin-only invocation per OED-01
  - _Requirements: 7, 9_
  - _Verify: unit test asserts the role's policy document does not grant access unless invoked with a TenantAdmin group context_
  - _Blocked by: OED-01_

- [ ] 35. Wire IAM role for `fn-audit-write`
  - Scope `dynamodb:PutItem` with tenant_id condition on AuditTrail table
  - _Requirements: 10, 9_
  - _Verify: `sam validate` succeeds and the IAM policy JSON includes a `Condition` block referencing `tenant_id`_

- [ ] 36. Wire Step Functions execution role
  - Scope `lambda:InvokeFunction` to the named pipeline stage function ARNs only
  - _Requirements: 2_
  - _Verify: `sam validate` succeeds and the IAM policy JSON's `Resource` list contains only the pipeline Lambda ARNs, not `*`_

- [ ] 37. Configure Cognito group-based route authorization
  - TenantAdmin group required on `/v1/runbooks/{runbookId}/approve`; TenantEngineer/TenantLeadership permitted on read routes
  - _Requirements: 6, 7_
  - _Verify: unit test asserts the approve route handler checks for `TenantAdmin` membership before proceeding_
