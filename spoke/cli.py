from __future__ import annotations
import argparse
import os
import socket
import httpx
from dataclasses import replace
from datetime import date
from pathlib import Path
from .config import load_config, load_config_file, ConfigError, Config
from .checks.structural import check_all
from .checks.staleness import check_staleness
from .checks.contradict import find_contradictions, plan_clusters
from .llm import LLMUnavailable
from .store import Store
from .doctor import run_doctor
from .notify import load_notify_config, send_notification
from . import schedule as schedule_mod
from .ledger import (
    LedgerError, NODE_TYPES, Node, Relation, describe_skipped, is_unruled,
)
from .ledger.flags import FLAG_KINDS, compute_flags
from .ledger.graph import LENSES, load_graph, resolve_lens
from .ledger.board import (
    BoardError, _require_node_type, build_lens, build_scale, lens_lines,
)
from .ledger.matrix import cross
from .ledger.schema import expect_subject
from .ledger.scale import (
    Axis, BASES, DEFAULT_MAX_VALUE, Observation, Score, composite, freshness,
    grid, score_stale_threshold,
)
from .scan import apply, scan_repos
from .ledger.preamble import render_preamble
from .ledger.store import LedgerStore
from . import workspace as _workspace_mod
from .projects import (
    extend_project,
    RegistryError, add_project, remove_project, load_registry, save_registry,
    select_project,
    config_for, derive_stores, project_for_path, default_claude_projects,
)

DEFAULT_REGISTRY = Path("~/.claude/spoke/projects.toml").expanduser()


def _registry_path() -> Path:
    return Path(os.environ.get("SPOKE_REGISTRY") or DEFAULT_REGISTRY).expanduser()


def _claude_projects() -> Path:
    # Delegates to spoke.projects.default_claude_projects() -- the same
    # SPOKE_CLAUDE_PROJECTS-at-call-time resolution the library functions
    # themselves now use by default (Finding 5), so there is exactly one
    # place this logic lives rather than two copies that can drift apart.
    return default_claude_projects()


def _config_file_path(args) -> Path:
    """The config.toml path a command resolves against: --config if given,
    else the default relative config.toml. Shared by _resolve (which loads
    it) and `serve` (which also needs it to hand to create_app, so the
    Settings screen knows what file it is editing)."""
    return Path(args.config) if getattr(args, "config", None) else Path("config.toml")


def _workspace(args):
    """The resolved Workspace for this command, built once.

    Stashed on `args` because `_resolve` and `_active_project` each used
    to load the registry and re-select the project, so every command did
    the whole resolution twice and could in principle have got two
    answers. Now it happens once and everything -- the config, the
    project's axes and vocabulary, and `today` -- comes from the same
    object.
    """
    ws = getattr(args, "_ws", None)
    if ws is None:
        ws = _workspace_mod.resolve(
            config_path=_config_file_path(args),
            project=getattr(args, "project", None),
        )
        args._ws = ws
        for notice in ws.notices:
            print(notice)
    return ws


def _resolve(args) -> tuple[Config, str | None]:
    """(Config, active project name or None) -- the shape most commands
    still want. `_workspace(args)` is the thing itself."""
    ws = _workspace(args)
    return ws.config, ws.name


# Re-exported from spoke.workspace, which is where it lives now: it had
# three importers and no home of its own (server.py imported it from here
# lazily to dodge a module cycle; mcp_server.py kept a byte-identical fork
# because importing cli pulls in a module that writes to stdout, and
# stdout is the MCP wire). Kept as a name here so existing callers and
# tests are unaffected.
_store_error = _workspace_mod.store_error


def _require_store(store_path: Path, label: str) -> int | None:
    """Print and return a non-zero exit code if `store_path` is unusable;
    None if the caller should proceed. Exit 2 -- the same code already used
    for a broken model integration -- since this is a configuration defect,
    not an ordinary finding count."""
    err = _store_error(store_path)
    if err is None:
        return None
    print(f"{label}: NOT RUN - {err}")
    return 2


def _cmd_check(args) -> int:
    cfg, project = _resolve(args)
    if project:
        print(f"project: {project}")
    rc = _require_store(cfg.store_path, "check")
    if rc is not None:
        return rc
    findings = check_all(cfg.store_path, cfg.accepted_absences,
                         _workspace(args).axes)
    if getattr(args, "probe", False):
        # Probe every expectation NOW, and record what was found -- the
        # same code path doctor runs on its schedule. Off by default:
        # `check` is structural and offline; a probe reaches the network.
        import httpx
        from .checks.expect import check_expectations
        from .ledger.store import LedgerStore
        with httpx.Client(timeout=10.0, trust_env=False) as client:
            findings += check_expectations(
                LedgerStore(cfg.store_path), client, _workspace(args).today,
                axes=_workspace(args).axes, scale_max=_workspace(args).scale_max)
    if not findings:
        print("check: no structural defects" + (", every expectation met" if getattr(args, "probe", False) else ""))
        return 0
    for f in findings:
        print(f"{f.kind}: {f.file}: {f.detail}")
    print(f"\ncheck: {len(findings)} defect(s)")
    return 1


def _cmd_stale(args) -> int:
    cfg, project = _resolve(args)
    if project:
        print(f"project: {project}")
    rc = _require_store(cfg.store_path, "stale")
    if rc is not None:
        return rc
    # trust_env=False: ambient proxy env vars (HTTP_PROXY etc.) must not be able to
    # redirect probe requests to an attacker-controlled proxy.
    with httpx.Client(timeout=10.0, trust_env=False) as client:
        findings = check_staleness(Store(cfg.store_path), client, _workspace(args).clock)
    for f in findings:
        print(f"{f.kind}: {f.file}: {f.detail}")
    print(f"\nstale: {len(findings)} finding(s)")
    return 1 if findings else 0


def _cmd_doctor(args) -> int:
    cfg, project = _resolve(args)
    if project:
        print(f"project: {project}")
    rc = _require_store(cfg.store_path, "doctor")
    if rc is not None:
        return rc
    state = Path(args.state) if args.state else Path(".spoke-doctor.json")
    # trust_env=False: ambient proxy env vars (HTTP_PROXY etc.) must not be able to
    # redirect probe requests to an attacker-controlled proxy.
    with httpx.Client(timeout=10.0, trust_env=False) as client:
        res = run_doctor(cfg, client, state, _workspace(args).today,
                         _workspace(args).axes, _workspace(args).scale_max)

    if res.state_reset:
        print(f"NOTE: {res.state_reset}")
    print(f"doctor: previous run {res.previous_run or '(none recorded)'}")

    for f in res.new:
        print(f"NEW      {f.kind}: {f.file}: {f.detail}")
    for f in res.resolved:
        print(f"RESOLVED {f.kind}: {f.file}: {f.detail}")
    print(f"\ndoctor: {res.total} open finding(s), {len(res.new)} new, "
          f"this run recorded in {state} as {_workspace(args).today.isoformat()}")

    notify_cfg = load_notify_config(_config_file_path(args))
    notify_failed = False
    if not notify_cfg.topic:
        # A stated absence, not a silent one: a watcher nobody wired up to
        # reach anyone must say so on every run, not just work quietly
        # (and uselessly) forever.
        print("notify: not configured")
    elif not res.new:
        # Only NEW defects notify -- a message on every clean run is exactly
        # the wallpaper this rule exists to avoid.
        print("notify: no new defects, nothing sent")
    else:
        # trust_env=False: same reasoning as the client above -- ambient
        # proxy env vars must not be able to redirect this to an
        # attacker-controlled proxy.
        with httpx.Client(timeout=10.0, trust_env=False) as nclient:
            nres = send_notification(notify_cfg, project, res.new, nclient)
        if nres.ok:
            print(f"notify: sent ({nres.detail})")
        else:
            notify_failed = True
            # Visible, not swallowed: a watcher that cannot reach anyone
            # must never report success, even if every check above it was
            # otherwise clean.
            print(f"notify: FAILED - {nres.detail}")

    return 1 if (res.new or notify_failed) else 0


def _cmd_contradictions(args) -> int:
    cfg, project = _resolve(args)
    if project:
        print(f"project: {project}")
    rc = _require_store(cfg.store_path, "contradictions")
    if rc is not None:
        return rc
    store = Store(cfg.store_path)
    plan = plan_clusters(store)
    # Report the size of the bill before any of it is spent: each cluster
    # actually sent is one model call, so a user must be able to see that
    # count -- and how many clusters were skipped as already covered --
    # up front, not after the spending has already happened.
    print(f"contradictions: {len(plan.all_groups)} cluster(s) total, "
          f"{len(plan.groups_to_send)} to send, "
          f"{len(plan.covered_pairs)} distinct pair(s) covered")
    # trust_env=False: this is the one client here that carries a bearer
    # token (the model API key) -- ambient proxy env vars (HTTP_PROXY etc.)
    # must not be able to redirect it to an attacker-controlled proxy, same
    # as `stale` and `doctor` already do.
    with httpx.Client(timeout=60.0, trust_env=False) as client:
        try:
            run = find_contradictions(store, cfg, client)
        except LLMUnavailable as exc:
            # Fail closed: a missing credential must never look like a
            # clean run. Exit non-zero and name what is missing, so a cron
            # invoking this cannot mistake "nothing checked" for "checked,
            # all clear" -- this is exactly the failure mode that let a
            # digest publish silently for four weeks.
            print(f"contradictions: NOT RUN - {exc}")
            return 2
    for c in run.findings:
        print(f"\n{c.files[0]}  vs  {c.files[1]}\n  A: {c.quote_a}\n  B: {c.quote_b}\n  why: {c.reason}")

    failed = run.unparseable + run.errored
    # ALWAYS report a non-zero failed count, whether or not every cluster
    # failed. Without this a run where every call was billed but returned
    # a refusal, markdown-fenced junk, or a 500 would print
    # "0 candidate(s)" and exit 0 -- identical to a genuinely clean store.
    # That is the precise failure shape this project exists to catch.
    if failed:
        detail = []
        if run.unparseable:
            detail.append(f"{run.unparseable} unparseable")
        if run.errored:
            detail.append(f"{run.errored} errored")
        print(f"\nWARNING: {failed} of {run.sent} cluster response(s) could not be parsed "
              f"({', '.join(detail)})")

    if run.malformed:
        # A response can be valid JSON -- so it never touched `unparseable`
        # -- while individual items inside it are missing the required
        # keys. Those items must not vanish uncounted: same failure shape
        # as `unparseable`, one level deeper (see contradict.py, fixed
        # upstream in 9ab60f5 for the response-level case).
        print(f"\nWARNING: {run.malformed} item(s) in an otherwise-parsed response "
              "were missing required fields and were dropped")

    print(f"\ncontradictions: {len(run.findings)} candidate(s)")

    if run.sent and failed == run.sent:
        # Every cluster that was actually sent -- and billed -- came back
        # unusable. This must never read as "checked, store is clean":
        # the integration is broken, not the store. Same fail-closed exit
        # code as a missing credential, with the reason named.
        print(f"contradictions: NOT RUN - all {run.sent} sent cluster response(s) "
              "could not be parsed; the model integration is broken, not the store clean")
        return 2

    return 0


