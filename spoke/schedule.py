"""Turns `doctor` from something a human has to remember to run into a
launchd agent macOS runs on its own -- and gives `--status` enough to tell
"installed but has never fired" apart from "ran and found nothing", which
is the whole point: a watcher indistinguishable from silence is the exact
failure doctor.py's own docstring names one layer down.

Every path this module writes into a plist is resolved to an absolute path
at install time (`Path.resolve()`), because launchd runs agents with none
of an interactive shell's assumptions about the current directory or PATH.

No estate identity here: a real install's launchd Label reflects whoever
is running it (e.g. a reverse-DNS name for the org that owns the
machine), but that name may never appear in the shipped package itself
(see tests/test_no_estate_identity.py -- it scans this whole tree,
literal file contents included, for exactly that). DEFAULT_LABEL_PREFIX
is therefore a neutral placeholder; the real prefix is supplied at
install time via --label-prefix or SPOKE_LAUNCHD_LABEL_PREFIX, never
baked into source.

`install()` also writes a small install RECORD (label, project, plist
path) next to the doctor state, because DEFAULT_LABEL_PREFIX is not what
most real installs actually use -- a machine that already has a
reverse-DNS convention installs under its own prefix. Without the record,
`--status`/`--uninstall` run with no flag fall back to guessing the
default prefix, and report "not installed" for an agent that is loaded
and running under a different label -- a status command that cannot see
its own agent, which is precisely the silence-indistinguishable-from-
health failure this whole tool exists to remove. `--status`/`--uninstall`
read the record first; an explicit --label-prefix still overrides it.
"""
from __future__ import annotations
import json
import os
import plistlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .projects import load_registry, select_project

DEFAULT_LABEL_PREFIX = "com.local"

# 08:30: late enough that a laptop closed overnight is almost always open
# and awake again by then (a missed StartCalendarInterval fires as soon as
# the machine wakes, but a time during the working day makes that fallback
# unnecessary most days), early enough to land before the day's memory
# work starts rather than after.
DEFAULT_HOUR = 8
DEFAULT_MINUTE = 30


def label_for(prefix: str) -> str:
    return f"{prefix}.spoke"


def serve_label_for(prefix: str) -> str:
    """A DIFFERENT label from the daily doctor run. One label for two
    agents would have the second install silently replace the first, and
    the daily check would stop with nothing saying so."""
    return f"{prefix}.spoke.serve"


def launch_agents_dir() -> Path:
    override = os.environ.get("SPOKE_LAUNCH_AGENTS_DIR")
    if override:
        return Path(override).expanduser()
    return Path("~/Library/LaunchAgents").expanduser()


def default_log_dir() -> Path:
    override = os.environ.get("SPOKE_SCHEDULE_LOG_DIR")
    if override:
        return Path(override).expanduser()
    return Path("~/Library/Logs/spoke").expanduser()


def default_state_dir() -> Path:
    override = os.environ.get("SPOKE_SCHEDULE_STATE_DIR")
    if override:
        return Path(override).expanduser()
    return Path("~/.claude/spoke/schedule").expanduser()


def plist_path(label: str, agents_dir: Path | None = None) -> Path:
    d = agents_dir if agents_dir is not None else launch_agents_dir()
    return d / f"{label}.plist"


def install_record_path(project: str, state_dir: Path | None = None) -> Path:
    d = state_dir if state_dir is not None else default_state_dir()
    return d / f"{project}.install.json"


@dataclass(frozen=True)
class ScheduleResult:
    ok: bool
    message: str
    plist_path: Path | None = None
    loaded: bool | None = None


