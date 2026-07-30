"""Wire the slopguard hook into Claude Code / Codex CLI user-level config."""
import contextlib
import json
import os
import shlex
import shutil
import stat
import sys
import tempfile

CLAUDE_SETTINGS = os.path.expanduser("~/.claude/settings.json")
CODEX_CONFIG = os.path.expanduser("~/.codex/config.toml")
MARKER = "slopguard hooks"


def hook_command(agent):
    # A pip-installed console script survives repo moves; fall back to the
    # in-repo launcher for source checkouts.
    exe = shutil.which("slopguard")
    if not exe:
        exe = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "bin", "slopguard")
    return "%s hook --agent %s" % (shlex.quote(exe), shlex.quote(agent))


def run_install(args):
    if args.agent == "claude":
        return install_claude()
    return install_codex()


def _atomic_write(path, content):
    directory = os.path.dirname(path) or "."
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        mode = None
    fd, temp_path = tempfile.mkstemp(prefix=".slopguard-", dir=directory)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(temp_path)
        raise


def install_claude():
    try:
        with open(CLAUDE_SETTINGS) as fh:
            settings = json.load(fh)
    except FileNotFoundError:
        settings = {}
    except ValueError as e:
        print("refusing to touch %s: does not parse as JSON (%s)" % (CLAUDE_SETTINGS, e),
              file=sys.stderr)
        return 1

    command = hook_command("claude")
    hooks = settings.setdefault("hooks", {})
    post = hooks.setdefault("PostToolUse", [])
    for entry in post:
        for h in entry.get("hooks", []):
            if "slopguard" in h.get("command", ""):
                print("already installed in %s" % CLAUDE_SETTINGS)
                return 0
    post.append({
        "matcher": "Edit|Write|MultiEdit|NotebookEdit",
        "hooks": [{"type": "command", "command": command, "timeout": 30}],
    })
    _atomic_write(CLAUDE_SETTINGS, json.dumps(settings, indent=2) + "\n")
    print("installed PostToolUse hook in %s" % CLAUDE_SETTINGS)
    return 0


CODEX_BLOCK = """
# --- {marker} (managed by `slopguard install codex`) ---
[[hooks.PostToolUse]]
matcher = "apply_patch"

[[hooks.PostToolUse.hooks]]
type = "command"
command = {command}
timeout = 30
statusMessage = "slopguard: checking edited files"

[[hooks.Stop]]

[[hooks.Stop.hooks]]
type = "command"
command = {command}
timeout = 60
statusMessage = "slopguard: scanning changed files"
# --- end {marker} ---
"""


def install_codex():
    try:
        with open(CODEX_CONFIG) as fh:
            existing = fh.read()
    except FileNotFoundError:
        existing = ""
    if MARKER in existing:
        print("already installed in %s" % CODEX_CONFIG)
        return 0
    block = CODEX_BLOCK.format(
        marker=MARKER, command=json.dumps(hook_command("codex")))
    separator = "\n" if existing and not existing.endswith("\n") else ""
    _atomic_write(CODEX_CONFIG, existing + separator + block)
    print("appended hook config to %s" % CODEX_CONFIG)
    return 0
