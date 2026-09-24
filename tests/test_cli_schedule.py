"""CLI wiring for `spoke schedule`. conftest.py's autouse fixture
already isolates SPOKE_REGISTRY, SPOKE_LAUNCH_AGENTS_DIR,
SPOKE_SCHEDULE_STATE_DIR and SPOKE_SCHEDULE_LOG_DIR to per-test temp
paths; these tests additionally monkeypatch spoke.schedule._run_launchctl
so no real `launchctl` (absent entirely on the Linux CI runner) is ever
invoked.
"""
import subprocess
import plistlib
from spoke.cli import main
from spoke.projects import add_project


def _fake_launchctl(monkeypatch, returncode=0):
    def run(args):
        return subprocess.CompletedProcess(args, returncode, "", "")
    monkeypatch.setattr("spoke.schedule._run_launchctl", run)


def test_schedule_status_on_a_missing_agent(monkeypatch, capsys):
    monkeypatch.setenv("SPOKE_LAUNCHD_LABEL_PREFIX", "com.example")
    assert main(["schedule", "--status"]) == 0
    out = capsys.readouterr().out
    assert "installed: NO" in out


def test_schedule_install_refuses_with_several_projects_and_no_project_flag(
    tmp_path, monkeypatch, capsys
):
    reg = tmp_path / "no-such-registry.toml"
    add_project(reg, "one", [tmp_path / "one"])
    add_project(reg, "two", [tmp_path / "two"])
    monkeypatch.setenv("SPOKE_REGISTRY", str(reg))
    monkeypatch.setenv("SPOKE_LAUNCHD_LABEL_PREFIX", "com.example")
    _fake_launchctl(monkeypatch)

    rc = main(["schedule", "--install"])
    assert rc == 2
    out = capsys.readouterr().out
    assert "config:" in out  # main()'s RegistryError -> "config: ..." handler
    assert "one" in out and "two" in out


def test_schedule_install_status_uninstall_round_trip(tmp_path, monkeypatch, capsys):
    reg = tmp_path / "no-such-registry.toml"
    add_project(reg, "estate", [tmp_path / "repo"])
    monkeypatch.setenv("SPOKE_REGISTRY", str(reg))
    monkeypatch.setenv("SPOKE_LAUNCHD_LABEL_PREFIX", "com.example")
    _fake_launchctl(monkeypatch)

    rc = main(["schedule", "--install", "--project", "estate"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "installed com.example.spoke" in out

    rc = main(["schedule", "--status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "installed: yes" in out
    assert "project estate" in out

    rc = main(["schedule", "--uninstall"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "removed com.example.spoke" in out

    rc = main(["schedule", "--status"])
    out = capsys.readouterr().out
    assert "installed: NO" in out


def test_schedule_install_honours_hour_and_minute_flags(tmp_path, monkeypatch, capsys):
    reg = tmp_path / "no-such-registry.toml"
    add_project(reg, "estate", [tmp_path / "repo"])
    monkeypatch.setenv("SPOKE_REGISTRY", str(reg))
    monkeypatch.setenv("SPOKE_LAUNCHD_LABEL_PREFIX", "com.example")
    monkeypatch.setenv("SPOKE_LAUNCH_AGENTS_DIR", str(tmp_path / "agents"))
    _fake_launchctl(monkeypatch)

    rc = main(["schedule", "--install", "--project", "estate", "--hour", "6", "--minute", "15"])
    assert rc == 0
    dest = tmp_path / "agents" / "com.example.spoke.plist"
    data = plistlib.loads(dest.read_bytes())
    assert data["StartCalendarInterval"][0] == {"Hour": 6, "Minute": 15}


def test_schedule_status_finds_a_non_default_prefix_with_no_flag_at_status_time(
    tmp_path, monkeypatch, capsys
):
    """CLI-level reproduction of the bug report: install once with an
    explicit --label-prefix, then call `schedule --status`/`--uninstall`
    with NEITHER --label-prefix NOR SPOKE_LAUNCHD_LABEL_PREFIX set --
    the install record found via conftest's isolated SPOKE_SCHEDULE_STATE_DIR
    must still make the agent visible."""
    reg = tmp_path / "no-such-registry.toml"
    add_project(reg, "estate", [tmp_path / "repo"])
    monkeypatch.setenv("SPOKE_REGISTRY", str(reg))
    _fake_launchctl(monkeypatch)

    rc = main(["schedule", "--install", "--project", "estate", "--label-prefix", "com.example"])
    assert rc == 0
    capsys.readouterr()

    monkeypatch.delenv("SPOKE_LAUNCHD_LABEL_PREFIX", raising=False)
    rc = main(["schedule", "--status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "installed: yes" in out
    assert "installed: NO" not in out
    assert "com.example.spoke" in out

    rc = main(["schedule", "--uninstall"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "com.example.spoke" in out

    rc = main(["schedule", "--status"])
    out = capsys.readouterr().out
    assert "installed: NO" in out


def test_schedule_install_reports_launchctl_load_failure_as_nonzero(tmp_path, monkeypatch, capsys):
    reg = tmp_path / "no-such-registry.toml"
    add_project(reg, "estate", [tmp_path / "repo"])
    monkeypatch.setenv("SPOKE_REGISTRY", str(reg))
    monkeypatch.setenv("SPOKE_LAUNCHD_LABEL_PREFIX", "com.example")
    _fake_launchctl(monkeypatch, returncode=1)

    rc = main(["schedule", "--install", "--project", "estate"])
    assert rc == 2
    out = capsys.readouterr().out
    assert "failed" in out.lower()
