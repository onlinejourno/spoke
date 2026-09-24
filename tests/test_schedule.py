"""spoke/schedule.py: builds and installs a launchd agent for `doctor`.

Every test here points SPOKE_LAUNCH_AGENTS_DIR at a temp directory and
injects a fake `run_launchctl` -- the real LaunchAgents directory and the
real `launchctl` binary (which does not even exist on the Linux CI runner)
must never be touched by the suite.
"""
import json
import plistlib
import textwrap
import pytest
from spoke import schedule
from spoke.projects import RegistryError, add_project


def _fake_launchctl(calls, returncode=0, stdout="", stderr=""):
    """Returns a callable matching subprocess.run's return shape, recording
    every invocation in `calls` instead of touching a real launchd."""
    import subprocess

    def run(args):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)
    return run


def _registry(tmp_path, name="estate", repo=None):
    reg_path = tmp_path / "projects.toml"
    add_project(reg_path, name, [repo or (tmp_path / "repo")])
    return reg_path


def test_label_for_uses_the_given_prefix():
    assert schedule.label_for("com.example") == "com.example.spoke"


def test_default_label_prefix_is_neutral_not_estate_identity():
    """The shipped default must name no real organisation -- the real
    label (e.g. com.example-org.spoke) is supplied by a CLI flag
    or config at install time on a real machine, never baked in here."""
    assert "example-org" not in schedule.DEFAULT_LABEL_PREFIX.lower()
    assert schedule.DEFAULT_LABEL_PREFIX == "com.local"


def test_spoke_bin_does_not_follow_a_symlinked_interpreter(tmp_path, monkeypatch):
    """Regression: a real install put a nonexistent path in the plist
    (.../Python.framework/Versions/3.14/bin/spoke) because
    Path(sys.executable).resolve() followed the venv's python symlink out
    to the real system interpreter -- a directory that has no
    spoke console script at all. Only the venv's own bin/ (next
    to sys.executable, unresolved) has it."""
    fake_venv_bin = tmp_path / "venv" / "bin"
    fake_venv_bin.mkdir(parents=True)
    real_interpreter = tmp_path / "real-python-elsewhere"
    real_interpreter.write_text("")
    symlinked_python = fake_venv_bin / "python"
    symlinked_python.symlink_to(real_interpreter)
    (fake_venv_bin / "spoke").write_text("")

    monkeypatch.setattr(schedule.sys, "executable", str(symlinked_python))
    assert schedule._spoke_bin() == fake_venv_bin / "spoke"


def test_install_writes_a_well_formed_plist_into_the_given_dir(tmp_path):
    agents_dir = tmp_path / "LaunchAgents"
    reg_path = _registry(tmp_path)
    calls = []
    res = schedule.install(
        reg_path, "estate", tmp_path / "config.toml",
        label_prefix="com.example", agents_dir=agents_dir,
        run_launchctl=_fake_launchctl(calls),
    )
    assert res.ok is True
    dest = agents_dir / "com.example.spoke.plist"
    assert dest.exists()

    data = plistlib.loads(dest.read_bytes())
    assert data["Label"] == "com.example.spoke"
    args = data["ProgramArguments"]
    assert args[0]  # an absolute path to the spoke binary
    assert args[0].startswith("/")
    assert "doctor" in args
    assert "--project" in args
    assert args[args.index("--project") + 1] == "estate"
    assert "--config" in args
    assert "--state" in args
    # Everything referenced must be absolute, resolved at install time.
    assert args[args.index("--config") + 1].startswith("/")
    assert args[args.index("--state") + 1].startswith("/")

    assert "EnvironmentVariables" in data
    assert "PATH" in data["EnvironmentVariables"]
    assert data["StartCalendarInterval"][0]["Hour"] == schedule.DEFAULT_HOUR
    assert data["StartCalendarInterval"][0]["Minute"] == schedule.DEFAULT_MINUTE
    assert data["StandardOutPath"] == data["StandardErrorPath"]
    assert data["StandardOutPath"].startswith("/")


