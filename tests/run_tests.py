#!/usr/bin/env python3
"""Smoke tests: run the CLI on the fixtures and check every rule fires."""
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin", "slopguard")
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
sys.path.insert(0, ROOT)

EXPECTED_RULES = {
    "unused-import", "mutable-default", "redundant-comment", "duplicate-function",
    "placeholder-body", "hedging-comment", "dead-code", "bare-except",
    "swallowed-exception", "write-only-attr", "unused-private",
    "single-method-class", "as-any", "debug-artifact", "duplicate-code",
}

EXPECTED_TEST_RULES = {
    "no-assert-test", "mock-only-test", "mock-echo-test", "tautological-assert",
    "conditional-assert", "brittle-exact-string", "overspecified-assert",
    "parametrize-candidate", "private-poke-test", "excessive-mocking",
    "sleep-in-test",
}

CLEAN_SOURCE = '''\
"""A small, clean module that should produce no findings."""
import math


def circle_area(radius):
    if radius < 0:
        raise ValueError("radius must be non-negative")
    return math.pi * radius ** 2


def classify(shapes):
    for shape in shapes:
        if shape.kind == "circle":
            yield circle_area(shape.radius)
        elif shape.kind == "square":
            yield shape.side ** 2
        elif shape.kind == "rect":
            yield shape.w * shape.h
        elif shape.kind == "tri":
            yield shape.base * shape.height / 2
        else:
            raise ValueError(shape.kind)
'''

failures = []


def check(name, condition, detail=""):
    status = "ok" if condition else "FAIL"
    print("[%s] %s %s" % (status, name, detail))
    if not condition:
        failures.append(name)


def run(args, stdin_data=None, env=None):
    return subprocess.run([sys.executable, BIN] + args, input=stdin_data,
                          capture_output=True, text=True, timeout=60, env=env)


def posttool_event(cwd, target):
    return json.dumps({
        "hook_event_name": "PostToolUse",
        "tool_name": "Write",
        "cwd": cwd,
        "tool_input": {"file_path": target},
    })


def test_scan_fixtures():
    r = run(["scan", FIXTURES, "--json", "--fail-on", "never"])
    check("scan exits 0 with --fail-on never", r.returncode == 0, r.stderr[:200])
    findings = json.loads(r.stdout)
    found = {f["rule"] for f in findings}
    missing = EXPECTED_RULES - found
    check("all expected rules fire", not missing, "missing: %s" % sorted(missing))
    check("unaliased dotted side-effect import is exempt", not any(
        f["rule"] == "unused-import" and "`integrations`" in f["message"]
        for f in findings))
    r2 = run(["scan", FIXTURES])
    check("scan exits 1 on findings by default", r2.returncode == 1)


def test_test_suite_rules():
    for fixture, label in (("test_slop_suite.py", "python"), ("slop_suite.test.ts", "typescript")):
        r = run(["scan", os.path.join(FIXTURES, fixture), "--json", "--fail-on", "never"])
        found = {f["rule"] for f in json.loads(r.stdout)}
        expected = EXPECTED_TEST_RULES if label == "python" else EXPECTED_TEST_RULES - {
            "mock-echo-test", "conditional-assert", "overspecified-assert", "private-poke-test",
            "excessive-mocking"}
        missing = expected - found
        check("all %s test-suite rules fire" % label, not missing, "missing: %s" % sorted(missing))


def test_clean_test_file():
    with tempfile.TemporaryDirectory() as td:
        clean = os.path.join(td, "test_clean.py")
        with open(clean, "w") as fh:
            fh.write('''\
"""A reasonable test file that should produce no test-slop findings."""
import pytest

from mymod import circle_area


@pytest.mark.parametrize("radius,expected", [(0, 0.0), (1, 3.141592653589793)])
def test_circle_area(radius, expected):
    assert circle_area(radius) == pytest.approx(expected)


def test_negative_radius_rejected():
    with pytest.raises(ValueError):
        circle_area(-1)


@pytest.mark.skip(reason="platform-specific coverage")
def test_skipped_placeholder():
    pass
''')
        r = run(["scan", clean, "--json"])
        found = json.loads(r.stdout)
        check("clean test file has no findings", r.returncode == 0 and not found,
              json.dumps(found)[:300])

# slopguard:ignore diverged-duplicate - shared scan harness is intentional
def test_clean_file():
    with tempfile.TemporaryDirectory() as td:
        clean = os.path.join(td, "clean.py")
        with open(clean, "w") as fh:
            fh.write(CLEAN_SOURCE)
        r = run(["scan", clean, "--json"])
        found = json.loads(r.stdout)
        check("clean file has no findings", r.returncode == 0 and not found,
              json.dumps(found)[:300])


def test_suppression():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "sup.py")
        with open(path, "w") as fh:
            fh.write("import os  # slopguard:ignore\n\n\ndef f():\n    return os.sep\n")
        r = run(["scan", path, "--json"])
        check("slopguard:ignore suppresses", r.returncode == 0 and json.loads(r.stdout) == [])

        scoped = os.path.join(td, "scoped.py")
        with open(scoped, "w") as fh:
            fh.write('''\
def right(x=[]):  # slopguard:ignore mutable-default — shared cache is the point
    return x


def wrong(y=[]):  # slopguard:ignore dead-code — names a rule that is not firing here
    return y


def reasoned(z=[]):  # slopguard:ignore — reason text only, suppresses everything
    return z
''')
        r = run(["scan", scoped, "--json", "--fail-on", "never"])
        left = {(f["rule"], f["line"]) for f in json.loads(r.stdout)}
        check("named-rule ignore suppresses that rule", ("mutable-default", 1) not in left,
              str(left))
        check("named-rule ignore does NOT suppress other rules", ("mutable-default", 5) in left,
              str(left))
        check("bare ignore with dash-reason still suppresses all",
              ("mutable-default", 9) not in left, str(left))

        string_path = os.path.join(td, "string_marker.py")
        with open(string_path, "w") as fh:
            fh.write('marker = "slopguard:ignore"\nimport os\n')
        r = run(["scan", string_path, "--json", "--fail-on", "never"])
        found = json.loads(r.stdout)
        check("Python string does not suppress", any(
            f["rule"] == "unused-import" for f in found), json.dumps(found)[:300])

        ts_path = os.path.join(td, "markers.ts")
        with open(ts_path, "w") as fh:
            fh.write('''\
const marker = "slopguard:ignore";
const blocked = value as any;
const allowed = other as any; // slopguard:ignore
''')
        r = run(["scan", ts_path, "--json", "--fail-on", "never"])
        found = [f for f in json.loads(r.stdout) if f["rule"] == "as-any"]
        check("generic suppression comes only from comments",
              len(found) == 1 and found[0]["line"] == 2, json.dumps(found)[:300])

        broken_path = os.path.join(td, "broken.py")
        with open(broken_path, "w") as fh:
            fh.write('""" slopguard:ignore\n')
        r = run(["scan", broken_path, "--json"])
        check("suppression falls back when Python tokenization fails",
              r.returncode == 0 and json.loads(r.stdout) == [])


