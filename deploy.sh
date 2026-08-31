#!/usr/bin/env bash
set -euo pipefail

PREFIX="app-b9dac5ac-bc8fbf47"
REGION="ap-southeast-1"
STACK_NAME="${STACK_NAME:-${PREFIX}-triage}"
ARTIFACTS_BUCKET="${TEMPLATE_BUCKET:-${PREFIX}-artifacts}"

cd "$(dirname "${BASH_SOURCE[0]}")"

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

echo "==> Deploy complete. Outputs written to outputs.json"