def test_install_loads_the_agent_via_launchctl(tmp_path):
    agents_dir = tmp_path / "LaunchAgents"
    reg_path = _registry(tmp_path)
    calls = []
    res = schedule.install(
        reg_path, "estate", tmp_path / "config.toml",
        label_prefix="com.example", agents_dir=agents_dir,
        run_launchctl=_fake_launchctl(calls),
    )
    assert res.ok is True
    assert any(c[:1] == ["load"] for c in calls), calls


def test_install_reports_launchctl_failure_and_is_not_ok(tmp_path):
    agents_dir = tmp_path / "LaunchAgents"
    reg_path = _registry(tmp_path)
    calls = []
    fake = _fake_launchctl(calls, returncode=1, stderr="Bootstrap failed: 5: Input/output error")
    res = schedule.install(
        reg_path, "estate", tmp_path / "config.toml",
        label_prefix="com.example", agents_dir=agents_dir,
        run_launchctl=fake,
    )
    assert res.ok is False
    assert "Bootstrap failed" in res.message
    # The plist was still written -- so a human can see what would run --
    # but the failure to load must not read as success.
    assert (agents_dir / "com.example.spoke.plist").exists()


def test_install_refuses_when_several_projects_and_none_named(tmp_path):
    reg_path = tmp_path / "projects.toml"
    add_project(reg_path, "one", [tmp_path / "one-repo"])
    add_project(reg_path, "two", [tmp_path / "two-repo"])
    calls = []
    with pytest.raises(RegistryError):
        schedule.install(
            reg_path, None, tmp_path / "config.toml",
            label_prefix="com.example", agents_dir=tmp_path / "LaunchAgents",
            run_launchctl=_fake_launchctl(calls),
        )
    assert calls == [], "must refuse before ever touching launchctl"


def test_install_with_one_registered_project_does_not_need_a_name(tmp_path):
    agents_dir = tmp_path / "LaunchAgents"
    reg_path = _registry(tmp_path, name="onlyone")
    calls = []
    res = schedule.install(
        reg_path, None, tmp_path / "config.toml",
        label_prefix="com.example", agents_dir=agents_dir,
        run_launchctl=_fake_launchctl(calls),
    )
    assert res.ok is True
    data = plistlib.loads((agents_dir / "com.example.spoke.plist").read_bytes())
    args = data["ProgramArguments"]
    assert args[args.index("--project") + 1] == "onlyone"


def test_install_with_unknown_project_name_refuses(tmp_path):
    reg_path = _registry(tmp_path, name="estate")
    calls = []
    with pytest.raises(RegistryError):
        schedule.install(
            reg_path, "not-registered", tmp_path / "config.toml",
            label_prefix="com.example", agents_dir=tmp_path / "LaunchAgents",
            run_launchctl=_fake_launchctl(calls),
        )


def test_status_on_a_missing_agent_says_so(tmp_path):
    calls = []
    out = schedule.status(label_prefix="com.example", agents_dir=tmp_path / "LaunchAgents",
                           run_launchctl=_fake_launchctl(calls))
    assert "installed: NO" in out
    assert "com.example.spoke" in out
    assert calls == [], "nothing to query in launchd when no plist was ever written"


def test_status_after_install_reports_loaded_and_never_run(tmp_path):
    agents_dir = tmp_path / "LaunchAgents"
    reg_path = _registry(tmp_path)
    schedule.install(
        reg_path, "estate", tmp_path / "config.toml",
        label_prefix="com.example", agents_dir=agents_dir,
        run_launchctl=_fake_launchctl([]),
    )

    def list_ok(args):
        import subprocess
        return subprocess.CompletedProcess(args, 0, "", "")

    out = schedule.status(label_prefix="com.example", agents_dir=agents_dir,
                           run_launchctl=list_ok)
    assert "installed: yes" in out
    assert "loaded by launchd: yes" in out
    assert "never" in out.lower()


