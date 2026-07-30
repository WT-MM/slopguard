"""Python-specific AST checks for AI-generated slop."""
import ast
import io
import os
import re
import sys
import tokenize

from .comments import hedging_phrase, is_banner, looks_like_code, redundancy
from .findings import Finding

_FUNC_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)
_BLOCK_NODES = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try)
_TERMINATORS = (ast.Return, ast.Raise, ast.Break, ast.Continue)
_SKIP_DECORATORS = {"skip", "skipif", "xfail"}
# Null-object / test-double classes have no-op methods by design.
_NOOP_CLASS_NAME = re.compile(r"noop|null|dummy|fake|mock|stub", re.I)
_NOOP_FILE_NAME = re.compile(r"^(mock|fake|stub)s?_|_(mock|fake|stub)s?\.py$", re.I)


def check_python(path, text, cfg):
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        interpreter = "%d.%d" % sys.version_info[:2]
        if sys.version_info < (3, 10) and re.search(r"^\s*match\s+.+:\s*(#.*)?$", text, re.M):
            return [Finding(
                "syntax-error", "info", path, getattr(e, "lineno", 1) or 1,
                "does not parse under Python %s (slopguard's interpreter) but uses "
                "match statements — likely valid on the newer Python it targets" % interpreter)]
        return [Finding(
            "syntax-error", "error", path, getattr(e, "lineno", 1) or 1,
            "file does not parse under Python %s: %s" % (interpreter, e.msg))]
    except (ValueError, RecursionError):
        return []

    findings = []
    nodes = list(ast.walk(tree))
    parents = _parent_map(nodes)
    _mutable_defaults(findings, path, nodes, text)
    _placeholder_bodies(findings, path, nodes, parents)
    _dead_code(findings, path, nodes)
    _exception_handling(findings, path, nodes, parents)
    _unused_imports(findings, path, tree, nodes, text)
    _unused_private(findings, path, tree, nodes)
    _write_only_attrs(findings, path, nodes)
    _size_and_nesting(findings, path, nodes, cfg)
    _single_method_classes(findings, path, nodes)
    _debug_prints(findings, path, nodes)
    _comments(findings, path, text)
    return findings


def _parent_map(nodes):
    parents = {}
    for node in nodes:
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _is_dunder(name):
    return name.startswith("__") and name.endswith("__")


def _body_without_docstring(fn):
    body = fn.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        return body[1:]
    return body


def _decorator_names(fn):
    names = []
    for dec in fn.decorator_list:
        node = dec.func if isinstance(dec, ast.Call) else dec
        while isinstance(node, ast.Attribute):
            names.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            names.append(node.id)
    return names


def _annotated_with_call(annotation):
    """Annotated[X, Query()]-style params: the framework owns the default."""
    return (isinstance(annotation, ast.Subscript)
            and isinstance(annotation.value, ast.Name)
            and annotation.value.id == "Annotated"
            and any(isinstance(n, ast.Call) for n in ast.walk(annotation)))


def _mutable_defaults(findings, path, nodes, text):
    lines = text.splitlines()
    for fn in (n for n in nodes if isinstance(n, _FUNC_NODES)):
        params = list(fn.args.posonlyargs) + list(fn.args.args)
        defaults = list(fn.args.defaults)
        annotated = {id(d) for p, d in zip(params[len(params) - len(defaults):], defaults)
                     if p.annotation is not None and _annotated_with_call(p.annotation)}
        for default in defaults + [d for d in fn.args.kw_defaults if d]:
            if id(default) in annotated:
                continue
            if 0 < default.lineno <= len(lines) and "noqa" in lines[default.lineno - 1]:
                continue
            bad = isinstance(default, (ast.List, ast.Dict, ast.Set)) or (
                isinstance(default, ast.Call) and isinstance(default.func, ast.Name)
                and default.func.id in ("list", "dict", "set"))
            if bad:
                findings.append(Finding(
                    "mutable-default", "warn", path, default.lineno,
                    "mutable default argument in %s(); shared across calls — use None" % fn.name))


