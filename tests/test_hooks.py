import json, subprocess, os
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent / "spoke" / "hooks"


def _run(script: str, payload: dict, env: dict) -> subprocess.CompletedProcess:
    e = {**os.environ, **env}
    return subprocess.run(["bash", str(HOOKS / script)], input=json.dumps(payload),
                          capture_output=True, text=True, env=e)


def test_preamble_hook_emits_a_system_message(tmp_path):
    fake = tmp_path / "fake-spoke"
    fake.write_text('#!/bin/bash\necho "ledger: 2 open, 1 blocked"\n')
    fake.chmod(0o755)
    r = _run("ledger-preamble.sh",
             {"transcript_path": str(tmp_path / "proj" / "x.jsonl")},
             {"SPOKE_BIN": str(fake)})
    assert r.returncode == 0
    assert "ledger" in r.stdout
    assert json.loads(r.stdout.strip())["systemMessage"]


def test_preamble_hook_says_so_when_the_tool_is_missing(tmp_path):
    r = _run("ledger-preamble.sh",
             {"transcript_path": str(tmp_path / "proj" / "x.jsonl")},
             {"SPOKE_BIN": str(tmp_path / "does-not-exist")})
    assert r.returncode == 0
    msg = json.loads(r.stdout.strip())["systemMessage"]
    assert "not" in msg.lower()


def test_preamble_hook_resolves_a_directory_to_a_project_before_calling_preamble(tmp_path):
    """The hook must call `projects which <dir>` and pass ITS output as
    --project, not the raw directory -- --project takes a registered NAME."""
    fake = tmp_path / "fake-spoke"
    fake.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = "projects" ] && [ "$2" = "which" ]; then\n'
        '  echo "estate"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "ledger" ] && [ "$2" = "preamble" ]; then\n'
        '  echo "resolved-project-was: $4"\n'
        "  exit 0\n"
        "fi\n"
        "exit 1\n"
    )
    fake.chmod(0o755)
    r = _run("ledger-preamble.sh",
             {"transcript_path": str(tmp_path / "proj" / "x.jsonl")},
             {"SPOKE_BIN": str(fake)})
    assert r.returncode == 0
    msg = json.loads(r.stdout.strip())["systemMessage"]
    assert "resolved-project-was: estate" in msg


def test_preamble_hook_distinguishes_unregistered_directory_from_an_empty_ledger(tmp_path):
    unregistered = tmp_path / "unregistered"
    fake_no_owner = tmp_path / "fake-no-owner"
    fake_no_owner.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = "projects" ] && [ "$2" = "which" ]; then\n'
        '  echo "no single registered project owns $3"\n'
        "  exit 1\n"
        "fi\n"
        "exit 1\n"
    )
    fake_no_owner.chmod(0o755)
    r1 = _run("ledger-preamble.sh",
              {"transcript_path": str(unregistered / "x.jsonl")},
              {"SPOKE_BIN": str(fake_no_owner)})
    assert r1.returncode == 0
    msg1 = json.loads(r1.stdout.strip())["systemMessage"]
    assert "not registered" in msg1.lower() or "no registered project" in msg1.lower()

    fake_empty_ledger = tmp_path / "fake-empty-ledger"
    fake_empty_ledger.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = "projects" ] && [ "$2" = "which" ]; then\n'
        '  echo "estate"\n'
        "  exit 0\n"
        "fi\n"
        'echo "ledger: 0 open items"\n'
    )
    fake_empty_ledger.chmod(0o755)
    r2 = _run("ledger-preamble.sh",
              {"transcript_path": str(tmp_path / "registered" / "x.jsonl")},
              {"SPOKE_BIN": str(fake_empty_ledger)})
    assert r2.returncode == 0
    msg2 = json.loads(r2.stdout.strip())["systemMessage"]
    assert "0 open items" in msg2

    # The two messages must be clearly distinguishable -- "nothing was
    # registered" must never read the same as "genuinely zero open items".
    assert msg1 != msg2
    assert "0 open items" not in msg1


def test_nag_fires_when_commits_happened_but_no_ledger_movement(tmp_path):
    fake = tmp_path / "fake"
    fake.write_text('#!/bin/bash\nexit 0\n'); fake.chmod(0o755)
    r = _run("ledger-nag.sh", {"transcript_path": str(tmp_path / "x.jsonl")},
             {"SPOKE_FAKE_COMMITS": "3", "SPOKE_FAKE_LEDGER_CHANGES": "0"})
    assert r.returncode == 0
    assert "ledger" in json.loads(r.stdout.strip())["systemMessage"].lower()


def test_nag_is_silent_when_nothing_was_committed(tmp_path):
    r = _run("ledger-nag.sh", {"transcript_path": str(tmp_path / "x.jsonl")},
             {"SPOKE_FAKE_COMMITS": "0", "SPOKE_FAKE_LEDGER_CHANGES": "0"})
    assert r.stdout.strip() == ""


def test_nag_is_silent_when_the_ledger_moved(tmp_path):
    r = _run("ledger-nag.sh", {"transcript_path": str(tmp_path / "x.jsonl")},
             {"SPOKE_FAKE_COMMITS": "3", "SPOKE_FAKE_LEDGER_CHANGES": "2"})
    assert r.stdout.strip() == ""
