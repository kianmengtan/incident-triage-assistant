"""Contract tests between template.yaml and the handler code.

Every P1 bug found in the first review of this repository was invisible to the
unit tests, because each one mocks boto3 and never sees the deployed topology: a
handler can serve a route API Gateway does not expose, or call a table index its
role cannot query, and every test still passes.

So these tests run the real SAM transform and assert the generated stack against
what the handlers actually do:

* the transformed template has no circular dependencies (one shipped, and it
  blocked `sam deploy` outright);
* every route a handler dispatches on exists on an API;
* every function whose code can reach common.tenant_scope may assume the
  tenant-scoped role;
* every DynamoDB index the handlers query is covered by an IAM grant;
* every Lambda the state machine names can be invoked by it.
"""
import ast
import json
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HANDLERS = ROOT / "src" / "handlers"
COMMON = ROOT / "src" / "layer" / "python" / "common"

samtranslator = pytest.importorskip(
    "samtranslator",
    reason=(
        "aws-sam-translator is required to validate template.yaml against the "
        "handlers; install tests/requirements-test.txt"
    ),
)


# ---------------------------------------------------------------------------
# The transformed stack
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def stack():
    """template.yaml as CloudFormation sees it, after the SAM transform.

    cfn-lint alone does not do this unless aws-sam-translator is installed, so a
    clean lint says nothing about the roles, API bodies or dependency graph SAM
    generates -- which is where the real defects were.
    """
    import cfnlint.decode.cfn_yaml
    from samtranslator.parser.parser import Parser
    from samtranslator.translator.translator import Translator

    loaded = cfnlint.decode.cfn_yaml.load(str(ROOT / "template.yaml"))
    template = loaded[0] if isinstance(loaded, tuple) else loaded

    # sam build replaces these with S3 URIs; the transform rejects local paths.
    for resource in template["Resources"].values():
        properties = resource.get("Properties", {})
        for key in ("CodeUri", "DefinitionUri", "ContentUri"):
            if key in properties:
                properties[key] = "s3://bucket/key"

    return Translator(None, Parser()).translate(sam_template=template, parameter_values={})