def _run_launchctl(args: list[str]) -> subprocess.CompletedProcess:
    """The real implementation, used only when the CLI actually runs on a
    machine with launchd -- every test injects a fake instead, since
    ubuntu-latest CI has no `launchctl` binary at all."""
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _spoke_bin() -> Path:
    """Absolute path to the installed `spoke` console script, next
    to the interpreter actually running this process. launchd's own PATH
    is minimal and does not include a project virtualenv, so the plist
    must reference a real absolute binary rather than a bare name.

    Deliberately `Path(sys.executable)`, NOT `.resolve()`: a venv's
    bin/python is itself a symlink chain out to the real interpreter (e.g.
    Homebrew's Python framework), and following it lands in a directory
    that has no `spoke` console script at all -- only the venv's
    own bin/ does. sys.executable is already absolute, so no resolution
    is needed to get an absolute path; resolving it here is actively
    wrong (verified against a real install: `.resolve()` produced
    .../Python.framework/Versions/3.14/bin/spoke, which does not
    exist, instead of the venv's own bin/spoke, which does)."""
    return Path(sys.executable).parent / "spoke"


def build_plist(
    label: str,
    spoke_bin: Path,
    project: str,
    config_path: Path,
    state_path: Path,
    log_path: Path,
    hour: int,
    minute: int,
    path_env: str,
) -> bytes:
    data = {
        "Label": label,
        "ProgramArguments": [
            str(spoke_bin), "doctor",
            "--project", project,
            "--config", str(config_path),
            "--state", str(state_path),
        ],
        "EnvironmentVariables": {"PATH": path_env},
        "StartCalendarInterval": [{"Hour": hour, "Minute": minute}],
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }
    return plistlib.dumps(data, fmt=plistlib.FMT_XML)


def build_serve_plist(
    label: str,
    spoke_bin: Path,
    project: str,
    config_path: Path,
    log_path: Path,
    port: int,
    path_env: str,
) -> bytes:
    """A launchd agent that keeps the local app running.

    `KeepAlive` rather than `RunAtLoad` alone: the point of a launcher is
    that the app is there when you go looking for it, and a server that
    died at 3am and stayed dead is exactly the silent failure this whole
    project is about.

    The port is written in explicitly. Left implicit it would be whatever
    the default happened to be on the day the plist was written, and
    `--status` would then be checking a different port from the one the
    agent serves.
    """
    return plistlib.dumps({
        "Label": label,
        "ProgramArguments": [
            str(spoke_bin), "serve",
            "--project", project,
            "--config", str(config_path),
            "--port", str(port),
        ],
        "EnvironmentVariables": {"PATH": path_env},
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }, fmt=plistlib.FMT_XML)


def install(
    registry_path: Path,
    project_name: str | None,
    config_path: Path,
    *,
    label_prefix: str = DEFAULT_LABEL_PREFIX,
    hour: int = DEFAULT_HOUR,
    minute: int = DEFAULT_MINUTE,
    agents_dir: Path | None = None,
    state_dir: Path | None = None,
    run_launchctl=None,
) -> ScheduleResult:
    """Write the plist and load it. Raises RegistryError (never guesses)
    if `project_name` is None and more than one project is registered --
    the same refusal `select_project` already uses everywhere else, so a
    forgotten --project fails the same way here as it does for `doctor
    --project`."""
    run_launchctl = run_launchctl or _run_launchctl
    reg = load_registry(registry_path)
    project = select_project(reg, project_name)  # refuses rather than guessing

    label = label_for(label_prefix)
    agents_dir = agents_dir if agents_dir is not None else launch_agents_dir()
    agents_dir.mkdir(parents=True, exist_ok=True)

    log_dir = default_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    state_dir = state_dir if state_dir is not None else default_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)

    state_path = (state_dir / f"{project.name}.json").resolve()
    log_path = (log_dir / f"{project.name}.log").resolve()
    resolved_config = Path(config_path).resolve()
    spoke_bin = _spoke_bin()
    path_env = os.environ.get("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")

    dest = plist_path(label, agents_dir)
    if dest.exists():
        # Best-effort: re-installing over an already-loaded agent must not
        # fail just because it is already loaded. The real signal of
        # success is the `load` call below.
        run_launchctl(["unload", str(dest)])

    dest.write_bytes(build_plist(
        label, spoke_bin, project.name, resolved_config,
        state_path, log_path, hour, minute, path_env,
    ))

    # Record what was actually installed -- label, project, plist path --
    # so `--status`/`--uninstall` with no flag can find this agent even
    # when it was installed under a non-default --label-prefix (see the
    # module docstring). Written regardless of whether the launchctl load
    # below succeeds: the plist is on disk either way, and "installed"
    # already means "the plist exists", not "launchd loaded it".
    install_record_path(project.name, state_dir).write_text(json.dumps({
        "label": label,
        "project": project.name,
        "plist_path": str(dest),
    }))

    r = run_launchctl(["load", "-w", str(dest)])
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip() or f"exit {r.returncode}"
        return ScheduleResult(
            ok=False,
            message=(f"wrote {dest} but `launchctl load -w {dest}` failed: {detail}. "
                      "The plist is on disk but NOT loaded -- it will not run until "
                      "this is fixed and `schedule --install` is run again."),
            plist_path=dest, loaded=False,
        )
    return ScheduleResult(
        ok=True,
        message=(f"installed {label} for project {project.name!r}\n"
                  f"  plist: {dest}\n"
                  f"  runs daily at {hour:02d}:{minute:02d}\n"
                  f"  state: {state_path}\n"
                  f"  log:   {log_path}"),
        plist_path=dest, loaded=True,
    )


