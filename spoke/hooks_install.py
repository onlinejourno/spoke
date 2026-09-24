"""Installing the two hook scripts into a Claude Code settings file.

The hooks were written, tested (7 tests in tests/test_hooks.py) and then
reachable by nobody: no install command beside `serve --install` and
`mcp --install`, and no mention in the README. A surfacing mechanism that
cannot be turned on surfaces nothing, which is this project's own failure
mode wearing its own uniform.

`jq` is checked here rather than assumed. Both scripts parse their stdin
payload with it, and a hook that cannot run reports nothing -- silence
that looks exactly like "nothing to report".
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

#: Which hook event each script answers to.
HOOKS: dict[str, str] = {
    "SessionStart": "ledger-preamble.sh",
    "Stop": "ledger-nag.sh",
}

#: The scripts ship INSIDE the package. They lived beside it, in the
#: repo, and an installed wheel had no such directory: `hooks --install`
#: would have registered paths that did not exist. Found by building the
#: wheel and looking, not by any test.
HOOK_DIR = Path(__file__).resolve().parent / "hooks"

DEFAULT_SETTINGS = Path.home() / ".claude" / "settings.json"


def hook_command(script: str) -> str:
    return str(HOOK_DIR / script)


def spoke_bin() -> str:
    """The `spoke` executable beside the interpreter running now.

    A hook runs under launchd's or the client's PATH, not the shell's,
    so `spoke` bare is usually not found there -- the script then says
    "binary not found" at the top of every session. Passing SPOKE_BIN
    explicitly is what makes the hook work outside an activated venv.
    """
    import sys

    exe = Path(sys.executable).parent / "spoke"
    return str(exe) if exe.exists() else "spoke"


STATUS = {"SessionStart": "Checking ledger...", "Stop": "Checking ledger hygiene..."}


def hook_entry(event: str, script: str) -> dict:
    """One registration, in the shape a hand-written one already had:
    the env the script needs, a timeout so a hung store cannot hang a
    session start, and a status line so the wait is not silent."""
    return {
        "type": "command",
        "command": f"SPOKE_BIN={spoke_bin()} {hook_command(script)}",
        "timeout": 10,
        "statusMessage": STATUS[event],
    }


def is_ours(command: str, script: str) -> bool:
    """Whether a registered command runs this script -- by the script's
    path appearing in it, NOT by string equality. A registration written
    by hand carries an env prefix and would otherwise be unrecognised,
    and `--install` would append a second, poorer copy beneath it.
    Found by pointing the dry run at a real settings file."""
    return hook_command(script) in command


def missing_scripts() -> list[str]:
    return [s for s in HOOKS.values() if not (HOOK_DIR / s).exists()]


def read_settings(path: Path) -> dict:
    """The settings file as a dict, or an empty one when it does not exist.

    A malformed file is an error rather than an empty default: silently
    replacing a settings file that failed to parse would discard every
    other hook and setting in it.
    """
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def plan(settings: dict) -> dict:
    """`settings` with this project's hooks added, leaving every other
    hook alone. Idempotent: installing twice adds nothing the second
    time."""
    out = json.loads(json.dumps(settings))  # deep copy, plain data
    hooks = out.setdefault("hooks", {})
    for event, script in HOOKS.items():
        entries = hooks.setdefault(event, [])
        if any(
            is_ours(h.get("command", ""), script)
            for entry in entries
            for h in entry.get("hooks", [])
        ):
            continue
        entries.append({"hooks": [hook_entry(event, script)]})
    return out


def remove(settings: dict) -> dict:
    """`settings` with only this project's hooks taken out."""
    out = json.loads(json.dumps(settings))
    hooks = out.get("hooks") or {}
    for event in list(hooks):
        kept = []
        for entry in hooks[event]:
            inner = [h for h in entry.get("hooks", [])
                     if not any(is_ours(h.get("command", ""), s) for s in HOOKS.values())]
            if inner:
                kept.append({**entry, "hooks": inner})
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        out.pop("hooks", None)
    return out


def installed(settings: dict) -> dict[str, bool]:
    """Which of this project's hooks the settings actually register."""
    commands = [
        h.get("command", "")
        for entries in (settings.get("hooks") or {}).values()
        for entry in entries
        for h in entry.get("hooks", [])
    ]
    return {e: any(is_ours(c, s) for c in commands) for e, s in HOOKS.items()}


def jq_path() -> str | None:
    return shutil.which("jq")
