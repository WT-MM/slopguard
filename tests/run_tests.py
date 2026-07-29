#!/usr/bin/env python3
"""Smoke tests: run the CLI on the fixtures and check every rule fires."""
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin", "slopguard")
FIXTURES = os.path.join(ROOT, "tests", "fixtures")

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


def run(args, stdin_data=None):
    return subprocess.run([sys.executable, BIN] + args, input=stdin_data,
                          capture_output=True, text=True, timeout=60)


def test_scan_fixtures():
    r = run(["scan", FIXTURES, "--json", "--fail-on", "never"])
    check("scan exits 0 with --fail-on never", r.returncode == 0, r.stderr[:200])
    found = {f["rule"] for f in json.loads(r.stdout)}
    missing = EXPECTED_RULES - found
    check("all expected rules fire", not missing, "missing: %s" % sorted(missing))
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
        event = json.dumps({
            "hook_event_name": "PostToolUse",
            "tool_name": "Write",
            "cwd": td,
            "tool_input": {"file_path": clean},
        })
        # NOTE: sibling context includes the sloppy file, but findings must
        # only be reported for the clean target file.
        r = run(["hook"], stdin_data=event)
        check("hook passes clean file", r.returncode == 0, r.stderr[:200])


def test_hook_stop_git():
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["git", "init", "-q", td], check=True, capture_output=True)
        target = os.path.join(td, "changed.ts")
        shutil.copy(os.path.join(FIXTURES, "slop_example.ts"), target)
        event = json.dumps({"hook_event_name": "Stop", "cwd": td, "session_id": "test-stop-1"})
        r = run(["hook", "--agent", "codex"], stdin_data=event)
        check("Stop hook finds git-dirty file", r.returncode == 2 and "as-any" in r.stderr,
              "rc=%d %s" % (r.returncode, r.stderr[:200]))
        r2 = run(["hook", "--agent", "codex"], stdin_data=event)
        check("Stop hook loop guard (same findings, same session)", r2.returncode == 0,
              "rc=%d" % r2.returncode)


def test_hook_garbage_stdin():
    r = run(["hook"], stdin_data="not json at all")
    check("hook tolerates garbage stdin", r.returncode == 0)


def main():
    for fn in (test_scan_fixtures, test_test_suite_rules, test_clean_file,
               test_clean_test_file, test_suppression,
               test_hook_posttooluse, test_hook_stop_git, test_hook_garbage_stdin):
        fn()
    print()
    if failures:
        print("%d test(s) failed: %s" % (len(failures), failures))
        return 1
    print("all tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
