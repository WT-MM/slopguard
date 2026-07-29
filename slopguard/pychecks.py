"""Python-specific AST checks for AI-generated slop."""
import ast
import io
import tokenize

from .comments import hedging_phrase, redundancy
from .findings import Finding

_FUNC_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)
_BLOCK_NODES = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try)
_TERMINATORS = (ast.Return, ast.Raise, ast.Break, ast.Continue)
_SKIP_DECORATORS = {"skip", "skipif", "xfail"}


def check_python(path, text, cfg):
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        return [Finding("syntax-error", "error", path, getattr(e, "lineno", 1) or 1,
                        "file does not parse: %s" % e.msg)]
    except (ValueError, RecursionError):
        return []

    findings = []
    nodes = list(ast.walk(tree))
    parents = _parent_map(nodes)
    _mutable_defaults(findings, path, nodes)
    _placeholder_bodies(findings, path, nodes, parents)
    _dead_code(findings, path, nodes)
    _exception_handling(findings, path, nodes)
    _unused_imports(findings, path, tree, nodes)
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


def _mutable_defaults(findings, path, nodes):
    for fn in (n for n in nodes if isinstance(n, _FUNC_NODES)):
        for default in list(fn.args.defaults) + [d for d in fn.args.kw_defaults if d]:
            bad = isinstance(default, (ast.List, ast.Dict, ast.Set)) or (
                isinstance(default, ast.Call) and isinstance(default.func, ast.Name)
                and default.func.id in ("list", "dict", "set"))
            if bad:
                findings.append(Finding(
                    "mutable-default", "warn", path, default.lineno,
                    "mutable default argument in %s(); shared across calls — use None" % fn.name))


def _placeholder_bodies(findings, path, nodes, parents):
    for fn in (n for n in nodes if isinstance(n, _FUNC_NODES)):
        decs = _decorator_names(fn)
        if any("abstract" in d or d == "overload" or d in _SKIP_DECORATORS
               for d in decs):
            continue
        parent = parents.get(fn)
        if isinstance(parent, ast.ClassDef):
            base_names = set()
            for b in parent.bases:
                node = b
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


def _exception_handling(findings, path, nodes):
    for node in nodes:
        if not isinstance(node, ast.ExceptHandler):
            continue
        if node.type is None:
            findings.append(Finding(
                "bare-except", "warn", path, node.lineno,
                "bare `except:` catches SystemExit/KeyboardInterrupt too — name the exception"))
        body_is_noop = all(isinstance(s, ast.Pass) for s in node.body)
        if body_is_noop:
            findings.append(Finding(
                "swallowed-exception", "warn", path, node.lineno,
                "exception silently swallowed (`except: pass`) — handle, log, or re-raise"))


def _top_level_imports(nodes):
    """(local_name, lineno) pairs; indented imports are often conditional/optional — skip."""
    imported = []
    for node in nodes:
        if getattr(node, "col_offset", 0) != 0:
            continue
        if isinstance(node, ast.Import):
            imported.extend(
                (a.asname or a.name.split(".")[0], node.lineno)
                for a in node.names if a.asname or "." not in a.name)
        elif isinstance(node, ast.ImportFrom) and node.module != "__future__":
            imported.extend((a.asname or a.name, node.lineno)
                            for a in node.names if a.name != "*")
    return imported


def _unused_imports(findings, path, tree, nodes):
    if path.endswith("__init__.py"):
        return
    imported = _top_level_imports(nodes)
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
            if name.startswith("_") and not _is_dunder(name) and name not in loads and name not in attr_refs:
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
                findings.append(Finding(
                    "unused-private", "warn", path, m.lineno,
                    "private method `%s.%s` is never called in this file "
                    "(if it's a framework hook, add a slopguard:ignore comment)" % (cls.name, name)))


def _write_only_attrs(findings, path, nodes):
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
                "write-only-attr", "warn", path, lineno,
                "`self.%s` is assigned but never read in this file — state added \"just in case\"?" % attr))


def _size_and_nesting(findings, path, nodes, cfg):
    max_lines = cfg.get("max_function_lines", 80)
    max_depth = cfg.get("max_nesting", 4)
    for fn in (n for n in nodes if isinstance(n, _FUNC_NODES)):
        end = getattr(fn, "end_lineno", fn.lineno)
        span = end - fn.lineno + 1
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
        if phrase:
            findings.append(Finding(
                "hedging-comment", "warn", path, tok.start[0],
                "hedging comment (\"%s...\") — implement the real thing or file a TODO with a ticket" % phrase))
            continue
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