def test_status_distinguishes_installed_but_never_run_from_ran_and_clean(tmp_path):
    """The entire point of --status: 'installed but silent' must not read
    the same as 'ran and found nothing'."""
    agents_dir = tmp_path / "LaunchAgents"
    reg_path = _registry(tmp_path)
    res = schedule.install(
        reg_path, "estate", tmp_path / "config.toml",
        label_prefix="com.example", agents_dir=agents_dir,
        run_launchctl=_fake_launchctl([]),
    )
    state_path = _state_path_from_plist(res.plist_path)

    def list_ok(args):
        import subprocess
        return subprocess.CompletedProcess(args, 0, "", "")

    before = schedule.status(label_prefix="com.example", agents_dir=agents_dir, run_launchctl=list_ok)
    assert "never" in before.lower()

    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"last_run": "2026-09-09", "findings": []}))

    after = schedule.status(label_prefix="com.example", agents_dir=agents_dir, run_launchctl=list_ok)
    assert "2026-09-09" in after
    assert "never" not in after.lower()


def _state_path_from_plist(path):
    data = plistlib.loads(path.read_bytes())
    args = data["ProgramArguments"]
    return __import__("pathlib").Path(args[args.index("--state") + 1])


def test_status_reports_not_loaded_when_launchctl_list_fails(tmp_path):
    agents_dir = tmp_path / "LaunchAgents"
    reg_path = _registry(tmp_path)
    schedule.install(
        reg_path, "estate", tmp_path / "config.toml",
        label_prefix="com.example", agents_dir=agents_dir,
        run_launchctl=_fake_launchctl([]),
    )

    def list_fail(args):
        import subprocess
        return subprocess.CompletedProcess(args, 1, "", "Could not find service")

    out = schedule.status(label_prefix="com.example", agents_dir=agents_dir, run_launchctl=list_fail)
    assert "loaded by launchd: NO" in out


def test_uninstall_removes_the_plist_and_unloads(tmp_path):
    agents_dir = tmp_path / "LaunchAgents"
    reg_path = _registry(tmp_path)
    schedule.install(
        reg_path, "estate", tmp_path / "config.toml",
        label_prefix="com.example", agents_dir=agents_dir,
        run_launchctl=_fake_launchctl([]),
    )
    dest = agents_dir / "com.example.spoke.plist"
    assert dest.exists()

    calls = []
    res = schedule.uninstall(label_prefix="com.example", agents_dir=agents_dir,
                              run_launchctl=_fake_launchctl(calls))
    assert res.ok is True
    assert not dest.exists()
    assert any(c[:1] == ["unload"] for c in calls)
    assert str(dest) in res.message


def test_uninstall_on_a_missing_agent_says_so_and_does_not_error(tmp_path):
    calls = []
    res = schedule.uninstall(label_prefix="com.example", agents_dir=tmp_path / "LaunchAgents",
                              run_launchctl=_fake_launchctl(calls))
    assert res.ok is False
    assert "no agent installed" in res.message.lower()
    assert calls == []


def test_agents_dir_env_override_is_respected(tmp_path, monkeypatch):
    monkeypatch.setenv("SPOKE_LAUNCH_AGENTS_DIR", str(tmp_path / "custom"))
    assert schedule.launch_agents_dir() == tmp_path / "custom"


# -- install record: --status/--uninstall must find an agent installed
# under a non-default --label-prefix without being told the prefix again --


def _list_ok(args):
    import subprocess
    return subprocess.CompletedProcess(args, 0, "", "")


def test_status_with_no_flag_finds_a_non_default_installed_prefix(tmp_path):
    """Reproduces the bug report verbatim: an agent installed with a
    non-default --label-prefix must still be found by a bare `--status`
    with no flag at all -- the whole point of the install record."""
    agents_dir = tmp_path / "LaunchAgents"
    state_dir = tmp_path / "state"
    reg_path = _registry(tmp_path)
    res = schedule.install(
        reg_path, "estate", tmp_path / "config.toml",
        label_prefix="com.example", agents_dir=agents_dir, state_dir=state_dir,
        run_launchctl=_fake_launchctl([]),
    )
    assert res.ok is True

    out = schedule.status(agents_dir=agents_dir, state_dir=state_dir, run_launchctl=_list_ok)
    assert "label com.example.spoke" in out
    assert "installed: yes" in out
    assert "installed: NO" not in out


