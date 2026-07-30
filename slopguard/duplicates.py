"""Duplicate-code detection.

Three passes:
1. Language-agnostic: hash sliding windows of normalized lines across all
   files; catches copy-paste blocks (the dominant agent failure mode).
2. Python-only: structural function comparison with identifiers normalized;
   catches "wrote the same helper again with different names".
3. Python-only: NEAR-duplicate functions via token-shingle Jaccard;
   catches diverged copies — forks where fixes landed on one side only,
   which exact matching is structurally blind to.
"""
import ast
import hashlib
import io
import textwrap
import tokenize

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


# ------------------------------------------------------------- near-duplicates

DIVERGED_LOW = 0.60    # below this, similarity is coincidence
DIVERGED_PARAM = 0.98  # at/above this the code is identical except literals
DIVERGED_MIN_SHINGLES = 40  # substantial functions only
_SHINGLE_W = 5
_MAX_SHINGLE_DF = 20   # shingles in >20 functions are boilerplate, not identity
_AGGREGATE_AT = 5      # this many pairs across one file pair = report the fork
_MAX_NEARDUP_FUNCTIONS = 10000


def _token_shingles(src):
    """Shingle-hash set over the function's tokens; literals normalized so
    edited constants don't mask sameness, identifiers kept so coincidentally
    shaped code doesn't fake it."""
    toks = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(textwrap.dedent(src)).readline):
            if tok.type in (tokenize.NAME, tokenize.OP):
                toks.append(tok.string)
            elif tok.type == tokenize.NUMBER:
                toks.append("0")
            elif tok.type == tokenize.STRING:
                toks.append("s")
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return frozenset()
    shingles = set()
    for i in range(len(toks) - _SHINGLE_W + 1):
        digest = hashlib.blake2b(
            "\x00".join(toks[i:i + _SHINGLE_W]).encode(), digest_size=8).digest()
        shingles.add(int.from_bytes(digest, "big"))
    return frozenset(shingles)


def _function_entries(py_files):
    """[(path, name, lineno, end_lineno, shingles, shape_digest)] for
    substantial functions. shape_digest identifies structural duplicates so
    pairs owned by find_duplicate_functions aren't double-reported."""
    entries = []
    for path, text in sorted(py_files.items()):
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError):
            continue
        lines = text.splitlines(keepends=True)
        functions = [node for node in ast.walk(tree)
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        # _shape mutates nodes in place (renames the whole subtree), so
        # capture every function's identity before shaping any of them.
        names = {id(node): node.name for node in functions}
        for node in functions:
            end = getattr(node, "end_lineno", node.lineno)
            shingles = _token_shingles("".join(lines[node.lineno - 1:end]))
            if len(shingles) < DIVERGED_MIN_SHINGLES:
                continue
            shape = hashlib.blake2b(
                _shape(node).encode(), digest_size=8).digest()
            entries.append((path, names[id(node)], node.lineno, end, shingles, shape))
            if len(entries) >= _MAX_NEARDUP_FUNCTIONS:
                return entries
    return entries


def _nested(a, b):
    return a[0] == b[0] and (
        (a[2] <= b[2] and b[3] <= a[3]) or (b[2] <= a[2] and a[3] <= b[3]))


def _candidate_pairs(entries):
    index = {}
    for i, entry in enumerate(entries):
        for h in entry[4]:
            index.setdefault(h, []).append(i)
    counts = {}
    for ids in index.values():
        if len(ids) < 2 or len(ids) > _MAX_SHINGLE_DF:
            continue
        for a in range(len(ids)):
            for b in range(a + 1, len(ids)):
                key = (ids[a], ids[b])
                counts[key] = counts.get(key, 0) + 1
    for (ia, ib), common in counts.items():
        smaller = min(len(entries[ia][4]), len(entries[ib][4]))
        if common >= DIVERGED_LOW * smaller * 0.75:  # cheap bound before exact
            yield ia, ib


def find_diverged_duplicates(py_files):
    """Near-duplicate function pairs: diverged copies that exact matching misses."""
    entries = _function_entries(py_files)
    pairs = []
    for ia, ib in _candidate_pairs(entries):
        A, B = entries[ia], entries[ib]
        if _nested(A, B) or A[5] == B[5]:
            continue  # structural duplicates belong to find_duplicate_functions
        jaccard = len(A[4] & B[4]) / len(A[4] | B[4])
        if jaccard >= DIVERGED_LOW:
            pairs.append((jaccard, A, B))

    by_file_pair = {}
    for jaccard, A, B in pairs:
        by_file_pair.setdefault((A[0], B[0]), []).append((jaccard, A, B))

    findings = []
    for (file_a, file_b), group in sorted(by_file_pair.items()):
        group.sort(key=lambda p: -p[0])
        if len(group) >= _AGGREGATE_AT:
            avg = sum(p[0] for p in group) / len(group)
            names = ", ".join(p[2][1] for p in group[:4])
            findings.append(Finding(
                "diverged-duplicate", "warn", file_b, group[0][2][2],
                "%d functions here (%s, …) are ~%d%% identical to counterparts in %s "
                "— a diverged fork: fixes land on one side and silently miss the other"
                % (len(group), names, avg * 100, file_a)))
            continue
        for jaccard, A, B in group:
            if jaccard >= DIVERGED_PARAM:
                message = ("%s() is identical to %s() at %s:%d except for literal "
                           "values — parameterize one function instead of copying"
                           % (B[1], A[1], A[0], A[2]))
            else:
                message = ("%s() is ~%d%% identical to %s() at %s:%d — diverged copy? "
                           "unify them or document why the fork is intentional"
                           % (B[1], jaccard * 100, A[1], A[0], A[2]))
            findings.append(Finding("diverged-duplicate", "warn", B[0], B[2], message))
    return findings[:MAX_REPORTS]
