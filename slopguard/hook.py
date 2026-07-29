"""Hook adapter for Claude Code and OpenAI Codex CLI.

Both agents speak the same protocol: a JSON event on stdin; exit code 2 with
text on stderr feeds the findings back to the model as blocking feedback.

Designed to fail OPEN: any internal error exits 0/1 (non-blocking) so a bug
here never bricks the agent's editing loop.
"""
import contextlib
import hashlib
import json
import os
import subprocess
import sys

from .cli import analyze, is_checkable, load_config, read_texts
from .findings import at_or_above, sort_findings

MAX_TARGET_FILES = 40
MAX_CONTEXT_FILES = 30
MAX_REPORT_LINES = 30
STATE_DIR = os.path.expanduser("~/.cache/slopguard")


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
    targets = [t for t in targets if "/fixtures/" not in t][:MAX_TARGET_FILES]
    if not targets:
        return 0

    cfg = load_config(cwd)
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

    def visit(value):
        if isinstance(value, str):
            for token in value.splitlines()[:200]:
                token = token.strip().lstrip("+-*").strip()
                if token.startswith("Update File:") or token.startswith("Add File:"):
                    token = token.split(":", 1)[1].strip()  # Codex apply_patch headers
                if not token or len(token) > 500 or " " in token:
                    continue
                candidate = token if os.path.isabs(token) else os.path.join(cwd, token)
                if os.path.isfile(candidate) and is_checkable(candidate):
                    found.append(os.path.abspath(candidate))
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


def _git_changed_files(cwd):
    files = []
    for cmd in (["git", "diff", "--name-only", "HEAD"],
                ["git", "ls-files", "--others", "--exclude-standard"]):
        try:
            out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return []
        if out.returncode != 0:
            continue
        for rel in out.stdout.splitlines():
            path = os.path.join(cwd, rel.strip())
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
    os.makedirs(STATE_DIR, exist_ok=True)
    state_file = os.path.join(STATE_DIR, "stop-%s" % hashlib.sha1(session.encode()).hexdigest()[:16])
    previous = None
    with contextlib.suppress(OSError):
        with open(state_file) as fh:
            previous = fh.read().strip()
    if previous == digest:
        return True
    with contextlib.suppress(OSError):
        with open(state_file, "w") as fh:
            fh.write(digest)
    return False
