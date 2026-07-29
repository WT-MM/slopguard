"""Wire the slopguard hook into Claude Code / Codex CLI user-level config."""
import json
import os
import sys

CLAUDE_SETTINGS = os.path.expanduser("~/.claude/settings.json")
CODEX_CONFIG = os.path.expanduser("~/.codex/config.toml")
MARKER = "slopguard hooks"


def hook_command(agent):
    bin_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "bin", "slopguard")
    return "%s hook --agent %s" % (bin_path, agent)


def run_install(args):
    if args.agent == "claude":
        return install_claude()
    return install_codex()


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
    with open(CLAUDE_SETTINGS, "w") as fh:
        json.dump(settings, fh, indent=2)
        fh.write("\n")
    print("installed PostToolUse hook in %s" % CLAUDE_SETTINGS)
    return 0


CODEX_BLOCK = """
# --- {marker} (managed by `slopguard install codex`) ---
[[hooks.PostToolUse]]
matcher = "apply_patch"

[[hooks.PostToolUse.hooks]]
type = "command"
command = "{command}"
timeout = 30
statusMessage = "slopguard: checking edited files"

[[hooks.Stop]]

[[hooks.Stop.hooks]]
type = "command"
command = "{command}"
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
    block = CODEX_BLOCK.format(marker=MARKER, command=hook_command("codex"))
    with open(CODEX_CONFIG, "a") as fh:
        if existing and not existing.endswith("\n"):
            fh.write("\n")
        fh.write(block)
    print("appended hook config to %s" % CODEX_CONFIG)
    return 0
