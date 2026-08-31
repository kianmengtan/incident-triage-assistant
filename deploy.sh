#!/usr/bin/env bash
set -euo pipefail

PREFIX="app-b9dac5ac-bc8fbf47"
REGION="ap-southeast-1"
STACK_NAME="${STACK_NAME:-${PREFIX}-triage}"
ARTIFACTS_BUCKET="${TEMPLATE_BUCKET:-${PREFIX}-artifacts}"

cd "$(dirname "${BASH_SOURCE[0]}")"

echo "==> Deploying stack: ${STACK_NAME}"

echo "==> Checking for an existing ${STACK_NAME} stack in a failed/rollback state"
STACK_STATUS=$(aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" \
  --region "${REGION}" \
  --query 'Stacks[0].StackStatus' \
  --output text 2>/dev/null || echo "STACK_NOT_FOUND")

case "${STACK_STATUS}" in
  CREATE_FAILED|ROLLBACK_COMPLETE|ROLLBACK_IN_PROGRESS|ROLLBACK_FAILED|UPDATE_FAILED|UPDATE_ROLLBACK_COMPLETE|UPDATE_ROLLBACK_IN_PROGRESS|DELETE_FAILED)
    echo "==> Stack ${STACK_NAME} is in ${STACK_STATUS}; deleting it before redeploying"
    aws cloudformation delete-stack --stack-name "${STACK_NAME}" --region "${REGION}"
    if ! timeout 300 aws cloudformation wait stack-delete-complete \
      --stack-name "${STACK_NAME}" \
      --region "${REGION}"; then
      echo "ERROR: Timed out waiting for stack ${STACK_NAME} to finish deleting" >&2
      exit 1
    fi
    echo "==> Stack ${STACK_NAME} deleted"
    ;;
  STACK_NOT_FOUND)
    echo "==> No existing ${STACK_NAME} stack found; proceeding"
    ;;
  *)
    echo "==> Stack ${STACK_NAME} exists in state ${STACK_STATUS}; proceeding with update"
    ;;
esac

echo "==> Ensuring artifacts bucket ${ARTIFACTS_BUCKET} exists in ${REGION}"
if ! aws s3api head-bucket --bucket "${ARTIFACTS_BUCKET}" --region "${REGION}" 2>/dev/null; then
  aws s3api create-bucket \
    --bucket "${ARTIFACTS_BUCKET}" \
    --region "${REGION}" \
    --create-bucket-configuration LocationConstraint="${REGION}"
fi

echo "==> sam build"
sam build --use-container --template-file template.yaml

echo "==> sam deploy"
sam deploy \
  --stack-name "${STACK_NAME}" \
  --s3-bucket "${ARTIFACTS_BUCKET}" \
  --region "${REGION}" \
  --capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
  --no-confirm-changeset \
  --no-fail-on-empty-changeset

echo "==> Collecting stack outputs"
OUTPUTS_JSON=$(aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" \
  --region "${REGION}" \
  --query 'Stacks[0].Outputs' \
  --output json)

python3 - "$OUTPUTS_JSON" <<'PY'
import json
import sys

outputs = json.loads(sys.argv[1] or "[]")
flat = {o["OutputKey"]: o["OutputValue"] for o in outputs}
flat["app_url"] = flat.get("AdminApiUrl", "")
with open("outputs.json", "w") as fh:
    json.dump(flat, fh, indent=2)
print(json.dumps(flat, indent=2))
PY

echo "==> Triggering async S3 Vectors setup (does not block this deploy)"
S3VECTORS_TOPIC_ARN=$(python3 - "$OUTPUTS_JSON" <<'PY'
import json
import sys

outputs = json.loads(sys.argv[1] or "[]")
flat = {o["OutputKey"]: o["OutputValue"] for o in outputs}
print(flat.get("S3VectorsSetupTopicArn", ""))
PY
)

if [ -n "${S3VECTORS_TOPIC_ARN}" ]; then
  aws sns publish \
    --topic-arn "${S3VECTORS_TOPIC_ARN}" \
    --message '{"action":"create"}' \
    --region "${REGION}" >/dev/null
  echo "==> S3 Vectors setup started asynchronously, check CloudWatch logs for progress"
else
  echo "WARNING: S3VectorsSetupTopicArn missing from stack outputs; skipped async S3 Vectors trigger" >&2
fi

echo "==> Deploy complete. Outputs written to outputs.json"