def test_duplicate_function_bindings():
    with tempfile.TemporaryDirectory() as td:
        distinct = os.path.join(td, "distinct.py")
        with open(distinct, "w") as fh:
            fh.write('''\
def sync_user(user):
    checked = validate_user(user)
    persist_user(checked)
    audit_user(checked)
    return checked


def sync_order(order):
    checked = validate_order(order)
    persist_order(checked)
    audit_order(checked)
    return checked
''')
        r = run(["scan", distinct, "--json", "--fail-on", "never"])
        found = json.loads(r.stdout)
        check("duplicate functions preserve global callees", not any(
            f["rule"] == "duplicate-function" for f in found), json.dumps(found)[:300])

        copied = os.path.join(td, "copied.py")
        with open(copied, "w") as fh:
            fh.write('''\
def prepare_user(user):
    checked = validate_user(user)
    persist_user(checked)
    audit_user(checked)
    return checked


def prepare_account(account):
    verified = validate_user(account)
    persist_user(verified)
    audit_user(verified)
    return verified
''')
        r = run(["scan", copied, "--json", "--fail-on", "never"])
        found = json.loads(r.stdout)
        check("duplicate functions normalize renamed locals", any(
            f["rule"] == "duplicate-function" for f in found), json.dumps(found)[:300])


def test_install_command_quoting():
    from slopguard import install

    original_file = install.__file__
    original_which = install.shutil.which
    try:
        install.__file__ = os.path.join(
            os.sep, "tmp", "repo with space", "slopguard", "install.py")
        install.shutil.which = lambda _name: None
        command = install.hook_command("codex")
        install.shutil.which = lambda _name: "/opt/venv/bin/slopguard"
        pip_command = install.hook_command("codex")
    finally:
        install.__file__ = original_file
        install.shutil.which = original_which
    check("installed hook command quotes repository paths",
          shlex.split(command)[0] == "/tmp/repo with space/bin/slopguard",
          command)
    check("pip-installed console script is preferred when on PATH",
          shlex.split(pip_command)[0] == "/opt/venv/bin/slopguard", pip_command)
    block = install.CODEX_BLOCK.format(
        marker=install.MARKER, command=json.dumps(command))
    check("Codex installer TOML-escapes the hook command",
          "command = %s" % json.dumps(command) in block)


def test_atomic_installs():
    from slopguard import install

    with tempfile.TemporaryDirectory() as td:
        claude_path = os.path.join(td, "settings.json")
        codex_path = os.path.join(td, "config.toml")
        with open(claude_path, "w") as fh:
            fh.write('{"theme": "dark"}\n')
        with open(codex_path, "w") as fh:
            fh.write('model = "test"')
        os.chmod(claude_path, 0o640)
        os.chmod(codex_path, 0o604)

        original_claude = install.CLAUDE_SETTINGS
        original_codex = install.CODEX_CONFIG
        try:
            install.CLAUDE_SETTINGS = claude_path
            install.CODEX_CONFIG = codex_path
            claude_rc = install.install_claude()
            codex_rc = install.install_codex()
        finally:
            install.CLAUDE_SETTINGS = original_claude
            install.CODEX_CONFIG = original_codex

        with open(claude_path) as fh:
            settings = json.load(fh)
        with open(codex_path) as fh:
            codex_text = fh.read()
        check("Claude installer atomically preserves content and mode",
              claude_rc == 0 and settings["theme"] == "dark"
              and settings["hooks"]["PostToolUse"]
              and os.stat(claude_path).st_mode & 0o777 == 0o640)
        check("Codex installer atomically preserves content and mode",
              codex_rc == 0 and codex_text.startswith('model = "test"\n')
              and install.MARKER in codex_text
              and os.stat(codex_path).st_mode & 0o777 == 0o604)
        check("atomic installs clean temporary files", not any(
            name.startswith(".slopguard-") for name in os.listdir(td)))


def _write_contract_schema(directory):
    with open(os.path.join(directory, "status.proto"), "w") as fh:
        fh.write('syntax = "proto3";\nmessage S {\n  string parent_frame = 1;\n'
                 '  double joint_speed_limit = 2;\n  int32 error_code = 3;\n'
                 '  string tool_name = 4;\n'
                 '  string legacy_parent = 5 [json_name = "wireParent"];\n}\n')


def test_contract_rules():
    with tempfile.TemporaryDirectory() as td:
        _write_contract_schema(td)
        handler = os.path.join(td, "handler.py")
        with open(handler, "w") as fh:
            fh.write('''\
def respond(state):
    return {
        "parentFrame": state.frame,
        "joint_speed_limit": state.limit,
        "error_code": state.error,
        "tool_name": state.tool,
    }
''')
        r = run(["scan", td, "--json", "--fail-on", "never"])
        rules = {f["rule"]: f["severity"] for f in json.loads(r.stdout)}
        # parent_frame IS in this schema, so 'parentFrame' is in-sync mapping
        # (info); it is NOT a drift key.
        check("in-sync camel mapping is info-level contract-case-skew",
              rules.get("contract-case-skew") == "info", json.dumps(rules))
        check("hand-rolled-contract fires at warn", rules.get("hand-rolled-contract") == "warn")
        check("no drift key when the field exists", "contract-drift-key" not in rules)

        drifted = os.path.join(td, "drifted.py")
        with open(drifted, "w") as fh:
            fh.write('''\
def respond_old(state):
    return {
        "joint_speed_limit": state.limit,
        "error_code": state.error,
        "tool_name": state.tool,
        "removedField": state.gone,
    }
''')
        r = run(["scan", drifted, "--json", "--fail-on", "never"])
        rules = {f["rule"]: f["severity"] for f in json.loads(r.stdout)}
        check("emitting a field the schema dropped is warn-level contract-drift-key",
              rules.get("contract-drift-key") == "warn", json.dumps(rules))
        event = json.dumps({
            "hook_event_name": "PostToolUse", "tool_name": "Edit", "cwd": td,
            "tool_input": {"file_path": drifted},
        })
        r = run(["hook"], stdin_data=event)
        check("hook discovers sibling schema files",
              r.returncode == 2 and "contract-drift-key" in r.stderr,
              "rc=%d %s" % (r.returncode, r.stderr[:200]))


