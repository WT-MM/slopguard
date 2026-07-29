"""Duplicate-code detection.

Two passes:
1. Language-agnostic: hash sliding windows of normalized lines across all
   files; catches copy-paste blocks (the dominant agent failure mode).
2. Python-only: structural function comparison with identifiers normalized;
   catches "wrote the same helper again with different names".
"""
import ast
import copy

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


class _Renamer(ast.NodeTransformer):
    def visit_Name(self, node):
        node.id = "x"
        return self.generic_visit(node)

    def visit_arg(self, node):
        node.arg = "x"
        node.annotation = None
        return node

    def visit_FunctionDef(self, node):
        node.name = "x"
        node.returns = None
        node.decorator_list = []
        return self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef


def _shape(fn):
    fn = copy.deepcopy(fn)
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
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name.startswith("__") and node.name.endswith("__"):
                continue  # dunders (esp. __init__) are boilerplate-shaped by nature
            n_stmts = sum(1 for d in ast.walk(node) if isinstance(d, ast.stmt))
            if n_stmts >= 5:  # includes the def itself; skip trivial bodies
                index.setdefault(_shape(node), []).append((path, node.lineno, node.name))

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