def test_status_reports_recorded_but_missing_plist_distinctly(tmp_path):
    """A record whose plist has vanished (deleted by hand, or by anything
    other than `schedule --uninstall`) is a different situation from never
    having installed, and must read differently -- never a bare
    'installed: NO' for an agent that was in fact installed."""
    agents_dir = tmp_path / "LaunchAgents"
    state_dir = tmp_path / "state"
    reg_path = _registry(tmp_path)
    res = schedule.install(
        reg_path, "estate", tmp_path / "config.toml",
        label_prefix="com.example", agents_dir=agents_dir, state_dir=state_dir,
        run_launchctl=_fake_launchctl([]),
    )
    res.plist_path.unlink()  # simulate the plist vanishing some other way

    out = schedule.status(agents_dir=agents_dir, state_dir=state_dir, run_launchctl=_list_ok)
    assert "installed: NO" not in out
    assert "RECORDED" in out
    assert "estate" in out
    assert str(res.plist_path) in out


def test_uninstall_with_no_flag_finds_recorded_label(tmp_path):
    agents_dir = tmp_path / "LaunchAgents"
    state_dir = tmp_path / "state"
    reg_path = _registry(tmp_path)
    res = schedule.install(
        reg_path, "estate", tmp_path / "config.toml",
        label_prefix="com.example", agents_dir=agents_dir, state_dir=state_dir,
        run_launchctl=_fake_launchctl([]),
    )
    record_path = schedule.install_record_path("estate", state_dir)
    assert record_path.exists()

    calls = []
    out = schedule.uninstall(agents_dir=agents_dir, state_dir=state_dir,
                              run_launchctl=_fake_launchctl(calls))
    assert out.ok is True
    assert "com.example.spoke" in out.message
    assert not res.plist_path.exists()
    assert not record_path.exists(), "the install record must be removed too"
    assert any(c[:1] == ["unload"] for c in calls)

    # And the round trip is now clean: nothing recorded, nothing installed.
    after = schedule.status(agents_dir=agents_dir, state_dir=state_dir, run_launchctl=_list_ok)
    assert "installed: NO" in after
    assert "RECORDED" not in after


def test_explicit_label_prefix_still_overrides_the_record(tmp_path):
    """An install recorded under one prefix must not leak into a --status
    call that explicitly names a different prefix -- explicit always
    wins, per the module docstring's stated precedence."""
    agents_dir = tmp_path / "LaunchAgents"
    state_dir = tmp_path / "state"
    reg_path = _registry(tmp_path)
    schedule.install(
        reg_path, "estate", tmp_path / "config.toml",
        label_prefix="com.recorded", agents_dir=agents_dir, state_dir=state_dir,
        run_launchctl=_fake_launchctl([]),
    )

    out = schedule.status(label_prefix="com.other", agents_dir=agents_dir, state_dir=state_dir,
                           run_launchctl=_list_ok)
    assert "label com.other.spoke" in out
    assert "installed: NO" in out  # nothing installed under com.other -- the record was ignored


def test_status_refuses_to_guess_between_two_recorded_installs(tmp_path):
    """More than one recorded install with no --project/--label-prefix to
    disambiguate must refuse, not silently pick one -- the same rule
    select_project already applies to project selection."""
    agents_dir = tmp_path / "LaunchAgents"
    state_dir = tmp_path / "state"
    reg_path = tmp_path / "projects.toml"
    add_project(reg_path, "one", [tmp_path / "one-repo"])
    add_project(reg_path, "two", [tmp_path / "two-repo"])
    schedule.install(
        reg_path, "one", tmp_path / "config.toml",
        label_prefix="com.one", agents_dir=agents_dir, state_dir=state_dir,
        run_launchctl=_fake_launchctl([]),
    )
    schedule.install(
        reg_path, "two", tmp_path / "config.toml",
        label_prefix="com.two", agents_dir=agents_dir, state_dir=state_dir,
        run_launchctl=_fake_launchctl([]),
    )

    out = schedule.status(agents_dir=agents_dir, state_dir=state_dir, run_launchctl=_list_ok)
    assert "multiple installs recorded" in out
    assert "one" in out and "two" in out
