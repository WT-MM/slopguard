"""Test-suite slop checks — over-specified, mock-verifying, or redundant tests.

The goal these rules push toward: a minimal set of tests that pin down
observable behavior, not the current implementation's wiring. They run only
on files `cli.is_test_file` classifies as tests.
"""
import ast
import re

from .duplicates import _argument_names
from .findings import Finding

# Calls to helpers with these fragments in their name count as assertions,
# so suites built on custom assert helpers aren't flagged as assertion-free.
_ASSERT_HELPER_HINTS = ("assert", "check", "verify", "expect", "validate")
_MOCK_ASSERT_PREFIXES = ("assert_called", "assert_awaited", "assert_any_call",
                         "assert_has_calls", "assert_not_called", "assert_not_awaited")
_RAISES_NAMES = {"raises", "assertRaises", "assertRaisesRegex", "assertWarns",
                 "assertWarnsRegex", "assertLogs"}
_NAMEDTUPLE_PUBLIC = {"_asdict", "_replace", "_fields", "_make"}
_TRIVIAL_CONSTANTS = {None, True, False, 0, 1, -1, ""}

MAX_MOCKS_PER_TEST = 5
LONG_STRING_ASSERT = 48
BIG_LITERAL_ENTRIES = 8
MIN_PARAMETRIZE_GROUP = 3
MIN_PROPERTY_GROUP = 6
_SLEEP_MODULES = {"asyncio", "time", "trio", "anyio", "gevent", "eventlet"}


def check_python_tests(path, text, cfg):
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return []
    if _is_manual_harness(tree):
        return []  # print-driven rig scripts aren't unit tests
    findings = []
    tests = _collect_test_functions(tree)
    sleep_modules, sleep_functions = _sleep_symbols(tree)
    assertful = _assertful_locals(tree)
    for fn in tests:
        _check_one_test(
            findings, path, fn, cfg, sleep_modules, sleep_functions, assertful)
    _parametrize_candidates(findings, path, tests, cfg)
    return findings


def _is_manual_harness(tree):
    """A test-named file with a __main__ entrypoint is a hand-run diagnostic
    script (rig/hardware harness), not a unit-test suite."""
    for node in tree.body:
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare) \
                and isinstance(node.test.left, ast.Name) \
                and node.test.left.id == "__name__":
            return True
    return False


def _assertful_locals(tree):
    """Names of module-local functions that assert internally — calling one
    counts as asserting (domain assert-helpers like _play_refused)."""
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name.startswith("test"):
            continue
        if any(isinstance(inner, ast.Assert) for inner in ast.walk(node)):
            names.add(node.name)
    return names


def _collect_test_functions(tree):
    tests = []
    def visit(body):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name.startswith("test"):
                tests.append(node)
            elif isinstance(node, ast.ClassDef):
                visit(node.body)
    visit(tree.body)
    return tests


def _classify_assertions(fn, assertful=frozenset()):
    """Returns [(node, kind)] with kind in plain|mock|raises|helper."""
    out = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Assert):
            out.append((node, "plain"))
        elif isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute):
                if f.attr.startswith(_MOCK_ASSERT_PREFIXES):
                    out.append((node, "mock"))
                elif f.attr in _RAISES_NAMES:
                    out.append((node, "raises"))
                elif f.attr.startswith("assert") or f.attr == "fail":
                    out.append((node, "plain"))
            elif isinstance(f, ast.Name):
                low = f.id.lower()
                if f.id in assertful or any(h in low for h in _ASSERT_HELPER_HINTS):
                    out.append((node, "helper"))
    return out


def _is_placeholder(fn):
    body = fn.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    return all(isinstance(s, ast.Pass) or
               (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)
                and s.value.value is Ellipsis)
               for s in body)


