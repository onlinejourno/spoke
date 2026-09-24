import httpx
from spoke.cli import main


def _clean_store(tmp_path):
    (tmp_path / "MEMORY.md").write_text("- [A](a.md) — hook\n")
    (tmp_path / "a.md").write_text("---\nname: a\ndescription: d\n---\n\nclean\n")
    return tmp_path


def _install_mock_client(monkeypatch):
    """Force _cmd_doctor's httpx.Client onto a MockTransport so no real network call happens."""
    real_client_cls = httpx.Client

    def handler(request):
        return httpx.Response(200)

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client_cls(*args, **kwargs)

    monkeypatch.setattr("spoke.cli.httpx.Client", fake_client)


def test_doctor_clean_store_exits_zero_and_reports_last_run(tmp_path, monkeypatch, capsys):
    _install_mock_client(monkeypatch)
    monkeypatch.setenv("SPOKE_STORE_PATH", str(_clean_store(tmp_path)))
    state = tmp_path / "state.json"
    assert main(["doctor", "--state", str(state)]) == 0
    out = capsys.readouterr().out
    assert "previous run (none recorded)" in out
    assert "0 open finding" in out
    assert state.exists()


def test_doctor_reports_only_new_defects_across_runs(tmp_path, monkeypatch, capsys):
    _install_mock_client(monkeypatch)
    d = _clean_store(tmp_path)
    monkeypatch.setenv("SPOKE_STORE_PATH", str(d))
    state = tmp_path / "state.json"

    assert main(["doctor", "--state", str(state)]) == 0
    capsys.readouterr()

    (d / "a.md").write_text("---\nname: a\ndescription: d\n---\n\n[[ghost]]\n")
    assert main(["doctor", "--state", str(state)]) == 1
    out = capsys.readouterr().out
    assert "NEW      broken-wikilink" in out
    assert "ghost" in out

    # Same defect, second run: no longer new, but still counted, and the
    # previous run's date is now visible in the output.
    assert main(["doctor", "--state", str(state)]) == 0
    out2 = capsys.readouterr().out
    assert "NEW" not in out2
    assert "1 open finding" in out2
    assert "previous run" in out2 and "(none recorded)" not in out2


def test_doctor_survives_corrupt_state_file(tmp_path, monkeypatch, capsys):
    _install_mock_client(monkeypatch)
    monkeypatch.setenv("SPOKE_STORE_PATH", str(_clean_store(tmp_path)))
    state = tmp_path / "state.json"
    state.write_text("not json at all {{{")

    assert main(["doctor", "--state", str(state)]) == 0
    out = capsys.readouterr().out
    assert "NOTE:" in out
    assert str(state) in out


# --- notify --------------------------------------------------------------
#
# `--config` always points at a nonexistent file in these tests, so the
# real (gitignored) config.toml at the repo root -- which has no [notify]
# section, but there is no reason to depend on that -- can never leak a
# topic into a test that wants notification off. SPOKE_NTFY_TOPIC is set
# or deliberately left unset instead.

def _no_config(tmp_path):
    return str(tmp_path / "no-such-config.toml")


def _install_dispatching_client(monkeypatch, handler):
    """Like _install_mock_client, but the handler sees the request and can
    respond differently to the doctor run's probe traffic (HEAD/GET) than
    to a notify POST -- needed once a single `doctor` run can make both."""
    real_client_cls = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client_cls(*args, **kwargs)

    monkeypatch.setattr("spoke.cli.httpx.Client", fake_client)


def test_doctor_with_no_topic_configured_states_the_absence(tmp_path, monkeypatch, capsys):
    _install_mock_client(monkeypatch)
    monkeypatch.delenv("SPOKE_NTFY_TOPIC", raising=False)
    monkeypatch.setenv("SPOKE_STORE_PATH", str(_clean_store(tmp_path)))
    state = tmp_path / "state.json"
    assert main(["doctor", "--state", str(state), "--config", _no_config(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "notify: not configured" in out


def test_doctor_sends_exactly_one_notification_for_new_findings(tmp_path, monkeypatch, capsys):
    posts = []

    def handler(request):
        if request.method == "POST":
            posts.append(request)
        return httpx.Response(200)

    _install_dispatching_client(monkeypatch, handler)
    monkeypatch.setenv("SPOKE_NTFY_TOPIC", "test-topic")
    d = _clean_store(tmp_path)
    monkeypatch.setenv("SPOKE_STORE_PATH", str(d))
    state = tmp_path / "state.json"

    # First run: store is clean, nothing new -- no notification.
    assert main(["doctor", "--state", str(state), "--config", _no_config(tmp_path)]) == 0
    capsys.readouterr()
    assert posts == []

    # A defect appears: exactly one POST, carrying the project and the count.
    (d / "a.md").write_text("---\nname: a\ndescription: d\n---\n\n[[ghost]]\n")
    rc = main(["doctor", "--state", str(state), "--config", _no_config(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert len(posts) == 1
    body = posts[0].content.decode("utf-8")
    assert "1 new defect" in body
    assert "ghost" in body
    assert "notify: sent" in out


def test_doctor_sends_nothing_when_zero_new_findings(tmp_path, monkeypatch, capsys):
    posts = []

    def handler(request):
        if request.method == "POST":
            posts.append(request)
        return httpx.Response(200)

    _install_dispatching_client(monkeypatch, handler)
    monkeypatch.setenv("SPOKE_NTFY_TOPIC", "test-topic")
    monkeypatch.setenv("SPOKE_STORE_PATH", str(_clean_store(tmp_path)))
    state = tmp_path / "state.json"

    assert main(["doctor", "--state", str(state), "--config", _no_config(tmp_path)]) == 0
    assert posts == [], "a clean run with zero new findings must never send a notification"


def test_doctor_notify_failure_is_visible_and_forces_nonzero_exit(tmp_path, monkeypatch, capsys):
    """A topic IS configured, and there IS something new to report -- so a
    send is attempted -- but the send itself fails (HTTP 500). That failure
    must be printed, naming the reason, and must force `doctor` non-zero:
    a watcher that cannot reach anyone must never report success, even
    though the underlying store's own defects are exactly what the send
    was trying to report (i.e. failure here is on the notify path, not
    hidden behind or confused with a dirty store)."""
    def handler(request):
        if request.method == "POST":
            return httpx.Response(500)
        return httpx.Response(200)

    _install_dispatching_client(monkeypatch, handler)
    monkeypatch.setenv("SPOKE_NTFY_TOPIC", "test-topic")
    d = _clean_store(tmp_path)
    monkeypatch.setenv("SPOKE_STORE_PATH", str(d))
    (d / "a.md").write_text("---\nname: a\ndescription: d\n---\n\n[[ghost]]\n")
    state = tmp_path / "state.json"

    rc = main(["doctor", "--state", str(state), "--config", _no_config(tmp_path)])
    out = capsys.readouterr().out
    assert rc != 0
    assert "notify: FAILED" in out
    assert "500" in out
