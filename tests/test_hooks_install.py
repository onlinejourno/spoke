"""`spoke hooks` -- the install seam the hook scripts never had.

The scripts were tested (tests/test_hooks.py) and reachable by nobody:
no install command beside `serve --install` and `mcp --install`, and
no README mention. These tests are about the seam, not the scripts.
"""
from __future__ import annotations

import json

from spoke import hooks_install as hk
from spoke.cli import main


def _settings(path, data):
    path.write_text(json.dumps(data))
    return path


def test_plan_adds_both_hooks_and_leaves_other_hooks_alone():
    theirs = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/theirs.sh"}]}]}}
    out = hk.plan(theirs)
    assert hk.installed(out) == {"SessionStart": True, "Stop": True}
    stop_cmds = [h["command"] for e in out["hooks"]["Stop"] for h in e["hooks"]]
    assert "/theirs.sh" in stop_cmds, "installing ours must not drop theirs"


def test_plan_is_idempotent():
    once = hk.plan({})
    twice = hk.plan(once)
    assert once == twice


def test_remove_takes_out_only_ours():
    theirs = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/theirs.sh"}]}]}}
    both = hk.plan(theirs)
    back = hk.remove(both)
    assert back == theirs
    assert hk.installed(back) == {"SessionStart": False, "Stop": False}


def test_remove_on_a_file_without_ours_changes_nothing():
    theirs = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/theirs.sh"}]}]}}
    assert hk.remove(theirs) == theirs


def test_install_writes_and_then_reports_what_is_true(tmp_path, capsys):
    settings = tmp_path / "settings.json"
    rc = main(["hooks", "--install", "--settings", str(settings)])
    out = capsys.readouterr().out
    assert f"hooks: wrote {settings}" in out
    assert "SessionStart registered" in out and "Stop registered" in out
    written = json.loads(settings.read_text())
    assert hk.installed(written) == {"SessionStart": True, "Stop": True}
    # rc reflects whether the hooks CAN RUN, which depends on jq
    assert rc == (0 if hk.jq_path() else 1)


def test_install_twice_says_nothing_changed(tmp_path, capsys):
    settings = tmp_path / "settings.json"
    main(["hooks", "--install", "--settings", str(settings)])
    capsys.readouterr()
    main(["hooks", "--install", "--settings", str(settings)])
    assert "already says this; nothing changed" in capsys.readouterr().out


def test_dry_run_prints_and_writes_nothing(tmp_path, capsys):
    settings = tmp_path / "settings.json"
    rc = main(["hooks", "--install", "--dry-run", "--settings", str(settings)])
    assert rc == 0
    assert not settings.exists()
    assert "dry run" in capsys.readouterr().out


def test_a_settings_file_that_cannot_be_parsed_is_never_rewritten(tmp_path, capsys):
    """Rewriting a file this could not read would discard every other
    hook and setting in it."""
    settings = tmp_path / "settings.json"
    settings.write_text("{ not json")
    rc = main(["hooks", "--install", "--settings", str(settings)])
    assert rc == 2
    assert "could not read" in capsys.readouterr().out
    assert settings.read_text() == "{ not json"


def test_status_says_when_jq_is_missing(tmp_path, capsys, monkeypatch):
    """A hook that cannot run reports nothing, and silence looks exactly
    like 'nothing to report'. So the status must go red without jq, and
    say why, rather than reporting 'registered' and stopping."""
    settings = _settings(tmp_path / "settings.json", hk.plan({}))
    monkeypatch.setattr(hk, "jq_path", lambda: None)
    rc = main(["hooks", "--status", "--settings", str(settings)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "jq" in out and "NOT on PATH" in out


def test_exactly_one_action_is_required(capsys):
    assert main(["hooks"]) == 2
    assert "exactly one of" in capsys.readouterr().out


def test_a_hand_written_registration_with_an_env_prefix_is_recognised_as_ours():
    """Found by pointing the dry run at a real settings file: both hooks
    were already registered, wrapped as `SPOKE_BIN=... script`, and the
    installer -- matching the command string exactly -- reported NOT
    registered and would have appended a second, poorer copy."""
    by_hand = {"hooks": {
        "SessionStart": [{"hooks": [{"type": "command",
            "command": "SPOKE_BIN=/x/.venv/bin/spoke " + hk.hook_command("ledger-preamble.sh"),
            "timeout": 10, "statusMessage": "Checking ledger..."}]}],
        "Stop": [{"hooks": [{"type": "command",
            "command": "SPOKE_BIN=/x/.venv/bin/spoke " + hk.hook_command("ledger-nag.sh")}]}],
    }}
    assert hk.installed(by_hand) == {"SessionStart": True, "Stop": True}
    assert hk.plan(by_hand) == by_hand, "nothing to add; must not duplicate"
    assert hk.remove(by_hand) == {}, "and remove must find them too"


def test_our_own_entry_carries_env_timeout_and_status():
    """A hook runs under the client's PATH, not the shell's, so without
    SPOKE_BIN the script says 'binary not found' every session. And a
    hung store must not hang a session start."""
    out = hk.plan({})
    for event in ("SessionStart", "Stop"):
        h = out["hooks"][event][0]["hooks"][0]
        assert h["command"].startswith("SPOKE_BIN=")
        assert h["timeout"] == 10
        assert h["statusMessage"]