def _placeholder_bodies(findings, path, nodes, parents):
    if _NOOP_FILE_NAME.search(os.path.basename(path)):
        return  # mock_/fake_ backends: no-op methods are the design
    for fn in (n for n in nodes if isinstance(n, _FUNC_NODES)):
        if _is_dunder(fn.name):
            continue  # `__exit__: pass` and friends are correct implementations
        decs = _decorator_names(fn)
        if any("abstract" in d or d == "overload" or d in _SKIP_DECORATORS
               for d in decs):
            continue
        parent = parents.get(fn)
        if isinstance(parent, ast.ClassDef) and _NOOP_CLASS_NAME.search(parent.name):
            continue  # null-object pattern
        if isinstance(parent, ast.ClassDef):
            base_names = set()
            for b in parent.bases:
                node = b.value if isinstance(b, ast.Subscript) else b
                while isinstance(node, ast.Attribute):
                    base_names.add(node.attr)
                    node = node.value
                if isinstance(node, ast.Name):
                    base_names.add(node.id)
            if base_names & {"Protocol", "ABC", "ABCMeta"}:
                continue
        body = _body_without_docstring(fn)
        empty = all(
            isinstance(s, ast.Pass) or
            (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and s.value.value is Ellipsis)
            for s in body
        )
        raises_not_impl = len(body) == 1 and isinstance(body[0], ast.Raise) and (
            (isinstance(body[0].exc, ast.Name) and body[0].exc.id == "NotImplementedError") or
            (isinstance(body[0].exc, ast.Call) and isinstance(body[0].exc.func, ast.Name)
             and body[0].exc.func.id == "NotImplementedError"))
        doc = ast.get_docstring(fn) or ""
        if re.search(r"subclass|override|no-?op|does nothing|by default|hook", doc, re.I):
            continue  # documented extension point, not an unimplemented stub
        if empty and not raises_not_impl:
            findings.append(Finding(
                "placeholder-body", "warn", path, fn.lineno,
                "%s() has no implementation (pass/... only) — implement it or delete it" % fn.name))


def _dead_code(findings, path, nodes):
    for node in nodes:
        for field in ("body", "orelse", "finalbody"):
            stmts = getattr(node, field, None)
            if not isinstance(stmts, list):
                continue
            terminated_at = None
            for s in stmts:
                if terminated_at is not None:
                    findings.append(Finding(
                        "dead-code", "error", path, s.lineno,
                        "unreachable: statement follows %s on line %d" % terminated_at))
                    break
                if isinstance(s, _TERMINATORS):
                    terminated_at = (type(s).__name__.lower(), s.lineno)


# Cancellation is a control-flow signal, not an error: `except CancelledError:
# pass` after task.cancel() is the canonical asyncio/trio/anyio shutdown idiom.
_CANCELLATION_NAMES = {"CancelledError", "Cancelled"}


def _caught_names(handler):
    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    names = []
    for t in types:
        if isinstance(t, ast.Attribute):
            names.append(t.attr)
        elif isinstance(t, ast.Name):
            names.append(t.id)
        else:
            names.append("")
    return names


def _in_loop(node, parents):
    cursor = parents.get(node)
    while cursor is not None:
        if isinstance(cursor, (ast.For, ast.AsyncFor, ast.While)):
            return True
        if isinstance(cursor, _FUNC_NODES):
            return False
        cursor = parents.get(cursor)
    return False


def _exception_handling(findings, path, nodes, parents):
    for node in nodes:
        if not isinstance(node, ast.ExceptHandler):
            continue
        if node.type is None:
            findings.append(Finding(
                "bare-except", "warn", path, node.lineno,
                "bare `except:` catches SystemExit/KeyboardInterrupt too — name the exception"))
        if not all(isinstance(s, ast.Pass) for s in node.body):
            continue
        if node.type is not None and \
                all(n in _CANCELLATION_NAMES for n in _caught_names(node)):
            continue
        parent_try = next((t for t in nodes if isinstance(t, ast.Try) and node in t.handlers), None)
        if node.type is not None and parent_try is not None and parent_try.body \
                and isinstance(parent_try.body[-1], (ast.Return, ast.Break, ast.Continue)) \
                and _in_loop(parent_try, parents):
            continue  # try-next-candidate SEARCH LOOP: pass moves to the next attempt
        findings.append(Finding(
            "swallowed-exception", "warn", path, node.lineno,
            "exception silently swallowed (`except: pass`) — handle, log, re-raise, "
            "or make suppression explicit with contextlib.suppress"))


def _top_level_imports(nodes, text):
    """(local_name, lineno) pairs; indented imports are often conditional/optional — skip.
    Imports carrying a noqa anywhere in their line span (side-effect
    registrations) and `_`/`__` aliases (the intentionally-unused idiom) are
    exempt."""
    lines = text.splitlines()

    def spans_noqa(node):
        end = getattr(node, "end_lineno", node.lineno)
        return any("noqa" in lines[i] for i in range(node.lineno - 1, min(end, len(lines))))

    imported = []
    for node in nodes:
        if getattr(node, "col_offset", 0) != 0:
            continue
        if not isinstance(node, (ast.Import, ast.ImportFrom)) or spans_noqa(node):
            continue
        if isinstance(node, ast.Import):
            names = ((a.asname or a.name.split(".")[0], node.lineno)
                     for a in node.names if a.asname or "." not in a.name)
        elif node.module != "__future__":
            names = ((a.asname or a.name, node.lineno)
                     for a in node.names if a.name != "*")
        else:
            continue
        imported.extend((n, ln) for n, ln in names if n not in ("_", "__"))
    return imported


