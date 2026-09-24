"""`spoke serve --install` -- a launcher, and a status that tells the truth.

Running the app meant activating a virtualenv, remembering a port and
starting a process that dies with the terminal. This keeps it running.

The rule the tests hold to: **loaded is not serving.** `launchctl list`
saying it knows the label proves a plist was loaded, not that anything
answers. A server that died at 3am and stayed dead looks identical from
launchctl, which is the exact shape of failure this project exists to
make visible.
"""
from types import SimpleNamespace

import pytest

from spoke import schedule as sched
from spoke.cli import _cmd_serve_status, _serve_reachable, _serve_url


def _args(**kw):
    base = dict(port=8765, install=False, status=False, uninstall=False,
                dry_run=False, open_browser=False, project=None, config=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_the_serve_agent_has_its_own_label():
    """One label for two agents would have the second install silently
    replace the first, and the daily doctor run would stop with nothing
    saying so."""
    assert sched.serve_label_for("com.x") != sched.label_for("com.x")
    assert sched.serve_label_for("com.x").startswith(sched.label_for("com.x"))


def test_the_agent_keeps_the_app_alive_and_pins_the_port(tmp_path):
    import plistlib

    body = sched.build_serve_plist(
        label="com.x.spoke.serve", spoke_bin=tmp_path / "spoke", project="p",
        config_path=tmp_path / "config.toml", log_path=tmp_path / "log",
        port=9123, path_env="/usr/bin",
    )
    data = plistlib.loads(body)
    assert data["KeepAlive"] is True and data["RunAtLoad"] is True
    # the port is explicit: left implicit, --status would check a
    # different port from the one the agent serves
    assert "9123" in data["ProgramArguments"]
    assert data["ProgramArguments"][1] == "serve"


def test_status_is_a_real_request_not_a_launchctl_reading(capsys, monkeypatch):
    """The whole point. An agent that is loaded and a server that is dead
    are indistinguishable from launchctl alone."""
    monkeypatch.setattr(sched, "_run_launchctl",
                        lambda a: SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr("spoke.cli._serve_reachable", lambda port, **k: "Connection refused")
    rc = _cmd_serve_status(_args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "launchd knows" in out          # loaded...
    assert "NOT answering" in out          # ...and dead


def test_status_succeeds_only_when_the_app_actually_answers(capsys, monkeypatch):
    monkeypatch.setattr(sched, "_run_launchctl",
                        lambda a: SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr("spoke.cli._serve_reachable", lambda port, **k: None)
    assert _cmd_serve_status(_args()) == 0
    assert "answering at" in capsys.readouterr().out


def test_reachable_reports_why_rather_than_just_false():
    """A bare False would make 'wrong port' and 'crashed on startup' the
    same answer."""
    why = _serve_reachable(1, timeout=0.5)      # nothing listens on port 1
    assert why and isinstance(why, str)


def test_the_url_is_loopback_only():
    assert _serve_url(8765) == "http://127.0.0.1:8765/"


def test_a_dry_run_writes_no_plist(tmp_path, capsys, monkeypatch):
    from spoke.cli import _cmd_serve_install

    monkeypatch.setenv("SPOKE_LAUNCH_AGENTS", str(tmp_path / "agents"))
    monkeypatch.setattr(sched, "_run_launchctl",
                        lambda a: pytest.fail(f"launchctl called: {a}"))
    monkeypatch.setattr("spoke.cli._workspace", lambda a: SimpleNamespace(
        project=SimpleNamespace(name="p")))
    assert _cmd_serve_install(_args(dry_run=True)) == 0
    out = capsys.readouterr().out
    assert "dry run" in out and "KeepAlive" in out
    assert not (tmp_path / "agents").exists()


def test_a_foreign_server_on_the_same_port_is_not_reported_as_spoke(monkeypatch):
    """Found by running it. Another of the same person's projects was
    already listening on 8765, so `--status` -- which only checked that
    the port answered -- certified a stranger as Spoke running. A status
    that cannot tell who answered is a status that verifies nothing."""
    import json
    from contextlib import contextmanager

    class _Resp:
        status = 200
        def __init__(self, body): self._b = body
        def read(self): return self._b

    @contextmanager
    def fake_urlopen(url, timeout=None):
        yield _Resp(json.dumps({"app": "something-else"}).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    why = _serve_reachable(8765)
    assert why and "something else" in why


def test_spokes_own_answer_counts(monkeypatch):
    import json
    from contextlib import contextmanager

    class _Resp:
        status = 200
        def __init__(self, body): self._b = body
        def read(self): return self._b

    @contextmanager
    def fake_urlopen(url, timeout=None):
        assert url.endswith("/api/health"), url
        yield _Resp(json.dumps({"app": "spoke", "store": "/tmp/s"}).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert _serve_reachable(8765) is None
