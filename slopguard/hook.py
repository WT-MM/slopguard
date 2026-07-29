"""Hook adapter for Claude Code and OpenAI Codex CLI.

Both agents speak the same protocol: a JSON event on stdin; exit code 2 with
text on stderr feeds the findings back to the model as blocking feedback.

Designed to fail OPEN: any internal error exits 0/1 (non-blocking) so a bug
here never bricks the agent's editing loop.
"""
import hashlib
import json
import os
import subprocess
import sys
from fnmatch import fnmatch

from .cli import analyze, is_checkable, load_config, read_texts
from .findings import at_or_above, sort_findings

MAX_TARGET_FILES = 40
MAX_CONTEXT_FILES = 30
MAX_REPORT_LINES = 30
STATE_DIR = os.environ.get(
    "SLOPGUARD_STATE_DIR", os.path.expanduser("~/.cache/slopguard"))


def run_hook(args):
    try:
        return _run(args)
    except Exception as e:  # never brick the agent on our own bugs
        print("slopguard hook internal error (non-blocking): %r" % e, file=sys.stderr)
        return 1


def _run(args):
    if os.environ.get("SLOPGUARD_DISABLE"):
        return 0
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    if not isinstance(event, dict):
        return 0
    if event.get("stop_hook_active"):
        return 0  # we already blocked this turn once; don't loop

    cwd = event.get("cwd") or os.getcwd()
    event_name = event.get("hook_event_name", "")

    targets = _files_from_tool_input(event.get("tool_input"), cwd)
    if not targets and (event_name.startswith("Stop") or not event_name):
        targets = _git_changed_files(cwd)
    cfg = load_config(cwd)
    targets = _filter_excluded(targets, cfg)[:MAX_TARGET_FILES]
    if not targets:
        return 0

    texts = read_texts(targets + _context_files(targets))
    findings = analyze(texts, cfg, report_files=set(targets))
    blocking = at_or_above(findings, "warn")
    if not blocking:
        return 0

    if event_name.startswith("Stop") and _already_reported(event, blocking):
        return 0  # same findings as last Stop of this session; don't loop

    lines = [f.format() for f in sort_findings(blocking)]
    shown = lines[:MAX_REPORT_LINES]
    header = ("slopguard found %d issue(s) in the files you just changed. "
              "Fix them, or add a `slopguard:ignore` comment on the flagged "
              "line if it is genuinely intentional:" % len(blocking))
    body = header + "\n" + "\n".join(shown)
    if len(lines) > len(shown):
        body += "\n... and %d more (run `slopguard scan` for the full list)" % (len(lines) - len(shown))
    print(body, file=sys.stderr)
    return 2


def _files_from_tool_input(tool_input, cwd):
    """Pull real file paths out of whatever shape the agent's tool input has."""
    found = []

    def add_path(value):
        value = value.strip()
        if not value or len(value) > 500:
            return False
        candidate = value if os.path.isabs(value) else os.path.join(cwd, value)
        if os.path.isfile(candidate) and is_checkable(candidate):
            found.append(os.path.abspath(candidate))
            return True
        return False

    def visit(value):
        if isinstance(value, str):
            if add_path(value):
                return
            for token in value.splitlines()[:200]:
                token = token.strip().lstrip("+-*").strip()
                is_patch_path = token.startswith(
                    ("Update File:", "Add File:", "Move to:"))
                if is_patch_path:
                    token = token.split(":", 1)[1].strip()  # Codex apply_patch headers
                if " " in token and not is_patch_path:
                    continue
                add_path(token)
        elif isinstance(value, dict):
            for v in value.values():
                visit(v)
        elif isinstance(value, list):
            for v in value[:50]:
                visit(v)

    visit(tool_input)
    seen, unique = set(), []
    for f in found:
        if f not in seen:
            seen.add(f)
            unique.append(f)
    return unique


def _filter_excluded(targets, cfg):
    patterns = cfg.get("hook_exclude", [])
    if isinstance(patterns, str):
        patterns = [patterns]
    if not isinstance(patterns, (list, tuple)):
        return targets
    return [
        path for path in targets
        if not any(fnmatch(os.path.abspath(path), pattern) for pattern in patterns)
    ]


def _git_changed_files(cwd):
    try:
        root_result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=cwd,
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if root_result.returncode != 0:
        return []
    root = root_result.stdout.strip()
    if not root:
        return []

    def git_output(cmd):
        try:
            return subprocess.run(
                cmd, cwd=root, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None

    diff = git_output(["git", "diff", "--name-only", "HEAD"])
    if diff is None:
        return []
    if diff.returncode == 0:
        results = [diff]
    else:
        # An unborn repository has no HEAD. Its staged files are not
        # "untracked", so compare both the index and working tree directly.
        results = [
            git_output(["git", "diff", "--name-only", "--cached"]),
            git_output(["git", "diff", "--name-only"]),
        ]
    results.append(git_output(
        ["git", "ls-files", "--others", "--exclude-standard"]))

    files = []
    for out in results:
        if out is None:
            return []
        if out.returncode != 0:
            continue
        for rel in out.stdout.splitlines():
            path = os.path.join(root, rel.strip())
            if os.path.isfile(path) and is_checkable(path):
                files.append(os.path.abspath(path))
    return files


def _context_files(targets):
    """Sibling same-extension files, so duplicate detection sees neighbors."""
    context = []
    seen = set(targets)
    for t in targets:
        ext = os.path.splitext(t)[1]
        try:
            names = sorted(os.listdir(os.path.dirname(t)))
        except OSError:
            continue
        for name in names:
            sibling = os.path.join(os.path.dirname(t), name)
            if sibling in seen or not name.endswith(ext):
                continue
            if os.path.isfile(sibling) and is_checkable(sibling):
                context.append(sibling)
                seen.add(sibling)
            if len(context) >= MAX_CONTEXT_FILES:
                return context
    return context


def _already_reported(event, blocking):
    """Loop guard for Stop hooks: block once per distinct finding set."""
    session = str(event.get("session_id", "nosession"))
    digest = hashlib.sha1(
        "\n".join(sorted(f.format() for f in blocking)).encode()).hexdigest()
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        state_file = os.path.join(
            STATE_DIR, "stop-%s" % hashlib.sha1(session.encode()).hexdigest()[:16])
        with open(state_file) as fh:
            previous = fh.read().strip()
    except FileNotFoundError:
        previous = None
    except OSError:
        return True  # cannot guarantee a loop guard, so fail open
    if previous == digest:
        return True
    try:
        with open(state_file, "w") as fh:
            fh.write(digest)
    except OSError:
        return True
    return False