def _check_one_test(findings, path, fn, cfg, sleep_modules, sleep_functions,
                    assertful=frozenset()):
    assertions = _classify_assertions(fn, assertful)

    if not assertions and not _is_placeholder(fn):
        findings.append(Finding(
            "no-assert-test", "warn", path, fn.lineno,
            "%s() never asserts anything — it only proves the code doesn't crash" % fn.name))
    elif assertions and all(kind == "mock" for _, kind in assertions):
        # info, not warn: the harness measured 0% precision on driver-style
        # tests where the mocked SDK IS the system boundary and call shape is
        # the observable behavior. Stays visible in scans, never blocks.
        findings.append(Finding(
            "mock-only-test", "info", path, fn.lineno,
            "%s() only asserts how collaborators were called — if the mock is not a "
            "hardware/SDK boundary, assert what the code returned or changed" % fn.name))

    _tautologies(findings, path, fn)
    _conditional_asserts(findings, path, fn)
    _overspecified_asserts(findings, path, fn)
    _mock_echo(findings, path, fn)
    _sleeps(findings, path, fn, sleep_modules, sleep_functions)
    _private_pokes(findings, path, fn)
    _mock_count(findings, path, fn)


def _same_expr(a, b):
    try:
        return ast.dump(a) == ast.dump(b)
    except (TypeError, RecursionError):
        return False


def _tautologies(findings, path, fn):
    for node in ast.walk(fn):
        expr = None
        if isinstance(node, ast.Assert):
            expr = node.test
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in ("assertEqual", "assertIs") and len(node.args) >= 2 \
                    and _same_expr(node.args[0], node.args[1]):
                findings.append(Finding(
                    "tautological-assert", "warn", path, node.lineno,
                    "%s compares an expression to itself — always passes" % node.func.attr))
                continue
            if node.func.attr == "assertTrue" and node.args \
                    and isinstance(node.args[0], ast.Constant) and node.args[0].value:
                expr = node.args[0]
        if expr is None:
            continue
        if isinstance(expr, ast.Constant) and expr.value:
            findings.append(Finding(
                "tautological-assert", "warn", path, node.lineno,
                "assertion on a truthy constant — always passes, tests nothing"))
        elif isinstance(expr, ast.Compare) and len(expr.ops) == 1 \
                and isinstance(expr.ops[0], ast.Eq) and _same_expr(expr.left, expr.comparators[0]):
            findings.append(Finding(
                "tautological-assert", "warn", path, node.lineno,
                "assertion compares an expression to itself — always passes"))


def _branch_asserts(stmts):
    for stmt in stmts:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Assert):
                return True
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr.startswith("assert"):
                return True
    return False


def _conditional_asserts(findings, path, fn):
    parents = {}
    for node in ast.walk(fn):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    for node, kind in _classify_assertions(fn):
        if kind == "helper":
            continue
        cursor = parents.get(node)
        while cursor is not None and cursor is not fn:
            if isinstance(cursor, ast.If):
                if cursor.orelse and _branch_asserts(cursor.body) and _branch_asserts(cursor.orelse):
                    cursor = parents.get(cursor)
                    continue  # if/else where BOTH branches assert — never vacuous
                findings.append(Finding(
                    "conditional-assert", "warn", path, node.lineno,
                    "assertion inside an `if` — the test only checks anything on some "
                    "paths; make each behavior its own unconditional test"))
                return  # once per test function is enough
            cursor = parents.get(cursor)


def _assert_compare_sides(fn):
    """Yield (lineno, expr) for each side of equality assertions in fn."""
    for node in ast.walk(fn):
        if isinstance(node, ast.Assert) and isinstance(node.test, ast.Compare) \
                and len(node.test.ops) == 1 and isinstance(node.test.ops[0], ast.Eq):
            yield node.lineno, node.test.left
            yield node.lineno, node.test.comparators[0]
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in ("assertEqual", "assertDictEqual", "assertListEqual"):
            for arg in node.args[:2]:
                yield node.lineno, arg


def _overspecified_asserts(findings, path, fn):
    for lineno, expr in _assert_compare_sides(fn):
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str) \
                and len(expr.value) >= LONG_STRING_ASSERT:
            findings.append(Finding(
                "brittle-exact-string", "warn", path, lineno,
                "equality against a %d-char string ties the test to incidental wording — "
                "assert the meaningful part (substring, parsed field)" % len(expr.value)))
        elif isinstance(expr, ast.Dict) and len(expr.keys) >= BIG_LITERAL_ENTRIES:
            findings.append(Finding(
                "overspecified-assert", "warn", path, lineno,
                "equality against a %d-key literal dict pins every field at once — "
                "assert the fields this test is actually about" % len(expr.keys)))
        elif isinstance(expr, (ast.List, ast.Tuple)) and len(expr.elts) >= BIG_LITERAL_ENTRIES:
            findings.append(Finding(
                "overspecified-assert", "warn", path, lineno,
                "equality against a %d-element literal pins the whole structure — "
                "assert the elements this test is actually about" % len(expr.elts)))