def test_contract_schema_formats():
    def scan_rules(path):
        r = run(["scan", path, "--json", "--fail-on", "never"])
        return {f["rule"]: f["severity"] for f in json.loads(r.stdout)}

    with tempfile.TemporaryDirectory() as td:
        # Message scoping: fields drawn from two unrelated messages must not
        # combine to legitimize a dict (the flat-vocabulary false positive).
        with open(os.path.join(td, "two.proto"), "w") as fh:
            fh.write('syntax = "proto3";\n'
                     'message A {\n  string alpha_rate = 1;\n  int32 beta_count = 2;\n}\n'
                     'message B {\n  string gamma_size = 1;\n  int32 delta_mode = 2;\n}\n')
        mixed = os.path.join(td, "mixed.py")
        with open(mixed, "w") as fh:
            fh.write('def emit(s):\n    return {\n        "alpha_rate": s.a,\n'
                     '        "beta_count": s.b,\n        "gamma_size": s.g,\n'
                     '        "strayKey": s.x,\n    }\n')
        rules = scan_rules(mixed)
        check("cross-message field mixing does not fire contract rules",
              not rules, json.dumps(rules))

    with tempfile.TemporaryDirectory() as td:
        with open(os.path.join(td, "events.graphql"), "w") as fh:
            fh.write('type PickEvent {\n  pickId: ID!\n  binLocation: String\n'
                     '  graspScore: Float\n  toolName: String\n}\n')
        gql = os.path.join(td, "emitter.py")
        with open(gql, "w") as fh:
            fh.write('def emit(e):\n    return {\n        "pickId": e.id,\n'
                     '        "binLocation": e.bin,\n        "graspScore": e.score,\n'
                     '        "toolName": e.tool,\n        "conveyorLane": e.lane,\n    }\n')
        rules = scan_rules(gql)
        check("GraphQL camel-declared fields match without case-skew",
              rules.get("hand-rolled-contract") == "warn"
              and "contract-case-skew" not in rules, json.dumps(rules))
        check("GraphQL drift key still fires", rules.get("contract-drift-key") == "warn")

    with tempfile.TemporaryDirectory() as td:
        with open(os.path.join(td, "openapi.yaml"), "w") as fh:
            fh.write('components:\n  schemas:\n    Robot:\n      type: object\n'
                     '      properties:\n        serial_number:\n          type: string\n'
                     '        firmware_rev:\n          type: string\n'
                     '        joint_count:\n          type: integer\n'
                     '        tool_type:\n          type: string\n')
        yml = os.path.join(td, "reporter.py")
        with open(yml, "w") as fh:
            fh.write('def report(r):\n    return {\n        "serial_number": r.sn,\n'
                     '        "firmware_rev": r.fw,\n        "joint_count": r.n,\n'
                     '        "tool_type": r.tool,\n    }\n')
        rules = scan_rules(yml)
        check("OpenAPI YAML properties are a schema source",
              rules.get("hand-rolled-contract") == "warn", json.dumps(rules))

    with tempfile.TemporaryDirectory() as td:
        # schema_roots config: schemas live outside the handler's subtree.
        os.makedirs(os.path.join(td, "schemas"))
        os.makedirs(os.path.join(td, "svc"))
        with open(os.path.join(td, "schemas", "status.proto"), "w") as fh:
            fh.write('syntax = "proto3";\nmessage S {\n  string parent_frame = 1;\n'
                     '  double joint_speed_limit = 2;\n  int32 error_code = 3;\n'
                     '  string tool_name = 4;\n}\n')
        with open(os.path.join(td, ".slopguard.json"), "w") as fh:
            fh.write('{"schema_roots": ["schemas"]}\n')
        handler = os.path.join(td, "svc", "handler.py")
        with open(handler, "w") as fh:
            fh.write('def respond_old(state):\n    return {\n'
                     '        "joint_speed_limit": state.limit,\n'
                     '        "error_code": state.error,\n'
                     '        "tool_name": state.tool,\n'
                     '        "removedField": state.gone,\n    }\n')
        event = json.dumps({
            "hook_event_name": "PostToolUse", "tool_name": "Edit", "cwd": td,
            "tool_input": {"file_path": handler},
        })
        r = run(["hook"], stdin_data=event)
        check("schema_roots config reaches schemas outside the edited subtree",
              r.returncode == 2 and "contract-drift-key" in r.stderr,
              "rc=%d %s" % (r.returncode, r.stderr[:200]))


def test_contract_block_parsers():
    from slopguard.contracts import schema_messages

    proto = '''\
message Outer {
  string outer_name = 1;
  int32 outer_count = 2;
  message Inner {
    string nested_name = 1;
    int32 nested_count = 2;
  }
  oneof result {
    string result_name = 3;
    int32 result_count = 4;
  }
}
'''
    messages = schema_messages({"nested.proto": proto})
    by_label = {label: fields for label, fields, _canon_fields in messages}
    check("nested proto fields stay out of their parent message",
          by_label["nested.proto Outer"]
          == {"outer_name", "outer_count", "result_name", "result_count"},
          repr(messages))
    check("nested proto messages are still parsed independently",
          by_label["nested.proto Inner"] == {"nested_name", "nested_count"},
          repr(messages))

    graphql = '''\
type Query {
  """A description may contain an unmatched } brace."""
  search(
    userId: ID!
    pageSize: Int
  ): Result
  requestName: String
  errorCode: Int
  toolName: String
}
'''
    messages = schema_messages({"query.graphql": graphql})
    fields = messages[0][1] if messages else set()
    check("GraphQL descriptions do not break brace matching",
          {"search", "requestName", "errorCode", "toolName"} <= fields,
          repr(messages))
    check("GraphQL arguments are not treated as message fields",
          not fields & {"userId", "pageSize"}, repr(messages))


def test_contract_canon_and_yaml():
    from slopguard.contracts import _canon, _yaml_properties, check_contracts

    yaml = '''\
components:
  schemas:
    Envelope:
      properties: # response fields
        user:
          type: object
          properties:
            "user_name":
              type: string
            user_role:
              type: string
        tags:
          type: array
          items:
            type: string
'''
    property_sets = [fields for _lineno, fields in _yaml_properties(yaml)]
    check("nested YAML properties become separate messages",
          {"user", "tags"} in property_sets
          and {"user_name", "user_role"} in property_sets,
          repr(property_sets))
    check("YAML property metadata and list items do not become fields",
          not any({"type", "items"} & fields for fields in property_sets),
          repr(property_sets))
    check("acronym canonicalization does not collide with letter-by-letter snake case",
          _canon("pickID") == "pick_id" and _canon("pickID") != _canon("pick_i_d"))

    messages = [("schema T", {"requestName"}, {"request_name"})]
    repeated = '''\
def emit(state):
    return {
        "requestName": state.name,
        "requestName": state.old_name,
        "requestName": state.fallback_name,
        "staleField": state.stale,
    }
'''
    check("duplicate dict keys do not inflate schema overlap",
          not check_contracts("handler.py", repeated, {}, messages))


