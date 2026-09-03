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

    return Translator(None, Parser()).translate(
        sam_template=template,
        parameter_values={"NamePrefix": template["Parameters"]["NamePrefix"]["Default"]},
    )


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
    [("query_diagnostics", "get"), ("chat", "post")],
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
    """module -> the modules it imports directly, common.* and sibling handlers.

    Sibling handlers count because they are all packaged from one CodeUri, so
    `import query_diagnostics` really does put that module's dependencies in the
    importing function's runtime. fn-chat reuses the readers in
    fn-query-diagnostics rather than reimplementing them, which means it reaches
    common.tenant_scope transitively and needs the same sts grant -- a graph
    that only followed common.* imports would have declared it exempt and let
    the deploy ship a function that fails on its first tool call.
    """
    handlers = {path.stem for path in HANDLERS.glob("*.py")}
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
            elif isinstance(node, ast.Import):
                # `import query_diagnostics as reads`
                imported.update(a.name for a in node.names if a.name in handlers)
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


#: The two tenant-scoped roles a function may assume. Both pin DynamoDB access
#: to dynamodb:LeadingKeys = the session's tenant_id tag and S3 access to that
#: tenant's object prefix; TenantScopedReadRole additionally has no write.
TENANT_SCOPED_ROLES = ("TenantScopedRole", "TenantScopedReadRole")


def _assumable_roles(stack, role):
    """tenant-scoped role logical id -> the sts actions this function holds on it."""
    held = {}
    for statement in _role_statements(stack, role):
        resource = json.dumps(statement.get("Resource"))
        for candidate in TENANT_SCOPED_ROLES:
            # Exact-ish: "TenantScopedRole" is a substring of nothing else here,
            # but check the longer name first so it is not mistaken for the other.
            if f'"{candidate}"' in resource or f"{candidate}.Arn" in resource:
                held.setdefault(candidate, set()).update(_actions(statement))
    return held


def test_every_function_that_can_reach_tenant_scope_may_assume_the_role(stack):
    """common.progress reaches tenant_scope, and it is imported transitively.

    fn-notify had sns:Publish only, so its progress.mark_stage call failed on
    every invocation. mark_stage swallows failures by design, so the last stage
    of the pipeline simply never appeared in the timeline and nothing said why.

    Either tenant-scoped role satisfies this: fn-chat assumes the read-only one,
    and which of the two a function is entitled to is a separate assertion
    below.
    """
    graph = _module_imports()
    missing = []
    for function, (module, role) in sorted(_lambda_functions(stack).items()):
        if module not in graph:
            continue
        if not _reaches(graph, module, "common.tenant_scope"):
            continue
        allowed = set()
        for actions in _assumable_roles(stack, role).values():
            allowed |= actions
        if not {"sts:AssumeRole", "sts:TagSession"} <= allowed:
            missing.append(f"{function} ({module}) has {sorted(allowed) or 'nothing'}")

    assert not missing, (
        "functions reaching common.tenant_scope without sts:AssumeRole + "
        "sts:TagSession on a tenant-scoped role: " + "; ".join(missing)
    )


def _function_env(stack, function):
    return (
        stack["Resources"][function]["Properties"]
        .get("Environment", {})
        .get("Variables", {})
    )


def test_each_function_may_assume_the_tenant_role_it_is_configured_to_use(stack):
    """TENANT_SCOPED_ROLE_ARN and the sts grant must name the SAME role.

    common.tenant_scope assumes whatever the env var names, so a function granted
    sts:AssumeRole on one role and pointed at the other fails with AccessDenied on
    its first data-plane call -- after a successful deploy, at runtime, looking
    like a broken feature rather than a mismatched pair of template edits.
    """
    graph = _module_imports()
    mismatched = []
    for function, (module, role) in sorted(_lambda_functions(stack).items()):
        if module not in graph or not _reaches(graph, module, "common.tenant_scope"):
            continue
        configured = json.dumps(_function_env(stack, function).get("TENANT_SCOPED_ROLE_ARN"))
        named = [r for r in TENANT_SCOPED_ROLES if f"{r}.Arn" in configured or f'"{r}"' in configured]
        assert named, f"{function} reaches tenant_scope with no TENANT_SCOPED_ROLE_ARN"
        held = _assumable_roles(stack, role)
        for candidate in named:
            if {"sts:AssumeRole", "sts:TagSession"} > held.get(candidate, set()):
                mismatched.append(
                    f"{function} is configured for {candidate} but may not assume it"
                )

    assert not mismatched, "; ".join(mismatched)


