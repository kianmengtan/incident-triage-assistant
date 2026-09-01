"""The entry-point scripts must not dereference a variable nothing sets.

Both scripts run under ``set -euo pipefail``. Under ``-u`` a single unset
variable aborts the script at that line, and because ``sam deploy`` runs before
any of them, the abort lands *after* CloudFormation has already succeeded. The
stack is then complete and serving, while everything the script does afterwards
-- publishing the console page, writing outputs.json -- never happens. That is
not a hypothetical: ``${PYTHON}`` was expanded four times in deploy.sh and
assigned nowhere, so every deploy died right after collecting the stack outputs,
leaving an empty console bucket behind a live CloudFront distribution. Requests
came back as an opaque S3 403 (AccessDenied, not 404, because the bucket policy
grants CloudFront s3:GetObject with no s3:ListBucket), which reads as a broken
bucket policy rather than a script that stopped early.

Text assertions on purpose: they need no AWS access and no third-party packages.
"""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ("deploy.sh", "destroy.sh")

# Set by the shell itself or by the environment the platform provides, so an
# expansion with no assignment in the file is legitimate for these.
EXTERNAL = {
    "BASH_SOURCE", "PWD", "HOME", "PATH", "IFS", "RANDOM", "LINENO",
    "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE", "CI",
}


def _body(name):
    return (ROOT / name).read_text()


def _assigned(script):
    """Names the script assigns: plain, exported, or as a loop variable."""
    names = set(re.findall(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=", script, re.MULTILINE))
    names |= set(re.findall(r"^\s*for\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b", script, re.MULTILINE))
    names |= set(re.findall(r"local\s+([A-Za-z_][A-Za-z0-9_]*)=", script))
    return names


def _dereferenced(script):
    """``${VAR}`` expansions that carry no ``:-`` / ``:=`` default of their own.

    An expansion written ``${VAR:-fallback}`` is safe under ``-u`` even when
    nothing assigns VAR, so it is not a finding.
    """
    found = set()
    for match in re.finditer(r"\$\{([A-Za-z_][A-Za-z0-9_]*)([^}]*)\}", script):
        name, rest = match.group(1), match.group(2)
        if rest.startswith((":-", ":=", ":?", "-", "+", ":+")):
            continue
        found.add(name)
    return found


@pytest.mark.parametrize("name", SCRIPTS)
def test_script_sets_every_variable_it_expands(name):
    script = _body(name)
    assert "set -euo pipefail" in script, (
        f"{name} must run under `set -euo pipefail`; this test's premise is that -u is on"
    )
    unset = sorted(_dereferenced(script) - _assigned(script) - EXTERNAL)
    assert not unset, (
        f"{name} expands these with nothing assigning them, so `set -u` aborts the "
        f"script mid-deploy (after sam deploy has already succeeded): {unset}"
    )


def test_deploy_defines_the_python_it_pipes_stack_outputs_through():
    """The specific regression: four `${PYTHON}` uses, no assignment."""
    script = _body("deploy.sh")
    assert "${PYTHON}" in script, "deploy.sh no longer pipes through ${PYTHON}; retarget this test"
    assert re.search(r'^PYTHON="?\$\{PYTHON:-', script, re.MULTILINE), (
        "deploy.sh must default PYTHON (e.g. PYTHON=\"${PYTHON:-python3}\") before "
        "expanding it, or every deploy dies at the first stack-output parse"
    )