def test_config_schema_files():
    import glob

    from slopguard.cli import config_schema_files, is_schema_file, load_config

    with tempfile.TemporaryDirectory() as td:
        special = os.path.join(td, "event[v1].avsc")
        second = os.path.join(td, "second.avsc")
        root = os.path.join(td, "root")
        os.mkdir(root)
        for path in (special, second, os.path.join(root, "third.proto")):
            with open(path, "w") as fh:
                fh.write("{}")

        cfg = {
            "_config_dir": td,
            "contract_schemas": [glob.escape(special), 3, second],
            "schema_roots": [root, None],
        }
        files = config_schema_files(cfg, max_files=2)
        check("config schema globs support absolute escaped paths",
              special in files, repr(files))
        check("configured schemas share one global file cap",
              len(files) == 2 and os.path.join(root, "third.proto") not in files,
              repr(files))

        with open(os.path.join(td, ".slopguard.json"), "w") as fh:
            fh.write("[]")
        check("non-object config fails open as empty config",
              isinstance(load_config(td), dict))

    check("common schema naming conventions are recognized",
          is_schema_file("service.graphqls")
          and is_schema_file("asyncapi.yaml")
          and not is_schema_file("payload.json"))


def test_contract_false_positive_guards():
    with tempfile.TemporaryDirectory() as td:
        _write_contract_schema(td)
        aliased = os.path.join(td, "aliased.py")
        with open(aliased, "w") as fh:
            fh.write('''\
def respond_wire(state):
    return {
        "joint_speed_limit": state.limit,
        "error_code": state.error,
        "tool_name": state.tool,
        "wireParent": state.frame,
    }
''')
        r = run(["scan", aliased, "--json", "--fail-on", "never"])
        rules = {f["rule"] for f in json.loads(r.stdout)}
        check("explicit proto json_name alias is not drift",
              "contract-drift-key" not in rules, json.dumps(sorted(rules)))

        uppercase = os.path.join(td, "uppercase.py")
        with open(uppercase, "w") as fh:
            fh.write('''\
def respond_metadata(state):
    return {
        "joint_speed_limit": state.limit,
        "error_code": state.error,
        "tool_name": state.tool,
        "LOCAL_MODE": state.mode,
    }
''')
        r = run(["scan", uppercase, "--json", "--fail-on", "never"])
        rules = {f["rule"] for f in json.loads(r.stdout)}
        check("non-camel metadata key is not drift",
              "contract-drift-key" not in rules, json.dumps(sorted(rules)))

        clean = os.path.join(td, "unrelated.py")
        with open(clean, "w") as fh:
            fh.write('''\
def parent_frame_of(node):
    note = "parentFrame"
    return node.parent_frame, note
''')
        r = run(["scan", clean, "--json"])
        check("contract names outside dict keys are ignored",
              r.returncode == 0 and not json.loads(r.stdout))


def test_schema_discovery_caps():
    from slopguard.cli import collect_schema_files

    with tempfile.TemporaryDirectory() as td:
        nested = os.path.join(td, "nested")
        os.mkdir(nested)
        _write_contract_schema(td)
        _write_contract_schema(nested)
        files = collect_schema_files([td], max_files=1)
        check("schema discovery stops at its file cap", len(files) == 1, repr(files))
        files = collect_schema_files([td], max_files=40, max_dirs=1)
        check("hook schema discovery stops at its directory cap",
              len(files) == 1 and os.path.dirname(files[0]) == td, repr(files))


def _property_examples(language):
    blocks = []
    for i in range(6):
        if language == "python":
            block = ("def test_case_%d():\n    result = normalize(%d)\n"
                     "    assert result == %d\n" % (i, i, i))
        else:
            block = ('it("case %d", () => {\n  const result = normalize(%d);\n'
                     '  expect(result).toBe(%d);\n});' % (i, i, i))
        blocks.append(block)
    return "\n\n".join(blocks)


def test_property_suggestion():
    with tempfile.TemporaryDirectory() as td:
        with open(os.path.join(td, ".slopguard.json"), "w") as fh:
            json.dump({"min_parametrize_group": 4}, fh)
        path = os.path.join(td, "test_examples.py")
        with open(path, "w") as fh:
            fh.write(_property_examples("python"))
        r = run(["scan", path, "--json", "--fail-on", "never"])
        messages = [
            f["message"] for f in json.loads(r.stdout)
            if f["rule"] == "parametrize-candidate"
        ]
        check("six Python clones suggest property-based testing",
              len(messages) == 1 and "hypothesis" in messages[0],
              json.dumps(messages))

        path = os.path.join(td, "examples.test.ts")
        with open(path, "w") as fh:
            fh.write(_property_examples("typescript"))
        r = run(["scan", path, "--json", "--fail-on", "never"])
        messages = [
            f["message"] for f in json.loads(r.stdout)
            if f["rule"] == "parametrize-candidate"
        ]
        check("six TypeScript clones suggest property-based testing",
              len(messages) == 1 and "fast-check" in messages[0],
              json.dumps(messages))


def test_diverged_duplicates():
    r = run(["scan", os.path.join(FIXTURES, "diverged_a.py"),
             os.path.join(FIXTURES, "diverged_b.py"), "--json", "--fail-on", "never"])
    found = [f for f in json.loads(r.stdout) if f["rule"] == "diverged-duplicate"]
    check("diverged copies are detected", len(found) == 1, json.dumps(found)[:300])
    check("diverged finding names both sides",
          found and "sync_inventory" in found[0]["message"]
          and "diverged_a.py" in found[0]["message"], found[0]["message"] if found else "")

    with tempfile.TemporaryDirectory() as td:
        # Identical-modulo-literals pairs belong to duplicate-function, not
        # diverged-duplicate (literals are normalized, so J would be ~1.0).
        for name, factor in (("m1.py", 10), ("m2.py", 99)):
            with open(os.path.join(td, name), "w") as fh:
                fh.write('''\
def scale_all(values, logger):
    scaled = []
    for value in values:
        if value is None:
            logger.warning("missing value")
            continue
        adjusted = value * %d + %d
        if adjusted > %d:
            adjusted = %d
        scaled.append(adjusted)
    logger.info("scaled %%d values", len(scaled))
    return scaled
''' % (factor, factor + 1, factor * 100, factor * 100))
        r = run(["scan", td, "--json", "--fail-on", "never"])
        found = json.loads(r.stdout)
        param = [f for f in found if f["rule"] == "diverged-duplicate"]
        check("literal-only variants get the parameterize message",
              len(param) == 1 and "parameterize" in param[0]["message"],
              json.dumps(param)[:300])
        check("literal-only variants are not structural duplicates",
              "duplicate-function" not in [f["rule"] for f in found])