def test_the_read_only_tenant_role_can_only_read(stack):
    """fn-chat is the one function whose actions a language model chooses.

    Its tool list contains no mutation and tests/test_chat.py asserts that, but a
    tool list is application code. This role is what makes a mutating tool added
    by mistake fail with AccessDenied instead of succeeding, so it must stay free
    of writes.
    """
    statements = _role_statements(stack, "TenantScopedReadRole")
    assert statements, "TenantScopedReadRole has no policy"

    actions = set()
    for statement in statements:
        assert statement["Effect"] == "Allow"
        actions |= _actions(statement)

    forbidden = sorted(
        action
        for action in actions
        if any(
            word in action.lower()
            for word in ("put", "update", "delete", "create", "write", "batchwrite", "*")
        )
    )
    assert not forbidden, f"TenantScopedReadRole grants writes: {forbidden}"
    assert actions <= {"dynamodb:GetItem", "dynamodb:Query", "s3:GetObject"}, sorted(actions)


def test_the_read_only_role_keeps_the_same_tenant_isolation(stack):
    """Read-only is not the interesting property on its own -- a role that could
    read every tenant's rows would be worse than the read/write one it replaces.
    """
    statements = _role_statements(stack, "TenantScopedReadRole")

    dynamo = [s for s in statements if "dynamodb:Query" in _actions(s)]
    assert dynamo, "no DynamoDB statement"
    for statement in dynamo:
        condition = json.dumps(statement.get("Condition"))
        assert "dynamodb:LeadingKeys" in condition
        assert "aws:PrincipalTag/tenant_id" in condition

    s3 = [s for s in statements if "s3:GetObject" in _actions(s)]
    assert s3, "no S3 statement"
    for statement in s3:
        resource = json.dumps(statement.get("Resource"))
        assert "aws:PrincipalTag/tenant_id" in resource, (
            "read access to S3 must still be confined to the tenant's own prefix"
        )


def test_the_chat_function_is_the_one_using_the_read_only_role(stack):
    """Stated as a fact about the stack so that pointing another function at it,
    or moving fn-chat back to the read/write role, is a deliberate edit here."""
    users = sorted(
        function
        for function in _lambda_functions(stack)
        if "TenantScopedReadRole" in json.dumps(
            _function_env(stack, function).get("TENANT_SCOPED_ROLE_ARN")
        )
    )
    assert users == ["ChatFunction"], users


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


# The properties that carry a physical name we choose. Listed rather than matched
# on a "*Name" suffix, because StageName and the CloudFront origin's DomainName
# end the same way and must not be prefixed.
NAME_PROPERTIES = (
    "AlarmName",
    "BucketName",
    "ClientName",
    "FunctionName",
    "LayerName",
    "LogGroupName",
    "Name",
    "QueueName",
    "RoleName",
    "StateMachineName",
    "TableName",
    "TopicName",
    "UsagePlanName",
    "UserPoolName",
)


def _rendered(node, name_prefix):
    """A name property as a string, with NamePrefix substituted in."""
    if isinstance(node, str):
        return node
    if isinstance(node, dict) and "Fn::Sub" in node:
        template = node["Fn::Sub"]
        template = template[0] if isinstance(template, list) else template
        if isinstance(template, str):
            return template.replace("${NamePrefix}", name_prefix)
    return None


def _points_at_another_resource(node, logical_ids):
    """True for a Ref/GetAtt to something else in the stack, rather than a name."""
    if not isinstance(node, dict):
        return False
    if isinstance(node.get("Ref"), str):
        return node["Ref"] in logical_ids
    target = node.get("Fn::GetAtt")
    if isinstance(target, str):
        return target.split(".")[0] in logical_ids
    if isinstance(target, list) and target:
        return target[0] in logical_ids
    return False