def _unused_imports(findings, path, tree, nodes, text):
    base = os.path.basename(path)
    if base == "__init__.py" or base in ("compat.py", "_compat.py"):
        return  # re-export surfaces: imports exist for consumers, not this file
    imported = _top_level_imports(nodes, text)
    if not imported:
        return
    used = {n.id for n in nodes if isinstance(n, ast.Name)}
    exported = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "__all__" and isinstance(node.value, (ast.List, ast.Tuple)):
                    exported |= {e.value for e in node.value.elts
                                 if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    # names referenced inside string annotations / docstring-typed code are rare; accept the risk
    for name, lineno in imported:
        if name not in used and name not in exported:
            findings.append(Finding(
                "unused-import", "warn", path, lineno,
                "`%s` is imported but never used" % name))


def _unused_private(findings, path, tree, nodes):
    loads = {n.id for n in nodes
             if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    attr_refs = {n.attr for n in nodes if isinstance(n, ast.Attribute)}

    for node in tree.body:
        if isinstance(node, _FUNC_NODES + (ast.ClassDef,)):
            name = node.name
            if not name.startswith("_") or _is_dunder(name):
                continue
            if name in loads or name in attr_refs:
                continue
            if isinstance(node, _FUNC_NODES) and \
                    any("fixture" in d for d in _decorator_names(node)):
                continue  # pytest fixtures are invoked by the framework
            findings.append(Finding(
                "unused-private", "warn", path, node.lineno,
                "private %s `%s` is never used in this file" %
                ("class" if isinstance(node, ast.ClassDef) else "function", name)))

    for cls in (n for n in nodes if isinstance(n, ast.ClassDef)):
        for m in cls.body:
            if not isinstance(m, _FUNC_NODES):
                continue
            name = m.name
            if not name.startswith("_") or _is_dunder(name):
                continue
            if _decorator_names(m):
                continue  # properties / framework decorators register themselves
            if name not in attr_refs and name not in loads:
                has_base = any(not (isinstance(b, ast.Name) and b.id == "object")
                               for b in cls.bases)
                findings.append(Finding(
                    "unused-private", "info" if has_base else "warn", path, m.lineno,
                    "private method `%s.%s` is never called in this file%s"
                    % (cls.name, name,
                       " (subclass: may implement a base-class template method)"
                       if has_base else
                       " (if it's a framework hook, add a slopguard:ignore comment)")))


def _write_only_attrs(findings, path, nodes):
    # Attrs on classes WITH bases may be consumed by the parent (stdlib
    # CookieJar reads _cookies_lock; template methods read child state) —
    # those demote to info.
    inherited_spans = []
    for cls in (n for n in nodes if isinstance(n, ast.ClassDef)):
        if any(not (isinstance(b, ast.Name) and b.id == "object") for b in cls.bases):
            inherited_spans.append((cls.lineno, getattr(cls, "end_lineno", cls.lineno)))

    def in_subclass(lineno):
        return any(lo <= lineno <= hi for lo, hi in inherited_spans)

    stores = {}  # attr -> first store lineno
    reads = set()
    for node in nodes:
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
            if isinstance(node.ctx, ast.Store):
                stores.setdefault(node.attr, node.lineno)
            else:
                reads.add(node.attr)
        if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Attribute) \
                and isinstance(node.target.value, ast.Name) and node.target.value.id == "self":
            reads.add(node.target.attr)
    for attr, lineno in sorted(stores.items(), key=lambda kv: kv[1]):
        if attr.startswith("_") and not _is_dunder(attr) and attr not in reads:
            findings.append(Finding(
                "write-only-attr", "info" if in_subclass(lineno) else "warn", path, lineno,
                "`self.%s` is assigned but never read in this file — state added \"just in case\"?" % attr))


def _size_and_nesting(findings, path, nodes, cfg):
    max_lines = cfg.get("max_function_lines", 80)
    max_depth = cfg.get("max_nesting", 4)
    for fn in (n for n in nodes if isinstance(n, _FUNC_NODES)):
        end = getattr(fn, "end_lineno", fn.lineno)
        body = _body_without_docstring(fn)
        start = body[0].lineno if body else fn.lineno
        span = end - start + 1  # signature and docstring are not complexity
        if span > max_lines:
            findings.append(Finding(
                "long-function", "warn", path, fn.lineno,
                "%s() is %d lines (max %d) — split it up" % (fn.name, span, max_lines)))
        worst = [0, fn.lineno]

        def descend(node, depth):
            for child in ast.iter_child_nodes(node):
                nd = depth + 1 if isinstance(child, _BLOCK_NODES) else depth
                if isinstance(node, ast.If) and isinstance(child, ast.If) \
                        and len(node.orelse) == 1 and node.orelse[0] is child:
                    nd = depth  # elif: an AST-nested If, but not deeper code
                if nd > worst[0]:
                    worst[0], worst[1] = nd, getattr(child, "lineno", worst[1])
                if not isinstance(child, _FUNC_NODES + (ast.ClassDef, ast.Lambda)):
                    descend(child, nd)

        descend(fn, 0)
        if worst[0] > max_depth:
            findings.append(Finding(
                "deep-nesting", "warn", path, worst[1],
                "%s() nests %d levels deep (max %d) — use early returns or extract helpers"
                % (fn.name, worst[0], max_depth)))


def _single_method_classes(findings, path, nodes):
    for cls in (n for n in nodes if isinstance(n, ast.ClassDef)):
        if cls.bases or cls.keywords or cls.decorator_list:
            continue
        methods, other = [], []
        for node in cls.body:
            if isinstance(node, _FUNC_NODES):
                methods.append(node)
            elif not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)):
                other.append(node)
        if other:
            continue
        public = [m for m in methods if not m.name.startswith("_")]
        dunders = [m for m in methods if _is_dunder(m.name) and m.name != "__init__"]
        if len(public) == 1 and not dunders and len(methods) <= 2:
            findings.append(Finding(
                "single-method-class", "info", path, cls.lineno,
                "class %s has one real method (%s) — a function may be simpler" % (cls.name, public[0].name)))