def _near_duplicate_source(tag, literal, isolate_names=False):
    suffix = "_" + tag if isolate_names else ""
    return '''\
def sync_%s(client%s, values%s, logger%s):
    results%s = []
    failures%s = []
    for value%s in values%s:
        record%s = client%s.fetch_%s(value%s)
        if record%s is None:
            logger%s.warning("missing record")
            failures%s.append(value%s)
            continue
        normalized%s = client%s.normalize(record%s)
        if normalized%s.is_valid:
            results%s.append(normalized%s.payload)
        else:
            failures%s.append(value%s)
    threshold%s = %d
    if len(failures%s) > threshold%s:
        client%s.report_failures(failures%s)
    return results%s, failures%s
''' % (
        tag, suffix, suffix, suffix, suffix, suffix, suffix, suffix, suffix,
        suffix, tag if isolate_names else "record", suffix, suffix, suffix,
        suffix, suffix, suffix, suffix, suffix, suffix, suffix, suffix, suffix,
        suffix, suffix, literal, suffix, suffix, suffix, suffix, suffix, suffix)


def test_diverged_candidate_recall_and_reporting():
    from slopguard.duplicates import find_diverged_duplicates

    family = {
        "/tmp/family_%02d.py" % i: _near_duplicate_source("records", i)
        for i in range(21)
    }
    findings = find_diverged_duplicates(family)
    check("DF-capped shingles do not hide a 21-copy family",
          len(findings) == 1 and "21 functions" in findings[0].message,
          repr([f.message for f in findings[:2]]))

    independent = {}
    for i in range(21):
        tag = "case%d" % i
        independent["/tmp/a_%02d.py" % i] = _near_duplicate_source(
            tag, 1, isolate_names=True)
        independent["/tmp/b_%02d.py" % i] = _near_duplicate_source(
            tag, 2, isolate_names=True)
    findings = find_diverged_duplicates(independent)
    check("near-duplicate reporting does not silently stop at 20",
          len(findings) == 21, "count=%d" % len(findings))

    same_file = "".join(
        _near_duplicate_source("worker%d" % i, i) for i in range(6))
    findings = find_diverged_duplicates({"/tmp/workers.py": same_file})
    check("same-file pairs are not aggregated as a fork of their own file",
          not any("functions here" in f.message for f in findings),
          repr([f.message for f in findings]))

    target = "/tmp/a_target.py"
    context = "/tmp/z_context.py"
    findings = find_diverged_duplicates(
        {
            target: _near_duplicate_source("edited", 1),
            context: _near_duplicate_source("edited", 2),
        },
        report_files={target})
    check("near-duplicate findings stay attached to the edited hook target",
          len(findings) == 1 and findings[0].file == target,
          repr([(f.file, f.message) for f in findings]))


def test_diverged_token_and_decorator_guards():
    from slopguard.duplicates import (
        _function_entries, _token_shingles, find_diverged_duplicates,
    )

    first = '''\
def render(left):
    values = list(left)
    values.sort()
    cleaned = [value for value in values if value is not None]
    total = sum(cleaned)
    average = total / len(cleaned)
    return f"left={left}: average={average}"
'''
    second = first.replace("left={left}", "right={right}")
    check("f-string contents normalize consistently",
          _token_shingles(first) == _token_shingles(second))

    pipeline = "\n".join(
        "        step_%d = transform(step_%d)" % (i, i - 1)
        for i in range(1, 12))
    properties = '''\
class Status:
    @property
    def value(self):
        step_0 = self.raw
%s
        return step_11

    @value.setter
    def value(self, new_value):
        step_0 = self.raw
%s
        return step_11
''' % (pipeline, pipeline)
    entries = _function_entries({"properties.py": properties})
    check("function shingles include decorator roles",
          len(entries) == 2 and entries[0][6] != entries[1][6],
          repr([(entry[1], entry[6]) for entry in entries]))
    check("property getter/setter twins are not diverged copies",
          not find_diverged_duplicates({"properties.py": properties}))

    overloaded = '''\
@overload
def convert(value):
    stage_0 = normalize(value)
%s
    return stage_11
''' % pipeline.replace("        ", "    ")
    check("typing overload declarations stay out of near-duplicate candidates",
          not _function_entries({"overload.py": overloaded}))


def test_parallel_scan_determinism():
    with tempfile.TemporaryDirectory() as td:
        with open(os.path.join(td, ".slopguard.json"), "w") as fh:
            fh.write("{}")
        with open(os.path.join(td, "status.proto"), "w") as fh:
            fh.write("message S { string parent_frame = 1; }\n")
        for i in range(205):
            with open(os.path.join(td, "module_%03d.py" % i), "w") as fh:
                fh.write("import os\nvalue_%d = %d\n" % (i, i))

        first = run(["scan", td, "--json", "--fail-on", "never"])
        second = run(["scan", td, "--json", "--fail-on", "never"])
        findings = json.loads(first.stdout)
        check("parallel scan pickles config and schema message sets",
              first.returncode == 0 and len(findings) == 205,
              "rc=%d findings=%d" % (first.returncode, len(findings)))
        check("parallel scan output order is deterministic",
              first.stdout == second.stdout)


