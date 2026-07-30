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
        decorators = [self.visit(d) for d in node.decorator_list]
        node.decorator_list = []
        self.scopes.append(_bound_names(node))
        node.name = "x"
        node.returns = None
        node = self.generic_visit(node)
        self.scopes.pop()
        node.decorator_list = decorators
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


def find_duplicate_functions(py_files, report_files=None):
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
        locs.sort()
        context = [loc for loc in locs
                   if report_files is not None and loc[0] not in report_files]
        first = context[0] if context else locs[0]
        for path, lineno, name in locs:
            if (path, lineno, name) == first:
                continue
            if report_files is not None and path not in report_files:
                continue
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
_MAX_FULL_NAME_GROUP = 100
_LARGE_NAME_NEIGHBORS = 20


def _decorator_name(node):
    while isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _decorator_name(node.value)
        return ("%s.%s" % (base, node.attr)) if base else node.attr
    return ""


def _token_shingles(src):
    """Shingle-hash set over the function's tokens; literals normalized so
    edited constants don't mask sameness, identifiers kept so coincidentally
    shaped code doesn't fake it."""
    toks = []
    fstring_depth = 0
    try:
        for tok in tokenize.generate_tokens(io.StringIO(textwrap.dedent(src)).readline):
            token_name = tokenize.tok_name.get(tok.type, "")
            if token_name == "FSTRING_START":
                if not fstring_depth:
                    toks.append("s")
                fstring_depth += 1
            elif fstring_depth:
                if token_name == "FSTRING_END":
                    fstring_depth -= 1
            elif tok.type in (tokenize.NAME, tokenize.OP):
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
    """[(path, name, lineno, end_lineno, shingles, shape_digest, decorators)] for
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
            name = names[id(node)]
            if name.startswith("__") and name.endswith("__"):
                continue
            decorators = frozenset(
                name for name in (_decorator_name(d) for d in node.decorator_list)
                if name)
            if any(name.rsplit(".", 1)[-1] == "overload" for name in decorators):
                continue
            start = min(
                [node.lineno] + [d.lineno for d in node.decorator_list])
            end = getattr(node, "end_lineno", node.lineno)
            shingles = _token_shingles("".join(lines[start - 1:end]))
            if len(shingles) < DIVERGED_MIN_SHINGLES:
                continue
            shape = hashlib.blake2b(
                _shape(node).encode(), digest_size=8).digest()
            entries.append(
                (path, name, node.lineno, end, shingles, shape, decorators))
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
    indexed_sizes = [0] * len(entries)
    for ids in index.values():
        if len(ids) < 2 or len(ids) > _MAX_SHINGLE_DF:
            continue
        for i in ids:
            indexed_sizes[i] += 1
        for a in range(len(ids)):
            for b in range(a + 1, len(ids)):
                key = (ids[a], ids[b])
                counts[key] = counts.get(key, 0) + 1
    candidates = set()
    for (ia, ib), common in counts.items():
        smaller = min(indexed_sizes[ia], indexed_sizes[ib])
        if common >= DIVERGED_LOW * smaller * 0.75:  # cheap bound before exact
            candidates.add((ia, ib))

    # A copied family can push every shared shingle over the DF cap. Function
    # names are deliberately retained by this rule, so same-name functions
    # provide a strong, bounded fallback candidate source.
    by_name = {}
    for i, entry in enumerate(entries):
        by_name.setdefault(entry[1], []).append(i)
    for ids in by_name.values():
        ids.sort(key=lambda i: (len(entries[i][4]), entries[i][0], entries[i][2]))
        reach = len(ids) if len(ids) <= _MAX_FULL_NAME_GROUP \
            else _LARGE_NAME_NEIGHBORS + 1
        for pos, ia in enumerate(ids):
            for ib in ids[pos + 1:pos + reach]:
                candidates.add((min(ia, ib), max(ia, ib)))
    yield from sorted(candidates)


def _property_twins(a, b):
    if a[0] != b[0] or a[1] != b[1]:
        return False
    roles = {
        name.rsplit(".", 1)[-1] for name in a[6] | b[6]
    }
    return bool(roles & {"property", "cached_property", "getter", "setter", "deleter"})


def _one_to_one(group, same_file):
    """Greedily retain the strongest non-conflicting counterpart pairs."""
    selected = []
    used_a = set()
    used_b = set()
    used_same_file = set()
    for pair in group:
        a_id = (pair[1][0], pair[1][2])
        b_id = (pair[2][0], pair[2][2])
        if same_file:
            if a_id in used_same_file or b_id in used_same_file:
                continue
            used_same_file.update((a_id, b_id))
        else:
            if a_id in used_a or b_id in used_b:
                continue
            used_a.add(a_id)
            used_b.add(b_id)
        selected.append(pair)
    return selected


def _multi_file_families(pairs, report_files=None):
    """Collapse connected near-duplicate families spanning 3+ files."""
    parent = {}

    def identity(entry):
        return entry[0], entry[2]

    def find(item):
        parent.setdefault(item, item)
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(a, b):
        a = find(a)
        b = find(b)
        if a != b:
            parent[max(a, b)] = min(a, b)

    entries = {}
    for _jaccard, a, b in pairs:
        a_id = identity(a)
        b_id = identity(b)
        entries[a_id] = a
        entries[b_id] = b
        union(a_id, b_id)

    groups = {}
    for item in entries:
        groups.setdefault(find(item), set()).add(item)
    family_roots = {
        root for root, items in groups.items()
        if len(items) >= 3 and len({entries[item][0] for item in items}) >= 3
    }

    findings = []
    for root in sorted(family_roots):
        items = sorted(groups[root])
        members = [entries[item] for item in items]
        edges = [
            pair for pair in pairs
            if find(identity(pair[1])) == root and find(identity(pair[2])) == root
        ]
        average = sum(pair[0] for pair in edges) / len(edges)
        names = ", ".join(sorted({entry[1] for entry in members})[:4])
        report_members = [
            entry for entry in members
            if report_files is not None and entry[0] in report_files
        ]
        target = report_members[-1] if report_members else members[-1]
        if all(pair[0] >= DIVERGED_PARAM for pair in edges):
            message = (
                "%d functions (%s) across %d files are identical except for literal "
                "values — consolidate and parameterize the shared implementation"
                % (len(members), names, len({entry[0] for entry in members})))
        else:
            message = (
                "%d functions (%s) across %d files form a ~%d%% near-duplicate "
                "family — consolidate the fork so fixes cannot diverge"
                % (len(members), names, len({entry[0] for entry in members}),
                   average * 100))
        findings.append(Finding(
            "diverged-duplicate", "warn", target[0], target[2], message))

    remaining = [
        pair for pair in pairs
        if find(identity(pair[1])) not in family_roots
    ]
    return findings, remaining


def find_diverged_duplicates(py_files, report_files=None):
    """Near-duplicate function pairs: diverged copies that exact matching misses."""
    entries = _function_entries(py_files)
    pairs = []
    for ia, ib in _candidate_pairs(entries):
        A, B = entries[ia], entries[ib]
        if _nested(A, B) or _property_twins(A, B) or A[5] == B[5]:
            continue  # structural duplicates belong to find_duplicate_functions
        jaccard = len(A[4] & B[4]) / len(A[4] | B[4])
        if jaccard >= DIVERGED_LOW:
            if report_files is not None \
                    and A[0] in report_files and B[0] not in report_files:
                A, B = B, A
            pairs.append((jaccard, A, B))

    findings, pairs = _multi_file_families(pairs, report_files=report_files)
    by_file_pair = {}
    for jaccard, A, B in pairs:
        by_file_pair.setdefault((A[0], B[0]), []).append((jaccard, A, B))

    for (file_a, file_b), group in sorted(by_file_pair.items()):
        group.sort(key=lambda p: (-p[0], p[1][2], p[2][2], p[1][1], p[2][1]))
        group = _one_to_one(group, file_a == file_b)
        if file_a != file_b and len(group) >= _AGGREGATE_AT:
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
    return findings