def uninstall(
    *,
    label_prefix: str | None = None,
    agents_dir: Path | None = None,
    project: str | None = None,
    state_dir: Path | None = None,
    run_launchctl=None,
) -> ScheduleResult:
    run_launchctl = run_launchctl or _run_launchctl
    agents_dir = agents_dir if agents_dir is not None else launch_agents_dir()
    state_dir = state_dir if state_dir is not None else default_state_dir()

    label, _record, note = _resolve_label(label_prefix, project, state_dir)
    if note:
        return ScheduleResult(ok=False, message=f"schedule: {note}", plist_path=None, loaded=None)

    dest = plist_path(label, agents_dir)

    # Remove any install record for this label regardless of how the label
    # was decided (explicit flag, --project, or the implicit single-record
    # case) -- a stale record left behind after an explicit-prefix
    # uninstall would make the NEXT --status (no flag) wrongly report
    # "recorded but missing" for an agent that was deliberately removed.
    removed_record = False
    record_project = _record.get("project") if _record else None
    for record_path, rec in _records_matching_label(label, state_dir):
        record_path.unlink()
        removed_record = True
        record_project = rec.get("project")

    if not dest.exists():
        if removed_record:
            return ScheduleResult(
                ok=True,
                message=(f"removed stale install record for {label} "
                          f"(project {record_project!r}); the plist {dest} was already gone"),
                plist_path=dest, loaded=False,
            )
        return ScheduleResult(
            ok=False,
            message=f"no agent installed: {dest} does not exist",
            plist_path=dest, loaded=None,
        )
    r = run_launchctl(["unload", str(dest)])
    dest.unlink()
    record_note = " and its install record" if removed_record else ""
    return ScheduleResult(
        ok=True,
        message=(f"removed {label}: unloaded ({'ok' if r.returncode == 0 else f'exit {r.returncode}'}) "
                  f"and deleted {dest}{record_note}"),
        plist_path=dest, loaded=False,
    )


_STATE_READ_ERRORS = (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError)


def _last_run(state_path: Path | None) -> str | None:
    if state_path is None or not state_path.exists():
        return None
    try:
        return json.loads(state_path.read_text()).get("last_run")
    except _STATE_READ_ERRORS:
        return None


def _project_and_state_from_plist(path: Path) -> tuple[str | None, Path | None]:
    try:
        data = plistlib.loads(path.read_bytes())
    except (OSError, ValueError):  # plistlib.InvalidFileException is a ValueError
        return None, None
    args = data.get("ProgramArguments") or []
    project = args[args.index("--project") + 1] if "--project" in args else None
    state = args[args.index("--state") + 1] if "--state" in args else None
    return project, (Path(state) if state else None)