def test_fingerprint_identity():
    from slopguard.findings import Finding, add_fingerprints

    path = "/repo/mod.py"
    texts = {path: "import os\nimport os\n"}
    second_only = [Finding("unused-import", "warn", path, 2, "second")]
    add_fingerprints(second_only, texts, "/repo")
    both = [
        Finding("unused-import", "warn", path, 1, "first"),
        Finding("unused-import", "warn", path, 2, "second"),
    ]
    add_fingerprints(both, texts, "/repo")
    check("suppression cannot transfer an identical line's fingerprint",
          second_only[0].fingerprint == both[1].fingerprint
          and second_only[0].fingerprint != both[0].fingerprint)

    forward = [
        Finding("unused-import", "warn", path, 1, "os"),
        Finding("unused-import", "warn", path, 1, "sys"),
    ]
    reverse = list(reversed([
        Finding("unused-import", "warn", path, 1, "os"),
        Finding("unused-import", "warn", path, 1, "sys"),
    ]))
    add_fingerprints(forward, texts, "/repo")
    add_fingerprints(reverse, texts, "/repo")
    check("same-line fingerprint assignment is deterministic",
          {f.message: f.fingerprint for f in forward}
          == {f.message: f.fingerprint for f in reverse})

    renamed = [Finding("unused-import", "warn", "/repo/renamed.py", 2, "second")]
    add_fingerprints(renamed, {"/repo/renamed.py": texts[path]}, "/repo")
    check("file renames conservatively resurface findings",
          renamed[0].fingerprint != second_only[0].fingerprint)


def test_baseline_roots_and_partial_updates():
    with tempfile.TemporaryDirectory() as td:
        first_dir = os.path.join(td, "first")
        second_dir = os.path.join(td, "second")
        os.makedirs(first_dir)
        os.makedirs(second_dir)
        first = os.path.join(first_dir, "one.py")
        second = os.path.join(second_dir, "two.py")
        with open(first, "w") as fh:
            fh.write("import os\n")
        with open(second, "w") as fh:
            fh.write("import sys\n")
        subprocess.run(["git", "-C", td, "init", "-q"], check=True)

        r = run(["baseline", first_dir])
        baseline = os.path.join(td, ".slopguard-baseline.json")
        check("baseline path uses the Git root, not the scanned subdirectory",
              r.returncode == 0 and os.path.isfile(baseline)
              and not os.path.exists(os.path.join(first_dir, ".slopguard-baseline.json")),
              r.stdout[:200])
        event = posttool_event(td, first)
        r = run(["hook"], stdin_data=event)
        check("hook discovers a baseline written from a subdirectory scan",
              r.returncode == 0, "rc=%d %s" % (r.returncode, r.stderr[:200]))

        r = run(["baseline", td])
        os.chmod(baseline, 0o640)
        r = run(["baseline", first_dir, "--update"])
        with open(baseline) as fh:
            entries = json.load(fh)["fingerprints"]
        files = {entry["file"] for entry in entries.values()}
        check("partial baseline update preserves findings outside its path",
              r.returncode == 0
              and files == {"first/one.py", "second/two.py"}, repr(files))
        check("baseline updates preserve file mode",
              os.stat(baseline).st_mode & 0o777 == 0o640,
              oct(os.stat(baseline).st_mode & 0o777))

        with open(baseline, "w") as fh:
            fh.write("[]\n")
        r = run(["scan", td, "--json", "--fail-on", "never"])
        check("malformed baseline shape fails open",
              r.returncode == 0 and len(json.loads(r.stdout)) == 2,
              "rc=%d %s" % (r.returncode, r.stderr[:200]))


def test_baseline_ratchet():
    src_v1 = "import os\n\n\ndef f(x=[]):\n    return x\n"
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "mod.py")
        with open(path, "w") as fh:
            fh.write(src_v1)

        r = run(["baseline", td])
        check("baseline command writes the file",
              r.returncode == 0 and os.path.isfile(os.path.join(td, ".slopguard-baseline.json")),
              r.stdout[:200])

        r = run(["scan", td])
        check("baselined findings no longer fail the scan", r.returncode == 0,
              r.stdout[:200])
        r = run(["scan", td, "--no-baseline"])
        check("--no-baseline shows them again", r.returncode == 1)

        # A NEW finding must stay hot even with a baseline present — and it
        # must survive line drift: insert lines ABOVE the old findings.
        with open(path, "w") as fh:
            fh.write("import json\n\n\n" + src_v1 + "\n\ndef g():\n    try:\n"
                     "        return json.loads('{}')\n    except Exception:\n"
                     "        pass\n")
        r = run(["scan", td, "--json", "--fail-on", "never"])
        hot = {f["rule"] for f in json.loads(r.stdout)}
        check("new finding stays hot; drifted old ones stay baselined",
              "swallowed-exception" in hot and "mutable-default" not in hot
              and "unused-import" not in hot, str(hot))

        event = json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Edit",
                            "cwd": td, "tool_input": {"file_path": path}})
        r = run(["hook"], stdin_data=event)
        check("hook blocks only on the new finding",
              r.returncode == 2 and "swallowed-exception" in r.stderr
              and "mutable-default" not in r.stderr, r.stderr[:300])

        # Fix the new finding, then ratchet-shrink: the baseline may only lose
        # entries, and fixed grandfathered findings can't come back.
        with open(path, "w") as fh:
            fh.write("def f(x=[]):\n    return x\n")  # unused-import gone, mutable stays
        r = run(["baseline", td, "--update"])
        check("baseline --update shrinks", r.returncode == 0 and "remain" in r.stdout,
              r.stdout[:200])
        with open(path, "w") as fh:
            fh.write("import os\n\n\ndef f(x=[]):\n    return x\n")  # reintroduce
        r = run(["scan", td, "--json", "--fail-on", "never"])
        hot = {f["rule"] for f in json.loads(r.stdout)}
        check("a fixed-then-reintroduced finding is hot again (ratchet)",
              "unused-import" in hot and "mutable-default" not in hot, str(hot))


