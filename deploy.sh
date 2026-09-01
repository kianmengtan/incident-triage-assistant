#!/usr/bin/env bash
#
# Builds, tests and deploys the incident triage assistant, then writes
# outputs.json at the repo root with app_url pointing at the deployed console.
set -euo pipefail

# Fixed prefix for every name in this deploy, per the platform contract.
# Passed to CloudFormation as NamePrefix, and to the functions as NAME_PREFIX,
# so the names the handlers build at runtime match the IAM policies too.
PREFIX="${PREFIX:-app-b9dac5ac-bc8fbf47}"
REGION="ap-southeast-1"
STACK_NAME="${STACK_NAME:-${PREFIX}-triage}"
ARTIFACTS_BUCKET="${TEMPLATE_BUCKET:-${PREFIX}-artifacts}"

# Parses the stack outputs below. This is expanded four times and used to be
# assigned nowhere: under `set -u` that aborted the script at the first use,
# which sits immediately AFTER sam deploy has already succeeded. The stack came
# out complete and serving while the console upload and outputs.json -- both
# later in this file -- never ran, so app_url pointed at an empty bucket and
# every request returned an opaque S3 403.
PYTHON="${PYTHON:-python3}"

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

# ---------------------------------------------------------------------------
# Tests first. This script used to run none, so a drifted inlined library or a
# broken handler deployed silently and was found in the browser afterwards.
# All three are fast and need no AWS credentials.
# ---------------------------------------------------------------------------
echo "==> Running backend tests"
if [[ -x .venv/bin/python ]]; then
  TEST_PYTHON=".venv/bin/python"
else
  TEST_PYTHON="${PYTHON}"
fi
# A failing test blocks the deploy; a missing test runner does not. pytest is a
# development dependency (tests/requirements-test.txt) and the build environment
# is not guaranteed to have it -- refusing to deploy over that would turn an
# absent dev tool into an outage.
if "${TEST_PYTHON}" -c 'import pytest' 2>/dev/null; then
  "${TEST_PYTHON}" -m pytest -q
else
  echo "    WARNING: pytest is not installed for ${TEST_PYTHON}; skipping backend tests" >&2
fi

echo "==> Running front-end tests"
( cd frontend && npm test )

# The pages carry an inlined copy of lib/triage.mjs. Deploying a stale one would
# ship a console whose logic differs from the code the tests just checked.
echo "==> Checking the inlined front-end library is in sync"
( cd frontend && npm run --silent check-sync )

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
  --parameter-overrides "NamePrefix=${PREFIX}" \
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

# The console is not optional: app_url IS the CloudFront domain in front of this
# bucket, so a deploy that cannot publish the page has not succeeded. This block
# used to be wrapped in `if [[ -n "${CONSOLE_BUCKET}" ]]`, which SKIPPED the whole
# publish whenever the output could not be read -- the deploy then exited 0 having
# shipped an empty bucket, and every request to app_url came back as an opaque S3
# 403 (AccessDenied rather than 404, because the OAC grant is s3:GetObject with no
# s3:ListBucket). Missing outputs are now a hard failure.
if [[ -z "${CONSOLE_BUCKET}" ]]; then
  echo "ERROR: ConsoleBucketName is missing from the ${STACK_NAME} stack outputs." >&2
  echo "       The console cannot be published, and app_url would serve a 403." >&2
  exit 1
fi
if [[ -z "${DISTRIBUTION_ID}" ]]; then
  echo "ERROR: ConsoleDistributionId is missing from the ${STACK_NAME} stack outputs." >&2
  echo "       Refusing to publish a console whose cache cannot be invalidated." >&2
  exit 1
fi
if [[ ! -f frontend/app.html ]]; then
  echo "ERROR: frontend/app.html is missing; there is no console page to publish." >&2
  exit 1
fi

echo "==> Publishing console to s3://${CONSOLE_BUCKET}"
# app.html is the live console. prototype.html stays in the repo as the offline
# design reference design/frontend-design.md points at, and is not deployed.
# Only the page itself is uploaded: lib/ and the sync script are build inputs, and
# the page already carries an inlined copy of the library.
aws s3 cp frontend/app.html "s3://${CONSOLE_BUCKET}/index.html" \
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

# Read the object back. An upload that failed without a non-zero exit would
# otherwise pass for a successful publish, which is the exact failure this
# section is here to prevent.
echo "==> Verifying the console object is readable"
aws s3api head-object \
  --bucket "${CONSOLE_BUCKET}" \
  --key index.html \
  --region "${REGION}" >/dev/null

echo "==> Invalidating CloudFront cache"
aws cloudfront create-invalidation \
  --distribution-id "${DISTRIBUTION_ID}" \
  --paths "/*" \
  --query 'Invalidation.Id' --output text >/dev/null

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