def _mock_echo(findings, path, fn):
    planted = {}  # constant value -> lineno where a mock was told to return it
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for t in node.targets:
                if isinstance(t, ast.Attribute) and t.attr == "return_value":
                    planted.setdefault(node.value.value, node.lineno)
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "return_value" and isinstance(kw.value, ast.Constant):
                    planted.setdefault(kw.value.value, node.lineno)
    if not planted:
        return
    for lineno, expr in _assert_compare_sides(fn):
        if isinstance(expr, ast.Constant) and expr.value not in _TRIVIAL_CONSTANTS:
            try:
                hit = expr.value in planted
            except TypeError:
                hit = False
            if hit:
                findings.append(Finding(
                    "mock-echo-test", "warn", path, lineno,
                    "asserts the exact value the mock was told to return (set on line %d) — "
                    "this verifies the mock, not the code under test" % planted[expr.value]))
                return


def _sleep_symbols(tree):
    modules = set()
    functions = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _SLEEP_MODULES:
                    modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module in _SLEEP_MODULES:
            for alias in node.names:
                if alias.name == "sleep":
                    functions.add(alias.asname or alias.name)
    return modules, functions


def _locally_bound_names(fn):
    names = _argument_names(fn.args)
    names.update(
        node.id for node in ast.walk(fn)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store))
    return names


def _passed_callbacks(fn):
    names = set()
    lambdas = set()
    for call in (n for n in ast.walk(fn) if isinstance(n, ast.Call)):
        values = list(call.args) + [keyword.value for keyword in call.keywords]
        for value in values:
            for node in ast.walk(value):
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                    names.add(node.id)
                elif isinstance(node, ast.Lambda):
                    lambdas.add(id(node))
    return names, lambdas


def _sleeps(findings, path, fn, sleep_modules, sleep_functions):
    shadowed = _locally_bound_names(fn)
    nested = set()
    passed_callbacks, passed_lambdas = _passed_callbacks(fn)
    for inner in ast.walk(fn):
        is_passed_function = isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)) \
            and inner.name in passed_callbacks
        is_passed_lambda = isinstance(inner, ast.Lambda) and id(inner) in passed_lambdas
        if inner is not fn and (is_passed_function or is_passed_lambda):
            nested.update(id(n) for n in ast.walk(inner))
    for node in ast.walk(fn):
        if id(node) in nested:
            continue  # sleep inside an injected callback IS the fixture behavior
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        is_sleep = (
            isinstance(f, ast.Attribute) and f.attr == "sleep"
            and isinstance(f.value, ast.Name)
            and f.value.id in sleep_modules and f.value.id not in shadowed
        ) or (
            isinstance(f, ast.Name) and f.id in sleep_functions
            and f.id not in shadowed
        )
        if not is_sleep:
            continue
        if node.args and isinstance(node.args[0], ast.Constant) and not node.args[0].value:
            continue  # sleep(0) is a yield, not a wait
        findings.append(Finding(
            "sleep-in-test", "warn", path, node.lineno,
            "real sleep in a test — slow and flaky; inject a clock or poll a condition"))


def _private_pokes(findings, path, fn):
    seen = set()
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)):
            continue
        attr = node.attr
        if not attr.startswith("_") or attr.startswith("__") or attr in _NAMEDTUPLE_PUBLIC:
            continue
        if isinstance(node.value, ast.Name) and node.value.id == "self":
            continue  # the test class's own helpers
        if attr in seen:
            continue
        seen.add(attr)
        findings.append(Finding(
            "private-poke-test", "warn", path, node.lineno,
            "test reads private `%s` — it pins the implementation, not the observable "
            "behavior; assert through the public API" % attr))