def test_precision_fixes():
    """Regressions for the harness-driven FP-class fixes (unanimous labels)."""
    with tempfile.TemporaryDirectory() as td:
        def scan_rules(name, content):
            p = os.path.join(td, name)
            with open(p, "w") as fh:
                fh.write(content)
            r = run(["scan", p, "--json", "--fail-on", "never"])
            if not r.stdout.strip():
                return set()  # file excluded entirely (e.g. generated marker)
            return {(f["rule"], f["severity"]) for f in json.loads(r.stdout)}

        gen = scan_rules("client_api.py",
                         '"""@generated by protoc-gen-x — DO NOT EDIT."""\nimport os\n')
        check("generated-marker files are skipped entirely", not gen, str(gen))

        noop = scan_rules("sinks.py", '''\
class NoopMetrics:
    def on_published(self, route_id, error_type):
        pass

    def on_error(self, route_id, error_type):
        pass


class _Resource:
    def read(self):
        return b""

    def __exit__(self, *exc):
        pass
''')
        check("null-object classes and dunders exempt from placeholder-body",
              ("placeholder-body", "warn") not in noop, str(noop))

        imp = scan_rules("sideeffects.py", '''\
from google.longrunning import (
    operations_pb2,  # noqa: F401 — registers the service in the default pool
)
from google.api import annotations_pb2 as _

import shutil


def f():
    return 1
''')
        check("noqa'd and underscore-aliased imports exempt; real unused still fires",
              not any(r == "unused-import" and "operations_pb2" in str(imp) for r, _s in imp)
              and ("unused-import", "warn") in imp, str(imp))

        fix = scan_rules("conftest_like.py", '''\
import pytest


@pytest.fixture(autouse=True)
def _event_loop():
    yield


def _dead_helper():
    return 3
''')
        check("pytest fixtures exempt from unused-private; dead helper still fires",
              ("unused-private", "warn") in fix and len(
                  [1 for r, s in fix if r == "unused-private"]) == 1, str(fix))

        ban = scan_rules("sections.py", '''\
# ── Board PDF ───────────────────────────────────────────── section
def board_pdf(x):
    return x
''')
        check("banner comments exempt from redundant-comment",
              not any(r == "redundant-comment" for r, _s in ban), str(ban))

        newer = scan_rules("newer.py", '''\
def f(x):
    match x:
        case 0:
            return "zero"
        case _:
            return "other"
''')
        if sys.version_info < (3, 10):
            check("match-statement files downgrade syntax-error to info",
                  ("syntax-error", "info") in newer and ("syntax-error", "error") not in newer,
                  str(newer))
        else:
            check("match-statement files parse fine on 3.10+", True)

        manual = scan_rules("test_rig_manual.py", '''\
import sys


def test_motion(arm):
    print("moving")
    return True


if __name__ == "__main__":
    sys.exit(0 if test_motion(None) else 1)
''')
        check("manual __main__ harnesses skip test-suite rules",
              not any(r == "no-assert-test" for r, _s in manual), str(manual))

        helper = scan_rules("test_helpers_suite.py", '''\
def _play_refused(state, reason):
    assert state.refusals[-1] == reason


def test_refused_on_slew(replay_state):
    _play_refused(replay_state, "EXCEEDS_SLEW")
''')
        check("local assert-helpers count as assertions",
              not any(r == "no-assert-test" for r, _s in helper), str(helper))

    with tempfile.TemporaryDirectory() as td:
        with open(os.path.join(td, "s.proto"), "w") as fh:
            fh.write('syntax = "proto3";\nmessage E {\n  string id = 1;\n  int32 support = 2;\n'
                     '  Aabb aabb = 3;\n  int32 obs_count = 4;\n  string derived_label = 5;\n'
                     '  bytes centroid = 6;\n  int64 first_seen_unix_ns = 7;\n'
                     '  int64 last_seen_unix_ns = 8;\n  repeated Part parts = 9;\n'
                     '  string kind = 10;\n}\n')
        flat = os.path.join(td, "view.py")
        with open(flat, "w") as fh:
            fh.write('def view(e):\n    return {\n        "id": e.id,\n        "support": e.support,\n'
                     '        "obsCount": e.obs_count,\n        "derivedLabel": e.derived_label,\n'
                     '        "centroid": e.centroid,\n        "firstSeenUnixNs": e.first_seen_unix_ns,\n'
                     '        "lastSeenUnixNs": e.last_seen_unix_ns,\n        "parts": [],\n'
                     '        "kind": e.kind,\n        "aabbMin": e.aabb.min,\n'
                     '        "aabbMax": e.aabb.max,\n        "vanishedField": e.gone,\n    }\n')
        r = run(["scan", flat, "--json", "--fail-on", "never"])
        drift = [f["message"] for f in json.loads(r.stdout) if f["rule"] == "contract-drift-key"]
        check("nested-field flattening (aabbMin) is not drift; real drift key still is",
              len(drift) == 1 and "vanishedField" in drift[0], str(drift))


def test_rule_coverage():
    """Self-check: every rule slopguard documents must demonstrably fire."""
    from slopguard.cli import RULES

    fired = set()
    r = run(["scan", FIXTURES, "--json", "--fail-on", "never"])
    fired |= {f["rule"] for f in json.loads(r.stdout)}
    with tempfile.TemporaryDirectory() as td:
        big = os.path.join(td, "big.py")
        body = ["def huge(flag):"] + ["    x%d = %d" % (i, i) for i in range(85)]
        body += ["    if flag:", "        if x1:", "            if x2:",
                 "                if x3:", "                    if x4:",
                 "                        return x5", "    return x0", ""]
        with open(big, "w") as fh:
            fh.write("\n".join(body))
        r = run(["scan", big, "--json", "--fail-on", "never"])
        fired |= {f["rule"] for f in json.loads(r.stdout)}
    missing = set(RULES) - fired
    check("every documented rule fires somewhere in the test corpus", not missing,
          "missing: %s" % sorted(missing))


def test_false_positive_guards():
    with tempfile.TemporaryDirectory() as td:
        py_path = os.path.join(td, "test_distinct_behaviors.py")
        with open(py_path, "w") as fh:
            fh.write('''\
def test_create(fake_clock):
    fake_clock.sleep(5)
    result = create_user("x")
    assert result.ok


def test_update(fake_clock):
    fake_clock.sleep(5)
    result = update_user("x")
    assert result.ok


def test_delete(fake_clock):
    fake_clock.sleep(5)
    result = delete_user("x")
    assert result.ok
''')
        r = run(["scan", py_path, "--json"])
        found = json.loads(r.stdout)
        check("distinct tests and injected sleeps stay clean",
              r.returncode == 0 and not found, json.dumps(found)[:300])

        ts_path = os.path.join(td, "literals.ts")
        with open(ts_path, "w") as fh:
            fh.write('''\
export const castAdvice = "never cast as any";
export const loggingExample = "console.log(1)";
export const catchExample = "catch (error) {}";
export const directiveExample = "@ts-ignore";
// Avoid `as any` when a real type is available.
''')
        r = run(["scan", ts_path, "--json"])
        found = json.loads(r.stdout)
        check("generic rules ignore comments and strings",
              r.returncode == 0 and not found, json.dumps(found)[:300])

        go_path = os.path.join(td, "doc.go")
        with open(go_path, "w") as fh:
            fh.write('''\
package store

// Store persists application records.
type Store interface {
\t// Delete an application
\tDelete(id string) error
}

// NewStore returns a Store backed by the given database.
func NewStore(db string) Store {
\treturn nil
}
''')
        r = run(["scan", go_path, "--json"])
        found = json.loads(r.stdout)
        check("godoc declaration comments are not redundant-comment",
              r.returncode == 0 and not found, json.dumps(found)[:300])


