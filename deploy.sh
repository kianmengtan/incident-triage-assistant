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

# ---------------------------------------------------------------------------
# Tests first. The workflow contract requires them to pass before a change is
# complete, so a deploy that skips them can ship a known-broken build.
# ---------------------------------------------------------------------------
echo "==> Running tests"
if [[ -x .venv/bin/python ]]; then
  PYTHON=.venv/bin/python
else
  PYTHON=python3
fi

# tests/test_template_contract.py runs the real SAM transform to assert the
# handlers against the stack CloudFormation will actually receive -- the check
# that catches an unwired route or a missing IAM grant, neither of which any unit
# test can see. Best-effort: without aws-sam-translator that one file skips
# rather than failing the deploy, so a sandbox with no package index is not a
# hard stop.
"${PYTHON}" -m pip install -q -r tests/requirements-test.txt 2>/dev/null || \
  echo "    (could not install test requirements; the template contract test will skip)"

"${PYTHON}" -m pytest -q
node --test frontend/test/*.test.mjs

# The prototype has to stay a single self-contained file, so the logic inside it
# is a generated copy. Publishing a drifted one would deploy logic no test covers.
node frontend/sync-lib.mjs --check

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

echo "==> Deploy complete. Console: $(${PYTHON} -c 'import json;print(json.load(open("outputs.json"))["app_url"])')"
