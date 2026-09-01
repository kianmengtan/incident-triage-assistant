"""The front end's copy of the rules must match the server's.

Two things are deliberately duplicated between Python and JavaScript, because the
browser needs them before it can make a request:

* the capability matrix (``common/rbac.py`` <-> ``frontend/lib/triage.mjs``), so a
  control the server will refuse renders disabled with the reason rather than
  looking available and failing on click;
* the consumer email-domain list (``common/tenancy.py`` <-> the same JS file), so
  the sign-up form can refuse an address before submitting it.

Duplication that drifts is worse than no duplication at all. If the matrices
disagree, the interface confidently offers an action the handler denies, or hides
one it would have allowed -- and nothing fails until a person hits it. These are
text assertions on purpose: they need no browser, no bundler and no AWS.
"""
import json
import pathlib
import re

from common import rbac, tenancy

ROOT = pathlib.Path(__file__).resolve().parents[1]
LIB = (ROOT / "frontend" / "lib" / "triage.mjs").read_text()


def _js_object(name):
    """Parse a `var name = { ... };` object literal out of the JS source.

    Only handles the shape actually used here -- keys of identifier form, values
    that are arrays of single-quoted strings -- and fails loudly rather than
    silently returning something partial.
    """
    match = re.search(rf"var {name} = \{{(.*?)\n\}};", LIB, re.DOTALL)
    assert match, f"{name} not found in frontend/lib/triage.mjs"
    body = match.group(1)

    parsed = {}
    for entry in re.finditer(r"(\w+)\s*:\s*\[([^\]]*)\]", body):
        key = entry.group(1)
        parsed[key] = tuple(re.findall(r"'([^']+)'", entry.group(2)))
    assert parsed, f"{name} parsed to nothing; the literal's shape must have changed"
    return parsed


def _js_array(name):
    match = re.search(rf"var {name} = \[(.*?)\];", LIB, re.DOTALL)
    assert match, f"{name} not found in frontend/lib/triage.mjs"
    values = re.findall(r"'([^']+)'", match.group(1))
    assert values, f"{name} parsed to nothing"
    return values


def test_the_capability_matrices_are_identical():
    js = _js_object("CAPABILITIES")
    py = {name: tuple(roles) for name, roles in rbac.CAPABILITIES.items()}

    assert set(js) == set(py), (
        "the two matrices list different capabilities:\n"
        f"  only in JS:     {sorted(set(js) - set(py))}\n"
        f"  only in Python: {sorted(set(py) - set(js))}"
    )

    mismatched = {
        name: {"js": js[name], "python": py[name]}
        for name in py
        if set(js[name]) != set(py[name])
    }
    assert not mismatched, (
        "these capabilities are granted to different roles on each side:\n"
        + json.dumps(mismatched, indent=2)
    )


def test_the_role_names_are_identical():
    js_labels = re.search(r"var ROLE_LABELS = \{(.*?)\n\};", LIB, re.DOTALL)
    assert js_labels, "ROLE_LABELS not found"
    js_roles = set(re.findall(r"(\w+)\s*:", js_labels.group(1)))
    assert js_roles == set(rbac.ROLES)


def test_the_consumer_email_domain_lists_are_identical():
    """fn-pre-signup refuses these; the form must refuse exactly the same set.

    A domain the form allowed but the trigger rejected would surface as an opaque
    Cognito error; one the form rejected but the trigger allowed would block a
    signup that should have worked.
    """
    js = set(_js_array("PUBLIC_EMAIL_DOMAINS"))
    assert js == set(tenancy.PUBLIC_EMAIL_DOMAINS), (
        f"only in JS: {sorted(js - set(tenancy.PUBLIC_EMAIL_DOMAINS))}, "
        f"only in Python: {sorted(set(tenancy.PUBLIC_EMAIL_DOMAINS) - js)}"
    )


def test_the_severity_vocabularies_agree():
    """The form offers severities the API must accept.

    ingest_normalize accepts a spread of vocabularies (sev1 / critical / p1 ...)
    and the console folds them onto four levels for display. Anything the JS can
    resolve must be something the API would take, or a severity that renders
    correctly is rejected on submit.
    """
    from common import alerts

    match = re.search(r"var SEVERITY_ALIASES = \{(.*?)\n\};", LIB, re.DOTALL)
    assert match, "SEVERITY_ALIASES not found"
    js_aliases = set(re.findall(r"(\w+)\s*:", match.group(1)))

    unknown_to_api = js_aliases - alerts.VALID_SEVERITIES
    assert not unknown_to_api, (
        f"the console resolves severities the API would reject: {sorted(unknown_to_api)}"
    )


def test_every_api_accepted_severity_can_be_displayed():
    """The other direction: an accepted severity that the UI cannot classify
    would render as "Unclassified" in the list and the overview."""
    from common import alerts

    match = re.search(r"var SEVERITY_ALIASES = \{(.*?)\n\};", LIB, re.DOTALL)
    js_aliases = set(re.findall(r"(\w+)\s*:", match.group(1)))

    undisplayable = alerts.VALID_SEVERITIES - js_aliases
    assert not undisplayable, (
        f"the API accepts severities the console cannot classify: {sorted(undisplayable)}"
    )


def test_the_capability_names_the_handlers_use_exist_in_the_matrix():
    """A handler asking for a capability the matrix does not define raises
    UnknownCapability at request time -- a 500 on a working feature."""
    handlers = (ROOT / "src" / "handlers").glob("*.py")
    used = set()
    for path in handlers:
        text = path.read_text()
        used.update(re.findall(r'rbac\.can\([^,]+,\s*"([a-z_]+)"\)', text))
        used.update(re.findall(r'CAPABILITY = "([a-z_]+)"', text))
        used.update(re.findall(r'_CAPABILITY = "([a-z_]+)"', text))

    assert used, "no capability usage found in the handlers; has the pattern changed?"
    unknown = used - set(rbac.CAPABILITIES)
    assert not unknown, f"handlers reference capabilities that do not exist: {sorted(unknown)}"