def _mock_count(findings, path, fn):
    count = 0
    for dec in fn.decorator_list:
        node = dec.func if isinstance(dec, ast.Call) else dec
        while isinstance(node, ast.Attribute):
            if "patch" in node.attr:
                count += 1
                break
            node = node.value
        else:
            if isinstance(node, ast.Name) and "patch" in node.id:
                count += 1
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
            if name == "patch" or name in ("MagicMock", "AsyncMock", "Mock"):
                count += 1
    if count > MAX_MOCKS_PER_TEST:
        findings.append(Finding(
            "excessive-mocking", "warn", path, fn.lineno,
            "%s() sets up %d mocks/patches — at this point it tests the wiring diagram; "
            "test a bigger unit or restructure the dependency" % (fn.name, count)))


class _ConstNorm(ast.NodeTransformer):
    def visit_Constant(self, node):
        return ast.copy_location(ast.Constant(value="C"), node)


def _test_shape(fn):
    import copy
    fn = copy.deepcopy(fn)
    body = fn.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        fn.body = body[1:]
    fn.name = "test"
    fn.decorator_list = []
    fn = _ConstNorm().visit(fn)
    return ast.dump(fn, annotate_fields=False)


def _parametrize_candidates(findings, path, tests, cfg):
    min_group = cfg.get("min_parametrize_group", MIN_PARAMETRIZE_GROUP)
    groups = {}
    for fn in tests:
        if len(fn.body) < 2:
            continue
        if any("parametrize" in ast.dump(d) for d in fn.decorator_list):
            continue
        groups.setdefault(_test_shape(fn), []).append(fn)
    for fns in groups.values():
        if len(fns) < min_group:
            continue
        names = ", ".join(f.name for f in fns[:4]) + ("…" if len(fns) > 4 else "")
        message = ("%d tests are identical except for literal values (%s) — collapse into one "
                   "parametrized test so the behavior space is stated in one place"
                   % (len(fns), names))
        if len(fns) >= MIN_PROPERTY_GROUP:
            message += ("; at this many cases, consider stating the rule once as a "
                        "property-based test (hypothesis) instead of enumerating examples")
        findings.append(Finding("parametrize-candidate", "warn", path, fns[0].lineno, message))


# ---------------------------------------------------------------- JS/TS side

_BLOCK_START = re.compile(r"^[ \t]*(?:it|test)(\.\w+)?\s*\(\s*[`'\"]", re.M)
_EXPECTISH = re.compile(
    r"\b(?:expect(?:TypeOf)?|assert\w*|check\w*|verify\w*|validate\w*)"
    r"\s*(?:<|\()", re.I)
_MOCK_CALL_ASSERT = re.compile(r"toHaveBeenCalled|toBeCalled|toHaveBeenNthCalled|toHaveBeenLastCalled")
_TAUTOLOGY = re.compile(
    r"expect\(\s*(true|false|\d+|[`'\"][^`'\"]*[`'\"])\s*\)\s*\.\s*(?:toBe|toEqual|toStrictEqual)\(\s*\1\s*\)")
_JS_SLEEP = re.compile(r"new\s+Promise\s*\([^;\n]*setTimeout")
_LONG_STR_EXPECT = re.compile(r"\.\s*to(?:Be|Equal|StrictEqual|Contain)\(\s*[`'\"]([^`'\"]{%d,})" % LONG_STRING_ASSERT)
_JS_MOCKS = re.compile(r"\b(?:jest|vi)\s*\.\s*(?:mock|spyOn|fn)\s*\(")


def _js_string_literals(text):
    """Yield raw JS string contents while ignoring comments."""
    values = []
    i = 0
    while i < len(text):
        if text.startswith("//", i):
            newline = text.find("\n", i + 2)
            i = len(text) if newline < 0 else newline + 1
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = len(text) if end < 0 else end + 2
            continue
        if text[i] not in "\"'`":
            i += 1
            continue
        quote = text[i]
        i += 1
        value = []
        while i < len(text):
            if text[i] == "\\" and i + 1 < len(text):
                value.extend(text[i:i + 2])
                i += 2
            elif text[i] == quote:
                values.append("".join(value))
                i += 1
                break
            else:
                value.append(text[i])
                i += 1
    return values