def test_every_name_the_stack_chooses_carries_this_runs_prefix(stack):
    """A name that does not carry the prefix is one the platform cannot clean up.

    Every physical name in the stack must route through the NamePrefix
    parameter so it carries the fixed contract prefix.
    """
    name_prefix = _raw_template()["Parameters"]["NamePrefix"]["Default"]
    logical_ids = set(stack["Resources"])
    checked = 0
    wrong = []
    for logical, resource in sorted(stack["Resources"].items()):
        for prop in NAME_PROPERTIES:
            if prop not in resource.get("Properties", {}):
                continue
            value = resource["Properties"][prop]
            if _points_at_another_resource(value, logical_ids):
                # A Lambda::Permission's FunctionName identifies the function it
                # grants on; it does not name anything of its own.
                continue
            checked += 1
            rendered = _rendered(value, name_prefix)
            if rendered is None or not rendered.startswith(name_prefix + "-"):
                wrong.append(f"{logical}.{prop} = {value!r}")

    assert checked > 20, (
        f"only {checked} name properties found; this test is not looking at the "
        f"stack it thinks it is"
    )
    assert not wrong, (
        f"names not derived from NamePrefix ({name_prefix}): " + "; ".join(wrong)
    )


# ---------------------------------------------------------------------------
# The deploy permissions boundary
# ---------------------------------------------------------------------------
# These three exist because of one outage. fn-tenant-provision asked for
# secretsmanager:CreateSecret; the boundary every role here must carry
# (brd-architect-deploy-boundary) grants Secrets Manager READ only. The deploy
# succeeded, IAM refused the call at runtime, the PostConfirmation trigger threw,
# and every user was confirmed with no custom:tenant_id -- which the console
# renders as a dead end straight after a successful sign-in. Nothing in the suite
# could see it, because a boundary is invisible to a mocked boto3 client.
SECRETS_MANAGER_WRITES = (
    "secretsmanager:CreateSecret",
    "secretsmanager:PutSecretValue",
    "secretsmanager:UpdateSecret",
    "secretsmanager:DeleteSecret",
    "secretsmanager:TagResource",
)


def _all_statements(stack):
    """(role logical id, statement) for every inline policy the stack creates."""
    for name, resource in stack["Resources"].items():
        if resource["Type"] != "AWS::IAM::Role":
            continue
        for policy in resource["Properties"].get("Policies", []):
            for statement in policy["PolicyDocument"]["Statement"]:
                yield name, statement


def _resource_strings(statement):
    """Every Resource on `statement`, with Fn::Sub reduced to its template."""
    resource = statement.get("Resource", [])
    if not isinstance(resource, list):
        resource = [resource]
    out = []
    for item in resource:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict) and "Fn::Sub" in item:
            value = item["Fn::Sub"]
            out.append(value[0] if isinstance(value, list) else value)
    return out


def test_no_role_asks_for_a_secrets_manager_write(stack):
    """A write the boundary forbids deploys cleanly and fails at runtime."""
    offenders = []
    for role, statement in _all_statements(stack):
        for action in _actions(statement) & set(SECRETS_MANAGER_WRITES):
            offenders.append(f"{role}: {action}")

    assert not offenders, (
        "the deploy boundary grants Secrets Manager read only, so these fail at "
        "runtime rather than at deploy time: " + "; ".join(sorted(offenders))
    )


def test_every_ssm_parameter_grant_stays_under_this_projects_path(stack):
    """The boundary allows parameter/app-* only. A grant outside it is denied."""
    offenders = []
    for role, statement in _all_statements(stack):
        if not any(a.startswith("ssm:") for a in _actions(statement)):
            continue
        for resource in _resource_strings(statement):
            if ":parameter/" not in resource:
                continue
            path = resource.split(":parameter/", 1)[1]
            if not path.startswith("${NamePrefix}/"):
                offenders.append(f"{role}: {resource}")

    assert not offenders, (
        "SSM grants outside parameter/${NamePrefix}/: " + "; ".join(sorted(offenders))
    )