def _read_install_record(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except _STATE_READ_ERRORS:
        return None


def _all_install_records(state_dir: Path) -> list[tuple[Path, dict]]:
    if not state_dir.exists():
        return []
    out = []
    for p in sorted(state_dir.glob("*.install.json")):
        rec = _read_install_record(p)
        if rec is not None:
            out.append((p, rec))
    return out


def _records_matching_label(label: str, state_dir: Path) -> list[tuple[Path, dict]]:
    return [(p, r) for p, r in _all_install_records(state_dir) if r.get("label") == label]


def _resolve_label(
    label_prefix: str | None, project: str | None, state_dir: Path
) -> tuple[str, dict | None, str | None]:
    """Decide which label a --status/--uninstall call with no --config
    registry to consult should use. Returns (label, record-or-None,
    error-note-or-None); a non-None note means the caller must refuse
    rather than proceed (never guess between two installed agents).

    Precedence, matching the promise in the module docstring:
      1. An explicit --label-prefix always wins (still looked up against
         any matching record, purely so --status can also show the
         "recorded but missing" case for an explicitly-named label).
      2. --project pins the lookup to that project's install record.
      3. With neither, an unambiguous single recorded install is used
         implicitly -- the same "one candidate needs no flag, more than
         one is refused rather than guessed" rule select_project already
         applies to project selection elsewhere in this tool.
      4. No record at all (a plist that predates this fix, or a machine
         with nothing installed): fall back to DEFAULT_LABEL_PREFIX, same
         as before this fix existed.
    """
    if label_prefix is not None:
        label = label_for(label_prefix)
        matches = _records_matching_label(label, state_dir)
        return label, (matches[0][1] if matches else None), None

    if project is not None:
        record = _read_install_record(install_record_path(project, state_dir))
        if record is not None:
            return record["label"], record, None
        return label_for(DEFAULT_LABEL_PREFIX), None, None

    records = _all_install_records(state_dir)
    if len(records) == 1:
        rec = records[0][1]
        return rec["label"], rec, None
    if len(records) > 1:
        names = ", ".join(sorted(r.get("project", "?") for _, r in records))
        return "", None, (
            f"multiple installs recorded ({names}) -- pass --project or "
            "--label-prefix to say which one"
        )
    return label_for(DEFAULT_LABEL_PREFIX), None, None


def status(
    *,
    label_prefix: str | None = None,
    agents_dir: Path | None = None,
    project: str | None = None,
    state_dir: Path | None = None,
    run_launchctl=None,
) -> str:
    run_launchctl = run_launchctl or _run_launchctl
    agents_dir = agents_dir if agents_dir is not None else launch_agents_dir()
    state_dir = state_dir if state_dir is not None else default_state_dir()

    label, record, note = _resolve_label(label_prefix, project, state_dir)
    if note:
        return f"schedule: {note}"

    dest = plist_path(label, agents_dir)
    lines = [f"schedule: label {label}"]
    if not dest.exists():
        if record is not None:
            # A record exists but the plist it points at is gone -- a
            # different situation from never having installed (the whole
            # reason this fix exists), so it must read differently, not
            # collapse into the same "installed: NO" line.
            lines.append(
                f"  installed: RECORDED, PLIST MISSING -- install recorded this label "
                f"for project {record.get('project')!r} at {dest}, but that file does "
                "not exist. The plist was removed some other way than "
                "`schedule --uninstall`. Run `schedule --uninstall` to clear the stale "
                "record, then `schedule --install` to reinstall."
            )
        else:
            lines.append(f"  installed: NO ({dest} does not exist)")
        return "\n".join(lines)

    lines.append(f"  installed: yes ({dest})")
    r = run_launchctl(["list", label])
    loaded = r.returncode == 0
    lines.append(f"  loaded by launchd: {'yes' if loaded else 'NO'}")

    plist_project, state_path = _project_and_state_from_plist(dest)
    last_run = _last_run(state_path)
    if last_run:
        lines.append(f"  last run: {last_run} (project {plist_project})")
    else:
        # This is the entire point of --status: distinguish "installed but
        # has never actually fired" from "ran and found nothing" -- the
        # two must never look the same.
        lines.append(
            f"  last run: never recorded -- installed but has not fired yet "
            f"(project {plist_project}, state file {state_path})"
        )
    return "\n".join(lines)
