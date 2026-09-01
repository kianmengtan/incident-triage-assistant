#!/usr/bin/env bash
#
# Builds, tests and deploys the incident triage assistant, then writes
# outputs.json at the repo root with app_url pointing at the deployed console.
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

# Verify the built layer, not just the template. A Python layer's modules have to
# sit at the root of the artifact, because that root is what becomes /opt/python
# -- the only directory of the layer on PYTHONPATH. This previously came out one
# level too deep (/opt/python/python/common), which build and deploy both reported
# as success and every function then failed at import.
echo "==> Verifying the built layer's import root"
LAYER_ROOT=".aws-sam/build/CommonLayer/python"
for module in cfnresponse.py common/config.py; do
  if [[ ! -e "${LAYER_ROOT}/${module}" ]]; then
    echo "!! ${LAYER_ROOT}/${module} is missing: the layer would deploy without it" >&2
    echo "!! built layer root contains:" >&2
    ls "${LAYER_ROOT}" >&2 || true
    exit 1
  fi
done
echo "    ok: cfnresponse and common/ are on the layer's import root"

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

# ---------------------------------------------------------------------------
# Publish the console. The stack creates the bucket and distribution; the page
# itself is a repo file, so it is uploaded here rather than baked into the
# template.
# ---------------------------------------------------------------------------
CONSOLE_BUCKET=$(printf '%s' "${OUTPUTS_JSON}" | "${PYTHON}" -c '
import json, sys
outputs = json.load(sys.stdin) or []
flat = {o["OutputKey"]: o["OutputValue"] for o in outputs}
print(flat.get("ConsoleBucketName", ""))
')
DISTRIBUTION_ID=$(printf '%s' "${OUTPUTS_JSON}" | "${PYTHON}" -c '
import json, sys
outputs = json.load(sys.stdin) or []
flat = {o["OutputKey"]: o["OutputValue"] for o in outputs}
print(flat.get("ConsoleDistributionId", ""))
')

if [[ -n "${CONSOLE_BUCKET}" ]]; then
  echo "==> Publishing console to s3://${CONSOLE_BUCKET}"
  # Keep lib/ and the sync script out of it: only the page is served.
  aws s3 cp frontend/prototype.html "s3://${CONSOLE_BUCKET}/index.html" \
    --content-type "text/html; charset=utf-8" \
    --cache-control "no-cache" \
    --region "${REGION}"

  # The deployed API and Cognito identifiers, for wiring the console to live
  # data. The page ships with mock data and reads no config today, so this is
  # published for the next step rather than consumed by it.
  # Written to a temp dir, never the repo root. The platform runs `git add -A`
  # after every change, so a deploy that died between writing this file and
  # removing it would commit the deployment's identifiers.
  CONFIG_DIR=$(mktemp -d)
  trap 'rm -rf "${CONFIG_DIR}"' EXIT
  printf '%s' "${OUTPUTS_JSON}" | "${PYTHON}" -c '
import json, sys
outputs = json.load(sys.stdin) or []
flat = {o["OutputKey"]: o["OutputValue"] for o in outputs}
json.dump(
    {
        "adminApiUrl": flat.get("AdminApiUrl", ""),
        "userPoolId": flat.get("UserPoolId", ""),
        "userPoolClientId": flat.get("UserPoolClientId", ""),
        "region": "ap-southeast-1",
    },
    open(sys.argv[1], "w"),
    indent=2,
)
' "${CONFIG_DIR}/config.json"
  aws s3 cp "${CONFIG_DIR}/config.json" "s3://${CONSOLE_BUCKET}/config.json" \
    --content-type "application/json" \
    --cache-control "no-cache" \
    --region "${REGION}"

  if [[ -n "${DISTRIBUTION_ID}" ]]; then
    echo "==> Invalidating CloudFront cache"
    aws cloudfront create-invalidation \
      --distribution-id "${DISTRIBUTION_ID}" \
      --paths "/*" \
      --query 'Invalidation.Id' --output text >/dev/null
  fi
fi

echo "==> Writing outputs.json"
printf '%s' "${OUTPUTS_JSON}" | "${PYTHON}" - <<'PY'
import json
import sys

# describe-stacks emits the literal null for a stack with no outputs, which
# json.loads turns into None rather than raising.
outputs = json.load(sys.stdin) or []
flat = {o["OutputKey"]: o["OutputValue"] for o in outputs}

# CloudFormation output names cannot contain an underscore, so the stack
# exports AppUrl and the contract's app_url key is derived from it here.
app_url = flat.get("AppUrl", "")
flat["app_url"] = app_url
if not app_url:
    # Exiting non-zero rather than writing an empty app_url: a deploy that
    # reports success with no reachable URL is worse than a failed one.
    sys.exit("app_url is missing from the stack outputs; refusing to write outputs.json")

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