# Calls that end in a parameter read. common.crypto and common.integrations wrap
# paramstore.read, so their own call to it is reached only through one of their
# public readers below -- which is why they are exempted from that pattern. Without
# the exemption every importer of common.crypto looks like a reader, including
# fn-tenant-provision, which imports it only for generate_dek and never reads.
PARAMETER_READERS = (
    "paramstore.read(",
    "crypto.get_fernet(",
    "crypto.encrypt_field(",
    "crypto.decrypt_field(",
    "integrations.creds(",
)
READ_WRAPPERS = ("common.crypto", "common.integrations")


def _reads_a_parameter(module):
    """Whether `module`'s own source performs a per-tenant parameter read."""
    if module.startswith("common."):
        path = COMMON / f"{module.split('.', 1)[1]}.py"
    else:
        path = HANDLERS / f"{module}.py"
    if not path.exists():
        return False
    source = path.read_text()
    patterns = [
        pattern
        for pattern in PARAMETER_READERS
        if not (module in READ_WRAPPERS and pattern == "paramstore.read(")
    ]
    return any(pattern in source for pattern in patterns)


def test_every_function_that_reads_tenant_key_material_may_read_its_parameters(stack):
    """A function that decrypts a field or reads an integration credential needs
    ssm:GetParameter, or it fails on first use -- the same runtime-only IAM
    failure as the outage above, just in a different handler.
    """
    graph = _module_imports()
    missing = []
    for function, (module, role) in sorted(_lambda_functions(stack).items()):
        if module not in graph:
            continue
        reachable = {module} | {m for m in graph if _reaches(graph, module, m)}
        if not any(_reads_a_parameter(m) for m in reachable):
            continue
        allowed = set()
        for statement in _role_statements(stack, role):
            if any(":parameter/" in r for r in _resource_strings(statement)):
                allowed |= _actions(statement)
        if "ssm:GetParameter" not in allowed:
            missing.append(f"{function} ({module})")

    assert not missing, (
        "functions that read per-tenant key material without ssm:GetParameter: "
        + ", ".join(missing)
    )


def test_the_provisioning_trigger_may_write_the_key_material_it_creates(stack):
    """The grant whose absence caused the outage, asserted directly."""
    allowed = set()
    for statement in _role_statements(stack, "TenantProvisionFunctionRole"):
        if any(":parameter/" in r for r in _resource_strings(statement)):
            allowed |= _actions(statement)
    assert "ssm:PutParameter" in allowed, (
        "fn-tenant-provision cannot create a tenant's DEK, so every signup "
        f"leaves the user without custom:tenant_id; it has {sorted(allowed)}"
    )


def test_the_authorizer_is_a_request_authorizer_that_receives_the_headers(stack):
    """src/handlers/authorizer.py reads event["headers"], which only a REQUEST
    authorizer is given. SAM defaults FunctionPayloadType to TOKEN, and the
    resulting authorizer passed {"authorizationToken": ...} with no headers at all
    -- so the handler saw no token, denied every authenticated request, and the
    console showed API Gateway's "not authorized to access this resource with an
    explicit deny" page on every call. Nothing in the handler's own tests could see
    it: they build the event themselves.

    SAM emits this inside the API's OpenAPI body rather than as its own resource,
    which is why it is read from securityDefinitions.
    """
    found = {}
    for name, resource in stack["Resources"].items():
        if resource["Type"] != "AWS::ApiGateway::RestApi":
            continue
        body = resource["Properties"].get("Body") or {}
        for scheme, definition in (body.get("securityDefinitions") or {}).items():
            spec = definition.get("x-amazon-apigateway-authorizer")
            if spec:
                found[f"{name}.{scheme}"] = spec

    assert found, "no Lambda authorizer is declared on any API"
    for name, spec in sorted(found.items()):
        assert spec.get("type") == "request", (
            f"{name} is a '{spec.get('type')}' authorizer; the handler reads "
            "event['headers'], which only a request authorizer provides"
        )
        assert spec.get("identitySource") == "method.request.header.Authorization", (
            f"{name} does not key off the Authorization header: {spec.get('identitySource')}"
        )
