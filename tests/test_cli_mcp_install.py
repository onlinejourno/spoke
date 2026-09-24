"""`spoke mcp --install` -- because the install was a wall.

Registering the server by hand meant knowing the absolute path of a
virtualenv interpreter, the module path, and the client's `mcp add`
syntax. The tool knows its own interpreter; the person should not have
to.

Two rules this holds to, both visible in the tests:

- **Spoke never hand-edits another program's config.** ~/.claude.json and
  .mcp.json belong to the client, their shape is the client's to change,
  and a tool that rewrites them is one release away from corrupting
  someone's whole MCP configuration. The client's own CLI is the seam.
- **Registered is not running.** Same discipline as `schedule --status`,
  which exists because "installed but has never actually fired" has to be
  visible.
"""
import json
from types import SimpleNamespace

import pytest

from spoke.cli import _mcp_install, _mcp_registration, _mcp_status, main


def _args(**kw):
    base = dict(name="spoke", scope="user", dry_run=False, project=None,
                install=False, status=False, uninstall=False, config=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_the_registration_names_an_absolute_interpreter():
    """A client starts a server with no virtualenv active, so a bare
    `python` would resolve to whatever happens to be on PATH."""
    import sys
    from pathlib import Path

    cmd = _mcp_registration()
    assert cmd[0] == sys.executable and Path(cmd[0]).is_absolute()
    assert cmd[1:] == ["-m", "spoke.cli", "mcp"]


def test_a_dry_run_changes_nothing_and_prints_both_forms(capsys, monkeypatch):
    ran = []
    monkeypatch.setattr("subprocess.run", lambda *a, **k: ran.append(a) or None)
    assert _mcp_install(_args(dry_run=True)) == 0
    out = capsys.readouterr().out
    assert ran == []                      # nothing was executed
    assert "mcp add spoke -s user" in out
    payload = json.loads(out[out.index("{"):])
    assert payload["mcpServers"]["spoke"]["args"] == ["-m", "spoke.cli", "mcp"]


def test_without_the_client_it_prints_the_registration_rather_than_guessing(
    capsys, monkeypatch
):
    """A tool that cannot find the client must not go looking for its
    config file and write one."""
    monkeypatch.setattr("shutil.which", lambda _n: None)
    ran = []
    monkeypatch.setattr("subprocess.run", lambda *a, **k: ran.append(a) or None)
    rc = _mcp_install(_args())
    out = capsys.readouterr().out
    assert rc == 1 and ran == []
    assert "not on PATH" in out and "nothing was changed" in out
    assert "mcpServers" in out


def test_install_drives_the_clients_own_cli(capsys, monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="Added stdio MCP server spoke",
                               stderr="")
    monkeypatch.setattr("shutil.which", lambda _n: "/usr/bin/claude")
    monkeypatch.setattr("subprocess.run", fake_run)

    assert _mcp_install(_args()) in (0, 1)     # status decides the exit code
    assert calls[0][:5] == ["/usr/bin/claude", "mcp", "add", "spoke", "-s"]
    assert "--" in calls[0]
    # and it checks the thing actually starts rather than stopping at "added"
    assert calls[-1][:3] == ["/usr/bin/claude", "mcp", "get"]
    assert "Checking it actually starts" in capsys.readouterr().out


def test_status_reports_a_registered_but_dead_server_as_a_failure(capsys, monkeypatch):
    """Registered is not running. A config line that produces a server
    the client cannot start is exactly the shape of failure this project
    exists to make visible."""
    monkeypatch.setattr("shutil.which", lambda _n: "/usr/bin/claude")
    monkeypatch.setattr("subprocess.run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout="spoke: ... ✗ Failed to connect", stderr=""))
    assert _mcp_status(_args()) == 1
    assert "Failed to connect" in capsys.readouterr().out


def test_status_reports_a_live_server_as_success(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _n: "/usr/bin/claude")
    monkeypatch.setattr("subprocess.run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout="spoke: ... ✓ Connected", stderr=""))
    assert _mcp_status(_args()) == 0


def test_the_project_is_carried_into_the_registration(capsys, monkeypatch):
    """With several projects registered, a server that does not name one
    would refuse at startup -- inside the client, where nobody sees it."""
    monkeypatch.setattr("shutil.which", lambda _n: None)
    monkeypatch.setattr("subprocess.run", lambda *a, **k: None)
    _mcp_install(_args(project="tulu"))
    assert "--project tulu" in capsys.readouterr().out


def test_install_never_writes_a_client_config_file(tmp_path, monkeypatch, capsys):
    """The strong form: no file outside the store is opened for writing."""
    opened = []
    real_open = open

    def watched_open(f, mode="r", *a, **k):
        if any(m in mode for m in ("w", "a", "+")):
            opened.append(str(f))
        return real_open(f, mode, *a, **k)

    monkeypatch.setattr("builtins.open", watched_open)
    monkeypatch.setattr("shutil.which", lambda _n: None)
    _mcp_install(_args())
    assert opened == [], opened


def test_mcp_without_a_flag_still_runs_the_server(monkeypatch):
    """The default must stay `serve` -- a client invokes `spoke mcp` with
    no flags and expects a protocol stream, not a helper."""
    import spoke.cli as cli

    started = []
    monkeypatch.setattr(cli, "_mcp_install", lambda a: started.append("install") or 0)
    monkeypatch.setattr("spoke.mcp_server.run_stdio", lambda s: started.append("serve"))
    monkeypatch.setenv("SPOKE_STORE_PATH", str(tmp := __import__("pathlib").Path("/nope")))
    main(["mcp"])
    assert "install" not in started