def _is_round_trip_expectation(block, match):
    """The expected literal must occur inside this expect(...) subject.

    Counting it anywhere in the test lets an unrelated variable or comment
    disable brittle-exact-string.
    """
    prefix = block[:match.start()]
    starts = list(re.finditer(r"\bexpect\s*\(", prefix))
    if not starts:
        return False
    tail = prefix[starts[-1].end():]
    depth = 0
    quote = None
    escaped = False
    end = len(tail)
    for i, char in enumerate(tail):
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "\"'`":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            if depth == 0:
                end = i
                break
            depth -= 1
        elif char == "," and depth == 0:
            end = i
            break
    subject = tail[:end]
    return match.group(1) in _js_string_literals(subject)


def check_generic_tests(path, text, cfg, ext):
    findings = []
    lines = text.splitlines()
    blocks = []
    for m in _BLOCK_START.finditer(text):
        if m.group(1) in (".todo", ".skip"):
            continue
        lineno = text.count("\n", 0, m.start()) + 1
        indent = len(lines[lineno - 1]) - len(lines[lineno - 1].lstrip())
        end_line = len(lines)
        for j in range(lineno, len(lines)):
            stripped = lines[j].strip()
            line_indent = len(lines[j]) - len(lines[j].lstrip())
            if stripped.startswith("}") and line_indent <= indent:
                end_line = j + 1
                break
        blocks.append((lineno, "\n".join(lines[lineno - 1:end_line])))

    shapes = {}
    for lineno, block in blocks:
        expects = _EXPECTISH.findall(block)
        n_expect = len(re.findall(r"\bexpect\s*\(", block))
        if not expects and block.count("\n") >= 3:
            findings.append(Finding(
                "no-assert-test", "warn", path, lineno,
                "test block never asserts anything — it only proves the code doesn't crash"))
        elif n_expect and len(_MOCK_CALL_ASSERT.findall(block)) >= n_expect:
            findings.append(Finding(
                "mock-only-test", "info", path, lineno,
                "test only asserts how collaborators were called — if the mock is not a "
                "hardware/SDK boundary, assert what the code returned or changed"))
        for m in _TAUTOLOGY.finditer(block):
            findings.append(Finding(
                "tautological-assert", "warn", path, lineno + block.count("\n", 0, m.start()),
                "expect(x).toBe(x) — always passes, tests nothing"))
        for m in _JS_SLEEP.finditer(block):
            findings.append(Finding(
                "sleep-in-test", "warn", path, lineno + block.count("\n", 0, m.start()),
                "real setTimeout wait in a test — use fake timers or poll a condition"))
        for m in _LONG_STR_EXPECT.finditer(block):
            if _is_round_trip_expectation(block, m):
                continue  # round-trip identity: the expected literal is also the input
            findings.append(Finding(
                "brittle-exact-string", "warn", path, lineno + block.count("\n", 0, m.start()),
                "equality against a %d-char string ties the test to incidental wording — "
                "assert the meaningful part" % len(m.group(1))))
        if len(_JS_MOCKS.findall(block)) > MAX_MOCKS_PER_TEST:
            findings.append(Finding(
                "excessive-mocking", "warn", path, lineno,
                "test sets up %d mocks/spies — it tests the wiring diagram, not behavior"
                % len(_JS_MOCKS.findall(block))))
        norm = re.sub(r"[`'\"][^`'\"]*[`'\"]", "S", block)
        norm = re.sub(r"\b\d+(?:\.\d+)?\b", "N", norm)
        norm = re.sub(r"\s+", " ", norm).strip()
        if len(norm) > 40 and block.count("\n") >= 2:
            shapes.setdefault(norm, []).append(lineno)

    min_group = cfg.get("min_parametrize_group", MIN_PARAMETRIZE_GROUP)
    for linenos in shapes.values():
        if len(linenos) < min_group:
            continue
        message = ("%d test blocks are identical except for literal values (lines %s) — "
                   "collapse into one it.each/test.each table"
                   % (len(linenos), ", ".join(str(l) for l in linenos)))
        if len(linenos) >= MIN_PROPERTY_GROUP:
            message += ("; at this many cases, consider stating the rule once as a "
                        "property-based test (fast-check) instead of enumerating examples")
        findings.append(Finding("parametrize-candidate", "warn", path, linenos[0], message))
    return findings
