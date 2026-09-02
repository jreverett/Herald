#!/usr/bin/env python3
"""Wire the tray's activity hooks into every Claude Code and Codex profile found.

The tray shows amber while an agent is working on herald's own work, which needs
a signal for when a turn starts and ends. Both editors emit that as hooks, and a
hook fires whether or not the model remembers to report anything - so this is
wired to the harness, never to the agent.

Run by install.sh, and safe to run again: entries are matched by their command,
existing hooks from other tools are left alone, and nothing is written when the
herald entries are already present.
"""
import json
import os
import sys
from pathlib import Path

# Claude Code and Codex use the same event names and the same payload fields.
# Notification is deliberately absent: it fires for a session idle at an empty
# prompt as well as for a permission prompt, and neither is herald's work.
EVENTS = {
    "PostToolUse": "working",
    "UserPromptSubmit": "working",
    "Stop": "idle",
    "SessionEnd": "idle",
}
MARKER = "herald activity"


def herald_command():
    """The herald entry point a hook shell can find. A hook does not run through
    a login shell, so PATH cannot be assumed."""
    wrapper = Path.home() / ".local" / "bin" / "herald"
    if wrapper.exists():
        return str(wrapper)
    return "herald"


def hook_entry(state, command):
    # Output is discarded because an editor can feed hook stdout back to the
    # model, and a non-zero exit must never fail the tool call that ran it.
    # No tool matcher: every tool call means the turn is live, and an editor that
    # does not understand the key would drop the entry.
    return {"hooks": [{"type": "command",
                       "command": f"{command} activity {state} >/dev/null 2>&1 || true"}]}


def merge_hooks(path, command):
    """Add the herald entries to one hooks object, keeping everything else."""
    try:
        config = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError):
        return None
    if not isinstance(config, dict):
        return None
    hooks = config.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        return None
    added = []
    for event, state in EVENTS.items():
        existing = hooks.setdefault(event, [])
        if not isinstance(existing, list):
            continue
        if any(MARKER in json.dumps(group) for group in existing):
            continue
        existing.append(hook_entry(state, command))
        added.append(event)
    if not added:
        return []
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=2) + "\n")
    except OSError:
        return None
    return added


def enable_codex_hooks(config_path):
    """Codex only reads hooks.json when the feature is on."""
    try:
        text = config_path.read_text() if config_path.exists() else ""
    except OSError:
        return
    lines = text.splitlines()
    if any(line.strip() == "[features]" for line in lines):
        start = next(i for i, line in enumerate(lines) if line.strip() == "[features]")
        end = next((i for i in range(start + 1, len(lines))
                    if lines[i].strip().startswith("[")), len(lines))
        if any(line.strip().replace(" ", "").startswith("hooks=")
               for line in lines[start + 1:end]):
            return
        lines.insert(start + 1, "hooks = true")
    else:
        lines += ["", "[features]", "hooks = true"]
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("\n".join(lines) + "\n")
    except OSError:
        pass


def profiles():
    """(label, hooks file, config.toml or None) for each profile that exists."""
    home = Path.home()
    found = []
    claude_dirs = [home / ".claude", *sorted(home.glob(".claude-*")),
                   *sorted(home.glob(".claude_*"))]
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        claude_dirs.append(Path(os.environ["CLAUDE_CONFIG_DIR"]))
    for d in claude_dirs:
        if d.is_dir():
            found.append(("Claude Code", d / "settings.json", None))
    codex_dirs = [home / ".codex", *sorted(home.glob(".codex-*")),
                  *sorted(home.glob(".codex_*"))]
    if os.environ.get("CODEX_HOME"):
        codex_dirs.append(Path(os.environ["CODEX_HOME"]))
    for d in codex_dirs:
        if d.is_dir():
            found.append(("Codex", d / "hooks.json", d / "config.toml"))
    seen, unique = set(), []
    for label, hooks_path, config_path in found:
        if hooks_path in seen:
            continue
        seen.add(hooks_path)
        unique.append((label, hooks_path, config_path))
    return unique


def main():
    command = herald_command()
    wired = False
    for label, hooks_path, config_path in profiles():
        added = merge_hooks(hooks_path, command)
        if added is None:
            print(f"Could not update {hooks_path} - add the herald activity hooks by hand")
            continue
        if config_path is not None:
            enable_codex_hooks(config_path)
        if added:
            print(f"{label} tray hooks added to {hooks_path} ({', '.join(added)})")
            wired = True
        else:
            print(f"{label} tray hooks already present in {hooks_path}")
    if wired and any(label == "Codex" for label, _, _ in profiles()):
        print("Codex skips a hook it has not been trusted with, and records the trust as a "
              "hash per hook in config.toml. Start Codex once and approve the prompt, or the "
              "tray will never see a Codex turn.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