def test_hook_posttooluse():
    with tempfile.TemporaryDirectory() as td:
        target = os.path.join(td, "edited.py")
        shutil.copy(os.path.join(FIXTURES, "slop_example.py"), target)
        event = json.dumps({
            "hook_event_name": "PostToolUse",
            "tool_name": "Edit",
            "cwd": td,
            "tool_input": {"file_path": target, "old_string": "a", "new_string": "b"},
        })
        r = run(["hook", "--agent", "claude"], stdin_data=event)
        check("hook blocks with exit 2", r.returncode == 2, "rc=%d" % r.returncode)
        check("hook report on stderr", "slopguard found" in r.stderr and "placeholder-body" in r.stderr,
              r.stderr[:200])

        clean = os.path.join(td, "clean.py")
        with open(clean, "w") as fh:
            fh.write(CLEAN_SOURCE)
        event = posttool_event(td, clean)
        # NOTE: sibling context includes the sloppy file, but findings must
        # only be reported for the clean target file.
        r = run(["hook"], stdin_data=event)
        check("hook passes clean file", r.returncode == 0, r.stderr[:200])


def test_hook_path_with_spaces():
    with tempfile.TemporaryDirectory() as td:
        target = os.path.join(td, "edited file.py")
        shutil.copy(os.path.join(FIXTURES, "slop_example.py"), target)
        event = posttool_event(td, target)
        r = run(["hook"], stdin_data=event)
        check("hook checks paths containing spaces",
              r.returncode == 2 and "placeholder-body" in r.stderr,
              "rc=%d %s" % (r.returncode, r.stderr[:200]))


def test_hook_exclude():
    with tempfile.TemporaryDirectory() as td:
        excluded_dir = os.path.join(td, "excluded")
        os.mkdir(excluded_dir)
        target = os.path.join(excluded_dir, "slop.py")
        shutil.copy(os.path.join(FIXTURES, "slop_example.py"), target)
        with open(os.path.join(td, ".slopguard.json"), "w") as fh:
            json.dump({"hook_exclude": ["*/excluded/*"]}, fh)
        event = posttool_event(td, target)
        r = run(["hook"], stdin_data=event)
        check("hook_exclude omits matching hook targets", r.returncode == 0,
              "rc=%d %s" % (r.returncode, r.stderr[:200]))
        r = run(["scan", target, "--json", "--fail-on", "never"])
        found = json.loads(r.stdout)
        check("hook_exclude does not affect explicit scans", any(
            f["severity"] in ("warn", "error") for f in found), json.dumps(found)[:300])


def test_hook_stop_git():
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["git", "init", "-q", td], check=True, capture_output=True)
        target = os.path.join(td, "changed.ts")
        shutil.copy(os.path.join(FIXTURES, "slop_example.ts"), target)
        subprocess.run(["git", "-C", td, "add", "changed.ts"],
                       check=True, capture_output=True)
        event = json.dumps({"hook_event_name": "Stop", "cwd": td, "session_id": "test-stop-1"})
        hook_env = os.environ.copy()
        hook_env["SLOPGUARD_STATE_DIR"] = os.path.join(td, "state")
        r = run(["hook", "--agent", "codex"], stdin_data=event, env=hook_env)
        check("Stop hook finds staged file before first commit",
              r.returncode == 2 and "as-any" in r.stderr,
              "rc=%d %s" % (r.returncode, r.stderr[:200]))
        r2 = run(["hook", "--agent", "codex"], stdin_data=event, env=hook_env)
        check("Stop hook loop guard (same findings, same session)", r2.returncode == 0,
              "rc=%d" % r2.returncode)

        blocked_state = os.path.join(td, "not-a-directory")
        with open(blocked_state, "w") as fh:
            fh.write("")
        hook_env["SLOPGUARD_STATE_DIR"] = blocked_state
        r3 = run(["hook", "--agent", "codex"], stdin_data=event, env=hook_env)
        check("Stop hook fails open when loop state is unwritable", r3.returncode == 0,
              "rc=%d %s" % (r3.returncode, r3.stderr[:200]))


def test_hook_stop_from_subdirectory():
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["git", "init", "-q", td], check=True, capture_output=True)
        target = os.path.join(td, "changed.ts")
        with open(target, "w") as fh:
            fh.write("export const value: unknown = 1;\n")
        subprocess.run(["git", "-C", td, "add", "changed.ts"], check=True, capture_output=True)
        subprocess.run([
            "git", "-C", td, "-c", "user.name=slopguard",
            "-c", "user.email=slopguard@example.invalid",
            "commit", "-qm", "initial",
        ], check=True, capture_output=True)
        shutil.copy(os.path.join(FIXTURES, "slop_example.ts"), target)
        nested = os.path.join(td, "nested")
        os.mkdir(nested)
        event = json.dumps({
            "hook_event_name": "Stop",
            "cwd": nested,
            "session_id": "test-stop-subdirectory",
        })
        hook_env = os.environ.copy()
        hook_env["SLOPGUARD_STATE_DIR"] = os.path.join(td, "state")
        r = run(["hook", "--agent", "codex"], stdin_data=event, env=hook_env)
        check("Stop hook resolves paths from repository root",
              r.returncode == 2 and "as-any" in r.stderr,
              "rc=%d %s" % (r.returncode, r.stderr[:200]))


def test_hook_garbage_stdin():
    r = run(["hook"], stdin_data="not json at all")
    check("hook tolerates garbage stdin", r.returncode == 0)


def main():
    for fn in (test_scan_fixtures, test_test_suite_rules, test_contract_rules,
               test_contract_schema_formats,
               test_contract_block_parsers, test_contract_canon_and_yaml,
               test_config_schema_files,
               test_contract_false_positive_guards, test_schema_discovery_caps,
               test_diverged_duplicates, test_baseline_ratchet, test_precision_fixes, test_diverged_candidate_recall_and_reporting,
               test_diverged_token_and_decorator_guards,
               test_parallel_scan_determinism,
               test_fingerprint_identity, test_baseline_roots_and_partial_updates,
               test_property_suggestion,
               test_rule_coverage, test_clean_file,
               test_clean_test_file, test_suppression, test_duplicate_function_bindings,
               test_install_command_quoting, test_atomic_installs,
               test_false_positive_guards,
               test_hook_posttooluse, test_hook_path_with_spaces, test_hook_exclude,
               test_hook_stop_git, test_hook_stop_from_subdirectory,
               test_hook_garbage_stdin):
        fn()
    print()
    if failures:
        print("%d test(s) failed: %s" % (len(failures), failures))
        return 1
    print("all tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