# The client's own installer is the seam. Spoke does NOT hand-edit
# ~/.claude.json or .mcp.json: that is another program's file, its shape
# is that program's to change, and a tool that rewrites it is one release
# away from corrupting someone's whole MCP configuration. `claude mcp`
# is the supported way in, so that is what this drives -- and when it is
# not on PATH, Spoke prints the registration for the human to paste
# rather than guessing where it goes.
_CLIENT = "claude"


def _mcp_registration() -> list[str]:
    """The command a client should run to start this server.

    `sys.executable` is the interpreter running right now, absolute, so
    the registration keeps working when no virtualenv is active -- which
    is exactly the situation a client launches a server in.
    """
    import sys

    return [sys.executable, "-m", "spoke.cli", "mcp"]


def _mcp_install(args) -> int:
    import json
    import shutil
    import subprocess

    cmd = _mcp_registration()
    name = args.name
    if args.project:
        cmd += ["--project", args.project]

    client = shutil.which(_CLIENT)
    add = [client or _CLIENT, "mcp", "add", name, "-s", args.scope, "--", *cmd]

    if client is None or args.dry_run:
        why = ("dry run" if client else
               f"{_CLIENT!r} is not on PATH -- nothing was changed")
        print(f"mcp: {why}. The registration is:\n")
        print("  " + " ".join(add))
        print("\nOr, as JSON for a client that takes one:\n")
        print(json.dumps({"mcpServers": {name: {
            "command": cmd[0], "args": cmd[1:],
        }}}, indent=2))
        return 0 if args.dry_run else 1

    print("mcp: " + " ".join(add))
    r = subprocess.run(add, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    if out:
        print(out)
    if r.returncode != 0:
        return 2
    # Registered is not running. Same discipline as `schedule --status`:
    # the install step succeeding says nothing about whether the thing
    # actually works.
    print("\nmcp: registered. Checking it actually starts:")
    return _mcp_status(args)


def _mcp_status(args) -> int:
    """Whether the client can START the server, not merely whether a line
    was written into a config file. `claude mcp get` health-checks it."""
    import shutil
    import subprocess

    client = shutil.which(_CLIENT)
    if client is None:
        print(f"mcp: {_CLIENT!r} is not on PATH -- cannot check")
        return 1
    r = subprocess.run([client, "mcp", "get", args.name],
                       capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    print(out or f"mcp: no server named {args.name!r} is registered")
    if r.returncode != 0:
        return 1
    # The client prints its own verdict; surface a failure to connect as a
    # non-zero exit so a script can tell, rather than only a human reading.
    return 0 if "Connected" in out or "✓" in out else 1


def _mcp_uninstall(args) -> int:
    import shutil
    import subprocess

    client = shutil.which(_CLIENT)
    if client is None:
        print(f"mcp: {_CLIENT!r} is not on PATH -- remove {args.name!r} by hand")
        return 1
    r = subprocess.run([client, "mcp", "remove", args.name],
                       capture_output=True, text=True)
    print((r.stdout + r.stderr).strip() or f"mcp: removed {args.name!r}")
    return r.returncode


def _cmd_demo(args) -> int:
    """`spoke demo <dir>` -- see spoke/demo.py. Touches nothing of the
    user's own: the store, registry and config it builds all live under
    <dir>, and the command it prints names them explicitly."""
    from . import demo as demo_mod

    repos = {}
    if args.tare:
        repos["tare"] = Path(args.tare).expanduser()
    if args.forage:
        repos["forage"] = Path(args.forage).expanduser()
    dest = Path(args.dir).expanduser()
    for name in ("tare", "forage"):
        if name not in repos:
            print(f"demo: cloning {demo_mod.DEMO_REPOS[name]} ...")
    try:
        r = demo_mod.build(dest, repos or None)
    except demo_mod.DemoError as e:
        print(f"demo: {e}")
        return 2
    print(f"demo: built under {r.root}")
    print(f"demo: scanned {r.scanned['proposals']} proposals -- {r.scanned['written']} written, "
          f"{r.scanned['refused']} refused; {r.nodes} nodes on the map with the examples on top")
    print("demo: raised: held, blocked, deviates, stale, and absent (on the capability lens); "
          "a former name resolves; one composite is issued and one withheld, each saying why")
    print("\nServe it with:\n")
    print(f"  {r.serve_command}\n")
    return 0


def _cmd_hooks(args) -> int:
    """`spoke hooks` -- turn the two surfacing hooks on, off, or report.

    Same discipline as `serve --install` and `mcp --install`: the install
    step succeeding says nothing about whether the thing can actually
    run, so every path ends by checking what is true rather than what was
    written.
    """
    import json

    from . import hooks_install as hk

    chosen = [a for a in ("install", "uninstall", "status") if getattr(args, a)]
    if len(chosen) != 1:
        print("hooks: pass exactly one of --install, --uninstall, --status")
        return 2
    action = chosen[0]
    path = Path(args.settings).expanduser() if args.settings else hk.DEFAULT_SETTINGS

    missing = hk.missing_scripts()
    if missing:
        print(f"hooks: script(s) not found in {hk.HOOK_DIR}: {', '.join(missing)}")
        return 2

    try:
        settings = hk.read_settings(path)
    except (OSError, json.JSONDecodeError) as e:
        # Never rewrite a settings file this could not read: doing so
        # would discard every other hook and setting in it.
        print(f"hooks: could not read {path}: {e}")
        return 2

    if action == "status":
        return _hooks_report(path, settings)

    updated = hk.plan(settings) if action == "install" else hk.remove(settings)
    if args.dry_run:
        print(f"hooks: dry run -- {path} would become:\n")
        print(json.dumps(updated, indent=2))
        return 0
    if updated == settings:
        print(f"hooks: {path} already says this; nothing changed")
        return _hooks_report(path, settings)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(updated, indent=2) + "\n")
    print(f"hooks: wrote {path}")
    return _hooks_report(path, updated)


def _hooks_report(path: Path, settings: dict) -> int:
    """What is TRUE right now: registered, script present, and jq
    available -- because a hook that cannot run reports nothing, and
    silence looks exactly like 'nothing to report'."""
    from . import hooks_install as hk

    state = hk.installed(settings)
    for event, on in sorted(state.items()):
        print(f"hooks: {event} {'registered' if on else 'NOT registered'} in {path}")
    jq = hk.jq_path()
    if jq is None:
        # Not a warning tucked at the end: both scripts parse their input
        # with jq, so without it they emit nothing at all.
        print("hooks: `jq` is NOT on PATH -- both hooks parse their input "
              "with it, so they would run and report nothing. Install it "
              "(`brew install jq`) or the hooks are decorative.")
        return 1
    print(f"hooks: jq {jq}")
    return 0


def _cmd_mcp(args) -> int:
    """`spoke mcp` -- run the MCP server over stdio, giving any
    MCP-capable agent the same read/write ledger tools a human has on
    the CLI, through the same _resolve()/LedgerStore/validate()/
    check_all()/render_preamble() functions every other command uses
    (see spoke/mcp_server.py). Resolves config and project through
    _resolve(), exactly as every other command does, so the registry,
    --project, and the override announcement all behave identically.

    stdout is the MCP JSON-RPC wire for this command specifically --
    every other command prints plain diagnostics to stdout, which is
    correct there and would corrupt the protocol stream here. Redirected
    to stderr for exactly the resolution step below; restored before the
    stdio server (which owns stdout for the rest of the process) starts.
    """
    if args.install:
        return _mcp_install(args)
    if args.status:
        return _mcp_status(args)
    if args.uninstall:
        return _mcp_uninstall(args)

    import sys
    from .mcp_server import build_server, run_stdio
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        cfg, project = _resolve(args)
        rc = _require_store(cfg.store_path, "mcp")
    except (ConfigError, RegistryError) as e:
        print(f"config: {e}", file=sys.stderr)
        return 2
    finally:
        sys.stdout = real_stdout
    if rc is not None:
        return rc
    server = build_server(_workspace(args))
    run_stdio(server)
    return 0


def _serve_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/"


def _serve_reachable(port: int, timeout: float = 2.0) -> str | None:
    """None when SPOKE answers on `port`; otherwise why it did not.

    An HTTP request, not `launchctl list`: "the agent is loaded" and "the
    app is serving" are different facts, and only one is the thing you
    wanted -- the same distinction `schedule --status` exists to make.

    And it asks WHO answered. Checking only that the port responds
    reported a different project's server, already listening on 8765, as
    Spoke running -- so a status that was meant to catch a dead agent
    instead certified a stranger. Ports collide; identity is the check.
    """
    import json
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(
            _serve_url(port) + "api/health", timeout=timeout
        ) as r:
            if r.status != 200:
                return f"HTTP {r.status}"
            who = json.loads(r.read() or b"{}").get("app")
            if who != "spoke":
                return f"something else is on this port (app={who!r})"
            return None
    except json.JSONDecodeError:
        return "something else is on this port (not JSON)"
    except urllib.error.URLError as e:
        return str(getattr(e, "reason", e))
    except OSError as e:
        return str(e)


def _cmd_serve_install(args) -> int:
    from . import schedule as sched

    ws = _workspace(args)
    if ws.project is None:
        print("serve: --install needs a registered project -- "
              "`spoke projects add <name> --repo <path>`")
        return 2
    label = sched.serve_label_for(
        os.environ.get("SPOKE_LAUNCHD_LABEL_PREFIX") or sched.DEFAULT_LABEL_PREFIX
    )
    agents = sched.launch_agents_dir()
    plist = sched.plist_path(label, agents)
    log_dir = sched.default_log_dir()
    log_path = log_dir / f"{label}.log"

    body = sched.build_serve_plist(
        label=label, spoke_bin=sched._spoke_bin(), project=ws.project.name,
        config_path=Path(_config_file_path(args)).resolve(),
        log_path=log_path, port=args.port, path_env=os.environ.get("PATH", ""),
    )
    if args.dry_run:
        print(f"serve: dry run -- would write {plist}\n")
        print(body.decode())
        return 0

    agents.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(body)
    sched._run_launchctl(["unload", str(plist)])
    r = sched._run_launchctl(["load", str(plist)])
    if r.returncode != 0:
        print(f"serve: launchctl load failed - {(r.stderr or r.stdout).strip()}")
        return 2
    print(f"serve: wrote {plist} and loaded it")
    print(f"serve: log {log_path}")
    # launchd returns as soon as it has SPAWNED the process; uvicorn then
    # takes a moment to bind. Checking immediately reported "connection
    # refused" for a server that was seconds from being fine -- a status
    # that is wrong right after an install teaches the reader to ignore
    # it, which is worse than not printing one.
    _wait_for_serve(args.port)
    if getattr(args, "open_browser", False):
        _open_board(args.port)
    return _cmd_serve_status(args)


def _open_board(port: int) -> None:
    """Open the board in the default browser.

    Never raises: a headless box, or no browser at all, must not turn a
    working server into a failed command. It says so instead -- the flag
    claimed something happened, and silence would leave the reader
    looking for a window that was never opened.
    """
    import webbrowser

    url = f"http://127.0.0.1:{port}/board"
    if webbrowser.open(url):
        print(f"serve: opened {url}")
    else:
        print(f"serve: no browser to open -- the board is at {url}")


def _wait_for_serve(port: int, seconds: float = 15.0) -> None:
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _serve_reachable(port, timeout=1.0) is None:
            return
        time.sleep(0.5)


def _cmd_serve_status(args) -> int:
    """Loaded is not serving."""
    from . import schedule as sched

    label = sched.serve_label_for(
        os.environ.get("SPOKE_LAUNCHD_LABEL_PREFIX") or sched.DEFAULT_LABEL_PREFIX
    )
    plist = sched.plist_path(label)
    print(f"serve: plist {plist} {'exists' if plist.exists() else 'MISSING'}")
    listed = sched._run_launchctl(["list", label])
    print(f"serve: launchd {'knows' if listed.returncode == 0 else 'does NOT know'} {label}")

    why = _serve_reachable(args.port)
    if why is None:
        print(f"serve: answering at {_serve_url(args.port)}")
        return 0
    # The whole point of a --status: an agent that is loaded and a server
    # that is dead look identical from launchctl alone.
    print(f"serve: NOT answering at {_serve_url(args.port)} - {why}")
    return 1


def _cmd_serve_uninstall(args) -> int:
    from . import schedule as sched

    label = sched.serve_label_for(
        os.environ.get("SPOKE_LAUNCHD_LABEL_PREFIX") or sched.DEFAULT_LABEL_PREFIX
    )
    plist = sched.plist_path(label)
    sched._run_launchctl(["unload", str(plist)])
    if plist.exists():
        plist.unlink()
        print(f"serve: unloaded and removed {plist}")
    else:
        print(f"serve: nothing installed at {plist}")
    return 0


def _cmd_serve(args) -> int:
    if args.install:
        return _cmd_serve_install(args)
    if args.status:
        return _cmd_serve_status(args)
    if args.uninstall:
        return _cmd_serve_uninstall(args)

    import uvicorn
    from .server import create_app
    # `serve` is the one command that starts WITHOUT a usable store.
    # Every other command refuses, correctly: a check over a store that
    # is not there would report "0 defects" forever. But the server is
    # where an installer sets the store up, and a server that refuses to
    # start until it is configured leaves them with nothing but the
    # terminal and this error. Unconfigured, it serves the first-run
    # screen and nothing else.
    try:
        cfg, project = _resolve(args)
    except (ConfigError, RegistryError) as e:
        cfg = load_config_file(_config_file_path(args))
        project = None
        print(f"serve: not configured yet -- {e}")
        print(f"serve: the first-run screen is at http://127.0.0.1:{args.port}/setup")
    else:
        if project:
            print(f"project: {project}")
        err = _store_error(cfg.store_path)
        if err:
            print(f"serve: {err}")
            print(f"serve: the first-run screen is at http://127.0.0.1:{args.port}/setup")
    # The board must show the project's own words, so the registry entry
    # -- not just the resolved Config -- has to reach the app.
    try:
        proj = _active_project(args)
    except (ConfigError, RegistryError):
        proj = None
    vocabulary = dict(proj.vocabulary) if proj else {}
    axes = tuple(proj.axes) if proj else ()
    if args.open_browser:
        # uvicorn.run blocks, so the browser is opened from a thread that
        # waits for the port to answer first. Opening before it binds
        # shows the reader a connection error for a server that is
        # seconds from being fine -- the same trap `_wait_for_serve`
        # exists for on the install path.
        import threading

        def _open_when_up() -> None:
            _wait_for_serve(args.port)
            _open_board(args.port)

        threading.Thread(target=_open_when_up, daemon=True).start()
    uvicorn.run(
        create_app(cfg, config_path=_config_file_path(args),
                   vocabulary=vocabulary, axes=axes,
                   scale_max=proj.scale_max if proj else None,
                   registry_path=_registry_path(),
                   project_name=proj.name if proj else None),
        host="127.0.0.1", port=args.port,
    )
    return 0


def _explicit_label_prefix(args) -> str | None:
    """None means "the user did not say" -- distinct from the default
    prefix, so --status/--uninstall can fall back to a recorded label
    (from `schedule --install`) before ever reaching for
    DEFAULT_LABEL_PREFIX. Only `install` collapses this to a concrete
    string, because install always needs one to write a plist with."""
    return getattr(args, "label_prefix", None) or os.environ.get("SPOKE_LAUNCHD_LABEL_PREFIX")


def _cmd_schedule(args) -> int:
    chosen = [a for a in ("install", "uninstall", "status") if getattr(args, a)]
    if len(chosen) != 1:
        print("schedule: pass exactly one of --install, --uninstall, --status")
        return 2
    action = chosen[0]
    explicit_prefix = _explicit_label_prefix(args)

    if action == "status":
        print(schedule_mod.status(label_prefix=explicit_prefix, project=args.project))
        return 0

    if action == "uninstall":
        res = schedule_mod.uninstall(label_prefix=explicit_prefix, project=args.project)
        print(res.message)
        return 0 if res.ok else 2

    # install
    label_prefix = explicit_prefix or schedule_mod.DEFAULT_LABEL_PREFIX
    hour = args.hour if args.hour is not None else schedule_mod.DEFAULT_HOUR
    minute = args.minute if args.minute is not None else schedule_mod.DEFAULT_MINUTE
    res = schedule_mod.install(
        _registry_path(), args.project, _config_file_path(args),
        label_prefix=label_prefix, hour=hour, minute=minute,
    )
    print(res.message)
    return 0 if res.ok else 2


def _cmd_projects_add(args) -> int:
    """Create a project, or grow one that exists.

    On an existing name, `--repo` extends the repo list and NOTHING else
    changes. `--memory-store` and `--accept` on an existing name are
    refused rather than applied: applied, they would overwrite settings
    the project already carries, under a command the caller thought was
    adding a repo.
    """
    reg = load_registry(_registry_path())
    if args.name in reg:
        if args.memory_store or args.accept:
            print(f"projects: {args.name!r} exists -- `add` with --repo grows it; "
                  "--memory-store and --accept would overwrite what it already has "
                  "and are refused here")
            return 2
        p, added = extend_project(_registry_path(), args.name, [Path(r) for r in args.repo])
        already = [r for r in [Path(x) for x in args.repo] if r not in added]
        for r in added:
            print(f"projects: added {r} to {p.name}")
        for r in already:
            print(f"projects: {r} is already in {p.name}; nothing changed")
        print(f"{p.name}: " + ", ".join(str(r) for r in p.repos))
        return 0
    p = add_project(
        _registry_path(),
        args.name,
        [Path(r) for r in args.repo],
        Path(args.memory_store) if args.memory_store else None,
        args.accept,
    )
    print(f"registered {p.name}: " + ", ".join(str(r) for r in p.repos))
    return 0


def _cmd_projects_remove(args) -> int:
    # Forgets the registration only -- the project's repos and its memory
    # store (the .md records under ~/.claude/projects/.../memory) are never
    # touched. This is a deregistration, not a deletion.
    remove_project(_registry_path(), args.name)
    print(f"removed {args.name}")
    return 0


def _cmd_projects_list(args) -> int:
    reg = load_registry(_registry_path())
    if not reg:
        print("no projects registered")
        return 0
    for name in sorted(reg):
        p = reg[name]
        stores = derive_stores(p, _claude_projects())
        print(f"{name}: {len(p.repos)} repo(s), {len(stores)} store(s)")
        for r in p.repos:
            print(f"  repo  {r}")
        for s in stores:
            print(f"  store {s} ({len(list(s.glob('*.md')))} records)")
        # Three states to print distinctly, matching Project.accepted_absences:
        # None (not printed at all -- nothing was declared, so there is
        # nothing project-specific to show); () (declared empty -- printed
        # explicitly, since silently saying nothing here would look
        # identical to "not declared" and hide the whole point of Fix 2);
        # populated (the list itself).
        if p.accepted_absences is not None:
            if p.accepted_absences:
                print(f"  accepted absences: {', '.join(p.accepted_absences)}")
            else:
                print("  accepted absences: (declared empty -- accepts nothing)")
    return 0


def _cmd_projects_which(args) -> int:
    """`projects which <path>` -- what a SessionStart hook calls to turn the
    directory it derived from transcript_path into a registered project
    NAME, which is what --project actually takes. Never guesses: no owner
    and more than one owner both print a clear message and exit non-zero,
    so the hook can tell "genuinely unregistered" from "broken"."""
    reg = load_registry(_registry_path())
    proj = project_for_path(reg, Path(args.path), _claude_projects())
    if proj is None:
        print(
            f"no single registered project owns {args.path} "
            "(it is not inside a registered repo, and no registered "
            "project's slug or derived store matches it -- or more than "
            "one project matches, which is just as unusable)"
        )
        return 1
    print(proj.name)
    return 0


def _session_by() -> str:
    """Who a ledger write is attributed to: the session identifier from
    SPOKE_SESSION if one is set (the same env var other sessions on this
    machine set to name themselves), else this machine's hostname. Never
    empty -- a node's `by` and its `human:<by>` provenance must always name
    someone, not fall back to blank."""
    return os.environ.get("SPOKE_SESSION") or socket.gethostname()


# The task ledger's four questions, as the default checklist a node can
# carry. The wording is the estate's own rule, verbatim in spirit: a
# thing is not done until each is answered, and question 4 is what an
# expectation makes checkable by machine.
FOUR_QUESTIONS = (
    "What observable thing proves this works? Name the URL, command, screen or row.",
    "How would this fail SILENTLY? Name every swallowed error and what it emits instead.",
    "Did I observe the thing from (1), live? Paste what was seen.",
    "What watches it from now on? Name the check -- or add an expectation to this node.",
)


def _checklist_from_args(args) -> tuple:
    items = []
    if getattr(args, "questions", False):
        items += [{"text": q, "done": False} for q in FOUR_QUESTIONS]
    for text in (getattr(args, "check", None) or []):
        items.append({"text": text, "done": False})
    return tuple(items)


# Exit code for "written and committed, but the push to the store's
# upstream failed". Distinct from a refusal (1 or 2, nothing written) so a
# script can tell "fix the node" from "fix the network" -- and non-zero,
# because a commit nobody else can see must not read as success.
EXIT_NOT_PUSHED = 3


def _after_write(res, rc: int = 0) -> int:
    """Print every advisory a successful write carried and return the
    exit code. Call after the success line of EVERY ledger write path.

    The store had the push's stderr and returned it; for a week no caller
    read it, and twelve nodes from one doctor run sat local-only behind
    twelve 'wrote' lines. `pushed` is only meaningful when an upstream
    exists, which is exactly when a failed push leaves an advisory."""
    for a in res.advisories:
        print(f"ledger: push failed - {a}")
    if res.advisories:
        print("ledger: the commit is local only; nobody else sees it until `git push` "
              "succeeds in the store")
        return EXIT_NOT_PUSHED
    return rc


def _cmd_ledger_tick(args) -> int:
    """Tick (or untick) one checklist item by its 1-based number."""
    cfg, project = _resolve(args)
    if project:
        print(f"project: {project}")
    rc = _require_store(cfg.store_path, "ledger")
    if rc is not None:
        return rc
    store = LedgerStore(cfg.store_path)
    try:
        node = store.read(args.name)
    except FileNotFoundError:
        print(f"ledger: no such node {args.name!r}")
        return 1
    if not 1 <= args.item <= len(node.checklist):
        print(f"ledger: {args.name} has {len(node.checklist)} checklist item(s); no item {args.item}")
        return 2
    items = [dict(c) for c in node.checklist]
    items[args.item - 1]["done"] = not args.undo
    if args.note:
        items[args.item - 1]["note"] = args.note
    today = _workspace(args).today.isoformat()
    prov = node.provenance + ({"field": f"checklist[{args.item}]", "by": f"human:{_session_by()}", "at": today},)
    res = _workspace(args).write(replace(node, checklist=tuple(items), updated=today, provenance=prov), store)
    if not res.ok:
        print(f"ledger: refused - {'; '.join(res.reasons)}")
        return 2
    left = sum(1 for c in items if not c["done"])
    print(f"ledger: {args.name} [{'x' if not args.undo else ' '}] {items[args.item - 1]['text']}")
    print(f"ledger: {left} of {len(items)} still unchecked" if left else "ledger: all checked")
    return _after_write(res)


def _cmd_ledger_expect(args) -> int:
    """State what a node expects to be observably true. `doctor` probes
    it on its schedule; `check --probe` probes it now."""
    cfg, project = _resolve(args)
    if project:
        print(f"project: {project}")
    rc = _require_store(cfg.store_path, "ledger")
    if rc is not None:
        return rc
    store = LedgerStore(cfg.store_path)
    try:
        node = store.read(args.name)
    except FileNotFoundError:
        print(f"ledger: no such node {args.name!r}")
        return 1
    if bool(args.url) == bool(args.path):
        print("ledger: say where the claim is checked -- --url for a page, --path for a repo on this machine")
        return 2
    e: dict = {"url": args.url} if args.url else {"path": args.path}
    if args.worktrees_max is not None:
        e["worktrees_max"] = args.worktrees_max
    if args.status is not None:
        e["status"] = args.status
    if args.contains:
        e["contains"] = args.contains
    if args.fresh_within:
        e["fresh_within"] = args.fresh_within
    if args.dated_by:
        e["dated_by"] = args.dated_by
    if args.note:
        e["note"] = args.note
    subject = expect_subject(e)
    expects = [x for x in node.expects if expect_subject(x) != subject] + [e]
    today = _workspace(args).today.isoformat()
    prov = node.provenance + ({"field": "expects", "by": f"human:{_session_by()}", "at": today},)
    res = _workspace(args).write(replace(node, expects=tuple(expects), updated=today, provenance=prov), store)
    if not res.ok:
        print(f"ledger: refused - {'; '.join(res.reasons)}")
        return 2
    clauses = ", ".join(f"{k}={v}" for k, v in e.items() if k not in ("url", "path", "note"))
    print(f"ledger: {args.name} expects {subject} ({clauses})")
    print("ledger: unmet until a probe says otherwise -- `spoke check --probe` runs one now; "
          "`doctor` runs them on its schedule")
    return _after_write(res)


def _cmd_ledger_new(args) -> int:
    cfg, project = _resolve(args)
    if project:
        print(f"project: {project}")
    rc = _require_store(cfg.store_path, "ledger")
    if rc is not None:
        return rc

    relations = []
    for spec in (args.rel or []):
        rel_type, sep, to = spec.partition(":")
        if not sep:
            print(f"ledger: --rel {spec!r} is not of the form <type>:<node>")
            return 2
        relations.append(Relation(rel=rel_type, to=to))

    blocked_by = tuple(args.blocked_by or ())

    # Which fields the caller actually supplied on this invocation, so
    # provenance records only those -- not every field the schema knows
    # about. name/type/state/title/body are required by the parser, so a
    # successful parse means the caller supplied all four every time.
    supplied = ["type", "state", "title", "body"]
    if args.ruling is not None:
        supplied.append("ruling")
    if blocked_by:
        supplied.append("blocked_by")
    if relations:
        supplied.append("relations")

    by = _session_by()
    today = _workspace(args).today.isoformat()
    provenance = tuple({"field": f, "by": f"human:{by}", "at": today} for f in supplied)

    node = Node(
        name=args.name,
        type=args.type,
        state=args.state,
        title=args.title,
        body=args.body,
        relations=tuple(relations),
        ruling=args.ruling,
        blocked_by=blocked_by,
        aliases=tuple(args.alias or ()),
        checklist=_checklist_from_args(args),
        provenance=provenance,
        opened=today,
        updated=today,
        by=by,
        claimed_by=None,
        claimed_at=None,
    )

    res = _workspace(args).write(node)
    if not res.ok:
        # Every reason, not just the first -- a refusal that hides the
        # other defects makes the caller fix-and-retry blind.
        for reason in res.reasons:
            print(f"ledger: refused - {reason}")
        return 1
    print(f"ledger: wrote {node.name} ({node.type}, {node.state})")
    return _after_write(res)


def _report_skipped(store: LedgerStore, dangling=()) -> None:
    """Print everything that was not read, one line per KIND.

    FINDING 3: this used to live only in `_cmd_ledger_list`, so
    `ledger preamble` -- the surface that runs unasked at session start
    -- silently said nothing about a condition `ledger list` reported.
    One function, called from both.

    It now also takes `dangling`, because the two conditions were being
    printed by two different pieces of code with two different phrasings,
    which is the same drift one layer up. `describe_skipped` owns the
    wording for every surface.
    """
    for line in describe_skipped(list(dangling) + list(store.skipped)):
        print(f"ledger: {line}")


def _cmd_ledger_list(args) -> int:
    cfg, project = _resolve(args)
    if project:
        print(f"project: {project}")
    rc = _require_store(cfg.store_path, "ledger")
    if rc is not None:
        return rc
    store = LedgerStore(cfg.store_path)
    nodes = store.list_nodes()
    _report_skipped(store)
    if not nodes:
        print("ledger: no nodes")
        return 0
    for n in nodes:
        # A node whose state requires a ruling but has none is a defect
        # hiding in plain sight -- the same condition validate() would
        # refuse a *new* write for, so list must not let an existing one
        # go unmarked. Uses the SAME predicate preamble.py ranks by (see
        # ledger/__init__.py's is_unruled) -- Finding 4 was exactly the
        # two modules disagreeing about this.
        marker = " [UNRULED]" if is_unruled(n) else ""
        print(f"{n.name}: {n.type} {n.state}{marker}")
    return 0


def _cmd_ledger_show(args) -> int:
    cfg, project = _resolve(args)
    if project:
        print(f"project: {project}")
    rc = _require_store(cfg.store_path, "ledger")
    if rc is not None:
        return rc
    store = LedgerStore(cfg.store_path)
    try:
        node = store.read(args.name)
    except LedgerError as e:
        # A name that resolves outside ledger/ is a refusal, not a crash --
        # this is the CLI-reachable path the traversal fix exists for.
        print(f"ledger: refused - {e}")
        return 2
    except FileNotFoundError:
        print(f"ledger: no such node {args.name!r}")
        return 1

    print(f"{node.name}: {node.type} {node.state}")
    print(f"title: {node.title}")
    if node.ruling:
        print(f"ruling: {node.ruling}")
    if node.blocked_by:
        print(f"blocked_by: {', '.join(node.blocked_by)}")
    if node.aliases:
        print(f"also known as: {', '.join(node.aliases)}")
    for i, c in enumerate(node.checklist, 1):
        mark = "x" if c.get("done") else " "
        note = f"  -- {c['note']}" if c.get("note") else ""
        print(f"[{mark}] {i}. {c['text']}{note}")
    for e in node.expects:
        clauses = ", ".join(f"{k}={v}" for k, v in e.items() if k not in ("url", "path", "last", "note"))
        last = e.get("last") or {}
        verdict = ("never checked" if not last
                   else f"{'met' if last.get('ok') else 'UNMET'} at {last.get('at')}: {last.get('detail')}")
        print(f"expects: {expect_subject(e)} ({clauses}) -- {verdict}")
    for r in node.relations:
        print(f"relation: {r.rel} {r.to}")
    for s in node.scores:
        readings = " -> ".join(
            f"{o.value} ({o.basis}, {o.on or 'undated'})" for o in s.series
        )
        print(f"score: {s.axis}: {readings}")
        if len(s.series) == 1:
            # Stated, not left silent: a reader looking for a direction
            # must be told it cannot be had yet, the same way a withheld
            # composite names its reason instead of printing nothing.
            print("    one observation -- no direction until this is re-scored")
    print()
    print(node.body)
    return 0


def _cmd_ledger_set(args) -> int:
    """MINOR 6: before this, there was no way to change a node's state
    through the gate -- marking an item `done` required a hand-edit of the
    .md file, which bypasses `validate()` entirely and makes the gate
    optional in practice (a hand-edited file can carry any state at all,
    including one `validate()` would refuse a fresh write for). `set`
    loads the existing node, applies only the fields the caller actually
    passed, and calls the SAME `LedgerStore.write()` -- the same gate, the
    same disk-untouched-on-refusal guarantee -- as `ledger new`.
    """
    cfg, project = _resolve(args)
    if project:
        print(f"project: {project}")
    rc = _require_store(cfg.store_path, "ledger")
    if rc is not None:
        return rc

    store = LedgerStore(cfg.store_path)
    try:
        node = store.read(args.name)
    except LedgerError as e:
        print(f"ledger: refused - {e}")
        return 2
    except FileNotFoundError:
        print(f"ledger: no such node {args.name!r}")
        return 1

    # Only fields the caller actually supplied on THIS invocation change --
    # and only those are named in the appended provenance entry, matching
    # `ledger new`'s own "supplied" convention (see _cmd_ledger_new).
    if args.body is not None and args.append_body is not None:
        print("ledger: --body and --append-body are mutually exclusive")
        return 2

    changes: dict = {}
    supplied: list[str] = []
    if args.state is not None:
        changes["state"] = args.state
        supplied.append("state")
    if args.ruling is not None:
        changes["ruling"] = args.ruling
        supplied.append("ruling")
    if args.blocked_by is not None:
        changes["blocked_by"] = tuple(args.blocked_by)
        supplied.append("blocked_by")
    if args.alias is not None:
        changes["aliases"] = tuple(args.alias)
        supplied.append("aliases")
    if args.body is not None:
        changes["body"] = args.body
        supplied.append("body")
    if args.append_body is not None:
        changes["body"] = node.body + "\n\n" + args.append_body
        supplied.append("body")

    if not supplied:
        print(
            "ledger: set requires at least one of --state, --ruling, "
            "--blocked-by, --alias, --body, --append-body"
        )
        return 2

    by = _session_by()
    today = _workspace(args).today.isoformat()
    changes["provenance"] = node.provenance + tuple(
        {"field": f, "by": f"human:{by}", "at": today} for f in supplied
    )
    changes["updated"] = today

    updated_node = replace(node, **changes)

    res = _workspace(args).write(updated_node, store)
    if not res.ok:
        # Every reason, not just the first -- same discipline as `ledger
        # new`'s refusal path, and the file is untouched: write() runs
        # validate() before anything reaches disk.
        for reason in res.reasons:
            print(f"ledger: refused - {reason}")
        return 1
    print(f"ledger: set {updated_node.name} ({updated_node.type}, {updated_node.state})")
    return _after_write(res)


# Spec S8.1 defines the real budget as `min(ceiling, share_of_context *
# context_window)`, config.toml-editable and scaled to the model in use.
# That config machinery is a separate, not-yet-built slice; this default
# is a plain fixed fallback (--budget overrides it) so `ledger preamble`
# is usable today rather than blocked on it.
_DEFAULT_PREAMBLE_BUDGET = 4000


def _cmd_ledger_lens(args) -> int:
    """`ledger lens <name>` -- the ledger asked a question.

    RENDERS `build_lens`; it does not re-derive it. The query used to be
    written out here AND in the MCP server, and the two had drifted: this
    one grew a `--flag` filter the agent-facing surface never got, and
    they disagreed about what to say when the result was empty.
    """
    ws = _workspace(args)
    if ws.name:
        print(f"project: {ws.name}")
    rc = _require_store(ws.store_path, "ledger")
    if rc is not None:
        return rc

    try:
        body = build_lens(
            ws.ledger(), args.name, ws.clock,
            hops=args.hops, flag_filter=tuple(args.flag or ()),
        )
    except BoardError as e:
        print(f"ledger: {e}")
        return 2

    for line in body["skipped_lines"]:
        print(f"ledger: {line}")
    for line in lens_lines(body):
        print(line)
    return 0


def _cmd_ledger_matrix(args) -> int:
    """`ledger matrix <rows> <cols>` -- two lenses crossed.

    Prints the grid with an explicit mark per cell and, per column, the
    coverage count. The count is the point (spec Sec.4.4): a number
    somebody counted by hand goes stale the moment the estate moves,
    and nobody knows when it did. This one is recomputed on every run.
    """
    cfg, project = _resolve(args)
    if project:
        print(f"project: {project}")
    rc = _require_store(cfg.store_path, "ledger")
    if rc is not None:
        return rc

    lenses = []
    for name in (args.rows, args.cols):
        lens = LENSES.get(name)
        if lens is None:
            print(
                f"ledger: no such lens {name!r} -- known lenses: "
                f"{', '.join(sorted(LENSES))}"
            )
            return 2
        lenses.append(lens)
    row_lens, col_lens = lenses

    store = LedgerStore(cfg.store_path)
    graph, dangling = load_graph(store)
    _report_skipped(store, dangling)

    if args.hops < 1:
        print("ledger: --hops must be at least 1")
        return 2
    m = cross(graph, row_lens, col_lens, _workspace(args).clock, args.hops)
    print(f"matrix: {row_lens.name} x {col_lens.name}")
    if not m.rows or not m.cols:
        # Not a crash and not a blank page: say which side is empty, or
        # a reader cannot tell an empty estate from a broken command.
        missing = []
        if not m.rows:
            missing.append(f"no {row_lens.name} nodes")
        if not m.cols:
            missing.append(f"no {col_lens.name} nodes")
        print(f"matrix: {' and '.join(missing)}")
        return 0

    width = max(len(r) for r in m.rows)
    header = " " * width + "  " + "  ".join(m.cols)
    print(header)
    for r in m.rows:
        marks = []
        for c in m.cols:
            cell = m.cell(r, c)
            mark = "#" if cell.covered else "."
            if any(f.kind != "absent" for f in cell.flags):
                mark = "!"
            marks.append(mark.center(len(c)))
        print(f"{r.ljust(width)}  " + "  ".join(marks))
    print()
    for c in m.cols:
        covered = [r for r in m.rows if m.cell(r, c).covered]
        print(f"{c}: {len(covered)} of {len(m.rows)} {row_lens.name}s"
              + (f" - {', '.join(covered)}" if covered else ""))
    print()
    print("legend: # covered, ! covered with a flag, . absent")
    return 0


# One character per basis, so the CLI grid carries the basis as TEXT and
# not only as a position. The board carries it as texture AND a word; a
# terminal has neither, so the mark is the channel.
_BASIS_MARK = {"measured": "*", "asserted": "~", "unverified": "?"}
# Freshness is a SECOND mark, appended, never a replacement for the basis
# one: a stale measurement is still a measurement somebody took, and a
# cell that dropped the `*` would lose the more important half. A terminal
# has no colour and no texture, so the mark is the only channel there is
# and the legend below spells both of these out.
_FRESHNESS_MARK = {"stale": "!", "undated": "#"}


def _active_project(args):
    """The registry entry for the active project, or None when there is no
    registry. Reads what `_workspace` already resolved rather than loading
    the registry a second time."""
    return _workspace(args).project


def _cmd_projects_axes(args) -> int:
    """Show or declare the capability axes this project is assessed on.

    Declared per project on purpose. A built-in axis list would be this
    package shipping one organisation's assessment framework, which is
    the same defect as a hardcoded customer name -- and the axes that
    matter for a publishing tool are not the ones that matter for a
    dictionary or a book.
    """
    reg_path = _registry_path()
    reg = load_registry(reg_path)
    proj = _active_project(args)
    if proj is None:
        print("projects: no registry -- add a project first with `spoke projects add`")
        return 2

    if args.max is not None:
        if args.max < 1:
            print(f"projects: --max must be at least 1, got {args.max}")
            return 2
        proj = replace(proj, scale_max=args.max)
        reg[proj.name] = proj
        save_registry(reg_path, reg)
        print(f"project: {proj.name}\nscale: 0-{proj.scale_max}")
        if not args.set:
            return 0

    if not args.set:
        print(f"project: {proj.name}")
        print(f"scale: 0-{proj.scale_max}")
        if not proj.axes:
            print(
                "axes: none declared -- declare them with "
                "`spoke projects axes --set 'reliability=Reliability:30'` "
                "(key=Label:weight, repeatable)"
            )
            return 0
        total = sum(a.weight for a in proj.axes) or 1
        for a in proj.axes:
            print(f"{a.key}: {a.label} (weight {a.weight:g}, {a.weight / total * 100:.0f}%)")
        return 0

    axes = list(proj.axes)
    for spec in args.set:
        key, sep, rest = spec.partition("=")
        key = key.strip()
        label, _, weight_s = rest.partition(":")
        label = label.strip() or key
        if not sep or not key:
            print(f"projects: --set {spec!r} is not of the form key=Label:weight")
            return 2
        try:
            weight = float(weight_s) if weight_s.strip() else 1.0
        except ValueError:
            print(f"projects: --set {spec!r}: weight {weight_s!r} is not a number")
            return 2
        if weight < 0:
            print(f"projects: --set {spec!r}: a weight cannot be negative")
            return 2
        new = Axis(key=key, label=label, weight=weight)
        # Replace in place rather than append, so re-declaring an axis
        # does not silently move it to the end of the reading order.
        for i, existing in enumerate(axes):
            if existing.key == key:
                axes[i] = new
                break
        else:
            axes.append(new)

    reg[proj.name] = replace(proj, axes=tuple(axes))
    save_registry(reg_path, reg)
    print(f"project: {proj.name}")
    total = sum(a.weight for a in axes) or 1
    for a in axes:
        print(f"{a.key}: {a.label} (weight {a.weight:g}, {a.weight / total * 100:.0f}%)")
    return 0


def _cmd_ledger_score(args) -> int:
    """Record one capability score on one node, through the same gate.

    Authored, never inferred: neither the repo scan nor a model writes a
    score. `--basis` is required and has no default -- a score whose
    basis defaulted to "measured" would claim somebody looked when
    nobody did, which is the exact lie this layer exists to prevent.
    """
    cfg, project = _resolve(args)
    if project:
        print(f"project: {project}")
    rc = _require_store(cfg.store_path, "ledger")
    if rc is not None:
        return rc
    proj = _active_project(args)
    axes = tuple(proj.axes) if proj else ()

    store = LedgerStore(cfg.store_path)
    try:
        node = store.read(args.name)
    except LedgerError as e:
        print(f"ledger: refused - {e}")
        return 2
    except FileNotFoundError:
        print(f"ledger: no such node {args.name!r}")
        return 1

    on = args.on or _workspace(args).today.isoformat()
    superseded = next((s for s in node.scores if s.axis == args.axis), None)
    previously: tuple[Observation, ...] = ()
    if superseded is not None:
        previously = superseded.previously
        # Same value, same basis, same day is the SAME observation, not a
        # second one -- a series padded by idempotent re-runs would show a
        # direction nobody measured.
        if (superseded.value, superseded.basis, superseded.on) != (args.value, args.basis, on):
            previously += (Observation(
                superseded.value, superseded.basis, superseded.on,
                superseded.note, superseded.by,
            ),)

    scores = [s for s in node.scores if s.axis != args.axis]
    scores.append(Score(
        axis=args.axis, value=args.value, basis=args.basis, on=on,
        note=args.note or None, by=f"human:{_session_by()}",
        previously=previously,
    ))
    # Declaration order, so the stored file reads in the same order the
    # scale renders -- a file a human opens should not be shuffled.
    order = {a.key: i for i, a in enumerate(axes)}
    scores.sort(key=lambda s: (order.get(s.axis, len(order)), s.axis))

    updated = replace(node, scores=tuple(scores), updated=_workspace(args).today.isoformat())
    res = _workspace(args).write(updated, store)
    if not res.ok:
        for reason in res.reasons:
            print(f"ledger: refused - {reason}")
        return 2
    print(f"ledger: scored {updated.name} {args.axis}={args.value} ({args.basis})")
    return _after_write(res)


def _cmd_scale(args) -> int:
    """The capability grid, and a composite per node -- or its refusal.

    RENDERS `build_scale`; it does not recompute it. This used to be a
    second implementation, and the two had already drifted: the terminal
    sourced nodes from `list_nodes()` where the deep module uses
    `load_graph`, so a dangling relation never reached the terminal at
    all; and it called `composite()` without a threshold, so the two
    agreed only for as long as the default did.

    The refusal is still the point. A number averaged over only the axes
    somebody happened to measure reads as a score for the whole thing.
    """
    ws = _workspace(args)
    if ws.name:
        print(f"project: {ws.name}")
    rc = _require_store(ws.store_path, "scale")
    if rc is not None:
        return rc
    # The malformed request first, the project's state second. Telling
    # someone to declare axes when their command was invalid answers a
    # question they did not ask -- and this is the SAME guard the HTTP
    # surface uses, not a second copy of the sentence.
    try:
        _require_node_type(args.type)
    except BoardError as e:
        print(f"scale: {e}")
        return 2
    try:
        body = build_scale(
            ws.ledger(), ws.axes, ws.clock,
            node_type=args.type, vocabulary=ws.vocabulary, max_value=ws.scale_max,
        )
    except BoardError as e:
        print(f"scale: {e}")
        return 2

    # BEFORE the axes guidance. A file this store could not read is worth
    # knowing whether or not anyone has declared axes yet -- returning
    # early on "no axes" swallowed it, which is the shape of failure this
    # tool exists to remove, in the command that exists to report it.
    for line in body["skipped_lines"]:
        print(f"scale: {line}")

    if not ws.axes:
        # An undeclared axis set is a state of the PROJECT, not a bad
        # argument, so it is reported rather than refused.
        print(
            "scale: this project has declared no capability axes -- declare them "
            "with `spoke projects axes --set 'reliability=Reliability:30'`. "
            "There is deliberately no built-in list: the axes that matter are "
            "the project's, not this tool's."
        )
        return 0

    rows = body["rows"]
    if not rows:
        print(f"scale: no {body['kind']} nodes to score")
        return 0

    axes = body["axes"]
    width = max(len(r["name"]) for r in rows)
    print(" " * width + "  " + "  ".join(a["label"] for a in axes))
    for row in rows:
        marks = []
        for cell, axis in zip(row["cells"], axes):
            if cell["value"] is None:
                # NOT the same as a zero, and it must not look like one.
                mark = "--"
            else:
                mark = (f"{cell['value']}{_BASIS_MARK[cell['basis']]}"
                        f"{_FRESHNESS_MARK.get(cell['freshness'], '')}")
            marks.append(mark.center(len(axis["label"])))
        print(f"{row['name'].ljust(width)}  " + "  ".join(marks))
    print()
    print("basis: " + ", ".join(f"{_BASIS_MARK[b]} {b}" for b in body["bases"])
          + ";  -- not scored (which is not a zero)")
    measured_for = body["score_stale_days"].get("measured")
    print(
        f"freshness: ! stale (measured over {measured_for}d ago), "
        "# undated (measured, no observation date) -- NEITHER counts toward "
        "a composite, so one can rise when a score ages out. Unmarked "
        "asserted and unverified scores never go stale: neither was an "
        "observation, so no date can make it untrue."
    )
    print()
    for row in rows:
        if row["composite"] is None:
            print(f"{row['name']}: NO COMPOSITE -- {row['withheld_reason']}")
        else:
            print(
                f"{row['name']}: {row['composite']:.1f} / {body['max_value']} "
                f"({row['measured_weight'] * 100:.0f}% of the weight measured, "
                f"threshold {row['threshold'] * 100:.0f}%)"
            )
            if row["missing"]:
                print(f"    not counted: {', '.join(row['missing'])}")
    return 0


def _cmd_projects_vocabulary(args) -> int:
    """Accept (or list) the words this project uses for each node type.

    The ledger's `type` never changes -- it is the engine's closed six.
    This is only what the surfaces RENDER, so a project that calls its
    units chapters can say so without the lens code learning the word.
    Once accepted, the scan stops proposing it.
    """
    from .ledger import NODE_TYPES

    reg_path = _registry_path()
    reg = load_registry(reg_path)
    requested = getattr(args, "project", None) or os.environ.get("SPOKE_PROJECT")
    proj = select_project(reg, requested)

    if not args.set:
        print(f"project: {proj.name}")
        if not proj.vocabulary:
            print("vocabulary: none accepted -- every surface shows the engine's own words")
            return 0
        for engine_type, word in sorted(proj.vocabulary.items()):
            print(f"{engine_type}: {word}")
        return 0

    vocab = dict(proj.vocabulary)
    for pair in args.set:
        engine_type, sep, word = pair.partition("=")
        engine_type, word = engine_type.strip(), word.strip()
        if not sep or not engine_type or not word:
            print(f"projects: --set {pair!r} is not of the form <type>=<word>")
            return 2
        if engine_type not in NODE_TYPES:
            # Refused, not silently stored: a word mapped to a type that
            # does not exist would never render anywhere, and the user
            # would have no way to tell that from it working.
            print(
                f"projects: unknown node type {engine_type!r} -- must be one of "
                f"{', '.join(NODE_TYPES)}"
            )
            return 2
        vocab[engine_type] = word

    reg[proj.name] = replace(proj, vocabulary=vocab)
    save_registry(reg_path, reg)
    print(f"project: {proj.name}")
    for engine_type, word in sorted(vocab.items()):
        print(f"{engine_type}: {word}")
    return 0


def _cmd_scan(args) -> int:
    """`spoke scan [--write]` -- derive the map from the project's repos.

    Without `--write` it prints and touches nothing. That is the default
    on purpose: a map the human has not looked at is a guess wearing a
    schema, and the point of this tool is to stop those from accumulating
    unexamined.

    The re-run rule is what makes the map derived rather than
    accumulated. A node the scan owns -- every provenance entry is this
    scanner's AND the body still hashes to what it recorded -- is
    re-derived. Anything else is DIVERGED: reported with what the scan
    would have said, and left exactly as it is. A human's edit to a
    markdown file in a markdown store is the expected case, not an error,
    and it must survive the next scan.
    """
    cfg, project_name = _resolve(args)
    reg = load_registry(_registry_path())
    proj = None
    if reg:
        requested = getattr(args, "project", None) or os.environ.get("SPOKE_PROJECT")
        proj = select_project(reg, requested)
    if proj is None or not proj.repos:
        print(
            "scan: no repos to scan -- register one with "
            "`spoke projects add <name> --repo <path>`"
        )
        return 2
    print(f"project: {proj.name}")

    rc = _require_store(cfg.store_path, "scan")
    if rc is not None:
        return rc

    result = scan_repos(list(proj.repos), _workspace(args).today, proj.vocabulary)
    for note in result.notes:
        print(f"scan: note - {note}")

    if result.proposed_vocabulary:
        # Criterion 4: a reader must never be shown a type name this
        # project's authors invented. Say which words are still guesses
        # and exactly how to accept them.
        for engine_type, word in sorted(result.proposed_vocabulary.items()):
            print(
                f"scan: proposes calling {engine_type} nodes {word!r} -- accept with "
                f"`spoke projects vocabulary --set '{engine_type}={word}' "
                f"--project {proj.name}`"
            )

    store = LedgerStore(cfg.store_path)
    applied = apply(result, store, _workspace(args).axes, _workspace(args).scale_max,
                    _workspace(args).today, write=args.write)
    label_of = {p.node.name: (p.label, p.accepted_label) for p in result.proposals}
    for line in applied.lines:
        name = line[2:].split(":", 1)[0]
        label, accepted = label_of.get(name, (None, True))
        mark = f" [{label}]" + ("" if accepted else " (label proposed)") if label else ""
        # the label sits after the name, as it always did
        print(line.replace(f" {name}:", f" {name}{mark}:", 1))
        if not args.write and line.startswith("+ "):
            for e in next((p.evidence for p in result.proposals if p.node.name == name), ()):
                print(f"    from {e}")
    if args.verbose:
        for prop in sorted(result.proposals, key=lambda p: p.node.name):
            if not any(l[2:].startswith(prop.node.name + ":") for l in applied.lines):
                print(f"= {prop.node.name} [{prop.label}]: unchanged")

    total = len(result.proposals)
    c = applied
    if args.write:
        print(f"scan: {total} proposals - {c.written} written, {c.unchanged} unchanged, "
              f"{c.diverged} diverged, {c.withdrawn} withdrawn")
    else:
        print(f"scan: {total} proposals - {c.unchanged} already match, {c.diverged} diverged, "
              f"{c.withdrawn} would be withdrawn. Nothing written; re-run with --write.")
    return 0


def _cmd_ledger_preamble(args) -> int:
    cfg, project = _resolve(args)
    if project:
        print(f"project: {project}")
    rc = _require_store(cfg.store_path, "ledger")
    if rc is not None:
        return rc

    store = LedgerStore(cfg.store_path)
    nodes = store.list_nodes()
    _report_skipped(store)
    # --ledger-only: pass no memory index lines at all, rather than
    # passing them and having render_preamble's budget squeeze them out --
    # Claude Code (the intended caller, see spoke/hooks/ledger-preamble.sh)
    # already injects MEMORY.md at SessionStart and this hook cannot
    # prevent that, so reading and ranking the index here would cost real
    # work to reproduce content the session already has.
    if args.ledger_only:
        memory_index_lines: list[str] = []
    else:
        index_path = Store(cfg.store_path).index_path
        memory_index_lines = (
            [line for line in index_path.read_text().splitlines() if line.strip()]
            if index_path.exists() else []
        )
    text, stats = render_preamble(nodes, memory_index_lines, args.budget)
    # In --ledger-only mode an empty body (no open ledger items) renders
    # as just the footer line -- indistinguishable from "broken" unless
    # this says so explicitly. Scoped to --ledger-only: the default mode
    # always has memory index lines to show, so this condition cannot
    # arise there.
    if args.ledger_only and stats["shown"] == 0:
        print("ledger: no open ledger items")
    print(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="spoke")
    p.add_argument("--config")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--probe", action="store_true",
                   help="also probe every node's expectations now and record what was found "
                        "(doctor does this on its schedule)")
    c.add_argument("--config")
    c.add_argument("--project")
    c.set_defaults(func=_cmd_check)
    s = sub.add_parser("stale")
    s.add_argument("--config")
    s.add_argument("--project")
    s.set_defaults(func=_cmd_stale)
    sv = sub.add_parser("serve")
    sv.add_argument("--config")
    sv.add_argument("--project")
    sv.add_argument("--port", type=int, default=8765)
    sv.add_argument(
        "--install", action="store_true",
        help="keep the app running in the background (launchd), instead of "
        "running it in this terminal",
    )
    sv.add_argument(
        "--status", action="store_true",
        help="is it installed, loaded, AND actually answering?",
    )
    sv.add_argument("--uninstall", action="store_true", help="unload and remove it")
    sv.add_argument(
        "--open", dest="open_browser", action="store_true",
        help="open the app in a browser once it is serving",
    )
    sv.add_argument(
        "--dry-run", action="store_true",
        help="print the launchd agent and change nothing",
    )
    sv.set_defaults(func=_cmd_serve)
    mc = sub.add_parser(
        "mcp",
        help="run the MCP server over stdio, exposing the ledger/memory "
        "read and write tools to any MCP-capable agent through the same "
        "gate the CLI uses",
    )
    mc.add_argument(
        "--install", action="store_true",
        help="register this server with the MCP client instead of running it",
    )
    mc.add_argument(
        "--status", action="store_true",
        help="is it registered, AND does the client actually manage to start it?",
    )
    mc.add_argument("--uninstall", action="store_true", help="remove the registration")
    mc.add_argument(
        "--scope", default="user", choices=["local", "user", "project"],
        help="where the registration lives (default: user, i.e. every project)",
    )
    mc.add_argument("--name", default="spoke", help="the name to register it under")
    mc.add_argument(
        "--dry-run", action="store_true",
        help="print the registration and change nothing",
    )
    mc.add_argument("--config")
    mc.add_argument("--project")
    mc.set_defaults(func=_cmd_mcp)

    dm = sub.add_parser(
        "demo",
        help="build a map of two real repos (Tare and Forage) under a directory, "
             "showing everything the product can say; then print how to serve it",
    )
    dm.add_argument("dir", help="where the demo lives: repos, store, registry, config")
    dm.add_argument("--tare", help="a local checkout to use instead of cloning")
    dm.add_argument("--forage", help="a local checkout to use instead of cloning")
    dm.set_defaults(func=_cmd_demo)

    hk = sub.add_parser(
        "hooks",
        help="install the SessionStart/Stop hooks that surface the ledger",
    )
    hk.add_argument("--install", action="store_true",
                    help="register both hooks in the settings file")
    hk.add_argument("--status", action="store_true",
                    help="report whether they are registered AND can run")
    hk.add_argument("--uninstall", action="store_true",
                    help="remove only this project's hooks, leaving others")
    hk.add_argument("--settings", default=None,
                    help="settings file to change (default: ~/.claude/settings.json)")
    hk.add_argument("--dry-run", action="store_true",
                    help="print what the settings file would become, change nothing")
    hk.set_defaults(func=_cmd_hooks)
    d = sub.add_parser("doctor")
    d.add_argument("--config")
    d.add_argument("--project")
    d.add_argument("--state")
    d.set_defaults(func=_cmd_doctor)
    x = sub.add_parser("contradictions")
    x.add_argument("--config")
    x.add_argument("--project")
    x.set_defaults(func=_cmd_contradictions)
    sc = sub.add_parser("schedule")
    sc.add_argument("--install", action="store_true")
    sc.add_argument("--uninstall", action="store_true")
    sc.add_argument("--status", action="store_true")
    sc.add_argument("--project")
    sc.add_argument("--config")
    sc.add_argument("--hour", type=int)
    sc.add_argument("--minute", type=int)
    sc.add_argument("--label-prefix")
    sc.set_defaults(func=_cmd_schedule)
    pr = sub.add_parser("projects")
    prs = pr.add_subparsers(dest="pcmd", required=True)
    pa = prs.add_parser("add")
    pa.add_argument("name")
    pa.add_argument("--repo", action="append", required=True)
    pa.add_argument("--memory-store")
    pa.add_argument(
        "--accept", action="append", default=None,
        help="an absence this project has explicitly accepted (repeatable). "
        "Omit entirely to leave this NOT DECLARED (falls back to the base "
        "config's accepted_absences); the CLI has no flag for the third "
        "state (DECLARED EMPTY, i.e. accept nothing) -- edit the registry "
        "file's accepted_absences = [] by hand for that.",
    )
    pa.set_defaults(func=_cmd_projects_add)
    pl = prs.add_parser("list")
    pl.set_defaults(func=_cmd_projects_list)
    pr_rm = prs.add_parser(
        "remove",
        help="forget a project's registration -- never touches its repos "
        "or its memory store",
    )
    pr_rm.add_argument("name")
    pr_rm.set_defaults(func=_cmd_projects_remove)
    pax = prs.add_parser(
        "axes",
        help="show or declare the capability axes this project is assessed "
        "on; there is deliberately no built-in list",
    )
    pax.add_argument(
        "--set", action="append", default=None, metavar="KEY=LABEL:WEIGHT",
        help="declare an axis, e.g. --set 'reliability=Reliability:30' "
        "(repeatable; re-declaring one keeps its position)",
    )
    pax.add_argument(
        "--max", type=int, default=None, metavar="N",
        help="the top of the score range (default 5); a project scoring 0-4 "
        "must say so or a full mark renders as 80%%",
    )
    pax.add_argument("--project")
    pax.set_defaults(func=_cmd_projects_axes)
    pv = prs.add_parser(
        "vocabulary",
        help="show or accept this project's own word for each node type; "
        "surfaces render the word, the ledger keeps the engine's type",
    )
    pv.add_argument(
        "--set", action="append", default=None, metavar="TYPE=WORD",
        help="accept a word, e.g. --set product=app (repeatable)",
    )
    pv.add_argument("--project")
    pv.set_defaults(func=_cmd_projects_vocabulary)
    pw = prs.add_parser(
        "which",
        help="print the registered project that owns a directory, or a "
        "clear non-zero-exit message when there is none or it is "
        "ambiguous -- what the SessionStart hook uses to turn a directory "
        "into the --project name",
    )
    pw.add_argument("path")
    pw.set_defaults(func=_cmd_projects_which)
    scl = sub.add_parser(
        "scale",
        help="the capability grid, and a composite per node -- or the "
        "stated reason there is none",
    )
    scl.add_argument(
        "--type", default="product",
        help="which node type to score (default: product)",
    )
    scl.add_argument("--config")
    scl.add_argument("--project")
    scl.set_defaults(func=_cmd_scale)
    sn = sub.add_parser(
        "scan",
        help="derive the map from the project's repositories; prints "
        "proposals and writes nothing unless --write is given",
    )
    sn.add_argument(
        "--write", action="store_true",
        help="write the proposals into the ledger, through the same gate a "
        "human meets, and withdraw (set abandoned, with a dated ruling) any "
        "node the scan wrote, nobody edited, and it no longer derives. "
        "Without this, scan only prints.",
    )
    sn.add_argument(
        "--verbose", action="store_true",
        help="also list the nodes that already match",
    )
    sn.add_argument("--config")
    sn.add_argument("--project")
    sn.set_defaults(func=_cmd_scan)
    lg = sub.add_parser("ledger")
    lgs = lg.add_subparsers(dest="lcmd", required=True)
    lgn = lgs.add_parser("new")
    lgn.add_argument("name")
    lgn.add_argument("--type", required=True)
    lgn.add_argument("--state", required=True)
    lgn.add_argument("--title", required=True)
    lgn.add_argument("--body", required=True)
    lgn.add_argument("--ruling")
    lgn.add_argument(
        "--blocked-by", dest="blocked_by", action="append", default=None,
        help="name of a node blocking this one (repeatable)",
    )
    lgn.add_argument(
        "--alias", action="append", default=None,
        help="a FORMER name this node answers to (repeatable). A shipped "
        "name is not erased by a rename; references written before it "
        "still point at the old one, and this is how they keep resolving.",
    )
    lgn.add_argument(
        "--rel", action="append", default=None,
        help="a relation to another node, as <type>:<node> (repeatable)",
    )
    lgn.add_argument("--config")
    lgn.add_argument("--project")
    lgn.add_argument("--questions", action="store_true",
                     help="seed the task ledger's four questions as this node's checklist")
    lgn.add_argument("--check", action="append", default=None, metavar="TEXT",
                     help="a checklist item (repeatable); the node cannot close until every item is ticked")
    lgn.set_defaults(func=_cmd_ledger_new)

    lgt = lgs.add_parser("tick", help="tick one checklist item on a node")
    lgt.add_argument("name")
    lgt.add_argument("item", type=int, help="1-based item number, as `ledger show` lists them")
    lgt.add_argument("--undo", action="store_true", help="untick it instead")
    lgt.add_argument("--note", help="what was observed, in one line")
    lgt.add_argument("--config")
    lgt.add_argument("--project")
    lgt.set_defaults(func=_cmd_ledger_tick)

    lge = lgs.add_parser("expect", help="state what a node expects to be observably true; doctor probes it")
    lge.add_argument("name")
    lge.add_argument("--url", help="the address the claim is checked at (http/https)")
    lge.add_argument("--path", help="a git repository on this machine the claim is checked in "
                                    "(absolute path; instead of --url)")
    lge.add_argument("--worktrees-max", dest="worktrees_max", type=int,
                     help="with --path: at most this many linked worktrees -- sessions add them per "
                          "task and nothing reaps them, so the count only grows unless something says stop")
    lge.add_argument("--status", type=int, help="expected HTTP status (default 200)")
    lge.add_argument("--contains", help="text the body must contain")
    lge.add_argument("--fresh-within", dest="fresh_within",
                     help="the page must be this fresh: '36h', '2d' -- judged by Last-Modified, "
                          "or by the date in the body after --dated-by")
    lge.add_argument("--dated-by", dest="dated_by",
                     help="text the page's own date follows, e.g. 'Beat: tech · ' -- for a page "
                          "that states its date in the body and sends no Last-Modified")
    lge.add_argument("--note", help="why this is the observable thing, in one line")
    lge.add_argument("--config")
    lge.add_argument("--project")
    lge.set_defaults(func=_cmd_ledger_expect)
    lgl = lgs.add_parser("list")
    lgl.add_argument("--config")
    lgl.add_argument("--project")
    lgl.set_defaults(func=_cmd_ledger_list)
    lgsh = lgs.add_parser("show")
    lgsh.add_argument("name")
    lgsh.add_argument("--config")
    lgsh.add_argument("--project")
    lgsh.set_defaults(func=_cmd_ledger_show)
    lgset = lgs.add_parser("set", help="change an existing node's state/ruling/blocked_by/body through the same gate `ledger new` uses")
    lgset.add_argument("name")
    lgset.add_argument("--state")
    lgset.add_argument("--ruling")
    lgset.add_argument(
        "--alias", action="append", default=None,
        help="a FORMER name this node answers to (repeatable); replaces "
        "the existing list",
    )
    lgset.add_argument(
        "--blocked-by", dest="blocked_by", action="append", default=None,
        help="name of a node blocking this one (repeatable); replaces the existing list",
    )
    lgset.add_argument(
        "--append-body", dest="append_body", default=None,
        help="append TEXT to the existing body, separated by a blank line "
        "(the common case: a ledger is a running record). Mutually "
        "exclusive with --body.",
    )
    lgset.add_argument(
        "--body", default=None,
        help="replace the body outright. For correcting something wrong, "
        "not for progress -- use --append-body for that. Mutually "
        "exclusive with --append-body.",
    )
    lgset.add_argument("--config")
    lgset.add_argument("--project")
    lgset.set_defaults(func=_cmd_ledger_set)
    lgsc = lgs.add_parser(
        "score",
        help="record one capability score on a node, through the same gate",
    )
    lgsc.add_argument("name")
    lgsc.add_argument("--axis", required=True, help="an axis key this project has declared")
    lgsc.add_argument("--value", type=int, required=True,
                      help=f"0 to the project's declared top (`projects axes --max`; "
                           f"{DEFAULT_MAX_VALUE} unless it says otherwise)")
    lgsc.add_argument(
        "--basis", required=True, choices=list(BASES),
        help="where the number came from. Required, with NO default: a "
        "basis that defaulted to 'measured' would claim somebody looked "
        "when nobody did.",
    )
    lgsc.add_argument("--note", help="what was actually observed, in one line")
    lgsc.add_argument("--on", help="ISO date of the observation (default: today)")
    lgsc.add_argument("--config")
    lgsc.add_argument("--project")
    lgsc.set_defaults(func=_cmd_ledger_score)
    lgle = lgs.add_parser(
        "lens",
        help="ask the ledger a question through one lens: hub nodes, "
        "their flags with origins, and their members",
    )
    lgle.add_argument("name", help=f"one of: {', '.join(sorted(LENSES))}")
    lgle.add_argument(
        "--flag", action="append", default=None,
        help="show only hub nodes carrying this flag kind (repeatable)",
    )
    lgle.add_argument(
        "--hops", type=int, default=1,
        help="how far from each hub node to pull members in (default 1). "
        "Widens the flags by the same radius.",
    )
    lgle.add_argument("--config")
    lgle.add_argument("--project")
    lgle.set_defaults(func=_cmd_ledger_lens)
    lgm = lgs.add_parser(
        "matrix",
        help="cross two lenses into a coverage grid, with the counts "
        "recomputed rather than remembered",
    )
    lgm.add_argument("rows", help=f"one of: {', '.join(sorted(LENSES))}")
    lgm.add_argument("cols", help=f"one of: {', '.join(sorted(LENSES))}")
    lgm.add_argument(
        "--hops", type=int, default=1,
        help="how far from each hub a node counts as covered (default 1). "
        "The HTTP surface has always accepted this; the terminal could "
        "not widen a matrix at all.",
    )
    lgm.add_argument("--config")
    lgm.add_argument("--project")
    lgm.set_defaults(func=_cmd_ledger_matrix)
    lgp = lgs.add_parser("preamble")
    lgp.add_argument(
        "--budget",
        type=int,
        default=_DEFAULT_PREAMBLE_BUDGET,
        help=(
            f"token budget for the preamble (default {_DEFAULT_PREAMBLE_BUDGET}). "
            "This is a fixed fallback, not yet scaled to the model's context "
            "window -- see spec S8.1 for the real min(ceiling, share_of_context "
            "* context_window) budget this will become."
        ),
    )
    lgp.add_argument(
        "--ledger-only",
        action="store_true",
        help=(
            "Render ledger items ONLY -- pass no memory index lines into "
            "the budget at all (default: off). WHY: Claude Code already "
            "injects MEMORY.md into the session at SessionStart and this "
            "hook cannot prevent that, so re-rendering the same index "
            "lines here is pure duplication, not a service -- measured "
            "against a real store, memory index lines were 123 of 126 "
            "rendered lines (nearly all of the budget) for a handful of lines of "
            "actually new information. Off by default so the shared-"
            "budget ranking (ledger items and memory index lines "
            "competing in one budget -- spec S8.1) is unchanged for any "
            "host that does NOT inject the index itself."
        ),
    )
    lgp.add_argument("--config")
    lgp.add_argument("--project")
    lgp.set_defaults(func=_cmd_ledger_preamble)
    args = p.parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, RegistryError) as e:
        # A misconfiguration is an expected operational condition, not a crash.
        # It gets the same clean, actionable form as an unusable store path --
        # a traceback tells the reader nothing they can act on.
        print(f"config: {e}")
        return 2
    except LedgerError as e:
        # Backstop for any ledger operation that raises rather than
        # returning a typed refusal (read() does, on an unsafe name) -- a
        # command that forgets to catch it locally still reports cleanly
        # here instead of a traceback.
        print(f"ledger: refused - {e}")
        return 2


if __name__ == "__main__":
    import sys
    sys.exit(main())
