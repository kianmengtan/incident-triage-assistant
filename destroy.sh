#!/usr/bin/env bash
set -euo pipefail

PREFIX="app-b9dac5ac-bc8fbf47"
REGION="ap-southeast-1"
STACK_NAME="${STACK_NAME:-${PREFIX}-triage}"

cd "$(dirname "${BASH_SOURCE[0]}")"

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text --region "${REGION}")

echo "==> Emptying S3 buckets owned by this stack (CloudFormation cannot delete non-empty buckets)"
for bucket in "${PREFIX}-context-cache-${ACCOUNT_ID}" "${PREFIX}-runbooks-${ACCOUNT_ID}" "${PREFIX}-audit-exports-${ACCOUNT_ID}"; do
  if aws s3api head-bucket --bucket "${bucket}" --region "${REGION}" 2>/dev/null; then
    aws s3 rm "s3://${bucket}" --recursive --region "${REGION}" || true
  fi
done

echo "==> Deleting stack ${STACK_NAME}"
aws cloudformation delete-stack --stack-name "${STACK_NAME}" --region "${REGION}"
aws cloudformation wait stack-delete-complete --stack-name "${STACK_NAME}" --region "${REGION}"

echo "==> Cleaning up per-tenant Secrets Manager entries (not owned by the CloudFormation stack)"
SECRET_ARNS=$(aws secretsmanager list-secrets \
  --region "${REGION}" \
  --filters Key=name,Values="${PREFIX}-tenant-" \
  --query 'SecretList[].ARN' \
  --output text || true)
for arn in ${SECRET_ARNS}; do
  aws secretsmanager delete-secret --secret-id "${arn}" --force-delete-without-recovery --region "${REGION}" || true
done

echo "==> Destroy complete"
