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
    try:
        install.__file__ = os.path.join(
            os.sep, "tmp", "repo with space", "slopguard", "install.py")
        command = install.hook_command("codex")
    finally:
        install.__file__ = original_file
    check("installed hook command quotes repository paths",
          shlex.split(command)[0] == "/tmp/repo with space/bin/slopguard",
          command)
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
// Avoid `as any` when a real type is available.
''')
        r = run(["scan", ts_path, "--json"])
        found = json.loads(r.stdout)
        check("generic rules ignore comments and strings",
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
    for fn in (test_scan_fixtures, test_test_suite_rules, test_clean_file,
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