def _referenced_names(node, names, out):
    """Every logical id `node` refers to, through Ref, GetAtt or Sub."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "Ref" and isinstance(value, str):
                out.add(value)
            elif key == "Fn::GetAtt":
                if isinstance(value, str):
                    out.add(value.split(".")[0])
                elif isinstance(value, list) and value:
                    out.add(value[0])
            elif key == "Fn::Sub":
                template = value[0] if isinstance(value, list) else value
                if isinstance(template, str):
                    # ${!Literal} is an escaped literal, not a reference.
                    for match in re.findall(r"\$\{([^}!][^}]*)\}", template):
                        out.add(match.split(".")[0].strip())
                if isinstance(value, list) and len(value) > 1:
                    _referenced_names(value[1], names, out)
            else:
                _referenced_names(value, names, out)
    elif isinstance(node, list):
        for item in node:
            _referenced_names(item, names, out)
    return out


def test_the_transformed_stack_has_no_circular_dependencies(stack):
    """A cycle does not fail at build time, it fails the deploy.

    The one that shipped was UserPool -> TenantProvisionFunction ->
    TenantProvisionFunctionRole -> UserPool: the pool names the function as its
    PostConfirmation trigger, and the function's policy did a GetAtt on the pool,
    which SAM puts in a role the function depends on.
    """
    resources = stack["Resources"]
    names = set(resources)
    graph = {}
    for name, resource in resources.items():
        deps = _referenced_names(resource.get("Properties", {}), names, set())
        depends_on = resource.get("DependsOn")
        if isinstance(depends_on, str):
            deps.add(depends_on)
        elif isinstance(depends_on, list):
            deps.update(depends_on)
        graph[name] = {d for d in deps if d in names and d != name}

    cycles = []
    state = dict.fromkeys(graph, "unvisited")
    path = []

    def walk(node):
        state[node] = "open"
        path.append(node)
        for neighbour in sorted(graph[node]):
            if state[neighbour] == "open":
                cycles.append(path[path.index(neighbour):] + [neighbour])
            elif state[neighbour] == "unvisited":
                walk(neighbour)
        path.pop()
        state[node] = "closed"

    for node in sorted(graph):
        if state[node] == "unvisited":
            walk(node)

    assert not cycles, "circular dependencies: " + "; ".join(
        " -> ".join(cycle) for cycle in cycles
    )


# ---------------------------------------------------------------------------
# Routes: what the handlers dispatch on vs what API Gateway exposes
# ---------------------------------------------------------------------------
def _api_routes(stack):
    routes = set()
    for resource in stack["Resources"].values():
        if resource["Type"] != "AWS::ApiGateway::RestApi":
            continue
        for path, methods in resource["Properties"].get("Body", {}).get("paths", {}).items():
            for method in methods:
                if method.lower() != "options":
                    routes.add((path, method.lower()))
    return routes


def _dispatched_resources(module):
    """The `event["resource"]` values a handler module compares against.

    Read out of the AST rather than by importing, so this stays a statement about
    the source and cannot be satisfied by a module-level constant.
    """
    tree = ast.parse((HANDLERS / f"{module}.py").read_text())
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name):
            if node.left.id != "resource":
                continue
            for comparator in node.comparators:
                if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                    found.add(comparator.value)
    return {r for r in found if r.startswith("/")}


@pytest.mark.parametrize(
    "module, method",
    [("query_diagnostics", "get")],
)
def test_every_route_a_handler_serves_is_exposed_by_an_api(stack, module, method):
    """Three routes used to be implemented, documented, and unreachable.

    fn-query-diagnostics served /v1/alerts, /v1/alerts/{alertId}/status and
    /v1/audit; none of them was wired, so the incident list, the pipeline stage
    timeline and the whole audit trail answered API Gateway's "Missing
    Authentication Token".
    """
    exposed = _api_routes(stack)
    missing = sorted(
        path for path in _dispatched_resources(module) if (path, method) not in exposed
    )
    assert not missing, f"{module} serves routes no API exposes: {missing}"


def test_the_approval_endpoints_the_remediation_handler_expects_exist(stack):
    exposed = _api_routes(stack)
    for path in ("/v1/runbooks/{runbookId}/approve", "/v1/runbooks/{runbookId}/decline"):
        assert (path, "post") in exposed, f"{path} is not exposed"


def test_the_integration_write_path_is_exposed(stack):
    """Without it a tenant's integration credentials stay the empty objects
    fn-tenant-provision creates, and both correlation stages, the IMS push and
    every remediation are permanently unconfigured."""
    exposed = _api_routes(stack)
    assert ("/v1/integrations", "get") in exposed
    assert ("/v1/integrations/{integration}", "put") in exposed
    assert ("/v1/integrations/{integration}", "delete") in exposed


def test_cors_allows_every_method_the_admin_api_actually_serves(stack):
    """The console is on a different origin, so the browser preflights. A method
    served but absent from AllowMethods is reachable by curl and not by the app."""
    served = {method.upper() for path, method in _api_routes(stack) if path.startswith("/v1/")}
    admin = next(
        r for name, r in stack["Resources"].items()
        if r["Type"] == "AWS::ApiGateway::RestApi" and "admin" in json.dumps(r).lower()
    )
    body = json.dumps(admin["Properties"]["Body"])
    # The ingestion API's POST is on the other API; check only what AdminApi holds.
    admin_methods = {
        m.upper()
        for p, ms in admin["Properties"]["Body"]["paths"].items()
        for m in ms
        if m.lower() != "options"
    }
    for method in sorted(admin_methods):
        assert method in body, f"{method} is served but never named in the API body"
    assert admin_methods <= served


# ---------------------------------------------------------------------------
# IAM: what the code needs vs what the generated roles allow
# ---------------------------------------------------------------------------
def _module_imports():
    """module -> the common.* modules it imports directly."""
    graph = {}
    for path in list(HANDLERS.glob("*.py")) + list(COMMON.glob("*.py")):
        key = path.stem if path.parent == HANDLERS else f"common.{path.stem}"
        imported = set()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                if node.module == "common":
                    imported.update(f"common.{a.name}" for a in node.names)
                elif node.module and node.module.startswith("common."):
                    imported.add(node.module)
                elif node.level == 1 and node.module is None:
                    # `from . import config, tenant_scope` inside common/
                    imported.update(f"common.{a.name}" for a in node.names)
        graph[key] = imported
    return graph


def _reaches(graph, start, target, seen=None):
    seen = seen or set()
    for dep in graph.get(start, ()):
        if dep == target:
            return True
        if dep not in seen:
            seen.add(dep)
            if _reaches(graph, dep, target, seen):
                return True
    return False


def _lambda_functions(stack):
    """logical id -> (handler module, generated role logical id)."""
    out = {}
    for name, resource in stack["Resources"].items():
        if resource["Type"] != "AWS::Lambda::Function":
            continue
        handler = resource["Properties"].get("Handler", "")
        out[name] = (handler.split(".")[0], f"{name}Role")
    return out


def _role_statements(stack, role_name):
    role = stack["Resources"].get(role_name)
    if not role:
        return []
    statements = []
    for policy in role["Properties"].get("Policies", []):
        statements.extend(policy["PolicyDocument"]["Statement"])
    return statements


def _actions(statement):
    action = statement.get("Action", [])
    return set(action if isinstance(action, list) else [action])


def test_every_function_that_can_reach_tenant_scope_may_assume_the_role(stack):
    """common.progress reaches tenant_scope, and it is imported transitively.

    fn-notify had sns:Publish only, so its progress.mark_stage call failed on
    every invocation. mark_stage swallows failures by design, so the last stage
    of the pipeline simply never appeared in the timeline and nothing said why.
    """
    graph = _module_imports()
    missing = []
    for function, (module, role) in sorted(_lambda_functions(stack).items()):
        if module not in graph:
            continue
        if not _reaches(graph, module, "common.tenant_scope"):
            continue
        allowed = set()
        for statement in _role_statements(stack, role):
            if "TenantScopedRole" in json.dumps(statement.get("Resource")):
                allowed |= _actions(statement)
        if not {"sts:AssumeRole", "sts:TagSession"} <= allowed:
            missing.append(f"{function} ({module}) has {sorted(allowed) or 'nothing'}")

    assert not missing, (
        "functions reaching common.tenant_scope without sts:AssumeRole + "
        "sts:TagSession on TenantScopedRole: " + "; ".join(missing)
    )


def _queried_indexes():
    """(table config attribute, index name) pairs the handlers query."""
    found = set()
    for path in HANDLERS.glob("*.py"):
        source = path.read_text()
        for index in re.findall(r'IndexName\s*=\s*"([^"]+)"', source):
            found.add(index)
    return found


def test_every_queried_index_is_covered_by_the_tenant_scoped_policy(stack):
    """A Query on a GSI needs the index ARN, not just the table's.

    The Alerts received-at-index was missing, so the incident list -- the one
    query that must use it, because the base sort key orders by alert id --
    failed with AccessDenied.
    """
    index_to_table = {
        "received-at-index": "AlertsTable",
        "status-index": "RunbooksTable",
    }
    granted = json.dumps(_role_statements(stack, "TenantScopedRole"))
    uncovered = []
    for index in sorted(_queried_indexes()):
        table = index_to_table.get(index)
        assert table, f"{index} is queried but this test does not know its table"
        if f"${{{table}.Arn}}/index/*" not in granted:
            uncovered.append(f"{index} on {table}")

    assert not uncovered, f"indexes queried with no IAM grant: {uncovered}"


def test_a_failed_execution_marks_the_alert_and_a_timed_out_one_does_too(stack):
    """An execution killed by the state machine's own TimeoutSeconds runs no Catch,
    so the in-machine failure path cannot record it. Something outside the
    execution has to, or the console shows "diagnosing" forever."""
    resources = stack["Resources"]
    machine = next(
        r for r in resources.values() if r["Type"] == "AWS::StepFunctions::StateMachine"
    )
    definition = json.dumps(machine["Properties"])
    assert "MarkPipelineFailedFunctionArn" in definition, (
        "the state machine's failure path does not mark the alert failed"
    )

    rules = [
        r["Properties"]
        for r in resources.values()
        if r["Type"] == "AWS::Events::Rule"
    ]
    timeout_rules = [
        r for r in rules
        if "TIMED_OUT" in json.dumps(r.get("EventPattern", {}))
    ]
    assert timeout_rules, "no EventBridge rule records a timed-out execution"
    pattern = json.dumps(timeout_rules[0]["EventPattern"])
    assert "aws.states" in pattern
    assert "ABORTED" in pattern, "a manually stopped execution leaves the same stuck alert"


def test_the_state_machine_can_invoke_every_function_it_names(stack):
    machine = next(
        r for r in stack["Resources"].values() if r["Type"] == "AWS::StepFunctions::StateMachine"
    )
    substitutions = machine["Properties"]["DefinitionSubstitutions"]
    named = {
        value["Fn::GetAtt"][0]
        for key, value in substitutions.items()
        if key.endswith("FunctionArn") and isinstance(value, dict) and "Fn::GetAtt" in value
    }
    invokable = json.dumps(_role_statements(stack, "StepFunctionsExecutionRole"))
    missing = sorted(f for f in named if f'"{f}"' not in invokable)
    assert not missing, f"state machine names functions its role cannot invoke: {missing}"


# ---------------------------------------------------------------------------
# Layer packaging: where the modules land at runtime
# ---------------------------------------------------------------------------
def _raw_template():
    """template.yaml before the transform: it rewrites ContentUri away."""
    import cfnlint.decode.cfn_yaml

    loaded = cfnlint.decode.cfn_yaml.load(str(ROOT / "template.yaml"))
    return loaded[0] if isinstance(loaded, tuple) else loaded


def _layer():
    template = _raw_template()
    for name, resource in template["Resources"].items():
        if resource["Type"] == "AWS::Serverless::LayerVersion":
            return name, resource
    raise AssertionError("no layer in the template")


def _layer_modules_the_handlers_import():
    """Top-level names the handlers expect the layer to put on sys.path."""
    wanted = set()
    for path in HANDLERS.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    wanted.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                wanted.add(node.module.split(".")[0])
    # Only the ones this repo vendors into the layer; stdlib and pip deps are not
    # this test's business.
    return wanted & {"cfnresponse", "common"}


def test_the_layer_puts_its_modules_where_the_runtime_looks_for_them():
    """The layer's ContentUri IS /opt/python once a BuildMethod is set.

    SAM adds the `python/` prefix itself when building a Python layer, so a
    ContentUri that itself contains a `python/` directory nests one inside the
    other and produces /opt/python/python/common/. Only /opt/python is on
    PYTHONPATH, so every function failed at import with "No module named 'common'"
    -- and the custom resource with "No module named 'cfnresponse'" -- after a
    build and a deploy that both reported success.

    Nothing else catches this. conftest.py puts src/layer/python on sys.path
    directly, so the unit tests import these modules whatever the template says,
    and the transform validates the template rather than the built artifact.
    """
    name, layer = _layer()
    build_method = (layer.get("Metadata") or {}).get("BuildMethod")
    content_uri = layer["Properties"]["ContentUri"]

    assert build_method, f"{name} has no BuildMethod, so nothing installs its dependencies"

    import_root = (ROOT / content_uri).resolve()
    assert import_root.is_dir(), f"{content_uri} is not a directory"

    nested = import_root / "python"
    assert not nested.is_dir(), (
        f"{content_uri} contains a python/ directory. With BuildMethod set, SAM "
        f"adds the python/ prefix itself, so this becomes /opt/python/python/ and "
        f"nothing in it is importable. Point ContentUri at {content_uri}python/ "
        f"instead."
    )

    for module in sorted(_layer_modules_the_handlers_import()):
        as_module = import_root / f"{module}.py"
        as_package = import_root / module / "__init__.py"
        assert as_module.is_file() or as_package.is_file(), (
            f"handlers import {module!r} but it is not directly inside {content_uri}, "
            f"so it will not be on /opt/python at runtime"
        )


def test_the_layers_pip_manifest_is_inside_its_content_uri():
    """SAM looks for the manifest inside ContentUri. Outside it, the build quietly
    installs no dependencies at all and every import of boto3 or cryptography fails
    in the deployed function."""
    name, layer = _layer()
    content_uri = layer["Properties"]["ContentUri"]
    manifest = (ROOT / content_uri / "requirements.txt").resolve()
    assert manifest.is_file(), (
        f"{content_uri} has no requirements.txt, so the layer would be built with "
        f"no dependencies"
    )


def test_the_conftest_path_matches_the_layers_real_import_root():
    """Otherwise the unit tests import from somewhere the runtime never sees, which
    is exactly how the nesting bug stayed invisible."""
    _, layer = _layer()
    import_root = (ROOT / layer["Properties"]["ContentUri"]).resolve()
    conftest = (ROOT / "tests" / "conftest.py").read_text()
    assert '"src", "layer", "python"' in conftest
    assert import_root == (ROOT / "src" / "layer" / "python").resolve(), (
        "conftest.py adds src/layer/python to sys.path; the layer's import root "
        f"is {import_root}. The tests and the runtime must agree."
    )


def test_every_role_the_stack_creates_carries_the_permissions_boundary(stack):
    """Required by the platform contract; role creation is denied without it."""
    without = [
        name
        for name, resource in stack["Resources"].items()
        if resource["Type"] == "AWS::IAM::Role"
        and "PermissionsBoundary" not in resource["Properties"]
    ]
    assert not without, f"roles with no PermissionsBoundary: {sorted(without)}"
