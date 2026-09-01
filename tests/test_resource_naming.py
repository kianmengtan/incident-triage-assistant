"""Every physical resource name is derived from one fixed prefix.

The platform contract fixes that prefix (``app-b9dac5ac-bc8fbf47``) and requires
every resource to start with it; resources named outside it are never cleaned
up by the platform. Keeping the prefix fixed (no per-run token) is what makes
deploy.sh idempotent to the same stack on every run, keeps destroy.sh able to
find exactly what deploy.sh created, and keeps derived names (which carry an
account id or extra suffix) under AWS's length limits.

These are text assertions on purpose. They hold with no third-party packages
installed, so the one thing that must not regress is checked even in an
environment where the SAM transform tests skip.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Fixed by CLAUDE.md. Every name must start with it; it is not ours to change.
CONTRACT_PREFIX = "app-b9dac5ac-bc8fbf47"

TEMPLATE = (ROOT / "template.yaml").read_text()
DEPLOY = (ROOT / "deploy.sh").read_text()
DESTROY = (ROOT / "destroy.sh").read_text()
CONFIG = (ROOT / "src" / "layer" / "python" / "common" / "config.py").read_text()


def _name_prefix_default():
    """The NamePrefix parameter's default, read out of the template text."""
    match = re.search(
        r"^Parameters:.*?^  NamePrefix:\n(?P<body>(?:    .*\n|\n)+)",
        TEMPLATE,
        re.MULTILINE | re.DOTALL,
    )
    assert match, "template.yaml declares no NamePrefix parameter"
    default = re.search(r"^    Default:\s*(\S+)\s*$", match.group("body"), re.MULTILINE)
    assert default, "the NamePrefix parameter has no Default"
    return default.group(1)


def test_the_name_prefix_is_exactly_the_contract_prefix():
    """No per-run token: the same prefix on every deploy keeps names fixed."""
    prefix = _name_prefix_default()
    assert prefix == CONTRACT_PREFIX, (
        f"NamePrefix is {prefix!r}; it must be exactly {CONTRACT_PREFIX!r} so "
        f"every deploy of this project lands on the same, predictable names"
    )


def test_no_resource_name_is_hardcoded_in_the_template():
    """The parameter default is the only place the contract prefix may appear.

    Anywhere else it is a name that bypasses the NamePrefix parameter.
    """
    # The Parameters block above Globals: declares the prefix; that is its job.
    # Everything from Globals: down is names, and none of them may spell it out.
    body_starts = TEMPLATE.index("\nGlobals:\n")
    first_line = TEMPLATE[:body_starts].count("\n") + 1
    offenders = [
        f"line {n}: {line.strip()}"
        for n, line in enumerate(TEMPLATE[body_starts:].splitlines(), first_line)
        if CONTRACT_PREFIX in line and not line.lstrip().startswith("#")
    ]
    assert not offenders, (
        "these names bypass the NamePrefix parameter:\n  " + "\n  ".join(offenders)
    )


def test_the_scripts_and_the_template_agree_on_the_prefix():
    """deploy.sh names the stack and destroy.sh has to find the same one."""
    prefix = _name_prefix_default()

    for name, script in (("deploy.sh", DEPLOY), ("destroy.sh", DESTROY)):
        assert re.search(rf'^PREFIX="\$\{{PREFIX:-{prefix}\}}"$', script, re.MULTILINE), (
            f"{name} does not default PREFIX to the fixed contract prefix "
            f"{prefix!r}, so it would act on a different set of resources than "
            f"the template creates"
        )
        assert re.search(r'^STACK_NAME="\$\{STACK_NAME:-\$\{PREFIX\}-triage\}"$', script, re.MULTILINE), (
            f"{name} does not build a fixed STACK_NAME off PREFIX, so deploys "
            f"would not be idempotent to the same stack"
        )

    assert "--parameter-overrides" in DEPLOY and "NamePrefix=" in DEPLOY, (
        "deploy.sh does not pass NamePrefix to sam deploy, so the stack would use "
        "the template default regardless of PREFIX"
    )


def test_the_runtime_prefix_matches_the_deployed_one():
    """Secret names are built in Python, and IAM only allows this prefix.

    common.config.PREFIX feeds every Secrets Manager name the handlers touch
    (``{PREFIX}-tenant-{id}-dek`` and friends), while the policies that authorise
    them are written as ``${NamePrefix}-tenant-*``. If the two disagree the
    deploy still succeeds and every tenant operation fails with AccessDenied.
    """
    assert 'os.environ.get("NAME_PREFIX"' in CONFIG, (
        "config.PREFIX is not read from the NAME_PREFIX environment variable, so "
        "it cannot follow the deployed prefix"
    )
    assert f'"{_name_prefix_default()}"' in CONFIG, (
        "config.PREFIX's fallback does not match the template's NamePrefix default"
    )
    assert "NAME_PREFIX: !Ref NamePrefix" in TEMPLATE, (
        "the template does not pass NAME_PREFIX to the functions, so they would "
        "fall back to the compiled-in default"
    )


def test_every_name_still_fits_the_tightest_aws_limit():
    """A longer prefix would silently push names past a limit AWS enforces.

    64 characters is the shortest cap among what this stack names (Lambda
    functions and IAM roles); S3 is 63 but its names carry an account id, which is
    accounted for here.
    """
    prefix = _name_prefix_default()
    too_long = []
    for suffix in re.findall(r"\$\{NamePrefix\}(-[A-Za-z0-9-]+)", TEMPLATE):
        # ${AWS::AccountId} follows some names; 12 digits replace it.
        name = prefix + suffix
        limit = 63 if "-context-cache" in suffix or "-console" in suffix else 64
        if len(name) + (12 if suffix.endswith("-") else 0) > limit:
            too_long.append(f"{name} ({len(name)} > {limit})")
    assert not too_long, "names over their AWS length limit: " + "; ".join(too_long)