def _debug_prints(findings, path, nodes):
    # print() is only suspicious when the module already has a logger —
    # in plain scripts/CLIs, print IS the output mechanism.
    logs = False
    for node in nodes:
        if isinstance(node, ast.Import):
            logs = logs or any(a.name.split(".")[0] in ("logging", "loguru", "structlog")
                               for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            logs = logs or (node.module or "").split(".")[0] in ("logging", "loguru", "structlog")
    if not logs:
        return
    for node in nodes:
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
            findings.append(Finding(
                "debug-artifact", "info", path, node.lineno,
                "print() in a module that uses logging — leftover debug output?"))


def _comments(findings, path, text):
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return
    lines = text.splitlines()

    def next_code_line(after_idx):
        for i in range(after_idx, len(lines)):
            stripped = lines[i].strip()
            if stripped and not stripped.startswith("#"):
                return stripped
        return None

    banner_lines = set()

    for tok in tokens:
        if tok.type != tokenize.COMMENT:
            continue
        comment = tok.string.lstrip("#").strip()
        if not comment or "slopguard:" in comment or "noqa" in comment:
            continue
        if "type: ignore" in comment:
            findings.append(Finding(
                "type-ignore", "info", path, tok.start[0],
                "`# type: ignore` hides a type error — fix the type instead"))
            continue
        phrase = hedging_phrase(comment)
        if phrase and "/examples/" not in path.replace(os.sep, "/"):
            findings.append(Finding(
                "hedging-comment", "warn", path, tok.start[0],
                "hedging comment (\"%s...\") — implement the real thing or file a TODO with a ticket" % phrase))
            continue
        if is_banner(comment) or looks_like_code(comment):
            banner_lines.add(tok.start[0])
            continue  # dividers organize / commented-out code isn't prose
        if tok.start[0] - 1 in banner_lines:
            banner_lines.add(tok.start[0])
            continue  # the title line under a banner divider
        code_part = lines[tok.start[0] - 1][:tok.start[1]].strip()
        code = code_part if code_part else next_code_line(tok.start[0])
        if not code:
            continue
        level = redundancy(comment, code)
        if level == "full":
            findings.append(Finding(
                "redundant-comment", "warn", path, tok.start[0],
                "comment restates the code (\"%s\") — delete it" % comment[:60]))
        elif level == "partial":
            findings.append(Finding(
                "redundant-comment", "info", path, tok.start[0],
                "comment mostly restates the code (\"%s\")" % comment[:60]))
