"""Duplicate-code detection.

Two passes:
1. Language-agnostic: hash sliding windows of normalized lines across all
   files; catches copy-paste blocks (the dominant agent failure mode).
2. Python-only: structural function comparison with identifiers normalized;
   catches "wrote the same helper again with different names".
"""
import ast

from .findings import Finding

WINDOW = 6           # consecutive normalized lines per window
MIN_BLOCK_CHARS = 120  # windows of trivial one-liners don't count
MAX_REPORTS = 20
_COMMENT_PREFIXES = ("#", "//", "/*", "*", "*/", "--")


def _norm_lines(text):
    out = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        s = raw.strip()
        if not s or s.startswith(_COMMENT_PREFIXES):
            continue
        s = " ".join(s.split())
        if len(s) < 5:
            continue
        out.append((s, lineno))
    return out


def find_duplicate_blocks(files):
    """files: {path: text}. Returns findings for copy-pasted blocks."""
    occurrences = {}  # window hash -> [(path, index)]
    seqs = {}
    for path, text in files.items():
        seq = _norm_lines(text)
        seqs[path] = seq
        for j in range(len(seq) - WINDOW + 1):
            win = tuple(s for s, _ in seq[j:j + WINDOW])
            if sum(len(s) for s in win) < MIN_BLOCK_CHARS:
                continue
            occurrences.setdefault(hash(win), []).append((path, j))

    pairs = set()
    for locs in occurrences.values():
        if len(locs) < 2 or len(locs) > 8:  # >8 = boilerplate pattern, not worth reporting
            continue
        first = locs[0]
        for other in locs[1:]:
            if other[0] == first[0] and abs(other[1] - first[1]) < WINDOW:
                continue  # overlapping window in the same file
            pairs.add((first, other))

    findings = []
    reported = set()
    for (pa, ja), (pb, jb) in sorted(pairs):
        if any((pa, pb, ja - k, jb - k) in reported for k in range(1, WINDOW * 4)):
            continue  # continuation of a block we already reported
        length = WINDOW
        while ((pa, ja + length - WINDOW + 1), (pb, jb + length - WINDOW + 1)) in pairs:
            length += 1
        reported.add((pa, pb, ja, jb))
        a_start, a_end = seqs[pa][ja][1], seqs[pa][ja + length - 1][1]
        b_start = seqs[pb][jb][1]
        findings.append(Finding(
            "duplicate-code", "warn", pb, b_start,
            "~%d-line block duplicates %s:%d-%d — extract and reuse"
            % (a_end - a_start + 1, pa, a_start, a_end)))
        if len(findings) >= MAX_REPORTS:
            break
    return findings


def _argument_names(args):
    names = {
        arg.arg for arg in (
            list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs))
    }
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    return names


def _bound_names(fn):
    names = _argument_names(fn.args)
    global_names = set()
    nonlocal_names = set()

    def visit(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(child.name)
                continue
            if isinstance(child, ast.Lambda):
                continue
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                names.add(child.id)
            elif isinstance(child, ast.Import):
                names.update(a.asname or a.name.split(".")[0] for a in child.names)
            elif isinstance(child, ast.ImportFrom):
                names.update(a.asname or a.name for a in child.names if a.name != "*")
            elif isinstance(child, ast.ExceptHandler) and child.name:
                names.add(child.name)
            elif isinstance(child, ast.Global):
                global_names.update(child.names)
            elif isinstance(child, ast.Nonlocal):
                nonlocal_names.update(child.names)
            visit(child)

    visit(fn)
    names.difference_update(global_names | nonlocal_names)
    return names


class _Renamer(ast.NodeTransformer):
    def __init__(self):
        self.scopes = []

    def visit_Name(self, node):
        if self.scopes and node.id in self.scopes[-1]:
            node.id = "x"
        return self.generic_visit(node)

    def visit_arg(self, node):
        node.arg = "x"
        node.annotation = None
        return node

    def visit_FunctionDef(self, node):
        self.scopes.append(_bound_names(node))
        node.name = "x"
        node.returns = None
        node.decorator_list = []
        node = self.generic_visit(node)
        self.scopes.pop()
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        self.scopes.append(_bound_names(node))
        node = self.generic_visit(node)
        self.scopes.pop()
        return node


def _shape(fn):
    body = fn.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        fn.body = body[1:]
    fn = _Renamer().visit(fn)
    return ast.dump(fn, annotate_fields=False)


def find_duplicate_functions(py_files):
    """py_files: {path: text}. Structural duplicates among Python functions."""
    index = {}  # shape -> [(path, lineno, name)]
    for path, text in py_files.items():
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError):
            continue
        functions = [
            node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        names = {id(node): node.name for node in functions}
        for node in functions:
            name = names[id(node)]
            if name.startswith("__") and name.endswith("__"):
                continue  # dunders (esp. __init__) are boilerplate-shaped by nature
            n_stmts = sum(1 for d in ast.walk(node) if isinstance(d, ast.stmt))
            if n_stmts >= 5:  # includes the def itself; skip trivial bodies
                index.setdefault(_shape(node), []).append((path, node.lineno, name))

    findings = []
    for locs in index.values():
        if len(locs) < 2:
            continue
        first = locs[0]
        for path, lineno, name in locs[1:]:
            findings.append(Finding(
                "duplicate-function", "error", path, lineno,
                "%s() is structurally identical to %s() at %s:%d — reuse it instead"
                % (name, first[2], first[0], first[1])))
    return findings
