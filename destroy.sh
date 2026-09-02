#!/usr/bin/env bash
#
# Tears down everything deploy.sh created, including the resources
# CloudFormation never owned.
set -euo pipefail

# Must resolve to the same names deploy.sh created.
PREFIX="${PREFIX:-app-b9dac5ac-bc8fbf47}"
REGION="ap-southeast-1"
STACK_NAME="${STACK_NAME:-${PREFIX}-triage}"
ARTIFACTS_BUCKET="${TEMPLATE_BUCKET:-${PREFIX}-artifacts}"

cd "$(dirname "${BASH_SOURCE[0]}")"

empty_bucket() {
  local bucket="$1"
  if aws s3api head-bucket --bucket "${bucket}" --region "${REGION}" 2>/dev/null; then
    echo "    emptying ${bucket}"
    aws s3 rm "s3://${bucket}" --recursive --region "${REGION}" >/dev/null || true
    # Delete markers and noncurrent versions too, so a versioned bucket does not
    # block DeleteStack after the objects appear to be gone.
    aws s3api list-object-versions --bucket "${bucket}" --region "${REGION}" \
      --query '{Objects: Versions[].{Key:Key,VersionId:VersionId}}' --output json 2>/dev/null \
      | grep -q '"Key"' && \
      aws s3api delete-objects --bucket "${bucket}" --region "${REGION}" \
        --delete "$(aws s3api list-object-versions --bucket "${bucket}" --region "${REGION}" \
          --query '{Objects: Versions[].{Key:Key,VersionId:VersionId}}' --output json)" >/dev/null 2>&1 || true
  fi
}

# ---------------------------------------------------------------------------
# Buckets are read from the stack rather than hardcoded: a renamed or added
# bucket would otherwise be missed here and silently block DeleteStack.
# ---------------------------------------------------------------------------
echo "==> Emptying S3 buckets owned by the stack (CloudFormation cannot delete non-empty buckets)"
STACK_BUCKETS=$(aws cloudformation list-stack-resources \
  --stack-name "${STACK_NAME}" \
  --region "${REGION}" \
  --query "StackResourceSummaries[?ResourceType=='AWS::S3::Bucket'].PhysicalResourceId" \
  --output text 2>/dev/null || true)

for bucket in ${STACK_BUCKETS}; do
  empty_bucket "${bucket}"
done

echo "==> Deleting S3 Vectors bucket/index (async setup, not owned by CloudFormation)"
SETUP_FUNCTION_NAME="${PREFIX}-fn-s3vectors-setup"
if aws lambda get-function --function-name "${SETUP_FUNCTION_NAME}" --region "${REGION}" >/dev/null 2>&1; then
  aws lambda invoke \
    --function-name "${SETUP_FUNCTION_NAME}" \
    --payload '{"action":"delete"}' \
    --cli-binary-format raw-in-base64-out \
    --region "${REGION}" \
    /tmp/s3vectors-delete-response.json || true
fi

echo "==> Deleting stack ${STACK_NAME}"
aws cloudformation delete-stack --stack-name "${STACK_NAME}" --region "${REGION}"
if ! aws cloudformation wait stack-delete-complete --stack-name "${STACK_NAME}" --region "${REGION}"; then
  echo "!! stack delete did not complete; remaining resources:" >&2
  aws cloudformation describe-stack-events --stack-name "${STACK_NAME}" --region "${REGION}" \
    --query "StackEvents[?ResourceStatus=='DELETE_FAILED'].[LogicalResourceId,ResourceStatusReason]" \
    --output text >&2 || true
  exit 1
fi

# ---------------------------------------------------------------------------
# Resources CloudFormation never owned.
# ---------------------------------------------------------------------------
# fn-tenant-provision writes these at signup, so CloudFormation never owned them
# and DeleteStack leaves them behind. They are SSM parameters rather than Secrets
# Manager secrets because the deploy permissions boundary grants Secrets Manager
# read only -- see src/layer/python/common/paramstore.py.
echo "==> Cleaning up per-tenant SSM parameters"
TENANT_PARAMS=$(aws ssm get-parameters-by-path \
  --path "/${PREFIX}/tenant" \
  --recursive \
  --region "${REGION}" \
  --query 'Parameters[].Name' \
  --output text || true)
# delete-parameters takes at most ten names per call.
BATCH=""
COUNT=0
for name in ${TENANT_PARAMS}; do
  BATCH="${BATCH} ${name}"
  COUNT=$((COUNT + 1))
  if [ "${COUNT}" -eq 10 ]; then
    aws ssm delete-parameters --names ${BATCH} --region "${REGION}" >/dev/null || true
    BATCH=""
    COUNT=0
  fi
done
if [ -n "${BATCH}" ]; then
  aws ssm delete-parameters --names ${BATCH} --region "${REGION}" >/dev/null || true
fi

echo "==> Deleting the deploy artifacts bucket"
empty_bucket "${ARTIFACTS_BUCKET}"
aws s3api delete-bucket --bucket "${ARTIFACTS_BUCKET}" --region "${REGION}" 2>/dev/null || true

# The vector bucket is created by the S3VectorsSetup custom resource, which
# deletes it on stack delete. Checked here because that delete is best-effort:
# a leftover vector bucket is a resource the platform will not clean up.
echo "==> Verifying the vector bucket is gone"
if aws s3vectors get-vector-bucket --vector-bucket-name "${PREFIX}-vectors" --region "${REGION}" >/dev/null 2>&1; then
  echo "    still present, deleting directly"
  aws s3vectors delete-index --vector-bucket-name "${PREFIX}-vectors" --index-name incidents --region "${REGION}" >/dev/null 2>&1 || true
  aws s3vectors delete-vector-bucket --vector-bucket-name "${PREFIX}-vectors" --region "${REGION}" >/dev/null 2>&1 || true
fi

rm -f outputs.json

echo "==> Destroy complete"
